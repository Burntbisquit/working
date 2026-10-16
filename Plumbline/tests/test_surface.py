import math

import numpy as np
import pytest
import shapely

from plumbline.core import geometry as G
from plumbline.core.surface import (TIN, build_tin, contour_levels, is_index_level, volume_between)


def grid_points(n=21, size=100.0, fn=lambda x, y: 0.1 * x + 0.05 * y + 10.0):
    xs = np.linspace(0, size, n)
    X, Y = np.meshgrid(xs, xs)
    return np.column_stack([X.ravel(), Y.ravel(), fn(X, Y).ravel()])


# ------------------------------------------------------------------ volumes
def test_plane_volume_to_datum_exact():
    tin, rep = build_tin(grid_points())
    # datum below everything: all cut, ∫(0.1x+0.05y) over 100x100 = 75000
    r = tin.volume_to_datum(10.0)
    assert r.cut == pytest.approx(75000.0, rel=1e-9)
    assert r.fill == pytest.approx(0.0, abs=1e-9)
    # datum 15: h = 0.1x + 0.05y - 5 (linear) -> cut = area(h>0) * h(centroid)
    box = shapely.box(0, 0, 100, 100)
    half = shapely.Polygon([(-1e4, 1e5), (1e5, -1e5 / 1.0), (1e5, 1e5), (-1e4, 1e5)])
    # build the half-plane 0.1x + 0.05y > 5  ->  y > 100 - 2x
    big = 1e4
    hp = shapely.Polygon([(-big, 100 + 2 * big), (big, 100 - 2 * big), (big, big * 10), (-big, big * 10)])
    pos = box.intersection(hp)
    cx, cy = pos.centroid.x, pos.centroid.y
    exp_cut = pos.area * (0.1 * cx + 0.05 * cy - 5.0)
    neg = box.difference(hp)
    nx, ny = neg.centroid.x, neg.centroid.y
    exp_fill = -neg.area * (0.1 * nx + 0.05 * ny - 5.0)
    r = tin.volume_to_datum(15.0)
    assert r.cut == pytest.approx(exp_cut, rel=1e-9)
    assert r.fill == pytest.approx(exp_fill, rel=1e-9)
    assert r.area_cut + r.area_fill == pytest.approx(10000.0, rel=1e-9)
    assert r.net == pytest.approx(25000.0, rel=1e-9)


def test_volume_region_clip():
    tin, _ = build_tin(grid_points(n=11))
    region = shapely.box(25, 25, 75, 75)       # 50x50 square, plane over it
    r = tin.volume_to_datum(10.0, region)
    # ∫(0.1x+0.05y) over region = 2500*(0.1*50+0.05*50) = 18750
    assert r.cut == pytest.approx(18750.0, rel=1e-9)
    assert r.area_total == pytest.approx(2500.0, rel=1e-9)


def test_gable_roof_volume_needs_breakline():
    # rectangle 20 x 10, ridge at y=5, z=3  -> volume of the prism = 0.5*10*3*20 = 300
    corners = np.array([[0, 0, 0], [20, 0, 0], [20, 10, 0], [0, 10, 0.0]])
    ridge = np.array([[0, 5, 3], [20, 5, 3.0]])
    # sprinkle extra eave points to tempt Delaunay into crossing the ridge
    eave = np.array([[x, y, 0.0] for x in (5, 10, 15) for y in (0, 10)])
    pts = np.vstack([corners, ridge, eave])
    tin, rep = build_tin(pts, breaklines=[ridge])
    assert tin.volume_to_datum(0.0).cut == pytest.approx(300.0, rel=1e-9)
    # ridge elevation is exact along the whole ridge
    xs = np.linspace(0.5, 19.5, 40)
    z = tin.z_at(xs, np.full_like(xs, 5.0))
    assert np.allclose(z, 3.0, atol=1e-9)


def test_volume_between_surfaces():
    a, _ = build_tin(grid_points(n=11, fn=lambda x, y: 10 + 0 * x))
    b, _ = build_tin(grid_points(n=11, fn=lambda x, y: 8 + 0 * x))
    r = volume_between(a, b)
    assert r.cut == pytest.approx(2.0 * 10000.0, rel=1e-6)
    assert r.fill == pytest.approx(0.0, abs=1e-6)
    r2 = volume_between(b, a)
    assert r2.fill == pytest.approx(20000.0, rel=1e-6)
    rg = volume_between(a, b, method="grid", cell=2.0)
    assert rg.cut == pytest.approx(20000.0, rel=0.02)


# ------------------------------------------------------------------ breaklines
def test_breakline_is_honoured_among_noisy_points():
    rng = np.random.default_rng(7)
    pts = np.column_stack([rng.uniform(0, 100, 600), rng.uniform(0, 100, 600), rng.uniform(0, 30, 600)])
    bl = np.array([[10, 10, 5.0], [90, 80, 25.0]])
    tin, rep = build_tin(pts, breaklines=[bl])
    assert rep.n_unenforced == 0
    t = np.linspace(0.02, 0.98, 60)
    xs, ys = 10 + t * 80, 10 + t * 70
    z = tin.z_at(xs, ys)
    assert np.allclose(z, 5 + t * 20, atol=1e-6)


def test_crossing_breaklines_are_noded():
    rng = np.random.default_rng(3)
    pts = np.vstack([np.column_stack([rng.uniform(0, 100, 300), rng.uniform(0, 100, 300), np.zeros(300)]),
                     [[0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0.0]]])
    b1 = np.array([[10, 10, 5.0], [90, 90, 5.0]])
    b2 = np.array([[10, 90, 7.0], [90, 10, 7.0]])
    tin, rep = build_tin(pts, breaklines=[b1, b2])
    assert any("crossing" in w for w in rep.warnings)
    z = tin.z_at(np.array([50.0]), np.array([50.0]))[0]
    assert z == pytest.approx(6.0, abs=1e-6)       # averaged at the crossing


def test_boundary_and_hole_and_max_edge():
    pts = grid_points(n=21, fn=lambda x, y: 0 * x)
    outer = np.array([[10, 10], [90, 10], [90, 90], [10, 90.0]])
    hole = np.array([[40, 40], [60, 40], [60, 60], [40, 60.0]])
    tin, rep = build_tin(pts, boundaries=[outer], holes=[hole])
    a = tin.areas2d().sum()
    assert a == pytest.approx(80 * 80 - 20 * 20, rel=0.02)
    assert np.isnan(tin.z_at(np.array([50.0]), np.array([50.0]))[0])
    assert tin.z_at(np.array([20.0]), np.array([20.0]))[0] == pytest.approx(0.0)
    # max edge removes long hull triangles of an L-shaped cloud
    L = np.array([[x, y, 0.0] for x in range(0, 30, 3) for y in range(0, 30, 3) if x < 6 or y < 6])
    t1, _ = build_tin(L)
    t2, r2 = build_tin(L, max_edge=4.5)
    assert t2.areas2d().sum() < t1.areas2d().sum()
    assert r2.n_removed_edge > 0


def test_duplicate_points_are_dropped():
    pts = np.vstack([grid_points(n=5), grid_points(n=5)])
    tin, rep = build_tin(pts)
    assert rep.n_duplicates == 25
    assert tin.n_points == 25


# ------------------------------------------------------------------ contours
def test_contours_of_a_slope_are_straight_lines():
    tin, _ = build_tin(grid_points(n=21, fn=lambda x, y: x * 0.1))
    lines = tin.contours([5.0])
    assert len(lines) == 1
    lvl, v, closed = lines[0]
    assert not closed
    assert np.allclose(v[:, 0], 50.0, atol=1e-6)
    assert np.allclose(v[:, 2], 5.0)
    assert G.polyline_length(v) == pytest.approx(100.0, rel=1e-9)


def test_contours_of_a_hill_close():
    tin, _ = build_tin(grid_points(n=41, size=40, fn=lambda x, y: 20 - np.hypot(x - 20, y - 20)))
    lines = tin.contours([15.0])
    assert len(lines) == 1
    lvl, v, closed = lines[0]
    assert closed
    r = np.hypot(v[:, 0] - 20, v[:, 1] - 20)
    assert r.mean() == pytest.approx(5.0, abs=0.1)
    # no duplicated vertices
    assert len(np.unique(np.round(v[:, :2], 9), axis=0)) == len(v)


def test_contour_through_exact_vertex_level_is_robust():
    # levels coincide with data values (very common with survey data)
    tin, _ = build_tin(grid_points(n=11, fn=lambda x, y: np.round(x / 10)))
    for lvl in range(1, 9):
        out = tin.contours([float(lvl)])
        assert len(out) >= 1


def test_levels_and_index():
    assert contour_levels(0.2, 5.1, 1.0) == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert contour_levels(0.2, 1.1, 0.5, base=0.25) == [0.25, 0.75]
    assert is_index_level(5.0, 1.0, 5) and not is_index_level(4.0, 1.0, 5)


# ------------------------------------------------------------------ profile, z_at, slope
def test_profile_exact_on_plane():
    tin, _ = build_tin(grid_points(n=11, fn=lambda x, y: 0.1 * x))
    st, z, xy = tin.profile(np.array([[0, 50.0], [100, 50.0]]))
    assert st[0] == 0 and st[-1] == pytest.approx(100.0)
    assert np.allclose(z, 0.1 * st, atol=1e-9)
    # line that leaves the surface -> NaN gaps
    st, z, xy = tin.profile(np.array([[-50, 50.0], [150, 50.0]]))
    assert np.isnan(z[0]) and np.isnan(z[-1])
    inside = np.isfinite(z)
    assert np.allclose(z[inside], 0.1 * xy[inside, 0], atol=1e-9)


def test_z_at_and_slope():
    tin, _ = build_tin(grid_points(n=11, fn=lambda x, y: 0.1 * x + 0.05 * y))
    assert tin.z_at(33.3, 71.2) == pytest.approx(0.1 * 33.3 + 0.05 * 71.2, abs=1e-9)
    assert np.isnan(tin.z_at(-5.0, 5.0))
    slope, aspect = tin.slope_at(50.0, 50.0)
    assert slope == pytest.approx(100 * math.hypot(0.1, 0.05), rel=1e-6)
    # surface falls toward -x,-y => downslope azimuth in the SW quadrant (180..270)
    assert 180 < aspect < 270


def test_sections():
    tin, _ = build_tin(grid_points(n=21, fn=lambda x, y: 0.1 * y))
    secs = tin.sections(np.array([[10, 10.0], [90, 10.0]]), interval=20, half_width=5)
    assert len(secs) == 5
    s0 = secs[0]
    assert s0["center"] == pytest.approx((10.0, 10.0))
    assert np.allclose(s0["z"], 0.1 * (10 + s0["offset"]), atol=1e-9)


def test_hillshade_range():
    tin, _ = build_tin(grid_points(n=11, fn=lambda x, y: 0.3 * x))
    h = tin.hillshade()
    assert np.all((h >= 0) & (h <= 1))


def test_surface_staleness_detection():
    from plumbline.core.project import Project
    from plumbline.core.surface import build_surface_from_project, is_stale, rebuild_surface
    pr = Project("s")
    for x in range(0, 60, 10):
        for y in range(0, 60, 10):
            pr.add_point(x, y, 0.1 * x + 0.05 * y, desc="GS")
    sf, _ = build_surface_from_project(pr, "EG", {"ground_only": True})
    pr.add_surface(sf)
    assert not is_stale(pr, sf)
    pr.add_point(25, 25, 99.0, desc="GS")                       # new shot -> out of date
    assert is_stale(pr, sf)
    rebuild_surface(pr, sf)
    assert not is_stale(pr, sf) and sf.tin().n_points == 37
    next(iter(pr.points.values())).z += 1.0                      # edited elevation -> out of date again
    pr.touch()                                                   # (the UI's state.edit() does this for every edit)
    assert is_stale(pr, sf)
    pr.add_point(5, 5, 7.0, desc="BLDG")                        # not a ground code: surface unaffected
    rebuild_surface(pr, sf)
    pr.add_point(6, 6, 7.0, desc="TREE 12")
    assert not is_stale(pr, sf)


# ------------------------------------------------------------------ review fixes (v0.1 -> v0.2)
def test_finder_is_built_once_under_concurrency():
    """The GUI may build the point locator on demand while a worker thread is already doing it."""
    import threading
    pts = grid_points(31)
    tin, _ = build_tin(pts)
    results = []
    n_workers = 7
    barrier = threading.Barrier(n_workers + 1)        # the workers AND the main thread

    def worker():
        barrier.wait()
        results.append(tin.finder())

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_workers)]
    for t in threads:
        t.start()
    try:
        barrier.wait()                    # release everybody at the same instant
        main = tin.finder()               # the "GUI thread" joins the race
    finally:
        for t in threads:
            t.join(10)
        assert not any(t.is_alive() for t in threads), "finder() deadlocked under contention"
    assert len(results) == n_workers
    assert all(r is main for r in results), "every thread must get the same locator instance"
    # and it still answers correctly afterwards
    assert float(tin.z_at(50.0, 50.0)) == pytest.approx(0.1 * 50 + 0.05 * 50 + 10.0, abs=1e-9)


def test_tin_survives_a_pickle_roundtrip():
    """TIN is pickled inside undo snapshots - the locator lock must not break that."""
    import pickle
    pts = grid_points(11)
    tin, _ = build_tin(pts)
    tin.finder()
    clone = pickle.loads(pickle.dumps(tin))
    assert float(clone.z_at(50.0, 50.0)) == pytest.approx(float(tin.z_at(50.0, 50.0)), abs=1e-12)


def test_build_tin_documents_grid_snap_dedup():
    """dup_tol snaps to a grid: 0.9*tol apart can survive, 1.1*tol apart can merge."""
    tol = 1.0
    pts = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 5.0],      # same cell as (0,0) -> merges
                    [10.0, 0.0, 0.0], [11.5, 0.0, 0.0],     # adjacent cells -> both kept
                    [0.0, 10.0, 0.0], [10.0, 10.0, 0.0]])
    _, rep = build_tin(pts, dup_tol=tol)
    # the two near-duplicates merge (1 fewer point), nothing else does
    assert rep.n_duplicates == 1, rep.n_duplicates


def test_z_zero_breakline_drapes_onto_point_elevations_without_cutting_zero_cliff():
    """2D breaklines sitting at z=0 must take point/surface elevations instead of cutting 0.00 cliffs."""
    pts = grid_points(n=11, size=100.0, fn=lambda x, y: 500.0 + 0.1 * x + 0.05 * y)
    # 1) Breakline whose endpoints sit on existing grid points (10,10) and (90,90) but with z=0.0
    bl_on_pts = np.array([[10.0, 10.0, 0.0], [50.0, 50.0, 0.0], [90.0, 90.0, 0.0]])
    # 2) Free 2D breakline at z=0.0 whose vertices do not coincide with exact grid points
    bl_free_2d = np.array([[13.3, 27.7, 0.0], [77.1, 63.4, 0.0]])
    tin, rep = build_tin(pts, breaklines=[bl_on_pts, bl_free_2d])
    assert rep.n_unenforced == 0
    # No vertex in the TIN may have been pulled down to z=0.0
    assert float(tin.pts[:, 2].min()) >= 500.0 - 1e-6
    assert float(tin.z_at(50.0, 50.0)) == pytest.approx(500.0 + 5.0 + 2.5, abs=1e-6)
    assert float(tin.z_at(45.2, 45.55)) == pytest.approx(500.0 + 0.1 * 45.2 + 0.05 * 45.55, abs=1e-4)

