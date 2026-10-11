"""The seam between Fieldwork Manager and Plumbline.

Everything in here is Qt-free and has no opinion about user interfaces: it moves
data across the boundary in one direction (field data -> project) and answers
questions both halves need to ask.

    field data (raw download, .fwk, check report)
        |
        |  read_working_file / read_point_file      <- what the crew handed you
        v
    working rows  [OID, Pt#, N, E, Z, Desc, Parent, Source] + 7 spare columns
        |
        |  run_checks()                              <- duplicates, transpositions
        |  decode_crew()                             <- which crew shot this point
        v
    cleaned rows
        |
        |  batch_from_rows()                         <- ImportBatch
        v
    Plumbline project (points, then apply_feature_codes + process_linework)

Why the working-row list is the pivot
-------------------------------------
The 8-column list is the format Fieldwork Manager has always used internally and
it is also exactly what a consolidated .fwk file is on disk, so import and file
I/O are the same operation.  Column order is *positional* and must not be sorted:
source order carries meaning (a line's points must stay in the order the crew
shot them).  See ``natural_key`` in utils_sort for the display-side sorting rules.

Design rules carried over from Fieldwork Manager (do not "improve" these)
------------------------------------------------------------------------
* Round half up, never truncate: ``decimal.ROUND_HALF_UP`` via ``Decimal(str(x))``.
* Point numbers sort naturally with letters first; descriptions sort numbers
  first; N/E/Z sort truly numerically.  Never string-sort a number.
* Never renumber a point just because two numbers look similar - that is a
  *finding* for the check report, not an action.
"""
from __future__ import annotations

import csv
import csv as _csv
import math
import re
import tempfile
from collections import Counter, OrderedDict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ..core import audit as AUD
from ..core import provenance as PROV
from ..core import reference as REF
from ..core.featurecodes import ENTITY_TO_KIND, FeatureCode, FeatureCodeTable
from ..io.f2f import CATEGORY_LAYERS, convert_file as convert_f2f_file
from . import config as F
from . import detectors as D
from . import io_carlson as IC
from . import linecheck as LC
from . import parse as P

# --------------------------------------------------------------------------------------------- vocabulary
WORKING_COLUMNS = 8                 # the meaningful ones
WORKING_WIDTH = 15                  # .fwk files are written 15 wide (Corr_* metadata lives at 8..14)
OID, PTNUM, NOR, EAS, ELE, DESC, PARENT, SOURCE = range(8)

# ENTITY_TO_KIND (Carlson "Entity Type" -> Plumbline feature kind) is imported from
# core.featurecodes: it is a fact about feature codes, Plumbline's own Field-to-Finish converter
# needs it too, and two copies of a table like this is how the two halves start disagreeing.


# --------------------------------------------------------------------------------------------- numeric helpers
def to_float(value, default=None):
    """Parse a coordinate the way a surveyor's file writes it, or return *default*.

    Accepts '1,234.56' (thousands separator), ' 1234.5 ', '1234.5ft', '' and None.
    Never raises - a bad number in a field file is a finding, not a crash.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else default
    s = str(value).strip().replace(",", "").replace("'", "")
    if not s:
        return default
    m = re.match(r"^([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)", s)
    if not m:
        return default
    try:
        f = float(m.group(1))
    except ValueError:
        return default
    return f if math.isfinite(f) else default


def round_half_up(value: float, places: int = 3) -> float:
    """Round the way survey practice expects (2.5 -> 3), never banker's rounding.

    0.5 rounding in Python's built-in round() is half-to-even: round(2.5) == 2,
    which is not what a surveyor checking a coordinate by hand will get.
    """
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return value
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------------------------- working rows
def working_row(oid, pt, n, e, z, desc, parent="", source=""):
    """Build one 15-wide working row (the 7 spare columns are Corr_* metadata)."""
    return [str(oid), str(pt), str(n), str(e), str(z), str(desc), str(parent), str(source)] + [""] * 7


def row_xyz(row):
    """(n, e, z) as floats; any unparseable field comes back as None."""
    return to_float(row[NOR]), to_float(row[EAS]), to_float(row[ELE])


def row_is_usable(row) -> bool:
    """A row is usable only if it has a point number and a north/east pair."""
    if not row or len(row) < 5:
        return False
    if not str(row[PTNUM]).strip():
        return False
    n, e, _ = row_xyz(row)
    return n is not None and e is not None


def pad_row(row) -> list[str]:
    row = [str(c).strip() if c is not None else "" for c in row]
    if len(row) < WORKING_WIDTH:
        row = row + [""] * (WORKING_WIDTH - len(row))
    return row[:WORKING_WIDTH]


def read_working_file(path) -> list[list[str]] | None:
    """Read a consolidated field-data file (.fwk) or crew point file (.csv) into 15-wide working rows.

    Column count is not trusted: a 5-column file, an 8-column file and a
    15-column file with Corr_* metadata all arrive here and all leave as 15.
    Blank lines are dropped, order is preserved, encoding is utf-8-sig with a
    cp1252 fallback because that is what a Windows field tablet produces.
    """
    path = Path(path)
    last_error = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            rows: list[list[str]] = []
            with open(path, "r", encoding=enc, errors="strict", newline="") as fh:
                for idx, raw in enumerate(csv.reader(fh), start=1):
                    non_empty = [c for c in raw if str(c).strip()]
                    if not non_empty:
                        continue
                    # 5-column point CSV: [Point, Northing, Easting, Elevation, Description]
                    # Check that coordinates (raw[1] and raw[2]) are numeric to avoid misinterpreting code tables
                    if len(raw) == 5 and to_float(raw[1]) is not None and to_float(raw[2]) is not None:
                        pt = raw[0].strip()
                        nor = raw[1].strip()
                        eas = raw[2].strip()
                        ele = raw[3].strip()
                        desc = raw[4].strip()
                        rows.append(pad_row([str(idx), pt, nor, eas, ele, desc, "", path.name]))
                    else:
                        rows.append(pad_row(raw))
            return rows or None
        except UnicodeDecodeError as exc:               # try the next encoding
            last_error = exc
        except OSError as exc:
            raise IOError(f"could not read {path.name}: {exc}") from exc
    raise IOError(f"could not decode {path.name}: {last_error}")


def write_working_file(path, rows, parent="", source="", width=WORKING_WIDTH) -> int:
    """Write working rows as a headerless .fwk (what the Edit Fieldwork tab saves).

    ``width=8`` reproduces the classic consolidated file; the default 15 leaves room
    for the Corr_* columns the check stage writes back.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            r = pad_row(r)[:width]
            if parent and not r[PARENT]:
                r[PARENT] = parent
            if source and not r[SOURCE]:
                r[SOURCE] = source
            w.writerow(r)
            written += 1
    return written


def write_point_csv(path, rows) -> int:
    """Write standard 5-column point CSV: Point, Northing, Easting, Elevation, Description."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow([r[PTNUM], r[NOR], r[EAS], r[ELE], r[DESC]])
            written += 1
    return written


# --------------------------------------------------------------------------------------------- source-file sniffing
def read_point_file(path) -> tuple[list[list[str]], dict]:
    """Read *any* of the field-file flavours and return (working rows, info).

    Recognised: a consolidated .fwk (8 or 15 columns), a 5-column point CSV
    (Pt,N,E,Z,Desc or N,E,Z,Desc), and a Carlson F2F code table
    (handled by :func:`feature_codes_from_f2f`, not here).

    ``info["kind"]`` says which one it was, so the caller can tell the user what
    it just did rather than guessing from the file extension.
    """
    path = Path(path)
    rows = read_working_file(path)
    if not rows:
        return [], {"kind": "empty", "path": str(path)}

    first = rows[0]
    looks_headered = first[OID].strip().casefold() in ("code", "oid", "seq", "sequence", "#")

    if looks_headered:
        raise ValueError(f"{path.name} looks like a Carlson F2F code table, not a job file - "
                         f"use feature_codes_from_f2f() instead")

    non_empty = [r for r in rows if any(c for c in r)]

    # 4-column point list: N,E,Z in the first three numeric slots, no point number.
    if all(to_float(r[0]) is not None and to_float(r[1]) is not None for r in non_empty[:5]) \
            and not any(r[4] for r in non_empty[:20]) and (any(to_float(r[0]) > 1000 for r in non_empty[:5]) or all(not r[5] for r in non_empty[:20])):
        out = []
        for i, r in enumerate(non_empty, start=1):
            n, e, z = to_float(r[0]), to_float(r[1]), to_float(r[2])
            desc = r[3] if len(r) > 3 else ""
            if n is None or e is None:
                continue
            out.append(working_row(i, str(i), n, e, z if z is not None else "",
                                   desc, source=path.name))
        return out, {"kind": "points-5col", "path": str(path), "rows": len(out)}

    for i, r in enumerate(rows):                         # keep OIDs stable and numeric
        if not r[OID].strip():
            r[OID] = str(i + 1)
    return rows, {"kind": "working", "path": str(path), "rows": len(rows)}


# --------------------------------------------------------------------------------------------- crew decoding
def crew_of_point(point_number) -> int | None:
    """Which crew's number block this point number falls in (1-9), or None.

    Fieldwork Manager's rule: crew N owns N*10^k .. N*10^k+10^k-1 at every magnitude.
    So crew 7 owns 700-799, 7,000-7,999, 70,000-79,999 and so on, while 1-999 stays
    control and is shared by everyone.  This is how a downloaded file identifies its
    own crew without anybody typing a crew number.
    """
    n = None
    try:
        n = int(str(point_number).strip().split("-")[0].split("+")[0])
    except (TypeError, ValueError):
        base = re.sub(r"\d+$", "", str(point_number).strip())
        try:
            n = int(re.sub(r"\D", "", str(point_number)) or -1)
        except ValueError:
            return None
        n = n if base else n
    if n is None or n <= 0:
        return None
    if F.CONTROL_RANGE[0] <= n <= F.CONTROL_RANGE[1]:
        return None                                      # control: shared, no crew
    for crew in range(1, 10):
        if F.is_in_crew_block(str(n), crew):
            return crew
    return None


def crew_for_rows(rows) -> tuple[int | None, Counter]:
    """The crew a file belongs to, plus the vote count behind that answer.

    Returns (crew, votes).  ``crew`` is None when the file has no general points or
    the general points disagree, and the caller should ask rather than assume.
    """
    votes: Counter = Counter()
    for r in rows:
        if not row_is_usable(r):
            continue
        c = crew_of_point(r[PTNUM])
        if c:
            votes[c] += 1
    if not votes:
        return None, votes
    top, count = votes.most_common(1)[0]
    if len(votes) > 1 and count < 0.9 * sum(votes.values()):
        return None, votes                                 # genuinely mixed - do not guess
    return top, votes


def split_by_crew(rows) -> "OrderedDict[int | None, list[list[str]]]":
    """Split working rows into per-crew buckets, preserving order, None key last."""
    out: "OrderedDict[int | None, list[list[str]]]" = OrderedDict()
    for r in rows:
        c = crew_of_point(r[PTNUM]) if row_is_usable(r) else None
        out.setdefault(c, []).append(r)
    return OrderedDict(sorted(out.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))


def source_files(rows) -> list[str]:
    """Distinct source-file values, in the order they first appear."""
    seen: "OrderedDict[str, None]" = OrderedDict()
    for r in rows:
        s = r[SOURCE].strip() if len(r) > SOURCE else ""
        if s and s not in seen:
            seen[s] = None
    return list(seen)


def source_folder(source: str) -> str:
    """The dated crew folder a source file came from, e.g. '2026-7-22-AE'."""
    s = str(source or "").replace("/", "\\")
    return s.split("\\")[0].strip() if "\\" in s else ""


def folder_crew(rows) -> dict[str, int | None]:
    """crew number per *dated folder* - the unit a crew actually downloads.

    A file can contain nothing but shared control points, in which case the file on
    its own decodes to no crew; its folder-mates settle it.  That is why the crew is
    decided per folder and only then applied to the files inside it.
    """
    by_folder: dict[str, list[list[str]]] = {}
    for r in rows:
        by_folder.setdefault(source_folder(r[SOURCE] if len(r) > SOURCE else ""), []).append(r)
    return {k: crew_for_rows(v)[0] for k, v in by_folder.items()}


def source_file_crew(rows) -> dict[str, int | None]:
    """crew number per source file - what the Sample Real World folders are named from."""
    by_file: dict[str, list[list[str]]] = {}
    for r in rows:
        key = r[SOURCE].strip() if len(r) > SOURCE else ""
        by_file.setdefault(key, []).append(r)
    return {k: crew_for_rows(v)[0] for k, v in by_file.items()}


# --------------------------------------------------------------------------------------------- checks
def run_checks(rows, ne_tol: float | None = None, elev_tol: float | None = None) -> dict:
    """Run the three duplicate detectors.  Returns groups of row *indices*.

    exact  - same point number twice (two crews both claimed number 42)
    similar- numbers that differ by a transposition or a repeated digit (1234 / 1243)
    close  - different numbers, but within a foot or two of each other horizontally
    """
    exact = D.find_exact_duplicate_groups(rows)
    similar = D.find_similar_number_groups(rows)
    close = D.find_close_ne_groups(rows, tol=ne_tol or F.NE_TOLERANCE,
                                   elev_tol=elev_tol or F.ELEV_TOLERANCE)
    return {"exact": exact, "similar": similar, "close": close,
            "flagged_rows": sum(len(g) for g in exact) + sum(len(g) for g in similar),
            "report_rows": IC.build_check_report_rows(rows, exact, similar, close)}


def _row_is_removed(row) -> bool:
    """A removed row stays in a working/report file for audit, but is not an active code check."""
    if not row:
        return False
    # Consolidated .fwk metadata: Corr_Status is column 10 (0-based).
    if len(row) > 10 and str(row[10]).strip().casefold() in {"removed", "deleted"}:
        return True
    # Unified .fwc Description rows: DisplayTab, ... , Status, Comments.
    return (len(row) == 9 and str(row[1]).strip().casefold() == "description"
            and str(row[7]).strip().casefold() in {"removed", "deleted"})


def _point_is_deleted(point) -> bool:
    attrs = getattr(point, "attrs", None) or {}
    if not isinstance(attrs, dict):
        return False
    if any(attrs.get(key) is True for key in ("deleted", "is_deleted", "qa_deleted")):
        return True
    return any(str(attrs.get(key, "")).strip().casefold() in {"deleted", "removed"}
               for key in ("status", "fieldwork_status", "qa_status"))


def describe_flags(rows, f2f=None, fieldbook_path=None) -> dict[int, dict]:
    """Parse active descriptions and report the ones with flags, keyed by row OID."""
    out = {}
    for r in rows:
        if not row_is_usable(r) or _row_is_removed(r):
            continue
        raw_description = str(r[DESC] or "")
        # Empty descriptions need no vocabulary. Without an active code table, do not
        # classify non-empty text as UnknownCode, but still surface genuinely blank rows.
        if not f2f and raw_description.strip():
            continue
        parsed = P.parse_desc_field(raw_description, f2f or set(), fieldbook_path=fieldbook_path)
        if parsed.get("flags"):
            out[int(r[OID]) if str(r[OID]).strip().isdigit() else r[OID]] = parsed
    return out


def summarise(rows) -> dict:
    """Counts a human wants before deciding whether to import anything."""
    usable = [r for r in rows if row_is_usable(r)]
    bad = [r for r in rows if not row_is_usable(r)]
    nums = []
    for r in usable:
        try:
            nums.append(int(str(r[PTNUM]).strip()))
        except ValueError:
            pass
    crews = Counter(c for c in (crew_of_point(r[PTNUM]) for r in usable) if c)
    files = source_files(rows)
    return {"rows": len(rows), "usable": len(usable), "unusable": len(bad),
            "duplicate_numbers": len(usable) - len({str(r[PTNUM]).strip() for r in usable}),
            "crews": sorted(crews), "files": len(files), "source_files": files,
            "min_number": min(nums) if nums else None, "max_number": max(nums) if nums else None}


# --------------------------------------------------------------------------------------------- feature codes (Seam A)
def feature_codes_from_f2f(f2f_csv_path, existing: FeatureCodeTable | None = None,
                           include_unknown: bool = True) -> tuple[FeatureCodeTable, dict]:
    """Build a Plumbline feature-code table from a Carlson F2F code table.

    The conversion itself now lives in :mod:`plumbline.io.f2f` - Plumbline converts a
    Field-to-Finish table in its own right (*Survey > Convert Field to Finish*), and this is the
    field window's door onto the same code rather than a second implementation of it.  What the
    F2F columns mean is documented there; what this function promises is that the field window
    and the main window convert the same file to the same table.

    ``include_unknown`` is accepted and ignored: an entity type Plumbline does not know is
    always kept as a point and reported in the stats, which is what this door has always done.
    """
    return convert_f2f_file(f2f_csv_path, existing=existing)


def write_fieldbook_from_f2f(f2f_csv_path, dest_fwb, name: str = "", commands=None) -> Path:
    """Convert a Carlson F2F code table into a Fieldwork Manager field book (.fwb).

    The two files carry the same six facts - it is a header change, not a
    conversion - but the .fwb is the one that travels with a job and can hold the
    code commands and correction rules agreed for that job.
    """
    headers, rows, _unknown = IC.read_carlson_fieldbook(Path(f2f_csv_path))
    dest_fwb = Path(dest_fwb)
    dest_fwb.parent.mkdir(parents=True, exist_ok=True)
    ok = IC.write_fwb_file(dest_fwb, headers, rows,
                           commands=commands or list(F.LINE_COMMAND_DEFAULTS), rules=[])
    if not ok:
        raise IOError(f"could not write {dest_fwb}")
    return dest_fwb


def f2f_code_set(f2f_csv_path) -> set[str]:
    """The set of code strings (casefolded) a description parser validates against.

    Fieldwork Manager's parser wants a path to a .fwb; a Carlson F2F is a CSV with
    different headers, so it is converted through a temporary .fwb rather than
    reimplementing the reader.
    """
    headers, rows, _unknown = IC.read_carlson_fieldbook(Path(f2f_csv_path))
    with tempfile.TemporaryDirectory() as td:
        fwb = Path(td) / "codes.fwb"
        IC.write_fwb_file(fwb, headers, rows)
        return P.build_f2f_set_from_fieldbook(fwb)


# --------------------------------------------------------------------------------------------- Plumbline handoff
def batch_from_rows(rows, crs=None, layer_override: str | None = None,
                    drop_unusable: bool = True, code_table: FeatureCodeTable | None = None):
    """Turn working rows into a Plumbline ImportBatch of points.

    Descriptions are carried through **exactly as the crew wrote them**, because
    Plumbline interprets the active Field Book's line-control commands and string
    identifiers from the raw description at draw time. A second column keeps the raw text for the check
    report.  Elevations that are blank become NaN, not 0.0 - a point with no
    elevation must not drag a surface to sea level.

    Linework is deliberately *not* built here: ``Project.process_linework()``
    already does it from the code table, and duplicating that logic is how two
    programs start disagreeing about where a line goes.
    """
    from ..core.model import ImportBatch, NAN, SurveyPoint

    batch = ImportBatch()
    stats = {"points": 0, "skipped": 0, "no_elevation": 0, "duplicate_numbers": 0,
             "layer_override": layer_override or ""}
    seen: set[str] = set()
    when = PROV.now()
    for r in rows:
        if not row_is_usable(r):
            if drop_unusable:
                stats["skipped"] += 1
                continue
            continue
        n, e, z = row_xyz(r)
        num = str(r[PTNUM]).strip()
        desc = str(r[DESC]).strip()
        if num in seen:
            stats["duplicate_numbers"] += 1
        seen.add(num)
        if z is None:
            stats["no_elevation"] += 1

        layer = layer_override or "POINTS"
        if code_table is not None and desc:
            fc = code_table.get(desc) if hasattr(code_table, "get") else None
            if fc is not None and getattr(fc, "layer", ""):
                layer = fc.layer
        pt = SurveyPoint(id=0, number=num, x=float(e), y=float(n),
                         z=(NAN if z is None else float(z)), desc=desc, layer=layer)
        pt.attrs["fieldwork_source"] = str(r[SOURCE]).strip() if len(r) > SOURCE else ""
        pt.attrs["fieldwork_oid"] = str(r[OID]).strip()
        pt.attrs["fieldwork_raw_desc"] = desc
        # ... and the provenance the working file has always had (Parent Folder / Source File),
        # in one record the point list, the Properties panel and the check dock can all read.
        PROV.stamp_one(pt, file=str(r[SOURCE]).strip() if len(r) > SOURCE else "",
                       folder=str(r[PARENT]).strip() if len(r) > PARENT else "",
                       set=fieldwork_set(r), when=when)
        batch.points.append(pt)
        stats["points"] += 1
    batch.info["source"] = "fieldwork"
    return batch, stats


def apply_rows_to_project(project, rows, crs=None, code_table=None,
                          dup_policy: str = "renumber", layer_override: str | None = None) -> dict:
    """Import already-cleaned working rows into a live Plumbline project.

    This is the single call the Fieldwork Manager window makes when the user says
    "send this to Plumbline".  It returns a plain dict of counts so both the UI
    and tests can assert on it.
    """
    batch, stats = batch_from_rows(rows, layer_override=layer_override, code_table=code_table)
    if not batch.points:
        return {**stats, "applied": False, "reason": "no points"}
    if code_table is not None and getattr(code_table, "codes", None):
        # Prefer the field code table so the job draws in the office's own layers
        # rather than Plumbline's generic default standard.
        try:
            project.codes = code_table
        except Exception:
            pass
    result = project.apply_batch(batch, dup_policy=dup_policy, layer_override=layer_override)
    try:
        codes = project.apply_codes_to_points()
        stats["coded"] = codes.get("matched", 0) if isinstance(codes, dict) else 0
        unknown = codes.get("unknown", {}) if isinstance(codes, dict) else {}
        stats["unknown_codes"] = dict(sorted(unknown.items(), key=lambda kv: -kv[1])[:20])
        stats["unknown_code_points"] = sum(unknown.values())
    except Exception as exc:                             # a bad description must not lose the import
        stats["coded_error"] = str(exc)
    # What the points were at the moment they landed - the state the Point(s) Audit reads
    # against.  Written here, not in the window, so the sample job and any script that calls
    # this get the same record a user does.
    AUD.record(project, [project.points[i] for i in result.get("point_ids", ()) if i in project.points])
    stats.update({"applied": True, "duplicates": result.get("duplicates", 0),
                  "renumbered": result.get("renumbered", 0), "skipped": result.get("skipped", 0)})
    return stats


#: The description parser's flag names, said the way somebody would say them out loud.  The raw
#: name travels beside it (``flag``), because that is what a fix tool will switch on.
FLAG_TITLES = {
    "CommonConversionError": "Common error",
    "UnknownCode": "Unknown code",
    "EmptyDescription": "No description",
    "OrphanCommand": "Orphan command",
    "MisplacedAfterSeparator": "Potential code in descriptor",
    "SeparatorSpacingError": "Spacing at the separator",
    "LineOrderError": "Line command order",
    "ControlMismatch": "Control point mismatch",
}


def fieldwork_set(r) -> str:
    """How to name this import in one line: whose field data it is."""
    crew = crew_of_point(str(r[PTNUM]).strip()) if len(r) > PTNUM else ""
    return f"Cleaned field data (crew {crew})" if crew else "Cleaned field data"


def working_rows_from_project(project) -> tuple[list[list[str]], list[int]]:
    """The project's own field points as working rows, plus the point id behind each row.

    The drawing window's **Check Fieldwork** dock runs the same three detectors and the same
    description parser the field window runs, over the points that are actually in the project -
    so a check can be re-run after an edit without going back to the download.  Reference points
    are somebody else's coordinates and are left out (see :mod:`plumbline.core.reference`).
    """
    rows: list[list[str]] = []
    ids: list[int] = []
    for pid, p in project.points.items():
        if REF.is_reference(p):
            continue
        attrs = p.attrs or {}
        rec = PROV.of(p)
        # QA follows the live project description. The preserved import-time raw value is provenance,
        # not a reason to keep reporting an issue after the point has been corrected in the editor.
        desc = str(p.desc or "")
        rows.append(working_row(attrs.get("fieldwork_oid") or pid, p.number, p.y, p.x, p.z,
                                desc, rec["folder"], rec["file"]))
        ids.append(pid)
    return rows, ids


def _control_file_path(project, explicit=None) -> Path | None:
    """Find the job's standard control list, honoring an explicit project setting first."""
    settings = getattr(project, "settings", {}) or {}
    candidates = []
    for value in (explicit, settings.get("control_file"), settings.get("control_points_file")):
        if value:
            path = Path(str(value)).expanduser()
            if not path.is_absolute() and getattr(project, "path", None):
                path = Path(project.path).expanduser().parent / path
            candidates.append(path)

    roots = []
    if getattr(project, "path", None):
        roots.append(Path(project.path).expanduser().parent)
    data_folder = settings.get("data_folder")
    if data_folder:
        data_path = Path(str(data_folder)).expanduser()
        roots.append(data_path.parent if data_path.name.casefold() == "field data" else data_path)
    candidates.extend(root / "Control" / "Control Points.csv" for root in roots)
    candidates.extend(root / "Control Points.csv" for root in roots)
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _read_control_file(path: Path) -> list[dict]:
    """Read the standard Point/Northing/Easting/Elevation CSV without guessing its coordinates."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, errors="strict", newline="") as fh:
                reader = csv.DictReader(fh)
                headers = {re.sub(r"[^a-z0-9]+", "", str(name or "").casefold()): name
                           for name in (reader.fieldnames or [])}
                def column(*aliases):
                    return next((headers[key] for key in aliases if key in headers), None)

                number_col = column("point", "pointnumber", "pt", "ptno", "pointno", "number", "name")
                north_col = column("northing", "north", "n", "y")
                east_col = column("easting", "east", "e", "x")
                elev_col = column("elevation", "elev", "height", "z")
                if not number_col or not (north_col or east_col):
                    return []
                records = []
                for row in reader:
                    number = str(row.get(number_col) or "").strip()
                    if not number:
                        continue
                    north = to_float(row.get(north_col)) if north_col else None
                    east = to_float(row.get(east_col)) if east_col else None
                    elev = to_float(row.get(elev_col)) if elev_col else None
                    if north is None and east is None:
                        continue
                    records.append({"number": number, "northing": north, "easting": east,
                                    "elevation": elev})
                return records
        except UnicodeDecodeError:
            continue
        except (OSError, csv.Error):
            return []
    return []


def _control_records(project, control_file=None) -> tuple[list[dict], str]:
    """Prefer the job's control CSV; use explicitly imported CONTROL reference points as fallback."""
    path = _control_file_path(project, control_file)
    if path is not None:
        records = _read_control_file(path)
        if records:
            return records, str(path)

    records = []
    for point in REF.reference_points(project, "control"):
        records.append({"number": str(point.number).strip(), "northing": to_float(point.y),
                        "easting": to_float(point.x), "elevation": to_float(point.z)})
    return records, "CONTROL reference points" if records else ""


def _control_tolerance() -> float:
    """User-configured tolerance, applied in the stored units of each compared coordinate."""
    try:
        from ..core.settings import settings
        value = float(settings().get("control_point_tolerance", 0.01))
        return value if math.isfinite(value) and value >= 0 else 0.01
    except (TypeError, ValueError, OverflowError):
        return 0.01


def _compare_control_points(project, ids: list[int], tolerance: float, control_file=None) -> dict:
    """Compare survey points with matching control numbers; differences within tolerance are rounding."""
    controls, source = _control_records(project, control_file)
    control_by_number = {}
    for control in controls:
        key = str(control.get("number") or "").strip().casefold()
        if key:
            control_by_number.setdefault(key, control)

    comparisons = []
    mismatches = []
    epsilon = max(1e-12, abs(tolerance) * 1e-12)
    for row_index, point_id in enumerate(ids):
        point = project.points.get(point_id)
        if point is None:
            continue
        control = control_by_number.get(str(point.number).strip().casefold())
        if control is None:
            continue
        field = {"northing": to_float(point.y), "easting": to_float(point.x),
                 "elevation": to_float(point.z)}
        delta = {}
        for key in ("northing", "easting", "elevation"):
            expected = to_float(control.get(key))
            observed = field[key]
            if expected is None or observed is None:
                continue
            delta[key] = observed - expected
        if not delta:
            continue
        detail = {"point_id": point_id, "number": str(point.number),
                  "field": field, "control": {key: to_float(control.get(key))
                                               for key in ("northing", "easting", "elevation")},
                  "delta": delta}
        comparisons.append(detail)
        if any(abs(value) > tolerance + epsilon for value in delta.values()):
            mismatches.append({"row": row_index, **detail})

    units = "project coordinate units"
    return {"checked": bool(comparisons), "available": bool(control_by_number), "source": source,
            "tolerance": tolerance, "units": units,
            "compared": len(comparisons), "mismatches": mismatches}


def line_issues(project, f2f=None, fieldbook_path=None) -> list[dict]:
    """The incomplete lines in a project's own points - one implementation, both windows.

    The field window's Line Repair tab and this function call the same reader
    (:mod:`plumbline.fieldwork.linecheck`), so a line that the field window calls unclosed is
    unclosed here too.  Reference points are left out, as everywhere else.

    No vocabulary means no answer rather than a wrong one: a line is grouped by its code, so
    without the codes every description reads unknown, no code owns any command, and the check
    would report a clean job.  Callers pass what they have and say which of the two they did.
    """
    rows, _ids = working_rows_from_project(project)
    if not rows:
        return []
    return LC.detect_line_errors(rows, fieldbook_path=fieldbook_path, f2f_set=f2f)


#: Where a stored check report lives on a project (change order, item 13: the checks have to be
#: auditable after the project is closed and reopened, and the project file is what survives that).
REPORT_KEY = "check_report"


def fieldbooks_in(job_root) -> list:
    """Every field book under a job folder, the job's own name first.

    ``Field Book/*.fwb`` is where the program puts one, but an office's book is often somewhere
    else - a ``Source`` folder, alongside the download in ``Field Data``, or the folder root - and
    a check that cannot see the vocabulary reports a hundred false alarms.  Sorted so that a book
    named after the job wins over anything else, then by name, so the same job picks the same book
    every time it is opened.
    """
    if not job_root:
        return []
    root = Path(job_root)
    if not root.is_dir():
        return []
    try:
        books = [p for p in root.rglob("*.fwb") if p.is_file()]
    except OSError:
        return []
    stem = root.name.casefold()
    return sorted(books, key=lambda p: (0 if p.stem.casefold() == stem else
                                        1 if p.parent.name.casefold().startswith("field") else 2,
                                        p.name.casefold()))


def vocabulary_for(project, job_root=None, fieldbook=None) -> dict:
    """What the description check will be run against, and where it came from.

    Returns ``{"source", "label", "codes", "path", "why"}``:

    * ``field book`` - the job's own (``project.settings["fieldbook_file"]``, else the best
      ``*.fwb`` under the job folder): the whole check;
    * ``field book + job codes`` - a book with no code table in it (a fresh job's template book is
      exactly that) merged with the job's own feature codes;
    * ``job codes`` - the codes converted from the office's Carlson table, or the built-in set;
    * ``none`` - no code vocabulary: the number and blank-description checks still run, while
      non-empty code validation is paused and the caller says so. An empty vocabulary is never run
      as if it were a vocabulary, because every code would come back unknown and a hundred false
      alarms is how a check gets switched off and stays off.
    """
    from . import parse as P

    root = Path(job_root).expanduser() if job_root else None
    if root is None and getattr(project, "path", None):
        root = Path(project.path).expanduser().parent

    def resolve_book_path(value):
        if not value:
            return None
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute() and root is not None:
            candidate = root / candidate
        return candidate

    p = resolve_book_path(fieldbook)
    if p is None or not p.is_file():
        chosen = resolve_book_path((project.settings or {}).get("fieldbook_file"))
        if chosen is not None and chosen.is_file():
            p = chosen
        else:
            books = fieldbooks_in(root)
            p = books[0] if books else None
    # The job's own codes count as a vocabulary only when they came from the office's table (a
    # Carlson Field-to-Finish export, converted through Survey > Convert Field to Finish).  The
    # program's built-in default codes are not a vocabulary for this job: they are a starting set,
    # and running them as one would flag every office code the job actually uses.
    job_source = (project.settings or {}).get("f2f_path") or ""
    job_codes = ({str(c).casefold() for c in getattr(getattr(project, "codes", None), "codes", {}) or {}}
                 if job_source else set())
    codes: set = set()
    label, source, why = "", "none", ""
    if p is not None and p.exists():
        try:
            codes = set(P.build_f2f_set_from_fieldbook(p))
        except Exception as ex:                                  # a book that cannot be read
            why = f"{p.name} could not be read: {ex}"
            codes = set()
        if codes:
            label, source = p.name, "field book"
    if job_codes:
        had_book_codes = bool(codes)
        codes = set(codes) | job_codes
        if not why:                              # a readable book or no book at all
            if had_book_codes:
                label, source = f"{p.name} + {Path(str(job_source)).name}", "field book + job codes"
            else:
                label, source = Path(str(job_source)).name, "job codes"
    return {"source": source if codes else "none", "label": label, "codes": codes,
            "path": str(p) if p is not None else "", "why": why,
            "job_codes": len(job_codes)}


def store_report(project, result: dict, *, vocabulary: dict | None = None, job_root=None,
                 note: str = "") -> dict:
    """Write the run into the project, so reopening the project reopens the check (item 13).

    Stored on the project - not only in a file - because the project is what is reopened, and a
    check report that has to be found before it can be read is a check report that is not read.
    The findings keep the point **numbers** as well as the ids: a renumbered import changes ids,
    and a report that says "points 12, 14, 15" is the one a person can act on.  A ``.fwc`` file is
    written next to the project as well when the caller wants one (``write_report``), so the same
    run can be handed to the field window or kept with the job.
    """
    rows = result.get("rows") or []
    ids = result.get("ids") or []
    num_of = {}
    for i, pid in enumerate(ids):
        p = project.points.get(pid)
        if p is not None and i < len(rows):
            num_of[pid] = str(p.number)
    findings = []
    for f in result.get("findings") or []:
        row_ids = [ids[i] for i in (f.get("rows") or []) if 0 <= i < len(ids)]
        findings.append({"level": f.get("level", "info"), "check": f.get("check", ""),
                         "message": f.get("message", ""), "count": len(row_ids),
                         "points": [row_ids[i] for i in range(min(len(row_ids), 500))],
                         "numbers": [num_of.get(i, "") for i in row_ids[:500]]})
    vocab = vocabulary or {}
    payload = {"when": _now(), "points": len(rows), "crews": (result.get("stats") or {}).get("crews") or [],
               "findings": findings,
               "errors": sum(1 for f in findings if f["level"] == "error"),
               "warnings": sum(1 for f in findings if f["level"] == "warn"),
               "vocabulary": vocab.get("source", "none"), "vocabulary_label": vocab.get("label", ""),
               "vocabulary_codes": len(vocab.get("codes") or ()), "note": note}
    project.settings[REPORT_KEY] = payload
    return payload


def stored_report(project) -> dict | None:
    """The last stored check on this project, or None."""
    rep = (project.settings or {}).get(REPORT_KEY)
    return rep if isinstance(rep, dict) and rep.get("when") else None


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def report_rows(project, result: dict, project_crs_label: str = "") -> list:
    """The stored run as unified ``.fwc`` rows (the field window's own report format)."""
    from . import io_carlson as ICO

    rows = result.get("rows") or []
    ids = result.get("ids") or []
    found = {"exact": [], "similar": [], "close": []}
    for f in result.get("findings") or []:
        name = (f.get("check") or "").lower()
        idxs = [i for i in (f.get("rows") or []) if 0 <= i < len(rows)]
        if not idxs:
            continue
        if "duplicate" in name:
            found["exact"].append(sorted(idxs))
        elif "look-alike" in name:
            found["similar"].append(sorted(idxs))
        elif "top of each other" in name or "close points" in name or "close" in name:
            found["close"].append(sorted(idxs))
    out = ICO.build_check_report_rows(rows, found["exact"], found["similar"], found["close"])
    # Description flags, one row per flagged OID (the same shape the field window writes).
    gid = (max([int(r[0]) for r in out if str(r[0]).isdigit()] or [0]) + 1)
    flagged = {}
    for f in result.get("findings") or []:
        flag = f.get("flag")
        if not flag:
            continue
        for i in (f.get("rows") or []):
            if 0 <= i < len(rows):
                flagged.setdefault(str(rows[i][0]), {"flags": flag, "detail": f.get("message", ""),
                                                     "issue": flag}) 
    for oid, rec in flagged.items():
        out.append([str(gid), "Description", rec["issue"], oid, rec["flags"], rec["detail"],
                    rec["detail"], "Open", ""])
        gid += 1
    # Line issues, read by the same code the field window's Line Repair tab uses.
    for i, issue in enumerate(result.get("line_issues") or [], start=gid):
        out.append([str(i), "Line", issue.get("issue", ""), str(issue.get("oid", "")),
                    issue.get("flags", ""), issue.get("detail", ""), issue.get("detail", ""),
                    "Open", ""])
    return out


def write_report(path, rows) -> bool:
    """Write a unified ``.fwc`` (``True`` on success).  The file is optional; the project is not."""
    from . import io_carlson as ICO
    return ICO.write_unified_report(Path(path), rows)


def check_project(project, f2f=None, fieldbook_path=None, ne_tol: float | None = None,
                  elev_tol: float | None = None, control_file=None) -> dict:
    """Run the field-data checks over a project's own points, as findings a dock can show.

    *f2f* is the field book's code set and *fieldbook_path* the file it came from (the parser
    also reads that job's command set and correction rules out of it). Without a code vocabulary,
    non-empty code descriptions are not classified as unknown; genuinely blank descriptions still
    surface, and an available Field Book command set can still validate line-command order.

    Returns {"rows", "ids", "findings", "stats", "flags", "code_checks"}.  Row indices in a
    finding index into "rows"; ``ids[i]`` is the project point the i-th row came from.
    """
    rows, ids = working_rows_from_project(project)
    # A caller may know the Field Book path but omit its decoded code set. Load the
    # vocabulary here so common-conversion rules and unknown codes still get checked.
    if not f2f and fieldbook_path:
        try:
            f2f = P.build_f2f_set_from_fieldbook(fieldbook_path)
        except Exception:
            f2f = set()
    f2f = set(f2f or ())
    out = {"rows": rows, "ids": ids, "findings": [], "stats": summarise(rows) if rows else {},
           "flags": {}, "code_checks": bool(f2f)}
    if not rows:
        out["findings"].append({"level": "info", "check": "no field points", "rows": [],
                                "message": "The project has no field points to check."})
        return out
    found = run_checks(rows, ne_tol=ne_tol, elev_tol=elev_tol)
    # Blank descriptions are checked even without a code table; describe_flags deliberately
    # leaves non-empty text alone when no vocabulary is available.
    flags = describe_flags(rows, f2f, fieldbook_path=fieldbook_path)
    # Keep single-point command-order findings in the project-side report as well as the
    # Fieldwork Manager's cross-point sequence findings. The row-level pass checks each code's
    # command group in a multicode description independently, without mixing groups together.
    if f2f or fieldbook_path:
        try:
            order_flags = P._validate_line_command_order(rows, f2f, fieldbook_path=fieldbook_path)
            order_details = getattr(P._validate_line_command_order, "details", {})
        except Exception:
            order_flags, order_details = {}, {}
        raw_by_oid = {str(row[OID]): str(row[DESC] or "") for row in rows}
        for oid, row_flags in order_flags.items():
            key = int(oid) if str(oid).strip().isdigit() else oid
            parsed = flags.get(key)
            if parsed is None:
                parsed = {"raw": raw_by_oid.get(str(oid), ""), "flags": [], "flag_detail": ""}
                flags[key] = parsed
            parsed_flags = parsed.setdefault("flags", [])
            for flag in row_flags:
                if flag not in parsed_flags:
                    parsed_flags.append(flag)
            details = order_details.get(oid, [])
            if details:
                existing_detail = str(parsed.get("flag_detail") or "")
                parsed["flag_detail"] = "; ".join([existing_detail, *details]).strip("; ")
    deleted_oids = set()
    for pid, point in project.points.items():
        if _point_is_deleted(point):
            attrs = point.attrs or {}
            deleted_oids.add(str(attrs.get("fieldwork_oid") or pid))
    for oid in list(flags):
        if str(oid) in deleted_oids:
            flags.pop(oid, None)
    oid_to_row = {str(r[OID]): i for i, r in enumerate(rows)}

    def rows_of(groups):
        return sorted({i for g in groups for i in g})

    exact, similar, close = found["exact"], found["similar"], found["close"]
    if exact:
        out["findings"].append({
            "level": "error", "check": "duplicate numbers", "rows": rows_of(exact), "groups": exact,
            "message": f"{len(exact)} point number(s) are used more than once - "
                       f"{_group_words(rows, exact)}."})
    if similar:
        out["findings"].append({
            "level": "warn", "check": "look-alike numbers", "rows": rows_of(similar), "groups": similar,
            "message": f"{len(similar)} group(s) of numbers differ by a transposition or a "
                       f"repeated digit - {_group_words(rows, similar)}."})
    if close:
        out["findings"].append({
            "level": "warn", "check": "Close Points", "rows": rows_of(close), "groups": close,
            "message": f"{len(close)} group(s) of different numbers are within the closeness "
                       f"tolerance of each other - {_group_words(rows, close)}."})
    if flags:
        by_flag: dict[str, dict] = {}
        for oid, parsed in flags.items():
            i = oid_to_row.get(str(oid))
            for flag in parsed.get("flags") or ["Description"]:
                name = str(flag).split(":")[0]
                rec = by_flag.setdefault(name, {"rows": set(), "sample": ""})
                if i is not None:
                    rec["rows"].add(i)
                if not rec["sample"]:
                    rec["sample"] = str(parsed.get("raw", "") or "")
        flag_rows = sorted(by_flag.items(), key=lambda kv: (-len(kv[1]["rows"]), kv[0]))
        common_errors = [item for item in flag_rows if item[0] == "CommonConversionError"]
        if common_errors:
            other_flags = [item for item in flag_rows if item[0] != "CommonConversionError"]
            unknown_at = next((index for index, (name, _rec) in enumerate(other_flags)
                               if name == "UnknownCode"), len(other_flags))
            flag_rows = other_flags[:unknown_at] + common_errors + other_flags[unknown_at:]
        for name, rec in flag_rows:
            title = FLAG_TITLES.get(name, name)
            lvl = "error" if name in {
                "UnknownCode", "OrphanCommand", "SeparatorSpacingError", "LineOrderError"
            } else "warn"
            out["findings"].append({
                "level": lvl, "check": title, "flag": name, "rows": sorted(rec["rows"]),
                "message": f"{len(rec['rows'])} description(s) flagged {title.lower()}"
                           + (f" (e.g. {rec['sample']!r})" if rec["sample"] else "") + "."})
    out["flags"] = flags

    # Control comparisons use the project's standard Control/Control Points.csv (or imported
    # CONTROL reference points). Differences at or below the configured tolerance are treated as
    # survey rounding; only larger offsets are surfaced as a warning.
    tolerance = _control_tolerance()
    control_comparison = _compare_control_points(project, ids, tolerance, control_file)
    out["control_comparison"] = control_comparison
    control_mismatches = control_comparison["mismatches"]
    if control_mismatches:
        examples = ", ".join(str(item["number"]) for item in control_mismatches[:5])
        out["findings"].append({
            "level": "warn", "check": "Control point mismatch", "flag": "ControlMismatch",
            "rows": sorted({item["row"] for item in control_mismatches}),
            "control_comparisons": control_mismatches,
            "tolerance": tolerance, "units": control_comparison["units"],
            "message": (f"{len(control_mismatches)} survey point(s) differ from matching control "
                        f"coordinates by more than {tolerance:g} {control_comparison['units']}; "
                        "differences at or below the tolerance are treated as rounding"
                        + (f" (for example, point(s) {examples})" if examples else "") + "."),
        })

    # The linework half: issues that belong to a whole line, not to one point.  Same rule as the
    # description check - it runs when there is a vocabulary, and the dock says which of the two it did.
    out["line_issues"] = []
    if f2f or fieldbook_path:
        try:
            found_lines = LC.detect_line_errors(rows, fieldbook_path=fieldbook_path, f2f_set=f2f)
        except Exception as ex:                      # a check must never break the window
            out["line_error"] = str(ex)
            found_lines = []
        out["line_issues"] = found_lines
        by_line: dict[str, dict] = {}
        for issue in found_lines:
            name = str(issue.get("issue_type", "line"))
            rec = by_line.setdefault(name, {"rows": set(), "samples": []})
            i = oid_to_row.get(str(issue.get("oid", "")))
            if i is not None:
                rec["rows"].add(i)
            if len(rec["samples"]) < 2:
                rec["samples"].append(str(issue.get("detail", "")))
        for name in [t for t in LC.ISSUE_TYPES if t in by_line]:
            rec = by_line[name]
            code = LC.ISSUE_FLAGS.get(name, name)
            out["findings"].append({
                "level": "error" if name == "Line Order" else "warn",
                "check": f"line: {name.lower()}", "flag": code,
                "rows": sorted(rec["rows"]),
                "message": f"{len(rec['rows'])} field point(s) in line(s) with {name.lower()} - "
                           + " ".join(rec["samples"][:1]) + ".",
            })
    if not out["findings"]:
        out["findings"].append({"level": "info", "check": "ok", "rows": [],
                                "message": "No problems found in the field data."})
    return out


def _group_words(rows, groups) -> str:
    """The first few groups, said in point numbers: "2 & 5, 12 & 21, ..."."""
    bits = []
    for g in groups[:3]:
        nums = [str(rows[i][PTNUM]).strip() for i in g if 0 <= i < len(rows)]
        bits.append(" & ".join(nums[:4]))
    if len(groups) > 3:
        bits.append("...")
    return ", ".join(bits)


__all__ = [
    "WORKING_WIDTH", "OID", "PTNUM", "NOR", "EAS", "ELE", "DESC", "PARENT", "SOURCE",
    "ENTITY_TO_KIND", "to_float", "round_half_up",
    "working_row", "row_xyz", "row_is_usable", "pad_row",
    "read_working_file", "write_working_file", "read_point_file",
    "crew_of_point", "crew_for_rows", "split_by_crew", "source_files", "source_file_crew",
    "source_folder", "folder_crew",
    "run_checks", "describe_flags", "summarise", "line_issues",
    "feature_codes_from_f2f", "f2f_code_set", "write_fieldbook_from_f2f",
    "batch_from_rows", "apply_rows_to_project",
    "working_rows_from_project", "check_project", "fieldwork_set",
]
