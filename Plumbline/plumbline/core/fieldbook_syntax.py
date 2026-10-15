"""Semantic definitions for Carlson-style field-book commands.

The stored command values are the office's tokens; application code works against their meanings.
This keeps separator and line-control behavior tied to the active field book instead of scattering
special-character checks throughout the parser and UI.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence


COMMAND_MEANINGS = (
    "start_line",
    "start_curve",
    "end_curve",
    "end_line",
    "close",
    "multicode",
    "description",
)

COMMAND_LABELS = (
    "Start Line",
    "Start Curve",
    "End Curve",
    "End Line",
    "Close",
    "Multi-code separator",
    "Description separator",
)

DEFAULT_COMMAND_TOKENS = ("ST", "PC", "PT", "END", "X", "-", "/")
DEFAULT_COMMAND_MAP = dict(zip(COMMAND_MEANINGS, DEFAULT_COMMAND_TOKENS))

_KEY_ALIASES = {
    "start_line": "start_line",
    "startline": "start_line",
    "start_curve": "start_curve",
    "startcurve": "start_curve",
    "end_curve": "end_curve",
    "endcurve": "end_curve",
    "end_line": "end_line",
    "endline": "end_line",
    "close": "close",
    "multicode": "multicode",
    "multicode_separator": "multicode",
    "multi_code_separator": "multicode",
    "description": "description",
    "description_separator": "description",
}


def _meaning_key(value) -> str:
    return str(value).strip().casefold().replace(" ", "_").replace("-", "_")


def command_map(commands=None) -> dict[str, str]:
    """Normalize a field book's ordered command list or meaning-to-token mapping.

    Historical field books may contain only the five line-control tokens.  Their missing trailing
    separator definitions inherit the Carlson defaults.  An explicitly empty cell in a full-length
    list stays empty, so disabling one meaning cannot shift every later row to a different meaning.
    """
    result = dict(DEFAULT_COMMAND_MAP)
    if commands is None:
        return result

    if isinstance(commands, Mapping):
        semantic_values = {}
        for key, value in commands.items():
            meaning = _KEY_ALIASES.get(_meaning_key(key))
            if meaning:
                semantic_values[meaning] = str(value or "").strip().upper()
        if semantic_values:
            result.update(semantic_values)
        return result

    if isinstance(commands, (str, bytes)) or not isinstance(commands, Sequence):
        return result

    for index, meaning in enumerate(COMMAND_MEANINGS):
        if index < len(commands):
            result[meaning] = str(commands[index] or "").strip().upper()
    return result


def command_token(meaning: str, commands=None) -> str:
    """Token assigned to a semantic command, or an empty string when disabled."""
    return command_map(commands).get(meaning, "")


def command_meanings(commands=None) -> dict[str, str]:
    """Map enabled command tokens (case-insensitively) back to their semantic meanings."""
    return {
        token.casefold(): meaning
        for meaning, token in command_map(commands).items()
        if token
    }


def first_line_order_pair_violation(meanings: Sequence[str]) -> tuple[int, int, str] | None:
    """Return the first of the three supported same-code command-pair inversions.

    ``meanings`` must already be limited to one Field Book code section and kept in source order.
    The checks intentionally do not validate general line completeness, repeated starts, or
    unmatched singleton commands. They only recognize these pair inversions:

    * Start Curve before Start Line;
    * End Line before End Curve; and
    * Close before End Curve.

    The first returned index is the location to report/highlight (the later Start Line for the
    first rule, or the earlier terminator for the other two); the second is its counterpart. A
    new Start Line starts a fresh line segment for the End Line/Close check, while End Line/Close
    ends the prior segment for the Start Curve/Start Line check.
    """
    start_curve_before_line = None
    terminator_before_curve = None

    for index, meaning in enumerate(meanings):
        if meaning == "start_line":
            if start_curve_before_line is not None:
                return index, start_curve_before_line, "Start Curve appears before Start Line"
            # A later End Curve belongs to a new segment after this Start Line, not to an
            # End Line/Close from the preceding segment.
            terminator_before_curve = None
        elif meaning == "start_curve":
            if start_curve_before_line is None:
                start_curve_before_line = index
        elif meaning == "end_line":
            start_curve_before_line = None
            if terminator_before_curve is None:
                terminator_before_curve = index
        elif meaning == "close":
            start_curve_before_line = None
            if terminator_before_curve is None:
                terminator_before_curve = index
        elif meaning == "end_curve" and terminator_before_curve is not None:
            terminal = meanings[terminator_before_curve]
            label = "End Line" if terminal == "end_line" else "Close"
            return (terminator_before_curve, index,
                    f"{label} appears before End Curve")

    return None


def command_token_validation_error(commands) -> str | None:
    """Describe why an edited command list is invalid, or return ``None`` when it is valid."""
    tokens = [str(token or "").strip() for token in commands or ()]
    assigned = [token.casefold() for token in tokens if token]
    if not assigned:
        return "Assign at least one semantic command token."
    if any(character.isspace() for token in tokens for character in token):
        return "Each command token must be a single word or symbol."
    if len(assigned) != len(set(assigned)):
        return "Each semantic command token must be unique."
    return None


def separator_token(meaning: str, commands=None) -> str:
    """Token assigned to a semantic separator, or an empty string when disabled."""
    return command_token(meaning, commands)


def spacing_preference(key: str, default: bool = True) -> bool:
    """Read a persisted spacing preference without making parser imports depend on the UI."""
    try:
        from .settings import settings
        return bool(settings().get(key, default))
    except Exception:
        return default


def separator_text(meaning: str, commands=None, *, spaced: bool | None = None) -> str:
    """Formatted separator for an auto-fix or merged description.

    The separator is selected by its field-book meaning. Spacing is user-configurable and defaults
    to a single blank on either side.
    """
    token = separator_token(meaning, commands)
    if not token:
        return ""
    if spaced is None:
        preference = {
            "multicode": "space_around_multicode_separator",
            "description": "space_around_description_separator",
        }.get(meaning, "space_between_commands")
        spaced = spacing_preference(preference)
    return f" {token} " if spaced else token


def command_joiner(*, spaced: bool | None = None) -> str:
    """Whitespace between a feature-code token and its line-control command."""
    if spaced is None:
        spaced = spacing_preference("space_between_commands")
    return " " if spaced else ""


def separator_pattern(token: str) -> str:
    """Regex for a separator, requiring word boundaries for alphanumeric command tokens."""
    import re

    if not token:
        return r"(?!)"
    left = r"(?<![A-Za-z0-9_])" if token[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_])" if token[-1].isalnum() else ""
    return left + re.escape(token) + right


def find_separator(value: str, token: str) -> int:
    """Find the first separator, case-insensitively, or return -1 when disabled/absent."""
    import re

    if not token:
        return -1
    match = re.search(separator_pattern(token), value, flags=re.IGNORECASE)
    return match.start() if match else -1


def split_at_separator(value: str, token: str, maxsplit: int = 1) -> list[str]:
    """Split on a command token, allowing any amount of surrounding whitespace."""
    import re

    if not token:
        return [value]
    return re.split(rf"\s*{separator_pattern(token)}\s*", value,
                    maxsplit=maxsplit, flags=re.IGNORECASE)


def separator_spacing_is_valid(value: str, start: int, token: str, *, spaced: bool) -> bool:
    """Whether exactly the configured spacing surrounds a separator occurrence."""
    end = start + len(token)
    before = value[start - 1] if start > 0 else ""
    after = value[end] if end < len(value) else ""
    if not spaced:
        return not before.isspace() and not after.isspace()
    before_is_single = before == " " and (start < 2 or value[start - 2] != " ")
    after_is_single = after == " " and (end + 1 >= len(value) or value[end + 1] != " ")
    return before_is_single and after_is_single


def normalize_separator_spacing(value: str, token: str, *, spaced: bool) -> str:
    """Normalize whitespace around every occurrence of one whole command token."""
    import re

    if not token:
        return value
    padding = " " if spaced else ""
    return re.sub(rf"\s*({separator_pattern(token)})\s*",
                  lambda match: f"{padding}{match.group(1)}{padding}",
                  value, flags=re.IGNORECASE)


def normalize_fieldbook_separator_spacing(value: str, commands=None) -> str:
    """Normalize configured separators only, preserving code groups and description text.

    Multi-code spacing is handled only before the first description separator. The description
    separator itself is normalized at its first occurrence so matching text inside a note is not
    mistaken for another delimiter.
    """
    text = str(value or "")
    meanings = command_map(commands)
    multi_token = meanings.get("multicode", "")
    description_token = meanings.get("description", "")
    description_index = find_separator(text, description_token)

    if description_index >= 0:
        description_token_text = text[description_index:description_index + len(description_token)]
        code_region = text[:description_index]
        note_region = text[description_index + len(description_token):]
    else:
        description_token_text = ""
        code_region = text
        note_region = ""

    if multi_token:
        code_region = normalize_separator_spacing(
            code_region, multi_token,
            spaced=spacing_preference("space_around_multicode_separator"),
        )

    if description_index < 0:
        return code_region

    left = code_region.rstrip()
    right = note_region.lstrip()
    padding = " " if spacing_preference("space_around_description_separator") else ""
    return f"{left}{padding}{description_token_text}{padding}{right}"


def trim_separator_edges(value: str, tokens) -> str:
    """Remove whole, configured separator tokens at the edges of a text fragment.

    A separator is stripped only at the very beginning or end; an alphanumeric command such as
    ``NOTE`` cannot accidentally eat the same letters inside ordinary words such as ``noteworthy``.
    Interior spacing and text are preserved for the caller to format as needed.
    """
    import re

    text = str(value or "")
    if isinstance(tokens, (str, bytes)):
        candidates = [tokens]
    else:
        try:
            candidates = list(tokens or ())
        except TypeError:
            candidates = [tokens]
    active = sorted({str(token).strip() for token in candidates if str(token).strip()},
                    key=lambda token: (-len(token), token.casefold()))
    changed = True
    while text and changed:
        changed = False
        for token in active:
            pattern = separator_pattern(token)
            leading = re.sub(rf"^\s*{pattern}\s*", "", text, count=1, flags=re.IGNORECASE)
            if leading != text:
                text, changed = leading, True
            trailing = re.sub(rf"\s*{pattern}\s*$", "", text, count=1, flags=re.IGNORECASE)
            if trailing != text:
                text, changed = trailing, True
    return text.strip()
