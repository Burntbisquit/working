"""Interactive UI dialogs for point recoding and field-to-finish linework editing."""
from __future__ import annotations

import copy
import math
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import point_linework_coder as PLC
from ..core.featurecodes import CLOSE_FLAGS, parse_description
from ..core.linework_editor import resolve_vertex_point_ids
from ..core.model import Polyline, SurveyPoint
from ..core.settings import settings
from ..core.fieldbook_syntax import command_joiner, command_map
from .widgets import Hint, hline


class JoinPointsDialog(QDialog):
    """Dialog to join selected points into a coded linework string by recoding point descriptions."""

    def __init__(self, state, point_ids: Sequence[int], parent=None):
        super().__init__(parent)
        self.state = state
        self.point_ids = list(point_ids)
        self.setWindowTitle("Create Linework from Points (Recode)")
        self.resize(680, 480)
        lay = QVBoxLayout(self)

        pr = state.project
        self.commands = (pr.settings or {}).get("f2f_commands")
        self.points = [pr.points[pid] for pid in self.point_ids if pid in pr.points]

        lay.addWidget(QLabel(f"<b>Recode {len(self.points)} point(s) into a linework figure:</b>"))

        # Setup form
        form = QFormLayout()
        self.cmb_code = QComboBox()
        self.cmb_code.setEditable(True)
        # Populate with existing line codes in project
        line_codes = [c.code for c in pr.codes if c.kind in ("line", "polygon")]
        if not line_codes:
            line_codes = ["EP", "TC", "TOC", "BC", "FL", "CL", "BLDG", "FNC", "WALL", "RW"]
        self.cmb_code.addItems(sorted(set(line_codes)))
        self.cmb_code.setCurrentText("EP")
        self.cmb_code.currentTextChanged.connect(self._update_preview)
        form.addRow("Feature Code:", self.cmb_code)

        self.le_string = QLineEdit("1")
        self.le_string.setPlaceholderText("String ID (e.g. 1, 2, A)")
        self.le_string.textChanged.connect(self._update_preview)
        form.addRow("String Number:", self.le_string)

        self.chk_closed = QCheckBox("Closed Figure (e.g. Building, Pad, Pond)")
        self.chk_closed.toggled.connect(self._update_preview)
        form.addRow("", self.chk_closed)
        lay.addLayout(form)

        lay.addWidget(QLabel("<b>Point Description Preview:</b>"))
        self.tbl = QTableWidget(len(self.points), 4)
        self.tbl.setHorizontalHeaderLabels(["Point #", "Current Desc", "Role in String", "New Desc"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        lay.addWidget(self.tbl, 1)

        lay.addWidget(Hint("Modifies underlying point descriptions so that linework will reprocess identically "
                           "when ported to Carlson, Civil 3D, or other survey CAD packages."))

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        b_apply = QPushButton("Apply & Generate Linework")
        b_apply.setProperty("accent", True)
        b_apply.clicked.connect(self._apply)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btn_row.addWidget(b_apply)
        btn_row.addWidget(b_cancel)
        lay.addLayout(btn_row)

        self._update_preview()

    def _update_preview(self):
        code = self.cmb_code.currentText().strip().upper()
        str_id = self.le_string.text().strip()
        closed = self.chk_closed.isChecked()
        prefix = f"{code}{str_id}"
        command_tokens = command_map(self.commands)
        start_token = command_tokens.get("start_line", "")
        end_token = command_tokens.get("close" if closed else "end_line", "")
        n = len(self.points)

        for r, p in enumerate(self.points):
            self.tbl.setItem(r, 0, QTableWidgetItem(str(p.number)))
            self.tbl.setItem(r, 1, QTableWidgetItem(str(p.desc)))

            if r == 0:
                role = f"Start Line ({start_token})" if start_token else "Start Line"
                token = prefix + (command_joiner() + start_token if start_token else "")
            elif r == n - 1:
                label = "Close" if closed else "End Line"
                role = f"{label} ({end_token})" if end_token else label
                token = prefix + (command_joiner() + end_token if end_token else "")
            else:
                role = "Vertex"
                token = prefix

            self.tbl.setItem(r, 2, QTableWidgetItem(role))
            new_desc = PLC.update_point_token(p.desc or "", code, token, self.commands)
            self.tbl.setItem(r, 3, QTableWidgetItem(new_desc))

    def _apply(self):
        code = self.cmb_code.currentText().strip().upper()
        str_id = self.le_string.text().strip()
        closed = self.chk_closed.isChecked()
        pr = self.state.project

        with self.state.edit("Create Linework from Points", kinds=("points", "entities")):
            PLC.join_points_to_string(self.points, code, str_id, closed=closed,
                                      commands=self.commands)
            pr.touch()
        self.state.log("Linework coding saved. Use Process Linework to build the geometry.", "info")
        self.accept()


class EditLineworkCodingDialog(QDialog):
    """Interactive editor to inspect and modify point coding for an existing line string."""

    def __init__(self, state, polyline_entity: Polyline, parent=None):
        super().__init__(parent)
        self.state = state
        self.entity = polyline_entity
        self.commands = (state.project.settings or {}).get("f2f_commands")
        self.setWindowTitle(f"Edit Linework String (Polyline {polyline_entity.id})")
        self.resize(760, 520)
        lay = QVBoxLayout(self)

        pr = state.project
        # Resolve stable point identities rather than trusting potentially duplicated point numbers.
        self.point_ids, self.link_issues = resolve_vertex_point_ids(pr, polyline_entity)
        self.source_point_ids = [pid for pid in self.point_ids if pid is not None]
        self.points = [copy.deepcopy(pr.points[pid]) for pid in self.source_point_ids]
        self.can_apply = bool(self.points) and not self.link_issues

        lay.addWidget(QLabel(f"<b>Linework String: {polyline_entity.layer} ({len(self.points)} Linked Points)</b>"))

        self.tbl = QTableWidget(len(self.points), 4)
        self.tbl.setHorizontalHeaderLabels(["Point #", "Current Description", "Role", "New Description"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)

        self.edits = []
        for r, p in enumerate(self.points):
            self.tbl.setItem(r, 0, QTableWidgetItem(str(p.number)))
            self.tbl.setItem(r, 1, QTableWidgetItem(str(p.desc)))
            role = "Start" if r == 0 else ("End / Close" if r == len(self.points) - 1 else "Vertex")
            self.tbl.setItem(r, 2, QTableWidgetItem(role))
            ed = QLineEdit(p.desc)
            self.tbl.setCellWidget(r, 3, ed)
            self.edits.append((p, ed))

        lay.addWidget(self.tbl, 1)

        # Quick actions
        act_row = QHBoxLayout()
        b_rev = QPushButton("Reverse Direction")
        b_rev.setToolTip("Swap start/end codes to reverse line drawing direction")
        b_rev.clicked.connect(self._reverse)

        b_close = QPushButton("Toggle Close/Open")
        b_close.setToolTip("Switch between the Field Book's Close and End Line meanings")
        b_close.clicked.connect(self._toggle_close)

        b_recode = QPushButton("Change Code Prefix...")
        b_recode.setToolTip("Batch update the feature code prefix across all member points")
        b_recode.clicked.connect(self._change_code)

        act_row.addWidget(b_rev)
        act_row.addWidget(b_close)
        act_row.addWidget(b_recode)
        act_row.addStretch(1)
        lay.addLayout(act_row)

        for button in (b_rev, b_close, b_recode):
            button.setEnabled(self.can_apply)
        if self.link_issues:
            status = "Point coding is disabled until every line vertex has a unique point link. " + self.link_issues[0]
        elif not self.points:
            status = "No linked survey points were found for this line."
        else:
            status = "Coding changes are staged on copies; Apply & Reprocess commits one undoable edit, while Cancel discards them."
        lay.addWidget(Hint(status))

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        b_apply = QPushButton("Apply & Reprocess")
        b_apply.setProperty("accent", True)
        b_apply.setEnabled(self.can_apply)
        b_apply.clicked.connect(self._apply)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btn_row.addWidget(b_apply)
        btn_row.addWidget(b_cancel)
        lay.addLayout(btn_row)

    def _reverse(self):
        PLC.reverse_string_coding(self.points, commands=self.commands)
        self.points = self.points[::-1]
        self._refresh_table()

    def _toggle_close(self):
        if not self.points:
            return
        last = self.points[-1]
        parsed = parse_description(last.desc or "", commands=self.commands,
                                    known_codes=self.state.project.codes.codes)
        close_token = command_map(self.commands).get("close", "")
        is_closed = bool(parsed.flags & CLOSE_FLAGS) or bool(
            close_token and any(flag.casefold() == close_token.casefold() for flag in parsed.flags))
        if is_closed:
            PLC.open_string_coding(last, commands=self.commands)
        else:
            PLC.close_string_coding(last, commands=self.commands)
        self._refresh_table()

    def _change_code(self):
        if not self.can_apply or not self.points:
            return
        new_code, ok = QMessageBox.getText(self, "Change Code", "New Feature Code Prefix (e.g. EP2, TOC1):") if hasattr(QMessageBox, "getText") else (None, False)
        if not ok or not new_code:
            from PySide6.QtWidgets import QInputDialog
            new_code, ok = QInputDialog.getText(self, "Change Code", "New Feature Code Prefix (e.g. EP2, TOC1):")
            if not ok or not new_code:
                return
        segments = PLC._split_multicode(self.points[0].desc or "", self.commands)
        main = PLC._description_parts(segments[0], self.commands)[0] if segments else ""
        parsed = parse_description(main, commands=self.commands,
                                   known_codes=self.state.project.codes.codes)
        old_prefix = parsed.code + parsed.string if parsed.code else ""
        PLC.change_string_code(self.points, old_prefix, new_code.strip().upper(),
                               commands=self.commands)
        self._refresh_table()

    def _refresh_table(self):
        self.edits = []
        self.tbl.setRowCount(len(self.points))
        for r, p in enumerate(self.points):
            self.tbl.setItem(r, 0, QTableWidgetItem(str(p.number)))
            self.tbl.setItem(r, 1, QTableWidgetItem(str(p.desc)))
            role = "Start" if r == 0 else ("End / Close" if r == len(self.points) - 1 else "Vertex")
            self.tbl.setItem(r, 2, QTableWidgetItem(role))
            ed = QLineEdit(p.desc)
            self.tbl.setCellWidget(r, 3, ed)
            self.edits.append((p, ed))

    def _apply(self):
        if not self.can_apply:
            return
        pr = self.state.project
        updates = {draft.id: ed.text().strip() for draft, ed in self.edits}
        missing = [point_id for point_id in updates if point_id not in pr.points]
        if missing:
            QMessageBox.warning(
                self,
                "Linework changed",
                "One or more linked survey points no longer exist. Reopen the point coder before applying.",
            )
            return
        changes = {point_id: description for point_id, description in updates.items()
                   if pr.points[point_id].desc != description}
        if not changes:
            self.accept()
            return
        try:
            with self.state.edit("Edit Linework Coding", kinds=("points", "entities")):
                for point_id, description in changes.items():
                    pr.points[point_id].desc = description
        except Exception as ex:
            QMessageBox.warning(self, "Apply Linework Coding", str(ex))
            return
        self.state.log("Linework coding updated. Use Process Linework to rebuild derived geometry.", "info")
        self.accept()
