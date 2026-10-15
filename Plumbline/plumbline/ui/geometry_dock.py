"""Right-hand Geometry panel: segment inventory and linked-point properties."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..core.geometry_segments import Segment, build_segments, segment_point_rows
from ..core.model import Polyline


class GeometryDock(QWidget):
    """A compact first Geometry panel; edits only linked survey point properties."""

    edit_mode_changed = Signal(bool)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._segments: list[Segment] = []
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
            self._populate_points()
        finally:
            self._updating = False

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
