"""Survey > File Coordinate Systems... - what system each file's points are stored in.

Imports record the project CRS after any import-time conversion and keep the declared or selected
source CRS as provenance. This dialog makes the current stored-coordinate state explicit and offers
exactly three corrections:

* **Set to Project** records the current project system without moving points.
* **Reproject** asks for the corrected source CRS, then converts only that file's points into the
  project's current CRS. Source and destination ground/grid SAFs are applied when enabled.
* **Ground / Grid SAF** rescales only that file's points without changing their horizontal datum.

The file list is built from point provenance, so older projects with no CRS record still show their
files with a blank system. Every correction is one undoable operation and reports how many points
it touched.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core import filecrs as FCRC
from .crs_dialog import CRSPicker
from .crs_extra import VerticalAndGroundPanel
from .widgets import Hint, error_box, info_box


class _PickCRS(QDialog):
    """Pick the system a file is actually in - coordinate, vertical datum and ground scale."""

    def __init__(self, parent, record: dict | None = None, title="What System Is This File In?"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 640)
        lay = QVBoxLayout(self)
        lay.addWidget(Hint("Pick the corrected source system these file coordinates are in now. "
                           "Reproject will convert them into the project system. Vertical datum and "
                           "ground scale are included; source and project SAFs are applied when enabled."))
        self.picker = CRSPicker(kinds=("projected", "geographic"))
        lay.addWidget(self.picker, 1)
        self.extra = VerticalAndGroundPanel(self)
        lay.addWidget(self.extra)
        row = QHBoxLayout()
        row.addStretch(1)
        b_ok = QPushButton("Use This System")
        b_ok.setProperty("accent", True)
        b_no = QPushButton("Cancel")
        row.addWidget(b_ok)
        row.addWidget(b_no)
        lay.addLayout(row)
        b_ok.clicked.connect(self.accept)
        b_no.clicked.connect(self.reject)
        if record:
            key = record.get("key") or ""
            if key:
                try:
                    self.picker.select_key(key)
                except Exception:
                    pass
            self.extra.cmb_datum.setCurrentIndex(max(0, self.extra.cmb_datum.findData(record.get("vertical") or "")))
            self.extra.cmb_geoid.setCurrentIndex(max(0, self.extra.cmb_geoid.findData(record.get("geoid") or "")))
            self.extra.chk_ground.setChecked(bool(record.get("ground")))
            self.extra.sp_cf.setValue(float(record.get("saf") or 1.0))
            self.extra.sp_by.setValue(float(record.get("base_n") or 0.0))
            self.extra.sp_bx.setValue(float(record.get("base_e") or 0.0))

    def record(self) -> dict:
        from ..core import crs as C
        key = self.picker.current_key() or ""
        try:
            crs = C.ProjectCRS.from_key(key)
            label, unit, vunit = crs.label, crs.unit, crs.vunit
        except Exception:
            label, unit, vunit = key, "", ""
        return {**FCRC.make(key=key, label=label, unit=unit, vunit=vunit, method="edited"),
                **{k: v for k, v in self.extra.record().items()}}


class FileCoordinateSystemsDialog(QDialog):
    """The files that brought data in, and what has to be true about their coordinates."""

    COLS = ["File", "Points", "Coordinate system", "Vertical", "SAF", "How it got that way"]

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("File Coordinate Systems")
        self.resize(1000, 620)
        lay = QVBoxLayout(self)
        lay.addWidget(Hint(
            "Each row lists the coordinate system of that file's points as they are stored now. "
            "**Set to Project** records the project's current system without moving coordinates. "
            "**Reproject** asks for the corrected source system and converts those points into the "
            "project system, applying source and project SAFs when present. **Ground / grid SAF** "
            "changes the scale only."))
        self.tbl = QTableWidget(0, len(self.COLS))
        self.tbl.setHorizontalHeaderLabels(self.COLS)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.tbl, 1)

        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)

        row = QHBoxLayout()
        self.b_set = QPushButton("Set to Project")
        self.b_repro = QPushButton("Reproject")
        self.b_ground = QPushButton("Ground / Grid SAF")
        for b in (self.b_set, self.b_repro, self.b_ground):
            row.addWidget(b)
        row.addStretch(1)
        self.b_cancel = QPushButton("Cancel")
        self.b_cancel.clicked.connect(self.reject)
        row.addWidget(self.b_cancel)
        lay.addLayout(row)
        lay.addWidget(Hint("Project system, for comparison: " + state.project.crs.label.replace("\n", "  ·  ")))

        self.tbl.itemSelectionChanged.connect(self._picked)
        self.b_set.clicked.connect(self.set_to_project)
        self.b_repro.clicked.connect(self.reproject)
        self.b_ground.clicked.connect(self.ground_grid)
        self.reload()

    # ------------------------------------------------------------------ table
    def reload(self):
        self.rows = FCRC.files(self.state.project)
        self.tbl.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            crs = row.get("crs") or {}
            label = crs.get("label") or ""
            label = label.splitlines()[0] if label else "(not recorded)"
            vals = [row["file"], f"{row['points']:,}", label,
                    crs.get("vertical") or "", f"{float(crs.get('saf') or 1.0):.9f}"
                    if crs.get("ground") else "grid",
                    FCRC.METHODS.get(row.get("method") or "", row.get("method") or "")]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 2 and not crs:
                    it.setToolTip("No system recorded for this file - set one.")
                elif c == 2 and crs.get("source_crs"):
                    source = crs["source_crs"]
                    origin = {"imported": "Import source", "reprojected": "Reprojected from"}.get(
                        crs.get("method"), "Recorded source")
                    it.setToolTip(f"Stored coordinates are in the system shown. {origin}: "
                                   f"{source.get('label') or source.get('key') or 'not recorded'}.")
                self.tbl.setItem(r, c, it)
        self.tbl.resizeColumnsToContents()
        self._picked()

    def _current(self) -> dict | None:
        r = self.tbl.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def _picked(self):
        row = self._current()
        have = row is not None
        self.b_set.setEnabled(have)
        self.b_repro.setEnabled(have and not self.state.project.crs.is_local)
        self.b_ground.setEnabled(have)
        if not have:
            self.lbl.setText("")
            return
        crs = row.get("crs") or {}
        self.lbl.setText(
            f"<b>{row['file']}</b> - {row['points']:,} point(s)"
            + (f" from {row['folder']}" if row.get("folder") else "")
            + (f".  Recorded as: {crs.get('label', '').replace(chr(10), '  ·  ')}"
               + (f" (SAF {float(crs.get('saf') or 1):.9f}, "
                  f"base N {float(crs.get('base_n') or 0):,.3f} E {float(crs.get('base_e') or 0):,.3f})"
                  if crs.get("ground") else " (grid)")
               + (f".  {crs.get('when')}" if crs.get("when") else "")
               if crs else ".  No coordinate system recorded - the numbers are wherever they landed."))

    # ------------------------------------------------------------------ operations
    def _apply(self, label: str, fn):
        try:
            with self.state.edit(label):
                out = fn()
        except Exception as ex:
            error_box(self, "File Coordinate Systems", str(ex))
            return None
        # state.edit already touches the project, emits the change notification, and updates undo.
        self.reload()
        return out

    def set_to_project(self):
        row = self._current()
        if row is None:
            return
        target = self.state.project.crs.label.splitlines()[0]
        out = self._apply(f"Set {row['file']} to project system",
                          lambda: FCRC.set_to_project(self.state.project, row["file"]))
        if out is not None:
            self.state.log(f"{row['file']}: recorded as the project coordinate system; no coordinates moved.", "info")
            self.lbl.setText(f"{row['file']} is set to {target}.  Nothing moved.")

    def reproject(self):
        row = self._current()
        if row is None:
            return
        dlg = _PickCRS(self, row.get("crs"), title=f"Correct source system - {row['file']}")
        if not dlg.exec():
            return
        source_rec = dlg.record()
        if not source_rec.get("key"):
            error_box(self, "File Coordinate Systems", "Pick the corrected source coordinate system.")
            return

        project_crs = self.state.project.crs
        n = len(FCRC.points_of(self.state.project, row["file"]))
        source_label = source_rec["label"].splitlines()[0]
        target_label = project_crs.label.splitlines()[0]
        if QMessageBox.question(
                self, "Reproject File",
                f"Treat the {n:,} point(s) from {row['file']} as {source_label}, then convert them "
                f"into the project system ({target_label})?\n\nSource and project ground/grid SAFs "
                f"are applied when enabled. Nothing else in the project is touched; this is one undo step.") != QMessageBox.Yes:
            return

        out = self._apply(f"Reproject {row['file']} to project system",
                          lambda: FCRC.reproject(self.state.project, row["file"], source_rec))
        if out:
            self.state.log(f"{row['file']}: {out['points']:,} point(s) reprojected from "
                           f"{out['from'].splitlines()[0]} to {out['to'].splitlines()[0]}.", "ok")
            self.lbl.setText(f"{row['file']} was reprojected into the project system.")

    def ground_grid(self):
        row = self._current()
        if row is None:
            return
        from PySide6.QtWidgets import QInputDialog
        crs = row.get("crs") or {}
        project_ground = self.state.project.crs.ground
        default_saf = project_ground.saf if project_ground.enabled else 1.0
        saf0 = float(crs.get("saf") or default_saf) or 1.0
        base_n = float(crs.get("base_n", project_ground.base_y) or 0.0)
        base_e = float(crs.get("base_e", project_ground.base_x) or 0.0)
        text, ok = QInputDialog.getText(self, "Ground / Grid SAF",
                                       f"SAF for {row['file']} (ground = grid x SAF).\n"
                                       f"Scale origin: N {base_n:,.3f}, E {base_e:,.3f}.\n\n"
                                       f"Enter the whole factor: 1.000136506 is not 1.00013650.",
                                       text=f"{saf0:.9f}".rstrip("0").rstrip(".") or "1.0")
        if not ok or not text.strip():
            return
        try:
            saf = float(text.strip())
        except ValueError:
            error_box(self, "Ground / Grid", f"'{text.strip()}' is not a number.")
            return
        if not (0.9 < saf < 1.1):
            error_box(self, "Ground / Grid", "That is more than 10% away from 1.0 - a SAF is a few "
                                             "parts per million.  Check the decimal point.")
            return
        direction = QMessageBox.question(
            self, "Ground / Grid SAF",
            f"Treat the {row['points']:,} point(s) from {row['file']} as:\n\n"
            f"  Yes  - GROUND, and convert to grid (divide by {saf:.9f})\n"
            f"  No   - GRID, and convert to ground (multiply by {saf:.9f})\n\n"
            f"Scale origin N {base_n:,.3f}, E {base_e:,.3f}. Either way it is one undo step.")
        to_ground = direction != QMessageBox.Yes
        out = self._apply(f"{'Ground' if to_ground else 'Grid'} rescale of {row['file']}",
                          lambda: FCRC.rescale_ground(self.state.project, row["file"], saf=saf,
                                                      to_ground=to_ground, base_n=base_n, base_e=base_e))
        if out:
            self.state.log(f"{row['file']}: {out['points']:,} point(s) rescaled with SAF {saf:.9f} "
                           f"({'grid -> ground' if to_ground else 'ground -> grid'}).", "ok")


__all__ = ["FileCoordinateSystemsDialog"]
