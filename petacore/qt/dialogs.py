"""Qt (KDE Plasma) dialogs for Petacore: setup wizard, project creation,
GitHub sign-in, settings and GPG key creation."""

import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QStackedWidget, QVBoxLayout,
                               QWidget, QWizard, QWizardPage)

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


def _tinted(pm):
    """The one-colour GitHub mark in the window's text colour, so it shows
    on a light theme as well as a dark one."""
    from PySide6.QtGui import QPainter, QPalette
    from PySide6.QtWidgets import QApplication
    colour = QApplication.palette().color(QPalette.WindowText)
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.drawPixmap(0, 0, pm)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(out.rect(), colour)
    painter.end()
    return out


def brand_pixmap(name, size=64):
    if name == "github":
        pm = _brand_file_pixmap(name, size)
        return _tinted(pm) if pm is not None else None
    return _brand_file_pixmap(name, size)


def _brand_file_pixmap(name, size=64):
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
        self.cloned_name = None
        try:
            if repo_url and not os.path.isdir(os.path.join(dest, ".git")):
                gitops.clone(repo_url, dest, config.get("github_token"))
                self.cloned_name = name
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
class _CodeSignal(QObject):
    """Carries the one-time code from the sign-in thread to the window."""
    code = Signal(str, str)


def _open_url(url):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl(url))


class GitHubLoginDialog(QDialog):
    """Connecting to GitHub with one button — the GNOME dialog's twin; see
    there and github.sign_in for the routes behind the button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("github_login"))
        self.setMinimumWidth(460)
        self._cancel = threading.Event()
        self._uri = github.DEVICE_PAGE
        self._signal = _CodeSignal()
        self._signal.code.connect(self._show_code)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 20)
        layout.setSpacing(12)
        pm = brand_pixmap("github", 60)
        if pm:
            icon = QLabel()
            icon.setPixmap(pm)
            icon.setAlignment(Qt.AlignCenter)
            layout.addWidget(icon)
        title = QLabel(_("gh_connect_title"))
        title.setProperty("title", True)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        self.body = QLabel(_("gh_connect_body"))
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignCenter)
        self.body.setProperty("dim", True)
        layout.addWidget(self.body)

        self.stack = QStackedWidget()
        self.go_btn = QPushButton(_("gh_connect_btn"))
        self.go_btn.setProperty("primary", True)
        self.go_btn.setMinimumHeight(38)
        self.go_btn.clicked.connect(self._start)
        self.install_btn = QPushButton(_("gh_install"))
        self.install_btn.setProperty("primary", True)
        self.install_btn.setMinimumHeight(38)
        self.install_btn.clicked.connect(self._install_gh)
        for button in (self.go_btn, self.install_btn):
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.addStretch(1)
            row.addWidget(button)
            row.addStretch(1)
            self.stack.addWidget(holder)

        waiting = QWidget()
        wl = QVBoxLayout(waiting)
        wl.setSpacing(6)
        self.code_label = QLabel()
        self.code_label.setAlignment(Qt.AlignCenter)
        self.code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.code_label.setStyleSheet(
            "font-family: monospace; font-size: 22pt; font-weight: 700;")
        wl.addWidget(self.code_label)
        for key in ("gh_code_copied", "device_waiting"):
            line = QLabel(_(key))
            line.setAlignment(Qt.AlignCenter)
            line.setProperty("dim", True)
            wl.addWidget(line)
        reopen = QPushButton(_("gh_reopen"))
        reopen.setFlat(True)
        reopen.clicked.connect(lambda: _open_url(self._uri))
        wl.addWidget(reopen, 0, Qt.AlignCenter)
        self.stack.addWidget(waiting)

        busy = QLabel("…")
        busy.setAlignment(Qt.AlignCenter)
        busy.setProperty("heading", True)
        self.stack.addWidget(busy)
        layout.addWidget(self.stack)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet("color: #e5534b;")
        self.status.hide()
        layout.addWidget(self.status)

        # by hand, folded away
        self.more = QPushButton(_("other_signin") + "  ▾")
        self.more.setFlat(True)
        self.more.setCheckable(True)
        layout.addWidget(self.more, 0, Qt.AlignCenter)
        self.other = QGroupBox()
        ol = QVBoxLayout(self.other)
        token_page = QPushButton(_("create_token_browser"))
        token_page.clicked.connect(lambda: _open_url(github.TOKEN_URL))
        ol.addWidget(token_page)
        token_row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText(_("or_paste_token"))
        token_row.addWidget(self.token, 1)
        token_btn = QPushButton(_("login"))
        token_btn.clicked.connect(self._token_login)
        token_row.addWidget(token_btn)
        ol.addLayout(token_row)
        self.client_id = QLineEdit(config.get("github_client_id") or "")
        self.client_id.setPlaceholderText(_("gh_client_id_label"))
        self.client_id.editingFinished.connect(self._client_id_set)
        ol.addWidget(self.client_id)
        self.other.hide()
        self.more.toggled.connect(self.other.setVisible)
        layout.addWidget(self.other)
        self._choose_route()

    # -- routes ---------------------------------------------------------------
    def _choose_route(self):
        from .pages import run_async
        if github.sign_in_route() == "manual":
            self.body.setText(_("gh_need_cli"))
            self.stack.setCurrentIndex(1)
            return
        self.body.setText(_("gh_connect_body"))
        self.stack.setCurrentIndex(0)
        run_async(self, github.gh_token, lambda token, _e: token and
                  self.body.setText(_("gh_connect_ready")))

    def _client_id_set(self):
        config.set("github_client_id", self.client_id.text().strip())
        self._choose_route()

    def _install_gh(self):
        from .pages import run_async
        from .. import launcher
        self._error("")
        self.stack.setCurrentIndex(3)
        self.body.setText(_("gh_installing"))

        def done(result, _error):
            if result and result[0] and github.gh_available():
                self._choose_route()
                self._start()
            else:
                self.stack.setCurrentIndex(1)
                self.body.setText(_("gh_need_cli"))
                self._error(_("gh_install_failed"))
        run_async(self, lambda: launcher.install_packages(["gh"]), done)

    def _start(self):
        from .pages import run_async
        self._error("")
        self.stack.setCurrentIndex(3)
        self._cancel.clear()
        signal = self._signal
        cancel = self._cancel
        run_async(self, lambda: github.sign_in(
            lambda code, uri: signal.code.emit(code, uri), cancel),
            self._signed_in)

    def _show_code(self, code, uri):
        self._uri = uri
        self.code_label.setText(code)
        QGuiApplication.clipboard().setText(code)
        self.stack.setCurrentIndex(2)
        _open_url(uri)

    def _signed_in(self, result, error):
        if self._cancel.is_set():
            return
        if error or not result:
            if str(error) == "no-route":
                self._choose_route()
                return
            self.stack.setCurrentIndex(0)
            self._error(str(error) if error else _("login_failed"))
            return
        self._save(*result)

    def _token_login(self):
        token = self.token.text().strip()
        if not token:
            return
        try:
            user = github.validate_token(token)
        except github.GitHubError:
            self._error(_("login_failed"))
            return
        self._save(token, user)

    def _save(self, token, user):
        config.set("github_token", token)
        config.set("github_user", user)
        parent = self.parent()
        if parent is not None and hasattr(parent, "toast"):
            parent.toast(_("gh_connected", user=user))
        self.accept()

    def _error(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def done(self, result):
        self._cancel.set()         # stop a sign-in still waiting
        super().done(result)


# --------------------------------------------------------------------------- #
# Upload to GitHub
# --------------------------------------------------------------------------- #
_PUBLISH_QSS = """
QPushButton[chip="true"] { border-radius: 13px; padding: 4px 14px;
                           min-height: 18px; font-weight: 600; }
QPushButton[chip="true"]:checked { border: 1px solid #3daee9;
                                   color: #3daee9; }
QFrame[card="true"] { border: 1px solid rgba(127,127,127,0.25);
                      border-radius: 10px; }
"""


class PublishDialog(QDialog):
    """"Upload to GitHub" — the GNOME PublishDialog's twin: code to main
    and, unless switched off, a new version with its packages attached."""

    def __init__(self, parent, project):
        super().__init__(parent)
        from .. import publish
        self._pub = publish
        self._project = project
        self._path = project["path"]
        self._hint = ""
        self._chips = []
        self._result_url = ""
        self.result = None
        self.setWindowTitle(_("pub_title"))
        self.setMinimumWidth(560)
        self.setStyleSheet(_PUBLISH_QSS)

        outer = QVBoxLayout(self)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        form = QWidget()
        layout = QVBoxLayout(form)
        layout.setSpacing(10)

        # account
        card = QFrame()
        card.setProperty("card", True)
        cl = QHBoxLayout(card)
        pm = brand_pixmap("github", 32)
        if pm:
            mark = QLabel()
            mark.setPixmap(pm)
            cl.addWidget(mark)
        names = QVBoxLayout()
        self.account = QLabel()
        self.account.setProperty("heading", True)
        self.where = QLabel()
        self.where.setProperty("dim", True)
        names.addWidget(self.account)
        names.addWidget(self.where)
        cl.addLayout(names, 1)
        self.connect_btn = QPushButton(_("gh_connect_btn"))
        self.connect_btn.setProperty("primary", True)
        self.connect_btn.clicked.connect(lambda: self._sign_in())
        cl.addWidget(self.connect_btn)
        layout.addWidget(card)

        try:
            self._target = publish.target(self._path)
            self._remote_error = ""
        except publish.PublishError:
            self._target = None
            self._remote_error = _("pub_err_not_github")

        # repository, only when there is none yet
        self.repo_box = QGroupBox()
        rl = QFormLayout(self.repo_box)
        self.repo_name = QLineEdit(github.repo_slug(
            os.path.basename(self._path.rstrip("/"))))
        self.repo_name.textChanged.connect(self._validate)
        rl.addRow(_("pub_repo_name"), self.repo_name)
        self.private = QCheckBox(_("pub_private"))
        self.private.setChecked(True)
        self.private.setToolTip(_("pub_private_hint"))
        rl.addRow("", self.private)
        self.repo_box.setVisible(self._target is None
                                 and not self._remote_error)
        layout.addWidget(self.repo_box)

        # version
        version = QGroupBox(_("pub_version"))
        vl = QVBoxLayout(version)
        self.as_release = QCheckBox(_("pub_as_release"))
        self.as_release.setChecked(True)
        self.as_release.setToolTip(_("pub_as_release_hint"))
        self.as_release.toggled.connect(self._mode)
        vl.addWidget(self.as_release)
        self.release_box = QWidget()
        rbl = QVBoxLayout(self.release_box)
        rbl.setContentsMargins(0, 4, 0, 0)
        hint = QLabel(_("pub_version_hint"))
        hint.setProperty("dim", True)
        rbl.addWidget(hint)
        self.chip_row = QHBoxLayout()
        self.chip_row.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        rbl.addLayout(self.chip_row)
        self.version = QLineEdit()
        self.version.textEdited.connect(self._typed)
        self.version.textChanged.connect(self._validate)
        rbl.addWidget(self.version)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setProperty("dim", True)
        rbl.addWidget(self.info)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText(_("pub_notes") + " — "
                                      + _("pub_notes_hint"))
        self.notes.setFixedHeight(76)
        rbl.addWidget(self.notes)
        vl.addWidget(self.release_box)
        layout.addWidget(version)

        branch = QFormLayout()
        self.branch = QLineEdit("main")
        self.branch.textChanged.connect(self._validate)
        branch.addRow(_("pub_branch"), self.branch)
        layout.addLayout(branch)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(_("cancel"))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.go_btn = QPushButton(_("pub_go"))
        self.go_btn.setProperty("primary", True)
        self.go_btn.clicked.connect(self._go)
        buttons.addWidget(self.go_btn)
        layout.addLayout(buttons)
        self.pages.addWidget(form)

        # done
        done = QWidget()
        dl = QVBoxLayout(done)
        dl.addStretch(1)
        tick = QLabel("✓")
        tick.setAlignment(Qt.AlignCenter)
        tick.setStyleSheet("font-size: 54pt; font-weight: 700;"
                           " color: #3fb950;")
        dl.addWidget(tick)
        self.done_title = QLabel()
        self.done_title.setProperty("title", True)
        self.done_title.setAlignment(Qt.AlignCenter)
        dl.addWidget(self.done_title)
        self.done_note = QLabel()
        self.done_note.setWordWrap(True)
        self.done_note.setAlignment(Qt.AlignCenter)
        self.done_note.setProperty("dim", True)
        dl.addWidget(self.done_note)
        open_btn = QPushButton(_("pub_open"))
        open_btn.setProperty("primary", True)
        open_btn.clicked.connect(lambda: _open_url(self._result_url))
        dl.addWidget(open_btn, 0, Qt.AlignCenter)
        close = QPushButton(_("close"))
        close.setFlat(True)
        close.clicked.connect(self.accept)
        dl.addWidget(close, 0, Qt.AlignCenter)
        dl.addStretch(1)
        self.pages.addWidget(done)

        self._show_account()
        self._suggest(publish.library_versions(self._path))
        self._mode()
        self._fetch_released()

    # -- account ----------------------------------------------------------------
    def _token(self):
        return config.get("github_token") or ""

    def _show_account(self):
        user = config.get("github_user")
        signed_in = bool(self._token() and user)
        self.connect_btn.setVisible(not signed_in)
        self.account.setText(f"@{user}" if signed_in
                             else _("pub_signed_out"))
        self.where.setText(self._remote_error or (
            "/".join(self._target) if self._target else _("pub_new_repo")))
        self._validate()

    def _sign_in(self, then=None):
        if GitHubLoginDialog(self).exec():
            self._show_account()
            self._fetch_released()
            if then:
                then()

    # -- versions ---------------------------------------------------------------
    def _suggest(self, library, released=()):
        options = self._pub.suggest_for(library, released)
        for _text, chip in self._chips:
            self.chip_group.removeButton(chip)
            chip.deleteLater()
        while self.chip_row.count():
            self.chip_row.takeAt(0)
        self._chips = []
        for item in options:
            text = self._pub.title(*item)
            chip = QPushButton(text)
            chip.setProperty("chip", True)
            chip.setCheckable(True)
            chip.clicked.connect(lambda _c=False, t=text: self._pick(t))
            self.chip_group.addButton(chip)
            self.chip_row.addWidget(chip)
            self._chips.append((text, chip))
        self.chip_row.addStretch(1)
        self._hint = options[0][0] if options else ""
        if self._chips:
            self._pick(self._chips[0][0])

    def _pick(self, text):
        self.version.setText(text)
        for t, chip in self._chips:
            chip.setChecked(t == text)

    def _typed(self, text):
        self.chip_group.setExclusive(False)
        for t, chip in self._chips:
            chip.setChecked(t == text)
        self.chip_group.setExclusive(True)

    def _fetch_released(self):
        from .pages import run_async
        token = self._token()
        if not token or not self._target:
            return
        path, pub = self._path, self._pub

        def done(known, error):
            if error or not known:
                return
            typed = self.version.text()
            untouched = any(t == typed for t, _c in self._chips)
            self._suggest(*known)
            if not untouched:
                self.version.setText(typed)
                self._typed(typed)
        run_async(self, lambda: (pub.library_versions(path),
                                 pub.released_on_github(token, path)), done)

    def _mode(self):
        on = self.as_release.isChecked()
        self.release_box.setVisible(on)
        self.go_btn.setText(_("pub_go") if on else _("pub_push_only"))
        self._validate()
        self.adjustSize()

    def _parsed(self):
        try:
            return self._pub.parse(self.version.text(), self._hint)
        except self._pub.PublishError:
            return None

    def _validate(self):
        ok = not self._remote_error
        if self.as_release.isChecked():
            parsed = self._parsed()
            if parsed is None:
                ok = False
                self.info.setText("⚠  " + _("pub_bad_version"))
            else:
                tag = self._pub.tag(*parsed)
                files = self._pub.packages_for(self._path, *parsed)
                line = (_("pub_tag_pre", tag=tag) if parsed[1] != "stable"
                        else _("pub_tag_stable", tag=tag))
                more = (_("pub_files", files=", ".join(
                    os.path.basename(f) for f in files))
                    if files else _("pub_no_files"))
                self.info.setText(line + "\n" + more)
        if not gitops.BRANCH.match(self.branch.text().strip()):
            ok = False
        if self.repo_box.isVisible() and not github.REPO_NAME.match(
                self.repo_name.text().strip()):
            ok = False
        self.go_btn.setEnabled(ok)

    # -- publishing -------------------------------------------------------------
    def _go(self):
        from .pages import run_async
        if not (self._token() and config.get("github_user")):
            self._sign_in(then=self._go)
            return
        release = self.as_release.isChecked()
        parsed = self._parsed() if release else None
        title = self._pub.title(*parsed) if parsed else ""
        branch = self.branch.text().strip()
        args = dict(version_text=self.version.text(),
                    number_hint=self._hint, branch=branch,
                    repo_name=self.repo_name.text().strip(),
                    private=self.private.isChecked(),
                    notes=self.notes.toPlainText(), release=release)
        token, path, pub = self._token(), self._path, self._pub
        status = _StatusSignal()
        status.text.connect(self._set_status)
        self._status_signal = status

        def on_status(stage, detail=""):
            status.text.emit({
                "account": _("pub_stage_account"),
                "repo": _("pub_stage_repo"),
                "push": _("pub_stage_push", branch=branch),
                "release": _("pub_stage_release", title=title),
                "upload": _("pub_stage_upload", name=detail),
            }.get(stage, ""))

        self.go_btn.setEnabled(False)
        self.as_release.setEnabled(False)
        run_async(self, lambda: pub.publish(path, token, on_status=on_status,
                                            **args),
                  lambda result, error: self._finished(result, error,
                                                       title, branch))

    def _set_status(self, text, error=False):
        self.status.setText(text)
        self.status.setStyleSheet("color: #e5534b;" if error else "")

    def _finished(self, result, error, title, branch):
        self.as_release.setEnabled(True)
        if error is not None:
            code = str(error)
            if isinstance(error, github.GitHubError) and error.status == 401:
                config.set("github_token", "")
                config.set("github_user", "")
                self._show_account()
                self._set_status("")
                self._sign_in(then=self._go)
                return
            self._set_status({
                "exists": _("pub_err_exists", title=title),
                "diverged": _("pub_err_diverged", branch=branch),
                "not-github": _("pub_err_not_github"),
                "bad-branch": _("pub_err_branch"),
                "bad-repo-name": _("pub_err_repo_name"),
                "bad-version": _("pub_bad_version"),
            }.get(code, _("pub_failed", err=code)), True)
            self._validate()
            return
        if result["repo"] and not self._project.get("repo_url"):
            config.set_project_field(self._path, "repo_url",
                                     result["repo"] + ".git")
        self.result = result
        self._result_url = result["url"]
        self.done_title.setText(_("pub_done_release", title=result["title"])
                                if result["title"] else _("pub_done_push"))
        if result["kept_back"]:
            self.done_note.setText(_("pub_kept_back",
                                     files=", ".join(result["kept_back"])))
        self.pages.setCurrentIndex(1)


class _StatusSignal(QObject):
    text = Signal(str)


# --------------------------------------------------------------------------- #
# Backups — everything a Drive sync replaced, ready to be put back
# --------------------------------------------------------------------------- #
AUTOPULL_MINUTES = [5, 10, 30, 60]
BACKUP_DAYS = [7, 30, 90, 365, 0]


class BackupsDialog(QDialog):
    """The GNOME BackupsWindow's twin: backups grouped by day, opened to
    show their files; put back a whole backup or a single file."""

    def __init__(self, parent, project):
        super().__init__(parent)
        from PySide6.QtWidgets import QTreeWidget
        self._window = parent if hasattr(parent, "toast") else parent.parent()
        self._project = project
        self.setWindowTitle(f'{_("backups_open")} — {project["name"]}')
        self.resize(640, 620)
        layout = QVBoxLayout(self)
        days = config.get("drive_backup_days")
        intro = QLabel(_("backups_intro", days=days) if days
                       else _("backups_intro_forever"))
        intro.setWordWrap(True)
        intro.setProperty("dim", True)
        layout.addWidget(intro)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setRootIsDecorated(True)
        self.tree.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.tree, 1)
        self.status = QLabel(_("backups_loading"))
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setProperty("dim", True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton(_("close"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        self.put_back = QPushButton(_("backup_restore"))
        self.put_back.setProperty("primary", True)
        self.put_back.setEnabled(False)
        self.put_back.clicked.connect(self._ask)
        row.addWidget(self.put_back)
        layout.addLayout(row)
        self._load()

    def _load(self):
        from .pages import run_async
        from .. import gdrive
        self.tree.clear()
        self.status.setText(_("backups_loading"))
        self.status.show()
        project = self._project
        run_async(self, lambda: gdrive.list_backups(project["path"],
                                                    project["name"]),
                  self._show)

    def _show(self, backups, error):
        import time as _time
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QTreeWidgetItem
        from ..util import human_size
        from ..gdrive import backup_day, backup_title
        if not backups:
            self.status.setText(_("backups_empty_title") + "\n"
                                + _("backups_empty"))
            return
        self.status.hide()
        bold = QFont()
        bold.setBold(True)
        current_day = None
        for backup in backups:
            day = backup_day(backup["time"])
            if day != current_day:
                current_day = day
                head = QTreeWidgetItem([day, ""])
                head.setFont(0, bold)
                head.setFlags(Qt.ItemIsEnabled)
                self.tree.addTopLevelItem(head)
            size = sum(s for _p, s in backup["files"])
            where = _("backup_where_drive") if backup["where"] == "drive" \
                else _("backup_where_local")
            item = QTreeWidgetItem([
                _time.strftime("%H:%M", _time.localtime(backup["time"]))
                + "  " + backup_title(backup),
                _("backup_files", n=len(backup["files"]),
                  size=human_size(size)) + " · " + where])
            item.setData(0, Qt.UserRole, (backup, None))
            for rel, fsize in backup["files"][:200]:
                child = QTreeWidgetItem([rel, human_size(fsize)])
                child.setData(0, Qt.UserRole, (backup, [rel]))
                item.addChild(child)
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)

    def _selected(self):
        items = self.tree.selectedItems()
        self.put_back.setEnabled(bool(items) and
                                 items[0].data(0, Qt.UserRole) is not None)

    def _ask(self):
        from .pages import run_async
        from .. import gdrive
        items = self.tree.selectedItems()
        if not items or items[0].data(0, Qt.UserRole) is None:
            return
        backup, files = items[0].data(0, Qt.UserRole)
        count = len(files) if files else len(backup["files"])
        if QMessageBox.question(self, _("backup_restore_q", n=count),
                                _("backup_restore_body")) != QMessageBox.Yes:
            return
        project = self._project

        def done(n, error):
            if error:
                self._window.toast(f"{_('drive_failed')}: {error}")
                return
            self._window.toast(_("backup_restored", n=n))
            if "project" in getattr(self._window, "pages", {}):
                self._window.pages["project"].refresh()
            self._load()
        run_async(self, lambda: gdrive.restore_backup(
            project["path"], project["name"], backup, files), done)


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

        # bringing in the other computers' changes — on unless turned off
        self.autopull = QCheckBox(_("autopull_setting"))
        self.autopull.setChecked(bool(config.get("drive_autopull")))
        gd_layout.addWidget(self.autopull)
        hint = QLabel(_("autopull_setting_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        gd_layout.addWidget(hint)
        gd_form = QFormLayout()
        self.every = QComboBox()
        self.every.addItems([_("every_min", n=m) for m in AUTOPULL_MINUTES])
        current = config.get("drive_autopull_minutes")
        self.every.setCurrentIndex(AUTOPULL_MINUTES.index(current)
                                   if current in AUTOPULL_MINUTES else 1)
        self.every.setEnabled(self.autopull.isChecked())
        gd_form.addRow(_("autopull_every"), self.every)
        self.keep = QComboBox()
        self.keep.addItems([_("keep_days", n=d) if d else _("keep_forever")
                            for d in BACKUP_DAYS])
        days = config.get("drive_backup_days")
        self.keep.setCurrentIndex(BACKUP_DAYS.index(days)
                                  if days in BACKUP_DAYS else 1)
        self.keep.currentIndexChanged.connect(lambda i: config.set(
            "drive_backup_days", BACKUP_DAYS[i]))
        gd_form.addRow(_("backup_keep"), self.keep)
        gd_layout.addLayout(gd_form)

        def autopull_changed(*_a):
            config.set("drive_autopull", self.autopull.isChecked())
            config.set("drive_autopull_minutes",
                       AUTOPULL_MINUTES[self.every.currentIndex()])
            self.every.setEnabled(self.autopull.isChecked())
            if hasattr(self.window, "restart_autopull"):
                self.window.restart_autopull(
                    first_delay=2 if self.autopull.isChecked() else None)
        self.autopull.toggled.connect(autopull_changed)
        self.every.currentIndexChanged.connect(autopull_changed)
        show = QPushButton(_("backups_show"))
        show.clicked.connect(lambda: config.active_project() and BackupsDialog(
            self, config.active_project()).exec())
        gd_layout.addWidget(show)
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
