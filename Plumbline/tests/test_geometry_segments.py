from __future__ import annotations

import math

import pytest

from plumbline.core.geometry_segments import build_segments, segment_point_rows
from plumbline.core.model import Polyline
from plumbline.core.project import Project


def _linked_line(project, points, code="SW", string="1"):
    return project.add_polyline(
        [[point.x, point.y, point.z] for point in points], "ROAD-SIDEWALK",
        attrs={"code": code, "string": string, "point_ids": [point.id for point in points]},
        derived="linework", kind="breakline")


def test_segments_autoname_reopened_code_occurrences_in_point_number_order():
    project = Project("Segments")
    # The two SW1 occurrences are deliberately interleaved with unrelated shots.
    p1 = project.add_point(0, 0, 100, number="6", desc="SW1 ST")
    p2 = project.add_point(5, 0, 100, number="10", desc="SW1 END")
    q1 = project.add_point(0, 8, 101, number="7", desc="SW2 ST")
    q2 = project.add_point(5, 8, 101, number="12", desc="SW2 END")
    r1 = project.add_point(20, 0, 102, number="20", desc="SW1 ST")
    r2 = project.add_point(25, 0, 102, number="22", desc="SW1 END")
    # Add out of creation order to prove the list is shot-number ordered.
    _linked_line(project, [r1, r2])
    _linked_line(project, [q1, q2], string="2")
    _linked_line(project, [p1, p2])

    segments = build_segments(project)
    assert [segment.seg_id for segment in segments] == ["SW1-01", "SW1-02", "SW2-01"]
    assert segments[0].point_numbers == ("6", "10")
    assert segments[1].point_numbers == ("20", "22")
    assert segments[0].linked


def test_segment_rows_use_linked_point_properties_and_measure_to_next():
    project = Project("Lengths")
    a = project.add_point(0, 0, 10, number="1", desc="EP1 ST")
    b = project.add_point(3, 4, 14, number="2", desc="EP1 END")
    _linked_line(project, [a, b], code="EP", string="1")
    segment = build_segments(project)[0]
    rows = segment_point_rows(project, segment)

    assert segment.length_2d == pytest.approx(5.0)
    assert segment.length_3d == pytest.approx(math.sqrt(41.0))
    assert rows[0]["number"] == "1"
    assert rows[0]["to_next"] == pytest.approx(5.0)
    assert rows[1]["to_next"] is None
