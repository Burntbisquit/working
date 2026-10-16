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


def test_geometry_dock_retains_selected_segment_after_linework_rebuild():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from plumbline.core.featurecodes import default_codes
    from plumbline.ui.app_state import AppState
    from plumbline.ui.geometry_dock import GeometryDock

    _app = QApplication.instance() or QApplication([])
    project = Project("Dock Selection")
    project.codes = default_codes()
    project.add_point(0, 0, 10, number="1", desc="EP1 ST")
    project.add_point(10, 0, 10, number="2", desc="EP1 END")
    project.add_point(0, 5, 10, number="3", desc="SW1 ST")
    project.add_point(10, 5, 10, number="4", desc="SW1 END")
    project.process_linework(order="number")

    state = AppState(project)
    dock = GeometryDock(state)
    dock.segments.selectRow(1)
    assert dock.current_segment is not None
    assert dock.current_segment.seg_id == "SW1-01"
    old_entity_id = dock.current_segment.entity_id

    # Editing a point in the Geometry panel triggers process_linework(order="number"),
    # which replaces derived polylines with new entity IDs while keeping seg_id stable.
    dock.points.item(0, 4).setText("1.5000")
    assert dock.current_segment is not None
    assert dock.current_segment.seg_id == "SW1-01"
    assert dock.current_segment.entity_id != old_entity_id


def test_segments_resolve_legacy_point_number_links_when_point_ids_are_absent():
    project = Project("Legacy linework")
    p1 = project.add_point(0, 0, 100, number="10", desc="EG ST")
    p2 = project.add_point(5, 0, 100, number="20", desc="EG END")
    project.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        "ROAD-GRAVEL",
        attrs={"code": "EG", "string": "", "points": ["10", "20"]},
        derived="linework",
        kind="breakline",
    )

    segments = build_segments(project)
    assert len(segments) == 1
    assert segments[0].linked
    assert segments[0].point_ids == (p1.id, p2.id)
    assert segments[0].point_numbers == ("10", "20")


def test_geometry_dock_runs_checks_and_applies_command_fix():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from plumbline.core.featurecodes import default_codes
    from plumbline.ui.app_state import AppState
    from plumbline.ui.geometry_dock import GeometryDock

    _app = QApplication.instance() or QApplication([])
    project = Project("Dock QA")
    project.codes = default_codes()
    p1 = project.add_point(0, 0, 100.0, number="1", desc="EP1")
    p2 = project.add_point(400, 0, 100.0, number="2", desc="EP1 END")
    project.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        "ROAD-PAVEMENT",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
        kind="breakline",
    )
    project.add_polyline(
        [[200.0, -25.0, 105.0], [200.0, 25.0, 105.0]],
        "ROAD-SIDEWALK",
        attrs={"code": "SW", "string": "1"},
        kind="breakline",
    )

    state = AppState(project)
    dock = GeometryDock(state)
    report = dock.run_checks()
    assert len(report.crossings) == 1
    assert len(report.long_segments) == 1
    assert dock.findings.rowCount() >= 3

    # Select the command-sequence finding with a repair proposal and apply the fix
    cmd_row = next(
        idx for idx, issue in enumerate(dock._issues)
        if issue.category == "command-sequence" and issue.proposal
    )
    dock.findings.selectRow(cmd_row)
    assert dock.apply_fix_btn.isEnabled()
    assert dock.apply_selected_fix()
    assert "ST" in project.points[p1.id].desc.upper()


def test_fix_point_errors_save_edits_keeps_docks_responsive():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QHeaderView
    from plumbline.core.featurecodes import default_codes
    from plumbline.ui.app_state import AppState
    from plumbline.ui.main_window import MainWindow
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    app = QApplication.instance() or QApplication([])
    project = Project("Responsive Save Edits")
    project.codes = default_codes()
    p1 = project.add_point(0.0, 0.0, 100.0, number="101", desc="EP1 ST")
    p2 = project.add_point(50.0, 0.0, 100.0, number="102", desc="EP1 END")
    p_bad = project.add_point(10.0, 10.0, 100.0, number="103", desc="EP1/NOTE")
    project.add_polyline(
        [[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z]],
        "ROAD-PAVEMENT",
        attrs={"code": "EP", "string": "1", "point_ids": [p1.id, p2.id]},
        derived="linework",
        kind="breakline",
    )

    state = AppState(project)
    win = MainWindow(state)
    win.show()
    app.processEvents()

    for table in (win.geometry.segments, win.geometry.points, win.geometry.findings, win.fieldbook_dock.tbl):
        for col in range(table.columnCount()):
            assert table.horizontalHeader().sectionResizeMode(col) != QHeaderView.ResizeToContents

    dlg = FixPointErrorsDialog(state, win)
    win._show_modal_qa_workbench(dlg)
    app.processEvents()

    sep_finding = next(
        (f for f in dlg.active_findings if not f.get("zero_check") and p_bad.id in f.get("pids", [])),
        None,
    )
    if sep_finding is not None:
        dlg._open_inline_editor(sep_finding)
        if getattr(dlg, "sep_corrections", None):
            dlg.sep_corrections[0][3].setCurrentText("Correct")
            assert dlg._action_save_issue()
            assert dlg.stack.currentIndex() == 0

    assert dlg._save_and_exit()
    app.processEvents()
    win.close()
    app.processEvents()




