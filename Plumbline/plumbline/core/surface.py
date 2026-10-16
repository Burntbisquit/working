"""TIN surfaces: build (breaklines / boundaries / holes), contours, volumes, profiles, sections.

Design notes
------------
* Triangulation is Delaunay (scipy/Qhull) made *conforming* to breaklines: any breakline segment that
  is not already a triangulation edge is split at its midpoint (Z interpolated) until it is.  This keeps
  breakline elevations exact and never moves data points.
* Crossing breaklines are noded (Z averaged, a warning is reported).
* Contours are exact marching-triangles with edge-id chaining, so they close perfectly.
* Volumes against a datum are exact (prismoidal, triangles split at the zero line).
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

import numpy as np
import shapely
from scipy.spatial import Delaunay, cKDTree
from shapely import STRtree

from . import geometry as G


# ============================================================================ TIN
class TIN:
    def __init__(self, pts, tris):
        self.pts = np.ascontiguousarray(pts, float).reshape(-1, 3)
        self.tris = np.ascontiguousarray(tris, np.int64).reshape(-1, 3)
        self._finder = None
        self._origin = None
        self._edges = None
        # The point locator is built lazily and may be pre-built on a worker thread (prepare()),
        # so building it has to be serialised - see finder().
        self._finder_lock = threading.Lock()

    def __getstate__(self):
        s = dict(self.__dict__)
        s.pop("_finder_lock", None)          # a lock cannot be pickled
        # the point locator is derived data and matplotlib's Triangulation cannot be pickled;
        # drop it and let the copy rebuild it lazily on first use
        s["_finder"] = None
        s["_origin"] = None
        return s

    def __setstate__(self, s):
        self.__dict__.update(s)
        self._finder_lock = threading.Lock()

    # ------------------------------------------------------------------ basics
    @property
    def n_points(self) -> int:
        return len(self.pts)

    @property
    def n_tris(self) -> int:
        return len(self.tris)

    def bounds(self):
        p = self.pts
        return (float(p[:, 0].min()), float(p[:, 1].min()), float(p[:, 2].min()),
                float(p[:, 0].max()), float(p[:, 1].max()), float(p[:, 2].max()))

    def _corners(self):
        P, T = self.pts, self.tris
        return P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]

    def areas2d(self) -> np.ndarray:
        A, B, C = self._corners()
        return 0.5 * np.abs((B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (C[:, 0] - A[:, 0]) * (B[:, 1] - A[:, 1]))

    def normals(self) -> np.ndarray:
        A, B, C = self._corners()
        n = np.cross(B - A, C - A)
        n[n[:, 2] < 0] *= -1                      # always point up
        return n

    def areas3d(self) -> np.ndarray:
        A, B, C = self._corners()
        return 0.5 * np.linalg.norm(np.cross(B - A, C - A), axis=1)

    def slopes_pct(self, vexag: float = 1.0) -> np.ndarray:
        n = self.normals()
        with np.errstate(divide="ignore", invalid="ignore"):
            s = np.hypot(n[:, 0], n[:, 1]) / np.where(n[:, 2] == 0, np.nan, n[:, 2])
        return np.nan_to_num(s * vexag * 100.0, nan=0.0, posinf=1e6)

    def aspects_deg(self) -> np.ndarray:
        """Downslope azimuth (degrees clockwise from north) per triangle."""
        n = self.normals()
        # plane z = a x + b y + c ; gradient = (-nx/nz, -ny/nz); downslope = -gradient = (nx/nz, ny/nz)
        with np.errstate(divide="ignore", invalid="ignore"):
            e = n[:, 0] / n[:, 2]
            nn = n[:, 1] / n[:, 2]
        return np.degrees(np.arctan2(e, nn)) % 360.0

    def hillshade(self, azimuth: float = 315.0, altitude: float = 45.0, vexag: float = 1.0) -> np.ndarray:
        n = self.normals().copy()
        n[:, 2] = n[:, 2] / max(vexag, 1e-9)
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        az, alt = math.radians(azimuth), math.radians(altitude)
        light = np.array([math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)])
        return np.clip(n @ light, 0.0, 1.0)

    def edges(self) -> np.ndarray:
        if self._edges is None:
            T = self.tris
            e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
            e.sort(axis=1)
            key = e[:, 0] * len(self.pts) + e[:, 1]
            _, idx = np.unique(key, return_index=True)
            self._edges = e[idx]
        return self._edges

    def summary(self) -> dict:
        xmin, ymin, zmin, xmax, ymax, zmax = self.bounds()
        a2, a3 = self.areas2d(), self.areas3d()
        sl = self.slopes_pct()
        w = a2 / a2.sum() if a2.sum() > 0 else a2
        # sliver triangles (created where breaklines pass a hair from a shot) have meaningless slopes: ignore them
        sig = a2 >= 1e-3 * (np.median(a2) if len(a2) else 0.0)
        return {"points": self.n_points, "triangles": self.n_tris, "xmin": xmin, "ymin": ymin, "xmax": xmax,
                "ymax": ymax, "zmin": zmin, "zmax": zmax, "area2d": float(a2.sum()), "area3d": float(a3.sum()),
                "mean_slope_pct": float((sl * w).sum()),
                "max_slope_pct": float(sl[sig].max()) if sig.any() else 0.0}

    # ------------------------------------------------------------------ location / interpolation
    def finder(self):
        if self._finder is None:
            # Two threads can race here: the GUI builds the locator on demand for the elevation
            # readout, while prepare() may be building it in the background.  Double-checked
            # locking keeps them from both doing the work (and from matplotlib's Triangulation
            # being constructed twice at once).
            with self._finder_lock:
                if self._finder is None:
                    from matplotlib.tri import Triangulation
                    self._origin = self.pts[:, :2].mean(axis=0)
                    ox, oy = self._origin
                    tri = Triangulation(self.pts[:, 0] - ox, self.pts[:, 1] - oy, self.tris)
                    self._finder = tri.get_trifinder()
        return self._finder

    def prepare(self):
        """Force-build the point locator (call from a worker thread for big TINs).

        Raises if the locator could not be built, so a background caller can report it rather
        than leaving the elevation readout silently blank forever.
        """
        if self.n_tris:
            self.finder()
        return self

    def locate(self, x, y) -> np.ndarray:
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        if self.n_tris == 0:
            return np.full(x.shape, -1)
        f = self.finder()
        return f(x - self._origin[0], y - self._origin[1])

    def z_at(self, x, y) -> np.ndarray:
        """Interpolated elevation (NaN outside the TIN)."""
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        scalar = x.ndim == 0
        x1, y1 = np.atleast_1d(x).ravel(), np.atleast_1d(y).ravel()
        idx = self.locate(x1, y1)
        z = np.full(x1.shape, np.nan)
        ok = idx >= 0
        if ok.any():
            t = self.tris[idx[ok]]
            A, B, C = self.pts[t[:, 0]], self.pts[t[:, 1]], self.pts[t[:, 2]]
            den = (B[:, 1] - C[:, 1]) * (A[:, 0] - C[:, 0]) + (C[:, 0] - B[:, 0]) * (A[:, 1] - C[:, 1])
            den = np.where(den == 0, np.nan, den)
            l1 = ((B[:, 1] - C[:, 1]) * (x1[ok] - C[:, 0]) + (C[:, 0] - B[:, 0]) * (y1[ok] - C[:, 1])) / den
            l2 = ((C[:, 1] - A[:, 1]) * (x1[ok] - C[:, 0]) + (A[:, 0] - C[:, 0]) * (y1[ok] - C[:, 1])) / den
            l3 = 1.0 - l1 - l2
            z[ok] = l1 * A[:, 2] + l2 * B[:, 2] + l3 * C[:, 2]
        z = z.reshape(x.shape if not scalar else (1,))
        return z[0] if scalar else z

    def slope_at(self, x: float, y: float):
        """(slope %, downslope azimuth deg) of the triangle at a point, or None."""
        idx = int(self.locate(np.array([x]), np.array([y]))[0])
        if idx < 0:
            return None
        sub = TIN(self.pts, self.tris[idx:idx + 1])
        return float(sub.slopes_pct()[0]), float(sub.aspects_deg()[0])

    # ------------------------------------------------------------------ contours
    def contours(self, levels, smooth: int = 0):
        """Exact contour polylines. Returns [(level, verts (n,3), closed)]."""
        pts, tris = self.pts, self.tris
        if len(tris) == 0:
            return []
        z = pts[:, 2]
        tz = z[tris]
        tmin, tmax = tz.min(axis=1), tz.max(axis=1)
        N = len(pts)
        ia = tris
        ib = np.roll(tris, -1, axis=1)
        ekeys = np.minimum(ia, ib).astype(np.int64) * N + np.maximum(ia, ib)
        out = []
        for level in levels:
            L = level + 1e-8 * max(1.0, abs(level))
            cand = np.nonzero((tmin < L) & (tmax >= L))[0]
            if cand.size == 0:
                continue
            tc = tris[cand]
            above = tz[cand] >= L
            cross = above != np.roll(above, -1, axis=1)
            e_idx = np.argsort(~cross, axis=1, kind="stable")[:, :2]
            r = np.arange(len(cand))[:, None]
            ek = ekeys[cand][r, e_idx]
            va = tc[r, e_idx]
            vb = tc[r, (e_idx + 1) % 3]
            za, zb = z[va], z[vb]
            t = (L - za) / (zb - za)
            px = pts[va, 0] + t * (pts[vb, 0] - pts[va, 0])
            py = pts[va, 1] + t * (pts[vb, 1] - pts[va, 1])
            uniq, inv = np.unique(ek.ravel(), return_inverse=True)
            seg = inv.reshape(-1, 2)
            nx = np.empty(len(uniq))
            ny = np.empty(len(uniq))
            nx[inv] = px.ravel()
            ny[inv] = py.ravel()
            for chain, closed in _chain_segments(seg, len(uniq)):
                if len(chain) < 2:
                    continue
                v = np.column_stack([nx[chain], ny[chain], np.full(len(chain), float(level))])
                if smooth > 0 and len(v) >= 3:
                    v = G.chaikin(v, smooth, closed)
                out.append((float(level), v, closed))
        return out

    # ------------------------------------------------------------------ volumes
    def volume_to_datum(self, datum: float, region=None) -> "VolumeResult":
        """Cut (above datum) and fill (below datum) volumes; exact for a piecewise-linear surface."""
        A, B, C = self._corners()
        area = 0.5 * np.abs((B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (C[:, 0] - A[:, 0]) * (B[:, 1] - A[:, 1]))
        h = np.column_stack([A[:, 2], B[:, 2], C[:, 2]]) - datum
        if region is None:
            pos, neg, apos, aneg = _split_volumes(area, h)
            return VolumeResult(float(pos.sum()), float(-neg.sum()), float(apos.sum()), float(aneg.sum()),
                                float(area.sum()), "exact (full surface)", self.n_tris)
        # ---- limited to a region polygon
        corners = np.stack([A[:, :2], B[:, :2], C[:, :2], A[:, :2]], axis=1)
        tri_polys = shapely.polygons(corners)
        shapely.prepare(region)
        inside = shapely.contains(region, tri_polys)
        partial = shapely.intersects(region, tri_polys) & ~inside
        pos, neg, apos, aneg = _split_volumes(area[inside], h[inside])
        cut, fill, acut, afill, atot = pos.sum(), -neg.sum(), apos.sum(), aneg.sum(), area[inside].sum()
        for k in np.nonzero(partial)[0]:
            piece = shapely.intersection(region, tri_polys[k])
            if piece.is_empty or piece.area <= 0:
                continue
            polys = [g for g in getattr(piece, "geoms", [piece]) if g.geom_type == "Polygon" and g.area > 0]
            sub = []
            for pg in polys:
                try:
                    tg = shapely.constrained_delaunay_triangulation(pg)
                    sub.extend(g for g in tg.geoms)
                except Exception:
                    sub.extend(g for g in getattr(shapely.delaunay_triangles(pg), "geoms", []))
            if not sub:
                continue
            q = np.array([np.asarray(g.exterior.coords)[:3, :2] for g in sub])          # (S,3,2)
            a2 = 0.5 * np.abs((q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1]) -
                              (q[:, 2, 0] - q[:, 0, 0]) * (q[:, 1, 1] - q[:, 0, 1]))
            a, b, c = A[k], B[k], C[k]
            den = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            hh = np.empty((len(sub), 3))
            for j in range(3):
                l1 = ((b[1] - c[1]) * (q[:, j, 0] - c[0]) + (c[0] - b[0]) * (q[:, j, 1] - c[1])) / den
                l2 = ((c[1] - a[1]) * (q[:, j, 0] - c[0]) + (a[0] - c[0]) * (q[:, j, 1] - c[1])) / den
                hh[:, j] = l1 * a[2] + l2 * b[2] + (1 - l1 - l2) * c[2] - datum
            p2, n2, ap2, an2 = _split_volumes(a2, hh)
            cut += p2.sum(); fill += -n2.sum(); acut += ap2.sum(); afill += an2.sum(); atot += a2.sum()
        return VolumeResult(float(cut), float(fill), float(acut), float(afill), float(atot),
                            "exact (clipped to region)", self.n_tris)

    # ------------------------------------------------------------------ profiles & sections
    def profile(self, xy) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Elevation along a polyline from exact TIN-edge intersections.

        Returns (station, z, xy) arrays.  z is NaN where the line leaves the TIN (with a NaN
        sample inserted so plots break there).
        """
        xy = np.asarray(xy, float).reshape(-1, 2)
        E = self.edges()
        P = self.pts
        Ea, Eb = P[E[:, 0]], P[E[:, 1]]
        emin_x, emax_x = np.minimum(Ea[:, 0], Eb[:, 0]), np.maximum(Ea[:, 0], Eb[:, 0])
        emin_y, emax_y = np.minimum(Ea[:, 1], Eb[:, 1]), np.maximum(Ea[:, 1], Eb[:, 1])
        st_out, z_out, xy_out = [], [], []
        base = 0.0
        for i in range(len(xy) - 1):
            p, q = xy[i], xy[i + 1]
            d = q - p
            L = float(np.hypot(*d))
            if L == 0:
                continue
            m = ((emax_x >= min(p[0], q[0])) & (emin_x <= max(p[0], q[0])) &
                 (emax_y >= min(p[1], q[1])) & (emin_y <= max(p[1], q[1])))
            a, b = Ea[m], Eb[m]
            e = b[:, :2] - a[:, :2]
            rxs = d[0] * e[:, 1] - d[1] * e[:, 0]
            qp = a[:, :2] - p
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (qp[:, 0] * e[:, 1] - qp[:, 1] * e[:, 0]) / rxs
                u = (qp[:, 0] * d[1] - qp[:, 1] * d[0]) / rxs
            ok = (np.abs(rxs) > 1e-12) & (t >= -1e-9) & (t <= 1 + 1e-9) & (u >= -1e-9) & (u <= 1 + 1e-9)
            tt = np.clip(t[ok], 0, 1)
            zz = a[ok, 2] + np.clip(u[ok], 0, 1) * (b[ok, 2] - a[ok, 2])
            z0 = float(self.z_at(p[0], p[1]))
            z1 = float(self.z_at(q[0], q[1]))
            ts = np.concatenate([[0.0], tt, [1.0]])
            zs = np.concatenate([[z0], zz, [z1]])
            order = np.argsort(ts, kind="stable")
            ts, zs = ts[order], zs[order]
            keep = np.concatenate([[True], np.diff(ts) * L > 1e-9])
            ts, zs = ts[keep], zs[keep]
            # insert NaN gaps where the interval lies outside the surface
            mid = 0.5 * (ts[:-1] + ts[1:])
            zmid = self.z_at(p[0] + mid * d[0], p[1] + mid * d[1]) if len(mid) else np.array([])
            sts, zss = [ts[0]], [zs[0]]
            for k in range(len(mid)):
                if np.isnan(zmid[k]):
                    sts.append(mid[k]); zss.append(np.nan)
                sts.append(ts[k + 1]); zss.append(zs[k + 1])
            sts = np.array(sts)
            zss = np.array(zss)
            stations = base + sts * L
            if st_out and stations[0] - st_out[-1][-1] < 1e-9:
                stations, zss, sts = stations[1:], zss[1:], sts[1:]
            st_out.append(stations)
            z_out.append(zss)
            xy_out.append(np.column_stack([p[0] + sts * d[0], p[1] + sts * d[1]]))
            base += L
        if not st_out:
            return np.array([]), np.array([]), np.empty((0, 2))
        return np.concatenate(st_out), np.concatenate(z_out), np.vstack(xy_out)

    def sections(self, alignment_xy, interval: float, half_width: float, start: float = 0.0, end: float | None = None):
        """Cross-sections perpendicular to an alignment. Returns list of dicts."""
        xy = np.asarray(alignment_xy, float).reshape(-1, 2)
        total = float(G.cumulative_stations(xy)[-1])
        end = total if end is None else min(end, total)
        out = []
        s = start
        while s <= end + 1e-9:
            p, ang = G.point_at_station(xy, s)
            nx, ny = -math.sin(ang), math.cos(ang)          # left normal
            a = p - half_width * np.array([nx, ny])          # right end (offset = -half_width)
            b = p + half_width * np.array([nx, ny])
            st, z, pxy = self.profile(np.array([a, b]))
            out.append({"station": s, "center": (float(p[0]), float(p[1])), "offset": st - half_width,
                        "z": z, "xy": pxy,
                        "center_z": float(self.z_at(p[0], p[1]))})
            s += interval
        return out


# ============================================================================ helpers
@dataclass
class VolumeResult:
    cut: float
    fill: float
    area_cut: float
    area_fill: float
    area_total: float
    method: str = ""
    n_tris: int = 0

    @property
    def net(self) -> float:           # + = net cut (export), - = net fill (import)
        return self.cut - self.fill


def _split_volumes(area, h):
    """Exact ∫max(h,0) and ∫min(h,0) over triangles whose height above datum varies linearly.

    Returns (pos_vol, neg_vol, pos_area, neg_area); neg_vol is <= 0.
    """
    area = np.asarray(area, float)
    if len(area) == 0:
        z = np.zeros(0)
        return z, z, z, z
    hs = np.sort(h, axis=1)
    h1, h2, h3 = hs[:, 0], hs[:, 1], hs[:, 2]
    total = area * (h1 + h2 + h3) / 3.0
    pos = np.zeros_like(total); neg = np.zeros_like(total)
    apos = np.zeros_like(total); aneg = np.zeros_like(total)
    allpos = h1 >= 0
    allneg = h3 <= 0
    pos[allpos] = total[allpos]; apos[allpos] = area[allpos]
    neg[allneg] = total[allneg]; aneg[allneg] = area[allneg]
    mixed = ~(allpos | allneg)
    a = mixed & (h2 >= 0)                    # one vertex below the datum
    if a.any():
        t12 = h1[a] / (h1[a] - h2[a])
        t13 = h1[a] / (h1[a] - h3[a])
        frac = t12 * t13
        neg[a] = area[a] * frac * h1[a] / 3.0
        pos[a] = total[a] - neg[a]
        aneg[a] = area[a] * frac
        apos[a] = area[a] - aneg[a]
    b = mixed & (h2 < 0)                     # one vertex above the datum
    if b.any():
        t31 = h3[b] / (h3[b] - h1[b])
        t32 = h3[b] / (h3[b] - h2[b])
        frac = t31 * t32
        pos[b] = area[b] * frac * h3[b] / 3.0
        neg[b] = total[b] - pos[b]
        apos[b] = area[b] * frac
        aneg[b] = area[b] - apos[b]
    return pos, neg, apos, aneg


def _chain_segments(seg: np.ndarray, n: int):
    """Chain (a,b) node-pairs into polylines. Returns [(node_list, closed)]."""
    adj = [[] for _ in range(n)]
    for a, b in seg.tolist():
        adj[a].append(b)
        adj[b].append(a)
    visited = [False] * n
    chains = []
    for s in range(n):
        if visited[s] or len(adj[s]) != 1:
            continue
        chain = [s]
        visited[s] = True
        cur = s
        while True:
            nxt = None
            for c in adj[cur]:
                if not visited[c]:
                    nxt = c
                    break
            if nxt is None:
                break
            chain.append(nxt)
            visited[nxt] = True
            cur = nxt
        chains.append((chain, False))
    for s in range(n):
        if visited[s]:
            continue
        chain = [s]
        visited[s] = True
        cur = s
        while True:
            nxt = None
            for c in adj[cur]:
                if not visited[c]:
                    nxt = c
                    break
            if nxt is None:
                break
            chain.append(nxt)
            visited[nxt] = True
            cur = nxt
        chains.append((chain, len(chain) >= 3 and s in adj[chain[-1]]))
    return chains


def contour_levels(zmin: float, zmax: float, interval: float, base: float = 0.0) -> list[float]:
    if interval <= 0:
        return []
    k0 = math.ceil((zmin - base) / interval - 1e-9)
    k1 = math.floor((zmax - base) / interval + 1e-9)
    return [base + k * interval for k in range(k0, k1 + 1)]


def is_index_level(level: float, interval: float, every: int, base: float = 0.0) -> bool:
    if every <= 0:
        return False
    k = (level - base) / (interval * every)
    return abs(k - round(k)) < 1e-6


def suggest_max_edge(xy: np.ndarray, factor: float = 8.0) -> float:
    """A reasonable "maximum triangle side" based on point spacing."""
    if len(xy) < 4:
        return 0.0
    tree = cKDTree(xy[: min(len(xy), 20000)])
    d, _ = tree.query(xy[: min(len(xy), 20000)], k=2)
    return float(np.median(d[:, 1]) * factor)


# ============================================================================ build
@dataclass
class BuildReport:
    n_input: int = 0
    n_duplicates: int = 0
    n_breakline_vertices: int = 0
    n_added: int = 0
    n_triangles: int = 0
    n_removed_edge: int = 0
    n_removed_boundary: int = 0
    n_unenforced: int = 0
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return dict(self.__dict__)


class _Verts:
    def __init__(self, tol: float):
        self.tol = tol
        self.x: list[float] = []
        self.y: list[float] = []
        self.z: list[float] = []
        self.map: dict = {}

    def key(self, x, y):
        return (int(round(x / self.tol)), int(round(y / self.tol)))

    def add(self, x, y, z, prefer=False) -> int:
        k = self.key(x, y)
        i = self.map.get(k)
        if i is None:
            i = len(self.x)
            self.x.append(float(x)); self.y.append(float(y)); self.z.append(float(z))
            self.map[k] = i
        elif prefer and not math.isnan(z):
            self.z[i] = float(z)
        return i

    def arrays(self):
        return np.array(self.x), np.array(self.y), np.array(self.z)


def _fill_nan_z(v: np.ndarray) -> np.ndarray | None:
    """Interpolate missing z along a polyline from its known vertices; None if none known."""
    v = v.copy()
    z = v[:, 2]
    ok = np.isfinite(z)
    if not ok.any():
        return None
    if ok.all():
        return v
    d = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(v[:, :2], axis=0).T))])
    z[~ok] = np.interp(d[~ok], d[ok], z[ok])
    return v


def build_tin(points, breaklines=(), boundaries=(), holes=(), max_edge: float | None = None,
              dup_tol: float | None = None, max_iter: int = 40) -> tuple[TIN, BuildReport]:
    """Build a TIN.

    points      (N,3) array of x,y,z
    breaklines  iterable of (n,3) arrays (z may be NaN for some vertices)
    boundaries  iterable of (n,2|3) arrays - triangles outside ALL boundaries are removed
    holes       iterable of (n,2|3) arrays - triangles inside any hole are removed
    max_edge    drop triangles having any side longer than this (2D)
    dup_tol     points are merged by *snapping to a grid of this size*, not by true distance:
                two points 0.9*tol apart can survive as separate vertices while two 1.1*tol
                apart can merge.  The default (extent * 1e-9) only catches exact duplicates,
                which is what you want; a large user-supplied value is "same cell", not
                "within this distance".
    """
    rep = BuildReport()
    P = np.asarray(points, float).reshape(-1, 3)
    P = P[np.isfinite(P).all(axis=1)]
    rep.n_input = len(P)
    if len(P) < 3:
        raise ValueError("Need at least 3 points with elevations to build a surface.")
    ext = max(np.ptp(P[:, 0]), np.ptp(P[:, 1]), 1.0)
    tol = dup_tol if dup_tol else max(1e-4, ext * 1e-9)
    V = _Verts(tol)

    # --- unique ground points
    keys = np.round(P[:, :2] / tol).astype(np.int64)
    _, first = np.unique(keys, axis=0, return_index=True)
    first.sort()
    rep.n_duplicates = len(P) - len(first)
    ground_pts = P[first]
    for x, y, z in ground_pts:
        V.add(x, y, z)

    ground_tin: TIN | None = None
    ground_tree: cKDTree | None = None

    def _drape_xy(xy: np.ndarray) -> np.ndarray:
        nonlocal ground_tin, ground_tree
        z_out = np.full(len(xy), np.nan, dtype=float)
        if len(ground_pts) >= 3:
            if ground_tin is None:
                try:
                    ground_tin = TIN(ground_pts.copy(), Delaunay(ground_pts[:, :2]).simplices.astype(np.int64))
                except Exception:
                    ground_tin = None
            if ground_tin is not None:
                z_out = np.asarray(ground_tin.z_at(xy[:, 0], xy[:, 1]), dtype=float)
        miss = ~np.isfinite(z_out)
        if miss.any() and len(ground_pts) > 0:
            if ground_tree is None:
                ground_tree = cKDTree(ground_pts[:, :2])
            _, nn_idx = ground_tree.query(xy[miss], k=1)
            z_out[miss] = ground_pts[np.asarray(nn_idx, dtype=int), 2]
        return z_out

    # --- breaklines
    # Phase 2 (§2.2): 2D polylines / vertices sitting at z == 0 (or NaN) are treated as
    # elevation-less: they still break the TIN, taking elevations from underlying survey
    # points or draping onto the ground surface rather than cutting 0.00 cliffs.
    segs: list[tuple[int, int]] = []
    nbl = 0
    for bl in breaklines:
        b_in = np.asarray(bl, float).reshape(-1, 3 if np.ndim(bl) > 1 and np.shape(bl)[1] >= 3 else 2)
        if len(b_in) < 2:
            continue
        if b_in.shape[1] == 2:
            b_in = np.column_stack([b_in, np.full(len(b_in), np.nan)])
        else:
            b_in = b_in[:, :3].copy()

        orig_z = b_in[:, 2].copy()
        has_own_z = np.isfinite(orig_z) & (np.abs(orig_z) > 1e-12)
        for vi in range(len(b_in)):
            if not has_own_z[vi]:
                k = V.key(float(b_in[vi, 0]), float(b_in[vi, 1]))
                existing = V.map.get(k)
                if existing is not None and math.isfinite(V.z[existing]):
                    b_in[vi, 2] = V.z[existing]
                else:
                    b_in[vi, 2] = np.nan

        b = _fill_nan_z(b_in)
        if b is None:
            draped_z = _drape_xy(b_in[:, :2])
            if not np.isfinite(draped_z).all():
                rep.warnings.append("A breakline without any elevations was skipped.")
                continue
            b = np.column_stack([b_in[:, :2], draped_z])

        idx = [
            V.add(float(x), float(y), float(z), prefer=bool(own))
            for (x, y, z), own in zip(b, has_own_z)
        ]
        nbl += len(idx)
        for i, j in zip(idx[:-1], idx[1:]):
            if i != j:
                segs.append((i, j))
    rep.n_breakline_vertices = nbl
    n_base = len(V.x)

    if segs:
        segs = _split_at_vertices(segs, V, tol)
        segs = _split_crossings(segs, V, tol, rep)

    X, Y, Z = V.arrays()
    XY = np.column_stack([X, Y])
    center = XY.mean(axis=0)
    seg_arr = np.array(segs, dtype=np.int64).reshape(-1, 2)
    tri, X, Y, Z, seg_arr, nmiss = _conforming_delaunay(X, Y, Z, seg_arr, center, max_iter)
    rep.n_added = len(X) - n_base
    rep.n_unenforced = nmiss
    if nmiss:
        rep.warnings.append(f"{nmiss} breakline segment(s) could not be enforced exactly.")
    tris = tri.simplices.astype(np.int64)

    # --- orientation + degenerate removal
    ax, ay = X[tris[:, 0]], Y[tris[:, 0]]
    cross = (X[tris[:, 1]] - ax) * (Y[tris[:, 2]] - ay) - (X[tris[:, 2]] - ax) * (Y[tris[:, 1]] - ay)
    flip = cross < 0
    tris[flip] = tris[flip][:, [0, 2, 1]]
    keep = np.abs(cross) > 1e-9 * ext * ext * 1e-3
    tris = tris[keep]

    # --- clip: max edge, boundaries, holes
    if len(tris):
        keep = np.ones(len(tris), bool)
        if max_edge and max_edge > 0:
            e0 = np.hypot(X[tris[:, 0]] - X[tris[:, 1]], Y[tris[:, 0]] - Y[tris[:, 1]])
            e1 = np.hypot(X[tris[:, 1]] - X[tris[:, 2]], Y[tris[:, 1]] - Y[tris[:, 2]])
            e2 = np.hypot(X[tris[:, 2]] - X[tris[:, 0]], Y[tris[:, 2]] - Y[tris[:, 0]])
            ok = np.maximum(np.maximum(e0, e1), e2) <= max_edge
            rep.n_removed_edge = int((~ok).sum())
            keep &= ok
        cx = (X[tris[:, 0]] + X[tris[:, 1]] + X[tris[:, 2]]) / 3.0
        cy = (Y[tris[:, 0]] + Y[tris[:, 1]] + Y[tris[:, 2]]) / 3.0
        boundaries = [np.asarray(b, float)[:, :2] for b in boundaries if len(b) >= 3]
        holes = [np.asarray(h, float)[:, :2] for h in holes if len(h) >= 3]
        if boundaries:
            inside = np.zeros(len(tris), bool)
            for b in boundaries:
                inside |= shapely.contains_xy(shapely.Polygon(b), cx, cy)
            rep.n_removed_boundary += int((keep & ~inside).sum())
            keep &= inside
        for h in holes:
            inh = shapely.contains_xy(shapely.Polygon(h), cx, cy)
            rep.n_removed_boundary += int((keep & inh).sum())
            keep &= ~inh
        tris = tris[keep]
    if len(tris) == 0:
        raise ValueError("No triangles were produced - check boundaries / maximum edge length.")

    used = np.unique(tris)
    remap = np.full(len(X), -1, np.int64)
    remap[used] = np.arange(len(used))
    pts = np.column_stack([X[used], Y[used], Z[used]])
    tris = remap[tris]
    rep.n_triangles = len(tris)
    return TIN(pts, tris), rep


def _conforming_delaunay(X, Y, Z, segs, center, max_iter):
    X, Y, Z = X.copy(), Y.copy(), Z.copy()
    segs = segs.copy()
    for it in range(max_iter + 1):
        tri = Delaunay(np.column_stack([X - center[0], Y - center[1]]))
        if len(segs) == 0:
            return tri, X, Y, Z, segs, 0
        S = tri.simplices
        N = len(X)
        e = np.concatenate([S[:, [0, 1]], S[:, [1, 2]], S[:, [2, 0]]])
        ek = np.minimum(e[:, 0], e[:, 1]).astype(np.int64) * N + np.maximum(e[:, 0], e[:, 1])
        sk = np.minimum(segs[:, 0], segs[:, 1]).astype(np.int64) * N + np.maximum(segs[:, 0], segs[:, 1])
        missing = ~np.isin(sk, ek)
        nmiss = int(missing.sum())
        if nmiss == 0 or it == max_iter:
            return tri, X, Y, Z, segs, nmiss
        ms = segs[missing]
        mx = 0.5 * (X[ms[:, 0]] + X[ms[:, 1]])
        my = 0.5 * (Y[ms[:, 0]] + Y[ms[:, 1]])
        mz = 0.5 * (Z[ms[:, 0]] + Z[ms[:, 1]])
        new_idx = np.arange(N, N + len(ms))
        X = np.concatenate([X, mx]); Y = np.concatenate([Y, my]); Z = np.concatenate([Z, mz])
        keep = segs[~missing]
        s1 = np.column_stack([ms[:, 0], new_idx])
        s2 = np.column_stack([new_idx, ms[:, 1]])
        segs = np.vstack([keep, s1, s2])
    return tri, X, Y, Z, segs, 0


def _split_at_vertices(segs, V: _Verts, tol):
    """Split breakline segments at any vertex lying on their interior."""
    X, Y, Z = V.arrays()
    xy = np.column_stack([X, Y])
    sa = np.array(segs)
    lines = shapely.linestrings(np.stack([xy[sa[:, 0]], xy[sa[:, 1]]], axis=1))
    ptree = STRtree(shapely.points(xy))
    # STRtree.query(geoms, predicate=..) returns (input_idx, tree_idx)
    li, pj = ptree.query(lines, predicate="dwithin", distance=2 * tol)
    splits: dict[int, list] = {}
    for s_i, v_i in zip(li, pj):
        a, b = sa[s_i]
        if v_i == a or v_i == b:
            continue
        p0, p1 = xy[a], xy[b]
        d = p1 - p0
        L2 = float(d @ d)
        if L2 == 0:
            continue
        t = float(((xy[v_i] - p0) @ d) / L2)
        if 1e-9 < t < 1 - 1e-9:
            splits.setdefault(int(s_i), []).append((t, int(v_i)))
    if not splits:
        return segs
    out = []
    for k, (a, b) in enumerate(segs):
        if k in splits:
            chain = [a] + [v for _, v in sorted(set(splits[k]))] + [b]
            out.extend((i, j) for i, j in zip(chain[:-1], chain[1:]) if i != j)
        else:
            out.append((a, b))
    return out


def _split_crossings(segs, V: _Verts, tol, rep: BuildReport):
    """Node breaklines that cross each other in plan (Z averaged)."""
    if len(segs) < 2:
        return segs
    for _round in range(3):
        X, Y, Z = V.arrays()
        xy = np.column_stack([X, Y])
        sa = np.array(segs)
        lines = shapely.linestrings(np.stack([xy[sa[:, 0]], xy[sa[:, 1]]], axis=1))
        tree = STRtree(lines)
        qi, ti = tree.query(lines, predicate="intersects")
        m = qi < ti
        splits: dict[int, list] = {}
        zconf = 0
        for a, b in zip(qi[m], ti[m]):
            sa_, sb_ = sa[a], sa[b]
            if len({sa_[0], sa_[1], sb_[0], sb_[1]}) < 4:
                continue                                   # share a vertex - fine
            p, q = xy[sa_[0]], xy[sb_[0]]
            r, s = xy[sa_[1]] - p, xy[sb_[1]] - q
            res = G.segment_intersection(p, r, q, s)
            if res is None:
                continue
            t, u = res
            e1, e2 = 1e-9, 1e-9
            if not (e1 < t < 1 - e1 and e2 < u < 1 - e2):
                continue
            ix, iy = p[0] + t * r[0], p[1] + t * r[1]
            za = Z[sa_[0]] + t * (Z[sa_[1]] - Z[sa_[0]])
            zb = Z[sb_[0]] + u * (Z[sb_[1]] - Z[sb_[0]])
            if abs(za - zb) > 0.01:
                zconf += 1
            idx = V.add(ix, iy, 0.5 * (za + zb), prefer=True)
            splits.setdefault(int(a), []).append((t, idx))
            splits.setdefault(int(b), []).append((u, idx))
        if zconf:
            rep.warnings.append(f"{zconf} crossing breakline pair(s) had different elevations at the crossing; averaged.")
        if not splits:
            return segs
        out = []
        for k, (a, b) in enumerate(segs):
            if k in splits:
                chain = [a] + [v for _, v in sorted(set(splits[k]))] + [b]
                out.extend((i, j) for i, j in zip(chain[:-1], chain[1:]) if i != j)
            else:
                out.append((a, b))
        segs = out
    return segs


# ============================================================================ project integration
def gather_surface_inputs(project, params: dict):
    """Collect (points xyz, breaklines, boundaries, holes) for a surface definition."""
    from .featurecodes import default_codes, parse_description
    ids, xyz = project.point_arrays()
    mask = np.isfinite(xyz[:, 2]) if len(ids) else np.zeros(0, bool)
    # A group that is switched off does not feed a surface either: a fence line the drawing is
    # not showing must not still be holding up (or cutting through) a TIN (core/groups.py).
    hidden = project.hidden_ids()
    layers = params.get("layers")
    excl = set(params.get("exclude_layers") or [])
    sel = params.get("point_ids")
    ground_only = params.get("ground_only", True)
    code_table = project.codes if len(project.codes) else default_codes()
    for k, pid in enumerate(ids):
        if not mask[k]:
            continue
        p = project.points[int(pid)]
        if p.id in hidden:
            mask[k] = False
        elif sel is not None and p.id not in sel:
            mask[k] = False
        elif layers and p.layer not in layers:
            mask[k] = False
        elif p.layer in excl:
            mask[k] = False
        elif ground_only:
            fc = code_table.get(parse_description(
                p.desc, commands=project.settings.get("f2f_commands"),
                known_codes=code_table.codes).code)
            if fc is not None and not fc.ground:
                mask[k] = False
    pts = xyz[mask]

    bl_layers = set(params.get("breakline_layers") or [])
    bl_ids = set(params.get("breakline_ids") or [])
    use_kind = params.get("use_breakline_kind", True)
    breaks = []
    for e in project.polylines():
        if e.id in hidden or len(e.verts) < 2:
            continue
        if (use_kind and e.kind == "breakline") or e.layer in bl_layers or e.id in bl_ids:
            verts = np.asarray(e.verts, dtype=float).copy()
            raw_pids = (e.attrs or {}).get("point_ids")
            if isinstance(raw_pids, (list, tuple)) and len(raw_pids) == len(verts):
                for vi, pid in enumerate(raw_pids):
                    if not (math.isfinite(verts[vi, 2]) and abs(float(verts[vi, 2])) > 1e-12):
                        try:
                            pt = project.points.get(int(pid)) if pid is not None else None
                        except (TypeError, ValueError):
                            pt = None
                        if pt is not None and math.isfinite(pt.z) and abs(float(pt.z)) > 1e-12:
                            verts[vi, 2] = float(pt.z)
            v = G.flatten_polyline(verts, e.bulges, e.closed, max_dev=0.05)
            if e.closed and len(v) >= 2:
                v = np.vstack([v, v[:1]])
            breaks.append(v)
    bounds = [project.entities[i].verts for i in params.get("boundary_ids", [])
              if i in project.entities and i not in hidden]
    holes = [project.entities[i].verts for i in params.get("hole_ids", [])
             if i in project.entities and i not in hidden]
    return pts, breaks, bounds, holes


def surface_signature(pts, breaks, bounds, holes, max_edge) -> str:
    """Fingerprint of everything a surface was built from (so we can tell when it is out of date)."""
    import hashlib
    h = hashlib.md5()
    h.update(np.ascontiguousarray(np.round(np.asarray(pts, float), 6)).tobytes())
    for group in (breaks, bounds, holes):
        h.update(str(len(group)).encode())
        for a in group:
            h.update(np.ascontiguousarray(np.round(np.asarray(a, float), 6)).tobytes())
    h.update(repr(max_edge or 0).encode())
    return h.hexdigest()


def build_surface_from_project(project, name: str, params: dict):
    from .model import Surface
    pts, breaks, bounds, holes = gather_surface_inputs(project, params)
    me = params.get("max_edge") or None
    tin, rep = build_tin(pts, breaks, bounds, holes, me)
    rd = rep.to_dict()
    rd["sig"] = surface_signature(pts, breaks, bounds, holes, me)
    sf = Surface(0, name, tin.pts, tin.tris, dict(params), report=rd)
    sf._tin = tin
    return sf, rep


def rebuild_surface(project, surface) -> BuildReport:
    pts, breaks, bounds, holes = gather_surface_inputs(project, surface.params)
    me = surface.params.get("max_edge") or None
    tin, rep = build_tin(pts, breaks, bounds, holes, me)
    surface.set_geometry(tin.pts, tin.tris)
    surface._tin = tin
    surface.stale = False
    surface.report = rep.to_dict()
    surface.report["sig"] = surface_signature(pts, breaks, bounds, holes, me)
    return rep


def is_stale(project, surface) -> bool:
    """True when the points / breaklines a surface was built from have changed since (imported surfaces never are)."""
    sig = (surface.report or {}).get("sig")
    if not sig or surface.params.get("source"):
        return False
    pts, breaks, bounds, holes = gather_surface_inputs(project, surface.params)
    return surface_signature(pts, breaks, bounds, holes, surface.params.get("max_edge") or None) != sig


def compute_contour_data(surface, interval: float, base: float = 0.0, smooth: int = 0):
    """Pure computation (safe on a worker thread): [(level, verts, closed)] for a surface."""
    tin = surface.tin()
    zmin, zmax = tin.bounds()[2], tin.bounds()[5]
    return tin.contours(contour_levels(zmin, zmax, interval, base), smooth)


def apply_contours(project, surface, data, interval: float, index_every: int = 5, base: float = 0.0,
                   labels: bool = True, text_height: float | None = None, min_length: float | None = None) -> dict:
    """Replace the generated contours of `surface` with `data` (call on the thread that owns the project)."""
    tag = f"contours:{surface.name}"
    project.remove_derived(tag)
    tin = surface.tin()
    zmin, zmax = tin.bounds()[2], tin.bounds()[5]
    levels = contour_levels(zmin, zmax, interval, base)
    project.ensure_layer("CONTOUR-MINOR", (150, 110, 60))
    project.ensure_layer("CONTOUR-INDEX", (230, 160, 70))
    project.ensure_layer("CONTOUR-LABEL", (255, 220, 160))
    th = text_height or project.settings.get("text_height", 2.0)
    n_lines = n_labels = 0
    for level, verts, closed in data:
        L = G.polyline_length(verts, None, closed)
        if min_length and L < min_length:
            continue
        idx = is_index_level(level, interval, index_every, base)
        project.add_polyline(verts, "CONTOUR-INDEX" if idx else "CONTOUR-MINOR", closed, None, "contour",
                             tag, {"elevation": level, "index": idx})
        n_lines += 1
        if labels and idx:
            n_labels += _place_labels(project, verts, closed, level, th, tag)
    project.touch()
    return {"levels": len(levels), "lines": n_lines, "labels": n_labels, "min": zmin, "max": zmax}


def generate_contours(project, surface, interval: float, index_every: int = 5, base: float = 0.0,
                      smooth: int = 0, labels: bool = True, text_height: float | None = None,
                      min_length: float | None = None) -> dict:
    """Create contour polylines (+ index labels) as generated entities for a surface."""
    data = compute_contour_data(surface, interval, base, smooth)
    return apply_contours(project, surface, data, interval, index_every, base, labels, text_height, min_length)


def _fmt_level(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s


def _place_labels(project, verts, closed, level, th, tag) -> int:
    xy = verts[:, :2]
    if closed:
        xy = np.vstack([xy, xy[:1]])
    cum = G.cumulative_stations(xy)
    total = float(cum[-1])
    text = _fmt_level(level)
    lab_len = th * 0.7 * len(text) + th
    if total < 4 * lab_len:
        return 0
    spacing = max(60 * th, 6 * lab_len)
    n_side = int((total * 0.5 - 2 * lab_len) // spacing)
    stations = [total * 0.5] + [total * 0.5 + k * spacing for k in range(1, n_side + 1)] + \
               [total * 0.5 - k * spacing for k in range(1, n_side + 1)]
    cnt = 0
    for s in stations:
        p, ang = G.point_at_station(xy, s)
        deg = math.degrees(ang)
        if deg > 90:
            deg -= 180
        elif deg <= -90:
            deg += 180
        project.add_text(p[0], p[1], text, th, deg, "CONTOUR-LABEL", tag, attrs={"elevation": level})
        cnt += 1
    return cnt


def volume_between(existing: TIN, proposed: TIN, region=None, method: str = "composite", cell: float | None = None):
    """Cut (existing above proposed) / fill between two surfaces over their common area."""
    if method == "grid":
        ex0, ey0, _, ex1, ey1, _ = existing.bounds()
        px0, py0, _, px1, py1, _ = proposed.bounds()
        x0, y0, x1, y1 = max(ex0, px0), max(ey0, py0), min(ex1, px1), min(ey1, py1)
        if x1 <= x0 or y1 <= y0:
            return VolumeResult(0, 0, 0, 0, 0, "grid (no overlap)", 0)
        cell = cell or max((x1 - x0), (y1 - y0)) / 400.0
        xs = np.arange(x0 + cell / 2, x1, cell)
        ys = np.arange(y0 + cell / 2, y1, cell)
        X, Y = np.meshgrid(xs, ys)
        ze, zp = existing.z_at(X, Y), proposed.z_at(X, Y)
        dz = ze - zp
        ok = np.isfinite(dz)
        if region is not None:
            ok &= shapely.contains_xy(region, X, Y)
        a = cell * cell
        cut = float(dz[ok & (dz > 0)].sum() * a)
        fill = float(-dz[ok & (dz < 0)].sum() * a)
        return VolumeResult(cut, fill, float((ok & (dz > 0)).sum() * a), float((ok & (dz < 0)).sum() * a),
                            float(ok.sum() * a), f"grid {cell:g}", int(ok.sum()))
    # composite: union of vertices, difference surface triangulated over the common area
    allpts = np.vstack([existing.pts[:, :2], proposed.pts[:, :2]])
    tol = 1e-6 * max(np.ptp(allpts[:, 0]), np.ptp(allpts[:, 1]), 1.0)
    _, u = np.unique(np.round(allpts / max(tol, 1e-9)).astype(np.int64), axis=0, return_index=True)
    allpts = allpts[np.sort(u)]
    ze, zp = existing.z_at(allpts[:, 0], allpts[:, 1]), proposed.z_at(allpts[:, 0], allpts[:, 1])
    ok = np.isfinite(ze) & np.isfinite(zp)
    allpts, dz = allpts[ok], (ze - zp)[ok]
    if len(allpts) < 3:
        return VolumeResult(0, 0, 0, 0, 0, "composite (no overlap)", 0)
    c = allpts.mean(axis=0)
    tri = Delaunay(allpts - c).simplices
    cx = allpts[tri, 0].mean(axis=1)
    cy = allpts[tri, 1].mean(axis=1)
    good = np.isfinite(existing.z_at(cx, cy)) & np.isfinite(proposed.z_at(cx, cy))
    tri = tri[good]
    pseudo = TIN(np.column_stack([allpts, dz]), tri)
    res = pseudo.volume_to_datum(0.0, region)
    res.method = "composite TIN (union of vertices)" + (" clipped to region" if region is not None else "")
    return res
