"""Breakline crossing, elevation-conflict, line-length, and segment command QA (Qt-free).

Implements Phase 2 of ``docs/GEOMETRY_SEGMENTS_PLAN.md``:

1. **All-crossings breakline detector** (:func:`detect_breakline_crossings`):
   - Corpus is every visible polyline flagged as a breakline (``entity.kind == "breakline"``
     or feature code ``kind == "breakline"``), regardless of whether it was generated from
     points or imported.
   - Bulge-aware flattening via :func:`plumbline.core.geometry.bulge_to_arc` so curved arcs
     are tested at true arc geometry.
   - **2D / z=0 elevation-less semantics**: polylines sitting at ``z == 0`` (or ``NaN``)
     pull elevations from linked survey points when available; if still elevation-less,
     a crossing in plan is always reported as ``plan-crossing`` while ``z-conflict`` is
     raised only when both lines carry meaningful non-zero elevation at the crossing.
   - Never early-exits: if a job has *N* crossings, all *N* are returned.

2. **Per-segment / per-span length threshold** (:func:`detect_long_segments`):
   - Flags spans or segments exceeding ``max_segment_length`` (defaults to ``300.0`` for
     ``ft`` / ``ftUS`` and ``100.0`` for metric ``m`` jobs, configurable via
     ``project.settings["max_segment_length"]``).

3. **Start/end & curve command sequence surfacing** (:func:`detect_segment_command_issues`):
   - Surfaces ``Missing ST``, ``Missing END``, ``Missing PC``, and ``Missing PT`` findings
     linked to segments and survey points with one-click repair proposals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import TYPE_CHECKING

import numpy as np

from . import geometry as G
from .geometry_segments import Segment, build_segments
from .model import Polyline

if TYPE_CHECKING:
    from .project import Project

try:
    from shapely import linestrings as _shp_linestrings
    from shapely.strtree import STRtree as _STRtree
    _HAS_SHAPELY = True
except Exception:  # pragma: no cover - fallback when shapely is unavailable
    _shp_linestrings = None
    _STRtree = None
    _HAS_SHAPELY = False


DEFAULT_MAX_SEGMENT_LENGTH_FT = 300.0
DEFAULT_MAX_SEGMENT_LENGTH_M = 100.0
_MEANINGFUL_Z_EPS = 1e-9


def is_meaningful_z(z: float) -> bool:
    """Return True when *z* is finite and non-zero (2D lines at z=0 are elevation-less)."""
    try:
        zf = float(z)
    except (TypeError, ValueError):
        return False
    return math.isfinite(zf) and abs(zf) > _MEANINGFUL_Z_EPS


def default_max_segment_length(project: "Project | None" = None) -> float:
    """Return the configured or unit-appropriate default maximum segment/span length."""
    if project is not None:
        settings = getattr(project, "settings", None) or {}
        for key in ("max_segment_length", "line_too_long_threshold"):
            raw = settings.get(key)
            if raw is not None:
                try:
                    val = float(raw)
                    if math.isfinite(val) and val > 0.0:
                        return val
                except (TypeError, ValueError):
                    pass
        crs = getattr(project, "crs", None)
        units = str(
            getattr(project, "units", None)
            or getattr(crs, "units", None)
            or getattr(project, "h_unit", None)
            or getattr(crs, "unit", None)
            or settings.get("units")
            or "ftUS"
        ).strip().lower()
        if units in ("m", "meter", "meters", "metre", "metres"):
            return DEFAULT_MAX_SEGMENT_LENGTH_M
    return DEFAULT_MAX_SEGMENT_LENGTH_FT


@dataclass
class CrossingFinding:
    """One breakline crossing or elevation conflict between two spans."""

    kind: str                          # "plan-crossing" | "z-conflict"
    severity: str                      # "error" | "warning"
    seg_a_id: str
    seg_b_id: str
    entity_a_id: int
    entity_b_id: int
    span_a: int                        # 0-based original vertex span index on entity A
    span_b: int                        # 0-based original vertex span index on entity B
    x: float
    y: float
    za: float = math.nan               # elevation on A at crossing (NaN if elevation-less)
    zb: float = math.nan               # elevation on B at crossing (NaN if elevation-less)
    dz: float = math.nan               # |za - zb| when both carry meaningful Z, else NaN
    is_self_crossing: bool = False
    point_ids: list[int] = field(default_factory=list)
    point_numbers: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def has_z_conflict(self) -> bool:
        return self.kind == "z-conflict"


@dataclass
class LongSegmentFinding:
    """A segment span (or whole segment) whose length exceeds the threshold."""

    seg_id: str
    entity_id: int
    code: str
    layer: str
    span_index: int                    # 0-based span index
    length_2d: float
    length_3d: float
    threshold: float
    x: float                           # midpoint X for zoom/focus
    y: float                           # midpoint Y for zoom/focus
    z: float = 0.0
    point_ids: list[int] = field(default_factory=list)
    point_numbers: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class SegmentCommandFinding:
    """A start/end or curve command sequence issue linked to a segment/point."""

    issue_type: str                    # "Missing ST" | "Missing END" | "Missing PC" | "Missing PT" | ...
    severity: str                      # "error" | "warning"
    seg_id: str
    entity_id: int | None
    code: str
    point_id: int | None
    point_number: str
    x: float
    y: float
    z: float
    raw_description: str
    proposal: str = ""
    detail: str = ""


@dataclass
class GeometryIssue:
    """Unified row representation for UI tables and reports."""

    category: str                      # "z-conflict" | "plan-crossing" | "line-too-long" | "command-sequence"
    severity: str                      # "error" | "warning"
    seg_ids: list[str]
    entity_ids: list[int]
    point_ids: list[int]
    point_numbers: list[str]
    x: float
    y: float
    z: float
    title: str
    detail: str
    proposal: str = ""
    target_point_id: int | None = None


@dataclass
class GeometryCheckReport:
    """Complete result of running Phase 2 geometry checks on a project."""

    crossings: list[CrossingFinding] = field(default_factory=list)
    long_segments: list[LongSegmentFinding] = field(default_factory=list)
    command_issues: list[SegmentCommandFinding] = field(default_factory=list)
    issues: list[GeometryIssue] = field(default_factory=list)
    breakline_count: int = 0
    max_segment_length: float = DEFAULT_MAX_SEGMENT_LENGTH_FT


def _entity_is_visible(project: "Project", entity: Polyline, hidden_ids: set) -> bool:
    if entity.id in hidden_ids:
        return False
    if not getattr(entity, "visible", True):
        return False
    layers = getattr(project, "layers", None) or {}
    lay = layers.get(entity.layer)
    if lay is not None and not getattr(lay, "visible", True):
        return False
    return True


def _is_breakline_segment(project: "Project", seg: Segment, entity: Polyline) -> bool:
    """Return True if *entity* / *seg* is part of the breakline crossing corpus."""
    if getattr(entity, "kind", "") == "breakline" or getattr(seg, "kind", "") == "breakline":
        return True
    attrs = getattr(entity, "attrs", None) or {}
    if bool(attrs.get("breakline", False)):
        return True
    if seg.code and hasattr(project, "codes"):
        fc = project.codes.get(seg.code)
        if fc is not None and (
            getattr(fc, "kind", "") == "breakline" or bool(getattr(fc, "breakline", False))
        ):
            return True
    return False


def resolve_polyline_vertex_elevations(
    verts: np.ndarray,
    point_ids: tuple[int | None, ...] | list[int | None] | None = None,
    points_by_id: dict | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Resolve vertex elevations using 2D z=0 elevation-less semantics.

    Returns ``(z_resolved, has_meaningful_z_mask)`` of shape ``(N,)``:
    - Any vertex with finite ``|z| > 1e-9`` keeps its own elevation.
    - Any vertex with ``z == 0`` or ``NaN`` checks its linked :class:`SurveyPoint`
      (when available); if that point has finite ``|z| > 1e-9``, the point's
      elevation is used.
    - If at least one vertex on the polyline has a meaningful elevation, remaining
      elevation-less vertices are linearly interpolated along cumulative 2D chord
      length (matching surface breakline draping).
    - If *no* vertex on the polyline has a meaningful elevation (a pure 2D / ``z=0``
      polyline), ``has_meaningful_z_mask`` is all ``False`` and ``z_resolved`` is ``NaN``.
    """
    v = np.asarray(verts, dtype=float)
    n = len(v)
    if n == 0:
        return np.empty(0, dtype=float), np.zeros(0, dtype=bool)

    z_work = np.full(n, np.nan, dtype=float)
    known = np.zeros(n, dtype=bool)
    raw_z = v[:, 2] if v.ndim == 2 and v.shape[1] >= 3 else np.zeros(n, dtype=float)

    for i in range(n):
        zi = float(raw_z[i])
        if is_meaningful_z(zi):
            z_work[i] = zi
            known[i] = True
        elif point_ids and points_by_id and i < len(point_ids):
            pid = point_ids[i]
            pt = points_by_id.get(pid) if pid is not None else None
            if pt is not None and is_meaningful_z(getattr(pt, "z", 0.0)):
                z_work[i] = float(pt.z)
                known[i] = True

    if not known.any():
        return z_work, known

    if not known.all() and n >= 2:
        d = np.hypot(np.diff(v[:, 0]), np.diff(v[:, 1]))
        s = np.concatenate([[0.0], np.cumsum(d)])
        if s[-1] > 0.0:
            z_work = np.interp(s, s[known], z_work[known])
        else:
            z_work[:] = z_work[known][0]
        known[:] = True

    return z_work, known


@dataclass
class _FlatSubSeg:
    """One flattened 2D sub-segment belonging to a breakline polyline span."""

    poly_idx: int
    span_idx: int
    orig_i: int
    orig_j: int
    t0: float
    t1: float
    p0: np.ndarray                     # (x, y)
    p1: np.ndarray                     # (x, y)
    z0: float
    z1: float
    has_z: bool


def _flatten_breakline(
    poly_idx: int,
    verts: np.ndarray,
    bulges: np.ndarray | None,
    closed: bool,
    z_resolved: np.ndarray,
    has_z_mask: np.ndarray,
    max_dev: float = 0.02,
) -> list[_FlatSubSeg]:
    """Flatten a polyline into sub-segments while preserving original span parameters."""
    v = np.asarray(verts, dtype=float)
    n = len(v)
    if n < 2:
        return []
    b = np.zeros(n, dtype=float) if bulges is None or len(bulges) == 0 else np.asarray(bulges, dtype=float)
    n_spans = n if closed else n - 1
    out: list[_FlatSubSeg] = []

    for s_idx in range(n_spans):
        i = s_idx
        j = (s_idx + 1) % n
        p_start = v[i, :2]
        p_end = v[j, :2]
        chord = float(math.hypot(p_end[0] - p_start[0], p_end[1] - p_start[1]))
        if chord <= 1e-12:
            continue
        z_start = float(z_resolved[i])
        z_end = float(z_resolved[j])
        span_has_z = bool(has_z_mask[i] and has_z_mask[j] and math.isfinite(z_start) and math.isfinite(z_end))
        bi = float(b[i]) if i < len(b) else 0.0

        if abs(bi) > 1e-12:
            sub_v = np.array(
                [[p_start[0], p_start[1], 0.0], [p_end[0], p_end[1], 0.0]],
                dtype=float,
            )
            arc_pts = G.flatten_polyline(sub_v, np.array([bi, 0.0]), closed=False, max_dev=max_dev)
            arc_xy = np.asarray(arc_pts[:, :2], dtype=float)
            m = len(arc_xy) - 1
            if m >= 1:
                d = np.hypot(np.diff(arc_xy[:, 0]), np.diff(arc_xy[:, 1]))
                cum = np.concatenate([[0.0], np.cumsum(d)])
                total = float(cum[-1]) if cum[-1] > 0.0 else float(m)
                for k in range(m):
                    t0 = float(cum[k] / total) if cum[-1] > 0.0 else float(k) / float(m)
                    t1 = float(cum[k + 1] / total) if cum[-1] > 0.0 else float(k + 1) / float(m)
                    zk0 = z_start + t0 * (z_end - z_start) if span_has_z else math.nan
                    zk1 = z_start + t1 * (z_end - z_start) if span_has_z else math.nan
                    out.append(
                        _FlatSubSeg(
                            poly_idx=poly_idx,
                            span_idx=s_idx,
                            orig_i=i,
                            orig_j=j,
                            t0=t0,
                            t1=t1,
                            p0=arc_xy[k].copy(),
                            p1=arc_xy[k + 1].copy(),
                            z0=zk0,
                            z1=zk1,
                            has_z=span_has_z,
                        )
                    )
                continue

        out.append(
            _FlatSubSeg(
                poly_idx=poly_idx,
                span_idx=s_idx,
                orig_i=i,
                orig_j=j,
                t0=0.0,
                t1=1.0,
                p0=p_start.copy(),
                p1=p_end.copy(),
                z0=z_start if span_has_z else math.nan,
                z1=z_end if span_has_z else math.nan,
                has_z=span_has_z,
            )
        )
    return out


def _candidate_subseg_pairs(subsegs: list[_FlatSubSeg], tol: float) -> list[tuple[int, int]]:
    """Return candidate pairs ``(i, j)`` with ``i < j`` whose 2D bounds intersect."""
    m = len(subsegs)
    if m < 2:
        return []

    coords = np.empty((m, 2, 2), dtype=float)
    for idx, s in enumerate(subsegs):
        coords[idx, 0, 0] = s.p0[0]
        coords[idx, 0, 1] = s.p0[1]
        coords[idx, 1, 0] = s.p1[0]
        coords[idx, 1, 1] = s.p1[1]

    if _HAS_SHAPELY and m >= 16:
        lines = _shp_linestrings(coords)
        tree = _STRtree(lines)
        left, right = tree.query(lines, predicate="intersects")
        mask = left < right
        if not np.any(mask):
            return []
        return list(zip(left[mask].tolist(), right[mask].tolist()))

    xmin = np.minimum(coords[:, 0, 0], coords[:, 1, 0]) - tol
    xmax = np.maximum(coords[:, 0, 0], coords[:, 1, 0]) + tol
    ymin = np.minimum(coords[:, 0, 1], coords[:, 1, 1]) - tol
    ymax = np.maximum(coords[:, 0, 1], coords[:, 1, 1]) + tol
    order = np.argsort(xmin, kind="mergesort")
    xmin_s = xmin[order]
    xmax_s = xmax[order]
    ymin_s = ymin[order]
    ymax_s = ymax[order]
    pairs: list[tuple[int, int]] = []
    for pos in range(m):
        i_orig = int(order[pos])
        hi = np.searchsorted(xmin_s, xmax_s[pos], side="right")
        if hi <= pos + 1:
            continue
        cand_slice = slice(pos + 1, hi)
        ymask = (ymin_s[cand_slice] <= ymax_s[pos]) & (ymax_s[cand_slice] >= ymin_s[pos])
        for j_orig in order[cand_slice][ymask]:
            j_int = int(j_orig)
            pairs.append((i_orig, j_int) if i_orig < j_int else (j_int, i_orig))
    pairs.sort()
    return pairs


def detect_breakline_crossings(
    project: "Project",
    *,
    segments: list[Segment] | None = None,
    z_tol: float = 0.01,
    xy_tol: float = 1e-4,
    max_dev: float = 0.02,
) -> list[CrossingFinding]:
    """Detect all breakline plan crossings and 3D elevation conflicts in *project*.

    Never early-exits: returns every crossing found across all breaklines.
    """
    segs = build_segments(project) if segments is None else list(segments)
    points_by_id = getattr(project, "points", {}) or {}
    entities_by_id = getattr(project, "entities", {}) or {}
    hidden_ids = project.hidden_ids() if hasattr(project, "hidden_ids") else set()

    breaklines: list[tuple[Segment, Polyline, np.ndarray, np.ndarray]] = []
    for seg in segs:
        ent = entities_by_id.get(seg.entity_id)
        if not isinstance(ent, Polyline) or len(ent.verts) < 2:
            continue
        if not _entity_is_visible(project, ent, hidden_ids):
            continue
        if not _is_breakline_segment(project, seg, ent):
            continue
        z_res, has_z = resolve_polyline_vertex_elevations(
            ent.verts, seg.point_ids, points_by_id
        )
        breaklines.append((seg, ent, z_res, has_z))

    if not breaklines:
        return []

    subsegs: list[_FlatSubSeg] = []
    for poly_idx, (_seg, ent, z_res, has_z) in enumerate(breaklines):
        subsegs.extend(
            _flatten_breakline(
                poly_idx=poly_idx,
                verts=ent.verts,
                bulges=ent.bulges,
                closed=bool(ent.closed),
                z_resolved=z_res,
                has_z_mask=has_z,
                max_dev=max_dev,
            )
        )

    if len(subsegs) < 2:
        return []

    pairs = _candidate_subseg_pairs(subsegs, xy_tol)
    findings: list[CrossingFinding] = []
    seen_keys: set[tuple[int, int, int, int, int, int]] = set()
    end_param_tol = 1e-5
    dedup_grid = max(xy_tol * 10.0, 1e-3)

    for idx_a, idx_b in pairs:
        sa = subsegs[idx_a]
        sb = subsegs[idx_b]

        # Same polyline, same span: adjacent tessellated arc pieces cannot cross each other
        if sa.poly_idx == sb.poly_idx and sa.span_idx == sb.span_idx:
            if abs(idx_a - idx_b) <= 1:
                continue

        r = sa.p1 - sa.p0
        s = sb.p1 - sb.p0
        hit = G.segment_intersection(sa.p0, r, sb.p0, s)
        if hit is None:
            continue
        t_sub, u_sub = hit
        if not (-end_param_tol <= t_sub <= 1.0 + end_param_tol and -end_param_tol <= u_sub <= 1.0 + end_param_tol):
            continue

        t_clamped = min(1.0, max(0.0, float(t_sub)))
        u_clamped = min(1.0, max(0.0, float(u_sub)))
        t_span = sa.t0 + t_clamped * (sa.t1 - sa.t0)
        u_span = sb.t0 + u_clamped * (sb.t1 - sb.t0)

        seg_a, ent_a, _za_arr, _hza = breaklines[sa.poly_idx]
        seg_b, ent_b, _zb_arr, _hzb = breaklines[sb.poly_idx]
        is_self = sa.poly_idx == sb.poly_idx

        at_a_start = t_span <= end_param_tol
        at_a_end = t_span >= 1.0 - end_param_tol
        at_b_start = u_span <= end_param_tol
        at_b_end = u_span >= 1.0 - end_param_tol

        # Benign shared vertex connectivity on same polyline
        if is_self:
            if (at_a_end and at_b_start and sa.orig_j == sb.orig_i) or (
                at_b_end and at_a_start and sb.orig_j == sa.orig_i
            ):
                continue
            if sa.span_idx == sb.span_idx and (
                (at_a_end and at_b_start) or (at_b_end and at_a_start)
            ):
                continue

        ix = float(sa.p0[0] + t_clamped * r[0])
        iy = float(sa.p0[1] + t_clamped * r[1])

        both_has_z = bool(sa.has_z and sb.has_z)
        za = float(sa.z0 + t_clamped * (sa.z1 - sa.z0)) if sa.has_z else math.nan
        zb = float(sb.z0 + u_clamped * (sb.z1 - sb.z0)) if sb.has_z else math.nan
        dz = abs(za - zb) if both_has_z else math.nan

        # Two distinct polylines meeting at an original vertex on both lines:
        # If they share that endpoint vertex (same linked point or same XY within xy_tol)
        # and do NOT have a Z-conflict there, it is a valid shared vertex / connection.
        if not is_self and (at_a_start or at_a_end) and (at_b_start or at_b_end):
            va_idx = sa.orig_i if at_a_start else sa.orig_j
            vb_idx = sb.orig_i if at_b_start else sb.orig_j
            pid_a = seg_a.point_ids[va_idx] if va_idx < len(seg_a.point_ids) else None
            pid_b = seg_b.point_ids[vb_idx] if vb_idx < len(seg_b.point_ids) else None
            va_xy = ent_a.verts[va_idx, :2]
            vb_xy = ent_b.verts[vb_idx, :2]
            same_vertex = bool(
                (pid_a is not None and pid_b is not None and pid_a == pid_b)
                or math.hypot(float(va_xy[0] - vb_xy[0]), float(va_xy[1] - vb_xy[1])) <= xy_tol
            )
            if same_vertex and (not both_has_z or dz <= z_tol):
                continue

        pa, pb = (sa.poly_idx, sb.poly_idx)
        spa, spb = (sa.span_idx, sb.span_idx)
        if (pa, spa) > (pb, spb):
            pa, pb = pb, pa
            spa, spb = spb, spa
        gx = int(round(ix / dedup_grid))
        gy = int(round(iy / dedup_grid))
        dedup_key = (pa, pb, spa, spb, gx, gy)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        has_z_conflict = bool(both_has_z and dz > z_tol)
        kind = "z-conflict" if has_z_conflict else "plan-crossing"
        severity = "error"

        pids: list[int] = []
        pnums: list[str] = []
        for seg_obj, v_indices in ((seg_a, (sa.orig_i, sa.orig_j)), (seg_b, (sb.orig_i, sb.orig_j))):
            for vi in v_indices:
                if vi < len(seg_obj.point_ids):
                    pid = seg_obj.point_ids[vi]
                    if pid is not None and int(pid) not in pids:
                        pids.append(int(pid))
                if vi < len(seg_obj.point_numbers):
                    pnum = str(seg_obj.point_numbers[vi] or "")
                    if pnum and pnum not in pnums:
                        pnums.append(pnum)

        if is_self:
            prefix = f"Self-crossing on {seg_a.seg_id} (spans {sa.span_idx + 1} & {sb.span_idx + 1})"
        else:
            prefix = f"{seg_a.seg_id} crosses {seg_b.seg_id}"

        if has_z_conflict:
            detail = (
                f"{prefix} at ({ix:.2f}, {iy:.2f}): "
                f"Z1={za:.2f}, Z2={zb:.2f} (dZ={dz:.2f} > {z_tol:.2f})"
            )
        elif both_has_z:
            detail = (
                f"{prefix} at ({ix:.2f}, {iy:.2f}): "
                f"plan crossing with matching elevation Z={0.5 * (za + zb):.2f}"
            )
        else:
            detail = (
                f"{prefix} at ({ix:.2f}, {iy:.2f}): "
                f"plan crossing (2D / z=0 elevation-less breakline)"
            )

        findings.append(
            CrossingFinding(
                kind=kind,
                severity=severity,
                seg_a_id=seg_a.seg_id,
                seg_b_id=seg_b.seg_id,
                entity_a_id=int(seg_a.entity_id),
                entity_b_id=int(seg_b.entity_id),
                span_a=sa.span_idx,
                span_b=sb.span_idx,
                x=ix,
                y=iy,
                za=za,
                zb=zb,
                dz=dz,
                is_self_crossing=is_self,
                point_ids=pids,
                point_numbers=pnums,
                detail=detail,
            )
        )

    return findings


def detect_long_segments(
    project: "Project",
    *,
    segments: list[Segment] | None = None,
    max_length: float | None = None,
) -> list[LongSegmentFinding]:
    """Detect segment spans (or single-span segments) exceeding *max_length*."""
    threshold = (
        float(max_length)
        if max_length is not None and math.isfinite(float(max_length)) and float(max_length) > 0.0
        else default_max_segment_length(project)
    )
    segs = build_segments(project) if segments is None else list(segments)
    entities_by_id = getattr(project, "entities", {}) or {}
    hidden_ids = project.hidden_ids() if hasattr(project, "hidden_ids") else set()
    findings: list[LongSegmentFinding] = []

    for seg in segs:
        ent = entities_by_id.get(seg.entity_id)
        if not isinstance(ent, Polyline) or len(ent.verts) < 2:
            continue
        if not _entity_is_visible(project, ent, hidden_ids):
            continue
        v = np.asarray(ent.verts, dtype=float)
        n = len(v)
        b = ent.bulges if ent.bulges is not None and len(ent.bulges) == n else None
        n_spans = n if ent.closed else n - 1

        span_flagged = False
        for s_idx in range(n_spans):
            i = s_idx
            j = (s_idx + 1) % n
            bi = float(b[i]) if b is not None else 0.0
            if abs(bi) > 1e-12:
                sub_v = np.vstack([v[i], v[j]])
                sub_b = np.array([bi, 0.0])
                len_2d = float(G.polyline_length(sub_v, sub_b, closed=False, three_d=False))
                len_3d = float(G.polyline_length(sub_v, sub_b, closed=False, three_d=True))
            else:
                len_2d = float(math.hypot(v[j, 0] - v[i, 0], v[j, 1] - v[i, 1]))
                dz = (
                    float(v[j, 2] - v[i, 2])
                    if v.shape[1] >= 3 and math.isfinite(v[i, 2]) and math.isfinite(v[j, 2])
                    else 0.0
                )
                len_3d = float(math.hypot(len_2d, dz))
            check_len = max(len_2d, len_3d)
            if check_len <= threshold:
                continue

            span_flagged = True
            mid = 0.5 * (v[i] + v[j])
            pids: list[int] = []
            pnums: list[str] = []
            for vi in (i, j):
                if vi < len(seg.point_ids) and seg.point_ids[vi] is not None:
                    pids.append(int(seg.point_ids[vi]))
                if vi < len(seg.point_numbers) and seg.point_numbers[vi]:
                    pnums.append(str(seg.point_numbers[vi]))

            pt_label = (
                f"pts {pnums[0]}->{pnums[1]}"
                if len(pnums) == 2
                else f"span {s_idx + 1}"
            )
            findings.append(
                LongSegmentFinding(
                    seg_id=seg.seg_id,
                    entity_id=int(seg.entity_id),
                    code=seg.code,
                    layer=seg.layer,
                    span_index=s_idx,
                    length_2d=len_2d,
                    length_3d=len_3d,
                    threshold=threshold,
                    x=float(mid[0]),
                    y=float(mid[1]),
                    z=float(mid[2]) if len(mid) >= 3 and math.isfinite(float(mid[2])) else 0.0,
                    point_ids=pids,
                    point_numbers=pnums,
                    detail=(
                        f"{seg.seg_id} ({pt_label}): 2D={len_2d:.2f}, 3D={len_3d:.2f} "
                        f"exceeds max {threshold:.1f}"
                    ),
                )
            )

        if not span_flagged and max(seg.length_2d, seg.length_3d) > threshold and n_spans == 1:
            mid = 0.5 * (v[0] + v[-1])
            findings.append(
                LongSegmentFinding(
                    seg_id=seg.seg_id,
                    entity_id=int(seg.entity_id),
                    code=seg.code,
                    layer=seg.layer,
                    span_index=0,
                    length_2d=float(seg.length_2d),
                    length_3d=float(seg.length_3d),
                    threshold=threshold,
                    x=float(mid[0]),
                    y=float(mid[1]),
                    z=float(mid[2]) if len(mid) >= 3 and math.isfinite(float(mid[2])) else 0.0,
                    point_ids=[int(p) for p in seg.point_ids if p is not None],
                    point_numbers=[str(n) for n in seg.point_numbers if n],
                    detail=(
                        f"{seg.seg_id}: 2D={seg.length_2d:.2f}, 3D={seg.length_3d:.2f} "
                        f"exceeds max {threshold:.1f}"
                    ),
                )
            )

    return findings


def detect_segment_command_issues(
    project: "Project",
    *,
    segments: list[Segment] | None = None,
) -> list[SegmentCommandFinding]:
    """Surface start/end (ST/END) and curve (PC/PT) sequence issues mapped to segments."""
    if not getattr(project, "points", None):
        return []
    try:
        from ..fieldwork import bridge as FB
    except Exception:
        return []

    segs = build_segments(project) if segments is None else list(segments)
    seg_by_pid: dict[int, Segment] = {}
    seg_by_code: dict[str, Segment] = {}
    for seg in segs:
        if seg.code and seg.code.upper() not in seg_by_code:
            seg_by_code[seg.code.upper()] = seg
        for pid in seg.point_ids:
            if pid is not None and int(pid) not in seg_by_pid:
                seg_by_pid[int(pid)] = seg

    f2f = set(
        project.codes.casefolded_set()
        if hasattr(project, "codes") and hasattr(project.codes, "casefolded_set")
        else (set(project.codes.codes.keys()) if hasattr(project, "codes") else set())
    )
    if not f2f:
        from .featurecodes import default_codes
        f2f = set(default_codes().casefolded_set())
    for seg in segs:
        if seg.code:
            f2f.add(seg.code.casefold())
    settings = getattr(project, "settings", None) or {}
    fieldbook_path = settings.get("fieldbook_file") or settings.get("f2f_path") or None
    try:
        lw_res = FB.check_linework_project_cached(
            project, f2f=f2f, fieldbook_path=fieldbook_path
        )
        if isinstance(lw_res, tuple):
            lw_res = lw_res[0]
    except Exception:
        try:
            lw_res = FB.check_linework_project(
                project, f2f=f2f, fieldbook_path=fieldbook_path
            )
        except Exception:
            return []

    rows = lw_res.get("rows") or []
    ids = lw_res.get("ids") or []
    oid_to_pid: dict[str, int] = {}
    for idx, row in enumerate(rows):
        if idx < len(ids):
            oid_to_pid[str(row[FB.OID])] = int(ids[idx])

    relevant_types = {
        "Missing ST",
        "Missing END",
        "Missing PC",
        "Missing PT",
        "Curve Error",
        "Duplicate Command",
        "Syntax Error",
    }
    out: list[SegmentCommandFinding] = []
    for item in lw_res.get("line_issues") or []:
        issue_type = str(item.get("issue_type", "") or "")
        if issue_type not in relevant_types:
            continue
        oid_str = str(item.get("oid", "") or "")
        pid = oid_to_pid.get(oid_str)
        pt = project.points.get(pid) if pid is not None else None
        pnum = str(item.get("point_number") or (getattr(pt, "number", "") if pt is not None else "") or "")
        code = str(item.get("code", "") or "")
        seg = (seg_by_pid.get(pid) if pid is not None else None) or (
            seg_by_code.get(code.upper()) if code else None
        )
        seg_id = seg.seg_id if seg is not None else (code or "—")
        entity_id = int(seg.entity_id) if seg is not None else None
        x = float(getattr(pt, "x", 0.0)) if pt is not None else 0.0
        y = float(getattr(pt, "y", 0.0)) if pt is not None else 0.0
        z = float(getattr(pt, "z", 0.0)) if pt is not None else 0.0
        proposal = str(item.get("proposal", "") or "")
        detail = str(item.get("detail", "") or "")
        raw_desc = str(item.get("raw_description") or (getattr(pt, "desc", "") if pt is not None else "") or "")
        severity = (
            "error"
            if issue_type in ("Missing ST", "Missing END", "Missing PC", "Missing PT", "Curve Error")
            else "warning"
        )
        out.append(
            SegmentCommandFinding(
                issue_type=issue_type,
                severity=severity,
                seg_id=seg_id,
                entity_id=entity_id,
                code=code,
                point_id=pid,
                point_number=pnum,
                x=x,
                y=y,
                z=z,
                raw_description=raw_desc,
                proposal=proposal,
                detail=detail,
            )
        )
    return out


def run_geometry_checks(
    project: "Project",
    *,
    segments: list[Segment] | None = None,
    max_length: float | None = None,
    z_tol: float = 0.01,
    xy_tol: float = 1e-4,
    include_command_issues: bool = True,
) -> GeometryCheckReport:
    """Run all Phase 2 geometry checks on *project* and return a :class:`GeometryCheckReport`."""
    segs = build_segments(project) if segments is None else list(segments)
    threshold = (
        float(max_length)
        if max_length is not None and math.isfinite(float(max_length)) and float(max_length) > 0.0
        else default_max_segment_length(project)
    )
    entities_by_id = getattr(project, "entities", {}) or {}
    breakline_count = sum(
        1
        for s in segs
        if isinstance((ent := entities_by_id.get(s.entity_id)), Polyline)
        and _is_breakline_segment(project, s, ent)
    )

    crossings = detect_breakline_crossings(
        project, segments=segs, z_tol=z_tol, xy_tol=xy_tol
    )
    long_segs = detect_long_segments(
        project, segments=segs, max_length=threshold
    )
    cmd_issues = (
        detect_segment_command_issues(project, segments=segs)
        if include_command_issues
        else []
    )

    issues: list[GeometryIssue] = []
    for c in crossings:
        seg_ids = [c.seg_a_id] if c.is_self_crossing else [c.seg_a_id, c.seg_b_id]
        ent_ids = [c.entity_a_id] if c.is_self_crossing else [c.entity_a_id, c.entity_b_id]
        z_val = 0.5 * (c.za + c.zb) if (math.isfinite(c.za) and math.isfinite(c.zb)) else 0.0
        title = "Breakline Z-conflict" if c.has_z_conflict else (
            "Breakline self-crossing" if c.is_self_crossing else "Breakline plan crossing"
        )
        issues.append(
            GeometryIssue(
                category=c.kind,
                severity=c.severity,
                seg_ids=seg_ids,
                entity_ids=ent_ids,
                point_ids=list(c.point_ids),
                point_numbers=list(c.point_numbers),
                x=c.x,
                y=c.y,
                z=z_val,
                title=title,
                detail=c.detail,
            )
        )

    for ls in long_segs:
        issues.append(
            GeometryIssue(
                category="line-too-long",
                severity="warning",
                seg_ids=[ls.seg_id],
                entity_ids=[ls.entity_id],
                point_ids=list(ls.point_ids),
                point_numbers=list(ls.point_numbers),
                x=ls.x,
                y=ls.y,
                z=ls.z,
                title=f"Line too long (>{ls.threshold:.0f})",
                detail=ls.detail,
            )
        )

    for ci in cmd_issues:
        issues.append(
            GeometryIssue(
                category="command-sequence",
                severity=ci.severity,
                seg_ids=[ci.seg_id] if ci.seg_id and ci.seg_id != "—" else [],
                entity_ids=[ci.entity_id] if ci.entity_id is not None else [],
                point_ids=[ci.point_id] if ci.point_id is not None else [],
                point_numbers=[ci.point_number] if ci.point_number else [],
                x=ci.x,
                y=ci.y,
                z=ci.z,
                title=ci.issue_type,
                detail=f"Pt {ci.point_number} ({ci.raw_description}): {ci.detail}",
                proposal=ci.proposal,
                target_point_id=ci.point_id,
            )
        )

    return GeometryCheckReport(
        crossings=crossings,
        long_segments=long_segs,
        command_issues=cmd_issues,
        issues=issues,
        breakline_count=breakline_count,
        max_segment_length=threshold,
    )
