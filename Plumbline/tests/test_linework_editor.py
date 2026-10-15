"""Regression coverage for staged geometry editing and preview-first line joins."""
from __future__ import annotations

import contextlib

import numpy as np
import pytest

from plumbline.core.featurecodes import default_codes
from plumbline.core.linework_editor import (
    JoinLinesDraft,
    LineEditDraft,
    LineworkEditError,
    bulge_point,
    commit_join_draft,
    commit_line_draft,
    resolve_vertex_point_ids,
    split_bulge,
)
from plumbline.core.model import Polyline
from plumbline.core.project import Project


class _State:
    """Small transaction-compatible state fake for core-only tests."""

    def __init__(self, project):
        self.project = project
        self.edits = []
        self.selection = None

    @contextlib.contextmanager
    def edit(self, label="Edit", kinds=("all",)):
        before = self.project.snapshot()
        try:
            yield self.project
        except BaseException:
            self.project.restore(before)
            raise
        self.edits.append((label, before))
        self.project.touch()

    def select(self, points=(), entities=(), mode="replace"):
        self.selection = (set(points), set(entities), mode)


def _coded_project():
    project = Project("Line editor")
    project.codes = default_codes()
    points = [
        project.add_point(0, 0, 10, number="1", desc="EP1 ST"),
        project.add_point(10, 0, 11, number="2", desc="EP1"),
        project.add_point(20, 0, 12, number="3", desc="EP1 END"),
    ]
    line = project.add_polyline(
        [[p.x, p.y, p.z] for p in points], "SITE-EDGE",
        attrs={"code": "EP", "string": "1", "points": [p.number for p in points],
               "point_ids": [p.id for p in points]},
        derived="linework")
    return project, points, line


def test_bulge_split_preserves_arc_and_draft_is_staged_with_local_undo():
    p0, p1 = np.array([0.0, 0.0, 0.0]), np.array([10.0, 0.0, 10.0])
    bulge = 1.0
    midpoint = bulge_point(p0, p1, bulge, 0.5)
    left, right = split_bulge(bulge, 0.5)
    assert midpoint[:2] == pytest.approx([5.0, -5.0])
    assert midpoint[2] == pytest.approx(5.0)
    assert left == pytest.approx(right)
    assert bulge_point(p0, midpoint, left, 0.5) == pytest.approx(bulge_point(p0, p1, bulge, 0.25))

    project, points, line = _coded_project()
    draft = LineEditDraft(project, line)
    assert draft.point_mode_ready
    draft.set_mode("points")
    inserted = draft.insert_vertex(0, 0.5)
    assert inserted.number == "4"
    assert len(draft.vertices) == 4
    draft.move_vertex(1, 5, 2, 10.5)
    assert draft.vertices[1].x == pytest.approx(5)
    assert draft.undo_last() == "Move vertex"
    assert draft.vertices[1].y == pytest.approx(0)
    assert draft.undo_last() == "Insert vertex"
    assert len(draft.vertices) == 3
    assert np.array_equal(line.verts, np.array([[0, 0, 10], [10, 0, 11], [20, 0, 12]], dtype=float))
    assert [p.x for p in points] == [0, 10, 20]


def test_point_edit_apply_keeps_linked_point_geometry_and_linework_override_consistent():
    project, points, line = _coded_project()
    state = _State(project)
    draft = LineEditDraft(project, line)
    draft.set_mode("points")
    draft.insert_vertex(0)
    draft.move_vertex(1, 5.0, 2.0, 10.5)

    # The live project remains untouched until explicit Apply.
    assert len(project.points) == 3
    assert points[1].x == pytest.approx(10.0)
    commit_line_draft(state, draft)

    edited = project.entities[line.id]
    assert len(project.points) == 4
    inserted = project.points[edited.attrs["point_ids"][1]]
    assert (inserted.x, inserted.y, inserted.z) == pytest.approx((5.0, 2.0, 10.5))
    assert np.allclose(edited.verts[1], [5.0, 2.0, 10.5])
    assert edited.derived == "manual-linework"
    assert edited.attrs["manual_linework_override"]
    assert edited.attrs["linework_override_keys"] == [["EP", "1"]]

    # Reprocessing coded points must not create a duplicate over the accepted manual line.
    result = project.process_linework()
    assert result["strings"] == 0
    assert [e.id for e in project.polylines() if e.derived == "manual-linework"] == [line.id]


def test_existing_vertex_can_be_interpolated_and_nudged_with_local_undo():
    project, _points, line = _coded_project()
    draft = LineEditDraft(project, line)
    draft.set_mode("points")

    draft.interpolate_vertex(1, 0.25)
    assert draft.vertices[1].xyz() == pytest.approx([5.0, 0.0, 10.5])
    draft.nudge_vertex(1, 0.25, -0.5)
    assert draft.vertices[1].xyz() == pytest.approx([5.25, -0.5, 10.5])
    assert draft.undo_last() == "Nudge point"
    assert draft.vertices[1].xyz() == pytest.approx([5.0, 0.0, 10.5])
    assert draft.undo_last() == "Interpolate vertex position"
    assert draft.vertices[1].xyz() == pytest.approx([10.0, 0.0, 11.0])

    with pytest.raises(LineworkEditError, match="endpoint"):
        draft.interpolate_vertex(0, 0.5)


def test_manual_override_suppresses_only_the_edited_point_sequence_for_shared_code_string():
    project, _points, edited_line = _coded_project()
    other_points = [
        project.add_point(100, 0, 0, number="11", desc="EP1 ST"),
        project.add_point(110, 0, 0, number="12", desc="EP1 END"),
    ]
    other_line = project.add_polyline(
        [[p.x, p.y, p.z] for p in other_points], "SITE-EDGE",
        attrs={"code": "EP", "string": "1", "points": [p.number for p in other_points],
               "point_ids": [p.id for p in other_points]}, derived="linework")
    state = _State(project)
    draft = LineEditDraft(project, edited_line)
    draft.move_vertex(1, 9, 1)
    commit_line_draft(state, draft)

    result = project.process_linework()
    assert result["strings"] == 1
    rebuilt = [entity for entity in project.polylines() if entity.derived == "linework"]
    assert len(rebuilt) == 1
    assert rebuilt[0].attrs["point_ids"] == [p.id for p in other_points]
    assert project.entities[edited_line.id].derived == "manual-linework"
    assert other_line.id not in project.entities  # generated entities were rebuilt atomically


def test_link_resolver_uses_geometry_to_disambiguate_duplicate_point_numbers():
    project = Project("Duplicate point numbers")
    first = project.add_point(0, 0, 0, number="10")
    second = project.add_point(10, 0, 0, number="10")
    line = project.add_polyline(
        [[0, 0, 0], [10, 0, 0]], attrs={"points": ["10", "10"]})
    ids, issues = resolve_vertex_point_ids(project, line)
    assert ids == [first.id, second.id]
    assert issues == []

    ambiguous = project.add_polyline([[0, 0, 0]], attrs={"points": ["10"]})
    ids, issues = resolve_vertex_point_ids(project, ambiguous)
    assert ids == [first.id]  # exact geometry is still a safe unique match
    assert issues == []

    project.add_point(0, 0, 0, number="11")
    tied = project.add_polyline([[0, 0, 0]], attrs={})
    ids, issues = resolve_vertex_point_ids(project, tied)
    assert ids == [None]
    assert "equally close" in issues[0]

    explicitly_unlinked = project.add_polyline([[10, 0, 0]], attrs={"point_ids": [None]})
    ids, issues = resolve_vertex_point_ids(project, explicitly_unlinked)
    assert ids == [None]
    assert "explicitly unlinked" in issues[0]

    repeated_explicit_id = project.add_polyline(
        [[0, 0, 0], [10, 0, 0]], attrs={"point_ids": [first.id, first.id]})
    ids, issues = resolve_vertex_point_ids(project, repeated_explicit_id)
    assert ids == [first.id, None]  # explicit IDs are authoritative; do not remap the repeated ID
    assert "repeated point ID" in issues[0]

    malformed_links = project.add_polyline(
        [[0, 0, 0], [10, 0, 0]], attrs={"point_ids": [first.id]})
    ids, issues = resolve_vertex_point_ids(project, malformed_links)
    assert ids == [None, None]
    assert "malformed" in issues[0]


def test_generated_line_override_requires_resolved_identity_and_point_links():
    project, _points, line = _coded_project()
    line.attrs["point_ids"] = [line.attrs["point_ids"][0], None, line.attrs["point_ids"][2]]
    state = _State(project)
    draft = LineEditDraft(project, line)
    draft.move_vertex(1, 10.0, 1.0)
    with pytest.raises(LineworkEditError, match="lack an unambiguous point link"):
        commit_line_draft(state, draft)
    assert project.entities[line.id].derived == "linework"
    assert not any(entity.derived == "manual-linework" for entity in project.polylines())

    other = project.add_polyline([[100, 0, 0], [110, 0, 0]], attrs={"code": "EP", "string": "2"},
                                  derived="linework")
    join = JoinLinesDraft(project, [project.entities[line.id], other])
    with pytest.raises(LineworkEditError, match="resolve every source-vertex point link"):
        join.build_preview()


def test_empty_manual_override_point_ids_do_not_suppress_entire_code_string():
    project = Project("Empty manual override")
    project.codes = default_codes()
    points = [
        project.add_point(0, 0, 0, number="1", desc="EP1 ST"),
        project.add_point(10, 0, 0, number="2", desc="EP1 END"),
    ]
    project.add_polyline(
        [[0, 1, 0], [10, 1, 0]], "SITE-EDGE",
        attrs={"manual_linework_override": True, "linework_override_keys": [["EP", "1"]],
               "linework_override_point_ids": []}, derived="manual-linework")
    result = project.process_linework()
    assert result["strings"] == 1
    assert any(entity.attrs.get("point_ids") == [point.id for point in points]
               for entity in project.polylines() if entity.derived == "linework")


def test_exact_override_source_sequence_preserves_same_code_line_from_subset_points():
    project = Project("Scoped override sequence")
    project.codes = default_codes()
    unrelated = project.add_point(-10, 0, 0, number="1", desc="")
    start = project.add_point(0, 0, 0, number="2", desc="EP1 ST")
    end = project.add_point(10, 0, 0, number="3", desc="EP1 END")
    project.add_polyline(
        [[0, 1, 0], [10, 1, 0]], "SITE-EDGE",
        attrs={
            "manual_linework_override": True,
            "linework_override_keys": [["EP", "1"]],
            "linework_override_point_ids": [unrelated.id, start.id, end.id],
            "linework_override_scopes": [{
                "code": "EP", "string": "1",
                "point_ids": [unrelated.id, start.id, end.id],
                "source_sequences": [[unrelated.id, start.id, end.id]],
            }],
        }, derived="manual-linework")
    result = project.process_linework()
    assert result["strings"] == 1  # [start, end] is a subset, but not the edited source sequence


def test_delete_rejects_curve_loss_and_preview_join_averages_before_acceptance():
    project, points, line = _coded_project()
    line.bulges = np.array([0.0, 1.0, 0.0])
    draft = LineEditDraft(project, line)
    with pytest.raises(LineworkEditError, match="Straighten"):
        draft.delete_vertex(1)
    assert len(draft.vertices) == 3
    assert not draft.has_pending_actions

    second_points = [
        project.add_point(20.2, 0, 12, number="4", desc="EP2 ST"),
        project.add_point(30, 0, 13, number="5", desc="EP2 END"),
    ]
    second_line = project.add_polyline(
        [[p.x, p.y, p.z] for p in second_points], "SITE-EDGE",
        attrs={"code": "EP", "string": "2", "points": [p.number for p in second_points],
               "point_ids": [p.id for p in second_points]},
        derived="linework")
    join = JoinLinesDraft(project, [line, second_line])
    preview = join.build_preview(average=True, point_mode=True)
    assert preview.verts[2] == pytest.approx([20.1, 0, 12])
    assert preview.code_violations
    assert preview.point_updates[points[-1].id] == pytest.approx((20.1, 0, 12))
    assert preview.point_updates[second_points[0].id] == pytest.approx((20.1, 0, 12))
    assert preview.join_gaps == pytest.approx([0.2])


def test_editor_dialog_stages_actions_and_exposes_project_actions():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from plumbline.ui.app_state import AppState
    from plumbline.ui.linework_editor_dialog import LineEditorDialog

    app = QApplication.instance() or QApplication([])
    project, _points, line = _coded_project()
    dialog = LineEditorDialog(AppState(project), line)
    try:
        assert [dialog.btn_apply.text(), dialog.btn_save.text(),
                dialog.btn_discard.text(), dialog.btn_return.text()] == [
                    "Apply", "Save", "Discard", "Return"]
        assert [dialog.cmb_view.itemData(i) for i in range(dialog.cmb_view.count())] == [
            "plan", "3d", "image", "all_data"]
        dialog.cmb_view.setCurrentIndex(1)
        assert dialog.preview.view_mode == "3d"
        dialog.cmb_view.setCurrentIndex(2)
        assert dialog.context_canvas.opts.show_imagery
        dialog.cmb_view.setCurrentIndex(3)
        assert dialog.context_canvas.opts.show_points and dialog.context_canvas.opts.show_lines

        dialog.cmb_mode.setCurrentIndex(1)
        dialog.tbl_vertices.selectRow(1)
        dialog.btn_interpolate.click()
        assert dialog.draft.vertices[1].x == pytest.approx(10.0)  # symmetric midpoint
        dialog.btn_nudge_north.click()
        assert dialog.draft.vertices[1].y == pytest.approx(0.1)
        assert len(project.points) == 3  # preview edits do not mutate the live project
        assert dialog.btn_undo.isEnabled()
        dialog.btn_undo.click()
        assert dialog.draft.vertices[1].y == pytest.approx(0.0)
        dialog.btn_undo.click()
        assert not dialog.draft.has_pending_actions
        assert len(dialog.draft.vertices) == 3
    finally:
        dialog._allow_close = True
        dialog.reject()
        app.processEvents()


def test_existing_point_coder_keeps_quick_actions_staged_until_apply():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from plumbline.ui.linework_dialog import EditLineworkCodingDialog

    app = QApplication.instance() or QApplication([])
    project, points, line = _coded_project()
    original = {point.id: point.desc for point in points}
    state = _State(project)
    dialog = EditLineworkCodingDialog(state, line)
    try:
        dialog._reverse()
        assert {point.id: point.desc for point in points} == original
        assert {draft.id: draft.desc for draft in dialog.points} != original
        dialog.reject()
        assert {point.id: point.desc for point in points} == original
    finally:
        dialog.close()
        app.processEvents()


def test_join_dialog_requires_an_explicit_preview_before_accept():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from plumbline.ui.linework_editor_dialog import JoinLinesDialog

    app = QApplication.instance() or QApplication([])
    project, _points, first = _coded_project()
    points = [
        project.add_point(20, 0, 12, number="4", desc="EP2 ST"),
        project.add_point(30, 0, 13, number="5", desc="EP2 END"),
    ]
    second = project.add_polyline(
        [[p.x, p.y, p.z] for p in points], "SITE-EDGE",
        attrs={"code": "EP", "string": "2", "points": [p.number for p in points],
               "point_ids": [p.id for p in points]}, derived="linework")
    before = [entity.verts.copy() for entity in (first, second)]
    dialog = JoinLinesDialog(_State(project), [first, second])
    try:
        assert not dialog.btn_accept.isEnabled()
        dialog._build_preview()
        assert dialog.preview_result is not None
        assert dialog.btn_accept.isEnabled()
        assert len(project.entities) >= 2
        assert all(np.array_equal(entity.verts, vertices)
                   for entity, vertices in zip((first, second), before))
    finally:
        dialog.reject()
        app.processEvents()


def test_join_can_auto_reverse_and_condense_near_duplicate_vertices():
    project = Project("Join orientation")
    first = project.add_polyline([[0, 0, 0], [10, 0, 0]], "0")
    second = project.add_polyline([[20, 0, 0], [10.004, 0, 0]], "0")
    draft = JoinLinesDraft(project, [first, second])
    draft.auto_orient()
    assert second.id in draft.reversed_ids
    preview = draft.build_preview(average=False, condense=True, tolerance=0.01)
    assert len(preview.verts) == 3
    assert preview.verts[1, :2] == pytest.approx([10.002, 0])
    assert preview.join_gaps == pytest.approx([0.004])


def test_join_accept_is_atomic_and_point_number_normalization_has_collision_guard():
    project, _points, first = _coded_project()
    second_points = [
        project.add_point(20, 0, 12, number="4", desc="EP2 ST"),
        project.add_point(30, 0, 13, number="5", desc="EP2 END"),
    ]
    second = project.add_polyline(
        [[p.x, p.y, p.z] for p in second_points], "SITE-EDGE",
        attrs={"code": "EP", "string": "2", "points": [p.number for p in second_points],
               "point_ids": [p.id for p in second_points]},
        derived="linework")
    outside = project.add_point(999, 999, 0, number="100")
    draft = JoinLinesDraft(project, [first, second])
    conflict = draft.build_preview(point_mode=True, normalize_numbers=True, start_number=100)
    assert conflict.number_conflicts == {"100": [outside.id]}
    with pytest.raises(LineworkEditError, match="conflicts"):
        commit_join_draft(_State(project), draft, conflict, point_mode=True, normalize_numbers=True)

    preview = draft.build_preview(point_mode=True, normalize_numbers=True, start_number=200)
    state = _State(project)
    joined_id = commit_join_draft(
        state, draft, preview, replace_sources=True, point_mode=True, normalize_numbers=True)
    joined = project.entities[joined_id]
    assert joined.attrs["joined"]
    assert joined.attrs["manual_linework_override"]
    assert first.id not in project.entities and second.id not in project.entities
    assert [project.points[pid].number for pid in preview.source_point_ids] == ["200", "201", "202", "203", "204"]
    assert state.selection[1] == {joined_id}
    assert project.process_linework()["strings"] == 0
