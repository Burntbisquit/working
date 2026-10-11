"""Item 14, second half: the linework check and its three tools, in both windows.

A point check reads one description at a time.  A *line* is a sequence, and the two ends of one
mistake usually sit on two different points - so the issue is invisible to the description check
and the fix routinely clears something it was not aimed at.  These tests pin:

* the four ways a line can be incomplete, and that a finished line is not one of them;
* that a proposal is **placed** where the command order wants it (ST -> PC -> PT -> END/X), never
  appended, and that no proposal is invented when the description is not a single code;
* that a fix is **validated against the line** afterwards, and the answer names the second issue
  it cleared - and says so plainly when it cleared nothing;
* that a decision (Ignore) is written into the check report and read back, so a re-run does not
  reopen it;
* that the same reader serves the field window's Line Repair tab and the drawing window's Check
  Fieldwork dock, so the two cannot disagree about where a line ends;
* that the drawing side hears about it at all ("the field-side flags on the drawing side").

Qt-free where it can be: the engine tests import nothing from Qt, and the two window tests use the
same offscreen fixtures the other UI tests use.
"""
from __future__ import annotations

import pytest

from plumbline.fieldwork import linecheck as LC

from test_ui import app, auto, pump, win                              # noqa: E402  (the UI fixtures)

F2F = {"toc", "ec", "gs"}


def rows(*descs, first_oid=1):
    """Working rows (OID, Pt#, N, E, Z, Desc, Parent, Source) in file order."""
    return [[str(first_oid + i), str(first_oid + i), f"{1000.0 + i:.2f}", f"{2000.0 + i:.2f}",
             "500.00", d, "", ""]
            for i, d in enumerate(descs)]


def kinds(issues):
    return sorted(e["issue_type"] for e in issues)


# ------------------------------------------------------------------ the four issues
def test_each_way_a_line_can_be_incomplete_is_named():
    assert kinds(LC.detect_line_errors(rows("TOC PC"), f2f_set=F2F)) == \
        ["Missing END", "Missing PT", "Missing ST"]
    assert "Missing END" in kinds(LC.detect_line_errors(rows("TOC ST", "TOC PC"), f2f_set=F2F))
    assert "Missing PC" in kinds(LC.detect_line_errors(rows("TOC ST", "TOC PT", "TOC END"), f2f_set=F2F))
    assert "Missing PT" in kinds(LC.detect_line_errors(rows("TOC ST", "TOC PC", "TOC END"), f2f_set=F2F))


def test_a_finished_line_is_not_an_issue_not_even_two_of_them_in_a_row():
    assert LC.detect_line_errors(rows("TOC ST", "TOC PC", "TOC PT", "TOC END"), f2f_set=F2F) == []
    assert LC.detect_line_errors(rows("TOC ST", "TOC PC", "TOC PT", "TOC X"), f2f_set=F2F) == []
    # reuse after END is allowed, and a curve inside the second segment is fine too
    assert LC.detect_line_errors(
        rows("TOC ST", "TOC END", "TOC ST", "TOC PC", "TOC PT", "TOC END"), f2f_set=F2F) == []


def test_a_code_that_is_never_a_line_is_left_to_the_description_check():
    """Only a code that ever carries a line command is a line."""
    assert LC.detect_line_errors(rows("GS", "GS", "TOC ST", "TOC END"), f2f_set=F2F) == []


def test_multicode_groups_keep_commands_with_their_code_and_ignore_singletons():
    active_codes = {"eg", "ec"}

    # The valid pair EG PC -> ST is reported; the separate EC1 END singleton is not.
    same_row = LC.detect_line_errors(rows("EG PC ST - EC1 END"), f2f_set=active_codes)
    same_row_order = [issue for issue in same_row if issue["issue_type"] == "Line Order"]
    assert [(issue["line_id"], issue["oid"]) for issue in same_row_order] == [("eg", "1")]
    assert "Start Curve appears before Start Line" in same_row_order[0]["detail"]

    # These descriptions each have only one command in each code section. They must not be
    # compared across EG/EC1 or EC/EG1, and repeated code names in separate sections stay apart.
    for description in (
        "EG ST - EC1 END",
        "EG PC - EC1 ST",
        "EG PC - EG ST",
    ):
        issues = LC.detect_line_errors(rows(description), f2f_set=active_codes)
        assert not any(issue["issue_type"] == "Line Order" for issue in issues), description

    # The same line code may continue across points; only the specified pair inversions are
    # surfaced, and a different line code's commands never complete that pair.
    continued = LC.detect_line_errors(rows("EG PC", "EG ST"), f2f_set=active_codes)
    continued_order = [issue for issue in continued if issue["issue_type"] == "Line Order"]
    assert [(issue["line_id"], issue["oid"]) for issue in continued_order] == [("eg", "2")]

    cross_code = LC.detect_line_errors(rows("EG PC - EC1 ST"), f2f_set=active_codes)
    assert not any(issue["issue_type"] == "Line Order" for issue in cross_code)


def test_custom_fieldbook_meanings_and_alphanumeric_separators_are_used():
    commands = {
        "start_line": "BEGINLN",
        "start_curve": "BC",
        "end_curve": "EC",
        "end_line": "FINISH",
        "close": "CLOSEFIG",
        "multicode": "PLUS",
        "description": "NOTE",
    }
    assert LC.detect_line_errors(
        rows("EA BEGINLN", "EA", "EA FINISH"), f2f_set={"ea"}, commands=commands) == []
    issues = LC.detect_line_errors(
        rows("EA BEGINLN", "EA"), f2f_set={"ea"}, commands=commands)
    assert "Missing END" in kinds(issues)

    from plumbline.fieldwork.parse import parse_desc_field
    parsed = parse_desc_field("EA BEGINLN PLUS SW NOTE roadway", {"ea", "sw"}, commands=commands)
    assert parsed["code_part"] == "EA BEGINLN PLUS SW"
    assert parsed["free_desc"] == "roadway"
    assert parsed["has_multicode_separator"]
    assert parsed["has_description_separator"]
    # Alphanumeric separators are whole tokens: NOTE inside a note is not a delimiter.
    parsed = parse_desc_field("EA NOTE noteworthy", {"ea"}, commands=commands)
    assert parsed["free_desc"] == "noteworthy"


def test_a_second_segment_starting_before_the_first_one_ends_is_a_missing_end_not_line_order():
    issues = LC.detect_line_errors(rows("TOC ST", "TOC ST", "TOC END"), f2f_set=F2F)
    assert "Missing END" in kinds(issues)
    assert "Line Order" not in kinds(issues)
    assert any(e["oid"] == "1" for e in issues), "the fix belongs on the point that should have ended"


def test_two_lines_are_judged_one_at_a_time():
    """A finished EP line must not be made unfinished by an unfinished TOC one."""
    issues = LC.detect_line_errors(rows("EP ST", "TOC ST", "EP END", "TOC END"),
                                   f2f_set={"ep", "toc"})
    assert issues == []


def test_only_the_three_requested_pair_orders_are_checked_across_points():
    # Start Curve before Start Line, including when the same line code continues across points.
    start_before_line = LC.detect_line_errors(rows("TOC PC", "TOC ST"), f2f_set=F2F)
    start_orders = [issue for issue in start_before_line if issue["issue_type"] == "Line Order"]
    assert [(issue["oid"], "Start Curve appears before Start Line" in issue["detail"])
            for issue in start_orders] == [("2", True)]

    # End Line/Close before End Curve remain errors even when commands are on separate points.
    for terminator, expected_reason in (
        ("END", "End Line appears before End Curve"),
        ("X", "Close appears before End Curve"),
    ):
        ended_before_curve = LC.detect_line_errors(
            rows("TOC ST", "TOC PC", f"TOC {terminator}", "TOC PT"), f2f_set=F2F)
        order_issues = [issue for issue in ended_before_curve if issue["issue_type"] == "Line Order"]
        assert len(order_issues) == 1, terminator
        assert order_issues[0]["oid"] == "3", terminator
        assert expected_reason in order_issues[0]["detail"]

    # The point-level pair is also caught without using a hard-coded token spelling.
    same_point = LC.detect_line_errors(rows("TOC PC ST"), f2f_set=F2F)
    assert any(issue["issue_type"] == "Line Order" and issue["oid"] == "1"
               for issue in same_point)

    # End Curve before Start Line, repeated starts, and an unmatched singleton are outside the
    # three pair checks; completeness remains covered by the Missing-* findings.
    for descriptions in (
        ("TOC PT ST",),
        ("TOC ST", "TOC ST", "TOC END"),
        ("TOC PC",),
        ("TOC END",),
        ("TOC ST", "TOC PT", "TOC PC", "TOC END"),
        ("TOC ST", "TOC PC", "TOC PC", "TOC PT", "TOC END"),
    ):
        issues = LC.detect_line_errors(rows(*descriptions), f2f_set=F2F)
        assert not any(issue["issue_type"] == "Line Order" for issue in issues), descriptions

    # A line may carry several properly paired curves; the narrower check leaves them alone.
    assert LC.detect_line_errors(
        rows("TOC ST", "TOC PC", "TOC PT", "TOC PC", "TOC PT", "TOC END"),
        f2f_set=F2F) == []


def test_line_order_fix_validation_checks_the_whole_sequence_after_a_fix_moves_the_error():
    working = rows("TOC ST", "TOC PC", "TOC END PT")
    result = LC.validate_fix(working, "3", "TOC PT END", f2f_set=F2F)
    assert result["ok"], result["note"]
    assert any(issue["issue_type"] == "Line Order" and issue["oid"] == "3"
               for issue in result["cleared"])


# ------------------------------------------------------------------ the proposal
def test_a_proposal_is_placed_where_the_command_order_wants_it():
    assert LC.propose_fix("Missing ST", "TOC PC") == "TOC ST PC"
    assert LC.propose_fix("Missing PC", "TOC ST PT END") == "TOC ST PC PT END"
    assert LC.propose_fix("Missing PT", "TOC ST PC END") == "TOC ST PC PT END"
    assert LC.propose_fix("Missing END", "TOC ST PC PT") == "TOC ST PC PT END"


def test_no_proposal_is_invented_when_there_is_nothing_safe_to_say():
    assert LC.propose_fix("Missing ST", "TOC ST PC") == ""          # already there
    assert LC.propose_fix("Missing PT", "") == ""                   # nothing to work from
    assert LC.propose_fix("Missing ST", "TOC ST - EC PC") == ""     # two codes: a judgement call
    assert LC.propose_fix("Missing ST", "PC") == ""                 # starts with a command


def test_the_detector_hands_each_issue_its_own_proposal():
    issues = LC.detect_line_errors(rows("TOC ST", "TOC PC"), f2f_set=F2F)
    assert [e["oid"] for e in issues] == ["2", "2"]
    assert all(e["proposal"].startswith("TOC PC") for e in issues)
    assert all(e["line_id"] == "toc" and e["gid"] for e in issues)


# ------------------------------------------------------------------ the validation
def test_a_fix_is_read_back_from_the_line_and_names_the_issue_it_also_cleared():
    """The field book's own note asked for this: 'also cleared ... on OID 46'."""
    working = rows("TOC ST", "TOC PC")
    res = LC.validate_fix(working, 2, "TOC PC PT END", f2f_set=F2F)
    assert res["ok"] and res["cleared"]
    assert "also cleared Missing PT" in res["note"] or "Missing PT" in res["note"]
    assert "TOC PC PT END" in res["note"]


def test_a_fix_that_leaves_the_issue_standing_says_so_instead_of_claiming_success():
    res = LC.validate_fix(rows("TOC ST", "TOC PC"), 2, "TOC PC", f2f_set=F2F)
    assert not res["ok"]
    assert "still has" in res["note"] and "Key-In" in res["note"]


def test_validating_an_oid_that_is_not_in_the_file_changes_nothing():
    res = LC.validate_fix(rows("TOC ST", "TOC PC"), 999, "TOC PC PT END", f2f_set=F2F)
    assert res["ok"] is False and "no point with oid 999" in res["note"].lower()


def test_a_fix_at_one_point_can_clear_an_issue_reported_on_another():
    """A line whose second point never got its ST: start it, and the point after it closes it.

    This is the case the field book's note described ("also cleared LineOrderError on OID 46"):
    the second half of the mistake is somebody else's row, and a tool that only reported its own
    row would leave it looking like work still to do.
    """
    working = rows("TOC ST", "EC END", "EC END")
    before = LC.detect_line_errors(working, f2f_set=F2F)
    assert kinds(before) == ["Missing END", "Missing ST", "Missing ST"], \
        "the crew never started the EC line, and never closed the TOC one"
    res = LC.validate_fix(working, "2", "EC ST END", f2f_set=F2F)
    assert res["ok"], res["note"]
    assert [(e["issue_type"], e["oid"]) for e in res["also_cleared"]] == [("Missing ST", "3")]
    assert "also cleared Missing ST at OID 3" in res["note"]


# ------------------------------------------------------------------ the decisions
def test_an_ignored_issue_is_written_to_the_report_and_read_back():
    issues = LC.detect_line_errors(rows("TOC ST", "TOC PC"), f2f_set=F2F)
    statuses = {("Missing END", "2"): {"status": "Ignored", "comments": "crew wrote it that way"}}
    report = LC.report_rows(issues, statuses)
    assert report and report[0][1] == "Line"
    assert any(r[7] == "Ignored" for r in report)
    back = LC.statuses_from_rows(report)
    assert back[("Missing END", "2")]["status"] == "Ignored"
    assert LC.statuses_from_rows(LC.report_rows(issues)) == {}, "an Open row is not a decision"


def test_the_report_carries_the_line_issues_beside_the_duplicates():
    from plumbline.fieldwork.io_carlson import build_unified_report_rows
    working = rows("TOC ST", "TOC PC")
    line_issues = LC.detect_line_errors(working, f2f_set=F2F)
    report = build_unified_report_rows(working, [[0, 1]], [], [], [], line_issues=line_issues)
    displays = [r[1] for r in report]
    assert displays.count("Duplicate") == 2 and displays.count("Line") == len(line_issues)
    gids = [int(r[0]) for r in report]
    assert gids == sorted(gids), "group ids run in order"
    assert len(set(gids[-len(line_issues):])) == len(line_issues), "one id per line issue"
    line_row = next(r for r in report if r[1] == "Line")
    assert line_row[5] and line_row[6], "a line row carries its detail and its suggestion"


# ------------------------------------------------------------------ the drawing side
def test_the_project_side_reads_lines_with_the_same_reader(win, app):
    from plumbline.fieldwork import bridge as FB
    from plumbline.core.featurecodes import FeatureCode, FeatureCodeTable

    pr = win.state.project
    for p in list(pr.points.values()):
        del pr.points[p.id]
    pr.codes = FeatureCodeTable([FeatureCode("TOC", "Top of curb", "line", "ROAD-CURB-TOP")])
    for i, desc in enumerate(["TOC ST", "TOC PC"]):
        pr.add_point(2000.0 + i, 1000.0 + i, 500.0, number=f"80{i}", desc=desc)

    mine = FB.line_issues(pr, f2f={"toc"})
    theirs = LC.detect_line_errors(FB.working_rows_from_project(pr)[0], f2f_set={"toc"})
    assert [e["issue_type"] for e in mine] == [e["issue_type"] for e in theirs]
    assert mine and all(e["issue_type"] in LC.ISSUE_TYPES for e in mine)


def test_project_fieldwork_code_flags_ignore_deleted_points():
    from plumbline.core.project import Project
    from plumbline.fieldwork.bridge import check_project

    project = Project("Deleted code")
    active = project.add_point(0, 0, 0, number="1", desc="UNKNOWNCODE")
    deleted = project.add_point(10, 0, 0, number="2", desc="UNKNOWNCODE",
                                attrs={"qa_deleted": True})
    report = check_project(project, f2f={"toc"})
    unknown = next(finding for finding in report["findings"]
                   if finding.get("flag") == "UnknownCode")
    assert [report["ids"][i] for i in unknown["rows"]] == [active.id]
    assert all(str(oid) != str(deleted.id) for oid in report["flags"])


def test_empty_description_is_reported_even_without_a_vocabulary():
    from plumbline.core.project import Project
    from plumbline.fieldwork.bridge import check_project

    project = Project("Blank description")
    point = project.add_point(0, 0, 0, number="1", desc="")
    report = check_project(project)

    finding = next(finding for finding in report["findings"]
                   if finding.get("flag") == "EmptyDescription")
    assert finding["rows"] == [0]
    assert report["ids"][finding["rows"][0]] == point.id
    assert not report["code_checks"]  # blankness does not require a Field Book


def test_line_order_findings_are_errors_not_warnings():
    from plumbline.core.project import Project
    from plumbline.fieldwork.bridge import check_project

    project = Project("Line order severity")
    project.add_point(0, 0, 0, number="1", desc="TOC ST PC END PT")
    report = check_project(project, f2f=F2F)
    line_order_findings = [finding for finding in report["findings"]
                           if finding.get("flag") == "LineOrderError"]
    assert line_order_findings
    assert all(finding["level"] == "error" for finding in line_order_findings)
    assert any(finding.get("check") == "line: line order" for finding in line_order_findings)
    assert any(finding.get("check") == "Line command order" for finding in line_order_findings)

    repeated_curve_project = Project("Repeated curve start")
    for i, desc in enumerate(("TOC ST", "TOC PC", "TOC PC", "TOC PT", "TOC END")):
        repeated_curve_project.add_point(i, 0, 0, number=str(i + 1), desc=desc)
    repeated_report = check_project(repeated_curve_project, f2f=F2F)
    repeated_order = [finding for finding in repeated_report["findings"]
                      if finding.get("flag") == "LineOrderError"]
    assert not repeated_order, "a repeated Start Curve is outside the three supported pair checks"


def test_orphan_commands_are_errors_not_warnings():
    from plumbline.core.project import Project
    from plumbline.fieldwork.bridge import check_project

    project = Project("Orphan command severity")
    point = project.add_point(0, 0, 0, number="1", desc="ST")

    report = check_project(project, f2f=F2F)
    orphan_findings = [finding for finding in report["findings"]
                       if finding.get("flag") == "OrphanCommand"]

    assert orphan_findings
    assert all(finding["level"] == "error" for finding in orphan_findings)
    assert all(report["ids"][i] == point.id
               for finding in orphan_findings for i in finding.get("rows", []))


def test_real_world_sample_single_curve_command_is_not_a_line_order_error():
    from pathlib import Path

    from plumbline.core.project import Project
    from plumbline.fieldwork import bridge as FB
    from plumbline.fieldwork.parse import build_f2f_set_from_fieldbook

    sample_root = Path(__file__).resolve().parents[1] / "samples" / "Real World"
    working_file = sample_root / "Source" / "Edit points.fwk"
    book = sample_root / "Field Book" / "Real World.fwb"
    rows = FB.read_working_file(working_file)
    row = next(row for row in rows if str(row[FB.OID]).strip() == "33")
    northing, easting, elevation = FB.row_xyz(row)
    project = Project("Real World singleton line command")
    project.add_point(easting, northing, elevation, number=row[FB.PTNUM], desc=row[FB.DESC],
                      attrs={"fieldwork_oid": row[FB.OID]})

    code_set = build_f2f_set_from_fieldbook(book)
    assert "eg" in code_set, "the real sample Field Book defines EG"
    report = FB.check_project(project, f2f=code_set, fieldbook_path=str(book))

    assert not any(finding.get("flag") == "LineOrderError" for finding in report["findings"])
    assert not any(issue.get("issue_type") == "Line Order" for issue in report["line_issues"])


def test_project_check_keeps_line_order_visible_for_a_code_unknown_to_the_active_book(tmp_path):
    from plumbline.core.project import Project
    from plumbline.fieldwork.bridge import check_project

    book = tmp_path / "active.fwb"
    book.write_text(
        "Code,Description,Symbol,Layer,Entity Type,Category\n"
        "TOC,Top of curb,,ROAD,Line,Road\n",
        encoding="utf-8",
    )
    project = Project("Unknown line code")
    point = project.add_point(0, 0, 0, number="17", desc="ZZ PC ST")

    report = check_project(project, f2f={"toc"}, fieldbook_path=str(book))
    finding = next(finding for finding in report["findings"]
                   if finding.get("check") == "Line command order")

    assert finding["flag"] == "LineOrderError"
    assert finding["level"] == "error"
    assert [report["ids"][i] for i in finding["rows"]] == [point.id]
    # The whole-line detector has no recognized ZZ string to group, so the row-level
    # semantic order check is what carries this issue into the project review.
    assert report["line_issues"] == []


def test_curve_start_before_line_start_is_flagged_when_feature_code_is_unknown():
    from plumbline.core.fieldbook_syntax import command_map
    from plumbline.fieldwork.parse import _validate_line_command_order

    def line_order_flags(description):
        row = ["17", "17", "0", "0", "0", description, "", "crew.csv"]
        return _validate_line_command_order([row], {"toc"}, commands=command_map())

    # ZZ is intentionally absent from this active-book code set; PC before ST is still an error.
    flags = line_order_flags("ZZ PC ST")
    assert "LineOrderError" in flags["17"]

    # A trailing note is outside the code portion and must not suppress the finding, while
    # command-looking text after the description separator is not part of line ordering.
    assert "LineOrderError" in line_order_flags("ZZ PC ST / note")["17"]
    assert "LineOrderError" not in line_order_flags("ZZ / note PC ST").get("17", [])
    assert "LineOrderError" not in line_order_flags("ZZ ST PC").get("17", [])


def test_point_order_checks_only_three_pairs_per_code_occurrence():
    from plumbline.core.fieldbook_syntax import command_map
    from plumbline.fieldwork.parse import _validate_line_command_order

    def has_order_error(description, codes=F2F, commands=None):
        row = ["17", "17", "0", "0", "0", description, "", "crew.csv"]
        flags = _validate_line_command_order(
            [row], codes, commands=commands or command_map())
        return "LineOrderError" in flags.get("17", [])

    # The three semantic pairs are checked within a single code-token occurrence.
    for description in (
        "TOC PC ST",                       # Start Curve before Start Line
        "TOC END PT",                      # End Line before End Curve
        "TOC X PT",                        # Close before End Curve
        "TOC ST PC END PT",                # End Line before End Curve in a longer section
        "TOC ST PC X PT",                  # Close before End Curve in a longer section
        "TOC PC ST - EC1 END",             # do not let the EC1 singleton create a second error
        "TOC ST PC - ZZ PC ST",            # unknown-code sections are still checked independently
    ):
        assert has_order_error(description), description

    # Singleton commands, different code sections, repeated same-code sections, and unrelated
    # sequence mistakes are outside these three checks.
    for description in (
        "TOC PC",
        "TOC END",
        "TOC ST PC",
        "TOC PT ST",                       # not one of the three requested pairs
        "TOC ST PC PT END",
        "TOC ST PC PC PT END",             # repeated Start Curve is not an order-pair check
        "TOC ST PC PT END ST PC PT END",   # two valid line segments
        "TOC PC - EC1 ST",                # one command on each distinct code
        "TOC ST - EC1 END",               # one command on each distinct code
        "TOC PC - TOC ST",                # same code spelling, distinct sections
        "ZZ PC",                           # singleton unknown-code command
    ):
        assert not has_order_error(description), description

    custom_commands = {
        "start_line": "BEGINLN", "start_curve": "BC", "end_curve": "EC",
        "end_line": "FINISH", "close": "CLOSEFIG", "multicode": "PLUS",
        "description": "NOTE",
    }
    assert has_order_error("EA BC BEGINLN", codes={"ea"}, commands=custom_commands)
    assert has_order_error("EA BEGINLN FINISH EC", codes={"ea"}, commands=custom_commands)
    assert has_order_error("EA BEGINLN CLOSEFIG EC", codes={"ea"}, commands=custom_commands)


def test_line_order_autofix_reorders_only_the_supported_pairs_in_each_code_section():
    from plumbline.core.point_linework_coder import fix_line_command_order

    assert fix_line_command_order("TOC PC ST - EC1 END / curb note") == \
        "TOC ST PC - EC1 END / curb note"
    assert fix_line_command_order("TOC ST PC END PT") == "TOC ST PC PT END"
    assert fix_line_command_order("TOC ST PC X PT") == "TOC ST PC PT X"
    assert fix_line_command_order("TOC PT ST") == "TOC PT ST"
    assert fix_line_command_order("TOC ST PC PC PT END") == "TOC ST PC PC PT END"
    assert fix_line_command_order("TOC ST PC PT END ST PC PT END") == \
        "TOC ST PC PT END ST PC PT END"

    custom_commands = {
        "start_line": "BEGINLN", "start_curve": "BC", "end_curve": "EC",
        "end_line": "FINISH", "close": "CLOSEFIG", "multicode": "PLUS",
        "description": "NOTE",
    }
    assert fix_line_command_order("EA BC BEGINLN", commands=custom_commands) == "EA BEGINLN BC"
    assert fix_line_command_order("EA BEGINLN FINISH EC", commands=custom_commands) == \
        "EA BEGINLN EC FINISH"


def test_the_dock_reports_the_line_issues_and_they_select_the_points(win, app):
    """The field-side flags, on the drawing side: findings over the project's own points."""
    from plumbline.ui.check_dock import CheckFieldworkDock

    pr = win.state.project
    for p in list(pr.points.values()):
        del pr.points[p.id]
    for i, desc in enumerate(["TOC ST", "TOC PC"]):
        pr.add_point(2000.0 + i, 1000.0 + i, 500.0, number=f"81{i}", desc=desc)
    ids = sorted(pr.points)

    dock = CheckFieldworkDock(win.state, job_root=lambda: None)
    dock.code_set = {"toc"}
    dock.result = None
    from plumbline.fieldwork import bridge as FB
    dock.result = FB.check_project(pr, f2f={"toc"}, fieldbook_path=None)

    line_findings = [f for f in dock.result["findings"] if f["check"].startswith("line:")]
    assert line_findings, [f["check"] for f in dock.result["findings"]]
    assert all(f["level"] == "warn" for f in line_findings)
    picked = {dock.result["ids"][i] for f in line_findings for i in f["rows"]}
    assert picked <= set(ids) and picked, "a finding names the points it is about"
    assert any(f["check"] == "line: missing end" for f in line_findings)
    assert any("never closed" in f["message"] for f in line_findings)


def test_fix_points_workbench_surfaces_blank_descriptions_with_job_book(win, app, tmp_path):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    project = win.state.project
    for point in list(project.points.values()):
        del project.points[point.id]
    project.settings.pop("fieldbook_file", None)
    project.settings.pop("f2f_path", None)
    job_root = tmp_path / "blank-job"
    book = job_root / "Field Book" / "Blank Job.fwb"
    book.parent.mkdir(parents=True)
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["TOC", "Top of curb", "", "ROAD", "Line", "Road"]],
    )
    win._job_root_choice = job_root
    point = project.add_point(100, 200, 5, number="9001", desc="")

    dialog = FixPointErrorsDialog(win.state, win)
    try:
        assert dialog.code_set == {"toc"}
        finding = next(item for item in dialog.active_findings
                       if item.get("flag") == "EmptyDescription")
        assert finding["pids"] == [point.id]
        assert dialog._filter_finding(finding)
        check = next(row for row in dialog._report_check_rows()
                     if row["check_key"] == "EmptyDescription")
        assert check["status"] == "FINDINGS"
    finally:
        dialog.close()


def test_fix_linework_keeps_lineorder_findings_in_fix_points(win, app, tmp_path):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.ui.qa_workspace import FixLineworkDialog

    project = win.state.project
    for point in list(project.points.values()):
        del project.points[point.id]
    project.settings.pop("fieldbook_file", None)
    project.settings.pop("f2f_path", None)
    job_root = tmp_path / "order-job"
    book = job_root / "Field Book" / "Order Job.fwb"
    book.parent.mkdir(parents=True)
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["EG", "Edge of gravel", "", "ROAD", "Line", "Road"],
         ["EC", "Edge of concrete", "", "ROAD", "Line", "Road"]],
    )
    win._job_root_choice = job_root
    project.add_point(100, 200, 5, number="9002", desc="EG PC ST - EC1 END")

    dialog = FixLineworkDialog(win.state, win)
    try:
        assert dialog.code_set == {"eg", "ec"}
        assert dialog.fieldbook_path == str(book)
        assert not any(item.get("flag") == "LineOrderError" for item in dialog.active_findings)
        assert not dialog._filter_finding({"check": "Line command order"})
        assert not dialog._filter_finding({"check": "Line command out of order"})
        assert not dialog._filter_finding({"check": "line: line order"})
        assert "Line Order" not in {row["check_key"] for row in dialog._report_check_rows()}
        # Other sequence-completeness checks remain in Fix Linework.
        assert any(item.get("check") == "line: missing st" for item in dialog.active_findings)
    finally:
        dialog.close()


def test_fix_points_does_not_pull_singleton_or_cross_code_order_findings(win, app, tmp_path):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.fieldwork.bridge import check_project
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    project = win.state.project
    for point in list(project.points.values()):
        del project.points[point.id]
    project.settings.pop("fieldbook_file", None)
    project.settings.pop("f2f_path", None)
    job_root = tmp_path / "singleton-order-job"
    book = job_root / "Field Book" / "Singletons.fwb"
    book.parent.mkdir(parents=True)
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["EG", "Edge of gravel", "", "ROAD", "Line", "Road"],
         ["EC", "Edge of concrete", "", "ROAD", "Line", "Road"]],
    )
    win._job_root_choice = job_root
    project.add_point(100, 200, 5, number="9101", desc="EG PC - EC1 ST")
    project.add_point(101, 201, 5, number="9102", desc="EC ST - EG1 END")

    report = check_project(project, f2f={"eg", "ec"}, fieldbook_path=str(book))
    assert not any(finding.get("flag") == "LineOrderError" for finding in report["findings"])
    assert not any(issue.get("issue_type") == "Line Order" for issue in report["line_issues"])

    dialog = FixPointErrorsDialog(win.state, win)
    try:
        assert not any(item.get("flag") == "LineOrderError" and not item.get("zero_check")
                       for item in dialog.active_findings)
        order_check = next(row for row in dialog._report_check_rows()
                           if row["check_key"] == "LineOrderError")
        assert order_check["status"] == "CLEAR"
    finally:
        dialog.close()


def test_fix_points_owns_valid_multicode_lineorder_findings(win, app, tmp_path):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    project = win.state.project
    for point in list(project.points.values()):
        del project.points[point.id]
    project.settings.pop("fieldbook_file", None)
    project.settings.pop("f2f_path", None)
    job_root = tmp_path / "order-job"
    book = job_root / "Field Book" / "Order Job.fwb"
    book.parent.mkdir(parents=True)
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["EG", "Edge of gravel", "", "ROAD", "Line", "Road"],
         ["EC", "Edge of concrete", "", "ROAD", "Line", "Road"]],
    )
    win._job_root_choice = job_root
    multicode_order = project.add_point(100, 200, 5, number="9003",
                                        desc="EG PC ST - EC1 END")
    curve_before_line = project.add_point(101, 201, 5, number="9004", desc="EG PC ST")
    valid_curves = project.add_point(102, 202, 5, number="9005",
                                     desc="EC ST PC PT PC PT END")

    dialog = FixPointErrorsDialog(win.state, win)
    try:
        assert dialog.code_set == {"eg", "ec"}
        assert dialog.fieldbook_path == str(book)
        assert not any(str(item.get("flag", "")).casefold().startswith("unknowncode")
                       for item in dialog.active_findings)
        point_finding = next(item for item in dialog.active_findings
                             if item.get("check") == "Line command order")
        sequence_finding = next(item for item in dialog.active_findings
                                if item.get("check") == "line: line order")
        assert point_finding["flag"] == sequence_finding["flag"] == "LineOrderError"
        assert point_finding["level"] == sequence_finding["level"] == "error"
        displayed_names = [dialog.tbl_active.item(row, 2).text()
                           for row in range(dialog.tbl_active.rowCount())]
        assert displayed_names.count("Line command order") >= 2
        assert "line: line order" not in displayed_names
        expected_order_points = [multicode_order.id, curve_before_line.id]
        assert point_finding["pids"] == expected_order_points
        assert sequence_finding["pids"] == [multicode_order.id]
        assert valid_curves.id not in point_finding["pids"]
        assert dialog._filter_finding(point_finding)
        assert dialog._filter_finding(sequence_finding)
        # The stable flag is preferred, but report-compatibility findings without it must
        # still be routed here instead of being dropped by the generic "line" filter.
        flagless_order = {"check": "Line command order", "level": "error", "rows": [0]}
        assert dialog._filter_finding(flagless_order)
        assert dialog._filter_finding({"check": "Line command out of order"})
        assert dialog._filter_finding({"check": "line: line order"})
        original_result = dialog.result
        dialog.result = {**original_result, "findings": [flagless_order]}
        fallback_check = next(row for row in dialog._report_check_rows()
                              if row["check_key"] == "LineOrderError")
        assert fallback_check["status"] == "FINDINGS"
        assert fallback_check["pids"] == [multicode_order.id]
        dialog.result = original_result
        check = next(row for row in dialog._report_check_rows()
                     if row["check_key"] == "LineOrderError")
        assert check["status"] == "FINDINGS"
    finally:
        dialog.close()


def test_line_order_review_uses_common_error_table_and_supports_skip_ignore_undo(win, app, tmp_path):
    from plumbline.core.point_linework_coder import fix_line_command_order
    from plumbline.fieldwork.io_carlson import write_fwb_file
    from plumbline.ui.qa_workspace import FixPointErrorsDialog

    project = win.state.project
    for point in list(project.points.values()):
        del project.points[point.id]
    project.settings.pop("fieldbook_file", None)
    project.settings.pop("f2f_path", None)
    job_root = tmp_path / "line-order-actions"
    book = job_root / "Field Book" / "Actions.fwb"
    book.parent.mkdir(parents=True)
    assert write_fwb_file(
        book,
        ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
        [["EG", "Edge of gravel", "", "ROAD", "Line", "Road"],
         ["EC", "Edge of concrete", "", "ROAD", "Line", "Road"]],
    )
    win._job_root_choice = job_root
    correct_point = project.add_point(100, 200, 5, number="9011", desc="EG PC ST - EC1 END")
    skip_point = project.add_point(101, 201, 5, number="9012", desc="EG PC ST")
    original_correct = correct_point.desc
    original_skip = skip_point.desc

    dialog = FixPointErrorsDialog(win.state, win)
    try:
        finding = next(item for item in dialog.active_findings
                       if item.get("check") == "Line command order")
        dialog._open_inline_editor(finding)
        assert dialog.lbl_edit_title.text() == "<b>Fix Line command order</b>"
        assert dialog.tbl_corrections.columnCount() == 4
        assert [dialog.tbl_corrections.horizontalHeaderItem(i).text() for i in range(4)] == [
            "Pt #", "Original Description", "Corrected Description", "Action",
        ]
        rows_by_pid = {item[0].id: item for item in dialog.sep_corrections}
        assert set(rows_by_pid) == {correct_point.id, skip_point.id}
        for point_id, item in rows_by_pid.items():
            point, _original, fixed, action, proposal, _proposal_number = item
            assert action.currentText() == "Correct"
            assert [action.itemText(i) for i in range(action.count())] == ["Skip", "Correct", "Ignore"]
            assert proposal == fix_line_command_order(point.desc)
            assert fixed.text() == proposal
        assert dialog._has_unapplied_issue_edits()

        # Apply the first proposal while leaving the second point active via Skip.
        rows_by_pid[skip_point.id][3].setCurrentText("Skip")
        assert dialog._action_apply_current_issue_changes()
        assert correct_point.desc == fix_line_command_order(original_correct)
        assert skip_point.desc == original_skip
        assert len(dialog.sep_corrections) == 1
        assert dialog.sep_corrections[0][0].id == skip_point.id
        assert dialog.sep_corrections[0][3].currentText() == "Skip"

        # Undo restores the dropdowns selected at Apply time, but never reapplies proposals.
        dialog._undo_issue()
        assert correct_point.desc == original_correct
        assert skip_point.desc == original_skip
        restored = {item[0].id: item for item in dialog.sep_corrections}
        assert restored[correct_point.id][3].currentText() == "Correct"
        assert restored[skip_point.id][3].currentText() == "Skip"
        assert restored[correct_point.id][2].text() == fix_line_command_order(original_correct)

        # Ignore suppresses only its point; Skip leaves the other point in the active finding.
        restored[correct_point.id][3].setCurrentText("Skip")
        restored[skip_point.id][3].setCurrentText("Ignore")
        assert dialog._action_apply_current_issue_changes()
        assert correct_point.desc == original_correct
        assert skip_point.desc == original_skip
        assert dialog.current_edit_finding["ignored_pids"] == {skip_point.id}
        assert dialog.current_edit_finding["pids"] == [correct_point.id]

        # Save applies Correct/Ignore and carries a skipped point's selection back to the
        # issue list so reopening it does not silently reset the row to Correct.
        save_correct = project.add_point(102, 202, 5, number="9013", desc="EG PC ST")
        save_skip = project.add_point(103, 203, 5, number="9014", desc="EG PC ST")
        dialog.run_check()
        save_finding = next(item for item in dialog.active_findings
                            if item.get("check") == "Line command order")
        dialog._open_inline_editor(save_finding)
        save_rows = {item[0].id: item for item in dialog.sep_corrections}
        save_rows[correct_point.id][3].setCurrentText("Ignore")
        save_rows[save_correct.id][3].setCurrentText("Correct")
        save_rows[save_skip.id][3].setCurrentText("Skip")
        assert dialog._action_apply_current_issue_changes(return_to_summary=True)
        assert save_correct.desc == fix_line_command_order("EG PC ST")
        assert save_skip.desc == "EG PC ST"
        remaining = next(item for item in dialog.active_findings
                         if item.get("check") == "Line command order")
        assert remaining["pids"] == [save_skip.id]
        assert remaining["separator_drafts"][save_skip.id]["action"] == "Skip"
        dialog._open_inline_editor(remaining)
        assert len(dialog.sep_corrections) == 1
        assert dialog.sep_corrections[0][0].id == save_skip.id
        assert dialog.sep_corrections[0][3].currentText() == "Skip"
    finally:
        dialog.close()


def test_a_job_with_no_vocabulary_is_told_the_line_check_cannot_run_and_is_not_faked(win, app):
    """No field book, no codes: the number checks run, the line check does not pretend."""
    from plumbline.ui.check_dock import CheckFieldworkDock
    from plumbline.fieldwork import bridge as FB

    pr = win.state.project
    for p in list(pr.points.values()):
        del pr.points[p.id]
    for i, desc in enumerate(["TOC ST", "TOC PC"]):
        pr.add_point(2000.0 + i, 1000.0 + i, 500.0, number=f"82{i}", desc=desc)
    pr.settings.pop("f2f_path", None)

    dock = CheckFieldworkDock(win.state, job_root=lambda: None)
    assert dock.code_set == set()
    dock.run()
    assert dock.result["line_issues"] == []
    assert not [f for f in dock.result["findings"] if f["check"].startswith("line:")]
    assert "line checks cannot" in dock.banner.text(), "and the banner says which half it ran"


def test_the_jobs_own_code_table_is_a_vocabulary_like_a_field_book(win, app, tmp_path):
    """The bug this pins: a feature-code table keeps its codes in capitals, the parser compares
    casefolded tokens, and passing the table's own keys made every code read as unknown - a
    hundred false alarms, which is how a check gets switched off and stays off."""
    from plumbline.ui.check_dock import CheckFieldworkDock

    pr = win.state.project
    for p in list(pr.points.values()):
        del pr.points[p.id]
    pr.settings["f2f_path"] = str(tmp_path / "office.csv")
    from plumbline.core.featurecodes import FeatureCode, FeatureCodeTable
    pr.codes = FeatureCodeTable([FeatureCode("TOC", "Top of curb", "line", "ROAD-CURB-TOP")])
    for i, desc in enumerate(["TOC ST", "TOC PC"]):
        pr.add_point(2000.0 + i, 1000.0 + i, 500.0, number=f"83{i}", desc=desc)

    dock = CheckFieldworkDock(win.state, job_root=lambda: None)
    assert dock.code_set == {"toc"}
    assert "all five checks" in dock.banner.text()
    dock.run()
    checks = {f["check"] for f in dock.result["findings"]}
    assert "Unknown code" not in checks, "the office's own table is a vocabulary, not a mystery"
    assert any(c.startswith("line: ") for c in checks), "and the line check could run on it"


# ------------------------------------------------------------------ the field window's tools
@pytest.fixture()
def field_window(app, tmp_path, monkeypatch):
    """The real field window (Survey > Fieldwork Manager), offscreen, pointed at a field book.

    The static QMessageBox helpers are the ones the tools speak through, and they run their own
    modal loop in C++ - so they are redirected here and their text is kept for the test to read.
    """
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QEvent
    said = []

    def record(kind):
        def _box(*args, **kwargs):
            title = str(args[2]) if len(args) > 2 else ""
            said.append((kind, title))
            return QMessageBox.StandardButton.Ok
        return staticmethod(_box)

    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind, record(kind))
    monkeypatch.setenv("PLUMBLINE_HOME", str(tmp_path / "home"))
    from plumbline.core import settings as S
    S._instance = None
    from plumbline.fieldwork.ui_main import MainWindow as FieldMainWindow
    win = FieldMainWindow()
    win.resize(1200, 800)
    win.said = said
    yield win
    win.close()
    win.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    S._instance = None


def _fieldbook(path, codes=("TOC", "EC", "GS")):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    path.parent.mkdir(parents=True, exist_ok=True)
    write_fwb_file(path, ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
                   [[c, c, "cross", "V-SITE", "1", ""] for c in codes],
                   commands=["ST", "PC", "PT", "END", "X"], rules=[])
    return path


def test_the_field_window_proposes_fixes_and_refuses_to_call_a_still_broken_line_fixed(
        field_window, tmp_path, app, auto):
    """Fix, then read the line again: the tool says what it did, including when it was not enough."""
    win = field_window
    win.fieldbook_path = str(_fieldbook(tmp_path / "office.fwb"))
    win._open_edit_tab(rows("TOC ST", "TOC PC"))
    win._refresh_line_repair()
    assert win.line_table.rowCount() == 2, [win.line_table.item(r, 1).text() for r in range(2)]
    assert "proposed:" in win.line_table.item(0, 6).text()

    # the first proposal closes the line; the curve inside it is still unfinished, and it says so
    win.line_table.selectRow(0)
    win._line_fix_first()
    assert win.edit_table.item(1, 5).text() == "TOC PC END"
    assert win.line_table.item(0, 7).text() == "Still flagged"
    assert "still has" in win.summary_label.text() and "Key-In" in win.summary_label.text()
    assert any("still stands" in t for _k, t in win.said), "and it says so in a box, not only in a strip"

    # ... so the second pass finishes it - and this time the answer is "corrected"
    win._refresh_line_repair()
    win.line_table.selectRow(0)
    win._line_fix_first()
    assert win.edit_table.item(1, 5).text() == "TOC PC PT END"
    assert "cleared" in win.summary_label.text()
    assert win._line_working_rows()[1][5] == "TOC PC PT END"


def test_key_in_writes_what_the_user_typed_and_validates_it(field_window, tmp_path, app, auto, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    win = field_window
    win.fieldbook_path = str(_fieldbook(tmp_path / "office.fwb"))
    win._open_edit_tab(rows("TOC ST", "TOC PC"))
    win._refresh_line_repair()
    win.line_table.selectRow(0)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("TOC PC PT END", True)))
    win._line_key_in()
    assert win.edit_table.item(1, 5).text() == "TOC PC PT END"
    assert win.line_table.item(0, 7).text() == "Corrected"


def test_an_ignored_line_issue_is_written_to_the_check_report_and_stays_ignored(
        field_window, tmp_path, app, auto):
    """A decision, kept: the next run reads it back instead of re-opening the issue."""
    from plumbline.fieldwork.io_carlson import (build_unified_report_rows, read_unified_report,
                                                write_unified_report)
    win = field_window
    win.fieldbook_path = str(_fieldbook(tmp_path / "office.fwb"))
    working = rows("TOC ST", "TOC PC")
    win._open_edit_tab(working)
    report = tmp_path / "check.fwc"
    write_unified_report(report, build_unified_report_rows(
        working, [], [], [], [], line_issues=LC.detect_line_errors(working, f2f_set=F2F)))
    win.check_report_path = str(report)

    win._refresh_line_repair()
    assert win.line_table.rowCount() == 2
    win.line_table.selectRow(0)
    win._line_ignore()
    assert win.line_table.item(0, 7).text() == "Ignored"
    assert "does not re-open" in win.summary_label.text()

    _h, written = read_unified_report(report)
    line_rows = [r for r in written if r[1] == "Line"]
    assert len(line_rows) == 2 and sum(1 for r in line_rows if r[7] == "Ignored") == 1

    win._line_decisions = {}                       # a fresh run, reading the report back
    win._refresh_line_repair()
    assert win.line_table.item(0, 7).text() == "Ignored"
    assert "Ignored in Line Repair" in win.line_table.item(0, 6).text()
