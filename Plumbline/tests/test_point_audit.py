"""The Point(s) Audit (item 17): the job's field points against the state they were imported in.

The change list asked for a *"Point(s) Audit" report* and recorded the decision with it: the audit
reads against **the imported state**, not a baseline the user marks by hand, so the same report
says the same thing tomorrow.  That makes the baseline something the program has to keep, because
nothing before this recorded what a point *was* when it arrived - and two of the four questions an
audit is asked (*which imported points are gone*, *which ones moved*) cannot be answered from the
live point set at all, since a deleted point takes its own evidence with it.

What is checked here: that every import door writes the record, that all four answers come back
right (missing, added, moved, changed), that a change of coordinate system is not mistaken for
every point on the job moving, that the record survives a save, a load and an undo, and that the
report is in the Reports menu where the item said it would be.
"""
import contextlib
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")

from plumbline.core import audit as AUD
from plumbline.core import crs as C
from plumbline.core import reference as REF
from plumbline.core.project import Project
from plumbline.fieldwork import bridge as FB
from plumbline.io import csv_points as CSV
from plumbline.io import reports
from plumbline.ui.import_export import ImportPlan, apply_import
from test_ui import app, auto, pump, win  # noqa: F401  (fixtures)

BASE_E, BASE_N = 2552700.0, 6967100.0


# --------------------------------------------------------------------------------------------- harness
class _State:
    """What apply_import needs and nothing else: a project, an edit step, a log."""

    def __init__(self, project):
        self.project = project
        self.sel_points: set = set()
        self.sel_entities: set = set()
        self.logged: list = []

    @contextlib.contextmanager
    def edit(self, label, kinds=("all",)):
        yield self

    def log(self, text, level="info"):
        self.logged.append((level, text))


def _project():
    return Project("Audit", C.ProjectCRS.from_key(6584))


def _csv(path: Path, rows, header="PT,NORTHING,EASTING,ELEVATION,DESCRIPTION"):
    path.write_text("\n".join([header] + [",".join(str(c) for c in r) for r in rows]) + "\n",
                    encoding="utf-8")
    return path


def _import_csv(state, path, label=None, role=None, job_root=None, policy="renumber"):
    batch = CSV.read_points(path, CSV.CsvMapping(*_sniff(path)))
    plan = ImportPlan(reference_role=role, dup_policy=policy)
    return apply_import(state, batch, plan, label or f"points ({path.name})", path, job_root)


def _sniff(path):
    sn = CSV.sniff(path)
    return (sn.delimiter, 1 if sn.has_header else 0, list(sn.roles))


def _three_points(tmp_path):
    """A job holding three imported points, and a state to import more into."""
    state = _State(_project())
    _import_csv(state, _csv(tmp_path / "crew6.csv",
                            [[1, BASE_N, BASE_E, 500.0, "EP1"],
                             [2, BASE_N + 100, BASE_E + 100, 501.0, "EP1"],
                             [3, BASE_N + 200, BASE_E + 200, 502.0, "EP1"]]))
    return state, state.project


def _by_number(pr, number):
    return next(p for p in pr.points.values() if p.number == str(number))


# --------------------------------------------------------------------------------------------- the record
def test_an_import_records_what_the_points_were(tmp_path):
    """The baseline is written by the import itself: coordinates, description, and where it came from."""
    state, pr = _three_points(tmp_path)
    rows = AUD.rows(pr)
    assert [r["number"] for r in rows] == ["1", "2", "3"]
    first = rows[0]
    assert first["y"] == pytest.approx(BASE_N) and first["x"] == pytest.approx(BASE_E)
    assert first["z"] == pytest.approx(500.0) and first["desc"] == "EP1"
    assert first["file"] == "crew6.csv" and first["when"]
    assert AUD.has_baseline(pr) and "3 imported point(s) on record" in AUD.describe(pr)


def test_transformed_import_records_project_crs_and_original_source_provenance(tmp_path):
    from pyproj import Transformer

    from plumbline.core import filecrs as FCRC

    state = _State(_project())
    source_crs = C.CRS.from_epsg(32614)
    easting, northing = Transformer.from_crs(4326, source_crs, always_xy=True).transform(-96.1, 32.8)
    path = _csv(tmp_path / "utm_points.csv", [[1, northing, easting, 123.0, "CP"]])
    batch = CSV.read_points(path, CSV.CsvMapping(*_sniff(path)))
    plan = ImportPlan(src_crs=source_crs)
    apply_import(state, batch, plan, "UTM control", path)

    point = _by_number(state.project, "1")
    expected_x, expected_y = state.project.crs.transform_from(source_crs)(
        np.array([easting]), np.array([northing]))
    record = FCRC.for_file(state.project, path.name)
    assert (point.x, point.y) == pytest.approx((expected_x[0], expected_y[0]))
    assert record["key"] == state.project.crs.authority
    assert record["method"] == "imported"
    assert record["source_crs"]["key"] == "EPSG:32614"


def test_a_fresh_job_is_clean_and_says_so(tmp_path):
    """Nothing has happened to the job yet, and the audit says exactly that rather than nothing."""
    _, pr = _three_points(tmp_path)
    a = AUD.compare(pr)
    assert a.clean and a.unchanged == 3
    assert a.summary_line() == "All 3 field point(s) are exactly as imported."


def test_a_point_deleted_after_the_import_is_reported_as_missing(tmp_path):
    """A deleted imported point is invisible in the live point set - the baseline is the only witness."""
    _, pr = _three_points(tmp_path)
    del pr.points[_by_number(pr, 2).id]
    a = AUD.compare(pr)
    assert [r["number"] for r in a.missing] == ["2"]
    assert a.missing[0]["file"] == "crew6.csv"
    assert a.missing[0]["y"] == pytest.approx(BASE_N + 100)


def test_a_point_drawn_by_hand_is_reported_as_added(tmp_path):
    """A point with no import behind it is what the drawing gained, and says so in plain words."""
    _, pr = _three_points(tmp_path)
    pr.add_point(BASE_E + 500, BASE_N + 500, 510.0, number="90", desc="set out")
    a = AUD.compare(pr)
    assert [r["number"] for r in a.added] == ["90"]
    assert a.added[0]["desc"] == "set out"


def test_a_moved_point_is_reported_with_its_deltas_distance_and_bearing(tmp_path):
    """The move is the interesting number, so the audit reports the maths, not just a flag."""
    _, pr = _three_points(tmp_path)
    p = _by_number(pr, 1)
    p.x += 3.0
    p.y += 4.0
    a = AUD.compare(pr)
    assert len(a.moved) == 1
    m = a.moved[0]
    assert (m["dn"], m["de"]) == pytest.approx((4.0, 3.0))
    assert m["dist"] == pytest.approx(5.0)
    assert m["azimuth"] == pytest.approx(36.8698976, abs=1e-6)          # NE, 3-4-5
    assert m["x0"] == pytest.approx(BASE_E) and m["x"] == pytest.approx(BASE_E + 3.0)


def test_float_noise_is_not_a_move(tmp_path):
    """An operation that should not move a point leaves it where it was; a millionth is not a survey."""
    _, pr = _three_points(tmp_path)
    _by_number(pr, 1).x += AUD.DEFAULT_TOLERANCE / 10.0
    a = AUD.compare(pr)
    assert not a.moved and a.clean


def test_every_attribute_change_is_reported_under_its_own_name(tmp_path):
    """Number, description, layer and elevation, each reported for what it is."""
    _, pr = _three_points(tmp_path)
    p1, p2, p3 = _by_number(pr, 1), _by_number(pr, 2), _by_number(pr, 3)
    p1.number = "12"
    p2.desc = "EP1 B"
    p3.layer = "V-UTIL"
    a = AUD.compare(pr)
    kinds = {c["number"]: c["kinds"] for c in a.changed}
    assert kinds == {"12": ["number"], "2": ["description"], "3": ["layer"]}
    p2.z = 501.4
    a = AUD.compare(pr)
    assert "elevation" in next(c["kinds"] for c in a.changed if c["number"] == "2")


def test_a_point_that_gained_or_lost_its_elevation_is_a_change(tmp_path):
    """No elevation is a fact, not a zero: gaining one is a change and so is losing one."""
    _, pr = _three_points(tmp_path)
    _by_number(pr, 1).z = float("nan")
    a = AUD.compare(pr)
    assert "elevation" in next(c["kinds"] for c in a.changed if c["number"] == "1")


def test_the_overwrite_policy_does_not_blame_the_import_for_what_it_wrote(tmp_path):
    """An import that lands on top of a number *is* the imported state - not an edit to report."""
    state, pr = _three_points(tmp_path)
    _import_csv(state, _csv(tmp_path / "fixed.csv", [[1, BASE_N + 7, BASE_E + 7, 505.0, "EP2"]]),
                policy="overwrite")
    a = AUD.compare(pr)
    assert a.clean, a.summary_line()
    assert _by_number(pr, 1).desc == "EP2"


def test_a_reference_import_is_not_audited(tmp_path):
    """Stake-out and control are somebody else's coordinates on loan - not this job's field work."""
    state, pr = _three_points(tmp_path)
    _import_csv(state, _csv(tmp_path / "stakeout.csv", [[1, BASE_N + 10, BASE_E + 10, 500.0, "SO"]]),
                role="stakeout")
    assert len(AUD.rows(pr)) == 3                       # the reference point was never recorded
    assert AUD.compare(pr).clean
    REF.remove_all(pr)
    assert AUD.compare(pr).clean                        # removing them is not a change either


def test_a_reprojection_carries_the_record_with_it(tmp_path):
    """A change of ruler is not a change to the data - the audit must not cry wolf on all of it.

    The record is converted *while* the reprojection happens (Project.reproject calls into
    core.audit), because afterwards a reprojection and an assignment look identical from the
    outside and the wrong guess reports every point on the job as having moved.
    """
    _, pr = _three_points(tmp_path)
    pr.reproject(C.ProjectCRS.from_key(6583))           # US ft -> metres, same zone
    assert AUD.store(pr)["crs"] == pr.crs.to_dict()     # converted, not left behind
    a = AUD.compare(pr)
    assert a.clean, a.summary_line()
    assert any("reprojected" in n for n in a.notes)
    assert AUD.crs_note(pr)                              # and the report can say so


def test_assigning_a_coordinate_system_is_not_a_reprojection(tmp_path):
    """*Assign* keeps every number where it is - so the audit must not report the job as moved.

    This is the trap the eager conversion exists for: from the outside, "the system changed" looks
    the same whether the numbers moved with it or not.  Assigning one is a relabel, so the record
    is relabelled too.
    """
    _, pr = _three_points(tmp_path)
    pr.assign_crs(C.ProjectCRS.from_key(6583))          # the numbers do NOT move
    a = AUD.compare(pr)
    assert a.clean, a.summary_line()
    assert any("assigned, not reprojected" in n for n in a.notes)


def test_a_local_job_that_is_given_a_system_keeps_its_record(tmp_path):
    """Import into an unassigned job, then assign the system: the ordinary way a job begins."""
    state = _State(Project("Unassigned", C.ProjectCRS.local()))
    _import_csv(state, _csv(tmp_path / "crew6.csv", [[1, BASE_N, BASE_E, 500.0, "EP1"]]))
    assert AUD.compare(state.project).clean
    state.project.assign_crs(C.ProjectCRS.from_key(6584))
    a = AUD.compare(state.project)
    assert a.clean and "assigned" in " ".join(a.notes)
    assert AUD.rows(state.project)[0]["y"] == pytest.approx(BASE_N)      # not converted anywhere


def test_a_record_that_cannot_follow_a_reprojection_is_cleared_rather_than_guessed(tmp_path):
    """A record that cannot follow the data is not a record of it: better none than a false one.

    Reaching this needs the conversion itself to fail, which the ordinary reprojection cannot
    arrange (its own transform refuses first for the systems that cannot be crossed), so the guard
    is driven directly.  What it must never do is leave the two sets of coordinates in place and
    call the difference between them movement.
    """
    _, pr = _three_points(tmp_path)
    bogus = {"name": "a system this build does not know", "wkt": None, "key": "??", "vunit": "ftUS"}
    note = AUD.reprojected(pr, type("C", (), {"to_dict": lambda self: bogus})())
    assert not AUD.has_baseline(pr)
    assert "cleared" in note
    a = AUD.compare(pr)
    assert a.baseline == 0 and not a.moved
    assert any("cleared" in n for n in a.notes)


def test_the_record_survives_a_save_and_a_load(tmp_path):
    """The baseline lives in the project, so the report says the same thing next month."""
    _, pr = _three_points(tmp_path)
    _by_number(pr, 2).x += 10.0
    path = tmp_path / "Audit.plb"
    pr.save(path)
    again = Project.load(path)
    before, after = AUD.compare(pr), AUD.compare(again)
    assert [m["number"] for m in after.moved] == [m["number"] for m in before.moved]
    assert after.moved[0]["de"] == pytest.approx(before.moved[0]["de"])
    assert after.baseline == before.baseline == 3


def test_the_record_travels_with_the_edit_that_wrote_it(tmp_path):
    """An import is one undo step, and the baseline is part of it - undo restores both."""
    state, pr = _three_points(tmp_path)
    snap = pr.snapshot()
    _import_csv(state, _csv(tmp_path / "crew7.csv", [[7, BASE_N + 400, BASE_E + 400, 520.0, "GS"]]))
    assert len(AUD.rows(pr)) == 4
    pr.restore(snap)
    assert len(AUD.rows(pr)) == 3
    assert not any(p.number == "7" for p in pr.points.values())


def test_a_project_with_no_record_says_so_rather_than_inventing_one(tmp_path):
    """A job from an older version has no baseline; the audit's job is to say that, not to guess."""
    pr = _project()
    pr.add_point(BASE_E, BASE_N, 500.0, number="1", desc="EP1")
    a = AUD.compare(pr)
    assert not AUD.has_baseline(pr)
    assert a.baseline == 0 and not a.missing and not a.added
    assert "nothing to audit" in a.summary_line().lower()
    assert any("No import has recorded" in n for n in a.notes)


def test_running_the_audit_does_not_change_the_project(tmp_path):
    """A report must not write to the thing it is reporting on - including the rebase it works out."""
    _, pr = _three_points(tmp_path)
    pr.reproject(C.ProjectCRS.from_key(6583))
    stored_before = repr(AUD.store(pr))
    AUD.compare(pr)
    assert repr(AUD.store(pr)) == stored_before


def test_the_field_doors_write_the_same_record(tmp_path):
    """The field window's hand-off and a plain CSV import write one kind of row, not two."""
    pr = _project()
    rows = [["", "10", str(BASE_N), str(BASE_E), "500.0", "EP1", "Week 1/Crew 6", "crew6.csv"]]
    FB.apply_rows_to_project(pr, rows, dup_policy="renumber")
    assert len(AUD.rows(pr)) == 1
    assert AUD.rows(pr)[0]["file"] == "crew6.csv"
    assert AUD.compare(pr).clean
    del pr.points[_by_number(pr, 10).id]
    assert [r["number"] for r in AUD.compare(pr).missing] == ["10"]


# --------------------------------------------------------------------------------------------- the report
def test_the_report_says_all_four_things(tmp_path):
    """Missing, added, moved and changed, each with the numbers behind it."""
    _, pr = _three_points(tmp_path)
    del pr.points[_by_number(pr, 3).id]
    pr.add_point(BASE_E + 900, BASE_N + 900, 530.0, number="90", desc="set out")
    _by_number(pr, 1).x += 5.0
    _by_number(pr, 2).desc = "EP1 B"
    rep = reports.point_audit_report(pr, AUD.compare(pr))
    assert rep.title == "Point(s) Audit"
    text = reports.to_html(rep)
    for wanted in ("Missing - imported, no longer in the drawing", "Added - points with no import",
                   "Moved - northing or easting", "Changed - description, number, layer"):
        assert wanted in text
    assert "1 point(s) imported and now gone" in text
    assert "5.000" in text and "90" in text


def test_a_clean_job_reads_as_a_sentence_not_as_empty_tables(tmp_path):
    """Nothing to report is itself a result, and an empty table reads as a mistake."""
    _, pr = _three_points(tmp_path)
    text = reports.to_html(reports.point_audit_report(pr, AUD.compare(pr)))
    assert "None. Every point that was imported is still here." in text
    assert "None. Every imported point is within" in text
    assert "All 3 field point(s) are exactly as imported." in text


def test_the_audit_opens_from_the_reports_menu(win, app, auto):
    """The item said *report*: it is one, in the Reports menu, with the viewer the others open."""
    win.report_audit()
    opened = [w for w in app.topLevelWidgets() if type(w).__name__ == "ReportViewer"]
    assert opened, "the Point(s) Audit did not open a report viewer"
    assert opened[-1].report.title == "Point(s) Audit"
    reports_menu = next((m for m in getattr(win, "_top_menus", []) if m.title().replace("&", "") == "Reports"), None) or next(m.menu() for m in list(win.menuBar().actions()) if m.text().replace("&", "") == "Reports")
    labels = [a.text().replace("&", "") for a in reports_menu.actions()]
    assert "Point(s) Audit..." in labels
