"""Qt (KDE Plasma) pages for Petacore.

The backend modules are shared with the GNOME build — only the presentation
layer differs, so both versions behave identically.
"""

import os
import re
import subprocess
import time

from PySide6.QtCore import QObject, QProcess, Qt, QThread, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QSpinBox, QSplitter, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import debbuild, detect, gitops, gpgsign
from ..config import config
from ..i18n import translator as _
from ..snapshots import SnapshotManager
from ..util import human_size

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".html", ".css", ".c", ".h", ".cpp",
            ".hpp", ".cs", ".java", ".rs", ".go", ".php", ".rb", ".swift",
            ".kt", ".lua", ".sh", ".sql", ".json", ".yml", ".yaml", ".toml"}
DOC_EXT = {".md", ".txt", ".pdf", ".csv", ".odt", ".docx"}
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

FILTERS = [("all", "filter_all"), ("code", "filter_code"),
           ("images", "filter_images"), ("media", "filter_media"),
           ("docs", "filter_docs"), ("folders", "filter_folders")]


def matches_filter(name, is_dir, mode):
    if mode == "all":
        return True
    if mode == "folders":
        return is_dir
    if is_dir:
        return False
    ext = os.path.splitext(name)[1].lower()
    return {
        "code": ext in CODE_EXT,
        "images": ext in IMAGE_EXT,
        "media": ext in AUDIO_EXT or ext in VIDEO_EXT,
        "docs": ext in DOC_EXT,
    }.get(mode, True)


class Worker(QThread):
    """Run a callable off the UI thread; emits (result, error)."""
    finished_with = Signal(object, object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.finished_with.emit(self._fn(), None)
        except Exception as e:  # noqa: BLE001 — surfaced in the UI
            self.finished_with.emit(None, e)


_WORKERS = []


def run_async(parent, fn, on_done):
    """Run `fn` off the UI thread. The worker is kept in a module-level list
    until it finishes, so it is never garbage-collected while running."""
    worker = Worker(fn)
    worker.finished_with.connect(on_done)

    def _cleanup():
        if worker in _WORKERS:
            _WORKERS.remove(worker)

    worker.finished.connect(_cleanup)
    _WORKERS.append(worker)
    worker.start()
    return worker


def wait_for_workers(timeout_ms=3000):
    """Let running workers finish (used on shutdown)."""
    for worker in list(_WORKERS):
        worker.wait(timeout_ms)


# --------------------------------------------------------------------------- #
# Project — Dolphin-style file browser
# --------------------------------------------------------------------------- #
class ProjectPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        project = config.active_project()
        self.root = os.path.realpath(project["path"]) if project else None
        self.current = self.root
        self._hist = [self.current] if self.current else []
        self._hist_i = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        if self.root:
            os.makedirs(os.path.join(self.root, "media"), exist_ok=True)

        # toolbar
        bar = QHBoxLayout()
        self.up_btn = QPushButton("←")
        self.up_btn.setFixedWidth(40)
        self.up_btn.setToolTip(_("go_up"))
        self.up_btn.clicked.connect(self.go_up)
        bar.addWidget(self.up_btn)

        self.path_label = QLabel()
        self.path_label.setProperty("heading", True)
        bar.addWidget(self.path_label, 1)

        media_btn = QPushButton(_("media_files"))
        media_btn.clicked.connect(self.go_media)
        bar.addWidget(media_btn)

        refresh = QPushButton(_("refresh"))
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        self.status_line = QLabel()
        self.status_line.setProperty("dim", True)
        layout.addWidget(self.status_line)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # search + filter
        srow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(_("search"))
        self.search.textChanged.connect(self.refresh)
        srow.addWidget(self.search, 1)

        self.deep = QCheckBox(_("search_all"))
        self.deep.stateChanged.connect(self.refresh)
        srow.addWidget(self.deep)

        self.filter_box = QComboBox()
        for _code, key in FILTERS:
            self.filter_box.addItem(_(key))
        self.filter_box.currentIndexChanged.connect(self.refresh)
        srow.addWidget(self.filter_box)
        layout.addLayout(srow)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.itemActivated.connect(self._activate)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.list, 1)

        QShortcut(QKeySequence("Ctrl+F"), self,
                  activated=lambda: self.search.setFocus())
        QShortcut(QKeySequence(Qt.Key_Delete), self.list,
                  activated=self._delete_selected)

        self.refresh()

    # -- navigation ---------------------------------------------------------
    def navigate_to(self, path):
        if path == self.current:
            return
        if self.search.text():
            self.search.clear()
        self.current = path
        del self._hist[self._hist_i + 1:]
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

    def go_up(self):
        if self.current and self.current != self.root:
            self.navigate_to(os.path.dirname(self.current))

    def go_media(self):
        if self.root:
            media = os.path.join(self.root, "media")
            os.makedirs(media, exist_ok=True)
            self.navigate_to(media)

    def mousePressEvent(self, event):
        if event.button() == Qt.BackButton:
            self.go_back()
        elif event.button() == Qt.ForwardButton:
            self.go_forward()
        else:
            super().mousePressEvent(event)

    # -- listing -------------------------------------------------------------
    def _collect(self, deep):
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
        else:
            try:
                for e in os.scandir(self.current):
                    if e.name in HIDDEN or e.name.startswith("."):
                        continue
                    is_dir = e.is_dir(follow_symlinks=False)
                    if is_encrypted_artifact(e.name, is_dir):
                        continue      # never show encrypted blobs
                    items.append((e.path, e.name, is_dir))
            except OSError:
                pass
        items.sort(key=lambda i: (not i[2], i[1].lower()))
        return items

    def refresh(self):
        self.list.clear()
        if not self.current:
            return
        rel = os.path.relpath(self.current, self.root)
        self.path_label.setText("/" if rel == "." else "/" + rel)
        self.up_btn.setEnabled(self.current != self.root)

        query = self.search.text().strip().lower()
        mode = FILTERS[self.filter_box.currentIndex()][0]
        deep = self.deep.isChecked() and bool(query)

        shown = 0
        for path, name, is_dir in self._collect(deep):
            if query and query not in name.lower():
                continue
            if not matches_filter(name, is_dir, mode):
                continue
            label = ("📁 " if is_dir else "📄 ") + name
            if not is_dir:
                try:
                    label += f"    {human_size(os.path.getsize(path))}"
                except OSError:
                    pass
            if deep:
                rel_dir = os.path.relpath(os.path.dirname(path), self.root)
                label += f"    ({'/' if rel_dir == '.' else '/' + rel_dir})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, (path, is_dir))
            self.list.addItem(item)
            shown += 1
            if shown >= 500:
                break

        if shown == 0:
            self.list.addItem(QListWidgetItem(
                _("no_matches", q=query) if query else _("empty_folder")))
        if query or mode != "all":
            self.path_label.setText(
                f'{self.path_label.text()}   —   {_("results_n", n=shown)}')

        if self.root:
            run_async(self, lambda: detect.project_summary(self.root),
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
        self.status_line.setText(
            f'{info["branch"] or "—"}   ·   {sync}   ·   '
            f'{_("procs_n", n=len(info["processes"]))}')

    # -- actions --------------------------------------------------------------
    def _selected_paths(self):
        out = []
        for item in self.list.selectedItems():
            data = item.data(Qt.UserRole)
            if data:
                out.append(data)
        return out

    def _activate(self, item):
        data = item.data(Qt.UserRole)
        if not data:
            return
        path, is_dir = data
        if is_dir:
            self.navigate_to(path)
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXT | AUDIO_EXT | VIDEO_EXT or \
                os.path.getsize(path) > MAX_EDIT_SIZE:
            subprocess.Popen(["xdg-open", path])
            return
        self.window.open_file(path)

    def _context_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        sel = self._selected_paths()
        if sel:
            menu.addAction(_("open_item"),
                           lambda: self._activate(self.list.currentItem()))
            if len(sel) == 1:
                menu.addAction(_("rename"), lambda: self._rename(sel[0][0]))
            menu.addAction(_("copy_path"), lambda: self._copy_path(sel[0][0]))
            menu.addAction(_("delete"), self._delete_selected)
            menu.addSeparator()
        menu.addAction(_("new_folder"), self._new_folder)
        menu.exec(self.list.mapToGlobal(pos))

    def _copy_path(self, path):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(path)
        self.window.toast(_("path_copied"))

    def _rename(self, path):
        name, ok = QInputDialog.getText(self, _("rename"), _("rename"),
                                        QLineEdit.Normal,
                                        os.path.basename(path))
        if ok and name.strip() and "/" not in name:
            try:
                os.rename(path, os.path.join(os.path.dirname(path),
                                             name.strip()))
            except OSError as e:
                self.window.toast(str(e))
            self.refresh()

    def _new_folder(self):
        name, ok = QInputDialog.getText(self, _("new_folder"),
                                        _("folder_name"), QLineEdit.Normal,
                                        _("new_folder"))
        if ok and name.strip() and "/" not in name:
            try:
                os.makedirs(os.path.join(self.current, name.strip()))
            except OSError as e:
                self.window.toast(str(e))
            self.refresh()

    def _delete_selected(self):
        import shutil
        sel = self._selected_paths()
        if not sel:
            return
        if QMessageBox.question(
                self, _("delete"),
                f'{_("delete")}  ({len(sel)})') != QMessageBox.Yes:
            return
        for path, is_dir in sel:
            try:
                if is_dir:
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except OSError as e:
                self.window.toast(str(e))
        self.window.toast(_("files_deleted", n=len(sel)))
        self.refresh()

    # -- Google Drive ----------------------------------------------------------
    def _drive_sync(self):
        from .. import gdrive
        project = config.active_project()
        if not project:
            return
        if not gdrive.available():
            self.window.toast(_("rclone_missing"))
            return
        if not gdrive.connected():
            if QMessageBox.question(self, _("connect_gdrive"),
                                    _("gdrive_hint") + "\n\n"
                                    + _("gdrive_wait")) != QMessageBox.Yes:
                return
            self.window.toast(_("gdrive_wait"))
            run_async(self, gdrive.connect,
                      lambda r, e: self._start_drive(project))
            return
        self._start_drive(project)

    def _set_drive_busy(self, enabled):
        """The Drive action lives in the toolbar at the top now, so that is
        the button that greys out while an upload is running."""
        button = getattr(self.window, "drive_sync_btn", None)
        if button is not None:
            button.setEnabled(enabled)

    def _start_drive(self, project, confirmed=False):
        from .. import gdrive
        if not confirmed and config.get("drive_shrink_warning"):
            self._check_shrink(project)
            return
        self._begin_drive(project)

    def _check_shrink(self, project):
        """An upload smaller than what is already in Drive usually means
        another computer holds work this one has not fetched."""
        from .. import gdrive

        def measure():
            return (gdrive.local_size(project["path"]),
                    gdrive.remote_size(project["name"]))

        def measured(sizes, error):
            self.window.end_operation()
            local, remote = sizes if sizes else (0, -1)
            if error or remote < 0 or local >= remote:
                self._start_drive(project, confirmed=True)
                return
            box = QMessageBox(self)
            box.setWindowTitle(_("shrink_title"))
            box.setText(_("shrink_title"))
            box.setInformativeText(
                _("shrink_body", local=human_size(local),
                  remote=human_size(remote), f=gdrive.ATTIC_FOLDER))
            upload = box.addButton(_("shrink_upload"),
                                   QMessageBox.DestructiveRole)
            cancel = box.addButton(_("cancel"), QMessageBox.RejectRole)
            box.setDefaultButton(cancel)
            box.exec()
            if box.clickedButton() is upload:
                self._start_drive(project, confirmed=True)

        self.window.begin_operation(_("op_drive"))
        run_async(self, measure, measured)

    def _begin_drive(self, project):
        from .. import gdrive
        self._set_drive_busy(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        from .. import gdrive as _gd
        self.window.begin_operation(_("op_drive"))
        self.window.toast(_("drive_mirror_note", f=_gd.ATTIC_FOLDER))

        def work():
            return gdrive.sync_project(project["path"], project["name"],
                                       progress=self.progress.setValue)

        def done(target, error):
            self.window.end_operation()
            self._set_drive_busy(True)
            self.progress.setVisible(False)
            self.window.toast(f"{_('drive_failed')}: {error}" if error
                              else _("drive_done", p=target))

        run_async(self, work, done)


# --------------------------------------------------------------------------- #
# Editor — Kate-like tabbed editor
# --------------------------------------------------------------------------- #
class EditorPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 8, 8, 0)
        new_btn = QPushButton(_("new_tab"))
        new_btn.clicked.connect(self.new_tab)
        bar.addWidget(new_btn)
        save_btn = QPushButton(_("save"))
        save_btn.setProperty("primary", True)
        save_btn.clicked.connect(self.save_current)
        bar.addWidget(save_btn)
        bar.addStretch(1)
        self.info = QLabel(_("no_file_hint"))
        self.info.setProperty("dim", True)
        bar.addWidget(self.info)
        layout.addLayout(bar)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        layout.addWidget(self.tabs, 1)

        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_current)
        QShortcut(QKeySequence("Ctrl+Q"), self,
                  activated=lambda: self._close_tab(self.tabs.currentIndex()))
        QShortcut(QKeySequence(Qt.Key_Escape), self,
                  activated=self._escape_leave)

    def _escape_leave(self):
        """Esc leaves the file and returns to the previous folder. Text gets
        a confirmation (configurable in Settings); media closes instantly."""
        edit = self.tabs.currentWidget()
        if not edit:
            return
        path = edit.property("path") or ""
        ext = os.path.splitext(path)[1].lower()
        is_media = ext in IMAGE_EXT or ext in AUDIO_EXT or ext in VIDEO_EXT
        if is_media or not config.get("esc_confirm"):
            self._close_tab(self.tabs.currentIndex())
            return
        class EscBox(QMessageBox):
            """QMessageBox that lets us see Esc/F1 before its own default
            'Escape closes me' behaviour swallows them."""
            choice = None

            def keyPressEvent(self, event):
                key = event.key()
                ctrl = bool(event.modifiers() & Qt.ControlModifier)
                if (ctrl and key == Qt.Key_S) or (
                        ctrl and key == Qt.Key_Escape):
                    self.choice = "save"
                    self.close()
                    return
                if key == Qt.Key_Escape:
                    self.choice = "leave"
                    self.close()
                    return
                super().keyPressEvent(event)

        box = EscBox(self)
        box.setWindowTitle(_("esc_question"))
        box.setText(_("esc_question"))
        box.setInformativeText(_("esc_detail") + "\n\n" + _("esc_hint_keys"))
        leave = box.addButton(_("esc_leave"), QMessageBox.AcceptRole)
        save = box.addButton(_("esc_save_leave"), QMessageBox.ActionRole)
        box.addButton(_("esc_stay"), QMessageBox.RejectRole)
        box.setDefaultButton(leave)
        box.exec()

        clicked = box.clickedButton()
        if box.choice == "save" or clicked is save:
            self._save_then_leave()
        elif box.choice == "leave" or clicked is leave:
            self._close_tab(self.tabs.currentIndex())

    def _save_then_leave(self):
        from PySide6.QtCore import QTimer
        edit = self.tabs.currentWidget()
        if edit:
            path = edit.property("path")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(edit.toPlainText())
            except OSError as e:
                self.window.toast(str(e))
                return
            self.window.show_save_animation(os.path.basename(path))
        QTimer.singleShot(120,
                          lambda: self._close_tab(self.tabs.currentIndex()))

    def open_file(self, path):
        path = os.path.realpath(path)
        ext = os.path.splitext(path)[1].lower()
        # Media and other binaries belong to the system viewer, not a text box.
        if ext in IMAGE_EXT | AUDIO_EXT | VIDEO_EXT:
            subprocess.Popen(["xdg-open", path])
            return
        for i in range(self.tabs.count()):
            if self.tabs.widget(i).property("path") == path:
                self.tabs.setCurrentIndex(i)
                return
        edit = QPlainTextEdit()
        edit.setProperty("path", path)
        font = QFont("Ubuntu Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(11)
        edit.setFont(font)
        try:
            with open(path, "r", encoding="utf-8") as f:
                edit.setPlainText(f.read())
        except UnicodeDecodeError:
            subprocess.Popen(["xdg-open", path])
            return
        except OSError as e:
            edit.setPlainText(f"# {e}")
        idx = self.tabs.addTab(edit, os.path.basename(path))
        self.tabs.setCurrentIndex(idx)

    def save_current(self):
        edit = self.tabs.currentWidget()
        if not edit:
            return
        path = edit.property("path")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(edit.toPlainText())
        except OSError as e:
            self.window.toast(str(e))
            return
        self.window.toast(_("saved", f=os.path.basename(path)))
        self.window.prompt_deploy(path)

    def new_tab(self):
        for i in range(self.tabs.count()):
            edit = self.tabs.widget(i)
            path = edit.property("path")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(edit.toPlainText())
            except OSError:
                pass
        project = config.active_project()
        folder = project["path"] if project else os.path.expanduser("~")
        base, i = _("untitled"), 1
        name = f"{base}.txt"
        while os.path.exists(os.path.join(folder, name)):
            name = f"{base}-{i}.txt"
            i += 1
        path = os.path.join(folder, name)
        open(path, "w").close()
        self.open_file(path)

    def _close_tab(self, index):
        if index < 0:
            return
        self.tabs.removeTab(index)
        if self.tabs.count() == 0:
            self.window.select_page("project")


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
class SnapshotsPage(QWidget):
    KIND_KEYS = {"manual": "manual", "auto": "auto",
                 "safety": "safety", "apply": "before_apply"}

    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        bar = QHBoxLayout()
        create = QPushButton(_("create_snapshot"))
        create.setProperty("primary", True)
        create.clicked.connect(self._create)
        bar.addWidget(create)
        restore = QPushButton(_("restore"))
        restore.clicked.connect(self._restore)
        bar.addWidget(restore)
        delete = QPushButton(_("delete"))
        delete.setProperty("danger", True)
        delete.clicked.connect(self._delete)
        bar.addWidget(delete)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.refresh()

    def _manager(self):
        project = config.active_project()
        return SnapshotManager(project["path"]) if project else None

    def refresh(self):
        self.list.clear()
        mgr = self._manager()
        if not mgr:
            return
        snaps = mgr.list()
        if not snaps:
            self.list.addItem(_("no_snapshots"))
            return
        for snap in snaps:
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(snap["time"]))
            kind = _(self.KIND_KEYS.get(snap["kind"], "manual"))
            item = QListWidgetItem(
                f'{when}    {kind}    {human_size(snap["size"])}')
            item.setData(Qt.UserRole, snap["name"])
            self.list.addItem(item)

    def _selected(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _create(self):
        mgr = self._manager()
        if mgr:
            run_async(self, lambda: mgr.create(kind="manual"),
                      lambda r, e: (self.window.toast(
                          str(e) if e else _("snapshot_created")),
                          self.refresh()))

    def _restore(self):
        name = self._selected()
        mgr = self._manager()
        if not name or not mgr:
            return
        if QMessageBox.question(self, _("restore_question"),
                                _("restore_detail")) != QMessageBox.Yes:
            return
        run_async(self, lambda: mgr.restore(name),
                  lambda r, e: (self.window.toast(
                      str(e) if e else _("restored")), self.refresh()))

    def _delete(self):
        name = self._selected()
        mgr = self._manager()
        if name and mgr:
            mgr.delete(name)
            self.refresh()


# --------------------------------------------------------------------------- #
# Terminal (Konsole-style, using QProcess + a simple view)
# --------------------------------------------------------------------------- #
class TerminalPage(QWidget):
    def __init__(self, window, argv=None, cwd=None):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        font = QFont("Ubuntu Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(11)
        self.view.setFont(font)
        self.view.setStyleSheet(
            "background:#1b1e20; color:#fcfcfc; border:none;")
        layout.addWidget(self.view, 1)

        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("$")
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        layout.addLayout(row)

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyRead.connect(self._read)
        project = config.active_project()
        self.proc.setWorkingDirectory(
            cwd or (project["path"] if project else os.path.expanduser("~")))
        self.proc.start(*(argv or [os.environ.get("SHELL", "/bin/bash"),
                                   ["-i"]]))

    def _read(self):
        from PySide6.QtGui import QTextCursor
        data = bytes(self.proc.readAll()).decode("utf-8", "replace")
        # Anything watching this terminal (the sandbox network watcher) gets
        # the same text the user sees.
        hook = getattr(self, "output_received", None)
        if hook is not None:
            try:
                hook(data)
            except Exception:
                pass
        self.view.moveCursor(QTextCursor.End)
        self.view.insertPlainText(data)
        self.view.ensureCursorVisible()

    def _send(self):
        text = self.input.text()
        self.input.clear()
        self.view.appendPlainText(f"$ {text}")
        self.proc.write((text + "\n").encode("utf-8"))

    def shell_pid(self):
        return int(self.proc.processId() or 0)

    def stop(self):
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(2000)

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# Sandbox — isolated session
# --------------------------------------------------------------------------- #
class SandboxPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.session = None
        self.terminal = None

        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(12, 12, 12, 12)

        self.idle = QWidget()
        idle_layout = QVBoxLayout(self.idle)
        idle_layout.addStretch(1)
        title = QLabel(_("sandbox"))
        title.setProperty("title", True)
        title.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(title)
        desc = QLabel(_("sandbox_simple"))
        desc.setWordWrap(True)
        desc.setProperty("dim", True)
        desc.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(desc)
        btns = QHBoxLayout()
        btns.addStretch(1)
        b1 = QPushButton(_("start_sandbox"))
        b1.setProperty("primary", True)
        b1.clicked.connect(lambda: self._start(False))
        btns.addWidget(b1)
        b2 = QPushButton(_("with_project"))
        b2.clicked.connect(lambda: self._start(True))
        btns.addWidget(b2)
        btns.addStretch(1)
        idle_layout.addLayout(btns)
        from ..sandbox import isolation_available
        state = QLabel(_("isolated_on") if isolation_available()
                       else _("isolated_off"))
        state.setAlignment(Qt.AlignCenter)
        state.setWordWrap(True)
        idle_layout.addWidget(state)
        idle_layout.addStretch(1)
        self.layout_.addWidget(self.idle)

        self.running = QWidget()
        run_layout = QVBoxLayout(self.running)
        run_layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.loc = QLabel()
        self.loc.setProperty("dim", True)
        top.addWidget(self.loc, 1)
        end = QPushButton(_("end_sim"))
        end.setProperty("danger", True)
        end.clicked.connect(self._end)
        top.addWidget(end)
        run_layout.addLayout(top)
        self.term_holder = QVBoxLayout()
        run_layout.addLayout(self.term_holder, 1)
        self.running.setVisible(False)
        self.layout_.addWidget(self.running, 1)

    def _start(self, with_project, network=None):
        from ..sandbox import SandboxSession
        session = SandboxSession(network=network)
        self._with_project = with_project
        if with_project:
            project = config.active_project()
            if not project:
                self.window.toast(_("no_project_title"))
                session.destroy()
                return
            try:
                session.copy_project(project["path"])
            except Exception as e:  # noqa: BLE001
                session.destroy()
                self.window.toast(str(e))
                return
        self.session = session
        net = _("net_on_badge") if session.network else _("net_off_badge")
        self.loc.setText(
            f'{_("isolated_on") if session.isolated else _("isolated_off")}'
            f'   ·   {net}   ·   {session.dir}')
        from ..netwatch import OutputWatcher
        self._watcher = (OutputWatcher(self._on_network_wanted)
                         if not session.network else None)
        self.terminal = TerminalPage(self.window,
                                     argv=[session.shell_argv()[0],
                                           session.shell_argv()[1:]],
                                     cwd=session.work)
        self.term_holder.addWidget(self.terminal)
        if self._watcher is not None:
            self.terminal.output_received = self._scan_output
        session.shell_pid = self.terminal.shell_pid()
        self.idle.setVisible(False)
        self.running.setVisible(True)

    def _scan_output(self, chunk):
        if self._watcher is None:
            return
        if not config.get("sandbox_network_prompt"):
            return
        self._watcher.feed(chunk)

    def _on_network_wanted(self):
        box = QMessageBox(self)
        box.setWindowTitle(_("sandbox_net_asked"))
        box.setText(_("sandbox_net_asked"))
        box.setInformativeText(_("sandbox_net_body"))
        allow = box.addButton(_("net_allow"), QMessageBox.DestructiveRole)
        deny = box.addButton(_("net_deny"), QMessageBox.RejectRole)
        never = box.addButton(_("net_never_ask"), QMessageBox.ActionRole)
        box.setDefaultButton(deny)
        box.exec()
        clicked = box.clickedButton()
        if clicked is never:
            config.set("sandbox_network_prompt", False)
        elif clicked is allow:
            self._confirm_network()

    def _confirm_network(self):
        confirm = QMessageBox(self)
        confirm.setWindowTitle(_("net_confirm_q"))
        confirm.setText(_("net_confirm_q"))
        confirm.setInformativeText(_("net_confirm_body"))
        yes = confirm.addButton(_("net_confirm_yes"),
                                QMessageBox.DestructiveRole)
        cancel = confirm.addButton(_("cancel"), QMessageBox.RejectRole)
        confirm.setDefaultButton(cancel)
        confirm.exec()
        if confirm.clickedButton() is not yes:
            return
        with_project = getattr(self, "_with_project", False)
        if self.terminal:
            self.terminal.stop()
            self.terminal.setParent(None)
            self.terminal = None
        if self.session:
            self.session.destroy()
            self.session = None
        self._start(with_project, network=True)

    def _end(self):
        if QMessageBox.question(self, _("end_sim_question"),
                                _("end_sim_detail")) != QMessageBox.Yes:
            return
        if self.terminal:
            self.terminal.stop()
            self.terminal.setParent(None)
            self.terminal = None
        if self.session:
            self.session.destroy()
            self.session = None
        self.running.setVisible(False)
        self.idle.setVisible(True)
        self.window.toast(_("sim_ended"))

    def end_all(self):
        if self.session:
            self.session.destroy()
            self.session = None


# --------------------------------------------------------------------------- #
# Package
# --------------------------------------------------------------------------- #
class PackagePage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        form_box = QGroupBox(_("package"))
        form = QFormLayout(form_box)
        self.version = QLineEdit("1.0.0")
        form.addRow(_("version"), self.version)
        user = os.environ.get("USER", "developer")
        self.maintainer = QLineEdit(f"{user} <{user}@localhost>")
        form.addRow(_("maintainer"), self.maintainer)
        self.description = QLineEdit()
        form.addRow(_("description_f"), self.description)
        from ..debbuild import LICENSE_CHOICES
        self._license_ids = [spdx for spdx, _d in LICENSE_CHOICES]
        self.license = QComboBox()
        self.license.addItems([desc for _s, desc in LICENSE_CHOICES]
                              + [_("license_other")])
        self.license.setToolTip(_("license_hint"))
        self.license.currentIndexChanged.connect(self._on_license_choice)
        form.addRow(_("license_f"), self.license)

        self.license_custom = QLineEdit()
        self.license_custom.setPlaceholderText(_("license_custom"))
        self.license_custom.setVisible(False)
        form.addRow("", self.license_custom)

        learn = QPushButton(_("license_learn"))
        learn.setToolTip(debbuild.LICENSE_GUIDE_URL)
        learn.clicked.connect(lambda: __import__("webbrowser").open(
            debbuild.LICENSE_GUIDE_URL))
        form.addRow("", learn)
        layout.addWidget(form_box)

        gpg_box = QGroupBox(_("gpg_title"))
        gpg_layout = QVBoxLayout(gpg_box)
        hint = QLabel(_("gpg_explain"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        gpg_layout.addWidget(hint)
        self.sign_check = QCheckBox(_("sign_packages"))
        self.sign_check.setChecked(bool(config.get("sign_packages")))
        self.sign_check.stateChanged.connect(
            lambda s: config.set("sign_packages", bool(s)))
        gpg_layout.addWidget(self.sign_check)
        layout.addWidget(gpg_box)

        btns = QHBoxLayout()
        deb = QPushButton(_("export_deb"))
        deb.setProperty("primary", True)
        deb.clicked.connect(lambda: self._export("deb"))
        btns.addWidget(deb)
        rpm = QPushButton(_("export_rpm"))
        rpm.clicked.connect(lambda: self._export("rpm"))
        btns.addWidget(rpm)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("dim", True)
        layout.addWidget(self.status)
        layout.addStretch(1)

    def _on_license_choice(self, index):
        self.license_custom.setVisible(index >= len(self._license_ids))

    def _chosen_license(self):
        index = self.license.currentIndex()
        if index < len(self._license_ids):
            return self._license_ids[index]
        return (self.license_custom.text().strip()
                or "LicenseRef-proprietary")

    def _export(self, fmt):
        project = config.active_project()
        if not project:
            return
        if QMessageBox.question(
                self, _("export_question"),
                _("export_detail",
                  n=project["name"].lower())) != QMessageBox.Yes:
            return
        out_dir = QFileDialog.getExistingDirectory(self, _("export_deb"))
        if not out_dir:
            return
        self.status.setText(_("building"))
        version = self.version.text()
        maint = self.maintainer.text()
        desc = self.description.text()
        lic = self._chosen_license()

        def work():
            builder = (debbuild.build_deb if fmt == "deb"
                       else debbuild.build_rpm)
            path = builder(project["path"], out_dir, project["name"],
                           version, maint, desc, lic,
                           debbuild.repo_homepage(project.get("repo_url")))
            signed = None
            if config.get("sign_packages"):
                key = gpgsign.signing_key(config.get("gpg_key"))
                if key:
                    try:
                        signed = (gpgsign.sign_rpm(path, key[1])
                                  if fmt == "rpm"
                                  else gpgsign.sign_deb(path, key[0]))
                    except gpgsign.GpgError as e:
                        signed = f"!{e}"
            return path, signed

        def done(res, error):
            if error:
                self.status.setText(f"{_('export_failed')}: {error}")
                return
            path, signed = res
            text = _("export_done", p=path)
            left_out = debbuild.find_secrets(project["path"])
            if left_out:
                text += "\n" + _("secrets_left_out", n=len(left_out))
            if signed and not str(signed).startswith("!"):
                text += "\n" + _("signed_ok", p=os.path.basename(str(signed)))
            self.status.setText(text)

        run_async(self, work, done)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
class KeysPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        hint = QLabel(_("keys_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        layout.addWidget(hint)

        bar = QHBoxLayout()
        create = QPushButton(_("create_key"))
        create.setProperty("primary", True)
        create.clicked.connect(self._create)
        bar.addWidget(create)
        sync_keys = QPushButton(_("sync_keys_only"))
        sync_keys.setToolTip(_("sync_only_hint"))
        sync_keys.clicked.connect(self._sync_keys)
        bar.addWidget(sync_keys)
        self._sync_keys_btn = sync_keys
        use = QPushButton(_("use_key"))
        use.clicked.connect(self._use)
        bar.addWidget(use)
        export = QPushButton(_("export_pubkey"))
        export.clicked.connect(self._export)
        bar.addWidget(export)
        delete = QPushButton(_("delete"))
        delete.setProperty("danger", True)
        delete.clicked.connect(self._delete)
        bar.addWidget(delete)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.refresh()

    def refresh(self):
        self.list.clear()
        run_async(self, gpgsign.list_keys_detailed, self._render)

    def _render(self, keys, error):
        if error or not keys:
            self.list.addItem(_("no_key"))
            return
        active = gpgsign.signing_key(config.get("gpg_key"))
        active_fpr = active[0] if active else ""
        for k in keys:
            created = time.strftime("%Y-%m-%d", time.localtime(k["created"])) \
                if k["created"] else "—"
            expires = time.strftime("%Y-%m-%d", time.localtime(k["expires"])) \
                if k["expires"] else _("exp_never")
            algo = k["algo"] if k["algo"] == "Ed25519" \
                else f'{k["algo"]} {k["bits"]}'
            mark = "★ " if k["fpr"] == active_fpr else "   "
            item = QListWidgetItem(
                f'{mark}{k["uid"]}\n     {algo} · {_("created_on")}: {created}'
                f' · {_("expires_on")}: {expires}')
            item.setData(Qt.UserRole, k["fpr"])
            self.list.addItem(item)

    def _selected(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _sync_keys(self):
        """Upload just the public keys — project files are left alone."""
        from .. import gdrive
        project = config.active_project()
        if not project:
            return
        if not (gdrive.available() and gdrive.connected()):
            self.window.toast(_("not_logged_in"))
            return
        self._sync_keys_btn.setEnabled(False)
        self.window.begin_operation(_("op_keys"))

        def done(count, error):
            self.window.end_operation()
            self._sync_keys_btn.setEnabled(True)
            self.window.toast(f"{_('drive_failed')}: {error}" if error
                              else _("sync_keys_done", n=count or 0))

        run_async(self, lambda: gdrive.push_public_keys(project["name"]), done)

    def _create(self):
        from .dialogs import GpgKeyDialog
        dlg = GpgKeyDialog(self)
        if dlg.exec():
            self.window.toast(_("key_created"))
            self.refresh()

    def _use(self):
        fpr = self._selected()
        if fpr:
            config.set("gpg_key", fpr)
            self.refresh()

    def _export(self):
        fpr = self._selected()
        if not fpr:
            return
        path, _sel = QFileDialog.getSaveFileName(self, _("export_pubkey"),
                                                 "public-key.asc")
        if path:
            run_async(self, lambda: gpgsign.export_public(fpr, path),
                      lambda p, e: self.window.toast(
                          str(e) if e else _("pubkey_done", p=p)))

    def _delete(self):
        fpr = self._selected()
        if not fpr:
            return
        if QMessageBox.question(self, _("delete_key_q"),
                                _("delete_key_detail")) != QMessageBox.Yes:
            return
        run_async(self, lambda: gpgsign.delete_key(fpr),
                  lambda r, e: (self.window.toast(
                      str(e) if e else _("key_deleted")), self.refresh()))


# --------------------------------------------------------------------------- #
# Next Updates
# --------------------------------------------------------------------------- #
PRIORITIES = [("high", "prio_high"), ("normal", "prio_normal"),
              ("low", "prio_low")]


class UpdatesPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        hint = QLabel(_("updates_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        layout.addWidget(hint)

        from .. import docs as _docs
        gb = round(_docs.total_ram_gb(), 1)
        docs_hint = QLabel(_("docs_in_ram", gb=gb) if _docs.prefer_ram()
                           else _("docs_on_disk", gb=gb))
        docs_hint.setProperty("dim", True)
        docs_hint.setWordWrap(True)
        layout.addWidget(docs_hint)

        project = config.active_project()
        if project and _docs.prefer_ram():
            run_async(self, lambda: _docs.preload(project["path"]),
                      lambda r, e: None)

        bar = QHBoxLayout()
        add = QPushButton(_("add_update"))
        add.setProperty("primary", True)
        add.clicked.connect(self._add)
        bar.addWidget(add)
        sync_plans = QPushButton(_("sync_plans_only"))
        sync_plans.setToolTip(_("sync_only_hint"))
        sync_plans.clicked.connect(self._sync_plans)
        bar.addWidget(sync_plans)
        self._sync_plans_btn = sync_plans
        edit = QPushButton(_("edit_update"))
        edit.clicked.connect(self._edit)
        bar.addWidget(edit)
        attach = QPushButton(_("attach_doc"))
        attach.clicked.connect(self._attach_doc)
        bar.addWidget(attach)
        open_doc = QPushButton(_("open_doc"))
        open_doc.clicked.connect(self._open_doc)
        bar.addWidget(open_doc)
        done = QPushButton(_("mark_done"))
        done.clicked.connect(self._toggle)
        bar.addWidget(done)
        delete = QPushButton(_("delete"))
        delete.setProperty("danger", True)
        delete.clicked.connect(self._delete)
        bar.addWidget(delete)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._copy_details)
        layout.addWidget(self.list, 1)
        self.refresh()

    def _copy_details(self, item):
        """Double-clicking a plan copies its details to the clipboard."""
        from PySide6.QtWidgets import QApplication
        created = item.data(Qt.UserRole)
        if created is None:
            return
        title, note = "", ""
        for entry in self._load():
            if entry.get("created") == created:
                title = (entry.get("title") or "").strip()
                note = (entry.get("detail") or "").strip()
                break
        if not title and not note:
            self.window.toast(_("nothing_to_copy"))
            return
        text = f"{title}: {note}" if note else title
        QApplication.clipboard().setText(text)
        self.window.toast(_("details_copied"))

    def _load(self):
        from .. import plans
        project = config.active_project()
        return plans.load(project["path"]) if project else []

    def _save(self, items):
        from .. import plans
        project = config.active_project()
        if project:
            plans.save(project["path"], items)

    def refresh(self):
        self.list.clear()
        items = self._load()
        if not items:
            self.list.addItem(_("no_updates"))
            return
        from .. import plans as _plans
        items.sort(key=_plans.sort_key)
        for item in items:
            prio = _(dict(PRIORITIES).get(
                _plans.CODE_TO_PRIORITY.get(item.get("priority", 2),
                                            "normal"), "prio_normal"))
            state = _("done_label") if item.get("done") else _("planned_label")
            from .. import docs as _d
            project = config.active_project()
            attached = _d.list_docs(project["path"], item.get("created")) \
                if project else []
            row = QListWidgetItem(
                f'{"✓" if item.get("done") else "○"}  {item.get("title")}'
                f'\n     {state} · {prio}'
                + (f' · {item["detail"]}' if item.get("detail") else "")
                + (f'  📎 {_("docs_count", n=len(attached))}'
                   if attached else ""))
            row.setData(Qt.UserRole, item.get("created"))
            self.list.addItem(row)

    def _sync_plans(self):
        """Two-way sync of just the plans — project files are left alone."""
        from .. import gdrive, plans
        project = config.active_project()
        if not project:
            return
        if not (gdrive.available() and gdrive.connected()):
            self.window.toast(_("not_logged_in"))
            return
        self._sync_plans_btn.setEnabled(False)
        self.window.begin_operation(_("op_plans"))

        def done(count, error):
            self.window.end_operation()
            self._sync_plans_btn.setEnabled(True)
            if error:
                self.window.toast(f"{_('drive_failed')}: {error}")
            else:
                self.window.toast(_("sync_plans_done", n=count or 0))
                self.refresh()

        run_async(self, lambda: plans.sync_with_drive(project["path"],
                                                     project["name"]), done)

    def _add(self):
        self._plan_dialog(None)

    def _edit(self):
        created = self._current_created()
        if created is None:
            return
        for entry in self._load():
            if entry.get("created") == created:
                self._plan_dialog(entry)
                return

    def _plan_dialog(self, item):
        """One form for adding and editing: title, details and priority."""
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                                       QVBoxLayout)
        if not config.active_project():
            self.window.toast(_("no_project_title"))
            return
        editing = item is not None

        dlg = QDialog(self)
        dlg.setWindowTitle(_("edit_update") if editing else _("add_update"))
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)
        form = QFormLayout()

        title_edit = QLineEdit(item.get("title", "") if editing else "")
        title_edit.setPlaceholderText(_("update_title"))
        form.addRow(_("update_title"), title_edit)

        note_edit = QLineEdit(item.get("detail", "") if editing else "")
        note_edit.setPlaceholderText(_("update_note"))
        form.addRow(_("update_note"), note_edit)

        prio_box = QComboBox()
        for _c, key in PRIORITIES:
            prio_box.addItem(_(key))
        from .. import plans as _plans
        codes = [c for c, _k in PRIORITIES]
        current = _plans.CODE_TO_PRIORITY.get(
            item.get("priority", 2), "normal") if editing else "normal"
        prio_box.setCurrentIndex(codes.index(current)
                                 if current in codes else 1)
        form.addRow(_("priority"), prio_box)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok |
                                   QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(
            _("save") if editing else _("add_update"))
        buttons.button(QDialogButtonBox.Ok).setProperty("primary", True)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if not dlg.exec():
            return
        title = title_edit.text().strip()
        if not title:
            return
        note = note_edit.text().strip()
        priority = _plans.PRIORITY_TO_CODE[
            PRIORITIES[prio_box.currentIndex()][0]]

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

    def _current_created(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _toggle(self):
        created = self._current_created()
        if created is None:
            return
        items = self._load()
        for i in items:
            if i.get("created") == created:
                i["done"] = not i.get("done", False)
        self._save(items)
        self.refresh()

    def _attach_doc(self):
        from .. import docs as _d
        created = self._current_created()
        project = config.active_project()
        if created is None or not project:
            return
        path, _sel = QFileDialog.getOpenFileName(self, _("attach_doc"))
        if not path:
            return
        try:
            meta = _d.attach(project["path"], created, path)
        except OSError as e:
            self.window.toast(str(e))
            return
        self.window.toast(_("doc_attached", f=meta["name"]))
        self.refresh()

    def _open_doc(self):
        from .. import docs as _d
        created = self._current_created()
        project = config.active_project()
        if created is None or not project:
            return
        attached = _d.list_docs(project["path"], created)
        if not attached:
            self.window.toast(_("no_docs"))
            return
        doc = attached[0]
        if len(attached) > 1:
            names = [d["name"] for d in attached]
            choice, ok = QInputDialog.getItem(self, _("open_doc"),
                                              _("open_doc"), names, 0, False)
            if not ok:
                return
            doc = attached[names.index(choice)]
        subprocess.Popen(["xdg-open", _d.resolve_for_open(doc["path"])])

    def _delete(self):
        from .. import docs as _d
        created = self._current_created()
        if created is None:
            return
        project = config.active_project()
        if project:
            _d.remove_all(project["path"], created)
        self._save([i for i in self._load() if i.get("created") != created])
        self.refresh()


# --------------------------------------------------------------------------- #
# Overview — the project at a glance, on a soft dark gradient
# --------------------------------------------------------------------------- #
OS_ICONS = {"Linux": "os-linux", "Windows": "os-windows",
            "Web": "os-web",
            "macOS": "os-apple", "Cross-platform": "os-cross"}


def _asset_pixmap(name, size=24):
    """A vector icon from brand-icons, rendered at the requested size."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "brand-icons", name + ".svg")
    if os.path.isfile(path):
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            return pixmap.scaled(size, size, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
    return None


class OverviewPage(QWidget):
    """Project identity, what it is made of, and the shareable card."""

    GRADIENT = ("background: qradialgradient(cx:0.5, cy:1, radius:1.9, "
                "fx:0.5, fy:1, stop:0 #3d3d3d, stop:0.30 #303030, "
                "stop:0.62 #232323, stop:1 #1a1a1a);")
    # Scoped with an object name: a bare stylesheet would be inherited by
    # every child label, drawing a box around each number.
    CARD = ("QWidget#petaCard { background: rgba(255,255,255,0.045);"
            "border: 1px solid rgba(255,255,255,0.08);"
            "border-radius: 14px; }"
            "QWidget#petaCard QLabel { background: transparent;"
            "border: none; }")
    CHIP = ("QWidget#petaChip { background: rgba(255,255,255,0.06);"
            "border: 1px solid rgba(255,255,255,0.10);"
            "border-radius: 10px; }"
            "QWidget#petaChip QLabel { background: transparent;"
            "border: none; }")

    def __init__(self, window):
        super().__init__()
        self.window = window
        self._data = None
        self.setStyleSheet(f"QWidget#overviewRoot {{ {self.GRADIENT} }}")
        self.setObjectName("overviewRoot")
        self.setAutoFillBackground(True)

        from PySide6.QtWidgets import QScrollArea
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        scroll.setStyleSheet(f"QScrollArea {{ {self.GRADIENT} border:none; }}")
        outer.addWidget(scroll)

        holder = QWidget()
        holder.setStyleSheet(f"{self.GRADIENT}")
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(40, 44, 40, 44)
        self.body.setSpacing(24)
        self.body.setAlignment(Qt.AlignTop)
        scroll.setWidget(holder)

        self.refresh()

    # -- helpers ----------------------------------------------------------------
    def _clear(self):
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._drop_layout(item.layout())

    def _drop_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._drop_layout(item.layout())

    def _card(self, icon_name, value, label):
        card = QWidget()
        card.setObjectName("petaCard")
        card.setStyleSheet(self.CARD)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        top = QHBoxLayout()
        pixmap = _asset_pixmap(icon_name, 20)
        if pixmap:
            icon = QLabel()
            icon.setPixmap(pixmap)
            top.addWidget(icon)
        number = QLabel(value)
        number.setStyleSheet("color:#ffffff; font-size:22pt; font-weight:700;")
        top.addWidget(number, 1)
        layout.addLayout(top)
        caption = QLabel(label)
        caption.setStyleSheet("color:#9a9a9a; font-size:9pt;")
        layout.addWidget(caption)
        return card

    # -- build -------------------------------------------------------------------
    def refresh(self):
        from .. import stats
        project = config.active_project()
        self._clear()
        if not project:
            return
        waiting = QLabel(_("analysing"))
        waiting.setStyleSheet("color:#b9b9b9; font-size:12pt;")
        waiting.setAlignment(Qt.AlignCenter)
        self.body.addWidget(waiting)
        run_async(self, lambda: stats.analyse(project["path"]),
                  lambda data, error: self._render(project, data, error))

    def _render(self, project, data, error):
        from .. import sharecard, stats
        self._clear()
        if error or not data:
            label = QLabel(str(error or ""))
            label.setStyleSheet("color:#b9b9b9;")
            self.body.addWidget(label)
            return
        self._data = data

        # hero
        logo_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "data", "icons", "128x128", "io.petacore.Petacore.png")
        if os.path.isfile(logo_path):
            logo = QLabel()
            logo.setPixmap(QPixmap(logo_path).scaled(
                72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            logo.setAlignment(Qt.AlignCenter)
            self.body.addWidget(logo)

        title = QLabel(project["name"])
        title.setStyleSheet("color:#ffffff; font-size:30pt; font-weight:700;")
        title.setAlignment(Qt.AlignCenter)
        self.body.addWidget(title)

        subtitle = QLabel(f'{_("stat_platform")}: {data["platform"]}')
        subtitle.setStyleSheet("color:#b9b9b9; font-size:12pt;")
        subtitle.setAlignment(Qt.AlignCenter)
        self.body.addWidget(subtitle)

        # one chip per target system
        chips = QHBoxLayout()
        chips.setSpacing(12)
        chips.addStretch(1)
        for name in [part.strip() for part in data["platform"].split("/")]:
            chip = QWidget()
            chip.setObjectName("petaChip")
            chip.setStyleSheet(self.CHIP)
            row = QHBoxLayout(chip)
            row.setContentsMargins(14, 9, 16, 9)
            row.setSpacing(10)
            pixmap = _asset_pixmap(OS_ICONS.get(name, "os-cross"), 22)
            if pixmap:
                icon = QLabel()
                icon.setPixmap(pixmap)
                row.addWidget(icon)
            text = QLabel(name)
            text.setStyleSheet("color:#ffffff; font-size:11pt;")
            row.addWidget(text)
            chips.addWidget(chip)
        chips.addStretch(1)
        self.body.addLayout(chips)

        # stat cards
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout()
        grid.setSpacing(14)
        cards = [
            ("stat-lines", f'{data["lines"]:,}'.replace(",", " "),
             _("stat_lines")),
            ("stat-chars", stats.human_count(data["characters"]),
             _("stat_chars")),
            ("stat-files", str(data["files"]), _("stat_files")),
        ]
        for index, (icon, value, label) in enumerate(cards):
            grid.addWidget(self._card(icon, value, label),
                           index // 3, index % 3)
        self.body.addLayout(grid)

        # language mix
        mix = QWidget()
        mix.setObjectName("petaCard")
        mix.setStyleSheet(self.CARD)
        mix_layout = QVBoxLayout(mix)
        mix_layout.setContentsMargins(16, 14, 16, 16)
        mix_layout.setSpacing(10)
        heading = QLabel(_("language_mix").upper())
        heading.setStyleSheet("color:#9a9a9a; font-size:9pt;"
                              "letter-spacing:1px;")
        mix_layout.addWidget(heading)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        for index, item in enumerate(data["languages"][:8]):
            segment = QLabel()
            segment.setFixedHeight(12)
            colour = sharecard.colour_for(item["language"], index)
            segment.setStyleSheet(
                f"background:{colour}; border-radius:6px;")
            bar.addWidget(segment, max(1, int(item["percent"] * 10)))
        mix_layout.addLayout(bar)

        legend = QGridLayout()
        legend.setHorizontalSpacing(18)
        legend.setVerticalSpacing(6)
        for index, item in enumerate(data["languages"][:8]):
            row = QHBoxLayout()
            dot = QLabel()
            dot.setFixedSize(12, 12)
            colour = sharecard.colour_for(item["language"], index)
            dot.setStyleSheet(f"background:{colour}; border-radius:6px;")
            row.addWidget(dot)
            text = QLabel(f'{item["language"]}  {item["percent"]}%')
            text.setStyleSheet("color:#c9c9c9; font-size:10pt;")
            row.addWidget(text, 1)
            holder = QWidget()
            holder.setLayout(row)
            legend.addWidget(holder, index // 3, index % 3)
        mix_layout.addLayout(legend)
        self.body.addWidget(mix)

        # share
        hint = QLabel(_("share_hint"))
        hint.setStyleSheet("color:#b9b9b9; font-size:11pt;")
        hint.setAlignment(Qt.AlignCenter)
        self.body.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for shape, key, primary in (("story", "shape_story", True),
                                    ("square", "shape_square", False),
                                    ("wide", "shape_wide", False)):
            button = QPushButton(_(key))
            if primary:
                button.setProperty("primary", True)
            button.clicked.connect(lambda _c=False, s=shape: self._share(s))
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.body.addLayout(buttons)

    # -- export --------------------------------------------------------------------
    def _share(self, shape):
        from .. import sharecard
        project = config.active_project()
        if not project or not self._data:
            return
        path, _sel = QFileDialog.getSaveFileName(
            self, _("share_card"), f'{project["name"]}-{shape}.png',
            "PNG (*.png)")
        if not path:
            return
        icon = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "data", "icons", "256x256", "io.petacore.Petacore.png")

        def work():
            return sharecard.write_png(
                path, project["name"], self._data, shape,
                logo_href=icon if os.path.isfile(icon) else None)

        def done(result, error):
            if isinstance(error, sharecard.PngUnavailable):
                self.window.toast(_("png_unavailable"))
            elif error:
                self.window.toast(str(error))
            else:
                self.window.toast(
                    _("card_saved", p=os.path.basename(result)))

        run_async(self, work, done)
