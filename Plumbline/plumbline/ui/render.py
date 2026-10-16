"""Scene rendering: world->screen maths, cached geometry, fast Qt paths, imagery tiles, labels.

Everything here paints onto any QPainter (the live canvas, a thumbnail, a PNG/PDF plot).
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QByteArray, QDataStream, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap,
                           QTransform)

from ..core import geometry as G
from ..core import imagery as IM
from ..core.crs import LocalCRSError
from ..core.model import Polyline, TextEntity
from ..core.tiles import TileSource
from . import theme

# ----------------------------------------------------------------------------- fast paths
_REC = np.dtype([("c", ">i4"), ("x", ">f8"), ("y", ">f8")])


def make_path(xy: np.ndarray, codes: np.ndarray) -> QPainterPath:
    """Build a QPainterPath from (N,2) screen coords and per-vertex codes (0 = moveTo, 1 = lineTo)."""
    n = len(xy)
    path = QPainterPath()
    if n == 0:
        return path
    rec = np.empty(n, dtype=_REC)
    rec["c"] = codes
    rec["x"] = xy[:, 0]
    rec["y"] = xy[:, 1]
    data = np.array([n], dtype=">i4").tobytes() + rec.tobytes() + b"\0" * 8
    ds = QDataStream(QByteArray(data))
    ds >> path
    return path


DASHES = {"DASHED": [7, 4], "HIDDEN": [4, 3], "DASHDOT": [9, 3, 1.5, 3], "CENTER": [14, 3, 3, 3], "DOT": [1, 3],
          "PHANTOM": [14, 3, 3, 3, 3, 3], "DIVIDE": [14, 3, 3, 3, 3, 3], "BORDER": [10, 3, 10, 3, 2, 3]}


def pen_for(rgb, width: float = 1.0, linetype: str = "CONTINUOUS", alpha: int = 255) -> QPen:
    pen = QPen(QColor(rgb[0], rgb[1], rgb[2], alpha), width)
    pen.setCosmetic(True)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    lt = (linetype or "CONTINUOUS").upper()
    for key, pat in DASHES.items():
        if lt.startswith(key):
            pen.setCapStyle(Qt.FlatCap)
            pen.setDashPattern([max(0.5, d / max(width, 1.0)) for d in pat])
            break
    return pen


# ----------------------------------------------------------------------------- view
class View:
    def __init__(self, cx=0.0, cy=0.0, scale=1.0, w=800, h=600):
        self.cx, self.cy, self.scale, self.w, self.h = cx, cy, scale, w, h

    def copy(self) -> "View":
        return View(self.cx, self.cy, self.scale, self.w, self.h)

    def to_screen_arr(self, xy: np.ndarray) -> np.ndarray:
        out = np.empty((len(xy), 2))
        out[:, 0] = (xy[:, 0] - self.cx) * self.scale + self.w / 2.0
        out[:, 1] = self.h / 2.0 - (xy[:, 1] - self.cy) * self.scale
        return out

    def to_screen(self, x: float, y: float):
        return (x - self.cx) * self.scale + self.w / 2.0, self.h / 2.0 - (y - self.cy) * self.scale

    def to_world(self, sx: float, sy: float):
        return self.cx + (sx - self.w / 2.0) / self.scale, self.cy - (sy - self.h / 2.0) / self.scale

    def bounds(self):
        x0, y1 = self.to_world(0, 0)
        x1, y0 = self.to_world(self.w, self.h)
        return x0, y0, x1, y1

    def fit(self, bbox, margin: float = 0.08):
        x0, y0, x1, y1 = bbox
        dx, dy = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        self.cx, self.cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.scale = min(self.w / (dx * (1 + 2 * margin)), self.h / (dy * (1 + 2 * margin)))


@dataclass
class DisplayOptions:
    show_points: bool = True
    show_numbers: bool = True
    show_elev: bool = True
    show_desc: bool = False
    show_lines: bool = True
    show_text: bool = True
    show_surfaces: bool = True
    show_imagery: bool = True
    show_grid: bool = False
    show_scalebar: bool = True
    point_px: float = 7.0
    label_px: float = 11.0
    line_px: float = 1.0
    attribution: str = ""
    error_point_ids: set[int] = field(default_factory=set)
    hide_non_error_points: bool = False
    dim_non_error_points: bool = False


# ----------------------------------------------------------------------------- cached scene geometry
class LineGroup:
    __slots__ = ("layer", "rgb", "linetype", "xy", "starts", "counts", "ids", "bbox", "index")

    def __init__(self, layer, rgb, linetype):
        self.layer, self.rgb, self.linetype = layer, rgb, linetype
        self.index = layer.upper().find("INDEX") >= 0


class SceneCache:
    """Everything the painter needs, flattened to numpy arrays. Rebuilt when project.revision changes."""

    def __init__(self, project):
        self.rev = project.revision
        self.pid = id(project)
        vis = {n: l for n, l in project.layers.items() if l.visible}
        hidden = project.hidden_ids()          # groups switched off - hidden everywhere (item 2)
        groups: dict = {}
        texts = []
        for e in project.entities.values():
            if e.id in hidden:
                continue
            lay = vis.get(e.layer)
            if lay is None:
                continue
            if isinstance(e, Polyline):
                if len(e.verts) < 2:
                    continue
                v = G.flatten_polyline(e.verts, e.bulges, e.closed, max_step=math.radians(3.0))
                if e.closed and len(v):
                    v = np.vstack([v, v[:1]])
                rgb = tuple(e.color) if e.color else tuple(lay.color)
                lt = e.linetype or lay.linetype
                key = (e.layer, rgb, lt)
                g = groups.setdefault(key, {"v": [], "ids": []})
                g["v"].append(v[:, :2])
                g["ids"].append(e.id)
            elif isinstance(e, TextEntity):
                rgb = tuple(e.color) if e.color else tuple(lay.color)
                texts.append((e.id, e.x, e.y, e.text, e.height, e.rotation, rgb, e.derived.startswith("contours")))
        self.groups: list[LineGroup] = []
        for (layer, rgb, lt), g in groups.items():
            lg = LineGroup(layer, rgb, lt)
            arrs = g["v"]
            lg.counts = np.array([len(a) for a in arrs], np.int64)
            lg.starts = np.concatenate([[0], np.cumsum(lg.counts)[:-1]])
            lg.xy = np.concatenate(arrs)
            lg.ids = np.array(g["ids"], np.int64)
            mn = np.minimum.reduceat(lg.xy, lg.starts, axis=0)
            mx = np.maximum.reduceat(lg.xy, lg.starts, axis=0)
            lg.bbox = np.column_stack([mn, mx])
            self.groups.append(lg)
        self.texts = texts
        # points, grouped by layer for colouring
        pts = [p for p in project.points.values() if p.layer in vis and p.id not in hidden]
        self.pt_ids = np.array([p.id for p in pts], np.int64)
        self.pt_xy = np.array([(p.x, p.y) for p in pts], float).reshape(-1, 2)
        self.pt_z = np.array([p.z for p in pts], float)
        self.pt_num = [p.number for p in pts]
        self.pt_desc = [p.desc for p in pts]
        lays = sorted({p.layer for p in pts})
        lidx = {n: i for i, n in enumerate(lays)}
        self.pt_layer_idx = np.array([lidx[p.layer] for p in pts], np.int64)
        self.pt_layer_rgb = [tuple(vis[n].color) for n in lays]


from ..core.ramps import RAMPS as _RAMPS  # noqa: E402  (shared with the 3D renderer)
from ..core.ramps import SLOPE_EDGES as _SLOPE_EDGES, lut as _lut  # noqa: E402


class SurfaceDraw:
    """Smooth (Gouraud) shading of a TIN: per-vertex elevation / slope + hill-shade rendered with matplotlib's Agg."""

    def __init__(self, surface, mode: str):
        self.key = (id(surface), id(surface.pts), id(surface.tris), mode)
        tin = surface.tin()
        self.mode = mode
        self.tin = tin
        self.pts, self.tris = tin.pts, tin.tris
        self.origin = tin.pts[:, :2].mean(axis=0) if len(tin.pts) else np.zeros(2)
        tri = tin.pts[tin.tris][:, :, :2]
        self.tbox = np.column_stack([tri.min(axis=1), tri.max(axis=1)])
        n = len(self.pts)
        a2 = tin.areas2d()
        ms = float((tin.slopes_pct() * a2).sum() / max(a2.sum(), 1e-12)) / 100.0 if tin.n_tris else 0.0
        self.vexag = float(np.clip(0.15 / max(ms, 0.004), 1.0, 10.0))
        # smooth vertex normals (area weighted) -> vertex hill-shade
        P = tin.pts[tin.tris]
        u, v = P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]
        nrm = np.cross(u * [1, 1, self.vexag], v * [1, 1, self.vexag])
        nrm[nrm[:, 2] < 0] *= -1
        vn = np.zeros((n, 3))
        for k in range(3):
            np.add.at(vn, tin.tris[:, k], nrm)
        ln = np.linalg.norm(vn, axis=1)
        ln[ln == 0] = 1
        vn /= ln[:, None]
        az, alt = math.radians(315.0), math.radians(45.0)
        L = np.array([math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)])
        self.vshade = np.clip(vn @ L, 0, 1)
        z = tin.pts[:, 2]
        lo, hi = (np.percentile(z, 1), np.percentile(z, 99)) if n else (0, 1)
        self.vz = np.clip((z - lo) / max(hi - lo, 1e-9), 0, 1)
        # vertex slope (percent): average of adjacent triangle slopes
        sl = tin.slopes_pct()
        acc, cnt = np.zeros(n), np.zeros(n)
        for k in range(3):
            np.add.at(acc, tin.tris[:, k], sl)
            np.add.at(cnt, tin.tris[:, k], 1)
        self.vslope = acc / np.maximum(cnt, 1)
        self._vcol = None
        self._edges = None
        self._flat = None
        self._base = {}
        self._base_buf = None

    # -------------------------------------------------------------- smooth raster
    def _vertex_colors(self):
        """Per-vertex RGBA where R = hill-shade, G = normalised elevation, B = normalised slope (Gouraud interpolates each)."""
        if self._vcol is None:
            sl = np.clip(np.interp(self.vslope, [0, 60], [0, 1]), 0, 1)
            self._vcol = np.column_stack([self.vshade, self.vz, sl, np.ones(len(self.vz))])
        return self._vcol

    def _render(self, view: "View", dpr: float, tri_idx=None):
        """One Agg pass over (a subset of) the triangles -> (H, W, 4) uint8 with the packed channels."""
        from matplotlib.backends.backend_agg import RendererAgg
        from matplotlib.transforms import Affine2D
        W, H = max(1, int(round(view.w * dpr))), max(1, int(round(view.h * dpr)))
        k = view.scale * dpr
        P = np.empty((len(self.pts), 2))
        P[:, 0] = (self.pts[:, 0] - view.cx) * k + W / 2.0
        P[:, 1] = H / 2.0 + (self.pts[:, 1] - view.cy) * k           # Agg's origin is bottom-left
        tris = self.tris if tri_idx is None else self.tris[tri_idx]
        r = RendererAgg(W, H, 100)
        r.clear()
        if len(tris):
            r.draw_gouraud_triangles(r.new_gc(), P[tris], self._vertex_colors()[tris], Affine2D())
        return np.asarray(r.buffer_rgba()), (W, H)

    def _colorize(self, buf: np.ndarray, W: int, H: int, theme_name: str) -> QImage:
        ramps = _RAMPS.get(theme_name, _RAMPS["dark"])
        shade = buf[..., 0].astype(np.float32) / 255.0
        hs = 0.55 + 0.64 * shade                                    # flat ground -> ~1.0
        if self.mode == "hillshade":
            g = np.clip(70 + 150 * shade, 0, 255) * (0.6 if theme_name == "dark" else 1.0)
            rgb = np.repeat(g[..., None], 3, axis=2)
        elif self.mode == "slope":
            pct = buf[..., 2].astype(np.float32) / 255.0 * 60.0
            cls = np.clip(np.searchsorted(_SLOPE_EDGES, pct), 0, 7)
            rgb = np.array(ramps["slope"] + [ramps["slope"][-1]], float)[:8][cls] * hs[..., None]
        else:
            rgb = _lut(ramps["elev"])[buf[..., 1]] * hs[..., None]
        out = np.empty((H, W, 4), np.uint8)
        out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
        out[..., 3] = buf[..., 3]
        out = np.ascontiguousarray(out)
        return QImage(out.data, W, H, W * 4, QImage.Format_RGBA8888).copy()

    def raster(self, view: "View", dpr: float, theme_name: str, tri_idx=None) -> QImage:
        """RGBA image aligned with `view` (physical size = view * dpr), drawing only the triangles in `tri_idx`."""
        buf, (W, H) = self._render(view, dpr, tri_idx)
        return self._colorize(buf, W, H, theme_name)

    def base_raster(self, theme_name: str, long_side: int = 2400):
        """A whole-surface image in world space, rendered once and reused while the view is zoomed out."""
        ent = self._base.get(theme_name)
        if ent is None:
            if self._base_buf is None:
                x0, y0, _, x1, y1, _ = self.tin.bounds()
                dx, dy = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
                sc = long_side / max(dx, dy)
                W, H = max(2, int(math.ceil(dx * sc))), max(2, int(math.ceil(dy * sc)))
                v = View((x0 + x1) / 2, (y0 + y1) / 2, sc, W, H)
                buf, _ = self._render(v, 1.0)
                self._base_buf = (buf.copy(), v, (x0, y0, x1, y1))
            buf, v, bounds = self._base_buf
            ent = self._base[theme_name] = (self._colorize(buf, v.w, v.h, theme_name), v.scale, bounds)
        return ent

    # -------------------------------------------------------------- flat fallback (huge TINs) + edges
    def edges(self):
        if self._edges is None:
            e = self.tris[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
            self._edges = np.unique(np.sort(e, axis=1), axis=0)
        return self._edges

    def flat_buckets(self, theme_name: str):
        if self._flat is None:
            ramps = _RAMPS.get(theme_name, _RAMPS["dark"])
            hs = 0.55 + 0.64 * self.vshade[self.tris].mean(axis=1)
            t = np.clip(self.vz[self.tris].mean(axis=1), 0, 1)
            lut = _lut(ramps["elev"])
            col = lut[(t * 255).astype(int)] * hs[:, None]
            q = (np.clip(col, 0, 255) / 255.0 * 7).round().astype(np.int64)
            key = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
            uniq, inv = np.unique(key, return_inverse=True)
            colors = [QColor(int((u // 64) * 255 / 7), int(((u // 8) % 8) * 255 / 7), int((u % 8) * 255 / 7)) for u in uniq]
            self._flat = (inv, colors)
        return self._flat


# ----------------------------------------------------------------------------- imagery
class ImageryManager(QObject):
    """Decodes and caches tile pixmaps; asks the TileStore for what's missing."""
    tilesChanged = Signal()
    _arrived = Signal(object, object, object)

    MAX_PIX = 420

    def __init__(self, state):
        super().__init__()
        self.state = state
        self._pix: OrderedDict = OrderedDict()
        self._geo: dict = {}
        self._files: dict = {}
        self._arrived.connect(self._on_arrived, Qt.QueuedConnection)
        self.last_error = ""
        self.pending = 0

    # -- tile data
    def _on_arrived(self, key, data, _):
        self.pending = max(0, self.pending - 1)
        if data is not None:
            img = QImage.fromData(data)
            if not img.isNull():
                self._pix[key] = QPixmap.fromImage(img)
                while len(self._pix) > self.MAX_PIX:
                    self._pix.popitem(last=False)
                self.tilesChanged.emit()

    def _request(self, src: TileSource, z, x, y):
        key = (src.key, z, x, y)
        if key in self._pix:
            return

        def cb(s, zz, xx, yy, data, key=key):
            self._arrived.emit(key, data, None)

        if self.state.tiles.request(src, z, x, y, cb):
            self.pending += 1

    def clear(self):
        self._pix.clear()
        self._geo.clear()
        self._files.clear()

    # -- geometry
    def _tile_corners(self, project, src: TileSource, z, x, y, nudge, strategy):
        """Project-coordinate positions of a tile's TL, TR, BL corners."""
        key = (src.key, z, x, y, project.crs.label, strategy, nudge)
        c = self._geo.get(key)
        if c is None:
            xmin, ymin, xmax, ymax = IM.tile_bounds_merc(z, x, y)
            lon, lat = IM.merc_to_lonlat(np.array([xmin, xmax, xmin]), np.array([ymax, ymax, ymin]))
            px, py = project.crs.from_lonlat(lon, lat, source=4326, strategy=strategy)
            c = np.column_stack([np.asarray(px) + nudge[0], np.asarray(py) + nudge[1]])
            if len(self._geo) > 4000:
                self._geo.clear()
            self._geo[key] = c
        return c

    def view_tile_range(self, project, view: View, src: TileSource, strategy):
        """Pick a zoom and the tile list covering the view."""
        x0, y0, x1, y1 = view.bounds()
        gx = np.array([x0, x1, x1, x0, (x0 + x1) / 2])
        gy = np.array([y0, y0, y1, y1, (y0 + y1) / 2])
        lon, lat = project.crs.to_lonlat(gx, gy, target=4326, strategy=strategy)
        lon, lat = np.asarray(lon), np.asarray(lat)
        if not (np.isfinite(lon).all() and np.isfinite(lat).all()):
            return None, []
        mx, my = IM.lonlat_to_merc(lon, lat)
        mpp = (1.0 / view.scale) * project.crs.unit_factor
        zf = IM.zoom_for_resolution(mpp, float(lat[-1]), src.tile_size)
        z = int(math.ceil(zf - 0.35))
        z = max(src.min_zoom, min(src.max_zoom, z))
        while z > src.min_zoom:
            tiles = IM.tiles_for_bbox(z, mx.min(), my.min(), mx.max(), my.max(), limit=90)
            if tiles:
                break
            z -= 1
        else:
            tiles = IM.tiles_for_bbox(z, mx.min(), my.min(), mx.max(), my.max(), limit=10 ** 6)[:90]
        return z, tiles

    # -- drawing
    def draw(self, p: QPainter, view: View, project, layers, opts: DisplayOptions) -> list:
        notes = []
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for lay in layers:
            if not lay.visible:
                continue
            p.setOpacity(max(0.0, min(1.0, lay.opacity)))
            try:
                if lay.kind == "tiles":
                    self._draw_tiles(p, view, project, lay, notes)
                elif lay.kind == "file":
                    self._draw_file(p, view, project, lay, notes)
            except LocalCRSError:
                notes.append("Imagery needs a coordinate system - this project uses local coordinates.")
            except Exception as ex:                      # a bad layer must never kill the canvas
                notes.append(f"{lay.name}: {ex}")
        p.restore()
        return notes

    def _draw_tiles(self, p: QPainter, view: View, project, lay, notes):
        src = TileSource.from_dict(lay.source)
        strategy = lay.source.get("strategy") or project.crs.strategy
        z, tiles = self.view_tile_range(project, view, src, strategy)
        if z is None:
            return
        cx, cy = view.w / 2, view.h / 2
        order = sorted(tiles, key=lambda t: 0)
        nudge = (round(lay.nudge[0], 6), round(lay.nudge[1], 6))
        want = []
        for (x, y) in order:
            corners = self._tile_corners(project, src, z, x, y, nudge, strategy)
            sc = view.to_screen_arr(corners)
            key = (src.key, z, x, y)
            pm = self._pix.get(key)
            sub = None
            if pm is not None:
                self._pix.move_to_end(key)
            else:
                want.append((x, y, sc))
                for up in range(1, 5):                   # fall back on a coarser tile already in memory
                    if z - up < 0:
                        break
                    k2 = (src.key, z - up, x >> up, y >> up)
                    pm2 = self._pix.get(k2)
                    if pm2 is not None:
                        s = pm2.width() / (2 ** up)
                        sub = (pm2, QRectF(((x % (2 ** up)) * s), ((y % (2 ** up)) * s), s, s))
                        break
            if pm is not None:
                self._blit(p, pm, QRectF(0, 0, pm.width(), pm.height()), sc)
            elif sub is not None:
                pm2, srect = sub
                self._blit(p, pm2, srect, sc, target=(src.tile_size, src.tile_size))
        # queue the missing ones, nearest to the centre first
        want.sort(key=lambda t: (t[2][:, 0].mean() - cx) ** 2 + (t[2][:, 1].mean() - cy) ** 2)
        for x, y, _ in want[:48]:
            self._request(src, z, x, y)
        self._attr = src.attribution

    @staticmethod
    def _blit(p: QPainter, pm: QPixmap, srect: QRectF, sc: np.ndarray, target=None):
        w, h = target if target else (srect.width(), srect.height())
        u = (sc[1] - sc[0]) / w
        v = (sc[2] - sc[0]) / h
        p.save()
        p.setTransform(QTransform(u[0], u[1], v[0], v[1], sc[0, 0], sc[0, 1]), True)
        p.drawPixmap(QRectF(0, 0, w + 0.6, h + 0.6), pm, srect)      # +0.6 px hides hairline seams
        p.restore()

    def _file_pixmap(self, lay):
        key = lay.source.get("path")
        ent = self._files.get(key)
        if ent is None:
            r = IM.read_georeferenced_image(key)
            rgba = np.ascontiguousarray(r["rgba"])
            img = QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.shape[1] * 4, QImage.Format_RGBA8888).copy()
            ent = (QPixmap.fromImage(img), r)
            self._files[key] = ent
        return ent

    def _draw_file(self, p: QPainter, view: View, project, lay, notes):
        pm, r = self._file_pixmap(lay)
        corners = np.array(lay.source.get("corners") or r["corners"], float)
        crs = lay.source.get("crs") if "crs" in lay.source else r["crs"]
        if crs:
            from ..core.crs import make_transform, resolve_crs
            gx, gy = project.crs.from_lonlat(corners[:, 0], corners[:, 1], source=crs) \
                if resolve_crs(crs).is_geographic else project.crs.transform_from(crs)(corners[:, 0], corners[:, 1])
            corners = np.column_stack([gx, gy])
        corners = corners + np.array(lay.nudge)
        self._blit(p, pm, QRectF(0, 0, pm.width(), pm.height()), view.to_screen_arr(corners))


# ----------------------------------------------------------------------------- scene painter
def nice_length(target: float) -> float:
    if target <= 0:
        return 1.0
    e = math.floor(math.log10(target))
    for m in (1, 2, 5, 10):
        if m * 10 ** e >= target * 0.7:
            return m * 10 ** e
    return 10 ** (e + 1)


class SceneRenderer:
    def __init__(self, state, imagery: ImageryManager | None = None):
        self.state = state
        self.imagery = imagery
        self._cache: SceneCache | None = None
        self._sd: dict = {}
        self.last_notes: list = []
        self.points_hidden = 0

    def scene(self) -> SceneCache:
        pr = self.state.project
        c = self._cache
        if c is None or c.rev != pr.revision or c.pid != id(pr):
            c = self._cache = SceneCache(pr)
        return c

    def invalidate(self):
        self._cache = None

    # ------------------------------------------------------------------ main paint
    def paint(self, p: QPainter, view: View, opts: DisplayOptions):
        pr = self.state.project
        col = theme.colors()
        p.fillRect(QRectF(0, 0, view.w, view.h), QColor(col["canvas"]))
        p.setRenderHint(QPainter.Antialiasing, True)
        notes: list = []
        self.points_hidden = 0
        if opts.show_grid:
            self._grid(p, view, col)
        if opts.show_imagery and self.imagery and pr.imagery:
            notes += self.imagery.draw(p, view, pr, list(pr.imagery.values()), opts)
        sc = self.scene()
        dpr = p.device().devicePixelRatioF() if hasattr(p.device(), "devicePixelRatioF") else 1.0
        _hidden = pr.hidden_ids()
        if opts.show_surfaces:
            for s in pr.surfaces.values():
                if s.id in _hidden:
                    continue
                self._surface(p, view, s, dpr)
        if opts.show_lines:
            self._lines(p, view, sc, opts)
        if opts.show_points and len(sc.pt_ids):
            self._points(p, view, sc, opts)
        if opts.show_text:
            self._texts(p, view, sc, col)
        if self.points_hidden:
            notes.append(f"{self.points_hidden:,} points are too dense to draw at this zoom - zoom in to see them.")
        if opts.show_scalebar:
            self.scalebar(p, view, pr, col, opts)
        self.last_notes = notes

    # ------------------------------------------------------------------ pieces
    def _grid(self, p, view, col):
        x0, y0, x1, y1 = view.bounds()
        step = nice_length(150.0 / view.scale)
        p.setPen(pen_for(QColor(col["grid"]).getRgb()[:3], 1.0))
        f = QFont()
        f.setPixelSize(10)
        p.setFont(f)
        gx = math.floor(x0 / step) * step
        while gx <= x1:
            sx, _ = view.to_screen(gx, 0)
            p.drawLine(QPointF(sx, 0), QPointF(sx, view.h))
            gx += step
        gy = math.floor(y0 / step) * step
        while gy <= y1:
            _, sy = view.to_screen(0, gy)
            p.drawLine(QPointF(0, sy), QPointF(view.w, sy))
            gy += step
        p.setPen(QColor(col["dim"]))
        gx = math.floor(x0 / step) * step
        while gx <= x1:
            sx, _ = view.to_screen(gx, 0)
            p.drawText(QPointF(sx + 3, 12), f"E {gx:,.{max(0, -int(math.floor(math.log10(step))))}f}")
            gx += step
        gy = math.floor(y0 / step) * step
        while gy <= y1:
            _, sy = view.to_screen(0, gy)
            p.drawText(QPointF(3, sy - 3), f"N {gy:,.{max(0, -int(math.floor(math.log10(step))))}f}")
            gy += step

    def _surface(self, p, view, s, dpr: float = 1.0):
        st = s.style or {}
        mode = st.get("mode") or ("elevation" if st.get("shade", True) else "off")
        show_edges = st.get("edges", False)
        if mode == "off" and not show_edges:
            return
        tin = s.tin()
        if tin.n_tris == 0:
            return
        want = mode if mode != "off" else "elevation"
        sd = self._sd.get(s.id)
        if sd is None or sd.key != (id(s), id(s.pts), id(s.tris), want):
            sd = self._sd[s.id] = SurfaceDraw(s, want)
        x0, y0, x1, y1 = view.bounds()
        vis = (sd.tbox[:, 2] >= x0) & (sd.tbox[:, 0] <= x1) & (sd.tbox[:, 3] >= y0) & (sd.tbox[:, 1] <= y1)
        idx = np.nonzero(vis)[0]
        if not len(idx):
            return
        p.save()
        if mode != "off":
            p.setOpacity(float(st.get("opacity", 0.85)))
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            if len(idx) > 40_000:
                base, bscale, (bx0, by0, bx1, by1) = sd.base_raster(theme.current())
            else:
                base = None
            if base is not None and view.scale <= bscale * 1.3:
                # big surface, zoomed out: reuse the cached whole-surface image (instant pan / zoom)
                tl = view.to_screen(bx0, by1)
                p.drawImage(QRectF(tl[0], tl[1], (bx1 - bx0) * view.scale, (by1 - by0) * view.scale), base)
            elif len(idx) <= 400_000:
                sub = None if len(idx) == len(sd.tris) else idx
                img = sd.raster(view, dpr, theme.current(), sub)
                p.drawImage(QRectF(0, 0, view.w, view.h), img)
            else:                                           # enormous TIN, zoomed in: flat colours
                S = view.to_screen_arr(sd.pts[:, :2])
                p.setRenderHint(QPainter.Antialiasing, False)
                p.setPen(Qt.NoPen)
                inv, colors = sd.flat_buckets(theme.current())
                b = inv[idx]
                order = np.argsort(b, kind="stable")
                idx_sorted, b_sorted = idx[order], b[order]
                for seg in np.split(np.arange(len(idx_sorted)), np.nonzero(np.diff(b_sorted))[0] + 1):
                    if len(seg):
                        xy = S[sd.tris[idx_sorted[seg]]].reshape(-1, 2)
                        p.setBrush(QBrush(colors[b_sorted[seg[0]]]))
                        p.drawPath(make_path(xy, np.tile(np.array([0, 1, 1]), len(seg))))
        if show_edges:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setOpacity(0.55)
            e = sd.edges()
            a, bb = sd.pts[e[:, 0], :2], sd.pts[e[:, 1], :2]
            m = (np.maximum(a[:, 0], bb[:, 0]) >= x0) & (np.minimum(a[:, 0], bb[:, 0]) <= x1) & \
                (np.maximum(a[:, 1], bb[:, 1]) >= y0) & (np.minimum(a[:, 1], bb[:, 1]) <= y1)
            if m.any():
                A, B = view.to_screen_arr(a[m]), view.to_screen_arr(bb[m])
                xy = np.empty((len(A) * 2, 2))
                xy[0::2], xy[1::2] = A, B
                p.setPen(pen_for((150, 190, 230) if theme.current() == "dark" else (70, 110, 160), 0.8))
                p.setBrush(Qt.NoBrush)
                p.drawPath(make_path(xy, np.tile([0, 1], len(A))))
        p.restore()

    def _lines(self, p, view, sc: SceneCache, opts):
        x0, y0, x1, y1 = view.bounds()
        sel = self.state.sel_entities
        for g in sc.groups:
            bb = g.bbox
            vis = (bb[:, 2] >= x0) & (bb[:, 0] <= x1) & (bb[:, 3] >= y0) & (bb[:, 1] <= y1)
            big = np.maximum(bb[:, 2] - bb[:, 0], bb[:, 3] - bb[:, 1]) * view.scale >= 0.7
            vis &= big
            if not vis.any():
                continue
            if vis.all():
                xy = g.xy
                starts = g.starts
            else:
                vmask = np.repeat(vis, g.counts)
                xy = g.xy[vmask]
                cnt = g.counts[vis]
                starts = np.concatenate([[0], np.cumsum(cnt)[:-1]])
            S = view.to_screen_arr(xy)
            codes = np.ones(len(S), np.int64)
            codes[starts] = 0
            path = make_path(S, codes)
            rgb = theme.display_color(g.rgb)
            w = opts.line_px * (1.7 if g.index else 1.0)
            p.setPen(pen_for(rgb, w, g.linetype))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

    def _points(self, p, view, sc: SceneCache, opts):
        x0, y0, x1, y1 = view.bounds()
        m = (sc.pt_xy[:, 0] >= x0) & (sc.pt_xy[:, 0] <= x1) & (sc.pt_xy[:, 1] >= y0) & (sc.pt_xy[:, 1] <= y1)
        idx = np.nonzero(m)[0]
        if not len(idx):
            return

        err_set = getattr(opts, "error_point_ids", set()) or set()
        hide_non_err = getattr(opts, "hide_non_error_points", False)
        dim_non_err = getattr(opts, "dim_non_error_points", False)

        if hide_non_err and err_set:
            err_mask = np.isin(sc.pt_ids[idx], list(err_set))
            idx = idx[err_mask]
            if not len(idx):
                return

        S = view.to_screen_arr(sc.pt_xy[idx])
        s = opts.point_px / 2.0
        lidx = sc.pt_layer_idx[idx]
        # level of detail: when points are packed closer than their own symbols, shrink them, then drop them
        spacing = math.sqrt(max(view.w * view.h, 1.0) / max(len(idx), 1))
        if spacing < 2.2 and len(idx) > 2000 and not hide_non_err:
            self.points_hidden = len(idx)
            return
        crosses = spacing >= 11 or hide_non_err
        dot_w = 3.0 if spacing >= 6 else 2.0 if spacing >= 3.5 else 1.4
        for li in np.unique(lidx):
            sel = lidx == li
            A = S[sel]
            n = len(A)
            rgb = theme.display_color(sc.pt_layer_rgb[li])
            alpha = 70 if (dim_non_err and err_set) else 255
            if crosses:
                xy = np.empty((n * 4, 2))
                xy[0::4] = A + [-s, 0]
                xy[1::4] = A + [s, 0]
                xy[2::4] = A + [0, -s]
                xy[3::4] = A + [0, s]
                p.setPen(pen_for(rgb, 1.0, alpha=alpha))
                p.drawPath(make_path(xy, np.tile([0, 1, 0, 1], n)))
            dot = np.repeat(A, 2, axis=0)
            p.setPen(pen_for(rgb, dot_w, alpha=alpha))
            p.drawPath(make_path(dot, np.tile([0, 1], n)))

        # Draw error flag pins on points with active issues
        if err_set:
            err_mask = np.isin(sc.pt_ids[idx], list(err_set))
            if err_mask.any():
                err_S = S[err_mask]
                p.save()
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setPen(pen_for((255, 60, 60), 1.8))
                p.setBrush(QBrush(QColor(255, 60, 60, 80)))
                for ex, ey in err_S:
                    p.drawEllipse(QPointF(ex, ey), s + 4, s + 4)
                    # Draw flagpole and triangle flag
                    p.drawLine(QPointF(ex, ey - s - 1), QPointF(ex, ey - s - 14))
                    flag = QPainterPath()
                    flag.moveTo(ex, ey - s - 14)
                    flag.lineTo(ex + 8, ey - s - 10)
                    flag.lineTo(ex, ey - s - 6)
                    flag.closeSubpath()
                    p.fillPath(flag, QBrush(QColor(255, 60, 60)))
                p.restore()

        if not crosses:
            return
        # labels (decluttered)
        if not (opts.show_numbers or opts.show_elev or opts.show_desc):
            return
        f = QFont()
        f.setPixelSize(int(opts.label_px))
        p.setFont(f)
        fm = QFontMetricsF(f)
        lines = int(opts.show_numbers) + int(opts.show_elev) + int(opts.show_desc)
        cell_w = max(46.0, fm.averageCharWidth() * 9)
        cell_h = lines * (opts.label_px + 2) + 6
        cx = np.floor(S[:, 0] / cell_w).astype(np.int64)
        cy = np.floor(S[:, 1] / cell_h).astype(np.int64)
        key = cx * 100003 + cy
        _, first = np.unique(key, return_index=True)
        first = first[:2500]
        col = theme.colors()
        hi = (110, 200, 255) if theme.current() == "dark" else (0, 90, 160)
        gr = (130, 220, 130) if theme.current() == "dark" else (20, 120, 40)
        txt = QColor(col["text"])
        for k in first:
            i = idx[k]
            x, y = S[k]
            yy = y - 4
            if opts.show_numbers:
                p.setPen(txt)
                p.drawText(QPointF(x + 6, yy), sc.pt_num[i])
                yy += opts.label_px + 1
            if opts.show_elev and np.isfinite(sc.pt_z[i]):
                p.setPen(QColor(*hi))
                p.drawText(QPointF(x + 6, yy), f"{sc.pt_z[i]:.2f}")
                yy += opts.label_px + 1
            if opts.show_desc and sc.pt_desc[i]:
                p.setPen(QColor(*gr))
                p.drawText(QPointF(x + 6, yy), sc.pt_desc[i])

    def _texts(self, p, view, sc: SceneCache, col):
        if not sc.texts:
            return
        x0, y0, x1, y1 = view.bounds()
        n = 0
        bg = QColor(col["canvas"])
        for (_id, x, y, text, h, rot, rgb, centered) in sc.texts:
            px = h * view.scale
            if px < 4 or px > 600 or not (x0 - h * 40 <= x <= x1 + h * 40 and y0 - h * 40 <= y <= y1 + h * 40):
                continue
            n += 1
            if n > 3000:
                break
            sx, sy = view.to_screen(x, y)
            f = QFont()
            f.setPixelSize(max(1, int(round(px))))
            p.save()
            p.translate(sx, sy)
            p.rotate(-rot)
            p.setFont(f)
            w = QFontMetricsF(f).horizontalAdvance(text)
            ox = -w / 2 if centered else 0.0
            oy = px * 0.35 if centered else 0.0
            if centered:                                  # halo so contour labels read over the line
                p.setPen(bg)
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    p.drawText(QPointF(ox + dx, oy + dy), text)
            p.setPen(QColor(*theme.display_color(rgb)))
            p.drawText(QPointF(ox, oy), text)
            p.restore()

    def scalebar(self, p, view, pr, col, opts):
        L = nice_length(110.0 / view.scale)
        px = L * view.scale
        x, y = 16, view.h - 18
        p.setPen(pen_for(QColor(col["text"]).getRgb()[:3], 1.6))
        p.drawLine(QPointF(x, y), QPointF(x + px, y))
        p.drawLine(QPointF(x, y - 4), QPointF(x, y + 4))
        p.drawLine(QPointF(x + px, y - 4), QPointF(x + px, y + 4))
        f = QFont()
        f.setPixelSize(11)
        p.setFont(f)
        from ..core import units as U
        u = U.LABEL.get(pr.h_unit, pr.h_unit)
        p.drawText(QPointF(x, y - 7), f"{L:g} {u}")
        if opts.attribution:
            fm = QFontMetricsF(f)
            w = fm.horizontalAdvance(opts.attribution)
            p.setPen(QColor(col["dim"]))
            p.drawText(QPointF(view.w - w - 8, view.h - 6), opts.attribution)
