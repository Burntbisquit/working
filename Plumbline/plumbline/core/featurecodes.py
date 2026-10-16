"""Field codes ("descriptions") -> layers, symbols and linework.

A point description is parsed using the active Field Book's code and command meanings:

    EA <Start Line>                 begin an edge-of-asphalt string
    EA <End Line>                   finish that string
    FNC <Close>                     close a fence figure
    T14 <Description separator> OAK point code plus a free-text note

Points with the same resolved code and string identifier are joined in point order. Historical
Carlson boundary aliases remain readable when no active token overrides their meaning.
"""
from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass, field

import numpy as np

from .fieldbook_syntax import (
    COMMAND_MEANINGS,
    command_map,
    command_meanings,
    find_separator,
    split_at_separator,
)

BEGIN_FLAGS = {"B", "BEG", "BEGIN", "START"}
END_FLAGS = {"E", "END"}
# Historical Carlson aliases remain accepted; the active Field Book supplies its own Close token.
CLOSE_FLAGS = {"C", "CLS", "CLOSE"}

_TOKEN = re.compile(r"^([A-Za-z][A-Za-z_\-]*?)(\d+)?$")

#: The whole leading word of a description, digits included: "THACK22", "T05", "EC".
#: Used to try an exact code match *before* treating trailing digits as a string number.
_LEAD_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*")

#: Carlson writes the entity type as a *number* in its own field-to-finish export and as a
#: word in a table somebody has edited by hand, so both spellings have to be understood.
#: Fieldwork Manager keeps its own copy of this table (it ships as a separate program and must
#: not import Plumbline); a test pins the two copies equal so they cannot drift apart.
CARLSON_ENTITY_TYPES = {"0": "Point", "1": "Line", "2": "2D Polyline", "3": "3D Polyline"}

#: A code table's entity type -> (Plumbline kind, is it a breakline).  An entity type that is
#: not listed is treated as a point: a stray line through the wrong points damages a surface,
#: a stray point does not.
ENTITY_TO_KIND = {
    "point": ("point", False),
    "line": ("line", True),
    "2d polyline": ("line", True),
    "polyline": ("line", True),
    "3d polyline": ("line", True),
    "arc": ("line", True),
    "circle": ("polygon", True),
    "polygon": ("polygon", True),
    "text": ("point", False),
    "block": ("point", False),
}


def kind_for_entity(entity: str) -> tuple[str, bool, bool]:
    """(kind, breakline, recognised) for the "Entity Type" cell of a code table.

    Recognised means "this file said something we understand", which is what the conversion
    reports back to the user - an office's custom entity type should be heard, not silently
    rounded off to a point.
    """
    word = (entity or "").strip().casefold()
    word = CARLSON_ENTITY_TYPES.get(word, word).casefold()
    if word in ENTITY_TO_KIND:
        kind, breakline = ENTITY_TO_KIND[word]
        return kind, breakline, True
    return "point", False, False


@dataclass
class FeatureCode:
    code: str
    name: str = ""
    kind: str = "point"                  # point | line | polygon
    layer: str = ""
    symbol: str = "cross"
    color: tuple = (255, 255, 255)
    linetype: str = "CONTINUOUS"
    breakline: bool = False              # line strings become TIN breaklines
    ground: bool = True                  # points contribute to the ground surface

    def to_dict(self):
        return {"code": self.code, "name": self.name, "kind": self.kind, "layer": self.layer,
                "symbol": self.symbol, "color": list(self.color), "linetype": self.linetype,
                "breakline": self.breakline, "ground": self.ground}

    @classmethod
    def from_dict(cls, d):
        return cls(d["code"].upper(), d.get("name", ""), d.get("kind", "point"), d.get("layer", ""),
                   d.get("symbol", "cross"), tuple(d.get("color", (255, 255, 255))),
                   d.get("linetype", "CONTINUOUS"), bool(d.get("breakline", False)),
                   bool(d.get("ground", True)))


class _CodeDict(dict):
    """Feature-code mapping that caches its casefolded key set until mutated."""

    __slots__ = ("_casefolded", "_sorted_json_cache")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._casefolded: frozenset[str] | None = None
        self._sorted_json_cache: tuple[tuple, str] | None = None

    def __getstate__(self):
        return None

    def __setstate__(self, _state):
        self._casefolded = None
        self._sorted_json_cache = None

    def _invalidate(self):
        self._casefolded = None
        self._sorted_json_cache = None

    def __setitem__(self, key, value):
        self._invalidate()
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._invalidate()
        super().__delitem__(key)

    def pop(self, *args):
        self._invalidate()
        return super().pop(*args)

    def popitem(self):
        self._invalidate()
        return super().popitem()

    def clear(self):
        self._invalidate()
        super().clear()

    def update(self, *args, **kwargs):
        self._invalidate()
        super().update(*args, **kwargs)

    def setdefault(self, key, default=None):
        self._invalidate()
        return super().setdefault(key, default)

    def casefolded_set(self) -> frozenset[str]:
        cached = getattr(self, "_casefolded", None)
        if cached is None:
            cached = frozenset(str(code).casefold() for code in self if str(code))
            self._casefolded = cached
        return cached


class FeatureCodeTable:
    def __init__(self, codes=None):
        self.codes: dict[str, FeatureCode] = _CodeDict()
        for c in codes or []:
            self.codes[c.code.upper()] = c

    def casefolded_set(self) -> frozenset[str]:
        if hasattr(self.codes, "casefolded_set"):
            return self.codes.casefolded_set()
        return frozenset(str(code).casefold() for code in self.codes if str(code))

    def sorted_codes_json(self) -> str:
        import json
        snap = tuple(
            (k, v.code, v.name, v.kind, v.layer, v.symbol, v.color, v.linetype, v.breakline, v.ground)
            for k, v in self.codes.items()
        )
        cache = getattr(self.codes, "_sorted_json_cache", None)
        if cache is not None and cache[0] == snap:
            return cache[1]
        codes = sorted(
            self.to_list(),
            key=lambda item: (str(item.get("code", "")).casefold(), str(item.get("code", ""))),
        )
        encoded = json.dumps(codes, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=str, allow_nan=True)
        if hasattr(self.codes, "_sorted_json_cache"):
            self.codes._sorted_json_cache = (snap, encoded)
        return encoded

    def get(self, code: str) -> FeatureCode | None:
        return self.codes.get((code or "").upper())

    def resolve(self, desc: str, commands=None) -> FeatureCode | None:
        """Resolve a code using the office table and active Field Book command syntax."""
        parsed = parse_description(desc, commands=commands, known_codes=self.codes)
        return self.codes.get(parsed.code) if parsed.code else None

    def resolve_parts(self, desc: str, commands=None) -> tuple[FeatureCode | None, str]:
        """Return the feature code and string identifier that own a point's linework."""
        parsed = parse_description(desc, commands=commands, known_codes=self.codes)
        return (self.codes.get(parsed.code) if parsed.code else None), parsed.string

    def add(self, fc: FeatureCode):
        self.codes[fc.code.upper()] = fc

    def remove(self, code: str):
        self.codes.pop(code.upper(), None)

    def items(self):
        return self.codes.items()

    def keys(self):
        return self.codes.keys()

    def values(self):
        return self.codes.values()

    def __contains__(self, code: str):
        return (code or "").upper() in self.codes

    def __getitem__(self, code: str):
        return self.codes[(code or "").upper()]

    def __iter__(self):
        return iter(self.codes.values())

    def __len__(self):
        return len(self.codes)

    def to_list(self):
        return [c.to_dict() for c in self.codes.values()]

    @classmethod
    def from_list(cls, lst):
        return cls([FeatureCode.from_dict(d) for d in lst])


@dataclass
class ParsedDesc:
    code: str = ""
    string: str = ""
    flags: frozenset = frozenset()
    note: str = ""


_EMPTY_CODES: frozenset[str] = frozenset()


@functools.lru_cache(maxsize=65536)
def _parse_description_cached(desc: str, command_tokens: tuple[str, ...], known_codes: frozenset[str]) -> ParsedDesc:
    commands = dict(zip(COMMAND_MEANINGS, command_tokens))
    token_meanings = command_meanings(commands)
    known = known_codes

    description_part = desc
    description_separator = commands.get("description", "")
    description_index = find_separator(description_part, description_separator)
    if description_index >= 0:
        description_part = description_part[:description_index]
    multicode_separator = commands.get("multicode", "")
    if multicode_separator:
        description_part = split_at_separator(description_part, multicode_separator, maxsplit=1)[0]

    tokens = description_part.strip().split()
    if not tokens:
        return ParsedDesc()

    line_meanings = set(COMMAND_MEANINGS[:5])
    line_commands = sorted(
        (token for token, meaning in token_meanings.items() if meaning in line_meanings),
        key=len, reverse=True)

    def is_code_prefix(value: str) -> bool:
        low = value.casefold()
        if low in known:
            return True
        match = _TOKEN.match(value)
        return bool(match and match.group(1).rstrip("-_").casefold() in known)

    def split_compact(value: str, depth=0) -> list[str]:
        low = value.casefold()
        if low in known or depth >= 8:
            return [value]
        for command in line_commands:
            if low.endswith(command) and len(value) > len(command):
                prefix = value[:-len(command)]
                prefix_tokens = split_compact(prefix, depth + 1)
                if prefix_tokens and is_code_prefix(prefix_tokens[0]):
                    return [*prefix_tokens, value[-len(command):]]
        return [value]

    # A spacing preference may join a line command directly to its code. Split only when
    # the leading portion is a code in this table; an exact office code always wins.
    if known:
        tokens = [*split_compact(tokens[0]), *tokens[1:]]

    head = tokens[0]
    exact_code = head.casefold() in known
    if exact_code:
        code, string = head.upper(), ""
    else:
        match = _TOKEN.match(head)
        if not match:
            code, string = head.upper(), ""
        else:
            code = match.group(1).rstrip("-_").upper()
            string = match.group(2) or ""

    rest = tokens[1:]
    if not string and rest and rest[0].isdigit():
        string = rest.pop(0)
    flags, notes = set(), []
    for token in rest:
        upper = token.upper()
        meaning = token_meanings.get(token.casefold(), "")
        if meaning in line_meanings or upper in BEGIN_FLAGS or upper in END_FLAGS or upper in CLOSE_FLAGS:
            flags.add(upper)
        else:
            notes.append(token)
    return ParsedDesc(code, string, frozenset(flags), " ".join(notes))


def parse_description(desc: str, commands=None, known_codes=None) -> ParsedDesc:
    """Parse a field description using the selected Field Book's command meanings.

    ``known_codes`` resolves the Carlson ambiguity between digits that are part of an office code
    and digits used as a line-string number. It also permits the no-space setting to emit a compact
    code followed by its active Start Line token.
    """
    semantic_tokens = command_map(commands)
    command_values = tuple(semantic_tokens[meaning] for meaning in COMMAND_MEANINGS)
    if not known_codes:
        normalized_codes = _EMPTY_CODES
    elif hasattr(known_codes, "casefolded_set"):
        normalized_codes = known_codes.casefolded_set()
    elif isinstance(known_codes, frozenset):
        normalized_codes = known_codes
    else:
        normalized_codes = frozenset(str(code).casefold() for code in known_codes if str(code))
    return _parse_description_cached(str(desc or ""), command_values, normalized_codes)

def _fc(code, name, kind, layer, symbol="cross", color=(255, 255, 255), linetype="CONTINUOUS",
        breakline=False, ground=True):
    return FeatureCode(code, name, kind, layer, symbol, color, linetype, breakline, ground)


def default_codes() -> FeatureCodeTable:
    """A generic topo/planimetric starter library - edit freely."""
    L = [
        # ground & control
        _fc("GS", "Ground shot", "point", "TOPO-GROUND", "cross", (200, 200, 200)),
        _fc("SPOT", "Spot elevation", "point", "TOPO-SPOT", "cross", (200, 200, 200)),
        _fc("BM", "Benchmark", "point", "CONTROL-BM", "triangle", (255, 90, 90), ground=False),
        _fc("CP", "Control point", "point", "CONTROL-CP", "triangle", (255, 90, 90), ground=False),
        # pavement & curb
        _fc("EP", "Edge of pavement", "line", "ROAD-EP", color=(255, 255, 0), breakline=True),
        _fc("TC", "Top of curb", "line", "ROAD-CURB-TOP", color=(255, 190, 0), breakline=True),
        _fc("TOC", "Top of curb", "line", "ROAD-CURB-TOP", color=(255, 190, 0), breakline=True),
        _fc("BC", "Back of curb", "line", "ROAD-CURB-BACK", color=(255, 150, 0), breakline=True),
        _fc("FL", "Flowline / gutter", "line", "ROAD-FL", color=(0, 200, 255), breakline=True),
        _fc("CL", "Centerline", "line", "ROAD-CL", color=(255, 0, 255), linetype="CENTER"),
        _fc("EG", "Edge of gravel", "line", "ROAD-GRAVEL", color=(200, 160, 100), linetype="DASHED", breakline=True),
        _fc("SW", "Sidewalk edge", "line", "ROAD-SIDEWALK", color=(190, 190, 190), breakline=True),
        _fc("DW", "Driveway edge", "line", "ROAD-DRIVE", color=(170, 170, 255), breakline=True),
        # structures & site
        _fc("BLDG", "Building", "polygon", "STRUCT-BLDG", color=(255, 128, 0), ground=False),
        _fc("FFE", "Finished floor elevation", "point", "STRUCT-FFE", "square", (255, 128, 0), ground=False),
        _fc("WALL", "Wall", "line", "STRUCT-WALL", color=(220, 120, 60), ground=False),
        _fc("RW", "Retaining wall", "line", "STRUCT-RW", color=(220, 120, 60), breakline=True),
        _fc("FNC", "Fence", "line", "SITE-FENCE", color=(140, 255, 140), linetype="DASHDOT", ground=False),
        _fc("SIGN", "Sign", "point", "SITE-SIGN", "square", (255, 255, 120), ground=False),
        # drainage & water
        _fc("CRK", "Creek centerline", "line", "HYDRO-CREEK", color=(0, 140, 255), breakline=True),
        _fc("TOB", "Top of bank", "line", "HYDRO-BANK-TOP", color=(120, 200, 120), breakline=True),
        _fc("BOB", "Bottom of bank", "line", "HYDRO-BANK-BOT", color=(60, 170, 220), breakline=True),
        _fc("WE", "Water edge", "line", "HYDRO-WATEREDGE", color=(80, 180, 255), breakline=True),
        _fc("SWL", "Swale / ditch flowline", "line", "HYDRO-SWALE", color=(0, 220, 200), breakline=True),
        _fc("TOS", "Top of slope", "line", "GRADE-TOS", color=(160, 255, 80), breakline=True),
        _fc("TOE", "Toe of slope", "line", "GRADE-TOE", color=(255, 160, 80), breakline=True),
        # utilities
        _fc("MH", "Manhole", "point", "UTIL-MH", "manhole", (0, 255, 255), ground=False),
        _fc("SSMH", "Sanitary sewer manhole", "point", "UTIL-SS", "manhole", (0, 220, 0), ground=False),
        _fc("SDMH", "Storm drain manhole", "point", "UTIL-SD", "manhole", (0, 180, 255), ground=False),
        _fc("CI", "Curb inlet", "point", "UTIL-SD", "square", (0, 180, 255), ground=False),
        _fc("WV", "Water valve", "point", "UTIL-WATER", "valve", (80, 140, 255), ground=False),
        _fc("FH", "Fire hydrant", "point", "UTIL-WATER", "hydrant", (255, 70, 70), ground=False),
        _fc("WM", "Water meter", "point", "UTIL-WATER", "square", (80, 140, 255), ground=False),
        _fc("PP", "Power pole", "point", "UTIL-POWER", "pole", (255, 100, 255), ground=False),
        _fc("LP", "Light pole", "point", "UTIL-POWER", "pole", (255, 255, 120), ground=False),
        _fc("GUY", "Guy anchor", "point", "UTIL-POWER", "x", (255, 100, 255), ground=False),
        _fc("OHE", "Overhead electric", "line", "UTIL-OHE", color=(255, 100, 255), linetype="DASHDOT", ground=False),
        # vegetation
        _fc("TREE", "Tree", "point", "VEG-TREE", "tree", (40, 200, 60), ground=False),
        _fc("BRUSH", "Brush line", "line", "VEG-BRUSH", color=(40, 160, 60), linetype="DASHED", ground=False),
        # boundaries
        _fc("PL", "Property line", "line", "BNDY-PL", color=(255, 255, 255), linetype="PHANTOM", ground=False),
        _fc("ROW", "Right of way", "line", "BNDY-ROW", color=(255, 120, 120), linetype="PHANTOM", ground=False),
        _fc("EASE", "Easement", "line", "BNDY-EASE", color=(180, 120, 255), linetype="HIDDEN", ground=False),
    ]
    return FeatureCodeTable(L)


# ----------------------------------------------------------------------------- linework builder
@dataclass
class LineString:
    code: str
    string: str
    ids: list = field(default_factory=list)       # point ids in order
    closed: bool = False


def build_linework(points, table: FeatureCodeTable, order: str = "file", commands=None) -> list[LineString]:
    """Group coded points into strings using the active Field Book's line-command meanings."""
    pts = list(points)
    if order == "number":
        def nk(point):
            try:
                return (0, float(point.number), point.id)
            except ValueError:
                return (1, 0.0, point.id)
        pts.sort(key=nk)
    else:
        pts.sort(key=lambda point: point.id)
    semantic_tokens = command_map(commands)
    open_: dict[tuple, LineString] = {}
    out: list[LineString] = []

    def flush(key, closed=False):
        line = open_.pop(key, None)
        if line and len(line.ids) >= 2:
            line.closed = line.closed or closed
            out.append(line)

    def has_meaning(parsed: ParsedDesc, meaning: str) -> bool:
        token = semantic_tokens.get(meaning, "")
        if token and any(flag.casefold() == token.casefold() for flag in parsed.flags):
            return True
        if meaning == "start_line":
            return bool(parsed.flags & BEGIN_FLAGS)
        if meaning == "end_line":
            return bool(parsed.flags & END_FLAGS)
        if meaning == "close":
            return bool(parsed.flags & CLOSE_FLAGS)
        return False

    desc_sep = semantic_tokens.get("description", "")
    multi_sep = semantic_tokens.get("multicode", "")
    multi_token = multi_sep.strip() if multi_sep else ""

    for point in pts:
        raw_desc = point.desc or ""
        if multi_token and multi_token in raw_desc:
            code_part = raw_desc
            if desc_sep:
                desc_idx = find_separator(code_part, desc_sep)
                if desc_idx >= 0:
                    code_part = code_part[:desc_idx]
            sections = [s.strip() for s in split_at_separator(code_part, multi_sep, maxsplit=0) if s.strip()]
            parsed_sections = [
                parse_description(sec, commands=semantic_tokens, known_codes=table.codes)
                for sec in sections
            ]
        else:
            parsed_sections = [parse_description(raw_desc, commands=semantic_tokens, known_codes=table.codes)]

        for parsed in parsed_sections:
            feature_code = table.codes.get(parsed.code) if parsed.code else None
            string_id = parsed.string
            if feature_code is None or feature_code.kind == "point":
                continue
            key = (feature_code.code, string_id)
            if has_meaning(parsed, "start_line"):
                flush(key)
            line = open_.get(key)
            if line is None:
                line = open_[key] = LineString(feature_code.code, string_id)
                line.closed = feature_code.kind == "polygon"
            line.ids.append(point.id)
            if has_meaning(parsed, "close"):
                flush(key, closed=True)
            elif has_meaning(parsed, "end_line"):
                flush(key)
    for key in list(open_):
        flush(key)
    return out


def point_style(desc: str, table: FeatureCodeTable, commands=None):
    """(layer, symbol, color) for a point using its active Field Book, or None if unknown."""
    fc = table.resolve(desc, commands=commands)
    if fc is None:
        return None
    return fc.layer or "POINTS", fc.symbol, fc.color
