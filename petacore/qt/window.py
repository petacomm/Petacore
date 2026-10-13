"""Qt (KDE Plasma) main window for Petacore."""

import os
import shutil

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (QColor, QIcon, QKeySequence, QPainter,
                           QPalette, QPen, QShortcut)
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel,
                               QSizePolicy,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPushButton, QStackedWidget,
                               QStatusBar, QToolBar, QVBoxLayout, QWidget)

from .. import gitops, sandbox, stats
from ..config import config
from ..i18n import translator as _
from ..snapshots import SnapshotManager
from . import theme
from .dialogs import (GitHubLoginDialog, NewProjectDialog, SettingsDialog,
                      SetupWizard)
from .pages import (EditorPage, KeysPage, LiveServerPage, OverviewPage,
                    PackagePage, ProjectPage, RepoPage, SandboxPage,
                    SnapshotsPage, TerminalPage, UpdatesPage, VersionsPage,
                    VirusScanPage, run_async)

# The native set: every page, for a project that targets an operating
# system (or nothing in particular). Package and Keys build and sign a
# .deb; Sandbox tries the project in an isolated shell.
PAGES = [
    ("overview_page", OverviewPage),
    ("project", ProjectPage),
    ("editor", EditorPage),
    ("snapshots", SnapshotsPage),
    ("terminal", TerminalPage),
    ("sandbox", SandboxPage),
    ("package", PackagePage),
    ("versions_page", VersionsPage),
    ("repo_page", RepoPage),
    ("keys_page", KeysPage),
    ("vt_page", VirusScanPage),
    ("updates", UpdatesPage),
]

# A web project is never packaged as a .deb and never signed, so Package
# and Keys have nothing to do there; Sandbox — a shell to try things in —
# is replaced by Live Server, a browser preview with reload.
WEB_PAGES = [
    ("overview_page", OverviewPage),
    ("project", ProjectPage),
    ("editor", EditorPage),
    ("snapshots", SnapshotsPage),
    ("terminal", TerminalPage),
    ("live_server", LiveServerPage),
    ("repo_page", RepoPage),
    ("vt_page", VirusScanPage),
    ("updates", UpdatesPage),
]


def page_spec_for(project):
    """Which pages belong in the sidebar for this project.

    A project that isn't (confidently) a web app keeps the full native set
    unchanged — this only ever swaps in the web set on a clear, positive
    match, never on the absence of one, so an unfamiliar or ambiguous
    project (BSD, an unrecognised stack, anything quick_web_check can't
    place) is treated exactly like Linux, Windows or macOS: nothing changes.
    """
    if project and stats.quick_web_check(project["path"]):
        return WEB_PAGES
    return PAGES


class _ProgressRing(QWidget):
    """A small circle that fills as work proceeds, and turns while the total
    is still unknown."""

    SIZE = 16

    def __init__(self):
        super().__init__()
        self.setFixedSize(self.SIZE, self.SIZE)
        self._fraction = None
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def set_fraction(self, fraction):
        self._fraction = fraction
        if fraction is None:
            if not self._timer.isActive():
                self._timer.start(40)
        else:
            self._timer.stop()
        self.update()

    def stop(self):
        self._timer.stop()

    def _advance(self):
        self._angle = (self._angle + 240) % 5760
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        colour = self.palette().color(QPalette.WindowText)
        rect = self.rect().adjusted(2, 2, -2, -2)

        faint = QColor(colour)
        faint.setAlphaF(0.25)
        painter.setPen(QPen(faint, 2.4))
        painter.drawArc(rect, 0, 5760)

        painter.setPen(QPen(colour, 2.4))
        if self._fraction is None:
            painter.drawArc(rect, -self._angle, 1440)
        else:
            span = int(5760 * max(0.0, min(1.0, self._fraction)))
            painter.drawArc(rect, 90 * 16, -span)


class PetacoreWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("Petacore")
        self.resize(1150, 740)
        icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "icons", "256x256", "io.petacore.Petacore.png")
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self._autosave = QTimer(self)
        self._autosave.timeout.connect(self._autosave_tick)
        self.pages = {}
        self.build()
        self.restart_autosave()
        self._autopull = QTimer(self)
        self._autopull.timeout.connect(self._autopull_tick)
        self._autopull_busy = False
        self.restart_autopull(first_delay=8)

        if config.get("first_run"):
            QTimer.singleShot(200, self.show_setup)

    # -- construction ---------------------------------------------------------
    def build(self):
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        new_btn = QPushButton(_("new_project"))
        new_btn.clicked.connect(self.show_new_project)
        toolbar.addWidget(new_btn)

        self.project_box = QComboBox()
        projects = config.get("projects")
        self._project_paths = [p["path"] for p in projects]
        from .. import crypto
        from .dialogs import brand_icon
        # dark themes need the white padlock, light ones the dark-ink version
        mode = config.get("theme")
        if mode == "system":
            from .main import _system_prefers_dark
            dark = _system_prefers_dark()
        else:
            dark = mode == "dark"
        lock = brand_icon("lock-white" if dark else "lock")
        for p in projects:
            if crypto.is_encrypted(p) and lock is not None:
                self.project_box.addItem(lock, p["name"])
            else:
                self.project_box.addItem(p["name"])
        active = config.get("active_project")
        if active in self._project_paths:
            self.project_box.setCurrentIndex(
                self._project_paths.index(active))
        self.project_box.currentIndexChanged.connect(self._switch_project)
        toolbar.addWidget(self.project_box)

        edit_btn = QPushButton("✎")
        edit_btn.setToolTip(_("edit_project"))
        edit_btn.setFixedWidth(38)
        edit_btn.clicked.connect(self.edit_project)
        toolbar.addWidget(edit_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)

        # Two destinations side by side: code to GitHub, files to Drive.
        from .dialogs import brand_icon
        sync_btn = QPushButton(_("pub_title"))
        sync_btn.setToolTip(_("pub_title"))
        gh_icon = brand_icon("github")
        if gh_icon:
            sync_btn.setIcon(gh_icon)
        sync_btn.setProperty("primary", True)
        sync_btn.clicked.connect(self.start_sync)
        toolbar.addWidget(sync_btn)
        self.sync_btn = sync_btn

        drive_btn = QPushButton(_("sync_drive"))
        drive_btn.setToolTip(_("sync_drive"))
        gd_icon = brand_icon("gdrive")
        if gd_icon:
            drive_btn.setIcon(gd_icon)
        drive_btn.clicked.connect(self.start_drive_sync)
        toolbar.addWidget(drive_btn)
        self.drive_sync_btn = drive_btn

        settings_btn = QPushButton(_("settings"))
        settings_btn.clicked.connect(lambda: SettingsDialog(self).exec())
        toolbar.addWidget(settings_btn)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(210)
        self.sidebar.currentRowChanged.connect(self._page_changed)
        # Sidebar plus, beneath it, the progress line for long operations —
        # the same position a file manager reports copies from.
        side_column = QWidget()
        side_column.setFixedWidth(210)
        side_layout = QVBoxLayout(side_column)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)
        self.sidebar.setFixedWidth(210)
        side_layout.addWidget(self.sidebar, 1)
        side_layout.addWidget(self._build_operation_widget())
        layout.addWidget(side_column)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())

        if config.active_project():
            self.page_spec = page_spec_for(config.active_project())
            for key, cls in self.page_spec:
                page = cls(self)
                self.pages[key] = page
                self.stack.addWidget(page)
                self.sidebar.addItem(QListWidgetItem(_(key)))
            self.sidebar.setCurrentRow(0)
        else:
            self.page_spec = PAGES
            empty = QWidget()
            empty_layout = QVBoxLayout(empty)
            empty_layout.addStretch(1)
            title = QLabel(_("no_project_title"))
            title.setProperty("title", True)
            title.setAlignment(Qt.AlignCenter)
            empty_layout.addWidget(title)
            body = QLabel(_("no_project_body"))
            body.setAlignment(Qt.AlignCenter)
            body.setProperty("dim", True)
            empty_layout.addWidget(body)
            btn = QPushButton(_("new_project"))
            btn.setProperty("primary", True)
            btn.clicked.connect(self.show_new_project)
            empty_layout.addWidget(btn, alignment=Qt.AlignCenter)
            empty_layout.addStretch(1)
            self.stack.addWidget(empty)
            self.sync_btn.setEnabled(False)

        QShortcut(QKeySequence("Ctrl+N"), self,
                  activated=self.show_new_project)
        QShortcut(QKeySequence("Ctrl+,"), self,
                  activated=lambda: SettingsDialog(self).exec())

    def rebuild(self):
        # Tear down whatever the previous build left running — a live
        # server, a sandbox session — before the pages that own them are
        # replaced.
        for page in self.pages.values():
            if hasattr(page, "stop"):
                page.stop()
            if hasattr(page, "end_all"):
                page.end_all()
        self.pages.clear()
        old = self.centralWidget()
        for bar in self.findChildren(QToolBar):
            self.removeToolBar(bar)
        if old:
            old.deleteLater()
        self.build()
        self.restart_autosave()

    def apply_theme(self):
        """Cross-dissolve between light and dark instead of snapping.

        Every visible window — this one and any dialog on top of it, such as
        Settings — gets its own veil in the incoming background colour. They
        are animated on the same clock, so the whole screen moves as one
        instead of the dialog snapping while the window behind it dissolves.
        """
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        from PySide6.QtWidgets import QApplication as _QApp
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        mode = config.get("theme")
        if mode == "system":
            from .main import _system_prefers_dark
            dark = _system_prefers_dark()
        else:
            dark = mode == "dark"
        colour = "#1e1e1e" if dark else "#fafafa"

        def make_veil(widget):
            veil = QWidget(widget)
            veil.setAttribute(Qt.WA_TransparentForMouseEvents)
            veil.setStyleSheet(f"background: {colour};")
            veil.setGeometry(widget.rect())
            effect = QGraphicsOpacityEffect(veil)
            effect.setOpacity(0.0)
            veil.setGraphicsEffect(effect)
            veil.show()
            veil.raise_()
            return veil, effect

        targets = [self] + [w for w in _QApp.topLevelWidgets()
                            if w is not self and w.isVisible()]
        veils, ins, outs = [], [], []
        for widget in targets:
            veil, effect = make_veil(widget)
            veils.append(veil)

            fade_in = QPropertyAnimation(effect, b"opacity", self)
            fade_in.setDuration(190)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.setEasingCurve(QEasingCurve.InOutCubic)

            fade_out = QPropertyAnimation(effect, b"opacity", self)
            fade_out.setDuration(320)     # the reveal gets the gentler half
            fade_out.setStartValue(1.0)
            fade_out.setEndValue(0.0)
            fade_out.setEasingCurve(QEasingCurve.InOutCubic)

            ins.append(fade_in)
            outs.append(fade_out)

        def swap():
            self.app.apply_theme()
            for animation in outs:
                animation.start()

        # only the first animation drives the swap, the rest just follow
        ins[0].finished.connect(swap)
        outs[0].finished.connect(
            lambda: [veil.deleteLater() for veil in veils])
        self._theme_anims = (ins, outs, veils)   # keep them alive
        for animation in ins:
            animation.start()

    # -- navigation ------------------------------------------------------------
    def _page_changed(self, row):
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        key = self.page_spec[row][0] if row < len(self.page_spec) else None
        page = self.pages.get(key)
        if page and hasattr(page, "refresh"):
            page.refresh()

    def select_page(self, key):
        for i, (name, _cls) in enumerate(self.page_spec):
            if name == key:
                self.sidebar.setCurrentRow(i)
                return

    def open_file(self, path):
        if "editor" in self.pages:
            self.select_page("editor")
            self.pages["editor"].open_file(path)

    # -- long operations ---------------------------------------------------------
    def begin_operation(self, label):
        """Report a long operation at the foot of the sidebar."""
        if getattr(self, "_op_widget", None) is None:
            return
        metrics = self._op_label.fontMetrics()
        self._op_label.setText(
            metrics.elidedText(label, Qt.ElideRight, 150))
        self._op_label.setToolTip(label)
        self._op_ring.set_fraction(None)      # indeterminate until told
        self._op_widget.setVisible(True)
        self._op_count = getattr(self, "_op_count", 0) + 1

    def update_operation(self, percent):
        if getattr(self, "_op_widget", None) is None or percent is None:
            return
        try:
            value = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return
        self._op_ring.set_fraction(value / 100.0)

    def end_operation(self):
        self._op_count = max(0, getattr(self, "_op_count", 1) - 1)
        if self._op_count or getattr(self, "_op_widget", None) is None:
            return
        self._op_ring.stop()
        self._op_widget.setVisible(False)

    def _build_operation_widget(self):
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(12, 6, 12, 10)
        layout.setSpacing(10)
        self._op_ring = _ProgressRing()
        layout.addWidget(self._op_ring)
        self._op_label = QLabel()
        self._op_label.setStyleSheet("font-size: 9pt;")
        # The sidebar is narrow, so long labels are cut with an ellipsis
        # rather than pushing the layout wider.
        self._op_label.setMinimumWidth(0)
        self._op_label.setTextInteractionFlags(Qt.NoTextInteraction)
        layout.addWidget(self._op_label, 1)
        holder.setVisible(False)
        self._op_widget = holder
        return holder

    def toast(self, text):
        self.statusBar().showMessage(text, 6000)

    def show_save_animation(self, filename):
        """A small floppy disk floats up the screen: it grows on its way to
        the middle, then shrinks and fades as it leaves the top."""
        from .. import saveanim
        from PySide6.QtCore import QElapsedTimer, QTimer
        from PySide6.QtGui import QPainter

        class FloppyOverlay(QWidget):
            def __init__(self, parent):
                super().__init__(parent)
                self.setAttribute(Qt.WA_TransparentForMouseEvents)
                self.progress = 0.0
                self.resize(parent.size())
                self.show()
                self.raise_()

            def paintEvent(self, _event):
                opacity = saveanim.alpha(self.progress)
                if opacity <= 0:
                    return
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing)
                size = saveanim.BASE_SIZE * saveanim.scale(self.progress)
                cx = self.width() / 2
                cy = saveanim.y_fraction(self.progress) * self.height()
                saveanim.draw_qt(painter, cx, cy, size, opacity)

        overlay = FloppyOverlay(self.centralWidget() or self)
        clock = QElapsedTimer()
        clock.start()
        timer = QTimer(self)

        def step():
            p = clock.elapsed() / saveanim.DURATION_MS
            if p >= 1.0:
                timer.stop()
                overlay.deleteLater()
                return
            overlay.progress = p
            overlay.update()

        timer.timeout.connect(step)
        timer.start(16)
        self.statusBar().showMessage(_("saved", f=filename), 2000)

    def _switch_project(self, index):
        if 0 <= index < len(self._project_paths):
            config.set("active_project", self._project_paths[index])
            self.rebuild()

    # -- project actions -------------------------------------------------------
    def show_new_project(self):
        dialog = NewProjectDialog(self)
        if dialog.exec():
            self.rebuild()
            if getattr(dialog, "cloned_name", None):
                self.offer_scan(dialog.cloned_name)

    def offer_scan(self, name):
        """Code just downloaded from GitHub: ask once whether VirusTotal
        should look at it before anything in it is run."""
        box = QMessageBox(self)
        box.setWindowTitle(_("clone_scan_q"))
        box.setText(_("clone_scan_q"))
        box.setInformativeText(_("clone_scan_body", name=name))
        scan = box.addButton(_("clone_scan_yes"), QMessageBox.AcceptRole)
        box.addButton(_("clone_scan_no"), QMessageBox.RejectRole)
        box.setDefaultButton(scan)
        box.exec()
        if box.clickedButton() is scan:
            self.start_scan()

    def start_scan(self):
        """Open the virus scan page and start checking the project."""
        if "vt_page" not in self.pages:
            return
        self.select_page("vt_page")
        page = self.pages["vt_page"]
        if page.vt.key_present():
            page._scan()
        else:
            page._ask_key()

    def edit_project(self):
        project = config.active_project()
        if not project:
            return
        from .dialogs import EditProjectDialog
        if EditProjectDialog(self, project).exec():
            self.rebuild()

    def show_setup(self):
        SetupWizard(self).exec()

    # -- sync -------------------------------------------------------------------
    def start_sync(self):
        """The GitHub button: push the code and, by default, publish it as
        a new version — all in one dialog."""
        project = config.active_project()
        if not project:
            return
        from .dialogs import PublishDialog
        dialog = PublishDialog(self, project)
        dialog.exec()
        result = dialog.result
        if result:
            self.toast(_("pub_done_release", title=result["title"])
                       if result["title"] else _("pub_done_push"))
            if "project" in self.pages:
                self.pages["project"].refresh()

    # -- deploy ------------------------------------------------------------------
    def start_drive_sync(self):
        """Upload the project files to Drive, without touching GitHub."""
        if "project" in self.pages:
            self.select_page("project")
            self.pages["project"]._drive_sync()

    def prompt_deploy(self, file_path):
        project = config.active_project()
        if not project:
            return
        if QMessageBox.question(self, _("deploy_question"),
                                _("deploy_detail")) != QMessageBox.Yes:
            return
        deploy_path = project.get("deploy_path", "")
        if not deploy_path or not os.path.isdir(deploy_path):
            deploy_path = QFileDialog.getExistingDirectory(
                self, _("deploy_choose"))
            if not deploy_path:
                return
            config.set_project_field(project["path"], "deploy_path",
                                     deploy_path)
        try:
            rel = os.path.relpath(os.path.realpath(file_path),
                                  os.path.realpath(project["path"]))
            dest = os.path.join(deploy_path, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(file_path, dest)
            self.toast(_("deployed", p=dest))
        except OSError as e:
            self.toast(f"{_('deploy_failed')}: {e}")

    # -- Google Drive imports ------------------------------------------------------
    def import_drive_projects(self, auto=False):
        from .. import gdrive

        def done(names, error):
            if error or not names:
                if not auto:
                    self.toast(_("no_new_drive"))
                return
            existing = {p["name"].lower() for p in config.get("projects")}
            new = [n for n in names if n.lower() not in existing]
            if not new:
                if not auto:
                    self.toast(_("no_new_drive"))
                return
            listing = "\n• " + "\n• ".join(new[:12])
            if QMessageBox.question(
                    self, _("drive_found", n=len(new)),
                    listing + "\n\n"
                    + _("drive_import_body")) == QMessageBox.Yes:
                self.import_projects(new)

        run_async(self, gdrive.list_remote_projects, done)

    def import_projects(self, names):
        from .. import gdrive
        base = os.path.join(os.path.expanduser("~"), "Projects")

        def work():
            done_paths = []
            for name in names:
                local = os.path.join(base, name)
                n = 2
                # never pour a project into a folder that already holds
                # something else
                while os.path.isdir(local) and os.listdir(local):
                    local = os.path.join(base, f"{name} ({n})")
                    n += 1
                os.makedirs(local, exist_ok=True)
                state = gdrive.pull_project(name, local)
                if not gitops.is_repo(local):
                    try:
                        gitops.init(local)
                    except gitops.GitError:
                        pass
                done_paths.append((name, local, state))
            return done_paths

        def done(paths, error):
            self.end_operation()
            if error:
                self.toast(f"{_('drive_failed')}: {error}")
            for name, local, state in (paths or []):
                config.add_project(name, local, "")
                gdrive.note_synced(local, state)
                self.toast(_("imported", p=name))
            if paths:
                self.rebuild()

        self.begin_operation(_("op_import"))
        run_async(self, work, done)

    # -- Drive: bring in what the other computers sent ----------------------------
    def restart_autopull(self, first_delay=None):
        """(Re)start the Drive check; see the GNOME window for the rules."""
        self._autopull.stop()
        if not config.get("drive_autopull"):
            return
        minutes = max(1, int(config.get("drive_autopull_minutes") or 10))
        self._autopull.start(minutes * 60 * 1000)
        if first_delay:
            QTimer.singleShot(first_delay * 1000, self._autopull_tick)

    def _autopull_tick(self):
        if config.get("drive_autopull") and not self._autopull_busy:
            self.autopull()

    def autopull(self):
        from .. import crypto, gdrive
        if not (gdrive.available() and gdrive.connected()):
            return
        self._autopull_busy = True
        projects = [p for p in config.get("projects")
                    if not crypto.is_encrypted(p)]
        known = {p["name"].lower() for p in config.get("projects")}

        def work():
            pulled, conflicts, new = [], [], []
            for project in projects:
                try:
                    where, state = gdrive.status(project)
                    if where == "pull":
                        gdrive.pull_project(project["name"], project["path"])
                        pulled.append((project, state))
                    elif where == "conflict" and state.get("rev") != \
                            project.get("drive_skip_rev"):
                        conflicts.append((project, state))
                except gdrive.DriveError:
                    continue
            new = [n for n in gdrive.list_remote_projects()
                   if n.lower() not in known]
            return pulled, conflicts, new

        def done(result, error):
            self._autopull_busy = False
            if error or not result:
                return
            pulled, conflicts, new = result
            for project, state in pulled:
                self.toast(_("autopull_done", p=project["name"],
                             host=state.get("host") or "?"))
                if project["path"] == config.get("active_project") \
                        and "project" in self.pages:
                    self.pages["project"].refresh()
            for project, state in conflicts:
                self._ask_conflict(project, state)
            if new:
                self.import_projects(new)

        run_async(self, work, done)

    def _ask_conflict(self, project, state):
        import time as _time
        from .. import gdrive
        box = QMessageBox(self)
        box.setWindowTitle(_("drive_conflict_title", p=project["name"]))
        box.setText(_("drive_conflict_title", p=project["name"]))
        box.setInformativeText(_("drive_conflict_body",
                                 host=state.get("host") or "?",
                                 when=_time.strftime("%d.%m %H:%M",
                                                     _time.localtime(
                                                         state.get("time", 0)))))
        take = box.addButton(_("drive_conflict_take"), QMessageBox.AcceptRole)
        send = box.addButton(_("drive_conflict_send"),
                             QMessageBox.DestructiveRole)
        box.addButton(_("drive_conflict_later"), QMessageBox.RejectRole)
        box.setDefaultButton(take)
        box.exec()
        if box.clickedButton() is take:
            self.begin_operation(_("op_drive"))
            run_async(self, lambda: gdrive.pull_project(project["name"],
                                                        project["path"]),
                      lambda r, e: self.pulled_by_hand(project, e))
        elif box.clickedButton() is send:
            config.set_project_field(project["path"], "drive_skip_rev",
                                     state.get("rev"))
            config.set("active_project", project["path"])
            self.rebuild()
            self.start_drive_sync()
        else:
            config.set_project_field(project["path"], "drive_skip_rev",
                                     state.get("rev"))

    def pulled_by_hand(self, project, error):
        self.end_operation()
        if error:
            self.toast(f"{_('drive_failed')}: {error}")
            return
        self.toast(_("pull_done", p=project["name"]))
        if project["path"] == config.get("active_project") \
                and "project" in self.pages:
            self.pages["project"].refresh()

    def show_backups(self, project=None):
        from .dialogs import BackupsDialog
        project = project or config.active_project()
        if project:
            BackupsDialog(self, project).exec()

    # -- autosave -------------------------------------------------------------------
    def restart_autosave(self):
        self._autosave.stop()
        if config.get("autosave_enabled"):
            minutes = max(1, int(config.get("autosave_minutes")))
            self._autosave.start(minutes * 60 * 1000)

    def _autosave_tick(self):
        project = config.active_project()
        if project:
            run_async(self,
                      lambda: SnapshotManager(project["path"]).create(
                          kind="auto"),
                      lambda r, e: None)

    def closeEvent(self, event):
        sandbox.destroy_all()
        for page in self.pages.values():
            if hasattr(page, "stop"):
                page.stop()
            if hasattr(page, "end_all"):
                page.end_all()
        from .pages import wait_for_workers
        wait_for_workers()
        super().closeEvent(event)
