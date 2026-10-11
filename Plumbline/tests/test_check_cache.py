from __future__ import annotations

from plumbline.core.project import Project
from plumbline.core.settings import settings
from plumbline.fieldwork import bridge as FB


def _project_with_point():
    project = Project("Cache test")
    point = project.add_point(500.0, 1000.0, 20.0, number="1001", desc="TOPO")
    return project, point


def _fake_check(calls):
    def check(project, f2f=None, fieldbook_path=None, ne_tol=None, elev_tol=None,
              control_file=None, *, _prepared_rows=None):
        calls.append((f2f, fieldbook_path, ne_tol, elev_tol, control_file))
        rows, ids = _prepared_rows or FB.working_rows_from_project(project)
        return {"rows": rows, "ids": ids,
                "findings": [{"level": "error", "rows": []}, {"level": "warn", "rows": []}],
                "stats": {}}
    return check


def test_cached_check_reuses_only_identical_inputs_and_isolates_mutable_findings(monkeypatch):
    project, point = _project_with_point()
    calls = []
    FB.clear_check_cache(project)
    monkeypatch.setattr(FB, "check_project", _fake_check(calls))

    first, status, signature = FB.check_project_cached(project)
    assert status == "miss"
    first["findings"][0]["rows"].append(99)  # UI decoration must not alter the cached raw result.

    second, status, second_signature = FB.check_project_cached(project)
    assert status == "hit"
    assert second_signature == signature
    assert second["findings"][0]["rows"] == []
    assert len(calls) == 1

    point.desc = "TOPO UPDATED"
    updated, status, updated_signature = FB.check_project_cached(project)
    assert status == "updated"
    assert updated_signature != signature
    assert updated["rows"][0][FB.DESC] == "TOPO UPDATED"
    assert len(calls) == 2

    # An explicit rerun bypasses the exact-match cache and replaces it with a fresh result.
    _, status, same_signature = FB.check_project_cached(project, force=True)
    assert status == "rerun"
    assert same_signature == updated_signature
    _, status, _ = FB.check_project_cached(project)
    assert status == "hit"
    assert len(calls) == 3


def test_external_qa_file_content_changes_invalidate_cache_even_at_same_size(tmp_path, monkeypatch):
    project, _point = _project_with_point()
    control = tmp_path / "control.csv"
    control.write_text("1,2\n", encoding="utf-8")
    calls = []
    FB.clear_check_cache(project)
    monkeypatch.setattr(FB, "check_project", _fake_check(calls))

    _, status, signature = FB.check_project_cached(project, control_file=control)
    assert status == "miss"
    control.write_text("3,2\n", encoding="utf-8")  # Same path and byte length; content differs.

    _, status, new_signature = FB.check_project_cached(project, control_file=control)
    assert status == "updated"
    assert new_signature != signature
    assert len(calls) == 2


def test_field_book_contents_are_part_of_the_cache_signature(tmp_path, monkeypatch):
    project, _point = _project_with_point()
    fieldbook = tmp_path / "Job.fwb"
    fieldbook.write_text("CODE,TOPO\n", encoding="utf-8")
    calls = []
    FB.clear_check_cache(project)
    monkeypatch.setattr(FB, "check_project", _fake_check(calls))

    _, status, signature = FB.check_project_cached(project, f2f={"TOPO"}, fieldbook_path=fieldbook)
    assert status == "miss"
    fieldbook.write_text("CODE,EDGE\n", encoding="utf-8")

    _, status, new_signature = FB.check_project_cached(
        project, f2f={"TOPO"}, fieldbook_path=fieldbook)
    assert status == "updated"
    assert new_signature != signature
    assert len(calls) == 2


def test_code_vocabulary_is_part_of_the_cache_signature(monkeypatch):
    project, _point = _project_with_point()
    calls = []
    FB.clear_check_cache(project)
    monkeypatch.setattr(FB, "check_project", _fake_check(calls))

    _, status, signature = FB.check_project_cached(project, f2f={"TOPO"})
    assert status == "miss"
    _, status, new_signature = FB.check_project_cached(project, f2f={"EDGE"})
    assert status == "updated"
    assert new_signature != signature
    assert len(calls) == 2


def test_settings_and_close_point_tolerances_invalidate_the_cached_check(monkeypatch):
    project, _point = _project_with_point()
    calls = []
    FB.clear_check_cache(project)
    monkeypatch.setattr(FB, "check_project", _fake_check(calls))
    monkeypatch.setitem(settings()._data, "control_point_tolerance", 0.01)

    _, status, _ = FB.check_project_cached(project, ne_tol=0.05)
    assert status == "miss"
    monkeypatch.setitem(settings()._data, "control_point_tolerance", 0.02)
    _, status, _ = FB.check_project_cached(project, ne_tol=0.05)
    assert status == "updated"
    _, status, _ = FB.check_project_cached(project, ne_tol=0.06)
    assert status == "updated"
    monkeypatch.setitem(settings()._data, "space_around_description_separator", False)
    _, status, _ = FB.check_project_cached(project, ne_tol=0.06)
    assert status == "updated"
    assert len(calls) == 4


def test_stored_report_keeps_its_input_signature_without_invalidating_itself():
    project, _point = _project_with_point()
    rows, ids = FB.working_rows_from_project(project)
    signature = FB.check_input_signature(project, _prepared_rows=(rows, ids))
    report = FB.store_report(
        project,
        {"rows": rows, "ids": ids, "findings": [], "stats": {}},
        input_signature=signature,
    )

    assert report["input_signature"] == signature
    assert FB.check_input_signature(project, _prepared_rows=(rows, ids)) == signature
