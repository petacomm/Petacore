"""Qt (KDE Plasma) pages for Petacore.

The backend modules are shared with the GNOME build — only the presentation
layer differs, so both versions behave identically.
"""

import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QProcess, Qt, QThread, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QKeySequence, QPainter,
                           QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDialog, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QFrame, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea,
                               QSpinBox, QSplitter, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

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
from .. import versions as _versions  # noqa: E402
# Larger files open in the system editor instead: the built-in editor is for
# changing a file and moving on, not for holding a database in memory.
MAX_EDIT_SIZE = 2 * 1024 * 1024

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
        else:
            try:
                for e in os.scandir(self.current):
                    if e.name in HIDDEN or e.name.startswith(".") \
                        or _versions.hidden(self.current, e.name):
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
# Live Server — the site itself, served locally and shown in the window
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEBENGINE = True
except ImportError:
    QWebEngineView = None
    HAVE_WEBENGINE = False


FS_ANIM_MS = 340


def _slide_in_from_right(widget, owner, duration=320):
    """Bring a widget in from the right edge of its parent.

    Qt has no equivalent of GTK's stack transition, so the geometry is
    animated by hand. It has to start after the layout has placed the
    widget — animating before that fights the layout and lands in the
    wrong place — hence the single-shot timer.
    """
    from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer

    def run():
        end = widget.geometry()
        if end.width() <= 0:
            return
        start = end.translated(end.width(), 0)
        animation = QPropertyAnimation(widget, b"geometry", owner)
        animation.setDuration(duration)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        owner._slide_anim = animation      # keep it alive until it finishes
        animation.start()

    QTimer.singleShot(0, run)


class LiveServerPage(QWidget):
    """The web equivalent of SandboxPage.

    The point of this page is the site, not the server, so the site is what
    it shows: a browser view filling the page, with a thin bar above it.
    F11 fills the window with the site; Escape brings the rest back.
    """

    def __init__(self, window):
        super().__init__()
        from .. import liveserve, stats
        self.liveserve = liveserve
        self.stats = stats
        self.window = window
        self.proc = None
        self.port = 0
        self.url = ""
        self.fullscreen = False
        self.view = None
        self.fs_window = None
        self._fs_closing = False

        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(12, 12, 12, 12)

        # -- idle ---------------------------------------------------------------
        self.idle = QWidget()
        idle_layout = QVBoxLayout(self.idle)
        idle_layout.addStretch(1)
        title = QLabel(_("live_server"))
        title.setProperty("title", True)
        title.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(title)
        desc = QLabel(_("live_server_hint"))
        desc.setWordWrap(True)
        desc.setProperty("dim", True)
        desc.setAlignment(Qt.AlignCenter)
        idle_layout.addWidget(desc)

        self.lan_check = QCheckBox(_("live_server_lan"))
        self.lan_check.setToolTip(_("live_server_lan_hint"))
        lan_row = QHBoxLayout()
        lan_row.addStretch(1)
        lan_row.addWidget(self.lan_check)
        lan_row.addStretch(1)
        idle_layout.addLayout(lan_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        start_btn = QPushButton(_("start_live_server"))
        start_btn.setProperty("primary", True)
        start_btn.clicked.connect(self._start)
        btn_row.addWidget(start_btn)
        btn_row.addStretch(1)
        idle_layout.addLayout(btn_row)

        if not HAVE_WEBENGINE:
            note = QLabel(_("live_server_no_webkit"))
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignCenter)
            note.setProperty("dim", True)
            idle_layout.addWidget(note)
        idle_layout.addStretch(1)
        self.layout_.addWidget(self.idle)

        # -- running -------------------------------------------------------------
        self.running = QWidget()
        run_layout = QVBoxLayout(self.running)
        run_layout.setContentsMargins(0, 0, 0, 0)

        self.bar = QWidget()
        top = QHBoxLayout(self.bar)
        top.setContentsMargins(0, 0, 0, 0)
        reload_btn = QPushButton("⟳")
        reload_btn.setToolTip(_("live_server_reload"))
        reload_btn.setFixedWidth(38)
        reload_btn.clicked.connect(self._reload)
        top.addWidget(reload_btn)

        self.url_label = QLabel()
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setProperty("dim", True)
        top.addWidget(self.url_label, 1)

        copy_btn = QPushButton(_("repo_copy_path"))
        copy_btn.clicked.connect(self._copy_url)
        top.addWidget(copy_btn)

        external_btn = QPushButton(_("live_server_open_browser"))
        external_btn.clicked.connect(self._open_external)
        top.addWidget(external_btn)

        full_btn = QPushButton(_("live_server_fullscreen"))
        full_btn.clicked.connect(self._toggle_fullscreen)
        top.addWidget(full_btn)

        stop_btn = QPushButton(_("stop_live_server"))
        stop_btn.setProperty("danger", True)
        stop_btn.clicked.connect(self._stop)
        top.addWidget(stop_btn)
        run_layout.addWidget(self.bar)

        self.lan_label = QLabel()
        self.lan_label.setProperty("dim", True)
        self.lan_label.setVisible(False)
        run_layout.addWidget(self.lan_label)

        self.view_holder = QVBoxLayout()
        run_layout.addLayout(self.view_holder, 1)
        self.running.setVisible(False)
        self.layout_.addWidget(self.running, 1)

        QShortcut(QKeySequence(Qt.Key_F11), self,
                  activated=self._toggle_fullscreen)
        self._esc = QShortcut(QKeySequence(Qt.Key_Escape), self,
                              activated=self._escape)

    # -- start ------------------------------------------------------------------
    def _start(self):
        project = config.active_project()
        if not project:
            self.window.toast(_("no_project_title"))
            return

        root = self.stats.find_web_root(project["path"])
        host = "0.0.0.0" if self.lan_check.isChecked() else "127.0.0.1"
        self.port = self.liveserve.find_free_port(
            self.liveserve.DEFAULT_PORT, host)
        self.url = f"http://127.0.0.1:{self.port}/"
        self.url_label.setText(self.url)

        self.lan_label.setVisible(False)
        if host == "0.0.0.0":
            lan_ip = self.liveserve.lan_address()
            if lan_ip:
                self.lan_label.setText(_(
                    "live_server_lan_url", u=f"http://{lan_ip}:{self.port}/"))
                self.lan_label.setVisible(True)

        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "petacore.liveserve", root,
                 "--port", str(self.port), "--host", host],
                cwd=root, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError as e:
            self.window.toast(str(e))
            return

        self._clear_view()
        if HAVE_WEBENGINE:
            self.view = QWebEngineView()
            self.view_holder.addWidget(self.view)
            # The server needs a moment to bind before the first request;
            # loading immediately shows a connection error the user would
            # have to clear by hand.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(350, self._load_once)
        else:
            self.view = None
            note = QLabel(_("live_server_no_webkit"))
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignCenter)
            self.view_holder.addWidget(note)

        self.idle.setVisible(False)
        self.running.setVisible(True)
        # The site arrives from the right rather than appearing in place.
        _slide_in_from_right(self.running, self)
        self.window.toast(_("live_server_started",
                            root=os.path.relpath(root, project["path"])
                            or "."))

    def _load_once(self):
        if self.view is not None and self.url:
            from PySide6.QtCore import QUrl
            self.view.setUrl(QUrl(self.url))

    def _clear_view(self):
        while self.view_holder.count():
            item = self.view_holder.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

    # -- controls ----------------------------------------------------------------
    def _reload(self):
        if self.view is not None:
            self.view.reload()

    def _copy_url(self):
        if self.url:
            QApplication.clipboard().setText(self.url)
            self.window.toast(_("repo_copied"))

    def _open_external(self):
        if self.url:
            subprocess.Popen(["xdg-open", self.url])

    # -- fullscreen ---------------------------------------------------------------
    # The site goes full screen, not the application window. Fullscreening
    # the main window would still leave the sidebar and the toolbar around
    # the page, so instead the browser view is moved into a window of its
    # own with nothing else in it — the same view, so the page keeps its
    # state and does not reload.
    def _escape(self):
        if self.fullscreen:
            self._leave_fullscreen()

    def _toggle_fullscreen(self):
        if not self.running.isVisible():
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

        self.fs_window = QWidget()
        self.fs_window.setWindowTitle(self.url)
        fs_layout = QVBoxLayout(self.fs_window)
        fs_layout.setContentsMargins(0, 0, 0, 0)
        self.view_holder.removeWidget(self.view)
        fs_layout.addWidget(self.view)

        QShortcut(QKeySequence(Qt.Key_Escape), self.fs_window,
                  activated=self._leave_fullscreen)
        QShortcut(QKeySequence(Qt.Key_F11), self.fs_window,
                  activated=self._leave_fullscreen)

        self.fullscreen = True
        self.fs_window.showFullScreen()
        # Comes in from the left and grows into the screen. Qt can animate
        # the geometry itself, so this is a real scale rather than a fade
        # standing in for one.
        self._animate_fs(grow=True)
        self.window.toast(_("live_server_fullscreen_hint"))

    def _animate_fs(self, grow, on_done=None):
        """Grow the view in from the left, or send it back the same way."""
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect
        from PySide6.QtCore import QTimer

        def run():
            full = self.fs_window.rect()
            if full.width() <= 0:
                if on_done:
                    on_done()
                return
            # a smaller rectangle, held against the left edge and centred
            # vertically — the state the page animates out of and back into
            small = QRect(0, full.height() // 6,
                          int(full.width() * 0.66), int(full.height() * 0.66))
            animation = QPropertyAnimation(self.view, b"geometry",
                                           self.fs_window)
            animation.setDuration(FS_ANIM_MS)
            animation.setStartValue(small if grow else full)
            animation.setEndValue(full if grow else small)
            animation.setEasingCurve(QEasingCurve.OutCubic if grow
                                     else QEasingCurve.InCubic)
            if on_done:
                animation.finished.connect(on_done)
            self._fs_anim = animation       # keep it alive until it finishes
            animation.start()

        QTimer.singleShot(0, run)

    def _leave_fullscreen(self, animate=True):
        """Send the site back the way it arrived, then put it in the page.

        `animate=False` is for teardown — closing the window or switching
        project — where waiting out an animation on a widget that is about
        to be destroyed would be a crash waiting to happen.
        """
        window = getattr(self, "fs_window", None)
        if window is None or self._fs_closing:
            return
        self.fullscreen = False

        if not animate or self.view is None:
            self._finish_leave_fullscreen(window)
            return

        self._fs_closing = True
        self._animate_fs(grow=False,
                         on_done=lambda: self._finish_leave_fullscreen(window))

    def _finish_leave_fullscreen(self, window):
        self._fs_closing = False
        self.fs_window = None
        if self.view is not None:
            self.view.setParent(None)
            self.view_holder.addWidget(self.view)
        window.close()
        window.deleteLater()

    # -- stop -------------------------------------------------------------------
    def _stop(self):
        if self.fullscreen:
            self._leave_fullscreen(animate=False)
        proc = self.proc
        self.proc = None
        self.view = None
        self._clear_view()
        self.running.setVisible(False)
        self.idle.setVisible(True)
        if proc:
            _terminate_server(proc)

    def end_all(self):
        if self.fullscreen:
            self._leave_fullscreen(animate=False)
        if self.proc:
            _terminate_server(self.proc)
            self.proc = None

    def stop(self):
        self.end_all()


def _terminate_server(proc):
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

        # Building a package and then forgetting to put it in the repository
        # is the most common way an update never reaches anyone, so the two
        # steps are joined here. The index is left alone: signing it is a
        # decision of its own.
        from .. import repo as _repo
        repo_box = QGroupBox(_("repo_page"))
        repo_layout = QVBoxLayout(repo_box)
        target = _repo.active_profile()
        self.repo_check = QCheckBox(_("repo_autoadd"))
        self.repo_check.setChecked(bool(config.get("repo_autoadd")))
        self.repo_check.setEnabled(bool(target))
        self.repo_check.setToolTip(target["name"] if target
                                   else _("repo_none_title"))
        self.repo_check.stateChanged.connect(
            lambda s: config.set("repo_autoadd", bool(s)))
        repo_layout.addWidget(self.repo_check)
        note = QLabel(_("repo_autoadd_hint"))
        note.setWordWrap(True)
        note.setProperty("dim", True)
        repo_layout.addWidget(note)
        layout.addWidget(repo_box)

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
            if fmt == "deb" and config.get("repo_autoadd"):
                self._add_to_repo(path)

        run_async(self, work, done)

    def _add_to_repo(self, deb_path):
        """Put a freshly built package into the active repository.

        RPMs are left out on purpose: this repository is an APT archive, and
        quietly copying an .rpm into it would produce a file nothing reads.
        """
        from .. import repo
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

        run_async(self, lambda: repo.add_packages(profile, [deb_path]), done)


# --------------------------------------------------------------------------- #
# Versions — the project's release history, and making the next release
# --------------------------------------------------------------------------- #
VERSION_CHANNELS = ["stable", "rc", "beta", "alpha"]
# Breeze's own colours for each release type, so the page sits with the
# rest of Plasma: positive green, the purple, the highlight blue, neutral
# orange.
CHANNEL_COLORS = {"stable": "#27ae60", "rc": "#9b59b6",
                  "beta": "#3daee9", "alpha": "#f67400"}

_VERSIONS_QSS = """
QFrame#pcHero { border-radius: 18px; border: 1px solid rgba(127,127,127,0.18); }
QLabel#pcBig { font-size: 30pt; font-weight: 800; }
QLabel#pcKicker { font-size: 8pt; font-weight: 800; letter-spacing: 1.5px; }
QFrame#pcSeries { border-radius: 14px; border: 1px solid rgba(127,127,127,0.22);
                  background: palette(base); }
QLabel#pcSeriesTitle { font-size: 13pt; font-weight: 700; }
QLabel#pcTitle { font-size: 11pt; font-weight: 700; }
QLabel[chip="true"] { border-radius: 7px; padding: 2px 9px; font-size: 8.5pt;
                      background: rgba(127,127,127,0.14); }
QLabel[chip="signed"] { border-radius: 7px; padding: 2px 9px; font-size: 8.5pt;
                        background: rgba(39,174,96,0.18); color: #27ae60; }
QFrame#pcDetails { border-radius: 12px; background: rgba(127,127,127,0.08); }
QPushButton#pcHead { border: none; background: transparent; text-align: left;
                     padding: 6px 8px; border-radius: 10px; }
QPushButton#pcHead:hover { background: rgba(127,127,127,0.10); }
QPushButton[pcsmall="true"] { border-radius: 12px; padding: 4px 12px; }
QPushButton[pcdanger="true"] { border-radius: 12px; padding: 4px 12px;
                               color: #da4453; background: rgba(218,68,83,0.12);
                               border: none; }
QPushButton[pcround="true"] { border-radius: 17px; padding: 0;
                              background: rgba(127,127,127,0.16); border: none; }
QPushButton[pcround="true"]:hover { background: rgba(127,127,127,0.26); }
"""


def _snug(widget):
    """Keep a badge at its own size. A label in a row otherwise stretches
    to the row's height, and a pill becomes a slab."""
    from PySide6.QtWidgets import QSizePolicy
    widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return widget


def _pill(channel):
    text = "RC" if channel == "rc" else _(f"ver_ch_{channel}").upper()
    label = QLabel(text)
    label.setStyleSheet(
        f"background: {CHANNEL_COLORS[channel]}; color: white; "
        "border-radius: 7px; padding: 1px 8px; font-size: 7.5pt; "
        "font-weight: 800;")
    return _snug(label)


def _chip(text, kind="true", tooltip=""):
    label = QLabel(text)
    label.setProperty("chip", kind)
    if tooltip:
        label.setToolTip(tooltip)
    return _snug(label)


def _chips(release):
    row = QHBoxLayout()
    row.setSpacing(6)
    for item in release["files"]:
        row.addWidget(_chip(f'.{item["kind"]}  {human_size(item["size"])}',
                            tooltip=item["name"]), 0, Qt.AlignVCenter)
    if release["signed_by"]:
        row.addWidget(_chip(f'🔒 {_("ver_badge_signed")}', "signed",
                            release["signed_by"][-16:]), 0, Qt.AlignVCenter)
    row.addStretch(1)
    return row


def _stamp(when):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else ""


def _group_series(releases):
    groups, order = {}, []
    for release in releases:
        series = ".".join(release["number"].split(".")[:2])
        if series not in groups:
            groups[series] = []
            order.append(series)
        groups[series].append(release)
    return [(s, groups[s]) for s in order]


class _Rail(QWidget):
    """The timeline down the left of a series: a line through every entry
    and a dot in the release type's colour. Painted rather than styled, so
    the line runs unbroken from one entry into the next."""

    def __init__(self, color, first, last):
        super().__init__()
        self.color, self.first, self.last = QColor(color), first, last
        self.setFixedWidth(24)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        x, dot_y = self.width() / 2, 21
        line = QColor(127, 127, 127, 70)
        painter.setPen(QPen(line, 2))
        if not self.first:
            painter.drawLine(int(x), 0, int(x), dot_y)
        if not self.last:
            painter.drawLine(int(x), dot_y, int(x), self.height())
        halo = QColor(self.color)
        halo.setAlpha(50)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(int(x - 9), dot_y - 9, 18, 18)
        painter.setBrush(self.color)
        painter.drawEllipse(int(x - 6), dot_y - 6, 12, 12)
        painter.end()


class VersionsPage(QWidget):
    """Every release, newest first, with the form for the next one below.
    Mirrors the GNOME page: the release people run first and largest, then
    each series as a timeline."""

    def __init__(self, window):
        super().__init__()
        from .. import versions
        self.versions = versions
        self.window = window
        self._undo = []
        self._keys = []
        self._just_created = False
        self.setAcceptDrops(True)
        self.setStyleSheet(_VERSIONS_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel(_("versions_page"))
        title.setProperty("title", True)
        head.addWidget(title, 1)
        self.undo_btn = QPushButton(_("repo_undo"))
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self._undo_remove)
        head.addWidget(self.undo_btn)
        layout.addLayout(head)
        hint = QLabel(_("versions_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        layout.addWidget(hint)

        self.library = QVBoxLayout()
        self.library.setSpacing(12)
        layout.addLayout(self.library)

        layout.addWidget(self._build_form())
        layout.addStretch(1)
        self.refresh()

    # -- the form -----------------------------------------------------------------
    def _build_form(self):
        project = config.active_project() or {}
        remembered = self.versions.defaults(project["path"]) \
            if project.get("path") else {}

        box = QGroupBox(_("ver_new"))
        form = QFormLayout(box)
        note = QLabel(_("ver_new_hint"))
        note.setProperty("dim", True)
        form.addRow(note)

        self.number = QLineEdit()
        self.number.textChanged.connect(self._update_preview)
        form.addRow(_("ver_number"), self.number)
        self.channel = QComboBox()
        self.channel.addItems([_(f"ver_ch_{c}") for c in VERSION_CHANNELS])
        self.channel.currentIndexChanged.connect(self._on_channel)
        form.addRow(_("ver_channel"), self.channel)
        self.pre = QSpinBox()
        self.pre.setRange(1, 99)
        self.pre.valueChanged.connect(self._update_preview)
        self.pre_label = QLabel(_("ver_pre"))
        form.addRow(self.pre_label, self.pre)
        self.preview = QLabel()
        self.preview.setProperty("dim", True)
        form.addRow(self.preview)

        wanted = (remembered.get("formats") or "deb").split(",")
        self.deb = QCheckBox(_("ver_fmt_deb"))
        self.deb.setChecked("deb" in wanted)
        self.rpm = QCheckBox(_("ver_fmt_rpm"))
        have_rpm = shutil.which("rpmbuild") is not None
        self.rpm.setEnabled(have_rpm)
        self.rpm.setChecked(have_rpm and "rpm" in wanted)
        if not have_rpm:
            self.rpm.setToolTip("rpmbuild — sudo apt install rpm")
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(self.deb)
        fmt_row.addWidget(self.rpm)
        form.addRow(_("ver_formats"), fmt_row)

        user = os.environ.get("USER", "developer")
        self.maint = QLineEdit(remembered.get("maintainer")
                               or f"{user} <{user}@localhost>")
        form.addRow(_("maintainer"), self.maint)
        self.desc = QLineEdit(remembered.get("description", ""))
        form.addRow(_("description_f"), self.desc)
        from ..debbuild import LICENSE_CHOICES
        self._license_ids = [spdx for spdx, _d in LICENSE_CHOICES]
        self.license = QComboBox()
        self.license.addItems([d for _s, d in LICENSE_CHOICES])
        if remembered.get("license") in self._license_ids:
            self.license.setCurrentIndex(
                self._license_ids.index(remembered["license"]))
        form.addRow(_("license_f"), self.license)

        self.sign = QCheckBox(_("sign_packages"))
        self.sign.setChecked(bool(remembered.get(
            "sign", config.get("sign_packages"))))
        self.sign.setToolTip(_("ver_signing_hint"))
        self.sign.stateChanged.connect(self._sync_signing)
        form.addRow(_("ver_signing"), self.sign)
        self.key = QComboBox()
        form.addRow(_("repo_key"), self.key)
        self._load_keys(remembered.get("key") or config.get("gpg_key"))

        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText(_("ver_notes_hint"))
        self.notes.setMinimumHeight(100)
        form.addRow(_("ver_notes"), self.notes)

        from .. import repo as _repo
        target = _repo.active_profile()
        self.to_repo = QCheckBox(_("repo_autoadd"))
        self.to_repo.setEnabled(bool(target))
        self.to_repo.setChecked(bool(target)
                                and bool(config.get("repo_autoadd")))
        form.addRow(self.to_repo)

        self.create_btn = QPushButton(_("ver_create"))
        self.create_btn.setProperty("primary", True)
        self.create_btn.clicked.connect(self._create)
        form.addRow(self.create_btn)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("dim", True)
        form.addRow(self.status)
        return box

    def _load_keys(self, preferred=""):
        from .. import gpgsign
        self._keys = gpgsign.list_keys_detailed()
        self.key.clear()
        for k in self._keys:
            self.key.addItem(f'{k["uid"]}  ·  {k["fpr"][-16:]}', k["fpr"])
            if k["fpr"] == preferred:
                self.key.setCurrentIndex(self.key.count() - 1)
        if not self._keys:
            self.key.addItem(_("no_key"), "")
        self._sync_signing()

    def _sync_signing(self, *_a):
        self.key.setEnabled(self.sign.isChecked() and bool(self._keys))

    def _channel_code(self):
        return VERSION_CHANNELS[self.channel.currentIndex()]

    def _on_channel(self, *_a):
        channel = self._channel_code()
        self.pre.setVisible(channel != "stable")
        self.pre_label.setVisible(channel != "stable")
        project = config.active_project()
        if project and channel != "stable":
            try:
                number = self.versions.clean_number(self.number.text())
                self.pre.setValue(self.versions.suggest_pre(
                    project["path"], number, channel))
            except self.versions.VersionError:
                pass
        self._update_preview()

    def _update_preview(self, *_a):
        channel = self._channel_code()
        pre = self.pre.value()
        try:
            number = self.versions.clean_number(self.number.text())
        except self.versions.VersionError as e:
            self.preview.setText(str(e))
            self.create_btn.setEnabled(False)
            return
        self.preview.setText(_(
            "ver_preview", l=self.versions.label(number, channel, pre),
            p=self.versions.package_version(number, channel, pre)))
        self.create_btn.setEnabled(True)

    # -- the history ----------------------------------------------------------------
    def _clear_library(self):
        while self.library.count():
            item = self.library.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)

    def refresh(self):
        self._clear_library()
        self._toggles = []
        project = config.active_project()
        if not project:
            return
        releases = self.versions.list_versions(project["path"])

        if not releases:
            empty = QFrame()
            empty.setObjectName("pcSeries")
            box = QVBoxLayout(empty)
            box.setContentsMargins(24, 24, 24, 24)
            head = QLabel(_("versions_empty"))
            head.setObjectName("pcSeriesTitle")
            head.setAlignment(Qt.AlignCenter)
            box.addWidget(head)
            body = QLabel(_("versions_empty_body"))
            body.setWordWrap(True)
            body.setAlignment(Qt.AlignCenter)
            body.setProperty("dim", True)
            box.addWidget(body)
            self.library.addWidget(empty)
        else:
            stable = [r for r in releases if r["channel"] == "stable"]
            latest = stable[0] if stable else releases[0]
            key = self.versions.sort_key
            newer = [r for r in releases
                     if key(r["number"], r["channel"], r["pre"])
                     > key(latest["number"], latest["channel"], latest["pre"])]

            summary = QLabel(_("ver_summary", n=len(releases), s=len(stable),
                               t=_ago(releases[0]["created"])))
            summary.setProperty("dim", True)
            self.library.addWidget(summary)
            self.library.addWidget(self._hero(latest,
                                              newer[0] if newer else None))
            for series, members in _group_series(releases):
                self.library.addWidget(self._series_card(series, members))

        if not self.number.text().strip() or self._just_created:
            self.number.setText(self.versions.suggest_next(project["path"]))
            self._just_created = False
        self._on_channel()

    def _hero(self, release, upcoming=None):
        color = CHANNEL_COLORS[release["channel"]]
        card = QFrame()
        card.setObjectName("pcHero")
        tint = QColor(color)
        card.setStyleSheet(
            "QFrame#pcHero { background: qlineargradient(x1:0, y1:0, x2:1, "
            f"y2:1, stop:0 rgba({tint.red()},{tint.green()},{tint.blue()},70), "
            f"stop:0.7 rgba({tint.red()},{tint.green()},{tint.blue()},10)); }}")
        box = QVBoxLayout(card)
        box.setContentsMargins(22, 18, 22, 18)
        box.setSpacing(8)

        top = QHBoxLayout()
        kicker = QLabel(_("ver_latest"))
        kicker.setObjectName("pcKicker")
        kicker.setProperty("dim", True)
        top.addWidget(kicker)
        top.addWidget(_pill(release["channel"]), 0, Qt.AlignVCenter)
        top.addStretch(1)
        when = QLabel(_ago(release["created"]))
        when.setProperty("dim", True)
        when.setToolTip(_stamp(release["created"]))
        top.addWidget(when)
        box.addLayout(top)

        big = QLabel(release["label"])
        big.setObjectName("pcBig")
        box.addWidget(big)
        if release["notes"]:
            notes = QLabel(release["notes"])
            notes.setWordWrap(True)
            box.addWidget(notes)

        bottom = _chips(release)
        bottom.addWidget(self._round("📂", _("repo_open_folder"),
                                     lambda: subprocess.Popen(
                                         ["xdg-open", release["path"]])))
        debs = [f["path"] for f in release["files"] if f["kind"] == "deb"]
        if debs:
            bottom.addWidget(self._round("⇪", _("ver_to_repo"),
                                         lambda: self._to_repo(debs[0])))
        box.addLayout(bottom)

        if upcoming:
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet("color: rgba(127,127,127,0.25);")
            box.addWidget(line)
            row = QHBoxLayout()
            dev = QLabel(_("ver_in_dev"))
            dev.setProperty("dim", True)
            row.addWidget(dev)
            name = QLabel(upcoming["label"])
            name.setObjectName("pcTitle")
            row.addWidget(name)
            row.addWidget(_pill(upcoming["channel"]), 0, Qt.AlignVCenter)
            row.addStretch(1)
            ago = QLabel(_ago(upcoming["created"]))
            ago.setProperty("dim", True)
            row.addWidget(ago)
            box.addLayout(row)
        return card

    def _series_card(self, series, members):
        card = QFrame()
        card.setObjectName("pcSeries")
        box = QVBoxLayout(card)
        box.setContentsMargins(10, 10, 12, 10)
        box.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(8, 2, 4, 6)
        title = QLabel(_("ver_series", s=series))
        title.setObjectName("pcSeriesTitle")
        head.addWidget(title)
        head.addStretch(1)
        count = _("ver_series_one") if len(members) == 1 \
            else _("ver_series_count", n=len(members))
        if not any(m["channel"] == "stable" for m in members):
            count += f'  ·  {_("ver_series_open")}'
        meta = QLabel(count)
        meta.setProperty("dim", True)
        head.addWidget(meta)
        box.addLayout(head)

        for index, release in enumerate(members):
            box.addWidget(self._timeline_entry(
                release, index == 0, index == len(members) - 1))
        return card

    def _timeline_entry(self, release, first, last):
        entry = QWidget()
        row = QHBoxLayout(entry)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(_Rail(CHANNEL_COLORS[release["channel"]], first, last))

        body = QVBoxLayout()
        body.setSpacing(4)
        body.setContentsMargins(0, 4, 0, 10)

        header = QPushButton()
        header.setObjectName("pcHead")
        header.setCursor(Qt.PointingHandCursor)
        head = QHBoxLayout(header)
        head.setContentsMargins(8, 4, 8, 4)
        name = QLabel(release["label"])
        name.setObjectName("pcTitle")
        head.addWidget(name)
        if release["channel"] != "stable":
            head.addWidget(_pill(release["channel"]), 0, Qt.AlignVCenter)
        if release["source"] == "imported":
            head.addWidget(_chip(_("ver_badge_imported")), 0,
                           Qt.AlignVCenter)
        head.addStretch(1)
        when = QLabel(_ago(release["created"]))
        when.setProperty("dim", True)
        when.setToolTip(_stamp(release["created"]))
        head.addWidget(when)
        chevron = QLabel("▾")
        chevron.setProperty("dim", True)
        head.addWidget(chevron)
        header.setMinimumHeight(36)
        body.addWidget(header)

        chips = _chips(release)
        chips.setContentsMargins(8, 0, 0, 0)
        body.addLayout(chips)

        details = QFrame()
        details.setObjectName("pcDetails")
        inner = QVBoxLayout(details)
        inner.setContentsMargins(14, 12, 14, 12)
        notes = QLabel(release["notes"] or _("ver_no_notes"))
        notes.setWordWrap(True)
        notes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if not release["notes"]:
            notes.setProperty("dim", True)
        inner.addWidget(notes)
        actions = QHBoxLayout()
        actions.addWidget(self._small(_("repo_open_folder"),
                                      lambda: subprocess.Popen(
                                          ["xdg-open", release["path"]])))
        debs = [f["path"] for f in release["files"] if f["kind"] == "deb"]
        if debs:
            actions.addWidget(self._small(_("ver_to_repo"),
                                          lambda: self._to_repo(debs[0])))
        actions.addWidget(self._small(_("ver_edit_notes"),
                                      lambda: self._edit_notes(release)))
        actions.addStretch(1)
        remove = QPushButton(_("ver_remove"))
        remove.setProperty("pcdanger", True)
        remove.clicked.connect(lambda: self._remove(release))
        actions.addWidget(remove)
        inner.addLayout(actions)
        details.setVisible(False)
        body.addWidget(details)

        def toggle():
            opening = not details.isVisible()
            details.setVisible(opening)
            chevron.setText("▴" if opening else "▾")
        header.clicked.connect(toggle)
        self._toggles.append(toggle)

        row.addLayout(body, 1)
        return entry

    def _round(self, glyph, tooltip, callback):
        button = QPushButton(glyph)
        button.setProperty("pcround", True)
        button.setFixedSize(34, 34)
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button

    def _small(self, text, callback):
        button = QPushButton(text)
        button.setProperty("pcsmall", True)
        button.clicked.connect(callback)
        return button

    # -- creating -----------------------------------------------------------------
    def _create(self):
        project = config.active_project()
        if not project:
            return
        formats = [f for f, box in (("deb", self.deb), ("rpm", self.rpm))
                   if box.isChecked()]
        channel = self._channel_code()
        pre = self.pre.value() if channel != "stable" else 0
        try:
            number = self.versions.clean_number(self.number.text())
        except self.versions.VersionError as e:
            self.window.toast(str(e))
            return
        sign = self.sign.isChecked()
        key_fpr = (self.key.currentData() or "") if sign else ""
        notes = self.notes.toPlainText()
        shown = self.versions.label(number, channel, pre)
        add_to_repo = self.to_repo.isChecked()
        maint, desc = self.maint.text(), self.desc.text()
        license_id = self._license_ids[self.license.currentIndex()]

        self.create_btn.setEnabled(False)
        self.status.setText(_("ver_creating", l=shown))
        self.window.begin_operation(_("ver_creating", l=shown))

        def work():
            from .. import debbuild
            release_dir = self.versions.create(
                project["path"], project["name"], number, channel, pre,
                formats, maint, desc, license_id,
                debbuild.repo_homepage(project.get("repo_url")),
                notes, sign, key_fpr)
            added = []
            if add_to_repo:
                from .. import repo
                profile = repo.active_profile()
                debs = [os.path.join(release_dir, n)
                        for n in os.listdir(release_dir) if n.endswith(".deb")]
                if profile and debs:
                    added, _f = repo.add_packages(profile, debs)
            return added

        def done(added, error):
            self.window.end_operation()
            self.create_btn.setEnabled(True)
            if error:
                self.status.setText(_("ver_failed", e=error))
                return
            self.status.setText("")
            self.window.toast(_("ver_created", l=shown))
            if added:
                self.window.toast(_("repo_added", n=len(added)))
            self.notes.clear()
            self._just_created = True
            self.refresh()

        run_async(self, work, done)

    def _to_repo(self, deb_path):
        from .. import repo
        profile = repo.active_profile()
        if not profile:
            self.window.toast(_("repo_none_title"))
            return
        run_async(self, lambda: repo.add_packages(profile, [deb_path]),
                  lambda r, e: self.window.toast(
                      str(e) if e else _("repo_added_to", n=profile["name"])))

    # -- notes and removal ------------------------------------------------------------
    def _edit_notes(self, release):
        text, ok = QInputDialog.getMultiLineText(
            self, release["label"], _("ver_notes"), release["notes"])
        if ok:
            self.versions.set_notes(release["path"], text)
            self.refresh()

    def _remove(self, release):
        project = config.active_project()
        if not project:
            return
        try:
            trashed = self.versions.remove(project["path"], release["path"])
        except (self.versions.VersionError, OSError) as e:
            self.window.toast(str(e))
            return
        self._undo.append((trashed, release["label"]))
        self.undo_btn.setEnabled(True)
        self.window.toast(_("ver_removed", l=release["label"]))
        self.refresh()

    def _undo_remove(self):
        project = config.active_project()
        if not project or not self._undo:
            return
        trashed, shown = self._undo.pop()
        self.undo_btn.setEnabled(bool(self._undo))
        try:
            self.versions.restore(project["path"], trashed)
        except (self.versions.VersionError, OSError) as e:
            self.window.toast(str(e))
            return
        self.window.toast(_("ver_restored", l=shown))
        self.refresh()

    # -- importing an older release -------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.toLocalFile().endswith(self.versions.PACKAGE_SUFFIXES)]
        if paths:
            event.acceptProposedAction()
            self._ask_import(paths)

    def _ask_import(self, paths):
        project = config.active_project()
        if not project:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(_("ver_import_q"))
        form = QFormLayout(dialog)
        names = QLabel("\n".join(os.path.basename(p) for p in paths[:6])
                       + f'\n\n{_("ver_import_hint")}')
        names.setWordWrap(True)
        form.addRow(names)
        match = re.search(r"[_-](\d+(?:\.\d+){0,3})",
                          os.path.basename(paths[0]))
        number = QLineEdit(match.group(1) if match else "")
        form.addRow(_("ver_number"), number)
        channel = QComboBox()
        channel.addItems([_(f"ver_ch_{c}") for c in VERSION_CHANNELS])
        form.addRow(_("ver_channel"), channel)
        pre = QSpinBox()
        pre.setRange(1, 99)
        form.addRow(_("ver_pre"), pre)
        buttons = QHBoxLayout()
        cancel = QPushButton(_("cancel"))
        cancel.clicked.connect(dialog.reject)
        go = QPushButton(_("ver_import_go"))
        go.setProperty("primary", True)
        go.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(go)
        form.addRow(buttons)
        if not dialog.exec():
            return

        chosen = VERSION_CHANNELS[channel.currentIndex()]
        pre_value = pre.value() if chosen != "stable" else 0
        text = number.text()

        def work():
            self.versions.import_files(project["path"], text, chosen,
                                       pre_value, paths)
            return self.versions.label(self.versions.clean_number(text),
                                       chosen, pre_value)

        def done(shown, error):
            self.window.toast(str(error) if error
                              else _("ver_imported", l=shown))
            self.refresh()

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
# Repository → Server: the repository folder on a server, over SSH
# --------------------------------------------------------------------------- #
from ..remote import TWO_FACTOR_HOWTO as _TWO_FACTOR_HOWTO_QT  # noqa: E402


class _AskBridge(QObject):
    """Carries ssh's questions from the askpass thread to the GUI thread.

    Qt widgets may only be touched on the thread that owns them; the
    question arrives on a background thread. A queued signal crosses over,
    the dialog runs here, and an Event hands the answer back."""
    asked = Signal(str, str)

    def __init__(self, parent):
        super().__init__()
        self.parent_widget = parent
        self._answer = None
        self._done = threading.Event()
        self.asked.connect(self._show)

    def ask(self, kind, prompt):
        if kind == "touch":
            return ""
        self._done.clear()
        self._answer = None
        self.asked.emit(kind, prompt)
        self._done.wait(300)
        answer, self._answer = self._answer, None
        return answer

    def _show(self, kind, prompt):
        heading = {"passphrase": _("srv_prompt_passphrase"),
                   "code": _("srv_prompt_code"),
                   "password": _("srv_prompt_password")}.get(
            kind, _("srv_prompt_other"))
        body = prompt if kind == "passphrase" \
            else f'{_("srv_prompt_from_server")}\n{prompt}'
        mode = QLineEdit.Normal if kind == "code" else QLineEdit.Password
        text, ok = QInputDialog.getText(self.parent_widget, heading, body,
                                        mode)
        self._answer = text if ok else None
        self._done.set()


class RemotePanel(QWidget):
    """The repository folder on a server, as a file manager. The security is
    in remote.py; see the GNOME panel for how the questions are handled."""

    def __init__(self, window, owner):
        super().__init__()
        from .. import remote
        self.remote = remote
        self.window = window
        self.owner = owner
        self.session = None
        self.cwd = ""
        self._undo = []
        self._asker = _AskBridge(self)
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setup = self._build_setup()
        self.browser = self._build_browser()
        layout.addWidget(self.setup)
        layout.addWidget(self.browser)
        self.browser.setVisible(False)

    @property
    def profile(self):
        return self.owner.profile

    # -- the connection form --------------------------------------------------------
    def _build_setup(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        col = QVBoxLayout(body)
        profile = self.profile

        title = QLabel(_("srv_title"))
        title.setProperty("heading", True)
        col.addWidget(title)
        hint = QLabel(_("srv_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        col.addWidget(hint)

        form = QFormLayout()
        self.host = QLineEdit(profile["server_host"])
        form.addRow(_("srv_host"), self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(profile["server_port"] or 22))
        form.addRow(_("srv_port"), self.port)
        self.user = QLineEdit(profile["server_user"])
        form.addRow(_("srv_user"), self.user)
        self.root = QLineEdit(profile["server_root"] or "/var/www/repo")
        form.addRow(_("srv_root"), self.root)
        key_row = QHBoxLayout()
        self.key = QComboBox()
        key_row.addWidget(self.key, 1)
        pick = QPushButton(_("srv_pick_key"))
        pick.clicked.connect(self._pick_identity)
        key_row.addWidget(pick)
        new_key = QPushButton(_("srv_new_key"))
        new_key.clicked.connect(self._new_key)
        key_row.addWidget(new_key)
        form.addRow(_("srv_identity"), key_row)
        col.addLayout(form)
        self._load_identities(profile["server_identity"])

        security = QGroupBox(_("srv_security"))
        sec = QVBoxLayout(security)
        summary = self.remote.security_summary()
        for text, detail in (
                (_("srv_sec_hostkey"), "StrictHostKeyChecking"),
                (_("srv_sec_pq") if summary["post_quantum"]
                 else _("srv_sec_kex"), summary["kex"]),
                (_("srv_sec_cipher"), summary["cipher"]),
                (_("srv_sec_nopass"), ""), (_("srv_sec_nostore"), ""),
                (_("srv_sec_2fa"), "")):
            line = QLabel(f'<span style="color:#27ae60">✔</span>&nbsp; {text}'
                          + (f'&nbsp;&nbsp;<span style="color:gray">'
                             f'{detail}</span>' if detail else ""))
            sec.addWidget(line)
        # A disclosure line rather than a checkable group box: a closed
        # group box still draws its frame, which reads as an empty field.
        howto = QPushButton("▸  " + _("srv_2fa_howto"))
        howto.setFlat(True)
        howto.setCheckable(True)
        howto.setStyleSheet("text-align: left; padding: 4px 2px;")
        text = QPlainTextEdit(_TWO_FACTOR_HOWTO_QT)
        text.setReadOnly(True)
        text.setMinimumHeight(190)
        text.setVisible(False)

        def disclose(opened):
            text.setVisible(opened)
            howto.setText(("▾  " if opened else "▸  ") + _("srv_2fa_howto"))
        howto.toggled.connect(disclose)
        sec.addWidget(howto)
        sec.addWidget(text)
        col.addWidget(security)

        row = QHBoxLayout()
        row.addStretch(1)
        self.connect_btn = QPushButton(_("srv_connect"))
        self.connect_btn.setProperty("primary", True)
        self.connect_btn.clicked.connect(self._connect)
        row.addWidget(self.connect_btn)
        row.addStretch(1)
        col.addLayout(row)
        self.setup_status = QLabel()
        self.setup_status.setAlignment(Qt.AlignCenter)
        self.setup_status.setProperty("dim", True)
        col.addWidget(self.setup_status)
        col.addStretch(1)
        return scroll

    def _load_identities(self, preferred=""):
        # The saved key is always offered, wherever it lives — otherwise the
        # list falls back to its first key and logs in as the wrong one.
        self.key.clear()
        preferred = os.path.expanduser(preferred or "")
        self._identities = self.remote.list_identities()
        if preferred and os.path.isfile(preferred) \
                and preferred not in self._identities:
            self._identities.insert(0, preferred)
        home = os.path.expanduser("~")
        for path in self._identities:
            label = "~" + path[len(home):] \
                if path.startswith(home + os.sep) else path
            self.key.addItem(label, path)
            if path == preferred:
                self.key.setCurrentIndex(self.key.count() - 1)
        if not self._identities:
            self.key.addItem(_("srv_no_identity"), "")

    def _pick_identity(self):
        path, _sel = QFileDialog.getOpenFileName(
            self, _("srv_pick_key"), os.path.expanduser("~/.ssh"))
        if not path:
            return
        if path.endswith(".pub"):
            path = path[:-4]
        if not os.path.isfile(path):
            self.window.toast(_("srv_key_missing"))
            return
        self._load_identities(path)

    def _new_key(self):
        host = self.host.text().strip() or "server"
        name, ok = QInputDialog.getText(
            self, _("srv_new_key"), _("srv_key_name"),
            text=f"petacore_{re.sub(r'[^a-z0-9]+', '_', host.lower())}")
        if not ok or not name.strip():
            return
        first, ok = QInputDialog.getText(self, _("srv_new_key"),
                                         _("srv_key_pass"), QLineEdit.Password)
        if not ok:
            return
        second, ok = QInputDialog.getText(self, _("srv_new_key"),
                                          _("srv_key_pass2"),
                                          QLineEdit.Password)
        if not ok:
            return
        if first != second:
            self.window.toast(_("srv_key_mismatch"))
            return
        path = os.path.join("~/.ssh", re.sub(r"[^A-Za-z0-9_.-]", "_",
                                             name.strip()))

        def done(public, error):
            if error:
                self.window.toast(str(error))
                return
            self._load_identities(os.path.expanduser(path))
            box = QMessageBox(self)
            box.setWindowTitle(_("srv_key_created"))
            box.setText(_("srv_key_where"))
            box.setDetailedText(public)
            copy = box.addButton(_("repo_copy_path"), QMessageBox.ActionRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is copy:
                QApplication.clipboard().setText(public)
                self.window.toast(_("repo_copied"))

        run_async(self, lambda: self.remote.generate_key(
            path, first, f"petacore@{host}"), done)

    # -- connecting ------------------------------------------------------------------
    def _connect(self):
        target = {"host": self.host.text().strip(), "port": self.port.value(),
                  "user": self.user.text().strip(),
                  "root": self.root.text().strip(),
                  "identity": self.key.currentData() or ""}
        if not target["host"] or not target["user"] or not target["root"]:
            self.window.toast(_("srv_fields_missing"))
            return
        try:
            target = self.remote.normalise_target(target)
        except self.remote.RemoteError as e:
            self.window.toast(str(e))
            return
        from .. import repo
        self.owner.profile = repo.save_profile(dict(
            self.profile, server_host=target["host"],
            server_port=target["port"], server_user=target["user"],
            server_root=target["root"],
            server_identity=target["identity"]), self.profile["name"])
        session = self.remote.Session(target, self._asker.ask)
        self.connect_btn.setEnabled(False)
        self.setup_status.setText(_("srv_connecting"))

        def done(_r, error):
            self.connect_btn.setEnabled(True)
            self.setup_status.setText("")
            if isinstance(error, self.remote.HostKeyUnknown):
                self._confirm_host(session, error)
                return
            if isinstance(error, self.remote.HostKeyChanged):
                self._host_changed(session)
                return
            if error:
                self.window.toast(str(error))
                return
            self.session = session
            self.cwd = ""
            self._fill_header()
            self.setup.setVisible(False)
            self.browser.setVisible(True)
            self.refresh()

        run_async(self, session.connect, done)

    def _confirm_host(self, session, info):
        box = QMessageBox(self)
        box.setWindowTitle(_("srv_trust_q"))
        box.setText(
            _("srv_trust_body", h=session.target["host"])
            + "\n\nssh-keygen -lf /etc/ssh/ssh_host_"
            + info.key_type.lower().split("-")[0] + "_key.pub\n\n"
            + f'{_("srv_fingerprint")} ({info.key_type}):\n{info.fingerprint}')
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.setStandardButtons(QMessageBox.Cancel)
        trust = box.addButton(_("srv_trust_go"), QMessageBox.AcceptRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is trust:
            try:
                self.remote.trust_host_key(session.target, info.line)
            except self.remote.RemoteError as e:
                self.window.toast(str(e))
                return
            self._connect()

    def _host_changed(self, session):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(_("srv_changed_q"))
        box.setText(_("srv_changed_body", h=session.target["host"]))
        box.setStandardButtons(QMessageBox.Close)
        forget = box.addButton(_("srv_forget_key"),
                               QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is forget:
            self.remote.forget_host_key(session.target)

    # -- the browser ---------------------------------------------------------------------
    def _build_browser(self):
        outer = QWidget()
        col = QVBoxLayout(outer)
        col.setContentsMargins(0, 0, 0, 0)

        head = QFrame()
        head.setObjectName("pcSeries")
        head.setStyleSheet("QFrame#pcSeries { border-radius: 14px; "
                           "border: 1px solid rgba(127,127,127,0.22); "
                           "background: palette(base); }")
        row = QHBoxLayout(head)
        row.setContentsMargins(14, 10, 14, 10)
        lock = QLabel("🔒")
        row.addWidget(lock)
        titles = QVBoxLayout()
        self.head_title = QLabel()
        self.head_title.setStyleSheet("font-weight: 700;")
        self.head_sub = QLabel()
        self.head_sub.setProperty("dim", True)
        self.head_sub.setTextInteractionFlags(Qt.TextSelectableByMouse)
        titles.addWidget(self.head_title)
        titles.addWidget(self.head_sub)
        row.addLayout(titles, 1)
        disconnect = QPushButton(_("srv_disconnect"))
        disconnect.clicked.connect(self.disconnect)
        row.addWidget(disconnect)
        col.addWidget(head)

        bar = QHBoxLayout()
        self.up_btn = QPushButton("↑ " + _("srv_root_up"))
        self.up_btn.clicked.connect(self._go_up)
        bar.addWidget(self.up_btn)
        self.crumb = QLabel()
        self.crumb.setStyleSheet("font-family: monospace;")
        bar.addWidget(self.crumb, 1)
        for text, slot in ((_("srv_new_folder"), self._new_folder),
                           (_("srv_upload"), self._pick_uploads)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        self.undo_btn = QPushButton(_("repo_undo"))
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self._undo_last)
        bar.addWidget(self.undo_btn)
        self.rebuild_btn = QPushButton(_("srv_rebuild"))
        self.rebuild_btn.setProperty("primary", True)
        self.rebuild_btn.clicked.connect(self._rebuild)
        bar.addWidget(self.rebuild_btn)
        col.addLayout(bar)

        self.stale = QLabel(_("srv_stale"))
        self.stale.setWordWrap(True)
        self.stale.setStyleSheet("background: rgba(61,174,233,0.18); "
                                 "border-radius: 8px; padding: 8px 12px;")
        self.stale.setVisible(False)
        col.addWidget(self.stale)

        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(self._on_activated)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._menu)
        col.addWidget(self.files, 1)
        note = QLabel(_("srv_deb_hint"))
        note.setProperty("dim", True)
        col.addWidget(note)
        return outer

    def _fill_header(self):
        t = self.session.target
        self.head_title.setText(f'{t["user"]}@{t["host"]}'
                                + (f':{t["port"]}' if t["port"] != 22 else ""))
        try:
            fingerprint = self.remote.scan_host_key(t)[0]
        except self.remote.RemoteError:
            fingerprint = ""
        self.head_sub.setText(f'{t["root"]}   ·   {fingerprint}')

    def refresh(self):
        if not self.session:
            return
        self.crumb.setText(self.session.path(self.cwd))
        self.up_btn.setEnabled(bool(self.cwd))

        def done(entries, error):
            if isinstance(error, self.remote.NotConnected):
                self._lost()
                return
            if error:
                self.window.toast(str(error))
                return
            self.files.clear()
            if not entries:
                self.files.addItem(_("srv_empty_folder"))
            for entry in entries or []:
                glyph = "🔗" if entry["link"] else "📁" if entry["dir"] \
                    else "📦" if entry["name"].endswith(".deb") else "📄"
                when = _ago(entry["mtime"])
                detail = (f'{_("srv_link")}  ·  {when}' if entry["link"]
                          else when if entry["dir"]
                          else f'{human_size(entry["size"])}  ·  {when}')
                item = QListWidgetItem(f'{glyph}  {entry["name"]}\n'
                                       f'      {detail}')
                item.setData(Qt.UserRole, entry)
                self.files.addItem(item)

        session, cwd = self.session, self.cwd
        run_async(self, lambda: (session.ensure_root(),
                                 session.listdir(cwd))[1], done)

    def _on_activated(self, item):
        entry = item.data(Qt.UserRole)
        if isinstance(entry, dict) and entry["dir"]:
            self.cwd = entry["rel"]
            self.refresh()

    def _go_up(self):
        self.cwd = posixpath.dirname(self.cwd.rstrip("/"))
        self.refresh()

    def _menu(self, point):
        item = self.files.itemAt(point)
        entry = item.data(Qt.UserRole) if item else None
        if not isinstance(entry, dict):
            return
        menu = QMenu(self)
        menu.addAction(_("srv_rename_q"), lambda: self._rename(entry))
        if not entry["dir"]:
            menu.addAction(_("srv_download"), lambda: self._download(entry))
        menu.addSeparator()
        menu.addAction(_("srv_remove"), lambda: self._remove(entry))
        menu.exec(self.files.viewport().mapToGlobal(point))

    # -- changing the server ---------------------------------------------------------
    def _pick_uploads(self):
        paths, _sel = QFileDialog.getOpenFileNames(self, _("srv_upload"))
        self._upload(paths)

    def dragEnterEvent(self, event):
        if self.session and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if os.path.isfile(u.toLocalFile())]
        if paths:
            event.acceptProposedAction()
            self._upload(paths)

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
            if isinstance(error, self.remote.NotConnected):
                self._lost()
                return
            if error:
                self.window.toast(str(error))
            else:
                self.window.toast(_("srv_uploaded", n=len(paths)))
                if packages:
                    self.stale.setVisible(True)
            self.refresh()

        run_async(self, work, done)

    def _new_folder(self):
        name, ok = QInputDialog.getText(self, _("srv_new_folder"),
                                        _("srv_folder_name"))
        if ok and name.strip():
            run_async(self, lambda: self.session.mkdir(self.cwd, name.strip()),
                      lambda r, e: (self.window.toast(str(e)) if e else None,
                                    self.refresh()))

    def _rename(self, entry):
        name, ok = QInputDialog.getText(self, _("srv_rename_q"),
                                        _("srv_rename_q"), text=entry["name"])
        if ok and name.strip() and name.strip() != entry["name"]:
            run_async(self, lambda: self.session.rename(entry["rel"],
                                                        name.strip()),
                      lambda r, e: (self.window.toast(str(e)) if e else None,
                                    self.refresh()))

    def _download(self, entry):
        path, _sel = QFileDialog.getSaveFileName(self, _("srv_download"),
                                                 entry["name"])
        if path:
            run_async(self, lambda: self.session.download(entry["rel"], path),
                      lambda r, e: self.window.toast(
                          str(e) if e else _("srv_downloaded", p=path)))

    def _remove(self, entry):
        session = self.session

        def done(record, error):
            if error:
                self.window.toast(str(error))
                return
            self._undo.append(record)
            self.undo_btn.setEnabled(True)
            self.window.toast(_("srv_removed", n=1))
            if entry["name"].endswith(".deb"):
                self.stale.setVisible(True)
            self.refresh()

        run_async(self, lambda: session.trash([entry["rel"]]), done)

    def _undo_last(self):
        if not self._undo or not self.session:
            return
        record = self._undo.pop()
        self.undo_btn.setEnabled(bool(self._undo))
        run_async(self, lambda: self.session.restore(record),
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
        self.rebuild_btn.setEnabled(False)
        self.window.begin_operation(_("srv_rebuilding"))

        def done(_r, error):
            self.window.end_operation()
            self.rebuild_btn.setEnabled(True)
            if isinstance(error, self.remote.NotConnected):
                self._lost()
                return
            if error:
                self.window.toast(_("repo_build_failed", e=str(error)))
                return
            self.stale.setVisible(False)
            self.window.toast(_("srv_rebuilt"))
            self.refresh()

        run_async(self, lambda: self.session.rebuild_index(profile), done)

    # -- ending --------------------------------------------------------------------------
    def _lost(self):
        self.session = None
        self.browser.setVisible(False)
        self.setup.setVisible(True)
        self.window.toast(_("srv_closed"))

    def disconnect(self):
        session, self.session = self.session, None
        self.browser.setVisible(False)
        self.setup.setVisible(True)
        if session:
            run_async(self, session.disconnect, lambda r, e: None)

    def stop(self):
        if self.session:
            try:
                self.session.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.session = None


# --------------------------------------------------------------------------- #
# Repository — the APT archive: the pool, the index, and the copy on the server
# --------------------------------------------------------------------------- #
REPO_FILTERS = [("all", "repo_filter_all"),
                ("unindexed", "repo_filter_unindexed"),
                ("unsigned", "repo_filter_unsigned"),
                ("unpublished", "repo_filter_unpublished"),
                ("old", "repo_filter_old")]


def _ago(when):
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


class RepoPage(QWidget):
    """The same archive as the GNOME build, in Plasma's idiom.

    Three states are kept apart, because confusing them is how an archive
    quietly stops working: on disk (in the pool), indexed (the signed index
    mentions it) and live (the server is really serving it).
    """

    def __init__(self, window):
        super().__init__()
        from .. import repo
        self.repo = repo
        self.window = window
        self._entries = []
        self._status = {}
        self._live = None
        self._live_error = ""
        self._undo = []
        self.setAcceptDrops(True)

        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(12, 12, 12, 12)
        self._build()

    # -- construction ---------------------------------------------------------
    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def stop(self):
        """Closing Petacore or switching project ends the server session."""
        panel = getattr(self, "server_panel", None)
        if panel is not None:
            panel.stop()

    def _build(self):
        self._clear_layout(self.body)
        self.profile = self.repo.active_profile()
        if not self.profile:
            title = QLabel(_("repo_none_title"))
            title.setProperty("heading", True)
            body = QLabel(_("repo_none_body"))
            body.setWordWrap(True)
            body.setProperty("dim", True)
            create = QPushButton(_("repo_new"))
            create.setProperty("primary", True)
            create.clicked.connect(lambda: self._edit_profile(None))
            self.body.addStretch(1)
            self.body.addWidget(title)
            self.body.addWidget(body)
            self.body.addWidget(create)
            self.body.addStretch(1)
            return

        # Two views of the same repository: this machine's tree, and the
        # folder on the server.
        previous = getattr(self, "server_panel", None)
        if previous is not None:
            previous.stop()
        self.tabs = QTabWidget()
        local = QWidget()
        self.local_layout = QVBoxLayout(local)
        self.local_layout.setContentsMargins(0, 8, 0, 0)
        self.tabs.addTab(local, _("repo_local"))
        self.server_panel = RemotePanel(self.window, self)
        self.tabs.addTab(self.server_panel, _("repo_server"))
        self.tabs.setCurrentIndex(getattr(self, "_tab", 0))
        self.tabs.currentChanged.connect(
            lambda i: setattr(self, "_tab", i))
        self.body.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        self.repo_box = QComboBox()
        for profile in self.repo.profiles():
            self.repo_box.addItem(profile["name"], profile["name"])
            if profile["name"] == self.profile["name"]:
                self.repo_box.setCurrentIndex(self.repo_box.count() - 1)
        self.repo_box.currentIndexChanged.connect(self._on_repo_chosen)
        bar.addWidget(self.repo_box)

        settings = QPushButton(_("repo_settings"))
        settings.clicked.connect(lambda: self._edit_profile(self.profile))
        bar.addWidget(settings)
        bar.addStretch(1)

        add = QPushButton(_("repo_add"))
        add.clicked.connect(self._on_add_clicked)
        bar.addWidget(add)
        self.build_btn = QPushButton(_("repo_rebuild"))
        self.build_btn.setProperty("primary", True)
        self.build_btn.clicked.connect(self._on_rebuild)
        bar.addWidget(self.build_btn)
        self.publish_btn = QPushButton(_("repo_publish"))
        self.publish_btn.clicked.connect(self._on_publish)
        bar.addWidget(self.publish_btn)
        self.live_btn = QPushButton(_("repo_check_live"))
        self.live_btn.clicked.connect(self._on_check_live)
        bar.addWidget(self.live_btn)

        more = QPushButton("⋯")
        more.setToolTip(_("repo_settings"))
        more.clicked.connect(self._page_menu)
        bar.addWidget(more)
        self.local_layout.addLayout(bar)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setProperty("dim", True)
        self.local_layout.addWidget(self.summary)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setProperty("danger", True)
        self.banner.setVisible(False)
        self.local_layout.addWidget(self.banner)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(_("repo_search"))
        self.search.textChanged.connect(self._render)
        search_row.addWidget(self.search, 1)
        self.filter_box = QComboBox()
        self.filter_box.addItems([_(key) for _c, key in REPO_FILTERS])
        self.filter_box.currentIndexChanged.connect(self._render)
        search_row.addWidget(self.filter_box)
        self.undo_btn = QPushButton(_("repo_undo"))
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self.undo_last)
        search_row.addWidget(self.undo_btn)
        self.local_layout.addLayout(search_row)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemDoubleClicked.connect(
            lambda item: self._show_details(item.data(Qt.UserRole)))
        self.local_layout.addWidget(self.list, 1)

        QShortcut(QKeySequence(Qt.Key_Delete), self.list,
                  activated=lambda: self._on_remove(self._selected()))
        QShortcut(QKeySequence("F2"), self.list,
                  activated=self._rename_current)
        QShortcut(QKeySequence("Ctrl+Z"), self.list,
                  activated=self.undo_last)

        self.refresh()

    # -- reading the repository ------------------------------------------------
    def refresh(self):
        if not getattr(self, "profile", None):
            return
        profile = self.profile

        def work():
            return self.repo.scan(profile), self.repo.status(profile)

        def done(result, error):
            if error or not result:
                self._entries, self._status = [], {}
            else:
                self._entries, self._status = result
            self._render()
            self._render_summary()

        run_async(self, work, done)

    def _render_summary(self):
        state = self._status or {}
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
        self.summary.setText("  ·  ".join(p for p in parts if p))

        if not self.repo.can_index():
            message = _("repo_no_index_tool")
        elif state.get("key") and not state.get("key_present"):
            message = _("repo_key_missing")
        elif state.get("stale"):
            message = f'{_("repo_stale")} — {_("repo_stale_body")}'
        else:
            message = ""
        self.banner.setText(message)
        self.banner.setVisible(bool(message))

    # -- the list --------------------------------------------------------------
    def _live_state(self, entry):
        return self.repo.live_state(entry, self._live)

    def _badges(self, entry):
        if entry["broken"]:
            return [_("repo_badge_broken")]
        badges = []
        if not entry["indexed"]:
            badges.append(_("repo_badge_unindexed"))
        if not entry["signed"]:
            badges.append(_("repo_badge_unsigned"))
        state = self._live_state(entry)
        badges += {"live": [_("repo_badge_live")],
                   "new": [_("repo_badge_new")],
                   "ahead": [_("repo_badge_ahead")],
                   "behind": [_("repo_badge_behind")]}.get(state, [])
        return badges

    def _visible_entries(self):
        mode = REPO_FILTERS[self.filter_box.currentIndex()][0]
        return self.repo.filter_entries(self._entries, self.search.text(),
                                        mode, self._live)

    def _render(self):
        self.list.clear()
        shown = self._visible_entries()
        if not shown:
            self.list.addItem(f'{_("repo_no_packages")}\n     '
                              f'{_("repo_drop_hint")}')
            return
        for entry in shown:
            badges = self._badges(entry)
            suffix = ("   [" + "] [".join(badges) + "]") if badges else ""
            details = "  ·  ".join(
                [entry["arch"] or "—", human_size(entry["size"]),
                 entry["rel"]])
            item = QListWidgetItem(
                f'{entry["package"]}  {entry["version"]}{suffix}\n'
                f'     {details}')
            item.setData(Qt.UserRole, entry)
            self.list.addItem(item)

    def _selected(self):
        return [item.data(Qt.UserRole) for item in self.list.selectedItems()
                if isinstance(item.data(Qt.UserRole), dict)]

    # -- menus -----------------------------------------------------------------
    def _page_menu(self):
        menu = QMenu(self)
        menu.addAction(_("repo_instructions"), self._copy_instructions)
        menu.addAction(_("repo_export_keyring"), self._export_keyring)
        menu.addAction(_("repo_open_root"),
                       lambda: self._open(self.profile["root"]))
        menu.addSeparator()
        menu.addAction(_("repo_prune"), self._on_prune)
        trash = self._status.get("trash", 0)
        menu.addAction(f'{_("repo_empty_trash")} ({trash})'
                       if trash else _("repo_empty_trash"),
                       self._on_empty_trash)
        menu.addSeparator()
        menu.addAction(_("repo_new"), lambda: self._edit_profile(None))
        menu.addAction(_("repo_forget"), self._on_forget)
        menu.exec(self.cursor().pos())

    def _context_menu(self, point):
        chosen = self._selected()
        if not chosen:
            return
        menu = QMenu(self)
        if len(chosen) == 1:
            entry = chosen[0]
            menu.addAction(_("repo_details"),
                           lambda: self._show_details(entry))
            menu.addAction(_("repo_rename"), lambda: self._rename(entry))
            menu.addAction(_("repo_copy_install"), lambda: self._copy(
                self.repo.install_command(entry["package"])))
            menu.addAction(_("repo_copy_path"),
                           lambda: self._copy(entry["path"]))
            menu.addAction(_("repo_verify"), lambda: self._verify(entry))
            menu.addAction(_("repo_open_folder"),
                           lambda: self._open(os.path.dirname(entry["path"])))
            menu.addSeparator()
        menu.addAction(_("repo_remove"), lambda: self._on_remove(chosen))
        menu.exec(self.list.viewport().mapToGlobal(point))

    # -- adding ----------------------------------------------------------------
    def _on_add_clicked(self):
        paths, _sel = QFileDialog.getOpenFileNames(
            self, _("repo_add"), "", "Debian package (*.deb)")
        self.add_paths(paths)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.toLocalFile().endswith(".deb")]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def add_paths(self, paths):
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
                self.window.toast(_("repo_add_failed", n=len(failures)))
            self.refresh()

        run_async(self, lambda: self.repo.add_packages(profile, paths), done)

    # -- removing --------------------------------------------------------------
    def _on_remove(self, entries):
        if not entries:
            return
        if QMessageBox.question(self, _("repo_remove_q", n=len(entries)),
                                _("repo_remove_detail")) != QMessageBox.Yes:
            return
        profile = self.profile
        paths = [e["path"] for e in entries]

        def done(record, error):
            if error:
                self.window.toast(str(error))
                return
            self._undo.append(record)
            del self._undo[:-10]
            self.undo_btn.setEnabled(True)
            self.window.toast(_("repo_removed", n=len(record["items"])))
            self.refresh()

        run_async(self, lambda: self.repo.remove_packages(profile, paths),
                  done)

    def undo_last(self):
        if not self._undo:
            return
        record = self._undo.pop()
        self.undo_btn.setEnabled(bool(self._undo))

        def done(restored, error):
            self.window.toast(str(error) if error
                              else _("repo_restored", n=len(restored or [])))
            self.refresh()

        run_async(self, lambda: self.repo.restore_removed(record), done)

    # -- renaming --------------------------------------------------------------
    def _rename_current(self):
        chosen = self._selected()
        if len(chosen) == 1:
            self._rename(chosen[0])

    def _rename(self, entry):
        name, ok = QInputDialog.getText(self, _("repo_rename_q"),
                                        _("repo_rename_hint"),
                                        text=entry["file"])
        if not ok or not name.strip() or name.strip() == entry["file"]:
            return
        try:
            path = self.repo.rename_package(entry["path"], name.strip())
        except (self.repo.RepoError, OSError) as e:
            self.window.toast(str(e))
            return
        self.window.toast(_("repo_renamed", n=os.path.basename(path)))
        self.refresh()

    # -- details, verification, clipboard --------------------------------------
    def _show_details(self, entry):
        if not isinstance(entry, dict):
            return

        def done(fields, _error):
            lines = [f"{key}: {value}"
                     for key, value in (fields or {}).items()]
            lines.append("")
            lines.append(entry["path"])
            QMessageBox.information(self, _("repo_details"), "\n".join(lines))

        def work():
            try:
                return self.repo.package_fields(entry["path"])
            except self.repo.RepoError as e:
                return {"Error": str(e)}

        run_async(self, work, done)

    def _verify(self, entry):
        self.window.begin_operation(_("repo_verify"))

        def done(valid, error):
            self.window.end_operation()
            self.window.toast(str(error) if error else
                              (_("repo_verify_ok") if valid
                               else _("repo_verify_bad")))

        run_async(self, lambda: self.repo.verify(entry["path"]), done)

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.window.toast(_("repo_copied"))

    def _copy_instructions(self):
        self._copy(self.repo.install_instructions(self.profile))

    def _open(self, path):
        subprocess.Popen(["xdg-open", path])

    def _export_keyring(self):
        profile = self.profile
        if not profile["key"]:
            self.window.toast(_("repo_key_hint"))
            return
        run_async(self, lambda: self.repo.export_key(
            profile["root"], profile["key"],
            self.repo.keyring_filename(profile)),
            lambda p, e: self.window.toast(str(e) if e
                                           else _("pubkey_done", p=p)))

    # -- the index -------------------------------------------------------------
    def _on_rebuild(self):
        profile = self.profile
        if not profile["key"]:
            self.window.toast(_("repo_key_hint"))
            self._edit_profile(profile)
            return
        self.build_btn.setEnabled(False)
        self.window.begin_operation(_("repo_building"))

        def done(_result, error):
            self.window.end_operation()
            self.build_btn.setEnabled(True)
            self.window.toast(_("repo_build_failed", e=str(error)) if error
                              else _("repo_built"))
            self.refresh()

        run_async(self, lambda: self.repo.build(profile), done)

    # -- publishing ------------------------------------------------------------
    def _on_publish(self):
        profile = self.profile
        if profile["publish_method"] == "none":
            self._edit_profile(profile)
            return
        self.publish_btn.setEnabled(False)

        def done(changes, error):
            self.publish_btn.setEnabled(True)
            if error:
                self.window.toast(_("repo_publish_failed", e=str(error)))
                return
            if not changes:
                self.window.toast(_("repo_publish_none"))
                return
            preview = "\n".join(changes[:14])
            if len(changes) > 14:
                preview += f"\n… {len(changes) - 14}"
            box = QMessageBox(self)
            box.setWindowTitle(_("repo_publish_q", n=len(changes)))
            box.setText(f'{profile["publish_target"]}\n\n'
                        f'{_("repo_publish_detail")}')
            box.setDetailedText(preview)
            box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
            if box.exec() != QMessageBox.Ok:
                return
            self._do_publish()

        run_async(self, lambda: self.repo.publish_preview(profile), done)

    def _do_publish(self):
        profile = self.profile
        self.publish_btn.setEnabled(False)
        self.window.begin_operation(_("repo_publishing"))

        def done(result, error):
            self.window.end_operation()
            self.publish_btn.setEnabled(True)
            if error:
                self.window.toast(_("repo_publish_failed", e=str(error)))
                return
            self.window.toast(_("repo_published",
                                n=(result or {}).get("files", 0)))
            self._on_check_live()

        run_async(self, lambda: self.repo.publish(profile), done)

    # -- the copy on the server ------------------------------------------------
    def _on_check_live(self):
        profile = self.profile
        if not profile["base_url"]:
            self._edit_profile(profile)
            return
        self.live_btn.setEnabled(False)

        def done(result, error):
            self.live_btn.setEnabled(True)
            if error:
                self._live, self._live_error = None, str(error)
            else:
                self._live = {name: info["version"]
                              for name, info in (result or {}).items()}
                self._live_error = ""
            self._render()
            self._render_summary()

        run_async(self, lambda: self.repo.remote_packages(profile), done)

    # -- housekeeping ----------------------------------------------------------
    def _on_prune(self):
        if QMessageBox.question(self, _("repo_prune_q"),
                                _("repo_prune_detail")) != QMessageBox.Yes:
            return
        profile = self.profile

        def done(record, error):
            if error:
                self.window.toast(str(error))
                return
            if record["items"]:
                self._undo.append(record)
                self.undo_btn.setEnabled(True)
            self.window.toast(_("repo_removed", n=len(record["items"])))
            self.refresh()

        run_async(self, lambda: self.repo.prune(profile, keep=1), done)

    def _on_empty_trash(self):
        count = self._status.get("trash", 0)
        if not count:
            return
        if QMessageBox.question(self, _("repo_empty_trash_q", n=count),
                                self.profile["root"]) != QMessageBox.Yes:
            return
        profile = self.profile
        # Emptying the trash is the one thing here that cannot be undone, so
        # the undo stack goes with it rather than pointing at files that are
        # no longer there.
        self._undo.clear()
        self.undo_btn.setEnabled(False)

        def done(removed, error):
            self.window.toast(str(error) if error
                              else _("repo_trash_emptied", n=removed or 0))
            self.refresh()

        run_async(self, lambda: self.repo.empty_trash(profile), done)

    def _on_forget(self):
        if QMessageBox.question(self, _("repo_forget_q"),
                                _("repo_forget_detail")) != QMessageBox.Yes:
            return
        self.repo.remove_profile(self.profile["name"])
        self._live, self._live_error = None, ""
        self._undo.clear()
        self._build()

    # -- profiles --------------------------------------------------------------
    def _on_repo_chosen(self, index):
        name = self.repo_box.itemData(index)
        if name and name != self.profile["name"]:
            self.repo.set_active(name)
            self._live, self._live_error = None, ""
            self._undo.clear()
            self._build()

    def _edit_profile(self, profile):
        from .dialogs import RepoSettingsDialog
        dialog = RepoSettingsDialog(self, profile)
        if dialog.exec():
            self._live, self._live_error = None, ""
            self._build()
            self.window.toast(_("repo_created"))


# --------------------------------------------------------------------------- #
# Virus Scan — the whole project, checked by VirusTotal's engines
# --------------------------------------------------------------------------- #
VT_COLORS = {"clean": "#27ae60", "flagged": "#da4453", "unknown": "#3daee9"}

_VT_QSS = """
QFrame#pcVtCard { border-radius: 18px; border: 1px solid rgba(127,127,127,0.18); }
QLabel#pcVtBig { font-size: 30pt; font-weight: 800; }
QLabel#pcVtKicker { font-size: 8pt; font-weight: 800; letter-spacing: 1.5px; }
QLabel#pcVtVerdict { font-size: 13pt; font-weight: 700; }
QLabel[vtchip="true"] { border-radius: 7px; padding: 2px 9px; font-size: 8.5pt;
                        background: rgba(127,127,127,0.16); }
QLabel[vtchip="warn"] { border-radius: 7px; padding: 2px 9px; font-size: 8.5pt;
                        background: rgba(246,116,0,0.16); color: #f67400; }
QFrame#pcVtEngines { border-radius: 12px; background: rgba(127,127,127,0.08); }
"""


class VirusScanPage(QWidget):
    """One button: check the whole project with VirusTotal. Mirrors the
    GNOME page — see there for why it is one archive, and when anything is
    uploaded."""

    def __init__(self, window):
        super().__init__()
        from .. import virustotal
        self.vt = virustotal
        self.window = window
        self.busy = False
        self._last = None
        self.setStyleSheet(_VT_QSS)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self._build()

    # -- construction ---------------------------------------------------------
    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _build(self):
        self._clear_layout(self.body)
        if not self.vt.key_present():
            self._build_no_key()
            return

        head = QHBoxLayout()
        title = QLabel(_("vt_project_title"))
        title.setProperty("title", True)
        head.addWidget(title, 1)
        menu = QPushButton("⋯")
        menu.setFixedWidth(40)
        menu.clicked.connect(self._menu)
        head.addWidget(menu)
        self.body.addLayout(head)
        hint = QLabel(_("vt_project_hint"))
        hint.setWordWrap(True)
        hint.setProperty("dim", True)
        self.body.addWidget(hint)

        self.card_holder = QVBoxLayout()
        self.body.addLayout(self.card_holder)

        row = QHBoxLayout()
        row.addStretch(1)
        self.scan_btn = QPushButton(_("vt_scan_project"))
        self.scan_btn.setProperty("primary", True)
        self.scan_btn.clicked.connect(lambda: self._scan())
        row.addWidget(self.scan_btn)
        row.addStretch(1)
        self.body.addLayout(row)
        self.stage = QLabel()
        self.stage.setAlignment(Qt.AlignCenter)
        self.stage.setProperty("dim", True)
        self.body.addWidget(self.stage)
        quota = QLabel(_("vt_quota"))
        quota.setAlignment(Qt.AlignCenter)
        quota.setProperty("dim", True)
        self.body.addWidget(quota)
        self.body.addStretch(1)
        self.refresh()

    def _build_no_key(self):
        self.body.addStretch(1)
        title = QLabel(_("vt_no_key_title"))
        title.setProperty("title", True)
        title.setAlignment(Qt.AlignCenter)
        self.body.addWidget(title)
        desc = QLabel(_("vt_no_key_body"))
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignCenter)
        desc.setProperty("dim", True)
        self.body.addWidget(desc)
        row = QHBoxLayout()
        row.addStretch(1)
        enter = QPushButton(_("vt_set_key"))
        enter.setProperty("primary", True)
        enter.clicked.connect(self._ask_key)
        row.addWidget(enter)
        get = QPushButton(_("vt_get_key"))
        get.clicked.connect(
            lambda: subprocess.Popen(["xdg-open", self.vt.KEY_PAGE]))
        row.addWidget(get)
        row.addStretch(1)
        self.body.addLayout(row)
        quota = QLabel(_("vt_quota"))
        quota.setAlignment(Qt.AlignCenter)
        quota.setProperty("dim", True)
        self.body.addWidget(quota)
        self.body.addStretch(1)

    def _menu(self):
        menu = QMenu(self)
        menu.addAction(_("vt_ask_again"), self._forget_consent)
        menu.addAction(_("vt_change_key"), self._ask_key)
        menu.addAction(_("vt_forget_key"), self._forget_key)
        menu.exec(self.cursor().pos())

    # -- the result card ----------------------------------------------------------
    def refresh(self):
        if not self.vt.key_present():
            self._build()
            return
        if not hasattr(self, "card_holder"):
            return
        project = config.active_project()
        if not project:
            return
        self._clear_layout(self.card_holder)
        state = self.vt.load_state(project["path"])
        summary = state.get("summary")
        self.card_holder.addWidget(self._card(project, state, summary))
        self.scan_btn.setText(_("vt_scan_again") if summary
                              else _("vt_scan_project"))
        self.scan_btn.setEnabled(not self.busy)

    def _card(self, project, state, summary):
        from .. import debbuild
        verdict = summary["verdict"] if summary else "unknown"
        tint = QColor(VT_COLORS[verdict])
        card = QFrame()
        card.setObjectName("pcVtCard")
        alpha = 30 if not summary else 70
        card.setStyleSheet(
            "QFrame#pcVtCard { background: qlineargradient(x1:0, y1:0, x2:1, "
            f"y2:1, stop:0 rgba({tint.red()},{tint.green()},{tint.blue()},{alpha}), "
            f"stop:0.7 rgba({tint.red()},{tint.green()},{tint.blue()},8)); }}")
        box = QVBoxLayout(card)
        box.setContentsMargins(22, 18, 22, 18)
        box.setSpacing(8)

        top = QHBoxLayout()
        kicker = QLabel(project["name"].upper())
        kicker.setObjectName("pcVtKicker")
        kicker.setProperty("dim", True)
        top.addWidget(kicker)
        top.addStretch(1)
        if state.get("scanned"):
            when = QLabel(_("vt_last_scan", t=_ago(state["scanned"])))
            when.setProperty("dim", True)
            top.addWidget(when)
        box.addLayout(top)

        if not summary:
            big = QLabel(_("vt_never_scanned"))
            big.setObjectName("pcVtBig")
            box.addWidget(big)
            return card

        flagged = summary["malicious"] + summary["suspicious"]
        big = QLabel(("⚠  " if verdict == "flagged" else "🛡  ")
                     + (f'{flagged} / {summary["engines"]}'
                        if summary["engines"] else "?"))
        big.setObjectName("pcVtBig")
        box.addWidget(big)
        line = QLabel(_("vt_verdict_clean") if verdict == "clean"
                      else _("vt_verdict_flagged", n=flagged,
                             t=summary["engines"])
                      if verdict == "flagged" else _("vt_verdict_unknown"))
        line.setObjectName("pcVtVerdict")
        line.setWordWrap(True)
        box.addWidget(line)

        chips = QHBoxLayout()
        if state.get("files"):
            chip = QLabel(_("vt_archive_info", n=state["files"],
                            s=human_size(state.get("size", 0))))
            chip.setProperty("vtchip", "true")
            chips.addWidget(_snug(chip), 0, Qt.AlignVCenter)
        left_out = (self._last or {}).get("left_out") or \
            debbuild.find_secrets(project["path"])
        if left_out:
            chip = QLabel(_("vt_left_out", n=len(left_out)))
            chip.setProperty("vtchip", "warn")
            chip.setToolTip("\n".join(left_out[:12]))
            chips.addWidget(_snug(chip), 0, Qt.AlignVCenter)
        chips.addStretch(1)
        if summary.get("permalink"):
            report = QPushButton(_("vt_open_report"))
            report.setProperty("pcsmall", True)
            report.clicked.connect(lambda: subprocess.Popen(
                ["xdg-open", summary["permalink"]]))
            chips.addWidget(report)
        box.addLayout(chips)

        if summary["flagged"]:
            engines = QFrame()
            engines.setObjectName("pcVtEngines")
            inner = QVBoxLayout(engines)
            inner.setContentsMargins(14, 12, 14, 12)
            heading = QLabel(_("vt_engines_flagged"))
            heading.setStyleSheet("font-weight: 700;")
            inner.addWidget(heading)
            for engine, result in summary["flagged"][:16]:
                row = QLabel(f"<b>{engine}</b>&nbsp;&nbsp;{result}")
                row.setTextInteractionFlags(Qt.TextSelectableByMouse)
                inner.addWidget(row)
            note = QLabel(_("vt_false_positive"))
            note.setWordWrap(True)
            note.setProperty("dim", True)
            inner.addWidget(note)
            box.addWidget(engines)

        if self._last and not self._last.get("uploaded") \
                and not self._last.get("needs_upload"):
            reused = QLabel(_("vt_reused"))
            reused.setProperty("dim", True)
            box.addWidget(reused)
        return card

    # -- scanning ---------------------------------------------------------------
    def _scan(self, allow_upload=None):
        project = config.active_project()
        if not project or self.busy:
            return
        if allow_upload is None:
            allow_upload = bool(self.vt.load_state(project["path"])
                                .get("consent"))
        self._set_busy(True)
        self.stage.setText(_("vt_stage_packing"))

        def done(result, error):
            self._set_busy(False)
            self.stage.setText("")
            if error:
                self.window.toast(str(error))
                return
            self._last = result
            if result["needs_upload"]:
                self._ask_consent(project, result)
                return
            self.refresh()

        # Stage messages are left to the start and end here: the worker
        # thread must not touch widgets, and the whole scan is usually
        # a matter of seconds when nothing needs uploading.
        run_async(self, lambda: self.vt.scan_project(project["path"],
                                                     allow_upload), done)

    def _set_busy(self, busy):
        self.busy = busy
        if hasattr(self, "scan_btn"):
            self.scan_btn.setEnabled(not busy)
        if busy:
            self.window.begin_operation(_("vt_checking"))
        else:
            self.window.end_operation()

    def _ask_consent(self, project, result):
        box = QMessageBox(self)
        box.setWindowTitle(_("vt_consent_q"))
        box.setText(_("vt_consent_body", n=result["files"],
                      s=human_size(result["size"])))
        remember = QCheckBox(_("vt_consent_remember"))
        box.setCheckBox(remember)
        box.setStandardButtons(QMessageBox.Cancel)
        send = box.addButton(_("vt_upload_go"), QMessageBox.AcceptRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not send:
            return
        if remember.isChecked():
            self.vt.save_state(project["path"], consent=True)
        self.stage.setText(_("vt_stage_uploading"))
        self._scan(allow_upload=True)

    def _forget_consent(self):
        project = config.active_project()
        if project:
            self.vt.save_state(project["path"], consent=False)
            self.window.toast(_("vt_ask_again"))

    # -- the key ------------------------------------------------------------------
    def _ask_key(self):
        key, ok = QInputDialog.getText(self, _("vt_set_key"),
                                       _("vt_no_key_body"),
                                       QLineEdit.Password)
        if not ok or not key.strip():
            return
        key = key.strip()

        def work():
            problem = self.vt.check_key(key)
            if problem:
                return f"refused:{problem}"
            return "stored" if self.vt.set_key(key) else "nowhere"

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
                                  if self.vt.why_unstored() == "no-tool"
                                  else _("vt_key_nowhere"))
                return
            self.window.toast(_("vt_key_saved", s=self.vt.storage_kind()))
            self._build()

        run_async(self, work, done)

    def _forget_key(self):
        self.vt.clear_key()
        self.window.toast(_("vt_key_forgotten"))
        self._build()


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
