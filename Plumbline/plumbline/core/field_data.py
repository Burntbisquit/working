"""Portable bookkeeping for field-data files staged into a project package."""
from __future__ import annotations

from pathlib import Path


def relative_file_identity(root, file_path) -> tuple[str, str]:
    """Return the selected-root-relative folder and filename for a field-data file.

    The selected folder's own name is part of the folder identity, followed by every nested
    folder.  A same-named file in another selected folder is therefore a different import.
    """
    root_path = Path(root).expanduser()
    file = Path(file_path).expanduser()
    try:
        relative = file.resolve().relative_to(root_path.resolve())
    except (OSError, RuntimeError, ValueError):
        relative = Path(file.name)

    folder_parts = ([root_path.name] if root_path.name else []) + list(relative.parent.parts)
    folder = Path(*folder_parts).as_posix().casefold() if folder_parts else ""
    return folder, relative.name.casefold()


def relative_file_key(root, file_path) -> str:
    """Return a single normalized key from the selected folder and file name."""
    folder, filename = relative_file_identity(root, file_path)
    return f"{folder}/{filename}" if folder else filename


def _normalized(text) -> str:
    return str(text or "").replace("\\", "/").strip("/").casefold()


def imported_file_matches(records, root, file_path, point_count) -> bool:
    """Whether the same folder/file was imported with the same parsed point count.

    File size is deliberately not part of identity: a source file can be rewritten in place.
    The recheck supplies its current parsed point count, and an updated file with a different
    count remains selectable even when its name and folder are unchanged. The legacy combined
    relative-path fields are still recognized so old project manifests can be migrated safely.
    """
    try:
        current_count = int(point_count)
    except (TypeError, ValueError):
        return False
    if current_count < 0:
        return False

    folder, filename = relative_file_identity(root, file_path)
    current_pair = (_normalized(folder), _normalized(filename))
    current_key = _normalized(relative_file_key(root, file_path))
    for record in records or ():
        if not isinstance(record, dict):
            continue
        try:
            prior_count = int(record.get("point_count"))
        except (TypeError, ValueError):
            continue
        if prior_count != current_count:
            continue

        recorded_pairs = {
            (_normalized(record.get("source_folder")), _normalized(record.get("source_file"))),
            (_normalized(record.get("stored_folder")), _normalized(record.get("stored_file"))),
        }
        recorded_pairs.discard(("", ""))
        if current_pair in recorded_pairs:
            return True

        # Migration compatibility for manifests written before file/folder and point-count
        # metadata was introduced. Their point_count is added only by parsing the preserved
        # staged copy; the obsolete size field is never consulted.
        for key in (record.get("source_relative"), record.get("stored_relative")):
            if _normalized(key) and _normalized(key) == current_key:
                return True
    return False


def make_import_record(source_root, source_file, stored_root=None, stored_file=None, *, point_count) -> dict:
    """Create the portable manifest row for one successfully imported source file."""
    source_folder, source_name = relative_file_identity(source_root, source_file)
    record = {
        "source_folder": source_folder,
        "source_file": source_name,
        "stored_folder": "",
        "stored_file": "",
        "point_count": int(point_count),
    }
    if stored_root is not None and stored_file is not None:
        record["stored_folder"], record["stored_file"] = relative_file_identity(
            stored_root, stored_file)
    return record


__all__ = [
    "relative_file_identity", "relative_file_key", "imported_file_matches", "make_import_record",
]
