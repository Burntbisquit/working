"""File > Project Folder... - one door for the folder the job lives in.

The change order (item 12) put it plainly: *"too many folder options - Location and project must
be one folder with subfolders, not two entries."*  The two entries were **Select Job Folder** and
**New Job Folder**, and the third folder question lived inside New Project as "Location".  They are
all the same decision - *where does this job live* - so they are one dialog:

* **Open the folder this job is in** (and reveal it in the desktop's file manager);
* **Open another folder** - and if it holds a project file, offer to open that project, with
  overwrite offered second and explained in full (``ui.job_setup.overwrite_job_folder``);
* **Create a job folder** named after the job, inside the configured default project-folder
  location, with the fixed standard folder tree as its subfolders.

Nothing here writes anything by itself: creating goes through ``run_job_setup``, which is
cancelable and rolls back, and overwriting goes through the three-step confirmation.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)

from ..core.settings import default_project_folder
from .job_setup import JobSetupPanel, run_job_setup
from .widgets import Hint, error_box


class ProjectFolderDialog(QDialog):
    """Open / create / reveal the project folder.  Sets ``chosen`` and ``open_project``."""

    def __init__(self, parent=None, current=None, default_name: str = "New Job"):
        super().__init__(parent)
        self.setWindowTitle("Project Folder")
        self.resize(720, 520)
        self.chosen: str | None = None
        self.open_project: str | None = None
        self.current = Path(current) if current else None

        root = QVBoxLayout(self)
        here = str(self.current) if self.current else "(this project has not been saved yet)"
        root.addWidget(QLabel(f"<b>This job's folder:</b>  {here}"))
        row = QHBoxLayout()
        self.b_open_here = QPushButton("Open it in the file manager")
        self.b_open_here.setEnabled(bool(self.current))
        self.b_pick = QPushButton("Open another folder...")
        for b in (self.b_open_here, self.b_pick):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)
        root.addWidget(Hint("A job folder holds the project file and, inside it, the folders for the "
                            "work: Field Data, Field Book, Control, Reports, Drawings, Surfaces, "
                            "Imagery.  The subfolders start empty; only Job Setup.txt and the project "
                            "file are created at the root.  Opening a folder that holds a project file "
                            "offers to open that project; starting fresh there deletes what is in it, "
                            "and asks twice before it does."))

        root.addWidget(QLabel("<b>Create a job folder</b>"))
        self.ed_name = QLineEdit(default_name)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Job name:"))
        name_row.addWidget(self.ed_name, 1)
        root.addLayout(name_row)
        self.setup = JobSetupPanel(self, str(default_project_folder()))
        self.ed_name.textChanged.connect(lambda t: self.setup.set_job_name(t))
        self.setup.set_job_name(self.ed_name.text())
        root.addWidget(self.setup, 1)

        bottom = QHBoxLayout()
        self.b_create = QPushButton("Create job folder")
        self.b_create.setProperty("accent", True)
        self.b_close = QPushButton("Close")
        bottom.addStretch(1)
        bottom.addWidget(self.b_create)
        bottom.addWidget(self.b_close)
        root.addLayout(bottom)

        self.b_open_here.clicked.connect(self._reveal)
        self.b_pick.clicked.connect(self._pick_folder)
        self.b_create.clicked.connect(self._create)
        self.b_close.clicked.connect(self.reject)

    # ------------------------------------------------------------------ actions
    def _reveal(self):
        if self.current is None:
            return
        self.chosen = str(self.current)
        self.open_project = None
        self.accept()

    def _pick_folder(self):
        start = str(self.current) if self.current else str(default_project_folder())
        folder = QFileDialog.getExistingDirectory(self, "Open a project folder", start)
        if not folder:
            return
        folder = Path(folder)
        projects = sorted(folder.glob("*.plb"))
        self.chosen = str(folder)
        if projects:
            from .job_setup import overwrite_job_folder     # the same questions, wherever they are asked
            # Opening is offered first; overwriting is the second door and it asks three times.
            from PySide6.QtWidgets import QMessageBox
            m = QMessageBox(self)
            m.setIcon(QMessageBox.Question)
            m.setWindowTitle("Project Folder")
            m.setText(f"{folder.name} holds {projects[0].name}.")
            m.setInformativeText("Open that project, or start fresh in this folder?")
            b_open = m.addButton("Open that project", QMessageBox.AcceptRole)
            b_fresh = m.addButton("Start fresh - delete what is here", QMessageBox.DestructiveRole)
            m.addButton("Cancel", QMessageBox.RejectRole)
            m.setDefaultButton(b_open)
            m.exec()
            clicked = m.clickedButton()
            if clicked is b_open:
                self.open_project = str(projects[0])
                self.accept()
                return
            if clicked is not b_fresh:
                return
            if not overwrite_job_folder(self, str(folder.parent), folder.name):
                return
            self.open_project = None
            self.chosen = str(folder)
            self.accept()
            return
        self.open_project = None
        self.accept()

    def _create(self):
        name = self.ed_name.text().strip()
        if not name:
            error_box(self, "Project Folder", "Give the job a name - it is also the job folder's name.")
            return
        reason = self.setup.validate()
        if reason:
            error_box(self, "Project Folder", reason)
            return
        creation = run_job_setup(self, self.setup.parent_folder, name, self.setup.template,
                                 self.setup.weeks, "UNASSIGNED (no CRS)")
        if creation is None:
            return
        self.chosen = str(creation.paths.root)
        self.open_project = str(creation.paths.project_file)
        self.accept()


__all__ = ["ProjectFolderDialog"]
