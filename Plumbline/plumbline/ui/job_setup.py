"""New Project -> the cancelable job-folder setup.

Making a job folder creates the standard subfolders, a short setup note, and the root
project file. The subfolders start empty; no placeholder Field Book or control file is
seeded. Doing this inside a dialog's OK handler freezes the window with no explanation
and no way out, so this runs it behind a progress dialog with a real, working **Cancel**.

Cancel means cancel
-------------------
Pressing Cancel stops at the next step boundary and *removes everything this run
created* - the job folder does not survive in a half-made state.  A half-made job
folder is worse than no job folder: the next attempt sees a non-empty directory and
refuses, and the user has to go and delete it by hand to find out why.

Nothing that already existed is touched, cancelled or not.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ..core import jobtemplate as JT
from ..core.settings import default_project_folder


# --------------------------------------------------------------------------------------------- options panel
class JobSetupPanel(QWidget):
    """Choose where to create a job and preview the one standard folder structure."""

    def __init__(self, parent=None, default_parent_folder: str = ""):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        row = QHBoxLayout()
        self.ed_folder = QLineEdit(default_parent_folder or str(default_project_folder()))
        self.ed_folder.setPlaceholderText("Parent folder - the job folder is created inside it")
        btn = QPushButton("Browse...")
        btn.clicked.connect(self._browse)
        row.addWidget(self.ed_folder, 1)
        row.addWidget(btn)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("Create project in:", holder)
        self.sp_weeks = None                       # compatibility: Field Data starts without week folders
        root.addLayout(form)

        self.lbl_preview = QLabel()
        self.lbl_preview.setObjectName("standardFolderPreview")
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setStyleSheet("color: #888; font-family: Consolas, monospace;")
        root.addWidget(self.lbl_preview)

        self.ed_folder.textChanged.connect(self._refresh_preview)
        self._job_name = ""
        self._refresh_preview()

    def _browse(self):
        start = self.ed_folder.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Where should the job folder go?", start)
        if folder:
            self.ed_folder.setText(folder)

    def set_job_name(self, name: str):
        """Show the actual ``<parent>/<job name>/`` package that will be created."""
        self._job_name = str(name or "").strip()
        self._refresh_preview()

    def folder_path(self, name: str | None = None) -> Path:
        """The job folder this panel would create (parent + safe job name)."""
        safe = "".join(("_" if c in '<>:"/\\|?*' else c) for c in str(name or self._job_name or "Untitled Job"))
        return Path(self.parent_folder).expanduser() / (safe.strip().strip(".") or "Untitled Job")

    def _refresh_preview(self):
        here = self.folder_path()
        rows = [f"Project folder: {here}/", f"Folders:"]
        rows.extend(f"  {folder.name}/ - {folder.purpose}" for folder in JT.JOB_TEMPLATE.folders)
        rows.append(f"Files at root: Job Setup.txt and {here.name}{JT.PROJECT_EXT}")
        rows.append("All subfolders start empty (no placeholder Field Book or control files).")
        self.lbl_preview.setText("\n".join(rows))

    @property
    def enabled(self) -> bool:
        """The standard package is always created; folder setup is not optional."""
        return True

    @property
    def parent_folder(self) -> str:
        return self.ed_folder.text().strip()

    @property
    def template(self) -> JT.JobTemplate:
        return JT.JOB_TEMPLATE

    @property
    def weeks(self) -> int | None:
        """Field Data starts empty; imported folder names are kept as supplied."""
        return None

    def validate(self) -> str | None:
        """A human-readable reason the settings cannot be used, or None."""
        if not self.parent_folder:
            return "Choose where the job folder should be created."
        parent = Path(self.parent_folder).expanduser()
        if parent.exists() and not parent.is_dir():
            return f"{parent} is a file, not a folder."
        if not self._job_name:
            return "Give the job a name - it is also the job folder's name."
        return None


# --------------------------------------------------------------------------------------------- progress
class _Worker(QObject):
    """Runs create_job off the GUI thread so Cancel can be pressed while it works."""

    step = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, parent_folder, name, template, weeks, crs_label, crs_record=None):
        super().__init__()
        self._args = (parent_folder, name, template, weeks, crs_label, crs_record)
        self.cancel_requested = False

    def run(self):
        try:
            out = JT.create_job(
                self._args[0], self._args[1], template=self._args[2], weeks=self._args[3],
                crs_label=self._args[4], crs_record=self._args[5],
                progress=lambda f, m: self.step.emit(f, m),
                is_cancelled=lambda: self.cancel_requested)
        except JT.JobCreationCancelled:
            self.failed.emit("cancelled")
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished.emit(out)


class JobSetupProgressDialog(QDialog):
    """Progress + Cancel while the job folder is built."""

    def __init__(self, parent, parent_folder: str, name: str, template: JT.JobTemplate,
                 weeks: int | None, crs_label: str, crs_record: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Setting Up the Job Folder")
        self.setModal(True)
        self.resize(520, 190)
        self.creation = None
        self.cancelled = False
        self.error: str | None = None

        root = QVBoxLayout(self)
        self.lbl = QLabel(f"Creating {name}...")
        self.lbl.setWordWrap(True)
        root.addWidget(self.lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        root.addWidget(self.bar)
        self.lbl_path = QLabel()
        self.lbl_path.setWordWrap(True)
        self.lbl_path.setStyleSheet("color: #888;")
        root.addWidget(self.lbl_path)
        root.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)

        self._thread = QThread(self)
        self._worker = _Worker(parent_folder, name, template, weeks, crs_label, crs_record)
        self._worker.moveToThread(self._thread)
        self._worker.step.connect(self._on_step)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(self._thread.deleteLater)

    def start(self) -> bool:
        self._thread.start()
        return self.exec() == QDialog.DialogCode.Accepted

    def _on_step(self, fraction: float, message: str):
        self.bar.setValue(int(fraction * 100))
        self.lbl.setText(message[:1].upper() + message[1:] + "...")

    def _cancel(self):
        self.cancelled = True
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText("Cancelling...")
        self._worker.cancel_requested = True
        self.lbl.setText("Cancelling and removing what was created...")

    def _on_finished(self, creation: JT.JobCreation):
        self.creation = creation
        self._thread.quit()
        self.accept()

    def _on_failed(self, message: str):
        self._thread.quit()
        if message == "cancelled":
            self.cancelled = True
            self.reject()
        else:
            self.error = message
            self.reject()

    def closeEvent(self, event):                       # closing the window is cancelling
        if self._thread.isRunning():
            self._cancel()
            event.ignore()
            return
        super().closeEvent(event)


# --------------------------------------------------------------------------------------------- existing folder
def overwrite_job_folder(parent, root: str, name: str, offer_open: bool = True) -> bool:
    """Deal with a job folder that is already there: open it, overwrite it, or stop.

    This is item 4 of the change order, and it is deliberately slow.  A folder that already holds
    a project is somebody's work, so:

    1. if it holds a project file the first question offers to **open it** - which is usually what
       was wanted, and it is asked first because opening cannot destroy anything;
    2. then "are you sure all prior data will be deleted?", with the counts;
    3. then "**Last chance to cancel, continue?**"

    and only then is anything removed.  The removal itself goes to the desktop's **recycle bin**
    where the platform provides one, so the wording "will be deleted" is a promise about what the
    folder looks like afterwards, not a promise that the bytes are gone for ever.  Returns True
    when the folder is clear (or was never there) and the caller should carry on creating.
    """
    from PySide6.QtCore import QFile

    from .widgets import destructive_confirm, info_box

    info = JT.job_folder_contents(root, name)
    if not info["exists"] or info["empty"]:
        return True
    where = info["folder"]
    if info["projects"] and offer_open:
        m = QMessageBox(parent)
        m.setIcon(QMessageBox.Question)
        m.setWindowTitle("Project Folder")
        m.setText(f"{where.name} already holds a project ({', '.join(info['projects'][:3])}).")
        m.setInformativeText("Open it, or start fresh in this folder and delete what is here?")
        b_open = m.addButton("Open that project", QMessageBox.AcceptRole)
        b_new = m.addButton("Start fresh - delete what is here", QMessageBox.DestructiveRole)
        m.addButton("Cancel", QMessageBox.RejectRole)
        m.setDefaultButton(b_open)
        m.exec()
        clicked = m.clickedButton()
        if clicked is b_open:
            if parent is not None and hasattr(parent, "open_project_path"):
                parent.open_project_path(str(info["folder"] / info["projects"][0]))
            return False
        if clicked is not b_new:
            return False
    mb = int(info["bytes"] / 1024)
    size = f"{mb // 1024} MB" if mb >= 1024 else f"{mb} KB"
    ok = destructive_confirm(
        parent, "Overwrite Project Folder",
        [(f"{where}\n\nholds {info['items']} item(s) ({info['files']} file(s), {info['folders']} "
          f"folder(s), {size}).\n\nStart fresh here?", "Start fresh - delete what is here"),
         ("Are you sure?  All prior data in this folder will be deleted.\n\n"
          f"{where}", "Yes, delete the prior data")],
        final="Last chance to cancel, continue?")
    if not ok:
        return False
    # Recycle bin first (Qt 5.15+/6), plain delete as the fallback.
    trashed = True
    for entry in list(info["folder"].iterdir()):
        if not QFile.moveToTrash(str(entry)):
            trashed = False
    if not trashed:
        JT.clear_job_folder(root, name)
    left = JT.job_folder_contents(root, name)
    info_box(parent, "Overwrite Project Folder",
             f"{where.name} is clear"
             + (" (the old contents went to the recycle bin)." if trashed else ".")
             + (f"\n\n{left['items']} item(s) could not be removed - check whether a file is open."
                if left["items"] else ""))
    return left["items"] == 0


# --------------------------------------------------------------------------------------------- entry point
def run_job_setup(parent, parent_folder: str, name: str, template: JT.JobTemplate,
                  weeks: int | None, crs_label: str, overwrite_checked: bool = False,
                  crs_record: dict | None = None) -> JT.JobCreation | None:
    """Build the job folder with a cancelable progress dialog.

    Returns the :class:`~plumbline.core.jobtemplate.JobCreation` on success, None if
    the user cancelled or it failed (having explained why), and raises nothing.
    """
    # An existing, non-empty job folder is a question before it is a build (item 4).
    # Creating a job folder does not offer "open the project that is already here" - the caller
    # has already decided to create one.  Project Folder... does offer it, because opening is what
    # that dialog is for.
    if not overwrite_checked and not overwrite_job_folder(parent, parent_folder, name, offer_open=False):
        return None
    dlg = JobSetupProgressDialog(parent, parent_folder, name, template, weeks, crs_label, crs_record)
    ok = dlg.start()
    if dlg.error:
        QMessageBox.critical(parent, "Job Setup", f"The job folder could not be created:\n\n{dlg.error}")
        return None
    if not ok or dlg.creation is None:
        return None                                    # cancelled - already rolled back
    return dlg.creation


__all__ = ["JobSetupPanel", "JobSetupProgressDialog", "run_job_setup", "overwrite_job_folder"]
