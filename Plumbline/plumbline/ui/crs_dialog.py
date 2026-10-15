"""Coordinate-system manager UI: CRS picker, project CRS dialog (assign / reproject / datum / ground), new-project dialog."""
from __future__ import annotations

import html
import math

import numpy as np
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QRadioButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from ..core import crs as C
from ..core import units as U
from ..core.settings import settings
from ..core import vdatum as VD
from .widgets import Banner, Collapsible, Hint, dspin, error_box, run_blocking, widen_chars


def _html_details(key: str) -> str:
    try:
        d = C.describe_crs(key)
    except Exception as ex:
        return f"<p>Cannot read this coordinate system: {html.escape(str(ex))}</p>"
    e = html.escape
    rows = [("Identifier", d["authority"]), ("Type", d["type"]), ("Units", f"{d['unit_name']} ({d['unit']})"),
            ("Projection", d.get("projection") or "-"), ("Datum", d["datum"] or "-"),
            ("Ellipsoid", d.get("ellipsoid", "-")), ("Area of use", d["area"] or "-"),
            ("Axes", "; ".join(d["axes"]))]
    h = [f"<h3 style='margin-bottom:2px'>{e(d['name'])}</h3><table cellpadding='2'>"]
    for k, v in rows:
        h.append(f"<tr><td><b>{e(k)}</b></td><td>{e(str(v))}</td></tr>")
    h.append("</table>")
    if d["params"]:
        h.append("<p><b>Projection parameters</b></p><table cellpadding='2'>")
        for n, v, u in d["params"]:
            try:
                vv = f"{float(v):.10g}"
            except (TypeError, ValueError):
                vv = str(v)
            h.append(f"<tr><td>{e(str(n))}</td><td align='right'>{vv}</td><td>{e(str(u))}</td></tr>")
        h.append("</table>")
    if d.get("deprecated"):
        h.append("<p><b>Deprecated</b> - prefer a current realisation.</p>")
    return "".join(h)


def texas_2011_usft_rows() -> list[tuple[int, str]]:
    """The five Texas NAD83(2011) State Plane zones in US survey feet, ordered north to south."""
    order = {"North Central": 0, "North": 1, "Central": 2,
             "South Central": 3, "South": 4}
    rows = [(int(key), str(info["name"]))
            for key, info in C.TEXAS_ZONES.items()
            if info.get("state") == "TX" and info.get("era") == "2011"
            and info.get("units") == "USft" and isinstance(key, int)]
    return sorted(rows, key=lambda row: order.get(C.TEXAS_ZONES[row[0]].get("zone"), 9))


# ----------------------------------------------------------------------------- picker
class CRSPicker(QWidget):
    """Search + favourites + details for choosing a coordinate system."""
    changed = Signal(str)             # key such as "EPSG:2276"

    def __init__(self, parent=None, kinds=("projected",), allow_kind_toggle: bool = False):
        super().__init__(parent)
        self.kinds = tuple(kinds)
        self.suggestions: list = []
        self._key = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by name or code:  texas north central  |  2276  |  UTM 14N  |  state plane ...")
        self.search.setClearButtonEnabled(True)
        row.addWidget(self.search, 1)
        self.chk_geo = QCheckBox("Include geographic (lat/lon)")
        self.chk_geo.setVisible(allow_kind_toggle)
        row.addWidget(self.chk_geo)
        self.star = QPushButton("☆ Favorite")
        self.star.setEnabled(False)
        row.addWidget(self.star)
        lay.addLayout(row)
        split = QSplitter(Qt.Horizontal)
        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        split.addWidget(self.list)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        split.addWidget(self.detail)
        split.setSizes([360, 340])
        lay.addWidget(split, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self._timer.start(180))
        self.chk_geo.toggled.connect(self.refresh)
        self.list.currentItemChanged.connect(self._on_current)
        self.star.clicked.connect(self._toggle_fav)
        self.refresh()

    # -- data
    def set_suggestions(self, items):
        self.suggestions = list(items)
        self.refresh()

    def _kinds(self):
        return self.kinds + (("geographic",) if self.chk_geo.isChecked() and "geographic" not in self.kinds else ())

    def refresh(self):
        q = self.search.text().strip()
        self.list.blockSignals(True)
        self.list.clear()
        favs = set(settings().get("crs_favorites") or [])

        def header(text):
            it = QListWidgetItem(text)
            it.setFlags(Qt.NoItemFlags)
            f = it.font()
            f.setBold(True)
            it.setFont(f)
            self.list.addItem(it)

        def add(key, name, extra=""):
            star = "★ " if key in favs else ""
            it = QListWidgetItem(f"{star}{key}   {name}{extra}")
            it.setData(Qt.UserRole, key)
            self.list.addItem(it)

        if not q:
            if self.suggestions:
                header("Suggested for your data")
                for key, name in self.suggestions:
                    add(key, name)
            header("Favorites")
            for key, name in C.default_favorites_records():
                try:
                    kind = "geographic" if C.resolve_crs(key).is_geographic else "projected"
                except Exception:
                    continue
                if kind in self._kinds():
                    add(key, name)
            header("Type to search the full EPSG / ESRI catalogue")
        else:
            # Texas first.  The state's systems are the ones a Texas surveyor is looking for,
            # and three of them (the international-foot realisations) have no EPSG code at all,
            # so they cannot come back from a catalogue search - this list is the only way to
            # reach them by typing.  Searching "nad27" or "international feet" finds them.
            seen = set()
            tx = C.texas_matches(q)
            if tx:
                header("Texas State Plane")
                for r in tx[:15]:
                    add(r.key, r.name, f"   - {r.area}" if r.area else "")
                    seen.add(r.key)
            res = C.search_crs(q, kinds=self._kinds(), limit=300)
            rest = [r for r in res if r.key not in seen]
            if rest and tx:
                header("Everywhere else (EPSG / ESRI catalogue)")
            for r in rest:
                add(r.key, r.name, f"   - {r.area}" if r.area else "")
            if not res and not tx:
                header("No matches")
        self.list.blockSignals(False)
        # keep the selection if still present
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == self._key:
                self.list.setCurrentRow(i)
                break

    def _on_current(self, cur, _prev):
        key = cur.data(Qt.UserRole) if cur else None
        if not key:
            return
        self._key = key
        self.detail.setHtml(_html_details(key))
        favs = set(settings().get("crs_favorites") or [])
        self.star.setEnabled(True)
        self.star.setText("★ Remove favorite" if key in favs else "☆ Add favorite")
        self.changed.emit(key)

    def _toggle_fav(self):
        if not self._key:
            return
        favs = list(settings().get("crs_favorites") or [])
        if self._key in favs:
            favs.remove(self._key)
        else:
            favs.append(self._key)
        settings().set("crs_favorites", favs)
        cur = self._key
        self.refresh()
        self.star.setText("★ Remove favorite" if cur in favs else "☆ Add favorite")

    def current_key(self) -> str:
        return self._key

    def select_key(self, key: str):
        self._key = key
        self.search.setText(key.split(":")[-1])
        self.refresh()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == key:
                self.list.setCurrentRow(i)
                return

    def clear_selection(self):
        """Clear the current search result without changing its query text."""
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        self._key = ""
        self.detail.clear()
        self.star.setEnabled(False)
        self.star.setText("☆ Favorite")


# ----------------------------------------------------------------------------- project CRS dialog
class CRSDialog(QDialog):
    """Coordinate System, Vertical Datum, Surface Adjustment Factor, Coordinate Calculator.

Four tabs, and one thing deliberately missing: there is **no datum-transformation tab**.  A
datum shift is a step inside a reprojection, not a setting of its own - PROJ runs the best
operation it can, the stated accuracy comes with the result, and the decisions that are the
user's (which system, and assign or reproject) are the ones the dialog asks for.
"""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.project = state.project
        self.setWindowTitle("Project Coordinate System")
        self.resize(980, 720)
        self.result_crs: C.ProjectCRS | None = None
        self.result_mode = "assign"
        cur = self.project.crs
        self._cur = cur
        root = QVBoxLayout(self)
        self.banner_current = Banner(f"Current: {cur.label.replace(chr(10), '   |   ')}", "info")
        root.addWidget(self.banner_current)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---- tab 1: coordinate system
        t1 = QWidget()
        l1 = QVBoxLayout(t1)
        quick = QGroupBox("Texas State Plane — NAD83 (2011), US survey feet")
        quick_layout = QFormLayout(quick)
        self.cmb_texas = QComboBox()
        for key, label in texas_2011_usft_rows():
            self.cmb_texas.addItem(f"EPSG:{key}   {label}", key)
        default_index = self.cmb_texas.findData(C.TEXAS_DEFAULT_EPSG)
        self.cmb_texas.setCurrentIndex(max(0, default_index))
        quick_layout.addRow("Zone:", self.cmb_texas)
        l1.addWidget(quick)
        self.search_area = Collapsible("Search other coordinate systems...", expanded=False,
                                       tooltip="The full projected CRS catalogue is searchable here; it is collapsed by default.")
        search_layout = self.search_area.body_layout()
        self.picker = CRSPicker(kinds=("projected",))
        self.picker.setMinimumHeight(220)
        search_layout.addWidget(self.picker, 1)
        l1.addWidget(self.search_area, 1)
        self._selected_key = f"EPSG:{self.cmb_texas.currentData()}"
        self.fit = Banner("", "info")
        l1.addWidget(self.fit)
        modes = QGroupBox("What Should Happen to the Coordinates in This Project?")
        ml = QVBoxLayout(modes)
        self.r_assign = QRadioButton("Assign - my numbers ALREADY are in this system; just label them (nothing moves)")
        self.r_reproj = QRadioButton("Reproject - convert my coordinates from the current system into this one")
        ml.addWidget(self.r_assign)
        ml.addWidget(self.r_reproj)
        l1.addWidget(modes)
        self.chk_net = QCheckBox("Allow Downloads (datum / geoid grids from cdn.proj.org when a conversion needs them)")
        self.chk_net.setToolTip("PROJ downloads a shift grid the first time it is needed and caches it.\n"
                                "Off, a conversion that needs a missing grid stops and says so instead of\n"
                                "quietly doing nothing - which is the right behaviour on a job site with no signal.")
        self.chk_net.setChecked(bool(settings().get("proj_network")))
        l1.addWidget(self.chk_net)
        self.btn_legacy = QPushButton("")
        self.btn_legacy.setVisible(False)
        self.btn_legacy.clicked.connect(self._use_2011)
        l1.addWidget(self.btn_legacy)
        self.tabs.addTab(t1, "Coordinate System")

        # ---- tab 2: vertical datum and geoid model
        t2 = QWidget()
        l2 = QVBoxLayout(t2)
        f2 = QFormLayout()
        self.cmb_v = QComboBox()
        for u in U.LINEAR_CHOICES:
            self.cmb_v.addItem(U.LABEL[u], u)
        self.cmb_v.setCurrentIndex(max(0, self.cmb_v.findData(cur.vunit)))
        self.cmb_vdatum = QComboBox()
        for d in VD.VERTICAL_DATUMS:
            self.cmb_vdatum.addItem(d.name, d.key)
        # An old free-text label is resolved to a real datum when it names one
        # ("NAVD88 (GEOID18)" -> NAVD88 + GEOID18); anything else is kept as written.
        vd_key, vd_geoid, vd_note = VD.resolve_label(cur.vdatum or VD.DEFAULT_DATUM)
        if VD.datum(vd_key) is None:
            self.cmb_vdatum.addItem(f"{vd_key}  (as saved in this project)", vd_key)
        self.cmb_vdatum.setCurrentIndex(max(0, self.cmb_vdatum.findData(vd_key)))
        self.ed_vdatum = self.cmb_vdatum                     # same object: the datum IS a combo now
        self.cmb_geoid = QComboBox()
        f2.addRow("Elevation Unit:", self.cmb_v)
        f2.addRow("Vertical Datum:", self.cmb_vdatum)
        f2.addRow("Geoid Model:", self.cmb_geoid)
        widen_chars(self.cmb_vdatum, 46)
        l2.addLayout(f2)
        self.lbl_vdatum = QLabel("")
        self.lbl_vdatum.setWordWrap(True)
        l2.addWidget(self.lbl_vdatum)
        self.lbl_geoid = QLabel("")
        self.lbl_geoid.setWordWrap(True)
        l2.addWidget(self.lbl_geoid)

        self.adv_v = Collapsible("Advanced - height units, ellipsoid and ellipsoid height notes",
                                 tooltip="What the numbers are, and what changes when the elevation unit changes")
        av = self.adv_v.body_layout("v")
        av.addWidget(Hint("Heights are never pushed through a horizontal datum shift - an orthometric height is not a "
                          "latitude.  Changing the elevation unit relabels; Reproject converts elevations when the "
                          "horizontal unit changes (e.g. US ft to metres).\n"
                          "Orthometric (NAVD88) = ellipsoid height - geoid separation N.  In Texas N is about -26 m, "
                          "so an ellipsoid height is LOWER than the published elevation of the same point.\n"
                          "There is deliberately no height calculator here: to move a job between orthometric and "
                          "ellipsoid heights, say what the elevations are (this tab) and use the reprojection tool - "
                          "one path, recorded, instead of two that can disagree."))
        l2.addWidget(self.adv_v)
        l2.addStretch(1)
        self.tabs.addTab(t2, "Vertical Datum")

        # ---- tab 4: Surface Adjustment Factor (ground scale)
        t4 = QWidget()
        l4 = QVBoxLayout(t4)
        g = self.project.crs.ground
        self.chk_ground = QCheckBox("Use Ground Coordinates (scale grid coordinates by a Surface Adjustment Factor)")
        self.chk_ground.setChecked(g.enabled)
        l4.addWidget(self.chk_ground)
        gl = QFormLayout()
        self.sp_bx = widen_chars(dspin(g.base_x, -1e12, 1e12, 3), 18)
        self.sp_by = widen_chars(dspin(g.base_y, -1e12, 1e12, 3), 18)
        self.sp_bx.setToolTip("Base point the SAF scales about - the TXDOT convention is the projection "
                              "origin (0, 0).  A ground coordinate is  base + (grid - base) x SAF.\n"
                              "Entered as Easting (X) only when Use Ground Coordinates is ticked, because "
                              "without ground coordinates there is no base point to scale about.")
        # The box only accepts a factor within 10% of 1.0.  Real state-plane factors are within a
        # few parts per million of 1.0, so anything past 10% is a misplaced decimal point or a
        # reciprocal typed into the wrong box - and either one would put every distance on the job
        # out by that much, with nothing on screen to show it.
        # 18 characters, not 14: a factor is read exactly, and the user asked for four more
        # characters of room in the SAF box than it used to have.
        # 11 decimals, not 8: a SAF is a number somebody read off a TXDOT sheet and types in full -
        # 1.000136506 is a real factor everywhere west of the Blackland Prairie, and a box that
        # silently truncates it to 1.00013650 changes every distance on the job by 6 parts in a
        # billion with nothing on screen to say so.
        self.sp_cf = widen_chars(dspin(g.saf if g.saf else 1.0, 0.9, 1.1, 11, 0.000001), 18)
        self.sp_cf.setToolTip("SAF = ground distance / grid distance.  Always slightly ABOVE 1.0 in Texas.\n"
                              "TXDOT county factors are typically 1.00010 - 1.00025.\n"
                              "Type the whole number - it is a scale, and 1.00017 is not 1.0001.\n"
                              "The box accepts 0.9 - 1.1 only: a real factor is within a few parts per\n"
                              "million of 1.0, so anything further out is a typo, not a factor.")
        # Northing above Easting: "N then E" is how a surveyor reads a coordinate, and the typed
        # coordinate boxes elsewhere in the program take the same order.
        gl.addRow("Base Northing:", self.sp_by)
        gl.addRow("Base Easting:", self.sp_bx)
        gl.addRow("Surface Adjustment Factor (ground / grid):", self.sp_cf)
        l4.addLayout(gl)
        # Nothing to type until ground coordinates are actually switched on: a SAF typed while the
        # box above is unticked would do nothing at all, which is worse than being greyed out.
        for _w in (self.sp_bx, self.sp_by, self.sp_cf):
            _w.setEnabled(g.enabled)
        self.chk_ground.toggled.connect(lambda on: [w.setEnabled(bool(on)) for w in (self.sp_bx, self.sp_by, self.sp_cf)])
        l4.addWidget(Hint("SAF is GROUND OVER GRID - the reciprocal of the combined factor, so 1 / 0.99988 = 1.00012. "
                          "Type the whole factor: the box keeps 11 decimals, and 1.000136506 is not 1.00013650. "
                          "Coordinates scale about the base point above from the projection origin (0,0) as TXDOT does. "
                          "The base point and the SAF are only entered when Use Ground Coordinates is ticked - "
                          "with the box clear, this job is grid and there is nothing to scale. "
                          "Reproject converts existing coordinates between grid and ground; Assign only relabels."))
        self.adv_saf = Collapsible("Advanced - compute the SAF from the location and height",
                                   tooltip="Grid scale factor x elevation factor, the long way round, if you want to check a factor")
        al = self.adv_saf.body_layout("grid")
        self.sp_h = dspin(0.0, -1000, 100000, 3)
        self.cmb_hu = QComboBox()
        for u in U.LINEAR_CHOICES:
            self.cmb_hu.addItem(U.LABEL[u], u)
        self.cmb_hu.setCurrentIndex(max(0, self.cmb_hu.findData(cur.vunit)))
        self.btn_center = QPushButton("Use the Centre of the Data as the Base Point")
        self.btn_calc = QPushButton("Compute SAF")
        al.addWidget(QLabel("Average ellipsoid height:"), 0, 0)
        al.addWidget(self.sp_h, 0, 1)
        al.addWidget(self.cmb_hu, 0, 2)
        al.addWidget(self.btn_center, 1, 0, 1, 2)
        al.addWidget(self.btn_calc, 1, 2)
        self.lbl_cf = QLabel("")
        self.lbl_cf.setWordWrap(True)
        al.addWidget(self.lbl_cf, 2, 0, 1, 3)
        al.addWidget(Hint("Ellipsoid height = orthometric (published) elevation + geoid height. Using the orthometric "
                          "elevation is a common approximation - the error is usually well under 1 ppm in Texas. "
                          "The Vertical Datum tab will give you the geoid separation properly if you want it."),
                     3, 0, 1, 3)
        l4.addWidget(self.adv_saf)
        l4.addStretch(1)
        self.tabs.addTab(t4, "Surface Adjustment Factor (Ground Scale)")

        # ---- tab 5: calculator
        t5 = QWidget()
        l5 = QVBoxLayout(t5)
        row = QHBoxLayout()
        self.ed_e = QLineEdit()
        self.ed_n = QLineEdit()
        self.ed_e.setPlaceholderText("Easting / X")
        self.ed_n.setPlaceholderText("Northing / Y")
        b1 = QPushButton("Project coords to lat/lon")
        row.addWidget(self.ed_e)
        row.addWidget(self.ed_n)
        row.addWidget(b1)
        l5.addLayout(row)
        row2 = QHBoxLayout()
        self.ed_lat = QLineEdit()
        self.ed_lon = QLineEdit()
        self.ed_lat.setPlaceholderText("Latitude (decimal)")
        self.ed_lon.setPlaceholderText("Longitude (decimal)")
        b2 = QPushButton("Lat/lon to project coords")
        row2.addWidget(self.ed_lat)
        row2.addWidget(self.ed_lon)
        row2.addWidget(b2)
        l5.addLayout(row2)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        l5.addWidget(self.out, 1)
        row3 = QHBoxLayout()
        self.btn_copy = QPushButton("Copy 'lat, lon' (paste into Google Earth / Maps search)")
        row3.addWidget(self.btn_copy)
        row3.addStretch(1)
        l5.addLayout(row3)
        self.tabs.addTab(t5, "Coordinate Calculator")

        # ---- buttons
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Apply")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        # ---- wiring
        self.picker.changed.connect(self._candidate_changed)
        self.cmb_texas.currentIndexChanged.connect(self._quick_crs_changed)
        self.r_assign.toggled.connect(self._mode_changed)
        self.chk_net.toggled.connect(self._net_toggled)
        self.cmb_vdatum.currentIndexChanged.connect(self._vdatum_changed)
        self.btn_center.clicked.connect(self._use_center)
        self.btn_calc.clicked.connect(self._compute_cf)
        b1.clicked.connect(self._calc_to_ll)
        b2.clicked.connect(self._calc_from_ll)
        self.btn_copy.clicked.connect(self._copy_ll)
        self._last_ll = None
        self._strategy = cur.strategy
        self._init_done = False
        self._v_touched = False
        self.cmb_v.activated.connect(lambda _i: setattr(self, "_v_touched", True))
        self._init_state()
        self.cmb_v.setCurrentIndex(max(0, self.cmb_v.findData(cur.vunit)))
        self._init_done = True

    # ------------------------------------------------------------------ setup
    def _init_state(self):
        pr = self.project
        cur = pr.crs
        ext = pr.extents()
        sug = []
        if ext:
            cx, cy = (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2
            if cur.is_local:
                favs = [k for k, _ in C.default_favorites_records()]
                try:
                    sug = [(k, n) for k, n, _, _ in C.suggest_crs(cx, cy, favs)]
                except Exception:
                    sug = []
        self.picker.set_suggestions(sug)
        if cur.is_local:
            self.r_assign.setChecked(True)
            self.r_reproj.setEnabled(False)
            self.r_reproj.setToolTip("A local project has no position on the earth, so there is nothing to convert from. Assign a CRS instead.")
            self.fit.set("This project uses local coordinates. Choose the Texas zone that matches your numbers, or search for another system.", "info")
            self._selected_key = f"EPSG:{C.TEXAS_DEFAULT_EPSG}"
            self.cmb_texas.setCurrentIndex(max(0, self.cmb_texas.findData(C.TEXAS_DEFAULT_EPSG)))
        else:
            self.r_assign.setChecked(True)
            if C.is_derived_key(cur.key):
                current_key = f"PLUMBLINE:{cur.key}"
            elif cur.authority.startswith(("EPSG:", "ESRI:")):
                current_key = cur.authority
            else:
                current_key = ""
            tail = current_key.split(":")[-1]
            quick_value = C.normal_key(tail) if tail else ""
            quick_index = self.cmb_texas.findData(quick_value)
            if quick_index >= 0:
                self.cmb_texas.setCurrentIndex(quick_index)
                self._selected_key = f"EPSG:{self.cmb_texas.itemData(quick_index)}"
            elif current_key:
                self.cmb_texas.blockSignals(True)
                self.cmb_texas.setCurrentIndex(-1)
                self.cmb_texas.blockSignals(False)
                self._selected_key = current_key
                self.picker.select_key(current_key)
            else:
                self.cmb_texas.blockSignals(True)
                self.cmb_texas.setCurrentIndex(-1)
                self.cmb_texas.blockSignals(False)
                self._selected_key = ""
        if cur.is_local:
            self.tabs.setTabEnabled(3, False)              # no ground scale without a projection
        self._strategy = "auto"                            # reprojection only - see the module note
        # older Texas realisations: offer the 2011 equivalent, never apply it silently
        repl = cur.legacy_replacement() if hasattr(cur, "legacy_replacement") else None
        if repl:
            self.btn_legacy.setText(f"This is a pre-2011 zone.  Use the NAD83(2011) equivalent (EPSG:{repl})")
            self.btn_legacy.setVisible(True)
        else:
            self.btn_legacy.setVisible(False)
        self._fill_geoids()

    # ------------------------------------------------------------------ candidate / fit
    def _candidate(self) -> C.ProjectCRS | None:
        key = self._selected_key
        if not key:
            return None
        try:
            cand = C.ProjectCRS.from_key(key)      # EPSG:6584, PLUMBLINE:6584-ft, ESRI:..., WKT
        except Exception:
            return None
        cand.vdatum = self.cmb_vdatum.currentData() or cand.vdatum
        cand.geoid = self.cmb_geoid.currentData() or ""
        cand.vunit = self.cmb_v.currentData() or cand.vunit
        return cand

    def _set_candidate_vunit(self, key: str):
        # Unless the user chose an elevation unit themselves, heights follow the selected system.
        if self._init_done and not self._v_touched:
            try:
                i = self.cmb_v.findData(C.describe_crs(key)["unit"])
                if i >= 0:
                    self.cmb_v.setCurrentIndex(i)
            except Exception:
                pass

    def _candidate_changed(self, key: str):
        if not key:
            return
        self._selected_key = key
        self.cmb_texas.blockSignals(True)
        self.cmb_texas.setCurrentIndex(-1)
        self.cmb_texas.blockSignals(False)
        self._set_candidate_vunit(key)
        self._update_fit()

    def _quick_crs_changed(self, index: int):
        if index < 0:
            return
        code = self.cmb_texas.itemData(index)
        if code is None:
            return
        self._selected_key = f"EPSG:{code}"
        self.picker.clear_selection()
        self._set_candidate_vunit(self._selected_key)
        self._update_fit()

    def _mode_changed(self):
        self._update_fit()

    def _update_fit(self):
        """Does the data actually fall inside the chosen system's area of use?

        The usual symptoms of a wrong zone, wrong units or swapped N/E all show up here, and
        this is the cheapest place to catch them - before a coordinate is converted.
        """
        cand = self._candidate()
        if cand is None:
            return
        pr = self.project
        if not pr.points:
            self.fit.set("", "info")
            return
        pts = list(pr.points.values())
        step = max(1, len(pts) // 400)
        x = np.array([p.x for p in pts[::step]])
        y = np.array([p.y for p in pts[::step]])
        try:
            if self.r_assign.isChecked():
                ok, tot = cand.area_of_use_ok(x, y)
                if ok == tot:
                    self.fit.set(f"All {tot} sampled points fall inside the area of use of this coordinate system - plausible.", "info")
                elif ok == 0:
                    self.fit.set(f"None of {tot} sampled points fall inside this coordinate system's area of use - probably the "
                                 f"wrong system (or wrong units / swapped N-E).", "bad")
                else:
                    self.fit.set(f"Only {ok} of {tot} sampled points fall inside this coordinate system's area of use.", "warn")
            else:
                self.fit.set("Coordinates will be converted by reprojection (see Assign / Reproject above).", "info")
        except Exception as ex:
            self.fit.set(f"Could not test the fit: {ex}", "warn")

    # ------------------------------------------------------------------ vertical datum
    def _fill_geoids(self):
        """The geoid list is per datum: an ellipsoid or assumed datum needs no geoid."""
        cur = self.project.crs
        want = self.cmb_vdatum.currentData() or cur.vdatum or VD.DEFAULT_DATUM
        resolved, label_geoid, _note = VD.resolve_label(want)
        want = resolved
        self.cmb_geoid.blockSignals(True)
        self.cmb_geoid.clear()
        for key, label in VD.geoid_choices(resolved):
            self.cmb_geoid.addItem(label, key)
        d = VD.datum(want)
        saved = cur.geoid or label_geoid or (VD.DEFAULT_GEOID if (d and d.kind == "orthometric") else "")
        i = self.cmb_geoid.findData(saved)
        self.cmb_geoid.setCurrentIndex(i if i >= 0 else 0)
        self.cmb_geoid.blockSignals(False)
        self.cmb_geoid.setEnabled(bool(self.cmb_geoid.currentData()))
        self._update_vdatum_note()

    def _vdatum_changed(self):
        self._fill_geoids()

    def _geoid_note(self):
        """Re-read the geoid/vertical-datum state (the downloads switch changes it)."""
        self._update_vdatum_note()

    def _update_vdatum_note(self):
        d = VD.describe(self.cmb_vdatum.currentData(), self.cmb_geoid.currentData() or "")
        self.lbl_vdatum.setText(f"<b>{html.escape(d['label'])}</b> - {html.escape(d['note'])}")
        if not d["needs_geoid"]:
            if d["kind"] == "tidal":
                # NGVD29 is not tied to the ellipsoid by a geoid at all - it is tied to NAVD88
                # by VERTCON, and NAVD88 is what carries the geoid.  Say so, or "no geoid
                # needed" reads as "ellipsoid height = NGVD29 + 0", which is 26 m out in Texas.
                self.lbl_geoid.setText("No geoid model is chosen for this datum: NGVD29 is related to "
                                       "NAVD88 by VERTCON, not to the ellipsoid.  This dialog no longer "
                                       "converts heights - set the datum here, then move the heights with "
                                       "the Reproject option in Survey > Project Coordinate System, which records "
                                       "what it did.")
            else:
                self.lbl_geoid.setText("No geoid model is needed for this datum.")
            return
        st = VD.grid_state(d["geoid"]) if d["geoid"] else {"present": False, "reason": "no model chosen"}
        if st.get("present"):
            n = VD.separation(-98.5, 32.0, d["geoid"]) if d["geoid"] else None
            where = f"  At a Texas test point: N = {n:+.3f} m." if n is not None else ""
            self.lbl_geoid.setText(f"{d['geoid_name']}: <b>installed</b>.  Stated accuracy "
                                   f"+/-{d['geoid_accuracy_m']:.3f} m on the model's own sheet.{where}")
        else:
            self.lbl_geoid.setText(f"{d['geoid_name']}: <b>not installed</b> - {html.escape(str(st.get('reason', '')))}."
                                   f"  Turn on Allow Downloads on the Coordinate System tab, or copy the grid into PROJ's "
                                   f"data folder by hand.  Heights keep working either way; only the geoid conversion needs it.")

    def _geoid_key(self) -> str:
        return self.cmb_geoid.currentData() or ""

    def _use_2011(self):
        """Swap a pre-2011 zone for its NAD83(2011) equivalent, in the same units."""
        repl = self.project.crs.legacy_replacement()
        if not repl:
            return
        key = f"PLUMBLINE:{repl}" if str(repl).endswith("-ft") else f"EPSG:{repl}"
        self.search_area.button.setChecked(True)
        self.picker.select_key(key)
        self.r_assign.setChecked(True)
        self.btn_legacy.setVisible(False)
        self.fit.set(f"Chosen: {C.ProjectCRS.from_key(repl).name}.  Assign keeps your coordinates as they are and labels "
                     f"them with the 2011 realisation; Reproject converts them.", "info")

    def _net_toggled(self, on):
        settings().set("proj_network", bool(on))
        C.set_proj_network(bool(on))
        VD.allow_downloads(bool(on))                 # geoid / VERTCON use the same switch
        if getattr(self, "cmb_geoid", None) is not None:
            self._update_vdatum_note()

    def _use_center(self):
        ext = self.project.extents()
        if ext:
            self.sp_bx.setValue((ext[0] + ext[2]) / 2)
            self.sp_by.setValue((ext[1] + ext[3]) / 2)
            self.chk_ground.setChecked(True)

    def _compute_cf(self):
        cand = self._candidate() or self.project.crs
        base = C.ProjectCRS(cand.crs) if isinstance(cand, C.ProjectCRS) else cand
        try:
            lon, lat = base.to_lonlat(self.sp_bx.value(), self.sp_by.value())
            h_m = self.sp_h.value() * U.M_PER_UNIT[self.cmb_hu.currentData()]
            f = C.combined_factor(base.crs, float(lon), float(lat), h_m)
        except Exception as ex:
            error_box(self, "Combined Factor", f"Could not compute: {ex}")
            return
        self.sp_cf.setValue(1.0 / f["combined"])
        self.chk_ground.setChecked(True)
        self.lbl_cf.setText(f"Grid scale factor {f['grid_factor']:.8f}  x  elevation factor {f['elevation_factor']:.8f}  "
                            f"=  combined {f['combined']:.8f}  ->  SAF {1.0 / f['combined']:.8f}.\n"
                            f"1000 grid units = {1000.0 / f['combined']:.4f} ground units.")

    def _calc_crs(self):
        cand = self._candidate()
        return cand if (cand is not None and not self.project.crs.is_local and False) else (cand or self.project.crs)

    def _calc_to_ll(self):
        try:
            crs = self.project.crs
            if crs.is_local:
                crs = self._candidate()
                if crs is None:
                    raise ValueError("Pick a coordinate system on the first tab first.")
            lon, lat = crs.to_lonlat(float(self.ed_e.text().replace(",", "")), float(self.ed_n.text().replace(",", "")))
            self._show_ll(crs, float(lon), float(lat), float(self.ed_e.text().replace(",", "")), float(self.ed_n.text().replace(",", "")))
        except Exception as ex:
            self.out.setPlainText(f"Error: {ex}")

    def _calc_from_ll(self):
        try:
            crs = self.project.crs if not self.project.crs.is_local else self._candidate()
            if crs is None:
                raise ValueError("Pick a coordinate system on the first tab first.")
            lon, lat = float(self.ed_lon.text()), float(self.ed_lat.text())
            x, y = crs.from_lonlat(lon, lat)
            self._show_ll(crs, lon, lat, float(x), float(y))
            self.ed_e.setText(f"{float(x):.4f}")
            self.ed_n.setText(f"{float(y):.4f}")
        except Exception as ex:
            self.out.setPlainText(f"Error: {ex}")

    def _show_ll(self, crs, lon, lat, x, y):
        self._last_ll = (lat, lon)
        u = U.LABEL.get(crs.unit, crs.unit)
        lines = [f"Project coordinates:  E {x:,.4f}   N {y:,.4f}  ({u})",
                 f"Latitude / longitude: {lat:.9f}, {lon:.9f}",
                 f"                      {C.format_lonlat(lon, lat, dms=True)}"]
        try:
            lines.append(f"Grid convergence:     {crs.convergence_at(x, y):.5f} deg")
            f = C.combined_factor(crs.crs, lon, lat, 0.0)
            lines.append(f"Grid scale factor:    {f['grid_factor']:.8f}   (elevation factor at height 0: {f['elevation_factor']:.8f})")
        except Exception:
            pass
        lines.append(f"Google Maps:          https://www.google.com/maps?q={lat:.7f},{lon:.7f}")
        self.out.setPlainText("\n".join(lines))

    def _copy_ll(self):
        if self._last_ll:
            QGuiApplication.clipboard().setText(f"{self._last_ll[0]:.8f}, {self._last_ll[1]:.8f}")

    # ------------------------------------------------------------------ accept
    def _build_result(self) -> C.ProjectCRS | None:
        cur = self.project.crs
        key = self._selected_key
        cand = self._candidate() if key else None
        if cand is not None:
            crs, chosen_key = cand.crs, cand.key
        elif not cur.is_local:
            crs, chosen_key = cur.crs, cur.key
        else:
            return None
        ground = C.SurfaceAdjustmentFactor(self.chk_ground.isChecked(), self.sp_bx.value(), self.sp_by.value(),
                                           self.sp_cf.value()) \
            if self.chk_ground.isEnabled() else C.SurfaceAdjustmentFactor()
        return C.ProjectCRS(crs, ground, self.cmb_v.currentData(),
                            self.cmb_vdatum.currentData() or "",
                            self._strategy, self.cmb_geoid.currentData() or "", chosen_key)

    def _accept(self):
        new = self._build_result()
        if new is None:
            error_box(self, "Coordinate System", "Choose a coordinate system from the list first.")
            self.tabs.setCurrentIndex(0)
            return
        cur = self.project.crs
        same_crs = (not cur.is_local) and new.crs == cur.crs
        mode = "assign" if self.r_assign.isChecked() else "reproject"
        if cur.is_local:
            mode = "assign"
        if same_crs:
            if self.chk_ground.isChecked() != cur.ground.enabled or abs(self.sp_cf.value() - cur.ground.saf) > 1e-12 \
                    or (self.chk_ground.isChecked() and (abs(self.sp_bx.value() - cur.ground.base_x) > 1e-9 or abs(self.sp_by.value() - cur.ground.base_y) > 1e-9)):
                pass          # mode chosen by the user decides: relabel or convert grid<->ground
            else:
                mode = "assign"
        self.result_crs, self.result_mode = new, mode
        self.accept()
