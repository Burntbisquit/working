"""The Check Fieldwork dock - the field-data checks, in the drawing window.

Two different questions get asked about a job, and they need two different tools:

* **Is this download right?** - the same point number twice, numbers that look like each other,
  two shots on the same spot, codes the office's field book does not have.  That is Fieldwork
  Manager's question, and it is asked *before* anything is imported: **Survey > Fieldwork
  Manager**.
* **Is what I am drawing right?** - the same checks over the points that are actually in this
  project, after the import renumbered them, after somebody edited a description, dropped a pin
  or merged a second download in.  That is this dock: *View > Panels > Check Fieldwork*.

It runs the project's point-error checks through the shared bridge and description parser. Whole-line
completeness and geometry-sequence checks are kept in the separate Fix Linework workbench and its
independent cache. The point check still includes **Line command order**, which is deliberately
routed to Fix Point Errors. What this dock adds is reach: click a finding to select those points in
the drawing, double-click to zoom to them, and the Points list's Source File / Parent Folder columns
say which download and which crew each one came from.

**A vocabulary is what makes the description check a check.**  Without the office's own codes the
parser would call every code unknown - a hundred false alarms, which is how a check gets switched
off and stays off.  So this dock runs what it can with what it has, and says which of the two it
did:

* a **field book** (``.fwb``) - the one the job remembers, or the first one in the job folder's
  ``Field Book`` folder: point and description checks run;
* the **job's own feature codes** - when they came from the office's Carlson table (*Survey >
  Convert Field to Finish*), that table is the vocabulary for point and description checks;
* **neither** - number checks and blank-description checks run, while vocabulary-dependent checks
  wait; the dock says so and offers both ways out.

**Line command order** is a description-level point finding and stays in Fix Point Errors. Whole-line
completeness and geometry-sequence checks are not run or cached here; open **Fix Linework** for that
separate check path. That separation prevents a point-check refresh from silently running a line
check or replacing its cached results.

There are no tolerances to set here on purpose: the checks use the same tolerances the field
window uses, because a check that finds something in one window and not the other is worse than
no check at all.
"""
from __future__ import annotations

import difflib
import math
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QRadioButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import provenance as PROV
from ..core import point_linework_coder as PLC
from ..core.featurecodes import parse_description
from ..core.fieldbook_syntax import command_map
from ..core.settings import settings
from .widgets import Banner, Hint

COLS = ("Level", "Check", "Finding", "Points", "Action")
COLORS = {"error": "#ff6b6b", "warn": "#f0b429", "info": "#8a94a1"}


def auto_fix_descriptions(state, code_set: set) -> int:
    """Batch auto-repair common safe description issues across the project.

    Fixes:
    - Lowercase/mixed-case code prefixes matching code table in uppercase
    - Code joined with parameter without space (e.g. IPF1/2 -> IPF 1/2 if IPF is in code table)
    - Trailing commas, semicolons, or periods on descriptions
    Returns the count of points updated.
    """
    pr = state.project
    if not code_set or not pr.points:
        return 0

    upper_codes = {str(c).upper(): str(c) for c in code_set}
    fixed_count = 0
    with state.edit("Auto-Fix Descriptions", kinds=("points",)):
        for p in pr.points.values():
            if not p.desc:
                continue
            orig = str(p.desc)
            clean = orig.strip().rstrip(",;.")
            tokens = clean.split()
            if not tokens:
                continue
            head = tokens[0]
            rest = " ".join(tokens[1:])

            # Case 1: Exact case mismatch (e.g., 'ipf' vs 'IPF')
            if head.upper() in upper_codes and head != upper_codes[head.upper()]:
                head = upper_codes[head.upper()]
                new_desc = f"{head} {rest}".strip()
                if new_desc != orig:
                    p.desc = new_desc
                    fixed_count += 1
                continue

            # Case 2: Code joined with size/number (e.g. IPF1/2 -> IPF 1/2, REBAR5/8 -> REBAR 5/8)
            matched_code = None
            for c in sorted(upper_codes.keys(), key=len, reverse=True):
                if head.upper().startswith(c) and len(head) > len(c):
                    remainder = head[len(c):]
                    if remainder[0].isdigit() or remainder[0] in ("/", "-", "#"):
                        matched_code = upper_codes[c]
                        head_fixed = f"{matched_code} {remainder}"
                        new_desc = f"{head_fixed} {rest}".strip()
                        if new_desc != orig:
                            p.desc = new_desc
                            fixed_count += 1
                        break
            if matched_code:
                continue

            # Case 3: Trailing punctuation cleaned
            if clean != orig:
                p.desc = clean
                fixed_count += 1

        if fixed_count:
            pr.touch()
    return fixed_count


class DuplicateResolveDialog(QDialog):
    """Interactive resolution dialog for duplicate point numbers or coordinate conflicts."""

    def __init__(self, state, point_ids: list[int], parent=None):
        super().__init__(parent)
        self.state = state
        self.point_ids = point_ids
        self.setWindowTitle("Resolve Duplicate Points")
        self.resize(700, 420)
        lay = QVBoxLayout(self)

        pr = state.project
        self.points = [pr.points[pid] for pid in point_ids if pid in pr.points]

        lay.addWidget(QLabel(f"<b>{len(self.points)} Duplicate / Conflicting Point(s) Found:</b>"))

        self.tbl = QTableWidget(len(self.points), 5)
        ne = settings().get("coord_order") == "NE"
        if ne:
            self.tbl.setHorizontalHeaderLabels(["Pt #", "Northing", "Easting", "Elevation", "Description"])
        else:
            self.tbl.setHorizontalHeaderLabels(["Pt #", "Easting", "Northing", "Elevation", "Description"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)

        for r, p in enumerate(self.points):
            self.tbl.setItem(r, 0, QTableWidgetItem(str(p.number)))
            if ne:
                self.tbl.setItem(r, 1, QTableWidgetItem(f"{p.y:,.3f}"))
                self.tbl.setItem(r, 2, QTableWidgetItem(f"{p.x:,.3f}"))
            else:
                self.tbl.setItem(r, 1, QTableWidgetItem(f"{p.x:,.3f}"))
                self.tbl.setItem(r, 2, QTableWidgetItem(f"{p.y:,.3f}"))
            self.tbl.setItem(r, 3, QTableWidgetItem("" if math.isnan(p.z) else f"{p.z:,.3f}"))
            self.tbl.setItem(r, 4, QTableWidgetItem(str(p.desc)))

        self.tbl.resizeColumnsToContents()
        lay.addWidget(self.tbl, 1)

        grp = QGroupBox("Resolution Strategy")
        glay = QVBoxLayout(grp)
        self.rb_renumber = QRadioButton("Renumber duplicates with new unique numbers (1000+)")
        self.rb_renumber.setChecked(True)
        self.rb_keep_first = QRadioButton("Keep first point, delete duplicate(s)")
        self.rb_keep_last = QRadioButton("Keep last/latest point, delete earlier point(s)")
        self.rb_average = QRadioButton("Average coordinates into single point")
        glay.addWidget(self.rb_renumber)
        glay.addWidget(self.rb_keep_first)
        glay.addWidget(self.rb_keep_last)
        glay.addWidget(self.rb_average)
        lay.addWidget(grp)

        row = QHBoxLayout()
        row.addStretch(1)
        b_apply = QPushButton("Apply Resolution")
        b_apply.setProperty("accent", True)
        b_apply.clicked.connect(self._apply)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        row.addWidget(b_apply)
        row.addWidget(b_cancel)
        lay.addLayout(row)

    def _apply(self):
        pr = self.state.project
        with self.state.edit("Resolve Duplicate Points", kinds=("points",)):
            if self.rb_renumber.isChecked():
                existing_nums = set()
                max_int = 0
                for p in pr.points.values():
                    existing_nums.add(str(p.number))
                    try:
                        num_val = int(p.number)
                        if num_val > max_int:
                            max_int = num_val
                    except ValueError:
                        pass
                next_num = max(1000, max_int + 1)
                for p in self.points[1:]:
                    while str(next_num) in existing_nums:
                        next_num += 1
                    p.number = str(next_num)
                    existing_nums.add(str(next_num))
                    next_num += 1
            elif self.rb_keep_first.isChecked():
                for p in self.points[1:]:
                    if p.id in pr.points:
                        del pr.points[p.id]
            elif self.rb_keep_last.isChecked():
                for p in self.points[:-1]:
                    if p.id in pr.points:
                        del pr.points[p.id]
            elif self.rb_average.isChecked():
                avg_x = sum(p.x for p in self.points) / len(self.points)
                avg_y = sum(p.y for p in self.points) / len(self.points)
                valid_zs = [p.z for p in self.points if not math.isnan(p.z)]
                avg_z = sum(valid_zs) / len(valid_zs) if valid_zs else math.nan
                p0 = self.points[0]
                p0.x = avg_x
                p0.y = avg_y
                p0.z = avg_z
                for p in self.points[1:]:
                    if p.id in pr.points:
                        del pr.points[p.id]
            pr.touch()
        self.accept()


class DescriptionFixDialog(QDialog):
    """Interactive resolution dialog for fixing unknown codes or descriptions."""

    def __init__(self, state, point_ids: list[int], vocabulary: set | None = None, parent=None):
        super().__init__(parent)
        self.state = state
        self.point_ids = point_ids
        self.vocabulary = vocabulary or set()
        self.setWindowTitle("Fix Unknown Descriptions & Feature Codes")
        self.resize(780, 460)
        lay = QVBoxLayout(self)

        pr = state.project
        self.points = [pr.points[pid] for pid in point_ids if pid in pr.points]

        lay.addWidget(QLabel(f"<b>Fix Descriptions for {len(self.points)} Point(s):</b>"))

        self.tbl = QTableWidget(len(self.points), 5)
        self.tbl.setHorizontalHeaderLabels(["Pt #", "Current Description", "Extracted Code", "Suggested Fix", "New Description"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)

        vocab_list = sorted(list(self.vocabulary))
        self.edits = []

        for r, p in enumerate(self.points):
            self.tbl.setItem(r, 0, QTableWidgetItem(str(p.number)))
            self.tbl.setItem(r, 1, QTableWidgetItem(str(p.desc)))

            tokens = (p.desc or "").split()
            raw_code = tokens[0].upper() if tokens else ""
            self.tbl.setItem(r, 2, QTableWidgetItem(raw_code))

            matches = difflib.get_close_matches(raw_code, vocab_list, n=1, cutoff=0.4) if vocab_list else []
            suggested = matches[0] if matches else raw_code
            self.tbl.setItem(r, 3, QTableWidgetItem(suggested))

            rest = " ".join(tokens[1:]) if len(tokens) > 1 else ""
            proposed = f"{suggested} {rest}".strip() if suggested else p.desc
            ed = QLineEdit(proposed)
            self.tbl.setCellWidget(r, 4, ed)
            self.edits.append((p, ed, suggested, rest))

        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        lay.addWidget(self.tbl, 1)

        btn_action_row = QHBoxLayout()
        b_use_suggested = QPushButton("Use All Suggested Codes")
        b_use_suggested.clicked.connect(self._apply_all_suggested)
        b_upper = QPushButton("Uppercase All Descriptions")
        b_upper.clicked.connect(self._apply_uppercase)
        btn_action_row.addWidget(b_use_suggested)
        btn_action_row.addWidget(b_upper)
        btn_action_row.addStretch(1)
        lay.addLayout(btn_action_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        b_apply = QPushButton("Apply Description Fixes")
        b_apply.setProperty("accent", True)
        b_apply.clicked.connect(self._apply)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btn_row.addWidget(b_apply)
        btn_row.addWidget(b_cancel)
        lay.addLayout(btn_row)

    def _apply_all_suggested(self):
        for p, ed, suggested, rest in self.edits:
            if suggested:
                ed.setText(f"{suggested} {rest}".strip())

    def _apply_uppercase(self):
        for p, ed, _, _ in self.edits:
            ed.setText(ed.text().upper())

    def _apply(self):
        pr = self.state.project
        with self.state.edit("Fix Descriptions", kinds=("points",)):
            for p, ed, _, _ in self.edits:
                new_desc = ed.text().strip()
                if new_desc != p.desc:
                    p.desc = new_desc
            pr.touch()
        self.accept()


class LineRepairDialog(QDialog):
    """Interactive resolution dialog for repairing linework sequences."""

    def __init__(self, state, point_ids: list[int], parent=None, commands=None):
        super().__init__(parent)
        self.state = state
        self.point_ids = point_ids
        self.setWindowTitle("Repair Linework String")
        self.resize(750, 420)
        lay = QVBoxLayout(self)

        pr = state.project
        self.points = [pr.points[pid] for pid in point_ids if pid in pr.points]
        self.commands = (commands if commands is not None
                         else (pr.settings or {}).get("f2f_commands"))
        semantic_tokens = command_map(self.commands)
        known_codes = getattr(pr.codes, "codes", {})

        lay.addWidget(QLabel(f"<b>Linework Sequence with {len(self.points)} Point(s):</b>"))

        self.tbl = QTableWidget(len(self.points), 4)
        self.tbl.setHorizontalHeaderLabels(["Pt #", "Current Description", "Suggested Action", "Fixed Description"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)

        self.edits = []
        n_pts = len(self.points)
        from copy import copy

        def has_meaning(parsed, meaning):
            token = semantic_tokens.get(meaning, "")
            return bool(token and any(flag.casefold() == token.casefold() for flag in parsed.flags))

        for r, p in enumerate(self.points):
            self.tbl.setItem(r, 0, QTableWidgetItem(str(p.number)))
            self.tbl.setItem(r, 1, QTableWidgetItem(str(p.desc)))

            fixed_desc = p.desc or ""
            parsed = parse_description(fixed_desc, commands=self.commands, known_codes=known_codes)
            if parsed.code not in known_codes:
                action_text = "Pass through (no Field Book code resolved)"
            elif r == 0 and not has_meaning(parsed, "start_line") and semantic_tokens.get("start_line"):
                suggestion = copy(p)
                PLC.start_string_coding(suggestion, parsed.code, self.commands)
                fixed_desc = suggestion.desc
                action_text = "Start line (add Start Line command)"
            elif (r == n_pts - 1 and not has_meaning(parsed, "end_line")
                  and not has_meaning(parsed, "close") and semantic_tokens.get("end_line")):
                suggestion = copy(p)
                PLC.open_string_coding(suggestion, parsed.code, self.commands)
                fixed_desc = suggestion.desc
                action_text = "End line (add End Line command)"
            else:
                action_text = "Pass through"

            self.tbl.setItem(r, 2, QTableWidgetItem(action_text))
            ed = QLineEdit(fixed_desc)
            self.tbl.setCellWidget(r, 3, ed)
            self.edits.append((p, ed))

        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        lay.addWidget(self.tbl, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        b_apply = QPushButton("Apply Linework Fix")
        b_apply.setProperty("accent", True)
        b_apply.clicked.connect(self._apply)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btn_row.addWidget(b_apply)
        btn_row.addWidget(b_cancel)
        lay.addLayout(btn_row)

    def _apply(self):
        pr = self.state.project
        with self.state.edit("Repair Linework String", kinds=("points", "entities")):
            for p, ed in self.edits:
                new_desc = ed.text().strip()
                if new_desc != p.desc:
                    p.desc = new_desc
            pr.touch()
        self.state.log("Line coding updated. Use Process Linework when you are ready to rebuild derived lines.",
                       "info")
        self.accept()


def fieldbook_in(job_root) -> Path | None:
    """The first field book in a job folder's ``Field Book`` directory, if there is one.

    The field window picks its field book the same way (first ``*.fwb``, by name), so a job that
    works there works here.
    """
    if not job_root:
        return None
    book_dir = Path(job_root) / "Field Book"
    if book_dir.is_dir():
        books = sorted(book_dir.glob("*.fwb"))
        if books:
            return books[0]
    return None


def codes_from_fieldbook(path) -> set:
    """The code vocabulary of a field book (``.fwb``) or a Carlson F2F table (``.csv``)."""
    from ..fieldwork import parse as P

    p = Path(str(path))
    if p.suffix.lower() == ".csv":
        from ..fieldwork import bridge as FB
        return FB.f2f_code_set(p)
    return P.build_f2f_set_from_fieldbook(p)


class CheckFieldworkDock(QWidget):
    """Run the field-data checks over this project's points, and show what they found."""

    def __init__(self, state, job_root=None, on_convert=None, parent=None):
        super().__init__(parent)
        self.state = state
        self._job_root = job_root                # a callable () -> Path | None, or a path
        self._on_convert = on_convert            # the window's Convert Field to Finish command
        self._alerted = False
        self._stale = False
        self.result = None
        self.code_set: set = set()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.banner = Banner("", "info")
        lay.addWidget(self.banner)

        row = QHBoxLayout()
        self.btn_workbench = QPushButton("Open QA Workbench...")
        self.btn_workbench.setProperty("accent", True)
        self.btn_workbench.setToolTip("Open full-screen split QA Workbench with 2D plan, 3D elevation view, and separated Point/Linework tabs")
        self.btn_run = QPushButton("Run the Check")
        self.btn_run.setProperty("accent", True)
        self.btn_autofix = QPushButton("Auto-Fix Codes")
        self.btn_autofix.setToolTip("Batch repair safe description casing and spacing typos")
        self.btn_book = QPushButton("Field Book...")
        self.btn_book.setToolTip("Point this job at an existing field book (.fwb) - it is "
                                 "remembered on the job")
        self.btn_conv = QPushButton("Convert Field to Finish...")
        self.btn_conv.setToolTip("Build a code table from the office's Carlson Field-to-Finish "
                                 "export (Survey > Convert Field to Finish)")
        row.addWidget(self.btn_workbench)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_autofix)
        row.addWidget(self.btn_book)
        row.addWidget(self.btn_conv)
        row.addStretch(1)
        self.lbl_when = QLabel("")
        row.addWidget(self.lbl_when)
        lay.addLayout(row)

        self.tbl = QTableWidget(0, len(COLS))
        self.tbl.setHorizontalHeaderLabels(list(COLS))
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.verticalHeader().setVisible(False)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        lay.addWidget(self.tbl, 1)

        lay.addWidget(Hint("Click a finding to select those points in the drawing; double-click to "
                           "zoom to them. The point checks use the fieldwork tolerances. Line command "
                           "order is reviewed in Fix Point Errors; other whole-line findings have a "
                           "separate run/cache in Fix Linework. The drawing-wide checks stay on "
                           "Survey > Data Quality Check."))

        self.btn_save = QPushButton("Save Report...")
        self.btn_save.setToolTip("Write this check out as a .fwc - the same file the field window "
                                 "reads.  A run is already stored in the project, and written into "
                                 "the job's Reports folder when the job has one.")
        row.addWidget(self.btn_save)
        self.btn_workbench.clicked.connect(self.open_workbench)
        self.btn_run.clicked.connect(lambda *_: self.run(alert=True, force=True))
        self.btn_autofix.clicked.connect(self.auto_fix)
        self.btn_save.clicked.connect(self.save_report)
        self.btn_book.clicked.connect(self.choose_field_book)
        self.btn_conv.clicked.connect(self.build_code_table)
        self.tbl.itemSelectionChanged.connect(self._select)
        self.tbl.cellDoubleClicked.connect(self._zoom)
        state.project_replaced.connect(self._project_changed)
        state.changed.connect(self._changed)
        self.refresh_banner()
        # Reopening a project reopens its last check (item 13): the run is stored on the project,
        # so it is there before anything is run again - and if there is none, run one.
        if not self.show_stored():
            # Running the check is read-only, and this one is not the user's doing: opening a
            # project must leave it clean and the undo stack empty, so the automatic run does not
            # record itself.  Pressing "Run the Check" does.
            self.run(store=False)

    def open_workbench(self):
        """Open the full-screen split QA Workbench (2D top view + 3D elevation view + categorized error tables)."""
        from .qa_workspace import QAWorkspaceDialog
        dlg = QAWorkspaceDialog(self.state, self)
        dlg.exec()
        self.run(store=False)

    def save_report(self):
        """Copy this run out as a .fwc wherever the user wants it."""
        from ..fieldwork import bridge as FB
        if self.result is None:
            return
        default = self._last_report_file if getattr(self, "_last_report_file", "") else ""
        start = default or str(Path(self.job_root() or Path.home()) / "check.fwc")
        path, _ = QFileDialog.getSaveFileName(self, "Save the Check Report", start,
                                              "Check report (*.fwc);;All files (*)")
        if not path:
            return
        rows = FB.report_rows(self.state.project, self.result)
        if FB.write_report(path, rows):
            self.state.log(f"Check report written to {path} ({len(rows)} row(s)).", "ok")
            self.lbl_when.setText(self.lbl_when.text() + f"  |  saved {Path(path).name}")
        else:
            QMessageBox.warning(self, "Save the Check Report", f"Could not write {path}.")

    # --------------------------------------------------------------------- the field book
    def job_root(self):
        """The job folder this window is working in, if it has one."""
        root = self._job_root
        try:
            return root() if callable(root) else root
        except Exception:
            return None

    def fieldbook(self) -> Path | None:
        """The field book this job is using: the one it remembers, else the job folder's.

        Uses the same search as the vocabulary itself (``bridge.fieldbooks_in``: any ``*.fwb``
        under the job folder, the job's own name first), so the book the dock *says* it will use
        and the book the check actually reads cannot be two different files.  An office book in
        ``Source/`` or beside the download in ``Field Data/`` counts.
        """
        chosen = (self.state.project.settings or {}).get("fieldbook_file")
        if chosen:
            path = Path(chosen).expanduser()
            if not path.is_absolute():
                root = self.job_root()
                if root is None and getattr(self.state.project, "path", None):
                    root = Path(self.state.project.path).expanduser().parent
                if root is not None:
                    path = Path(root) / path
            if path.is_file():
                return path
        from ..fieldwork import bridge as FB
        books = FB.fieldbooks_in(self.job_root())
        return books[0] if books else fieldbook_in(self.job_root())

    def vocabulary(self) -> dict:
        """What the description check runs against - one resolver, in ``fieldwork.bridge``.

        It looks for the job's field book (the one the job remembers, else every ``*.fwb`` under
        the job folder, the job's own name first), falls back to the job's own feature codes, and
        says which of those it did.  A field book *with no code table* is not a vocabulary: a
        fresh job's template book is exactly that, and using its empty table would report every
        code as unknown.
        """
        from ..fieldwork import bridge as FB
        return FB.vocabulary_for(self.state.project, job_root=self.job_root(),
                                 fieldbook=self.fieldbook())

    def refresh_banner(self):
        """Say what the checks will be able to do, before anything is run."""
        v = self.vocabulary()
        self.code_set = set(v["codes"])
        self._vocabulary = v
        n = len(self.code_set)
        if v["source"] == "none":
            head = (f"{v['why']}.  " if v.get("why") else "No field book.  ")
            self.banner.set(head + "Number/position and blank-description checks run; vocabulary-based "
                            "description and Line command order checks are paused. Whole-line checks "
                            "are separate in Fix Linework. Point the job at a field book (Field Book...), "
                            "or build a code table from the office's Carlson export (Convert Field to "
                            "Finish...).", "warn")
            return
        if v["source"] == "field book":
            self.banner.set(f"Field book: {v['label']} - {n:,} codes. Point-error checks are active; "
                            "whole-line checks run separately in Fix Linework.", "info")
        elif v["source"] == "field book + job codes":
            self.banner.set(f"Field book {v['label']} holds no code table, so this job's own "
                            f"{n:,} feature codes are used as well. Point-error checks are active; "
                            "whole-line checks run separately in Fix Linework.", "info")
        else:
            self.banner.set(f"No field book, but this job's {n:,} feature codes are the vocabulary "
                            f"({v['label']}). Point-error checks are active; whole-line checks run "
                            "separately in Fix Linework.", "info")

    def choose_field_book(self, _checked=False, *, run_check: bool = True):
        from .fieldbook_dialog import FieldBookDialog
        dlg = FieldBookDialog(self.state, self)
        dlg.exec()
        self.refresh_banner()
        if run_check:
            self.run()

    def build_code_table(self, _checked=False, *, run_check: bool = True):
        """Hand off to the window's own Convert Field to Finish, then re-check with its codes."""
        from .fieldbook_dialog import FieldBookDialog
        dlg = FieldBookDialog(self.state, self)
        dlg.action_convert()
        self.refresh_banner()
        if run_check:
            self.run()

    def alert_if_no_fieldbook(self):
        """Say it once, in a box, when a check is run on a job without a vocabulary.

        The banner says it every time the dock is looked at; this is the one that offers to *do*
        something about it, because a check that quietly cannot run is the failure this alert
        exists to prevent.  A second press of the button does not ask again.
        """
        if self._alerted or self.code_set:
            return
        if self._no_field_points():
            return                          # nothing to check yet - say nothing
        self._alerted = True
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Warning)
        m.setWindowTitle("Field Book Needed")
        m.setText("No field book is loaded for this job, so the description check cannot run.\n\n"
                  "The number and position checks will run with or without it.")
        m.setInformativeText("Pick an existing .fwb now, or build a code table from the office's "
                             "Carlson Field-to-Finish export?")
        b_pick = m.addButton("Pick a Field Book...", QMessageBox.AcceptRole)
        b_conv = m.addButton("Convert Field to Finish...", QMessageBox.ActionRole)
        b_later = m.addButton("Run the Other Checks", QMessageBox.RejectRole)
        m.setDefaultButton(b_pick)
        m.exec()
        if m.clickedButton() is b_pick:
            self.choose_field_book(run_check=False)
        elif m.clickedButton() is b_conv:
            self.build_code_table(run_check=False)
        elif m.clickedButton() is b_later:
            pass  # The outer run() continues with the checks that do not need a code table.

    def _no_field_points(self) -> bool:
        from ..fieldwork import bridge as FB
        try:
            return not FB.working_rows_from_project(self.state.project)[0]
        except Exception:
            return True

    # --------------------------------------------------------------------- the check
    def _project_changed(self):
        self._alerted = False
        self.refresh_banner()
        if not self.show_stored():
            self.run(store=False)

    def _changed(self, kinds=None):
        """Flag the point run stale only when point-check inputs changed."""
        changed_kinds = set(kinds or {"all"})
        if not changed_kinds.intersection({"points", "all"}):
            return
        if self.result is None or self._stale:
            return
        self._stale = True
        self.btn_run.setText("Update Check *")
        self.btn_run.setToolTip("QA inputs changed since this check. Click to compute a fresh check "
                                "and update the cache.")
        current = self.lbl_when.text().split("  |  Inputs changed", 1)[0]
        self.lbl_when.setText(current + "  |  Inputs changed; update the check.")

    def run(self, alert: bool = False, store: bool = True, force: bool = False):
        """Run the field-data checks and show the findings.

        *alert* is set from the Run button: with no vocabulary loaded the check can only do half
        its job, and that is the moment to say so in a box rather than in a strip. *force* is for an
        explicit rerun; automatic refreshes reuse a result only when the complete QA-input
        signature still matches. *store* is False for a run nobody asked for (the one that fills
        the dock when a project is opened), because opening a window must not dirty the project.
        """
        from ..fieldwork import bridge as FB

        self.refresh_banner()
        if alert:
            self.alert_if_no_fieldbook()
        book = self.fieldbook()
        v = getattr(self, "_vocabulary", None) or self.vocabulary()
        try:
            self.result, cache_status, input_signature = FB.check_project_cached(
                self.state.project, f2f=self.code_set,
                fieldbook_path=book or (v.get("path") or None), force=force)
        except Exception as ex:                          # a check must never break the window
            self.banner.set(f"The check could not run: {ex}", "bad")
            self.state.log(f"Check fieldwork could not run: {ex}", "warn")
            return
        # The run is written into the project (item 13): reopen the project and the check is
        # there, with what it found, when it ran and which vocabulary it used.  It goes inside an
        # edit so it is undoable and so the project knows it has changed.
        if store:
            self._store(v, input_signature)
        self._stale = False
        self.btn_run.setText("Run the Check")
        self.btn_run.setToolTip("Force a fresh check now; automatic refreshes reuse results only "
                                "when QA inputs are unchanged.")
        self._fill_table()
        findings = self.result["findings"]
        stats = self.result["stats"] or {}
        points = stats.get("rows", 0)
        crews = stats.get("crews") or []
        cache_note = {"hit": "cached inputs unchanged", "updated": "refreshed after input changes",
                      "rerun": "freshly recomputed"}.get(
                          cache_status, "freshly computed" if force else "")
        self.lbl_when.setText(f"{points:,} point(s) in the check"
                              + (f", crews {', '.join(str(c) for c in crews)}" if crews else "")
                              + (f"  |  {cache_note}" if cache_note else ""))
        errors = sum(1 for f in findings if f["level"] == "error")
        warns = sum(1 for f in findings if f["level"] == "warn")
        self.state.log(f"Check fieldwork: {points:,} field point(s), {len(findings)} finding(s)"
                       + (f" ({errors} error, {warns} warning)" if errors or warns else "")
                       + ".  " + PROV.summary(self.state.project), "warn" if errors else "ok")

    # --------------------------------------------------------------------- the audit trail
    def _store(self, vocabulary: dict, input_signature: str | None = None):
        """Save this run, its verified inputs and the same report beside the job when available."""
        from ..fieldwork import bridge as FB
        try:
            with self.state.edit("Run the Check"):
                payload = FB.store_report(self.state.project, self.result, vocabulary=vocabulary,
                                          job_root=self.job_root(), input_signature=input_signature)
                path = None
                root = self.job_root()
                if root:
                    reports = Path(root) / "Reports"
                    reports.mkdir(parents=True, exist_ok=True)
                    stamp = payload["when"].replace(":", "").replace("-", "").replace(" ", "-")
                    path = reports / f"Check Fieldwork {stamp}.fwc"
                    rows = FB.report_rows(self.state.project, self.result)
                    if not FB.write_report(path, rows):
                        path = None
                payload["file"] = str(path) if path else ""
        except Exception as ex:                          # never let the trail break the check
            self.state.log(f"The check ran, but its report could not be saved: {ex}", "warn")
            return
        self._last_report_file = payload.get("file") or ""
        self.state.log(f"Check written to the project ({payload['points']:,} point(s), "
                       f"{len(payload['findings'])} finding(s), vocabulary: {payload['vocabulary']}"
                       + (f", {Path(self._last_report_file).name}" if self._last_report_file else "")
                       + ").", "info")

    def stored(self) -> dict | None:
        from ..fieldwork import bridge as FB
        return FB.stored_report(self.state.project)

    def show_stored(self) -> bool:
        """Show the check the project is carrying, if any.  True when something was shown.

        This is what makes a check auditable after the project is closed and reopened: the last run
        comes back with the project, in the same table, with its point numbers, and the strip says
        when it ran and what it ran against - so nobody has to take "I ran it" on trust.
        """
        payload = self.stored()
        if not payload:
            return False
        from ..fieldwork import bridge as FB
        rows, ids = FB.working_rows_from_project(self.state.project)
        vocabulary = getattr(self, "_vocabulary", None) or self.vocabulary()
        book = self.fieldbook()
        saved_signature = payload.get("input_signature")
        try:
            current_signature = FB.check_input_signature(
                self.state.project, f2f=self.code_set,
                fieldbook_path=book or (vocabulary.get("path") or None),
                _prepared_rows=(rows, ids))
        except Exception:
            current_signature = None
        signature_matches = bool(saved_signature and current_signature == saved_signature)
        id_to_row = {pid: i for i, pid in enumerate(ids)}
        findings = []
        for f in payload.get("findings") or []:
            check_name = str(f.get("check", ""))
            is_line_order = (str(f.get("flag", "")).casefold() == "lineordererror"
                             or check_name.strip().casefold() in {
                                 "line command order", "line command out of order", "line: line order"})
            # Older point reports carried whole-line findings in this same list. Keep the
            # point-side Line command order entry, but leave other line findings to Fix Linework.
            if check_name.casefold().startswith("line:") and not is_line_order:
                continue
            idxs = [id_to_row[i] for i in (f.get("points") or []) if i in id_to_row]
            findings.append({"level": f.get("level", "info"), "check": check_name,
                             "flag": f.get("flag", ""), "message": f.get("message", ""),
                             "rows": idxs, "stored": True})
        self.result = {"rows": rows, "ids": ids, "findings": findings, "stats": {}, "flags": {},
                       "code_checks": payload.get("vocabulary") not in ("none", "", None),
                       "line_issues": [], "stored": payload}
        self._stale = not signature_matches or bool(self.state.dirty)
        self.btn_run.setText("Update Check *" if self._stale else "Run the Check")
        if self._stale:
            if signature_matches and self.state.dirty:
                self.btn_run.setToolTip("The project has unsaved changes. Click to compute a fresh check "
                                        "and update the cache.")
            else:
                self.btn_run.setToolTip("The saved check is unverified or its QA inputs changed. "
                                        "Click to compute a fresh check and update the cache.")
        else:
            self.btn_run.setToolTip("Force a fresh check now; automatic refreshes reuse results only "
                                "when QA inputs are unchanged.")
        self._fill_table()
        when = payload.get("when", "")
        vocab = payload.get("vocabulary_label") or payload.get("vocabulary", "")
        extra = (f", {payload.get('errors', 0)} error, {payload.get('warnings', 0)} warning"
                 if payload.get("errors") or payload.get("warnings") else "")
        self.lbl_when.setText(f"Last run {when} - {payload.get('points', 0):,} point(s), "
                              f"{len(findings)} finding(s){extra}"
                              + (f", vocabulary {vocab}" if vocab else "")
                              + (f"  |  {Path(payload['file']).name}" if payload.get("file") else ""))
        if not saved_signature:
            self.lbl_when.setText(self.lbl_when.text() + "  |  Older report: QA inputs were not "
                                  "fingerprinted; update check to verify.")
        elif not signature_matches:
            self.lbl_when.setText(self.lbl_when.text() + "  |  QA inputs changed since this run; "
                                  "update the check.")
        elif self.state.dirty:
            self.lbl_when.setText(self.lbl_when.text() + "  |  The project has unsaved changes.")
        return True

    def _fill_table(self):
        findings = self.result["findings"]
        self.tbl.setRowCount(len(findings))
        ids = self.result.get("ids") or []
        for r, f in enumerate(findings):
            row_idxs = f.get("rows") or []
            point_ids = [ids[i] for i in row_idxs if 0 <= i < len(ids)]
            check_name = str(f.get("check", ""))
            is_line_order = (str(f.get("flag", "")).casefold() == "lineordererror"
                             or check_name.casefold() in {
                                 "line command order", "line command out of order", "line: line order"})
            display_name = "Line command order" if is_line_order else check_name
            vals = [f["level"].upper(), display_name, f["message"],
                    f"{len(row_idxs):,}" if row_idxs else (f"{f.get('count'):,}"
                                                           if f.get("count") else "")]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setForeground(QColor(COLORS.get(f["level"], COLORS["info"])))
                item.setToolTip(f["message"])
                self.tbl.setItem(r, c, item)

            chk = (f.get("check") or "").lower()
            msg = (f.get("message") or "").lower()
            if point_ids:
                if "duplicate" in chk or "duplicate" in msg or ("number" in chk and "used" in msg):
                    btn = QPushButton("Resolve...")
                    btn.setToolTip("Resolve duplicate point number or coordinate conflicts")
                    btn.clicked.connect(lambda _=False, pids=point_ids: self._open_duplicate_resolver(pids))
                    self.tbl.setCellWidget(r, 4, btn)
                elif is_line_order:
                    btn = QPushButton("Fix...")
                    btn.setToolTip("Review Line command order in Fix Point Errors")
                    btn.clicked.connect(lambda _checked=False: self.open_workbench())
                    self.tbl.setCellWidget(r, 4, btn)
                elif "description" in chk or "code" in chk or "unknown" in msg or "vocabulary" in msg:
                    btn = QPushButton("Fix...")
                    btn.setToolTip("Interactively fix descriptions and match feature codes")
                    btn.clicked.connect(lambda _=False, pids=point_ids: self._open_description_fixer(pids))
                    self.tbl.setCellWidget(r, 4, btn)
                elif "line" in chk or "figure" in chk or "unclosed" in msg or "sequence" in msg:
                    btn = QPushButton("Repair...")
                    btn.setToolTip("Open the line-specific repair tool for this older stored finding")
                    btn.clicked.connect(lambda _=False, pids=point_ids: self._open_line_repair(pids))
                    self.tbl.setCellWidget(r, 4, btn)
                else:
                    self.tbl.setCellWidget(r, 4, None)
            else:
                self.tbl.setCellWidget(r, 4, None)

        self.tbl.resizeRowsToContents()

    def auto_fix(self):
        """Batch repair safe description casing and spacing typos across the project."""
        n = auto_fix_descriptions(self.state, self.code_set)
        if n > 0:
            self.state.log(f"Auto-fixed {n} point description(s).", "ok")
            self.run(alert=False)
        else:
            QMessageBox.information(self, "Auto-Fix Descriptions", "No automatic description fixes were needed.")

    def _open_duplicate_resolver(self, point_ids: list[int]):
        dlg = DuplicateResolveDialog(self.state, point_ids, self)
        if dlg.exec():
            self.run(alert=False)

    def _open_description_fixer(self, point_ids: list[int]):
        dlg = DescriptionFixDialog(self.state, point_ids, vocabulary=self.code_set, parent=self)
        if dlg.exec():
            self.run(alert=False)

    def _open_line_repair(self, point_ids: list[int]):
        commands = (self.state.project.settings or {}).get("f2f_commands")
        try:
            book = self.fieldbook()
            if book:
                from ..fieldwork.io_carlson import read_fwb_extra
                book_commands = read_fwb_extra(book).get("commands")
                if book_commands is not None:
                    commands = book_commands
        except Exception:
            pass
        dlg = LineRepairDialog(self.state, point_ids, self, commands=commands)
        if dlg.exec():
            self.run(alert=False)

    # --------------------------------------------------------------------- acting on a finding
    def _ids(self) -> list:
        if self.result is None:
            return []
        row = self.tbl.currentRow()
        if row < 0 or row >= len(self.result["findings"]):
            return []
        ids = self.result["ids"]
        return [ids[i] for i in self.result["findings"][row]["rows"] if 0 <= i < len(ids)]

    def _select(self):
        ids = self._ids()
        if ids:
            self.state.select(points=ids)

    def _zoom(self, r, c):
        ids = self._ids()
        pr = self.state.project
        pts = [pr.points[i] for i in ids if i in pr.points]
        if pts:
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            pad = 15.0
            self.state.zoom_to_bbox((min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))

