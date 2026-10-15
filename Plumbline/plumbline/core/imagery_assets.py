"""Local imagery assets that travel with a Plumbline project."""
from __future__ import annotations

import shutil
from pathlib import Path


# World files, projection metadata, and GDAL sidecars used by supported raster formats.
_IMAGE_SIDECARS = (
    ".tfw", ".tifw", ".tiffw", ".jgw", ".jpgw", ".jpegw", ".pgw", ".pngw",
    ".bpw", ".bmpw", ".gfw", ".gifw", ".j2w", ".wld", ".prj", ".ovr", ".aux.xml",
)


def _available_path(folder: Path, name: str) -> Path:
    """Choose a unique image filename without replacing files already in the job."""
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


def _copy_image_sidecars(source_file: Path, destination_image: Path) -> list[Path]:
    """Copy matching world-file, CRS, overview, and auxiliary metadata sidecars."""
    names = {source_file.stem + suffix for suffix in _IMAGE_SIDECARS}
    names.update(source_file.name + suffix for suffix in (".aux.xml", ".ovr"))
    wanted = {name.casefold() for name in names}
    copied = []
    try:
        for sidecar in sorted(source_file.parent.iterdir(), key=lambda path: path.name.casefold()):
            if sidecar.name.casefold() not in wanted or not sidecar.is_file():
                continue
            if sidecar.name.casefold().startswith(source_file.name.casefold()):
                suffix = sidecar.name[len(source_file.name):]
                target = destination_image.with_name(destination_image.name + suffix)
            elif sidecar.name.casefold().startswith(source_file.stem.casefold()):
                suffix = sidecar.name[len(source_file.stem):]
                target = destination_image.with_name(destination_image.stem + suffix)
            else:
                target = destination_image.parent / sidecar.name
            if not target.exists():
                shutil.copy2(sidecar, target)
                copied.append(target)
    except Exception:
        for path in reversed(copied):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return copied


def stage_imagery_asset(source_path, project_folder) -> Path:
    """Copy an external image and its dependencies into ``<project>/Imagery/``.

    An image already stored in that folder is left in place. Copies get a collision-free
    name so importing a second raster never overwrites the first one. If copying fails,
    any partial output from the attempt is removed.
    """
    try:
        source = Path(str(source_path)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise FileNotFoundError(f"Imagery file does not exist: {source_path}") from exc
    if not source.is_file():
        raise FileNotFoundError(f"Imagery file does not exist: {source_path}")
    if project_folder in (None, ""):
        raise ValueError("The project folder is not available for local imagery.")
    root = Path(str(project_folder)).expanduser().resolve()
    imagery_folder = root / "Imagery"
    if source.is_relative_to(imagery_folder.resolve()):
        return source

    imagery_folder.mkdir(parents=True, exist_ok=True)
    destination = _available_path(imagery_folder, source.name)
    copied = []
    try:
        shutil.copy2(source, destination)
        copied.append(destination)
        copied.extend(_copy_image_sidecars(source, destination))
    except Exception:
        for path in reversed(copied):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return destination.resolve()
