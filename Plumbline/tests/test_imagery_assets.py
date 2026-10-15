"""Imported local imagery is self-contained inside its project folder."""
from plumbline.core.imagery_assets import stage_imagery_asset


def test_stage_imagery_asset_copies_sidecars_and_avoids_overwriting(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    image = source_root / "aerial.tif"
    image.write_bytes(b"georeferenced image")
    sidecars = {
        "aerial.tfw": b"world file",
        "aerial.prj": b"projection",
        "aerial.tif.aux.xml": b"auxiliary metadata",
        "aerial.tif.ovr": b"overview",
    }
    for name, content in sidecars.items():
        (source_root / name).write_bytes(content)

    project_root = tmp_path / "Job"
    first = stage_imagery_asset(image, project_root)
    assert first.parent == project_root / "Imagery"
    assert first.read_bytes() == image.read_bytes()
    assert {path.name for path in first.parent.iterdir()} == {
        "aerial.tif", "aerial.tfw", "aerial.prj", "aerial.tif.aux.xml", "aerial.tif.ovr"
    }
    for name, content in sidecars.items():
        assert (first.parent / name).read_bytes() == content

    second = stage_imagery_asset(image, project_root)
    assert second.name == "aerial (2).tif"
    assert second.with_name(second.stem + ".tfw").is_file()
    assert second.with_name(second.name + ".aux.xml").is_file()
    assert stage_imagery_asset(first, project_root) == first  # already staged; no duplicate copy
