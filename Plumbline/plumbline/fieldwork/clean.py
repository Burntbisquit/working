"""
clean.py — Clean Descriptions feature (2026-09-20)

Provides CleanDescriptionDialog (4 tools) + renumber lock behind cleaned state.

Flag types: UnknownCode, OrphanCommand, MisplacedAfterSeparator, LineOrderError, EmptyDescription

Tools (dialog buttons):
  1. Fix Error — replace with best guess (closest F2F code)
  2. Key-In Fix Token — user enters replacement token + validate
  3. Remove Point — delete point from consolidated file (before Skip/Ignore)
  4. Next (Skip) — leave unresolved, move to next flagged
  5. Ignore — mark as Ignored at end, counts as handled
  6. Key-In Entire Code — full rewrite (e.g., asldf→NG validates and clears dependents)
  Nav: Previous / Next on bottom row (sequential clean)

Number assignment: Control 1-999, Boundary 1000-9999, General 10000+, crew blocks 300/3000/30000...
Renumber locked behind cleaned descriptions (all flags fixed/skipped or no flagged rows).

Usage: MainWindow double-clicks Description Error row → opens dialog → applies edit to working file.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QMessageBox, QGroupBox, QFormLayout,
    QWidget, QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QSplitter
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor

from .config import (crew_blocks, CONTROL_RANGE, BOUNDARY_RANGE, GENERAL_START,
                    get_command_map, get_command_set, get_separator_texts, get_separator_tokens)
from ..core.fieldbook_syntax import (command_joiner, find_separator, normalize_separator_spacing,
                                     spacing_preference, split_at_separator, trim_separator_edges)
from .parse import (parse_desc_field, _validate_line_command_order, build_f2f_set_from_fieldbook,
                    _strip_trailing_digits, common_conversion_rule_warning)
from .detectors import normalize_point_number

import re


def _trim_separator_edges(text: str, tokens) -> str:
    """Remove whole active separator tokens from the edges of a free-text fragment."""
    return trim_separator_edges(text, tokens)


def _split_concatenated_code_command(token: str, f2f_set: set, command_set=None,
                                     fieldbook_path=None, commands=None) -> list[str]:
    """Split a known feature code from an active command token when no space was entered.

    Returns the resolved ``[code, command]`` pair when valid, otherwise ``[token]``.
    """
    try:
        if not token or not f2f_set:
            return [token]
        low = token.casefold()
        cs = command_set if command_set is not None else get_command_set(fieldbook_path, commands)
        separator_set = {token.casefold() for token in get_separator_tokens(fieldbook_path, commands).values() if token}
        for cmd in sorted(cs, key=len, reverse=True):
            if str(cmd).casefold() in separator_set:
                continue
            if low.endswith(cmd):
                prefix = token[:len(token)-len(cmd)]
                if not prefix:
                    continue
                if prefix.casefold() in f2f_set:
                    return [prefix, cmd.upper()]
                base, num = _strip_trailing_digits(prefix)
                if base and base.casefold() in f2f_set:
                    return [prefix, cmd.upper()]
        return [token]
    except Exception:
        return [token]

def _extract_tokens_with_split(code_part: str, f2f_set: set = None, command_set=None,
                               fieldbook_path=None, commands=None):
    """Extract tokens but also split concatenated code+command like TOC1ST."""
    from .parse import _extract_tokens as _et
    raw = _et(code_part)
    out = []
    for tok in raw:
        if f2f_set is not None:
            split = _split_concatenated_code_command(tok, f2f_set, command_set=command_set,
                                                     fieldbook_path=fieldbook_path, commands=commands)
            if len(split) == 2:
                out.extend(split)
            else:
                out.append(tok)
        else:
            out.append(tok)
    return out

def _best_guess_for_token(bad_token: str, f2f_set: set, fieldbook_path=None) -> str | None:
    """Best guess for UnknownCode: strip trailing digits then find closest case-insensitive match.
    For tree codes, preserve inches (trailing digits) from bad_token: TW015 -> TWO15 not TWO02.
    Assumes inches are near the code being fixed (the number suffix).
    Handles casefolded f2f_set (from build_f2f_set_from_fieldbook) by returning fieldbook canonical case.
    """
    if not bad_token or not f2f_set:
        return None
    low_map = {c.lower(): c for c in f2f_set}
    # Try to build canonical map from fieldbook if available to recover original case (fieldbook stores upper)
    canonical_map = {}
    if fieldbook_path:
        try:
            from pathlib import Path
            from .io_carlson import read_fwb_file
            if Path(fieldbook_path).exists():
                h, rows = read_fwb_file(Path(fieldbook_path))
                for r in rows:
                    if r and r[0].strip():
                        code = r[0].strip()
                        canonical_map[code.lower()] = code
        except Exception:
            pass
    # Helper to return canonical case
    def _canonical(code):
        if not code:
            return code
        low = code.lower()
        if low in canonical_map:
            return canonical_map[low]
        # Fallback: if f2f_set was casefolded, the stored value is lower — return upper as fieldbook canonical is typically upper
        # Check if upper version maps to same lower (i.e., code is in F2F case-insensitively)
        if low in low_map:
            stored = low_map[low]
            # If stored is all lower but fieldbook likely upper, return upper
            if stored.islower() and stored.upper().lower() == low:
                # Prefer upper if bad_token was upper or if stored is lower
                return stored.upper()
            return stored
        return code
    if bad_token.lower() in low_map:
        return _canonical(low_map[bad_token.lower()])
    # Split trailing digits (inches) — preserve them
    base_bad, num_bad = _strip_trailing_digits(bad_token)
    # Normalize num for matching (strip leading zeros for comparison, but preserve original for reconstruction)
    num_norm = num_bad.lstrip('0') if num_bad else ""
    # If base part exists and has inches, try to find best base and reattach inches
    if num_bad and base_bad:
        # Check if base without digits directly matches a code base (e.g., for line codes)
        if base_bad.lower() in low_map:
            # If F2F has base itself, return base + num (even if not in F2F, we preserve inches)
            candidate_with_num = low_map[base_bad.lower()] + num_bad
            # Check if that exact candidate exists (case-insensitive)
            if candidate_with_num.lower() in low_map:
                return low_map[candidate_with_num.lower()]
            # Also try normalized num without leading zeros
            cand_norm = low_map[base_bad.lower()] + num_norm if num_norm else low_map[base_bad.lower()]
            if cand_norm and cand_norm.lower() in low_map:
                return low_map[cand_norm.lower()]
            # Fallback: preserve inches and return base guess + num (user wants inches preserved)
            # But try to find F2F code that matches base + num_norm
            # Search for codes with same base prefix and same num
            for c in f2f_set:
                cb, cn = _strip_trailing_digits(c)
                if cb.casefold() == base_bad.casefold() and cn.lstrip('0') == num_norm:
                    return c
            # If not found, return base + num as best we can (even if not in F2F, caller will validate)
            # But prefer to return base+num that is closest to existing code length
            return _canonical(candidate_with_num)
        # For tree codes where base + num is the whole code (e.g., TOAK02), base without digits not in F2F
        # Build map of base -> example code for prefix matching
        # Find candidate bases: bases of F2F codes that start with base_bad prefix
        # Collect unique bases (stripped) of F2F codes
        f2f_bases = {}
        for c in f2f_set:
            cb, cn = _strip_trailing_digits(c)
            key = cb.casefold()
            if key not in f2f_bases:
                f2f_bases[key] = cb  # original case
        # Find bases that start with base_bad[:2] or full base_bad
        prefix = base_bad[:2].lower() if len(base_bad) >= 2 else base_bad.lower()
        candidates_bases = [b for b in f2f_bases.values() if b.lower().startswith(prefix) or b.lower().startswith(base_bad.lower())]
        # More precise: bases that share first 2 letters
        if not candidates_bases and len(base_bad) >= 2:
            candidates_bases = [b for b in f2f_bases.values() if b.lower()[:2] == base_bad.lower()[:2]]
        if candidates_bases:
            # Sort by exact base match first, then length diff then alphabetical (preserve inches)
            candidates_bases.sort(key=lambda b: (0 if b.casefold()==base_bad.casefold() else 1, abs(len(b)-len(base_bad)), b))
            for cand_base in candidates_bases:
                # Try to construct code with preserved inches
                # Use same num formatting as original (preserve leading zeros? F2F codes use no leading zero for 2-digit inches, e.g., TWO15 not TWO015)
                # Try both original num and normalized num (strip leading zero)
                for try_num in (num_bad, num_norm, num_bad.lstrip('0').zfill(2) if num_norm else ""):
                    if not try_num:
                        continue
                    cand_code = cand_base + try_num
                    if cand_code.lower() in low_map:
                        return _canonical(low_map[cand_code.lower()])
                    # Also try zero-padded 2-digit
                    if len(try_num)==1:
                        cand_code2 = cand_base + "0" + try_num
                        if cand_code2.lower() in low_map:
                            return _canonical(low_map[cand_code2.lower()])
                # If no exact match with this base, try any code with that base that has same numeric value (e.g., TWO15 vs TWO015)
                for c in f2f_set:
                    cb, cn = _strip_trailing_digits(c)
                    if cb.casefold() == cand_base.casefold() and cn.lstrip('0') == num_norm:
                        return _canonical(c)
            # If none matched with same inches, fallback to first candidate base + normalized inches (preserve inches even if not exact)
            best_base = candidates_bases[0]
            cand_fallback = best_base + (num_norm if num_norm else num_bad)
            # Prefer returning exact F2F code if fallback exists, else return fallback string (caller will check validate)
            if cand_fallback.lower() in low_map:
                return _canonical(low_map[cand_fallback.lower()])
            return _canonical(cand_fallback)
    # Fallback non-numeric: original logic
    stripped = re.sub(r'\d+$', '', bad_token)
    low_map = {c.lower(): c for c in f2f_set}
    if stripped.lower() in low_map:
        return _canonical(low_map[stripped.lower()])
    candidates = [c for c in f2f_set if c.lower().startswith(stripped[:2].lower())] if len(stripped) >= 2 else []
    if candidates:
        candidates.sort(key=lambda c: (abs(len(c)-len(bad_token)), c))
        return _canonical(candidates[0])
    return None

def _autocorrect_desc(raw_desc: str, f2f_set: set, fieldbook_path=None, rules=None, commands=None) -> str | None:
    """Repair a description with the active Field Book's meanings and separator tokens.

    Field Book correction rules run first, followed by code matching and command-order repair.
    The selected meaning-to-token map controls both line commands and separator placement.
    Returns a changed description only when it parses without an unknown code or orphan command.
    """
    if not raw_desc or not f2f_set:
        return None
    multicode_sep, description_sep = get_separator_texts(fieldbook_path, commands)
    multicode_sep = multicode_sep or " "
    description_sep = description_sep or " "
    separator_tokens = get_separator_tokens(fieldbook_path, commands)
    description_token = separator_tokens.get("description", "")
    command_glue = command_joiner()
    # Check rules first — exact match on raw_desc (case-insensitive, separator-spacing tolerant)
    if rules is None and fieldbook_path:
        try:
            from .config import get_correction_rules
            rules = get_correction_rules(fieldbook_path)
        except Exception:
            rules = None
    if rules:
        raw_stripped = raw_desc.strip()
        for err, fix in rules:
            if not err or not fix:
                continue
            if raw_stripped.casefold() == err.casefold():
                warning = common_conversion_rule_warning(
                    err, fix, f2f_set, fieldbook_path=fieldbook_path, commands=commands)
                if warning:
                    return None  # do not offer a known lossy auto-fix
                return fix
            # Also handle normalized dash/space: compare tokens
            # e.g., err "EC" with fix "EC1" and raw "ec" -> fix
            # For substring, if err is single token and raw contains it as token, replace that token
            # Simple token-level replace
            import re as _re2
            if _re2.fullmatch(r'[A-Za-z0-9]+', err.strip()):
                # Single token rule — replace that token wherever it appears (case-insensitive, word-bound)
                if _re2.search(rf'\b{re.escape(err.strip())}\b', raw_stripped, flags=re.I):
                    # Use fix as replacement for that token (preserve dash/space around)
                    return _re2.sub(rf'\b{re.escape(err.strip())}\b', fix, raw_stripped, count=1, flags=re.I)
    # Early handling for a feature code concatenated directly with an active command token.
    try:
        if f2f_set and raw_desc:
            import re as _re2
            # The active book controls both line commands and separator meanings.
            cs_for_split = get_command_set(fieldbook_path, commands)
            # Try to find concatenated token and insert space
            for cmd in sorted(cs_for_split, key=len, reverse=True):
                if str(cmd).casefold() in {token.casefold() for token in separator_tokens.values() if token}:
                    continue
                # Pattern: word chars ending with command without space, and prefix is code-like
                # Match a code-like prefix immediately followed by this active command token.
                pat = re.compile(r'\b([A-Za-z]+\d*)(' + re.escape(cmd) + r')\b', re.I)
                m = pat.search(raw_desc)
                if m:
                    prefix = m.group(1)
                    # Check if prefix is a code (or code base)
                    low_pre = prefix.casefold()
                    is_code = False
                    if low_pre in f2f_set:
                        is_code = True
                    else:
                        base, num = _strip_trailing_digits(prefix)
                        if base and base.casefold() in f2f_set:
                            is_code = True
                    if is_code:
                        # Separate the resolved feature code and active command token.
                        corrected_split = pat.sub(r'\1' + command_glue + cmd.upper(), raw_desc, count=1)
                        if corrected_split != raw_desc:
                            # Validate corrected has no UnknownCode for this part
                            try:
                                from .parse import parse_desc_field as _pdf2
                                chk = _pdf2(corrected_split, f2f_set, fieldbook_path=fieldbook_path, command_set=cs_for_split, rules=rules, commands=commands)
                                if not any("UnknownCode" in f for f in chk.get("flags", [])):
                                    return corrected_split
                            except:
                                return corrected_split
    except Exception:
        pass
    try:
        from .parse import parse_desc_field, _extract_tokens, _classify_tokens_sequential
        from .config import COMMAND_SET
        # Use custom command set if fieldbook has one
        cmd_set = get_command_set(fieldbook_path, commands)
        parsed = parse_desc_field(raw_desc, f2f_set, fieldbook_path=fieldbook_path, command_set=cmd_set, rules=rules, commands=commands)
        if any("CommonConversionError" in flag for flag in parsed.get("flags", [])):
            return None  # preserve the valid multi-code source until a user reviews the rule
        # Only attempt if has UnknownCode, Orphan, MisplacedAfterSeparator, or SeparatorSpacingError
        has_unknown = any("UnknownCode" in f for f in parsed.get("flags", []))
        has_orphan = any("OrphanCommand" in f for f in parsed.get("flags", []))
        has_misplaced = any("MisplacedAfterSeparator" in f for f in parsed.get("flags", []))
        has_slash_err = any("SeparatorSpacingError" in f for f in parsed.get("flags", []))
        if not has_unknown and not has_orphan and not has_misplaced and not has_slash_err:
            return None
        # Handle MisplacedAfterSeparator first: pull misplaced code(s) into description by appending multicode_sep + misplaced + free desc
        if has_misplaced:
            code_part = parsed.get("code_part", "").strip()
            free_desc = parsed.get("free_desc", "").strip()
            misplaced_sets = parsed.get("misplaced_sets", []) or []
            # Also collect individual misplaced tokens if sets empty but flag exists
            if not misplaced_sets:
                # fallback: extract from flags
                for f in parsed.get("flags", []):
                    if f.startswith("MisplacedAfterSeparator:"):
                        ms = f.split(":",1)[1].strip()
                        if ms:
                            misplaced_sets.append(ms)
            if misplaced_sets:
                # Build corrected code part
                if code_part:
                    corrected_code = code_part + multicode_sep + multicode_sep.join(misplaced_sets)
                else:
                    corrected_code = multicode_sep.join(misplaced_sets)
                # Remove misplaced codes from free_desc to get remaining free text
                remaining = free_desc
                import re as _re_mis
                for ms in misplaced_sets:
                    toks = ms.split()
                    if len(toks) == 1:
                        pat = r'\b' + _re_mis.escape(toks[0]) + r'\b'
                    else:
                        pat = r'\b' + r'\s+'.join(map(_re_mis.escape, toks)) + r'\b'
                    remaining = _re_mis.sub(pat, '', remaining, count=1, flags=re.I)
                    remaining = _re_mis.sub(r'\s{2,}', ' ', remaining).strip()
                    remaining = _trim_separator_edges(remaining, separator_tokens.values())
                    remaining = _re_mis.sub(r'\s+', ' ', remaining).strip()
                remaining = _trim_separator_edges(remaining, separator_tokens.values())
                if remaining:
                    corrected = f"{corrected_code}{description_sep}{remaining}" if corrected_code else remaining
                else:
                    corrected = corrected_code
                if corrected and corrected.strip() != raw_desc.strip():
                    # Validate corrected has no misplaced and no unknown for the moved codes
                    try:
                        check = parse_desc_field(corrected, f2f_set, fieldbook_path=fieldbook_path, command_set=cmd_set, rules=rules, commands=commands)
                        if not any("MisplacedAfterSeparator" in f for f in check.get("flags", [])):
                            return corrected
                    except Exception:
                        return corrected
        # Normalize spacing around the Field Book's separators using the Settings preferences.
        if any("SeparatorSpacingError" in flag for flag in parsed.get("flags", [])):
            corrected_spacing = raw_desc
            multicode_token = separator_tokens.get("multicode", "")
            description_token = separator_tokens.get("description", "")
            if multicode_token:
                description_index = find_separator(corrected_spacing, description_token)
                code_region = corrected_spacing[:description_index] if description_index >= 0 else corrected_spacing
                remainder = corrected_spacing[description_index:] if description_index >= 0 else ""
                code_region = normalize_separator_spacing(
                    code_region, multicode_token,
                    spaced=spacing_preference("space_around_multicode_separator"))
                corrected_spacing = code_region + remainder
            if description_token:
                corrected_spacing = normalize_separator_spacing(
                    corrected_spacing, description_token,
                    spaced=spacing_preference("space_around_description_separator"))
            if corrected_spacing.strip() != raw_desc.strip():
                check_spacing = parse_desc_field(
                    corrected_spacing, f2f_set, fieldbook_path=fieldbook_path,
                    command_set=cmd_set, rules=rules, commands=commands)
                if not any("SeparatorSpacingError" in flag for flag in check_spacing.get("flags", [])):
                    return corrected_spacing
        
        # Work on code_part + free_desc handling: if misplaced after slash, free tokens are ignored — we fix code_part only
        code_part = parsed.get("code_part", raw_desc)
        # Use classified to rebuild
        cl = parsed.get("code_classified", [])
        if not cl:
            # Fallback: extract tokens directly from raw_desc and try to map each to best guess
            toks = _extract_tokens_with_split(raw_desc, f2f_set, cmd_set, fieldbook_path, commands) if cmd_set is not None else _extract_tokens(raw_desc)
            if not toks:
                return None
            # Try to map each tok
            mapped = []
            changed=False
            for tok in toks:
                low = tok.casefold()
                if low in f2f_set or low in cmd_set:
                    mapped.append(tok)
                else:
                    guess = _best_guess_for_token(tok, f2f_set, fieldbook_path=fieldbook_path)
                    if guess:
                        mapped.append(guess)
                        changed=True
                    else:
                        mapped.append(tok)
            if not changed:
                return None
            # Simple join with ' - ' between code-like tokens, commands with space
            # Heuristic: group code + following commands
            # For now join with ' - ' if multiple codes, else space
            # Use parse to validate groups
            return multicode_sep.join(mapped) if len(mapped)>1 else mapped[0]

        # Build corrected token list with best guesses for unknowns
        new_items = []  # list of dicts with raw corrected
        changed=False
        for item in cl:
            if item.get("status") == "unknown":
                guess = _best_guess_for_token(item.get("raw",""), f2f_set, fieldbook_path=fieldbook_path)
                if guess:
                    new_items.append({"raw": guess, "type": "code", "status": "exact"})
                    changed=True
                else:
                    new_items.append(item)
            else:
                new_items.append(item)

        if not changed:
            # No unknown could be guessed — try to pull valid codes out and preserve free text
            # Build groups from valid code+command items using the configured multi-code separator.
            # Preserve unknown free text as a note when it follows a valid code group.
            valid_groups=[]
            cur=[]
            unknown_texts=[]
            for it in cl:
                if it.get("type")=="code" and it.get("status") in ("exact","line_instance"):
                    if cur:
                        valid_groups.append(command_glue.join(cur))
                    cur=[it["raw"]]
                elif it.get("type")=="command" and it.get("status")=="valid":
                    if cur:
                        cur.append(it["raw"])
                    else:
                        if cur:
                            valid_groups.append(command_glue.join(cur))
                        valid_groups.append(it["raw"])
                        cur=[]
                else:
                    # unknown/orphan — collect as potential free text (e.g., UNDER ROOTS)
                    if cur:
                        valid_groups.append(command_glue.join(cur))
                        cur=[]
                    # collect unknown token for free text preservation
                    unknown_texts.append(it.get("raw",""))
            if cur:
                valid_groups.append(command_glue.join(cur))
            if valid_groups:
                corrected_code = multicode_sep.join(valid_groups)
                free_desc = parsed.get("free_desc","")
                has_slash = parsed.get("has_slash", False)
                # Combine unknown_texts as free text if they look like free description (not close to F2F)
                unknown_free = " ".join(unknown_texts).strip() if unknown_texts else ""
                # If we have unknown free text and no slash originally, propose description_sep version
                # Simple: if unknown_free exists, treat it as free description
                if unknown_free:
                    # Build two candidates: one without free (drop), one with free as description_sep
                    # Prefer the one with free if it validates clean
                    # First try with free
                    try:
                        # Combine original free_desc and unknown_free
                        combined_free = " ".join(filter(None, [unknown_free, free_desc])).strip()
                        corrected_with_free = f"{corrected_code}{description_sep}{combined_free}" if corrected_code and combined_free else (corrected_code or combined_free)
                        if corrected_with_free and corrected_with_free.strip() != raw_desc.strip():
                            check_free = parse_desc_field(corrected_with_free, f2f_set, fieldbook_path=fieldbook_path, command_set=cmd_set, rules=rules, commands=commands)
                            # Check if corrected_with_free has no UnknownCode (since unknown moved to free, it should be clean)
                            if not any("UnknownCode" in f for f in check_free.get("flags", [])):
                                # Also ensure we are not creating a new Misplaced issue
                                return corrected_with_free
                    except Exception:
                        pass
                # Fallback: without free (original drop behavior)
                if has_slash and free_desc:
                    corrected = f"{corrected_code}{description_sep}{free_desc}" if corrected_code else free_desc
                else:
                    corrected = corrected_code
                if corrected and corrected.strip() != raw_desc.strip():
                    check2 = parse_desc_field(corrected, f2f_set, fieldbook_path=fieldbook_path, command_set=cmd_set, rules=rules, commands=commands)
                    if not any("UnknownCode" in f for f in check2.get("flags", [])):
                        return corrected
            # If only orphan, we can't fix without reordering — leave for line order fix
            return None

        # Rebuild groups: code + its attached valid commands -> group string
        # Need to reconstruct attachment based on new_items order
        # Simulate classify again for new token raws to get proper attachment
        new_raws = [it.get("raw","") for it in new_items]
        reclassified = _classify_tokens_sequential(
            new_raws, f2f_set, command_set=cmd_set, commands=commands)
        groups = []
        current_group = []
        for it in reclassified:
            if it["type"] == "code" and it["status"] in ("exact","line_instance"):
                if current_group:
                    groups.append(command_glue.join(current_group))
                current_group = [it["raw"]]
            elif it["type"] == "command" and it.get("status") == "valid":
                # attach to current group if exists, else start new
                if current_group:
                    current_group.append(it["raw"])
                else:
                    # orphan command without code — keep as separate
                    if current_group:
                        groups.append(command_glue.join(current_group))
                    groups.append(it["raw"])
                    current_group = []
            else:
                # unknown/orphan remains — keep as separate group
                if current_group:
                    groups.append(command_glue.join(current_group))
                    current_group=[]
                groups.append(it["raw"])
        if current_group:
            groups.append(command_glue.join(current_group))

        corrected_code = multicode_sep.join(groups)
        # Preserve free_desc after slash if present (free text not codes)
        free_desc = parsed.get("free_desc","")
        has_slash = parsed.get("has_slash", False)
        if has_slash and free_desc:
            corrected = f"{corrected_code}{description_sep}{free_desc}" if corrected_code else free_desc
        else:
            corrected = corrected_code

        if not corrected or corrected.strip() == raw_desc.strip():
            return None
        # Validate corrected has no UnknownCode
        check = parse_desc_field(corrected, f2f_set, fieldbook_path=fieldbook_path, command_set=cmd_set, rules=rules, commands=commands)
        if any("UnknownCode" in f for f in check.get("flags", [])):
            # Still has unknown — try more aggressive: if corrected still has unknown, return None
            return None
        return corrected
    except Exception:
        return None


def _autocorrect_desc_leave_number(raw_desc: str, f2f_set=None, fieldbook_path=None, command_set=None, rules=None, commands=None) -> str:
    """Move recognized codes into the code section while preserving numeric note values.

    Supports measurements written before or after a detected code, with the final separator
    and spacing determined by the active Field Book and Settings preferences.
    """
    import re
    multicode_sep, description_sep = get_separator_texts(fieldbook_path, commands)
    multicode_sep = multicode_sep or " "
    description_sep = description_sep or " "
    separator_tokens = get_separator_tokens(fieldbook_path, commands)
    description_token = separator_tokens.get("description", "")
    from .parse import parse_desc_field, _strip_leading_digits, _strip_trailing_digits

    if raw_desc is None:
        return ""
    raw = str(raw_desc).strip()
    if not raw:
        return ""

    parsed = parse_desc_field(raw, f2f_set or set(), fieldbook_path=fieldbook_path, command_set=command_set, rules=rules, commands=commands)
    code_part = parsed.get("code_part", "").strip()
    free_desc = parsed.get("free_desc", "").strip()
    misplaced_sets = parsed.get("misplaced_sets", []) or []
    has_description_separator = bool(parsed.get("has_description_separator", parsed.get("has_slash", False)))

    if not misplaced_sets:
        for f in parsed.get("flags", []):
            if f.startswith("MisplacedAfterSeparator:"):
                ms = f.split(":", 1)[1].strip()
                if ms:
                    misplaced_sets.append(ms)

    if not misplaced_sets and has_description_separator:
        tokens = free_desc.split()
        for tok in tokens:
            clean_tok = tok.strip("\"'")
            num_lead, base_lead = _strip_leading_digits(clean_tok)
            base_trail, num_trail = _strip_trailing_digits(clean_tok)
            if num_lead and base_lead and f2f_set and base_lead.casefold() in f2f_set:
                misplaced_sets.append(tok)
            elif num_trail and base_trail and f2f_set and base_trail.casefold() in f2f_set:
                misplaced_sets.append(tok)
            elif f2f_set and clean_tok.casefold() in f2f_set:
                misplaced_sets.append(tok)

    if not misplaced_sets and not has_description_separator:
        # Check patterns like: 30"rcp, 30 rcp, 30rcp, rcp30, rcp 30", rcp 30
        # 1. Leading number followed by code (e.g. 30"rcp, 30 rcp, 30rcp)
        m_lead = re.match(r'^(\d+["\']?)\s*([a-zA-Z_]\w*)(.*)$', raw)
        if m_lead and f2f_set and m_lead.group(2).casefold() in f2f_set:
            num = m_lead.group(1).strip()
            cd = m_lead.group(2).strip()
            rem = m_lead.group(3).strip()
            desc_val = f"{num} {rem}".strip() if rem else num
            return f"{cd}{description_sep}{desc_val}"

        # 2. Code followed by trailing number (e.g. rcp 30", rcp 30, rcp30)
        m_trail = re.match(r'^([a-zA-Z_]\w*)\s*(\d+["\']?)(.*)$', raw)
        if m_trail and f2f_set and m_trail.group(1).casefold() in f2f_set:
            cd = m_trail.group(1).strip()
            num = m_trail.group(2).strip()
            rem = m_trail.group(3).strip()
            desc_val = f"{num} {rem}".strip() if rem else num
            return f"{cd}{description_sep}{desc_val}"

        clean_raw = raw.strip("\"'")
        num_lead, base_lead = _strip_leading_digits(clean_raw)
        base_trail, num_trail = _strip_trailing_digits(clean_raw)
        if num_lead and base_lead and f2f_set and base_lead.casefold() in f2f_set:
            return f"{base_lead}{description_sep}{num_lead}"
        elif num_trail and base_trail and f2f_set and base_trail.casefold() in f2f_set:
            return f"{base_trail}{description_sep}{num_trail}"
        return raw

    moved_codes = []
    remaining = free_desc

    for ms in misplaced_sets:
        toks = ms.split()
        for t in toks:
            clean_t = t.strip("\"'")
            num_lead, base_lead = _strip_leading_digits(clean_t)
            base_trail, num_trail = _strip_trailing_digits(clean_t)

            b2, n2 = _strip_trailing_digits(base_lead) if base_lead else ("", "")
            if num_lead and base_lead and f2f_set and (base_lead.casefold() in f2f_set or (b2 and b2.casefold() in f2f_set)):
                moved_codes.append(base_lead)
                m_unit = re.match(r'^(\d+["\']?)(.*)$', t)
                num_to_keep = m_unit.group(1) if m_unit else num_lead
                pat = r'\b' + re.escape(t) + r'\b'
                remaining = re.sub(pat, num_to_keep, remaining, count=1, flags=re.I)
            elif num_trail and base_trail and f2f_set and base_trail.casefold() in f2f_set:
                moved_codes.append(base_trail)
                m_unit = re.search(r'(\d+["\']?)$', t)
                num_to_keep = m_unit.group(1) if m_unit else num_trail
                pat = r'\b' + re.escape(t) + r'\b'
                remaining = re.sub(pat, num_to_keep, remaining, count=1, flags=re.I)
            elif f2f_set and clean_t.casefold() in f2f_set:
                moved_codes.append(clean_t)
                pat = r'\b' + re.escape(t) + r'\b'
                remaining = re.sub(pat, '', remaining, count=1, flags=re.I)
            else:
                moved_codes.append(t)
                pat = r'\b' + re.escape(t) + r'\b'
                remaining = re.sub(pat, '', remaining, count=1, flags=re.I)

    remaining = re.sub(r'\s{2,}', ' ', remaining).strip()
    remaining = _trim_separator_edges(remaining, separator_tokens.values())

    code_part_has_valid = False
    if code_part and f2f_set:
        for c_tok in code_part.split():
            b, _ = _strip_trailing_digits(c_tok)
            if c_tok.casefold() in f2f_set or b.casefold() in f2f_set:
                code_part_has_valid = True
                break

    if code_part and not code_part_has_valid:
        combined_desc = f"{remaining} {code_part}".strip() if remaining else code_part
        corrected_code = multicode_sep.join(moved_codes)
        return f"{corrected_code}{description_sep}{combined_desc}" if combined_desc else corrected_code

    if code_part:
        corrected_code = code_part + multicode_sep + multicode_sep.join(moved_codes)
    else:
        corrected_code = multicode_sep.join(moved_codes)

    if remaining:
        return f"{corrected_code}{description_sep}{remaining}"
    return corrected_code

def _get_autofix_for_desc(raw_desc: str, flags: str, flag_detail: str, f2f_set: set, fieldbook_path=None) -> str | None:
    """Helper to compute Auto Fix for a description (same logic as CleanDescriptionDialog).
    Returns best_guess_desc or best_guess if available, else None. Used for batch Clean Auto (replaces Clean Selected)."""
    if not raw_desc or not f2f_set:
        return None
    # Try autocorrect first
    try:
        auto = _autocorrect_desc(raw_desc, f2f_set, fieldbook_path=fieldbook_path)
        if auto and auto.strip() != raw_desc.strip():
            from .parse import parse_desc_field as _pdf
            from .config import get_command_set
            cs = get_command_set(fieldbook_path) if fieldbook_path else None
            chk = _pdf(auto, f2f_set, fieldbook_path=fieldbook_path, command_set=cs)
            if not any("UnknownCode" in f for f in chk.get("flags", [])):
                return auto
    except Exception:
        pass
    # LineOrderError — order the active semantic meanings using the Field Book token map.
    if flags and "LineOrderError" in flags:
        try:
            from ..core.point_linework_coder import fix_line_command_order
            commands = get_command_map(fieldbook_path)
            corrected = fix_line_command_order(raw_desc, commands)
            if corrected and corrected != raw_desc:
                return corrected
        except Exception:
            pass
    # UnknownCode token guess
    m = re.search(r'UnknownCode\s*:\s*([A-Za-z0-9]+)', (flags or "") + " " + (flag_detail or ""), re.I)
    if m and f2f_set:
        bad = m.group(1)
        g = _best_guess_for_token(bad, f2f_set, fieldbook_path=fieldbook_path)
        if g:
            # Return full desc with token replaced, not just token, to match dialog behavior
            try:
                new_desc = re.sub(re.escape(bad), g, raw_desc, count=1, flags=re.I)
                if new_desc.strip() != raw_desc.strip():
                    return new_desc
                return g
            except Exception:
                return g
    if flags and "UnknownCode" in flags:
        toks = re.findall(r'[A-Za-z0-9]+', raw_desc)
        if toks:
            g = _best_guess_for_token(toks[0], f2f_set, fieldbook_path=fieldbook_path)
            if g:
                try:
                    new_desc = re.sub(re.escape(toks[0]), g, raw_desc, count=1, flags=re.I)
                    if new_desc.strip() != raw_desc.strip():
                        return new_desc
                except Exception:
                    pass
                return g
    # For MisplacedAfterSeparator / SeparatorSpacingError etc., try autocorrect again via _autocorrect_desc already did
    # If still none, return None
    return None


class BatchCleanDialog(QDialog):
    """Clean Auto — REPLACES Clean Selected: shows only rows that have autofix guesses, with checkboxes.
    Must not pull any that already have corrections (Correction col non-empty or Ignore/Removed).
    Select All / Clear All support."""
    def __init__(self, candidates: list, parent=None):
        # candidates: list of dicts {oid, pt_num, raw_desc, flags, flag_detail, auto_fix}
        super().__init__(parent)
        self.setWindowTitle(f"Clean Auto — Auto Fix ({len(candidates)} Candidates)")
        self.resize(900, 500)
        self.candidates = candidates
        self.result_selected = []  # list of oids that were checked on Accept
        outer = QVBoxLayout(self)
        info = QLabel(f"Clean Auto — Showing {len(candidates)} flagged rows that have Auto Fix guesses and no existing Correction/Ignore. Check to include in batch. Only checked will be fixed. (Replaces Clean Selected)")
        info.setWordWrap(True)
        info.setStyleSheet("color: #333; font-size: 11px;")
        outer.addWidget(info)
        # Select All / Clear All
        sel_row = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        self.clear_all_btn = QPushButton("Clear All")
        self.clear_all_btn.clicked.connect(self._clear_all)
        sel_row.addWidget(self.select_all_btn)
        sel_row.addWidget(self.clear_all_btn)
        sel_row.addStretch()
        # Count label
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #555; font-size: 11px;")
        sel_row.addWidget(self.count_label)
        outer.addLayout(sel_row)
        # Table
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        from PySide6.QtCore import Qt
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Include", "OID", "PtNum", "Raw Desc", "Auto Fix", "Flags"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed | QTableWidget.EditTrigger.SelectedClicked)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(True)
        outer.addWidget(self.table)
        # Populate
        self.table.setSortingEnabled(False)
        for cand in candidates:
            r = self.table.rowCount()
            self.table.insertRow(r)
            # Include checkbox
            chk_item = QTableWidgetItem()
            chk_item.setFlags(chk_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk_item.setCheckState(Qt.CheckState.Checked)
            chk_item.setText("")
            self.table.setItem(r, 0, chk_item)
            self.table.setItem(r, 1, QTableWidgetItem(str(cand.get("oid",""))))
            self.table.setItem(r, 2, QTableWidgetItem(str(cand.get("pt_num",""))))
            self.table.setItem(r, 3, QTableWidgetItem(str(cand.get("raw_desc",""))))
            af = cand.get("auto_fix","")
            af_item = QTableWidgetItem(str(af))
            af_item.setToolTip(str(af) + " — editable (double-click)")
            af_item.setBackground(__import__('PySide6.QtGui', fromlist=['QBrush','QColor']).QBrush(__import__('PySide6.QtGui', fromlist=['QColor']).QColor("#E8F5E9")))
            # Auto Fix (now col 4) editable
            af_item.setFlags(af_item.flags() | __import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 4, af_item)
            flags_item = QTableWidgetItem(str(cand.get("flags","")))
            flags_item.setToolTip(str(cand.get("flags","")))
            flags_item.setFlags(flags_item.flags() & ~__import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 5, flags_item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)
        self._update_count()
        # Connect checkbox change to count update
        self.table.itemChanged.connect(lambda it: self._update_count() if it.column()==0 else None)
        # Bottom buttons
        bot = QHBoxLayout()
        bot.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.apply_btn = QPushButton(f"Apply Auto Fix to Selected")
        self.apply_btn.setStyleSheet("background-color: #E8F5E9; font-weight: bold;")
        self.apply_btn.clicked.connect(self._do_apply)
        bot.addWidget(self.cancel_btn)
        bot.addWidget(self.apply_btn)
        outer.addLayout(bot)
        self._update_count()

    def _select_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it:
                it.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_count()

    def _clear_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it:
                it.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_count()

    def _update_count(self):
        try:
            cnt = sum(1 for r in range(self.table.rowCount()) if self.table.item(r,0) and self.table.item(r,0).checkState()==Qt.CheckState.Checked)
            total = self.table.rowCount()
            self.count_label.setText(f"{cnt} of {total} selected")
            if hasattr(self, 'apply_btn'):
                self.apply_btn.setText(f"Apply Auto Fix to {cnt} Selected" if cnt else "Apply Auto Fix to Selected")
                self.apply_btn.setEnabled(cnt>0)
        except Exception:
            pass

    def _do_apply(self):
        sel = []
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and chk.checkState()==Qt.CheckState.Checked:
                oid = self.table.item(r, 1).text().strip() if self.table.item(r,1) else ""
                # Auto Fix now at col 4 (Flags moved to 5/end)
                af = self.table.item(r, 4).text().strip() if self.table.item(r,4) else ""
                if oid and af:
                    sel.append((oid, af))
        if not sel:
            QMessageBox.warning(self, "Clean Auto", "No rows selected — check at least one Include box or click Select All.")
            return
        self.result_selected = sel
        self.accept()


class CleanDescriptionDialog(QDialog):

    def __init__(self, oid: str, pt_num: str, raw_desc: str, flags: str, flag_detail: str,
                 parsed_code: str, free_desc: str, f2f_set: set, parent=None,
                 fieldbook_path: str = None, fieldbook_rows: list = None):
        super().__init__(parent)
        self.setWindowTitle(f"Clean Description — OID {oid}  Pt {pt_num}")
        self.resize(920, 620)
        self.oid = oid
        self.pt_num = pt_num
        self.raw_desc = raw_desc
        self.flags = flags
        self.is_common_conversion_error = "CommonConversionError" in str(flags)
        self.flag_detail = flag_detail
        self.f2f_set = f2f_set
        self.parsed_code = parsed_code
        self.free_desc = free_desc
        self.result_action = None  # "fix" | "key_token" | "skip" | "key_entire" | None
        self.result_new_desc = None
        # Fieldbook lookup data — try parent, explicit rows/path, then f2f_set fallback
        self._fb_path = fieldbook_path
        if not self._fb_path and parent is not None and hasattr(parent, 'fieldbook_path'):
            self._fb_path = getattr(parent, 'fieldbook_path', None)
        self._fb_rows = fieldbook_rows  # list of 6-col rows if supplied
        if self._fb_rows is None and self._fb_path:
            try:
                from .io_carlson import read_fwb_file
                from pathlib import Path
                h, r = read_fwb_file(Path(self._fb_path))
                if r:
                    self._fb_rows = r
            except Exception:
                self._fb_rows = None
        # Fallback: parent's fieldbook_table in-memory
        if self._fb_rows is None and parent is not None and hasattr(parent, 'fieldbook_table') and parent.fieldbook_table is not None:
            try:
                if parent.fieldbook_table.rowCount() > 0:
                    self._fb_rows = []
                    for rr in range(parent.fieldbook_table.rowCount()):
                        code = parent.fieldbook_table.item(rr, 0).text() if parent.fieldbook_table.item(rr, 0) else ""
                        desc = parent.fieldbook_table.item(rr, 1).text() if parent.fieldbook_table.item(rr, 1) else ""
                        sym = parent.fieldbook_table.item(rr, 2).text() if parent.fieldbook_table.item(rr, 2) else ""
                        layer = parent.fieldbook_table.item(rr, 3).text() if parent.fieldbook_table.item(rr, 3) else ""
                        et = parent.fieldbook_table.item(rr, 4).text() if parent.fieldbook_table.item(rr, 4) else ""
                        cat = parent.fieldbook_table.item(rr, 5).text() if parent.fieldbook_table.item(rr, 5) else ""
                        self._fb_rows.append([code, desc, sym, layer, et, cat])
            except Exception:
                pass
        # Final fallback: synthesize from f2f_set
        if not self._fb_rows and f2f_set:
            self._fb_rows = [[c, "", "", "", "", ""] for c in sorted(f2f_set)]

        # Splitter: left = clean controls, right = fieldbook lookup
        from PySide6.QtWidgets import QSplitter, QTableWidget, QTableWidgetItem, QHeaderView, QComboBox
        from PySide6.QtCore import Qt
        outer = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter)

        # ----- LEFT -----
        left_w = QWidget()
        lay = QVBoxLayout(left_w)
        lay.setContentsMargins(0,0,0,0)
        lay.setSpacing(6)

        # Info
        info = QGroupBox("Flagged Description")
        form = QFormLayout(info)
        form.setVerticalSpacing(4)
        form.addRow("OID:", QLabel(oid))
        form.addRow("PtNum:", QLabel(pt_num))
        raw_lbl = QLineEdit(raw_desc); raw_lbl.setReadOnly(True)
        form.addRow("Raw Desc:", raw_lbl)
        parsed_lbl = QLineEdit(parsed_code or "(none)"); parsed_lbl.setReadOnly(True)
        form.addRow("Parsed Code:", parsed_lbl)
        free_lbl = QLineEdit(free_desc or ""); free_lbl.setReadOnly(True)
        form.addRow("Free Desc (/ after):", free_lbl)
        form.addRow("Flags:", QLabel(flags))
        det = QTextEdit(flag_detail or ""); det.setReadOnly(True); det.setMaximumHeight(60)
        form.addRow("Detail:", det)
        lay.addWidget(info)

        # Best guess preview — try autocorrect first (pull codes, include ' - '), then fallback to single-token guess
        self.best_guess = None
        self.best_guess_desc = None  # full corrected desc for autocorrect/line-order
        # Autocorrect: try to pull codes out of description with ' - ' handling (uses fieldbook rules/commands)
        try:
            fb_path_for_auto = getattr(self, '_fb_path', None) or fieldbook_path
            auto = _autocorrect_desc(raw_desc, f2f_set, fieldbook_path=fb_path_for_auto)
            if auto and auto.strip() != raw_desc.strip():
                # Validate auto still has no UnknownCode
                from .parse import parse_desc_field as _pdf
                from .config import get_command_set
                cs = get_command_set(fb_path_for_auto) if fb_path_for_auto else None
                chk = _pdf(auto, f2f_set, fieldbook_path=fb_path_for_auto, command_set=cs)
                if not any("UnknownCode" in f for f in chk.get("flags", [])):
                    self.best_guess_desc = auto
                    self.best_guess = auto  # for Fix button — will apply full desc
        except Exception:
            pass
        # For a line-order issue, use semantic meanings from the active Field Book map.
        if not self.best_guess and "LineOrderError" in flags:
            try:
                from ..core.point_linework_coder import fix_line_command_order
                from .config import get_command_map as _get_commands

                fb_path_lo = getattr(self, "_fb_path", None) or fieldbook_path
                commands_lo = _get_commands(fb_path_lo)
                corrected_lo = fix_line_command_order(raw_desc, commands_lo)
                if corrected_lo and corrected_lo != raw_desc:
                    self.best_guess_desc = corrected_lo
                    self.best_guess = corrected_lo
            except Exception:
                pass
        # Extract first UnknownCode token if present (fallback single-token guess)
        if not self.best_guess:
            m = re.search(r'UnknownCode\s*:\s*([A-Za-z0-9]+)', flags + " " + flag_detail, re.I)
            if m and f2f_set:
                bad = m.group(1)
                self.best_guess = _best_guess_for_token(bad, f2f_set, fieldbook_path=getattr(self, 'fieldbook_path', None))
        if not self.best_guess and flags and "UnknownCode" in flags:
            toks = re.findall(r'[A-Za-z0-9]+', raw_desc)
            if toks:
                self.best_guess = _best_guess_for_token(toks[0], f2f_set, fieldbook_path=getattr(self, 'fieldbook_path', None))

        # Key-In area
        key_group = QGroupBox("Key-In")
        k_lay = QVBoxLayout(key_group)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Fix Token:"))
        self.token_edit = QLineEdit()
        self.token_edit.setPlaceholderText("e.g., NG or EC1 — validates against F2F")
        row1.addWidget(self.token_edit)
        k_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Entire Code:"))
        self.entire_edit = QLineEdit()
        self.entire_edit.setPlaceholderText("Full description using Field Book codes and active command tokens")
        self.entire_edit.setText(raw_desc)
        row2.addWidget(self.entire_edit)
        k_lay.addLayout(row2)
        lay.addWidget(key_group)

        # Correction Information — ONLY Existing Correction + Auto Fix (always shown, N/A if none)
        self.corr_info_box = QGroupBox("Correction Information")
        corr_form = QFormLayout(self.corr_info_box)
        corr_form.setVerticalSpacing(2)
        # Existing correction/ignore from parent table if any — combined
        existing_corr = ""
        existing_ignore = ""
        try:
            if parent and hasattr(parent, 'desc_parse_table') and parent.desc_parse_table is not None:
                for rr in range(parent.desc_parse_table.rowCount()):
                    oid_it = parent.desc_parse_table.item(rr, 0)
                    if oid_it and oid_it.text().strip() == str(oid):
                        c_it = parent.desc_parse_table.item(rr, 10)
                        i_it = parent.desc_parse_table.item(rr, 11)
                        if c_it:
                            existing_corr = c_it.text().strip()
                        if i_it:
                            existing_ignore = i_it.text().strip()
                        break
        except Exception:
            pass
        # Combine: prefer correction, else ignore/removed, else (none)
        if existing_corr:
            existing_combined = existing_corr
            existing_style = "color: #2E7D32; font-weight: bold;"
            existing_tip = "Existing Correction — will be overwritten by new Fix/Key-In"
        elif existing_ignore:
            existing_combined = existing_ignore
            if existing_ignore == "Removed":
                existing_style = "color: #C62828; font-weight: bold;"
            elif existing_ignore == "Ignored":
                existing_style = "color: #EF6C00; font-weight: bold;"
            else:
                existing_style = "color: #C62828;"
            existing_tip = "Existing Ignore/Removed — counts as handled"
        else:
            existing_combined = "(none)"
            existing_style = "color: #888;"
            existing_tip = "No existing correction for this point"
        self.existing_corr_label = QLabel(existing_combined)
        self.existing_corr_label.setStyleSheet(existing_style)
        self.existing_corr_label.setToolTip(existing_tip)
        corr_form.addRow("Existing Correction:", self.existing_corr_label)
        # Keep reference for backwards compat (tests may check ignore label)
        self.existing_ignore_label = self.existing_corr_label
        # Auto Fix — always shown, editable QLineEdit (user can modify before submit), N/A if none
        auto_fix_raw = self.best_guess_desc if self.best_guess_desc else self.best_guess
        if auto_fix_raw:
            auto_fix_text = auto_fix_raw.upper()
            auto_fix_style = "color: #2E7D32; font-weight: bold; font-family: monospace;"
            auto_fix_tip = "Auto Fix — best guess from fieldbook (editable — modify before 3. Auto Fix)"
            auto_fix_enabled = True
        else:
            auto_fix_text = ""
            auto_fix_style = "color: #888; font-style: italic;"
            auto_fix_tip = "No Auto Fix found — Fix button disabled (edit to provide fix)"
            auto_fix_enabled = False
        self.auto_fix_edit = QLineEdit()
        self.auto_fix_edit.setText(auto_fix_text)
        if not auto_fix_raw:
            self.auto_fix_edit.setPlaceholderText(
                "Enter the complete verified description" if self.is_common_conversion_error else "N/A")
        self.auto_fix_edit.setStyleSheet(auto_fix_style)
        self.auto_fix_edit.setToolTip(auto_fix_tip)
        self.auto_fix_edit.setReadOnly(False)
        self.auto_fix_edit.setEnabled(True)
        # Keep QLabel alias for backwards compat (tests may check label)
        self.auto_fix_label = self.auto_fix_edit
        corr_form.addRow(
            "Corrected Description:" if self.is_common_conversion_error else "Auto Fix:",
            self.auto_fix_edit)
        # Keep dummy labels for compat / no longer shown in this box
        self.proposed_label = QLabel("")
        self.proposed_label.setVisible(False)
        self.point_change_label = QLabel("")
        self.point_change_label.setVisible(False)
        lay.addWidget(self.corr_info_box)
        # (Live preview removed — Auto Fix is static; only Existing + Auto Fix shown per request)

        # Buttons — 1,2,3 on same row per request (iterate)
        btn_row = QHBoxLayout()
        self.key_token_btn = QPushButton("&1. Key-In Fix Token")
        self.key_token_btn.setToolTip("Enter replacement token, validates against F2F (Alt+1)")
        self.key_token_btn.setShortcut("Alt+1")
        self.key_token_btn.clicked.connect(self._do_key_token)

        self.key_entire_btn = QPushButton("&2. Key-In Entire Code")
        self.key_entire_btn.setToolTip("Full rewrite, validates (Alt+2)")
        self.key_entire_btn.setShortcut("Alt+2")
        self.key_entire_btn.clicked.connect(self._do_key_entire)

        # Button 3 — Auto Fix (editable — user can modify Auto Fix field before submit)
        auto_fix_for_btn = self.best_guess_desc if self.best_guess_desc else self.best_guess
        if auto_fix_for_btn:
            fix_enabled = True
            fix_tip = "Apply Auto Fix (editable — modify Auto Fix field before clicking)"
            fix_style = "background-color: #E8F5E9;"
        else:
            fix_enabled = False
            fix_tip = "No Auto Fix available — edit Auto Fix field to enable"
            fix_style = "background-color: #EEEEEE; color: #888;"
        self.fix_btn = QPushButton(
            "&3. Correct" if self.is_common_conversion_error else "&3. Auto Fix")
        if self.is_common_conversion_error:
            fix_tip = "Apply a complete, verified description before considering Ignore"
        self.fix_btn.setToolTip(fix_tip + " (Alt+3)")
        self.fix_btn.setShortcut("Alt+3")
        self.fix_btn.clicked.connect(self._do_fix)
        self.fix_btn.setEnabled(fix_enabled)
        self.fix_btn.setStyleSheet(fix_style)
        # Enable Fix button when user edits Auto Fix field to non-empty valid content
        try:
            self.auto_fix_edit.textChanged.connect(lambda txt: self.fix_btn.setEnabled(bool(txt.strip()) and txt.strip().upper() != "N/A"))
        except Exception:
            pass

        btn_row.addWidget(self.key_token_btn)
        btn_row.addWidget(self.key_entire_btn)
        btn_row.addWidget(self.fix_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        # Disable single-token fix for non-token errors or multiple issues (per requirement)
        try:
            flags_str = (flags or "") + " " + (flag_detail or "")
            flags_only = flags or ""
            # Count distinct flag types in Flags field (not detail) to avoid double-counting UnknownCode in both
            flag_types = []
            for ft in ["CommonConversionError", "UnknownCode", "OrphanCommand", "MisplacedAfterSeparator", "LineOrderError", "SeparatorSpacingError", "EmptyDescription"]:
                if ft in flags_only:
                    flag_types.append(ft)
            # Per request: UnknownCode and OrphanCommand alone (or together) should NOT count as "multiple" that disables Key-In
            only_unknown_orphan = set(flag_types).issubset({"UnknownCode", "OrphanCommand"}) and len(flag_types) >= 1
            has_multiple = (len(flag_types) > 1 or flags_only.count(";") >= 1 or flags_only.count(",") >= 1) and not only_unknown_orphan
            # Non-token errors are anything except UnknownCode and OrphanCommand (per request: these should NOT gray out Key-In Fix Token)
            is_non_token = any(ft in flags_only for ft in ["CommonConversionError", "LineOrderError", "MisplacedAfterSeparator", "SeparatorSpacingError", "EmptyDescription"])
            if is_non_token or has_multiple:
                self.key_token_btn.setEnabled(False)
                self.key_token_btn.setToolTip("Single-token fix disabled — multiple issues or non-token error (use Auto Fix or Entire Code)")
                self.key_token_btn.setStyleSheet("background-color: #EEEEEE; color: #888;")
            # Also check if the parsed code has multiple UnknownCodes (count in Flags only)
            try:
                unknown_count = flags_only.count("UnknownCode")
                if unknown_count > 1:
                    self.key_token_btn.setEnabled(False)
                    self.key_token_btn.setToolTip("Multiple UnknownCodes — use Key-In Entire Code or Auto Fix")
            except Exception:
                pass
        except Exception:
            pass

        # Bottom nav row — Back (for misclick), Remove, Ignore, Cancel — Previous/Next removed per request
        bottom_row = QHBoxLayout()
        self.back_btn = QPushButton("◀ Back")
        self.back_btn.setToolTip("Back to previous description (for misclick) (Alt+B)")
        self.back_btn.setShortcut("Alt+B")
        self.back_btn.clicked.connect(self._do_back)
        self.back_btn.setEnabled(False)
        # Keep prev_btn as alias for backwards compat (handlers in ui_main check for back_btn first)
        self.prev_btn = self.back_btn
        # Keep skip_btn as alias but hidden (no longer shown) for compat if old handlers call it
        self.skip_btn = QPushButton("&Next ▶")
        self.skip_btn.setVisible(False)

        self.remove_btn = QPushButton("&Remove Point")
        self.remove_btn.setToolTip("Delete this point from consolidated file (before Skip/Ignore) — removes OID from working file (Alt+R)")
        self.remove_btn.setShortcut("Alt+R")
        self.remove_btn.setStyleSheet("background-color: #FFEBEE; color: #B71C1C; font-weight: bold;")
        self.remove_btn.clicked.connect(self._do_remove)

        self.ignore_btn = QPushButton("&Ignore")  # was 5. Ignore Error
        self.ignore_btn.setToolTip("Mark as Ignored at end, counts as handled for renumber lock (Alt+I)")
        self.ignore_btn.setShortcut("Alt+I")
        self.ignore_btn.clicked.connect(self._do_ignore)

        bottom_row.addWidget(self.back_btn)
        bottom_row.addWidget(self.remove_btn)
        bottom_row.addWidget(self.ignore_btn)
        bottom_row.addStretch()
        cancel = QPushButton("&Cancel")
        cancel.setToolTip("Close dialog (X) — stops sequential clean (Alt+C / Esc)")
        cancel.setShortcut("Alt+C")
        cancel.clicked.connect(self.reject)
        bottom_row.addWidget(cancel)
        lay.addLayout(bottom_row)
        lay.addStretch()
        splitter.addWidget(left_w)

        # ----- RIGHT — Fieldbook Lookup (inside popup, usable while cleaning) -----
        right_w = QWidget()
        r_lay = QVBoxLayout(right_w)
        r_lay.setContentsMargins(8, 0, 0, 0)
        r_lay.setSpacing(4)
        rt = QLabel("Fieldbook Lookup")
        rt.setStyleSheet("font-weight: bold; font-size: 12px;")
        r_lay.addWidget(rt)
        hint = QLabel("Search Code or Description — *contains* wildcard. Filter by Category.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 10px;")
        r_lay.addWidget(hint)
        # Search
        search_row = QHBoxLayout()
        self.fb_search = QLineEdit()
        self.fb_search.setPlaceholderText("Search — type to filter (*search* in Code or Description)")
        self.fb_search.setClearButtonEnabled(True)
        self.fb_search.textChanged.connect(self._fb_apply_filter)
        self.fb_search.setToolTip("Contains search: Code or Description contains your text (case-insensitive, *implicit*)")
        search_row.addWidget(self.fb_search)
        clr = QPushButton("×")
        clr.setFixedWidth(24)
        clr.setToolTip("Clear search")
        clr.clicked.connect(lambda: self.fb_search.clear())
        search_row.addWidget(clr)
        r_lay.addLayout(search_row)
        # Category
        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("Category:"))
        self.fb_category = QComboBox()
        self.fb_category.addItem("All Categories")
        cats = sorted({r[5].strip() for r in (self._fb_rows or []) if len(r) > 5 and r[5].strip()})
        for c in cats:
            self.fb_category.addItem(c)
        self.fb_category.currentTextChanged.connect(self._fb_apply_filter)
        cat_row.addWidget(self.fb_category)
        cat_row.addStretch()
        r_lay.addLayout(cat_row)
        # Table
        from .utils_sort import NaturalSortItem
        self.fb_table = QTableWidget()
        self.fb_table.setColumnCount(3)
        self.fb_table.setHorizontalHeaderLabels(["Code", "Description", "Category"])
        self.fb_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.fb_table.setSortingEnabled(True)
        self.fb_table.setAlternatingRowColors(True)
        self.fb_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.fb_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.fb_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.fb_table.setToolTip("Double-click a row to put Code into Fix Token; use buttons to apply")
        self.fb_table.cellDoubleClicked.connect(self._fb_on_double_click)
        r_lay.addWidget(self.fb_table)
        # Count
        self.fb_count = QLabel("")
        self.fb_count.setStyleSheet("color: #666; font-size: 10px;")
        r_lay.addWidget(self.fb_count)
        # Use buttons
        use_row = QHBoxLayout()
        self.fb_use_token_btn = QPushButton("→ Use for Token")
        self.fb_use_token_btn.setToolTip("Put selected Code into Fix Token field")
        self.fb_use_token_btn.clicked.connect(self._fb_use_token)
        # Per request: when Key-In Fix Token has text, gray out Use for Token (to avoid confusion)
        try:
            self.token_edit.textChanged.connect(lambda txt: self.fb_use_token_btn.setEnabled(not bool(txt.strip())))
        except Exception:
            pass
        self.fb_use_entire_btn = QPushButton("→ Use for Entire")
        self.fb_use_entire_btn.setToolTip("Put selected Code into Entire Code field (replaces first token)")
        self.fb_use_entire_btn.clicked.connect(self._fb_use_entire)
        use_row.addWidget(self.fb_use_token_btn)
        use_row.addWidget(self.fb_use_entire_btn)
        r_lay.addLayout(use_row)
        # Hint for use
        use_hint = QLabel("Tip: double-click fills Token; or select + → Use")
        use_hint.setStyleSheet("color: #888; font-size: 10px;")
        use_hint.setWordWrap(True)
        r_lay.addWidget(use_hint)
        splitter.addWidget(right_w)
        splitter.setSizes([520, 380])
        # Populate table
        self._fb_populate()

        # Also auto-prefill search with bad token if UnknownCode
        try:
            m2 = re.search(r'UnknownCode\s*:\s*([A-Za-z0-9]+)', flags + " " + flag_detail, re.I)
            if m2:
                self.fb_search.setText(m2.group(1)[:3])
        except Exception:
            pass


    # ----- Fieldbook lookup helpers (inside popup) -----
    def _fb_populate(self):
        if not hasattr(self, 'fb_table') or self.fb_table is None:
            return
        rows = self._fb_rows or []
        self.fb_table.setSortingEnabled(False)
        self.fb_table.blockSignals(True)
        self.fb_table.setRowCount(0)
        from .utils_sort import NaturalSortItem
        from PySide6.QtWidgets import QTableWidgetItem
        for r in rows:
            code = r[0] if len(r) > 0 else ""
            desc = r[1] if len(r) > 1 else ""
            cat = r[5] if len(r) > 5 else ""
            idx = self.fb_table.rowCount()
            self.fb_table.insertRow(idx)
            self.fb_table.setItem(idx, 0, NaturalSortItem(code, letters_first=True))
            self.fb_table.setItem(idx, 1, NaturalSortItem(desc, letters_first=False))
            self.fb_table.setItem(idx, 2, QTableWidgetItem(cat))
        self.fb_table.resizeColumnsToContents()
        self.fb_table.setSortingEnabled(True)
        self.fb_table.blockSignals(False)
        self._fb_apply_filter()

    def _fb_apply_filter(self, _text=None):
        if not hasattr(self, 'fb_table') or self.fb_table is None:
            return
        txt = self.fb_search.text().strip().lower() if hasattr(self, 'fb_search') and self.fb_search else ""
        cat = self.fb_category.currentText() if hasattr(self, 'fb_category') and self.fb_category else "All Categories"
        visible = 0
        for r in range(self.fb_table.rowCount()):
            code = self.fb_table.item(r, 0).text().lower() if self.fb_table.item(r, 0) else ""
            desc = self.fb_table.item(r, 1).text().lower() if self.fb_table.item(r, 1) else ""
            category = self.fb_table.item(r, 2).text() if self.fb_table.item(r, 2) else ""
            match_text = True
            if txt:
                match_text = (txt in code) or (txt in desc)
            match_cat = (cat == "All Categories" or category == cat)
            show = match_text and match_cat
            self.fb_table.setRowHidden(r, not show)
            if show:
                visible += 1
        total = self.fb_table.rowCount()
        if total == 0:
            self.fb_count.setText("No codes in fieldbook — load a .fwb")
        else:
            self.fb_count.setText(f"{visible} of {total} codes shown")

    def _fb_selected_code(self):
        if not hasattr(self, 'fb_table') or self.fb_table.rowCount() == 0:
            return None
        sel = self.fb_table.selectionModel().selectedRows()
        row = None
        if sel:
            row = sel[0].row()
        else:
            for r in range(self.fb_table.rowCount()):
                if not self.fb_table.isRowHidden(r):
                    row = r
                    break
        if row is None:
            return None
        it = self.fb_table.item(row, 0)
        return it.text().strip() if it else None

    def _fb_on_double_click(self, row, col):
        code = self.fb_table.item(row, 0).text().strip() if self.fb_table.item(row, 0) else ""
        if code:
            self.token_edit.setText(code)
            self.token_edit.setFocus()
            self.token_edit.selectAll()

    def _fb_use_token(self):
        code = self._fb_selected_code()
        if code:
            self.token_edit.setText(code)
            self.token_edit.setFocus()
            # Submit change immediately (user requested Use should submit)
            self._do_key_token()

    def _fb_use_entire(self):
        code = self._fb_selected_code()
        if not code:
            return
        # Replace the first feature-code token while preserving configured separators and commands.
        import re as _re
        from ..core.fieldbook_syntax import find_separator, separator_pattern

        cur = self.entire_edit.text().strip()
        command_tokens = get_command_map(self._fb_path)
        line_commands = {command_tokens.get(meaning, "").casefold()
                         for meaning in ("start_line", "start_curve", "end_curve", "end_line", "close")
                         if command_tokens.get(meaning)}
        description_token = command_tokens.get("description", "")
        multicode_token = command_tokens.get("multicode", "")
        description_index = find_separator(cur, description_token)
        code_end = description_index if description_index >= 0 else len(cur)
        code_region = cur[:code_end]
        masked = (_re.sub(separator_pattern(multicode_token),
                           lambda match: " " * len(match.group()), code_region, flags=_re.I)
                  if multicode_token else code_region)
        match_to_replace = None
        replacement = code
        known_codes = {str(item).casefold() for item in self.f2f_set or ()}
        for match in _re.finditer(r"[A-Za-z0-9]+", masked):
            token = cur[match.start():match.end()]
            folded = token.casefold()
            if folded in line_commands:
                continue
            suffix = next((command for command in sorted(line_commands, key=len, reverse=True)
                           if folded.endswith(command) and len(token) > len(command)), "")
            if suffix and folded not in known_codes:
                replacement = f"{code}{token[-len(suffix):]}"
            match_to_replace = match
            break
        if match_to_replace is None:
            self.entire_edit.setText(code)
            self.entire_edit.setFocus()
            # Submit entire change immediately
            self._do_key_entire()
            return
        cur = (cur[:match_to_replace.start()] + replacement + cur[match_to_replace.end():])
        self.entire_edit.setText(cur)
        self.entire_edit.setFocus()
        # Submit change immediately
        self._do_key_entire()

    def _validate_token(self, tok: str) -> bool:
        if not tok or not self.f2f_set:
            return False
        # Direct exact
        if tok in self.f2f_set:
            return True
        # Case-insensitive
        low = {c.lower(): c for c in self.f2f_set}
        if tok.lower() in low:
            return True
        # Strip trailing digits then check (per 12cirf rule)
        stripped = re.sub(r'\d+$', '', tok)
        if stripped.lower() in low:
            return True
        return False

    def _update_corr_preview(self, _text=None):
        """Live correction preview — shows what will change in the point if Fix/Key-In applied."""
        try:
            if not hasattr(self, 'proposed_label') or self.proposed_label is None:
                return
            tok = self.token_edit.text().strip() if hasattr(self, 'token_edit') and self.token_edit else ""
            entire = self.entire_edit.text().strip() if hasattr(self, 'entire_edit') and self.entire_edit else ""
            proposed = None
            if tok and self._validate_token(tok):
                new_desc = self.raw_desc
                m = re.search(r'UnknownCode\s*:\s*([A-Za-z0-9]+)', self.flags + " " + self.flag_detail, re.I)
                if m:
                    bad = m.group(1)
                    new_desc = re.sub(re.escape(bad), tok, new_desc, count=1, flags=re.I)
                else:
                    new_desc = re.sub(r'[A-Za-z0-9]+', tok, new_desc, count=1)
                proposed = new_desc
            elif entire and entire != self.raw_desc:
                proposed = entire
            elif self.best_guess:
                if hasattr(self, 'best_guess_desc') and self.best_guess_desc:
                    proposed = self.best_guess_desc
                else:
                    new_desc = self.raw_desc
                    m = re.search(r'UnknownCode\s*:\s*([A-Za-z0-9]+)', self.flags + " " + self.flag_detail, re.I)
                    if m:
                        bad = m.group(1)
                        new_desc = re.sub(re.escape(bad), self.best_guess, new_desc, count=1, flags=re.I)
                    else:
                        new_desc = re.sub(r'[A-Za-z0-9]+', self.best_guess, new_desc, count=1)
                    proposed = new_desc
            if proposed and proposed != self.raw_desc:
                diff = f"'{self.raw_desc}' → '{proposed}'"
                self.proposed_label.setText(diff)
                self.proposed_label.setStyleSheet("color: #2E7D32; font-weight: bold; font-family: monospace;")
                self.point_change_label.setText(f"Point {self.pt_num} (OID {self.oid}) WILL CHANGE: Description '{self.raw_desc}' → '{proposed}' — Consolidated file & final out-file will use new desc.")
                self.point_change_label.setStyleSheet("color: #2E7D32; font-size: 10px; font-weight: bold;")
            elif proposed and proposed == self.raw_desc:
                self.proposed_label.setText(f"'{proposed}' (no change from current)")
                self.proposed_label.setStyleSheet("color: #888; font-family: monospace;")
                self.point_change_label.setText("No description change — use Remove/Ignore for point-level action.")
                self.point_change_label.setStyleSheet("color: #888; font-size: 10px;")
            else:
                self.proposed_label.setText("(select Fix / Key-In Token / Key-In Entire to preview)")
                self.proposed_label.setStyleSheet("color: #1565C0; font-weight: bold; font-family: monospace;")
                self.point_change_label.setText(f"Will update Consolidated row for Pt {self.pt_num} (OID {self.oid}) — Description column — if Fix/Key-In applied. Remove/Ignore marks point for final out-file.")
                self.point_change_label.setStyleSheet("color: #555; font-size: 10px;")
        except Exception:
            pass

    def _do_fix(self):
        # Prefer edited Auto Fix field if user modified it
        edited = None
        try:
            if hasattr(self, 'auto_fix_edit') and self.auto_fix_edit is not None:
                txt = self.auto_fix_edit.text().strip()
                if txt and txt.upper() != "N/A":
                    edited = txt
        except Exception:
            edited = None
        if edited:
            # Common-conversion findings need a safe full description, not a first-token-only
            # check: the correction rule itself is what may have discarded valid codes.
            try:
                from .parse import parse_desc_field as _pdf_fix
                from .config import get_command_set as _gcs_fix
                fb_path_fix = getattr(self, '_fb_path', None)
                cs_fix = _gcs_fix(fb_path_fix) if fb_path_fix else None
                chk = _pdf_fix(edited, self.f2f_set, fieldbook_path=fb_path_fix,
                               command_set=cs_fix, rules=[] if self.is_common_conversion_error else None)
                flags_fix = chk.get("flags", [])
                if self.is_common_conversion_error:
                    blocked = [flag for flag in flags_fix if any(
                        kind in flag for kind in ("UnknownCode", "OrphanCommand",
                                                  "MisplacedAfterSeparator", "EmptyDescription"))]
                    valid_codes = [item for item in
                                   chk.get("code_classified", []) + chk.get("free_classified", [])
                                   if item.get("type") == "code"
                                   and item.get("status") in ("exact", "line_instance")]
                    if blocked or not valid_codes:
                        reason = ", ".join(blocked) if blocked else "no valid Field Book code"
                        QMessageBox.warning(self, "Fix Error",
                                            f"Enter a complete description with verified Field Book codes ({reason}).")
                        return
                elif any("UnknownCode" in flag for flag in flags_fix):
                    # Legacy edit flow allows a single known first code when other text is being repaired.
                    if not self._validate_token(edited.split()[0]):
                        QMessageBox.warning(self, "Fix Error", f"Edited fix '{edited}' still has UnknownCode — check fieldbook.")
                        return
            except Exception as ex:
                if self.is_common_conversion_error:
                    QMessageBox.warning(self, "Fix Error", f"Could not verify the corrected description: {ex}")
                    return
            self.result_action = "fix"
            self.result_new_desc = edited
            self.accept()
            return
        if not self.best_guess:
            QMessageBox.warning(self, "Fix Error", "No best guess available for this flag.")
            return
        # If we have a full corrected desc (autocorrect or LineOrderError), apply it
        if hasattr(self, 'best_guess_desc') and self.best_guess_desc:
            # For autocorrect or LineOrderError, best_guess_desc is full corrected desc
            self.result_action = "fix"
            self.result_new_desc = self.best_guess_desc
            self.accept()
            return
        # Validate best guess token
        if not self._validate_token(self.best_guess):
            QMessageBox.warning(self, "Fix Error", f"Best guess {self.best_guess} not in F2F — cannot apply.")
            return
        # Replace first UnknownCode token in raw_desc with best guess
        # Simple: replace bad token occurrence
        new_desc = self.raw_desc
        # Find bad token from flags
        m = re.search(r'UnknownCode\s*:\s*([A-Za-z0-9]+)', self.flags + " " + self.flag_detail, re.I)
        if m:
            bad = m.group(1)
            # Replace first case-insensitive occurrence
            new_desc = re.sub(re.escape(bad), self.best_guess, new_desc, count=1, flags=re.I)
        else:
            # Fallback: replace first token
            new_desc = re.sub(r'[A-Za-z0-9]+', self.best_guess, new_desc, count=1)
        self.result_action = "fix"
        self.result_new_desc = new_desc
        self.accept()

    def _do_key_token(self):
        tok = self.token_edit.text().strip()
        if not tok:
            QMessageBox.warning(self, "Key-In Fix Token", "Enter a replacement token.")
            return
        if not self._validate_token(tok):
            QMessageBox.warning(self, "Key-In Fix Token", f"Token '{tok}' not in Field Book (F2F).")
            return
        # Replace bad token with user token
        new_desc = self.raw_desc
        m = re.search(r'UnknownCode\s*:\s*([A-Za-z0-9]+)', self.flags + " " + self.flag_detail, re.I)
        if m:
            bad = m.group(1)
            new_desc = re.sub(re.escape(bad), tok, new_desc, count=1, flags=re.I)
        else:
            # For Orphan/Misplaced, replace first flagged token? For now replace first word
            # If multiple tokens, ask to use Entire Code instead
            new_desc = re.sub(r'[A-Za-z0-9]+', tok, new_desc, count=1)
        self.result_action = "key_token"
        self.result_new_desc = new_desc
        self.accept()

    def _do_remove(self):
        # Remove point — no warning per request (meta flagged, not immediate delete)
        self.result_action = "remove"
        self.result_new_desc = None
        self.accept()

    def _do_back(self):
        self.result_action = "back"  # Back to previous for misclick
        self.result_new_desc = None
        self.accept()

    def _do_previous(self):
        self.result_action = "back"
        self.result_new_desc = None
        self.accept()

    def _do_skip(self):
        self.result_action = "skip"  # leave unresolved, move to next (kept for compat but button hidden)
        self.result_new_desc = None
        self.accept()

    def _do_ignore(self):
        self.result_action = "ignore"  # mark as Ignored at end, counts as handled
        self.result_new_desc = None
        self.accept()

    def _do_key_entire(self):
        entire = self.entire_edit.text().strip()
        if not entire:
            QMessageBox.warning(self, "Key-In Entire Code", "Enter a full description.")
            return
        # Validate entire desc: must parse without UnknownCode? For now check that at least first token is in F2F
        # Use fieldbook-aware parse (rules + custom commands)
        fb_path = getattr(self, '_fb_path', None)
        try:
            from .config import get_command_set
            cs = get_command_set(fb_path) if fb_path else None
        except Exception:
            cs = None
        parsed = parse_desc_field(entire, self.f2f_set, fieldbook_path=fb_path, command_set=cs) if self.f2f_set else {"flags": []}
        # Allow if no UnknownCode flags, or if user explicitly wants to override
        # For this dialog, we allow any, but warn if still flagged
        if any("UnknownCode" in f for f in parsed.get("flags", [])):
            resp = QMessageBox.question(self, "Key-In Entire Code", f"Still flagged: {parsed.get('flags')} — keep anyway? (You can Fix again)", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if resp != QMessageBox.StandardButton.Yes:
                return
        self.result_action = "key_entire"
        self.result_new_desc = entire
        self.accept()


class RenumberSuggestionDialog(QDialog):
    """Single-point renumber suggestions — not automation, just suggestions.

    Shows: current PtNum → type + crew detect, then options:
      Fill holes (conserve) vs At end, each in crew+type / crew-only / type-only,
      with break-out for Control+Crew vs just Control.
    Writes to Corr_* metadata, not directly to PtNum.
    """
    def __init__(self, oid: str, cur_pt: str, crew: int, used_numbers: set[int],
                 external_used: set[int] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Renumber Suggestions — OID {oid}  Pt {cur_pt}")
        self.resize(600, 420)
        self.oid = oid
        self.cur_pt = cur_pt
        self.crew = crew
        self.used = set(used_numbers) if used_numbers else set()
        self.external_used = set(external_used) if external_used else set()
        self.all_used = self.used | self.external_used
        self.result_new_pt = None
        self.result_type = None  # "hole" | "at_end" | "group" | "inject"
        self.result_detail = ""

        from .config import CONTROL_RANGE, BOUNDARY_RANGE, GENERAL_START, crew_blocks, parse_carlson_ranges, find_holes_in_ranges, next_at_end, number_type_label

        lay = QVBoxLayout(self)
        # Current
        cur_box = QGroupBox("Current Point")
        cur_form = QFormLayout(cur_box)
        cur_form.addRow("OID:", QLabel(oid))
        cur_form.addRow("PtNum:", QLabel(cur_pt))
        try:
            n = int(cur_pt)
            typ = number_type_label(cur_pt)
            in_crew = any(s <= n <= e for s,e in crew_blocks(crew))
            cur_form.addRow("Type:", QLabel(f"{typ} ({CONTROL_RANGE}, {BOUNDARY_RANGE}, {GENERAL_START}+)"))
            cur_form.addRow("Crew detect:", QLabel(f"Crew {crew} blocks {crew_blocks(crew)[:2]} — {'IN crew block' if in_crew else 'OUTSIDE crew block (break-out)'}"))
        except:
            cur_form.addRow("Type:", QLabel("Unknown"))
        lay.addWidget(cur_box)

        # Suggestions — compute holes
        all_used = self.all_used
        # Candidate type ranges
        try:
            n = int(cur_pt)
            if CONTROL_RANGE[0] <= n <= CONTROL_RANGE[1]:
                cur_type_range = CONTROL_RANGE; cur_type_name = "Control 1-999"
            elif BOUNDARY_RANGE[0] <= n <= BOUNDARY_RANGE[1]:
                cur_type_range = BOUNDARY_RANGE; cur_type_name = "Boundary 1000-9999"
            else:
                cur_type_range = (GENERAL_START, 9999999); cur_type_name = "General 10000+"
        except:
            cur_type_range = (GENERAL_START, 9999999); cur_type_name = "General"

        holes_crew_type = find_holes_in_ranges(all_used, crew=crew, type_range=cur_type_range)[:5]
        holes_type = find_holes_in_ranges(all_used, crew=None, type_range=cur_type_range)[:5]
        holes_crew = find_holes_in_ranges(all_used, crew=crew, type_range=None)[:5]
        at_end_crew_type = next_at_end(all_used, crew=crew, type_range=cur_type_range)
        at_end_type = next_at_end(all_used, crew=None, type_range=cur_type_range)
        at_end_crew = next_at_end(all_used, crew=crew, type_range=None)

        sug_box = QGroupBox("Suggestions — Pick One (Writes to Corr_PtNum Metadata, Not Final PtNum Yet)")
        sug_form = QFormLayout(sug_box)
        # Fill holes
        hole_opts = []
        if holes_crew_type:
            hole_opts.append(f"Next hole IN crew+type ({cur_type_name} ∩ Crew {crew}): {holes_crew_type[0]} (also {', '.join(map(str,holes_crew_type[1:3]))})")
        if holes_type:
            hole_opts.append(f"Next hole IN type ({cur_type_name}): {holes_type[0]}")
        if holes_crew:
            hole_opts.append(f"Next hole IN crew {crew} (any type): {holes_crew[0]}")
        if not hole_opts:
            hole_opts.append("No holes found in searched ranges")

        at_end_opts = []
        if at_end_crew_type: at_end_opts.append(f"At end IN crew+type: {at_end_crew_type}")
        if at_end_type: at_end_opts.append(f"At end IN type: {at_end_type}")
        if at_end_crew: at_end_opts.append(f"At end IN crew: {at_end_crew}")

        # Show as buttons
        btn_hole_crew_type = QPushButton(f"Fill hole crew+type → {holes_crew_type[0] if holes_crew_type else 'none'}")
        btn_hole_crew_type.setEnabled(bool(holes_crew_type))
        btn_hole_crew_type.clicked.connect(lambda: self._pick(holes_crew_type[0], "hole", f"Fill hole crew+type {cur_type_name}"))

        btn_hole_type = QPushButton(f"Fill hole type → {holes_type[0] if holes_type else 'none'}")
        btn_hole_type.setEnabled(bool(holes_type))
        btn_hole_type.clicked.connect(lambda: self._pick(holes_type[0], "hole", f"Fill hole type {cur_type_name}"))

        btn_at_end = QPushButton(f"At end crew+type → {at_end_crew_type}")
        btn_at_end.setEnabled(bool(at_end_crew_type))
        btn_at_end.clicked.connect(lambda: self._pick(at_end_crew_type, "at_end", f"At end crew+type {cur_type_name}"))

        btn_at_end_type = QPushButton(f"At end type → {at_end_type}")
        btn_at_end_type.setEnabled(bool(at_end_type))
        btn_at_end_type.clicked.connect(lambda: self._pick(at_end_type, "at_end", f"At end type {cur_type_name}"))

        # Break-out toggle
        self.break_out_label = QLabel("Break-out: shows Outside vs Inside crew block — crew 3 Control is 300-399, but Control 1-999 outside crew is also an option (Hole IN type but NOT IN crew).")
        self.break_out_label.setWordWrap(True)
        self.break_out_label.setStyleSheet("color:#555; font-size:11px;")

        row1 = QHBoxLayout(); row1.addWidget(btn_hole_crew_type); row1.addWidget(btn_at_end)
        row2 = QHBoxLayout(); row2.addWidget(btn_hole_type); row2.addWidget(btn_at_end_type)
        sug_form.addRow(row1)
        sug_form.addRow(row2)
        sug_form.addRow(self.break_out_label)
        lay.addWidget(sug_box)

        # Manual key-in
        man_box = QGroupBox("Manual / Inject")
        man_lay = QHBoxLayout(man_box)
        man_lay.addWidget(QLabel("Key-In PtNum:"))
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("e.g., 350 — will check against used + external + Carlson ranges")
        man_lay.addWidget(self.manual_edit)
        manual_btn = QPushButton("Use Key-In")
        manual_btn.clicked.connect(self._do_manual)
        man_lay.addWidget(manual_btn)
        inject_btn = QPushButton("Inject (shift all after +1)")
        inject_btn.setToolTip("Raw data with trace back — shifts tail, keeps line order. Preview before accept.")
        inject_btn.clicked.connect(self._do_inject)
        man_lay.addWidget(inject_btn)
        lay.addWidget(man_box)

        # Info
        info = QLabel(f"Used locally: {len(self.used)} | External (Carlson) used: {len(self.external_used)} | All used: {len(all_used)} — suggestions avoid both. Final write creates clean 5-col CSV only where Corr_Status=Accepted.")
        info.setWordWrap(True); info.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(info)

        # Bottom
        bot = QHBoxLayout(); bot.addStretch()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject); bot.addWidget(cancel)
        lay.addLayout(bot)

    def _pick(self, pt, typ, detail):
        self.result_new_pt = str(pt)
        self.result_type = typ
        self.result_detail = detail
        self.accept()

    def _do_manual(self):
        txt = self.manual_edit.text().strip()
        if not txt:
            QMessageBox.warning(self, "Key-In", "Enter a point number.")
            return
        try:
            n = int(txt)
        except:
            QMessageBox.warning(self, "Key-In", "Point number must be integer.")
            return
        if n in self.all_used:
            resp = QMessageBox.question(self, "Key-In", f"{n} already used (local or Carlson) — use anyway? Will be duplicate until you shift.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if resp != QMessageBox.StandardButton.Yes:
                return
        self.result_new_pt = txt
        self.result_type = "manual"
        self.result_detail = f"Manual {txt}"
        self.accept()

    def _do_inject(self):
        txt = self.manual_edit.text().strip()
        if not txt:
            QMessageBox.warning(self, "Inject", "Enter a point number to inject at (e.g., 350).")
            return
        try:
            n = int(txt)
        except:
            QMessageBox.warning(self, "Inject", "Point number must be integer.")
            return
        # Preview: would shift all >=n up by 1
        affected = sorted([p for p in self.all_used if p >= n])[:8]
        preview = ", ".join(map(str, affected)) + (" ..." if len(affected)>=8 else "")
        resp = QMessageBox.question(self, "Inject", f"Inject at {n} will shift tail ({preview}) +1. Raw trace kept in SourceFile. Proceed with Corr_PtNum={n} + Inject flag?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if resp != QMessageBox.StandardButton.Yes:
            return
        self.result_new_pt = txt
        self.result_type = "inject"
        self.result_detail = f"Inject at {n}, shift tail"
        self.accept()

# ----- Duplicate Merge Helper -----
def _merge_duplicate_descriptions(raw_descs: list[str], f2f_set: set, fieldbook_path=None, commands=None) -> str:
    """Merge descriptions using the active Field Book separator meanings and spacing preferences."""
    if not raw_descs:
        return ""
    multicode_sep, description_sep = get_separator_texts(fieldbook_path, commands)
    separator_tokens = get_separator_tokens(fieldbook_path, commands)
    multicode_token = separator_tokens.get("multicode", "")
    multicode_sep = multicode_sep or " "
    description_sep = description_sep or " "
    try:
        from .parse import parse_desc_field
    except Exception:
        uniq = list(dict.fromkeys(str(rd).strip() for rd in raw_descs if str(rd).strip()))
        return multicode_sep.join(uniq) if uniq else ""
    codes = []
    seen_codes = set()
    frees = []
    seen_frees = set()
    for rd in raw_descs:
        rd = rd.strip()
        if not rd:
            continue
        try:
            parsed = parse_desc_field(rd, f2f_set or set(), fieldbook_path=fieldbook_path, commands=commands)
            code_part = parsed.get("code_part", "").strip() if isinstance(parsed, dict) else ""
            free_part = parsed.get("free_desc", "").strip() if isinstance(parsed, dict) else ""
            # code_part may contain MULTICODE_SEP already
            if code_part:
                # split by MULTICODE_SEP
                parts = [p.strip() for p in split_at_separator(code_part, multicode_token, maxsplit=0) if p.strip()] if multicode_token else [code_part]
                if not parts:
                    parts = [code_part]
                for cp in parts:
                    # Keep as is, dedup case-insensitive
                    low = cp.casefold()
                    if low not in seen_codes:
                        seen_codes.add(low)
                        codes.append(cp)
            if free_part:
                # free may contain multiple words; keep whole free string as unit, dedup
                lowf = free_part.casefold()
                if lowf not in seen_frees:
                    seen_frees.add(lowf)
                    frees.append(free_part)
            elif not code_part:
                # No parse, treat raw as free?
                lowf = rd.casefold()
                if lowf not in seen_frees:
                    seen_frees.add(lowf)
                    frees.append(rd)
        except Exception:
            # Fallback: treat raw as code
            low = rd.casefold()
            if low not in seen_codes:
                seen_codes.add(low)
                codes.append(rd)
    # Build merged
    merged_code = multicode_sep.join(codes) if codes else ""
    merged_free = " ".join(frees) if frees else ""
    if merged_code and merged_free:
        return f"{merged_code}{description_sep}{merged_free}"
    elif merged_code:
        return merged_code
    elif merged_free:
        return merged_free
    else:
        # Fallback dedup raw
        uniq = []
        seen = set()
        for rd in raw_descs:
            r = rd.strip()
            if r and r.casefold() not in seen:
                seen.add(r.casefold())
                uniq.append(r)
        return multicode_sep.join(uniq)

class DuplicateMergeDialog(QDialog):
    """Duplicate Set Merge — Primary/Merge/Ignore/Remove per OID in a GroupID set.
    Enforces only 1 Primary, warns if >1. User must Approve/Deny. Preview shows merged desc for Primary+Merge.
    Choices:
      Primary — keeper, will receive merged desc (only 1 per set, enforced)
      Merge — contributes desc to Primary and will be Removed (deleted) on Approve
      Ignore — pulls out of merge, keeps point as Ignored (not removed, not merged)
      Remove — excludes from merge and will be Removed (deleted, not merged)
    On Approve, returns dict: primary_oid, merge_oids, ignore_oids, remove_oids, merged_desc
    """
    def __init__(self, group_id: str, issue_type: str, rows: list, parent=None, f2f_set=None, fieldbook_path=None):
        # rows: list of dicts {oid, pt_num, n, e, z, desc, detail, status, comments, raw_row}
        super().__init__(parent)
        self.setWindowTitle(f"Duplicate Set — Group {group_id}  [{issue_type}]  ({len(rows)} Points)")
        self.resize(1100, 620)
        self.group_id = group_id
        self.issue_type = issue_type
        self.rows = rows
        self.f2f_set = f2f_set or set()
        self.fieldbook_path = fieldbook_path
        self.result_action = None  # "approve" or "deny"
        self.result_primary = None
        self.result_merge = []
        self.result_ignore = []
        self.result_remove = []
        self.result_merged_desc = ""
        outer = QVBoxLayout(self)
        info = QLabel(f"Group {group_id} - {issue_type} - {len(rows)} points. Choose 1 Primary, then for each other point select Merge / Ignore / Remove. Only 1 Primary allowed. Merge = include desc in Primary and delete point. Ignore = keep point (not merged). Remove = delete point (not merged). Must Approve to apply.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #333; font-size: 11px; font-weight: bold;")
        outer.addWidget(info)
        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["OID", "PtNum", "N", "E", "Z", "Raw Desc", "Detail", "Choice"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(False)
        outer.addWidget(self.table)
        self.combos = []  # list of QComboBox per row
        for r in rows:
            idx = self.table.rowCount()
            self.table.insertRow(idx)
            self.table.setItem(idx, 0, QTableWidgetItem(str(r.get("oid",""))))
            self.table.setItem(idx, 1, QTableWidgetItem(str(r.get("pt_num",""))))
            self.table.setItem(idx, 2, QTableWidgetItem(str(r.get("n",""))))
            self.table.setItem(idx, 3, QTableWidgetItem(str(r.get("e",""))))
            self.table.setItem(idx, 4, QTableWidgetItem(str(r.get("z",""))))
            desc_item = QTableWidgetItem(str(r.get("desc","")))
            desc_item.setToolTip(str(r.get("desc","")))
            self.table.setItem(idx, 5, desc_item)
            self.table.setItem(idx, 6, QTableWidgetItem(str(r.get("detail",""))))
            cb = QComboBox()
            cb.addItems(["Merge", "Ignore", "Remove", "Primary"])
            # Default: first row Primary, others Merge (if not already handled)
            cur_status = str(r.get("status","")).strip()
            if cur_status == "Ignored":
                cb.setCurrentText("Ignore")
            elif cur_status == "Removed":
                cb.setCurrentText("Remove")
            elif idx == 0 and cur_status == "Open":
                cb.setCurrentText("Primary")
            else:
                # If already has merged/primary, keep as Merge unless status handled
                if cur_status in ("Merged", "Corrected"):
                    cb.setCurrentText("Primary")
                else:
                    cb.setCurrentText("Merge")
            cb.currentTextChanged.connect(self._on_choice_changed)
            self.table.setCellWidget(idx, 7, cb)
            self.combos.append(cb)
            # Color row based on issue
            if idx == 0:
                for c in range(7):
                    it = self.table.item(idx, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E3F2FD") if issue_type=="ExactDuplicate" else QColor("#FFF8E1") if issue_type=="SimilarNumber" else QColor("#E8F5E9")))
        self.table.resizeColumnsToContents()
        # Preview area
        preview_box = QGroupBox("Merged Preview (Primary + Merge)")
        pv_lay = QVBoxLayout(preview_box)
        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("font-family: monospace; font-size: 11px; color: #2E7D32; font-weight: bold; background: #E8F5E9; padding: 6px;")
        self.preview_label.setText("(select Primary and Merge to preview)")
        pv_lay.addWidget(self.preview_label)
        self.detail_preview = QLabel("")
        self.detail_preview.setWordWrap(True)
        self.detail_preview.setStyleSheet("color: #555; font-size: 10px;")
        pv_lay.addWidget(self.detail_preview)
        outer.addWidget(preview_box)
        # Count / warning
        self.warn_label = QLabel("")
        self.warn_label.setStyleSheet("color: #C62828; font-weight: bold; font-size: 11px;")
        self.warn_label.setWordWrap(True)
        outer.addWidget(self.warn_label)
        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.deny_btn = QPushButton("Deny")
        self.deny_btn.setToolTip("Deny — close without applying (no changes)")
        self.deny_btn.clicked.connect(self.reject)
        self.approve_btn = QPushButton("Approve — Apply Merge")
        self.approve_btn.setStyleSheet("background-color: #E8F5E9; font-weight: bold;")
        self.approve_btn.setToolTip("Approve — must have exactly 1 Primary to apply; will merge Primary+Merge descs and mark Merge/Remove as Removed, Ignore as Ignored")
        self.approve_btn.clicked.connect(self._do_approve)
        btn_row.addWidget(self.deny_btn)
        btn_row.addWidget(self.approve_btn)
        outer.addLayout(btn_row)
        self._update_preview()

    def _on_choice_changed(self, txt):
        # Enforce only 1 Primary
        primaries = [cb.currentText() for cb in self.combos].count("Primary")
        if primaries > 1:
            self.warn_label.setText(f"⚠ Only 1 Primary allowed — you have {primaries} selected. Please keep only 1 Primary, change others to Merge/Ignore/Remove.")
            self.approve_btn.setEnabled(False)
        elif primaries == 0:
            self.warn_label.setText("⚠ No Primary selected — select exactly 1 Primary to merge into.")
            self.approve_btn.setEnabled(False)
        else:
            self.warn_label.setText("")
            self.approve_btn.setEnabled(True)
        self._update_preview()

    def _update_preview(self):
        try:
            primaries = [i for i, cb in enumerate(self.combos) if cb.currentText() == "Primary"]
            merges = [i for i, cb in enumerate(self.combos) if cb.currentText() == "Merge"]
            if not primaries:
                self.preview_label.setText("(no Primary)")
                self.detail_preview.setText("Select exactly 1 Primary. Merge points will have descs combined into Primary.")
                return
            if len(primaries) > 1:
                self.preview_label.setText("(multiple Primaries — fix before preview)")
                return
            # Collect raw descs for Primary + Merge
            raw_list = []
            for idx in primaries + merges:
                rd = self.table.item(idx, 5).text() if self.table.item(idx, 5) else ""
                raw_list.append(rd)
            merged = _merge_duplicate_descriptions(raw_list, self.f2f_set, fieldbook_path=self.fieldbook_path)
            # Also show counts
            ignores = sum(1 for cb in self.combos if cb.currentText() == "Ignore")
            removes = sum(1 for cb in self.combos if cb.currentText() == "Remove")
            self.preview_label.setText(merged if merged else "(empty merged desc — check raw)")
            self.detail_preview.setText(f"Primary OID {self.table.item(primaries[0],0).text() if primaries else ''} + {len(merges)} Merge → 1 kept point. {ignores} Ignore (kept, not merged), {removes} Remove (deleted, not merged). Total {len(self.combos)} in set.")
        except Exception as e:
            self.preview_label.setText(f"Preview error: {e}")

    def _do_approve(self):
        primaries = [i for i, cb in enumerate(self.combos) if cb.currentText() == "Primary"]
        if len(primaries) != 1:
            QMessageBox.warning(self, "Primary Required", f"Exactly 1 Primary required — you have {len(primaries)}. Please set only 1 Primary.")
            return
        # Confirm
        # Build preview again for confirm text
        merges = [i for i, cb in enumerate(self.combos) if cb.currentText() == "Merge"]
        ignores = [i for i, cb in enumerate(self.combos) if cb.currentText() == "Ignore"]
        removes = [i for i, cb in enumerate(self.combos) if cb.currentText() == "Remove"]
        raw_list = [self.table.item(i,5).text() if self.table.item(i,5) else "" for i in ([primaries[0]] + merges)]
        merged = _merge_duplicate_descriptions(raw_list, self.f2f_set, fieldbook_path=self.fieldbook_path)
        prim_oid = self.table.item(primaries[0],0).text()
        msg = f"Approve merge for Group {self.group_id}?\\n\\nPrimary OID {prim_oid} will get merged desc:\\n'{merged}'\\n\\nMerge ({len(merges)}): will be Removed after merging descs\\nIgnore ({len(ignores)}): kept, not merged\\nRemove ({len(removes)}): deleted, not merged\\n\\nThis will mark Merge/Remove as Removed (omitted in final out file) and Ignore as Ignored. Primary gets Merged status."
        resp = QMessageBox.question(self, "Approve Merge", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if resp != QMessageBox.StandardButton.Yes:
            return
        self.result_primary = prim_oid
        self.result_merge = [self.table.item(i,0).text() for i in merges]
        self.result_ignore = [self.table.item(i,0).text() for i in ignores]
        self.result_remove = [self.table.item(i,0).text() for i in removes]
        self.result_merged_desc = merged
        self.result_action = "approve"
        self.accept()

class BatchDuplicateDialog(QDialog):
    """Batch Duplicate Merge — lists all open duplicate groups, checkbox to include, auto-primary = smallest OID.
    For each group, merges Primary+Merge descs with codes - codes / frees. User checks which groups to apply.
    """
    def __init__(self, groups: list, parent=None, f2f_set=None, fieldbook_path=None):
        # groups: list of dict {group_id, issue_type, rows: [{oid, pt_num, n, e, z, desc, detail, status, comments}]}
        super().__init__(parent)
        self.setWindowTitle(f"Fix Auto — Duplicate Merge ({len(groups)} Groups)")
        self.resize(1000, 600)
        self.groups = groups
        self.f2f_set = f2f_set or set()
        self.fieldbook_path = fieldbook_path
        self.result_selected = []  # list of (group_id, primary_oid, merge_oids, ignore_oids, remove_oids, merged_desc)
        outer = QVBoxLayout(self)
        info = QLabel(f"Fix Auto — {len(groups)} duplicate groups with Status=Open. Checked groups will be auto-merged (Primary = smallest OID, others = Merge). Uncheck to skip. You can also double-click a group to handle manually via Fix First.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #333; font-size: 11px;")
        outer.addWidget(info)
        sel_row = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        self.clear_all_btn = QPushButton("Clear All")
        self.clear_all_btn.clicked.connect(self._clear_all)
        sel_row.addWidget(self.select_all_btn)
        sel_row.addWidget(self.clear_all_btn)
        sel_row.addStretch()
        self.count_label = QLabel("")
        sel_row.addWidget(self.count_label)
        outer.addLayout(sel_row)
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Include", "GroupID", "IssueType", "OIDs (Primary* first)", "Merged Preview"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        outer.addWidget(self.table)
        self.table.setSortingEnabled(False)
        for g in groups:
            gid = g.get("group_id","")
            issue = g.get("issue_type","")
            rows = g.get("rows", [])
            # Sort rows by OID numeric
            try:
                rows_sorted = sorted(rows, key=lambda r: int(str(r.get("oid","")).strip()) if str(r.get("oid","")).strip().isdigit() else str(r.get("oid","")).strip())
            except:
                rows_sorted = rows
            if not rows_sorted:
                continue
            primary = rows_sorted[0]
            merges = rows_sorted[1:]
            # Build merged preview using primary + merges
            raw_list = [r.get("desc","") for r in [primary] + merges]
            merged = _merge_duplicate_descriptions(raw_list, self.f2f_set, fieldbook_path=self.fieldbook_path)
            oids_str = ", ".join([str(r.get("oid","")) + ("*" if i==0 else "") for i, r in enumerate(rows_sorted)])
            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(str(gid)))
            self.table.setItem(r, 2, QTableWidgetItem(str(issue)))
            oids_item = QTableWidgetItem(oids_str)
            oids_item.setToolTip("Primary* = smallest OID, others Merge")
            self.table.setItem(r, 3, oids_item)
            prev_item = QTableWidgetItem(merged)
            prev_item.setToolTip(merged)
            prev_item.setBackground(QBrush(QColor("#E8F5E9")))
            self.table.setItem(r, 4, prev_item)
            # Store full group for later
            # Use UserRole to store group data? Instead keep parallel list
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)
        self.table.itemChanged.connect(lambda it: self._update_count() if it.column()==0 else None)
        self._update_count()
        bot = QHBoxLayout()
        bot.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.apply_btn = QPushButton("Apply Auto Merge to Selected")
        self.apply_btn.setStyleSheet("background-color: #E8F5E9; font-weight: bold;")
        self.apply_btn.clicked.connect(self._do_apply)
        bot.addWidget(self.cancel_btn)
        bot.addWidget(self.apply_btn)
        outer.addLayout(bot)
        self._update_count()

    def _select_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it:
                it.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_count()

    def _clear_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it:
                it.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_count()

    def _update_count(self):
        try:
            cnt = sum(1 for r in range(self.table.rowCount()) if self.table.item(r,0) and self.table.item(r,0).checkState()==Qt.CheckState.Checked)
            total = self.table.rowCount()
            self.count_label.setText(f"{cnt} of {total} groups selected")
            if hasattr(self, 'apply_btn'):
                self.apply_btn.setText(f"Apply Auto Merge to {cnt} Selected" if cnt else "Apply Auto Merge to Selected")
                self.apply_btn.setEnabled(cnt>0)
        except:
            pass

    def _do_apply(self):
        sel = []
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and chk.checkState()==Qt.CheckState.Checked:
                gid = self.table.item(r, 1).text().strip() if self.table.item(r,1) else ""
                # Find original group
                g = next((gg for gg in self.groups if str(gg.get("group_id","")).strip()==gid), None)
                if not g:
                    continue
                rows = g.get("rows", [])
                try:
                    rows_sorted = sorted(rows, key=lambda rr: int(str(rr.get("oid","")).strip()) if str(rr.get("oid","")).strip().isdigit() else str(rr.get("oid","")).strip())
                except:
                    rows_sorted = rows
                if not rows_sorted:
                    continue
                primary_oid = str(rows_sorted[0].get("oid","")).strip()
                merge_oids = [str(rr.get("oid","")).strip() for rr in rows_sorted[1:]]
                raw_list = [rr.get("desc","") for rr in rows_sorted]
                merged = _merge_duplicate_descriptions(raw_list, self.f2f_set, fieldbook_path=self.fieldbook_path)
                sel.append((gid, primary_oid, merge_oids, [], [], merged))
        if not sel:
            QMessageBox.warning(self, "Fix Auto", "No groups selected.")
            return
        # Confirm
        msg = f"Apply auto-merge to {len(sel)} groups? Primary = smallest OID per group, others = Merge (Removed). No Ignores in auto mode — use Fix First for custom Ignore/Remove."
        resp = QMessageBox.question(self, "Confirm Auto Merge", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if resp != QMessageBox.StandardButton.Yes:
            return
        self.result_selected = sel
        self.accept()

# Helper for renumber lock

# ----- Duplicate Renumber Dialog (2026-09-24) -----
class DuplicateRenumberDialog(QDialog):
    """Renumber same-number points after Auto — hole/at-end/keyed-in per point, writes to Dup_Renumber column (final collapsed data), unlocked.
    Covers Control 1-999, Boundary 1000-9999, General 10000+, crew blocks 300/3000/30000 etc.
    Options per row: Hole (next hole in crew+type), At End (max+1), Keyed-In (user typed, validated).
    """
    def __init__(self, dup_rows: list, crew: int, used_numbers: set[int], external_used: set[int]=None, master_used: set[int]=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Renumber Duplicates — {len(dup_rows)} Points (Hole / At-End / Keyed-In)")
        self.resize(950, 500)
        self.dup_rows = dup_rows  # list of dict {oid, cur_pt, type_label}
        self.crew = crew
        self.used = set(used_numbers) if used_numbers else set()
        self.external_used = set(external_used) if external_used else set()
        self.master_used = set(master_used) if master_used else set()
        self.all_used = self.used | self.external_used | self.master_used
        self.result_map = {}  # oid -> (new_pt, method, detail)

        from .config import CONTROL_RANGE, BOUNDARY_RANGE, GENERAL_START, crew_blocks, find_holes_in_ranges, next_at_end, number_type_label

        outer = QVBoxLayout(self)
        info = QLabel("Duplicate renumber — per point choose: Hole (conserve), At End, or Key-In. This writes to final collapsed column (Corr_DupRenumber) and does NOT lock renumber tool. Crew blocks: 300/3000/30000 etc. + type ranges.")
        info.setWordWrap(True); info.setStyleSheet("color:#333; font-size:11px; font-weight:bold;")
        outer.addWidget(info)

        # Info about ranges
        rng = QLabel(f"Control {CONTROL_RANGE}  Boundary {BOUNDARY_RANGE}  General {GENERAL_START}+  Crew {crew} blocks {crew_blocks(crew)[:3]} ...  Used: local {len(self.used)} + external {len(self.external_used)} + master {len(self.master_used)} = {len(self.all_used)}")
        rng.setWordWrap(True); rng.setStyleSheet("color:#555; font-size:10px;")
        outer.addWidget(rng)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["Include","OID","Current","Type","Hole →","At End →","Key-In","Chosen"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        outer.addWidget(self.table)

        # Populate
        for rec in dup_rows:
            oid = str(rec.get("oid",""))
            cur = str(rec.get("cur_pt",""))
            # Determine type range
            try:
                n = int(cur)
                if CONTROL_RANGE[0] <= n <= CONTROL_RANGE[1]:
                    cur_type_range = CONTROL_RANGE; cur_type_name = "Control"
                elif BOUNDARY_RANGE[0] <= n <= BOUNDARY_RANGE[1]:
                    cur_type_range = BOUNDARY_RANGE; cur_type_name = "Boundary"
                else:
                    cur_type_range = (GENERAL_START, 9999999); cur_type_name = "General"
            except:
                cur_type_range = (GENERAL_START, 9999999); cur_type_name = "General"

            holes = find_holes_in_ranges(self.all_used, crew=crew, type_range=cur_type_range)
            hole = holes[0] if holes else None
            at_end = next_at_end(self.all_used, crew=crew, type_range=cur_type_range)

            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QTableWidgetItem(); chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable); chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(oid))
            self.table.setItem(r, 2, QTableWidgetItem(cur))
            self.table.setItem(r, 3, QTableWidgetItem(cur_type_name))
            # Hole button as item with text
            hole_item = QTableWidgetItem(str(hole) if hole else "none")
            hole_item.setToolTip(f"Next hole IN crew+type ({cur_type_name} ∩ Crew {crew})" if hole else "No hole")
            hole_item.setBackground(QBrush(QColor("#E8F5E9")) if hole else QBrush(QColor("#FFEBEE")))
            hole_item.setFlags(hole_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 4, hole_item)
            at_item = QTableWidgetItem(str(at_end) if at_end else "none")
            at_item.setToolTip(f"Next at end IN crew+type" if at_end else "none")
            at_item.setFlags(at_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 5, at_item)
            key_item = QTableWidgetItem("")
            key_item.setToolTip("Key-In: type a number and it will be used if valid, else warned if duplicate")
            key_item.setFlags(key_item.flags() | Qt.ItemFlag.ItemIsEditable)
            key_item.setBackground(QBrush(QColor("#FFFDE7")))
            self.table.setItem(r, 6, key_item)
            chosen = QTableWidgetItem(str(hole) if hole else (str(at_end) if at_end else ""))
            chosen.setToolTip("Chosen renumber — click Hole or At End buttons below to fill, or type Key-In")
            chosen.setBackground(QBrush(QColor("#E3F2FD")))
            self.table.setItem(r, 7, chosen)

        self.table.resizeColumnsToContents()

        # Buttons row for per-selection
        btn_row = QHBoxLayout()
        self.btn_hole = QPushButton("Apply Hole to Selected")
        self.btn_hole.clicked.connect(lambda: self._apply_to_selected("hole"))
        self.btn_atend = QPushButton("Apply At End to Selected")
        self.btn_atend.clicked.connect(lambda: self._apply_to_selected("at_end"))
        self.btn_keyed = QPushButton("Apply Key-In to Selected")
        self.btn_keyed.clicked.connect(lambda: self._apply_to_selected("keyed"))
        btn_row.addWidget(self.btn_hole); btn_row.addWidget(self.btn_atend); btn_row.addWidget(self.btn_keyed); btn_row.addStretch()
        outer.addLayout(btn_row)

        # Select all / clear
        sel_row = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All"); self.select_all_btn.clicked.connect(self._select_all)
        self.clear_all_btn = QPushButton("Clear All"); self.clear_all_btn.clicked.connect(self._clear_all)
        sel_row.addWidget(self.select_all_btn); sel_row.addWidget(self.clear_all_btn); sel_row.addStretch()
        self.count_label = QLabel(""); sel_row.addWidget(self.count_label)
        outer.addLayout(sel_row)

        self.table.itemChanged.connect(lambda it: self._update_count() if it and it.column()==0 else None)
        self._update_count()

        # Bottom
        bot = QHBoxLayout(); bot.addStretch()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject); bot.addWidget(cancel)
        self.apply_btn = QPushButton("Apply Renumber to Final Column"); self.apply_btn.setStyleSheet("background-color: #E8F5E9; font-weight: bold;")
        self.apply_btn.clicked.connect(self._do_apply); bot.addWidget(self.apply_btn)
        outer.addLayout(bot)

    def _select_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it=self.table.item(r,0)
            if it: it.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False); self._update_count()
    def _clear_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it=self.table.item(r,0)
            if it: it.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False); self._update_count()
    def _update_count(self):
        try:
            cnt=sum(1 for r in range(self.table.rowCount()) if self.table.item(r,0) and self.table.item(r,0).checkState()==Qt.CheckState.Checked)
            total=self.table.rowCount()
            self.count_label.setText(f"{cnt} of {total} selected")
            if hasattr(self,'apply_btn'):
                self.apply_btn.setEnabled(cnt>0)
        except: pass
    def _apply_to_selected(self, mode):
        for r in range(self.table.rowCount()):
            chk=self.table.item(r,0)
            if not chk or chk.checkState()!=Qt.CheckState.Checked:
                continue
            hole_txt=self.table.item(r,4).text() if self.table.item(r,4) else ""
            at_txt=self.table.item(r,5).text() if self.table.item(r,5) else ""
            key_txt=self.table.item(r,6).text().strip() if self.table.item(r,6) else ""
            chosen_item=self.table.item(r,7)
            if mode=="hole" and hole_txt and hole_txt!="none":
                chosen_item.setText(hole_txt)
            elif mode=="at_end" and at_txt and at_txt!="none":
                chosen_item.setText(at_txt)
            elif mode=="keyed" and key_txt:
                # Validate keyed
                try:
                    n=int(key_txt)
                    if n in self.all_used:
                        QMessageBox.warning(self, "Key-In", f"{n} already used (local/master) — will still apply but will be duplicate until recheck.")
                    chosen_item.setText(key_txt)
                except:
                    QMessageBox.warning(self, "Key-In", f"Invalid number '{key_txt}'")
            # else if keyed empty, keep hole

    def _do_apply(self):
        mp={}
        for r in range(self.table.rowCount()):
            chk=self.table.item(r,0)
            if not chk or chk.checkState()!=Qt.CheckState.Checked:
                continue
            oid=self.table.item(r,1).text().strip() if self.table.item(r,1) else ""
            cur=self.table.item(r,2).text().strip() if self.table.item(r,2) else ""
            chosen=self.table.item(r,7).text().strip() if self.table.item(r,7) else ""
            keyed=self.table.item(r,6).text().strip() if self.table.item(r,6) else ""
            hole=self.table.item(r,4).text().strip() if self.table.item(r,4) else ""
            at_end=self.table.item(r,5).text().strip() if self.table.item(r,5) else ""
            if not chosen:
                continue
            # Determine method
            if chosen==keyed and keyed:
                method="keyed"
            elif chosen==hole:
                method="hole"
            elif chosen==at_end:
                method="at_end"
            else:
                method="manual" if keyed else ("hole" if chosen==hole else "at_end")
            # Validate not duplicate of already chosen in this batch or existing used
            try:
                n=int(chosen)
                # Check against all_used + other chosen
                other_chosen = {int(v[0]) for k,v in mp.items() if v[0].isdigit()}
                if n in self.all_used and str(n)!=cur:
                    # Allow but warn? Overwrite protection: if n in all_used, it would duplicate — we still allow with warning already
                    pass
                if n in other_chosen:
                    QMessageBox.warning(self, "Renumber", f"Duplicate chosen number {n} in batch — pick unique per point.")
                    return
            except:
                QMessageBox.warning(self, "Renumber", f"Invalid number '{chosen}' for OID {oid}")
                return
            mp[oid]=(chosen, method, f"{method} {cur}→{chosen} (crew {self.crew})")
            # Update used to avoid next suggestions reusing same number in this batch (incremental)
            try: self.all_used.add(int(chosen))
            except: pass
        if not mp:
            QMessageBox.warning(self, "Renumber", "No rows selected/valid — check Include and choose Hole/At End/Key-In")
            return
        self.result_map=mp
        self.accept()

# ----- Global Renumber Dialog (2026-09-24) -----
class GlobalRenumberDialog(QDialog):
    """Global Renumber — checks against master file + duplicate corrected numbers (dup numbers before originals). Overwrites duplicate renumbering. Hole/at-end/keyed-in per point, with master CSV path.
    """
    def __init__(self, rows: list, crew: int, used_numbers: set[int], master_used: set[int], dup_corrected: set[int], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Global Renumber — {len(rows)} Points (Master + Dup Corrected as Used, Overwrites)")
        self.resize(950, 520)
        self.rows = rows
        self.crew = crew
        self.used = set(used_numbers) if used_numbers else set()
        self.master_used = set(master_used) if master_used else set()
        self.dup_corrected = set(dup_corrected) if dup_corrected else set()
        # all_used = master + dup_corrected + local used (dup_corrected read before originals per spec)
        self.all_used = self.used | self.master_used | self.dup_corrected
        self.result_map = {}

        from .config import CONTROL_RANGE, BOUNDARY_RANGE, GENERAL_START, crew_blocks, find_holes_in_ranges, next_at_end

        outer = QVBoxLayout(self)
        info = QLabel("Global Renumber — master file + duplicate corrected numbers are treated as used before originals. This column overwrites duplicate renumbering on final export. Offers hole / at-end / keyed-in per point.")
        info.setWordWrap(True); info.setStyleSheet("color:#333; font-size:11px; font-weight:bold;")
        outer.addWidget(info)
        master_info = QLabel(f"Master used: {len(self.master_used)}  Dup corrected as used: {len(self.dup_corrected)} (read before originals)  Local used: {len(self.used)}  Total blocked: {len(self.all_used)}  Master path: {getattr(parent,'master_file_path','') if parent else ''}")
        master_info.setWordWrap(True); master_info.setStyleSheet("color:#555; font-size:10px;")
        outer.addWidget(master_info)
        if not self.master_used:
            warn = QLabel("No master file set — set in Settings > Project Paths > Master File CSV. Without it, global behaves like duplicate renumber (local + dup corrected only).")
            warn.setWordWrap(True); warn.setStyleSheet("color:#C62828; font-size:11px;")
            outer.addWidget(warn)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["Include","OID","Current","Type","Hole →","At End →","Key-In","Global Chosen"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setAlternatingRowColors(True)
        outer.addWidget(self.table)

        for rec in rows:
            oid=str(rec.get("oid",""))
            cur=str(rec.get("cur_pt",""))
            try:
                n=int(cur)
                if CONTROL_RANGE[0] <= n <= CONTROL_RANGE[1]:
                    cur_type_range=CONTROL_RANGE; cur_type_name="Control"
                elif BOUNDARY_RANGE[0] <= n <= BOUNDARY_RANGE[1]:
                    cur_type_range=BOUNDARY_RANGE; cur_type_name="Boundary"
                else:
                    cur_type_range=(GENERAL_START, 9999999); cur_type_name="General"
            except:
                cur_type_range=(GENERAL_START, 9999999); cur_type_name="General"
            holes=find_holes_in_ranges(self.all_used, crew=crew, type_range=cur_type_range)
            hole=holes[0] if holes else None
            at_end=next_at_end(self.all_used, crew=crew, type_range=cur_type_range)
            r=self.table.rowCount(); self.table.insertRow(r)
            chk=QTableWidgetItem(); chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable); chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r,0,chk)
            self.table.setItem(r,1,QTableWidgetItem(oid))
            self.table.setItem(r,2,QTableWidgetItem(cur))
            self.table.setItem(r,3,QTableWidgetItem(cur_type_name))
            hole_item=QTableWidgetItem(str(hole) if hole else "none"); hole_item.setFlags(hole_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            hole_item.setBackground(QBrush(QColor("#E8F5E9")) if hole else QBrush(QColor("#FFEBEE")))
            self.table.setItem(r,4,hole_item)
            at_item=QTableWidgetItem(str(at_end) if at_end else "none"); at_item.setFlags(at_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r,5,at_item)
            key_item=QTableWidgetItem(""); key_item.setFlags(key_item.flags() | Qt.ItemFlag.ItemIsEditable); key_item.setBackground(QBrush(QColor("#FFFDE7")))
            self.table.setItem(r,6,key_item)
            chosen=QTableWidgetItem(str(hole) if hole else (str(at_end) if at_end else "")); chosen.setBackground(QBrush(QColor("#FFF3E0")))
            self.table.setItem(r,7,chosen)
        self.table.resizeColumnsToContents()

        btn_row=QHBoxLayout()
        self.btn_hole=QPushButton("Apply Hole to Selected"); self.btn_hole.clicked.connect(lambda: self._apply_to_selected("hole"))
        self.btn_atend=QPushButton("Apply At End to Selected"); self.btn_atend.clicked.connect(lambda: self._apply_to_selected("at_end"))
        self.btn_keyed=QPushButton("Apply Key-In to Selected"); self.btn_keyed.clicked.connect(lambda: self._apply_to_selected("keyed"))
        btn_row.addWidget(self.btn_hole); btn_row.addWidget(self.btn_atend); btn_row.addWidget(self.btn_keyed); btn_row.addStretch()
        outer.addLayout(btn_row)

        sel_row=QHBoxLayout()
        self.select_all_btn=QPushButton("Select All"); self.select_all_btn.clicked.connect(self._select_all)
        self.clear_all_btn=QPushButton("Clear All"); self.clear_all_btn.clicked.connect(self._clear_all)
        sel_row.addWidget(self.select_all_btn); sel_row.addWidget(self.clear_all_btn); sel_row.addStretch()
        self.count_label=QLabel(""); sel_row.addWidget(self.count_label)
        outer.addLayout(sel_row)
        self.table.itemChanged.connect(lambda it: self._update_count() if it and it.column()==0 else None)
        self._update_count()

        bot=QHBoxLayout(); bot.addStretch()
        cancel=QPushButton("Cancel"); cancel.clicked.connect(self.reject); bot.addWidget(cancel)
        self.apply_btn=QPushButton("Apply Global Renumber (overwrites)"); self.apply_btn.setStyleSheet("background-color: #FFF3E0; font-weight: bold;")
        self.apply_btn.clicked.connect(self._do_apply); bot.addWidget(self.apply_btn)
        outer.addLayout(bot)

    def _select_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it=self.table.item(r,0)
            if it: it.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False); self._update_count()
    def _clear_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it=self.table.item(r,0)
            if it: it.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False); self._update_count()
    def _update_count(self):
        try:
            cnt=sum(1 for r in range(self.table.rowCount()) if self.table.item(r,0) and self.table.item(r,0).checkState()==Qt.CheckState.Checked)
            total=self.table.rowCount()
            self.count_label.setText(f"{cnt} of {total} selected")
            if hasattr(self,'apply_btn'): self.apply_btn.setEnabled(cnt>0)
        except: pass
    def _apply_to_selected(self, mode):
        for r in range(self.table.rowCount()):
            chk=self.table.item(r,0)
            if not chk or chk.checkState()!=Qt.CheckState.Checked: continue
            hole_txt=self.table.item(r,4).text() if self.table.item(r,4) else ""
            at_txt=self.table.item(r,5).text() if self.table.item(r,5) else ""
            key_txt=self.table.item(r,6).text().strip() if self.table.item(r,6) else ""
            chosen_item=self.table.item(r,7)
            if mode=="hole" and hole_txt and hole_txt!="none": chosen_item.setText(hole_txt)
            elif mode=="at_end" and at_txt and at_txt!="none": chosen_item.setText(at_txt)
            elif mode=="keyed" and key_txt:
                try:
                    n=int(key_txt)
                    if n in self.all_used: QMessageBox.warning(self, "Key-In", f"{n} already used — will apply but will be duplicate.")
                    chosen_item.setText(key_txt)
                except: QMessageBox.warning(self, "Key-In", f"Invalid '{key_txt}'")
    def _do_apply(self):
        mp={}
        for r in range(self.table.rowCount()):
            chk=self.table.item(r,0)
            if not chk or chk.checkState()!=Qt.CheckState.Checked: continue
            oid=self.table.item(r,1).text().strip() if self.table.item(r,1) else ""
            cur=self.table.item(r,2).text().strip() if self.table.item(r,2) else ""
            chosen=self.table.item(r,7).text().strip() if self.table.item(r,7) else ""
            keyed=self.table.item(r,6).text().strip() if self.table.item(r,6) else ""
            hole=self.table.item(r,4).text().strip() if self.table.item(r,4) else ""
            at_end=self.table.item(r,5).text().strip() if self.table.item(r,5) else ""
            if not chosen: continue
            if chosen==keyed and keyed: method="keyed"
            elif chosen==hole: method="hole"
            elif chosen==at_end: method="at_end"
            else: method="manual" if keyed else ("hole" if chosen==hole else "at_end")
            try:
                n=int(chosen)
                other_chosen={int(v[0]) for k,v in mp.items() if v[0].lstrip('-').isdigit()}
                if n in other_chosen:
                    QMessageBox.warning(self, "Global Renumber", f"Duplicate chosen {n} in batch — pick unique.")
                    return
            except:
                QMessageBox.warning(self, "Global Renumber", f"Invalid '{chosen}' for OID {oid}")
                return
            mp[oid]=(chosen, method, f"global {method} {cur}→{chosen}")
            try: self.all_used.add(int(chosen))
            except: pass
        if not mp:
            QMessageBox.warning(self, "Global Renumber", "No rows selected/valid")
            return
        self.result_map=mp
        self.accept()

# ----- Line Repair helpers (2026-09-24) -----
def detect_line_errors(working_rows, fieldbook_path=None):
    """Compatibility wrapper around the shared, Field Book-aware linework checker."""
    from .linecheck import detect_line_errors as detect
    return detect(working_rows, fieldbook_path=fieldbook_path)

def is_descriptions_clean(desc_table_row_count: int) -> bool:
    """Clean if Description Error tab has zero flagged rows (or all Skipped via dialog handling)."""
    return desc_table_row_count == 0

def validate_renumber_allowed(desc_row_count: int, fieldbook_path: str) -> tuple[bool, str]:
    if desc_row_count > 0:
        return False, f"Descriptions not clean — {desc_row_count} flagged rows remain. Clean all (Fix/Skip/Key-In) before renumbering to keep line order."
    if not fieldbook_path:
        return False, "No Field Book loaded."
    return True, "Ready to renumber"
