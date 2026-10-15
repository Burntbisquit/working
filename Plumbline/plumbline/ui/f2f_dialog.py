"""Survey > Convert Field to Finish - the conversion dialog.

The engine is :mod:`plumbline.io.f2f`; this is the part that asks.  It is written around one
idea: **Carlson's own layout is the answer**, so the default path is a single click - the format
radio is already on Carlson, every code is already ticked, and *Convert* reads a 1,717-code
office standard correctly without being told anything.  Everything else here exists for the file
that is not Carlson's.

What *Custom* is for:

* **The columns are wrong.**  Some offices export their table from a database with the columns in
  their own order, or with the header row stripped.  Custom points at the column for each fact by
  hand, and it offers every column of the file *by letter and by the name that was over it*, so
  the headers may be ignored completely.
* **The first row is not a header.**  If the file starts straight in on codes, that switch says
  so and the file is read again with the first row kept - otherwise the first "code" in the table
  is a code called ``Code``.

The row ticks work in either format.  An office standard is a large table and much of it belongs
to somebody else's kind of work: type ``PROP`` in the filter, press *Only the matches*, and the
property codes come in while the 1,400 utility codes stay where they are.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                               QMessageBox, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.featurecodes import kind_for_entity
from ..core.fieldbook_syntax import (COMMAND_MEANINGS, command_map,
                                     command_token_validation_error)
from ..io import f2f
from .widgets import Banner, Hint, hline

COLS = ("Code", "Description", "Kind", "Layer", "Category")
WIDTHS = (130, 0, 80, 260, 120)          # 0 = take the rest


class CodeCommandsDialog(QDialog):
    """Edit Field Book command tokens by their fixed semantic meanings."""

    def __init__(self, commands=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Field Book Commands — Linework & Separators")
        self.resize(540, 390)
        lay = QVBoxLayout(self)
        lay.addWidget(Hint("Stored with the Field Book so it travels with the job. The Meaning column explains each role; edit only the token. Defaults follow Carlson Field-to-Finish. Spacing is configured in Settings."))

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Command token (editable)", "Meaning (fixed)"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)

        current_map = command_map(commands)
        current = [current_map[meaning] for meaning in COMMAND_MEANINGS]
        labels = f2f.DEFAULT_COMMAND_LABELS
        self.table.setRowCount(len(labels))

        for row, meaning in enumerate(labels):
            cmd = str(current[row]).strip().upper() if row < len(current) else (f2f.DEFAULT_COMMANDS[row] if row < len(f2f.DEFAULT_COMMANDS) else "")
            item_cmd = QTableWidgetItem(cmd)
            item_cmd.setFlags(item_cmd.flags() | Qt.ItemIsEditable)
            self.table.setItem(row, 0, item_cmd)
            item_meaning = QTableWidgetItem(meaning)
            item_meaning.setFlags(item_meaning.flags() & ~Qt.ItemIsEditable)
            item_meaning.setBackground(QBrush(QColor("#2a3037" if parent is not None else "#f0f0f0")))
            self.table.setItem(row, 1, item_meaning)

        lay.addWidget(self.table)

        btn_row = QHBoxLayout()
        b_def = QPushButton("Reset to Defaults")
        b_def.clicked.connect(self._reset_defaults)
        btn_row.addWidget(b_def)
        btn_row.addStretch(1)
        ok = QPushButton("OK")
        ok.setProperty("accent", True)
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(ok)
        btn_row.addWidget(cancel)
        lay.addLayout(btn_row)

    def accept(self):
        error = command_token_validation_error(self.get_commands())
        if error:
            QMessageBox.warning(self, "Field Book Commands", error)
            return
        super().accept()

    def _reset_defaults(self):
        for row, cmd in enumerate(f2f.DEFAULT_COMMANDS):
            if row < self.table.rowCount():
                it = self.table.item(row, 0)
                if it:
                    it.setText(cmd)

    def get_commands(self) -> list[str]:
        cmds = []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            txt = it.text().strip().upper() if it else ""
            cmds.append(txt)
        return cmds


class CorrectionRulesDialog(QDialog):
    """Two-column rules: Common Error (what was typed in field) -> Fix (valid code)."""

    def __init__(self, rules=None, fieldbook_codes=None, parent=None, commands=None):
        super().__init__(parent)
        self.setWindowTitle("Correction Rules — Common Errors")
        self.resize(650, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(Hint("Stored with the Field Book. Fix must match an active code. Amber rows could collapse a valid multi-code description into one code; review them before saving."))

        self.fieldbook_codes = [str(c).upper() for c in (fieldbook_codes or [])]
        self._fb_set = {c.casefold() for c in self.fieldbook_codes}
        self.commands = commands
        self.common_conversion_warnings: dict[int, str] = {}

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Common Error", "Fix"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)

        rules = rules or []
        self.table.setRowCount(len(rules))
        for r, pair in enumerate(rules):
            err = pair[0] if len(pair) > 0 else ""
            fix = pair[1] if len(pair) > 1 else ""
            self.table.setItem(r, 0, QTableWidgetItem(str(err)))
            self.table.setItem(r, 1, QTableWidgetItem(str(fix)))
        lay.addWidget(self.table)
        self.table.cellChanged.connect(lambda *_args: self._refresh_conversion_warnings())
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #946200; font-size: 11px;")
        lay.addWidget(self.warning_label)
        self._refresh_conversion_warnings()

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Row")
        add_btn.clicked.connect(self._add_row)
        del_btn = QPushButton("Remove Selected")
        del_btn.clicked.connect(self._del_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)
        ok = QPushButton("OK")
        ok.setProperty("accent", True)
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(ok)
        btn_row.addWidget(cancel)
        lay.addLayout(btn_row)

    def _refresh_conversion_warnings(self):
        from ..fieldwork.parse import common_conversion_rule_warning
        warnings = {}
        for r in range(self.table.rowCount()):
            err_item, fix_item = self.table.item(r, 0), self.table.item(r, 1)
            err = err_item.text().strip() if err_item else ""
            fix = fix_item.text().strip() if fix_item else ""
            warning = common_conversion_rule_warning(
                err, fix, self._fb_set, commands=self.commands)
            if warning:
                warnings[r] = warning
            for item in (err_item, fix_item):
                if item is None:
                    continue
                item.setBackground(QBrush(QColor("#FFF1C2")) if warning else QBrush())
                item.setToolTip(warning or "")
        self.common_conversion_warnings = warnings
        if warnings:
            self.warning_label.setText(
                f"{len(warnings)} rule(s) may collapse valid multi-code descriptions — highlighted amber. "
                "Review before saving.")
        else:
            self.warning_label.setText("")

    def _add_row(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(""))
        self.table.setItem(r, 1, QTableWidgetItem(""))

    def _del_row(self):
        sel = self.table.selectionModel().selectedRows()
        rows = sorted([s.row() for s in sel], reverse=True)
        if not rows:
            if self.table.rowCount() > 0:
                self.table.removeRow(self.table.rowCount() - 1)
            self._refresh_conversion_warnings()
            return
        for r in rows:
            self.table.removeRow(r)
        self._refresh_conversion_warnings()

    def validation_error(self) -> str | None:
        """Return the first invalid row, ensuring every Fix is an active Field Book code."""
        for r in range(self.table.rowCount()):
            err_item = self.table.item(r, 0)
            fix_item = self.table.item(r, 1)
            err = err_item.text().strip() if err_item else ""
            fix = fix_item.text().strip() if fix_item else ""
            if not err and not fix:
                continue
            if not err:
                return f"Row {r + 1}: enter a Common Error."
            if not fix:
                return f"Row {r + 1}: enter a Fix code."
            if not self._fb_set:
                return "Load or select a Field Book with feature codes before saving Fix values."
            if fix.casefold() not in self._fb_set:
                return f"Row {r + 1}: Fix '{fix}' is not a code in the active Field Book."
        return None

    def accept(self):
        error = self.validation_error()
        if error:
            QMessageBox.warning(self, "Correction Rules", error)
            return
        if self.common_conversion_warnings:
            examples = [self.table.item(r, 0).text().strip()
                        for r in sorted(self.common_conversion_warnings)[:5]
                        if self.table.item(r, 0)]
            reply = QMessageBox.question(
                self, "Common Conversion Warning",
                "Some rules can collapse valid multi-code descriptions "
                f"({', '.join(examples)}). Review the amber rows. Save these rules anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        super().accept()

    def get_rules(self) -> list[list[str]]:
        rules = []
        for r in range(self.table.rowCount()):
            e = self.table.item(r, 0)
            f = self.table.item(r, 1)
            err = e.text().strip().upper() if e else ""
            fix = f.text().strip().upper() if f else ""
            if err or fix:
                rules.append([err, fix])
        return rules


class ConvertFieldToFinishDialog(QDialog):
    """Ask which codes of a Field-to-Finish file belong in this job, and how to read them."""

    def __init__(self, table: f2f.F2FTable, project=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Convert Field to Finish")
        self.setMinimumWidth(760)
        self.table = table
        self.project = project
        self.mapping = f2f.auto_map(table)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._recount)

        extra = f2f.read_fwb_extra(table.path) if table.path else {}
        proj_settings = getattr(project, "settings", {}) or {}
        stored_commands = extra.get("commands") or proj_settings.get("f2f_commands") or f2f.DEFAULT_COMMANDS
        normalized_commands = command_map(stored_commands)
        self.commands = [normalized_commands[meaning] for meaning in COMMAND_MEANINGS]
        self.rules = list(extra.get("rules") or proj_settings.get("f2f_rules") or [])

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.addWidget(QLabel(f"<b>{table.path.name}</b>"))
        root.addWidget(Hint(str(table.path.parent)))
        self.lbl_layout = Hint(self.mapping.describe(table.headers))
        root.addWidget(self.lbl_layout)

        # ---- format ---------------------------------------------------------------------
        box = QGroupBox("Format")
        bl = QVBoxLayout(box)
        self.rb_carlson = QRadioButton("Carlson's own layout (default) - Code, Description, "
                                       "Symbol, then Layer and Entity Type")
        self.rb_custom = QRadioButton("Custom - choose the columns and the rows myself")
        self.rb_carlson.setChecked(True)
        bl.addWidget(self.rb_carlson)
        bl.addWidget(self.rb_custom)
        self.custom = _CustomColumns(table, self)
        self.custom.setVisible(False)
        bl.addWidget(self.custom)
        self.rb_custom.toggled.connect(self._set_custom)
        root.addWidget(box)

        # ---- commands and rules ---------------------------------------------------------
        box_extra = QGroupBox("Field Book Commands & Correction Rules")
        el = QHBoxLayout(box_extra)
        self.btn_commands = QPushButton("Command Meanings & Separators...")
        self.btn_commands.clicked.connect(self._edit_commands)
        self.btn_rules = QPushButton("Correction Rules...")
        self.btn_rules.clicked.connect(self._edit_rules)
        el.addWidget(self.btn_commands)
        el.addWidget(self.btn_rules)
        el.addStretch(1)
        root.addWidget(box_extra)

        # ---- the codes ------------------------------------------------------------------
        root.addWidget(hline())
        head = QHBoxLayout()
        self.lbl_rows = QLabel(f"Codes  ({len(table.rows):,})")
        head.addWidget(self.lbl_rows)
        head.addStretch(1)
        self.le_filter = QLineEdit()
        self.le_filter.setPlaceholderText("Filter codes, descriptions, layers...")
        self.le_filter.setClearButtonEnabled(True)
        self.le_filter.textChanged.connect(self._refilter)
        head.addWidget(self.le_filter, 1)
        root.addLayout(head)

        self.tbl = QTableWidget(len(table.rows), len(COLS), self)
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        for c, w in enumerate(WIDTHS):
            if w:
                self.tbl.setColumnWidth(c, w)
            else:
                hh.setSectionResizeMode(c, QHeaderView.Stretch)
        self.tbl.itemChanged.connect(lambda _item: self._timer.start())
        root.addWidget(self.tbl, 1)

        picks = QHBoxLayout()
        for text, slot in (("All", "all"), ("None", "none"), ("Invert", "invert"),
                           ("Only the matches", "matches")):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, how=slot: self._tick(how))
            picks.addWidget(b)
        picks.addStretch(1)
        root.addLayout(picks)

        # ---- into the job ---------------------------------------------------------------
        self.into = QGroupBox("Into this job's feature code table")
        il = QVBoxLayout(self.into)
        self.rb_replace = QRadioButton("Replace - the job keeps only the codes brought in here")
        self.rb_merge = QRadioButton("Merge - keep the codes the job already has")
        self.rb_replace.setChecked(True)
        il.addWidget(self.rb_replace)
        il.addWidget(self.rb_merge)
        self._merge_offered = self._job_code_count() > 0
        self.into.setVisible(self._merge_offered)
        root.addWidget(self.into)
        self.rb_merge.toggled.connect(lambda _on: self._timer.start())

        self.lbl_preview = Banner("", "info")
        root.addWidget(self.lbl_preview)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Convert")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._fill()
        self._recount()

    def _edit_commands(self):
        dlg = CodeCommandsDialog(self.commands, self)
        if dlg.exec():
            self.commands = dlg.get_commands()

    def _edit_rules(self):
        # Correction rules belong to the Field Book, not to this job's subset of converted codes.
        codes = [self.table.code_of(i, self.mapping) for i in range(len(self.table.rows))]
        dlg = CorrectionRulesDialog(self.rules, codes, self, commands=self.commands)
        if dlg.exec():
            self.rules = dlg.get_rules()

    # ------------------------------------------------------------------ the table
    def _fill(self):
        """One row per code: the facts as the current column map reads them, and a tick."""
        m = self.mapping
        self.tbl.blockSignals(True)
        self.tbl.setUpdatesEnabled(False)
        for i, row in enumerate(self.table.rows):
            entity = f2f._cell(row, m.entity)
            kind, _break, _known = kind_for_entity(entity)
            cells = (self.table.code_of(i, m), f2f._cell(row, m.description), kind.title(),
                     f2f._cell(row, m.layer), self.table.categories[i])
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked)
                self.tbl.setItem(i, c, item)
        self.tbl.setUpdatesEnabled(True)
        self.tbl.blockSignals(False)

    def _ticked(self) -> set:
        return {i for i in range(self.tbl.rowCount())
                if self.tbl.item(i, 0) is not None
                and self.tbl.item(i, 0).checkState() == Qt.Checked}

    def _ticked_codes(self) -> set:
        return {self.table.code_of(i, self.mapping) for i in self._ticked()}

    def _tick(self, how: str):
        """Tick by rule: all, none, invert, or the rows the filter is showing."""
        self.tbl.blockSignals(True)
        for i in range(self.tbl.rowCount()):
            item = self.tbl.item(i, 0)
            if item is None:
                continue
            if how == "invert":
                item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
            elif how == "matches":
                item.setCheckState(Qt.Checked if not self.tbl.isRowHidden(i) else Qt.Unchecked)
            else:
                item.setCheckState(Qt.Checked if how == "all" else Qt.Unchecked)
        self.tbl.blockSignals(False)
        self._recount()

    def _retick(self, rows=None, codes=None):
        """Re-tick after the rows have been redrawn: by row index, or by code when they moved.

        Row indices survive a change of *column map* - the rows have not moved, only the meaning
        of their cells.  They do not survive a re-read with a different header choice, so that
        case passes the codes instead, which is why the codes are read before the re-read.
        """
        inbox = {str(c).upper() for c in codes} if codes is not None else None
        self.tbl.blockSignals(True)
        for i in range(self.tbl.rowCount()):
            item = self.tbl.item(i, 0)
            if item is None:
                continue
            if inbox is not None:
                hit = self.table.code_of(i, self.mapping) in inbox
            else:
                hit = i in (rows or set())
            item.setCheckState(Qt.Checked if hit else Qt.Unchecked)
        self.tbl.blockSignals(False)
        self._recount()

    def _refilter(self, text: str):
        needle = text.strip().casefold()
        for i in range(self.tbl.rowCount()):
            hay = " ".join((self.tbl.item(i, c).text() if self.tbl.item(i, c) else "")
                           for c in range(len(COLS))).casefold()
            self.tbl.setRowHidden(i, bool(needle) and needle not in hay)
        shown = sum(0 if self.tbl.isRowHidden(i) else 1 for i in range(self.tbl.rowCount()))
        self.lbl_rows.setText(f"Codes  ({shown:,} shown of {self.tbl.rowCount():,})")

    # ------------------------------------------------------------------ the column map
    def _set_custom(self, on: bool):
        self.custom.setVisible(on)
        self.use_mapping(self.custom.mapping() if on else f2f.auto_map(self.table))

    def use_mapping(self, mapping: f2f.ColumnMap, keep_codes=None):
        """Adopt a column map (from the combos or from the format radio) and redraw the rows."""
        rows = self._ticked()
        self.mapping = mapping
        self.lbl_layout.setText(mapping.describe(self.table.headers))
        self.custom.follow(mapping)
        self._fill()
        self._retick(rows=rows, codes=keep_codes)

    # ------------------------------------------------------------------ the count
    def _job_code_count(self) -> int:
        codes = getattr(self.project, "codes", None)
        return len(codes) if codes is not None else 0

    def mode(self) -> str:
        return "merge" if self._merge_offered and self.rb_merge.isChecked() else "replace"

    def _recount(self):
        """Say what the ticks and the merge choice would produce, before anything is changed."""
        only = self._ticked()
        job = self._job_code_count()
        merge = self.mode() == "merge"
        # the job's own table is handed in even in Replace mode: nothing is written from here, and
        # it is how the count of codes the file does not name is known before the button is pressed
        _table, stats = f2f.convert(self.table, self.mapping, only=only,
                                    existing=getattr(self.project, "codes", None))
        if not only:
            text, warn = "No codes are ticked - nothing would be converted.", True
        else:
            text = f2f.describe_stats(stats, mention_kept=merge)
            warn = bool(stats["unknown_entity_types"])
            if job and not merge and stats["kept"]:
                text += (f" {stats['kept']:,} of this job's {job:,} code(s) are not in this file "
                         f"and would be dropped - Merge keeps them.")
                warn = True
        self.lbl_preview.set(text, "warn" if warn else "info")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(only))

    # ------------------------------------------------------------------ answers
    def choices(self):
        """(column map, ticked row indices, "replace" | "merge", commands, rules)"""
        return self.mapping, self._ticked(), self.mode(), self.commands, self.rules


class _CustomColumns(QWidget):
    """A box per fact: which column of *this* file holds it.  Headers are not required."""

    def __init__(self, table: f2f.F2FTable, dialog: ConvertFieldToFinishDialog):
        super().__init__(dialog)
        self.dialog = dialog
        form = QFormLayout(self)
        form.setContentsMargins(18, 4, 0, 0)
        self.chk_header = QCheckBox("The first row is a header, not a code")
        self.chk_header.setChecked(bool(table.header_row))
        self.chk_header.setToolTip("Un-tick this when the file starts straight in on codes: it is "
                                   "read again with the first row kept.  The ticks you have made "
                                   "survive, because they are remembered by code.")
        self.chk_header.toggled.connect(self._reread)
        form.addRow("", self.chk_header)
        self.combos = {}
        for fact in f2f.FACTS:
            c = QComboBox()
            if fact == "category":
                c.addItem("- the file has no category column -", -1)
            for i in range(table.width):
                c.addItem(table.column_label(i), i)
            c.setCurrentIndex(max(0, c.findData(f2f.auto_map(table).index(fact))))
            c.currentIndexChanged.connect(self._changed)
            self.combos[fact] = c
            form.addRow(f2f.FACT_LABELS[fact], c)

    def follow(self, mapping: f2f.ColumnMap):
        """Move the combos to a map that came from somewhere else (the Carlson radio)."""
        for fact, combo in self.combos.items():
            i = combo.findData(mapping.index(fact))
            if i >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)

    def mapping(self) -> f2f.ColumnMap:
        return f2f.ColumnMap(found_by="the columns you chose",
                             **{f: self.combos[f].currentData() for f in f2f.FACTS})

    def _changed(self, _index: int = 0):
        if self.dialog.rb_custom.isChecked():
            self.dialog.use_mapping(self.mapping())

    def _reread(self, header_row: bool):
        keep = self.dialog._ticked_codes()               # read *before* the table is replaced
        try:
            fresh = f2f.read(self.dialog.table.path, header_row=header_row)
        except Exception:
            self.chk_header.setChecked(bool(self.dialog.table.header_row))
            return
        self.dialog.table = fresh
        self.dialog.tbl.setRowCount(len(fresh.rows))
        self.dialog.use_mapping(self.mapping() if self.dialog.rb_custom.isChecked()
                                else f2f.auto_map(fresh), keep_codes=keep)
