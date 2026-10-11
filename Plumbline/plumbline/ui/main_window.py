"""The main window: menus, toolbars, docks, status bar and all the workflows that tie the pieces together."""
from __future__ import annotations

import math
import os
import threading
import time
import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDockWidget, QFileDialog, QFormLayout,
                               QHBoxLayout, QVBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSizePolicy, QSpinBox, QToolBar, QToolButton, QWidget)

from .. import __version__, plugins
from ..core import audit as AUD
from ..core import crs as C
from ..core import imagery as IM
from ..core import maps
from ..core import reference as REF
from ..core import units as U
from ..core.model import ImageryLayer, Polyline, Surface
from ..core.project import Project
from ..core.project_package import create_project_package
from ..core.qa import run_checks
from ..core.settings import settings, user_dir
from ..core.surface import (apply_contours, build_tin, compute_contour_data, gather_surface_inputs, surface_signature)
from ..io import f2f, kml_io, reports
from . import icons, theme
from .app_state import AppState
from .canvas import CanvasView
from .crs_dialog import CRSDialog
from .depthview import DepthPanel
from .dialogs import (AboutDialog, FeatureCodesDialog, HelpDialog, PluginsDialog, QADialog, ReportViewer, SettingsDialog,
                      TransformDialog, TraverseDialog, WelcomeDialog)
from .docks import ConsoleDock, FieldBookDock, LayersDock, MessagesDock, PointsDock, PropertiesDock, SurfacesDock
from .groups_dock import GroupsDock
from .export_dialogs import export_dxf, export_gis, export_kml, export_landxml, export_points_csv
from .f2f_dialog import ConvertFieldToFinishDialog
from .import_export import (IMPORT_FILTER, POINTS_FILTER, ImportPlan, Importer, ReferenceFolderDialog,
                            apply_import, describe_folder_import)
from .imagery_ui import AddImageryDialog, ImageryDock, _acc_text
from .new_project import NewProjectDialog
from .project_package_dialog import ProjectPackageDialog
from .surface_dialogs import ContourDialog, ProfileDialog, SurfaceDialog, VolumeDialog, build_report_text
from .tools import (ArcTool, DepthLineTool, DrawPolylineTool, ImageryNudgeTool, MeasureTool, MoveTool,
                    PanTool, PointTool, SelectTool, TextTool, ZoomWindowTool, parse_coordinate)
from .view3d import SceneProvider, View3DPanel
from .widgets import FormDialog, confirm, dspin, error_box, info_box, ispin, run_blocking


class ParamDialog(FormDialog):
    """Auto-built input dialog for a plugin command's Param list."""

    def __init__(self, cmd, parent=None):
        super().__init__(parent, cmd.name, cmd.description, "Run", 420)
        self.cmd = cmd
        self.w = {}
        for p in cmd.params:
            lbl = p.label or p.name
            if p.type is bool:
                w = QCheckBox()
                w.setChecked(bool(p.default))
            elif p.type is int:
                w = ispin(int(p.default or 0), int(p.minimum if p.minimum is not None else -10 ** 9),
                          int(p.maximum if p.maximum is not None else 10 ** 9))
            elif p.type is float:
                w = dspin(float(p.default or 0.0), p.minimum if p.minimum is not None else -1e12,
                          p.maximum if p.maximum is not None else 1e12, 4, 1.0)
            elif p.choices:
                w = QComboBox()
                w.addItems([str(c) for c in p.choices])
                w.setCurrentText(str(p.default))
            else:
                w = QLineEdit("" if p.default is None else str(p.default))
            self.w[p.name] = (p, w)
            self.form.addRow(lbl + ":", w)

    def values(self) -> dict:
        out = {}
        for name, (p, w) in self.w.items():
            if p.type is bool:
                out[name] = w.isChecked()
            elif p.type in (int, float):
                out[name] = w.value()
            elif isinstance(w, QComboBox):
                out[name] = w.currentText()
            else:
                out[name] = w.text()
        return out


class DockTitleBar(QWidget):
    """Custom dock widget title bar with Auto-Hide / Pin toggle, title, and close buttons."""

    def __init__(self, dock: QDockWidget, title: str):
        super().__init__(dock)
        self.dock = dock
        self.title_text = title
        self.pinned = True

        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(4, 2, 4, 2)
        self.lay.setSpacing(4)

        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet("font-weight: 600; font-size: 11px;")
        self.lay.addWidget(self.lbl_title, 1)

        self.btn_pin = QToolButton(self)
        self.btn_pin.setCheckable(True)
        self.btn_pin.setChecked(True)
        self.btn_pin.setToolTip("Pin palette (keep open) / Unpin to auto-hide")
        self.btn_pin.setText("📌")
        self.btn_pin.setStyleSheet("QToolButton { border: none; background: transparent; font-size: 11px; padding: 1px 2px; } "
                                   "QToolButton:hover { background: rgba(128, 128, 128, 0.25); border-radius: 2px; }")
        self.btn_pin.toggled.connect(self._on_pin_clicked)
        self.lay.addWidget(self.btn_pin)

        self.btn_close = QToolButton(self)
        self.btn_close.setText("✕")
        self.btn_close.setToolTip("Close palette")
        self.btn_close.setStyleSheet("QToolButton { border: none; background: transparent; font-size: 10px; padding: 1px 2px; font-weight: bold; } "
                                     "QToolButton:hover { background: rgba(255, 60, 60, 0.3); border-radius: 2px; }")
        self.btn_close.clicked.connect(self.dock.close)
        self.lay.addWidget(self.btn_close)

    def set_pinned_state(self, checked: bool):
        self.pinned = checked
        self.btn_pin.blockSignals(True)
        self.btn_pin.setChecked(checked)
        self.btn_pin.blockSignals(False)

        w = self.dock.widget()
        if checked:
            self.btn_pin.setText("📌")
            self.btn_pin.setToolTip("Pinned (click to auto-hide)")
            self.lbl_title.setVisible(True)
            self.btn_close.setVisible(True)
            if w:
                w.setVisible(True)
        else:
            self.btn_pin.setText("📍")
            self.btn_pin.setToolTip("Auto-hide collapsed (click pin to expand)")
            self.lbl_title.setVisible(False)
            self.btn_close.setVisible(False)
            if w:
                w.setVisible(False)

    def _on_pin_clicked(self, checked: bool):
        mw = self.dock.parent()
        if isinstance(mw, QMainWindow) and hasattr(mw, "set_dock_group_pinned"):
            mw.set_dock_group_pinned(self.dock, checked)
        else:
            self.set_pinned_state(checked)

    def mousePressEvent(self, event):
        if not self.pinned:
            mw = self.dock.parent()
            if isinstance(mw, QMainWindow) and hasattr(mw, "set_dock_group_pinned"):
                mw.set_dock_group_pinned(self.dock, True)
            else:
                self.set_pinned_state(True)
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, project: Project | AppState | None = None, open_path: str | None = None, welcome: bool = False):
        super().__init__()
        self.state = project if isinstance(project, AppState) else AppState(project)
        self.state._save_untitled_before_edit = self.save_as
        self.setWindowIcon(icons.app_icon())
        self.resize(1480, 920)
        self._icon_actions: list = []
        self._imagery_nudge_tool = None
        s = settings()
        theme.apply_theme(QApplication.instance(), s.get("theme"))
        C.set_proj_network(bool(s.get("proj_network")))

        self.canvas = CanvasView(self.state)
        self.setCentralWidget(self.canvas)
        self.importer = Importer(self)
        self._build_tools()
        self._build_docks()
        self._build_actions()
        self._build_toolbars()
        self._build_menus()
        self._build_statusbar()
        self._wire()
        self.rebuild_plugins_menu(load=True)
        # Pan is the tool a surveyor starts in: it is the only one that cannot change data,
        # and Esc comes back to it from anywhere (change order, item 8).
        self.set_tool("pan")
        self.update_title()
        self.state.log(f"Plumbline {__version__} ready.", "info")
        if open_path:
            self.open_project_path(open_path)
        elif welcome and s.get("show_welcome", True):
            QTimer.singleShot(150, self.show_welcome)

    # ================================================================== construction
    def _build_tools(self):
        c = self.canvas
        self.tools = {"select": SelectTool(c), "pan": PanTool(c),
                      "polyline": DrawPolylineTool(c), "arc": ArcTool(c), "point": PointTool(c),
                      "text": TextTool(c), "measure": MeasureTool(c), "area": MeasureTool(c, area=True), "move": MoveTool(c),
                      "zoom_window": ZoomWindowTool(c),
                      "depth_line": DepthLineTool(c, on_pick=self._depth_line_picked)}

    def _dock(self, title, widget, area, name):
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
        title_bar = DockTitleBar(d, title)
        d.setTitleBarWidget(title_bar)
        self.addDockWidget(area, d)
        return d

    def set_dock_group_pinned(self, source_dock: QDockWidget, pinned: bool):
        """Set pinned / collapsed state for all tabified docks in the palette group."""
        area = self.dockWidgetArea(source_dock)
        group = [source_dock] + self.tabifiedDockWidgets(source_dock)
        for d in self.findChildren(QDockWidget):
            if d.isVisible() and self.dockWidgetArea(d) == area and d not in group:
                group.append(d)

        for d in group:
            tb = d.titleBarWidget()
            if isinstance(tb, DockTitleBar):
                tb.set_pinned_state(pinned)

        if pinned:
            if area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
                width = 340 if area == Qt.LeftDockWidgetArea else 280
                self.resizeDocks(group, [width] * len(group), Qt.Horizontal)
            elif area == Qt.BottomDockWidgetArea:
                self.resizeDocks(group, [210] * len(group), Qt.Vertical)
        else:
            # Collapse down to a compact box only showing the pin
            if area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
                self.resizeDocks(group, [28] * len(group), Qt.Horizontal)
            elif area == Qt.BottomDockWidgetArea:
                self.resizeDocks(group, [28] * len(group), Qt.Vertical)

    def _build_docks(self):
        st, cv = self.state, self.canvas
        self.layers = LayersDock(st)
        self.groups = GroupsDock(st)
        self.surfaces = SurfacesDock(st)
        self.imagery = ImageryDock(st, cv)
        self.fieldbook_dock = FieldBookDock(st)
        self.props = PropertiesDock(st)
        self.points = PointsDock(st)
        self.messages = MessagesDock(st)
        self.console = ConsoleDock(st, cv)

        # Left dock area: Layers, Surfaces, Imagery, Groups, Points
        self.d_layers = self._dock("Layers", self.layers, Qt.LeftDockWidgetArea, "layers")
        self.d_surf = self._dock("Surfaces", self.surfaces, Qt.LeftDockWidgetArea, "surfaces")
        self.d_img = self._dock("Imagery", self.imagery, Qt.LeftDockWidgetArea, "imagery")
        self.d_groups = self._dock("Groups", self.groups, Qt.LeftDockWidgetArea, "groups")
        self.d_pts = self._dock("Points", self.points, Qt.LeftDockWidgetArea, "points")

        # Right dock area: Properties, Field Book
        self.d_props = self._dock("Properties", self.props, Qt.RightDockWidgetArea, "props")
        self.d_fieldbook = self._dock("Field Book", self.fieldbook_dock, Qt.RightDockWidgetArea, "fieldbook")

        # Bottom dock area: Messages, Python Console
        self.d_msg = self._dock("Messages", self.messages, Qt.BottomDockWidgetArea, "messages")
        self.d_con = self._dock("Python Console", self.console, Qt.BottomDockWidgetArea, "console")

        self.tabifyDockWidget(self.d_layers, self.d_surf)
        self.tabifyDockWidget(self.d_surf, self.d_img)
        self.tabifyDockWidget(self.d_img, self.d_groups)
        self.tabifyDockWidget(self.d_groups, self.d_pts)
        self.d_layers.raise_()

        self.tabifyDockWidget(self.d_props, self.d_fieldbook)
        self.d_props.raise_()

        self.tabifyDockWidget(self.d_msg, self.d_con)
        self.d_msg.raise_()

        self.resizeDocks([self.d_layers, self.d_props], [340, 280], Qt.Horizontal)
        self.resizeDocks([self.d_msg], [160], Qt.Vertical)
        self.d_con.hide()
        # the 3D view and the depth view: docked next to the plan the first time they are opened (see _toggle_view_dock)
        self.scene_provider = SceneProvider(st)
        self.view3d = View3DPanel(st, self.scene_provider)
        self.depth = DepthPanel(st, self.scene_provider, cv)
        self.d_3d = self._dock("3D View", self.view3d, Qt.RightDockWidgetArea, "view3d")
        self.d_depth = self._dock("Depth View", self.depth, Qt.RightDockWidgetArea, "depthview")
        self.d_3d.hide()
        self.d_depth.hide()
        self.depth.pick_line_requested.connect(lambda: self.set_tool("depth_line"))
        self.depth.view.changed.connect(cv.update)

    def _act(self, text, slot=None, shortcut=None, icon=None, checkable=False, tip=None, checked=False):
        a = QAction(text, self)
        if icon:
            a.setIcon(icons.icon(icon))
            self._icon_actions.append((a, icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        if checkable:
            a.setChecked(checked)
        if tip:
            a.setToolTip(tip if not shortcut else f"{tip} ({QKeySequence(shortcut).toString()})")
            a.setStatusTip(tip)
        if slot:
            (a.toggled if checkable else a.triggered).connect(slot)
        return a

    def _build_actions(self):
        A = self._act
        # ---- file
        self.a_new = A("&New Project...", self.new_project, "Ctrl+N", "new", tip="New project")
        self.a_open = A("&Open Project...", self.open_project, "Ctrl+O", "open", tip="Open a project")
        self.a_sample = A("Open Synthetic &Sample (A Computer-Generated Lot)", self.open_sample,
          tip="A clean synthetic site - the right sample for learning the drawing tools")
        self.a_sample_real = A("Open &Real World Sample (A Real Reduced Job)", self.open_sample_real,
                tip="A real download: 3 crews, 3,773 points, real duplicate numbers "
                                   "and the office's own feature codes")
        self.a_save = A("&Save", self.save, "Ctrl+S", "save", tip="Save project")
        self.a_saveas = A("Save &As...", self.save_as, "Ctrl+Shift+S")
        self.a_import = A("&Import...", self.import_dialog, "Ctrl+I", "import", tip="Import points / LandXML / DXF / GIS / KML")
        self.a_exp_dxf = A("&DXF...", lambda: export_dxf(self))
        self.a_exp_lx = A("&LandXML...", lambda: export_landxml(self))
        self.a_exp_gis = A("&GIS (GeoPackage / Shapefile / GeoJSON)...", lambda: export_gis(self))
        self.a_exp_kml = A("&Google Earth (KML / KMZ)...", self._export_kml)
        self.a_exp_csv = A("&Points (Text / CSV)...", lambda: export_points_csv(self))
        self.a_exp_png = A("Drawing as &Image (PNG)...", self.export_png)
        self.a_quit = A("E&xit", self.close, "Ctrl+Q")
        # ---- edit
        self.a_undo = A("&Undo", self.undo, "Ctrl+Z", "undo")
        self.a_redo = A("&Redo", self.redo, "Ctrl+Y", "redo")
        self.a_selall = A("Select &All", self.select_all, "Ctrl+A")
        self.a_delete = A("&Delete", self.delete_selection, "Delete", "trash", tip="Delete the selection")
        self.a_find = A("&Find Point...", self.find_point, "Ctrl+F", "search")
        self.a_transform = A("&Translate / Rotate / Scale...", self.transform_dialog, icon="move")
        self.a_settings = A("&Settings...", self.settings_dialog, icon="settings")
        # ---- view
        self.a_ext = A("Zoom &Extents", self.state.zoom_extents, "Ctrl+0", "zoom_extents", tip="Zoom to everything")
        self.a_zsel = A("Zoom to &Selection", self.zoom_selection, icon="zoom_selected")
        self.a_zin = A("Zoom &In", lambda: self.canvas.zoom_at(self.canvas.width() / 2, self.canvas.height() / 2, 1.5))
        self.a_zout = A("Zoom &Out", lambda: self.canvas.zoom_at(self.canvas.width() / 2, self.canvas.height() / 2, 1 / 1.5))
        o = self.canvas.opts
        self.view_toggles = {}
        for key, text, default in (("show_points", "Points", o.show_points), ("show_numbers", "Point Numbers", o.show_numbers),
                                   ("show_elev", "Point Elevations", o.show_elev), ("show_desc", "Point Descriptions", o.show_desc),
                                   ("show_lines", "Linework", o.show_lines), ("show_text", "Text", o.show_text),
                                   ("show_imagery", "Imagery", o.show_imagery),
                                   ("show_grid", "Coordinate Grid", o.show_grid), ("show_scalebar", "Scale Bar", o.show_scalebar)):
            a = A(text, lambda on, k=key: self.canvas.set_option(k, bool(on)), checkable=True, checked=default)
            self.view_toggles[key] = a
        self.a_snap = A("Object &Snap", self._snap_toggle, "F3", "snap", checkable=True, checked=True, tip="Toggle snapping")
        self.snap_acts = {}
        for key, text in (("point", "Survey Points"), ("node", "Vertices"), ("mid", "Midpoints"), ("nearest", "Nearest on Line")):
            self.snap_acts[key] = A(text, lambda on, k=key: self._snap_mode(k, on), checkable=True, checked=True)
        self.a_theme = A("Toggle &Dark / Light", self.toggle_theme, icon="sun")
        self.a_3d = A("&3D View", lambda on: self._toggle_view_dock(self.d_3d, self.d_depth, on), "Ctrl+3", "orbit", checkable=True,
                      tip="Orbit around the surface, points and linework in 3D")
        self.a_depth = A("&Depth View", lambda on: self._toggle_view_dock(self.d_depth, self.d_3d, on), "Ctrl+4", "section", checkable=True,
          tip="A side-on view you can rotate to any bearing")
        # ---- tools
        self.tool_group = QActionGroup(self)
        self.tool_acts = {}
        for key, text, icon, tip, sc in (("pan", "Pan", "pan", "Drag the view - the default tool, and Esc comes back here", None),
                                         ("select", "Select", "select", "Select objects", "S"),
                                         ("polyline", "Polyline", "polyline", "Draw a polyline", "L"),
                                         ("arc", "Arc (3 Point)", "arc", "Draw an arc through three points", "A"),
                                         ("point", "Survey Point", "point", "Place survey points", "P"),
                                         ("text", "Text", "text", "Place text", "T"),
                                         ("measure", "Distance / Bearing", "measure", "Measure distances and bearings", "M"),
                                         ("area", "Area", "area", "Measure an area", None),
                                         ("move", "Move", "move", "Move the selection", None),
                                         ("zoom_window", "Zoom Window", "zoom_window", "Zoom to a window", "Z"),
                                         ("depth_line", "Depth Line", "section_line", "Pick the line the depth view looks across", None)):
            a = A(text, lambda on, k=key: on and self.set_tool(k), sc, icon, checkable=True, tip=tip)
            self.tool_group.addAction(a)
            self.tool_acts[key] = a
        # ---- survey
        self.a_fix_points = A("Fix &Point Errors...", self.open_fix_point_errors, icon="check_points",
                              tip="Fix Point Errors (closeness duplicates, unknown codes, descriptions)")
        self.a_fix_linework = A("Fix &Linework...", self.open_fix_linework, icon="check_lines",
                                tip="Fix Linework (missing start/end, curve commands, bowties, reclass/reorder)")
        self.a_qa = self.a_fix_points
        self.a_codes = A("Apply &Feature Codes to All Points", self.apply_codes, icon="tag")
        self.a_linework = A("Process &Linework", self.process_linework, icon="polyline")
        self.a_join_points = A("&Create Linework from Selected Points...", self.join_selected_points_dialog, icon="polyline",
                               tip="Recode selected points in description coding to form a linework figure")
        self.a_edit_linework_coding = A("&Edit Linework Coding (Point Coder)...", self.edit_linework_coding_dialog, icon="polyline",
                                        tip="Inspect, reverse, close/open, or recode figure points")
        self.a_edit_line_geometry = A("Edit Line &Geometry...", self.edit_selected_line_geometry, icon="polyline",
                                     tip="Stage vertex, curve, endpoint, and geometry edits with a preview")
        self.a_join_lines = A("Join Selected &Lines...", self.join_selected_lines_dialog, icon="polyline",
                              tip="Draft, orient, average, condense, preview, and accept selected line joins")
        self.a_fieldbook = A("&Field Book...", self.open_fieldbook_dialog, icon="layers",
                             tip="Field Book: Convert Carlson code table, Select/Pull field book, or View field book report")
        self.a_f2f = self.a_fieldbook
        self.a_codetable = A("Feature Code &Table...", self.codes_dialog)
        self.a_cogo = A("&COGO — Traverse and Inverse...", self.cogo_dialog, icon="inverse")
        # ---- surface
        self.a_sf_new = A("&Create Surface...", lambda: self.create_surface(None), icon="surface")
        self.a_sf_edit = A("&Edit / Rebuild Surface...", self.edit_surface)
        self.a_sf_ctr = A("Create C&ontours...", self.contours_for_active, icon="contour")
        self.a_sf_vol = A("&Volumes...", self.volumes, icon="volume")
        self.a_sf_prof = A("&Profile of Selected Polyline...", self.profile_active, icon="profile")
        self.a_sf_rep = A("Surface &Report...", self.report_surface_active, icon="report")
        # ---- imagery
        self.a_img_add = A("&Add Imagery...", self.add_imagery, icon="image")
        self.a_img_ge_in = A("Import &Google Earth Pins...", self.import_ge_pins)
        self.a_gmaps = A("Open View Center in &Google Maps (Satellite)", lambda: self.open_google_maps(True), "Ctrl+Shift+M", "pin",
          tip="Open the middle of the plan view in Google Maps, satellite layer")
        self.a_gmaps_map = A("Open View Center in Google Maps (Street &Map)", lambda: self.open_google_maps(False))
        self.a_gmaps_copy = A("Copy Google Maps &Link for View Center", lambda: self.open_google_maps(True, copy_only=True))
        # ---- coordinates
        self.a_crs = A("&Project Coordinate System...", self.crs_dialog, icon="globe", tip="Coordinate system manager")
        self.a_calc = A("Coordinate &Calculator...", lambda: self.crs_dialog(calculator=True))
        # ---- reports
        self.a_r_pts = A("&Point List", self.report_points, icon="table")
        self.a_r_lines = A("&Line and Curve Table (Selected Polylines)", self.report_polylines)
        self.a_r_qa = A("&Data Quality", self.report_qa)
        self.a_r_audit = A("&Point(s) Audit...", self.report_audit,
                           icon="table",
            tip="The job's field points against the state they were imported in: "
                               "what is missing, what was added, what moved, what changed")
        self.a_r_crs = A("&Coordinate System", self.report_crs)
        # ---- the field-data half of the program.  It opens from Survey now: there is no Tools
        #      menu any more - a folder and a window are not tools.
        self.a_fw_manager = A("&Fieldwork Manager...", self.open_fieldwork,
               tip="Reduce raw field data: duplicate checks, description parsing, renumbering")
        self.a_fw_import = A("Import &Cleaned Field Data...", self.import_fieldwork,
              tip="Bring a consolidated .fwk into this project through the field-data checks")
        self.a_points_table = A("Survey &Points Table", self.open_survey_points, icon="table",
              tip="Open the searchable survey-points table")
        self.a_ref_file = A("Import Points from &File...", self.import_points_file,
             tip="A CSV of stake-out, control or other reference coordinates.  "
                                "One layer per role, and the points stay out of the fieldwork list.")
        self.a_file_crs = A("File &Coordinate Systems...", self.file_crs_dialog,
             tip="Set a file's coordinates to the project system, reproject them, or correct their "
                 "ground/grid Surface Adjustment Factor (SAF).")
        self.a_ref_folder = A("Import Points from F&older...", self.import_points_folder,
               tip="Every point file in a folder, as one import.  The default is this "
                                  "job's field data; choose a reference role for somebody else's "
                                  "control or stake-out list.  The folder is recorded on the job, so "
                                  "the next file dialog starts there.")
        # ---- the job folder, under File where the project lives
        # One entry for the folder the job lives in (change order, item 12): open it, open
        # another one, create a new job folder, or hand it to the desktop's file manager.
        self.a_project_folder = A("&Project Folder...", self.project_folder_dialog, icon="open",
                   tip="The folder this job lives in: open it, open another, or create a "
                                      "new job folder.  One place, because the project file and the "
                                      "Field Data folder belong to the same job.")
        self.a_job_select = self.a_project_folder        # compatibility alias
        # ---- help
        self.a_help = A("&Quick Start", lambda: HelpDialog(self).exec(), "F1")
        self.a_licence = A("&Licence Agreement...", self.show_licence)

        self.a_about = A("&About Plumbline", lambda: AboutDialog(self).exec())

    def _build_menus(self):
        mb = self.menuBar()
        self._top_menus = []
        self._all_menus = []
        def add_menu(title, parent=mb):
            menu = parent.addMenu(title)
            self._all_menus.append(menu)
            if parent is mb:
                self._top_menus.append(menu)
            return menu

        m = add_menu("&File")
        m.addActions([self.a_new, self.a_open])
        m_samp = add_menu("Open Sa&mple Project", m)
        m_samp.addActions([self.a_sample, self.a_sample_real])
        self.m_recent = add_menu("Open &Recent", m)
        self.m_recent.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        m.addActions([self.a_save, self.a_saveas])
        m.addSeparator()
        m.addAction(self.a_project_folder)
        m.addSeparator()
        m.addAction(self.a_import)
        me = add_menu("&Export", m)
        me.addActions([self.a_exp_dxf, self.a_exp_lx, self.a_exp_gis, self.a_exp_kml, self.a_exp_csv])
        self.m_export = me
        m.addAction(self.a_exp_png)
        m.addSeparator()
        m.addAction(self.a_quit)
        m = add_menu("&Edit")
        m.addActions([self.a_undo, self.a_redo])
        m.addSeparator()
        m.addActions([self.a_selall, self.a_delete, self.a_find])
        m.addSeparator()
        m.addActions([self.a_transform, self.a_settings])
        m = add_menu("&View")
        m.addActions([self.a_ext, self.a_zsel, self.a_zin, self.a_zout, self.tool_acts["zoom_window"]])
        m.addSeparator()
        m.addActions([self.a_3d, self.a_depth, self.tool_acts["depth_line"]])
        m.addSeparator()
        mv = add_menu("&Show", m)
        mv.addActions(list(self.view_toggles.values()))
        ms = add_menu("S&nap", m)
        ms.addAction(self.a_snap)
        ms.addSeparator()
        ms.addActions(list(self.snap_acts.values()))
        m.addAction(self.a_theme)
        m.addSeparator()
        mtb = add_menu("&Toolbars", m)
        mtb.addAction(self.tb_draw_tools.toggleViewAction())
        mtb.addAction(self.tb_survey.toggleViewAction())
        mtb.addAction(self.tb_draw_controls.toggleViewAction())
        md = add_menu("&Panels", m)
        for d in (self.d_layers, self.d_surf, self.d_img, self.d_groups, self.d_pts, self.d_props, self.d_fieldbook,
                  self.d_msg, self.d_con, self.d_3d, self.d_depth):
            md.addAction(d.toggleViewAction())
        md.addSeparator()
        md.addMenu(self.points.col_menu)          # the point list's own columns live here too
        m = add_menu("&Draw")
        for k in ("select", "polyline", "arc", "point", "text", "measure", "area", "move"):
            m.addAction(self.tool_acts[k])
        m = add_menu("&Survey")
        # the field-data half of the program opens from here
        m.addActions([self.a_fw_manager, self.a_fw_import, self.a_points_table])
        m.addSeparator()
        m.addActions([self.a_ref_file, self.a_ref_folder])
        m.addSeparator()
        m.addActions([self.a_crs, self.a_calc, self.a_file_crs])
        m.addSeparator()
        m.addActions([self.a_fix_points, self.a_fix_linework, self.a_fieldbook, self.a_codes, self.a_linework,
                      self.a_join_points, self.a_join_lines, self.a_edit_line_geometry, self.a_edit_linework_coding])
        m.addSeparator()
        m.addActions([self.a_cogo, self.a_transform])
        m = add_menu("S&urface")
        m.addActions([self.a_sf_new, self.a_sf_edit, self.a_sf_ctr, self.a_sf_vol, self.a_sf_prof, self.a_sf_rep])
        m = add_menu("&Imagery")
        m.addAction(self.a_img_add)
        m.addSeparator()
        m.addActions([self.a_gmaps, self.a_gmaps_copy])
        m = add_menu("&Reports")
        m.addActions([self.a_r_pts, self.a_r_lines, self.a_r_qa, self.a_r_audit, self.a_r_crs, self.a_sf_rep])
        # (there is no Tools menu: its four items were a folder, a window and two imports, and
        #  each of those now sits in the menu it belongs to - File and Survey)
        self.m_plugins = add_menu("&Plugins")
        m = add_menu("&Help")
        m.addActions([self.a_help, self.a_licence, self.a_about])

    def _build_toolbars(self):
        def tb_top(title, name):
            t = QToolBar(title, self)
            t.setObjectName(name)
            t.setIconSize(QSize(20, 20))
            t.setMovable(True)
            self.addToolBar(Qt.ToolBarArea.TopToolBarArea, t)
            return t

        # TOP toolbars: Draw Tools + Survey (Row 1), Draw Controls (Row 2)
        self.tb_draw_tools = tb_top("Draw Tools", "tb_draw_tools")
        for k in ("pan", "select", "polyline", "arc", "point", "text", "measure", "area", "move"):
            self.tb_draw_tools.addAction(self.tool_acts[k])
        self.tb_draw_tools.addSeparator()
        self.tb_draw_tools.addActions([self.tool_acts["zoom_window"], self.a_ext, self.a_zsel])
        self.tb_draw_tools.addSeparator()
        self.tb_draw_tools.addAction(self.a_snap)

        self.tb_survey = tb_top("Survey", "tb_survey")
        self.tb_survey.addActions([self.a_crs, self.a_fix_points, self.a_fix_linework, self.a_fieldbook, self.a_sf_new, self.a_sf_ctr, self.a_sf_vol, self.a_sf_prof])
        self.tb_survey.addSeparator()
        self.tb_survey.addActions([self.a_img_add, self.a_gmaps])
        self.tb_survey.addSeparator()
        self.tb_survey.addActions([self.a_3d, self.a_depth])

        # Place draw controls on a new line below other toolbars
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self.tb_draw_controls = tb_top("Draw Controls", "tb_draw_controls")
        w_draw = QWidget()
        lay_draw = QHBoxLayout(w_draw)
        lay_draw.setContentsMargins(4, 1, 4, 1)

        lay_draw.addWidget(QLabel("Layer:"))
        self.cmb_layer = QComboBox()
        self.cmb_layer.setMinimumWidth(160)
        self.cmb_layer.setToolTip("Layer new objects are drawn on")
        lay_draw.addWidget(self.cmb_layer)

        lay_draw.addWidget(QLabel(" Desc:"))
        self.ed_desc = QLineEdit()
        self.ed_desc.setPlaceholderText("e.g. EP")
        self.ed_desc.setMaximumWidth(120)
        lay_draw.addWidget(self.ed_desc)

        lay_draw.addWidget(QLabel(" Type:"))
        self.cmb_ptrole = QComboBox()
        self.cmb_ptrole.setMinimumWidth(140)
        self.cmb_ptrole.setToolTip("What the point tool drops.\n\n"
                                   "Reference points (stake-out / control / other) go on their own layer with their own\n"
                                   "point numbers, and are kept out of the fieldwork list and the data-quality checks.")
        self.cmb_ptrole.addItem("Field data", "")
        for role in REF.ROLES:
            self.cmb_ptrole.addItem(f"{REF.ROLE_LABELS[role]}", role)
        lay_draw.addWidget(self.cmb_ptrole)

        lay_draw.addWidget(QLabel(" Command:"))
        self.ed_cmd = QLineEdit()
        self.ed_cmd.setPlaceholderText("N,E  @dN,dE  @dist<bearing  point#  - Enter")
        self.ed_cmd.setMinimumWidth(260)
        lay_draw.addWidget(self.ed_cmd)
        self.tb_draw_controls.addWidget(w_draw)

    def _build_statusbar(self):
        sb = self.statusBar()
        self.lbl_hint = QLabel("")
        self.lbl_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_hint.setMinimumWidth(120)
        sb.addWidget(self.lbl_hint, 1)
        self.lbl_coord = QLabel("")
        self.lbl_coord.setMinimumWidth(330)
        self.lbl_coord.setFont(theme.mono_font())
        self.lbl_ll = QLabel("")
        self.lbl_ll.setFont(theme.mono_font())
        self.lbl_crs = QPushButton("")
        self.lbl_crs.setFlat(True)
        self.lbl_crs.clicked.connect(self.crs_dialog)
        self.lbl_crs.setToolTip("Click to open the coordinate system manager")
        self.lbl_tiles = QLabel("")
        for w in (self.lbl_tiles, self.lbl_coord, self.lbl_ll, self.lbl_crs):
            sb.addPermanentWidget(w)

    def _wire(self):
        st, cv = self.state, self.canvas
        st.dirty_changed.connect(lambda d: self.update_title())
        st.project_replaced.connect(self._project_replaced)
        st.undo_changed.connect(self._undo_state)
        st.changed.connect(lambda k: self._refresh_layer_combo())
        cv.cursor_moved.connect(self._cursor)
        cv.hint_changed.connect(self._hint)
        cv.escape_pressed.connect(self._escape)
        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.WindowShortcut)
        self._escape_shortcut.activated.connect(self._escape)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._update_escape_shortcut)
            self._update_escape_shortcut(None, app.focusWidget())
        cv.files_dropped.connect(self._files_dropped)
        self.layers.current_changed.connect(self._current_layer)
        self.cmb_layer.activated.connect(lambda i: self.layers.set_current(self.cmb_layer.currentText()))
        self.ed_desc.textChanged.connect(lambda t: setattr(self.canvas, "point_desc", t.strip()))
        self.cmb_ptrole.currentIndexChanged.connect(
            lambda i: setattr(self.canvas, "point_role", self.cmb_ptrole.currentData() or ""))
        self.ed_cmd.returnPressed.connect(self._command_entered)
        self.surfaces.new_requested.connect(lambda: self.create_surface(None))
        self.surfaces.edit_requested.connect(lambda i: self.create_surface(self.state.project.surfaces.get(i)))
        self.surfaces.contours_requested.connect(self.contours_for)
        self.surfaces.volumes_requested.connect(self.volumes)
        self.surfaces.profile_requested.connect(self.profile_for)
        self.surfaces.report_requested.connect(self.report_surface)
        self.surfaces.delete_requested.connect(self.delete_surface)
        self.d_3d.visibilityChanged.connect(lambda vis: self._view_visibility(self.a_3d, self.d_3d, vis))
        self.d_depth.visibilityChanged.connect(lambda vis: self._view_visibility(self.a_depth, self.d_depth, vis))
        self.imagery.add_requested.connect(self.add_imagery)
        self.imagery.ge_import_requested.connect(self.import_ge_pins)
        self.imagery.ge_export_requested.connect(self._export_kml)
        self.imagery.nudge_by_points_requested.connect(self.toggle_imagery_nudge)
        self._tile_timer = QTimer(self)
        self._tile_timer.timeout.connect(self._tile_status)
        self._tile_timer.start(400)
        self._refresh_layer_combo()
        self._undo_state()
        self._project_replaced()

    # ================================================================== housekeeping
    def update_title(self):
        pr = self.state.project
        self.setWindowTitle(f"{pr.name}{' *' if self.state.dirty else ''} — Plumbline")

    def _hint(self, text: str):
        self.lbl_hint.setText(text)
        self.lbl_hint.setToolTip(text)

    def _undo_state(self):
        st = self.state
        self.a_undo.setEnabled(bool(st.undo_stack))
        self.a_redo.setEnabled(bool(st.redo_stack))
        self.a_undo.setText(f"&Undo {st.undo_stack[-1][0]}" if st.undo_stack else "&Undo")
        self.a_redo.setText(f"&Redo {st.redo_stack[-1][0]}" if st.redo_stack else "&Redo")

    def _project_replaced(self):
        self.set_tool("pan")
        self.layers.current = "0"
        self.canvas.current_layer = "0"
        self._refresh_layer_combo()
        self.update_title()
        crs = self.state.project.crs
        self._crs_label()
        self.lbl_ll.setVisible(not crs.is_local)

    def _refresh_layer_combo(self):
        pr = self.state.project
        self.cmb_layer.blockSignals(True)
        self.cmb_layer.clear()
        names = sorted(pr.layers, key=lambda n: (n != "0", n))
        self.cmb_layer.addItems(names)
        self.cmb_layer.setCurrentText(self.canvas.current_layer if self.canvas.current_layer in pr.layers else "0")
        self.cmb_layer.blockSignals(False)
        self._crs_label()
        self.lbl_ll.setVisible(not pr.crs.is_local)

    def _crs_label(self):
        crs = self.state.project.crs
        short = "UNASSIGNED (no CRS)" if crs.is_local else crs.authority
        if crs.ground.enabled:
            short += " (ground)"
        if crs.is_local:
            self.lbl_crs.setStyleSheet("color: #e0a030; font-weight: 600;")   # amber = needs attention
        else:
            self.lbl_crs.setStyleSheet("")
        self.lbl_crs.setText(short + f"  [{U.LABEL.get(crs.unit, crs.unit)}]")
        self.lbl_crs.setToolTip(crs.label + "\nClick to open the coordinate system manager")

    def _current_layer(self, name):
        self.canvas.current_layer = name
        self._refresh_layer_combo()

    def _cursor(self, x, y, z):
        pr = self.state.project
        ne = settings().get("coord_order") == "NE"
        a = f"N {y:>14,.3f}   E {x:>14,.3f}" if ne else f"X {x:>14,.3f}   Y {y:>14,.3f}"
        if math.isfinite(z):
            a += f"   Z {z:>9,.3f}"
        self.lbl_coord.setText(a)
        if not pr.crs.is_local:
            try:
                lon, lat = pr.crs.to_lonlat(x, y)
                self.lbl_ll.setText(f"{float(lat):.6f}, {float(lon):.6f}")
            except Exception:
                self.lbl_ll.setText("")

    def _tile_status(self):
        n = self.canvas.imagery.pending
        self.lbl_tiles.setText(f"loading {n} tile(s)... " if n else "")

    def _files_dropped(self, files):
        for f in files:
            self.importer.import_path(f)

    def maybe_save(self) -> bool:
        if not self.state.dirty:
            return True
        m = QMessageBox(self)
        m.setWindowTitle("Unsaved Changes")
        m.setText(f"Save changes to '{self.state.project.name}' before continuing?")
        b_save = m.addButton("Save", QMessageBox.AcceptRole)
        m.addButton("Discard", QMessageBox.DestructiveRole)
        b_cancel = m.addButton("Cancel", QMessageBox.RejectRole)
        m.exec()
        if m.clickedButton() is b_cancel:
            return False
        if m.clickedButton() is b_save:
            return self.save()
        return True

    def closeEvent(self, ev):
        if not self.maybe_save():
            ev.ignore()
            return
        self.state.tiles.shutdown()
        super().closeEvent(ev)

    # ================================================================== tools
    def set_tool(self, name: str):
        tool = self.tools[name]
        if self.canvas.tool is not tool:
            self.canvas.set_tool(tool)
        a = self.tool_acts.get(name)
        if a is not None and not a.isChecked():
            a.blockSignals(True)
            a.setChecked(True)
            a.blockSignals(False)

    def _update_escape_shortcut(self, _old_focus, focus=None):
        """Enable the window shortcut for toolbar/panel focus, leaving canvas Esc to its tool."""
        widget = focus
        while widget is not None:
            if widget is self.canvas or widget == self.canvas:
                self._escape_shortcut.setEnabled(False)
                return
            widget = widget.parentWidget()
        self._escape_shortcut.setEnabled(True)

    def _escape(self):
        """Cancel the current tool/action and return keyboard focus and cursor to Pan."""
        tool = self.canvas.tool
        if tool is not None and tool.name != "pan":
            try:
                tool.cancel()
            except Exception:
                pass
            self.state.log("Tool released - Pan.  Drag to move the view.", "info")
        self.set_tool("pan")
        self.canvas._panning = False
        self.canvas._pan_last = None
        self.canvas.setCursor(self.tools["pan"].cursor)
        self.canvas.setFocus(Qt.OtherFocusReason)

    def _command_entered(self):
        text = self.ed_cmd.text().strip()
        tool = self.canvas.tool
        if not text or tool is None:
            return
        pr = self.state.project
        try:
            ref = tool.last_point()
            x, y = parse_coordinate(text, pr, ref, settings().get("coord_order"))
        except ValueError as ex:
            self.lbl_hint.setText(str(ex))
            return
        if tool.name in ("select", "zoom_window", "text"):
            self.state.center_on(x, y)
        else:
            tool.enter_coordinate(x, y)
        self.ed_cmd.clear()
        self.canvas.setFocus()

    def _snap_toggle(self, on):
        self.canvas.snap_enabled = bool(on)
        self.canvas.update()

    def _snap_mode(self, key, on):
        (self.canvas.snap_modes.add if on else self.canvas.snap_modes.discard)(key)

    def toggle_theme(self):
        s = settings()
        new = "light" if theme.current() == "dark" else "dark"
        s.set("theme", new)
        self._apply_theme()

    def _apply_theme(self):
        theme.apply_theme(QApplication.instance(), settings().get("theme"))
        icons.clear_cache()
        for a, name in self._icon_actions:
            a.setIcon(icons.icon(name))
        self.canvas.renderer.invalidate()
        self.canvas.renderer._sd.clear()
        self.canvas.invalidate()
        self.layers.refresh()
        self.scene_provider.invalidate()

    # ================================================================== 3D view, depth view, Google Maps
    def _toggle_view_dock(self, dock: QDockWidget, other: QDockWidget, on: bool):
        """Show / hide the 3D or depth view.  The first time one opens it becomes a tab beside Properties, so it gets the
        whole height of the right-hand column; drag its tab out (or onto the plan) to see it next to another panel."""
        if on:
            first = not getattr(dock, "_placed", False)
            if first:
                dock._placed = True
                self.tabifyDockWidget(self.d_props, dock)
            dock.show()
            dock.raise_()
            if first:                                          # sizes only stick once the dock is on screen
                QTimer.singleShot(0, lambda: self.resizeDocks([dock], [520], Qt.Horizontal))
        else:
            dock.hide()

    def _view_visibility(self, act: QAction, dock: QDockWidget, visible: bool):
        """Keep the toolbar button (and the depth view's slab on the plan) in step with its dock.  `visible` comes from the
        dock's own signal: it is False for a tab that is hidden behind another one, which isVisible() would not tell us."""
        if act.isChecked() != visible:
            act.blockSignals(True)
            act.setChecked(visible)
            act.blockSignals(False)
        if dock is self.d_depth:
            ov = self.depth.view.paint_plan_overlay
            if visible and ov not in self.canvas.view_overlays:
                self.canvas.view_overlays.append(ov)
            elif not visible and ov in self.canvas.view_overlays:
                self.canvas.view_overlays.remove(ov)
            self.canvas.update()

    def _depth_line_picked(self, a, b):
        self.depth.view.set_line(a, b)
        if not self.a_depth.isChecked():
            self.a_depth.setChecked(True)
        self.set_tool("select")
        self.state.log(f"Depth view looks across the line you picked ({self.depth.view.spec.azimuth:.1f} deg).", "info")

    def open_google_maps(self, satellite: bool = True, copy_only: bool = False):
        """Open the center of the plan view in Google Maps (or just copy the link)."""
        pr, v = self.state.project, self.canvas.view
        try:
            url, lat, lon, zoom = maps.maps_link_for_view(pr, v.cx, v.cy, v.scale, satellite)
        except C.LocalCRSError:
            info_box(self, "Google Maps", "This project uses a local coordinate system, so the view has no real-world location.\n\n"
                                          "Assign a coordinate system first (Survey > Project Coordinate System).")
            return
        except Exception as ex:
            error_box(self, "Google Maps", f"Could not turn the view center into a latitude / longitude:\n{ex}")
            return
        kind = "satellite" if satellite else "street map"
        if copy_only:
            QApplication.clipboard().setText(url)
            self.state.log(f"Google Maps link copied ({kind}, {lat:.6f}, {lon:.6f}): {url}", "ok")
        elif QDesktopServices.openUrl(QUrl(url)):
            self.state.log(f"Opened Google Maps ({kind}) at {lat:.6f}, {lon:.6f}, zoom {zoom:.1f}: {url}", "ok")
        else:
            QApplication.clipboard().setText(url)
            self.state.log(f"Could not start a web browser. The Google Maps link ({lat:.6f}, {lon:.6f}) is on the clipboard: {url}", "warn")

    # ================================================================== file
    def new_project(self):
        if not self.maybe_save():
            return
        dlg = NewProjectDialog(self, job_hint=self._job_hint() or str(Path.cwd()))
        if not dlg.exec():
            return
        name = dlg.project_name
        # Every new project is a standard job package.  There is no minimal-folder branch or
        # folder-style choice; the fixed folder list is previewed in the setup panel.
        from .job_setup import run_job_setup
        creation = run_job_setup(self, dlg.setup_parent_folder, name, dlg.setup_template,
                                 dlg.setup_weeks, dlg.crs.label, crs_record=dlg.crs.to_dict())
        if creation is None:
            self.state.log("Job folder setup cancelled", "warn")
            return
        self._job_root_choice = None
        self._open(str(creation.paths.project_file), route="new")
        self.state.log(f"Created job folder {creation.paths.root} - {creation.summary}", "ok")

    # ------------------------------------------------------------------ field data / job folder
    def open_fieldwork(self):
        """Survey > Fieldwork Manager - the field-data window, pointed at this job."""
        if not self._ensure_fieldbook_for_processing():
            return
        from .fieldwork_window import open_fieldwork_manager
        win = open_fieldwork_manager(self)
        if win is not None:
            self.state.log("Opened the Fieldwork Manager window", "ok")

    def import_points_file(self):
        """Survey > Import Points from File - a CSV or point file with a selectable role.

        The ordinary file command defaults to this job's field data. The Open Project route's
        optional control-file picker uses the same importer with Control preselected. Reference
        points stay on their own layer and out of the fieldwork list and data-quality checks.
        """
        p, _ = QFileDialog.getOpenFileName(self, "Import Points from File", self._job_hint(), POINTS_FILTER)
        if p:
            self.importer.import_path(p)

    def import_points_folder(self):
        """Survey > Import Points from Folder - one reference role for every point file in a folder.

        One dialog for the folder: the role is chosen once, every readable file is imported, and
        the folder is recorded on the job (Project > job metadata) so the Fieldwork Manager and
        the file dialogs start there next time.
        """
        folder = QFileDialog.getExistingDirectory(self, "Import Points from Folder",
                                                  self._job_hint() or str(Path.home()))
        if not folder:
            return
        dlg = ReferenceFolderDialog(self.state, Path(folder), self)
        if not dlg.exec():
            return
        role = dlg.role()
        # Get selected files from the dialog
        selected = dlg.selected_files()
        tally = self.importer.import_reference_folder(folder, role, dlg.chk_sub.isChecked(), selected_files=selected)
        if not tally["points"]:
            error_box(self, "Import Points from Folder",
                      f"No points were read from {Path(folder).name}."
                      + (f"\n\n{tally['failed']} file(s) were skipped - the message log names them "
                         f"and says why." if tally["failed"] else ""))
            return
        self.state.log((f"Reference import: {describe_folder_import(tally)} on layer "
                        f"{REF.layer_for(role)}, from {folder}") if role else
                       (f"Field data import: {describe_folder_import(tally)} - joins the fieldwork "
                        f"list - from {folder}"), "ok")
        if tally["failed"]:
            info_box(self, "Import Points from Folder",
                     f"{tally['points']:,} point(s) came in from {tally['files']} file(s).\n\n"
                     f"{tally['failed']} file(s) could not be read and were skipped; the message log "
                     f"names them and says why.  Nothing was guessed - check those files and import "
                     f"them one at a time if they matter.")
        if role is None and tally.get("points"):
            self._complete_import_onboarding(imported_points=tally["points"])

    def import_fieldwork(self):
        """Survey > Import Cleaned Field Data - a .fwk straight into this project."""
        from .fieldwork_window import FieldworkBridge, SendToPlumblineDialog
        p, _ = QFileDialog.getOpenFileName(self, "Import Cleaned Field Data", self._job_hint(),
                                           "Field data (*.fwk *.csv *.txt);;All files (*)")
        if not p:
            return
        bridge = FieldworkBridge(self)
        rows = None
        try:
            from ..fieldwork import bridge as FB
            rows, info = FB.read_point_file(p)
        except Exception as ex:
            error_box(self, "Import Field Data", f"{Path(p).name} could not be read:\n{ex}")
            return
        if not rows:
            error_box(self, "Import Field Data", f"{Path(p).name} has no usable points in it.")
            return
        pr = self.state.project
        existing = {pt.number for pt in pr.points.values()}
        incoming = {str(r[FB.PTNUM]).strip() for r in rows if FB.row_is_usable(r)}
        dupes = len(incoming & existing)
        dlg = SendToPlumblineDialog(self, n_points=len(incoming), n_dupes=dupes,
                                    source=Path(p).name, job_name=pr.name,
                                    has_codes=False)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = FB.apply_rows_to_project(pr, rows, dup_policy=dlg.policy)
        except Exception as ex:
            error_box(self, "Import Field Data", f"The import failed:\n{ex}", traceback.format_exc())
            return
        bridge.last_result = result
        self.state.log(f"Imported {result.get('points', 0):,} field points from {Path(p).name}", "ok")
        changed = getattr(self.state, "project_changed", None)
        if callable(changed):
            changed()
        if result.get("points", 0):
            self._complete_import_onboarding(imported_points=result["points"])

    def file_crs_dialog(self):
        """Survey > File Coordinate Systems (change order, item 6)."""
        from .filecrs_dialog import FileCoordinateSystemsDialog
        FileCoordinateSystemsDialog(self.state, self).exec()

    def new_job_folder(self):
        """Create a job folder on its own - reached from Project Folder... > Create a new job folder.

        (This used to be its own menu entry, File > New Job Folder.  It is the same folder setup
        File > New Project offers, so it lives in one place now - see project_folder_dialog.)
        """
        from .job_setup import JobSetupPanel, run_job_setup
        from .widgets import FormDialog

        class _Dlg(FormDialog):
            def validate(self):
                return panel.validate()

        dlg = _Dlg(self, "New Job Folder",
                   intro="Creates the folder tree, an empty field book and control list, and a project "
                         "file with no coordinate system assigned yet.")
        name_edit = QLineEdit("New Job")
        dlg.form.addRow("Job name:", name_edit)
        panel = JobSetupPanel(dlg, self._job_hint())
        dlg.root.insertWidget(2, panel)
        if not dlg.exec():
            return
        creation = run_job_setup(self, panel.parent_folder, name_edit.text().strip() or "New Job",
                                 panel.template, panel.weeks, self.state.project.crs.label,
                                 crs_record=self.state.project.crs.to_dict())
        if creation is not None:
            self.state.log(f"Created job folder {creation.paths.root}", "ok")

    def _job_folder(self) -> Path | None:
        """The job folder: the one chosen with File > Select Job Folder, else the project's own.

        The job folder is normally simply the folder the project file lives in.  It is a
        separate idea only because a job can have a download sitting in it *before* there is a
        project - that is the case Select Job Folder exists for.
        """
        chosen = getattr(self, "_job_root_choice", None)
        if chosen is not None and Path(chosen).is_dir():
            return Path(chosen)
        pr = self.state.project
        p = getattr(pr, "path", None)
        if p and Path(p).parent.is_dir():
            return Path(p).parent
        return None

    def project_folder_dialog(self):
        """File > Project Folder... - one entry for the folder this job lives in.

        There used to be two: *Select Job Folder* (point at a folder) and *New Job Folder* (make
        one).  Same decision, two doors, and a project file that could end up somewhere other than
        its job - so the two are one dialog now (change order, item 12), and it can open the
        folder that is already in use, open another one, or create a new job folder inside any
        folder (the current directory by default).
        """
        from .project_folder import ProjectFolderDialog
        dlg = ProjectFolderDialog(self, current=self._job_folder())
        if not dlg.exec():
            return
        if dlg.chosen:
            self._job_root_choice = Path(dlg.chosen)
            self.state.log(f"Project folder: {dlg.chosen}.  The Fieldwork Manager and the file "
                           f"dialogs start here.", "ok")
        if dlg.open_project:
            self.open_project_path(dlg.open_project)

    def select_job_folder(self):
        """Select Job Folder - kept as the call the rest of the program makes internally.

        The menu entry that used to point here (File > Select Job Folder) is now File > Project
        Folder..., because the two folder entries were the same decision asked twice.

        Whatever is chosen here is what the Fieldwork Manager and its file hints start from, and
        if the folder holds a project file it is offered for opening.  Choosing the folder that is
        already in use does the other useful thing with the same click: it opens the folder in the
        desktop's file manager.
        """
        current = self._job_folder()
        folder = QFileDialog.getExistingDirectory(self, "Select Job Folder",
                                                  str(current) if current else str(Path.home()))
        if not folder:
            return
        folder = Path(folder)
        if current is not None and folder.resolve() == Path(current).resolve():
            self.open_job_folder(folder)
            return
        projects = sorted(folder.glob("*.plb"))
        if projects and confirm(self, "Select Job Folder",
                                f"{folder.name} holds {projects[0].name}.\n\nOpen that project?",
                                "Open"):
            self.open_project_path(projects[0])
            return
        self._job_root_choice = folder
        self.state.log(f"Job folder: {folder}.  The Fieldwork Manager and the file dialogs start here.",
                       "ok")

    def open_job_folder(self, folder=None):
        """Hand the job folder to the desktop (File > Select Job Folder does this when the folder
        is already the one in use)."""
        folder = Path(folder) if folder else self._job_folder()
        if folder is None or not Path(folder).is_dir():
            QMessageBox.information(self, "Job Folder",
                                    "This project has not been saved yet, so it has no job folder.\n\n"
                                    "Use File > Save As to put it somewhere, or File > Select Job "
                                    "Folder to point at a folder that already holds the job.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _job_hint(self) -> str:
        """Where the file dialogs start: the job folder, else the folder the data came from.

        A project that has not been saved yet has no job folder, but it does know where its
        points came from - an import from a folder records that folder on the job - and that is
        the folder the next dialog should open in.
        """
        folder = self._job_folder()
        if folder is None:
            data = self.state.project.settings.get("data_folder")
            if data and Path(data).is_dir():
                return str(data)
        return str(folder) if folder else ""

    def _default_field_data_folder(self) -> str:
        """The user-level import folder, falling back to Downloads and then Home."""
        configured = str(settings().get("field_data_import_folder", "") or "").strip()
        if configured and Path(configured).expanduser().is_dir():
            return str(Path(configured).expanduser())
        downloads = Path.home() / "Downloads"
        return str(downloads if downloads.is_dir() else Path.home())

    def _existing_field_data_start(self) -> str:
        """Start at the last field-work source folder, falling back to packaged Field Data."""
        source = self.state.project.settings.get("field_data_source_folder")
        if source and Path(source).expanduser().is_dir():
            return str(Path(source).expanduser())
        data = self.state.project.settings.get("data_folder")
        if data and Path(data).expanduser().is_dir():
            return str(Path(data).expanduser())
        root = self._job_folder()
        if root is not None and (root / "Field Data").is_dir():
            return str(root / "Field Data")
        return self._default_field_data_folder()

    def _complete_import_onboarding(self, imported_points: int = 0):
        """Finish the point-import route with CRS, default imagery, then the QA offer."""
        pr = self.state.project
        if imported_points and pr.crs.is_local:
            self._ensure_project_crs()

        survey_points = REF.survey_points(pr)
        imagery_added = False
        has_visible_imagery = any(layer.visible for layer in pr.imagery.values())
        if survey_points and not pr.crs.is_local and not pr.imagery:
            imagery_added = self._offer_default_esri_imagery()
        # This offer is deliberately independent of the imagery answer: declining imagery must
        # never skip the Fix Point Errors review. If imagery was just added or was already visible,
        # let the canvas paint its refreshed frame and give visible tile requests a short, bounded
        # chance to settle.
        if survey_points:
            if imagery_added or has_visible_imagery:
                self._offer_fix_point_errors_after_imagery()
            else:
                self._offer_linework_then_fix_point_errors()

    def _offer_default_esri_imagery(self) -> bool:
        """Offer the shipped Esri World Imagery layer after field data has a project CRS."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Add Default Imagery")
        box.setText("Would you like to add Esri World Imagery to this project?")
        box.setInformativeText(
            "Visible satellite tiles will be fetched automatically as the map draws and cached for "
            "later use. Internet access is needed for tiles that are not already cached.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False

        from ..core.tiles import PRESETS
        source = next((tile for tile in PRESETS if tile.name == "Esri World Imagery"), None)
        if source is None:
            self.state.log("The default Esri World Imagery source is not available.", "warn")
            return False
        self.create_imagery_layer({"kind": "tiles", "name": source.name,
                                   "source": source.to_dict()})
        self.state.log("Esri World Imagery added; visible map tiles are loading automatically.", "ok")
        return True

    def _offer_process_linework(self) -> bool:
        """Offer the dedicated linework-processing action when current inputs are unprocessed."""
        project = self.state.project
        if not REF.survey_points(project) or project.linework_is_processed():
            return False
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Process Linework")
        box.setText("Linework has not been processed for the current survey points and Feature Codes. Process it now?")
        box.setInformativeText(
            "This builds derived linework from the current point descriptions and Feature Codes. "
            "You can also run Process Linework later from Survey.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        # Reuse the dedicated command, whose explicit confirmation and Field Book gate also apply
        # when this offer is accepted during Open Route.
        self.process_linework()
        return True

    def _offer_fix_point_errors(self) -> bool:
        """Offer the Fix Point Errors workbench after the import route, regardless of imagery choice."""
        if not REF.survey_points(self.state.project):
            return False
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Fix Point Errors")
        box.setText("Would you like to review the survey points for errors now?")
        box.setInformativeText("You can open Fix Point Errors later from Survey at any time.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        self.open_fix_point_errors()
        return True

    def _offer_linework_then_fix_point_errors(self) -> None:
        """Keep the optional Process Linework offer immediately before point-error review."""
        self._offer_process_linework()
        self._offer_fix_point_errors()

    def _offer_fix_point_errors_after_imagery(self) -> None:
        """Defer both post-import offers until the refreshed imagery has had a chance to draw."""
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            QTimer.singleShot(0, self._offer_linework_then_fix_point_errors)
            return

        # Force the invalidated canvas through a paint now so visible tile requests start before
        # the prompt. Wait for those requests to settle, but cap the delay for offline/slow tiles.
        if canvas.isVisible():
            canvas.repaint()
        started = time.monotonic()
        no_tiles_deadline = started + 0.45
        max_deadline = started + 2.5
        imagery = getattr(canvas, "imagery", None)
        saw_pending = bool(getattr(imagery, "pending", 0))
        settled_at = None

        def wait_for_refresh():
            nonlocal saw_pending, settled_at
            now = time.monotonic()
            tile_manager = getattr(canvas, "imagery", None)
            pending = int(getattr(tile_manager, "pending", 0) or 0)
            if pending:
                saw_pending = True
                settled_at = None
            elif saw_pending:
                if settled_at is None:
                    settled_at = now
                elif now - settled_at >= 0.2:
                    self._offer_linework_then_fix_point_errors()
                    return
            elif now >= no_tiles_deadline:
                self._offer_linework_then_fix_point_errors()
                return

            if now >= max_deadline:
                self._offer_linework_then_fix_point_errors()
                return
            QTimer.singleShot(75, wait_for_refresh)

        QTimer.singleShot(75, wait_for_refresh)

    def _ask_field_data_route(self, route: str) -> int:
        """Initial-project onboarding: offer a field-data folder scan/import."""
        if route == "new":
            title = "Import Field Data"
            question = "Would you like to select and import a field-data folder now?"
            detail = "The selected point files will be copied into this project's standard Field Data folder."
            start = self._default_field_data_folder()
            picker_title = "Select a Field Data Folder"
        else:
            title = "Check Field Data"
            question = "Would you like to check the parent field-work folder for new survey files?"
            detail = "Unchanged files already imported into this project will be greyed out."
            start = self._existing_field_data_start()
            picker_title = "Choose the Parent Field-Work Folder"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(question)
        box.setInformativeText(detail)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return 0

        folder = QFileDialog.getExistingDirectory(self, picker_title, start)
        if not folder:
            return 0
        settings().set("field_data_import_folder", str(Path(folder).expanduser()))
        return self._run_field_data_folder_import(Path(folder))

    def _control_file_start(self) -> str:
        """Start the optional control picker at the job's standard Control folder."""
        root = self._job_folder()
        control_dir = root / "Control" if root is not None else None
        if control_dir is not None and control_dir.is_dir():
            return str(control_dir)
        return self._job_hint() or self._default_field_data_folder()

    def _remember_control_file(self, path: str) -> None:
        """Keep a chosen control file portable when it lives inside the project folder."""
        control_path = Path(path).expanduser().resolve()
        project_path = getattr(self.state.project, "path", None)
        project_root = Path(project_path).expanduser().parent.resolve() if project_path else None
        value = str(control_path)
        if project_root is not None:
            try:
                value = control_path.relative_to(project_root).as_posix()
            except ValueError:
                pass
        if self.state.project.settings.get("control_file") == value:
            return
        with self.state.edit("Set control file"):
            self.state.project.settings["control_file"] = value

    def _import_optional_control_file(self) -> bool:
        """Offer a control-point file just before field points; Cancel means skip."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose Control File (optional)", self._control_file_start(), POINTS_FILTER)
        if not path:
            return False
        imported = self.importer.import_path(path, default_role="control")
        if imported:
            self._remember_control_file(path)
        return imported

    def _run_field_data_folder_import(self, folder: Path) -> int:
        """Show the per-file picker; offer an optional control file immediately before importing."""
        folder = Path(folder).expanduser()
        if not folder.is_dir():
            error_box(self, "Import Field Data", f"{folder} is not a folder.")
            return 0
        dlg = ReferenceFolderDialog(self.state, folder, self, field_data_only=True)
        if not dlg.exec():
            return 0
        selected = dlg.selected_files()
        if not selected:
            return 0
        self._import_optional_control_file()
        tally = self.importer.import_reference_folder(
            folder, None, dlg.chk_sub.isChecked(), selected_files=selected)
        if not tally.get("points"):
            error_box(self, "Import Field Data",
                      f"No points were imported from {folder.name}."
                      + (f"\n\n{tally.get('failed', 0)} file(s) could not be read; the message log names them."
                         if tally.get("failed") else ""))
            return 0
        self.state.log(
            f"Field data import: {describe_folder_import(tally)} - copied to the project's "
            f"Field Data folder - from {folder}", "ok")
        if tally.get("failed"):
            info_box(self, "Import Field Data",
                     f"{tally['points']:,} point(s) came in from {tally['files']} file(s).\n\n"
                     f"{tally['failed']} file(s) could not be read and were skipped; the message log "
                     "names them and says why.")
        return int(tally.get("points", 0) or 0)

    def open_project(self):
        if not self.maybe_save():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Open Project", "", "Plumbline project (*.plb);;All files (*)")
        if p:
            self._open(p)

    def open_project_path(self, path: str):
        if self.maybe_save():
            self._open(path)

    def _open(self, path, *, route: str = "existing"):
        try:
            self.state.open_project(path)
        except Exception as ex:
            error_box(self, "Open Project", f"Could not open {Path(path).name}:\n{ex}", traceback.format_exc())
            return
        self.state.log(f"Opened {path}", "ok")
        if self.state.project.crs.is_local:
            self._ensure_project_crs()
        # The Field Book step is automatic, not another yes/no question. Closing its tool leaves
        # the project open; processing actions will reopen it and remain blocked until loaded.
        self._ensure_fieldbook_for_processing(notify=False)
        imported_points = self._ask_field_data_route("new" if route == "new" else "existing")
        self._complete_import_onboarding(imported_points=imported_points)

    def open_sample(self):
        if not self.maybe_save():
            return
        from ..sample import make_sample_project
        self.state.set_project(make_sample_project(), dirty=False)
        self.state.log("Sample project loaded - a SYNTHETIC site (not a real survey). Try Data Quality, Contours, Volumes and Imagery.", "ok")
        self._ensure_fieldbook_for_processing(notify=False)

    def open_sample_real(self):
        """File > Open Sample Project > Real World - a real reduced survey, in its job folder.

        It opens the built sample *in place* rather than copying it: the job folder, the field
        data and the report are the point of this sample, and the Fieldwork Manager window will
        point straight at them.
        """
        if not self.maybe_save():
            return
        from ..fieldwork.sample_real import find_sample_dir, repo_root
        folder = find_sample_dir()
        if folder is None:
            info_box(self, "Real World Sample",
                     "The Real World sample is not built in this copy of Plumbline.\n\n"
                     "It is made from a real field download that ships with the source:\n"
                     f"    {repo_root() / 'samples' / 'Real World' / 'Source'}\n\n"
                     "Build it once from a terminal:\n"
                     "    python -m plumbline sample-real\n\n"
                     "…or open any consolidated .fwk with Survey > Import Cleaned Field Data.")
            return
        project = next(iter(sorted(folder.glob("*.plb"))), None)
        if project is None:
            error_box(self, "Real World Sample", f"No project file in {folder}.")
            return
        self._open(str(project))
        self.state.log(f"Real World sample - a real reduced job from {folder}. "
                       "Try Survey > Fieldwork Manager: it opens the same job's field data.", "ok")

    def show_welcome(self):
        dlg = WelcomeDialog(self)
        dlg.exec()
        c = dlg.choice
        if c == "new":
            self.new_project()
        elif c == "open":
            self.open_project()
        elif c == "sample":
            self.open_sample()
        elif c == "sample_real":
            self.open_sample_real()
        elif c.startswith("recent:"):
            self.open_project_path(c[7:])

    def _fill_recent(self):
        self.m_recent.clear()
        seen = set()
        rec = []
        for p in settings().get("recent_files", []):
            try:
                res = str(Path(p).resolve())
                if Path(res).exists() and res.casefold() not in seen:
                    seen.add(res.casefold())
                    rec.append(res)
            except Exception:
                pass
        for p in rec:
            self.m_recent.addAction(Path(p).name, lambda p=p: self.open_project_path(p)).setToolTip(p)
        if not rec:
            self.m_recent.addAction("(none)").setEnabled(False)

    def save(self) -> bool:
        pr = self.state.project
        if not pr.path:
            return self.save_as()
        try:
            self.state.save_project()
        except Exception as ex:
            error_box(self, "Save", str(ex), traceback.format_exc())
            return False
        self.state.log(f"Saved {pr.path}", "ok")
        self.update_title()
        return True

    def save_as(self) -> bool:
        pr = self.state.project
        if pr.path:
            current_root = Path(pr.path).expanduser().parent
            parent_start = current_root.parent
            suggested_name = f"{pr.name or 'Project'} Copy"
        else:
            parent_start = Path.cwd()
            data_folder = (pr.settings or {}).get("data_folder")
            if data_folder and Path(str(data_folder)).expanduser().exists():
                parent_start = Path(str(data_folder)).expanduser().parent
            suggested_name = ("Untitled Project" if str(pr.name or "").strip().casefold() in {"", "untitled"}
                              else str(pr.name))
        dlg = ProjectPackageDialog(self, name=suggested_name, parent_folder=str(parent_start))
        if not dlg.exec():
            return False
        try:
            package = create_project_package(
                pr, dlg.parent_folder, dlg.project_name)
        except Exception as ex:
            error_box(self, "Save Project Package", str(ex), traceback.format_exc())
            return False

        # Adopt the package's rebased settings/assets without replacing AppState or
        # discarding the current selection and undo stack.
        pr.restore(package.snapshot())
        pr.path = package.path
        pr._portable_paths = True
        self._job_root_choice = None
        self.state._prune_selection()
        self.state.set_dirty(False)
        self.state._after_change({"all"})
        self.state.undo_changed.emit()
        settings().add_recent(str(pr.path))
        self.state.log(f"Saved project package {pr.path}", "ok")
        self.update_title()
        return True

    def import_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import", "", IMPORT_FILTER + self._plugin_import_filter())
        for p in paths:
            self.importer.import_path(p)

    def _plugin_import_filter(self):
        extra = "".join(f";;{i.name}" for i in plugins.registry.importers)
        return extra

    def export_png(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save drawing as image", f"{self.state.project.name}.png", "PNG image (*.png)")
        if p:
            img = self.canvas.render_image(2400)
            img.save(p)
            self.state.log(f"Image written: {p} ({img.width()} x {img.height()})", "ok")

    def _export_kml(self):
        export_kml(self)

    # ================================================================== edit
    def undo(self):
        lbl = self.state.undo()
        if lbl:
            self.state.log(f"Undid: {lbl}", "info")

    def redo(self):
        lbl = self.state.redo()
        if lbl:
            self.state.log(f"Redid: {lbl}", "info")

    def select_all(self):
        pr = self.state.project
        vis = {n for n, l in pr.layers.items() if l.visible and not l.locked}
        self.state.select([i for i, p in pr.points.items() if p.layer in vis], [i for i, e in pr.entities.items() if e.layer in vis])

    def delete_selection(self):
        st = self.state
        if not (st.sel_points or st.sel_entities):
            return
        n = len(st.sel_points) + len(st.sel_entities)
        if n > 500 and not confirm(self, "Delete", f"Delete {n:,} objects?", "Delete"):
            return
        pts, ents = list(st.sel_points), list(st.sel_entities)
        with st.edit(f"Delete {n} object(s)"):
            st.project.remove_points(pts)
            st.project.remove_entities(ents)
        st.log(f"Deleted {n:,} object(s). Ctrl+Z undoes.", "info")

    def find_point(self):
        text, ok = QInputDialog.getText(self, "Find point", "Point number, or part of a description / layer:")
        text = text.strip()
        if not ok or not text:
            return
        pr = self.state.project
        exact = pr.point_by_number(text)
        if exact:
            self.state.select(points=[exact.id])
            self.state.center_on(exact.x, exact.y)
            return
        t = text.lower()
        hits = [p for p in pr.points.values() if t in p.desc.lower() or t in p.layer.lower() or t in p.number.lower()]
        if not hits:
            self.state.log(f"Nothing matches '{text}'.", "warn")
            return
        self.state.select(points=[p.id for p in hits])
        xs, ys = [p.x for p in hits], [p.y for p in hits]
        self.state.zoom_to_bbox((min(xs) - 5, min(ys) - 5, max(xs) + 5, max(ys) + 5))
        self.state.log(f"{len(hits)} point(s) match '{text}'.", "info")

    def zoom_selection(self):
        bb = self.state.selection_bbox()
        if bb:
            self.state.zoom_to_bbox(bb)

    def transform_dialog(self):
        dlg = TransformDialog(self.state, self)
        if dlg.exec():
            st = dlg.apply()
            self.state.log(f"Transformed {st['points']:,} points, {st['entities']:,} objects, {st['surfaces']} surfaces.", "ok")
            self.state.zoom_extents()

    def show_licence(self):
        """Help > Licence Agreement - read what you accepted, any time."""
        from .licence_dialog import show_licence as _show
        _show(self)

    def settings_dialog(self):
        dlg = SettingsDialog(self)
        if dlg.exec():
            dlg.apply()
            self._apply_theme()
            self.points.reload()
            self.props.refresh()
            self.canvas.opts.point_px = float(settings().get("point_size_px"))
            self.canvas.opts.label_px = float(settings().get("label_px"))
            self.canvas.invalidate()

    # ================================================================== survey
    def _show_modal_qa_workbench(self, workbench):
        """Show a central-widget QA window modally while keeping the main window alive."""
        self._qa_workbench_window = workbench
        workbench.setWindowModality(Qt.WindowModal)
        workbench.setAttribute(Qt.WA_DeleteOnClose, True)
        workbench.destroyed.connect(self._clear_qa_workbench_reference)
        workbench.show()
        workbench.raise_()
        workbench.activateWindow()

    def _clear_qa_workbench_reference(self, *_):
        self._qa_workbench_window = None

    def open_survey_points(self):
        """Survey > Survey Points Table: reveal the existing searchable points dock."""
        self.d_pts.show()
        self.d_pts.raise_()
        self.points.view.setFocus()

    def open_fix_point_errors(self):
        """Survey > Fix Point Errors: open the dedicated Fix Point Errors workbench."""
        if not self._ensure_fieldbook_for_processing():
            return
        from .qa_workspace import FixPointErrorsDialog
        self._show_modal_qa_workbench(FixPointErrorsDialog(self.state, self))

    def open_fix_linework(self):
        """Survey > Fix Linework: open the dedicated Fix Linework workbench."""
        if not self._ensure_fieldbook_for_processing():
            return
        from .qa_workspace import FixLineworkDialog
        self._show_modal_qa_workbench(FixLineworkDialog(self.state, self))

    def qa_dialog(self):
        self.open_fix_point_errors()

    def codes_dialog(self):
        FeatureCodesDialog(self.state, self).exec()

    def _has_active_fieldbook(self) -> bool:
        """Whether this project has a readable Field Book with an active code vocabulary."""
        from ..fieldwork.bridge import vocabulary_for

        project = self.state.project
        root = self._job_folder()
        explicit = project.settings.get("fieldbook_file") if project.settings else None
        if explicit:
            path = Path(str(explicit)).expanduser()
            if not path.is_absolute():
                base = root or (Path(project.path).expanduser().parent if project.path else None)
                if base is not None:
                    path = base / path
            explicit = path if path.is_file() else None

        vocabulary = vocabulary_for(project, job_root=root, fieldbook=explicit)
        source = str(vocabulary.get("source", ""))
        raw_path = str(vocabulary.get("path") or "").strip()
        if not raw_path:
            return False
        book_path = Path(raw_path).expanduser()
        if not book_path.is_absolute():
            base = root or (Path(project.path).expanduser().parent if project.path else None)
            if base is not None:
                book_path = base / book_path
        return (source in {"field book", "field book + job codes"}
                and bool(vocabulary.get("codes")) and book_path.is_file())

    def _ensure_fieldbook_for_processing(self, *, notify: bool = True) -> bool:
        """Open Field Book automatically when required; refuse processing if it stays unavailable."""
        if not bool(settings().get("require_fieldbook_for_processing", True)):
            return True
        if self._has_active_fieldbook():
            return True

        self.open_fieldbook_dialog()
        if self._has_active_fieldbook():
            return True
        if notify:
            info_box(
                self, "Field Book Required",
                "Load a readable Field Book with feature codes before running point/code or linework "
                "processing. The Field Book tool was opened; select or convert a book, or close the tool. "
                "Processing stays blocked until a book is loaded. You can turn off this requirement in Settings.")
        return False

    def open_fieldbook_dialog(self):
        """Survey > Field Book: open the unified Field Book manager (Convert, Select, Report)."""
        from .fieldbook_dialog import FieldBookDialog
        dlg = FieldBookDialog(self.state, self)
        dlg.exec()

    def convert_field_to_finish(self):
        """Survey > Convert Field Book: an office code table becomes this job's codes."""
        pr = self.state.project
        start = pr.settings.get("f2f_path") or os.path.dirname(pr.path or "") or ""
        path, _ = QFileDialog.getOpenFileName(self, "Convert Field to Finish", start,
                                              "Code Table (*.csv *.txt);;All Files (*)")
        if not path:
            return
        try:
            table = f2f.read(path)
        except Exception as ex:
            error_box(self, "Convert Field Book", str(ex))
            return
        dlg = ConvertFieldToFinishDialog(table, pr, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        res = dlg.choices()
        mapping, only, mode = res[0], res[1], res[2]
        try:
            new_codes, stats = f2f.convert(table, mapping, only=only,
                                           existing=pr.codes if mode == "merge" else None)
        except Exception as ex:
            error_box(self, "Convert Field Book", str(ex), traceback.format_exc())
            return
        with self.state.edit("Convert Field Book"):
            pr.codes = new_codes
            pr.settings["f2f_path"] = str(path)
        self.state.log(f"Field to Finish: {f2f.describe_stats(stats)} "
                       f"({os.path.basename(path)}; {mapping.found_by}) "
                       f"- Apply Feature Codes redraws the points with them.",
                       "warn" if stats["unknown_entity_types"] else "ok")

    def convert_fieldbook(self):
        """Survey > Field Book: delegate to Field Book manager."""
        self.open_fieldbook_dialog()
    def apply_codes(self):
        if not self._ensure_fieldbook_for_processing():
            return
        with self.state.edit("Apply feature codes"):
            r = self.state.project.apply_codes_to_points()
        msg = f"Feature codes applied: {r['matched']:,} point(s) matched."
        if r["unknown"]:
            top = ", ".join(f"{k} ({v})" for k, v in sorted(r["unknown"].items(), key=lambda kv: -kv[1])[:6])
            msg += f" Unknown codes: {top}."
        self.state.log(msg, "ok" if not r["unknown"] else "warn")

    def process_linework(self):
        if not self._ensure_fieldbook_for_processing():
            return
        with self.state.edit("Process linework", kinds=("entities",)):
            r = self.state.project.process_linework()
        self.state.log(f"Linework: {r['strings']} string(s) created" + (f", {r['replaced']} previous replaced" if r["replaced"] else "") + ".", "ok")

    def join_selected_points_dialog(self):
        if not self._ensure_fieldbook_for_processing():
            return
        pts = list(self.state.sel_points)
        if len(pts) < 2:
            info_box(self, "Join Points", "Select 2 or more points in the drawing or point list first.")
            return
        from .linework_dialog import JoinPointsDialog
        dlg = JoinPointsDialog(self.state, pts, self)
        dlg.exec()

    def edit_linework_coding_dialog(self):
        if not self._ensure_fieldbook_for_processing():
            return
        sel_ents = [self.state.project.entities[eid] for eid in self.state.sel_entities if eid in self.state.project.entities]
        poly = next((e for e in sel_ents if isinstance(e, Polyline) and
                     (e.derived.startswith("linework") or (e.attrs or {}).get("points"))), None)
        if poly is None:
            info_box(self, "Edit Linework Coding", "Select a coded linework polyline first.")
            return
        from .linework_dialog import EditLineworkCodingDialog
        dlg = EditLineworkCodingDialog(self.state, poly, self)
        dlg.exec()

    def edit_selected_line_geometry(self):
        """Open the transactional vertex/curve editor for exactly one selected polyline."""
        if not self._ensure_fieldbook_for_processing():
            return
        polylines = [self.state.project.entities[eid] for eid in sorted(self.state.sel_entities)
                     if isinstance(self.state.project.entities.get(eid), Polyline)]
        if len(polylines) != 1:
            info_box(self, "Edit Line Geometry", "Select exactly one polyline in the drawing or Properties panel.")
            return
        from .linework_editor_dialog import LineEditorDialog
        LineEditorDialog(self.state, polylines[0], self).exec()

    def join_selected_lines_dialog(self):
        """Open a separate preview-first workflow for joining two or more selected polylines."""
        if not self._ensure_fieldbook_for_processing():
            return
        polylines = [self.state.project.entities[eid] for eid in sorted(self.state.sel_entities)
                     if isinstance(self.state.project.entities.get(eid), Polyline)]
        if len(polylines) < 2:
            info_box(self, "Join Lines", "Select two or more open polylines in the drawing first.")
            return
        from .linework_editor_dialog import JoinLinesDialog
        JoinLinesDialog(self.state, polylines, self).exec()

    def cogo_dialog(self):
        TraverseDialog(self.state, self).exec()

    # ================================================================== coordinates
    def _ensure_project_crs(self) -> bool:
        """Open the project CRS picker when unassigned, and report whether it was assigned."""
        if not self.state.project.crs.is_local:
            return True
        self.crs_dialog()
        return not self.state.project.crs.is_local

    def crs_dialog(self, calculator: bool = False):
        dlg = CRSDialog(self.state, self)
        if calculator:
            dlg.tabs.setCurrentIndex(4)
        if not dlg.exec() or dlg.result_crs is None:
            return
        new, mode = dlg.result_crs, dlg.result_mode
        pr = self.state.project
        old = pr.crs
        try:
            if mode == "reproject" and not old.is_local:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    with self.state.edit("Reproject project"):
                        pr.reproject(new, new.strategy)
                finally:
                    QApplication.restoreOverrideCursor()
                self.state.log(f"Coordinates converted to {new.label}.", "ok")
            else:
                with self.state.edit("Assign coordinate system"):
                    pr.assign_crs(new)
                self.state.log(f"Coordinate system assigned: {new.label} (no coordinates were changed).", "ok")
        except Exception as ex:
            error_box(self, "Coordinate system", str(ex), traceback.format_exc())
            return
        self.imagery_clear_geo()
        self._refresh_layer_combo()
        self.state.zoom_extents()

    def imagery_clear_geo(self):
        self.canvas.imagery._geo.clear()
        self.canvas.invalidate()

    # ================================================================== surfaces
    def create_surface(self, existing: Surface | None):
        pr = self.state.project
        dlg = SurfaceDialog(self.state, existing, self)
        if existing:
            dlg.ed_name.setEnabled(False)
        if not dlg.exec():
            return
        params, name = dlg.params(), dlg.ed_name.text().strip()
        pts, breaks, bounds, holes = gather_surface_inputs(pr, params)
        if len(pts) < 3:
            error_box(self, "Surface", f"Only {len(pts)} usable point(s) with elevations - a surface needs at least 3.")
            return
        me = params.get("max_edge") or None
        try:
            tin, rep = run_blocking(self, f"Building surface '{name}' ({len(pts):,} points) ...", build_tin, pts, breaks, bounds, holes, me)
        except RuntimeError as ex:
            error_box(self, "Surface", "The surface could not be built.", str(ex))
            return
        sig = surface_signature(pts, breaks, bounds, holes, me)
        rd = rep.to_dict()
        rd["sig"] = sig
        with self.state.edit("Rebuild surface" if existing else "Create surface"):
            if existing is not None:
                s = pr.surfaces[existing.id]
                s.set_geometry(tin.pts, tin.tris)
                s._tin = tin
                s.params, s.report, s.stale = dict(params), rd, False
            else:
                s = Surface(0, name, tin.pts, tin.tris, dict(params), report=rd)
                s._tin = tin
                pr.add_surface(s)
        self.state.active_surface_id = s.id
        self.state.log(f"Surface '{s.name}' built:\n" + build_report_text(rd, pr.h_unit), "warn" if (rep.warnings or rep.n_unenforced) else "ok")
        if not existing:
            self.d_surf.raise_()
            self.state.zoom_extents()
        self.canvas.prepare_surface_locator(s, tin)        # build the point locator for the elevation readout

    def edit_surface(self):
        s = self.state.active_surface()
        if s is None:
            self.create_surface(None)
        else:
            self.create_surface(s)

    def delete_surface(self, sid: int):
        s = self.state.project.surfaces.get(sid)
        if s is None or not confirm(self, "Delete surface", f"Delete surface '{s.name}' and its contours?", "Delete"):
            return
        with self.state.edit("Delete surface"):
            self.state.project.remove_derived(f"contours:{s.name}")
            del self.state.project.surfaces[sid]
        self.state.active_surface_id = next(iter(self.state.project.surfaces), None)

    def contours_for_active(self):
        s = self.state.active_surface()
        if s is None:
            error_box(self, "Contours", "Create a surface first.")
            return
        self.contours_for(s.id)

    def contours_for(self, sid: int):
        pr = self.state.project
        s = pr.surfaces.get(sid)
        if s is None:
            return
        dlg = ContourDialog(self.state, s, self)
        if not dlg.exec():
            return
        v = dlg.values()
        try:
            data = run_blocking(self, "Tracing contours ...", compute_contour_data, s, v["interval"], v["base"], v["smooth"])
        except RuntimeError as ex:
            error_box(self, "Contours", "Contouring failed.", str(ex))
            return
        with self.state.edit("Create contours"):
            r = apply_contours(pr, s, data, v["interval"], v["index_every"], v["base"], v["labels"], v["text_height"], v["min_length"])
            pr.settings.update({"contour_interval": v["interval"], "index_every": v["index_every"], "contour_base": v["base"],
                                "contour_smooth": v["smooth"], "contour_labels": v["labels"], "text_height": v["text_height"]})
        self.state.log(f"Contours for '{s.name}': {r['lines']:,} lines, {r['labels']:,} labels ({r['levels']} levels, {r['min']:.2f} to {r['max']:.2f}).", "ok")

    def volumes(self):
        if not self.state.project.surfaces:
            error_box(self, "Volumes", "Create a surface first (Surface menu).")
            return
        VolumeDialog(self.state, self).exec()

    def _selected_polyline(self):
        pr = self.state.project
        for i in self.state.sel_entities:
            e = pr.entities.get(i)
            if isinstance(e, Polyline):
                return e
        return None

    def profile_active(self):
        s = self.state.active_surface()
        if s is None:
            error_box(self, "Profile", "Create a surface first.")
            return
        self.profile_for(s.id)

    def profile_for(self, sid: int):
        s = self.state.project.surfaces.get(sid)
        e = self._selected_polyline()
        if s is None:
            return
        if e is None:
            info_box(self, "Profile", "Select a polyline in the drawing first (or draw one with the Polyline tool) - it becomes the profile line.")
            return
        ProfileDialog(self.state, s, e, self).exec()

    def report_surface_active(self):
        s = self.state.active_surface()
        if s is None:
            error_box(self, "Report", "Create a surface first.")
            return
        self.report_surface(s.id)

    def report_surface(self, sid: int):
        s = self.state.project.surfaces.get(sid)
        if s:
            ReportViewer(self, reports.surface_report(self.state.project, s)).exec()

    # ================================================================== imagery
    def toggle_imagery_nudge(self):
        """Pick image/target point pairs in the plan view and apply their mean display shift."""
        active = self._imagery_nudge_tool
        if active is not None and self.canvas.tool is active:
            active.cancel()
            self.set_tool("pan")
            return

        layer = self.imagery.current_layer()
        if layer is None:
            info_box(self, "Nudge Imagery", "Select an imagery layer first.")
            return
        if not layer.visible:
            info_box(self, "Nudge Imagery", "Turn on the selected imagery layer before nudging it by points.")
            return
        if not self.canvas.opts.show_imagery:
            self.view_toggles["show_imagery"].setChecked(True)

        def on_deactivate():
            self.imagery.set_nudge_mode_active(False)
            if self._imagery_nudge_tool is tool:
                self._imagery_nudge_tool = None

        def on_finish(applied: bool):
            if not applied:
                self.state.log("Imagery nudge cancelled; no point pairs were applied.", "info")
            if self.canvas.tool is tool:
                self.set_tool("pan")

        tool = ImageryNudgeTool(self.canvas, layer.id, on_finish=on_finish, on_deactivate=on_deactivate)
        self._imagery_nudge_tool = tool
        self.tool_group.setExclusive(False)
        for action in self.tool_acts.values():
            action.setChecked(False)
        self.tool_group.setExclusive(True)
        self.imagery.set_nudge_mode_active(True)
        self.canvas.set_tool(tool)

    def _unique_layer_name(self, base: str) -> str:
        names = {l.name for l in self.state.project.imagery.values()}
        n, k = base, 2
        while n in names:
            n = f"{base} ({k})"
            k += 1
        return n

    def create_imagery_layer(self, spec: dict):
        pr = self.state.project
        with self.state.edit("Add imagery", kinds=("imagery",)):
            lid = pr.new_id()
            pr.imagery[lid] = ImageryLayer(lid, self._unique_layer_name(spec["name"]), spec["kind"], dict(spec["source"]), True, 1.0, (0.0, 0.0))
        self.canvas.opts.show_imagery = True
        self.view_toggles["show_imagery"].setChecked(True)
        self.d_img.show()
        self.d_img.raise_()
        if pr.extents() is None:
            self._default_view_for_crs()
        self.canvas.invalidate()
        self.canvas.update()
        self.state.log(f"Imagery layer added: {spec['name']}", "ok")

    def _default_view_for_crs(self):
        crs = self.state.project.crs
        try:
            aou = crs.crs.area_of_use
            if aou is not None:
                lon, lat = (aou.west + aou.east) / 2, (aou.south + aou.north) / 2
                x, y = crs.from_lonlat(lon, lat)
                self.state.center_on(float(x), float(y), 0.02 / U.M_PER_UNIT[crs.unit] * 5)
        except Exception:
            pass

    def add_imagery(self):
        if not self._ensure_project_crs():
            return
        dlg = AddImageryDialog(self.state, self)
        if dlg.exec() and dlg.layer_spec:
            self.create_imagery_layer(dlg.layer_spec)

    def add_imagery_file(self, path: str) -> bool:
        if not self._ensure_project_crs():
            return False
        try:
            r = run_blocking(self, "Reading image ...", IM.read_georeferenced_image, path)
        except RuntimeError as ex:
            error_box(self, "Imagery", str(ex).strip().splitlines()[-1], str(ex))
            return False
        self.create_imagery_layer({"kind": "file", "name": Path(path).stem,
                                   "source": {"path": path, "corners": [list(c) for c in r["corners"]], "crs": r["crs"], "attribution": ""}})
        return True

    def add_kml_overlay(self, o: dict) -> bool:
        if not self._ensure_project_crs():
            return False
        corners = IM.kml_overlay_corners(o["north"], o["south"], o["east"], o["west"], o.get("rotation", 0.0))
        self.create_imagery_layer({"kind": "file", "name": o.get("name", "Overlay"),
                                   "source": {"path": o["file"], "corners": [list(c) for c in corners], "crs": "EPSG:4326", "attribution": ""}})
        return True



    def _datum_note(self) -> str:
        crs = self.state.project.crs
        if crs.is_local:
            return ""
        s = crs.strategy
        try:
            if s == "none":
                return "No datum shift applied (NAD83 and WGS84 positions treated as identical)"
            ops = C.list_operations(crs, 4326)
            b = ops[0]
            name = b.name.replace(" (with axis order normalized for visualization)", "")
            if b.accuracy is None:
                acc = "unknown accuracy"
            else:
                acc = f"stated accuracy {b.accuracy:g} m" + (" - no shift grids, so this is only a rough approximation" if b.accuracy >= 1 else "")
            return f"{'Automatic' if s == 'auto' else s}: {name} ({acc})"
        except Exception:
            return s


    def import_ge_pins(self):
        """Imagery > Import Google Earth Pins - the pins come in as reference points.

        A pin is a coordinate somebody drew on the imagery, so it arrives the way every other
        outside coordinate does: on the OTHER reference layer, with the pin's own name as its
        number, kept out of the fieldwork list and out of the data-quality checks.  Comparing a pin
        with the point it was dropped on is a measurement, not a report - select the two and use
        Draw > Measure.  Re-importing the same file updates the pins rather than doubling them.
        """
        pr = self.state.project
        if pr.crs.is_local:
            error_box(self, "Google Earth pins", "Assign a project coordinate system first.")
            return
        p, _ = QFileDialog.getOpenFileName(self, "Google Earth pins", self._job_hint(), "KML / KMZ (*.kml *.kmz)")
        if not p:
            return
        try:
            batch = kml_io.read_kml(p, cache_dir=user_dir())
        except Exception as ex:
            error_box(self, "Google Earth pins", f"That file could not be read as KML/KMZ:\n{ex}", traceback.format_exc())
            return
        if not batch.points:
            error_box(self, "Google Earth pins", "No placemarks with points were found in that file.")
            return
        strat = pr.settings.get("kml_strategy") or "none"
        for pin in batch.points:                      # read_kml works in lon/lat - same datum handling as the export
            x, y = pr.crs.from_lonlat(pin.x, pin.y, source=4326, strategy=strat)
            pin.x, pin.y = float(x), float(y)
        plan = ImportPlan(dup_policy="overwrite", reference_role="other")
        stats = apply_import(self.state, batch, plan, f"Google Earth pins ({Path(p).name})", p)
        done = (f"{stats['overwritten']:,} updated" if stats.get("overwritten")
                else "measure between a pin and its point if you want the offset")
        self.state.log(f"{stats['points']:,} Google Earth pin(s) came in as reference points on layer "
                       f"{REF.layer_for('other')} - {done}.", "ok")
        self.d_img.show()
        self.d_img.raise_()
        self.imagery.refresh()
        self.state.zoom_extents()

    # ================================================================== reports
    def _show(self, rep):
        ReportViewer(self, rep).exec()

    def report_points(self):
        ids = set(self.state.sel_points) or None
        self._show(reports.points_report(self.state.project, ids, "Point List" + (" (selected)" if ids else "")))

    def report_polylines(self):
        pr = self.state.project
        ids = {i for i in self.state.sel_entities if isinstance(pr.entities.get(i), Polyline)}
        if not ids:
            info_box(self, "Line and Curve Table", "Select one or more polylines first.")
            return
        self._show(reports.polyline_report(pr, ids))

    def report_qa(self):
        if not self._ensure_fieldbook_for_processing():
            return
        pr = self.state.project
        self._show(reports.qa_report(pr, run_checks(pr)))

    def report_audit(self):
        """The Point(s) Audit: this job's field points against the state they were imported in.

        The comparison itself is :mod:`plumbline.core.audit`; the run is logged, because a report
        nobody can find again is a report nobody read, and the counts are the answer to the
        question the item was asked.
        """
        pr = self.state.project
        a = AUD.compare(pr)
        self.state.log(f"Point(s) Audit: {a.summary_line()}", "warn" if not a.clean else "ok")
        self._show(reports.point_audit_report(pr, a))

    def report_crs(self):
        self._show(reports.crs_report(self.state.project, self._datum_note()))

    # ================================================================== plugins
    def rebuild_plugins_menu(self, load: bool = False):
        if load:
            plugins.load_plugins()
        m = self.m_plugins
        m.clear()
        self._plugin_actions = []
        self._plugin_submenus = {}
        for cmd in plugins.registry.commands:
            parts = [p for p in cmd.menu.split("/") if p] if cmd.menu else []
            node = m
            path = ()
            for part in parts:
                path = path + (part,)
                if path not in self._plugin_submenus:
                    sub = node.addMenu(part)
                    self._all_menus.append(sub)
                    self._plugin_submenus[path] = sub
                node = self._plugin_submenus[path]
            act = QAction(cmd.name, self)
            act.setStatusTip(cmd.description)
            if cmd.shortcut:
                act.setShortcut(QKeySequence(cmd.shortcut))
            act.triggered.connect(lambda _=False, c=cmd: self.run_plugin_command(c))
            node.addAction(act)
            self._plugin_actions.append(act)
        # exporters join the File > Export menu (rebuilt each time)
        for a in getattr(self, "_plugin_export_actions", []):
            self.m_export.removeAction(a)
        self._plugin_export_actions = []
        for spec in plugins.registry.exporters:
            a = QAction(f"{spec.name} (plugin)...", self)
            a.triggered.connect(lambda _=False, sp=spec: self.run_plugin_exporter(sp))
            self.m_export.addAction(a)
            self._plugin_export_actions.append(a)
        if plugins.registry.commands:
            m.addSeparator()
        m.addAction("Run &Script...", self.run_script)
        m.addAction("Python &Console", lambda: (self.d_con.show(), self.d_con.raise_()))
        m.addSeparator()
        m.addAction("&Plugin Manager...", lambda: PluginsDialog(self, lambda: self.rebuild_plugins_menu(load=True)).exec())
        m.addAction("&Open Plugin Folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(plugins.plugin_dir()))))
        m.addAction("&Reload Plugins", lambda: (self.rebuild_plugins_menu(load=True), self.state.log("Plugins reloaded.", "info")))
        if plugins.registry.errors:
            self.state.log(f"{len(plugins.registry.errors)} plugin file(s) failed to load - see Plugins > Plugin Manager.", "warn")

    def _api(self):
        return plugins.PluginAPI(self.state.project, self.state, log=lambda s: self.state.log(s, "info"),
                                 ask=lambda p, d: QInputDialog.getText(self, "Plumbline", p, text=d)[0] or None,
                                 message=lambda t, m: info_box(self, t, m))

    def run_plugin_command(self, cmd):
        values = None
        if cmd.params:
            dlg = ParamDialog(cmd, self)
            if not dlg.exec():
                return
            values = dlg.values()
        api = self._api()
        try:
            with self.state.edit(cmd.name, discard_if_unchanged=True):
                plugins.run_command(cmd, api, values)
        except Exception as ex:
            error_box(self, cmd.name, f"The command failed and its changes were rolled back:\n{ex}", traceback.format_exc())
            self.state.log(f"Plugin command '{cmd.name}' failed: {ex}", "error")

    def run_plugin_exporter(self, spec):
        p, _ = QFileDialog.getSaveFileName(self, spec.name, f"{self.state.project.name}{spec.extension}", f"*{spec.extension}")
        if not p:
            return
        try:
            spec.func(self.state.project, p, self._api())
            self.state.log(f"{spec.name}: wrote {p}", "ok")
        except Exception as ex:
            error_box(self, spec.name, str(ex), traceback.format_exc())

    def run_script(self):
        p, _ = QFileDialog.getOpenFileName(self, "Run script", "", "Python (*.py)")
        if not p:
            return
        class _ScriptError(Exception):
            pass

        try:
            with self.state.edit(f"Script {Path(p).name}", discard_if_unchanged=True):
                err = plugins.run_script(p, self.state.project, self.state, log=lambda s: self.state.log(s, "info"),
                                         ask=lambda pr, d: QInputDialog.getText(self, "Plumbline", pr, text=d)[0] or None,
                                         message=lambda t, m: info_box(self, t, m))
                if err:
                    raise _ScriptError(err)
        except _ScriptError as ex:
            error_box(self, "Script", "The script raised an error; its changes were rolled back.", str(ex))
            self.state.log(f"Script {Path(p).name} failed.", "error")
        else:
            self.state.log(f"Script {Path(p).name} finished.", "ok")
