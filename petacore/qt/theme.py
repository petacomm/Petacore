"""KDE Plasma (Breeze) look for the Qt build of Petacore.

Colours follow the Breeze and Breeze Dark palettes so the app sits naturally
next to Dolphin, Kate and System Settings. When a real Plasma session is
running, the platform theme already supplies Breeze; these palettes make the
app look right everywhere else too (and give us an explicit light/dark switch).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

# -- Breeze (light) ---------------------------------------------------------
LIGHT = {
    "window": "#eff0f1",
    "base": "#fcfcfc",
    "alt_base": "#eff0f1",
    "text": "#232629",
    "dim": "#7f8c8d",
    "button": "#eff0f1",
    "highlight": "#3daee9",
    "highlight_text": "#ffffff",
    "border": "#bdc3c7",
    "accent": "#3daee9",
    "positive": "#27ae60",
    "negative": "#da4453",
    "neutral": "#f67400",
}

# -- Breeze Dark ------------------------------------------------------------
DARK = {
    "window": "#2a2e32",
    "base": "#1b1e20",
    "alt_base": "#31363b",
    "text": "#fcfcfc",
    "dim": "#a1a9b1",
    "button": "#31363b",
    "highlight": "#3daee9",
    "highlight_text": "#ffffff",
    "border": "#4d5359",
    "accent": "#3daee9",
    "positive": "#27ae60",
    "negative": "#da4453",
    "neutral": "#f67400",
}


def colors(variant):
    """variant may be the legacy bool (True=dark, False=light) or the
    string "light" / "dark"."""
    if isinstance(variant, bool):
        variant = "dark" if variant else "light"
    return {"light": LIGHT, "dark": DARK}.get(variant, LIGHT)


def build_palette(variant) -> QPalette:
    c = colors(variant)
    p = QPalette()
    p.setColor(QPalette.Window, QColor(c["window"]))
    p.setColor(QPalette.WindowText, QColor(c["text"]))
    p.setColor(QPalette.Base, QColor(c["base"]))
    p.setColor(QPalette.AlternateBase, QColor(c["alt_base"]))
    p.setColor(QPalette.Text, QColor(c["text"]))
    p.setColor(QPalette.Button, QColor(c["button"]))
    p.setColor(QPalette.ButtonText, QColor(c["text"]))
    p.setColor(QPalette.Highlight, QColor(c["highlight"]))
    p.setColor(QPalette.HighlightedText, QColor(c["highlight_text"]))
    p.setColor(QPalette.ToolTipBase, QColor(c["alt_base"]))
    p.setColor(QPalette.ToolTipText, QColor(c["text"]))
    p.setColor(QPalette.PlaceholderText, QColor(c["dim"]))
    p.setColor(QPalette.Link, QColor(c["accent"]))
    disabled = QColor(c["dim"])
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    return p


def stylesheet(variant) -> str:
    """Breeze-flavoured stylesheet: flat surfaces, 3px radii, thin borders,
    a sidebar that reads like Dolphin's places panel."""
    c = colors(variant)
    return f"""
    * {{
        font-family: "Noto Sans", "Ubuntu", sans-serif;
        font-size: 10pt;
    }}
    QMainWindow, QDialog, QWizard {{
        background: {c["window"]};
        color: {c["text"]};
    }}
    QLabel {{ color: {c["text"]}; }}
    QLabel[dim="true"] {{ color: {c["dim"]}; }}
    QLabel[heading="true"] {{ font-size: 13pt; font-weight: 600; }}
    QLabel[title="true"] {{ font-size: 18pt; font-weight: 700; }}

    /* Sidebar — Dolphin/System Settings places list */
    QListWidget#sidebar {{
        background: {c["alt_base"]};
        border: none;
        border-right: 1px solid {c["border"]};
        outline: none;
        padding: 6px 4px;
    }}
    QListWidget#sidebar::item {{
        padding: 8px 10px;
        border-radius: 3px;
        color: {c["text"]};
    }}
    QListWidget#sidebar::item:selected {{
        background: {c["highlight"]};
        color: {c["highlight_text"]};
    }}
    QListWidget#sidebar::item:hover:!selected {{
        background: rgba(61, 174, 233, 0.18);
    }}

    QListWidget, QTreeWidget, QPlainTextEdit, QTextEdit, QLineEdit {{
        background: {c["base"]};
        color: {c["text"]};
        border: 1px solid {c["border"]};
        border-radius: 3px;
        selection-background-color: {c["highlight"]};
        selection-color: {c["highlight_text"]};
    }}
    QListWidget::item {{ padding: 6px 8px; border-radius: 3px; }}
    QListWidget::item:selected {{
        background: {c["highlight"]};
        color: {c["highlight_text"]};
    }}
    QLineEdit {{ padding: 6px 8px; }}

    QPushButton {{
        background: {c["button"]};
        color: {c["text"]};
        border: 1px solid {c["border"]};
        border-radius: 3px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{ border-color: {c["highlight"]}; }}
    QPushButton:pressed {{ background: {c["alt_base"]}; }}
    QPushButton:disabled {{ color: {c["dim"]}; }}
    QPushButton[primary="true"] {{
        background: {c["highlight"]};
        color: {c["highlight_text"]};
        border: 1px solid {c["highlight"]};
        font-weight: 600;
    }}
    QPushButton[primary="true"]:hover {{ background: #4fb9ed; }}
    QPushButton[danger="true"] {{
        background: {c["negative"]};
        color: #ffffff;
        border: 1px solid {c["negative"]};
    }}

    QComboBox, QSpinBox {{
        background: {c["base"]};
        color: {c["text"]};
        border: 1px solid {c["border"]};
        border-radius: 3px;
        padding: 5px 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {c["base"]};
        color: {c["text"]};
        selection-background-color: {c["highlight"]};
    }}

    QGroupBox {{
        border: 1px solid {c["border"]};
        border-radius: 3px;
        margin-top: 14px;
        padding: 10px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {c["dim"]};
        font-weight: 600;
    }}

    QTabBar::tab {{
        background: {c["alt_base"]};
        color: {c["text"]};
        border: 1px solid {c["border"]};
        border-bottom: none;
        padding: 6px 12px;
        margin-right: 2px;
        border-top-left-radius: 3px;
        border-top-right-radius: 3px;
    }}
    QTabBar::tab:selected {{
        background: {c["base"]};
        border-bottom: 2px solid {c["highlight"]};
    }}
    QTabWidget::pane {{ border: 1px solid {c["border"]}; }}

    QToolBar {{
        background: {c["window"]};
        border: none;
        border-bottom: 1px solid {c["border"]};
        spacing: 6px;
        padding: 4px 6px;
    }}
    QStatusBar {{ color: {c["dim"]}; border-top: 1px solid {c["border"]}; }}
    QProgressBar {{
        border: 1px solid {c["border"]};
        border-radius: 3px;
        text-align: center;
        background: {c["base"]};
        color: {c["text"]};
    }}
    QProgressBar::chunk {{ background: {c["highlight"]}; border-radius: 2px; }}
    QCheckBox, QRadioButton {{ color: {c["text"]}; spacing: 8px; }}
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {c["border"]}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QSplitter::handle {{ background: {c["border"]}; }}
    """
