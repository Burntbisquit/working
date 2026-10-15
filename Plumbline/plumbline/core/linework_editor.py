"""Transactional line-geometry editing and multi-line join drafts.

The editor deliberately works on copies.  A preview/action is cheap to undo locally, and only an
explicit Apply/Accept writes to the project through one AppState transaction.  Point references
are resolved conservatively: an ambiguous duplicate point number never silently selects a point.
"""
from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from . import point_linework_coder as PLC
from .fieldbook_syntax import command_map
from .model import NAN, Polyline, SurveyPoint


class LineworkEditError(ValueError):
    """An unsafe or invalid staged linework operation."""


class LineworkEditConflict(LineworkEditError):
    """The project object changed after its draft was opened."""


def _finite_z(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return NAN
    return value if math.isfinite(value) else NAN


def _xyz(value) -> np.ndarray:
    arr = np.asarray(value, dtype=float).reshape(-1)
    if len(arr) == 2:
        arr = np.r_[arr, NAN]
    if len(arr) < 3:
        raise LineworkEditError("A line vertex requires easting and northing coordinates.")
    return arr[:3]


def bulge_point(start, end, bulge: float, fraction: float) -> np.ndarray:
    """Return a point at *fraction* of a DXF bulge segment, including interpolated Z."""
    a, b = _xyz(start), _xyz(end)
    f = float(fraction)
    if not 0.0 < f < 1.0:
        raise LineworkEditError("The insert position must be strictly inside the segment.")
    if abs(float(bulge)) <= 1e-14:
        xy = a[:2] + (b[:2] - a[:2]) * f
    else:
        theta = 4.0 * math.atan(float(bulge))
        chord = float(np.linalg.norm(b[:2] - a[:2]))
        if chord <= 1e-14:
            raise LineworkEditError("A curved segment cannot have coincident endpoints.")
        half_tangent = math.tan(theta / 2.0)
        if abs(half_tangent) <= 1e-14:
            raise LineworkEditError("The curve is too close to a full circle to split safely.")
        direction = (b[:2] - a[:2]) / chord
        left_normal = np.array([-direction[1], direction[0]])
        center = (a[:2] + b[:2]) / 2.0 + left_normal * (chord / (2.0 * half_tangent))
        radius = float(np.linalg.norm(a[:2] - center))
        start_angle = math.atan2(a[1] - center[1], a[0] - center[0])
        angle = start_angle + theta * f
        xy = center + radius * np.array([math.cos(angle), math.sin(angle)])
    if math.isfinite(a[2]) and math.isfinite(b[2]):
        z = a[2] + (b[2] - a[2]) * f
    elif math.isfinite(a[2]):
        z = a[2]
    elif math.isfinite(b[2]):
        z = b[2]
    else:
        z = NAN
    return np.array([xy[0], xy[1], z], dtype=float)


def split_bulge(bulge: float, fraction: float) -> tuple[float, float]:
    """Split one DXF bulge into two bulges that preserve the same circular arc."""
    f = float(fraction)
    if not 0.0 < f < 1.0:
        raise LineworkEditError("The insert position must be strictly inside the segment.")
    theta = 4.0 * math.atan(float(bulge))
    return math.tan(theta * f / 4.0), math.tan(theta * (1.0 - f) / 4.0)


def bulge_points(start, end, bulge: float, min_steps: int = 8) -> np.ndarray:
    """A polyline approximation suitable for an on-screen preview of a bulge segment."""
    a, b = _xyz(start), _xyz(end)
    if abs(float(bulge)) <= 1e-14:
        return np.vstack([a, b])
    theta = 4.0 * math.atan(float(bulge))
    steps = max(int(min_steps), int(math.ceil(abs(theta) / (math.pi / 24.0))))
    points = [a]
    for i in range(1, steps):
        points.append(bulge_point(a, b, bulge, i / steps))
    points.append(b)
    return np.asarray(points, dtype=float)


def _nearby_point_id(project, xy, candidates: Iterable[int], used: set[int], tolerance: float):
    tol2 = float(tolerance) ** 2
    ranked = []
    for pid in candidates:
        point = project.points.get(pid)
        if point is None or pid in used:
            continue
        distance2 = (point.x - float(xy[0])) ** 2 + (point.y - float(xy[1])) ** 2
        if distance2 <= tol2:
            ranked.append((distance2, pid))
    ranked.sort()
    if not ranked:
        return None, False
    if len(ranked) > 1:
        # Equal/near-equal candidates are ambiguous even when their point numbers differ.
        margin = max(1e-16, tol2 * 1e-8)
        if ranked[1][0] - ranked[0][0] <= margin:
            return None, True
    return ranked[0][1], False


def resolve_vertex_point_ids(project, polyline: Polyline, tolerance: float = 0.005) -> tuple[list[int | None], list[str]]:
    """Resolve each polyline vertex to one point ID, reporting ambiguity instead of guessing."""
    count = len(polyline.verts)
    attrs = polyline.attrs or {}
    explicit = attrs.get("point_ids")
    legacy_numbers = attrs.get("points")
    if not isinstance(legacy_numbers, (tuple, list)):
        legacy_numbers = []
    linked: list[int | None] = [None] * count
    issues: list[str] = []
    used: set[int] = set()

    explicit_errors = {}
    explicit_unlinked: set[int] = set()
    has_explicit_attribute = "point_ids" in attrs
    has_explicit_links = isinstance(explicit, (tuple, list)) and len(explicit) == count
    if has_explicit_attribute and not has_explicit_links:
        issues.append("The polyline's explicit point-ID list is malformed or does not match its vertices.")
    if has_explicit_links:
        for i, raw_id in enumerate(explicit):
            if raw_id is None:
                explicit_unlinked.add(i)
                explicit_errors[i] = f"Vertex {i + 1} is explicitly unlinked from a survey point."
                continue
            try:
                pid = int(raw_id)
            except (TypeError, ValueError):
                pid = None
            if pid is None:
                explicit_errors[i] = f"Vertex {i + 1} has an invalid explicit point ID."
            elif pid in project.points and pid not in used:
                linked[i] = pid
                used.add(pid)
            else:
                explicit_errors[i] = f"Vertex {i + 1} has a missing or repeated point ID {pid}."

    all_ids = list(project.points)
    for i, vertex in enumerate(polyline.verts):
        if linked[i] is not None:
            continue
        if has_explicit_attribute:
            if i in explicit_errors:
                issues.append(explicit_errors[i])
            continue
        if i in explicit_unlinked:
            issues.append(explicit_errors[i])
            continue
        candidates = all_ids
        if len(legacy_numbers) == count and legacy_numbers[i] not in (None, ""):
            number = str(legacy_numbers[i])
            candidates = [pid for pid, point in project.points.items() if str(point.number) == number]
        pid, ambiguous = _nearby_point_id(project, vertex[:2], candidates, used, tolerance)
        if pid is None and candidates is not all_ids:
            # Legacy number attributes are sometimes stale after renumbering; coordinates may
            # still provide a unique match, but only within the same conservative tolerance.
            pid, ambiguous = _nearby_point_id(project, vertex[:2], all_ids, used, tolerance)
        if pid is not None:
            linked[i] = pid
            used.add(pid)
        elif ambiguous:
            issues.append(explicit_errors.get(i, f"Vertex {i + 1} is equally close to multiple survey points."))
        else:
            issues.append(explicit_errors.get(
                i, f"Vertex {i + 1} has no linked survey point within {tolerance:g} units."))
    return linked, list(dict.fromkeys(issues))


def _entity_signature(entity: Polyline) -> dict:
    return {
        "id": entity.id,
        "layer": entity.layer,
        "closed": bool(entity.closed),
        "kind": entity.kind,
        "derived": entity.derived,
        "attrs": copy.deepcopy(entity.attrs or {}),
        "verts": np.array(entity.verts, dtype=float, copy=True),
        "bulges": None if entity.bulges is None else np.array(entity.bulges, dtype=float, copy=True),
    }


def _point_signature(point: SurveyPoint) -> dict:
    return {
        "id": point.id, "number": str(point.number), "x": float(point.x), "y": float(point.y),
        "z": float(point.z), "desc": str(point.desc or ""), "layer": str(point.layer),
        "attrs": copy.deepcopy(point.attrs or {}),
    }


def _values_equal(first, second) -> bool:
    if isinstance(first, np.ndarray) or isinstance(second, np.ndarray):
        try:
            return np.array_equal(np.asarray(first), np.asarray(second), equal_nan=True)
        except (TypeError, ValueError):
            return False
    if isinstance(first, dict) and isinstance(second, dict):
        return (first.keys() == second.keys() and
                all(_values_equal(first[key], second[key]) for key in first))
    if isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
        return (len(first) == len(second) and
                all(_values_equal(a, b) for a, b in zip(first, second)))
    if isinstance(first, (float, np.floating)) and isinstance(second, (float, np.floating)):
        return (math.isnan(float(first)) and math.isnan(float(second))) or float(first) == float(second)
    try:
        value = first == second
        return bool(value)
    except (TypeError, ValueError):
        return False


def _same_entity(entity: Polyline | None, signature: dict) -> bool:
    if entity is None or entity.id != signature["id"]:
        return False
    if (entity.layer != signature["layer"] or bool(entity.closed) != signature["closed"] or
            entity.kind != signature["kind"] or entity.derived != signature["derived"] or
            not _values_equal(entity.attrs or {}, signature["attrs"])):
        return False
    if entity.verts.shape != signature["verts"].shape or not np.allclose(
            entity.verts, signature["verts"], equal_nan=True):
        return False
    if entity.bulges is None or signature["bulges"] is None:
        return entity.bulges is None and signature["bulges"] is None
    return entity.bulges.shape == signature["bulges"].shape and np.allclose(
        entity.bulges, signature["bulges"], equal_nan=True)


def _same_point(point: SurveyPoint | None, signature: dict) -> bool:
    if point is None:
        return False
    current = _point_signature(point)
    for key in ("id", "number", "desc", "layer"):
        if current[key] != signature[key]:
            return False
    if not _values_equal(current["attrs"], signature["attrs"]):
        return False
    for key in ("x", "y", "z"):
        a, b = current[key], signature[key]
        if math.isnan(a) and math.isnan(b):
            continue
        if a != b:
            return False
    return True


@dataclass
class DraftVertex:
    x: float
    y: float
    z: float = NAN
    point_id: int | None = None
    number: str = ""
    description: str = ""
    layer: str = "POINTS"
    temp_id: str = ""
    source_vertex: bool = False
    is_new_point: bool = False

    @classmethod
    def from_point_or_vertex(cls, xyz, point: SurveyPoint | None = None):
        xyz = _xyz(xyz)
        return cls(
            float(xyz[0]), float(xyz[1]), float(xyz[2]),
            point.id if point is not None else None,
            str(point.number) if point is not None else "",
            str(point.desc or "") if point is not None else "",
            str(point.layer) if point is not None else "POINTS",
            f"point:{point.id}" if point is not None else "",
            source_vertex=True,
            is_new_point=False,
        )

    def xyz(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z], dtype=float)


class LineEditDraft:
    """An isolated editable copy of one polyline plus an operation-level undo stack."""

    def __init__(self, project, polyline: Polyline, tolerance: float = 0.005):
        if not isinstance(polyline, Polyline):
            raise LineworkEditError("Select a polyline to edit.")
        self.entity_id = int(polyline.id)
        self.base_entity = _entity_signature(polyline)
        self.attrs = copy.deepcopy(polyline.attrs or {})
        self.layer = str(polyline.layer)
        self.kind = str(polyline.kind)
        self.color = copy.deepcopy(polyline.color)
        self.linetype = polyline.linetype
        self.derived = str(polyline.derived or "")
        self.closed = bool(polyline.closed)
        self.vertices: list[DraftVertex] = []
        self.tolerance = float(tolerance)
        self.linked_point_ids, self.link_issues = resolve_vertex_point_ids(project, polyline, tolerance)
        self.base_points = {}
        for pid in self.linked_point_ids:
            if pid is not None and pid not in self.base_points:
                self.base_points[pid] = _point_signature(project.points[pid])
        for index, xyz in enumerate(polyline.verts):
            pid = self.linked_point_ids[index]
            point = project.points.get(pid) if pid is not None else None
            self.vertices.append(DraftVertex.from_point_or_vertex(xyz, point))
        bulges = np.zeros(len(self.vertices), dtype=float) if polyline.bulges is None else np.asarray(polyline.bulges, float)
        if len(bulges) < len(self.vertices):
            bulges = np.pad(bulges, (0, len(self.vertices) - len(bulges)))
        self.bulges = list(map(float, bulges[:len(self.vertices)]))
        self.removed_vertices: list[DraftVertex] = []
        self.actions: list[str] = []
        self._undo: list[tuple[str, dict]] = []
        self.mode = "graphics"
        self._used_numbers = {str(p.number).casefold() for p in project.points.values()}
        self._next_numeric = max(
            [int(p.number) for p in project.points.values() if str(p.number).isdigit()] + [0]) + 1
        self.project = project

    @property
    def point_mode_ready(self) -> bool:
        return bool(self.vertices) and all(pid is not None for pid in self.linked_point_ids) and not self.link_issues

    @property
    def segment_count(self) -> int:
        return len(self.vertices) if self.closed else max(0, len(self.vertices) - 1)

    @property
    def has_pending_actions(self) -> bool:
        return bool(self._undo)

    def set_mode(self, mode: str):
        mode = str(mode).casefold()
        if mode not in ("graphics", "points"):
            raise LineworkEditError("Choose graphics-only or point-edit mode.")
        if mode == "points" and not self.point_mode_ready:
            raise LineworkEditError("Point-edit mode requires an unambiguous survey-point link for every original vertex.")
        self.mode = mode
        if mode == "points":
            for vertex in self.vertices:
                if vertex.source_vertex or vertex.point_id is not None:
                    continue
                self._prepare_new_point(vertex)

    def _allocate_number(self) -> str:
        while str(self._next_numeric).casefold() in self._used_numbers:
            self._next_numeric += 1
        number = str(self._next_numeric)
        self._used_numbers.add(number.casefold())
        self._next_numeric += 1
        return number

    def _line_code_prefix(self) -> str:
        code = str(self.attrs.get("code", "") or "").strip()
        string = str(self.attrs.get("string", "") or "").strip()
        if code:
            if string:
                return f"{code}{string}"
            if self.vertices:
                commands = self.project.settings.get("f2f_commands")
                feature, parsed_string = self.project.codes.resolve_parts(
                    self.vertices[0].description, commands=commands)
                if feature is not None and feature.code.casefold() == code.casefold():
                    return f"{code}{parsed_string}"
            return code
        if self.vertices:
            parsed = PLC._split_multicode(self.vertices[0].description, self.project.settings.get("f2f_commands"))
            main = PLC._description_parts(parsed[0], self.project.settings.get("f2f_commands"))[0] if parsed else ""
            tokens = main.split()
            return tokens[0] if tokens else ""
        return ""

    def _prepare_new_point(self, vertex: DraftVertex):
        if not vertex.number:
            vertex.number = self._allocate_number()
        else:
            self._used_numbers.add(vertex.number.casefold())
        if not vertex.temp_id:
            vertex.temp_id = uuid.uuid4().hex
        vertex.is_new_point = True
        prefix = self._line_code_prefix()
        if prefix and not vertex.description:
            vertex.description = prefix
        code = str(self.attrs.get("code", "") or "").strip()
        fc = self.project.codes.get(code) if code else None
        if fc is not None and fc.layer:
            vertex.layer = fc.layer
        elif self.vertices:
            vertex.layer = self.vertices[0].layer or "POINTS"

    def _snapshot(self) -> dict:
        return {
            "vertices": copy.deepcopy(self.vertices),
            "bulges": list(self.bulges),
            "closed": self.closed,
            "removed_vertices": copy.deepcopy(self.removed_vertices),
            "attrs": copy.deepcopy(self.attrs),
        }

    def _restore(self, snapshot: dict):
        self.vertices = copy.deepcopy(snapshot["vertices"])
        self.bulges = list(snapshot["bulges"])
        self.closed = bool(snapshot["closed"])
        self.removed_vertices = copy.deepcopy(snapshot["removed_vertices"])
        self.attrs = copy.deepcopy(snapshot["attrs"])

    def _mutate(self, label: str, function):
        before = self._snapshot()
        try:
            function()
            self._validate()
        except Exception:
            self._restore(before)
            raise
        self._undo.append((str(label), before))
        self.actions.append(str(label))
        return label

    def _validate(self):
        minimum = 3 if self.closed else 2
        if len(self.vertices) < minimum:
            raise LineworkEditError(f"A {'closed' if self.closed else 'open'} line needs at least {minimum} vertices.")
        if len(self.bulges) != len(self.vertices):
            raise LineworkEditError("The vertex and curve-control lists are inconsistent.")
        if any(not math.isfinite(v.x) or not math.isfinite(v.y) for v in self.vertices):
            raise LineworkEditError("Every vertex needs finite easting and northing coordinates.")

    def insert_vertex(self, segment_index: int, fraction: float = 0.5) -> DraftVertex:
        result: list[DraftVertex] = []

        def operation():
            index = int(segment_index)
            if index < 0 or index >= self.segment_count:
                raise LineworkEditError("Select a valid segment before inserting a vertex.")
            next_index = (index + 1) % len(self.vertices)
            first, second = self.vertices[index], self.vertices[next_index]
            left_bulge, right_bulge = split_bulge(self.bulges[index], fraction)
            xyz = bulge_point(first.xyz(), second.xyz(), self.bulges[index], fraction)
            new_vertex = DraftVertex(float(xyz[0]), float(xyz[1]), float(xyz[2]), source_vertex=False)
            if self.mode == "points":
                self._prepare_new_point(new_vertex)
            if self.closed and index == len(self.vertices) - 1:
                self.vertices.append(new_vertex)
                self.bulges[index] = left_bulge
                self.bulges.append(right_bulge)
            else:
                self.vertices.insert(index + 1, new_vertex)
                self.bulges[index] = left_bulge
                self.bulges.insert(index + 1, right_bulge)
            result.append(new_vertex)

        self._mutate("Insert vertex", operation)
        return result[0]

    def insert_end(self, xyz, at_start: bool = False) -> DraftVertex:
        xyz = _xyz(xyz)
        result: list[DraftVertex] = []

        def operation():
            if self.closed:
                raise LineworkEditError("Open the line before inserting a new endpoint.")
            vertex = DraftVertex(float(xyz[0]), float(xyz[1]), float(xyz[2]), source_vertex=False)
            if self.mode == "points":
                self._prepare_new_point(vertex)
            if at_start:
                self.vertices.insert(0, vertex)
                self.bulges.insert(0, 0.0)
            else:
                self.vertices.append(vertex)
                self.bulges[-1] = 0.0
                self.bulges.append(0.0)
            result.append(vertex)

        self._mutate("Insert start vertex" if at_start else "Insert end vertex", operation)
        return result[0]

    def delete_vertex(self, index: int):
        index = int(index)

        def operation():
            n = len(self.vertices)
            minimum = 3 if self.closed else 2
            if n <= minimum:
                raise LineworkEditError(f"A {'closed' if self.closed else 'open'} line must retain at least {minimum} vertices.")
            if index < 0 or index >= n:
                raise LineworkEditError("Select a valid vertex to delete.")
            old_segments = list(self.bulges[:n] if self.closed else self.bulges[:n - 1])
            if self.closed:
                incoming, outgoing = old_segments[(index - 1) % n], old_segments[index]
                if abs(incoming) > 1e-12 or abs(outgoing) > 1e-12:
                    raise LineworkEditError("Straighten the adjacent curve segment(s) before deleting this vertex.")
                old_indices = [i for i in range(n) if i != index]
                new_segments = []
                for j, old_from in enumerate(old_indices):
                    old_to = old_indices[(j + 1) % len(old_indices)]
                    new_segments.append(old_segments[old_from] if (old_to - old_from) % n == 1 else 0.0)
            else:
                if index == 0:
                    if abs(old_segments[0]) > 1e-12:
                        raise LineworkEditError("Straighten the adjacent curve segment before deleting this endpoint.")
                    new_segments = old_segments[1:]
                elif index == n - 1:
                    if abs(old_segments[-1]) > 1e-12:
                        raise LineworkEditError("Straighten the adjacent curve segment before deleting this endpoint.")
                    new_segments = old_segments[:-1]
                else:
                    if abs(old_segments[index - 1]) > 1e-12 or abs(old_segments[index]) > 1e-12:
                        raise LineworkEditError("Straighten the adjacent curve segment(s) before deleting this vertex.")
                    new_segments = old_segments[:index - 1] + [0.0] + old_segments[index + 1:]
            removed = self.vertices.pop(index)
            if removed.point_id is not None or removed.is_new_point:
                self.removed_vertices.append(removed)
            self.bulges = list(new_segments) if self.closed else list(new_segments) + [0.0]

        self._mutate("Delete vertex", operation)

    def move_vertex(self, index: int, x: float, y: float, z: float | None = None):
        index = int(index)

        def operation():
            if index < 0 or index >= len(self.vertices):
                raise LineworkEditError("Select a valid vertex to move.")
            x_value, y_value = float(x), float(y)
            if not math.isfinite(x_value) or not math.isfinite(y_value):
                raise LineworkEditError("Easting and northing must be finite numbers.")
            vertex = self.vertices[index]
            vertex.x, vertex.y = x_value, y_value
            if z is not None:
                vertex.z = _finite_z(z)

        self._mutate("Move vertex", operation)

    def interpolate_vertex(self, index: int, fraction: float = 0.5):
        """Place an existing interior vertex between its two neighbors, interpolating X/Y/Z."""
        index = int(index)
        t = float(fraction)

        def operation():
            if index < 0 or index >= len(self.vertices):
                raise LineworkEditError("Select a valid vertex to interpolate.")
            if not math.isfinite(t) or not 0.0 <= t <= 1.0:
                raise LineworkEditError("Interpolation fraction must be between 0 and 1.")
            if not self.closed and index in (0, len(self.vertices) - 1):
                raise LineworkEditError("Interpolate an interior point; an open-line endpoint has only one neighbor.")
            previous = self.vertices[(index - 1) % len(self.vertices)]
            following = self.vertices[(index + 1) % len(self.vertices)]
            vertex = self.vertices[index]
            vertex.x = previous.x + (following.x - previous.x) * t
            vertex.y = previous.y + (following.y - previous.y) * t
            if math.isfinite(previous.z) and math.isfinite(following.z):
                vertex.z = previous.z + (following.z - previous.z) * t
            elif math.isfinite(previous.z):
                vertex.z = previous.z
            elif math.isfinite(following.z):
                vertex.z = following.z
            else:
                vertex.z = NAN

        self._mutate("Interpolate vertex position", operation)

    def nudge_vertex(self, index: int, dx: float, dy: float, dz: float = 0.0):
        """Move a vertex by a small finite offset; point-mode commit follows its linked point."""
        index = int(index)
        offsets = tuple(float(value) for value in (dx, dy, dz))

        def operation():
            if index < 0 or index >= len(self.vertices):
                raise LineworkEditError("Select a valid vertex to nudge.")
            if not all(math.isfinite(value) for value in offsets):
                raise LineworkEditError("Nudge offsets must be finite numbers.")
            vertex = self.vertices[index]
            vertex.x += offsets[0]
            vertex.y += offsets[1]
            if math.isfinite(vertex.z):
                vertex.z += offsets[2]

        self._mutate("Nudge point", operation)

    def swap_vertices(self, first_index: int, second_index: int):
        first, second = int(first_index), int(second_index)

        def operation():
            n = len(self.vertices)
            if first < 0 or first >= n or second < 0 or second >= n or abs(first - second) != 1:
                raise LineworkEditError("Swap a vertex only with its immediately adjacent vertex.")
            if any(abs(value) > 1e-12 for value in self.bulges):
                raise LineworkEditError("Straighten curve controls before swapping vertices.")
            self.vertices[first], self.vertices[second] = self.vertices[second], self.vertices[first]
            self.bulges = [0.0] * n

        self._mutate("Swap adjacent vertices", operation)

    def reverse(self):
        def operation():
            n = len(self.vertices)
            old = list(self.bulges)
            self.vertices.reverse()
            if self.closed:
                self.bulges = [-old[(n - 2 - i) % n] for i in range(n)]
            else:
                segments = old[:n - 1]
                self.bulges = [-segments[n - 2 - i] for i in range(n - 1)] + [0.0]

        self._mutate("Reverse line direction", operation)

    def set_curve(self, segment_index: int, sweep_degrees: float):
        index = int(segment_index)
        sweep = float(sweep_degrees)

        def operation():
            if index < 0 or index >= self.segment_count:
                raise LineworkEditError("Select a valid segment to set a curve.")
            if not math.isfinite(sweep) or abs(sweep) >= 359.0:
                raise LineworkEditError("Curve sweep must be finite and between -359 and 359 degrees.")
            self.bulges[index] = math.tan(math.radians(sweep) / 4.0) if abs(sweep) >= 0.01 else 0.0

        self._mutate("Set curve control", operation)

    def straighten(self, segment_index: int):
        self.set_curve(segment_index, 0.0)
        self.actions[-1] = "Straighten segment"
        self._undo[-1] = ("Straighten segment", self._undo[-1][1])

    def toggle_closed(self, closed: bool | None = None):
        requested = not self.closed if closed is None else bool(closed)

        def operation():
            if requested == self.closed:
                raise LineworkEditError("The line already has that open/closed state.")
            if requested and len(self.vertices) < 3:
                raise LineworkEditError("At least three vertices are required to close a line.")
            if self.closed and abs(self.bulges[-1]) > 1e-12:
                raise LineworkEditError("Straighten the closing curve before opening the line.")
            self.closed = requested
            if not requested:
                self.bulges[-1] = 0.0

        self._mutate("Close line" if requested else "Open line", operation)

    def undo_last(self) -> str | None:
        if not self._undo:
            return None
        label, snapshot = self._undo.pop()
        self._restore(snapshot)
        if self.actions:
            self.actions.pop()
        return label

    def reset(self):
        entity = self.project.entities.get(self.entity_id)
        if not isinstance(entity, Polyline):
            raise LineworkEditConflict("The original line no longer exists.")
        fresh = LineEditDraft(self.project, entity, self.tolerance)
        self.__dict__.update(fresh.__dict__)

    def assert_project_unchanged(self, project):
        entity = project.entities.get(self.entity_id)
        if not _same_entity(entity if isinstance(entity, Polyline) else None, self.base_entity):
            raise LineworkEditConflict("This line changed after the editor opened. Reopen it before applying.")
        for pid, signature in self.base_points.items():
            if not _same_point(project.points.get(pid), signature):
                raise LineworkEditConflict(f"Survey point ID {pid} changed after the editor opened. Reopen the line before applying.")


def linework_override_keys(attrs: dict | None) -> list[tuple[str, str]]:
    attrs = attrs or {}
    keys: list[tuple[str, str]] = []
    for raw in attrs.get("linework_override_keys", []) or []:
        if isinstance(raw, (tuple, list)) and len(raw) >= 2:
            key = (str(raw[0]).strip(), str(raw[1]).strip())
            if key[0]:
                keys.append(key)
    code = str(attrs.get("code", "") or "").strip()
    if code:
        key = (code, str(attrs.get("string", "") or "").strip())
        if key not in keys:
            keys.append(key)
    unique = {}
    for code, string in keys:
        unique[(code.casefold(), string.casefold())] = (code, string)
    return list(unique.values())


def _coerce_point_id_set(values) -> set[int]:
    result = set()
    if not isinstance(values, (list, tuple, set)):
        return result
    for value in values:
        if value is None:
            continue
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _coerce_source_sequences(values) -> set[tuple[int, ...]]:
    result = set()
    if not isinstance(values, (list, tuple)):
        return result
    for value in values:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        try:
            sequence = tuple(int(point_id) for point_id in value)
        except (TypeError, ValueError):
            continue
        result.add(sequence)
    return result


def _override_scope_map(attrs: dict | None) -> dict[tuple[str, str], dict]:
    """Read exact source-sequence scopes, with compatibility for older set-only overrides."""
    attrs = attrs or {}
    result = {}
    raw_scopes = attrs.get("linework_override_scopes", [])
    if isinstance(raw_scopes, (list, tuple)):
        for raw in raw_scopes:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code", "") or "").strip()
            string = str(raw.get("string", "") or "").strip()
            if not code:
                continue
            key = (code.casefold(), string.casefold())
            entry = result.setdefault(key, {
                "code": code, "string": string, "point_ids": set(), "source_sequences": set()})
            entry["point_ids"].update(_coerce_point_id_set(raw.get("point_ids", [])))
            entry["source_sequences"].update(_coerce_source_sequences(raw.get("source_sequences", [])))
    if result:
        return result

    point_ids = _coerce_point_id_set(attrs.get("linework_override_point_ids", []))
    sequences = _coerce_source_sequences(attrs.get("linework_override_source_sequences", []))
    for code, string in linework_override_keys(attrs):
        result[(code.casefold(), string.casefold())] = {
            "code": code, "string": string, "point_ids": set(point_ids),
            "source_sequences": set(sequences)}
    return result


def _serialize_override_scope_map(scopes: dict[tuple[str, str], dict]) -> list[dict]:
    return [
        {"code": entry["code"], "string": entry["string"],
         "point_ids": sorted(entry["point_ids"]),
         "source_sequences": [list(sequence) for sequence in sorted(entry["source_sequences"])]}
        for _key, entry in sorted(scopes.items())
    ]


def _merge_override_scope(scopes: dict[tuple[str, str], dict], key: tuple[str, str],
                          point_ids=(), source_sequences=()):
    code, string = str(key[0]).strip(), str(key[1]).strip()
    if not code:
        return
    normalized = (code.casefold(), string.casefold())
    entry = scopes.setdefault(normalized, {
        "code": code, "string": string, "point_ids": set(), "source_sequences": set()})
    entry["point_ids"].update(_coerce_point_id_set(list(point_ids)))
    entry["source_sequences"].update(_coerce_source_sequences(list(source_sequences)))


def _inferred_linework_keys(project, attrs: dict | None, point_ids: Iterable[int | None]) -> list[tuple[str, str]]:
    keys = linework_override_keys(attrs)
    if keys:
        return keys
    commands = (getattr(project, "settings", {}) or {}).get("f2f_commands")
    found = {}
    for pid in point_ids:
        point = project.points.get(pid) if pid is not None else None
        if point is None:
            continue
        feature, string_id = project.codes.resolve_parts(point.desc or "", commands=commands)
        if feature is not None and feature.kind in ("line", "polygon"):
            found[(feature.code.casefold(), str(string_id).casefold())] = (feature.code, str(string_id))
    return list(found.values()) if len(found) == 1 else []


def _set_line_curve_commands(description: str, code_prefix: str, curve_meanings: set[str], commands=None) -> str:
    """Replace only this line's PC/PT tokens, preserving other codes and free-text notes."""
    if not code_prefix:
        return description or ""
    semantic_tokens = command_map(commands)
    parts = PLC._split_multicode(description or "", semantic_tokens)
    changed = False
    for index, part in enumerate(parts):
        main, note, had_separator = PLC._description_parts(part, semantic_tokens)
        tokens = main.split()
        if not tokens or not PLC._code_matches(tokens[0], code_prefix):
            continue
        kept = [token for token in tokens[1:]
                if PLC._meaning(token, semantic_tokens) not in ("start_curve", "end_curve")]
        for meaning in ("start_curve", "end_curve"):
            token = semantic_tokens.get(meaning, "")
            if meaning in curve_meanings and token:
                kept.append(token)
        parts[index] = PLC._format_part(
            PLC._format_tokens([tokens[0], *kept], semantic_tokens), note,
            had_separator, semantic_tokens)
        changed = True
        break
    return PLC._join_multicode(parts, semantic_tokens) if changed else (description or "")


def _draft_point_description(description: str, code_prefix: str, boundary: str | None,
                             curve_meanings: set[str], commands=None) -> str:
    value = description or ""
    if code_prefix:
        value, _ = PLC._rewrite_code_part(
            value, code_prefix, boundary=boundary, commands=commands, clear_boundaries=True)
        value = _set_line_curve_commands(value, code_prefix, curve_meanings, commands)
    return PLC.fix_line_command_order(value, commands)


def _assert_new_numbers_unique(project, draft: LineEditDraft):
    reserved = {str(point.number).casefold(): point.id for point in project.points.values()}
    for vertex in draft.vertices:
        if not vertex.is_new_point or draft.mode != "points":
            continue
        number = str(vertex.number).strip()
        if not number:
            raise LineworkEditError("Every inserted point needs a point number.")
        prior = reserved.get(number.casefold())
        if prior is not None:
            raise LineworkEditError(f"Point number {number} is already used by point ID {prior}.")
        reserved[number.casefold()] = -1


def commit_line_draft(state, draft: LineEditDraft) -> int:
    """Apply one draft as a single undoable project edit and return its entity ID."""
    project = state.project
    if not draft.has_pending_actions:
        return draft.entity_id
    draft.assert_project_unchanged(project)
    if draft.mode == "points":
        if not draft.point_mode_ready:
            raise LineworkEditError("Point-edit mode no longer has complete, unambiguous vertex links.")
        _assert_new_numbers_unique(project, draft)

    project_entity = project.entities.get(draft.entity_id)
    if not isinstance(project_entity, Polyline):
        raise LineworkEditConflict("The line to edit no longer exists.")
    code_prefix = draft._line_code_prefix()
    commands = project.settings.get("f2f_commands")
    with state.edit("Edit line geometry", kinds=("points", "entities")) as pr:
        entity = pr.entities.get(draft.entity_id)
        if not isinstance(entity, Polyline):
            raise LineworkEditConflict("The line to edit no longer exists.")
        # Check again inside the transaction in case an integration changed it just before Apply.
        draft.assert_project_unchanged(pr)
        if draft.mode == "points":
            _assert_new_numbers_unique(pr, draft)
        id_by_temp: dict[str, int] = {}
        final_ids: list[int | None] = []
        if draft.mode == "points":
            for vertex in draft.vertices:
                if vertex.point_id is not None:
                    point = pr.points.get(vertex.point_id)
                    if point is None:
                        raise LineworkEditConflict(f"Linked point ID {vertex.point_id} no longer exists.")
                    point.x, point.y, point.z = float(vertex.x), float(vertex.y), _finite_z(vertex.z)
                    if vertex.description:
                        point.desc = str(vertex.description)
                    final_ids.append(point.id)
                elif vertex.is_new_point:
                    point_id = pr.new_id()
                    point = SurveyPoint(
                        point_id, str(vertex.number), float(vertex.x), float(vertex.y), _finite_z(vertex.z),
                        str(vertex.description or ""), str(vertex.layer or "POINTS"),
                        {"manual_linework_vertex": draft.entity_id})
                    point.set_original_state()
                    pr.ensure_layer(point.layer)
                    pr.points[point.id] = point
                    id_by_temp[vertex.temp_id] = point.id
                    final_ids.append(point.id)
                else:
                    raise LineworkEditError("An inserted vertex is not assigned to a survey point.")
            removed_ids = {vertex.point_id for vertex in draft.removed_vertices
                           if vertex.point_id is not None and vertex.point_id not in final_ids}
            if code_prefix:
                for pid in removed_ids:
                    point = pr.points.get(pid)
                    if point is not None:
                        point.desc = PLC.remove_point_token(point.desc or "", code_prefix, commands)

            curve_meanings_by_vertex = [set() for _ in draft.vertices]
            for segment_index in range(draft.segment_count):
                if abs(draft.bulges[segment_index]) > 1e-12:
                    start_index = segment_index
                    end_index = (segment_index + 1) % len(draft.vertices)
                    curve_meanings_by_vertex[start_index].add("start_curve")
                    curve_meanings_by_vertex[end_index].add("end_curve")
            boundary_by_vertex = [None] * len(draft.vertices)
            if draft.vertices:
                boundary_by_vertex[0] = "start_line"
                boundary_by_vertex[-1] = "close" if draft.closed else "end_line"
            for vertex, boundary, curve_meanings in zip(
                    draft.vertices, boundary_by_vertex, curve_meanings_by_vertex):
                if vertex.point_id is not None:
                    point = pr.points[vertex.point_id]
                    point.desc = _draft_point_description(
                        point.desc or vertex.description, code_prefix, boundary,
                        curve_meanings, commands)
                elif vertex.temp_id in id_by_temp:
                    point = pr.points[id_by_temp[vertex.temp_id]]
                    point.desc = _draft_point_description(
                        point.desc, code_prefix, boundary, curve_meanings, commands)
                    vertex.point_id = point.id
                    vertex.number = point.number
            final_ids = [vertex.point_id for vertex in draft.vertices]

        entity.verts = np.asarray([vertex.xyz() for vertex in draft.vertices], dtype=float)
        entity.bulges = np.asarray(draft.bulges, dtype=float) if any(
            abs(value) > 1e-14 for value in draft.bulges) else None
        entity.closed = bool(draft.closed)
        entity.attrs = copy.deepcopy(draft.attrs)
        if draft.mode == "points":
            entity.attrs["point_ids"] = list(final_ids)
            entity.attrs["points"] = [pr.points[pid].number for pid in final_ids]
        else:
            entity.attrs["point_ids"] = [vertex.point_id for vertex in draft.vertices]
            entity.attrs["points"] = [
                pr.points[vertex.point_id].number if vertex.point_id in pr.points else ""
                for vertex in draft.vertices]
        linked_for_inference = [vertex.point_id for vertex in draft.vertices]
        linked_for_inference.extend(vertex.point_id for vertex in draft.removed_vertices)
        override_keys = _inferred_linework_keys(pr, entity.attrs, linked_for_inference)
        is_generated_linework = bool(entity.derived and entity.derived.startswith("linework"))
        if is_generated_linework and not override_keys:
            raise LineworkEditError(
                "Cannot safely preserve this generated line: its feature-code/string identity could not be resolved.")
        override_point_ids = set(draft.base_points)
        try:
            override_point_ids.update(int(pid) for pid in draft.attrs.get("linework_override_point_ids", [])
                                      if pid is not None)
        except (TypeError, ValueError):
            pass
        if draft.mode == "points":
            override_point_ids.update(pid for pid in final_ids if pid is not None)
        if is_generated_linework and not draft.point_mode_ready:
            raise LineworkEditError(
                "Cannot safely override this generated line because one or more source vertices lack an unambiguous point link.")
        is_linework = is_generated_linework or bool(override_keys and override_point_ids)
        if is_linework:
            entity.attrs["manual_linework_override"] = True
            entity.attrs["linework_override_keys"] = [list(key) for key in override_keys]
            entity.attrs["linework_override_point_ids"] = sorted(override_point_ids)
            original_ids = list(draft.linked_point_ids)
            sequences = []
            if original_ids and all(pid is not None for pid in original_ids):
                sequences.append(tuple(map(int, original_ids)))
            if draft.mode == "points" and final_ids and all(pid is not None for pid in final_ids):
                sequences.append(tuple(map(int, final_ids)))
            primary_code = str(entity.attrs.get("code", "") or "").strip()
            primary_string = str(entity.attrs.get("string", "") or "").strip()
            primary_key = ((primary_code.casefold(), primary_string.casefold())
                           if primary_code else None)
            scope_map = copy.deepcopy(_override_scope_map(draft.attrs))
            for key in override_keys:
                normalized_key = (key[0].casefold(), key[1].casefold())
                if len(override_keys) == 1 or normalized_key == primary_key:
                    scoped_ids = override_point_ids
                    scoped_sequences = sequences
                else:
                    scoped_ids = set()
                    for pid in override_point_ids:
                        point = pr.points.get(pid)
                        if point is None:
                            continue
                        point_keys = _inferred_linework_keys(pr, {}, [pid])
                        if any((code.casefold(), string.casefold()) == normalized_key
                               for code, string in point_keys):
                            scoped_ids.add(pid)
                    scoped_sequences = []
                _merge_override_scope(scope_map, key, scoped_ids, scoped_sequences)
            entity.attrs["linework_override_scopes"] = _serialize_override_scope_map(scope_map)
            entity.derived = "manual-linework"
        else:
            entity.attrs.pop("manual_linework_override", None)
            entity.attrs.pop("linework_override_keys", None)
            entity.attrs.pop("linework_override_point_ids", None)
            entity.attrs.pop("linework_override_scopes", None)
    return draft.entity_id


@dataclass
class JoinPreview:
    verts: np.ndarray
    bulges: np.ndarray
    point_ids: list[int | None]
    source_point_ids: list[int]
    source_entity_ids: list[int]
    merge_pairs: list[tuple[int, int, tuple[float, float, float]]]
    point_updates: dict[int, tuple[float, float, float]]
    number_changes: dict[int, str]
    duplicate_numbers: dict[str, list[int]]
    number_conflicts: dict[str, list[int]]
    code_keys: list[tuple[str, str]]
    code_violations: list[str]
    link_issues: list[str]
    join_gaps: list[float]
    closed: bool = False


@dataclass
class _JoinSource:
    entity_id: int
    layer: str
    kind: str
    color: tuple | None
    linetype: str | None
    attrs: dict
    derived: str
    closed: bool
    verts: np.ndarray
    bulges: np.ndarray
    point_ids: list[int | None]
    link_issues: list[str]
    signature: dict
    base_points: dict[int, dict]


class JoinLinesDraft:
    """Draft a join/reversal/averaging result without changing any source entity or point."""

    def __init__(self, project, polylines: Sequence[Polyline], tolerance: float = 0.005):
        if len(polylines) < 2:
            raise LineworkEditError("Select at least two polylines to join.")
        self.project = project
        self.tolerance = float(tolerance)
        self.sources: dict[int, _JoinSource] = {}
        self.order: list[int] = []
        self.reversed_ids: set[int] = set()
        for entity in polylines:
            if not isinstance(entity, Polyline):
                continue
            if entity.id in self.sources:
                continue
            point_ids, link_issues = resolve_vertex_point_ids(project, entity, tolerance)
            bulges = np.zeros(len(entity.verts), dtype=float) if entity.bulges is None else np.asarray(entity.bulges, float)
            if len(bulges) < len(entity.verts):
                bulges = np.pad(bulges, (0, len(entity.verts) - len(bulges)))
            source = _JoinSource(
                int(entity.id), str(entity.layer), str(entity.kind), copy.deepcopy(entity.color), entity.linetype,
                copy.deepcopy(entity.attrs or {}), str(entity.derived or ""), bool(entity.closed),
                np.array(entity.verts, dtype=float, copy=True), np.asarray(bulges[:len(entity.verts)], float),
                list(point_ids), list(link_issues), _entity_signature(entity),
                {pid: _point_signature(project.points[pid]) for pid in point_ids if pid is not None})
            self.sources[entity.id] = source
            self.order.append(entity.id)
        if len(self.sources) < 2:
            raise LineworkEditError("Select at least two polylines to join.")

    def reorder(self, entity_id: int, offset: int):
        if entity_id not in self.order:
            raise LineworkEditError("The selected line is not part of this join draft.")
        index = self.order.index(entity_id)
        target = max(0, min(len(self.order) - 1, index + int(offset)))
        self.order.insert(target, self.order.pop(index))

    def set_reversed(self, entity_id: int, reverse: bool):
        if entity_id not in self.sources:
            raise LineworkEditError("The selected line is not part of this join draft.")
        if reverse:
            self.reversed_ids.add(int(entity_id))
        else:
            self.reversed_ids.discard(int(entity_id))

    def auto_orient(self):
        if len(self.order) < 2:
            return
        self.reversed_ids.clear()
        first = self.sources[self.order[0]]
        endpoint = first.verts[-1, :2]
        for entity_id in self.order[1:]:
            source = self.sources[entity_id]
            first_distance = float(np.linalg.norm(endpoint - source.verts[0, :2]))
            last_distance = float(np.linalg.norm(endpoint - source.verts[-1, :2]))
            reverse = last_distance < first_distance
            if reverse:
                self.reversed_ids.add(entity_id)
                endpoint = source.verts[0, :2]
            else:
                endpoint = source.verts[-1, :2]

    def build_preview(self, *, average: bool = True, condense: bool = False,
                      tolerance: float = 0.005, point_mode: bool = False,
                      normalize_numbers: bool = False, start_number: int = 1) -> JoinPreview:
        sources = [self.sources[entity_id] for entity_id in self.order]
        if any(source.closed for source in sources):
            raise LineworkEditError("Open closed polylines before joining them end-to-end.")
        if any(len(source.verts) < 2 for source in sources):
            raise LineworkEditError("Every selected line must contain at least two vertices.")
        for source in sources:
            if source.derived.startswith("linework") and (
                    source.link_issues or any(pid is None for pid in source.point_ids)):
                raise LineworkEditError(
                    f"Cannot safely join generated line {source.entity_id}: resolve every source-vertex point link first.")

        vertices: list[DraftVertex] = []
        bulges: list[float] = []
        point_ids: list[int | None] = []
        join_gaps: list[float] = []
        merge_pairs: list[tuple[int, int, tuple[float, float, float]]] = []
        source_point_ids: list[int] = []
        link_issues = []
        code_keys: list[tuple[str, str]] = []
        code_signature: list[tuple[str, str] | None] = []
        for source in sources:
            if source.link_issues:
                link_issues.extend(f"Polyline {source.entity_id}: {message}" for message in source.link_issues)
            line_vertices = [DraftVertex.from_point_or_vertex(
                xyz, self.project.points.get(source.point_ids[i]) if source.point_ids[i] is not None else None)
                for i, xyz in enumerate(source.verts)]
            line_bulges = list(map(float, source.bulges))
            line_point_ids = list(source.point_ids)
            if source.entity_id in self.reversed_ids:
                n = len(line_vertices)
                line_vertices.reverse()
                line_point_ids.reverse()
                if n:
                    line_bulges = [-line_bulges[(n - 2 - i) % n] for i in range(n)]
            for pid in line_point_ids:
                if pid is not None and pid not in source_point_ids:
                    source_point_ids.append(pid)
            keys = _inferred_linework_keys(self.project, source.attrs, source.point_ids)
            if source.derived.startswith("linework") and not keys:
                raise LineworkEditError(
                    f"Polyline {source.entity_id} has no resolvable code/string identity for a safe override.")
            code_keys.extend(keys)
            signature = keys[0] if len(keys) == 1 else None
            code_signature.append(signature)
            if not vertices:
                vertices = line_vertices
                bulges = line_bulges
                point_ids = line_point_ids
                continue
            gap = float(np.linalg.norm(vertices[-1].xyz()[:2] - line_vertices[0].xyz()[:2]))
            join_gaps.append(gap)
            if average:
                left, right = vertices[-1], line_vertices[0]
                xyz = (left.xyz() + right.xyz()) / 2.0
                if math.isfinite(left.z) and math.isfinite(right.z):
                    z_value = float((left.z + right.z) / 2.0)
                else:
                    z_value = left.z if math.isfinite(left.z) else right.z
                xyz[2] = z_value
                vertices[-1].x, vertices[-1].y, vertices[-1].z = map(float, xyz)
                left_id, right_id = point_ids[-1], line_point_ids[0]
                if left_id is None and right_id is not None:
                    vertices[-1].point_id = right_id
                    point_ids[-1] = right_id
                if left_id is not None and right_id is not None and left_id != right_id:
                    merge_pairs.append((left_id, right_id, tuple(map(float, xyz))))
                # The merged vertex is the joint: its outgoing arc comes from the next line.
                bulges[-1] = line_bulges[0]
                vertices.extend(line_vertices[1:])
                point_ids.extend(line_point_ids[1:])
                bulges.extend(line_bulges[1:])
            else:
                # Keep both endpoints and leave a straight connector between them.
                bulges[-1] = 0.0
                vertices.extend(line_vertices)
                point_ids.extend(line_point_ids)
                bulges.extend(line_bulges)

        if condense and len(vertices) > 2:
            vertices, point_ids, bulges, extra_merges = self._condense(
                vertices, point_ids, bulges, max(0.0, float(tolerance)))
            merge_pairs.extend(extra_merges)
        if len(vertices) < 2:
            raise LineworkEditError("Condensing would leave fewer than two line vertices.")

        # Open polylines have one unused trailing bulge slot for consistent model storage.
        if len(bulges) < len(vertices):
            bulges.extend([0.0] * (len(vertices) - len(bulges)))
        bulges = bulges[:len(vertices)]
        if bulges:
            bulges[-1] = 0.0

        code_keys = list({(c.casefold(), s.casefold()): (c, s) for c, s in code_keys}.values())
        normalized_signatures = {None if item is None else (item[0].casefold(), item[1].casefold())
                                 for item in code_signature}
        code_violations = []
        if len(normalized_signatures) > 1 or (None in normalized_signatures and len(normalized_signatures) > 1):
            code_violations.append("Selected lines use different or missing field-to-finish code/string identities.")
        elif len(code_keys) > 1:
            code_violations.append("The joined line preserves more than one code/string override.")

        number_ids: dict[str, list[int]] = {}
        for pid, point in self.project.points.items():
            number_ids.setdefault(str(point.number), []).append(pid)
        selected_ids = set(source_point_ids)
        duplicates = {
            number: ids for number, ids in number_ids.items()
            if len(ids) > 1 and any(pid in selected_ids for pid in ids)
        }
        number_changes: dict[int, str] = {}
        number_conflicts: dict[str, list[int]] = {}
        if normalize_numbers:
            try:
                first_number = int(start_number)
            except (TypeError, ValueError):
                raise LineworkEditError("The normalized point-number start must be an integer.")
            outside_by_number: dict[str, list[int]] = {}
            for pid, point in self.project.points.items():
                if pid not in source_point_ids:
                    outside_by_number.setdefault(str(point.number).casefold(), []).append(pid)
            for offset, pid in enumerate(source_point_ids):
                number = str(first_number + offset)
                conflicts = outside_by_number.get(number.casefold(), [])
                if conflicts:
                    number_conflicts[number] = list(conflicts)
                number_changes[pid] = number

        point_updates: dict[int, tuple[float, float, float]] = {}
        if point_mode:
            for vertex, pid in zip(vertices, point_ids):
                if pid is not None:
                    point_updates[pid] = tuple(map(float, vertex.xyz()))
            for left_id, right_id, xyz in merge_pairs:
                point_updates[left_id] = xyz
                point_updates[right_id] = xyz
        return JoinPreview(
            np.asarray([vertex.xyz() for vertex in vertices], dtype=float),
            np.asarray(bulges, dtype=float), list(point_ids), list(source_point_ids),
            list(self.order), merge_pairs, point_updates, number_changes, duplicates,
            number_conflicts, code_keys, code_violations, list(dict.fromkeys(link_issues)),
            join_gaps, False)

    @staticmethod
    def _condense(vertices, point_ids, bulges, tolerance):
        if not vertices:
            return vertices, point_ids, bulges, []
        output_vertices = [copy.deepcopy(vertices[0])]
        output_ids = [point_ids[0]]
        output_bulges = [0.0]
        merges = []
        for i in range(1, len(vertices)):
            current = copy.deepcopy(vertices[i])
            distance = float(np.linalg.norm(output_vertices[-1].xyz()[:2] - current.xyz()[:2]))
            if distance <= tolerance:
                if abs(output_bulges[-1]) > 1e-12 or abs(bulges[i - 1]) > 1e-12:
                    raise LineworkEditError("Condensing this duplicate would alter an adjacent curve; straighten it first.")
                left_id, right_id = output_ids[-1], point_ids[i]
                xyz = (output_vertices[-1].xyz() + current.xyz()) / 2.0
                if math.isfinite(output_vertices[-1].z) and math.isfinite(current.z):
                    xyz[2] = (output_vertices[-1].z + current.z) / 2.0
                else:
                    xyz[2] = output_vertices[-1].z if math.isfinite(output_vertices[-1].z) else current.z
                output_vertices[-1].x, output_vertices[-1].y, output_vertices[-1].z = map(float, xyz)
                if output_ids[-1] is None and right_id is not None:
                    output_ids[-1] = right_id
                    output_vertices[-1].point_id = right_id
                if left_id is not None and right_id is not None and left_id != right_id:
                    merges.append((left_id, right_id, tuple(map(float, xyz))))
                if i < len(vertices) - 1:
                    output_bulges[-1] = float(bulges[i])
                else:
                    output_bulges[-1] = 0.0
                continue
            output_bulges[-1] = float(bulges[i - 1])
            output_vertices.append(current)
            output_ids.append(point_ids[i])
            output_bulges.append(float(bulges[i]) if i < len(vertices) - 1 else 0.0)
        output_bulges[-1] = 0.0
        return output_vertices, output_ids, output_bulges, merges

    def assert_sources_unchanged(self, project):
        for entity_id in self.order:
            source = self.sources[entity_id]
            entity = project.entities.get(entity_id)
            if not isinstance(entity, Polyline) or not _same_entity(entity, source.signature):
                raise LineworkEditConflict(f"Polyline {entity_id} changed after the join draft was made.")
            for pid, signature in source.base_points.items():
                if not _same_point(project.points.get(pid), signature):
                    raise LineworkEditConflict(
                        f"Survey point ID {pid} changed after the join draft was made.")


def commit_join_draft(state, draft: JoinLinesDraft, preview: JoinPreview, *,
                      replace_sources: bool = True, point_mode: bool = False,
                      normalize_numbers: bool = False) -> int:
    """Accept a reviewed join preview as one undoable edit and return the new entity ID."""
    project = state.project
    draft.assert_sources_unchanged(project)
    if normalize_numbers and not point_mode:
        raise LineworkEditError("Normalize point numbers only in point-edit mode.")
    if point_mode and preview.link_issues:
        raise LineworkEditError("Point-edit mode requires an unambiguous survey point at every source vertex.")
    if normalize_numbers and preview.number_conflicts:
        text = ", ".join(sorted(preview.number_conflicts))
        raise LineworkEditError(f"Normalization conflicts with point number(s) already used outside the selected lines: {text}.")
    if normalize_numbers:
        selected_ids = set(preview.source_point_ids)
        current_conflicts = {}
        for number in preview.number_changes.values():
            owners = [pid for pid, point in project.points.items()
                      if pid not in selected_ids and str(point.number).casefold() == str(number).casefold()]
            if owners:
                current_conflicts[str(number)] = owners
        if current_conflicts:
            text = ", ".join(sorted(current_conflicts))
            raise LineworkEditError(
                f"Normalization now conflicts with point number(s) used outside the selected lines: {text}.")
    if point_mode:
        for pid in preview.source_point_ids:
            if pid not in project.points:
                raise LineworkEditConflict(f"Linked point ID {pid} no longer exists.")

    sources = [draft.sources[entity_id] for entity_id in draft.order]
    layer = sources[0].layer
    kind = sources[0].kind if all(source.kind == sources[0].kind for source in sources) else "line"
    color = sources[0].color if all(source.color == sources[0].color for source in sources) else None
    linetype = sources[0].linetype if all(source.linetype == sources[0].linetype for source in sources) else None
    override_keys = [list(key) for key in preview.code_keys]
    scope_map = {}
    all_override_point_ids = set(preview.source_point_ids)
    for source in sources:
        prior_scopes = _override_scope_map(source.attrs)
        for key, entry in prior_scopes.items():
            _merge_override_scope(scope_map, (entry["code"], entry["string"]),
                                  entry["point_ids"], entry["source_sequences"])
            all_override_point_ids.update(entry["point_ids"])
        source_keys = _inferred_linework_keys(project, source.attrs, source.point_ids)
        source_ids = {pid for pid in source.point_ids if pid is not None}
        source_ids.update(_coerce_point_id_set(source.attrs.get("linework_override_point_ids", [])))
        all_override_point_ids.update(source_ids)
        source_sequence = (tuple(map(int, source.point_ids))
                           if source.point_ids and all(pid is not None for pid in source.point_ids) else None)
        if len(source_keys) == 1:
            _merge_override_scope(scope_map, source_keys[0], source_ids,
                                  [source_sequence] if source_sequence else [])
        else:
            for key in source_keys:
                normalized_key = (key[0].casefold(), key[1].casefold())
                keyed_ids = set()
                for pid in source_ids:
                    point = project.points.get(pid)
                    if point is None:
                        continue
                    point_keys = _inferred_linework_keys(project, {}, [pid])
                    if any((code.casefold(), string.casefold()) == normalized_key
                           for code, string in point_keys):
                        keyed_ids.add(pid)
                _merge_override_scope(scope_map, key, keyed_ids)
    for code, string in preview.code_keys:
        _merge_override_scope(scope_map, (code, string))
    attrs = {
        "joined": True,
        "source_entity_ids": list(preview.source_entity_ids),
        "source_point_ids": list(preview.source_point_ids),
        "point_ids": list(preview.point_ids),
        "points": [project.points[pid].number if pid is not None and pid in project.points else ""
                   for pid in preview.point_ids],
        "manual_linework_override": True,
        "linework_override_keys": override_keys,
        "linework_override_point_ids": sorted(all_override_point_ids),
        "linework_override_scopes": _serialize_override_scope_map(scope_map),
    }
    if len(preview.code_keys) == 1:
        attrs["code"], attrs["string"] = preview.code_keys[0]
    with state.edit("Join lines", kinds=("points", "entities")) as pr:
        draft.assert_sources_unchanged(pr)
        if point_mode:
            for pid, xyz in preview.point_updates.items():
                point = pr.points.get(pid)
                if point is not None:
                    point.x, point.y, point.z = float(xyz[0]), float(xyz[1]), _finite_z(xyz[2])
            if normalize_numbers:
                for pid, number in preview.number_changes.items():
                    if pid in pr.points:
                        pr.points[pid].number = str(number)
                # Keep legacy point-number lists in other linked polylines coherent after a
                # range normalization, using stable point IDs as the source of truth.
                for other in pr.entities.values():
                    if not isinstance(other, Polyline):
                        continue
                    other_attrs = other.attrs or {}
                    linked = other_attrs.get("point_ids")
                    if isinstance(linked, (tuple, list)) and len(linked) == len(other.verts):
                        other_attrs["points"] = [
                            pr.points[int(pid)].number if pid is not None and int(pid) in pr.points else ""
                            for pid in linked]
            attrs["points"] = [pr.points[pid].number if pid is not None and pid in pr.points else ""
                               for pid in preview.point_ids]
        joined = pr.add_polyline(
            np.asarray(preview.verts, dtype=float), layer, False, preview.bulges,
            kind, "manual-linework", attrs, color, linetype)
        if replace_sources:
            pr.remove_entities(preview.source_entity_ids)
        # Keep the just-added line selected after the project's object IDs settle.
        state.select(points=(), entities=[joined.id], mode="replace")
    return joined.id


__all__ = [
    "LineworkEditError", "LineworkEditConflict", "DraftVertex", "LineEditDraft", "JoinPreview",
    "JoinLinesDraft", "bulge_point", "split_bulge", "bulge_points", "resolve_vertex_point_ids",
    "linework_override_keys", "commit_line_draft", "commit_join_draft",
]
