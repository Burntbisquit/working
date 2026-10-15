"""Export option dialogs (DXF, LandXML, GIS, KML/KMZ, point CSV) and the functions that run them."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from ..core import crs as C
from ..core import reference as REF
from ..core import units as U
from ..io import csv_points as CSV
from ..io import dxf_io, gis_io, kml_io, landxml
from .import_export import CRSChooser
from .widgets import Banner, FormDialog, Hint, dspin, error_box, run_blocking


def _selected_ids(state):
    return (set(state.sel_points) or None), (set(state.sel_entities) or None)


# ----------------------------------------------------------------------------- DXF
class DxfExportDialog(FormDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent, "Export DXF", "Layers, colours, arcs, contours at elevation and labelled points - ready for AutoCAD / Civil 3D / BricsCAD.",
                         "Export", 520)
        self.state = state
        pr = state.project
        self.cmb_ver = QComboBox()
        for lbl, v in [("AutoCAD 2018 / 2019 / 2020...", "R2018"), ("AutoCAD 2013 / 2014 / 2015 / 2016 / 2017", "R2013"),
                       ("AutoCAD 2010 / 2011 / 2012", "R2010"), ("AutoCAD 2007 / 2008 / 2009", "R2007"),
                       ("AutoCAD 2004 / 2005 / 2006", "R2004"), ("AutoCAD 2000 / 2000i / 2002", "R2000")]:
            self.cmb_ver.addItem(lbl, v)
        self.cmb_pts = QComboBox()
        for lbl, v in [("POINT entities + text labels", "point+text"), ("POINT entities only", "point"),
                       ("Attributed block (number / elevation / description) + labels", "block")]:
            self.cmb_pts.addItem(lbl, v)
        self.chk_num = QCheckBox("Point number")
        self.chk_elev = QCheckBox("Elevation")
        self.chk_desc = QCheckBox("Description")
        for c in (self.chk_num, self.chk_elev, self.chk_desc):
            c.setChecked(True)
        lab = QHBoxLayout()
        for c in (self.chk_num, self.chk_elev, self.chk_desc):
            lab.addWidget(c)
        self.sp_dec = QSpinBox()
        self.sp_dec.setRange(0, 6)
        self.sp_dec.setValue(2)
        self.sp_th = dspin(float(pr.settings.get("text_height", 2.0)), 0.001, 1e6, 3, 0.5)
        self.sp_scale = dspin(float(pr.settings.get("plot_scale", 20.0)), 0.001, 1e6, 2, 10)
        self.sp_in = dspin(0.10, 0.01, 2.0, 3, 0.01)
        self.lbl_from = QLabel("")
        self.chk_contours = QCheckBox("Contours")
        self.chk_clabels = QCheckBox("Contour labels")
        self.chk_faces = QCheckBox("TIN triangles as 3DFACE (large)")
        self.chk_sel = QCheckBox("Only the selected objects")
        self.chk_vis = QCheckBox("Only visible layers")
        self.chk_zero = QCheckBox("Points without elevation get Z = 0 (otherwise they are placed at 0 anyway - DXF has no 'no value')")
        self.chk_contours.setChecked(True)
        self.chk_clabels.setChecked(True)
        self.chk_vis.setChecked(True)
        self.chk_zero.setChecked(True)
        self.chk_zero.setVisible(False)
        self.chk_sel.setEnabled(bool(state.sel_points or state.sel_entities))
        self.form.addRow("DXF version:", self.cmb_ver)
        self.form.addRow("Points as:", self.cmb_pts)
        self.form.addRow("Label with:", lab)
        self.form.addRow("Elevation decimals:", self.sp_dec)
        self.form.addRow("Text height (drawing units):", self.sp_th)
        row = QHBoxLayout()
        row.addWidget(QLabel("or size it for plotting - 1 inch ="))
        row.addWidget(self.sp_scale)
        row.addWidget(QLabel(f"{U.LABEL.get(pr.h_unit, pr.h_unit)}, text"))
        row.addWidget(self.sp_in)
        row.addWidget(QLabel("in"))
        w = QWidget()
        w.setLayout(row)
        self.form.addRow("", w)
        self.form.addRow("", self.lbl_from)
        self.form.addRow("Include:", self.chk_contours)
        self.form.addRow("", self.chk_clabels)
        self.form.addRow("", self.chk_faces)
        self.form.addRow("Limit to:", self.chk_sel)
        self.form.addRow("", self.chk_vis)
        self.sp_scale.valueChanged.connect(self._from_scale)
        self.sp_in.valueChanged.connect(self._from_scale)
        self._from_scale()

    def _from_scale(self):
        h = self.sp_scale.value() * self.sp_in.value()
        self.lbl_from.setText(f"-> text height {h:g} drawing units  (click to apply)")
        self._h_from_scale = h
        self.sp_th.setValue(h)

    def options(self) -> dxf_io.DxfOptions:
        pid, eid = _selected_ids(self.state) if self.chk_sel.isChecked() else (None, None)
        return dxf_io.DxfOptions(version=self.cmb_ver.currentData(), points_mode=self.cmb_pts.currentData(),
                                 label_number=self.chk_num.isChecked(), label_elev=self.chk_elev.isChecked(),
                                 label_desc=self.chk_desc.isChecked(), text_height=self.sp_th.value(),
                                 elev_decimals=self.sp_dec.value(), include_contours=self.chk_contours.isChecked(),
                                 include_contour_labels=self.chk_clabels.isChecked(),
                                 include_surface_faces=self.chk_faces.isChecked(),
                                 visible_layers_only=self.chk_vis.isChecked(), point_ids=pid, entity_ids=eid)


def export_dxf(window):
    st = window.state
    dlg = DxfExportDialog(st, window)
    if not dlg.exec():
        return
    start = str(Path(st.project.path).with_suffix(".dxf")) if st.project.path else f"{st.project.name}.dxf"
    path, _ = QFileDialog.getSaveFileName(window, "Export DXF", start, "DXF drawing (*.dxf)")
    if not path:
        return
    opts = dlg.options()
    stats = run_blocking(window, "Writing DXF ...", dxf_io.write_dxf, path, st.project, opts)
    st.log(f"DXF written: {path}  ({stats['points']:,} points, {stats['polylines'] + stats['polylines3d']:,} polylines, "
           f"{stats['texts']:,} texts, {stats['faces']:,} faces, {stats['layers']} layers)", "ok")


# ----------------------------------------------------------------------------- LandXML
class LandXmlExportDialog(FormDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent, "Export LandXML", "Writes points, TIN surfaces and linework (lines and arcs) as LandXML 1.2.", "Export", 460)
        self.state = state
        self.chk_pts = QCheckBox("Points (CgPoints)")
        self.chk_lines = QCheckBox("Linework (PlanFeatures)")
        self.chk_pts.setChecked(True)
        self.chk_lines.setChecked(True)
        self.lst = QListWidget()
        for s in state.project.surfaces.values():
            it = QListWidgetItem(f"{s.name}   ({s.tin().n_tris:,} triangles)")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, s.id)
            self.lst.addItem(it)
        self.lst.setMaximumHeight(110)
        self.form.addRow("Include:", self.chk_pts)
        self.form.addRow("", self.chk_lines)
        self.form.addRow("Surfaces:", self.lst)

    def surfaces(self):
        return [self.state.project.surfaces[self.lst.item(i).data(Qt.UserRole)] for i in range(self.lst.count())
                if self.lst.item(i).checkState() == Qt.Checked]


def export_landxml(window):
    st = window.state
    dlg = LandXmlExportDialog(st, window)
    if not dlg.exec():
        return
    start = str(Path(st.project.path).with_suffix(".xml")) if st.project.path else f"{st.project.name}.xml"
    path, _ = QFileDialog.getSaveFileName(window, "Export LandXML", start, "LandXML (*.xml)")
    if not path:
        return
    stats = run_blocking(window, "Writing LandXML ...", landxml.write_landxml, path, st.project, dlg.surfaces(),
                         dlg.chk_pts.isChecked(), dlg.chk_lines.isChecked())
    st.log(f"LandXML written: {path}  ({stats['points']:,} points, {stats['surfaces']} surfaces, {stats['polylines']} features)", "ok")


# ----------------------------------------------------------------------------- shared "output coordinate system" widget
class OutputCrsBox(QGroupBox):
    """Choose what coordinates a file should be written in."""

    def __init__(self, project, allow_geographic=True, geographic_default=False, parent=None):
        super().__init__("Coordinates in the file", parent)
        self.project = project
        pc = project.crs
        self._other = None
        lay = QVBoxLayout(self)
        self.r_proj = QRadioButton(f"The project's system - {pc.label}" + ("  (SAF removed)" if pc.ground.enabled else ""))
        self.r_other = QRadioButton("Another system:")
        row = QHBoxLayout()
        row.addWidget(self.r_other)
        self.lbl = QLabel("(none chosen)")
        self.btn = QPushButton("Choose...")
        row.addWidget(self.lbl, 1)
        row.addWidget(self.btn)
        lay.addWidget(self.r_proj)
        lay.addLayout(row)
        self.r_proj.setChecked(True)
        self.allow_geographic = allow_geographic
        self.btn.clicked.connect(self._choose)
        if pc.is_local:
            self.r_proj.setText("Local coordinates (no coordinate system - the file will not say where it is)")
            self.r_other.setEnabled(False)
            self.btn.setEnabled(False)
            self.lbl.setText("(needs a project coordinate system)")
        if geographic_default and not pc.is_local:
            self._set_other(C.CRS.from_epsg(4326))
            self.r_other.setChecked(True)

    def _set_other(self, crs):
        self._other = crs
        self.lbl.setText(f"{crs.name}")

    def _choose(self):
        d = CRSChooser(self, "Output coordinate system", allow_geographic=self.allow_geographic)
        if d.exec():
            self._set_other(C.resolve_crs(d.key()))
            self.r_other.setChecked(True)

    def spec(self):
        """-> (crs_for_file | None, xy_fn | None, h_unit_out)"""
        pc = self.project.crs
        if self.r_other.isChecked() and self._other is not None:
            fn = pc.transform_to(self._other)
            unit = "deg" if self._other.is_geographic else C.describe_crs(self._other)["unit"]
            return self._other, fn, unit
        if pc.is_local:
            return None, None, pc.unit
        fn = None
        if pc.ground.enabled:
            fn = pc.transform_to(pc.crs)               # removes the ground scaling, keeps the CRS
        return pc.crs, fn, pc.unit


# ----------------------------------------------------------------------------- GIS
class GisExportDialog(FormDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent, "Export GIS", "Points, lines and polygons with attributes for QGIS / ArcGIS.", "Export", 560)
        self.state = state
        self.cmb_fmt = QComboBox()
        for lbl, ext, drv in [("GeoPackage (.gpkg) - one file, three layers", ".gpkg", "GPKG"),
                              ("Shapefile (.shp) - separate points / lines / polygons files", ".shp", "ESRI Shapefile"),
                              ("GeoJSON (.geojson) - separate files", ".geojson", "GeoJSON")]:
            self.cmb_fmt.addItem(lbl, (ext, drv))
        self.crs_box = OutputCrsBox(state.project)
        self.chk_pts = QCheckBox("Points")
        self.chk_lines = QCheckBox("Lines")
        self.chk_polys = QCheckBox("Polygons (closed polylines)")
        for c in (self.chk_pts, self.chk_lines, self.chk_polys):
            c.setChecked(True)
        self.chk_sel = QCheckBox("Only the selected objects")
        self.chk_sel.setEnabled(bool(state.sel_points or state.sel_entities))
        self.cmb_z = QComboBox()
        for lbl, v in [("Same as the project", None), ("Metres", "m"), ("US survey feet", "ftUS"), ("International feet", "ft")]:
            self.cmb_z.addItem(lbl, v)
        self.form.addRow("Format:", self.cmb_fmt)
        self.root.insertWidget(1, self.crs_box)
        self.form.addRow("Include:", self.chk_pts)
        self.form.addRow("", self.chk_lines)
        self.form.addRow("", self.chk_polys)
        self.form.addRow("Limit to:", self.chk_sel)
        self.form.addRow("Elevation unit:", self.cmb_z)


def export_gis(window):
    st = window.state
    pr = st.project
    dlg = GisExportDialog(st, window)
    if not dlg.exec():
        return
    ext, drv = dlg.cmb_fmt.currentData()
    start = str(Path(pr.path).with_suffix(ext)) if pr.path else f"{pr.name}{ext}"
    path, _ = QFileDialog.getSaveFileName(window, "Export GIS", start, f"*{ext}")
    if not path:
        return
    crs, fn, _unit = dlg.crs_box.spec()
    zu = dlg.cmb_z.currentData()
    z_scale = (U.M_PER_UNIT[pr.v_unit] / U.M_PER_UNIT[zu]) if zu else 1.0
    pid, eid = _selected_ids(st) if dlg.chk_sel.isChecked() else (None, None)
    crs_arg = None
    if crs is not None:
        try:
            a = crs.to_authority()
            crs_arg = f"{a[0]}:{a[1]}" if a else crs.to_wkt()
        except Exception:
            crs_arg = crs.to_wkt()
    stats = run_blocking(window, "Writing GIS data ...", gis_io.write_gis, path, pr, drv, fn, crs_arg, pid, eid,
                         dlg.chk_pts.isChecked(), dlg.chk_lines.isChecked(), dlg.chk_polys.isChecked(), z_scale)
    st.log(f"GIS data written: {', '.join(stats['files'])}  ({stats['points']:,} points, {stats['lines']:,} lines, {stats['polygons']:,} polygons)"
           + ("" if crs is not None else "  - NOTE: no coordinate system was written (local project)"), "ok")


# ----------------------------------------------------------------------------- KML / KMZ
class KmlExportDialog(FormDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent, "Export for Google Earth (KML / KMZ)",
                         "Writes survey points and linework as WGS84 longitude / latitude, clamped to the ground. "
                         "Use it to eyeball your survey against Google Earth's imagery.", "Export", 560)
        self.state = state
        pr = state.project
        if pr.crs.is_local:
            self.root.insertWidget(1, Banner("This project uses local coordinates - assign a coordinate system first (Survey menu); "
                                             "otherwise there is no way to place it on the earth.", "bad"))
        self.chk_pts = QCheckBox("Survey points")
        self.chk_lab = QCheckBox("Show point numbers as labels")
        self.chk_lines = QCheckBox("Linework")
        self.chk_ctr = QCheckBox("Contours (can be large)")
        self.chk_sel = QCheckBox("Only the selected objects")
        for c in (self.chk_pts, self.chk_lab, self.chk_lines):
            c.setChecked(True)
        self.chk_sel.setEnabled(bool(state.sel_points or state.sel_entities))
        self.cmb_kmz = QComboBox()
        self.cmb_kmz.addItem("KMZ (compressed, recommended)", True)
        self.cmb_kmz.addItem("KML (plain text)", False)
        self.cmb_strat = QComboBox()
        self.cmb_strat.addItem("No datum shift (NAD83 / WGS84 treated as equal)", "none")
        self.cmb_strat.addItem("Automatic (PROJ best available)", "auto")
        self.cmb_strat.setCurrentIndex(max(0, self.cmb_strat.findData(pr.crs.strategy if pr.crs.strategy in ("none", "auto") else "none")))
        self.lbl_acc = Hint("")
        self.form.addRow("Format:", self.cmb_kmz)
        self.form.addRow("Include:", self.chk_pts)
        self.form.addRow("", self.chk_lab)
        self.form.addRow("", self.chk_lines)
        self.form.addRow("", self.chk_ctr)
        self.form.addRow("Limit to:", self.chk_sel)
        self.form.addRow("Datum handling:", self.cmb_strat)
        self.form.addRow("", self.lbl_acc)
        self.cmb_strat.currentIndexChanged.connect(self._acc)
        self._acc()

    def _acc(self):
        pr = self.state.project
        if pr.crs.is_local:
            self.lbl_acc.setText("")
            return
        try:
            ops = C.list_operations(pr.crs, 4326)
            best = ops[0]
            if self.cmb_strat.currentData() == "none":
                self.lbl_acc.setText("No shift is applied. For NAD83(2011) / NAD83(HARN) to WGS84 the real difference is typically 1-2 m "
                                     "in Texas - small compared with typical satellite-imagery accuracy, but not zero.")
            else:
                self.lbl_acc.setText(f"PROJ would use: {best.name} (stated accuracy {best.accuracy if best.accuracy is not None else 'unknown'} m"
                                     f"{'' if best.available else ', needs shift grids that are not installed'}).")
        except Exception:
            self.lbl_acc.setText("")


def export_kml(window):
    st = window.state
    pr = st.project
    if pr.crs.is_local:
        error_box(window, "Export KML", "This project uses local coordinates. Assign a coordinate system first "
                                        "(Survey > Project Coordinate System).")
        return
    dlg = KmlExportDialog(st, window)
    if not dlg.exec():
        return
    kmz = dlg.cmb_kmz.currentData()
    ext = ".kmz" if kmz else ".kml"
    start = str(Path(pr.path).with_suffix(ext)) if pr.path else f"{pr.name}{ext}"
    path, _ = QFileDialog.getSaveFileName(window, "Export Google Earth file", start, f"*{ext}")
    if not path:
        return
    strat = dlg.cmb_strat.currentData()
    pr.settings["kml_strategy"] = strat            # remembered so imported Google Earth pins use the same datum handling
    tf = pr.crs.transform_to(4326, strat)
    pid, eid = _selected_ids(st) if dlg.chk_sel.isChecked() else (None, None)
    note = f"Datum handling: {strat}."
    stats = run_blocking(window, "Writing KML ...", kml_io.write_kml, path, pr, lambda x, y: tf(x, y), pid, eid,
                         dlg.chk_pts.isChecked(), dlg.chk_lines.isChecked(), dlg.chk_ctr.isChecked(), dlg.chk_lab.isChecked(),
                         kmz, 2_000_000, note)
    st.log(f"Google Earth file written: {path}  ({stats['points']:,} points, {stats['lines']:,} lines)", "ok")


# ----------------------------------------------------------------------------- point CSV
class CsvExportDialog(FormDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent, "Export points", "Write a text file for a data collector, total station or spreadsheet.", "Export", 560)
        self.state = state
        self.cmb_fmt = QComboBox()
        fmts = [(k, v) for k, v in CSV.PRESETS.items() if "latitude" not in v and "longitude" not in v]
        fmts += [(k, v) for k, v in CSV.PRESETS.items() if "latitude" in v or "longitude" in v]
        for k, v in fmts:
            self.cmb_fmt.addItem(k, v)
        self.cmb_delim = QComboBox()
        for lbl, d in [("Comma", ","), ("Tab", "\t"), ("Space", " "), ("Semicolon", ";")]:
            self.cmb_delim.addItem(lbl, d)
        self.chk_head = QCheckBox("Write a header row")
        self.sp_dec = QSpinBox()
        self.sp_dec.setRange(0, 9)
        self.sp_dec.setValue(3)
        self.chk_sel = QCheckBox("Only the selected points")
        self.chk_sel.setEnabled(bool(state.sel_points))
        # ---- which points.  The reference lists are separate lists, so they are exported
        #      separately: a stake-out file wants the stake-out points, not the field book too.
        self.cmb_src = QComboBox()
        for word, value in [("This job's field data", None), ("Stake-out points", "stakeout"),
                            ("Control points", "control"), ("Other reference points", "other"),
                            ("Everything, reference included", "all")]:
            self.cmb_src.addItem(f"{word}  ({len(REF.select(state.project, value)):,})", value)
        self.crs_box = OutputCrsBox(state.project)
        self.cmb_z = QComboBox()
        for lbl, v in [("Same as the project", None), ("Metres", "m"), ("US survey feet", "ftUS"), ("International feet", "ft")]:
            self.cmb_z.addItem(lbl, v)
        self.form.addRow("Points:", self.cmb_src)
        self.form.addRow("Columns:", self.cmb_fmt)
        self.form.addRow("Separator:", self.cmb_delim)
        self.form.addRow("", self.chk_head)
        self.form.addRow("Decimals:", self.sp_dec)
        self.form.addRow("Elevation unit:", self.cmb_z)
        self.form.addRow("", self.chk_sel)
        self.root.insertWidget(1, self.crs_box)
        self.cmb_fmt.currentIndexChanged.connect(self._fmt_changed)

    def source(self) -> str | None:
        """None = the fieldwork list, a role name = that reference list, "all" = everything."""
        return self.cmb_src.currentData()

    def source_word(self) -> str:
        """How to say which points these are, in the log line."""
        src = self.source()
        if src == "all":
            return "points"
        if src:
            return f"{REF.ROLE_LABELS[src].lower()} reference points"
        return "field points"

    def _fmt_changed(self):
        roles = self.cmb_fmt.currentData()
        if "latitude" in roles and not self.state.project.crs.is_local and not self.crs_box.r_other.isChecked():
            self.crs_box._set_other(C.CRS.from_epsg(4326))
            self.crs_box.r_other.setChecked(True)
        if "latitude" in roles:
            self.sp_dec.setValue(8)


def export_points_csv(window):
    st = window.state
    pr = st.project
    dlg = CsvExportDialog(st, window)
    if not dlg.exec():
        return
    start = str(Path(pr.path).with_suffix(".csv")) if pr.path else f"{pr.name}_points.csv"
    path, _ = QFileDialog.getSaveFileName(window, "Export points", start, "Text / CSV (*.csv *.txt)")
    if not path:
        return
    cols = tuple(dlg.cmb_fmt.currentData())
    geo = "latitude" in cols
    crs, fn, _ = dlg.crs_box.spec()
    if geo and (crs is None or not crs.is_geographic):
        if pr.crs.is_local:
            error_box(window, "Export points", "Latitude / longitude output needs a project coordinate system.")
            return
        fn = pr.crs.transform_to(4326)
    zu = dlg.cmb_z.currentData()
    z_scale = (U.M_PER_UNIT[pr.v_unit] / U.M_PER_UNIT[zu]) if zu else 1.0
    only = set(st.sel_points) if dlg.chk_sel.isChecked() else None
    pts = REF.select(pr, dlg.source(), only)
    n = CSV.write_points_csv(path, pts, cols, dlg.cmb_delim.currentData(), dlg.chk_head.isChecked(), dlg.sp_dec.value(),
                             (lambda x, y: fn(x, y)) if fn else None, z_scale)
    st.log(f"Wrote {n:,} {dlg.source_word()} to {path}", "ok")
