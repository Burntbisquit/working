"""General dialogs: report viewer, settings, feature codes, QA, transform, traverse / inverse, plugins, help, welcome."""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QMarginsF, QSizeF, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from .. import __version__
from ..core import cogo
from ..core import units as U
from ..core.featurecodes import FeatureCode, FeatureCodeTable, default_codes
from ..core.settings import default_project_folder, settings, user_dir
from ..io import reports
from ..core.qa import run_checks
from .widgets import Banner, ColorButton, FormDialog, Hint, dspin, error_box, info_box, ispin, confirm


# ----------------------------------------------------------------------------- report viewer
class ReportViewer(QDialog):
    def __init__(self, parent, report: reports.Report, kind: str = "report"):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle(report.title)
        self.resize(940, 760)
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.b_pdf = QPushButton("Save PDF...")
        self.b_html = QPushButton("Save HTML...")
        self.b_csv = QPushButton("Export CSV...")
        self.b_xlsx = QPushButton("Export Excel...")
        for b in (self.b_pdf, self.b_html, self.b_csv, self.b_xlsx):
            bar.addWidget(b)
        bar.addStretch(1)
        self.b_close = QPushButton("Close")
        bar.addWidget(self.b_close)
        lay.addLayout(bar)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        # reports are designed as a white page (and print / PDF that way) whatever the app theme is
        self.view.setStyleSheet("QTextBrowser { background: #ffffff; color: #1f252b; border: 1px solid #c9ced6; }")
        self.html = reports.to_html(report)
        self.view.setHtml(self.html)
        lay.addWidget(self.view, 1)
        self.b_pdf.clicked.connect(self.save_pdf)
        self.b_html.clicked.connect(self.save_html)
        self.b_csv.clicked.connect(self.save_csv)
        self.b_xlsx.clicked.connect(self.save_xlsx)
        self.b_close.clicked.connect(self.accept)
        self._stem = "".join(c if c.isalnum() else "_" for c in report.title)[:40]

    def _path(self, caption, ext, filt):
        p, _ = QFileDialog.getSaveFileName(self, caption, f"{self._stem}{ext}", filt)
        return p

    def write_pdf(self, path: str):
        doc = QTextDocument()
        doc.setHtml(self.html)
        w = QPdfWriter(path)
        w.setPageSize(QPageSize(QPageSize.Letter))
        w.setPageMargins(QMarginsF(14, 14, 14, 14), QPageLayout.Millimeter)
        w.setResolution(120)
        w.setTitle(self.report.title)
        w.setCreator("Plumbline")
        doc.setPageSize(QSizeF(w.width(), w.height()))
        doc.print_(w)

    def save_pdf(self):
        p = self._path("Save PDF", ".pdf", "PDF (*.pdf)")
        if p:
            self.write_pdf(p)
            info_box(self, "Report", f"Saved {p}")

    def save_html(self):
        p = self._path("Save HTML", ".html", "HTML (*.html)")
        if p:
            Path(p).write_text(self.html, encoding="utf-8")

    def save_csv(self):
        p = self._path("Export CSV", ".csv", "CSV (*.csv)")
        if p:
            reports.to_csv(self.report, p)

    def save_xlsx(self):
        p = self._path("Export Excel", ".xlsx", "Excel (*.xlsx)")
        if p:
            reports.to_xlsx(self.report, p)


# ----------------------------------------------------------------------------- settings
class SettingsDialog(FormDialog):
    def __init__(self, parent=None):
        super().__init__(parent, "Settings", "", "Save", 680)
        self.setMinimumWidth(640)
        s = settings()
        self.cmb_theme = QComboBox()
        self.cmb_theme.addItem("Dark", "dark")
        self.cmb_theme.addItem("Light", "light")
        self.cmb_theme.setCurrentIndex(max(0, self.cmb_theme.findData(s.get("theme"))))
        self.cmb_order = QComboBox()
        self.cmb_order.addItem("Northing, Easting  (surveyor's order)", "NE")
        self.cmb_order.addItem("X, Y  (Easting, Northing - CAD order)", "XY")
        self.cmb_order.setCurrentIndex(max(0, self.cmb_order.findData(s.get("coord_order"))))
        self.cmb_ang = QComboBox()
        self.cmb_ang.addItem("Quadrant bearings (N 45°30'15\" E)", "quadrant")
        self.cmb_ang.addItem("Azimuths (45°30'15\")", "azimuth")
        self.cmb_ang.setCurrentIndex(max(0, self.cmb_ang.findData(s.get("angle_format"))))
        self.chk_dms = QCheckBox("Degrees-minutes-seconds (otherwise decimal degrees)")
        self.chk_dms.setChecked(bool(s.get("angle_dms")))
        self.sp_dec = ispin(int(s.get("angle_decimals")), 0, 4)
        self.sp_snap = ispin(int(s.get("snap_px")), 4, 40)
        self.sp_pt = ispin(int(s.get("point_size_px")), 3, 24)
        self.sp_lab = ispin(int(s.get("label_px")), 7, 24)
        try:
            control_tolerance = float(s.get("control_point_tolerance", 0.01))
            if not math.isfinite(control_tolerance) or control_tolerance < 0:
                control_tolerance = 0.01
        except (TypeError, ValueError, OverflowError):
            control_tolerance = 0.01
        self.sp_control_tolerance = dspin(control_tolerance, 0.0, 10000.0, 4, 0.01)
        self.sp_control_tolerance.setToolTip(
            "Survey-point differences at or below this amount are treated as coordinate rounding. "
            "The value is applied in each coordinate's stored units (horizontal for northing/easting, "
            "vertical for elevation).")
        self.chk_require_fieldbook = QCheckBox(
            "Require a Field Book before point, code, and linework processing")
        self.chk_require_fieldbook.setChecked(bool(s.get("require_fieldbook_for_processing", True)))
        self.chk_require_fieldbook.setToolTip(
            "When enabled, opening a project without a usable Field Book opens the Field Book tool automatically. "
            "Processing actions open it again if needed and stay blocked unless a Field Book is loaded. "
            "Turn this off to allow processing without one.")
        self.ed_cache = QLineEdit(s.get("tile_cache_dir") or "")
        self.ed_cache.setPlaceholderText(str(s.tile_cache_dir))
        self.ed_project_folder = QLineEdit(
            str(s.get("default_project_folder") or default_project_folder()))
        self.ed_project_folder.setToolTip(
            "New Project, New Job Folder, and Save As create project folders inside this location.")
        self.browse_project_folder = QPushButton("Browse...")
        self.browse_project_folder.clicked.connect(self._browse_project_folder)
        project_folder_row = QHBoxLayout()
        project_folder_row.addWidget(self.ed_project_folder, 1)
        project_folder_row.addWidget(self.browse_project_folder)
        project_folder_holder = QWidget()
        project_folder_holder.setLayout(project_folder_row)
        self.chk_space_commands = QCheckBox("Separate a feature code from its line command")
        self.chk_space_commands.setChecked(bool(s.get("space_between_commands", True)))
        self.chk_space_multicode = QCheckBox("Add spaces around the multi-code separator")
        self.chk_space_multicode.setChecked(bool(s.get("space_around_multicode_separator", True)))
        self.chk_space_description = QCheckBox("Add spaces around the description separator")
        self.chk_space_description.setChecked(bool(s.get("space_around_description_separator", True)))
        self.form.addRow("Theme:", self.cmb_theme)
        self.form.addRow("Typed coordinate order:", self.cmb_order)
        self.form.addRow("Angles:", self.cmb_ang)
        self.form.addRow("", self.chk_dms)
        self.form.addRow("Seconds decimals:", self.sp_dec)
        self.form.addRow("Snap distance (pixels):", self.sp_snap)
        self.form.addRow("Point symbol size (pixels):", self.sp_pt)
        self.form.addRow("Label size (pixels):", self.sp_lab)
        self.form.addRow("Control comparison tolerance:", self.sp_control_tolerance)
        self.form.addRow("", self.chk_require_fieldbook)
        self.form.addRow("Default project folder:", project_folder_holder)
        self.form.addRow("Imagery cache folder:", self.ed_cache)
        syntax_box = QGroupBox("Field Book command spacing")
        syntax_layout = QVBoxLayout(syntax_box)
        syntax_layout.addWidget(self.chk_space_commands)
        syntax_layout.addWidget(self.chk_space_multicode)
        syntax_layout.addWidget(self.chk_space_description)
        self.root.insertWidget(self.root.count() - 1, syntax_box)

        # -- the settings file and the list of everything we pull from (items 3 and 9)
        row = QHBoxLayout()
        self.b_sites = QPushButton("External data sources...")
        self.b_sites.setToolTip("Every imagery, coordinate-system, geoid and update source this program reads,\n"
                               "listed and editable - and where a settings file is saved and loaded.")
        self.b_save_settings = QPushButton("Save settings...")
        self.b_load_settings = QPushButton("Load settings...")
        for b in (self.b_sites, self.b_save_settings, self.b_load_settings):
            row.addWidget(b)
        row.addStretch(1)
        self.root.insertLayout(self.root.count() - 1, row)
        self.b_sites.clicked.connect(self.open_registry)
        self.b_save_settings.clicked.connect(self.save_settings)
        self.b_load_settings.clicked.connect(self.load_settings)

    def _browse_project_folder(self):
        start = Path(self.ed_project_folder.text()).expanduser()
        if not start.is_dir():
            start = start.parent if start.parent.is_dir() else Path.home()
        folder = QFileDialog.getExistingDirectory(
            self, "Default project folder", str(start))
        if folder:
            self.ed_project_folder.setText(folder)

    # -- settings files / the pull-site registry
    def open_registry(self):
        from .registry_ui import RegistryDialog
        RegistryDialog(self).exec()

    def save_settings(self):
        """Save the settings file from inside Settings - the values on screen are written first."""
        self.apply()
        p, _ = QFileDialog.getSaveFileName(self, "Save settings", "plumbline-settings.json",
                                           "JSON (*.json);;All files (*)")
        if not p:
            return
        try:
            settings().export_to(p)
        except OSError as ex:
            error_box(self, "Save Settings", f"Could not write {p}", str(ex))
            return
        info_box(self, "Save Settings", f"Saved to {p}")

    def load_settings(self):
        p, _ = QFileDialog.getOpenFileName(self, "Load settings", "", "JSON (*.json);;All files (*)")
        if not p:
            return
        try:
            changed = settings().load_from(p)
        except Exception as ex:
            error_box(self, "Load Settings", f"Could not read {p}", str(ex))
            return
        # show what it did, then put the loaded values on screen
        self.__init__(self.parent())
        info_box(self, "Load Settings",
                 f"Read {p}.\n\nChanged: {', '.join(changed) if changed else 'nothing - the file matches'}")

    def apply(self):
        s = settings()
        s.set("theme", self.cmb_theme.currentData(), False)
        s.set("coord_order", self.cmb_order.currentData(), False)
        s.set("angle_format", self.cmb_ang.currentData(), False)
        s.set("angle_dms", self.chk_dms.isChecked(), False)
        s.set("angle_decimals", self.sp_dec.value(), False)
        s.set("snap_px", self.sp_snap.value(), False)
        s.set("point_size_px", self.sp_pt.value(), False)
        s.set("label_px", self.sp_lab.value(), False)
        s.set("control_point_tolerance", self.sp_control_tolerance.value(), False)
        s.set("require_fieldbook_for_processing", self.chk_require_fieldbook.isChecked(), False)
        s.set("space_between_commands", self.chk_space_commands.isChecked(), False)
        s.set("space_around_multicode_separator", self.chk_space_multicode.isChecked(), False)
        s.set("space_around_description_separator", self.chk_space_description.isChecked(), False)
        s.set("default_project_folder", self.ed_project_folder.text().strip(), False)
        s.set("tile_cache_dir", self.ed_cache.text().strip(), True)


# ----------------------------------------------------------------------------- feature codes
class FeatureCodesDialog(QDialog):
    COLS = ["Code", "Name", "Kind", "Layer", "Colour", "Linetype", "Breakline", "Ground"]

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Feature Codes")
        self.resize(1000, 640)
        lay = QVBoxLayout(self)
        lay.addWidget(Hint("A point description starting with a code puts the point on that code's layer and, for line / polygon codes, "
                           "joins consecutive points into linework (EP B ... EP ... EP E).  Breakline codes become TIN breaklines; points of codes "
                           "that are not 'Ground' stay out of the ground surface."))
        self.tbl = QTableWidget(0, len(self.COLS))
        self.tbl.setHorizontalHeaderLabels(self.COLS)
        self.tbl.horizontalHeader().setStretchLastSection(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(self.tbl, 1)
        row = QHBoxLayout()
        self.b_add = QPushButton("Add code")
        self.b_del = QPushButton("Delete selected")
        self.b_def = QPushButton("Reset to defaults")
        for b in (self.b_add, self.b_del, self.b_def):
            row.addWidget(b)
        row.addStretch(1)
        self.chk_apply = QCheckBox("Re-apply codes to existing points (move them to their layers)")
        self.chk_apply.setChecked(True)
        row.addWidget(self.chk_apply)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.b_add.clicked.connect(lambda: self._add_row(FeatureCode("NEW", "New code", "point", "NEW-LAYER")))
        self.b_del.clicked.connect(self._del)
        self.b_def.clicked.connect(self._defaults)
        self._fill(state.project.codes)

    def _fill(self, table: FeatureCodeTable):
        self.tbl.setRowCount(0)
        for fc in table:
            self._add_row(fc)
        self.tbl.resizeColumnsToContents()
        self.tbl.setColumnWidth(1, 220)
        self.tbl.setColumnWidth(3, 160)

    def _add_row(self, fc: FeatureCode):
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        self.tbl.setItem(r, 0, QTableWidgetItem(fc.code))
        self.tbl.setItem(r, 1, QTableWidgetItem(fc.name))
        k = QComboBox()
        for kind in ("point", "line", "polygon"):
            k.addItem(kind)
        k.setCurrentText(fc.kind)
        self.tbl.setCellWidget(r, 2, k)
        self.tbl.setItem(r, 3, QTableWidgetItem(fc.layer))
        cb = ColorButton(fc.color)
        self.tbl.setCellWidget(r, 4, cb)
        lt = QComboBox()
        for name in ("CONTINUOUS", "DASHED", "DASHDOT", "CENTER", "HIDDEN", "PHANTOM", "DOT"):
            lt.addItem(name)
        lt.setCurrentText(fc.linetype if fc.linetype in ("CONTINUOUS", "DASHED", "DASHDOT", "CENTER", "HIDDEN", "PHANTOM", "DOT") else "CONTINUOUS")
        self.tbl.setCellWidget(r, 5, lt)
        for col, val in ((6, fc.breakline), (7, fc.ground)):
            it = QTableWidgetItem()
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            it.setCheckState(Qt.Checked if val else Qt.Unchecked)
            self.tbl.setItem(r, col, it)

    def _del(self):
        for r in sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True):
            self.tbl.removeRow(r)

    def _defaults(self):
        if confirm(self, "Feature Codes", "Replace the table with the built-in starter library?", "Replace"):
            self._fill(default_codes())

    def _table(self) -> FeatureCodeTable:
        out = []
        for r in range(self.tbl.rowCount()):
            code = (self.tbl.item(r, 0).text() if self.tbl.item(r, 0) else "").strip().upper()
            if not code:
                continue
            out.append(FeatureCode(code, self.tbl.item(r, 1).text() if self.tbl.item(r, 1) else "", self.tbl.cellWidget(r, 2).currentText(),
                                   self.tbl.item(r, 3).text().strip() if self.tbl.item(r, 3) else "", "cross",
                                   self.tbl.cellWidget(r, 4).rgb(), self.tbl.cellWidget(r, 5).currentText(),
                                   self.tbl.item(r, 6).checkState() == Qt.Checked, self.tbl.item(r, 7).checkState() == Qt.Checked))
        return FeatureCodeTable(out)

    def _ok(self):
        raw = [(self.tbl.item(r, 0).text() if self.tbl.item(r, 0) else "").strip().upper() for r in range(self.tbl.rowCount())]
        raw = [c for c in raw if c]
        dups = sorted({c for c in raw if raw.count(c) > 1})
        if dups:                                  # (the table itself is a dict - it would silently keep only the last row)
            error_box(self, "Feature Codes", f"Two rows use the same code: {', '.join(dups)}.")
            return
        t = self._table()
        with self.state.edit("Edit feature codes"):
            pr = self.state.project
            pr.codes = t
            for fc in t:
                if fc.layer:
                    lay = pr.ensure_layer(fc.layer, fc.color, fc.linetype if fc.kind != "point" else "CONTINUOUS")
                    lay.color = tuple(fc.color)
            if self.chk_apply.isChecked():
                pr.apply_codes_to_points()
        self.accept()


# ----------------------------------------------------------------------------- QA
class QADialog(QDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Data Quality Check")
        self.resize(920, 560)
        self.issues = []
        lay = QVBoxLayout(self)
        f = QHBoxLayout()
        self.sp_xy = dspin(0.01, 0.0, 1000, 3, 0.01)
        self.sp_z = dspin(0.05, 0.0, 1000, 3, 0.01)
        self.sp_k = dspin(6.0, 1.0, 50, 1, 1.0)
        self.sp_min = dspin(round(0.23 / U.M_PER_UNIT.get(state.project.v_unit, 1.0), 2), 0.0, 1000, 2, 0.1)
        for lbl, w in (("Same-spot distance", self.sp_xy), ("Elevation tolerance", self.sp_z), ("Bust sensitivity (x scatter)", self.sp_k),
                       ("Smallest bust", self.sp_min)):
            f.addWidget(QLabel(lbl))
            f.addWidget(w)
        self.btn_run = QPushButton("Run checks")
        self.btn_run.setProperty("accent", True)
        f.addWidget(self.btn_run)
        lay.addLayout(f)
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["Level", "Check", "Finding", "Points"])
        self.tbl.horizontalHeader().setStretchLastSection(False)
        self.tbl.setColumnWidth(0, 70)
        self.tbl.setColumnWidth(1, 150)
        self.tbl.setColumnWidth(2, 560)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.tbl, 1)
        lay.addWidget(Hint("Select a finding to select those points in the drawing; double-click to zoom to them."))
        bb = QHBoxLayout()
        self.b_rep = QPushButton("Report...")
        self.b_close = QPushButton("Close")
        bb.addWidget(self.b_rep)
        bb.addStretch(1)
        bb.addWidget(self.b_close)
        lay.addLayout(bb)
        self.btn_run.clicked.connect(self.run)
        self.tbl.itemSelectionChanged.connect(self._select)
        self.tbl.cellDoubleClicked.connect(self._zoom)
        self.b_rep.clicked.connect(self._report)
        self.b_close.clicked.connect(self.accept)
        self.run()

    def run(self):
        self.issues = run_checks(self.state.project, self.sp_z.value(), self.sp_xy.value(), self.sp_k.value(), self.sp_min.value() or None)
        self.tbl.setRowCount(len(self.issues))
        colors = {"error": "#ff6b6b", "warn": "#f0b429", "info": "#8a94a1"}
        for r, i in enumerate(self.issues):
            vals = [i.severity.upper(), i.kind, i.message, str(len(i.point_ids)) if i.point_ids else ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    from PySide6.QtGui import QColor
                    it.setForeground(QColor(colors[i.severity]))
                self.tbl.setItem(r, c, it)
            self.tbl.setRowHeight(r, 38)
            self.tbl.item(r, 2).setToolTip(i.message)
        self.state.log(f"Data check: {sum(1 for i in self.issues if i.severity in ('error', 'warn'))} finding(s) need attention.", "info")

    def _ids(self):
        r = self.tbl.currentRow()
        return self.issues[r].point_ids if 0 <= r < len(self.issues) else []

    def _select(self):
        ids = self._ids()
        if ids:
            self.state.select(points=ids)

    def _zoom(self, r, c):
        ids = self._ids()
        pr = self.state.project
        pts = [pr.points[i] for i in ids if i in pr.points]
        if pts:
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            pad = 15
            self.state.zoom_to_bbox((min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))

    def _report(self):
        ReportViewer(self, reports.qa_report(self.state.project, self.issues)).exec()


# ----------------------------------------------------------------------------- transform
class TransformDialog(FormDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent, "Translate / Rotate / Scale",
                         "Order of operations: scale about the base point, rotate (counter-clockwise) about it, then shift. "
                         "Use it to localise a drawing, fix a rotation, or convert a drawing in the wrong units.", "Apply", 520)
        self.state = state
        pr = state.project
        has_sel = bool(state.sel_points or state.sel_entities)
        self.r_all = QRadioButton("Everything (points, linework, text and surfaces)")
        self.r_sel = QRadioButton("Only the selected objects")
        self.r_all.setChecked(not has_sel)
        self.r_sel.setChecked(has_sel)
        self.r_sel.setEnabled(has_sel)
        ext = pr.extents()
        cx, cy = ((ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2) if ext else (0.0, 0.0)
        self.sp_bn = dspin(cy, -1e12, 1e12, 3)
        self.sp_be = dspin(cx, -1e12, 1e12, 3)
        self.sp_scale = dspin(1.0, -1e6, 1e6, 8, 0.001)
        self.sp_rot = dspin(0.0, -360, 360, 6, 0.1)
        self.sp_dn = dspin(0.0, -1e12, 1e12, 3)
        self.sp_de = dspin(0.0, -1e12, 1e12, 3)
        self.sp_dz = dspin(0.0, -1e9, 1e9, 3)
        self.form.addRow("Apply to:", self.r_all)
        self.form.addRow("", self.r_sel)
        self.form.addRow("Base northing:", self.sp_bn)
        self.form.addRow("Base easting:", self.sp_be)
        self.form.addRow("Scale factor:", self.sp_scale)
        self.form.addRow("Rotate (deg, counter-clockwise):", self.sp_rot)
        self.form.addRow("Shift northing:", self.sp_dn)
        self.form.addRow("Shift easting:", self.sp_de)
        self.form.addRow("Shift elevation:", self.sp_dz)

    def validate(self):
        if self.sp_scale.value() == 0:
            return "The scale factor cannot be zero."
        return None

    def apply(self):
        st = self.state
        sel = self.r_sel.isChecked()
        with st.edit("Translate / rotate / scale"):
            st.state_stats = st.project.apply_similarity(
                (self.sp_be.value(), self.sp_bn.value()), self.sp_scale.value(), self.sp_rot.value(),
                (self.sp_de.value(), self.sp_dn.value()), self.sp_dz.value(), 1.0,
                set(st.sel_points) if sel else None, set(st.sel_entities) if sel else None)
        return st.state_stats


# ----------------------------------------------------------------------------- traverse & inverse
class TraverseDialog(QDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("COGO — Traverse and Inverse")
        self.resize(820, 620)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)
        # ---- traverse
        t = QWidget()
        tl = QVBoxLayout(t)
        f = QHBoxLayout()
        pr = state.project
        self.sp_n = dspin(0.0, -1e12, 1e12, 3)
        self.sp_e = dspin(0.0, -1e12, 1e12, 3)
        ext = pr.extents()
        f.addWidget(QLabel("Start northing"))
        f.addWidget(self.sp_n)
        f.addWidget(QLabel("easting"))
        f.addWidget(self.sp_e)
        self.chk_close = QCheckBox("Closes back to the start")
        self.chk_close.setChecked(True)
        f.addWidget(self.chk_close)
        tl.addLayout(f)
        tl.addWidget(Hint("Enter each course as a bearing (N45-30-15E, S12°30'W, N10E) or an azimuth (123.4567) and a distance."))
        self.tbl = QTableWidget(12, 2)
        self.tbl.setHorizontalHeaderLabels(["Bearing / azimuth", f"Distance ({U.LABEL.get(pr.h_unit, pr.h_unit)})"])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setColumnWidth(0, 240)
        tl.addWidget(self.tbl, 1)
        row = QHBoxLayout()
        self.b_calc = QPushButton("Compute")
        self.b_calc.setProperty("accent", True)
        self.b_poly = QPushButton("Draw polyline")
        self.b_pts = QPushButton("Add as points")
        self.chk_adj = QCheckBox("Use compass-rule adjusted coordinates")
        for w in (self.b_calc, self.b_poly, self.b_pts, self.chk_adj):
            row.addWidget(w)
        row.addStretch(1)
        tl.addLayout(row)
        self.out = QTextBrowser()
        self.out.setMaximumHeight(210)
        tl.addWidget(self.out)
        tabs.addTab(t, "Traverse")
        # ---- inverse
        i = QWidget()
        il = QFormLayout(i)
        self.ed_a = QLineEdit()
        self.ed_b = QLineEdit()
        self.ed_a.setPlaceholderText("point number")
        self.ed_b.setPlaceholderText("point number")
        il.addRow("From point:", self.ed_a)
        il.addRow("To point:", self.ed_b)
        self.b_inv = QPushButton("Inverse")
        il.addRow("", self.b_inv)
        self.inv_out = QTextBrowser()
        il.addRow(self.inv_out)
        tabs.addTab(i, "Inverse Between Points")
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.b_calc.clicked.connect(self.compute)
        self.b_poly.clicked.connect(lambda: self._add("poly"))
        self.b_pts.clicked.connect(lambda: self._add("pts"))
        self.b_inv.clicked.connect(self.inverse)
        self.result = None

    def _courses(self):
        out = []
        for r in range(self.tbl.rowCount()):
            a = self.tbl.item(r, 0).text().strip() if self.tbl.item(r, 0) else ""
            d = self.tbl.item(r, 1).text().strip() if self.tbl.item(r, 1) else ""
            if not a and not d:
                continue
            az = cogo.parse_bearing(a)
            try:
                dist = float(d.replace(",", ""))
            except ValueError:
                raise ValueError(f"Row {r + 1}: '{d}' is not a distance.")
            if az is None:
                raise ValueError(f"Row {r + 1}: '{a}' is not a bearing or azimuth.")
            out.append((az, dist))
        if not out:
            raise ValueError("Enter at least one course.")
        return out

    def compute(self):
        try:
            courses = self._courses()
        except ValueError as ex:
            self.out.setHtml(f"<p style='color:#ff6b6b'>{ex}</p>")
            return None
        res = cogo.traverse((self.sp_e.value(), self.sp_n.value()), courses, self.chk_close.isChecked())
        pts = res["points"]
        adj = None
        if self.chk_close.isChecked() and len(pts) > 2:
            adj = cogo.bowditch(pts, [abs(d) for _, d in courses])
        u = U.LABEL.get(self.state.project.h_unit, self.state.project.h_unit)
        s = settings()
        h = [f"<p><b>Perimeter</b> {res['perimeter']:,.3f} {u}"]
        if "linear_error" in res:
            prec = "perfect" if math.isinf(res["precision"]) else f"1 : {res['precision']:,.0f}"
            h.append(f" &nbsp; <b>Misclosure</b> dN {res['misclosure_n']:+.4f}, dE {res['misclosure_e']:+.4f} &nbsp; "
                     f"<b>Linear error</b> {res['linear_error']:.4f} {u} &nbsp; <b>Precision</b> {prec}<br/>"
                     f"<b>Area</b> {res['area']:,.2f} sq {self.state.project.h_unit} = {U.area_to_acres(res['area'], self.state.project.h_unit):,.4f} acres")
        h.append("</p><table cellpadding='2'><tr><th>#</th><th>Bearing</th><th>Distance</th><th>Northing</th><th>Easting</th>" +
                 ("<th>Adj. N</th><th>Adj. E</th>" if adj is not None else "") + "</tr>")
        for k in range(len(pts)):
            b = d = ""
            if k > 0:
                az, dist = courses[k - 1]
                b = cogo.format_angle(az, s.get("angle_format"), s.get("angle_dms"), s.get("angle_decimals"))
                d = f"{dist:,.3f}"
            row = f"<tr><td>{k}</td><td>{b}</td><td align='right'>{d}</td><td align='right'>{pts[k, 1]:,.3f}</td><td align='right'>{pts[k, 0]:,.3f}</td>"
            if adj is not None:
                row += f"<td align='right'>{adj[k, 1]:,.3f}</td><td align='right'>{adj[k, 0]:,.3f}</td>"
            h.append(row + "</tr>")
        h.append("</table>")
        self.out.setHtml("".join(h))
        self.result = (pts, adj, res)
        return self.result

    def _add(self, what):
        r = self.compute()
        if r is None:
            return
        pts, adj, res = r
        use = adj if (adj is not None and self.chk_adj.isChecked()) else pts
        pr = self.state.project
        closed = self.chk_close.isChecked() and len(use) > 2
        if closed:
            use = use[:-1]
        with self.state.edit("Traverse"):
            if what == "poly":
                pr.ensure_layer("TRAVERSE", (255, 120, 255))
                e = pr.add_polyline(np.column_stack([use, np.full(len(use), math.nan)]), "TRAVERSE", closed=closed)
                self.state.select(entities=[e.id])
            else:
                for x, y in use:
                    pr.add_point(float(x), float(y), math.nan, desc="TRAV")
        self.state.zoom_extents()
        self.accept()

    def inverse(self):
        pr = self.state.project
        a, b = pr.point_by_number(self.ed_a.text().strip()), pr.point_by_number(self.ed_b.text().strip())
        if a is None or b is None:
            self.inv_out.setHtml("<p style='color:#ff6b6b'>One of those point numbers does not exist.</p>")
            return
        az, d = cogo.inverse(a.x, a.y, b.x, b.y)
        s = settings()
        u = U.LABEL.get(pr.h_unit, pr.h_unit)
        h = (f"<p><b>{a.number} to {b.number}</b></p><p>Horizontal distance <b>{d:,.4f}</b> {u}<br/>"
             f"Bearing <b>{cogo.format_bearing(az, True, 2)}</b> &nbsp; azimuth <b>{cogo.format_azimuth(az, True, 2)}</b> "
             f"({az:.6f}°)<br/>dN {b.y - a.y:+,.4f} &nbsp; dE {b.x - a.x:+,.4f}")
        if not (math.isnan(a.z) or math.isnan(b.z)):
            dz = b.z - a.z
            h += f"<br/>dElev {dz:+,.3f} &nbsp; slope {100 * dz / d if d else 0:+.2f}% &nbsp; 3D distance {math.hypot(d, dz):,.4f}"
        self.inv_out.setHtml(h + "</p>")


# ----------------------------------------------------------------------------- plugins / help / about / welcome
class PluginsDialog(QDialog):
    def __init__(self, parent=None, on_reload=None):
        super().__init__(parent)
        from .. import plugins
        self.setWindowTitle("Plugins")
        self.resize(760, 560)
        self.on_reload = on_reload
        lay = QVBoxLayout(self)
        self.view = QTextBrowser()
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        b1 = QPushButton("Open plugin folder")
        b2 = QPushButton("Install example plugins")
        b3 = QPushButton("Reload")
        for b in (b1, b2, b3):
            row.addWidget(b)
        row.addStretch(1)
        bc = QPushButton("Close")
        row.addWidget(bc)
        lay.addLayout(row)
        b1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(plugins.plugin_dir()))))
        b2.clicked.connect(self._install)
        b3.clicked.connect(self._reload)
        bc.clicked.connect(self.accept)
        self._render()

    def _install(self):
        from .. import plugins
        names = plugins.install_examples()
        self._reload()
        info_box(self, "Plugins", f"Installed: {', '.join(names)}" if names else "The examples are already in your plugin folder.")

    def _reload(self):
        if self.on_reload:
            self.on_reload()
        self._render()

    def _render(self):
        from .. import plugins
        import html
        r = plugins.registry
        h = [f"<h3>Plugin folder</h3><p><code>{html.escape(str(plugins.plugin_dir()))}</code></p>",
             "<p>Any <code>.py</code> file in this folder is loaded at start-up. See the example plugins for the (small) API.</p>"]
        h.append(f"<h3>Commands ({len(r.commands)})</h3><ul>" + "".join(
            f"<li><b>{html.escape(c.path)}</b> - {html.escape(c.description or '')} <i>({html.escape(Path(c.source).name)})</i></li>" for c in r.commands) + "</ul>")
        h.append(f"<h3>Importers ({len(r.importers)})</h3><ul>" + "".join(
            f"<li>{html.escape(i.name)} {html.escape(' '.join(i.extensions))}</li>" for i in r.importers) + "</ul>")
        h.append(f"<h3>Exporters ({len(r.exporters)})</h3><ul>" + "".join(
            f"<li>{html.escape(e.name)}</li>" for e in r.exporters) + "</ul>")
        if r.errors:
            h.append("<h3 style='color:#ff6b6b'>Errors</h3>")
            for f, tb in r.errors:
                h.append(f"<p><b>{html.escape(Path(f).name)}</b></p><pre>{html.escape(tb)}</pre>")
        self.view.setHtml("".join(h))


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Plumbline — Quick Start")
        self.resize(820, 760)
        lay = QVBoxLayout(self)
        v = QTextBrowser()
        p = Path(__file__).resolve().parent.parent / "resources" / "quickstart.md"
        try:
            v.setMarkdown(p.read_text("utf-8"))
        except OSError:
            v.setPlainText("Quick-start file not found.")
        lay.addWidget(v)
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        lay.addWidget(b, 0, Qt.AlignRight)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Plumbline")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        import PySide6, ezdxf, pyproj, shapely
        try:
            import rasterio
            rv = rasterio.__version__
        except Exception:
            rv = "not installed"
        t = QTextBrowser()
        t.setOpenExternalLinks(True)
        t.setHtml(f"<h2>Plumbline {__version__}</h2><p>Planimetric and topographic CAD for survey data.</p>"
                  f"<p>Python {sys.version.split()[0]} &middot; Qt/PySide6 {PySide6.__version__} &middot; pyproj {pyproj.__version__} "
                  f"(PROJ {pyproj.proj_version_str}) &middot; shapely {shapely.__version__} &middot; ezdxf {ezdxf.__version__} &middot; rasterio {rv}</p>"
                  f"<p>User data folder: <code>{user_dir()}</code></p>"
                  "<p>Imagery tiles come from third-party services (Esri, USGS, OpenStreetMap...) under their own terms; "
                  "attribution is shown on the map. Satellite imagery is a secondary source - see the accuracy lookup.</p>")
        lay.addWidget(t)
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        lay.addWidget(b, 0, Qt.AlignRight)


class WelcomeDialog(QDialog):
    """Returns .choice = new | open | sample | recent:<path> | none"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Plumbline")
        self.setMinimumWidth(520)
        self.choice = "none"
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<h2>Plumbline</h2><p>Planimetric and topographic CAD for your survey data.</p>"))
        for text, key in (("New project...", "new"), ("Open project...", "open"),
                          ("Open the synthetic sample (a computer-generated lot)", "sample"),
                          ("Open the Real World sample (a real reduced job)", "sample_real")):
            b = QPushButton(text)
            b.setMinimumHeight(34)
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            lay.addWidget(b)
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
        if rec:
            lay.addWidget(QLabel("Recent projects"))
            self.lst = QListWidget()
            self.lst.setMaximumHeight(130)
            for p in rec:
                it = QListWidgetItem(Path(p).name + "    " + str(Path(p).parent))
                it.setData(Qt.UserRole, p)
                self.lst.addItem(it)
            self.lst.itemActivated.connect(lambda it: self._pick("recent:" + it.data(Qt.UserRole)))
            lay.addWidget(self.lst)
        self.chk = QCheckBox("Show this window at start-up")
        self.chk.setChecked(bool(settings().get("show_welcome", True)))
        lay.addWidget(self.chk)
        b = QPushButton("Start with an empty local project")
        b.clicked.connect(lambda: self._pick("none"))
        lay.addWidget(b)

    def _pick(self, key):
        self.choice = key
        settings().set("show_welcome", self.chk.isChecked())
        self.accept()

    def reject(self):
        settings().set("show_welcome", self.chk.isChecked())
        super().reject()
