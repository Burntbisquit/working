"""Metadata columns (item 15, first half): where every imported point came from.

The working file the field data is cleaned in has always carried two provenance columns beside the
coordinates - **Parent Folder** and **Source File** (docs/WORKING_PROFILE.md, columns 7-8).  They
are what answers the two questions that come up on every job: *which download is this point from*
and *which crew shot it*.  Until now the drawing half dropped them at the doorstep.

What is checked here is that the record is written by **every** import door, that it survives a save
and a load, and that the point list shows it - and stops showing it - when asked.
"""
import contextlib
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt

from plumbline.core import crs as C
from plumbline.core import provenance as PROV
from plumbline.core.project import Project
from plumbline.core.settings import settings
from plumbline.fieldwork import bridge as FB
from plumbline.io import csv_points as CSV
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
    return Project("Provenance", C.ProjectCRS.from_key(6584))


def _csv(path: Path, rows, header="PT,NORTHING,EASTING,ELEVATION,DESCRIPTION"):
    path.write_text("\n".join([header] + [",".join(str(c) for c in r) for r in rows]) + "\n",
                    encoding="utf-8")
    return path


def _import_csv(state, path, label=None, role=None, job_root=None):
    batch = CSV.read_points(path, CSV.CsvMapping(*_sniff(path)))
    plan = ImportPlan(reference_role=role, dup_policy="renumber")
    return apply_import(state, batch, plan, label or f"points ({path.name})", path, job_root)


def _sniff(path):
    sn = CSV.sniff(path)
    return (sn.delimiter, 1 if sn.has_header else 0, list(sn.roles))


# --------------------------------------------------------------------------------------------- the record
def test_an_import_records_the_file_the_folder_and_the_import_it_belonged_to(tmp_path):
    """One record per point, in the job's own words: the file, where it was, what it was called."""
    job = tmp_path / "23-036 Elm Street"
    src = job / "Field Data" / "Week 1" / "Crew 6" / "crew6.csv"
    src.parent.mkdir(parents=True)
    _csv(src, [(1, BASE_N, BASE_E, 500.0, "GS"), (2, BASE_N + 10, BASE_E + 10, 500.1, "EP B")])
    pr = _project()
    st = _State(pr)

    stats = _import_csv(st, src, label=f"points ({src.name})", job_root=job)
    assert stats["points"] == 2
    rec = PROV.of(pr.point_by_number("1"))
    assert rec["file"] == "crew6.csv"
    assert rec["folder"] == "Field Data/Week 1/Crew 6", "relative to the job folder: the job travels"
    assert rec["set"] == "points", "the file name is already in its own column"
    assert len(rec["when"]) >= 16 and rec["when"][4] == "-" and rec["when"][10] == "T"


def test_a_source_path_written_by_windows_arrives_as_a_name_and_a_folder(tmp_path):
    """Field files are written by Windows programs: the Source column holds ``folder\\file.csv``."""
    pr = _project()
    st = _State(pr)
    rows = [FB.working_row(1, "1", BASE_N, BASE_E, 500.0, "GS",
                           "2026-07-19-S1", "2026-07-19-S1\\2026-07-19GPS S1.csv"),
            FB.working_row(2, "2", BASE_N + 10, BASE_E + 10, 500.1, "GS",
                           "2026-07-19-S1", "2026-07-19-S1/2026-07-19GPS S1.csv")]
    FB.apply_rows_to_project(pr, rows, dup_policy="renumber")

    for number in ("1", "2"):
        rec = PROV.of(pr.point_by_number(number))
        assert rec["file"] == "2026-07-19GPS S1.csv", "either separator, same answer"
        assert rec["folder"] == "2026-07-19-S1"
    assert "\\" not in PROV.describe(pr.point_by_number("1"))


def test_a_folder_outside_the_job_is_kept_whole(tmp_path):
    """A control file from somebody else's folder is not made to look local."""
    pr = _project()
    st = _State(pr)
    other = tmp_path / "elsewhere" / "control.csv"
    other.parent.mkdir(parents=True)
    _csv(other, [(1, BASE_N, BASE_E, 500.0, "CP")])
    _import_csv(st, other, role="control", job_root=tmp_path / "23-036 Elm Street")
    rec = PROV.of(pr.points[list(pr.points)[0]])
    assert rec["folder"] == str(other.parent) and rec["set"] == "Control points"


def test_the_record_survives_a_save_and_a_load(tmp_path):
    """It is part of the point, so it is part of the job file - not a session note."""
    pr = _project()
    st = _State(pr)
    src = tmp_path / "crew7.csv"
    _csv(src, [(7, BASE_N, BASE_E, 500.0, "GS")])
    _import_csv(st, src, job_root=tmp_path)
    f = tmp_path / "job.plb"
    pr.save(f)

    back = Project.load(f)
    assert PROV.of(back.point_by_number("7")) == PROV.of(pr.point_by_number("7"))


def test_the_history_counts_files_not_points(tmp_path):
    """3,773 identical stamps are one row in a person's head: the crew file from Tuesday."""
    pr = _project()
    st = _State(pr)
    for crew, n in ((6, 3), (7, 2)):
        f = tmp_path / f"crew{crew}.csv"
        _csv(f, [(i, BASE_N + i, BASE_E + i, 500.0, "GS") for i in range(1, n + 1)])
        _import_csv(st, f, job_root=tmp_path)
    hist = PROV.history(pr)
    assert len(hist) == 2
    assert {h["file"] for h in hist} == {"crew6.csv", "crew7.csv"}
    assert sorted(h["points"] for h in hist) == [2, 3]
    assert "5 point(s)" in PROV.summary(pr) and "2 source file(s)" in PROV.summary(pr)


def test_a_point_with_no_record_says_nothing_rather_than_guessing(tmp_path):
    pr = _project()
    pr.add_point(BASE_E, BASE_N, 500.0, number="1", desc="GS")
    assert PROV.of(pr.point_by_number("1")) == {"file": "", "folder": "", "set": "", "when": ""}
    assert PROV.describe(pr.point_by_number("1")) == ""
    assert PROV.value(pr.point_by_number("1"), "source_file") == ""
    assert PROV.summary(pr).startswith("No imported points")
    assert PROV.history(pr) == []


# --------------------------------------------------------------------------------------------- the columns
def test_the_point_list_shows_the_two_columns_the_working_file_has_always_carried(win, app, auto):
    """Both on by default, in the order the working file keeps them, and switchable."""
    m = win.points.model
    assert m.headers()[-2:] == ["Source File", "Parent Folder"]
    assert m.columnCount() == 8

    win.points.show_column("imported", True)
    assert m.headers()[-1] == "Imported" and m.columnCount() == 9
    assert settings().get("point_columns")[-1] == "imported", "the choice is remembered"

    win.points.show_column("source_folder", False)
    assert "Parent Folder" not in m.headers() and "Source File" in m.headers()
    assert m.columnCount() == 8
    win.points.show_column("imported", False)
    assert m.columnCount() == 7 and settings().get("point_columns") == ["source_file"]

    assert win.points.col_acts["source_folder"].isChecked() is False, "the menu agrees with the list"


def test_a_source_cell_is_a_fact_not_an_edit(win, app, auto):
    """Nobody edits where a point came from; the cell is there to be read and clicked."""
    pr = win.state.project
    p = pr.point_by_number("1") or pr.points[list(pr.points)[0]]
    PROV.stamp_one(p, file="crew6.csv", folder="Week 1/Crew 6", set="Cleaned field data")
    m = win.points.model
    row = m.ids.index(p.id)
    col = m.headers().index("Source File")
    idx = m.index(row, col)
    assert m.data(idx) == "crew6.csv"
    assert not (m.flags(idx) & Qt.ItemIsEditable), "a source cell is a fact, not a field"
    assert m.setData(idx, "somebody else's file") is False
    assert m.data(idx) == "crew6.csv"
    assert m.data(m.index(row, col), Qt.ToolTipRole) == PROV.describe(p)


def test_the_filter_finds_a_crews_points_and_the_sort_keys_are_numbers_not_strings(win, app, auto):
    """A source column is as filterable and sortable as any other - that is the point of a column."""
    pr = win.state.project
    ids = list(pr.points)
    for i, pid in enumerate(ids):
        PROV.stamp_one(pr.points[pid], file=("crew6.csv" if i < 2 else "crew7.csv"),
                       folder="Week 1")
    win.points.reload()
    win.points.ed.setText("crew6")
    pump(app, 3)
    assert win.points.proxy.rowCount() == 2
    win.points.ed.clear()
    pump(app, 3)
    assert win.points.proxy.rowCount() == len(ids)

    m = win.points.model
    assert m.data(m.index(0, 0), Qt.UserRole) is not None, "sort key for the point number"
    assert m.data(m.index(0, m.headers().index("Source File")), Qt.UserRole) == "crew6.csv", \
        "text sorts as text"


def test_an_import_through_the_window_fills_the_columns(tmp_path, win, app, auto):
    """The end of the line: a CSV imported in the drawing window shows up in its own column."""
    src = tmp_path / "Field Data" / "Week 1" / "Crew 9" / "crew9.csv"
    src.parent.mkdir(parents=True)
    _csv(src, [(901, BASE_N, BASE_E, 500.0, "GS"), (902, BASE_N + 5, BASE_E + 5, 500.2, "EP B")])
    win.importer.import_path(str(src))
    pump(app, 3)

    points = [p for p in win.state.project.points.values() if p.number in ("901", "902")]
    assert len(points) == 2
    assert {PROV.of(p)["file"] for p in points} == {"crew9.csv"}
    folder = {PROV.of(p)["folder"] for p in points}
    assert len(folder) == 1 and next(iter(folder)).endswith("Field Data/Week 1/Crew 9")
    row = win.points.model.ids.index(points[0].id)
    assert win.points.model.data(win.points.model.index(row, 6)).endswith("crew9.csv")


def test_the_column_menu_is_on_the_headers_and_in_the_view_menu(win, app, auto):
    """"Show/hide" means right where the columns are, and where every other panel is."""
    labels = [a.text() for a in win.points.col_menu.actions()]
    assert labels == ["Source File", "Parent Folder", "Imported", "Import"]
    view = next((m for m in getattr(win, "_top_menus", []) if m.title() == "&View"), None) or {a.text(): a.menu() for a in list(win.menuBar().actions()) if a.menu()}["&View"]
    panels = next((m for m in getattr(win, "_all_menus", []) if m.title() == "&Panels"), None) or {a.text(): a.menu() for a in list(view.actions()) if a.menu()}["&Panels"]
    assert win.points.col_menu in [a.menu() for a in panels.actions() if a.menu()]


# --------------------------------------------------------------------------------------------- the dock
def test_the_points_dock_is_tabbed_with_groups_in_left_panel(win, app, auto):
    assert win.d_pts in win.tabifiedDockWidgets(win.d_groups), "points is tabbed in left panel with groups"


def test_the_dock_runs_the_checks_on_the_projects_points_and_says_how_many(win, app, auto):
    from plumbline.ui.check_dock import CheckFieldworkDock
    dock = CheckFieldworkDock(win.state, job_root=win._job_folder)
    dock.run()
    pump(app, 2)
    assert dock.result["stats"]["rows"] == len(win.state.project.points)
    assert dock.tbl.rowCount() == len(dock.result["findings"])
    assert "point(s) in the check" in dock.lbl_when.text()


def test_the_dock_finds_a_duplicate_number_and_selects_the_points_it_names(win, app, auto):
    from plumbline.ui.check_dock import CheckFieldworkDock
    dock = CheckFieldworkDock(win.state, job_root=win._job_folder)
    pr = win.state.project
    rows = [FB.working_row(1, "9001", BASE_N, BASE_E, 500.0, "GS"),
            FB.working_row(2, "9001", BASE_N + 40, BASE_E + 40, 500.5, "GS")]
    with win.state.edit("test points"):
        FB.apply_rows_to_project(pr, rows, dup_policy="keep")
    dock.run()
    pump(app, 2)

    finding = next(f for f in dock.result["findings"] if f["check"] == "duplicate numbers")
    ids = [dock.result["ids"][i] for i in finding["rows"]]
    assert len(ids) == 2
    assert {pr.points[i].number for i in ids} == {"9001"}

    row = dock.result["findings"].index(finding)
    dock.tbl.selectRow(row)
    pump(app, 2)
    assert win.state.sel_points == set(ids), "clicking a finding selects those points in the drawing"


def test_the_dock_says_when_the_check_is_out_of_date_and_button_forces_a_rerun(
        win, app, auto, monkeypatch):
    from plumbline.ui.check_dock import CheckFieldworkDock

    calls = []
    real_check = FB.check_project

    def counted_check(*args, **kwargs):
        calls.append(1)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(FB, "check_project", counted_check)
    dock = CheckFieldworkDock(win.state, job_root=win._job_folder)
    dock.run(store=False)
    count_after_check = len(calls)
    dock.run(store=False)
    assert len(calls) == count_after_check, "automatic unchanged-input checks reuse the cache"

    win.state.changed.emit({"points"})
    pump(app, 2)
    assert dock.btn_run.text().endswith("*")
    dock.run(store=False)
    assert not dock.btn_run.text().endswith("*")

    dock.alert_if_no_fieldbook = lambda: None  # Avoid a modal vocabulary prompt in this button test.
    dock.btn_run.click()
    assert len(calls) == count_after_check + 1, "the explicit Run button computes and refreshes the cache"


def _write_fieldbook(path: Path, codes):
    from plumbline.fieldwork.io_carlson import write_fwb_file
    path.parent.mkdir(parents=True, exist_ok=True)
    write_fwb_file(path, ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"],
                   [[c, c, "cross", "V-SITE", "1", ""] for c in codes],
                   commands=["ST", "PC", "PT", "END"], rules=[])


def test_without_a_field_book_the_dock_runs_the_other_checks_and_offers_the_two_ways_out(win, app, auto):
    """The failure this dock exists to prevent: a check that quietly cannot run."""
    from plumbline.ui.check_dock import CheckFieldworkDock

    win.state.project.settings.pop("f2f_path", None)
    dock = CheckFieldworkDock(win.state, job_root=lambda: None)
    assert dock.code_set == set()
    assert "No field book" in dock.banner.text()
    assert "description and line checks cannot" in dock.banner.text()

    rows = [FB.working_row(1, "9002", BASE_N, BASE_E, 500.0, "ZZ TOP"),
            FB.working_row(2, "9002", BASE_N + 30, BASE_E + 30, 500.5, "ZZ TOP")]
    with win.state.edit("test points"):
        FB.apply_rows_to_project(win.state.project, rows, dup_policy="keep")
    dock.run()
    checks = {f["check"] for f in dock.result["findings"]}
    assert "duplicate numbers" in checks
    assert "Unknown code" not in checks, "no vocabulary, no description check - not 300 false alarms"

    dock.alert_if_no_fieldbook()
    assert any("No field book is loaded" in b for b in auto["boxes"]), "and it offers to fix it"
    dock.alert_if_no_fieldbook()
    assert sum(1 for b in auto["boxes"] if "No field book is loaded" in b) == 1, "once, not every press"


def test_with_a_field_book_in_the_job_folder_the_description_check_runs(win, app, auto, tmp_path):
    from plumbline.ui.check_dock import CheckFieldworkDock

    job = tmp_path / "23-036 Elm"
    book = job / "Field Book" / "office.fwb"
    _write_fieldbook(book, ["GS", "EP"])
    win.state.project.settings.pop("f2f_path", None)

    dock = CheckFieldworkDock(win.state, job_root=lambda: job)
    assert dock.fieldbook() == book
    assert dock.code_set == {"gs", "ep"}
    assert "All five checks run" in dock.banner.text()   # numbers, positions, descriptions, lines

    rows = [FB.working_row(1, "9003", BASE_N, BASE_E, 500.0, "GS"),
            FB.working_row(2, "9003", BASE_N + 20, BASE_E + 20, 500.5, "XYZZY")]
    with win.state.edit("test points"):
        FB.apply_rows_to_project(win.state.project, rows, dup_policy="keep")
    dock.run()
    flagged = {dock.result["ids"][i] for f in dock.result["findings"]
               if f.get("flag") == "UnknownCode" for i in f["rows"]}
    mine = {p.id for p in win.state.project.points.values() if p.desc == "XYZZY"}
    assert mine and mine <= flagged, "the code the field book does not have is flagged"
    assert dock.result["code_checks"] is True
    # every other sample code is flagged too - the test field book only has GS and EP - but the
    # point whose description IS in the field book is not
    assert {p.id for p in win.state.project.points.values() if p.desc == "GS"} - flagged


def test_the_docks_checks_are_the_field_windows_checks(win, app, auto):
    """One implementation, two windows: the same rows through either door say the same thing."""
    rows = [FB.working_row(1, "9004", BASE_N, BASE_E, 500.0, "GS"),
            FB.working_row(2, "9004", BASE_N + 10, BASE_E + 10, 500.1, "EP"),
            FB.working_row(3, "9005", BASE_N + 1e5, BASE_E + 1e5, 500.2, "GS")]
    with win.state.edit("test points"):
        FB.apply_rows_to_project(win.state.project, rows, dup_policy="keep")
    mine = {p.id for p in win.state.project.points.values() if p.number == "9004"}

    field_side = FB.run_checks(rows)
    dock_side = FB.check_project(win.state.project)
    # the pair the field window flags in its own row space is the pair the dock flags in the job
    assert [len(g) for g in field_side["exact"]] == [2]
    dup = next(f for f in dock_side["findings"] if f["check"] == "duplicate numbers")
    assert {dock_side["ids"][i] for i in dup["rows"]} == mine
    assert "9004" in dup["message"]
