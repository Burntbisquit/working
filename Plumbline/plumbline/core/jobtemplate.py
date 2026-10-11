"""The shape of a survey job folder, and the machinery to create one.

A job folder is the thing that outlives the software.  Three years from now someone
opens ``23-036.03/`` and has to find the raw download, the code table that decoded
it, the control, and the last drawing without asking anybody.  This module is that
convention written down once, so New Project, the field-data importer and the
sample-data builder all produce the same layout.

    <Job>/
        <Job>.plb               the Plumbline project (single file, opens by double-click)
        Field Data/             raw downloads, exactly as they came off the collector
            (whatever you name it)/       one folder per download, per week, per crew
                Crew 6/                    numbered from the point-number blocks
        Field Book/             the F2F / code table, code commands, correction rules
        Control/                control points, coordinate-system record
        Drawings/               DXF and PDF
        Surfaces/               surface exports, volumes
        Imagery/                aerials, KMZ for Google Earth
        Reports/                check reports, point lists, data-quality reports

Everything here is Qt-free so the CLI, the tests and the GUI all use one
implementation.  Creation is **cancelable and rolled back**: if a user cancels
half-way through, the job does not exist in a half-made state that a later
"create" would refuse to write into.
"""
from __future__ import annotations

import datetime as _dt
import re
import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------------------------- template
@dataclass(frozen=True)
class JobFolder:
    name: str
    purpose: str
    children: tuple["JobFolder", ...] = ()

    @property
    def relative(self) -> str:
        return self.name


#: What goes inside Field Data/ when a job is created.  It is a *note*, not a folder: the
#: naming inside Field Data is the office's business - "Week 1", "Stage 2", "North line",
#: "2026-07-22" - and a program that pre-builds "Week 1" only teaches people to ignore it.
FIELD_DATA_NOTE = "How To Use This Folder.txt"

FIELD_DATA_NOTE_TEXT = """Field Data - what goes in here

One folder per download, named the way your office names them.  Any of these is right:

    Week 1/  Crew 6/          a week of work, one folder per crew
    Stage 2/  Setout/         by stage, or by what the crew was doing
    2026-07-22/               by the date the file came off the collector
    North line/               by the area

Nothing in this program depends on those names.  The Fieldwork Manager reads every
field file under Field Data, whichever folder it is in, and works the crew out from the
point numbers themselves - so a new week, a new crew or a new naming scheme costs nothing.

The raw download is the record: keep the files exactly as they came off the collector here,
and let everything else (the consolidated file, the drawing, the surface) live where the job
folder says it lives.
"""


@dataclass(frozen=True)
class JobTemplate:
    """A named folder convention.  ``job_paths()`` and the UI both read this."""
    name: str
    folders: tuple[JobFolder, ...]
    description: str = ""

    def all_paths(self) -> list[str]:
        out: list[str] = []

        def walk(f: JobFolder, prefix: str = ""):
            rel = f"{prefix}/{f.name}" if prefix else f.name
            out.append(rel)
            for c in f.children:
                walk(c, rel)

        for f in self.folders:
            walk(f)
        return out


#: The default.  Field Data/ is created **empty** - no Week 1, no Crew folders - with a note
#: explaining that the naming inside it is the office's own.  The program works out the
#: crews from the point numbers, so it has no opinion about what the folder is called.
JOB_TEMPLATE = JobTemplate(
    name="Standard survey job",
    description="Field data, code table, control and output folders, with the office standards kept beside it.",
    folders=(
        JobFolder("Field Data", "imported survey folders and source files"),
        JobFolder("Field Book", "selected/imported field book and code rules"),
        JobFolder("Control", "control point files and coordinate-system record"),
        JobFolder("Drawings", "DXF and PDF output"),
        JobFolder("Surfaces", "surface exports and volume reports"),
        JobFolder("Imagery", "aerial imagery and KMZ for Google Earth"),
        JobFolder("Reports", "check reports, point lists, data-quality reports"),
    ),
)

#: Deliberately flat - for a small job, folders are overhead.
MINIMAL_TEMPLATE = JobTemplate(
    name="Minimal",
    description="One folder for field data and one for output.",
    folders=(
        JobFolder("Field Data", "raw downloads and consolidated field data"),
        JobFolder("Output", "drawings, surfaces, reports"),
    ),
)

TEMPLATES = {t.name: t for t in (JOB_TEMPLATE, MINIMAL_TEMPLATE)}

PROJECT_EXT = ".plb"
FIELDBOOK_EXT = ".fwb"
CONTROL_CSV = "Control Points.csv"
SETUP_NOTE = "Job Setup.txt"


# --------------------------------------------------------------------------------------------- paths
@dataclass
class JobPaths:
    root: Path
    name: str

    @property
    def project_file(self) -> Path:
        return self.root / f"{self.name}{PROJECT_EXT}"

    @property
    def field_data(self) -> Path:
        return self.root / "Field Data"

    @property
    def fieldbook_dir(self) -> Path:
        return self.root / "Field Book"

    @property
    def fieldbook_file(self) -> Path:
        return self.fieldbook_dir / f"{self.name}{FIELDBOOK_EXT}"

    @property
    def control_file(self) -> Path:
        return self.root / "Control" / CONTROL_CSV

    @property
    def reports(self) -> Path:
        return self.root / "Reports"

    @property
    def drawings(self) -> Path:
        return self.root / "Drawings"

    @property
    def surfaces(self) -> Path:
        return self.root / "Surfaces"

    @property
    def imagery(self) -> Path:
        return self.root / "Imagery"

    @property
    def setup_note(self) -> Path:
        return self.root / SETUP_NOTE

    def week(self, week: int) -> Path:
        """``Field Data/Week N`` - a convenience for a job that names its folders that way
        (the samples do, and so do most offices).  Not created by :func:`create_job`."""
        return self.field_data / f"Week {week}"

    def crew(self, week: int, crew: int) -> Path:
        return self.week(week) / f"Crew {crew}"

    def to_dict(self) -> dict:
        return {"root": str(self.root), "name": self.name,
                "project_file": str(self.project_file),
                "field_data": str(self.field_data),
                "fieldbook_file": str(self.fieldbook_file),
                "control_file": str(self.control_file)}


# --------------------------------------------------------------------------------------------- creation
class JobCreationCancelled(RuntimeError):
    """Raised when the caller cancels; by then everything created has been removed."""


@dataclass
class JobCreation:
    paths: JobPaths
    folders: list[Path] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def summary(self) -> str:
        if self.cancelled:
            return "cancelled - nothing was kept"
        return f"{len(self.folders)} folders, {len(self.files)} files"

    def as_dict(self) -> dict:
        return {"root": str(self.paths.root), "name": self.paths.name,
                "folders": [str(p) for p in self.folders], "files": [str(p) for p in self.files],
                "skipped": list(self.skipped), "cancelled": self.cancelled}


def _safe_name(name: str) -> str:
    """A folder name Windows, macOS and Linux all accept, and that sorts sensibly.

    Reserved characters become underscores and runs of them collapse, so
    ``A/B:C*D?"E`` becomes ``A_B_C_D_E`` rather than ``A_B_C_D__E``.
    """
    bad = '<>:"/\\|?*'
    out = "".join(("_" if c in bad or ord(c) < 32 else c) for c in str(name))
    out = re.sub(r"_{2,}", "_", out).strip().strip(".")
    return out or "Untitled Job"


def _setup_note_text(paths: JobPaths, template: JobTemplate, crs_label: str, comment: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"{paths.name} - job folder setup",
        "=" * (len(paths.name) + 21),
        f"Created      {stamp}",
        f"Root         {paths.root}",
        f"Project file {paths.project_file.name}",
        f"Folder layout  {template.name}",
        f"Coordinate system  {crs_label}",
        "",
        "Layout",
        "------",
    ]
    for rel in template.all_paths():
        lines.append(f"  {rel}/")
    lines += [
        "",
        "Conventions this job follows",
        "----------------------------",
        "  * Field Data is never edited in place - the collector's download is the record.",
        "  * Field Data/ is yours to name - one folder per download (Week 1, Stage 2, a date).",
        "    Crew folders are named from the point-number blocks:",
        "      Control    1 - 999            (shared by every crew)",
        "      Boundary   1,000 - 9,999",
        "      General    10,000 and up",
        "      Crew N owns N00-N99, N,000-N,999, N0,000-N9,999 ... at every magnitude,",
        "      so crew 7 shoots 7,001-7,999 and 70,001-79,999 but never 6,xxx or 8,xxx.",
        "  * Coordinates are rounded half-up on output; nothing is truncated.",
        "  * Distances on the ground are adjusted by SAF (surface adjustment factor,",
        "    ground over grid), scaled from the projection origin per TXDOT practice.",
    ]
    if comment.strip():
        lines += ["", "Notes", "-----", comment.strip()]
    lines.append("")
    return "\n".join(lines)


def job_folder_contents(root, name: str) -> dict:
    """What is already in ``root/name`` - the facts a "this will be deleted" question needs."""
    folder = Path(root).expanduser() / _safe_name(name)
    out = {"folder": folder, "exists": folder.exists(), "items": 0, "files": 0, "folders": 0,
           "projects": [], "bytes": 0, "empty": True}
    if not folder.exists():
        return out
    for entry in folder.iterdir():
        out["items"] += 1
        if entry.is_dir():
            out["folders"] += 1
        else:
            out["files"] += 1
        if entry.suffix.lower() == PROJECT_EXT:
            out["projects"].append(entry.name)
        try:
            out["bytes"] += sum(f.stat().st_size for f in ([entry] if entry.is_file() else entry.rglob("*"))
                                if f.is_file())
        except OSError:
            pass
    out["empty"] = out["items"] == 0
    return out


def clear_job_folder(root, name: str) -> dict:
    """Empty ``root/name`` so a job folder can be made over the top of it.

    **This deletes.**  It is only ever called from the UI after the three-step confirmation
    (``ui.widgets.destructive_confirm``) and after the caller has offered the recycle bin
    (``ui.job_setup.overwrite_job_folder``); it does not ask again, and it does not delete the
    folder itself, only what is inside it.
    """
    import shutil
    folder = Path(root).expanduser() / _safe_name(name)
    removed = {"files": 0, "folders": 0, "bytes": 0}
    if not folder.exists():
        return removed
    for entry in list(folder.iterdir()):
        try:
            if entry.is_dir() and not entry.is_symlink():
                removed["folders"] += 1
                removed["bytes"] += sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                shutil.rmtree(entry)
            else:
                removed["files"] += 1
                removed["bytes"] += entry.stat().st_size
                entry.unlink()
        except OSError:
            continue                        # a file in use: leave it, stop nothing
    return removed


def create_job(root, name: str, template: JobTemplate | str = JOB_TEMPLATE,
               crs_label: str = "UNASSIGNED (no CRS)",
               comment: str = "", weeks: int | None = None, overwrite: bool = False,
               starter_files: bool = True,
               progress=None, is_cancelled=None, crs_record: dict | None = None) -> JobCreation:
    """Create a job folder tree.  Safe to cancel at any point.

    Parameters
    ----------
    root            parent folder the job folder is created inside
    name            job name (also the project file name)
    template        :data:`JOB_TEMPLATE`, :data:`MINIMAL_TEMPLATE`, or a name
    crs_label       human-readable coordinate-system text for the setup note
    crs_record      serialized :class:`ProjectCRS` used for the project file; unlike
                    ``crs_label``, this preserves units, derived systems, vertical datum,
                    geoid, ground scale and its base point exactly
    weeks           how many ``Field Data/Week N`` folders to make
    overwrite       allow writing into a non-empty existing job folder
    starter_files   write Job Setup.txt and the project file at the job root;
                    standard subfolders remain empty. False leaves only the folder tree.
    progress        ``progress(fraction, message)`` - called before each step
    is_cancelled    ``is_cancelled() -> bool`` - polled before each step

    If ``is_cancelled`` ever returns True, every folder and file this call created
    is removed and :class:`JobCreationCancelled` is raised.  Anything that already
    existed before the call is left strictly alone.
    """
    if isinstance(template, str):
        template = TEMPLATES.get(template, JOB_TEMPLATE)
    name = _safe_name(name)
    root = Path(root).expanduser()
    paths = JobPaths(root / name, name)

    if paths.root.exists() and any(paths.root.iterdir()) and not overwrite:
        raise FileExistsError(
            f"{paths.root} already exists and is not empty. "
            f"Choose another name, another folder, or allow writing into it.")

    creation = JobCreation(paths=paths)
    # Include the root itself.  On cancellation an empty job folder that existed before
    # this call is just as much the user's data as a file inside a non-empty one.
    pre_existing = ({paths.root, *paths.root.rglob("*")} if paths.root.exists() else set())

    def check(step: str, fraction: float):
        if is_cancelled is not None and is_cancelled():
            _rollback(creation, pre_existing)
            creation.cancelled = True
            raise JobCreationCancelled(f"cancelled before {step}")
        if progress is not None:
            progress(fraction, step)

    try:
        check(f"creating {paths.root.name}", 0.05)
        paths.root.mkdir(parents=True, exist_ok=True)
        creation.folders.append(paths.root)

        folders = list(template.folders)
        if weeks:
            # Older callers asked for "Week N" folders.  They are no longer pre-built, and
            # saying so out loud is better than quietly making folders nobody asked for.
            print(f"[job] 'weeks={weeks}' is ignored - Field Data/ is created empty by design")

        total = sum(1 + _count_children(f) for f in folders)
        done = 0
        for f in folders:
            check(f"creating {f.name}", 0.05 + 0.6 * (done / max(total, 1)))
            for rel in _expand(f):
                p = paths.root / rel
                if not p.exists():
                    p.mkdir(parents=True, exist_ok=True)
                    creation.folders.append(p)
                done += 1

        check("setting up the Field Data folder", 0.66)
        # Field Data/ is created empty - name the folders inside it your way.

        if not starter_files:
            if progress is not None:
                progress(1.0, "done")
            return creation

        check("writing the job setup note", 0.76)
        paths.setup_note.write_text(_setup_note_text(paths, template, crs_label, comment),
                                    encoding="utf-8")
        creation.files.append(paths.setup_note)

        check("saving the project", 0.90)
        _write_empty_project(paths, crs_label, crs_record)
        creation.files.append(paths.project_file)

        if progress is not None:
            progress(1.0, "done")
        return creation
    except JobCreationCancelled:
        raise
    except Exception:
        _rollback(creation, pre_existing)
        raise


def _count_children(f: JobFolder) -> int:
    return sum(1 + _count_children(c) for c in f.children)


def _expand(f: JobFolder, prefix: str = ""):
    rel = f"{prefix}/{f.name}" if prefix else f.name
    yield rel
    for c in f.children:
        yield from _expand(c, rel)


def _write_empty_project(paths: JobPaths, crs_label: str, crs_record: dict | None = None) -> None:
    """Write the starter project without reverse-parsing its display label.

    ``crs_label`` remains supported for older callers and setup-note compatibility.  New
    callers provide ``crs_record`` because a label cannot faithfully represent an
    unassigned project's units or every property of a derived/vertical/ground CRS.
    """
    from ..core.project import Project
    from ..core.crs import ProjectCRS
    project_crs = ProjectCRS.from_dict(crs_record) if crs_record else _crs_from_label(crs_label)
    prj = Project(paths.name, project_crs)
    # The standard subfolders are intentionally empty. Preserve the default command
    # vocabulary for the first Field Book the user chooses, without pointing at a
    # placeholder file that does not exist.
    from .fieldbook_syntax import DEFAULT_COMMAND_TOKENS
    prj.settings["f2f_commands"] = list(DEFAULT_COMMAND_TOKENS)
    prj.settings["f2f_rules"] = []
    prj.notes = (f"New job created {_dt.datetime.now():%Y-%m-%d}.\n"
                 f"Field data goes in {paths.field_data.name}/ - "
                 f"see {SETUP_NOTE} for the layout.")
    prj.save(paths.project_file)


def _crs_from_label(label: str):
    """Turn the setup-note wording back into a CRS object (unassigned by default)."""
    from ..core.crs import ProjectCRS
    from ..core.crs import resolve_crs
    label = (label or "").strip()
    if not label or "UNASSIGNED" in label.upper():
        return ProjectCRS.unassigned("ftUS")
    import re
    m = re.search(r"(\d{4,5})", label)
    if m:
        try:
            return ProjectCRS.from_epsg(int(m.group(1)))
        except Exception:
            pass
    try:
        return ProjectCRS(resolve_crs(label))
    except Exception:
        return ProjectCRS.unassigned("ftUS")


def _rollback(creation: JobCreation, pre_existing: set) -> None:
    """Remove everything this call made, deepest first.  Never touch what was there."""
    for f in reversed(creation.files):
        try:
            if f.exists() and f not in pre_existing:
                f.unlink()
        except OSError:
            pass
    for d in sorted(creation.folders, key=lambda p: len(str(p)), reverse=True):
        try:
            if d.exists() and d not in pre_existing:
                _remove_if_empty(d)
        except OSError:
            pass


def _remove_if_empty(d: Path, up_to: Path | None = None) -> None:
    for child in d.rglob("*"):
        if child.is_file():
            return                                       # something made it in - leave it
    for sub in sorted((p for p in d.rglob("*") if p.is_dir()), key=lambda p: len(str(p)), reverse=True):
        try:
            sub.rmdir()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass


__all__ = ["JobFolder", "JobTemplate", "JOB_TEMPLATE", "MINIMAL_TEMPLATE", "TEMPLATES",
           "JobPaths", "JobCreation", "JobCreationCancelled", "create_job", "PROJECT_EXT"]
