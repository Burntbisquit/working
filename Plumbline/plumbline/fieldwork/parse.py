# parse.py — description parsing, line order validation, F2F set
import re, math
from collections import defaultdict
from pathlib import Path
from .config import COMMAND_SET, _TOKEN_RE, get_command_map, get_command_set
from ..core.fieldbook_syntax import (
    COMMAND_MEANINGS,
    command_meanings,
    find_separator,
    first_line_order_pair_violation,
    separator_pattern,
    separator_spacing_is_valid,
    split_at_separator,
    spacing_preference,
)
from .io_carlson import read_fwb_file  # avoid circular: io does not import parse

def _strip_trailing_digits(token: str):
    """Return (base, number_str) where base is token without trailing digits. If no trailing digits, number_str=''."""
    import re
    m = re.match(r'^(.*?)(\d+)$', token)
    if m:
        return m.group(1), m.group(2)
    return token, ""

def _strip_leading_digits(token: str):
    """Return (number_str, base) where base is token without leading digits. If no leading digits, number_str=''."""
    import re
    m = re.match(r'^(\d+)(.*)$', token)
    if m:
        return m.group(1), m.group(2)
    return "", token

def _is_command(tok: str, command_set=None) -> bool:
    cs = command_set if command_set is not None else COMMAND_SET
    # If still default, try to refresh from global (in case set_command_set_global was called)
    try:
        # Use get_command_set if available for dynamic
        from .config import get_command_set
        # If command_set is None and we have no explicit, we could keep global COMMAND_SET which may have been updated
        pass
    except Exception:
        pass
    return tok.strip().casefold() in cs

def _classify_tokens_sequential(raw_tokens, f2f_set, command_set=None, rules=None, commands=None):
    """Classify tokens and attach enabled Field Book commands to the preceding valid code."""
    result = []
    meanings = command_meanings(commands)
    last_code_idx = None
    for token in raw_tokens:
        low = token.strip().casefold()
        cs = command_set if command_set is not None else COMMAND_SET
        if low in cs:
            meaning = meanings.get(low, "")
            if last_code_idx is not None:
                result.append({"raw": token, "norm": low, "type": "command", "status": "valid",
                               "meaning": meaning, "attached_to": last_code_idx})
            else:
                result.append({"raw": token, "norm": low, "type": "command", "status": "orphan",
                               "meaning": meaning, "error": "OrphanCommand"})
            continue

        if low in f2f_set:
            result.append({"raw": token, "norm": low, "type": "code", "status": "exact",
                           "base": low, "line_num": ""})
            last_code_idx = len(result) - 1
            continue

        base, number = _strip_trailing_digits(token)
        base_low = base.strip().casefold()
        if number and base and base_low in f2f_set:
            result.append({"raw": token, "norm": low, "type": "code", "status": "line_instance",
                           "base": base_low, "line_num": number})
            last_code_idx = len(result) - 1
            continue

        lead_num, lead_base = _strip_leading_digits(token)
        lead_base_low = lead_base.strip().casefold()
        base2, number2 = _strip_trailing_digits(lead_base)
        base2_low = base2.strip().casefold()
        if lead_num and lead_base and (lead_base_low in f2f_set or
                                       (number2 and base2 and base2_low in f2f_set)):
            matched_base = lead_base_low if lead_base_low in f2f_set else base2_low
            result.append({"raw": token, "norm": low, "type": "code",
                           "status": "line_instance" if number2 else "exact", "base": matched_base,
                           "line_num": number2, "lead_num": lead_num})
            last_code_idx = len(result) - 1
        else:
            result.append({"raw": token, "norm": low, "type": "code", "status": "unknown",
                           "error": "UnknownCode"})
            last_code_idx = None
    return result


def _extract_tokens(code_part: str):
    """Extract alphanumeric tokens from a string, handling missing dash/spaces."""
    return _TOKEN_RE.findall(code_part)


def _is_known_code(token: str, f2f_set) -> bool:
    """Whether a token is an exact, numbered, or leading-number instance of an F2F code."""
    low = token.casefold()
    if low in f2f_set:
        return True
    base, number = _strip_trailing_digits(token)
    if number and base.casefold() in f2f_set:
        return True
    lead_num, lead_base = _strip_leading_digits(token)
    if lead_num and lead_base:
        if lead_base.casefold() in f2f_set:
            return True
        base, number = _strip_trailing_digits(lead_base)
        return bool(number and base.casefold() in f2f_set)
    return False


def _extract_semantic_tokens(text: str, f2f_set, command_set, commands=None) -> list[str]:
    """Extract codes and commands while respecting active separators and compact code-command pairs."""
    from .config import get_command_map

    meanings = command_meanings(commands)
    semantic_tokens = get_command_map(commands=commands)
    multicode = semantic_tokens.get("multicode", "")
    segments = split_at_separator(text, multicode, maxsplit=0) if multicode else [text]
    line_commands = {
        token for token, meaning in meanings.items()
        if meaning in COMMAND_MEANINGS[:5] and token in command_set
    }
    def split_compact(token, depth=0):
        low = token.casefold()
        if low in command_set or _is_known_code(token, f2f_set) or depth >= 8:
            return [token]
        for command in sorted(line_commands, key=len, reverse=True):
            if low.endswith(command) and len(token) > len(command):
                prefix = token[:-len(command)]
                split_prefix = split_compact(prefix, depth + 1)
                if split_prefix and split_prefix[0].casefold() not in command_set and _is_known_code(split_prefix[0], f2f_set):
                    return [*split_prefix, token[-len(command):]]
        return [token]

    extracted = []
    for segment in segments:
        for raw_token in _extract_tokens(segment):
            extracted.extend(split_compact(raw_token))
    return extracted


def matching_correction_rule(raw_desc: str, rules=None, fieldbook_path=None, commands=None):
    """Return the first Field Book common-error rule matching the code portion of a description.

    A correction rule is meaningful even when its source is not a valid Field Book code. Keep
    matching independent of token classification so an unknown code cannot hide the rule that
    explains how it should be corrected. Free-text after the active description separator is not
    searched.
    """
    raw = str(raw_desc or "")
    if not raw.strip():
        return None
    if rules is None and fieldbook_path:
        try:
            from .config import get_correction_rules
            rules = get_correction_rules(fieldbook_path)
        except Exception:
            rules = None
    if not rules:
        return None
    try:
        semantic_commands = get_command_map(fieldbook_path, commands)
    except Exception:
        semantic_commands = get_command_map(commands=commands)
    description_token = semantic_commands.get("description", "")
    boundary = find_separator(raw, description_token)
    code_part = raw[:boundary] if boundary >= 0 else raw

    for rule in rules:
        try:
            error_text, fix_text = rule[0], rule[1]
        except (TypeError, IndexError):
            continue
        error_text = str(error_text or "").strip()
        fix_text = str(fix_text or "").strip()
        tokens = re.findall(r"[A-Za-z0-9]+", error_text)
        if not tokens or not fix_text:
            continue
        token_separator = r"[\W_]+"
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + token_separator.join(re.escape(token) for token in tokens)
            + r"(?![A-Za-z0-9_])", re.IGNORECASE,
        )
        match = pattern.search(code_part)
        if match is None:
            continue
        start, end = match.span()
        return {
            "error": error_text,
            "fix": fix_text,
            "matched": match.group(0),
            "suggestion": raw[:start] + fix_text + raw[end:],
        }
    return None


def parse_desc_field(raw_desc: str, f2f_set, fieldbook_path=None, command_set=None, rules=None, commands=None):
    """Parse a raw Description field into code_part / free_desc and classified tokens.
    Handles missing dash/spaces, case-insensitive, no leading strip, trailing strip only on fail.
    Returns dict with keys: raw, code_part, free_desc, code_tokens_classified, free_tokens_classified, flags, flag_detail, parsed_code_str
    Flags: list of strings like UnknownCode:xyz, OrphanCommand:st, MisplacedAfterSeparator:ec2 st
    """
    import re
    if raw_desc is None:
        raw_desc = ""
    raw = str(raw_desc)
    common_conversion_warning = None
    # Resolve the Field Book's description boundary before checking conversion rules, so
    # a rule that affects valid codes is still found when free-text notes follow them.
    try:
        semantic_commands = get_command_map(fieldbook_path, commands)
    except Exception:
        semantic_commands = get_command_map(commands=commands)
    description_token = semantic_commands.get("description", "")
    multicode_token = semantic_commands.get("multicode", "")

    # Correction Rules are reviewable Common Errors in QA, not silent parser rewrites.
    # Match the code portion regardless of whether its source token is currently known;
    # keep the raw description intact so the user can compare and apply the book's fix.
    correction_rule = matching_correction_rule(
        raw, rules=rules, fieldbook_path=fieldbook_path, commands=semantic_commands)
    if correction_rule:
        common_conversion_warning = common_conversion_rule_warning(
            correction_rule["error"], correction_rule["fix"], f2f_set,
            fieldbook_path=fieldbook_path, command_set=command_set,
            commands=semantic_commands)
        common_conversion_warning = common_conversion_warning or (
            f"Field Book common error rule {correction_rule['error']!r} → "
            f"{correction_rule['fix']!r} matches this description. "
            "Review and apply the proposed correction.")

    if command_set is None:
        try:
            command_set = get_command_set(fieldbook_path, commands)
        except Exception:
            command_set = COMMAND_SET
    # Empty description — flag as EmptyDescription
    if not raw.strip():
        return {
            "raw": raw,
            "code_part": "",
            "free_desc": "",
            "has_slash": False,
            "has_description_separator": False,
            "has_multicode_separator": False,
            "code_raw_tokens": [],
            "code_classified": [],
            "free_raw_tokens": [],
            "free_classified": [],
            "flags": ["EmptyDescription"],
            "flag_detail": "Empty description — no code, command, or free text (point will not draw)",
            "parsed_code_str": "",
            "misplaced_sets": [],
        }
    # The Field Book meanings, not punctuation literals, define the two boundaries.
    separator_index = find_separator(raw, description_token)
    has_description_separator = separator_index >= 0
    if has_description_separator:
        parts = split_at_separator(raw, description_token, maxsplit=1)
        code_part = parts[0].strip()
        free_desc = parts[1].strip() if len(parts) > 1 else ""
        description_spacing_error = not separator_spacing_is_valid(
            raw, separator_index, description_token,
            spaced=spacing_preference("space_around_description_separator"),
        )
    else:
        code_part = raw.strip()
        free_desc = ""
        description_spacing_error = False

    # Check every multi-code separator in the code portion; notes may contain punctuation freely.
    multicode_region = raw[:separator_index] if has_description_separator else raw
    multicode_matches = list(re.finditer(separator_pattern(multicode_token), multicode_region,
                                             flags=re.IGNORECASE)) if multicode_token else []
    has_multicode_separator = bool(multicode_matches)
    multicode_spacing_error = any(
        not separator_spacing_is_valid(
            multicode_region, match.start(), multicode_token,
            spaced=spacing_preference("space_around_multicode_separator"),
        )
        for match in multicode_matches
    )
    # Retain the historical result key for callers that have not adopted semantic names yet.
    has_slash = has_description_separator
    # Extract tokens for code part
    code_raw_tokens = _extract_semantic_tokens(code_part, f2f_set, command_set, semantic_commands)
    code_classified = _classify_tokens_sequential(
        code_raw_tokens, f2f_set, command_set=command_set, rules=rules, commands=semantic_commands)
    # Extract tokens for free_desc (for flagging misplaced)
    free_raw_tokens = _extract_semantic_tokens(
        free_desc, f2f_set, command_set, semantic_commands) if has_slash else []
    free_classified = _classify_tokens_sequential(
        free_raw_tokens, f2f_set, command_set=command_set, rules=rules,
        commands=semantic_commands) if free_raw_tokens else []
    flags = []
    details = []
    if common_conversion_warning:
        flags.append("CommonConversionError")
        details.append(common_conversion_warning)
    # Unknown codes in code part
    for item in code_classified:
        if item.get("status") == "unknown":
            flags.append(f"UnknownCode:{item['raw']}")
            details.append(f"UnknownCode '{item['raw']}' not in F2F (tried {item['raw']} → base {item.get('base','')})")
        elif item.get("status") == "orphan":
            flags.append(f"OrphanCommand:{item['raw']}")
            details.append(f"OrphanCommand '{item['raw']}' without preceding code (e.g. ec1 st st → second st, st ec1 st → first st)")
    # Valid feature codes and commands after the description separator are misplaced
    # Group free_desc valid code+command sets as one misplaced set
    # Walk free_classified to find valid code or valid command attached to code
    # For flag, collect contiguous valid sets: code (exact/line_instance) optionally followed by one valid command
    i = 0
    misplaced_sets = []
    while i < len(free_classified):
        item = free_classified[i]
        if item["type"] == "code" and item["status"] in ("exact","line_instance"):
            # start set
            set_tokens = [item["raw"]]
            # check if next is valid command attached to this code
            if i+1 < len(free_classified) and free_classified[i+1]["type"] == "command" and free_classified[i+1].get("status") == "valid" and free_classified[i+1].get("attached_to") == i:
                set_tokens.append(free_classified[i+1]["raw"])
                i += 2
            else:
                i += 1
            misplaced_str = " ".join(set_tokens)
            flags.append(f"MisplacedAfterSeparator:{misplaced_str}")
            details.append(f"Potential code '{misplaced_str}' appears after the description separator — move it before the separator.")
            misplaced_sets.append(misplaced_str)
        elif item["type"] == "command" and item["status"] == "valid":
            # orphan valid command without code in free_desc (should not happen as classify would mark orphan if no code)
            # A command by itself in free text is orphaned, not a misplaced code group.
            flags.append(f"OrphanAfterSeparator:{item['raw']}")
            details.append(f"Orphan command '{item['raw']}' after the description separator without a feature code.")
            i+=1
        elif item["type"] == "command" and item["status"] == "orphan":
            flags.append(f"OrphanAfterSeparator:{item['raw']}")
            details.append(f"Orphan command '{item['raw']}' after the description separator without a preceding feature code.")
            i+=1
        else:
            # unknown or orphan in free_desc: unknown in free_desc is just noise (tree on pavement), not flagged as misplaced unless it's a known code
            # So skip unknown codes in free_desc (they are noise, not F2F)
            i+=1
    # Unknown words in free text are ordinary notes; only recognized codes/commands are flagged.
    if multicode_spacing_error:
        flags.append("SeparatorSpacingError:multicode")
        expectation = "use exactly one space on each side" if spacing_preference("space_around_multicode_separator") else "have no surrounding spaces"
        details.append(f"Spacing around the multi-code separator should {expectation} (see Settings).")
    if has_description_separator and description_spacing_error:
        flags.append("SeparatorSpacingError:description")
        expectation = "use exactly one space on each side" if spacing_preference("space_around_description_separator") else "have no surrounding spaces"
        details.append(f"Spacing around the description separator should {expectation} (see Settings).")
    # Parsed code string for display: rebuild code_part tokens that were valid (exact/line_instance) + valid commands
    parsed_tokens = []
    for item in code_classified:
        if item["status"] in ("exact","line_instance"):
            parsed_tokens.append(item["raw"])
        elif item["status"] == "valid" and item["type"] == "command":
            parsed_tokens.append(item["raw"])
    parsed_code_str = "".join(f"({tok})" for tok in parsed_tokens) if parsed_tokens else ""
    # Also build free_desc display (original)
    return {
        "raw": raw,
        "code_part": code_part,
        "free_desc": free_desc,
        "has_slash": has_slash,
        "has_description_separator": has_description_separator,
        "has_multicode_separator": has_multicode_separator,
        "separator_tokens": {"multicode": multicode_token, "description": description_token},
        "code_raw_tokens": code_raw_tokens,
        "code_classified": code_classified,
        "free_raw_tokens": free_raw_tokens,
        "free_classified": free_classified,
        "flags": flags,
        "flag_detail": "; ".join(details) if details else "",
        "correction_rule": correction_rule,
        "autofix_suggestion": correction_rule.get("suggestion") if correction_rule else None,
        "parsed_code_str": parsed_code_str,
        "misplaced_sets": misplaced_sets,
    }

def common_conversion_rule_warning(common_error: str, fix: str, f2f_set,
                                   fieldbook_path=None, command_set=None, commands=None) -> str | None:
    """Describe a correction rule that collapses a valid multi-code error into one code.

    Rules are evaluated against the Field Book's real separators and command meanings, with
    correction rules disabled to avoid recursively applying the very rule being checked.
    """
    if not common_error or not fix or not f2f_set:
        return None
    try:
        source = parse_desc_field(common_error, f2f_set, fieldbook_path=fieldbook_path,
                                  command_set=command_set, rules=[], commands=commands)
        target = parse_desc_field(fix, f2f_set, fieldbook_path=fieldbook_path,
                                  command_set=command_set, rules=[], commands=commands)
        source_flags = source.get("flags", [])
        target_flags = target.get("flags", [])
        if any(flag.startswith("UnknownCode") for flag in source_flags + target_flags):
            return None

        def code_count(parsed):
            classified = (parsed.get("code_classified", [])
                          + parsed.get("free_classified", []))
            return sum(item.get("type") == "code"
                       and item.get("status") in ("exact", "line_instance")
                       for item in classified)

        before, after = code_count(source), code_count(target)
        if before >= 2 and after == 1:
            return (f"Correction rule {common_error!r} → {fix!r} would collapse "
                    f"{before} valid Field Book codes into one. Review the common-conversion rule "
                    "and preserve any valid codes before accepting this description.")
    except Exception:
        return None
    return None


def _first_line_command_order_violation(meanings, settings=None):
    """Return the first supported pair inversion within one code section.

    ``settings`` remains accepted for compatibility with existing callers, but the check is now
    deliberately limited to the three requested semantic pairs: Start Curve before Start Line,
    End Line before End Curve, and Close before End Curve. A lone command, a repeated start, or an
    unrelated line-state issue is not a LineOrderError.
    """
    violation = first_line_order_pair_violation(meanings)
    if violation is None:
        return None
    bad_index, _counterpart_index, reason = violation
    return bad_index, reason


def _validate_line_command_order(working_rows, f2f_set, settings=None, fieldbook_path=None,
                                 command_set=None, rules=None, commands=None):
    """Check ordered line commands per code occurrence, using Field Book command meanings.

    Code validity is not an ordering prerequisite. Known code occurrences attach commands
    normally; unknown-code sections are checked separately so their command sequence is still
    reviewed without mixing it with adjacent multicode groups.
    """
    if settings is None:
        settings = {}
    if rules is None and fieldbook_path:
        try:
            from .config import get_correction_rules
            rules = get_correction_rules(fieldbook_path)
        except Exception:
            rules = None
    try:
        from .config import get_command_map
        semantic_tokens = get_command_map(fieldbook_path, commands)
    except Exception:
        from ..core.fieldbook_syntax import command_map
        semantic_tokens = command_map(commands)
    if command_set is None:
        try:
            command_set = get_command_set(fieldbook_path, semantic_tokens)
        except Exception:
            command_set = COMMAND_SET
    meanings = command_meanings(semantic_tokens)

    from collections import defaultdict

    oid_flags = defaultdict(list)
    oid_details = defaultdict(list)
    _validate_line_command_order.details = oid_details
    line_meanings = {"start_line", "start_curve", "end_curve", "end_line", "close"}

    for idx, working_row in enumerate(working_rows):
        oid = working_row[0] if len(working_row) > 0 else str(idx)
        raw_desc = working_row[5] if len(working_row) > 5 else ""
        parsed = parse_desc_field(
            raw_desc, f2f_set, fieldbook_path=fieldbook_path, command_set=command_set,
            rules=rules, commands=semantic_tokens)
        classified = parsed["code_classified"]
        commands_by_code_index = defaultdict(list)
        code_raw_by_index = {}
        for item in classified:
            if item["type"] != "command" or item.get("status") != "valid":
                continue
            meaning = item.get("meaning") or meanings.get(item.get("norm", "").casefold(), "")
            if meaning not in line_meanings or "attached_to" not in item:
                continue
            code_index = item["attached_to"]
            code_item = classified[code_index] if 0 <= code_index < len(classified) else None
            if code_item and code_item["type"] == "code":
                # Keep each code occurrence separate, even when the same code appears in
                # multiple multicode sections. Commands are checked only against their owner.
                commands_by_code_index[code_index].append((meaning, item["raw"]))
                code_raw_by_index[code_index] = code_item["raw"]

        for code_index, events in commands_by_code_index.items():
            violation = _first_line_command_order_violation(
                [meaning for meaning, _token in events], settings)
            if violation is None:
                continue
            bad_index, reason = violation
            code_label = code_raw_by_index[code_index]
            observed = " ".join(token for _meaning, token in events)
            bad_token = events[bad_index][1]
            oid_flags[oid].append("LineOrderError")
            oid_details[oid].append(
                f"{reason} at '{bad_token}' in code section '{code_label}' "
                f"(commands: {observed}) — '{raw_desc}'")

        # Known codes are handled above. Also validate each unknown-code section independently:
        # its commands are classified as orphans, but that must not hide an ordering error such
        # as ZZ PC ST. Keep the fallback inside the code portion and split multicode groups first,
        # so note text and neighboring code sections cannot supply the missing commands.
        if (any(item.get("status") == "unknown" for item in classified)
                and any(item.get("type") == "command" for item in classified)):
            code_part = parsed.get("code_part", "")
            multicode = semantic_tokens.get("multicode", "")
            code_segments = split_at_separator(code_part, multicode, maxsplit=0) if multicode else [code_part]
            for segment in code_segments:
                segment_tokens = _extract_semantic_tokens(
                    segment, f2f_set, command_set, semantic_tokens)
                segment_classified = _classify_tokens_sequential(
                    segment_tokens, f2f_set, command_set=command_set, rules=rules,
                    commands=semantic_tokens)
                unknown_commands = defaultdict(list)
                unknown_code_raw = {}
                active_unknown_code = None
                for item_index, item in enumerate(segment_classified):
                    if item.get("type") == "code":
                        active_unknown_code = (item_index if item.get("status") == "unknown" else None)
                        if active_unknown_code is not None:
                            unknown_code_raw[active_unknown_code] = item.get("raw", "")
                        continue
                    if item.get("type") != "command" or active_unknown_code is None:
                        continue
                    meaning = item.get("meaning") or meanings.get(item.get("norm", "").casefold(), "")
                    if meaning in line_meanings:
                        unknown_commands[active_unknown_code].append((meaning, item.get("raw", "")))

                for code_index, events in unknown_commands.items():
                    violation = _first_line_command_order_violation(
                        [meaning for meaning, _token in events], settings)
                    if violation is None:
                        continue
                    bad_index, reason = violation
                    oid_flags[oid].append("LineOrderError")
                    oid_details[oid].append(
                        f"{reason} at '{events[bad_index][1]}' in unrecognized code section "
                        f"'{unknown_code_raw[code_index]}' — '{raw_desc}'")

    return dict(oid_flags)

def build_f2f_set_from_fieldbook(fieldbook_path):
    """Load F2F .fwb and return set of code strings casefolded. Returns empty set if no file."""
    from pathlib import Path
    if not fieldbook_path or not Path(fieldbook_path).exists():
        return set()
    headers, rows = read_fwb_file(Path(fieldbook_path))
    if not headers or not rows:
        return set()
    # Code is column 0
    s = set()
    for r in rows:
        if len(r) > 0 and r[0].strip():
            s.add(r[0].strip().casefold())
    return s

