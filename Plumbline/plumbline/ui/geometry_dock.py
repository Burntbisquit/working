"""Right-hand Geometry panel: segment inventory and linked-point properties."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.crossings import (GeometryCheckReport, GeometryIssue,
                              default_max_segment_length, run_geometry_checks)
from ..core.geometry_segments import Segment, build_segments, segment_point_rows
from ..core.geometry_tools import (AutoNudgeResult, ShrinkCommandPreview,
                                   SquareShapeResult, add_polyline_node,
                                   apply_connect_segments, apply_shrink_line_commands,
                                   auto_nudge_crossings, cogo_points_from_lines,
                                   flip_past, inject_survey_point, join_lines_average,
                                   move_line_end_to_end, nudge_point,
                                   preview_connect_segments, preview_shrink_line_commands,
                                   square_shape, untwist_polyline)
from ..core.model import Polyline


class SquareShapeDialog(QDialog):
    """Dialog for squaring a building/structure polyline to 90° corners (§3.6)."""

    def __init__(self, parent=None, *, initial_mode: str = "average", locked_text: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Square Shape (90° Corners)")
        form = QFormLayout(self)
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("Average (mean orthogonal dimensions)", "average")
        self.mode_combo.addItem("Small (hold smaller parallel sides)", "small")
        self.mode_combo.addItem("Large (hold larger parallel sides)", "large")
        self.mode_combo.addItem("Lock points (keep anchor vertices fixed)", "lock_points")
        idx = self.mode_combo.findData(initial_mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)
        form.addRow("Sizing mode:", self.mode_combo)

        self.locked_edit = QLineEdit(locked_text, self)
        self.locked_edit.setPlaceholderText("0-based vertex indices, e.g. 0, 2")
        form.addRow("Locked vertices:", self.locked_edit)

        self.propagate_chk = QCheckBox("Update linked survey point coordinates", self)
        self.propagate_chk.setChecked(True)
        form.addRow("", self.propagate_chk)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @property
    def selected_mode(self) -> str:
        return str(self.mode_combo.currentData() or "average")

    @property
    def locked_indices(self) -> list[int]:
        out: list[int] = []
        for tok in self.locked_edit.text().replace(";", ",").split(","):
            tok = tok.strip()
            if tok.isdigit():
                out.append(int(tok))
        return out


class GeometryDock(QWidget):
    """A compact first Geometry panel; edits only linked survey point properties."""

    edit_mode_changed = Signal(bool)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._segments: list[Segment] = []
        self._issues: list[GeometryIssue] = []
        self.last_check_report: GeometryCheckReport | None = None
        self._updating = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 5)
        intro = QLabel("Segments are auto-named by field occurrence. Linked linework is locked until Edit linked linework is enabled.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        controls = QHBoxLayout()
        self.edit_mode = QCheckBox("Edit linked linework")
        self.edit_mode.setToolTip("Moves a linked field point and rebuilds every linework string using that point.")
        self.edit_mode.toggled.connect(self.edit_mode_changed)
        controls.addWidget(self.edit_mode, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        lay.addLayout(controls)

        lay.addWidget(QLabel("Segments"))
        self.segments = QTableWidget(0, 7)
        self.segments.setHorizontalHeaderLabels(["Segment", "Source", "Points", "2D Len", "Layer", "Kind", "Lock"])
        self.segments.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.segments.setSelectionMode(QAbstractItemView.SingleSelection)
        self.segments.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.segments.verticalHeader().setVisible(False)
        self.segments.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.segments.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.segments.itemSelectionChanged.connect(self._segment_selected)
        lay.addWidget(self.segments, 1)

        lay.addWidget(QLabel("Points — edit Code, Command, Description, E, N, or Z; click a row to select the survey point."))
        self.points = QTableWidget(0, 9)
        self.points.setHorizontalHeaderLabels(["#", "Code", "Command", "Description", "E", "N", "Z", "To next", "Link"])
        self.points.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.points.setSelectionMode(QAbstractItemView.SingleSelection)
        self.points.verticalHeader().setVisible(False)
        self.points.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.points.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.points.itemSelectionChanged.connect(self._point_selected)
        self.points.itemChanged.connect(self._point_changed)
        lay.addWidget(self.points, 1)

        tools_bar = QHBoxLayout()
        self.untwist_btn = QPushButton("Untwist")
        self.untwist_btn.setToolTip("Uncross a self-crossing bowtie on the selected segment.")
        self.untwist_btn.clicked.connect(self.untwist_selected)
        tools_bar.addWidget(self.untwist_btn)

        self.flip_btn = QPushButton("Flip past")
        self.flip_btn.setToolTip("Reflect the selected interior vertex across its adjacent chord.")
        self.flip_btn.clicked.connect(self.flip_past_selected)
        tools_bar.addWidget(self.flip_btn)

        self.add_node_btn = QPushButton("Add node")
        self.add_node_btn.setToolTip("Insert a pure geometry node (no survey point) after the selected vertex.")
        self.add_node_btn.clicked.connect(self.add_node_selected)
        tools_bar.addWidget(self.add_node_btn)

        self.inject_pt_btn = QPushButton("Inject point")
        self.inject_pt_btn.setToolTip("Insert a numbered survey point after the selected vertex.")
        self.inject_pt_btn.clicked.connect(self.inject_point_selected)
        tools_bar.addWidget(self.inject_pt_btn)

        self.cogo_btn = QPushButton("COGO from lines")
        self.cogo_btn.setToolTip("Create deduplicated survey points at endpoints of the selected polyline.")
        self.cogo_btn.clicked.connect(self.cogo_from_selected)
        tools_bar.addWidget(self.cogo_btn)

        self.shrink_btn = QPushButton("Shrink Line Commands")
        self.shrink_btn.setToolTip("Repair missing ST/END commands and compact jumped-to line suffixes.")
        self.shrink_btn.clicked.connect(self.shrink_line_commands)
        tools_bar.addWidget(self.shrink_btn)

        self.auto_nudge_btn = QPushButton("Auto-nudge")
        self.auto_nudge_btn.setToolTip("Iteratively resolve breakline crossings using untwist, flip-past, and micro-nudge.")
        self.auto_nudge_btn.clicked.connect(self.auto_nudge_selected)
        tools_bar.addWidget(self.auto_nudge_btn)

        self.square_btn = QPushButton("Square shape...")
        self.square_btn.setToolTip("Snap building/structure edges to 90° angles (average / small / large / lock points).")
        self.square_btn.clicked.connect(self.square_shape_selected)
        tools_bar.addWidget(self.square_btn)
        tools_bar.addStretch(1)
        lay.addLayout(tools_bar)

        qa_bar = QHBoxLayout()
        qa_bar.addWidget(QLabel("Geometry QA — Max len:"))
        self.max_length_spin = QDoubleSpinBox()
        self.max_length_spin.setRange(1.0, 100000.0)
        self.max_length_spin.setDecimals(1)
        self.max_length_spin.setSingleStep(25.0)
        self.max_length_spin.setValue(default_max_segment_length(self.state.project))
        self.max_length_spin.setToolTip("Flag segment spans longer than this threshold (default 300 ft / 100 m).")
        self.max_length_spin.valueChanged.connect(self._max_length_changed)
        qa_bar.addWidget(self.max_length_spin)
        self.check_btn = QPushButton("Check geometry")
        self.check_btn.setToolTip("Detect all breakline crossings, Z-conflicts, long spans, and ST/END/PC/PT issues.")
        self.check_btn.clicked.connect(self.run_checks)
        qa_bar.addWidget(self.check_btn)
        self.apply_fix_btn = QPushButton("Apply fix")
        self.apply_fix_btn.setEnabled(False)
        self.apply_fix_btn.setToolTip("Apply the proposed point description repair for the selected command finding.")
        self.apply_fix_btn.clicked.connect(self.apply_selected_fix)
        qa_bar.addWidget(self.apply_fix_btn)
        qa_bar.addStretch(1)
        lay.addLayout(qa_bar)

        self.findings = QTableWidget(0, 5)
        self.findings.setHorizontalHeaderLabels(["Issue", "Segments", "Points", "Detail", "Proposed Fix"])
        self.findings.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.findings.setSelectionMode(QAbstractItemView.SingleSelection)
        self.findings.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.findings.verticalHeader().setVisible(False)
        self.findings.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.findings.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.findings.itemSelectionChanged.connect(self._finding_selected)
        lay.addWidget(self.findings, 1)

        self.status = QLabel("Select a segment to inspect its field points.")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        state.changed.connect(lambda _k: self.refresh(keep_selection=True))
        state.project_replaced.connect(self.refresh)
        self.refresh()

    @staticmethod
    def _item(text, editable=False, data=None):
        item = QTableWidgetItem(str(text))
        flags = item.flags()
        item.setFlags((flags | Qt.ItemIsEditable) if editable else (flags & ~Qt.ItemIsEditable))
        if data is not None:
            item.setData(Qt.UserRole, data)
        return item

    def refresh(self, keep_selection=False):
        selected_id = self.current_segment.entity_id if keep_selection and self.current_segment else None
        selected_seg_id = self.current_segment.seg_id if keep_selection and self.current_segment else None
        self._updating = True
        try:
            self._segments = build_segments(self.state.project)
            self.segments.setRowCount(len(self._segments))
            for row, segment in enumerate(self._segments):
                values = [segment.seg_id, segment.source, len(segment.point_ids), f"{segment.length_2d:.2f}",
                          segment.layer, segment.kind, "Linked" if segment.linked else "Free"]
                for col, value in enumerate(values):
                    self.segments.setItem(row, col, self._item(value, data=segment.entity_id if col == 0 else None))
            if selected_id is not None:
                for row, segment in enumerate(self._segments):
                    if segment.entity_id == selected_id:
                        self.segments.selectRow(row)
                        break
            if self.segments.currentRow() < 0 and selected_seg_id is not None:
                for row, segment in enumerate(self._segments):
                    if segment.seg_id == selected_seg_id:
                        self.segments.selectRow(row)
                        break
            if self.segments.currentRow() < 0 and self._segments:
                self.segments.selectRow(0)
            self.max_length_spin.blockSignals(True)
            self.max_length_spin.setValue(default_max_segment_length(self.state.project))
            self.max_length_spin.blockSignals(False)
            self._populate_points()
            if self.last_check_report is not None:
                self._run_checks_internal()
        finally:
            self._updating = False

    def _max_length_changed(self, value: float):
        if self._updating:
            return
        if self.state.project.settings is None:
            self.state.project.settings = {}
        self.state.project.settings["max_segment_length"] = float(value)
        if self.last_check_report is not None:
            self.run_checks()

    def _run_checks_internal(self) -> GeometryCheckReport:
        report = run_geometry_checks(
            self.state.project,
            segments=self._segments,
            max_length=float(self.max_length_spin.value()),
        )
        self.last_check_report = report
        self._issues = list(report.issues)
        self._populate_findings()
        return report

    def run_checks(self) -> GeometryCheckReport:
        self._updating = True
        try:
            report = self._run_checks_internal()
        finally:
            self._updating = False
        n_cross = len(report.crossings)
        n_zconf = sum(1 for c in report.crossings if c.has_z_conflict)
        n_long = len(report.long_segments)
        n_cmd = len(report.command_issues)
        self.status.setText(
            f"Geometry QA ({report.breakline_count} breakline(s), max {report.max_segment_length:.0f}): "
            f"{n_cross} crossing(s) ({n_zconf} Z-conflict), {n_long} long span(s), {n_cmd} command issue(s)."
        )
        return report

    def _populate_findings(self):
        self.findings.setRowCount(len(self._issues))
        for row, issue in enumerate(self._issues):
            seg_text = ", ".join(issue.seg_ids) if issue.seg_ids else "—"
            pt_text = ", ".join(issue.point_numbers) if issue.point_numbers else "—"
            values = [issue.title, seg_text, pt_text, issue.detail, issue.proposal]
            for col, value in enumerate(values):
                self.findings.setItem(row, col, self._item(value, editable=False, data=row if col == 0 else None))
        self.apply_fix_btn.setEnabled(bool(self.current_issue and self.current_issue.proposal and self.current_issue.target_point_id is not None))

    @property
    def current_issue(self) -> GeometryIssue | None:
        row = self.findings.currentRow()
        return self._issues[row] if 0 <= row < len(self._issues) else None

    def _finding_selected(self):
        issue = self.current_issue
        self.apply_fix_btn.setEnabled(bool(issue and issue.proposal and issue.target_point_id is not None))
        if self._updating or issue is None:
            return
        self._updating = True
        try:
            if issue.entity_ids:
                primary_eid = issue.entity_ids[0]
                for row, seg in enumerate(self._segments):
                    if seg.entity_id == primary_eid:
                        self.segments.selectRow(row)
                        self._populate_points()
                        break
            self.state.select(points=issue.point_ids, entities=issue.entity_ids, mode="replace")
            self.status.setText(issue.detail)
        finally:
            self._updating = False

    def apply_selected_fix(self) -> bool:
        issue = self.current_issue
        if issue is None or not issue.proposal or issue.target_point_id is None:
            return False
        point = self.state.project.points.get(issue.target_point_id)
        if point is None:
            return False
        with self.state.edit(f"Fix {issue.title} on point {point.number}"):
            point.desc = issue.proposal
            self.state.project.process_linework(order="number")
        self.run_checks()
        self.status.setText(f"Applied fix on point {point.number}: '{issue.proposal}'.")
        return True

    @property
    def current_segment(self) -> Segment | None:
        row = self.segments.currentRow()
        return self._segments[row] if 0 <= row < len(self._segments) else None

    def _segment_selected(self):
        if self._updating:
            return
        segment = self.current_segment
        if segment is None:
            return
        self.state.select(entities=[segment.entity_id], mode="replace")
        self._updating = True
        try:
            self._populate_points()
        finally:
            self._updating = False

    def _command_text(self, desc: str) -> str:
        from ..core.featurecodes import parse_description
        commands = self.state.project.settings.get("f2f_commands")
        parsed = parse_description(desc, commands=commands, known_codes=self.state.project.codes.codes)
        return " ".join(sorted(parsed.flags))

    def _populate_points(self):
        segment = self.current_segment
        rows = segment_point_rows(self.state.project, segment) if segment else []
        self.points.setRowCount(len(rows))
        for row, point in enumerate(rows):
            pid = point["point_id"]
            editable = bool(point["linked"])
            z_text = "" if not math.isfinite(point["z"]) else f"{point['z']:.4f}"
            code_prefix = f"{segment.code}{segment.string}" if segment else ""
            values = [point["number"], code_prefix, self._command_text(point["desc"]), point["desc"],
                      f"{point['x']:.4f}", f"{point['y']:.4f}", z_text,
                      "" if point["to_next"] is None else f"{point['to_next']:.3f}",
                      "Point" if editable else "Node"]
            for col, value in enumerate(values):
                can_edit = editable and col in (1, 2, 3, 4, 5, 6)
                self.points.setItem(row, col, self._item(value, can_edit, pid if col == 0 else None))

    def _point_selected(self):
        if self._updating:
            return
        item = self.points.item(self.points.currentRow(), 0)
        point_id = item.data(Qt.UserRole) if item else None
        if point_id is not None:
            self.state.select(points=[int(point_id)], mode="replace")

    def _point_changed(self, item):
        if self._updating or item.column() not in (1, 2, 3, 4, 5, 6):
            return
        id_item = self.points.item(item.row(), 0)
        point_id = id_item.data(Qt.UserRole) if id_item else None
        point = self.state.project.points.get(point_id)
        segment = self.current_segment
        if point is None or segment is None:
            return
        prefix = f"{segment.code}{segment.string}"
        try:
            with self.state.edit("Edit geometry point"):
                if item.column() == 1:
                    from ..core.point_linework_coder import set_point_line_code
                    point.desc = set_point_line_code(point.desc, prefix, item.text(),
                                                     self.state.project.settings.get("f2f_commands"))
                elif item.column() == 2:
                    from ..core.point_linework_coder import set_point_line_boundary
                    point.desc = set_point_line_boundary(point.desc, prefix, item.text(),
                                                         self.state.project.settings.get("f2f_commands"))
                elif item.column() == 3:
                    point.desc = item.text().strip()
                elif item.column() == 4:
                    point.x = float(item.text())
                elif item.column() == 5:
                    point.y = float(item.text())
                else:
                    point.z = float(item.text()) if item.text().strip() else float("nan")
                self.state.project.process_linework(order="number")
            self.status.setText(f"Updated point {point.number}; point-driven linework rebuilt in ascending point number.")
        except ValueError as exc:
            self.status.setText(str(exc) or "Coordinates must be valid numbers. The prior value was kept.")
        self.refresh(keep_selection=True)

    # ------------------------------------------------------------------ Phase 3 tools
    def untwist_selected(self) -> bool:
        segment = self.current_segment
        if segment is None:
            return False
        try:
            with self.state.edit(f"Untwist {segment.seg_id}"):
                untwist_polyline(self.state.project, segment.entity_id)
            self.status.setText(f"Untwisted bowtie on {segment.seg_id}.")
            self.refresh(keep_selection=True)
            return True
        except ValueError as exc:
            self.status.setText(str(exc))
            return False

    def flip_past_selected(self) -> bool:
        segment = self.current_segment
        row = self.points.currentRow()
        if segment is None or row < 0:
            return False
        try:
            with self.state.edit(f"Flip vertex on {segment.seg_id}"):
                flip_past(self.state.project, segment.entity_id, [row])
            self.status.setText(f"Flipped vertex {row + 1} on {segment.seg_id}.")
            self.refresh(keep_selection=True)
            return True
        except ValueError as exc:
            self.status.setText(str(exc))
            return False

    def add_node_selected(self) -> bool:
        segment = self.current_segment
        if segment is None:
            return False
        span_idx = max(0, min(self.points.currentRow(), max(0, len(segment.point_ids) - 2)))
        try:
            with self.state.edit(f"Add node on {segment.seg_id}"):
                add_polyline_node(self.state.project, segment.entity_id, span_idx)
            self.status.setText(f"Added pure geometry node on span {span_idx + 1} of {segment.seg_id}.")
            self.refresh(keep_selection=True)
            return True
        except ValueError as exc:
            self.status.setText(str(exc))
            return False

    def inject_point_selected(self):
        segment = self.current_segment
        if segment is None:
            return None
        span_idx = max(0, min(self.points.currentRow(), max(0, len(segment.point_ids) - 2)))
        try:
            with self.state.edit(f"Inject point on {segment.seg_id}"):
                pt = inject_survey_point(self.state.project, segment.entity_id, span_idx)
            self.status.setText(f"Injected survey point #{pt.number} on span {span_idx + 1} of {segment.seg_id}.")
            self.refresh(keep_selection=True)
            return pt
        except ValueError as exc:
            self.status.setText(str(exc))
            return None

    def cogo_from_selected(self) -> list:
        segment = self.current_segment
        target_ids = [segment.entity_id] if segment is not None else None
        with self.state.edit("Create COGO points from lines"):
            created = cogo_points_from_lines(self.state.project, target_ids)
        self.status.setText(f"Created {len(created)} new COGO survey point(s) (shared endpoints deduplicated).")
        self.refresh(keep_selection=True)
        return created

    def shrink_line_commands(self) -> ShrinkCommandPreview:
        preview = preview_shrink_line_commands(self.state.project)
        if not preview.changes:
            self.status.setText("Shrink Line Commands: no missing ST/END or compactable suffixes found.")
            return preview
        with self.state.edit("Shrink line commands"):
            apply_shrink_line_commands(self.state.project, preview, rebuild_linework=True)
        self.status.setText(
            f"Shrink Line Commands updated {len(preview.changes)} point(s) and compacted {len(preview.compactions)} occurrence(s)."
        )
        self.refresh(keep_selection=True)
        return preview

    def nudge_selected_point(self, dx: float, dy: float, dz: float = 0.0) -> bool:
        row = self.points.currentRow()
        item = self.points.item(row, 0) if row >= 0 else None
        pid = item.data(Qt.UserRole) if item else None
        if pid is None:
            return False
        with self.state.edit("Nudge point"):
            nudge_point(self.state.project, int(pid), dx, dy, dz)
        self.refresh(keep_selection=True)
        return True

    def connect_points(self, point_a_id: int, point_b_id: int, *, base_code: str | None = None, snap_tolerance: float = 0.0):
        preview = preview_connect_segments(
            self.state.project, point_a_id, point_b_id, base_code=base_code, snap_tolerance=snap_tolerance
        )
        with self.state.edit(f"Connect lines ({preview.chosen_label})"):
            apply_connect_segments(self.state.project, preview, rebuild_linework=True)
        self.status.setText(f"Connected {len(preview.routed_point_ids)} point(s) as {preview.chosen_label}.")
        self.refresh(keep_selection=True)
        return preview

    def join_average_segments(self, entity_a_id: int, end_a: str, entity_b_id: int, end_b: str) -> dict:
        with self.state.edit("Join lines (average endpoints)"):
            res = join_lines_average(self.state.project, entity_a_id, end_a, entity_b_id, end_b)
        self.status.setText(f"Averaged endpoints at ({res['x']:.3f}, {res['y']:.3f}); both point numbers preserved.")
        self.refresh(keep_selection=True)
        return res

    def move_end_to_end_segments(self, mover_entity_id: int, mover_end: str, target_entity_id: int, target_end: str, *, whole_line: bool = False) -> dict:
        with self.state.edit("Move line end to end"):
            res = move_line_end_to_end(self.state.project, mover_entity_id, mover_end, target_entity_id, target_end, whole_line=whole_line)
        self.status.setText(f"Moved endpoint by ({res['dx']:.3f}, {res['dy']:.3f}).")
        self.refresh(keep_selection=True)
        return res

    # ------------------------------------------------------------------ Phase 4 solvers
    def auto_nudge_selected(self, *, max_displacement: float = 5.0) -> AutoNudgeResult:
        with self.state.edit("Auto-nudge breakline crossings"):
            res = auto_nudge_crossings(self.state.project, max_displacement=max_displacement)
        self.refresh(keep_selection=True)
        self.run_checks()
        self.status.setText(
            f"Auto-nudge removed {res.crossings_removed}/{res.initial_crossings} crossing(s) "
            f"({res.points_moved} moved, total disp {res.total_displacement:.3f}, {res.remaining_crossings} residue)."
        )
        return res

    def square_shape_selected(
        self,
        *,
        mode: str | None = None,
        locked_indices: list[int] | None = None,
        propagate_to_points: bool = True,
    ) -> SquareShapeResult | None:
        segment = self.current_segment
        if segment is None:
            return None
        if mode is None:
            cur_pt = self.points.currentRow()
            default_locked = str(cur_pt) if cur_pt >= 0 else ""
            dlg = SquareShapeDialog(self, locked_text=default_locked)
            if dlg.exec() != QDialog.Accepted:
                return None
            mode = dlg.selected_mode
            locked_indices = dlg.locked_indices
            propagate_to_points = dlg.propagate_chk.isChecked()
        try:
            with self.state.edit(f"Square shape {segment.seg_id} ({mode})"):
                res = square_shape(
                    self.state.project,
                    segment.entity_id,
                    mode=mode,
                    locked_indices=locked_indices or (),
                    propagate_to_points=propagate_to_points,
                )
            self.refresh(keep_selection=True)
            self.status.setText(
                f"Squared {segment.seg_id} ({res.mode}, azimuth {res.orientation_deg:.2f}°, max disp {res.max_displacement:.3f})."
            )
            return res
        except ValueError as exc:
            self.status.setText(str(exc))
            return None


