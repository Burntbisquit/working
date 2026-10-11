"""Reference layers (item 12): a CSV of stake-out / control / other points is not field data.

A CSV that arrives in the office is usually somebody else's coordinates - a stake-out list from
the engineer, published control, a design file.  It has to draw, snap and export with everything
else, but it must not join the fieldwork list, must not be checked as if this job had shot it, and
must not have its point numbers renumbered against ours.  One layer per role, checked here.
"""
import contextlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
pytest.importorskip("PySide6")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plumbline.core import crs as C                                     # noqa: E402
from plumbline.core import reference as REF                              # noqa: E402
from plumbline.core.qa import run_checks                                 # noqa: E402
from plumbline.core.project import Project                               # noqa: E402
from plumbline.io import csv_points as CSV                               # noqa: E402
from plumbline.ui.import_export import (ImportPlan, ImportOptionsPanel, CsvImportDialog,  # noqa: E402
                                       apply_import, reference_folder_files, describe_folder_import)

# Mesquite, Texas - inside the North Central zone, and a believable place for a job
BASE_E, BASE_N = 2552700.0, 6967100.0


# --------------------------------------------------------------------------- a state to import into
class _State:
    """The smallest thing apply_import() needs: a project, an edit step, a log and a zoom."""

    def __init__(self, project):
        self.project = project
        self.sel_points: set = set()
        self.sel_entities: set = set()
        self.logged: list = []
        self.zoomed = 0

    @contextlib.contextmanager
    def edit(self, label, kinds=("all",)):
        yield self

    def log(self, text, level="info"):
        self.logged.append((level, text))

    def zoom_extents(self):
        self.zoomed += 1

    def changed_(self, *a, **k):
        pass


def _project():
    pr = Project("Reference test", C.ProjectCRS.from_key(6584))
    return pr


def _csv(path: Path, rows, header="PT,NORTHING,EASTING,ELEVATION,DESCRIPTION"):
    text = "\n".join([header] + [",".join(str(c) for c in r) for r in rows]) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _mapping(path):
    sn = CSV.sniff(path)
    return CSV.CsvMapping(sn.delimiter, 1 if sn.has_header else 0, list(sn.roles))


def _import_csv(state, path, role=None, dup_policy="renumber"):
    batch = CSV.read_points(path, _mapping(path))
    plan = ImportPlan(reference_role=role, dup_policy=dup_policy)
    return apply_import(state, batch, plan, f"points ({Path(path).name})")


def test_import_never_processes_linework_implicitly(tmp_path):
    project = _project()
    state = _State(project)
    source = _csv(tmp_path / "field.csv", [(1, BASE_N, BASE_E, 500.0, "EP ST")])
    calls = []
    project.process_linework = lambda: calls.append("processed")

    _import_csv(state, source)

    assert calls == []
    assert not hasattr(ImportPlan(), "process_linework")


# --------------------------------------------------------------------------- core: the lists
def test_a_stakeout_csv_lands_on_its_own_layer_and_not_in_the_fieldwork_list(tmp_path):
    pr = _project()
    pr.add_point(BASE_E, BASE_N, 500.0, number="1", desc="GS")
    st = _State(pr)
    f = _csv(tmp_path / "stakeout.csv", [(1, BASE_N + 50, BASE_E + 50, 501.5, "STAKE"),
                                         (2, BASE_N + 60, BASE_E + 60, 501.0, "STAKE")])
    stats = _import_csv(st, f, role="stakeout")

    assert stats["points"] == 2
    assert stats["duplicates"] == 0, "the stake-out list has its own numbering - point 1 is not a duplicate"
    assert [p.number for p in REF.reference_points(pr, "stakeout")] == ["1", "2"]
    assert [p.layer for p in REF.reference_points(pr, "stakeout")] == ["STAKE-OUT", "STAKE-OUT"]
    assert [p.number for p in REF.survey_points(pr)] == ["1"], "the field point is untouched"
    assert "STAKE-OUT" in pr.layers, "the role's layer is created in the project"
    assert REF.role_of(pr.point_by_number("1")) is None, "the field point 1 is still field data"


def test_the_three_roles_keep_their_own_layers_and_their_own_numbers(tmp_path):
    pr = _project()
    st = _State(pr)
    f = _csv(tmp_path / "both.csv", [(1, BASE_N, BASE_E, 500.0, "P"),
                                     (2, BASE_N + 10, BASE_E + 10, 501.0, "P")])
    _import_csv(st, f, role="stakeout")
    _import_csv(st, f, role="control")
    _import_csv(st, f, role="other")

    got = {(REF.role_of(p), p.layer) for p in pr.points.values()}
    assert got == {("stakeout", "STAKE-OUT"), ("control", "CONTROL"), ("other", "OTHER")}
    assert len(pr.points) == 6, "six points, three separate lists of two"
    assert REF.counts(pr)["survey"] == 0
    assert REF.describe(pr) == "Reference: 2 stake-out, 2 control, 2 other"


def test_a_plain_import_still_renumbers_against_the_fieldwork_list(tmp_path):
    """The old behaviour is intact: a field-data CSV renumbers against our own points."""
    pr = _project()
    pr.add_point(BASE_E, BASE_N, 500.0, number="1", desc="GS")
    st = _State(pr)
    f = _csv(tmp_path / "field.csv", [(1, BASE_N + 50, BASE_E + 50, 501.5, "EP")])
    stats = _import_csv(st, f)                     # no role -> field data

    assert stats["duplicates"] == 1 and stats["renumbered"] == 1
    assert [p.number for p in REF.survey_points(pr)] == ["1", "2"]
    assert REF.counts(pr) == {"stakeout": 0, "control": 0, "other": 0, "survey": 2}


def test_reference_numbers_never_borrow_from_the_fields_numbering(tmp_path):
    """A stake-out list numbered 1..3 next to field points numbered 1..3 stays 1..3."""
    pr = _project()
    for i in (1, 2, 3):
        pr.add_point(BASE_E + i, BASE_N + i, 500.0, number=str(i), desc="EP")
    st = _State(pr)
    f = _csv(tmp_path / "stake.csv", [(i, BASE_N + 100 * i, BASE_E + 100 * i, 500.0, "STAKE")
                                      for i in (1, 2, 3)])
    stats = _import_csv(st, f, role="stakeout")
    assert stats["renumbered"] == 0 and stats["duplicates"] == 0
    assert sorted(p.number for p in REF.reference_points(pr, "stakeout")) == ["1", "2", "3"]
    assert sorted(p.number for p in REF.survey_points(pr)) == ["1", "2", "3"]


def test_the_point_tool_numbers_a_new_reference_point_in_the_roles_own_space(tmp_path):
    pr = _project()
    st = _State(pr)
    f = _csv(tmp_path / "stake.csv", [(1, BASE_N, BASE_E, 500.0, "STAKE"),
                                      (2, BASE_N + 10, BASE_E + 10, 500.0, "STAKE")])
    _import_csv(st, f, role="stakeout")
    assert REF.next_number(pr, "stakeout") == "3", "the role's own list decides the next number"
    assert REF.next_number(pr, "control") == "1"
    p = REF.add_one(pr, BASE_E + 20, BASE_N + 20, 499.0, number=REF.next_number(pr, "stakeout"),
                    desc="STAKE", role="stakeout")
    assert (p.number, p.layer) == ("3", "STAKE-OUT")


def test_the_data_quality_report_checks_the_field_data_and_names_what_it_set_aside(tmp_path):
    pr = _project()
    # two field points sharing a number: a real error, and one the report must still catch
    pr.add_point(BASE_E, BASE_N, 500.0, number="7", desc="EP")
    pr.add_point(BASE_E + 10, BASE_N + 10, 500.0, number="7", desc="EP")
    st = _State(pr)
    f = _csv(tmp_path / "control.csv", [(7, BASE_N + 900, BASE_E + 900, 400.0, "PUBLIC CONTROL")])
    _import_csv(st, f, role="control")

    issues = run_checks(pr)
    kinds = [i.kind for i in issues]
    assert "duplicate-number" in kinds, "the field duplicate is still an error"
    dup = next(i for i in issues if i.kind == "duplicate-number")
    assert len(dup.point_ids) == 2, "the control point numbered 7 is not part of that duplicate"
    note = next(i for i in issues if i.kind == "reference")
    assert "1 reference point" in note.message and "not this job's field data" in note.message


def test_a_project_with_only_reference_points_says_so(tmp_path):
    pr = _project()
    st = _State(pr)
    f = _csv(tmp_path / "control.csv", [(1, BASE_N, BASE_E, 400.0, "CONTROL")])
    _import_csv(st, f, role="control")
    issues = run_checks(pr)
    assert any(i.kind == "empty" and "no field points" in i.message for i in issues)
    assert any(i.kind == "reference" for i in issues)


# --------------------------------------------------------------------------- a folder of files
def test_a_folder_of_point_files_is_read_in_the_order_a_person_reads_it(tmp_path):
    for name in ("crew 10.csv", "crew 2.csv", "crew 9.txt", "notes.pdf", "Job Setup.txt"):
        (tmp_path / name).write_text("PT,N,E,Z\n1,1,1,1\n", encoding="utf-8")
    # Sub-folders are now included by default - create subfolder first
    sub = tmp_path / "week 2"
    sub.mkdir()
    (sub / "crew 6.csv").write_text("PT,N,E,Z\n1,1,1,1\n", encoding="utf-8")
    # Now get the list - subfolder file should be included
    got = [p.name for p in reference_folder_files(tmp_path)]
    # Original crew files plus the subfolder file (included by default)
    assert got == ["crew 2.csv", "crew 9.txt", "crew 10.csv", "Job Setup.txt", "crew 6.csv"]

    # Also verify explicit recursive gives same result (subfolder file already included by default)
    assert [p.name for p in reference_folder_files(tmp_path, recursive=True)] == got


def test_importing_a_folder_puts_every_file_on_the_roles_layer_and_records_the_folder(tmp_path):
    from plumbline.ui.import_export import Importer

    pr = _project()
    st = _State(pr)
    for i, name in enumerate(("crew 2.csv", "crew 10.csv"), start=1):
        _csv(tmp_path / name, [(i, BASE_N + i, BASE_E + i, 500.0 + i, "STAKE")])
    (tmp_path / "readme.txt").write_text("nothing to see here\n", encoding="utf-8")

    class _Win:
        state = st
        _job_root_choice = None

    tally = Importer(_Win()).import_reference_folder(tmp_path, "stakeout", recursive=False)
    assert tally["files"] == 2 and tally["points"] == 2
    assert "2 point(s) from 2 file(s)" in describe_folder_import(tally)
    assert [p.layer for p in pr.points.values()] == ["STAKE-OUT", "STAKE-OUT"]
    assert pr.settings["data_folder"] == str(tmp_path), "the folder is recorded on the job"
    assert pr.settings["reference_imports"][-1]["role"] == "stakeout"
    assert pr.settings["reference_imports"][-1]["points"] == 2
    assert st.zoomed == 1


def test_a_file_that_cannot_be_read_is_skipped_and_named_not_guessed(tmp_path):
    from plumbline.ui.import_export import Importer

    pr = _project()
    st = _State(pr)
    _csv(tmp_path / "good.csv", [(1, BASE_N, BASE_E, 500.0, "STAKE")])
    (tmp_path / "swapped.csv").write_text("PT,LAT,LON\n", encoding="utf-8")     # no columns to read

    class _Win:
        state = st
        _job_root_choice = None

    tally = Importer(_Win()).import_reference_folder(tmp_path, "stakeout")
    assert tally["points"] == 1 and tally["failed"] == 1
    assert [n for n, _ in tally["unreadable"]] == ["swapped.csv"]
    assert any("swapped.csv" in text for _lvl, text in st.logged)


# --------------------------------------------------------------------------- the interface
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_the_import_dialog_offers_the_three_roles_and_a_way_in_for_field_data(tmp_path, qapp):
    from PySide6.QtWidgets import QDialogButtonBox
    from plumbline.ui.app_state import AppState

    st = AppState(_project())
    f = _csv(tmp_path / "stake.csv", [(1, BASE_N, BASE_E, 500.0, "STAKE")])
    dlg = CsvImportDialog(st, f, None)
    labels = [dlg.cmb_role.itemText(i) for i in range(dlg.cmb_role.count())]
    # A CSV is this job's field data by default (change order, item 6); a reference role is one
    # click away and each role still has its own layer and its own point numbers.
    assert dlg.target_role() is None, "a CSV is this job's field data unless the user says otherwise"
    assert labels[0].startswith("This job's field data")
    assert labels[0].startswith("This job's field data")
    assert "Stake-out - reference layer STAKE-OUT" in labels
    assert "Control - reference layer CONTROL" in labels
    assert "Other reference - reference layer OTHER" in labels
    assert dlg.cmb_role.itemData(0) is None
    assert dlg.bb.button(QDialogButtonBox.Ok).text() == "Import Points"

    control_dlg = CsvImportDialog(st, f, None, default_role="control")
    assert control_dlg.target_role() == "control"

    dlg.cmb_role.setCurrentIndex(dlg.cmb_role.findData("stakeout"))
    assert dlg.target_role() == "stakeout"
    assert not dlg.panel.ed_layer.isEnabled(), "the role owns the layer, so the layer box steps aside"
    assert not hasattr(dlg.panel, "chk_lw"), "imports never process linework automatically"
    assert "STAKE-OUT" in dlg.status.text() or "stake-out" in dlg.status.text().lower()

    dlg.cmb_role.setCurrentIndex(0)                                  # this job's field data
    assert dlg.target_role() is None
    assert dlg.panel.ed_layer.isEnabled()


def test_the_export_dialog_offers_each_list_with_its_count(tmp_path, qapp):
    from plumbline.ui.app_state import AppState
    from plumbline.ui.export_dialogs import CsvExportDialog

    pr = _project()
    for i in (1, 2):
        pr.add_point(BASE_E + i, BASE_N + i, 500.0, number=str(i), desc="EP")
    st = _State(pr)
    f = _csv(tmp_path / "stake.csv", [(1, BASE_N, BASE_E, 500.0, "STAKE")])
    _import_csv(st, f, role="stakeout")

    st2 = AppState(pr)
    dlg = CsvExportDialog(st2, None)
    items = [dlg.cmb_src.itemText(i) for i in range(dlg.cmb_src.count())]
    assert "This job's field data  (2)" in items
    assert "Stake-out points  (1)" in items
    assert "Control points  (0)" in items
    assert dlg.source() is None and dlg.source_word() == "field points"
    dlg.cmb_src.setCurrentIndex(1)
    assert dlg.source() == "stakeout" and dlg.source_word() == "stake-out reference points"
    assert [p.number for p in REF.select(pr, dlg.source())] == ["1"]
    assert len(REF.select(pr, "all")) == 3


def test_the_points_of_a_role_can_be_written_and_read_back_as_that_role(tmp_path, qapp):
    """Round trip: export the stake-out list, import it again as stake-out, same layer and numbers."""
    from plumbline.ui.app_state import AppState
    from plumbline.ui.export_dialogs import CsvExportDialog

    pr = _project()
    st = _State(pr)
    f = _csv(tmp_path / "stake.csv", [(4, BASE_N + 40, BASE_E + 40, 512.25, "STAKE CURB"),
                                      (5, BASE_N + 50, BASE_E + 50, 512.75, "STAKE CURB")])
    _import_csv(st, f, role="stakeout")

    out = tmp_path / "export.csv"
    dlg = CsvExportDialog(AppState(pr), None)
    dlg.cmb_src.setCurrentIndex(1)                                  # stake-out points
    cols = tuple(dlg.cmb_fmt.currentData()) if "number" in (dlg.cmb_fmt.currentData() or ()) else \
        ("number", "northing", "easting", "elevation", "description")
    n = CSV.write_points_csv(str(out), REF.reference_points(pr, "stakeout"), cols, ",", True, 3)
    assert n == 2

    pr2 = _project()
    st2 = _State(pr2)
    stats = _import_csv(st2, out, role="stakeout")
    assert stats["points"] == 2 and stats["renumbered"] == 0
    back = [(p.number, round(p.z, 3), p.desc, p.layer) for p in REF.reference_points(pr2, "stakeout")]
    assert back == [("4", 512.25, "STAKE CURB", "STAKE-OUT"), ("5", 512.75, "STAKE CURB", "STAKE-OUT")]


def test_reference_folder_dialog_lists_and_filters_csvs(tmp_path, qapp):
    from plumbline.ui.app_state import AppState
    from plumbline.ui.import_export import Importer, ReferenceFolderDialog

    # Setup folder with multiple CSV files across subfolders
    (tmp_path / "Crew 1").mkdir()
    (tmp_path / "Crew 2").mkdir()
    f1 = _csv(tmp_path / "Crew 1" / "crew1.csv", [(1, BASE_N + 1, BASE_E + 1, 501.0, "CP")])
    f2 = _csv(tmp_path / "Crew 2" / "crew2.csv", [(2, BASE_N + 2, BASE_E + 2, 502.0, "CP")])
    f3 = _csv(tmp_path / "root.csv", [(3, BASE_N + 3, BASE_E + 3, 503.0, "CP")])

    st = AppState(_project())
    dlg = ReferenceFolderDialog(st, tmp_path, None)

    # All 3 files listed in the table
    assert len(dlg.files()) == 3
    assert dlg.chk_table.rowCount() == 3
    assert len(dlg.selected_files()) == 3
    assert "3 of 3 selected" in dlg.lbl_file_count.text()

    # Deselect one file
    dlg._checkboxes[0].setChecked(False)
    assert len(dlg.selected_files()) == 2
    assert "2 of 3 selected" in dlg.lbl_file_count.text()

    # Select none
    dlg._select_none()
    assert len(dlg.selected_files()) == 0
    assert "0 of 3 selected" in dlg.lbl_file_count.text()
    assert dlg.validate() == "Please select at least one file to import."

    # Select all
    dlg._select_all()
    assert len(dlg.selected_files()) == 3
    assert dlg.validate() is None

    # Partial import using selected_files
    selected = [f1, f3]
    class _Win:
        state = st
        _job_root_choice = None
    tally = Importer(_Win()).import_reference_folder(tmp_path, "control", selected_files=selected)
    assert tally["files"] == 2
    assert tally["points"] == 2



def test_field_data_import_is_staged_and_same_folder_file_point_count_is_greyed(tmp_path, qapp):
    from PySide6.QtCore import Qt
    from plumbline.ui.app_state import AppState
    from plumbline.ui.import_export import Importer, ReferenceFolderDialog

    source_root = tmp_path / "Downloads" / "Crew 1"
    source_root.mkdir(parents=True)
    source = _csv(source_root / "crew1.csv", [(7, BASE_N + 7, BASE_E + 7, 507.0, "EP")])
    job_root = tmp_path / "Job"
    job_root.mkdir()
    state = AppState(_project())
    state.project.path = str(job_root / "Job.plb")

    class _Win:
        def __init__(self, app_state):
            self.state = app_state

        def _job_folder(self):
            return job_root

    tally = Importer(_Win(state)).import_reference_folder(
        source_root, None, recursive=True, selected_files=[source])
    stored = job_root / "Field Data" / source.name
    assert tally["points"] == 1 and stored.is_file()
    assert state.project.settings["data_folder"] == str(job_root / "Field Data")
    manifest = state.project.settings["field_data_imports"][0]
    assert manifest["source_folder"] == "crew 1"
    assert manifest["source_file"] == "crew1.csv"
    assert manifest["point_count"] == 1
    assert "size" not in manifest

    dlg = ReferenceFolderDialog(state, source_root, None, field_data_only=True)
    assert [dlg.chk_table.horizontalHeaderItem(i).text() for i in range(4)] == [
        "", "File", "Subfolder / Path", "Points",
    ]
    assert dlg._file_point_counts == [1]
    assert dlg._imported_flags == [True]
    assert dlg._checkboxes[0].isEnabled() is False
    assert dlg.chk_table.cellWidget(0, 0).isEnabled() is False
    assert all(not (dlg.chk_table.item(0, col).flags() & Qt.ItemFlag.ItemIsEnabled)
               for col in range(1, 4))
    assert dlg.selected_files() == []

    # The disabled/grey state survives save and reopen, and also matches the packaged Field Data copy.
    state.project.save()
    reopened_state = AppState(Project.load(state.project.path))
    reopened_dlg = ReferenceFolderDialog(reopened_state, source_root, None, field_data_only=True)
    assert reopened_dlg._imported_flags == [True]
    stored_dlg = ReferenceFolderDialog(
        state, job_root / "Field Data", None, field_data_only=True)
    assert stored_dlg._imported_flags == [True]

    # Same path, but the recheck now parses two points instead of the one originally imported.
    source.write_text(
        "PT,NORTHING,EASTING,ELEVATION,DESCRIPTION\n"
        f"7,{BASE_N + 7},{BASE_E + 7},507.0,EP\n"
        f"8,{BASE_N + 8},{BASE_E + 8},508.0,EP\n",
        encoding="utf-8")
    dlg._refresh_files()
    assert dlg._file_point_counts == [2]
    assert dlg._imported_flags == [False]
    assert dlg._checkboxes[0].isEnabled() is True
    assert dlg.selected_files() == [source]

    # The manifest shares one undo step with its imported points.
    assert state.undo().startswith("Import points")
    assert not state.project.points
    assert state.project.settings.get("field_data_imports", []) == []


def test_legacy_field_data_manifest_rechecks_the_stored_file_point_count(tmp_path, qapp):
    from plumbline.ui.app_state import AppState
    from plumbline.ui.import_export import ReferenceFolderDialog

    source_root = tmp_path / "Downloads" / "Crew 1"
    source_root.mkdir(parents=True)
    source = _csv(source_root / "crew1.csv", [(7, BASE_N + 7, BASE_E + 7, 507.0, "EP")])
    job_root = tmp_path / "Job"
    stored_root = job_root / "Field Data"
    stored_root.mkdir(parents=True)
    stored = _csv(stored_root / source.name, [(7, BASE_N + 7, BASE_E + 7, 507.0, "EP")])

    state = AppState(_project())
    state.project.path = str(job_root / "Job.plb")
    state.project.settings["data_folder"] = str(stored_root)
    state.project.settings["field_data_imports"] = [{
        "source_relative": "crew 1/crew1.csv",
        "stored_relative": "field data/crew1.csv",
        "size": source.stat().st_size,
    }]

    dlg = ReferenceFolderDialog(state, source_root, None, field_data_only=True)
    assert dlg._file_point_counts == [1]
    assert dlg._imported_flags == [True]

    source.write_text(
        "PT,NORTHING,EASTING,ELEVATION,DESCRIPTION\n"
        f"7,{BASE_N + 7},{BASE_E + 7},507.0,EP\n"
        f"8,{BASE_N + 8},{BASE_E + 8},508.0,EP\n",
        encoding="utf-8")
    dlg._refresh_files()
    assert dlg._file_point_counts == [2]
    assert dlg._imported_flags == [False]


def test_field_data_recheck_does_not_guess_parsed_count_from_point_provenance(tmp_path, qapp):
    from plumbline.ui.app_state import AppState
    from plumbline.ui.import_export import ReferenceFolderDialog

    source_root = tmp_path / "Downloads" / "Crew 2"
    source_root.mkdir(parents=True)
    source = _csv(source_root / "legacy.csv", [(11, BASE_N + 11, BASE_E + 11, 511.0, "EP")])
    state = AppState(_project())
    point = state.project.add_point(BASE_E + 11, BASE_N + 11, 511.0,
                                    number="11", desc="EP")
    point.attrs["import"] = {
        "file": source.name,
        "folder": str(source_root),
        "set": "points",
        "when": "2026-10-10T12:00:00",
    }

    # Provenance identifies the source path, but cannot prove the original parsed count once
    # there is no manifest/staged copy. Do not grey a file by guessing from points still present.
    dlg = ReferenceFolderDialog(state, source_root, None, field_data_only=True)
    assert dlg._file_point_counts == [1]
    assert dlg._imported_flags == [False]

    source.write_text(
        "PT,NORTHING,EASTING,ELEVATION,DESCRIPTION\n"
        f"11,{BASE_N + 11},{BASE_E + 11},511.0,EP\n"
        f"12,{BASE_N + 12},{BASE_E + 12},512.0,EP\n",
        encoding="utf-8")
    dlg._refresh_files()
    assert dlg._file_point_counts == [2]
    assert dlg._imported_flags == [False]


def test_first_field_data_import_resolves_job_root_after_save_as(tmp_path, qapp):
    from plumbline.ui.app_state import AppState
    from plumbline.ui.import_export import Importer, ReferenceFolderDialog

    source_root = tmp_path / "Downloads" / "First Import"
    source_root.mkdir(parents=True)
    source = _csv(source_root / "survey.csv", [(8, BASE_N + 8, BASE_E + 8, 508.0, "EP")])
    job_root = tmp_path / "Saved Job"
    state = AppState(Project("Untitled", C.ProjectCRS.from_key(6584)))

    def _save_as_before_edit():
        job_root.mkdir()
        state.project.path = str(job_root / "Saved Job.plb")
        return True

    state._save_untitled_before_edit = _save_as_before_edit

    class _Win:
        def __init__(self, app_state):
            self.state = app_state

        def _job_folder(self):
            path = self.state.project.path
            return Path(path).parent if path else None

    tally = Importer(_Win(state)).import_reference_folder(
        source_root, None, recursive=True, selected_files=[source])
    stored = job_root / "Field Data" / source.name
    assert tally["points"] == 1 and stored.is_file()
    assert len(state.project.settings["field_data_imports"]) == 1
    record = state.project.settings["field_data_imports"][0]
    assert record["point_count"] == 1 and "size" not in record

    dlg = ReferenceFolderDialog(state, source_root, None, field_data_only=True)
    assert dlg._imported_flags == [True]
    assert dlg.selected_files() == []


def test_folder_points_preview_dialog(tmp_path, qapp):
    from plumbline.ui.import_export import FolderPointsPreviewDialog

    (tmp_path / "Sub").mkdir()
    f1 = _csv(tmp_path / "Sub" / "crew1.csv", [
        (101, BASE_N + 10, BASE_E + 20, 510.5, "TREE 12IN"),
        (102, BASE_N + 30, BASE_E + 40, 511.0, "EP"),
    ])
    f2 = _csv(tmp_path / "control.csv", [
        (1, BASE_N, BASE_E, 500.0, "BM1"),
    ])

    dlg = FolderPointsPreviewDialog([f1, f2], None)
    assert len(dlg.all_points) == 3
    assert dlg.table.rowCount() == 3
    assert "3 points" in dlg.lbl_summary.text()

    # Test filtering points by description
    dlg.ed_filter.setText("TREE")
    assert dlg.table.rowCount() == 1
    assert dlg.table.item(0, 0).text() == "101"
    assert dlg.table.item(0, 4).text() == "TREE 12IN"

    # Test filtering by point number
    dlg.ed_filter.setText("1")
    # Matches '101', '102', and '1'
    assert dlg.table.rowCount() == 3

    # Clear filter
    dlg.ed_filter.setText("")
    assert dlg.table.rowCount() == 3
