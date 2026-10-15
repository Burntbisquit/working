"""Imagery: the add-imagery dialog and the Imagery panel (layers, accuracy, Google Earth round trip)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QRadioButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from ..core import imagery as IM
from ..core import units as U
from ..core.model import ImageryLayer
from ..core.settings import settings
from ..core.tiles import PRESETS, TileSource, fetch_esri_metadata
from .widgets import Banner, Hint, dspin, error_box, info_box, run_blocking


# ----------------------------------------------------------------------------- add imagery
class AddImageryDialog(QDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Add Imagery")
        self.setMinimumWidth(620)
        self.layer_spec: dict | None = None
        root = QVBoxLayout(self)
        if state.project.crs.is_local:
            root.addWidget(Banner("Assign a project coordinate system before importing imagery, including georeferenced files. "
                                  "Cancel the CRS picker to leave imagery unchanged.", "warn"))
        # --- tiles
        self.r_tiles = QRadioButton("Online satellite / map tiles")
        self.r_tiles.setChecked(not state.project.crs.is_local)
        root.addWidget(self.r_tiles)
        self.cmb = QComboBox()
        custom = [TileSource.from_dict(d) for d in (settings().get("custom_tile_sources") or [])]
        # A shipped source the user moved or switched off is out of the list; the registry
        # (core/registry.py) is where it stays visible and can be restored.
        _gone = set(settings().get("replaced_tile_sources") or []) | set(settings().get("hidden_tile_sources") or [])
        self.sources = [p for p in PRESETS if p.name not in _gone] + custom
        for s in self.sources:
            self.cmb.addItem(s.name)
        root.addWidget(self.cmb)
        self.note = Hint("")
        root.addWidget(self.note)
        # --- custom xyz
        self.r_xyz = QRadioButton("Custom tile URL  (XYZ / slippy-map - e.g. a county orthophoto service)")
        root.addWidget(self.r_xyz)
        f = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("https://server/path/{z}/{x}/{y}.jpg")
        self.sp_zoom = QSpinBox()
        self.sp_zoom.setRange(1, 24)
        self.sp_zoom.setValue(19)
        self.ed_attr = QLineEdit()
        f.addRow("Name:", self.ed_name)
        f.addRow("URL template:", self.ed_url)
        f.addRow("Max zoom:", self.sp_zoom)
        f.addRow("Attribution:", self.ed_attr)
        self.xyz_box = QWidget()
        self.xyz_box.setLayout(f)
        root.addWidget(self.xyz_box)
        # --- file
        self.r_file = QRadioButton("Georeferenced image file  (GeoTIFF, or PNG / JPG with a world file)")
        root.addWidget(self.r_file)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.ed_file = QLineEdit()
        self.btn_file = QPushButton("Browse...")
        row.addWidget(self.ed_file, 1)
        row.addWidget(self.btn_file)
        col.addLayout(row)
        col.addWidget(Hint("Use this for survey-grade orthophotos from your county or state (and for plans you have georeferenced). "
                           "A world-file image is assumed to be in the project's coordinate system."))
        self.file_box = QWidget()
        self.file_box.setLayout(col)
        root.addWidget(self.file_box)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Add")
        self.bb.accepted.connect(self._ok)
        self.bb.rejected.connect(self.reject)
        root.addWidget(self.bb)
        self.cmb.currentIndexChanged.connect(self._src)
        self.btn_file.clicked.connect(self._browse)
        for r in (self.r_tiles, self.r_xyz, self.r_file):
            r.toggled.connect(self._mode)
        if state.project.crs.is_local:
            self.r_file.setChecked(True)
            self.r_tiles.setEnabled(False)
            self.r_xyz.setEnabled(False)
        self._src()
        self._mode()

    def _mode(self):
        self.cmb.setEnabled(self.r_tiles.isChecked())
        self.xyz_box.setEnabled(self.r_xyz.isChecked())
        self.file_box.setEnabled(self.r_file.isChecked())

    def _src(self):
        s = self.sources[self.cmb.currentIndex()]
        txt = f"Max zoom {s.max_zoom}.  {s.attribution}"
        if "Esri" in s.name:
            txt += ("\n\nEsri's service is free to view but governed by Esri's terms of use - keep the attribution, and check your licence before "
                    "putting screenshots in deliverables. Its published horizontal accuracy varies by place (often several metres): look it up with "
                    "'Source accuracy' in the Imagery panel.")
        elif "USGS" in s.name:
            txt += "\n\nUSGS orthoimagery is public domain; zoom is limited (about 0.3 m pixels at best, depends on the area)."
        elif "OpenStreetMap" in s.name:
            txt += "\n\nStreet map for context only - not imagery. Please respect the OSM tile usage policy (no bulk downloading)."
        self.note.setText(txt)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Georeferenced image", "", "Images (*.tif *.tiff *.png *.jpg *.jpeg);;All files (*)")
        if p:
            self.ed_file.setText(p)

    def _ok(self):
        if self.state.project.crs.is_local:
            error_box(self, "Add Imagery", "Assign a project coordinate system before importing imagery.")
            return
        if self.r_tiles.isChecked():
            s = self.sources[self.cmb.currentIndex()]
            self.layer_spec = {"kind": "tiles", "name": s.name, "source": s.to_dict()}
        elif self.r_xyz.isChecked():
            url = self.ed_url.text().strip()
            if not all(k in url for k in ("{z}", "{x}", "{y}")):
                error_box(self, "Custom Tiles", "The URL template must contain {z}, {x} and {y}.")
                return
            name = self.ed_name.text().strip() or "Custom tiles"
            src = TileSource(name, url, self.sp_zoom.value(), 0, self.ed_attr.text().strip())
            custom = list(settings().get("custom_tile_sources") or [])
            custom = [d for d in custom if d["url"] != url] + [src.to_dict()]
            settings().set("custom_tile_sources", custom)
            self.layer_spec = {"kind": "tiles", "name": name, "source": src.to_dict()}
        else:
            path = self.ed_file.text().strip()
            if not path or not Path(path).exists():
                error_box(self, "Image File", "Choose an image file.")
                return
            try:
                r = run_blocking(self, "Reading image ...", IM.read_georeferenced_image, path)
            except RuntimeError as ex:
                msg = str(ex).strip().splitlines()[-1]
                error_box(self, "Image File", msg, str(ex))
                return
            crs = r["crs"]
            if crs is None:
                crs = None           # world file: assumed to be in the project coordinate system
            self.layer_spec = {"kind": "file", "name": Path(path).stem,
                               "source": {"path": path, "corners": [list(c) for c in r["corners"]], "crs": crs,
                                          "attribution": ""}}
        self.accept()


# ----------------------------------------------------------------------------- dock
def _acc_text(meta: dict, unit: str) -> str:
    if not meta:
        return ""
    a = meta.get("accuracy_m")
    bits = []
    if meta.get("source") or meta.get("product"):
        bits.append(f"{meta.get('source', '')} {meta.get('product', '')} {meta.get('sensor', '')}".strip())
    if meta.get("date"):
        bits.append(f"captured {meta['date']}")
    if meta.get("resolution_m"):
        bits.append(f"{meta['resolution_m']:g} m pixels")
    if a:
        bits.append(f"stated accuracy {a:g} m ({a / U.M_PER_UNIT.get(unit, 0.3048):,.1f} {unit})")
    return ", ".join(bits)


class ImageryDock(QWidget):
    add_requested = Signal()
    ge_import_requested = Signal()
    ge_export_requested = Signal()
    nudge_by_points_requested = Signal()

    def __init__(self, state, canvas, parent=None):
        super().__init__(parent)
        self.state = state
        self.canvas = canvas
        self._busy = False
        self._nudge_active = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setSpacing(8)

        # ---- layers
        g1 = QGroupBox("Imagery Layers")
        l1 = QVBoxLayout(g1)
        self.lst = QListWidget()
        self.lst.setMaximumHeight(90)
        l1.addWidget(self.lst)
        row = QHBoxLayout()
        self.btn_add = QPushButton("Add...")
        self.btn_rm = QPushButton("Remove")
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_rm)
        l1.addLayout(row)
        form = QFormLayout()
        self.sl_op = QSlider(Qt.Horizontal)
        self.sl_op.setRange(5, 100)
        self.sl_op.setValue(100)
        self.sp_de = dspin(0, -1e6, 1e6, 3, 0.5)
        self.sp_dn = dspin(0, -1e6, 1e6, 3, 0.5)
        form.addRow("Opacity:", self.sl_op)
        form.addRow("Nudge east:", self.sp_de)
        form.addRow("Nudge north:", self.sp_dn)
        l1.addLayout(form)
        self.btn_nudge_points = QPushButton("Nudge by points...")
        self.btn_nudge_points.setToolTip(
            "In the top view, click an image feature, then where it should land; the target click uses the current snap setting. "
            "Repeat for more pairs; press Enter or right-click to apply the average shift, or Esc to cancel.")
        l1.addWidget(self.btn_nudge_points)
        self.lbl_layer = Hint("")
        l1.addWidget(self.lbl_layer)
        self.lbl_legacy = Hint("")
        self.lbl_legacy.setVisible(False)
        l1.addWidget(self.lbl_legacy)
        row = QHBoxLayout()
        self.chk_off = QCheckBox("Offline (cache only)")
        self.chk_off.setChecked(bool(settings().get("offline_imagery")))
        self.btn_cache = QPushButton("Clear tile cache")
        row.addWidget(self.chk_off)
        row.addWidget(self.btn_cache)
        l1.addLayout(row)
        lay.addWidget(g1)

        # ---- source accuracy
        g2 = QGroupBox("Source Accuracy")
        l2 = QVBoxLayout(g2)
        self.btn_meta = QPushButton("Look up imagery source at the view centre")
        self.lbl_meta = Banner("", "info")
        self.lbl_meta.setVisible(False)
        l2.addWidget(self.btn_meta)
        l2.addWidget(self.lbl_meta)
        lay.addWidget(g2)

        # ---- Google Earth round trip
        g4 = QGroupBox("Google Earth Round Trip")
        l4 = QVBoxLayout(g4)
        l4.addWidget(Hint("1) Export your points to KMZ and open it in Google Earth.  2) Drop a pin on the same feature for each point and give it the "
                          "point's NUMBER as its name.  3) Save the pins as KML/KMZ and import them here.  The pins come in as reference points on the "
                          "GOOGLE-EARTH-PINS layer, so you can measure between a pin and its point by hand."))
        r3 = QHBoxLayout()
        self.btn_ge_out = QPushButton("Export KMZ...")
        self.btn_ge_in = QPushButton("Import pins...")
        r3.addWidget(self.btn_ge_out)
        r3.addWidget(self.btn_ge_in)
        l4.addLayout(r3)
        lay.addWidget(g4)
        lay.addStretch(1)

        # wiring
        self.btn_add.clicked.connect(self.add_requested.emit)
        self.btn_rm.clicked.connect(self._remove)
        self.btn_nudge_points.clicked.connect(self.nudge_by_points_requested.emit)
        self.lst.currentRowChanged.connect(self._layer_selected)
        self.lst.itemChanged.connect(self._item_changed)
        self.sl_op.valueChanged.connect(self._opacity)
        self.sp_de.valueChanged.connect(self._nudge_changed)
        self.sp_dn.valueChanged.connect(self._nudge_changed)
        self.chk_off.toggled.connect(self._offline)
        self.btn_cache.clicked.connect(self._clear_cache)
        self.btn_meta.clicked.connect(self._lookup)
        self.btn_ge_out.clicked.connect(self.ge_export_requested.emit)
        self.btn_ge_in.clicked.connect(self.ge_import_requested.emit)
        state.changed.connect(self._state_changed)
        state.project_replaced.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ helpers
    def _unit(self):
        return self.state.project.h_unit


    def current_layer(self):
        i = self.lst.currentRow()
        if i < 0:
            return None
        return self.state.project.imagery.get(self.lst.item(i).data(Qt.UserRole))

    def current_layer_name(self) -> str:
        lay = self.current_layer()
        return lay.name if lay else ""

    def _state_changed(self, kinds):
        if self._busy:
            return
        self.refresh(keep_selection=True)

    # ------------------------------------------------------------------ refresh
    def refresh(self, keep_selection: bool = False):
        self._busy = True
        pr = self.state.project
        cur_id = None
        if keep_selection and self.lst.currentRow() >= 0:
            cur_id = self.lst.item(self.lst.currentRow()).data(Qt.UserRole)
        self.lst.clear()
        for lay in pr.imagery.values():
            it = QListWidgetItem(lay.name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if lay.visible else Qt.Unchecked)
            it.setData(Qt.UserRole, lay.id)
            self.lst.addItem(it)
        if self.lst.count():
            row = 0
            if cur_id is not None:
                for i in range(self.lst.count()):
                    if self.lst.item(i).data(Qt.UserRole) == cur_id:
                        row = i
            self.lst.setCurrentRow(row)
        n_chk = len(pr.checks)
        self.lbl_legacy.setVisible(bool(n_chk))
        if n_chk:
            self.lbl_legacy.setText(f"{n_chk:,} imagery check(s) saved by an older version are still kept with this "
                                    f"project and are saved again on save.  Checking points against imagery was "
                                    f"removed - use Draw > Measure between two points instead.")
        self._busy = False
        self._layer_selected(self.lst.currentRow())

    def _layer_selected(self, row):
        lay = self.current_layer()
        enabled = lay is not None
        self.lst.setEnabled(not self._nudge_active)
        self.btn_add.setEnabled(not self._nudge_active)
        self.btn_rm.setEnabled(enabled and not self._nudge_active)
        self.btn_nudge_points.setEnabled(enabled or self._nudge_active)
        for w in (self.sl_op, self.sp_de, self.sp_dn):
            w.setEnabled(enabled and not self._nudge_active)
        for w in (self.btn_meta, self.btn_ge_out, self.btn_ge_in, self.chk_off, self.btn_cache):
            w.setEnabled(not self._nudge_active)
        if lay is None:
            self.lbl_layer.setText("No imagery loaded. Use Add...")
            return
        self._busy = True
        self.sl_op.setValue(int(lay.opacity * 100))
        self.sp_de.setValue(lay.nudge[0])
        self.sp_dn.setValue(lay.nudge[1])
        self._busy = False
        meta = lay.source.get("meta")
        txt = lay.source.get("attribution", "") or ("Georeferenced image file" if lay.kind == "file" else "")
        self.lbl_layer.setText(txt)
        if meta:
            self._show_meta(meta)
        else:
            self.lbl_meta.setVisible(False)

    def set_nudge_mode_active(self, active: bool):
        self._nudge_active = bool(active)
        self.btn_nudge_points.setText("Cancel point nudge" if self._nudge_active else "Nudge by points...")
        self.btn_nudge_points.setToolTip(
            "Click an image feature, then where it should land (the target follows the current snap setting). "
            "Repeat pairs, then press Enter/right-click to apply the average nudge, or Esc to cancel."
            if self._nudge_active else
            "In the top view, click an image feature, then where it should land; the target click uses the current snap setting. "
            "Repeat for more pairs; press Enter or right-click to apply the average shift, or Esc to cancel.")
        self._layer_selected(self.lst.currentRow())

    def _item_changed(self, it):
        if self._busy:
            return
        lay = self.state.project.imagery.get(it.data(Qt.UserRole))
        if lay is not None:
            lay.visible = it.checkState() == Qt.Checked
            self._busy = True
            self.state.refresh(("imagery",))
            self._busy = False

    def _opacity(self, v):
        lay = self.current_layer()
        if lay is None or self._busy:
            return
        lay.opacity = v / 100.0
        self._busy = True
        self.state.refresh(("imagery",))
        self._busy = False

    def _nudge_changed(self):
        lay = self.current_layer()
        if lay is None or self._busy:
            return
        lay.nudge = (self.sp_de.value(), self.sp_dn.value())
        self._busy = True
        self.state.refresh(("imagery",))
        self._busy = False

    def _offline(self, on):
        settings().set("offline_imagery", bool(on))
        self.state.tiles.offline = bool(on)
        self.state.tiles.clear_failed()
        self.canvas.invalidate()

    def _clear_cache(self):
        import shutil
        d = settings().tile_cache_dir
        mb = self.state.tiles.cache_size_mb()
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        self.canvas.imagery.clear()
        self.canvas.invalidate()
        self.state.log(f"Tile cache cleared ({mb:,.0f} MB).", "ok")

    def _remove(self):
        lay = self.current_layer()
        if lay is None:
            return
        with self.state.edit("Remove imagery", kinds=("imagery",)):
            del self.state.project.imagery[lay.id]

    # ------------------------------------------------------------------ source accuracy
    def _show_meta(self, meta: dict):
        """Say what the provider claims, and what that does and does not support.

        There is no tolerance number to compare it against any more - that belonged to the imagery
        checks, which are gone - so the panel states the figure and what it means in words.  The
        warning colour is kept for imagery whose own accuracy is worse than a metre, because that
        is the case where a picture is easy to trust too far.
        """
        txt = _acc_text(meta, self._unit())
        a = meta.get("accuracy_m")
        kind = "warn" if a and a > 1.0 else "info"
        if a:
            u = self._unit()
            txt += (f"\nThat is the provider's own figure.  A gap between a point and the imagery smaller than "
                    f"{a:g} m ({a / U.M_PER_UNIT.get(u, 0.3048):,.1f} {U.LABEL.get(u, u)}) says nothing about the "
                    f"survey: imagery is a secondary source.  For a precise comparison use a high-accuracy "
                    f"orthophoto (Add Imagery > Georeferenced image).")
        self.lbl_meta.set(txt, kind)

    def _lookup(self):
        pr = self.state.project
        lay = self.current_layer()
        if pr.crs.is_local:
            error_box(self, "Source Accuracy", "Assign a project coordinate system first.")
            return
        if lay is None or lay.kind != "tiles" or "Esri" not in lay.name:
            info_box(self, "Source Accuracy", "The lookup is available for Esri World Imagery. For other imagery, check the provider's documentation "
                                              "or the orthophoto's metadata.")
            return
        v = self.canvas.view
        try:
            lon, lat = pr.crs.to_lonlat(v.cx, v.cy, target=4326)
            meta = run_blocking(self, "Asking Esri about the imagery here ...", fetch_esri_metadata, float(lon), float(lat))
        except RuntimeError as ex:
            error_box(self, "Source Accuracy", "Could not reach the service (offline?).", str(ex))
            return
        lay.source["meta"] = meta
        self._busy = True
        self.state.refresh(("imagery",))
        self._busy = False
        self._show_meta(meta)








