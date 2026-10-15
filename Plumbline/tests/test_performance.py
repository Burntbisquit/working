from __future__ import annotations

import json

from plumbline.core.performance import benchmark_project, run_project_benchmark
from plumbline.core.project import Project


def _project():
    project = Project("Potato comparison")
    points = [
        project.add_point(0, 0, 10, number="1", desc="EP1 ST"),
        project.add_point(10, 0, 11, number="2", desc="EP1 END"),
    ]
    project.add_polyline([[point.x, point.y, point.z] for point in points], "ROAD",
                         attrs={"code": "EP", "string": "1", "point_ids": [point.id for point in points]},
                         derived="linework")
    return project


def test_quick_benchmark_is_non_mutating_and_has_drilldown_rows():
    project = _project()
    before = project.snapshot()
    report = benchmark_project(project, "Quick")

    assert project.snapshot() == before
    assert report.profile == "Quick"
    assert [metric.operation for metric in report.measurements] == [
        "Project extents", "Linework input fingerprint", "Geometry segment inventory", "Spatial index build (cold)",
    ]
    assert all(metric.mean_ms >= 0 for metric in report.measurements)
    assert "Plumbline Performance Report" in report.to_markdown()


def test_benchmark_writes_markdown_and_json_reports(tmp_path):
    report, markdown_path, json_path = run_project_benchmark(_project(), "Quick", tmp_path)

    assert markdown_path.exists() and json_path.exists()
    assert markdown_path.suffix == ".md"
    assert "Project workload" in markdown_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["profile"] == report.profile == "Quick"
    assert payload["project"]["name"] == "Potato comparison"
    assert payload["measurements"][2]["operation"] == "Geometry segment inventory"
