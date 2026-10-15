"""New-project dialog.

Project CRS is deliberately not selected here. A new project starts unassigned, then the
Project Coordinate System dialog opens as a separate step after the job is created.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QScrollArea, QVBoxLayout, QWidget)

from ..core import crs as C
from .job_setup import JobSetupPanel
from .widgets import error_box


class NewProjectDialog(QDialog):
    _no_autofit = True

    def __init__(self, parent=None, name: str = "Untitled", job_hint: str = ""):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.resize(780, 560)
        self.setMinimumSize(680, 460)
        self.crs: C.ProjectCRS = C.ProjectCRS.unassigned("ftUS")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        inner = QWidget()
        root = QVBoxLayout(inner)
        root.setSpacing(10)
        root.setContentsMargins(4, 4, 10, 4)

        form = QFormLayout()
        self.ed_name = QLineEdit(name)
        self.ed_name.setPlaceholderText("e.g. 23-036.03 Murchison")
        form.addRow("Job name:", self.ed_name)
        root.addLayout(form)

        self.setup = JobSetupPanel(self, job_hint)
        self.ed_name.textChanged.connect(lambda text: self.setup.set_job_name(text))
        self.setup.set_job_name(self.ed_name.text())
        root.addWidget(self.setup)

        self.crs_step_note = QLabel(
            "The project starts without a coordinate system. The Project Coordinate System "
            "screen opens after creation so you can choose it as a separate step.")
        root.addWidget(self.crs_step_note)
        root.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def _accept(self):
        reason = self.setup.validate()
        if reason:
            error_box(self, "New Project", reason)
            return
        self.crs = C.ProjectCRS.unassigned("ftUS")
        self.accept()

    @property
    def project_name(self) -> str:
        return self.ed_name.text().strip() or "Untitled"

    @property
    def setup_job(self) -> bool:
        return self.setup.enabled

    @property
    def job_folder(self) -> str:
        """The job folder this dialog will create (parent + the job's name)."""
        return str(self.setup.folder_path(self.project_name))

    @property
    def setup_parent_folder(self) -> str:
        return self.setup.parent_folder

    @property
    def setup_template(self):
        return self.setup.template

    @property
    def setup_weeks(self) -> int | None:
        return self.setup.weeks       # always None - Field Data/ is created empty
