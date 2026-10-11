"""End-to-end UI tests (offscreen Qt): real mouse / key events, dialogs auto-accepted, a local fake tile server."""
import gc
import http.server
import io
import math
import os
import socketserver
import threading
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from plumbline.core import crs as C
from plumbline.core.model import Polyline, TextEntity
from plumbline.core.project import Project
from plumbline.sample import make_sample_project, write_sample_files
from plumbline.ui.main_window import MainWindow


@pytest.fixture(scope="session")
def app():
    a = QApplication.instance() or QApplication([])
    return a


@pytest.fixture()
def auto(monkeypatch, app):
    """Dialogs 'click OK' immediately; message boxes are recorded instead of blocking."""
    log = {"boxes": []}

    def fake_exec(self):
        if isinstance(self, QMessageBox):
            log["boxes"].append(self.text())
            return 0
        hook = getattr(type(self), "_test_hook", None)
        if hook:
            hook(self)
        self.show()
        if hasattr(self, "_try_accept"):
            self._try_accept()
        elif hasattr(self, "_ok"):
            self._ok()
        elif hasattr(self, "_accept"):
            self._accept()
        else:
            self.accept()
        return 1 if self.result() == QDialog.Accepted else 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    return log


@pytest.fixture()
def win(app, auto, tmp_path, monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOME", str(tmp_path / "home"))
    from plumbline.core import settings as S
    S._instance = None
    w = MainWindow(project=make_sample_project())
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    w.canvas.zoom_extents()
    app.processEvents()
    yield w
    w.state.tiles.shutdown()
    w.close()
    app.processEvents()
    S._instance = None


def pump(app, n=5, ms=0):
    for _ in range(n):
        app.processEvents()
        if ms:
            QTest.qWait(ms)


def click_world(win, x, y, button=Qt.LeftButton, mods=Qt.NoModifier):
    sx, sy = win.canvas.view.to_screen(x, y)
    QTest.mouseClick(win.canvas, button, mods, QPoint(int(round(sx)), int(round(sy))))


def drag_world(win, x0, y0, x1, y1):
    a = QPoint(*[int(round(v)) for v in win.canvas.view.to_screen(x0, y0)])
    b = QPoint(*[int(round(v)) for v in win.canvas.view.to_screen(x1, y1)])
    QTest.mousePress(win.canvas, Qt.LeftButton, Qt.NoModifier, a)
    QTest.mouseMove(win.canvas, QPoint((a.x() + b.x()) // 2, (a.y() + b.y()) // 2))
    QTest.mouseMove(win.canvas, b)
    QTest.mouseRelease(win.canvas, Qt.LeftButton, Qt.NoModifier, b)


# ------------------------------------------------------------------ boot / render
def test_window_boots_and_renders(win, app):
    assert "Sample site" in win.windowTitle()
    img = win.canvas.render_image(900)
    assert img.width() == 900 and img.height() > 100
    arr = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)[:, :900]
    assert len(np.unique(arr.reshape(-1, 4), axis=0)) > 50           # not a blank canvas
    assert not win.a_undo.isEnabled()
    assert win.points.model.rowCount() == len(win.state.project.points)


def test_theme_toggle_does_not_break(win, app):
    win.toggle_theme()
    pump(app)
    win.canvas.render_image(400)
    win.toggle_theme()


def test_opening_an_unassigned_project_asks_for_crs_before_field_data_route(win, monkeypatch, tmp_path):
    project_path = tmp_path / "Unassigned.plb"
    Project("Unassigned").save(project_path)
    events = []
    monkeypatch.setattr(win, "_ensure_project_crs", lambda: events.append("CRS") or False)
    monkeypatch.setattr(win, "_ensure_fieldbook_for_processing",
                        lambda notify=True: events.append("Field Book") or False)
    monkeypatch.setattr(win, "_ask_field_data_route",
                        lambda route: events.append(("route", route)) or 0)
    monkeypatch.setattr(win, "_complete_import_onboarding",
                        lambda imported_points=0: events.append(("complete", imported_points)))

    win._open(str(project_path))

    assert events == ["CRS", "Field Book", ("route", "existing"), ("complete", 0)]


def test_imagery_import_is_blocked_if_the_project_crs_picker_is_cancelled(win, monkeypatch, tmp_path):
    project = Project("Unassigned imagery")
    win.state.set_project(project, dirty=False)
    prompts = []
    monkeypatch.setattr(win, "crs_dialog", lambda *args, **kwargs: prompts.append("CRS"))

    assert not win._ensure_project_crs()
    assert not win.add_imagery_file(str(tmp_path / "not-a-real-image.tif"))
    assert not win.add_kml_overlay({})
    assert prompts == ["CRS", "CRS", "CRS"]
    assert not project.imagery


def test_declining_default_imagery_does_not_skip_fix_point_errors_offer(win, auto):
    project = win.state.project
    project.imagery.clear()
    auto["boxes"].clear()

    win._complete_import_onboarding()

    texts = auto["boxes"]
    imagery_index = next(i for i, text in enumerate(texts) if "Esri World Imagery" in text)
    fix_index = next(i for i, text in enumerate(texts) if "review the survey points" in text)
    assert imagery_index < fix_index
    assert not project.imagery, "the test message-box handler declines both offers"


def test_accepting_imagery_defers_fix_point_errors_offer_until_canvas_refresh(win, monkeypatch):
    project = win.state.project
    project.imagery.clear()
    assert not project.crs.is_local

    events = []
    monkeypatch.setattr(win, "_offer_default_esri_imagery",
                        lambda: events.append("imagery") or True)
    monkeypatch.setattr(win, "_offer_fix_point_errors_after_imagery",
                        lambda: events.append("wait for canvas refresh"))
    monkeypatch.setattr(win, "_offer_fix_point_errors",
                        lambda: events.append("show immediately"))

    win._complete_import_onboarding()

    assert events == ["imagery", "wait for canvas refresh"]


def test_accepting_default_imagery_adds_the_shipped_esri_tile_source(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    specs = []
    monkeypatch.setattr(win, "create_imagery_layer", specs.append)
    monkeypatch.setattr(QMessageBox, "exec", lambda _self: QMessageBox.StandardButton.Yes)

    assert win._offer_default_esri_imagery()
    assert specs[0]["kind"] == "tiles"
    assert specs[0]["name"] == "Esri World Imagery"
    assert specs[0]["source"]["name"] == "Esri World Imagery"


def test_processing_actions_open_fieldbook_and_are_blocked_if_it_stays_unloaded(win, monkeypatch, auto):
    from plumbline.core.settings import settings

    monkeypatch.setitem(settings()._data, "require_fieldbook_for_processing", True)
    monkeypatch.setattr(win, "_has_active_fieldbook", lambda: False)
    opened = []
    monkeypatch.setattr(win, "open_fieldbook_dialog", lambda: opened.append("Field Book"))
    processed = []
    monkeypatch.setattr(win.state.project, "apply_codes_to_points", lambda: processed.append("codes"))
    monkeypatch.setattr(win.state.project, "process_linework", lambda: processed.append("linework"))

    win.open_fix_point_errors()
    win.open_fix_linework()
    win.apply_codes()
    win.process_linework()
    win.report_qa()
    win.open_fieldwork()
    win.edit_selected_line_geometry()

    assert len(opened) == 7
    assert processed == []
    assert getattr(win, "_qa_workbench_window", None) is None


def test_disabling_the_fieldbook_requirement_allows_processing(win, monkeypatch):
    from plumbline.core.settings import settings

    monkeypatch.setitem(settings()._data, "require_fieldbook_for_processing", False)
    monkeypatch.setattr(win, "_has_active_fieldbook", lambda: False)
    opened = []
    monkeypatch.setattr(win, "open_fieldbook_dialog", lambda: opened.append("Field Book"))
    processed = []
    monkeypatch.setattr(win.state.project, "process_linework",
                        lambda: processed.append("linework") or {"strings": 0, "replaced": 0})
    monkeypatch.setattr(win.state.project, "apply_codes_to_points",
                        lambda: processed.append("codes") or {"matched": 0, "unknown": {}})

    assert win._ensure_fieldbook_for_processing()
    win.process_linework()
    win.apply_codes()

    assert opened == []
    assert processed == ["linework", "codes"]


def test_empty_job_fieldbook_does_not_satisfy_processing_requirement(win, tmp_path):
    from plumbline.fieldwork.io_carlson import write_fwb_file

    project = win.state.project
    project.path = str(tmp_path / "job" / "job.plb")
    book = tmp_path / "job" / "Field Book" / "job.fwb"
    book.parent.mkdir(parents=True)
    project.settings["fieldbook_file"] = str(book)

    assert write_fwb_file(book, ["Code", "Description"], [])
    assert not win._has_active_fieldbook()

    assert write_fwb_file(book, ["Code", "Description"], [["EA", "Asphalt"]])
    assert win._has_active_fieldbook()


def test_button_highlights_use_neutral_theme_colors(win):
    from plumbline.ui import theme

    for colors in theme.THEMES.values():
        css = theme.stylesheet(colors)
        assert (f"QPushButton:default, QPushButton[accent=\"true\"] {{ background: {colors['button']}; "
                f"border-color: {colors['border']}; color: {colors['text']}; font-weight: 600;") in css
        assert (f"QToolButton:checked {{ background: {colors['alt']}; "
                f"border-color: {colors['border']}; }}") in css


def test_escape_returns_to_pan_from_toolbar_and_panel_focus(win, app):
    win.set_tool("polyline")
    draw_button = win.tb_draw_tools.widgetForAction(win.tool_acts["polyline"])
    assert draw_button is not None
    draw_button.setFocus()
    pump(app)
    assert QApplication.focusWidget() == draw_button
    QTest.keyClick(draw_button, Qt.Key_Escape)
    pump(app)
    assert win.canvas.tool.name == "pan"
    assert win.tool_acts["pan"].isChecked()
    assert win.canvas.cursor().shape() == Qt.OpenHandCursor

    # Dock/panel focus follows the same global Escape shortcut.
    win.set_tool("select")
    win.d_pts.show()
    win.d_pts.raise_()
    win.points.view.setFocus()
    pump(app)
    assert QApplication.focusWidget() == win.points.view
    QTest.keyClick(win.points.view, Qt.Key_Escape)
    pump(app)
    assert win.canvas.tool.name == "pan"
    assert win.canvas.hasFocus()
    assert win.canvas.cursor().shape() == Qt.OpenHandCursor


# ------------------------------------------------------------------ tools with real mouse events
def test_draw_polyline_with_clicks_and_undo_redo(win, app):
    pr = win.state.project
    n_before = sum(1 for _ in pr.polylines())
    win.set_tool("polyline")
    x0, y0, x1, y1 = pr.extents()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for dx, dy in ((0, 0), (40, 0), (40, 30)):
        click_world(win, cx + dx, cy + dy)
    QTest.keyClick(win.canvas, Qt.Key_Return)
    pump(app)
    assert sum(1 for _ in pr.polylines()) == n_before + 1
    e = max(pr.polylines(), key=lambda e: e.id)
    assert len(e.verts) == 3 and e.layer == "0"
    assert win.a_undo.isEnabled() and "polyline" in win.a_undo.text().lower()
    win.undo()
    assert sum(1 for _ in pr.polylines()) == n_before
    win.redo()
    assert sum(1 for _ in pr.polylines()) == n_before + 1


def test_typed_coordinates_and_polar_entry(win, app):
    pr = win.state.project
    win.set_tool("polyline")
    p1 = pr.point_by_number("1")
    win.ed_cmd.setText("1")                                           # a point number
    win._command_entered()
    win.ed_cmd.setText("@100<N90E")                                   # 100 ft due east of the last vertex
    win._command_entered()
    win.ed_cmd.setText("@10,0")                                       # 10 ft north (N,E order)
    win._command_entered()
    QTest.keyClick(win.canvas, Qt.Key_Return)
    e = max(pr.polylines(), key=lambda e: e.id)
    assert len(e.verts) == 3
    assert (e.verts[0, 0], e.verts[0, 1]) == pytest.approx((p1.x, p1.y))
    assert (e.verts[1, 0] - p1.x, e.verts[1, 1] - p1.y) == pytest.approx((100.0, 0.0), abs=1e-6)
    assert (e.verts[2, 0] - e.verts[1, 0], e.verts[2, 1] - e.verts[1, 1]) == pytest.approx((0.0, 10.0), abs=1e-6)
    win.ed_cmd.setText("@50<garbage")
    win._command_entered()
    assert "bearing" in win.lbl_hint.text().lower()                   # friendly error, no crash


def test_arc_tool_creates_bulge(win, app):
    pr = win.state.project
    win.set_tool("arc")
    x0, y0, *_ = pr.extents()
    for x, y in ((x0 + 50, y0 + 100), (x0 + 60, y0 + 110), (x0 + 70, y0 + 100)):
        win.canvas.tool.enter_coordinate(x, y)
    e = max(pr.polylines(), key=lambda e: e.id)
    assert e.bulges is not None and abs(e.bulges[0]) > 0.05          # a real arc (bulge), not a straight chord


def test_box_selection_window_vs_crossing_and_delete(win, app):
    pr = win.state.project
    st = win.state
    win.set_tool("select")
    ext = pr.extents()
    # window selection (drag left -> right) around the building footprint picks the corners + its outline
    bx = [p for p in pr.points.values() if p.desc == "BLDG"]
    xs, ys = [p.x for p in bx], [p.y for p in bx]
    drag_world(win, min(xs) - 8, max(ys) + 8, max(xs) + 8, min(ys) - 8)
    assert {p.id for p in bx} <= st.sel_points
    bld = [e for e in pr.polylines() if e.layer == "STRUCT-BLDG"]
    assert bld and bld[0].id in st.sel_entities
    n_pts = len(pr.points)
    win.delete_selection()
    assert len(pr.points) < n_pts
    win.undo()
    assert len(pr.points) == n_pts
    # crossing selection (drag right -> left) needs only a touch
    st.clear_selection()
    drag_world(win, ext[2] + 5, ext[1] - 5, ext[2] - 30, ext[1] + 30)
    assert st.sel_points or st.sel_entities


def test_click_select_shift_and_ctrl(win, app):
    pr = win.state.project
    win.set_tool("select")
    p1, p2 = pr.point_by_number("20"), pr.point_by_number("24")
    click_world(win, p1.x, p1.y)
    assert win.state.sel_points == {p1.id}
    click_world(win, p2.x, p2.y, mods=Qt.ShiftModifier)
    assert win.state.sel_points == {p1.id, p2.id}
    click_world(win, p1.x, p1.y, mods=Qt.ControlModifier)
    assert win.state.sel_points == {p2.id}
    click_world(win, p2.x + 2000, p2.y + 2000)                      # empty space clears
    assert not win.state.sel_points


def test_vertex_grip_drag_and_insert_vertex(win, app):
    pr = win.state.project
    win.set_tool("select")
    e = next(e for e in pr.polylines() if e.layer == "STRUCT-BLDG")
    win.state.select(entities=[e.id])
    pump(app)
    v0 = e.verts[0].copy()
    drag_world(win, v0[0], v0[1], v0[0] + 25.0, v0[1] + 15.0)          # into the footprint, clear of the snap radius
    e2 = pr.entities[e.id]
    moved = math.hypot(e2.verts[0, 0] - v0[0], e2.verts[0, 1] - v0[1])
    assert 15.0 < moved < 40.0
    assert "vertex" in win.a_undo.text().lower()
    win.undo()
    assert pr.entities[e.id].verts[0, 0] == pytest.approx(v0[0])
    # double-click on a segment inserts a vertex
    n = len(pr.entities[e.id].verts)
    a, b = pr.entities[e.id].verts[0], pr.entities[e.id].verts[1]
    mid = (a + b) / 2
    sx, sy = win.canvas.view.to_screen(mid[0], mid[1])
    QTest.mouseDClick(win.canvas, Qt.LeftButton, Qt.NoModifier, QPoint(int(sx), int(sy)))
    assert len(pr.entities[e.id].verts) == n + 1                     # a vertex was inserted mid-segment


def test_measure_tool_reports_distance_and_area(win, app):
    win.set_tool("measure")
    t = win.canvas.tool
    t.enter_coordinate(1000.0, 1000.0)
    t.enter_coordinate(1100.0, 1100.0)
    assert "141.421" in t.result and "N 45" in t.result
    win.set_tool("area")
    t = win.canvas.tool
    for p in ((0, 0), (100, 0), (100, 100), (0, 100)):
        t.enter_coordinate(*p)
    assert "10,000.00" in t.result and "0.2296" in t.result          # 10,000 sq ft = 0.2296 acre


def test_move_tool_moves_selection(win, app):
    pr = win.state.project
    p = pr.point_by_number("20")
    win.state.select(points=[p.id])
    x, y = p.x, p.y
    win.set_tool("move")
    win.canvas.tool.enter_coordinate(0.0, 0.0)
    win.canvas.tool.enter_coordinate(5.0, -3.0)
    assert (pr.points[p.id].x, pr.points[p.id].y) == pytest.approx((x + 5.0, y - 3.0))
    win.undo()
    assert (pr.points[p.id].x, pr.points[p.id].y) == pytest.approx((x, y))


def test_point_tool_applies_code_layer(win, app):
    pr = win.state.project
    win.set_tool("point")
    win.ed_desc.setText("MH")
    n = len(pr.points)
    x0, y0, *_ = pr.extents()
    win.canvas.tool.enter_coordinate(x0 + 10.5, y0 + 77.25)
    assert len(pr.points) == n + 1
    p = max(pr.points.values(), key=lambda p: p.id)
    assert p.desc == "MH" and p.layer == "UTIL-MH"


# ------------------------------------------------------------------ docks
def test_points_table_edit_and_selection_sync(win, app):
    pr = win.state.project
    m = win.points.model
    win.points.reload()
    row = 0
    pid = m.ids[row]
    idx = m.index(row, 4)
    assert m.setData(idx, "TEST DESC")
    assert pr.points[pid].desc == "TEST DESC"
    assert not m.setData(m.index(row, 1), "not a number")             # rejected, project untouched
    win.undo()
    assert pr.points[pid].desc != "TEST DESC"
    win.state.select(points=[m.ids[3], m.ids[4]])
    pump(app)
    sel = {win.points.proxy.data(i, Qt.UserRole + 1) for i in win.points.view.selectionModel().selectedRows()}
    assert sel == {m.ids[3], m.ids[4]}
    win.points.view.selectRow(0)
    pump(app)
    assert win.state.sel_points == {win.points.proxy.data(win.points.proxy.index(0, 0), Qt.UserRole + 1)}


def test_layer_visibility_and_isolate(win, app):
    pr = win.state.project
    d = win.layers
    d.refresh()
    names = [d.tbl.item(r, 3).text() for r in range(d.tbl.rowCount())]
    r = names.index("ROAD-EP")
    d._clicked(r, 0)                                                  # eye
    assert not pr.layers["ROAD-EP"].visible
    d._show_all()
    assert pr.layers["ROAD-EP"].visible
    d.tbl.setCurrentCell(r, 3)
    d._isolate()
    assert [n for n, l in pr.layers.items() if l.visible] == ["ROAD-EP"]
    d._show_all()
    d._dbl(r, 3)
    assert win.canvas.current_layer == "ROAD-EP"


def test_properties_dock_edits_a_point(win, app):
    pr = win.state.project
    p = pr.point_by_number("20")
    win.state.select(points=[p.id])
    pump(app)
    from PySide6.QtWidgets import QLineEdit, QPushButton
    fields = win.props.body.findChildren(QLineEdit)
    assert any(f.text() == "20" for f in fields)
    desc = [f for f in fields if f.text() == p.desc][0]
    desc.setText("CHANGED")
    next(b for b in win.props.body.findChildren(QPushButton) if b.text() == "Apply changes").click()
    assert pr.points[p.id].desc == "CHANGED"


# ------------------------------------------------------------------ surfaces
def test_surface_create_edit_contours_volume_profile_report(win, app, auto):
    pr = win.state.project
    win.create_surface(None)                                          # SurfaceDialog accepted with defaults
    assert len(pr.surfaces) == 2
    sf = list(pr.surfaces.values())[-1]
    assert sf.tin().n_tris > 100 and sf.report.get("sig")
    win.state.active_surface_id = sf.id
    win.contours_for(sf.id)
    assert any(e.kind == "contour" and e.derived == f"contours:{sf.name}" for e in pr.entities.values())
    n_c = sum(1 for e in pr.entities.values() if e.derived == f"contours:{sf.name}")
    win.contours_for(sf.id)                                           # again: replaced, not doubled
    assert sum(1 for e in pr.entities.values() if e.derived == f"contours:{sf.name}") == n_c
    win.undo()
    # volume: datum below the lowest ground
    from plumbline.ui.surface_dialogs import VolumeDialog
    vd = VolumeDialog(win.state, win)
    vd.cmb_a.setCurrentIndex(vd.cmb_a.findData(sf.id))
    vd.sp_datum.setValue(float(sf.pts[:, 2].min()) - 1.0)
    vd._calc()
    assert vd.result is not None and vd.result.cut > 0 and vd.result.fill == pytest.approx(0, abs=1e-6)
    expect = sf.tin().areas2d().sum() * 1.0 + 0
    assert vd.result.cut > expect * 1.0                               # mean depth > 1 ft below ground
    # profile along a selected polyline
    e = next(e for e in pr.polylines() if e.layer == "ROAD-EP")
    win.state.select(entities=[e.id])
    from plumbline.ui.surface_dialogs import ProfileDialog
    pd = ProfileDialog(win.state, sf, e, win)
    assert np.isfinite(pd.z).sum() > 10
    pd.view.resize(700, 320)
    pd.view.grab()
    # surface report renders
    win.report_surface(sf.id)


def test_surface_goes_stale_and_rebuilds(win, app, auto):
    pr = win.state.project
    sf = next(iter(pr.surfaces.values()))
    win.surfaces._compute_stale()
    assert not win.surfaces._stale.get(sf.id)
    with win.state.edit("add shot"):
        pr.add_point(pr.extents()[0] + 20, pr.extents()[1] + 120, 999.0, desc="GS")
    win.surfaces._compute_stale()
    assert win.surfaces._stale.get(sf.id) is True
    assert "out of date" in win.surfaces.lst.item(0).text()
    win.create_surface(sf)                                            # Edit / Rebuild
    win.surfaces._compute_stale()
    assert not win.surfaces._stale.get(sf.id)
    assert sf.tin().bounds()[5] >= 999.0


# ------------------------------------------------------------------ import / export through the window
def test_import_every_format_through_the_dispatcher(win, app, auto, tmp_path):
    files = write_sample_files(tmp_path / "samp")
    st = win.state
    # fresh local project: CSV keeps it local; a CRS-bearing LandXML assigns its CRS
    st.new_project("t", C.ProjectCRS.local("ftUS"))
    assert win.importer.import_path(str(files["csv"]))
    assert len(st.project.points) == 238 and st.project.crs.is_local
    st.new_project("t2", C.ProjectCRS.local("ftUS"))
    assert win.importer.import_path(str(files["landxml"]))
    pr = st.project
    assert len(pr.points) == 238 and len(pr.surfaces) == 1 and pr.crs.authority == "EPSG:6584"
    assert win.importer.import_path(str(files["dxf"]))                # duplicates renumbered, CRS stays
    assert len(pr.points) == 238 * 2 and pr.crs.authority == "EPSG:6584"
    n = len(pr.entities)
    assert win.importer.import_path(str(files["gpkg"]))              # points + lines + polygons
    assert len(pr.entities) > n
    # KMZ is lon/lat -> converted into the project CRS and lands where the originals are
    st.new_project("t3", C.ProjectCRS.from_epsg(6584))
    assert win.importer.import_path(str(files["kmz"]))
    p = pr = st.project
    ref = make_sample_project(with_surface=False)
    a, b = ref.point_by_number("20"), pr.point_by_number("20")
    assert math.hypot(a.x - b.x, a.y - b.y) < 0.05               # lon/lat 9 decimals round-trip
    # .plb opens as a project
    assert win.importer.import_path(str(files["project"]))
    assert win.state.project.name.startswith("Sample site")


def test_import_metric_files_into_a_us_foot_project(win, app, auto, tmp_path):
    """A LandXML (metres, EPSG:32138) and a DXF (metres, no CRS) land in the same place as the original US-foot data."""
    from plumbline.io import dxf_io, landxml
    ref = make_sample_project(with_surface=False)
    metric = make_sample_project(with_surface=True)
    metric.reproject(C.ProjectCRS.from_epsg(32138))                   # coordinates AND elevations to metres
    assert metric.crs.unit == "m" and metric.point_by_number("20").z == pytest.approx(ref.point_by_number("20").z * 1200 / 3937, abs=1e-6)
    lx = tmp_path / "metric.xml"
    landxml.write_landxml(lx, metric)
    dx = tmp_path / "metric.dxf"
    dxf_io.write_dxf(dx, metric)
    st = win.state
    for path in (lx, dx):
        st.new_project("us", C.ProjectCRS.from_epsg(2276))
        assert win.importer.import_path(str(path)), path
        a, b = ref.point_by_number("20"), st.project.point_by_number("20") or next(p for p in st.project.points.values())
        if path.suffix == ".xml":
            assert (b.x, b.y) == pytest.approx((a.x, a.y), abs=0.01)          # CRS conversion m -> US ft (same zone)
            assert b.z == pytest.approx(a.z, abs=0.01)                          # elevations converted too
            sf = next(iter(st.project.surfaces.values()))
            assert sf.pts[:, 2].max() < 600 and sf.pts[:, 2].min() > 500       # a surface in feet, not metres
        else:
            xs = np.array([p.x for p in st.project.points.values()])
            assert xs.min() == pytest.approx(min(p.x for p in ref.points.values()), abs=0.05)   # unit scale from $INSUNITS
            zs = np.array([p.z for p in st.project.points.values() if not math.isnan(p.z)])
            assert 500 < zs.min() and zs.max() < 530                            # feet again


def test_import_wrong_crs_is_flagged_and_swap_works(win, app, auto, tmp_path):
    files = write_sample_files(tmp_path / "s2")
    st = win.state
    st.new_project("p", C.ProjectCRS.from_epsg(2276))
    from plumbline.ui.import_export import CsvImportDialog
    d = CsvImportDialog(st, files["tsv"])
    roles = d.roles()
    ie, inn = roles.index("easting"), roles.index("northing")
    d.roles_cb[ie].setCurrentIndex(d.roles_cb[ie].findData("northing"))
    d.roles_cb[inn].setCurrentIndex(d.roles_cb[inn].findData("easting"))
    assert "Probably wrong" in d.panel.fit.text()
    d.panel.chk_swap.setChecked(True)
    assert "Looks right" in d.panel.fit.text()


def test_exports_write_valid_files(win, app, auto, monkeypatch, tmp_path):
    import ezdxf
    from plumbline.io import gis_io, kml_io, landxml
    st = win.state
    outs = {}

    def fake_save(parent, caption, start, filt):
        ext = {"DXF": ".dxf", "LandXML": ".xml", "GIS": ".gpkg", "Google": ".kmz", "points": ".csv"}
        for k, v in ext.items():
            if k.lower() in caption.lower():
                p = str(tmp_path / f"out{v}")
                outs[k] = p
                return p, ""
        return str(tmp_path / "x.out"), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_save))
    win.a_exp_dxf.trigger()
    win.a_exp_lx.trigger()
    win.a_exp_gis.trigger()
    win.a_exp_kml.trigger()
    win.a_exp_csv.trigger()
    pump(app, 10)
    assert len(ezdxf.readfile(outs["DXF"]).modelspace()) > 300
    assert len(landxml.read_landxml(outs["LandXML"]).points) == len(st.project.points)
    assert gis_io.list_gis_layers(outs["GIS"])
    assert len(kml_io.read_kml(outs["Google"]).points) == len(st.project.points)
    assert open(outs["points"]).read().count("\n") >= len(st.project.points)
    assert st.project.settings.get("kml_strategy") in ("none", "auto")


# ------------------------------------------------------------------ CRS manager through the window
def test_crs_assign_then_reproject_through_window(win, app, auto, monkeypatch):
    from plumbline.ui.crs_dialog import CRSDialog
    st = win.state
    st.new_project("loc", C.ProjectCRS.local("ftUS"))
    for i in range(10):
        st.project.add_point(2552600.0 + 20 * i, 6967000.0 + 10 * i, 500.0 + i)

    def hook_assign(self):
        self.picker.select_key("EPSG:2276")
        self.r_assign.setChecked(True)

    monkeypatch.setattr(CRSDialog, "_test_hook", hook_assign, raising=False)
    win.crs_dialog()
    assert st.project.crs.authority == "EPSG:2276" and st.project.points[1].x == 2552600.0     # relabelled only
    x_before = next(iter(st.project.points.values())).x                # US feet, before the conversion

    def hook_reproj(self):
        self.picker.select_key("EPSG:32138")
        self.r_reproj.setChecked(True)

    monkeypatch.setattr(CRSDialog, "_test_hook", hook_reproj, raising=False)
    win.crs_dialog()
    p = next(iter(st.project.points.values()))
    assert st.project.crs.authority == "EPSG:32138" and st.project.crs.unit == "m"
    assert p.x == pytest.approx(x_before * 1200 / 3937, abs=0.01)     # same zone, metric false origin
    assert p.z == pytest.approx(500.0 * 1200 / 3937, abs=1e-6)         # elevations converted to metres too
    win.undo()
    assert st.project.crs.authority == "EPSG:2276"


# ------------------------------------------------------------------ imagery with a fake tile server
class _Tiles(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        from PIL import Image, ImageDraw
        _Tiles.hits += 1
        im = Image.new("RGB", (256, 256), (70, 120, 70))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 255, 255], outline=(255, 255, 255))
        d.line([0, 0, 255, 255], fill=(255, 220, 0), width=3)
        buf = io.BytesIO()
        im.save(buf, "JPEG")
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def tile_server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Tiles)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/" + "{z}/{x}/{y}.jpg"
    srv.shutdown()


def test_imagery_tiles_render_offline_and_the_panel_has_no_check_controls(win, app, auto, tile_server, monkeypatch):
    """Imagery still works; the checks and their report are gone (item 16), the display nudge stays.

    What is pinned here is the boundary of the removal: the tiles arrive and draw, a manual nudge
    still moves the imagery for display only, offline mode still serves from the cache - and there
    is no control left anywhere in the panel that starts, records, totals or reports a check.
    """
    st, pr = win.state, win.state.project
    win.create_imagery_layer({"kind": "tiles", "name": "Fake imagery",
                              "source": {"name": "Fake imagery", "url": tile_server, "max_zoom": 19, "min_zoom": 0,
                                         "attribution": "test tiles", "tile_size": 256, "subdomains": "", "ext": "jpg", "headers": {}}})
    win.canvas.zoom_extents()
    deadline = time.time() + 15
    while time.time() < deadline and (win.canvas.imagery.pending or not win.canvas.imagery._pix):
        pump(app, 2, 40)
    assert len(win.canvas.imagery._pix) > 0, "no tiles arrived from the fake server"
    win.canvas._dirty = True
    img = win.canvas.render_image(700)
    assert img.width() == 700

    for gone in ("btn_start", "btn_stop", "btn_report", "btn_csv", "btn_del",
                 "table", "stats", "cmb_which", "ed_filter", "sp_tol", "sp_mpp", "lbl_tol",
                 "_point_ids", "refresh_checks", "refresh_stats", "_apply_nudge", "_export_csv",
                 "start_requested", "stop_requested", "report_requested"):
        assert not hasattr(win.imagery, gone), f"the imagery panel still has {gone}"
    assert [b.text() for b in (win.imagery.btn_add, win.imagery.btn_rm, win.imagery.btn_meta,
                               win.imagery.btn_ge_out, win.imagery.btn_ge_in)] == \
        ["Add...", "Remove", "Look up imagery source at the view centre", "Export KMZ...", "Import pins..."]
    assert win.imagery.btn_nudge_points.text() == "Nudge by points..."
    assert not pr.checks, "nothing in the interface writes a check record any more"

    # the nudge is a display offset; the survey itself never moves
    lay = next(iter(pr.imagery.values()))
    pid = next(iter(pr.points))
    before = (pr.points[pid].x, pr.points[pid].y)
    win.imagery.lst.setCurrentRow(0)
    win.imagery.sp_de.setValue(-2.5)
    win.imagery.sp_dn.setValue(1.25)
    assert lay.nudge == pytest.approx((-2.5, 1.25))
    assert (pr.points[pid].x, pr.points[pid].y) == before

    # offline mode still shows cached tiles, never hits the network
    hits = _Tiles.hits
    win.imagery.chk_off.setChecked(True)
    win.canvas.imagery.clear()
    win.canvas.invalidate()
    win.canvas.zoom_extents()
    for _ in range(25):
        pump(app, 2, 40)
    assert _Tiles.hits == hits
    assert len(win.canvas.imagery._pix) > 0


def test_imagery_nudge_collects_point_pairs_and_changes_only_the_display_offset(win, app, auto):
    from plumbline.core.model import ImageryLayer

    pr = win.state.project
    layer_id = pr.new_id()
    layer = ImageryLayer(layer_id, "Nudge test", "file", {"path": ""}, nudge=(10.0, -3.0))
    pr.imagery[layer_id] = layer
    win.imagery.refresh()
    win.imagery.lst.setCurrentRow(0)
    win.d_img.show()
    point = next(iter(pr.points.values()))
    original_xy = (point.x, point.y)

    win.imagery.btn_nudge_points.click()
    tool = win._imagery_nudge_tool
    assert tool is not None and win.canvas.tool is tool
    assert win.imagery.btn_nudge_points.text() == "Cancel point nudge"

    # The image feature is 8 units west and 4 north of the target survey point. Click it first,
    # then click the survey point; aligning the image should add (+8, -4) to its current nudge.
    click_world(win, point.x - 8.0, point.y + 4.0)
    click_world(win, point.x, point.y)
    assert len(tool.pairs) == 1
    QTest.keyClick(win.canvas, Qt.Key_Return)

    assert layer.nudge == pytest.approx((18.0, -7.0), abs=0.5)
    assert (point.x, point.y) == original_xy
    assert win.canvas.tool.name == "pan"
    assert win._imagery_nudge_tool is None
    assert win.imagery.btn_nudge_points.text() == "Nudge by points..."
    assert win.state.undo_stack[-1][0] == "Nudge imagery by 1 point pair(s)"


def test_google_earth_pins_come_in_as_reference_points(win, app, auto, tmp_path, monkeypatch):
    """A pin is a coordinate: it arrives on the OTHER reference layer, not as a check record."""
    from plumbline.core import reference as REF
    pr = win.state.project
    pr.settings["kml_strategy"] = "none"
    tf = pr.crs.transform_to(4326, "none")
    # "Google Earth": pins named after points, each 2 ft east / 1 ft north of the survey position
    pins = []
    for num in ("20", "21", "22", "23"):
        p = pr.point_by_number(num)
        lon, lat = tf(p.x + 2.0, p.y + 1.0)
        pins.append(f"<Placemark><name>{num}</name><Point><coordinates>{lon:.10f},{lat:.10f},0</coordinates></Point></Placemark>")
    f = tmp_path / "pins.kml"
    f.write_text('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + "".join(pins) +
                 "<Placemark><name>999</name><Point><coordinates>-96.6,32.7,0</coordinates></Point></Placemark></Document></kml>")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(f), "")))
    win.import_ge_pins()

    got = {p.number: p for p in REF.reference_points(pr, "other")}
    assert set(got) == {"20", "21", "22", "23", "999"}, "every placemark comes in, matched to a point or not"
    assert all(p.layer == "OTHER" for p in got.values())
    p20 = pr.point_by_number("20")
    assert got["20"].x - p20.x == pytest.approx(2.0, abs=0.01)
    assert got["20"].y - p20.y == pytest.approx(1.0, abs=0.01)
    assert not pr.checks, "no check records are written any more"
    assert all(not REF.is_reference(pr.point_by_number(n)) for n in ("20", "21", "22", "23"))
    assert [p.number for p in REF.survey_points(pr)] == [p.number for p in pr.points.values() if not REF.is_reference(p)]

    # re-importing the same file updates the pins instead of doubling them
    win.import_ge_pins()
    assert len(REF.reference_points(pr, "other")) == 5

    # a corrupt file is reported, not raised
    bad = tmp_path / "bad.kml"
    bad.write_text("<kml><Placemark><name>1</name>")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(bad), "")))
    win.import_ge_pins()
    assert len(REF.reference_points(pr, "other")) == 5 and any("could not be read" in b for b in auto["boxes"])


# ------------------------------------------------------------------ misc
def test_save_dirty_open_roundtrip(win, app, auto, tmp_path, monkeypatch):
    st = win.state
    path = str(tmp_path / "job.plb")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (path, "")))
    assert not st.dirty
    with st.edit("x"):
        st.project.add_point(1.0, 2.0, 3.0)
    assert st.dirty and "*" in win.windowTitle()
    assert win.save()
    assert not st.dirty and "*" not in win.windowTitle() and os.path.exists(path)
    n = len(st.project.points)
    st.new_project("blank", C.ProjectCRS.local("m"))
    win.open_project_path(path)
    assert len(win.state.project.points) == n
    assert any("parent field-work folder" in text for text in auto["boxes"])


def test_plugin_menu_runs_command_as_one_undo_step(win, app, auto):
    from plumbline import plugins
    plugins.install_examples()
    win.rebuild_plugins_menu(load=True)
    cmd = next(c for c in plugins.registry.commands if c.name == "Label spot elevations")
    n = sum(1 for e in win.state.project.entities.values() if isinstance(e, TextEntity))
    win.run_plugin_command(cmd)                                        # ParamDialog auto-accepted with defaults
    n2 = sum(1 for e in win.state.project.entities.values() if isinstance(e, TextEntity))
    assert n2 > n
    win.undo()
    assert sum(1 for e in win.state.project.entities.values() if isinstance(e, TextEntity)) == n
    tools = next((m for m in getattr(win, "_plugin_submenus", {}).values() if m.title() == "Tools"), None) or next(a.menu() for a in list(win.m_plugins.actions()) if a.menu() and a.text() == "Tools")
    assert {"Round elevations", "Label spot elevations"} <= {a.text() for a in tools.actions()}
    assert any("(plugin)" in a.text() for a in win.m_export.actions())          # the example exporter joined File > Export


def test_failed_plugin_command_rolls_back(win, app, auto):
    from plumbline import plugins
    pr = win.state.project

    def bad(api):
        pr.add_point(1, 1, 1)
        raise RuntimeError("boom")

    cmd = plugins.Command("Test/Bad", bad, [], "")
    n = len(pr.points)
    win.run_plugin_command(cmd)
    assert len(win.state.project.points) == n and any("rolled back" in b for b in auto["boxes"])


def test_report_viewer_writes_pdf_html_csv_xlsx(win, app, auto, tmp_path):
    from plumbline.io import reports
    from plumbline.ui.dialogs import ReportViewer
    pr = win.state.project
    rv = ReportViewer(win, reports.points_report(pr))
    pdf = tmp_path / "r.pdf"
    rv.write_pdf(str(pdf))
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 5000
    rv2 = ReportViewer(win, reports.crs_report(pr))
    assert "NAD83" in rv2.html


def test_qa_dialog_finds_the_planted_bust_and_selects_it(win, app, auto):
    from plumbline.ui.dialogs import QADialog
    d = QADialog(win.state, win)
    kinds = [i.kind for i in d.issues]
    assert "spike" in kinds
    r = kinds.index("spike")
    d.tbl.selectRow(r)
    pump(app)
    assert len(win.state.sel_points) == 1
    bust = next(iter(win.state.sel_points))
    assert win.state.project.points[bust].desc == "GS"


def test_cogo_traverse_dialog_and_inverse(win, app, auto):
    from plumbline.ui.dialogs import TraverseDialog
    st = win.state
    d = TraverseDialog(st, win)
    d.sp_n.setValue(1000.0)
    d.sp_e.setValue(2000.0)
    rows = [("N0E", "100"), ("S90E", "100"), ("S0E", "100"), ("N90W", "99.5")]
    for r, (a, b) in enumerate(rows):
        from PySide6.QtWidgets import QTableWidgetItem
        d.tbl.setItem(r, 0, QTableWidgetItem(a))
        d.tbl.setItem(r, 1, QTableWidgetItem(b))
    pts, adj, res = d.compute()
    assert res["linear_error"] == pytest.approx(0.5) and res["precision"] == pytest.approx(399.5 / 0.5)
    assert "1 : 799" in d.out.toPlainText()
    n = sum(1 for _ in st.project.polylines())
    d._add("poly")
    assert sum(1 for _ in st.project.polylines()) == n + 1
    d2 = TraverseDialog(st, win)
    d2.ed_a.setText("1")
    d2.ed_b.setText("2")
    d2.inverse()
    assert "Bearing" in d2.inv_out.toPlainText()


def test_transform_dialog_rotates_everything(win, app, auto):
    from plumbline.ui.dialogs import TransformDialog
    pr = win.state.project
    p = pr.point_by_number("20")
    x_orig = p.x
    d0 = math.hypot(p.x - pr.point_by_number("24").x, p.y - pr.point_by_number("24").y)
    d = TransformDialog(win.state, win)
    d.sp_rot.setValue(37.0)
    d.sp_scale.setValue(2.0)
    d._try_accept()
    d.apply()
    q = pr.point_by_number("24")
    assert math.hypot(pr.point_by_number("20").x - q.x, pr.point_by_number("20").y - q.y) == pytest.approx(2 * d0)
    win.undo()
    assert win.state.project.point_by_number("20").x == pytest.approx(x_orig)


def test_command_line_info_and_sample(tmp_path, capsys):
    from plumbline import cli
    assert cli.main(["sample", str(tmp_path / "s")]) == 0
    assert cli.main(["info", str(tmp_path / "s" / "sample_site.plb")]) == 0
    out = capsys.readouterr().out
    assert "EPSG:6584" in out and "points: 238" in out
    assert cli.main(["export-dxf", str(tmp_path / "s" / "sample_site.plb"), str(tmp_path / "o.dxf")]) == 0
    script = tmp_path / "s.py"
    script.write_text("print(len(project.points))\n")
    assert cli.main(["run", str(script), str(tmp_path / "s" / "sample_site.plb")]) == 0
    assert "238" in capsys.readouterr().out
