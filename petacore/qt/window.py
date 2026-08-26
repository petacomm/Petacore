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

from .. import gitops, sandbox
from ..config import config
from ..i18n import translator as _
from ..snapshots import SnapshotManager
from . import theme
from .dialogs import (GitHubLoginDialog, NewProjectDialog, SettingsDialog,
                      SetupWizard)
from .pages import (EditorPage, KeysPage, OverviewPage, PackagePage,
                    ProjectPage, SandboxPage, SnapshotsPage, TerminalPage,
                    UpdatesPage, run_async)

PAGES = [
    ("overview_page", OverviewPage),
    ("project", ProjectPage),
    ("editor", EditorPage),
    ("snapshots", SnapshotsPage),
    ("terminal", TerminalPage),
    ("sandbox", SandboxPage),
    ("package", PackagePage),
    ("keys_page", KeysPage),
    ("updates", UpdatesPage),
]


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
        sync_btn = QPushButton(_("sync_github"))
        sync_btn.setToolTip(_("sync_github"))
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
            for key, cls in PAGES:
                page = cls(self)
                self.pages[key] = page
                self.stack.addWidget(page)
                self.sidebar.addItem(QListWidgetItem(_(key)))
            self.sidebar.setCurrentRow(0)
        else:
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
        key = PAGES[row][0] if row < len(PAGES) else None
        page = self.pages.get(key)
        if page and hasattr(page, "refresh"):
            page.refresh()

    def select_page(self, key):
        for i, (name, _cls) in enumerate(PAGES):
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
        if NewProjectDialog(self).exec():
            self.rebuild()

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
        project = config.active_project()
        if not project:
            return
        if QMessageBox.question(
                self, _("sync_question"),
                _("sync_detail")) != QMessageBox.Yes:
            return
        self.sync_btn.setEnabled(False)
        self.begin_operation(_("op_github"))

        def work():
            """Code to GitHub, then plans + public keys to Drive."""
            from .. import github
            token = config.get("github_token")
            if not token:
                token = github.gh_token() or ""
                if token:
                    config.set("github_token", token)
            gitops.sync(project["path"], token)

            # Plans and keys have their own buttons now, so this stays a
            # pure code push.
            return {}

        def done(result, error):
            self.end_operation()
            self.sync_btn.setEnabled(True)
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
                         ("authentication", "denied", "403", "username")):
                    self.toast(_("sync_failed"))
                    GitHubLoginDialog(self).exec()
                else:
                    self.toast(f"{_('sync_failed')}: {err}")
            else:
                self.toast(_("sync_done"))
            if "project" in self.pages:
                self.pages["project"].refresh()

        run_async(self, work, done)

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
                os.makedirs(local, exist_ok=True)
                gdrive.pull_project(name, local)
                if not gitops.is_repo(local):
                    try:
                        gitops.init(local)
                    except gitops.GitError:
                        pass
                done_paths.append((name, local))
            return done_paths

        def done(paths, error):
            self.end_operation()
            if error:
                self.toast(f"{_('drive_failed')}: {error}")
            for name, local in (paths or []):
                config.add_project(name, local, "")
                self.toast(_("imported", p=name))
            if paths:
                self.rebuild()

        self.begin_operation(_("op_import"))
        run_async(self, work, done)

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
