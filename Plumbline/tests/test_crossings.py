"""Tests for Phase 2 breakline crossing, z=0 semantics, line-too-long, and command QA."""
from __future__ import annotations

import math
import numpy as np
import pytest

from plumbline.core.crossings import (
    DEFAULT_MAX_SEGMENT_LENGTH_FT,
    DEFAULT_MAX_SEGMENT_LENGTH_M,
    default_max_segment_length,
    detect_breakline_crossings,
    detect_long_segments,
    detect_segment_command_issues,
    run_geometry_checks,
)
from plumbline.core.project import Project


def test_detect_breakline_crossings_returns_all_n_crossings():
    """Never early-exits: a 4x5 grid of breaklines must report all 20 crossings."""
    pr = Project("Grid Crossings")
    for i in range(4):
        y = 10.0 * (i + 1)
        pr.add_polyline(
            [[0.0, y, 100.0], [100.0, y, 100.0]],
            layer="BRK",
            kind="breakline",
            attrs={"code": "SW", "string": str(i + 1)},
        )
    for j in range(5):
        x = 15.0 * (j + 1)
        pr.add_polyline(
            [[x, 0.0, 105.0], [x, 60.0, 105.0]],
            layer="BRK",
            kind="breakline",
            attrs={"code": "EP", "string": str(j + 1)},
        )

    findings = detect_breakline_crossings(pr)
    assert len(findings) == 20
    assert all(f.kind == "z-conflict" for f in findings)
    assert all(f.dz == pytest.approx(5.0) for f in findings)


def test_two_breaklines_crossing_multiple_times_report_all_intersections():
    """A snake breakline crossing a straight breakline 4 times reports all 4 crossings."""
    pr = Project("Multi Cross Pair")
    pr.add_polyline(
        [[0.0, 0.0, 50.0], [100.0, 0.0, 50.0]],
        layer="BRK",
        kind="breakline",
        attrs={"code": "CL", "string": "1"},
    )
    pr.add_polyline(
        [
            [10.0, -10.0, 52.0],
            [20.0, 10.0, 52.0],
            [40.0, -10.0, 52.0],
            [60.0, 10.0, 52.0],
            [80.0, -10.0, 52.0],
        ],
        layer="BRK",
        kind="breakline",
        attrs={"code": "SW", "string": "1"},
    )

    findings = detect_breakline_crossings(pr)
    assert len(findings) == 4
    xs = sorted(f.x for f in findings)
    assert xs == pytest.approx([15.0, 30.0, 50.0, 70.0], abs=1e-4)


def test_2d_z_zero_breakline_reports_plan_crossing_not_fake_z_conflict():
    """2D polylines sitting at z=0 are elevation-less and must not fake a Z-conflict."""
    pr = Project("Z Zero Semantics")
    # 3D curb breakline at z=512.0
    pr.add_polyline(
        [[0.0, 50.0, 512.0], [100.0, 50.0, 512.0]],
        layer="CURB",
        kind="breakline",
        attrs={"code": "BC", "string": "1"},
    )
    # 2D stripe / imported breakline sitting at z=0.0
    pr.add_polyline(
        [[50.0, 0.0, 0.0], [50.0, 100.0, 0.0]],
        layer="STRIPE",
        kind="breakline",
        attrs={"code": "ST", "string": "1"},
    )

    findings = detect_breakline_crossings(pr)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "plan-crossing"
    assert not f.has_z_conflict
    assert math.isnan(f.dz)
    assert f.x == pytest.approx(50.0)
    assert f.y == pytest.approx(50.0)


def test_z_zero_breakline_linked_to_3d_points_uses_point_elevations():
    """If a z=0 breakline is linked to 3D survey points, point elevations drive Z-conflict detection."""
    pr = Project("Point Backed Z")
    p1 = pr.add_point(50.0, 0.0, 520.0, number="1", desc="SW1 ST")
    p2 = pr.add_point(50.0, 100.0, 520.0, number="2", desc="SW1 END")
    # Polyline vertices sit at z=0, but point_ids link to p1, p2 at z=520
    pr.add_polyline(
        [[50.0, 0.0, 0.0], [50.0, 100.0, 0.0]],
        layer="SW",
        kind="breakline",
        attrs={"code": "SW", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )
    pr.add_polyline(
        [[0.0, 50.0, 510.0], [100.0, 50.0, 510.0]],
        layer="EP",
        kind="breakline",
        attrs={"code": "EP", "string": "1"},
    )

    findings = detect_breakline_crossings(pr)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "z-conflict"
    assert f.has_z_conflict
    assert f.dz == pytest.approx(10.0)


def test_shared_endpoint_is_not_flagged_as_crossing():
    """Two breaklines meeting cleanly at a shared endpoint vertex are valid connectivity."""
    pr = Project("Shared Endpoint")
    p1 = pr.add_point(0.0, 0.0, 100.0, number="1")
    p2 = pr.add_point(50.0, 0.0, 100.0, number="2")
    p3 = pr.add_point(50.0, 50.0, 100.0, number="3")
    pr.add_polyline(
        [[0.0, 0.0, 100.0], [50.0, 0.0, 100.0]],
        layer="BRK",
        kind="breakline",
        attrs={"code": "SW", "string": "1", "point_ids": [p1.id, p2.id]},
    )
    pr.add_polyline(
        [[50.0, 0.0, 100.0], [50.0, 50.0, 100.0]],
        layer="BRK",
        kind="breakline",
        attrs={"code": "SW", "string": "2", "point_ids": [p2.id, p3.id]},
    )

    assert detect_breakline_crossings(pr) == []


def test_bulge_arc_crossing_and_self_crossing_detected():
    """Bulged arcs are flattened so true curve crossings and self-bowties are caught."""
    pr = Project("Arc and Self Crossing")
    # Chord is at y=0 from x=0..20, but bulge=-1.0 bows up to y=+10 at x=10, crossing y=6
    pr.add_polyline(
        [[0.0, 0.0, 100.0], [20.0, 0.0, 100.0]],
        bulges=[-1.0, 0.0],
        layer="BRK",
        kind="breakline",
        attrs={"code": "CURV", "string": "1"},
    )
    pr.add_polyline(
        [[0.0, 6.0, 102.0], [20.0, 6.0, 102.0]],
        layer="BRK",
        kind="breakline",
        attrs={"code": "STR", "string": "1"},
    )
    # Self-crossing figure-8 breakline far away around (200, 200)
    pr.add_polyline(
        [
            [200.0, 200.0, 50.0],
            [220.0, 220.0, 50.0],
            [220.0, 200.0, 50.0],
            [200.0, 220.0, 50.0],
        ],
        closed=False,
        layer="BRK",
        kind="breakline",
        attrs={"code": "BOW", "string": "1"},
    )

    findings = detect_breakline_crossings(pr)
    # Arc crosses y=6 twice, plus 1 self-crossing on BOW1-01 = 3 total
    assert len(findings) == 3
    self_crossings = [f for f in findings if f.is_self_crossing]
    assert len(self_crossings) == 1
    assert self_crossings[0].seg_a_id == "BOW1-01"


def test_detect_long_segments_defaults_and_custom_threshold():
    """Default threshold is 300 ft for ftUS and 100 m for metric, configurable via settings."""
    pr = Project("Length Check")
    assert default_max_segment_length(pr) == pytest.approx(DEFAULT_MAX_SEGMENT_LENGTH_FT)
    pr.crs.units = "m"
    assert default_max_segment_length(pr) == pytest.approx(DEFAULT_MAX_SEGMENT_LENGTH_M)
    pr.crs.units = "ftUS"

    p1 = pr.add_point(0.0, 0.0, 10.0, number="1", desc="EP1 ST")
    p2 = pr.add_point(120.0, 0.0, 10.0, number="2", desc="EP1")
    p3 = pr.add_point(500.0, 0.0, 10.0, number="3", desc="EP1 END")
    pr.add_polyline(
        [[0.0, 0.0, 10.0], [120.0, 0.0, 10.0], [500.0, 0.0, 10.0]],
        layer="ROAD",
        kind="breakline",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id, p3.id]},
        derived="linework",
    )

    # Default 300 ft flags only span 1 (120 -> 500, length 380 ft)
    long_default = detect_long_segments(pr)
    assert len(long_default) == 1
    assert long_default[0].span_index == 1
    assert long_default[0].length_2d == pytest.approx(380.0)
    assert long_default[0].point_numbers == ["2", "3"]

    # Custom threshold via project.settings
    pr.settings["max_segment_length"] = 100.0
    long_custom = detect_long_segments(pr)
    assert len(long_custom) == 2


def test_run_geometry_checks_combines_crossings_lengths_and_commands():
    """Unified runner surfaces crossings, long spans, and fieldwork command sequence issues."""
    pr = Project("Unified Geometry QA")
    # Two points with EP1 missing initial ST and spanning 400 ft
    p1 = pr.add_point(0.0, 0.0, 100.0, number="1", desc="EP1")
    p2 = pr.add_point(400.0, 0.0, 100.0, number="2", desc="EP1 END")
    pr.add_polyline(
        [[0.0, 0.0, 100.0], [400.0, 0.0, 100.0]],
        layer="ROAD",
        kind="breakline",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )
    # Crossing breakline at x=200
    pr.add_polyline(
        [[200.0, -50.0, 105.0], [200.0, 50.0, 105.0]],
        layer="ROAD",
        kind="breakline",
        attrs={"code": "SW", "string": "1"},
    )

    report = run_geometry_checks(pr)
    assert report.breakline_count == 2
    assert len(report.crossings) == 1
    assert len(report.long_segments) == 1
    assert any(ci.issue_type == "Missing ST" for ci in report.command_issues)
    categories = {issue.category for issue in report.issues}
    assert {"z-conflict", "line-too-long", "command-sequence"}.issubset(categories)
