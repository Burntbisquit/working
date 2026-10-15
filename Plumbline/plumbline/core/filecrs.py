"""Per-file coordinate-system records and corrections.

Imports store their point coordinates in the project CRS; when an import transformed from a
selected source CRS, that source is retained as provenance. The Survey > File Coordinate Systems
dialog can correct a file after import:

* **Set to Project** records the current project CRS without moving coordinates.
* **Reproject** treats a user-selected corrected CRS as the file's current source and transforms
  only that file's points into the project's current CRS. Source and destination SAFs are applied
  around the respective transformations.
* **Ground / grid SAF** changes the scale of only that file's points, without changing datum.

The record lives on the project, not on the points: the points carry the file name they came from
(:mod:`plumbline.core.provenance`). Older projects with no record remain visible with a blank
system rather than an inferred one.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from . import units as U

#: Where the records live: ``project.settings[KEY]`` is ``{file name: record}``.
KEY = "file_crs"

#: How the file's system was arrived at, in the words the dialog shows.
METHODS = {
    "project": "set to the project's coordinate system",
    "imported": "converted into the project's system at import",
    "file": "read from the file itself",
    "chosen": "chosen at import time",
    "edited": "changed afterwards on this screen",
    "reprojected": "reprojected into the project's system",
    "field-book": "the job's field book / fieldwork import",
}


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def make(*, key: str = "", label: str = "", unit: str = "", vunit: str = "", vertical: str = "",
         geoid: str = "", saf: float = 1.0, ground: bool = False, base_n: float = 0.0,
         base_e: float = 0.0, method: str = "project", folder: str = "", when: str = "") -> dict:
    """One file's coordinate-system record.  Everything is a plain value so it survives save/load."""
    return {"key": str(key or ""), "label": str(label or ""), "unit": str(unit or ""),
            "vunit": str(vunit or ""), "vertical": str(vertical or ""), "geoid": str(geoid or ""),
            "saf": float(saf or 1.0), "ground": bool(ground), "base_n": float(base_n or 0.0),
            "base_e": float(base_e or 0.0), "method": str(method or "project"),
            "folder": str(folder or ""), "when": str(when or _stamp())}


def make_from_project(project, method: str = "project", **kw) -> dict:
    """The record that describes "in the project's system, as the project stands now"."""
    c = project.crs
    g = c.ground
    return make(key=c.authority or "", label=c.label or c.name or "", unit=c.unit, vunit=c.vunit,
                vertical=getattr(c, "vdatum", "") or "", geoid=getattr(c, "geoid", "") or "",
                saf=float(getattr(g, "saf", 1.0) or 1.0), ground=bool(getattr(g, "enabled", False)),
                base_n=float(getattr(g, "base_y", 0.0) or 0.0),
                base_e=float(getattr(g, "base_x", 0.0) or 0.0), method=method, **kw)


# --------------------------------------------------------------------------------------------- store
def record(project, file: str, rec: dict) -> dict:
    """Write one file's record (replacing any earlier one for the same name)."""
    table = dict(project.settings.get(KEY) or {})
    table[str(file)] = dict(rec)
    project.settings[KEY] = table
    return table[str(file)]


def forget(project, file: str) -> None:
    table = dict(project.settings.get(KEY) or {})
    table.pop(str(file), None)
    project.settings[KEY] = table


def for_file(project, file: str) -> dict | None:
    return (project.settings.get(KEY) or {}).get(str(file))


def files(project) -> list[dict]:
    """Every file that brought data into this project, with its record (or an empty one).

    The list of files comes from the points themselves, so a file imported before this feature
    existed still appears - with a blank CRS to fill in - and a file whose points were all deleted
    disappears with them.
    """
    from . import provenance as PROV
    seen: dict[str, dict] = {}
    for p in project.points.values():
        rec = PROV.of(p)
        name = rec.get("file") or ""
        if not name:
            continue
        row = seen.setdefault(name, {"file": name, "folder": rec.get("folder", ""), "points": 0})
        row["points"] += 1
    table = project.settings.get(KEY) or {}
    for name in list(seen) + [k for k in table if k not in seen]:
        seen.setdefault(name, {"file": name, "folder": "", "points": 0})
    out = []
    for name, row in seen.items():
        rec = table.get(name)
        out.append({**row, "crs": rec or None,
                    "key": (rec or {}).get("key", ""), "label": (rec or {}).get("label", ""),
                    "vertical": (rec or {}).get("vertical", ""), "saf": (rec or {}).get("saf", 1.0),
                    "ground": (rec or {}).get("ground", False),
                    "method": (rec or {}).get("method", ""), "when": (rec or {}).get("when", "")})
    return sorted(out, key=lambda r: (-r["points"], r["file"].lower()))


def points_of(project, file: str) -> list:
    from . import provenance as PROV
    return [p for p in project.points.values() if (PROV.of(p).get("file") or "") == str(file)]


# --------------------------------------------------------------------------------------------- the two operations
def relabel(project, file: str, rec: dict) -> dict:
    """Change one file's recorded system without changing any coordinates."""
    rec = dict(rec)
    rec["method"] = "edited"
    rec["when"] = _stamp()
    return record(project, file, rec)


def set_to_project(project, file: str) -> dict:
    """Record that this file's points are already in the project's current coordinate system."""
    previous = for_file(project, file) or {}
    rec = make_from_project(project, method="project")
    if previous.get("source_crs"):
        rec["source_crs"] = dict(previous["source_crs"])
    return record(project, file, rec)


def reproject(project, file: str, source_rec: dict, *, convert_z: bool = True) -> dict:
    """Convert this file's points from the corrected source system into the project system.

    ``source_rec`` describes what the selected file coordinates are in *now*.  The destination is
    always the project's current CRS.  This direction matters for a file whose projection was
    assigned incorrectly: the user supplies the corrected source system, and the file's points
    are transformed into the project system.  ``ProjectCRS.transform_to`` applies the source
    ground-to-grid SAF and the project's grid-to-ground SAF, when either is enabled.
    """
    from . import audit as AUD

    source = to_projectcrs(source_rec)
    if source is None:
        raise ValueError("Pick the corrected coordinate system these file coordinates are in.")
    target = project.crs
    if target.is_local:
        raise ValueError("Assign a project coordinate system before reprojecting file coordinates.")
    pts = points_of(project, file)
    fn = source.transform_to(target, target.strategy)
    zs = (U.M_PER_UNIT.get(source.vunit, 1.0) / U.M_PER_UNIT.get(target.vunit, 1.0)
          if convert_z else 1.0)
    xs = np.array([p.x for p in pts], float)
    ys = np.array([p.y for p in pts], float)
    nx, ny = fn(xs, ys)
    for p, a, b in zip(pts, nx, ny):
        p.x, p.y = float(a), float(b)
        if convert_z and np.isfinite(p.z):
            p.z *= zs
    AUD.record(project, pts)          # the audit trail is the same one the reprojection tool uses
    out = make_from_project(project, method="reprojected")
    out["reprojected_from"] = source_rec.get("label") or source_rec.get("key") or "the corrected source system"
    out["source_crs"] = dict(source_rec)
    record(project, file, out)
    return {"points": len(pts), "from": source.label, "to": target.label, "record": out}


def rescale_ground(project, file: str, *, saf: float, to_ground: bool, base_n: float = 0.0,
                   base_e: float = 0.0) -> dict:
    """Fix the ground/grid state of one file's points: "assumed grid, was ground".

    The TXDOT operation, applied to one file's points only: ``grid = ground / SAF`` when the
    numbers arrived as ground and the project is storing grid, and the multiplication the other
    way.  No datum changes and no scale factor is taken from anywhere but this argument, so what
    the dialog showed is what the coordinates got.
    """
    from .crs import GroundScale
    if not (saf and saf > 0):
        raise ValueError("a SAF is needed: ground = grid x SAF")
    if abs(saf - 1.0) < 1e-12:
        return {"points": 0, "note": "SAF 1.000000000 is grid; nothing to do."}
    pts = points_of(project, file)
    # A scale that is switched off is not a scale: build the one asked for here rather than
    # reading the project's (which is off whenever the job is grid - the case this fixes).
    gr = GroundScale(enabled=True, base_x=float(base_e), base_y=float(base_n), saf=float(saf))
    fn = gr.from_grid if to_ground else gr.to_grid
    for p in pts:
        nx, ny = fn(p.x, p.y)
        p.x, p.y = float(nx), float(ny)
    from . import audit as AUD
    AUD.record(project, pts)
    rec = dict(for_file(project, file) or make_from_project(project))
    rec.update({"saf": float(saf), "ground": bool(to_ground), "base_n": float(base_n),
                "base_e": float(base_e), "method": "edited", "when": _stamp()})
    if to_ground:
        rec["note"] = f"ground = grid x {saf:.9f} from N {base_n:,.3f}, E {base_e:,.3f}"
    else:
        rec["note"] = f"grid = ground / {saf:.9f} from N {base_n:,.3f}, E {base_e:,.3f}"
    record(project, file, rec)
    return {"points": len(pts), "saf": float(saf), "to_ground": bool(to_ground), "record": rec}


def record_from_crs(crs, *, method: str = "chosen", **kw) -> dict:
    """The record that describes a particular ProjectCRS (the one an import converted from)."""
    g = getattr(crs, "ground", None)
    return make(key=getattr(crs, "key", "") or (crs.authority if crs.authority != "custom" else ""),
                label=crs.label or crs.name or "", unit=getattr(crs, "unit", ""),
                vunit=getattr(crs, "vunit", ""), vertical=getattr(crs, "vdatum", "") or "",
                geoid=getattr(crs, "geoid", "") or "",
                saf=float(getattr(g, "saf", 1.0) or 1.0),
                ground=bool(getattr(g, "enabled", False)),
                base_n=float(getattr(g, "base_y", 0.0) or 0.0),
                base_e=float(getattr(g, "base_x", 0.0) or 0.0), method=method, **kw)


def to_projectcrs(rec: dict):
    """A record -> a :class:`~plumbline.core.crs.ProjectCRS`, for a transform.  None if it cannot.

    The ground part matters here: two records with the same EPSG code and different SAF are the
    difference between grid and ground coordinates, and ``ProjectCRS.transform_to`` applies that
    step when both sides are ProjectCRS objects - which is how "assumed grid, was ground" is
    fixed by the same code path that fixes a wrong zone.
    """
    from .crs import GroundScale, ProjectCRS
    rec = rec or {}
    key = str(rec.get("key") or "").strip()
    if not key:
        return None
    try:
        pc = ProjectCRS.from_key(key)
    except Exception:
        try:
            pc = ProjectCRS.from_epsg(int(key.split(":")[-1]))
        except Exception:
            return None
    if rec.get("vunit"):
        pc.vunit = rec["vunit"]
    pc.vdatum = rec.get("vertical") or pc.vdatum
    pc.geoid = rec.get("geoid") or pc.geoid
    pc.ground = GroundScale(enabled=bool(rec.get("ground")),
                            base_x=float(rec.get("base_e") or 0.0),
                            base_y=float(rec.get("base_n") or 0.0),
                            saf=float(rec.get("saf") or 1.0))
    return pc


__all__ = ["KEY", "METHODS", "make", "make_from_project", "record_from_crs", "to_projectcrs",
           "record", "forget", "for_file", "files", "points_of", "relabel", "set_to_project",
           "reproject", "rescale_ground"]
