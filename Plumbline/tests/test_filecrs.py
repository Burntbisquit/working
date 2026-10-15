"""Per-file coordinate corrections reproject into the project's current system."""
from __future__ import annotations

import numpy as np
import pytest
from pyproj import Transformer

from plumbline.core import filecrs as FCRC
from plumbline.core import provenance as PROV
from plumbline.core.crs import GroundScale, ProjectCRS
from plumbline.core.project import Project


def test_reproject_uses_corrected_source_project_target_and_both_safs():
    source = ProjectCRS.from_epsg(
        32614, ground=GroundScale(enabled=True, base_x=500_000.0, base_y=0.0, saf=1.00015))
    project_crs = ProjectCRS.from_epsg(
        32615, ground=GroundScale(enabled=True, base_x=200_000.0, base_y=0.0, saf=1.00008))
    project = Project("File reprojection", project_crs)

    # Raw file coordinates are in the wrong/uncorrected zone until Reproject is used.
    to_source = Transformer.from_crs(4326, source.crs, always_xy=True)
    x, y = to_source.transform(-96.1, 32.8)
    point = project.add_point(x, y, 123.0, number="101", desc="CP")
    PROV.stamp([point], file="control.csv", set="Control points")
    other = project.add_point(555_000.0, 3_600_000.0, 90.0, number="102", desc="GS")
    PROV.stamp([other], file="other.csv", set="Other points")

    # The row may currently say "project"; the user assigns the corrected source system here.
    FCRC.record(project, "control.csv", FCRC.make_from_project(project, method="project"))
    corrected_source = FCRC.record_from_crs(source, method="edited")

    source_grid_x, source_grid_y = source.to_grid(np.array([x]), np.array([y]))
    transform = Transformer.from_crs(source.crs, project_crs.crs, always_xy=True)
    target_grid_x, target_grid_y = transform.transform(source_grid_x, source_grid_y)
    expected_x, expected_y = project_crs.from_grid(target_grid_x, target_grid_y)
    before_other = (other.x, other.y, other.z)

    result = FCRC.reproject(project, "control.csv", corrected_source)

    assert (point.x, point.y) == pytest.approx((float(expected_x[0]), float(expected_y[0])), abs=1e-4)
    assert point.z == 123.0
    assert (other.x, other.y, other.z) == before_other  # only this file's points move
    assert result["points"] == 1
    assert result["from"] == source.label
    assert result["to"] == project_crs.label
    current = FCRC.for_file(project, "control.csv")
    assert current["method"] == "reprojected"
    assert current["ground"] is True
    assert current["saf"] == project_crs.ground.saf
    assert current["source_crs"]["key"] == corrected_source["key"]


def test_set_to_project_changes_only_the_file_coordinate_record():
    project = Project("Assign file system", ProjectCRS.from_epsg(32615))
    point = project.add_point(500_000.0, 3_600_000.0, 10.0, number="1")
    PROV.stamp([point], file="survey.csv")
    old_xy = (point.x, point.y)
    FCRC.record(project, "survey.csv", FCRC.make(key="EPSG:32614", label="Wrong zone"))

    record = FCRC.set_to_project(project, "survey.csv")

    assert (point.x, point.y) == old_xy
    assert record["method"] == "project"
    assert record["key"] == project.crs.authority
