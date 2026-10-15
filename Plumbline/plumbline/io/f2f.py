"""Field-to-Finish code tables -> Plumbline's own feature codes.

A *Field-to-Finish* (F2F) file is a surveying office's code standard in a spreadsheet: one row
per field code, saying what the code is called, which CAD layer it draws on, which symbol it puts
down, and whether it draws a point, a line or a polygon.  Carlson Software's is the one that
arrives in Texas, and it is a large table - the office standard in the samples folder is 1,717
codes over 216 layers, 249 columns wide.

Plumbline already holds the other half of it: *code -> layer + symbol + kind*.  Converting is
therefore a rename, not a translation, which is why it is worth doing here instead of asking
somebody to retype a code table:

    Code          ->  FeatureCode.code      (upper-cased - Plumbline's codes are upper case)
    Description   ->  FeatureCode.name
    Layer         ->  FeatureCode.layer
    Symbol        ->  FeatureCode.symbol    ("cross" when the file is silent)
    Entity Type   ->  FeatureCode.kind      Point -> point; Line / 2D|3D Polyline / Arc -> line;
                                            Circle / Polygon -> polygon.  Anything that draws a
                                            line also becomes a TIN breakline, because the
                                            office's linework is the office's breaklines.
    Category      ->  the layer, when the row has no layer of its own

Two things about the *shape* of these files are why this is not a five-line CSV reader:

* **The column positions are not the facts.**  Carlson's own export puts Code, Description,
  Symbol and Symbol Size first, then Layer and Entity Type in the **fifth and sixth** columns;
  a reader that takes the first six columns in order reads Symbol Size as the layer and Entity
  Type as something else, and every code silently lands on a number.  So the facts are found
  **by column name** first - the header row is full of them, "Layer", "Entity Type" - and
  Carlson's own positions are only the fallback for a file whose headers were renamed.
* **The category is a row, not a column.**  ``Category,Corners,,,,...`` sits above the group of
  codes it labels.  Reading it as a code puts a code called "Category" in the table; skipping it
  loses the only hint about what to do with the rows that carry no layer of their own.

The other half of the job is the user's: :func:`read` gives the rows, :func:`auto_map` says where
the facts were found and how, and the conversion dialog lets every column be pointed at by hand
(the headers may be ignored entirely) and every row be ticked or left out before anything in the
job is touched.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from ..core.featurecodes import FeatureCode, FeatureCodeTable, kind_for_entity

#: The facts a code table can carry, in the order the interface asks for them.
FACTS = ("code", "description", "symbol", "layer", "entity", "category")

FACT_LABELS = {"code": "Code", "description": "Description", "symbol": "Symbol",
               "layer": "Layer", "entity": "Entity type", "category": "Category"}

#: Carlson's own export: which column each fact sits in.  Note 3 - "Symbol Size" - is skipped
#: on purpose: it is the one column in the leading group that is not a fact about the code, and
#: it is exactly why reading columns 0..5 in order gets the layer wrong.
CARLSON = {"code": 0, "description": 1, "symbol": 2, "layer": 4, "entity": 5, "category": -1}

#: Header names a column may carry, lower-case.  Matched exactly, so "Distinct Pt Layer" and
#: "Symbol Layer" - which a Carlson export also has - do not stand in for "Layer".
COLUMN_NAMES = {
    "code": ("code", "feature code", "f2f code", "field code"),
    "description": ("description", "desc", "full name"),
    "symbol": ("symbol", "symbol name", "point symbol"),
    "layer": ("layer", "layer name"),
    "entity": ("entity type", "entity", "type"),
    "category": ("category", "group"),
}

#: Carlson's own name for the codes that come before the first ``Category,`` row (its reader
#: starts every file in a category called "Default", and two house codes - DEFAULT and MISC -
#: are always above the first category line).
DEFAULT_CATEGORY = "Default"

#: Layer to fall back on when a row has no layer of its own and its category is one of these.
#: Only the three categories that mean something to Plumbline; anything else becomes "V-<CAT>".
CATEGORY_LAYERS = {"corners": "V-PROP-CRNR", "default": "V-SITE-DEFAULT", "utilities": "V-UTIL"}


def _cell(row, i: int) -> str:
    """A cell by index, "" when the row is shorter than the table is wide."""
    if row is None or i is None or i < 0 or i >= len(row):
        return ""
    return (row[i] or "").strip()


@dataclass
class ColumnMap:
    """Which column of a file holds which fact.  ``-1`` means "this file has not got one"."""

    code: int = CARLSON["code"]
    description: int = CARLSON["description"]
    symbol: int = CARLSON["symbol"]
    layer: int = CARLSON["layer"]
    entity: int = CARLSON["entity"]
    category: int = CARLSON["category"]
    found_by: str = "Carlson's own column positions"

    def index(self, fact: str) -> int:
        return int(getattr(self, fact))

    def as_dict(self) -> dict:
        return {f: self.index(f) for f in FACTS}

    def describe(self, headers=None) -> str:
        """One line saying where the facts are - what the dialog shows under the file name."""
        bits = []
        for f in FACTS:
            i = self.index(f)
            if i < 0:
                continue
            name = ""
            if headers and 0 <= i < len(headers) and headers[i]:
                name = f" {headers[i]!r}"
            bits.append(f"{FACT_LABELS[f]} = column {i + 1}{name}")
        return f"Read by {self.found_by}: " + "; ".join(bits) + "."


@dataclass
class F2FTable:
    """A code table as it sits in the file: the rows, and the category each row was under."""

    path: Path
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    header_row: bool = True
    category_rows: int = 0
    blank_rows: int = 0

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=len(self.headers))

    def code_of(self, i: int, mapping: ColumnMap | None = None) -> str:
        m = mapping or auto_map(self)
        return _cell(self.rows[i], m.code).upper()

    def column_label(self, i: int) -> str:
        """How a column is named in the interface: ``C — Symbol``, or ``C — column 3``."""
        letter = ""
        n = i
        while True:
            letter = chr(ord("A") + n % 26) + letter
            n = n // 26 - 1
            if n < 0:
                break
        name = self.headers[i] if 0 <= i < len(self.headers) else ""
        return f"{letter} - {name.strip()}" if name and name.strip() else f"{letter} - column {i + 1}"


def read(path, header_row: bool | None = None) -> F2FTable:
    """Read a code table.  ``header_row=None`` decides by looking at the first row.

    Blank rows are dropped and ``Category,<name>`` rows are folded into the category of the rows
    under them rather than becoming codes of their own.  Source order is always kept.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        raise ValueError(f"{path.name} is empty.")
    if looks_like_a_point_file(raw[0]):
        raise ValueError(f"{path.name} looks like a point or job file (N, E, Z, Desc), not a code "
                         f"table - pick the office's Field-to-Finish table instead.")
    if header_row is None:
        header_row = _first_row_is_a_header(raw[0])
    headers = [c.strip() for c in raw[0]] if header_row else []
    body = raw[1:] if header_row else raw
    table = F2FTable(path=path, headers=headers, header_row=bool(header_row))
    category = ""
    for row in body:
        row = [c.strip() for c in row]
        if not any(row):
            table.blank_rows += 1
            continue
        if row[0].casefold() == "category":
            category = (row[1] if len(row) > 1 else "").strip()
            table.category_rows += 1
            continue
        table.rows.append(row)
        table.categories.append(category)
    if table.category_rows:
        table.categories = [c or DEFAULT_CATEGORY for c in table.categories]
    if not table.rows:
        raise ValueError(f"{path.name} holds no code rows - is it a code table "
                         f"(one field code per row), not a point or job file?")
    return table


def _first_row_is_a_header(row) -> bool:
    """Judge the first row: a header names its columns, a code row starts with a code."""
    cells = [c.strip().casefold() for c in row]
    if any(c in ("code", "description", "entity type") for c in cells):
        return True
    return False


def looks_like_a_point_file(headers) -> bool:
    """True when the header row is a *point* file's: it names coordinates and a description.

    Both of the shapes a crew's file arrives in are covered - ``PT,N,E,Z,Desc`` and
    ``Northing,Easting,Elevation,Description`` - and neither is a name a code table uses, so a
    mistaken pick is refused by name rather than turned into 400 codes called "1".
    """
    names = {h.strip().casefold() for h in headers or []}
    if {"n", "e"} <= names and ({"z", "desc"} & names):
        return True
    if {"northing", "easting"} <= names:
        return True
    return bool({"pt", "ptno", "point"} & names and {"desc", "description"} & names
                and not looks_like_carlson(headers))


def looks_like_carlson(headers) -> bool:
    """True when the header row carries the two names Carlson always writes."""
    names = {h.strip().casefold() for h in headers or []}
    return "code" in names and bool({"description", "entity type"} & names)


def auto_map(table: F2FTable) -> ColumnMap:
    """Where the facts are in this file: the header names first, Carlson's positions second.

    All five drawing facts have to be found by name for the name pass to be used at all.  A map
    half from names and half from positions is how a table ends up 216 layers deep in numbers.
    """
    if table.headers:
        found = {}
        low = [h.strip().casefold() for h in table.headers]
        for fact in FACTS:
            for name in COLUMN_NAMES[fact]:
                if name in low:
                    found[fact] = low.index(name)
                    break
        if all(f in found for f in ("code", "description", "symbol", "layer", "entity")):
            return ColumnMap(found_by="the header names",
                             **{f: found.get(f, -1) for f in FACTS})
    return ColumnMap(found_by="Carlson's own column positions")


def _copy(table: FeatureCodeTable) -> FeatureCodeTable:
    return FeatureCodeTable([FeatureCode.from_dict(c.to_dict()) for c in table])


def convert(table: F2FTable, mapping: ColumnMap | None = None, *, only=None,
            existing: FeatureCodeTable | None = None) -> tuple[FeatureCodeTable, dict]:
    """Build a feature code table from ``table``.

    ``only`` is a set of row indices to take (the dialog's ticks) - ``None`` takes every row.
    ``existing`` is the job's table when the user chose to merge; it is copied, never written
    through, so an undo of the conversion gets the table back exactly as it was.
    """
    m = mapping or auto_map(table)
    target = _copy(existing) if existing is not None else FeatureCodeTable()
    had = set(target.codes)
    written: set[str] = set()
    stats = {"rows": len(table.rows), "codes": 0, "points": 0, "lines": 0, "polygons": 0,
             "layers": set(), "symbols": set(), "categories": set(), "skipped": 0,
             "skipped_by_choice": 0, "duplicates": 0, "replaced": 0, "no_layer": 0,
             "unknown_entity_types": set()}
    for i, row in enumerate(table.rows):
        if only is not None and i not in only:
            stats["skipped_by_choice"] += 1
            continue
        code = _cell(row, m.code).upper()
        if not code:
            stats["skipped"] += 1
            continue
        category = _cell(row, m.category) or table.categories[i]
        entity = _cell(row, m.entity)
        kind, breakline, recognised = kind_for_entity(entity)
        if entity and not recognised:
            stats["unknown_entity_types"].add(entity)
        layer = _cell(row, m.layer) or CATEGORY_LAYERS.get(category.casefold(), "")
        if not layer and category:
            layer = f"V-{category.upper()}"
        symbol = _cell(row, m.symbol) or "cross"
        if code in written:
            stats["duplicates"] += 1                  # two rows in the *file* want the same code
        elif code in had:
            stats["replaced"] += 1                    # this file re-defines a code the job had
        if not layer:
            stats["no_layer"] += 1
        target.codes[code] = FeatureCode(code=code, name=_cell(row, m.description), kind=kind,
                                         layer=layer, symbol=symbol, breakline=breakline)
        written.add(code)
        stats["codes"] += 1
        stats["polygons" if kind == "polygon" else "lines" if kind == "line" else "points"] += 1
        if layer:
            stats["layers"].add(layer)
        if symbol:
            stats["symbols"].add(symbol)
        if category:
            stats["categories"].add(category)
    stats["kept"] = len(had - written)          # the job's codes this file does not mention
    for key in ("layers", "symbols", "categories", "unknown_entity_types"):
        stats[key] = sorted(stats[key])
    return target, stats


def convert_file(path, *, existing: FeatureCodeTable | None = None, only=None,
                 mapping: ColumnMap | None = None,
                 header_row: bool | None = None) -> tuple[FeatureCodeTable, dict]:
    """Read and convert in one step - what a caller wants when nobody is being asked anything."""
    table = read(path, header_row=header_row)
    return convert(table, mapping, only=only, existing=existing)


def select_by_code(table: F2FTable, codes, mapping: ColumnMap | None = None) -> set[int]:
    """The row indices whose code is in ``codes`` - a filter box's answer, as row ticks."""
    m = mapping or auto_map(table)
    wanted = {str(c).strip().upper() for c in codes}
    return {i for i in range(len(table.rows)) if _cell(table.rows[i], m.code).upper() in wanted}


def describe_stats(stats: dict, *, mention_kept: bool = True) -> str:
    """One line a user can read: what the conversion produced, and what it left out."""
    shapes = [f"{stats['points']:,} points", f"{stats['lines']:,} lines"]
    if stats["polygons"]:
        shapes.append(f"{stats['polygons']:,} polygons")
    parts = [f"{stats['codes']:,} codes", f"{' / '.join(shapes)}",
             f"{len(stats['layers']):,} layers"]
    if stats["symbols"]:
        parts.append(f"{len(stats['symbols']):,} symbols")
    line = " - ".join(parts) + "."
    if stats.get("kept") and mention_kept:
        line += f" {stats['kept']:,} code(s) already in the job are kept."
    if stats["skipped_by_choice"]:
        line += f" {stats['skipped_by_choice']:,} row(s) left unticked."
    if stats["skipped"]:
        line += f" {stats['skipped']:,} row(s) had no code."
    if stats["duplicates"]:
        line += f" {stats['duplicates']:,} row(s) repeated a code already read (the last one wins)."
    if stats.get("replaced"):
        line += f" {stats['replaced']:,} of them re-define a code the job already had."
    if stats["no_layer"]:
        line += f" {stats['no_layer']:,} code(s) named no layer."
    if stats["unknown_entity_types"]:
        line += (f" Entity types not recognised: {', '.join(stats['unknown_entity_types'])}"
                 f" - those codes are points.")
    return line


from ..core.fieldbook_syntax import COMMAND_LABELS as DEFAULT_COMMAND_LABELS
from ..core.fieldbook_syntax import DEFAULT_COMMAND_TOKENS

DEFAULT_COMMANDS = list(DEFAULT_COMMAND_TOKENS)
DEFAULT_COMMAND_LABELS = list(DEFAULT_COMMAND_LABELS)


def read_fwb_extra(path: Path | str) -> dict:
    """Read canonical command meanings/rules from the active book, including legacy metadata."""
    from ..fieldwork.io_carlson import read_fwb_extra as read_extra
    return read_extra(Path(path))


def write_fwb_extra(path: Path | str, rules=None, commands=None) -> bool:
    """Update an existing Field Book's metadata without losing its rows or unknown JSON keys."""
    from ..fieldwork.io_carlson import write_fwb_extra as write_extra
    return write_extra(Path(path), rules=rules, commands=commands)


def write_fwb(dest_path: Path | str, table: F2FTable, mapping: ColumnMap | None = None,
              only: set | None = None, commands: list | None = None,
              rules: list | None = None) -> Path:
    """Write a converted Field Book (.fwb) CSV containing all converted code rows
    and the trailing #EXTRA_JSON line with line commands and correction rules.
    """
    import json
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    m = mapping or auto_map(table)
    headers = ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"]
    rows = []
    for i, row in enumerate(table.rows):
        if only is not None and i not in only:
            continue
        code = _cell(row, m.code).upper()
        if not code:
            continue
        category = _cell(row, m.category) or (table.categories[i] if i < len(table.categories) else "") or "Default"
        entity = _cell(row, m.entity)
        kind, _break, _ = kind_for_entity(entity)
        entity_name = "Point" if kind == "point" else "3D Polyline" if kind == "line" else "Polygon" if kind == "polygon" else (entity or "Point")
        layer = _cell(row, m.layer) or CATEGORY_LAYERS.get(category.casefold(), "")
        if not layer and category:
            layer = f"V-{category.upper()}"
        symbol = _cell(row, m.symbol) or "CG08"
        rows.append([code, _cell(row, m.description), symbol, layer, entity_name, category])

    with open(dest, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)
        extras = {
            "commands": commands if commands is not None else list(DEFAULT_COMMANDS),
            "rules": rules if rules is not None else [],
        }
        fh.write(f"#EXTRA_JSON {json.dumps(extras, ensure_ascii=False)}\n")
    return dest
