"""Import pipeline: options panel, per-format dialogs and the dispatcher used by the main window."""
from __future__ import annotations

import filecmp
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QRadioButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import audit as AUD
from ..core import crs as C
from ..core import filecrs as FCRC
from ..core import field_data as FD
from ..core import provenance as PROV
from ..core import reference as REF
from ..core import units as U
from ..core.model import ImageryLayer, ImportBatch
from ..core.settings import settings
from ..fieldwork.utils_sort import natural_key
from ..io import csv_points as CSV
from . import theme
from .crs_dialog import CRSPicker
from .crs_extra import VerticalAndGroundPanel
from .widgets import Banner, FormDialog, Hint, error_box, info_box, run_blocking, fmt_coord

CSV_EXT = {".csv", ".txt", ".pnt", ".pts", ".asc", ".dat", ".xyz_"}
#: the extensions a folder-wide reference import will read, and the filter for the single-file one
POINTS_EXT = (".csv", ".txt", ".pnt", ".pts", ".asc", ".dat", ".xyz", ".pnl", ".fwk")
POINTS_FILTER = ("Point files (*.csv *.txt *.pnt *.pts *.asc *.dat *.xyz);;All files (*)")
LANDXML_EXT = {".xml", ".landxml"}
GIS_EXT = {".shp", ".gpkg", ".geojson", ".json"}
KML_EXT = {".kml", ".kmz"}
IMAGE_EXT = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}

IMPORT_FILTER = ("All supported (*.csv *.txt *.pnt *.pts *.asc *.dat *.xml *.landxml *.dxf *.shp *.gpkg *.geojson *.json "
                 "*.kml *.kmz *.plb);;Point files (*.csv *.txt *.pnt *.pts *.asc *.dat);;LandXML (*.xml *.landxml);;DXF (*.dxf);;"
                 "GIS (*.shp *.gpkg *.geojson *.json);;Google Earth (*.kml *.kmz);;Plumbline project (*.plb);;All files (*)")


# ----------------------------------------------------------------------------- plan
@dataclass
class ImportPlan:
    src_crs: object | None = None          # pyproj CRS; None => numbers already in project coordinates
    geographic: bool = False               # batch holds lon / lat
    assign_crs: C.ProjectCRS | None = None  # give a local project this CRS first
    swap_xy: bool = False
    scale_xy: float = 1.0
    z_scale: float = 1.0
    dup_policy: str = "renumber"
    layer_override: str | None = None
    process_linework: bool = False
    reference_role: str | None = None      # stakeout | control | other -> a reference list, not field data


def file_crs_from_info(info: dict):
    try:
        if info.get("epsg"):
            return C.CRS.from_epsg(int(info["epsg"]))
        if info.get("crs"):
            return C.CRS.from_user_input(info["crs"])
        if info.get("wkt"):
            return C.CRS.from_wkt(info["wkt"])
    except Exception:
        return None
    return None


def make_batch_transform(project, plan: ImportPlan):
    """-> fn(x, y) -> (x', y') that takes the batch's coordinates into project coordinates."""
    pc = plan.assign_crs or project.crs
    if plan.geographic:
        src = plan.src_crs if plan.src_crs is not None else 4326
        return lambda x, y: pc.from_lonlat(x, y, source=src)
    if plan.src_crs is not None and not pc.is_local:
        return pc.transform_from(plan.src_crs)
    k = plan.scale_xy
    return lambda x, y: (np.asarray(x, float) * k, np.asarray(y, float) * k)


def apply_import(state, batch: ImportBatch, plan: ImportPlan, label: str, path=None,
                 job_root=None) -> dict:
    """Apply an imported batch to the project, and record where each point came from.

    Every door into this program ends here, so this is where the provenance record is written -
    the file, the folder, the import's name and the time (:mod:`plumbline.core.provenance`).
    The point list's source columns, the Properties panel and the Check Fieldwork dock all read
    that one record, and a batch that arrives without a file name simply has an empty one.
    """
    pr = state.project
    src = Path(str(path or batch.info.get("path") or ""))
    root = job_root if job_root is not None else (Path(pr.path).parent if getattr(pr, "path", None) else None)
    folder = str(src.parent) if (src.name and len(src.parts) > 1) else ""
    if plan.reference_role:
        name = f"{REF.label_for(plan.reference_role)} points"
    else:
        name = PROV.set_label(label)
    if batch.points:
        PROV.stamp(batch.points, file=src.name, folder=PROV.short_folder(folder, root), set=name)
    with state.edit(f"Import {label}"):
        if plan.assign_crs is not None:
            pr.assign_crs(plan.assign_crs)
        if plan.reference_role:
            REF.ensure_layers(pr)
            REF.mark_batch(batch, plan.reference_role)
        if plan.swap_xy:
            batch.transform_xy(lambda x, y: (np.asarray(y, float), np.asarray(x, float)))
        fn = make_batch_transform(pr, plan)
        batch.transform_xy(fn, plan.z_scale)
        stats = pr.apply_batch(batch, plan.dup_policy, plan.layer_override,
                               reference_role=plan.reference_role)
        # ... and what it landed as, which is the state the Point(s) Audit reads against (item 17).
        # The ids exist by now - including the id of a point an overwrite landed *on*, which is
        # the whole reason apply_batch hands them back - and it goes inside the edit, so undoing
        # the import takes the baseline rows with it.
        if not plan.reference_role:
            AUD.record(pr, [pr.points[i] for i in stats.get("point_ids", ()) if i in pr.points])
        # What coordinate system these numbers were in, written down against the file (item 6).
        # This is inside the edit, so undoing the import takes the record with it, and it is what
        # "change the CRS of a file afterwards" reads and rewrites.
        if src.name:
            converted_on_import = bool(plan.src_crs is not None or plan.geographic or
                                       plan.scale_xy != 1.0 or plan.z_scale != 1.0)
            rec = FCRC.make_from_project(pr, method="imported" if converted_on_import else "project")
            # Keep the file's declared/chosen source as provenance, but the row's primary CRS
            # describes the coordinates now stored in the project after apply_import's transform.
            source_rec = None
            if plan.src_crs is not None:
                try:
                    from ..core.crs import ProjectCRS as _PC
                    source_rec = FCRC.record_from_crs(_PC(plan.src_crs), method="chosen")
                except Exception:
                    source_rec = None
            elif plan.geographic:
                source_rec = FCRC.make(key="EPSG:4326", label="longitude / latitude (WGS 84)",
                                       unit="degree", vunit=pr.crs.vunit, vertical="HAE", method="chosen")
            if source_rec is not None:
                rec["source_crs"] = source_rec
            FCRC.record(pr, src.name, rec)
        if plan.process_linework:
            stats["linework"] = pr.process_linework()["strings"]
    return stats


# ----------------------------------------------------------------------------- small CRS chooser
class CRSChooser(QDialog):
    def __init__(self, parent=None, title="Choose Coordinate System", allow_geographic=False, suggestions=()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        self.picker = CRSPicker(kinds=("projected", "geographic") if allow_geographic else ("projected",))
        self.picker.set_suggestions(suggestions)
        lay.addWidget(self.picker, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self):
        if not self.picker.current_key():
            error_box(self, "Coordinate System", "Pick one from the list.")
            return
        self.accept()

    def key(self) -> str:
        return self.picker.current_key()


# ----------------------------------------------------------------------------- options panel
UNIT_CHOICES = [("Same as the project", None), ("US survey feet", "ftUS"), ("International feet", "ft"), ("Metres", "m")]


class ImportOptionsPanel(QWidget):
    """Where the numbers live (CRS / units), plausibility check, duplicates, layers."""

    def __init__(self, state, geographic: bool = False, file_crs=None, file_unit: str | None = None,
                 default_geo_crs: int = 4326, assume_project_crs: bool = False, parent=None):
        super().__init__(parent)
        self.state = state
        self.project = state.project
        self.geographic = geographic
        self.file_crs = file_crs
        self.file_unit = file_unit
        #: A CSV cannot say where it is, so the importer assumes the project's own system and only
        #: reprojects when the user says the file is in a different one (change order, item 6).
        #: Formats that carry their own CRS pass False, and the file's own answer is the default.
        self.assume_project_crs = bool(assume_project_crs)
        self.batch: ImportBatch | None = None
        self._chosen = None                  # CRS the user picked via "Choose..."
        self._assign = None                  # ProjectCRS to assign (local projects)
        pc = self.project.crs
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        g = QGroupBox("Where Are These Coordinates?")
        gl = QVBoxLayout(g)
        self.r_proj = QRadioButton()
        self.r_other = QRadioButton("A different coordinate system:")
        self.r_assign = QRadioButton()
        row = QHBoxLayout()
        row.addWidget(self.r_other)
        self.lbl_other = QLabel("(none chosen)")
        self.btn_other = QPushButton("Choose...")
        row.addWidget(self.lbl_other, 1)
        row.addWidget(self.btn_other)
        if geographic:
            self.r_proj.setVisible(False)
            self.r_other.setText("Longitude / latitude in:")
        if pc.is_local and not geographic:
            self.r_proj.setText("Local coordinates - keep this project local (use the numbers as they are)")
            self.r_assign.setText("")
        else:
            self.r_proj.setText(f"The project's system  ({pc.label})")
        gl.addWidget(self.r_proj)
        self.r_assign_row = QHBoxLayout()
        self.r_assign_row.addWidget(self.r_assign)
        self.lbl_assign = QLabel("")
        self.lbl_assign.setWordWrap(True)
        self.r_assign_row.addWidget(self.lbl_assign, 1)
        self.btn_assign = QPushButton("Pick...")
        self.r_assign_row.addWidget(self.btn_assign)
        self.r_assign.setVisible(pc.is_local and not geographic)
        self.lbl_assign.setVisible(pc.is_local and not geographic)
        self.btn_assign.setVisible(pc.is_local and not geographic)
        gl.addLayout(self.r_assign_row)
        gl.addLayout(row)
        form = QFormLayout()
        self.cmb_units = QComboBox()
        for lbl, v in UNIT_CHOICES:
            self.cmb_units.addItem(lbl, v)
        self.cmb_zunits = QComboBox()
        for lbl, v in [("Same as horizontal", "same"), ("Metres", "m"), ("US survey feet", "ftUS"), ("International feet", "ft"),
                       ("Same as the project's elevation unit", None)]:
            self.cmb_zunits.addItem(lbl, v)
        self.chk_swap = QCheckBox("Swap the two coordinate columns")
        self.chk_swap.setVisible(False)
        form.addRow("Horizontal units in file:", self.cmb_units)
        form.addRow("Elevation units in file:", self.cmb_zunits)
        gl.addLayout(form)
        gl.addWidget(Hint("A CSV is assumed to be in the project's own system - pick \"a different "
                          "coordinate system\" above only when it is not."))
        lay.addWidget(g)

        vg = QGroupBox("Heights and Ground Scale")
        vgl = QVBoxLayout(vg)
        self.vg = VerticalAndGroundPanel(vg)
        self.vg.set_from(pc)
        self.vg.setEnabled(False)
        vgl.addWidget(self.vg)
        lay.addWidget(vg)

        self.fit = Banner("", "info")
        self.fit.setVisible(False)
        lay.addWidget(self.fit)

        o = QGroupBox("Options")
        of = QFormLayout(o)
        self.cmb_dup = QComboBox()
        for lbl, v in [("Renumber the new ones (keep both)", "renumber"), ("Skip the new ones", "skip"),
                       ("Overwrite the existing ones", "overwrite"), ("Keep both with the same number", "keep")]:
            self.cmb_dup.addItem(lbl, v)
        self.ed_layer = QLineEdit()
        self.ed_layer.setPlaceholderText("(blank = keep layers from the file / feature codes)")
        self.chk_lw = QCheckBox("Build linework from feature codes after import")
        of.addRow("If a point number already exists:", self.cmb_dup)
        of.addRow("Put everything on layer:", self.ed_layer)
        of.addRow("", self.chk_lw)
        lay.addWidget(o)

        self.btn_other.clicked.connect(self._choose_other)
        self.btn_assign.clicked.connect(self._choose_assign)
        for w in (self.r_proj, self.r_other, self.r_assign):
            w.toggled.connect(self._refresh)
        self.cmb_units.currentIndexChanged.connect(self._refresh)
        self.cmb_zunits.currentIndexChanged.connect(self._refresh)
        self.chk_swap.toggled.connect(self._refresh)
        self._init_defaults(default_geo_crs)

    # -- defaults
    def _init_defaults(self, geo_default: int):
        pc = self.project.crs
        if self.geographic:
            self._chosen = C.CRS.from_epsg(geo_default)
            self.lbl_other.setText(self._chosen.name)
            self.r_other.setChecked(True)
            self.cmb_zunits.setCurrentIndex(self.cmb_zunits.findData("m"))
            return
        if self.file_unit:
            i = self.cmb_units.findData(self.file_unit)
            if i >= 0 and self.file_unit != pc.unit:
                self.cmb_units.setCurrentIndex(i)
        if pc.is_local:
            if self.file_crs is not None and not self.file_crs.is_geographic:
                try:
                    self._assign = C.ProjectCRS(self.file_crs)
                    self.lbl_assign.setText(f"Assign the file's system to the project: {self._assign.label}")
                    self.r_assign.setChecked(True)
                except Exception:
                    self.r_proj.setChecked(True)
            else:
                self.lbl_assign.setText("Assign a coordinate system to this project (the numbers are already in it)")
                self.r_proj.setChecked(True)
        elif self.assume_project_crs:
            # The numbers are taken to be in the project's system - no conversion, nothing moves.
            # If the file itself says otherwise, that is said here and can be overridden just below.
            self.r_proj.setChecked(True)
            if self.file_crs is not None and self.file_crs != pc.crs:
                self.lbl_other.setText(f"{self.file_crs.name}   (what the file claims - only used if you pick it)")
        else:
            if self.file_crs is not None and self.file_crs != pc.crs:
                self._chosen = self.file_crs
                self.lbl_other.setText(f"{self.file_crs.name}   (from the file)")
                self.r_other.setChecked(True)
            else:
                self.r_proj.setChecked(True)

    def _choose_other(self):
        dlg = CRSChooser(self, "Coordinate system of the file", allow_geographic=True)
        if dlg.exec():
            k = dlg.key()
            self._chosen = C.resolve_crs(k)
            self.lbl_other.setText(f"{k}   {self._chosen.name}")
            self.r_other.setChecked(True)
            self._refresh()

    def _choose_assign(self):
        sug = []
        if self.batch is not None and not self.batch.is_empty():
            xy = self.batch.sample_xy(50)
            if len(xy):
                favs = [k for k, _ in C.default_favorites_records()]
                sug = [(k, n) for k, n, _, _ in C.suggest_crs(float(np.median(xy[:, 0])), float(np.median(xy[:, 1])), favs)]
        dlg = CRSChooser(self, "Coordinate system to assign to the project", suggestions=sug)
        if dlg.exec():
            k = dlg.key()
            self._assign = C.ProjectCRS.from_epsg(int(k.split(":")[1])) if k.startswith("EPSG:") else C.ProjectCRS(k)
            self.lbl_assign.setText(f"Assign to the project: {self._assign.label}")
            self.r_assign.setChecked(True)
            self._refresh()

    # -- batch & plausibility
    def set_batch(self, batch: ImportBatch):
        self.batch = batch
        self._refresh()

    def plan(self) -> ImportPlan:
        pc = self.project.crs
        # Vertical datum and ground scale are properties of the project, not of the file: applying
        # them here is what makes them "part of import" (change order, item 7).
        self.vg.apply_to(pc)
        p = ImportPlan(geographic=self.geographic, dup_policy=self.cmb_dup.currentData(),
                       layer_override=self.ed_layer.text().strip() or None, process_linework=self.chk_lw.isChecked(),
                       swap_xy=self.chk_swap.isChecked() and not self.geographic)
        eff = self._assign if (self.r_assign.isChecked() and self._assign is not None) else pc
        if self.r_other.isChecked() and self._chosen is not None:
            p.src_crs = self._chosen
        if self.r_assign.isChecked() and self._assign is not None:
            p.assign_crs = self._assign
        # horizontal scale when there is no CRS conversion doing it for us
        hu = self.cmb_units.currentData()
        if hu and p.src_crs is None and not self.geographic:
            p.scale_xy = U.M_PER_UNIT[hu] / U.M_PER_UNIT[eff.unit]
        # elevation scale
        zu = self.cmb_zunits.currentData()
        if zu == "same":
            src_u = hu or (C.describe_crs(p.src_crs)["unit"] if (p.src_crs is not None and not p.src_crs.is_geographic) else eff.unit)
        elif zu is None:
            src_u = eff.vunit
        else:
            src_u = zu
        p.z_scale = U.M_PER_UNIT.get(src_u, 1.0) / U.M_PER_UNIT.get(eff.vunit, 1.0)
        return p

    def _refresh(self):
        pc = self.project.crs
        self.btn_other.setEnabled(True)
        if self.r_proj.isChecked():
            self.vg.set_from(pc)
            self.vg.setEnabled(False)
        else:
            self.vg.setEnabled(True)
        if self.batch is None or self.batch.is_empty():
            return
        try:
            plan = self.plan()
            xy = self.batch.sample_xy(300)
            if plan.swap_xy:
                xy = xy[:, ::-1]
            fn = make_batch_transform(self.project, plan)
            x, y = fn(xy[:, 0], xy[:, 1])
            eff = plan.assign_crs or pc
            ok, tot = eff.area_of_use_ok(np.asarray(x), np.asarray(y))
            if eff.is_local:
                self.fit.setVisible(False)
                return
            self.fit.setVisible(True)
            if ok == tot:
                self.fit.set(f"Looks right: all {tot} sampled coordinates fall inside the area of use of {eff.authority}.", "info")
            elif ok == 0:
                self.fit.set(f"Probably wrong: none of {tot} sampled coordinates land inside the area of use of {eff.authority} after "
                             f"conversion. Check the coordinate system, the units, or swap northing/easting.", "bad")
            else:
                self.fit.set(f"Only {ok} of {tot} sampled coordinates land inside the area of use of {eff.authority}.", "warn")
        except C.LocalCRSError:
            self.fit.setVisible(True)
            self.fit.set("This project is local - assign a coordinate system to convert from lat/lon or another system.", "warn")
        except Exception as ex:
            self.fit.setVisible(True)
            self.fit.set(f"Could not test the coordinates: {ex}", "warn")

    def validate(self) -> str | None:
        if self.r_other.isChecked() and self._chosen is None:
            return "Choose the coordinate system the file uses."
        if self.r_assign.isChecked() and self._assign is None:
            return "Pick the coordinate system to assign to the project."
        if self.project.crs.is_local and self.r_other.isChecked() and not self.geographic:
            return ("This project uses local coordinates, so a file in another system cannot be converted. "
                    "Either assign a coordinate system to the project, or import the numbers as local coordinates.")
        if self.geographic and self.project.crs.is_local:
            return "Longitude/latitude data needs a project coordinate system. Assign one first (Survey menu)."
        return self.vg.validate()
    


class _ImportDialog(QDialog):
    """Dialog shell: header text + panel + OK/Cancel (used by every format except CSV)."""

    def __init__(self, parent, title, summary, panel: ImportOptionsPanel, messages=()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(Banner(summary, "info"))
        for m in list(messages)[:4]:
            lay.addWidget(Banner(m, "warn"))
        self.panel = panel
        lay.addWidget(panel)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Import")
        self.bb.accepted.connect(self._ok)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

    def _ok(self):
        err = self.panel.validate()
        if err:
            error_box(self, "Import", err)
            return
        self.accept()


# ----------------------------------------------------------------------------- CSV dialog
class CsvImportDialog(QDialog):
    MAXPREV = 40

    def __init__(self, state, path, parent=None):
        super().__init__(parent)
        self.state = state
        self.path = Path(path)
        self.setWindowTitle(f"Import Points — {self.path.name}")
        self.setMinimumSize(960, 700)
        self.sniff = CSV.sniff(path)
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Delimiter:"))
        self.cmb_delim = QComboBox()
        for lbl, d in [("Comma", ","), ("Tab", "\t"), ("Semicolon", ";"), ("Space", " "), ("Pipe", "|")]:
            self.cmb_delim.addItem(lbl, d)
        self.cmb_delim.setCurrentIndex(max(0, self.cmb_delim.findData(self.sniff.delimiter)))
        top.addWidget(self.cmb_delim)
        top.addWidget(QLabel("Skip first"))
        self.sp_skip = QSpinBox()
        self.sp_skip.setRange(0, 1000)
        self.sp_skip.setValue(1 if self.sniff.has_header else 0)
        top.addWidget(self.sp_skip)
        top.addWidget(QLabel("line(s) (headers)"))
        top.addSpacing(16)
        top.addWidget(QLabel("Format:"))
        self.cmb_fmt = QComboBox()
        self.cmb_fmt.addItem("Custom - set the column roles below", None)
        for k, v in CSV.PRESETS.items():
            self.cmb_fmt.addItem(k, v)
        top.addWidget(self.cmb_fmt, 1)
        root.addLayout(top)
        # ---- what these points are.  A CSV is usually not our own field data: it is a stake-out
        #      list, published control, or something another crew or agency handed over.  That
        #      goes on its own reference layer and keeps its own point numbering.
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("These points are:"))
        self.cmb_role = QComboBox()
        # Field data first and selected by default: a file of shot points is this job's field data
        # unless the user says otherwise (change order, item 6).  A stake-out list is the exception,
        # and choosing it is one click.
        for label, role in [("This job's field data - joins the fieldwork list", None),
                            ("Stake-out - reference layer STAKE-OUT", "stakeout"),
                            ("Control - reference layer CONTROL", "control"),
                            ("Other reference - reference layer OTHER", "other")]:
            self.cmb_role.addItem(label, role)
        self.cmb_role.setCurrentIndex(0)
        self.cmb_role.setToolTip("Reference points draw, snap and export with everything else, but they are kept\n"
                                 "out of the fieldwork list and out of the data-quality checks - they are not our shots.\n"
                                 "Each role has its own layer and its own point numbers.")
        row2.addWidget(self.cmb_role, 1)
        root.addLayout(row2)
        self.tbl = QTableWidget()
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        root.addWidget(self.tbl, 1)
        self.status = Banner("", "info")
        root.addWidget(self.status)
        self.panel = ImportOptionsPanel(state, geographic=False, assume_project_crs=True)
        root.addWidget(self.panel)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Import points")
        self.bb.accepted.connect(self._ok)
        self.bb.rejected.connect(self.reject)
        root.addWidget(self.bb)
        self.roles_cb: list[QComboBox] = []
        self._building = False
        self.cmb_delim.currentIndexChanged.connect(self._delimiter_changed)
        self.sp_skip.valueChanged.connect(lambda: self._rebuild(keep_roles=True))
        self.cmb_fmt.currentIndexChanged.connect(self._preset)
        self.cmb_role.currentIndexChanged.connect(self._role_target_changed)
        self._rebuild(keep_roles=False)
        self._role_target_changed()

    # -- table
    def _load_rows(self):
        text, _ = CSV.read_text(self.path)
        lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith(("#", "//"))]
        d = self.cmb_delim.currentData()
        rows = [[c.strip() for c in CSV._split(l, d)] for l in lines[:self.MAXPREV + self.sp_skip.value()]]
        return rows

    def _delimiter_changed(self):
        self._rebuild(keep_roles=False)

    def _rebuild(self, keep_roles: bool):
        self._building = True
        old_roles = [cb.currentData() for cb in self.roles_cb] if keep_roles else None
        rows = self._load_rows()
        skip = self.sp_skip.value()
        data = rows[skip:]
        header = rows[skip - 1] if skip >= 1 and skip - 1 < len(rows) else None
        n = max((len(r) for r in data), default=0)
        n = max(n, len(old_roles or []))
        if old_roles is None:
            roles = CSV.guess_roles(header if (skip and header) else None, data)
            roles += ["ignore"] * (n - len(roles))
        else:
            roles = (old_roles + ["ignore"] * n)[:n]
        self.tbl.clear()
        self.tbl.setRowCount(len(data) + 1)
        self.tbl.setColumnCount(n)
        self.roles_cb = []
        for c in range(n):
            cb = QComboBox()
            for r in CSV.ROLES:
                cb.addItem(CSV.ROLE_LABELS[r], r)
            cb.setCurrentIndex(max(0, cb.findData(roles[c] if c < len(roles) else "ignore")))
            cb.currentIndexChanged.connect(lambda _i, col=c: self._role_changed(col))
            self.tbl.setCellWidget(0, c, cb)
            self.roles_cb.append(cb)
        for r, row in enumerate(data, start=1):
            for c in range(n):
                self.tbl.setItem(r, c, QTableWidgetItem(row[c] if c < len(row) else ""))
        self.tbl.setRowHeight(0, 30)
        self.tbl.resizeColumnsToContents()
        for c in range(n):
            self.tbl.setColumnWidth(c, max(self.tbl.columnWidth(c), 130))
        self._building = False
        self._geo_changed()
        self._update_preview()

    def _role_changed(self, col):
        if self._building:
            return
        role = self.roles_cb[col].currentData()
        if role not in ("ignore", "attr"):
            self._building = True
            for c, cb in enumerate(self.roles_cb):
                if c != col and cb.currentData() == role:
                    cb.setCurrentIndex(cb.findData("ignore"))
            self._building = False
        self.cmb_fmt.blockSignals(True)
        self.cmb_fmt.setCurrentIndex(0)
        self.cmb_fmt.blockSignals(False)
        self._geo_changed()
        self._update_preview()

    def _preset(self):
        roles = self.cmb_fmt.currentData()
        if not roles:
            return
        self._building = True
        for c, cb in enumerate(self.roles_cb):
            cb.setCurrentIndex(cb.findData(roles[c] if c < len(roles) else "ignore"))
        self._building = False
        self._geo_changed()
        self._update_preview()

    def roles(self):
        return [cb.currentData() for cb in self.roles_cb]

    # -- where the points end up
    def target_role(self) -> str | None:
        """The reference role chosen, or None when the points are this job's field data."""
        return self.cmb_role.currentData()

    def _role_target_changed(self):
        """One layer per role: a role fixes the layer, so the layer box steps aside."""
        role = self.target_role()
        self.panel.ed_layer.setEnabled(role is None)
        self.panel.ed_layer.setPlaceholderText("(the layer of the chosen role)" if role
                                              else "(blank = keep layers from the file / feature codes)")
        self.panel.chk_lw.setEnabled(role is None)
        if role:
            self.panel.ed_layer.clear()
            self.panel.chk_lw.setChecked(False)
        self._update_preview()

    def _geo_changed(self):
        """Swap the options panel when the mapping says lat/lon."""
        geo = "latitude" in self.roles() and "longitude" in self.roles()
        if geo != self.panel.geographic:
            lay = self.layout()
            i = lay.indexOf(self.panel)
            self.panel.setParent(None)
            self.panel.deleteLater()
            self.panel = ImportOptionsPanel(self.state, geographic=geo,
                                            assume_project_crs=not geo)
            lay.insertWidget(i, self.panel)

    def _mapping(self) -> CSV.CsvMapping:
        return CSV.CsvMapping(self.cmb_delim.currentData(), self.sp_skip.value(), self.roles())

    def _update_preview(self):
        if self._building:
            return
        try:
            b = CSV.read_points(self.path, self._mapping(), limit=600)
        except ValueError as ex:
            self.status.set(str(ex), "warn")
            return
        extra = f" ({b.info.get('skipped', 0)} unreadable line(s) in the preview)" if b.info.get("skipped") else ""
        role = self.target_role()
        if role:
            dest = (f"  They become {REF.ROLE_LABELS[role].lower()} reference points on layer "
                    f"{REF.layer_for(role)}, with their own point numbers - not this job's field data.")
        else:
            dest = "  They join this job's field data (the fieldwork list)."
        self.status.set(f"Preview: {len(b.points)} point(s) read from the first lines{extra}.{dest}",
                        "info" if b.points else "warn")
        self.panel.set_batch(b)

    def _ok(self):
        try:
            self.batch = CSV.read_points(self.path, self._mapping())
        except ValueError as ex:
            error_box(self, "Import Points", str(ex))
            return
        if not self.batch.points:
            error_box(self, "Import Points", "No readable points - check the delimiter, header lines and column roles.")
            return
        err = self.panel.validate()
        if err:
            error_box(self, "Import Points", err)
            return
        self.plan = self.panel.plan()
        self.plan.reference_role = self.target_role()
        if self.plan.reference_role:
            self.plan.layer_override = None        # the role's own layer is the layer
        self.accept()


# ----------------------------------------------------------------------------- GIS layer dialog
class GisLayerDialog(QDialog):
    def __init__(self, state, path, parent=None):
        super().__init__(parent)
        from pyogrio import read_info
        from ..io import gis_io
        self.path = str(path)
        self.state = state
        self.setWindowTitle(f"Import GIS Data — {Path(path).name}")
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        layers = gis_io.list_gis_layers(path)
        form = QFormLayout()
        self.cmb_layer = QComboBox()
        for n, g in layers:
            self.cmb_layer.addItem(f"{n}   [{g}]", n)
        form.addRow("Layer:", self.cmb_layer)
        self.chk_all = QCheckBox(f"Import all {len(layers)} layers of this file (point number / description / elevation fields are guessed)")
        self.chk_all.setVisible(len(layers) > 1)
        self.chk_all.setChecked(len(layers) > 1)
        self.layer_names = [n for n, _ in layers]
        form.addRow("", self.chk_all)
        self.cmb_num = QComboBox()
        self.cmb_desc = QComboBox()
        self.cmb_z = QComboBox()
        form.addRow("Point number field:", self.cmb_num)
        form.addRow("Description / code field:", self.cmb_desc)
        form.addRow("Elevation field:", self.cmb_z)
        lay.addLayout(form)
        self.info_lbl = Banner("", "info")
        lay.addWidget(self.info_lbl)
        self.panel_holder = QVBoxLayout()
        lay.addLayout(self.panel_holder)
        self.panel: ImportOptionsPanel | None = None
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Import")
        self.bb.accepted.connect(self._ok)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)
        self._read_info = read_info
        self._gis = gis_io
        self.cmb_layer.currentIndexChanged.connect(self._layer_changed)
        self.chk_all.toggled.connect(self._all_toggled)
        self._layer_changed()
        self._all_toggled(self.chk_all.isChecked())

    def _all_toggled(self, on):
        for w in (self.cmb_layer, self.cmb_num, self.cmb_desc, self.cmb_z):
            w.setEnabled(not on)

    def _layer_changed(self):
        name = self.cmb_layer.currentData()
        info = self._read_info(self.path, layer=name)
        fields = [str(f) for f in info["fields"]]
        guess = self._gis.guess_fields(fields)
        for cmb, role in ((self.cmb_num, "number"), (self.cmb_desc, "description"), (self.cmb_z, "elevation")):
            cmb.clear()
            cmb.addItem("(none)", None)
            for f in fields:
                cmb.addItem(f, f)
            cmb.setCurrentIndex(max(0, cmb.findData(guess.get(role))))
        crs = None
        try:
            crs = C.CRS.from_user_input(info["crs"]) if info.get("crs") else None
        except Exception:
            crs = None
        geographic = bool(crs is not None and crs.is_geographic)
        self.info_lbl.set(f"{info.get('features', '?'):,} feature(s), {info.get('geometry_type', '')}.  "
                          f"File coordinate system: {crs.name if crs else 'NOT STATED - you must say which one it is'}", "info" if crs else "warn")
        if self.panel is not None:
            self.panel.setParent(None)
            self.panel.deleteLater()
        unit = C.describe_crs(crs)["unit"] if crs is not None and not geographic else None
        self.panel = ImportOptionsPanel(self.state, geographic=geographic, file_crs=crs, file_unit=unit)
        self.panel_holder.addWidget(self.panel)

    def _ok(self):
        err = self.panel.validate()
        if err:
            error_box(self, "Import", err)
            return
        self.all_layers = self.chk_all.isChecked() and self.chk_all.isVisible()
        self.layer = self.cmb_layer.currentData()
        self.fields = {k: v for k, v in (("number", self.cmb_num.currentData()), ("description", self.cmb_desc.currentData()),
                                         ("elevation", self.cmb_z.currentData())) if v}
        self.plan = self.panel.plan()
        self.accept()


# ----------------------------------------------------------------------------- dispatcher
class Importer:
    def __init__(self, window):
        self.w = window
        self.state = window.state

    def import_path(self, path) -> bool:
        p = Path(path)
        ext = p.suffix.lower()
        try:
            if ext == ".plb":
                self.w.open_project_path(str(p))
                return True
            if ext in LANDXML_EXT:
                return self._landxml(p)
            if ext == ".dxf":
                return self._dxf(p)
            if ext in GIS_EXT:
                return self._gis(p)
            if ext in KML_EXT:
                return self._kml(p)
            from .. import plugins
            for spec in plugins.registry.importers:
                if ext in spec.extensions:
                    return self._plugin(p, spec)
            if ext in IMAGE_EXT:
                return self.w.add_imagery_file(str(p))
            if ext in CSV_EXT or True:
                return self._csv(p)
        except Exception as ex:
            import traceback
            error_box(self.w, "Import Failed", f"{p.name} could not be imported:\n{ex}", traceback.format_exc())
            self.state.log(f"Import of {p.name} failed: {ex}", "error")
        return False

    def _job_folder(self):
        """The job folder this window is working in, when there is one (folders read relative to it)."""
        try:
            return self.w._job_folder()
        except Exception:
            return None

    # -- shared tail
    def _finish(self, batch: ImportBatch, plan: ImportPlan, label: str, path: Path):
        first = not self.state.project.points and not self.state.project.entities
        stats = apply_import(self.state, batch, plan, label, path, job_root=self._job_folder())
        word = (f"{REF.ROLE_LABELS[plan.reference_role].lower()} reference points"
                if plan.reference_role else "points")
        bits = [f"{stats['points']:,} {word}"] if stats["points"] else []
        for k, lab in (("polylines", "polylines"), ("texts", "texts"), ("surfaces", "surfaces")):
            if stats.get(k):
                bits.append(f"{stats[k]:,} {lab}")
        msg = f"Imported {', '.join(bits) or 'nothing'} from {path.name}."
        if stats.get("renumbered"):
            msg += f" {stats['renumbered']} point number(s) already existed and were renumbered."
        if stats.get("skipped"):
            msg += f" {stats['skipped']} duplicate(s) skipped."
        self.state.log(msg, "ok")
        for m in batch.messages:
            self.state.log(f"{path.name}: {m}", "warn")
        self.state.zoom_extents()
        if plan.reference_role is None and stats.get("points", 0):
            complete = getattr(self.w, "_complete_import_onboarding", None)
            if callable(complete):
                complete(imported_points=stats["points"])
        return True

    def _csv(self, p: Path) -> bool:
        dlg = CsvImportDialog(self.state, p, self.w)
        if not dlg.exec():
            return False
        return self._finish(dlg.batch, dlg.plan, f"points ({p.name})", p)

    def _generic(self, p: Path, batch: ImportBatch, title: str, geographic=False, file_crs=None, unit=None, label="") -> bool:
        if batch.is_empty():
            error_box(self.w, title, "\n".join(batch.messages) or "Nothing importable was found in this file.")
            return False
        panel = ImportOptionsPanel(self.state, geographic=geographic, file_crs=file_crs, file_unit=unit)
        panel.set_batch(batch)
        dlg = _ImportDialog(self.w, title, f"{p.name}: {batch.summary()}", panel, batch.messages)
        if not dlg.exec():
            return False
        return self._finish(batch, panel.plan(), label or p.name, p)

    def _landxml(self, p: Path) -> bool:
        from ..io import landxml
        batch = run_blocking(self.w, f"Reading {p.name} ...", landxml.read_landxml, str(p))
        return self._generic(p, batch, "Import LandXML", file_crs=file_crs_from_info(batch.info), unit=batch.info.get("units"),
                             label=f"LandXML ({p.name})")

    def _dxf(self, p: Path) -> bool:
        from ..io import dxf_io
        batch = run_blocking(self.w, f"Reading {p.name} ...", dxf_io.read_dxf, str(p))
        return self._generic(p, batch, "Import DXF", unit=batch.info.get("units"), label=f"DXF ({p.name})")

    def _gis(self, p: Path) -> bool:
        from ..io import gis_io
        dlg = GisLayerDialog(self.state, p, self.w)
        if not dlg.exec():
            return False
        if dlg.all_layers:
            batch = ImportBatch()
            for name in dlg.layer_names:
                b = run_blocking(self.w, f"Reading {p.name} / {name} ...", gis_io.read_gis, str(p), name, None)
                batch.points += b.points
                batch.polylines += b.polylines
                batch.texts += b.texts
                batch.messages += [f"{name}: {m}" for m in b.messages]
                batch.info = batch.info or b.info
        else:
            batch = run_blocking(self.w, f"Reading {p.name} ...", gis_io.read_gis, str(p), dlg.layer, dlg.fields)
        if batch.is_empty():
            error_box(self.w, "Import GIS", "The layer has no usable geometry.")
            return False
        return self._finish(batch, dlg.plan, f"GIS ({p.name})", p)

    def _kml(self, p: Path) -> bool:
        from ..io import kml_io
        from ..core.settings import user_dir
        batch = kml_io.read_kml(str(p), cache_dir=user_dir())
        ov = batch.info.get("overlays", [])
        added = 0
        if ov:
            for o in ov:
                if o.get("file") and self.w.add_kml_overlay(o):
                    added += 1
        if batch.is_empty():
            if added:
                self.state.log(f"Added {added} image overlay(s) from {p.name}.", "ok")
                return True
            error_box(self.w, "Import KML/KMZ", "\n".join(batch.messages) or "Nothing importable was found.")
            return False
        ok = self._generic(p, batch, "Import KML / KMZ", geographic=True, label=f"KML ({p.name})")
        if added:
            self.state.log(f"Added {added} image overlay(s) from {p.name}.", "ok")
        return ok

    # -- a whole folder of point files, either this job's field data or one reference role
    def import_reference_folder(self, folder, role: str | None, recursive: bool = False,
                                selected_files: list[Path] | None = None) -> dict:
        """Read selected point files, stage field data inside the package, and import the points.

        A source file is copied under the job's standard ``Field Data`` folder before its points
        are applied.  The relative source/stored paths and byte size are remembered so a later
        folder scan can disable unchanged files while allowing new or changed-size files.
        Reference-role imports keep their existing in-place behavior.
        """
        source_root = Path(folder)
        files = reference_folder_files(source_root, recursive) if selected_files is None else list(selected_files)
        tally = dict(files=0, points=0, duplicates=0, skipped=0, failed=0, unreadable=[])
        for source_file in files:
            source_file = Path(source_file)
            try:
                sn = CSV.sniff(source_file)
                mapping = CSV.CsvMapping(sn.delimiter, 1 if sn.has_header else 0, list(sn.roles))
                batch = CSV.read_points(source_file, mapping)
            except Exception as ex:
                tally["failed"] += 1
                tally["unreadable"].append((source_file.name, str(ex)))
                continue
            if not batch.points:
                tally["failed"] += 1
                tally["unreadable"].append((source_file.name, "no readable points"))
                continue

            try:
                plan = ImportPlan(reference_role=role, dup_policy="renumber")
                label = (f"{REF.label_for(role)} points ({source_file.name})" if role
                         else f"points ({source_file.name})")
                if role is None:
                    # Let the edit transaction finish Save As first.  An Untitled project has no
                    # job root yet, so computing Field Data/ before entering this block silently
                    # skipped both the package copy and the manifest for its first folder import.
                    with self.state.edit(f"Import {label}"):
                        job_root = self._job_folder()
                        field_root = Path(job_root) / "Field Data" if job_root else None
                        stored_file = (self._stage_field_data_file(source_file, source_root, field_root)
                                       if field_root is not None else source_file)
                        stats = apply_import(self.state, batch, plan, label, stored_file,
                                             job_root=job_root)
                        if stats.get("points", 0):
                            self._remember_field_data_file(
                                source_root, source_file, field_root, stored_file)
                else:
                    job_root = self._job_folder()
                    stats = apply_import(self.state, batch, plan, label, source_file,
                                         job_root=job_root)
            except Exception as ex:
                tally["failed"] += 1
                tally["unreadable"].append((source_file.name, str(ex)))
                continue

            tally["files"] += 1
            tally["points"] += stats.get("points", 0)
            tally["duplicates"] += stats.get("duplicates", 0)
            tally["skipped"] += stats.get("skipped", 0)

        self.record_reference_folder(source_root, role, tally)
        for name, why in tally["unreadable"]:
            self.state.log(f"{name}: skipped ({why})", "warn")
        if tally["points"]:
            self.state.zoom_extents()
        return tally

    @staticmethod
    def _stage_field_data_file(source_file: Path, source_root: Path, field_root: Path) -> Path:
        """Copy a source file to Field Data without replacing an earlier import."""
        source = source_file.expanduser()
        source_abs = source.resolve()
        root_abs = source_root.expanduser().resolve()
        try:
            relative = source_abs.relative_to(root_abs)
        except (OSError, RuntimeError, ValueError):
            relative = Path(source.name)
        destination_root = field_root.expanduser()
        destination = destination_root / relative
        try:
            if destination.resolve() == source_abs:
                return source
        except (OSError, RuntimeError):
            pass
        if destination.is_file():
            try:
                if filecmp.cmp(source, destination, shallow=False):
                    return destination
            except OSError:
                pass
        if destination.exists():
            stem, suffix = destination.stem, destination.suffix
            counter = 2
            while destination.exists():
                destination = destination.with_name(f"{stem} ({counter}){suffix}")
                counter += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    def _remember_field_data_file(self, source_root: Path, source_file: Path,
                                  field_root: Path | None, stored_file: Path):
        """Add one file to the manifest inside its point-import undo transaction."""
        try:
            if field_root is not None:
                record = FD.make_import_record(source_root, source_file, field_root, stored_file)
                data_folder = field_root
            else:
                # Keep identity even if this state has no package root yet.  The source-relative
                # key is enough to suppress a repeat scan of the same selected folder.
                record = {
                    "source_relative": FD.relative_file_key(source_root, source_file),
                    "stored_relative": "",
                    "size": int(Path(source_file).stat().st_size),
                }
                data_folder = source_root
        except OSError as ex:
            self.state.log(f"Could not record field-data file {source_file.name}: {ex}", "warn")
            return
        pr = self.state.project
        records = [r for r in (pr.settings.get("field_data_imports", []) or []) if isinstance(r, dict)]
        key = (record["source_relative"], record["size"], record["stored_relative"])
        already_recorded = any(
            (str(old.get("source_relative", "")).casefold(), int(old.get("size", -1)),
             str(old.get("stored_relative", "")).casefold()) == key
            for old in records
            if str(old.get("size", "")).lstrip("-+").isdigit()
        )
        if not already_recorded:
            records.append(record)
        pr.settings["field_data_imports"] = records
        pr.settings["field_data_source_folder"] = str(source_root.expanduser())
        pr.settings["data_folder"] = str(data_folder)

    def record_reference_folder(self, folder, role: str | None, tally: dict):
        """Keep lightweight folder history; per-file manifests share their point-import undo."""
        job_root = self._job_folder()
        pr = self.state.project
        history = [r for r in pr.settings.get("reference_imports", [])
                   if not (r.get("folder") == str(folder) and r.get("role") == role)]
        history.append(dict(folder=str(folder), role=role, files=int(tally.get("files", 0)),
                            points=int(tally.get("points", 0)), when=time.strftime("%Y-%m-%d %H:%M")))
        pr.settings["reference_imports"] = history[-20:]
        # With a packaged field-data import, these are set atomically alongside the points in
        # _remember_field_data_file(). Keep the legacy folder hint when no package exists.
        if role is not None or job_root is None:
            pr.settings["data_folder"] = str(folder)
        pr.touch()

    def _plugin(self, p: Path, spec) -> bool:
        from ..plugins import PluginAPI
        api = PluginAPI(self.state.project, self.state, log=lambda s: self.state.log(s))
        batch = spec.func(str(p), api)
        return self._generic(p, batch, f"Import - {spec.name}", label=f"{spec.name} ({p.name})")


# ------------------------------------------------------------ a folder of reference points
def reference_folder_files(folder, recursive: bool = True) -> list[Path]:
    """The point files in a folder, in the order a person would read them.

    ``crew 2`` comes before ``crew 10`` (natural sort, see fieldwork/utils_sort), because a folder
    of downloads is read in name order and string order gets that wrong.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    found = root.rglob("*") if recursive else root.iterdir()
    return sorted((p for p in found if p.is_file() and p.suffix.lower() in POINTS_EXT),
                  key=lambda p: natural_key(str(p.relative_to(root))))


class FolderPointsPreviewDialog(QDialog):
    """Preview all points parsed from selected CSV / point files."""

    def __init__(self, files: list[Path], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preview Selected Points")
        self.resize(940, 600)
        self.setMinimumSize(780, 440)
        self.files = list(files)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        # Load points from files
        self.all_points, self.errors = self._load_points()

        # Top summary and filter
        top = QHBoxLayout()
        count_text = f"<b>{len(self.all_points):,} points</b> from {len(self.files)} selected file(s)"
        self.lbl_summary = QLabel(count_text)
        top.addWidget(self.lbl_summary)
        top.addStretch(1)

        top.addWidget(QLabel("Filter:"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("Filter by #, description, or file...")
        self.ed_filter.setClearButtonEnabled(True)
        self.ed_filter.setFixedWidth(280)
        top.addWidget(self.ed_filter)
        lay.addLayout(top)

        if self.errors:
            err_msg = "; ".join(f"{name}: {err}" for name, err in self.errors)
            lay.addWidget(Banner(f"Some files could not be read: {err_msg}", "warn"))

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["Point #", "Northing", "Easting", "Elevation", "Description", "Source File", "Subfolder"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table, 1)

        self.lbl_status = QLabel("")
        lay.addWidget(self.lbl_status)

        # Close button
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)

        self.ed_filter.textChanged.connect(self._populate_table)
        self._populate_table()

    def _load_points(self):
        pts = []
        errs = []
        for f in self.files:
            try:
                sn = CSV.sniff(f)
                m = CSV.CsvMapping(sn.delimiter, 1 if sn.has_header else 0, list(sn.roles))
                batch = CSV.read_points(f, m)
                if not batch.points:
                    errs.append((f.name, "no points"))
                else:
                    for p in batch.points:
                        folder_name = f.parent.name if f.parent and f.parent.name else ""
                        pts.append((str(p.number), p.y, p.x, p.z, str(p.desc or ""), f.name, folder_name))
            except Exception as ex:
                errs.append((f.name, str(ex)))
        return pts, errs

    def _populate_table(self):
        query = self.ed_filter.text().strip().lower()
        if query:
            filtered = [
                pt for pt in self.all_points
                if query in pt[0].lower() or query in pt[4].lower() or query in pt[5].lower() or query in pt[6].lower()
            ]
        else:
            filtered = self.all_points

        # Cap display to 3,000 rows for smooth UI performance if there are tens of thousands of points
        max_rows = 3000
        display_rows = filtered[:max_rows]
        self.table.setRowCount(len(display_rows))

        for row, (num, y, x, z, desc, fname, fol) in enumerate(display_rows):
            item_num = QTableWidgetItem(num)
            item_num.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 0, item_num)

            item_y = QTableWidgetItem(fmt_coord(y, 3))
            item_y.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 1, item_y)

            item_x = QTableWidgetItem(fmt_coord(x, 3))
            item_x.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 2, item_x)

            item_z = QTableWidgetItem(fmt_coord(z, 3) if math.isfinite(z) else "-")
            item_z.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 3, item_z)

            item_desc = QTableWidgetItem(desc)
            self.table.setItem(row, 4, item_desc)

            item_file = QTableWidgetItem(fname)
            self.table.setItem(row, 5, item_file)

            item_fol = QTableWidgetItem(fol)
            self.table.setItem(row, 6, item_fol)

        if len(filtered) > max_rows:
            self.lbl_status.setText(f"Showing first {max_rows:,} of {len(filtered):,} points matching filter ({len(self.all_points):,} total)")
        elif query:
            self.lbl_status.setText(f"Showing {len(filtered):,} of {len(self.all_points):,} points matching \"{query}\"")
        else:
            self.lbl_status.setText(f"Showing all {len(self.all_points):,} points")


class ReferenceFolderDialog(FormDialog):
    """Select point files from one folder for field-data or reference-point import."""

    def __init__(self, state, folder: Path, parent=None, *, field_data_only: bool = False):
        intro = ("Choose the survey files to add to this project's Field Data folder. "
                 "Previously imported files with the same relative path and size are greyed out; "
                 "new and changed-size files are selectable." if field_data_only else
                 "Every point file is imported with the role chosen here. Files that cannot be "
                 "read are left alone and reported - nothing is guessed silently.")
        title = "Import Field Data Folder" if field_data_only else "Import Points from Folder"
        super().__init__(parent, title, intro, "Import", 700)
        self.state = state
        self.folder = Path(folder)
        self.field_data_only = bool(field_data_only)
        self.cmb_role = QComboBox()
        self.cmb_role.addItem("This job's field data - joins the fieldwork list", None)
        for role in REF.ROLES:
            self.cmb_role.addItem(f"{REF.ROLE_LABELS[role]} - layer {REF.layer_for(role)}", role)
        self.chk_sub = QCheckBox("Include sub-folders")
        self.chk_sub.setChecked(True)

        self.form.addRow("Folder:", QLabel(str(self.folder)))
        if not self.field_data_only:
            self.form.addRow("These points are:", self.cmb_role)
        self.form.addRow("", self.chk_sub)

        self.lbl_manifest = QLabel("Matching-size files already imported are greyed out.")
        self.lbl_manifest.setStyleSheet("color: #777; font-style: italic;")
        self.lbl_manifest.setVisible(self.field_data_only)
        self.form.addRow(self.lbl_manifest)

        file_head_widget = QWidget()
        file_head_lay = QHBoxLayout(file_head_widget)
        file_head_lay.setContentsMargins(0, 0, 0, 0)
        self.lbl_file_count = QLabel("Point files found:")
        file_head_lay.addWidget(self.lbl_file_count)
        file_head_lay.addStretch(1)
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_none = QPushButton("Select None")
        self.btn_preview = QPushButton("Preview Points...")
        self.btn_preview.setToolTip("View all points and coordinates from selected files")
        file_head_lay.addWidget(self.btn_select_all)
        file_head_lay.addWidget(self.btn_select_none)
        file_head_lay.addWidget(self.btn_preview)
        self.form.addRow(file_head_widget)

        self.chk_table = QTableWidget()
        self.chk_table.setColumnCount(4)
        self.chk_table.setHorizontalHeaderLabels(["", "File", "Subfolder / Path", "Size"])
        self.chk_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.chk_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.chk_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.chk_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.chk_table.setMinimumHeight(220)
        self.chk_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.chk_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.form.addRow(self.chk_table)

        self.lbl_none = QLabel("No point files (.csv, .txt, .pts, .xyz ...) found in this folder.")
        self.lbl_none.setStyleSheet("color: #888; font-style: italic;")
        self.lbl_none.setVisible(False)
        self.form.addRow(self.lbl_none)

        self._file_paths: list[Path] = []
        self._checkboxes: list[QCheckBox] = []
        self._imported_flags: list[bool] = []

        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_select_none.clicked.connect(self._select_none)
        self.btn_preview.clicked.connect(self._preview_points)
        self.chk_table.cellClicked.connect(self._cell_clicked)
        self.chk_table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.cmb_role.currentIndexChanged.connect(self._refresh_files)
        self.chk_sub.toggled.connect(self._refresh_files)
        self._refresh_files()

    def files(self) -> list[Path]:
        return reference_folder_files(self.folder, self.chk_sub.isChecked())

    def role(self) -> str | None:
        return self.cmb_role.currentData()

    def _refresh_files(self):
        files = self.files()
        self._file_paths = list(files)
        self._checkboxes = []
        self._imported_flags = []
        if not files:
            self.chk_table.setRowCount(0)
            self.chk_table.setVisible(False)
            self.lbl_none.setText("No point files (.csv, .txt, .pts, .xyz ...) found in this folder.")
            self.lbl_none.setVisible(True)
            self.btn_select_all.setEnabled(False)
            self.btn_select_none.setEnabled(False)
            self.btn_preview.setEnabled(False)
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            self.lbl_file_count.setText("No point files found.")
            return

        tracking_field_data = self.field_data_only or self.role() is None
        self.lbl_manifest.setVisible(tracking_field_data)
        records = self.state.project.settings.get("field_data_imports", []) if tracking_field_data else []
        imported = [FD.imported_file_matches(records, self.folder, file) for file in files]
        self.chk_table.setVisible(True)
        self.lbl_none.setVisible(False)
        self.chk_table.setRowCount(len(files))

        for row, (file, already_imported) in enumerate(zip(files, imported)):
            chk = QCheckBox()
            chk.setChecked(not already_imported)
            chk.setEnabled(not already_imported)
            if already_imported:
                chk.setToolTip("Already imported: matching relative path and file size")
            chk.toggled.connect(self._update_count)
            self._checkboxes.append(chk)
            self._imported_flags.append(already_imported)
            chk_widget = QWidget()
            chk_widget.setEnabled(not already_imported)
            chk_lay = QHBoxLayout(chk_widget)
            chk_lay.addWidget(chk)
            chk_lay.setAlignment(Qt.AlignCenter)
            chk_lay.setContentsMargins(4, 2, 4, 2)
            self.chk_table.setCellWidget(row, 0, chk_widget)

            try:
                rel = file.relative_to(self.folder).parent.as_posix()
                if rel == ".":
                    rel = "(root)"
            except Exception:
                rel = str(file.parent)
            size = file.stat().st_size if file.exists() else 0
            size_text = f"{size:,} B" if size < 1024 else f"{size / 1024:.1f} KB"
            items = (QTableWidgetItem(file.name), QTableWidgetItem(rel), QTableWidgetItem(size_text))
            items[2].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for column, item in enumerate(items, start=1):
                if already_imported:
                    item.setForeground(QColor(theme.colors()["dim"]))
                    item.setBackground(QColor(theme.colors()["alt"]))
                    item.setToolTip("Already imported: matching relative path and file size")
                    disabled_flags = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                                      | Qt.ItemFlag.ItemIsEditable)
                    item.setFlags(item.flags() & ~disabled_flags)
                self.chk_table.setItem(row, column, item)

        available = sum(1 for chk in self._checkboxes if chk.isEnabled())
        if self.field_data_only and available == 0:
            self.lbl_none.setText("All matching-size point files have already been imported.")
            self.lbl_none.setVisible(True)
        else:
            self.lbl_none.setVisible(False)
        self.btn_select_all.setEnabled(available > 0)
        self.btn_select_none.setEnabled(available > 0)
        self._update_count()

    def _cell_clicked(self, row: int, col: int):
        if col > 0 and 0 <= row < len(self._checkboxes):
            chk = self._checkboxes[row]
            if chk.isEnabled():
                chk.setChecked(not chk.isChecked())

    def _cell_double_clicked(self, row: int, col: int):
        if 0 <= row < len(self._file_paths):
            dlg = FolderPointsPreviewDialog([self._file_paths[row]], self)
            dlg.exec()

    def _select_all(self):
        for chk in self._checkboxes:
            if chk.isEnabled():
                chk.setChecked(True)

    def _select_none(self):
        for chk in self._checkboxes:
            if chk.isEnabled():
                chk.setChecked(False)

    def _preview_points(self):
        selected = self.selected_files()
        if not selected:
            error_box(self, "Preview Points", "Please select at least one point file to preview.")
            return
        FolderPointsPreviewDialog(selected, self).exec()

    def _update_count(self):
        selected = sum(1 for chk in self._checkboxes if chk.isChecked())
        total = len(self._checkboxes)
        already = sum(1 for chk in self._checkboxes if not chk.isEnabled())
        self.lbl_file_count.setText(
            f"Point files found ({selected} of {total} selected" +
            (f", {already} already imported" if already else "") + "):")
        self.btn_preview.setEnabled(selected > 0)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(selected > 0)

    def selected_files(self) -> list[Path]:
        """Return the list of files the user checked."""
        return [file for file, chk in zip(self._file_paths, self._checkboxes) if chk.isEnabled() and chk.isChecked()]

    def validate(self):
        if not self.files():
            return "There are no point files in this folder."
        if not self.selected_files():
            if self.field_data_only and self._file_paths and all(self._imported_flags):
                return "All matching-size files have already been imported."
            return "Please select at least one file to import."
        return None


def describe_folder_import(tally: dict) -> str:
    """One line for the log / the status bar after a folder import."""
    bits = [f"{tally.get('points', 0):,} point(s) from {tally.get('files', 0)} file(s)"]
    if tally.get("duplicates"):
        bits.append(f"{tally['duplicates']:,} duplicate number(s) (renumbered inside the role)")
    if tally.get("failed"):
        bits.append(f"{tally['failed']} file(s) skipped")
    return ", ".join(bits)
