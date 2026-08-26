"""Petacore main window: GNOME headerbar + sidebar navigation."""

import os
import shutil

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from . import github, gitops, sandbox  # noqa: E402
from .config import config  # noqa: E402
from .dialogs import (EditProjectDialog, GitHubLoginDialog,  # noqa: E402
                      LanguageDialog, NewProjectDialog, PreferencesDialog)
from .i18n import translator as _  # noqa: E402
from .pages import (EditorPage, KeysPage, OverviewPage,  # noqa: E402
                    PackagePage, ProjectPage, SandboxPage, SnapshotsPage,
                    TerminalPage, UpdatesPage)
from .snapshots import SnapshotManager  # noqa: E402
from .util import run_async  # noqa: E402

PAGES = [
    ("overview_page", "view-grid-symbolic"),
    ("project",   "folder-symbolic"),
    ("editor",    "accessories-text-editor-symbolic"),
    ("snapshots", "document-save-symbolic"),
    ("terminal",  "utilities-terminal-symbolic"),
    ("sandbox",   "application-x-addon-symbolic"),
    ("package",   "package-x-generic-symbolic"),
    ("keys_page", "channel-secure-symbolic"),
    ("updates",   "view-list-ordered-symbolic"),
]


class _ProgressRing(Gtk.DrawingArea):
    """A small circle that fills as work proceeds.

    Before any percentage is known it turns slowly, so the user can see that
    something is happening even when the total is unknown. Drawing is guarded
    because python3-cairo is an optional package on some systems; without it
    the ring simply stays blank rather than raising on every frame.
    """

    SIZE = 16

    def __init__(self):
        super().__init__()
        self.set_size_request(self.SIZE, self.SIZE)
        self.set_valign(Gtk.Align.CENTER)
        self._fraction = None
        self._angle = 0.0
        self._tick = None
        self.set_draw_func(self._draw)

    def set_fraction(self, fraction):
        self._fraction = fraction
        if fraction is None:
            self._start_spin()
        else:
            self._stop_spin()
        self.queue_draw()

    def stop(self):
        self._stop_spin()

    def _start_spin(self):
        if self._tick is None:
            self._tick = self.add_tick_callback(self._advance)

    def _stop_spin(self):
        if self._tick is not None:
            self.remove_tick_callback(self._tick)
            self._tick = None

    def _advance(self, _widget, _clock):
        self._angle = (self._angle + 0.09) % 6.2832
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, _area, cr, width, height):
        try:
            import math
            colour = self.get_color()
            radius = min(width, height) / 2 - 1.5
            cx, cy = width / 2, height / 2

            cr.set_line_width(2.4)
            cr.set_source_rgba(colour.red, colour.green, colour.blue, 0.25)
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.stroke()

            cr.set_source_rgba(colour.red, colour.green, colour.blue, 0.95)
            if self._fraction is None:
                start = self._angle
                cr.arc(cx, cy, radius, start, start + 1.6)
            else:
                start = -math.pi / 2
                cr.arc(cx, cy, radius, start,
                       start + 2 * math.pi * max(0.0, min(1.0,
                                                          self._fraction)))
            cr.stroke()
        except Exception:
            return


class PetacoreWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Petacore",
                         default_width=1100, default_height=720)
        self._autosave_id = None
        _.connect_changed(self._rebuild)
        # keep the padlock readable when the light/dark scheme changes
        Adw.StyleManager.get_default().connect(
            "notify::dark", lambda *a: self._swap_lock_icon())
        self.connect("close-request", self._on_close)
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self._on_window_key)
        self.add_controller(kc)
        self._build()
        self.restart_autosave()

        if config.get("first_run"):
            GLib.idle_add(self._show_first_run)

    # ------------------------------------------------------------------ UI --
    def _build(self):
        self.split = Adw.NavigationSplitView(min_sidebar_width=220,
                                             max_sidebar_width=260)

        # ---- Sidebar ---------------------------------------------------------
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle(title="Petacore"))

        new_btn = Gtk.Button(icon_name="list-add-symbolic",
                             tooltip_text=_("new_project"))
        new_btn.connect("clicked", lambda *a: self.show_new_project())
        sidebar_header.pack_start(new_btn)
        sidebar_toolbar.add_top_bar(sidebar_header)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Project selector
        projects = config.get("projects")
        if projects:
            from . import crypto
            names = [p["name"] for p in projects]
            self.project_drop = Gtk.DropDown.new_from_strings(names)
            paths = [p["path"] for p in projects]
            active = config.get("active_project")
            if active in paths:
                self.project_drop.set_selected(paths.index(active))
            self.project_drop.connect("notify::selected",
                                      self._on_project_selected, paths)
            drop_box = Gtk.Box(spacing=6, margin_top=12, margin_bottom=6,
                               margin_start=12, margin_end=12)
            self.project_drop.set_hexpand(True)
            drop_box.append(self.project_drop)

            # Vector padlock shown while the selected project is encrypted.
            self.lock_icon = self._lock_image()
            self.lock_icon.set_tooltip_text(_("enc_locked"))
            drop_box.append(self.lock_icon)
            self._refresh_lock()

            edit_btn = Gtk.Button(icon_name="document-edit-symbolic",
                                  tooltip_text=_("edit_project"),
                                  css_classes=["flat"],
                                  valign=Gtk.Align.CENTER)
            edit_btn.connect("clicked", self._on_edit_project)
            drop_box.append(edit_btn)
            sidebar_box.append(drop_box)

        self.nav_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for key, icon in PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8,
                          margin_start=6, margin_end=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=_(key), xalign=0))
            row.set_child(box)
            row.page_key = key
            self.nav_list.append(row)
        self.nav_list.connect("row-selected", self._on_nav)
        sidebar_box.append(self.nav_list)

        # Progress for long operations lives at the foot of the sidebar,
        # flush against it — the same place and shape a file manager uses,
        # so it reads as status rather than as something demanding attention.
        sidebar_box.append(self._build_operation_widget())
        sidebar_toolbar.set_content(sidebar_box)

        sidebar_page = Adw.NavigationPage(title="Petacore",
                                          child=sidebar_toolbar)
        self.split.set_sidebar(sidebar_page)

        # ---- Content ---------------------------------------------------------
        content_toolbar = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar()
        self.content_title = Adw.WindowTitle(title=_("project"))
        self.content_header.set_title_widget(self.content_title)

        # Two separate destinations, side by side where the old single Sync
        # button used to be: code to GitHub, files to Google Drive.
        from .dialogs import brand_image
        sync_box = Gtk.Box(spacing=4, css_classes=["linked"])

        self.sync_btn = Gtk.Button(tooltip_text=_("sync_github"))
        gh_icon = brand_image("github", 16,
                              fallback_icon="emblem-synchronizing-symbolic")
        self.sync_btn.set_child(gh_icon)
        self.sync_btn.connect("clicked", lambda *a: self.start_sync())
        sync_box.append(self.sync_btn)

        self.drive_sync_btn = Gtk.Button(tooltip_text=_("sync_drive"))
        gd_icon = brand_image("gdrive", 16,
                              fallback_icon="folder-remote-symbolic")
        self.drive_sync_btn.set_child(gd_icon)
        self.drive_sync_btn.connect("clicked",
                                    lambda *a: self.start_drive_sync())
        sync_box.append(self.drive_sync_btn)

        self.content_header.pack_start(sync_box)

        menu = Gio.Menu()
        menu.append(_("new_project"), "app.new-project")
        menu.append(_("settings"), "app.preferences")
        menu.append(_("about"), "app.about")
        menu.append(_("quit"), "app.quit")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                  menu_model=menu)
        self.content_header.pack_end(menu_btn)
        content_toolbar.add_top_bar(self.content_header)

        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.pages = {}

        if config.active_project():
            self.pages["overview_page"] = OverviewPage(self)
            self.pages["project"] = ProjectPage(self)
            self.pages["editor"] = EditorPage(self)
            self.pages["snapshots"] = SnapshotsPage(self)
            self.pages["terminal"] = TerminalPage(self)
            self.pages["sandbox"] = SandboxPage(self)
            self.pages["package"] = PackagePage(self)
            self.pages["keys_page"] = KeysPage(self)
            self.pages["updates"] = UpdatesPage(self)
            for key, page in self.pages.items():
                self.stack.add_named(page, key)
        else:
            status = Adw.StatusPage(icon_name="folder-symbolic",
                                    title=_("no_project_title"),
                                    description=_("no_project_body"))
            btn = Gtk.Button(label=_("new_project"), halign=Gtk.Align.CENTER,
                             css_classes=["suggested-action", "pill"])
            btn.connect("clicked", lambda *a: self.show_new_project())
            status.set_child(btn)
            self.stack.add_named(status, "empty")
            self.sync_btn.set_sensitive(False)

        content_toolbar.set_content(self.stack)
        content_page = Adw.NavigationPage(title=_("project"),
                                          child=content_toolbar)
        self.split.set_content(content_page)

        # Toast overlay wraps everything
        self.toaster = Adw.ToastOverlay()
        self.toaster.set_child(self.split)
        # An overlay above everything, used by the Ctrl+Esc save animation.
        self.anim_overlay = Gtk.Overlay(child=self.toaster)
        self.set_content(self.anim_overlay)

        first = self.nav_list.get_row_at_index(0)
        if first:
            self.nav_list.select_row(first)

    def _lock_image(self):
        """The bundled vector padlock. GTK does not recolour SVGs loaded from
        a file, so the white variant is used on dark themes and the dark-ink
        one on light themes; otherwise it would vanish into the background."""
        dark = Adw.StyleManager.get_default().get_dark()
        name = "lock-white.svg" if dark else "lock.svg"
        path = os.path.join(os.path.dirname(__file__), "brand-icons", name)
        if os.path.isfile(path):
            image = Gtk.Image.new_from_file(path)
            image.set_pixel_size(16)
        else:
            image = Gtk.Image(icon_name="channel-secure-symbolic")
        image.set_valign(Gtk.Align.CENTER)
        return image

    def animate_theme_change(self, apply_theme):
        """Cross-dissolve between light and dark instead of snapping.

        A veil in the *incoming* background colour eases in, the new scheme is
        applied behind it, then the veil eases out — so the change reads as
        one soft dissolve rather than a hard flash. Timings and the
        ease-in-out curve are chosen to feel gentle rather than snappy.
        """
        going_dark = self._incoming_is_dark()
        veil = Gtk.Box(can_target=False,
                       css_classes=["petacore-veil-dark" if going_dark
                                    else "petacore-veil-light"])
        veil.set_opacity(0.0)
        self.anim_overlay.add_overlay(veil)

        def fade(widget, start, end, duration, done=None):
            target = Adw.CallbackAnimationTarget.new(
                lambda value: widget.set_opacity(value))
            anim = Adw.TimedAnimation.new(widget, start, end, duration, target)
            anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
            if done is not None:
                anim.connect("done", lambda *_a: done())
            anim.play()
            return anim

        # Any dialog sitting on top (Settings, for instance) is its own
        # toplevel, so it would otherwise snap to the new colours while the
        # window behind it dissolves. Each one gets the same veil, driven off
        # the same clock, so the whole screen moves as one.
        others = []
        for window in Gtk.Window.list_toplevels():
            if window is self or not window.get_mapped():
                continue
            try:
                content = window.get_content() \
                    if hasattr(window, "get_content") else window.get_child()
                if content is None or isinstance(content, Gtk.Overlay):
                    overlay = content
                else:
                    overlay = Gtk.Overlay()
                    if hasattr(window, "set_content"):
                        window.set_content(overlay)
                    else:
                        window.set_child(overlay)
                    overlay.set_child(content)
                if overlay is None:
                    continue
                sub_veil = Gtk.Box(
                    can_target=False,
                    css_classes=["petacore-veil-dark" if going_dark
                                 else "petacore-veil-light"])
                sub_veil.set_opacity(0.0)
                overlay.add_overlay(sub_veil)
                others.append((overlay, sub_veil))
            except Exception:
                continue          # a dialog we can't veil simply won't animate

        def after_veil():
            apply_theme()
            self._swap_lock_icon()

            def cleanup():
                self.anim_overlay.remove_overlay(veil)

            # slightly longer on the way out: the reveal is the part the eye
            # follows, so it gets the gentler half of the motion
            self._veil_out = fade(veil, 1.0, 0.0, 320, cleanup)
            self._other_out = [
                fade(sub, 1.0, 0.0, 320,
                     (lambda o=overlay, s=sub: o.remove_overlay(s)))
                for overlay, sub in others]

        self._veil_in = fade(veil, 0.0, 1.0, 190, after_veil)
        self._other_in = [fade(sub, 0.0, 1.0, 190)
                          for _overlay, sub in others]

    @staticmethod
    def _incoming_is_dark():
        theme = config.get("theme")
        if theme == "dark":
            return True
        if theme == "light":
            return False
        return Adw.StyleManager.get_default().get_system_supports_color_schemes() \
            and Adw.StyleManager.get_default().get_dark()

    def _swap_lock_icon(self):
        if not hasattr(self, "lock_icon"):
            return
        parent = self.lock_icon.get_parent()
        if parent is None:
            return
        new_icon = self._lock_image()
        new_icon.set_tooltip_text(_("enc_locked"))
        parent.insert_child_after(new_icon, self.lock_icon)
        parent.remove(self.lock_icon)
        self.lock_icon = new_icon
        self._refresh_lock()

    def _refresh_lock(self):
        from . import crypto
        project = config.active_project()
        if hasattr(self, "lock_icon"):
            self.lock_icon.set_visible(bool(project)
                                       and crypto.is_encrypted(project))

    def _rebuild(self):
        """Called when the UI language changes — rebuild everything."""
        self._build()

    def _on_window_key(self, _ctrl, keyval, _code, state):
        from gi.repository import Gdk
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        name = (Gdk.keyval_name(keyval) or "").lower()
        on_project = (self.stack.get_visible_child_name() == "project"
                      and "project" in self.pages)
        if name == "z" and on_project:
            self.pages["project"].undo_last()
            return True
        if name == "a" and on_project:
            self.pages["project"].listbox.select_all()
            self.pages["project"].listbox.grab_focus()
            return True
        if name == "f" and on_project:
            self.pages["project"].search_entry.grab_focus()
            return True
        if name == "c" and on_project:
            self.pages["project"]._copy_selection()
            return True
        if name == "v" and on_project:
            self.pages["project"]._paste_clipboard()
            return True
        return False

    def _on_close(self, *_args):
        """No sandbox ever outlives Petacore: kill and wipe them all."""
        sandbox.destroy_all()
        return False

    # ------------------------------------------------------------ navigation --
    def _on_nav(self, _list, row):
        if not row:
            return
        key = row.page_key
        self.content_title.set_title(_(key))
        if key in self.pages:
            self.stack.set_visible_child_name(key)
            page = self.pages[key]
            if key in ("overview_page", "project", "snapshots",
                       "keys_page", "updates"):
                page.refresh()
            elif key == "package":
                page._refresh_key()
            elif key == "terminal":
                page.spawn()

    def _on_project_selected(self, drop, _pspec, paths):
        idx = drop.get_selected()
        if 0 <= idx < len(paths):
            config.set("active_project", paths[idx])
            self._refresh_lock()
            self._build()
            self.restart_autosave()

    def refresh_pages(self):
        if "project" in self.pages:
            self.pages["project"].refresh()

    def select_page(self, key):
        for i in range(len(PAGES)):
            row = self.nav_list.get_row_at_index(i)
            if row and getattr(row, "page_key", None) == key:
                self.nav_list.select_row(row)
                return

    def open_file(self, path):
        """Open a file in the built-in editor (called by the Project page)."""
        if "editor" in self.pages:
            self.select_page("editor")
            self.pages["editor"].open_file(path)

    # -- Deploy: sync a saved file to the installed application ---------------
    def prompt_deploy(self, file_path):
        project = config.active_project()
        if not project:
            return
        dialog = Adw.MessageDialog(transient_for=self,
                                   heading=_("deploy_question"),
                                   body=_("deploy_detail"))
        dialog.add_response("no", _("no"))
        dialog.add_response("yes", _("yes"))
        dialog.set_response_appearance("yes",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("yes")

        def responded(_d, response):
            if response != "yes":
                return
            deploy_path = project.get("deploy_path", "")
            if deploy_path and os.path.isdir(deploy_path):
                self._do_deploy(project, file_path, deploy_path)
            else:
                fd = Gtk.FileDialog(title=_("deploy_choose"))
                fd.select_folder(self, None, self._deploy_folder_chosen,
                                 project, file_path)

        dialog.connect("response", responded)
        dialog.present()

    def _deploy_folder_chosen(self, dialog, result, project, file_path):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if not folder:
            return
        deploy_path = folder.get_path()
        config.set_project_field(project["path"], "deploy_path", deploy_path)
        project["deploy_path"] = deploy_path
        self._do_deploy(project, file_path, deploy_path)

    def _do_deploy(self, project, file_path, deploy_path):
        def work():
            rel = os.path.relpath(os.path.realpath(file_path),
                                  os.path.realpath(project["path"]))
            if rel.startswith(".."):
                raise ValueError(rel)
            dest = os.path.join(deploy_path, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(file_path, dest)
            return dest

        def done(dest, error):
            if error:
                self.toast(f"{_('deploy_failed')}: {error}")
            else:
                self.toast(_("deployed", p=dest))

        run_async(work, done)

    def ask_project_secret(self, project, on_ok):
        """Ask for an encrypted project's password and hand it to `on_ok`.
        The password is only held in memory for the transfer."""
        from . import crypto
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("enc_ask_secret", p=project["name"]),
            body=_("enc_unlock_body", p=project["name"]))
        entry = Adw.PasswordEntryRow(title=_("enc_secret"))
        group = Gtk.ListBox(css_classes=["boxed-list"],
                            selection_mode=Gtk.SelectionMode.NONE)
        group.append(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("ok", _("enc_unlock"))
        dialog.set_response_appearance("ok",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def responded(_d, response):
            if response != "ok":
                return
            secret = entry.get_text()
            expected = project.get("encrypt_check", "")
            if expected and not crypto.check(secret, expected):
                self.toast(_("enc_wrong"))
                return
            on_ok(secret)

        dialog.connect("response", responded)
        dialog.present()

    # -- long operations ---------------------------------------------------------
    def begin_operation(self, label: str):
        """Show a quiet indicator in the corner while something runs.

        Syncing can take a while, and a toast that appears once tells the
        user nothing about whether the work is still going. This sits in the
        bottom-left, out of the way, for as long as the operation lasts —
        the same place a file manager puts its copy progress.
        """
        if getattr(self, "_op_box", None) is None:
            self._build_operation_widget()
        self._op_label.set_text(label)
        self._op_ring.set_fraction(None)          # indeterminate until told
        self._op_box.set_visible(True)
        self._op_count = getattr(self, "_op_count", 0) + 1

    def update_operation(self, percent):
        """Called with 0-100 as work proceeds; ignored when unknown."""
        if getattr(self, "_op_box", None) is None or percent is None:
            return
        try:
            fraction = max(0.0, min(1.0, float(percent) / 100.0))
        except (TypeError, ValueError):
            return
        self._op_ring.set_fraction(fraction)

    def end_operation(self):
        """Hide the indicator once the last running operation finishes."""
        self._op_count = max(0, getattr(self, "_op_count", 1) - 1)
        if self._op_count or getattr(self, "_op_box", None) is None:
            return
        self._op_ring.stop()
        self._op_box.set_visible(False)

    def _build_operation_widget(self):
        box = Gtk.Box(spacing=10, css_classes=["petacore-operation"],
                      margin_start=14, margin_end=14,
                      margin_top=8, margin_bottom=12,
                      visible=False)
        self._op_ring = _ProgressRing()
        box.append(self._op_ring)
        self._op_label = Gtk.Label(xalign=0, hexpand=True,
                                   ellipsize=Pango.EllipsizeMode.END,
                                   css_classes=["petacore-operation-label"])
        box.append(self._op_label)
        self._op_box = box
        return box

    def toast(self, text: str):
        self.toaster.add_toast(Adw.Toast(title=text, timeout=4))

    def show_save_animation(self, filename: str):
        """A small floppy disk floats up the screen: it grows on its way to
        the middle, then shrinks and fades as it leaves the top."""
        from . import saveanim

        area = Gtk.DrawingArea(can_target=False, hexpand=True, vexpand=True)
        state = {"start": None}

        def draw(_area, cr, width, height):
            p = state.get("progress", 0.0)
            opacity = saveanim.alpha(p)
            if opacity <= 0:
                return
            size = saveanim.BASE_SIZE * saveanim.scale(p)
            cx = width / 2
            cy = saveanim.y_fraction(p) * height
            saveanim.draw_cairo(cr, cx, cy, size, opacity)

        area.set_draw_func(draw)
        self.anim_overlay.add_overlay(area)

        def tick(widget, clock):
            now = clock.get_frame_time()          # microseconds
            if state["start"] is None:
                state["start"] = now
            elapsed = (now - state["start"]) / 1000.0     # ms
            p = elapsed / saveanim.DURATION_MS
            if p >= 1.0:
                self.anim_overlay.remove_overlay(area)
                return GLib.SOURCE_REMOVE
            state["progress"] = p
            widget.queue_draw()
            return GLib.SOURCE_CONTINUE

        area.add_tick_callback(tick)

    # ----------------------------------------------------------------- flows --
    def _show_first_run(self):
        from .dialogs import SetupWizard
        SetupWizard(self, on_done=self._build).present()

    def show_new_project(self):
        NewProjectDialog(self, on_created=self._project_created).present()

    def _on_edit_project(self, _btn):
        project = config.active_project()
        if not project:
            return
        EditProjectDialog(self, project, on_done=self._project_edited).present()

    def _project_edited(self, message):
        self._build()
        self.restart_autosave()
        if message:
            self.toast(message)

    def _project_created(self):
        self._build()
        self.restart_autosave()

    def show_preferences(self):
        PreferencesDialog(self).present()

    def start_drive_sync(self):
        """Upload the project files to Drive, without touching GitHub."""
        if "project" in self.pages:
            self.select_page("project")
            self.pages["project"]._on_drive_sync(None)

    # -- Sync -----------------------------------------------------------------
    def start_sync(self):
        project = config.active_project()
        if not project:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("sync_question"),
            body=_("sync_detail"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("sync", _("sync"))
        dialog.set_response_appearance("sync",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._sync_confirmed, project)
        dialog.present()

    def _sync_confirmed(self, _dialog, response, project):
        if response != "sync":
            return
        self.sync_btn.set_sensitive(False)
        self.begin_operation(_("op_github"))

        def work():
            """Sync everything that belongs to this project: the code to
            GitHub, then the Next Updates plans and the public signing keys
            to Google Drive (when an account is connected)."""
            token = config.get("github_token")
            if not token:
                # Zero-friction path: reuse an existing GitHub CLI session.
                token = github.gh_token() or ""
                if token:
                    config.set("github_token", token)
                    try:
                        config.set("github_user",
                                   github.validate_token(token))
                    except github.GitHubError:
                        pass
            gitops.sync(project["path"], token)

            # Plans and keys have their own buttons now, so this stays a
            # pure code push and never surprises the user with extra uploads.
            return {}

        def done(result, error):
            self.sync_btn.set_sensitive(True)
            self.end_operation()
            if not error and isinstance(result, dict):
                if "plans" in result:
                    self.toast(_("sync_plans_done", n=result["plans"]))
                if "keys" in result:
                    self.toast(_("sync_keys_done", n=result["keys"]))
            if error:
                err = str(error)
                if err == "no-remote":
                    self.toast(_("no_remote"))
                elif any(s in err.lower() for s in
                         ("authentication", "username", "denied", "403")):
                    # Credentials are the problem — walk the user through login.
                    self.toast(_("sync_failed"))
                    GitHubLoginDialog(self, on_done=self.start_sync).present()
                else:
                    self.toast(f"{_('sync_failed')}: {err}")
            else:
                self.toast(_("sync_done"))
            self.refresh_pages()

        run_async(work, done)

    # -- Google Drive: auto-import projects from Petacomm Petacore™ ---------------
    def import_drive_projects(self, auto=False, parent_window=None):
        """Scan the Drive folder; offer to download every project that is
        not yet registered locally (second-computer flow).

        `parent_window` should be the currently visible top-level (e.g. the
        setup wizard) when this is triggered from inside a modal dialog —
        otherwise the confirmation would open behind it and look like
        nothing happened."""
        from . import gdrive
        top = parent_window or self

        def scanned(names, error):
            if error or names is None:
                if not auto:
                    self.toast(f"{_('drive_failed')}: {error or ''}")
                return
            existing = {p["name"].lower() for p in config.get("projects")}
            new = [n for n in names if n.lower() not in existing]
            if not new:
                if not auto:
                    self.toast(_("no_new_drive"))
                return
            dialog = Adw.MessageDialog(
                transient_for=top,
                heading=_("drive_found", n=len(new)),
                body="\u2022 " + "\n\u2022 ".join(new[:12])
                     + ("\n…" if len(new) > 12 else "")
                     + "\n\n" + _("drive_import_body"))
            dialog.add_response("cancel", _("cancel"))
            dialog.add_response("import", _("import_all"))
            dialog.set_response_appearance(
                "import", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect(
                "response",
                lambda _d, r: self._import_projects(new) if r == "import"
                else None)
            dialog.present()

        run_async(gdrive.list_remote_projects, scanned)

    def _import_projects(self, names):
        from . import gdrive
        base = os.path.join(os.path.expanduser("~"), "Projects")

        def work():
            done_paths = []
            for name in names:
                GLib.idle_add(self.toast, _("importing", p=name))
                local = os.path.join(base, name)
                os.makedirs(local, exist_ok=True)
                gdrive.pull_project(name, local)
                if not gitops.is_repo(local):
                    try:
                        gitops.init(local)
                    except gitops.GitError:
                        pass
                done_paths.append((name, local))
            return done_paths

        def finished(done_paths, error):
            self.end_operation()
            if error:
                self.toast(f"{_('drive_failed')}: {error}")
            for name, local in (done_paths or []):
                config.add_project(name, local, "")
                self.toast(_("imported", p=name))
            if done_paths:
                self._build()
                self.restart_autosave()

        self.begin_operation(_("op_import"))
        run_async(work, finished)

    # -- Autosave ---------------------------------------------------------------
    def restart_autosave(self):
        if self._autosave_id:
            GLib.source_remove(self._autosave_id)
            self._autosave_id = None
        if not config.get("autosave_enabled"):
            return
        minutes = max(1, int(config.get("autosave_minutes")))
        self._autosave_id = GLib.timeout_add_seconds(
            minutes * 60, self._autosave_tick)

    def _autosave_tick(self):
        project = config.active_project()
        if project:
            run_async(
                lambda: SnapshotManager(project["path"]).create(kind="auto"),
                lambda r, e: None)
        return True  # keep the timer running
