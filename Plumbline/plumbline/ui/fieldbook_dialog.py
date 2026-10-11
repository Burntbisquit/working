"""Survey > Field Book: manage, convert, select, and report project field books."""
from __future__ import annotations

import csv
import datetime
import html
import os
from pathlib import Path
import shutil
import traceback
import zipfile

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QSplitter, QTabWidget, QTableWidget,
                               QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from ..core.featurecodes import FeatureCode, FeatureCodeTable, kind_for_entity
from ..core.fieldbook_syntax import COMMAND_MEANINGS, command_map
from ..core.fieldbook import archive_old_fieldbooks, generate_fieldbook_report_text, place_fieldbook_in_project
from ..io import f2f
from .f2f_dialog import ConvertFieldToFinishDialog, CodeCommandsDialog, CorrectionRulesDialog
from .widgets import Banner, Hint, hline, error_box


class FieldBookReportDialog(QDialog):
    """View and export a comprehensive Field Book report."""

    def __init__(self, project, parent=None, fwb_path=None):
        super().__init__(parent)
        self.project = project
        self.fwb_path = fwb_path or project.settings.get("fieldbook_file", "")
        self.setWindowTitle("Field Book Report")

        report_text = generate_fieldbook_report_text(project, self.fwb_path)
        # Default start width large enough to prevent word wrap at startup, but allows resizing
        max_line_len = max((len(l) for l in report_text.splitlines()), default=80)
        needed_width = max(880, min(int(max_line_len * 9.0) + 60, 1400))
        self.resize(needed_width, 600)
        lay = QVBoxLayout(self)

        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setFont(QFont("Consolas", 10))
        self.text_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.text_view.setPlainText(report_text)
        lay.addWidget(self.text_view)

        btn_row = QHBoxLayout()
        b_export = QPushButton("Export Report (Text / CSV)...")
        b_export.clicked.connect(self._export_report)
        b_copy = QPushButton("Copy to Clipboard")
        b_copy.clicked.connect(self._copy_clipboard)
        btn_row.addWidget(b_export)
        btn_row.addWidget(b_copy)
        btn_row.addStretch(1)

        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        btn_row.addWidget(b_close)
        lay.addLayout(btn_row)

    def _copy_clipboard(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.text_view.toPlainText())
        QMessageBox.information(self, "Field Book Report", "Report copied to clipboard.")

    def _default_report_path(self) -> str:
        """Start beside the selected Field Book, using its name for the exported report."""
        if self.fwb_path:
            return str(Path(self.fwb_path).expanduser().with_suffix(".txt"))
        project_path = str(getattr(self.project, "path", "") or "").strip()
        if project_path:
            source_path = Path(project_path).expanduser()
            if source_path.suffix:
                return str(source_path.with_suffix(".txt"))
        project_name = Path(str(getattr(self.project, "name", "") or "").strip()).name
        return f"{project_name or 'Field_Book_Report'}.txt"

    def _export_report(self):
        path, filt = QFileDialog.getSaveFileName(
            self, "Export Field Book Report", self._default_report_path(),
            "Text Report (*.txt);;CSV Table (*.csv);;All Files (*)")
        if not path:
            return
        selected_filter = str(filt or "").casefold()
        target_extension = ""
        if "csv" in selected_filter:
            target_extension = ".csv"
        elif "text report" in selected_filter:
            target_extension = ".txt"
        if target_extension:
            target_path = Path(path)
            if target_path.suffix.casefold() != target_extension:
                path = str(target_path.with_suffix(target_extension))
        codes = self.project.codes
        codes_items = list(codes.items()) if hasattr(codes, "items") else list(getattr(codes, "codes", {}).items()) if hasattr(codes, "codes") else ([(c.code, c) for c in codes] if isinstance(codes, (list, tuple)) else [])
        if path.lower().endswith(".csv") or "csv" in selected_filter:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["Code", "Description", "Kind", "Layer", "Symbol"])
                for code, fc in sorted(codes_items):
                    w.writerow([code, fc.name, fc.kind, fc.layer, fc.symbol])
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.text_view.toPlainText())
        QMessageBox.information(self, "Export Report", f"Report saved to:\n{path}")


class FieldBookDialog(QDialog):
    """Survey > Field Book: unified options to Convert, Select, and Report field books."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.project = state.project
        self.setWindowTitle("Field Book")
        self.resize(840, 600)
        self.setMinimumWidth(700)
        self.setMinimumHeight(480)

        main_lay = QVBoxLayout(self)

        # Top Banner
        self.banner = Banner("", "info")
        main_lay.addWidget(self.banner)

        # Primary Options Bar: Convert, Select, Report (standard buttons without blue highlight)
        opt_box = QGroupBox("Field Book Actions")
        opt_lay = QHBoxLayout(opt_box)

        self.btn_convert = QPushButton("Convert Field Book...")
        self.btn_convert.setAutoDefault(False)
        self.btn_convert.setToolTip("Convert a Carlson Field-to-Finish CSV or custom code table into a project field book (.fwb)")
        self.btn_convert.clicked.connect(self.action_convert)

        self.btn_select = QPushButton("Select Field Book...")
        self.btn_select.setAutoDefault(False)
        self.btn_select.setToolTip("Pick an existing field book file (.fwb). If selected from another location, it will be copied into the project's Field Book folder with previous versions archived.")
        self.btn_select.clicked.connect(self.action_select)

        self.btn_report = QPushButton("Field Book Report...")
        self.btn_report.setAutoDefault(False)
        self.btn_report.setToolTip("View and export the code table, line commands, and correction rules report.")
        self.btn_report.clicked.connect(self.action_report)

        opt_lay.addWidget(self.btn_convert)
        opt_lay.addWidget(self.btn_select)
        opt_lay.addWidget(self.btn_report)
        main_lay.addWidget(opt_box)

        # Tabbed details: Codes, command meanings, Correction Rules
        self.tabs = QTabWidget()

        # Tab 1: Codes Table
        tab_codes = QWidget()
        tab_codes_lay = QVBoxLayout(tab_codes)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Search codes:"))
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("Filter by code, description, layer, or symbol...")
        self.ed_filter.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.ed_filter)
        self.lbl_count = QLabel("0 codes")
        filter_row.addWidget(self.lbl_count)
        tab_codes_lay.addLayout(filter_row)

        self.table_codes = QTableWidget()
        self.table_codes.setColumnCount(5)
        self.table_codes.setHorizontalHeaderLabels(["Code", "Description", "Kind", "Layer", "Symbol"])
        self.table_codes.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table_codes.verticalHeader().setVisible(False)
        self.table_codes.setAlternatingRowColors(True)
        self.table_codes.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_codes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tab_codes_lay.addWidget(self.table_codes)

        self.tabs.addTab(tab_codes, "Feature Codes")

        # Tab 2: Code Commands
        tab_cmds = QWidget()
        tab_cmds_lay = QVBoxLayout(tab_cmds)
        tab_cmds_lay.addWidget(Hint("Field Book commands map editable tokens to fixed meanings for line starts, curves, line ends, closes, multi-code groups, and free-text descriptions. Spacing preferences are configured in Settings."))
        self.table_cmds = QTableWidget()
        self.table_cmds.setColumnCount(2)
        self.table_cmds.setHorizontalHeaderLabels(["Meaning", "Command Token"])
        self.table_cmds.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table_cmds.verticalHeader().setVisible(False)
        self.table_cmds.setAlternatingRowColors(True)
        self.table_cmds.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tab_cmds_lay.addWidget(self.table_cmds)

        btn_cmd_row = QHBoxLayout()
        b_edit_cmds = QPushButton("Edit Command Meanings...")
        b_edit_cmds.setToolTip("Edit the command token assigned to each fixed Field Book meaning; save it with the active book.")
        b_edit_cmds.clicked.connect(self._edit_commands)
        btn_cmd_row.addWidget(b_edit_cmds)
        btn_cmd_row.addStretch(1)
        tab_cmds_lay.addLayout(btn_cmd_row)

        self.tabs.addTab(tab_cmds, "Commands & Meanings")

        # Tab 3: Correction Rules
        tab_rules = QWidget()
        tab_rules_lay = QVBoxLayout(tab_rules)
        tab_rules_lay.addWidget(Hint("Correction rules map field typos to valid codes; each Fix is checked against the active Field Book."))
        self.table_rules = QTableWidget()
        self.table_rules.setColumnCount(2)
        self.table_rules.setHorizontalHeaderLabels(["Common Error", "Fix"])
        self.table_rules.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table_rules.verticalHeader().setVisible(False)
        self.table_rules.setAlternatingRowColors(True)
        self.table_rules.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tab_rules_lay.addWidget(self.table_rules)

        btn_rules_row = QHBoxLayout()
        b_edit_rules = QPushButton("Edit Correction Rules...")
        b_edit_rules.clicked.connect(self._edit_rules)
        btn_rules_row.addWidget(b_edit_rules)
        btn_rules_row.addStretch(1)
        tab_rules_lay.addLayout(btn_rules_row)

        self.tabs.addTab(tab_rules, "Correction Rules")

        main_lay.addWidget(self.tabs, 1)

        # Bottom row
        bot_row = QHBoxLayout()
        bot_row.addStretch(1)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        bot_row.addWidget(b_close)
        main_lay.addLayout(bot_row)

        self._refresh()

    def _refresh(self):
        pr = self.project
        fb_path = pr.settings.get("fieldbook_file") or ""
        if fb_path and Path(fb_path).exists():
            try:
                extra = f2f.read_fwb_extra(fb_path)
                # The selected book is the source of truth for its command meanings and rules.
                pr.settings["f2f_commands"] = extra.get("commands", list(f2f.DEFAULT_COMMANDS))
                pr.settings["f2f_rules"] = extra.get("rules", [])
            except Exception:
                pass
        n_codes = len(pr.codes)

        if fb_path and Path(fb_path).exists() and n_codes > 0:
            self.banner.set(f"Active Field Book: {Path(fb_path).name} ({n_codes:,} codes loaded) — Location: {fb_path}", "ok")
        elif n_codes > 0:
            self.banner.set(f"Feature Codes loaded: {n_codes:,} code(s) active in project.", "info")
        else:
            self.banner.set("No Field Book codes loaded. Select or convert a Field Book to populate this project.", "warn")

        self._fill_codes()
        self._fill_commands()
        self._fill_rules()

    def _fill_codes(self):
        codes = self.project.codes
        filter_text = self.ed_filter.text().strip().casefold()
        codes_items = list(codes.items()) if hasattr(codes, "items") else list(getattr(codes, "codes", {}).items()) if hasattr(codes, "codes") else ([(c.code, c) for c in codes] if isinstance(codes, (list, tuple)) else [])
        items = []
        for code, fc in sorted(codes_items):
            if filter_text:
                haystack = f"{code} {fc.name} {fc.layer} {fc.symbol}".casefold()
                if filter_text not in haystack:
                    continue
            items.append((code, fc))

        self.table_codes.setRowCount(len(items))
        for r, (code, fc) in enumerate(items):
            kind_str = "Point" if fc.kind == "point" else "Line" if fc.kind == "line" else "Polygon"
            self.table_codes.setItem(r, 0, QTableWidgetItem(code))
            self.table_codes.setItem(r, 1, QTableWidgetItem(fc.name))
            self.table_codes.setItem(r, 2, QTableWidgetItem(kind_str))
            self.table_codes.setItem(r, 3, QTableWidgetItem(fc.layer))
            self.table_codes.setItem(r, 4, QTableWidgetItem(fc.symbol))

        self.table_codes.resizeColumnsToContents()
        min_widths = [110, 240, 90, 220, 130]
        for c, min_w in enumerate(min_widths):
            if self.table_codes.columnWidth(c) < min_w:
                self.table_codes.setColumnWidth(c, min_w)

        self.lbl_count.setText(f"{len(items):,} of {len(codes_items):,} code(s)")

    def _apply_filter(self):
        self._fill_codes()

    def _fill_commands(self):
        cmds = command_map(self.project.settings.get("f2f_commands"))
        labels = f2f.DEFAULT_COMMAND_LABELS
        self.table_cmds.setRowCount(len(labels))
        for r, meaning in enumerate(labels):
            cmd = str(cmds[COMMAND_MEANINGS[r]])
            self.table_cmds.setItem(r, 0, QTableWidgetItem(meaning))
            self.table_cmds.setItem(r, 1, QTableWidgetItem(cmd))
        self.table_cmds.resizeColumnsToContents()
        min_widths = [320, 160]
        for c, min_w in enumerate(min_widths):
            if self.table_cmds.columnWidth(c) < min_w:
                self.table_cmds.setColumnWidth(c, min_w)

    def _fill_rules(self):
        rules = self.project.settings.get("f2f_rules") or []
        self.table_rules.setRowCount(len(rules))
        for r, rule in enumerate(rules):
            err = str(rule[0]) if len(rule) > 0 else ""
            fix = str(rule[1]) if len(rule) > 1 else ""
            self.table_rules.setItem(r, 0, QTableWidgetItem(err))
            self.table_rules.setItem(r, 1, QTableWidgetItem(fix))
        self.table_rules.resizeColumnsToContents()
        min_widths = [260, 260]
        for c, min_w in enumerate(min_widths):
            if self.table_rules.columnWidth(c) < min_w:
                self.table_rules.setColumnWidth(c, min_w)

    def _edit_commands(self):
        current = self.project.settings.get("f2f_commands") or list(f2f.DEFAULT_COMMANDS)
        dlg = CodeCommandsDialog(current, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cmds = dlg.get_commands()
            with self.state.edit("Edit Code Commands"):
                self.project.settings["f2f_commands"] = new_cmds
            self._fill_commands()
            # If an active .fwb exists, update it too
            fb_path = self.project.settings.get("fieldbook_file")
            if fb_path and Path(fb_path).exists():
                try:
                    self._save_extra_to_fwb(fb_path)
                except Exception:
                    pass

    def _edit_rules(self):
        fb_path = self.project.settings.get("fieldbook_file") or ""
        code_set = set()
        if fb_path and Path(fb_path).exists():
            try:
                from ..fieldwork.io_carlson import read_fwb_file
                _headers, rows = read_fwb_file(Path(fb_path))
                code_set = {str(row[0]).strip() for row in rows or []
                            if row and str(row[0]).strip()}
            except Exception:
                code_set = set()
        current = self.project.settings.get("f2f_rules") or []
        dlg = CorrectionRulesDialog(
            current, fieldbook_codes=code_set, parent=self,
            commands=self.project.settings.get("f2f_commands"))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_rules = dlg.get_rules()
            with self.state.edit("Edit Correction Rules"):
                self.project.settings["f2f_rules"] = new_rules
            self._fill_rules()
            # If an active .fwb exists, update it too
            fb_path = self.project.settings.get("fieldbook_file")
            if fb_path and Path(fb_path).exists():
                try:
                    self._save_extra_to_fwb(fb_path)
                except Exception:
                    pass

    def _save_extra_to_fwb(self, fwb_path: Path | str):
        path = Path(fwb_path)
        if not path.exists():
            return False
        return f2f.write_fwb_extra(
            path,
            commands=self.project.settings.get("f2f_commands") or list(f2f.DEFAULT_COMMANDS),
            rules=self.project.settings.get("f2f_rules") or [],
        )

    def action_convert(self):
        """Convert a Carlson F2F CSV or custom code table -> choose name & location -> copy & archive old."""
        pr = self.project
        start = pr.settings.get("f2f_path") or (os.path.dirname(pr.path) if pr.path else "") or ""
        path, _ = QFileDialog.getOpenFileName(self, "Convert Field to Finish", start,
                                              "Code Table (*.csv *.txt);;All Files (*)")
        if not path:
            return
        try:
            table = f2f.read(path)
        except Exception as ex:
            error_box(self, "Convert Field Book", str(ex))
            return

        dlg = ConvertFieldToFinishDialog(table, pr, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        mapping, only, mode, commands, rules = dlg.choices()

        # Allow user to name and place this field book
        job_dir = Path(pr.path).parent if pr.path and Path(pr.path).is_file() else (Path(pr.path) if pr.path else Path("."))
        source_stem = Path(path).stem.strip() or pr.name or "New Job"
        default_dest = job_dir / "Field Book" / f"{source_stem}.fwb"
        dest_path_str, _ = QFileDialog.getSaveFileName(self, "Save Converted Field Book (.fwb)",
                                                       str(default_dest),
                                                       "Field Book (*.fwb);;All Files (*)")
        if not dest_path_str:
            return

        dest_path = Path(dest_path_str)

        try:
            new_codes, stats = f2f.convert(table, mapping, only=only,
                                           existing=pr.codes if mode == "merge" else None)
        except Exception as ex:
            error_box(self, "Convert Field Book", str(ex), traceback.format_exc())
            return

        # Write converted .fwb to chosen destination
        f2f.write_fwb(dest_path, table, mapping, only=only, commands=commands, rules=rules)

        # Place a copy in the expected project Field Book directory with old versions archived
        project_fwb = place_fieldbook_in_project(dest_path, pr)

        with self.state.edit("Convert Field Book"):
            pr.codes = new_codes
            pr.settings["f2f_path"] = str(path)
            pr.settings["fieldbook_file"] = str(project_fwb)
            pr.settings["f2f_commands"] = commands
            pr.settings["f2f_rules"] = rules
            from ..core.layer_definitions import populate_layers_from_fieldbook
            populate_layers_from_fieldbook(pr, new_codes)

        self.state.log(f"Field to Finish: {f2f.describe_stats(stats)} "
                       f"({os.path.basename(path)}; {mapping.found_by}) "
                       f"— Saved to {dest_path.name} and copied to project Field Book.", "ok")
        self._refresh()

    def action_select(self):
        """Pull an already converted field book from project or external location -> archive old and copy to project."""
        pr = self.project
        job_dir = Path(pr.path).parent if pr.path and Path(pr.path).is_file() else (Path(pr.path) if pr.path else Path("."))
        start_dir = str(job_dir / "Field Book" if (job_dir / "Field Book").exists() else job_dir)

        path, _ = QFileDialog.getOpenFileName(self, "Select Field Book", start_dir,
                                              "Field Book (*.fwb);;Code Table (*.csv);;All Files (*)")
        if not path:
            return

        src_path = Path(path)
        try:
            # Place in project Field Book directory, archiving previous versions
            project_fwb = place_fieldbook_in_project(src_path, pr)

            table = f2f.read(project_fwb)
            new_codes, stats = f2f.convert(table)
            extra = f2f.read_fwb_extra(project_fwb)
            commands = extra.get("commands", list(f2f.DEFAULT_COMMANDS))
            rules = extra.get("rules", [])

            with self.state.edit("Select Field Book"):
                pr.codes = new_codes
                pr.settings["fieldbook_file"] = str(project_fwb)
                pr.settings["f2f_commands"] = commands
                pr.settings["f2f_rules"] = rules
                from ..core.layer_definitions import populate_layers_from_fieldbook
                populate_layers_from_fieldbook(pr, new_codes)

            self.state.log(f"Field Book loaded: {src_path.name} ({len(new_codes):,} codes) "
                           f"— active in project Field Book.", "ok")
            self._refresh()
        except Exception as ex:
            error_box(self, "Select Field Book", str(ex), traceback.format_exc())

    def action_report(self):
        """Open comprehensive Field Book report dialog."""
        FieldBookReportDialog(self.project, self).exec()
