"""Right-side performance benchmark panel."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..core.performance import PROFILES, run_project_benchmark
from .widgets import error_box, run_blocking


class PerformanceDock(QWidget):
    """Runs a non-mutating current-project workload benchmark and exposes its report."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._markdown_path: Path | None = None
        self._report_dir: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        intro = QLabel(
            "Benchmark this exact job on a field laptop or a workstation. It does not modify the project. "
            "A Markdown and JSON report are written so individual subsystems can be compared."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Profile:"))
        self.profile = QComboBox()
        self.profile.addItems(list(PROFILES))
        self.profile.setCurrentText("Standard")
        self.profile.setToolTip("Quick: one pass. Standard: normal repeatable comparison. Deep: seven passes for lower timing noise.")
        controls.addWidget(self.profile, 1)
        self.run_button = QPushButton("Run benchmark")
        self.run_button.setToolTip("Measure the current project and write a performance report without editing it.")
        self.run_button.clicked.connect(self.run_benchmark)
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Operation", "Mean ms", "Best", "Worst", "Detail"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.machine = QLabel("No benchmark has been run for this session.")
        self.machine.setWordWrap(True)
        layout.addWidget(self.machine)

        actions = QHBoxLayout()
        self.open_report = QPushButton("Open report")
        self.open_report.setEnabled(False)
        self.open_report.clicked.connect(self._open_report)
        actions.addWidget(self.open_report)
        self.open_folder = QPushButton("Open report folder")
        self.open_folder.setEnabled(False)
        self.open_folder.clicked.connect(self._open_folder)
        actions.addWidget(self.open_folder)
        layout.addLayout(actions)

    def run_benchmark(self):
        profile = self.profile.currentText()
        self.run_button.setEnabled(False)
        try:
            report, markdown_path, _json_path = run_blocking(
                self, f"Running {profile.lower()} performance benchmark…",
                run_project_benchmark, self.state.project, profile,
            )
        except RuntimeError as exc:
            error_box(self, "Performance benchmark", "The benchmark did not complete.", str(exc))
            return
        finally:
            self.run_button.setEnabled(True)

        self._markdown_path = markdown_path
        self._report_dir = markdown_path.parent
        self.table.setRowCount(len(report.measurements))
        for row, metric in enumerate(report.measurements):
            for column, value in enumerate((metric.operation, f"{metric.mean_ms:.3f}",
                                             f"{metric.best_ms:.3f}", f"{metric.worst_ms:.3f}", metric.detail)):
                item = QTableWidgetItem(str(value))
                if column in (1, 2, 3):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, item)
        self.machine.setText(
            f"Saved {markdown_path.name}\n"
            f"{report.machine['cpu']} · {report.machine['logical_cpus']} logical CPUs · "
            f"{report.machine['memory_gb']} · {report.machine['os']}"
        )
        self.open_report.setEnabled(True)
        self.open_folder.setEnabled(True)
        self.state.log(f"Performance report written: {markdown_path}", "ok")

    def _open_report(self):
        if self._markdown_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._markdown_path)))

    def _open_folder(self):
        if self._report_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._report_dir)))
