"""Create a portable, self-contained folder for a Plumbline project.

A ``.plb`` file stores the drawing and project settings, but field books, source
field data, and file-backed imagery may live beside it or somewhere else.  Save As
therefore makes a new job-folder tree, copies an existing job folder when present,
collects referenced external assets, and rewrites the copied project's paths.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import jobtemplate as JT
from .project import Project


_IMAGE_SIDECARS = (".tfw", ".tifw", ".jgw", ".pgw", ".j2w", ".wld", ".prj", ".ovr", ".aux.xml")


def _resolved(path) -> Path | None:
    if path in (None, ""):
        return None
    try:
        return Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _available_path(folder: Path, name: str) -> Path:
    """Return a non-conflicting file/folder name without overwriting package data."""
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while True:
        option = folder / f"{stem} ({index}){suffix}"
        if not option.exists():
            return option
        index += 1


def _is_job_folder(folder: Path) -> bool:
    """Only copy a whole neighboring folder when it looks like a job package.

    A standalone project can be saved on a Desktop or in a general-purpose folder;
    cloning every sibling there would be surprising.  Standard Plumbline folder names
    (including the smaller supported templates) are the marker that the folder itself
    is a package and all of its contents should travel with Save As.
    """
    top_levels = {
        path.split("/", 1)[0]
        for template in JT.TEMPLATES.values()
        for path in template.all_paths()
    }
    return any((folder / name).is_dir() for name in top_levels) or any(
        (folder / marker).is_file() for marker in (JT.SETUP_NOTE, JT.FIELD_DATA_NOTE)
    )


def create_project_package(project: Project, parent_folder, name: str,
                           template: JT.JobTemplate | str | None = None) -> Project:
    """Create a unique job folder and return a saved project whose assets live inside it.

    Existing standard job folders are copied in full (not just the files known to the
    application).  Referenced files outside that folder are copied into the appropriate
    ``Field Book``, ``Field Data``, or ``Imagery`` subfolder.  The new ``.plb`` replaces
    the copied source project's file while leaving the original folder untouched.

    The destination must not already exist; this operation never merges into or overwrites
    an existing project folder.
    """
    safe_name = JT._safe_name(name)
    parent = Path(parent_folder).expanduser().resolve()
    destination = parent / safe_name
    if destination.exists():
        raise FileExistsError(f"The project folder already exists:\n{destination}\n\nChoose a unique folder name.")

    source_project_path = _resolved(getattr(project, "path", None))
    source_root = source_project_path.parent if source_project_path is not None else None
    copy_source_folder = bool(source_root and source_root.is_dir() and _is_job_folder(source_root))
    if copy_source_folder and _is_within(destination.resolve(), source_root):
        raise ValueError("Choose a location outside the current project folder so the package does not copy itself.")

    asset_map: dict[Path, Path] = {}
    try:
        creation = JT.create_job(
            parent, safe_name, template=template or JT.JOB_TEMPLATE,
            crs_label=getattr(getattr(project, "crs", None), "label", "UNASSIGNED (no CRS)"),
            crs_record=project.crs.to_dict(), starter_files=False,
        )
        root = creation.paths.root

        if copy_source_folder:
            # Preserve every file already in a real Plumbline job folder; template folders
            # created above are merged rather than replaced.
            shutil.copytree(source_root, root, dirs_exist_ok=True)

        package = Project.from_dict(project.to_dict())
        package.name = safe_name
        package._portable_paths = True

        def package_path(raw_path, preferred_folder: str, *, allow_directory: bool = False) -> str:
            if raw_path in (None, ""):
                return str(raw_path or "")
            raw = Path(str(raw_path)).expanduser()
            candidates = [raw]
            if not raw.is_absolute() and source_root is not None:
                candidates.insert(0, source_root / raw)
            existing = next((candidate for candidate in candidates if candidate.exists()), None)
            if existing is None:
                # Keep a missing reference as-is; the package remains openable and the
                # missing source is not silently mistaken for a copied file.
                return str(raw_path)
            source = existing.resolve()
            if copy_source_folder and _is_within(source, source_root):
                return str(root / source.relative_to(source_root))
            if source in asset_map:
                return str(asset_map[source])

            preferred = root / preferred_folder
            preferred.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if not allow_directory:
                    return str(raw_path)
                target = _available_path(preferred, source.name)
                shutil.copytree(source, target)
            else:
                target = _available_path(preferred, source.name)
                shutil.copy2(source, target)
            asset_map[source] = target
            return str(target)

        # Project settings that point outside the package.
        for key, subfolder, allow_directory in (
            ("fieldbook_file", "Field Book", False),
            ("f2f_path", "Field Book/Source Files", False),
            ("data_folder", "Field Data", True),
        ):
            value = (package.settings or {}).get(key)
            if value:
                package.settings[key] = package_path(value, subfolder, allow_directory=allow_directory)

        # File imagery often needs its world file / projection sidecar alongside the image.
        for layer in package.imagery.values():
            source = getattr(layer, "source", None)
            if not isinstance(source, dict) or not source.get("path"):
                continue
            old_path = Path(str(source["path"])).expanduser()
            candidates = [old_path]
            if not old_path.is_absolute() and source_root is not None:
                candidates.insert(0, source_root / old_path)
            existing = next((candidate for candidate in candidates if candidate.is_file()), None)
            new_path = package_path(source["path"], "Imagery")
            source["path"] = new_path
            if existing is None:
                continue
            source_file = existing.resolve()
            if copy_source_folder and _is_within(source_file, source_root):
                continue
            destination_image = Path(new_path)
            if destination_image == source_file or not destination_image.exists():
                continue
            sidecar_names = {source_file.stem + suffix for suffix in _IMAGE_SIDECARS}
            sidecar_names.update({source_file.name + suffix for suffix in (".aux.xml", ".ovr")})
            for sidecar_name in sidecar_names:
                sidecar = source_file.with_name(sidecar_name)
                if not sidecar.is_file():
                    continue
                if sidecar.name.startswith(source_file.name):
                    suffix = sidecar.name[len(source_file.name):]
                    sidecar_target = destination_image.with_name(destination_image.name + suffix)
                elif sidecar.name.startswith(source_file.stem):
                    suffix = sidecar.name[len(source_file.stem):]
                    sidecar_target = destination_image.with_name(destination_image.stem + suffix)
                else:
                    sidecar_target = destination_image.parent / sidecar.name
                if not sidecar_target.exists():
                    shutil.copy2(sidecar, sidecar_target)

        # Keep import provenance usable after moving the job.  Folder names recorded
        # relative to the original job remain relative; external folders are carried in
        # Field Data/Source Files and rewritten as package-relative provenance.
        for point in package.points.values():
            attrs = point.attrs if isinstance(point.attrs, dict) else {}
            point.attrs = attrs
            records = []
            imported = attrs.get("import")
            if isinstance(imported, dict) and imported.get("folder"):
                records.append((imported, "folder", "Field Data/Source Files"))
            if attrs.get("fieldwork_folder"):
                records.append((attrs, "fieldwork_folder", "Field Data/Source Files"))
            for record, key, subfolder in records:
                raw_folder = str(record.get(key) or "")
                raw = Path(raw_folder).expanduser()
                candidates = [raw]
                if not raw.is_absolute() and source_root is not None:
                    candidates.insert(0, source_root / raw)
                existing = next((candidate for candidate in candidates if candidate.is_dir()), None)
                if existing is None:
                    continue
                folder = existing.resolve()
                if copy_source_folder and _is_within(folder, source_root):
                    record[key] = str(folder.relative_to(source_root))
                else:
                    packaged_folder = Path(package_path(str(folder), subfolder, allow_directory=True))
                    try:
                        record[key] = str(packaged_folder.relative_to(root))
                    except ValueError:
                        record[key] = str(packaged_folder)

            legacy_source = attrs.get("fieldwork_source")
            if legacy_source:
                raw_source = Path(str(legacy_source)).expanduser()
                source_candidates = [raw_source]
                if not raw_source.is_absolute() and source_root is not None:
                    source_candidates.insert(0, source_root / raw_source)
                if any(candidate.is_file() for candidate in source_candidates):
                    packaged_source = Path(package_path(
                        str(legacy_source), "Field Data/Source Files"))
                    try:
                        attrs["fieldwork_source"] = str(packaged_source.relative_to(root))
                    except ValueError:
                        attrs["fieldwork_source"] = str(packaged_source)

        project_file = creation.paths.project_file
        package.path = str(project_file)
        package.save(project_file)
        return package
    except Exception:
        # The target did not exist on entry, so any partial tree is ours to remove.
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise
