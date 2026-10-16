"""Core geometry tools and active-occurrence line coding operations (Qt-free).

Implements Phase 3 of ``docs/GEOMETRY_SEGMENTS_PLAN.md``:

1. **Nudge points / vertices** (:func:`nudge_point`, :func:`nudge_segment_vertex`)
2. **Untwist bowtie** (:func:`untwist_polyline`)
3. **Flip past** (:func:`flip_past`)
4. **Curve / recurve / straighten** (:func:`curve_segment`, :func:`recurve_segment`,
   :func:`straighten_segment`)
5. **Active-occurrence Connect lines** (:func:`sweep_code_family_occurrences`,
   :func:`preview_connect_segments`, :func:`apply_connect_segments`)
6. **Geometry-only Join lines, average** (:func:`join_lines_average`)
7. **Move end to end** (:func:`move_line_end_to_end`)
8. **Add node & Inject point** (:func:`add_polyline_node`, :func:`inject_survey_point`)
9. **COGO from lines** (:func:`cogo_points_from_lines`)
10. **Shrink Line Commands** (:func:`preview_shrink_line_commands`,
    :func:`apply_shrink_line_commands`)

All operations honour the sacred point-number rule (§2.4): existing survey point
numbers are never renumbered or deleted.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import math
import re
from typing import TYPE_CHECKING, Sequence

import numpy as np

from . import geometry as G
from . import point_linework_coder as PLC
from .featurecodes import (
    BEGIN_FLAGS,
    CLOSE_FLAGS,
    END_FLAGS,
    default_codes,
    parse_description,
)
from .fieldbook_syntax import command_map, separator_text
from .geometry_segments import Segment, build_segments
from .linework_editor import (
    LineEditDraft,
    bulge_point,
    resolve_vertex_point_ids,
    split_bulge,
)
from .model import NAN, Polyline, SurveyPoint

if TYPE_CHECKING:
    from .project import Project


def _point_sort_key(point: SurveyPoint) -> tuple[int, float, str, int]:
    s = str(point.number or "").strip()
    try:
        return (0, float(s), s, int(point.id))
    except (TypeError, ValueError):
        return (1, 0.0, s.casefold(), int(point.id))


def _number_sort_key(num_str: str, fallback_id: int = 0) -> tuple[int, float, str, int]:
    s = str(num_str or "").strip()
    try:
        return (0, float(s), s, int(fallback_id))
    except (TypeError, ValueError):
        return (1, 0.0, s.casefold(), int(fallback_id))


def _ensure_point_ids_attr(project: "Project", entity: Polyline) -> list[int | None]:
    attrs = entity.attrs if entity.attrs is not None else {}
    entity.attrs = attrs
    raw = attrs.get("point_ids")
    if isinstance(raw, (list, tuple)) and len(raw) == len(entity.verts):
        out: list[int | None] = []
        for v in raw:
            try:
                out.append(int(v) if v is not None else None)
            except (TypeError, ValueError):
                out.append(None)
        return out
    pids, _issues = resolve_vertex_point_ids(project, entity)
    attrs["point_ids"] = list(pids)
    attrs["points"] = [
        project.points[pid].number if pid is not None and pid in project.points else ""
        for pid in pids
    ]
    return list(pids)


def _mark_manual_override_if_derived(project: "Project", entity: Polyline, point_ids: Sequence[int | None]) -> None:
    """Mark a derived polyline as a manual override when geometry-only edits are made."""
    if not (entity.derived and str(entity.derived).startswith("linework")):
        return
    attrs = entity.attrs if entity.attrs is not None else {}
    entity.attrs = attrs
    code = str(attrs.get("code") or "").strip()
    string = str(attrs.get("string") or "").strip()
    valid_pids = [int(p) for p in point_ids if p is not None]
    attrs["manual_linework_override"] = True
    if code:
        attrs["linework_override_keys"] = [[code, string]]
        attrs["linework_override_point_ids"] = sorted(set(valid_pids))
        seqs = [list(valid_pids)] if len(valid_pids) >= 2 else []
        attrs["linework_override_scopes"] = [
            {
                "code": code,
                "string": string,
                "point_ids": sorted(set(valid_pids)),
                "source_sequences": seqs,
            }
        ]
    entity.derived = "manual-linework"


def _sync_point_to_polylines(project: "Project", point_id: int) -> list[int]:
    """Update every polyline vertex linked to *point_id* to match the point's XYZ."""
    pt = project.points.get(point_id)
    if pt is None:
        return []
    touched: list[int] = []
    for ent in project.polylines():
        raw_pids = (ent.attrs or {}).get("point_ids")
        if not isinstance(raw_pids, (list, tuple)) or len(raw_pids) != len(ent.verts):
            continue
        changed = False
        for idx, pid in enumerate(raw_pids):
            if pid == point_id:
                ent.verts[idx, 0] = float(pt.x)
                ent.verts[idx, 1] = float(pt.y)
                if math.isfinite(pt.z):
                    ent.verts[idx, 2] = float(pt.z)
                changed = True
        if changed:
            touched.append(int(ent.id))
    return touched


# =============================================================================
# 1. Nudge point / vertex
# =============================================================================
def nudge_point(
    project: "Project",
    point_id: int,
    dx: float,
    dy: float,
    dz: float = 0.0,
    *,
    rebuild_linework: bool = False,
) -> SurveyPoint:
    """Offset a survey point by ``(dx, dy, dz)`` and propagate to linked polylines.

    Point numbers and descriptions are never altered.
    """
    pt = project.points.get(int(point_id))
    if pt is None:
        raise ValueError(f"Point ID {point_id} does not exist.")
    for val in (dx, dy, dz):
        if not math.isfinite(float(val)):
            raise ValueError("Nudge offsets must be finite numbers.")
    pt.x = float(pt.x) + float(dx)
    pt.y = float(pt.y) + float(dy)
    if math.isfinite(pt.z):
        pt.z = float(pt.z) + float(dz)
    _sync_point_to_polylines(project, pt.id)
    if rebuild_linework and project.linework_is_processed():
        order = str((project.settings or {}).get("linework_processed_order") or "number")
        project.process_linework(order=order)
    else:
        project.touch()
    return pt


def nudge_segment_vertex(
    project: "Project",
    entity_id: int,
    vertex_index: int,
    dx: float,
    dy: float,
    dz: float = 0.0,
    *,
    propagate_to_point: bool = True,
    rebuild_linework: bool = False,
) -> dict:
    """Nudge one vertex of a polyline; if linked to a point, nudges the survey point."""
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline):
        raise ValueError(f"Polyline {entity_id} does not exist.")
    idx = int(vertex_index)
    if idx < 0 or idx >= len(ent.verts):
        raise ValueError(f"Vertex index {idx} is out of range.")
    pids = _ensure_point_ids_attr(project, ent)
    pid = pids[idx] if idx < len(pids) else None
    if propagate_to_point and pid is not None and pid in project.points:
        pt = nudge_point(project, pid, dx, dy, dz, rebuild_linework=rebuild_linework)
        return {"entity_id": int(entity_id), "vertex_index": idx, "point_id": pt.id, "x": pt.x, "y": pt.y, "z": pt.z}

    draft = LineEditDraft(project, ent)
    draft.nudge_vertex(idx, dx, dy, dz)
    ent.verts[idx, 0] = draft.vertices[idx].x
    ent.verts[idx, 1] = draft.vertices[idx].y
    ent.verts[idx, 2] = draft.vertices[idx].z
    _mark_manual_override_if_derived(project, ent, pids)
    project.touch()
    return {
        "entity_id": int(entity_id),
        "vertex_index": idx,
        "point_id": None,
        "x": float(ent.verts[idx, 0]),
        "y": float(ent.verts[idx, 1]),
        "z": float(ent.verts[idx, 2]),
    }


# =============================================================================
# 2. Untwist (bowtie removal)
# =============================================================================
def _find_self_crossing_spans(verts: np.ndarray, closed: bool) -> tuple[int, int] | None:
    v = np.asarray(verts, dtype=float)
    n = len(v)
    n_spans = n if closed else n - 1
    if n_spans < 3:
        return None
    eps = 1e-7
    for sa in range(n_spans):
        ia, ja = sa, (sa + 1) % n
        pa = v[ia, :2]
        ra = v[ja, :2] - pa
        for sb in range(sa + 2, n_spans):
            ib, jb = sb, (sb + 1) % n
            if ia in (ib, jb) or ja in (ib, jb):
                continue
            pb = v[ib, :2]
            sb_vec = v[jb, :2] - pb
            hit = G.segment_intersection(pa, ra, pb, sb_vec)
            if hit is None:
                continue
            t, u = hit
            if -eps <= t <= 1.0 + eps and -eps <= u <= 1.0 + eps:
                return sa, sb
    return None


def untwist_polyline(
    project: "Project",
    entity_id: int,
    span_a: int | None = None,
    span_b: int | None = None,
    *,
    swap_point_coordinates: bool = False,
) -> dict:
    """Uncross a self-crossing bowtie on *entity_id* by reversing the twisted interior run.

    When ``span_a`` and ``span_b`` are omitted, automatically locates the first
    pair of non-adjacent crossing spans ``(span_a, span_b)`` and reverses the
    vertex sub-chain ``[span_a + 1 .. span_b]`` (2-opt untwist).

    Point numbers are never changed. If ``swap_point_coordinates=True`` and the
    twisted vertices are linked to survey points, their XYZ coordinates are
    swapped on the points so point-number order is preserved; otherwise the
    polyline vertex order is updated directly as a geometry override.
    """
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 4:
        raise ValueError("Untwist requires a polyline with at least 4 vertices.")

    if span_a is None or span_b is None:
        detected = _find_self_crossing_spans(ent.verts, bool(ent.closed))
        if detected is None:
            raise ValueError("No self-crossing bowtie was found on this polyline.")
        span_a, span_b = detected

    sa, sb = sorted((int(span_a), int(span_b)))
    n = len(ent.verts)
    if sa < 0 or sb >= n or sb <= sa + 1:
        raise ValueError("Untwist requires two non-adjacent spans.")

    pids = _ensure_point_ids_attr(project, ent)
    lo = sa + 1
    hi = sb  # inclusive

    if swap_point_coordinates and all(pids[k] is not None and pids[k] in project.points for k in range(lo, hi + 1)):
        pts = [project.points[int(pids[k])] for k in range(lo, hi + 1)]
        coords = [(float(p.x), float(p.y), float(p.z)) for p in pts]
        for pt, (nx, ny, nz) in zip(pts, reversed(coords)):
            pt.x, pt.y, pt.z = nx, ny, nz
            _sync_point_to_polylines(project, pt.id)
        project.touch()
        return {
            "entity_id": int(entity_id),
            "span_a": sa,
            "span_b": sb,
            "reversed_indices": list(range(lo, hi + 1)),
            "mode": "point-coordinates",
        }

    ent.verts[lo : hi + 1] = ent.verts[lo : hi + 1][::-1].copy()
    if ent.bulges is not None and len(ent.bulges) == n:
        ent.bulges[lo:hi] = -ent.bulges[lo:hi][::-1]
        ent.bulges[sa] = 0.0
        ent.bulges[sb] = 0.0
    pids[lo : hi + 1] = list(reversed(pids[lo : hi + 1]))
    ent.attrs["point_ids"] = list(pids)
    ent.attrs["points"] = [
        project.points[pid].number if pid is not None and pid in project.points else ""
        for pid in pids
    ]
    _mark_manual_override_if_derived(project, ent, pids)
    project.touch()
    return {
        "entity_id": int(entity_id),
        "span_a": sa,
        "span_b": sb,
        "reversed_indices": list(range(lo, hi + 1)),
        "mode": "geometry",
    }


# =============================================================================
# 3. Flip past (reflect vertices across reference edge)
# =============================================================================
def flip_past(
    project: "Project",
    entity_id: int,
    vertex_indices: Sequence[int],
    ref_p0: Sequence[float] | None = None,
    ref_p1: Sequence[float] | None = None,
    *,
    propagate_to_points: bool = True,
) -> dict:
    """Reflect selected vertices across a reference edge without altering descriptions or numbers.

    If ``ref_p0`` and ``ref_p1`` are omitted, the reflection axis is the line
    connecting the vertex immediately before ``min(vertex_indices)`` and the
    vertex immediately after ``max(vertex_indices)``.
    """
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 2:
        raise ValueError("Flip past requires a valid polyline.")
    n = len(ent.verts)
    indices = sorted({int(i) for i in vertex_indices})
    if not indices or any(i < 0 or i >= n for i in indices):
        raise ValueError("Select at least one valid vertex index to flip.")

    if ref_p0 is None or ref_p1 is None:
        if n < 3:
            raise ValueError("Provide a reference edge when flipping vertices on a 2-vertex line.")
        i_prev = (indices[0] - 1) % n if ent.closed else max(0, indices[0] - 1)
        i_next = (indices[-1] + 1) % n if ent.closed else min(n - 1, indices[-1] + 1)
        if i_prev in indices or i_next in indices or i_prev == i_next:
            raise ValueError("Provide an explicit reference edge when flipping endpoints.")
        p0 = ent.verts[i_prev, :2].astype(float)
        p1 = ent.verts[i_next, :2].astype(float)
    else:
        p0 = np.asarray(ref_p0, dtype=float)[:2]
        p1 = np.asarray(ref_p1, dtype=float)[:2]

    dx = float(p1[0] - p0[0])
    dy = float(p1[1] - p0[1])
    len_sq = dx * dx + dy * dy
    if len_sq <= 1e-18:
        raise ValueError("Reflection reference edge cannot have zero length.")

    pids = _ensure_point_ids_attr(project, ent)
    flipped_coords: dict[int, tuple[float, float]] = {}
    for idx in indices:
        vx, vy = float(ent.verts[idx, 0]), float(ent.verts[idx, 1])
        # Orthogonal projection of (vx, vy) onto line p0 -> p1
        t = ((vx - p0[0]) * dx + (vy - p0[1]) * dy) / len_sq
        proj_x = float(p0[0] + t * dx)
        proj_y = float(p0[1] + t * dy)
        rx = 2.0 * proj_x - vx
        ry = 2.0 * proj_y - vy
        ent.verts[idx, 0] = rx
        ent.verts[idx, 1] = ry
        flipped_coords[idx] = (rx, ry)
        pid = pids[idx] if idx < len(pids) else None
        if propagate_to_points and pid is not None and pid in project.points:
            pt = project.points[pid]
            pt.x, pt.y = rx, ry
            _sync_point_to_polylines(project, pid)

    if ent.bulges is not None and len(ent.bulges) == n:
        for idx in indices:
            ent.bulges[idx] = -float(ent.bulges[idx])

    if not propagate_to_points:
        _mark_manual_override_if_derived(project, ent, pids)
    project.touch()
    return {"entity_id": int(entity_id), "flipped": flipped_coords}


# =============================================================================
# 4. Curve / Recurve / Straighten
# =============================================================================
def _sync_span_curve_tokens(
    project: "Project",
    entity: Polyline,
    span_index: int,
    has_curve: bool,
) -> None:
    """Update PC/PT tokens on linked points for *span_index* without altering point numbers."""
    pids = _ensure_point_ids_attr(project, entity)
    n = len(entity.verts)
    if n < 2:
        return
    code = str((entity.attrs or {}).get("code") or "").strip()
    string = str((entity.attrs or {}).get("string") or "").strip()
    code_prefix = f"{code}{string}" if code else ""
    if not code_prefix:
        return
    commands = (project.settings or {}).get("f2f_commands")
    semantic = command_map(commands)

    bulges = np.zeros(n, dtype=float) if entity.bulges is None else np.asarray(entity.bulges, dtype=float)
    n_spans = n if entity.closed else n - 1
    curve_by_vertex: list[set[str]] = [set() for _ in range(n)]
    for s_idx in range(n_spans):
        if s_idx < len(bulges) and abs(float(bulges[s_idx])) > 1e-12:
            curve_by_vertex[s_idx].add("start_curve")
            curve_by_vertex[(s_idx + 1) % n].add("end_curve")

    for v_idx in (span_index, (span_index + 1) % n):
        pid = pids[v_idx] if v_idx < len(pids) else None
        pt = project.points.get(pid) if pid is not None else None
        if pt is None:
            continue
        parts = PLC._split_multicode(pt.desc or "", semantic)
        changed = False
        for p_idx, part in enumerate(parts):
            main, note, had_sep = PLC._description_parts(part, semantic)
            tokens = main.split()
            if not tokens or not PLC._code_matches(tokens[0], code_prefix):
                continue
            kept = [
                tok
                for tok in tokens[1:]
                if PLC._meaning(tok, semantic) not in ("start_curve", "end_curve")
            ]
            for meaning in ("start_curve", "end_curve"):
                tok = semantic.get(meaning, "")
                if meaning in curve_by_vertex[v_idx] and tok:
                    kept.append(tok)
            parts[p_idx] = PLC._format_part(
                PLC._format_tokens([tokens[0], *kept], semantic),
                note,
                had_sep,
                semantic,
            )
            changed = True
            break
        if changed:
            pt.desc = PLC.fix_line_command_order(PLC._join_multicode(parts, semantic), semantic)


def curve_segment(
    project: "Project",
    entity_id: int,
    span_index: int,
    *,
    sweep_degrees: float | None = None,
    radius: float | None = None,
    through_point: Sequence[float] | None = None,
    ccw: bool = True,
) -> float:
    """Set a circular arc bulge on *span_index* of *entity_id* and return the new bulge.

    Accepts ``sweep_degrees``, ``radius`` (with ``ccw`` sign), or ``through_point`` ``(x, y)``.
    """
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 2:
        raise ValueError("Select a valid polyline to curve.")
    n = len(ent.verts)
    n_spans = n if ent.closed else n - 1
    s_idx = int(span_index)
    if s_idx < 0 or s_idx >= n_spans:
        raise ValueError(f"Span index {s_idx} is out of range.")

    p0 = ent.verts[s_idx, :2]
    p1 = ent.verts[(s_idx + 1) % n, :2]

    if through_point is not None:
        pm = np.asarray(through_point, dtype=float)[:2]
        bulge = float(G.bulge_from_three_points(p0, pm, p1))
    elif radius is not None:
        chord = float(math.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        r = abs(float(radius))
        if r <= 0.0 or chord <= 1e-12 or r < 0.5 * chord:
            raise ValueError("Curve radius must be at least half the chord length.")
        half_theta = math.asin(min(1.0, (0.5 * chord) / r))
        bulge = math.tan(0.5 * half_theta) * (1.0 if ccw else -1.0)
    elif sweep_degrees is not None:
        sweep = float(sweep_degrees)
        if not math.isfinite(sweep) or abs(sweep) >= 359.0:
            raise ValueError("Curve sweep must be between -359 and 359 degrees.")
        bulge = math.tan(math.radians(sweep) / 4.0) if abs(sweep) >= 0.01 else 0.0
    else:
        raise ValueError("Provide sweep_degrees, radius, or through_point.")

    if ent.bulges is None or len(ent.bulges) != n:
        ent.bulges = np.zeros(n, dtype=float)
    ent.bulges[s_idx] = float(bulge)
    _sync_span_curve_tokens(project, ent, s_idx, abs(bulge) > 1e-12)
    pids = _ensure_point_ids_attr(project, ent)
    _mark_manual_override_if_derived(project, ent, pids)
    project.touch()
    return float(bulge)


def recurve_segment(
    project: "Project",
    entity_id: int,
    pc_index: int,
    mid_index: int,
    pt_index: int | None = None,
    *,
    remove_mid_vertex: bool = False,
) -> float:
    """Refit a circular arc through ``(pc_index, mid_index, pt_index)`` and update curve tokens."""
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 2:
        raise ValueError("Select a valid polyline to recurve.")
    n = len(ent.verts)
    i_pc = int(pc_index)
    i_mid = int(mid_index)
    i_pt = int(pt_index) if pt_index is not None else (i_mid + 1) % n
    if any(idx < 0 or idx >= n for idx in (i_pc, i_mid, i_pt)):
        raise ValueError("PC, midpoint, and PT indices must be valid vertex indices.")

    p_pc = ent.verts[i_pc, :2].copy()
    p_mid = ent.verts[i_mid, :2].copy()
    p_pt = ent.verts[i_pt, :2].copy()
    bulge = float(G.bulge_from_three_points(p_pc, p_mid, p_pt))

    pids = _ensure_point_ids_attr(project, ent)
    if remove_mid_vertex and i_pt == (i_pc + 2) % n and i_mid == (i_pc + 1) % n and n >= 3:
        ent.verts = np.delete(ent.verts, i_mid, axis=0)
        if ent.bulges is None or len(ent.bulges) != n:
            ent.bulges = np.zeros(n - 1, dtype=float)
        else:
            ent.bulges = np.delete(ent.bulges, i_mid)
        pids.pop(i_mid)
        ent.attrs["point_ids"] = list(pids)
        ent.attrs["points"] = [
            project.points[pid].number if pid is not None and pid in project.points else ""
            for pid in pids
        ]
        target_span = i_pc if i_pc < len(ent.verts) else len(ent.verts) - 1
        ent.bulges[target_span] = bulge
        _sync_span_curve_tokens(project, ent, target_span, abs(bulge) > 1e-12)
    else:
        if ent.bulges is None or len(ent.bulges) != n:
            ent.bulges = np.zeros(n, dtype=float)
        ent.bulges[i_pc] = bulge
        _sync_span_curve_tokens(project, ent, i_pc, abs(bulge) > 1e-12)

    _mark_manual_override_if_derived(project, ent, pids)
    project.touch()
    return bulge


def straighten_segment(project: "Project", entity_id: int, span_index: int) -> None:
    """Clear the curve bulge on *span_index* and remove PC/PT tokens on its endpoints."""
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 2:
        raise ValueError("Select a valid polyline to straighten.")
    n = len(ent.verts)
    s_idx = int(span_index)
    if s_idx < 0 or s_idx >= (n if ent.closed else n - 1):
        raise ValueError(f"Span index {s_idx} is out of range.")
    if ent.bulges is not None and s_idx < len(ent.bulges):
        ent.bulges[s_idx] = 0.0
        if not np.any(np.abs(ent.bulges) > 1e-14):
            ent.bulges = None
    _sync_span_curve_tokens(project, ent, s_idx, False)
    pids = _ensure_point_ids_attr(project, ent)
    _mark_manual_override_if_derived(project, ent, pids)
    project.touch()


# =============================================================================
# 5. Active-occurrence sweep & Connect lines (§4.1 & §4.2)
# =============================================================================
@dataclass
class OccurrenceInterval:
    """One occurrence of a line code instance ``CODE{string}`` in ascending point-number order."""

    label: str
    code: str
    string: str
    occurrence: int
    start_order: int                   # 0-based rank in ascending project point-number sweep
    end_order: int                     # 0-based rank of last vertex in this occurrence
    start_point_id: int
    end_point_id: int
    start_number: str
    end_number: str
    point_ids: list[int] = field(default_factory=list)
    point_numbers: list[str] = field(default_factory=list)
    section_indices: list[int] = field(default_factory=list)
    closed_properly: bool = False      # True when closed with END or X
    closed_ring: bool = False          # True when closed with X
    missing_initial_st: bool = False
    missing_end_before_st: bool = False
    missing_st_after_end: bool = False

    @property
    def seg_id(self) -> str:
        return f"{self.label}-{self.occurrence:02d}"


def _active_code_table(project: "Project"):
    if hasattr(project, "codes") and len(project.codes):
        return project.codes
    return default_codes()


def _has_flag_meaning(parsed, meaning: str, semantic_tokens: dict[str, str]) -> bool:
    token = semantic_tokens.get(meaning, "")
    if token and any(flag.casefold() == token.casefold() for flag in parsed.flags):
        return True
    if meaning == "start_line":
        return bool(parsed.flags & BEGIN_FLAGS)
    if meaning == "end_line":
        return bool(parsed.flags & END_FLAGS)
    if meaning == "close":
        return bool(parsed.flags & CLOSE_FLAGS)
    return False


def sweep_code_family_occurrences(
    project: "Project",
    base_code: str | None = None,
) -> list[OccurrenceInterval]:
    """Sweep project points in ascending point-number order and return occurrence intervals.

    If ``base_code`` is provided (e.g. ``"SW"`` or ``"SIDEWALK"``), returns only
    occurrences belonging to that code family; otherwise returns occurrences for
    all line/polygon codes.
    """
    settings = getattr(project, "settings", None) or {}
    semantic_tokens = command_map(settings.get("f2f_commands"))
    table = _active_code_table(project)
    known_codes = table.codes
    target_family = str(base_code or "").strip().casefold()

    ordered_points = sorted(project.points.values(), key=_point_sort_key)
    open_occ: dict[tuple[str, str], OccurrenceInterval] = {}
    had_closed_occ: dict[tuple[str, str], bool] = {}
    occ_counts: dict[tuple[str, str], int] = {}
    finished: list[OccurrenceInterval] = []

    for order_idx, pt in enumerate(ordered_points):
        parts = PLC._split_multicode(pt.desc or "", semantic_tokens)
        for sec_idx, part in enumerate(parts):
            main, _note, _had_sep = PLC._description_parts(part, semantic_tokens)
            if not main:
                continue
            parsed = parse_description(main, commands=semantic_tokens, known_codes=known_codes)
            if not parsed.code:
                continue
            code_upper = parsed.code.upper()
            if target_family and code_upper.casefold() != target_family:
                continue
            fc = table.get(code_upper)
            if fc is not None and fc.kind == "point" and not target_family:
                continue

            string_id = str(parsed.string or "")
            key = (code_upper.casefold(), string_id.casefold())
            label = f"{code_upper}{string_id}"
            has_st = _has_flag_meaning(parsed, "start_line", semantic_tokens)
            has_end = _has_flag_meaning(parsed, "end_line", semantic_tokens)
            has_close = _has_flag_meaning(parsed, "close", semantic_tokens)

            current = open_occ.get(key)
            if has_st and current is not None:
                # A new ST while the same label is still open: flag missing END on prior occurrence
                current.missing_end_before_st = True
                finished.append(open_occ.pop(key))
                current = None

            if current is None:
                occ_num = occ_counts.get(key, 0) + 1
                occ_counts[key] = occ_num
                was_closed_before = had_closed_occ.get(key, False)
                current = OccurrenceInterval(
                    label=label,
                    code=code_upper,
                    string=string_id,
                    occurrence=occ_num,
                    start_order=order_idx,
                    end_order=order_idx,
                    start_point_id=int(pt.id),
                    end_point_id=int(pt.id),
                    start_number=str(pt.number),
                    end_number=str(pt.number),
                    missing_initial_st=(not has_st and not was_closed_before),
                    missing_st_after_end=(not has_st and was_closed_before),
                )
                open_occ[key] = current

            current.end_order = order_idx
            current.end_point_id = int(pt.id)
            current.end_number = str(pt.number)
            current.point_ids.append(int(pt.id))
            current.point_numbers.append(str(pt.number))
            current.section_indices.append(sec_idx)

            if has_end or has_close:
                current.closed_properly = True
                current.closed_ring = bool(has_close)
                had_closed_occ[key] = True
                finished.append(open_occ.pop(key))

    for key in list(open_occ):
        finished.append(open_occ.pop(key))

    finished.sort(key=lambda occ: (occ.code.casefold(), _number_sort_key(occ.string)[0], _number_sort_key(occ.string)[1], occ.occurrence))
    return finished


def _label_is_safe_on_interval(
    occurrences: Sequence[OccurrenceInterval],
    string_id: str,
    low_order: int,
    high_order: int,
    connector_point_ids: set[int],
) -> tuple[bool, bool]:
    """Return ``(is_safe, has_prior_closed_occurrence)`` for ``string_id`` over ``[low_order, high_order]``.

    - An unclosed occurrence (``closed_properly=False``) remains live from ``start_order``
      through the end of the file (``+inf``), so it overlaps ``[low_order, high_order]``
      whenever ``start_order <= high_order``.
    - A closed occurrence occupies ``[start_order, end_order]``.
    - Any occurrence that already touches one of the connector points is also excluded
      so a single point never receives duplicate sections of the exact same label.
    """
    s_key = str(string_id or "").casefold()
    has_prior_closed = False
    for occ in occurrences:
        if occ.string.casefold() != s_key:
            continue
        if connector_point_ids.intersection(occ.point_ids):
            return False, False
        eff_end = occ.end_order if occ.closed_properly else math.inf
        if occ.start_order <= high_order and eff_end >= low_order:
            return False, False
        if occ.closed_properly and occ.end_order < low_order:
            has_prior_closed = True
    return True, has_prior_closed


@dataclass
class ConnectLinesPreview:
    """Preview of an active-occurrence Connect Lines operation (§4.2)."""

    base_code: str
    chosen_string: str
    chosen_label: str
    reused_closed_label: bool
    low_point_id: int
    high_point_id: int
    routed_point_ids: list[int]
    routed_point_numbers: list[str]
    description_updates: dict[int, tuple[str, str]]  # point_id -> (old_desc, new_desc)
    family_intervals: list[OccurrenceInterval]
    warnings: list[str] = field(default_factory=list)


def preview_connect_segments(
    project: "Project",
    point_a_id: int,
    point_b_id: int,
    *,
    base_code: str | None = None,
    snap_tolerance: float = 0.0,
) -> ConnectLinesPreview:
    """Build a non-mutating preview of connecting two points using the active-occurrence algorithm (§4.2)."""
    if int(point_a_id) == int(point_b_id):
        raise ValueError("Select two distinct points to connect.")
    pt_a = project.points.get(int(point_a_id))
    pt_b = project.points.get(int(point_b_id))
    if pt_a is None or pt_b is None:
        raise ValueError("Both endpoint survey points must exist in the project.")

    settings = getattr(project, "settings", None) or {}
    semantic_tokens = command_map(settings.get("f2f_commands"))
    table = _active_code_table(project)

    if not base_code:
        for pt in (pt_a, pt_b):
            for part in PLC._split_multicode(pt.desc or "", semantic_tokens):
                main, _n, _h = PLC._description_parts(part, semantic_tokens)
                parsed = parse_description(main, commands=semantic_tokens, known_codes=table.codes)
                if parsed.code:
                    base_code = parsed.code.upper()
                    break
            if base_code:
                break
    code_upper = str(base_code or "EP").strip().upper()

    ordered_points = sorted(project.points.values(), key=_point_sort_key)
    order_by_pid = {int(pt.id): idx for idx, pt in enumerate(ordered_points)}
    if int(pt_a.id) not in order_by_pid or int(pt_b.id) not in order_by_pid:
        raise ValueError("Endpoints must be project points.")

    if order_by_pid[int(pt_a.id)] <= order_by_pid[int(pt_b.id)]:
        low_pt, high_pt = pt_a, pt_b
    else:
        low_pt, high_pt = pt_b, pt_a
    low_order = order_by_pid[int(low_pt.id)]
    high_order = order_by_pid[int(high_pt.id)]

    # Step 3 (§4.2): Route through existing points within snap_tolerance that preserve ascending trace
    routed: list[SurveyPoint] = [low_pt]
    if snap_tolerance > 0.0 and high_order > low_order + 1:
        p0 = np.array([float(low_pt.x), float(low_pt.y)], dtype=float)
        p1 = np.array([float(high_pt.x), float(high_pt.y)], dtype=float)
        chord = p1 - p0
        chord_len_sq = float(chord[0] ** 2 + chord[1] ** 2)
        if chord_len_sq > 1e-12:
            candidates: list[tuple[float, int, SurveyPoint]] = []
            for ord_idx in range(low_order + 1, high_order):
                cand = ordered_points[ord_idx]
                rel = np.array([float(cand.x) - p0[0], float(cand.y) - p0[1]], dtype=float)
                t = float((rel[0] * chord[0] + rel[1] * chord[1]) / chord_len_sq)
                if t <= 1e-4 or t >= 1.0 - 1e-4:
                    continue
                proj = p0 + t * chord
                dist = float(math.hypot(float(cand.x) - proj[0], float(cand.y) - proj[1]))
                if dist <= float(snap_tolerance):
                    candidates.append((t, ord_idx, cand))
            candidates.sort(key=lambda item: item[0])
            last_ord = low_order
            for _t, ord_idx, cand in candidates:
                if ord_idx > last_ord:
                    routed.append(cand)
                    last_ord = ord_idx
    routed.append(high_pt)
    routed_ids = [int(p.id) for p in routed]
    routed_id_set = set(routed_ids)

    # Step 1 & 2 (§4.2): Sweep family occurrence intervals and choose safe label
    intervals = sweep_code_family_occurrences(project, code_upper)
    existing_numeric = sorted(
        {int(occ.string) for occ in intervals if occ.string.isdigit() and int(occ.string) > 0}
    )
    chosen_string: str | None = None
    reused_closed = False

    # Priority 1: lowest eligible existing numeric label with a previously closed occurrence
    for num in existing_numeric:
        safe, had_prior = _label_is_safe_on_interval(
            intervals, str(num), low_order, high_order, routed_id_set
        )
        if safe and had_prior:
            chosen_string = str(num)
            reused_closed = True
            break

    # Priority 2: lowest otherwise-free suffix 1, 2, 3, ...
    if chosen_string is None:
        cand_num = 1
        while True:
            safe, _had_prior = _label_is_safe_on_interval(
                intervals, str(cand_num), low_order, high_order, routed_id_set
            )
            if safe:
                chosen_string = str(cand_num)
                break
            cand_num += 1

    chosen_label = f"{code_upper}{chosen_string}"
    st_token = semantic_tokens.get("start_line", "ST") or "ST"
    end_token = semantic_tokens.get("end_line", "END") or "END"

    description_updates: dict[int, tuple[str, str]] = {}
    for idx, pt in enumerate(routed):
        if idx == 0:
            sec = PLC._format_tokens([chosen_label, st_token], semantic_tokens)
        elif idx == len(routed) - 1:
            sec = PLC._format_tokens([chosen_label, end_token], semantic_tokens)
        else:
            sec = chosen_label
        old_desc = str(pt.desc or "")
        parts = PLC._split_multicode(old_desc, semantic_tokens)
        parts.append(sec)
        new_desc = PLC._join_multicode(parts, semantic_tokens)
        description_updates[int(pt.id)] = (old_desc, new_desc)

    return ConnectLinesPreview(
        base_code=code_upper,
        chosen_string=chosen_string,
        chosen_label=chosen_label,
        reused_closed_label=reused_closed,
        low_point_id=int(low_pt.id),
        high_point_id=int(high_pt.id),
        routed_point_ids=routed_ids,
        routed_point_numbers=[str(p.number) for p in routed],
        description_updates=description_updates,
        family_intervals=intervals,
    )


def apply_connect_segments(
    project: "Project",
    preview: ConnectLinesPreview,
    *,
    rebuild_linework: bool = True,
) -> ConnectLinesPreview:
    """Apply a :class:`ConnectLinesPreview` to *project* without altering any point numbers."""
    for pid, (_old_desc, new_desc) in preview.description_updates.items():
        pt = project.points.get(int(pid))
        if pt is None:
            raise ValueError(f"Point ID {pid} no longer exists.")
        pt.desc = new_desc
    if rebuild_linework:
        if len(project.codes) == 0:
            project.codes = default_codes()
        project.process_linework(order="number")
    else:
        project.touch()
    return preview


# =============================================================================
# 6. Join lines, average (geometry-only, numbers protected — §2.4 & §3.6)
# =============================================================================
def join_lines_average(
    project: "Project",
    entity_a_id: int,
    end_a: str,
    entity_b_id: int,
    end_b: str,
) -> dict:
    """Move two line endpoints to their average XYZ while preserving both point numbers."""
    ent_a = project.entities.get(int(entity_a_id))
    ent_b = project.entities.get(int(entity_b_id))
    if not isinstance(ent_a, Polyline) or not isinstance(ent_b, Polyline):
        raise ValueError("Select two valid polylines to join-average.")
    if len(ent_a.verts) < 2 or len(ent_b.verts) < 2:
        raise ValueError("Both polylines must have at least 2 vertices.")

    idx_a = 0 if str(end_a).strip().lower() in ("start", "0", "first") else len(ent_a.verts) - 1
    idx_b = 0 if str(end_b).strip().lower() in ("start", "0", "first") else len(ent_b.verts) - 1

    pids_a = _ensure_point_ids_attr(project, ent_a)
    pids_b = _ensure_point_ids_attr(project, ent_b)
    pid_a = pids_a[idx_a] if idx_a < len(pids_a) else None
    pid_b = pids_b[idx_b] if idx_b < len(pids_b) else None
    pt_a = project.points.get(pid_a) if pid_a is not None else None
    pt_b = project.points.get(pid_b) if pid_b is not None else None

    xa = float(pt_a.x if pt_a is not None else ent_a.verts[idx_a, 0])
    ya = float(pt_a.y if pt_a is not None else ent_a.verts[idx_a, 1])
    za = float(pt_a.z if pt_a is not None else ent_a.verts[idx_a, 2])
    xb = float(pt_b.x if pt_b is not None else ent_b.verts[idx_b, 0])
    yb = float(pt_b.y if pt_b is not None else ent_b.verts[idx_b, 1])
    zb = float(pt_b.z if pt_b is not None else ent_b.verts[idx_b, 2])

    mx = 0.5 * (xa + xb)
    my = 0.5 * (ya + yb)
    if math.isfinite(za) and math.isfinite(zb):
        mz = 0.5 * (za + zb)
    elif math.isfinite(za):
        mz = za
    elif math.isfinite(zb):
        mz = zb
    else:
        mz = NAN

    ent_a.verts[idx_a, 0], ent_a.verts[idx_a, 1], ent_a.verts[idx_a, 2] = mx, my, mz
    ent_b.verts[idx_b, 0], ent_b.verts[idx_b, 1], ent_b.verts[idx_b, 2] = mx, my, mz

    if pt_a is not None:
        pt_a.x, pt_a.y, pt_a.z = mx, my, mz
        _sync_point_to_polylines(project, pt_a.id)
    else:
        _mark_manual_override_if_derived(project, ent_a, pids_a)

    if pt_b is not None:
        pt_b.x, pt_b.y, pt_b.z = mx, my, mz
        _sync_point_to_polylines(project, pt_b.id)
    else:
        _mark_manual_override_if_derived(project, ent_b, pids_b)

    project.touch()
    return {
        "x": mx,
        "y": my,
        "z": mz,
        "point_a_id": pid_a,
        "point_b_id": pid_b,
        "point_a_number": pt_a.number if pt_a is not None else None,
        "point_b_number": pt_b.number if pt_b is not None else None,
    }


# =============================================================================
# 7. Move end to end (§3.6)
# =============================================================================
def move_line_end_to_end(
    project: "Project",
    mover_entity_id: int,
    mover_end: str,
    target_entity_id: int,
    target_end: str,
    *,
    whole_line: bool = False,
    propagate_to_points: bool = True,
) -> dict:
    """Move the mover line's endpoint onto the target line's endpoint via the point link.

    When ``whole_line=False`` (default), moves only the selected endpoint vertex
    (and its linked survey point). When ``whole_line=True``, translates every
    vertex of the mover line by ``(dx, dy, dz)``. Point numbers never change.
    """
    mover = project.entities.get(int(mover_entity_id))
    target = project.entities.get(int(target_entity_id))
    if not isinstance(mover, Polyline) or not isinstance(target, Polyline):
        raise ValueError("Select valid mover and target polylines.")
    if len(mover.verts) < 2 or len(target.verts) < 2:
        raise ValueError("Both polylines must have at least 2 vertices.")

    m_idx = 0 if str(mover_end).strip().lower() in ("start", "0", "first") else len(mover.verts) - 1
    t_idx = 0 if str(target_end).strip().lower() in ("start", "0", "first") else len(target.verts) - 1

    m_pids = _ensure_point_ids_attr(project, mover)
    t_pids = _ensure_point_ids_attr(project, target)
    t_pid = t_pids[t_idx] if t_idx < len(t_pids) else None
    t_pt = project.points.get(t_pid) if t_pid is not None else None

    tx = float(t_pt.x if t_pt is not None else target.verts[t_idx, 0])
    ty = float(t_pt.y if t_pt is not None else target.verts[t_idx, 1])
    tz = float(t_pt.z if t_pt is not None else target.verts[t_idx, 2])

    mx = float(mover.verts[m_idx, 0])
    my = float(mover.verts[m_idx, 1])
    mz = float(mover.verts[m_idx, 2])
    dx, dy = tx - mx, ty - my
    dz = (tz - mz) if (math.isfinite(tz) and math.isfinite(mz)) else 0.0

    indices = list(range(len(mover.verts))) if whole_line else [m_idx]
    moved_pids: list[int] = []
    seen_pids: set[int] = set()
    for vi in indices:
        mover.verts[vi, 0] += dx
        mover.verts[vi, 1] += dy
        if math.isfinite(mover.verts[vi, 2]):
            mover.verts[vi, 2] += dz
        pid = m_pids[vi] if vi < len(m_pids) else None
        if propagate_to_points and pid is not None and pid in project.points and pid not in seen_pids:
            seen_pids.add(int(pid))
            pt = project.points[pid]
            pt.x = float(mover.verts[vi, 0])
            pt.y = float(mover.verts[vi, 1])
            if math.isfinite(mover.verts[vi, 2]):
                pt.z = float(mover.verts[vi, 2])
            _sync_point_to_polylines(project, pid)
            moved_pids.append(int(pid))

    if not moved_pids:
        _mark_manual_override_if_derived(project, mover, m_pids)
    project.touch()
    return {
        "mover_entity_id": int(mover_entity_id),
        "target_entity_id": int(target_entity_id),
        "dx": dx,
        "dy": dy,
        "dz": dz,
        "moved_point_ids": moved_pids,
    }


# =============================================================================
# 8. Add node (pure vertex) & Inject point (numbered SurveyPoint) (§3.6)
# =============================================================================
def add_polyline_node(
    project: "Project",
    entity_id: int,
    span_index: int,
    fraction: float = 0.5,
    xyz: Sequence[float] | None = None,
) -> dict:
    """Insert a pure geometry node (no survey point) on *span_index* of *entity_id*."""
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 2:
        raise ValueError("Select a valid polyline to add a node.")
    n = len(ent.verts)
    n_spans = n if ent.closed else n - 1
    s_idx = int(span_index)
    if s_idx < 0 or s_idx >= n_spans:
        raise ValueError(f"Span index {s_idx} is out of range.")

    pids = _ensure_point_ids_attr(project, ent)
    next_idx = (s_idx + 1) % n
    b_val = float(ent.bulges[s_idx]) if ent.bulges is not None and s_idx < len(ent.bulges) else 0.0
    left_b, right_b = split_bulge(b_val, float(fraction))

    if xyz is not None:
        arr = np.asarray(xyz, dtype=float)
        pt_xyz = np.array([float(arr[0]), float(arr[1]), float(arr[2]) if len(arr) >= 3 else float(ent.verts[s_idx, 2])], dtype=float)
    else:
        pt_xyz = bulge_point(ent.verts[s_idx], ent.verts[next_idx], b_val, float(fraction))

    insert_pos = n if (ent.closed and s_idx == n - 1) else s_idx + 1
    ent.verts = np.insert(ent.verts, insert_pos, pt_xyz, axis=0)
    if ent.bulges is not None and len(ent.bulges) == n:
        bulges = list(map(float, ent.bulges))
        bulges[s_idx] = left_b
        bulges.insert(insert_pos, right_b)
        ent.bulges = np.asarray(bulges, dtype=float)

    pids.insert(insert_pos, None)
    ent.attrs["point_ids"] = list(pids)
    ent.attrs["points"] = [
        project.points[pid].number if pid is not None and pid in project.points else ""
        for pid in pids
    ]
    _mark_manual_override_if_derived(project, ent, pids)
    project.touch()
    return {
        "entity_id": int(entity_id),
        "vertex_index": insert_pos,
        "point_id": None,
        "x": float(pt_xyz[0]),
        "y": float(pt_xyz[1]),
        "z": float(pt_xyz[2]),
    }


def _allocate_range_preserving_number(project: "Project", after_pid: int | None, before_pid: int | None) -> str:
    used = {str(p.number).strip().casefold() for p in project.points.values()}
    if after_pid is not None and before_pid is not None:
        pa = project.points.get(after_pid)
        pb = project.points.get(before_pid)
        if pa is not None and pb is not None and str(pa.number).isdigit() and str(pb.number).isdigit():
            na, nb = int(pa.number), int(pb.number)
            lo, hi = (na, nb) if na <= nb else (nb, na)
            for cand in range(lo + 1, hi):
                if str(cand).casefold() not in used:
                    return str(cand)
    return project.next_point_number()


def inject_survey_point(
    project: "Project",
    entity_id: int,
    span_index: int,
    fraction: float = 0.5,
    xyz: Sequence[float] | None = None,
    *,
    number: str | None = None,
) -> SurveyPoint:
    """Insert a real numbered :class:`SurveyPoint` on *span_index* of *entity_id*."""
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 2:
        raise ValueError("Select a valid polyline to inject a survey point.")
    n = len(ent.verts)
    n_spans = n if ent.closed else n - 1
    s_idx = int(span_index)
    if s_idx < 0 or s_idx >= n_spans:
        raise ValueError(f"Span index {s_idx} is out of range.")

    pids = _ensure_point_ids_attr(project, ent)
    next_idx = (s_idx + 1) % n
    b_val = float(ent.bulges[s_idx]) if ent.bulges is not None and s_idx < len(ent.bulges) else 0.0
    left_b, right_b = split_bulge(b_val, float(fraction))

    if xyz is not None:
        arr = np.asarray(xyz, dtype=float)
        pt_xyz = np.array([float(arr[0]), float(arr[1]), float(arr[2]) if len(arr) >= 3 else float(ent.verts[s_idx, 2])], dtype=float)
    else:
        pt_xyz = bulge_point(ent.verts[s_idx], ent.verts[next_idx], b_val, float(fraction))

    pid_before = pids[s_idx] if s_idx < len(pids) else None
    pid_after = pids[next_idx] if next_idx < len(pids) else None
    pt_num = str(number).strip() if number else _allocate_range_preserving_number(project, pid_before, pid_after)
    if any(str(p.number).strip().casefold() == pt_num.casefold() for p in project.points.values()):
        raise ValueError(f"Point number {pt_num} is already in use.")

    code = str((ent.attrs or {}).get("code") or "").strip()
    string = str((ent.attrs or {}).get("string") or "").strip()
    desc = f"{code}{string}" if code else ""
    layer = ent.layer or "POINTS"
    pt = project.add_point(float(pt_xyz[0]), float(pt_xyz[1]), float(pt_xyz[2]), number=pt_num, desc=desc, layer=layer)

    insert_pos = n if (ent.closed and s_idx == n - 1) else s_idx + 1
    ent.verts = np.insert(ent.verts, insert_pos, pt_xyz, axis=0)
    if ent.bulges is not None and len(ent.bulges) == n:
        bulges = list(map(float, ent.bulges))
        bulges[s_idx] = left_b
        bulges.insert(insert_pos, right_b)
        ent.bulges = np.asarray(bulges, dtype=float)

    pids.insert(insert_pos, pt.id)
    ent.attrs["point_ids"] = list(pids)
    ent.attrs["points"] = [
        project.points[pid].number if pid is not None and pid in project.points else ""
        for pid in pids
    ]
    _mark_manual_override_if_derived(project, ent, pids)
    project.touch()
    return pt


# =============================================================================
# 9. COGO from lines (§3.6)
# =============================================================================
def cogo_points_from_lines(
    project: "Project",
    entity_ids: Sequence[int] | None = None,
    *,
    include_interior: bool = False,
    dedup_tolerance: float = 1e-3,
    default_desc: str = "COGO",
) -> list[SurveyPoint]:
    """Create survey points from imported/unlinked polylines, deduplicating shared vertices.

    Records the vertex-to-point mapping in ``entity.attrs["point_ids"]`` and
    ``entity.attrs["points"]`` for every processed polyline.
    """
    tol = max(1e-6, float(dedup_tolerance))
    target_ids = (
        [int(eid) for eid in entity_ids]
        if entity_ids is not None
        else [int(e.id) for e in project.polylines()]
    )

    # Spatial grid index of existing + newly created points so shared endpoints deduplicate
    spatial_map: dict[tuple[int, int], SurveyPoint] = {}
    for pt in project.points.values():
        key = (int(round(float(pt.x) / tol)), int(round(float(pt.y) / tol)))
        spatial_map.setdefault(key, pt)

    def find_or_create(x: float, y: float, z: float, desc: str, layer: str) -> tuple[SurveyPoint, bool]:
        gx = int(round(x / tol))
        gy = int(round(y / tol))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cand = spatial_map.get((gx + dx, gy + dy))
                if cand is not None and math.hypot(float(cand.x) - x, float(cand.y) - y) <= tol:
                    return cand, False
        pt = project.add_point(x, y, z if math.isfinite(z) else NAN, desc=desc, layer=layer)
        spatial_map[(gx, gy)] = pt
        return pt, True

    created: list[SurveyPoint] = []
    for eid in target_ids:
        ent = project.entities.get(eid)
        if not isinstance(ent, Polyline) or len(ent.verts) < 1:
            continue
        n = len(ent.verts)
        pids = _ensure_point_ids_attr(project, ent)
        code = str((ent.attrs or {}).get("code") or default_desc or "COGO").strip()
        string = str((ent.attrs or {}).get("string") or "").strip()
        desc = f"{code}{string}" if code else default_desc

        target_indices = range(n) if include_interior else ({0, n - 1} if n >= 2 else {0})
        for vi in sorted(target_indices):
            existing_pid = pids[vi] if vi < len(pids) else None
            if existing_pid is not None and existing_pid in project.points:
                continue
            vx, vy, vz = float(ent.verts[vi, 0]), float(ent.verts[vi, 1]), float(ent.verts[vi, 2])
            pt, is_new = find_or_create(vx, vy, vz, desc, ent.layer or "POINTS")
            pids[vi] = pt.id
            if is_new:
                created.append(pt)

        ent.attrs["point_ids"] = list(pids)
        ent.attrs["points"] = [
            project.points[pid].number if pid is not None and pid in project.points else ""
            for pid in pids
        ]

    project.touch()
    return created


# =============================================================================
# 10. Shrink Line Commands — sequence repair & suffix compaction (§3.5 & §7)
# =============================================================================
@dataclass
class PointDescriptionChange:
    """One proposed point description edit produced by Shrink Line Commands."""

    point_id: int
    point_number: str
    old_desc: str
    new_desc: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class CompactionMapping:
    """One closed occurrence suffix compaction (e.g. ``SIDEWALK10`` -> ``SIDEWALK1``)."""

    code: str
    old_string: str
    new_string: str
    occurrence: int
    start_number: str
    end_number: str
    point_ids: list[int] = field(default_factory=list)

    @property
    def old_label(self) -> str:
        return f"{self.code}{self.old_string}"

    @property
    def new_label(self) -> str:
        return f"{self.code}{self.new_string}"


@dataclass
class ShrinkCommandPreview:
    """Preview of Shrink Line Commands before applying."""

    changes: list[PointDescriptionChange] = field(default_factory=list)
    compactions: list[CompactionMapping] = field(default_factory=list)
    skipped_occurrences: list[str] = field(default_factory=list)
    repaired_counts: dict[str, int] = field(default_factory=dict)


def _rewrite_section_at_index(
    desc: str,
    section_index: int,
    *,
    new_code_label: str | None = None,
    add_boundary: str | None = None,
    commands=None,
) -> str:
    """Rewrite a specific multicode section index while leaving all other sections untouched."""
    semantic = command_map(commands)
    parts = PLC._split_multicode(desc or "", semantic)
    if section_index < 0 or section_index >= len(parts):
        return desc or ""

    main, note, had_sep = PLC._description_parts(parts[section_index], semantic)
    tokens = main.split()
    if not tokens:
        return desc or ""

    head = new_code_label if new_code_label is not None else tokens[0]
    rest = list(tokens[1:])
    if add_boundary:
        cmd_tok = semantic.get(add_boundary, "")
        if cmd_tok and not any(PLC._meaning(t, semantic) == add_boundary for t in rest):
            # Insert start_line before start_curve if PC is present so command order stays valid
            if add_boundary == "start_line":
                rest.insert(0, cmd_tok)
            else:
                rest.append(cmd_tok)

    parts[section_index] = PLC._format_part(
        PLC._format_tokens([head, *rest], semantic),
        note,
        had_sep,
        semantic,
    )
    return PLC.fix_line_command_order(PLC._join_multicode(parts, semantic), semantic)


def preview_shrink_line_commands(
    project: "Project",
    *,
    repair_missing_st_end: bool = True,
    compact_suffixes: bool = True,
    base_code: str | None = None,
) -> ShrinkCommandPreview:
    """Build a per-point preview of sequence break repairs and safe suffix compaction (§3.5).

    Never alters point numbers, coordinates, vertex order, or unrelated multicode sections.
    """
    settings = getattr(project, "settings", None) or {}
    semantic = command_map(settings.get("f2f_commands"))
    occurrences = sweep_code_family_occurrences(project, base_code=base_code)

    working_desc: dict[int, str] = {int(pid): str(pt.desc or "") for pid, pt in project.points.items()}
    reasons_by_pid: dict[int, list[str]] = {}
    repaired_counts = {
        "Missing initial ST": 0,
        "Missing END before ST": 0,
        "Missing ST after END": 0,
        "Compacted suffix": 0,
    }
    compactions: list[CompactionMapping] = []
    skipped: list[str] = []

    # Part A: Sequence break repairs (Missing initial ST, Missing END before ST, Missing ST after END)
    if repair_missing_st_end:
        for occ in occurrences:
            if len(occ.point_ids) < 2:
                continue
            if occ.missing_initial_st:
                pid0 = occ.point_ids[0]
                sec0 = occ.section_indices[0]
                working_desc[pid0] = _rewrite_section_at_index(
                    working_desc[pid0], sec0, add_boundary="start_line", commands=semantic
                )
                reasons_by_pid.setdefault(pid0, []).append(f"Add Missing initial ST on {occ.label}")
                repaired_counts["Missing initial ST"] += 1

            if occ.missing_st_after_end:
                pid0 = occ.point_ids[0]
                sec0 = occ.section_indices[0]
                working_desc[pid0] = _rewrite_section_at_index(
                    working_desc[pid0], sec0, add_boundary="start_line", commands=semantic
                )
                reasons_by_pid.setdefault(pid0, []).append(f"Add Missing ST after END on {occ.label}")
                repaired_counts["Missing ST after END"] += 1

            if occ.missing_end_before_st:
                pid_last = occ.point_ids[-1]
                sec_last = occ.section_indices[-1]
                working_desc[pid_last] = _rewrite_section_at_index(
                    working_desc[pid_last], sec_last, add_boundary="end_line", commands=semantic
                )
                occ.closed_properly = True
                reasons_by_pid.setdefault(pid_last, []).append(f"Add Missing END before ST on {occ.label}")
                repaired_counts["Missing END before ST"] += 1

    # Part B: Safe compaction of jumped-to numeric suffixes (e.g. SIDEWALK10 -> SIDEWALK1)
    if compact_suffixes:
        by_code: dict[str, list[OccurrenceInterval]] = {}
        for occ in occurrences:
            by_code.setdefault(occ.code.upper(), []).append(occ)

        for code_upper, fam_occs in sorted(by_code.items()):
            # Process in ascending start_order so earlier occurrences claim low labels first
            fam_sorted = sorted(fam_occs, key=lambda o: (o.start_order, o.end_order))
            for occ in fam_sorted:
                if not occ.string.isdigit():
                    continue
                cur_num = int(occ.string)
                if cur_num <= 1:
                    continue
                if not occ.closed_properly or len(occ.point_ids) < 2:
                    skipped.append(
                        f"{occ.seg_id} (pts {occ.start_number}..{occ.end_number}): left as {occ.label} (occurrence is still open)"
                    )
                    continue

                occ_pids = set(occ.point_ids)
                chosen_num: int | None = None
                for cand_num in range(1, cur_num):
                    safe, _prior = _label_is_safe_on_interval(
                        fam_sorted,
                        str(cand_num),
                        occ.start_order,
                        occ.end_order,
                        occ_pids,
                    )
                    if safe:
                        chosen_num = cand_num
                        break

                if chosen_num is None:
                    continue

                old_string = occ.string
                new_string = str(chosen_num)
                new_label = f"{code_upper}{new_string}"
                for pid, sec_idx in zip(occ.point_ids, occ.section_indices):
                    working_desc[pid] = _rewrite_section_at_index(
                        working_desc[pid], sec_idx, new_code_label=new_label, commands=semantic
                    )
                    reasons_by_pid.setdefault(pid, []).append(
                        f"Compact {occ.label} -> {new_label}"
                    )
                occ.string = new_string
                occ.label = new_label
                repaired_counts["Compacted suffix"] += 1
                compactions.append(
                    CompactionMapping(
                        code=code_upper,
                        old_string=old_string,
                        new_string=new_string,
                        occurrence=occ.occurrence,
                        start_number=occ.start_number,
                        end_number=occ.end_number,
                        point_ids=list(occ.point_ids),
                    )
                )

    changes: list[PointDescriptionChange] = []
    for pt in sorted(project.points.values(), key=_point_sort_key):
        pid = int(pt.id)
        old_d = str(pt.desc or "")
        new_d = working_desc.get(pid, old_d)
        if new_d != old_d:
            changes.append(
                PointDescriptionChange(
                    point_id=pid,
                    point_number=str(pt.number),
                    old_desc=old_d,
                    new_desc=new_d,
                    reasons=reasons_by_pid.get(pid, []),
                )
            )

    return ShrinkCommandPreview(
        changes=changes,
        compactions=compactions,
        skipped_occurrences=skipped,
        repaired_counts=repaired_counts,
    )


def apply_shrink_line_commands(
    project: "Project",
    preview: ShrinkCommandPreview | None = None,
    *,
    rebuild_linework: bool = True,
) -> ShrinkCommandPreview:
    """Apply a :class:`ShrinkCommandPreview` to *project* and rebuild linework in number order."""
    if preview is None:
        preview = preview_shrink_line_commands(project)
    for change in preview.changes:
        pt = project.points.get(int(change.point_id))
        if pt is not None:
            pt.desc = change.new_desc
    if rebuild_linework and preview.changes:
        if len(project.codes) == 0:
            project.codes = default_codes()
        project.process_linework(order="number")
    elif preview.changes:
        project.touch()
    return preview


# =============================================================================
# 11. Auto-nudge crossing solver (Phase 4 — §3.6 & §6)
# =============================================================================
@dataclass
class AutoNudgeStep:
    """One accepted crossing-resolution move chosen by :func:`auto_nudge_crossings`."""

    method: str                        # "untwist" | "flip-past" | "micro-nudge"
    description: str
    entity_ids: list[int]
    point_ids: list[int]
    crossings_before: int
    crossings_after: int
    points_moved: int
    displacement: float


@dataclass
class AutoNudgeResult:
    """Summary and unfixable residue from :func:`auto_nudge_crossings`."""

    initial_crossings: int
    remaining_crossings: int
    crossings_removed: int
    points_moved: int
    total_displacement: float
    steps: list[AutoNudgeStep] = field(default_factory=list)
    residue: list = field(default_factory=list)  # list[CrossingFinding]

    @property
    def resolved_all(self) -> bool:
        return self.remaining_crossings == 0


def _capture_entities_and_points(project: "Project", entity_ids: Sequence[int]) -> dict:
    ent_snap = {}
    pt_ids: set[int] = set()
    for eid in set(int(x) for x in entity_ids):
        ent = project.entities.get(eid)
        if isinstance(ent, Polyline):
            ent_snap[eid] = (
                ent.verts.copy(),
                None if ent.bulges is None else ent.bulges.copy(),
                copy.deepcopy(ent.attrs),
                str(ent.derived or ""),
            )
            for pid in (ent.attrs or {}).get("point_ids") or ():
                if pid is not None:
                    try:
                        pt_ids.add(int(pid))
                    except (TypeError, ValueError):
                        pass
    pt_snap = {
        pid: (float(project.points[pid].x), float(project.points[pid].y), float(project.points[pid].z))
        for pid in pt_ids
        if pid in project.points
    }
    return {"entities": ent_snap, "points": pt_snap}


def _restore_entities_and_points(project: "Project", snap: dict) -> None:
    for eid, (verts, bulges, attrs, derived) in snap["entities"].items():
        ent = project.entities.get(eid)
        if isinstance(ent, Polyline):
            ent.verts = verts.copy()
            ent.bulges = None if bulges is None else bulges.copy()
            ent.attrs = copy.deepcopy(attrs)
            ent.derived = derived
    for pid, (x, y, z) in snap["points"].items():
        pt = project.points.get(pid)
        if pt is not None:
            pt.x, pt.y, pt.z = x, y, z
            _sync_point_to_polylines(project, pid)


def _move_vertex_xy(project: "Project", entity: Polyline, v_idx: int, nx: float, ny: float) -> list[int]:
    pids = _ensure_point_ids_attr(project, entity)
    pid = pids[v_idx] if v_idx < len(pids) else None
    entity.verts[v_idx, 0] = float(nx)
    entity.verts[v_idx, 1] = float(ny)
    if pid is not None and pid in project.points:
        pt = project.points[pid]
        pt.x = float(nx)
        pt.y = float(ny)
        _sync_point_to_polylines(project, pid)
        return [int(pid)]
    _mark_manual_override_if_derived(project, entity, pids)
    return []


def auto_nudge_crossings(
    project: "Project",
    *,
    max_iterations: int = 25,
    max_displacement: float = 5.0,
    step_sizes: Sequence[float] = (0.05, 0.15, 0.35, 0.75, 1.5, 3.0, 5.0),
) -> AutoNudgeResult:
    """Iteratively resolve breakline crossings using untwist, flip-past, and micro-nudge (§3.6).

    Candidates are scored lexicographically by:
    1. **Most crossings removed** (``crossings_before - crossings_after``)
    2. **Fewest points/vertices moved**
    3. **Least total 2D displacement**

    Iterates until zero crossings remain or no candidate within ``max_displacement``
    can reduce the crossing count; any remaining crossings are returned in ``residue``.
    Point numbers are never changed.
    """
    from .crossings import detect_breakline_crossings

    initial_list = detect_breakline_crossings(project)
    initial_count = len(initial_list)
    if initial_count == 0:
        return AutoNudgeResult(
            initial_crossings=0,
            remaining_crossings=0,
            crossings_removed=0,
            points_moved=0,
            total_displacement=0.0,
        )

    steps: list[AutoNudgeStep] = []
    moved_point_tracker: set[int] = set()
    moved_vertex_tracker: set[tuple[int, int]] = set()
    total_disp = 0.0
    allowed_steps = [float(s) for s in step_sizes if 0.0 < float(s) <= float(max_displacement)]
    if float(max_displacement) not in allowed_steps:
        allowed_steps.append(float(max_displacement))
    allowed_steps.sort()

    for _iteration in range(max(1, int(max_iterations))):
        cur_crossings = detect_breakline_crossings(project)
        cur_count = len(cur_crossings)
        if cur_count == 0:
            break

        # Prioritise self-crossings first, then two-line crossings
        ordered_targets = sorted(cur_crossings, key=lambda c: (0 if c.is_self_crossing else 1, c.seg_a_id, c.seg_b_id))
        accepted_step: AutoNudgeStep | None = None

        for target in ordered_targets:
            eids = [target.entity_a_id] if target.is_self_crossing else [target.entity_a_id, target.entity_b_id]
            base_snap = _capture_entities_and_points(project, eids)
            best_score: tuple[int, int, float] | None = None
            best_after_snap: dict | None = None
            best_meta: tuple[str, str, list[int], int, float] | None = None

            def evaluate_trial(method: str, desc: str, moved_pids: list[int], n_moved: int, disp: float):
                nonlocal best_score, best_after_snap, best_meta
                if disp > float(max_displacement) + 1e-6 and method != "untwist":
                    return
                after_crossings = detect_breakline_crossings(project)
                after_count = len(after_crossings)
                removed = cur_count - after_count
                if removed <= 0:
                    return
                score = (-removed, int(n_moved), round(float(disp), 6))
                if best_score is None or score < best_score:
                    best_score = score
                    best_after_snap = _capture_entities_and_points(project, eids)
                    best_meta = (method, desc, list(moved_pids), after_count, float(disp))

            # Candidate 1: Untwist bowtie (for self-crossings)
            if target.is_self_crossing:
                for swap_pts in (False, True):
                    _restore_entities_and_points(project, base_snap)
                    try:
                        res = untwist_polyline(
                            project,
                            target.entity_a_id,
                            target.span_a,
                            target.span_b,
                            swap_point_coordinates=swap_pts,
                        )
                        rev_idx = res["reversed_indices"]
                        evaluate_trial(
                            "untwist",
                            f"Untwist bowtie on {target.seg_a_id} (spans {target.span_a + 1} & {target.span_b + 1})",
                            [],
                            len(rev_idx),
                            0.0,
                        )
                    except ValueError:
                        pass

            # Candidate 2: Flip-past interior vertex
            ent_a = project.entities.get(target.entity_a_id)
            ent_b = project.entities.get(target.entity_b_id)
            if isinstance(ent_a, Polyline) and isinstance(ent_b, Polyline):
                for ent_obj, span_idx, seg_label in (
                    (ent_a, target.span_a, target.seg_a_id),
                    (ent_b, target.span_b, target.seg_b_id),
                ):
                    n_v = len(ent_obj.verts)
                    for vi in (span_idx, (span_idx + 1) % n_v):
                        if not ent_obj.closed and vi in (0, n_v - 1):
                            continue
                        _restore_entities_and_points(project, base_snap)
                        orig_xy = ent_obj.verts[vi, :2].copy()
                        try:
                            flip_past(project, ent_obj.id, [vi])
                            new_xy = ent_obj.verts[vi, :2]
                            disp = float(math.hypot(new_xy[0] - orig_xy[0], new_xy[1] - orig_xy[1]))
                            pids = _ensure_point_ids_attr(project, ent_obj)
                            mpid = [int(pids[vi])] if vi < len(pids) and pids[vi] is not None else []
                            evaluate_trial(
                                "flip-past",
                                f"Flip vertex {vi + 1} on {seg_label}",
                                mpid,
                                1,
                                disp,
                            )
                        except ValueError:
                            pass

            # Candidate 3: Per-point micro-nudge along crossing normal / endpoint clearance
            _restore_entities_and_points(project, base_snap)
            ent_a = project.entities.get(target.entity_a_id)
            ent_b = project.entities.get(target.entity_b_id)
            if isinstance(ent_a, Polyline) and isinstance(ent_b, Polyline):
                na, nb = len(ent_a.verts), len(ent_b.verts)
                ia0, ia1 = target.span_a, (target.span_a + 1) % na
                ib0, ib1 = target.span_b, (target.span_b + 1) % nb

                span_pairs = [
                    (ent_a, target.seg_a_id, ia0, ia1, ent_b, ib0, ib1),
                    (ent_b, target.seg_b_id, ib0, ib1, ent_a, ia0, ia1),
                ]
                for mover_ent, mover_label, m0, m1, other_ent, o0, o1 in span_pairs:
                    q0 = other_ent.verts[o0, :2].copy()
                    q1 = other_ent.verts[o1, :2].copy()
                    odx, ody = float(q1[0] - q0[0]), float(q1[1] - q0[1])
                    olen = math.hypot(odx, ody)
                    if olen <= 1e-12:
                        continue
                    # Unit normal and unit tangent of the other crossing segment
                    ux, uy = odx / olen, ody / olen
                    nx, ny = -uy, ux

                    for m_vi in (m0, m1):
                        p_orig = mover_ent.verts[m_vi, :2].copy()
                        # Signed perpendicular distance from p_orig to line q0 -> q1
                        signed_d = (float(p_orig[0] - q0[0]) * nx) + (float(p_orig[1] - q0[1]) * ny)
                        # Exact normal clearance distance to cross to the opposite side of q0->q1
                        exact_clear = abs(signed_d) + 0.02
                        cand_dists = sorted({d for d in (*allowed_steps, exact_clear) if 0.0 < d <= float(max_displacement)})

                        # Directions to test: across normal (-sign(signed_d)), +normal, and past nearer endpoint of q0->q1
                        dir_sign = -1.0 if signed_d >= 0.0 else 1.0
                        dirs = [
                            (dir_sign * nx, dir_sign * ny),
                            (-dir_sign * nx, -dir_sign * ny),
                            (-ux, -uy),
                            (ux, uy),
                        ]
                        for d_step in cand_dists:
                            for dx_u, dy_u in dirs:
                                _restore_entities_and_points(project, base_snap)
                                moved_pids = _move_vertex_xy(
                                    project,
                                    mover_ent,
                                    m_vi,
                                    float(p_orig[0] + d_step * dx_u),
                                    float(p_orig[1] + d_step * dy_u),
                                )
                                evaluate_trial(
                                    "micro-nudge",
                                    f"Nudge vertex {m_vi + 1} on {mover_label} by {d_step:.3f}",
                                    moved_pids,
                                    1,
                                    d_step,
                                )

            if best_after_snap is not None and best_meta is not None and best_score is not None:
                _restore_entities_and_points(project, best_after_snap)
                method, desc, moved_pids, after_count, disp = best_meta
                n_moved = int(best_score[1])
                for pid in moved_pids:
                    moved_point_tracker.add(int(pid))
                if not moved_pids:
                    moved_vertex_tracker.add((target.entity_a_id, target.span_a))
                total_disp += disp
                accepted_step = AutoNudgeStep(
                    method=method,
                    description=desc,
                    entity_ids=eids,
                    point_ids=moved_pids,
                    crossings_before=cur_count,
                    crossings_after=after_count,
                    points_moved=n_moved,
                    displacement=disp,
                )
                steps.append(accepted_step)
                break
            else:
                _restore_entities_and_points(project, base_snap)

        if accepted_step is None:
            break

    if steps:
        project.touch()
    remaining = detect_breakline_crossings(project)
    total_moved = len(moved_point_tracker) + len(moved_vertex_tracker)
    return AutoNudgeResult(
        initial_crossings=initial_count,
        remaining_crossings=len(remaining),
        crossings_removed=max(0, initial_count - len(remaining)),
        points_moved=total_moved,
        total_displacement=total_disp,
        steps=steps,
        residue=remaining,
    )


# =============================================================================
# 12. Square-shape orthogonal solver (Phase 4 — §3.6 & §6)
# =============================================================================
SQUARE_SHAPE_MODES = ("average", "small", "large", "lock_points")


@dataclass
class SquareShapeResult:
    """Result of squaring a building / structure polyline to 90° corners."""

    entity_id: int
    mode: str
    orientation_deg: float             # dominant azimuth (degrees CCW from +X, mod 90)
    old_verts: np.ndarray
    new_verts: np.ndarray
    max_displacement: float
    locked_indices: list[int] = field(default_factory=list)
    updated_point_ids: list[int] = field(default_factory=list)


def square_shape(
    project: "Project",
    entity_id: int,
    *,
    mode: str = "average",
    locked_indices: Sequence[int] = (),
    propagate_to_points: bool = True,
) -> SquareShapeResult:
    """Orthogonalize a polyline/polygon so all corners snap to 90° angles (§3.6).

    Modes:
    - ``"average"``: hold mean dimensions along dominant orthogonal axes.
    - ``"small"``: hold smaller parallel edge dimensions.
    - ``"large"``: hold larger parallel edge dimensions.
    - ``"lock_points"`` (or ``"lock"``): keep anchor vertices in ``locked_indices``
      fixed and solve 90° corners around them.

    Point numbers and descriptions are never changed.
    """
    ent = project.entities.get(int(entity_id))
    if not isinstance(ent, Polyline) or len(ent.verts) < 3:
        raise ValueError("Square shape requires a polyline or polygon with at least 3 vertices.")

    norm_mode = str(mode or "average").strip().lower().replace("-", "_").replace(" ", "_")
    if norm_mode == "lock":
        norm_mode = "lock_points"
    if norm_mode not in SQUARE_SHAPE_MODES:
        raise ValueError(f"Unknown square_shape mode '{mode}'. Choose from: {', '.join(SQUARE_SHAPE_MODES)}.")

    old_verts = ent.verts.copy()
    n_raw = len(old_verts)
    has_dup_close = (
        n_raw >= 4
        and math.hypot(float(old_verts[-1, 0] - old_verts[0, 0]), float(old_verts[-1, 1] - old_verts[0, 1])) <= 1e-6
    )
    is_closed = bool(ent.closed or has_dup_close)
    m = n_raw - 1 if has_dup_close else n_raw
    if m < 3:
        raise ValueError("Square shape requires at least 3 distinct vertices.")

    xy = old_verts[:m, :2].astype(float).copy()
    n_edges = m if is_closed else m - 1

    # Step 1: Dominant orientation mod 90° (pi/2) via 4*theta circular mean weighted by edge length
    sin4 = 0.0
    cos4 = 0.0
    edges: list[tuple[int, int, float, float, float]] = []
    for k in range(n_edges):
        i = k
        j = (k + 1) % m
        dx = float(xy[j, 0] - xy[i, 0])
        dy = float(xy[j, 1] - xy[i, 1])
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            continue
        ang = math.atan2(dy, dx)
        sin4 += length * math.sin(4.0 * ang)
        cos4 += length * math.cos(4.0 * ang)
        edges.append((i, j, dx, dy, length))

    if not edges:
        raise ValueError("Polyline has no non-zero length edges to square.")

    theta0 = 0.25 * math.atan2(sin4, cos4)
    if theta0 < 0.0:
        theta0 += 0.5 * math.pi
    u_axis = np.array([math.cos(theta0), math.sin(theta0)], dtype=float)
    v_axis = np.array([-math.sin(theta0), math.cos(theta0)], dtype=float)

    locked_set = {int(idx) % m for idx in locked_indices if 0 <= int(idx) < n_raw}
    centroid = xy.mean(axis=0)
    rel = xy - centroid
    u_coords = rel @ u_axis
    v_coords = rel @ v_axis

    # Step 2: Classify edges onto u-axis (axis 0 -> v is constant) or v-axis (axis 1 -> u is constant)
    # and group vertices sharing constant-u or constant-v lines via union-find.
    parent_u = list(range(m))
    parent_v = list(range(m))

    def find_u(x: int) -> int:
        while parent_u[x] != x:
            parent_u[x] = parent_u[parent_u[x]]
            x = parent_u[x]
        return x

    def union_u(a: int, b: int) -> None:
        ra, rb = find_u(a), find_u(b)
        if ra != rb:
            parent_u[rb] = ra

    def find_v(x: int) -> int:
        while parent_v[x] != x:
            parent_v[x] = parent_v[parent_v[x]]
            x = parent_v[x]
        return x

    def union_v(a: int, b: int) -> None:
        ra, rb = find_v(a), find_v(b)
        if ra != rb:
            parent_v[rb] = ra

    u_edge_lengths: list[float] = []
    v_edge_lengths: list[float] = []
    for i, j, dx, dy, length in edges:
        du = dx * u_axis[0] + dy * u_axis[1]
        dv = dx * v_axis[0] + dy * v_axis[1]
        if abs(du) >= abs(dv):
            # Edge runs along u-axis -> vertices i and j share the same v coordinate
            union_v(i, j)
            u_edge_lengths.append(length)
        else:
            # Edge runs along v-axis -> vertices i and j share the same u coordinate
            union_u(i, j)
            v_edge_lengths.append(length)

    u_groups: dict[int, list[int]] = {}
    v_groups: dict[int, list[int]] = {}
    for vi in range(m):
        u_groups.setdefault(find_u(vi), []).append(vi)
        v_groups.setdefault(find_v(vi), []).append(vi)

    u_solved = np.zeros(m, dtype=float)
    v_solved = np.zeros(m, dtype=float)

    for members in u_groups.values():
        locked_members = [vi for vi in members if vi in locked_set]
        if norm_mode == "lock_points" and locked_members:
            val = float(np.mean([u_coords[vi] for vi in locked_members]))
        else:
            val = float(np.mean([u_coords[vi] for vi in members]))
        for vi in members:
            u_solved[vi] = val

    for members in v_groups.values():
        locked_members = [vi for vi in members if vi in locked_set]
        if norm_mode == "lock_points" and locked_members:
            val = float(np.mean([v_coords[vi] for vi in locked_members]))
        else:
            val = float(np.mean([v_coords[vi] for vi in members]))
        for vi in members:
            v_solved[vi] = val

    # Step 3: Adjust overall dimensions for average / small / large modes
    if norm_mode in ("average", "small", "large"):
        cur_u_span = float(np.ptp(u_solved))
        cur_v_span = float(np.ptp(v_solved))
        if u_edge_lengths and cur_u_span > 1e-9:
            if norm_mode == "small":
                target_u = float(min(u_edge_lengths))
            elif norm_mode == "large":
                target_u = float(max(u_edge_lengths))
            else:
                target_u = float(np.mean(u_edge_lengths)) if len(u_groups) == 2 else cur_u_span
            u_mid = 0.5 * (float(u_solved.min()) + float(u_solved.max()))
            u_solved = u_mid + (u_solved - u_mid) * (target_u / cur_u_span)

        if v_edge_lengths and cur_v_span > 1e-9:
            if norm_mode == "small":
                target_v = float(min(v_edge_lengths))
            elif norm_mode == "large":
                target_v = float(max(v_edge_lengths))
            else:
                target_v = float(np.mean(v_edge_lengths)) if len(v_groups) == 2 else cur_v_span
            v_mid = 0.5 * (float(v_solved.min()) + float(v_solved.max()))
            v_solved = v_mid + (v_solved - v_mid) * (target_v / cur_v_span)

    if norm_mode == "lock_points" and locked_set:
        for vi in locked_set:
            u_solved[vi] = u_coords[vi]
            v_solved[vi] = v_coords[vi]

    new_xy = centroid + np.outer(u_solved, u_axis) + np.outer(v_solved, v_axis)
    new_verts = old_verts.copy()
    new_verts[:m, :2] = new_xy
    if has_dup_close:
        new_verts[-1, :2] = new_xy[0]

    disps = np.hypot(new_verts[:m, 0] - old_verts[:m, 0], new_verts[:m, 1] - old_verts[:m, 1])
    max_disp = float(disps.max()) if len(disps) else 0.0

    ent.verts = new_verts.copy()
    if ent.bulges is not None:
        ent.bulges = None

    pids = _ensure_point_ids_attr(project, ent)
    updated_pids: list[int] = []
    if propagate_to_points:
        for vi in range(n_raw):
            if (vi % m) in locked_set and norm_mode == "lock_points":
                continue
            pid = pids[vi] if vi < len(pids) else None
            if pid is not None and pid in project.points:
                pt = project.points[pid]
                pt.x = float(new_verts[vi, 0])
                pt.y = float(new_verts[vi, 1])
                _sync_point_to_polylines(project, pid)
                if int(pid) not in updated_pids:
                    updated_pids.append(int(pid))

    if not updated_pids:
        _mark_manual_override_if_derived(project, ent, pids)
    project.touch()

    return SquareShapeResult(
        entity_id=int(entity_id),
        mode=norm_mode,
        orientation_deg=float(math.degrees(theta0)),
        old_verts=old_verts,
        new_verts=new_verts,
        max_displacement=max_disp,
        locked_indices=sorted(locked_set),
        updated_point_ids=updated_pids,
    )

