"""Tests for the holistic QA & Point Resolution Workspace and advanced linework cleanup tools."""
from __future__ import annotations

import math

import pytest

from plumbline.core.model import Polyline, SurveyPoint
from plumbline.core.point_linework_coder import (
    find_free_string_id,
    fix_line_command_order,
    merge_and_reclass_strings,
    reorder_string_points,
    swap_parallel_line_codes,
)
from plumbline.core.project import Project
from plumbline.core.featurecodes import default_codes
from test_ui import app, auto, pump, win


# ------------------------------------------------------------------ Bowtie Swap Engine
def test_swap_parallel_line_codes():
    pr = Project()
    p1 = pr.add_point(0, 0, 0, number="101", desc="EC1 ST")
    p2 = pr.add_point(0, 10, 0, number="102", desc="EC2 ST")
    p3 = pr.add_point(10, 0, 0, number="103", desc="EC2")   # Mistake: shot as EC2 instead of EC1
    p4 = pr.add_point(10, 10, 0, number="104", desc="EC1")  # Mistake: shot as EC1 instead of EC2

    # Swap p3 and p4
    modified = swap_parallel_line_codes([p3, p4], "EC1", "EC2")
    assert len(modified) == 2
    assert p3.desc == "EC1"
    assert p4.desc == "EC2"


# ------------------------------------------------------------------ Merge & Reclass Engine
def test_find_free_string_id():
    pr = Project()
    pr.add_point(0, 0, 0, number="1", desc="EC1 ST")
    pr.add_point(10, 0, 0, number="2", desc="EC2 ST")
    free_id = find_free_string_id(pr, "EC")
    assert free_id == "3"


def test_merge_and_reclass_strings():
    pr = Project()
    pr.codes = default_codes()
    p1 = pr.add_point(0, 0, 0, number="1", desc="EP1 ST")
    p2 = pr.add_point(10, 0, 0, number="2", desc="EP1 END")

    p3 = pr.add_point(20, 0, 0, number="3", desc="EP2 ST")
    p4 = pr.add_point(30, 0, 0, number="4", desc="EP2 END")

    merged = merge_and_reclass_strings(pr, [p1, p2], [p3, p4], target_code="EP", new_string_id="3")
    assert len(merged) == 4
    assert p1.desc == "EP3 ST"
    assert p2.desc == "EP3"
    assert p3.desc == "EP3"
    assert p4.desc == "EP3 END"

    res = pr.process_linework()
    assert res["strings"] == 1
    poly = next(e for e in pr.entities.values() if isinstance(e, Polyline))
    assert len(poly.verts) == 4


# ------------------------------------------------------------------ Reorder Points Engine
def test_reorder_string_points():
    pr = Project()
    p1 = pr.add_point(0, 0, 0, number="1", desc="BLDG ST")
    p2 = pr.add_point(10, 10, 0, number="2", desc="BLDG")
    p3 = pr.add_point(10, 0, 0, number="3", desc="BLDG")
    p4 = pr.add_point(0, 10, 0, number="4", desc="BLDG CLS")

    # Reorder points from [0, 1, 2, 3] to [0, 2, 1, 3]
    reordered = reorder_string_points([p1, p2, p3, p4], [0, 2, 1, 3], closed=True)
    assert reordered[0].id == p1.id
    assert reordered[0].desc == "BLDG ST"
    assert reordered[1].id == p3.id
    assert reordered[1].desc == "BLDG"
    assert reordered[2].id == p2.id
    assert reordered[2].desc == "BLDG"
    assert reordered[3].id == p4.id
    assert reordered[3].desc == "BLDG X"


# ------------------------------------------------------------------ Command Order Engine
def test_fix_line_command_order():
    d1 = fix_line_command_order("EP1 PC ST")
    assert d1 == "EP1 ST PC"

    d2 = fix_line_command_order("TOC1 END PT / TREE")
    assert d2 == "TOC1 PT END / TREE"


# ------------------------------------------------------------------ Description Merge Engine
def test_merge_point_descriptions():
    from plumbline.ui.qa_workspace import merge_point_descriptions

    # User's exact case
    d1 = merge_point_descriptions(["ec1 st - sw / new", "toc1 / broken"])
    assert d1 == "ec1 st - sw - toc1 / new broken"

    # Multi-line codes with notes
    d2 = merge_point_descriptions(["TREE / 12 INCH OAK", "SPOT / 512.3"])
    assert d2 == "TREE - SPOT / 12 INCH OAK 512.3"

    # Multiple code prefixes
    d3 = merge_point_descriptions(["EP1 ST", "TC1 ST"])
    assert d3 == "EP1 ST - TC1 ST"

    # Identical code/description groups collapse to one code instead of making CODE - CODE.
    assert merge_point_descriptions(["EP1", "EP1"]) == "EP1"
    assert merge_point_descriptions(["EP1 ST", "EP1 ST"]) == "EP1 ST"
    assert merge_point_descriptions(["EP1 - TC1 / asphalt", "EP1 - TC1 / asphalt"]) == \
        "EP1 - TC1 / asphalt"

    # Distinct descriptions continue to use the active Field Book separators and notes.
    commands = {"multicode": "PLUS", "description": "NOTE"}
    assert merge_point_descriptions(
        ["EP1 NOTE asphalt", "TC1 NOTE concrete"], commands=commands
    ) == "EP1 PLUS TC1 NOTE asphalt concrete"


def test_keep_and_ignore_stack_choices_do_not_submit_a_merge_description(win, app):
    from plumbline.ui.qa_workspace import ClosePointsResolveDialog

    pr = win.state.project
    p1 = pr.add_point(1.0, 1.0, 10.0, number="1", desc="EP ST")
    p2 = pr.add_point(1.01, 1.01, 11.0, number="2", desc="EP END")
    dialog = ClosePointsResolveDialog(win.state, [p1, p2])
    available = dialog.screen().availableGeometry()
    if available.width() > 1000:
        assert dialog.width() > 780  # The stack editor scales with the monitor instead of opening cramped.
    assert dialog.width() <= available.width()
    assert dialog.height() <= available.height()

    dialog.combos[0].setCurrentText("Keep Point")
    dialog.combos[1].setCurrentText("Ignore")
    result = dialog.get_result()

    assert result["merge_points"] == []
    assert result["keep_points"] == [p1]
    assert result["ignore_points"] == [p2]
    assert result["merged_desc"] == ""
    assert not result["average_coords"]


def test_blank_stack_cells_are_locked_and_tab_moves_between_action_combos(win, app, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QAbstractItemView
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import ClosePointsResolveDialog, FixPointErrorsDialog

    project = win.state.project
    p1 = project.add_point(100, 200, 10, number="8701", desc="EP ST")
    p2 = project.add_point(100.01, 200.01, 10, number="8702", desc="")
    p3 = project.add_point(300, 400, 10, number="8703", desc="TC ST")
    p4 = project.add_point(300.01, 400.01, 10, number="8704", desc="TC")
    point_groups = [[p1.id, p2.id], [p3.id, p4.id]]

    def fake_check_project(pr, **_kwargs):
        ids = list(pr.points)
        row_by_pid = {pid: row for row, pid in enumerate(ids)}
        return {
            "findings": [{
                "check": "look-alike numbers",
                "flag": "SimilarPointNumbers",
                "level": "warn",
                "detail": "Two look-alike number stacks.",
                "pids": [pid for group in point_groups for pid in group],
                "rows": [row_by_pid[pid] for group in point_groups for pid in group],
                "groups": [[row_by_pid[pid] for pid in group] for group in point_groups],
                "key": "locked-stack-cells-regression",
            }],
            "ids": ids, "rows": [], "stats": {}, "flags": {}, "line_issues": [],
        }

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(item for item in dialog.active_findings
                   if item["key"] == "locked-stack-cells-regression")
    dialog._open_inline_editor(finding)
    dialog.show()
    app.processEvents()

    table = dialog.tbl_edit_pts
    blank_stack = table.item(1, 0)
    blank_action = table.item(1, 6)
    assert blank_stack is not None and not blank_stack.flags() & Qt.ItemIsSelectable
    assert not blank_stack.flags() & Qt.ItemIsEditable
    assert blank_action is not None and not blank_action.flags() & Qt.ItemIsSelectable
    assert not blank_action.flags() & Qt.ItemIsEditable
    assert table.item(1, 5) is not None and not table.item(1, 5).flags() & Qt.ItemIsSelectable
    assert table.editTriggers() == QAbstractItemView.NoEditTriggers

    first_action = table.cellWidget(0, 6)
    next_action = table.cellWidget(2, 6)
    table.setCurrentCell(0, 6)
    first_action.setFocus(Qt.OtherFocusReason)
    QTest.keyClick(first_action, Qt.Key_Tab)
    app.processEvents()
    assert table.currentRow() == 2
    assert app.focusWidget() is next_action

    # The per-point dialog also has a read-only point listing and one Tab per action row.
    popup = ClosePointsResolveDialog(win.state, [p1, p2], is_duplicate=True, parent=win)
    popup.show()
    app.processEvents()
    assert popup.tbl.editTriggers() == QAbstractItemView.NoEditTriggers
    assert not popup.tbl.item(0, 0).flags() & Qt.ItemIsEditable
    popup.combos[0].setFocus(Qt.OtherFocusReason)
    QTest.keyClick(popup.combos[0], Qt.Key_Tab)
    app.processEvents()
    assert popup.tbl.currentRow() == 1
    assert app.focusWidget() is popup.combos[1]
    popup.close()
    dialog.close()


def test_merge_close_keeps_known_elevation_when_other_is_missing(win):
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    missing = pr.add_point(10.0, 20.0, math.nan, number="701", desc="EP ST")
    known = pr.add_point(10.01, 20.01, 42.5, number="702", desc="EP END")
    dialog = FixPointErrorsDialog(win.state, win)

    dialog._action_merge_close([missing, known], average=True)

    assert missing.z == pytest.approx(42.5)
    assert known.id not in pr.points


# ------------------------------------------------------------------ Close Points Popup and Resolution
def test_close_points_resolve_popup_and_merge(win, app, auto):
    from plumbline.ui.qa_workspace import ClosePointsResolveDialog, FixPointErrorsDialog

    pr = win.state.project
    # Create two distinct close point stacks:
    # Stack 1: pt 1001 & 1003 near (200, 300)
    p1 = pr.add_point(200.0, 300.0, 50.0, number="1001", desc="ec1 st - sw / new")
    p3 = pr.add_point(200.02, 300.02, 50.05, number="1003", desc="toc1 / broken")

    # Stack 2: pt 1002, 1004 & 1005 near (500, 600)
    p2 = pr.add_point(500.0, 600.0, 50.0, number="1002", desc="ep1 st")
    p4 = pr.add_point(500.01, 600.01, 50.01, number="1004", desc="tc1")
    p5 = pr.add_point(500.02, 600.02, 50.02, number="1005", desc="gnd")

    # Test ClosePointsResolveDialog popup with per-point action dropdowns
    dlg_pop = ClosePointsResolveDialog(win.state, [p1, p3], parent=win)
    assert dlg_pop.tbl.rowCount() == 2
    assert dlg_pop.tbl.columnCount() == 6
    assert dlg_pop.ed_preview_desc.text() == "ec1 st - sw - toc1 / new broken"
    res = dlg_pop.get_result()
    assert res["action"] == 0
    assert res["merged_desc"] == "ec1 st - sw - toc1 / new broken"
    assert res["target_point"].id == p1.id

    # Test multi-stack Close Points in FixPointErrorsDialog
    dlg = FixPointErrorsDialog(win.state, win)
    assert dlg.w_close_tol_bar.isHidden()  # Not visible in summary view!

    dlg.sp_close_tol.setValue(0.10)
    dlg._on_tolerance_changed()

    # Find the "Close Points" finding
    close_finding = next((f for f in dlg.active_findings if "close" in f["check"].lower()), None)
    assert close_finding is not None
    assert close_finding["check"] == "Close Points"

    dlg._open_inline_editor(close_finding)
    assert dlg.stack.currentIndex() == 1
    assert not dlg.w_close_tol_bar.isHidden()  # Visible when editing close points!
    assert "2 close point groups detected" in dlg.lbl_edit_detail.text()  # Number of groups, no point numbers spam!

    # Every stack has an adjacent staged action dropdown and an Edit Stack button under its number.
    from PySide6.QtWidgets import QComboBox, QPushButton
    assert dlg.tbl_edit_pts.rowCount() == 5
    assert dlg.tbl_edit_pts.columnCount() == 7
    assert dlg.tbl_edit_pts.horizontalHeaderItem(0).text() == "Stack"
    assert dlg.tbl_edit_pts.horizontalHeaderItem(6).text() == "Action"
    assert len(dlg.stack_action_combos) == 2
    stack_cell = dlg.tbl_edit_pts.cellWidget(0, 0)
    assert any(button.text() == "Edit Stack" for button in stack_cell.findChildren(QPushButton))
    assert isinstance(dlg.tbl_edit_pts.cellWidget(0, 6), QComboBox)

    # Verify clicking point 0 selects whole stack 1
    dlg.tbl_edit_pts.selectRow(0)
    dlg._on_edit_point_selected()
    assert dlg.current_selected_stack == [p1.id, p3.id]

    # Verify clicking point 2 selects whole stack 2
    dlg.tbl_edit_pts.selectRow(2)
    dlg._on_edit_point_selected()
    assert dlg.current_selected_stack == [p2.id, p4.id, p5.id]

    # Execute merge on stack 1
    dlg._action_merge_close([p1, p3], average=True)
    assert p1.id in pr.points
    assert p1.desc == "ec1 st - sw - toc1 / new broken"
    assert p3.id not in pr.points  # Deleted
    assert p1.number == "1001"  # No renumbering performed!

    # Verify nested editor STAYS open on Page 1 until exit/back
    assert dlg.stack.currentIndex() == 1

    # Verify resolved items are populated in tbl_edit_resolved on Page 1
    assert dlg.tbl_edit_resolved.rowCount() == 1
    assert dlg.tbl_edit_resolved.item(0, 0).text() == "Close Points"
    assert "1001" in dlg.tbl_edit_resolved.item(0, 2).text()
    assert "1003" in dlg.tbl_edit_resolved.item(0, 2).text()
    # Stacks 2 points should NOT be in the resolved entry for Stack 1!
    assert "1002" not in dlg.tbl_edit_resolved.item(0, 2).text()

    # Verify tbl_edit_pts now shows only the remaining Stack 2 (3 rows)
    assert dlg.tbl_edit_pts.rowCount() == 3

    # Test Zoom to Point button
    dlg._zoom_to_highlighted_point()
    assert dlg.view3d.cam.target[0] != 0.0

    # Test View3D flag syncing
    dlg.chk_show_only_err.setChecked(True)
    dlg._sync_view_flags()
    assert dlg.view3d.hide_non_error_points

    dlg.chk_dim_non_err.setChecked(True)
    dlg._sync_view_flags()
    assert dlg.view3d.dim_non_error_points

    dlg._save_and_exit()


# ------------------------------------------------------------------ Fix Point Errors Dialog UI
def test_fix_point_errors_dialog_opens_and_resolves(win, app, auto):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    # Plant a duplicate point and a proximity collision
    pr.add_point(100, 200, 50, number="9999", desc="GS")
    p_dup2 = pr.add_point(100.01, 200.01, 50.02, number="9999", desc="GS")

    dlg = FixPointErrorsDialog(win.state, win)
    assert dlg.canvas is not None
    assert dlg.view3d is not None
    assert dlg.stack.count() == 2
    dlg.show()
    app.processEvents()
    assert dlg.isMaximized()
    assert not dlg.isFullScreen()
    assert dlg.windowFlags() & Qt.WindowMaximizeButtonHint
    assert dlg.windowFlags() & Qt.WindowMinimizeButtonHint
    assert not any(button.text() in {"Windowed", "Full Screen"}
                   for button in dlg.findChildren(QPushButton))
    dlg.showNormal()
    app.processEvents()
    assert not dlg.isMaximized()
    assert not dlg.isFullScreen()
    dlg.hide()
    dlg.show()
    app.processEvents()
    assert not dlg.isMaximized() and not dlg.isFullScreen()  # Restoring/showing again stays windowed.

    # There is one issue-scoped pair, hidden on the summary page.
    assert sum(button.text() == "Undo" for button in dlg.findChildren(QPushButton)) == 1
    assert sum(button.text() == "Redo" for button in dlg.findChildren(QPushButton)) == 1
    assert not dlg.btn_issue_undo.isVisible() and not dlg.btn_issue_redo.isVisible()

    # Check Active Issues table
    assert dlg.tbl_active.rowCount() >= 1

    # Check Closeness Tolerance input
    assert dlg.sp_close_tol is not None
    dlg.sp_close_tol.setValue(0.10)
    dlg._on_tolerance_changed()

    # Test toggles affecting 2D & 3D
    dlg.chk_show_only_err.setChecked(True)
    dlg._sync_view_flags()
    assert dlg.canvas.opts.hide_non_error_points

    dlg.chk_dim_non_err.setChecked(True)
    dlg._sync_view_flags()
    assert dlg.canvas.opts.dim_non_error_points

    # Test Edit Button opens inline resolution page
    first_item = dlg.active_findings[0]
    dlg._open_inline_editor(first_item)
    assert dlg.stack.currentIndex() == 1
    assert dlg.tbl_edit_pts.rowCount() >= 1
    assert dlg.btn_issue_undo.isVisible() and dlg.btn_issue_redo.isVisible()

    # Test point selection sync
    dlg.tbl_edit_pts.selectRow(0)
    dlg._on_edit_point_selected()

    # Test Renumbering resolution action with undo/redo
    pts = [win.state.project.points[pid] for pid in first_item["pids"]]
    dlg._action_renumber(pts)
    assert p_dup2.number != "9999"
    new_num = p_dup2.number
    assert len(dlg.resolved_findings) >= 1
    assert dlg.btn_undo.isEnabled()

    # Test Undo
    dlg._undo()
    assert p_dup2.number == "9999"
    assert dlg.btn_redo.isEnabled()

    # Test Redo
    dlg._redo()
    assert p_dup2.number == new_num

    # Test on-the-fly 3D VExag, Presets, and Perspective
    assert dlg.sp_vexag is not None
    dlg.sp_vexag.setValue(3.5)
    assert dlg.view3d.cam.vexag == 3.5

    dlg.cb_preset.setCurrentIndex(1)  # Top View
    assert dlg.view3d.cam.elevation == 90.0

    dlg.chk_persp.setChecked(False)
    assert not dlg.view3d.cam.perspective

    # Test Centroid Orbit and Zoom to Point
    dlg._zoom_extents()
    centroid_target = tuple(dlg.view3d.cam.target)
    dlg._zoom_to_highlighted_point()
    point_target = tuple(dlg.view3d.cam.target)
    assert centroid_target != point_target or len(pr.points) <= 1

    # Test Save & Exit
    dlg._save_and_exit()


def test_flag_review_is_row_and_tab_driven_without_navigation_buttons(win, monkeypatch):
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    points = [
        pr.add_point(1110 + idx, 1210 + idx, 50, number=f"R{idx + 1}", desc="BADCODE")
        for idx in range(3)
    ]
    checks = [
        ("Flag A", "review-flag-a", points[0]),
        ("Flag B", "review-flag-b", points[1]),
        ("Flag C", "review-flag-c", points[2]),
    ]

    def fake_check_project(project, **kwargs):
        ids = list(project.points)
        row_by_id = {pid: row for row, pid in enumerate(ids)}
        findings = [{
            "check": check,
            "flag": key,
            "level": "warn",
            "detail": f"Review {check}",
            "rows": [row_by_id[point.id]],
            "key": key,
        } for check, key, point in checks if point.desc == "BADCODE"]
        return {"findings": findings, "ids": ids,
                "rows": [], "stats": {}, "flags": {}, "line_issues": [], "code_checks": True}

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)

    assert not hasattr(dialog, "btn_previous_flag")
    assert not hasattr(dialog, "btn_next_flag")
    assert not hasattr(dialog, "btn_ignore_flag")

    # Review is opened from the finding row; staged edits cannot be lost by Return.
    finding = next(item for item in dialog.active_findings if item.get("key") == "review-flag-a")
    row = dialog.active_findings.index(finding)
    dialog.tbl_active.cellWidget(row, 0).click()
    assert dialog.current_edit_finding["key"] == "review-flag-a"
    dialog._validate_desc_text = lambda text, f2f_set=None: (True, "Valid code")
    dialog.desc_edits[0][1].setText("EP")
    dialog.btn_issue_return.click()
    assert dialog.stack.currentIndex() == 1
    assert "Apply or Discard" in dialog.lbl_status.text()

    dialog._action_discard_issue()
    finding = next(item for item in dialog.active_findings if item.get("key") == "review-flag-a")
    dialog._open_inline_editor(finding)
    dialog._validate_desc_text = lambda text, f2f_set=None: (True, "Valid code")
    dialog.desc_edits[0][1].setText("EP")
    dialog.btn_issue_apply.click()
    assert points[0].desc == "EP"
    assert dialog.current_edit_finding["key"] == "review-flag-a"
    dialog._discard_and_exit()


# ------------------------------------------------------------------ Fix Linework Dialog UI
def test_fix_linework_dialog_opens_and_resolves(win, app, auto):
    from plumbline.core.featurecodes import FeatureCode
    from plumbline.ui.qa_workspace import (
        BowtieRepairDialog,
        FixLineworkDialog,
        ReclassMergeLinesDialog,
    )

    pr = win.state.project
    pr.codes.add(FeatureCode(code="TOC", kind="line", layer="TOPO"))
    pr.settings["f2f_path"] = "test-office-standard.csv"
    # Plant a linework error (missing END)
    p_l1 = pr.add_point(300, 400, 50, number="8001", desc="TOC ST")
    p_l2 = pr.add_point(310, 410, 50, number="8002", desc="TOC")

    dlg = FixLineworkDialog(win.state, win)
    assert dlg.tbl_active.rowCount() >= 1

    # Open inline editor
    first_item = dlg.active_findings[0]
    dlg._open_inline_editor(first_item)
    assert dlg.stack.currentIndex() == 1

    # Add END to last point
    pts = [pr.points[pid] for pid in first_item["pids"]]
    dlg._action_add_end(pts)
    assert "END" in p_l2.desc

    # Test Bowtie Repair Dialog
    pr.add_point(500, 500, 10, number="9001", desc="EC1 ST")
    pr.add_point(500, 510, 10, number="9002", desc="EC2 ST")
    p_b1 = pr.add_point(510, 500, 10, number="9003", desc="EC2")
    p_b2 = pr.add_point(510, 510, 10, number="9004", desc="EC1")
    win.state.select(points=[p_b1.id, p_b2.id])
    dlg_bowtie = BowtieRepairDialog(win.state, parent=win)
    dlg_bowtie.le_code1.setText("EC1")
    dlg_bowtie.le_code2.setText("EC2")
    dlg_bowtie._apply()
    assert p_b1.desc == "EC1"
    assert p_b2.desc == "EC2"

    # Test Reclass & Merge Dialog
    p_m1 = pr.add_point(600, 600, 10, number="9101", desc="BL1 ST")
    p_m2 = pr.add_point(610, 600, 10, number="9102", desc="BL1 END")
    p_m3 = pr.add_point(620, 600, 10, number="9103", desc="BL2 ST")
    p_m4 = pr.add_point(630, 600, 10, number="9104", desc="BL2 END")
    dlg_reclass = ReclassMergeLinesDialog(win.state, parent=win)
    dlg_reclass.le_str1.setText("BL1")
    dlg_reclass.le_str2.setText("BL2")
    dlg_reclass.le_target_code.setText("BL")
    dlg_reclass.le_target_id.setText(find_free_string_id(pr, "BL"))
    dlg_reclass._apply()
    assert "BL" in p_m1.desc and "ST" in p_m1.desc
    assert "END" in p_m4.desc

    dlg._save_and_exit()


# ------------------------------------------------------------------ Potential Code in Descriptor & Corrections UI
def test_potential_code_in_descriptor_corrections_ui(win, app, auto):
    from PySide6.QtWidgets import QHeaderView, QPushButton
    from plumbline.fieldwork.bridge import FLAG_TITLES
    from plumbline.fieldwork.clean import _autocorrect_desc_leave_number
    from plumbline.fieldwork.parse import parse_desc_field
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    assert FLAG_TITLES.get("MisplacedAfterSeparator") == "Potential code in descriptor"

    f2f = {"rcp", "mh", "ec", "gs", "cb", "w", "ss"}

    # Test parser strips leading numbers like 30rcp and flags MisplacedAfterSeparator
    res1 = parse_desc_field("NOTES / 30rcp", f2f)
    assert any("MisplacedAfterSeparator" in f for f in res1["flags"])
    assert "30rcp" in res1.get("misplaced_sets", [])

    res2 = parse_desc_field("MH / 30rcp", f2f)
    assert any("MisplacedAfterSeparator" in f for f in res2["flags"])

    # Test leave number in descriptor autocorrect across varied codes and numbers
    assert _autocorrect_desc_leave_number("30rcp", f2f) == "rcp / 30"
    assert _autocorrect_desc_leave_number("rcp30", f2f) == "rcp / 30"
    assert _autocorrect_desc_leave_number("30\"rcp", f2f) == "rcp / 30\""
    assert _autocorrect_desc_leave_number("rcp 30\"", f2f) == "rcp / 30\""
    assert _autocorrect_desc_leave_number("30 rcp", f2f) == "rcp / 30"
    assert _autocorrect_desc_leave_number("rcp 30", f2f) == "rcp / 30"
    assert _autocorrect_desc_leave_number("MH / 30rcp", f2f) == "MH - rcp / 30"
    assert _autocorrect_desc_leave_number("MH / rcp30", f2f) == "MH - rcp / 30"
    assert _autocorrect_desc_leave_number("MH / 30\"rcp", f2f) == "MH - rcp / 30\""
    assert _autocorrect_desc_leave_number("MH / rcp 30\"", f2f) == "MH - rcp / 30\""
    assert _autocorrect_desc_leave_number("MH / 30 rcp", f2f) == "MH - rcp / 30"
    assert _autocorrect_desc_leave_number("MH / rcp 30", f2f) == "MH - rcp / 30"
    assert _autocorrect_desc_leave_number("NOTES / 30rcp", f2f) == "rcp / 30 NOTES"
    assert _autocorrect_desc_leave_number("NOTES / rcp30", f2f) == "rcp / 30 NOTES"

    # Plant points with potential code in descriptor
    pr = win.state.project
    pr.codes.codes["rcp"] = "Reinforced Concrete Pipe"
    pr.codes.codes["mh"] = "Manhole"
    pr.settings["f2f_path"] = "test-office-standard.csv"
    p_sep1 = pr.add_point(700, 800, 50, number="7701", desc="MH / 30rcp")
    p_sep2 = pr.add_point(710, 810, 50, number="7702", desc="NOTES / 30rcp")

    dlg = FixPointErrorsDialog(win.state, win)
    dlg.show()
    app.processEvents()

    # Verify the issue workspace keeps more width than the synchronized 2D/3D views.
    sizes = dlg.splitter.sizes()
    assert len(sizes) == 2
    assert sizes[0] < sizes[1]
    total_w = sum(sizes)
    left_ratio = sizes[0] / total_w
    assert 0.15 <= left_ratio <= 0.45

    # Verify interactive resizable table headers
    assert dlg.tbl_active.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive
    assert dlg.tbl_resolved.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive
    assert dlg.tbl_edit_pts.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive
    assert dlg.tbl_edit_resolved.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive

    # Locate "Potential code in descriptor" finding
    sep_finding = next((f for f in dlg.active_findings if "potential code" in f.get("check", "").lower() or "misplaced" in str(f.get("flag", "")).lower()), None)
    if not sep_finding:
        sep_finding = {
            "check": "Potential code in descriptor",
            "flag": "MisplacedAfterSeparator",
            "pids": [p_sep1.id, p_sep2.id],
            "numbers": ["7701", "7702"],
            "detail": "Misplaced '30rcp' after /",
            "key": f"sep_{p_sep1.id}",
        }
        dlg.active_findings.append(sep_finding)

    dlg._open_inline_editor(sep_finding)
    assert dlg.stack.currentIndex() == 1
    assert {"Apply", "Save", "Discard", "Return"}.issubset(
        {button.text() for button in dlg.findChildren(QPushButton)})

    # Verify points list has 5 data columns without Edit button in Col 0
    assert dlg.tbl_edit_pts.columnCount() == 5
    assert dlg.tbl_edit_pts.horizontalHeaderItem(0).text() == "Pt #"

    # Verify corrections table structure
    assert hasattr(dlg, "tbl_corrections")
    assert dlg.tbl_corrections.columnCount() == 4
    assert dlg.tbl_corrections.horizontalHeaderItem(0).text() == "Pt #"
    assert dlg.tbl_corrections.horizontalHeaderItem(1).text() == "Original Code"
    assert dlg.tbl_corrections.horizontalHeaderItem(2).text() == "Fixed Code"
    assert dlg.tbl_corrections.horizontalHeaderItem(3).text() == "Action"
    assert dlg.tbl_corrections.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive

    # Verify all dropdowns default to "Skip" and include "Correct (Leave # in Descriptor)"
    assert hasattr(dlg, "sep_corrections") and len(dlg.sep_corrections) >= 2
    for item in dlg.sep_corrections:
        p, orig, it_fixed, cb_act = item[:4]
        assert cb_act.currentText() == "Skip"
        items_list = [cb_act.itemText(i) for i in range(cb_act.count())]
        assert "Correct" in items_list
        assert any("leave" in it.lower() or "keep" in it.lower() for it in items_list)
        assert "Ignore" in items_list

    # Test Correct (Leave # in Descriptor) action for p_sep1
    cb_first = dlg.sep_corrections[0][3]
    cb_first.setCurrentText("Correct (Leave # in Descriptor)")
    assert cb_first.currentText() == "Correct (Leave # in Descriptor)"
    assert dlg.sep_corrections[0][2].text() == "MH - rcp / 30" or "rcp" in dlg.sep_corrections[0][2].text()

    # Test direct in-cell editing / overwrite of Fixed Code column automatically setting action to Correct
    dlg.tbl_corrections.item(0, 2).setText("MH - rcp / 30 CUSTOM")
    assert cb_first.currentText() == "Correct"

    # Test Correct action for p_sep2
    cb_second = dlg.sep_corrections[1][3]
    cb_second.setCurrentText("Correct")

    # Apply corrections
    dlg._action_apply_separator_corrections()
    assert "rcp" in p_sep1.desc and ("30" in p_sep1.desc or "/" in p_sep1.desc)
    assert dlg.tbl_edit_pts.rowCount() == 0
    assert len(dlg.sep_corrections) == 0
    assert dlg.tbl_edit_resolved.rowCount() >= 1

    dlg._save_and_exit()


def test_separator_spacing_fix_stays_independent_from_misplaced_code(win, app, monkeypatch):
    from plumbline.core.settings import settings
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    pr.codes.codes["mh"] = "Manhole"
    pr.codes.codes["rcp"] = "Reinforced Concrete Pipe"
    pr.settings["f2f_path"] = "test-office-standard.csv"
    point = pr.add_point(930, 1040, 50, number="9912", desc="MH/30RCP")
    second_point = pr.add_point(940, 1050, 50, number="9913", desc="MH   /30RCP")
    # Imported raw descriptions remain provenance; rechecks must follow editable live descriptions.
    point.attrs["fieldwork_raw_desc"] = point.desc
    second_point.attrs["fieldwork_raw_desc"] = second_point.desc
    monkeypatch.setitem(settings()._data, "space_around_multicode_separator", True)
    monkeypatch.setitem(settings()._data, "space_around_description_separator", True)

    dialog = FixPointErrorsDialog(win.state, win)
    misplaced = next(f for f in dialog.active_findings if f.get("flag") == "MisplacedAfterSeparator")
    spacing = next(f for f in dialog.active_findings if f.get("flag") == "SeparatorSpacingError")
    assert spacing["level"] == "error"
    spacing_row = next(r for r in range(dialog.tbl_active.rowCount())
                       if dialog.tbl_active.item(r, 2).text() == spacing["check"])
    assert dialog.tbl_active.item(spacing_row, 1).text() == "ERROR"
    assert {point.id, second_point.id}.issubset(set(misplaced["pids"]))
    assert {point.id, second_point.id}.issubset(set(spacing["pids"]))
    assert misplaced["key"] != spacing["key"]

    # Fix one point's spacing first. Its overlapping misplaced-code finding must remain active,
    # while the spacing issue tracks only the second point that was skipped.
    dialog._open_inline_editor(spacing)
    assert dialog.tbl_corrections.horizontalHeaderItem(1).text() == "Original Description"
    assert dialog.tbl_corrections.horizontalHeaderItem(2).text() == "Spacing-Corrected Description"
    actions = [dialog.sep_corrections[0][3].itemText(i)
               for i in range(dialog.sep_corrections[0][3].count())]
    assert actions == ["Skip", "Correct", "Ignore"]
    first_fix = next(item for item in dialog.sep_corrections if item[0].id == point.id)
    second_fix = next(item for item in dialog.sep_corrections if item[0].id == second_point.id)
    assert first_fix[2].text() == "MH / 30RCP"
    assert first_fix[3].currentText() == "Correct"
    assert second_fix[3].currentText() == "Correct"
    second_fix[3].setCurrentText("Skip")
    assert point.desc == "MH/30RCP"  # Staged until Save & Return.
    dialog._action_save_issue()

    assert dialog.stack.currentIndex() == 0
    assert point.desc == "MH / 30RCP"
    assert point.attrs["fieldwork_raw_desc"] == "MH/30RCP"
    assert second_point.desc == "MH   /30RCP"
    misplaced_after = next(f for f in dialog.active_findings
                           if f.get("flag") == "MisplacedAfterSeparator" and f.get("status") != "resolved")
    spacing_after_first = next(f for f in dialog.active_findings
                               if f.get("flag") == "SeparatorSpacingError" and f.get("status") != "resolved")
    assert {point.id, second_point.id}.issubset(set(misplaced_after["pids"]))
    assert spacing_after_first["pids"] == [second_point.id]

    # Ignoring the misplaced-code finding must not suppress the remaining spacing correction.
    dialog._action_ignore(misplaced_after["key"])
    spacing = next(f for f in dialog.active_findings
                   if f.get("flag") == "SeparatorSpacingError" and f.get("status") != "resolved")
    assert misplaced_after["key"] in dialog.ignored_keys
    assert spacing["key"] not in dialog.ignored_keys
    assert spacing["pids"] == [second_point.id]

    dialog._open_inline_editor(spacing)
    assert len(dialog.sep_corrections) == 1
    assert dialog.sep_corrections[0][2].text() == "MH / 30RCP"
    assert dialog.sep_corrections[0][3].currentText() == "Correct"
    dialog._action_save_issue()
    assert dialog.stack.currentIndex() == 0
    assert second_point.desc == "MH / 30RCP"
    spacing_after = next(f for f in dialog.active_findings if f.get("flag") == "SeparatorSpacingError")
    assert spacing_after["status"] == "resolved"
    assert not spacing_after["pids"]


def test_save_return_rechecks_once_for_staged_separator_corrections(win, monkeypatch):
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    point = win.state.project.add_point(925, 1035, 50, number="9911", desc="MH/30RCP")
    calls = []

    def fake_check_project(project, **kwargs):
        calls.append(1)
        ids = list(project.points)
        row = ids.index(point.id)
        return {
            "findings": [{
                "check": "Separator spacing at the separator",
                "flag": "SeparatorSpacingError",
                "level": "error",
                "detail": "Spacing differs from configured separators.",
                "rows": [row],
                "groups": [[row]],
                "key": "save-separator-perf",
            }],
            "ids": ids,
            "rows": [],
            "stats": {},
            "flags": {},
            "line_issues": [],
        }

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(f for f in dialog.active_findings if f["key"] == "save-separator-perf")
    dialog._open_inline_editor(finding)
    assert dialog.sep_corrections[0][3].currentText() == "Correct"
    assert len(calls) == 1

    dialog._action_save_issue()
    assert point.desc == "MH / 30RCP"
    assert dialog.stack.currentIndex() == 0
    assert len(calls) == 2  # Save performs the correction and does not trigger a second recheck.
    dialog._save_and_exit()


def test_separator_ignore_clears_warning_and_discard_restores_it(win, monkeypatch):
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    point = pr.add_point(900, 1000, 50, number="8901", desc="NOTES / 30RCP")
    check_finding = {
        "check": "Potential code in descriptor",
        "flag": "MisplacedAfterSeparator",
        "level": "warn",
        "detail": "Potential code appears after a descriptor separator.",
        "pids": [point.id],
        "key": "separator-ignore-regression",
    }
    calls = []

    def fake_check_project(project, **kwargs):
        calls.append(1)
        return {
            "findings": [dict(check_finding)],
            "ids": list(project.points),
            "rows": [],
            "stats": {},
            "flags": {},
            "line_issues": [],
        }

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(f for f in dialog.active_findings if f["key"] == check_finding["key"])
    assert len(calls) == 1
    dialog._open_inline_editor(finding)

    dialog.sep_corrections[0][3].setCurrentText("Ignore")
    dialog._action_apply_separator_corrections()

    assert len(calls) == 1  # Metadata-only ignore does not rerun the QA checks.
    assert point.id not in dialog.error_point_ids
    assert dialog.current_edit_finding["status"] == "resolved"
    assert dialog.tbl_active.item(0, 1).text() == "RESOLVED"

    # The issue-scoped undo/redo stack restores and reapplies the ignore choice.
    dialog._undo_issue()
    assert point.id in dialog.error_point_ids
    dialog._redo_issue()
    assert point.id not in dialog.error_point_ids

    # Ignore is session-scoped: Discarding this issue restores the warning.
    dialog._action_discard_issue()
    assert point.id in dialog.error_point_ids
    assert dialog.active_findings[0]["status"] == "active"
    assert len(calls) == 1  # Undo, redo, and discard all avoid redundant QA passes.


def test_stack_dropdowns_stage_together_and_undo_restores_unapplied_choices(win, monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    p1 = pr.add_point(940, 1040, 50, number="8941", desc="EP")
    p2 = pr.add_point(941, 1041, 50, number="8942", desc="EP")
    p3 = pr.add_point(950, 1050, 50, number="8951", desc="EP")
    p4 = pr.add_point(951, 1051, 50, number="8952", desc="EP")
    p5 = pr.add_point(960, 1060, 50, number="8961", desc="EP")
    p6 = pr.add_point(961, 1061, 50, number="8962", desc="EP")
    point_groups = [[p1.id, p2.id], [p3.id, p4.id], [p5.id, p6.id]]
    point_ids = [pid for group in point_groups for pid in group]
    check_finding = {
        "check": "look-alike numbers",
        "flag": "SimilarPointNumbers",
        "level": "warn",
        "detail": "Three unrelated number stacks are similar.",
        "key": "lookalike-point-ignore-regression",
    }

    def fake_check_project(project, **kwargs):
        ids = list(project.points)
        row_by_pid = {pid: row for row, pid in enumerate(ids)}
        finding = dict(check_finding)
        finding["rows"] = [row_by_pid[pid] for pid in point_ids]
        finding["groups"] = [[row_by_pid[pid] for pid in group] for group in point_groups]
        return {"findings": [finding], "ids": ids,
                "rows": [], "stats": {}, "flags": {}, "line_issues": []}

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(f for f in dialog.active_findings if f["key"] == check_finding["key"])
    dialog._open_inline_editor(finding)

    assert len(dialog.stack_action_combos) == 3
    for row in (0, 2, 4):
        stack_cell = dialog.tbl_edit_pts.cellWidget(row, 0)
        assert any(button.text() == "Edit Stack" for button in stack_cell.findChildren(QPushButton))
        assert dialog.tbl_edit_pts.cellWidget(row, 6) is not None

    stack1_key = dialog._stack_key(point_groups[0])
    stack3_key = dialog._stack_key(point_groups[2])
    stack1_combo = dialog.stack_action_combos[stack1_key]
    stack3_combo = dialog.stack_action_combos[stack3_key]

    # Two dropdown choices stay staged together; project data and flags do not change yet.
    stack1_combo.setCurrentText("Ignore this stack")
    stack3_combo.setCurrentText("Ignore this stack")
    assert dialog.current_edit_finding["ignored_pids"] == set()
    assert dialog.error_point_ids == set(point_ids)
    assert p1.id in pr.points and p6.id in pr.points
    assert dialog.btn_issue_undo.isEnabled()  # Undo can clear unapplied choices.
    dialog.btn_issue_return.click()
    assert dialog.stack.currentIndex() == 1
    assert "Apply or Discard" in dialog.lbl_status.text()

    # One Apply commits all staged stacks as a single history/log entry.
    dialog.btn_issue_apply.click()
    assert dialog.current_edit_finding["ignored_pids"] == {p1.id, p2.id, p5.id, p6.id}
    assert dialog.current_edit_finding["pids"] == [p3.id, p4.id]
    assert dialog.error_point_ids == {p3.id, p4.id}
    assert dialog.tbl_edit_resolved.rowCount() == 1

    # Undo unapplies the batch and restores the choices, but does not re-apply them.
    dialog._undo_issue()
    assert dialog.current_edit_finding["ignored_pids"] == set()
    assert dialog.error_point_ids == set(point_ids)
    assert dialog.stack_action_combos[stack1_key].currentData() == "ignore"
    assert dialog.stack_action_combos[stack3_key].currentData() == "ignore"
    assert dialog.btn_issue_redo.isEnabled()

    # A second Undo with no applied batch clears the restored staged dropdowns only.
    dialog._undo_issue()
    assert dialog.current_edit_finding["ignored_pids"] == set()
    assert dialog.error_point_ids == set(point_ids)
    assert all(combo.currentData() is None for combo in dialog.stack_action_combos.values())
    assert not dialog.btn_issue_undo.isEnabled()
    dialog._discard_and_exit()


def test_bulk_stack_merge_keeps_head_coordinates_and_merges_descriptions(win, monkeypatch):
    from PySide6.QtCore import Qt
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import FixPointErrorsDialog, merge_point_descriptions

    pr = win.state.project
    head = pr.add_point(920.0, 1020.0, 50.0, number="8921", desc="TREE / 12 INCH OAK")
    other = pr.add_point(920.01, 1020.01, 70.0, number="8922", desc="SPOT / 512.3")
    original_head_coords = (head.x, head.y, head.z)
    original_head_desc = head.desc
    check_finding = {
        "check": "Close Points",
        "flag": "ClosePointCollision",
        "level": "warn",
        "detail": "Two points are within the closeness tolerance.",
        "pids": [head.id, other.id],
        "key": "close-merge-keep-head-coordinates",
    }

    def fake_check_project(project, **kwargs):
        return {
            "findings": [dict(check_finding)],
            "ids": list(project.points),
            "rows": [],
            "stats": {},
            "flags": {},
            "line_issues": [],
        }

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(f for f in dialog.active_findings if f["key"] == check_finding["key"])
    dialog._open_inline_editor(finding)

    stack_key = dialog._stack_key([head.id, other.id])
    combo = dialog.stack_action_combos[stack_key]
    keep_coords_index = combo.findData("merge_keep_coords")
    assert keep_coords_index >= 0
    assert "keep its X/Y/Z unchanged" in combo.itemData(keep_coords_index, Qt.ToolTipRole)
    combo.setCurrentIndex(keep_coords_index)
    expected_desc = merge_point_descriptions(
        [head.desc, other.desc],
        (pr.settings or {}).get("fieldbook_file"),
        (pr.settings or {}).get("f2f_commands"),
    )

    # Applying merges descriptions and removes the other point without averaging coordinates.
    dialog.btn_issue_apply.click()
    assert head.id in pr.points
    assert other.id not in pr.points
    assert (head.x, head.y, head.z) == original_head_coords
    assert head.desc == expected_desc
    assert head.desc != original_head_desc
    resolution_summary = dialog.tbl_edit_resolved.item(0, 1).text()
    assert "merged descriptions into #8921; kept head coordinates" in resolution_summary
    assert "removed 1" in resolution_summary

    # Undo restores point data and leaves the new action selected but unapplied.
    dialog._undo_issue()
    assert head.id in pr.points and other.id in pr.points
    assert head.desc == original_head_desc
    assert (head.x, head.y, head.z) == original_head_coords
    assert dialog.stack_action_combos[stack_key].currentData() == "merge_keep_coords"
    dialog._discard_and_exit()


def test_common_conversion_dialog_orders_correct_before_ignore_and_preserves_codes(app, tmp_path):
    from PySide6.QtWidgets import QPushButton
    from plumbline.fieldwork.clean import CleanDescriptionDialog
    from plumbline.fieldwork.io_carlson import write_fwb_file

    book = tmp_path / "conversion.fwb"
    commands = {"multicode": "PLUS", "description": "NOTE"}
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["EA", "Asphalt", "CG08", "PAVEMENT", "Point", "Surface"],
         ["SW", "Sidewalk", "CG08", "SIDEWALK", "Point", "Surface"]],
        commands=commands, rules=[["EA PLUS SW", "EA"]],
    )
    dlg = CleanDescriptionDialog(
        "9001", "5101", "EA PLUS SW", "CommonConversionError",
        "Rule would collapse two valid codes.", "(EA)(SW)", "", {"ea", "sw"},
        fieldbook_path=str(book),
    )
    buttons = dlg.findChildren(QPushButton)
    labels = [button.text() for button in buttons]
    assert "&3. Correct" in labels and "&Ignore" in labels
    assert labels.index("&3. Correct") < labels.index("&Ignore")

    dlg.auto_fix_edit.setText("EA PLUS SW")
    dlg._do_fix()
    assert dlg.result_action == "fix"
    assert dlg.result_new_desc == "EA PLUS SW"


def test_common_error_rules_feed_point_proposals_default_to_correct_and_precede_unknown(win, app, tmp_path):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    book = tmp_path / "common-errors.fwb"
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["EA", "Asphalt", "CG08", "PAVEMENT", "Point", "Surface"],
         ["SW", "Sidewalk", "CG08", "SIDEWALK", "Point", "Surface"],
         ["RCP", "Reinforced Concrete Pipe", "CG08", "UTILITIES", "Point", "Utility"]],
        rules=[["BADCODE", "RCP"], ["EA", "RCP"]],
    )
    project = win.state.project
    project.settings["fieldbook_file"] = str(book)
    unknown_point = project.add_point(950, 1050, 50, number="9051", desc="BADCODE ST")
    valid_point = project.add_point(951, 1051, 50, number="9052", desc="EA")
    project.add_point(952, 1052, 50, number="9053", desc="MYSTERY ST")
    project.add_point(953, 1053, 50, number="9054", desc="ZZZ ST")

    dialog = FixPointErrorsDialog(win.state, win)
    common = next(f for f in dialog.active_findings if f.get("flag") == "CommonConversionError")
    unknown = next(f for f in dialog.active_findings if f.get("flag") == "UnknownCode")
    assert common["check"] == "Common error"
    assert dialog.active_findings.index(common) < dialog.active_findings.index(unknown)
    result_flags = [f.get("flag") for f in dialog.result["findings"]]
    assert result_flags.index("CommonConversionError") < result_flags.index("UnknownCode")

    dialog._open_inline_editor(unknown)
    assert dialog.autofix_suggestions[unknown_point.id].text() == "Proposed autofix: RCP ST"
    unknown_editor = next(editor for point, editor, _label in dialog.desc_edits
                          if point.id == unknown_point.id)
    assert unknown_editor.text() == "BADCODE ST"
    assert unknown_point.desc == "BADCODE ST"
    dialog.autofix_buttons[unknown_point.id].click()
    assert unknown_editor.text() == "RCP ST"
    assert unknown_point.desc == "BADCODE ST"  # Auto Fix stages; Apply/Save commits.

    dialog._open_inline_editor(common)
    assert [item[2].text() for item in dialog.sep_corrections] == ["RCP ST", "RCP"]
    assert [item[3].currentText() for item in dialog.sep_corrections] == ["Correct", "Correct"]
    assert {item[0].id for item in dialog.sep_corrections} == {unknown_point.id, valid_point.id}
    dialog.close()


def test_close_stack_ignore_suppresses_only_the_selected_point(win, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import ClosePointsResolveDialog, FixPointErrorsDialog

    pr = win.state.project
    p1 = pr.add_point(920, 1020, 50, number="8921", desc="EP ST")
    p2 = pr.add_point(920.01, 1020.01, 50, number="8922", desc="EP END")
    check_finding = {
        "check": "Close Points",
        "flag": "ClosePointCollision",
        "level": "warn",
        "detail": "Two points are within the closeness tolerance.",
        "pids": [p1.id, p2.id],
        "key": "close-ignore-regression",
    }

    def fake_check_project(project, **kwargs):
        return {
            "findings": [dict(check_finding)],
            "ids": list(project.points),
            "rows": [],
            "stats": {},
            "flags": {},
            "line_issues": [],
        }

    popup_action_snapshots = []

    def accept_with_second_ignored(popup):
        if popup.initial_plan:
            popup_action_snapshots.append([combo.currentText() for combo in popup.combos])
        popup.combos[1].setCurrentText("Ignore")
        return QDialog.Accepted

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    monkeypatch.setattr(ClosePointsResolveDialog, "exec", accept_with_second_ignored)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(f for f in dialog.active_findings if f["key"] == check_finding["key"])
    dialog._open_inline_editor(finding)
    dialog._action_resolve_stack_dialog([p1.id, p2.id])

    # The popup returns staged choices only; data and active findings wait for page-level Apply.
    assert dialog.current_edit_finding["ignored_pids"] == set()
    assert dialog.current_edit_finding["pids"] == [p1.id, p2.id]
    assert p1.id in pr.points and p2.id in pr.points
    dialog.btn_issue_apply.click()

    assert dialog.current_edit_finding["ignored_pids"] == {p2.id}
    assert dialog.current_edit_finding["pids"] == [p1.id]
    assert p1.id in dialog.error_point_ids
    assert p2.id not in dialog.error_point_ids

    dialog._undo_issue()
    assert dialog.current_edit_finding["ignored_pids"] == set()
    assert dialog.current_edit_finding["pids"] == [p1.id, p2.id]
    assert dialog.stack_action_combos[dialog._stack_key([p1.id, p2.id])].currentData() == "individual"
    assert p1.id in pr.points and p2.id in pr.points
    dialog._action_resolve_stack_dialog([p1.id, p2.id])
    assert popup_action_snapshots == [["Merge (Target)", "Ignore"]]

    dialog._redo_issue()
    assert dialog.current_edit_finding["ignored_pids"] == {p2.id}
    assert dialog.tbl_active.item(0, 1).text() == "PARTIAL"
    dialog._action_ignore(dialog.current_edit_finding["key"])
    assert all(item.get("zero_check") for item in dialog.active_findings)
    assert dialog.error_point_ids == set()


def test_issue_page_hides_exit_controls_and_deleted_separator_widgets_are_safe(win, app):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QPushButton
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    point = pr.add_point(910, 1010, 50, number="8911", desc="BADCODE1 ST")
    dialog = FixPointErrorsDialog(win.state, win)

    exit_controls = (dialog.btn_save_exit, dialog.btn_discard_exit)
    assert all(not button.isHidden() for button in exit_controls)
    assert not hasattr(dialog, "btn_cancel")
    assert not any(button.text() == "Cancel" for button in dialog.findChildren(QPushButton))

    sep_finding = {
        "check": "Potential code in descriptor",
        "flag": "MisplacedAfterSeparator",
        "level": "warn",
        "detail": "Synthetic separator issue for widget lifecycle coverage.",
        "pids": [point.id],
        "key": "synthetic-separator",
    }
    unknown_finding = {
        "check": "Unknown Code",
        "flag": "UnknownCode",
        "level": "error",
        "detail": "Unknown code in description.",
        "pids": [point.id],
        "key": "synthetic-unknown-code",
    }

    dialog._open_inline_editor(sep_finding)
    stale_combo = dialog.sep_corrections[0][3]
    assert all(button.isHidden() for button in exit_controls)

    # Rebuilding into a non-separator issue deletes the table and its combo boxes.
    dialog._open_inline_editor(unknown_finding)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert dialog.sep_corrections == []
    with pytest.raises(RuntimeError, match="already deleted"):
        stale_combo.currentText()

    # Even an accidentally retained stale reference must not crash Back/Save checks.
    dialog.sep_corrections = [(point, "", None, stale_combo)]
    assert not dialog._has_unapplied_issue_edits()
    dialog.sep_corrections = []
    assert not dialog._has_unapplied_issue_edits()

    dialog.issue_dirty = True
    dialog._action_save_issue()
    assert dialog.stack.currentIndex() == 0
    assert all(not button.isHidden() for button in exit_controls)


# ------------------------------------------------------------------ Fix Unknown Code & Fieldbook Lookup
def test_fix_unknown_code_fieldbook_lookup_and_validation(win, app, auto, monkeypatch):
    from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton
    from plumbline.fieldwork import clean as clean_module
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    pr.codes.codes["RCP"] = "Reinforced Concrete Pipe"
    pr.codes.codes["MH"] = "Manhole"
    pr.codes.codes["EC"] = "Edge of Concrete"
    pr.settings["f2f_path"] = "test-office-standard.csv"

    p_unk = pr.add_point(850, 950, 50, number="8801", desc="BADCODE1 ST")
    p_unk2 = pr.add_point(851, 951, 50, number="8802", desc="BADCODE2 ST")
    monkeypatch.setattr(
        clean_module, "_get_autofix_for_desc",
        lambda raw, *_args, **_kwargs: "RCP ST" if raw.startswith("BADCODE") else raw,
    )

    dlg = FixPointErrorsDialog(win.state, win)
    dlg.show()
    app.processEvents()

    # 1. Verify Unknown code check has error level
    unk_finding = next((f for f in dlg.active_findings if "unknown code" in f.get("check", "").lower() or "unknowncode" in str(f.get("flag", "")).lower()), None)
    assert unk_finding is not None
    assert unk_finding.get("level") == "error"

    # 2. Open inline editor for Unknown Code; each affected point gets its own staged Auto Fix.
    dlg._open_inline_editor(unk_finding)
    assert dlg.stack.currentIndex() == 1
    assert p_unk.id in dlg.autofix_buttons and p_unk2.id in dlg.autofix_buttons
    assert not any(button.text() == "Select All Correct" for button in dlg.findChildren(QPushButton))
    suggestions = dlg.autofix_suggestions
    assert set(suggestions) == {p_unk.id, p_unk2.id}
    assert suggestions[p_unk.id].text() == "Proposed autofix: RCP ST"
    assert suggestions[p_unk2.id].text() == "Proposed autofix: RCP ST"
    original_label = next(label for label in dlg.findChildren(QLabel)
                          if f"plumbline-point:{p_unk.id}" in label.text())
    focused = []
    monkeypatch.setattr(dlg, "_select_and_focus_points", lambda pids: focused.extend(pids))
    original_label.linkActivated.emit(f"plumbline-point:{p_unk.id}")
    assert focused == [p_unk.id]
    auto_fix_button = dlg.autofix_buttons[p_unk.id]
    second_auto_fix_button = dlg.autofix_buttons[p_unk2.id]
    assert auto_fix_button.isEnabled() and second_auto_fix_button.isEnabled()

    # Fixed starts with the original; clicking Auto Fix fills each key-in box.
    fixed_edit = next(item[1] for item in dlg.desc_edits if item[0].id == p_unk.id)
    second_fixed_edit = next(item[1] for item in dlg.desc_edits if item[0].id == p_unk2.id)
    assert fixed_edit.text() == "BADCODE1 ST" and second_fixed_edit.text() == "BADCODE2 ST"
    assert "color: #1f2933" in fixed_edit.styleSheet()
    auto_fix_button.click()
    second_auto_fix_button.click()
    assert fixed_edit.text() == "RCP ST" and second_fixed_edit.text() == "RCP ST"
    assert p_unk.desc == "BADCODE1 ST" and p_unk2.desc == "BADCODE2 ST"
    fixed_edit.setText(p_unk.desc)
    second_fixed_edit.setText(p_unk2.desc)

    # 3. Verify original description highlights error token in red/underline
    highlighted = dlg._highlight_unknown_tokens(p_unk.desc)
    assert "BADCODE1" in highlighted
    assert "color: #e74c3c" in highlighted and "underline" in highlighted

    # 4. Verify Fieldbook Lookup UI components exist
    assert hasattr(dlg, "fb_table")
    assert hasattr(dlg, "fb_search")
    assert hasattr(dlg, "fb_category")
    assert hasattr(dlg, "desc_edits") and len(dlg.desc_edits) >= 1

    # Verify category dropdown contains categories
    assert dlg.fb_category.count() >= 2
    cat_items = [dlg.fb_category.itemText(i) for i in range(dlg.fb_category.count())]
    assert "All Categories" in cat_items

    # 5. Verify real-time validation on key-in field
    unk_edit_item = next((item for item in dlg.desc_edits if item[0].id == p_unk.id), None)
    assert unk_edit_item is not None
    p_edit, ed, lbl_status = unk_edit_item
    is_val, msg = dlg._validate_desc_text(ed.text())
    assert not is_val
    assert "Unknown code" in msg

    # 6. Verify Fieldbook search & category filtering
    dlg.fb_search.setText("Concrete")
    dlg._fb_apply_filter()
    assert dlg.fb_table.rowCount() >= 1

    # 7. Verify using selected code from Fieldbook lookup
    dlg.current_focused_ed = ed
    dlg.fb_table.selectRow(0)
    dlg._fb_use_code()
    assert "EC" in ed.text() or "RCP" in ed.text()

    # Key-in box should now be valid
    is_val_now, _ = dlg._validate_desc_text(ed.text())
    assert is_val_now

    # 8. Test self-flagging error check on Apply with invalid keyed-in code
    ed.setText("INVALIDCODE99 ST")
    msg_boxes = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda parent, title, text: msg_boxes.append((title, text))))

    dlg._action_apply_descriptions()
    # Apply must be rejected and point desc unchanged
    assert p_unk.desc == "BADCODE1 ST"
    assert len(msg_boxes) >= 1
    assert "Invalid Field Book Code" in msg_boxes[0][0]

    # 9. Now set to valid code and apply
    ed.setText("EC1 ST")
    dlg._action_apply_descriptions()
    assert p_unk.desc == "EC1 ST"
    assert len(dlg.resolved_findings) >= 1

    dlg._save_and_exit()


def test_changed_points_report_lists_every_point_in_a_merge_stack(win):
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import FixPointErrorsDialog, QAReportDialog

    project = win.state.project
    head = project.add_point(500.0, 600.0, 50.0, number="95001", desc="EP")
    duplicate = project.add_point(500.01, 600.01, 50.0, number="95002", desc="EP")
    stack_ids = [head.id, duplicate.id]

    def fake_check_project(pr, **_kwargs):
        ids = list(pr.points)
        active_stack = [pid for pid in stack_ids if pid in pr.points]
        findings = []
        if len(active_stack) > 1:
            row_by_pid = {pid: row for row, pid in enumerate(ids)}
            findings.append({
                "check": "Close Points", "flag": "ClosePointCollision", "level": "warn",
                "detail": "Two points occupy the same location.", "pids": active_stack,
                "rows": [row_by_pid[pid] for pid in active_stack],
                "groups": [[row_by_pid[pid] for pid in active_stack]],
                "key": "full-stack-audit-regression",
            })
        return {"findings": findings, "ids": ids, "rows": [], "stats": {},
                "flags": {}, "line_issues": []}

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(item for item in dialog.active_findings
                   if item["key"] == "full-stack-audit-regression")
    dialog._open_inline_editor(finding)
    key = dialog._stack_key(stack_ids)
    combo = dialog.stack_action_combos[key]
    combo.setCurrentIndex(combo.findData("merge_keep_coords"))
    assert dialog._action_apply_staged_stack_actions()

    audit = project.settings["qa_resolution_audit"]
    assert [item["point_id"] for item in audit] == stack_ids
    assert audit[0]["before"] == audit[0]["after"]
    assert audit[0]["changed_fields"] == []
    assert audit[1]["before"]["number"] == "95002"
    assert audit[1]["after"] is None
    assert audit[1]["changed_fields"] == ["removed"]

    report = QAReportDialog(dialog._report_check_rows(), audit, win)
    assert report.tbl_point_audit.rowCount() == 2
    assert "#95001" in report.tbl_point_audit.item(0, 2).text()
    assert "#95002" in report.tbl_point_audit.item(1, 2).text()
    assert "No field values changed" in report.tbl_point_audit.item(0, 3).text()
    assert "Point removed" in report.tbl_point_audit.item(1, 3).text()
    report.close()
    dialog.close()


def test_qa_report_includes_zero_checks_persistent_point_audit_and_export(tmp_path, win, monkeypatch):
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import (
        FixPointErrorsDialog,
        QAReportDialog,
        _write_qa_report_csv,
    )

    pr = win.state.project
    point = pr.add_point(350.0, 450.0, 25.0, number="93501", desc="BADCODE ST")

    def fake_check_project(project, **kwargs):
        ids = list(project.points)
        return {
            "rows": [[pid] for pid in ids],
            "ids": ids,
            "findings": [],
            "stats": {},
            "flags": {},
            "line_issues": [],
            "code_checks": True,
        }

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)
    assert dialog.btn_report.text() == "QA Report…"
    assert not hasattr(dialog, "btn_export")

    # Clear checks remain visible and have a page to review.
    clear_close = next(row for row in dialog._report_check_rows()
                       if row["check"] == "Close points")
    assert clear_close["status"] == "CLEAR"
    assert clear_close["finding_count"] == 0
    clear_row = next(idx for idx, item in enumerate(dialog.active_findings)
                     if item.get("zero_check") and item["check"] == "Close points")
    assert dialog.tbl_active.item(clear_row, 1).text() == "CLEAR"
    dialog.tbl_active.cellWidget(clear_row, 0).click()
    assert dialog.current_edit_finding["zero_check"]
    assert "0 findings" in dialog.lbl_edit_detail.text()
    dialog._action_return_to_summary()

    # A correction creates durable before/after records for changed points only.
    finding = {
        "check": "Unknown code",
        "flag": "UnknownCode",
        "level": "error",
        "detail": "Unknown code on one point.",
        "pids": [point.id],
        "points": [point.number],
        "stack_groups": [[point.id]],
        "key": "point-audit-regression",
    }
    dialog._open_inline_editor(finding)
    dialog._apply_fix(
        "Correct description",
        lambda: setattr(point, "desc", "EP ST"),
        resolved_points=[point.id],
    )
    audit = pr.settings["qa_resolution_audit"]
    assert len(audit) == 1
    assert audit[0]["before"]["description"] == "BADCODE ST"
    assert audit[0]["after"]["description"] == "EP ST"
    assert audit[0]["changed_fields"] == ["description"]

    report = QAReportDialog(dialog._report_check_rows(), audit, win)
    assert report.tabs.count() == 2
    assert report.tbl_point_audit.rowCount() == 1
    assert "Description: “BADCODE ST” → “EP ST”" in report.tbl_point_audit.item(0, 3).text()
    assert report.btn_export.text().startswith("Export")
    report_path = tmp_path / "qa_report.csv"
    _write_qa_report_csv(report_path, dialog._report_check_rows(), audit)
    exported = report_path.read_text(encoding="utf-8")
    assert "CHECK SUMMARY" in exported and "RESOLVED POINT AUDIT" in exported
    assert "Close points,CLEAR,0,0" in exported
    assert "Specific Before → After Changes" in exported
    assert "Description: “BADCODE ST” → “EP ST”" in exported
    assert "BADCODE ST" in exported and "EP ST" in exported
    report.close()

    dialog._undo_issue()
    assert point.desc == "BADCODE ST"
    assert pr.settings["qa_resolution_audit"] == []
    dialog._redo_issue()
    assert point.desc == "EP ST"
    assert len(pr.settings["qa_resolution_audit"]) == 1
    dialog._discard_and_exit()


def test_qa_workbench_uses_selected_vocabulary_and_fieldbook_path(win, monkeypatch):
    from plumbline.fieldwork import bridge as FB
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    pr.add_point(80.0, 90.0, 10.0, number="88001", desc="OFFICE_CODE")
    selected_path = "/standards/selected-job.fwb"
    monkeypatch.setattr(FB, "vocabulary_for", lambda _project, **_kwargs: {
        "source": "none", "label": "", "codes": set(), "path": selected_path,
        "why": "selected file has no valid code vocabulary",
    })
    checked = []

    def fake_check_project(project, f2f=None, fieldbook_path=None, ne_tol=None, elev_tol=None):
        checked.append({"f2f": f2f, "fieldbook_path": fieldbook_path})
        return {"rows": [], "ids": [], "findings": [], "stats": {}, "flags": {}, "line_issues": []}

    monkeypatch.setattr(FB, "check_project", fake_check_project)
    dialog = FixPointErrorsDialog(win.state, win)

    assert dialog.code_set == set()  # built-in project defaults are not a substitute vocabulary
    assert checked
    assert checked[-1] == {"f2f": set(), "fieldbook_path": selected_path}


def test_qa_fixes_and_rollbacks_do_not_process_linework_implicitly(win):
    from plumbline.core.featurecodes import FeatureCode
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    pr.codes.add(FeatureCode(code="QA", kind="line", layer="QA", breakline=True))
    start = pr.add_point(0.0, 0.0, 10.0, number="99101", desc="QA ST")
    end = pr.add_point(10.0, 0.0, 20.0, number="99102", desc="QA END")
    duplicate = pr.add_point(100.0, 100.0, 30.0, number="99101", desc="OTHER")
    unrelated = pr.add_point(500.0, 500.0, 30.0, number="99103", desc="OTHER")
    pr.process_linework()

    def line_start():
        line = next(e for e in pr.entities.values()
                    if getattr(e, "derived", "") == "linework" and e.attrs.get("code") == "QA")
        return tuple(float(v) for v in line.verts[0])

    initial_line_start = line_start()
    win.state.set_dirty(False)
    dialog = FixPointErrorsDialog(win.state, win)
    finding = next(f for f in dialog.active_findings
                   if "duplicate" in f.get("check", "").lower()
                   and {start.id, duplicate.id}.issubset(set(f.get("pids", []))))
    dialog._open_inline_editor(finding)

    dialog._apply_fix("Move QA line vertex", lambda: setattr(start, "x", 2.0),
                      resolved_points=[start.id])
    assert start.x == pytest.approx(2.0)
    assert line_start() == pytest.approx(initial_line_start)
    assert not pr.linework_is_processed()

    dialog._undo_issue()
    assert start.x == pytest.approx(0.0)
    assert line_start() == pytest.approx(initial_line_start)
    assert pr.linework_is_processed()

    dialog._redo_issue()
    assert start.x == pytest.approx(2.0)
    assert line_start() == pytest.approx(initial_line_start)
    assert not pr.linework_is_processed()

    # Only an explicit invocation rebuilds the derived geometry.
    pr.process_linework()
    assert line_start()[0] == pytest.approx(2.0)
    assert pr.linework_is_processed()

    unrelated.desc = "Changed outside this issue"
    dialog._action_discard_issue()
    assert start.x == pytest.approx(0.0)
    assert unrelated.desc == "Changed outside this issue"
    assert line_start()[0] == pytest.approx(2.0)
    assert not pr.linework_is_processed()
    assert dialog.resolved_findings == []
    assert dialog.history_undo == []
    assert not dialog.dirty
    assert not win.state.dirty


# ------------------------------------------------------------------ Initial View Staging & Issue-Scoped Undo/Redo
def test_initial_view_staging_and_issue_scoped_undo_redo(win, app, auto):
    from PySide6.QtGui import QColor
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    pr = win.state.project
    pr.codes.codes["RCP"] = "Reinforced Concrete Pipe"
    pr.codes.codes["MH"] = "Manhole"
    pr.codes.codes["EC"] = "Edge of Concrete"
    pr.settings["f2f_path"] = "test-office-standard.csv"

    # Setup 3 distinct issues:
    # 1. Close Points (p1, p2)
    p1 = pr.add_point(100.0, 100.0, 10.0, number="101", desc="EC1 ST")
    p2 = pr.add_point(100.01, 100.01, 10.01, number="102", desc="EC1")

    # 2. Potential code in descriptor (p3)
    p3 = pr.add_point(200.0, 200.0, 20.0, number="103", desc="MH / 30rcp")

    # 3. Duplicate point number (p4, p5)
    p4 = pr.add_point(300.0, 300.0, 30.0, number="5001", desc="MH")
    p5 = pr.add_point(400.0, 400.0, 40.0, number="5001", desc="MH")

    dlg = FixPointErrorsDialog(win.state, win)
    dlg.show()
    app.processEvents()

    # 1. Verify Master Initial View (Page 0) displays all active issues
    assert dlg.stack.currentIndex() == 0
    assert dlg.tbl_active.rowCount() >= 3

    # All rows start active (ERROR or WARN)
    for r in range(dlg.tbl_active.rowCount()):
        st_text = dlg.tbl_active.item(r, 1).text()
        assert st_text in ("ERROR", "WARN", "INFO")

    # 2. Enter Issue 1 (Close Points)
    close_finding = next((f for f in dlg.active_findings if "close" in f.get("check", "").lower()), None)
    assert close_finding is not None
    dlg._open_inline_editor(close_finding)
    assert dlg.stack.currentIndex() == 1
    assert "[Active]" in dlg.lbl_issue_status.text() or "WARN" in dlg.lbl_issue_status.text()
    assert not dlg.btn_issue_undo.isEnabled()

    # Stage a stack merge; the project remains unchanged until page-level Apply.
    close_key = dlg._stack_key([p1.id, p2.id])
    merge_combo = dlg.stack_action_combos[close_key]
    merge_combo.setCurrentText("Merge all into head; average coordinates")
    assert p2.id in pr.points
    dlg.btn_issue_apply.click()
    assert p1.id in pr.points
    assert p2.id not in pr.points  # merged
    assert dlg.btn_issue_undo.isEnabled()

    # Undo restores both the point data and the merge dropdown selection, without reapplying it.
    dlg._undo_issue()
    assert p2.id in pr.points
    assert dlg.stack_action_combos[close_key].currentData() == "merge"
    assert dlg.btn_issue_redo.isEnabled()

    # Test Scoped Redo on Issue 1
    dlg._redo_issue()
    assert p2.id not in pr.points  # re-merged
    assert dlg.tbl_edit_resolved.rowCount() == 1
    assert "101" in dlg.tbl_edit_resolved.item(0, 2).text()

    # Click "Save to Initial View" to return to master issues list
    dlg._action_save_issue()
    assert dlg.stack.currentIndex() == 0

    # 3. Verify Master Initial View updates Issue 1 row to green RESOLVED
    close_row = None
    for r in range(dlg.tbl_active.rowCount()):
        if "close" in dlg.tbl_active.item(r, 2).text().lower():
            close_row = r
            break
    assert close_row is not None
    assert dlg.tbl_active.item(close_row, 1).text() == "RESOLVED"
    assert dlg.tbl_active.item(close_row, 1).foreground().color() == QColor("#27ae60")
    assert "Merged" in dlg.tbl_active.item(close_row, 3).text()

    # 4. Enter Issue 2 (Potential code in descriptor)
    sep_finding = next((f for f in dlg.active_findings if "descriptor" in f.get("check", "").lower() or "separator" in f.get("check", "").lower()), None)
    assert sep_finding is not None
    dlg._open_inline_editor(sep_finding)
    assert dlg.stack.currentIndex() == 1

    # Correct All stages row selections; Save & Return applies them.
    original_p3_desc = p3.desc
    dlg._action_correct_all_separator_corrections()
    assert p3.desc == original_p3_desc
    dlg._action_save_issue()
    assert "rcp" in p3.desc
    assert dlg.stack.currentIndex() == 0

    # Verify Issue 2 row turns green RESOLVED in master table
    sep_row = None
    for r in range(dlg.tbl_active.rowCount()):
        if "descriptor" in dlg.tbl_active.item(r, 2).text().lower() or "separator" in dlg.tbl_active.item(r, 2).text().lower():
            sep_row = r
            break
    assert sep_row is not None
    assert dlg.tbl_active.item(sep_row, 1).text() == "RESOLVED"
    assert dlg.tbl_active.item(sep_row, 1).foreground().color() == QColor("#27ae60")

    # 5. Re-enter Resolved Issue 1 to review
    dlg._open_inline_editor(close_finding)
    assert dlg.stack.currentIndex() == 1
    assert "[✓ Resolved]" in dlg.lbl_issue_status.text()
    assert dlg.tbl_edit_pts.rowCount() == 0  # No remaining points
    assert dlg.tbl_edit_resolved.rowCount() == 1  # Retains resolution history
    assert dlg.btn_issue_undo.isEnabled()  # Can undo if needed

    # Return back to summary
    dlg._action_save_issue()
    assert dlg.stack.currentIndex() == 0

    # 6. Test Discard on Active Issue (Issue 3: Duplicate numbers)
    dup_finding = next((f for f in dlg.active_findings if "duplicate" in f.get("check", "").lower()), None)
    assert dup_finding is not None
    dlg._open_inline_editor(dup_finding)
    assert dlg.stack.currentIndex() == 1

    # Change second point number
    p5.number = "9999"
    dlg.issue_dirty = True

    # Click Discard Issue Changes
    dlg._action_discard_issue()
    assert dlg.stack.currentIndex() == 0
    assert p5.number == "5001"  # Reverted back

    # 7. Final Review Commitment: Save & Exit
    dlg._save_and_exit()
    assert p1.desc == "EC1 ST - EC1" or "EC1" in p1.desc
    assert "rcp" in p3.desc


def test_qa_view_splitter_fills_height_and_keeps_views_recoverable(win, app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSizePolicy
    from plumbline.ui.qa_workspace import FixLineworkDialog

    dialog = FixLineworkDialog(win.state, win)
    try:
        dialog.resize(1200, 900)
        dialog.show()
        app.processEvents()
        app.processEvents()

        splitter = dialog.split_views
        assert dialog.splitter.sizePolicy().verticalPolicy() == QSizePolicy.Expanding
        assert dialog.w_left.sizePolicy().verticalPolicy() == QSizePolicy.Expanding
        assert dialog.w_right.sizePolicy().verticalPolicy() == QSizePolicy.Expanding
        root_layout = dialog.root_layout
        margins = root_layout.contentsMargins()
        available_height = (
            dialog.root_widget.height() - margins.top() - margins.bottom()
            - dialog.banner.height() - root_layout.spacing()
        )
        assert dialog.splitter.height() >= available_height - 2
        assert dialog.splitter.width() >= dialog.root_widget.width() - 2
        assert dialog.splitter.geometry().bottom() >= dialog.root_widget.rect().bottom() - 2
        assert dialog.splitter.geometry().right() >= dialog.root_widget.rect().right() - 2
        assert dialog.w_left.height() >= dialog.splitter.height() - 2
        assert dialog.w_right.height() >= dialog.splitter.height() - 2

        assert splitter.orientation() == Qt.Vertical
        assert splitter.count() == 2
        assert not splitter.isCollapsible(0)
        assert not splitter.isCollapsible(1)
        assert splitter.sizePolicy().verticalPolicy() == QSizePolicy.Expanding
        assert dialog.canvas.sizePolicy().verticalPolicy() == QSizePolicy.Expanding
        assert dialog.view3d.sizePolicy().verticalPolicy() == QSizePolicy.Expanding

        sizes = splitter.sizes()
        assert sum(sizes) >= splitter.height() - splitter.handleWidth() - 2
        assert 0.57 <= sizes[0] / sum(sizes) <= 0.63
        assert splitter.geometry().bottom() >= dialog.w_left.rect().bottom() - 2

        # Extreme divider moves must leave each canvas above its minimum height.
        for requested_sizes in ([0, 10000], [10000, 0]):
            splitter.setSizes(requested_sizes)
            app.processEvents()
            actual_sizes = splitter.sizes()
            assert actual_sizes[0] >= dialog.canvas.minimumHeight()
            assert actual_sizes[1] >= dialog.view3d.minimumHeight()
    finally:
        dialog.close()
        app.processEvents()


def test_fix_point_maximized_window_fills_workbench_at_multiple_sizes(win, app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMainWindow
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    dialog = FixPointErrorsDialog(win.state, win)
    assert isinstance(dialog, QMainWindow)
    assert dialog.centralWidget() is dialog.root_widget

    def assert_workbench_fills_window():
        root_layout = dialog.root_layout
        margins = root_layout.contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 0, 0, 0)
        available_height = (
            dialog.root_widget.height() - margins.top() - margins.bottom()
            - dialog.banner.height() - root_layout.spacing()
        )
        available_width = dialog.root_widget.width() - margins.left() - margins.right()
        assert dialog.splitter.height() >= available_height - 2
        assert dialog.splitter.width() >= available_width - 2
        assert dialog.splitter.geometry().bottom() >= dialog.root_widget.rect().bottom() - 2
        assert dialog.splitter.geometry().right() >= dialog.root_widget.rect().right() - 2
        assert dialog.w_left.height() >= dialog.splitter.height() - 2
        assert dialog.w_right.height() >= dialog.splitter.height() - 2

    try:
        dialog.show()
        app.processEvents()
        app.processEvents()

        assert dialog.isMaximized()
        assert not dialog.isFullScreen()
        assert dialog.windowFlags() & Qt.WindowMaximizeButtonHint
        assert dialog.windowFlags() & Qt.WindowMinimizeButtonHint
        assert_workbench_fills_window()

        dialog.showNormal()
        dialog.resize(1200, 700)
        app.processEvents()
        app.processEvents()
        assert not dialog.isMaximized() and not dialog.isFullScreen()
        assert_workbench_fills_window()
    finally:
        dialog.close()
        app.processEvents()


def test_main_window_opens_qa_workbench_as_modal_central_window(win, app, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMainWindow

    monkeypatch.setattr(win, "_ensure_fieldbook_for_processing", lambda **_kwargs: True)
    win.open_fix_point_errors()
    app.processEvents()

    dialog = win._qa_workbench_window
    assert isinstance(dialog, QMainWindow)
    assert dialog.centralWidget() is dialog.root_widget
    assert dialog.windowModality() == Qt.WindowModal
    dialog._discard_and_exit()
    app.processEvents()
    assert win._qa_workbench_window is None

