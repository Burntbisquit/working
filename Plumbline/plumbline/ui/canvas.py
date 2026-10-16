"""The drawing canvas widget: pan / zoom / snapping / tool dispatch on top of render.SceneRenderer."""
from __future__ import annotations

import dataclasses
import math
import threading

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ..core.model import Polyline
from ..core.settings import settings
from . import theme
from .render import DisplayOptions, ImageryManager, SceneRenderer, View, make_path, pen_for


class CanvasView(QWidget):
    cursor_moved = Signal(float, float, float)      # world x, y, surface z (nan if none)
    view_changed = Signal()
    hint_changed = Signal(str)
    files_dropped = Signal(list)
    context_requested = Signal(object)              # QPoint (global)
    tool_changed = Signal(str)
    escape_pressed = Signal()          # Esc in the drawing view: release the tool, go to Pan
    snap_changed = Signal(str)

    OVERSCAN = 0.22

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.view = View(0, 0, 1.0, 800, 600)
        self.opts = DisplayOptions()
        self.imagery = ImageryManager(state)
        self.renderer = SceneRenderer(state, self.imagery)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 240)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        self._pix: QPixmap | None = None
        self._pix_view: View | None = None
        self._dirty = True
        self._panning = False
        self._pan_last = None
        self._fit_pending = True
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._rerender_now)

        self.snap_enabled = True
        self.snap_modes = {"point", "node", "mid", "nearest"}
        self.snap_hit = None
        self.cursor_world: tuple | None = None
        self.current_layer = "0"
        self.point_desc = ""
        self.point_role = ""            # "" = field data; else stakeout / control / other
        self.tool = None
        self._prep_started: set = set()
        self.extra_overlay = None            # callable(painter, view) for dialogs/workflows
        self.view_overlays: list = []        # more callables(painter, view), e.g. the depth view's slab

        state.changed.connect(self._on_changed)
        state.selection_changed.connect(self.update)
        state.project_replaced.connect(self._on_project_replaced)
        state.view_request.connect(self.handle_view_request)
        self.imagery.tilesChanged.connect(self._tiles_changed)
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.timeout.connect(self.invalidate)

    # ------------------------------------------------------------------ tools
    def set_option(self, name: str, value):
        setattr(self.opts, name, value)
        self.invalidate()

    def set_tool(self, tool):
        if self.tool is not None:
            self.tool.deactivate()
        self.tool = tool
        tool.activate()
        self.setCursor(tool.cursor)
        self.snap_hit = None
        self.hint_changed.emit(tool.hint())
        self.tool_changed.emit(tool.name)
        self.update()

    # ------------------------------------------------------------------ state reactions
    def _on_changed(self, kinds):
        self.renderer.invalidate()
        self.invalidate()

    def _on_project_replaced(self):
        self.renderer.invalidate()
        self.imagery.clear()
        self._prep_started.clear()
        self.snap_hit = None
        self.zoom_extents()

    def _tiles_changed(self):
        self._tile_timer.start(60)

    def invalidate(self):
        self._dirty = True
        self.update()

    # ------------------------------------------------------------------ view control
    def handle_view_request(self, req):
        kind = req[0]
        if kind == "extents":
            self.zoom_extents()
        elif kind == "bbox":
            self.zoom_bbox(req[1])
        elif kind == "center":
            self.view.cx, self.view.cy = req[1], req[2]
            if len(req) > 3 and req[3]:
                self.view.scale = self._clamp_scale(req[3])
            self._view_moved()

    def _clamp_scale(self, s: float) -> float:
        return max(1e-5, min(1e6, s))

    def project_bbox(self):
        ext = self.state.project.extents()
        return ext

    def zoom_extents(self):
        ext = self.project_bbox()
        self.zoom_bbox(ext) if ext else self._default_view()

    def _default_view(self):
        self.view.cx = self.view.cy = 0.0
        self.view.scale = 1.0
        self._view_moved()

    def zoom_bbox(self, bbox, margin: float = 0.08):
        x0, y0, x1, y1 = bbox
        pad = max(x1 - x0, y1 - y0, 10.0)
        if x1 - x0 < 1e-9 and y1 - y0 < 1e-9:
            x0, x1, y0, y1 = x0 - 25, x0 + 25, y0 - 25, y0 + 25
        elif max(x1 - x0, y1 - y0) < 1e-6:
            x0, x1, y0, y1 = x0 - 5, x1 + 5, y0 - 5, y1 + 5
        v = self.view
        dx, dy = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        v.cx, v.cy = (x0 + x1) / 2, (y0 + y1) / 2
        v.scale = self._clamp_scale(min(v.w / (dx * (1 + 2 * margin)), v.h / (dy * (1 + 2 * margin))))
        self._view_moved()

    def zoom_at(self, sx: float, sy: float, factor: float):
        v = self.view
        wx, wy = v.to_world(sx, sy)
        v.scale = self._clamp_scale(v.scale * factor)
        v.cx = wx - (sx - v.w / 2) / v.scale
        v.cy = wy + (sy - v.h / 2) / v.scale
        self._view_moved()

    def pan_pixels(self, dx: float, dy: float):
        v = self.view
        v.cx -= dx / v.scale
        v.cy += dy / v.scale
        self._view_moved(soft=True)

    def _view_moved(self, soft: bool = False):
        self.view_changed.emit()
        self.update()
        self._render_timer.start(45 if not soft else 120)

    def _rerender_now(self):
        self._dirty = True
        self.update()

    # ------------------------------------------------------------------ geometry helpers
    def snap_tol_world(self) -> float:
        return float(settings().get("snap_px")) / self.view.scale

    def snap_point(self, sx: float, sy: float):
        """-> (x, y, hit|None).  Applies snapping only if the active tool wants it."""
        x, y = self.view.to_world(sx, sy)
        if self.snap_enabled and self.tool is not None and self.tool.wants_snap:
            hit = self.state.spatial_index().snap(x, y, self.snap_tol_world(), tuple(self.snap_modes))
            skip = getattr(self.tool, "snap_exclude", None)
            if hit is not None and skip is not None and skip(hit):
                hit = None                      # e.g. the vertex being dragged must not snap to itself
            if hit is not None:
                return hit.x, hit.y, hit
        return x, y, None

    def prepare_surface_locator(self, surface, tin):
        """Build the TIN point locator in the background, once per surface.

        If the build fails, forget that we tried so the next hover retries it - otherwise the
        elevation readout would stay blank for the rest of the session with no explanation.
        """
        if surface.id in self._prep_started:
            return
        self._prep_started.add(surface.id)

        def work():
            try:
                tin.prepare()
            except Exception:
                self._prep_started.discard(surface.id)

        threading.Thread(target=work, daemon=True).start()

    def surface_z(self, x: float, y: float) -> float:
        s = self.state.active_surface()
        if s is None or s.tin().n_tris == 0:
            return math.nan
        tin = s.tin()
        if tin._finder is None:
            self.prepare_surface_locator(s, tin)
            return math.nan
        try:
            return float(tin.z_at(x, y))
        except Exception:
            return math.nan

    # ------------------------------------------------------------------ painting
    def resizeEvent(self, e):
        self.view.w, self.view.h = max(1, self.width()), max(1, self.height())
        if self._fit_pending and self.width() > 50:
            self._fit_pending = False
            self.zoom_extents()
        self._dirty = True
        super().resizeEvent(e)

    def _render_cache(self):
        v = self.view
        m = int(max(v.w, v.h) * self.OVERSCAN)
        W, H = v.w + 2 * m, v.h + 2 * m
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(W * dpr), int(H * dpr))
        pm.setDevicePixelRatio(dpr)
        rv = View(v.cx, v.cy, v.scale, W, H)
        p = QPainter(pm)
        try:
            self.renderer.paint(p, rv, dataclasses.replace(self.opts, show_scalebar=False))
        finally:
            p.end()
        self._pix, self._pix_view, self._pix_margin = pm, rv, m
        self._dirty = False
        if self.renderer.last_notes:
            self.hint_changed.emit(self.renderer.last_notes[0])

    def paintEvent(self, ev):
        if self._dirty or self._pix is None:
            self._render_cache()
        p = QPainter(self)
        v, cv, m = self.view, self._pix_view, self._pix_margin
        # place the cached pixmap according to how far the view has moved since it was rendered
        r = v.scale / cv.scale
        wx, wy = cv.to_world(0, 0)
        px, py = v.to_screen(wx, wy)
        if abs(r - 1.0) < 1e-12:
            p.drawPixmap(QPointF(px, py), self._pix)
        else:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(QRectF(px, py, cv.w * r, cv.h * r), self._pix, QRectF(self._pix.rect()))
        p.setRenderHint(QPainter.Antialiasing, True)
        if self.opts.show_scalebar:
            self.renderer.scalebar(p, v, self.state.project, theme.colors(), self.opts)
        self._paint_selection(p)
        if self.extra_overlay:
            self.extra_overlay(p, v)
        for fn in self.view_overlays:
            fn(p, v)
        if self.tool is not None:
            self.tool.paint(p, v)
        self._paint_snap(p)
        self._paint_imagery_status(p)
        p.end()

    def _paint_imagery_status(self, p):
        im = self.imagery
        notes = list(self.renderer.last_notes)
        if im.pending:
            notes.append(f"Loading imagery: {im.pending} tile(s)...")
        pr = self.state.project
        attr = ""
        for lay in pr.imagery.values():
            if lay.visible and lay.kind == "tiles":
                attr = lay.source.get("attribution", "")
        f = QFont()
        f.setPixelSize(11)
        p.setFont(f)
        y = 16
        for n in notes[:3]:
            p.setPen(QColor(theme.colors()["warn"]))
            p.drawText(QPointF(12, y), n)
            y += 14
        if attr and self.opts.show_imagery:
            w = p.fontMetrics().horizontalAdvance(attr)
            p.setPen(QColor(255, 255, 255, 230) if theme.current() == "dark" else QColor(40, 40, 40, 230))
            p.drawText(QPointF(self.width() - w - 8, self.height() - 6), attr)

    def _paint_selection(self, p: QPainter):
        st, v = self.state, self.view
        pr = st.project
        col = QColor(theme.colors()["select"])
        if st.sel_points:
            pts = [pr.points[i] for i in list(st.sel_points)[:20000] if i in pr.points]
            if pts:
                S = v.to_screen_arr(np.array([(q.x, q.y) for q in pts]))
                p.setPen(pen_for(col.getRgb()[:3], 1.8))
                p.setBrush(Qt.NoBrush)
                for sx, sy in S[:4000]:
                    if -20 < sx < v.w + 20 and -20 < sy < v.h + 20:
                        p.drawEllipse(QPointF(sx, sy), 8, 8)
        if st.sel_entities:
            ents = [pr.entities[i] for i in list(st.sel_entities)[:3000] if i in pr.entities]
            p.setBrush(Qt.NoBrush)
            from ..core import geometry as G
            from ..core.model import TextEntity
            for e in ents:
                if isinstance(e, Polyline):
                    f = G.flatten_polyline(e.verts, e.bulges, e.closed, max_step=math.radians(3))
                    if e.closed and len(f):
                        f = np.vstack([f, f[:1]])
                    S = v.to_screen_arr(f[:, :2])
                    codes = np.ones(len(S), np.int64)
                    codes[0] = 0
                    p.setPen(pen_for(col.getRgb()[:3], 3.0, alpha=110))
                    p.drawPath(make_path(S, codes))
                    p.setPen(pen_for(col.getRgb()[:3], 1.2, "DASHED"))
                    p.drawPath(make_path(S, codes))
                    if len(ents) == 1:
                        p.setPen(pen_for(col.getRgb()[:3], 1.4))
                        p.setBrush(QColor(0, 0, 0, 120))
                        for sx, sy in v.to_screen_arr(e.verts[:, :2]):
                            p.drawRect(QRectF(sx - 3.5, sy - 3.5, 7, 7))
                        p.setBrush(Qt.NoBrush)
                elif isinstance(e, TextEntity):
                    sx, sy = v.to_screen(e.x, e.y)
                    p.setPen(pen_for(col.getRgb()[:3], 1.4))
                    w = max(20, len(e.text) * e.height * v.scale * 0.6)
                    p.drawRect(QRectF(sx - 2, sy - e.height * v.scale - 2, w + 4, e.height * v.scale + 6))

    def _paint_snap(self, p: QPainter):
        h = self.snap_hit
        if h is None or self.tool is None or not self.tool.wants_snap:
            return
        sx, sy = self.view.to_screen(h.x, h.y)
        col = QColor(theme.colors()["snap"])
        p.setPen(pen_for(col.getRgb()[:3], 2.0))
        p.setBrush(Qt.NoBrush)
        s = 7
        if h.kind in ("point", "node"):
            p.drawRect(QRectF(sx - s, sy - s, 2 * s, 2 * s))
        elif h.kind == "mid":
            p.drawPolygon([QPointF(sx, sy - s), QPointF(sx + s, sy + s), QPointF(sx - s, sy + s)])
        else:
            p.drawLine(QPointF(sx - s, sy - s), QPointF(sx + s, sy + s))
            p.drawLine(QPointF(sx - s, sy + s), QPointF(sx + s, sy - s))
        f = QFont()
        f.setPixelSize(11)
        p.setFont(f)
        label = {"point": "Point", "node": "Vertex", "mid": "Midpoint", "nearest": "On line"}[h.kind]
        p.setPen(col)
        p.drawText(QPointF(sx + 11, sy - 9), label)

    # ------------------------------------------------------------------ mouse / keyboard
    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if d == 0:
            return
        pos = ev.position()
        self.zoom_at(pos.x(), pos.y(), 1.25 ** (d / 120.0))
        ev.accept()

    def _tool_event(self, name, ev):
        pos = ev.position()
        x, y, hit = self.snap_point(pos.x(), pos.y())
        self.snap_hit = hit
        getattr(self.tool, name)(ev, x, y, pos.x(), pos.y())

    def mousePressEvent(self, ev):
        self.setFocus()
        if ev.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last = ev.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if self.tool is None:
            return
        if ev.button() == Qt.LeftButton:
            self._tool_event("press", ev)
        elif ev.button() == Qt.RightButton:
            self.tool.right_click(ev)

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._panning:
            d = pos - self._pan_last
            self._pan_last = pos
            self.pan_pixels(d.x(), d.y())
            return
        x, y, hit = self.snap_point(pos.x(), pos.y())
        self.snap_hit = hit
        self.cursor_world = (x, y)
        self.cursor_moved.emit(x, y, self.surface_z(x, y))
        if self.tool is not None:
            self.tool.move(ev, x, y, pos.x(), pos.y())
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(self.tool.cursor if self.tool else Qt.ArrowCursor)
            self._render_timer.start(10)
            return
        if self.tool is not None and ev.button() == Qt.LeftButton:
            self._tool_event("release", ev)
            self.update()

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MiddleButton:
            self.zoom_extents()
        elif ev.button() == Qt.LeftButton and self.tool is not None:
            self._tool_event("double_click", ev)

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_Escape:
            # Give the active tool a chance to clear its transient state, then always
            # return the main view to Pan. Toolbar/panel focus is handled by MainWindow.
            if self.tool is not None:
                self.tool.key(ev)
            self.escape_pressed.emit()
            ev.accept()
            return
        if self.tool is not None and self.tool.key(ev):
            ev.accept()
            return
        step = 80
        if k == Qt.Key_Left:
            self.pan_pixels(step, 0)
        elif k == Qt.Key_Right:
            self.pan_pixels(-step, 0)
        elif k == Qt.Key_Up:
            self.pan_pixels(0, step)
        elif k == Qt.Key_Down:
            self.pan_pixels(0, -step)
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_at(self.width() / 2, self.height() / 2, 1.4)
        elif k == Qt.Key_Minus:
            self.zoom_at(self.width() / 2, self.height() / 2, 1 / 1.4)
        else:
            super().keyPressEvent(ev)

    def leaveEvent(self, ev):
        self.snap_hit = None
        self.update()

    def contextMenuEvent(self, ev):
        pass

    # ------------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if files:
            self.files_dropped.emit(files)

    # ------------------------------------------------------------------ export
    def render_image(self, width: int = 1600, bbox=None, transparent: bool = False, opts: DisplayOptions | None = None) -> QImage:
        """Render the drawing (or bbox) to an image - used for PNG export and report thumbnails."""
        pr = self.state.project
        bbox = bbox or pr.extents() or (0, 0, 100, 100)
        x0, y0, x1, y1 = bbox
        dx, dy = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        h = max(1, int(width * dy / dx))
        view = View(w=width, h=h)
        view.fit(bbox, 0.04)
        img = QImage(width, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent if transparent else QColor(theme.colors()["canvas"]))
        p = QPainter(img)
        self.renderer.paint(p, view, opts or self.opts)
        p.end()
        return img

    def grab_region(self, cx: float, cy: float, size_px: int = 220, scale: float | None = None, marks=()) -> QImage:
        """Render a small square view centred on (cx, cy) at the given scale, with optional marks
        [(x, y, kind, rgb)] drawn on top (kind: 'cross' | 'ring')."""
        view = View(cx, cy, scale or self.view.scale, size_px, size_px)
        img = QImage(size_px, size_px, QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(theme.colors()["canvas"]))
        p = QPainter(img)
        o = DisplayOptions(**{**self.opts.__dict__, "show_scalebar": False, "show_numbers": False,
                              "show_elev": False, "show_desc": False})
        self.renderer.paint(p, view, o)
        p.setRenderHint(QPainter.Antialiasing, True)
        for x, y, kind, rgb in marks:
            sx, sy = view.to_screen(x, y)
            p.setPen(pen_for(rgb, 2.0))
            if kind == "cross":
                p.drawLine(QPointF(sx - 11, sy), QPointF(sx + 11, sy))
                p.drawLine(QPointF(sx, sy - 11), QPointF(sx, sy + 11))
            else:
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(sx, sy), 7, 7)
        p.end()
        return img
