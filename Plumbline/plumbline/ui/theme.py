"""Dark / light themes (palette + stylesheet) and canvas colours."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QComboBox

THEMES = {
    "dark": dict(window="#1d2126", base="#14171b", alt="#191d22", text="#d9dee5", dim="#8a94a1", button="#2a3037",
                 border="#363d46", accent="#3b8eea", accent_text="#ffffff", canvas="#0f1317", grid="#1a2027",
                 select="#00e5ff", snap="#ffd400", tooltip="#2b3138", hover="#323a43", good="#4cc38a", warn="#f0b429",
                 bad="#ff6b6b"),
    "light": dict(window="#f2f3f5", base="#ffffff", alt="#f6f7f9", text="#1f252b", dim="#6a7480", button="#e6e9ed",
                  border="#c9ced6", accent="#1f6fd6", accent_text="#ffffff", canvas="#fbfbf9", grid="#ececea",
                  select="#0078d4", snap="#d98200", tooltip="#ffffe8", hover="#dde3ea", good="#1f8f5a", warn="#b7791f",
                  bad="#c53030"),
}

_current = "dark"


def current() -> str:
    return _current


def colors(name: str | None = None) -> dict:
    return THEMES[name or _current]


def qcolor(key: str) -> QColor:
    return QColor(THEMES[_current][key])


def ui_font() -> QFont:
    """DejaVu Sans where it is installed (Linux), otherwise the operating system's own UI font (Segoe UI on Windows)."""
    from PySide6.QtGui import QFontDatabase
    if "DejaVu Sans" in QFontDatabase.families():
        f = QFont("DejaVu Sans")
    else:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    f.setPointSizeF(9.5)
    return f


def mono_font(point_size: float | None = None) -> QFont:
    """A fixed-width font that exists on this machine: DejaVu Sans Mono (Linux), Consolas (Windows), Menlo (macOS) ..."""
    from PySide6.QtGui import QFontDatabase
    have = set(QFontDatabase.families())
    for name in ("DejaVu Sans Mono", "Consolas", "Menlo", "Courier New"):
        if name in have:
            f = QFont(name)
            break
    else:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if point_size:
        f.setPointSizeF(point_size)
    return f


def display_color(rgb, theme: str | None = None) -> tuple:
    """Make CAD colours readable on the current background (white <-> black swap like AutoCAD colour 7)."""
    r, g, b = (int(c) for c in rgb)
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    t = theme or _current
    if t == "light":
        if lum > 0.82:
            return (25, 25, 25)
        if lum > 0.6:                      # pale yellow / cyan are unreadable on white
            return (int(r * 0.62), int(g * 0.62), int(b * 0.62))
    elif lum < 0.10:
        return (225, 225, 225)
    return (r, g, b)


def _check_icon() -> str:
    """Write the tick used by checked check boxes (Qt style sheets need a file path) and return its path."""
    from ..core.settings import user_dir
    d = user_dir() / "ui"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "check.svg"
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M3.2 8.6l3.1 3.1 6.5-7" fill="none" '
           'stroke="#ffffff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    try:
        if not f.exists() or f.read_text("utf-8") != svg:
            f.write_text(svg, "utf-8")
    except OSError:
        pass
    return f.as_posix()


def stylesheet(c: dict) -> str:
    chk = _check_icon()
    return f"""
    QCheckBox::indicator, QAbstractItemView::indicator {{ width: 14px; height: 14px; border: 2px solid {c['dim']};
        border-radius: 4px; background: {c['base']}; }}
    QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
    QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']};
        image: url({chk}); }}
    QCheckBox::indicator:disabled {{ border-color: {c['border']}; }}
    QRadioButton::indicator {{ width: 14px; height: 14px; border: 2px solid {c['dim']}; border-radius: 9px; background: {c['base']}; }}
    QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
    QRadioButton::indicator:checked {{ border-color: {c['accent']}; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
        fx:0.5, fy:0.5, stop:0 {c['accent']}, stop:0.5 {c['accent']}, stop:0.6 {c['base']}, stop:1 {c['base']}); }}
    QRadioButton::indicator:disabled {{ border-color: {c['border']}; }}
    QWidget {{ color: {c['text']}; }}
    QMainWindow, QDialog {{ background: {c['window']}; }}
    QToolTip {{ background: {c['tooltip']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
    QMenuBar {{ background: {c['window']}; }}
    QMenuBar::item {{ padding: 5px 10px; background: transparent; border-radius: 4px; }}
    QMenuBar::item:selected {{ background: {c['hover']}; }}
    QMenu {{ background: {c['window']}; border: 1px solid {c['border']}; padding: 4px; }}
    QMenu::item {{ padding: 6px 28px 6px 24px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
    QMenu::item:disabled {{ color: {c['dim']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
    QToolBar {{ background: {c['window']}; border: none; spacing: 2px; padding: 3px; }}
    QToolBar::separator {{ background: {c['border']}; width: 1px; margin: 4px 6px; }}
    QToolButton {{ border: 1px solid transparent; border-radius: 5px; padding: 4px; }}
    QToolButton:hover {{ background: {c['hover']}; }}
    QToolButton:checked {{ background: {c['alt']}; border-color: {c['border']}; }}
    QToolButton:pressed {{ background: {c['border']}; }}
    QDockWidget {{ titlebar-close-icon: none; }}
    QDockWidget::title {{ background: {c['alt']}; padding: 6px 8px; border-bottom: 1px solid {c['border']}; font-weight: 600; }}
    QStatusBar {{ background: {c['window']}; border-top: 1px solid {c['border']}; }}
    QStatusBar::item {{ border: none; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background: {c['base']}; border: 1px solid {c['border']}; border-radius: 4px; padding: 3px 6px;
        selection-background-color: {c['accent']}; selection-color: {c['accent_text']}; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {c['accent']}; }}
    QComboBox QAbstractItemView {{ background: {c['base']}; selection-background-color: {c['accent']}; }}
    QPushButton {{ background: {c['button']}; border: 1px solid {c['border']}; border-radius: 5px; padding: 5px 14px; }}
    QPushButton:hover {{ background: {c['hover']}; }}
    QPushButton:default, QPushButton[accent="true"] {{ background: {c['button']}; border-color: {c['border']}; color: {c['text']}; font-weight: 600; }}
    QPushButton:disabled {{ color: {c['dim']}; }}
    QTableView, QTreeView, QListView, QTableWidget, QListWidget, QTreeWidget {{
        background: {c['base']}; alternate-background-color: {c['alt']}; border: 1px solid {c['border']};
        gridline-color: {c['border']}; selection-background-color: {c['accent']}; selection-color: {c['accent_text']}; }}
    QHeaderView::section {{ background: {c['button']}; border: none; border-right: 1px solid {c['border']};
        border-bottom: 1px solid {c['border']}; padding: 4px 6px; font-weight: 600; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; top: -1px; }}
    QTabBar::tab {{ background: {c['window']}; padding: 6px 14px; border: 1px solid transparent; }}
    QTabBar::tab:selected {{ background: {c['base']}; border: 1px solid {c['border']}; border-bottom-color: {c['base']}; }}
    QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 10px; padding-top: 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['dim']}; }}
    QScrollBar:vertical {{ background: {c['window']}; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 24px; margin: 2px; }}
    QScrollBar:horizontal {{ background: {c['window']}; height: 12px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 5px; min-width: 24px; margin: 2px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QSplitter::handle {{ background: {c['border']}; }}
    QProgressBar {{ border: 1px solid {c['border']}; border-radius: 4px; text-align: center; background: {c['base']}; }}
    QProgressBar::chunk {{ background: {c['accent']}; }}
    QLabel[hint="true"] {{ color: {c['dim']}; }}
    QLabel[banner="warn"] {{ background: {c['warn']}; color: #1b1b1b; padding: 6px 10px; border-radius: 5px; }}
    QLabel[banner="info"] {{ background: {c['hover']}; padding: 6px 10px; border-radius: 5px; }}
    QLabel[banner="bad"] {{ background: {c['bad']}; color: #ffffff; padding: 6px 10px; border-radius: 5px; }}
    """


def apply_theme(app: QApplication, name: str = "dark"):
    global _current
    _current = name if name in THEMES else "dark"
    c = THEMES[_current]
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(c["window"]))
    pal.setColor(QPalette.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.Base, QColor(c["base"]))
    pal.setColor(QPalette.AlternateBase, QColor(c["alt"]))
    pal.setColor(QPalette.Text, QColor(c["text"]))
    pal.setColor(QPalette.Button, QColor(c["button"]))
    pal.setColor(QPalette.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.Highlight, QColor(c["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor(c["accent_text"]))
    pal.setColor(QPalette.ToolTipBase, QColor(c["tooltip"]))
    pal.setColor(QPalette.ToolTipText, QColor(c["text"]))
    pal.setColor(QPalette.PlaceholderText, QColor(c["dim"]))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(c["dim"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(c["dim"]))
    app.setPalette(pal)
    app.setFont(ui_font())
    app.setStyleSheet(stylesheet(c))
    # Keep every dialog on the screen (ui/dialogfit.py).  Installed from here because every
    # entry point - window, tools, screenshots - calls apply_theme before it opens anything,
    # so no dialog has to remember to ask for it, including dialogs written later.
    from . import dialogfit
    dialogfit.install(app)

    # Prevent mouse wheel scrolling on closed QComboBox dropdowns application-wide
    global _combo_wheel_filter
    if _combo_wheel_filter is None:
        _combo_wheel_filter = _ComboWheelFilter(app)
        app.installEventFilter(_combo_wheel_filter)


class _ComboWheelFilter(QObject):
    """Prevent mouse wheel from scrolling QComboBoxes when their popup is closed.

    Scrolling through forms or dialogs with the mouse wheel should not accidentally
    change dropdown selections.  When the dropdown popup view is open, it handles
    its own scrolling normally.
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, QComboBox):
            view = obj.view()
            if view is None or not view.isVisible():
                event.ignore()
                return True
        return super().eventFilter(obj, event)


_combo_wheel_filter: _ComboWheelFilter | None = None
