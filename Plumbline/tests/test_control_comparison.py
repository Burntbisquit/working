from __future__ import annotations

from plumbline.core.project import Project
from plumbline.core.settings import ALWAYS_SAVED, DEFAULTS, settings
from plumbline.fieldwork.bridge import check_project


def test_control_tolerance_is_a_portable_settings_value():
    assert DEFAULTS["control_point_tolerance"] == 0.01
    assert "control_point_tolerance" in ALWAYS_SAVED


def test_imported_control_reference_points_are_used_when_no_control_csv_exists(monkeypatch):
    from plumbline.core.reference import mark

    project = Project("Reference control")
    control = project.add_point(500.0, 1000.0, 20.0, number="1001", desc="CONTROL")
    mark(control, "control")
    survey = project.add_point(500.025, 1000.0, 20.0, number="1001", desc="TOPO")
    monkeypatch.setitem(settings()._data, "control_point_tolerance", 0.01)

    report = check_project(project)
    finding = next(item for item in report["findings"] if item.get("flag") == "ControlMismatch")
    assert report["control_comparison"]["source"] == "CONTROL reference points"
    assert [report["ids"][row] for row in finding["rows"]] == [survey.id]


def test_control_csv_mismatches_warn_only_above_settings_tolerance(tmp_path, monkeypatch):
    job = tmp_path / "Control Job"
    control_file = job / "Control" / "Control Points.csv"
    control_file.parent.mkdir(parents=True)
    control_file.write_text(
        "Point,Northing,Easting,Elevation,Datum,Description,Note\n"
        "1001,1000.000,500.000,20.000,NAVD88,Control,\n"
        "1002,1100.000,600.000,25.000,NAVD88,Control,\n",
        encoding="utf-8",
    )

    project = Project("Control Job")
    project.path = str(job / "Control Job.plb")
    rounded = project.add_point(500.005, 1000.005, 20.005, number="1001", desc="TOPO")
    outside = project.add_point(600.025, 1100.000, 25.000, number="1002", desc="TOPO")
    monkeypatch.setitem(settings()._data, "control_point_tolerance", 0.01)

    report = check_project(project)
    finding = next(item for item in report["findings"] if item.get("flag") == "ControlMismatch")
    assert finding["level"] == "warn"
    assert report["control_comparison"]["checked"]
    assert report["control_comparison"]["compared"] == 2
    assert report["control_comparison"]["tolerance"] == 0.01
    assert [report["ids"][row] for row in finding["rows"]] == [outside.id]
    assert "treated as rounding" in finding["message"]
    assert finding["control_comparisons"][0]["point_id"] == outside.id
    assert rounded.id not in {report["ids"][row] for row in finding["rows"]}

    # The same difference is no longer a warning when the operator raises the setting.
    monkeypatch.setitem(settings()._data, "control_point_tolerance", 0.03)
    report = check_project(project)
    assert all(item.get("flag") != "ControlMismatch" for item in report["findings"])
    assert report["control_comparison"]["tolerance"] == 0.03
