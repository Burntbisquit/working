"""Application state shared by every widget: the project, selection, undo/redo, tile store, signals."""
from __future__ import annotations

import contextlib
import time

from PySide6.QtCore import QObject, Signal

from ..core.crs import ProjectCRS
from ..core.project import Project
from ..core.settings import settings
from ..core.spatial import SpatialIndex
from ..core.tiles import TileStore

MAX_UNDO = 30


class AppState(QObject):
    changed = Signal(object)            # set of change kinds, e.g. {"points", "entities", "all"}
    selection_changed = Signal()
    project_replaced = Signal()
    dirty_changed = Signal(bool)
    message = Signal(str, str)          # level (info|warn|error|ok), text
    view_request = Signal(object)       # ("extents",) | ("bbox", (x0, y0, x1, y1)) | ("center", x, y)
    undo_changed = Signal()

    def __init__(self, project: Project | None = None):
        super().__init__()
        if isinstance(project, AppState):
            self.project = project.project
        else:
            self.project = project or Project("Untitled")
        # MainWindow installs a Save As callback.  Keeping it optional leaves the
        # Qt-free state usable by scripts and unit tests that explicitly manage paths.
        self._save_untitled_before_edit = None
        self.sel_points: set[int] = set()
        self.sel_entities: set[int] = set()
        self.undo_stack: list[tuple[str, bytes]] = []
        self.redo_stack: list[tuple[str, bytes]] = []
        self._dirty = False
        self.active_surface_id: int | None = None
        self._index: SpatialIndex | None = None
        self._index_key = None
        self._depth = 0
        st = settings()
        self.tiles = TileStore(st.tile_cache_dir, offline=bool(st.get("offline_imagery")))

    # ------------------------------------------------------------------ project lifecycle
    @property
    def dirty(self) -> bool:
        return self._dirty

    def set_dirty(self, v: bool):
        if v != self._dirty:
            self._dirty = v
            self.dirty_changed.emit(v)

    def set_project(self, project: Project, dirty: bool = False):
        self.project = project
        self.sel_points.clear()
        self.sel_entities.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._index = None
        self.active_surface_id = next(iter(project.surfaces), None)
        self.set_dirty(dirty)
        self.project_replaced.emit()
        self.undo_changed.emit()

    def new_project(self, name: str = "Untitled", crs: ProjectCRS | None = None):
        self.set_project(Project(name, crs))

    def open_project(self, path):
        p = Project.load(path)
        self.set_project(p)
        settings().add_recent(str(path))

    def save_project(self, path=None):
        self.project.save(path)
        settings().add_recent(str(self.project.path))
        self.set_dirty(False)

    # ------------------------------------------------------------------ editing / undo
    @contextlib.contextmanager
    def edit(self, label: str = "Edit", kinds=("all",), discard_if_unchanged: bool = False):
        """Run a block that modifies the project as ONE undoable step; rolls back if it raises.

        In the GUI, the initial generic ``Untitled`` project must be saved as a package
        before an edit can stick.  If the user cancels Save As, the action is rolled back
        and the state stays clean. Nested edits collapse into their outer transaction.
        """
        if self._depth > 0:
            yield self.project
            return

        dirty_before = self.dirty
        before = self.project.snapshot()
        cancelled_for_save = False
        if (not self.project.path and str(self.project.name or "").strip().casefold() in {"", "untitled"}
                and callable(self._save_untitled_before_edit)):
            try:
                saved = bool(self._save_untitled_before_edit())
            except Exception as exc:
                saved = False
                self.message.emit("error", f"Could not save the Untitled project before editing: {exc}")
            cancelled_for_save = not saved or not bool(self.project.path)
            if not cancelled_for_save:
                # Save As may rebase package paths and rename the project. Those changes
                # are the new baseline, not part of the edit's undo record.
                before = self.project.snapshot()

        self._depth += 1
        try:
            yield self.project
        except BaseException:
            self._depth -= 1
            self.project.restore(before)
            self._after_change({"all"}, quiet=True)
            raise
        self._depth -= 1

        if cancelled_for_save:
            self.project.restore(before)
            self._prune_selection()
            self.set_dirty(dirty_before)
            self._after_change({"all"})
            self.undo_changed.emit()
            self.message.emit(
                "warn",
                "Save As is required before editing an Untitled project. The attempted change was discarded.",
            )
            return

        if discard_if_unchanged and self.project.snapshot() == before:
            return
        self.undo_stack.append((label, before))
        if len(self.undo_stack) > MAX_UNDO:
            del self.undo_stack[0]
        self.redo_stack.clear()
        self.project.touch()
        self._prune_selection()
        self.set_dirty(True)
        self._after_change(set(kinds))
        self.undo_changed.emit()

    def _after_change(self, kinds, quiet: bool = False):
        """Invalidate the spatial index and tell the views. `quiet` skips the repaint signal -
        everything that paints reads the project directly, so a caller that already knows the
        view is about to be thrown away (the rollback path in edit()) does not need it."""
        self._index = None
        if not quiet:
            self.changed.emit(set(kinds))

    def undo(self):
        if not self.undo_stack:
            return None
        label, blob = self.undo_stack.pop()
        self.redo_stack.append((label, self.project.snapshot()))
        self.project.restore(blob)
        self._prune_selection()
        self.set_dirty(True)
        self._after_change({"all"})
        self.undo_changed.emit()
        return label

    def redo(self):
        if not self.redo_stack:
            return None
        label, blob = self.redo_stack.pop()
        self.undo_stack.append((label, self.project.snapshot()))
        self.project.restore(blob)
        self._prune_selection()
        self.set_dirty(True)
        self._after_change({"all"})
        self.undo_changed.emit()
        return label

    def refresh(self, kinds=("all",)):
        """Tell the UI the project changed (no undo entry) - for non-undoable tweaks like layer visibility."""
        self.project.touch()
        self.set_dirty(True)
        self._after_change(set(kinds))

    # ------------------------------------------------------------------ selection
    def _prune_selection(self):
        pts = {i for i in self.sel_points if i in self.project.points}
        ents = {i for i in self.sel_entities if i in self.project.entities}
        if pts != self.sel_points or ents != self.sel_entities:
            self.sel_points, self.sel_entities = pts, ents
            self.selection_changed.emit()

    def select(self, points=(), entities=(), mode: str = "replace"):
        pts, ents = set(points), set(entities)
        if mode == "replace":
            self.sel_points, self.sel_entities = pts, ents
        elif mode == "add":
            self.sel_points |= pts
            self.sel_entities |= ents
        elif mode == "toggle":
            self.sel_points ^= pts
            self.sel_entities ^= ents
        elif mode == "remove":
            self.sel_points -= pts
            self.sel_entities -= ents
        self.selection_changed.emit()

    def clear_selection(self):
        if self.sel_points or self.sel_entities:
            self.sel_points.clear()
            self.sel_entities.clear()
            self.selection_changed.emit()

    def selection_bbox(self):
        from ..core import geometry as G
        arrs = [[(self.project.points[i].x, self.project.points[i].y)] for i in self.sel_points if i in self.project.points]
        import numpy as np
        xy = [np.array(a) for a in arrs]
        for i in self.sel_entities:
            e = self.project.entities.get(i)
            if e is None:
                continue
            if hasattr(e, "verts"):
                xy.append(e.verts[:, :2])
            else:
                xy.append(np.array([[e.x, e.y]]))
        return G.bbox_of(xy) if xy else None

    # ------------------------------------------------------------------ helpers
    def spatial_index(self) -> SpatialIndex:
        key = (id(self.project), self.project.revision)
        if self._index is None or self._index_key != key:
            self._index = SpatialIndex(self.project)
            self._index_key = key
        return self._index

    def active_surface(self):
        s = self.project.surfaces.get(self.active_surface_id) if self.active_surface_id else None
        if s is None and self.project.surfaces:
            s = next(iter(self.project.surfaces.values()))
            self.active_surface_id = s.id
        return s

    def log(self, text: str, level: str = "info"):
        self.message.emit(level, text)

    def zoom_extents(self):
        self.view_request.emit(("extents",))

    def zoom_to_bbox(self, bbox):
        if bbox:
            self.view_request.emit(("bbox", tuple(bbox)))

    def center_on(self, x: float, y: float, scale: float | None = None):
        self.view_request.emit(("center", x, y, scale))

    @property
    def title_name(self) -> str:
        return self.project.name or "Untitled"
