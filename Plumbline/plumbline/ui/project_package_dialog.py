"""Save As dialog for a uniquely named project-package folder."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from ..core import jobtemplate as JT


class ProjectPackageDialog(QDialog):
    """Choose the parent folder and unique name for a self-contained project package."""

    def __init__(self, parent=None, *, name: str = "Untitled Project", parent_folder: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Save Project Package As")
        self.setMinimumWidth(620)
        self.setSizeGripEnabled(True)
        self._project_name = ""
        self._parent_folder = ""

        root = QVBoxLayout(self)
        intro = QLabel(
            "Save As creates a new project folder with the standard job subfolders. "
            "Files in the current job folder and referenced field books, field data, and "
            "local imagery are copied into the new package."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        self.ed_name = QLineEdit(str(name or "Untitled Project"))
        self.ed_name.setPlaceholderText("Project folder name")
        form.addRow("Project name:", self.ed_name)

        path_row = QHBoxLayout()
        self.ed_parent = QLineEdit(str(parent_folder or Path.cwd()))
        self.ed_parent.setPlaceholderText("Choose a parent folder")
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._browse)
        path_row.addWidget(self.ed_parent, 1)
        path_row.addWidget(self.btn_browse)
        path_holder = QWidget()
        path_holder.setLayout(path_row)
        form.addRow("Create package in:", path_holder)
        root.addLayout(form)

        self.lbl_preview = QLabel()
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setStyleSheet("color: #777;")
        root.addWidget(self.lbl_preview)

        folder_lines = [f"  {folder.name}/ - {folder.purpose}" for folder in JT.JOB_TEMPLATE.folders]
        self.lbl_folders = QLabel("Standard folders created:\n" + "\n".join(folder_lines))
        self.lbl_folders.setObjectName("standardFolderPreview")
        self.lbl_folders.setWordWrap(True)
        self.lbl_folders.setStyleSheet("color: #777; font-family: Consolas, monospace;")
        root.addWidget(self.lbl_folders)

        self.ed_name.textChanged.connect(self._refresh_preview)
        self.ed_parent.textChanged.connect(self._refresh_preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Save As")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh_preview()

    def _browse(self):
        start = self.ed_parent.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose a parent folder for the project package", start)
        if folder:
            self.ed_parent.setText(folder)

    def _refresh_preview(self, *_):
        parent = Path(self.ed_parent.text().strip() or Path.cwd()).expanduser()
        safe_name = JT._safe_name(self.ed_name.text())
        self.lbl_preview.setText(f"New folder: {parent / safe_name}")

    def _accept(self):
        typed_name = self.ed_name.text().strip()
        parent_text = self.ed_parent.text().strip()
        if not typed_name:
            QMessageBox.warning(self, "Save Project Package", "Enter a project name.")
            self.ed_name.setFocus()
            return
        if not parent_text:
            QMessageBox.warning(self, "Save Project Package", "Choose a parent folder.")
            self.ed_parent.setFocus()
            return
        parent_folder = Path(parent_text).expanduser()
        if parent_folder.exists() and not parent_folder.is_dir():
            QMessageBox.warning(self, "Save Project Package", f"{parent_folder} is a file, not a folder.")
            return
        safe_name = JT._safe_name(typed_name)
        destination = parent_folder / safe_name
        if destination.exists():
            QMessageBox.warning(
                self, "Project Folder Already Exists",
                f"{destination} already exists.\n\nChoose another project name or parent folder; "
                "Save As never merges with or overwrites an existing folder."
            )
            self.ed_name.setFocus()
            self.ed_name.selectAll()
            return
        self._project_name = safe_name
        self._parent_folder = str(parent_folder)
        self.accept()

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def parent_folder(self) -> str:
        return self._parent_folder
