import http.server
import json
import math
import socketserver
import threading
from pathlib import Path

import numpy as np
import pytest

from plumbline.core import cogo, crs as C, geometry as G, imagery as IM, units as U
from plumbline.core.featurecodes import build_linework, default_codes, parse_description
from plumbline.core.model import ImportBatch, Polyline, SurveyPoint
from plumbline.core.project import Project
from plumbline.core.qa import run_checks
from plumbline.core.spatial import SpatialIndex
from plumbline.core.surface import build_surface_from_project, generate_contours
from plumbline.core.tiles import TileSource, TileStore

MESQ_E, MESQ_N = 2552722.8663471513, 6967192.917956618       # lon -96.5992, lat 32.7668


# ============================================================ CRS manager
def test_search_and_describe():
    r = C.search_crs("texas north central")
    keys = [x.key for x in r]
    assert "EPSG:2276" in keys and "EPSG:32138" in keys and "EPSG:6584" in keys
    assert C.search_crs("2276")[0].key == "EPSG:2276"
    d = C.describe_crs("EPSG:2276")
    assert d["unit"] == "ftUS" and d["type"] == "Projected" and "Lambert" in d["projection"]
    assert C.describe_crs(32138)["unit"] == "m"
    assert C.describe_crs(4326)["unit"] == "deg"


def test_project_crs_roundtrip_and_serialisation():
    p = C.ProjectCRS.from_epsg(2276)
    lon, lat = p.to_lonlat(MESQ_E, MESQ_N)
    assert lon == pytest.approx(-96.5992, abs=1e-5) and lat == pytest.approx(32.7668, abs=1e-5)
    x, y = p.from_lonlat(lon, lat)
    assert (x, y) == pytest.approx((MESQ_E, MESQ_N), abs=1e-6)
    q = C.ProjectCRS.from_dict(p.to_dict())
    assert q.authority == "EPSG:2276" and q.unit == "ftUS"
    assert p.unit_factor == pytest.approx(1200 / 3937)


def test_saf_round_trip_and_distances():
    """SAF is GROUND / GRID and is always the reciprocal of a textbook combined factor."""
    saf = 1.0 / 0.99986                                    # what SAF a CSF of 0.99986 means
    g = C.SurfaceAdjustmentFactor(True, 2552000.0, 6967000.0, saf)
    p = C.ProjectCRS.from_epsg(2276, ground=g)
    x0, y0 = p.to_grid(2552000.0, 6967000.0)
    assert (x0, y0) == pytest.approx((2552000.0, 6967000.0))          # base point unchanged
    gx, gy = p.to_grid(2553000.0, 6967000.0)                          # 1000 ground ft east of base
    assert gx - 2552000.0 == pytest.approx(1000 * 0.99986)            # grid distance is shorter
    assert gx - 2552000.0 < 1000                                      # ...and shorter means < 1000
    rx, ry = p.from_grid(gx, gy)
    assert rx == pytest.approx(2553000.0, abs=1e-9)
    # lon/lat of a ground coordinate accounts for the scale factor
    plain = C.ProjectCRS.from_epsg(2276)
    a = p.to_lonlat(2553000.0, 6967000.0)
    b = plain.to_lonlat(2552000.0 + 1000 * 0.99986, 6967000.0)
    assert a == pytest.approx(b, abs=1e-10)


def test_saf_legacy_files_are_inverted_on_load():
    """An old .plb storing the grid/ground 'factor' must come back as the right SAF."""
    legacy = {"schema": 1, "enabled": True, "base_x": 0.0, "base_y": 0.0,
              "factor": 0.99986, "note": "written by an older Plumbline"}
    g = C.SurfaceAdjustmentFactor.from_dict(legacy)
    assert g.saf == pytest.approx(1.0 / 0.99986)
    assert g.factor == pytest.approx(0.99986)                # legacy read-back still agrees
    assert g.is_txdot_origin_scale                            # base (0,0) = TXDOT SOP
    round_trip = C.SurfaceAdjustmentFactor.from_dict(g.to_dict())
    assert round_trip.saf == pytest.approx(g.saf) and round_trip.base_x == g.base_x


def test_txdot_saf_scales_from_origin():
    """TXDOT county factor, e.g. Bexar 1.00017 applied from (0,0): ground = grid * SAF."""
    pr = C.ProjectCRS.from_epsg(6584, ground=C.SurfaceAdjustmentFactor.txdot_default(1.00017))
    e, n, z = 2_074_000.0, 13_800_000.0, 500.0
    ge, gn = pr.from_grid(e, n)                               # grid -> stored ground
    assert ge == pytest.approx(e * 1.00017) and gn == pytest.approx(n * 1.00017)
    be, bn = pr.to_grid(ge, gn)                               # and back, exactly
    assert (be, bn) == pytest.approx((e, n), rel=1e-12)
    # A 1000 ft grid distance is 1000.17 ft on the ground - the factor is not a tiny roundoff.
    g1 = pr.from_grid(e, n)
    g2 = pr.from_grid(e + 1000.0, n)
    assert (g2[0] - g1[0]) == pytest.approx(1000.0 * 1.00017, abs=1e-6)
    assert (pr.to_grid(*g2)[0] - pr.to_grid(*g1)[0]) == pytest.approx(1000.0, abs=1e-6)


def test_combined_factor_dfw():
    f = C.combined_factor(2276, -96.6, 32.77, 150.0)
    assert f["grid_factor"] == pytest.approx(0.99988463, abs=2e-6)
    assert f["elevation_factor"] == pytest.approx(0.9999765, abs=2e-6)
    assert f["combined"] == pytest.approx(f["grid_factor"] * f["elevation_factor"])


def test_datum_strategies_and_operations():
    auto = C.make_transform(2276, 4326, "auto")
    none = C.make_transform(2276, 4326, "none")
    a, b = auto(MESQ_E, MESQ_N), none(MESQ_E, MESQ_N)
    assert a == pytest.approx(b, abs=1e-9)                              # PROJ's NAD83->WGS84 is a null shift
    ops = C.list_operations(6584, 3857)
    assert ops and ops[0].available
    assert ops[0].accuracy is not None and ops[0].accuracy >= 1.0       # stated 2 m - not survey grade!
    t = C.make_transform(2276, 3857, "op:does-not-exist")               # falls back to auto, no crash
    assert np.isfinite(t(MESQ_E, MESQ_N)[0])
    inv = auto.inverse()
    assert inv(*auto(MESQ_E, MESQ_N)) == pytest.approx((MESQ_E, MESQ_N), abs=1e-5)


def test_proj_transform_falls_back_only_for_invalid_array_members(monkeypatch):
    class FakeTransformer:
        source_crs = C.CRS.from_epsg(4326)
        target_crs = C.CRS.from_epsg(3857)
        accuracy = 0.0

        def transform(self, x, y, direction=None):
            x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
            return np.where(x < 0, np.nan, x + 10.0), y + 20.0

    fallback = C.CoordTransform(
        lambda x, y: (np.asarray(x) + 100.0, np.asarray(y) + 200.0),
        lambda x, y: (np.asarray(x) - 100.0, np.asarray(y) - 200.0),
        "no datum shift",
    )
    monkeypatch.setattr(C, "_null_datum", lambda *_args: fallback)

    transform = C._proj_transform(FakeTransformer(), "fake projection")
    x, y = transform(np.array([1.0, -1.0, 2.0]), np.array([2.0, 3.0, 4.0]))
    assert x == pytest.approx([11.0, 99.0, 12.0])
    assert y == pytest.approx([22.0, 203.0, 24.0])


def test_projection_only_fallback_does_not_recurse(monkeypatch):
    calls = []

    class InvalidTransformer:
        accuracy = None

        def transform(self, x, y, direction=None):
            calls.append(1)
            x_array = np.asarray(x, dtype=float)
            return np.full_like(x_array, np.nan), np.full_like(x_array, np.nan)

    class TransformerFactory:
        @staticmethod
        def from_crs(*_args, **_kwargs):
            return InvalidTransformer()

    monkeypatch.setattr(C, "Transformer", TransformerFactory)
    projection_only = C._null_datum(C.CRS.from_epsg(2276), C.CRS.from_epsg(32138))
    x, y = projection_only(100.0, 200.0)

    assert math.isnan(float(x)) and math.isnan(float(y))
    assert len(calls) == 2  # unproject + project; neither step starts another fallback


def test_transform_between_ftus_and_metres():
    a = C.ProjectCRS.from_epsg(2276)
    b = C.ProjectCRS.from_epsg(32138)
    x, y = a.transform_to(b)(MESQ_E, MESQ_N)
    # same zone & parameters in metres: false origin (600000 m, 2000000 m) == (1968500, 6561666.667) US ft
    assert x == pytest.approx(MESQ_E * 1200 / 3937, abs=0.002)
    assert y == pytest.approx(MESQ_N * 1200 / 3937, abs=0.002)
    xb, yb = b.transform_to(a)(x, y)
    assert (xb, yb) == pytest.approx((MESQ_E, MESQ_N), abs=1e-5)


def test_suggest_and_area_of_use():
    s = C.suggest_crs(MESQ_E, MESQ_N, ["EPSG:2276", "EPSG:6584", "EPSG:32138", "EPSG:2277"])
    keys = [k for k, *_ in s]
    assert "EPSG:2276" in keys and "EPSG:32138" not in keys
    p = C.ProjectCRS.from_epsg(2276)
    ok, tot = p.area_of_use_ok(np.array([MESQ_E, MESQ_E]), np.array([MESQ_N, 1000.0]))
    assert (ok, tot) == (1, 2)


def test_convergence_dfw():
    p = C.ProjectCRS.from_epsg(2276)
    assert p.convergence_at(MESQ_E, MESQ_N) == pytest.approx(1.036, abs=0.02)


# ============================================================ geometry
def test_bulge_arc_length_and_area():
    # semicircle of diameter 2 -> bulge 1
    v = np.array([[0, 0, 0], [2, 0, 0.0]])
    assert G.polyline_length(v, np.array([1.0, 0.0])) == pytest.approx(math.pi)
    # closed "D": chord + semicircle -> area pi/2
    assert G.polygon_area(v, np.array([1.0, 0.0])) == pytest.approx(math.pi / 2)
    # circle from two semicircles
    assert G.polygon_area(v, np.array([1.0, 1.0])) == pytest.approx(math.pi)
    # CW orientation gives the same area
    assert G.polygon_area(v, np.array([-1.0, -1.0])) == pytest.approx(math.pi)
    flat = G.flatten_polyline(v, np.array([1.0, 0.0]), closed=False, max_dev=0.001)
    r = np.hypot(flat[:, 0] - 1, flat[:, 1] - 0)
    assert np.allclose(r, 1.0, atol=1e-9) and len(flat) > 20


def test_polygon_area_square_and_station():
    sq = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0.0]])
    assert G.polygon_area(sq) == 100.0
    p, ang = G.point_at_station(sq[:, :2], 15.0)
    assert tuple(p) == pytest.approx((10.0, 5.0)) and ang == pytest.approx(math.pi / 2)


# ============================================================ cogo
def test_bearing_parse_format_roundtrip():
    assert cogo.parse_bearing("N45-30-15E") == pytest.approx(45 + 30 / 60 + 15 / 3600)
    assert cogo.parse_bearing("S 12°30'00\" W") == pytest.approx(192.5)
    assert cogo.parse_bearing("N10W") == pytest.approx(350.0)
    assert cogo.parse_bearing("S10E") == pytest.approx(170.0)
    assert cogo.parse_bearing("123.5") == pytest.approx(123.5)
    assert cogo.parse_bearing("N95E") is None and cogo.parse_bearing("garbage") is None
    for az in (0.5, 45.5042, 133.3333, 200.2, 359.99):
        assert cogo.parse_bearing(cogo.format_bearing(az, True, 2)) == pytest.approx(az, abs=2e-4)
    assert cogo.format_bearing(45.504166666) == "N 45°30'15\" E"
    assert cogo.format_azimuth(359.99999, True, 0).startswith("0°")      # carry, no 360°00'00"


def test_inverse_and_traverse_closure():
    az, d = cogo.inverse(0, 0, 100, 100)
    assert az == pytest.approx(45.0) and d == pytest.approx(141.4213562)
    sq = cogo.traverse((1000, 1000), [(0, 100), (90, 100), (180, 100), (270, 100)])
    assert sq["linear_error"] == pytest.approx(0, abs=1e-9) and sq["area"] == pytest.approx(10000)
    bad = cogo.traverse((0, 0), [(0, 100), (90, 100), (180, 100), (270, 99.5)])
    assert bad["linear_error"] == pytest.approx(0.5) and bad["precision"] == pytest.approx(399.5 / 0.5)
    adj = cogo.bowditch(bad["points"], [100, 100, 100, 99.5])
    assert np.hypot(*(adj[-1] - adj[0])) == pytest.approx(0, abs=1e-9)


# ============================================================ units
def test_units():
    assert U.unit_from_factor(0.30480060960121924, "US survey foot") == "ftUS"
    assert U.unit_from_factor(0.3048, "foot") == "ft" and U.unit_from_factor(1.0, "metre") == "m"
    assert U.area_to_acres(43560.0, "ftUS") == pytest.approx(1.0)
    assert U.volume_to_cubic_yards(27.0, "ft") == pytest.approx(1.0)
    assert U.volume_to_cubic_meters(1.0, "m") == 1.0


# ============================================================ feature codes
def test_parse_description():
    p = parse_description("EP")
    assert (p.code, p.string, p.flags) == ("EP", "", frozenset())
    p = parse_description("ep1 b")
    assert (p.code, p.string) == ("EP", "1") and "B" in p.flags
    p = parse_description("EP 2 CLS")
    assert p.string == "2" and "CLS" in p.flags
    p = parse_description("TREE 18 OAK")
    assert p.code == "TREE" and p.note == "OAK" and p.string == "18"
    assert parse_description("").code == "" and parse_description("  ").code == ""
    assert parse_description("SSMH-1").code == "SSMH"


def _pts(rows):
    return [SurveyPoint(i + 1, str(i + 1), x, y, z, d) for i, (x, y, z, d) in enumerate(rows)]


def test_linework_strings_flags_and_closing():
    rows = [(0, 0, 1, "EP B"), (10, 0, 1, "EP"), (20, 0, 1, "EP E"),
            (0, 10, 2, "EP B"), (10, 10, 2, "EP E"),
            (50, 50, 0, "BLDG"), (60, 50, 0, "BLDG"), (60, 60, 0, "BLDG"), (50, 60, 0, "BLDG"),
            (5, 5, 1, "TREE"), (7, 7, 1, "FL1"), (8, 8, 1, "FL2"), (9, 9, 1, "FL1")]
    ls = build_linework(_pts(rows), default_codes())
    by = {}
    for s in ls:
        by.setdefault(s.code, []).append(s)
    assert len(by["EP"]) == 2 and by["EP"][0].ids == [1, 2, 3]
    assert by["BLDG"][0].closed and len(by["BLDG"][0].ids) == 4
    assert "TREE" not in by
    # interleaved strings are separated by their string number: FL1 has 2 points, FL2 only one (no line)
    assert [(x.string, x.ids) for x in by["FL"]] == [("1", [11, 13])]


def test_project_process_linework_and_layers():
    pr = Project("t")
    # This test exercises the opt-in generic library, not a new project's empty table.
    pr.codes = default_codes()
    for x, y, z, d in [(0, 0, 1, "EP"), (10, 0, 1, "EP"), (20, 0, 1.5, "EP"), (3, 3, 2, "MH"), (9, 9, 2, "XYZ")]:
        pr.add_point(x, y, z, desc=d)
    res = pr.apply_codes_to_points()
    assert res["matched"] == 4 and "XYZ" in res["unknown"]
    assert pr.point_by_number("4").layer == "UTIL-MH"
    assert not pr.linework_is_processed()
    out = pr.process_linework()
    assert out["strings"] == 1
    pl = next(pr.polylines(kinds={"breakline"}))
    assert pl.layer == "ROAD-EP" and len(pl.verts) == 3 and pl.derived == "linework"
    assert pr.linework_is_processed()
    assert pr.process_linework()["replaced"] == 1          # regenerating replaces, never duplicates
    assert pr.linework_is_processed()
    assert Project.from_dict(pr.to_dict()).linework_is_processed()

    pr.point_by_number("1").desc = "MH"
    pr.touch()
    assert not pr.linework_is_processed(), "point edits stale the processed-state marker"
    pr.process_linework()
    assert pr.linework_is_processed()


def test_linework_processed_state_tracks_the_requested_order():
    pr = Project("linework order")
    pr.codes = default_codes()
    pr.add_point(0, 0, 0, number="20", desc="EP")
    pr.add_point(10, 0, 0, number="10", desc="EP")

    file_signature = pr.linework_input_signature("file")
    number_signature = pr.linework_input_signature("number")
    assert file_signature != number_signature

    pr.process_linework(order="file")
    assert pr.linework_is_processed()
    pr.process_linework(order="number")
    assert pr.linework_is_processed(), "the saved processing order is checked with its own fingerprint"


def test_manual_linework_overrides_are_part_of_the_processed_state():
    pr = Project("manual linework")
    pr.codes = default_codes()
    first = pr.add_point(0, 0, 0, number="1", desc="EP")
    second = pr.add_point(10, 0, 0, number="2", desc="EP")
    pr.process_linework()
    assert pr.linework_is_processed()

    manual = pr.add_polyline(
        [[0, 0, 0], [10, 1, 0]], layer="ROAD-EP", kind="breakline",
        derived="manual-linework",
        attrs={"manual_linework_override": True,
               "linework_override_keys": [["EP", "1"]],
               "linework_override_point_ids": [first.id, second.id]},
    )
    assert not pr.linework_is_processed(), "a new manual override changes the build inputs"

    pr.process_linework()
    assert pr.linework_is_processed()
    manual.attrs["linework_override_point_ids"] = [first.id]
    pr.touch()
    assert not pr.linework_is_processed(), "editing an override must make the built state stale"


def test_legacy_project_linework_gets_a_processed_baseline_then_goes_stale_on_import():
    pr = Project("legacy linework")
    pr.codes = default_codes()
    pr.add_point(0, 0, 0, number="1", desc="EP ST")
    pr.add_point(10, 0, 0, number="2", desc="EP END")
    pr.process_linework()
    data = pr.to_dict()
    data["settings"].pop("linework_processed_signature", None)
    data["settings"].pop("linework_processed_order", None)

    restored = Project.from_dict(data)
    assert restored.linework_is_processed()
    restored.add_point(20, 0, 0, number="3", desc="EP")
    assert not restored.linework_is_processed()


# ============================================================ project
def test_new_projects_start_with_an_empty_feature_code_table_but_legacy_files_keep_defaults():
    pr = Project("empty")
    assert len(pr.codes) == 0

    legacy = pr.to_dict()
    legacy.pop("codes")  # Older files without a persisted table retain the legacy starter library.
    restored = Project.from_dict(legacy)
    assert len(restored.codes) == len(default_codes())


def make_site(n=15):
    pr = Project("site", C.ProjectCRS.from_epsg(2276))
    rng = np.random.default_rng(5)
    xs, ys = np.meshgrid(np.linspace(0, 140, n), np.linspace(0, 140, n))
    for x, y in zip(xs.ravel() + 2552000, ys.ravel() + 6967000):
        pr.add_point(x, y, 500 + 0.05 * (x - 2552000) + 2 * math.sin((y - 6967000) / 20), desc="GS")
    return pr


def test_save_load_roundtrip(tmp_path):
    pr = make_site()
    sf, rep = build_surface_from_project(pr, "Existing", {})
    pr.add_surface(sf)
    generate_contours(pr, sf, 1.0, 5)
    pr.add_polyline([[0, 0, 1], [1, 1, 2]], "X", bulges=[0.5, 0])
    pr.add_text(1, 2, "hello", 3.0, 45.0)
    f = tmp_path / "a.plb"
    pr.save(f)
    q = Project.load(f)
    assert len(q.points) == len(pr.points) and len(q.entities) == len(pr.entities)
    assert q.crs.authority == "EPSG:2276" and len(q.codes) == len(pr.codes)
    s2 = next(iter(q.surfaces.values()))
    assert np.allclose(s2.pts, sf.pts) and np.array_equal(s2.tris, sf.tris)
    pl = [e for e in q.entities.values() if isinstance(e, Polyline) and e.layer == "X"][0]
    assert pl.bulges is not None and pl.bulges[0] == 0.5
    # NaN elevation survives JSON
    pr.add_point(1, 2, float("nan"), desc="x")
    pr.save(f)
    assert math.isnan(Project.load(f).point_by_number(str(len(pr.points))).z)


def test_snapshot_restore():
    pr = make_site(5)
    snap = pr.snapshot()
    n = len(pr.points)
    pr.add_point(0, 0, 0)
    pr.crs = C.ProjectCRS.from_epsg(32138)
    assert len(pr.points) == n + 1
    pr.restore(snap)
    assert len(pr.points) == n and pr.crs.authority == "EPSG:2276"


def test_apply_batch_duplicate_policies():
    pr = Project("d")
    pr.add_point(0, 0, 1, number="1", desc="a")
    pr.add_point(1, 1, 1, number="2", desc="b")

    def batch():
        b = ImportBatch()
        b.points = [SurveyPoint(0, "2", 5, 5, 9, "dup"), SurveyPoint(0, "7", 6, 6, 9, "new")]
        return b

    s = pr.apply_batch(batch(), "renumber")
    assert s["renumbered"] == 1 and s["points"] == 2 and {p.number for p in pr.points.values()} == {"1", "2", "3", "7"}
    pr2 = Project("d")
    pr2.add_point(0, 0, 1, number="2")
    s = pr2.apply_batch(batch(), "skip")
    assert s["skipped"] == 1 and len(pr2.points) == 2
    pr3 = Project("d")
    pr3.add_point(0, 0, 1, number="2")
    s = pr3.apply_batch(batch(), "overwrite")
    assert pr3.point_by_number("2").x == 5 and len(pr3.points) == 2
    pr4 = Project("d")
    pr4.add_point(0, 0, 1, number="2")
    pr4.apply_batch(batch(), "keep")
    assert sum(1 for p in pr4.points.values() if p.number == "2") == 2


def test_reproject_units_and_back():
    pr = make_site(6)
    orig = [(p.x, p.y, p.z) for p in pr.points.values()]
    sf, _ = build_surface_from_project(pr, "S", {})
    pr.add_surface(sf)
    z_ft = sf.pts[0, 2]
    pr.reproject(C.ProjectCRS.from_epsg(32138))
    assert pr.crs.unit == "m" and pr.crs.vunit == "m"
    p0 = next(iter(pr.points.values()))
    assert p0.z == pytest.approx(orig[0][2] * 1200 / 3937)
    assert next(iter(pr.surfaces.values())).pts[0, 2] == pytest.approx(z_ft * 1200 / 3937)
    pr.reproject(C.ProjectCRS.from_epsg(2276))
    back = [(p.x, p.y, p.z) for p in pr.points.values()]
    assert np.allclose(back, orig, atol=1e-5)


def test_assign_crs_does_not_move_data():
    pr = make_site(4)
    before = [(p.x, p.y) for p in pr.points.values()]
    pr.assign_crs(C.ProjectCRS.from_epsg(6584))
    assert pr.crs.authority == "EPSG:6584" and before == [(p.x, p.y) for p in pr.points.values()]


def test_generate_contours_on_project_layers_and_labels():
    pr = make_site(15)
    sf, _ = build_surface_from_project(pr, "S", {})
    pr.add_surface(sf)
    out = generate_contours(pr, sf, 1.0, 5, labels=True, text_height=2.0)
    assert out["lines"] > 5 and out["labels"] > 0
    layers = {e.layer for e in pr.polylines(kinds={"contour"})}
    assert layers <= {"CONTOUR-MINOR", "CONTOUR-INDEX"}
    # regenerating replaces, never duplicates
    n1 = sum(1 for _ in pr.polylines(kinds={"contour"}))
    generate_contours(pr, sf, 1.0, 5, labels=True, text_height=2.0)
    assert sum(1 for _ in pr.polylines(kinds={"contour"})) == n1
    # labels are never upside-down
    from plumbline.core.model import TextEntity
    rots = [e.rotation for e in pr.entities.values() if isinstance(e, TextEntity)]
    assert rots and all(-90 < r <= 90 for r in rots)


# ============================================================ spatial index
def test_spatial_pick_snap_select():
    pr = Project("s")
    a = pr.add_point(0, 0, 1)
    b = pr.add_point(10, 0, 2)
    ln = pr.add_polyline([[0, 5, 0], [10, 5, 0], [10, 15, 0]], "0")
    sq = pr.add_polyline([[20, 20, 0], [30, 20, 0], [30, 30, 0], [20, 30, 0]], "0", closed=True)
    tx = pr.add_text(50, 50, "T")
    ix = SpatialIndex(pr)
    assert ix.pick(0.3, 0.2, 1.0) == ("pt", a.id)
    assert ix.pick(5.0, 5.3, 1.0) == ("en", ln.id)
    assert ix.pick(50.2, 50.2, 1.0) == ("en", tx.id)
    assert ix.pick(100, 100, 1.0) is None
    # a point lying ON a line still wins when the click is within tolerance, even if the line is marginally closer
    c = pr.add_point(0.0, 5.0, 3.0)
    ix2 = SpatialIndex(pr)
    assert ix2.pick(0.4, 5.0, 1.0) == ("pt", c.id)
    assert ix2.pick(5.0, 5.1, 1.0) == ("en", ln.id)               # away from the point the line is still pickable
    h = ix.snap(9.8, 5.2, 1.0)
    assert h.kind == "node" and (h.x, h.y) == (10.0, 5.0)
    h = ix.snap(5.1, 5.3, 1.0)
    assert h.kind == "mid" and h.x == 5.0
    h = ix.snap(3.0, 5.4, 1.0, modes=("nearest",))
    assert h.kind == "nearest" and (h.x, h.y) == pytest.approx((3.0, 5.0))
    h = ix.snap(0.1, 0.1, 1.0)
    assert h.kind == "point" and h.z == 1.0
    # closing segment of the polygon is pickable
    assert ix.pick(25.0, 20.2, 0.5) == ("en", sq.id)
    # window needs everything inside, crossing only needs a touch
    pts, ents = ix.select_box(18, 18, 32, 32, crossing=False)
    assert ents == {sq.id}
    pts, ents = ix.select_box(25, 18, 26, 22, crossing=False)
    assert ents == set()
    pts, ents = ix.select_box(25, 18, 26, 22, crossing=True)
    assert ents == {sq.id}
    pts, ents = ix.select_box(-1, -1, 11, 1, crossing=True)
    assert pts == {a.id, b.id}
    # hidden / locked layers are ignored
    pr.layers["0"].visible = False
    assert SpatialIndex(pr).pick(5.0, 5.3, 1.0) is None


# ============================================================ imagery
def test_web_mercator_and_tiles():
    x, y = IM.lonlat_to_merc(-96.5992, 32.7668)
    lon, lat = IM.merc_to_lonlat(x, y)
    assert (float(lon), float(lat)) == pytest.approx((-96.5992, 32.7668), abs=1e-9)
    # EPSG:3857 agreement with PROJ
    t = C.make_transform(4326, 3857)(-96.5992, 32.7668)
    assert (float(x), float(y)) == pytest.approx(t, abs=1e-6)
    xmin, ymin, xmax, ymax = IM.tile_bounds_merc(0, 0, 0)
    assert xmax - xmin == pytest.approx(2 * math.pi * IM.R_EARTH)
    tx, ty = IM.merc_to_tile_float(18, x, y)
    b = IM.tile_bounds_merc(18, int(tx), int(ty))
    assert b[0] <= x <= b[2] and b[1] <= y <= b[3]
    # z=18 tile at 32.8N is about 58 m wide on the ground
    # a z18 tile is ~152.9 Mercator-metres wide = ~128.5 ground metres at 32.8N
    assert (b[2] - b[0]) * math.cos(math.radians(32.77)) == pytest.approx(128.5, abs=0.2)
    z = IM.zoom_for_resolution(0.5, 32.77)
    assert 17.5 < z < 19.5


def test_check_stats_bias_vs_scatter():
    dx = np.array([1.0, 1.0, 1.0, 1.0])
    dy = np.array([0.0, 0.0, 0.0, 0.0])
    s = IM.compute_check_stats(dx, dy, tol=1.5)
    assert s.mean_dx == 1.0 and s.rmse_x == 1.0 and s.rmse_r == pytest.approx(1.0)
    assert s.rmse_r_after_shift == pytest.approx(0.0)
    assert (s.shift_dx, s.shift_dy) == (-1.0, 0.0)           # move the imagery by -mean
    assert s.n_within_tol == 4 and any("20" in w for w in s.warnings)
    rng = np.random.default_rng(0)
    dx, dy = rng.normal(0, 0.5, 40), rng.normal(0, 0.5, 40)
    s = IM.compute_check_stats(dx, dy)
    assert s.nssda_95 == pytest.approx(2.4477 * 0.5 * (s.rmse_x + s.rmse_y)) and not s.warnings
    assert s.nssda_95 == pytest.approx(1.7308 * s.rmse_r, rel=0.25)


def test_fit_similarity_recovers_transform():
    rng = np.random.default_rng(1)
    src = rng.uniform(0, 100, (12, 2))
    ang, sc, t = math.radians(0.7), 1.00002, np.array([1.2, -0.8])
    R = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    dst = sc * src @ R.T + t
    s, rot, tt, rmse = IM.fit_similarity(src, dst)
    assert s == pytest.approx(sc, abs=1e-9) and rot == pytest.approx(ang, abs=1e-9)
    assert tt == pytest.approx(t, abs=1e-6) and rmse < 1e-9


# ============================================================ QA
def test_qa_finds_bust_duplicate_and_wrong_crs():
    pr = make_site(15)
    ids = list(pr.points)
    pr.points[ids[112]].z += 6.0                                       # a bust in the middle
    dup = pr.points[ids[40]]
    pr.add_point(dup.x, dup.y, dup.z + 1.0, desc="GS")                 # same spot, different Z
    issues = {i.kind: i for i in run_checks(pr)}
    assert ids[112] in issues["spike"].point_ids
    assert "coincident-conflict" in issues
    assert "outside-crs" not in issues
    pr.add_point(100.0, 200.0, 5.0)                                    # nonsense for Texas State Plane
    kinds = {i.kind for i in run_checks(pr)}
    assert "outside-crs" in kinds
    clean = make_site(10)
    assert [i.kind for i in run_checks(clean) if i.severity != "info"] == []


def test_qa_unknown_code_check_skips_deleted_points_and_blank_descriptions():
    project = Project("Deleted description")
    project.codes = default_codes()
    active = project.add_point(0, 0, 1, number="1", desc="UNKNOWNCODE")
    project.add_point(10, 0, 1, number="2", desc="UNKNOWNCODE",
                      attrs={"qa_deleted": True})
    project.add_point(20, 0, 1, number="3", desc="")

    issues = run_checks(project)
    unknown = next(issue for issue in issues if issue.kind == "unknown-code")
    assert unknown.point_ids == [active.id]
    assert all(issue.kind != "empty-description" for issue in issues)


# ============================================================ tile store
class _H(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        if "/bad/" in self.path:
            self.send_response(404); self.end_headers(); return
        _H.hits += 1
        body = b"\xff\xd8\xff" + b"x" * 200
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_tile_store_cache_and_failure(tmp_path):
    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        src = TileSource("local", f"http://127.0.0.1:{port}/" + "{z}/{x}/{y}.jpg", 19)
        st = TileStore(tmp_path)
        assert st.get_cached(src, 3, 1, 2) is None
        assert st.fetch(src, 3, 1, 2)[:3] == b"\xff\xd8\xff"
        hits = _H.hits
        assert st.get_cached(src, 3, 1, 2) is not None and st.fetch(src, 3, 1, 2)
        assert _H.hits == hits                                         # second read came from disk
        got = []
        ev = threading.Event()
        st.request(src, 4, 5, 6, lambda *a: (got.append(a), ev.set()))
        assert ev.wait(5) and got[0][4] is not None and (tmp_path / src.key / "4" / "5" / "6.jpg").exists()
        bad = TileSource("bad", f"http://127.0.0.1:{port}/bad/" + "{z}/{x}/{y}.jpg")
        ev2 = threading.Event(); got2 = []
        st.request(bad, 1, 1, 1, lambda *a: (got2.append(a), ev2.set()))
        assert ev2.wait(5) and got2[0][4] is None
        assert st.request(bad, 1, 1, 1, lambda *a: None) is False        # negative-cached, no hammering
        st.offline = True
        got3, ev3 = [], threading.Event()
        assert st.request(src, 4, 5, 6, lambda *a: (got3.append(a), ev3.set()))          # cached tile still loads offline
        assert ev3.wait(5) and got3[0][4] is not None
        before = _H.hits
        got4, ev4 = [], threading.Event()
        assert st.request(src, 9, 9, 9, lambda *a: (got4.append(a), ev4.set()))          # uncached: nothing fetched
        assert ev4.wait(5) and got4[0][4] is None and _H.hits == before
        st.shutdown()
    finally:
        srv.shutdown()


# ============================================================ local (non-geodetic) projects
def test_local_crs_never_pretends_to_be_geodetic():
    pr = Project("local")
    assert pr.crs.is_local and pr.crs.unit == "ftUS" and pr.h_unit == "ftUS"
    with pytest.raises(C.LocalCRSError):
        pr.crs.to_lonlat(1000.0, 2000.0)
    with pytest.raises(C.LocalCRSError):
        pr.crs.transform_to(C.ProjectCRS.from_epsg(2276))
    # reprojecting a local project is refused, but ASSIGNING a CRS works (the numbers are then just labelled)
    pr.add_point(2552000.0, 6967000.0, 500.0)
    with pytest.raises(C.LocalCRSError):
        pr.reproject(C.ProjectCRS.from_epsg(2276))
    pr.assign_crs(C.ProjectCRS.from_epsg(2276))
    assert pr.crs.authority == "EPSG:2276" and pr.crs.to_lonlat(2552000.0, 6967000.0)[0] < -96
    # QA does not flag local data as "outside the area of use"
    loc = Project("l2")
    loc.add_point(5.0, 5.0, 1.0)
    assert "outside-crs" not in {i.kind for i in run_checks(loc)}
    # serialisation round trip keeps it local
    q = C.ProjectCRS.from_dict(C.ProjectCRS.local("m").to_dict())
    assert q.is_local and q.unit == "m"


# ============================================================ georeferenced imagery files
def test_world_file_image_corners(tmp_path):
    from PIL import Image
    im = Image.new("RGB", (40, 20), (200, 30, 30))
    f = tmp_path / "ortho.png"
    im.save(f)
    # 0.5 ft pixels, north-up, centre of top-left pixel at (1000.25, 2000.25 - 0.0)
    (tmp_path / "ortho.pgw").write_text("0.5\n0\n0\n-0.5\n1000.25\n1999.75\n")
    r = IM.read_georeferenced_image(f)
    assert r["crs"] is None and r["size"] == (40, 20) and r["rgba"].shape == (20, 40, 4)
    tl, tr, bl = r["corners"]
    assert tl == pytest.approx((1000.0, 2000.0)) and tr == pytest.approx((1020.0, 2000.0)) and bl == pytest.approx((1000.0, 1990.0))
    with pytest.raises(ValueError):
        Image.new("RGB", (4, 4)).save(tmp_path / "bare.png")
        IM.read_georeferenced_image(tmp_path / "bare.png")


def test_geotiff_corners_and_crs(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    f = tmp_path / "g.tif"
    data = (np.arange(30 * 50).reshape(1, 30, 50) % 255).astype("uint8").repeat(3, axis=0)
    with rasterio.open(f, "w", driver="GTiff", height=30, width=50, count=3, dtype="uint8", crs="EPSG:2276",
                       transform=from_origin(2552000.0, 6967100.0, 2.0, 2.0)) as ds:
        ds.write(data)
    r = IM.read_georeferenced_image(f)
    assert "2276" in r["crs"] or "Texas North Central" in r["crs"]
    tl, tr, bl = r["corners"]
    assert tl == pytest.approx((2552000.0, 6967100.0)) and tr == pytest.approx((2552100.0, 6967100.0)) and bl == pytest.approx((2552000.0, 6967040.0))
    assert r["rgba"].shape == (30, 50, 4) and r["rgba"][..., 3].min() == 255
    big = IM.read_georeferenced_image(f, max_dim=25)               # decimated, same ground extent
    assert big["rgba"].shape[1] == 25 and big["corners"] == r["corners"]


def test_kml_overlay_corners_rotation():
    c = IM.kml_overlay_corners(32.78, 32.77, -96.59, -96.60, 0.0)
    assert c[0] == (-96.60, 32.78) and c[1] == (-96.59, 32.78) and c[2] == (-96.60, 32.77)
    r = IM.kml_overlay_corners(32.78, 32.77, -96.59, -96.60, 90.0)
    # rotating 90 deg CCW about the centre sends top-left to bottom-left-ish: corners remain equidistant from the centre
    cx, cy = -96.595, 32.775
    d0 = [math.hypot((x - cx) * math.cos(math.radians(cy)), y - cy) for x, y in c]
    d1 = [math.hypot((x - cx) * math.cos(math.radians(cy)), y - cy) for x, y in r]
    assert d0 == pytest.approx(d1, rel=1e-9)


# ============================================================ settings & favourites
def test_settings_defaults_and_favourites(tmp_path):
    from plumbline.core.settings import DEFAULTS, Settings
    st = Settings(tmp_path / "s.json")
    favs = st.get("crs_favorites", [])               # caller's empty default must NOT hide the built-in list
    assert favs == DEFAULTS["crs_favorites"]
    favs.append("EPSG:9999")                          # and mutating the copy never corrupts the defaults
    assert "EPSG:9999" not in DEFAULTS["crs_favorites"]
    st.set("crs_favorites", ["EPSG:4326"])
    assert Settings(tmp_path / "s.json").get("crs_favorites") == ["EPSG:4326"]
    assert st.get("no_such_key", 7) == 7
    recs = C.default_favorites_records()
    assert any(k == "EPSG:2276" for k, _ in recs)
    # a local project holding State Plane numbers gets sensible suggestions
    keys = [k for k, *_ in C.suggest_crs(2552700.0, 6967100.0, [k for k, _ in recs])]
    assert "EPSG:2276" in keys and "EPSG:32138" not in keys


def test_apply_similarity_rotates_scales_and_keeps_surfaces_in_step():
    pr = make_site(5)
    sf, _ = build_surface_from_project(pr, "S", {})
    pr.add_surface(sf)
    pr.add_polyline([[2552000, 6967000, 5], [2552010, 6967000, 5]], "X", bulges=[0.3, 0])
    pr.add_text(2552005, 6967005, "T", 2.0, 10.0)
    p0 = next(iter(pr.points.values()))
    base = (p0.x, p0.y)
    z_before = [p.z for p in pr.points.values()]
    q = list(pr.points.values())[7]
    dist_before = math.hypot(q.x - base[0], q.y - base[1])
    area_before = sf.tin().areas2d().sum()
    pr.apply_similarity(base, scale=2.0, rot_deg=90.0, shift=(100.0, -50.0), dz=1.5)
    p0b = next(iter(pr.points.values()))
    assert (p0b.x, p0b.y) == pytest.approx((base[0] + 100.0, base[1] - 50.0))          # the base point only shifts
    q = list(pr.points.values())[7]
    assert math.hypot(q.x - p0b.x, q.y - p0b.y) == pytest.approx(2.0 * dist_before)    # scaled about the base
    assert [p.z for p in pr.points.values()] == pytest.approx([z + 1.5 for z in z_before])
    assert sf.tin().areas2d().sum() == pytest.approx(4.0 * area_before)                  # surface scaled with the points
    t = [e for e in pr.entities.values() if hasattr(e, "text")][0]
    assert t.rotation == pytest.approx(100.0) and t.height == pytest.approx(4.0)
    # a 90 degree CCW rotation about the base sends +E (east) to +N (north)
    pr2 = Project("r")
    pr2.add_point(10.0, 0.0, 1.0)
    pr2.apply_similarity((0, 0), rot_deg=90.0)
    r = next(iter(pr2.points.values()))
    assert (r.x, r.y) == pytest.approx((0.0, 10.0), abs=1e-9)
    # partial transform: only chosen points move
    pr3 = make_site(3)
    ids = list(pr3.points)[:2]
    x_other = pr3.points[list(pr3.points)[5]].x
    pr3.apply_similarity((0, 0), shift=(5, 0), point_ids=set(ids), entity_ids=set())
    assert pr3.points[list(pr3.points)[5]].x == x_other


# ------------------------------------------------------------------ review fixes (v0.1 -> v0.2)
def test_land_measure_and_earthwork_quantities_use_one_convention():
    """Both must follow the US conventional definitions so a foot job agrees with an estimator."""
    # 27 cubic feet is a cubic yard, for either kind of foot (the two differ by ~2 ppm, and
    # mixing conventions - as this module used to - makes areas and volumes disagree)
    assert U.volume_to_cubic_yards(27.0, "ft") == pytest.approx(1.0, abs=1e-12)
    assert U.volume_to_cubic_yards(27.0, "ftUS") == pytest.approx(1.0, abs=1e-12)
    assert U.area_to_acres(43560.0, "ftUS") == pytest.approx(1.0, abs=1e-12)
    # metric still converts exactly
    assert U.volume_to_cubic_yards(1.0, "m") == pytest.approx(1.0 / (0.9144 ** 3))
    assert U.volume_to_cubic_yards(1.0, "m") != pytest.approx(1.0)
    assert U.volume_to_cubic_meters(1.0, "m") == 1.0
    assert U.volume_to_cubic_feet(1.0, "ft") == pytest.approx(1.0)
    # a real earthwork number: 100 x 100 ft pad, 2 ft deep -> 20000 cu ft = 740.74 cy
    assert U.volume_to_cubic_yards(20000.0, "ftUS") == pytest.approx(740.740, abs=1e-3)


def test_undo_snapshot_covers_the_project_name():
    """_STATE_KEYS must carry everything an edit can change, or undo silently drops it."""
    p = Project("Original")
    p.add_point(0, 0, 0, "1")
    before = p.snapshot()
    p.name = "Renamed"
    p.restore(before)
    assert p.name == "Original", "undo did not restore the project name"


def test_edit_discards_a_noop_but_keeps_a_rename():
    """ApiState.edit(discard_if_unchanged=True) must notice a rename, not swallow it."""
    pytest.importorskip("PySide6")
    from plumbline.ui.app_state import AppState
    st = AppState(Project("Script target"))
    with st.edit("Do nothing", discard_if_unchanged=True):
        pass
    assert not st.undo_stack, "a no-op edit must not create an undo entry"
    with st.edit("Rename", discard_if_unchanged=True):
        st.project.name = "Renamed by plugin"
    assert len(st.undo_stack) == 1, "a rename inside an edit must be undoable"
    st.undo()
    assert st.project.name == "Script target"


def test_untitled_project_edit_rolls_back_on_cancel_and_saves_before_mutation(tmp_path):
    pytest.importorskip("PySide6")
    from plumbline.ui.app_state import AppState
    from plumbline.core.project_package import create_project_package

    state = AppState(Project("Untitled"))
    calls = []
    state._save_untitled_before_edit = lambda: calls.append("cancel") or False
    with state.edit("Add point"):
        state.project.add_point(1, 2, 3, desc="TOC")
    assert not state.project.points and not state.dirty and calls == ["cancel"]

    def save_package():
        package = create_project_package(state.project, tmp_path, "Saved Before Edit")
        state.project.restore(package.snapshot())
        state.project.path = package.path
        state.project._portable_paths = package._portable_paths
        state.set_dirty(False)
        calls.append("saved")
        return True

    state._save_untitled_before_edit = save_package
    with state.edit("Add point"):
        state.project.add_point(1, 2, 3, desc="TOC")
    assert calls == ["cancel", "saved"]
    assert len(state.project.points) == 1 and state.dirty
    assert Path(state.project.path).is_file()
    state.undo()
    assert not state.project.points and state.project.path
    state.project.save()
    saved = json.loads(Path(state.project.path).read_text(encoding="utf-8"))
    assert saved["package"]["relative_asset_paths"] is True


def test_tile_cache_key_is_stable_and_filesystem_safe():
    a = TileSource("Esri World Imagery", "https://example.com/{z}/{x}/{y}")
    b = TileSource("Esri World Imagery", "https://example.com/{z}/{x}/{y}")
    c = TileSource("Esri World Imagery", "https://example.com/{z}/{x}/{y}.png")
    assert a.key == b.key and a.key != c.key
    assert a.key.isalnum() or "_" in a.key


def test_arc_to_bulge_refuses_a_full_circle():
    """tan(pi/2) is not a bulge - callers must split a full circle into two segments."""
    with pytest.raises(ValueError):
        G.arc_to_bulge(0.0, 0.0, 10.0, 0.0, 10.0, 0.0, ccw=True)
    # a semicircle is fine and round-trips through bulge_to_arc
    b = G.arc_to_bulge(0.0, 0.0, 10.0, 0.0, -10.0, 0.0, ccw=True)
    assert b == pytest.approx(1.0, abs=1e-9)
    cx, cy, r, a0, th = G.bulge_to_arc(10.0, 0.0, -10.0, 0.0, b)
    assert (cx, cy) == pytest.approx((0.0, 0.0), abs=1e-9) and r == pytest.approx(10.0)
    assert abs(th) == pytest.approx(math.pi)
