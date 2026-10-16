"""Spatial index for picking, snapping and box-selection (numpy / KD-tree based)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from . import geometry as G
from .model import Polyline, TextEntity


@dataclass
class SnapHit:
    kind: str                 # point | node | mid | nearest
    x: float
    y: float
    z: float = float("nan")
    ref: tuple | None = None  # ("pt", id) | ("en", id, vertex_index)


def _liang_barsky(A: np.ndarray, B: np.ndarray, xmin, ymin, xmax, ymax) -> np.ndarray:
    """Boolean per segment: does A->B intersect the rectangle?"""
    d = B - A
    t0 = np.zeros(len(A))
    t1 = np.ones(len(A))
    ok = np.ones(len(A), bool)
    for p, q in ((-d[:, 0], A[:, 0] - xmin), (d[:, 0], xmax - A[:, 0]),
                 (-d[:, 1], A[:, 1] - ymin), (d[:, 1], ymax - A[:, 1])):
        parallel = p == 0
        with np.errstate(divide="ignore", invalid="ignore"):
            r = q / p
        ok &= ~(parallel & (q < 0))
        neg = (p < 0) & ~parallel
        pos = (p > 0) & ~parallel
        t0 = np.where(neg, np.maximum(t0, r), t0)
        t1 = np.where(pos, np.minimum(t1, r), t1)
    return ok & (t0 <= t1)


class SpatialIndex:
    """Snapshot of the visible, pickable geometry in a project."""

    def __init__(self, project, include_locked: bool = False):
        self.rev = project.revision
        vis = {n for n, l in project.layers.items() if l.visible and (include_locked or not l.locked)}
        ids, xy, zs = [], [], []
        for p in project.points.values():
            if p.layer in vis:
                ids.append(p.id); xy.append((p.x, p.y)); zs.append(p.z)
        self.pt_ids = np.array(ids, np.int64)
        self.pt_xy = np.array(xy, float).reshape(-1, 2)
        self.pt_z = np.array(zs, float)
        self.pt_tree = cKDTree(self.pt_xy) if len(ids) else None

        segA, segB, segOwner, segVidx = [], [], [], []
        nodes, nodeOwner, nodeVidx, nodeZ = [], [], [], []
        ent_ids, ent_bbox, vtx_xy, vtx_owner = [], [], [], []
        tx_ids, tx_xy = [], []
        for e in project.entities.values():
            if e.layer not in vis:
                continue
            if isinstance(e, Polyline) and len(e.verts) >= 1:
                v, flags = G.flatten_with_node_flags(e.verts, e.bulges, e.closed)
                if e.closed and len(v) > 1:
                    v = np.vstack([v, v[:1]])
                    flags = np.concatenate([flags, [False]])
                k = len(ent_ids)
                ent_ids.append(e.id)
                vx, vy = v[:, 0], v[:, 1]
                x0, y0, x1, y1 = float(vx.min()), float(vy.min()), float(vx.max()), float(vy.max())
                if np.isnan(x0) or np.isnan(y0) or np.isnan(x1) or np.isnan(y1):
                    x0, y0, x1, y1 = float(np.nanmin(vx)), float(np.nanmin(vy)), float(np.nanmax(vx)), float(np.nanmax(vy))
                ent_bbox.append((x0, y0, x1, y1))
                orig = e.verts
                vtx_xy.append(orig[:, :2]); vtx_owner.append(np.full(len(orig), k))
                oi = np.cumsum(flags) - 1
                if len(v) > 1:
                    segA.append(v[:-1, :2]); segB.append(v[1:, :2])
                    segOwner.append(np.full(len(v) - 1, e.id)); segVidx.append(np.maximum(oi[:-1], 0))
                mask = flags
                nodes.append(v[mask, :2]); nodeZ.append(v[mask, 2])
                nodeOwner.append(np.full(int(mask.sum()), e.id))
                nodeVidx.append(oi[mask])
            elif isinstance(e, TextEntity):
                tx_ids.append(e.id); tx_xy.append((e.x, e.y))

        def cat(lst, shape=None, dtype=float):
            if lst:
                return np.concatenate(lst)
            return np.empty(shape or (0,), dtype)

        self.segA = cat(segA, (0, 2)); self.segB = cat(segB, (0, 2))
        self.segOwner = cat(segOwner, dtype=np.int64); self.segVidx = cat(segVidx, dtype=np.int64)
        self.node_xy = cat(nodes, (0, 2)); self.node_z = cat(nodeZ)
        self.node_owner = cat(nodeOwner, dtype=np.int64); self.node_vidx = cat(nodeVidx, dtype=np.int64)
        self.node_tree = cKDTree(self.node_xy) if len(self.node_xy) else None
        self.ent_ids = np.array(ent_ids, np.int64)
        self.ent_bbox = np.array(ent_bbox, float).reshape(-1, 4)
        self.vtx_xy = cat(vtx_xy, (0, 2)); self.vtx_owner = cat(vtx_owner, dtype=np.int64)
        self.tx_ids = np.array(tx_ids, np.int64)
        self.tx_xy = np.array(tx_xy, float).reshape(-1, 2)
        if len(self.segA):
            self.seg_min = np.minimum(self.segA, self.segB)
            self.seg_max = np.maximum(self.segA, self.segB)
            self.mid = 0.5 * (self.segA + self.segB)
            self.mid_tree = cKDTree(self.mid)
        else:
            self.seg_min = self.seg_max = self.mid = np.empty((0, 2)); self.mid_tree = None

    # ------------------------------------------------------------------ nearest queries
    def nearest_point(self, x, y, tol):
        if self.pt_tree is None:
            return None
        d, i = self.pt_tree.query([x, y], distance_upper_bound=tol)
        if not np.isfinite(d):
            return None
        return int(self.pt_ids[i]), float(d)

    def nearest_node(self, x, y, tol):
        if self.node_tree is None:
            return None
        d, i = self.node_tree.query([x, y], distance_upper_bound=tol)
        if not np.isfinite(d):
            return None
        return i, float(d)

    def nearest_segment(self, x, y, tol):
        if not len(self.segA):
            return None
        m = ((self.seg_max[:, 0] >= x - tol) & (self.seg_min[:, 0] <= x + tol) &
             (self.seg_max[:, 1] >= y - tol) & (self.seg_min[:, 1] <= y + tol))
        idx = np.nonzero(m)[0]
        if not len(idx):
            return None
        d, t, qx, qy = G.closest_on_segments(x, y, self.segA[idx], self.segB[idx])
        k = int(np.argmin(d))
        if d[k] > tol:
            return None
        return int(idx[k]), float(d[k]), float(qx[k]), float(qy[k]), float(t[k])

    def nearest_text(self, x, y, tol):
        if not len(self.tx_ids):
            return None
        d = np.hypot(self.tx_xy[:, 0] - x, self.tx_xy[:, 1] - y)
        k = int(np.argmin(d))
        return (int(self.tx_ids[k]), float(d[k])) if d[k] <= tol else None

    # ------------------------------------------------------------------ pick / snap
    def pick(self, x, y, tol):
        """Closest pickable object -> ("pt", id) | ("en", id) | None.

        A survey point inside the tolerance always wins over a line passing through it (the usual CAD rule) -
        otherwise points sitting on linework could never be selected.
        """
        r = self.nearest_point(x, y, tol)
        if r:
            return ("pt", r[0])
        best = None
        s = self.nearest_segment(x, y, tol)
        if s:
            best = (s[1], ("en", int(self.segOwner[s[0]])))
        t = self.nearest_text(x, y, tol)
        if t and (best is None or t[1] < best[0]):
            best = (t[1], ("en", t[0]))
        return best[1] if best else None

    def snap(self, x, y, tol, modes=("point", "node", "mid", "nearest")) -> SnapHit | None:
        hits = []
        if "point" in modes:
            r = self.nearest_point(x, y, tol)
            if r:
                k = int(np.nonzero(self.pt_ids == r[0])[0][0])
                hits.append((r[1], 0, SnapHit("point", *self.pt_xy[k], float(self.pt_z[k]), ("pt", r[0]))))
        if "node" in modes:
            r = self.nearest_node(x, y, tol)
            if r:
                i, d = r
                hits.append((d, 0, SnapHit("node", *self.node_xy[i], float(self.node_z[i]),
                                           ("en", int(self.node_owner[i]), int(self.node_vidx[i])))))
        if hits:
            hits.sort(key=lambda h: h[0])
            return hits[0][2]
        if "mid" in modes and self.mid_tree is not None:
            d, i = self.mid_tree.query([x, y], distance_upper_bound=tol)
            if np.isfinite(d):
                return SnapHit("mid", float(self.mid[i, 0]), float(self.mid[i, 1]), float("nan"),
                               ("en", int(self.segOwner[i])))
        if "nearest" in modes:
            s = self.nearest_segment(x, y, tol)
            if s:
                return SnapHit("nearest", s[2], s[3], float("nan"), ("en", int(self.segOwner[s[0]])))
        return None

    # ------------------------------------------------------------------ box selection
    def select_box(self, x0, y0, x1, y1, crossing: bool):
        xmin, xmax = sorted((x0, x1))
        ymin, ymax = sorted((y0, y1))
        pts = set()
        if len(self.pt_ids):
            m = ((self.pt_xy[:, 0] >= xmin) & (self.pt_xy[:, 0] <= xmax) &
                 (self.pt_xy[:, 1] >= ymin) & (self.pt_xy[:, 1] <= ymax))
            pts = set(self.pt_ids[m].tolist())
        ents = set()
        if len(self.ent_ids):
            if crossing:
                if len(self.segA):
                    hit = _liang_barsky(self.segA, self.segB, xmin, ymin, xmax, ymax)
                    ents |= set(np.unique(self.segOwner[hit]).tolist())
            else:
                inside = ((self.vtx_xy[:, 0] >= xmin) & (self.vtx_xy[:, 0] <= xmax) &
                          (self.vtx_xy[:, 1] >= ymin) & (self.vtx_xy[:, 1] <= ymax))
                n = len(self.ent_ids)
                tot = np.bincount(self.vtx_owner, minlength=n)
                got = np.bincount(self.vtx_owner[inside], minlength=n)
                ok = (tot > 0) & (got == tot)
                ents |= set(self.ent_ids[ok].tolist())
        if len(self.tx_ids):
            m = ((self.tx_xy[:, 0] >= xmin) & (self.tx_xy[:, 0] <= xmax) &
                 (self.tx_xy[:, 1] >= ymin) & (self.tx_xy[:, 1] <= ymax))
            ents |= set(self.tx_ids[m].tolist())
        return pts, ents
