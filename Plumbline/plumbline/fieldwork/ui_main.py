
# ui_main.py — MainWindow, dialogs, tabs, unified checks
import sys, json, csv, os, re, math, pathlib
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog,
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit,
    QFileDialog, QMessageBox, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QTabWidget, QSplitter, QHeaderView,
    QSpinBox, QGroupBox, QComboBox, QMenu,
    QStyledItemDelegate, QCompleter, QCheckBox
)
from PySide6.QtCore import Qt

from .config import PROJECT_FILE_EXT, WORKING_FILE_EXT, FIELDBOOK_EXT, CHECK_REPORT_EXT, LEGACY_CHECK_EXT, COORDINATE_UNITS, XY_TOLERANCE, NE_TOLERANCE, ELEV_TOLERANCE, CHECK_REPORT_HEADERS, UNIFIED_REPORT_HEADERS, LEGACY_CHECK_HEADERS, DESC_PARSE_HEADERS, CONTROL_RANGE, BOUNDARY_RANGE, GENERAL_START, DEFAULT_CREW_NUMBER, crew_blocks
from .coord_systems import EPSG_LIBRARY, DEFAULT_ACTIVE_EPSGS, epsg_choices_for_combo, get_epsg_info, MESQUITE_TX_RECOMMENDED_EPSG
from .steps_report import compute_steps_status, write_steps_csv, write_steps_html
from .renumber_tool import SingleRenumberDialog, RangeRenumberDialog, apply_single_to_table
from .utils_sort import natural_key, NaturalSortItem, NumericSortItem
from .detectors import normalize_point_number, point_number_core, find_exact_duplicate_groups, find_similar_number_groups, find_close_ne_groups, _working_row_to_float, _cardinal_direction, _bearing_dms, _format_dms, _format_distance_direction
from .clean import CleanDescriptionDialog, is_descriptions_clean, validate_renumber_allowed
from .io_carlson import read_carlson_fieldbook, write_fwb_file, read_fwb_file, write_check_report, read_check_report, build_check_report_rows, build_unified_report_rows, write_unified_report, read_unified_report
from .parse import _strip_trailing_digits, _classify_tokens_sequential, _extract_tokens, parse_desc_field, _validate_line_command_order, build_f2f_set_from_fieldbook
from .linecheck import (detect_line_errors as detect_line_issues,
                        propose_fix as propose_line_fix,
                        validate_fix as validate_line_fix,
                        statuses_from_rows as line_statuses_from_rows,
                        report_rows as line_report_rows)


def _working_row_sequence_key(row):
    oid = str(row[0]).strip() if row else ""
    return (0, int(oid)) if oid.isdigit() else (1, natural_key(oid, letters_first=False))


def _widen_saf(widget, characters: int = 18):
    """Give a SAF box room for *characters* characters.

    A Surface Adjustment Factor is read digit by digit - 1.00017 and 1.000170 are not the same
    factor - so the box is sized from its own font instead of being left at Qt's guess, which
    comes out two or three characters short of what a user types into it.  Every SAF entry on
    the field side goes through here; the CRS dialog sizes its own box the same way.
    """
    from PySide6.QtGui import QFontMetrics
    fm = QFontMetrics(widget.font())
    widget.setMinimumWidth(max(fm.horizontalAdvance("0" * int(characters)) + 14,
                               widget.minimumWidth()))
    return widget


class ProjectPathsDialog(QDialog):
    def __init__(self, project_path="", fieldwork_path="", fieldbook_path="", master_file_path="", parent=None, *args, **kwargs):
        # Backward compat: old call ProjectPathsDialog(a,b,c, parentWidget) where 4th arg is parent, not master
        # Detect if master_file_path is actually a QWidget (parent)
        try:
            from PySide6.QtWidgets import QWidget
            if master_file_path is not None and isinstance(master_file_path, QWidget):
                parent = master_file_path
                master_file_path = ""
            elif master_file_path is not None and not isinstance(master_file_path, str):
                # If it's not a string (e.g., MainWindow passed as 4th positional), treat as parent
                if hasattr(master_file_path, 'isWidgetType') or hasattr(master_file_path, 'windowTitle'):
                    parent = master_file_path
                    master_file_path = ""
        except Exception:
            # Fallback: if master_file_path looks like object not path, treat as parent
            if master_file_path is not None and not isinstance(master_file_path, str):
                parent = master_file_path
                master_file_path = ""
        # Also handle kwargs master_file_path
        if "master_file_path" in kwargs:
            master_file_path = kwargs.pop("master_file_path", master_file_path)
        if "master" in kwargs:
            master_file_path = kwargs.pop("master", master_file_path)
        # If args contains extra positional (old parent passed as 5th)
        if args:
            # first extra arg is parent if parent is still None
            if parent is None and args:
                parent = args[0]

        super().__init__(parent)
        self.setWindowTitle("Project Paths")
        self.resize(650, 270)

        layout = QVBoxLayout(self)

        paths_group = QGroupBox("Paths")
        paths_layout = QFormLayout(paths_group)

        proj_row = QHBoxLayout()
        self.project_input = QLineEdit(project_path)
        proj_browse = QPushButton("Browse...")
        proj_browse.clicked.connect(self._browse_project)
        proj_row.addWidget(self.project_input)
        proj_row.addWidget(proj_browse)

        field_row = QHBoxLayout()
        self.fieldwork_input = QLineEdit(fieldwork_path)
        field_browse = QPushButton("Browse...")
        field_browse.clicked.connect(self._browse_fieldwork)
        field_row.addWidget(self.fieldwork_input)
        field_row.addWidget(field_browse)

        fwb_row = QHBoxLayout()
        self.fieldbook_input = QLineEdit(fieldbook_path)
        self.fieldbook_input.setPlaceholderText(f"Select existing {FIELDBOOK_EXT} (or convert via Tools)")
        fwb_browse = QPushButton("Browse...")
        fwb_browse.clicked.connect(self._browse_fieldbook)
        fwb_row.addWidget(self.fieldbook_input)
        fwb_row.addWidget(fwb_browse)

        master_row = QHBoxLayout()
        self.master_input = QLineEdit(master_file_path)
        self.master_input.setPlaceholderText("Master final CSV (for Global Renumber — existing project data)")
        master_browse = QPushButton("Browse...")
        master_browse.clicked.connect(self._browse_master)
        master_row.addWidget(self.master_input)
        master_row.addWidget(master_browse)

        paths_layout.addRow("Project Path:", proj_row)
        paths_layout.addRow("Field Data Folder (Raw):", field_row)
        paths_layout.addRow(f"Field Book ({FIELDBOOK_EXT}):", fwb_row)
        paths_layout.addRow("Master File CSV (Global):", master_row)
        layout.addWidget(paths_group)

        # Small hint explaining field-book wiring
        hint = QLabel(
            f"Field Book: headered {FIELDBOOK_EXT} generated from Carlson F2F CSV via Tools > Convert. "
            "You can also pick an existing file here."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _browse_project(self):
        path = QFileDialog.getExistingDirectory(self, "Select Project Folder")
        if path:
            self.project_input.setText(path)

    def _browse_fieldwork(self):
        path = QFileDialog.getExistingDirectory(self, "Select Field Data Folder")
        if path:
            self.fieldwork_input.setText(path)

    def _browse_master(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Master File CSV",
            self.master_input.text() or self.project_input.text(),
            "CSV Files (*.csv);;All Files (*)")
        if path:
            self.master_input.setText(path)

    def _browse_fieldbook(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Field Book",
            self.fieldbook_input.text() or self.project_input.text(),
            f"Field Book (*{FIELDBOOK_EXT});;All Files (*)")
        if path:
            self.fieldbook_input.setText(path)

    def get_paths(self):
        # Return 4-tuple for compat, but also support unpacking 3
        return self.project_input.text(), self.fieldwork_input.text(), self.fieldbook_input.text(), self.master_input.text()


# ----- Precision dialog -----
class PrecisionDialog(QDialog):
    def __init__(self, ne_decimals=5, elev_decimals=5, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Precision")
        self.resize(400, 130)

        layout = QVBoxLayout(self)

        prec_group = QGroupBox("Decimal Precision (Rounding)")
        prec_layout = QFormLayout(prec_group)

        self.ne_spin = QSpinBox()
        self.ne_spin.setRange(0, 9)
        self.ne_spin.setValue(ne_decimals)
        self.ne_spin.setToolTip("Decimal places for Northing & Easting")

        self.elev_spin = QSpinBox()
        self.elev_spin.setRange(0, 9)
        self.elev_spin.setValue(elev_decimals)
        self.elev_spin.setToolTip("Decimal places for Elevation")

        prec_layout.addRow("Northing/Easting (decimal places):", self.ne_spin)
        prec_layout.addRow("Elevation (decimal places):", self.elev_spin)
        layout.addWidget(prec_group)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_precisions(self):
        return self.ne_spin.value(), self.elev_spin.value()


# ----- Correction Rules dialog (common error -> fix) — 2nd col searches fieldbook & validates -----
class _FieldbookCodeDelegate(QStyledItemDelegate):
    """Delegate for Fix column: QLineEdit with QCompleter that searches fieldbook codes (contains, case-insensitive)."""
    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self.codes = sorted(set(codes), key=lambda s: s.lower()) if codes else []
    def createEditor(self, parent, option, index):
        if index.column() == 1:
            editor = QLineEdit(parent)
            if self.codes:
                completer = QCompleter(self.codes, editor)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                try:
                    completer.setFilterMode(Qt.MatchFlag.MatchContains)
                except Exception:
                    pass
                completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                editor.setCompleter(completer)
            editor.setPlaceholderText("type to search Field Book codes…")
            return editor
        return super().createEditor(parent, option, index)

class CorrectionRulesDialog(QDialog):
    """Two-column rules: Common Error (what was typed) -> Fix (fieldbook code).
    Stored in fieldbook file extra (.fwb single-file, #EXTRA_JSON) so fieldbook carries its own rules.
    2nd column (Fix) searches fieldbook codes via completer and validates — prevents creating errors on autofix.
    """
    def __init__(self, rules=None, fieldbook_codes=None, parent=None, fieldbook_path=None, commands=None):
        super().__init__(parent)
        self.fieldbook_path = fieldbook_path
        self.commands = commands
        self.setWindowTitle("Correction Rules — Common Errors")
        self.resize(650, 420)
        lay = QVBoxLayout(self)
        hint = QLabel("Stored in the Field Book file (.fwb single-file) so it travels with the fieldbook.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555; font-size: 11px;")
        lay.addWidget(hint)

        self.fieldbook_codes = fieldbook_codes or []
        self._fb_set = {str(c).casefold() for c in self.fieldbook_codes}
        self.common_conversion_warnings = {}

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Common Error", "Fix"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        # Install completer delegate for Fix column
        if self.fieldbook_codes:
            self.table.setItemDelegateForColumn(1, _FieldbookCodeDelegate(self.fieldbook_codes, self.table))

        # Populate
        rules = rules or []
        self.table.setRowCount(len(rules))
        for r, (err, fix) in enumerate(rules):
            self.table.setItem(r, 0, QTableWidgetItem(str(err)))
            item = QTableWidgetItem(str(fix))
            self.table.setItem(r, 1, item)
        lay.addWidget(self.table)

        # Status / validation label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #a00; font-size: 11px;")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Row")
        add_btn.clicked.connect(self._add_row)
        del_btn = QPushButton("Remove Selected")
        del_btn.clicked.connect(self._del_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        # Fieldbook code helper + search tip
        if self.fieldbook_codes:
            helper = QLabel(f"Field Book has {len(self.fieldbook_codes)} codes — start typing in Fix to search: {', '.join(sorted(self.fieldbook_codes)[:12])}{' …' if len(self.fieldbook_codes)>12 else ''}")
            helper.setStyleSheet("color: #666; font-size: 10px;")
            helper.setWordWrap(True)
            lay.addWidget(helper)
        else:
            helper = QLabel("No Field Book codes loaded — fixes cannot be verified.")
            helper.setStyleSheet("color: #888; font-size: 10px;")
            lay.addWidget(helper)

        bot = QHBoxLayout()
        ok = QPushButton("OK")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        bot.addStretch()
        bot.addWidget(ok)
        bot.addWidget(cancel)
        lay.addLayout(bot)

        # Live validation on change
        self.table.itemChanged.connect(lambda *a: self._validate_rows())
        self._validate_rows()

    def _add_row(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(""))
        self.table.setItem(r, 1, QTableWidgetItem(""))
        self._validate_rows()

    def _del_row(self):
        sel = self.table.selectionModel().selectedRows()
        rows = sorted([s.row() for s in sel], reverse=True)
        if not rows:
            if self.table.rowCount()>0:
                self.table.removeRow(self.table.rowCount()-1)
            self._validate_rows()
            return
        for r in rows:
            self.table.removeRow(r)
        self._validate_rows()

    def _validate_rows(self):
        from PySide6.QtGui import QBrush, QColor
        from .parse import common_conversion_rule_warning
        fb_set = self._fb_set
        bad = []
        conversion_warnings = {}
        for r in range(self.table.rowCount()):
            err_item = self.table.item(r, 0)
            item = self.table.item(r, 1)
            if not item:
                continue
            err = err_item.text().strip() if err_item else ""
            fix = item.text().strip()
            first = fix.split()[0] if fix else ""
            if not fix:
                item.setBackground(QBrush(QColor("#FFFFFF")))
                item.setToolTip("")
                if err_item:
                    err_item.setBackground(QBrush())
                    err_item.setToolTip("")
                continue
            valid_fix = bool(fb_set and first.casefold() in fb_set)
            if not fb_set:
                bad.append(f"Row {r + 1}: no active Field Book codes are available to verify '{fix}'.")
                item.setBackground(QBrush(QColor("#FFCCCC")))
                item.setToolTip("Select a Field Book with codes before saving Fix values.")
            elif not valid_fix:
                bad.append(f"Row {r + 1}: '{first}' is not in the active Field Book.")
                item.setBackground(QBrush(QColor("#FFCCCC")))
                item.setToolTip(f"'{first}' not in Field Book — autofix would create UnknownCode")
            else:
                item.setBackground(QBrush(QColor("#FFFFFF")))
                item.setToolTip("Valid Field Book code")

            warning = common_conversion_rule_warning(
                err, fix, fb_set, fieldbook_path=self.fieldbook_path, commands=self.commands)
            if warning:
                conversion_warnings[r] = warning
                if err_item:
                    err_item.setBackground(QBrush(QColor("#FFF1C2")))
                    err_item.setToolTip(warning)
                if valid_fix:
                    item.setBackground(QBrush(QColor("#FFF1C2")))
                    item.setToolTip(warning)
            elif err_item:
                err_item.setBackground(QBrush())
                err_item.setToolTip("")
        self.common_conversion_warnings = conversion_warnings
        if bad:
            message = f"{len(bad)} Fix value(s) cannot be verified against the active Field Book:\n" + "\n".join(bad[:5])
            if conversion_warnings:
                message += f"\n{len(conversion_warnings)} common-conversion warning(s) are highlighted amber."
            self.status_label.setText(message)
            self.status_label.setStyleSheet("color: #a00; font-size: 11px;")
        elif conversion_warnings:
            self.status_label.setText(
                f"{len(conversion_warnings)} common-conversion rule(s) may collapse valid codes — highlighted amber for review.")
            self.status_label.setStyleSheet("color: #946200; font-size: 11px;")
        else:
            self.status_label.setText("All fixes valid ✓" if self.table.rowCount() else "")
            self.status_label.setStyleSheet("color: #080; font-size: 11px;")

    def accept(self):
        fb_set = self._fb_set
        bad = []
        for r in range(self.table.rowCount()):
            err_item = self.table.item(r, 0)
            fix_item = self.table.item(r, 1)
            err = err_item.text().strip() if err_item else ""
            fix = fix_item.text().strip() if fix_item else ""
            if not err and not fix:
                continue
            if not err:
                bad.append(f"Row {r + 1}: enter a Common Error.")
                continue
            if not fix:
                bad.append(f"Row {r + 1}: enter a Fix code.")
                continue
            first = fix.split()[0]
            if not fb_set:
                bad.append(f"Row {r + 1}: '{err}' → '{fix}' cannot be verified because the active Field Book has no codes.")
            elif first.casefold() not in fb_set:
                bad.append(f"Row {r + 1}: '{err}' → '{fix}' (code '{first}' not in Field Book)")
        if bad:
            QMessageBox.warning(
                self, "Fix Not in Field Book",
                "Fix values must start with a code from the active Field Book:\n" +
                "\n".join(bad[:6]) + ("\n…" if len(bad) > 6 else ""),
            )
            return
        if self.common_conversion_warnings:
            examples = [self.table.item(r, 0).text().strip()
                        for r in sorted(self.common_conversion_warnings)[:5]
                        if self.table.item(r, 0)]
            reply = QMessageBox.question(
                self, "Common Conversion Warning",
                "Some rules can collapse valid multi-code descriptions into one code "
                f"({', '.join(examples)}). Review the amber rows. Save these rules anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        super().accept()

    def get_rules(self):
        rules=[]
        for r in range(self.table.rowCount()):
            err = self.table.item(r,0).text().strip() if self.table.item(r,0) else ""
            fix = self.table.item(r,1).text().strip() if self.table.item(r,1) else ""
            if err or fix:
                if err:
                    rules.append([err, fix])
        return rules


# ----- Field Book Commands dialog -----
class CodeCommandsDialog(QDialog):
    """Edit Field Book commands by meaning; the meanings stay fixed, while tokens travel with the book."""
    def __init__(self, commands=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Field Book Commands — Linework & Separators")
        self.resize(520, 380)
        lay = QVBoxLayout(self)
        hint = QLabel("Saved with the Field Book. The locked Meaning column identifies each command's role; edit the token in the first column. Defaults follow Carlson Field-to-Finish. Spacing preferences are in Settings.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555; font-size: 11px;")
        lay.addWidget(hint)

        from PySide6.QtGui import QBrush, QColor
        from .config import LINE_COMMAND_LABELS
        from ..core.fieldbook_syntax import COMMAND_MEANINGS, command_map

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Command token (editable)", "Meaning (fixed)"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setRowCount(len(LINE_COMMAND_LABELS))
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)

        # Keep each token attached to its semantic meaning, including intentionally blank meanings.
        current = command_map(commands)
        for row, meaning in enumerate(LINE_COMMAND_LABELS):
            cmd = str(current[COMMAND_MEANINGS[row]])
            item_cmd = QTableWidgetItem(cmd)
            item_cmd.setFlags(item_cmd.flags() | Qt.ItemFlag.ItemIsEditable)
            item_cmd.setToolTip(f"Editable — token for {meaning}")
            self.table.setItem(row, 0, item_cmd)
            item_meaning = QTableWidgetItem(meaning)
            item_meaning.setFlags(item_meaning.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_meaning.setBackground(QBrush(QColor("#F0F0F0")))
            item_meaning.setToolTip("Locked — meaning")
            self.table.setItem(row, 1, item_meaning)

        lay.addWidget(self.table)

        self.info = QLabel("")
        self.info.setStyleSheet("color: #888; font-size: 10px;")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.table.itemChanged.connect(lambda *a: self._update_info())
        self._update_info()

        bot = QHBoxLayout()
        ok = QPushButton("OK")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        bot.addStretch()
        bot.addWidget(ok)
        bot.addWidget(cancel)
        lay.addLayout(bot)

    def _update_info(self):
        commands = self.get_commands()
        assigned = sum(bool(token) for token in commands)
        self.info.setText(f"{assigned} of {self.table.rowCount()} command meanings assigned. Row order is fixed and saved with the Field Book.")

    def accept(self):
        from ..core.fieldbook_syntax import command_token_validation_error
        error = command_token_validation_error(self.get_commands())
        if error:
            QMessageBox.warning(self, "Field Book Commands", error)
            return
        super().accept()

    def get_commands(self):
        # Keep empty cells in place: each row is bound to one fixed semantic meaning.
        return [
            self.table.item(row, 0).text().strip().upper() if self.table.item(row, 0) else ""
            for row in range(self.table.rowCount())
        ]

class CoordinateSystemDialog(QDialog):
    """Coordinate System Manager — 2011 Texas only per user request.
    Shows 2011 Texas State Plane zones (USft + m), TOP LAYER SCALER (SAF) from origin (0,0),
    with Import/Save options. SAF is user-keyed TXDOT county factor: final = grid * SAF from origin.
    Grid = Ground / SAF (first out last in). Pure-python Texas LCC fallback works without pyproj.
    """
    def __init__(self, current_epsg=6584, current_saf=1.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Coordinate System Manager — 2011 Texas Only (TXDOT SAF from Origin 0,0)")
        self.resize(700, 520)
        layout = QVBoxLayout(self)
        # Top layer scaler explanation
        top_group = QGroupBox("Top Layer Scaler — First Out Last In (TXDOT SOP)")
        top_lay = QVBoxLayout(top_group)
        top_hint = QLabel("GPS → State Plane grid (e.g., TXNC 2011) → <b>SAF from origin (0,0)</b> → Final Ground (what you store).<br>Final Ground = Grid * SAF where SAF is county-wide TXDOT factor (e.g., Bexar 1.00017) scaling from 0,0 of the pre-final TXNC 2011 system (approx 6M N by 3M E, origin 0,0).<br>For KML/Grid conversion: Grid = Ground / SAF (divide). If you store Grid, use SAF 1.0.")
        top_hint.setWordWrap(True)
        top_hint.setTextFormat(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.TextFormat.RichText)
        top_hint.setStyleSheet("color:#1a237e; font-size:11px;")
        top_lay.addWidget(top_hint)
        saf_row = QHBoxLayout()
        self.saf_use_ground = QCheckBox("Use ground coordinates")
        self.saf_use_ground.setToolTip("SAF is only in force when ground coordinates are switched on;\n"
                                       "with this clear the system is grid and the factor is locked at 1.0.")
        top_lay.addWidget(self.saf_use_ground)
        saf_row.addWidget(QLabel("TXDOT SAF:"))
        self.saf_edit = QLineEdit(str(current_saf))
        self.saf_edit.setPlaceholderText("1.0 = grid (no SAF); e.g., 1.00017 for Bexar County")
        _widen_saf(self.saf_edit)
        saf_row.addWidget(self.saf_edit)
        self.saf_info = QLabel("1.0 = no scale; >1 = ground larger than grid")
        self.saf_info.setStyleSheet("color:#555; font-size:10px;")
        saf_row.addWidget(self.saf_info)
        top_lay.addLayout(saf_row)
        layout.addWidget(top_group)

        # Coordinate system selection — 2011 Texas only
        cs_group = QGroupBox("2011 Texas State Plane Coordinate System (NAD83(2011) — USft Preferred)")
        cs_lay = QVBoxLayout(cs_group)
        cs_hint = QLabel("Only 2011 Texas adjustments are shown per request. TXNC 2011 is approx 6M N by 3M E (origin 0,0) before SAF. Choose zone that matches your fieldwork (check master or GPS base).")
        cs_hint.setWordWrap(True)
        cs_hint.setStyleSheet("color:#555; font-size:11px;")
        cs_lay.addWidget(cs_hint)
        self.epsg_combo = QComboBox()
        # Populate only 2011 Texas
        try:
            from .coord_systems import EPSG_LIBRARY
            # Filter to 2011 Texas only (6581-6588, incl 6577/6578)
            texas_2011 = {k: v for k, v in EPSG_LIBRARY.items() if "2011" in v.get("name","") and v["state"]=="TX"}
            # Sort by zone order North -> South
            order = {"North":0, "North Central":1, "Central":2, "South Central":3, "South":4}
            sorted_eps = sorted(texas_2011.items(), key=lambda kv: order.get(kv[1]["zone"].split()[0], 99))
            for epsg, info in sorted_eps:
                label = f"{epsg} — {info['name']} ({info['units']})"
                self.epsg_combo.addItem(label, epsg)
        except Exception as e:
            self.epsg_combo.addItem(f"{current_epsg} — Current", current_epsg)
        # Select current
        for i in range(self.epsg_combo.count()):
            if self.epsg_combo.itemData(i) == current_epsg:
                self.epsg_combo.setCurrentIndex(i)
                break
        cs_lay.addWidget(self.epsg_combo)
        # Import / Save row inside group
        imp_row = QHBoxLayout()
        self.import_btn = QPushButton("Import... (from library/file)")
        self.import_btn.setToolTip("Import a 2011 Texas EPSG from full library or from .prj/.json")
        self.save_btn = QPushButton("Save to Project")
        self.save_btn.setToolTip("Save selected EPSG + SAF to current project (.fmp)")
        self.save_file_btn = QPushButton("Save to File... (.json)")
        self.load_file_btn = QPushButton("Load from File...")
        imp_row.addWidget(self.import_btn)
        imp_row.addWidget(self.save_btn)
        imp_row.addWidget(self.save_file_btn)
        imp_row.addWidget(self.load_file_btn)
        imp_row.addStretch()
        cs_lay.addLayout(imp_row)
        layout.addWidget(cs_group)

        # Status
        self.status = QLabel("Select 2011 zone and enter TXDOT county SAF. Import adds 2011 zones; Save stores to project.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(self.status)

        # Buttons
        btn_box = QHBoxLayout()
        btn_box.addStretch()
        self.ok_btn = QPushButton("OK")
        self.cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(self.cancel_btn)
        btn_box.addWidget(self.ok_btn)
        layout.addLayout(btn_box)
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        self.import_btn.clicked.connect(self._on_import)
        self.save_btn.clicked.connect(self._on_save_project)
        self.save_file_btn.clicked.connect(self._on_save_file)
        self.load_file_btn.clicked.connect(self._on_load_file)
        self.saf_edit.textChanged.connect(self._validate)

        # SAF is only in force with ground coordinates switched on: locked at 1.0 until then, the
        # same rule as the Coordinate System tab and the main window's CRS dialog.
        def _ground_toggled(on):
            on = bool(on)
            self.saf_edit.setEnabled(on)
            if not on:
                self.saf_edit.setText("1.0")
                self.saf_info.setText("grid - tick Use ground coordinates to enter a SAF")
                self.saf_info.setStyleSheet("color:#555;")
        try:
            self.saf_use_ground.setChecked(abs(float(current_saf) - 1.0) > 1e-12)
        except Exception:
            self.saf_use_ground.setChecked(False)
        self.saf_use_ground.toggled.connect(_ground_toggled)
        _ground_toggled(self.saf_use_ground.isChecked())

    def _validate(self):
        try:
            saf = float(self.saf_edit.text().strip())
            if abs(saf-1.0) < 1e-9:
                self.saf_info.setText("1.0 = grid")
                self.saf_info.setStyleSheet("color:#2e7d32;")
            elif saf > 1:
                self.saf_info.setText(f"{saf} → ground larger (grid * SAF from 0,0)")
                self.saf_info.setStyleSheet("color:#ef6c00;")
            else:
                self.saf_info.setText(f"{saf} → ground smaller")
                self.saf_info.setStyleSheet("color:#c62828;")
        except:
            self.saf_info.setText("Enter numeric SAF")
            self.saf_info.setStyleSheet("color:#c62828;")

    def _on_import(self):
        from PySide6.QtWidgets import QInputDialog, QFileDialog, QMessageBox
        from .coord_systems import EPSG_LIBRARY, add_custom_epsg
        # Offer to pick from full EPSG library (but we only want 2011 Texas per request, so filter)
        # Still allow any EPSG if user types
        txt, ok = QInputDialog.getText(self, "Import Coordinate System", "Enter 2011 Texas EPSG to import (e.g., 6584 TX North Central USft, 6578 TX Central USft) or paste PROJ string file path:")
        if not ok or not txt.strip():
            return
        import re, pathlib
        # If it's a file path
        p = pathlib.Path(txt.strip())
        if p.exists() and p.suffix.lower() in (".prj",".json",".txt"):
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                # Try to parse EPSG from content
                m = re.search(r"EPSG[:\"]?(\d+)", content)
                if m:
                    epsg = int(m.group(1))
                    if epsg in EPSG_LIBRARY:
                        # Find and select
                        for i in range(self.epsg_combo.count()):
                            if self.epsg_combo.itemData(i)==epsg:
                                self.epsg_combo.setCurrentIndex(i)
                                self.status.setText(f"Imported {epsg} from file {p.name}")
                                return
                    # Try to add custom
                    add_custom_epsg(epsg, f"Imported from {p.name}")
                    self.epsg_combo.addItem(f"{epsg} — Imported from {p.name}", epsg)
                    for i in range(self.epsg_combo.count()):
                        if self.epsg_combo.itemData(i)==epsg:
                            self.epsg_combo.setCurrentIndex(i)
                            break
                    self.status.setText(f"Imported {epsg} from {p.name}")
                    return
                else:
                    QMessageBox.information(self, "Import", "No EPSG found in file")
                    return
            except Exception as e:
                QMessageBox.warning(self, "Import", str(e))
                return
        # Else treat as EPSG number
        m = re.search(r"\d+", txt)
        if m:
            epsg = int(m.group(0))
            if epsg in EPSG_LIBRARY:
                for i in range(self.epsg_combo.count()):
                    if self.epsg_combo.itemData(i)==epsg:
                        self.epsg_combo.setCurrentIndex(i)
                        self.status.setText(f"Selected {epsg} — {EPSG_LIBRARY[epsg]['name']}")
                        return
                # If not in combo but in library (maybe filtered), add it
                info = EPSG_LIBRARY[epsg]
                self.epsg_combo.addItem(f"{epsg} — {info['name']} ({info['units']})", epsg)
                for i in range(self.epsg_combo.count()):
                    if self.epsg_combo.itemData(i)==epsg:
                        self.epsg_combo.setCurrentIndex(i)
                        break
                self.status.setText(f"Imported {epsg}")
            else:
                # Try to add custom
                if add_custom_epsg(epsg, f"EPSG {epsg}"):
                    self.epsg_combo.addItem(f"{epsg} — EPSG {epsg}", epsg)
                    for i in range(self.epsg_combo.count()):
                        if self.epsg_combo.itemData(i)==epsg:
                            self.epsg_combo.setCurrentIndex(i)
                            break
                    self.status.setText(f"Added custom EPSG {epsg}")
                else:
                    QMessageBox.warning(self, "Import", f"Failed to add EPSG {epsg}")

    def _on_save_project(self):
        from PySide6.QtWidgets import QMessageBox
        # Save will be done by caller via result values, just notify
        QMessageBox.information(self, "Save to Project", "Click OK to save selected EPSG + SAF to project (will be applied on next KML export and reload).")

    def _on_save_file(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import json, pathlib
        path, _ = QFileDialog.getSaveFileName(self, "Save Coordinate System to File", "TX2011_CoordSystem.json", "JSON (*.json)")
        if not path:
            return
        try:
            saf = float(self.saf_edit.text().strip()) if self.saf_edit.text().strip() else 1.0
        except:
            saf = 1.0
        epsg = self.epsg_combo.currentData()
        data = {"epsg": epsg, "saf": saf, "note": "TXDOT SAF from origin 0,0: final = grid * SAF, 2011 Texas only"}
        pathlib.Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        QMessageBox.information(self, "Saved", f"Saved to {path}")

    def _on_load_file(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import json, pathlib
        path, _ = QFileDialog.getOpenFileName(self, "Load Coordinate System from File", "", "JSON (*.json);;All (*.*)")
        if not path:
            return
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
            epsg = int(data.get("epsg", 0))
            saf = float(data.get("saf", 1.0))
            # Select EPSG
            for i in range(self.epsg_combo.count()):
                if self.epsg_combo.itemData(i)==epsg:
                    self.epsg_combo.setCurrentIndex(i)
                    break
            else:
                # Add if not present
                from .coord_systems import EPSG_LIBRARY, add_custom_epsg
                info = EPSG_LIBRARY.get(epsg)
                label = f"{epsg} — {info['name']}" if info else f"{epsg} — Imported"
                self.epsg_combo.addItem(label, epsg)
                for i in range(self.epsg_combo.count()):
                    if self.epsg_combo.itemData(i)==epsg:
                        self.epsg_combo.setCurrentIndex(i)
                        break
            self.saf_edit.setText(str(saf))
            self.status.setText(f"Loaded {epsg} SAF {saf} from {pathlib.Path(path).name}")
        except Exception as e:
            QMessageBox.warning(self, "Load", str(e))

    def result_values(self):
        try:
            saf = float(self.saf_edit.text().strip()) if self.saf_edit.text().strip() else 1.0
        except:
            saf = 1.0
        epsg = self.epsg_combo.currentData()
        return int(epsg) if epsg else 6584, float(saf)

# Backward compat alias — old code may import FieldCommandsDialog
FieldCommandsDialog = CodeCommandsDialog
LineCommandsDialog = CodeCommandsDialog
FieldCommandsDialog = CodeCommandsDialog
LineCommandsDialog = CodeCommandsDialog



# ----- Main window -----
class MainWindow(QMainWindow):
    HEADERS = ["Point Number", "Northing", "Easting", "Elevation",
               "Description", "Parent Folder", "Source File"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fieldwork Manager")
        self.resize(1200, 750)

        # Project (.fmp) state — custom ext, JSON inside
        self.current_file = None
        self.is_dirty = False
        self.project_path = ""
        self.fieldwork_path = ""
        self.fieldbook_path = ""
        self.master_file_path = ""
        self._external_used = set()
        self._master_used = set()
        self.coord_epsg = 6584
        self.coord_surface_factor = 1.0
        self.coord_factor_mode = "ground_to_grid"  # compat: always ground_to_grid (top-layer divide from 0,0), no grid mode
        self.coord_active_epsgs = DEFAULT_ACTIVE_EPSGS[:]
        self.fieldwork_files = []
        self.ne_decimals = 5
        self.elev_decimals = 5

        # Consolidated (.fwk consolidated file) state; the tab exists from startup
        self.edit_file_path = None
        self.edit_dirty = False
        self.edit_table = None
        self.edit_tab = None

        # Field book viewer state — .fwb is read-only, Model A artifact
        self.fieldbook_table = None
        self.fieldbook_tab = None
        self.fieldbook_hint_label = None

        # Check report state — headered .fwc (.chk legacy), 1 list with filter + comments (OID-referenced)
        self.check_report_path = ""
        self.check_dirty = False
        self.check_table = None
        self.check_tab = None
        self.check_hint_label = None
        self.check_filter_combo = None
        self.check_path_label = None

        # Description Parse state — per-point token vs F2F, flag-only, separate tab
        self.desc_parse_table = None
        self.desc_parse_tab = None
        self.desc_parse_hint_label = None
        self.desc_parse_filter_combo = None
        self.desc_parse_path_label = None
        self._desc_parse_all_rows = []

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.status_label = QLabel()
        self.summary_label = QLabel("No Raw Field Data folder set")
        main_layout.addWidget(self.status_label)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        main_layout.addWidget(self.summary_label)

        # Tab 1: Fieldwork
        self.fieldwork_tab = QWidget()
        fw_layout = QVBoxLayout(self.fieldwork_tab)

        top_bar = QHBoxLayout()
        self.select_folder_btn = QPushButton("Select Field Data Folder...")
        self.select_folder_btn.setToolTip("Choose the folder containing raw field CSVs — updates Project Paths")
        self.select_folder_btn.clicked.connect(self._on_select_field_data_folder)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._scan_fieldwork)
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.clicked.connect(self._select_none)
        self.edit_fieldwork_btn = QPushButton("Create Consolidated Field Data...")
        self.edit_fieldwork_btn.clicked.connect(self._edit_fieldwork)
        top_bar.addWidget(self.select_folder_btn)
        top_bar.addWidget(self.refresh_btn)
        top_bar.addWidget(self.select_all_btn)
        top_bar.addWidget(self.select_none_btn)
        top_bar.addWidget(self.edit_fieldwork_btn)
        top_bar.addStretch()
        fw_layout.addLayout(top_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.file_list = QListWidget()
        self.file_list.setMaximumWidth(320)
        self.file_list.itemChanged.connect(self._on_item_changed)
        self.file_list.itemDoubleClicked.connect(self._on_file_double_clicked)

        self.points_table = QTableWidget()
        self.points_table.setColumnCount(len(self.HEADERS))
        self.points_table.setHorizontalHeaderLabels(self.HEADERS)
        self.points_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.points_table.setSortingEnabled(True)
        self.points_table.setAlternatingRowColors(True)
        self.points_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._install_column_hide_show(self.points_table)

        splitter.addWidget(self.file_list)
        splitter.addWidget(self.points_table)
        splitter.setSizes([280, 920])
        fw_layout.addWidget(splitter)

        # Tab 2: Notes
        self.notes_tab = QWidget()
        notes_layout = QVBoxLayout(self.notes_tab)
        self.notes = QTextEdit()
        self.notes.textChanged.connect(self._mark_dirty)
        notes_layout.addWidget(self.notes)

        # Tabs are persistent: closing one only hides it (View menu restores
        # it); the edit + field-book tabs exist from startup even empty.
        self.tabs.addTab(self.fieldwork_tab, "Raw Field Data")
        self.tabs.addTab(self.notes_tab, "Notes")
        self._build_edit_tab()
        # Consolidated stays closed until file created/opened — remove if _build added it
        try:
            idx = self.tabs.indexOf(self.edit_tab)
            if idx != -1:
                self.tabs.removeTab(idx)
        except Exception:
            pass
        self._build_fieldbook_tab()
        self._build_check_report_tab()
        self._build_final_report_tab()
        self._build_desc_parse_tab()
        self._build_line_repair_tab()
        self._build_steps_tab()
        self._build_coord_tab()
        self._build_numbering_error_tab()
        # Wire line table double click? use Fix First button
        try:
            self._install_column_hide_show(self.line_table)
        except: pass
        # Wire clean dialog: double-click Description Error row → Fix/Skip dialog
        try:
            self.desc_parse_table.cellDoubleClicked.connect(self._on_desc_clean_double_clicked)
        except Exception:
            pass
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)

        self._create_menus()
        self._update_status()
        try:
            self._update_view_menu_state()
            self._update_run_checks_enabled()
        except Exception:
            pass
        # Startup page (New/Open) — show only in interactive mode, not offscreen tests
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(200, self._maybe_show_startup_dialog)
        except Exception:
            pass

    # ----- Menus -----
    def _create_menus(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        new_action = file_menu.addAction("New Project (Blank)...")
        new_action.setShortcut("Ctrl+N")
        new_action.setToolTip("Start a fresh blank project — opens Paths popup")
        new_action.triggered.connect(self._on_new_project)
        open_action = file_menu.addAction("Open")
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._open)

        file_menu.addSeparator()

        save_action = file_menu.addAction("Save")
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._save)
        save_as_action = file_menu.addAction("Save As...")
        save_as_action.setShortcut("Ctrl+Shift+S")
        save_as_action.triggered.connect(self._save_as)
        file_menu.addSeparator()

        close_action = file_menu.addAction("Close Project")
        close_action.setShortcut("Ctrl+W")
        close_action.triggered.connect(self._close_project)

        refresh_action = file_menu.addAction("Refresh Raw Field Data")
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self._scan_fieldwork)
        file_menu.addSeparator()

        exit_action = file_menu.addAction("Exit")
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)

        view_menu = menubar.addMenu("View")
        view_fieldwork_action = view_menu.addAction("Raw Field Data")
        view_fieldwork_action.triggered.connect(
            lambda: self._show_tab(self.fieldwork_tab, "Raw Field Data"))
        view_notes_action = view_menu.addAction("Notes")
        view_notes_action.triggered.connect(
            lambda: self._show_tab(self.notes_tab, "Notes"))
        view_edit_action = view_menu.addAction("Consolidated Field Data")
        view_edit_action.triggered.connect(
            lambda: self._show_tab(self.edit_tab, "Consolidated Field Data"))
        view_fieldbook_action = view_menu.addAction("Field Book")
        view_fieldbook_action.triggered.connect(
            lambda: self._show_tab(self.fieldbook_tab, "Field Book"))
        view_check_action = view_menu.addAction("Duplicate Error")
        view_check_action.triggered.connect(
            lambda: self._show_tab(self.check_tab, "Duplicate Error"))
        view_desc_action = view_menu.addAction("Description Error")
        view_desc_action.triggered.connect(
            lambda: self._show_tab(self.desc_parse_tab, "Description Error"))
        view_line_action = view_menu.addAction("Line Repair")
        view_line_action.triggered.connect(
            lambda: self._show_tab(self.line_repair_tab, "Line Repair"))
        view_steps_action = view_menu.addAction("Steps / Workflow Report")
        view_steps_action.triggered.connect(
            lambda: self._show_tab(self.steps_tab, "Steps"))
        view_steps_action.setToolTip("Steps: Fix All Description → Duplicate → Line → Master (Master Protected) → Final Export/KML")
        view_coord_action = view_menu.addAction("Coordinate Settings")
        view_coord_action.triggered.connect(
            lambda: self._show_tab(self.coord_tab, "Coordinate Settings"))
        view_numbering_action = view_menu.addAction("Numbering Error (Master Overlap)")
        view_numbering_action.triggered.connect(
            lambda: self._show_tab(self.numbering_tab, "Numbering Error"))
        view_numbering_action.setToolTip("Shows PtNums that already exist in master file (protected) — must renumber")
        view_coord_action.setToolTip("2011 Texas State Plane + TXDOT SAF (top layer scaler from 0,0), Google Earth (clampToGround)")
        # Store for gray-out logic
        self.view_fieldwork_action = view_fieldwork_action
        self.view_edit_action = view_edit_action
        self.view_fieldbook_action = view_fieldbook_action
        self.view_check_action = view_check_action
        self.view_desc_action = view_desc_action
        self.view_line_action = view_line_action
        self.view_steps_action = view_steps_action
        self.view_coord_action = view_coord_action
        self.view_numbering_action = view_numbering_action
        self.view_check_action.setEnabled(False)
        self.view_desc_action.setEnabled(False)
        self.view_line_action.setEnabled(False)
        view_menu.addSeparator()
        columns_menu = view_menu.addMenu("Columns (Current Tab)")
        self._columns_menu = columns_menu
        show_all_act = columns_menu.addAction("Show All Columns")
        show_all_act.triggered.connect(lambda: self._set_all_columns_visible(self._current_table(), True) if self._current_table() else None)
        hide_menu = columns_menu.addMenu("Hide Column")
        self._hide_columns_submenu = hide_menu
        columns_menu.aboutToShow.connect(self._refresh_columns_menu)
        hint = columns_menu.addAction("Right-click any table header for per-column toggle")
        hint.setEnabled(False)

        tools_menu = menubar.addMenu("Tools")
        convert_action = tools_menu.addAction(f"Convert Field Book (F2F CSV → {FIELDBOOK_EXT})...")
        convert_action.triggered.connect(self._convert_fieldbook)
        tools_menu.addSeparator()
        import_ranges_action = tools_menu.addAction("Import Carlson Used Ranges... (e.g., 1000-10014,10016)")
        import_ranges_action.triggered.connect(self._import_carlson_ranges)
        import_grp_action = tools_menu.addAction("Import Carlson .grp (Point Groups)...")
        import_grp_action.triggered.connect(self._import_grp)
        tools_menu.addSeparator()
        renum_single_action = tools_menu.addAction("Renumber Single Point... (Master Protected)")
        renum_single_action.setToolTip("Pick OID → new number, validated against current + master protected")
        renum_single_action.triggered.connect(self._renumber_single)
        renum_range_action = tools_menu.addAction("Renumber Range... (Master Protected)")
        renum_range_action.setToolTip("Map old range A-B → new start C, validates contiguous free block vs master")
        renum_range_action.triggered.connect(self._renumber_range)
        tools_menu.addSeparator()
        steps_export_csv = tools_menu.addAction("Export Steps Report CSV...")
        steps_export_csv.triggered.connect(self._export_steps_csv)
        steps_export_html = tools_menu.addAction("Export Steps Report HTML (PDF)...")
        steps_export_html.triggered.connect(self._export_steps_html)
        tools_menu.addSeparator()
        kml_export_action = tools_menu.addAction("Export to Google Earth (KML)...")
        kml_export_action.setToolTip("Uses Coordinate Settings (2011 Texas EPSG + TXDOT SAF top-layer from origin 0,0) to create KML — clampToGround; supports selected in Consolidated or any error table via new Tool")
        kml_export_action.triggered.connect(self._export_kml)
        tools_menu.addSeparator()
        # Coordinate System Manager in Tools (per user request) — also in View and via coord tab Manage button
        coord_mgr_tools_action = tools_menu.addAction("Coordinate System Manager... (2011 Texas TXDOT SAF)")
        coord_mgr_tools_action.setToolTip("2011 Texas State Plane ONLY (NAD83 2011, 5 zones USft/m) + TXDOT SAF top-layer scaler from origin 0,0 — Import / Save to project/file")
        coord_mgr_tools_action.triggered.connect(self._on_coord_manage)
        self.coord_mgr_tools_action = coord_mgr_tools_action
        # Send selected errors (any error table, by OID) to Map — imaging check in Google Earth / QGIS
        send_errors_map_action = tools_menu.addAction("Send Selected Errors to Map... (Any Error Table → KML/QGIS)")
        send_errors_map_action.setToolTip("Pick selected rows (by OID) in ANY error table — Duplicate, Description, Line Repair, Numbering, Final Report — and export those points to KML/KMZ (Google Earth) or GeoJSON (QGIS) for imaging check using current 2011 EPSG + SAF (TXDOT top-layer from 0,0) — clampToGround")
        send_errors_map_action.triggered.connect(self._export_selected_errors_to_map)
        self.send_errors_map_action = send_errors_map_action
        master_protect_action = tools_menu.addAction("Show Master Protected Points...")
        master_protect_action.triggered.connect(self._show_master_protected)

        settings_menu = menubar.addMenu("Settings")
        paths_action = settings_menu.addAction("Project Paths...")
        paths_action.triggered.connect(self._open_project_paths)
        precision_action = settings_menu.addAction("Precision...")
        precision_action.triggered.connect(self._open_precision)
        settings_menu.addSeparator()
        rules_action = settings_menu.addAction("Correction Rules...")
        rules_action.setToolTip("Two-column rules: Common Error → Fix (from Field Book). Stored in Field Book file")
        rules_action.triggered.connect(self._open_correction_rules)
        commands_action = settings_menu.addAction("Code Commands...")
        commands_action.setToolTip("Edit Start Line / Start Curve / End Curve / End Line / Close code commands. Stored in Field Book file (fillable then locked)")
        commands_action.triggered.connect(self._open_line_commands)

    # ----- Tab visibility -----
    def _show_tab(self, widget, title):
        """Re-insert a closed tab (if hidden) and focus it.

        removeTab only detaches the widget from the tab bar; the widget and
        everything in it survive, so re-adding it restores the tab as-is.
        """
        if self.tabs.indexOf(widget) == -1:
            self.tabs.addTab(widget, title)
            if widget is self.edit_tab:
                self._update_edit_title()
            if widget is self.fieldbook_tab:
                self._update_fieldbook_title()
            if widget is self.check_tab:
                self._update_check_title()
            if widget is self.desc_parse_tab:
                self._update_desc_parse_title()
        self.tabs.setCurrentWidget(widget)

    def _on_tab_close_requested(self, index):
        """Hide a tab. Dirty consolidated file or check report get Save/Discard/Cancel
        guards first; Canceling leaves the tab open."""
        widget = self.tabs.widget(index)
        if widget is self.edit_tab:
            if not self._confirm_edit_discard():
                return
            self.edit_dirty = False   # guard saved, or user chose Discard
            self._update_edit_title()
        if widget is self.check_tab:
            if not self._confirm_check_discard():
                return
            self.check_dirty = False
            self._update_check_title()
        if widget is self.desc_parse_tab:
            # No dirty, just hide
            pass
        self.tabs.removeTab(index)

    # ----- Column hide/show (general across all tabled info) -----
    def _install_column_hide_show(self, table):
        """Install header right-click menu for hide/show columns (general)."""
        header = table.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # Use closure to capture table
        header.customContextMenuRequested.connect(lambda pos, tbl=table: self._show_header_column_menu(tbl, pos))

    def _show_header_column_menu(self, table, pos):
        """Show per-table column toggle menu at header pos."""
        header = table.horizontalHeader()
        menu = QMenu(self)
        menu.setTitle("Columns")
        # List each column with checkable action
        for col in range(table.columnCount()):
            h_item = table.horizontalHeaderItem(col)
            title = h_item.text() if h_item and h_item.text() else f"Column {col+1}"
            act = menu.addAction(title)
            act.setCheckable(True)
            act.setChecked(not table.isColumnHidden(col))
            # Use lambda with default args to capture col
            act.triggered.connect(lambda checked, c=col, tbl=table: tbl.setColumnHidden(c, not checked))
        menu.addSeparator()
        show_all = menu.addAction("Show All Columns")
        show_all.triggered.connect(lambda: self._set_all_columns_visible(table, True))
        hide_all = menu.addAction("Hide All (Except First)")
        hide_all.triggered.connect(lambda: self._set_all_columns_visible(table, False))
        # Map pos to global and exec
        global_pos = header.mapToGlobal(pos)
        menu.exec(global_pos)

    def _set_all_columns_visible(self, table, visible):
        """Show all columns if visible=True else hide all but first (keeps one visible)."""
        for col in range(table.columnCount()):
            if not visible and col == 0:
                table.setColumnHidden(col, False)
            else:
                table.setColumnHidden(col, not visible)
        # If hiding, ensure at least one stays visible — already handled

    def _current_table(self):
        """Return the QTableWidget of the current tab, if any."""
        w = self.tabs.currentWidget()
        if w is self.fieldwork_tab:
            return self.points_table
        if w is self.edit_tab:
            return self.edit_table
        if w is self.fieldbook_tab:
            return self.fieldbook_table
        if w is self.check_tab:
            return self.check_table
        if w is self.desc_parse_tab:
            return self.desc_parse_table
        return None

    def _refresh_columns_menu(self):
        """Rebuild Hide Column submenu for current table (called on aboutToShow)."""
        if not hasattr(self, '_hide_columns_submenu') or not hasattr(self, '_columns_menu'):
            return
        tbl = self._current_table()
        submenu = self._hide_columns_submenu
        submenu.clear()
        if tbl is None:
            act = submenu.addAction("(no table)")
            act.setEnabled(False)
            return
        for col in range(tbl.columnCount()):
            h = tbl.horizontalHeaderItem(col)
            title = h.text() if h and h.text() else f"Column {col+1}"
            act = submenu.addAction(title)
            act.setCheckable(True)
            act.setChecked(not tbl.isColumnHidden(col))
            act.triggered.connect(lambda checked, c=col, t=tbl: t.setColumnHidden(c, not checked))
        submenu.addSeparator()
        show_all = submenu.addAction("Show All")
        show_all.triggered.connect(lambda: self._set_all_columns_visible(tbl, True))

    # ----- New helpers for UI cleanups -----
    def _on_new_project(self):
        """File > New Project (Blank) — start fresh with Paths popup."""
        if not self._confirm_discard() or not self._confirm_edit_discard() or not self._confirm_check_discard():
            return
        # Clear current state (like close but without extra confirm)
        self._reset_edit_phase()
        self._reset_check_phase()
        self._clear_desc_parse()
        self.current_file = None
        self.project_path = ""
        self.fieldwork_path = ""
        self.fieldbook_path = ""
        self.master_file_path = ""
        self._external_used = set()
        self._master_used = set()
        self.coord_epsg = 6584
        self.coord_surface_factor = 1.0
        self.coord_factor_mode = "ground_to_grid"  # compat: always ground_to_grid (top-layer divide from 0,0), no grid mode
        self.coord_active_epsgs = DEFAULT_ACTIVE_EPSGS[:]
        self.file_list.blockSignals(True)
        self.file_list.clear()
        self.file_list.blockSignals(False)
        self.points_table.setRowCount(0)
        self._refresh_fieldbook_tab()
        self._update_view_menu_state()
        # Show paths popup
        dlg = ProjectPathsDialog(self.project_path, self.fieldwork_path, self.fieldbook_path, getattr(self,'master_file_path',''), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            paths = dlg.get_paths()
            if len(paths)==4:
                self.project_path, self.fieldwork_path, self.fieldbook_path, self.master_file_path = paths
                try:
                    from .config import read_master_ptnums
                    self._master_used = read_master_ptnums(self.master_file_path)
                except: pass
            else:
                self.project_path, self.fieldwork_path, self.fieldbook_path = paths
            self._mark_dirty()
            self._scan_fieldwork()
            self._refresh_fieldbook_tab()
            self._refresh_desc_parse_tab()
            self._update_view_menu_state()
            self._update_run_checks_enabled()

    def _on_select_field_data_folder(self):
        """Raw Field Data > Select Field Data Folder — updates Settings."""
        path = QFileDialog.getExistingDirectory(self, "Select Field Data Folder", self.fieldwork_path or self.project_path or "")
        if path:
            self.fieldwork_path = path
            self._mark_dirty()
            self._scan_fieldwork()
            self._update_view_menu_state()
            self.summary_label.setText(f"Field Data Folder: {path} — {len(self.fieldwork_files)} CSVs found")

    def _update_view_menu_state(self):
        """Gray out consolidated and error tabs when not linked / no check report."""
        try:
            has_consolidated = bool(self.edit_file_path and Path(self.edit_file_path).exists()) or (self.edit_table is not None and self.edit_table.rowCount() > 0)
            has_checks = bool(self.check_report_path and Path(self.check_report_path).exists()) or (self.check_table is not None and self.check_table.rowCount() > 0) or (self.desc_parse_table is not None and self.desc_parse_table.rowCount() > 0)
            if hasattr(self, 'view_edit_action'):
                self.view_edit_action.setEnabled(has_consolidated)
            if hasattr(self, 'view_check_action'):
                self.view_check_action.setEnabled(has_checks)
            if hasattr(self, 'view_desc_action'):
                self.view_desc_action.setEnabled(has_checks)
            if hasattr(self, 'view_line_action'):
                self.view_line_action.setEnabled(has_checks or has_consolidated)
            try:
                if hasattr(self, 'line_repair_tab') and self.line_repair_tab is not None:
                    idxL = self.tabs.indexOf(self.line_repair_tab)
                    if idxL != -1:
                        self.tabs.setTabEnabled(idxL, has_checks or has_consolidated)
            except: pass
            # Gray out tab bar itself (not just View menu) — disable tab until linked
            try:
                if hasattr(self, 'tabs') and hasattr(self, 'edit_tab') and self.edit_tab is not None:
                    idx = self.tabs.indexOf(self.edit_tab)
                    if idx != -1:
                        self.tabs.setTabEnabled(idx, has_consolidated)
                        # Tooltip when disabled
                        self.tabs.setTabToolTip(idx, "" if has_consolidated else "No consolidated file linked — create or open via Raw Field Data tab")
                if hasattr(self, 'check_tab') and self.check_tab is not None:
                    idx2 = self.tabs.indexOf(self.check_tab)
                    if idx2 != -1:
                        # check_tab may be hidden (not in tab bar) — only enable if exists
                        self.tabs.setTabEnabled(idx2, has_checks)
                if hasattr(self, 'desc_parse_tab') and self.desc_parse_tab is not None:
                    idx3 = self.tabs.indexOf(self.desc_parse_tab)
                    if idx3 != -1:
                        self.tabs.setTabEnabled(idx3, has_checks)
            except Exception:
                pass
        except Exception:
            pass

    def _update_run_checks_enabled(self):
        """Enable Run Checks when consolidated file present; Field Book is optional (warns/prompts if missing). Trigger on create AND open — fixes greyed-out when opening .fwk."""
        try:
            has_fb = bool(self.fieldbook_path and Path(self.fieldbook_path).exists()) or (self.fieldbook_table is not None and self.fieldbook_table.rowCount() > 0)
            has_consolidated = bool(self.edit_file_path and Path(self.edit_file_path).exists()) or (self.edit_table is not None and self.edit_table.rowCount() > 0)
            enabled = has_consolidated
            if hasattr(self, 'check_file_btn'):
                self.check_file_btn.setEnabled(enabled)
                if not has_consolidated:
                    self.check_file_btn.setToolTip("Consolidated file required — create or open via Raw Field Data tab first")
                elif not has_fb:
                    self.check_file_btn.setToolTip("Field Book not loaded — click Run Checks to be prompted (Duplicates + Close NE still run, Description parse needs Field Book)")
                else:
                    self.check_file_btn.setToolTip("New Run Checks — starts completely over like never ran before — Duplicates + Close NE + Description parse")
            # Also update the raw/open button states if needed
            if hasattr(self, 'open_check_btn_consolidated'):
                self.open_check_btn_consolidated.setEnabled(True)  # always enabled for relink
        except Exception:
            pass

    # ----- Fieldwork scan -----
    def _scan_fieldwork(self):
        """Rebuild the file list from every .csv under the fieldwork root."""
        self.file_list.blockSignals(True)
        self.file_list.clear()
        self.points_table.setRowCount(0)
        self.fieldwork_files = []

        if not self.fieldwork_path:
            self.summary_label.setText("No Raw Field Data folder set - Go to Settings > Project Paths")
            self.file_list.blockSignals(False)
            return

        root = Path(self.fieldwork_path)
        if not root.exists():
            self.summary_label.setText(f"Field Data Folder does not exist: {root}")
            self.file_list.blockSignals(False)
            return

        allowed = {".csv"}
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in allowed:
                self.fieldwork_files.append(p)

        if not self.fieldwork_files:
            self.summary_label.setText(f"No CSV files found under: {root}")
            self.file_list.blockSignals(False)
            return

        for f in sorted(self.fieldwork_files):
            item = QListWidgetItem(f"{f.name} ({f.parent.name})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, str(f))
            self.file_list.addItem(item)

        self.file_list.blockSignals(False)
        self._update_combined_points()

    # ----- File selection -----
    def _select_all(self):
        self.file_list.blockSignals(True)
        for i in range(self.file_list.count()):
            self.file_list.item(i).setCheckState(Qt.CheckState.Checked)
        self.file_list.blockSignals(False)
        self._update_combined_points()

    def _select_none(self):
        self.file_list.blockSignals(True)
        for i in range(self.file_list.count()):
            self.file_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.file_list.blockSignals(False)
        self._update_combined_points()

    def _on_item_changed(self, item):
        self._update_combined_points()

    # ----- Table helpers -----
    def _format_decimal(self, num, decimals):
        """Round half-up (survey convention) to a fixed decimal count.

        Decimal(str(num)) parses the printed value, so binary-float
        artifacts (round(2.675, 2) -> 2.67) cannot leak through.
        """
        if num is None:
            return ""
        quantum = Decimal(1).scaleb(-decimals)
        rounded = Decimal(str(num)).quantize(quantum, rounding=ROUND_HALF_UP)
        return f"{rounded:.{decimals}f}"

    def _row_texts(self, table, r):
        """Return the display text of every cell in table row r."""
        texts = []
        for c in range(table.columnCount()):
            item = table.item(r, c)
            texts.append(item.text() if item else "")
        return texts

    def _fill_row(self, r, row, parent_name, rel):
        """Fill one points-table row from a 5-column raw CSV row."""
        # 0 Point Number: natural sort, letters-first (a2, a4, a20, 1, 3, 6a, 6b, 15)
        self.points_table.setItem(
            r, 0, NaturalSortItem(row[0] if len(row) > 0 else "", letters_first=True))

        # 1-3 N/E/Z: numeric sort, half-up rounded display
        for col, decimals, value in (
            (1, self.ne_decimals,   row[1] if len(row) > 1 else None),
            (2, self.ne_decimals,   row[2] if len(row) > 2 else None),
            (3, self.elev_decimals, row[3] if len(row) > 3 else None),
        ):
            item = NumericSortItem(value)
            item.setText(self._format_decimal(item.numeric_value(), decimals))
            self.points_table.setItem(r, col, item)

        # 4 Description: natural sort, numbers-first (1, 2, 3, a, b, c)
        self.points_table.setItem(
            r, 4, NaturalSortItem(row[4] if len(row) > 4 else "", letters_first=False))

        # 5 Parent Folder / 6 Source File: plain text provenance
        self.points_table.setItem(r, 5, QTableWidgetItem(parent_name))
        self.points_table.setItem(r, 6, QTableWidgetItem(str(rel)))

    def _get_display_paths(self, file_path: Path, root):
        """Return (relative folder, file name) for the provenance columns."""
        file_name = file_path.name
        if root is None:
            return str(file_path.parent), file_name
        try:
            rel_parent = file_path.relative_to(root).parent
            folder_path = "" if str(rel_parent) == "." else str(rel_parent)
            return folder_path, file_name
        except ValueError:
            return str(file_path.parent), file_name

    # ----- Points table views -----
    def _update_combined_points(self):
        """Rebuild the combined table from the checked files."""
        self.points_table.setSortingEnabled(False)
        self.points_table.setRowCount(0)
        total_points = 0
        checked_files = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                path_str = item.data(Qt.ItemDataRole.UserRole)
                if path_str:
                    checked_files.append(Path(path_str))
        root = Path(self.fieldwork_path) if self.fieldwork_path else None
        for file_path in checked_files:
            points = self._read_one_file(file_path)
            parent_name = file_path.parent.name
            try:
                rel = file_path.relative_to(root) if root else file_path.name
            except ValueError:
                rel = file_path.name
            for row in points:
                r = self.points_table.rowCount()
                self.points_table.insertRow(r)
                self._fill_row(r, row, parent_name, rel)
                total_points += 1
        self.points_table.resizeColumnsToContents()
        self.points_table.setSortingEnabled(True)
        self.summary_label.setText(
            f"Showing {len(checked_files)} of {len(self.fieldwork_files)} files, {total_points} points")

    def _on_file_double_clicked(self, item):
        """Temporary single-file view for the double-clicked file."""
        path_str = item.data(Qt.ItemDataRole.UserRole)
        if not path_str:
            return

        file_path = Path(path_str)
        points = self._read_one_file(file_path)
        root = Path(self.fieldwork_path) if self.fieldwork_path else None
        folder_path, file_name = self._get_display_paths(file_path, root)

        self.points_table.setSortingEnabled(False)
        self.points_table.setRowCount(0)

        for row in points:
            r = self.points_table.rowCount()
            self.points_table.insertRow(r)
            self._fill_row(r, row, folder_path, file_name)

        self.points_table.resizeColumnsToContents()
        self.points_table.setSortingEnabled(True)
        self.summary_label.setText(
            f"Viewing {len(points)} points from {file_path.name}. "
            "Toggle a checkbox or click Refresh to return to combined view.")

    # ----- Raw CSV reading -----
    def _read_one_file(self, file_path: Path):
        """Read a headerless 5-column point CSV; commas beyond column 5 are
        re-joined into the Description."""
        rows = []
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore', newline='') as f:
                reader = csv.reader(f)
                for raw in reader:
                    if not raw or len(raw) == 0:
                        continue
                    raw = [c.strip() for c in raw]
                    if len(raw) < 5:
                        continue
                    if len(raw) > 5:
                        point = raw[:4] + [",".join(raw[4:])]
                    else:
                        point = raw
                    rows.append(point)
        except Exception as e:
            print(f"Failed to read {file_path}: {e}")
        return rows

    # ----- Consolidated Field Data: export working CSV + open edit tab -----
    def _edit_fieldwork(self):
        """Export the current table as the consolidated file and open it
        in the consolidated tab. Rows are written in canonical order (Source File
        path, then Point Number, both natural letters-first) and OID 1..N is
        assigned after sorting. Column contract is append-only:
        OID, Point Number, N, E, Z, Description, Parent Folder, Source File."""
        if not self.fieldwork_path or not Path(self.fieldwork_path).exists():
            QMessageBox.warning(self, "Consolidated Field Data Error",
                                "No Field Data Folder selected — click 'Select Field Data Folder...' on the Raw Field Data tab first.")
            return
        if self.points_table.rowCount() == 0:
            QMessageBox.warning(self, "Consolidated Field Data Error",
                                "No field data to consolidate — select a folder with CSVs and ensure at least one file is checked, then click Create.")
            return
        # Starting a new consolidated file must not silently kill unsaved edits.
        if self.edit_table is not None and not self._confirm_edit_discard():
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Create Consolidated Field Data - Save Working File", self.project_path,
            f"Consolidated Working File (*{WORKING_FILE_EXT})")
        if not path:
            return
        if Path(path).suffix.lower() != WORKING_FILE_EXT:
            path += WORKING_FILE_EXT

        rows = [self._row_texts(self.points_table, r)
                for r in range(self.points_table.rowCount())]
        rows.sort(key=lambda row: (
            natural_key(row[6], letters_first=True),   # Source File path
            natural_key(row[0], letters_first=True),   # Point Number
        ))
        working_rows = [[str(oid)] + row for oid, row in enumerate(rows, start=1)]

        if not self._write_working_rows(working_rows, path):
            return
        self.edit_file_path = path
        self.edit_dirty = False
        self._mark_dirty()   # the project now references this file
        self._open_edit_tab(working_rows)
        self.summary_label.setText(
            f"Consolidated data created: {len(working_rows)} points -> {os.path.basename(path)}")

    # ----- Consolidated Field Data tab (consolidated file view) -----
    def _build_edit_tab(self):
        """Create the edit-phase tab once; reused for every consolidated file."""
        self.edit_tab = QWidget()
        layout = QVBoxLayout(self.edit_tab)

        top_bar = QHBoxLayout()
        self.check_file_btn = QPushButton("New Run Checks")
        self.check_file_btn.setToolTip("New Run Checks — starts completely over like never ran before — Duplicates + Close NE + Description parse")
        self.check_file_btn.clicked.connect(self._run_all_checks)
        # (Renumber Dups / Global / Line Repair buttons removed per request — use Duplicate tab Renumber / Global in Settings / Line tab)
        self.open_check_btn_consolidated = QPushButton("Open Check Report...")
        self.open_check_btn_consolidated.setToolTip("Open an existing check report (.fwc, .chk legacy) — relink even if checks tabs are hidden/grayed")
        self.open_check_btn_consolidated.clicked.connect(self._on_load_check_report)
        top_bar.addWidget(self.check_file_btn)
        top_bar.addWidget(self.open_check_btn_consolidated)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        self.edit_hint_label = QLabel(
            "No consolidated data loaded - create from Raw Field Data via 'Create Consolidated Field Data...' "
            "or open via File > Load Consolidated Field Data (Ctrl+E)")
        layout.addWidget(self.edit_hint_label)

        # Consolidated file now has Corr_* metadata cols for renumber suggestions (hidden, not in final 5-col CSV)
        self.CORR_HEADERS = ["Corr_PtNum", "Corr_Type", "Corr_Status", "Corr_Detail", "Dup_Renumber", "Global_Renumber", "Final_PtNum"]
        edit_headers = ["OID"] + self.HEADERS + self.CORR_HEADERS
        self.edit_table = QTableWidget()
        self.edit_table.setColumnCount(len(edit_headers))
        self.edit_table.setHorizontalHeaderLabels(edit_headers)
        self.edit_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.edit_table.setSortingEnabled(True)
        self.edit_table.setAlternatingRowColors(True)
        self.edit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.edit_table.verticalHeader().setVisible(False)   # OID replaces row numbers
        self._install_column_hide_show(self.edit_table)
        # Hide Corr_* cols by default (Cols 8-11) — toggle via Columns menu
        for ci in range(len(edit_headers)-len(self.CORR_HEADERS), len(edit_headers)):
            self.edit_table.setColumnHidden(ci, True)
        # Context menu for renumber suggestions on PtNum
        self.edit_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.edit_table.customContextMenuRequested.connect(self._on_edit_table_context_menu)
        layout.addWidget(self.edit_table)

        # Closed until file created/opened — do NOT addTab here (shown via _open_edit_tab/_show_tab)
        # self.tabs.addTab(self.edit_tab, "Consolidated Field Data")  # deferred

    def _open_edit_tab(self, working_rows):
        self._fill_edit_table(working_rows)
        self._show_tab(self.edit_tab, "Consolidated Field Data")   # restore if hidden
        self._update_edit_title()
        try:
            self._update_view_menu_state()
            self._update_run_checks_enabled()
        except Exception:
            pass

    def _fill_edit_table(self, working_rows):
        """Fill the edit table from 8- or 12-col working rows (OID first + Corr_*).

        Kept separate from _fill_row: the consolidated file has an extra OID
        column, and cell text is shown as stored (precision was locked at
        export time). Corr_* cols are hidden by default.
        """
        self.edit_table.setSortingEnabled(False)
        self.edit_table.setRowCount(0)
        for row in working_rows:
            r = self.edit_table.rowCount()
            self.edit_table.insertRow(r)
            # Ensure row has at least 12 cols (pad Corr_* if legacy 8-col)
            if len(row) < 15:
                row = row + [""] * (15 - len(row))
            oid_item = NumericSortItem(row[0])                       # 0 OID
            oid_item.setText(row[0])
            self.edit_table.setItem(r, 0, oid_item)
            self.edit_table.setItem(
                r, 1, NaturalSortItem(row[1], letters_first=True))   # Point Number
            for col in (2, 3, 4):                                    # N / E / Z
                item = NumericSortItem(row[col])
                item.setText(row[col])
                self.edit_table.setItem(r, col, item)
            self.edit_table.setItem(
                r, 5, NaturalSortItem(row[5], letters_first=False))  # Description
            self.edit_table.setItem(r, 6, QTableWidgetItem(row[6]))  # Parent Folder
            self.edit_table.setItem(r, 7, QTableWidgetItem(row[7]))  # Source File
            # Corr_* hidden cols 8-11
            for ci, val in enumerate(row[8:15], start=8):
                it = QTableWidgetItem(val)
                if ci == 9:  # Corr_Type
                    it.setToolTip("Corr_Type: hole/at_end/manual/inject/group")
                self.edit_table.setItem(r, ci, it)
        self.edit_table.resizeColumnsToContents()
        self.edit_table.setSortingEnabled(True)
        self._update_edit_hint()


    # ----- Send Selected Errors (any error table, by OID) to Map — NEW 2026-09-25 per user request -----
    def _collect_selected_oids_from_all_tables(self):
        """Collect OIDs from selected rows in ANY error/related table (by OID), deduped.
        Scans Duplicate (check_table col2), Description (desc_parse_table col0),
        Line Repair (line_table col2), Numbering (numbering_table col0),
        Final Report (final_report_table col3), Consolidated (edit_table col0),
        returns {oid: {source, row, pt, n, e, z, desc}} and list of oid strings.
        """
        oid_map = {}
        oid_set = set()
        def add_from_table(table, oid_col, name, n_col=None, e_col=None, z_col=None, pt_col=None, desc_col=None):
            if table is None or not hasattr(table, 'selectedItems') or not table.selectedItems():
                return 0
            try:
                rows = {it.row() for it in table.selectedItems()}
            except:
                return 0
            cnt=0
            for r in rows:
                try:
                    oid_item = table.item(r, oid_col)
                    if not oid_item:
                        continue
                    oid = oid_item.text().strip()
                    if not oid:
                        continue
                    if oid in oid_map:
                        continue
                    pt = ""
                    n = ""
                    e = ""
                    z = ""
                    desc = ""
                    try:
                        if pt_col is not None and table.item(r, pt_col):
                            pt = table.item(r, pt_col).text().strip()
                        if n_col is not None and table.item(r, n_col):
                            n = table.item(r, n_col).text().strip()
                        if e_col is not None and table.item(r, e_col):
                            e = table.item(r, e_col).text().strip()
                        if z_col is not None and table.item(r, z_col):
                            z = table.item(r, z_col).text().strip()
                        if desc_col is not None and table.item(r, desc_col):
                            desc = table.item(r, desc_col).text().strip()
                    except:
                        pass
                    oid_map[oid] = {"source": name, "row": r, "pt": pt, "n": n, "e": e, "z": z, "desc": desc, "table": table}
                    oid_set.add(oid)
                    cnt+=1
                except:
                    continue
            return cnt

        try: add_from_table(getattr(self, 'check_table', None), 2, "Duplicate Error", n_col=4, e_col=5, z_col=6, pt_col=3, desc_col=7)
        except: pass
        try: add_from_table(getattr(self, 'desc_parse_table', None), 0, "Description Error", n_col=2, e_col=3, z_col=4, pt_col=1, desc_col=5)
        except: pass
        try: add_from_table(getattr(self, 'line_table', None), 2, "Line Repair", pt_col=3, desc_col=5)
        except: pass
        try: add_from_table(getattr(self, 'numbering_table', None), 0, "Numbering Error", n_col=2, e_col=3, z_col=4, pt_col=1, desc_col=5)
        except: pass
        try: add_from_table(getattr(self, 'final_report_table', None), 3, "Final Report", desc_col=5)
        except: pass
        try: add_from_table(getattr(self, 'edit_table', None), 0, "Consolidated", n_col=2, e_col=3, z_col=4, pt_col=1, desc_col=5)
        except: pass
        return oid_map, sorted(oid_set, key=lambda x: int(x) if str(x).isdigit() else str(x))

    def _write_geojson(self, dest, working_rows, epsg, factor, mode, is_ground=False):
        """Write GeoJSON for QGIS — points in WGS84 (EPSG:4326) using same SAF/EPSG transform as KML."""
        import json
        try:
            from .coord_systems import convert_to_wgs84
        except:
            convert_to_wgs84 = lambda n,e,epsg,factor,mode, **kw: (None,None)
        features=[]
        skipped=0
        for row in working_rows:
            if len(row)<5:
                continue
            oid=row[0]; pt=row[1]; n=row[2]; e=row[3]; z=row[4] if len(row)>4 else ""; desc=row[5] if len(row)>5 else ""
            try:
                lon, lat = convert_to_wgs84(n, e, epsg, factor, mode, is_ground=is_ground)
            except:
                lon, lat = None, None
            if lon is None or lat is None:
                if int(epsg)==4326:
                    try:
                        lat=float(n); lon=float(e)
                    except:
                        skipped+=1; continue
                else:
                    skipped+=1; continue
            props = {"OID": oid, "PtNum": pt, "N": n, "E": e, "Z": z, "Desc": desc, "EPSG": epsg, "SAF": factor, "Mode": mode, "IsGround": bool(is_ground)}
            try:
                if len(row)>7 and row[7]:
                    props["ErrorSource"] = row[7]
                if len(row)>8 and row[8]:
                    props["IssueType"] = row[8]
            except: pass
            features.append({"type":"Feature","geometry":{"type":"Point","coordinates":[lon, lat]},"properties":props})
        gj={"type":"FeatureCollection","name": dest.stem, "features": features, "crs": {"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::4326"}}, "_meta": {"EPSG": epsg, "SAF": factor, "mode": mode, "clampToGround": True, "skipped": skipped}}
        try:
            dest.write_text(json.dumps(gj, indent=2), encoding="utf-8")
            return True, len(features), skipped
        except Exception as ex:
            print(f"GeoJSON write failed {ex}")
            return False, 0, skipped

    def _export_selected_errors_to_map(self):
        """Tools > Send Selected Errors to Map — selected rows (by OID) in ANY error table → KML/KMZ (Google Earth) or GeoJSON (QGIS) for imaging check.
        Uses current 2011 Texas EPSG + TXDOT SAF (top-layer from origin 0,0) same as main KML export, clampToGround.
        """
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox, QInputDialog
        oid_map, oid_list = self._collect_selected_oids_from_all_tables()
        if not oid_list:
            QMessageBox.information(self, "Send Selected Errors to Map",
                "No rows selected in any error table.\n\n"
                "Select rows (by OID) in ANY error table — Duplicate Error, Description Error, Line Repair, Numbering Error (Master Overlap), or Final Report — "
                "using Ctrl+Click / Shift+Click (row selection), then use Tools > Send Selected Errors to Map.\n\n"
                "Those OIDs will be looked up in Consolidated Field Data (N/E) and exported with your current Coordinate System Manager EPSG (2011 Texas) + TXDOT SAF (from origin 0,0, clampToGround) for imaging check in Google Earth or QGIS.\n\n"
                "Tip: You can also select in Consolidated directly.")
            return
        epsg = getattr(self, "coord_epsg", 6584)
        try:
            if hasattr(self, "coord_epsg_combo") and self.coord_epsg_combo.count()>0:
                epsg = self.coord_epsg_combo.currentData()
                self.coord_epsg = epsg
        except: pass
        saf = getattr(self, "coord_surface_factor", 1.0)
        try:
            if hasattr(self, "coord_csf_edit"):
                saf = float(self.coord_csf_edit.text().strip()) if self.coord_csf_edit.text().strip() else 1.0
                self.coord_surface_factor = saf
        except: saf = 1.0
        factor = saf
        is_ground = False
        try:
            if hasattr(self, "coord_is_ground_cb"):
                is_ground = self.coord_is_ground_cb.isChecked()
                mode = "ground_to_grid" if is_ground else "grid_to_ground"
                self.coord_factor_mode = mode
            elif hasattr(self, "coord_mode_combo"):
                mode = "ground_to_grid" if self.coord_mode_combo.currentIndex()==0 else "grid_to_ground"
                is_ground = (mode == "ground_to_grid")
                self.coord_factor_mode = mode
            else:
                mode = getattr(self, "coord_factor_mode", "ground_to_grid" if abs(saf-1.0)>1e-12 else "grid_to_ground")
                is_ground = (mode == "ground_to_grid")
        except:
            mode = "ground_to_grid" if abs(saf-1.0)>1e-12 else "grid_to_ground"
            is_ground = (mode == "ground_to_grid")
        consol_by_oid = {}
        try:
            if self.edit_table and self.edit_table.rowCount()>0:
                for r in range(self.edit_table.rowCount()):
                    oid_it = self.edit_table.item(r, 0)
                    if not oid_it: continue
                    oid = oid_it.text().strip()
                    if not oid: continue
                    row=[]
                    for c in range(min(8, self.edit_table.columnCount())):
                        it = self.edit_table.item(r,c)
                        row.append(it.text().strip() if it else "")
                    consol_by_oid[oid]=row
        except: pass
        working_rows=[]
        missing=[]
        for oid in oid_list:
            if oid in consol_by_oid:
                wr = consol_by_oid[oid][:]
                src = oid_map.get(oid,{}).get("source","")
                issue = ""
                try:
                    tbl = oid_map.get(oid,{}).get("table")
                    r0 = oid_map.get(oid,{}).get("row")
                    if tbl is not None and r0 is not None:
                        if tbl is getattr(self,'check_table',None) and tbl.item(r0,1):
                            issue = tbl.item(r0,1).text().strip()
                        elif tbl is getattr(self,'line_table',None) and tbl.item(r0,1):
                            issue_item = tbl.item(r0,1)
                            issue = str(issue_item.data(Qt.UserRole) or issue_item.text()).strip()
                except: pass
                while len(wr)<7: wr.append("")
                if len(wr)==7: wr.append(src)
                elif len(wr)>7: wr[7]=src
                if len(wr)==8: wr.append(issue)
                elif len(wr)>8: wr[8]=issue
                working_rows.append(wr)
            else:
                info = oid_map.get(oid,{})
                n = info.get("n","")
                e = info.get("e","")
                pt = info.get("pt","")
                z = info.get("z","")
                desc = info.get("desc","")
                src = info.get("source","")
                if n and e:
                    try:
                        float(n); float(e)
                        row=[oid, pt, n, e, z, desc, "", src]
                        working_rows.append(row)
                    except:
                        missing.append(oid)
                else:
                    missing.append(oid)
        if not working_rows:
            QMessageBox.warning(self, "Send Selected Errors to Map",
                f"Selected {len(oid_list)} OID(s) but none have coordinates in Consolidated.\n\nMissing: {', '.join(missing[:20])}\n\nEnsure Consolidated Field Data is loaded (Edit > Create Consolidated) and those OIDs exist.")
            return
        base = str(Path(getattr(self,"current_file","") or getattr(self,"project_path","") or "."))
        try:
            pbase = Path(base)
            if pbase.is_file():
                base = str(pbase.parent)
            elif not pbase.exists():
                base = "."
        except: base = "."
        default = str(Path(base) / f"Selected_Errors_Map_{len(working_rows)}pts")
        first_src = list(oid_map.values())[0].get("source","error") if oid_map else "error"
        path, selected_filter = QFileDialog.getSaveFileName(self,
            f"Send {len(working_rows)} Selected Error Points to Map (by OID — {first_src} + others) — Choose format for Google Earth (KML/KMZ) or QGIS (GeoJSON)",
            default,
            "Google Earth KML (*.kml);;Google Earth KMZ (*.kmz);;QGIS GeoJSON (*.geojson);;CSV with lat/lon (*.csv)")
        if not path:
            return
        p = Path(path)
        ext = p.suffix.lower()
        if ext not in (".kml",".kmz",".geojson",".json",".csv"):
            if "KMZ" in selected_filter:
                p = p.with_suffix(".kmz"); ext=".kmz"
            elif "GeoJSON" in selected_filter:
                p = p.with_suffix(".geojson"); ext=".geojson"
            elif "CSV" in selected_filter:
                p = p.with_suffix(".csv"); ext=".csv"
            else:
                p = p.with_suffix(".kml"); ext=".kml"
        else:
            if ext==".json":
                ext=".geojson"
        try:
            import pyproj
            has_pyproj = True
        except:
            has_pyproj = False
            if ext in (".kml",".kmz"):
                ret = QMessageBox.warning(self, "KML — pyproj Missing", "pyproj not installed — needed for State Plane → WGS84.\n\nInstall: pip install pyproj\n\nContinue anyway / Cancel?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
                if ret != QMessageBox.StandardButton.Yes:
                    return
        ok=False
        if ext in (".kml",".kmz"):
            ok = self._write_kml(p, working_rows, epsg, factor, mode, has_pyproj, is_ground)
            kind = "KMZ" if ext==".kmz" else "KML"
            if ok:
                saf_label = f"SAF {factor} ({'Ground' if is_ground else 'Grid'} from 0,0)"
                sources = {}
                for oid in oid_list:
                    src = oid_map.get(oid,{}).get("source","?")
                    sources[src]=sources.get(src,0)+1
                src_txt = ", ".join([f"{k}:{v}" for k,v in sources.items()])
                missing_text = ("\nMissing OIDs (no coords): " + ", ".join(missing)) if missing else ""
                QMessageBox.information(self, kind, f"Exported {kind} for imaging check:\n{p}\nEPSG {epsg} {saf_label} TXDOT origin 0,0 clampToGround\nPoints: {len(working_rows)} (selected OIDs: {len(oid_list)}, sources {src_txt}){missing_text}\n\nOpen in Google Earth to verify vs imagery.")
                self.summary_label.setText(f"{kind} (selected errors) exported: {p.name} ({len(working_rows)} pts, {src_txt}, EPSG {epsg}, {saf_label})")
                try: self._refresh_steps()
                except: pass
            else:
                QMessageBox.warning(self, kind, f"Failed to write {p}")
        elif ext==".geojson":
            ok, feat_count, skipped = self._write_geojson(p, working_rows, epsg, factor, mode, is_ground)
            if ok:
                saf_label = f"SAF {factor} ({'Ground' if is_ground else 'Grid'} from 0,0)"
                sources = {}
                for oid in oid_list:
                    src = oid_map.get(oid,{}).get("source","?")
                    sources[src]=sources.get(src,0)+1
                src_txt = ", ".join([f"{k}:{v}" for k,v in sources.items()])
                QMessageBox.information(self, "GeoJSON (QGIS)", f"Exported QGIS GeoJSON:\n{p}\nEPSG {epsg} {saf_label} TXDOT origin 0,0\nFeatures: {feat_count} (selected {len(oid_list)}, sources {src_txt}, skipped {skipped})\n\nDrag into QGIS — it will appear in WGS84 (EPSG:4326) over imagery (e.g., Google Satellite, ESRI).")
                self.summary_label.setText(f"GeoJSON (selected errors) exported: {p.name} ({feat_count} feats, {src_txt}, EPSG {epsg}, {saf_label})")
            else:
                QMessageBox.warning(self, "GeoJSON", f"Failed to write {p}")
        elif ext==".csv":
            import csv
            try:
                from .coord_systems import convert_to_wgs84
            except:
                convert_to_wgs84 = lambda n,e,epsg,factor,mode, **kw: (None,None)
            with p.open("w", newline="", encoding="utf-8") as f:
                w=csv.writer(f)
                w.writerow(["OID","PtNum","N","E","Z","Desc","Source","IssueType","Lon_WGS84","Lat_WGS84","EPSG","SAF","Mode"])
                cnt=0
                for row in working_rows:
                    oid=row[0] if len(row)>0 else ""; pt=row[1] if len(row)>1 else ""; n=row[2] if len(row)>2 else ""; e=row[3] if len(row)>3 else ""; z=row[4] if len(row)>4 else ""; desc=row[5] if len(row)>5 else ""; src=row[7] if len(row)>7 else ""; issue=row[8] if len(row)>8 else ""
                    try:
                        lon, lat = convert_to_wgs84(n,e,epsg,factor,mode, is_ground=is_ground)
                    except:
                        lon, lat = "", ""
                    if lon is None: lon=""
                    if lat is None: lat=""
                    w.writerow([oid,pt,n,e,z,desc,src,issue, lon, lat, epsg, factor, mode])
                    cnt+=1
                ok=True
            if ok:
                QMessageBox.information(self, "CSV", f"Exported CSV with lat/lon:\n{p}\nRows: {cnt} (EPSG {epsg} SAF {factor} {mode})\n\nOpen in QGIS via Add Delimited Text Layer (X=Lon_WGS84 Y=Lat_WGS84, CRS EPSG:4326) or Excel.")
                self.summary_label.setText(f"CSV (selected errors) exported: {p.name} ({cnt} rows, EPSG {epsg})")
        if missing and ok:
            if len(missing) <= 20:
                QMessageBox.warning(self, "Some OIDs Had No Coordinates", f"Exported {len(working_rows)} points, but {len(missing)} selected OID(s) could not be located in Consolidated and had no N/E in their error table: {', '.join(missing)}")

    def _on_error_table_context_menu(self, table, pos):
        """Right-click on any error table body — offer Send Selected to Map (by OID) for imaging check."""
        # Determine which table and how many selected
        name_map = {
            getattr(self, 'check_table', None): "Duplicate Error",
            getattr(self, 'desc_parse_table', None): "Description Error",
            getattr(self, 'line_table', None): "Line Repair",
            getattr(self, 'numbering_table', None): "Numbering Error",
            getattr(self, 'final_report_table', None): "Final Report",
            getattr(self, 'edit_table', None): "Consolidated",
        }
        # Only show if there is a selection in that table (or any error table)
        try:
            if table is None or not hasattr(table, 'selectedItems') or not table.selectedItems():
                # Also check if any error table has selection — still offer via generic handler
                oid_map, oid_list = self._collect_selected_oids_from_all_tables()
                if not oid_list:
                    return
                # Show menu at table pos anyway
                n = len(oid_list)
            else:
                # Count selected rows in this specific table
                rows = {it.row() for it in table.selectedItems()}
                n = len(rows)
                # Also total across all tables for label
                _, all_oids = self._collect_selected_oids_from_all_tables()
                total = len(all_oids)
                if total != n:
                    n = total  # show total
            from PySide6.QtWidgets import QMenu
            menu = QMenu(self)
            title = menu.addAction(f"📍 Send Selected ({n}) to Map... (Google Earth KML / QGIS GeoJSON)")
            title.setToolTip("Export selected rows (by OID) from ANY error table — Duplicate, Description, Line, Numbering — using 2011 Texas EPSG + TXDOT SAF (from origin 0,0, clampToGround) for imaging check")
            # Also offer Coordinate System Manager for convenience
            coord_act = menu.addAction("⚙ Coordinate System Manager... (2011 Texas TXDOT SAF)")
            menu.addSeparator()
            # Show menu
            action = menu.exec(table.mapToGlobal(pos))
            if action == title:
                self._export_selected_errors_to_map()
            elif action == coord_act:
                self._on_coord_manage()
        except Exception as ex:
            print(f"error table context menu failed: {ex}")

    def _on_edit_table_context_menu(self, pos):
        """Right-click on Consolidated table — renumber suggestions + range tools + Send Selected to Map (any OID)."""
        table = self.edit_table
        item = table.itemAt(pos)
        # If click on empty area or not PtNum/OID, still allow Send Selected to Map via generic handler
        # Check for selected rows first for map feature — if multiple selected, offer map regardless of col
        try:
            # If there are selected rows (any col), offer Send to Map as first option even if col not 0/1
            has_sel = bool(table.selectedItems())
            if has_sel:
                # Show quick map menu if right-click not on OID/PtNum? We'll still show map option
                from PySide6.QtWidgets import QMenu
                # If col is not OID/PtNum, show only map menu
                if item is None or item.column() not in (0,1):
                    # Use generic error-table menu style for map
                    self._on_error_table_context_menu(table, pos)
                    return
        except:
            pass
        if item is None:
            return
        row = item.row(); col = item.column()
        # Only for PtNum col (1) or OID col (0) for renumber, but also include map
        if col not in (0,1):
            return
        # Must be clean descriptions to renumber (locked)
        if self.desc_parse_table is not None and self.desc_parse_table.rowCount() > 0:
            QMessageBox.warning(self, "Renumber Locked", f"Descriptions not clean — {self.desc_parse_table.rowCount()} flagged rows remain. Double-click each in Description Error to Fix/Skip before renumbering (keeps line order).")
            return
        # Gather used numbers (local + external Carlson ranges)
        used = set()
        external = getattr(self, '_external_used', set())
        for r in range(table.rowCount()):
            it = table.item(r, 1)
            if it and it.text().strip().isdigit():
                try: used.add(int(it.text().strip()))
                except: pass
            # Also Corr_PtNum if accepted (col 8)
            corr_it = table.item(r, 8)
            if corr_it and corr_it.text().strip().isdigit():
                try: used.add(int(corr_it.text().strip()))
                except: pass
        cur_it = table.item(row, 1)
        cur_pt = cur_it.text().strip() if cur_it else ""
        oid_it = table.item(row, 0)
        oid = oid_it.text().strip() if oid_it else ""
        # Offer menu: Renumber vs Send Selected to Map (by OID) — so same right-click can do imaging check
        try:
            from PySide6.QtWidgets import QMenu
            from PySide6.QtGui import QAction
            menu2 = QMenu(self)
            ren_act = menu2.addAction(f"Renumber Point OID {oid} ({cur_pt})...")
            ren_act.setToolTip("Renumber suggestion (master protected, crew blocks)")
            # Count selected for map
            try:
                oid_map_tmp2, oid_list_tmp2 = self._collect_selected_oids_from_all_tables()
                sel_n = len(oid_list_tmp2)
            except:
                sel_n = 0
            if sel_n == 0:
                # fallback to this single row
                sel_n = 1
                oid_list_tmp2 = [oid]
            map_act2 = menu2.addAction(f"📍 Send Selected ({sel_n}) to Map... (KML/QGIS)")
            map_act2.setToolTip("Send selected rows (by OID) — from any error table or this Consolidated selection — to KML (Google Earth) / GeoJSON (QGIS) for imaging check via current 2011 EPSG + SAF")
            # Also include Coordinate System Manager for convenience
            coord_act2 = menu2.addAction("⚙ Coordinate System Manager...")
            chosen = menu2.exec(table.mapToGlobal(pos))
            if chosen == map_act2:
                self._export_selected_errors_to_map()
                return
            elif chosen == coord_act2:
                self._on_coord_manage()
                return
            elif chosen != ren_act:
                return
        except Exception as ex:
            print(f"consolidated menu failed {ex}")
            pass
        crew = DEFAULT_CREW_NUMBER
        from .clean import RenumberSuggestionDialog
        dlg = RenumberSuggestionDialog(oid, cur_pt, crew, used, external, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_new_pt:
            return
        # Write to Corr_* metadata cols (8-11), not directly to PtNum
        table.item(row, 8).setText(dlg.result_new_pt)
        table.item(row, 9).setText(dlg.result_type or "")
        table.item(row, 10).setText("Suggested")
        table.item(row, 11).setText(dlg.result_detail or "")
        # If inject, shift all after (suggest range)
        if dlg.result_type == "inject":
            try:
                inject_n = int(dlg.result_new_pt)
                # Preview shift tail — for now just mark subsequent rows' Corr_Detail
                for rr in range(table.rowCount()):
                    if rr == row: continue
                    it2 = table.item(rr, 1)
                    if it2 and it2.text().strip().isdigit() and int(it2.text().strip()) >= inject_n:
                        table.item(rr, 11).setText(f"tail shift preview: {it2.text()} -> {int(it2.text())+1} (if inject accepted)")
            except: pass
        self.edit_dirty = True
        self._update_edit_title()
        # Unhide Corr cols briefly so user sees metadata was added
        for ci in range(8,15):
            table.setColumnHidden(ci, False)
        self.summary_label.setText(f"OID {oid}: renumber suggestion {cur_pt} → {dlg.result_new_pt} ({dlg.result_type}) — Corr_* metadata, not final PtNum until Write Final CSV")

    def _update_edit_hint(self):
        """Show the 'No consolidated file loaded' hint only when the edit table
        is empty."""
        self.edit_hint_label.setVisible(self.edit_table.rowCount() == 0)

    # ----- Field Book tab (read-only .fwb viewer) -----
    def _build_fieldbook_tab(self):
        """Create the field-book viewer tab once; shows the headered .fwb."""
        self.fieldbook_tab = QWidget()
        layout = QVBoxLayout(self.fieldbook_tab)

        top_bar = QHBoxLayout()
        self.load_fieldbook_btn = QPushButton("Open Field Book...")
        self.load_fieldbook_btn.clicked.connect(self._on_load_fieldbook_file)
        self.load_fieldbook_btn.setToolTip(f"Pick an existing {FIELDBOOK_EXT} — also set in Settings > Project Paths")
        self.convert_fieldbook_btn = QPushButton(f"Convert F2F → {FIELDBOOK_EXT}...")
        self.convert_fieldbook_btn.clicked.connect(self._convert_fieldbook)
        self.convert_fieldbook_btn.setToolTip("Create/update the field book from a Carlson F2F CSV (Model A)")
        top_bar.addWidget(self.load_fieldbook_btn)
        top_bar.addWidget(self.convert_fieldbook_btn)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        self.fieldbook_path_label = QLabel(f"No field book loaded — use Open or Tools > Convert ({FIELDBOOK_EXT})")
        self.fieldbook_path_label.setWordWrap(True)
        self.fieldbook_path_label.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self.fieldbook_path_label)

        self.fieldbook_hint_label = QLabel(
            f"No field book loaded — convert a Carlson F2F CSV via Tools, or open an existing {FIELDBOOK_EXT}."
        )
        self.fieldbook_hint_label.setWordWrap(True)
        layout.addWidget(self.fieldbook_hint_label)

        self.fieldbook_table = QTableWidget()
        self.fieldbook_table.setColumnCount(6)
        self.fieldbook_table.setHorizontalHeaderLabels(
            ["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"])
        self.fieldbook_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.fieldbook_table.setSortingEnabled(True)
        self.fieldbook_table.setAlternatingRowColors(True)
        self.fieldbook_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._install_column_hide_show(self.fieldbook_table)
        layout.addWidget(self.fieldbook_table)

        self.tabs.addTab(self.fieldbook_tab, "Field Book")

    def _update_fieldbook_title(self):
        if self.fieldbook_tab is None:
            return
        index = self.tabs.indexOf(self.fieldbook_tab)
        if index == -1:
            return
        # No dirty star — field book is Model A read-only; title stays plain
        self.tabs.setTabText(index, "Field Book")

    def _update_fieldbook_hint(self):
        if self.fieldbook_table is None or self.fieldbook_hint_label is None:
            return
        self.fieldbook_hint_label.setVisible(self.fieldbook_table.rowCount() == 0)

    def _update_fieldbook_path_label(self):
        if self.fieldbook_path_label is None:
            return
        if not self.fieldbook_path or not Path(self.fieldbook_path).exists():
            self.fieldbook_path_label.setText(
                f"No field book assigned — use Open or Tools > Convert to create {FIELDBOOK_EXT}.")
        else:
            self.fieldbook_path_label.setText(
                f"Field Book: {os.path.basename(self.fieldbook_path)}  "
                f"({self.fieldbook_table.rowCount()} codes) — {self.fieldbook_path}")

    def _fill_fieldbook_table(self, headers, rows):
        """Fill the viewer from a headered .fwb (never sorted on disk)."""
        self.fieldbook_table.setSortingEnabled(False)
        self.fieldbook_table.setRowCount(0)
        if headers and len(headers) == 6:
            self.fieldbook_table.setHorizontalHeaderLabels(headers)
        for row in rows:
            r = self.fieldbook_table.rowCount()
            self.fieldbook_table.insertRow(r)
            # Code: natural letters-first; Description: numbers-first
            self.fieldbook_table.setItem(
                r, 0, NaturalSortItem(row[0] if len(row) > 0 else "", letters_first=True))
            self.fieldbook_table.setItem(
                r, 1, NaturalSortItem(row[1] if len(row) > 1 else "", letters_first=False))
            self.fieldbook_table.setItem(r, 2, QTableWidgetItem(row[2] if len(row) > 2 else ""))
            self.fieldbook_table.setItem(r, 3, QTableWidgetItem(row[3] if len(row) > 3 else ""))
            self.fieldbook_table.setItem(r, 4, QTableWidgetItem(row[4] if len(row) > 4 else ""))
            self.fieldbook_table.setItem(r, 5, QTableWidgetItem(row[5] if len(row) > 5 else ""))
        self.fieldbook_table.resizeColumnsToContents()
        min_widths = [100, 240, 120, 220, 110, 140]
        for c, min_w in enumerate(min_widths):
            if self.fieldbook_table.columnWidth(c) < min_w:
                self.fieldbook_table.setColumnWidth(c, min_w)
        self.fieldbook_table.setSortingEnabled(True)
        self._update_fieldbook_hint()
        self._update_fieldbook_path_label()

    def _load_fieldbook_file(self, path):
        """Attach a .fwb to the project and show it in the viewer — also loads fieldbook dependencies (Code Commands + Correction Rules)."""
        headers, rows = read_fwb_file(Path(path))
        if headers is None:
            self.summary_label.setText(f"Field Book load failed: {os.path.basename(path)}")
            return False
        self.fieldbook_path = path
        # Load dependencies from fieldbook file
        try:
            from .config import get_command_set, set_command_set_global, get_correction_rules
            from .io_carlson import read_fwb_extra
            extra = read_fwb_extra(Path(path))
            cmds = extra.get("commands")
            if cmds:
                set_command_set_global(cmds)
            # Correction rules live in same file — fetch to ensure they are available (on-demand via get_correction_rules)
            rules = get_correction_rules(path)
            # Store count for status if needed
            self._fieldbook_rules_count = len(rules) if rules else 0
        except Exception:
            pass
        self._fill_fieldbook_table(headers, rows)
        self._show_tab(self.fieldbook_tab, "Field Book")
        # Update status to show dependencies loaded
        try:
            from .config import get_command_set
            cs = get_command_set(path)
            rc = getattr(self, '_fieldbook_rules_count', 0)
            self.summary_label.setText(f"Field Book loaded: {Path(path).name} ({len(rows)} codes) — Code Commands: {', '.join(sorted(cs)) if cs else 'defaults'}; Correction Rules: {rc}")
        except Exception:
            pass
        return True

    def _on_load_fieldbook_file(self):
        """Open Field Book... button / viewer loader (also sets project dirty)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Field Book",
            self.project_path or self.fieldbook_path or "",
            f"Field Book (*{FIELDBOOK_EXT});;All Files (*)")
        if path and self._load_fieldbook_file(path):
            self._mark_dirty()
            # _load_fieldbook_file already set summary with Code Commands + Correction Rules — do not overwrite
            pass

    def _refresh_fieldbook_tab(self):
        """Re-sync the viewer with self.fieldbook_path (after Settings/Convert/Open)."""
        # Sync dependencies from fieldbook file — Code Commands + Correction Rules (both live in .fwb)
        try:
            from .config import get_command_set, set_command_set_global, get_correction_rules
            from .io_carlson import read_fwb_extra
            if self.fieldbook_path and Path(self.fieldbook_path).exists():
                extra = read_fwb_extra(Path(self.fieldbook_path))
                cmds = extra.get("commands")
                if cmds:
                    set_command_set_global(cmds)
                # Correction rules also live in .fwb — preload count for status/autocorrect
                try:
                    self._fieldbook_rules_count = len(get_correction_rules(self.fieldbook_path))
                except Exception:
                    pass
        except Exception:
            pass
        if self.fieldbook_table is None:
            return
        if self.fieldbook_path and Path(self.fieldbook_path).exists():
            h, r = read_fwb_file(Path(self.fieldbook_path))
            if h is not None:
                self._fill_fieldbook_table(h, r)
                try:
                    self._update_run_checks_enabled()
                except Exception:
                    pass
                return
        # No valid book — clear to hint state
        self.fieldbook_table.setSortingEnabled(False)
        self.fieldbook_table.setRowCount(0)
        self.fieldbook_table.setSortingEnabled(True)
        self._update_fieldbook_hint()
        self._update_fieldbook_path_label()
        try:
            self._update_run_checks_enabled()
        except Exception:
            pass
    # ----- Consolidated Field Data tab actions -----
    def _save_edit_file(self):
        """Save the consolidated file. The file on disk always stays canonical:
        rows are re-sorted (Source path, then Point Number) and OID is
        re-numbered 1..N regardless of how the view is sorted."""
        if self.edit_table is None or self.edit_file_path is None:
            return
        rows = [self._row_texts(self.edit_table, r)
                for r in range(self.edit_table.rowCount())]
        rows.sort(key=lambda row: (
            natural_key(row[7], letters_first=True),   # Source File path
            natural_key(row[1], letters_first=True),   # Point Number
        ))
        working_rows = [[str(oid)] + row[1:] for oid, row in enumerate(rows, start=1)]
        if not self._write_working_rows(working_rows, self.edit_file_path):
            return
        self.edit_dirty = False
        self._update_edit_title()
        self.summary_label.setText(
            f"Consolidated file saved: {os.path.basename(self.edit_file_path)}")

    def _on_check_file(self):
        """Legacy Validate button — now routes to unified checks. If no field book, prompts to pick one. Kept for compat; use Run Checks (All)... for single-file unified."""
        # No consolidated file yet → nothing to check
        if self.edit_table is None or self.edit_table.rowCount() == 0:
            self.summary_label.setText("Check File: no consolidated file loaded — create one first")
            return

        # If no field book is assigned, offer to pick one now (per ruling #4)
        if not self.fieldbook_path or not Path(self.fieldbook_path).exists():
            reply = QMessageBox.question(
                self, "Field Book Needed",
                f"No field book ({FIELDBOOK_EXT}) is assigned to this project.\n"
                "Pick an existing file now? (You can also use Tools > Convert...)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                self.summary_label.setText(f"Check File: no field book assigned ({FIELDBOOK_EXT} not set)")
                return
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Field Book",
                self.project_path or "",
                f"Field Book (*{FIELDBOOK_EXT});;All Files (*)")
            if not path:
                return
            self.fieldbook_path = path
            self._refresh_fieldbook_tab()
            self._mark_dirty()

        # Try to read the field book and report a quick summary
        headers, rows = read_fwb_file(Path(self.fieldbook_path))
        if headers is None:
            self.summary_label.setText(f"Check File: failed to read field book {os.path.basename(self.fieldbook_path)}")
            return

        # Quick summary (field book stats) — full validation is via unified _run_all_checks (OID-minimal .fwc).
        cat_count = len({r[5] for r in (rows or []) if len(r) > 5})
        self.summary_label.setText(
            f"Check File: consolidated file has {self.edit_table.rowCount()} points — "
            f"field book {os.path.basename(self.fieldbook_path)}: {len(rows)} codes, {cat_count} categories "
            "(full checks next)")

    def _convert_fieldbook(self):
        """Tools > Convert Field Book: Carlson F2F CSV → distilled headered .fwb.

        Model A forever: the .fwb is a generated artifact. To update codes,
        add them in Carlson, export a fresh CSV, and convert again to
        overwrite the .fwb. The output path is auto-wired into the project.
        """
        # 1. Pick Carlson source CSV
        src, _ = QFileDialog.getOpenFileName(
            self, "Select Carlson F2F CSV",
            self.project_path or "",
            "CSV Files (*.csv);;All Files (*)")
        if not src:
            return

        headers, rows, unknown = read_carlson_fieldbook(Path(src))
        if not headers or not rows:
            QMessageBox.warning(self, "Convert Field Book",
                                f"No codes found in {os.path.basename(src)}.\n"
                                "Is this a Carlson F2F export (Code,Description,Symbol,Layer,Entity Type ...)?")
            return

        # 2. Pick destination .fwb (default: project folder, same stem)
        default_dir = self.project_path if self.project_path and Path(self.project_path).exists() else str(Path(src).parent)
        default_name = str(Path(default_dir) / (Path(src).stem + FIELDBOOK_EXT))
        dst, _ = QFileDialog.getSaveFileName(
            self, f"Save Field Book ({FIELDBOOK_EXT})",
            default_name,
            f"Field Book (*{FIELDBOOK_EXT});;All Files (*)")
        if not dst:
            return
        if Path(dst).suffix.lower() != FIELDBOOK_EXT:
            dst += FIELDBOOK_EXT

        # Prompt for Code Commands (Start Line, Start Curve, End Curve, End Line, Close) — fillable then locked, stored in fieldbook
        # Pre-fill with existing if overwriting, else defaults
        existing_rules = None
        existing_cmds = None
        try:
            from .io_carlson import read_fwb_extra
            from .config import LINE_COMMAND_DEFAULTS
            # Always start fresh for new F2F — do not carry over existing rules/commands from existing file (fixes remembering bug on new project)
            # (If user is overwriting, Windows dialog already handled overwrite, but we start with defaults/empty to avoid remembering)
            existing_rules = []
            existing_cmds = None
            # Show Code Commands dialog before write — per requirement: when we create a fieldbook be prompted to enter code commands
            cmds_for_dialog = LINE_COMMAND_DEFAULTS
            dlg_cmd = LineCommandsDialog(cmds_for_dialog, self)
            dlg_cmd.setWindowTitle("Set Code Commands for New Field Book")
            # Add extra label explaining
            # We already have hint inside dialog
            if dlg_cmd.exec() != QDialog.DialogCode.Accepted:
                # User cancelled code commands — abort creation (no legacy fallback)
                QMessageBox.warning(self, "Convert Field Book", "Field Book creation cancelled — code commands not set.")
                return
            line_cmds_to_store = dlg_cmd.get_commands()
            if not any(line_cmds_to_store):
                QMessageBox.warning(self, "Convert Field Book", "Assign at least one Field Book command meaning — creation cancelled.")
                return
            assigned = [token.casefold() for token in line_cmds_to_store if token]
            if len(assigned) != len(set(assigned)):
                QMessageBox.warning(self, "Convert Field Book", "Each Field Book command token must be unique.")
                return
            # Use dialog result as commands to store
            existing_cmds = line_cmds_to_store
            # Prompt for Correction Rules after Code Commands (per requirement: always prompt both)
            try:
                # Build fieldbook codes list from rows for CorrectionRulesDialog autocomplete
                _fb_codes_for_rules = [r[0] for r in rows if r and len(r) > 0 and r[0].strip()]
                _rules_for_dialog = existing_rules if isinstance(existing_rules, list) else []
                dlg_rules = CorrectionRulesDialog(
                    _rules_for_dialog, _fb_codes_for_rules, self, commands=existing_cmds)
                dlg_rules.setWindowTitle("Set Correction Rules for New Field Book")
                if dlg_rules.exec() == QDialog.DialogCode.Accepted:
                    existing_rules = dlg_rules.get_rules()
                # else keep existing_rules as is (user cancelled but we still create fieldbook)
            except Exception as e_rules:
                # Non-fatal: continue without rules if dialog fails
                print(f"Correction Rules prompt failed (non-fatal): {e_rules}")
        except Exception as e:
            QMessageBox.warning(self, "Convert Field Book", f"Code Commands prompt failed: {e}")
            return
        if not write_fwb_file(Path(dst), headers, rows, rules=existing_rules, commands=existing_cmds):
            QMessageBox.critical(self, "Convert Field Book", f"Failed to write {dst}")
            return

        # 3. Auto-wire into project (ruling #4: output autoloads)
        self.fieldbook_path = dst
        self._refresh_fieldbook_tab()
        self._mark_dirty()

        cat_count = len({r[5] for r in rows if len(r) > 5})
        warn = ""
        if unknown:
            warn = f"\nWarnings: unknown Entity Type codes {sorted(unknown)} were kept as-is."
        QMessageBox.information(
            self, "Convert Field Book",
            f"Converted {len(rows)} codes → {os.path.basename(dst)}\n"
            f"Categories: {cat_count}\n"
            f"Headers: {', '.join(headers)}\n"
            f"Field book is now assigned to this project (remember to Save).{warn}")
        self.summary_label.setText(
            f"Field book converted: {len(rows)} codes, {cat_count} categories → {os.path.basename(dst)}")

    # ----- Consolidated file IO -----
    def _write_working_rows(self, working_rows, path):
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)   # handles commas inside descriptions, Corr_* preserved
                for row in working_rows:
                    writer.writerow(row)
        except OSError as e:
            self.summary_label.setText(f"Write failed: {e}")
            return False
        return True

    def _read_working_file(self, path):
        """Read a headerless consolidated file. Rows shorter than 8 fields are
        padded. Only the base 8 columns are loaded for now; extra metadata
        columns arrive with future edit features."""
        rows = []
        try:
            with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
                for raw in csv.reader(f):
                    row = [c.strip() for c in raw]
                    if not any(row):
                        continue
                    if len(row) < 8:
                        row.extend([""] * (8 - len(row)))
                    # Handle Corr_* metadata (12-col) — pad or keep 8 for legacy
                    if len(row) >= 15:
                        rows.append(row[:15])
                    elif len(row) > 8:
                        # 9-14 cols: pad to 15
                        row.extend([""] * (15 - len(row)))
                        rows.append(row[:15])
                    else:
                        rows.append(row[:8] + [""]*7)
        except OSError as e:
            self.summary_label.setText(f"Read failed: {e}")
            return None
        if not rows:
            self.summary_label.setText(f"No points found in {os.path.basename(path)}")
            return None
        return rows

    def _load_edit_file(self, path):
        """Attach a consolidated file: read it and open the edit tab."""
        rows = self._read_working_file(path)
        if rows is None:
            return False
        self.edit_file_path = path
        self.edit_dirty = False
        self._open_edit_tab(rows)
        try:
            self._update_view_menu_state()
            self._update_run_checks_enabled()
        except Exception:
            pass
        return True

    def _on_load_edit_file(self):
        """File > Load Consolidated Field Data: locate an existing .fwk (e.g. after it moved)."""
        if self.edit_table is not None and not self._confirm_edit_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Consolidated Field Data", self.project_path,
            f"Consolidated Working File (*{WORKING_FILE_EXT})")
        if path and self._load_edit_file(path):
            self._mark_dirty()   # the project now references this file
            self.summary_label.setText(
                f"Consolidated file loaded: {os.path.basename(path)} "
                f"({self.edit_table.rowCount()} points)")

    def _reset_edit_phase(self):
        """Detach the consolidated file and clear the edit tab contents. Tab closes until created/opened again."""
        if self.edit_table is not None:
            self.edit_table.setRowCount(0)
        self.edit_file_path = None
        self.edit_dirty = False
        self._update_edit_title()
        if self.edit_tab is not None:
            self._update_edit_hint()
            # Close tab until next create/open
            try:
                idx = self.tabs.indexOf(self.edit_tab)
                if idx != -1:
                    self.tabs.removeTab(idx)
            except Exception:
                pass
        try:
            self._update_view_menu_state()
            self._update_run_checks_enabled()
        except Exception:
            pass

    # ----- Consolidated tab dirty state -----
    def _mark_edit_dirty(self):
        self.edit_dirty = True
        self._update_edit_title()

    def _update_edit_title(self):
        if self.edit_tab is None:
            return
        index = self.tabs.indexOf(self.edit_tab)
        if index == -1:   # tab is currently hidden
            return
        star = " *" if self.edit_dirty else ""
        self.tabs.setTabText(index, f"Consolidated Field Data{star}")

    def _confirm_edit_discard(self):
        """Save/Discard/Cancel guard for the consolidated file."""
        if self.edit_table is None or not self.edit_dirty:
            return True
        reply = QMessageBox.warning(
            self, "Unsaved Working File",
            "The consolidated file has unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Save:
            self._save_edit_file()
            return not self.edit_dirty
        elif reply == QMessageBox.StandardButton.Discard:
            return True
        else:
            return False

    # ----- Settings dialogs -----
    def _open_project_paths(self):
        dialog = ProjectPathsDialog(self.project_path, self.fieldwork_path, self.fieldbook_path, getattr(self,'master_file_path',''), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            paths = dialog.get_paths()
            if len(paths)==4:
                self.project_path, self.fieldwork_path, self.fieldbook_path, self.master_file_path = paths
                # update master used
                try:
                    from .config import read_master_ptnums
                    self._master_used = read_master_ptnums(self.master_file_path)
                except: pass
            else:
                self.project_path, self.fieldwork_path, self.fieldbook_path = paths
            self._mark_dirty()
            self._scan_fieldwork()   # paths changed -> rescan disk
            self._refresh_fieldbook_tab()
            self._refresh_desc_parse_tab()

    def _open_precision(self):
        dialog = PrecisionDialog(self.ne_decimals, self.elev_decimals, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.ne_decimals, self.elev_decimals = dialog.get_precisions()
            self._mark_dirty()
            self._update_combined_points()   # display only -> redraw table

    def _open_correction_rules(self):
        """Edit correction rules stored in Field Book file."""
        if not self.fieldbook_path or not Path(self.fieldbook_path).exists():
            QMessageBox.warning(self, "Correction Rules", f"No Field Book loaded — load or convert a Field Book ({FIELDBOOK_EXT}) first to store rules.")
            return
        try:
            from .config import get_correction_rules
            from .io_carlson import read_fwb_extra, write_fwb_extra
        except Exception as e:
            QMessageBox.warning(self, "Correction Rules", f"Failed to load rules module: {e}")
            return
        rules = get_correction_rules(self.fieldbook_path)
        # Get fieldbook codes for helper
        fb_codes=[]
        try:
            headers, rows = read_fwb_file(Path(self.fieldbook_path))
            if rows:
                fb_codes = [r[0] for r in rows if r and r[0]]
        except Exception:
            pass
        dlg = CorrectionRulesDialog(rules, fb_codes, self, fieldbook_path=self.fieldbook_path)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_rules = dlg.get_rules()
            # Active-book validation and the common-conversion confirmation are handled in the dialog.
            if write_fwb_extra(Path(self.fieldbook_path), rules=new_rules):
                self.summary_label.setText(f"Correction rules saved to {Path(self.fieldbook_path).name}: {len(new_rules)} rules")
                QMessageBox.information(self, "Correction Rules", f"Saved {len(new_rules)} rules to Field Book {Path(self.fieldbook_path).name}")
            else:
                QMessageBox.warning(self, "Correction Rules", "Failed to save rules to Field Book file.")

    def _open_line_commands(self):
        """Edit code commands stored in Field Book file — fillable Command then locked Meaning."""
        if not self.fieldbook_path or not Path(self.fieldbook_path).exists():
            QMessageBox.warning(self, "Code Commands", f"No Field Book loaded — load or convert a Field Book ({FIELDBOOK_EXT}) first to store code commands.")
            return
        try:
            from .io_carlson import read_fwb_extra, write_fwb_extra
            from .config import set_command_set_global
        except Exception as e:
            QMessageBox.warning(self, "Field Book Commands", f"Failed to load commands module: {e}")
            return
        current = None
        try:
            current = read_fwb_extra(Path(self.fieldbook_path)).get("commands")
        except Exception:
            pass
        dlg = CodeCommandsDialog(current, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cmds = dlg.get_commands()
            if write_fwb_extra(Path(self.fieldbook_path), commands=new_cmds):
                # Update global command membership for the current session.
                try:
                    set_command_set_global(token for token in new_cmds if token)
                except Exception:
                    pass
                active_count = sum(bool(token) for token in new_cmds)
                self.summary_label.setText(f"Field Book command meanings saved: {active_count} assigned")
                QMessageBox.information(self, "Field Book Commands", f"Saved {active_count} command meanings to Field Book {Path(self.fieldbook_path).name}.")
                # Refresh desc parse label hint and ensure dependencies loaded
                try:
                    self._refresh_fieldbook_tab()
                    self._refresh_desc_parse_tab()
                except Exception:
                    pass
            else:
                QMessageBox.warning(self, "Code Commands", "Failed to save code commands to Field Book file.")

    # Alias for backward compat — old name still works
    def _open_field_commands(self):
        return self._open_line_commands()

    # ----- Project file ops -----
    def _open(self):
        if not self._confirm_discard():
            return
        if not self._confirm_edit_discard():   # a project switch swaps the consolidated file too
            return
        if not self._confirm_check_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open Project", "", f"Fieldwork Manager Project (*{PROJECT_FILE_EXT});;All Files (*)")
        if path:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            self._reset_edit_phase()
            self._reset_check_phase()
            self.project_path = data.get("project_path", "")
            self.fieldwork_path = data.get("fieldwork_path", "")
            self.fieldbook_path = data.get("fieldbook_file", data.get("fieldbook_path", ""))
            self.master_file_path = data.get("master_file", data.get("master_file_path", ""))
            try:
                from .config import read_master_ptnums
                self._master_used = read_master_ptnums(self.master_file_path)
            except: self._master_used=set()
            self.ne_decimals = data.get("ne_decimals", 5)
            self.elev_decimals = data.get("elev_decimals", 5)
            self.coord_epsg = data.get("coord_epsg", 6584)
            self.coord_surface_factor = data.get("coord_surface_factor", 1.0)
            self.coord_factor_mode = data.get("coord_factor_mode", "ground_to_grid")
            self.coord_active_epsgs = data.get("coord_active_epsgs", DEFAULT_ACTIVE_EPSGS)
            # Migrate legacy pre-2011 EPSG -> 2011 Texas only per user request (2026-09-25)
            try:
                from .coord_systems import migrate_epsg, migrate_active_list, get_epsg_info
                orig_epsg = self.coord_epsg
                self.coord_epsg = migrate_epsg(self.coord_epsg)
                if orig_epsg != self.coord_epsg:
                    print(f"[coord] migrated legacy EPSG {orig_epsg} -> {self.coord_epsg} (2011 Texas)")
                # Migrate active list and also handle old CSF named key = coord_csf vs coord_surface_factor
                if "coord_csf" in data and "coord_surface_factor" not in data:
                    try: self.coord_surface_factor = float(data.get("coord_csf", 1.0))
                    except: pass
                self.coord_active_epsgs = migrate_active_list(self.coord_active_epsgs)
                # Ensure 2011 only for display
                if get_epsg_info(self.coord_epsg) is None or "2011" not in get_epsg_info(self.coord_epsg).get("name",""):
                    # fallback to Mesquite recommended 6584
                    self.coord_epsg = 6584
            except Exception as ex:
                print(f"[coord] migrate failed: {ex}")
            self.notes.setPlainText(data.get("notes", ""))
            self.current_file = path
            self.is_dirty = False
            self._update_title()
            self._update_status()
            self._scan_fieldwork()
            # Reattach the project's consolidated file, if it has one.
            working = data.get("working_file", "")
            if working:
                if Path(working).exists():
                    self._load_edit_file(working)
                else:
                    self.summary_label.setText(
                        "Consolidated file not found - use File > Load Consolidated Field Data (Ctrl+E) to relocate it")
            # Sync Field Book viewer (loads if file exists, else shows hint)
            self._refresh_fieldbook_tab()
            # Sync Check Report (loads if exists)
            chk = data.get("check_report_file", data.get("check_report", ""))
            self.check_report_path = chk if chk else ""
            if self.check_report_path and Path(self.check_report_path).exists():
                self._load_check_report(self.check_report_path)
                self.check_dirty = False
                self._update_check_title()
            else:
                self._reset_check_phase()
                if self.check_report_path and not Path(self.check_report_path).exists():
                    self.summary_label.setText(
                        f"Check report not found: {os.path.basename(self.check_report_path)} - use Tools > Run Checks or Open Report")
            # Report missing field book non-blockingly (project still opens)
            if self.fieldbook_path and not Path(self.fieldbook_path).exists():
                self.summary_label.setText(
                    f"Field book not found: {os.path.basename(self.fieldbook_path)} - "
                    "fix in Settings > Project Paths")
            self._refresh_desc_parse_tab()

    def _save(self):
        if self.current_file:
            self._write_file(self.current_file)
        else:
            self._save_as()

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Project", "", f"Fieldwork Manager Project (*{PROJECT_FILE_EXT});;All Files (*)")
        if path:
            if Path(path).suffix.lower() != PROJECT_FILE_EXT:
                path = str(Path(path).with_suffix(PROJECT_FILE_EXT))
            self.current_file = path
            self._write_file(path)

    def _import_carlson_ranges(self):
        """Import Carlson used ranges like '1000-10014,10016' for additive projects."""
        from PySide6.QtWidgets import QInputDialog
        txt, ok = QInputDialog.getText(self, "Import Carlson Ranges", "Paste Carlson range string (e.g., 1000-10014,10016) or path to .txt:")
        if not ok or not txt.strip():
            return
        p = pathlib.Path(txt.strip())
        if p.exists():
            try:
                txt = p.read_text(encoding='utf-8', errors='ignore').strip()
            except Exception as e:
                QMessageBox.warning(self, "Import Ranges", f"Failed to read {p}: {e}")
                return
        from .config import parse_carlson_ranges
        used, _ = parse_carlson_ranges(txt.strip())
        if not used:
            QMessageBox.warning(self, "Import Ranges", "No ranges parsed — expected like 1000-10014,10016")
            return
        self._external_used = set(used)
        self.summary_label.setText(f"Imported Carlson used ranges: {len(used)} points (10015 hole etc.) — suggestions now avoid these + local used")
        QMessageBox.information(self, "Import Ranges", f"Imported {len(used)} used points from Carlson. Renumber suggestions will avoid them (used+holes). Missing example: 10015 is hole in 1000-10014,10016.")

    def _import_grp(self):
        """Import .grp text file (ext can be read natively) — updates groups companion on final export."""
        path, _ = QFileDialog.getOpenFileName(self, "Import Carlson .grp", self.project_path or "", "Group Files (*.grp *.txt);;All Files (*)")
        if not path:
            return
        from .config import read_grp_file
        groups = read_grp_file(pathlib.Path(path))
        if not groups:
            QMessageBox.warning(self, "Import .grp", f"No groups found in {path}")
            return
        self._grp_groups = groups
        self._grp_path = path
        total = sum(len(v) for v in groups.values())
        self.summary_label.setText(f"Imported .grp: {len(groups)} groups, {total} points — will create updated copy on Write Final CSV (groups don't carry across crd consolidations)")
        QMessageBox.information(self, "Import .grp", f"Imported {len(groups)} groups from {pathlib.Path(path).name}:\n" + "\n".join([f"{k}: {len(v)} pts" for k,v in list(groups.items())[:5]]) + ("\n..." if len(groups)>5 else ""))

    def _toggle_corr_columns(self):
        if not hasattr(self, 'edit_table') or not hasattr(self, 'CORR_HEADERS'):
            return
        # Toggle hidden state
        hidden = self.edit_table.isColumnHidden(8)
        for ci in range(8, 12):
            self.edit_table.setColumnHidden(ci, not hidden if hidden else True)
        self.summary_label.setText(f"Corr_* columns {'shown' if hidden else 'hidden'} — metadata for renumber suggestions, not final PtNum")

    def _write_file(self, path):
        data = {
            "project_path": self.project_path,
            "fieldwork_path": self.fieldwork_path,
            "fieldbook_file": self.fieldbook_path if self.fieldbook_path else "",
            "master_file": self.master_file_path if getattr(self, "master_file_path", "") else "",
            "check_report_file": self.check_report_path if getattr(self, "check_report_path", "") else "",
            "ne_decimals": self.ne_decimals,
            "elev_decimals": self.elev_decimals,
            "coord_epsg": getattr(self, "coord_epsg", 6584),
            "coord_surface_factor": getattr(self, "coord_surface_factor", 1.0),
            "coord_factor_mode": getattr(self, "coord_factor_mode", "ground_to_grid"),
            "coord_active_epsgs": getattr(self, "coord_active_epsgs", DEFAULT_ACTIVE_EPSGS),
            "notes": self.notes.toPlainText(),
            "working_file": self.edit_file_path if self.edit_file_path else "",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.is_dirty = False
        self._update_title()
        self._update_status()

    def _close_project(self):
        if not self._confirm_discard():
            return
        if not self._confirm_edit_discard():
            return
        if not self._confirm_check_discard():
            return
        self.current_file = None
        self.project_path = ""
        self.fieldwork_path = ""
        self.fieldbook_path = ""
        self.master_file_path = ""
        self._external_used = set()
        self._master_used = set()
        self.coord_epsg = 6584
        self.coord_surface_factor = 1.0
        self.coord_factor_mode = "ground_to_grid"  # compat: always ground_to_grid (top-layer divide from 0,0), no grid mode
        self.coord_active_epsgs = DEFAULT_ACTIVE_EPSGS[:]
        self.ne_decimals = 5
        self.elev_decimals = 5
        self.notes.clear()              # fires textChanged -> _mark_dirty
        self.file_list.blockSignals(True)
        self.file_list.clear()
        self.file_list.blockSignals(False)
        self.points_table.setRowCount(0)
        self._reset_edit_phase()
        self._reset_check_phase()
        self._clear_desc_parse()
        self._refresh_fieldbook_tab()
        self._refresh_check_tab()
        self._refresh_desc_parse_tab()
        self.is_dirty = False           # must come after notes.clear()
        self._update_title()
        self._update_status()
        self.summary_label.setText("No project open — use File > New Project (Blank) to start")
        try:
            self._update_view_menu_state()
            self._update_run_checks_enabled()
        except Exception:
            pass

    def _maybe_show_startup_dialog(self):
        # Only show if no project and not offscreen (tests set offscreen)
        import os
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        if self.current_file or self.project_path or self.fieldwork_path:
            return
        # Show startup page dialog with New / Open
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("Fieldwork Manager — Start")
        dlg.setModal(True)
        dlg.resize(400, 200)
        lay = QVBoxLayout(dlg)
        title = QLabel("Welcome to Fieldwork Manager")
        title.setStyleSheet("font-weight: bold; font-size: 16px;")
        lay.addWidget(title)
        info = QLabel("Create a new project or open an existing one.\nNew will prompt for Project Paths initially.")
        info.setWordWrap(True)
        lay.addWidget(info)
        btn_row = QHBoxLayout()
        new_btn = QPushButton("New Project")
        open_btn = QPushButton("Open Project…")
        cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(new_btn)
        btn_row.addWidget(open_btn)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

        def do_new():
            dlg.accept()
            # Trigger Project Paths to set initially
            self._open_project_paths()
            # Optionally trigger New Project flow? For now just paths, user can then Save As
            # If user wants, we can also prompt for Save As after paths
            from PySide6.QtWidgets import QMessageBox
            if self.project_path:
                resp = QMessageBox.question(self, "New Project", f"Project paths set. Save new project ({PROJECT_FILE_EXT}) now?",
                                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if resp == QMessageBox.StandardButton.Yes:
                    self._save_as()

        def do_open():
            dlg.accept()
            self._open()

        new_btn.clicked.connect(do_new)
        open_btn.clicked.connect(do_open)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def closeEvent(self, event):
        # Warn if check report has unsaved Status/Comments (not all solved)
        if not self._confirm_check_discard():
            event.ignore()
            return
        if self._confirm_discard() and self._confirm_edit_discard():
            event.accept()
        else:
            event.ignore()

    # ----- Check Report tab (headered .fwc (.chk legacy), 1 list, filter + comments, pseudo-Euclidean) -----
    def _build_check_report_tab(self):
        self.check_tab = QWidget()
        layout = QVBoxLayout(self.check_tab)

        # Top bar: Fix First / Fix Auto — aligned with Desc tab, Filter, Final (Renumber pulled/disabled per request)
        top = QHBoxLayout()
        self.dup_fix_first_btn = QPushButton("Fix First")
        self.dup_fix_first_btn.setToolTip("Fix First — sequential through duplicate sets (GroupIDs) with Back for misclick, Primary/Merge/Ignore/Remove")
        self.dup_fix_first_btn.clicked.connect(self._duplicate_fix_first)
        self.dup_fix_auto_btn = QPushButton("Fix Auto")
        self.dup_fix_auto_btn.setToolTip("Fix Auto — batch auto-merge duplicate sets (Primary = smallest OID, others = Merge)")
        self.dup_fix_auto_btn.clicked.connect(self._duplicate_fix_auto)
        self.dup_final_btn = QPushButton("Final Check")
        self.dup_final_btn.setToolTip("Final Check — create Report tab of all errors + choices (Duplicate + Description)")
        self.dup_final_btn.clicked.connect(self._on_final_report)
        # Filter: All / ExactDuplicate / SimilarNumber / CloseNE
        self.check_filter_combo = QComboBox()
        self.check_filter_combo.addItems(["All", "ExactDuplicate", "SimilarNumber", "CloseNE"])
        self.check_filter_combo.setToolTip("Filter the list — table shows only that IssueType")
        self.check_filter_combo.currentTextChanged.connect(self._apply_check_filter)
        self.dup_bypass_btn = QPushButton("Bypass / Ignore Selected")
        self.dup_bypass_btn.setToolTip("Bypass CloseNE / SimilarNumber that are not real errors — marks selected group as Ignored (counts as handled)")
        self.dup_bypass_btn.clicked.connect(self._duplicate_bypass_selected)
        top.addWidget(self.dup_fix_first_btn)
        top.addWidget(self.dup_fix_auto_btn)
        top.addWidget(self.dup_bypass_btn)
        top.addWidget(self.dup_final_btn)
        top.addWidget(QLabel("Filter:"))
        top.addWidget(self.check_filter_combo)
        top.addStretch()
        layout.addLayout(top)
        # Keep aliases for compat (old code may reference open/save)
        self.open_check_btn = self.dup_fix_first_btn
        self.save_check_btn = self.dup_fix_auto_btn
        # Renumber pulled from this tab — keep hidden disabled alias for compat
        self.dup_renumber_btn = QPushButton("Renumber")
        self.dup_renumber_btn.setVisible(False)
        self.dup_renumber_btn.setEnabled(False)

        self.check_path_label = QLabel(f"No duplicate report — run checks from the consolidated file (.fwk) to generate {CHECK_REPORT_EXT}")
        self.check_path_label.setWordWrap(True)
        self.check_path_label.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self.check_path_label)

        self.check_hint_label = QLabel(
            f"No report yet — click Run Checks (needs a consolidated file with OIDs). "
            f"Results are a lean headered {CHECK_REPORT_EXT} (CSV) with GroupID/IssueType/OID…Status/Comments, filterable. "
            f"Northing=X, Easting=Y, Close NE = pseudo-Euclidean ≤{NE_TOLERANCE} (internal units)."
        )
        self.check_hint_label.setWordWrap(True)
        layout.addWidget(self.check_hint_label)

        self.check_table = QTableWidget()
        self.check_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.check_table.customContextMenuRequested.connect(lambda pos, t=self.check_table: self._on_error_table_context_menu(t, pos))
        self.check_table.setColumnCount(len(CHECK_REPORT_HEADERS))
        self.check_table.setHorizontalHeaderLabels(CHECK_REPORT_HEADERS)
        self.check_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.check_table.setSortingEnabled(True)
        self.check_table.setAlternatingRowColors(True)
        # Comments + Status editable; rest read-only. Use NoEditTriggers then
        # flip per-cell flags in _fill_check_table.
        self.check_table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.SelectedClicked)
        self.check_table.itemChanged.connect(self._on_check_item_changed)
        self.check_table.cellDoubleClicked.connect(self._on_duplicate_double_clicked)
        self._install_column_hide_show(self.check_table)
        layout.addWidget(self.check_table)

        unit_hint = QLabel("Tolerances are pseudo-Euclidean on Northing (X) / Easting (Y). Future: state-plane/SAF/lat-long conversions planned.")
        unit_hint.setVisible(False)  # hidden until coord feature exposes unit picker
        unit_hint.setWordWrap(True)
        unit_hint.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(unit_hint)

        # Keep Duplicate Error hidden until checks run (closed until ran)
        # self.tabs.addTab(self.check_tab, "Duplicate Error")  # deferred
        self._update_check_title()

    def _build_final_report_tab(self):
        self.final_report_tab = QWidget()
        layout = QVBoxLayout(self.final_report_tab)
        title = QLabel("Final Error Report — All Errors + Choices (Duplicate + Description)")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)
        hint = QLabel("Generated via Final Check from either error tab — shows every GroupID/IssueType/OID with current Status/Comments and original Detail. Use to validate before final out file. Re-run Final Check to refresh.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555; font-size: 11px;")
        layout.addWidget(hint)
        self.final_report_table = QTableWidget()
        self.final_report_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.final_report_table.customContextMenuRequested.connect(lambda pos, t=self.final_report_table: self._on_error_table_context_menu(t, pos))
        self.final_report_table.setColumnCount(8)
        self.final_report_table.setHorizontalHeaderLabels(["Tab", "GroupID", "IssueType", "OID", "PtNum", "Status", "Comments/Choice", "Detail"])
        self.final_report_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.final_report_table.setSortingEnabled(True)
        self.final_report_table.setAlternatingRowColors(True)
        self.final_report_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._install_column_hide_show(self.final_report_table)
        layout.addWidget(self.final_report_table)
        # Save row for _VALID.csv
        save_row = QHBoxLayout()
        save_row.addStretch()
        self.final_save_btn = QPushButton("Save Final Error Report...")
        self.final_save_btn.setToolTip("Save as (name)_VALID.csv — hard check failures flagged red")
        self.final_save_btn.clicked.connect(self._on_save_final_report)
        save_row.addWidget(self.final_save_btn)
        layout.addLayout(save_row)
        # Keep hidden until Final Check
        # self.tabs.addTab(self.final_report_tab, "Final Report")  # deferred
        self._update_final_report_title()


    def _on_save_final_report(self):
        """Save Final Error Report as (name)_VALID.csv — hard check flagged red retained via status column."""
        try:
            from PySide6.QtWidgets import QInputDialog, QFileDialog, QMessageBox
            from pathlib import Path
            import csv
            if self.final_report_table.rowCount() == 0:
                QMessageBox.information(self, "Final Error Report", "No rows to save — run Final Check first.")
                return
            name, ok = QInputDialog.getText(self, "Save _VALID.csv", "Enter name (will be saved as <name>_VALID.csv):")
            if not ok or not name.strip():
                return
            base = name.strip().replace(" ", "_")
            # Remove existing _VALID suffix if user typed it
            if base.upper().endswith("_VALID"):
                base = base[:-6]
            if base.upper().endswith("_VALID.CSV"):
                base = base[:-10]
            initial = f"{base}_VALID.csv"
            # Propose initial path in project dir or default
            start_dir = str(getattr(self, 'project_path', '') or ".")
            try:
                start_path = str(Path(start_dir) / initial)
            except:
                start_path = initial
            path, _ = QFileDialog.getSaveFileName(self, "Save Final Error Report _VALID.csv", start_path, "CSV (*.csv)")
            if not path:
                return
            pth = Path(path)
            # Ensure _VALID.csv suffix
            stem = pth.stem
            if not stem.upper().endswith("_VALID"):
                pth = pth.with_name(stem + "_VALID.csv")
            if pth.suffix.lower() != ".csv":
                pth = pth.with_suffix(".csv")
            # Gather headers and rows from final table
            headers = []
            try:
                headers = [self.final_report_table.horizontalHeaderItem(c).text() if self.final_report_table.horizontalHeaderItem(c) else f"Col{c}" for c in range(self.final_report_table.columnCount())]
            except:
                headers = ["Tab","GroupID","IssueType","OID","PtNum","Status","Comments/Choice","Detail"]
            rows = []
            for r in range(self.final_report_table.rowCount()):
                row = []
                for c in range(self.final_report_table.columnCount()):
                    it = self.final_report_table.item(r, c)
                    row.append(it.text() if it else "")
                rows.append(row)
            # Write CSV
            with open(pth, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(headers)
                w.writerows(rows)
            try:
                from PySide6.QtWidgets import QMessageBox as _MB
                _MB.information(self, "Saved", f"Final Error Report saved as\n{pth}\n({len(rows)} rows)")
            except:
                pass
            try:
                self.summary_label.setText(f"Final Error Report saved: {pth.name} ({len(rows)} rows)")
            except:
                pass
        except Exception as e:
            try:
                from PySide6.QtWidgets import QMessageBox as _MB2
                _MB2.warning(self, "Save failed", str(e))
            except:
                print(f"save final report failed: {e}")

    def _update_final_report_title(self):
        n = self.final_report_table.rowCount() if hasattr(self, 'final_report_table') and self.final_report_table else 0
        star = " ●" if getattr(self, 'final_dirty', False) else ""
        # Find tab index if exists
        try:
            idx = self.tabs.indexOf(self.final_report_tab)
            if idx != -1:
                self.tabs.setTabText(idx, f"Final Error Report{star} ({n})" if n else f"Final Error Report{star}")
        except:
            pass

    def _on_final_report(self):
        """Final Check — build Report tab of all errors + choices from both Duplicate and Description."""
        # Ensure tabs built
        if not hasattr(self, 'final_report_tab') or self.final_report_tab is None:
            self._build_final_report_tab()
        # Collect duplicate rows from _check_all_rows
        dup_rows = getattr(self, '_check_all_rows', []) or []
        desc_rows = getattr(self, '_desc_parse_all_rows', []) or []
        # Build OID -> PtNum map for display
        oid_map = {}
        try:
            if self.edit_table and self.edit_table.rowCount() > 0:
                for rr in range(self.edit_table.rowCount()):
                    oid_it = self.edit_table.item(rr, 0)
                    if oid_it:
                        o = oid_it.text().strip()
                        pt = self.edit_table.item(rr, 1).text() if self.edit_table.item(rr, 1) else ""
                        oid_map[o] = pt
        except:
            pass
        self.final_report_table.blockSignals(True)
        self.final_report_table.setSortingEnabled(False)
        self.final_report_table.setRowCount(0)
        # Add duplicate
        for ur in dup_rows:
            # ur: [GroupID, DisplayTab, IssueType, OID, Flags, Detail, FlagDetail, Status, Comments]
            gid = str(ur[0]).strip() if len(ur) > 0 else ""
            tab = str(ur[1]).strip() if len(ur) > 1 else "Duplicate"
            issue = str(ur[2]).strip() if len(ur) > 2 else ""
            oid = str(ur[3]).strip() if len(ur) > 3 else ""
            detail = str(ur[5]).strip() if len(ur) > 5 else ""
            status = str(ur[7]).strip() if len(ur) > 7 else "Open"
            comments = str(ur[8]).strip() if len(ur) > 8 else ""
            pt = oid_map.get(oid, "")
            # Fallback to table if not in edit
            if not pt:
                try:
                    for rr in range(self.check_table.rowCount()):
                        it = self.check_table.item(rr, 2)
                        if it and it.text().strip() == oid:
                            pt = self.check_table.item(rr, 3).text() if self.check_table.item(rr, 3) else ""
                            break
                except:
                    pass
            r = self.final_report_table.rowCount()
            self.final_report_table.insertRow(r)
            self.final_report_table.setItem(r, 0, QTableWidgetItem(tab))
            self.final_report_table.setItem(r, 1, QTableWidgetItem(gid))
            self.final_report_table.setItem(r, 2, QTableWidgetItem(issue))
            self.final_report_table.setItem(r, 3, QTableWidgetItem(oid))
            self.final_report_table.setItem(r, 4, QTableWidgetItem(pt))
            self.final_report_table.setItem(r, 5, QTableWidgetItem(status))
            self.final_report_table.setItem(r, 6, QTableWidgetItem(comments))
            self.final_report_table.setItem(r, 7, QTableWidgetItem(detail))
            # Hard check: flag red any that don't pass (including Ignored) — only Corrected/Merged green
            from PySide6.QtGui import QBrush, QColor
            if status in ("Merged", "Corrected"):
                for c in range(8):
                    it = self.final_report_table.item(r, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
            else:  # Open, Ignored, Removed — all hard fail red
                for c in range(8):
                    it = self.final_report_table.item(r, c)
                    if it:
                        it.setBackground(QBrush(QColor("#FFCDD2")))
                        # Make text dark red for visibility
                        it.setForeground(QBrush(QColor("#B71C1C")))
        # Add line repair if exists
        line_rows = getattr(self, '_line_all_rows', []) or []
        for lr in line_rows:
            gid = lr.get("gid","") if isinstance(lr, dict) else str(lr[0])
            issue = lr.get("issue_type","Line") if isinstance(lr, dict) else "Line"
            oid = lr.get("oid","") if isinstance(lr, dict) else ""
            detail = lr.get("detail","") if isinstance(lr, dict) else ""
            line_id = lr.get("line_id","") if isinstance(lr, dict) else ""
            status = "Open"
            # Try to get actual status from line_table
            try:
                for r2 in range(self.line_table.rowCount()):
                    issue_item = self.line_table.item(r2, 1)
                    issue_type = (issue_item.data(Qt.UserRole) or issue_item.text()) if issue_item else ""
                    if self.line_table.item(r2,2) and self.line_table.item(r2,2).text().strip()==str(oid) and issue_type==issue:
                        status = self.line_table.item(r2,7).text() if self.line_table.item(r2,7) else "Open"
                        break
            except: pass
            pt = oid_map.get(str(oid), "")
            if not pt:
                try:
                    for r2 in range(self.line_table.rowCount()):
                        it=self.line_table.item(r2,2)
                        if it and it.text().strip()==str(oid):
                            pt=self.line_table.item(r2,3).text() if self.line_table.item(r2,3) else ""
                            break
                except: pass
            r = self.final_report_table.rowCount()
            self.final_report_table.insertRow(r)
            self.final_report_table.setItem(r, 0, QTableWidgetItem("Line"))
            self.final_report_table.setItem(r, 1, QTableWidgetItem(str(gid)))
            self.final_report_table.setItem(r, 2, QTableWidgetItem(issue + (" ("+line_id+")" if line_id else "")))
            self.final_report_table.setItem(r, 3, QTableWidgetItem(str(oid)))
            self.final_report_table.setItem(r, 4, QTableWidgetItem(pt))
            self.final_report_table.setItem(r, 5, QTableWidgetItem(status))
            self.final_report_table.setItem(r, 6, QTableWidgetItem(line_id))
            self.final_report_table.setItem(r, 7, QTableWidgetItem(detail))
            from PySide6.QtGui import QBrush, QColor
            if status in ("Corrected","Merged"):
                for c in range(8):
                    it=self.final_report_table.item(r,c)
                    if it: it.setBackground(QBrush(QColor("#E8F5E9")))
            else:
                for c in range(8):
                    it=self.final_report_table.item(r,c)
                    if it: 
                        it.setBackground(QBrush(QColor("#FFCDD2")))
                        it.setForeground(QBrush(QColor("#B71C1C")))
        # Add description
        for ur in desc_rows:
            gid = str(ur[0]).strip() if len(ur) > 0 else ""
            tab = str(ur[1]).strip() if len(ur) > 1 else "Description"
            issue = str(ur[2]).strip() if len(ur) > 2 else ""
            oid = str(ur[3]).strip() if len(ur) > 3 else ""
            detail = str(ur[5]).strip() if len(ur) > 5 else ""
            # For desc, detail may be in flagdetail
            flagdetail = str(ur[6]).strip() if len(ur) > 6 else ""
            if flagdetail and flagdetail != detail:
                detail = f"{detail}; {flagdetail}" if detail else flagdetail
            status = str(ur[7]).strip() if len(ur) > 7 else "Open"
            comments = str(ur[8]).strip() if len(ur) > 8 else ""
            pt = oid_map.get(oid, "")
            if not pt:
                try:
                    for rr in range(self.desc_parse_table.rowCount()):
                        it = self.desc_parse_table.item(rr, 0)
                        if it and it.text().strip() == oid:
                            pt = self.desc_parse_table.item(rr, 1).text() if self.desc_parse_table.item(rr, 1) else ""
                            break
                except:
                    pass
            r = self.final_report_table.rowCount()
            self.final_report_table.insertRow(r)
            self.final_report_table.setItem(r, 0, QTableWidgetItem(tab))
            self.final_report_table.setItem(r, 1, QTableWidgetItem(gid))
            self.final_report_table.setItem(r, 2, QTableWidgetItem(issue))
            self.final_report_table.setItem(r, 3, QTableWidgetItem(oid))
            self.final_report_table.setItem(r, 4, QTableWidgetItem(pt))
            self.final_report_table.setItem(r, 5, QTableWidgetItem(status))
            self.final_report_table.setItem(r, 6, QTableWidgetItem(comments))
            self.final_report_table.setItem(r, 7, QTableWidgetItem(detail))
            from PySide6.QtGui import QBrush, QColor
            if status in ("Merged", "Corrected"):
                for c in range(8):
                    it = self.final_report_table.item(r, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
            else:  # Open, Ignored, Removed — hard fail red (exclude look-in-desc handled via Corrected only)
                for c in range(8):
                    it = self.final_report_table.item(r, c)
                    if it:
                        it.setBackground(QBrush(QColor("#FFCDD2")))
                        it.setForeground(QBrush(QColor("#B71C1C")))
        self.final_report_table.resizeColumnsToContents()
        self.final_report_table.setSortingEnabled(True)
        self.final_report_table.blockSignals(False)
        self._show_tab(self.final_report_tab, "Final Error Report")
        self._update_final_report_title()
        # Summary
        total = self.final_report_table.rowCount()
        open_cnt = sum(1 for ur in (dup_rows + desc_rows) if str(ur[7]).strip() == "Open" if len(ur) > 7)
        self.summary_label.setText(f"Final Error Report generated: {total} errors (Duplicate {len(dup_rows)} + Description {len(desc_rows)}), {open_cnt} still Open — review Status/Comments column for choices.")
        try: self._refresh_steps()
        except: pass

    # ----- Description Parse tab (separate tab, flag-only, tolerant dash/space, no leading strip) -----
    def _build_desc_parse_tab(self):
        self.desc_parse_tab = QWidget()
        layout = QVBoxLayout(self.desc_parse_tab)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        top = QHBoxLayout()
        # Parse button removed — combined checks run from Edit/Tools, tab stays closed until ran
        self.clean_first_btn = QPushButton("Clean First")
        self.clean_first_btn.setToolTip("Clean First — sequential clean; Back goes to previous description for misclick recovery")
        self.clean_first_btn.clicked.connect(self._clean_first)
        self.clean_auto_btn = QPushButton("Clean Auto")
        self.clean_selected_btn = self.clean_auto_btn  # alias for compat
        self.clean_auto_btn.setToolTip("Clean Auto — batch fix only rows with Auto Fix guesses (checkbox Include, Select All/Clear All), excludes already Corrected/Ignored/Removed — REPLACES Clean Selected")
        self.clean_auto_btn.clicked.connect(self._clean_auto)
        self.desc_final_btn = QPushButton("Final Check")
        self.desc_final_btn.setToolTip("Final Check — create Report tab of all errors + choices (Duplicate + Description)")
        self.desc_final_btn.clicked.connect(self._on_final_report)
        # Clear removed per request — no clear button on Description Error tab
        self.desc_parse_filter_combo = QComboBox()
        self.desc_parse_filter_combo.addItems(["All", "UnknownCode", "OrphanCommand", "MisplacedAfterSeparator", "LineOrderError", "EmptyDescription"])
        self.desc_parse_filter_combo.setToolTip("Filter rows by Flags — All = all flagged (EmptyDescription now included)")
        self.desc_parse_filter_combo.currentTextChanged.connect(self._apply_desc_parse_filter)
        top.addWidget(self.clean_first_btn)
        top.addWidget(self.clean_selected_btn)
        top.addWidget(self.desc_final_btn)
        top.addWidget(QLabel("Filter:"))
        top.addWidget(self.desc_parse_filter_combo)
        top.addStretch()
        layout.addLayout(top)

        # Compact F2F header — single line, full path in tooltip
        self.desc_parse_path_label = QLabel("No F2F loaded — load a Field Book (.fwb) to enable code checks")
        self.desc_parse_path_label.setWordWrap(False)
        self.desc_parse_path_label.setStyleSheet("color: #666; font-size: 10px;")
        self.desc_parse_path_label.setMaximumHeight(18)
        layout.addWidget(self.desc_parse_path_label)

        self.desc_parse_hint_label = QLabel(
            "Flagged only — unflagged are clean. Run checks (consolidated file + Field Book) to populate. Double-click a row to clean."
        )
        self.desc_parse_hint_label.setWordWrap(False)
        self.desc_parse_hint_label.setStyleSheet("color: #888; font-size: 10px;")
        self.desc_parse_hint_label.setMaximumHeight(18)
        # Hint tooltip has the detailed explanation
        self.desc_parse_hint_label.setToolTip(
            "Unified .fwc is OID-minimal — raw PtNum/N/E/Z/Desc live-pulled from Edit via OID.\n"
            "Codes and command tokens are read using the active Field Book; commands are recognized by meaning and must follow a feature code.\n"
            "Potential feature codes after the active description separator are flagged for review."
        )
        layout.addWidget(self.desc_parse_hint_label)

        self.desc_parse_table = QTableWidget()
        self.desc_parse_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.desc_parse_table.customContextMenuRequested.connect(lambda pos, t=self.desc_parse_table: self._on_error_table_context_menu(t, pos))
        self.desc_parse_table.setColumnCount(len(DESC_PARSE_HEADERS))
        self.desc_parse_table.setHorizontalHeaderLabels(DESC_PARSE_HEADERS)
        self.desc_parse_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.desc_parse_table.setSortingEnabled(True)
        self.desc_parse_table.setAlternatingRowColors(True)
        self.desc_parse_table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed | QTableWidget.EditTrigger.SelectedClicked)
        self.desc_parse_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.desc_parse_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._install_column_hide_show(self.desc_parse_table)
        # Put Raw (5), Correction/Auto Fix (10) and Ignore (11) adjacent — Flags to end (visual order: Raw, Correction, Ignore, ParsedCode, FreeDesc, Flags, FlagDetail)
        try:
            self.desc_parse_table.horizontalHeader().moveSection(10, 6)
        except Exception:
            pass
        try:
            self.desc_parse_table.horizontalHeader().moveSection(11, 7)
        except Exception:
            pass
        # Allow direct editing of Correction (Auto Fix) — handle via itemChanged
        try:
            self.desc_parse_table.itemChanged.connect(self._on_desc_correction_edited)
        except Exception:
            pass
        layout.addWidget(self.desc_parse_table)

        # Keep Description Error hidden until checks run
        # self.tabs.addTab(self.desc_parse_tab, "Description Error")
        self._update_desc_parse_title()
        self._build_check_settings_tab()

    def _build_check_settings_tab(self):
        self.check_settings_tab = QWidget()
        layout = QVBoxLayout(self.check_settings_tab)

        title = QLabel("Check Settings — code command protection & tolerances")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        hint = QLabel(
            "Tolerances are definitive in code (NE 0.1, Elev 0.1, N=X,E=Y). Future: tan vs non-tan code separation, State Plane, units picker.\n"
            "Line and curve command tokens come from the active Field Book and are checked by meaning. "
            "Valid flow: Start Line → Start Curve → End Curve → End Line or Close. "
            "A curve must have both ends, and a line must start before it ends or closes."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555; font-size: 11px;")
        layout.addWidget(hint)

        form = QFormLayout()
        self.tol_ne_label = QLabel(f"{NE_TOLERANCE}")
        self.tol_elev_label = QLabel(f"{ELEV_TOLERANCE}")
        form.addRow("NE tolerance (horizontal):", self.tol_ne_label)
        form.addRow("Elev tolerance:", self.tol_elev_label)

        from PySide6.QtWidgets import QCheckBox
        self.protect_st_pc_cb = QCheckBox("Protect Start Curve before End Curve")
        self.protect_st_pc_cb.setChecked(True)
        self.protect_st_pc_cb.setToolTip("Flag an End Curve command that is not preceded by Start Curve")
        form.addRow(self.protect_st_pc_cb)

        self.protect_pt_end_cb = QCheckBox("Protect End Curve → End Line")
        self.protect_pt_end_cb.setChecked(True)
        self.protect_pt_end_cb.setToolTip("Check curve completion before the line ends")
        form.addRow(self.protect_pt_end_cb)

        self.protect_pt_x_cb = QCheckBox("Protect End Curve → Close")
        self.protect_pt_x_cb.setChecked(True)
        form.addRow(self.protect_pt_x_cb)

        self.protect_orphan_cb = QCheckBox("Protect orphan commands (a command without a feature code)")
        self.protect_orphan_cb.setChecked(True)
        self.protect_orphan_cb.setToolTip("Already flagged in Description Parse, but also influences line-order checks")
        form.addRow(self.protect_orphan_cb)

        self.protect_pc_after_end_cb = QCheckBox("Protect Start Curve after End Line/Close")
        self.protect_pc_after_end_cb.setChecked(True)
        form.addRow(self.protect_pc_after_end_cb)

        self.protect_st_after_end_cb = QCheckBox("Protect Start Line after End Line/Close without a new segment")
        self.protect_st_after_end_cb.setChecked(False)
        self.protect_st_after_end_cb.setToolTip("Flag Start Line appearing after a line was already ended without a new segment")
        form.addRow(self.protect_st_after_end_cb)

        layout.addLayout(form)

        tan_group = QGroupBox("Future: Tan vs Non-Tan Code Separation (Carlson Generic)")
        tan_layout = QVBoxLayout(tan_group)
        tan_hint = QLabel("Codes are currently generic — Carlson guesses tan vs non-tan. Future: separate handling, maybe categories from F2F. Placeholder for now.")
        tan_hint.setWordWrap(True)
        tan_hint.setStyleSheet("color: #777; font-size: 11px;")
        tan_layout.addWidget(tan_hint)
        self.tan_codes_edit = QLineEdit()
        self.tan_codes_edit.setPlaceholderText("Comma-separated tan codes (future, not yet enforced)")
        tan_layout.addWidget(self.tan_codes_edit)
        layout.addWidget(tan_group)

        layout.addStretch()
        # Hidden: Check Settings tab internal only — flag rules fixed in code, not user-toggled
        # self.tabs.addTab(self.check_settings_tab, "Check Settings")


    def _build_line_repair_tab(self):
        self.line_repair_tab = QWidget()
        layout = QVBoxLayout(self.line_repair_tab)
        layout.setContentsMargins(6,4,6,4)
        title = QLabel("Line Repair — semantic line and curve commands, missing segments, string reuse; any feature code can define a line")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)
        hint = QLabel("Detects missing Start Line, End Line or Close, unmatched Start Curve/End Curve commands, and curve commands out of order across a point sequence. Command meanings and tokens come from the active Field Book. Allows string reuse after a segment ends; fixes are validated against the complete line.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(hint)

        top = QHBoxLayout()
        self.line_refresh_btn = QPushButton("Refresh Line Check")
        self.line_refresh_btn.setToolTip("Re-scan consolidated file for line errors (uses current fieldbook commands)")
        self.line_refresh_btn.clicked.connect(self._refresh_line_repair)
        self.line_fix_first_btn = QPushButton("Fix Selected")
        self.line_fix_first_btn.setToolTip("Apply the proposed description to the selected issue "
                                           "(the first Open one if nothing is selected), then read "
                                           "the line again and say what the fix cleared")
        self.line_fix_first_btn.clicked.connect(self._line_fix_first)
        self.line_key_in_btn = QPushButton("Key-In...")
        self.line_key_in_btn.setToolTip("Type the corrected description for the selected issue - "
                                        "validated the same way, and refused when it leaves the "
                                        "issue standing (use Ignore if it is the field book that is wrong)")
        self.line_key_in_btn.clicked.connect(self._line_key_in)
        self.line_ignore_btn = QPushButton("Ignore")
        self.line_ignore_btn.setToolTip("Leave this issue alone and remember that; it keeps its "
                                        "status in the check report instead of coming back every run")
        self.line_ignore_btn.clicked.connect(self._line_ignore)
        self.line_fix_auto_btn = QPushButton("Fix Auto (Dialog)")
        self.line_fix_auto_btn.setToolTip("Dialog-based auto-fix: review proposed Start Line, End Line, Start Curve, and End Curve repairs before applying")
        self.line_fix_auto_btn.clicked.connect(self._line_fix_auto_dialog)
        self.line_final_btn = QPushButton("Final Check")
        self.line_final_btn.setToolTip("Final Check — include Line errors in validation")
        self.line_final_btn.clicked.connect(self._on_final_report)
        self.line_filter_combo = QComboBox()
        self.line_filter_combo.addItem("All", "")
        for issue_type, label in (("Missing ST", "Missing Start Line"),
                                  ("Missing END", "Missing End Line"),
                                  ("Missing PC", "Missing Start Curve"),
                                  ("Missing PT", "Missing End Curve"),
                                  ("Line Order", "Line Command Order")):
            self.line_filter_combo.addItem(label, issue_type)
        self.line_filter_combo.currentIndexChanged.connect(self._apply_line_filter)
        top.addWidget(self.line_refresh_btn)
        top.addWidget(self.line_fix_first_btn)
        top.addWidget(self.line_key_in_btn)
        top.addWidget(self.line_ignore_btn)
        top.addWidget(self.line_fix_auto_btn)
        top.addWidget(self.line_final_btn)
        top.addWidget(QLabel("Filter:")); top.addWidget(self.line_filter_combo)
        top.addStretch()
        layout.addLayout(top)

        self.line_path_label = QLabel("No line check yet — click Refresh or Run Checks (needs consolidated file)")
        self.line_path_label.setWordWrap(True)
        self.line_path_label.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(self.line_path_label)

        self.line_table = QTableWidget()
        self.line_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.line_table.customContextMenuRequested.connect(lambda pos, t=self.line_table: self._on_error_table_context_menu(t, pos))
        self.line_table.setColumnCount(8)
        self.line_table.setHorizontalHeaderLabels(["GroupID","IssueType","OID","PtNum","LineID","Detail","Fix Suggestion","Status"])
        self.line_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.line_table.setSortingEnabled(True)
        self.line_table.setAlternatingRowColors(True)
        self.line_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # self._install_column_hide_show(self.line_table) will be done after
        layout.addWidget(self.line_table)
        # keep hidden until checks run
        self._line_all_rows = []
        # do not addTab here — shown after refresh

    def _refresh_line_repair(self):
        """Build line errors from current working rows, through the shared line engine.

        ``fieldwork/linecheck.py`` is the one reader of a line: this tab and the drawing window's
        Fix Linework workbench both use it, so their whole-line findings cannot disagree.
        """
        try:
            detect_line_errors = detect_line_issues
        except Exception as e:
            QMessageBox.warning(self, "Line Repair", f"the line check failed: {e}")
            return
        working_rows=None
        if self.edit_table is not None and self.edit_table.rowCount()>0:
            working_rows=[self._row_texts(self.edit_table, r) for r in range(self.edit_table.rowCount())]
            working_rows.sort(key=_working_row_sequence_key)
        elif self.edit_file_path and Path(self.edit_file_path).exists():
            working_rows=self._read_working_file(self.edit_file_path)
        else:
            QMessageBox.warning(self, "Line Repair", "No consolidated file loaded")
            return
        errors=detect_line_errors(working_rows, fieldbook_path=getattr(self,"fieldbook_path",None))
        # Decisions already made about these issues (Ignore, comments) live in the check report;
        # read them back so a re-run does not resurrect one the user has already dealt with.
        self._line_decisions = {}
        try:
            if getattr(self, "check_report_path", None) and Path(self.check_report_path).exists():
                _h, saved_rows = read_unified_report(Path(self.check_report_path))
                self._line_decisions = line_statuses_from_rows(saved_rows)
        except Exception:
            self._line_decisions = {}
        self._fill_line_table(errors)
        if errors:
            self._show_tab(self.line_repair_tab, "Line Repair")
        self.line_path_label.setText(f"Line repair: {len(errors)} issues found — {', '.join(set(e['issue_type'] for e in errors)) if errors else 'no issues'}")
        self.summary_label.setText(f"Line Check: {len(errors)} line issues (active Field Book command meanings)")
        try: self._refresh_steps()
        except: pass

    def _fill_line_table(self, errors):
        from PySide6.QtGui import QBrush, QColor
        if not hasattr(self, 'line_table') or self.line_table is None:
            return
        self.line_table.blockSignals(True)
        self.line_table.setSortingEnabled(False)
        self.line_table.setRowCount(0)
        self._line_all_rows = errors[:]
        # Build OID -> PtNum map for display
        oid_map={}
        try:
            if self.edit_table and self.edit_table.rowCount()>0:
                for rr in range(self.edit_table.rowCount()):
                    oid_it=self.edit_table.item(rr,0)
                    if oid_it:
                        oid_map[oid_it.text().strip()]=self.edit_table.item(rr,1).text() if self.edit_table.item(rr,1) else ""
        except: pass
        for e in errors:
            gid=e.get("gid","")
            issue=e.get("issue_type","Line")
            oid=e.get("oid","")
            pt=oid_map.get(str(oid),"")
            line_id=e.get("line_id","")
            detail=e.get("detail","")
            sug=e.get("fix_suggestion","")
            saved = (getattr(self, "_line_decisions", {}) or {}).get((issue, str(oid)), {})
            status = saved.get("status") or "Open"
            suggestion = sug
            if e.get("proposal"):
                suggestion = f"{sug} — proposed: {e['proposal']}"
            if saved.get("comments"):
                suggestion = f"{suggestion} [{saved['comments']}]"
            r=self.line_table.rowCount(); self.line_table.insertRow(r)
            self.line_table.setItem(r,0, NumericSortItem(gid)); self.line_table.item(r,0).setText(str(gid))
            display_issue = {
                "Missing ST": "Missing Start Line",
                "Missing END": "Missing End Line",
                "Missing PC": "Missing Start Curve",
                "Missing PT": "Missing End Curve",
                "Line Order": "Line Command Order",
            }.get(issue, issue)
            issue_item = QTableWidgetItem(display_issue)
            issue_item.setData(Qt.UserRole, issue)
            self.line_table.setItem(r,1, issue_item)
            self.line_table.setItem(r,2, NumericSortItem(oid)); self.line_table.item(r,2).setText(str(oid))
            self.line_table.setItem(r,3, NaturalSortItem(pt, letters_first=True))
            self.line_table.setItem(r,4, QTableWidgetItem(line_id))
            d_item=QTableWidgetItem(detail); d_item.setToolTip(detail); self.line_table.setItem(r,5, d_item)
            s_item=QTableWidgetItem(suggestion); s_item.setToolTip(suggestion)
            self.line_table.setItem(r,6, s_item)
            self.line_table.setItem(r,7, QTableWidgetItem(status))
            # color
            if "Missing" in issue:
                for c in range(8):
                    it=self.line_table.item(r,c)
                    if it: it.setBackground(QBrush(QColor("#FFEBEE")))
            elif issue=="Line Order":
                for c in range(8):
                    it=self.line_table.item(r,c)
                    if it: it.setBackground(QBrush(QColor("#FFF8E1")))
        self.line_table.resizeColumnsToContents()
        self.line_table.setSortingEnabled(True)
        self.line_table.blockSignals(False)
        self._apply_line_filter()

    def _apply_line_filter(self, _index=None):
        if not hasattr(self,'line_table') or self.line_table is None or not hasattr(self,'line_filter_combo'):
            return
        issue_filter = self.line_filter_combo.currentData()
        for row in range(self.line_table.rowCount()):
            item = self.line_table.item(row, 1)
            issue_type = item.data(Qt.UserRole) if item else ""
            show = not issue_filter or issue_type == issue_filter
            self.line_table.setRowHidden(row, not show)

    # ----- line issues: Fix / Key-In / Ignore, validated against the line itself -------------
    def _line_working_rows(self):
        """The working rows the line tools read and validate against (same source as detection)."""
        if self.edit_table is not None and self.edit_table.rowCount() > 0:
            rows = [self._row_texts(self.edit_table, r) for r in range(self.edit_table.rowCount())]
            rows.sort(key=_working_row_sequence_key)
            return rows
        if self.edit_file_path and Path(self.edit_file_path).exists():
            return self._read_working_file(self.edit_file_path)
        return []

    def _line_target_rows(self):
        """The rows a tool acts on: what is selected, else the first issue still Open."""
        if not hasattr(self, "line_table") or self.line_table is None or self.line_table.rowCount() == 0:
            return []
        picked = sorted({it.row() for it in self.line_table.selectedItems()})
        if picked:
            return picked
        for r in range(self.line_table.rowCount()):
            if self.line_table.isRowHidden(r):
                continue
            st = self.line_table.item(r, 7)
            if st is None or st.text().strip() in ("", "Open"):
                return [r]
        return []

    def _line_issue_of(self, row) -> dict:
        """The detected issue behind a table row (matched on GroupID - the table can be sorted)."""
        gid = self.line_table.item(row, 0).text().strip() if self.line_table.item(row, 0) else ""
        for e in (getattr(self, "_line_all_rows", None) or []):
            if str(e.get("gid")) == gid:
                return e
        return {}

    def _line_current_desc(self, oid, working_rows=None) -> str:
        for wr in (working_rows if working_rows is not None else self._line_working_rows()):
            if str(wr[0]).strip() == str(oid).strip():
                return str(wr[5]).strip() if len(wr) > 5 else ""
        return ""

    def _line_set_desc(self, oid, new_desc) -> bool:
        """Write the corrected description into the Edit table - the source every other tab reads."""
        try:
            for rr in range(self.edit_table.rowCount()):
                if self.edit_table.item(rr, 0) and self.edit_table.item(rr, 0).text().strip() == str(oid).strip():
                    self.edit_table.item(rr, 5).setText(new_desc)
                    self.edit_dirty = True
                    try:
                        self._update_edit_title()
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        return False

    def _line_mark(self, row, status, colour=None):
        try:
            if self.line_table.item(row, 7):
                self.line_table.item(row, 7).setText(status)
            if colour:
                from PySide6.QtGui import QBrush, QColor
                for c in range(self.line_table.columnCount()):
                    it = self.line_table.item(row, c)
                    if it:
                        it.setBackground(QBrush(QColor(colour)))
        except Exception:
            pass

    def _line_mark_cleared(self, res, oid):
        """Colour the issues a validated fix cleared - including the ones it was not aimed at."""
        done = {(e["issue_type"], str(e["oid"])) for e in list(res.get("cleared") or []) + list(res.get("also_cleared") or [])}
        if not done:
            return
        for r in range(self.line_table.rowCount()):
            issue_item = self.line_table.item(r, 1)
            issue = str(issue_item.data(Qt.UserRole) or issue_item.text()).strip() if issue_item else ""
            roid = self.line_table.item(r, 2).text().strip() if self.line_table.item(r, 2) else ""
            if (issue, roid) in done:
                self._line_mark(r, "Corrected" if roid == str(oid).strip() else f"Cleared by fix at OID {oid}", "#E8F5E9")

    def _line_apply(self, row, new_desc, how="Fix") -> dict:
        """Apply a corrected description, then **validate it against the line** and say what it did.

        The validation is the point of the tool.  The field book's own note asked for it: fixing
        one end of a line routinely clears an issue at the other end, and a tool that reports only
        the row it was aimed at makes the second one look like work still to do.
        """
        issue = self._line_issue_of(row)
        oid = issue.get("oid") or (self.line_table.item(row, 2).text().strip() if self.line_table.item(row, 2) else "")
        # Read the file *before* writing, so "what did this clear?" is answered against the line
        # as it was - validate_fix applies the description itself, in memory.
        before_rows = self._line_working_rows()
        res = validate_line_fix(before_rows, oid, new_desc, fieldbook_path=getattr(self, "fieldbook_path", None))
        if not self._line_set_desc(oid, new_desc):
            QMessageBox.warning(self, "Line Repair", f"OID {oid} is not in the Consolidated table - nothing was changed.")
            return {}
        self._line_mark_cleared(res, oid)
        self._line_mark(row, "Corrected" if res["ok"] else "Still flagged", "#E8F5E9" if res["ok"] else "#FFF3E0")
        if not res["ok"]:
            QMessageBox.information(
                self, "Line Repair",
                f"{res['note']}\n\nThe description was written, but the issue still stands. Key-In "
                f"the corrected description, or Ignore this issue if the field book is what is wrong.")
        return res

    def _line_fix_first(self):
        """Fix: apply the proposed description to the selected issue and validate the result."""
        rows = self._line_target_rows()
        if not rows:
            QMessageBox.information(self, "Line Repair", "No line issues - Refresh Line Check first.")
            return
        row = rows[0]
        issue = self._line_issue_of(row)
        oid = issue.get("oid") or ""
        cur = self._line_current_desc(oid)
        proposal = issue.get("proposal") or propose_line_fix(issue.get("issue_type", ""), cur, issue.get("line_id", ""))
        if not proposal:
            QMessageBox.information(
                self, "Line Repair",
                f"No safe guess for {issue.get('issue_type','this issue')} at OID {oid} - the "
                f"description is '{cur}'.  Key-In the corrected description instead.")
            return
        if proposal == cur:
            QMessageBox.information(self, "Line Repair", f"OID {oid} already reads '{cur}'.")
            return
        res = self._line_apply(row, proposal, how="Line Fix")
        try:
            self._autosave_fwk()
        except Exception:
            pass
        try:
            self._refresh_steps()
        except Exception:
            pass
        if res:
            self.summary_label.setText(f"Line Fix: {res['note']}")

    def _line_key_in(self):
        """Key-In: the user writes the corrected description; it is validated before it stands."""
        rows = self._line_target_rows()
        if not rows:
            QMessageBox.information(self, "Line Repair", "No line issues - Refresh Line Check first.")
            return
        row = rows[0]
        issue = self._line_issue_of(row)
        oid = issue.get("oid") or ""
        cur = self._line_current_desc(oid)
        from PySide6.QtWidgets import QInputDialog
        new_desc, ok = QInputDialog.getText(
            self, f"Key-In Fix - {issue.get('issue_type','line issue')} at OID {oid}",
            f"{issue.get('detail','')}\n\nCurrent description: '{cur}'\n"
            "Tokens are interpreted by their active Field Book meanings and belong to the feature code before them.",
            text=cur)
        new_desc = (new_desc or "").strip()
        if not ok or not new_desc or new_desc == cur:
            return
        res = self._line_apply(row, new_desc, how="Line Key-In")
        try:
            self._autosave_fwk()
        except Exception:
            pass
        try:
            self._refresh_steps()
        except Exception:
            pass
        if res:
            self.summary_label.setText(f"Line Key-In: {res['note']}")

    def _line_ignore(self):
        """Ignore: leave the issue alone and remember it, in the check report, not in the session."""
        rows = self._line_target_rows()
        if not rows:
            QMessageBox.information(self, "Line Repair", "No line issues - Refresh Line Check first.")
            return
        for row in rows:
            issue = self._line_issue_of(row)
            key = (issue.get("issue_type", ""), str(issue.get("oid", "")))
            if not key[0]:
                continue
            self._line_decisions = dict(getattr(self, "_line_decisions", {}) or {})
            self._line_decisions[key] = {"status": "Ignored",
                                         "comments": "Ignored in Line Repair - left as the crew wrote it"}
            self._line_mark(row, "Ignored", "#ECEFF1")
        self._write_line_decisions()
        n = len(rows)
        self.summary_label.setText(f"Line Repair: {n} issue(s) ignored - kept in the check report so the next run does not re-open them")

    def _write_line_decisions(self):
        """Keep the decisions where a re-run will find them: the check report's Line rows."""
        path = getattr(self, "check_report_path", None)
        if not path or not Path(path).exists():
            return
        try:
            _headers, rows = read_unified_report(Path(path))
            keep = [r for r in (rows or []) if str(r[1]).strip().lower() != "line"]
            issues = [e for e in (getattr(self, "_line_all_rows", None) or [])]
            tail = line_report_rows(issues, getattr(self, "_line_decisions", {}) or {}, start_gid=len(keep) + 1)
            out = []
            gid = 1
            for r in keep + tail:
                r2 = r[:]
                r2[0] = str(gid)
                out.append(r2)
                gid += 1
            write_unified_report(Path(path), out)
        except Exception as ex:
            print(f"line decisions could not be written to {path}: {ex}")

    def _line_fix_auto(self):
        """Fix All Proposed: every issue with a safe guess, each one validated, none invented."""
        if not hasattr(self, 'line_table') or self.line_table.rowCount() == 0:
            QMessageBox.information(self, "Line Repair", "No line issues - Refresh Line Check first.")
            return
        working_rows = self._line_working_rows()
        applied = refused = skipped = 0
        for r in range(self.line_table.rowCount()):
            if self.line_table.isRowHidden(r):
                continue
            status = self.line_table.item(r, 7).text().strip() if self.line_table.item(r, 7) else ""
            if status not in ("", "Open", "Still flagged"):
                continue
            issue = self._line_issue_of(r)
            oid = issue.get("oid") or ""
            cur = self._line_current_desc(oid, working_rows)
            proposal = issue.get("proposal") or propose_line_fix(issue.get("issue_type", ""), cur, issue.get("line_id", ""))
            if not proposal or proposal == cur:
                skipped += 1
                continue
            self._line_set_desc(oid, proposal)
            working_rows = self._line_working_rows()
            res = validate_line_fix(working_rows, oid, proposal, fieldbook_path=getattr(self, "fieldbook_path", None))
            self._line_mark_cleared(res, oid)
            self._line_mark(r, "Corrected" if res["ok"] else "Still flagged", "#E8F5E9" if res["ok"] else "#FFF3E0")
            applied += 1
            if not res["ok"]:
                refused += 1
        try:
            self._autosave_fwk()
        except Exception:
            pass
        try:
            self._refresh_steps()
        except Exception:
            pass
        self.summary_label.setText(
            f"Line Fix: {applied} applied ({applied - refused} verified clean, {refused} still flagged), "
            f"{skipped} with no safe guess - use Fix Selected or Key-In for those")


    # ----- Steps / Workflow Report (new) -----
    def _build_steps_tab(self):
        self.steps_tab = QWidget()
        layout = QVBoxLayout(self.steps_tab)
        layout.setContentsMargins(6,4,6,4)
        title = QLabel("Steps — Fix All Description → Duplicate → Line → Master (Master Protected) → Final Export/KML")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)
        hint = QLabel("Order is enforced: each step shows Total / Fixed / Open and progress. Master points are PROTECTED (all numbers in master file cannot be overwritten — new conflicts must renumber single/range). Use buttons to run each step or export.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(hint)

        top = QHBoxLayout()
        self.steps_refresh_btn = QPushButton("Refresh Steps")
        self.steps_refresh_btn.setToolTip("Recalculate counts from live tables")
        self.steps_refresh_btn.clicked.connect(self._refresh_steps)
        self.steps_export_csv_btn = QPushButton("Export CSV")
        self.steps_export_csv_btn.clicked.connect(self._export_steps_csv)
        self.steps_export_html_btn = QPushButton("Export HTML (PDF)")
        self.steps_export_html_btn.clicked.connect(self._export_steps_html)
        self.steps_kml_btn = QPushButton("Export KML...")
        self.steps_kml_btn.setToolTip("Export to Google Earth via Coordinate Settings EPSG + surface factor")
        self.steps_kml_btn.clicked.connect(self._export_kml)
        top.addWidget(self.steps_refresh_btn)
        top.addWidget(self.steps_export_csv_btn)
        top.addWidget(self.steps_export_html_btn)
        top.addWidget(self.steps_kml_btn)
        top.addStretch()
        layout.addLayout(top)

        self.steps_table = QTableWidget()
        self.steps_table.setColumnCount(5)
        self.steps_table.setHorizontalHeaderLabels(["Step", "Status", "Progress", "Detail", "Next Action"])
        self.steps_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.steps_table.setSortingEnabled(False)
        self.steps_table.setAlternatingRowColors(True)
        self.steps_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.steps_table.verticalHeader().setDefaultSectionSize(44)
        layout.addWidget(self.steps_table)

        # Quick action row
        act_row = QHBoxLayout()
        self.steps_fix_desc_btn = QPushButton("1. Fix Description (Clean First)")
        self.steps_fix_desc_btn.clicked.connect(self._clean_first)
        self.steps_fix_dup_btn = QPushButton("2. Duplicate: Renumber/ Merge")
        self.steps_fix_dup_btn.clicked.connect(self._on_dup_renumber)
        self.steps_fix_line_btn = QPushButton("3. Fix Line")
        self.steps_fix_line_btn.clicked.connect(self._refresh_line_repair)
        self.steps_fix_master_btn = QPushButton("4. Master: Renumber Single/Range")
        self.steps_fix_master_btn.clicked.connect(self._renumber_single)
        act_row.addWidget(self.steps_fix_desc_btn)
        act_row.addWidget(self.steps_fix_dup_btn)
        act_row.addWidget(self.steps_fix_line_btn)
        act_row.addWidget(self.steps_fix_master_btn)
        act_row.addStretch()
        layout.addLayout(act_row)

        # Do not addTab here — shown via View or after checks
        # self.tabs.addTab(self.steps_tab, "Steps")
        self._steps_all_rows = []

    def _refresh_steps(self):
        from .steps_report import compute_steps_status
        from PySide6.QtGui import QBrush, QColor
        if not hasattr(self, 'steps_table') or self.steps_table is None:
            return
        steps = compute_steps_status(self)
        self.steps_table.blockSignals(True)
        self.steps_table.setRowCount(0)
        for s in steps:
            r = self.steps_table.rowCount()
            self.steps_table.insertRow(r)
            self.steps_table.setItem(r, 0, QTableWidgetItem(s["title"]))
            # Status with badge color
            status_item = QTableWidgetItem(s["status"])
            if s["status"] == "Complete":
                status_item.setBackground(QBrush(QColor("#C8E6C9")))
            elif "Progress" in s["status"]:
                status_item.setBackground(QBrush(QColor("#FFF3E0")))
            else:
                status_item.setBackground(QBrush(QColor("#E0E0E0")))
            self.steps_table.setItem(r, 1, status_item)
            prog = f'{s["fixed"]}/{s["total"]}  {s["pct"]}%'
            self.steps_table.setItem(r, 2, QTableWidgetItem(prog))
            self.steps_table.setItem(r, 3, QTableWidgetItem(s["detail"]))
            # Next action hint
            nxt = ""
            if s["key"]=="desc": nxt = "Clean First / Clean Auto"
            elif s["key"]=="duplicate": nxt = "Duplicate tab → Merge/Remove/Renumber"
            elif s["key"]=="line": nxt = "Line Repair → Refresh / Fix"
            elif s["key"]=="master": nxt = "Tools → Renumber Single/Range (master protected)"
            elif s["key"]=="export": nxt = "Final Error Report → _VALID.csv + KML"
            self.steps_table.setItem(r, 4, QTableWidgetItem(nxt))
            # ToolTip = desc
            for c in range(5):
                it = self.steps_table.item(r,c)
                if it:
                    it.setToolTip(s["desc"])
                    if s["status"]=="Complete":
                        it.setBackground(QBrush(QColor("#E8F5E9")))
        self.steps_table.resizeColumnsToContents()
        self.steps_table.blockSignals(False)
        # Update summary
        open_total = sum(s["open"] for s in steps)
        self.summary_label.setText(f"Steps: {sum(1 for s in steps if s['status']=='Complete')}/{len(steps)} complete — {open_total} Open across all steps — Master protected {len(getattr(self,'_master_used',set()))} points")
        # Show tab if hidden
        try:
            if self.tabs.indexOf(self.steps_tab)==-1:
                self._show_tab(self.steps_tab, "Steps")
        except: pass

    def _export_steps_csv(self):
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from .steps_report import write_steps_csv
        default = str(Path(getattr(self, "project_path","") or ".") / "STEPS_REPORT.csv") if getattr(self,"project_path","") else "STEPS_REPORT.csv"
        try:
            default = str(Path(getattr(self,"current_file","")).parent / "STEPS_REPORT.csv") if getattr(self,"current_file","") else default
        except: pass
        path, _ = QFileDialog.getSaveFileName(self, "Export Steps CSV", default, "CSV (*.csv)")
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() != ".csv":
            p = p.with_suffix(".csv")
        if write_steps_csv(p, self):
            QMessageBox.information(self, "Steps CSV", f"Saved {p} ({p.stat().st_size} bytes)")

    def _export_steps_html(self):
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from .steps_report import write_steps_html
        default = str(Path(getattr(self, "project_path","") or ".") / "STEPS_REPORT.html") if getattr(self,"project_path","") else "STEPS_REPORT.html"
        try:
            default = str(Path(getattr(self,"current_file","")).parent / "STEPS_REPORT.html") if getattr(self,"current_file","") else default
        except: pass
        path, _ = QFileDialog.getSaveFileName(self, "Export Steps HTML", default, "HTML (*.html)")
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() not in (".html",".htm"):
            p = p.with_suffix(".html")
        if write_steps_html(p, self):
            QMessageBox.information(self, "Steps HTML", f"Saved {p}\nOpen in browser → Print to PDF for PDF report.")
            # Try to show
            try:
                self._show_tab(self.steps_tab, "Steps")
            except: pass

    # ----- Coordinate Settings (new) -----
    def _build_coord_tab(self):
        from PySide6.QtWidgets import QCheckBox  # local fallback for hidden compat
        self.coord_tab = QWidget()
        layout = QVBoxLayout(self.coord_tab)
        layout.setContentsMargins(6,4,6,4)
        title = QLabel("Coordinate Settings — 2011 Texas State Plane (NAD83 2011) + TXDOT SAF Top-Layer Scaler (from origin 0,0)")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold; font-size: 13px; color:#0d47a1;")
        layout.addWidget(title)
        hint = QLabel("GPS-tied State Plane: pick 2011 Texas zone (e.g., TXNC 2011 ≈ 6M N × 3M E, origin 0,0). Final Ground = Grid × SAF (TXDOT county-wide factor, e.g., Bexar 1.00017) scaling from origin 0,0. For KML export, Grid = Ground ÷ SAF (first out last in). Library is 2011 Texas ONLY (6581/6582 North, 6583/6584 North Central, 6577/6578 Central, 6587/6588 South Central, 6585/6586 South) + WGS84.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#1a237e; font-size:11px; background:#e3f2fd; border-radius:6px; padding:8px;")
        layout.addWidget(hint)

        # Manager dialog button (2011 Texas only, Import/Save + TOP LAYER SCALER)
        diag_row = QHBoxLayout()
        self.coord_manage_btn = QPushButton("⚙ Manage Coordinate System — 2011 Texas (Import / Save, TXDOT SAF from Origin 0,0)...")
        self.coord_manage_btn.setStyleSheet("QPushButton{background:#e3f2fd;color:#0d47a1;border:1px solid #90caf9;border-radius:6px;padding:8px 14px;font-weight:bold;} QPushButton:hover{background:#bbdefb;}")
        self.coord_manage_btn.setToolTip("Open dialog: pick 2011 Texas zone, enter TXDOT SAF (top-layer scaler from 0,0), Import/Save to project/file")
        self.coord_manage_btn.clicked.connect(self._on_coord_manage)
        diag_row.addWidget(self.coord_manage_btn)
        diag_row.addStretch()
        layout.addLayout(diag_row)

        form = QFormLayout()
        form.setLabelAlignment(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.AlignmentFlag.AlignRight)
        self.coord_epsg_combo = QComboBox()
        self.coord_epsg_combo.setToolTip("Choose 2011 Texas EPSG for N/E → WGS84 for KML. Recommended Mesquite, TX: 6584 TX North Central 2011 (USft)")
        form.addRow("2011 Texas Zone (EPSG):", self.coord_epsg_combo)

        # TOP LAYER SCALER — SAF from origin (0,0) — separate group
        self.coord_saf_group = QGroupBox("Top Layer Scaler — TXDOT SAF (First Out Last In, from Origin 0,0)")
        saf_lay = QVBoxLayout(self.coord_saf_group)
        saf_hint = QLabel("No CSF/Grid-Ground toggle — only SAF remains. SAF is the outer scaler: point > TXNC 2011 grid (≈6M×3M, origin 0,0) > <b>× SAF</b> > Final stored coordinate. For export: <b>Final ÷ SAF</b> → TXNC 2011 grid → WGS84. Enter TXDOT county-wide factor or 1.0 for grid.")
        saf_hint.setWordWrap(True)
        saf_hint.setTextFormat(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.TextFormat.RichText)
        saf_hint.setStyleSheet("color:#4e342e; font-size:10px;")
        saf_lay.addWidget(saf_hint)
        saf_row = QHBoxLayout()
        saf_row.addWidget(QLabel("TXDOT SAF:"))
        self.coord_csf_edit = QLineEdit("1.0")
        self.coord_csf_edit.setPlaceholderText("1.0 = grid (no SAF); e.g., 1.00017 Bexar County")
        _widen_saf(self.coord_csf_edit)           # 18 characters of SAF, same as the CRS dialog
        # Backward compat alias: coord_saf_edit points to same widget
        self.coord_saf_edit = self.coord_csf_edit
        self.coord_saf_value = self.coord_csf_edit
        saf_row.addWidget(self.coord_csf_edit)
        self.coord_saf_info = QLabel("1.0 = grid; county SAF scales from origin 0,0")
        self.coord_saf_info.setStyleSheet("color:#555; font-size:10px;")
        saf_row.addWidget(self.coord_saf_info)
        saf_row.addStretch()
        saf_lay.addLayout(saf_row)
        # Use Ground is the switch the SAF box hangs off: a factor typed while this is clear would
        # do nothing at all, and a surveyor reading "1.000136506" in a live box would believe it
        # was in force.  The tick is the same flag the KML/GeoJSON writers read through
        # coord_is_ground_cb below, so what is exported is what is on screen.
        self.coord_use_ground = QCheckBox("Use ground coordinates (TXDOT SAF - work is stored as ground)")
        self.coord_use_ground.setToolTip("Tick to store ground coordinates: stored = grid x SAF from 0,0.\n"
                                         "With this clear the job is grid, the SAF is 1.0, and the box is locked.")
        saf_lay.addWidget(self.coord_use_ground)
        # keep hidden compat widgets so old project loads don't crash
        self.coord_is_ground_cb = QCheckBox("Ground (hidden compat)")
        self.coord_is_ground_cb.setChecked(True)
        self.coord_is_ground_cb.setVisible(False)
        self.coord_mode_combo = QComboBox()
        self.coord_mode_combo.addItems(["ground_to_grid (field is ground, divide by SAF)", "grid_to_ground (field is grid, multiply)"])
        self.coord_mode_combo.setVisible(False)
        self.coord_mode_combo.setCurrentIndex(0)
        def _use_ground_toggled(on: bool):
            """The one place the ground flag is set: the tick, not the text in the SAF box."""
            on = bool(on)
            self.coord_csf_edit.setEnabled(on)
            self.coord_is_ground_cb.setChecked(on)
            if not on:
                self.coord_csf_edit.blockSignals(True)
                self.coord_csf_edit.setText("1.0")     # grid: no scaler, nothing to type
                self.coord_csf_edit.blockSignals(False)
                self.coord_saf_info.setText("grid - tick Use ground coordinates to enter a SAF from 0,0")
                self.coord_saf_info.setStyleSheet("color:#555; font-size:10px;")
            else:
                self.coord_mode_combo.setCurrentIndex(0)
                try:
                    _auto_saf()
                except Exception:
                    pass
        # update is_ground auto based on SAF !=1
        def _auto_saf(*_a):
            try:
                saf = float(self.coord_csf_edit.text().strip())
                is_ground = abs(saf - 1.0) > 1e-12
                self.coord_is_ground_cb.setChecked(is_ground)
                self.coord_mode_combo.setCurrentIndex(0 if is_ground else 1)
                if abs(saf-1.0) < 1e-9:
                    self.coord_saf_info.setText("1.0 = grid (no top-layer scale)")
                    self.coord_saf_info.setStyleSheet("color:#2e7d32; font-size:10px;")
                elif saf > 1:
                    self.coord_saf_info.setText(f"{saf:g} → ground larger (grid × SAF from 0,0)")
                    self.coord_saf_info.setStyleSheet("color:#ef6c00; font-size:10px;")
                else:
                    self.coord_saf_info.setText(f"{saf:g} → ground smaller")
                    self.coord_saf_info.setStyleSheet("color:#c62828; font-size:10px;")
            except:
                self.coord_saf_info.setText("Enter numeric SAF")
                self.coord_saf_info.setStyleSheet("color:#c62828;")
        self.coord_csf_edit.textChanged.connect(_auto_saf)
        self.coord_use_ground.toggled.connect(_use_ground_toggled)
        # Initial: ground on when the job being opened is already ground (SAF not 1.0)
        try:
            _on = bool(current_saf) and abs(float(current_saf) - 1.0) > 1e-12
        except Exception:
            _on = False
        self.coord_use_ground.setChecked(_on)
        _use_ground_toggled(_on)
        try: _auto_saf()
        except: pass
        form.addRow(self.coord_saf_group)

        self.coord_active_combo = QComboBox()
        self.coord_active_combo.setToolTip("Active 2011 Texas library subset")
        form.addRow("Active 2011 Library:", self.coord_active_combo)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self.coord_refresh_btn = QPushButton("Refresh 2011 List")
        self.coord_refresh_btn.clicked.connect(self._refresh_coord_combos)
        self.coord_import_btn = QPushButton("Import EPSG from Library...")
        self.coord_import_btn.clicked.connect(self._import_epsg)
        self.coord_test_btn = QPushButton("Test Convert (first point → lat/lon)")
        self.coord_test_btn.clicked.connect(self._test_coord_convert)
        self.coord_kml_btn = QPushButton("Export KML... (clampToGround)")
        self.coord_kml_btn.setToolTip("Export to Google Earth — KML uses Grid = Ground / SAF from origin 0,0 (TXDOT top-layer)")
        self.coord_kml_btn.clicked.connect(self._export_kml)
        btn_row.addWidget(self.coord_refresh_btn)
        btn_row.addWidget(self.coord_import_btn)
        btn_row.addWidget(self.coord_test_btn)
        btn_row.addWidget(self.coord_kml_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.coord_status = QLabel("No conversion test yet — pyproj for best accuracy; pure-python Texas 2011 LCC fallback active if needed.")
        self.coord_status.setWordWrap(True)
        self.coord_status.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(self.coord_status)

        # Info box — TXDOT SOP top-layer
        info = QLabel("TXDOT SOP top-layer: Ground = Grid × SAF from origin 0,0 (TXNC 2011 pre-final grid approx 6M N by 3M E). County-wide SAF (e.g., Bexar 1.00017) is the outer scaler — first out last in. Only 2011 Texas State Plane zones are offered (m + USft per 5 Texas zones). Save = to .fmp project or .json file; Import = from EPSG number or .prj/.json. KML is clampToGround (altitude dropped).")
        info.setWordWrap(True)
        info.setStyleSheet("color:#777; font-size:10px; background:#fafafa; padding:6px; border-radius:4px;")
        layout.addWidget(info)
        layout.addStretch()
        # Init combos
        try:
            self._refresh_coord_combos()
            # Set SAF edit from saved
            try:
                self.coord_csf_edit.setText(str(getattr(self,'coord_surface_factor',1.0)))
            except: pass
        except: pass

    def _on_coord_manage(self):
        try:
            cur_epsg = int(self.coord_epsg or 6584)
        except:
            cur_epsg = 6584
        try:
            cur_saf = float(self.coord_surface_factor or 1.0)
        except:
            cur_saf = 1.0
        dlg = CoordinateSystemDialog(current_epsg=cur_epsg, current_saf=cur_saf, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            epsg, saf = dlg.result_values()
            from .coord_systems import EPSG_LIBRARY
            info = EPSG_LIBRARY.get(epsg)
            if not info or "2011" not in info.get("name",""):
                from PySide6.QtWidgets import QMessageBox
                m = QMessageBox(self)
                m.setIcon(m.Icon.Warning)
                m.setWindowTitle("Not 2011 Texas")
                m.setText(f"EPSG {epsg} is not 2011 Texas. Only 2011 Texas (6581-6588, 6577/6578) is supported per request.")
                m.setInformativeText("Select a 2011 Texas zone (e.g., 6584 TX North Central 2011 USft).")
                m.exec()
                return
            self.coord_epsg = epsg
            self.coord_surface_factor = float(saf)
            try:
                if hasattr(self,'coord_csf_edit'):
                    self.coord_csf_edit.blockSignals(True)
                    self.coord_csf_edit.setText(str(saf))
                    self.coord_csf_edit.blockSignals(False)
            except: pass
            try:
                for i in range(self.coord_epsg_combo.count()):
                    if self.coord_epsg_combo.itemData(i)==epsg:
                        self.coord_epsg_combo.setCurrentIndex(i)
                        break
                self._refresh_coord_combos()
            except: pass
            self.is_dirty = True
            try: self._refresh_coord_canvases()
            except: pass
            try:
                self._render_detail_vis_poly(); self._render_project_vis()
            except: pass
            self.statusBar().showMessage(f"Coordinate system updated: {epsg} TX 2011 + TXDOT SAF {saf} (from origin 0,0, top-layer)", 6000)

    def _refresh_coord_combos(self):
        try:
            from .coord_systems import epsg_choices_for_combo
            active = getattr(self, "coord_active_epsgs", DEFAULT_ACTIVE_EPSGS)
            # Enforce 2011 Texas only: filter any legacy if still in saved project
            try:
                from .coord_systems import EPSG_LIBRARY
                filtered = [e for e in active if EPSG_LIBRARY.get(e) and "2011" in EPSG_LIBRARY[e].get("name","") or e==4326]
                if set(filtered) != set(active):
                    active = filtered or DEFAULT_ACTIVE_EPSGS
                    self.coord_active_epsgs = active
            except: pass
            choices = epsg_choices_for_combo(active)
            self.coord_epsg_combo.blockSignals(True)
            self.coord_epsg_combo.clear()
            for epsg, label in choices:
                self.coord_epsg_combo.addItem(label, epsg)
            cur = getattr(self, "coord_epsg", 6584)
            for i in range(self.coord_epsg_combo.count()):
                if self.coord_epsg_combo.itemData(i)==cur:
                    self.coord_epsg_combo.setCurrentIndex(i)
                    break
            self.coord_epsg_combo.blockSignals(False)
            self.coord_active_combo.clear()
            for epsg, label in choices:
                self.coord_active_combo.addItem(label)
            try:
                import pyproj
                has = True
                ver = pyproj.__version__
            except:
                has = False
                ver = ""
            saf = self.coord_csf_edit.text() if hasattr(self,'coord_csf_edit') else '1.0'
            # SAF is top-layer scaler from origin 0,0 — no grid/ground toggle, infer from SAF !=1
            try:
                saf_f = float(saf)
                mode_s = "Ground (÷SAF from 0,0 for KML)" if abs(saf_f-1.0)>1e-12 else "Grid (SAF 1.0)"
            except:
                mode_s = "SAF invalid"
            if has:
                self.coord_status.setText(f"pyproj {ver} — {len(choices)} active (2011 Texas only), TOP-LAYER SAF={saf} {mode_s} from origin 0,0")
                self.coord_status.setStyleSheet("color:#2e7d32; font-size:11px;")
            else:
                self.coord_status.setText(f"pyproj NOT installed — Texas 2011 LCC fallback for 6581-6588/6577-6578 (SAF={saf} {mode_s}, origin 0,0).")
                self.coord_status.setStyleSheet("color:#ef6c00; font-size:11px;")
        except Exception as e:
            self.coord_status.setText(f"Refresh failed: {e}")


    # ----- Numbering Error (Master Overlap) -----
    def _build_numbering_error_tab(self):
        self.numbering_tab = QWidget()
        layout = QVBoxLayout(self.numbering_tab)
        layout.setContentsMargins(6,4,6,4)
        title = QLabel("Numbering Error — PtNums that already exist in Master File (Protected)")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)
        hint = QLabel("Shows numbers in your Consolidated Field Data that already exist in the master file CSV. Master points are PROTECTED and cannot be overwritten — new data must renumber (single/range). This is separated from Duplicate Error (internal Exact/Similar/CloseNE). Use Global Renumber or single/range tools to resolve.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(hint)
        top = QHBoxLayout()
        self.numbering_refresh_btn = QPushButton("Refresh Against Master")
        self.numbering_refresh_btn.setToolTip("Re-scan Consolidated vs Master file for overlapping PtNums")
        self.numbering_refresh_btn.clicked.connect(self._refresh_numbering_error)
        self.numbering_renum_single_btn = QPushButton("Renumber Single...")
        self.numbering_renum_single_btn.clicked.connect(self._renumber_single)
        self.numbering_renum_range_btn = QPushButton("Renumber Range...")
        self.numbering_renum_range_btn.clicked.connect(self._renumber_range)
        self.numbering_global_btn = QPushButton("Global Renumber...")
        self.numbering_global_btn.clicked.connect(self._on_global_renumber)
        top.addWidget(self.numbering_refresh_btn)
        top.addWidget(self.numbering_renum_single_btn)
        top.addWidget(self.numbering_renum_range_btn)
        top.addWidget(self.numbering_global_btn)
        top.addStretch()
        layout.addLayout(top)
        self.numbering_path_label = QLabel("No master file set — set in File > Project Paths > Master File")
        self.numbering_path_label.setWordWrap(True)
        self.numbering_path_label.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(self.numbering_path_label)
        self.numbering_table = QTableWidget()
        self.numbering_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.numbering_table.customContextMenuRequested.connect(lambda pos, t=self.numbering_table: self._on_error_table_context_menu(t, pos))
        self.numbering_table.setColumnCount(7)
        self.numbering_table.setHorizontalHeaderLabels(["OID","PtNum","N","E","Z","Desc","Master Detail"])
        self.numbering_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.numbering_table.setSortingEnabled(True)
        self.numbering_table.setAlternatingRowColors(True)
        self.numbering_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.numbering_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.numbering_table)
        self._numbering_all_rows = []
        # Do not addTab here — shown via View or after checks
        # self.tabs.addTab(self.numbering_tab, "Numbering Error")
        self._update_numbering_title()

    def _update_numbering_title(self):
        try:
            n = self.numbering_table.rowCount() if hasattr(self, 'numbering_table') and self.numbering_table else 0
            idx = self.tabs.indexOf(self.numbering_tab)
            if idx != -1:
                self.tabs.setTabText(idx, f"Numbering Error ({n})" if n else "Numbering Error")
        except: pass

    def _refresh_numbering_error(self):
        try:
            from .config import read_master_ptnums
            master_path = getattr(self, "master_file_path", "") or ""
            if not master_path or not pathlib.Path(master_path).exists():
                self.numbering_path_label.setText("No master file set — set in Project Paths > Master File (all master points protected)")
                self._update_numbering_title()
                return
            master_set = read_master_ptnums(master_path)
            self._master_used = master_set
            self.numbering_path_label.setText(f"Master: {pathlib.Path(master_path).name} — {len(master_set)} protected points — scanning Consolidated for overlaps")
            # Build working rows from edit_table or file
            working_rows = []
            if self.edit_table and self.edit_table.rowCount()>0:
                for r in range(self.edit_table.rowCount()):
                    row = [self.edit_table.item(r,c).text().strip() if self.edit_table.item(r,c) else "" for c in range(min(8, self.edit_table.columnCount()))]
                    working_rows.append(row)
            elif self.edit_file_path and pathlib.Path(self.edit_file_path).exists():
                working_rows = self._read_working_file(self.edit_file_path)
            else:
                self.numbering_path_label.setText("No Consolidated file loaded")
                return
            # Find overlaps: PtNum (as int via core) in master
            import re as _re
            overlaps = []
            for row in working_rows:
                if len(row)<2: continue
                pt = row[1]
                m = _re.search(r'-?\d+', pt)
                if m:
                    try:
                        n = int(m.group(0))
                        if n in master_set:
                            overlaps.append(row)
                    except: pass
            self._fill_numbering_table(overlaps, master_set)
            if overlaps:
                self._show_tab(self.numbering_tab, "Numbering Error")
            self.summary_label.setText(f"Numbering Error: {len(overlaps)} PtNums overlap master (protected) — renumber required")
            try: self._refresh_steps()
            except: pass
            try: self._update_numbering_title()
            except: pass
            # Also refresh Duplicate to remove master overlaps from its view (filter)
            try: self._apply_check_filter()
            except: pass
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.warning(self, "Numbering Error", str(e))

    def _fill_numbering_table(self, overlaps, master_set):
        from PySide6.QtGui import QBrush, QColor
        if not hasattr(self, 'numbering_table') or self.numbering_table is None:
            return
        self.numbering_table.blockSignals(True)
        self.numbering_table.setSortingEnabled(False)
        self.numbering_table.setRowCount(0)
        self._numbering_all_rows = overlaps[:]
        for row in overlaps:
            oid = row[0] if len(row)>0 else ""
            pt = row[1] if len(row)>1 else ""
            n = row[2] if len(row)>2 else ""
            e = row[3] if len(row)>3 else ""
            z = row[4] if len(row)>4 else ""
            desc = row[5] if len(row)>5 else ""
            detail = f"PtNum {pt} already in master — protected"
            r = self.numbering_table.rowCount()
            self.numbering_table.insertRow(r)
            self.numbering_table.setItem(r,0, QTableWidgetItem(oid))
            self.numbering_table.setItem(r,1, QTableWidgetItem(pt))
            self.numbering_table.setItem(r,2, QTableWidgetItem(n))
            self.numbering_table.setItem(r,3, QTableWidgetItem(e))
            self.numbering_table.setItem(r,4, QTableWidgetItem(z))
            self.numbering_table.setItem(r,5, QTableWidgetItem(desc))
            self.numbering_table.setItem(r,6, QTableWidgetItem(detail))
            for c in range(7):
                it = self.numbering_table.item(r,c)
                if it:
                    it.setBackground(QBrush(QColor("#FFCDD2")))
                    it.setToolTip(detail)
        self.numbering_table.resizeColumnsToContents()
        self.numbering_table.setSortingEnabled(True)
        self.numbering_table.blockSignals(False)

    def _duplicate_bypass_selected(self):
        """Bypass / Ignore selected CloseNE/SimilarNumber groups — marks Status as Bypassed/Ignored so error can be cleared."""
        if not hasattr(self, 'check_table') or self.check_table is None or self.check_table.rowCount()==0:
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.information(self, "Bypass", "No duplicate rows to bypass")
            return
        # Get selected rows or if none selected, prompt
        sel_rows = sorted({it.row() for it in self.check_table.selectedItems()}) if self.check_table.selectedItems() else []
        if not sel_rows:
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.information(self, "Bypass", "Select rows (at least one per GroupID) to bypass. CloseNE and SimilarNumber can be bypassed when they are not real errors.")
            return
        # For each selected row, set Status to Ignored/Bypassed, Comments to Bypassed by user
        from PySide6.QtGui import QBrush, QColor
        cnt=0
        for r in sel_rows:
            issue_it = self.check_table.item(r,1)
            issue = issue_it.text().strip() if issue_it else ""
            # Only allow bypass for CloseNE/SimilarNumber (ExactDuplicate should be resolved via Merge/Remove/Renumber, but allow if user insists)
            status_item = self.check_table.item(r,11)
            comments_item = self.check_table.item(r,12)
            if status_item is None:
                continue
            # If already Corrected/Merged, skip
            cur_status = status_item.text().strip() if status_item else ""
            if cur_status in ("Corrected","Merged"):
                continue
            status_item.setText("Ignored")
            if comments_item:
                comments_item.setText("Bypassed — user marked CloseNE/Similar as not an error")
            # Color green (handled)
            for c in range(self.check_table.columnCount()):
                it = self.check_table.item(r,c)
                if it:
                    it.setBackground(QBrush(QColor("#E8F5E9")))
            # Also update underlying unified rows
            try:
                oid = self.check_table.item(r,2).text().strip() if self.check_table.item(r,2) else ""
                for ur in getattr(self, "_check_all_rows", []):
                    if str(ur[3]).strip() == str(oid) and str(ur[2]).strip()==issue:
                        ur[7]="Ignored"
                        ur[8]="Bypassed — user marked as not an error"
            except: pass
            cnt+=1
        if cnt:
            self.check_dirty=True
            try: self._update_check_title()
            except: pass
            try: self._refresh_steps()
            except: pass
            try: self._autosave_fwc()
            except: pass
            self.summary_label.setText(f"Bypassed {cnt} rows (CloseNE/SimilarNumber ignored) — now counts as handled")
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.information(self, "Bypass", f"Bypassed {cnt} rows — Status set to Ignored (counts as cleared). Re-run Steps to see progress.")

    def _line_fix_auto_dialog(self):
        """Fix Checked (Review): the proposals, on the table, before anything is written.

        The proposals come from the same engine the single Fix uses (``linecheck.propose_fix``),
        so a word is *placed* where the command order wants it rather than appended to the end of
        the description; each applied one is then validated against the line, and the status line
        says what it cleared - including the issues it was not aimed at.
        """
        if not hasattr(self, 'line_table') or self.line_table.rowCount() == 0:
            QMessageBox.information(self, "Line Repair", "No line issues - Refresh Line Check first.")
            return
        proposals = []
        for r in range(self.line_table.rowCount()):
            if self.line_table.isRowHidden(r):
                continue
            status = self.line_table.item(r, 7).text().strip() if self.line_table.item(r, 7) else ""
            if status not in ("", "Open", "Still flagged"):
                continue
            issue = self._line_issue_of(r)
            oid = issue.get("oid") or ""
            cur = self._line_current_desc(oid)
            proposal = issue.get("proposal") or propose_line_fix(issue.get("issue_type", ""), cur, issue.get("line_id", ""))
            proposals.append({"row": r, "gid": issue.get("gid", ""), "issue": issue.get("issue_type", ""),
                              "oid": oid, "line_id": issue.get("line_id", ""), "cur": cur,
                              "proposed": proposal, "detail": issue.get("detail", "")})
        if not proposals:
            QMessageBox.information(self, "Line Repair", "No Open line issues to fix.")
            return
        # Nothing to propose is a finding too: say so rather than showing a dialog full of blanks.
        if not any(p["proposed"] and p["proposed"] != p["cur"] for p in proposals):
            QMessageBox.information(self, "Line Repair",
                                    "None of these issues has a safe guess - Key-In the corrected "
                                    "description, or Ignore the issue.")
            return
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        from PySide6.QtCore import Qt as _Qt
        dlg = QDialog(self)
        dlg.setWindowTitle("Line Repair - Fix Review")
        dlg.resize(1100, 500)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"{len(proposals)} open line issue(s). Ticked ones with a proposal are applied, "
                             f"then read back from the line to check they worked."))
        tbl = QTableWidget(len(proposals), 7)
        tbl.setHorizontalHeaderLabels(["Apply", "GroupID", "OID", "Issue", "LineID", "Current Desc", "Proposed Desc"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        from PySide6.QtGui import QColor, QBrush
        for i, pr in enumerate(proposals):
            can = bool(pr["proposed"]) and pr["proposed"] != pr["cur"]
            chk = QTableWidgetItem("")
            chk.setFlags(chk.flags() | _Qt.ItemFlag.ItemIsUserCheckable | _Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(_Qt.CheckState.Checked if can else _Qt.CheckState.Unchecked)
            if not can:
                chk.setFlags(chk.flags() & ~_Qt.ItemFlag.ItemIsEnabled)
            tbl.setItem(i, 0, chk)
            tbl.setItem(i, 1, QTableWidgetItem(str(pr["gid"])))
            tbl.setItem(i, 2, QTableWidgetItem(str(pr["oid"])))
            tbl.setItem(i, 3, QTableWidgetItem(pr["issue"]))
            tbl.setItem(i, 4, QTableWidgetItem(pr["line_id"]))
            tbl.setItem(i, 5, QTableWidgetItem(pr["cur"]))
            tbl.setItem(i, 6, QTableWidgetItem(pr["proposed"] or "(no safe guess - Key-In)"))
            if can:
                tbl.item(i, 6).setBackground(QBrush(QColor("#E3F2FD")))
            else:
                tbl.item(i, 6).setBackground(QBrush(QColor("#FFF3E0")))
        lay.addWidget(tbl)
        sel_row = QHBoxLayout()
        sel_all = QPushButton("Select All")
        sel_none = QPushButton("Select None")
        sel_row.addWidget(sel_all)
        sel_row.addWidget(sel_none)
        sel_row.addStretch()
        lay.addLayout(sel_row)

        def set_all(state):
            for i, pr in enumerate(proposals):
                if pr["proposed"] and pr["proposed"] != pr["cur"]:
                    tbl.item(i, 0).setCheckState(state)
        sel_all.clicked.connect(lambda: set_all(_Qt.CheckState.Checked))
        sel_none.clicked.connect(lambda: set_all(_Qt.CheckState.Unchecked))
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        lay.addWidget(btn_box)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        applied = verified = 0
        for i, pr in enumerate(proposals):
            if tbl.item(i, 0).checkState() != _Qt.CheckState.Checked:
                continue
            if not pr["proposed"] or pr["proposed"] == pr["cur"]:
                continue
            self._line_set_desc(pr["oid"], pr["proposed"])
            working_rows = self._line_working_rows()
            res = validate_line_fix(working_rows, pr["oid"], pr["proposed"],
                                    fieldbook_path=getattr(self, "fieldbook_path", None))
            self._line_mark_cleared(res, pr["oid"])
            self._line_mark(pr["row"], "Corrected" if res["ok"] else "Still flagged",
                            "#E8F5E9" if res["ok"] else "#FFF3E0")
            applied += 1
            verified += 1 if res["ok"] else 0
        if applied:
            try:
                self._autosave_fwk()
            except Exception:
                pass
            try:
                self._refresh_steps()
            except Exception:
                pass
            self.summary_label.setText(f"Line Fix Review: {applied} applied, {verified} verified clean "
                                       f"by re-reading the line")
        else:
            self.summary_label.setText("Line Fix Review: nothing applied")

    def _import_epsg(self):

        from PySide6.QtWidgets import QInputDialog, QMessageBox
        from .coord_systems import EPSG_LIBRARY, add_custom_epsg
        # Show list of all library EPSGs not yet active
        try:
            active = set(getattr(self, "coord_active_epsgs", []))
            all_eps = sorted(EPSG_LIBRARY.keys())
            choices = [f"{eps} — {EPSG_LIBRARY[eps]['name']}" for eps in all_eps if eps not in active]
            if not choices:
                QMessageBox.information(self, "Import EPSG", "All library EPSGs already active. To add a custom EPSG, enter it manually next.")
                # Still allow manual
                choices = []
            # Let user pick from combo via input dialog with edit
            # Simple: ask for EPSG number
            txt, ok = QInputDialog.getText(self, "Import EPSG", "Enter 2011 Texas EPSG to import (e.g., 6584 TX North Central USft, 6578 TX Central USft, 6582 TX North USft) or pick from: \n" + "\n".join(choices[:30]) + ("\n..." if len(choices)>30 else ""))
            if not ok or not txt.strip():
                return
            # Parse first integer
            import re
            m = re.search(r'\d{4,5}', txt)
            if not m:
                QMessageBox.warning(self, "Import EPSG", "No EPSG number found")
                return
            eps = int(m.group(0))
            # Migrate legacy to 2011 Texas only
            try:
                from .coord_systems import migrate_epsg
                me = migrate_epsg(eps)
                if me != eps:
                    from PySide6.QtWidgets import QMessageBox as _MB
                    _msg = _MB(self)
                    _msg.setIcon(_MB.Icon.Information)
                    _msg.setWindowTitle("Migrated to 2011 Texas")
                    _msg.setText(f"Legacy EPSG {eps} maps to 2011 Texas {me} ({EPSG_LIBRARY.get(me,{}).get('name','')}) — using 2011 only per request.")
                    _msg.exec()
                    eps = me
            except: pass
            if eps in active:
                QMessageBox.information(self, "Import EPSG", f"{eps} already active")
                return
            info = EPSG_LIBRARY.get(eps)
            name = info["name"] if info else f"EPSG:{eps}"
            # Enforce 2011 Texas only
            if info and "2011" not in info.get("name","") and eps != 4326:
                QMessageBox.warning(self, "Import EPSG", f"Only 2011 Texas State Plane (6581-6588, 6577/6578) + WGS84 4326 is supported. EPSG {eps} ({info.get('name','')}) is not 2011 Texas.")
                return
            ok_add = add_custom_epsg(eps, name)
            if ok_add and eps not in EPSG_LIBRARY:
                # add_custom will have added
                pass
            if eps not in getattr(self, "coord_active_epsgs", []):
                self.coord_active_epsgs.append(eps)
            self._refresh_coord_combos()
            # Select it
            for i in range(self.coord_epsg_combo.count()):
                if self.coord_epsg_combo.itemData(i)==eps:
                    self.coord_epsg_combo.setCurrentIndex(i)
                    self.coord_epsg = eps
                    break
            QMessageBox.information(self, "Import EPSG", f"Imported {eps} — {name}")
        except Exception as e:
            QMessageBox.warning(self, "Import EPSG", str(e))

    def _test_coord_convert(self):
        try:
            from .coord_systems import convert_to_wgs84
            # Get first point
            if not self.edit_table or self.edit_table.rowCount()==0:
                QMessageBox.information(self, "Test Convert", "No consolidated points loaded")
                return
            # Get EPSG and factor
            epsg = self.coord_epsg_combo.currentData() if hasattr(self, "coord_epsg_combo") else getattr(self, "coord_epsg", 6584)
            try:
                saf = float(self.coord_csf_edit.text().strip()) if self.coord_csf_edit.text().strip() else 1.0
            except:
                saf = 1.0
            factor = saf
            is_ground = self.coord_is_ground_cb.isChecked() if hasattr(self, "coord_is_ground_cb") else (self.coord_mode_combo.currentIndex()==0 if hasattr(self, "coord_mode_combo") else False)
            mode = "ground_to_grid" if is_ground else "grid_to_ground"
            # Take first row
            r=0
            n = self.edit_table.item(r,2).text().strip() if self.edit_table.item(r,2) else ""
            e = self.edit_table.item(r,3).text().strip() if self.edit_table.item(r,3) else ""
            pt = self.edit_table.item(r,1).text().strip() if self.edit_table.item(r,1) else ""
            lon, lat = convert_to_wgs84(n, e, epsg, factor, mode, is_ground=is_ground)
            if lon is None or lat is None:
                self.coord_status.setText("Test failed — pyproj not installed or N/E not numeric. pip install pyproj")
                self.coord_status.setStyleSheet("color:#c62828")
                QMessageBox.warning(self, "Test Convert", "Conversion failed — install pyproj:\n pip install pyproj\n\nOr check N/E are numeric and EPSG matches (6584 TX North Central 2011 USft for Mesquite).")
            else:
                self.coord_status.setText(f"Test OK: Pt {pt} N={n} E={e} (EPSG {epsg}, SAF {factor} {mode}) → lat {lat:.7f}, lon {lon:.7f}")
                self.coord_status.setStyleSheet("color:#2e7d32")
                QMessageBox.information(self, "Test Convert", f"Pt {pt}: N={n} E={e}\nEPSG {epsg} SAF {factor} ({mode})\n→ lat {lat:.7f}\n→ lon {lon:.7f}\n\nKML will use these.")
        except Exception as e:
            QMessageBox.warning(self, "Test Convert", str(e))

    # ----- Master protected helpers -----
    def _show_master_protected(self):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton
        from pathlib import Path
        d = QDialog(self)
        d.setWindowTitle("Master Protected Points — Cannot Be Overwritten")
        d.resize(600, 500)
        lay = QVBoxLayout(d)
        mpath = getattr(self, "master_file_path","") or ""
        mset = getattr(self, "_master_used", set())
        lay.addWidget(QLabel(f"Master file: {mpath or '(none set in Project Paths)'}"))
        lay.addWidget(QLabel(f"Protected count: {len(mset)} — any new point number already in this set must renumber (single/range)."))
        tbl = QTableWidget(len(mset), 1)
        tbl.setHorizontalHeaderLabels(["PtNum (protected)"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for i, n in enumerate(sorted(mset)):
            tbl.setItem(i,0, QTableWidgetItem(str(n)))
        lay.addWidget(tbl)
        btn = QPushButton("Close")
        btn.clicked.connect(d.accept)
        lay.addWidget(btn)
        d.exec()

    # ----- Renumber single/range (master protected) -----
    def _renumber_single(self):
        from .renumber_tool import SingleRenumberDialog, apply_single_to_table
        # Need a context: if duplicate table has selection, use that OID, else ask for OID
        oid = ""
        old_pt = ""
        try:
            # Try to get selected row from Duplicate tab
            if hasattr(self, "check_table") and self.check_table and self.check_table.selectedItems():
                rows = sorted({it.row() for it in self.check_table.selectedItems()})
                if rows:
                    r = rows[0]
                    oid = self.check_table.item(r,2).text().strip() if self.check_table.item(r,2) else ""
                    old_pt = self.check_table.item(r,3).text().strip() if self.check_table.item(r,3) else ""
            # Fallback try edit table selection
            if not oid and hasattr(self, "edit_table") and self.edit_table and self.edit_table.selectedItems():
                rows = sorted({it.row() for it in self.edit_table.selectedItems()})
                if rows:
                    r = rows[0]
                    oid = self.edit_table.item(r,0).text().strip() if self.edit_table.item(r,0) else ""
                    old_pt = self.edit_table.item(r,1).text().strip() if self.edit_table.item(r,1) else ""
        except: pass
        if not oid:
            from PySide6.QtWidgets import QInputDialog
            oid, ok = QInputDialog.getText(self, "Renumber Single", "Enter OID to renumber (from Consolidated Field Data col OID):")
            if not ok or not oid.strip():
                return
            oid = oid.strip()
            # Lookup old_pt
            try:
                for r in range(self.edit_table.rowCount()):
                    if self.edit_table.item(r,0).text().strip()==oid:
                        old_pt = self.edit_table.item(r,1).text().strip() if self.edit_table.item(r,1) else ""
                        break
            except: pass
        dlg = SingleRenumberDialog(self, oid=oid, old_pt=old_pt, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        new_pt = dlg.result_new
        # Ask which column to write: Dup_Renumber vs Global_Renumber
        from PySide6.QtWidgets import QInputDialog as _QID
        col, ok = _QID.getItem(self, "Target Column", "Write renumber to:", ["Dup_Renumber","Global_Renumber","Final_PtNum"], 0, False)
        if not ok:
            return
        target = col
        # For Final_PtNum, we actually write to Global_Renumber if master protected, but allow choose
        if target == "Final_PtNum":
            target = "Global_Renumber"
        if apply_single_to_table(self, oid, new_pt, target):
            self.summary_label.setText(f"Renumber single OID {oid} {old_pt} → {new_pt} in {target} (master protected checked)")
            self._refresh_steps()

    def _renumber_range(self):
        from .renumber_tool import RangeRenumberDialog
        dlg = RangeRenumberDialog(self, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        mapping = dlg.result_mapping
        if not mapping:
            return
        from PySide6.QtWidgets import QInputDialog as _QID
        col, ok = _QID.getItem(self, "Target Column", "Write range renumber to:", ["Dup_Renumber","Global_Renumber"], 0, False)
        if not ok:
            col = "Dup_Renumber"
        # Apply each old->new by finding rows with PtNum == old (may be multiple OIDs sharing old)
        applied = 0
        dup_col_found = -1
        # Find col index
        try:
            for c in range(self.edit_table.columnCount()):
                hdr = self.edit_table.horizontalHeaderItem(c)
                if hdr and hdr.text().strip()==col:
                    dup_col_found = c
                    break
        except: pass
        if dup_col_found==-1:
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.warning(self, "Range", f"Column {col} not found — ensure consolidated file has Corr_* columns")
            return
        # For each old->new, find all rows where PtNum col1 == old
        for old, new in mapping.items():
            old_s = str(old); new_s = str(new)
            for r in range(self.edit_table.rowCount()):
                pt_it = self.edit_table.item(r,1)
                if pt_it and pt_it.text().strip()==old_s:
                    item = self.edit_table.item(r, dup_col_found)
                    if item is None:
                        from PySide6.QtWidgets import QTableWidgetItem as _QI
                        item = _QI(new_s)
                        self.edit_table.setItem(r, dup_col_found, item)
                    else:
                        item.setText(new_s)
                    try:
                        from PySide6.QtGui import QColor, QBrush
                        item.setBackground(QBrush(QColor("#E3F2FD")))
                    except: pass
                    applied+=1
        if applied:
            try:
                self.edit_dirty=True
                self._update_edit_title()
                try: self._autosave_fwk()
                except: pass
            except: pass
            self.summary_label.setText(f"Renumber range {len(mapping)} points → {col}, applied to {applied} rows (master protected checked)")
            self._refresh_steps()
        else:
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.information(self, "Range", "No matching PtNum found for range — check old range values vs current file")

    # ----- KML Export -----
    def _export_kml(self):
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox, QInputDialog
        # Ensure we have consolidated points
        if not self.edit_table or self.edit_table.rowCount()==0:
            QMessageBox.information(self, "KML", "No consolidated points to export — create/open Consolidated Field Data first")
            return
        # Get EPSG and SAF from coord tab — TXDOT SOP: SAF from origin (0,0), Ground=Grid*SAF
        epsg = getattr(self, "coord_epsg", 6584)
        try:
            if hasattr(self, "coord_epsg_combo") and self.coord_epsg_combo.count()>0:
                epsg = self.coord_epsg_combo.currentData()
                self.coord_epsg = epsg
        except: pass
        saf = getattr(self, "coord_surface_factor", 1.0)
        try:
            if hasattr(self, "coord_csf_edit"):
                saf = float(self.coord_csf_edit.text().strip()) if self.coord_csf_edit.text().strip() else 1.0
                self.coord_surface_factor = saf
        except: saf = 1.0
        factor = saf
        # TXDOT SAF: is_ground checked means input is Ground (SAF applied) -> divides by SAF from origin 0,0
        is_ground = False
        try:
            if hasattr(self, "coord_is_ground_cb"):
                is_ground = self.coord_is_ground_cb.isChecked()
                # Sync legacy mode for compat
                mode = "ground_to_grid" if is_ground else "grid_to_ground"
                self.coord_factor_mode = mode
            elif hasattr(self, "coord_mode_combo"):
                mode = "ground_to_grid" if self.coord_mode_combo.currentIndex()==0 else "grid_to_ground"
                is_ground = (mode == "ground_to_grid")
                self.coord_factor_mode = mode
            else:
                mode = getattr(self, "coord_factor_mode", "grid_to_ground")
                is_ground = (mode == "ground_to_grid")
        except:
            mode = "grid_to_ground"
            is_ground = False
        # Check pyproj
        try:
            import pyproj
            has_pyproj = True
        except:
            has_pyproj = False
            ret = QMessageBox.warning(self, "KML — pyproj Missing", "pyproj not installed — needed for State Plane → WGS84.\n\nInstall with: pip install pyproj\n\nContinue with stub KML (no transform, will warn)?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if ret != QMessageBox.StandardButton.Yes:
                return
        # Choose file — support KML and KMZ (KMZ is zipped KML for Google Earth)
        base = str(Path(getattr(self,"current_file","") or getattr(self,"project_path","") or "."))
        try:
            pbase = Path(base)
            if pbase.is_file():
                base = str(pbase.parent)
            elif not pbase.exists():
                base = "."
        except: base = "."
        default = str(Path(base) / "Fieldwork_GoogleEarth.kml")
        path, selected_filter = QFileDialog.getSaveFileName(self, "Export KML/KMZ for Google Earth (Selected Points)", default, "KML (*.kml);;KMZ (*.kmz)")
        if not path:
            return
        p = Path(path)
        # If no extension, default to kml
        if p.suffix.lower() not in (".kml", ".kmz"):
            # Use filter hint or default to .kml
            if "KMZ" in selected_filter:
                p = p.with_suffix(".kmz")
            else:
                p = p.with_suffix(".kml")
        # Build working rows — SELECTED points only for repair (if selection exists, else all)
        working_rows = []
        selected_oids = set()
        try:
            if self.edit_table and self.edit_table.selectedItems():
                # Collect selected rows' OIDs
                sel_rows = {it.row() for it in self.edit_table.selectedItems()}
                for r in sel_rows:
                    oid_it = self.edit_table.item(r, 0)
                    if oid_it:
                        selected_oids.add(oid_it.text().strip())
        except: pass
        # If selection empty, also check duplicate/line/desc tables selections? For repair, prefer edit_table selection
        # If still empty, ask user whether to export all or selected
        use_selected = bool(selected_oids)
        if not use_selected:
            # No selection — ask
            from PySide6.QtWidgets import QMessageBox as _MB2
            ret = _MB2.question(self, "KML Export — Selection", "No points selected in Consolidated table.\n\nExport ALL points (Yes) or Cancel to select points first?", _MB2.StandardButton.Yes | _MB2.StandardButton.Cancel)
            if ret != _MB2.StandardButton.Yes:
                return
        # Build rows
        for r in range(self.edit_table.rowCount()):
            oid_it = self.edit_table.item(r, 0)
            oid = oid_it.text().strip() if oid_it else ""
            # If we have a selection, skip non-selected
            if use_selected and oid not in selected_oids:
                continue
            row = []
            for c in range(min(8, self.edit_table.columnCount())):
                it = self.edit_table.item(r,c)
                row.append(it.text().strip() if it else "")
            working_rows.append(row)
        if not working_rows:
            QMessageBox.information(self, "KML", "No points to export (selection empty)")
            return
        # Generate KML (or KMZ) — _write_kml handles both via extension
        ok = self._write_kml(p, working_rows, epsg, factor, mode, has_pyproj, is_ground)
        if ok:
            kind = "KMZ" if p.suffix.lower()==".kmz" else "KML"
            saf_label = f"SAF {factor} ({'Ground' if is_ground else 'Grid'} from 0,0)"
            QMessageBox.information(self, kind, f"Exported Google Earth {kind}:\n{p}\nEPSG {epsg} {saf_label} TXDOT origin 0,0 clampToGround\nPoints: {len(working_rows)} {'(selected)' if use_selected else '(all)'} \nOpen in Google Earth to verify.")
            self.summary_label.setText(f"{kind} exported: {p.name} ({len(working_rows)} pts {'selected' if use_selected else 'all'}, EPSG {epsg}, {saf_label})")
            try: self._refresh_steps()
            except: pass

    def _write_kml(self, dest: Path, working_rows, epsg, factor, mode, has_pyproj, is_ground=False):
        import re, zipfile
        """Write KML/KMZ points and any coded lines using the active Field Book meanings."""
        try:
            from .coord_systems import convert_to_wgs84
        except:
            convert_to_wgs84 = lambda n,e,epsg,factor,mode: (None,None)
        # Build point placemarks
        point_placemarks = []
        skipped = 0
        for row in working_rows:
            if len(row)<5:
                continue
            oid = row[0]; pt = row[1]; n=row[2]; e=row[3]; z=row[4] if len(row)>4 else ""; desc = row[5] if len(row)>5 else ""
            try:
                lon, lat = convert_to_wgs84(n, e, epsg, factor, mode, is_ground=is_ground)
            except:
                lon, lat = None, None
            if lon is None or lat is None:
                # Without pyproj, try to use N/E as lat/lon if epsg 4326
                if int(epsg)==4326:
                    try:
                        lat = float(n); lon = float(e)
                    except:
                        skipped+=1
                        continue
                else:
                    skipped+=1
                    continue
            # Style by point type? Use control/boundary/general coloring
            try:
                pt_int = int(re.search(r'-?\\d+', pt).group(0)) if re.search(r'-?\\d+', pt) else 0
                if 1 <= pt_int <= 999:
                    color = "ff0000ff"  # red for control (KML abgr)
                elif 1000 <= pt_int <= 9999:
                    color = "ff00a5ff"  # orange boundary
                else:
                    color = "ff32c800"  # green general
            except:
                color = "ff32c800"
            # TXDOT SOP: clampToGround, drop elev for KML/KMZ (if clampToGround, elev ignored)
            placemark = f"""    <Placemark>
        <name>{pt} (OID {oid})</name>
        <description><![CDATA[OID: {oid}<br>Pt: {pt}<br>N: {n} E: {e} Z: {z}<br>Desc: {desc}<br>State Plane EPSG {epsg} SAF {factor} ({"Ground" if is_ground else "Grid"} from origin 0,0)]]></description>
        <Style><IconStyle><color>{color}</color><scale>1.0</scale><Icon><href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href></Icon></IconStyle></Style>
        <Point><altitudeMode>clampToGround</altitudeMode><coordinates>{lon},{lat},0</coordinates></Point>
    </Placemark>"""
            point_placemarks.append(placemark)
        # Use Field Book command meanings rather than searching descriptions for token text.
        line_placemarks = []
        try:
            from .parse import parse_desc_field, build_f2f_set_from_fieldbook
            from ..core.fieldbook_syntax import COMMAND_MEANINGS
            fieldbook_path = getattr(self, "fieldbook_path", None)
            f2f = build_f2f_set_from_fieldbook(fieldbook_path or "")
            line_meanings = set(COMMAND_MEANINGS[:5])
            line_groups = {}
            for row in working_rows:
                oid = row[0]; pt = row[1]; n = row[2]; e = row[3]
                desc = row[5] if len(row) > 5 else ""
                if not desc:
                    continue
                try:
                    parsed = parse_desc_field(desc, f2f, fieldbook_path=fieldbook_path)
                    classified = parsed.get("code_classified", [])
                    for code_index, item in enumerate(classified):
                        if item.get("type") != "code" or item.get("status") not in ("exact", "line_instance"):
                            continue
                        attached = [command for command in classified
                                    if command.get("type") == "command"
                                    and command.get("status") == "valid"
                                    and command.get("attached_to") == code_index]
                        if not any(command.get("meaning") in line_meanings for command in attached):
                            continue
                        line_id = item.get("raw", "").strip()
                        lon, lat = convert_to_wgs84(n, e, epsg, factor, mode, is_ground=is_ground)
                        if lon is not None and lat is not None:
                            line_groups.setdefault(line_id, []).append((oid, pt, lon, lat))
                except Exception:
                    continue
            for line_id, points in line_groups.items():
                if len(points) < 2:
                    continue
                try:
                    points.sort(key=lambda point: int(point[0]) if str(point[0]).isdigit() else str(point[0]))
                except Exception:
                    pass
                coordinates = " ".join(f"{lon},{lat},0" for _, _, lon, lat in points)
                line_placemarks.append(f"""    <Placemark>
        <name>Line {line_id} ({len(points)} pts)</name>
        <description><![CDATA[Line {line_id} uses active Field Book command meanings — verify in field]]></description>
        <Style><LineStyle><color>ff0000ff</color><width>2</width></LineStyle></Style>
        <LineString><tessellate>1</tessellate><coordinates>{coordinates}</coordinates></LineString>
    </Placemark>""")
        except Exception as e:
            print(f"KML line build failed {e}")
        # Write file
        kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
    <name>Fieldwork Manager — {dest.stem}</name>
    <description><![CDATA[Export from Fieldwork Manager<br>EPSG {epsg} SAF {factor} ({mode}) TOP-LAYER from origin 0,0<br>Points: {len(point_placemarks)} lines: {len(line_placemarks)} skipped: {skipped}<br>Master protected: {len(getattr(self,'_master_used',set()))} points]]></description>
""" + "\n".join(point_placemarks) + "\n" + "\n".join(line_placemarks) + "\n</Document>\n</kml>"
        # Handle KMZ (zip containing doc.kml) vs KML
        try:
            if dest.suffix.lower() == ".kmz":
                # Write KML into zip as doc.kml
                import io
                with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("doc.kml", kml.encode("utf-8"))
            else:
                dest.write_text(kml, encoding="utf-8")
            return True
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.warning(self, "KML write", str(e))
            return False


    def _on_dup_renumber(self):
        """Renumber duplicates after Auto — hole/at-end/keyed per point, writes to Dup_Renumber col (final collapsed)."""
        if self.edit_table is None or self.edit_table.rowCount()==0:
            QMessageBox.warning(self, "Renumber Dups", "No consolidated file")
            return
        # Collect duplicate OIDs needing renumber: those in duplicate groups where status not Open? Actually after Fix Auto, Merge/Removed points could be renumbered instead of removed.
        # For this dialog we collect all OIDs that are in duplicate groups (from _check_all_rows) where they are Exact/Similar with >1 per group.
        # Simplest: offer all points that share PtNum with another (from edit_table) — exact duplicate numbers
        from collections import defaultdict
        pt_to_oids=defaultdict(list)
        for rr in range(self.edit_table.rowCount()):
            oid=self.edit_table.item(rr,0).text().strip() if self.edit_table.item(rr,0) else ""
            pt=self.edit_table.item(rr,1).text().strip() if self.edit_table.item(rr,1) else ""
            if pt:
                pt_to_oids[pt.casefold()].append(oid)
        dup_oids=[]
        for pt, oids in pt_to_oids.items():
            if len(oids)>1:
                # Keep all but first (smallest OID) as renumber candidates (primary keeps original)
                try: s=sorted(oids, key=lambda x: int(x) if x.isdigit() else x)
                except: s=oids
                dup_oids.extend(s[1:])
        if not dup_oids:
            QMessageBox.information(self, "Renumber Dups", "No duplicate PtNums found — Run Checks first or duplicates already resolved")
            return
        # Build dup_rows
        dup_rows=[]
        for oid in dup_oids:
            # Find cur pt
            cur=""
            for rr in range(self.edit_table.rowCount()):
                if self.edit_table.item(rr,0).text().strip()==str(oid):
                    cur=self.edit_table.item(rr,1).text().strip() if self.edit_table.item(rr,1) else ""
                    break
            dup_rows.append({"oid": oid, "cur_pt": cur})
        # Collect used numbers
        used=set()
        for rr in range(self.edit_table.rowCount()):
            it=self.edit_table.item(rr,1)
            if it and it.text().strip().lstrip("-").isdigit():
                try: used.add(int(it.text().strip()))
                except: pass
            # Also include already assigned Dup_Renumber (col 12) and Global_Renumber (13)
            if self.edit_table.columnCount()>12:
                it2=self.edit_table.item(rr,12)
                if it2 and it2.text().strip().lstrip("-").isdigit():
                    try: used.add(int(it2.text().strip()))
                    except: pass
                if self.edit_table.columnCount()>13:
                    it3=self.edit_table.item(rr,13)
                    if it3 and it3.text().strip().lstrip("-").isdigit():
                        try: used.add(int(it3.text().strip()))
                        except: pass
        # Also master
        master_used=set()
        try:
            from .config import read_master_ptnums
            master_used=read_master_ptnums(getattr(self,"master_file_path",""))
        except: master_used=set()
        external=getattr(self,"_external_used", set())
        from .clean import DuplicateRenumberDialog
        dlg=DuplicateRenumberDialog(dup_rows, crew=3, used_numbers=used, external_used=external, master_used=master_used, parent=self)
        if dlg.exec()!=dlg.DialogCode.Accepted: return
        mp=dlg.result_map
        # Write to Dup_Renumber col 12
        for oid, (new_pt, method, detail) in mp.items():
            for rr in range(self.edit_table.rowCount()):
                if self.edit_table.item(rr,0).text().strip()==str(oid):
                    # Ensure columns exist
                    if self.edit_table.columnCount()<=12:
                        continue
                    self.edit_table.item(rr,12).setText(str(new_pt))
                    # Also set Corr_Detail? For now set Corr_Detail col 11 with method
                    self.edit_table.item(rr,11).setText(detail)
                    # Set Final_PtNum col 14 as computed (dup overwrites original, global later overwrites dup)
                    try:
                        from .config import final_ptnum_for_oid
                        cur_pt=self.edit_table.item(rr,1).text().strip() if self.edit_table.item(rr,1) else ""
                        dup_renum=self.edit_table.item(rr,12).text().strip() if self.edit_table.columnCount()>12 else ""
                        glob_renum=self.edit_table.item(rr,13).text().strip() if self.edit_table.columnCount()>13 else ""
                        final=final_ptnum_for_oid(cur_pt, dup_renum, glob_renum)
                        if self.edit_table.columnCount()>14:
                            self.edit_table.item(rr,14).setText(final)
                    except: pass
                    break
        self.edit_dirty=True
        self._update_edit_title()
        for ci in range(8,15): self.edit_table.setColumnHidden(ci, False)
        self.summary_label.setText(f"Renumber Dups: {len(mp)} points → Dup_Renumber column (final collapsed), method hole/at-end/keyed")
        try: self._refresh_steps()
        except: pass
        # Autosave working file?
        try: self._write_working_rows([[self.edit_table.item(rr,c).text() if self.edit_table.item(rr,c) else "" for c in range(self.edit_table.columnCount())] for rr in range(self.edit_table.rowCount())], self.edit_file_path)
        except: pass

    def _on_global_renumber(self):
        """Global Renumber — overwrites duplicate renumber, reads duplicate corrected numbers before originals."""
        if self.edit_table is None or self.edit_table.rowCount()==0:
            QMessageBox.warning(self, "Global Renumber", "No consolidated file")
            return
        # For global, offer all points (or at least those not Removed) — user can filter
        rows=[]
        for rr in range(self.edit_table.rowCount()):
            oid=self.edit_table.item(rr,0).text().strip() if self.edit_table.item(rr,0) else ""
            cur=self.edit_table.item(rr,1).text().strip() if self.edit_table.item(rr,1) else ""
            rows.append({"oid": oid, "cur_pt": cur})
        if not rows:
            QMessageBox.information(self, "Global Renumber", "No points")
            return
        # Collect used
        used=set()
        for rr in range(self.edit_table.rowCount()):
            it=self.edit_table.item(rr,1)
            if it and it.text().strip().lstrip("-").isdigit():
                try: used.add(int(it.text().strip()))
                except: pass
        # dup_corrected from Dup_Renumber col 12
        dup_corrected=set()
        for rr in range(self.edit_table.rowCount()):
            if self.edit_table.columnCount()>12:
                it=self.edit_table.item(rr,12)
                if it and it.text().strip().lstrip("-").isdigit():
                    try: dup_corrected.add(int(it.text().strip()))
                    except: pass
        master_used=set()
        try:
            from .config import read_master_ptnums
            master_used=read_master_ptnums(getattr(self,"master_file_path",""))
        except: master_used=set()
        external=getattr(self,"_external_used", set())
        # For dialog, let user pick subset: show dialog with all rows but they can Select All/Clear All
        # For MVP, just show first 30 to avoid huge dialog if many points?
        # Show all
        from .clean import GlobalRenumberDialog
        dlg=GlobalRenumberDialog(rows, crew=3, used_numbers=used, master_used=master_used, dup_corrected=dup_corrected, parent=self)
        if dlg.exec()!=dlg.DialogCode.Accepted: return
        mp=dlg.result_map
        for oid, (new_pt, method, detail) in mp.items():
            for rr in range(self.edit_table.rowCount()):
                if self.edit_table.item(rr,0).text().strip()==str(oid):
                    if self.edit_table.columnCount()<=13: continue
                    self.edit_table.item(rr,13).setText(str(new_pt))
                    self.edit_table.item(rr,11).setText(detail)
                    try:
                        from .config import final_ptnum_for_oid
                        cur_pt=self.edit_table.item(rr,1).text().strip() if self.edit_table.item(rr,1) else ""
                        dup_renum=self.edit_table.item(rr,12).text().strip() if self.edit_table.columnCount()>12 else ""
                        glob_renum=self.edit_table.item(rr,13).text().strip() if self.edit_table.columnCount()>13 else ""
                        final=final_ptnum_for_oid(cur_pt, dup_renum, glob_renum)
                        if self.edit_table.columnCount()>14:
                            self.edit_table.item(rr,14).setText(final)
                    except: pass
                    break
        self.edit_dirty=True
        self._update_edit_title()
        for ci in range(8,15): self.edit_table.setColumnHidden(ci, False)
        self.summary_label.setText(f"Global Renumber: {len(mp)} points → Global_Renumber column (overwrites dup) — master {len(master_used)} + dup corrected {len(dup_corrected)} treated as used before originals")

    def _on_line_repair(self):
        self._refresh_line_repair()
        if hasattr(self,'line_repair_tab') and self.line_repair_tab:
            self._show_tab(self.line_repair_tab, "Line Repair")



    def _update_desc_parse_title(self):
        if self.desc_parse_tab is None:
            return
        idx = self.tabs.indexOf(self.desc_parse_tab)
        if idx == -1:
            return
        # No dirty flag for now — flag-only, but could add star if needed
        self.tabs.setTabText(idx, "Description Error")

    def _update_desc_parse_hint(self):
        if self.desc_parse_table is None or self.desc_parse_hint_label is None:
            return
        self.desc_parse_hint_label.setVisible(self.desc_parse_table.rowCount() == 0)

    def _update_desc_parse_path_label(self):
        if self.desc_parse_path_label is None:
            return
        if not self.fieldbook_path or not __import__('pathlib').Path(self.fieldbook_path).exists():
            self.desc_parse_path_label.setText("No F2F loaded — use Open Field Book or Tools > Convert to enable code checks")
            self.desc_parse_path_label.setToolTip("No Field Book loaded")
        else:
            try:
                s = build_f2f_set_from_fieldbook(self.fieldbook_path)
                base = __import__('os').path.basename(self.fieldbook_path)
                self.desc_parse_path_label.setText(f"F2F: {base}  ({len(s)} codes)")
                self.desc_parse_path_label.setToolTip(f"{self.fieldbook_path}\n{len(s)} codes — tokens: missing dash/space tolerant, strip trailing digits only after miss, orphan commands flagged")
            except Exception:
                base = __import__('os').path.basename(self.fieldbook_path)
                self.desc_parse_path_label.setText(f"F2F: {base}")
                self.desc_parse_path_label.setToolTip(self.fieldbook_path)

    def _fill_desc_parse_table(self, rows):
        """Fill Description Parse table from list of row dicts (already parsed)."""
        from PySide6.QtGui import QBrush, QColor
        self.desc_parse_table.blockSignals(True)
        self.desc_parse_table.setSortingEnabled(False)
        self.desc_parse_table.setRowCount(0)
        self._desc_parse_all_rows = rows[:] if rows else []
        for r in rows:
            row_idx = self.desc_parse_table.rowCount()
            self.desc_parse_table.insertRow(row_idx)
            # r is list matching DESC_PARSE_HEADERS: OID, PtNum, N, E, Z, RawDesc, ParsedCode, FreeDesc, Flags, FlagDetail, Correction, Ignore
            for c, val in enumerate(r):
                if c in (0,):  # OID numeric
                    item = NumericSortItem(val)
                    item.setText(str(val))
                elif c in (1,):  # PointNumber
                    item = NaturalSortItem(str(val), letters_first=True)
                else:
                    item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~__import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
                # Color flags
                flags_str = r[8] if len(r) > 8 else ""
                if flags_str:
                    if "MisplacedAfterSeparator" in flags_str:
                        item.setBackground(QBrush(QColor("#FFF3E0")))  # orange tint
                    elif "UnknownCode" in flags_str:
                        item.setBackground(QBrush(QColor("#FFEBEE")))  # red tint
                    elif "OrphanCommand" in flags_str:
                        item.setBackground(QBrush(QColor("#F3E5F5")))  # purple tint
                self.desc_parse_table.setItem(row_idx, c, item)
        self.desc_parse_table.resizeColumnsToContents()
        self.desc_parse_table.setSortingEnabled(True)
        self.desc_parse_table.blockSignals(False)
        self._update_desc_parse_hint()
        self._update_desc_parse_path_label()
        self._apply_desc_parse_filter()

    def _fill_check_table_unified(self, unified_rows, working_rows):
        """Fill Duplicate Error tab from unified OID-minimal rows — live-pull raw via OID. Only DisplayTab=Duplicate shown."""
        from PySide6.QtGui import QBrush, QColor
        self.check_table.blockSignals(True)
        self.check_table.setSortingEnabled(False)
        self.check_table.setRowCount(0)
        # Keep unfiltered copy (unified)
        self._check_all_rows = [r for r in unified_rows if r[1]=="Duplicate"] if unified_rows else []
        # Build OID -> working row map for live pull
        oid_map = {wr[0]: wr for wr in (working_rows or [])}
        # Group for styling
        prev_gid = None
        for r in unified_rows:
            if r[1] != "Duplicate":
                continue
            # r is [GroupID, DisplayTab, IssueType, OID, Flags, Detail, FlagDetail, Status, Comments] (9 cols)
            gid, display, issue, oid, flags, detail, flagdetail, status, comments = (r + [""]*9)[:9]
            raw = oid_map.get(str(oid))
            if raw:
                # raw is 8-col [OID, PtNum, N, E, Z, Desc, Parent, Source]
                pt, n, e, z, desc, parent, source = raw[1], raw[2], raw[3], raw[4], raw[5], raw[6], raw[7]
            else:
                pt, n, e, z, desc, parent, source = "<missing>", "", "", "", "<missing>", "", ""
                # If OID not found, show missing but keep error row
            # Build display row matching CHECK_REPORT_HEADERS (13 cols) via live pull
            # Headers: GroupID, IssueType, OID, PtNum, N, E, Z, Desc, Parent, Source, Detail, Status, Comments
            display_row = [gid, issue, oid, pt, n, e, z, desc, parent, source, detail, status, comments]
            row_idx = self.check_table.rowCount()
            self.check_table.insertRow(row_idx)
            is_first = (gid != prev_gid)
            if is_first:
                prev_gid = gid
            for c, val in enumerate(display_row):
                # Use same styling as _fill_check_table
                if c in (0, 2):
                    item = NumericSortItem(val)
                    item.setText(str(val))
                elif c == 3:
                    disp = "    ↳ " + str(val) if not is_first else str(val)
                    item = NaturalSortItem(disp, letters_first=True)
                else:
                    item = QTableWidgetItem(str(val))
                if c in (11,12):
                    item.setFlags(item.flags() | __import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~__import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
                if is_first:
                    f = item.font(); f.setBold(True); item.setFont(f)
                    if issue == "ExactDuplicate": bg = QColor("#E3F2FD")
                    elif issue == "SimilarNumber": bg = QColor("#FFF8E1")
                    elif issue == "CloseNE": bg = QColor("#E8F5E9")
                    else: bg = QColor("#F5F5F5")
                    item.setBackground(QBrush(bg))
                else:
                    if issue == "SimilarNumber": item.setBackground(QBrush(QColor("#FFFDE7")))
                    elif issue == "CloseNE": item.setBackground(QBrush(QColor("#F1F8E9")))
                self.check_table.setItem(row_idx, c, item)
        self.check_table.resizeColumnsToContents()
        self.check_table.setSortingEnabled(True)
        self.check_table.blockSignals(False)
        self._update_check_hint()
        self._update_check_path_label()
        self._apply_check_filter()

    def _fill_desc_parse_table_unified(self, unified_rows, working_rows, fresh=False):
        """Fill Description Error tab from unified OID-minimal — flagged-only, live-pull raw. Only DisplayTab=Description shown. Update colors based on existing changes."""
        from PySide6.QtGui import QBrush, QColor
        # Capture existing Correction/Ignore/Removed states before clearing (for color update on open)
        # For fresh Run Checks (fresh=True), do NOT pull old .fwc/.chk data — run checks is fresh against consolidated+fieldbook
        existing_corr = {}
        existing_ignore = {}
        if not fresh:
            try:
                if self.desc_parse_table and self.desc_parse_table.rowCount() > 0:
                    for rr in range(self.desc_parse_table.rowCount()):
                        oid_it = self.desc_parse_table.item(rr, 0)
                        if not oid_it:
                            continue
                        o = oid_it.text().strip()
                        corr_it = self.desc_parse_table.item(rr, 10)
                        ign_it = self.desc_parse_table.item(rr, 11)
                        if corr_it and corr_it.text().strip():
                            existing_corr[o] = corr_it.text().strip()
                        if ign_it and ign_it.text().strip():
                            existing_ignore[o] = ign_it.text().strip()
            except Exception:
                pass
        # Also check edit_table for Removed meta (persisted) — only when not fresh (fresh New Run Checks must not load leftovers)
        edit_removed = set()
        if not fresh:
            try:
                if self.edit_table and self.edit_table.rowCount() > 0:
                    for rr in range(self.edit_table.rowCount()):
                        oid_it = self.edit_table.item(rr, 0)
                        if not oid_it:
                            continue
                        o = oid_it.text().strip()
                        if self.edit_table.columnCount() > 10:
                            status_it = self.edit_table.item(rr, 10)
                            if status_it and status_it.text().strip() == "Removed":
                                edit_removed.add(o)
            except Exception:
                pass
        self.desc_parse_table.blockSignals(True)
        self.desc_parse_table.setSortingEnabled(False)
        self.desc_parse_table.setRowCount(0)
        # Filter to Description
        desc_rows = [r for r in (unified_rows or []) if r[1]=="Description"]
        self._desc_parse_all_rows = desc_rows[:]  # store unified for filter
        oid_map = {wr[0]: wr for wr in (working_rows or [])}
        f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
        for r in desc_rows:
            gid, display, issue, oid, flags, detail, flagdetail, status, comments = (r + [""]*9)[:9]
            raw = oid_map.get(str(oid))
            if raw:
                pt, n, e, z, raw_desc, parent, source = raw[1], raw[2], raw[3], raw[4], raw[5], raw[6], raw[7]
            else:
                pt, n, e, z, raw_desc = "<missing>", "", "", "", "<missing>"
            parsed = parse_desc_field(raw_desc, f2f_set, fieldbook_path=self.fieldbook_path) if raw_desc != "<missing>" else {"parsed_code_str": "", "free_desc": ""}
            parsed_code = parsed.get("parsed_code_str","")
            free_desc = parsed.get("free_desc","")
            # FWC is source of truth: Status/Comments hold Corrected/Ignored/Removed
            # For fresh New Run Checks, unified rows have Status Open so corr/ign stay empty
            # For Open Report (fresh=False), pull from unified Status/Comments (persisted in .fwc)
            corr = ""
            ign = ""
            if status == "Corrected" and comments:
                corr = comments
            elif status == "Ignored":
                ign = "Ignored"
            elif status == "Removed":
                ign = "Removed"
            else:
                # Fallback for unsaved in-memory edits (not yet written to .fwc)
                corr = existing_corr.get(str(oid), "")
                ign = existing_ignore.get(str(oid), "")
                if str(oid) in edit_removed:
                    ign = "Removed"
                    corr = ""
            display_row = [oid, pt, n, e, z, raw_desc, parsed_code, free_desc, flags, flagdetail or detail, corr, ign]
            row_idx = self.desc_parse_table.rowCount()
            self.desc_parse_table.insertRow(row_idx)
            # Determine row color state
            is_removed = (ign == "Removed")
            is_ignored = (ign == "Ignored")
            has_corr = bool(corr)
            for c, val in enumerate(display_row):
                if c == 0:
                    item = NumericSortItem(val); item.setText(str(val))
                elif c == 1:
                    item = NaturalSortItem(str(val), letters_first=True)
                else:
                    item = QTableWidgetItem(str(val))
                if c == 10:  # Correction — now editable and next to Raw (visually moved), stores Auto Fix
                    item.setToolTip("Auto Fix (Correction) — editable directly, double-click row to Fix/Key-In; best guess shown")
                    item.setFlags(item.flags() | __import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
                    if has_corr:
                        item.setBackground(QBrush(QColor("#C8E6C9")))
                    elif is_removed or is_ignored:
                        item.setBackground(QBrush(QColor("#FFFFFF")))
                    else:
                        item.setBackground(QBrush(QColor("#F5F5F5")))
                elif c == 11:  # Ignore/Removed at end
                    if is_removed:
                        item.setToolTip("Removed — will be omitted in final out file")
                        item.setBackground(QBrush(QColor("#FFCDD2")))
                    elif is_ignored:
                        item.setToolTip("Ignored — counts as handled")
                        item.setBackground(QBrush(QColor("#C8E6C9")))
                    else:
                        item.setToolTip("Ignore/Removed — set via Clean dialog")
                        item.setBackground(QBrush(QColor("#F5F5F5")))
                else:
                    item.setFlags(item.flags() & ~__import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
                # Row background based on state
                if is_removed:
                    item.setBackground(QBrush(QColor("#FFEBEE")) if c not in (10,11) else item.background())
                    if c == 11:
                        item.setBackground(QBrush(QColor("#FFCDD2")))
                elif is_ignored or has_corr:
                    # Green for handled
                    if c not in (10,11):
                        item.setBackground(QBrush(QColor("#E8F5E9")))
                    if c == 10 and has_corr:
                        item.setBackground(QBrush(QColor("#C8E6C9")))
                    if c == 11 and is_ignored:
                        item.setBackground(QBrush(QColor("#C8E6C9")))
                elif flags and c not in (10,11):
                    # Red when nothing done yet
                    item.setBackground(QBrush(QColor("#FFCDD2")))
                # Ignore remains via dialog only; Correction is now directly editable (Auto Fix)
                if c == 11:
                    item.setFlags(item.flags() & ~__import__('PySide6.QtCore', fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)
                # Correction (10) stays editable per above
                self.desc_parse_table.setItem(row_idx, c, item)
        self.desc_parse_table.resizeColumnsToContents()
        self.desc_parse_table.setSortingEnabled(True)
        self.desc_parse_table.blockSignals(False)
        self._update_desc_parse_hint()
        self._update_desc_parse_path_label()
        self._apply_desc_parse_filter()
        try:
            self._update_renumber_lock()
        except Exception:
            pass

    def _apply_desc_parse_filter(self, _text=None):
        if self.desc_parse_table is None or self.desc_parse_filter_combo is None:
            return
        filt = self.desc_parse_filter_combo.currentText()
        for r in range(self.desc_parse_table.rowCount()):
            item = self.desc_parse_table.item(r, 8)  # Flags col
            txt = item.text() if item else ""
            if filt == "All":
                show = True
            else:
                show = filt in txt
            self.desc_parse_table.setRowHidden(r, not show)

    def _clear_desc_parse(self):
        if self.desc_parse_table is not None:
            self.desc_parse_table.setRowCount(0)
        self._desc_parse_all_rows = []
        self._update_desc_parse_hint()
        try:
            self._update_renumber_lock()
        except Exception:
            pass
        try:
            self._update_view_menu_state()
        except Exception:
            pass

    def _on_desc_clean_double_clicked(self, row, col):
        """Open CleanDescriptionDialog — Back goes to previous description for misclick recovery (flagged-only, display order). Fixed rows now also open directly."""
        if self.desc_parse_table is None or row < 0:
            return
        if row >= self.desc_parse_table.rowCount():
            return
        def _is_handled(r):
            ign = self.desc_parse_table.item(r, 11)
            corr = self.desc_parse_table.item(r, 10)
            is_ignored = ign and ign.text().strip() == "Ignored"
            is_removed = ign and ign.text().strip() == "Removed"
            has_corr = corr and corr.text().strip() != ""
            flags = self.desc_parse_table.item(r, 8)
            is_flagged = flags and flags.text().strip()
            return is_flagged and (is_ignored or is_removed or has_corr)
        # If double-clicked row is already fixed (handled), open dialog for that single point specifically (allow re-edit)
        try:
            if _is_handled(row):
                # Open single dialog for this row only, not sequential
                oid_item = self.desc_parse_table.item(row, 0)
                oid = oid_item.text().strip() if oid_item else ""
                pt_item = self.desc_parse_table.item(row, 1)
                pt = pt_item.text().strip() if pt_item else ""
                raw_item = self.desc_parse_table.item(row, 5)
                raw = raw_item.text() if raw_item else ""
                parsed_item = self.desc_parse_table.item(row, 6)
                parsed = parsed_item.text() if parsed_item else ""
                free_item = self.desc_parse_table.item(row, 7)
                free = free_item.text() if free_item else ""
                flags_item = self.desc_parse_table.item(row, 8)
                flags = flags_item.text() if flags_item else ""
                detail_item = self.desc_parse_table.item(row, 9)
                detail = detail_item.text() if detail_item else ""
                f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
                dlg = CleanDescriptionDialog(oid, pt, raw, flags, detail, parsed, free, f2f_set, self, fieldbook_path=self.fieldbook_path)
                # Disable Back for single-open
                try:
                    if hasattr(dlg, 'back_btn'):
                        dlg.back_btn.setEnabled(False)
                    elif hasattr(dlg, 'prev_btn'):
                        dlg.prev_btn.setEnabled(False)
                except: pass
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    action = dlg.result_action
                    new_desc = dlg.result_new_desc
                    # Apply same logic as sequential handler for single
                    if action == "remove":
                        remove_item = self.desc_parse_table.item(row, 11)
                        if remove_item is None:
                            from PySide6.QtWidgets import QTableWidgetItem
                            remove_item = QTableWidgetItem("Removed")
                            self.desc_parse_table.setItem(row, 11, remove_item)
                        else:
                            remove_item.setText("Removed")
                        corr_item = self.desc_parse_table.item(row, 10)
                        if corr_item:
                            corr_item.setText("")
                        from PySide6.QtGui import QBrush, QColor
                        remove_item.setBackground(QBrush(QColor("#FFCDD2")))
                        for c in range(self.desc_parse_table.columnCount()):
                            it = self.desc_parse_table.item(row, c)
                            if it:
                                it.setBackground(QBrush(QColor("#FFEBEE")))
                        self._update_desc_parse_hint()
                        try: self._update_renumber_lock()
                        except: pass
                        try:
                            for ur in getattr(self, "_desc_parse_all_rows", []):
                                if ur[3] == str(oid):
                                    ur[7] = "Removed"
                                    ur[8] = "Removed via Clean — will be omitted in final out file"
                                    break
                            self.check_dirty = True
                            self._update_check_title()
                            try: self._autosave_fwc()
                            except: pass
                        except: pass
                        self.summary_label.setText(f"OID {oid}: Removed (re-edited)")
                    elif action == "ignore":
                        ignore_item = self.desc_parse_table.item(row, 11)
                        if ignore_item is None:
                            from PySide6.QtWidgets import QTableWidgetItem
                            ignore_item = QTableWidgetItem("Ignored")
                            self.desc_parse_table.setItem(row, 11, ignore_item)
                        else:
                            ignore_item.setText("Ignored")
                        corr_item = self.desc_parse_table.item(row, 10)
                        if corr_item:
                            corr_item.setText("")
                        from PySide6.QtGui import QBrush, QColor
                        ignore_item.setBackground(QBrush(QColor("#C8E6C9")))
                        for c in range(self.desc_parse_table.columnCount()):
                            it = self.desc_parse_table.item(row, c)
                            if it:
                                it.setBackground(QBrush(QColor("#E8F5E9")))
                        self._update_desc_parse_hint()
                        try: self._update_renumber_lock()
                        except: pass
                        self.summary_label.setText(f"OID {oid}: Ignored — {flags} (re-edited)")
                        try:
                            for ur in getattr(self, "_desc_parse_all_rows", []):
                                if ur[3] == str(oid):
                                    ur[7] = "Ignored"
                                    ur[8] = f"Ignored — {flags}"
                                    break
                            self.check_dirty = True
                            self._update_check_title()
                            try: self._autosave_fwc()
                            except: pass
                        except: pass
                    elif action in ("fix", "key_token", "key_entire") and new_desc is not None:
                        corr_item = self.desc_parse_table.item(row, 10)
                        if corr_item is None:
                            from PySide6.QtWidgets import QTableWidgetItem
                            corr_item = QTableWidgetItem(new_desc)
                            self.desc_parse_table.setItem(row, 10, corr_item)
                        else:
                            corr_item.setText(new_desc)
                        from PySide6.QtGui import QBrush, QColor
                        corr_item.setBackground(QBrush(QColor("#C8E6C9")))
                        for c in range(self.desc_parse_table.columnCount()):
                            it = self.desc_parse_table.item(row, c)
                            if it:
                                it.setBackground(QBrush(QColor("#E8F5E9")))
                        ignore_item = self.desc_parse_table.item(row, 11)
                        if ignore_item:
                            ignore_item.setText("")
                            ignore_item.setBackground(QBrush(QColor("#FFFFFF")))
                        self._update_desc_parse_hint()
                        try: self._update_renumber_lock()
                        except: pass
                        try:
                            for ur in getattr(self, "_desc_parse_all_rows", []):
                                if ur[3] == str(oid):
                                    ur[7] = "Corrected"
                                    ur[8] = new_desc
                                    break
                            self.check_dirty = True
                            self._update_check_title()
                            try: self._autosave_fwc()
                            except: pass
                        except: pass
                        self.summary_label.setText(f"OID {oid}: {action} → Correction '{new_desc}' (re-edited)")
                return
        except Exception as e:
            print(f"fixed double-click handler failed: {e}")
        def _is_handled_original(r):
            ign = self.desc_parse_table.item(r, 11)
            corr = self.desc_parse_table.item(r, 10)
            is_ignored = ign and ign.text().strip() == "Ignored"
            is_removed = ign and ign.text().strip() == "Removed"
            has_corr = corr and corr.text().strip() != ""
            flags = self.desc_parse_table.item(r, 8)
            is_flagged = flags and flags.text().strip()
            return is_flagged and (is_ignored or is_removed or has_corr)
        def _ordered_error_oids():
            oids = []
            for rr in range(self.desc_parse_table.rowCount()):
                if _is_handled(rr):
                    continue
                if self.desc_parse_table.isRowHidden(rr):
                    continue
                f = self.desc_parse_table.item(rr, 8)
                if not f or not f.text().strip():
                    continue
                oid_it = self.desc_parse_table.item(rr, 0)
                if oid_it and oid_it.text().strip():
                    oids.append(oid_it.text().strip())
            return oids
        start_oid_item = self.desc_parse_table.item(row, 0)
        start_oid = start_oid_item.text().strip() if start_oid_item else ""
        # Session tracking: capture ordered list at session start for Back navigation (allow revisiting handled points for misclick)
        session_oids = _ordered_error_oids()
        # Also keep a live ordered for fallback, but session is source for navigation
        ordered = session_oids  # alias for compat
        try:
            idx = session_oids.index(start_oid) if start_oid in session_oids else 0
        except Exception:
            idx = 0
        # Keep history stack for debugging, session_oids is static snapshot (handled points stay in session for Back)
        # Disable sorting during sequential clean to keep display order stable (prevents skipping when row resorts)
        prev_sort = self.desc_parse_table.isSortingEnabled()
        if prev_sort:
            self.desc_parse_table.setSortingEnabled(False)
        # Loop to allow Back navigation across session (handled points remain accessible for misclick)
        while 0 <= idx < len(session_oids):
            cur_oid = session_oids[idx]
            # For live ordered compat, also sync ordered var
            ordered = session_oids
            cur_row = -1
            for rr in range(self.desc_parse_table.rowCount()):
                it = self.desc_parse_table.item(rr, 0)
                if it and it.text().strip() == str(cur_oid):
                    cur_row = rr
                    break
            if cur_row == -1:
                idx += 1
                continue
            try:
                oid_item = self.desc_parse_table.item(cur_row, 0)
                oid = oid_item.text().strip() if oid_item else ""
                pt_item = self.desc_parse_table.item(cur_row, 1)
                pt = pt_item.text().strip() if pt_item else ""
                raw_item = self.desc_parse_table.item(cur_row, 5)
                raw = raw_item.text() if raw_item else ""
                parsed_item = self.desc_parse_table.item(cur_row, 6)
                parsed = parsed_item.text() if parsed_item else ""
                free_item = self.desc_parse_table.item(cur_row, 7)
                free = free_item.text() if free_item else ""
                flags_item = self.desc_parse_table.item(cur_row, 8)
                flags = flags_item.text() if flags_item else ""
                detail_item = self.desc_parse_table.item(cur_row, 9)
                detail = detail_item.text() if detail_item else ""
            except Exception:
                idx += 1
                continue
            f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
            dlg = CleanDescriptionDialog(oid, pt, raw, flags, detail, parsed, free, f2f_set, self, fieldbook_path=self.fieldbook_path)
            try:
                if hasattr(dlg, 'back_btn'):
                    dlg.back_btn.setEnabled(idx > 0)
                elif hasattr(dlg, 'prev_btn'):
                    dlg.prev_btn.setEnabled(idx > 0)
            except Exception:
                pass
            if dlg.exec() != QDialog.DialogCode.Accepted:
                break
            action = dlg.result_action
            new_desc = dlg.result_new_desc
            if action in ("previous", "back"):
                # Back to previous in session (even if handled, for misclick recovery)
                idx = max(0, idx - 1)
                # No need to recompute ordered, session_oids is static
                continue
            elif action == "remove":
                remove_item = self.desc_parse_table.item(cur_row, 11)
                if remove_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    remove_item = QTableWidgetItem("Removed")
                    self.desc_parse_table.setItem(cur_row, 11, remove_item)
                else:
                    remove_item.setText("Removed")
                corr_item = self.desc_parse_table.item(cur_row, 10)
                if corr_item:
                    corr_item.setText("")
                from PySide6.QtGui import QBrush, QColor
                remove_item.setBackground(QBrush(QColor("#FFCDD2")))
                for c in range(self.desc_parse_table.columnCount()):
                    it = self.desc_parse_table.item(cur_row, c)
                    if it:
                        it.setBackground(QBrush(QColor("#FFEBEE")))
                self._update_desc_parse_hint()
                try:
                    self._update_renumber_lock()
                except Exception:
                    pass
                try:
                    self._update_view_menu_state()
                    self._update_run_checks_enabled()
                except Exception:
                    pass
                # FWC-only: store Removed in desc table and mark check report dirty; do NOT mutate .fwk — autosave to FWC immediately
                try:
                    for ur in getattr(self, "_desc_parse_all_rows", []):
                        if ur[3] == str(oid):
                            ur[7] = "Removed"
                            ur[8] = "Removed via Clean — will be omitted in final out file"
                            break
                    self.check_dirty = True
                    self._update_check_title()
                    self.summary_label.setText(f"OID {oid}: Removed (stored in .fwc only — .fwk unchanged)")
                    try:
                        self._autosave_fwc()
                    except Exception:
                        pass
                except Exception as e:
                    print(f"fwc update remove failed: {e}")
                    self.summary_label.setText(f"OID {oid}: Removed")
                # Move to next in session (handled point stays in session, so advance to next session index)
                idx += 1
                if idx >= len(session_oids):
                    break
                continue
            elif action == "ignore":
                ignore_item = self.desc_parse_table.item(cur_row, 11)
                if ignore_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    ignore_item = QTableWidgetItem("Ignored")
                    self.desc_parse_table.setItem(cur_row, 11, ignore_item)
                else:
                    ignore_item.setText("Ignored")
                corr_item = self.desc_parse_table.item(cur_row, 10)
                if corr_item:
                    corr_item.setText("")
                from PySide6.QtGui import QBrush, QColor
                ignore_item.setBackground(QBrush(QColor("#C8E6C9")))
                for c in range(self.desc_parse_table.columnCount()):
                    it = self.desc_parse_table.item(cur_row, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
                self._update_desc_parse_hint()
                try:
                    self._update_renumber_lock()
                except Exception:
                    pass
                self.summary_label.setText(f"OID {oid}: Ignored — {flags} (green)")
                # FWC-only: mark dirty, keep .fwk unchanged — autosave
                try:
                    for ur in getattr(self, "_desc_parse_all_rows", []):
                        if ur[3] == str(oid):
                            ur[7] = "Ignored"
                            ur[8] = f"Ignored — {flags}"
                            break
                    self.check_dirty = True
                    self._update_check_title()
                    try:
                        self._autosave_fwc()
                    except Exception:
                        pass
                except Exception as e:
                    print(f"fwc ignore update failed: {e}")
                # Move to next in session
                idx += 1
                if idx >= len(session_oids):
                    break
                continue
            elif action in ("fix", "key_token", "key_entire") and new_desc is not None:
                corr_item = self.desc_parse_table.item(cur_row, 10)
                if corr_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    corr_item = QTableWidgetItem(new_desc)
                    self.desc_parse_table.setItem(cur_row, 10, corr_item)
                else:
                    corr_item.setText(new_desc)
                from PySide6.QtGui import QBrush, QColor
                corr_item.setBackground(QBrush(QColor("#C8E6C9")))
                for c in range(self.desc_parse_table.columnCount()):
                    it = self.desc_parse_table.item(cur_row, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
                ignore_item = self.desc_parse_table.item(cur_row, 11)
                if ignore_item:
                    ignore_item.setText("")
                    ignore_item.setBackground(QBrush(QColor("#FFFFFF")))
                # FWC-only: keep .fwk Desc pristine; store Correction in .fwc Comments and desc table
                updated = True
                if updated:
                    self._update_desc_parse_hint()
                    try:
                        self._update_renumber_lock()
                    except Exception:
                        pass
                    # Persist correction into in-memory unified rows (Comments field holds corrected Desc) — autosave
                    try:
                        for ur in getattr(self, "_desc_parse_all_rows", []):
                            if ur[3] == str(oid):
                                ur[7] = "Corrected"
                                ur[8] = new_desc
                                break
                        self.check_dirty = True
                        self._update_check_title()
                        try:
                            self._autosave_fwc()
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"fwc fix update failed: {e}")
                    self.summary_label.setText(f"OID {oid}: {action} → Correction '{new_desc}' (stored in .fwc only — .fwk unchanged)")
                else:
                    QMessageBox.warning(self, "Clean Descriptions", f"OID {oid} not found in Edit table — open consolidated file first.")
                # Move to next in session
                idx += 1
                if idx >= len(session_oids):
                    break
                continue
            else:
                # Unknown action — break
                break
        # Restore sorting
        try:
            if prev_sort:
                self.desc_parse_table.setSortingEnabled(True)
        except Exception:
            pass


    def _clean_first(self):
        """Sequential clean — Back goes to previous description for misclick recovery (flagged-only display order). FIXED: no longer skips every other row."""
        if self.desc_parse_table is None or self.desc_parse_table.rowCount()==0:
            QMessageBox.information(self, "Clean", "No Flagged Descriptions — Run Checks First")
            return
        def is_handled(r):
            ign = self.desc_parse_table.item(r, 11)
            corr = self.desc_parse_table.item(r, 10)
            is_ignored = ign and ign.text().strip() == "Ignored"
            is_removed = ign and ign.text().strip() == "Removed"
            has_corr = corr and corr.text().strip() != ""
            flags = self.desc_parse_table.item(r, 8)
            is_flagged = flags and flags.text().strip()
            return is_flagged and (is_ignored or is_removed or has_corr)
        def _ordered_error_oids():
            oids = []
            for rr in range(self.desc_parse_table.rowCount()):
                if is_handled(rr):
                    continue
                if self.desc_parse_table.isRowHidden(rr):
                    continue
                f = self.desc_parse_table.item(rr, 8)
                if not f or not f.text().strip():
                    continue
                oid_it = self.desc_parse_table.item(rr, 0)
                if oid_it and oid_it.text().strip():
                    oids.append(oid_it.text().strip())
            return oids
        session_oids = _ordered_error_oids()
        ordered = session_oids
        if not session_oids:
            QMessageBox.information(self, "Clean", "All flagged rows already Corrected/Ignored/Removed")
            return
        idx = 0
        # Disable sorting during sequential clean
        prev_sort_first = self.desc_parse_table.isSortingEnabled()
        if prev_sort_first:
            self.desc_parse_table.setSortingEnabled(False)
        while 0 <= idx < len(session_oids):
            cur_oid = session_oids[idx]
            ordered = session_oids
            # Find row for cur_oid in current display order
            row = -1
            for r2 in range(self.desc_parse_table.rowCount()):
                it2 = self.desc_parse_table.item(r2, 0)
                if it2 and it2.text().strip() == str(cur_oid):
                    row = r2
                    break
            if row == -1:
                idx += 1
                continue
            # Pull data for this row
            try:
                oid = self.desc_parse_table.item(row, 0).text().strip()
                pt = self.desc_parse_table.item(row, 1).text().strip()
                raw = self.desc_parse_table.item(row, 5).text().strip()
                flags = self.desc_parse_table.item(row, 8).text().strip()
                detail = self.desc_parse_table.item(row, 9).text().strip()
                parsed = self.desc_parse_table.item(row, 6).text().strip()
                free = self.desc_parse_table.item(row, 7).text().strip()
            except:
                idx += 1
                continue
            f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
            dlg = CleanDescriptionDialog(oid, pt, raw, flags, detail, parsed, free, f2f_set, self, fieldbook_path=self.fieldbook_path)
            # Enable Back if not first (for misclick recovery)
            try:
                if hasattr(dlg, 'back_btn'):
                    dlg.back_btn.setEnabled(idx > 0)
                elif hasattr(dlg, 'prev_btn'):
                    dlg.prev_btn.setEnabled(idx > 0)
            except:
                pass
            res = dlg.exec()
            if res != QDialog.DialogCode.Accepted:
                break
            action = dlg.result_action
            new_desc = dlg.result_new_desc
            if action in ("previous", "back"):
                # Back to previous in session (even if handled, for misclick recovery)
                idx = max(0, idx - 1)
                # No need to recompute ordered, session_oids is static
                continue
            elif action == "remove":
                # Remove like Ignore — meta flagged for final out file processing (not immediate deletion)
                remove_item = self.desc_parse_table.item(row, 11)
                if remove_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    remove_item = QTableWidgetItem("Removed")
                    self.desc_parse_table.setItem(row, 11, remove_item)
                else:
                    remove_item.setText("Removed")
                corr_it = self.desc_parse_table.item(row, 10)
                if corr_it:
                    corr_it.setText("")
                from PySide6.QtGui import QBrush, QColor
                remove_item.setBackground(QBrush(QColor("#FFCDD2")))
                for c in range(self.desc_parse_table.columnCount()):
                    it = self.desc_parse_table.item(row, c)
                    if it:
                        it.setBackground(QBrush(QColor("#FFEBEE")))
                # FWC-only: do not mutate .fwk; store in unified rows and mark dirty
                try:
                    for ur in getattr(self, "_desc_parse_all_rows", []):
                        if ur[3] == str(oid):
                            ur[7] = "Removed"
                            ur[8] = "Removed via Clean — will be omitted in final out file"
                            break
                    self.check_dirty = True
                    self._update_check_title()
                except:
                    pass
                self.summary_label.setText(f"OID {oid}: Removed (stored in .fwc only → Next)")
                # FIXED: stay at same idx — next error slides into current position after handling
                try:
                    self._autosave_fwc()
                except Exception:
                    pass
                idx += 1
                if idx >= len(session_oids):
                    break
                continue
            elif action == "ignore":
                ignore_item = self.desc_parse_table.item(row, 11)
                if ignore_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    ignore_item = QTableWidgetItem("Ignored")
                    self.desc_parse_table.setItem(row, 11, ignore_item)
                else:
                    ignore_item.setText("Ignored")
                corr_item = self.desc_parse_table.item(row, 10)
                if corr_item:
                    corr_item.setText("")
                from PySide6.QtGui import QBrush, QColor
                ignore_item.setBackground(QBrush(QColor("#C8E6C9")))
                for c in range(self.desc_parse_table.columnCount()):
                    it = self.desc_parse_table.item(row, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
                # FWC-only: store Ignored in unified rows, do not mutate .fwk
                try:
                    for ur in getattr(self, "_desc_parse_all_rows", []):
                        if ur[3] == str(oid):
                            ur[7] = "Ignored"
                            ur[8] = f"Ignored — {flags}"
                            break
                    self.check_dirty = True
                    self._update_check_title()
                except:
                    pass
                try:
                    self._autosave_fwc()
                except Exception:
                    pass
                idx += 1
                if idx >= len(session_oids):
                    break
                continue
            elif action in ("fix", "key_token", "key_entire") and new_desc is not None:
                corr_item = self.desc_parse_table.item(row, 10)
                if corr_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    corr_item = QTableWidgetItem(new_desc)
                    self.desc_parse_table.setItem(row, 10, corr_item)
                else:
                    corr_item.setText(new_desc)
                from PySide6.QtGui import QBrush, QColor
                corr_item.setBackground(QBrush(QColor("#C8E6C9")))
                for c in range(self.desc_parse_table.columnCount()):
                    it = self.desc_parse_table.item(row, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
                ignore_item = self.desc_parse_table.item(row, 11)
                if ignore_item:
                    ignore_item.setText("")
                # FWC-only: do not mutate .fwk
                try:
                    for ur in getattr(self, "_desc_parse_all_rows", []):
                        if ur[3] == str(oid):
                            ur[7] = "Corrected"
                            ur[8] = new_desc
                            break
                    self.check_dirty = True
                    self._update_check_title()
                except:
                    pass
                try:
                    self._autosave_fwc()
                except Exception:
                    pass
                idx += 1
                if idx >= len(session_oids):
                    break
                continue
            self._update_desc_parse_hint()
            try:
                self._update_renumber_lock()
            except:
                pass
            idx += 1
        # Restore sorting
        try:
            if prev_sort_first:
                self.desc_parse_table.setSortingEnabled(True)
        except Exception:
            pass

    def _clean_auto(self):
        """Clean Auto — REPLACES Clean Selected: batch fix only rows with Auto Fix guesses, checkbox Include, Select All/Clear All, excludes already Corrected/Ignored/Removed."""
        if self.desc_parse_table is None or self.desc_parse_table.rowCount()==0:
            QMessageBox.information(self, "Clean Auto", "No Flagged Descriptions — Run Checks First")
            return
        # Build candidate list: flagged rows that have autofix and are not already handled (no Correction/Ignore/Removed)
        f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
        candidates = []
        # Determine source rows: if user has highlighted rows, use highlighted subset; otherwise use all visible flagged
        sel = self.desc_parse_table.selectionModel().selectedRows()
        source_rows = sorted([s.row() for s in sel]) if sel else list(range(self.desc_parse_table.rowCount()))
        # Also respect filter: skip hidden rows if filter active
        from .clean import _get_autofix_for_desc
        for r in source_rows:
            # Skip hidden (filtered out) unless it was explicitly selected
            if not sel and self.desc_parse_table.isRowHidden(r):
                continue
            try:
                oid = self.desc_parse_table.item(r, 0).text().strip() if self.desc_parse_table.item(r,0) else ""
                pt = self.desc_parse_table.item(r, 1).text().strip() if self.desc_parse_table.item(r,1) else ""
                raw = self.desc_parse_table.item(r, 5).text().strip() if self.desc_parse_table.item(r,5) else ""
                flags = self.desc_parse_table.item(r, 8).text().strip() if self.desc_parse_table.item(r,8) else ""
                detail = self.desc_parse_table.item(r, 9).text().strip() if self.desc_parse_table.item(r,9) else ""
                corr = self.desc_parse_table.item(r, 10).text().strip() if self.desc_parse_table.item(r,10) else ""
                ign = self.desc_parse_table.item(r, 11).text().strip() if self.desc_parse_table.item(r,11) else ""
                if not flags:
                    continue
                # Must not pull any that already have corrections
                if corr or ign in ("Ignored","Removed"):
                    continue
                # Must have autofix guess
                auto_fix = _get_autofix_for_desc(raw, flags, detail, f2f_set, fieldbook_path=self.fieldbook_path)
                if not auto_fix:
                    continue
                candidates.append({"oid": oid, "pt_num": pt, "raw_desc": raw, "flags": flags, "flag_detail": detail, "auto_fix": auto_fix, "row": r})
            except Exception:
                continue
        if not candidates:
            # No autofix candidates in selection/all — inform with guidance
            if sel:
                QMessageBox.information(self, "Clean Auto", f"No selected rows have Auto Fix guesses and no existing Correction. Highlighted {len(source_rows)} rows — none are autofix-able (or already Corrected/Ignored). Use double-click to handle manually.")
            else:
                QMessageBox.information(self, "Clean Auto", "No flagged rows have Auto Fix guesses available (or all already Corrected/Ignored). Double-click individual rows to clean manually.")
            return
        from .clean import BatchCleanDialog
        dlg = BatchCleanDialog(candidates, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.result_selected  # list of (oid, auto_fix)
        if not selected:
            return
        # Apply Auto Fix to selected OIDs — update Desc table Correction only (fwc-only), plus colors
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtGui import QBrush, QColor
        applied = 0
        for oid2, new_desc in selected:
            # Find row in desc_parse_table
            for r in range(self.desc_parse_table.rowCount()):
                it_oid = self.desc_parse_table.item(r, 0)
                if it_oid and it_oid.text().strip() == str(oid2):
                    corr_it = self.desc_parse_table.item(r, 10)
                    if corr_it is None:
                        corr_it = QTableWidgetItem(new_desc)
                        self.desc_parse_table.setItem(r, 10, corr_it)
                    else:
                        corr_it.setText(new_desc)
                    corr_it.setBackground(QBrush(QColor("#C8E6C9")))
                    # Green row for handled
                    for c in range(self.desc_parse_table.columnCount()):
                        itc = self.desc_parse_table.item(r, c)
                        if itc:
                            itc.setBackground(QBrush(QColor("#E8F5E9")))
                    # Ensure Ignore cleared
                    ign_it = self.desc_parse_table.item(r, 11)
                    if ign_it:
                        ign_it.setText("")
                        ign_it.setBackground(QBrush(QColor("#FFFFFF")))
                    # FWC-only: do not mutate .fwk; keep raw Desc in edit table
                    # Store correction only in unified rows Comments
                    try:
                        for ur in getattr(self, "_desc_parse_all_rows", []):
                            if ur[3] == str(oid2):
                                ur[7] = "Corrected"
                                ur[8] = new_desc
                                break
                    except:
                        pass
                    applied += 1
                    break
        if applied:
            self.check_dirty = True
            self._update_check_title()
            self._update_desc_parse_hint()
            try:
                self._update_renumber_lock()
            except Exception:
                pass
            # FWC-only: mark check report dirty and autosave to FWC immediately
            try:
                self.check_dirty = True
                self._update_check_title()
                self._autosave_fwc()
            except Exception as e:
                print(f"fwc batch autosave failed: {e}")
            self.summary_label.setText(f"Clean Auto: {applied} of {len(candidates)} autofix candidates applied (checked) — saved to .fwc")
        else:
            self.summary_label.setText("Clean Auto: no rows applied")

    def _clean_selected(self):
        """Alias — Clean Selected now replaced by Clean Auto."""
        return self._clean_auto()

    def _update_renumber_lock(self):
        """Enable renumber only when Description + Duplicate are clean (no flagged rows not handled)."""
        # Renumber pulled from Duplicate tab — keep hidden disabled, lock logic only for desc tab renumber if exists
        try:
            if hasattr(self, 'dup_renumber_btn'):
                self.dup_renumber_btn.setVisible(False)
                self.dup_renumber_btn.setEnabled(False)
        except:
            pass
        if not hasattr(self, 'renumber_btn') or self.desc_parse_table is None:
            return
        # Count rows where Ignore != Ignored and Correction is blank (still flagged and not handled)
        remaining = 0
        for r in range(self.desc_parse_table.rowCount()):
            ignore_it = self.desc_parse_table.item(r, 11)
            corr_it = self.desc_parse_table.item(r, 10)
            is_ignored = ignore_it and ignore_it.text().strip() == "Ignored"
            is_removed = ignore_it and ignore_it.text().strip() == "Removed"
            has_corr = corr_it and corr_it.text().strip() != ""
            if not is_ignored and not is_removed and not has_corr:
                # Also check if row is actually flagged (Flags col 8 not empty)
                flags_it = self.desc_parse_table.item(r, 8)
                if flags_it and flags_it.text().strip():
                    remaining += 1
        clean = (remaining == 0)
        # Fallback to old logic if no Correction/Ignore cols yet
        if self.desc_parse_table.columnCount() < 12:
            clean = is_descriptions_clean(self.desc_parse_table.rowCount())
            remaining = self.desc_parse_table.rowCount()
        self.renumber_btn.setEnabled(clean)
        self.renumber_btn.setText("Renumber" if clean else "Renumber (locked)")
        if clean:
            self.renumber_btn.setToolTip(f"Ready — crew {DEFAULT_CREW_NUMBER} blocks {crew_blocks(DEFAULT_CREW_NUMBER)[:2]}... + Control {CONTROL_RANGE}, Boundary {BOUNDARY_RANGE}")
        else:
            self.renumber_btn.setToolTip(f"Locked — {remaining} flagged descriptions remain (not Corrected/Ignored). Double-click each to Fix/Skip before renumbering (keeps line order).")

    def _on_renumber(self):
        """Renumber placeholder — locked behind cleaned descriptions. Preserves line order (OID order within each line)."""
        if self.desc_parse_table is None or self.desc_parse_table.rowCount() > 0:
            QMessageBox.warning(self, "Renumber", "Descriptions not clean — double-click each flagged row in Description Error and Fix/Skip them first. Renumbering moves points in line order, so broken codes would corrupt lines.")
            return
        crew = DEFAULT_CREW_NUMBER
        blocks = crew_blocks(crew)
        # TODO: preserve Field Book line/curve command order alongside crew blocks and type ranges.
        QMessageBox.information(self, "Renumber", f"Renumber ready for crew {crew} — blocks {blocks[:3]}... + Control {CONTROL_RANGE}, Boundary {BOUNDARY_RANGE}, General {GENERAL_START}+\n\nAll descriptions clean. Next: implement renumber that walks OIDs in line order (not numeric sort) and assigns next available in crew's blocks by type.\n\nFor now, this is a placeholder — no numbers changed. Tell me the exact renumber rule for Control vs Boundary vs General within crew blocks and I'll wire it.")

    def _run_all_checks(self):
        """New Run Checks: starts completely over like never ran before — single .fwc unified OID-minimal, DisplayTab routed, Description flagged-only, live-pull raw via OID."""
        # --- New Run Checks: completely fresh — clear any previous checks like never ran before ---
        # Must not load any previous .fwc/.chk; do not merge. Clear tables and state first.
        try:
            # Remember old path for default dir but clear state so fresh=True has no pull
            _old_check_path = getattr(self, 'check_report_path', '')
            self._check_loading = True
            # Clear both check tables and their stored rows
            if hasattr(self, '_check_all_rows'):
                self._check_all_rows = []
            if hasattr(self, '_desc_parse_all_rows'):
                self._desc_parse_all_rows = []
            if self.check_table is not None:
                self.check_table.setRowCount(0)
            if self.desc_parse_table is not None:
                self.desc_parse_table.setRowCount(0)
            self.check_report_path = ""
            self.check_dirty = False
            self._update_check_title()
            self._update_desc_parse_title()
            self._update_check_hint()
            self._update_check_path_label()
            self._update_desc_parse_hint()
            try:
                self._update_renumber_lock()
            except Exception:
                pass
            self._check_loading = False
        except Exception:
            pass
        # Gather working rows sorted by OID (canonical)
        working_rows = None
        if self.edit_table is not None and self.edit_table.rowCount() > 0:
            working_rows = [self._row_texts(self.edit_table, r) for r in range(self.edit_table.rowCount())]
            try:
                working_rows.sort(key=lambda r: int(r[0]) if str(r[0]).isdigit() else r[0])
            except Exception:
                pass
        elif self.edit_file_path and Path(self.edit_file_path).exists():
            working_rows = self._read_working_file(self.edit_file_path)
        else:
            QMessageBox.warning(self, "Consolidated Field Data Required",
                                "No consolidated data loaded — create Consolidated Field Data from Raw Field Data first.\n"
                                "Select a Field Data Folder on the Raw Field Data tab, then click Create Consolidated Field Data.")
            return
        # Field Book required — must be selected before saving checks (per user: don't save without fieldbook)
        if not self.fieldbook_path or not Path(self.fieldbook_path).exists():
            # Prompt to pick existing fieldbook (like legacy _on_check_file) — if user cancels, don't save
            reply = QMessageBox.question(
                self, "Field Book Required",
                f"No Field Book ({FIELDBOOK_EXT}) selected.\n"
                f"Pick an existing field book now? (You can also use Tools > Convert Field Book to create one)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if reply == QMessageBox.StandardButton.Yes:
                pick, _ = QFileDialog.getOpenFileName(
                    self, "Select Field Book",
                    self.project_path or "",
                    f"Field Book (*{FIELDBOOK_EXT});;All Files (*)")
                if pick:
                    self.fieldbook_path = pick
                    self._refresh_fieldbook_tab()
                    self._mark_dirty()
                else:
                    return
            else:
                return
            # Re-check after pick
            if not self.fieldbook_path or not Path(self.fieldbook_path).exists():
                QMessageBox.warning(self, "Field Book Required",
                                    f"No Field Book ({FIELDBOOK_EXT}) selected — convert or load a Field Book before running checks.")
                return
        f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
        # Cache command_set and correction rules once (avoid per-row file reads — was 2*3773 reads)
        try:
            from .config import get_command_set, get_correction_rules
            command_set = get_command_set(self.fieldbook_path)
            rules = get_correction_rules(self.fieldbook_path)
        except Exception:
            command_set = None
            rules = None
        # Detectors (Duplicate)
        exact = find_exact_duplicate_groups(working_rows)
        similar = find_similar_number_groups(working_rows)
        close = find_close_ne_groups(working_rows, tol=NE_TOLERANCE)
        # Description parsing — flagged-only, with line-order protection
        desc_flagged = []
        if f2f_set:
            # Internal flag rules — Check Settings hidden, all protections always on (false flags Skipped)
            settings = {
                "protect_st_pc": True,
                "protect_pt_end": True,
                "protect_pt_x": True,
                "protect_pc_after_end": True,
                "protect_st_after_end": True,
                "protect_orphan": True,
            }
            line_flags_by_oid = _validate_line_command_order(working_rows, f2f_set, settings, fieldbook_path=self.fieldbook_path, command_set=command_set, rules=rules)
            line_details_by_oid = getattr(_validate_line_command_order, 'details', {})
            for wr in working_rows:
                oid = wr[0] if len(wr)>0 else ""
                raw_desc = wr[5] if len(wr)>5 else ""
                parsed = parse_desc_field(raw_desc, f2f_set, fieldbook_path=self.fieldbook_path, command_set=command_set, rules=rules)
                flags = parsed["flags"][:]
                details = [parsed["flag_detail"]] if parsed["flag_detail"] else []
                extra = line_flags_by_oid.get(oid, [])
                extra_details = line_details_by_oid.get(oid, [])
                for ef in extra:
                    flags.append(ef)
                for ed in extra_details:
                    details.append(ed)
                # Also handle case where extra was old style with detail embedded (fallback)
                if not extra_details and extra and any(" " in ef for ef in extra):
                    # Old code may have detail in flag, ensure detail gets it
                    pass
                if not flags:
                    continue  # flagged-only
                flags_str = "; ".join(flags)
                detail_str = "; ".join([d for d in details if d])
                # IssueType = first flag type before colon or Multiple
                first_type = flags[0].split(":")[0] if flags else "DescFlag"
                if len(flags) > 1 and len({f.split(":")[0] for f in flags}) > 1:
                    # If multiple distinct types, mark Multiple but keep first as primary
                    issue = "Multiple"
                else:
                    issue = first_type
                desc_flagged.append({"oid": oid, "flags_str": flags_str, "flag_detail": detail_str, "issue_type": issue})
        else:
            # No F2F — warn once but still allow duplicate report
            QMessageBox.warning(self, "Parse Descriptions", "No Field Book (.fwb) loaded — open or convert a Carlson F2F CSV via Tools > Convert first. Duplicate checks will still run.")
        # Line issues — the ones no single description shows — go in the same report, so the
        # linework half of the cleaning is carried by the same file as everything else.
        line_issues = []
        try:
            line_issues = detect_line_issues(working_rows, fieldbook_path=getattr(self, "fieldbook_path", None))
        except Exception as ex:
            print(f"line check failed during Run Checks: {ex}")
        line_statuses = {}
        try:
            if getattr(self, "check_report_path", None) and Path(self.check_report_path).exists():
                _h, _old_rows = read_unified_report(Path(self.check_report_path))
                line_statuses = line_statuses_from_rows(_old_rows)
        except Exception:
            line_statuses = {}
        # Build unified rows (OID-minimal)
        unified_rows = build_unified_report_rows(working_rows, exact, similar, close, desc_flagged,
                                                 line_issues=line_issues, line_statuses=line_statuses)
        # If nothing flagged, still inform but create empty file? Write empty unified
        if not unified_rows:
            QMessageBox.information(self, "Run Checks", "No issues found — no exact duplicates, no similar numbers, no close NE ≤0.1, no flagged descriptions.")
            # Still write empty to allow tab opening with zero? We'll still prompt save
        # Prompt save location (default project folder or previous report dir) — New Run Checks is fresh so default to project/edit dir, not old report
        try:
            old_dir = str(Path(_old_check_path).parent) if '_old_check_path' in locals() and _old_check_path else ""
        except Exception:
            old_dir = ""
        default_dir = self.project_path if self.project_path and Path(self.project_path).exists() else (str(Path(self.edit_file_path).parent) if self.edit_file_path else (old_dir if old_dir and Path(old_dir).exists() else ""))
        default_name = str(Path(default_dir) / f"check_report{CHECK_REPORT_EXT}") if default_dir else f"check_report{CHECK_REPORT_EXT}"
        path, _ = QFileDialog.getSaveFileName(self, f"Save Check Report ({CHECK_REPORT_EXT})", default_name, f"Check Report (*{CHECK_REPORT_EXT});;All Files (*)")
        if not path:
            return
        if Path(path).suffix.lower() not in (CHECK_REPORT_EXT, LEGACY_CHECK_EXT):
            path += CHECK_REPORT_EXT
        if not write_unified_report(Path(path), unified_rows):
            QMessageBox.critical(self, "Run Checks", f"Failed to write {path}")
            return
        self.check_report_path = path
        self._check_loading = True
        # Fill both tabs from unified rows (live-pull raw)
        self._fill_check_table_unified(unified_rows, working_rows)
        self._fill_desc_parse_table_unified(unified_rows, working_rows, fresh=True)
        self._check_loading = False
        self.check_dirty = False
        self._mark_dirty()
        # Ensure both tabs visible (hidden until ran)
        self._show_tab(self.check_tab, "Duplicate Error")
        self._show_tab(self.desc_parse_tab, "Description Error")
        self._update_check_title()
        self._update_desc_parse_title()
        dup_count = sum(1 for r in unified_rows if r[1]=="Duplicate")
        line_cnt = len(getattr(self, "_line_all_rows", []) or [])
        desc_count = sum(1 for r in unified_rows if r[1]=="Description")
        # Also auto-refresh line repair tab on run checks
        try:
            self._refresh_line_repair()
        except: pass
        line_count = sum(1 for r in unified_rows if r[1] == "Line")
        self.summary_label.setText(f"Checks: {len(exact)} exact, {len(similar)} similar, {len(close)} close-NE, {len(desc_flagged)} flagged desc, {len(line_issues)} line → {len(unified_rows)} rows ({dup_count} dup, {desc_count} desc, {line_count} line) → {os.path.basename(path)} (Line tab refreshed)")
        try:
            self._update_renumber_lock()
        except Exception:
            pass
        try:
            self._update_view_menu_state()
        except Exception:
            pass
        

    def _run_desc_parse(self):
        """Parse consolidated file Description field vs F2F — flagged-only, OID-minimal, DisplayTab=Description. Standalone still writes unified (or filters existing .fwc/.chk)."""
        # Need working rows and F2F
        working_rows = None
        if self.edit_table is not None and self.edit_table.rowCount() > 0:
            working_rows = [self._row_texts(self.edit_table, r) for r in range(self.edit_table.rowCount())]
            # Sort by OID numeric to ensure line order validation sees canonical order, not display sort
            try:
                working_rows.sort(key=lambda r: int(r[0]) if str(r[0]).isdigit() else r[0])
            except Exception:
                pass
        elif self.edit_file_path and __import__('pathlib').Path(self.edit_file_path).exists():
            working_rows = self._read_working_file(self.edit_file_path)
        else:
            QMessageBox.warning(self, "Parse Descriptions", "No consolidated data (.fwk) loaded — create from Raw Field Data first.")
            return
        f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
        if not f2f_set:
            QMessageBox.warning(self, "Parse Descriptions", "No Field Book (.fwb) loaded — open or convert a Carlson F2F CSV via Tools > Convert first.")
            return
        try:
            from .config import get_command_set, get_correction_rules
            command_set = get_command_set(self.fieldbook_path)
            rules = get_correction_rules(self.fieldbook_path)
        except Exception:
            command_set = None
            rules = None
        # Line order validation across points (per line id) — internal rules, tab hidden
        settings = {
            "protect_st_pc": True,
            "protect_pt_end": True,
            "protect_pt_x": True,
            "protect_pc_after_end": True,
            "protect_st_after_end": True,
            "protect_orphan": True,
        }
        line_flags_by_oid = _validate_line_command_order(working_rows, f2f_set, settings, fieldbook_path=self.fieldbook_path, command_set=command_set, rules=rules)
        line_details_by_oid = getattr(_validate_line_command_order, 'details', {})

        rows_for_table = []
        flagged = 0
        for wr in working_rows:
            oid = wr[0] if len(wr) > 0 else ""
            pt = wr[1] if len(wr) > 1 else ""
            n = wr[2] if len(wr) > 2 else ""
            e = wr[3] if len(wr) > 3 else ""
            z = wr[4] if len(wr) > 4 else ""
            raw_desc = wr[5] if len(wr) > 5 else ""
            parsed = parse_desc_field(raw_desc, f2f_set, fieldbook_path=self.fieldbook_path, command_set=command_set, rules=rules)
            flags = parsed["flags"][:]
            details = [parsed["flag_detail"]] if parsed["flag_detail"] else []
            # Merge line order flags for this OID (Flags short, Details descriptive)
            extra = line_flags_by_oid.get(oid, [])
            extra_details = line_details_by_oid.get(oid, [])
            for ef in extra:
                flags.append(ef)
            for ed in extra_details:
                details.append(ed)
            flags_str = "; ".join(flags) if flags else ""
            detail_str = "; ".join([d for d in details if d])
            if flags_str:
                flagged += 1
            rows_for_table.append([
                oid, pt, n, e, z,
                raw_desc,
                parsed["parsed_code_str"],
                parsed["free_desc"],
                flags_str,
                detail_str
            ])
        # Flagged-only filtering already done; rows_for_table currently holds all with flags_str maybe empty — filter to flagged only for display
        flagged_rows = [r for r in rows_for_table if r[8]]  # Flags col
        # For unified standalone, also write to .chk if no existing unified file — merge or create
        # Build desc_flagged for unified
        desc_flagged = []
        line_issues = []
        for r in flagged_rows:
            # r is [OID, PtNum, N, E, Z, RawDesc, ParsedCode, FreeDesc, Flags, FlagDetail] — but we need to convert to minimal
            # Instead rebuild from flagged info: we have flags in r[8], detail in r[9]
            flags_str = r[8]
            detail = r[9]
            first_type = flags_str.split(";")[0].split(":")[0].strip() if flags_str else "DescFlag"
            issue = first_type
            if len(flags_str.split(";")) > 1 and len({f.split(":")[0].strip() for f in flags_str.split(";")}) > 1:
                issue = "Multiple"
            desc_flagged.append({"oid": r[0], "flags_str": flags_str, "flag_detail": detail, "issue_type": issue})
        # If we have an existing unified file, merge desc part; else create new unified with only desc
        if self.check_report_path and Path(self.check_report_path).exists():
            # Load existing, keep Duplicate rows, replace Description rows
            _, existing = read_unified_report(Path(self.check_report_path))
            dup_rows = [r for r in (existing or []) if r[1]=="Duplicate"]
            line_statuses = line_statuses_from_rows(existing or [])
            # Rebuild GroupIDs sequential
            unified_rows = []
            gid = 1
            for r in dup_rows:
                r2 = r[:]
                r2[0] = str(gid)
                unified_rows.append(r2)
                gid += 1
            for item in desc_flagged:
                unified_rows.append([str(gid), "Description", item["issue_type"], item["oid"], item["flags_str"], item["flags_str"], item["flag_detail"], "Open", ""])
                gid += 1
            line_issues = []
            try:
                line_issues = detect_line_issues(working_rows, fieldbook_path=getattr(self, "fieldbook_path", None))
            except Exception as ex:
                print(f"line check failed during Parse Descriptions: {ex}")
            unified_rows.extend(line_report_rows(line_issues, line_statuses, start_gid=gid))
            write_unified_report(Path(self.check_report_path), unified_rows)
            self._fill_desc_parse_table_unified(unified_rows, working_rows)
            # Also refresh duplicate tab to keep dup rows consistent
            self._fill_check_table_unified(unified_rows, working_rows)
        else:
            # No existing — just fill desc table with flagged only (legacy display, not unified file)
            self._fill_desc_parse_table(flagged_rows)
        self._show_tab(self.desc_parse_tab, "Description Error")
        self.summary_label.setText(f"Parsed {len(working_rows)} descriptions vs F2F ({len(f2f_set)} codes): {len(flagged_rows)} flagged"
                                   + (f", {len(line_issues)} line issue(s)" if line_issues else ""))

    def _working_row_by_oid(self, oid):
        """Live-pull raw 8-col row by OID from Edit table or file (OID-minimal join). Returns list or None."""
        oid_s = str(oid).strip()
        # Search edit_table first (live)
        if self.edit_table is not None and self.edit_table.rowCount() > 0:
            for r in range(self.edit_table.rowCount()):
                it = self.edit_table.item(r, 0)
                if it and it.text().strip() == oid_s:
                    return self._row_texts(self.edit_table, r)
        # Fallback to file if table not loaded but path exists
        if self.edit_file_path and Path(self.edit_file_path).exists():
            try:
                rows = self._read_working_file(self.edit_file_path)
                for rw in rows or []:
                    if str(rw[0]).strip() == oid_s:
                        return rw
            except Exception:
                pass
        return None

    def _refresh_desc_parse_tab(self):
        # Clear if no consolidated file or F2F changed; no auto-reparse to keep flag-only
        if self.desc_parse_table is None:
            return
        self._update_desc_parse_path_label()
        # Keep existing rows but update hint

    def _update_check_title(self):
        if self.check_tab is None:
            return
        idx = self.tabs.indexOf(self.check_tab)
        if idx == -1:
            return
        star = " *" if getattr(self, "check_dirty", False) else ""
        self.tabs.setTabText(idx, f"Duplicate Error{star}")

    def _update_check_hint(self):
        if self.check_table is None or self.check_hint_label is None:
            return
        # hint visible only when no rows at all (not just filtered)
        # We track unfiltered row count via stored data; for now use table row count vs filtered
        # Simpler: if table has zero rows -> show hint
        self.check_hint_label.setVisible(self.check_table.rowCount() == 0)

    def _update_check_path_label(self):
        if self.check_path_label is None:
            return
        if not self.check_report_path or not Path(self.check_report_path).exists():
            self.check_path_label.setText(
                f"No duplicate report loaded — click Run Checks to generate {CHECK_REPORT_EXT} from the consolidated file, or Open an existing report.")
        else:
            total = getattr(self, "_check_all_rows", [])
            n = len(total)
            self.check_path_label.setText(
                f"Duplicate Error: {os.path.basename(self.check_report_path)} ({n} rows) — {self.check_report_path}")

    def _fill_check_table(self, headers, rows):
        """Fill from headered .chk rows — with grouped visual stacking."""
        from PySide6.QtGui import QBrush, QColor, QFont
        self.check_table.blockSignals(True)
        self.check_table.setSortingEnabled(False)
        self.check_table.setRowCount(0)
        if headers and len(headers) == len(CHECK_REPORT_HEADERS):
            self.check_table.setHorizontalHeaderLabels(headers)
        # Keep unfiltered copy for filtering
        self._check_all_rows = rows[:] if rows else []
        # Track group boundaries for styling (GroupID col 0)
        prev_gid = None
        for r in rows:
            row_idx = self.check_table.rowCount()
            self.check_table.insertRow(row_idx)
            gid = r[0] if len(r) > 0 else ""
            issue = r[1] if len(r) > 1 else ""
            is_first_in_group = (gid != prev_gid)
            if is_first_in_group:
                prev_gid = gid
            for c, val in enumerate(r):
                display_val = str(val)
                # Stack Similar/Close under first instance: tab-out PointNumber for non-first rows
                if c == 3 and not is_first_in_group:
                    # Indent point number to show stacking under first instance
                    display_val = "    ↳ " + display_val
                # OID col 2 numeric sort, GroupID col 0 numeric
                if c in (0, 2):
                    item = NumericSortItem(val)
                    item.setText(str(val) if not (c == 0 and not is_first_in_group) else str(val))
                    # Keep GroupID visible for all, but style differs
                elif c == 3:
                    item = NaturalSortItem(display_val, letters_first=True)
                else:
                    item = QTableWidgetItem(display_val)
                # Status (11) and Comments (12) editable, else read-only
                if c in (11, 12):
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # Bold/color headers: first row of each GroupID gets bold + colored background
                if is_first_in_group:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    # Light blue header for Exact (soft blue), light amber for Similar, light green for CloseNE
                    if issue == "ExactDuplicate":
                        bg = QColor("#E3F2FD")  # blue 50
                    elif issue == "SimilarNumber":
                        bg = QColor("#FFF8E1")  # amber 50
                    elif issue == "CloseNE":
                        bg = QColor("#E8F5E9")  # green 50
                    else:
                        bg = QColor("#F5F5F5")
                    item.setBackground(QBrush(bg))
                else:
                    # Non-first rows: subtle alternating but slightly muted for stacking
                    if issue == "SimilarNumber":
                        item.setBackground(QBrush(QColor("#FFFDE7")))  # very light amber
                    elif issue == "CloseNE":
                        item.setBackground(QBrush(QColor("#F1F8E9")))
                self.check_table.setItem(row_idx, c, item)
        self.check_table.resizeColumnsToContents()
        self.check_table.setSortingEnabled(True)
        self.check_table.blockSignals(False)
        self._update_check_hint()
        self._update_check_path_label()
        self._apply_check_filter()

    def _apply_check_filter(self, _text=None):
        """Show/hide rows by IssueType filter."""
        if self.check_table is None or self.check_filter_combo is None:
            return
        filt = self.check_filter_combo.currentText()
        # Column 1 = IssueType
        for r in range(self.check_table.rowCount()):
            item = self.check_table.item(r, 1)
            show = (filt == "All") or (item and item.text() == filt)
            self.check_table.setRowHidden(r, not show)

    def _on_desc_correction_edited(self, item):
        """Handle direct edit of Correction (Auto Fix) column — makes it editable and persists to FWC.
        When user types a correction directly in the table, update the underlying unified row and mark dirty.
        """
        if item is None or getattr(self, "_check_loading", False):
            return
        # Note: visual column 6 is logical 10 (Correction) after moveSection, but item.column() returns logical index
        if item.column() == 10:
            try:
                row = item.row()
                oid_item = self.desc_parse_table.item(row, 0)
                if not oid_item:
                    return
                oid = oid_item.text().strip()
                new_corr = item.text().strip()
                # Update in-memory unified
                for ur in getattr(self, "_desc_parse_all_rows", []) or []:
                    if str(ur[3]).strip() == oid:
                        if new_corr:
                            ur[7] = "Corrected"
                            ur[8] = new_corr
                        else:
                            # Cleared correction -> revert to Open if not Ignored/Removed
                            ign_item = self.desc_parse_table.item(row, 11)
                            ign = ign_item.text().strip() if ign_item else ""
                            if ign not in ("Ignored", "Removed"):
                                ur[7] = "Open"
                                ur[8] = ""
                        break
                self.check_dirty = True
                self._update_check_title()
                # Update row color
                from PySide6.QtGui import QBrush, QColor
                is_removed = False
                is_ignored = False
                try:
                    ign_item = self.desc_parse_table.item(row, 11)
                    is_removed = ign_item and ign_item.text().strip() == "Removed"
                    is_ignored = ign_item and ign_item.text().strip() == "Ignored"
                except Exception:
                    pass
                has_corr = bool(new_corr)
                # Update row background similarly to _fill logic
                for c in range(self.desc_parse_table.columnCount()):
                    it = self.desc_parse_table.item(row, c)
                    if not it:
                        continue
                    if c == 10:
                        if has_corr:
                            it.setBackground(QBrush(QColor("#C8E6C9")))
                        elif is_removed or is_ignored:
                            it.setBackground(QBrush(QColor("#FFFFFF")))
                        else:
                            it.setBackground(QBrush(QColor("#F5F5F5")))
                    else:
                        if is_removed:
                            it.setBackground(QBrush(QColor("#FFEBEE")) if c != 11 else QBrush(QColor("#FFCDD2")))
                        elif is_ignored or has_corr:
                            it.setBackground(QBrush(QColor("#E8F5E9")) if c not in (10,11) else it.background())
                        elif c not in (10,11):
                            # Check if flagged
                            flags_it = self.desc_parse_table.item(row, 8)
                            if flags_it and flags_it.text().strip():
                                it.setBackground(QBrush(QColor("#FFCDD2")))
                try:
                    self._autosave_fwc()
                except Exception:
                    pass
                try:
                    self._update_renumber_lock()
                except Exception:
                    pass
            except Exception as e:
                print(f"_on_desc_correction_edited failed: {e}")

    def _on_check_item_changed(self, item):
        """Mark dirty when Status/Comments edited — autosave to FWC so it is truth file."""
        if item is None:
            return
        if item.column() in (11, 12):
            if not getattr(self, "_check_loading", False):
                self.check_dirty = True
                self._update_check_title()
                try:
                    self._autosave_fwc()
                except Exception:
                    pass

    def _on_duplicate_double_clicked(self, row, col):
        """Open DuplicateMergeDialog for the GroupID of the clicked row — Primary/Merge/Ignore/Remove per OID, only 1 Primary, approve/deny, merge descs with separators."""
        if self.check_table is None or row < 0 or row >= self.check_table.rowCount():
            return
        # Get GroupID from column 0
        gid_item = self.check_table.item(row, 0)
        if not gid_item:
            return
        gid = gid_item.text().strip()
        if not gid:
            return
        # Collect all rows with same GroupID from underlying unified rows (_check_all_rows) for status/comments, and live pull for N/E/Z/Desc
        # Use _check_all_rows (unified minimal) as source of truth for group
        group_unified = [r for r in getattr(self, "_check_all_rows", []) if str(r[0]).strip() == str(gid)]
        if not group_unified:
            # Fallback: collect from table display
            group_unified = []
            for rr in range(self.check_table.rowCount()):
                it = self.check_table.item(rr, 0)
                if it and it.text().strip() == str(gid):
                    # Build unified-like row from table display cols 0,1,2,10,11,12
                    issue = self.check_table.item(rr, 1).text() if self.check_table.item(rr, 1) else ""
                    oid = self.check_table.item(rr, 2).text() if self.check_table.item(rr, 2) else ""
                    detail = self.check_table.item(rr, 10).text() if self.check_table.item(rr, 10) else ""
                    status = self.check_table.item(rr, 11).text() if self.check_table.item(rr, 11) else "Open"
                    comments = self.check_table.item(rr, 12).text() if self.check_table.item(rr, 12) else ""
                    group_unified.append([gid, "Duplicate", issue, oid, "", detail, "", status, comments])
        if not group_unified:
            return
        # Determine issue_type for title (take first)
        issue_type = group_unified[0][2] if len(group_unified[0]) > 2 else "Duplicate"
        # Build oid_map for live pull
        oid_map = {}
        try:
            if self.edit_table and self.edit_table.rowCount() > 0:
                for rr in range(self.edit_table.rowCount()):
                    oid_it = self.edit_table.item(rr, 0)
                    if oid_it:
                        o = oid_it.text().strip()
                        # Pull full row texts via _row_texts if available, else manually
                        try:
                            row_texts = self._row_texts(self.edit_table, rr)
                        except:
                            row_texts = [self.edit_table.item(rr, c).text() if self.edit_table.item(rr, c) else "" for c in range(self.edit_table.columnCount())]
                        oid_map[o] = row_texts
            elif self.edit_file_path:
                try:
                    from pathlib import Path
                    wr = self._read_working_file(self.edit_file_path)
                    for r in wr or []:
                        oid_map[str(r[0])] = r
                except:
                    pass
        except:
            pass
        # Build rows for dialog: OID, PtNum, N,E,Z, Desc, Detail, Status, Comments
        dialog_rows = []
        for ur in group_unified:
            # ur: [GroupID, DisplayTab, IssueType, OID, Flags, Detail, FlagDetail, Status, Comments]
            oid = str(ur[3]).strip() if len(ur) > 3 else ""
            detail = ur[5] if len(ur) > 5 else ""
            status = ur[7] if len(ur) > 7 else "Open"
            comments = ur[8] if len(ur) > 8 else ""
            raw = oid_map.get(oid)
            if raw and len(raw) >= 6:
                # raw is [OID, PtNum, N, E, Z, Desc, ...] (maybe 8 cols)
                pt = raw[1] if len(raw) > 1 else ""
                n = raw[2] if len(raw) > 2 else ""
                e = raw[3] if len(raw) > 3 else ""
                z = raw[4] if len(raw) > 4 else ""
                desc = raw[5] if len(raw) > 5 else ""
            else:
                # Fallback to table display
                found = None
                for rr in range(self.check_table.rowCount()):
                    it = self.check_table.item(rr, 2)
                    if it and it.text().strip() == oid:
                        found = rr
                        break
                if found is not None:
                    pt = self.check_table.item(found, 3).text() if self.check_table.item(found, 3) else ""
                    n = self.check_table.item(found, 4).text() if self.check_table.item(found, 4) else ""
                    e = self.check_table.item(found, 5).text() if self.check_table.item(found, 5) else ""
                    z = self.check_table.item(found, 6).text() if self.check_table.item(found, 6) else ""
                    desc = self.check_table.item(found, 7).text() if self.check_table.item(found, 7) else ""
                else:
                    pt=n=e=z=desc=""
            dialog_rows.append({"oid": oid, "pt_num": pt, "n": n, "e": e, "z": z, "desc": desc, "detail": detail, "status": status, "comments": comments, "raw_row": raw})
        # Sort by OID for stable dialog (numeric)
        try:
            dialog_rows.sort(key=lambda d: int(d["oid"]) if str(d["oid"]).isdigit() else str(d["oid"]))
        except:
            pass
        # Need f2f_set for merging
        try:
            from .clean import DuplicateMergeDialog
            f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
        except Exception as e:
            print(f"duplicate dialog f2f load failed: {e}")
            f2f_set = set()
        dlg = DuplicateMergeDialog(gid, issue_type, dialog_rows, self, f2f_set=f2f_set, fieldbook_path=getattr(self, "fieldbook_path", None))
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        if getattr(dlg, "result_action", None) != "approve":
            return
        # Apply results: primary gets merged desc as Corrected/Merged, merges+removes -> Removed, ignores -> Ignored
        primary_oid = str(dlg.result_primary).strip() if dlg.result_primary else ""
        merge_oids = set(str(o).strip() for o in getattr(dlg, "result_merge", []))
        ignore_oids = set(str(o).strip() for o in getattr(dlg, "result_ignore", []))
        remove_oids = set(str(o).strip() for o in getattr(dlg, "result_remove", []))
        merged_desc = str(getattr(dlg, "result_merged_desc", "")).strip()
        if not primary_oid:
            QMessageBox.warning(self, "Duplicate Merge", "No Primary selected — abort.")
            return
        # Validation: only 1 primary already enforced in dialog
        # Build lookup for status update
        # Update unified rows (_check_all_rows) and table display
        # For primary: Status = "Merged" (or "Corrected"), Comments = merged_desc
        # For merge_oids: Status = "Removed", Comments = f"Merged into {primary_oid} — '{merged_desc}'"
        # For remove_oids: Status = "Removed", Comments = "Removed via duplicate merge (not merged)"
        # For ignore_oids: Status = "Ignored", Comments = "Ignored via duplicate set (kept)"
        # Update table
        self.check_table.blockSignals(True)
        self._check_loading = True
        # Update _check_all_rows
        updated_gids = set()
        for ur in getattr(self, "_check_all_rows", []):
            if str(ur[0]).strip() != str(gid):
                continue
            oid = str(ur[3]).strip() if len(ur) > 3 else ""
            if oid == primary_oid:
                ur[7] = "Merged"
                ur[8] = merged_desc
                updated_gids.add(oid)
            elif oid in merge_oids:
                ur[7] = "Removed"
                ur[8] = f"Merged into {primary_oid} — '{merged_desc}'"
                updated_gids.add(oid)
            elif oid in remove_oids:
                ur[7] = "Removed"
                ur[8] = "Removed via duplicate merge (not merged)"
                updated_gids.add(oid)
            elif oid in ignore_oids:
                ur[7] = "Ignored"
                ur[8] = "Ignored via duplicate set (kept)"
                updated_gids.add(oid)
        # Update table display rows with same GroupID
        from PySide6.QtGui import QBrush, QColor
        for rr in range(self.check_table.rowCount()):
            it_gid = self.check_table.item(rr, 0)
            if not it_gid or it_gid.text().strip() != str(gid):
                continue
            it_oid = self.check_table.item(rr, 2)
            oid = it_oid.text().strip() if it_oid else ""
            status_item = self.check_table.item(rr, 11)
            comments_item = self.check_table.item(rr, 12)
            if status_item is None:
                from PySide6.QtWidgets import QTableWidgetItem
                status_item = QTableWidgetItem("")
                self.check_table.setItem(rr, 11, status_item)
            if comments_item is None:
                from PySide6.QtWidgets import QTableWidgetItem
                comments_item = QTableWidgetItem("")
                self.check_table.setItem(rr, 12, comments_item)
            if oid == primary_oid:
                status_item.setText("Merged")
                comments_item.setText(merged_desc)
                # Green for merged primary
                for c in range(self.check_table.columnCount()):
                    it = self.check_table.item(rr, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
            elif oid in merge_oids:
                status_item.setText("Removed")
                comments_item.setText(f"Merged into {primary_oid}")
                for c in range(self.check_table.columnCount()):
                    it = self.check_table.item(rr, c)
                    if it:
                        it.setBackground(QBrush(QColor("#FFEBEE")))
                status_item.setBackground(QBrush(QColor("#FFCDD2")))
            elif oid in remove_oids:
                status_item.setText("Removed")
                comments_item.setText("Removed via duplicate merge")
                for c in range(self.check_table.columnCount()):
                    it = self.check_table.item(rr, c)
                    if it:
                        it.setBackground(QBrush(QColor("#FFEBEE")))
                status_item.setBackground(QBrush(QColor("#FFCDD2")))
            elif oid in ignore_oids:
                status_item.setText("Ignored")
                comments_item.setText("Ignored via duplicate set")
                for c in range(self.check_table.columnCount()):
                    it = self.check_table.item(rr, c)
                    if it:
                        it.setBackground(QBrush(QColor("#E8F5E9")))
                status_item.setBackground(QBrush(QColor("#C8E6C9")))
        self.check_table.blockSignals(False)
        self._check_loading = False
        self.check_dirty = True
        self._update_check_title()
        self._update_check_hint()
        try:
            self._autosave_fwc()
        except Exception as e:
            print(f"autosave after duplicate merge failed: {e}")
        # Also need to update working edit_table if primary's desc changed? The duplicate merge's merged_desc is for primary's Desc field in working file (desc correction for duplicates).
        # For now, we store merged desc only in check report's Comments for primary; final out file processor will need to apply it.
        # Optionally, if we want live Desc correction, update edit_table's Desc for primary OID?
        # Let's also update edit_table's Desc column (5) for primary if merged_desc differs, and mark that Desc change in desc table?
        try:
            if primary_oid and merged_desc:
                # Find edit_table row for primary and update Desc (col 5) in memory? But we keep FWC-only pattern: don't mutate .fwk directly, just store in unified.
                # The final out file generation already respects Merged/Removed via _check_all_rows, so no need to touch edit_table.
                pass
        except:
            pass
        self.summary_label.setText(f"Group {gid} merged: Primary {primary_oid} gets '{merged_desc}' ({len(merge_oids)} merged+removed, {len(ignore_oids)} ignored, {len(remove_oids)} removed)")

    def _duplicate_fix_first(self):
        """Fix First for duplicates — sequential through GroupIDs (session tracking, Back for misclick)."""
        if self.check_table is None or self.check_table.rowCount() == 0:
            QMessageBox.information(self, "Fix First", "No Duplicate Groups — Run Checks First")
            return
        # Build groups from _check_all_rows where Status == Open (not yet handled)
        # Use GroupID grouping
        from collections import defaultdict
        groups = defaultdict(list)
        for ur in getattr(self, '_check_all_rows', []):
            if str(ur[7]).strip() != "Open":
                continue
            gid = str(ur[0]).strip()
            groups[gid].append(ur)
        if not groups:
            QMessageBox.information(self, "Fix First", "All Duplicate Groups Already Merged/Ignored/Removed")
            return
        # Session tracking: ordered GroupIDs in display order (as they appear in table) — include hidden so partial Fix Auto remainder is not missed
        session_gids = []
        seen = set()
        for rr in range(self.check_table.rowCount()):
            gid_it = self.check_table.item(rr, 0)
            if not gid_it:
                continue
            gid = gid_it.text().strip()
            if gid not in seen and gid in groups:
                seen.add(gid)
                session_gids.append(gid)
        # Also add any open groups not in table (e.g., filtered out or not yet displayed) — ensure partial autofix remainder is detected
        for gid in groups.keys():
            if gid not in seen:
                session_gids.append(gid)
        if not session_gids:
            session_gids = sorted(groups.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))
        else:
            # Keep display order but ensure numeric sort for any added at end is stable
            # Preserve original order for existing, appended remain at end
            pass
        idx = 0
        # Disable sorting during session
        prev_sort = self.check_table.isSortingEnabled()
        if prev_sort:
            self.check_table.setSortingEnabled(False)
        while 0 <= idx < len(session_gids):
            gid = session_gids[idx]
            # Find a representative row for this gid to get issue_type
            # Collect group rows for dialog
            group_urs = groups.get(gid, [])
            if not group_urs:
                idx += 1
                continue
            issue_type = group_urs[0][2] if len(group_urs[0]) > 2 else "Duplicate"
            # Build dialog_rows as in double-click handler
            oid_map = {}
            try:
                if self.edit_table and self.edit_table.rowCount() > 0:
                    for rr in range(self.edit_table.rowCount()):
                        oid_it = self.edit_table.item(rr, 0)
                        if oid_it:
                            o = oid_it.text().strip()
                            try:
                                row_texts = self._row_texts(self.edit_table, rr)
                            except:
                                row_texts = [self.edit_table.item(rr, c).text() if self.edit_table.item(rr, c) else "" for c in range(self.edit_table.columnCount())]
                            oid_map[o] = row_texts
                elif self.edit_file_path:
                    try:
                        from pathlib import Path
                        wr = self._read_working_file(self.edit_file_path)
                        for r in wr or []:
                            oid_map[str(r[0])] = r
                    except:
                        pass
            except:
                pass
            dialog_rows = []
            for ur in group_urs:
                oid = str(ur[3]).strip() if len(ur) > 3 else ""
                detail = ur[5] if len(ur) > 5 else ""
                status = ur[7] if len(ur) > 7 else "Open"
                comments = ur[8] if len(ur) > 8 else ""
                raw = oid_map.get(oid)
                if raw and len(raw) >= 6:
                    pt = raw[1] if len(raw) > 1 else ""
                    n = raw[2] if len(raw) > 2 else ""
                    e = raw[3] if len(raw) > 3 else ""
                    z = raw[4] if len(raw) > 4 else ""
                    desc = raw[5] if len(raw) > 5 else ""
                else:
                    # fallback to table
                    found = None
                    for rr in range(self.check_table.rowCount()):
                        it = self.check_table.item(rr, 2)
                        if it and it.text().strip() == oid:
                            found = rr
                            break
                    if found is not None:
                        pt = self.check_table.item(found, 3).text() if self.check_table.item(found, 3) else ""
                        n = self.check_table.item(found, 4).text() if self.check_table.item(found, 4) else ""
                        e = self.check_table.item(found, 5).text() if self.check_table.item(found, 5) else ""
                        z = self.check_table.item(found, 6).text() if self.check_table.item(found, 6) else ""
                        desc = self.check_table.item(found, 7).text() if self.check_table.item(found, 7) else ""
                    else:
                        pt=n=e=z=desc=""
                dialog_rows.append({"oid": oid, "pt_num": pt, "n": n, "e": e, "z": z, "desc": desc, "detail": detail, "status": status, "comments": comments, "raw_row": raw})
            try:
                dialog_rows.sort(key=lambda d: int(d["oid"]) if str(d["oid"]).isdigit() else str(d["oid"]))
            except:
                pass
            try:
                from .clean import DuplicateMergeDialog
                f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
            except:
                f2f_set = set()
            dlg = DuplicateMergeDialog(gid, issue_type, dialog_rows, self, f2f_set=f2f_set, fieldbook_path=getattr(self, "fieldbook_path", None))
            try:
                if hasattr(dlg, 'back_btn'):
                    dlg.back_btn.setEnabled(idx > 0)
            except:
                pass
            if dlg.exec() != dlg.DialogCode.Accepted:
                break
            if getattr(dlg, "result_action", None) == "back":
                idx = max(0, idx - 1)
                continue
            if getattr(dlg, "result_action", None) != "approve":
                # Deny or other -> stay? For Fix First, deny should skip to next
                idx += 1
                continue
            # Approve -> apply same as double-click handler
            primary_oid = str(dlg.result_primary).strip() if dlg.result_primary else ""
            merge_oids = set(str(o).strip() for o in getattr(dlg, "result_merge", []))
            ignore_oids = set(str(o).strip() for o in getattr(dlg, "result_ignore", []))
            remove_oids = set(str(o).strip() for o in getattr(dlg, "result_remove", []))
            merged_desc = str(getattr(dlg, "result_merged_desc", "")).strip()
            # Update _check_all_rows and table
            self.check_table.blockSignals(True)
            self._check_loading = True
            for ur in getattr(self, "_check_all_rows", []):
                if str(ur[0]).strip() != str(gid):
                    continue
                oid = str(ur[3]).strip() if len(ur) > 3 else ""
                if oid == primary_oid:
                    ur[7] = "Merged"
                    ur[8] = merged_desc
                elif oid in merge_oids:
                    ur[7] = "Removed"
                    ur[8] = f"Merged into {primary_oid}"
                elif oid in remove_oids:
                    ur[7] = "Removed"
                    ur[8] = "Removed via duplicate merge"
                elif oid in ignore_oids:
                    ur[7] = "Ignored"
                    ur[8] = "Ignored via duplicate set"
            from PySide6.QtGui import QBrush, QColor
            for rr in range(self.check_table.rowCount()):
                it_gid = self.check_table.item(rr, 0)
                if not it_gid or it_gid.text().strip() != str(gid):
                    continue
                it_oid = self.check_table.item(rr, 2)
                oid = it_oid.text().strip() if it_oid else ""
                status_item = self.check_table.item(rr, 11)
                comments_item = self.check_table.item(rr, 12)
                if status_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    status_item = QTableWidgetItem("")
                    self.check_table.setItem(rr, 11, status_item)
                if comments_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    comments_item = QTableWidgetItem("")
                    self.check_table.setItem(rr, 12, comments_item)
                if oid == primary_oid:
                    status_item.setText("Merged")
                    comments_item.setText(merged_desc)
                    for c in range(self.check_table.columnCount()):
                        it = self.check_table.item(rr, c)
                        if it:
                            it.setBackground(QBrush(QColor("#E8F5E9")))
                elif oid in merge_oids:
                    status_item.setText("Removed")
                    comments_item.setText(f"Merged into {primary_oid}")
                    for c in range(self.check_table.columnCount()):
                        it = self.check_table.item(rr, c)
                        if it:
                            it.setBackground(QBrush(QColor("#FFEBEE")))
                    status_item.setBackground(QBrush(QColor("#FFCDD2")))
                elif oid in remove_oids:
                    status_item.setText("Removed")
                    comments_item.setText("Removed via duplicate merge")
                    for c in range(self.check_table.columnCount()):
                        it = self.check_table.item(rr, c)
                        if it:
                            it.setBackground(QBrush(QColor("#FFEBEE")))
                    status_item.setBackground(QBrush(QColor("#FFCDD2")))
                elif oid in ignore_oids:
                    status_item.setText("Ignored")
                    comments_item.setText("Ignored via duplicate set")
                    for c in range(self.check_table.columnCount()):
                        it = self.check_table.item(rr, c)
                        if it:
                            it.setBackground(QBrush(QColor("#E8F5E9")))
                    status_item.setBackground(QBrush(QColor("#C8E6C9")))
            self.check_table.blockSignals(False)
            self._check_loading = False
            self.check_dirty = True
            self._update_check_title()
            try:
                self._autosave_fwc()
            except:
                pass
            self._update_renumber_lock()
            # Remove handled gid from session tracking? Keep session static so Back can revisit, but forward should go to next
            idx += 1
        try:
            if prev_sort:
                self.check_table.setSortingEnabled(True)
        except:
            pass

    def _duplicate_fix_auto(self):
        """Fix Auto for duplicates — batch auto-merge all open groups (Primary = smallest OID)."""
        if self.check_table is None or self.check_table.rowCount() == 0:
            QMessageBox.information(self, "Fix Auto", "No Duplicate Groups — Run Checks First")
            return
        # Group open groups
        from collections import defaultdict
        groups_dict = defaultdict(list)
        for ur in getattr(self, '_check_all_rows', []):
            if str(ur[7]).strip() != "Open":
                continue
            gid = str(ur[0]).strip()
            groups_dict[gid].append(ur)
        if not groups_dict:
            QMessageBox.information(self, "Fix Auto", "All Duplicate Groups Already Merged/Ignored/Removed")
            return
        # Build groups for dialog: need rows with live desc
        oid_map = {}
        try:
            if self.edit_table and self.edit_table.rowCount() > 0:
                for rr in range(self.edit_table.rowCount()):
                    oid_it = self.edit_table.item(rr, 0)
                    if oid_it:
                        o = oid_it.text().strip()
                        try:
                            row_texts = self._row_texts(self.edit_table, rr)
                        except:
                            row_texts = [self.edit_table.item(rr, c).text() if self.edit_table.item(rr, c) else "" for c in range(self.edit_table.columnCount())]
                        oid_map[o] = row_texts
            elif self.edit_file_path:
                try:
                    from pathlib import Path
                    wr = self._read_working_file(self.edit_file_path)
                    for r in wr or []:
                        oid_map[str(r[0])] = r
                except:
                    pass
        except:
            pass
        groups = []
        for gid, urs in groups_dict.items():
            issue_type = urs[0][2] if len(urs[0]) > 2 else "Duplicate"
            rows = []
            for ur in urs:
                oid = str(ur[3]).strip() if len(ur) > 3 else ""
                detail = ur[5] if len(ur) > 5 else ""
                status = ur[7] if len(ur) > 7 else "Open"
                comments = ur[8] if len(ur) > 8 else ""
                raw = oid_map.get(oid)
                if raw and len(raw) >= 6:
                    pt = raw[1] if len(raw) > 1 else ""
                    n = raw[2] if len(raw) > 2 else ""
                    e = raw[3] if len(raw) > 3 else ""
                    z = raw[4] if len(raw) > 4 else ""
                    desc = raw[5] if len(raw) > 5 else ""
                else:
                    found = None
                    for rr in range(self.check_table.rowCount()):
                        it = self.check_table.item(rr, 2)
                        if it and it.text().strip() == oid:
                            found = rr
                            break
                    if found is not None:
                        pt = self.check_table.item(found, 3).text() if self.check_table.item(found, 3) else ""
                        n = self.check_table.item(found, 4).text() if self.check_table.item(found, 4) else ""
                        e = self.check_table.item(found, 5).text() if self.check_table.item(found, 5) else ""
                        z = self.check_table.item(found, 6).text() if self.check_table.item(found, 6) else ""
                        desc = self.check_table.item(found, 7).text() if self.check_table.item(found, 7) else ""
                    else:
                        pt=n=e=z=desc=""
                rows.append({"oid": oid, "pt_num": pt, "n": n, "e": e, "z": z, "desc": desc, "detail": detail, "status": status, "comments": comments})
            groups.append({"group_id": gid, "issue_type": issue_type, "rows": rows})
        # Sort groups by GroupID
        try:
            groups.sort(key=lambda g: int(str(g.get("group_id","")).strip()) if str(g.get("group_id","")).strip().isdigit() else str(g.get("group_id","")).strip())
        except:
            pass
        try:
            from .clean import BatchDuplicateDialog
            f2f_set = build_f2f_set_from_fieldbook(self.fieldbook_path)
        except:
            f2f_set = set()
        dlg = BatchDuplicateDialog(groups, self, f2f_set=f2f_set, fieldbook_path=getattr(self, "fieldbook_path", None))
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        selected = getattr(dlg, "result_selected", [])
        if not selected:
            return
        # Apply each selected group
        from PySide6.QtGui import QBrush, QColor
        self.check_table.blockSignals(True)
        self._check_loading = True
        applied = 0
        for gid, primary_oid, merge_oids, ignore_oids, remove_oids, merged_desc in selected:
            merge_set = set(merge_oids)
            # Update unified
            for ur in getattr(self, "_check_all_rows", []):
                if str(ur[0]).strip() != str(gid):
                    continue
                oid = str(ur[3]).strip() if len(ur) > 3 else ""
                if oid == str(primary_oid).strip():
                    ur[7] = "Merged"
                    ur[8] = merged_desc
                elif oid in merge_set:
                    ur[7] = "Removed"
                    ur[8] = f"Merged into {primary_oid}"
            # Update table
            for rr in range(self.check_table.rowCount()):
                it_gid = self.check_table.item(rr, 0)
                if not it_gid or it_gid.text().strip() != str(gid):
                    continue
                it_oid = self.check_table.item(rr, 2)
                oid = it_oid.text().strip() if it_oid else ""
                status_item = self.check_table.item(rr, 11)
                comments_item = self.check_table.item(rr, 12)
                if status_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    status_item = QTableWidgetItem("")
                    self.check_table.setItem(rr, 11, status_item)
                if comments_item is None:
                    from PySide6.QtWidgets import QTableWidgetItem
                    comments_item = QTableWidgetItem("")
                    self.check_table.setItem(rr, 12, comments_item)
                if oid == str(primary_oid).strip():
                    status_item.setText("Merged")
                    comments_item.setText(merged_desc)
                    for c in range(self.check_table.columnCount()):
                        it = self.check_table.item(rr, c)
                        if it:
                            it.setBackground(QBrush(QColor("#E8F5E9")))
                elif oid in merge_set:
                    status_item.setText("Removed")
                    comments_item.setText(f"Merged into {primary_oid}")
                    for c in range(self.check_table.columnCount()):
                        it = self.check_table.item(rr, c)
                        if it:
                            it.setBackground(QBrush(QColor("#FFEBEE")))
                    status_item.setBackground(QBrush(QColor("#FFCDD2")))
            applied += 1
        self.check_table.blockSignals(False)
        self._check_loading = False
        self.check_dirty = True
        self._update_check_title()
        self._update_check_hint()
        try:
            self._autosave_fwc()
        except:
            pass
        self._update_renumber_lock()
        self.summary_label.setText(f"Fix Auto: {applied} duplicate groups auto-merged (Primary=smallest OID) — Next: Renumber Dups (hole/at-end/keyed) to assign final numbers to remaining duplicate OIDs via Dup_Renumber column")



    def _load_check_report(self, path):
        # Try unified first, fallback to legacy
        headers, rows = read_unified_report(Path(path))
        if headers is None:
            headers, rows = read_check_report(Path(path))
            if headers is None:
                self.summary_label.setText(f"Check report load failed: {os.path.basename(path)}")
                return False
            # Convert legacy to unified already done in read_unified, but if we fell back, rows are legacy 13-col
            # Convert manually
            rows = [[r[0], "Duplicate", r[1], r[2], "", r[10], "", r[11], r[12]] for r in rows]
            headers = UNIFIED_REPORT_HEADERS
        self.check_report_path = path
        self._check_loading = True
        # Need working rows for live pull
        working_rows = None
        if self.edit_table is not None and self.edit_table.rowCount() > 0:
            working_rows = [self._row_texts(self.edit_table, r) for r in range(self.edit_table.rowCount())]
            try: working_rows.sort(key=lambda r: int(r[0]) if str(r[0]).isdigit() else r[0])
            except: pass
        elif self.edit_file_path and Path(self.edit_file_path).exists():
            working_rows = self._read_working_file(self.edit_file_path) or []
        else:
            working_rows = []
        self._fill_check_table_unified(rows, working_rows)
        self._fill_desc_parse_table_unified(rows, working_rows)
        self._check_loading = False
        self.check_dirty = False
        self._update_check_title()
        self._update_desc_parse_title()
        # Ensure both tabs visible if file had rows
        if any(r[1]=="Duplicate" for r in rows):
            self._show_tab(self.check_tab, "Duplicate Error")
        if any(r[1]=="Description" for r in rows):
            self._show_tab(self.desc_parse_tab, "Description Error")
        return True

    def _on_load_check_report(self):
        if not self._confirm_check_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Check Report", self.project_path or "", f"Check Report (*{CHECK_REPORT_EXT});;All Files (*)")
        if path and self._load_check_report(path):
            self._mark_dirty()  # project now points at this report
            self.summary_label.setText(f"Check Report loaded: {os.path.basename(path)} ({self.check_table.rowCount()} rows shown, {len(getattr(self,'_check_all_rows',[]))} total)")

    def _save_check_report(self):
        if not self.check_report_path:
            default_dir = self.project_path if self.project_path and Path(self.project_path).exists() else ""
            path, _ = QFileDialog.getSaveFileName(self, f"Save Check Report ({CHECK_REPORT_EXT})", default_dir, f"Check Report (*{CHECK_REPORT_EXT});;All Files (*)")
            if not path:
                return
        if Path(path).suffix.lower() not in (CHECK_REPORT_EXT, LEGACY_CHECK_EXT):
            path += CHECK_REPORT_EXT
            self.check_report_path = path
            self._mark_dirty()
        # Gather unified rows from both tables' underlying stores (_check_all_rows is unified minimal for Duplicate, _desc_parse_all_rows for Description)
        # But edits to Status/Comments are in table items — we need to sync them back to unified rows before save
        # Collect current unified from file + live edits: rebuild from tables' current Status/Comments
        unified = []
        # For Duplicate table, map GroupID+OID -> Status/Comments from table
        # Build lookup from check_table rows (GroupID col0, OID col2, Status col11, Comments col12)
        dup_lookup = {}
        for r in range(self.check_table.rowCount()):
            gid = self.check_table.item(r,0).text() if self.check_table.item(r,0) else ""
            oid = self.check_table.item(r,2).text() if self.check_table.item(r,2) else ""
            status = self.check_table.item(r,11).text() if self.check_table.item(r,11) else "Open"
            comments = self.check_table.item(r,12).text() if self.check_table.item(r,12) else ""
            dup_lookup[(gid, oid)] = (status, comments)
        # For Description table, need mapping: DESC table has Flags col8, FlagDetail col9 — but unified For Description stores Flags/Detail/FlagDetail
        # We'll use OID + IssueType as key
        # Simpler: read current unified file if exists and update Status/Comments from tables, else use in-memory _check_all_rows
        # Try to load existing unified file to preserve GroupID/DisplayTab/IssueType/Flags/Detail/FlagDetail
        existing = []
        if Path(self.check_report_path).exists():
            _, existing = read_unified_report(Path(self.check_report_path))
            existing = existing or []
        else:
            existing = getattr(self, "_check_all_rows", []) or []
            # If _check_all_rows is legacy 9-col unified, keep; else if empty, use desc too
            if not existing:
                existing = []
        # If existing empty, try to reconstruct from tables (fallback)
        if not existing:
            # Build from check table display (legacy fallback)
            for r in range(self.check_table.rowCount()):
                gid = self.check_table.item(r,0).text() if self.check_table.item(r,0) else str(r+1)
                issue = self.check_table.item(r,1).text() if self.check_table.item(r,1) else ""
                oid = self.check_table.item(r,2).text() if self.check_table.item(r,2) else ""
                detail = self.check_table.item(r,10).text() if self.check_table.item(r,10) else ""
                status = self.check_table.item(r,11).text() if self.check_table.item(r,11) else "Open"
                comments = self.check_table.item(r,12).text() if self.check_table.item(r,12) else ""
                unified.append([gid, "Duplicate", issue, oid, "", detail, "", status, comments])
            # Add desc rows similarly (if any)
            for r in range(self.desc_parse_table.rowCount()):
                oid = self.desc_parse_table.item(r,0).text() if self.desc_parse_table.item(r,0) else ""
                flags = self.desc_parse_table.item(r,8).text() if self.desc_parse_table.item(r,8) else ""
                flagdetail = self.desc_parse_table.item(r,9).text() if self.desc_parse_table.item(r,9) else ""
                corr = self.desc_parse_table.item(r,10).text().strip() if self.desc_parse_table.item(r,10) else ""
                ign = self.desc_parse_table.item(r,11).text().strip() if self.desc_parse_table.item(r,11) else ""
                if corr:
                    status, comments = "Corrected", corr
                elif ign == "Removed":
                    status, comments = "Removed", "Removed via Clean — will be omitted in final out file"
                elif ign == "Ignored":
                    status, comments = "Ignored", f"Ignored — {flags}"
                else:
                    status, comments = "Open", ""
                # IssueType from flags first
                issue = flags.split(";")[0].split(":")[0].strip() if flags else "DescFlag"
                unified.append([str(len(unified)+1), "Description", issue, oid, flags, flags, flagdetail, status, comments])
            rows = unified
        else:
            # Build lookup for Description corrections from desc table and from in-memory _desc_parse_all_rows (fwc-only)
            desc_lookup = {}
            # First pull from desc_parse_table live edits (Correction col10, Removed col11)
            try:
                for r2 in range(self.desc_parse_table.rowCount()):
                    oid2 = self.desc_parse_table.item(r2, 0).text().strip() if self.desc_parse_table.item(r2, 0) else ""
                    if not oid2:
                        continue
                    corr_it = self.desc_parse_table.item(r2, 10)
                    ign_it = self.desc_parse_table.item(r2, 11)
                    corr = corr_it.text().strip() if corr_it else ""
                    ign = ign_it.text().strip() if ign_it else ""
                    # Flags for Ignored detail
                    flags2 = self.desc_parse_table.item(r2, 8).text().strip() if self.desc_parse_table.item(r2, 8) else ""
                    if corr:
                        desc_lookup[oid2] = ("Corrected", corr)
                    elif ign == "Removed":
                        desc_lookup[oid2] = ("Removed", "Removed via Clean — will be omitted in final out file")
                    elif ign == "Ignored":
                        desc_lookup[oid2] = ("Ignored", f"Ignored — {flags2}")
            except Exception:
                pass
            # Also merge from in-memory unified rows (covers batch updates not yet reflected if table reload lag)
            try:
                for ur in getattr(self, "_desc_parse_all_rows", []):
                    oid3 = str(ur[3]).strip()
                    st, cm = ur[7], ur[8]
                    if st in ("Corrected","Ignored","Removed"):
                        desc_lookup[oid3] = (st, cm)
            except Exception:
                pass
            # Update Status/Comments from tables
            rows = []
            for r in existing:
                gid, display, issue, oid = r[0], r[1], r[2], r[3]
                status, comments = r[7], r[8]
                # Try to find updated status/comments
                if display == "Duplicate":
                    # Find in dup_lookup by (gid, oid)
                    if (gid, oid) in dup_lookup:
                        status, comments = dup_lookup[(gid, oid)]
                else:
                    # For Description, pull from desc_lookup (fwc is source of truth — Correction lives in .fwc Comments)
                    if str(oid) in desc_lookup:
                        status, comments = desc_lookup[str(oid)]
                    elif st_inmem := next((ur[7:9] for ur in getattr(self, "_desc_parse_all_rows", []) if str(ur[3])==str(oid)), None):
                        # fallback — already covered by desc_lookup, but keep
                        if st_inmem[0] in ("Corrected","Ignored","Removed"):
                            status, comments = st_inmem
                # Preserve other cols
                rows.append([gid, display, issue, oid, r[4], r[5], r[6], status, comments])
        if not write_unified_report(Path(self.check_report_path), rows):
            QMessageBox.critical(self, "Save Check Report", f"Failed to write {self.check_report_path}")
            return
        # Sync in-memory unified stores to written rows (fwc is source of truth)
        try:
            self._check_all_rows = [r for r in rows if r[1]=="Duplicate"]
            self._desc_parse_all_rows = [r for r in rows if r[1]=="Description"]
        except Exception:
            pass
        self.check_dirty = False
        self._update_check_title()
        self.summary_label.setText(f"Check Report saved: {os.path.basename(self.check_report_path)} ({len(rows)} rows)")
        self._update_check_path_label()


    def _autosave_fwc(self):
        """Autosave current unified state to FWC file if path exists — makes FWC the truth file immediately after Fix/Ignore/Removed.
        Called after each Clean action so REMOVED/IGNORED/CORRECTED are persisted without manual Save Report.
        If no file yet, just marks dirty (will be saved on next manual Save).
        Merges table edits (Status/Comments) like _save_check_report so direct table edits are also persisted.
        """
        try:
            if not getattr(self, "check_report_path", "") or not pathlib.Path(self.check_report_path).exists():
                return False
            # Build lookup from tables for current Status/Comments (handles direct edits)
            dup_lookup = {}
            try:
                if self.check_table is not None:
                    for r in range(self.check_table.rowCount()):
                        gid = self.check_table.item(r,0).text() if self.check_table.item(r,0) else ""
                        oid = self.check_table.item(r,2).text() if self.check_table.item(r,2) else ""
                        status = self.check_table.item(r,11).text() if self.check_table.item(r,11) else "Open"
                        comments = self.check_table.item(r,12).text() if self.check_table.item(r,12) else ""
                        if gid and oid:
                            dup_lookup[(gid, oid)] = (status, comments)
            except Exception:
                pass
            desc_lookup = {}
            try:
                if self.desc_parse_table is not None:
                    for r in range(self.desc_parse_table.rowCount()):
                        oid2 = self.desc_parse_table.item(r, 0).text().strip() if self.desc_parse_table.item(r, 0) else ""
                        if not oid2:
                            continue
                        corr_it = self.desc_parse_table.item(r, 10)
                        ign_it = self.desc_parse_table.item(r, 11)
                        corr = corr_it.text().strip() if corr_it else ""
                        ign = ign_it.text().strip() if ign_it else ""
                        flags2 = self.desc_parse_table.item(r, 8).text().strip() if self.desc_parse_table.item(r, 8) else ""
                        if corr:
                            desc_lookup[oid2] = ("Corrected", corr)
                        elif ign == "Removed":
                            desc_lookup[oid2] = ("Removed", "Removed via Clean — will be omitted in final out file")
                        elif ign == "Ignored":
                            desc_lookup[oid2] = ("Ignored", f"Ignored — {flags2}")
            except Exception:
                pass
            # Also merge from in-memory unified rows (covers batch updates not yet reflected in table due to lag)
            try:
                for ur in getattr(self, "_desc_parse_all_rows", []) or []:
                    oid3 = str(ur[3]).strip()
                    st, cm = ur[7], ur[8]
                    if st in ("Corrected","Ignored","Removed"):
                        desc_lookup[oid3] = (st, cm)
                for ur in getattr(self, "_check_all_rows", []) or []:
                    gid3 = str(ur[0]).strip()
                    oid3 = str(ur[3]).strip()
                    st, cm = ur[7], ur[8]
                    if st and st != "Open":
                        # Only override if not already in dup_lookup (table is more recent)
                        if (gid3, oid3) not in dup_lookup:
                            dup_lookup[(gid3, oid3)] = (st, cm)
            except Exception:
                pass
            # Load existing file to preserve GroupID/DisplayTab/IssueType/Flags/Detail/FlagDetail, then update Status/Comments
            from .io_carlson import read_unified_report, write_unified_report
            _, existing = read_unified_report(pathlib.Path(self.check_report_path))
            existing = existing or []
            if not existing:
                # Fallback: use in-memory stores directly
                existing = (getattr(self, "_check_all_rows", []) or []) + (getattr(self, "_desc_parse_all_rows", []) or [])
            if not existing:
                return False
            rows = []
            for r in existing:
                gid, display, issue, oid = r[0], r[1], r[2], r[3]
                status, comments = r[7], r[8]
                if display == "Duplicate":
                    if (gid, oid) in dup_lookup:
                        status, comments = dup_lookup[(gid, oid)]
                else:
                    if str(oid) in desc_lookup:
                        status, comments = desc_lookup[str(oid)]
                rows.append([gid, display, issue, oid, r[4], r[5], r[6], status, comments])
            if write_unified_report(pathlib.Path(self.check_report_path), rows):
                # Sync in-memory stores
                try:
                    self._check_all_rows = [r for r in rows if r[1]=="Duplicate"]
                    self._desc_parse_all_rows = [r for r in rows if r[1]=="Description"]
                except Exception:
                    pass
                self.check_dirty = False
                self._update_check_title()
                try:
                    self._update_check_path_label()
                except Exception:
                    pass
                return True
        except Exception as e:
            print(f"_autosave_fwc failed: {e}")
        return False

    def _confirm_check_discard(self):
        if not getattr(self, "check_dirty", False):
            return True
        reply = QMessageBox.warning(
            self, "Unsaved Check Report",
            "Check report has unsaved Status/Comments. Save before continuing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Save:
            self._save_check_report()
            return not self.check_dirty
        elif reply == QMessageBox.StandardButton.Discard:
            return True
        else:
            return False

    def _reset_check_phase(self):
        if self.check_table is not None:
            self.check_table.blockSignals(True)
            self.check_table.setRowCount(0)
            self.check_table.blockSignals(False)
        self.check_report_path = ""
        self.check_dirty = False
        self._check_all_rows = []
        if hasattr(self, "_check_loading"):
            self._check_loading = False
        self._update_check_title()
        if self.check_tab is not None:
            self._update_check_hint()
            self._update_check_path_label()

    def _refresh_check_tab(self):
        if self.check_table is None:
            return
        if self.check_report_path and Path(self.check_report_path).exists():
            self._load_check_report(self.check_report_path)
            self.check_dirty = False
            self._update_check_title()
        else:
            self._reset_check_phase()
            self._update_check_path_label()

    def _run_checks(self):
        """Legacy entry — now routes to unified _run_all_checks (single file, DisplayTab). Kept for internal callers; use _run_all_checks directly."""
        # Delegate to unified to avoid duplicate logic and keep OID-minimal single file
        return self._run_all_checks()

    # ----- Project dirty state -----
    def _confirm_discard(self):
        if not self.is_dirty:
            return True
        reply = QMessageBox.warning(
            self, "Unsaved Changes", "You have unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        )
        if reply == QMessageBox.StandardButton.Save:
            self._save()
            return not self.is_dirty
        elif reply == QMessageBox.StandardButton.Discard:
            return True
        else:
            return False

    def _mark_dirty(self):
        self.is_dirty = True
        self._update_title()
        self._update_status()

    def _update_title(self):
        star = " *" if self.is_dirty else ""
        name = os.path.basename(self.current_file) if self.current_file else "Untitled"
        self.setWindowTitle(f"Fieldwork Manager — {name}{star}")

    def _update_status(self):
        star = " *" if self.is_dirty else ""
        if self.current_file:
            name = os.path.basename(self.current_file)
            self.status_label.setText(f"Project: {name}{star}")
        else:
            self.status_label.setText(f"Untitled{star}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

