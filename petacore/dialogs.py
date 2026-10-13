"""Dialogs for Petacore: first-run language chooser, New Project wizard,
GitHub login, and application preferences."""

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import github, gitops  # noqa: E402
from .config import config  # noqa: E402
from .i18n import LANGUAGES, translator as _  # noqa: E402
from .util import human_size, run_async  # noqa: E402


# --------------------------------------------------------------------------- #
# Brand icons (github.svg, gdrive.svg) — loaded like <img src>, with fallback
# --------------------------------------------------------------------------- #
import os as _os  # noqa: E402
from .config import DATA_DIR as _DATA_DIR  # noqa: E402

_BRAND_DIRS = [
    _os.path.join(_DATA_DIR, "brand-icons"),
    _os.path.join(_os.path.dirname(__file__), "brand-icons"),
]
_brand_cache = {}


def _symbolic_ready():
    """Let GTK find Petacore's own symbolic icons (petacore-*-symbolic)."""
    if _brand_cache.get("symbolic") is None:
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display is None:
            return False
        theme = Gtk.IconTheme.get_for_display(display)
        for d in _BRAND_DIRS:
            if _os.path.isfile(_os.path.join(d, "petacore-github-symbolic.svg")):
                theme.add_search_path(d)
        _brand_cache["symbolic"] = True
    return True


def brand_image(name, size=64, fallback_icon="image-missing-symbolic"):
    """Gtk.Image for petacore/brand-icons/<name>.svg|png, or the fallback
    themed icon when the asset is missing.

    The GitHub mark is one colour, so it is drawn as a symbolic icon: GTK
    paints it in the text colour, dark on a light window and light on a
    dark one, and repaints it when the style changes."""
    if name == "github" and _symbolic_ready():
        return Gtk.Image(icon_name="petacore-github-symbolic",
                         pixel_size=size)
    key = (name, size)
    if key not in _brand_cache:
        path = None
        for d in _BRAND_DIRS:
            for suffix in (".svg", ".png"):
                candidate = _os.path.join(d, name + suffix)
                if _os.path.isfile(candidate):
                    path = candidate
                    break
            if path:
                break
        tex = None
        if path:
            try:
                from gi.repository import Gdk, GdkPixbuf
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, size * 2, size * 2, True)
                tex = Gdk.Texture.new_for_pixbuf(pb)
            except Exception:
                tex = None
        _brand_cache[key] = tex
    tex = _brand_cache[key]
    if tex is None:
        return Gtk.Image(icon_name=fallback_icon, pixel_size=size)
    img = Gtk.Image.new_from_paintable(tex)
    img.set_pixel_size(size)
    return img


# --------------------------------------------------------------------------- #
# First-run language selection
# --------------------------------------------------------------------------- #
class LanguageDialog(Adw.Window):
    """Shown on first launch: pick a language before anything else."""

    def __init__(self, parent, on_done):
        super().__init__(transient_for=parent, modal=True,
                         default_width=420, default_height=520)
        self._on_done = on_done

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False,
                               show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Petacore"))
        toolbar.add_top_bar(header)

        page = Adw.StatusPage(
            icon_name="preferences-desktop-locale-symbolic",
            title=_("welcome_title"),
            description=_("choose_language"),
        )

        listbox = Gtk.ListBox(css_classes=["boxed-list"],
                              selection_mode=Gtk.SelectionMode.NONE,
                              margin_start=24, margin_end=24)
        for code, label in LANGUAGES:
            row = Adw.ActionRow(title=label, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", self._on_pick, code)
            listbox.append(row)
        page.set_child(listbox)

        toolbar.set_content(page)
        self.set_content(toolbar)

    def _on_pick(self, _row, code):
        config.set("language", code)
        config.set("first_run", False)
        _.set_language(code)
        self.close()
        self._on_done()


# --------------------------------------------------------------------------- #
# New Project wizard
# --------------------------------------------------------------------------- #
class NewProjectDialog(Adw.Window):
    """Wizard: project name → GitHub repository URL → location → create."""

    def __init__(self, parent, on_created):
        super().__init__(transient_for=parent, modal=True,
                         default_width=540, default_height=700,
                         title=_("new_project"))
        self._on_created = on_created
        self._parent = parent

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("cancel"))
        cancel.connect("clicked", lambda *a: self.close())
        header.pack_start(cancel)

        self.create_btn = Gtk.Button(label=_("create"),
                                     css_classes=["suggested-action"])
        self.create_btn.connect("clicked", self._on_create)
        header.pack_end(self.create_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("new_project"))

        self.name_row = Adw.EntryRow(title=_("project_name"))
        self.name_row.connect("changed", self._validate)
        group.add(self.name_row)

        self.repo_row = Adw.EntryRow(title=_("repo_url"))
        group.add(self.repo_row)

        default_loc = os.path.join(os.path.expanduser("~"), "Projects")
        self.location = default_loc
        self.loc_row = Adw.ActionRow(title=_("location"), subtitle=default_loc)
        browse = Gtk.Button(label=_("browse"), valign=Gtk.Align.CENTER)
        browse.connect("clicked", self._on_browse)
        self.loc_row.add_suffix(browse)
        group.add(self.loc_row)

        hint = Adw.PreferencesGroup(description=_("repo_hint"))
        page.add(group)
        page.add(hint)

        # -- Encryption, offered right when the project is created ----------
        enc = Adw.PreferencesGroup(title=_("encryption"),
                                   description=_("encrypt_explain"))
        self._enc_modes = ["none", "pin4", "pin8", "password"]
        enc_model = Gtk.StringList.new([_("enc_none"), _("enc_pin4"),
                                        _("enc_pin8"), _("enc_password")])
        self.enc_row = Adw.ComboRow(title=_("encrypt_upload"),
                                    model=enc_model)
        self.enc_row.connect("notify::selected", self._on_enc_mode)
        enc.add(self.enc_row)

        self.enc_secret = Adw.PasswordEntryRow(title=_("enc_secret"))
        self.enc_secret2 = Adw.PasswordEntryRow(title=_("enc_secret_again"))
        self.enc_secret.set_visible(False)
        self.enc_secret2.set_visible(False)
        enc.add(self.enc_secret)
        enc.add(self.enc_secret2)
        page.add(enc)

        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, margin_top=12)
        self.status_label = Gtk.Label(halign=Gtk.Align.CENTER,
                                      css_classes=["dim-label"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        box.append(self.spinner)
        box.append(self.status_label)

        toolbar.set_content(box)
        self.set_content(toolbar)
        self._validate()

    def _on_enc_mode(self, _row, _pspec):
        needs = self._enc_modes[self.enc_row.get_selected()] != "none"
        self.enc_secret.set_visible(needs)
        self.enc_secret2.set_visible(needs)
        self.status_label.set_text("")

    def _encryption_choice(self):
        """(mode, secret) when valid, or None when the entries are wrong —
        the reason is shown in the status label."""
        from . import crypto
        mode = self._enc_modes[self.enc_row.get_selected()]
        if mode == "none":
            return ("none", "")
        secret = self.enc_secret.get_text()
        if secret != self.enc_secret2.get_text():
            self.status_label.set_text(_("enc_mismatch"))
            return None
        try:
            crypto.validate(mode, secret)
        except crypto.CryptoError as e:
            self.status_label.set_text({
                "pin4": _("enc_bad_pin4"),
                "pin8": _("enc_bad_pin8"),
                "password": _("enc_bad_pass"),
            }.get(str(e), _("enc_bad_pass")))
            return None
        return (mode, secret)

    def _validate(self, *args):
        self.create_btn.set_sensitive(bool(self.name_row.get_text().strip()))

    def _on_browse(self, _btn):
        dialog = Gtk.FileDialog(title=_("location"))
        dialog.select_folder(self, None, self._on_browsed)

    def _on_browsed(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder:
            self.location = folder.get_path()
            self.loc_row.set_subtitle(self.location)

    def _on_create(self, _btn):
        name = self.name_row.get_text().strip()
        repo_url = self.repo_row.get_text().strip()
        dest = os.path.join(self.location, name)

        choice = self._encryption_choice()
        if choice is None:
            return

        self.create_btn.set_sensitive(False)
        self.spinner.start()
        self.status_label.set_text(_("creating"))

        def work():
            if repo_url and not os.path.isdir(os.path.join(dest, ".git")):
                gitops.clone(repo_url, dest, config.get("github_token"))
                return dest, True
            if not gitops.is_repo(dest):
                gitops.init(dest, repo_url)
            return dest, False

        def done(result, error):
            self.spinner.stop()
            if error:
                self.status_label.set_text(str(error))
                self.create_btn.set_sensitive(True)
                return
            path, cloned = result
            config.add_project(name, path, repo_url)
            mode, secret = choice
            if mode != "none":
                from . import crypto
                crypto.enable(path, mode, secret)
            self.close()
            # code that came from the internet: offer to scan it
            self._on_created(cloned_name=name if cloned else None)

        run_async(work, done)


# --------------------------------------------------------------------------- #
# Edit Project
# --------------------------------------------------------------------------- #
class EditProjectDialog(Adw.Window):
    """Edit an existing project: name, folder, GitHub URL, installed-app
    folder for save-syncing — or remove it from the list."""

    def __init__(self, parent, project, on_done):
        super().__init__(transient_for=parent, modal=True,
                         default_width=560, default_height=760,
                         title=_("edit_project"))
        self._on_done = on_done
        self._project = project
        self._old_path = project["path"]
        self._new_path = project["path"]
        self._deploy_path = project.get("deploy_path", "")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("cancel"))
        cancel.connect("clicked", lambda *a: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label=_("save"), css_classes=["suggested-action"])
        save.connect("clicked", self._on_save)
        header.pack_end(save)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("edit_project"))

        self.name_row = Adw.EntryRow(title=_("project_name"),
                                     text=project.get("name", ""))
        group.add(self.name_row)

        self.repo_row = Adw.EntryRow(title=_("repo_url"),
                                     text=project.get("repo_url", ""))
        group.add(self.repo_row)

        self.folder_row = Adw.ActionRow(title=_("project_folder"),
                                        subtitle=self._new_path,
                                        subtitle_lines=2)
        fbtn = Gtk.Button(label=_("browse"), valign=Gtk.Align.CENTER)
        fbtn.connect("clicked", self._pick_folder)
        self.folder_row.add_suffix(fbtn)
        group.add(self.folder_row)

        self.deploy_row = Adw.ActionRow(
            title=_("installed_folder"),
            subtitle=self._deploy_path or _("not_set"), subtitle_lines=2)
        dbtn = Gtk.Button(label=_("browse"), valign=Gtk.Align.CENTER)
        dbtn.connect("clicked", self._pick_deploy)
        clear = Gtk.Button(icon_name="edit-clear-symbolic",
                           valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text=_("clear"))
        clear.connect("clicked", self._clear_deploy)
        self.deploy_row.add_suffix(dbtn)
        self.deploy_row.add_suffix(clear)
        group.add(self.deploy_row)
        page.add(group)

        # -- Encryption (cloud side only) ---------------------------------
        from . import crypto
        enc = Adw.PreferencesGroup(title=_("encryption"),
                                   description=_("encrypt_explain"))
        self._enc_modes = ["none", "pin4", "pin8", "password"]
        enc_model = Gtk.StringList.new([_("enc_none"), _("enc_pin4"),
                                        _("enc_pin8"), _("enc_password")])
        self.enc_row = Adw.ComboRow(title=_("encrypt_upload"),
                                    model=enc_model)
        current_mode = crypto.mode_for(project)
        self.enc_row.set_selected(self._enc_modes.index(current_mode)
                                  if current_mode in self._enc_modes else 0)
        self.enc_row.connect("notify::selected", self._on_enc_mode)
        enc.add(self.enc_row)

        self.enc_secret = Adw.PasswordEntryRow(title=_("enc_secret"))
        self.enc_secret2 = Adw.PasswordEntryRow(title=_("enc_secret_again"))
        enc.add(self.enc_secret)
        enc.add(self.enc_secret2)
        self.enc_error = Gtk.Label(css_classes=["error"], wrap=True)
        enc.add(self.enc_error)
        self._update_enc_visibility()
        page.add(enc)

        danger = Adw.PreferencesGroup(description=_("remove_detail"))
        remove = Gtk.Button(label=_("remove_project"),
                            halign=Gtk.Align.CENTER,
                            css_classes=["destructive-action"])
        remove.connect("clicked", self._on_remove)
        danger.add(remove)
        page.add(danger)

        toolbar.set_content(page)
        self.set_content(toolbar)

    def _on_enc_mode(self, _row, _pspec):
        self._update_enc_visibility()

    def _update_enc_visibility(self):
        needs_secret = self._enc_modes[self.enc_row.get_selected()] != "none"
        self.enc_secret.set_visible(needs_secret)
        self.enc_secret2.set_visible(needs_secret)
        self.enc_error.set_text("")

    def _apply_encryption(self, path):
        """Returns True when the settings are valid (or unchanged)."""
        from . import crypto
        mode = self._enc_modes[self.enc_row.get_selected()]
        if mode == "none":
            crypto.disable(path)
            return True
        secret = self.enc_secret.get_text()
        secret2 = self.enc_secret2.get_text()
        if not secret and crypto.mode_for(self._project) == mode:
            return True                      # unchanged, keep the old secret
        if secret != secret2:
            self.enc_error.set_text(_("enc_mismatch"))
            return False
        try:
            crypto.enable(path, mode, secret)
        except crypto.CryptoError as e:
            self.enc_error.set_text({
                "pin4": _("enc_bad_pin4"),
                "pin8": _("enc_bad_pin8"),
                "password": _("enc_bad_pass"),
            }.get(str(e), _("enc_bad_pass")))
            return False
        return True

    def _pick_folder(self, _btn):
        fd = Gtk.FileDialog(title=_("project_folder"))
        fd.select_folder(self, None, self._folder_picked)

    def _folder_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder:
            self._new_path = folder.get_path()
            self.folder_row.set_subtitle(self._new_path)

    def _pick_deploy(self, _btn):
        fd = Gtk.FileDialog(title=_("deploy_choose"))
        fd.select_folder(self, None, self._deploy_picked)

    def _deploy_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder:
            self._deploy_path = folder.get_path()
            self.deploy_row.set_subtitle(self._deploy_path)

    def _clear_deploy(self, _btn):
        self._deploy_path = ""
        self.deploy_row.set_subtitle(_("not_set"))

    def _on_save(self, _btn):
        if not self._apply_encryption(self._new_path):
            return
        name = self.name_row.get_text().strip() or self._project.get("name")
        repo_url = self.repo_row.get_text().strip()
        config.update_project(self._old_path, name=name, path=self._new_path,
                              repo_url=repo_url,
                              deploy_path=self._deploy_path)
        # Keep git's origin in line with the edited URL (best effort).
        if repo_url != self._project.get("repo_url", "") \
                and gitops.is_repo(self._new_path):
            gitops.set_remote(self._new_path, repo_url)
        self.close()
        self._on_done(_("saved_changes"))

    def _on_remove(self, _btn):
        config.remove_project(self._old_path)
        self.close()
        self._on_done(None)



class GitHubLoginDialog(Adw.Window):
    """Connecting to GitHub: one button.

    github.sign_in takes the quickest route this computer offers. When a
    code has to be approved, it is copied to the clipboard and GitHub's page
    is opened, so all that is left is paste and Authorize. Pasting a token
    by hand stays available, folded away, for computers without a browser.
    """

    def __init__(self, parent, on_done=None):
        super().__init__(transient_for=parent, modal=True,
                         default_width=480, default_height=540,
                         title=_("github_login"))
        self._on_done = on_done or (lambda: None)
        self._cancel = threading.Event()
        self._uri = github.DEVICE_PAGE
        self.connect("close-request", self._on_close)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=28, margin_bottom=24,
                      margin_start=32, margin_end=32)
        box.append(brand_image("github", 64,
                               fallback_icon="web-browser-symbolic"))
        box.append(Gtk.Label(label=_("gh_connect_title"),
                             css_classes=["title-1"]))
        self.body = Gtk.Label(label=_("gh_connect_body"), wrap=True,
                              justify=Gtk.Justification.CENTER,
                              css_classes=["dim-label"], max_width_chars=44)
        box.append(self.body)

        # -- the one action --------------------------------------------------
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               vhomogeneous=False, margin_top=8)
        self.go_btn = Gtk.Button(label=_("gh_connect_btn"),
                                 halign=Gtk.Align.CENTER,
                                 css_classes=["suggested-action", "pill"])
        self.go_btn.connect("clicked", lambda *a: self._start())
        self.install_btn = Gtk.Button(label=_("gh_install"),
                                      halign=Gtk.Align.CENTER,
                                      css_classes=["suggested-action", "pill"])
        self.install_btn.connect("clicked", lambda *a: self._install_gh())
        self.stack.add_named(self.go_btn, "go")
        self.stack.add_named(self.install_btn, "install")

        waiting = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          halign=Gtk.Align.CENTER)
        self.code_label = Gtk.Label(selectable=True,
                                    css_classes=["title-1", "monospace"])
        waiting.append(self.code_label)
        copied = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        copied.append(Gtk.Image(icon_name="edit-copy-symbolic",
                                css_classes=["dim-label"]))
        copied.append(Gtk.Label(label=_("gh_code_copied"),
                                css_classes=["dim-label"]))
        waiting.append(copied)
        spin = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        spinner = Gtk.Spinner(spinning=True)
        spin.append(spinner)
        spin.append(Gtk.Label(label=_("device_waiting"),
                              css_classes=["caption", "dim-label"]))
        waiting.append(spin)
        reopen = Gtk.Button(label=_("gh_reopen"), halign=Gtk.Align.CENTER,
                            css_classes=["flat"])
        reopen.connect("clicked", lambda *a: self._open_page())
        waiting.append(reopen)
        self.stack.add_named(waiting, "waiting")

        busy = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER,
                           width_request=24, height_request=24)
        self.stack.add_named(busy, "busy")
        box.append(self.stack)

        self.status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                css_classes=["error"], visible=False)
        box.append(self.status)

        # -- by hand, for when nothing else works ------------------------------
        manual = Adw.PreferencesGroup(margin_top=10)
        expander = Adw.ExpanderRow(title=_("other_signin"))
        manual.add(expander)
        browser_row = Adw.ActionRow(title=_("create_token_browser"),
                                    activatable=True,
                                    subtitle=_("token_explain"),
                                    subtitle_lines=3)
        browser_row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        browser_row.connect("activated", lambda *a: Gio.AppInfo
                            .launch_default_for_uri(github.TOKEN_URL, None))
        expander.add_row(browser_row)
        self.token_row = Adw.PasswordEntryRow(title=_("or_paste_token"))
        self.token_row.connect("apply", lambda *a: self._on_token())
        self.token_row.set_show_apply_button(True)
        expander.add_row(self.token_row)
        self.client_row = Adw.EntryRow(title=_("gh_client_id_label"),
                                       text=config.get("github_client_id")
                                       or "", show_apply_button=True)
        self.client_row.connect("apply", lambda r: (
            config.set("github_client_id", r.get_text().strip()),
            self._choose_route()))
        expander.add_row(self.client_row)
        box.append(manual)

        clamp = Adw.Clamp(maximum_size=440, child=box)
        scroller = Gtk.ScrolledWindow(child=clamp,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True)
        toolbar.set_content(scroller)
        self.set_content(toolbar)
        self._choose_route()

    # -- routes -----------------------------------------------------------------
    def _choose_route(self):
        route = github.sign_in_route()
        if route == "manual":
            self.body.set_text(_("gh_need_cli"))
            self.stack.set_visible_child_name("install")
            return
        self.body.set_text(_("gh_connect_body"))
        self.stack.set_visible_child_name("go")
        # Already signed in with the GitHub CLI? Then say so: it is one click.
        run_async(github.gh_token, lambda token, _e: token and
                  self.body.set_text(_("gh_connect_ready")))

    def _install_gh(self):
        from . import launcher
        self.stack.set_visible_child_name("busy")
        self._show_error("")
        self.body.set_text(_("gh_installing"))

        def done(result, _error):
            ok = bool(result and result[0])
            if ok and github.gh_available():
                self._choose_route()
                self._start()
            else:
                self.stack.set_visible_child_name("install")
                self.body.set_text(_("gh_need_cli"))
                self._show_error(_("gh_install_failed"))
        run_async(lambda: launcher.install_packages(["gh"]), done)

    def _start(self):
        self._show_error("")
        self.stack.set_visible_child_name("busy")
        self._cancel.clear()

        def on_code(code, uri):
            GLib.idle_add(self._show_code, code, uri)

        run_async(lambda: github.sign_in(on_code, self._cancel),
                  self._signed_in)

    def _show_code(self, code, uri):
        self._uri = uri
        self.code_label.set_text(code)
        self.get_clipboard().set(code)
        self.stack.set_visible_child_name("waiting")
        self._open_page()
        return False

    def _open_page(self):
        Gio.AppInfo.launch_default_for_uri(self._uri, None)

    def _signed_in(self, result, error):
        if self._cancel.is_set():
            return
        if error or not result:
            self.stack.set_visible_child_name("go")
            if str(error) == "no-route":
                self._choose_route()
            else:
                self._show_error(str(error) if error else _("login_failed"))
            return
        token, user = result
        self._save(token, user)

    def _on_token(self):
        token = self.token_row.get_text().strip()
        if not token:
            return
        self.stack.set_visible_child_name("busy")

        def done(user, error):
            if error or not user:
                self.stack.set_visible_child_name("go")
                self._show_error(_("login_failed"))
                return
            self._save(token, user)
        run_async(lambda: github.validate_token(token), done)

    def _save(self, token, user):
        config.set("github_token", token)
        config.set("github_user", user)
        root = self.get_transient_for()
        if root is not None and hasattr(root, "toast"):
            root.toast(_("gh_connected", user=user))
        self._cancel.set()
        self.close()
        self._on_done()

    def _show_error(self, text):
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    def _on_close(self, *_a):
        self._cancel.set()          # stops a sign-in still waiting in the
        return False                # background


# --------------------------------------------------------------------------- #
# Upload to GitHub — push the code, and publish it as a version
# --------------------------------------------------------------------------- #
_PUBLISH_CSS = """
.pc-pub-chip { border-radius: 999px; padding: 4px 14px; min-height: 0;
               font-weight: 600; }
.pc-pub-chip:checked { background: alpha(@accent_bg_color, 0.16);
                       color: @accent_color;
                       box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.7); }
.pc-pub-chips { padding: 10px 12px; }
.pc-pub-done image.icon { color: @success_color; }
"""
_publish_css_done = []


def _install_publish_css():
    if _publish_css_done:
        return
    from gi.repository import Gdk
    provider = Gtk.CssProvider()
    provider.load_from_string(_PUBLISH_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _publish_css_done.append(provider)


class PublishDialog(Adw.Window):
    """"Upload to GitHub": the code goes to main, and — unless switched
    off — it becomes a new version on the project's GitHub page, with the
    packages from the version library attached.

    Everything has a sensible default: the repository is made if there is
    none, the version is the one most likely to come next, the branch is
    main. Usually the only click is Publish.
    """

    def __init__(self, parent, project, on_done=None):
        super().__init__(transient_for=parent, modal=True,
                         default_width=560, default_height=720,
                         title=_("pub_title"))
        from . import publish
        _install_publish_css()
        self._pub = publish
        self._project = project
        self._path = project["path"]
        self._on_done = on_done or (lambda result: None)
        self._chips = []
        self._syncing_chip = False
        self._hint = ""

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        self.cancel_btn = Gtk.Button(label=_("cancel"))
        self.cancel_btn.connect("clicked", lambda *a: self.close())
        header.pack_start(self.cancel_btn)
        self.go_btn = Gtk.Button(label=_("pub_go"),
                                 css_classes=["suggested-action"])
        self.go_btn.connect("clicked", lambda *a: self._go())
        header.pack_end(self.go_btn)
        toolbar.add_top_bar(header)

        self.view = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        page = Adw.PreferencesPage()

        # -- where it goes ---------------------------------------------------------
        account = Adw.PreferencesGroup()
        self.account_row = Adw.ActionRow()
        self.account_row.add_prefix(brand_image(
            "github", 32, fallback_icon="system-users-symbolic"))
        self.connect_btn = Gtk.Button(label=_("gh_connect_btn"),
                                      valign=Gtk.Align.CENTER,
                                      css_classes=["suggested-action", "pill"])
        self.connect_btn.connect("clicked", lambda *a: self._sign_in())
        self.account_row.add_suffix(self.connect_btn)
        account.add(self.account_row)
        page.add(account)

        try:
            self._where = publish.target(self._path)
            self._remote_error = ""
        except publish.PublishError:
            self._where = None
            self._remote_error = _("pub_err_not_github")

        self.repo_group = Adw.PreferencesGroup(visible=self._where is None
                                               and not self._remote_error)
        self.repo_row = Adw.EntryRow(
            title=_("pub_repo_name"),
            text=github.repo_slug(os.path.basename(self._path.rstrip("/"))))
        self.repo_row.connect("changed", lambda *a: self._validate())
        self.repo_group.add(self.repo_row)
        self.private_row = Adw.SwitchRow(title=_("pub_private"),
                                         subtitle=_("pub_private_hint"),
                                         active=True)
        self.repo_group.add(self.private_row)
        page.add(self.repo_group)

        # -- which version -----------------------------------------------------------
        version = Adw.PreferencesGroup(title=_("pub_version"),
                                       description=_("pub_version_hint"))
        self.release_row = Adw.SwitchRow(title=_("pub_as_release"),
                                         subtitle=_("pub_as_release_hint"),
                                         active=True)
        self.release_row.connect("notify::active", lambda *a: self._mode())
        version.add(self.release_row)

        self.chip_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                    column_spacing=8, row_spacing=8,
                                    max_children_per_line=6,
                                    css_classes=["pc-pub-chips"])
        self.chip_row = Adw.PreferencesRow(activatable=False,
                                           child=self.chip_box)
        version.add(self.chip_row)

        self.version_row = Adw.EntryRow(title=_("pub_version"))
        self.version_row.connect("changed", lambda *a: self._validate())
        version.add(self.version_row)

        self.info_row = Adw.ActionRow(subtitle_lines=4, activatable=False)
        _symbolic_ready()
        self.info_icon = Gtk.Image(icon_name="petacore-tag-symbolic")
        self.info_row.add_prefix(self.info_icon)
        version.add(self.info_row)

        self.notes_row = Adw.ExpanderRow(title=_("pub_notes"),
                                         subtitle=_("pub_notes_hint"))
        self.notes = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  top_margin=10, bottom_margin=10,
                                  left_margin=12, right_margin=12,
                                  height_request=110)
        self.notes_row.add_row(self.notes)
        version.add(self.notes_row)
        page.add(version)

        branch = Adw.PreferencesGroup()
        self.branch_row = Adw.EntryRow(title=_("pub_branch"), text="main")
        self.branch_row.add_prefix(Gtk.Image(icon_name="media-playlist-consecutive-symbolic"))
        self.branch_row.connect("changed", lambda *a: self._validate())
        branch.add(self.branch_row)
        page.add(branch)

        # -- progress ------------------------------------------------------------------
        progress = Adw.PreferencesGroup()
        line = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=4)
        self.spinner = Gtk.Spinner()
        self.status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                max_width_chars=56)
        line.append(self.spinner)
        line.append(self.status)
        progress.add(line)
        page.add(progress)
        self.view.add_named(page, "form")

        # -- done ----------------------------------------------------------------------
        self.done_page = Adw.StatusPage(icon_name="emblem-ok-symbolic",
                                        css_classes=["pc-pub-done"])
        done_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           halign=Gtk.Align.CENTER)
        self.open_btn = Gtk.Button(label=_("pub_open"),
                                   css_classes=["suggested-action", "pill"])
        self.open_btn.connect("clicked", lambda *a: Gio.AppInfo
                              .launch_default_for_uri(self._result_url, None))
        done_box.append(self.open_btn)
        close = Gtk.Button(label=_("close"), css_classes=["flat"])
        close.connect("clicked", lambda *a: self.close())
        done_box.append(close)
        self.done_page.set_child(done_box)
        self.view.add_named(self.done_page, "done")

        toolbar.set_content(self.view)
        self.set_content(toolbar)

        self._show_account()
        self._suggest(publish.library_versions(self._path))
        self._mode()
        self._fetch_released()

    # -- account and repository ---------------------------------------------------
    def _token(self):
        return config.get("github_token") or ""

    def _show_account(self):
        user = config.get("github_user")
        signed_in = bool(self._token() and user)
        self.connect_btn.set_visible(not signed_in)
        self.account_row.set_title(f"@{user}" if signed_in
                                   else _("pub_signed_out"))
        if self._remote_error:
            self.account_row.set_subtitle(self._remote_error)
        elif self._where:
            self.account_row.set_subtitle("/".join(self._where))
        else:
            self.account_row.set_subtitle(_("pub_new_repo"))
        self._validate()

    def _sign_in(self, then=None):
        def done():
            self._show_account()
            self._fetch_released()
            if then:
                then()
        GitHubLoginDialog(self, on_done=done).present()

    # -- versions ------------------------------------------------------------------
    def _suggest(self, library, released=()):
        options = self._pub.suggest_for(library, released)
        while (child := self.chip_box.get_first_child()) is not None:
            self.chip_box.remove(child)
        self._chips = []
        group = None
        for item in options:
            text = self._pub.title(*item)
            chip = Gtk.ToggleButton(label=text, css_classes=["pc-pub-chip"])
            if group is None:
                group = chip
            else:
                chip.set_group(group)
            chip.connect("toggled", self._chip_toggled, text)
            self.chip_box.append(chip)
            self._chips.append((text, chip))
        self._hint = options[0][0] if options else ""
        if self._chips:
            self._chips[0][1].set_active(True)

    def _chip_toggled(self, chip, text):
        if chip.get_active() and not self._syncing_chip:
            self._syncing_chip = True
            self.version_row.set_text(text)
            self.version_row.set_position(-1)
            self._syncing_chip = False

    def _fetch_released(self):
        token = self._token()
        if not token or not self._where:
            return
        path = self._path
        pub = self._pub

        def work():
            return (pub.library_versions(path),
                    pub.released_on_github(token, path))

        def done(known, error):
            if error or known is None:
                return
            typed = self.version_row.get_text()
            untouched = any(text == typed for text, _c in self._chips)
            self._suggest(*known)
            if not untouched:
                self.version_row.set_text(typed)
        run_async(work, done)

    def _mode(self):
        on = self.release_row.get_active()
        for row in (self.chip_row, self.version_row, self.info_row,
                    self.notes_row):
            row.set_visible(on)
        self.go_btn.set_label(_("pub_go") if on else _("pub_push_only"))
        self._validate()

    def _parsed(self):
        try:
            return self._pub.parse(self.version_row.get_text(), self._hint)
        except self._pub.PublishError:
            return None

    def _validate(self):
        ok = not self._remote_error
        if self.release_row.get_active():
            parsed = self._parsed()
            typed = self.version_row.get_text()
            if not self._syncing_chip:
                for text, chip in self._chips:
                    if chip.get_active() and text != typed:
                        self._syncing_chip = True
                        chip.set_active(False)
                        self._syncing_chip = False
            if parsed is None:
                ok = False
                self.info_row.set_title(_("pub_bad_version"))
                self.info_row.set_subtitle("")
                self.info_icon.set_from_icon_name("dialog-warning-symbolic")
            else:
                tag = self._pub.tag(*parsed)
                self.info_icon.set_from_icon_name("petacore-tag-symbolic")
                self.info_row.set_title(
                    _("pub_tag_pre", tag=tag) if parsed[1] != "stable"
                    else _("pub_tag_stable", tag=tag))
                files = self._pub.packages_for(self._path, *parsed)
                self.info_row.set_subtitle(
                    _("pub_files", files=", ".join(os.path.basename(f)
                                                   for f in files))
                    if files else _("pub_no_files"))
        if not gitops.BRANCH.match(self.branch_row.get_text().strip()):
            ok = False
        if self.repo_group.get_visible() and not github.REPO_NAME.match(
                self.repo_row.get_text().strip()):
            ok = False
        self.go_btn.set_sensitive(ok)

    # -- publishing ------------------------------------------------------------------
    def _go(self):
        if not (self._token() and config.get("github_user")):
            self._sign_in(then=self._go)
            return
        release = self.release_row.get_active()
        parsed = self._parsed() if release else None
        title = self._pub.title(*parsed) if parsed else ""
        branch = self.branch_row.get_text().strip()
        buf = self.notes.get_buffer()
        notes = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        args = dict(version_text=self.version_row.get_text(),
                    number_hint=self._hint, branch=branch,
                    repo_name=self.repo_row.get_text().strip(),
                    private=self.private_row.get_active(), notes=notes,
                    release=release)
        token = self._token()
        path = self._path
        pub = self._pub

        def on_status(stage, detail=""):
            text = {"account": _("pub_stage_account"),
                    "repo": _("pub_stage_repo"),
                    "push": _("pub_stage_push", branch=branch),
                    "release": _("pub_stage_release", title=title),
                    "upload": _("pub_stage_upload", name=detail)}.get(stage, "")
            GLib.idle_add(self._set_status, text)

        self._busy(True)
        run_async(lambda: pub.publish(path, token, on_status=on_status,
                                      **args),
                  lambda result, error: self._finished(result, error,
                                                       title, branch))

    def _set_status(self, text, error=False):
        self.status.set_text(text)
        if error:
            self.status.add_css_class("error")
        else:
            self.status.remove_css_class("error")
        return False

    def _busy(self, busy):
        self.spinner.set_spinning(busy)
        self.go_btn.set_sensitive(not busy)
        self.release_row.set_sensitive(not busy)

    def _finished(self, result, error, title, branch):
        self._busy(False)
        if error is not None:
            code = str(error)
            if isinstance(error, github.GitHubError) and error.status == 401:
                config.set("github_token", "")
                config.set("github_user", "")
                self._show_account()
                self._set_status("")
                self._sign_in(then=self._go)
                return
            message = {
                "exists": _("pub_err_exists", title=title),
                "diverged": _("pub_err_diverged", branch=branch),
                "not-github": _("pub_err_not_github"),
                "bad-branch": _("pub_err_branch"),
                "bad-repo-name": _("pub_err_repo_name"),
                "bad-version": _("pub_bad_version"),
            }.get(code, _("pub_failed", err=code))
            self._set_status(message, error=True)
            self._validate()
            return
        if result["repo"] and not self._project.get("repo_url"):
            config.set_project_field(self._path, "repo_url",
                                     result["repo"] + ".git")
        self._result_url = result["url"]
        self.done_page.set_title(_("pub_done_release", title=result["title"])
                                 if result["title"] else _("pub_done_push"))
        if result["kept_back"]:
            self.done_page.set_description(_("pub_kept_back",
                                             files=", ".join(result["kept_back"])))
        self.go_btn.set_visible(False)
        self.cancel_btn.set_visible(False)
        self.view.set_visible_child_name("done")
        self._on_done(result)


# --------------------------------------------------------------------------- #
# Backups — everything a Drive sync replaced, ready to be put back
# --------------------------------------------------------------------------- #
AUTOPULL_MINUTES = [5, 10, 30, 60]
BACKUP_DAYS = [7, 30, 90, 365, 0]
_BACKUP_ICONS = {"push": "folder-remote-symbolic",
                 "pull": "folder-download-symbolic",
                 "restore": "edit-undo-symbolic"}


from .gdrive import backup_day, backup_title  # noqa: E402


class BackupsWindow(Adw.Window):
    """A project's Drive backups, grouped by day. Each one can be opened
    to see its files, and put back whole or one file at a time."""

    def __init__(self, parent, project):
        super().__init__(transient_for=parent, modal=True,
                         default_width=620, default_height=700)
        # opened from Settings, the main window is the one that toasts
        self._window = parent if hasattr(parent, "toast") \
            else parent.get_transient_for()
        self._project = project
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("backups_open"),
                                                subtitle=project["name"]))
        toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          valign=Gtk.Align.CENTER)
        loading.append(Gtk.Spinner(spinning=True, width_request=28,
                                   height_request=28))
        loading.append(Gtk.Label(label=_("backups_loading"),
                                 css_classes=["dim-label"]))
        self.stack.add_named(loading, "loading")
        toolbar.set_content(self.stack)
        self.set_content(toolbar)
        self._load()

    def _intro(self):
        days = config.get("drive_backup_days")
        return (_("backups_intro", days=days) if days
                else _("backups_intro_forever"))

    def _load(self):
        from . import gdrive
        self.stack.set_visible_child_name("loading")
        project = self._project
        run_async(lambda: gdrive.list_backups(project["path"],
                                              project["name"]),
                  self._show)

    def _show(self, backups, error):
        old = self.stack.get_child_by_name("list")
        if old is not None:
            self.stack.remove(old)
        if not backups:
            page = Adw.StatusPage(icon_name="security-high-symbolic",
                                  title=_("backups_empty_title"),
                                  description=_("backups_empty") + "\n\n"
                                  + self._intro())
            self.stack.add_named(page, "list")
            self.stack.set_visible_child_name("list")
            return
        page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(description=self._intro())
        page.add(intro)
        groups = {}
        for backup in backups:
            day = backup_day(backup["time"])
            if day not in groups:
                groups[day] = Adw.PreferencesGroup(title=day)
                page.add(groups[day])
            groups[day].add(self._row(backup))
        self.stack.add_named(page, "list")
        self.stack.set_visible_child_name("list")

    def _row(self, backup):
        import time as _time
        size = sum(s for _p, s in backup["files"])
        where = _("backup_where_drive") if backup["where"] == "drive" \
            else _("backup_where_local")
        row = Adw.ExpanderRow(
            title=backup_title(backup),
            subtitle=" · ".join([
                _time.strftime("%H:%M", _time.localtime(backup["time"])),
                _("backup_files", n=len(backup["files"]),
                  size=human_size(size)),
                where]))
        row.add_prefix(Gtk.Image(icon_name=_BACKUP_ICONS.get(
            backup["kind"], "document-save-symbolic")))
        put_back = Gtk.Button(label=_("backup_restore"),
                              valign=Gtk.Align.CENTER)
        put_back.connect("clicked", lambda *a: self._ask(backup, None))
        row.add_suffix(put_back)
        for rel, fsize in backup["files"][:200]:
            file_row = Adw.ActionRow(title=GLib.markup_escape_text(rel),
                                     subtitle=human_size(fsize))
            one = Gtk.Button(icon_name="edit-undo-symbolic",
                             tooltip_text=_("backup_restore"),
                             valign=Gtk.Align.CENTER, css_classes=["flat"])
            one.connect("clicked", lambda *a, r=rel: self._ask(backup, [r]))
            file_row.add_suffix(one)
            row.add_row(file_row)
        return row

    def _ask(self, backup, files):
        count = len(files) if files else len(backup["files"])
        dialog = Adw.MessageDialog(
            transient_for=self, heading=_("backup_restore_q", n=count),
            body=_("backup_restore_body"))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("restore", _("backup_restore"))
        dialog.set_response_appearance("restore",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "restore"
                       and self._restore(backup, files))
        dialog.present()

    def _restore(self, backup, files):
        from . import gdrive
        project = self._project

        def done(count, error):
            if error:
                self._window.toast(f"{_('drive_failed')}: {error}")
                return
            self._window.toast(_("backup_restored", n=count))
            if hasattr(self._window, "refresh_pages"):
                self._window.refresh_pages()
            self._load()
        self.stack.set_visible_child_name("loading")
        run_async(lambda: gdrive.restore_backup(project["path"],
                                                project["name"], backup,
                                                files), done)


# --------------------------------------------------------------------------- #
# Detailed GPG signing key creation
# --------------------------------------------------------------------------- #
KEY_TYPES = [("ed25519", "Ed25519"), ("rsa4096", "RSA 4096"),
             ("rsa3072", "RSA 3072"), ("rsa2048", "RSA 2048")]
EXPIRES = [("0", "exp_never"), ("1y", "exp_1y"),
           ("2y", "exp_2y"), ("5y", "exp_5y")]


class GpgKeyDialog(Adw.Window):
    """Full-control GPG key creation: identity, algorithm, expiry,
    optional passphrase."""

    def __init__(self, parent, default_name="", default_email="",
                 on_done=None):
        super().__init__(transient_for=parent, modal=True,
                         default_width=520, default_height=620,
                         title=_("create_key"))
        self._on_done = on_done or (lambda: None)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("cancel"))
        cancel.connect("clicked", lambda *a: self.close())
        header.pack_start(cancel)
        self.create_btn = Gtk.Button(label=_("create"),
                                     css_classes=["suggested-action"])
        self.create_btn.connect("clicked", self._on_create)
        header.pack_end(self.create_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        ident = Adw.PreferencesGroup(title=_("create_key"))
        self.name_row = Adw.EntryRow(title=_("full_name"), text=default_name)
        self.name_row.connect("changed", self._validate)
        ident.add(self.name_row)
        self.email_row = Adw.EntryRow(title=_("email"), text=default_email)
        self.email_row.connect("changed", self._validate)
        ident.add(self.email_row)
        self.comment_row = Adw.EntryRow(title=_("comment_f"))
        ident.add(self.comment_row)
        page.add(ident)

        tech = Adw.PreferencesGroup(description=_("key_type_hint"))
        type_model = Gtk.StringList.new([label for _c, label in KEY_TYPES])
        self.type_row = Adw.ComboRow(title=_("key_type"), model=type_model)
        tech.add(self.type_row)

        exp_model = Gtk.StringList.new([_(k) for _c, k in EXPIRES])
        self.exp_row = Adw.ComboRow(title=_("expiration"), model=exp_model)
        tech.add(self.exp_row)
        page.add(tech)

        secret = Adw.PreferencesGroup(description=_("passphrase_hint"))
        self.pass_row = Adw.PasswordEntryRow(title=_("passphrase"))
        secret.add(self.pass_row)
        page.add(secret)

        status_group = Adw.PreferencesGroup()
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER)
        self.status = Gtk.Label(css_classes=["dim-label"], wrap=True,
                                justify=Gtk.Justification.CENTER)
        status_group.add(self.spinner)
        status_group.add(self.status)
        page.add(status_group)

        toolbar.set_content(page)
        self.set_content(toolbar)
        self._validate()

    def _validate(self, *_a):
        ok = bool(self.name_row.get_text().strip()) and \
            "@" in self.email_row.get_text()
        self.create_btn.set_sensitive(ok)

    def _on_create(self, _btn):
        from . import gpgsign
        name = self.name_row.get_text().strip()
        email = self.email_row.get_text().strip()
        comment = self.comment_row.get_text().strip()
        key_type = KEY_TYPES[self.type_row.get_selected()][0]
        expire = EXPIRES[self.exp_row.get_selected()][0]
        passphrase = self.pass_row.get_text()

        self.create_btn.set_sensitive(False)
        self.spinner.start()
        self.status.set_text(_("creating_key"))

        def done(_res, error):
            self.spinner.stop()
            if error:
                self.create_btn.set_sensitive(True)
                self.status.set_text(str(error))
                return
            self.close()
            self._on_done()

        from .util import run_async as _ra
        _ra(lambda: gpgsign.create_key(name, email, comment=comment,
                                       key_type=key_type, expire=expire,
                                       passphrase=passphrase), done)



THEMES = ["system", "light", "dark"]


class RepoSettingsDialog(Adw.Window):
    """Everything one APT archive needs to know about itself.

    Written as one page rather than a wizard: a repository has six or seven
    settings, most of which have a sensible default, and stepping through
    them one at a time would make a small decision feel like a large one.
    Only two fields have no default — the folder and the signing key — and
    the Save button stays insensitive until both are answered.
    """

    def __init__(self, parent, profile=None, on_saved=None):
        from . import repo
        super().__init__(transient_for=parent, modal=True,
                         default_width=560, default_height=680,
                         title=_("repo_settings") if profile
                         else _("repo_new"))
        self._repo = repo
        self._previous = (profile or {}).get("name", "")
        self._on_saved = on_saved or (lambda stored: None)
        settings = repo.normalise(profile or {})
        if not profile:
            settings["name"] = ""

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("cancel"))
        cancel.connect("clicked", lambda *a: self.close())
        header.pack_start(cancel)
        self.save_btn = Gtk.Button(label=_("repo_save"),
                                   css_classes=["suggested-action"])
        self.save_btn.connect("clicked", self._on_save)
        header.pack_end(self.save_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        # -- identity ---------------------------------------------------------
        basics = Adw.PreferencesGroup(title=_("repo_page"),
                                      description=_("repo_page_hint"))
        self.name_row = Adw.EntryRow(title=_("repo_name"),
                                     text=settings["name"])
        self.name_row.connect("changed", self._validate)
        basics.add(self.name_row)

        self.folder_row = Adw.ActionRow(title=_("repo_folder"),
                                        subtitle=settings["root"]
                                        or _("repo_folder_hint"),
                                        subtitle_lines=2)
        browse = Gtk.Button(label=_("browse"), valign=Gtk.Align.CENTER)
        browse.connect("clicked", self._choose_folder)
        self.folder_row.add_suffix(browse)
        basics.add(self.folder_row)
        self._root = settings["root"]

        self.url_row = Adw.EntryRow(title=_("repo_address"),
                                    text=settings["base_url"])
        basics.add(self.url_row)
        url_hint = Adw.ActionRow(title=_("repo_address_hint"),
                                 css_classes=["dim-label"], subtitle_lines=2)
        basics.add(url_hint)
        page.add(basics)

        # -- signing ----------------------------------------------------------
        signing = Adw.PreferencesGroup(title=_("repo_key"),
                                       description=_("repo_key_hint"))
        from . import gpgsign
        self._keys = gpgsign.list_keys_detailed()
        labels = [f'{k["uid"]}  ·  {k["fpr"][-16:]}' for k in self._keys] \
            or [_("no_key")]
        self.key_row = Adw.ComboRow(title=_("repo_key"),
                                    model=Gtk.StringList.new(labels))
        for index, key in enumerate(self._keys):
            if key["fpr"] == settings["key"]:
                self.key_row.set_selected(index)
        self.key_row.connect("notify::selected", self._validate)
        signing.add(self.key_row)

        create_key = Adw.ActionRow(title=_("create_key"), activatable=True)
        create_key.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
        create_key.connect("activated", self._create_key)
        signing.add(create_key)
        page.add(signing)

        # -- archive layout ---------------------------------------------------
        layout = Adw.PreferencesGroup(title=_("repo_archs"),
                                      description=_("repo_archs_hint"))
        self.suite_row = Adw.EntryRow(title=_("repo_suite"),
                                      text=settings["suite"])
        layout.add(self.suite_row)
        self.component_row = Adw.EntryRow(title=_("repo_component"),
                                          text=settings["component"])
        layout.add(self.component_row)
        self.archs_row = Adw.EntryRow(title=_("repo_archs"),
                                      text=", ".join(settings["archs"]))
        layout.add(self.archs_row)
        self.origin_row = Adw.EntryRow(title=_("repo_origin"),
                                       text=settings["origin"])
        layout.add(self.origin_row)
        self.label_row = Adw.EntryRow(title=_("repo_label"),
                                      text=settings["label"])
        layout.add(self.label_row)
        self.desc_row = Adw.EntryRow(title=_("repo_desc"),
                                     text=settings["description"])
        layout.add(self.desc_row)
        page.add(layout)

        # -- publishing -------------------------------------------------------
        self._methods = ["none", "folder", "rsync"]
        publishing = Adw.PreferencesGroup(title=_("repo_publish"),
                                          description=_("repo_target_hint"))
        method_model = Gtk.StringList.new([_("repo_pub_none"),
                                           _("repo_pub_folder"),
                                           _("repo_pub_rsync")])
        self.method_row = Adw.ComboRow(title=_("repo_publish_how"),
                                       model=method_model)
        self.method_row.set_selected(
            self._methods.index(settings["publish_method"]))
        self.method_row.connect("notify::selected", self._on_method)
        publishing.add(self.method_row)

        self.target_row = Adw.EntryRow(title=_("repo_target"),
                                       text=settings["publish_target"])
        publishing.add(self.target_row)

        self.target_browse = Adw.ActionRow(title=_("repo_pub_folder"),
                                           activatable=True)
        self.target_browse.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        self.target_browse.connect("activated", self._choose_target)
        publishing.add(self.target_browse)
        page.add(publishing)

        # -- what the user is told --------------------------------------------
        self.tools_group = Adw.PreferencesGroup()
        missing = [name for name, present in repo.tools_available().items()
                   if not present]
        if missing:
            row = Adw.ActionRow(title=", ".join(missing),
                                subtitle=_("repo_no_index_tool")
                                if not repo.can_index() else "",
                                css_classes=["dim-label"], subtitle_lines=2)
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            self.tools_group.add(row)
        page.add(self.tools_group)

        toolbar.set_content(page)
        self.set_content(toolbar)
        self._on_method()
        self._validate()

    # -- interaction -----------------------------------------------------------
    def _on_method(self, *_a):
        method = self._methods[self.method_row.get_selected()]
        self.target_row.set_visible(method != "none")
        self.target_browse.set_visible(method == "folder")
        self._validate()

    def _validate(self, *_a):
        self.save_btn.set_sensitive(
            bool(self.name_row.get_text().strip()) and bool(self._root)
            and bool(self._keys))

    def _choose_folder(self, _button):
        dialog = Gtk.FileDialog(title=_("repo_folder"))
        dialog.select_folder(self, None, self._folder_chosen)

    def _folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder and folder.get_path():
            self._root = folder.get_path()
            self.folder_row.set_subtitle(self._root)
            self._validate()

    def _choose_target(self, _row):
        dialog = Gtk.FileDialog(title=_("repo_target"))
        dialog.select_folder(self, None, self._target_chosen)

    def _target_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder and folder.get_path():
            self.target_row.set_text(folder.get_path())

    def _create_key(self, _row):
        user = _os.environ.get("USER", "developer")

        def created():
            from . import gpgsign
            self._keys = gpgsign.list_keys_detailed()
            labels = [f'{k["uid"]}  ·  {k["fpr"][-16:]}' for k in self._keys] \
                or [_("no_key")]
            self.key_row.set_model(Gtk.StringList.new(labels))
            if self._keys:
                self.key_row.set_selected(len(self._keys) - 1)
            self._validate()

        GpgKeyDialog(self, default_name=user,
                     default_email=f"{user}@localhost",
                     on_done=created).present()

    def _on_save(self, _button):
        method = self._methods[self.method_row.get_selected()]
        key = ""
        if self._keys:
            index = min(self.key_row.get_selected(), len(self._keys) - 1)
            key = self._keys[index]["fpr"]

        profile = {
            "name": self.name_row.get_text().strip(),
            "root": self._root,
            "base_url": self.url_row.get_text().strip(),
            "suite": self.suite_row.get_text().strip(),
            "component": self.component_row.get_text().strip(),
            "archs": self.archs_row.get_text(),
            "origin": self.origin_row.get_text().strip(),
            "label": self.label_row.get_text().strip(),
            "description": self.desc_row.get_text().strip(),
            "key": key,
            "publish_method": method,
            "publish_target": self.target_row.get_text().strip(),
        }
        try:
            stored = self._repo.save_profile(profile, self._previous)
            # The empty tree is created now rather than at the first build:
            # a folder that looks like a repository is easier to recognise
            # than an empty one the user has to trust is being used.
            self._repo.ensure_layout(stored)
        except (self._repo.RepoError, OSError) as e:
            self.folder_row.set_subtitle(str(e))
            return
        self.close()
        self._on_saved(stored)


class PreferencesDialog(Adw.PreferencesWindow):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title=_("settings"))
        self._parent = parent

        page = Adw.PreferencesPage(title=_("general"),
                                   icon_name="preferences-system-symbolic")

        # -- General ---------------------------------------------------------
        general = Adw.PreferencesGroup(title=_("general"))

        lang_model = Gtk.StringList.new([label for _c, label in LANGUAGES])
        self.lang_row = Adw.ComboRow(title=_("language"), model=lang_model)
        codes = [c for c, _l in LANGUAGES]
        self.lang_row.set_selected(codes.index(config.get("language"))
                                   if config.get("language") in codes else 0)
        self.lang_row.connect("notify::selected", self._on_language)
        general.add(self.lang_row)

        theme_model = Gtk.StringList.new(
            [_("theme_system"), _("theme_light"), _("theme_dark")])
        self.theme_row = Adw.ComboRow(title=_("theme"), model=theme_model)
        self.theme_row.set_selected(THEMES.index(config.get("theme"))
                                    if config.get("theme") in THEMES else 0)
        self.theme_row.connect("notify::selected", self._on_theme)
        general.add(self.theme_row)

        self.focus_row = Adw.SwitchRow(title=_("focus_mode"),
                                       active=config.get("focus_mode"))
        self.focus_row.connect(
            "notify::active",
            lambda r, _p: config.set("focus_mode", r.get_active()))
        general.add(self.focus_row)

        self.shrink_row = Adw.SwitchRow(
            title=_("shrink_setting"),
            subtitle=_("shrink_setting_hint"),
            subtitle_lines=3,
            active=config.get("drive_shrink_warning"))
        self.shrink_row.connect(
            "notify::active",
            lambda r, _p: config.set("drive_shrink_warning", r.get_active()))
        general.add(self.shrink_row)

        self.net_row = Adw.SwitchRow(
            title=_("sandbox_net_setting"),
            subtitle=_("sandbox_net_setting_hint"),
            subtitle_lines=3,
            active=config.get("sandbox_network"))
        self.net_row.connect(
            "notify::active",
            lambda r, _p: config.set("sandbox_network", r.get_active()))
        general.add(self.net_row)

        self.esc_row = Adw.SwitchRow(title=_("esc_setting"),
                                     active=config.get("esc_confirm"))
        self.esc_row.connect(
            "notify::active",
            lambda r, _p: config.set("esc_confirm", r.get_active()))
        general.add(self.esc_row)
        page.add(general)

        # -- Interface (GNOME ⇄ KDE Plasma) ------------------------------------
        ui_group = Adw.PreferencesGroup(title=_("interface_group"))
        ui_row = Adw.ActionRow(title=_("ui_variant"), subtitle=_("ui_gnome"))
        ui_row.add_prefix(Gtk.Image(icon_name="preferences-desktop-theme-symbolic"))
        switch_btn = Gtk.Button(label=f'{_("switch_ui")} \u2192 {_("ui_kde")}',
                                valign=Gtk.Align.CENTER)
        switch_btn.connect("clicked", lambda *a: self._switch_ui("kde"))
        ui_row.add_suffix(switch_btn)
        ui_group.add(ui_row)
        page.add(ui_group)

        # -- Autosave ---------------------------------------------------------
        autosave = Adw.PreferencesGroup(title=_("autosave"))
        self.auto_switch = Adw.SwitchRow(title=_("autosave"),
                                         active=config.get("autosave_enabled"))
        self.auto_switch.connect("notify::active", self._on_autosave)
        autosave.add(self.auto_switch)

        adj = Gtk.Adjustment(lower=1, upper=120, step_increment=1,
                             value=config.get("autosave_minutes"))
        self.interval_row = Adw.SpinRow(title=_("autosave_interval"),
                                        adjustment=adj)
        self.interval_row.connect("notify::value", self._on_interval)
        autosave.add(self.interval_row)
        page.add(autosave)

        # -- GitHub -----------------------------------------------------------
        gh = Adw.PreferencesGroup(title=_("github"))
        user = config.get("github_user")
        self.gh_row = Adw.ActionRow(
            title=_("logged_in_as", u=user) if user else _("not_logged_in"))
        self.gh_row.add_prefix(brand_image(
            "github", 28, fallback_icon="system-users-symbolic"))
        self.gh_btn = Gtk.Button(valign=Gtk.Align.CENTER,
                                 label=_("logout") if user else _("login"))
        self.gh_btn.connect("clicked", self._on_github)
        self.gh_row.add_suffix(self.gh_btn)
        gh.add(self.gh_row)

        from . import secrets as _sec
        store_row = Adw.ActionRow(
            title=_("token_keyring") if _sec.available() else _("token_file"),
            subtitle_lines=2)
        store_row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
        gh.add(store_row)

        self.cid_row = Adw.EntryRow(title=_("client_id_setting"),
                                    text=config.get("github_client_id"))
        self.cid_row.connect(
            "changed",
            lambda r: config.set("github_client_id", r.get_text().strip()))
        gh.add(self.cid_row)
        page.add(gh)

        # -- Google Drive -------------------------------------------------------
        from . import gdrive
        gd = Adw.PreferencesGroup(title=_("gdrive"),
                                  description=_("gdrive_hint"))
        is_conn = gdrive.connected()
        self.gd_row = Adw.ActionRow(
            title=_("signed_google") if is_conn else _("not_logged_in"),
            subtitle=config.get("gdrive_email") if is_conn else "")
        self.gd_row.add_prefix(brand_image(
            "gdrive", 28, fallback_icon="folder-remote-symbolic"))
        self.gd_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.gd_btn.set_label(_("disconnect") if is_conn
                              else _("connect_gdrive"))
        self.gd_btn.connect("clicked", self._on_gdrive)
        self.gd_row.add_suffix(self.gd_btn)
        gd.add(self.gd_row)

        self.gd_import_row = Adw.ActionRow(title=_("drive_import"),
                                           activatable=True,
                                           visible=is_conn)
        self.gd_import_row.add_prefix(
            Gtk.Image(icon_name="folder-download-symbolic"))
        self.gd_import_row.connect(
            "activated",
            lambda *a: self._parent.import_drive_projects(
                auto=False, parent_window=self))
        gd.add(self.gd_import_row)

        # bringing in the other computers' changes — on unless turned off
        self.autopull_row = Adw.SwitchRow(
            title=_("autopull_setting"), subtitle=_("autopull_setting_hint"),
            subtitle_lines=3, active=config.get("drive_autopull"))
        self.autopull_row.add_prefix(Gtk.Image(
            icon_name="emblem-synchronizing-symbolic"))
        gd.add(self.autopull_row)
        minutes = AUTOPULL_MINUTES
        self.every_row = Adw.ComboRow(
            title=_("autopull_every"),
            model=Gtk.StringList.new([_("every_min", n=m) for m in minutes]),
            sensitive=config.get("drive_autopull"))
        self.every_row.add_prefix(Gtk.Image(icon_name="alarm-symbolic"))
        current = config.get("drive_autopull_minutes")
        self.every_row.set_selected(minutes.index(current)
                                    if current in minutes else 1)
        gd.add(self.every_row)

        def autopull_changed(*_a):
            config.set("drive_autopull", self.autopull_row.get_active())
            config.set("drive_autopull_minutes",
                       minutes[self.every_row.get_selected()])
            self.every_row.set_sensitive(self.autopull_row.get_active())
            if hasattr(self._parent, "restart_autopull"):
                self._parent.restart_autopull(
                    first_delay=2 if self.autopull_row.get_active() else None)
        self.autopull_row.connect("notify::active", autopull_changed)
        self.every_row.connect("notify::selected", autopull_changed)

        keep = BACKUP_DAYS
        self.keep_row = Adw.ComboRow(
            title=_("backup_keep"),
            model=Gtk.StringList.new([_("keep_days", n=d) if d
                                      else _("keep_forever") for d in keep]))
        self.keep_row.add_prefix(Gtk.Image(icon_name="security-high-symbolic"))
        days = config.get("drive_backup_days")
        self.keep_row.set_selected(keep.index(days) if days in keep else 1)
        self.keep_row.connect("notify::selected", lambda r, _p: config.set(
            "drive_backup_days", keep[r.get_selected()]))
        gd.add(self.keep_row)

        backups_row = Adw.ActionRow(title=_("backups_show"), activatable=True)
        backups_row.add_prefix(Gtk.Image(icon_name="document-open-recent-symbolic"))
        backups_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        backups_row.connect("activated", lambda *a: config.active_project()
                            and BackupsWindow(self, config.active_project())
                            .present())
        gd.add(backups_row)
        page.add(gd)

        if is_conn:
            self._refresh_gdrive_email()

        self.add(page)

    def _on_language(self, row, _pspec):
        code = LANGUAGES[row.get_selected()][0]
        if code == config.get("language"):
            return
        config.set("language", code)
        _.set_language(code)
        self.set_title(_("settings"))

        # Some widgets are built once and keep their original labels, so the
        # only way to translate everything is to start the app again.
        dialog = Adw.MessageDialog(transient_for=self,
                                   heading=_("lang_restart_q"),
                                   body=_("lang_restart_detail"))
        dialog.add_response("later", _("restart_later"))
        dialog.add_response("restart", _("restart_now"))
        dialog.set_response_appearance("restart",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("restart")

        def responded(_d, response):
            if response == "restart":
                from . import launcher
                if not launcher.relaunch_current() and self._parent:
                    self._parent.toast(_("restart_failed"))

        dialog.connect("response", responded)
        dialog.present()

    def _on_theme(self, row, _pspec):
        theme = THEMES[row.get_selected()]
        config.set("theme", theme)

        def apply_scheme():
            Adw.StyleManager.get_default().set_color_scheme({
                "system": Adw.ColorScheme.DEFAULT,
                "light": Adw.ColorScheme.FORCE_LIGHT,
                "dark": Adw.ColorScheme.FORCE_DARK,
            }[theme])

        if self._parent and hasattr(self._parent, "animate_theme_change"):
            self._parent.animate_theme_change(apply_scheme)
        else:
            apply_scheme()

    def _on_autosave(self, row, _pspec):
        config.set("autosave_enabled", row.get_active())
        if self._parent:
            self._parent.restart_autosave()

    def _on_interval(self, row, _pspec):
        config.set("autosave_minutes", int(row.get_value()))
        if self._parent:
            self._parent.restart_autosave()

    def _on_gdrive(self, _btn):
        from . import gdrive
        from .util import run_async
        if not gdrive.available():
            self.gd_row.set_title(_("rclone_missing"))
            return
        if gdrive.connected():
            try:
                gdrive.disconnect()
            except gdrive.DriveError:
                pass
            config.set("gdrive_email", "")
            self.gd_row.set_title(_("not_logged_in"))
            self.gd_row.set_subtitle("")
            self.gd_btn.set_label(_("connect_gdrive"))
            self.gd_import_row.set_visible(False)
            return
        self.gd_btn.set_sensitive(False)
        self.gd_row.set_title(_("gdrive_wait"))

        def done(_r, error):
            self.gd_btn.set_sensitive(True)
            if gdrive.connected():
                self.gd_row.set_title(_("signed_google"))
                self.gd_btn.set_label(_("disconnect"))
                self.gd_import_row.set_visible(True)
                self._refresh_gdrive_email()
                # second-computer flow: offer the Drive projects right away
                if self._parent:
                    self._parent.import_drive_projects(
                        auto=True, parent_window=self)
            else:
                self.gd_row.set_title(str(error) if error
                                      else _("drive_failed"))

        run_async(gdrive.connect, done)

    def _refresh_gdrive_email(self):
        from . import gdrive
        from .util import run_async

        def got(email, _err):
            if email:
                config.set("gdrive_email", email)
                self.gd_row.set_subtitle(email)

        cached = config.get("gdrive_email")
        if cached:
            self.gd_row.set_subtitle(cached)
        run_async(gdrive.account_email, got)

    def _switch_ui(self, target):
        from . import launcher
        available = (launcher.qt_available() if target == "kde"
                    else launcher.gtk_available())
        ui_label = _("ui_kde") if target == "kde" else _("ui_gnome")
        if not available:
            self._offer_install(target, ui_label)
            return
        self._confirm_switch(target, ui_label)

    def _offer_install(self, target, ui_label):
        from . import launcher
        cmd = launcher.KDE_INSTALL_CMD if target == "kde" \
            else launcher.GNOME_INSTALL_CMD
        packages = (launcher.KDE_PACKAGES if target == "kde"
                   else launcher.GNOME_PACKAGES)

        dialog = Adw.MessageDialog(transient_for=self, heading=ui_label,
                                   body=_("ui_missing", ui=ui_label, cmd=cmd))
        dialog.add_response("cancel", _("cancel"))
        if launcher.pkexec_available():
            dialog.add_response("install", _("install_now"))
            dialog.set_response_appearance(
                "install", Adw.ResponseAppearance.SUGGESTED)

        def responded(_d, response):
            if response == "install":
                self._run_install(target, ui_label, packages)

        dialog.connect("response", responded)
        dialog.present()

    def _run_install(self, target, ui_label, packages):
        from . import launcher
        if self._parent:
            self._parent.toast(_("installing_pkgs", ui=ui_label))

        def done(result, _err):
            ok, output = result
            if ok:
                if self._parent:
                    self._parent.toast(_("install_done", ui=ui_label))
                self._confirm_switch(target, ui_label)
            else:
                cmd = (launcher.KDE_INSTALL_CMD if target == "kde"
                      else launcher.GNOME_INSTALL_CMD)
                info = Adw.MessageDialog(
                    transient_for=self, heading=ui_label,
                    body=_("install_failed_ui", cmd=cmd) + f"\n\n{output}")
                info.add_response("ok", _("close"))
                info.present()

        run_async(lambda: launcher.install_packages(packages), done)

    def _confirm_switch(self, target, ui_label):
        from . import launcher
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("switch_ui_q", ui=ui_label),
            body=_("switch_ui_detail", ui=ui_label))
        dialog.add_response("cancel", _("cancel"))
        dialog.add_response("switch", _("switch_ui"))
        dialog.set_response_appearance("switch",
                                       Adw.ResponseAppearance.SUGGESTED)

        def responded(_d, response):
            if response == "switch":
                launcher.relaunch_as(target)

        dialog.connect("response", responded)
        dialog.present()

    def _on_github(self, _btn):
        if config.get("github_user"):
            config.set("github_token", "")
            config.set("github_user", "")
            self.gh_row.set_title(_("not_logged_in"))
            self.gh_btn.set_label(_("login"))
        else:
            def refreshed():
                user = config.get("github_user")
                if user:
                    self.gh_row.set_title(_("logged_in_as", u=user))
                    self.gh_btn.set_label(_("logout"))
            GitHubLoginDialog(self, on_done=refreshed).present()


# --------------------------------------------------------------------------- #
# Setup wizard — DaVinci/Creo-style first-run installer screen, GNOME-native
# --------------------------------------------------------------------------- #
COPYRIGHT = "© Petacomm I.S. 2026 All Rights Reserved"


class SetupWizard(Adw.Window):
    def __init__(self, parent, on_done):
        super().__init__(transient_for=parent, modal=True,
                         default_width=660, default_height=600,
                         resizable=False)
        self._parent = parent
        self._on_done = on_done
        self.add_css_class("petacore-setup")
        self.connect("close-request", self._finish)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        header = Adw.HeaderBar(css_classes=["flat"],
                               show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Petacore Setup"))
        root.append(header)

        self.carousel = Adw.Carousel(vexpand=True,
                                     allow_scroll_wheel=False,
                                     allow_long_swipes=False)
        for builder in (self._page_welcome, self._page_github,
                        self._page_drive, self._page_drive_projects,
                        self._page_features,
                        self._page_project):
            self.carousel.append(builder())
        root.append(self.carousel)

        dots = Adw.CarouselIndicatorDots(carousel=self.carousel)
        root.append(dots)

        # navigation + slogan footer
        nav = Gtk.Box(spacing=8, margin_top=8, margin_bottom=4,
                      margin_start=20, margin_end=20)
        self.back_btn = Gtk.Button(label=_("back"))
        self.back_btn.connect("clicked", lambda *a: self._go(-1))
        nav.append(self.back_btn)
        nav.append(Gtk.Box(hexpand=True))
        self.skip_btn = Gtk.Button(label=_("skip"), css_classes=["flat"])
        self.skip_btn.connect("clicked", lambda *a: self._go(+1))
        nav.append(self.skip_btn)
        self.next_btn = Gtk.Button(label=_("next"),
                                   css_classes=["suggested-action", "pill"])
        self.next_btn.connect("clicked", self._on_next)
        nav.append(self.next_btn)
        root.append(nav)

        root.append(Gtk.Label(label=COPYRIGHT, margin_top=6, margin_bottom=10,
                              css_classes=["dim-label", "caption"]))

        self.set_content(root)
        self.carousel.connect("page-changed", self._page_changed)
        self._page_changed(self.carousel, 0)

    # -- page frame ----------------------------------------------------------
    def _card(self, *children):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=24, margin_bottom=24,
                       margin_start=36, margin_end=36,
                       valign=Gtk.Align.CENTER, vexpand=True,
                       css_classes=["petacore-setup-card"])
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                        margin_top=28, margin_bottom=28,
                        margin_start=28, margin_end=28)
        for child in children:
            inner.append(child)
        card.append(inner)
        return card

    def _title(self, text):
        return Gtk.Label(label=text, css_classes=["title-2"], wrap=True,
                         justify=Gtk.Justification.CENTER)

    def _body(self, text):
        return Gtk.Label(label=text, wrap=True, css_classes=["dim-label"],
                         justify=Gtk.Justification.CENTER,
                         max_width_chars=54)

    # -- pages ------------------------------------------------------------------
    def _page_welcome(self):
        logo = Gtk.Image(icon_name="io.petacore.Petacore", pixel_size=96)
        codes = [c for c, _l in LANGUAGES]
        drop = Gtk.DropDown.new_from_strings([l for _c, l in LANGUAGES])
        drop.set_halign(Gtk.Align.CENTER)
        if config.get("language") in codes:
            drop.set_selected(codes.index(config.get("language")))

        def lang_changed(d, _p):
            code = codes[d.get_selected()]
            if code != config.get("language"):
                config.set("language", code)
                _.set_language(code)
                self.close()
                SetupWizard(self._parent, self._on_done).present()

        drop.connect("notify::selected", lang_changed)
        return self._card(logo, self._title(_("welcome_title")),
                          self._body(_("setup_welcome_body")), drop)

    def _page_github(self):
        icon = brand_image("github", 64,
                           fallback_icon="system-users-symbolic")
        self.gh_status = Gtk.Label(css_classes=["success"])
        btn = Gtk.Button(label=_("github_login"), halign=Gtk.Align.CENTER,
                         css_classes=["suggested-action", "pill"])
        btn.connect("clicked", self._gh_login)
        self._refresh_gh()
        return self._card(icon, self._title(_("setup_gh_title")),
                          self._body(_("setup_gh_body")), btn,
                          self.gh_status)

    def _refresh_gh(self):
        user = config.get("github_user")
        self.gh_status.set_text(
            _("logged_in_as", u=user) if user else "")

    def _gh_login(self, _btn):
        GitHubLoginDialog(self, on_done=self._refresh_gh).present()

    def _page_drive(self):
        icon = brand_image("gdrive", 64,
                           fallback_icon="folder-remote-symbolic")
        self.gd_status = Gtk.Label(css_classes=["success"], wrap=True)
        btn = Gtk.Button(label=_("connect_gdrive"), halign=Gtk.Align.CENTER,
                         css_classes=["suggested-action", "pill"])
        btn.connect("clicked", self._gd_connect, btn)
        from . import gdrive
        if gdrive.connected():
            self.gd_status.set_text(_("gdrive_connected"))
        return self._card(icon, self._title(_("setup_gd_title")),
                          self._body(_("setup_gd_body")), btn,
                          self.gd_status)

    def _gd_connect(self, _b, btn):
        from . import gdrive
        from .util import run_async
        if not gdrive.available():
            self.gd_status.set_text(_("rclone_missing"))
            return
        btn.set_sensitive(False)
        self.gd_status.set_text(_("gdrive_wait"))

        def done(_r, error):
            btn.set_sensitive(True)
            if gdrive.connected():
                self.gd_status.set_text(_("gdrive_connected"))
                if self._parent:
                    self._parent.import_drive_projects(
                        auto=True, parent_window=self)
            else:
                self.gd_status.set_text(str(error) if error
                                        else _("drive_failed"))

        run_async(gdrive.connect, done)

    def _page_drive_projects(self):
        """Explicit step: 'We found X and Y in your Drive — fetch them?'"""
        self.found_label = Gtk.Label(wrap=True,
                                     justify=Gtk.Justification.CENTER,
                                     css_classes=["dim-label"])
        self.found_list = Gtk.Label(wrap=True,
                                    justify=Gtk.Justification.CENTER)
        self.found_btn = Gtk.Button(label=_("import_all"),
                                    halign=Gtk.Align.CENTER,
                                    sensitive=False,
                                    css_classes=["suggested-action", "pill"])
        self.found_btn.connect("clicked", self._do_drive_import)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        holder.append(self.found_label)
        holder.append(self.found_list)
        holder.append(self.found_btn)
        refresh = Gtk.Button(label=_("refresh"), halign=Gtk.Align.CENTER,
                             css_classes=["flat"])
        refresh.connect("clicked", lambda *a: self._scan_drive())
        holder.append(refresh)
        GLib.idle_add(self._scan_drive)
        return self._card(self._title(_("drive_import")), holder)

    def _scan_drive(self):
        from . import gdrive
        from .util import run_async as _ra
        if not gdrive.connected():
            self.found_label.set_text(_("not_logged_in"))
            self.found_btn.set_sensitive(False)
            return False
        self.found_label.set_text("…")

        def done(names, error):
            if error or not names:
                self.found_label.set_text(_("no_new_drive"))
                self.found_btn.set_sensitive(False)
                return
            existing = {p["name"].lower() for p in config.get("projects")}
            new = [n for n in names if n.lower() not in existing]
            if not new:
                self.found_label.set_text(_("no_new_drive"))
                self.found_btn.set_sensitive(False)
                return
            self._new_projects = new
            self.found_label.set_text(_("drive_found", n=len(new)) + "\n"
                                      + _("drive_import_body"))
            self.found_list.set_text("\u2022 " + "\n\u2022 ".join(new[:12]))
            self.found_btn.set_sensitive(True)

        _ra(gdrive.list_remote_projects, done)
        return False

    def _do_drive_import(self, _btn):
        names = getattr(self, "_new_projects", [])
        if names and self._parent:
            self.found_btn.set_sensitive(False)
            self.found_label.set_text(_("drive_syncing"))
            self._parent._import_projects(names)

    def _page_features(self):
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        icons = ["folder-symbolic", "edit-paste-symbolic",
                 "document-save-symbolic", "package-x-generic-symbolic"]
        for i, key in enumerate(("feat_1", "feat_2", "feat_3", "feat_4")):
            row = Gtk.Box(spacing=12)
            row.append(Gtk.Image(icon_name=icons[i], pixel_size=22))
            row.append(Gtk.Label(label=_(key), wrap=True, xalign=0,
                                 hexpand=True))
            rows.append(row)
        return self._card(self._title(_("setup_feat_title")), rows)

    def _page_project(self):
        icon = Gtk.Image(icon_name="list-add-symbolic", pixel_size=64)
        btn = Gtk.Button(label=_("new_project"), halign=Gtk.Align.CENTER,
                         css_classes=["suggested-action", "pill"])
        btn.connect("clicked", self._create_project)
        return self._card(icon, self._title(_("setup_first_title")),
                          self._body(_("setup_first_body")), btn)

    def _create_project(self, _btn):
        def created():
            if self._parent:
                self._parent._project_created()
            self._finish()
        NewProjectDialog(self, on_created=created).present()

    # -- navigation ---------------------------------------------------------------
    def _pos(self):
        return round(self.carousel.get_position())

    def _go(self, delta):
        target = self._pos() + delta
        n = self.carousel.get_n_pages()
        if 0 <= target < n:
            self.carousel.scroll_to(self.carousel.get_nth_page(target), True)
        elif target >= n:
            self._finish()

    def _on_next(self, *_a):
        if self._pos() >= self.carousel.get_n_pages() - 1:
            self._finish()
        else:
            self._go(+1)

    def _page_changed(self, carousel, index):
        index = round(index) if not isinstance(index, int) else index
        last = index >= carousel.get_n_pages() - 1
        self.back_btn.set_visible(index > 0)
        self.skip_btn.set_visible(index in (1, 2))
        self.next_btn.set_label(_("finish") if last else _("next"))

    def _finish(self, *_a):
        config.set("first_run", False)
        self._on_done()
        self.destroy()
        return False
