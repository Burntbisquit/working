"""Semantic Field Book command and separator handling."""
from __future__ import annotations

from plumbline.core.fieldbook_syntax import (
    DEFAULT_COMMAND_MAP,
    command_map,
    command_token_validation_error,
    find_separator,
    normalize_fieldbook_separator_spacing,
    normalize_separator_spacing,
    separator_text,
    split_at_separator,
    trim_separator_edges,
)


def test_command_map_keeps_meanings_attached_to_tokens():
    assert command_map() == DEFAULT_COMMAND_MAP
    mapped = command_map({"Start Line": "BEGINLN", "Multi-code separator": "PLUS"})
    assert mapped["start_line"] == "BEGINLN"
    assert mapped["multicode"] == "PLUS"
    assert mapped["description"] == DEFAULT_COMMAND_MAP["description"]

    # A blank cell disables that meaning without shifting the rows that follow it.
    values = command_map(["BEGINLN", "", "ENDCURVE", "FINISH", "CLOSEFIG", "PLUS", "NOTE"])
    assert values["start_curve"] == ""
    assert values["end_curve"] == "ENDCURVE"
    assert values["end_line"] == "FINISH"
    assert values["multicode"] == "PLUS"


def test_command_tokens_must_be_assigned_single_and_case_insensitively_unique():
    assert command_token_validation_error(["", " ", ""]) == "Assign at least one semantic command token."
    assert command_token_validation_error(["Begin Line", "", ""]) == \
        "Each command token must be a single word or symbol."
    assert command_token_validation_error(["begin", "BEGIN"]) == \
        "Each semantic command token must be unique."
    assert command_token_validation_error(["+", "NOTE", ""]) is None


def test_alphanumeric_separators_match_only_as_whole_tokens():
    assert find_separator("EA NOTE oak", "NOTE") == 3
    assert find_separator("EA noteworthy oak", "NOTE") == -1
    assert split_at_separator("EA PLUS SW", "PLUS", maxsplit=0) == ["EA", "SW"]
    assert split_at_separator("SAND EA", "AND", maxsplit=0) == ["SAND EA"]
    assert normalize_separator_spacing("EA PLUS  SW", "PLUS", spaced=False) == "EAPLUSSW"
    assert separator_text("description", {"description": "NOTE"}, spaced=False) == "NOTE"


def test_spacing_only_normalizer_preserves_code_order_and_note_text(monkeypatch):
    from plumbline.core.settings import settings

    monkeypatch.setitem(settings()._data, "space_around_multicode_separator", True)
    monkeypatch.setitem(settings()._data, "space_around_description_separator", True)
    assert normalize_fieldbook_separator_spacing("MH/30rcp") == "MH / 30rcp"
    assert normalize_fieldbook_separator_spacing("EA  -  SW/30rcp") == "EA - SW / 30rcp"
    assert normalize_fieldbook_separator_spacing(
        "EA  plus  SW  note  Road", {"multicode": "PLUS", "description": "NOTE"}
    ) == "EA plus SW note Road"

    monkeypatch.setitem(settings()._data, "space_around_multicode_separator", False)
    monkeypatch.setitem(settings()._data, "space_around_description_separator", False)
    assert normalize_fieldbook_separator_spacing("EA  -  SW / 30rcp") == "EA-SW/30rcp"


def test_separator_edge_trimming_is_whole_token_and_preserves_interior_text():
    assert trim_separator_edges("  NOTE  oak  /  ", ["NOTE", "/"]) == "oak"
    assert trim_separator_edges("noteworthy / sidewalk", ["NOTE", "/"]) == "noteworthy / sidewalk"
    assert trim_separator_edges("PLUS  Oak  tree  PLUS", "PLUS") == "Oak  tree"
    assert trim_separator_edges(" / NOTE / ", ["NOTE", "/"]) == ""


def test_common_conversion_rule_flags_and_preserves_a_valid_multi_code_description(tmp_path):
    from plumbline.fieldwork.bridge import check_project
    from plumbline.fieldwork.clean import _autocorrect_desc
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.fieldwork.parse import parse_desc_field
    from plumbline.core.project import Project
    from plumbline.io import f2f

    commands = {"multicode": "PLUS", "description": "NOTE"}
    rules = [["EA PLUS SW", "EA"], ["EA PLUS MYSTERY", "EA"], ["EA", "SW"]]
    book = tmp_path / "office.fwb"
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["EA", "Asphalt", "CG08", "PAVEMENT", "Point", "Surface"],
         ["SW", "Sidewalk", "CG08", "SIDEWALK", "Point", "Surface"]],
        commands=commands, rules=rules,
    )

    parsed = parse_desc_field("EA PLUS SW", {"ea", "sw"}, fieldbook_path=book)
    assert "CommonConversionError" in parsed["flags"]
    assert [item["base"] for item in parsed["code_classified"]
            if item.get("status") in ("exact", "line_instance")] == ["ea", "sw"]
    assert "collapse 2 valid Field Book codes into one" in parsed["flag_detail"]
    assert _autocorrect_desc("EA PLUS SW", {"ea", "sw"}, fieldbook_path=book) is None

    # The same destructive conversion must be caught when free-text follows NOTE.
    with_note = parse_desc_field("EA PLUS SW NOTE sidewalk", {"ea", "sw"}, fieldbook_path=book)
    assert with_note["raw"] == "EA PLUS SW NOTE sidewalk"
    assert "CommonConversionError" in with_note["flags"]
    assert [item["base"] for item in with_note["code_classified"]
            if item.get("status") in ("exact", "line_instance")] == ["ea", "sw"]

    # A Field Book Common Error is still reported when the source includes an unknown token.
    unknown_rule = parse_desc_field("EA PLUS MYSTERY", {"ea", "sw"}, fieldbook_path=book)
    assert unknown_rule["raw"] == "EA PLUS MYSTERY"
    assert "CommonConversionError" in unknown_rule["flags"]
    assert "UnknownCode:MYSTERY" in unknown_rule["flags"]
    assert unknown_rule["autofix_suggestion"] == "EA"

    # Matching a valid code is also a Common Error when the active Field Book says so.
    valid_rule = parse_desc_field("EA ST", {"ea", "sw"}, fieldbook_path=book)
    assert valid_rule["raw"] == "EA ST"
    assert "CommonConversionError" in valid_rule["flags"]
    assert valid_rule["autofix_suggestion"] == "SW ST"

    # A correction rule must not rewrite a token that occurs only in free text.
    note_only = parse_desc_field("EA NOTE SIDEWALK", {"ea"}, commands=commands,
                                 rules=[["SIDEWALK", "SW"]])
    assert note_only["raw"] == "EA NOTE SIDEWALK"

    project = Project("Conversion QA")
    project.settings["fieldbook_file"] = str(book)
    project.codes, _stats = f2f.convert(f2f.read(book))
    project.add_point(100, 200, 5, number="5101", desc="EA PLUS SW NOTE roadway")
    project.add_point(101, 201, 5, number="5102", desc="EA PLUS MYSTERY")
    project.add_point(102, 202, 5, number="5103", desc="EA")
    report = check_project(project, f2f={"ea", "sw"}, fieldbook_path=str(book))
    findings = report["findings"]
    finding = next(f for f in findings if f.get("flag") == "CommonConversionError")
    unknown = next(f for f in findings if f.get("flag") == "UnknownCode")
    assert finding["check"] == "Common error"
    assert finding["level"] == "warn"
    assert [f.get("flag") for f in findings].index("CommonConversionError") < \
        [f.get("flag") for f in findings].index("UnknownCode")
    assert report["ids"][unknown["rows"][0]] == 2


def test_parser_reads_semantic_separators_from_the_active_fieldbook(tmp_path):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.fieldwork.parse import parse_desc_field

    commands = {
        "start_line": "BEGINLN",
        "start_curve": "BC",
        "end_curve": "EC",
        "end_line": "FINISH",
        "close": "CLOSEFIG",
        "multicode": "PLUS",
        "description": "NOTE",
    }
    book = tmp_path / "office.fwb"
    assert write_fwb_file(book, ["Code", "Description"], [["EA", "Asphalt"], ["SW", "Sidewalk"]],
                          commands=commands)
    parsed = parse_desc_field("EA BEGINLN PLUS SW NOTE roadway", {"ea", "sw"},
                              fieldbook_path=book)
    assert parsed["code_raw_tokens"] == ["EA", "BEGINLN", "SW"]
    assert parsed["code_part"] == "EA BEGINLN PLUS SW"
    assert parsed["free_desc"] == "roadway"
    assert not parsed["flags"]
