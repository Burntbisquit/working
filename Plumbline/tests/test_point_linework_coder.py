"""Tests for the point linework coder engine.

Validates that linework modifications (joining, reversing, open/close, prefix changes,
splitting) are executed directly as point description modifications so that linework
reprocessing or external export (Carlson, Civil 3D, Leica, Trimble) reproduces identical
geometry.
"""
from __future__ import annotations

import pytest

from plumbline.core.model import Polyline, SurveyPoint
from plumbline.core.featurecodes import default_codes
from plumbline.core.point_linework_coder import (
    change_string_code,
    close_string_coding,
    join_points_to_string,
    open_string_coding,
    remove_point_token,
    reverse_string_coding,
    split_string_coding,
    start_string_coding,
    update_point_token,
)
from plumbline.core.project import Project


def create_test_project():
    pr = Project()
    # These linework tests deliberately use Plumbline's generic starter library.
    pr.codes = default_codes()
    pr.add_point(100.0, 200.0, 10.0, number="101", desc="EP ST")
    pr.add_point(150.0, 250.0, 10.5, number="102", desc="EP")
    pr.add_point(200.0, 300.0, 11.0, number="103", desc="EP")
    pr.add_point(250.0, 350.0, 11.5, number="104", desc="EP END")
    return pr


# ------------------------------------------------------------------ Join Points
def test_join_points_to_string():
    pr = Project()
    pr.codes = default_codes()
    p1 = pr.add_point(0, 0, 0, number="1", desc="GS")
    p2 = pr.add_point(10, 0, 0, number="2", desc="GS")
    p3 = pr.add_point(20, 0, 0, number="3", desc="GS")

    pts = [p1, p2, p3]
    modified = join_points_to_string(pts, code="EP", string_id="1", closed=False)
    assert len(modified) == 3
    assert p1.desc == "EP1 ST - GS"
    assert "EP1" in p2.desc
    assert "EP1 END" in p3.desc

    res = pr.process_linework()
    assert res["strings"] == 1
    poly = next(e for e in pr.entities.values() if isinstance(e, Polyline))
    assert len(poly.verts) == 3
    assert not poly.closed


def test_join_points_closed():
    pr = Project()
    pr.codes = default_codes()
    p1 = pr.add_point(0, 0, 0, number="1", desc="")
    p2 = pr.add_point(10, 0, 0, number="2", desc="")
    p3 = pr.add_point(10, 10, 0, number="3", desc="")

    pts = [p1, p2, p3]
    modified = join_points_to_string(pts, code="BLDG", closed=True)
    assert p1.desc == "BLDG ST"
    assert p2.desc == "BLDG"
    assert p3.desc == "BLDG X"

    res = pr.process_linework()
    assert res["strings"] == 1
    poly = next(e for e in pr.entities.values() if isinstance(e, Polyline))
    assert poly.closed


# ------------------------------------------------------------------ Reverse Linework
def test_reverse_string_coding():
    pr = create_test_project()
    # Initial: 101: EP ST, 102: EP, 103: EP, 104: EP END
    pts = [pr.points[pid] for pid in [1, 2, 3, 4]]
    reversed_pts = reverse_string_coding(pts)

    assert len(reversed_pts) == 4
    # Point 104 (id=4) is now first in sequence and has ST
    assert reversed_pts[0].id == 4
    assert reversed_pts[0].desc == "EP ST"
    # Point 101 (id=1) is now last in sequence and has END
    assert reversed_pts[-1].id == 1
    assert reversed_pts[-1].desc == "EP END"
    # Intermediate points
    assert reversed_pts[1].id == 3
    assert reversed_pts[1].desc == "EP"
    assert reversed_pts[2].id == 2
    assert reversed_pts[2].desc == "EP"


def test_reverse_with_curves():
    pr = Project()
    p1 = pr.add_point(0, 0, 0, number="1", desc="BC ST")
    p2 = pr.add_point(10, 0, 0, number="2", desc="BC PC")
    p3 = pr.add_point(20, 5, 0, number="3", desc="BC PT")
    p4 = pr.add_point(30, 5, 0, number="4", desc="BC END")

    pts = [p1, p2, p3, p4]
    reversed_pts = reverse_string_coding(pts)
    assert reversed_pts[0].id == 4
    assert reversed_pts[0].desc == "BC ST"
    assert reversed_pts[1].id == 3
    assert reversed_pts[1].desc == "BC PC"
    assert reversed_pts[2].id == 2
    assert reversed_pts[2].desc == "BC PT"
    assert reversed_pts[3].id == 1
    assert reversed_pts[3].desc == "BC END"


# ------------------------------------------------------------------ Multicodes
def test_join_and_update_with_multicodes():
    pr = Project()
    p1 = pr.add_point(0, 0, 0, number="1", desc="TREE - TOC ST")
    p2 = pr.add_point(10, 0, 0, number="2", desc="TREE - TOC")
    p3 = pr.add_point(20, 0, 0, number="3", desc="TREE - TOC END")

    pts = [p1, p2, p3]
    change_string_code(pts, old_code_prefix="TOC", new_code_prefix="CURB2")
    assert "CURB2 ST" in p1.desc
    assert "TREE" in p1.desc
    assert "CURB2" in p2.desc
    assert "CURB2 END" in p3.desc


# ------------------------------------------------------------------ Close / Open Linework
def test_close_and_open_string_coding():
    pr = create_test_project()
    pts = [pr.points[pid] for pid in [1, 2, 3, 4]]

    # Close string
    close_string_coding(pts[-1])
    assert pr.points[4].desc == "EP X"

    pr.process_linework()
    poly = next(e for e in pr.entities.values() if isinstance(e, Polyline))
    assert poly.closed

    # Open string back up
    open_string_coding(pts[-1])
    assert pr.points[4].desc == "EP END"

    pr.process_linework()
    poly = next(e for e in pr.entities.values() if isinstance(e, Polyline))
    assert not poly.closed


# ------------------------------------------------------------------ Split Linework
def test_split_string_coding():
    pr = create_test_project()
    pts = [pr.points[pid] for pid in [1, 2, 3, 4]]

    # Split between point 2 and 3
    before = pts[:2]
    after = pts[2:]

    b_res, a_res = split_string_coding(before, after, new_string_id="2")
    assert pr.points[2].desc == "EP END"
    assert pr.points[3].desc == "EP2 ST"
    assert pr.points[4].desc == "EP2 END"

    res = pr.process_linework()
    assert res["strings"] == 2
    polys = [e for e in pr.entities.values() if isinstance(e, Polyline)]
    assert len(polys) == 2
    assert len(polys[0].verts) == 2
    assert len(polys[1].verts) == 2


# ------------------------------------------------------------------ Change String Code
def test_change_string_code():
    pr = create_test_project()
    pts = [pr.points[pid] for pid in [1, 2, 3, 4]]

    change_string_code(pts, old_code_prefix="EP", new_code_prefix="EDGE5")
    assert pr.points[1].desc == "EDGE5 ST"
    assert pr.points[2].desc == "EDGE5"
    assert pr.points[3].desc == "EDGE5"
    assert pr.points[4].desc == "EDGE5 END"


# ------------------------------------------------------------------ Update / Remove Token
def test_update_and_remove_token():
    desc = "TOC ST"
    d1 = update_point_token(desc, "TOC", "BC ST")
    assert d1 == "BC ST"

    d2 = update_point_token("TOC ST - EP 1", "EP", "EP 2 ST")
    assert d2 == "TOC ST - EP 2 ST"

    d3 = remove_point_token("TOC ST - EP 1", "EP")
    assert d3 == "TOC ST"


def test_boundary_helpers_use_active_fieldbook_meanings_and_keep_notes():
    commands = {
        "start_line": "BEGINLN",
        "start_curve": "BC",
        "end_curve": "EC",
        "end_line": "FINISH",
        "close": "CLOSEFIG",
        "multicode": "PLUS",
        "description": "NOTE",
    }
    point = SurveyPoint(1, "1", 0, 0, 0, "EA NOTE curb")
    start_string_coding(point, "EA", commands)
    assert point.desc == "EA BEGINLN NOTE curb"
    open_string_coding(point, "EA", commands)
    assert point.desc == "EA FINISH NOTE curb"
    close_string_coding(point, "EA", commands)
    assert point.desc == "EA CLOSEFIG NOTE curb"


def test_custom_fieldbook_tokens_drive_linework_and_string_ids(monkeypatch):
    from plumbline.core.featurecodes import FeatureCode
    from plumbline.core.point_linework_coder import find_free_string_id
    from plumbline.core.settings import settings

    commands = {
        "start_line": "BEGINLN",
        "start_curve": "BC",
        "end_curve": "EC",
        "end_line": "FINISH",
        "close": "CLOSEFIG",
        "multicode": "PLUS",
        "description": "NOTE",
    }
    monkeypatch.setitem(settings()._data, "space_between_commands", True)
    monkeypatch.setitem(settings()._data, "space_around_multicode_separator", True)
    monkeypatch.setitem(settings()._data, "space_around_description_separator", True)

    pr = Project("Custom Field Book")
    pr.settings["f2f_commands"] = commands
    pr.codes.add(FeatureCode(code="EA", kind="line", layer="ASPHALT", breakline=True))
    pr.codes.add(FeatureCode(code="SW", kind="line", layer="WALK", breakline=True))
    first = pr.add_point(0, 0, 1, number="1", desc="EA NOTE curb")
    last = pr.add_point(10, 0, 1, number="2", desc="EA NOTE curb")

    join_points_to_string([first, last], "EA", closed=True, commands=commands)
    assert first.desc == "EA BEGINLN NOTE curb"
    assert last.desc == "EA CLOSEFIG NOTE curb"
    result = pr.process_linework()
    assert result["strings"] == 1
    line = next(entity for entity in pr.entities.values() if isinstance(entity, Polyline))
    assert line.closed

    pr.add_point(20, 0, 1, number="3", desc="EA1 BEGINLN")
    pr.add_point(20, 10, 1, number="4", desc="EA3 PLUS SW")
    assert find_free_string_id(pr, "EA") == "2"
