"""The 3D view: orbit / pan / zoom around the project's surface, points and linework.  View only - there are no
editing or selection tools here; select in the plan view and the selection lights up in 3D.

Rendering is core.scene3d (numpy + Agg).  While you drag, a cheaper level of detail is drawn; a moment after you let go
the full-quality image replaces it.
"""
from __future__ import annotations

import math
import time

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from ..core import scene3d as S
from . import theme

PRESETS = {"top": (0.0, 90.0), "front": (0.0, 0.0), "right": (270.0, 0.0), "back": (180.0, 0.0), "left": (90.0, 0.0),
           "iso": (30.0, 35.0)}


# ============================================================================ shared snapshot
class SceneProvider(QObject):
    """One snapshot of the project for both the 3D and the depth view, rebuilt only when somebody asks after a change."""
    changed = Signal()

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._scene: S.Scene | None = None
        self._key = None
        state.changed.connect(self.invalidate)
        state.project_replaced.connect(self.invalidate)

    def invalidate(self, *_):
        self._scene = None
        self.changed.emit()

    def scene(self) -> S.Scene:
        pr = self.state.project
        key = (id(pr), pr.revision, theme.current())
        if self._scene is None or self._key != key:
            try:
                self._scene = S.build_scene(pr, theme.display_color)
            except Exception as ex:                    # a damaged project must not stop the window from painting
                self._scene = S.Scene()
                self.state.log(f"The 3D / depth views could not read this project: {ex}", "error")
            self._key = key
        return self._scene


# ============================================================================ the drawing widget
class View3D(QWidget):
    camera_changed = Signal()

    def __init__(self, state, provider: SceneProvider, parent=None, *, auto_fit_vexag: bool = True):
        super().__init__(parent)
        self.state, self.provider = state, provider
        self.auto_fit_vexag = bool(auto_fit_vexag)
        self.cam = S.Camera(np.zeros(3), 30.0, 35.0, 1000.0, 40.0, True, 1.0)
        self.show_surface = self.show_points = self.show_lines = True
        self.show_wire = self.show_numbers = False
        self.error_point_ids: set[int] = set()
        self.hide_non_error_points: bool = False
        self.dim_non_error_points: bool = False
        self._img: QImage | None = None
        self._dirty = True
        self._drag = None
        self._interacting = False
        self._lod = 1.0                      # level-of-detail budget while moving: shrinks if frames are slow
        self._fitted_for = None
        self.last_render_ms = 0.0
        self.setMinimumSize(240, 200)
        self.setMouseTracking(False)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.OpenHandCursor)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._settled)
        provider.changed.connect(self.invalidate)
        state.selection_changed.connect(self.invalidate)

    # ------------------------------------------------------------------ state changes
    def invalidate(self):
        self._dirty = True
        self.update()

    def _settled(self):
        self._interacting = False
        self.invalidate()

    def _interact(self):
        self._interacting = True
        self._settle.start(140)
        self.invalidate()
        self.camera_changed.emit()

    def set_preset(self, name: str):
        az, el = PRESETS[name]
        self.cam.set_view(az, el)
        self.fit()

    def fit(self):
        sc = self.provider.scene()
        if sc.bounds is not None:
            self.cam.fit(sc.bounds, max(self.width(), 50), max(self.height(), 50))
        self.invalidate()
        self.camera_changed.emit()

    def set_vexag(self, v: float):
        self.cam.vexag = max(float(v), 0.01)
        self.invalidate()
        self.camera_changed.emit()

    def auto_vexag(self):
        sc = self.provider.scene()
        if sc.bounds is not None:
            self.cam.vexag = S.auto_vexag(sc.bounds)
        self.fit()

    def set_perspective(self, on: bool):
        self.cam.perspective = bool(on)
        self.invalidate()
        self.camera_changed.emit()

    def center_on_point(self, x: float, y: float, z: float = 0.0, distance: float | None = None):
        """Fix the 3D orbit camera target to a specific 3D point (unscaled world coordinates)."""
        self.cam.target = np.array([float(x), float(y), float(z)], float)
        if distance is not None:
            self.cam.distance = max(float(distance), 5.0)
        else:
            self.cam.distance = min(self.cam.distance, 150.0)
        self.invalidate()
        self.camera_changed.emit()

    def set_option(self, name: str, value: bool):
        setattr(self, name, bool(value))
        self.invalidate()

    # ------------------------------------------------------------------ rendering
    def _first_fit(self):
        """Frame a newly opened project once, optionally choosing readable exaggeration."""
        pid = id(self.state.project)
        sc = self.provider.scene()
        if self._fitted_for != pid and sc.bounds is not None and self.width() > 60:
            self._fitted_for = pid
            if self.auto_fit_vexag:
                self.cam.vexag = S.auto_vexag(sc.bounds)
            self.cam.fit(sc.bounds, self.width(), self.height())
            self.camera_changed.emit()

    def render_image(self, lod: bool = False) -> QImage:
        dpr = self.devicePixelRatioF()
        W, H = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
        sc = self.provider.scene()
        opts = S.RenderOptions(surface=self.show_surface, points=self.show_points, lines=self.show_lines, wire=self.show_wire,
                               point_px=6.0 * dpr, line_px=1.6 * dpr, dark=theme.current() == "dark",
                               max_tris=int(60000 * self._lod) if lod else 0, max_points=int(30000 * self._lod) if lod else 0,
                               max_segments=int(40000 * self._lod) if lod else 0,
                               selected=frozenset(self.state.sel_points), sel_rgb=QColor(theme.colors()["select"]).getRgb()[:3],
                               error_point_ids=frozenset(self.error_point_ids),
                               hide_non_error_points=getattr(self, "hide_non_error_points", False),
                               dim_non_error_points=getattr(self, "dim_non_error_points", False))
        t0 = time.perf_counter()
        buf = S.render_orbit(sc, self.cam, W, H, opts)
        dt = time.perf_counter() - t0
        self.last_render_ms = dt * 1000.0
        if lod:                                              # keep dragging near 15 fps on big data
            self._lod = float(np.clip(self._lod * (0.07 / max(dt, 1e-3)) ** 0.7, 0.06, 1.5))
        img = QImage(buf.data, W, H, W * 4, QImage.Format_RGBA8888).copy()
        img.setDevicePixelRatio(dpr)
        return img

    def paintEvent(self, ev):
        self._first_fit()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.colors()["canvas"]))
        sc = self.provider.scene()
        if sc.bounds is None:
            self._paint_note(p, "Nothing to show in 3D yet.\nImport points with elevations or build a surface.")
            p.end()
            return
        if self._dirty or self._img is None:
            self._img = self.render_image(self._interacting)
            self._dirty = False
        p.drawImage(0, 0, self._img)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self.show_numbers:
            self._paint_numbers(p, sc)
        self._paint_error_flags(p, sc)
        self._paint_gizmo(p)
        self._paint_readout(p, sc)
        p.end()

    def _paint_error_flags(self, p, sc):
        if not self.error_point_ids or not len(sc.pts_xyz) or not len(sc.pts_id):
            return
        W, H = self.width(), self.height()
        s = self.cam.project(sc.pts_xyz, W, H)
        ok = (s[:, 2] > self.cam.near) & np.isfinite(s[:, 0]) & (s[:, 0] > 0) & (s[:, 0] < W) & (s[:, 1] > 0) & (s[:, 1] < H)
        idx = np.nonzero(ok)[0]
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        pen_pole = QPen(QColor(255, 60, 60, 230), 2.0)
        for i in idx:
            pid = sc.pts_id[i]
            if pid in self.error_point_ids:
                sx, sy = s[i, 0], s[i, 1]
                p.setPen(pen_pole)
                p.drawLine(QPointF(sx, sy), QPointF(sx, sy - 20))
                p.setBrush(QColor(255, 60, 60, 230))
                p.setPen(QPen(QColor(255, 255, 255), 1.0))
                poly = [QPointF(sx, sy - 20), QPointF(sx + 10, sy - 15), QPointF(sx, sy - 10), QPointF(sx, sy - 20)]
                p.drawPolygon(poly)
        p.restore()

    def _paint_note(self, p, text):
        p.setPen(QColor(theme.colors()["dim"]))
        p.drawText(self.rect(), Qt.AlignCenter, text)

    def _paint_readout(self, p, sc):
        c = self.cam
        f = QFont()
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(theme.colors()["dim"]))
        lines = [f"Az {c.azimuth:05.1f}   El {c.elevation:04.1f}   Vertical x{c.vexag:g}   {'Perspective' if c.perspective else 'Orthographic'}"]
        if self._interacting and (any(m.n_tris > 60000 * self._lod for m in sc.meshes) or len(sc.pts_xyz) > 30000 * self._lod):
            lines.append("(simplified while moving)")
        if sc.pts_skipped:
            lines.append(f"{sc.pts_skipped:,} point(s) without an elevation are not shown")
        for i, t in enumerate(lines):
            p.drawText(QPointF(10, 16 + 14 * i), t)

    def _paint_gizmo(self, p):
        """A small compass: where north is on screen."""
        r, u, f = self.cam.basis()
        cx, cy, rad = self.width() - 40.0, 42.0, 24.0
        p.setPen(QPen(QColor(theme.colors()["dim"]), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), rad, rad)
        col = QColor(theme.colors()["text"])
        for name, vec, strong in (("N", (0, 1, 0), True), ("E", (1, 0, 0), False), ("S", (0, -1, 0), False), ("W", (-1, 0, 0), False)):
            v = np.array(vec, float)
            sx, sy = float(v @ r), -float(v @ u)               # screen direction (y down)
            n = math.hypot(sx, sy)
            if n < 0.12:                                       # pointing at / away from the viewer
                continue
            sx, sy = sx / n * min(1.0, n * 1.6), sy / n * min(1.0, n * 1.6)
            p.setPen(QPen(col if strong else QColor(theme.colors()["dim"]), 2.0 if strong else 1.0))
            p.drawLine(QPointF(cx, cy), QPointF(cx + sx * rad, cy + sy * rad))
            f = QFont()
            f.setPixelSize(11 if strong else 9)
            f.setBold(strong)
            p.setFont(f)
            p.drawText(QRectF(cx + sx * (rad + 9) - 7, cy + sy * (rad + 9) - 7, 14, 14), Qt.AlignCenter, name)

    def _paint_numbers(self, p, sc):
        if not len(sc.pts_xyz):
            return
        W, H = self.width(), self.height()
        s = self.cam.project(sc.pts_xyz, W, H)
        ok = (s[:, 2] > self.cam.near) & np.isfinite(s[:, 0]) & (s[:, 0] > 0) & (s[:, 0] < W) & (s[:, 1] > 0) & (s[:, 1] < H)
        idx = np.nonzero(ok)[0]
        if len(idx) > 220:                                     # label only the nearest ones
            idx = idx[np.argsort(s[idx, 2])[:220]]
        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(QColor(theme.colors()["text"]))
        for i in idx:
            p.drawText(QPointF(s[i, 0] + 5, s[i, 1] - 4), sc.pts_num[i])

    # ------------------------------------------------------------------ mouse / keyboard
    def mousePressEvent(self, ev):
        self.setFocus()
        b, m = ev.button(), ev.modifiers()
        if b == Qt.LeftButton and not (m & (Qt.ShiftModifier | Qt.ControlModifier)):
            self._drag = "orbit"
        elif (b == Qt.LeftButton and (m & Qt.ShiftModifier)) or b in (Qt.RightButton, Qt.MiddleButton):
            self._drag = "pan"
        elif (b == Qt.LeftButton and (m & Qt.ControlModifier)):
            self._drag = "zoom"
        else:
            return
        self._last = ev.position()
        self.setCursor(Qt.ClosedHandCursor if self._drag == "orbit" else (Qt.SizeVerCursor if self._drag == "zoom" else Qt.SizeAllCursor))

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return
        pos = ev.position()
        dx, dy = pos.x() - self._last.x(), pos.y() - self._last.y()
        self._last = pos
        if self._drag == "orbit":
            self.cam.orbit(-dx * 0.4, dy * 0.4)               # drag right: the scene turns clockwise; drag down: look from higher up
        elif self._drag == "zoom":
            self.cam.zoom(1.0 - dy * 0.01)
        else:
            self.cam.pan(dx, dy, self.height())
        self._interact()

    def mouseReleaseEvent(self, ev):
        if self._drag:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            self._settle.start(30)

    def mouseDoubleClickEvent(self, ev):
        self.fit()

    def wheelEvent(self, ev):
        d = ev.angleDelta().y() or ev.angleDelta().x() or ev.pixelDelta().y()
        if d:
            self.cam.zoom(1.18 ** (d / 120.0))
            self._interact()
        ev.accept()

    def keyPressEvent(self, ev):
        k = ev.key()
        step = 5.0
        if k == Qt.Key_Left:
            self.cam.orbit(step, 0)
        elif k == Qt.Key_Right:
            self.cam.orbit(-step, 0)
        elif k == Qt.Key_Up:
            self.cam.orbit(0, -step)
        elif k == Qt.Key_Down:
            self.cam.orbit(0, step)
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self.cam.zoom(1.3)
        elif k == Qt.Key_Minus:
            self.cam.zoom(1 / 1.3)
        elif k == Qt.Key_Home:
            self.fit()
            return
        else:
            super().keyPressEvent(ev)
            return
        self._interact()

    def resizeEvent(self, ev):
        self._dirty = True
        super().resizeEvent(ev)


# ============================================================================ the panel (controls + view)
def _btn(text, tip, slot, checkable=False):
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setAutoRaise(False)
    b.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    (b.toggled if checkable else b.clicked).connect(slot)
    return b


class View3DPanel(QWidget):
    def __init__(self, state, provider: SceneProvider, parent=None):
        super().__init__(parent)
        self.view = View3D(state, provider)
        v = self.view
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        row1 = QHBoxLayout()
        row1.setSpacing(3)
        row1.addWidget(QLabel("View:"))
        for name, tip in (("top", "Straight down, north up - the plan"), ("front", "Looking north from the south"),
                          ("right", "Looking west from the east"), ("back", "Looking south from the north"),
                          ("left", "Looking east from the west"), ("iso", "A 3D three-quarter view")):
            row1.addWidget(_btn(name.capitalize(), tip, lambda _=False, n=name: v.set_preset(n)))
        row1.addWidget(_btn("Fit", "Zoom to everything (double-click the view)", lambda _=False: v.fit()))
        row1.addStretch(1)
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.cmb_proj = QComboBox()
        self.cmb_proj.addItems(["Perspective", "Orthographic"])
        self.cmb_proj.currentIndexChanged.connect(lambda i: v.set_perspective(i == 0))
        row2.addWidget(self.cmb_proj)
        row2.addWidget(QLabel("Vertical x"))
        self.sp_vex = QDoubleSpinBox()
        self.sp_vex.setRange(0.1, 50.0)
        self.sp_vex.setDecimals(1)
        self.sp_vex.setSingleStep(0.5)
        self.sp_vex.setValue(1.0)
        self.sp_vex.setFixedWidth(72)
        self.sp_vex.setToolTip("Vertical exaggeration - a flat site needs several times to show its shape")
        self.sp_vex.valueChanged.connect(v.set_vexag)
        row2.addWidget(self.sp_vex)
        row2.addWidget(_btn("Auto", "Pick a readable exaggeration", lambda _=False: v.auto_vexag()))
        row2.addStretch(1)
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        for text, attr, on in (("Surface", "show_surface", True), ("Points", "show_points", True), ("Lines", "show_lines", True),
                               ("Mesh", "show_wire", False), ("Numbers", "show_numbers", False)):
            c = QCheckBox(text)
            c.setChecked(on)
            c.toggled.connect(lambda val, a=attr: v.set_option(a, val))
            row3.addWidget(c)
        row3.addStretch(1)
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addLayout(row3)
        lay.addWidget(v, 1)
        self.lbl_info = QLabel("")
        self.lbl_info.setProperty("hint", "true")
        hint = QLabel("Drag to orbit - Shift/right/middle drag to pan - wheel to zoom - double-click to fit")
        hint.setProperty("hint", "true")
        for lab in (self.lbl_info, hint):                          # long texts clip instead of forcing the dock wide
            lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(lab)
        v.camera_changed.connect(self._sync)
        provider.changed.connect(self._info)
        self._sync()
        self._info()

    def _sync(self):
        c = self.view.cam
        self.sp_vex.blockSignals(True)
        self.sp_vex.setValue(c.vexag)
        self.sp_vex.blockSignals(False)
        self.cmb_proj.blockSignals(True)
        self.cmb_proj.setCurrentIndex(0 if c.perspective else 1)
        self.cmb_proj.blockSignals(False)

    def _info(self):
        sc = self.view.provider.scene()
        self.lbl_info.setText(sc.summary())
