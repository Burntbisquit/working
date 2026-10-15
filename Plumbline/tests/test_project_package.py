"""Project Save As packages copy their job tree and every referenced local asset."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from plumbline.core.model import ImageryLayer
from plumbline.core.project import Project
from plumbline.core.project_package import create_project_package


def test_save_as_creates_unique_job_package_with_referenced_assets(tmp_path):
    source_root = tmp_path / "Source Job"
    (source_root / "Field Data" / "Week 1").mkdir(parents=True)
    (source_root / "Field Book").mkdir()
    (source_root / "operator-notes.txt").write_text("keep the whole job folder", encoding="utf-8")
    raw_inside = source_root / "Field Data" / "Week 1" / "raw.csv"
    raw_inside.write_text("Point,Northing,Easting\n", encoding="utf-8")

    external = tmp_path / "external"
    external.mkdir()
    fieldbook = external / "office.fwb"
    fieldbook.write_text("field-book asset", encoding="utf-8")
    f2f = external / "codes.csv"
    f2f.write_text("F2F asset", encoding="utf-8")
    data_folder = external / "downloads"
    data_folder.mkdir()
    raw_external = data_folder / "collector.csv"
    raw_external.write_text("download", encoding="utf-8")
    image = external / "aerial.tif"
    image.write_bytes(b"image payload")
    world_file = external / "aerial.tfw"
    world_file.write_text("1\n0\n0\n-1\n0\n0\n", encoding="utf-8")

    project = Project("Source Job")
    project.settings.update({
        "fieldbook_file": str(fieldbook),
        "f2f_path": str(f2f),
        "data_folder": str(data_folder),
    })
    point = project.add_point(
        1, 2, 3, number="1", desc="TOC",
        attrs={"import": {"folder": str(data_folder)},
               "fieldwork_folder": str(data_folder),
               "fieldwork_source": str(raw_external)},
    )
    image_id = project.new_id()
    project.imagery[image_id] = ImageryLayer(
        image_id, "Aerial", "file", {"path": str(image)}, True, 1.0, (0.0, 0.0))
    source_project_file = source_root / "Source Job.plb"
    project.save(source_project_file)

    package = create_project_package(project, tmp_path / "output", "Survey Copy")
    root = Path(package.path).parent

    assert root == tmp_path / "output" / "Survey Copy"
    assert (root / "operator-notes.txt").read_text(encoding="utf-8") == "keep the whole job folder"
    assert (root / "Field Data" / "Week 1" / "raw.csv").is_file()
    assert Path(package.settings["fieldbook_file"]).is_file()
    assert Path(package.settings["f2f_path"]).is_file()
    assert Path(package.settings["data_folder"], "collector.csv").is_file()

    imagery = next(layer for layer in package.imagery.values() if layer.name == "Aerial")
    packaged_image = Path(imagery.source["path"])
    assert packaged_image.is_file() and packaged_image != image
    assert packaged_image.parent == root / "Imagery"
    assert packaged_image.with_suffix(".tfw").is_file()

    packaged_point = package.points[point.id]
    assert Path(packaged_point.attrs["import"]["folder"]).is_absolute() is False
    assert (root / packaged_point.attrs["import"]["folder"]).is_dir()
    assert (root / packaged_point.attrs["fieldwork_folder"]).is_dir()
    assert (root / packaged_point.attrs["fieldwork_source"]).is_file()
    assert Path(package.path).is_file()
    assert source_project_file.is_file()  # Save As leaves the original package untouched.

    # The saved .plb stores internal assets relative to its own folder, and loading
    # resolves those paths for the running app. Moving the folder must keep them live.
    saved_data = json.loads(Path(package.path).read_text(encoding="utf-8"))
    assert saved_data["package"]["relative_asset_paths"] is True
    assert not Path(saved_data["settings"]["fieldbook_file"]).is_absolute()
    reloaded = Project.load(package.path)
    assert Path(reloaded.settings["fieldbook_file"]).is_file()
    assert Path(next(iter(reloaded.imagery.values())).source["path"]).is_file()

    moved_parent = tmp_path / "relocated"
    moved_parent.mkdir()
    moved_root = moved_parent / root.name
    shutil.move(str(root), moved_root)
    moved_project = Project.load(moved_root / Path(package.path).name)
    assert Path(moved_project.settings["fieldbook_file"]).is_file()
    assert Path(moved_project.settings["f2f_path"]).is_file()
    assert (Path(moved_project.settings["data_folder"]) / "collector.csv").is_file()
    moved_image = Path(next(iter(moved_project.imagery.values())).source["path"])
    assert moved_image.is_file() and moved_image.parent == moved_root / "Imagery"
    moved_project.save()
    assert not Path(json.loads((moved_root / Path(package.path).name).read_text(
        encoding="utf-8"))["settings"]["fieldbook_file"]).is_absolute()


def test_save_as_refuses_to_merge_with_existing_project_folder(tmp_path):
    destination = tmp_path / "Already There"
    destination.mkdir()
    project = Project("Untitled")

    with pytest.raises(FileExistsError, match="already exists"):
        create_project_package(project, tmp_path, destination.name)

    assert list(destination.iterdir()) == []
