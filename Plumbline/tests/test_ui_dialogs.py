"""Smoke / behaviour tests for the dialogs and menu actions that the bigger UI tests don't reach."""
import math

import pytest
pytest.importorskip("PySide6")
from PySide6.QtWidgets import (QDialog, QDockWidget, QFileDialog, QInputDialog, QMenu, QMessageBox,
                               QPushButton, QTableWidgetItem, QToolBar)

from plumbline.core import crs as C
from plumbline.core.project import Project
from plumbline.core.settings import settings
from test_ui import app, auto, pump, win  # noqa: F401  (fixtures)


def test_settings_dialog_applies_theme_and_values(win, app, auto, tmp_path):
    from plumbline.ui import theme
    from plumbline.ui.dialogs import SettingsDialog
    old_project_folder = settings().get("default_project_folder", "")
    win.settings_dialog()                                             # accepted unchanged: nothing breaks
    d = SettingsDialog(win)
    d.cmb_theme.setCurrentIndex(d.cmb_theme.findData("light"))
    d.cmb_order.setCurrentIndex(d.cmb_order.findData("XY"))
    d.sp_snap.setValue(20)
    project_folder = str(tmp_path / "Projects")
    d.ed_project_folder.setText(project_folder)
    assert d.chk_require_fieldbook.isChecked()
    d.chk_require_fieldbook.setChecked(False)
    d.apply()
    s = settings()
    assert s.get("theme") == "light" and s.get("coord_order") == "XY" and s.get("snap_px") == 20
    assert s.get("require_fieldbook_for_processing") is False
    assert s.get("default_project_folder") == project_folder
    win._apply_theme()
    assert theme.current() == "light"
    win.points.reload()
    assert win.points.model.headers()[1] == "Easting"                  # typed / displayed order follows the setting
    win.canvas.render_image(300)
    s.set("coord_order", "NE")
    s.set("require_fieldbook_for_processing", True)
    s.set("default_project_folder", old_project_folder)
    win.toggle_theme()                                                # back to dark


# ------------------------------------------------------------------ where things live in the menus
def _top_menus(win):
    return {m.title(): m for m in getattr(win, "_top_menus", [])} or {a.text(): a.menu() for a in win.menuBar().actions() if a.menu() is not None}


def test_file_coordinate_systems_has_only_the_four_requested_actions_and_set_is_metadata_only(win, app, auto):
    from plumbline.core import filecrs as FCRC, provenance as PROV
    from plumbline.core.crs import ProjectCRS
    from plumbline.ui.filecrs_dialog import FileCoordinateSystemsDialog

    project = Project("File CRS dialog", ProjectCRS.from_epsg(32615))
    point = project.add_point(500_000.0, 3_600_000.0, 12.0, number="1")
    PROV.stamp([point], file="survey.csv")
    initial = FCRC.make(key="EPSG:32614", label="Original import source")
    initial["source_crs"] = {"key": "EPSG:32613", "label": "Earlier source"}
    FCRC.record(project, "survey.csv", initial)
    win.state.set_project(project)

    dialog = FileCoordinateSystemsDialog(win.state, win)
    buttons = [button.text() for button in dialog.findChildren(QPushButton)]
    assert buttons == ["Set to Project", "Reproject", "Ground / Grid SAF", "Cancel"]
    dialog.tbl.selectRow(0)
    old_xy = (point.x, point.y)
    dialog.b_set.click()

    rec = FCRC.for_file(project, "survey.csv")
    assert (point.x, point.y) == old_xy
    assert rec["method"] == "project" and rec["key"] == project.crs.authority
    assert rec["source_crs"] == initial["source_crs"]
    assert win.state.undo_stack[-1][0] == "Set survey.csv to project system"
    dialog.b_cancel.click()
    assert dialog.result() == QDialog.Rejected


def test_file_reproject_dialog_uses_corrected_source_project_target_and_safs(win, app, auto, monkeypatch):
    import numpy as np
    from pyproj import Transformer

    from plumbline.core import filecrs as FCRC, provenance as PROV
    from plumbline.core.crs import GroundScale, ProjectCRS
    from plumbline.ui.filecrs_dialog import FileCoordinateSystemsDialog, _PickCRS

    source_ground = GroundScale(enabled=True, base_x=500_000.0, base_y=0.0, saf=1.00015)
    target_ground = GroundScale(enabled=True, base_x=200_000.0, base_y=0.0, saf=1.00008)
    source = ProjectCRS.from_epsg(32614, ground=source_ground)
    target = ProjectCRS.from_epsg(32615, ground=target_ground)
    project = Project("File CRS reprojection", target)
    to_source = Transformer.from_crs(4326, source.crs, always_xy=True)
    x, y = to_source.transform(-96.1, 32.8)
    point = project.add_point(x, y, 123.0, number="101")
    PROV.stamp([point], file="control.csv")
    FCRC.record(project, "control.csv", FCRC.make_from_project(project, method="project"))
    win.state.set_project(project)

    expected_x, expected_y = source.transform_to(target)(np.array([x]), np.array([y]))

    def choose_corrected_source(dialog):
        dialog.picker.select_key("EPSG:32614")
        dialog.extra.chk_ground.setChecked(True)
        dialog.extra.sp_by.setValue(0.0)
        dialog.extra.sp_bx.setValue(500_000.0)
        dialog.extra.sp_cf.setValue(1.00015)

    monkeypatch.setattr(_PickCRS, "_test_hook", choose_corrected_source, raising=False)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    dialog = FileCoordinateSystemsDialog(win.state, win)
    dialog.tbl.selectRow(0)
    dialog.reproject()

    assert (point.x, point.y) == pytest.approx((float(expected_x[0]), float(expected_y[0])), abs=1e-4)
    assert point.z == 123.0
    rec = FCRC.for_file(project, "control.csv")
    assert rec["method"] == "reprojected"
    assert rec["key"] == target.authority
    assert rec["source_crs"]["key"] == "EPSG:32614"
    assert rec["source_crs"]["saf"] == pytest.approx(1.00015)
    assert win.state.undo_stack[-1][0] == "Reproject control.csv to project system"


def test_coordinate_tools_are_in_survey_and_the_coordinates_menu_is_gone(win, app, auto):
    top = _top_menus(win)
    assert "&Coordinates" not in top
    survey = [a.text() for a in top["&Survey"].actions() if a.text()]
    assert "&Project Coordinate System..." in survey
    assert "Coordinate &Calculator..." in survey


def test_the_tools_menu_is_gone_and_its_items_moved_where_they_belong(win, app, auto):
    """A window and a folder are not tools.  The field-data half of the program opens from
    Survey, and the job folder is on File with the project it belongs to."""
    top = _top_menus(win)
    assert "&Tools" not in top
    survey = [a.text() for a in top["&Survey"].actions() if a.text()]
    assert survey[:2] == ["&Fieldwork Manager...", "Import &Cleaned Field Data..."]
    file_items = [a.text() for a in top["&File"].actions() if a.text()]
    # One folder entry, not two (change order, item 12): Select Job Folder and New Job Folder were
    # the same decision - where does this job live - asked at two different doors.
    assert "&Project Folder..." in file_items
    assert "Select &Job Folder..." not in file_items
    assert "New &Job Folder..." not in file_items
    assert "Open This &Job's Folder" not in file_items


def test_select_job_folder_points_the_program_at_a_job_with_no_project_yet(win, app, auto, monkeypatch, tmp_path):
    """A download can be sitting in a job folder before there is a project in it.  Choosing the
    folder is what tells the Fieldwork Manager where the job is."""
    from PySide6.QtGui import QDesktopServices
    from plumbline.ui.fieldwork_window import FieldworkBridge

    job = tmp_path / "23-036 Murchison"
    (job / "Field Data" / "Week 1" / "Crew 6").mkdir(parents=True)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(job)))
    win.select_job_folder()
    assert win._job_hint() == str(job)
    assert FieldworkBridge(win).job_root() == job

    # choosing the folder already in use does the other useful thing: opens it
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl",
                        staticmethod(lambda url: opened.append(url.toString()) or True))
    win.select_job_folder()
    assert opened and "Murchison" in opened[0]


def test_select_job_folder_offers_the_project_inside_the_folder(win, app, auto, monkeypatch, tmp_path):
    """If the folder holds a project, that is what the user meant - offer it, do not silently
    point the program at a folder while an open project sits in another one."""
    from plumbline.core.crs import ProjectCRS
    from plumbline.core.project import Project
    from plumbline.ui import main_window as MW

    job = tmp_path / "23-041 Elm Street"
    job.mkdir()
    pr = Project("Elm Street", ProjectCRS.from_key(6584))
    pr.add_point(2552700.0, 6967100.0, 500.0, number="1", desc="GS")
    pr.save(job / "Elm Street.plb")

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(job)))
    monkeypatch.setattr(MW, "confirm", lambda *a, **k: True)
    win.select_job_folder()
    assert win.state.project.name == "Elm Street" and win.state.project.crs.key == "6584"


def test_the_titles_are_in_title_case(win, app, auto):
    """Titles everywhere: menus, docks and toolbars.  Small words (and, of, to, from, the) stay
    lower case, acronyms keep their capitals, and nothing else sneaks in lower case."""
    small = {"a", "an", "the", "and", "but", "or", "nor", "for", "of", "to", "from", "in", "on",
             "at", "by", "with", "as", "vs", "per", "up", "if", "e", "g"}

    def offending(text: str) -> list[str]:
        plain = text.replace("&", "").split("...")[0].split("  ")[0]
        fields = [w for w in plain.split() if w[0].isalpha() or w[0].isdigit()]
        bad = []
        for i, w in enumerate(fields):
            core = w.strip("()[]\u2014-/:,")
            if not core or not core[0].isalpha():
                continue
            if core.isupper() and len(core) > 1:          # TXDOT, SAF, KML
                continue
            if core.lower() in small and 0 < i < len(fields) - 1:
                continue
            if not core[0].isupper():
                bad.append(core)
        return bad

    checked = 0
    for a in win.menuBar().actions():
        assert not offending(a.text().replace("&", "")), a.text()
    for menu in win.menuBar().findChildren(QMenu):
        for act in menu.actions():
            text = act.text()
            if not text or act.isSeparator() or "/" in text or text.endswith(")"):
                continue                                   # hints, key lists and notes are sentences
            bad = offending(text)
            assert not bad, f"{text!r} -> {bad}"
            checked += 1
    for dock in win.findChildren(QDockWidget):
        assert not offending(dock.windowTitle()), dock.windowTitle()
    for tb in win.findChildren(QToolBar):
        assert not offending(tb.windowTitle()), tb.windowTitle()
    assert checked > 40


def test_destructive_confirmation_has_a_separate_last_chance(monkeypatch):
    from plumbline.ui import widgets

    shown = []

    class FakeBox:
        Warning, AcceptRole, RejectRole, Cancel = range(4)

        def __init__(self, parent=None):
            self.accept = None

        def setIcon(self, value): pass
        def setWindowTitle(self, value): pass
        def setInformativeText(self, value): pass
        def setDefaultButton(self, value): pass
        def setText(self, value): shown.append(value)

        def addButton(self, text, role):
            button = object()
            if role == self.AcceptRole:
                self.accept = button
            return button

        def exec(self): pass
        def clickedButton(self): return self.accept

    monkeypatch.setattr(widgets, "QMessageBox", FakeBox)
    assert widgets.destructive_confirm(None, "Delete",
                                       [("Start fresh?", "Start fresh"),
                                        ("Are you sure?", "Yes, delete")],
                                       "Last chance to cancel, continue?")
    assert shown == ["Start fresh?", "Are you sure?", "Last chance to cancel, continue?"]


def test_a_tall_dialog_is_kept_on_the_screen_and_still_reaches_its_buttons(win, app, auto):
    """The report and settings dialogs grew until their buttons were off a survey laptop's
    screen.  Any dialog this program builds is now clamped to the screen and its body scrolls,
    with the OK/Cancel row left outside the scroll area so it is always reachable."""
    from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QScrollArea, QVBoxLayout)
    from plumbline.ui import dialogfit

    class Tall(QDialog):
        def __init__(self, rows):
            super().__init__()
            lay = QVBoxLayout(self)
            for i in range(rows):
                lay.addWidget(QLabel(f"row {i}"))
            self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            lay.addWidget(self.bb)

    max_w, max_h = dialogfit.available_size(win)
    d = Tall(80)
    hint = d.sizeHint().height()                            # before anything fits it
    d.show(); pump(app)
    assert hint > max_h                                     # it really did not fit
    assert d.height() <= max_h and d.width() <= max_w
    sc = d.findChild(QScrollArea)
    assert sc is not None and len(sc.widget().findChildren(QLabel)) == 80
    assert d.bb.parent() is d and d.bb.isVisible()          # the buttons did not go into the scroll
    d.close()

    small = Tall(3)
    small.show(); pump(app)
    assert small.findChild(QScrollArea) is None             # and a dialog that fits is untouched
    assert small.height() <= max_h
    small.close()


def test_feature_code_editor_changes_codes_and_layers(win, app, auto):
    from plumbline.ui.dialogs import FeatureCodesDialog
    pr = win.state.project
    d = FeatureCodesDialog(win.state, win)
    codes = [d.tbl.item(r, 0).text() for r in range(d.tbl.rowCount())]
    assert "EP" in codes and "BLDG" in codes
    r = codes.index("EP")
    d.tbl.item(r, 3).setText("PAVEMENT-EDGE")
    d._add_row(__import__("plumbline.core.featurecodes", fromlist=["FeatureCode"]).FeatureCode("CB", "Catch basin", "point", "UTIL-CB"))
    d._ok()
    assert pr.codes.get("EP").layer == "PAVEMENT-EDGE" and pr.codes.get("CB") is not None
    assert "PAVEMENT-EDGE" in pr.layers and "UTIL-CB" in pr.layers
    assert all(p.layer == "PAVEMENT-EDGE" for p in pr.points.values() if p.desc.startswith("EP"))   # re-applied
    win.undo()
    assert win.state.project.codes.get("EP").layer == "ROAD-EP"
    # duplicate codes are refused
    d2 = FeatureCodesDialog(win.state, win)
    d2._add_row(__import__("plumbline.core.featurecodes", fromlist=["FeatureCode"]).FeatureCode("GS", "dup", "point", "X"))
    n = len(win.state.undo_stack)
    d2._ok()
    assert len(win.state.undo_stack) == n and any("same code" in b for b in auto["boxes"])


def test_welcome_help_about_plugins_dialogs_open(win, app, auto):
    from plumbline.ui.dialogs import AboutDialog, HelpDialog, PluginsDialog, WelcomeDialog
    for cls in (HelpDialog, AboutDialog):
        d = cls(win)
        d.show()
        pump(app)
        d.close()
    reloaded = []
    pdlg = PluginsDialog(win, on_reload=lambda: reloaded.append(1))
    pdlg._reload()
    assert reloaded and "Plugin folder" in pdlg.view.toPlainText()
    w = WelcomeDialog(win)
    w._pick("sample")
    assert w.choice == "sample"
    win.show_welcome()                                                 # exec auto-accepts, choice "none"


def test_new_project_dialog_defers_crs_to_the_project_crs_step(win, app, auto, tmp_path, monkeypatch):
    from plumbline.ui.new_project import NewProjectDialog

    parent_folder = str(tmp_path / "Project folders")
    monkeypatch.setitem(settings()._data, "default_project_folder", parent_folder)
    dialog = NewProjectDialog(win)
    assert dialog.setup_parent_folder == parent_folder
    assert dialog.setup_job is True
    assert not hasattr(dialog, "r_unassigned")
    assert not hasattr(dialog, "cmb_texas")
    assert not hasattr(dialog, "picker")
    assert all(name in dialog.setup.lbl_preview.text() for name in
               ("Field Data", "Field Book", "Control", "Drawings", "Surfaces", "Imagery", "Reports"))

    dialog.ed_name.setText("Job 42")
    assert "Job Setup.txt" in dialog.setup.lbl_preview.text()
    assert "All subfolders start empty" in dialog.setup.lbl_preview.text()
    dialog._accept()
    assert dialog.project_name == "Job 42"
    assert dialog.crs.is_unassigned and dialog.crs.unit == "ftUS"
    assert "Project Coordinate System" in dialog.crs_step_note.text()

    # Unassigned geometry still works; only geodetic operations wait for the next CRS step.
    scratch = Project("unassigned", dialog.crs)
    scratch.add_point(0.0, 0.0, 1.0, number="1", desc="GS")
    assert scratch.extents() is not None
    with pytest.raises(C.LocalCRSError) as err:
        scratch.crs.to_lonlat(0.0, 0.0)
    assert "UNASSIGNED" in str(err.value) and "Select CRS" in str(err.value)


def test_save_as_dialog_shows_the_fixed_standard_folder_tree(win, app):
    from plumbline.ui.project_package_dialog import ProjectPackageDialog

    dlg = ProjectPackageDialog(win, name="Job 42", parent_folder="/tmp")
    preview = dlg.lbl_folders.text()
    for folder in ("Field Data", "Field Book", "Control", "Drawings", "Surfaces", "Imagery", "Reports"):
        assert f"{folder}/" in preview


def test_job_setup_uses_the_configured_default_project_parent(app, tmp_path, monkeypatch):
    from plumbline.core.settings import settings
    from plumbline.ui.job_setup import JobSetupPanel

    parent_folder = str(tmp_path / "Survey Jobs")
    monkeypatch.setitem(settings()._data, "default_project_folder", parent_folder)
    panel = JobSetupPanel()
    panel.set_job_name("Job 42")
    assert panel.parent_folder == parent_folder
    assert panel.folder_path() == tmp_path / "Survey Jobs" / "Job 42"


def test_find_select_all_zoom_and_layer_creation(win, app, auto, monkeypatch):
    st = win.state
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("20", True)))
    win.find_point()
    p20 = st.project.point_by_number("20")
    assert st.sel_points == {p20.id}
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("BLDG", True)))
    win.find_point()
    assert len(st.sel_points) == 4
    win.zoom_selection()
    x0, y0, x1, y1 = win.canvas.view.bounds()
    assert all(x0 <= p.x <= x1 and y0 <= p.y <= y1 for p in st.project.points.values() if p.id in st.sel_points)
    win.select_all()
    assert len(st.sel_points) == len(st.project.points)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("MY-NEW-LAYER", True)))
    win.layers._new()
    assert "MY-NEW-LAYER" in st.project.layers and win.canvas.current_layer == "MY-NEW-LAYER"
    assert win.cmb_layer.currentText() == "MY-NEW-LAYER"


def test_png_export_and_reports_open(win, app, auto, monkeypatch, tmp_path):
    monkeypatch.setattr(win, "_ensure_fieldbook_for_processing", lambda **_kwargs: True)
    out = tmp_path / "view.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    win.export_png()
    assert out.exists() and out.stat().st_size > 5000
    from plumbline.ui import dialogs
    shown = []
    monkeypatch.setattr(dialogs.ReportViewer, "exec", lambda self: shown.append(self.report.title))
    win.report_points()
    win.report_qa()
    win.report_crs()
    win.report_surface_active()
    pr = win.state.project
    e = next(e for e in pr.polylines() if e.closed)
    win.state.select(entities=[e.id])
    win.report_polylines()
    assert len(shown) == 5 and "Line" in shown[-1]
    win.state.clear_selection()
    win.report_polylines()                                            # nothing selected -> message, not a crash
    assert len(shown) == 5


def test_local_imagery_import_is_copied_into_the_project_and_survives_a_move(win, app, tmp_path):
    import json
    import shutil
    from pathlib import Path

    from PIL import Image

    project_root = tmp_path / "Portable Job"
    project_root.mkdir()
    project_file = project_root / "Portable Job.plb"
    project = win.state.project
    project.save(project_file)

    source_root = tmp_path / "Original imagery"
    source_root.mkdir()
    image = source_root / "aerial.png"
    Image.new("RGB", (4, 4), (20, 100, 30)).save(image)
    image.with_suffix(".pgw").write_text(chr(10).join(("1", "0", "0", "-1", "100.5", "200.5")) + chr(10), encoding="ascii")
    image.with_suffix(".prj").write_text("test projection sidecar", encoding="utf-8")

    assert win.create_imagery_layer({"kind": "file", "name": "Local aerial", "source": {
        "path": str(image), "corners": [[100, 200], [104, 200], [100, 196]],
        "crs": None, "attribution": ""}})
    layer = next(layer for layer in project.imagery.values() if layer.name == "Local aerial")
    staged = Path(layer.source["path"])
    assert staged.parent == project_root / "Imagery"
    assert staged.read_bytes() == image.read_bytes()
    assert staged.with_suffix(".pgw").is_file() and staged.with_suffix(".prj").is_file()
    assert image.is_file()  # the import leaves the source untouched

    project.save()
    saved = json.loads(project_file.read_text(encoding="utf-8"))
    stored_layer = next(layer for layer in saved["imagery"] if layer["name"] == "Local aerial")
    assert stored_layer["source"]["path"] == "Imagery/aerial.png"

    moved_root = tmp_path / "Moved" / project_root.name
    moved_root.parent.mkdir()
    shutil.move(str(project_root), moved_root)
    moved_project = Project.load(moved_root / project_file.name)
    moved_image = Path(next(layer for layer in moved_project.imagery.values()
                            if layer.name == "Local aerial").source["path"])
    assert moved_image == moved_root / "Imagery" / "aerial.png"
    assert moved_image.with_suffix(".pgw").is_file() and moved_image.with_suffix(".prj").is_file()


def test_the_imagery_panel_has_no_check_controls_and_notes_checks_from_older_files(win, app, auto):
    """Item 16: checking points against imagery is gone - and old check records say so out loud."""
    pr = win.state.project
    win.create_imagery_layer({"kind": "tiles", "name": "Dummy", "source": {
        "name": "Dummy", "url": "http://127.0.0.1:9/{z}/{x}/{y}.jpg", "max_zoom": 19, "min_zoom": 0, "attribution": "",
        "tile_size": 256, "subdomains": "", "ext": "jpg", "headers": {}}})
    win.state.tiles.offline = True                                    # never touch the network here
    win.imagery.lst.setCurrentRow(0)

    # nothing in the panel can start, hold, count or export a check
    for gone in ("btn_start", "btn_stop", "btn_report", "btn_csv", "btn_nudge", "btn_del", "table",
                 "stats", "cmb_which", "ed_filter", "sp_tol", "sp_mpp", "lbl_tol", "_point_ids",
                 "refresh_checks", "refresh_stats", "_apply_nudge", "_delete_checks", "_export_csv"):
        assert not hasattr(win.imagery, gone), f"the imagery panel still has {gone}"
    assert win.imagery.lbl_legacy.isHidden(), "no note when the project holds no old checks"

    # a project written by an older version keeps its checks, and the panel says so
    from plumbline.core.model import ImageryCheck
    p = pr.point_by_number("20")
    pr.checks[1] = ImageryCheck(1, p.id, p.number, (p.x, p.y), (p.x + 1.0, p.y), "Dummy", "note", "2026-01-01 08:00", "", True)
    win.imagery.refresh()
    assert not win.imagery.lbl_legacy.isHidden()
    txt = win.imagery.lbl_legacy.text()
    assert "older version" in txt and "1 imagery check" in txt and "Measure" in txt


def test_layer_lock_makes_objects_unpickable_and_hidden_unselectable(win, app):
    pr = win.state.project
    p = pr.point_by_number("20")
    pr.layers[p.layer].locked = True
    pr.touch()
    assert win.state.spatial_index().pick(p.x, p.y, 2.0) != ("pt", p.id)
    pr.layers[p.layer].locked = False
    pr.layers[p.layer].visible = False
    pr.touch()
    assert win.state.spatial_index().pick(p.x, p.y, 2.0) != ("pt", p.id)
def test_the_file_menu_offers_both_samples_and_the_real_one_opens(win, app, auto):
    """File > Open Sample Project has two entries: the synthetic site, and the real job.
    The real one must open the *whole job folder* - that is the point of it."""
    from pathlib import Path

    from PySide6.QtWidgets import QMenu
    labels = {}
    for menu in getattr(win, "_all_menus", win.menuBar().findChildren(QMenu)):
        for act in menu.actions():
            labels[act.text()] = act
            if act.menu() is not None:
                try:
                    for sub in act.menu().actions():
                        labels[sub.text()] = sub
                except RuntimeError:
                    pass
    assert win.a_sample.text() in labels and win.a_sample_real.text() in labels

    real = Path(__file__).resolve().parent.parent / "samples" / "Real World" / "Real World.plb"
    if not real.exists():
        pytest.skip("Real World sample is not built")
    said = []
    win.state.message.connect(lambda _level, text: said.append(text))
    win.open_sample_real()
    pr = win.state.project
    assert len(pr.points) == 3773
    assert pr.crs.authority == "EPSG:6584"
    assert Path(pr.path).parent.name == "Real World"        # opened in place, job folder and all
    assert any("Real World sample" in t for t in said)
    # ...and the same job folder is what Survey > Fieldwork Manager points at
    from plumbline.ui.fieldwork_window import FieldworkBridge
    assert FieldworkBridge(win).job_root() == Path(pr.path).parent
