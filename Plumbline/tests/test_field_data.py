from __future__ import annotations

from plumbline.core.field_data import imported_file_matches, make_import_record


def test_imported_field_data_matches_folder_file_and_point_count(tmp_path):
    source_root = tmp_path / "Downloads" / "batch"
    source_root.mkdir(parents=True)
    source = source_root / "crew 1.csv"
    source.write_bytes(b"original bytes are not used as identity")

    stored_root = tmp_path / "Job" / "Field Data"
    stored_root.mkdir(parents=True)
    stored = stored_root / "batch" / source.name
    stored.parent.mkdir()
    stored.write_bytes(b"different bytes, same imported point count")

    record = make_import_record(
        source_root, source, stored_root, stored, point_count=3)
    assert record == {
        "source_folder": "batch",
        "source_file": "crew 1.csv",
        "stored_folder": "field data/batch",
        "stored_file": "crew 1.csv",
        "point_count": 3,
    }
    assert imported_file_matches([record], source_root, source, 3)
    assert imported_file_matches([record], stored_root, stored, 3)

    source.write_bytes(b"updated source bytes with a different length")
    assert imported_file_matches([record], source_root, source, 3), \
        "the same path/file and point count still match regardless of byte size"

    # The same name and folder are not enough if the updated file now has a different number
    # of usable points. File byte size and contents are deliberately not identity fields.
    assert not imported_file_matches([record], source_root, source, 4)

    renamed = source_root / "crew 2.csv"
    renamed.write_bytes(source.read_bytes())
    assert not imported_file_matches([record], source_root, renamed, 3), \
        "same-count files with different names are new field data"

    other_parent = tmp_path / "Downloads" / "other-batch"
    other_parent.mkdir()
    same_name_elsewhere = other_parent / source.name
    same_name_elsewhere.write_bytes(source.read_bytes())
    assert not imported_file_matches([record], other_parent, same_name_elsewhere, 3), \
        "the selected parent folder is part of field-data identity"


def test_legacy_size_manifest_never_matches_without_a_point_count(tmp_path):
    source_root = tmp_path / "Downloads" / "batch"
    source_root.mkdir(parents=True)
    source = source_root / "crew.csv"
    source.write_bytes(b"legacy source")
    legacy_record = {
        "source_relative": "batch/crew.csv",
        "stored_relative": "field data/crew.csv",
        "size": source.stat().st_size,
    }

    assert not imported_file_matches([legacy_record], source_root, source, 1)
    migrated = {**legacy_record, "point_count": 1}
    assert imported_file_matches([migrated], source_root, source, 1)
