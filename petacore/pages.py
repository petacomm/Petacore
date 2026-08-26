"""The five content pages of the Petacore main window."""

import os
import re
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
                           and not is_encrypted_artifact(d, True)]
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
            if e.name in HIDDEN or e.name.startswith("."):
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
