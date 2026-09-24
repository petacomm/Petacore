"""Qt (KDE Plasma) dialogs for Petacore: setup wizard, project creation,
GitHub sign-in, settings and GPG key creation."""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget,
                               QWizard, QWizardPage)

from .. import github, gitops
from ..config import config
from ..i18n import LANGUAGES, translator as _

COPYRIGHT = "© Petacomm I.S. 2026 All Rights Reserved"
# Cloud-encryption strengths offered in the New Project and Edit Project
# dialogs (the local project is never encrypted).
ENC_MODES = ["none", "pin4", "pin8", "password"]
ASSET_DIRS = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "brand-icons"),
]
ICON_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "icons")


def brand_pixmap(name, size=64):
    for d in ASSET_DIRS:
        for ext in (".svg", ".png"):
            path = os.path.join(d, name + ext)
            if os.path.isfile(path):
                pm = QPixmap(path)
                if not pm.isNull():
                    return pm.scaled(size, size, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
    return None


def brand_icon(name):
    """QIcon for one of the bundled vector assets (brand-icons/<name>.svg)."""
    from PySide6.QtGui import QIcon
    for d in ASSET_DIRS:
        for ext in (".svg", ".png"):
            path = os.path.join(d, name + ext)
            if os.path.isfile(path):
                icon = QIcon(path)
                if not icon.isNull():
                    return icon
    return None


def app_pixmap(size=96):
    path = os.path.join(ICON_DIR, "512x512", "io.petacore.Petacore.png")
    if os.path.isfile(path):
        pm = QPixmap(path)
        if not pm.isNull():
            return pm.scaled(size, size, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
    return None


# --------------------------------------------------------------------------- #
# New project
# --------------------------------------------------------------------------- #
class NewProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("new_project"))
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit()
        form.addRow(_("project_name"), self.name)
        self.repo = QLineEdit()
        self.repo.setPlaceholderText(_("repo_hint"))
        form.addRow(_("repo_url"), self.repo)

        loc_row = QHBoxLayout()
        self.location = QLineEdit(
            os.path.join(os.path.expanduser("~"), "Projects"))
        loc_row.addWidget(self.location, 1)
        browse = QPushButton(_("browse"))
        browse.clicked.connect(self._browse)
        loc_row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(loc_row)
        form.addRow(_("location"), holder)
        layout.addLayout(form)

        # -- Encryption, offered right when the project is created ----------
        enc_box = QGroupBox(_("encryption"))
        enc_layout = QVBoxLayout(enc_box)
        explain = QLabel(_("encrypt_explain"))
        explain.setWordWrap(True)
        explain.setProperty("dim", True)
        enc_layout.addWidget(explain)
        enc_form = QFormLayout()
        self.enc_mode = QComboBox()
        self.enc_mode.addItems([_("enc_none"), _("enc_pin4"), _("enc_pin8"),
                                _("enc_password")])
        self.enc_mode.currentIndexChanged.connect(self._enc_changed)
        enc_form.addRow(_("encrypt_upload"), self.enc_mode)
        self.enc_secret = QLineEdit()
        self.enc_secret.setEchoMode(QLineEdit.Password)
        self.enc_secret.setEnabled(False)
        enc_form.addRow(_("enc_secret"), self.enc_secret)
        self.enc_secret2 = QLineEdit()
        self.enc_secret2.setEchoMode(QLineEdit.Password)
        self.enc_secret2.setEnabled(False)
        enc_form.addRow(_("enc_secret_again"), self.enc_secret2)
        enc_layout.addLayout(enc_form)
        layout.addWidget(enc_box)

        self.status = QLabel()
        self.status.setProperty("dim", True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok |
                                   QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_("create"))
        buttons.button(QDialogButtonBox.Ok).setProperty("primary", True)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, _("location"))
        if path:
            self.location.setText(path)

    def _enc_changed(self):
        needs = ENC_MODES[self.enc_mode.currentIndex()] != "none"
        self.enc_secret.setEnabled(needs)
        self.enc_secret2.setEnabled(needs)
        self.status.setText("")

    def _encryption_choice(self):
        from .. import crypto
        mode = ENC_MODES[self.enc_mode.currentIndex()]
        if mode == "none":
            return ("none", "")
        secret = self.enc_secret.text()
        if secret != self.enc_secret2.text():
            self.status.setText(_("enc_mismatch"))
            return None
        try:
            crypto.validate(mode, secret)
        except crypto.CryptoError as e:
            self.status.setText({
                "pin4": _("enc_bad_pin4"),
                "pin8": _("enc_bad_pin8"),
                "password": _("enc_bad_pass"),
            }.get(str(e), _("enc_bad_pass")))
            return None
        return (mode, secret)

    def _create(self):
        name = self.name.text().strip()
        if not name:
            return
        choice = self._encryption_choice()
        if choice is None:
            return
        dest = os.path.join(self.location.text(), name)
        repo_url = self.repo.text().strip()
        self.status.setText(_("creating"))
        try:
            if repo_url and not os.path.isdir(os.path.join(dest, ".git")):
                gitops.clone(repo_url, dest, config.get("github_token"))
            elif not gitops.is_repo(dest):
                gitops.init(dest, repo_url)
        except Exception as e:  # noqa: BLE001
            self.status.setText(str(e))
            return
        config.add_project(name, dest, repo_url)
        mode, secret = choice
        if mode != "none":
            from .. import crypto
            crypto.enable(dest, mode, secret)
        self.accept()


# --------------------------------------------------------------------------- #
# GitHub sign-in
# --------------------------------------------------------------------------- #
class GitHubLoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("github_login"))
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)

        head = QHBoxLayout()
        pm = brand_pixmap("github", 40)
        if pm:
            icon = QLabel()
            icon.setPixmap(pm)
            head.addWidget(icon)
        title = QLabel(_("sign_in_github"))
        title.setProperty("heading", True)
        head.addWidget(title, 1)
        layout.addLayout(head)

        desc = QLabel(_("oauth_hint"))
        desc.setWordWrap(True)
        desc.setProperty("dim", True)
        layout.addWidget(desc)

        client_id = (config.get("github_client_id") or "").strip()
        if not client_id:
            steps = QLabel(_("one_time_setup") + "\n" + _("setup_steps"))
            steps.setWordWrap(True)
            layout.addWidget(steps)
            open_btn = QPushButton(_("open_github_setup"))
            open_btn.clicked.connect(lambda: os.system(
                "xdg-open https://github.com/settings/applications/new &"))
            layout.addWidget(open_btn)
            self.client_id = QLineEdit()
            self.client_id.setPlaceholderText(_("paste_client_id"))
            layout.addWidget(self.client_id)
        else:
            self.client_id = None

        self.code_label = QLabel()
        self.code_label.setAlignment(Qt.AlignCenter)
        self.code_label.setProperty("title", True)
        layout.addWidget(self.code_label)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton(_("cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.login_btn = QPushButton(_("login"))
        self.login_btn.setProperty("primary", True)
        self.login_btn.clicked.connect(self._start)
        btns.addWidget(self.login_btn)
        layout.addLayout(btns)

        # secondary options
        other = QGroupBox(_("other_signin"))
        other_layout = QVBoxLayout(other)
        gh_btn = QPushButton(_("sign_in_gh"))
        gh_btn.clicked.connect(self._gh_cli)
        other_layout.addWidget(gh_btn)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText(_("or_paste_token"))
        other_layout.addWidget(self.token)
        token_btn = QPushButton(_("login"))
        token_btn.clicked.connect(self._token_login)
        other_layout.addWidget(token_btn)
        layout.addWidget(other)

    def _finish(self, token):
        try:
            user = github.validate_token(token)
        except github.GitHubError as e:
            self.status.setText(str(e))
            return
        config.set("github_token", token)
        config.set("github_user", user)
        self.accept()

    def _start(self):
        from .pages import run_async
        client_id = (config.get("github_client_id") or "").strip()
        if self.client_id is not None:
            client_id = self.client_id.text().strip()
            if client_id:
                config.set("github_client_id", client_id)
        if not client_id:
            self.status.setText(_("client_id_missing"))
            return
        self.login_btn.setEnabled(False)
        self.status.setText("…")

        def done(data, error):
            if error or not data:
                self.login_btn.setEnabled(True)
                self.status.setText(str(error))
                return
            self.code_label.setText(data["user_code"])
            self.status.setText(_("device_waiting"))
            os.system(f"xdg-open {data.get('verification_uri', '')} &")
            run_async(self, lambda: github.device_poll(
                client_id, data["device_code"], data.get("interval", 5),
                data.get("expires_in", 900)), self._polled)

        run_async(self, lambda: github.device_start(client_id), done)

    def _polled(self, token, error):
        if error or not token:
            self.login_btn.setEnabled(True)
            self.status.setText(str(error) if error else _("login_failed"))
            return
        self._finish(token)

    def _gh_cli(self):
        token = github.gh_token()
        if not token:
            self.status.setText(_("gh_not_ready"))
            return
        self._finish(token)

    def _token_login(self):
        token = self.token.text().strip()
        if token:
            self._finish(token)


# --------------------------------------------------------------------------- #
# GPG key
# --------------------------------------------------------------------------- #
KEY_TYPES = [("ed25519", "Ed25519"), ("rsa4096", "RSA 4096"),
             ("rsa3072", "RSA 3072"), ("rsa2048", "RSA 2048")]
EXPIRES = [("0", "exp_never"), ("1y", "exp_1y"), ("2y", "exp_2y"),
           ("5y", "exp_5y")]


class GpgKeyDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("create_key"))
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        user = os.environ.get("USER", "developer")
        self.name = QLineEdit(user)
        form.addRow(_("full_name"), self.name)
        self.email = QLineEdit(f"{user}@localhost")
        form.addRow(_("email"), self.email)
        self.comment = QLineEdit()
        form.addRow(_("comment_f"), self.comment)
        self.key_type = QComboBox()
        for _c, label in KEY_TYPES:
            self.key_type.addItem(label)
        form.addRow(_("key_type"), self.key_type)
        self.expire = QComboBox()
        for _c, key in EXPIRES:
            self.expire.addItem(_(key))
        form.addRow(_("expiration"), self.expire)
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.Password)
        form.addRow(_("passphrase"), self.passphrase)
        layout.addLayout(form)

        hint = QLabel(_("passphrase_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        layout.addWidget(hint)

        self.status = QLabel()
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok |
                                   QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_("create"))
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create(self):
        from .. import gpgsign
        self.status.setText(_("creating_key"))
        try:
            gpgsign.create_key(
                self.name.text(), self.email.text(),
                comment=self.comment.text(),
                key_type=KEY_TYPES[self.key_type.currentIndex()][0],
                expire=EXPIRES[self.expire.currentIndex()][0],
                passphrase=self.passphrase.text())
        except Exception as e:  # noqa: BLE001
            self.status.setText(str(e))
            return
        self.accept()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
THEMES = ["system", "light", "dark"]


class RepoSettingsDialog(QDialog):
    """Everything one APT archive needs to know about itself.

    One page rather than a wizard: a repository has six or seven settings,
    most with a sensible default, and only two — the folder and the signing
    key — that nobody can answer for the user. Save stays disabled until
    those two are filled in.
    """

    def __init__(self, parent, profile=None):
        super().__init__(parent)
        from .. import gpgsign, repo
        self._repo = repo
        self._previous = (profile or {}).get("name", "")
        self.stored = None
        settings = repo.normalise(profile or {})
        if not profile:
            settings["name"] = ""
        self.setWindowTitle(_("repo_settings") if profile else _("repo_new"))
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        hint = QLabel(_("repo_page_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        layout.addWidget(hint)

        form = QFormLayout()
        self.name = QLineEdit(settings["name"])
        self.name.textChanged.connect(self._validate)
        form.addRow(_("repo_name"), self.name)

        folder_row = QHBoxLayout()
        self.folder = QLineEdit(settings["root"])
        self.folder.setPlaceholderText(_("repo_folder_hint"))
        self.folder.textChanged.connect(self._validate)
        folder_row.addWidget(self.folder, 1)
        browse = QPushButton(_("browse"))
        browse.clicked.connect(self._choose_folder)
        folder_row.addWidget(browse)
        form.addRow(_("repo_folder"), folder_row)

        self.url = QLineEdit(settings["base_url"])
        self.url.setPlaceholderText(_("repo_address_hint"))
        form.addRow(_("repo_address"), self.url)

        self._keys = gpgsign.list_keys_detailed()
        self.key = QComboBox()
        for k in self._keys:
            self.key.addItem(f'{k["uid"]}  ·  {k["fpr"][-16:]}', k["fpr"])
            if k["fpr"] == settings["key"]:
                self.key.setCurrentIndex(self.key.count() - 1)
        if not self._keys:
            self.key.addItem(_("no_key"), "")
        self.key.setToolTip(_("repo_key_hint"))
        form.addRow(_("repo_key"), self.key)

        self.suite = QLineEdit(settings["suite"])
        form.addRow(_("repo_suite"), self.suite)
        self.component = QLineEdit(settings["component"])
        form.addRow(_("repo_component"), self.component)
        self.archs = QLineEdit(", ".join(settings["archs"]))
        self.archs.setPlaceholderText(_("repo_archs_hint"))
        form.addRow(_("repo_archs"), self.archs)
        self.origin = QLineEdit(settings["origin"])
        form.addRow(_("repo_origin"), self.origin)
        self.label = QLineEdit(settings["label"])
        form.addRow(_("repo_label"), self.label)
        self.description = QLineEdit(settings["description"])
        form.addRow(_("repo_desc"), self.description)

        self._methods = ["none", "folder", "rsync"]
        self.method = QComboBox()
        self.method.addItems([_("repo_pub_none"), _("repo_pub_folder"),
                              _("repo_pub_rsync")])
        self.method.setCurrentIndex(
            self._methods.index(settings["publish_method"]))
        self.method.currentIndexChanged.connect(self._on_method)
        form.addRow(_("repo_publish_how"), self.method)

        target_row = QHBoxLayout()
        self.target = QLineEdit(settings["publish_target"])
        self.target.setPlaceholderText(_("repo_target_hint"))
        target_row.addWidget(self.target, 1)
        self.target_browse = QPushButton(_("browse"))
        self.target_browse.clicked.connect(self._choose_target)
        target_row.addWidget(self.target_browse)
        form.addRow(_("repo_target"), target_row)
        layout.addLayout(form)

        missing = [name for name, present in repo.tools_available().items()
                   if not present]
        if missing:
            note = QLabel(", ".join(missing) + (
                "  —  " + _("repo_no_index_tool") if not repo.can_index()
                else ""))
            note.setWordWrap(True)
            note.setProperty("dim", True)
            layout.addWidget(note)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty("danger", True)
        layout.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Save
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._save_btn = buttons.button(QDialogButtonBox.Save)
        self._on_method()
        self._validate()

    def _on_method(self, *_a):
        method = self._methods[self.method.currentIndex()]
        self.target.setEnabled(method != "none")
        self.target_browse.setEnabled(method == "folder")

    def _validate(self, *_a):
        self._save_btn.setEnabled(bool(self.name.text().strip())
                                  and bool(self.folder.text().strip())
                                  and bool(self._keys))

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, _("repo_folder"))
        if path:
            self.folder.setText(path)

    def _choose_target(self):
        path = QFileDialog.getExistingDirectory(self, _("repo_target"))
        if path:
            self.target.setText(path)

    def _save(self):
        profile = {
            "name": self.name.text().strip(),
            "root": self.folder.text().strip(),
            "base_url": self.url.text().strip(),
            "suite": self.suite.text().strip(),
            "component": self.component.text().strip(),
            "archs": self.archs.text(),
            "origin": self.origin.text().strip(),
            "label": self.label.text().strip(),
            "description": self.description.text().strip(),
            "key": self.key.currentData() or "",
            "publish_method": self._methods[self.method.currentIndex()],
            "publish_target": self.target.text().strip(),
        }
        try:
            self.stored = self._repo.save_profile(profile, self._previous)
            self._repo.ensure_layout(self.stored)
        except (self._repo.RepoError, OSError) as e:
            self.error.setText(str(e))
            return
        self.accept()



class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle(_("settings"))
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)

        general = QGroupBox(_("general"))
        form = QFormLayout(general)
        self.lang = QComboBox()
        for _c, label in LANGUAGES:
            self.lang.addItem(label)
        codes = [c for c, _l in LANGUAGES]
        if config.get("language") in codes:
            self.lang.setCurrentIndex(codes.index(config.get("language")))
        self.lang.currentIndexChanged.connect(self._on_lang)
        form.addRow(_("language"), self.lang)

        self.theme = QComboBox()
        self.theme.addItems([_("theme_system"), _("theme_light"),
                             _("theme_dark")])
        if config.get("theme") in THEMES:
            self.theme.setCurrentIndex(THEMES.index(config.get("theme")))
        self.theme.currentIndexChanged.connect(self._on_theme)
        form.addRow(_("theme"), self.theme)

        self.shrink_check = QCheckBox()
        self.shrink_check.setChecked(bool(config.get("drive_shrink_warning")))
        self.shrink_check.setToolTip(_("shrink_setting_hint"))
        self.shrink_check.stateChanged.connect(
            lambda s: config.set("drive_shrink_warning", bool(s)))
        form.addRow(_("shrink_setting"), self.shrink_check)

        self.net_check = QCheckBox()
        self.net_check.setChecked(bool(config.get("sandbox_network")))
        self.net_check.setToolTip(_("sandbox_net_setting_hint"))
        self.net_check.stateChanged.connect(
            lambda s: config.set("sandbox_network", bool(s)))
        form.addRow(_("sandbox_net_setting"), self.net_check)

        self.esc_check = QCheckBox()
        self.esc_check.setChecked(bool(config.get("esc_confirm")))
        self.esc_check.stateChanged.connect(
            lambda s: config.set("esc_confirm", bool(s)))
        form.addRow(_("esc_setting"), self.esc_check)
        layout.addWidget(general)

        auto = QGroupBox(_("autosave"))
        auto_form = QFormLayout(auto)
        self.auto_check = QCheckBox()
        self.auto_check.setChecked(bool(config.get("autosave_enabled")))
        self.auto_check.stateChanged.connect(
            lambda s: config.set("autosave_enabled", bool(s)))
        auto_form.addRow(_("autosave"), self.auto_check)
        self.interval = QSpinBox()
        self.interval.setRange(1, 120)
        self.interval.setValue(int(config.get("autosave_minutes")))
        self.interval.valueChanged.connect(
            lambda v: config.set("autosave_minutes", v))
        auto_form.addRow(_("autosave_interval"), self.interval)
        layout.addWidget(auto)

        gh = QGroupBox(_("github"))
        gh_layout = QVBoxLayout(gh)
        user = config.get("github_user")
        gh_head = QHBoxLayout()
        gh_pm = brand_pixmap("github", 28)
        if gh_pm:
            gh_icon = QLabel()
            gh_icon.setPixmap(gh_pm)
            gh_head.addWidget(gh_icon)
        self.gh_label = QLabel(_("logged_in_as", u=user) if user
                               else _("not_logged_in"))
        gh_head.addWidget(self.gh_label, 1)
        gh_layout.addLayout(gh_head)
        gh_btn = QPushButton(_("logout") if user else _("login"))
        gh_btn.clicked.connect(self._github)
        self.gh_btn = gh_btn
        gh_layout.addWidget(gh_btn)
        self.client_id = QLineEdit(config.get("github_client_id") or "")
        self.client_id.setPlaceholderText(_("client_id_setting"))
        self.client_id.textChanged.connect(
            lambda t: config.set("github_client_id", t.strip()))
        gh_layout.addWidget(self.client_id)
        layout.addWidget(gh)

        gd = QGroupBox(_("gdrive"))
        gd_layout = QVBoxLayout(gd)
        from .. import gdrive
        connected = gdrive.connected()
        gd_head = QHBoxLayout()
        gd_pm = brand_pixmap("gdrive", 28)
        if gd_pm:
            gd_icon = QLabel()
            gd_icon.setPixmap(gd_pm)
            gd_head.addWidget(gd_icon)
        self.gd_label = QLabel(
            f'{_("signed_google")}\n{config.get("gdrive_email")}'
            if connected else _("not_logged_in"))
        gd_head.addWidget(self.gd_label, 1)
        gd_layout.addLayout(gd_head)
        gd_btn = QPushButton(_("disconnect") if connected
                             else _("connect_gdrive"))
        gd_btn.clicked.connect(self._gdrive)
        self.gd_btn = gd_btn
        gd_layout.addWidget(gd_btn)
        fetch = QPushButton(_("drive_import"))
        fetch.clicked.connect(lambda: self.window.import_drive_projects(False))
        gd_layout.addWidget(fetch)
        layout.addWidget(gd)

        ui_box = QGroupBox(_("interface_group"))
        ui_layout = QHBoxLayout(ui_box)
        ui_layout.addWidget(QLabel(f'{_("ui_variant")}: {_("ui_kde")}'))
        ui_layout.addStretch(1)
        switch_btn = QPushButton(f'{_("switch_ui")} \u2192 {_("ui_gnome")}')
        switch_btn.clicked.connect(lambda: self._switch_ui("gnome"))
        ui_layout.addWidget(switch_btn)
        layout.addWidget(ui_box)

        close = QPushButton(_("close"))
        close.clicked.connect(self.accept)
        layout.addWidget(close)

    def _on_lang(self, index):
        code = LANGUAGES[index][0]
        if code == config.get("language"):
            return
        config.set("language", code)
        _.set_language(code)

        # Widgets keep the labels they were built with, so restart to apply.
        box = QMessageBox(self)
        box.setWindowTitle(_("lang_restart_q"))
        box.setText(_("lang_restart_q"))
        box.setInformativeText(_("lang_restart_detail"))
        restart = box.addButton(_("restart_now"), QMessageBox.AcceptRole)
        box.addButton(_("restart_later"), QMessageBox.RejectRole)
        box.setDefaultButton(restart)
        box.exec()
        if box.clickedButton() is restart:
            from .. import launcher
            if not launcher.relaunch_current():
                self.window.toast(_("restart_failed"))

    def _on_theme(self, index):
        config.set("theme", THEMES[index])
        self.window.apply_theme()

    def _github(self):
        if config.get("github_user"):
            config.set("github_token", "")
            config.set("github_user", "")
            self.gh_label.setText(_("not_logged_in"))
            self.gh_btn.setText(_("login"))
            return
        if GitHubLoginDialog(self).exec():
            user = config.get("github_user")
            self.gh_label.setText(_("logged_in_as", u=user))
            self.gh_btn.setText(_("logout"))

    def _gdrive(self):
        from .. import gdrive
        from .pages import run_async
        if gdrive.connected():
            try:
                gdrive.disconnect()
            except gdrive.DriveError:
                pass
            config.set("gdrive_email", "")
            self.gd_label.setText(_("not_logged_in"))
            self.gd_btn.setText(_("connect_gdrive"))
            return
        if not gdrive.available():
            self.gd_label.setText(_("rclone_missing"))
            return
        self.gd_label.setText(_("gdrive_wait"))
        run_async(self, gdrive.connect, lambda r, e: self._gd_done())

    def _gd_done(self):
        from .. import gdrive
        if gdrive.connected():
            email = gdrive.account_email()
            if email:
                config.set("gdrive_email", email)
            self.gd_label.setText(f'{_("signed_google")}\n{email}')
            self.gd_btn.setText(_("disconnect"))
            self.window.import_drive_projects(auto=True)

    def _switch_ui(self, target):
        from .. import launcher
        available = (launcher.gtk_available() if target == "gnome"
                    else launcher.qt_available())
        ui_label = _("ui_gnome") if target == "gnome" else _("ui_kde")
        if not available:
            self._offer_install(target, ui_label)
            return
        self._confirm_switch(target, ui_label)

    def _offer_install(self, target, ui_label):
        from .. import launcher
        cmd = (launcher.GNOME_INSTALL_CMD if target == "gnome"
              else launcher.KDE_INSTALL_CMD)
        packages = (launcher.GNOME_PACKAGES if target == "gnome"
                   else launcher.KDE_PACKAGES)
        if not launcher.pkexec_available():
            QMessageBox.information(self, ui_label,
                                    _("ui_missing", ui=ui_label, cmd=cmd))
            return
        box = QMessageBox(self)
        box.setWindowTitle(ui_label)
        box.setText(_("ui_missing", ui=ui_label, cmd=cmd))
        install_btn = box.addButton(_("install_now"), QMessageBox.AcceptRole)
        box.addButton(_("cancel"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is install_btn:
            self._run_install(target, ui_label, packages)

    def _run_install(self, target, ui_label, packages):
        from .. import launcher
        from .pages import run_async
        self.window.toast(_("installing_pkgs", ui=ui_label))

        def done(result, _err):
            ok, output = result
            if ok:
                self.window.toast(_("install_done", ui=ui_label))
                self._confirm_switch(target, ui_label)
            else:
                cmd = (launcher.GNOME_INSTALL_CMD if target == "gnome"
                      else launcher.KDE_INSTALL_CMD)
                QMessageBox.warning(
                    self, ui_label,
                    _("install_failed_ui", cmd=cmd) + f"\n\n{output}")

        run_async(self, lambda: launcher.install_packages(packages), done)

    def _confirm_switch(self, target, ui_label):
        from .. import launcher
        if QMessageBox.question(
                self, _("switch_ui_q", ui=ui_label),
                _("switch_ui_detail", ui=ui_label)) == QMessageBox.Yes:
            launcher.relaunch_as(target)


# --------------------------------------------------------------------------- #
# Setup wizard
# --------------------------------------------------------------------------- #
class SetupWizard(QWizard):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Petacore Setup")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setMinimumSize(680, 560)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)

        self.addPage(self._welcome())
        self.addPage(self._github())
        self.addPage(self._drive())
        self.drive_page = self._drive_projects()
        self.addPage(self.drive_page)
        self.addPage(self._features())
        self.addPage(self._project())

        self.setButtonText(QWizard.FinishButton, _("finish"))
        self.setButtonText(QWizard.NextButton, _("next"))
        self.setButtonText(QWizard.BackButton, _("back"))
        self.setButtonText(QWizard.CancelButton, _("cancel"))

    @staticmethod
    def _page(title, body, extra=None):
        page = QWizardPage()
        layout = QVBoxLayout(page)
        heading = QLabel(title)
        heading.setProperty("title", True)
        heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(heading)
        text = QLabel(body)
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignCenter)
        text.setProperty("dim", True)
        layout.addWidget(text)
        if extra:
            layout.addWidget(extra)
        layout.addStretch(1)
        footer = QLabel(COPYRIGHT)
        footer.setAlignment(Qt.AlignCenter)
        footer.setProperty("dim", True)
        layout.addWidget(footer)
        return page

    def _welcome(self):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        pm = app_pixmap(96)
        if pm:
            icon = QLabel()
            icon.setPixmap(pm)
            icon.setAlignment(Qt.AlignCenter)
            layout.addWidget(icon)
        combo = QComboBox()
        for _c, label in LANGUAGES:
            combo.addItem(label)
        codes = [c for c, _l in LANGUAGES]
        if config.get("language") in codes:
            combo.setCurrentIndex(codes.index(config.get("language")))
        combo.currentIndexChanged.connect(
            lambda i: (config.set("language", LANGUAGES[i][0]),
                       _.set_language(LANGUAGES[i][0])))
        layout.addWidget(combo)
        return self._page(_("welcome_title"), _("setup_welcome_body"), holder)

    def _github(self):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        pm = brand_pixmap("github", 56)
        if pm:
            icon = QLabel()
            icon.setPixmap(pm)
            icon.setAlignment(Qt.AlignCenter)
            layout.addWidget(icon)
        btn = QPushButton(_("github_login"))
        btn.setProperty("primary", True)
        btn.clicked.connect(lambda: GitHubLoginDialog(self).exec())
        layout.addWidget(btn)
        return self._page(_("setup_gh_title"), _("setup_gh_body"), holder)

    def _drive(self):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        pm = brand_pixmap("gdrive", 56)
        if pm:
            icon = QLabel()
            icon.setPixmap(pm)
            icon.setAlignment(Qt.AlignCenter)
            layout.addWidget(icon)
        btn = QPushButton(_("connect_gdrive"))
        btn.setProperty("primary", True)
        self.gd_status = QLabel()
        self.gd_status.setAlignment(Qt.AlignCenter)
        btn.clicked.connect(self._connect_drive)
        layout.addWidget(btn)
        layout.addWidget(self.gd_status)
        return self._page(_("setup_gd_title"), _("setup_gd_body"), holder)

    def _connect_drive(self):
        from .. import gdrive
        from .pages import run_async
        if not gdrive.available():
            self.gd_status.setText(_("rclone_missing"))
            return
        self.gd_status.setText(_("gdrive_wait"))
        run_async(self, gdrive.connect,
                  lambda r, e: self.gd_status.setText(
                      _("gdrive_connected") if gdrive.connected()
                      else str(e or "")))

    def _drive_projects(self):
        """Dedicated step: 'We found X and Y in your Drive — fetch them?'"""
        page = QWizardPage()
        layout = QVBoxLayout(page)
        heading = QLabel(_("drive_import"))
        heading.setProperty("title", True)
        heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(heading)
        self.found_label = QLabel()
        self.found_label.setWordWrap(True)
        self.found_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.found_label)
        self.found_list = QListWidget()
        layout.addWidget(self.found_list, 1)
        self.import_btn = QPushButton(_("import_all"))
        self.import_btn.setProperty("primary", True)
        self.import_btn.clicked.connect(self._do_import)
        layout.addWidget(self.import_btn)
        footer = QLabel(COPYRIGHT)
        footer.setAlignment(Qt.AlignCenter)
        footer.setProperty("dim", True)
        layout.addWidget(footer)

        def initialize():
            from .. import gdrive
            from .pages import run_async
            self.found_list.clear()
            self.found_label.setText("…")
            if not gdrive.connected():
                self.found_label.setText(_("not_logged_in"))
                self.import_btn.setEnabled(False)
                return

            def done(names, error):
                if error or not names:
                    self.found_label.setText(_("no_new_drive"))
                    self.import_btn.setEnabled(False)
                    return
                existing = {p["name"].lower()
                            for p in config.get("projects")}
                new = [n for n in names if n.lower() not in existing]
                if not new:
                    self.found_label.setText(_("no_new_drive"))
                    self.import_btn.setEnabled(False)
                    return
                self._new_projects = new
                self.found_label.setText(
                    _("drive_found", n=len(new)) + "\n"
                    + _("drive_import_body"))
                for name in new:
                    self.found_list.addItem(QListWidgetItem(name))
                self.import_btn.setEnabled(True)

            run_async(self, gdrive.list_remote_projects, done)

        page.initializePage = initialize
        return page

    def _do_import(self):
        names = getattr(self, "_new_projects", [])
        if names:
            self.import_btn.setEnabled(False)
            self.window.import_projects(names)

    def _features(self):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        for key in ("feat_1", "feat_2", "feat_3", "feat_4"):
            label = QLabel("•  " + _(key))
            label.setWordWrap(True)
            layout.addWidget(label)
        return self._page(_("setup_feat_title"), "", holder)

    def _project(self):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        btn = QPushButton(_("new_project"))
        btn.setProperty("primary", True)
        btn.clicked.connect(self._new_project)
        layout.addWidget(btn)
        return self._page(_("setup_first_title"), _("setup_first_body"),
                          holder)

    def _new_project(self):
        if NewProjectDialog(self).exec():
            self.window.rebuild()

    def done(self, result):
        config.set("first_run", False)
        self.window.rebuild()
        super().done(result)


# --------------------------------------------------------------------------- #
# Edit project (KDE build) — name, folder, repo, deploy target and encryption
# --------------------------------------------------------------------------- #
class EditProjectDialog(QDialog):
    def __init__(self, parent, project):
        super().__init__(parent)
        self._project = project
        self._new_path = project["path"]
        self._deploy = project.get("deploy_path", "")
        self.removed = False

        self.setWindowTitle(_("edit_project"))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(project.get("name", ""))
        form.addRow(_("project_name"), self.name)
        self.repo = QLineEdit(project.get("repo_url", ""))
        form.addRow(_("repo_url"), self.repo)

        folder_row = QHBoxLayout()
        self.folder_label = QLabel(self._new_path)
        folder_row.addWidget(self.folder_label, 1)
        pick_folder = QPushButton(_("browse"))
        pick_folder.clicked.connect(self._pick_folder)
        folder_row.addWidget(pick_folder)
        holder = QWidget(); holder.setLayout(folder_row)
        form.addRow(_("project_folder"), holder)

        deploy_row = QHBoxLayout()
        self.deploy_label = QLabel(self._deploy or _("not_set"))
        deploy_row.addWidget(self.deploy_label, 1)
        pick_deploy = QPushButton(_("browse"))
        pick_deploy.clicked.connect(self._pick_deploy)
        deploy_row.addWidget(pick_deploy)
        clear_deploy = QPushButton(_("clear"))
        clear_deploy.clicked.connect(self._clear_deploy)
        deploy_row.addWidget(clear_deploy)
        holder2 = QWidget(); holder2.setLayout(deploy_row)
        form.addRow(_("installed_folder"), holder2)
        layout.addLayout(form)

        # -- Encryption (cloud side only) --------------------------------------
        from .. import crypto
        enc_box = QGroupBox(_("encryption"))
        enc_layout = QVBoxLayout(enc_box)
        explain = QLabel(_("encrypt_explain"))
        explain.setWordWrap(True)
        explain.setProperty("dim", True)
        enc_layout.addWidget(explain)

        enc_form = QFormLayout()
        self.enc_mode = QComboBox()
        self.enc_mode.addItems([_("enc_none"), _("enc_pin4"), _("enc_pin8"),
                                _("enc_password")])
        current = crypto.mode_for(project)
        self.enc_mode.setCurrentIndex(ENC_MODES.index(current)
                                      if current in ENC_MODES else 0)
        self.enc_mode.currentIndexChanged.connect(self._enc_changed)
        enc_form.addRow(_("encrypt_upload"), self.enc_mode)

        self.enc_secret = QLineEdit()
        self.enc_secret.setEchoMode(QLineEdit.Password)
        enc_form.addRow(_("enc_secret"), self.enc_secret)
        self.enc_secret2 = QLineEdit()
        self.enc_secret2.setEchoMode(QLineEdit.Password)
        enc_form.addRow(_("enc_secret_again"), self.enc_secret2)
        enc_layout.addLayout(enc_form)

        self.enc_error = QLabel()
        self.enc_error.setWordWrap(True)
        enc_layout.addWidget(self.enc_error)
        layout.addWidget(enc_box)
        self._enc_changed()

        remove = QPushButton(_("remove_project"))
        remove.setProperty("danger", True)
        remove.clicked.connect(self._remove)
        layout.addWidget(remove)
        hint = QLabel(_("remove_detail"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok |
                                   QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_("save"))
        buttons.button(QDialogButtonBox.Ok).setProperty("primary", True)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _enc_changed(self):
        needs = ENC_MODES[self.enc_mode.currentIndex()] != "none"
        self.enc_secret.setEnabled(needs)
        self.enc_secret2.setEnabled(needs)
        self.enc_error.setText("")

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, _("project_folder"))
        if path:
            self._new_path = path
            self.folder_label.setText(path)

    def _pick_deploy(self):
        path = QFileDialog.getExistingDirectory(self, _("deploy_choose"))
        if path:
            self._deploy = path
            self.deploy_label.setText(path)

    def _clear_deploy(self):
        self._deploy = ""
        self.deploy_label.setText(_("not_set"))

    def _remove(self):
        if QMessageBox.question(self, _("remove_project"),
                                _("remove_detail")) == QMessageBox.Yes:
            config.remove_project(self._project["path"])
            self.removed = True
            self.accept()

    def _apply_encryption(self):
        from .. import crypto
        mode = ENC_MODES[self.enc_mode.currentIndex()]
        if mode == "none":
            crypto.disable(self._new_path)
            return True
        secret = self.enc_secret.text()
        if not secret and crypto.mode_for(self._project) == mode:
            return True                     # unchanged, keep the old secret
        if secret != self.enc_secret2.text():
            self.enc_error.setText(_("enc_mismatch"))
            return False
        try:
            crypto.enable(self._new_path, mode, secret)
        except crypto.CryptoError as e:
            self.enc_error.setText({
                "pin4": _("enc_bad_pin4"),
                "pin8": _("enc_bad_pin8"),
                "password": _("enc_bad_pass"),
            }.get(str(e), _("enc_bad_pass")))
            return False
        return True

    def _save(self):
        if not self._apply_encryption():
            return
        from .. import gitops
        name = self.name.text().strip() or self._project.get("name")
        repo = self.repo.text().strip()
        config.update_project(self._project["path"], name=name,
                              path=self._new_path, repo_url=repo,
                              deploy_path=self._deploy)
        if repo != self._project.get("repo_url", "") \
                and gitops.is_repo(self._new_path):
            gitops.set_remote(self._new_path, repo)
        self.accept()
