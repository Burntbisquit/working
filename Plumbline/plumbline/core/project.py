"""The Project: everything in one drawing/job, plus (de)serialisation and undo snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import pickle
import time
import zlib
from pathlib import Path

import numpy as np

from . import units as U
from .crs import ProjectCRS
from .featurecodes import FeatureCodeTable, build_linework, default_codes, parse_description
from .groups import GroupSet
from .model import (ImageryCheck, ImageryLayer, ImportBatch, Layer, NAN, Polyline, SurveyPoint,
                    Surface, TextEntity)

FORMAT = "plumbline-project"
VERSION = 1

PALETTE = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 128, 0),
           (0, 160, 255), (255, 90, 90), (170, 255, 120), (200, 160, 255), (255, 200, 120), (120, 220, 200)]

DEFAULT_SETTINGS = {
    "contour_interval": 1.0,
    "index_every": 5,
    "contour_base": 0.0,
    "contour_smooth": 0,
    "contour_labels": True,
    "text_height": 2.0,          # drawing units (ground) for generated labels
    "plot_scale": 20.0,          # 1 in = N units, used by DXF text sizing
    "f2f_path": "",              # the office's field-to-finish code table last converted from
}


def _package_relative_path(root: Path, value) -> str:
    """Store an asset under a package root relative to the project file."""
    text = str(value or "")
    if not text or "://" in text:
        return text
    try:
        path = Path(text).expanduser()
        candidate = path if path.is_absolute() else root / path
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError, TypeError):
        return text


def _package_absolute_path(root: Path, value) -> str:
    """Resolve an internal relative package reference for the live application session."""
    text = str(value or "")
    if not text or "://" in text:
        return text
    try:
        path = Path(text).expanduser()
        if path.is_absolute():
            return text
        return str((root / path).resolve())
    except (OSError, RuntimeError, ValueError, TypeError):
        return text


# Everything an edit can change, and therefore everything undo/redo must carry.  `name` is in
# here so undo can restore a rename (and so AppState.edit(discard_if_unchanged=True) notices
# one); `path` deliberately is not - undoing an edit should never change which file you are
# saving to.
_STATE_KEYS = ("name", "crs", "settings", "layers", "points", "entities", "surfaces",
               "imagery", "checks", "codes", "_next_id", "notes", "groups")


class Project:
    def __init__(self, name: str = "Untitled", crs: ProjectCRS | None = None):
        self.name = name
        self.path: str | None = None
        self._portable_paths = False
        # No silent geodetic assumption: a project without a CRS uses plain local coordinates (US survey feet)
        self.crs: ProjectCRS = crs or ProjectCRS.local("ftUS")
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.layers: dict[str, Layer] = {}
        self.points: dict[int, SurveyPoint] = {}
        self.entities: dict[int, object] = {}          # Polyline | TextEntity
        self.surfaces: dict[int, Surface] = {}
        self.imagery: dict[int, ImageryLayer] = {}
        self.checks: dict[int, ImageryCheck] = {}
        # Named sets of objects that can be switched on and off together (core/groups.py).  A
        # group switched off hides its objects **everywhere** - drawing, exports and surfaces.
        self.groups: GroupSet = GroupSet()
        # A new project has no office vocabulary until a Field Book is selected or converted.
        # The built-in starter library remains available explicitly through the Feature Codes UI.
        self.codes: FeatureCodeTable = FeatureCodeTable()
        self.notes: str = ""
        self._next_id = 1
        self.revision = 0
        self._pa_cache = None
        # Layers start empty and populate from fieldwork processed through fieldbook using fieldbook settings

    # ------------------------------------------------------------------ basics
    @property
    def h_unit(self) -> str:
        return self.crs.unit

    @property
    def v_unit(self) -> str:
        return self.crs.vunit

    def new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def touch(self):
        self.revision += 1
        self._pa_cache = None

    def ensure_layer(self, name: str, color=None, linetype: str = "CONTINUOUS") -> Layer:
        lay = self.layers.get(name)
        if lay is None:
            if color is None:
                from .layer_definitions import get_layer_definition
                ld = get_layer_definition(name)
                color = ld.color
                linetype = ld.linetype
            lay = self.layers[name] = Layer(name, tuple(color), linetype)
        return lay

    def layer_color(self, name: str) -> tuple:
        lay = self.layers.get(name)
        return lay.color if lay else (255, 255, 255)

    # ------------------------------------------------------------------ points
    def next_point_number(self) -> str:
        mx = 0
        for p in self.points.values():
            try:
                mx = max(mx, int(p.number))
            except ValueError:
                pass
        return str(mx + 1)

    def point_by_number(self, number: str) -> SurveyPoint | None:
        number = str(number)
        for p in self.points.values():
            if p.number == number:
                return p
        return None

    def add_point(self, x, y, z=NAN, number: str | None = None, desc: str = "", layer: str | None = None,
                  attrs: dict | None = None) -> SurveyPoint:
        number = str(number) if number not in (None, "") else self.next_point_number()
        p = SurveyPoint(self.new_id(), number, float(x), float(y), NAN if z is None else float(z), desc,
                        layer or "POINTS", attrs or {})
        p.set_original_state()
        self.ensure_layer(p.layer)
        self.points[p.id] = p
        self.touch()
        return p

    def add_polyline(self, verts, layer: str = "0", closed: bool = False, bulges=None, kind: str = "line",
                     derived: str = "", attrs: dict | None = None, color=None, linetype=None) -> Polyline:
        self.ensure_layer(layer)
        pl = Polyline(self.new_id(), layer, np.asarray(verts, float), closed, bulges, kind, color, linetype,
                      attrs or {}, derived)
        self.entities[pl.id] = pl
        self.touch()
        return pl

    def add_text(self, x, y, text, height: float | None = None, rotation: float = 0.0, layer: str = "TEXT",
                 derived: str = "", color=None, attrs=None) -> TextEntity:
        self.ensure_layer(layer, (200, 255, 200))
        t = TextEntity(self.new_id(), layer, float(x), float(y), text,
                       height or self.settings.get("text_height", 2.0), rotation, color, attrs or {}, derived)
        self.entities[t.id] = t
        self.touch()
        return t

    def add_surface(self, surface: Surface) -> Surface:
        surface.id = self.new_id()
        self.surfaces[surface.id] = surface
        self.touch()
        return surface

    def surface_by_name(self, name: str) -> Surface | None:
        for s in self.surfaces.values():
            if s.name == name:
                return s
        return None

    def remove_points(self, ids):
        gone = [i for i in list(ids) if i in self.points]
        for i in gone:
            self.points.pop(i, None)
        self.groups.remove_objects(gone)          # a deleted object is not a group member
        self.touch()

    def remove_entities(self, ids):
        gone = [i for i in list(ids) if i in self.entities]
        for i in gone:
            self.entities.pop(i, None)
        self.groups.remove_objects(gone)
        self.touch()

    # ------------------------------------------------------------------ groups / visibility
    def hidden_ids(self) -> set:
        """Object ids hidden by a group that is switched off.  Empty when there are no groups."""
        return self.groups.hidden_ids()

    def visible_points(self) -> list:
        h = self.hidden_ids()
        return [p for p in self.points.values() if p.id not in h]

    def visible_entities(self) -> list:
        h = self.hidden_ids()
        return [e for e in self.entities.values() if getattr(e, "id", None) not in h]

    def visible_surfaces(self) -> list:
        h = self.hidden_ids()
        return [s for s in self.surfaces.values() if s.id not in h]

    def hidden_count(self) -> dict:
        h = self.hidden_ids()
        return {"points": sum(1 for i in self.points if i in h),
                "entities": sum(1 for i in self.entities if i in h),
                "surfaces": sum(1 for i in self.surfaces if i in h),
                "total": len(h)}

    def remove_derived(self, prefix: str) -> int:
        dead = [i for i, e in self.entities.items() if e.derived and e.derived.startswith(prefix)]
        for i in dead:
            del self.entities[i]
        if dead:
            self.touch()
        return len(dead)

    # ------------------------------------------------------------------ bulk ops
    def apply_batch(self, batch: ImportBatch, dup_policy: str = "renumber", layer_override: str | None = None,
                    code_layers: bool = True, reference_role: str | None = None) -> dict:
        """Merge an ImportBatch.  dup_policy: renumber | skip | overwrite | keep.

        *reference_role* is set when the batch is reference data (stake-out / control / other).
        A duplicate number then means "duplicate of the same reference list", never of the field
        points: the two lists keep their own numbering, which is the whole point of keeping them
        apart (see :mod:`plumbline.core.reference`).
        """
        from . import reference as REF

        st = dict(points=0, duplicates=0, renumbered=0, skipped=0, overwritten=0, polylines=0, texts=0,
                  surfaces=0, point_ids=[])
        original_numbers: dict[int, str] = {}
        for name, lay in batch.layers.items():
            if name not in self.layers:
                self.layers[name] = lay
        if reference_role:
            def in_scope(other):                    # noqa: E306 - a one-line predicate, kept close to its use
                return REF.role_of(other) == reference_role
        else:
            def in_scope(other):
                return not REF.is_reference(other)
        num_index = {p.number: p.id for p in self.points.values() if in_scope(p)}
        mx = 0
        for n in num_index:
            try:
                mx = max(mx, int(n))
            except ValueError:
                pass
        for p in batch.points:
            original_number = str(p.number)
            if layer_override:
                p.layer = layer_override
            if p.number in num_index and dup_policy != "keep":
                st["duplicates"] += 1
                if dup_policy == "skip":
                    st["skipped"] += 1
                    continue
                if dup_policy == "overwrite":
                    old = self.points[num_index[p.number]]
                    old.set_original_state()
                    old.x, old.y, old.z, old.desc = p.x, p.y, p.z, p.desc
                    st["overwritten"] += 1
                    st["point_ids"].append(old.id)          # the batch landed on this point
                    continue
                mx += 1
                if p.attrs is None:
                    p.attrs = {}
                p.attrs["orig_number"] = p.number
                p.number = str(mx)
                st["renumbered"] += 1
            else:
                try:
                    mx = max(mx, int(p.number))
                except ValueError:
                    pass
            p.id = self.new_id()
            self.ensure_layer(p.layer)
            self.points[p.id] = p
            original_numbers[p.id] = original_number
            num_index[p.number] = p.id
            st["points"] += 1
            st["point_ids"].append(p.id)
        for pl in batch.polylines:
            if layer_override:
                pl.layer = layer_override
            pl.id = self.new_id()
            self.ensure_layer(pl.layer)
            self.entities[pl.id] = pl
            st["polylines"] += 1
        for t in batch.texts:
            if layer_override:
                t.layer = layer_override
            t.id = self.new_id()
            self.ensure_layer(t.layer)
            self.entities[t.id] = t
            st["texts"] += 1
        for s in batch.surfaces:
            base, k = s.name, 1
            while self.surface_by_name(s.name):
                k += 1
                s.name = f"{base} ({k})"
            s.id = self.new_id()
            self.surfaces[s.id] = s
            st["surfaces"] += 1
        if code_layers and batch.points and not layer_override:
            self.apply_codes_to_points([p for p in batch.points if p.id in self.points])
        for pid, original_number in original_numbers.items():
            point = self.points.get(pid)
            if point is not None:
                point.set_original_state(number=original_number)
        self.touch()
        return st

    # ------------------------------------------------------------------ feature codes
    def apply_codes_to_points(self, pts=None) -> dict:
        """Move points onto the layer of their feature code.  Returns matched/unknown counts."""
        pts = list(self.points.values()) if pts is None else list(pts)
        matched, unknown = 0, {}
        commands = self.settings.get("f2f_commands")
        for p in pts:
            pd = parse_description(p.desc, commands=commands, known_codes=self.codes.codes)
            fc = self.codes.get(pd.code) if pd.code else None
            if fc is not None:
                if fc.layer:
                    self.ensure_layer(fc.layer, fc.color, fc.linetype if fc.kind != "point" else "CONTINUOUS")
                    p.layer = fc.layer
                matched += 1
            elif pd.code:
                unknown[pd.code] = unknown.get(pd.code, 0) + 1
        self.touch()
        return {"matched": matched, "unknown": unknown}

    def linework_input_signature(self, order: str = "file") -> str:
        """Fingerprint the point coding, coordinates, feature codes and command map used to build lines."""
        # Mirror build_linework's ordering exactly: its default/file mode sorts by point id, while
        # number mode sorts by numeric point number (with the same id tie-breaker).
        ordered_points = list(self.points.values())
        if order == "number":
            def point_number_key(point):
                try:
                    return (0, float(point.number), point.id)
                except ValueError:
                    return (1, 0.0, point.id)
            ordered_points.sort(key=point_number_key)
        else:
            ordered_points.sort(key=lambda point: point.id)
        points = [
            [point.id, str(point.number), point.x, point.y, point.z, str(point.desc or "")]
            for point in ordered_points
        ]
        if hasattr(self.codes, "sorted_codes_json"):
            codes_json = self.codes.sorted_codes_json()
        else:
            codes = sorted(self.codes.to_list(),
                           key=lambda item: (str(item.get("code", "")).casefold(),
                                             str(item.get("code", ""))))
            codes_json = json.dumps(codes, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, default=str, allow_nan=True)
        manual_overrides = [
            [entity.id, entity.attrs or {}]
            for entity in sorted(self.entities.values(), key=lambda item: item.id)
            if isinstance(entity, Polyline)
            and (entity.attrs or {}).get("manual_linework_override")
        ]
        commands_json = json.dumps((self.settings or {}).get("f2f_commands"),
                                   sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, default=str, allow_nan=True)
        overrides_json = json.dumps(manual_overrides, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, default=str, allow_nan=True)
        order_json = json.dumps(str(order), ensure_ascii=False)
        points_json = json.dumps(points, separators=(",", ":"),
                                 ensure_ascii=False, default=str, allow_nan=True)
        encoded = (
            f'{{"codes":{codes_json},"commands":{commands_json},'
            f'"manual_overrides":{overrides_json},"order":{order_json},'
            f'"points":{points_json},"version":"linework-build-v1"}}'
        ).encode("utf-8")
        return hashlib.blake2b(encoded, digest_size=24).hexdigest()

    def linework_is_processed(self) -> bool:
        """Whether derived linework matches the current build inputs.

        Older projects predate the content signature. Their existing generated lines are treated as
        the last processed state once loaded; from_dict records that baseline so subsequent point or
        code edits make it stale.
        """
        signature = str((self.settings or {}).get("linework_processed_signature") or "")
        if signature:
            order = str((self.settings or {}).get("linework_processed_order") or "file")
            return signature == self.linework_input_signature(order)
        return any(str(getattr(entity, "derived", "") or "").startswith("linework")
                   or getattr(entity, "derived", "") == "manual-linework"
                   for entity in self.entities.values())

    def process_linework(self, order: str = "file") -> dict:
        """(Re)build coded linework, respecting geometry edits accepted as manual overrides."""
        input_signature = self.linework_input_signature(order)
        manual_overrides = {}
        for entity in self.entities.values():
            if not isinstance(entity, Polyline):
                continue
            attrs = entity.attrs or {}
            if not attrs.get("manual_linework_override"):
                continue
            raw_scopes = attrs.get("linework_override_scopes", []) or []
            valid_scopes = False
            if isinstance(raw_scopes, (tuple, list)):
                for raw in raw_scopes:
                    if not isinstance(raw, dict) or not str(raw.get("code", "")).strip():
                        continue
                    code = str(raw.get("code", "")).strip()
                    string = str(raw.get("string", "") or "").strip()
                    source_ids = set()
                    for pid in raw.get("point_ids", []) or []:
                        try:
                            source_ids.add(int(pid))
                        except (TypeError, ValueError):
                            continue
                    source_sequences = set()
                    for sequence in raw.get("source_sequences", []) or []:
                        if not isinstance(sequence, (tuple, list)) or len(sequence) < 2:
                            continue
                        try:
                            source_sequences.add(tuple(int(pid) for pid in sequence))
                        except (TypeError, ValueError):
                            continue
                    key = (code.casefold(), string.casefold())
                    manual_overrides.setdefault(key, []).append((source_ids, source_sequences))
                    valid_scopes = True
            if not valid_scopes:
                raw_ids = attrs.get("linework_override_point_ids", []) or []
                source_ids = set()
                for pid in raw_ids:
                    try:
                        source_ids.add(int(pid))
                    except (TypeError, ValueError):
                        continue
                source_sequences = set()
                for sequence in attrs.get("linework_override_source_sequences", []) or []:
                    if not isinstance(sequence, (tuple, list)) or len(sequence) < 2:
                        continue
                    try:
                        source_sequences.add(tuple(int(pid) for pid in sequence))
                    except (TypeError, ValueError):
                        continue
                for raw in attrs.get("linework_override_keys", []) or []:
                    if isinstance(raw, (tuple, list)) and len(raw) >= 2 and str(raw[0]).strip():
                        key = (str(raw[0]).casefold(), str(raw[1]).casefold())
                        manual_overrides.setdefault(key, []).append((source_ids, source_sequences))
        removed = self.remove_derived("linework")
        strings = build_linework(self.points.values(), self.codes, order,
                                 commands=self.settings.get("f2f_commands"))
        made = 0
        for ls in strings:
            override_sets = manual_overrides.get((str(ls.code).casefold(), str(ls.string).casefold()), [])
            line_sequence = tuple(int(pid) for pid in ls.ids)
            line_ids = set(line_sequence)
            suppressed = False
            for source_ids, source_sequences in override_sets:
                if source_sequences:
                    if any(line_sequence == sequence or line_sequence == sequence[::-1]
                           for sequence in source_sequences):
                        suppressed = True
                        break
                elif source_ids and line_ids and line_ids.issubset(source_ids):
                    # Compatibility for old overrides that only recorded the source point set.
                    suppressed = True
                    break
            if suppressed:
                continue
            fc = self.codes.get(ls.code)
            pts = [self.points[i] for i in ls.ids if i in self.points]
            if len(pts) < 2:
                continue
            verts = np.array([[p.x, p.y, p.z] for p in pts])
            self.add_polyline(verts, fc.layer or "0", ls.closed, None,
                              "breakline" if fc.breakline else "line", "linework",
                              {"code": ls.code, "string": ls.string,
                               "points": [p.number for p in pts], "point_ids": [p.id for p in pts]})
            made += 1
        self.settings["linework_processed_signature"] = input_signature
        self.settings["linework_processed_order"] = str(order)
        self.touch()
        return {"strings": made, "replaced": removed}

    # ------------------------------------------------------------------ arrays
    def point_arrays(self, ids=None):
        """(ids (n,), xyz (n,3)) - cached per revision when ids is None."""
        if ids is None and self._pa_cache and self._pa_cache[0] == self.revision:
            return self._pa_cache[1], self._pa_cache[2]
        pts = self.points.values() if ids is None else [self.points[i] for i in ids if i in self.points]
        n = len(self.points) if ids is None else len(pts)
        out_ids = np.empty(n, np.int64)
        xyz = np.empty((n, 3))
        for k, p in enumerate(pts):
            out_ids[k] = p.id
            xyz[k] = (p.x, p.y, p.z)
        if ids is None:
            self._pa_cache = (self.revision, out_ids, xyz)
        return out_ids, xyz

    def extents(self):
        """(xmin, ymin, xmax, ymax) over everything, or None when empty."""
        boxes = []
        if self.points:
            _, xyz = self.point_arrays()
            boxes.append((xyz[:, 0].min(), xyz[:, 1].min(), xyz[:, 0].max(), xyz[:, 1].max()))
        for e in self.entities.values():
            if isinstance(e, Polyline) and len(e.verts):
                v = e.verts
                boxes.append((np.nanmin(v[:, 0]), np.nanmin(v[:, 1]), np.nanmax(v[:, 0]), np.nanmax(v[:, 1])))
            elif isinstance(e, TextEntity):
                boxes.append((e.x, e.y, e.x, e.y))
        for s in self.surfaces.values():
            if len(s.pts):
                boxes.append((s.pts[:, 0].min(), s.pts[:, 1].min(), s.pts[:, 0].max(), s.pts[:, 1].max()))
        boxes = [b for b in boxes if all(math.isfinite(v) for v in b)]
        if not boxes:
            return None
        b = np.array(boxes)
        return float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 2].max()), float(b[:, 3].max())

    def polylines(self, kinds=None, layers=None, derived=None):
        for e in self.entities.values():
            if not isinstance(e, Polyline):
                continue
            if kinds and e.kind not in kinds:
                continue
            if layers and e.layer not in layers:
                continue
            if derived is not None and not e.derived.startswith(derived):
                continue
            yield e

    # ------------------------------------------------------------------ CRS operations
    def assign_crs(self, new_crs: ProjectCRS):
        """Re-label the coordinate system WITHOUT moving any data."""
        self.crs = new_crs
        self.touch()

    def reproject(self, new_crs: ProjectCRS, strategy: str | None = None, convert_z: bool = True):
        """Transform every coordinate into a new CRS (data physically moves)."""
        old_crs = self.crs
        fn = self.crs.transform_to(new_crs, strategy)
        zs = U.M_PER_UNIT[self.crs.vunit] / U.M_PER_UNIT[new_crs.vunit] if convert_z else 1.0
        hs = U.M_PER_UNIT[self.crs.unit] / U.M_PER_UNIT[new_crs.unit]
        if self.points:
            ids, xyz = self.point_arrays()
            nx, ny = fn(xyz[:, 0], xyz[:, 1])
            for i, a, b in zip(ids, nx, ny):
                p = self.points[int(i)]
                p.x, p.y = float(a), float(b)
                if not math.isnan(p.z):
                    p.z *= zs
        for e in self.entities.values():
            if isinstance(e, Polyline):
                nx, ny = fn(e.verts[:, 0], e.verts[:, 1])
                e.verts = np.column_stack([nx, ny, e.verts[:, 2] * zs])
            elif isinstance(e, TextEntity):
                nx, ny = fn(np.array([e.x]), np.array([e.y]))
                e.x, e.y = float(nx[0]), float(ny[0])
                e.height *= hs
        for s in self.surfaces.values():
            nx, ny = fn(s.pts[:, 0], s.pts[:, 1])
            s.set_geometry(np.column_stack([nx, ny, s.pts[:, 2] * zs]), s.tris)
        for c in self.checks.values():
            sx, sy = fn(np.array([c.survey_xy[0]]), np.array([c.survey_xy[1]]))
            ix, iy = fn(np.array([c.image_xy[0]]), np.array([c.image_xy[1]]))
            c.survey_xy, c.image_xy = (float(sx[0]), float(sy[0])), (float(ix[0]), float(iy[0]))
        for im in self.imagery.values():
            im.nudge = (im.nudge[0] * hs, im.nudge[1] * hs)
        self.settings["text_height"] = self.settings.get("text_height", 2.0) * hs
        self.settings["contour_interval"] = self.settings.get("contour_interval", 1.0) * zs
        self.crs = new_crs
        # The Point(s) Audit's record of what the points were is in coordinates too, so it is
        # moved with them.  Done here, at the one moment the system change and the coordinate
        # change are both known: told afterwards, a reprojection and an assignment (which changes
        # the name and keeps every number) look identical, and the wrong guess reports the whole
        # job as having moved.  See core/audit.py.
        from . import audit as AUD
        AUD.reprojected(self, old_crs)
        self.touch()

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        data = {
            "format": FORMAT, "version": VERSION, "name": self.name, "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
            "crs": self.crs.to_dict(), "settings": self.settings, "notes": self.notes,
            "layers": [l.to_dict() for l in self.layers.values()],
            "points": [p.to_list() for p in self.points.values()],
            "entities": [e.to_dict() for e in self.entities.values()],
            "surfaces": [s.to_dict() for s in self.surfaces.values()],
            "imagery": [i.to_dict() for i in self.imagery.values()],
            "checks": [c.to_dict() for c in self.checks.values()],
            "codes": self.codes.to_list(), "next_id": self._next_id,
            "groups": self.groups.to_list(),
        }
        if self._portable_paths:
            data["package"] = {"relative_asset_paths": True}
            if self.path:
                root = Path(self.path).expanduser().resolve().parent
                data["settings"] = copy.deepcopy(self.settings)
                for key in ("fieldbook_file", "f2f_path", "data_folder", "field_data_source_folder"):
                    value = data["settings"].get(key)
                    if value:
                        data["settings"][key] = _package_relative_path(root, value)
                data["imagery"] = copy.deepcopy(data["imagery"])
                for layer in data["imagery"]:
                    source = layer.get("source")
                    if isinstance(source, dict) and source.get("path"):
                        source["path"] = _package_relative_path(root, source["path"])
                data["points"] = copy.deepcopy(data["points"])
                for point in data["points"]:
                    attrs = point[7] if len(point) > 7 else None
                    if not isinstance(attrs, dict):
                        continue
                    for key in ("fieldwork_source", "fieldwork_folder"):
                        if attrs.get(key):
                            attrs[key] = _package_relative_path(root, attrs[key])
                    imported = attrs.get("import")
                    if isinstance(imported, dict) and imported.get("folder"):
                        imported["folder"] = _package_relative_path(root, imported["folder"])
        if self.path:
            # File-backed imagery inside the project folder should remain usable after
            # the folder is moved, even for projects not created through Save As.
            root = Path(self.path).expanduser().resolve().parent
            data["imagery"] = [
                {**layer, "source": dict(layer.get("source") or {})}
                for layer in data["imagery"]
            ]
            has_internal_imagery = False
            for layer in data["imagery"]:
                if layer.get("kind") != "file":
                    continue
                source = layer.get("source")
                value = source.get("path") if isinstance(source, dict) else None
                if not value or "://" in str(value):
                    continue
                try:
                    path = Path(str(value)).expanduser()
                    candidate = path if path.is_absolute() else root / path
                    source["path"] = candidate.resolve().relative_to(root).as_posix()
                except (OSError, RuntimeError, ValueError, TypeError):
                    continue
                has_internal_imagery = True
            if has_internal_imagery:
                package_meta = data.get("package")
                if not isinstance(package_meta, dict):
                    package_meta = {}
                data["package"] = {**package_meta, "relative_asset_paths": True}
        return data

    def save(self, path: str | os.PathLike | None = None):
        path = Path(path or self.path or "")
        if not str(path):
            raise ValueError("no path")
        previous_path = self.path
        self.path = str(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            self.path = previous_path
            raise
        self.path = str(path)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        if d.get("format") != FORMAT:
            raise ValueError("Not a Plumbline project file")
        p = cls(d.get("name", "Untitled"), ProjectCRS.from_dict(d["crs"]))
        package_meta = d.get("package") or {}
        p._portable_paths = bool(
            isinstance(package_meta, dict) and package_meta.get("relative_asset_paths"))
        p.settings = {**DEFAULT_SETTINGS, **d.get("settings", {})}
        p.notes = d.get("notes", "")
        p.layers = {l["name"]: Layer.from_dict(l) for l in d.get("layers", [])}
        p.points = {v[0]: SurveyPoint.from_list(v) for v in d.get("points", [])}
        for e in d.get("entities", []):
            ent = Polyline.from_dict(e) if e.get("t") == "pl" else TextEntity.from_dict(e)
            p.entities[ent.id] = ent
        for s in d.get("surfaces", []):
            sf = Surface.from_dict(s)
            p.surfaces[sf.id] = sf
        for i in d.get("imagery", []):
            im = ImageryLayer.from_dict(i)
            p.imagery[im.id] = im
        for c in d.get("checks", []):
            ck = ImageryCheck.from_dict(c)
            p.checks[ck.id] = ck
        p.groups = GroupSet.from_list(d.get("groups", []))
        if "codes" in d:
            # An explicit empty list is meaningful for a new job with no selected Field Book.
            p.codes = FeatureCodeTable.from_list(d.get("codes") or [])
        else:
            # Preserve the historical fallback for older project files that predate the code table.
            p.codes = default_codes()
        p._next_id = int(d.get("next_id", 1))
        p.ensure_layer("0", (230, 230, 230))
        p.ensure_layer("POINTS", (255, 255, 255))
        # Older project files have no input signature. Treat any existing derived linework as
        # processed at the loaded point/code state, then let signature comparison mark it stale
        # after the next edit/import.
        if (not p.settings.get("linework_processed_signature")
                and any(str(getattr(entity, "derived", "") or "").startswith("linework")
                        or getattr(entity, "derived", "") == "manual-linework"
                        for entity in p.entities.values())):
            p.settings["linework_processed_order"] = "file"
            p.settings["linework_processed_signature"] = p.linework_input_signature("file")
        p.touch()
        return p

    @classmethod
    def load(cls, path) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        p = cls.from_dict(d)
        p.path = str(path)
        if p._portable_paths:
            root = Path(path).expanduser().resolve().parent
            for key in ("fieldbook_file", "f2f_path", "data_folder", "field_data_source_folder"):
                value = p.settings.get(key)
                if value:
                    p.settings[key] = _package_absolute_path(root, value)
            for layer in p.imagery.values():
                source = getattr(layer, "source", None)
                if isinstance(source, dict) and source.get("path"):
                    source["path"] = _package_absolute_path(root, source["path"])
        return p

    # ------------------------------------------------------------------ transforms
    def apply_similarity(self, base=(0.0, 0.0), scale: float = 1.0, rot_deg: float = 0.0, shift=(0.0, 0.0),
                         dz: float = 0.0, z_scale: float = 1.0, point_ids=None, entity_ids=None,
                         include_surfaces: bool = True) -> dict:
        """Scale and rotate (counter-clockwise) about `base`, then shift.  None ids = everything.

        Surfaces move with a whole-project transform; for a partial transform they are left alone (and will report stale).
        """
        bx, by = base
        a = math.radians(rot_deg)
        c, s = math.cos(a) * scale, math.sin(a) * scale

        def xf(x, y):
            x = np.asarray(x, float) - bx
            y = np.asarray(y, float) - by
            return bx + c * x - s * y + shift[0], by + s * x + c * y + shift[1]

        n_p = n_e = n_s = 0
        for p in self.points.values():
            if point_ids is None or p.id in point_ids:
                p.x, p.y = (float(v) for v in xf(p.x, p.y))
                if not math.isnan(p.z):
                    p.z = p.z * z_scale + dz
                n_p += 1
        for e in self.entities.values():
            if entity_ids is None or e.id in entity_ids:
                if isinstance(e, Polyline):
                    x, y = xf(e.verts[:, 0], e.verts[:, 1])
                    z = e.verts[:, 2] * z_scale + dz
                    e.verts = np.column_stack([x, y, z])
                    if e.bulges is not None and scale < 0:
                        e.bulges = -e.bulges
                else:
                    e.x, e.y = (float(v) for v in xf(e.x, e.y))
                    e.rotation = (e.rotation + rot_deg) % 360.0
                    e.height *= abs(scale)
                n_e += 1
        if include_surfaces and point_ids is None and entity_ids is None:
            for sf in self.surfaces.values():
                x, y = xf(sf.pts[:, 0], sf.pts[:, 1])
                sf.set_geometry(np.column_stack([x, y, sf.pts[:, 2] * z_scale + dz]), sf.tris)
                n_s += 1
        self.touch()
        return {"points": n_p, "entities": n_e, "surfaces": n_s}

    # ------------------------------------------------------------------ undo snapshots
    def snapshot(self) -> bytes:
        state = {k: getattr(self, k) for k in _STATE_KEYS}
        return zlib.compress(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL), 1)

    def restore(self, blob: bytes):
        state = pickle.loads(zlib.decompress(blob))
        for k, v in state.items():
            setattr(self, k, v)
        self.touch()

    # ------------------------------------------------------------------ misc
    def summary(self) -> dict:
        return {"points": len(self.points),
                "polylines": sum(1 for e in self.entities.values() if isinstance(e, Polyline)),
                "texts": sum(1 for e in self.entities.values() if isinstance(e, TextEntity)),
                "surfaces": len(self.surfaces), "layers": len(self.layers),
                "checks": len(self.checks), "crs": self.crs.label}
