"""Dock widgets: Layers, Points table, Properties, Surfaces, Messages, Python console."""
from __future__ import annotations

import code
import contextlib
import html
import io
import math
import re
import time

import numpy as np
from PySide6.QtCore import (QAbstractTableModel, QItemSelection, QItemSelectionModel, QModelIndex, QSortFilterProxyModel,
                            Qt, QTimer, Signal)
from PySide6.QtGui import QAction, QColor, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QMenu,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
                               QSlider, QTableView, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from ..core import geometry as G
from ..core import provenance as PROV
from ..core import units as U
from ..core.model import Polyline, TextEntity
from ..core.settings import settings
from ..core.surface import is_stale
from . import theme
from .widgets import Hint, confirm, dspin, error_box


# ----------------------------------------------------------------------------- layers
class LayersDock(QWidget):
    current_changed = Signal(str)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.current = "0"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        # Layer State Group bar
        state_row = QHBoxLayout()
        state_row.addWidget(QLabel("State:"))
        self.cmb_state = QComboBox()
        self.cmb_state.setMinimumWidth(110)
        self.cmb_state.setToolTip("Saved Layer States - switch between saved visibility/locked setups")
        self.cmb_state.currentIndexChanged.connect(self._on_state_selected)
        state_row.addWidget(self.cmb_state, 1)

        self.b_save_state = QPushButton("Save...")
        self.b_save_state.setToolTip("Save current layer visibility, lock, and color settings as a Layer State")
        self.b_save_state.clicked.connect(self._save_state)
        state_row.addWidget(self.b_save_state)

        self.b_del_state = QPushButton("Delete")
        self.b_del_state.setToolTip("Delete selected Layer State")
        self.b_del_state.clicked.connect(self._delete_state)
        state_row.addWidget(self.b_del_state)
        lay.addLayout(state_row)

        # Table with multi-selection support (Shift + click, Ctrl + click)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["", "", "", "Layer", "Objects"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setShowGrid(False)
        self.tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._context_menu)
        h = self.tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Fixed)
        h.setSectionResizeMode(1, QHeaderView.Fixed)
        h.setSectionResizeMode(2, QHeaderView.Fixed)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        for c in (0, 1, 2):
            self.tbl.setColumnWidth(c, 28)
        self.tbl.setColumnWidth(4, 64)
        lay.addWidget(self.tbl, 1)

        # Bulk selected & general actions
        row = QHBoxLayout()
        self.b_new = QPushButton("New")
        self.b_all = QPushButton("Show all")
        self.b_iso = QPushButton("Isolate")
        self.b_purge = QPushButton("Purge empty")
        for b in (self.b_new, self.b_all, self.b_iso, self.b_purge):
            row.addWidget(b)
        lay.addLayout(row)

        sel_row = QHBoxLayout()
        self.b_sel_on = QPushButton("Show Sel")
        self.b_sel_on.setToolTip("Turn visibility ON for all selected layers")
        self.b_sel_on.clicked.connect(lambda: self._set_selected_visibility(True))
        self.b_sel_off = QPushButton("Hide Sel")
        self.b_sel_off.setToolTip("Turn visibility OFF for all selected layers")
        self.b_sel_off.clicked.connect(lambda: self._set_selected_visibility(False))
        self.b_sel_lock = QPushButton("Lock Sel")
        self.b_sel_lock.setToolTip("Lock all selected layers")
        self.b_sel_lock.clicked.connect(lambda: self._set_selected_locked(True))
        self.b_sel_unlock = QPushButton("Unlock Sel")
        self.b_sel_unlock.setToolTip("Unlock all selected layers")
        self.b_sel_unlock.clicked.connect(lambda: self._set_selected_locked(False))
        for b in (self.b_sel_on, self.b_sel_off, self.b_sel_lock, self.b_sel_unlock):
            sel_row.addWidget(b)
        lay.addLayout(sel_row)

        lay.addWidget(Hint("Click eye/lock/swatch (affects all selected layers when multi-selected). Shift/Ctrl to select."))
        self.tbl.cellClicked.connect(self._clicked)
        self.tbl.cellDoubleClicked.connect(self._dbl)
        self.b_new.clicked.connect(self._new)
        self.b_all.clicked.connect(self._show_all)
        self.b_iso.clicked.connect(self._isolate)
        self.b_purge.clicked.connect(self._purge)
        state.changed.connect(lambda k: self.refresh())
        state.project_replaced.connect(self._on_project_replaced)
        state.selection_changed.connect(lambda: None)
        self._refresh_state_list()
        self.refresh()

    def _on_project_replaced(self):
        self._refresh_state_list()
        self.refresh()

    def _refresh_state_list(self):
        pr = getattr(self.state, "project", self.state)
        settings = getattr(pr, "settings", {}) or {}
        saved_states = settings.get("layer_states", {}) if isinstance(settings, dict) else {}
        self.cmb_state.blockSignals(True)
        self.cmb_state.clear()
        self.cmb_state.addItem("(Layer States)", "")
        self.cmb_state.addItem("All Layers ON", "__ALL_ON__")
        self.cmb_state.addItem("All Layers OFF", "__ALL_OFF__")
        self.cmb_state.addItem("All Layers Unlocked", "__ALL_UNLOCKED__")
        self.cmb_state.addItem("All Layers Locked", "__ALL_LOCKED__")
        if isinstance(saved_states, dict):
            for name in sorted(saved_states.keys()):
                self.cmb_state.addItem(f"📁 {name}", name)
        self.cmb_state.blockSignals(False)

    def _save_state(self):
        name, ok = QInputDialog.getText(self, "Save Layer State", "Enter name for this Layer State:")
        name = name.strip()
        if not ok or not name:
            return
        pr = getattr(self.state, "project", self.state)
        if "layer_states" not in pr.settings or not isinstance(pr.settings["layer_states"], dict):
            pr.settings["layer_states"] = {}
        snapshot = {}
        for n, lay in pr.layers.items():
            snapshot[n] = {
                "visible": bool(lay.visible),
                "locked": bool(lay.locked),
                "color": list(lay.color),
                "linetype": str(lay.linetype),
            }
        with self.state.edit(f"Save Layer State '{name}'", kinds=("layers",)):
            pr.settings["layer_states"][name] = snapshot
        self._refresh_state_list()
        idx = self.cmb_state.findData(name)
        if idx >= 0:
            self.cmb_state.setCurrentIndex(idx)
        self.state.log(f"Layer State '{name}' saved ({len(snapshot)} layers captured).", "ok")

    def _delete_state(self):
        cur_data = self.cmb_state.currentData()
        if not cur_data or str(cur_data).startswith("__"):
            return
        name = str(cur_data)
        pr = getattr(self.state, "project", self.state)
        if confirm(self, "Delete Layer State", f"Delete saved Layer State '{name}'?", "Delete"):
            with self.state.edit(f"Delete Layer State '{name}'", kinds=("layers",)):
                pr.settings.get("layer_states", {}).pop(name, None)
            self._refresh_state_list()
            self.state.log(f"Layer State '{name}' deleted.", "info")

    def _on_state_selected(self, index):
        code = self.cmb_state.itemData(index)
        if not code:
            return
        pr = getattr(self.state, "project", self.state)
        if code == "__ALL_ON__":
            with self.state.edit("All Layers ON", kinds=("layers",)):
                for l in pr.layers.values():
                    l.visible = True
            self.state.refresh(("layers",))
            return
        if code == "__ALL_OFF__":
            with self.state.edit("All Layers OFF", kinds=("layers",)):
                for l in pr.layers.values():
                    l.visible = False
            self.state.refresh(("layers",))
            return
        if code == "__ALL_UNLOCKED__":
            with self.state.edit("All Layers Unlocked", kinds=("layers",)):
                for l in pr.layers.values():
                    l.locked = False
            self.state.refresh(("layers",))
            return
        if code == "__ALL_LOCKED__":
            with self.state.edit("All Layers Locked", kinds=("layers",)):
                for l in pr.layers.values():
                    l.locked = True
            self.state.refresh(("layers",))
            return

        saved_states = pr.settings.get("layer_states", {}) if isinstance(getattr(pr, "settings", None), dict) else {}
        st_data = saved_states.get(str(code))
        if st_data and isinstance(st_data, dict):
            with self.state.edit(f"Restore Layer State '{code}'", kinds=("layers",)):
                for n, s in st_data.items():
                    if n in pr.layers:
                        pr.layers[n].visible = bool(s.get("visible", True))
                        pr.layers[n].locked = bool(s.get("locked", False))
                        if "color" in s and isinstance(s["color"], (list, tuple)) and len(s["color"]) >= 3:
                            pr.layers[n].color = tuple(s["color"])
                        if "linetype" in s:
                            pr.layers[n].linetype = str(s["linetype"])
            self.state.refresh(("layers",))
            self.state.log(f"Restored Layer State: '{code}'.", "ok")

    def _counts(self):
        pr = getattr(self.state, "project", self.state)
        c: dict[str, int] = {}
        if hasattr(pr, "points"):
            for p in pr.points.values():
                c[p.layer] = c.get(p.layer, 0) + 1
        if hasattr(pr, "entities"):
            for e in pr.entities.values():
                c[e.layer] = c.get(e.layer, 0) + 1
        return c

    def _selected_layer_names(self) -> list[str]:
        rows = sorted({idx.row() for idx in self.tbl.selectedIndexes()})
        return [self._name(r) for r in rows if r < self.tbl.rowCount()]

    def _context_menu(self, pos):
        sel = self._selected_layer_names()
        m = QMenu(self)
        if sel:
            m.addAction(f"Show Selected ({len(sel)})", lambda: self._set_selected_visibility(True))
            m.addAction(f"Hide Selected ({len(sel)})", lambda: self._set_selected_visibility(False))
            m.addSeparator()
            m.addAction(f"Lock Selected ({len(sel)})", lambda: self._set_selected_locked(True))
            m.addAction(f"Unlock Selected ({len(sel)})", lambda: self._set_selected_locked(False))
            m.addSeparator()
            m.addAction(f"Isolate Selected ({len(sel)})", lambda: self._isolate_selected(sel))
            m.addSeparator()
        m.addAction("Select All", self.tbl.selectAll)
        m.addAction("New Layer...", self._new)
        m.addAction("Save Current as Layer State...", self._save_state)
        m.exec(self.tbl.viewport().mapToGlobal(pos))

    def _set_selected_visibility(self, visible: bool):
        sel = self._selected_layer_names()
        if not sel and self.tbl.currentRow() >= 0:
            sel = [self._name(self.tbl.currentRow())]
        if not sel:
            return
        with self.state.edit("Toggle selected layer visibility", kinds=("layers",)):
            for n in sel:
                if n in self.state.project.layers:
                    self.state.project.layers[n].visible = visible
        self.state.refresh(("layers",))

    def _set_selected_locked(self, locked: bool):
        sel = self._selected_layer_names()
        if not sel and self.tbl.currentRow() >= 0:
            sel = [self._name(self.tbl.currentRow())]
        if not sel:
            return
        with self.state.edit("Toggle selected layer lock", kinds=("layers",)):
            for n in sel:
                if n in self.state.project.layers:
                    self.state.project.layers[n].locked = locked
        self.state.refresh(("layers",))

    def _isolate_selected(self, sel_names: list[str]):
        with self.state.edit("Isolate selected layers", kinds=("layers",)):
            for n, l in self.state.project.layers.items():
                l.visible = (n in sel_names)
        self.state.refresh(("layers",))

    def refresh(self):
        from .icons import icon
        pr = self.state.project
        counts = self._counts()
        names = sorted(pr.layers, key=lambda n: (n != "0", n))
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(names))
        for r, n in enumerate(names):
            lay = pr.layers[n]
            eye = QTableWidgetItem()
            eye.setIcon(icon("eye" if lay.visible else "eye_off"))
            lk = QTableWidgetItem()
            lk.setIcon(icon("lock" if lay.locked else "unlock"))
            sw = QTableWidgetItem()
            sw.setBackground(QColor(*theme.display_color(lay.color)))
            nm = QTableWidgetItem(n)
            f = nm.font()
            f.setBold(n == self.current)
            nm.setFont(f)
            if not lay.visible:
                nm.setForeground(QColor(theme.colors()["dim"]))
            ct = QTableWidgetItem(str(counts.get(n, 0)))
            ct.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for c, it in enumerate((eye, lk, sw, nm, ct)):
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.tbl.setItem(r, c, it)
        self.tbl.blockSignals(False)

    def _name(self, row):
        item = self.tbl.item(row, 3)
        return item.text() if item else ""

    def _clicked(self, row, col):
        n = self._name(row)
        if not n or n not in self.state.project.layers:
            return
        lay = self.state.project.layers[n]
        sel_names = self._selected_layer_names()
        target_layers = sel_names if len(sel_names) > 1 and n in sel_names else [n]

        if col == 0:
            target_visible = not lay.visible
            with self.state.edit("Toggle layer visibility", kinds=("layers",)):
                for name in target_layers:
                    if name in self.state.project.layers:
                        self.state.project.layers[name].visible = target_visible
            self.state.refresh(("layers",))
        elif col == 1:
            target_locked = not lay.locked
            with self.state.edit("Toggle layer lock", kinds=("layers",)):
                for name in target_layers:
                    if name in self.state.project.layers:
                        self.state.project.layers[name].locked = target_locked
            self.state.refresh(("layers",))
        elif col == 2:
            c = QColorDialog.getColor(QColor(*lay.color), self, f"Colour of {n}")
            if c.isValid():
                with self.state.edit("Layer colour", kinds=("layers",)):
                    for name in target_layers:
                        if name in self.state.project.layers:
                            self.state.project.layers[name].color = (c.red(), c.green(), c.blue())
                self.state.refresh(("layers",))

    def _dbl(self, row, col):
        if col == 3:
            self.set_current(self._name(row))

    def set_current(self, name: str):
        self.current = name
        self.current_changed.emit(name)
        self.refresh()

    def _new(self):
        name, ok = QInputDialog.getText(self, "New layer", "Layer name:")
        name = name.strip()
        if ok and name:
            with self.state.edit("New layer", kinds=("layers",)):
                self.state.project.ensure_layer(name)
            self.set_current(name)

    def _show_all(self):
        for l in self.state.project.layers.values():
            l.visible = True
        self.state.refresh(("layers",))

    def _isolate(self):
        r = self.tbl.currentRow()
        if r < 0:
            return
        keep = self._name(r)
        for n, l in self.state.project.layers.items():
            l.visible = (n == keep)
        self.state.refresh(("layers",))

    def _purge(self):
        counts = self._counts()
        dead = [n for n in self.state.project.layers if counts.get(n, 0) == 0 and n not in ("0", "POINTS", self.current)]
        if not dead:
            self.state.log("No empty layers to purge.", "info")
            return
        if confirm(self, "Purge Layers", f"Remove {len(dead)} empty layer(s)?", "Purge"):
            with self.state.edit("Purge layers", kinds=("layers",)):
                for n in dead:
                    del self.state.project.layers[n]


# ----------------------------------------------------------------------------- field book palette
class FieldBookDock(QWidget):
    """Field Book tool palette displaying feature codes, descriptions, kinds, layers, and symbology."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        # Top search & filter
        top_row = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("Search field book codes, layers, descriptions...")
        self.ed_search.textChanged.connect(self.refresh)
        top_row.addWidget(self.ed_search)
        lay.addLayout(top_row)

        # Codes table (excludes commands and rules)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["Code", "Description", "Kind", "Layer", "Symbol"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        lay.addWidget(self.tbl, 1)

        # Bottom info row
        bot_row = QHBoxLayout()
        self.lbl_count = QLabel("0 codes")
        bot_row.addWidget(self.lbl_count)
        bot_row.addStretch(1)
        self.btn_manage = QPushButton("Field Book...")
        self.btn_manage.setToolTip("Open Field Book manager to convert, select, or report")
        self.btn_manage.clicked.connect(self._open_dialog)
        bot_row.addWidget(self.btn_manage)
        lay.addLayout(bot_row)

        state.changed.connect(lambda k: self.refresh() if "codes" in k or not k else None)
        state.project_replaced.connect(self.refresh)
        self.refresh()

    def _open_dialog(self):
        from .fieldbook_dialog import FieldBookDialog
        FieldBookDialog(self.state, self).exec()

    def refresh(self):
        pr = self.state.project
        codes = getattr(pr, "codes", {}) or {}
        codes_items = list(codes.items()) if hasattr(codes, "items") else list(getattr(codes, "codes", {}).items()) if hasattr(codes, "codes") else ([(c.code, c) for c in codes] if isinstance(codes, (list, tuple)) else [])

        query = self.ed_search.text().strip().casefold()
        filtered = []
        for code, fc in sorted(codes_items):
            if query:
                text = f"{code} {getattr(fc, 'name', '')} {getattr(fc, 'layer', '')} {getattr(fc, 'symbol', '')}".casefold()
                if query not in text:
                    continue
            filtered.append((code, fc))

        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(filtered))
        for r, (code, fc) in enumerate(filtered):
            kind_str = "Point" if fc.kind == "point" else "Line" if fc.kind == "line" else "Polygon"
            self.tbl.setItem(r, 0, QTableWidgetItem(str(code)))
            self.tbl.setItem(r, 1, QTableWidgetItem(str(getattr(fc, "name", ""))))
            self.tbl.setItem(r, 2, QTableWidgetItem(kind_str))
            self.tbl.setItem(r, 3, QTableWidgetItem(str(getattr(fc, "layer", ""))))
            self.tbl.setItem(r, 4, QTableWidgetItem(str(getattr(fc, "symbol", ""))))
        self.tbl.blockSignals(False)
        self.lbl_count.setText(f"{len(filtered):,} of {len(codes_items):,} codes")


# ----------------------------------------------------------------------------- points table
#: One line per provenance column for the menu: what it holds and where it comes from.
PROV_TIPS = {
    "source_file": "The file this point was read from - one name per crew's download",
    "source_folder": "The folder that file was in, relative to the job folder",
    "imported": "When the point landed in this job, to the minute",
    "import_set": "The import as a whole: which list, folder or pin set it was part of",
}


class PointsModel(QAbstractTableModel):
    """The point list: point number, coordinates, description, layer - plus where each point
    came from, once you ask for that column.

    The provenance columns (Source File, Parent Folder, Imported, Import) are **off until they
    are switched on** - see :class:`PointsDock` for the menu that switches them.  They are real
    columns of this model rather than a second table, so filtering, sorting and "select these"
    keep working on them: filter "crew 6" and the list shows that crew, sort by *Source File* and
    every file groups up.
    """

    FIXED = ("number", "n", "e", "z", "desc", "layer")

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.ids: list[int] = []
        self.ne = True
        self.extra: list[str] = []                          # provenance column keys, in order

    def headers(self):
        fixed = ["Pt", "Northing" if self.ne else "Easting", "Easting" if self.ne else "Northing", "Elev", "Description", "Layer"]
        return fixed + [PROV.column_label(k) for k in self.extra]

    def column_key(self, c: int) -> str:
        """The key behind a column: one of FIXED, or a provenance key."""
        return self.FIXED[c] if c < len(self.FIXED) else self.extra[c - len(self.FIXED)]

    def set_columns(self, keys):
        """Show exactly these provenance columns (the six standard ones are always shown)."""
        keys = [k for k in keys if k in dict(PROV.COLUMNS)]
        if keys == self.extra:
            return
        self.beginResetModel()
        self.extra = keys
        self.endResetModel()

    def reload(self):
        self.beginResetModel()
        self.ne = settings().get("coord_order") == "NE"
        self.ids = list(self.state.project.points.keys())
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.ids)

    def columnCount(self, parent=QModelIndex()):
        return len(self.FIXED) + len(self.extra)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.headers()[section]
        return None

    def _cols(self, p):
        a, b = (p.y, p.x) if self.ne else (p.x, p.y)
        return [p.number, a, b, p.z, p.desc, p.layer] + [PROV.value(p, k) for k in self.extra]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        p = self.state.project.points.get(self.ids[index.row()])
        if p is None:
            return None
        c = index.column()
        v = self._cols(p)[c]
        text = c >= len(self.FIXED)                          # a provenance column: text, read-only
        if role == Qt.DisplayRole:
            if text:
                return str(v)
            if c in (1, 2):
                return f"{v:,.3f}"
            if c == 3:
                return "" if math.isnan(v) else f"{v:,.3f}"
            return v
        if role == Qt.EditRole:
            if text:
                return str(v)
            if c in (1, 2, 3):
                return "" if (isinstance(v, float) and math.isnan(v)) else f"{v:.6f}".rstrip("0").rstrip(".")
            return v
        if role == Qt.TextAlignmentRole and c in (1, 2, 3):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.UserRole:                            # sort key - never a string sort for numbers
            if text:
                return str(v).lower()
            if c == 0:
                try:
                    return (0, float(v), v)
                except ValueError:
                    return (1, 0.0, v)
            if c in (1, 2, 3):
                return -1e300 if (isinstance(v, float) and math.isnan(v)) else v
            return str(v).lower()
        if role == Qt.UserRole + 1:
            return p.id
        if role == Qt.ToolTipRole and text:
            return PROV.describe(p) or None
        return None

    def flags(self, index):
        if index.column() >= len(self.FIXED):
            return Qt.ItemIsEnabled | Qt.ItemIsSelectable    # where a point came from is a fact
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or index.column() >= len(self.FIXED):
            return False
        pid = self.ids[index.row()]
        p = self.state.project.points.get(pid)
        if p is None:
            return False
        c = index.column()
        text = str(value).strip()
        try:
            with self.state.edit("Edit point"):
                p = self.state.project.points[pid]
                if c == 0:
                    if not text:
                        raise ValueError("A point needs a number.")
                    p.number = text
                elif c in (1, 2):
                    v = float(text.replace(",", ""))
                    if (c == 1) == self.ne:
                        p.y = v
                    else:
                        p.x = v
                elif c == 3:
                    p.z = math.nan if text == "" else float(text.replace(",", ""))
                elif c == 4:
                    p.desc = text
                elif c == 5:
                    if not text:
                        raise ValueError("A layer name is needed.")
                    self.state.project.ensure_layer(text)
                    p.layer = text
        except ValueError as ex:
            self.state.log(f"Edit rejected: {ex}", "warn")
            return False
        return True


class _Proxy(QSortFilterProxyModel):
    def lessThan(self, a, b):
        return (a.data(Qt.UserRole) or 0) < (b.data(Qt.UserRole) or 0)


class PointsDock(QWidget):
    """The point list, and the menu that decides which columns it shows.

    A job file carries more than coordinates: every imported point remembers the file and folder
    it came from (see :mod:`plumbline.core.provenance`).  Those columns are **off by default and
    switched on from here** - right-click the point list's headings, or *View > Panels > Point
    Columns* - and the choice is remembered between sessions, because it is a way of working
    rather than a property of the job.  Source File and Parent Folder start switched on: they are
    the two columns the field data has always carried, and after a folder import they are what
    tells one crew's download from another's.
    """

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._sync = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.ed = QLineEdit()
        self.ed.setPlaceholderText("Filter by number, description, layer ...")
        self.ed.setClearButtonEnabled(True)
        self.lbl = QLabel("")
        row.addWidget(self.ed, 1)
        row.addWidget(self.lbl)
        lay.addLayout(row)
        self.model = PointsModel(state)
        self.model.set_columns(settings().get("point_columns") or list(PROV.DEFAULT_COLUMNS))
        self.proxy = _Proxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(22)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        lay.addWidget(self.view, 1)
        # ---- the column menu: the point list's headings, and the View menu, share these actions
        self.col_menu = QMenu("Point &Columns", self)
        self.col_acts = {}
        for key, label in PROV.COLUMNS:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setChecked(key in self.model.extra)
            a.setToolTip(PROV_TIPS.get(key, ""))
            a.toggled.connect(lambda on, k=key: self.show_column(k, on))
            self.col_menu.addAction(a)
            self.col_acts[key] = a
        self.view.horizontalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.horizontalHeader().customContextMenuRequested.connect(self._header_menu)
        self.ed.textChanged.connect(self._filter)
        self.view.selectionModel().selectionChanged.connect(self._view_selection)
        self.view.clicked.connect(lambda i: None)
        self.view.activated.connect(self._zoom)
        state.changed.connect(self._changed)
        state.project_replaced.connect(self.reload)
        state.selection_changed.connect(self._state_selection)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.reload)
        self.reload()

    def _changed(self, kinds):
        self._timer.start(30)

    def show_column(self, key: str, on: bool):
        """Switch one provenance column on or off, and remember it for next time."""
        keys = [k for k in self.model.extra if k != key] + ([key] if on else [])
        keys = [k for k, _ in PROV.COLUMNS if k in keys]                # keep the standard order
        self.model.set_columns(keys)
        settings().set("point_columns", keys)
        if self.col_acts[key].isChecked() != on:
            self.col_acts[key].setChecked(on)
        self.reload()

    def _header_menu(self, pos):
        self.col_menu.exec(self.view.horizontalHeader().mapToGlobal(pos))

    def reload(self):
        self._sync = True
        self.model.reload()
        self._sync = False
        self.view.resizeColumnsToContents()
        self.view.horizontalHeader().setStretchLastSection(True)
        self.lbl.setText(f"{len(self.model.ids):,} points")
        self._state_selection()

    def _filter(self, text):
        rx = re.escape(text.strip())
        self.proxy.setFilterRegularExpression(rx)
        self.lbl.setText(f"{self.proxy.rowCount():,} of {len(self.model.ids):,}" if text.strip() else f"{len(self.model.ids):,} points")

    def _view_selection(self, *a):
        if self._sync:
            return
        ids = [self.proxy.data(i, Qt.UserRole + 1) for i in self.view.selectionModel().selectedRows()]
        self._sync = True
        self.state.select(points=ids)
        self._sync = False

    def _state_selection(self):
        if self._sync:
            return
        self._sync = True
        sel = self.state.sel_points
        sm = self.view.selectionModel()
        sm.clearSelection()
        if sel and len(sel) <= 20000:
            rows = []
            last = self.model.columnCount() - 1
            pos = {pid: r for r, pid in enumerate(self.model.ids)}
            for pid in sel:
                r = pos.get(pid)
                if r is not None:
                    pi = self.proxy.mapFromSource(self.model.index(r, 0)).row()
                    if pi >= 0:
                        rows.append(pi)
            rows.sort()
            isel = QItemSelection()
            start = prev = None
            for r in rows:
                if start is None:
                    start = prev = r
                elif r == prev + 1:
                    prev = r
                else:
                    isel.select(self.proxy.index(start, 0), self.proxy.index(prev, last))
                    start = prev = r
            if start is not None:
                isel.select(self.proxy.index(start, 0), self.proxy.index(prev, last))
            sm.select(isel, QItemSelectionModel.Select)
            if rows:
                self.view.scrollTo(self.proxy.index(rows[0], 0))
        self._sync = False

    def _zoom(self, idx):
        pid = self.proxy.data(idx, Qt.UserRole + 1)
        p = self.state.project.points.get(pid)
        if p:
            self.state.center_on(p.x, p.y)


# ----------------------------------------------------------------------------- properties
class PropertiesDock(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(8, 8, 8, 8)
        self.body = QWidget()
        self.lay.addWidget(self.body)
        self.lay.addStretch(1)
        state.selection_changed.connect(self.refresh)
        state.changed.connect(lambda k: self.refresh())
        state.project_replaced.connect(self.refresh)
        self.refresh()

    def _rebuild(self):
        self.lay.removeWidget(self.body)
        self.body.deleteLater()
        self.body = QWidget()
        self.lay.insertWidget(0, self.body)
        return QFormLayout(self.body)

    def refresh(self):
        st = self.state
        pr = st.project
        f = self._rebuild()
        f.setLabelAlignment(Qt.AlignRight)
        pts = [pr.points[i] for i in st.sel_points if i in pr.points]
        ents = [pr.entities[i] for i in st.sel_entities if i in pr.entities]
        if not pts and not ents:
            s = pr.summary()
            f.addRow(QLabel(f"<b>{html.escape(pr.name)}</b>"))
            f.addRow("Points:", QLabel(f"{s['points']:,}"))
            f.addRow("Polylines:", QLabel(f"{s['polylines']:,}"))
            f.addRow("Texts:", QLabel(f"{s['texts']:,}"))
            f.addRow("Surfaces:", QLabel(f"{s['surfaces']}"))
            if s["checks"]:
                # only worth a row when there are any: checking points against imagery was removed,
                # so these records can only have come from a project written by an older version
                f.addRow("Saved imagery checks:", QLabel(f"{s['checks']:,} <i>(from an older version)</i>"))
            ext = pr.extents()
            if ext:
                f.addRow("Extents N:", QLabel(f"{ext[1]:,.2f} to {ext[3]:,.2f}"))
                f.addRow("Extents E:", QLabel(f"{ext[0]:,.2f} to {ext[2]:,.2f}"))
            lbl = QLabel(pr.crs.label)
            lbl.setWordWrap(True)
            f.addRow("Coordinates:", lbl)
            return
        if len(pts) == 1 and not ents:
            self._point_form(f, pts[0])
        elif len(ents) == 1 and not pts:
            self._entity_form(f, ents[0])
        else:
            f.addRow(QLabel(f"<b>{len(pts)} point(s), {len(ents)} object(s) selected</b>"))
            if len(pts) >= 2 and not ents:
                b_join = QPushButton(f"Create Linework from {len(pts)} Points...")
                b_join.setToolTip("Recode selected points into a linework figure")
                def join_pts():
                    from .linework_dialog import JoinPointsDialog
                    dlg = JoinPointsDialog(self.state, [p.id for p in pts], self)
                    dlg.exec()
                b_join.clicked.connect(join_pts)
                f.addRow("", b_join)
            cmb = QComboBox()
            cmb.setEditable(True)
            cmb.addItems(sorted(pr.layers))
            cmb.setCurrentText("")
            b = QPushButton("Move to layer")
            b.clicked.connect(lambda: self._move_layer(cmb.currentText().strip()))
            f.addRow("Layer:", cmb)
            f.addRow("", b)
            zs = [p.z for p in pts if not math.isnan(p.z)]
            if zs:
                f.addRow("Elevation:", QLabel(f"{min(zs):,.3f} to {max(zs):,.3f}"))

    def _move_layer(self, name):
        if not name:
            return
        st = self.state
        with st.edit("Move to layer"):
            st.project.ensure_layer(name)
            for i in st.sel_points:
                if i in st.project.points:
                    st.project.points[i].layer = name
            for i in st.sel_entities:
                if i in st.project.entities:
                    st.project.entities[i].layer = name

    def _point_form(self, f, p):
        pr = self.state.project
        ne = settings().get("coord_order") == "NE"
        e_num, e_n, e_e = QLineEdit(p.number), QLineEdit(f"{p.y:.4f}"), QLineEdit(f"{p.x:.4f}")
        e_z = QLineEdit("" if math.isnan(p.z) else f"{p.z:.4f}")
        e_d = QLineEdit(p.desc)
        c_l = QComboBox()
        c_l.setEditable(True)
        c_l.addItems(sorted(pr.layers))
        c_l.setCurrentText(p.layer)
        f.addRow(QLabel(f"<b>Point {html.escape(p.number)}</b>"))
        f.addRow("Number:", e_num)
        if ne:
            f.addRow("Northing:", e_n)
            f.addRow("Easting:", e_e)
        else:
            f.addRow("Easting:", e_e)
            f.addRow("Northing:", e_n)
        f.addRow("Elevation:", e_z)
        f.addRow("Description:", e_d)
        f.addRow("Layer:", c_l)
        b = QPushButton("Apply changes")
        f.addRow("", b)

        if p.is_modified:
            f.addRow(QLabel("<hr style='margin:4px 0;'/>"))
            f.addRow(QLabel("<b>Original Field State:</b>"))
            f.addRow("Orig Pt #:", QLabel(html.escape(p.orig_number)))
            if ne:
                f.addRow("Orig Northing:", QLabel(f"{p.orig_y:,.4f}"))
                f.addRow("Orig Easting:", QLabel(f"{p.orig_x:,.4f}"))
            else:
                f.addRow("Orig Easting:", QLabel(f"{p.orig_x:,.4f}"))
                f.addRow("Orig Northing:", QLabel(f"{p.orig_y:,.4f}"))
            f.addRow("Orig Elev:", QLabel("" if math.isnan(p.orig_z) else f"{p.orig_z:,.4f}"))
            f.addRow("Orig Desc:", QLabel(html.escape(p.orig_desc)))
            if p.delta_xy > 0.0001:
                f.addRow("Shift (ΔXY):", QLabel(f"{p.delta_xy:,.4f}"))
            b_revert = QPushButton("Revert to Original")
            b_revert.setToolTip("Reset this point's coordinates, number, description, and layer back to baseline import state")
            def revert():
                with self.state.edit("Revert point to original"):
                    q = self.state.project.points[p.id]
                    q.revert_to_original()
            b_revert.clicked.connect(revert)
            f.addRow("", b_revert)

        def apply():
            try:
                x, y = float(e_e.text().replace(",", "")), float(e_n.text().replace(",", ""))
                z = math.nan if not e_z.text().strip() else float(e_z.text().replace(",", ""))
            except ValueError:
                error_box(self, "Point", "Coordinates and elevation must be numbers.")
                return
            with self.state.edit("Edit point"):
                q = self.state.project.points[p.id]
                q.number, q.x, q.y, q.z, q.desc = e_num.text().strip() or q.number, x, y, z, e_d.text().strip()
                ln = c_l.currentText().strip()
                if ln:
                    self.state.project.ensure_layer(ln)
                    q.layer = ln

        b.clicked.connect(apply)

    def _entity_form(self, f, e):
        pr = self.state.project
        c_l = QComboBox()
        c_l.setEditable(True)
        c_l.addItems(sorted(pr.layers))
        c_l.setCurrentText(e.layer)
        if isinstance(e, Polyline):
            u = pr.h_unit
            f.addRow(QLabel(f"<b>Polyline {e.id}</b>"))
            f.addRow("Kind:", QLabel(e.kind + (f"  (generated: {e.derived})" if e.derived else "")))
            f.addRow("Vertices:", QLabel(str(len(e.verts))))
            f.addRow("Length:", QLabel(f"{G.polyline_length(e.verts, e.bulges, e.closed):,.3f} {u}"))
            if e.closed:
                a = G.polygon_area(e.verts, e.bulges)
                f.addRow("Area:", QLabel(f"{a:,.2f} sq {u}  ({U.area_to_acres(a, u):,.4f} ac)"))
            zs = e.verts[:, 2][np.isfinite(e.verts[:, 2])]
            if len(zs):
                f.addRow("Elevation:", QLabel(f"{zs.min():,.3f} to {zs.max():,.3f}"))
            chk = QCheckBox("Closed")
            chk.setChecked(e.closed)
            f.addRow("", chk)
            f.addRow("Layer:", c_l)
            b = QPushButton("Apply changes")
            f.addRow("", b)

            b_geometry = QPushButton("Edit Line Geometry (Staged)...")
            b_geometry.setToolTip("Preview vertex and curve edits; Apply creates one undoable project change")
            def edit_geometry(checked=False, ent=e):
                from .linework_editor_dialog import LineEditorDialog
                LineEditorDialog(self.state, ent, self).exec()
            b_geometry.clicked.connect(edit_geometry)
            f.addRow("", b_geometry)

            selected_lines = [self.state.project.entities[entity_id]
                              for entity_id in sorted(self.state.sel_entities)
                              if isinstance(self.state.project.entities.get(entity_id), Polyline)]
            if len(selected_lines) > 1:
                b_join = QPushButton(f"Join {len(selected_lines)} Selected Lines...")
                b_join.setToolTip("Draft the join, inspect a preview, then accept as one undoable edit")
                def join_selected(checked=False, lines=selected_lines):
                    from .linework_editor_dialog import JoinLinesDialog
                    JoinLinesDialog(self.state, lines, self).exec()
                b_join.clicked.connect(join_selected)
                f.addRow("", b_join)

            if e.derived.startswith("linework") or (e.attrs or {}).get("points"):
                b_recode = QPushButton("Edit Linework Coding (Recode Points)...")
                b_recode.setToolTip("Inspect and modify the point descriptions defining this figure")
                def edit_coding(checked=False, ent=e):
                    from .linework_dialog import EditLineworkCodingDialog
                    dlg = EditLineworkCodingDialog(self.state, ent, self)
                    dlg.exec()
                b_recode.clicked.connect(edit_coding)
                f.addRow("", b_recode)

            def apply():
                with self.state.edit("Edit polyline"):
                    q = self.state.project.entities[e.id]
                    q.closed = chk.isChecked() and len(q.verts) >= 3
                    ln = c_l.currentText().strip()
                    if ln:
                        self.state.project.ensure_layer(ln)
                        q.layer = ln
            b.clicked.connect(apply)
        elif isinstance(e, TextEntity):
            et = QLineEdit(e.text)
            eh = dspin(e.height, 0.001, 1e6, 3)
            er = dspin(e.rotation, -360, 360, 2)
            f.addRow(QLabel(f"<b>Text {e.id}</b>"))
            f.addRow("Text:", et)
            f.addRow("Height:", eh)
            f.addRow("Rotation:", er)
            f.addRow("Layer:", c_l)
            b = QPushButton("Apply changes")
            f.addRow("", b)

            def apply():
                with self.state.edit("Edit text"):
                    q = self.state.project.entities[e.id]
                    q.text, q.height, q.rotation = et.text(), eh.value(), er.value()
                    ln = c_l.currentText().strip()
                    if ln:
                        self.state.project.ensure_layer(ln)
                        q.layer = ln
            b.clicked.connect(apply)


# ----------------------------------------------------------------------------- surfaces
class SurfacesDock(QWidget):
    new_requested = Signal()
    edit_requested = Signal(int)
    contours_requested = Signal(int)
    volumes_requested = Signal()
    profile_requested = Signal(int)
    report_requested = Signal(int)
    delete_requested = Signal(int)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.lst = QListWidget()
        lay.addWidget(self.lst, 1)
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        r1 = QHBoxLayout()
        self.b_new = QPushButton("New...")
        self.b_edit = QPushButton("Edit / Rebuild")
        self.b_del = QPushButton("Delete")
        for b in (self.b_new, self.b_edit, self.b_del):
            r1.addWidget(b)
        lay.addLayout(r1)
        r2 = QHBoxLayout()
        self.b_ctr = QPushButton("Contours...")
        self.b_vol = QPushButton("Volumes...")
        self.b_prof = QPushButton("Profile...")
        self.b_rep = QPushButton("Report...")
        for b in (self.b_ctr, self.b_vol, self.b_prof, self.b_rep):
            r2.addWidget(b)
        lay.addLayout(r2)
        form = QFormLayout()
        self.cmb_mode = QComboBox()
        for lbl, v in [("Elevation tint + shading", "elevation"), ("Hillshade only", "hillshade"), ("Slope classes", "slope"), ("Off", "off")]:
            self.cmb_mode.addItem(lbl, v)
        self.chk_edges = QCheckBox("Show TIN edges")
        self.sl_op = QSlider(Qt.Horizontal)
        self.sl_op.setRange(10, 100)
        form.addRow("Display:", self.cmb_mode)
        form.addRow("", self.chk_edges)
        form.addRow("Opacity:", self.sl_op)
        lay.addLayout(form)
        self.b_new.clicked.connect(self.new_requested.emit)
        self.b_edit.clicked.connect(lambda: self._emit(self.edit_requested))
        self.b_del.clicked.connect(lambda: self._emit(self.delete_requested))
        self.b_ctr.clicked.connect(lambda: self._emit(self.contours_requested))
        self.b_vol.clicked.connect(self.volumes_requested.emit)
        self.b_prof.clicked.connect(lambda: self._emit(self.profile_requested))
        self.b_rep.clicked.connect(lambda: self._emit(self.report_requested))
        self.lst.currentRowChanged.connect(self._selected)
        self.cmb_mode.currentIndexChanged.connect(self._style)
        self.chk_edges.toggled.connect(self._style)
        self.sl_op.valueChanged.connect(self._style)
        self._busy = False
        self._stale: dict = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._compute_stale)
        state.changed.connect(self._changed)
        state.project_replaced.connect(self.refresh)
        self.refresh()

    def _emit(self, sig):
        s = self.current()
        if s is not None:
            sig.emit(s.id)

    def current(self):
        r = self.lst.currentRow()
        if r < 0:
            return None
        return self.state.project.surfaces.get(self.lst.item(r).data(Qt.UserRole))

    def _changed(self, kinds):
        if self._busy:
            return
        self.refresh(keep=True)
        self._timer.start(500)

    def refresh(self, keep=False):
        pr = self.state.project
        cur = self.current().id if (keep and self.current() is not None) else self.state.active_surface_id
        self._busy = True
        self.lst.clear()
        for s in pr.surfaces.values():
            mark = "  ⚠ out of date" if self._stale.get(s.id) else ""
            it = QListWidgetItem(f"{s.name}   ({s.tin().n_tris:,} triangles){mark}")
            it.setData(Qt.UserRole, s.id)
            self.lst.addItem(it)
            if s.id == cur:
                self.lst.setCurrentItem(it)
        if self.lst.count() and self.lst.currentRow() < 0:
            self.lst.setCurrentRow(0)
        self._busy = False
        self._selected(self.lst.currentRow())
        en = self.lst.count() > 0
        for b in (self.b_edit, self.b_del, self.b_ctr, self.b_prof, self.b_rep, self.b_vol, self.cmb_mode, self.chk_edges, self.sl_op):
            b.setEnabled(en)

    def _compute_stale(self):
        pr = self.state.project
        changed = False
        for s in pr.surfaces.values():
            try:
                v = is_stale(pr, s)
            except Exception:
                v = False
            if self._stale.get(s.id) != v:
                self._stale[s.id] = v
                changed = True
        if changed:
            self.refresh(keep=True)

    def _selected(self, row):
        s = self.current()
        if s is None:
            self.lbl.setText("No surfaces yet. Use New... to build one from your ground shots.")
            return
        self.state.active_surface_id = s.id
        st = s.style or {}
        self._busy = True
        mode = st.get("mode") or "elevation"
        self.cmb_mode.setCurrentIndex(max(0, self.cmb_mode.findData(mode)))
        self.chk_edges.setChecked(bool(st.get("edges", False)))
        self.sl_op.setValue(int(100 * float(st.get("opacity", 0.85))))
        self._busy = False
        a = s.tin().summary()
        u = self.state.project.h_unit
        txt = (f"{a['points']:,} points, {a['triangles']:,} triangles - {a['area2d']:,.0f} sq {u} ({U.area_to_acres(a['area2d'], u):,.2f} ac)\n"
               f"Elevation {a['zmin']:,.2f} to {a['zmax']:,.2f} - mean slope {a['mean_slope_pct']:.1f}%")
        if self._stale.get(s.id):
            txt += "\nThe points or breaklines changed since this surface was built - use Edit / Rebuild."
        self.lbl.setText(txt)

    def _style(self):
        if self._busy:
            return
        s = self.current()
        if s is None:
            return
        s.style = {**(s.style or {}), "mode": self.cmb_mode.currentData(), "edges": self.chk_edges.isChecked(),
                   "opacity": self.sl_op.value() / 100.0}
        self._busy = True
        self.state.refresh(("surfaces",))
        self._busy = False


# ----------------------------------------------------------------------------- messages
class MessagesDock(QWidget):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        lay.addWidget(self.view)
        state.message.connect(self.add)
        self._n = 0

    def add(self, level: str, text: str):
        col = {"info": theme.colors()["dim"], "ok": theme.colors()["good"], "warn": theme.colors()["warn"],
               "error": theme.colors()["bad"]}.get(level, theme.colors()["text"])
        t = time.strftime("%H:%M:%S")
        self.view.append(f"<span style='color:{theme.colors()['dim']}'>{t}</span> <span style='color:{col}'>{html.escape(text)}</span>")
        self._n += 1
        if self._n > 600:
            self.view.clear()
            self._n = 0


# ----------------------------------------------------------------------------- python console
class ConsoleDock(QWidget):
    def __init__(self, state, canvas, parent=None):
        super().__init__(parent)
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        from .theme import mono_font
        f = mono_font(9.5)
        self.out.setFont(f)
        self.out.setMaximumBlockCount(3000)
        self.inp = QLineEdit()
        self.inp.setFont(f)
        self.inp.setPlaceholderText("Python - e.g.  len(project.points)   |   api.edit('x')   |   help(api)")
        lay.addWidget(self.out, 1)
        lay.addWidget(self.inp)
        self.history: list[str] = []
        self.hpos = 0
        self.buffer: list[str] = []
        from ..plugins import PluginAPI
        import math as _m
        import numpy as _np
        from ..core import cogo, geometry, units
        self.api = PluginAPI(state.project, state, log=lambda s: self.write(s + "\n"))
        self.ns = {"np": _np, "math": _m, "G": geometry, "cogo": cogo, "U": units, "state": state, "canvas": canvas,
                   "api": self.api, "project": state.project, "refresh": state.refresh}
        self.interp = code.InteractiveInterpreter(self.ns)
        self.write("Plumbline console - 'project' is the open project, 'api' the plugin API, 'refresh()' repaints.\n"
                   "Use  with api.edit('label'):  for undoable changes.\n")
        self.inp.returnPressed.connect(self.run_line)
        self.inp.installEventFilter(self)
        state.project_replaced.connect(self._project_replaced)

    def _project_replaced(self):
        self.api.project = self.state.project
        self.ns["project"] = self.state.project

    def write(self, s: str):
        self.out.moveCursor(QTextCursor.End)
        self.out.insertPlainText(s)
        self.out.moveCursor(QTextCursor.End)

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.inp and ev.type() == QEvent.KeyPress:
            if ev.key() == Qt.Key_Up and self.history:
                self.hpos = max(0, self.hpos - 1)
                self.inp.setText(self.history[self.hpos])
                return True
            if ev.key() == Qt.Key_Down and self.history:
                self.hpos = min(len(self.history), self.hpos + 1)
                self.inp.setText(self.history[self.hpos] if self.hpos < len(self.history) else "")
                return True
        return super().eventFilter(obj, ev)

    def run_line(self):
        line = self.inp.text()
        self.inp.clear()
        if line.strip():
            self.history.append(line)
        self.hpos = len(self.history)
        self.write((">>> " if not self.buffer else "... ") + line + "\n")
        self.buffer.append(line)
        src = "\n".join(self.buffer)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                more = self.interp.runsource(src, "<console>")
        except SystemExit:
            more = False
        if buf.getvalue():
            self.write(buf.getvalue())
        if not more:
            self.buffer = []
