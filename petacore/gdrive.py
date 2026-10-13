"""Google Drive synchronization for Petacore, built on rclone.

Each user connects their own Google Drive account (browser OAuth handled by
rclone). Petacore creates a "Petacomm Petacore™" folder in their Drive and
mirrors every project into its own subfolder — source code and media files
included (.git and other noise excluded).
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

REMOTE = "petacore-gdrive"
DRIVE_FOLDER = "Petacomm Petacore™"
# Petacore's own corner of the Drive folder: backups and the note saying
# which computer sent each project last. The dot keeps it out of the way
# and out of the project list.
SYSTEM_FOLDER = ".petacore"
# Where earlier versions kept replaced files. Its contents are moved into
# the backups the first time a project is synced again, and it goes away.
LEGACY_ATTIC = "_Replaced files"
ATTIC_FOLDER = LEGACY_ATTIC          # kept for older translations
EXCLUDES = [".git/**", ".petacore/**", "__pycache__/**",
            "node_modules/**", ".venv/**"]
# Documents Petacore keeps next to a project on Drive only (Next Updates,
# public keys). A sync neither downloads them nor treats them as removed.
DRIVE_ONLY = ["/petacore-updates.json", "/petacore-public-keys.json"]


class DriveError(Exception):
    pass


def _run(args, timeout=600):
    try:
        proc = subprocess.run(["rclone"] + args, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise DriveError("rclone-missing")
    except subprocess.TimeoutExpired:
        raise DriveError("rclone timed out")
    if proc.returncode != 0:
        raise DriveError((proc.stderr or proc.stdout or "rclone failed")
                         .strip()[-400:])
    return proc.stdout


def available() -> bool:
    return shutil.which("rclone") is not None


def connected() -> bool:
    if not available():
        return False
    try:
        return f"{REMOTE}:" in _run(["listremotes"], timeout=15)
    except DriveError:
        return False


def connect():
    """Start the Google sign-in: rclone opens the browser, the user clicks
    'Allow', and the remote is saved. Blocks until finished."""
    _run(["config", "create", REMOTE, "drive", "scope=drive"], timeout=600)


def disconnect():
    _run(["config", "delete", REMOTE], timeout=30)


def remote_size(project_name: str) -> int:
    """Total bytes the project currently occupies in Drive.

    Returns -1 when the size cannot be established (no such folder yet, or
    rclone could not answer), so callers can tell "nothing there" apart from
    "genuinely empty".
    """
    import json
    target = f"{REMOTE}:{DRIVE_FOLDER}/{project_name}"
    try:
        output = _run(["size", target, "--json"], timeout=120)
        return int(json.loads(output).get("bytes", -1))
    except (DriveError, ValueError, TypeError):
        return -1


def local_size(project_path: str) -> int:
    """Bytes the project occupies here, counting only what would be
    uploaded — the same exclusions the sync applies, so the two figures
    describe the same thing."""
    import fnmatch
    skip_dirs = {pattern.split("/")[0] for pattern in EXCLUDES}
    total = 0
    for base, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                continue
    return total


def _filters():
    args = []
    for pattern in EXCLUDES + DRIVE_ONLY:
        args += ["--exclude", pattern]
    return args


def _stream(args, progress):
    """Run rclone, reporting 0–100 to `progress` as it goes."""
    import re
    args = args + ["--stats", "500ms", "--stats-one-line", "-v"]
    try:
        proc = subprocess.Popen(["rclone"] + args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise DriveError("rclone-missing")
    tail = []
    pct_re = re.compile(r"(\d{1,3})%")
    for line in proc.stderr:
        tail.append(line.strip())
        del tail[:-8]
        m = pct_re.search(line)
        if m:
            try:
                progress(min(100, int(m.group(1))))
            except Exception:
                pass
    proc.wait(timeout=60)
    if proc.returncode != 0:
        raise DriveError("\n".join(tail)[-400:] or "rclone failed")
    progress(100)


def _transfer(args, progress):
    if progress is None:
        _run(args, timeout=3600)
    else:
        _stream(args, progress)


def sync_project(project_path: str, project_name: str, progress=None) -> str:
    """Send the project to Drive:  Petacomm Petacore™/<name>/ …

    Drive is made to match this computer, but nothing on Drive is ever
    destroyed: a file the upload replaces or removes is moved into the
    project's backups first, where Petacore lists it and can put it back.
    Afterwards Drive notes that this computer sent the latest copy, which
    is what lets the others bring it in on their own.
    """
    target = f"{REMOTE}:{DRIVE_FOLDER}/{project_name}"
    backup = f"{_drive_backups(project_name)}/{_stamp()} · {_host()}"
    args = ["sync", project_path, target, "--create-empty-src-dirs",
            "--backup-dir", backup] + _filters()
    _transfer(args, progress)
    _after_transfer(project_path, project_name, sent=True)
    return f"{DRIVE_FOLDER}/{project_name}"


def account_email() -> str:
    """The Gmail address of the connected account ('' when unknown)."""
    import json
    # 1) rclone's own userinfo, when the backend supports it
    try:
        out = _run(["config", "userinfo", f"{REMOTE}:", "--json"], timeout=30)
        data = json.loads(out)
        for value in data.values():
            if isinstance(value, str) and "@" in value:
                return value
    except (DriveError, ValueError):
        pass
    # 2) Drive API about?fields=user with the stored token
    try:
        out = _run(["config", "dump"], timeout=15)
        token_raw = json.loads(out).get(REMOTE, {}).get("token", "")
        access = json.loads(token_raw).get("access_token", "")
        if access:
            import urllib.request
            req = urllib.request.Request(
                "https://www.googleapis.com/drive/v3/about?fields=user",
                headers={"Authorization": f"Bearer {access}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                info = json.loads(resp.read().decode("utf-8"))
            return info.get("user", {}).get("emailAddress", "")
    except Exception:
        pass
    return ""


def list_remote_projects():
    """Folder names under 'Petacomm Petacore™' in the connected Drive."""
    import json
    try:
        out = _run(["lsjson", f"{REMOTE}:{DRIVE_FOLDER}", "--dirs-only"],
                   timeout=120)
        return sorted(e["Name"] for e in json.loads(out)
                      if e.get("IsDir") and e.get("Name")
                      and not e["Name"].startswith((".", "_")))
    except (DriveError, ValueError):
        return []


def pull_project(project_name: str, local_path: str, progress=None,
                 reason: str = "pull"):
    """Bring the Drive copy of a project into a local folder.

    Files this computer has but Drive does not, and files Drive has newer,
    are handled the same careful way as on upload: whatever is replaced or
    removed here is first moved into this computer's backups. .git and the
    other excluded folders are left exactly as they are.
    """
    source = f"{REMOTE}:{DRIVE_FOLDER}/{project_name}"
    state = remote_state(project_name)
    stamp = _stamp()
    backup = os.path.join(_local_backups(local_path), stamp)
    os.makedirs(local_path, exist_ok=True)
    args = ["sync", source, local_path, "--create-empty-src-dirs",
            "--backup-dir", backup] + _filters()
    _transfer(args, progress)
    if os.path.isdir(backup):
        _write_sidecar(backup, {
            "kind": reason,
            "from": (state or {}).get("host", ""),
            "time": time.time()})
    _after_transfer(local_path, project_name, sent=False, state=state)
    return state or {}


def note_synced(project_path, state):
    """For a project registered only after it was pulled: remember that it
    matches Drive now, so it is not taken for changed-here later."""
    from .config import config
    if state and state.get("rev"):
        config.set_project_field(project_path, "drive_rev", state["rev"])
    config.set_project_field(project_path, "drive_synced_at", time.time())
    config.set_project_field(project_path, "drive_files",
                             file_set(project_path))


# --------------------------------------------------------------------------- #
# Small JSON documents (Next Updates plans, public keys) stored alongside the
# project inside "Petacomm Petacore™/<project>/".
# --------------------------------------------------------------------------- #
def put_json(project_name: str, filename: str, payload) -> str:
    """Upload a small JSON document next to the project on Drive."""
    import json
    import tempfile
    target = f"{REMOTE}:{DRIVE_FOLDER}/{project_name}/{filename}"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        local = f.name
    try:
        _run(["copyto", local, target], timeout=300)
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
    return target


def get_json(project_name: str, filename: str):
    """Download a small JSON document, or None when it isn't there yet."""
    import json
    import tempfile
    source = f"{REMOTE}:{DRIVE_FOLDER}/{project_name}/{filename}"
    dest_dir = tempfile.mkdtemp(prefix="petacore-drive-")
    local = os.path.join(dest_dir, filename)
    try:
        _run(["copyto", source, local], timeout=300)
        with open(local, "r", encoding="utf-8") as f:
            return json.load(f)
    except (DriveError, OSError, ValueError):
        return None
    finally:
        shutil.rmtree(dest_dir, ignore_errors=True)


def push_public_keys(project_name: str):
    """Back up the *public* halves of the signing keys so other machines can
    verify packages. Private keys are deliberately never uploaded."""
    from . import gpgsign
    keys = gpgsign.list_keys_detailed()
    exported = []
    for key in keys:
        try:
            armored = _export_public_armored(key["fpr"])
        except Exception:
            continue
        exported.append({
            "uid": key["uid"],
            "fingerprint": key["fpr"],
            "algo": key["algo"],
            "bits": key["bits"],
            "created": key["created"],
            "expires": key["expires"],
            "public_key": armored,
        })
    put_json(project_name, "petacore-public-keys.json",
             {"version": 1, "keys": exported})
    return len(exported)


def _export_public_armored(fingerprint: str) -> str:
    import subprocess as sp
    proc = sp.run(["gpg", "--batch", "--armor", "--export", fingerprint],
                  capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or "BEGIN PGP PUBLIC KEY BLOCK" not in proc.stdout:
        raise DriveError("could not export public key")
    return proc.stdout


# --------------------------------------------------------------------------- #
# Which computer sent what: lets the others bring changes in on their own
# --------------------------------------------------------------------------- #
def machine_id() -> str:
    from .config import config
    mid = config.get("machine_id")
    if not mid:
        mid = uuid.uuid4().hex
        config.set("machine_id", mid)
    return mid


def _host() -> str:
    """This computer's name as people know it, made safe for a folder."""
    name = socket.gethostname().split(".")[0] or "computer"
    return re.sub(r"[^\w .-]+", "", name)[:40] or "computer"


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H.%M.%S")


def _state_path(project_name):
    return f"{REMOTE}:{DRIVE_FOLDER}/{SYSTEM_FOLDER}/state/{project_name}.json"


def _put_file(text, target):
    fd, local = tempfile.mkstemp(prefix="petacore-drive-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        _run(["copyto", local, target], timeout=120)
    finally:
        try:
            os.remove(local)
        except OSError:
            pass


def remote_state(project_name):
    """Who sent the project to Drive last, and when — None if unknown."""
    try:
        text = _run(["cat", _state_path(project_name)], timeout=60)
        state = json.loads(text)
        return state if isinstance(state, dict) and state.get("rev") else None
    except (DriveError, ValueError):
        return None


def _project_entry(project_path):
    from .config import config
    for p in config.get("projects"):
        if p.get("path") == project_path:
            return p
    return None


def _after_transfer(project_path, project_name, sent, state=None):
    """Remember where this computer stands relative to Drive, write the
    note for the others after an upload, and tidy old backups."""
    from .config import config
    if sent:
        state = {"rev": uuid.uuid4().hex, "machine": machine_id(),
                 "host": _host(), "time": time.time()}
        try:
            _put_file(json.dumps(state), _state_path(project_name))
        except DriveError:
            state = None
        try:
            _absorb_legacy(project_name)
        except DriveError:
            pass
    if _project_entry(project_path) is not None:
        if state:
            config.set_project_field(project_path, "drive_rev", state["rev"])
        config.set_project_field(project_path, "drive_synced_at", time.time())
        config.set_project_field(project_path, "drive_files",
                                 file_set(project_path))
    try:
        prune_backups(project_path, project_name)
    except (DriveError, OSError):
        pass


def _walk(project_path):
    """(relative path, mtime) of every file a sync would carry."""
    skip = {pattern.split("/")[0] for pattern in EXCLUDES}
    for base, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, project_path)
            if "/" + rel in DRIVE_ONLY:
                continue
            try:
                yield rel, os.lstat(full).st_mtime
            except OSError:
                continue


def file_set(project_path) -> str:
    """A short fingerprint of which files the project has — a file added
    or deleted here changes it, an edit does not (times catch those)."""
    names = sorted(rel for rel, _m in _walk(project_path))
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:24]


def changed_here_since(project_path, since, files=None) -> bool:
    """Has anything in the project changed on this computer after `since`?
    An edited file shows by its time; an added or deleted one by the file
    list no longer matching the one noted at the last sync."""
    if files and file_set(project_path) != files:
        return True
    return any(mtime > since + 2 for _rel, mtime in _walk(project_path))


def status(project):
    """Where a project stands against Drive:

      ("same", state)      nothing new on Drive
      ("pull", state)      another computer sent a newer copy; nothing has
                           changed here since, so it can simply come in
      ("conflict", state)  a newer copy on Drive *and* changes here
      ("none", None)       Drive has no note for it (never sent, or sent by
                           an older Petacore)
    """
    state = remote_state(project["name"])
    if not state:
        return "none", None
    if state.get("rev") == project.get("drive_rev"):
        return "same", state
    if state.get("machine") == machine_id():
        return "same", state             # our own upload, noted elsewhere
    path = project.get("path", "")
    if not os.path.isdir(path):
        return "pull", state
    since = project.get("drive_synced_at") or state.get("time") or 0
    if changed_here_since(path, since, project.get("drive_files")):
        return "conflict", state
    return "pull", state


# --------------------------------------------------------------------------- #
# Backups: every file a sync replaced or removed, on both sides
# --------------------------------------------------------------------------- #
from .config import DATA_DIR  # noqa: E402

LOCAL_BACKUPS = os.path.join(DATA_DIR, "drive-backups")
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})(?: (\d{2})\.(\d{2})\.(\d{2}))?"
                    r"(?: · (.+))?$")


def _drive_backups(project_name):
    return f"{REMOTE}:{DRIVE_FOLDER}/{SYSTEM_FOLDER}/backups/{project_name}"


def _local_backups(project_path):
    real = os.path.realpath(project_path)
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.basename(real)).strip("-")
    digest = hashlib.sha1(real.encode("utf-8")).hexdigest()[:8]
    return os.path.join(LOCAL_BACKUPS, f"{base or 'project'}-{digest}")


def _write_sidecar(folder, data):
    with open(folder + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f)


def _stamp_time(name):
    match = _STAMP.match(name)
    if not match:
        return None
    day, hh, mm, ss = match.group(1, 2, 3, 4)
    try:
        when = datetime.datetime.strptime(
            f"{day} {hh or '00'}:{mm or '00'}:{ss or '00'}",
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return when.timestamp()


def list_backups(project_path, project_name, include_drive=True):
    """Every backup of this project, newest first. Each is a dict:

      where   "local" (kept on this computer) or "drive"
      id      its folder name
      time    when it was made
      kind    "push"    — an upload to Drive replaced these
              "pull"    — bringing Drive's copy in replaced these
              "restore" — these were here before a backup was put back
      host    the computer involved
      files   [(relative path, size)]
    """
    found = []
    root = _local_backups(project_path)
    if os.path.isdir(root):
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            meta = {}
            try:
                with open(folder + ".json", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                pass
            files = []
            for base, _dirs, names in os.walk(folder):
                for n in names:
                    full = os.path.join(base, n)
                    files.append((os.path.relpath(full, folder),
                                  os.path.getsize(full)))
            if not files:
                continue
            found.append({"where": "local", "id": name,
                          "time": meta.get("time") or _stamp_time(name) or 0,
                          "kind": meta.get("kind", "pull"),
                          "host": meta.get("from", ""),
                          "files": sorted(files)})
    if include_drive:
        try:
            out = _run(["lsjson", "-R", "--files-only",
                        _drive_backups(project_name)], timeout=120)
            grouped = {}
            for entry in json.loads(out):
                parts = entry.get("Path", "").split("/", 1)
                if len(parts) == 2:
                    grouped.setdefault(parts[0], []).append(
                        (parts[1], int(entry.get("Size", 0))))
            for name, files in grouped.items():
                match = _STAMP.match(name)
                found.append({"where": "drive", "id": name,
                              "time": _stamp_time(name) or 0, "kind": "push",
                              "host": (match.group(5) if match else "") or "",
                              "files": sorted(files)})
        except (DriveError, ValueError):
            pass
    found.sort(key=lambda b: b["time"], reverse=True)
    return found


def _inside(root, rel):
    target = os.path.realpath(os.path.join(root, rel))
    return target == root or target.startswith(root + os.sep)


def restore_backup(project_path, project_name, backup, files=None):
    """Put a backup's files back into the project. What is in the project
    now under those names is kept first, as a backup of its own — putting
    something back can always be undone. Returns how many files came back.
    """
    root = os.path.realpath(project_path)
    wanted = [rel for rel, _size in backup["files"]
              if files is None or rel in files]
    wanted = [rel for rel in wanted
              if not os.path.isabs(rel) and ".." not in rel.split(os.sep)
              and _inside(root, rel)]
    if not wanted:
        return 0

    # 1. keep what is about to be overwritten
    safety = os.path.join(_local_backups(project_path), _stamp())
    kept = 0
    for rel in wanted:
        current = os.path.join(root, rel)
        if os.path.isfile(current) and not os.path.islink(current):
            dest = os.path.join(safety, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(current, dest)
            kept += 1
    if kept:
        _write_sidecar(safety, {"kind": "restore", "from": _host(),
                                "time": time.time()})

    # 2. bring the backup's copies in
    if backup["where"] == "local":
        source = os.path.realpath(os.path.join(_local_backups(project_path),
                                               backup["id"]))
        for rel in wanted:
            src = os.path.join(source, rel)
            if not os.path.realpath(src).startswith(source + os.sep):
                continue
            if os.path.isfile(src) and not os.path.islink(src):
                dest = os.path.join(root, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
    else:
        fd, listing = tempfile.mkstemp(prefix="petacore-restore-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(wanted) + "\n")
        try:
            _run(["copy", f"{_drive_backups(project_name)}/{backup['id']}",
                  root, "--files-from", listing], timeout=1800)
        finally:
            os.remove(listing)
    return len(wanted)


def prune_backups(project_path, project_name, days=None):
    """Drop backups older than the kept period (Settings; 0 = keep all)."""
    from .config import config
    days = config.get("drive_backup_days") if days is None else days
    if not days:
        return
    cutoff = time.time() - int(days) * 86400
    root = _local_backups(project_path)
    if os.path.isdir(root):
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            when = _stamp_time(name.removesuffix(".json"))
            if when is not None and when < cutoff:
                if os.path.isdir(folder):
                    shutil.rmtree(folder, ignore_errors=True)
                elif name.endswith(".json"):
                    os.remove(folder)
    out = _run(["lsjson", "--dirs-only", _drive_backups(project_name)],
               timeout=120)
    for entry in json.loads(out or "[]"):
        when = _stamp_time(entry.get("Name", ""))
        if when is not None and when < cutoff:
            _run(["purge", f"{_drive_backups(project_name)}/{entry['Name']}"],
                 timeout=600)


def _absorb_legacy(project_name):
    """Move this project's "_Replaced files" folders into its backups, then
    remove the old folder once it is empty."""
    old = f"{REMOTE}:{DRIVE_FOLDER}/{LEGACY_ATTIC}"
    try:
        out = _run(["lsjson", "--dirs-only", f"{old}/{project_name}"],
                   timeout=60)
    except DriveError:
        return
    for entry in json.loads(out or "[]"):
        day = entry.get("Name", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        _run(["move", f"{old}/{project_name}/{day}",
              f"{_drive_backups(project_name)}/{day} 00.00.00",
              "--delete-empty-src-dirs"], timeout=1800)
    try:
        _run(["rmdirs", old], timeout=120)
    except DriveError:
        pass


def backup_day(when):
    """"Today", "Yesterday" or the date, for grouping backups."""
    from .i18n import translator as _
    day = time.strftime("%Y-%m-%d", time.localtime(when))
    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d",
                              time.localtime(time.time() - 86400))
    if day == today:
        return _("backup_today")
    if day == yesterday:
        return _("backup_yesterday")
    return time.strftime("%d.%m.%Y", time.localtime(when))


def backup_title(backup):
    from .i18n import translator as _
    if backup["kind"] == "push":
        return _("backup_kind_push", host=backup["host"] or "?")
    if backup["kind"] == "restore":
        return _("backup_kind_restore")
    return _("backup_kind_pull")
