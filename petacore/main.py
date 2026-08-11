"""Petacore application entry point."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from .config import config  # noqa: E402
from .i18n import translator as _  # noqa: E402
from .window import PetacoreWindow  # noqa: E402

APP_ID = "io.petacore.Petacore"
VERSION = "1.0.0"


class PetacoreApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        _.set_language(config.get("language"))
        self._add_action("new-project", lambda: self.win.show_new_project())
        self._add_action("preferences", lambda: self.win.show_preferences())
        self._add_action("about", self._show_about)
        self._add_action("quit", self.quit)
        self.set_accels_for_action("app.quit", ["<Ctrl>Q"])
        self.set_accels_for_action("app.new-project", ["<Ctrl>N"])
        self.set_accels_for_action("app.preferences", ["<Ctrl>comma"])

    def _add_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *a: callback())
        self.add_action(action)

    def do_activate(self):
        style = Adw.StyleManager.get_default()
        style.set_color_scheme({
            "system": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(config.get("theme"), Adw.ColorScheme.DEFAULT))

        self._load_css()
        from gi.repository import Gtk
        Gtk.Window.set_default_icon_name(APP_ID)

        win = self.get_active_window()
        if not win:
            win = PetacoreWindow(self)
        self.win = win
        win.present()

    def _load_css(self):
        from gi.repository import Gdk, Gtk
        css = b"""
        vte-terminal.petacore-terminal {
            padding: 12px;
            background-color: #300a24;
        }
        scrolledwindow.petacore-terminal-frame {
            border-radius: 0;
            background-color: #300a24;
        }
        scrolledwindow.petacore-terminal-frame undershoot,
        scrolledwindow.petacore-terminal-frame overshoot {
            background: none;
        }
        popover.petacore-preview-pop > contents,
        popover.petacore-preview-pop > arrow {
            background-color: #1e1e1e;
            padding: 0;
        }
        box.petacore-preview {
            background-color: #1e1e1e;
            border-radius: 12px;
        }
        box.petacore-preview label {
            color: #f2f2f2;
        }
        box.petacore-preview label.dim-label {
            color: #9a9a9a;
        }
        window, popover, tooltip, headerbar {
            font-family: "Ubuntu", sans-serif;
        }
        textview.petacore-editor,
        textview.petacore-editor text,
        label.monospace {
            font-family: "Ubuntu Mono", monospace;
        }
        /* Overview page: soft dark gradient like the Petacomm site. */
        .petacore-overview {
            background-image: radial-gradient(
                ellipse 140% 90% at 50% 100%,
                #3d3d3d 0%, #303030 30%, #232323 62%, #1a1a1a 100%);
        }
        .petacore-hero-title {
            font-size: 34pt;
            font-weight: 300;
            color: #ffffff;
        }
        .petacore-hero-strong {
            font-size: 34pt;
            font-weight: 700;
            color: #ffffff;
        }
        .petacore-hero-sub {
            font-size: 12pt;
            color: #b9b9b9;
        }
        .petacore-card {
            background-color: rgba(255, 255, 255, 0.045);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 14px;
            padding: 16px;
        }
        .petacore-card-value {
            font-size: 24pt;
            font-weight: 700;
            color: #ffffff;
        }
        .petacore-card-label {
            font-size: 9pt;
            color: #9a9a9a;
        }
        .petacore-chip {
            background-color: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.10);
            border-radius: 10px;
            padding: 10px 14px;
        }
        .petacore-chip-label {
            font-size: 11pt;
            color: #ffffff;
        }
        .petacore-section {
            font-size: 10pt;
            color: #9a9a9a;
        }

        /* Veils used for the soft light/dark cross-dissolve. */
        .petacore-veil-light { background-color: #fafafa; }
        .petacore-veil-dark  { background-color: #1e1e1e; }

        window.petacore-setup {
            background-color: #1a1a1a;
        }
        window.petacore-setup headerbar {
            background-color: #1a1a1a;
            box-shadow: none;
        }
        .petacore-setup-card {
            background-color: #2d2d2d;
            border-radius: 16px;
        }
        window.petacore-setup,
        window.petacore-setup * {
            font-family: "Ubuntu", sans-serif;
        }
        .petacore-slogan {
            font-family: "Ubuntu", sans-serif;
            color: #ffffff;
            letter-spacing: 2px;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _show_about(self):
        about = Adw.AboutWindow(
            transient_for=self.get_active_window(),
            application_name="Petacore",
            application_icon="io.petacore.Petacore",
            version=VERSION,
            developer_name="Petacore",
            license_type=0,
            comments=_("about_comment"),
            website="https://github.com",
        )
        about.present()


def main():
    app = PetacoreApp()
    # GTK rejects command-line options it doesn't recognise and exits, so the
    # launcher's own flags (--gnome / --kde …) must be removed first —
    # otherwise a restart from Settings would die immediately.
    own_flags = {"--gnome", "--gtk", "--kde", "--qt"}
    argv = [a for a in sys.argv if a not in own_flags]
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
