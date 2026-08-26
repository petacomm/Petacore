"""Dialogs for Petacore: first-run language chooser, New Project wizard,
GitHub login, and application preferences."""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import github, gitops  # noqa: E402
from .config import config  # noqa: E402
from .i18n import LANGUAGES, translator as _  # noqa: E402
from .util import run_async  # noqa: E402


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


def brand_image(name, size=64, fallback_icon="image-missing-symbolic"):
    """Gtk.Image for petacore/brand-icons/<name>.svg|png, or the fallback
    themed icon when the asset is missing."""
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
            else:
                if not gitops.is_repo(dest):
                    gitops.init(dest, repo_url)
            return dest

        def done(result, error):
            self.spinner.stop()
            if error:
                self.status_label.set_text(str(error))
                self.create_btn.set_sensitive(True)
                return
            config.add_project(name, result, repo_url)
            mode, secret = choice
            if mode != "none":
                from . import crypto
                crypto.enable(result, mode, secret)
            self.close()
            self._on_created()

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
    def __init__(self, parent, on_done=None):
        super().__init__(transient_for=parent, modal=True,
                         default_width=520, default_height=560,
                         title=_("github_login"))
        self._on_done = on_done or (lambda: None)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("cancel"))
        cancel.connect("clicked", lambda *a: self.close())
        header.pack_start(cancel)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        client_id = (config.get("github_client_id") or "").strip()

        if client_id:
            # -- Ready: "Sign in with GitHub" is the one obvious action ---------
            oauth_group = Adw.PreferencesGroup()
            oauth_row = Adw.ActionRow(title=_("sign_in_github"),
                                      subtitle=_("oauth_hint"),
                                      subtitle_lines=2)
            oauth_row.add_prefix(brand_image(
                "github", 32, fallback_icon="web-browser-symbolic"))
            self.oauth_btn = Gtk.Button(label=_("login"),
                                        valign=Gtk.Align.CENTER,
                                        css_classes=["suggested-action",
                                                     "pill"])
            self.oauth_btn.connect("clicked", self._on_oauth)
            oauth_row.add_suffix(self.oauth_btn)
            oauth_group.add(oauth_row)

            self.code_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=4, visible=False,
                                    margin_top=10, margin_bottom=4,
                                    halign=Gtk.Align.CENTER)
            self.code_hint = Gtk.Label(label=_("device_enter_code"),
                                       css_classes=["dim-label"])
            self.code_label = Gtk.Label(css_classes=["title-1"],
                                        selectable=True)
            self.code_wait = Gtk.Label(label=_("device_waiting"),
                                       css_classes=["dim-label", "caption"])
            self.code_box.append(self.code_hint)
            self.code_box.append(self.code_label)
            self.code_box.append(self.code_wait)
            oauth_group.add(self.code_box)
            page.add(oauth_group)
        else:
            # -- Not ready: a friendly, self-contained one-time setup card ------
            self.oauth_btn = None
            setup_group = Adw.PreferencesGroup(
                title=_("sign_in_github"),
                description=_("one_time_setup") + "\n" + _("setup_steps"))
            open_row = Adw.ActionRow(title=_("open_github_setup"),
                                     activatable=True)
            open_row.add_prefix(brand_image(
                "github", 24, fallback_icon="web-browser-symbolic"))
            open_row.add_suffix(
                Gtk.Image(icon_name="adw-external-link-symbolic"))
            open_row.connect(
                "activated", lambda *a: Gio.AppInfo.launch_default_for_uri(
                    "https://github.com/settings/applications/new", None))
            setup_group.add(open_row)

            self.client_id_row = Adw.EntryRow(title=_("paste_client_id"))
            setup_group.add(self.client_id_row)

            save_row = Adw.ActionRow()
            self.save_id_btn = Gtk.Button(
                label=_("save_and_signin"), halign=Gtk.Align.END,
                valign=Gtk.Align.CENTER,
                css_classes=["suggested-action"])
            self.save_id_btn.connect("clicked", self._on_save_client_id)
            save_row.add_suffix(self.save_id_btn)
            setup_group.add(save_row)
            page.add(setup_group)

            # placeholders so the shared code-flow methods still work
            self.code_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    visible=False, halign=Gtk.Align.CENTER)
            self.code_hint = Gtk.Label(label=_("device_enter_code"))
            self.code_label = Gtk.Label(css_classes=["title-1"])
            self.code_wait = Gtk.Label(label=_("device_waiting"))
            self.code_box.append(self.code_hint)
            self.code_box.append(self.code_label)
            self.code_box.append(self.code_wait)
            code_group = Adw.PreferencesGroup()
            code_row = Adw.ActionRow()
            code_row.set_child(self.code_box)
            code_group.add(code_row)
            page.add(code_group)

        # Status message right below the primary action — always visible.
        status_group = Adw.PreferencesGroup()
        self.status = Gtk.Label(css_classes=["warning"], wrap=True,
                                justify=Gtk.Justification.CENTER)
        status_group.add(self.status)
        page.add(status_group)

        # -- Secondary options, tucked away so they're not clicked by mistake --
        other_group = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title=_("other_signin"))
        other_group.add(expander)
        page.add(other_group)

        gh_row = Adw.ActionRow(title=_("sign_in_gh"), subtitle=_("gh_hint"),
                               subtitle_lines=2)
        gh_row.add_prefix(Gtk.Image(icon_name="emblem-default-symbolic",
                                    css_classes=["success"]))
        self.gh_btn = Gtk.Button(label=_("login"), valign=Gtk.Align.CENTER)
        self.gh_btn.connect("clicked", self._on_gh_login)
        gh_row.add_suffix(self.gh_btn)
        if not github.gh_available():
            gh_row.set_subtitle(_("gh_not_ready"))
            gh_row.set_subtitle_lines(3)
        expander.add_row(gh_row)

        browser_row = Adw.ActionRow(title=_("create_token_browser"),
                                    activatable=True,
                                    subtitle=_("token_explain"),
                                    subtitle_lines=3)
        browser_row.add_prefix(Gtk.Image(icon_name="web-browser-symbolic"))
        browser_row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        browser_row.connect("activated", self._open_token_page)
        expander.add_row(browser_row)

        self.token_row = Adw.PasswordEntryRow(title=_("or_paste_token"))
        expander.add_row(self.token_row)

        login_row = Adw.ActionRow()
        self.login_btn = Gtk.Button(label=_("login"), halign=Gtk.Align.END,
                                    valign=Gtk.Align.CENTER)
        self.login_btn.connect("clicked", self._on_login)
        login_row.add_suffix(self.login_btn)
        expander.add_row(login_row)

        self.toaster = Adw.ToastOverlay()
        self.toaster.set_child(page)
        toolbar.set_content(self.toaster)
        self.set_content(toolbar)

    def _msg(self, text):
        """Show feedback both inline (top) and as a toast."""
        self.status.set_text(text)
        self.toaster.add_toast(Adw.Toast(title=text[:120], timeout=5))

    def _open_token_page(self, *_a):
        Gio.AppInfo.launch_default_for_uri(github.TOKEN_URL, None)

    def _on_save_client_id(self, _btn):
        client_id = self.client_id_row.get_text().strip()
        if not client_id:
            return
        config.set("github_client_id", client_id)
        self.save_id_btn.set_sensitive(False)
        self.status.set_text("…")
        run_async(lambda: github.device_start(client_id),
                  lambda data, err: self._device_started(client_id, data, err))

    # -- Sign in with GitHub (device flow) -------------------------------------
    def _on_oauth(self, _btn):
        client_id = (config.get("github_client_id") or "").strip()
        if not client_id:
            self._msg(_("client_id_missing"))
            return
        self.oauth_btn.set_sensitive(False)
        self.status.set_text("…")
        run_async(lambda: github.device_start(client_id),
                  lambda data, err: self._device_started(client_id, data, err))

    def _device_started(self, client_id, data, error):
        if error:
            if self.oauth_btn:
                self.oauth_btn.set_sensitive(True)
            else:
                self.save_id_btn.set_sensitive(True)
            self._msg(str(error))
            return
        self.status.set_text("")
        self.code_label.set_text(data["user_code"])
        self.code_box.set_visible(True)
        Gio.AppInfo.launch_default_for_uri(
            data.get("verification_uri", "https://github.com/login/device"),
            None)

        run_async(
            lambda: github.device_poll(client_id, data["device_code"],
                                       data.get("interval", 5),
                                       data.get("expires_in", 900)),
            self._device_polled)

    def _device_polled(self, token, error):
        self.code_box.set_visible(False)
        if error or not token:
            if self.oauth_btn:
                self.oauth_btn.set_sensitive(True)
            else:
                self.save_id_btn.set_sensitive(True)
            self._msg(str(error) if error else _("login_failed"))
            return
        self._finish_with_token(token)

    def _finish_with_token(self, token):
        def done(user, error):
            self.gh_btn.set_sensitive(True)
            self.login_btn.set_sensitive(True)
            if error or not user:
                self._msg(_("login_failed"))
                return
            config.set("github_token", token)
            config.set("github_user", user)
            self.close()
            self._on_done()
        run_async(lambda: github.validate_token(token), done)

    def _on_gh_login(self, _btn):
        self.gh_btn.set_sensitive(False)
        self.status.set_text("…")

        def done(token, _error):
            if not token:
                self.gh_btn.set_sensitive(True)
                self._msg(_("gh_not_ready"))
                return
            self._finish_with_token(token)

        run_async(github.gh_token, done)

    def _on_login(self, _btn):
        token = self.token_row.get_text().strip()
        if not token:
            self._msg(_("or_paste_token"))
            return
        self.login_btn.set_sensitive(False)
        self.status.set_text("…")
        self._finish_with_token(token)


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
