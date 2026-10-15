"""Semantic point-description edits for field-to-finish linework.

The command strings and separators come from the active Field Book.  A surveyor can
therefore change the office's spelling without changing how joining, reversing,
closing, or repairing linework behaves.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Sequence

from .featurecodes import BEGIN_FLAGS, CLOSE_FLAGS, END_FLAGS, parse_description
from .fieldbook_syntax import (
    COMMAND_MEANINGS,
    command_joiner,
    command_map,
    command_meanings,
    find_separator,
    first_line_order_pair_violation,
    separator_text,
    split_at_separator,
)

if TYPE_CHECKING:
    from .model import SurveyPoint
    from .project import Project


_LINE_MEANINGS = frozenset(COMMAND_MEANINGS[:5])
_BOUNDARY_MEANINGS = frozenset(("start_line", "end_line", "close"))


def _split_multicode(desc: str, commands=None) -> list[str]:
    token = command_map(commands).get("multicode", "")
    parts = split_at_separator(desc or "", token, maxsplit=0) if token else [desc or ""]
    return [part.strip() for part in parts if part.strip()]


def _join_multicode(parts, commands=None) -> str:
    joiner = separator_text("multicode", commands) or " "
    return joiner.join(str(part).strip() for part in parts if str(part).strip())


def _meaning(token: str, commands=None) -> str:
    meaning = command_meanings(commands).get(str(token).strip().casefold(), "")
    if meaning:
        return meaning
    # Keep the familiar Carlson aliases readable in old point files.  Active command
    # meanings take precedence, so a Field Book can freely rename its function tokens.
    upper = str(token).strip().upper()
    if upper in BEGIN_FLAGS:
        return "start_line"
    if upper in END_FLAGS:
        return "end_line"
    if upper in CLOSE_FLAGS:
        return "close"
    return ""


def _description_parts(part: str, commands=None) -> tuple[str, str, bool]:
    token = command_map(commands).get("description", "")
    index = find_separator(part, token)
    if index < 0:
        return part.strip(), "", False
    return part[:index].strip(), part[index + len(token):].strip(), True


def _format_part(main: str, note: str = "", had_description_separator: bool = False,
                 commands=None) -> str:
    main = re.sub(r"\s+", " ", str(main or "")).strip()
    note = re.sub(r"\s+", " ", str(note or "")).strip()
    if note:
        joiner = separator_text("description", commands)
        return f"{main}{joiner}{note}" if main and joiner else (f"{main} {note}" if main else note)
    if had_description_separator and main:
        joiner = separator_text("description", commands)
        return f"{main}{joiner.rstrip()}" if joiner else main
    return main


def _format_tokens(tokens: Sequence[str], commands=None) -> str:
    if not tokens:
        return ""
    output = str(tokens[0])
    only_commands_so_far = True
    for token in tokens[1:]:
        if _meaning(token, commands) in _LINE_MEANINGS and only_commands_so_far:
            output += command_joiner() + str(token)
        else:
            output += " " + str(token)
            only_commands_so_far = False
    return re.sub(r"\s+", " ", output).strip()


def _code_matches(token: str, prefix: str) -> bool:
    """Match a whole code/string prefix without letting EP1 also select EP10."""
    target = str(prefix or "").strip().casefold()
    value = str(token or "").strip().casefold()
    if not target or not value.startswith(target):
        return False
    suffix = value[len(target):]
    return not (target[-1].isdigit() and suffix and suffix[0].isdigit())


def _rewrite_code_part(desc: str, old_code_prefix: str = "", new_code: str | None = None,
                       boundary: str | None = None, *, commands=None,
                       clear_boundaries: bool = True) -> tuple[str, bool]:
    """Rewrite one code group's name/boundary flags and preserve any note attached to it."""
    semantic_tokens = command_map(commands)
    parts = _split_multicode(desc, semantic_tokens)
    if not parts:
        parts = [""]
    chosen = next((i for i, part in enumerate(parts)
                   if _code_matches(_description_parts(part, semantic_tokens)[0].split()[0]
                                    if _description_parts(part, semantic_tokens)[0].split() else "",
                                    old_code_prefix)), None)
    if chosen is None:
        chosen = 0 if not old_code_prefix and (new_code is not None or boundary is not None) else None
    if chosen is None:
        return desc or "", False

    main, note, had_separator = _description_parts(parts[chosen], semantic_tokens)
    tokens = main.split()
    if not tokens:
        tokens = [str(new_code or old_code_prefix).strip()]
    head = tokens[0]
    rewritten_head = str(new_code).strip() if new_code is not None else head
    rest = tokens[1:]
    if clear_boundaries:
        rest = [token for token in rest if _meaning(token, semantic_tokens) not in _BOUNDARY_MEANINGS]
    if boundary:
        command = semantic_tokens.get(boundary, "")
        if command:
            rest = [token for token in rest if _meaning(token, semantic_tokens) != boundary]
            rest.append(command)
    parts[chosen] = _format_part(
        _format_tokens([rewritten_head, *rest], semantic_tokens), note, had_separator, semantic_tokens)
    return _join_multicode(parts, semantic_tokens), True


def set_point_line_code(desc: str, old_code_prefix: str, new_code: str, commands=None) -> str:
    """Change one multicode section's code token without touching its line commands."""
    new_code = str(new_code or "").strip()
    if not new_code:
        raise ValueError("A line code cannot be blank.")
    updated, changed = _rewrite_code_part(desc or "", old_code_prefix, new_code=new_code,
                                          commands=commands, clear_boundaries=False)
    if not changed:
        raise ValueError(f"Line code '{old_code_prefix}' was not found on this point.")
    return updated


def set_point_line_boundary(desc: str, code_prefix: str, boundary: str | None, commands=None) -> str:
    """Set one code section's ST/END/X boundary while preserving other code sections.

    ``boundary`` accepts semantic names or the active Field Book token.  An empty
    value clears only start/end/close commands, retaining PC/PT and free text.
    """
    semantic_tokens = command_map(commands)
    aliases = {str(token).casefold(): meaning for meaning, token in semantic_tokens.items() if token}
    aliases.update({"st": "start_line", "end": "end_line", "x": "close", "pc": "start_curve", "pt": "end_curve"})
    requested = str(boundary or "").strip()
    meaning = aliases.get(requested.casefold(), requested.casefold()) if requested else None
    if meaning is not None and meaning not in _LINE_MEANINGS:
        raise ValueError("Use ST, END, X, PC, PT, or leave the command blank.")
    updated, changed = _rewrite_code_part(desc or "", code_prefix, boundary=meaning,
                                          commands=semantic_tokens, clear_boundaries=True)
    if not changed:
        raise ValueError(f"Line code '{code_prefix}' was not found on this point.")
    return updated


def _contains_meaning(desc: str, meaning: str, code_prefix: str = "", commands=None) -> bool:
    for part in _split_multicode(desc, commands):
        main, _note, _had_separator = _description_parts(part, commands)
        tokens = main.split()
        if not tokens or (code_prefix and not _code_matches(tokens[0], code_prefix)):
            continue
        if any(_meaning(token, commands) == meaning for token in tokens[1:]):
            return True
    return False


def update_point_token(desc: str, old_code_prefix: str, new_token: str, commands=None) -> str:
    """Replace a code group, or add it using the Field Book's multi-code separator."""
    if not desc:
        return new_token
    updated, replaced = _rewrite_code_part(
        desc, old_code_prefix, new_code=None, commands=commands, clear_boundaries=False)
    if replaced:
        # _rewrite_code_part above preserves the old head; replace the full matching segment
        # with the caller's complete token while retaining any trailing free-text note.
        semantic_tokens = command_map(commands)
        parts = _split_multicode(desc, semantic_tokens)
        for index, part in enumerate(parts):
            main, note, had_separator = _description_parts(part, semantic_tokens)
            tokens = main.split()
            if tokens and _code_matches(tokens[0], old_code_prefix):
                parts[index] = _format_part(new_token, note, had_separator, semantic_tokens)
                break
        return _join_multicode(parts, semantic_tokens)
    parts = _split_multicode(desc, commands)
    parts.append(new_token)
    return _join_multicode(parts, commands)


def remove_point_token(desc: str, code_prefix: str, commands=None) -> str:
    """Remove one coded group without treating the description-note separator as a code split."""
    if not desc:
        return ""
    semantic_tokens = command_map(commands)
    remaining = []
    for part in _split_multicode(desc, semantic_tokens):
        main, note, had_separator = _description_parts(part, semantic_tokens)
        tokens = main.split()
        if tokens and _code_matches(tokens[0], code_prefix):
            if note:
                remaining.append(_format_part("", note, had_separator, semantic_tokens))
            continue
        remaining.append(part)
    return _join_multicode(remaining, semantic_tokens)


def join_points_to_string(points: Sequence[SurveyPoint], code: str,
                          string_id: str = "", closed: bool = False,
                          start_flag: str | None = None, end_flag: str | None = None,
                          commands=None) -> list[SurveyPoint]:
    """Recode points as one line, using the Field Book's Start, End, and Close tokens."""
    if not points:
        return []
    semantic_tokens = command_map(commands)
    start_flag = start_flag if start_flag is not None else semantic_tokens.get("start_line", "")
    end_flag = end_flag if end_flag is not None else semantic_tokens.get("end_line", "")
    closing_flag = semantic_tokens.get("close", "") if closed else end_flag
    prefix = f"{(code or 'EP').strip().upper()}{str(string_id).strip()}".strip()
    modified = []

    for index, point in enumerate(points):
        if len(points) == 1:
            token = prefix
        elif index == 0:
            token = _format_tokens([prefix, start_flag], semantic_tokens) if start_flag else prefix
        elif index == len(points) - 1:
            token = _format_tokens([prefix, closing_flag], semantic_tokens) if closing_flag else prefix
        else:
            token = prefix

        original = point.desc or ""
        old_prefix = ""
        first_part = _split_multicode(original, semantic_tokens)
        if first_part:
            first_main, _note, _had = _description_parts(first_part[0], semantic_tokens)
            first_tokens = first_main.split()
            old_prefix = first_tokens[0] if first_tokens else ""
        if old_prefix and old_prefix.upper().startswith(prefix.rstrip("0123456789").upper()):
            point.desc = update_point_token(original, old_prefix, token, semantic_tokens)
        elif not original:
            point.desc = token
        else:
            point.desc = _join_multicode([token, original], semantic_tokens)
        modified.append(point)
    return modified


def reverse_string_coding(points: Sequence[SurveyPoint], code_prefix: str = "", commands=None) -> list[SurveyPoint]:
    """Reverse line direction, exchanging endpoint meanings and curve start/end meanings."""
    if len(points) < 2:
        return list(points)
    semantic_tokens = command_map(commands)
    points = list(points)
    first, last = points[0], points[-1]
    if not code_prefix:
        segments = _split_multicode(first.desc or "", semantic_tokens)
        main = _description_parts(segments[0], semantic_tokens)[0] if segments else ""
        code_prefix = main.split()[0] if main.split() else "EP"

    was_closed = _contains_meaning(last.desc or "", "close", code_prefix, semantic_tokens)
    first.desc, _ = _rewrite_code_part(
        first.desc or "", code_prefix, boundary="close" if was_closed else "end_line",
        commands=semantic_tokens, clear_boundaries=True)
    last.desc, _ = _rewrite_code_part(last.desc or "", code_prefix, boundary="start_line",
                                      commands=semantic_tokens, clear_boundaries=True)

    for point in points[1:-1]:
        new_parts = []
        for part in _split_multicode(point.desc or "", semantic_tokens):
            main, note, had_separator = _description_parts(part, semantic_tokens)
            tokens = main.split()
            if tokens and _code_matches(tokens[0], code_prefix):
                swapped = []
                for token in tokens[1:]:
                    meaning = _meaning(token, semantic_tokens)
                    if meaning == "start_curve":
                        swapped.append(semantic_tokens.get("end_curve", token))
                    elif meaning == "end_curve":
                        swapped.append(semantic_tokens.get("start_curve", token))
                    else:
                        swapped.append(token)
                main = _format_tokens([tokens[0], *swapped], semantic_tokens)
            new_parts.append(_format_part(main, note, had_separator, semantic_tokens))
        point.desc = _join_multicode(new_parts, semantic_tokens)
    return points[::-1]


def start_string_coding(first_point: SurveyPoint, code_prefix: str = "", commands=None) -> SurveyPoint:
    """Apply the Field Book's Start Line meaning to the selected line's first point."""
    first_point.desc, _ = _rewrite_code_part(
        first_point.desc or "", code_prefix, boundary="start_line", commands=commands,
        clear_boundaries=True)
    return first_point


def close_string_coding(last_point: SurveyPoint, code_prefix: str = "", commands=None) -> SurveyPoint:
    """Apply the Field Book's Close meaning to the selected line's endpoint."""
    last_point.desc, _ = _rewrite_code_part(
        last_point.desc or "", code_prefix, boundary="close", commands=commands, clear_boundaries=True)
    return last_point


def open_string_coding(last_point: SurveyPoint, code_prefix: str = "", commands=None) -> SurveyPoint:
    """Apply the Field Book's End Line meaning to open the selected endpoint."""
    last_point.desc, _ = _rewrite_code_part(
        last_point.desc or "", code_prefix, boundary="end_line", commands=commands, clear_boundaries=True)
    return last_point


def split_string_coding(points_before: Sequence[SurveyPoint], points_after: Sequence[SurveyPoint],
                        new_string_id: str = "2", commands=None) -> tuple[list[SurveyPoint], list[SurveyPoint]]:
    """End the first sequence and start a newly numbered sequence at the split."""
    if not points_before or not points_after:
        return list(points_before), list(points_after)
    semantic_tokens = command_map(commands)
    end_point, start_point = points_before[-1], points_after[0]
    end_point.desc, _ = _rewrite_code_part(
        end_point.desc or "", boundary="end_line", commands=semantic_tokens, clear_boundaries=True)

    parts = _split_multicode(start_point.desc or "", semantic_tokens)
    main = _description_parts(parts[0], semantic_tokens)[0] if parts else ""
    raw_code = main.split()[0] if main.split() else "EP"
    parsed = parse_description(raw_code, commands=semantic_tokens)
    base_code = parsed.code or raw_code
    new_prefix = f"{base_code}{new_string_id}"

    for index, point in enumerate(points_after):
        boundary = "start_line" if index == 0 else (
            "end_line" if index == len(points_after) - 1 else None)
        point.desc, _ = _rewrite_code_part(
            point.desc or "", base_code, new_code=new_prefix, boundary=boundary,
            commands=semantic_tokens, clear_boundaries=True)
    return list(points_before), list(points_after)


def change_string_code(points: Sequence[SurveyPoint], old_code_prefix: str, new_code_prefix: str,
                       commands=None) -> list[SurveyPoint]:
    """Change a code in one or more multi-code groups, preserving commands and notes."""
    semantic_tokens = command_map(commands)
    old_prefix = old_code_prefix.strip().upper()
    new_prefix = new_code_prefix.strip().upper()
    modified = []
    for point in points:
        changed = False
        parts = []
        for part in _split_multicode(point.desc or "", semantic_tokens):
            main, note, had_separator = _description_parts(part, semantic_tokens)
            tokens = main.split()
            if tokens and not changed and _code_matches(tokens[0], old_prefix):
                tokens[0] = new_prefix
                changed = True
                main = _format_tokens(tokens, semantic_tokens)
                parts.append(_format_part(main, note, had_separator, semantic_tokens))
            else:
                parts.append(part)
        if not changed and not parts:
            parts = [new_prefix]
        point.desc = _join_multicode(parts, semantic_tokens)
        modified.append(point)
    return modified


def find_free_string_id(project: Project, base_code: str) -> str:
    """Find the next available string number for a code in any multi-code group."""
    used_ids = set()
    target = str(base_code or "").strip().casefold()
    settings = getattr(project, "settings", {}) or {}
    semantic_tokens = command_map(settings.get("f2f_commands"))
    known_codes = getattr(getattr(project, "codes", None), "codes", {})
    for point in project.points.values():
        for part in _split_multicode(point.desc or "", semantic_tokens):
            main, _note, _had_separator = _description_parts(part, semantic_tokens)
            parsed = parse_description(main, commands=semantic_tokens, known_codes=known_codes)
            if parsed.code.casefold() == target and parsed.string.isdigit():
                used_ids.add(int(parsed.string))
    candidate = 1
    while candidate in used_ids:
        candidate += 1
    return str(candidate)


def reorder_string_points(points: Sequence[SurveyPoint], new_order: Sequence[int], closed: bool = False,
                          commands=None) -> list[SurveyPoint]:
    """Reorder a figure and rewrite only its line-boundary meanings."""
    if not points or not new_order or len(points) != len(new_order):
        return list(points)
    semantic_tokens = command_map(commands)
    reordered = [points[index] for index in new_order]
    parts = _split_multicode(reordered[0].desc or "", semantic_tokens)
    main = _description_parts(parts[0], semantic_tokens)[0] if parts else ""
    code_prefix = main.split()[0] if main.split() else "EP"
    for index, point in enumerate(reordered):
        boundary = "start_line" if index == 0 else (
            ("close" if closed else "end_line") if index == len(reordered) - 1 else None)
        point.desc, _ = _rewrite_code_part(
            point.desc or "", code_prefix, boundary=boundary, commands=semantic_tokens,
            clear_boundaries=True)
    return reordered


def merge_and_reclass_strings(project: Project, string1_points: Sequence[SurveyPoint],
                              string2_points: Sequence[SurveyPoint], target_code: str = "",
                              new_string_id: str | None = None, closed: bool = False,
                              commands=None) -> list[SurveyPoint]:
    """Merge two line strings under one code/string ID, preserving non-boundary commands."""
    if not string1_points and not string2_points:
        return []
    if not string1_points:
        return list(string2_points)
    if not string2_points:
        return list(string1_points)

    semantic_tokens = command_map(commands if commands is not None else
                                  (getattr(project, "settings", {}) or {}).get("f2f_commands"))
    points = list(string1_points) + list(string2_points)
    if not target_code:
        parts = _split_multicode(points[0].desc or "", semantic_tokens)
        main = _description_parts(parts[0], semantic_tokens)[0] if parts else ""
        raw_code = main.split()[0] if main.split() else "EP"
        target_code = parse_description(raw_code, commands=semantic_tokens).code or raw_code
    if new_string_id is None:
        new_string_id = find_free_string_id(project, target_code)
    new_prefix = f"{target_code}{new_string_id}"

    for index, point in enumerate(points):
        boundary = "start_line" if index == 0 else (
            ("close" if closed else "end_line") if index == len(points) - 1 else None)
        parts = _split_multicode(point.desc or "", semantic_tokens)
        main = _description_parts(parts[0], semantic_tokens)[0] if parts else ""
        old_code = main.split()[0] if main.split() else target_code
        point.desc, _ = _rewrite_code_part(
            point.desc or "", old_code, new_code=new_prefix, boundary=boundary,
            commands=semantic_tokens, clear_boundaries=True)
    return points


def swap_parallel_line_codes(points: Sequence[SurveyPoint], code_prefix1: str, code_prefix2: str,
                             commands=None) -> list[SurveyPoint]:
    """Swap the two requested code groups while retaining Field Book separators and commands."""
    first = code_prefix1.strip().upper()
    second = code_prefix2.strip().upper()
    semantic_tokens = command_map(commands)
    modified = []
    for point in points:
        changed = False
        new_parts = []
        for part in _split_multicode(point.desc or "", semantic_tokens):
            main, note, had_separator = _description_parts(part, semantic_tokens)
            tokens = main.split()
            if tokens and _code_matches(tokens[0], first):
                tokens[0] = second
                changed = True
                new_parts.append(_format_part(_format_tokens(tokens, semantic_tokens), note,
                                              had_separator, semantic_tokens))
            elif tokens and _code_matches(tokens[0], second):
                tokens[0] = first
                changed = True
                new_parts.append(_format_part(_format_tokens(tokens, semantic_tokens), note,
                                              had_separator, semantic_tokens))
            else:
                new_parts.append(part)
        if changed:
            point.desc = _join_multicode(new_parts, semantic_tokens)
            modified.append(point)
    return modified


def fix_line_command_order(desc: str, commands=None) -> str:
    """Correct only the three supported semantic pair inversions in each code section."""
    if not desc:
        return ""
    semantic_tokens = command_map(commands)
    description_token = semantic_tokens.get("description", "")
    description_index = find_separator(desc, description_token)
    if description_index >= 0:
        code_text = desc[:description_index].strip()
        note = desc[description_index + len(description_token):].strip()
    else:
        code_text, note = desc.strip(), ""

    fixed_groups = []
    any_changed = False
    for part in _split_multicode(code_text, semantic_tokens):
        tokens = part.split()
        if len(tokens) <= 1:
            fixed_groups.append(part)
            continue

        code, command_tokens = tokens[0], tokens[1:]
        meanings = [_meaning(token, semantic_tokens) for token in command_tokens]
        part_changed = False
        # Each swap removes the first forbidden pair while leaving every other token and code
        # section in place. The cap is defensive; ordinary inputs converge after one swap per pair.
        for _ in range(max(1, len(command_tokens) ** 2)):
            violation = first_line_order_pair_violation(meanings)
            if violation is None:
                break
            first_index, second_index, _reason = violation
            if first_index == second_index:
                break
            command_tokens[first_index], command_tokens[second_index] = (
                command_tokens[second_index], command_tokens[first_index])
            meanings[first_index], meanings[second_index] = meanings[second_index], meanings[first_index]
            part_changed = True
        if part_changed:
            fixed_groups.append(_format_tokens([code, *command_tokens], semantic_tokens))
            any_changed = True
        else:
            fixed_groups.append(part)

    if not any_changed:
        return desc
    fixed = _join_multicode(fixed_groups, semantic_tokens)
    if note:
        return _format_part(fixed, note, True, semantic_tokens)
    return fixed
