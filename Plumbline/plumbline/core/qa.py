"""Data quality checks for survey points - the things that bite you after the field crew leaves."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from .featurecodes import parse_description
from . import reference as REF


@dataclass
class Issue:
    severity: str                  # error | warn | info
    kind: str
    message: str
    point_ids: list = field(default_factory=list)


def _is_deleted_point(point) -> bool:
    """Recognize legacy/import deletion markers without treating them as live survey data."""
    attrs = getattr(point, "attrs", None) or {}
    if not isinstance(attrs, dict):
        return False
    if any(attrs.get(key) is True for key in ("deleted", "is_deleted", "qa_deleted")):
        return True
    return any(str(attrs.get(key, "")).strip().casefold() in {"deleted", "removed"}
               for key in ("status", "fieldwork_status", "qa_status"))


def run_checks(project, z_tol: float = 0.05, xy_tol: float = 0.01, spike_k: float = 6.0,
               spike_min: float | None = None, neighbours: int = 8) -> list[Issue]:
    issues: list[Issue] = []
    # The checks are about this job's field data.  Stake-out, control and other reference points
    # share the drawing and share the point store, but they are somebody else's coordinates: they
    # are not duplicates of our point 12, they are not our spikes and they are not our blunders.
    # They are counted, named, and left alone (see core/reference.py).
    pts = REF.survey_points(project)
    n_ref = len(project.points) - len(pts)
    note = (f"{n_ref:,} reference point(s) (stake-out / control / other) are on the drawing and were "
            f"not checked - they are not this job's field data.") if n_ref else ""
    if not pts:
        out = [Issue("info", "empty", "The project has no field points.")]
        if note:
            out.append(Issue("info", "reference", note))
        return out

    # 1. duplicate numbers
    seen: dict[str, list] = {}
    for p in pts:
        seen.setdefault(p.number, []).append(p.id)
    dups = {n: ids for n, ids in seen.items() if len(ids) > 1}
    if dups:
        ids = [i for v in dups.values() for i in v]
        issues.append(Issue("error", "duplicate-number",
                            f"{len(dups)} point number(s) are used more than once (e.g. {next(iter(dups))}).", ids))

    ids = np.array([p.id for p in pts])
    xyz = np.array([[p.x, p.y, p.z] for p in pts])

    # 2. coincident points
    tree = cKDTree(xyz[:, :2])
    pairs = tree.query_pairs(max(xy_tol, 1e-9), output_type="ndarray")
    if len(pairs):
        zi, zj = xyz[pairs[:, 0], 2], xyz[pairs[:, 1], 2]
        both = np.isfinite(zi) & np.isfinite(zj)
        conflict = both & (np.abs(zi - zj) > z_tol)
        same = both & ~conflict
        if conflict.any():
            pid = ids[np.unique(pairs[conflict])].tolist()
            worst = float(np.abs(zi - zj)[conflict].max())
            issues.append(Issue("error", "coincident-conflict",
                                f"{int(conflict.sum())} pair(s) of points share a location but differ in elevation "
                                f"(up to {worst:.3f}).", pid))
        if same.any():
            issues.append(Issue("info", "coincident-duplicate",
                                f"{int(same.sum())} pair(s) of points are exact duplicates (same location and elevation).",
                                ids[np.unique(pairs[same])].tolist()))

    # 3. missing elevations
    nz = ~np.isfinite(xyz[:, 2])
    if nz.any():
        issues.append(Issue("warn", "no-elevation", f"{int(nz.sum())} point(s) have no elevation.", ids[nz].tolist()))

    # 4. unknown feature codes
    unknown: dict[str, list] = {}
    commands = project.settings.get("f2f_commands")
    for p in pts:
        # A point explicitly removed by a fieldwork import/legacy project is not an
        # active description error, even if it remains in the serialized point store.
        if _is_deleted_point(p):
            continue
        pd = parse_description(p.desc, commands=commands, known_codes=project.codes.codes)
        if pd.code and project.codes.get(pd.code) is None:
            unknown.setdefault(pd.code, []).append(p.id)
    if unknown:
        top = ", ".join(f"{c} ({len(v)})" for c, v in sorted(unknown.items(), key=lambda kv: -len(kv[1]))[:8])
        issues.append(Issue("info", "unknown-code", f"{len(unknown)} description code(s) are not in the feature-code table: {top}.",
                            [i for v in unknown.values() for i in v]))

    # 5. outside the CRS area of use (usually the wrong CRS or swapped N/E)
    try:
        ok, total = project.crs.area_of_use_ok(xyz[:, 0], xyz[:, 1])
        if total and ok < total:
            sev = "error" if ok < 0.5 * total else "warn"
            issues.append(Issue(sev, "outside-crs",
                                f"{total - ok} of {total} point(s) fall outside the area of use of {project.crs.authority} "
                                f"- wrong CRS, wrong units or swapped northing/easting?"))
    except Exception:
        pass

    # 6. isolated points (typo'd coordinates)
    if len(pts) >= 20:
        d, _ = tree.query(xyz[:, :2], k=2)
        nn = d[:, 1]
        med = float(np.median(nn)) or 1e-9
        iso = nn > max(40 * med, 1e-6)
        if 0 < iso.sum() <= max(3, len(pts) // 50):
            issues.append(Issue("warn", "isolated",
                                f"{int(iso.sum())} point(s) are very far from every other point (typo in a coordinate?).",
                                ids[iso].tolist()))

    # 7. elevation spikes (busts): compare each ground shot with the surface its neighbours imply.
    #    The prediction is the MEDIAN of the planes through every triple of neighbours, so one bad shot
    #    next door cannot drag its neighbours into the report (a least-squares plane would).
    ok = np.isfinite(xyz[:, 2])
    ground = np.array([(project.codes.get(parse_description(
        p.desc, commands=commands, known_codes=project.codes.codes).code) is None or
        project.codes.get(parse_description(
            p.desc, commands=commands, known_codes=project.codes.codes).code).ground) for p in pts])
    use = ok & ground
    if use.sum() >= neighbours + 2:
        if spike_min is None:
            from . import units as U
            spike_min = 0.23 / U.M_PER_UNIT.get(project.v_unit, 1.0)          # 0.75 ft expressed in project units
        sub_ids, sub = ids[use], xyz[use]
        res = _spike_residuals(sub, neighbours)
        v = res[np.isfinite(res)]
        if len(v) > 10:
            mad = 1.4826 * float(np.median(np.abs(v - np.median(v)))) or 1e-9
            thr = max(spike_min, spike_k * mad)
            bad = np.isfinite(res) & (np.abs(res) > thr)
            if bad.any():
                worst = sub_ids[bad][np.argsort(-np.abs(res[bad]))][:5]
                nums = ", ".join(project.points[int(i)].number for i in worst if int(i) in project.points)
                issues.append(Issue("warn", "spike",
                                    f"{int(bad.sum())} possible elevation bust(s): elevation differs from its neighbours' surface by more "
                                    f"than {thr:.2f} (largest {np.nanmax(np.abs(res[bad])):.2f}; worst points {nums}).",
                                    sub_ids[bad].tolist()))
    if not issues:
        issues.append(Issue("info", "ok", "No problems found."))
    if note:
        issues.append(Issue("info", "reference", note))
    return issues


def _spike_residuals(xyz: np.ndarray, k: int = 8, chunk: int = 3000) -> np.ndarray:
    """z - (median plane prediction from neighbour triples); NaN where the point is not surrounded."""
    n = len(xyz)
    kk = min(k + 1, n)
    tree = cKDTree(xyz[:, :2])
    dist, nb = tree.query(xyz[:, :2], k=kk)
    nb = nb[:, 1:]
    kk -= 1
    trip = np.array(list(itertools.combinations(range(kk), 3)))
    res = np.full(n, np.nan)
    for s in range(0, n, chunk):
        e = min(n, s + chunk)
        P = xyz[nb[s:e]]                                   # (c, K, 3)
        xi, yi = xyz[s:e, 0][:, None], xyz[s:e, 1][:, None]
        # surrounded test: neighbours must wrap around the point (no extrapolation at the edge of the data)
        ang = np.sort(np.arctan2(P[:, :, 1] - yi, P[:, :, 0] - xi), axis=1)
        gaps = np.diff(np.concatenate([ang, ang[:, :1] + 2 * np.pi], axis=1), axis=1)
        inside = gaps.max(axis=1) <= np.pi
        p0, p1, p2 = P[:, trip[:, 0]], P[:, trip[:, 1]], P[:, trip[:, 2]]
        u, v = p1 - p0, p2 - p0
        nx = u[..., 1] * v[..., 2] - u[..., 2] * v[..., 1]
        ny = u[..., 2] * v[..., 0] - u[..., 0] * v[..., 2]
        nz = u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]
        norm = np.sqrt(nx * nx + ny * ny + nz * nz)
        with np.errstate(divide="ignore", invalid="ignore"):
            zp = p0[..., 2] - (nx * (xi - p0[..., 0]) + ny * (yi - p0[..., 1])) / nz
            zp[(np.abs(nz) < 0.15 * norm) | ~np.isfinite(zp)] = np.nan     # skinny / near-vertical triples
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(zp, axis=1)
        r = xyz[s:e, 2] - med
        r[~inside] = np.nan
        res[s:e] = r
    return res
