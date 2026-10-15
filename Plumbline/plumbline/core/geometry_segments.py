"""Qt-free geometry segment inventory.

A segment is a displayed line occurrence, not a permanent field-code name.  Field
crews can close and later reopen ``SW1``; generated linework consequently contains
separate polylines which this module numbers ``SW1-01``, ``SW1-02`` for display.
The inventory deliberately reads geometry already in the project, so imported
polylines and point-driven linework appear in the same panel.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

import numpy as np

from .model import Polyline

if TYPE_CHECKING:  # pragma: no cover - imports only for type checkers
    from .project import Project


@dataclass(frozen=True, slots=True)
class Segment:
    """A uniform, display-only view of one project polyline."""

    seg_id: str
    entity_id: int
    source: str
    code: str
    string: str
    occurrence: int
    point_ids: tuple[int | None, ...]
    point_numbers: tuple[str, ...]
    closed: bool
    layer: str
    kind: str
    length_2d: float
    length_3d: float

    @property
    def linked(self) -> bool:
        return bool(self.point_ids) and all(point_id is not None for point_id in self.point_ids)


def _number_key(value: str) -> tuple[int, float | str]:
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value).casefold())


def _lengths(vertices: np.ndarray, closed: bool) -> tuple[float, float]:
    if len(vertices) < 2:
        return 0.0, 0.0
    points = np.asarray(vertices, dtype=float)
    if closed:
        points = np.vstack((points, points[0]))
    delta = np.diff(points, axis=0)
    length_2d = float(np.hypot(delta[:, 0], delta[:, 1]).sum())
    dz = np.where(np.isfinite(delta[:, 2]), delta[:, 2], 0.0)
    length_3d = float(np.sqrt(delta[:, 0] ** 2 + delta[:, 1] ** 2 + dz ** 2).sum())
    return length_2d, length_3d


def _coerce_point_ids(entity: Polyline) -> tuple[int | None, ...]:
    raw = (entity.attrs or {}).get("point_ids", [])
    if not isinstance(raw, (list, tuple)) or len(raw) != len(entity.verts):
        return tuple(None for _ in entity.verts)
    result = []
    for value in raw:
        try:
            result.append(int(value) if value is not None else None)
        except (TypeError, ValueError):
            result.append(None)
    return tuple(result)


def build_segments(project: Project) -> list[Segment]:
    """Return all project polylines as numbered, display-only geometry segments.

    Generated linework is ordered by its first linked point number, matching the
    field-book reading direction.  Other geometry remains deterministic by entity
    id.  No project state is changed.
    """
    rows = []
    for entity in project.entities.values():
        if not isinstance(entity, Polyline) or len(entity.verts) < 1:
            continue
        attrs = entity.attrs or {}
        code = str(attrs.get("code") or entity.kind or "LINE").strip().upper()
        string = str(attrs.get("string") or "").strip()
        point_ids = _coerce_point_ids(entity)
        numbers = tuple(str(project.points[p].number) if p in project.points else ""
                        for p in point_ids)
        first_number = next((number for number in numbers if number), "")
        # Field-created linework sorts in shot order; imported entities keep stable ID order.
        source_order = _number_key(first_number) if entity.derived == "linework" else (2, entity.id)
        rows.append((code.casefold(), string.casefold(), source_order, entity.id,
                     entity, code, string, point_ids, numbers))
    rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))

    occurrences: dict[tuple[str, str], int] = {}
    result = []
    for _code_key, _string_key, _sort, entity_id, entity, code, string, point_ids, numbers in rows:
        key = (code.casefold(), string.casefold())
        occurrence = occurrences.get(key, 0) + 1
        occurrences[key] = occurrence
        label = f"{code}{string}" if string else code
        length_2d, length_3d = _lengths(entity.verts, entity.closed)
        result.append(Segment(
            seg_id=f"{label}-{occurrence:02d}", entity_id=entity_id,
            source="Point linework" if entity.derived == "linework" else "Geometry",
            code=code, string=string, occurrence=occurrence, point_ids=point_ids,
            point_numbers=numbers, closed=bool(entity.closed), layer=entity.layer,
            kind=entity.kind, length_2d=length_2d, length_3d=length_3d,
        ))
    return result


def segment_point_rows(project: Project, segment: Segment) -> list[dict]:
    """Rows for a segment's point list, including the distance to the next vertex."""
    entity = project.entities.get(segment.entity_id)
    if not isinstance(entity, Polyline):
        return []
    rows = []
    for index, vertex in enumerate(entity.verts):
        point_id = segment.point_ids[index] if index < len(segment.point_ids) else None
        point = project.points.get(point_id) if point_id is not None else None
        if index + 1 < len(entity.verts):
            next_vertex = entity.verts[index + 1]
            to_next = float(math.hypot(next_vertex[0] - vertex[0], next_vertex[1] - vertex[1]))
        elif entity.closed and len(entity.verts) > 1:
            next_vertex = entity.verts[0]
            to_next = float(math.hypot(next_vertex[0] - vertex[0], next_vertex[1] - vertex[1]))
        else:
            to_next = None
        rows.append({
            "index": index, "point_id": point_id, "number": point.number if point else "—",
            "desc": point.desc if point else "", "x": float(point.x if point else vertex[0]),
            "y": float(point.y if point else vertex[1]), "z": float(point.z if point else vertex[2]),
            "to_next": to_next, "linked": point is not None,
        })
    return rows
