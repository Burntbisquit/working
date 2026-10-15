"""Regression tests for segment-boundary command QA without Qt fixtures."""
from __future__ import annotations

from plumbline.fieldwork.linecheck import detect_line_errors


def _rows(*descriptions):
    return [[str(index), str(index), "0", "0", "0", desc, "", ""]
            for index, desc in enumerate(descriptions, start=1)]


def test_first_plain_vertex_and_continuation_after_end_need_start_line():
    issues = detect_line_errors(_rows("TOC", "TOC END"), f2f_set={"toc"})
    assert [(issue["issue_type"], issue["oid"]) for issue in issues] == [("Missing ST", "1")]

    issues = detect_line_errors(
        _rows("TOC ST", "GS", "TOC END", "GS", "TOC", "TOC END"),
        f2f_set={"toc", "gs"},
    )
    starts = [issue for issue in issues if issue["issue_type"] == "Missing ST"]
    assert [(issue["oid"], "after End Line/Close" in issue["detail"]) for issue in starts] == [("5", True)]


def test_new_start_breaks_the_line_but_reports_missing_end_on_prior_matching_vertex():
    issues = detect_line_errors(_rows("SW1 ST", "GS", "SW1", "SW1 ST", "SW1 END"),
                                f2f_set={"sw", "gs"})
    ends = [issue for issue in issues if issue["issue_type"] == "Missing END"]
    assert [(issue["oid"], issue["proposal"]) for issue in ends] == [("3", "SW1 END")]
