"""Canvas tools: select, pan, zoom window, draw polyline / arc, point, text, measure, move, depth line."""
from __future__ import annotations

import base64
import math
import re

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPolygonF
from PySide6.QtWidgets import QInputDialog

from ..core import cogo
from ..core import geometry as G
from ..core import reference as REF
from ..core import units as U
from ..core.model import Polyline, TextEntity
from ..core.settings import settings
from . import theme
from .render import make_path, pen_for


# ----------------------------------------------------------------------------- typed coordinates
def parse_coordinate(text: str, project, ref=None, order: str = "NE"):
    """'N,E' | 'E,N' (per order) | '@dN,dE' | '@dist<bearing' | '101' (a point number) -> (x, y)."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Type a coordinate first.")
    rel = t.startswith("@")
    if rel:
        t = t[1:].strip()
    if "<" in t:
        d, b = t.split("<", 1)
        try:
            dist = float(d)
        except ValueError:
            raise ValueError(f"'{d}' is not a distance.")
        az = cogo.parse_bearing(b)
        if az is None:
            raise ValueError(f"'{b}' is not a bearing or azimuth (try N45-30E or 135.5).")
        if ref is None:
            raise ValueError("A polar entry needs a start point - click one first.")
        return cogo.direct(ref[0], ref[1], az, dist)
    parts = [p for p in re.split(r"[,\s]+", t) if p]
    if len(parts) == 1 and not rel:
        pt = project.point_by_number(parts[0])
        if pt is None:
            raise ValueError(f"No point numbered '{parts[0]}'.")
        return pt.x, pt.y
    if len(parts) == 2:
        try:
            a, b = float(parts[0].replace(",", "")), float(parts[1])
        except ValueError:
            raise ValueError("Coordinates must be two numbers.")
        x, y = (b, a) if order == "NE" else (a, b)
        if rel:
            if ref is None:
                raise ValueError("A relative entry needs a start point - click one first.")
            return ref[0] + x, ref[1] + y
        return x, y
    raise ValueError("Use  N,E   @dN,dE   @distance<bearing   or a point number.")


def _finite(v) -> bool:
    return v is not None and isinstance(v, (int, float)) and math.isfinite(v)


# ----------------------------------------------------------------------------- base
class Tool:
    name = "tool"
    label = "Tool"
    cursor = Qt.CrossCursor
    wants_snap = False

    def __init__(self, canvas):
        self.c = canvas
        self.state = canvas.state

    def activate(self): ...
    def deactivate(self): ...
    def hint(self) -> str: return ""
    def press(self, ev, x, y, sx, sy): ...
    def move(self, ev, x, y, sx, sy): ...
    def release(self, ev, x, y, sx, sy): ...
    def double_click(self, ev, x, y, sx, sy): ...
    def right_click(self, ev): ...
    def key(self, ev) -> bool: return False
    def paint(self, p: QPainter, view): ...
    def cancel(self): ...
    def enter_coordinate(self, x: float, y: float): ...

    def say(self, text: str):
        self.c.hint_changed.emit(text)

    def last_point(self):
        return None


# ----------------------------------------------------------------------------- pan
class PanTool(Tool):
    """Drag the drawing.  The resting state of the program.

    Escape from any other tool lands here (see ``CanvasView.keyPressEvent``): the first Esc
    puts down whatever is being picked, the drawing is then in a known state, and dragging
    moves the view instead of selecting something the user did not mean to touch.
    """

    name = "pan"
    label = "Pan"
    cursor = Qt.OpenHandCursor

    def __init__(self, canvas):
        super().__init__(canvas)
        self._last = None

    def hint(self):
        return "Drag to pan the view  -  Esc puts any tool down and comes back here"

    def press(self, ev, x, y, sx, sy):
        self._last = (sx, sy)
        self.c.setCursor(Qt.ClosedHandCursor)

    def move(self, ev, x, y, sx, sy):
        if self._last is None:
            return
        dx, dy = sx - self._last[0], sy - self._last[1]
        self._last = (sx, sy)
        self.c.pan_pixels(dx, dy)

    def release(self, ev, x, y, sx, sy):
        self._last = None
        self.c.setCursor(self.cursor)

    def cancel(self):
        self._last = None


# ----------------------------------------------------------------------------- imagery nudge
class ImageryNudgeTool(Tool):
    """Nudge one imagery layer from image-to-target point pairs picked in the plan view.

    Each pair begins at a feature in the displayed imagery, then clicks where that feature should
    land (usually a survey point). The first click is never snapped; the target click follows the
    canvas's current snap setting. Extra pairs are averaged to reduce the effect of a loose click.
    Only the imagery layer's display offset changes; survey coordinates are left alone.
    """

    name = "imagery_nudge"
    label = "Nudge Imagery by Points"
    cursor = Qt.CrossCursor

    def __init__(self, canvas, layer_id: int, on_finish=None, on_deactivate=None):
        super().__init__(canvas)
        self.layer_id = int(layer_id)
        layer = self.state.project.imagery.get(self.layer_id)
        self.initial_nudge = tuple(layer.nudge) if layer is not None else (0.0, 0.0)
        self.pairs: list[tuple[float, float, float, float]] = []
        self.pending_image = None        # (x, y) awaiting its target location
        self._done = False
        self._closed_notified = False
        self._on_finish = on_finish
        self._on_deactivate = on_deactivate

    @property
    def wants_snap(self) -> bool:
        # The image location must be raw; let the user snap only the target click.
        return self.pending_image is not None

    def hint(self) -> str:
        if self.pending_image is not None:
            return ("Image point picked. Click where it should land (snap is optional); "
                    "then repeat for more pairs or press Enter/right-click to apply.")
        n = len(self.pairs)
        prefix = f"{n} point pair(s) ready. " if n else ""
        return (prefix + "Click an image feature, then its target location. "
                "Press Enter/right-click to apply the average nudge; Esc cancels.")

    def press(self, ev, x, y, sx, sy):
        if self._done:
            return
        if self.pending_image is None:
            self.pending_image = (float(x), float(y))
            self.say(self.hint())
            self.c.update()
            return

        image_x, image_y = self.pending_image
        self.pairs.append((image_x, image_y, float(x), float(y)))
        self.pending_image = None
        self.say(self.hint())
        self.c.update()

    def right_click(self, ev):
        self.finish()

    def key(self, ev) -> bool:
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish()
            return True
        if ev.key() == Qt.Key_Escape:
            self.cancel()
            return True
        return False

    def finish(self):
        if self._done:
            return
        if self.pending_image is not None:
            self.pending_image = None
            self.say("Incomplete point pair discarded. Click another image feature, or press Enter again to apply.")
            self.c.update()
            return

        layer = self.state.project.imagery.get(self.layer_id)
        if not self.pairs or layer is None:
            self.cancel()
            if self._on_finish is not None:
                self._on_finish(False)
            return

        dx = float(np.mean([target_x - image_x for image_x, _image_y, target_x, _target_y in self.pairs]))
        dy = float(np.mean([target_y - image_y for _image_x, image_y, _target_x, target_y in self.pairs]))
        new_nudge = (self.initial_nudge[0] + dx, self.initial_nudge[1] + dy)
        self._done = True
        with self.state.edit(f"Nudge imagery by {len(self.pairs)} point pair(s)", kinds=("imagery",)):
            layer = self.state.project.imagery.get(self.layer_id)
            if layer is not None:
                layer.nudge = new_nudge
        self.state.log(f"Imagery nudged by {dx:,.3f} east, {dy:,.3f} north from "
                       f"{len(self.pairs)} point pair(s). Survey points were not moved.", "ok")
        if self._on_finish is not None:
            self._on_finish(True)

    def cancel(self):
        self._done = True
        self.pending_image = None
        self.pairs.clear()
        self.c.update()

    def deactivate(self):
        if not self._done:
            self.cancel()
        if not self._closed_notified and self._on_deactivate is not None:
            self._closed_notified = True
            self._on_deactivate()

    def paint(self, painter: QPainter, view):
        if not self.pairs and self.pending_image is None:
            return
        from PySide6.QtGui import QPen
        col = QColor(theme.colors()["accent"])
        pen = QPen(col, 1.5, Qt.DashLine)
        painter.save()
        painter.setPen(pen)
        for image_x, image_y, target_x, target_y in self.pairs:
            ix, iy = view.to_screen(image_x, image_y)
            tx, ty = view.to_screen(target_x, target_y)
            painter.drawLine(int(ix), int(iy), int(tx), int(ty))
            painter.drawEllipse(QPointF(ix, iy), 4.0, 4.0)
            painter.drawEllipse(QPointF(tx, ty), 5.0, 5.0)
        if self.pending_image is not None:
            x, y = self.pending_image
            sx, sy = view.to_screen(x, y)
            painter.drawEllipse(QPointF(sx, sy), 7.0, 7.0)
        painter.restore()


# ----------------------------------------------------------------------------- select
class SelectTool(Tool):
    name = "select"
    label = "Select"
    cursor = Qt.ArrowCursor

    def __init__(self, canvas):
        super().__init__(canvas)
        self._press = None
        self._drag = False
        self._rect = None
        self._grip = None
        self._grip_pos = None
        self.hover = None

    @property
    def wants_snap(self):
        return self._grip is not None

    def snap_exclude(self, hit) -> bool:
        g = self._grip
        return bool(g and hit.kind == "node" and hit.ref and len(hit.ref) > 2 and hit.ref[1] == g[0] and hit.ref[2] == g[1])

    def hint(self):
        return "Click to select - drag right for window, left for crossing - Shift adds, Ctrl toggles - drag a vertex grip to edit - Del deletes"

    def _grips(self):
        st = self.state
        if len(st.sel_entities) == 1 and not st.sel_points:
            e = st.project.entities.get(next(iter(st.sel_entities)))
            if isinstance(e, Polyline):
                return e
        return None

    def _grip_at(self, sx, sy):
        e = self._grips()
        if e is None:
            return None
        S = self.c.view.to_screen_arr(e.verts[:, :2])
        d = np.hypot(S[:, 0] - sx, S[:, 1] - sy)
        i = int(np.argmin(d))
        return (e.id, i) if d[i] <= 8 else None

    def press(self, ev, x, y, sx, sy):
        self._press = (sx, sy, x, y)
        self._drag = False
        self._rect = None
        self._grip = self._grip_at(sx, sy)
        self._grip_pos = None

    def move(self, ev, x, y, sx, sy):
        if self._press is None:
            idx = self.state.spatial_index()
            tol = self.c.snap_tol_world()
            self.hover = idx.pick(x, y, tol)
            return
        px, py = self._press[0], self._press[1]
        if self._grip is not None:
            self._grip_pos = (x, y)
            if self._drag or math.hypot(sx - px, sy - py) > 3:
                self._drag = True
            return
        if self._drag or math.hypot(sx - px, sy - py) > 4:
            self._drag = True
            self._rect = (px, py, sx, sy)

    def release(self, ev, x, y, sx, sy):
        if self._press is None:
            return
        px, py, wx, wy = self._press
        mods = ev.modifiers()
        mode = "add" if mods & Qt.ShiftModifier else "toggle" if mods & Qt.ControlModifier else "replace"
        grip, self._grip, self._press = self._grip, None, None
        if grip is not None and self._drag:
            eid, i = grip
            hit = self.c.snap_hit
            with self.state.edit("Move vertex"):
                e = self.state.project.entities[eid]
                e.verts[i, 0], e.verts[i, 1] = x, y
                if hit is not None and _finite(hit.z):
                    e.verts[i, 2] = hit.z
            self._rect = None
            return
        if self._drag:
            x0, y0 = self.c.view.to_world(px, py)
            crossing = sx < px
            pts, ents = self.state.spatial_index().select_box(x0, y0, x, y, crossing)
            self.state.select(pts, ents, mode)
            self._rect = None
            self._drag = False
            n = len(pts) + len(ents)
            self.say(f"{n} object(s) selected ({'crossing' if crossing else 'window'})")
            return
        hit = self.state.spatial_index().pick(wx, wy, self.c.snap_tol_world())
        if hit is None:
            if mode == "replace":
                self.state.clear_selection()
                self.say("Nothing selected")
            return
        kind, oid = hit
        if kind == "pt":
            self.state.select(points=[oid], mode=mode)
        else:
            self.state.select(entities=[oid], mode=mode)

    def double_click(self, ev, x, y, sx, sy):
        """Double-click a straight segment of the selected polyline to insert a vertex."""
        e = self._grips()
        if e is None:
            return
        v = e.verts
        n = len(v)
        last = n if e.closed else n - 1
        best = None
        for i in range(last):
            j = (i + 1) % n
            if e.bulges is not None and abs(e.bulges[i]) > 1e-12:
                continue
            d, t, qx, qy = G.closest_on_segments(x, y, v[i:i + 1, :2], v[j:j + 1, :2])
            if best is None or d[0] < best[0]:
                best = (float(d[0]), i, float(qx[0]), float(qy[0]), float(t[0]))
        if best and best[0] <= self.c.snap_tol_world():
            _, i, qx, qy, t = best
            j = (i + 1) % n
            z = v[i, 2] + t * (v[j, 2] - v[i, 2]) if (_finite(v[i, 2]) and _finite(v[j, 2])) else math.nan
            with self.state.edit("Insert vertex"):
                ent = self.state.project.entities[e.id]
                ent.verts = np.insert(ent.verts, i + 1, [qx, qy, z], axis=0)
                if ent.bulges is not None:
                    ent.bulges = np.insert(ent.bulges, i + 1, 0.0)

    def key(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.state.clear_selection()
            self._rect = self._press = self._grip = None
            return True
        return False

    def paint(self, p, view):
        col = QColor(theme.colors()["select"])
        if self._rect:
            x0, y0, x1, y1 = self._rect
            crossing = x1 < x0
            c = QColor(70, 220, 90) if crossing else col
            c.setAlpha(55)
            p.setBrush(c)
            p.setPen(pen_for((c.red(), c.green(), c.blue()), 1.2, "DASHED" if crossing else "CONTINUOUS"))
            p.drawRect(QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))
        if self.hover and self._press is None:
            kind, oid = self.hover
            p.setBrush(Qt.NoBrush)
            pr = self.state.project
            if kind == "pt" and oid in pr.points:
                q = pr.points[oid]
                sx, sy = view.to_screen(q.x, q.y)
                p.setPen(pen_for(col.getRgb()[:3], 1.2, alpha=170))
                p.drawEllipse(QPointF(sx, sy), 6, 6)
            elif oid in pr.entities and isinstance(pr.entities[oid], Polyline) and oid not in self.state.sel_entities:
                e = pr.entities[oid]
                f = G.flatten_polyline(e.verts, e.bulges, e.closed, max_step=math.radians(3))
                if e.closed and len(f):
                    f = np.vstack([f, f[:1]])
                S = view.to_screen_arr(f[:, :2])
                codes = np.ones(len(S), np.int64)
                codes[0] = 0
                p.setPen(pen_for(col.getRgb()[:3], 2.4, alpha=120))
                p.drawPath(make_path(S, codes))
        if self._grip is not None and self._grip_pos is not None:
            sx, sy = view.to_screen(*self._grip_pos)
            p.setPen(pen_for((255, 212, 0), 1.6))
            p.setBrush(QColor(255, 212, 0, 90))
            p.drawRect(QRectF(sx - 5, sy - 5, 10, 10))


# ----------------------------------------------------------------------------- zoom window
class ZoomWindowTool(Tool):
    name = "zoom_window"
    label = "Zoom window"

    def __init__(self, canvas):
        super().__init__(canvas)
        self._a = None
        self._b = None

    def hint(self):
        return "Drag a rectangle to zoom to it"

    def press(self, ev, x, y, sx, sy):
        self._a = (sx, sy)
        self._b = (sx, sy)

    def move(self, ev, x, y, sx, sy):
        if self._a:
            self._b = (sx, sy)

    def release(self, ev, x, y, sx, sy):
        if self._a is None:
            return
        a, b = self._a, (sx, sy)
        self._a = self._b = None
        if abs(a[0] - b[0]) < 6 or abs(a[1] - b[1]) < 6:
            self.c.zoom_at(sx, sy, 2.0)
            return
        x0, y0 = self.c.view.to_world(*a)
        x1, y1 = self.c.view.to_world(*b)
        self.c.zoom_bbox((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), margin=0.0)

    def paint(self, p, view):
        if self._a and self._b:
            (x0, y0), (x1, y1) = self._a, self._b
            p.setBrush(QColor(255, 255, 255, 25))
            p.setPen(pen_for((255, 255, 255), 1.0, "DASHED"))
            p.drawRect(QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))


# ----------------------------------------------------------------------------- drawing
class DrawPolylineTool(Tool):
    name = "polyline"
    label = "Polyline"
    wants_snap = True

    def __init__(self, canvas):
        super().__init__(canvas)
        self.verts: list = []
        self.cursor_xy = None

    def hint(self):
        return ("Click vertices - type N,E / @dist<bearing in the command box - Enter or right-click finishes - "
                "C closes - U undoes a vertex - Esc cancels")

    def last_point(self):
        return (self.verts[-1][0], self.verts[-1][1]) if self.verts else None

    def _add(self, x, y, z=math.nan):
        if self.verts and abs(self.verts[-1][0] - x) < 1e-9 and abs(self.verts[-1][1] - y) < 1e-9:
            return
        self.verts.append((x, y, z))

    def press(self, ev, x, y, sx, sy):
        hit = self.c.snap_hit
        self._add(x, y, hit.z if hit is not None and _finite(hit.z) else math.nan)

    def enter_coordinate(self, x, y):
        self._add(x, y)
        self.c.update()

    def move(self, ev, x, y, sx, sy):
        self.cursor_xy = (x, y)

    def right_click(self, ev):
        self.finish()

    def double_click(self, ev, x, y, sx, sy):
        self.finish()

    def key(self, ev):
        k = ev.key()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self.finish()
        elif k == Qt.Key_Escape:
            self.cancel()
        elif k in (Qt.Key_U, Qt.Key_Backspace):
            if self.verts:
                self.verts.pop()
                self.c.update()
        elif k == Qt.Key_C:
            self.finish(closed=True)
        else:
            return False
        return True

    def cancel(self):
        self.verts = []
        self.c.update()

    def finish(self, closed: bool = False):
        v = self.verts
        self.verts = []
        if len(v) < 2:
            self.c.update()
            return
        arr = np.array(v, float)
        if closed and len(arr) < 3:
            closed = False
        with self.state.edit("Draw polyline"):
            self.state.project.ensure_layer(self.c.current_layer)
            e = self.state.project.add_polyline(arr, self.c.current_layer, closed=closed)
        self.state.select(entities=[e.id])
        self.say(f"Polyline added ({len(arr)} vertices, length {G.polyline_length(arr, None, closed):,.3f})")
        self.c.update()

    def paint(self, p, view):
        if not self.verts:
            return
        pts = [view.to_screen(x, y) for x, y, _ in self.verts]
        col = (255, 212, 0)
        p.setPen(pen_for(col, 1.8))
        p.setBrush(Qt.NoBrush)
        S = np.array(pts)
        codes = np.ones(len(S), np.int64)
        codes[0] = 0
        p.drawPath(make_path(S, codes))
        for sx, sy in pts:
            p.drawRect(QRectF(sx - 3, sy - 3, 6, 6))
        if self.cursor_xy:
            cx, cy = view.to_screen(*self.cursor_xy)
            p.setPen(pen_for(col, 1.2, "DASHED"))
            p.drawLine(QPointF(*pts[-1]), QPointF(cx, cy))
            az, d = cogo.inverse(self.verts[-1][0], self.verts[-1][1], *self.cursor_xy)
            f = QFont()
            f.setPixelSize(12)
            p.setFont(f)
            p.setPen(QColor(*col))
            s = settings()
            p.drawText(QPointF(cx + 14, cy + 20),
                       f"{d:,.3f}  {cogo.format_angle(az, s.get('angle_format'), s.get('angle_dms'), s.get('angle_decimals'))}")


def arc_bulge_from_3pts(s, m, e) -> float | None:
    """DXF bulge for the circular arc through start s, mid m, end e (None when collinear)."""
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    a_s = math.atan2(ay - uy, ax - ux)
    a_m = math.atan2(by - uy, bx - ux)
    a_e = math.atan2(cy - uy, cx - ux)
    ccw = (a_e - a_s) % (2 * math.pi)
    if (a_m - a_s) % (2 * math.pi) < ccw:
        return math.tan(ccw / 4.0)
    return -math.tan((2 * math.pi - ccw) / 4.0)


class ArcTool(Tool):
    name = "arc"
    label = "Arc (3 point)"
    wants_snap = True

    def __init__(self, canvas):
        super().__init__(canvas)
        self.pts: list = []
        self.cursor_xy = None

    def hint(self):
        return "Click the arc start, a point on the arc, then the end - Esc cancels"

    def last_point(self):
        return self.pts[-1][:2] if self.pts else None

    def _add(self, x, y, z=math.nan):
        self.pts.append((x, y, z))
        if len(self.pts) == 3:
            self._commit()

    def press(self, ev, x, y, sx, sy):
        hit = self.c.snap_hit
        self._add(x, y, hit.z if hit is not None and _finite(hit.z) else math.nan)

    def enter_coordinate(self, x, y):
        self._add(x, y)

    def move(self, ev, x, y, sx, sy):
        self.cursor_xy = (x, y)

    def key(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.cancel()
            return True
        return False

    def right_click(self, ev):
        self.cancel()

    def cancel(self):
        self.pts = []
        self.c.update()

    def _commit(self):
        s, m, e = self.pts
        self.pts = []
        b = arc_bulge_from_3pts(s[:2], m[:2], e[:2])
        with self.state.edit("Draw arc"):
            self.state.project.ensure_layer(self.c.current_layer)
            if b is None:
                ent = self.state.project.add_polyline([s, e], self.c.current_layer)
            else:
                ent = self.state.project.add_polyline([s, e], self.c.current_layer, bulges=[b, 0.0])
        self.state.select(entities=[ent.id])
        self.c.update()

    def paint(self, p, view):
        if not self.pts:
            return
        col = (255, 212, 0)
        pts = list(self.pts) + ([(*self.cursor_xy, math.nan)] if self.cursor_xy else [])
        S = [view.to_screen(x, y) for x, y, _ in pts]
        p.setPen(pen_for(col, 1.4, "DASHED"))
        for a, b in zip(S[:-1], S[1:]):
            p.drawLine(QPointF(*a), QPointF(*b))
        if len(pts) == 3:
            b = arc_bulge_from_3pts(pts[0][:2], pts[1][:2], pts[2][:2])
            if b is not None:
                v = np.array([[pts[0][0], pts[0][1], 0], [pts[2][0], pts[2][1], 0]])
                f = G.flatten_polyline(v, np.array([b, 0.0]), False, max_step=math.radians(2))
                Sf = view.to_screen_arr(f[:, :2])
                codes = np.ones(len(Sf), np.int64)
                codes[0] = 0
                p.setPen(pen_for(col, 2.0))
                p.drawPath(make_path(Sf, codes))
        for sx, sy in S[:len(self.pts)]:
            p.drawRect(QRectF(sx - 3, sy - 3, 6, 6))


class PointTool(Tool):
    name = "point"
    label = "Point"
    wants_snap = True

    def hint(self):
        role = getattr(self.c, "point_role", "")
        what = f"{REF.ROLE_LABELS[role].lower()} reference point" if role else "survey point"
        return f"Click to place a {what} (description comes from the toolbar box) - Esc to stop"

    def press(self, ev, x, y, sx, sy):
        hit = self.c.snap_hit
        z = hit.z if hit is not None and _finite(hit.z) else math.nan
        self._place(x, y, z)

    def enter_coordinate(self, x, y):
        self._place(x, y, math.nan)

    def _place(self, x, y, z):
        pr = self.state.project
        role = getattr(self.c, "point_role", "")
        if role:
            # A reference point by hand - a stake-out pin dropped on the design, a control point
            # keyed in from a county sheet.  It gets the role's layer and the role's own point
            # numbers, and it never takes part in the fieldwork checks.
            with self.state.edit(f"Add {REF.ROLE_LABELS[role].lower()} point"):
                REF.ensure_layers(pr)
                p = REF.add_one(pr, x, y, z, number=REF.next_number(pr, role),
                                desc=self.c.point_desc, role=role)
            self.say(f"{REF.ROLE_LABELS[role]} point {p.number} added")
        else:
            with self.state.edit("Add point"):
                p = pr.add_point(x, y, z, desc=self.c.point_desc)
                if self.c.point_desc:
                    pr.apply_codes_to_points([p])
            self.say(f"Point {p.number} added")
        self.c.update()

    def key(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.c.hint_changed.emit("")
            return True
        return False


class TextTool(Tool):
    name = "text"
    label = "Text"

    def hint(self):
        return "Click where the text should go"

    def press(self, ev, x, y, sx, sy):
        text, ok = QInputDialog.getText(self.c, "Add text", "Text:")
        if not ok or not text.strip():
            return
        pr = self.state.project
        h = float(pr.settings.get("text_height", 2.0))
        with self.state.edit("Add text"):
            pr.ensure_layer("TEXT", (255, 255, 255))
            e = pr.add_text(x, y, text.strip(), h, 0.0, "TEXT")
        self.state.select(entities=[e.id])


# ----------------------------------------------------------------------------- measure
class MeasureTool(Tool):
    name = "measure"
    label = "Measure"
    wants_snap = True

    def __init__(self, canvas, area: bool = False):
        super().__init__(canvas)
        self.area = area
        self.pts: list = []
        self.cursor_xy = None
        self.done = False
        self.result = ""
        self.name = "area" if area else "measure"

    def hint(self):
        return ("Click corners; Enter/right-click finishes and reports - Esc clears" if self.area else
                "Click points: distance, bearing and running total - right-click or Esc to clear")

    def last_point(self):
        return self.pts[-1] if self.pts else None

    def press(self, ev, x, y, sx, sy):
        if self.done:
            self.pts, self.done, self.result = [], False, ""
        self.pts.append((x, y))
        self._update_text()

    def enter_coordinate(self, x, y):
        self.pts.append((x, y))
        self._update_text()

    def move(self, ev, x, y, sx, sy):
        self.cursor_xy = (x, y)

    def right_click(self, ev):
        if self.area and len(self.pts) >= 3:
            self.done = True
            self._update_text()
        else:
            self.cancel()

    def key(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and self.area and len(self.pts) >= 3:
            self.done = True
            self._update_text()
            return True
        if ev.key() == Qt.Key_Escape:
            self.cancel()
            return True
        if ev.key() in (Qt.Key_U, Qt.Key_Backspace) and self.pts:
            self.pts.pop()
            self._update_text()
            return True
        return False

    def cancel(self):
        self.pts, self.done, self.result = [], False, ""
        self.say("")
        self.c.update()

    def _fmt(self, az):
        s = settings()
        return cogo.format_angle(az, s.get("angle_format"), s.get("angle_dms"), s.get("angle_decimals"))

    def _update_text(self):
        pr = self.state.project
        u = U.LABEL.get(pr.h_unit, pr.h_unit)
        if len(self.pts) >= 2 and not self.area:
            az, d = cogo.inverse(*self.pts[-2], *self.pts[-1])
            tot = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(self.pts[:-1], self.pts[1:]))
            self.result = f"Segment {d:,.3f} {u}  {self._fmt(az)}   |   Total {tot:,.3f} {u}"
        elif self.area and len(self.pts) >= 3:
            v = np.column_stack([np.array(self.pts), np.zeros(len(self.pts))])
            a = G.polygon_area(v)
            per = G.polyline_length(v, None, True)
            self.result = (f"Area {a:,.2f} sq {pr.h_unit}  =  {U.area_to_acres(a, pr.h_unit):,.4f} acres   |   "
                           f"Perimeter {per:,.3f} {u}")
        else:
            self.result = ""
        self.say(self.result)
        self.c.update()

    def paint(self, p, view):
        if not self.pts:
            return
        S = [view.to_screen(x, y) for x, y in self.pts]
        pts = S + ([view.to_screen(*self.cursor_xy)] if self.cursor_xy and not self.done else [])
        col = (0, 230, 255)
        poly = QPolygonF([QPointF(*s) for s in pts])
        if self.area and len(pts) >= 3:
            p.setBrush(QColor(0, 230, 255, 40))
            p.setPen(pen_for(col, 1.8))
            p.drawPolygon(poly)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(pen_for(col, 1.8))
            p.drawPolyline(poly)
        for sx, sy in S:
            p.drawRect(QRectF(sx - 3, sy - 3, 6, 6))
        if self.result:
            f = QFont()
            f.setPixelSize(12)
            f.setBold(True)
            p.setFont(f)
            tx, ty = pts[-1]
            w = p.fontMetrics().horizontalAdvance(self.result)
            p.setBrush(QColor(0, 0, 0, 170))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(tx + 12, ty + 6, w + 14, 22), 4, 4)
            p.setPen(QColor(*col))
            p.drawText(QPointF(tx + 19, ty + 21), self.result)


# ----------------------------------------------------------------------------- move
class MoveTool(Tool):
    name = "move"
    label = "Move"
    wants_snap = True

    def __init__(self, canvas):
        super().__init__(canvas)
        self.base = None
        self.cursor_xy = None

    def hint(self):
        if not (self.state.sel_points or self.state.sel_entities):
            return "Select objects first (Select tool), then choose Move"
        return "Click a base point, then the destination (or type @dN,dE) - Esc cancels"

    def last_point(self):
        return self.base

    def press(self, ev, x, y, sx, sy):
        self.enter_coordinate(x, y)

    def enter_coordinate(self, x, y):
        if not (self.state.sel_points or self.state.sel_entities):
            self.say("Nothing is selected.")
            return
        if self.base is None:
            self.base = (x, y)
            self.say("Now click the destination")
            return
        dx, dy = x - self.base[0], y - self.base[1]
        self.base = None
        pr = self.state.project
        with self.state.edit("Move"):
            for i in self.state.sel_points:
                if i in pr.points:
                    pr.points[i].x += dx
                    pr.points[i].y += dy
            for i in self.state.sel_entities:
                e = pr.entities.get(i)
                if isinstance(e, Polyline):
                    e.verts[:, 0] += dx
                    e.verts[:, 1] += dy
                elif e is not None:
                    e.x += dx
                    e.y += dy
        self.say(f"Moved by dE {dx:,.3f}, dN {dy:,.3f}")

    def move(self, ev, x, y, sx, sy):
        self.cursor_xy = (x, y)

    def key(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.base = None
            self.c.update()
            return True
        return False

    def right_click(self, ev):
        self.base = None
        self.c.update()

    def paint(self, p, view):
        if self.base and self.cursor_xy:
            a, b = view.to_screen(*self.base), view.to_screen(*self.cursor_xy)
            p.setPen(pen_for((255, 212, 0), 1.4, "DASHED"))
            p.drawLine(QPointF(*a), QPointF(*b))
            p.drawRect(QRectF(a[0] - 4, a[1] - 4, 8, 8))


# ----------------------------------------------------------------------------- depth line
class DepthLineTool(Tool):
    """Two clicks on the plan view: the depth view will look across that line, towards its LEFT-hand side."""
    name = "depth_line"
    label = "Depth line"
    wants_snap = True

    def __init__(self, canvas, on_pick):
        super().__init__(canvas)
        self.on_pick = on_pick
        self.a = None
        self.cursor_xy = None

    def hint(self):
        return ("Click the start and the end of the line the depth view should look across - it looks to the LEFT of the line "
                "(draw it the other way to look the other way) - Esc cancels")

    def activate(self):
        self.a = None

    def last_point(self):
        return self.a

    def _point(self, x, y):
        if self.a is None:
            self.a = (x, y)
        else:
            a, self.a = self.a, None
            if math.hypot(x - a[0], y - a[1]) > 1e-9:
                self.on_pick(a, (x, y))
        self.c.update()

    def press(self, ev, x, y, sx, sy):
        self._point(x, y)

    def enter_coordinate(self, x, y):
        self._point(x, y)

    def move(self, ev, x, y, sx, sy):
        self.cursor_xy = (x, y)

    def right_click(self, ev):
        self.cancel()

    def key(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.cancel()
            return True
        return False

    def cancel(self):
        self.a = None
        self.say("")
        self.c.update()

    def paint(self, p, view):
        if self.a is None or self.cursor_xy is None:
            return
        col = (255, 196, 0)
        A, B = view.to_screen(*self.a), view.to_screen(*self.cursor_xy)
        p.setBrush(Qt.NoBrush)
        p.setPen(pen_for(col, 2.0))
        p.drawLine(QPointF(*A), QPointF(*B))
        dx, dy = B[0] - A[0], B[1] - A[1]
        n = math.hypot(dx, dy)
        if n > 14:                                            # an arrow to the left of the line: where the view will look
            lx, ly = dy / n, -dx / n
            mx, my = (A[0] + B[0]) / 2, (A[1] + B[1]) / 2
            tip = QPointF(mx + lx * 30, my + ly * 30)
            p.drawLine(QPointF(mx, my), tip)
            p.drawLine(tip, QPointF(tip.x() - lx * 9 + ly * 6, tip.y() - ly * 9 - lx * 6))
            p.drawLine(tip, QPointF(tip.x() - lx * 9 - ly * 6, tip.y() - ly * 9 + lx * 6))
