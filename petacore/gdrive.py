"""Google Drive synchronization for Petacore, built on rclone.

Each user connects their own Google Drive account (browser OAuth handled by
rclone). Petacore creates a "Petacomm Petacore™" folder in their Drive and
mirrors every project into its own subfolder — source code and media files
included (.git and other noise excluded).
"""

import os
import shutil
import subprocess

REMOTE = "petacore-gdrive"
DRIVE_FOLDER = "Petacomm Petacore™"
EXCLUDES = [".git/**", ".petacore/**", "__pycache__/**",
            "node_modules/**", ".venv/**"]


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


def sync_project(project_path: str, project_name: str, progress=None) -> str:
    """Mirror the project into Drive:  Petacomm Petacore™/<name>/ …
    If `progress` is given it is called with 0–100 as rclone reports stats."""
    target = f"{REMOTE}:{DRIVE_FOLDER}/{project_name}"
    args = ["sync", project_path, target, "--create-empty-src-dirs"]
    for pattern in EXCLUDES:
        args += ["--exclude", pattern]

    if progress is None:
        _run(args, timeout=3600)
        return f"{DRIVE_FOLDER}/{project_name}"

    import re
    args += ["--stats", "500ms", "--stats-one-line", "-v"]
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
                      if e.get("IsDir") and e.get("Name"))
    except (DriveError, ValueError):
        return []


def pull_project(project_name: str, local_path: str, progress=None):
    """Download a Drive project folder into a local folder (full sync down,
    media files included)."""
    import re
    source = f"{REMOTE}:{DRIVE_FOLDER}/{project_name}"
    args = ["sync", source, local_path, "--create-empty-src-dirs"]
    if progress is None:
        _run(args, timeout=3600)
        return local_path
    args += ["--stats", "500ms", "--stats-one-line", "-v"]
    try:
        proc = subprocess.Popen(["rclone"] + args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise DriveError("rclone-missing")
    tail, pct_re = [], re.compile(r"(\d{1,3})%")
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
    return local_path


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
