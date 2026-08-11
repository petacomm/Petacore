"""Entry point for the KDE Plasma (Qt) build of Petacore."""

import os
import sys

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from ..config import config
from ..i18n import translator as _
from . import theme


def _system_prefers_dark() -> bool:
    """Follow the desktop's colour scheme when the theme is set to 'system'."""
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        # Qt.ColorScheme.Dark == 2
        if int(scheme) == 2:
            return True
        if int(scheme) == 1:
            return False
    except Exception:
        pass
    # Plasma writes the active scheme name into kdeglobals
    path = os.path.expanduser("~/.config/kdeglobals")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "dark" in f.read().lower().split("colorscheme=")[1][:40]
    except (OSError, IndexError):
        return False


class PetacoreQtApp(QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.setApplicationName("Petacore")
        self.setApplicationDisplayName("Petacore")
        self.setDesktopFileName("io.petacore.Petacore")
        _.set_language(config.get("language"))
        self.apply_theme()

    def apply_theme(self):
        mode = config.get("theme")
        dark = _system_prefers_dark() if mode == "system" else mode == "dark"
        self.setStyle("Fusion")
        self.setPalette(theme.build_palette(dark))
        self.setStyleSheet(theme.stylesheet(dark))


def main():
    own_flags = {"--gnome", "--gtk", "--kde", "--qt"}
    argv = [a for a in sys.argv if a not in own_flags]
    app = PetacoreQtApp(argv)
    from .window import PetacoreWindow
    window = PetacoreWindow(app)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
