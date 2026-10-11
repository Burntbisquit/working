"""Linework, read whole: the issues one description cannot show, and the tools for them.

Every other check in this program reads **one point at a time**. A line is a sequence: a point may
carry a valid description while the line has no semantic End Line/Close, or a curve may start without
an End Curve. The two ends of one mistake usually sit on **two different points**, which is why the
tool that fixes them has to be allowed to clear a second flag it was not aimed at.

The Field Book's meanings define the three line-order pairs this module flags:
Start Curve before Start Line, End Line before End Curve, and Close before End Curve. Singleton
commands and unrelated sequence-state issues are not reported as line-order errors. A fix is
**proposed, then validated**: after it is applied, the line is read again to verify the result.

This module is Qt-free on purpose: the field window's Line Repair tab and the drawing window's
Check Fieldwork dock both use it, and neither should be able to disagree with the other about
where a line ends.
"""
from __future__ import annotations

from . import config as C
from ..core.fieldbook_syntax import first_line_order_pair_violation
from .utils_sort import natural_key


def _sequence_sort_key(row):
    """Sort numeric OIDs numerically and other OIDs naturally, without mixed-type failures."""
    oid = str(row[0]).strip() if row else ""
    if oid.isdigit():
        return 0, int(oid)
    return 1, natural_key(oid, letters_first=False)


#: The line-sequence issues exposed in Line Repair and persisted to ``.fwc`` IssueType rows.
ISSUE_TYPES = ("Missing ST", "Missing END", "Missing PC", "Missing PT", "Line Order")

#: ...and the short token each one travels as, because a tool switches on a flag, not on prose.
ISSUE_FLAGS = {
    "Missing ST": "MissingStart",
    "Missing END": "MissingEnd",
    "Missing PC": "MissingPC",
    "Missing PT": "MissingPT",
    "Line Order": "LineOrderError",
}

#: The semantic command hierarchy is fixed even when an office changes its command tokens.
RANK = {"start_line": 0, "start_curve": 1, "end_curve": 2, "end_line": 3, "close": 3}


def _command_map(fieldbook_path=None, commands=None):
    from .config import get_command_map
    return get_command_map(fieldbook_path, commands)


def _command_set(command_set=None, commands=None) -> set:
    if command_set is not None:
        return {str(c).strip().casefold() for c in command_set if str(c).strip()}
    from .config import get_command_set
    return get_command_set(commands=commands)


def _f2f_for(fieldbook_path, f2f_set):
    """The code vocabulary: the caller's if it has one, else the field book's, else none."""
    if f2f_set:
        return f2f_set
    if fieldbook_path:
        try:
            from .parse import build_f2f_set_from_fieldbook
            return build_f2f_set_from_fieldbook(fieldbook_path)
        except Exception:
            return set()
    return set()


def _command_set_for(fieldbook_path, command_set, commands=None):
    if command_set is not None:
        return command_set
    try:
        from .config import get_command_set
        return get_command_set(fieldbook_path, commands)
    except Exception:
        return None


# --------------------------------------------------------------------------------- detection
def detect_line_errors(working_rows, fieldbook_path=None, f2f_set=None, command_set=None,
                       rules=None, commands=None) -> list[dict]:
    """Read each coded line in file order and report incomplete line/curve command sequences."""
    if not working_rows:
        return []
    from .parse import parse_desc_field
    from ..core.fieldbook_syntax import command_meanings

    f2f = _f2f_for(fieldbook_path, f2f_set)
    semantic_tokens = _command_map(fieldbook_path, commands)
    token_meanings = command_meanings(semantic_tokens)
    cs = _command_set_for(fieldbook_path, command_set, semantic_tokens)
    line_meanings = set(RANK)

    oid_list = []
    for working_row in working_rows:
        oid = str(working_row[0]).strip() if len(working_row) > 0 else ""
        raw = str(working_row[5]).strip() if len(working_row) > 5 else ""
        oid_list.append((oid, raw, working_row))
    oid_list.sort(key=lambda row: _sequence_sort_key(row[2]))

    # Keep each code-token occurrence independent, including repeated code names in one
    # multicode description. The second key component links corresponding occurrences across
    # points without merging two separate sections on the same point.
    segments_by_line: dict[tuple[str, int], list] = {}
    for oid, raw, _working_row in oid_list:
        parsed = parse_desc_field(raw, f2f, fieldbook_path=fieldbook_path,
                                  command_set=cs, rules=rules, commands=semantic_tokens)
        classified = parsed.get("code_classified", [])
        commands_by_code_index: dict[int, list] = {}
        line_key_by_code_index: dict[int, tuple[str, int]] = {}
        code_raw_by_index: dict[int, str] = {}
        occurrence_counts: dict[str, int] = {}

        for code_index, item in enumerate(classified):
            if item["type"] != "code" or item["status"] not in ("exact", "line_instance"):
                continue
            line_id = item["raw"].strip().casefold()
            occurrence = occurrence_counts.get(line_id, 0)
            occurrence_counts[line_id] = occurrence + 1
            commands_by_code_index[code_index] = []
            line_key_by_code_index[code_index] = (line_id, occurrence)
            code_raw_by_index[code_index] = item["raw"]

        for item in classified:
            if item["type"] != "command" or item.get("status") != "valid" or "attached_to" not in item:
                continue
            meaning = item.get("meaning") or token_meanings.get(item.get("norm", "").casefold(), "")
            if meaning not in line_meanings:
                continue
            code_index = item["attached_to"]
            if code_index in commands_by_code_index:
                commands_by_code_index[code_index].append(meaning)

        for code_index, line_commands in commands_by_code_index.items():
            line_key = line_key_by_code_index[code_index]
            segments_by_line.setdefault(line_key, []).append(
                (oid, line_commands, raw, code_raw_by_index[code_index]))

    errors: list[dict] = []

    def raw_of(oid):
        return next((raw for current_oid, raw, _row in oid_list if current_oid == str(oid)), "")

    def issues_for(line_key, points):
        line_id = line_key[0]
        out = []
        segments = []
        current_segment = []
        in_line = False
        code_raw = points[0][3] if points else line_id

        def add(issue_type, oid, detail, suggestion):
            out.append({
                "issue_type": issue_type, "oid": oid, "line_id": line_id,
                "detail": detail, "fix_suggestion": suggestion, "seg_id": len(segments) + 1,
                "proposal": propose_fix(issue_type, raw_of(oid), line_id,
                                         command_set=cs, commands=semantic_tokens,
                                         f2f_set=f2f, fieldbook_path=fieldbook_path),
            })

        for oid, line_commands, raw, _code in points:
            if "start_line" in line_commands:
                if in_line and current_segment:
                    add("Missing END", current_segment[-1][0],
                        f"Line {code_raw} segment is missing End Line/Close before a new Start Line "
                        f"at OID {oid} ({raw}); previous segment started at OID {current_segment[0][0]}",
                        f"Add End Line to OID {current_segment[-1][0]}")
                    segments.append(current_segment)
                    current_segment = []
                in_line = True
                current_segment.append((oid, line_commands, raw))
            elif in_line:
                current_segment.append((oid, line_commands, raw))
                if "end_line" in line_commands or "close" in line_commands:
                    segments.append(current_segment)
                    current_segment = []
                    in_line = False
            elif line_commands:
                add("Missing ST", oid,
                    f"Line {code_raw} has {line_commands} at OID {oid} ({raw}) without a prior Start Line",
                    f"Add Start Line to OID {oid}")
                in_line = True
                current_segment = [(oid, line_commands, raw)]
                if "end_line" in line_commands or "close" in line_commands:
                    segments.append(current_segment)
                    current_segment = []
                    in_line = False
        if current_segment:
            add("Missing END", current_segment[-1][0],
                f"Line {code_raw} segment starting at OID {current_segment[0][0]} never received "
                f"End Line/Close (last point OID {current_segment[-1][0]})",
                f"Add End Line to OID {current_segment[-1][0]}")
            segments.append(current_segment)

        sequence_events = [(meaning, oid, raw) for oid, line_commands, raw, _code in points
                           for meaning in line_commands if meaning in RANK]
        order_violation = first_line_order_pair_violation(
            [meaning for meaning, _oid, _raw in sequence_events])
        if order_violation is not None:
            bad_index, counterpart_index, reason = order_violation
            _bad_meaning, bad_oid, _bad_raw = sequence_events[bad_index]
            first_index, second_index = sorted((bad_index, counterpart_index))
            first_meaning, first_oid, first_raw = sequence_events[first_index]
            second_meaning, second_oid, second_raw = sequence_events[second_index]
            first_label = first_meaning.replace("_", " ").title()
            second_label = second_meaning.replace("_", " ").title()
            if reason == "Start Curve appears before Start Line":
                suggestion = "Place Start Line before Start Curve in this code section."
            elif reason == "End Line appears before End Curve":
                suggestion = "Place End Curve before End Line in this code section."
            else:
                suggestion = "Place End Curve before Close in this code section."
            add("Line Order", bad_oid,
                f"Line {code_raw} has {reason} in its same-code point sequence: "
                f"{first_label} at OID {first_oid} ({first_raw}) precedes "
                f"{second_label} at OID {second_oid} ({second_raw}).",
                suggestion)

        for segment in segments:
            curve_open_oid = None
            for oid, line_commands, _raw in segment:
                for meaning in line_commands:
                    if meaning == "start_curve":
                        curve_open_oid = oid
                    elif meaning == "end_curve":
                        curve_open_oid = None
            curve_starts = [oid for oid, line_commands, _raw in segment
                            if "start_curve" in line_commands]
            curve_ends = [oid for oid, line_commands, _raw in segment
                          if "end_curve" in line_commands]
            if curve_open_oid is not None:
                add("Missing PT", curve_open_oid,
                    f"Line {code_raw} has Start Curve at OID {curve_open_oid} without a matching End Curve",
                    f"Add End Curve to OID {curve_open_oid} (after Start Curve, before End Line/Close)")
            if curve_ends and not curve_starts:
                add("Missing PC", curve_ends[0],
                    f"Line {code_raw} has End Curve at OID {curve_ends[0]} without a prior Start Curve",
                    f"Add Start Curve to OID {curve_ends[0]} (before End Curve)")
        return out

    for line_key, points in segments_by_line.items():
        has_line_command = any(any(command in line_meanings for command in line_commands)
                               for _oid, line_commands, _raw, _code in points)
        if not has_line_command:
            continue
        for issue in issues_for(line_key, points):
            issue["gid"] = str(len(errors) + 1)
            errors.append(issue)
    return errors


# --------------------------------------------------------------------------------- the tools
def propose_fix(issue_type: str, current_desc: str, line_id: str = "", command_set=None,
                commands=None, f2f_set=None, fieldbook_path=None) -> str:
    """Place the missing semantic command in its Field Book-defined, correctly ordered position."""
    if not (current_desc or "").strip():
        return ""
    from ..core.fieldbook_syntax import command_joiner, command_meanings
    from .parse import parse_desc_field

    semantic_tokens = _command_map(fieldbook_path, commands)
    token_meanings = command_meanings(semantic_tokens)
    cs = _command_set(command_set, semantic_tokens)
    meanings_ordered = list(RANK)
    parsed = None
    if f2f_set:
        parsed = parse_desc_field(current_desc, f2f_set, fieldbook_path=fieldbook_path,
                                  command_set=cs, commands=semantic_tokens)

    if parsed is not None:
        if parsed.get("free_desc"):
            return ""
        classified = parsed.get("code_classified", [])
        code_items = [item for item in classified if item["type"] == "code" and
                      item.get("status") in ("exact", "line_instance")]
        if len(code_items) != 1 or any(item.get("status") in ("unknown", "orphan")
                                        for item in classified):
            return ""
        head = code_items[0]["raw"]
        rest_meanings = []
        for item in classified:
            if item["type"] != "command":
                continue
            meaning = item.get("meaning") or token_meanings.get(item.get("norm", "").casefold(), "")
            if item.get("status") != "valid" or meaning not in meanings_ordered:
                return ""
            rest_meanings.append(meaning)
    else:
        words = current_desc.split()
        if not words:
            return ""
        separator_tokens = {semantic_tokens.get("multicode", "").casefold(),
                            semantic_tokens.get("description", "").casefold()}
        if words[0].casefold() in cs:
            return ""
        code_words = [word for word in words if word.casefold() not in cs and
                      word.casefold() not in separator_tokens]
        if len(code_words) != 1:
            return ""
        head = code_words[0]
        rest_meanings = []
        for word in words:
            meaning = token_meanings.get(word.casefold(), "")
            if meaning in meanings_ordered:
                rest_meanings.append(meaning)
            elif word != head and word.casefold() not in separator_tokens and word.casefold() not in cs:
                return ""

    line = (line_id or "").casefold().strip()
    if line and head.casefold() != line:
        return ""

    if issue_type == "Missing ST":
        required = "start_line"
        if required in rest_meanings:
            return ""
        insert_at = 0
    elif issue_type == "Missing END":
        required = "end_line"
        if any(meaning in ("end_line", "close") for meaning in rest_meanings):
            return ""
        insert_at = len(rest_meanings)
    elif issue_type == "Missing PC":
        required = "start_curve"
        if required in rest_meanings:
            return ""
        insert_at = next((i for i, meaning in enumerate(rest_meanings)
                          if meaning in ("end_curve", "end_line", "close")), len(rest_meanings))
    elif issue_type == "Missing PT":
        required = "end_curve"
        if required in rest_meanings:
            return ""
        insert_at = next((i for i, meaning in enumerate(rest_meanings)
                          if meaning in ("end_line", "close")), len(rest_meanings))
    else:
        return ""

    token = semantic_tokens.get(required, "")
    if not token:
        return ""
    rest_meanings.insert(insert_at, required)
    command_tokens = [semantic_tokens.get(meaning, "") for meaning in rest_meanings]
    if any(not command for command in command_tokens):
        return ""
    return command_joiner().join([head, *command_tokens])


def validate_fix(working_rows, oid, new_desc, fieldbook_path=None, f2f_set=None,
                 command_set=None, rules=None, commands=None) -> dict:
    """Apply a proposal in memory, re-read the line, and report what it cleared."""
    cs = _command_set_for(fieldbook_path, command_set, commands)
    before = detect_line_errors(working_rows, fieldbook_path=fieldbook_path, f2f_set=f2f_set,
                                command_set=cs, rules=rules, commands=commands)
    rows2 = []
    found = False
    for working_row in working_rows:
        row = list(working_row)
        if str(row[0]).strip() == str(oid).strip():
            if len(row) > 5:
                row[5] = new_desc
            found = True
        rows2.append(row)
    if not found:
        return {"ok": False, "cleared": [], "also_cleared": [], "still": [],
                "note": f"There is no point with OID {oid} in the working file - nothing was changed."}
    after = detect_line_errors(rows2, fieldbook_path=fieldbook_path, f2f_set=f2f_set,
                               command_set=cs, rules=rules, commands=commands)
    key = lambda issue: (str(issue["issue_type"]), str(issue["oid"]),
                         str(issue.get("line_id", "")))  # noqa: E731
    seen_after = {key(issue) for issue in after}
    target_line_ids = {
        str(issue.get("line_id", "")) for issue in before
        if issue.get("issue_type") == "Line Order" and str(issue["oid"]).strip() == str(oid).strip()
    }
    if target_line_ids:
        # A sequence-level error can move to a different point after a key-in. Validate
        # the whole line, not only the point that originally carried the finding.
        still = [issue for issue in after if issue.get("issue_type") == "Line Order"
                 and str(issue.get("line_id", "")) in target_line_ids]
        cleared = [issue for issue in before if key(issue) not in seen_after
                   and str(issue.get("line_id", "")) in target_line_ids]
        also = [issue for issue in before if key(issue) not in seen_after
                and str(issue.get("line_id", "")) not in target_line_ids
                and str(issue["oid"]).strip() != str(oid).strip()]
    else:
        cleared = [issue for issue in before if key(issue) not in seen_after and
                   str(issue["oid"]).strip() == str(oid).strip()]
        also = [issue for issue in before if key(issue) not in seen_after and
                str(issue["oid"]).strip() != str(oid).strip()]
        still = [issue for issue in after if str(issue["oid"]).strip() == str(oid).strip()]
    return {"ok": not still, "cleared": cleared, "also_cleared": also, "still": still,
            "note": fix_note(oid, new_desc, cleared, also, still)}

def fix_note(oid, new_desc, cleared, also, still) -> str:
    """One sentence a status line can carry: what the fix did, and what it left."""
    bits = []
    if cleared:
        bits.append(f"cleared {', '.join(e['issue_type'] for e in cleared)} at OID {oid}")
    if also:
        bits.append("also cleared " + ", ".join(f"{e['issue_type']} at OID {e['oid']}" for e in also))
    if still:
        bits.append(f"OID {oid} still has " + ", ".join(e["issue_type"] for e in still)
                    + f" after '{new_desc}' - write it by hand (Key-In)")
    if not bits:
        return f"'{new_desc}' applied. No line issue was flagged at OID {oid}, before or after."
    return f"'{new_desc}' applied - " + "; ".join(bits) + "."


# --------------------------------------------------------------------------------- the report
def report_rows(issues, statuses=None, start_gid: int = 1) -> list:
    """``.fwc`` rows (DisplayTab=Line) for a list of issues - one row per issue, OID-minimal.

    *statuses* is keyed by ``(issue_type, oid)`` and carries the decisions a user has already
    made about these issues, so a re-run does not quietly resurrect an Ignored one.  See
    :func:`statuses_from_rows` for the other half of that round trip.
    """
    statuses = statuses or {}
    rows = []
    gid = start_gid
    for e in issues:
        issue = str(e.get("issue_type", ""))
        oid = str(e.get("oid", ""))
        decided = statuses.get((issue, oid)) or statuses.get((issue, str(oid))) or {}
        rows.append([
            str(gid), "Line", issue, oid,
            ISSUE_FLAGS.get(issue, ""),
            str(e.get("detail", "")),
            str(e.get("fix_suggestion", "")),
            str(decided.get("status") or "Open"),
            str(decided.get("comments") or ""),
        ])
        gid += 1
    return rows


def statuses_from_rows(rows) -> dict:
    """The decisions stored in a ``.fwc``'s Line rows: ``{(issue_type, oid): {status, comments}}``.

    Only rows that actually decided something are returned - an Open row with no comment is
    the absence of a decision, not a decision.
    """
    out = {}
    for r in rows or []:
        try:
            display, issue, oid, status, comments = r[1], r[2], r[3], r[7], r[8]
        except IndexError:
            continue
        if str(display).strip().lower() != "line":
            continue
        if str(status).strip().lower() in ("", "open") and not str(comments).strip():
            continue
        out[(str(issue).strip(), str(oid).strip())] = {"status": str(status).strip(),
                                                       "comments": str(comments).strip()}
    return out


__all__ = ["ISSUE_TYPES", "ISSUE_FLAGS", "RANK", "detect_line_errors", "propose_fix",
           "validate_fix", "fix_note", "report_rows", "statuses_from_rows"]
