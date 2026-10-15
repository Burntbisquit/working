"""Transactional line geometry editor and standalone Join Lines draft/preview dialog."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.linework_editor import (JoinLinesDraft, LineEditDraft, LineworkEditError,
                                    commit_join_draft, commit_line_draft, bulge_points)
from ..core.model import Polyline
from .canvas import CanvasView
from .widgets import Hint


class LinePreviewWidget(QWidget):
    """Small plan preview that can show the original line behind a staged result."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 220)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        self.vertices = np.empty((0, 3), dtype=float)
        self.bulges = np.empty(0, dtype=float)
        self.closed = False
        self.ghost = None
        self.view_mode = "plan"

    def set_view_mode(self, mode: str):
        self.view_mode = "3d" if str(mode).casefold() == "3d" else "plan"
        self.update()

    def set_line(self, vertices, bulges=None, closed=False, ghost=None):
        self.vertices = np.asarray(vertices, dtype=float).reshape(-1, 3) if len(vertices) else np.empty((0, 3))
        self.bulges = np.zeros(len(self.vertices)) if bulges is None else np.asarray(bulges, dtype=float)
        if len(self.bulges) < len(self.vertices):
            self.bulges = np.pad(self.bulges, (0, len(self.vertices) - len(self.bulges)))
        self.closed = bool(closed)
        self.ghost = ghost
        self.update()

    @staticmethod
    def _sample(vertices, bulges, closed):
        vertices = np.asarray(vertices, dtype=float).reshape(-1, 3)
        if len(vertices) < 2:
            return vertices
        segment_count = len(vertices) if closed else len(vertices) - 1
        samples = []
        for index in range(segment_count):
            nxt = (index + 1) % len(vertices)
            values = bulge_points(vertices[index], vertices[nxt], bulges[index] if index < len(bulges) else 0.0)
            samples.extend(values[:-1])
        samples.append(vertices[0] if closed else vertices[-1])
        return np.asarray(samples, dtype=float)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        raw_ghost_lines = []
        if self.ghost:
            raw_ghosts = self.ghost if isinstance(self.ghost, list) else [self.ghost]
            raw_ghost_lines = [self._sample(*line) for line in raw_ghosts]
        raw_preview_points = self._sample(self.vertices, self.bulges, self.closed)
        raw_all = [value for value in [*raw_ghost_lines, raw_preview_points] if len(value)]
        if not raw_all:
            painter.setPen(self.palette().placeholderText().color())
            painter.drawText(self.rect(), Qt.AlignCenter, "Line preview")
            return
        if self.view_mode == "3d":
            finite_z = np.concatenate([value[:, 2][np.isfinite(value[:, 2])] for value in raw_all])
            z_origin = float(np.min(finite_z)) if len(finite_z) else 0.0

            def project(points):
                values = np.asarray(points, dtype=float)
                z = np.where(np.isfinite(values[:, 2]), values[:, 2], z_origin) - z_origin
                return np.column_stack(((values[:, 0] - values[:, 1]) / math.sqrt(2.0),
                                        (values[:, 0] + values[:, 1]) / math.sqrt(6.0)
                                        + z * math.sqrt(2.0 / 3.0)))
        else:
            def project(points):
                return np.asarray(points, dtype=float)[:, :2]

        ghost_lines = [project(points) for points in raw_ghost_lines]
        preview_points = project(raw_preview_points)
        vertex_points = project(self.vertices)
        joined = np.vstack([*ghost_lines, preview_points])
        finite = joined[np.isfinite(joined).all(axis=1)]
        if not len(finite):
            return
        xmin, ymin = np.min(finite, axis=0)
        xmax, ymax = np.max(finite, axis=0)
        span_x, span_y = max(float(xmax - xmin), 1e-6), max(float(ymax - ymin), 1e-6)
        margin = 28.0
        width = max(1.0, self.width() - 2.0 * margin)
        height = max(1.0, self.height() - 2.0 * margin)
        scale = min(width / span_x, height / span_y)
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0

        def mapped(xy):
            return QPointF(self.width() / 2.0 + (float(xy[0]) - cx) * scale,
                           self.height() / 2.0 - (float(xy[1]) - cy) * scale)

        for ghost_points in ghost_lines:
            if len(ghost_points) < 2:
                continue
            ghost_path = QPainterPath(mapped(ghost_points[0]))
            for point in ghost_points[1:]:
                ghost_path.lineTo(mapped(point))
            painter.setPen(QPen(QColor(130, 140, 150, 130), 2, Qt.DashLine))
            painter.drawPath(ghost_path)
        if len(preview_points) > 1:
            path = QPainterPath(mapped(preview_points[0]))
            for point in preview_points[1:]:
                path.lineTo(mapped(point))
            painter.setPen(QPen(QColor(55, 155, 235), 2.5))
            painter.drawPath(path)
        for index, point in enumerate(vertex_points):
            pos = mapped(point)
            painter.setPen(QPen(QColor(25, 70, 110), 1))
            painter.setBrush(QColor(245, 190, 75))
            painter.drawEllipse(pos, 4.5, 4.5)
            painter.setPen(QPen(self.palette().text().color(), 1))
            painter.drawText(pos + QPointF(5, -5), str(index + 1))
        painter.setPen(self.palette().placeholderText().color())
        painter.drawText(10, 18, "3D — isometric" if self.view_mode == "3d" else "Plan — Easting / Northing")


class LineEditorDialog(QDialog):
    """Stage, preview, locally undo, and transactionally apply one line's geometry edits."""

    def __init__(self, state, polyline: Polyline, parent=None):
        super().__init__(parent)
        self.state = state
        window = parent.window() if parent is not None and callable(getattr(parent, "window", None)) else parent
        self.parent_window = window
        self._allow_close = False
        self.draft = LineEditDraft(state.project, polyline)
        self.setWindowTitle(f"Line Geometry Editor — {polyline.layer} / {polyline.id}")
        self.resize(1160, 760)
        root = QVBoxLayout(self)
        root.addWidget(Hint(
            "Edits are staged on a working copy. Apply creates one project undo step; "
            "Undo Last Function reverses only the most recent staged operation."))

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Edit mode:"))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("Graphical only", "graphics")
        self.cmb_mode.addItem("Point editing — keep linked points in sync", "points")
        if not self.draft.point_mode_ready:
            self.cmb_mode.model().item(1).setEnabled(False)
        self.cmb_mode.currentIndexChanged.connect(self._change_mode)
        mode_row.addWidget(self.cmb_mode)
        self.lbl_link_status = QLabel()
        self.lbl_link_status.setProperty("hint", "true")
        mode_row.addWidget(self.lbl_link_status, 1)
        root.addLayout(mode_row)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_lay = QVBoxLayout(left)
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("View:"))
        self.cmb_view = QComboBox()
        self.cmb_view.addItem("Plan", "plan")
        self.cmb_view.addItem("3D", "3d")
        self.cmb_view.addItem("Image", "image")
        self.cmb_view.addItem("All data", "all_data")
        self.cmb_view.currentIndexChanged.connect(self._change_view)
        view_row.addWidget(self.cmb_view)
        view_row.addStretch(1)
        left_lay.addLayout(view_row)

        self.view_stack = QStackedWidget()
        self.preview = LinePreviewWidget()
        self.view_stack.addWidget(self.preview)
        self.context_canvas = CanvasView(state)
        self.context_canvas._fit_pending = False
        self.context_canvas.opts.show_grid = False
        self.context_canvas.opts.show_imagery = True
        self.context_canvas.opts.show_points = False
        self.context_canvas.opts.show_lines = False
        self.context_canvas.opts.show_text = False
        self.context_canvas.extra_overlay = self._paint_draft_overlay
        self.view_stack.addWidget(self.context_canvas)
        left_lay.addWidget(self.view_stack, 2)
        self.lbl_view_hint = QLabel("Staged line geometry preview.")
        self.lbl_view_hint.setWordWrap(True)
        self.lbl_view_hint.setProperty("hint", "true")
        left_lay.addWidget(self.lbl_view_hint)

        self.tbl_vertices = QTableWidget(0, 6)
        self.tbl_vertices.setHorizontalHeaderLabels(["Vertex", "Point #", "Easting", "Northing", "Elevation", "Outgoing curve °"])
        self.tbl_vertices.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_vertices.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_vertices.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_vertices.verticalHeader().setVisible(False)
        self.tbl_vertices.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_vertices.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for col in (2, 3, 4, 5):
            self.tbl_vertices.horizontalHeader().setSectionResizeMode(col, QHeaderView.Stretch)
        self.tbl_vertices.itemSelectionChanged.connect(self._select_vertex)
        left_lay.addWidget(self.tbl_vertices, 1)
        split.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        operation_group = QGroupBox("Line operations")
        op_lay = QVBoxLayout(operation_group)
        self.btn_insert = QPushButton("Insert vertex at selected segment midpoint")
        self.btn_insert.clicked.connect(self._insert_vertex)
        self.btn_delete = QPushButton("Delete selected vertex")
        self.btn_delete.clicked.connect(self._delete_vertex)
        op_lay.addWidget(self.btn_insert)
        op_lay.addWidget(self.btn_delete)

        coords = QFormLayout()
        self.ed_x = QLineEdit()
        self.ed_y = QLineEdit()
        self.ed_z = QLineEdit()
        self.ed_x.setPlaceholderText("Easting")
        self.ed_y.setPlaceholderText("Northing")
        self.ed_z.setPlaceholderText("blank clears elevation")
        coords.addRow("Move to E:", self.ed_x)
        coords.addRow("N:", self.ed_y)
        coords.addRow("Z:", self.ed_z)
        self.btn_move = QPushButton("Move selected vertex")
        self.btn_move.clicked.connect(self._move_vertex)
        coords.addRow(self.btn_move)

        interpolation_row = QHBoxLayout()
        self.sp_interpolation = QDoubleSpinBox()
        self.sp_interpolation.setRange(0.0, 100.0)
        self.sp_interpolation.setDecimals(1)
        self.sp_interpolation.setSuffix("%")
        self.sp_interpolation.setValue(50.0)
        self.sp_interpolation.setToolTip("0% uses the previous neighbor; 100% uses the following neighbor.")
        self.btn_interpolate = QPushButton("Set interpolated position")
        self.btn_interpolate.setToolTip("Place an interior vertex between its previous and next neighbors, including elevation.")
        self.btn_interpolate.clicked.connect(self._interpolate_vertex)
        interpolation_row.addWidget(self.sp_interpolation)
        interpolation_row.addWidget(self.btn_interpolate)
        coords.addRow("Interpolate:", interpolation_row)

        nudge_row = QHBoxLayout()
        nudge_row.addWidget(QLabel("Nudge distance:"))
        self.sp_nudge = QDoubleSpinBox()
        self.sp_nudge.setRange(0.0001, 1000000.0)
        self.sp_nudge.setDecimals(4)
        self.sp_nudge.setSingleStep(0.1)
        self.sp_nudge.setValue(0.1)
        self.sp_nudge.setToolTip("Distance in the project's coordinate units.")
        nudge_row.addWidget(self.sp_nudge)
        nudge_row.addStretch(1)
        coords.addRow(nudge_row)

        nudge_buttons = QHBoxLayout()
        self.btn_nudge_west = QPushButton("← West")
        self.btn_nudge_east = QPushButton("East →")
        self.btn_nudge_south = QPushButton("↓ South")
        self.btn_nudge_north = QPushButton("↑ North")
        for button, dx, dy in (
            (self.btn_nudge_west, -1.0, 0.0), (self.btn_nudge_east, 1.0, 0.0),
            (self.btn_nudge_south, 0.0, -1.0), (self.btn_nudge_north, 0.0, 1.0),
        ):
            button.setToolTip("Nudge the selected line vertex; in point-edit mode its linked survey point moves on Apply.")
            button.clicked.connect(lambda _checked=False, x=dx, y=dy: self._nudge_vertex(x, y))
            nudge_buttons.addWidget(button)
        coords.addRow("Nudge point:", nudge_buttons)
        op_lay.addLayout(coords)

        add_row = QHBoxLayout()
        self.btn_start = QPushButton("Insert at start")
        self.btn_start.clicked.connect(lambda: self._insert_end(at_start=True))
        self.btn_end = QPushButton("Insert at end")
        self.btn_end.clicked.connect(lambda: self._insert_end(at_start=False))
        add_row.addWidget(self.btn_start)
        add_row.addWidget(self.btn_end)
        op_lay.addLayout(add_row)

        middle_row = QHBoxLayout()
        self.btn_swap = QPushButton("Swap with previous")
        self.btn_swap.clicked.connect(self._swap_vertex)
        self.btn_reverse = QPushButton("Reverse direction")
        self.btn_reverse.clicked.connect(self._reverse)
        middle_row.addWidget(self.btn_swap)
        middle_row.addWidget(self.btn_reverse)
        op_lay.addLayout(middle_row)

        curve_row = QHBoxLayout()
        self.sp_sweep = QDoubleSpinBox()
        self.sp_sweep.setRange(-358.99, 358.99)
        self.sp_sweep.setDecimals(2)
        self.sp_sweep.setSuffix("°")
        self.sp_sweep.setValue(90.0)
        self.btn_curve = QPushButton("Set curve")
        self.btn_curve.clicked.connect(self._set_curve)
        self.btn_straight = QPushButton("Straighten")
        self.btn_straight.clicked.connect(self._straighten)
        curve_row.addWidget(self.sp_sweep)
        curve_row.addWidget(self.btn_curve)
        curve_row.addWidget(self.btn_straight)
        op_lay.addLayout(curve_row)

        self.btn_close = QPushButton("Close / open line")
        self.btn_close.clicked.connect(self._toggle_closed)
        op_lay.addWidget(self.btn_close)
        right_lay.addWidget(operation_group)

        self.lst_actions = QListWidget()
        self.lst_actions.setMinimumHeight(110)
        right_lay.addWidget(QLabel("Staged functions"))
        right_lay.addWidget(self.lst_actions, 1)
        self.btn_undo = QPushButton("Undo Last Function")
        self.btn_undo.clicked.connect(self._undo_last)
        right_lay.addWidget(self.btn_undo)
        self.btn_join = QPushButton("Join-average with selected lines…")
        self.btn_join.setToolTip("Open the separate Join Lines draft/preview tool for this line and other selected lines.")
        self.btn_join.clicked.connect(self._open_join_tool)
        right_lay.addWidget(self.btn_join)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setProperty("accent", True)
        self.btn_apply.clicked.connect(self._apply)
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self._save)
        self.btn_discard = QPushButton("Discard")
        self.btn_discard.setToolTip("Discard staged functions since the last Apply; previously applied work remains undoable.")
        self.btn_discard.clicked.connect(self._discard)
        self.btn_return = QPushButton("Return")
        self.btn_return.clicked.connect(self._return)
        for button in (self.btn_apply, self.btn_save, self.btn_discard, self.btn_return):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self._refresh()

    def _selected_vertex(self) -> int | None:
        row = self.tbl_vertices.currentRow()
        return row if 0 <= row < len(self.draft.vertices) else None

    def _selected_segment(self) -> int | None:
        row = self._selected_vertex()
        if row is None or row >= self.draft.segment_count:
            return None
        return row

    def _change_view(self, _index):
        mode = self.cmb_view.currentData()
        if mode in ("plan", "3d"):
            self.preview.set_view_mode(mode)
            self.view_stack.setCurrentWidget(self.preview)
            self.lbl_view_hint.setText(
                "2D plan view of the staged line." if mode == "plan" else
                "Isometric 3D view of the staged line; elevations are shown relative to its lowest vertex.")
            return

        self.view_stack.setCurrentWidget(self.context_canvas)
        self.context_canvas.opts.show_imagery = True
        if mode == "image":
            self.context_canvas.opts.show_points = False
            self.context_canvas.opts.show_lines = False
            self.context_canvas.opts.show_text = False
            self.context_canvas.opts.show_surfaces = False
            visible_images = any(layer.visible for layer in self.state.project.imagery.values())
            self.lbl_view_hint.setText(
                "Georeferenced imagery with the staged line overlaid. Project points and other lines are hidden."
                if visible_images else
                "No visible imagery layer is loaded. Add imagery from the main window; the staged line remains visible here.")
        else:
            self.context_canvas.opts.show_points = True
            self.context_canvas.opts.show_lines = True
            self.context_canvas.opts.show_text = True
            self.context_canvas.opts.show_surfaces = True
            self.context_canvas.opts.show_numbers = True
            self.context_canvas.opts.show_desc = True
            self.lbl_view_hint.setText(
                "All project points, linework, labels and visible imagery, with this line's staged geometry highlighted.")
        self.context_canvas.invalidate()
        QTimer.singleShot(0, self._fit_context_view)

    def _fit_context_view(self):
        if self.cmb_view.currentData() == "all_data":
            self.context_canvas.zoom_extents()
            return
        vertices = self.draft.vertices
        if not vertices:
            self.context_canvas.zoom_extents()
            return
        xmin = min(vertex.x for vertex in vertices)
        xmax = max(vertex.x for vertex in vertices)
        ymin = min(vertex.y for vertex in vertices)
        ymax = max(vertex.y for vertex in vertices)
        span = max(xmax - xmin, ymax - ymin, 10.0)
        padding = span * 0.12
        self.context_canvas.zoom_bbox(
            (xmin - padding, ymin - padding, xmax + padding, ymax + padding), margin=0.02)

    def _paint_draft_overlay(self, painter, view):
        vertices = np.asarray([vertex.xyz() for vertex in self.draft.vertices], dtype=float)
        original = self.draft.base_entity
        ghost = LinePreviewWidget._sample(
            original["verts"], original["bulges"] if original["bulges"] is not None
            else np.zeros(len(original["verts"])), original["closed"])
        staged = LinePreviewWidget._sample(vertices, self.draft.bulges, self.draft.closed)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if len(ghost) > 1:
            path = QPainterPath(QPointF(*view.to_screen(float(ghost[0, 0]), float(ghost[0, 1]))))
            for point in ghost[1:]:
                path.lineTo(QPointF(*view.to_screen(float(point[0]), float(point[1]))))
            painter.setPen(QPen(QColor(130, 140, 150, 180), 2, Qt.DashLine))
            painter.drawPath(path)
        if len(staged) > 1:
            path = QPainterPath(QPointF(*view.to_screen(float(staged[0, 0]), float(staged[0, 1]))))
            for point in staged[1:]:
                path.lineTo(QPointF(*view.to_screen(float(point[0]), float(point[1]))))
            painter.setPen(QPen(QColor(35, 145, 235), 3))
            painter.drawPath(path)
        painter.setPen(QPen(QColor(25, 70, 110), 1))
        painter.setBrush(QColor(245, 190, 75))
        for index, vertex in enumerate(self.draft.vertices):
            sx, sy = view.to_screen(vertex.x, vertex.y)
            painter.drawEllipse(QPointF(sx, sy), 5.0, 5.0)
            painter.drawText(QPointF(sx + 6, sy - 5), str(index + 1))
        painter.restore()

    def _change_mode(self, _index):
        mode = self.cmb_mode.currentData()
        if mode is None:
            return
        try:
            self.draft.set_mode(mode)
        except LineworkEditError as ex:
            QMessageBox.warning(self, "Line Edit Mode", str(ex))
            self.cmb_mode.blockSignals(True)
            self.cmb_mode.setCurrentIndex(0)
            self.cmb_mode.blockSignals(False)
        self._refresh()

    def _select_vertex(self):
        index = self._selected_vertex()
        if index is not None:
            vertex = self.draft.vertices[index]
            self.ed_x.setText(f"{vertex.x:.4f}")
            self.ed_y.setText(f"{vertex.y:.4f}")
            self.ed_z.setText("" if not math.isfinite(vertex.z) else f"{vertex.z:.4f}")
        self._refresh_buttons()

    def _report_error(self, title, error):
        QMessageBox.warning(self, title, str(error))

    def _run_draft(self, callback):
        try:
            callback()
        except (LineworkEditError, ValueError, IndexError) as ex:
            self._report_error("Line Edit", ex)
            return
        self._refresh()

    def _insert_vertex(self):
        index = self._selected_segment()
        if index is None:
            self._report_error("Insert Vertex", "Select the start vertex of a segment first.")
            return
        self._run_draft(lambda: self.draft.insert_vertex(index))
        self.tbl_vertices.selectRow(index + 1)

    def _delete_vertex(self):
        index = self._selected_vertex()
        if index is None:
            self._report_error("Delete Vertex", "Select a vertex to delete first.")
            return
        self._run_draft(lambda: self.draft.delete_vertex(index))
        self.tbl_vertices.selectRow(max(0, min(index, len(self.draft.vertices) - 1)))

    @staticmethod
    def _coordinate(text, label, allow_blank=False):
        value = str(text).strip()
        if allow_blank and not value:
            return None
        try:
            number = float(value)
        except ValueError as ex:
            raise LineworkEditError(f"Enter a valid {label}.") from ex
        if not math.isfinite(number):
            raise LineworkEditError(f"{label.capitalize()} must be finite.")
        return number

    def _move_vertex(self):
        index = self._selected_vertex()
        if index is None:
            self._report_error("Move Vertex", "Select a vertex to move first.")
            return
        try:
            x = self._coordinate(self.ed_x.text(), "easting")
            y = self._coordinate(self.ed_y.text(), "northing")
            z = self._coordinate(self.ed_z.text(), "elevation", allow_blank=True)
        except LineworkEditError as ex:
            self._report_error("Move Vertex", ex)
            return
        if z is None:
            z = float("nan")
        self._run_draft(lambda: self.draft.move_vertex(index, x, y, z))
        self.tbl_vertices.selectRow(index)

    def _interpolate_vertex(self):
        index = self._selected_vertex()
        if index is None:
            self._report_error("Interpolate Point", "Select a vertex to interpolate first.")
            return
        fraction = self.sp_interpolation.value() / 100.0
        self._run_draft(lambda: self.draft.interpolate_vertex(index, fraction))
        self.tbl_vertices.selectRow(index)

    def _nudge_vertex(self, dx, dy):
        index = self._selected_vertex()
        if index is None:
            self._report_error("Nudge Point", "Select a vertex to nudge first.")
            return
        distance = self.sp_nudge.value()
        self._run_draft(lambda: self.draft.nudge_vertex(index, dx * distance, dy * distance))
        self.tbl_vertices.selectRow(index)

    def _insert_end(self, at_start):
        try:
            xyz = (self._coordinate(self.ed_x.text(), "easting"),
                   self._coordinate(self.ed_y.text(), "northing"),
                   self._coordinate(self.ed_z.text(), "elevation", allow_blank=True))
        except LineworkEditError as ex:
            self._report_error("Insert Endpoint", ex)
            return
        if xyz[2] is None:
            xyz = (*xyz[:2], float("nan"))
        before = len(self.draft.vertices)
        self._run_draft(lambda: self.draft.insert_end(xyz, at_start=at_start))
        self.tbl_vertices.selectRow(0 if at_start else before)

    def _swap_vertex(self):
        index = self._selected_vertex()
        if index is None or index == 0:
            self._report_error("Swap Vertex", "Select a vertex after the first vertex.")
            return
        self._run_draft(lambda: self.draft.swap_vertices(index - 1, index))
        self.tbl_vertices.selectRow(index - 1)

    def _reverse(self):
        self._run_draft(self.draft.reverse)

    def _set_curve(self):
        index = self._selected_segment()
        if index is None:
            self._report_error("Curve Control", "Select the start vertex of a segment first.")
            return
        self._run_draft(lambda: self.draft.set_curve(index, self.sp_sweep.value()))

    def _straighten(self):
        index = self._selected_segment()
        if index is None:
            self._report_error("Curve Control", "Select the start vertex of a segment first.")
            return
        self._run_draft(lambda: self.draft.straighten(index))

    def _toggle_closed(self):
        self._run_draft(self.draft.toggle_closed)

    def _undo_last(self):
        self.draft.undo_last()
        self._refresh()

    def _open_join_tool(self):
        if self.draft.has_pending_actions:
            self._report_error("Join Lines", "Apply or discard the current staged functions before opening Join Lines.")
            return
        ids = sorted(self.state.sel_entities)
        if self.draft.entity_id not in ids:
            ids.append(self.draft.entity_id)
        entities = [self.state.project.entities.get(i) for i in ids]
        polylines = [entity for entity in entities if isinstance(entity, Polyline)]
        if len(polylines) < 2:
            self._report_error("Join Lines", "Select at least one other line, then open this tool again.")
            return
        dialog = JoinLinesDialog(self.state, polylines, self)
        if dialog.exec():
            self._allow_close = True
            self.accept()

    def _apply(self):
        if not self.draft.has_pending_actions:
            return
        try:
            commit_line_draft(self.state, self.draft)
        except (LineworkEditError, ValueError) as ex:
            self._report_error("Apply Line Edits", ex)
            return
        entity = self.state.project.entities.get(self.draft.entity_id)
        if isinstance(entity, Polyline):
            self.draft = LineEditDraft(self.state.project, entity)
            self.cmb_mode.setCurrentIndex(0)
        self._refresh()

    def _save(self):
        if self.draft.has_pending_actions:
            self._apply()
            if self.draft.has_pending_actions:
                return
        save = getattr(self.parent_window, "save", None)
        if callable(save):
            if not save():
                return
        elif self.state.project.path:
            try:
                self.state.save_project()
            except Exception as ex:
                self._report_error("Save Project", ex)
                return
        else:
            self._report_error("Save Project", "Use Save As from the main window to choose a project path.")
            return
        self.accept()

    def _discard(self):
        if not self.draft.has_pending_actions:
            return
        answer = QMessageBox.question(
            self, "Discard Staged Functions",
            "Discard functions staged since the last Apply? Previously applied changes remain in the project.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.draft.reset()
            self.cmb_mode.setCurrentIndex(0)
            self._refresh()

    def _return(self):
        if self.draft.has_pending_actions:
            answer = QMessageBox.question(
                self, "Return to Drawing",
                "Discard staged functions since the last Apply and return to the drawing?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self._allow_close = True
        self.reject()

    def closeEvent(self, event):
        if self._allow_close or not self.draft.has_pending_actions:
            event.accept()
            return
        answer = QMessageBox.question(
            self, "Return to Drawing",
            "Discard staged functions since the last Apply and return to the drawing?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            event.accept()
        else:
            event.ignore()

    def _refresh_buttons(self):
        vertex = self._selected_vertex()
        segment = self._selected_segment()
        self.btn_insert.setEnabled(segment is not None)
        self.btn_delete.setEnabled(vertex is not None and len(self.draft.vertices) > (3 if self.draft.closed else 2))
        self.btn_swap.setEnabled(vertex is not None and vertex > 0)
        self.btn_curve.setEnabled(segment is not None)
        self.btn_straight.setEnabled(segment is not None and abs(self.draft.bulges[segment]) > 1e-12)
        can_interpolate = (vertex is not None and
                           (self.draft.closed or vertex not in (0, len(self.draft.vertices) - 1)))
        self.btn_interpolate.setEnabled(can_interpolate)
        for button in (self.btn_nudge_west, self.btn_nudge_east,
                       self.btn_nudge_south, self.btn_nudge_north):
            button.setEnabled(vertex is not None)
        self.btn_end.setEnabled(not self.draft.closed)
        self.btn_start.setEnabled(not self.draft.closed)
        self.btn_close.setText("Open line" if self.draft.closed else "Close line")
        self.btn_undo.setEnabled(bool(self.draft._undo))
        self.btn_apply.setEnabled(self.draft.has_pending_actions)

    def _refresh(self):
        vertices = self.draft.vertices
        current = self._selected_vertex()
        self.tbl_vertices.blockSignals(True)
        self.tbl_vertices.setRowCount(len(vertices))
        for row, vertex in enumerate(vertices):
            point_id = vertex.point_id
            point_text = f"{vertex.number} (ID {point_id})" if point_id is not None else (vertex.number or "—")
            bulge = self.draft.bulges[row] if row < self.draft.segment_count else 0.0
            sweep = math.degrees(4.0 * math.atan(bulge)) if abs(bulge) > 1e-14 else 0.0
            values = [str(row + 1), point_text, f"{vertex.x:.4f}", f"{vertex.y:.4f}",
                      "—" if not math.isfinite(vertex.z) else f"{vertex.z:.4f}", f"{sweep:.2f}"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.tbl_vertices.setItem(row, column, item)
        if vertices:
            self.tbl_vertices.selectRow(min(current if current is not None else 0, len(vertices) - 1))
        self.tbl_vertices.blockSignals(False)
        if vertices:
            self._select_vertex()
        self.preview.set_line(
            np.asarray([vertex.xyz() for vertex in vertices]), self.draft.bulges, self.draft.closed)
        self.preview.ghost = (self.draft.base_entity["verts"],
                              self.draft.base_entity["bulges"] if self.draft.base_entity["bulges"] is not None
                              else np.zeros(len(self.draft.base_entity["verts"])),
                              self.draft.base_entity["closed"])
        self.preview.update()
        self.context_canvas.update()
        self.lst_actions.clear()
        self.lst_actions.addItems(self.draft.actions)
        if self.draft.point_mode_ready:
            self.lbl_link_status.setText(f"{len(self.draft.vertices)} original vertex links are unambiguous.")
        else:
            self.lbl_link_status.setText("Point edit is disabled: " + (self.draft.link_issues[0] if self.draft.link_issues else "no complete point links."))
        self._refresh_buttons()


class JoinLinesDialog(QDialog):
    """A separate staged Join Lines workflow: order, reverse, preview, then accept."""

    def __init__(self, state, polylines, parent=None):
        super().__init__(parent)
        self.state = state
        self.draft = JoinLinesDraft(state.project, list(polylines))
        self.preview_result = None
        self._preview_key = None
        self.setWindowTitle("Join Lines — Draft and Preview")
        self.resize(1120, 760)
        root = QVBoxLayout(self)
        root.addWidget(Hint(
            "Arrange line order and direction, choose how endpoints join, then build a preview. "
            "No project geometry changes until Accept Join."))
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_lay = QVBoxLayout(left)
        self.tbl_sources = QTableWidget(0, 5)
        self.tbl_sources.setHorizontalHeaderLabels(["Order", "Line ID", "Layer", "Vertices", "Reverse"])
        self.tbl_sources.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_sources.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_sources.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_sources.verticalHeader().setVisible(False)
        self.tbl_sources.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_sources.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_sources.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_sources.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_sources.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        left_lay.addWidget(self.tbl_sources, 1)
        order_row = QHBoxLayout()
        self.btn_up = QPushButton("Move Up")
        self.btn_up.clicked.connect(lambda: self._move_source(-1))
        self.btn_down = QPushButton("Move Down")
        self.btn_down.clicked.connect(lambda: self._move_source(1))
        self.btn_auto_orient = QPushButton("Auto Orient")
        self.btn_auto_orient.setToolTip("Reverse each following line when that makes its nearer end join the current end.")
        self.btn_auto_orient.clicked.connect(self._auto_orient)
        order_row.addWidget(self.btn_up)
        order_row.addWidget(self.btn_down)
        order_row.addWidget(self.btn_auto_orient)
        left_lay.addLayout(order_row)
        self.preview = LinePreviewWidget()
        left_lay.addWidget(self.preview, 2)
        split.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        options = QGroupBox("Join options")
        form = QFormLayout(options)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("Graphical only", "graphics")
        self.cmb_mode.addItem("Point editing — move linked points with the line", "points")
        all_linked = all(not source.link_issues for source in self.draft.sources.values())
        if not all_linked:
            self.cmb_mode.model().item(1).setEnabled(False)
        self.cmb_mode.currentIndexChanged.connect(self._update_mode_controls)
        form.addRow("Edit mode:", self.cmb_mode)
        self.chk_average = QCheckBox("Average connected endpoints (join-average)")
        self.chk_average.setChecked(True)
        self.chk_average.toggled.connect(self._invalidate_preview)
        form.addRow("", self.chk_average)
        self.chk_condense = QCheckBox("Condense consecutive near-duplicate vertices")
        self.chk_condense.toggled.connect(self._invalidate_preview)
        form.addRow("", self.chk_condense)
        self.sp_tolerance = QDoubleSpinBox()
        self.sp_tolerance.setRange(0.0, 1000.0)
        self.sp_tolerance.setDecimals(4)
        self.sp_tolerance.setValue(0.005)
        self.sp_tolerance.valueChanged.connect(self._invalidate_preview)
        form.addRow("Condense tolerance:", self.sp_tolerance)
        self.chk_normalize = QCheckBox("Normalize selected point numbers to a new range")
        self.chk_normalize.toggled.connect(self._number_normalization_toggled)
        form.addRow("", self.chk_normalize)
        self.sp_start_number = QSpinBox()
        self.sp_start_number.setRange(1, 2_000_000_000)
        try:
            self.sp_start_number.setValue(int(state.project.next_point_number()))
        except (TypeError, ValueError):
            self.sp_start_number.setValue(1)
        self.sp_start_number.valueChanged.connect(self._invalidate_preview)
        form.addRow("Range starts at:", self.sp_start_number)
        self.chk_replace = QCheckBox("Replace selected source lines")
        self.chk_replace.setChecked(True)
        self.chk_replace.toggled.connect(self._invalidate_preview)
        form.addRow("", self.chk_replace)
        right_lay.addWidget(options)
        self.lbl_status = QLabel("Build a preview to inspect the joined geometry.")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setProperty("hint", "true")
        right_lay.addWidget(self.lbl_status)
        right_lay.addStretch(1)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_preview = QPushButton("Preview Join")
        self.btn_preview.clicked.connect(self._build_preview)
        self.btn_accept = QPushButton("Accept Join")
        self.btn_accept.setProperty("accent", True)
        self.btn_accept.setEnabled(False)
        self.btn_accept.clicked.connect(self._accept_join)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_preview)
        buttons.addWidget(self.btn_accept)
        buttons.addWidget(self.btn_cancel)
        root.addLayout(buttons)
        self._refresh_sources()
        self._update_mode_controls()

    def _current_id(self):
        row = self.tbl_sources.currentRow()
        return self.draft.order[row] if 0 <= row < len(self.draft.order) else None

    def _number_normalization_toggled(self, checked):
        self.sp_start_number.setEnabled(bool(checked) and self.cmb_mode.currentData() == "points")
        self._invalidate_preview()

    def _update_mode_controls(self, *_):
        can_edit_points = (self.cmb_mode.currentData() == "points" and
                           all(not source.link_issues for source in self.draft.sources.values()))
        if not can_edit_points and self.chk_normalize.isChecked():
            self.chk_normalize.blockSignals(True)
            self.chk_normalize.setChecked(False)
            self.chk_normalize.blockSignals(False)
        self.chk_normalize.setEnabled(can_edit_points)
        self.sp_start_number.setEnabled(can_edit_points and self.chk_normalize.isChecked())
        self._invalidate_preview()

    def _refresh_sources(self):
        self.tbl_sources.blockSignals(True)
        self.tbl_sources.setRowCount(len(self.draft.order))
        for row, entity_id in enumerate(self.draft.order):
            source = self.draft.sources[entity_id]
            for col, value in enumerate((str(row + 1), str(entity_id), source.layer, str(len(source.verts)))):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.tbl_sources.setItem(row, col, item)
            reverse = QCheckBox()
            reverse.setChecked(entity_id in self.draft.reversed_ids)
            reverse.setToolTip("Reverse this source line before joining")
            reverse.toggled.connect(lambda checked, eid=entity_id: self._set_reversed(eid, checked))
            self.tbl_sources.setCellWidget(row, 4, reverse)
        self.tbl_sources.blockSignals(False)
        if self.draft.order:
            self.tbl_sources.selectRow(0)

    def _set_reversed(self, entity_id, checked):
        self.draft.set_reversed(entity_id, checked)
        self._invalidate_preview()

    def _move_source(self, offset):
        entity_id = self._current_id()
        if entity_id is None:
            return
        old_index = self.draft.order.index(entity_id)
        self.draft.reorder(entity_id, offset)
        self._refresh_sources()
        new_index = self.draft.order.index(entity_id)
        self.tbl_sources.selectRow(new_index)
        self._invalidate_preview()

    def _auto_orient(self):
        self.draft.auto_orient()
        self._refresh_sources()
        self._invalidate_preview()

    def _invalidate_preview(self, *_):
        self.preview_result = None
        self._preview_key = None
        self.btn_accept.setEnabled(False)
        self.lbl_status.setText("Options changed — build a new preview before accepting.")

    def _build_preview(self):
        point_mode = self.cmb_mode.currentData() == "points"
        try:
            result = self.draft.build_preview(
                average=self.chk_average.isChecked(),
                condense=self.chk_condense.isChecked(),
                tolerance=self.sp_tolerance.value(),
                point_mode=point_mode,
                normalize_numbers=self.chk_normalize.isChecked(),
                start_number=self.sp_start_number.value())
        except LineworkEditError as ex:
            QMessageBox.warning(self, "Join Lines Preview", str(ex))
            self._invalidate_preview()
            return
        self.preview_result = result
        self._preview_key = self._option_key()
        ghost = self._source_ghost()
        self.preview.set_line(result.verts, result.bulges, result.closed, ghost)
        messages = [f"Preview: {len(result.verts)} vertices, {len(result.source_entity_ids)} source lines."]
        if result.join_gaps:
            messages.append("Join gaps: " + ", ".join(f"{value:.4f}" for value in result.join_gaps) + " units.")
        if result.duplicate_numbers:
            numbers = ", ".join(sorted(result.duplicate_numbers))
            messages.append(f"Duplicate input point numbers: {numbers}.")
        if result.number_conflicts:
            numbers = ", ".join(sorted(result.number_conflicts))
            messages.append(f"Range conflicts with unselected points: {numbers}.")
        if result.code_violations:
            messages.extend(result.code_violations)
        if point_mode and result.link_issues:
            messages.append("Point editing unavailable: " + result.link_issues[0])
        self.lbl_status.setText("\n".join(messages))
        self.btn_accept.setEnabled(not (point_mode and result.link_issues) and not result.number_conflicts)

    def _source_ghost(self):
        lines = []
        for entity_id in self.draft.order:
            source = self.draft.sources[entity_id]
            lines.append((source.verts, source.bulges, source.closed))
        return lines or None

    def _option_key(self):
        return (tuple(self.draft.order), tuple(sorted(self.draft.reversed_ids)),
                self.cmb_mode.currentData(), self.chk_average.isChecked(), self.chk_condense.isChecked(),
                self.sp_tolerance.value(), self.chk_normalize.isChecked(), self.sp_start_number.value(),
                self.chk_replace.isChecked())

    def _accept_join(self):
        preview = self.preview_result
        if preview is None or self._preview_key != self._option_key():
            self._invalidate_preview()
            QMessageBox.information(self, "Join Lines", "Build a current preview before accepting.")
            return
        point_mode = self.cmb_mode.currentData() == "points"
        if preview.number_conflicts:
            numbers = ", ".join(sorted(preview.number_conflicts))
            QMessageBox.warning(self, "Point Number Conflict",
                                f"The requested normalized range conflicts with unselected point numbers: {numbers}.")
            return
        if preview.code_violations:
            answer = QMessageBox.question(
                self, "Confirm Field-to-Finish Code Violation",
                "The selected lines do not share one consistent code/string identity. The accepted join "
                "will be stored as a manual linework override; existing point descriptions are retained.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        if preview.duplicate_numbers and not self.chk_normalize.isChecked():
            duplicates = ", ".join(sorted(preview.duplicate_numbers))
            answer = QMessageBox.question(
                self, "Confirm Existing Duplicate Point Numbers",
                f"Point numbers used by the selected lines are duplicated in the project ({duplicates}). "
                "No point numbers will be changed. Continue with the join?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        try:
            commit_join_draft(
                self.state, self.draft, preview,
                replace_sources=self.chk_replace.isChecked(), point_mode=point_mode,
                normalize_numbers=self.chk_normalize.isChecked())
        except (LineworkEditError, ValueError) as ex:
            QMessageBox.warning(self, "Accept Join", str(ex))
            return
        self.accept()
