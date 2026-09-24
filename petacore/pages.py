"""The five content pages of the Petacore main window."""

import os
import posixpath
import re
import shutil
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte
    HAVE_VTE = True
except (ValueError, ImportError):
    HAVE_VTE = False

# The embedded browser used by the Live Server page. Optional, like VTE:
# without it the page still runs the server and offers the address, it just
# cannot draw the site inside the window.
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    HAVE_WEBKIT = True
except (ValueError, ImportError):
    WebKit = None
    HAVE_WEBKIT = False

from . import debbuild, detect  # noqa: E402
from .config import config  # noqa: E402
from .i18n import translator as _  # noqa: E402
from .snapshots import SnapshotManager  # noqa: E402
from .util import human_size, run_async  # noqa: E402

FILE_MARKER = re.compile(r"^\s*(?:#|//|;|--|<!--)?\s*[Ff]ile\s*:\s*(\S+?)\s*(?:-->)?\s*$")


# GNOME Console-style terminal theme (GNOME color palette).
# Ubuntu default terminal theme (aubergine background + Ubuntu/Tango palette).
TERM_BG = "#300a24"
TERM_FG = "#ffffff"
TERM_PALETTE = [
    "#2e3436", "#cc0000", "#4e9a06", "#c4a000",
    "#3465a4", "#75507b", "#06989a", "#d3d7cf",
    "#555753", "#ef2929", "#8ae234", "#fce94f",
    "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec",
]


def make_terminal():
    """A VTE terminal styled like GNOME Console: dark palette, padding,
    modern cursor, mouse-friendly scrollback."""
    from gi.repository import Gdk

    def rgba(value):
        color = Gdk.RGBA()
        color.parse(value)
        return color

    term = Vte.Terminal(vexpand=True, hexpand=True)
    term.set_colors(rgba(TERM_FG), rgba(TERM_BG),
                    [rgba(c) for c in TERM_PALETTE])
    term.set_font(Pango.FontDescription("Ubuntu Mono 12"))
    term.set_cursor_shape(Vte.CursorShape.BLOCK)
    term.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
    term.set_bold_is_bright(True)
    term.set_audible_bell(False)
    term.set_scroll_on_keystroke(True)
    term.set_scrollback_lines(10000)
    term.add_css_class("petacore-terminal")
    return term


def terminal_frame(term):
    """Square, edge-to-edge terminal surface (Ubuntu terminal style)."""
    sw = _scrolled(term)
    frame = Gtk.Box()
    sw.set_hexpand(True)
    sw.add_css_class("petacore-terminal-frame")
    frame.append(sw)
    return frame


def _scrolled(child):
    sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    sw.set_child(child)
    return sw


# --------------------------------------------------------------------------- #
# Project — GNOME Files-style browser of the project tree
# --------------------------------------------------------------------------- #
try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
    HAVE_SRC = True
except (ValueError, ImportError):
    HAVE_SRC = False

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
HIDDEN = {".git", ".petacore"}
from . import versions as _versions  # noqa: E402

# Encrypted artefacts must never surface in Petacore: the app always shows the
# plain project. Anything that is an encrypted blob (a stray rclone crypt
# staging folder, or a file left behind by an interrupted encrypted transfer)
# is filtered out of every listing.
ENCRYPTED_DIRS = {".petacore-crypt", ".rclone-crypt", ".petacore-encrypted"}
ENCRYPTED_EXT = {".bin", ".enc", ".gpg", ".age", ".rclone_chunk"}


def is_encrypted_artifact(name: str, is_dir: bool) -> bool:
    """True for anything that is an encrypted blob rather than real project
    content — those are hidden from the user everywhere in the app.

    Written defensively on purpose: if this ever fails it must hide nothing,
    never everything, so a bug here can't leave the user with an empty
    file list."""
    try:
        if is_dir:
            return name in ENCRYPTED_DIRS
        lowered = name.lower()
        if os.path.splitext(lowered)[1] in ENCRYPTED_EXT:
            return True
        stem, ext = os.path.splitext(name)
        if not ext and len(stem) >= 32 and re.fullmatch(r"[a-z2-7]+", stem):
            return True
    except Exception:
        return False
    return False

CODE_EXT = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".css", ".scss", ".sass", ".c", ".h", ".cpp",
    ".hpp", ".cc", ".hh", ".cxx", ".cs", ".java", ".rs", ".go",
    ".php", ".rb", ".swift", ".kt", ".kts", ".lua", ".sh", ".bash",
    ".zsh", ".sql", ".json", ".yml", ".yaml", ".toml", ".xml",
}
DOC_EXT = {".md", ".markdown", ".txt", ".pdf", ".odt", ".doc", ".docx",
           ".rtf", ".csv"}

FILTERS = [
    ("all", "filter_all"),
    ("code", "filter_code"),
    ("images", "filter_images"),
    ("media", "filter_media"),
    ("docs", "filter_docs"),
    ("folders", "filter_folders"),
]


def _matches_filter(name, is_dir, mode):
    if mode == "all":
        return True
    if mode == "folders":
        return is_dir
    if is_dir:
        return False
    ext = os.path.splitext(name)[1].lower()
    if mode == "code":
        return ext in CODE_EXT
    if mode == "images":
        return ext in IMAGE_EXT
    if mode == "media":
        return ext in AUDIO_EXT or ext in VIDEO_EXT
    if mode == "docs":
        return ext in DOC_EXT
    return True
MAX_EDIT_SIZE = 2 * 1024 * 1024  # larger files open externally


def _file_icon(name, is_dir):
    if is_dir:
        return "folder-pictures-symbolic" if name == "media" else "folder-symbolic"
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image-x-generic-symbolic"
    if ext in AUDIO_EXT:
        return "audio-x-generic-symbolic"
    if ext in VIDEO_EXT:
        return "video-x-generic-symbolic"
    return "text-x-generic-symbolic"


# --- language logos ("img src" style): drop SVG/PNG files into
#     ~/.local/share/petacore/filetype-icons/  (instant) or the bundled
#     petacore/filetype-icons/ folder. See the README.txt in that folder.
from .config import DATA_DIR as _DATA_DIR  # noqa: E402

LANG_ICON = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".sass": "css",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".hh": "cpp", ".cxx": "cpp",
    ".cs": "csharp",
    ".java": "java",
    ".rs": "rust",
    ".go": "go",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin",
    ".lua": "lua",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql",
    ".json": "json",
    ".md": "markdown", ".markdown": "markdown",
}

_ICON_DIRS = [
    os.path.join(_DATA_DIR, "filetype-icons"),
    os.path.join(os.path.dirname(__file__), "filetype-icons"),
]
_texture_cache = {}


def _lang_icon_image(filename, size=22):
    """A Gtk.Image for the file's language logo, or None to fall back to
    the generic themed icon."""
    ext = os.path.splitext(filename)[1].lower()
    logo = LANG_ICON.get(ext)
    if not logo:
        return None
    if logo in _texture_cache:
        tex = _texture_cache[logo]
        return Gtk.Image.new_from_paintable(tex) if tex else None
    path = None
    for d in _ICON_DIRS:
        for suffix in (".svg", ".png"):
            candidate = os.path.join(d, logo + suffix)
            if os.path.isfile(candidate):
                path = candidate
                break
        if path:
            break
    if not path:
        _texture_cache[logo] = None
        return None
    try:
        from gi.repository import Gdk, GdkPixbuf
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            path, size * 2, size * 2, True)
        tex = Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception:
        _texture_cache[logo] = None
        return None
    _texture_cache[logo] = tex
    img = Gtk.Image.new_from_paintable(tex)
    img.set_pixel_size(size)
    return img


class ProjectPage(Gtk.Box):
    """File-manager view of the active project. Folders navigate, text files
    open in the built-in editor, media opens with the system default app.
    A `media` folder is always available for social-media assets."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=12, margin_bottom=12,
                         margin_start=14, margin_end=14)
        self.window = window
        project = config.active_project()
        self.root = os.path.realpath(project["path"]) if project else None
        self.current = self.root
        self._undo_stack = []  # [{"created": [...], "backups": [(orig, stash)]}]
        self._hist = [self.current] if self.current else []
        self._hist_i = 0

        if self.root:  # the Media Files folder always exists
            try:
                os.makedirs(os.path.join(self.root, "media"), exist_ok=True)
            except OSError:
                pass

        # -- toolbar ----------------------------------------------------------
        bar = Gtk.Box(spacing=6)
        self.up_btn = Gtk.Button(icon_name="go-up-symbolic",
                                 tooltip_text=_("go_up"),
                                 css_classes=["flat"])
        self.up_btn.connect("clicked", self._go_up)
        bar.append(self.up_btn)

        self.path_label = Gtk.Label(xalign=0, hexpand=True,
                                    css_classes=["heading"],
                                    ellipsize=Pango.EllipsizeMode.START)
        bar.append(self.path_label)

        media_btn = Gtk.Button(css_classes=["flat"])
        media_btn.set_child(Adw.ButtonContent(
            icon_name="folder-pictures-symbolic", label=_("media_files")))
        media_btn.connect("clicked", self._go_media)
        bar.append(media_btn)

        self.undo_btn = Gtk.Button(icon_name="edit-undo-symbolic",
                                   tooltip_text=_("undo") + " (Ctrl+Z)",
                                   css_classes=["flat"], sensitive=False)
        self.undo_btn.connect("clicked", lambda *a: self.undo_last())
        bar.append(self.undo_btn)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text=_("refresh"), css_classes=["flat"])
        refresh.connect("clicked", lambda *a: self.refresh())
        bar.append(refresh)
        self.append(bar)

        # compact codebase status line (branch · sync · running processes)
        self.status_line = Gtk.Label(xalign=0,
                                     css_classes=["dim-label", "caption"])
        self.append(self.status_line)

        # -- search + filter row ------------------------------------------------
        search_row = Gtk.Box(spacing=8)
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("search"),
                                            hexpand=True)
        self.search_entry.connect("search-changed",
                                  lambda *a: self.refresh())
        self.search_entry.connect(
            "stop-search", lambda *a: self._clear_search())
        search_row.append(self.search_entry)

        self.deep_btn = Gtk.ToggleButton(icon_name="folder-saved-search-symbolic",
                                         tooltip_text=_("search_all"))
        self.deep_btn.connect("toggled", lambda *a: self.refresh())
        search_row.append(self.deep_btn)

        self.filter_drop = Gtk.DropDown.new_from_strings(
            [_(k) for _c, k in FILTERS])
        self.filter_drop.set_tooltip_text(_("filter"))
        self.filter_drop.connect("notify::selected",
                                 lambda *a: self.refresh())
        search_row.append(self.filter_drop)
        self.append(search_row)

        self.listbox = Gtk.ListBox(css_classes=["boxed-list"],
                                   selection_mode=Gtk.SelectionMode.MULTIPLE,
                                   valign=Gtk.Align.START)
        self.listbox.set_activate_on_single_click(False)

        # wrapper so clicks on the empty (darker) area below the list are ours
        self.list_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 vexpand=True)
        self.list_wrap.append(self.listbox)

        # rubber-band rectangle drawn above the list (events pass through)
        self.rubber = Gtk.DrawingArea(can_target=False,
                                      hexpand=True, vexpand=True)
        self.rubber.set_draw_func(self._draw_rubber)
        self._rubber_rect = None
        overlay = Gtk.Overlay(child=self.list_wrap)
        overlay.add_overlay(self.rubber)
        self.append(_scrolled(overlay))

        # Google Drive upload progress
        self.drive_progress = Gtk.ProgressBar(show_text=True, visible=False,
                                              margin_top=2)
        self.append(self.drive_progress)

        # Ctrl + hover preview popover.
        # Reading modifier state off the motion event is unreliable (it's
        # frequently stale on Wayland), so Ctrl press/release is tracked
        # explicitly instead and combined with the last known pointer pos.
        self.preview_pop = Gtk.Popover(autohide=False, can_focus=False)
        self.preview_pop.set_parent(self.list_wrap)
        self.preview_pop.add_css_class("petacore-preview-pop")
        self._preview_row = None
        self._ctrl_held = False
        self._last_xy = (0, 0)

        ctrl_key = Gtk.EventControllerKey()
        ctrl_key.connect("key-pressed", self._on_preview_key_pressed)
        ctrl_key.connect("key-released", self._on_preview_key_released)
        self.add_controller(ctrl_key)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *a: self._hide_preview())
        self.list_wrap.add_controller(motion)

        # Ctrl + mouse wheel scrolls INSIDE the text preview
        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_preview_scroll)
        self.list_wrap.add_controller(scroll)
        self._preview_lines = None
        self._preview_offset = 0

        # rubber-band drag (starts on empty area)
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.list_wrap.add_controller(drag)

        # keyboard: Ctrl+A select all, Delete removes selection (undoable)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_list_key)
        self.listbox.add_controller(keys)

        # left click on empty area clears the selection
        lclick = Gtk.GestureClick(button=1)
        lclick.connect("pressed", self._on_left_click)
        self.list_wrap.add_controller(lclick)

        # right click works on rows AND on the empty area
        rclick = Gtk.GestureClick(button=3)
        rclick.connect("pressed", self._on_right_click)
        self.list_wrap.add_controller(rclick)

        # mouse back (8) / forward (9) buttons navigate folder history
        nav_click = Gtk.GestureClick(button=0)
        nav_click.connect("pressed", self._on_nav_button)
        self.add_controller(nav_click)

        self.menu_pop = Gtk.Popover(has_arrow=True)
        self.menu_pop.set_parent(self.list_wrap)

        # Drag & drop from the file manager: copy dropped files/folders into
        # the folder currently shown (great for the Media Files folder).
        from gi.repository import Gdk
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.refresh()

    # -- selection / keyboard / context menu ----------------------------------------
    def _selected_paths(self):
        return [(r._path, r._is_dir) for r in self.listbox.get_selected_rows()
                if getattr(r, "_path", None)]

    def _on_list_key(self, _ctrl, keyval, _code, state):
        from gi.repository import Gdk
        name = (Gdk.keyval_name(keyval) or "").lower()
        if (state & Gdk.ModifierType.CONTROL_MASK) and name == "a":
            self.listbox.select_all()
            return True
        if (state & Gdk.ModifierType.CONTROL_MASK) and name == "c":
            self._copy_selection()
            return True
        if (state & Gdk.ModifierType.CONTROL_MASK) and name == "v":
            self._paste_clipboard()
            return True
        if name in ("delete", "kp_delete"):
            paths = self._selected_paths()
            if paths:
                self._delete_paths([p for p, _d in paths])
            return True
        if name == "f2":
            paths = self._selected_paths()
            if paths:
                self._rename_dialog(paths[0][0])
            return True
        if (state & Gdk.ModifierType.ALT_MASK) and name == "left":
            self.go_back()
            return True
        if (state & Gdk.ModifierType.ALT_MASK) and name == "right":
            self.go_forward()
            return True
        return False

    def _row_at(self, x, y):
        """The ListBoxRow under the pointer, or None (empty/dark area)."""
        picked = self.list_wrap.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and not isinstance(picked, Gtk.ListBoxRow):
            picked = picked.get_parent()
        return picked

    def _on_left_click(self, _gesture, _n, x, y):
        if self._row_at(x, y) is None:
            self.listbox.unselect_all()

    def _on_right_click(self, gesture, _n, x, y):
        from gi.repository import Gdk
        row = self._row_at(x, y)
        if row is not None and getattr(row, "_path", None):
            if row not in self.listbox.get_selected_rows():
                self.listbox.unselect_all()
                self.listbox.select_row(row)
        else:
            self.listbox.unselect_all()
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.menu_pop.set_pointing_to(rect)
        self.menu_pop.set_child(self._build_menu())
        self.menu_pop.popup()

    def _build_menu(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        paths = self._selected_paths()

        def item(label, icon, cb, destructive=False):
            btn = Gtk.Button(css_classes=["flat"] +
                             (["destructive-action"] if destructive else []))
            content = Adw.ButtonContent(icon_name=icon, label=label,
                                        halign=Gtk.Align.START)
            btn.set_child(content)
            btn.connect("clicked",
                        lambda *a: (self.menu_pop.popdown(), cb()))
            box.append(btn)

        if paths:
            first, first_dir = paths[0]
            item(_("open_item"), "document-open-symbolic",
                 lambda: self._on_activate(None, first, first_dir))
            if len(paths) == 1:
                item(_("rename"), "document-edit-symbolic",
                     lambda: self._rename_dialog(first))
            item(_("copy_path"), "edit-copy-symbolic",
                 lambda: self._copy_path(first))
            item(_("delete"), "user-trash-symbolic",
                 lambda: self._delete_paths([p for p, _d in paths]),
                 destructive=True)
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item(_("new_folder"), "folder-new-symbolic", self._new_folder)
        return box

    def _copy_path(self, path):
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(path)
            self.window.toast(_("path_copied"))

    def _delete_paths(self, paths):
        """Delete = move into the undo stash, fully reversible with Ctrl+Z."""
        op = {"created": [], "backups": []}
        n = 0
        for path in paths:
            try:
                self._stash(path, op)
                n += 1
            except OSError as e:
                self.window.toast(str(e))
        if n:
            self._undo_stack.append(op)
            del self._undo_stack[:-10]
            self.undo_btn.set_sensitive(True)
            toast = Adw.Toast(title=_("files_deleted", n=n), timeout=6)
            toast.set_button_label(_("undo"))
            toast.connect("button-clicked", lambda *a: self.undo_last())
            self.window.toaster.add_toast(toast)
            self.refresh()

    def _rename_dialog(self, path):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("rename"))
        entry = Gtk.Entry(text=os.path.basename(path), activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("rename", _("rename"))
        dialog.set_response_appearance("rename",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")

        def responded(_d, response):
            if response != "rename":
                return
            new_name = entry.get_text().strip()
            if not new_name or "/" in new_name or new_name == \
                    os.path.basename(path):
                return
            new_path = os.path.join(os.path.dirname(path), new_name)
            if os.path.exists(new_path):
                self.window.toast(_("conflict_title", n=1))
                return
            try:
                os.rename(path, new_path)
            except OSError as e:
                self.window.toast(str(e))
                return
            self._undo_stack.append({"created": [], "backups": [],
                                     "renames": [(path, new_path)]})
            del self._undo_stack[:-10]
            self.undo_btn.set_sensitive(True)
            self.refresh()

        dialog.connect("response", responded)
        dialog.present()

    def _new_folder(self):
        """Ubuntu-desktop style: the folder appears immediately with an
        inline naming popover (name preselected, OK/Enter confirms)."""
        default = _("new_folder")
        path = self._unique_dest(self.current, default)
        try:
            os.makedirs(path)
        except OSError as e:
            self.window.toast(str(e))
            return
        op = {"created": [path], "backups": []}
        self._undo_stack.append(op)
        del self._undo_stack[:-10]
        self.undo_btn.set_sensitive(True)
        self.refresh()
        GLib.idle_add(self._popup_folder_name, path, op)

    def _find_row(self, path):
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            i += 1
            if getattr(row, "_path", None) == path:
                return row
        return None

    def _popup_folder_name(self, path, op):
        from gi.repository import Gdk
        row = self._find_row(path)

        pop = Gtk.Popover()
        pop.set_parent(self.list_wrap)
        if row is not None:
            ok, b = row.compute_bounds(self.list_wrap)
            rect = Gdk.Rectangle()
            if ok:
                rect.x = int(b.origin.x + 40)
                rect.y = int(b.origin.y)
                rect.width, rect.height = 1, int(b.size.height)
                pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=8, margin_bottom=8,
                      margin_start=10, margin_end=10)
        box.append(Gtk.Label(label=_("folder_name"), xalign=0,
                             css_classes=["dim-label", "caption"]))
        hbox = Gtk.Box(spacing=6)
        entry = Gtk.Entry(text=os.path.basename(path), width_chars=18)
        hbox.append(entry)
        ok_btn = Gtk.Button(label="OK", css_classes=["suggested-action"])
        hbox.append(ok_btn)
        box.append(hbox)
        pop.set_child(box)

        def confirm(*_a):
            name = entry.get_text().strip()
            pop.popdown()
            if not name or "/" in name or name == os.path.basename(path):
                return
            new_path = os.path.join(os.path.dirname(path), name)
            if os.path.exists(new_path):
                self.window.toast(_("conflict_title", n=1))
                return
            try:
                os.rename(path, new_path)
            except OSError as e:
                self.window.toast(str(e))
                return
            op["created"] = [new_path]   # keep Ctrl+Z pointing at the folder
            self.refresh()

        entry.connect("activate", confirm)
        ok_btn.connect("clicked", confirm)
        pop.connect("closed", lambda *a: GLib.idle_add(pop.unparent))
        pop.popup()
        entry.grab_focus()
        entry.select_region(0, -1)
        return False

    # -- Ctrl + hover preview -------------------------------------------------------
    def _on_preview_key_pressed(self, _ctrl, keyval, _code, _state):
        from gi.repository import Gdk
        if keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R):
            self._ctrl_held = True
            self._update_preview_at(*self._last_xy)
        return False

    def _on_preview_key_released(self, _ctrl, keyval, _code, _state):
        from gi.repository import Gdk
        if keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R):
            self._ctrl_held = False
            self._hide_preview()
        return False

    def _on_motion(self, _ctrl, x, y):
        self._last_xy = (x, y)
        if self._ctrl_held:
            self._update_preview_at(x, y)

    def _update_preview_at(self, x, y):
        if not self._ctrl_held:
            return
        row = self._row_at(x, y)
        if row is self._preview_row:
            return
        self._preview_row = row
        if row is None or not getattr(row, "_path", None):
            self._hide_preview(keep_row=True)
            return
        content = self._build_preview(row._path, row._is_dir)
        if content is None:
            self._hide_preview(keep_row=True)
            return
        from gi.repository import Gdk as _Gdk
        rect = _Gdk.Rectangle()
        ok, bounds = row.compute_bounds(self.list_wrap)
        if ok:
            rect.x = int(bounds.origin.x + bounds.size.width / 2)
            rect.y = int(bounds.origin.y)
            rect.width, rect.height = 1, int(bounds.size.height)
        else:
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.preview_pop.set_pointing_to(rect)
        self.preview_pop.set_child(content)
        self.preview_pop.popup()

    def _hide_preview(self, keep_row=False):
        if not keep_row:
            self._preview_row = None
        self._preview_lines = None
        self.preview_pop.popdown()

    # -- GNOME-viewer-style metadata sidebar ------------------------------------
    @staticmethod
    def _file_meta(path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        folder = os.path.basename(os.path.dirname(path)) or os.sep
        return {
            "folder": folder,
            "size": st.st_size,
            "created": time.strftime("%m/%d/%Y %I:%M %p",
                                     time.localtime(st.st_ctime)),
            "modified": time.strftime("%m/%d/%Y %I:%M %p",
                                      time.localtime(st.st_mtime)),
        }

    @staticmethod
    def _meta_row(label, value):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.append(Gtk.Label(label=label, xalign=0,
                             css_classes=["caption", "dim-label"]))
        box.append(Gtk.Label(label=value, xalign=0, wrap=True,
                             css_classes=["caption-heading"]))
        return box

    def _sidebar(self, path, extra_rows):
        """Right-hand metadata column: Folder, then whatever the caller
        passes (Image Size / Format, or Lines / Type), then size + dates."""
        meta = self._file_meta(path)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=14, margin_bottom=14,
                      margin_start=14, margin_end=14, width_request=170)
        if meta:
            col.append(self._meta_row("Folder", meta["folder"]))
        for label, value in extra_rows:
            col.append(self._meta_row(label, value))
        if meta:
            col.append(self._meta_row("File Size", human_size(meta["size"])))
            col.append(self._meta_row("Created", meta["created"]))
            col.append(self._meta_row("Modified", meta["modified"]))
        return col

    def _build_preview(self, path, is_dir):
        self._preview_lines = None
        name = os.path.basename(path)

        if is_dir:
            try:
                n = len([e for e in os.listdir(path)
                         if not e.startswith(".")])
            except OSError:
                return None
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                           valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                           width_request=260, margin_top=20, margin_bottom=20)
            body.append(Gtk.Image(icon_name="folder-symbolic",
                                  pixel_size=64, css_classes=["dim-label"]))
            body.append(Gtk.Label(label=name, css_classes=["title-4"],
                                  wrap=True, justify=Gtk.Justification.CENTER))
            sidebar = self._sidebar(path, [("Items", str(n))])
            return self._preview_frame(name, body, sidebar)

        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXT:
            try:
                from gi.repository import Gdk, GdkPixbuf
                fmt_info, iw, ih = GdkPixbuf.Pixbuf.get_file_info(path)
                fmt_name = (fmt_info.get_name().upper() if fmt_info
                           else ext.lstrip(".").upper())
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, 360, 280, True)
                img = Gtk.Image.new_from_paintable(
                    Gdk.Texture.new_for_pixbuf(pb))
                img.set_size_request(pb.get_width(), pb.get_height())
            except Exception:
                return None
            body = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                           margin_top=14, margin_bottom=14,
                           margin_start=14, margin_end=14)
            body.append(img)
            sidebar = self._sidebar(path, [
                ("Image Size", f"{iw} \u00d7 {ih}"),
                ("Image Format", fmt_name),
            ])
            return self._preview_frame(name, body, sidebar)

        # text / code
        try:
            if os.path.getsize(path) > MAX_EDIT_SIZE:
                return None
            with open(path, "r", encoding="utf-8") as f:
                lines = []
                for _i in range(4000):
                    line = f.readline()
                    if not line:
                        break
                    lines.append(line.rstrip()[:96])
        except (OSError, UnicodeDecodeError):
            return None
        self._preview_lines = lines or [" "]
        self._preview_offset = 0
        self._preview_name = name

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                           width_request=380, margin_top=12,
                           margin_bottom=12, margin_start=12, margin_end=12)
        self._preview_header = Gtk.Label(xalign=0, css_classes=["dim-label"])
        text_box.append(self._preview_header)
        self._preview_label = Gtk.Label(xalign=0,
                                        css_classes=["monospace", "caption"])
        self._preview_label.set_max_width_chars(60)
        text_box.append(self._preview_label)
        self._render_preview_text()

        lang = LANG_ICON.get(ext, ext.lstrip(".") or "Text").upper()
        sidebar = self._sidebar(path, [
            ("Lines", str(len(lines))),
            ("Type", lang),
        ])
        return self._preview_frame(name, text_box, sidebar)

    @staticmethod
    def _preview_frame(name, body, sidebar):
        """GNOME image-viewer layout: dark surface, content on the left,
        a thin separator, metadata column on the right."""
        outer = Gtk.Box(css_classes=["petacore-preview"])
        body.set_hexpand(True)
        outer.append(body)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        outer.append(sidebar)
        return outer

    PREVIEW_ROWS = 24

    def _render_preview_text(self):
        lines = self._preview_lines or []
        total = len(lines)
        start = self._preview_offset
        end = min(start + self.PREVIEW_ROWS, total)
        self._preview_label.set_text("\n".join(lines[start:end]) or " ")
        hint = "  \u00b7  Ctrl+scroll" if total > self.PREVIEW_ROWS else ""
        self._preview_header.set_text(
            f"{self._preview_name}   {start + 1}\u2013{end} / {total}{hint}")

    def _on_preview_scroll(self, _ctrl, _dx, dy):
        if not self._ctrl_held:
            return False
        if not self.preview_pop.get_visible() or not self._preview_lines:
            return False
        total = len(self._preview_lines)
        max_off = max(0, total - self.PREVIEW_ROWS)
        step = 3 if dy > 0 else -3
        new_off = max(0, min(max_off, self._preview_offset + step))
        if new_off != self._preview_offset:
            self._preview_offset = new_off
            self._render_preview_text()
        return True  # consume so the file list underneath stays still

    # -- rubber-band selection --------------------------------------------------------
    def _draw_rubber(self, _area, cr, _w, _h):
        if not self._rubber_rect:
            return
        # python3-cairo is an optional package on some systems; without it
        # this callback would raise on every frame. Selection still works,
        # it simply isn't outlined.
        try:
            x, y, w, h = self._rubber_rect
            cr.set_source_rgba(0.35, 0.55, 0.95, 0.18)
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_source_rgba(0.35, 0.55, 0.95, 0.65)
            cr.set_line_width(1)
            cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
            cr.stroke()
        except Exception:
            return

    def _on_drag_begin(self, gesture, x, y):
        self._rubber_start = (x, y)
        self._rubber_on = self._row_at(x, y) is None

    def _on_drag_update(self, gesture, dx, dy):
        if not getattr(self, "_rubber_on", False):
            return
        if abs(dx) < 4 and abs(dy) < 4:
            return
        sx, sy = self._rubber_start
        x, y = min(sx, sx + dx), min(sy, sy + dy)
        w, h = abs(dx), abs(dy)
        self._rubber_rect = (x, y, w, h)
        self.rubber.queue_draw()

        self.listbox.unselect_all()
        row = self.listbox.get_row_at_index(0)
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            i += 1
            if not getattr(row, "_path", None):
                continue
            ok, b = row.compute_bounds(self.list_wrap)
            if not ok:
                continue
            rx, ry = b.origin.x, b.origin.y
            rw, rh = b.size.width, b.size.height
            if rx < x + w and rx + rw > x and ry < y + h and ry + rh > y:
                self.listbox.select_row(row)

    def _on_drag_end(self, *_a):
        self._rubber_on = False
        self._rubber_rect = None
        self.rubber.queue_draw()

    # -- system clipboard copy / paste --------------------------------------------------
    def _copy_selection(self):
        from gi.repository import Gdk
        paths = [p for p, _d in self._selected_paths()]
        if not paths:
            return
        uris = "\r\n".join(
            Gio.File.new_for_path(p).get_uri() for p in paths)
        gnome = "copy\n" + "\n".join(
            Gio.File.new_for_path(p).get_uri() for p in paths)
        providers = [
            Gdk.ContentProvider.new_for_bytes(
                "x-special/gnome-copied-files",
                GLib.Bytes.new(gnome.encode("utf-8"))),
            Gdk.ContentProvider.new_for_bytes(
                "text/uri-list", GLib.Bytes.new(uris.encode("utf-8"))),
            Gdk.ContentProvider.new_for_bytes(
                "text/plain;charset=utf-8",
                GLib.Bytes.new("\n".join(paths).encode("utf-8"))),
        ]
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set_content(
                Gdk.ContentProvider.new_union(providers))
            self.window.toast(_("files_copied", n=len(paths)))

    def _paste_clipboard(self):
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if not display:
            return
        clipboard = display.get_clipboard()

        def got_value(clip, result):
            try:
                value = clip.read_value_finish(result)
                files = value.get_files() if value else []
                srcs = [f.get_path() for f in files
                        if f.get_path() and os.path.exists(f.get_path())]
                if srcs:
                    self._ingest_sources(srcs)
                    return
            except GLib.Error:
                pass
            clipboard.read_text_async(None, got_text)

        def got_text(clip, result):
            try:
                text = clip.read_text_finish(result) or ""
            except GLib.Error:
                return
            srcs = []
            for line in text.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line or line in ("copy", "cut"):
                    continue
                if line.startswith("file://"):
                    line = Gio.File.new_for_uri(line).get_path() or ""
                if line and os.path.exists(line):
                    srcs.append(line)
            if srcs:
                self._ingest_sources(srcs)

        try:
            clipboard.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT,
                                       None, got_value)
        except Exception:
            clipboard.read_text_async(None, got_text)

    # -- Google Drive --------------------------------------------------------------
    def _on_drive_sync(self, _btn):
        from . import gdrive
        project = config.active_project()
        if not project:
            return
        if not gdrive.available():
            self.window.toast(_("rclone_missing"))
            return
        if not gdrive.connected():
            dialog = Adw.MessageDialog(transient_for=self.window,
                                       heading=_("connect_gdrive"),
                                       body=_("gdrive_hint") + "\n\n"
                                            + _("gdrive_wait"))
            dialog.add_response("cancel", _("cancel"))
            dialog.add_response("connect", _("connect_gdrive"))
            dialog.set_response_appearance(
                "connect", Adw.ResponseAppearance.SUGGESTED)

            def responded(_d, response):
                if response != "connect":
                    return
                self.window.toast(_("gdrive_wait"))
                run_async(gdrive.connect,
                          lambda r, e: self._drive_connected(e, project))

            dialog.connect("response", responded)
            dialog.present()
            return
        self._start_drive_sync(project)

    def _drive_connected(self, error, project):
        from . import gdrive
        if error or not gdrive.connected():
            self.window.toast(f"{_('drive_failed')}: {error or ''}")
            return
        self.window.toast(_("gdrive_connected"))
        self.window.import_drive_projects(auto=True)
        self._start_drive_sync(project)

    def _check_shrink(self, project, secret):
        from . import gdrive

        def measure():
            return (gdrive.local_size(project["path"]),
                    gdrive.remote_size(project["name"]))

        def measured(sizes, error):
            local, remote = sizes if sizes else (0, -1)
            # -1 means the folder is not there yet, or the size could not be
            # read; either way there is nothing to warn about.
            if error or remote < 0 or local >= remote:
                self._start_drive_sync(project, secret, confirmed=True)
                return
            dialog = Adw.MessageDialog(
                transient_for=self.window,
                heading=_("shrink_title"),
                body=_("shrink_body", local=human_size(local),
                       remote=human_size(remote), f=gdrive.ATTIC_FOLDER))
            dialog.add_response("cancel", _("cancel"))
            dialog.add_response("upload", _("shrink_upload"))
            dialog.set_response_appearance(
                "upload", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.connect(
                "response",
                lambda _d, r: self._start_drive_sync(
                    project, secret, confirmed=True) if r == "upload" else None)
            dialog.present()

        self.window.begin_operation(_("op_drive"))

        def finished(sizes, error):
            self.window.end_operation()
            measured(sizes, error)

        run_async(measure, finished)

    def _set_drive_progress(self, pct):
        self.drive_progress.set_fraction(pct / 100.0)
        self.drive_progress.set_text(f"{pct}%")
        return False

    def _set_drive_busy(self, sensitive):
        """The Drive action lives in the header bar now, so that is the button
        that greys out while an upload is running."""
        button = getattr(self.window, "drive_sync_btn", None)
        if button is not None:
            button.set_sensitive(sensitive)

    def _start_drive_sync(self, project, secret=None, confirmed=False):
        from . import crypto, gdrive
        if crypto.is_encrypted(project) and not secret:
            self.window.ask_project_secret(
                project, lambda s: self._start_drive_sync(project, s))
            return
        if not confirmed and config.get("drive_shrink_warning"):
            # Compare before sending anything: an upload smaller than what
            # is already there usually means another computer has work this
            # one has not fetched.
            self._check_shrink(project, secret)
            return
        self._set_drive_busy(False)
        self.drive_progress.set_fraction(0)
        self.drive_progress.set_text("0%")
        self.drive_progress.set_visible(True)
        from . import gdrive as _gd
        self.window.begin_operation(_("op_drive"))
        self.window.toast(_("drive_mirror_note", f=_gd.ATTIC_FOLDER))

        def progress(pct):
            GLib.idle_add(self._set_drive_progress, pct)

        def done(target, error):
            self.window.end_operation()
            self._set_drive_busy(True)
            self.drive_progress.set_visible(False)
            if error:
                msg = _("rclone_missing") if str(error) == "rclone-missing" \
                    else f"{_('drive_failed')}: {error}"
                self.window.toast(msg)
            else:
                self.window.toast(_("drive_done", p=target))

        def work():
            if crypto.is_encrypted(project):
                # Encrypted upload: the copy on Drive is unreadable, while
                # the folder on this machine stays plain source code.
                return crypto.push(project["path"], project["name"], secret,
                                   progress=progress)
            return gdrive.sync_project(project["path"], project["name"],
                                       progress=progress)

        run_async(work, done)

    # -- drag & drop -------------------------------------------------------------
    @staticmethod
    def _unique_dest(folder, name):
        dest = os.path.join(folder, name)
        if not os.path.exists(dest):
            return dest
        stem, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(os.path.join(folder, f"{stem} ({i}){ext}")):
            i += 1
        return os.path.join(folder, f"{stem} ({i}){ext}")

    def _on_drop(self, _target, value, _x, _y):
        if not self.current:
            return False
        try:
            files = value.get_files()
        except Exception:
            return False
        srcs = [g.get_path() for g in files
                if g.get_path() and os.path.exists(g.get_path())]
        if not srcs:
            return False
        self._ingest_sources(srcs)
        return True

    def _ingest_sources(self, srcs):
        """Copy external files into the current folder, with conflict
        resolution and undo. Used by drag&drop AND clipboard paste."""
        conflicts = [s for s in srcs if os.path.exists(
            os.path.join(self.current, os.path.basename(s)))]
        if conflicts:
            dialog = Adw.MessageDialog(
                transient_for=self.window,
                heading=_("conflict_title", n=len(conflicts)),
                body=_("conflict_body"))
            dialog.add_response("cancel", _("cancel"))
            dialog.add_response("skip", _("skip"))
            dialog.add_response("keep", _("keep_both"))
            dialog.add_response("replace", _("replace_merge"))
            dialog.set_response_appearance(
                "replace", Adw.ResponseAppearance.SUGGESTED)

            def responded(_d, response):
                if response in ("skip", "keep", "replace"):
                    self._perform_drop(srcs, response)

            dialog.connect("response", responded)
            dialog.present()
        else:
            self._perform_drop(srcs, "keep")

    # -- copy with conflict mode + undo record -----------------------------------
    def _stash_dir(self):
        from .config import DATA_DIR
        d = os.path.join(DATA_DIR, "undo",
                         time.strftime("%Y%m%d-%H%M%S-") + str(os.getpid()))
        os.makedirs(d, exist_ok=True)
        return d

    def _stash(self, path, op):
        """Move an existing file/folder aside so undo can restore it."""
        import shutil
        stash = os.path.join(self._stash_dir(),
                             f"{len(op['backups'])}-{os.path.basename(path)}")
        shutil.move(path, stash)
        op["backups"].append((path, stash))

    def _merge_dir(self, src, dest, op):
        import shutil
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_root = dest if rel == "." else os.path.join(dest, rel)
            if not os.path.exists(target_root):
                os.makedirs(target_root)
                op["created"].append(target_root)
                # copy everything below in one go
                for name in files:
                    shutil.copy2(os.path.join(root, name),
                                 os.path.join(target_root, name))
                for d in list(dirs):
                    shutil.copytree(os.path.join(root, d),
                                    os.path.join(target_root, d))
                dirs.clear()
                continue
            for name in files:
                s = os.path.join(root, name)
                d = os.path.join(target_root, name)
                if os.path.exists(d):
                    self._stash(d, op)
                else:
                    op["created"].append(d)
                shutil.copy2(s, d)

    def _perform_drop(self, srcs, mode):
        import shutil
        op = {"created": [], "backups": []}
        count = 0
        for src in srcs:
            dest = os.path.join(self.current, os.path.basename(src))
            try:
                if os.path.exists(dest):
                    if mode == "skip":
                        continue
                    if mode == "keep":
                        dest = self._unique_dest(self.current,
                                                 os.path.basename(src))
                    elif mode == "replace":
                        if os.path.isdir(src) and os.path.isdir(dest):
                            self._merge_dir(src, dest, op)
                            count += 1
                            continue
                        self._stash(dest, op)
                if os.path.isdir(src):
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
                op["created"].append(dest)
                count += 1
            except OSError as e:
                self.window.toast(str(e))

        if op["created"] or op["backups"]:
            self._undo_stack.append(op)
            del self._undo_stack[:-10]
            self.undo_btn.set_sensitive(True)
        if count:
            toast = Adw.Toast(title=_("files_added", n=count), timeout=6)
            toast.set_button_label(_("undo"))
            toast.connect("button-clicked", lambda *a: self.undo_last())
            self.window.toaster.add_toast(toast)
            self.refresh()

    def undo_last(self):
        import shutil
        if not self._undo_stack:
            self.window.toast(_("nothing_undo"))
            return
        op = self._undo_stack.pop()
        n = 0
        # remove what was created (deepest paths first)
        for path in sorted(set(op["created"]), key=len, reverse=True):
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.remove(path)
                n += 1
            except OSError:
                pass
        # revert renames
        for old_path, new_path in reversed(op.get("renames", [])):
            try:
                os.rename(new_path, old_path)
                n += 1
            except OSError:
                pass
        # restore what was replaced
        for orig, stash in reversed(op["backups"]):
            try:
                os.makedirs(os.path.dirname(orig), exist_ok=True)
                if os.path.exists(orig):
                    if os.path.isdir(orig) and not os.path.islink(orig):
                        shutil.rmtree(orig, ignore_errors=True)
                    else:
                        os.remove(orig)
                shutil.move(stash, orig)
                n += 1
            except OSError:
                pass
        self.undo_btn.set_sensitive(bool(self._undo_stack))
        self.window.toast(_("undone", n=n))
        self.refresh()

    # -- navigation -------------------------------------------------------------
    def _navigate_to(self, path):
        """Change folder and record it in the back/forward history."""
        if path == self.current:
            return
        if self.search_entry.get_text():
            self.search_entry.set_text("")
            self.deep_btn.set_active(False)
        self.current = path
        del self._hist[self._hist_i + 1:]   # dropping the forward branch
        self._hist.append(path)
        self._hist_i = len(self._hist) - 1
        self.refresh()

    def go_back(self):
        if self._hist_i > 0:
            self._hist_i -= 1
            self.current = self._hist[self._hist_i]
            self.refresh()

    def go_forward(self):
        if self._hist_i < len(self._hist) - 1:
            self._hist_i += 1
            self.current = self._hist[self._hist_i]
            self.refresh()

    def _on_nav_button(self, gesture, _n, _x, _y):
        button = gesture.get_current_button()
        if button == 8:
            self.go_back()
        elif button == 9:
            self.go_forward()

    def _go_up(self, *_a):
        if self.current and self.current != self.root:
            self._navigate_to(os.path.dirname(self.current))

    def _go_media(self, *_a):
        if self.root:
            media = os.path.join(self.root, "media")
            os.makedirs(media, exist_ok=True)
            self._navigate_to(media)

    def _collect(self, deep):
        """(path, name, is_dir) for the current folder, or the whole project
        when a deep search is active."""
        items = []
        if deep:
            for base, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs
                           if d not in HIDDEN and not d.startswith(".")
                           and not is_encrypted_artifact(d, True)
                           and not _versions.hidden(base, d)]
                for d in dirs:
                    items.append((os.path.join(base, d), d, True))
                for f in files:
                    if not f.startswith(".") \
                            and not is_encrypted_artifact(f, False):
                        items.append((os.path.join(base, f), f, False))
                if len(items) > 4000:
                    break
            items.sort(key=lambda i: (not i[2], i[1].lower()))
            return items
        try:
            entries = sorted(
                os.scandir(self.current),
                key=lambda e: (not e.is_dir(follow_symlinks=False),
                               e.name.lower()))
        except OSError:
            return items
        for e in entries:
            if e.name in HIDDEN or e.name.startswith(".") \
                        or _versions.hidden(self.current, e.name):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if is_encrypted_artifact(e.name, is_dir):
                continue          # never show encrypted blobs
            items.append((e.path, e.name, is_dir))
        return items

    def _clear_search(self):
        self.search_entry.set_text("")
        self.deep_btn.set_active(False)
        self.refresh()

    def refresh(self):
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        if not self.current:
            return

        rel = os.path.relpath(self.current, self.root)
        self.path_label.set_text("/" if rel == "." else "/" + rel)
        self.up_btn.set_sensitive(self.current != self.root)

        query = self.search_entry.get_text().strip().lower()
        mode = FILTERS[self.filter_drop.get_selected()][0]
        deep = self.deep_btn.get_active() and bool(query)

        entries = self._collect(deep)

        shown = 0
        for path, name, is_dir in entries:
            if query and query not in name.lower():
                continue
            if not _matches_filter(name, is_dir, mode):
                continue
            title = _("media_files") if (is_dir and name == "media"
                                         and os.path.dirname(path)
                                         == self.root) else name
            row = Adw.ActionRow(title=title, activatable=True)
            logo = None if is_dir else _lang_icon_image(name)
            row.add_prefix(logo if logo else
                           Gtk.Image(icon_name=_file_icon(name, is_dir)))
            sub = ""
            if deep:  # show where the hit lives
                rel_dir = os.path.relpath(os.path.dirname(path), self.root)
                sub = "/" if rel_dir == "." else "/" + rel_dir
            if is_dir:
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic",
                                         css_classes=["dim-label"]))
            else:
                try:
                    size = human_size(os.path.getsize(path))
                    sub = f"{sub}  ·  {size}" if sub else size
                except OSError:
                    pass
            if sub:
                row.set_subtitle(sub)
            row.connect("activated", self._on_activate, path, is_dir)
            row._path = path
            row._is_dir = is_dir
            self.listbox.append(row)
            shown += 1
            if shown >= 500:      # keep huge trees responsive
                break

        if shown == 0:
            if query:
                hint = Adw.ActionRow(title=_("no_matches", q=query))
                hint.add_prefix(Gtk.Image(icon_name="edit-find-symbolic"))
            else:
                hint = Adw.ActionRow(title=_("empty_folder"),
                                     subtitle=_("drop_hint"))
                hint.add_prefix(Gtk.Image(icon_name="insert-object-symbolic"))
            hint.set_selectable(False)
            hint._path = None
            self.listbox.append(hint)

        if query or mode != "all":
            self.path_label.set_text(
                f'{self.path_label.get_text()}   —   {_("results_n", n=shown)}')

        run_async(lambda: detect.project_summary(self.root),
                  self._status_ready)

    def _status_ready(self, info, error):
        if error or not info:
            return
        if info["dirty"]:
            sync = _("changes_pending")
        elif info["ahead_behind"] and info["ahead_behind"] != (0, 0):
            a, b = info["ahead_behind"]
            sync = _("ahead_behind", a=a, b=b)
        elif info["remote"]:
            sync = _("up_to_date")
        else:
            sync = _("no_remote")
        parts = [info["branch"] or "—", sync,
                 _("procs_n", n=len(info["processes"]))]
        self.status_line.set_text("   ·   ".join(parts))

    # -- opening ----------------------------------------------------------------
    def _on_activate(self, _row, path, is_dir):
        if is_dir:
            self._navigate_to(path)
            return
        ext = os.path.splitext(path)[1].lower()
        binary_ext = IMAGE_EXT | AUDIO_EXT | VIDEO_EXT | {
            ".pdf", ".zip", ".tar", ".gz", ".deb", ".so", ".bin", ".exe"}
        try:
            too_big = os.path.getsize(path) > MAX_EDIT_SIZE
        except OSError:
            too_big = False
        if ext in binary_ext or too_big:
            Gio.AppInfo.launch_default_for_uri(f"file://{path}", None)
            return
        try:  # confirm it is readable text
            with open(path, "r", encoding="utf-8") as f:
                f.read(4096)
        except (UnicodeDecodeError, OSError):
            Gio.AppInfo.launch_default_for_uri(f"file://{path}", None)
            return
        self.window.open_file(path)


# --------------------------------------------------------------------------- #
# Editor — built-in multi-tab code editor with micro-style shortcuts
# --------------------------------------------------------------------------- #
class EditorTab(Gtk.Box):
    """One open file. micro keybindings: Ctrl+S save, Ctrl+Q close,
    Ctrl+F find (Enter = next, Esc = close), Ctrl+K cut line,
    Ctrl+D duplicate line; Ctrl+Z / Ctrl+Y native undo/redo."""

    def __init__(self, window, editor_page, path):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.editor_page = editor_page
        self.path = path

        # sections toolbar
        sec_bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=2,
                          margin_start=10, margin_end=10)
        self.sec_pop = Gtk.Popover()
        sec_btn = Gtk.MenuButton(popover=self.sec_pop, css_classes=["flat"])
        sec_btn.set_child(Adw.ButtonContent(icon_name="view-list-symbolic",
                                            label=_("sections")))
        self.sec_pop.connect("show", lambda *a: self._rebuild_sections())
        sec_bar.append(sec_btn)
        self.focus_clear_btn = Gtk.Button(css_classes=["flat"],
                                          visible=False,
                                          tooltip_text=_("clear_focus"))
        self.focus_clear_btn.set_child(Adw.ButtonContent(
            icon_name="edit-clear-symbolic", label=_("clear_focus")))
        self.focus_clear_btn.connect("clicked",
                                     lambda *a: self.clear_focus())
        sec_bar.append(self.focus_clear_btn)
        self.append(sec_bar)

        # find bar
        self.search_rev = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        sbox = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6,
                       margin_start=10, margin_end=10)
        self.search_entry = Gtk.SearchEntry(hexpand=True,
                                            placeholder_text=_("find"))
        self.search_entry.connect("activate", self._find_next)
        self.search_entry.connect("search-changed", self._find_next)
        sbox.append(self.search_entry)
        self.search_rev.set_child(sbox)
        self.append(self.search_rev)

        # buffer + view
        if HAVE_SRC:
            self.buffer = GtkSource.Buffer()
            lang = GtkSource.LanguageManager.get_default().guess_language(
                path, None)
            if lang:
                self.buffer.set_language(lang)
            dark = Adw.StyleManager.get_default().get_dark()
            scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(
                "Adwaita-dark" if dark else "Adwaita")
            if scheme:
                self.buffer.set_style_scheme(scheme)
            self.view = GtkSource.View(
                css_classes=["petacore-editor"],
                buffer=self.buffer, monospace=True,
                show_line_numbers=True, highlight_current_line=True,
                auto_indent=True, insert_spaces_instead_of_tabs=True,
                tab_width=4,
                top_margin=8, bottom_margin=8,
                left_margin=8, right_margin=8)
        else:
            self.buffer = Gtk.TextBuffer()
            self.view = Gtk.TextView(buffer=self.buffer, monospace=True,
                                     css_classes=["petacore-editor"],
                                     top_margin=8, bottom_margin=8,
                                     left_margin=8, right_margin=8)

        try:
            with open(path, "r", encoding="utf-8") as f:
                self.buffer.set_text(f.read())
        except OSError as e:
            self.buffer.set_text(f"# {e}")
        self.buffer.set_modified(False)
        self.buffer.connect("modified-changed", self._title_changed)

        self.dim_tag = self.buffer.create_tag(
            "petacore-dim", foreground="#8a8a8a")
        self._focused = False

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.view.add_controller(key)

        self.append(_scrolled(self.view))

    # -- sections & focus mode ----------------------------------------------------
    def _project_root(self):
        project = config.active_project()
        return project["path"] if project else os.path.dirname(self.path)

    def _sections_store(self):
        root = self._project_root()
        return (os.path.join(root, ".petacore", "sections.json"),
                os.path.relpath(self.path, root))

    def _load_sections(self):
        import json
        store, rel = self._sections_store()
        try:
            with open(store, "r", encoding="utf-8") as f:
                return json.load(f).get(rel, [])
        except (OSError, ValueError):
            return []

    def _save_sections(self, sections):
        import json
        store, rel = self._sections_store()
        data = {}
        try:
            with open(store, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
        data[rel] = sections
        os.makedirs(os.path.dirname(store), exist_ok=True)
        with open(store, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def _rebuild_sections(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8)
        sections = self._load_sections()
        if not sections:
            box.append(Gtk.Label(label=_("no_sections"),
                                 css_classes=["dim-label"],
                                 margin_start=6, margin_end=6))
        for i, sec in enumerate(sections):
            row = Gtk.Box(spacing=4)
            btn = Gtk.Button(css_classes=["flat"], hexpand=True)
            btn.set_child(Gtk.Label(
                label=f'{sec["name"]}   ({sec["start"]}\u2013{sec["end"]})',
                xalign=0))
            btn.connect("clicked", self._goto_section,
                        sec["start"], sec["end"])
            row.append(btn)
            rm = Gtk.Button(icon_name="user-trash-symbolic",
                            css_classes=["flat"])
            rm.connect("clicked", self._remove_section, i)
            row.append(rm)
            box.append(row)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        add = Gtk.Button(css_classes=["flat"])
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                        label=_("add_section")))
        add.connect("clicked", self._add_section_dialog)
        box.append(add)
        self.sec_pop.set_child(box)

    def _remove_section(self, _btn, index):
        sections = self._load_sections()
        if 0 <= index < len(sections):
            del sections[index]
            self._save_sections(sections)
        self._rebuild_sections()

    def _add_section_dialog(self, _btn):
        self.sec_pop.popdown()
        total = max(1, self.buffer.get_line_count())
        bounds = self.buffer.get_selection_bounds()
        if bounds:
            s = bounds[0].get_line() + 1
            e = bounds[1].get_line() + 1
        else:
            cur = self.buffer.get_iter_at_mark(
                self.buffer.get_insert()).get_line() + 1
            s, e = cur, min(cur + 10, total)

        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("add_section"))
        grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        name_entry = Gtk.Entry(placeholder_text=_("section_name"))
        grid.append(name_entry)
        row = Gtk.Box(spacing=8)
        start_spin = Gtk.SpinButton.new_with_range(1, total, 1)
        start_spin.set_value(s)
        end_spin = Gtk.SpinButton.new_with_range(1, total, 1)
        end_spin.set_value(e)
        row.append(Gtk.Label(label=_("start_line")))
        row.append(start_spin)
        row.append(Gtk.Label(label=_("end_line")))
        row.append(end_spin)
        grid.append(row)
        dialog.set_extra_child(grid)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("add", _("add_section"))
        dialog.set_response_appearance("add",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_d, response):
            if response != "add":
                return
            name = name_entry.get_text().strip() or _("sections")
            a = int(start_spin.get_value())
            b = int(end_spin.get_value())
            if a > b:
                a, b = b, a
            sections = self._load_sections()
            sections.append({"name": name, "start": a, "end": b})
            sections.sort(key=lambda x: x["start"])
            self._save_sections(sections)

        dialog.connect("response", responded)
        dialog.present()

    def _line_start_iter(self, n):
        it = self.buffer.get_iter_at_line(n)
        return it[1] if isinstance(it, tuple) else it

    def _goto_section(self, _btn, start, end):
        self.sec_pop.popdown()
        it = self._line_start_iter(max(0, start - 1))
        self.buffer.place_cursor(it)
        self.view.scroll_to_iter(it, 0.1, True, 0, 0.1)
        self.view.grab_focus()
        if config.get("focus_mode"):
            self._apply_focus(start, end)

    def _apply_focus(self, start, end):
        b = self.buffer
        s_all, e_all = b.get_bounds()
        b.remove_tag(self.dim_tag, s_all, e_all)
        sec_start = self._line_start_iter(max(0, start - 1))
        if end >= b.get_line_count():
            sec_end = b.get_end_iter()
        else:
            sec_end = self._line_start_iter(end)  # first line after section
        b.apply_tag(self.dim_tag, s_all, sec_start)
        b.apply_tag(self.dim_tag, sec_end, e_all)
        self._focused = True
        self.focus_clear_btn.set_visible(True)

    def clear_focus(self):
        s_all, e_all = self.buffer.get_bounds()
        self.buffer.remove_tag(self.dim_tag, s_all, e_all)
        self._focused = False
        self.focus_clear_btn.set_visible(False)

    # -- helpers ----------------------------------------------------------------
    def _title_changed(self, *_a):
        self.editor_page.update_title(self)

    def title(self):
        name = os.path.basename(self.path)
        return ("● " + name) if self.buffer.get_modified() else name

    def _text(self):
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, False)

    # -- micro keybindings --------------------------------------------------------
    def _on_key(self, _ctrl, keyval, _keycode, state):
        from gi.repository import Gdk
        if keyval == Gdk.KEY_Escape and self.search_rev.get_reveal_child():
            self.search_rev.set_reveal_child(False)
            self.view.grab_focus()
            return True
        if keyval == Gdk.KEY_Escape and self._focused:
            self.clear_focus()
            return True
        if keyval == Gdk.KEY_Escape:
            self._escape_leave()
            return True
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        name = (Gdk.keyval_name(keyval) or "").lower()
        if name == "s":
            self.save()
            return True
        if name == "q":
            self.editor_page.request_close(self)
            return True
        if name == "f":
            show = not self.search_rev.get_reveal_child()
            self.search_rev.set_reveal_child(show)
            if show:
                self.search_entry.grab_focus()
            return True
        if name == "k":
            self._cut_line()
            return True
        if name == "d":
            self._dup_line()
            return True
        return False

    def _escape_leave(self):
        """Esc leaves the file and returns to the previous folder.

        Text and code get a confirmation first (it is easy to hit Esc by
        accident while editing); images and video close straight away, and
        the confirmation can be turned off in Settings.

        While the confirmation is up:
          Esc        → leave without saving
          Ctrl+Esc   → save (with a brief animation) and leave
        """
        ext = os.path.splitext(self.path)[1].lower()
        is_media = ext in IMAGE_EXT or ext in AUDIO_EXT or ext in VIDEO_EXT
        if is_media or not config.get("esc_confirm"):
            self.editor_page.request_close(self)
            return

        dialog = Adw.MessageDialog(
            transient_for=self.window,
            heading=_("esc_question"),
            body=_("esc_detail") + "\n\n" + _("esc_hint_keys"))
        dialog.add_response("stay", _("esc_stay"))
        dialog.add_response("save", _("esc_save_leave"))
        dialog.add_response("leave", _("esc_leave"))
        dialog.set_response_appearance("leave",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("leave")
        dialog.set_close_response("stay")

        handled = {"done": False}

        def on_key(_ctrl, keyval, _code, state):
            from gi.repository import Gdk
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            name = (Gdk.keyval_name(keyval) or "").lower()
            if (ctrl and name == "s") or \
                    (keyval == Gdk.KEY_Escape and ctrl):
                handled["done"] = True
                dialog.close()
                self._save_then_leave()                   # save, then leave
                return True
            if keyval == Gdk.KEY_Escape:
                handled["done"] = True
                dialog.close()
                self.editor_page.request_close(self)      # leave, no saving
                return True
            return False

        key = Gtk.EventControllerKey()
        # CAPTURE runs before the dialog's own Escape-closes-it handling,
        # otherwise the second Esc never reaches us.
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", on_key)
        dialog.add_controller(key)

        def responded(_d, response):
            if handled["done"]:
                return
            if response == "leave":
                self.editor_page.request_close(self)
            elif response == "save":
                self._save_then_leave()
            else:
                self.view.grab_focus()

        dialog.connect("response", responded)
        dialog.present()

    def _save_then_leave(self):
        """Save, show the floppy flying up, then close the tab. The tab is
        closed almost immediately so the app feels instant — the animation
        lives on the window overlay and keeps playing on its own."""
        self.save(prompt_deploy=False)
        self.window.show_save_animation(os.path.basename(self.path))
        GLib.timeout_add(120,
                         lambda: (self.editor_page.request_close(self),
                                  False)[1])

    def _line_bounds(self):
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        start = it.copy()
        start.set_line_offset(0)
        end = start.copy()
        if not end.forward_line():        # last line: go to buffer end
            end = self.buffer.get_end_iter()
        return start, end

    def _cut_line(self):
        start, end = self._line_bounds()
        text = self.buffer.get_text(start, end, False)
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(text)
        self.buffer.begin_user_action()
        self.buffer.delete(start, end)
        self.buffer.end_user_action()

    def _dup_line(self):
        start, end = self._line_bounds()
        text = self.buffer.get_text(start, end, False)
        if not text.endswith("\n"):
            text = "\n" + text
        self.buffer.begin_user_action()
        self.buffer.insert(end, text)
        self.buffer.end_user_action()

    def _find_next(self, *_a):
        query = self.search_entry.get_text()
        if not query:
            return
        text = self._text()
        cursor = self.buffer.get_iter_at_mark(
            self.buffer.get_insert()).get_offset()
        idx = text.find(query, cursor)
        if idx == -1:
            idx = text.find(query)        # wrap around
        if idx == -1:
            return
        start = self.buffer.get_iter_at_offset(idx)
        end = self.buffer.get_iter_at_offset(idx + len(query))
        self.buffer.select_range(end, start)
        self.view.scroll_to_iter(start, 0.2, False, 0, 0)

    # -- saving -----------------------------------------------------------------
    def save(self, prompt_deploy=True):
        content = self._text()
        if not content.endswith("\n"):
            content += "\n"
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            self.window.toast(str(e))
            return False
        self.buffer.set_modified(False)
        self.window.toast(_("saved", f=os.path.basename(self.path)))
        if prompt_deploy:
            self.window.prompt_deploy(self.path)
        return True


class EditorPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window

        self.tabview = Adw.TabView(vexpand=True)
        self.tabview.connect("close-page", self._on_close_page)
        self.tabview.connect("notify::n-pages", self._on_pages_changed)
        tabbar = Adw.TabBar(view=self.tabview, autohide=False)

        actions = Gtk.Box(spacing=2)
        new_btn = Gtk.Button(icon_name="tab-new-symbolic",
                             tooltip_text=_("new_tab"),
                             css_classes=["flat"])
        new_btn.connect("clicked", self.new_tab)
        actions.append(new_btn)
        save_btn = Gtk.Button(icon_name="document-save-symbolic",
                              tooltip_text=_("save") + " (Ctrl+S)",
                              css_classes=["flat"])
        save_btn.connect("clicked", self._save_current)
        actions.append(save_btn)
        tabbar.set_end_action_widget(actions)

        tabs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tabs_box.append(tabbar)
        tabs_box.append(self.tabview)

        empty = Adw.StatusPage(icon_name="accessories-text-editor-symbolic",
                               title=_("no_file_open"),
                               description=_("no_file_hint"), vexpand=True)

        self.mode_stack = Gtk.Stack(vexpand=True)
        self.mode_stack.add_named(empty, "empty")
        self.mode_stack.add_named(tabs_box, "tabs")
        self.append(self.mode_stack)

    def save_all(self):
        """Save every modified open tab (no deploy prompts)."""
        n = 0
        for i in range(self.tabview.get_n_pages()):
            tab = self.tabview.get_nth_page(i).get_child()
            if tab.buffer.get_modified():
                if tab.save(prompt_deploy=False):
                    n += 1
        return n

    def new_tab(self, *_a):
        """Save all open tabs, then open a fresh untitled file."""
        saved = self.save_all()
        if saved:
            self.window.toast(_("all_saved", n=saved))

        project = config.active_project()
        folder = None
        proj_page = self.window.pages.get("project")
        if proj_page and proj_page.current:
            folder = proj_page.current
        elif project:
            folder = project["path"]
        else:
            folder = os.path.expanduser("~")

        base = _("untitled")
        name, i = f"{base}.txt", 1
        while os.path.exists(os.path.join(folder, name)):
            name = f"{base}-{i}.txt"
            i += 1
        path = os.path.join(folder, name)
        try:
            open(path, "w", encoding="utf-8").close()
        except OSError as e:
            self.window.toast(str(e))
            return
        self.open_file(path)
        if proj_page:
            proj_page.refresh()

    def open_file(self, path):
        path = os.path.realpath(path)
        for i in range(self.tabview.get_n_pages()):
            page = self.tabview.get_nth_page(i)
            if page.get_child().path == path:
                self.tabview.set_selected_page(page)
                return
        tab = EditorTab(self.window, self, path)
        page = self.tabview.append(tab)
        page.set_title(tab.title())
        page.set_icon(Gio.ThemedIcon.new("text-x-generic-symbolic"))
        self.tabview.set_selected_page(page)
        tab.view.grab_focus()

    def update_title(self, tab):
        page = self.tabview.get_page(tab)
        if page:
            page.set_title(tab.title())

    def request_close(self, tab):
        page = self.tabview.get_page(tab)
        if page:
            self.tabview.close_page(page)

    def _save_current(self, *_a):
        page = self.tabview.get_selected_page()
        if page:
            page.get_child().save()

    def _on_close_page(self, view, page):
        tab = page.get_child()
        if not tab.buffer.get_modified():
            view.close_page_finish(page, True)
            return True
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            heading=_("unsaved_question", f=os.path.basename(tab.path)))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("discard", _("discard"))
        dialog.add_response("save", _("save"))
        dialog.set_response_appearance("discard",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_d, response):
            if response == "save":
                view.close_page_finish(page, tab.save())
            elif response == "discard":
                view.close_page_finish(page, True)
            else:
                view.close_page_finish(page, False)

        dialog.connect("response", responded)
        dialog.present()
        return True

    def _on_pages_changed(self, view, _pspec):
        self.mode_stack.set_visible_child_name(
            "tabs" if view.get_n_pages() > 0 else "empty")
        if view.get_n_pages() == 0 \
                and self.window.stack.get_visible_child_name() == "editor":
            self.window.select_page("project")


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
class SnapshotsPage(Gtk.Box):
    KIND_KEYS = {"manual": "manual", "auto": "auto",
                 "safety": "safety", "apply": "before_apply"}

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=18, margin_bottom=18,
                         margin_start=18, margin_end=18)
        self.window = window

        top = Gtk.Box(spacing=6)
        self.create_btn = Gtk.Button(label=_("create_snapshot"),
                                     css_classes=["suggested-action"])
        self.create_btn.connect("clicked", self._on_create)
        top.append(self.create_btn)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text=_("refresh"))
        refresh.connect("clicked", lambda *a: self.refresh())
        top.append(refresh)
        self.append(top)

        self.listbox = Gtk.ListBox(css_classes=["boxed-list"],
                                   selection_mode=Gtk.SelectionMode.NONE,
                                   valign=Gtk.Align.START)
        self.append(_scrolled(self.listbox))
        self.refresh()

    def _manager(self):
        project = config.active_project()
        return SnapshotManager(project["path"]) if project else None

    def refresh(self):
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        mgr = self._manager()
        if not mgr:
            return
        snaps = mgr.list()
        if not snaps:
            self.listbox.append(Adw.ActionRow(title=_("no_snapshots")))
            return
        for snap in snaps:
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(snap["time"])) if snap["time"] else snap["name"]
            kind = _(self.KIND_KEYS.get(snap["kind"], "manual"))
            subtitle = f"{kind} · {human_size(snap['size'])}"
            if snap["label"]:
                subtitle += f" · {snap['label']}"
            row = Adw.ActionRow(title=when, subtitle=subtitle)
            row.add_prefix(Gtk.Image(icon_name="document-save-symbolic"))

            restore = Gtk.Button(label=_("restore"), valign=Gtk.Align.CENTER)
            restore.connect("clicked", self._on_restore, snap["name"])
            row.add_suffix(restore)

            delete = Gtk.Button(icon_name="user-trash-symbolic",
                                valign=Gtk.Align.CENTER, css_classes=["flat"])
            delete.connect("clicked", self._on_delete, snap["name"])
            row.add_suffix(delete)
            self.listbox.append(row)

    def _on_create(self, _btn):
        mgr = self._manager()
        if not mgr:
            return
        self.create_btn.set_sensitive(False)
        run_async(lambda: mgr.create(kind="manual"), self._created)

    def _created(self, _result, error):
        self.create_btn.set_sensitive(True)
        self.window.toast(str(error) if error else _("snapshot_created"))
        self.refresh()

    def _on_restore(self, _btn, name):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("restore_question"),
                                   body=_("restore_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("restore", _("restore"))
        dialog.set_response_appearance("restore",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._restore_confirmed, name)
        dialog.present()

    def _restore_confirmed(self, _dialog, response, name):
        if response != "restore":
            return
        mgr = self._manager()
        run_async(lambda: mgr.restore(name), self._restored)

    def _restored(self, _result, error):
        self.window.toast(str(error) if error else _("restored"))
        self.refresh()
        self.window.refresh_pages()

    def _on_delete(self, _btn, name):
        mgr = self._manager()
        if mgr:
            mgr.delete(name)
        self.refresh()


# --------------------------------------------------------------------------- #
# Terminal — multi-tab, GNOME Console style
# --------------------------------------------------------------------------- #
class TerminalPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self._count = 0

        if not HAVE_VTE:
            self.append(Adw.StatusPage(
                icon_name="utilities-terminal-symbolic",
                title=_("terminal"),
                description="gir1.2-vte-3.91 is required for the integrated "
                            "terminal (sudo apt install gir1.2-vte-3.91)",
                vexpand=True))
            return

        self.tabview = Adw.TabView(vexpand=True)
        tabbar = Adw.TabBar(view=self.tabview, autohide=False)
        new_btn = Gtk.Button(icon_name="tab-new-symbolic",
                             tooltip_text=_("new_tab"),
                             css_classes=["flat"])
        new_btn.connect("clicked", lambda *a: self.new_tab())
        tabbar.set_end_action_widget(new_btn)

        self.append(tabbar)
        self.append(self.tabview)

    def _cwd(self):
        project = config.active_project()
        return project["path"] if project else os.path.expanduser("~")

    def new_tab(self):
        if not HAVE_VTE:
            return
        self._count += 1
        term = make_terminal()
        shell = os.environ.get("SHELL", "/bin/bash")
        term.spawn_async(Vte.PtyFlags.DEFAULT, self._cwd(), [shell], None,
                         GLib.SpawnFlags.DEFAULT, None, None, -1, None, None)

        content = terminal_frame(term)
        page = self.tabview.append(content)
        page.set_title(f"{_('terminal')} {self._count}")
        page.set_icon(Gio.ThemedIcon.new("utilities-terminal-symbolic"))
        self.tabview.set_selected_page(page)

        # Close the tab automatically when its shell exits.
        term.connect("child-exited",
                     lambda *a: self._close_if_open(page))
        term.grab_focus()

    def _close_if_open(self, page):
        try:
            self.tabview.close_page(page)
        except Exception:  # page already gone
            pass

    def spawn(self):
        """Ensure at least one tab exists (called when the page is shown)."""
        if HAVE_VTE and self.tabview.get_n_pages() == 0:
            self.new_tab()


# --------------------------------------------------------------------------- #
# Package builder — .deb / .rpm with one-click GPG signing
# --------------------------------------------------------------------------- #
from . import gpgsign  # noqa: E402


class PackagePage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("package"))

        self.version_row = Adw.EntryRow(title=_("version"), text="1.0.0")
        group.add(self.version_row)

        user = os.environ.get("USER", "developer")
        self.maint_row = Adw.EntryRow(title=_("maintainer"),
                                      text=f"{user} <{user}@localhost>")
        group.add(self.maint_row)

        self.desc_row = Adw.EntryRow(title=_("description_f"))
        group.add(self.desc_row)

        from .debbuild import LICENSE_CHOICES
        self._license_ids = [spdx for spdx, _d in LICENSE_CHOICES]
        model = Gtk.StringList.new([desc for _s, desc in LICENSE_CHOICES]
                                   + [_("license_other")])
        self.license_row = Adw.ComboRow(title=_("license_f"),
                                        subtitle=_("license_hint"),
                                        subtitle_lines=2, model=model)
        self.license_row.set_selected(0)
        self.license_row.connect("notify::selected", self._on_license_choice)
        group.add(self.license_row)

        # Only shown when "Other" is picked, so the common path stays a
        # single choice rather than a blank field to fill in.
        self.license_custom = Adw.EntryRow(title=_("license_custom"),
                                           visible=False)
        group.add(self.license_custom)

        # Choosing a licence is a decision with consequences, so the
        # explanation is one click away rather than something to search for.
        learn_row = Adw.ActionRow(title=_("license_learn"), activatable=True)
        learn_row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        learn_row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        learn_row.connect(
            "activated",
            lambda *a: Gio.AppInfo.launch_default_for_uri(
                debbuild.LICENSE_GUIDE_URL, None))
        group.add(learn_row)
        page.add(group)

        # -- GPG signing, explained simply -------------------------------------
        gpg_group = Adw.PreferencesGroup(title=_("gpg_title"),
                                         description=_("gpg_explain"))
        self.key_row = Adw.ActionRow(subtitle_lines=2)
        self.key_row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
        self.key_btn = Gtk.Button(label=_("create_key"),
                                  valign=Gtk.Align.CENTER)
        self.key_btn.connect("clicked", self._on_create_key)
        self.key_row.add_suffix(self.key_btn)
        self.pub_btn = Gtk.Button(icon_name="send-to-symbolic",
                                  valign=Gtk.Align.CENTER,
                                  css_classes=["flat"], visible=False,
                                  tooltip_text=_("export_pubkey"))
        self.pub_btn.connect("clicked", self._on_export_pubkey)
        self.key_row.add_suffix(self.pub_btn)
        gpg_group.add(self.key_row)

        self.sign_switch = Adw.SwitchRow(title=_("sign_packages"),
                                         active=config.get("sign_packages"))
        self.sign_switch.connect(
            "notify::active",
            lambda r, _p: config.set("sign_packages", r.get_active()))
        gpg_group.add(self.sign_switch)
        page.add(gpg_group)
        self._refresh_key()

        # -- straight into the archive ------------------------------------------
        # Building a package and then forgetting to put it in the repository
        # is the most common way an update never reaches anyone, so the two
        # steps are joined here — with the index left alone, because signing
        # it is a decision of its own.
        from . import repo as _repo
        repo_group = Adw.PreferencesGroup(title=_("repo_page"),
                                          description=_("repo_autoadd_hint"))
        self.repo_switch = Adw.SwitchRow(title=_("repo_autoadd"),
                                         active=config.get("repo_autoadd"))
        self.repo_switch.connect(
            "notify::active",
            lambda r, _p: config.set("repo_autoadd", r.get_active()))
        target = _repo.active_profile()
        self.repo_switch.set_subtitle(target["name"] if target
                                      else _("repo_none_title"))
        self.repo_switch.set_sensitive(bool(target))
        repo_group.add(self.repo_switch)
        page.add(repo_group)

        # -- export buttons -------------------------------------------------------
        action = Adw.PreferencesGroup()
        btns = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.deb_btn = Gtk.Button(label=_("export_deb"),
                                  css_classes=["suggested-action", "pill"])
        self.deb_btn.connect("clicked", self._on_export, "deb")
        btns.append(self.deb_btn)
        self.rpm_btn = Gtk.Button(label=_("export_rpm"), css_classes=["pill"])
        self.rpm_btn.connect("clicked", self._on_export, "rpm")
        btns.append(self.rpm_btn)
        action.add(btns)

        self.status = Gtk.Label(css_classes=["dim-label"], wrap=True,
                                margin_top=8, selectable=True)
        action.add(self.status)
        page.add(action)

        self.append(_scrolled(page))

    # -- GPG key ------------------------------------------------------------------
    def _refresh_key(self):
        chosen = config.get("gpg_key")

        def work():
            key = gpgsign.signing_key(chosen)
            status = gpgsign.key_status(key[0]) if key else None
            # Distinguish "no key at all" from "the key you picked is gone":
            # the second is worth saying out loud, because it means nothing
            # will be signed until it is sorted out.
            missing = bool(chosen and key is None
                           and gpgsign.list_secret_keys())
            return key, status, missing

        def done(result, _err):
            if not result:
                return
            key, status, missing = result
            self._key = key
            if key:
                # The full fingerprint is what actually identifies a key —
                # a name and address can be shared by several.
                fingerprint = gpgsign.pretty_fingerprint(key[0])
                detail = f'{status["algo"]}  \u00b7  {fingerprint}' \
                    if status else fingerprint
                if status and status.get("expired"):
                    self.key_row.set_title(
                        f'{_("signing_as")}: {key[1]}  ({_("key_expired")})')
                    self.key_row.set_subtitle(
                        _("key_expired_warn") + "\n" + detail)
                    self.key_row.add_css_class("error")
                else:
                    self.key_row.remove_css_class("error")
                    self.key_row.set_title(f'{_("signing_as")}: {key[1]}')
                    self.key_row.set_subtitle(detail)
                self.key_row.set_subtitle_lines(3)
                self.key_btn.set_visible(False)
                self.pub_btn.set_visible(True)
            else:
                self.key_row.remove_css_class("error")
                self.key_row.set_title(
                    _("key_missing") if missing else _("no_key"))
                self.key_row.set_subtitle("")
                self.key_btn.set_visible(True)
                self.pub_btn.set_visible(False)
        run_async(work, done)

    def _on_create_key(self, _btn):
        from .dialogs import GpgKeyDialog
        maint = self.maint_row.get_text()
        name, email = maint, ""
        if "<" in maint and ">" in maint:
            name = maint.split("<")[0].strip()
            email = maint.split("<")[1].split(">")[0].strip()

        def created():
            self.window.toast(_("key_created"))
            self._refresh_key()

        GpgKeyDialog(self.window, default_name=name, default_email=email,
                     on_done=created).present()

    def _on_export_pubkey(self, _btn):
        if not getattr(self, "_key", None):
            return
        fd = Gtk.FileDialog(title=_("export_pubkey"),
                            initial_name="public-key.asc")
        fd.save(self.window, None, self._pubkey_dest)

    def _pubkey_dest(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        if not gfile:
            return
        dest = gfile.get_path()
        run_async(lambda: gpgsign.export_public(self._key[0], dest),
                  lambda p, e: self.window.toast(
                      str(e) if e else _("pubkey_done", p=p)))

    # -- export -------------------------------------------------------------------
    def _on_license_choice(self, row, _pspec):
        picking_other = row.get_selected() >= len(self._license_ids)
        self.license_custom.set_visible(picking_other)

    def _chosen_license(self):
        index = self.license_row.get_selected()
        if index < len(self._license_ids):
            return self._license_ids[index]
        return self.license_custom.get_text().strip() or "LicenseRef-proprietary"

    def _add_to_repo(self, deb_path):
        """Put a freshly built package into the active repository.

        RPMs are left out on purpose: this repository is an APT archive, and
        quietly copying an .rpm into it would produce a file nothing reads.
        """
        from . import repo
        profile = repo.active_profile()
        if not profile:
            return

        def done(result, error):
            if error:
                self.window.toast(str(error))
                return
            added, failures = result
            if added:
                self.window.toast(_("repo_added_to", n=profile["name"]))
                page = self.window.pages.get("repo_page")
                if page:
                    page.refresh()
            elif failures:
                self.window.toast(_("repo_add_failed", n=len(failures)))

        run_async(lambda: repo.add_packages(profile, [deb_path]), done)

    def _on_export(self, _btn, fmt):
        project = config.active_project()
        if not project:
            return
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            heading=_("export_question"),
            body=_("export_detail", n=project["name"].lower()))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("export", _("export_button"))
        dialog.set_response_appearance("export",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._confirmed, project, fmt)
        dialog.present()

    def _confirmed(self, _dialog, response, project, fmt):
        if response != "export":
            return
        file_dialog = Gtk.FileDialog(title=_("export_deb") if fmt == "deb"
                                     else _("export_rpm"))
        file_dialog.select_folder(self.window, None, self._folder_chosen,
                                  project, fmt)

    def _folder_chosen(self, dialog, result, project, fmt):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if not folder:
            return
        out_dir = folder.get_path()
        self.deb_btn.set_sensitive(False)
        self.rpm_btn.set_sensitive(False)
        self.status.set_text(_("building"))

        version = self.version_row.get_text()
        maint = self.maint_row.get_text()
        desc = self.desc_row.get_text()
        license_name = self._chosen_license()

        def work():
            builder = (debbuild.build_deb if fmt == "deb"
                       else debbuild.build_rpm)
            path = builder(project["path"], out_dir, project["name"],
                           version, maint, desc, license_name,
                           debbuild.repo_homepage(project.get("repo_url")))
            signed = None
            if config.get("sign_packages"):
                key = gpgsign.signing_key(config.get("gpg_key"))
                if key:
                    try:
                        if fmt == "rpm":
                            signed = gpgsign.sign_rpm(path, key[1])
                        else:
                            signed = gpgsign.sign_deb(path, key[0])
                    except gpgsign.GpgError as e:
                        signed = f"!{e}"
            return path, signed

        def done(res, error):
            self.deb_btn.set_sensitive(True)
            self.rpm_btn.set_sensitive(True)
            if error:
                self.status.set_text(f"{_('export_failed')}: {error}")
                return
            path, signed = res
            text = _("export_done", p=path)
            if signed:
                if str(signed).startswith("!"):
                    text += "\n" + _("sign_failed") + ": " + str(signed)[1:]
                else:
                    text += "\n" + _("signed_ok", p=os.path.basename(str(signed)))
            self.status.set_text(text)
            self.window.toast(_("export_done", p=os.path.basename(path)))
            if fmt == "deb" and config.get("repo_autoadd"):
                self._add_to_repo(path)
            # Say what was deliberately withheld, so the omission is never a
            # surprise and never silent.
            project = config.active_project()
            if project:
                left_out = debbuild.find_secrets(project["path"])
                if left_out:
                    self.window.toast(
                        _("secrets_left_out", n=len(left_out)))

        run_async(work, done)


# --------------------------------------------------------------------------- #
# Versions — the project's release history, and making the next release
# --------------------------------------------------------------------------- #
from . import versions  # noqa: E402

# Display order of the release types. Stable first, because it is what most
# releases are; the rest in the order a release moves through them.
CHANNEL_ORDER = ["stable", "rc", "beta", "alpha"]
CHANNEL_ICONS = {"stable": "emblem-ok-symbolic",
                 "rc": "starred-symbolic",
                 "beta": "applications-science-symbolic",
                 "alpha": "applications-engineering-symbolic"}


# Colours come from libadwaita's own palette, so both the light and the
# dark style get a shade that was chosen for them rather than one guessed
# here. One colour per release type, used for the dot on the timeline, the
# pill, and the wash behind the latest release.
_VERSIONS_CSS = """
.pc-ver-hero {
  border-radius: 18px; padding: 20px 22px;
  border: 1px solid alpha(currentColor, 0.08);
}
.pc-ver-hero.stable { background: linear-gradient(135deg, alpha(@green_3, 0.28), alpha(@green_3, 0.04) 70%); }
.pc-ver-hero.rc     { background: linear-gradient(135deg, alpha(@purple_2, 0.28), alpha(@purple_2, 0.04) 70%); }
.pc-ver-hero.beta   { background: linear-gradient(135deg, alpha(@blue_3, 0.26), alpha(@blue_3, 0.04) 70%); }
.pc-ver-hero.alpha  { background: linear-gradient(135deg, alpha(@orange_3, 0.26), alpha(@orange_3, 0.04) 70%); }
.pc-ver-big    { font-size: 30pt; font-weight: 800; }
.pc-ver-kicker { font-size: 8pt; font-weight: 800; letter-spacing: 1.5px; opacity: 0.7; }
.pc-ver-notes  { font-size: 10.5pt; opacity: 0.9; }
.pc-ver-pill {
  border-radius: 999px; padding: 1px 9px;
  font-size: 8pt; font-weight: 800; letter-spacing: 0.5px; color: white;
}
.pc-ver-pill.stable { background: @green_4; }
.pc-ver-pill.rc     { background: @purple_3; }
.pc-ver-pill.beta   { background: @blue_3; }
.pc-ver-pill.alpha  { background: @orange_4; }
.pc-ver-chip {
  border-radius: 999px; padding: 2px 10px; font-size: 8.5pt;
  background: alpha(currentColor, 0.07);
}
.pc-ver-chip.signed { background: alpha(@success_color, 0.16); color: @success_color; }
.pc-ver-series { padding: 6px 6px 10px 6px; }
.pc-ver-series-head { padding: 10px 12px 6px 14px; }
.pc-ver-entry  { padding: 0 8px 0 10px; }
.pc-ver-rail   { min-width: 22px; }
.pc-ver-line.top { min-height: 15px; margin-top: 0; }
.pc-ver-line.none { background: none; }
.pc-ver-dot    { margin: 2px 0; }
.pc-ver-sep    { margin: 4px 0 2px 0; background: alpha(currentColor, 0.1); }
button.pc-ver-round { background: alpha(currentColor, 0.08); min-width: 34px; min-height: 34px; }
button.pc-ver-round:hover { background: alpha(currentColor, 0.14); }
button.pc-ver-danger { color: @error_color; background: alpha(@error_color, 0.1); }
button.pc-ver-danger:hover { background: alpha(@error_color, 0.18); }
.pc-ver-dot    { min-width: 12px; min-height: 12px; border-radius: 999px;
                 box-shadow: 0 0 0 3px alpha(currentColor, 0.06); }
.pc-ver-dot.stable { background: @green_4; }
.pc-ver-dot.rc     { background: @purple_3; }
.pc-ver-dot.beta   { background: @blue_3; }
.pc-ver-dot.alpha  { background: @orange_4; }
.pc-ver-line   { min-width: 2px; background: alpha(currentColor, 0.14); }
.pc-ver-head   { padding: 8px 10px; border-radius: 10px; }
.pc-ver-title  { font-weight: 700; font-size: 11.5pt; }
.pc-ver-chips-row { margin: 0 10px 10px 10px; }
.pc-ver-details { margin: 2px 10px 12px 10px; padding: 12px 14px; border-radius: 12px;
                  background: alpha(currentColor, 0.04); }
.pc-ver-empty  { padding: 28px; }
button.small-pill { padding: 4px 12px; min-height: 0; font-size: 9.5pt; }
"""
_versions_css_done = False


def _install_versions_css():
    global _versions_css_done
    if _versions_css_done:
        return
    from gi.repository import Gdk
    provider = Gtk.CssProvider()
    provider.load_from_string(_VERSIONS_CSS)
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _versions_css_done = True


def _pill(channel):
    return Gtk.Label(label=_(f"ver_ch_{channel}").upper()
                     if channel != "rc" else "RC",
                     valign=Gtk.Align.CENTER,
                     css_classes=["pc-ver-pill", channel])


def _chips(release, css=None):
    box = Gtk.Box(spacing=6, css_classes=css or [])
    for item in release["files"]:
        box.append(Gtk.Label(label=f'.{item["kind"]}  {human_size(item["size"])}',
                             css_classes=["pc-ver-chip"],
                             tooltip_text=item["name"]))
    if release["signed_by"]:
        chip = Gtk.Box(spacing=4, css_classes=["pc-ver-chip", "signed"])
        chip.append(Gtk.Image(icon_name="channel-secure-symbolic",
                              pixel_size=12))
        chip.append(Gtk.Label(label=_("ver_badge_signed")))
        chip.set_tooltip_text(release["signed_by"][-16:])
        box.append(chip)
    return box


def _stamp(when):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else ""


def _open_path(path):
    Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(),
                                       None)


def _group_series(releases):
    """[(series, [releases newest first])] — 1.6.2 belongs to 1.6."""
    groups = {}
    order = []
    for release in releases:
        series = ".".join(release["number"].split(".")[:2])
        if series not in groups:
            groups[series] = []
            order.append(series)
        groups[series].append(release)
    return [(s, groups[s]) for s in order]


class VersionsPage(Gtk.Box):
    """Every release, newest first, with the form for the next one below.

    The history is read from the project's versions folder each time the
    page is shown, so a release copied in by hand from a file manager
    appears as well. Removing a release moves it to a trash inside that
    folder, with an undo — a release is the one artefact that cannot be
    rebuilt identically later, because the code has moved on.
    """

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self._undo = []
        self._keys = []

        self.page = Adw.PreferencesPage(vexpand=True)
        self.append(self.page)

        self.history = Adw.PreferencesGroup(title=_("versions_page"),
                                            description=_("versions_hint"))
        self.library = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                               spacing=14)
        self.history.add(self.library)
        self.page.add(self.history)
        self._entries = []
        self._build_form()

        # Dropping existing packages onto the page brings an older release
        # into the history — the way to record what was shipped before
        # Petacore was keeping track.
        from gi.repository import Gdk
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.refresh()

    # -- the form -----------------------------------------------------------------
    def _build_form(self):
        project = config.active_project() or {}
        path = project.get("path", "")
        remembered = versions.defaults(path) if path else {}

        # what
        group = Adw.PreferencesGroup(title=_("ver_new"),
                                     description=_("ver_new_hint"))
        self.number_row = Adw.EntryRow(title=_("ver_number"))
        self.number_row.connect("changed", lambda *a: self._update_preview())
        group.add(self.number_row)

        self.channel_row = Adw.ComboRow(
            title=_("ver_channel"),
            model=Gtk.StringList.new([_(f"ver_ch_{c}")
                                      for c in CHANNEL_ORDER]))
        self.channel_row.connect("notify::selected",
                                 lambda *a: self._on_channel())
        group.add(self.channel_row)

        self.pre_row = Adw.SpinRow.new_with_range(1, 99, 1)
        self.pre_row.set_title(_("ver_pre"))
        self.pre_row.connect("notify::value", lambda *a: self._update_preview())
        group.add(self.pre_row)

        self.preview_row = Adw.ActionRow(css_classes=["property"])
        self.preview_row.add_prefix(Gtk.Image(icon_name="document-properties-symbolic"))
        group.add(self.preview_row)
        self.page.add(group)

        # which formats
        formats = Adw.PreferencesGroup(title=_("ver_formats"))
        wanted = (remembered.get("formats") or "deb").split(",")
        self.deb_row = Adw.SwitchRow(title=_("ver_fmt_deb"),
                                     active="deb" in wanted)
        formats.add(self.deb_row)
        have_rpm = shutil.which("rpmbuild") is not None
        self.rpm_row = Adw.SwitchRow(
            title=_("ver_fmt_rpm"), active=have_rpm and "rpm" in wanted,
            sensitive=have_rpm,
            subtitle="" if have_rpm else "rpmbuild — sudo apt install rpm")
        formats.add(self.rpm_row)
        self.page.add(formats)

        # what goes in the metadata
        details = Adw.PreferencesGroup(title=_("ver_details"))
        user = os.environ.get("USER", "developer")
        self.maint_row = Adw.EntryRow(
            title=_("maintainer"),
            text=remembered.get("maintainer") or f"{user} <{user}@localhost>")
        details.add(self.maint_row)
        self.desc_row = Adw.EntryRow(title=_("description_f"),
                                     text=remembered.get("description", ""))
        details.add(self.desc_row)

        from .debbuild import LICENSE_CHOICES
        self._license_ids = [spdx for spdx, _d in LICENSE_CHOICES]
        self.license_row = Adw.ComboRow(
            title=_("license_f"),
            model=Gtk.StringList.new([d for _s, d in LICENSE_CHOICES]))
        if remembered.get("license") in self._license_ids:
            self.license_row.set_selected(
                self._license_ids.index(remembered["license"]))
        details.add(self.license_row)
        self.page.add(details)

        # signing
        signing = Adw.PreferencesGroup(title=_("ver_signing"),
                                       description=_("ver_signing_hint"))
        self.sign_row = Adw.SwitchRow(
            title=_("sign_packages"),
            active=remembered.get("sign", config.get("sign_packages")))
        self.sign_row.connect("notify::active",
                              lambda *a: self._sync_signing())
        signing.add(self.sign_row)
        self.key_row = Adw.ComboRow(title=_("repo_key"))
        signing.add(self.key_row)
        self.new_key_row = Adw.ActionRow(title=_("create_key"),
                                         activatable=True)
        self.new_key_row.add_prefix(
            Gtk.Image(icon_name="channel-secure-symbolic"))
        self.new_key_row.connect("activated", lambda *a: self._create_key())
        signing.add(self.new_key_row)
        self.page.add(signing)
        self._load_keys(remembered.get("key") or config.get("gpg_key"))

        # notes
        notes = Adw.PreferencesGroup(title=_("ver_notes"),
                                     description=_("ver_notes_hint"))
        self.notes = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  top_margin=10, bottom_margin=10,
                                  left_margin=12, right_margin=12)
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(
            child=self.notes, min_content_height=110,
            hscrollbar_policy=Gtk.PolicyType.NEVER))
        notes.add(frame)
        self.page.add(notes)

        # and go
        finish = Adw.PreferencesGroup()
        from . import repo as _repo
        target = _repo.active_profile()
        self.repo_row = Adw.SwitchRow(
            title=_("repo_autoadd"),
            subtitle=target["name"] if target else _("repo_none_title"),
            active=bool(target) and config.get("repo_autoadd"),
            sensitive=bool(target))
        finish.add(self.repo_row)

        self.create_btn = Gtk.Button(label=_("ver_create"),
                                     halign=Gtk.Align.CENTER, margin_top=12,
                                     css_classes=["suggested-action", "pill"])
        self.create_btn.connect("clicked", lambda *a: self._create())
        finish.add(self.create_btn)
        self.status = Gtk.Label(wrap=True, margin_top=6,
                                css_classes=["dim-label", "caption"])
        finish.add(self.status)
        self.page.add(finish)

    def _load_keys(self, preferred=""):
        from . import gpgsign
        self._keys = gpgsign.list_keys_detailed()
        labels = [f'{k["uid"]}  ·  {k["fpr"][-16:]}' for k in self._keys] \
            or [_("no_key")]
        self.key_row.set_model(Gtk.StringList.new(labels))
        for index, key in enumerate(self._keys):
            if key["fpr"] == preferred:
                self.key_row.set_selected(index)
        self._sync_signing()

    def _sync_signing(self):
        signing = self.sign_row.get_active()
        self.key_row.set_visible(signing)
        self.key_row.set_sensitive(bool(self._keys))
        self.new_key_row.set_visible(signing and not self._keys)

    def _create_key(self):
        from .dialogs import GpgKeyDialog
        user = os.environ.get("USER", "developer")
        GpgKeyDialog(self.window, default_name=user,
                     default_email=f"{user}@localhost",
                     on_done=lambda *a: self._load_keys()).present()

    def _channel(self):
        return CHANNEL_ORDER[self.channel_row.get_selected()]

    def _on_channel(self):
        channel = self._channel()
        self.pre_row.set_visible(channel != "stable")
        project = config.active_project()
        if project and channel != "stable":
            try:
                number = versions.clean_number(self.number_row.get_text())
                self.pre_row.set_value(
                    versions.suggest_pre(project["path"], number, channel))
            except versions.VersionError:
                pass
        self._update_preview()

    def _update_preview(self):
        channel = self._channel()
        pre = int(self.pre_row.get_value())
        try:
            number = versions.clean_number(self.number_row.get_text())
        except versions.VersionError as e:
            self.preview_row.set_title(str(e))
            self.preview_row.set_subtitle("")
            self.create_btn.set_sensitive(False)
            return
        self.preview_row.set_title(versions.label(number, channel, pre))
        self.preview_row.set_subtitle(_(
            "ver_preview", l=versions.label(number, channel, pre),
            p=versions.package_version(number, channel, pre)))
        self.create_btn.set_sensitive(True)

    # -- the history ----------------------------------------------------------------
    # Laid out the way people think about releases rather than as a flat log:
    # the release users actually run comes first and largest; anything newer
    # still in the works sits apart from it; everything else is grouped by
    # series (1.6 with its alphas, betas and candidates) on a timeline, since
    # "1.6 Beta 2" only means something next to the 1.6 it led to.
    def refresh(self):
        _install_versions_css()
        child = self.library.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.library.remove(child)
            child = following
        self._entries = []

        project = config.active_project()
        if not project:
            return
        releases = versions.list_versions(project["path"])

        if not releases:
            self.library.append(self._empty_state())
        else:
            stable = [r for r in releases if r["channel"] == "stable"]
            latest = stable[0] if stable else releases[0]
            newer = [r for r in releases
                     if versions.sort_key(r["number"], r["channel"], r["pre"])
                     > versions.sort_key(latest["number"], latest["channel"],
                                         latest["pre"])]

            self.library.append(Gtk.Label(
                label=_("ver_summary", n=len(releases), s=len(stable),
                        t=_ago(releases[0]["created"])
                        if releases[0]["created"] else "—"),
                xalign=0, css_classes=["dim-label", "caption"]))
            self.library.append(self._hero(latest,
                                           newer[0] if newer else None))
            for series, members in _group_series(releases):
                self.library.append(self._series_card(series, members))

        # The form follows the history: the next number, and the next beta.
        if not self.number_row.get_text().strip() or getattr(
                self, "_just_created", False):
            self.number_row.set_text(versions.suggest_next(project["path"]))
            self._just_created = False
        self._on_channel()

    def _empty_state(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      css_classes=["card", "pc-ver-empty"])
        box.append(Gtk.Image(icon_name="document-open-recent-symbolic",
                             pixel_size=40, css_classes=["dim-label"]))
        box.append(Gtk.Label(label=_("versions_empty"),
                             css_classes=["title-4"]))
        box.append(Gtk.Label(label=_("versions_empty_body"), wrap=True,
                             justify=Gtk.Justification.CENTER,
                             css_classes=["dim-label"]))
        return box

    # -- the release people run -----------------------------------------------------
    def _hero(self, release, upcoming=None):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       css_classes=["pc-ver-hero", release["channel"]])

        kicker = Gtk.Box(spacing=8)
        kicker.append(Gtk.Label(label=_("ver_latest"),
                                css_classes=["pc-ver-kicker"]))
        kicker.append(_pill(release["channel"]))
        kicker.append(Gtk.Label(label=_ago(release["created"]), xalign=1,
                                hexpand=True, css_classes=["dim-label"],
                                tooltip_text=_stamp(release["created"])))
        card.append(kicker)

        card.append(Gtk.Label(label=release["label"], xalign=0,
                              css_classes=["pc-ver-big"]))

        if release["notes"]:
            card.append(Gtk.Label(label=release["notes"], xalign=0,
                                  wrap=True, lines=3,
                                  ellipsize=Pango.EllipsizeMode.END,
                                  css_classes=["pc-ver-notes"]))

        bottom = Gtk.Box(spacing=6)
        bottom.append(_chips(release))
        spacer = Gtk.Box(hexpand=True)
        bottom.append(spacer)
        bottom.append(self._icon_button("folder-open-symbolic",
                                        _("repo_open_folder"),
                                        lambda: _open_path(release["path"])))
        debs = [f["path"] for f in release["files"] if f["kind"] == "deb"]
        if debs:
            bottom.append(self._icon_button(
                "system-software-install-symbolic", _("ver_to_repo"),
                lambda: self._to_repo(debs[0])))
        card.append(bottom)

        # Something newer than what users run — a beta of the next version —
        # is worth a line here, beside the release it will replace, rather
        # than a box of its own repeating what the timeline below shows.
        if upcoming:
            card.append(Gtk.Separator(css_classes=["pc-ver-sep"]))
            line = Gtk.Box(spacing=8)
            line.append(Gtk.Image(icon_name="applications-engineering-symbolic",
                                  css_classes=["dim-label"]))
            line.append(Gtk.Label(label=_("ver_in_dev"),
                                  css_classes=["dim-label"]))
            line.append(Gtk.Label(label=upcoming["label"],
                                  css_classes=["heading"]))
            line.append(_pill(upcoming["channel"]))
            line.append(Gtk.Label(label=_ago(upcoming["created"]),
                                  hexpand=True, xalign=1,
                                  css_classes=["dim-label", "caption"],
                                  tooltip_text=_stamp(upcoming["created"])))
            card.append(line)
        return card

    # -- a series, as a timeline ------------------------------------------------------
    def _series_card(self, series, members):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       css_classes=["card", "pc-ver-series"])

        head = Gtk.Box(spacing=10, css_classes=["pc-ver-series-head"])
        head.append(Gtk.Label(label=_("ver_series", s=series),
                              css_classes=["title-4"]))
        shipped = any(m["channel"] == "stable" for m in members)
        count = _("ver_series_one") if len(members) == 1 \
            else _("ver_series_count", n=len(members))
        head.append(Gtk.Label(
            label=count if shipped
            else f'{count}  ·  {_("ver_series_open")}',
            hexpand=True, xalign=1, css_classes=["dim-label", "caption"]))
        card.append(head)

        for index, release in enumerate(members):
            card.append(self._timeline_entry(
                release, first=index == 0,
                last=index == len(members) - 1))
        return card

    def _timeline_entry(self, release, first, last):
        row = Gtk.Box(spacing=0, css_classes=["pc-ver-entry"])

        # The rail: a segment from the entry above, the dot in the channel's
        # colour, and a segment down to the next. Drawn as three pieces so
        # the line runs unbroken through every entry of the series.
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       css_classes=["pc-ver-rail"])
        rail.append(Gtk.Box(halign=Gtk.Align.CENTER,
                            css_classes=["pc-ver-line", "top"]
                            + ([] if not first else ["none"])))
        rail.append(Gtk.Box(css_classes=["pc-ver-dot", release["channel"]],
                            halign=Gtk.Align.CENTER))
        rail.append(Gtk.Box(vexpand=True, halign=Gtk.Align.CENTER,
                            css_classes=["pc-ver-line"]
                            + ([] if not last else ["none"])))
        row.append(rail)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        header = Gtk.Button(css_classes=["flat", "pc-ver-head"])
        head_box = Gtk.Box(spacing=8)
        head_box.append(Gtk.Label(label=release["label"],
                                  css_classes=["pc-ver-title"]))
        if release["channel"] != "stable":
            head_box.append(_pill(release["channel"]))
        if release["source"] == "imported":
            head_box.append(Gtk.Label(label=_("ver_badge_imported"),
                                      css_classes=["pc-ver-chip"]))
        head_box.append(Gtk.Label(label=_ago(release["created"]),
                                  hexpand=True, xalign=1,
                                  css_classes=["dim-label", "caption"],
                                  tooltip_text=_stamp(release["created"])))
        chevron = Gtk.Image(icon_name="pan-down-symbolic",
                            css_classes=["dim-label"])
        head_box.append(chevron)
        header.set_child(head_box)
        body.append(header)

        body.append(_chips(release, css=["pc-ver-chips-row"]))

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          css_classes=["pc-ver-details"])
        details.append(Gtk.Label(label=release["notes"] or _("ver_no_notes"),
                                 xalign=0, wrap=True, selectable=True,
                                 css_classes=[] if release["notes"]
                                 else ["dim-label"]))
        actions = Gtk.Box(spacing=6)
        actions.append(self._text_button("folder-open-symbolic",
                                         _("repo_open_folder"),
                                         lambda: _open_path(release["path"])))
        debs = [f["path"] for f in release["files"] if f["kind"] == "deb"]
        if debs:
            actions.append(self._text_button(
                "system-software-install-symbolic", _("ver_to_repo"),
                lambda: self._to_repo(debs[0])))
        actions.append(self._text_button("document-edit-symbolic",
                                         _("ver_edit_notes"),
                                         lambda: self._edit_notes(release)))
        actions.append(Gtk.Box(hexpand=True))
        remove = self._text_button("user-trash-symbolic", _("ver_remove"),
                                   lambda: self._remove(release))
        remove.add_css_class("pc-ver-danger")
        actions.append(remove)
        details.append(actions)

        revealer = Gtk.Revealer(
            child=details,
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=220)
        body.append(revealer)

        def toggle(*_a):
            opening = not revealer.get_reveal_child()
            revealer.set_reveal_child(opening)
            chevron.set_from_icon_name("pan-up-symbolic" if opening
                                       else "pan-down-symbolic")
        header.connect("clicked", toggle)
        self._entries.append(toggle)

        row.append(body)
        return row

    def _icon_button(self, icon, tooltip, callback):
        button = Gtk.Button(icon_name=icon, tooltip_text=tooltip,
                            valign=Gtk.Align.CENTER,
                            css_classes=["circular", "pc-ver-round"])
        button.connect("clicked", lambda *a: callback())
        return button

    def _text_button(self, icon, label, callback):
        button = Gtk.Button(css_classes=["pill", "small-pill"])
        button.set_child(Adw.ButtonContent(icon_name=icon, label=label))
        button.connect("clicked", lambda *a: callback())
        return button

    # -- creating -----------------------------------------------------------------
    def _create(self):
        project = config.active_project()
        if not project:
            return
        formats = [f for f, on in (("deb", self.deb_row.get_active()),
                                   ("rpm", self.rpm_row.get_active())) if on]
        channel = self._channel()
        pre = int(self.pre_row.get_value()) if channel != "stable" else 0
        try:
            number = versions.clean_number(self.number_row.get_text())
        except versions.VersionError as e:
            self.window.toast(str(e))
            return

        sign = self.sign_row.get_active()
        key_fpr = ""
        if sign and self._keys:
            key_fpr = self._keys[min(self.key_row.get_selected(),
                                     len(self._keys) - 1)]["fpr"]
        buffer = self.notes.get_buffer()
        notes = buffer.get_text(buffer.get_start_iter(),
                                buffer.get_end_iter(), False)
        shown = versions.label(number, channel, pre)
        add_to_repo = self.repo_row.get_active()

        self.create_btn.set_sensitive(False)
        self.status.set_text(_("ver_creating", l=shown))
        self.window.begin_operation(_("ver_creating", l=shown))

        def work():
            release_dir = versions.create(
                project["path"], project["name"], number, channel, pre,
                formats, self.maint_row.get_text(), self.desc_row.get_text(),
                self._license_ids[self.license_row.get_selected()],
                debbuild.repo_homepage(project.get("repo_url")),
                notes, sign, key_fpr)
            added = []
            if add_to_repo:
                from . import repo
                profile = repo.active_profile()
                debs = [os.path.join(release_dir, n)
                        for n in os.listdir(release_dir) if n.endswith(".deb")]
                if profile and debs:
                    added, _f = repo.add_packages(profile, debs)
            return added

        def done(added, error):
            self.window.end_operation()
            self.create_btn.set_sensitive(True)
            if error:
                self.status.set_text(_("ver_failed", e=error))
                return
            self.status.set_text("")
            self.window.toast(_("ver_created", l=shown))
            if added:
                self.window.toast(_("repo_added", n=len(added)))
                page = self.window.pages.get("repo_page")
                if page:
                    page.refresh()
            buffer.set_text("")
            self._just_created = True
            self.refresh()
            left_out = debbuild.find_secrets(project["path"])
            if left_out:
                self.window.toast(_("secrets_left_out", n=len(left_out)))

        run_async(work, done)

    def _to_repo(self, deb_path):
        from . import repo
        profile = repo.active_profile()
        if not profile:
            self.window.toast(_("repo_none_title"))
            return
        run_async(lambda: repo.add_packages(profile, [deb_path]),
                  lambda r, e: self.window.toast(
                      str(e) if e else _("repo_added_to", n=profile["name"])))

    # -- notes and removal ------------------------------------------------------------
    def _edit_notes(self, release):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=release["label"])
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            top_margin=8, bottom_margin=8,
                            left_margin=8, right_margin=8)
        view.get_buffer().set_text(release["notes"])
        dialog.set_extra_child(Gtk.Frame(child=Gtk.ScrolledWindow(
            child=view, min_content_height=160, min_content_width=380)))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("save", _("save"))
        dialog.set_response_appearance("save",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_d, response):
            if response != "save":
                return
            buffer = view.get_buffer()
            versions.set_notes(release["path"], buffer.get_text(
                buffer.get_start_iter(), buffer.get_end_iter(), False))
            self.refresh()

        dialog.connect("response", responded)
        dialog.present()

    def _remove(self, release):
        project = config.active_project()
        if not project:
            return
        try:
            trashed = versions.remove(project["path"], release["path"])
        except (versions.VersionError, OSError) as e:
            self.window.toast(str(e))
            return
        self._undo.append((trashed, release["label"]))
        toast = Adw.Toast(title=_("ver_removed", l=release["label"]),
                          timeout=6)
        toast.set_button_label(_("repo_undo"))
        toast.connect("button-clicked", lambda *a: self._undo_remove())
        self.window.toaster.add_toast(toast)
        self.refresh()

    def _undo_remove(self):
        project = config.active_project()
        if not project or not self._undo:
            return
        trashed, shown = self._undo.pop()
        try:
            versions.restore(project["path"], trashed)
        except (versions.VersionError, OSError) as e:
            self.window.toast(str(e))
            return
        self.window.toast(_("ver_restored", l=shown))
        self.refresh()

    # -- importing an older release -------------------------------------------------
    def _on_drop(self, _target, value, _x, _y):
        try:
            files = value.get_files()
        except Exception:  # noqa: BLE001
            return False
        paths = [f.get_path() for f in files
                 if f.get_path()
                 and f.get_path().endswith(versions.PACKAGE_SUFFIXES)]
        if not paths:
            return False
        self._ask_import(paths)
        return True

    def _ask_import(self, paths):
        project = config.active_project()
        if not project:
            return
        dialog = Adw.MessageDialog(
            transient_for=self.window, heading=_("ver_import_q"),
            body="\n".join(os.path.basename(p) for p in paths[:6])
            + f'\n\n{_("ver_import_hint")}')

        box = Gtk.Box(spacing=8, margin_top=8)
        number = Gtk.Entry(placeholder_text="1.0", hexpand=True,
                           text=_guess_number(paths[0]))
        box.append(number)
        channel = Gtk.DropDown.new_from_strings(
            [_(f"ver_ch_{c}") for c in CHANNEL_ORDER])
        box.append(channel)
        pre = Gtk.SpinButton.new_with_range(1, 99, 1)
        box.append(pre)
        dialog.set_extra_child(box)

        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("add", _("ver_import_go"))
        dialog.set_response_appearance("add",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_d, response):
            if response != "add":
                return
            chosen = CHANNEL_ORDER[channel.get_selected()]
            pre_value = int(pre.get_value()) if chosen != "stable" else 0

            def work():
                versions.import_files(project["path"], number.get_text(),
                                      chosen, pre_value, paths)
                return versions.label(
                    versions.clean_number(number.get_text()),
                    chosen, pre_value)

            run_async(work, lambda shown, e: (
                self.window.toast(str(e) if e
                                  else _("ver_imported", l=shown)),
                self.refresh()))

        dialog.connect("response", responded)
        dialog.present()


def _guess_number(path):
    """"myapp_1.4.2_amd64.deb" -> "1.4.2", to save typing on import."""
    match = re.search(r"[_-](\d+(?:\.\d+){0,3})", os.path.basename(path))
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# Keys — manage GPG signing keys
# --------------------------------------------------------------------------- #
class KeysPage(Gtk.Box):
    """Every GPG signing key with its details. The active key (checkmark)
    signs all exported packages so users can verify who they came from."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         margin_top=14, margin_bottom=14,
                         margin_start=16, margin_end=16)
        self.window = window

        hint = Gtk.Label(label=_("keys_hint"), wrap=True, xalign=0,
                         css_classes=["dim-label"])
        self.append(hint)

        bar = Gtk.Box(spacing=6)
        create = Gtk.Button(css_classes=["suggested-action"])
        create.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                           label=_("create_key")))
        create.connect("clicked", self._on_create)
        bar.append(create)

        from .dialogs import brand_image
        sync_keys = Gtk.Button(tooltip_text=_("sync_keys_only"))
        sync_keys.set_child(Adw.ButtonContent(
            icon_name="folder-remote-symbolic", label=_("sync_keys_only")))
        sync_keys.connect("clicked", self._on_sync_keys)
        bar.append(sync_keys)
        self._sync_keys_btn = sync_keys
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text=_("refresh"), css_classes=["flat"])
        refresh.connect("clicked", lambda *a: self.refresh())
        bar.append(refresh)
        self.append(bar)

        self.listbox = Gtk.ListBox(css_classes=["boxed-list"],
                                   selection_mode=Gtk.SelectionMode.NONE,
                                   valign=Gtk.Align.START)
        self.append(_scrolled(self.listbox))
        self.refresh()

    def refresh(self):
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        run_async(gpgsign.list_keys_detailed, self._render)

    def _render(self, keys, error):
        if error or not keys:
            self.listbox.append(Adw.ActionRow(title=_("no_key")))
            return
        active = gpgsign.signing_key(config.get("gpg_key"))
        active_fpr = active[0] if active else ""

        for k in keys:
            created = time.strftime("%Y-%m-%d",
                                    time.localtime(k["created"])) \
                if k["created"] else "\u2014"
            expires = time.strftime("%Y-%m-%d",
                                    time.localtime(k["expires"])) \
                if k["expires"] else _("exp_never")
            fpr_short = " ".join(k["fpr"][i:i+4]
                                 for i in range(0, 16, 4)) + "\u2026"
            algo = k["algo"] if k["algo"] == "Ed25519" \
                else f'{k["algo"]} {k["bits"]}'
            subtitle = (f'{algo}  \u00b7  {_("created_on")}: {created}  \u00b7  '
                        f'{_("expires_on")}: {expires}\n'
                        f'{_("fingerprint")}: {fpr_short}')

            row = Adw.ActionRow(title=k["uid"], subtitle=subtitle,
                                subtitle_lines=2)
            is_active = k["fpr"] == active_fpr
            row.add_prefix(Gtk.Image(
                icon_name="emblem-default-symbolic" if is_active
                else "channel-secure-symbolic",
                css_classes=["success"] if is_active else ["dim-label"]))
            if is_active:
                badge = Gtk.Label(label=_("in_use"),
                                  css_classes=["caption", "success"],
                                  valign=Gtk.Align.CENTER)
                row.add_suffix(badge)
            else:
                use = Gtk.Button(label=_("use_key"),
                                 valign=Gtk.Align.CENTER)
                use.connect("clicked", self._on_use, k["fpr"])
                row.add_suffix(use)

            export = Gtk.Button(icon_name="send-to-symbolic",
                                valign=Gtk.Align.CENTER,
                                css_classes=["flat"],
                                tooltip_text=_("export_pubkey"))
            export.connect("clicked", self._on_export, k["fpr"])
            row.add_suffix(export)

            delete = Gtk.Button(icon_name="user-trash-symbolic",
                                valign=Gtk.Align.CENTER,
                                css_classes=["flat"],
                                tooltip_text=_("delete"))
            delete.connect("clicked", self._on_delete, k)
            row.add_suffix(delete)

            self.listbox.append(row)

    def _on_sync_keys(self, _btn):
        """Upload just the public keys — project files are left alone."""
        from . import gdrive
        project = config.active_project()
        if not project:
            return
        if not (gdrive.available() and gdrive.connected()):
            self.window.toast(_("not_logged_in"))
            return
        self._sync_keys_btn.set_sensitive(False)
        self.window.begin_operation(_("op_keys"))

        def done(count, error):
            self.window.end_operation()
            self._sync_keys_btn.set_sensitive(True)
            self.window.toast(f"{_('drive_failed')}: {error}" if error
                              else _("sync_keys_done", n=count or 0))

        run_async(lambda: gdrive.push_public_keys(project["name"]), done)

    def _on_create(self, _btn):
        from .dialogs import GpgKeyDialog
        user = os.environ.get("USER", "developer")

        def created():
            self.window.toast(_("key_created"))
            self.refresh()

        GpgKeyDialog(self.window, default_name=user,
                     default_email=f"{user}@localhost",
                     on_done=created).present()

    def _on_use(self, _btn, fpr):
        config.set("gpg_key", fpr)
        self.refresh()

    def _on_export(self, _btn, fpr):
        fd = Gtk.FileDialog(title=_("export_pubkey"),
                            initial_name="public-key.asc")
        fd.save(self.window, None, self._export_dest, fpr)

    def _export_dest(self, dialog, result, fpr):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        if not gfile:
            return
        dest = gfile.get_path()
        run_async(lambda: gpgsign.export_public(fpr, dest),
                  lambda p, e: self.window.toast(
                      str(e) if e else _("pubkey_done", p=p)))

    def _on_delete(self, _btn, key):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("delete_key_q"),
                                   body=f'{key["uid"]}\n\n'
                                        f'{_("delete_key_detail")}')
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("delete", _("delete"))
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_d, response):
            if response != "delete":
                return
            run_async(lambda: gpgsign.delete_key(key["fpr"]),
                      lambda r, e: (self.window.toast(
                          str(e) if e else _("key_deleted")),
                          self.refresh()))

        dialog.connect("response", responded)
        dialog.present()

# --------------------------------------------------------------------------- #
# Repository — the APT archive: the pool, the index, and the copy on the server
# --------------------------------------------------------------------------- #
from . import repo  # noqa: E402

REPO_FILTERS = [("all", "repo_filter_all"),
                ("unindexed", "repo_filter_unindexed"),
                ("unsigned", "repo_filter_unsigned"),
                ("unpublished", "repo_filter_unpublished"),
                ("old", "repo_filter_old")]


def _ago(when: float) -> str:
    """A timestamp in the words a person would use."""
    if not when:
        return ""
    seconds = max(0, time.time() - when)
    if seconds < 90:
        return _("repo_just_now")
    if seconds < 3600:
        return _("repo_minutes_ago", n=int(seconds // 60))
    if seconds < 86400:
        return _("repo_hours_ago", n=int(seconds // 3600))
    return _("repo_days_ago", n=int(seconds // 86400))


class RepoPage(Gtk.Box):
    """Everything a personal APT archive needs, without a terminal.

    The page shows the pool as a list, because that is what the repository
    actually is. Three states are kept apart and shown separately, since
    confusing them is how an archive quietly stops working:

      * on disk   — the file is in the pool
      * indexed   — the signed index mentions it, so apt can see it
      * live      — the server is really serving it right now

    A package can be in the first and not the second (added but not rebuilt),
    or in the second and not the third (rebuilt but not published). Both are
    invisible in a file manager, which is why they are labelled here.
    """

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=12, margin_bottom=12,
                         margin_start=14, margin_end=14)
        self.window = window
        self._entries = []
        self._live = None          # {package: version} once the server is read
        self._live_error = ""
        self._undo = []            # removal records, newest last
        self._busy = False
        self._build()

    def stop(self):
        """Closing Petacore or switching project ends the server session."""
        panel = getattr(self, "server_panel", None)
        if panel is not None:
            panel.stop()

    # -- construction ---------------------------------------------------------
    def _clear(self):
        child = self.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.remove(child)
            child = following

    def _build(self):
        self._clear()
        profile = repo.active_profile()
        if not profile:
            self._build_empty()
            return
        self.profile = profile

        # Two views of the same repository: the tree on this machine, and
        # the folder on the server. The switch keeps its place across
        # rebuilds, so saving settings does not throw you out of a session.
        self.views = Gtk.Stack(
            vexpand=True,
            transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
            transition_duration=220)
        self.local = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.views.add_titled(self.local, "local", _("repo_local"))
        previous = getattr(self, "server_panel", None)
        if previous is not None:
            previous.stop()
        self.server_panel = RemotePanel(self.window, self)
        self.views.add_titled(self.server_panel, "server", _("repo_server"))
        switcher = Gtk.StackSwitcher(stack=self.views,
                                     halign=Gtk.Align.CENTER)
        self.append(switcher)
        self.append(self.views)

        self._build_toolbar()
        self._build_banner()
        self._build_list()
        self.views.set_visible_child_name(getattr(self, "_view", "local"))
        self.views.connect("notify::visible-child-name", lambda s, _p: setattr(
            self, "_view", s.get_visible_child_name()))
        self.refresh()

    def _build_empty(self):
        status = Adw.StatusPage(icon_name="package-x-generic-symbolic",
                                title=_("repo_none_title"),
                                description=_("repo_none_body"),
                                vexpand=True)
        button = Gtk.Button(label=_("repo_new"), halign=Gtk.Align.CENTER,
                            css_classes=["suggested-action", "pill"])
        button.connect("clicked", lambda *a: self._edit_profile(None))
        status.set_child(button)
        self.append(status)

    def _build_toolbar(self):
        bar = Gtk.Box(spacing=6)

        names = [p["name"] for p in repo.profiles()]
        self.repo_drop = Gtk.DropDown.new_from_strings(names)
        if self.profile["name"] in names:
            self.repo_drop.set_selected(names.index(self.profile["name"]))
        self.repo_drop.connect("notify::selected", self._on_repo_chosen, names)
        bar.append(self.repo_drop)

        settings = Gtk.Button(icon_name="emblem-system-symbolic",
                              tooltip_text=_("repo_settings"),
                              css_classes=["flat"])
        settings.connect("clicked",
                         lambda *a: self._edit_profile(self.profile))
        bar.append(settings)

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)

        add = Gtk.Button(tooltip_text=_("repo_add"))
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                        label=_("repo_add")))
        add.connect("clicked", self._on_add_clicked)
        bar.append(add)

        self.build_btn = Gtk.Button(css_classes=["suggested-action"])
        self.build_btn.set_child(Adw.ButtonContent(
            icon_name="view-refresh-symbolic", label=_("repo_rebuild")))
        self.build_btn.connect("clicked", lambda *a: self._on_rebuild())
        bar.append(self.build_btn)

        self.publish_btn = Gtk.Button(tooltip_text=_("repo_publish"))
        self.publish_btn.set_child(Adw.ButtonContent(
            icon_name="send-to-symbolic", label=_("repo_publish")))
        self.publish_btn.connect("clicked", lambda *a: self._on_publish())
        bar.append(self.publish_btn)

        self.live_btn = Gtk.Button(icon_name="network-transmit-receive-symbolic",
                                   tooltip_text=_("repo_check_live"),
                                   css_classes=["flat"])
        self.live_btn.connect("clicked", lambda *a: self._on_check_live())
        bar.append(self.live_btn)

        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic",
                                  css_classes=["flat"])
        menu_btn.set_popover(self._page_menu())
        bar.append(menu_btn)
        self.local.append(bar)

        # One line of plain facts: how much is here, how old the index is,
        # whether it is signed, and what the server is actually serving.
        self.summary = Gtk.Label(xalign=0, wrap=True,
                                 css_classes=["dim-label", "caption"])
        self.local.append(self.summary)

    def _page_menu(self):
        popover = Gtk.Popover(has_arrow=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)

        def item(label, callback, destructive=False):
            classes = ["flat"] + (["destructive-action"] if destructive else [])
            button = Gtk.Button(label=label, css_classes=classes)
            button.get_child().set_xalign(0)
            button.connect("clicked",
                           lambda *a: (popover.popdown(), callback()))
            box.append(button)
            return button

        item(_("repo_instructions"), self._copy_instructions)
        item(_("repo_export_keyring"), self._export_keyring)
        item(_("repo_open_root"), self._open_root)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item(_("repo_prune"), self._on_prune)
        self.trash_item = item(_("repo_empty_trash"), self._on_empty_trash,
                               destructive=True)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item(_("repo_new"), lambda: self._edit_profile(None))
        item(_("repo_forget"), self._on_forget, destructive=True)

        popover.set_child(box)
        return popover

    def _build_banner(self):
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda *a: self._on_rebuild())
        self.local.append(self.banner)

    def _build_list(self):
        search_row = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("repo_search"),
                                      hexpand=True)
        self.search.connect("search-changed", lambda *a: self._render())
        search_row.append(self.search)

        self.filter_drop = Gtk.DropDown.new_from_strings(
            [_(key) for _c, key in REPO_FILTERS])
        self.filter_drop.connect("notify::selected", lambda *a: self._render())
        search_row.append(self.filter_drop)

        self.undo_btn = Gtk.Button(icon_name="edit-undo-symbolic",
                                   tooltip_text=_("repo_undo") + " (Ctrl+Z)",
                                   css_classes=["flat"], sensitive=False)
        self.undo_btn.connect("clicked", lambda *a: self.undo_last())
        search_row.append(self.undo_btn)
        self.local.append(search_row)

        self.listbox = Gtk.ListBox(css_classes=["boxed-list"],
                                   selection_mode=Gtk.SelectionMode.MULTIPLE,
                                   valign=Gtk.Align.START)
        self.listbox.set_activate_on_single_click(False)
        self.listbox.connect("row-activated", self._on_row_activated)

        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        wrap.append(self.listbox)
        self.local.append(_scrolled(wrap))

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.listbox.add_controller(keys)

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click)
        self.listbox.add_controller(click)
        self.row_menu = Gtk.Popover(has_arrow=True)
        self.row_menu.set_parent(wrap)

        # Dropping .deb files anywhere on the page adds them to the pool.
        # This is the shortest path from "I just built something" to "it is
        # in the archive", and it is the same gesture the Project page uses.
        from gi.repository import Gdk
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.local.add_controller(drop)

    # -- reading the repository ------------------------------------------------
    def refresh(self):
        if not getattr(self, "profile", None):
            return
        profile = self.profile

        def work():
            return repo.scan(profile), repo.status(profile)

        def done(result, error):
            if error or not result:
                self._entries, self._status = [], {}
                self._render()
                return
            self._entries, self._status = result
            self._render()
            self._render_summary()

        run_async(work, done)

    def _render_summary(self):
        state = getattr(self, "_status", {}) or {}
        parts = [_("repo_count", n=state.get("packages", 0)),
                 human_size(state.get("size", 0))]

        built = state.get("built", 0)
        parts.append(_("repo_index_built", t=_ago(built)) if built
                     else _("repo_index_never"))
        parts.append(_("repo_index_signed") if state.get("signed_index")
                     else _("repo_index_unsigned"))

        if self._live_error:
            parts.append(_("repo_live_failed", e=self._live_error))
        elif self._live is not None:
            parts.append(_("repo_live_count", n=len(self._live)))
        else:
            parts.append(_("repo_live_unknown"))

        self.summary.set_text("  ·  ".join(p for p in parts if p))

        if hasattr(self, "trash_item"):
            self.trash_item.set_label(
                f'{_("repo_empty_trash")}  ({state.get("trash", 0)})'
                if state.get("trash") else _("repo_empty_trash"))

        # The banner carries one problem at a time, in the order that stops
        # the archive working: no tool, no key, then an index left behind.
        if not repo.can_index():
            self._banner(_("repo_no_index_tool"), "")
        elif state.get("key") and not state.get("key_present"):
            self._banner(_("repo_key_missing"), "")
        elif state.get("stale"):
            self._banner(f'{_("repo_stale")} — {_("repo_stale_body")}',
                         _("repo_rebuild"))
        else:
            self.banner.set_revealed(False)

    def _banner(self, title, button_label):
        self.banner.set_title(title)
        self.banner.set_button_label(button_label or "")
        self.banner.set_revealed(True)

    # -- the list --------------------------------------------------------------
    def _live_state(self, entry):
        return repo.live_state(entry, self._live)

    def _visible_entries(self):
        mode = REPO_FILTERS[self.filter_drop.get_selected()][0]
        return repo.filter_entries(self._entries, self.search.get_text(),
                                   mode, self._live)

    def _render(self):
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)

        shown = self._visible_entries()
        if not shown:
            empty = Adw.ActionRow(title=_("repo_no_packages"),
                                  subtitle=_("repo_drop_hint"),
                                  subtitle_lines=2)
            empty.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic"))
            self.listbox.append(empty)
            return

        for entry in shown:
            self.listbox.append(self._row(entry))

    def _row(self, entry):
        title = f'{entry["package"]}  {entry["version"]}'.strip()
        details = [entry["arch"] or "—", human_size(entry["size"])]
        if entry["summary"]:
            details.append(entry["summary"])
        row = Adw.ActionRow(title=title, subtitle="  ·  ".join(details) + "\n"
                            + entry["rel"], subtitle_lines=2)
        row._entry = entry
        row.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic"))

        for label, css in self._badges(entry):
            row.add_suffix(Gtk.Label(label=label, valign=Gtk.Align.CENTER,
                                     css_classes=["caption"] + css))

        menu = Gtk.MenuButton(icon_name="view-more-symbolic",
                              valign=Gtk.Align.CENTER,
                              css_classes=["flat"])
        menu.set_popover(self._row_popover([entry]))
        row.add_suffix(menu)

        self._attach_drag(row, entry)
        return row

    def _badges(self, entry):
        badges = []
        if entry["broken"]:
            return [(_("repo_badge_broken"), ["error"])]
        if not entry["indexed"]:
            badges.append((_("repo_badge_unindexed"), ["warning"]))
        if not entry["signed"]:
            badges.append((_("repo_badge_unsigned"), ["dim-label"]))
        state = self._live_state(entry)
        if state == "live":
            badges.append((_("repo_badge_live"), ["success"]))
        elif state == "new":
            badges.append((_("repo_badge_new"), ["accent"]))
        elif state == "ahead":
            badges.append((_("repo_badge_ahead"), ["accent"]))
        elif state == "behind":
            badges.append((_("repo_badge_behind"), ["warning"]))
        return badges

    def _attach_drag(self, row, entry):
        """Let a package be dragged out to a file manager.

        Dragging in adds; dragging out takes a copy. Neither changes the
        archive behind the user's back — the file is copied, never moved.
        """
        from gi.repository import Gdk
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)

        def prepare(_source, _x, _y):
            try:
                return Gdk.ContentProvider.new_for_value(
                    Gio.File.new_for_path(entry["path"]))
            except Exception:
                return None

        source.connect("prepare", prepare)
        row.add_controller(source)

    # -- selection helpers -----------------------------------------------------
    def _selected(self):
        chosen = [getattr(r, "_entry", None)
                  for r in self.listbox.get_selected_rows()]
        return [e for e in chosen if e]

    def _on_row_activated(self, _listbox, row):
        entry = getattr(row, "_entry", None)
        if entry:
            self._show_details(entry)

    def _on_key(self, _controller, keyval, _code, state):
        from gi.repository import Gdk
        name = (Gdk.keyval_name(keyval) or "").lower()
        if name == "delete":
            self._on_remove(self._selected())
            return True
        if name == "f2":
            chosen = self._selected()
            if len(chosen) == 1:
                self._rename(chosen[0])
            return True
        if name == "z" and (state & Gdk.ModifierType.CONTROL_MASK):
            self.undo_last()
            return True
        return False

    def _on_right_click(self, _gesture, _n, x, y):
        from gi.repository import Gdk
        row = self.listbox.get_row_at_y(int(y))
        if row is None or not getattr(row, "_entry", None):
            return
        if row not in self.listbox.get_selected_rows():
            self.listbox.unselect_all()
            self.listbox.select_row(row)
        chosen = self._selected() or [row._entry]
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.row_menu.set_pointing_to(rect)
        self.row_menu.set_child(self._menu_box(chosen, self.row_menu))
        self.row_menu.popup()

    def _row_popover(self, entries):
        popover = Gtk.Popover(has_arrow=True)
        popover.set_child(self._menu_box(entries, popover))
        return popover

    def _menu_box(self, entries, popover):
        """The actions for a package, shared by the row button and the
        right-click menu so both offer exactly the same thing."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)

        def item(label, callback, destructive=False):
            classes = ["flat"] + (["destructive-action"] if destructive else [])
            button = Gtk.Button(label=label, css_classes=classes)
            button.get_child().set_xalign(0)
            button.connect("clicked",
                           lambda *a: (popover.popdown(), callback()))
            box.append(button)

        single = entries[0] if len(entries) == 1 else None
        if single:
            item(_("repo_details"), lambda: self._show_details(single))
            item(_("repo_rename"), lambda: self._rename(single))
            item(_("repo_copy_install"),
                 lambda: self._copy(repo.install_command(single["package"])))
            item(_("repo_copy_path"), lambda: self._copy(single["path"]))
            item(_("repo_verify"), lambda: self._verify(single))
            item(_("repo_open_folder"),
                 lambda: self._open(os.path.dirname(single["path"])))
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        item(_("repo_remove"), lambda: self._on_remove(entries),
             destructive=True)
        return box

    # -- adding ----------------------------------------------------------------
    def _on_add_clicked(self, _button):
        dialog = Gtk.FileDialog(title=_("repo_add"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        deb_filter = Gtk.FileFilter(name="Debian package (*.deb)")
        deb_filter.add_pattern("*.deb")
        filters.append(deb_filter)
        dialog.set_filters(filters)
        dialog.open_multiple(self.window, None, self._add_chosen)

    def _add_chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = []
        for index in range(files.get_n_items()):
            gfile = files.get_item(index)
            if gfile and gfile.get_path():
                paths.append(gfile.get_path())
        self.add_paths(paths)

    def _on_drop(self, _target, value, _x, _y):
        try:
            files = value.get_files()
        except Exception:
            return False
        paths = [f.get_path() for f in files
                 if f.get_path() and f.get_path().endswith(".deb")]
        if not paths:
            return False
        self.add_paths(paths)
        return True

    def add_paths(self, paths):
        """Copy packages into the pool. Used by the button, drag and drop,
        and by the Package page when a build finishes."""
        if not paths or not getattr(self, "profile", None):
            return
        profile = self.profile

        def done(result, error):
            if error:
                self.window.toast(str(error))
                return
            added, failures = result
            if added:
                self.window.toast(_("repo_added", n=len(added)))
            if failures:
                self.window.toast(_("repo_add_failed", n=len(failures))
                                  + f" — {failures[0][1]}")
            self.refresh()

        run_async(lambda: repo.add_packages(profile, paths), done)

    # -- removing --------------------------------------------------------------
    def _on_remove(self, entries):
        if not entries:
            return
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_remove_q", n=len(entries)),
                                   body=_("repo_remove_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("remove", _("repo_remove"))
        dialog.set_response_appearance("remove",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response != "remove":
                return
            profile = self.profile
            paths = [e["path"] for e in entries]

            def done(record, error):
                if error:
                    self.window.toast(str(error))
                    return
                self._undo.append(record)
                del self._undo[:-10]
                self.undo_btn.set_sensitive(True)
                toast = Adw.Toast(title=_("repo_removed",
                                          n=len(record["items"])), timeout=6)
                toast.set_button_label(_("repo_undo"))
                toast.connect("button-clicked", lambda *a: self.undo_last())
                self.window.toaster.add_toast(toast)
                self.refresh()

            run_async(lambda: repo.remove_packages(profile, paths), done)

        dialog.connect("response", responded)
        dialog.present()

    def undo_last(self):
        if not self._undo:
            return
        record = self._undo.pop()
        self.undo_btn.set_sensitive(bool(self._undo))

        def done(restored, error):
            self.window.toast(str(error) if error
                              else _("repo_restored", n=len(restored or [])))
            self.refresh()

        run_async(lambda: repo.restore_removed(record), done)

    # -- renaming --------------------------------------------------------------
    def _rename(self, entry):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_rename_q"),
                                   body=_("repo_rename_hint"))
        field = Gtk.Entry(text=entry["file"], activates_default=True,
                          margin_top=6)
        dialog.set_extra_child(field)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("rename", _("repo_rename"))
        dialog.set_response_appearance("rename",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")

        def responded(_dialog, response):
            if response != "rename":
                return
            new_name = field.get_text().strip()
            if not new_name or new_name == entry["file"]:
                return
            try:
                path = repo.rename_package(entry["path"], new_name)
            except (repo.RepoError, OSError) as e:
                self.window.toast(str(e))
                return
            self.window.toast(_("repo_renamed", n=os.path.basename(path)))
            self.refresh()

        dialog.connect("response", responded)
        dialog.present()

    # -- details, verification, clipboard --------------------------------------
    def _show_details(self, entry):
        def work():
            try:
                fields = repo.package_fields(entry["path"])
            except repo.RepoError as e:
                fields = {"Error": str(e)}
            return fields

        def done(fields, _error):
            lines = [f'{key}: {value}' for key, value in (fields or {}).items()
                     if key != "Description"]
            lines.append("")
            lines.append(f'{_("repo_copy_path")}: {entry["path"]}')
            body = "\n".join(lines)

            dialog = Adw.MessageDialog(transient_for=self.window,
                                       heading=_("repo_details"))
            label = Gtk.Label(label=body, xalign=0, selectable=True,
                              wrap=True, css_classes=["monospace", "caption"])
            scroller = Gtk.ScrolledWindow(child=label, min_content_height=260,
                                          propagate_natural_height=True)
            dialog.set_extra_child(scroller)
            dialog.add_response("close", _("close"))
            dialog.present()

        run_async(work, done)

    def _verify(self, entry):
        self.window.begin_operation(_("repo_verify"))

        def done(valid, error):
            self.window.end_operation()
            if error:
                self.window.toast(str(error))
                return
            self.window.toast(_("repo_verify_ok") if valid
                              else _("repo_verify_bad"))

        run_async(lambda: repo.verify(entry["path"]), done)

    def _copy(self, text):
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(text)
            self.window.toast(_("repo_copied"))

    def _copy_instructions(self):
        self._copy(repo.install_instructions(self.profile))

    def _open(self, path):
        # Built through Gio rather than by pasting the path into a string:
        # a folder name with a space or a "#" in it produces a URI that
        # opens the wrong place, or nothing at all.
        Gio.AppInfo.launch_default_for_uri(
            Gio.File.new_for_path(path).get_uri(), None)

    def _open_root(self):
        self._open(self.profile["root"])

    def _export_keyring(self):
        profile = self.profile
        if not profile["key"]:
            self.window.toast(_("repo_key_hint"))
            return

        def done(path, error):
            self.window.toast(str(error) if error
                              else _("pubkey_done", p=path))

        run_async(lambda: repo.export_key(profile["root"], profile["key"],
                                          repo.keyring_filename(profile)),
                  done)

    # -- the index -------------------------------------------------------------
    def _on_rebuild(self):
        profile = self.profile
        if not profile["key"]:
            self.window.toast(_("repo_key_hint"))
            self._edit_profile(profile)
            return
        self.build_btn.set_sensitive(False)
        self.window.begin_operation(_("repo_building"))

        def done(_result, error):
            self.window.end_operation()
            self.build_btn.set_sensitive(True)
            self.window.toast(_("repo_build_failed", e=str(error)) if error
                              else _("repo_built"))
            self.refresh()

        run_async(lambda: repo.build(profile), done)

    # -- publishing ------------------------------------------------------------
    def _on_publish(self):
        profile = self.profile
        if profile["publish_method"] == "none":
            self._edit_profile(profile)
            return
        self.publish_btn.set_sensitive(False)

        def done(changes, error):
            self.publish_btn.set_sensitive(True)
            if error:
                self.window.toast(_("repo_publish_failed", e=str(error)))
                return
            if not changes:
                self.window.toast(_("repo_publish_none"))
                return
            self._confirm_publish(changes)

        run_async(lambda: repo.publish_preview(profile), done)

    def _confirm_publish(self, changes):
        preview = "\n".join(changes[:14])
        if len(changes) > 14:
            preview += f"\n… {len(changes) - 14}"
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            heading=_("repo_publish_q", n=len(changes)),
            body=f'{self.profile["publish_target"]}\n\n'
                 f'{_("repo_publish_detail")}')
        label = Gtk.Label(label=preview, xalign=0, selectable=True,
                          css_classes=["monospace", "caption"])
        dialog.set_extra_child(Gtk.ScrolledWindow(
            child=label, min_content_height=160,
            propagate_natural_height=True))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("publish", _("repo_publish"))
        dialog.set_response_appearance("publish",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_dialog, response):
            if response != "publish":
                return
            profile = self.profile
            self.publish_btn.set_sensitive(False)
            self.window.begin_operation(_("repo_publishing"))

            def done(result, error):
                self.window.end_operation()
                self.publish_btn.set_sensitive(True)
                if error:
                    self.window.toast(_("repo_publish_failed", e=str(error)))
                    return
                self.window.toast(_("repo_published",
                                    n=(result or {}).get("files", 0)))
                self._on_check_live()

            run_async(lambda: repo.publish(profile), done)

        dialog.connect("response", responded)
        dialog.present()

    # -- the copy on the server ------------------------------------------------
    def _on_check_live(self):
        profile = self.profile
        if not profile["base_url"]:
            self._edit_profile(profile)
            return
        self.live_btn.set_sensitive(False)

        def done(result, error):
            self.live_btn.set_sensitive(True)
            if error:
                self._live, self._live_error = None, str(error)
            else:
                self._live = {name: info["version"]
                              for name, info in (result or {}).items()}
                self._live_error = ""
            self._render()
            self._render_summary()

        run_async(lambda: repo.remote_packages(profile), done)

    # -- housekeeping ----------------------------------------------------------
    def _on_prune(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_prune_q"),
                                   body=_("repo_prune_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("prune", _("repo_prune"))
        dialog.set_response_appearance("prune",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response != "prune":
                return
            profile = self.profile

            def done(record, error):
                if error:
                    self.window.toast(str(error))
                    return
                if record["items"]:
                    self._undo.append(record)
                    self.undo_btn.set_sensitive(True)
                self.window.toast(_("repo_removed", n=len(record["items"])))
                self.refresh()

            run_async(lambda: repo.prune(profile, keep=1), done)

        dialog.connect("response", responded)
        dialog.present()

    def _on_empty_trash(self):
        count = (getattr(self, "_status", {}) or {}).get("trash", 0)
        if not count:
            return
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_empty_trash_q", n=count),
                                   body=self.profile["root"])
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("empty", _("repo_empty_trash"))
        dialog.set_response_appearance("empty",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response != "empty":
                return
            profile = self.profile
            # Emptying the trash is the one thing here that cannot be undone,
            # so the undo stack is dropped with it rather than left pointing
            # at files that are gone.
            self._undo.clear()
            self.undo_btn.set_sensitive(False)

            def done(removed, error):
                self.window.toast(str(error) if error
                                  else _("repo_trash_emptied", n=removed or 0))
                self.refresh()

            run_async(lambda: repo.empty_trash(profile), done)

        dialog.connect("response", responded)
        dialog.present()

    def _on_forget(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("repo_forget_q"),
                                   body=_("repo_forget_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("forget", _("repo_forget"))
        dialog.set_response_appearance("forget",
                                       Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response == "forget":
                repo.remove_profile(self.profile["name"])
                self._live, self._live_error = None, ""
                self._build()

        dialog.connect("response", responded)
        dialog.present()

    # -- profiles --------------------------------------------------------------
    def _on_repo_chosen(self, drop, _pspec, names):
        index = drop.get_selected()
        if 0 <= index < len(names) and names[index] != self.profile["name"]:
            repo.set_active(names[index])
            self._live, self._live_error = None, ""
            self._undo.clear()
            self._build()

    def _edit_profile(self, profile):
        from .dialogs import RepoSettingsDialog

        def saved(_stored):
            self._live, self._live_error = None, ""
            self._build()
            self.window.toast(_("repo_created"))

        RepoSettingsDialog(self.window, profile, on_saved=saved).present()


# --------------------------------------------------------------------------- #
# Virus Scan — the whole project, checked by VirusTotal's engines
# --------------------------------------------------------------------------- #
from . import virustotal  # noqa: E402

_VT_CSS = """
.pc-vt-card { border-radius: 18px; padding: 22px 24px;
              border: 1px solid alpha(currentColor, 0.08); }
.pc-vt-card.clean   { background: linear-gradient(135deg, alpha(@green_3, 0.26), alpha(@green_3, 0.04) 70%); }
.pc-vt-card.flagged { background: linear-gradient(135deg, alpha(@red_3, 0.24), alpha(@red_3, 0.04) 70%); }
.pc-vt-card.unknown { background: linear-gradient(135deg, alpha(@blue_3, 0.16), alpha(@blue_3, 0.03) 70%); }
.pc-vt-big     { font-size: 30pt; font-weight: 800; }
.pc-vt-kicker  { font-size: 8pt; font-weight: 800; letter-spacing: 1.5px; opacity: 0.7; }
.pc-vt-chip    { border-radius: 999px; padding: 2px 10px; font-size: 8.5pt;
                 background: alpha(currentColor, 0.08); }
.pc-vt-chip.warn { background: alpha(@warning_color, 0.16); color: @warning_color; }
.pc-vt-engines { border-radius: 12px; padding: 12px 14px; background: alpha(currentColor, 0.05); }
.pc-vt-engine  { font-size: 10pt; }
.pc-vt-engine-name { font-weight: 700; }
"""
_vt_css_done = False


def _install_vt_css():
    global _vt_css_done
    if _vt_css_done:
        return
    from gi.repository import Gdk
    provider = Gtk.CssProvider()
    provider.load_from_string(_VT_CSS)
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _vt_css_done = True


class VirusScanPage(Gtk.Box):
    """One button: check the whole project with VirusTotal.

    The project is packed into a single archive — one request instead of
    hundreds, which is what makes this usable on a free key — and the
    archive is built the same way every time, so an unchanged project is
    recognised by its hash and never uploaded twice.

    Nothing is sent until the person has agreed, once per project, in plain
    words; and what packaging keeps back (credentials, .git, node_modules,
    the release history) is kept back here too.
    """

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.busy = False
        self._last = None          # the result of this session's scan
        _install_vt_css()
        self.page = Adw.PreferencesPage(vexpand=True)
        self.append(self.page)
        self._build()

    # -- construction ---------------------------------------------------------
    def _build(self):
        for group in list(getattr(self, "_groups", [])):
            self.page.remove(group)
        self._groups = []
        if not virustotal.key_present():
            self._build_no_key()
            return

        group = Adw.PreferencesGroup(title=_("vt_project_title"),
                                     description=_("vt_project_hint"))
        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic",
                                  valign=Gtk.Align.START,
                                  css_classes=["flat"])
        menu_btn.set_popover(self._key_menu())
        group.set_header_suffix(menu_btn)
        self.card_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        group.add(self.card_holder)

        self.scan_btn = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=18,
                                   css_classes=["suggested-action", "pill"])
        self.scan_btn.connect("clicked", lambda *a: self._scan())
        group.add(self.scan_btn)
        self.stage = Gtk.Label(margin_top=8, wrap=True,
                               css_classes=["dim-label", "caption"])
        group.add(self.stage)
        group.add(Gtk.Label(label=_("vt_quota"), margin_top=6,
                            css_classes=["dim-label", "caption"]))
        self.page.add(group)
        self._groups.append(group)
        self.refresh()

    def _build_no_key(self):
        group = Adw.PreferencesGroup()
        status = Adw.StatusPage(icon_name="security-medium-symbolic",
                                title=_("vt_no_key_title"),
                                description=_("vt_no_key_body"),
                                vexpand=True)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      halign=Gtk.Align.CENTER)
        enter = Gtk.Button(label=_("vt_set_key"),
                           css_classes=["suggested-action", "pill"])
        enter.connect("clicked", lambda *a: self._ask_key())
        col.append(enter)
        col.append(Gtk.LinkButton(uri=virustotal.KEY_PAGE,
                                  label=_("vt_get_key")))
        col.append(Gtk.Label(label=_("vt_quota"), wrap=True,
                             justify=Gtk.Justification.CENTER,
                             css_classes=["dim-label", "caption"]))
        status.set_child(col)
        group.add(status)
        self.page.add(group)
        self._groups.append(group)

    def _key_menu(self):
        popover = Gtk.Popover(has_arrow=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)

        def item(label, callback, destructive=False):
            classes = ["flat"] + (["destructive-action"] if destructive else [])
            button = Gtk.Button(label=label, css_classes=classes)
            button.get_child().set_xalign(0)
            button.connect("clicked",
                           lambda *a: (popover.popdown(), callback()))
            box.append(button)

        item(_("vt_ask_again"), self._forget_consent)
        item(_("vt_change_key"), self._ask_key)
        item(_("vt_forget_key"), self._forget_key, destructive=True)
        popover.set_child(box)
        return popover

    # -- the result card ----------------------------------------------------------
    def refresh(self):
        if not virustotal.key_present():
            self._build()
            return
        if not hasattr(self, "card_holder"):
            return
        project = config.active_project()
        if not project:
            return
        child = self.card_holder.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.card_holder.remove(child)
            child = following

        state = virustotal.load_state(project["path"])
        summary = state.get("summary")
        self.card_holder.append(self._card(project, state, summary))
        self.scan_btn.set_label(_("vt_scan_again") if summary
                                else _("vt_scan_project"))
        self.scan_btn.set_sensitive(not self.busy)

    def _card(self, project, state, summary):
        verdict = summary["verdict"] if summary else "unknown"
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       css_classes=["pc-vt-card", verdict])

        top = Gtk.Box(spacing=8)
        top.append(Gtk.Label(label=project["name"].upper(),
                             css_classes=["pc-vt-kicker"]))
        if state.get("scanned"):
            top.append(Gtk.Label(label=_("vt_last_scan",
                                         t=_ago(state["scanned"])),
                                 hexpand=True, xalign=1,
                                 css_classes=["dim-label"]))
        card.append(top)

        if not summary:
            idle = Gtk.Box(spacing=14)
            idle.append(Gtk.Image(icon_name="security-medium-symbolic",
                                  pixel_size=40, css_classes=["dim-label"]))
            idle.append(Gtk.Label(label=_("vt_never_scanned"), xalign=0,
                                  css_classes=["pc-vt-big"]))
            card.append(idle)
            return card

        flagged = summary["malicious"] + summary["suspicious"]
        big = Gtk.Box(spacing=14)
        big.append(Gtk.Image(
            icon_name={"clean": "security-high-symbolic",
                       "flagged": "dialog-warning-symbolic"}.get(
                verdict, "security-medium-symbolic"),
            pixel_size=40))
        big.append(Gtk.Label(
            label=f'{flagged} / {summary["engines"]}'
            if summary["engines"] else "?",
            css_classes=["pc-vt-big"]))
        card.append(big)
        card.append(Gtk.Label(
            label=_("vt_verdict_clean") if verdict == "clean"
            else _("vt_verdict_flagged", n=flagged, t=summary["engines"])
            if verdict == "flagged" else _("vt_verdict_unknown"),
            xalign=0, wrap=True, css_classes=["title-4"]))

        chips = Gtk.Box(spacing=6)
        if state.get("files"):
            chips.append(Gtk.Label(
                label=_("vt_archive_info", n=state["files"],
                        s=human_size(state.get("size", 0))),
                css_classes=["pc-vt-chip"]))
        left_out = (self._last or {}).get("left_out") or \
            debbuild.find_secrets(project["path"])
        if left_out:
            chips.append(Gtk.Label(label=_("vt_left_out", n=len(left_out)),
                                   tooltip_text="\n".join(left_out[:12]),
                                   css_classes=["pc-vt-chip", "warn"]))
        chips.append(Gtk.Box(hexpand=True))
        if summary.get("permalink"):
            report = Gtk.Button(css_classes=["pill", "small-pill"])
            report.set_child(Adw.ButtonContent(
                icon_name="adw-external-link-symbolic",
                label=_("vt_open_report")))
            report.connect("clicked", lambda *a: Gio.AppInfo
                           .launch_default_for_uri(summary["permalink"], None))
            chips.append(report)
        card.append(chips)

        if summary["flagged"]:
            engines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                              css_classes=["pc-vt-engines"])
            engines.append(Gtk.Label(label=_("vt_engines_flagged"), xalign=0,
                                     css_classes=["heading"]))
            for engine, result in summary["flagged"][:16]:
                line = Gtk.Box(spacing=8)
                line.append(Gtk.Label(label=engine, xalign=0,
                                      css_classes=["pc-vt-engine",
                                                   "pc-vt-engine-name"]))
                line.append(Gtk.Label(label=result, xalign=0, hexpand=True,
                                      selectable=True,
                                      css_classes=["pc-vt-engine",
                                                   "dim-label"]))
                engines.append(line)
            engines.append(Gtk.Label(label=_("vt_false_positive"), xalign=0,
                                     wrap=True, margin_top=6,
                                     css_classes=["dim-label", "caption"]))
            card.append(engines)

        if self._last and not self._last.get("uploaded") \
                and not self._last.get("needs_upload"):
            card.append(Gtk.Label(label=_("vt_reused"), xalign=0,
                                  css_classes=["dim-label", "caption"]))
        return card

    # -- scanning ---------------------------------------------------------------
    def _scan(self, allow_upload=None):
        project = config.active_project()
        if not project or self.busy:
            return
        if allow_upload is None:
            allow_upload = bool(virustotal.load_state(project["path"])
                                .get("consent"))
        self._set_busy(True)

        def on_status(stage, *extra):
            # called on the worker thread; the label belongs to the main one
            GLib.idle_add(self._show_stage, stage, extra)

        def done(result, error):
            self._set_busy(False)
            if error:
                self.stage.set_text("")
                self.window.toast(str(error))
                return
            self._last = result
            if result["needs_upload"]:
                self.stage.set_text("")
                self._ask_consent(project, result)
                return
            self.stage.set_text("")
            self.refresh()

        run_async(lambda: virustotal.scan_project(
            project["path"], allow_upload, on_status=on_status), done)

    def _show_stage(self, stage, extra):
        text = {"packing": _("vt_stage_packing"),
                "lookup": _("vt_checking"),
                "uploading": _("vt_stage_uploading")}.get(stage, "")
        if stage == "waiting" and len(extra) == 2:
            text = _("vt_stage_waiting", n=extra[0], t=extra[1])
        self.stage.set_text(text)
        return GLib.SOURCE_REMOVE

    def _set_busy(self, busy):
        self.busy = busy
        if hasattr(self, "scan_btn"):
            self.scan_btn.set_sensitive(not busy)
        if busy:
            self.window.begin_operation(_("vt_checking"))
        else:
            self.window.end_operation()

    # -- the one place anything is uploaded --------------------------------------
    def _ask_consent(self, project, result):
        dialog = Adw.MessageDialog(
            transient_for=self.window, heading=_("vt_consent_q"),
            body=_("vt_consent_body", n=result["files"],
                   s=human_size(result["size"])))
        remember = Gtk.CheckButton(label=_("vt_consent_remember"),
                                   margin_top=6)
        dialog.set_extra_child(remember)
        dialog.add_response("no", _("cancel"))
        dialog.add_response("yes", _("vt_upload_go"))
        dialog.set_response_appearance("yes",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("no")

        def responded(_d, response):
            if response != "yes":
                return
            if remember.get_active():
                virustotal.save_state(project["path"], consent=True)
            self._scan(allow_upload=True)

        dialog.connect("response", responded)
        dialog.present()

    def _forget_consent(self):
        project = config.active_project()
        if project:
            virustotal.save_state(project["path"], consent=False)
            self.window.toast(_("vt_ask_again"))

    # -- the key ------------------------------------------------------------------
    def _ask_key(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("vt_set_key"),
                                   body=_("vt_no_key_body"))
        field = Gtk.PasswordEntry(show_peek_icon=True, margin_top=8)
        dialog.set_extra_child(field)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("save", _("save"))
        dialog.set_response_appearance("save",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def responded(_dialog, response):
            if response != "save":
                return
            key = field.get_text().strip()
            if not key:
                return

            def work():
                # Checked before it is stored, so a mistyped key is caught
                # here rather than on the first real scan.
                problem = virustotal.check_key(key)
                if problem:
                    return f"refused:{problem}"
                return "stored" if virustotal.set_key(key) else "nowhere"

            def done(outcome, error):
                if error:
                    self.window.toast(str(error))
                    return
                if outcome.startswith("refused"):
                    reason = outcome.split(":", 1)[1].strip()
                    self.window.toast(f'{_("vt_key_refused")} — {reason}'
                                      if reason else _("vt_key_refused"))
                    return
                if outcome == "nowhere":
                    self.window.toast(_("vt_key_no_tool")
                                      if virustotal.why_unstored() == "no-tool"
                                      else _("vt_key_nowhere"))
                    return
                self.window.toast(_("vt_key_saved",
                                    s=virustotal.storage_kind()))
                self._build()

            run_async(work, done)

        dialog.connect("response", responded)
        dialog.present()

    def _forget_key(self):
        virustotal.clear_key()
        self.window.toast(_("vt_key_forgotten"))
        self._build()


# --------------------------------------------------------------------------- #
# Next Updates — per-project roadmap of planned features
# --------------------------------------------------------------------------- #
PRIORITIES = [("high", "prio_high"), ("normal", "prio_normal"),
              ("low", "prio_low")]
PRIO_ICON = {"high": "\u25cf", "normal": "\u25cf", "low": "\u25cf"}
PRIO_CLASS = {"high": "error", "normal": "accent", "low": "dim-label"}


class UpdatesPage(Gtk.Box):
    """Planned features / updates for the active project, stored in
    <project>/.petacore/updates.json so they travel with the project."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         margin_top=14, margin_bottom=14,
                         margin_start=16, margin_end=16)
        self.window = window

        self.hint = Gtk.Label(label=_("updates_hint"), wrap=True, xalign=0,
                              css_classes=["dim-label"])
        self.append(self.hint)

        from . import docs as _docs
        gb = round(_docs.total_ram_gb(), 1)
        self.docs_hint = Gtk.Label(
            label=_("docs_in_ram", gb=gb) if _docs.prefer_ram()
            else _("docs_on_disk", gb=gb),
            wrap=True, xalign=0, css_classes=["dim-label", "caption"])
        self.append(self.docs_hint)

        project = config.active_project()
        if project and _docs.prefer_ram():
            # Warm the cache in the background so the first open is instant.
            run_async(lambda: _docs.preload(project["path"]),
                      lambda r, e: None)

        bar = Gtk.Box(spacing=6)
        add = Gtk.Button(css_classes=["suggested-action"])
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                        label=_("add_update")))
        add.connect("clicked", self._on_add)
        bar.append(add)

        sync_plans = Gtk.Button(tooltip_text=_("sync_plans_only"))
        sync_plans.set_child(Adw.ButtonContent(
            icon_name="folder-remote-symbolic", label=_("sync_plans_only")))
        sync_plans.connect("clicked", self._on_sync_plans)
        bar.append(sync_plans)
        self._sync_plans_btn = sync_plans
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text=_("refresh"), css_classes=["flat"])
        refresh.connect("clicked", lambda *a: self.refresh())
        bar.append(refresh)
        self.append(bar)

        self.listbox = Gtk.ListBox(css_classes=["boxed-list"],
                                   selection_mode=Gtk.SelectionMode.NONE,
                                   valign=Gtk.Align.START)
        self.append(_scrolled(self.listbox))
        self.refresh()

    # -- storage ---------------------------------------------------------------
    def _load(self):
        from . import plans
        project = config.active_project()
        return plans.load(project["path"]) if project else []

    def _save(self, items):
        from . import plans
        project = config.active_project()
        if project:
            plans.save(project["path"], items)

    # -- rendering --------------------------------------------------------------
    def refresh(self):
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        items = self._load()
        if not items:
            self.listbox.append(Adw.ActionRow(title=_("no_updates")))
            return
        from . import plans as _plans
        items.sort(key=_plans.sort_key)
        for index, item in enumerate(items):
            code = item.get("priority", 2)
            prio = _plans.CODE_TO_PRIORITY.get(code, "normal")
            done = item.get("done", False)
            prio_label = _(dict(PRIORITIES).get(prio, "prio_normal"))
            state = _("done_label") if done else _("planned_label")
            subtitle = f'{state}  \u00b7  {prio_label}'
            if item.get("detail"):
                subtitle += f'\n{item["detail"]}'
            row = Adw.ActionRow(title=item.get("title", ""),
                                subtitle=subtitle, subtitle_lines=2)
            if done:
                row.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic",
                                         css_classes=["success"]))
                row.set_css_classes(["dim-label"])
            else:
                row.add_prefix(Gtk.Image(
                    icon_name="starred-symbolic" if prio == "high"
                    else "view-list-symbolic",
                    css_classes=[PRIO_CLASS.get(prio, "accent")]))
                check = Gtk.Button(icon_name="object-select-symbolic",
                                   valign=Gtk.Align.CENTER,
                                   css_classes=["flat"],
                                   tooltip_text=_("mark_done"))
                check.connect("clicked", self._toggle_done, item)
                row.add_suffix(check)

            from . import docs as _d
            attached = _d.list_docs(config.active_project()["path"],
                                    item.get("created")) \
                if config.active_project() else []
            if attached:
                subtitle += f'\n\U0001F4CE  {_("docs_count", n=len(attached))}'
                row.set_subtitle(subtitle)
                open_btn = Gtk.Button(icon_name="document-open-symbolic",
                                      valign=Gtk.Align.CENTER,
                                      css_classes=["flat"],
                                      tooltip_text=_("open_doc"))
                open_btn.connect("clicked", self._on_open_doc, item)
                row.add_suffix(open_btn)

            attach = Gtk.Button(icon_name="mail-attachment-symbolic",
                                valign=Gtk.Align.CENTER,
                                css_classes=["flat"],
                                tooltip_text=_("attach_doc"))
            attach.connect("clicked", self._on_attach_doc, item)
            row.add_suffix(attach)

            edit = Gtk.Button(icon_name="document-edit-symbolic",
                              valign=Gtk.Align.CENTER,
                              css_classes=["flat"],
                              tooltip_text=_("edit_update"))
            edit.connect("clicked", self._on_edit, item)
            row.add_suffix(edit)

            delete = Gtk.Button(icon_name="user-trash-symbolic",
                                valign=Gtk.Align.CENTER,
                                css_classes=["flat"],
                                tooltip_text=_("delete"))
            delete.connect("clicked", self._on_delete, item)
            row.add_suffix(delete)

            # Double-click the row to copy its details to the clipboard.
            click = Gtk.GestureClick(button=1)
            click.connect("pressed", self._on_row_click, item)
            row.add_controller(click)
            row.set_tooltip_text(_("details_copied"))

            self.listbox.append(row)

    def _on_row_click(self, gesture, n_press, _x, _y, item):
        """Double-click copies the plan as "Title: details"."""
        if n_press < 2:
            return
        title = (item.get("title") or "").strip()
        note = (item.get("detail") or "").strip()
        if not title and not note:
            self.window.toast(_("nothing_to_copy"))
            return
        text = f"{title}: {note}" if note else title
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(text)
            self.window.toast(_("details_copied"))

    # -- actions ----------------------------------------------------------------
    def _on_sync_plans(self, _btn):
        """Two-way sync of just the plans — project files are left alone."""
        from . import gdrive, plans
        project = config.active_project()
        if not project:
            return
        if not (gdrive.available() and gdrive.connected()):
            self.window.toast(_("not_logged_in"))
            return
        self._sync_plans_btn.set_sensitive(False)
        self.window.begin_operation(_("op_plans"))

        def done(count, error):
            self.window.end_operation()
            self._sync_plans_btn.set_sensitive(True)
            if error:
                self.window.toast(f"{_('drive_failed')}: {error}")
            else:
                self.window.toast(_("sync_plans_done", n=count or 0))
                self.refresh()

        run_async(lambda: plans.sync_with_drive(project["path"],
                                                project["name"]), done)

    def _on_add(self, _btn):
        self._plan_dialog(None)

    def _on_edit(self, _btn, item):
        self._plan_dialog(item)

    def _plan_dialog(self, item):
        """One dialog for both adding and editing: title, details and
        priority are all editable. `item` is None when adding."""
        if not config.active_project():
            self.window.toast(_("no_project_title"))
            return
        editing = item is not None
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            heading=_("edit_update") if editing else _("add_update"))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title_entry = Gtk.Entry(placeholder_text=_("update_title"),
                                activates_default=True,
                                text=item.get("title", "") if editing else "")
        box.append(title_entry)
        note_entry = Gtk.Entry(placeholder_text=_("update_note"),
                               text=item.get("detail", "") if editing else "")
        box.append(note_entry)
        prio_row = Gtk.Box(spacing=8)
        prio_row.append(Gtk.Label(label=_("priority")))
        prio_drop = Gtk.DropDown.new_from_strings(
            [_(k) for _c, k in PRIORITIES])
        from . import plans as _plans
        codes = [c for c, _k in PRIORITIES]
        current = _plans.CODE_TO_PRIORITY.get(
            item.get("priority", 2), "normal") if editing else "normal"
        prio_drop.set_selected(codes.index(current)
                               if current in codes else 1)
        prio_row.append(prio_drop)
        box.append(prio_row)

        dialog.set_extra_child(box)

        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("ok", _("save") if editing else _("add_update"))
        dialog.set_response_appearance("ok",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def responded(_d, response):
            if response != "ok":
                return
            title = title_entry.get_text().strip()
            if not title:
                return
            note = note_entry.get_text().strip()
            priority = _plans.PRIORITY_TO_CODE[
                PRIORITIES[prio_drop.get_selected()][0]]
            items = self._load()
            if editing:
                for entry in items:
                    if entry.get("created") == item.get("created"):
                        entry["title"] = title
                        entry["detail"] = note
                        entry["priority"] = priority
                        entry["updated"] = time.time()
                self._save(items)
                self.window.toast(_("update_saved"))
            else:
                items.append({
                    "title": title,
                    "detail": note,
                    "priority": priority,
                    "done": False,
                    "created": time.time(),
                    "updated": time.time(),
                })
                self._save(items)
                self.window.toast(_("update_added"))
            self.refresh()

        dialog.connect("response", responded)
        dialog.present()

    def _toggle_done(self, _btn, item):
        items = self._load()
        for i in items:
            if i.get("created") == item.get("created") \
                    and i.get("title") == item.get("title"):
                i["done"] = not i.get("done", False)
        self._save(items)
        self.refresh()

    def _on_attach_doc(self, _btn, item):
        project = config.active_project()
        if not project:
            return
        dialog = Gtk.FileDialog(title=_("attach_doc"))
        dialog.open(self.window, None, self._doc_chosen, item)

    def _doc_chosen(self, dialog, result, item):
        from . import docs as _d
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if not gfile or not gfile.get_path():
            return
        project = config.active_project()
        try:
            meta = _d.attach(project["path"], item.get("created"),
                             gfile.get_path())
        except OSError as e:
            self.window.toast(str(e))
            return
        self.window.toast(_("doc_attached", f=meta["name"]))
        self.refresh()

    def _on_open_doc(self, _btn, item):
        from . import docs as _d
        project = config.active_project()
        if not project:
            return
        attached = _d.list_docs(project["path"], item.get("created"))
        if not attached:
            self.window.toast(_("no_docs"))
            return
        if len(attached) == 1:
            self._launch_doc(attached[0])
            return
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        for doc in attached:
            row = Gtk.Box(spacing=4)
            btn = Gtk.Button(label=doc["name"], css_classes=["flat"],
                             hexpand=True)
            btn.connect("clicked",
                        lambda _b, d=doc: (pop.popdown(),
                                           self._launch_doc(d)))
            row.append(btn)
            rm = Gtk.Button(icon_name="user-trash-symbolic",
                            css_classes=["flat"])
            rm.connect("clicked",
                       lambda _b, d=doc, it=item: (pop.popdown(),
                                                   self._remove_doc(it, d)))
            row.append(rm)
            box.append(row)
        pop.set_child(box)
        pop.set_parent(_btn)
        pop.popup()

    def _launch_doc(self, doc):
        from . import docs as _d
        path = _d.resolve_for_open(doc["path"])
        Gio.AppInfo.launch_default_for_uri(f"file://{path}", None)

    def _remove_doc(self, item, doc):
        from . import docs as _d
        project = config.active_project()
        if project:
            _d.detach(project["path"], item.get("created"), doc["name"])
            self.window.toast(_("doc_removed"))
            self.refresh()

    def _on_delete(self, _btn, item):
        from . import docs as _d
        project = config.active_project()
        if project:
            _d.remove_all(project["path"], item.get("created"))
        items = [i for i in self._load()
                 if not (i.get("created") == item.get("created")
                         and i.get("title") == item.get("title"))]
        self._save(items)
        self.refresh()

# --------------------------------------------------------------------------- #
# Sandbox — run commands with zero effect on the real system
# --------------------------------------------------------------------------- #
from .sandbox import SandboxSession, isolation_available  # noqa: E402


class SandboxPage(Gtk.Box):
    """One button to start an isolated shell, one to end it. Inside, the
    whole system is read-only, so nothing the user types can damage it."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        window.sandbox_page = self
        self.session = None

        self.stack = Gtk.Stack(vexpand=True)
        self.append(self.stack)

        # -- idle ---------------------------------------------------------------
        start = Adw.StatusPage(icon_name="application-x-addon-symbolic",
                               title=_("sandbox"),
                               description=_("sandbox_simple"), vexpand=True)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      halign=Gtk.Align.CENTER)
        btns = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        b1 = Gtk.Button(label=_("start_sandbox"),
                        css_classes=["suggested-action", "pill"])
        b1.connect("clicked", lambda *a: self._start(with_project=False))
        b2 = Gtk.Button(label=_("with_project"), css_classes=["pill"])
        b2.connect("clicked", lambda *a: self._start(with_project=True))
        btns.append(b1)
        btns.append(b2)
        col.append(btns)

        ok = isolation_available()
        col.append(Gtk.Label(
            label=_("isolated_on") if ok else _("isolated_off"),
            wrap=True, justify=Gtk.Justification.CENTER,
            css_classes=["success"] if ok else ["warning"]))
        start.set_child(col)
        self.stack.add_named(start, "idle")

        # -- running -------------------------------------------------------------
        run_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8,
                      margin_start=12, margin_end=12)
        self.status = Gtk.Label(xalign=0, hexpand=True,
                                css_classes=["dim-label", "caption"],
                                ellipsize=Pango.EllipsizeMode.MIDDLE)
        bar.append(self.status)
        end_btn = Gtk.Button(label=_("end_sim"),
                             css_classes=["destructive-action"])
        end_btn.connect("clicked", lambda *a: self._end())
        bar.append(end_btn)
        run_box.append(bar)

        self.term_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   vexpand=True)
        run_box.append(self.term_holder)
        self.stack.add_named(run_box, "running")

        self.stack.set_visible_child_name("idle")

    # -- start ------------------------------------------------------------------
    def _start(self, with_project, network=None):
        session = SandboxSession(network=network)
        self._with_project = with_project
        if not with_project:
            self._open(session, None)
            return
        project = config.active_project()
        if not project:
            self.window.toast(_("no_project_title"))
            session.destroy()
            return
        self.window.toast(_("extracting"))
        run_async(lambda: session.copy_project(project["path"]),
                  lambda wd, err: self._open(session, err))

    def _open(self, session, error):
        if error:
            session.destroy()
            self.window.toast(f"{_('error')}: {error}")
            return
        self.session = session
        mark = _("isolated_on") if session.isolated else _("isolated_off")
        net = _("net_on_badge") if session.network else _("net_off_badge")
        self.status.set_text(f"{mark}   \u00b7   {net}   \u00b7   {session.dir}")

        # Watch the output for the sign that something wanted the network.
        from .netwatch import OutputWatcher
        self._watcher = OutputWatcher(self._on_network_wanted) \
            if not session.network else None

        for child in list(self.term_holder):
            self.term_holder.remove(child)
        if HAVE_VTE:
            self.term = make_terminal()
            self.term.spawn_async(
                Vte.PtyFlags.DEFAULT, session.work, session.shell_argv(),
                None, GLib.SpawnFlags.DEFAULT, None, None, -1, None,
                lambda _t, pid, err, *a: setattr(session, "shell_pid", pid)
                if not err and pid > 0 else None)
            self.term_holder.append(terminal_frame(self.term))
            self.term.grab_focus()
            if self._watcher is not None:
                self.term.connect("contents-changed", self._scan_output)
        else:
            self.term = None
            self.term_holder.append(Adw.StatusPage(
                icon_name="utilities-terminal-symbolic", title=_("terminal"),
                description="gir1.2-vte-3.91 is required "
                            "(sudo apt install gir1.2-vte-3.91)",
                vexpand=True))
        self.stack.set_visible_child_name("running")

    # -- network permission ------------------------------------------------------
    def _scan_output(self, terminal):
        """Read the visible text and let the watcher judge it."""
        if self._watcher is None or self._watcher.fired:
            return
        if not config.get("sandbox_network_prompt"):
            return
        try:
            text = terminal.get_text_range_format(
                Vte.Format.TEXT, 0, 0,
                terminal.get_row_count(), terminal.get_column_count())
            text = text[0] if isinstance(text, tuple) else text
        except Exception:
            return
        self._watcher.feed(text or "")

    def _on_network_wanted(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("sandbox_net_asked"),
                                   body=_("sandbox_net_body"))
        dialog.add_response("never", _("net_never_ask"))
        dialog.add_response("deny", _("net_deny"))
        dialog.add_response("allow", _("net_allow"))
        dialog.set_response_appearance("allow",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("deny")
        dialog.set_close_response("deny")

        def responded(_d, response):
            if response == "never":
                config.set("sandbox_network_prompt", False)
            elif response == "allow":
                self._confirm_network()

        dialog.connect("response", responded)
        dialog.present()

    def _confirm_network(self):
        """Opening the network is worth a second look — it is the one action
        here that removes a protection rather than adding one."""
        confirm = Adw.MessageDialog(transient_for=self.window,
                                    heading=_("net_confirm_q"),
                                    body=_("net_confirm_body"))
        confirm.add_response("cancel", _("cancel"))
        confirm.add_response("open", _("net_confirm_yes"))
        confirm.set_response_appearance("open",
                                        Adw.ResponseAppearance.DESTRUCTIVE)
        confirm.set_default_response("cancel")

        def responded(_d, response):
            if response != "open":
                return
            # The network namespace is fixed when the jail is built, so the
            # session is replaced rather than modified.
            with_project = getattr(self, "_with_project", False)
            if self.session:
                self.session.destroy()
                self.session = None
            self._start(with_project, network=True)

        confirm.connect("response", responded)
        confirm.present()

    # -- end --------------------------------------------------------------------
    def _end(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("end_sim_question"),
                                   body=_("end_sim_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("end", _("end_sim"))
        dialog.set_response_appearance("end",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._end_confirmed)
        dialog.present()

    def _end_confirmed(self, _d, response):
        if response != "end":
            return
        session = self.session
        self.session = None
        for child in list(self.term_holder):
            self.term_holder.remove(child)
        self.stack.set_visible_child_name("idle")
        if session:
            run_async(session.destroy,
                      lambda r, e: self.window.toast(_("sim_ended")))

    def end_all(self):
        if self.session:
            self.session.destroy()
            self.session = None


# --------------------------------------------------------------------------- #
# Repository → Server: the repository folder on a server, over SSH
# --------------------------------------------------------------------------- #
from . import remote  # noqa: E402

from .remote import TWO_FACTOR_HOWTO as _TWO_FACTOR_HOWTO  # noqa: E402


_REMOTE_CSS = """
.pc-srv-head { padding: 12px 16px; border-radius: 14px; }
"""
_remote_css_done = False


def _install_remote_css():
    global _remote_css_done
    if _remote_css_done:
        return
    from gi.repository import Gdk
    provider = Gtk.CssProvider()
    provider.load_from_string(_REMOTE_CSS)
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _remote_css_done = True


def _short_path(path):
    """~/.ssh/id_ed25519 rather than id_ed25519: two keys with the same file
    name in different folders must not look identical in the list."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


class RemotePanel(Gtk.Box):
    """The repository folder on a server, as a file manager.

    All the security lives in remote.py; this is only the window onto it.
    The one thing this class must get right itself is the questions: a
    passphrase or a two-factor code is asked for in a dialog, handed to ssh
    and not kept, and anything the *server* wrote is shown as coming from
    the server, so a hostile one cannot pass its prompt off as Petacore's.
    """

    def __init__(self, window, owner):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        _install_remote_css()
        self.window = window
        self.owner = owner
        self.session = None
        self.cwd = ""
        self._undo = []
        self.stack = Gtk.Stack(
            vexpand=True,
            transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
            transition_duration=220)
        self.append(self.stack)
        self.stack.add_named(self._build_setup(), "setup")
        self.stack.add_named(self._build_browser(), "browser")
        self.stack.set_visible_child_name("setup")

    @property
    def profile(self):
        return self.owner.profile

    # -- the connection form --------------------------------------------------------
    def _build_setup(self):
        page = Adw.PreferencesPage()
        profile = self.profile

        server = Adw.PreferencesGroup(title=_("srv_title"),
                                      description=_("srv_hint"))
        self.host_row = Adw.EntryRow(title=_("srv_host"),
                                     text=profile["server_host"])
        server.add(self.host_row)
        self.port_row = Adw.EntryRow(title=_("srv_port"),
                                     text=str(profile["server_port"] or 22),
                                     input_purpose=Gtk.InputPurpose.DIGITS)
        server.add(self.port_row)
        self.user_row = Adw.EntryRow(title=_("srv_user"),
                                     text=profile["server_user"])
        server.add(self.user_row)
        self.root_row = Adw.EntryRow(
            title=_("srv_root"),
            text=profile["server_root"] or "/var/www/repo")
        server.add(self.root_row)
        page.add(server)

        identity = Adw.PreferencesGroup(title=_("srv_identity"))
        self.key_row = Adw.ComboRow(title=_("srv_identity"))
        identity.add(self.key_row)
        pick = Adw.ActionRow(title=_("srv_pick_key"), activatable=True)
        pick.add_prefix(Gtk.Image(icon_name="document-open-symbolic"))
        pick.connect("activated", lambda *a: self._pick_identity())
        identity.add(pick)
        new_key = Adw.ActionRow(title=_("srv_new_key"),
                                subtitle=_("srv_new_key_hint"),
                                activatable=True)
        new_key.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        new_key.connect("activated", lambda *a: self._new_key_dialog())
        identity.add(new_key)
        page.add(identity)
        self._load_identities(profile["server_identity"])

        security = Adw.PreferencesGroup(title=_("srv_security"))
        summary = remote.security_summary()
        for icon, title, subtitle in (
                ("channel-secure-symbolic", _("srv_sec_hostkey"),
                 "StrictHostKeyChecking"),
                ("security-high-symbolic",
                 _("srv_sec_pq") if summary["post_quantum"]
                 else _("srv_sec_kex"), summary["kex"]),
                ("changes-prevent-symbolic", _("srv_sec_cipher"),
                 summary["cipher"]),
                ("dialog-password-symbolic", _("srv_sec_nopass"), ""),
                ("edit-clear-symbolic", _("srv_sec_nostore"), ""),
                ("phone-symbolic", _("srv_sec_2fa"), "")):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image(icon_name=icon,
                                     css_classes=["success"]))
            security.add(row)
        howto = Adw.ExpanderRow(title=_("srv_2fa_howto"))
        howto.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        howto.add_row(Gtk.Label(label=_TWO_FACTOR_HOWTO, xalign=0,
                                selectable=True, wrap=False,
                                margin_top=10, margin_bottom=10,
                                margin_start=14, margin_end=14,
                                css_classes=["monospace", "caption"]))
        security.add(howto)
        page.add(security)

        go = Adw.PreferencesGroup()
        self.connect_btn = Gtk.Button(label=_("srv_connect"),
                                      halign=Gtk.Align.CENTER,
                                      css_classes=["suggested-action", "pill"])
        self.connect_btn.connect("clicked", lambda *a: self._connect())
        go.add(self.connect_btn)
        self.setup_status = Gtk.Label(margin_top=8, wrap=True,
                                      css_classes=["dim-label", "caption"])
        go.add(self.setup_status)
        page.add(go)
        return page

    def _load_identities(self, preferred=""):
        # The saved key is always offered, wherever it lives. Leaving it out
        # because it is not in ~/.ssh would make the list fall back to the
        # first key it has — logging in as someone the person never chose.
        preferred = os.path.expanduser(preferred or "")
        self._identities = remote.list_identities()
        if preferred and os.path.isfile(preferred) \
                and preferred not in self._identities:
            self._identities.insert(0, preferred)
        labels = [_short_path(p) for p in self._identities] \
            or [_("srv_no_identity")]
        self.key_row.set_model(Gtk.StringList.new(labels))
        for index, path in enumerate(self._identities):
            if path == preferred:
                self.key_row.set_selected(index)

    def _pick_identity(self):
        dialog = Gtk.FileDialog(title=_("srv_pick_key"))
        dialog.set_initial_folder(
            Gio.File.new_for_path(os.path.expanduser("~/.ssh")))

        def chosen(d, result):
            try:
                picked = d.open_finish(result)
            except GLib.Error:
                return
            path = picked.get_path() if picked else ""
            if not path:
                return
            if path.endswith(".pub"):
                # the public half was picked; ssh needs the private one
                path = path[:-4]
            if not os.path.isfile(path):
                self.window.toast(_("srv_key_missing"))
                return
            self._load_identities(path)

        dialog.open(self.window, None, chosen)

    def _chosen_identity(self):
        if not self._identities:
            return ""
        return self._identities[min(self.key_row.get_selected(),
                                    len(self._identities) - 1)]

    # -- a key of its own -------------------------------------------------------------
    def _new_key_dialog(self):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("srv_new_key"),
                                   body=_("srv_new_key_hint"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=6)
        host = self.host_row.get_text().strip() or "server"
        name = Gtk.Entry(text=f"petacore_{re.sub(r'[^a-z0-9]+', '_', host.lower())}",
                         placeholder_text=_("srv_key_name"))
        first = Gtk.PasswordEntry(show_peek_icon=True,
                                  placeholder_text=_("srv_key_pass"))
        second = Gtk.PasswordEntry(show_peek_icon=True,
                                   placeholder_text=_("srv_key_pass2"))
        for widget in (name, first, second):
            box.append(widget)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("create", _("create_key"))
        dialog.set_response_appearance("create",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_d, response):
            if response != "create":
                return
            if first.get_text() != second.get_text():
                self.window.toast(_("srv_key_mismatch"))
                return
            path = os.path.join("~/.ssh", re.sub(r"[^A-Za-z0-9_.-]", "_",
                                                 name.get_text().strip()))
            passphrase = first.get_text()
            first.set_text("")
            second.set_text("")

            def done(public, error):
                if error:
                    self.window.toast(str(error))
                    return
                self._load_identities(os.path.expanduser(path))
                self._show_public_key(public)

            run_async(lambda: remote.generate_key(
                path, passphrase, f"petacore@{host}"), done)

        dialog.connect("response", responded)
        dialog.present()

    def _show_public_key(self, public):
        dialog = Adw.MessageDialog(transient_for=self.window,
                                   heading=_("srv_key_created"),
                                   body=_("srv_key_where"))
        label = Gtk.Label(label=public, wrap=True, selectable=True,
                          wrap_mode=Pango.WrapMode.CHAR,
                          css_classes=["monospace", "caption"])
        dialog.set_extra_child(label)
        dialog.add_response("copy", _("repo_copy_path"))
        dialog.add_response("close", _("close"))

        def responded(_d, response):
            if response == "copy":
                from gi.repository import Gdk
                Gdk.Display.get_default().get_clipboard().set(public)
                self.window.toast(_("repo_copied"))

        dialog.connect("response", responded)
        dialog.present()

    # -- connecting ------------------------------------------------------------------
    def _target(self):
        return {"host": self.host_row.get_text().strip(),
                "port": self.port_row.get_text().strip() or "22",
                "user": self.user_row.get_text().strip(),
                "root": self.root_row.get_text().strip(),
                "identity": self._chosen_identity()}

    def _remember(self, target):
        stored = repo.save_profile(dict(
            self.profile, server_host=target["host"],
            server_port=target["port"], server_user=target["user"],
            server_root=target["root"],
            server_identity=target["identity"]), self.profile["name"])
        self.owner.profile = stored

    def _connect(self):
        target = self._target()
        if not target["host"] or not target["user"] or not target["root"]:
            self.window.toast(_("srv_fields_missing"))
            return
        try:
            target = remote.normalise_target(target)
        except remote.RemoteError as e:
            self.window.toast(str(e))
            return
        self._remember(target)
        session = remote.Session(target, self._ask)
        self.connect_btn.set_sensitive(False)
        self.setup_status.set_text(_("srv_connecting"))

        def done(_result, error):
            self.connect_btn.set_sensitive(True)
            self.setup_status.set_text("")
            if isinstance(error, remote.HostKeyUnknown):
                self._confirm_host(session, error)
                return
            if isinstance(error, remote.HostKeyChanged):
                self._host_changed(session)
                return
            if error:
                self.window.toast(str(error))
                return
            self.session = session
            self.cwd = ""
            self._fill_header()
            self.stack.set_visible_child_name("browser")
            self.refresh()

        run_async(session.connect, done)

    def _confirm_host(self, session, info):
        host = session.target["host"]
        dialog = Adw.MessageDialog(
            transient_for=self.window, heading=_("srv_trust_q"),
            body=_("srv_trust_body", h=host)
            + "\n\nssh-keygen -lf /etc/ssh/ssh_host_"
            + info.key_type.lower().split("-")[0] + "_key.pub")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=10)
        box.append(Gtk.Label(label=_("srv_fingerprint") + f"  ({info.key_type})",
                             xalign=0, css_classes=["dim-label", "caption"]))
        # Pango hyphenates a broken line by default, and a "-" appearing
        # inside a fingerprint is exactly what must not happen when a person
        # is comparing it character by character. So: no hyphens, and a size
        # that fits the whole thing on one line in the dialog.
        no_hyphens = Pango.AttrList()
        no_hyphens.insert(Pango.attr_insert_hyphens_new(False))
        box.append(Gtk.Label(label=info.fingerprint, xalign=0,
                             selectable=True, wrap=True,
                             wrap_mode=Pango.WrapMode.CHAR,
                             attributes=no_hyphens,
                             css_classes=["monospace", "heading"]))
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("trust", _("srv_trust_go"))
        dialog.set_response_appearance("trust",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")

        def responded(_d, response):
            if response != "trust":
                return
            try:
                remote.trust_host_key(session.target, info.line)
            except remote.RemoteError as e:
                self.window.toast(str(e))
                return
            self._connect()

        dialog.connect("response", responded)
        dialog.present()

    def _host_changed(self, session):
        dialog = Adw.MessageDialog(
            transient_for=self.window, heading=_("srv_changed_q"),
            body=_("srv_changed_body", h=session.target["host"]))
        dialog.add_response("close", _("close"))
        dialog.add_response("forget", _("srv_forget_key"))
        dialog.set_response_appearance("forget",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("close")

        def responded(_d, response):
            if response == "forget":
                # Forgetting only removes the old record; the new key still
                # has to be checked and trusted by hand on the next connect.
                remote.forget_host_key(session.target)

        dialog.connect("response", responded)
        dialog.present()

    # -- the questions ssh asks ----------------------------------------------------------
    def _ask(self, kind, prompt):
        """Called on a background thread by the askpass bridge. Blocks until
        the person answers, then returns the answer — which is not stored
        anywhere on the way."""
        if kind == "touch":
            GLib.idle_add(lambda: self.window.toast(_("srv_touch")) and False)
            return ""
        done = threading.Event()
        box = {"answer": None}

        def show():
            heading = {"passphrase": _("srv_prompt_passphrase"),
                       "code": _("srv_prompt_code"),
                       "password": _("srv_prompt_password")}.get(
                kind, _("srv_prompt_other"))
            # A passphrase prompt comes from ssh on this machine; anything
            # else was written by the server, and is labelled as such.
            body = prompt if kind == "passphrase" \
                else f'{_("srv_prompt_from_server")}\n{prompt}'
            dialog = Adw.MessageDialog(transient_for=self.window,
                                       heading=heading, body=body)
            if kind == "code":
                entry = Gtk.Entry(input_purpose=Gtk.InputPurpose.DIGITS,
                                  max_length=12, activates_default=True,
                                  css_classes=["title-2"], xalign=0.5)
            else:
                entry = Gtk.PasswordEntry(show_peek_icon=True,
                                          activates_default=True)
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", _("cancel"))
            dialog.add_response("ok", _("srv_ok"))
            dialog.set_response_appearance("ok",
                                           Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("ok")

            def responded(_d, response):
                box["answer"] = entry.get_text() if response == "ok" \
                    else None
                entry.set_text("")
                done.set()

            dialog.connect("response", responded)
            dialog.present()
            entry.grab_focus()
            return False

        GLib.idle_add(show)
        done.wait(300)
        answer, box["answer"] = box["answer"], None
        return answer

    # -- the browser ---------------------------------------------------------------------
    def _build_browser(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        head = Gtk.Box(spacing=10, css_classes=["card", "pc-srv-head"])
        head.append(Gtk.Image(icon_name="channel-secure-symbolic",
                              pixel_size=22, css_classes=["success"]))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.head_title = Gtk.Label(xalign=0, css_classes=["heading"])
        self.head_sub = Gtk.Label(xalign=0, selectable=True,
                                  css_classes=["dim-label", "caption",
                                               "monospace"])
        titles.append(self.head_title)
        titles.append(self.head_sub)
        head.append(titles)
        disconnect = Gtk.Button(label=_("srv_disconnect"),
                                valign=Gtk.Align.CENTER)
        disconnect.connect("clicked", lambda *a: self.disconnect())
        head.append(disconnect)
        outer.append(head)

        bar = Gtk.Box(spacing=6)
        self.up_btn = Gtk.Button(icon_name="go-up-symbolic",
                                 tooltip_text=_("srv_root_up"),
                                 css_classes=["flat"])
        self.up_btn.connect("clicked", lambda *a: self._go_up())
        bar.append(self.up_btn)
        self.crumb = Gtk.Label(xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.START,
                               css_classes=["monospace"])
        bar.append(self.crumb)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             css_classes=["flat"])
        refresh.connect("clicked", lambda *a: self.refresh())
        bar.append(refresh)
        folder = Gtk.Button(icon_name="folder-new-symbolic",
                            tooltip_text=_("srv_new_folder"),
                            css_classes=["flat"])
        folder.connect("clicked", lambda *a: self._new_folder())
        bar.append(folder)
        self.undo_btn = Gtk.Button(icon_name="edit-undo-symbolic",
                                   tooltip_text=_("repo_undo"),
                                   sensitive=False, css_classes=["flat"])
        self.undo_btn.connect("clicked", lambda *a: self._undo_last())
        bar.append(self.undo_btn)
        upload = Gtk.Button()
        upload.set_child(Adw.ButtonContent(icon_name="document-send-symbolic",
                                           label=_("srv_upload")))
        upload.connect("clicked", lambda *a: self._pick_uploads())
        bar.append(upload)
        self.rebuild_btn = Gtk.Button(css_classes=["suggested-action"])
        self.rebuild_btn.set_child(Adw.ButtonContent(
            icon_name="view-refresh-symbolic", label=_("srv_rebuild")))
        self.rebuild_btn.connect("clicked", lambda *a: self._rebuild())
        bar.append(self.rebuild_btn)
        outer.append(bar)

        self.stale_banner = Adw.Banner(title=_("srv_stale"),
                                       button_label=_("srv_rebuild"),
                                       revealed=False)
        self.stale_banner.connect("button-clicked", lambda *a: self._rebuild())
        outer.append(self.stale_banner)

        self.files = Gtk.ListBox(css_classes=["boxed-list"],
                                 selection_mode=Gtk.SelectionMode.NONE,
                                 valign=Gtk.Align.START)
        self.files.connect("row-activated", self._on_activated)
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        wrap.append(self.files)
        outer.append(_scrolled(wrap))
        outer.append(Gtk.Label(label=_("srv_deb_hint"), xalign=0,
                               css_classes=["dim-label", "caption"]))

        from gi.repository import Gdk
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        outer.add_controller(drop)
        return outer

    def _fill_header(self):
        t = self.session.target
        self.head_title.set_text(f'{t["user"]}@{t["host"]}'
                                 + (f':{t["port"]}' if t["port"] != 22 else ""))
        try:
            fingerprint = remote.scan_host_key(t)[0]
        except remote.RemoteError:
            fingerprint = ""
        self.head_sub.set_text(f'{t["root"]}   ·   {fingerprint}')

    def refresh(self):
        if not self.session:
            return
        self.crumb.set_text(self.session.path(self.cwd))
        self.up_btn.set_sensitive(bool(self.cwd))

        def done(entries, error):
            if isinstance(error, remote.NotConnected):
                self._lost()
                return
            if error:
                self.window.toast(str(error))
                return
            self._render(entries or [])

        run_async(lambda: (self.session.ensure_root(),
                           self.session.listdir(self.cwd))[1], done)

    def _render(self, entries):
        while (row := self.files.get_row_at_index(0)) is not None:
            self.files.remove(row)
        if not entries:
            empty = Adw.ActionRow(title=_("srv_empty_folder"))
            empty.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            self.files.append(empty)
            return
        for entry in entries:
            icon = ("insert-link-symbolic" if entry["link"]
                    else "folder-symbolic" if entry["dir"]
                    else "package-x-generic-symbolic"
                    if entry["name"].endswith(".deb")
                    else "text-x-script-symbolic"
                    if entry["name"].endswith(".sh")
                    else "text-x-generic-symbolic")
            when = _ago(entry["mtime"])
            if entry["link"]:
                # A link on the server is shown for what it is. Petacore
                # never follows one, so it must not look like a file that
                # can be opened or a folder that can be entered.
                subtitle = f'{_("srv_link")}  ·  {when}'
            elif entry["dir"]:
                subtitle = when
            else:
                subtitle = f'{human_size(entry["size"])}  ·  {when}'
            row = Adw.ActionRow(title=entry["name"], subtitle=subtitle,
                                activatable=entry["dir"])
            row.set_tooltip_text(_stamp(entry["mtime"]))
            if entry["link"]:
                row.add_css_class("dim-label")
            row._entry = entry
            row.add_prefix(Gtk.Image(icon_name=icon))
            menu = Gtk.MenuButton(icon_name="view-more-symbolic",
                                  valign=Gtk.Align.CENTER,
                                  css_classes=["flat"])
            menu.set_popover(self._row_menu(entry))
            row.add_suffix(menu)
            if entry["dir"]:
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic",
                                         css_classes=["dim-label"]))
            self.files.append(row)

    def _row_menu(self, entry):
        popover = Gtk.Popover(has_arrow=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)

        def item(label, callback, destructive=False):
            button = Gtk.Button(label=label, css_classes=["flat"] + (
                ["destructive-action"] if destructive else []))
            button.get_child().set_xalign(0)
            button.connect("clicked",
                           lambda *a: (popover.popdown(), callback()))
            box.append(button)

        item(_("srv_rename_q"), lambda: self._rename(entry))
        if not entry["dir"]:
            item(_("srv_download"), lambda: self._download(entry))
        item(_("srv_remove"), lambda: self._remove(entry), destructive=True)
        popover.set_child(box)
        return popover

    def _on_activated(self, _list, row):
        entry = getattr(row, "_entry", None)
        if entry and entry["dir"]:
            self.cwd = entry["rel"]
            self.refresh()

    def _go_up(self):
        self.cwd = posixpath.dirname(self.cwd.rstrip("/"))
        self.refresh()

    # -- changing the server ---------------------------------------------------------
    def _pick_uploads(self):
        dialog = Gtk.FileDialog(title=_("srv_upload"))
        dialog.open_multiple(self.window, None, self._picked)

    def _picked(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = [files.get_item(i).get_path()
                 for i in range(files.get_n_items())
                 if files.get_item(i).get_path()]
        self._upload(paths)

    def _on_drop(self, _target, value, _x, _y):
        try:
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
        except Exception:  # noqa: BLE001
            return False
        paths = [p for p in paths if os.path.isfile(p)]
        if paths:
            self._upload(paths)
        return bool(paths)

    def _upload(self, paths):
        if not self.session or not paths:
            return
        session, cwd = self.session, self.cwd
        component = self.profile["component"]
        self.window.begin_operation(_("srv_uploading", n=len(paths)))

        def work():
            packages = False
            for path in paths:
                if path.endswith(".deb"):
                    session.upload_package(path, component)
                    packages = True
                else:
                    session.upload(path, cwd)
            return packages

        def done(packages, error):
            self.window.end_operation()
            if isinstance(error, remote.NotConnected):
                self._lost()
                return
            if error:
                self.window.toast(str(error))
            else:
                self.window.toast(_("srv_uploaded", n=len(paths)))
                if packages:
                    self.stale_banner.set_revealed(True)
            self.refresh()

        run_async(work, done)

    def _new_folder(self):
        self._ask_name(_("srv_new_folder"), "", lambda name: run_async(
            lambda: self.session.mkdir(self.cwd, name),
            lambda r, e: (self.window.toast(str(e)) if e else None,
                          self.refresh())))

    def _rename(self, entry):
        self._ask_name(_("srv_rename_q"), entry["name"], lambda name: run_async(
            lambda: self.session.rename(entry["rel"], name),
            lambda r, e: (self.window.toast(str(e)) if e else None,
                          self.refresh())))

    def _ask_name(self, heading, current, callback):
        dialog = Adw.MessageDialog(transient_for=self.window, heading=heading)
        entry = Gtk.Entry(text=current, activates_default=True,
                          placeholder_text=_("srv_folder_name"))
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("ok", _("srv_ok"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.connect("response", lambda _d, r: callback(
            entry.get_text().strip()) if r == "ok"
            and entry.get_text().strip()
            and entry.get_text().strip() != current else None)
        dialog.present()

    def _download(self, entry):
        dialog = Gtk.FileDialog(title=_("srv_download"),
                                initial_name=entry["name"])

        def chosen(d, result):
            try:
                target = d.save_finish(result)
            except GLib.Error:
                return
            if not target or not target.get_path():
                return
            path = target.get_path()
            run_async(lambda: self.session.download(entry["rel"], path),
                      lambda r, e: self.window.toast(
                          str(e) if e else _("srv_downloaded", p=path)))

        dialog.save(self.window, None, chosen)

    def _remove(self, entry):
        session = self.session

        def done(record, error):
            if error:
                self.window.toast(str(error))
                return
            self._undo.append(record)
            self.undo_btn.set_sensitive(True)
            toast = Adw.Toast(title=_("srv_removed", n=1), timeout=6)
            toast.set_button_label(_("repo_undo"))
            toast.connect("button-clicked", lambda *a: self._undo_last())
            self.window.toaster.add_toast(toast)
            if entry["name"].endswith(".deb"):
                self.stale_banner.set_revealed(True)
            self.refresh()

        run_async(lambda: session.trash([entry["rel"]]), done)

    def _undo_last(self):
        if not self._undo or not self.session:
            return
        record = self._undo.pop()
        self.undo_btn.set_sensitive(bool(self._undo))
        run_async(lambda: self.session.restore(record),
                  lambda r, e: (self.window.toast(
                      str(e) if e else _("repo_restored", n=len(record))),
                      self.refresh()))

    def _rebuild(self):
        if not self.session:
            return
        profile = self.profile
        if not profile["key"]:
            self.window.toast(_("repo_key_hint"))
            return
        self.rebuild_btn.set_sensitive(False)
        self.window.begin_operation(_("srv_rebuilding"))

        def done(_r, error):
            self.window.end_operation()
            self.rebuild_btn.set_sensitive(True)
            if isinstance(error, remote.NotConnected):
                self._lost()
                return
            if error:
                self.window.toast(_("repo_build_failed", e=str(error)))
                return
            self.stale_banner.set_revealed(False)
            self.window.toast(_("srv_rebuilt"))
            self.refresh()

        run_async(lambda: self.session.rebuild_index(profile), done)

    # -- ending --------------------------------------------------------------------------
    def _lost(self):
        self.session = None
        self.stack.set_visible_child_name("setup")
        self.window.toast(_("srv_closed"))

    def disconnect(self):
        session, self.session = self.session, None
        self.stack.set_visible_child_name("setup")
        if session:
            run_async(session.disconnect, lambda r, e: None)

    def stop(self):
        """Window closing or project switching: close the connection now,
        rather than leaving it to time out on its own."""
        if self.session:
            try:
                self.session.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.session = None


# --------------------------------------------------------------------------- #
# Live Server — the site itself, served locally and shown in the window
# --------------------------------------------------------------------------- #
from . import liveserve, stats  # noqa: E402


FS_ANIM_MS = 340


def _fade(widget, start, end, duration, steps=17):
    """Ramp a widget's opacity, since GTK4 has no property animation.

    Deliberately small and self-contained: one timeout that walks the
    opacity and stops, with nothing to cancel or keep alive afterwards.
    """
    if widget is None:
        return
    widget.set_opacity(start)
    state = {"i": 0}

    def tick():
        state["i"] += 1
        fraction = state["i"] / steps
        if fraction >= 1.0:
            widget.set_opacity(end)
            return GLib.SOURCE_REMOVE
        # ease-out, so it settles rather than stopping dead
        eased = 1 - (1 - fraction) ** 3
        widget.set_opacity(start + (end - start) * eased)
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(max(1, duration // steps), tick)


class LiveServerPage(Gtk.Box):
    """The web equivalent of the Sandbox page.

    The point of this page is the site, not the server, so the site is what
    it shows: a browser view filling the page, with a thin bar above it. The
    server's own log is of no interest while it is working — when something
    goes wrong the browser says so in the page itself, which is where a web
    developer would look anyway.

    F11 fills the window with the site and hides everything else; Escape
    brings the rest back.
    """

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.proc = None
        self.port = 0
        self.url = ""
        self.fullscreen = False
        self.view = None
        self.fs_window = None
        self.fs_revealer = None
        self._fs_closing = False

        # The site arrives from the right rather than appearing in place:
        # a page that slides in reads as "this is the thing you asked for",
        # where a sudden swap reads as the window having glitched.
        self.stack = Gtk.Stack(
            vexpand=True,
            transition_type=Gtk.StackTransitionType.SLIDE_LEFT,
            transition_duration=320)
        self.append(self.stack)

        # -- idle ---------------------------------------------------------------
        start = Adw.StatusPage(icon_name="network-server-symbolic",
                               title=_("live_server"),
                               description=_("live_server_hint"),
                               vexpand=True)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      halign=Gtk.Align.CENTER)

        self.lan_switch = Adw.SwitchRow(title=_("live_server_lan"),
                                        subtitle=_("live_server_lan_hint"))
        lan_frame = Gtk.ListBox(css_classes=["boxed-list"],
                                selection_mode=Gtk.SelectionMode.NONE,
                                width_request=360)
        lan_frame.append(self.lan_switch)
        col.append(lan_frame)

        start_btn = Gtk.Button(label=_("start_live_server"),
                               halign=Gtk.Align.CENTER,
                               css_classes=["suggested-action", "pill"])
        start_btn.connect("clicked", lambda *a: self._start())
        col.append(start_btn)

        if not HAVE_WEBKIT:
            col.append(Gtk.Label(
                label=_("live_server_no_webkit"),
                wrap=True, justify=Gtk.Justification.CENTER,
                max_width_chars=44, css_classes=["dim-label", "caption"]))
        start.set_child(col)
        self.stack.add_named(start, "idle")

        # -- running -------------------------------------------------------------
        run_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6,
                           margin_start=10, margin_end=10)

        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                tooltip_text=_("live_server_reload"),
                                css_classes=["flat"])
        reload_btn.connect("clicked", lambda *a: self._reload())
        self.bar.append(reload_btn)

        self.url_label = Gtk.Label(xalign=0, hexpand=True, selectable=True,
                                   ellipsize=Pango.EllipsizeMode.MIDDLE,
                                   css_classes=["dim-label", "monospace",
                                                "caption"])
        self.bar.append(self.url_label)

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                              tooltip_text=_("repo_copy_path"),
                              css_classes=["flat"])
        copy_btn.connect("clicked", lambda *a: self._copy_url())
        self.bar.append(copy_btn)

        external_btn = Gtk.Button(icon_name="web-browser-symbolic",
                                  tooltip_text=_("live_server_open_browser"),
                                  css_classes=["flat"])
        external_btn.connect("clicked", lambda *a: self._open_external())
        self.bar.append(external_btn)

        full_btn = Gtk.Button(icon_name="view-fullscreen-symbolic",
                              tooltip_text=_("live_server_fullscreen"),
                              css_classes=["flat"])
        full_btn.connect("clicked", lambda *a: self._toggle_fullscreen())
        self.bar.append(full_btn)

        stop_btn = Gtk.Button(label=_("stop_live_server"),
                              css_classes=["destructive-action"])
        stop_btn.connect("clicked", lambda *a: self._stop())
        self.bar.append(stop_btn)
        run_box.append(self.bar)

        self.lan_label = Gtk.Label(xalign=0, visible=False, selectable=True,
                                   margin_start=10, margin_bottom=6,
                                   css_classes=["dim-label", "caption"])
        run_box.append(self.lan_label)

        self.view_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   vexpand=True)
        run_box.append(self.view_holder)
        self.stack.add_named(run_box, "running")

        self.stack.set_visible_child_name("idle")

        # F11 and Escape are handled in the capture phase so the browser
        # view — which happily consumes ordinary key presses — never sees
        # them first and swallows the way back out of fullscreen.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # -- start ------------------------------------------------------------------
    def _start(self):
        project = config.active_project()
        if not project:
            self.window.toast(_("no_project_title"))
            return

        root = stats.find_web_root(project["path"])
        host = "0.0.0.0" if self.lan_switch.get_active() else "127.0.0.1"
        self.port = liveserve.find_free_port(liveserve.DEFAULT_PORT, host)
        self.url = f"http://127.0.0.1:{self.port}/"
        self.url_label.set_text(self.url)

        self.lan_label.set_visible(False)
        if host == "0.0.0.0":
            lan_ip = liveserve.lan_address()
            if lan_ip:
                self.lan_label.set_text(_(
                    "live_server_lan_url", u=f"http://{lan_ip}:{self.port}/"))
                self.lan_label.set_visible(True)

        import subprocess
        import sys
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "petacore.liveserve", root,
                 "--port", str(self.port), "--host", host],
                cwd=root, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError as e:
            self.window.toast(str(e))
            return

        for child in list(self.view_holder):
            self.view_holder.remove(child)

        if HAVE_WEBKIT:
            self.view = WebKit.WebView(vexpand=True, hexpand=True)
            self.view_holder.append(self.view)
            # The server needs a moment to bind before the first request;
            # loading immediately shows a connection error the user would
            # have to clear by hand.
            GLib.timeout_add(350, self._load_once)
        else:
            self.view = None
            fallback = Adw.StatusPage(
                icon_name="web-browser-symbolic",
                title=_("live_server_no_webkit_title"),
                description=_("live_server_no_webkit"), vexpand=True)
            open_btn = Gtk.Button(label=_("live_server_open_browser"),
                                  halign=Gtk.Align.CENTER,
                                  css_classes=["suggested-action", "pill"])
            open_btn.connect("clicked", lambda *a: self._open_external())
            fallback.set_child(open_btn)
            self.view_holder.append(fallback)

        self.stack.set_visible_child_name("running")
        self.window.toast(_("live_server_started", root=os.path.relpath(
            root, project["path"]) or "."))

    def _load_once(self):
        if self.view is not None and self.url:
            self.view.load_uri(self.url)
        return GLib.SOURCE_REMOVE

    # -- controls ----------------------------------------------------------------
    def _reload(self):
        if self.view is not None:
            self.view.reload()

    def _copy_url(self):
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display and self.url:
            display.get_clipboard().set(self.url)
            self.window.toast(_("repo_copied"))

    def _open_external(self):
        if self.url:
            Gio.AppInfo.launch_default_for_uri(self.url, None)

    # -- fullscreen ---------------------------------------------------------------
    # The site goes full screen, not the application window. Fullscreening
    # the main window would still leave the sidebar and the header bar
    # around the page, so instead the browser view is moved into a window
    # of its own with nothing else in it — the same view, so the page keeps
    # its state and does not reload.
    def _on_key(self, _controller, keyval, _code, _state):
        from gi.repository import Gdk
        if self.stack.get_visible_child_name() != "running":
            return False
        if keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
            return True
        return False

    def _toggle_fullscreen(self):
        if self.stack.get_visible_child_name() != "running":
            return
        if self.fullscreen:
            self._leave_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if self.view is None:
            # Nothing to show full screen without the embedded browser;
            # say so rather than opening an empty black window.
            self.window.toast(_("live_server_no_webkit_title"))
            return

        self.view_holder.remove(self.view)
        # SLIDE_RIGHT moves the child rightwards as it is revealed, so the
        # site enters from the left edge. Leaving plays the same animation
        # backwards, which is why Escape returns it the way it came.
        self.fs_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
            transition_duration=FS_ANIM_MS, reveal_child=False,
            hexpand=True, vexpand=True)
        self.fs_revealer.set_child(self.view)

        self.fs_window = Gtk.Window(transient_for=self.window, modal=False,
                                    title=self.url)
        self.fs_window.set_child(self.fs_revealer)
        self.fs_window.fullscreen()

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_fs_key)
        self.fs_window.add_controller(keys)
        self.fs_window.connect("close-request", self._on_fs_closed)

        self.fullscreen = True
        self.fs_window.present()
        # Revealed a frame after the window is up: asking for the animation
        # before the compositor has shown the window plays it to nobody.
        GLib.timeout_add(60, self._reveal_fullscreen)
        self.window.toast(_("live_server_fullscreen_hint"))

    def _reveal_fullscreen(self):
        if getattr(self, "fs_revealer", None) is not None:
            self.fs_revealer.set_reveal_child(True)
            # GTK cannot scale a live browser view, so the sense of the
            # page growing into the screen comes from fading it up as it
            # slides — the nearest honest equivalent.
            _fade(self.view, 0.35, 1.0, FS_ANIM_MS)
        return GLib.SOURCE_REMOVE

    def _on_fs_key(self, _controller, keyval, _code, _state):
        from gi.repository import Gdk
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_F11):
            self._leave_fullscreen()
            return True
        return False

    def _on_fs_closed(self, *_a):
        # The window manager closed it (Alt+F4, the compositor) rather than
        # Escape — put the view back all the same, or the page would come
        # back empty.
        self._leave_fullscreen()
        return True

    def _leave_fullscreen(self, animate=True):
        """Send the site back the way it arrived, then put it in the page.

        `animate=False` is for teardown — closing the window or switching
        project — where waiting out an animation on a widget that is about
        to be destroyed would be a crash waiting to happen.
        """
        window = getattr(self, "fs_window", None)
        revealer = getattr(self, "fs_revealer", None)
        if window is None or self._fs_closing:
            return
        self.fullscreen = False

        if not animate:
            self._finish_leave_fullscreen(window, revealer)
            return

        self._fs_closing = True
        revealer.set_reveal_child(False)          # slides back out leftwards
        _fade(self.view, 1.0, 0.35, FS_ANIM_MS)
        GLib.timeout_add(
            FS_ANIM_MS + 40,
            lambda: self._finish_leave_fullscreen(window, revealer))

    def _finish_leave_fullscreen(self, window, revealer):
        self._fs_closing = False
        self.fs_window = None
        self.fs_revealer = None
        if self.view is not None and revealer is not None \
                and self.view.get_parent() is revealer:
            revealer.set_child(None)
            self.view.set_opacity(1.0)
            self.view_holder.append(self.view)
        window.destroy()
        return GLib.SOURCE_REMOVE

    # -- stop -------------------------------------------------------------------
    def _stop(self):
        if self.fullscreen:
            self._leave_fullscreen(animate=False)
        proc = self.proc
        self.proc = None
        self.view = None
        for child in list(self.view_holder):
            self.view_holder.remove(child)
        self.stack.set_visible_child_name("idle")
        if proc:
            run_async(lambda: _terminate(proc), lambda r, e: None)

    def stop(self):
        """Called on project switch and on window close."""
        if self.fullscreen:
            self._leave_fullscreen(animate=False)
        if self.proc:
            _terminate(self.proc)
            self.proc = None

    end_all = stop


def _terminate(proc):
    """Stop the server, and don't leave a zombie behind if it ignores us."""
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:  # noqa: BLE001 - already going away
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Overview — the project at a glance, on a soft dark gradient
# --------------------------------------------------------------------------- #
_PAINT_PROVIDERS = []


def _paint(widget, colour, radius=0):
    """Give a widget a solid colour without a drawing context.

    Cairo is an optional dependency on some systems, and a missing
    python3-cairo used to fill the log with draw-callback errors — plain CSS
    has no such requirement.
    """
    css = (f"* {{ background-color: {colour};"
           f" border-radius: {radius}px; }}").encode()
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    widget.get_style_context().add_provider(
        provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _PAINT_PROVIDERS.append(provider)      # keep it alive


OS_ICONS = {"Linux": "os-linux", "Windows": "os-windows",
            "Web": "os-web",
            "macOS": "os-apple", "Cross-platform": "os-cross"}


def _asset(name, size=24):
    """A vector icon from brand-icons, sized for the interface."""
    path = os.path.join(os.path.dirname(__file__), "brand-icons",
                        name + ".svg")
    if os.path.isfile(path):
        image = Gtk.Image.new_from_file(path)
        image.set_pixel_size(size)
        return image
    return Gtk.Image(icon_name="image-missing-symbolic", pixel_size=size)


class OverviewPage(Gtk.Box):
    """Project identity, what it is made of, and the shareable card."""

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self._data = None
        self.add_css_class("petacore-overview")

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26,
                            margin_top=48, margin_bottom=48,
                            margin_start=40, margin_end=40,
                            halign=Gtk.Align.CENTER,
                            hexpand=True)
        self.body.set_size_request(720, -1)

        scroller = _scrolled(self.body)
        scroller.add_css_class("petacore-overview")
        self.append(scroller)
        self.refresh()

    # -- helpers ---------------------------------------------------------------
    def _clear(self):
        while (child := self.body.get_first_child()) is not None:
            self.body.remove(child)

    @staticmethod
    def _card(icon_name, value, label):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       css_classes=["petacore-card"], hexpand=True)
        top = Gtk.Box(spacing=10)
        top.append(_asset(icon_name, 20))
        top.append(Gtk.Label(label=value, xalign=0,
                             css_classes=["petacore-card-value"]))
        card.append(top)
        card.append(Gtk.Label(label=label, xalign=0,
                              css_classes=["petacore-card-label"]))
        return card

    # -- build ------------------------------------------------------------------
    def refresh(self):
        from . import stats
        project = config.active_project()
        self._clear()
        if not project:
            return
        placeholder = Gtk.Label(label=_("analysing"),
                                css_classes=["petacore-hero-sub"])
        self.body.append(placeholder)
        run_async(lambda: stats.analyse(project["path"]),
                  lambda data, error: self._render(project, data, error))

    def _render(self, project, data, error):
        from . import sharecard
        self._clear()
        if error or not data:
            self.body.append(Gtk.Label(label=str(error or ""),
                                       css_classes=["petacore-hero-sub"]))
            return
        self._data = data

        # -- hero -------------------------------------------------------------
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       halign=Gtk.Align.CENTER)
        logo_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "icons", "128x128", "io.petacore.Petacore.png")
        if os.path.isfile(logo_path):
            logo = Gtk.Image.new_from_file(logo_path)
            logo.set_pixel_size(72)
            logo.set_halign(Gtk.Align.CENTER)
            hero.append(logo)
        hero.append(Gtk.Label(label=project["name"],
                              css_classes=["petacore-hero-strong"],
                              halign=Gtk.Align.CENTER))
        hero.append(Gtk.Label(
            label=_("stat_platform") + ": " + data["platform"],
            css_classes=["petacore-hero-sub"], halign=Gtk.Align.CENTER))
        self.body.append(hero)

        # -- platform chips, one box per system --------------------------------
        chips = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        for name in [part.strip() for part in data["platform"].split("/")]:
            chip = Gtk.Box(spacing=10, css_classes=["petacore-chip"])
            chip.append(_asset(OS_ICONS.get(name, "os-cross"), 22))
            chip.append(Gtk.Label(label=name,
                                  css_classes=["petacore-chip-label"]))
            chips.append(chip)
        self.body.append(chips)

        # -- stat cards ---------------------------------------------------------
        grid = Gtk.Grid(column_spacing=14, row_spacing=14,
                        column_homogeneous=True)
        cards = [
            ("stat-lines", f'{data["lines"]:,}'.replace(",", " "),
             _("stat_lines")),
            ("stat-chars", stats_human(data["characters"]), _("stat_chars")),
            ("stat-files", str(data["files"]), _("stat_files")),
        ]
        for index, (icon, value, label) in enumerate(cards):
            grid.attach(self._card(icon, value, label),
                        index % 3, index // 3, 1, 1)
        self.body.append(grid)

        # -- language mix --------------------------------------------------------
        mix = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      css_classes=["petacore-card"])
        mix.append(Gtk.Label(label=_("language_mix").upper(), xalign=0,
                             css_classes=["petacore-section"]))
        stack_bar = Gtk.Box(spacing=2, height_request=12)
        for index, item in enumerate(data["languages"][:8]):
            colour = sharecard.colour_for(item["language"], index)
            segment = Gtk.Box(hexpand=True)
            segment.set_size_request(max(4, int(item["percent"] * 4)), 12)
            _paint(segment, colour, radius=6)
            stack_bar.append(segment)
        mix.append(stack_bar)

        legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                             max_children_per_line=3, row_spacing=6,
                             column_spacing=18)
        for index, item in enumerate(data["languages"][:8]):
            entry = Gtk.Box(spacing=8)
            dot = Gtk.Box()
            dot.set_size_request(12, 12)
            dot.set_valign(Gtk.Align.CENTER)
            _paint(dot, sharecard.colour_for(item["language"], index),
                   radius=6)
            entry.append(dot)
            entry.append(Gtk.Label(
                label=f'{item["language"]}  {item["percent"]}%',
                css_classes=["petacore-card-label"]))
            legend.append(entry)
        mix.append(legend)
        self.body.append(mix)

        # -- share --------------------------------------------------------------
        share = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        halign=Gtk.Align.CENTER)
        share.append(Gtk.Label(label=_("share_hint"),
                               css_classes=["petacore-hero-sub"],
                               halign=Gtk.Align.CENTER))
        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        for shape, key in (("story", "shape_story"),
                           ("square", "shape_square"),
                           ("wide", "shape_wide")):
            button = Gtk.Button(label=_(key), css_classes=["pill"])
            if shape == "story":
                button.add_css_class("suggested-action")
            button.connect("clicked", self._on_share, shape)
            buttons.append(button)
        share.append(buttons)
        self.body.append(share)

    # -- drawing helpers ---------------------------------------------------------
    # -- export -------------------------------------------------------------------
    def _on_share(self, _btn, shape):
        project = config.active_project()
        if not project or not self._data:
            return
        # The project's own name may be an internal codename, so it is shown
        # and editable before anything is written — the card is meant to be
        # posted publicly.
        ask = Adw.MessageDialog(transient_for=self.window,
                                heading=_("share_card"),
                                body=_("card_name_hint"))
        entry = Adw.EntryRow(title=_("card_name"), text=project["name"])
        holder = Gtk.ListBox(css_classes=["boxed-list"],
                             selection_mode=Gtk.SelectionMode.NONE)
        holder.append(entry)
        ask.set_extra_child(holder)
        ask.add_response("cancel", _("cancel"))
        ask.add_response("go", _("share_card"))
        ask.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        ask.set_default_response("go")
        ask.connect("response", lambda _d, r: self._choose_card_file(
            shape, entry.get_text().strip() or project["name"])
            if r == "go" else None)
        ask.present()

    def _choose_card_file(self, shape, display_name):
        self._card_name = display_name
        dialog = Gtk.FileDialog(
            title=_("share_card"),
            initial_name=f'{display_name}-{shape}.png')
        png_filter = Gtk.FileFilter()
        png_filter.set_name("PNG")
        png_filter.add_pattern("*.png")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(png_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(png_filter)
        dialog.save(self.window, None, self._save_card, shape)

    def _save_card(self, dialog, result, shape):
        from . import sharecard
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        if not gfile:
            return
        path = gfile.get_path()
        project = config.active_project()
        display_name = getattr(self, "_card_name", None) or project["name"]
        icon = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "icons", "256x256", "io.petacore.Petacore.png")

        def work():
            return sharecard.write_png(
                path, display_name, self._data, shape,
                logo_href=icon if os.path.isfile(icon) else None)

        def done(result, error):
            if isinstance(error, sharecard.PngUnavailable):
                self.window.toast(_("png_unavailable"))
            elif error:
                self.window.toast(str(error))
            else:
                self.window.toast(
                    _("card_saved", p=os.path.basename(result)))

        run_async(work, done)


def stats_human(value):
    from . import stats as _stats
    return _stats.human_count(value)
