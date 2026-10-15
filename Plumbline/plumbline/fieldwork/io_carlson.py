# io_carlson.py — Carlson F2F, .fwb, .fwk, .chk unified/legacy
import csv, re
from pathlib import Path
from collections import defaultdict
import math
from .config import carlson_entity_types, CHECK_REPORT_HEADERS, UNIFIED_REPORT_HEADERS, LEGACY_CHECK_HEADERS, DESC_PARSE_HEADERS, NE_TOLERANCE, ELEV_TOLERANCE
from .detectors import _working_row_to_float, _bearing_dms, _format_dms, _cardinal_direction

def read_carlson_fieldbook(file_path: Path):
    """Read a fixed-format Carlson Field Book CSV and prepare .fwb rows.

    Keeps source columns Code(0), Description(1), Symbol(2), Layer(4),
    and Entity Type(5) — Entity Type is mapped through
    carlson_entity_types to a word (\"Point\" etc.). Appends a Category
    column driven by \"Category,<name>\" group rows. Blank rows are
    skipped. Source order is preserved (never sorted) so category
    grouping stays intact. Header row is always expected (blank row 2 is
    harmless — it is skipped).

    Returns (headers, rows, unknown_entity_codes).
    Headers are [Code, Description, Symbol, Layer, Entity Type, Category]
    drawn from the source header names so the .fwb keeps the same words.
    """
    # Code, Description, Symbol, Layer, Entity Type (Carlson order 0,1,2,4,5)
    selected_columns = (0, 1, 2, 4, 5)
    required_column_count = 6

    fieldbook_headers = []
    fieldbook_rows = []
    unknown_entity_codes = set()
    active_category = "Default"

    try:
        with open(file_path, "r", encoding="utf-8-sig",
                  errors="ignore", newline="") as file:
            reader = csv.reader(file)

            source_headers = next(reader, None)
            if source_headers is None:
                return fieldbook_headers, fieldbook_rows, unknown_entity_codes

            source_headers = [cell.strip() for cell in source_headers]
            if len(source_headers) < required_column_count:
                raise ValueError(
                    f"Field book needs at least {required_column_count} columns: "
                    f"{file_path.name}"
                )

            fieldbook_headers = [source_headers[i] for i in selected_columns]
            fieldbook_headers.append("Category")

            for raw_row in reader:
                row = [cell.strip() for cell in raw_row]

                if not any(row):
                    continue

                if len(row) < required_column_count:
                    row.extend([""] * (required_column_count - len(row)))

                if row[0].casefold() == "category":
                    active_category = row[1]
                    continue

                entity_code = row[5]
                entity_type = carlson_entity_types.get(entity_code, entity_code)
                if entity_code and entity_code not in carlson_entity_types:
                    unknown_entity_codes.add(entity_code)

                fieldbook_rows.append([
                    row[0],        # Code
                    row[1],        # Description
                    row[2],        # Symbol
                    row[4],        # Layer
                    entity_type,   # Entity Type as word
                    active_category,
                ])

    except (OSError, csv.Error, ValueError) as error:
        print(f"Failed to read Carlson Field Book {file_path}: {error}")

    return fieldbook_headers, fieldbook_rows, unknown_entity_codes


def write_fwb_file(dest_path: Path, headers, rows, rules=None, commands=None, extra_fields=None):
    """Write a headered .fwb CSV (never sorted, UTF-8). Returns True/False.

    ``commands`` may be an ordered token list or a meaning-to-token mapping. Unknown
    ``#EXTRA_JSON`` keys are kept when editing an existing book via ``write_fwb_extra``.
    """
    import json
    try:
        with open(dest_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
            extras = dict(extra_fields or {})
            if commands is not None:
                extras["commands"] = commands
            if rules is not None:
                extras["rules"] = rules
            if extras:
                f.write(f"#EXTRA_JSON {json.dumps(extras, ensure_ascii=False)}\n")
    except OSError as e:
        print(f"Write .fwb failed {dest_path}: {e}")
        return False
    return True

def _read_fwb_extra_raw(file_path: Path):
    """Read the un-normalized metadata so edits do not discard future/office-specific keys."""
    import json
    extras = {}
    try:
        sidecar = Path(str(file_path) + ".meta.json")
        if sidecar.exists():
            with open(sidecar, "r", encoding="utf-8") as sf:
                data = json.load(sf)
                if isinstance(data, dict):
                    extras.update(data)
    except Exception:
        pass
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#EXTRA_JSON"):
                    try:
                        data = json.loads(line[len("#EXTRA_JSON"):].strip())
                        if isinstance(data, dict):
                            extras.update(data)  # embedded metadata takes precedence
                    except Exception:
                        pass
                elif line.startswith("#RULES_JSON"):
                    try:
                        extras["rules"] = json.loads(line[len("#RULES_JSON"):].strip())
                    except Exception:
                        pass
                elif line.startswith("#COMMANDS_JSON"):
                    try:
                        extras["commands"] = json.loads(line[len("#COMMANDS_JSON"):].strip())
                    except Exception:
                        pass
    except Exception:
        pass
    return extras

def read_fwb_extra(file_path: Path):
    """Read active Field Book command meanings and correction rules from ``#EXTRA_JSON``.

    Earlier files used ``commands``/``rules``; accept the longer descriptive keys too,
    including a meaning-to-token command map, while returning the canonical keys used by
    the parser and dialogs.
    """
    from ..core.fieldbook_syntax import DEFAULT_COMMAND_TOKENS
    raw = _read_fwb_extra_raw(Path(file_path))
    commands = raw.get("commands")
    if commands is None:
        for alias in ("commands_meanings", "command_meanings", "meanings"):
            if raw.get(alias) is not None:
                commands = raw[alias]
                break
    rules = raw.get("rules")
    if rules is None:
        rules = raw.get("correction_rules", raw.get("correctionRules", []))
    return {**raw,
            "commands": commands if commands is not None else list(DEFAULT_COMMAND_TOKENS),
            "rules": rules if isinstance(rules, list) else []}

def write_fwb_extra(file_path: Path, rules=None, commands=None):
    """Update embedded Field Book metadata and preserve all code rows and unknown JSON keys."""
    headers, rows = read_fwb_file(file_path)
    if headers is None:
        return False
    raw = _read_fwb_extra_raw(Path(file_path))
    current = read_fwb_extra(Path(file_path))
    if rules is None:
        rules = current.get("rules", [])
    if commands is None:
        commands = current.get("commands")
    extras = dict(raw)
    # Remove stale aliases so the canonical values cannot be shadowed by old metadata.
    for alias in ("commands_meanings", "command_meanings", "meanings", "correction_rules", "correctionRules"):
        extras.pop(alias, None)
    return write_fwb_file(file_path, headers, rows, rules=rules, commands=commands,
                          extra_fields=extras)


def read_fwb_file(file_path: Path):
    """Read a headered .fwb file. Returns (headers, rows) or (None, None) on fail.

    Rows beyond 6 columns are truncated; short rows are padded. Header row
    is consumed (not returned as data). Blank rows and #EXTRA_JSON comment lines are skipped.
    """
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            # Pre-filter comment lines before csv parsing to keep simple
            lines = []
            for line in f:
                if line.lstrip().startswith("#EXTRA_JSON") or line.lstrip().startswith("#RULES_JSON") or line.lstrip().startswith("#COMMANDS_JSON"):
                    continue
                # Also skip sidecar marker lines
                if line.lstrip().startswith("#"):
                    # Generic comment — skip if it looks like JSON extra, else treat as not data
                    # We skip any line starting with # to avoid polluting fieldbook rows
                    if "EXTRA_JSON" in line or "RULES" in line:
                        continue
                    # For other # comments, skip as well (not part of fieldbook data)
                    if line.lstrip().startswith("#"):
                        continue
                lines.append(line)
            import io
            reader = csv.reader(io.StringIO("".join(lines)))
            headers = next(reader, None)
            if headers is None:
                return None, None
            headers = [h.strip() for h in headers]
            rows = []
            for raw in reader:
                row = [c.strip() for c in raw]
                if not any(row):
                    continue
                # Skip if row is actually a comment that slipped through
                if row[0].startswith("#"):
                    continue
                if len(row) < 6:
                    row.extend([""] * (6 - len(row)))
                rows.append(row[:6])
            return headers, rows
    except (OSError, csv.Error) as e:
        print(f"Read .fwb failed {file_path}: {e}")
        return None, None


# ----- Check Report detectors (OID-referenced, detection only) -----
# Units: US Survey Feet definitive (CONUS, pseudo-Euclidean: N=X, E=Y per user).

def normalize_point_number(pn: str) -> str:
    """Trim + casefold for exact grouping. Empty -> ''."""
    if pn is None:
        return ""
    return str(pn).strip().casefold()


def point_number_core(pn: str) -> str:
    """Core numeric part: strip leading/trailing letters (A-Z) case-insensitively.

    Examples: 'a602'->'602', '602a'->'602', '602'->'602', '53602a'->'53602',
    '6a'->'6', 'PRS843728484041'->'843728484041'. Returns '' if no digits.
    The remaining core is casefolded for grouping.
    """
    if pn is None:
        return ""
    s = str(pn).strip()
    if s == "":
        return ""
    # Remove leading letters
    s = re.sub(r'^[A-Za-z]+', '', s)
    # Remove trailing letters
    s = re.sub(r'[A-Za-z]+$', '', s)
    s = s.strip()
    return s.casefold()


def _working_row_to_float(value):
    try:
        return float(str(value).strip()) if str(value).strip() != "" else None
    except (ValueError, TypeError):
        return None


def find_exact_duplicate_groups(working_rows):
    """Exact point-number groups (case-insensitive trimmed).

    working_rows: list of 8-col lists [OID, PtNum, N, E, Z, Desc, Parent, Source].
    Returns list of groups, each group is list of row indices into working_rows
    where normalized point number repeats (len>1). Sorted by first OID.
    """
    buckets = defaultdict(list)
    for idx, row in enumerate(working_rows):
        pn = row[1] if len(row) > 1 else ""
        norm = normalize_point_number(pn)
        if norm == "":
            continue
        buckets[norm].append(idx)
    groups = [idxs for idxs in buckets.values() if len(idxs) > 1]
    # Canonical order: by smallest OID numeric
    def group_key(idxs):
        try:
            return min(int(working_rows[i][0]) for i in idxs if str(working_rows[i][0]).isdigit())
        except Exception:
            return idxs[0]
    groups.sort(key=group_key)
    return groups


def find_similar_number_groups(working_rows):
    """Suggestive similar-number groups: same core after stripping edge letters,
    but at least 2 distinct normalized point numbers in the group.

    Excludes pure-exact-only groups (distinct count ==1). Allows overlap with
    exact groups — a point may appear in both Exact and Similar reports.
    """
    buckets = defaultdict(list)
    for idx, row in enumerate(working_rows):
        pn = row[1] if len(row) > 1 else ""
        norm = normalize_point_number(pn)
        if norm == "":
            continue
        core = point_number_core(pn)
        if core == "":
            continue
        # Core that is purely numeric/alpha? Keep any non-empty core.
        buckets[core].append(idx)
    groups = []
    for core, idxs in buckets.items():
        if len(idxs) < 2:
            continue
        distinct = {normalize_point_number(working_rows[i][1]) for i in idxs}
        if len(distinct) < 2:
            continue
        # Core equality already groups; distinct ensures variant decor.
        groups.append(idxs)
    def group_key(idxs):
        try:
            return min(int(working_rows[i][0]) for i in idxs if str(working_rows[i][0]).isdigit())
        except Exception:
            return idxs[0]
    groups.sort(key=group_key)
    return groups


def find_close_ne_groups(working_rows, tol=NE_TOLERANCE, elev_tol=ELEV_TOLERANCE):
    """Spatial near-duplicates: horizontal distance on N(X)/E(Y) ≤ tol and elev diff ≤ elev_tol.

    Horizontal is pseudo-Euclidean sqrt(dN^2+dE^2) (USft). Elevation is separate diff |dZ| ≤ elev_tol.
    Builds adjacency where both conditions hold, then connected components. Keeps only components
    where ≥2 distinct point numbers (different numbers to merge).

    Returns list of groups (list of indices). Sorted by smallest OID.
    """
    n = len(working_rows)
    if n < 2:
        return []
    # Pre-parse floats; skip rows with missing N/E
    coords = []  # (n, e, z)
    valid_idx = []
    for idx, row in enumerate(working_rows):
        n_val = _working_row_to_float(row[2] if len(row) > 2 else "")
        e_val = _working_row_to_float(row[3] if len(row) > 3 else "")
        z_val = _working_row_to_float(row[4] if len(row) > 4 else "")
        if n_val is None or e_val is None:
            continue
        coords.append((n_val, e_val, z_val))
        valid_idx.append(idx)
    m = len(valid_idx)
    if m < 2:
        return []
    parent = list(range(m))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    tol2 = tol * tol
    order = sorted(range(m), key=lambda i: coords[i][0])
    for pos_a, a in enumerate(order):
        na, ea, za = coords[a]
        for pos_b in range(pos_a + 1, m):
            b = order[pos_b]
            nb, eb, zb = coords[b]
            dN = nb - na
            if dN > tol:
                break
            if dN < -tol:
                continue
            dE = eb - ea
            if abs(dE) > tol:
                continue
            if dN * dN + dE * dE > tol2 + 1e-9:
                continue
            # Elev diff check (if both have elev)
            if za is not None and zb is not None:
                if abs(zb - za) > elev_tol + 1e-9:
                    continue
            # If one elev missing, treat as pass (horizontal only)
            union(a, b)

    # Collect components
    comps = defaultdict(list)
    for i in range(m):
        r = find(i)
        comps[r].append(valid_idx[i])

    groups = []
    for members in comps.values():
        if len(members) < 2:
            continue
        distinct = {normalize_point_number(working_rows[idx][1]) for idx in members}
        # Keep only groups where different point numbers to merge
        if len(distinct) < 2:
            continue
        groups.append(sorted(members, key=lambda idx: int(working_rows[idx][0]) if str(working_rows[idx][0]).isdigit() else idx))
    groups.sort(key=lambda idxs: min(int(working_rows[i][0]) for i in idxs if str(working_rows[i][0]).isdigit()) if any(str(working_rows[i][0]).isdigit() for i in idxs) else idxs[0])
    return groups

# Alias for older callers / tests
find_close_xy_groups = find_close_ne_groups


# ----- Check report file (headered .fwc (.chk legacy), lean CSV, 1 list) -----
CHECK_REPORT_HEADERS = [
    "GroupID", "IssueType", "OID", "PointNumber", "Northing", "Easting", "Elevation",
    "Description", "ParentFolder", "SourceFile", "Detail", "Status", "Comments"
]
# Unified OID-minimal report — single file for both tabs, DisplayTab routed, live-pull raw via OID
UNIFIED_REPORT_HEADERS = [
    "GroupID", "DisplayTab", "IssueType", "OID", "Flags", "Detail", "FlagDetail", "Status", "Comments"
]
# Legacy 13-col headers kept for reading old .fwc/.chk files (compat)
LEGACY_CHECK_HEADERS = CHECK_REPORT_HEADERS[:]

# Description Parse tab: per-point code vs free desc, flag-only
DESC_PARSE_HEADERS = [
    "OID", "PointNumber", "Northing", "Easting", "Elevation",
    "RawDescription", "ParsedCode", "FreeDesc", "Flags", "FlagDetail"
]


def write_check_report(dest_path: Path, report_rows):
    """Write headered .fwc (.chk legacy) (CSV). report_rows are list of lists matching header len."""
    try:
        with open(dest_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(CHECK_REPORT_HEADERS)
            w.writerows(report_rows)
        return True
    except OSError as e:
        print(f"Write check report failed {dest_path}: {e}")
        return False


def read_check_report(file_path: Path):
    """Read headered .fwc/.chk. Returns (headers, rows) or (None, None)."""
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            r = csv.reader(f)
            headers = next(r, None)
            if headers is None:
                return None, None
            headers = [h.strip() for h in headers]
            rows = []
            for raw in r:
                # Keep all cols, pad/truncate to header len
                row = [c.strip() for c in raw]
                if not any(row):
                    continue
                if len(row) < len(CHECK_REPORT_HEADERS):
                    row.extend([""] * (len(CHECK_REPORT_HEADERS) - len(row)))
                rows.append(row[:len(CHECK_REPORT_HEADERS)])
            return headers, rows
    except (OSError, csv.Error) as e:
        print(f"Read check report failed {file_path}: {e}")
        return None, None


def build_check_report_rows(working_rows, exact_groups, similar_groups, close_groups):
    """Flatten groups into per-point report rows with GroupID/IssueType/Detail.

    working_rows: 8-col list; groups: list of index lists.
    Returns list of row lists ready for write (without header).
    GroupID is 1..N across all issue types, stable order: Exact, Similar, Close.
    """
    report = []
    gid = 1
    # Helper to make detail string
    def detail_for_group(idxs, issue):
        pts = [working_rows[i][1] for i in idxs]
        if issue == "CloseNE":
            # compute max horizontal distance within group (NE) and its DMS bearing + elev diff
            coords = [(_working_row_to_float(working_rows[i][2]), _working_row_to_float(working_rows[i][3]), _working_row_to_float(working_rows[i][4])) for i in idxs]
            maxd = 0.0
            max_pair = ""
            max_dN = max_dE = max_dZ = 0.0
            max_bear = ""
            for a in range(len(coords)):
                for b in range(a + 1, len(coords)):
                    na, ea, za = coords[a]
                    nb, eb, zb = coords[b]
                    if na is None or ea is None or nb is None or eb is None:
                        continue
                    dN = nb - na
                    dE = eb - ea
                    d = math.hypot(dN, dE)
                    # Only consider pairs that also pass elev check (if elev present)
                    if za is not None and zb is not None and abs(zb - za) > ELEV_TOLERANCE + 1e-9:
                        continue
                    if d > maxd:
                        maxd = d
                        max_pair = f"{working_rows[idxs[a]][1]}↔{working_rows[idxs[b]][1]}"
                        max_dN, max_dE = dN, dE
                        max_dZ = (zb - za) if (za is not None and zb is not None) else 0.0
                        max_bear = _bearing_dms(dN, dE)
            # Hide units — keep internal tolerances
            if max_pair:
                # Simpler invert: distance + cardinal + elev (per request: e.g., "0.35 NE 0.02 EL")
                try:
                    # Compute cardinal for max pair
                    # Need dN/dE for max pair — we have max_dN/max_dE from loop but not stored in this scope's second function
                    # For first function (build_check_report_rows) we have max_dN/max_dE
                    cardinal = _cardinal_direction(max_dN, max_dE) if 'max_dN' in locals() else ""
                except:
                    cardinal = ""
                dd = f"{maxd:.2f} {cardinal}" if cardinal and cardinal != "—" else f"{maxd:.2f}"
                elev_s = f" {max_dZ:+.2f} EL" if any(c[2] is not None for c in coords) else ""
                return f"Close NE ≤{NE_TOLERANCE} ({len(idxs)} pts) max {dd}{elev_s} {max_pair}"
            else:
                return f"Close NE ≤{NE_TOLERANCE} ({len(idxs)} pts)"
        elif issue == "ExactDuplicate":
            return f"Exact duplicate point number '{working_rows[idxs[0]][1]}' ({len(idxs)} pts)"
        else:  # SimilarNumber
            core = point_number_core(working_rows[idxs[0]][1])
            uniq = sorted({working_rows[i][1] for i in idxs})
            return f"Similar core '{core}' variants {', '.join(uniq)} ({len(idxs)} pts)"

    for groups, itype in [(exact_groups, "ExactDuplicate"), (similar_groups, "SimilarNumber"), (close_groups, "CloseNE")]:
        for idxs in groups:
            group_det = detail_for_group(idxs, itype)
            # For CloseNE, precompute per-row delta from first point (smallest OID) — combine N/E into distance+ DMS bearing, plus elev diff
            base_n = base_e = base_z = None
            if itype == "CloseNE" and idxs:
                base_n = _working_row_to_float(working_rows[idxs[0]][2])
                base_e = _working_row_to_float(working_rows[idxs[0]][3])
                base_z = _working_row_to_float(working_rows[idxs[0]][4])
            for pos, idx in enumerate(idxs):
                r = working_rows[idx]
                det = group_det
                if itype == "CloseNE" and base_n is not None and base_e is not None:
                    n = _working_row_to_float(r[2] if len(r) > 2 else "")
                    e = _working_row_to_float(r[3] if len(r) > 3 else "")
                    z = _working_row_to_float(r[4] if len(r) > 4 else "")
                    if n is not None and e is not None:
                        dN = n - base_n
                        dE = e - base_e
                        dist = math.hypot(dN, dE)
                        cardinal_pt = _cardinal_direction(dN, dE)
                        dZ = (z - base_z) if (z is not None and base_z is not None) else None
                        elev_s = f" {dZ:+.2f} EL" if dZ is not None else ""
                        if pos == 0:
                            det = group_det + " — origin"
                        else:
                            dd = f"{dist:.2f} {cardinal_pt}" if cardinal_pt and cardinal_pt != "—" else f"{dist:.2f}"
                            det = group_det + f" • this pt {dd}{elev_s}"
                elif itype == "SimilarNumber" and pos > 0:
                    # Stack similar under first: prepend indent marker for visual tab-out (also handled in table styling)
                    det = group_det
                # r: OID, PtNum, N,E,Z, Desc, Parent, Source
                report.append([
                    str(gid),
                    itype,
                    r[0] if len(r) > 0 else "",
                    r[1] if len(r) > 1 else "",
                    r[2] if len(r) > 2 else "",
                    r[3] if len(r) > 3 else "",
                    r[4] if len(r) > 4 else "",
                    r[5] if len(r) > 5 else "",
                    r[6] if len(r) > 6 else "",
                    r[7] if len(r) > 7 else "",
                    det,
                    "Open",  # Status default
                    ""       # Comments
                ])
            gid += 1
    # Sort by OID numeric within report for stable save? Keep group order already.
    return report


def build_unified_report_rows(working_rows, exact_groups, similar_groups, close_groups, desc_flagged,
                              line_issues=None, line_statuses=None):
    """Build OID-minimal unified rows: Duplicate groups + flagged Description rows + line issues.
    working_rows: 8-col OID-first list sorted by OID
    desc_flagged: list of dicts {oid, flags_str, flag_detail, issue_type} for flagged OIDs only
    line_issues: the incomplete lines (fieldwork.linecheck) — the issues no single description shows
    line_statuses: {(issue_type, oid): {status, comments}} — decisions already made about them, so a
                   re-run does not resurrect an Ignored issue
    Returns list of rows matching UNIFIED_REPORT_HEADERS (9 cols).
    """
    rows = []
    gid = 1
    # Duplicate part — reuse existing detail logic but OID-minimal
    # Helper to mimic detail_for_group from build_check_report_rows
    def detail_for_group(idxs, issue):
        pts = [working_rows[i][1] for i in idxs]
        if issue == "CloseNE":
            coords = [(_working_row_to_float(working_rows[i][2]), _working_row_to_float(working_rows[i][3]), _working_row_to_float(working_rows[i][4])) for i in idxs]
            maxd = 0.0
            max_pair = ""
            max_dZ = 0.0
            max_bear = ""
            for a in range(len(coords)):
                for b in range(a + 1, len(coords)):
                    na, ea, za = coords[a]
                    nb, eb, zb = coords[b]
                    if na is None or ea is None or nb is None or eb is None:
                        continue
                    dN = nb - na
                    dE = eb - ea
                    d = math.hypot(dN, dE)
                    if za is not None and zb is not None and abs(zb - za) > ELEV_TOLERANCE + 1e-9:
                        continue
                    if d > maxd:
                        maxd = d
                        max_pair = f"{working_rows[idxs[a]][1]}↔{working_rows[idxs[b]][1]}"
                        max_dZ = (zb - za) if (za is not None and zb is not None) else 0.0
                        max_bear = _bearing_dms(dN, dE)
            if max_pair:
                # Simpler invert: distance + cardinal + elev (per request: e.g., "0.35 NE 0.02 EL")
                try:
                    # Compute cardinal for max pair
                    # Need dN/dE for max pair — we have max_dN/max_dE from loop but not stored in this scope's second function
                    # For first function (build_check_report_rows) we have max_dN/max_dE
                    cardinal = _cardinal_direction(max_dN, max_dE) if 'max_dN' in locals() else ""
                except:
                    cardinal = ""
                dd = f"{maxd:.2f} {cardinal}" if cardinal and cardinal != "—" else f"{maxd:.2f}"
                elev_s = f" {max_dZ:+.2f} EL" if any(c[2] is not None for c in coords) else ""
                return f"Close NE ≤{NE_TOLERANCE} ({len(idxs)} pts) max {dd}{elev_s} {max_pair}"
            else:
                return f"Close NE ≤{NE_TOLERANCE} ({len(idxs)} pts)"
        elif issue == "ExactDuplicate":
            return f"Exact duplicate point number '{working_rows[idxs[0]][1]}' ({len(idxs)} pts)"
        else:
            core = point_number_core(working_rows[idxs[0]][1])
            uniq = sorted({working_rows[i][1] for i in idxs})
            return f"Similar core '{core}' variants {', '.join(uniq)} ({len(idxs)} pts)"
    for groups, itype in [(exact_groups, "ExactDuplicate"), (similar_groups, "SimilarNumber"), (close_groups, "CloseNE")]:
        for idxs in groups:
            det = detail_for_group(idxs, itype)
            # One unified row per point in group (OID-minimal)
            for idx in idxs:
                oid = working_rows[idx][0] if len(working_rows[idx])>0 else ""
                # Flags empty for duplicate; Detail is group detail
                rows.append([str(gid), "Duplicate", itype, oid, "", det, "", "Open", ""])
            gid += 1
    # Description flagged part — one row per flagged OID (already flagged-only)
    for item in desc_flagged:
        oid = item.get("oid","")
        flags = item.get("flags_str","")
        detail = item.get("flag_detail","")
        # IssueType = first flag type or Multiple
        issue = item.get("issue_type","DescFlag")
        rows.append([str(gid), "Description", issue, oid, flags, detail, detail, "Open", ""])
        gid += 1
    # Line part — the issues that belong to a whole line, not to one point.
    if line_issues:
        from .linecheck import report_rows as _line_rows
        rows.extend(_line_rows(line_issues, line_statuses, start_gid=gid))
    return rows


def write_unified_report(dest_path: Path, rows):
    """Write unified .fwc (OID-minimal)."""
    try:
        with open(dest_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(UNIFIED_REPORT_HEADERS)
            w.writerows(rows)
        return True
    except OSError as e:
        print(f"Write unified report failed {dest_path}: {e}")
        return False


def read_unified_report(file_path: Path):
    """Read unified or legacy .fwc/.chk — auto-detect header. Returns (headers, rows) unified 9-col normalized.
    Legacy 13-col files are converted to unified minimal (DisplayTab inferred, Flags empty).
    """
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            r = csv.reader(f)
            headers = next(r, None)
            if headers is None:
                return None, None
            headers = [h.strip() for h in headers]
            rows = []
            is_legacy = headers == CHECK_REPORT_HEADERS or headers == LEGACY_CHECK_HEADERS
            is_unified = headers == UNIFIED_REPORT_HEADERS
            for raw in r:
                row = [c.strip() for c in raw]
                if not any(row):
                    continue
                if is_legacy:
                    # Legacy 13-col: [GroupID, IssueType, OID, PtNum, N, E, Z, Desc, Parent, Source, Detail, Status, Comments]
                    if len(row) < len(CHECK_REPORT_HEADERS):
                        row.extend([""]*(len(CHECK_REPORT_HEADERS)-len(row)))
                    row = row[:len(CHECK_REPORT_HEADERS)]
                    # Convert to unified minimal: GroupID, DisplayTab=Duplicate, IssueType, OID, Flags="", Detail=row[10], FlagDetail="", Status, Comments
                    uni = [row[0], "Duplicate", row[1], row[2], "", row[10], "", row[11], row[12]]
                    rows.append(uni)
                elif is_unified:
                    if len(row) < len(UNIFIED_REPORT_HEADERS):
                        row.extend([""]*(len(UNIFIED_REPORT_HEADERS)-len(row)))
                    rows.append(row[:len(UNIFIED_REPORT_HEADERS)])
                else:
                    # Unknown header — try to interpret as unified if has DisplayTab column
                    if "DisplayTab" in headers:
                        # Map by header name
                        idx = {h:i for i,h in enumerate(headers)}
                        def get(col, default=""):
                            return row[idx[col]] if col in idx and idx[col] < len(row) else default
                        uni = [get("GroupID"), get("DisplayTab","Duplicate"), get("IssueType"), get("OID"), get("Flags",""), get("Detail"), get("FlagDetail",""), get("Status","Open"), get("Comments","")]
                        rows.append(uni)
                    else:
                        # Fallback treat as legacy
                        rows.append(row[:9])
            return UNIFIED_REPORT_HEADERS, rows
    except (OSError, csv.Error) as e:
        print(f"Read unified report failed {file_path}: {e}")
        return None, None

def read_master_file_for_renumber(master_path: str, dup_corrected: set[int] = None) -> set[int]:
    """Combine master file PtNums + dup_corrected before original numbers (priority: dup_corrected numbers are considered used before originals)."""
    from .config import read_master_ptnums
    master = read_master_ptnums(master_path)
    if dup_corrected:
        return master | set(dup_corrected)
    return master

def get_dup_corrected_numbers(edit_table=None, check_all_rows=None) -> set[int]:
    """Collect duplicate corrected numbers (Corr_PtNum where Corr_Status not empty or check report Merged)."""
    out=set()
    # From edit_table Corr_PtNum col 8 if status Suggested/Accepted
    try:
        if edit_table is not None:
            for r in range(edit_table.rowCount()):
                it = edit_table.item(r, 8)
                if it and it.text().strip().isdigit():
                    out.add(int(it.text().strip()))
                # Also Dup_Renumber col if present (14)
                if edit_table.columnCount() > 12:
                    it2 = edit_table.item(r, 12)
                    if it2 and it2.text().strip().isdigit():
                        out.add(int(it2.text().strip()))
    except Exception:
        pass
    try:
        if check_all_rows:
            for ur in check_all_rows:
                # ur is [gid, display, issue, oid, flags, detail, flagdetail, status, comments]
                # For Merged primary, comments holds merged desc? Not pt. We need to parse dup renumber from comments? 
                # Instead we treat Dup_Renumber stored in edit_table, not here.
                pass
    except Exception:
        pass
    return out

