"""Tests for Phase 3 geometry tools and active-occurrence coding operations."""
from __future__ import annotations

import math
import numpy as np
import pytest

from plumbline.core.crossings import detect_breakline_crossings
from plumbline.core.featurecodes import default_codes
from plumbline.core.geometry_segments import build_segments
from plumbline.core.geometry_tools import (
    add_polyline_node,
    apply_connect_segments,
    apply_shrink_line_commands,
    auto_nudge_crossings,
    cogo_points_from_lines,
    curve_segment,
    flip_past,
    inject_survey_point,
    join_lines_average,
    move_line_end_to_end,
    nudge_point,
    nudge_segment_vertex,
    preview_connect_segments,
    preview_shrink_line_commands,
    recurve_segment,
    square_shape,
    straighten_segment,
    sweep_code_family_occurrences,
    untwist_polyline,
)
from plumbline.core.project import Project


def test_nudge_point_propagates_to_all_linked_polylines_and_preserves_numbers():
    pr = Project("Nudge")
    p1 = pr.add_point(10.0, 20.0, 100.0, number="101", desc="EP1 ST")
    p2 = pr.add_point(30.0, 20.0, 100.0, number="102", desc="EP1 END / SW1 ST")
    p3 = pr.add_point(30.0, 50.0, 102.0, number="103", desc="SW1 END")
    l1 = pr.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        layer="ROAD",
        kind="breakline",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )
    l2 = pr.add_polyline(
        [[p2.x, p2.y, p2.z], [p3.x, p3.y, p3.z]],
        layer="WALK",
        kind="breakline",
        attrs={"code": "SW", "string": "1", "point_ids": [p2.id, p3.id]},
        derived="linework",
    )

    nudge_point(pr, p2.id, 1.5, -2.5, 0.5)
    assert (p2.x, p2.y, p2.z) == pytest.approx((31.5, 17.5, 100.5))
    assert p2.number == "102"
    assert pr.entities[l1.id].verts[1] == pytest.approx([31.5, 17.5, 100.5])
    assert pr.entities[l2.id].verts[0] == pytest.approx([31.5, 17.5, 100.5])

    # Free-vertex nudge via nudge_segment_vertex with propagate_to_point=False
    res = nudge_segment_vertex(pr, l1.id, 0, -1.0, 2.0, propagate_to_point=False)
    assert (res["x"], res["y"]) == pytest.approx((9.0, 22.0))
    assert (p1.x, p1.y) == pytest.approx((10.0, 20.0))


def test_untwist_polyline_removes_self_crossing_bowtie_and_preserves_point_numbers():
    pr = Project("Untwist")
    p1 = pr.add_point(0.0, 0.0, 50.0, number="10", desc="SW1 ST")
    p2 = pr.add_point(20.0, 20.0, 50.0, number="20", desc="SW1")
    p3 = pr.add_point(20.0, 0.0, 50.0, number="30", desc="SW1")
    p4 = pr.add_point(0.0, 20.0, 50.0, number="40", desc="SW1 END")
    line = pr.add_polyline(
        [[p.x, p.y, p.z] for p in (p1, p2, p3, p4)],
        layer="WALK",
        kind="breakline",
        attrs={"code": "SW", "string": "1", "point_ids": [p1.id, p2.id, p3.id, p4.id]},
        derived="linework",
    )
    assert len(detect_breakline_crossings(pr)) == 1

    res = untwist_polyline(pr, line.id)
    assert res["reversed_indices"] == [1, 2]
    assert detect_breakline_crossings(pr) == []
    # Point numbers are completely unchanged
    assert [pr.points[pid].number for pid in (p1.id, p2.id, p3.id, p4.id)] == ["10", "20", "30", "40"]


def test_flip_past_reflects_interior_vertex_across_chord_and_preserves_description():
    pr = Project("Flip Past")
    p1 = pr.add_point(0.0, 0.0, 10.0, number="1", desc="EP1 ST")
    p2 = pr.add_point(10.0, 6.0, 11.0, number="2", desc="EP1")
    p3 = pr.add_point(20.0, 0.0, 12.0, number="3", desc="EP1 END")
    line = pr.add_polyline(
        [[p.x, p.y, p.z] for p in (p1, p2, p3)],
        layer="ROAD",
        kind="breakline",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id, p3.id]},
        derived="linework",
    )

    flip_past(pr, line.id, [1])
    assert (p2.x, p2.y, p2.z) == pytest.approx((10.0, -6.0, 11.0))
    assert pr.entities[line.id].verts[1] == pytest.approx([10.0, -6.0, 11.0])
    assert p2.number == "2" and p2.desc == "EP1"


def test_curve_recurve_and_straighten_update_bulges_and_pc_pt_tokens():
    pr = Project("Curve Tools")
    p1 = pr.add_point(0.0, 0.0, 100.0, number="1", desc="BC1 ST")
    p2 = pr.add_point(20.0, 0.0, 100.0, number="2", desc="BC1 END")
    line = pr.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        layer="CURB",
        kind="breakline",
        attrs={"code": "BC", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )

    b = curve_segment(pr, line.id, 0, through_point=(10.0, 10.0))
    assert b == pytest.approx(-1.0)
    assert "PC" in p1.desc.split()
    assert "PT" in p2.desc.split()

    straighten_segment(pr, line.id, 0)
    assert pr.entities[line.id].bulges is None
    assert "PC" not in p1.desc.split()
    assert "PT" not in p2.desc.split()

    # Recurve through 3 vertices (PC, mid, PT) removing the mid vertex from polyline
    q1 = pr.add_point(0.0, 0.0, 100.0, number="10", desc="BC2 ST")
    q2 = pr.add_point(10.0, -5.0, 100.0, number="11", desc="BC2")
    q3 = pr.add_point(20.0, 0.0, 100.0, number="12", desc="BC2 END")
    line2 = pr.add_polyline(
        [[q.x, q.y, q.z] for q in (q1, q2, q3)],
        layer="CURB",
        kind="breakline",
        attrs={"code": "BC", "string": "2", "point_ids": [q1.id, q2.id, q3.id]},
        derived="linework",
    )
    b2 = recurve_segment(pr, line2.id, 0, 1, 2, remove_mid_vertex=True)
    assert b2 == pytest.approx(0.5)
    assert len(pr.entities[line2.id].verts) == 2
    assert "PC" in q1.desc.split() and "PT" in q3.desc.split()


def test_active_occurrence_connect_reuses_closed_label_avoids_live_label_and_routes_snap():
    """§4.1 & §4.2: Connect lines reuses a formerly closed SW1 while avoiding live SW2."""
    pr = Project("Active Occurrence Connect")
    pr.codes = default_codes()

    # SW1 is open on 2..6 and closes on 6
    pr.add_point(0.0, 0.0, 100.0, number="2", desc="SW1 ST")
    pr.add_point(10.0, 0.0, 100.0, number="6", desc="SW1 END")

    # SW2 opens on 4 and stays live all the way to 30
    pr.add_point(0.0, 20.0, 100.0, number="4", desc="SW2 ST")
    pr.add_point(50.0, 20.0, 100.0, number="30", desc="SW2 END")

    # Target points to connect: #10 at (20, 0), #18 within snap tol at (30, 0.1), #25 at (40, 0)
    pt10 = pr.add_point(20.0, 0.0, 100.0, number="10", desc="GS")
    pt18 = pr.add_point(30.0, 0.1, 100.0, number="18", desc="GS")
    # Out-of-order point #5 sits geometrically between pt10 and pt25 but has number < 10 -> must NOT be routed!
    _pt5 = pr.add_point(25.0, 0.0, 100.0, number="5", desc="GS")
    pt25 = pr.add_point(40.0, 0.0, 100.0, number="25", desc="GS")

    preview = preview_connect_segments(
        pr, pt25.id, pt10.id, base_code="SW", snap_tolerance=0.5
    )
    # Must choose SW1 (closed at #6 before #10), NOT SW2 (live 4..30)
    assert preview.chosen_label == "SW1"
    assert preview.reused_closed_label is True
    assert preview.routed_point_numbers == ["10", "18", "25"]

    apply_connect_segments(pr, preview)
    segs = build_segments(pr)
    assert [s.seg_id for s in segs] == ["SW1-01", "SW1-02", "SW2-01"]
    assert segs[1].point_numbers == ("10", "18", "25")
    # Point numbers never changed
    assert pt10.number == "10" and pt18.number == "18" and pt25.number == "25"


def test_join_lines_average_moves_both_endpoints_to_mean_and_preserves_both_point_numbers():
    pr = Project("Join Average")
    p1 = pr.add_point(0.0, 0.0, 100.0, number="10", desc="EP1 ST")
    p2 = pr.add_point(10.0, 0.0, 100.0, number="11", desc="EP1 END")
    p3 = pr.add_point(10.4, 0.6, 102.0, number="20", desc="EP2 ST")
    p4 = pr.add_point(20.0, 0.6, 102.0, number="21", desc="EP2 END")

    l1 = pr.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        layer="ROAD",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )
    l2 = pr.add_polyline(
        [[p3.x, p3.y, p3.z], [p4.x, p4.y, p4.z]],
        layer="ROAD",
        attrs={"code": "EP", "string": "2", "point_ids": [p3.id, p4.id]},
        derived="linework",
    )

    res = join_lines_average(pr, l1.id, "end", l2.id, "start")
    assert (res["x"], res["y"], res["z"]) == pytest.approx((10.2, 0.3, 101.0))
    assert (p2.x, p2.y, p2.z) == pytest.approx((10.2, 0.3, 101.0))
    assert (p3.x, p3.y, p3.z) == pytest.approx((10.2, 0.3, 101.0))
    # Both point numbers survive unchanged
    assert p2.number == "11" and p3.number == "20"
    assert len(pr.points) == 4


def test_move_line_end_to_end_endpoint_and_whole_line_modes():
    pr = Project("Move End to End")
    p1 = pr.add_point(0.0, 0.0, 50.0, number="1", desc="SW1 ST")
    p2 = pr.add_point(10.0, 0.0, 50.0, number="2", desc="SW1 END")
    p3 = pr.add_point(12.0, 3.0, 51.0, number="3", desc="SW2 ST")
    p4 = pr.add_point(22.0, 3.0, 51.0, number="4", desc="SW2 END")

    target = pr.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        layer="WALK",
        attrs={"code": "SW", "string": "1", "point_ids": [p1.id, p2.id]},
    )
    mover = pr.add_polyline(
        [[p3.x, p3.y, p3.z], [p4.x, p4.y, p4.z]],
        layer="WALK",
        attrs={"code": "SW", "string": "2", "point_ids": [p3.id, p4.id]},
    )

    res = move_line_end_to_end(pr, mover.id, "start", target.id, "end", whole_line=True)
    assert (res["dx"], res["dy"], res["dz"]) == pytest.approx((-2.0, -3.0, -1.0))
    assert (p3.x, p3.y, p3.z) == pytest.approx((10.0, 0.0, 50.0))
    assert (p4.x, p4.y, p4.z) == pytest.approx((20.0, 0.0, 50.0))
    assert p3.number == "3" and p4.number == "4"


def test_add_polyline_node_vs_inject_survey_point_range_preserving():
    pr = Project("Node vs Point")
    p1 = pr.add_point(0.0, 0.0, 10.0, number="10", desc="EP1 ST")
    p2 = pr.add_point(20.0, 0.0, 20.0, number="20", desc="EP1 END")
    line = pr.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        layer="ROAD",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )

    # 1) Add node creates no survey point
    node_res = add_polyline_node(pr, line.id, 0, fraction=0.25)
    assert node_res["point_id"] is None
    assert (node_res["x"], node_res["y"], node_res["z"]) == pytest.approx((5.0, 0.0, 12.5))
    assert len(pr.points) == 2
    assert pr.entities[line.id].attrs["point_ids"] == [p1.id, None, p2.id]

    # 2) Inject point allocates range-preserving number "11" between "10" and "20"
    line2 = pr.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        layer="ROAD",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )
    injected = inject_survey_point(pr, line2.id, 0, fraction=0.5)
    assert injected.number == "11"
    assert (injected.x, injected.y, injected.z) == pytest.approx((10.0, 0.0, 15.0))
    assert injected.desc == "EP1"
    assert pr.entities[line2.id].attrs["point_ids"] == [p1.id, injected.id, p2.id]


def test_cogo_points_from_lines_deduplicates_shared_endpoints_and_links_attrs():
    pr = Project("COGO from Lines")
    # Two imported LiDAR breaklines sharing endpoint (50, 50, 120)
    l1 = pr.add_polyline([[0.0, 0.0, 110.0], [10.0, 20.0, 115.0], [50.0, 50.0, 120.0]], layer="LIDAR", kind="breakline")
    l2 = pr.add_polyline([[50.0, 50.0002, 120.0], [80.0, 90.0, 125.0]], layer="LIDAR", kind="breakline")

    created = cogo_points_from_lines(pr, [l1.id, l2.id], include_interior=False, dedup_tolerance=1e-3)
    # 3 unique endpoints: (0,0), (50,50), (80,90)
    assert len(created) == 3
    pids1 = pr.entities[l1.id].attrs["point_ids"]
    pids2 = pr.entities[l2.id].attrs["point_ids"]
    assert pids1[0] is not None and pids1[1] is None and pids1[2] is not None
    assert pids1[2] == pids2[0]  # Shared endpoint deduplicated!


def test_shrink_line_commands_repairs_missing_st_end_and_compacts_jumped_suffixes():
    """§3.5: Repairs Missing initial ST, Missing END before ST, Missing ST after END, and compacts SW10 -> SW1."""
    pr = Project("Shrink Line Commands")
    pr.codes = default_codes()

    # SW1: starts without ST (Missing initial ST), then a new SW1 ST appears without prior END (Missing END before ST),
    # then SW1 END closes it, then another SW1 appears later without ST (Missing ST after END) and ends with SW1 END.
    p1 = pr.add_point(0.0, 0.0, 100.0, number="1", desc="SW1")
    p2 = pr.add_point(10.0, 0.0, 100.0, number="2", desc="SW1")
    p3 = pr.add_point(20.0, 0.0, 100.0, number="3", desc="SW1 ST")
    p4 = pr.add_point(30.0, 0.0, 100.0, number="4", desc="SW1 END")
    p5 = pr.add_point(40.0, 0.0, 100.0, number="5", desc="SW1")
    p6 = pr.add_point(50.0, 0.0, 100.0, number="6", desc="SW1 END")

    # SW2 is left open across 7..25 so SW2 is NOT closed
    p7 = pr.add_point(0.0, 10.0, 100.0, number="7", desc="SW2 ST")
    p8 = pr.add_point(10.0, 10.0, 100.0, number="8", desc="SW2")

    # Crew jumped to SW10 on points 20..21 after SW1 had already closed on point 6!
    p20 = pr.add_point(60.0, 0.0, 100.0, number="20", desc="SW10 ST / TREE")
    p21 = pr.add_point(70.0, 0.0, 100.0, number="21", desc="SW10 END")

    preview = preview_shrink_line_commands(pr)
    assert preview.repaired_counts["Missing initial ST"] == 1
    assert preview.repaired_counts["Missing END before ST"] == 1
    assert preview.repaired_counts["Missing ST after END"] == 1
    assert preview.repaired_counts["Compacted suffix"] == 1
    assert len(preview.compactions) == 1
    assert preview.compactions[0].old_label == "SW10"
    assert preview.compactions[0].new_label == "SW1"

    apply_shrink_line_commands(pr, preview)
    assert p1.desc == "SW1 ST"
    assert p2.desc == "SW1 END"
    assert p5.desc == "SW1 ST"
    # SW10 compacted to SW1 while preserving unrelated '/ TREE' multicode section!
    assert p20.desc == "SW1 ST / TREE"
    assert p21.desc == "SW1 END"

    segs = build_segments(pr)
    assert [s.seg_id for s in segs] == ["SW1-01", "SW1-02", "SW1-03", "SW1-04", "SW2-01"]


def test_auto_nudge_crossings_resolves_bowtie_and_near_crossing_and_reports_residue():
    """Phase 4 (§3.6): Auto-nudge resolves fixable crossings and reports unfixable residue."""
    pr = Project("Auto Nudge")
    # 1) Self-crossing bowtie around (0..20, 0..20)
    pr.add_polyline(
        [[0.0, 0.0, 100.0], [20.0, 20.0, 100.0], [20.0, 0.0, 100.0], [0.0, 20.0, 100.0]],
        layer="BRK",
        kind="breakline",
        attrs={"code": "BOW", "string": "1"},
    )
    # 2) Two breaklines near y=100 where one endpoint pokes 0.4 ft across the other
    p1 = pr.add_point(100.0, 100.0, 50.0, number="10", desc="EP1 ST")
    p2 = pr.add_point(120.0, 100.0, 50.0, number="11", desc="EP1 END")
    p3 = pr.add_point(110.0, 99.6, 52.0, number="12", desc="SW1 ST")
    p4 = pr.add_point(110.0, 115.0, 52.0, number="13", desc="SW1 END")
    pr.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        layer="ROAD",
        kind="breakline",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
    )
    pr.add_polyline(
        [[p3.x, p3.y, p3.z], [p4.x, p4.y, p4.z]],
        layer="WALK",
        kind="breakline",
        attrs={"code": "SW", "string": "1", "point_ids": [p3.id, p4.id]},
        derived="linework",
    )
    # 3) Deep X-crossing requiring >25 ft move (beyond max_displacement=2.0) -> stays in residue
    pr.add_polyline(
        [[500.0, 500.0, 80.0], [560.0, 560.0, 80.0]],
        layer="BRK",
        kind="breakline",
        attrs={"code": "DEEP", "string": "1"},
    )
    pr.add_polyline(
        [[500.0, 560.0, 85.0], [560.0, 500.0, 85.0]],
        layer="BRK",
        kind="breakline",
        attrs={"code": "DEEP", "string": "2"},
    )

    res = auto_nudge_crossings(pr, max_displacement=2.0)
    assert res.initial_crossings == 3
    assert res.crossings_removed == 2
    assert res.remaining_crossings == 1
    assert len(res.residue) == 1
    assert {res.residue[0].seg_a_id, res.residue[0].seg_b_id} == {"DEEP1-01", "DEEP2-01"}
    # Point numbers never changed
    assert [p1.number, p2.number, p3.number, p4.number] == ["10", "11", "12", "13"]


def _corner_angles_deg(verts: np.ndarray) -> list[float]:
    m = len(verts)
    angles: list[float] = []
    for i in range(m):
        p_prev = verts[(i - 1) % m, :2]
        p_cur = verts[i, :2]
        p_next = verts[(i + 1) % m, :2]
        v1 = p_prev - p_cur
        v2 = p_next - p_cur
        cos_a = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))
        angles.append(math.degrees(math.acos(max(-1.0, min(1.0, cos_a)))))
    return angles


def test_square_shape_snaps_tilted_building_to_90_degrees_in_all_four_modes():
    """Phase 4 (§3.6): Square shape snaps corners to 90° under average, small, large, and lock_points."""
    theta = math.radians(30.0)
    u = np.array([math.cos(theta), math.sin(theta)])
    v = np.array([-math.sin(theta), math.cos(theta)])

    def make_building_project():
        pr = Project("Square Shape")
        # Slightly out-of-square rectangle in (u, v) frame:
        # bottom edge ~20.6, top edge ~19.4; left edge ~10.4, right edge ~9.6
        local_uv = [
            (0.0, 0.0),
            (20.6, -0.2),
            (20.0, 9.4),
            (0.6, 10.4),
        ]
        pts = []
        for idx, (ui, vi) in enumerate(local_uv, start=1):
            xy = 100.0 + ui * u + vi * v
            pts.append(pr.add_point(float(xy[0]), float(xy[1]), 50.0, number=str(idx), desc="BLDG"))
        line = pr.add_polyline(
            [[p.x, p.y, p.z] for p in pts],
            closed=True,
            layer="STRUCT-BLDG",
            attrs={"code": "BLDG", "string": "1", "point_ids": [p.id for p in pts]},
        )
        return pr, line, pts

    # 1) Average mode
    pr_avg, line_avg, _pts_avg = make_building_project()
    res_avg = square_shape(pr_avg, line_avg.id, mode="average")
    verts_avg = pr_avg.entities[line_avg.id].verts
    assert _corner_angles_deg(verts_avg) == pytest.approx([90.0, 90.0, 90.0, 90.0], abs=1e-4)
    avg_w = float(np.linalg.norm(verts_avg[1, :2] - verts_avg[0, :2]))
    avg_h = float(np.linalg.norm(verts_avg[2, :2] - verts_avg[1, :2]))

    # 2) Small mode holds smaller dimensions
    pr_sm, line_sm, _pts_sm = make_building_project()
    square_shape(pr_sm, line_sm.id, mode="small")
    verts_sm = pr_sm.entities[line_sm.id].verts
    assert _corner_angles_deg(verts_sm) == pytest.approx([90.0, 90.0, 90.0, 90.0], abs=1e-4)
    sm_w = float(np.linalg.norm(verts_sm[1, :2] - verts_sm[0, :2]))
    sm_h = float(np.linalg.norm(verts_sm[2, :2] - verts_sm[1, :2]))

    # 3) Large mode holds larger dimensions
    pr_lg, line_lg, _pts_lg = make_building_project()
    square_shape(pr_lg, line_lg.id, mode="large")
    verts_lg = pr_lg.entities[line_lg.id].verts
    assert _corner_angles_deg(verts_lg) == pytest.approx([90.0, 90.0, 90.0, 90.0], abs=1e-4)
    lg_w = float(np.linalg.norm(verts_lg[1, :2] - verts_lg[0, :2]))
    lg_h = float(np.linalg.norm(verts_lg[2, :2] - verts_lg[1, :2]))

    assert sm_w < avg_w < lg_w
    assert sm_h < avg_h < lg_h
    assert res_avg.orientation_deg == pytest.approx(30.0, abs=1.5)

    # 4) Lock points mode keeps vertex 0 at its exact original coordinate
    pr_lk, line_lk, pts_lk = make_building_project()
    orig_p0 = (pts_lk[0].x, pts_lk[0].y)
    square_shape(pr_lk, line_lk.id, mode="lock_points", locked_indices=[0])
    verts_lk = pr_lk.entities[line_lk.id].verts
    assert _corner_angles_deg(verts_lk) == pytest.approx([90.0, 90.0, 90.0, 90.0], abs=1e-4)
    assert (pts_lk[0].x, pts_lk[0].y) == pytest.approx(orig_p0, abs=1e-9)
    assert (verts_lk[0, 0], verts_lk[0, 1]) == pytest.approx(orig_p0, abs=1e-9)
    assert [p.number for p in pts_lk] == ["1", "2", "3", "4"]

