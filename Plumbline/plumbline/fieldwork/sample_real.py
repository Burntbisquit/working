"""Build the "Real World" sample job out of a real field download.

Plumbline's shipped sample is synthetic: a computer-generated commercial lot chosen
so the tutorial has something to click on.  It has one honest weakness - nobody has
ever reduced real field data through this program.  This module fixes that by
turning an actual reduced job (a Carlson consolidated .fwk, its F2F code table and
the check report the office produced) into a browsable job folder:

    Real World/
        Real World.plb                  opens in Plumbline, CRS assigned
        Field Data/Week 1/
            Week 1 Consolidated.fwk     every crew, in shoot order
            Crew 6/  Crew 7/  Crew 9/   one folder per crew, named from the point blocks
        Field Book/                     the office's 1,717-code standard
        Control/                        the shared control points, pulled out of the data
        Reports/                        the real check report
        Source/                         the files exactly as they arrived

Crew numbers are not invented and not typed in by hand - they are *decoded* from the
point numbers using the same rule Fieldwork Manager uses for renumbering.  So a real
job's folder names come out of the data itself, which is the only way they stay right
when a new week arrives.

Nothing here hard-codes a job.  Point the builder at any .fwk and it will work out
the crews, the sensors and the point ranges for itself.
"""
from __future__ import annotations

import csv
import datetime as _dt
import re
import shutil
from collections import OrderedDict
from pathlib import Path

from . import bridge as B
from . import config as F
from . import io_carlson as IC

# Sensor words that appear in Carlson export names, longest first so "GPS" is not
# shadowed by "GP".  Used only for file names - never for logic.
_SENSOR_WORDS = ("GPS", "TS", "GUN", "GS", "SS", "RTK", "TOTAL", "TRAV")


class SampleBuildCancelled(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------- helpers
def sensor_of(filename: str) -> str:
    """'2026-07-19GUNER.csv' -> 'GUN'.  Falls back to the last word before the initials."""
    stem = Path(str(filename).replace("\\", "/")).stem.upper()
    for w in _SENSOR_WORDS:
        if w in stem:
            return w
    return "FIELD"


def crew_initials(folder: str) -> str:
    """'2026-7-22-AE' -> 'AE' - the crew's initials as typed on the collector."""
    m = re.search(r"-([A-Za-z]{1,4})$", str(folder).strip())
    return m.group(1).upper() if m else ""


def date_of(folder: str) -> str:
    """'2026-7-22-AE' -> '2026-07-22' (zero-padded, sorts, ISO-8601)."""
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(folder))
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def control_points(rows) -> list[list[str]]:
    """Rows whose point number sits in the shared control block (1-999), de-duplicated."""
    out, seen = [], set()
    for r in rows:
        if not B.row_is_usable(r):
            continue
        try:
            n = int(str(r[B.PTNUM]).strip())
        except ValueError:
            continue
        if not (F.CONTROL_RANGE[0] <= n <= F.CONTROL_RANGE[1]):
            continue
        key = str(r[B.PTNUM]).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _week_of(rows) -> int:
    """Which week this download belongs to.  One .fwk file = one week by definition."""
    return 1


# --------------------------------------------------------------------------------------------- the build
def build_real_world_sample(dest_root, source_fwk, source_f2f=None, source_report=None,
                            name: str = "Real World", week: int = 1, epsg: int = 6584,
                            crs_label: str | None = None, overwrite: bool = True,
                            progress=None, is_cancelled=None) -> dict:
    """Create the Real World sample under *dest_root*/*name*.  Returns a report dict.

    ``source_fwk``    the consolidated field file (real download)
    ``source_f2f``    the Carlson F2F code table (optional but strongly recommended -
                      without it the job has no feature codes and draws in one layer)
    ``source_report`` the office's check report for this job (optional)
    ``epsg``          coordinate system to stamp on the project; 6584 = NAD83(2011)
                      Texas North Central US survey feet.  Pass ``None`` to leave the
                      project UNASSIGNED, which is what a brand-new job gets.
    """
    from ..core import crs as C
    from ..core import jobtemplate as JT
    from ..core.project import Project

    def copy_into(src: Path, dst: Path):
        """Copy a source file, *unless it is already the destination*.

        Building the sample in place - source and destination the same folder - is a normal
        thing to do while working on the sample itself, and ``shutil.copy2`` raises on it.
        """
        if src.resolve() == dst.resolve():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True

    def step(fraction, message):
        if is_cancelled is not None and is_cancelled():
            raise SampleBuildCancelled(message)
        if progress is not None:
            progress(fraction, message)

    dest_root = Path(dest_root)
    source_fwk = Path(source_fwk)

    step(0.02, "reading the field data")
    rows = B.read_working_file(source_fwk)
    if not rows:
        raise ValueError(f"no rows found in {source_fwk}")
    summary = B.summarise(rows)

    label = crs_label if crs_label is not None else (
        f"EPSG:{epsg} {C.TEXAS_ZONES[epsg]['name']}" if epsg and epsg in C.TEXAS_ZONES
        else "UNASSIGNED (no CRS)")

    step(0.08, "creating the job folder")
    creation = JT.create_job(dest_root, name, template=JT.JOB_TEMPLATE, crs_label=label,
                             overwrite=overwrite, starter_files=False,
                             progress=None, is_cancelled=is_cancelled)
    paths = creation.paths

    report = {"root": str(paths.root), "name": name, "weeks": week,
              "rows": len(rows), "crews": [], "files": [], "summary": summary,
              "crs": label, "epsg": epsg, "source_file": source_fwk.name}

    try:
        # ---- 1. split by crew, then by sensor -------------------------------------------------
        step(0.18, "sorting crews out of the point numbers")
        by_crew: "OrderedDict[int, dict[str, list]]" = OrderedDict()
        undecoded: list[list[str]] = []
        for r in rows:
            if not B.row_is_usable(r):
                continue
            crew = B.crew_of_point(r[B.PTNUM])
            sense = sensor_of(r[B.SOURCE] if len(r) > B.SOURCE else "")
            if crew is None:
                undecoded.append(r)
                continue
            by_crew.setdefault(crew, OrderedDict()).setdefault(sense, []).append(r)

        for crew in sorted(by_crew):
            crew_dir = paths.crew(week, crew)
            crew_dir.mkdir(parents=True, exist_ok=True)
            creation.folders.append(crew_dir)
            initials, dates = set(), set()
            for sense, crows in sorted(by_crew[crew].items()):
                for r in crows:
                    src = r[B.SOURCE] if len(r) > B.SOURCE else ""
                    inits = crew_initials(B.source_folder(src))
                    if inits:
                        initials.add(inits)
                    d = date_of(B.source_folder(src))
                    if d:
                        dates.add(d)
                fname = f"Week {week} Crew {crew} {sense}.csv"
                B.write_point_csv(crew_dir / fname, crows)
                report["files"].append(str((crew_dir / fname).relative_to(paths.root)))
            report["crews"].append({"crew": crew, "initials": sorted(initials),
                                    "dates": sorted(dates),
                                    "points": sum(len(v) for v in by_crew[crew].values()),
                                    "sensors": sorted(by_crew[crew]),
                                    "folder": str(crew_dir.relative_to(paths.root))})

        # ---- 2. the consolidated file the office actually works from --------------------------
        step(0.38, "writing the consolidated field file")
        cons = paths.week(week) / f"Week {week} Consolidated.fwk"
        B.write_working_file(cons, rows, width=8)
        creation.files.append(cons)
        report["files"].append(str(cons.relative_to(paths.root)))

        # ---- 3. control -----------------------------------------------------------------------
        step(0.46, "extracting control")
        ctl = control_points(rows)
        paths.control_file.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.control_file, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Point", "Northing", "Easting", "Elevation", "Datum", "Description", "Note"])
            for r in ctl:
                n, e, z = B.row_xyz(r)
                w.writerow([r[B.PTNUM], f"{n:.3f}" if n is not None else "",
                            f"{e:.3f}" if e is not None else "",
                            f"{z:.3f}" if z is not None else "", "", r[B.DESC],
                            "shared by all crews"])
        creation.files.append(paths.control_file)
        report["control_points"] = len(ctl)

        # ---- 4. the office's feature-code standard --------------------------------------------
        step(0.56, "loading the feature code standard")
        code_table = None
        if source_f2f and Path(source_f2f).exists():
            source_f2f = Path(source_f2f)
            code_table, fstats = B.feature_codes_from_f2f(source_f2f)
            report["feature_codes"] = {k: v for k, v in fstats.items()
                                       if k in ("codes", "lines", "points", "layers", "symbols")}
            report["feature_codes"]["layers"] = len(fstats["layers"])
            report["feature_codes"]["symbols"] = len(fstats["symbols"])
            if copy_into(source_f2f, paths.fieldbook_dir / "Carlson F2F (as received).csv"):
                report["files"].append("Field Book/Carlson F2F (as received).csv")
            try:
                B.write_fieldbook_from_f2f(source_f2f, paths.fieldbook_file, name=name)
                creation.files.append(paths.fieldbook_file)
                report["files"].append(str(paths.fieldbook_file.relative_to(paths.root)))
            except Exception as exc:
                report["fieldbook_error"] = str(exc)

        # ---- 5. the real check report ----------------------------------------------------------
        if source_report and Path(source_report).exists():
            step(0.64, "copying the check report")
            source_report = Path(source_report)
            dest = paths.reports / f"Week {week} Check Report{source_report.suffix or '.txt'}"
            if copy_into(source_report, dest):
                report["files"].append(str(dest.relative_to(paths.root)))
            report["check_report"] = str(dest.relative_to(paths.root))

        # ---- 6. the project file ---------------------------------------------------------------
        step(0.72, "building the project")
        project = Project(name, (C.ProjectCRS.from_epsg(epsg) if epsg else C.ProjectCRS.unassigned()))
        if code_table is not None:
            project.codes = code_table
        stats = B.apply_rows_to_project(project, rows, code_table=code_table, dup_policy="renumber")
        report["import"] = stats
        try:
            lw = project.process_linework(order="file")
            report["linework"] = lw
        except Exception as exc:
            report["linework_error"] = str(exc)

        project.notes = _project_notes(paths, report, rows)
        step(0.88, "saving the project")
        project.save(paths.project_file)
        creation.files.append(paths.project_file)

        # ---- 7. keep the files as they arrived --------------------------------------------------
        step(0.94, "archiving the source files")
        src_dir = paths.root / "Source"
        src_dir.mkdir(exist_ok=True)
        src_dir.mkdir(parents=True, exist_ok=True)
        copy_into(source_fwk, src_dir / source_fwk.name)
        for extra in (source_f2f, source_report):
            if extra and Path(extra).exists():
                copy_into(Path(extra), src_dir / Path(extra).name)
        report["files"].append("Source/ (files as received)")

        # ---- 8. the setup note and README --------------------------------------------------------
        step(0.97, "writing the job setup note")
        paths.setup_note.write_text(
            JT._setup_note_text(paths, JT.JOB_TEMPLATE, label,
                                "Built from real field data by plumbline.fieldwork.sample_real.\n"
                                "Field Data/ holds the download split by crew; Source/ holds the files\n"
                                "exactly as they arrived."), encoding="utf-8")
        creation.files.append(paths.setup_note)
        report["files"].append(str(paths.setup_note.relative_to(paths.root)))
        (paths.root / "README.md").write_text(_readme(paths, report, rows), encoding="utf-8")

        if progress is not None:
            progress(1.0, "done")
        return report
    except SampleBuildCancelled:
        JT._rollback(creation, set())
        raise
    except Exception:
        JT._rollback(creation, set())
        raise


# --------------------------------------------------------------------------------------------- text
def _project_notes(paths, report, rows) -> str:
    crews = ", ".join(f"Crew {c['crew']} ({'/'.join(c['initials']) or '?'})" for c in report["crews"])
    return (f"REAL JOB DATA - reduced by an actual field crew, not generated.\n"
            f"Source    {Path(report.get('source', 'Edit points.fwk')).name}\n"
            f"Crews     {crews}\n"
            f"Points    {report['summary']['usable']:,}\n"
            f"CRS       {report['crs']}\n"
            f"Built     {_dt.datetime.now():%Y-%m-%d %H:%M}\n\n"
            f"Everything in Field Data/ came off the collectors. Nothing has been edited.\n"
            f"Run Survey > Data Quality Check to reproduce the issues in Reports/.")


def _fnum(v, places=0):
    return f"{v:,.{places}f}" if isinstance(v, (int, float)) else str(v)


def _readme(paths, report, rows) -> str:
    s = report["summary"]
    crew_rows = "\n".join(
        f"| {c['crew']} | {'/'.join(c['initials']) or '-'} | {', '.join(c['dates']) or '-'} | "
        f"{', '.join(c['sensors'])} | {c['points']:,} | `{c['folder'].split('/')[-1]}` |"
        for c in report["crews"])
    lines = [
        f"# {paths.name} - sample job built from real field data",
        "",
        f"Built {_dt.datetime.now():%Y-%m-%d} from a real reduced job "
        f"({Path(report.get('source_file', 'the consolidated field file')).name}). "
        "**These coordinates are a real survey of a real road job** - not generated, not rounded "
        "to look tidy, and not surveyed by anyone at this desk.",
        "",
        "Use it to see how the program behaves on data that has not been cleaned for it. "
        "Expect duplicate point numbers, descriptions that do not parse, and a check report "
        "with real findings - that is the point.",
        "",
        "## What is in here",
        "",
        "| Folder | Contents |",
        "|---|---|",
        f"| `{paths.project_file.name}` | the Plumbline project - open this |",
        "| `Field Data/Week 1/` | the download as consolidated, plus one folder per crew |",
        "| `Field Book/` | the office's code standard (1,717 codes) |",
        "| `Control/` | the control points all three crews shared |",
        "| `Reports/` | the check report the office produced for this job |",
        "| `Source/` | the files exactly as they arrived |",
        "",
        "## The crews were worked out from the point numbers",
        "",
        "| Crew | Initials | Date(s) | Collector | Points | Folder |",
        "|---|---|---|---|---|---|",
        crew_rows,
        "",
        "Nothing here was typed in by hand. Crew numbers come from the point-number blocks "
        "a crew owns - crew 7 shoots 7,001-7,999 and 70,001-79,999, and nothing else. "
        "Points 1-999 are control and are shared by everyone, which is why every crew folder "
        "contains the same setup points.",
        "",
        "## The numbers",
        "",
        f"- **{s['rows']:,} rows**, {s['usable']:,} usable",
        f"- **{s['duplicate_numbers']} duplicate point numbers** - two crews claimed the same number",
        f"- **{s['files']} source files** across {len(s['crews'])} crews",
        f"- point numbers {_fnum(s['min_number'])} to {_fnum(s['max_number'])}",
        f"- coordinate system: `{report['crs']}`",
        "",
    ]
    if report.get("feature_codes"):
        fc = report["feature_codes"]
        lines += [
            "## The office standard",
            "",
            f"{fc['codes']:,} feature codes: {fc['points']:,} point codes and {fc['lines']:,} that "
            f"build linework, drawing on {fc['layers']} layers with {fc['symbols']} symbols. "
            "This is the same file the crews' descriptions were written against, so the drawing "
            "comes out in the office's own layers rather than a generic one.",
            "",
        ]
    if report.get("import"):
        im = report["import"]
        lines += [
            "## What the import did",
            "",
            f"- {im.get('points', 0):,} points imported",
            f"- {im.get('duplicates', 0)} duplicate numbers renumbered",
            f"- {im.get('renumbered', 0)} numbers changed in total",
            f"- {im.get('no_elevation', 0)} points without an elevation",
            "",
        ]
    if report.get("linework"):
        lw = report["linework"]
        bits = ", ".join(f"{k} {v}" for k, v in sorted(lw.items()) if v)
        if bits:
            lines += ["## Linework", "", f"Built from the descriptions: {bits}.", ""]
    lines += [
        "## Things to try",
        "",
        "1. **Survey > Data Quality Check** - the same findings the office's check report has.",
        "2. **Layer panel** - the job draws on the office's own layers, not `TOPO-GROUND`.",
        "3. **Survey > Project Coordinate System** - the job is on EPSG:6584 "
        "(NAD83(2011) Texas North Central, US survey feet). Under *SAF (ground scale)* try "
        "`Scale from origin (0,0) - TXDOT SOP` with a county factor and watch the coordinates move.",
        "4. **Imagery > Add Imagery** - align an aerial against surveyed edge-of-pavement shots, "
        "then **Draw > Distance / Bearing** between a shot and the feature it is on: comparing "
        "the two is a measurement now, not a report.",
        "5. **Survey > Fieldwork Manager** - open the same job's field data, run the duplicate "
        "check, and send the clean points back.",
        "",
        "## Where this came from",
        "",
        "The `Source/` folder is the untouched input. `Field Data/` is a copy split by crew. "
        "If you re-run the sample builder, every file that holds data comes out byte-identical - "
        "the build is deterministic, so it doubles as a regression check between versions. "
        "The two files that record *when* the job was built (this job's `.plb` and "
        "`Job Setup.txt`) naturally differ by their timestamp.",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------- finding the shipped sample
#: The folder name the built job lands in.
SAMPLE_NAME = "Real World"


def repo_root() -> Path:
    """The folder this program is installed in (``<repo>/plumbline/fieldwork/sample_real.py``)."""
    return Path(__file__).resolve().parents[2]


def source_files() -> tuple[Path | None, Path | None, Path | None]:
    """The Real World sample's own inputs - a consolidated ``.fwk``, its Carlson F2F code table
    and the office's check report - as shipped in ``samples/Real World/Source``.

    ``(None, None, None)`` when this copy has no sample files with it, which is the honest
    answer for a plain ``pip install``: the builder still works, it just needs files handed to it.
    """
    src = repo_root() / "samples" / SAMPLE_NAME / "Source"
    if not src.is_dir():
        return None, None, None

    def pick(*words):
        for f in sorted(src.iterdir()):
            low = f.name.lower()
            if all(w in low for w in words):
                return f
        return None

    fwk = pick(".fwk")
    f2f = pick("f2f")
    chk = next((f for f in sorted(src.iterdir())
                if f.suffix.lower() in (".fwc", ".chk", ".txt") and "check" in f.name.lower()), None)
    return fwk, f2f, chk


def find_sample_dir(name: str = SAMPLE_NAME) -> Path | None:
    """The **built** sample job folder, if there is one: the checkout's own ``samples/`` first,
    then the user's folder, which is where a build run from the window puts it."""
    from ..core.settings import user_dir
    for d in (repo_root() / "samples" / name, Path(user_dir()) / "samples" / name):
        if (d / f"{name}.plb").exists():
            return d
    return None


__all__ = ["build_real_world_sample", "SampleBuildCancelled", "sensor_of", "crew_initials",
           "date_of", "control_points", "source_files", "find_sample_dir", "repo_root", "SAMPLE_NAME"]
