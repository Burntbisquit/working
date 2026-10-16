"""2D/3D geometry helpers: arcs from DXF-style bulges, polyline length/area, smoothing."""
from __future__ import annotations

import math

import numpy as np

NAN = float("nan")


# --------------------------------------------------------------------------- arcs
def bulge_to_arc(x0, y0, x1, y1, bulge):
    """Arc from (x0,y0) to (x1,y1) described by a DXF bulge (tan(theta/4), + = CCW).

    Returns (cx, cy, r, a0, theta): centre, radius, start angle (rad) and signed sweep (rad).
    """
    dx, dy = x1 - x0, y1 - y0
    chord = math.hypot(dx, dy)
    theta = 4.0 * math.atan(bulge)
    if chord == 0 or abs(bulge) < 1e-14:
        return None
    half = theta / 2.0
    r = abs(chord / (2.0 * math.sin(half)))
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    nx, ny = -dy / chord, dx / chord  # left normal
    d = (chord / 2.0) / math.tan(half)
    cx, cy = mx + nx * d, my + ny * d
    a0 = math.atan2(y0 - cy, x0 - cx)
    return cx, cy, r, a0, theta


def arc_to_bulge(cx, cy, x0, y0, x1, y1, ccw: bool) -> float:
    """Bulge for an arc about (cx,cy) from start to end travelling CCW/CW.

    A full circle (start angle == end angle) cannot be expressed as one DXF bulge - the sweep
    would be 2*pi and the bulge tan(pi/2), a near-infinite number that would produce nonsense
    downstream.  Callers must split it into two semicircular segments instead.
    """
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if ccw:
        sweep = (a1 - a0) % (2 * math.pi)
    else:
        sweep = -((a0 - a1) % (2 * math.pi))
    if abs(sweep) < 1e-12:
        raise ValueError("a full circle cannot be written as a single bulge - use two segments")
    return math.tan(sweep / 4.0)


def bulge_from_three_points(p0, p_mid, p1) -> float:
    """Return the DXF bulge (tan(theta/4), + = CCW) for the circular arc through p0 -> p_mid -> p1."""
    x0, y0 = float(p0[0]), float(p0[1])
    xm, ym = float(p_mid[0]), float(p_mid[1])
    x1, y1 = float(p1[0]), float(p1[1])
    ux, uy = xm - x0, ym - y0
    vx, vy = x1 - xm, y1 - ym
    lu = math.hypot(ux, uy)
    lv = math.hypot(vx, vy)
    if lu <= 1e-12 or lv <= 1e-12:
        return 0.0
    cross = ux * vy - uy * vx
    dot = ux * vx + uy * vy
    denom = lu * lv + dot
    if abs(denom) <= 1e-12:
        return 0.0
    return float(cross / denom)


def _arc_samples(x0, y0, z0, x1, y1, z1, bulge, max_dev, max_step):
    arc = bulge_to_arc(x0, y0, x1, y1, bulge)
    if arc is None:
        return np.empty((0, 3))
    cx, cy, r, a0, theta = arc
    step = max_step
    if max_dev and r > max_dev:
        step = min(step, 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - max_dev / r))))
    n = int(max(2, min(720, math.ceil(abs(theta) / step))))
    t = np.linspace(0.0, 1.0, n + 1)[1:-1]
    ang = a0 + theta * t
    xs = cx + r * np.cos(ang)
    ys = cy + r * np.sin(ang)
    if math.isnan(z0) or math.isnan(z1):
        zs = np.full_like(xs, z0 if not math.isnan(z0) else z1 if not math.isnan(z1) else NAN)
    else:
        zs = z0 + (z1 - z0) * t
    return np.column_stack([xs, ys, zs])


def flatten_polyline(verts, bulges=None, closed=False, max_dev=None, max_step=math.radians(6.0)):
    """Return (M,3) vertices with arcs tessellated.  Original vertices are preserved.

    The closing segment is included as an explicit last vertex only for arcs; callers
    that need a closed ring should append the first vertex themselves.
    """
    v = np.asarray(verts, float).reshape(-1, 3)
    if bulges is None or len(v) < 2 or not np.any(np.abs(bulges) > 1e-14):
        return v
    out = []
    n = len(v)
    last = n if closed else n - 1
    for i in range(last):
        out.append(v[i:i + 1])
        b = float(bulges[i])
        j = (i + 1) % n
        if abs(b) > 1e-14:
            seg = _arc_samples(v[i, 0], v[i, 1], v[i, 2], v[j, 0], v[j, 1], v[j, 2], b, max_dev, max_step)
            if len(seg):
                out.append(seg)
    if not closed:
        out.append(v[n - 1:n])
    return np.vstack(out)


def flatten_with_node_flags(verts, bulges=None, closed=False, max_dev=None):
    """Like flatten_polyline but also returns a boolean array flagging original vertices."""
    v = np.asarray(verts, float).reshape(-1, 3)
    if bulges is None or len(v) < 2 or not np.any(np.abs(bulges) > 1e-14):
        return v, np.ones(len(v), bool)
    out, flags = [], []
    n = len(v)
    last = n if closed else n - 1
    for i in range(last):
        out.append(v[i:i + 1]); flags.append(np.ones(1, bool))
        b = float(bulges[i]); j = (i + 1) % n
        if abs(b) > 1e-14:
            seg = _arc_samples(v[i, 0], v[i, 1], v[i, 2], v[j, 0], v[j, 1], v[j, 2], b, max_dev, math.radians(6.0))
            if len(seg):
                out.append(seg); flags.append(np.zeros(len(seg), bool))
    if not closed:
        out.append(v[n - 1:n]); flags.append(np.ones(1, bool))
    return np.vstack(out), np.concatenate(flags)


# --------------------------------------------------------------------------- lengths, areas
def polyline_length(verts, bulges=None, closed=False, three_d=False) -> float:
    v = np.asarray(verts, float).reshape(-1, 3)
    n = len(v)
    if n < 2:
        return 0.0
    total = 0.0
    last = n if closed else n - 1
    for i in range(last):
        j = (i + 1) % n
        dx, dy = v[j, 0] - v[i, 0], v[j, 1] - v[i, 1]
        b = float(bulges[i]) if bulges is not None else 0.0
        if abs(b) > 1e-14:
            arc = bulge_to_arc(v[i, 0], v[i, 1], v[j, 0], v[j, 1], b)
            L = arc[2] * abs(arc[4]) if arc else math.hypot(dx, dy)
        else:
            L = math.hypot(dx, dy)
        if three_d and not (math.isnan(v[i, 2]) or math.isnan(v[j, 2])):
            L = math.hypot(L, v[j, 2] - v[i, 2])
        total += L
    return total


def polygon_area(verts, bulges=None) -> float:
    """Area enclosed by a closed polyline, honouring arc segments."""
    v = np.asarray(verts, float).reshape(-1, 3)
    n = len(v)
    if n < 3 and not (n == 2 and bulges is not None):
        return 0.0
    x, y = v[:, 0], v[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    a = 0.5 * float(np.sum(x * y2 - x2 * y))
    if bulges is not None:
        for i in range(n):
            b = float(bulges[i])
            if abs(b) > 1e-14:
                j = (i + 1) % n
                arc = bulge_to_arc(x[i], y[i], x[j], y[j], b)
                if arc:
                    r, th = arc[2], arc[4]
                    a += 0.5 * r * r * (th - math.sin(th))
    return abs(a)


def centroid_xy(verts) -> tuple[float, float]:
    v = np.asarray(verts, float).reshape(-1, 3)
    return float(np.nanmean(v[:, 0])), float(np.nanmean(v[:, 1]))


def point_at_station(xy: np.ndarray, station: float):
    """Point and tangent azimuth (rad, math convention) at a distance along a 2D polyline."""
    d = np.hypot(*np.diff(xy, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(d)])
    s = min(max(station, 0.0), cum[-1])
    i = int(np.searchsorted(cum, s, side="right") - 1)
    i = min(max(i, 0), len(d) - 1)
    t = (s - cum[i]) / d[i] if d[i] > 0 else 0.0
    p = xy[i] + t * (xy[i + 1] - xy[i])
    ang = math.atan2(xy[i + 1, 1] - xy[i, 1], xy[i + 1, 0] - xy[i, 0])
    return p, ang


def cumulative_stations(xy: np.ndarray) -> np.ndarray:
    d = np.hypot(*np.diff(xy, axis=0).T) if len(xy) > 1 else np.array([])
    return np.concatenate([[0.0], np.cumsum(d)])


def chaikin(pts: np.ndarray, iterations: int = 1, closed: bool = False) -> np.ndarray:
    """Chaikin corner cutting (keeps end points on open lines)."""
    p = np.asarray(pts, float)
    for _ in range(iterations):
        if len(p) < 3 and not closed:
            break
        q = 0.75 * p[:-1] + 0.25 * p[1:]
        r = 0.25 * p[:-1] + 0.75 * p[1:]
        inter = np.empty((len(q) * 2, p.shape[1]))
        inter[0::2], inter[1::2] = q, r
        if closed:
            q2 = 0.75 * p[-1] + 0.25 * p[0]
            r2 = 0.25 * p[-1] + 0.75 * p[0]
            p = np.vstack([inter, q2, r2])
        else:
            p = np.vstack([p[:1], inter, p[-1:]])
    return p


# --------------------------------------------------------------------------- distance helpers
def closest_on_segments(px: float, py: float, a: np.ndarray, b: np.ndarray):
    """Closest point on many segments (a->b arrays (S,2)) to (px,py).

    Returns (dist (S,), t (S,), qx (S,), qy (S,)).
    """
    d = b - a
    L2 = np.einsum("ij,ij->i", d, d)
    w = np.column_stack([px - a[:, 0], py - a[:, 1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(L2 > 0, np.einsum("ij,ij->i", w, d) / L2, 0.0)
    t = np.clip(t, 0.0, 1.0)
    qx = a[:, 0] + t * d[:, 0]
    qy = a[:, 1] + t * d[:, 1]
    return np.hypot(qx - px, qy - py), t, qx, qy


def segment_intersection(p, r, q, s):
    """Intersection of segments p + t r and q + u s.  Returns (t, u) or None if parallel."""
    rxs = r[0] * s[1] - r[1] * s[0]
    if abs(rxs) < 1e-18:
        return None
    qp = (q[0] - p[0], q[1] - p[1])
    t = (qp[0] * s[1] - qp[1] * s[0]) / rxs
    u = (qp[0] * r[1] - qp[1] * r[0]) / rxs
    return t, u


def bbox_of(arrs) -> tuple[float, float, float, float] | None:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for a in arrs:
        a = np.asarray(a, float).reshape(-1, a.shape[-1] if np.ndim(a) > 1 else 2)
        if len(a) == 0:
            continue
        xs0.append(np.nanmin(a[:, 0])); xs1.append(np.nanmax(a[:, 0]))
        ys0.append(np.nanmin(a[:, 1])); ys1.append(np.nanmax(a[:, 1]))
    if not xs0:
        return None
    return min(xs0), min(ys0), max(xs1), max(ys1)
