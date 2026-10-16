"""
Fieldwork Manager — Modular (program/fieldwork_manager/)
Current: 2026-09-23, America/Chicago, Mesquite TX

Purpose: Fieldwork CSV scan → Working file (.fwk, canonical OID) → Field Book (.fwb, Carlson F2F distilled) → Unified error report (.fwc, OID-minimal, DisplayTab routed) → Error tabs (Duplicate / Description / CloseNE, hidden until Run Checks).

Key rulings (locked):
- Units: US Survey Feet definitive, CONUS, pseudo-Euclidean N=X, E=Y, dist sqrt(dN²+dE²)
- Tolerances: NE 0.1, Elev 0.1 (definitive in code, not UI)
- Detectors: ExactDuplicate (casefold trimmed), SimilarNumber (core digits, distinct variants), CloseNE (horizontal ≤0.1 + elev ≤0.1, ≥2 distinct numbers)
- Description parsing: tolerant spacing, separator-split potential-code checks, Field Book correction rules, and semantic line-command order
- Field Book command meanings: Start Line, Start Curve, End Curve, End Line, Close, Multi-code separator, and Description separator; editable tokens are stored with the .fwb and interpreted by meaning
- Startup: 5 tabs visible [Fieldwork, Notes, Edit Fieldwork, Field Book, Check Settings]; error tabs hidden until Run Checks; New Project → Project Paths dialog; Correction Rules (2-col Common Error → Fix) stored in .fwb
- Unified .fwc (.chk legacy) (OID-minimal, DisplayTab column): Duplicate + flagged Description only; live-pull PtNum/N/E/Z/Desc/Parent/Source from Edit via OID; Desc tab flagged only
- Program is modular: program/run.py + program/fieldwork_manager/*; ship is FieldworkManager_v1.0001.zip (program/ zipped)
"""

# config.py — constants, headers, tolerances
import functools
import re
from pathlib import Path

PROJECT_FILE_EXT = ".fmp"
# Legacy check report extension .chk still readable (Windows thought .chk = recovered fragments)
LEGACY_CHECK_EXT = ".chk"
WORKING_FILE_EXT = ".fwk"
FIELDBOOK_EXT = ".fwb"
CHECK_REPORT_EXT = ".fwc"
COORDINATE_UNITS = "US Survey Feet"
XY_TOLERANCE = 0.1
NE_TOLERANCE = XY_TOLERANCE
ELEV_TOLERANCE = 0.1

# Semantic command definitions are shared with the converter and the project's linework engine.
# Stored tokens remain editable in each field book; parser and correction code asks for meaning.
from ..core.fieldbook_syntax import (
    COMMAND_LABELS as LINE_COMMAND_LABELS,
    DEFAULT_COMMAND_TOKENS as _DEFAULT_COMMAND_TOKENS,
    command_map as normalize_command_map,
    separator_text,
    separator_token,
    spacing_preference,
)

LINE_COMMAND_DEFAULTS = list(_DEFAULT_COMMAND_TOKENS)
LINE_COMMAND_LABELS = list(LINE_COMMAND_LABELS)
CODE_COMMAND_DEFAULTS = LINE_COMMAND_DEFAULTS
CODE_COMMAND_LABELS = LINE_COMMAND_LABELS
DEFAULT_COMMAND_SET = {str(c).casefold() for c in LINE_COMMAND_DEFAULTS if c}
COMMAND_SET = set(DEFAULT_COMMAND_SET)
_TOKEN_RE = re.compile(r'[A-Za-z0-9]+')

# Backward-compatible display constants; runtime code uses the active Field Book definitions.
MULTICODE_SEP = separator_text("multicode", spaced=True)
DESCRIPTION_SEP = separator_text("description", spaced=True)


def get_command_map(fieldbook_path=None, commands=None):
    """Return semantic command meanings for an explicit command list or the active .fwb file."""
    if commands is None and fieldbook_path:
        try:
            from .io_carlson import read_fwb_extra
            commands = read_fwb_extra(Path(fieldbook_path)).get("commands")
        except Exception:
            commands = None
    return normalize_command_map(commands)


def get_separator_texts(fieldbook_path=None, commands=None):
    """Formatted multi-code and description separators for one book and the user's preferences."""
    meanings = get_command_map(fieldbook_path, commands)
    return (separator_text("multicode", meanings), separator_text("description", meanings))


def get_separator_tokens(fieldbook_path=None, commands=None):
    """Raw separator tokens, keyed by meaning, as stored by the field book."""
    meanings = get_command_map(fieldbook_path, commands)
    return {"multicode": separator_token("multicode", meanings),
            "description": separator_token("description", meanings)}


def get_command_set(fieldbook_path=None, commands=None):
    """Return enabled command tokens from the active field book (including its separators)."""
    if commands is None and not fieldbook_path:
        return set(COMMAND_SET)
    if commands is None and fieldbook_path:
        try:
            from .io_carlson import read_fwb_extra
            commands = read_fwb_extra(Path(fieldbook_path)).get("commands")
        except Exception:
            commands = None
    if isinstance(commands, dict):
        semantic_names = {"start_line", "startline", "start_curve", "startcurve", "end_curve",
                          "endcurve", "end_line", "endline", "close", "multicode",
                          "multicode_separator", "multi_code_separator", "description", "description_separator"}
        if not any(str(k).strip().casefold().replace(" ", "_").replace("-", "_") in semantic_names
                   for k in commands):
            return {str(k).strip().casefold() for k, enabled in commands.items()
                    if enabled and str(k).strip()}
    return {str(token).strip().casefold() for token in normalize_command_map(commands).values()
            if str(token).strip()}

def get_correction_rules(fieldbook_path=None):
    """Return rules list [[common_error, fix], ...] for given fieldbook, or [].
    Stored in fieldbook file as extra rules.
    """
    if fieldbook_path:
        try:
            from pathlib import Path
            from .io_carlson import read_fwb_extra
            extra = read_fwb_extra(Path(fieldbook_path))
            rules = extra.get("rules")
            if isinstance(rules, list):
                # Validate each is [error, fix]
                out=[]
                for r in rules:
                    if isinstance(r, (list, tuple)) and len(r)>=2:
                        out.append([str(r[0]).strip(), str(r[1]).strip()])
                return out
        except Exception:
            pass
    return []

def set_command_set_global(commands):
    """Override global COMMAND_SET (for current session). commands is iterable of strings."""
    global COMMAND_SET
    COMMAND_SET = {str(c).strip().casefold() for c in commands if str(c).strip()}
    return COMMAND_SET

CHECK_REPORT_HEADERS = [
    "GroupID", "IssueType", "OID", "PointNumber", "Northing", "Easting", "Elevation",
    "Description", "ParentFolder", "SourceFile", "Detail", "Status", "Comments"
]
UNIFIED_REPORT_HEADERS = [
    "GroupID", "DisplayTab", "IssueType", "OID", "Flags", "Detail", "FlagDetail", "Status", "Comments"
]
LEGACY_CHECK_HEADERS = CHECK_REPORT_HEADERS[:]

DESC_PARSE_HEADERS = [
    "OID", "PointNumber", "Northing", "Easting", "Elevation",
    "RawDescription", "ParsedCode", "FreeDesc", "Flags", "FlagDetail", "Correction", "Ignore"
]

# --- Number Assignment Settings (2026-09-20) ---
# Control 1-999, Boundary 1000-9999, General 10000+ — internal, not user-toggled (hidden like Check Settings)
CONTROL_RANGE = (1, 999)
BOUNDARY_RANGE = (1000, 9999)
GENERAL_START = 10000

# Crew blocking: crew N gets all N-blocks at each magnitude (e.g., crew 3 → 300-399, 3000-3999, 30000-39999 ...)
# Used for renumbering; locked behind cleaned descriptions (all flags Skipped/Fixed)
DEFAULT_CREW_NUMBER = 3

@functools.lru_cache(maxsize=16)
def crew_blocks(crew: int) -> list[tuple[int, int]]:
    """Return list of (start, end) inclusive for crew blocks across magnitudes.
    Crew must be 1-9. Blocks are crew * 10^k  to crew*10^k + 10^k -1 for k=2..6 (3-digit up to 7-digit).
    e.g., crew 3 → [(300,399), (3000,3999), (30000,39999), (300000,399999), (3000000,3999999)]
    """
    if not 1 <= crew <= 9:
        return []
    blocks = []
    for k in range(2, 7):  # 10^2=100 up to 10^6
        start = crew * (10 ** k)
        end = start + (10 ** k) - 1
        blocks.append((start, end))
    return blocks

def is_in_crew_block(pt_num: str | int, crew: int) -> bool:
    if isinstance(pt_num, int):
        n = pt_num
    else:
        try:
            n = int(str(pt_num).strip())
        except Exception:
            return False
    for s, e in crew_blocks(crew):
        if s <= n <= e:
            return True
    return False

def number_type_label(pt_num: str) -> str:
    try:
        n = int(str(pt_num).strip())
    except:
        return "Unknown"
    if CONTROL_RANGE[0] <= n <= CONTROL_RANGE[1]:
        return "Control"
    if BOUNDARY_RANGE[0] <= n <= BOUNDARY_RANGE[1]:
        return "Boundary"
    if n >= GENERAL_START:
        return "General"
    return "Reserved"

def parse_carlson_ranges(range_str: str) -> tuple[set[int], list[tuple[int,int]]]:
    """Parse Carlson ranges like '1000-10014,10016' → (used_set, ranges).
    Only point missing from 1000-10016 is 10015. Returns set of ints and list of (start,end)."""
    used = set()
    ranges = []
    if not range_str:
        return used, ranges
    for part in range_str.split(','):
        part=part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                s,e = part.split('-',1)
                s=int(s.strip()); e=int(e.strip())
                if s>e: s,e = e,s
                ranges.append((s,e))
                used.update(range(s, e+1))
            except: continue
        else:
            try:
                n=int(part)
                ranges.append((n,n))
                used.add(n)
            except: continue
    return used, ranges

def find_holes_in_ranges(used: set[int], crew: int = None, type_range: tuple[int,int] = None) -> list[int]:
    """Find holes (gaps) — next available conserving numbers. If crew+type given, intersect."""
    candidates = []
    if crew is not None and type_range is not None:
        for s,e in crew_blocks(crew):
            isec_s = max(s, type_range[0])
            isec_e = min(e, type_range[1])
            if isec_s <= isec_e:
                for n in range(isec_s, isec_e+1):
                    if n not in used:
                        candidates.append(n)
                if len(candidates) >= 20:
                    break
        if type_range[0] >= GENERAL_START and crew is not None:
            for s,e in crew_blocks(crew):
                if s >= GENERAL_START and s not in range(type_range[0], type_range[1]+1):
                    for n in range(s, e+1):
                        if n not in used:
                            candidates.append(n)
                            if len(candidates) >= 20:
                                break
                if len(candidates) >= 20:
                    break
    elif crew is not None:
        for s,e in crew_blocks(crew):
            for n in range(s, e+1):
                if n not in used:
                    candidates.append(n)
                    if len(candidates) >= 20:
                        break
            if len(candidates) >= 20:
                break
    elif type_range is not None:
        for n in range(type_range[0], type_range[1]+1):
            if n not in used:
                candidates.append(n)
                if len(candidates) >= 20:
                    break
    return candidates[:20]

def next_at_end(used: set[int], crew: int = None, type_range: tuple[int,int] = None) -> int | None:
    """At end — max+1 in assignment range."""
    if not used:
        if type_range: return type_range[0]
        if crew: return crew_blocks(crew)[0][0]
        return None
    if crew is not None and type_range is not None:
        max_n = -1
        for s,e in crew_blocks(crew):
            isec_s = max(s, type_range[0]); isec_e = min(e, type_range[1])
            if isec_s <= isec_e:
                in_block = [n for n in used if isec_s <= n <= isec_e]
                if in_block: max_n = max(max_n, max(in_block))
        if max_n != -1: return max_n + 1
        for s,e in crew_blocks(crew):
            if type_range[0] <= s <= type_range[1]:
                return s
        return type_range[1]+1
    if crew is not None:
        in_crew = [n for n in used if any(s <= n <= e for s,e in crew_blocks(crew))]
        if in_crew: return max(in_crew)+1
        return crew_blocks(crew)[0][0]
    if type_range is not None:
        in_type = [n for n in used if type_range[0] <= n <= type_range[1]]
        if in_type: return max(in_type)+1
        return type_range[0]
    return max(used)+1

def read_grp_file(path):
    """Read .grp text file: group name → list of point numbers. Handles 'Group: 1-5,10' lines."""
    from pathlib import Path
    groups = {}
    try:
        txt = Path(path).read_text(encoding='utf-8', errors='ignore')
        cur = None
        for line in txt.splitlines():
            line=line.strip()
            if not line or line.startswith(';') or line.startswith('#'):
                continue
            if line.startswith('[') and line.endswith(']'):
                cur = line[1:-1].strip()
                groups[cur]=[]
                continue
            if ':' in line and not line[0].isdigit():
                name, pts = line.split(':',1)
                name=name.strip(); pts=pts.strip()
                cur=name; groups.setdefault(cur, [])
                used,_ = parse_carlson_ranges(pts)
                groups[cur].extend(sorted(used))
                continue
            if cur is not None and (line[0].isdigit() or '-' in line or ',' in line):
                used,_ = parse_carlson_ranges(line)
                groups[cur].extend(sorted(used))
            elif cur is None and line:
                cur=line; groups.setdefault(cur, [])
    except Exception as e:
        print(f"read_grp failed {path}: {e}")
    return groups




# --- Master file & final collapsed helpers (2026-09-24) ---

def read_master_ptnums(master_path: str) -> set[int]:
    """Read master final file CSV (e.g., 8-col OID,PtNum,N,E,Z,Desc,Parent,Source or simple PtNum list) → set of ints."""
    if not master_path:
        return set()
    from pathlib import Path
    import csv as _csv
    mp = Path(master_path)
    if not mp.exists():
        return set()
    out=set()
    try:
        with open(mp, newline='', encoding='utf-8', errors='ignore') as f:
            reader = _csv.reader(f)
            for row in reader:
                if not row:
                    continue
                # Try column 1 as PtNum if row looks like working file (OID first)
                cand = None
                if len(row) >= 2 and row[0].strip().isdigit() and row[1].strip().lstrip('-').isdigit():
                    cand = row[1].strip()
                else:
                    # Fallback: first column that is integer
                    for c in row:
                        if c.strip().lstrip('-').isdigit():
                            # Prefer PtNum-like (not OID huge? but take first numeric)
                            cand = c.strip()
                            break
                if cand is not None:
                    try:
                        # Handle alphanumeric like 907a — strip letters? For master we want numeric core?
                        import re
                        m = re.search(r'-?\d+', cand)
                        if m:
                            n=int(m.group(0))
                            out.add(n)
                    except:
                        pass
    except Exception as e:
        print(f"read_master_ptnums failed {master_path}: {e}")
    return out

def final_ptnum_for_oid(original_ptnum: str, dup_renum: str, global_renum: str) -> str:
    """Global overwrites duplicate renumber, which overwrites original. Returns final PtNum str."""
    if global_renum and global_renum.strip():
        return global_renum.strip()
    if dup_renum and dup_renum.strip():
        return dup_renum.strip()
    return original_ptnum


# Carlson entity types
carlson_entity_types = {
    "0": "Point",
    "1": "Line",
    "2": "2D Polyline",
    "3": "3D Polyline",
}
