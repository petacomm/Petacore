"""Encrypted projects — protection at the cloud-sync level.

The rule that shapes this module: **nothing encrypted ever appears inside
Petacore.** The local project stays ordinary, readable source code; the
encryption happens on the way to Google Drive and is undone on the way back.
What lands in Drive is unreadable to anyone without the password — including
file and folder names — but the developer never sees an encrypted blob in the
Project page, the editor, or anywhere else in the app.

This is built on rclone's `crypt` backend, which wraps the existing Drive
remote:

    petacore-gdrive:            plain Drive access (used by normal projects)
    petacore-crypt-<slug>:      crypt layer on top of the project's folder

Three password strengths are offered, per the product brief:

    pin4      4-digit PIN
    pin8      8-digit PIN
    password  free-form passphrase, up to 4096 characters
"""

import hashlib
import os
import re
import subprocess

from .config import config

MODES = ("none", "pin4", "pin8", "password")
MAX_PASSWORD_LEN = 4096


class CryptoError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Password rules
# --------------------------------------------------------------------------- #
def validate(mode: str, secret: str):
    """Raise CryptoError when the secret doesn't match the chosen mode."""
    secret = secret or ""
    if mode == "pin4":
        if not (secret.isdigit() and len(secret) == 4):
            raise CryptoError("pin4")
    elif mode == "pin8":
        if not (secret.isdigit() and len(secret) == 8):
            raise CryptoError("pin8")
    elif mode == "password":
        if not (1 <= len(secret) <= MAX_PASSWORD_LEN):
            raise CryptoError("password")
    else:
        raise CryptoError("mode")
    return True


def verifier(secret: str) -> str:
    """A salted hash kept locally so a wrong password can be rejected before
    any transfer starts. The password itself is never written to disk."""
    salt = b"petacore-project-encryption-v1"
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"),
                               salt, 200_000).hex()


def check(secret: str, expected: str) -> bool:
    if not expected:
        return True
    import hmac
    return hmac.compare_digest(verifier(secret), expected)


# --------------------------------------------------------------------------- #
# Project settings
# --------------------------------------------------------------------------- #
def slug(project_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", project_name).strip("-").lower()


def mode_for(project) -> str:
    return (project or {}).get("encrypt_mode") or "none"


def is_encrypted(project) -> bool:
    return mode_for(project) != "none"


def enable(project_path: str, mode: str, secret: str):
    validate(mode, secret)
    config.set_project_field(project_path, "encrypt_mode", mode)
    config.set_project_field(project_path, "encrypt_check", verifier(secret))


def disable(project_path: str):
    config.set_project_field(project_path, "encrypt_mode", "none")
    config.set_project_field(project_path, "encrypt_check", "")


# --------------------------------------------------------------------------- #
# rclone crypt remote
# --------------------------------------------------------------------------- #
def _rclone(args, timeout=120, check_rc=True):
    try:
        proc = subprocess.run(["rclone"] + args, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise CryptoError("rclone-missing")
    except subprocess.TimeoutExpired:
        raise CryptoError("timeout")
    if check_rc and proc.returncode != 0:
        raise CryptoError((proc.stderr or proc.stdout or "rclone failed")
                          .strip()[-300:])
    return proc.stdout


def obscure(secret: str) -> str:
    """rclone stores passwords lightly obfuscated; this is its own encoder."""
    return _rclone(["obscure", secret], timeout=30).strip()


def crypt_remote(project_name: str) -> str:
    return f"petacore-crypt-{slug(project_name)}"


def ensure_crypt_remote(project_name: str, secret: str) -> str:
    """(Re)create the crypt remote for this project. It wraps the project's
    own folder inside 'Petacomm Petacore™', and encrypts file *and* directory
    names so the Drive listing reveals nothing."""
    from . import gdrive
    name = crypt_remote(project_name)
    target = f"{gdrive.REMOTE}:{gdrive.DRIVE_FOLDER}/{project_name}"
    obscured = obscure(secret)

    _rclone(["config", "delete", name], timeout=30, check_rc=False)
    _rclone([
        "config", "create", name, "crypt",
        "remote", target,
        "password", obscured,
        "filename_encryption", "standard",
        "directory_name_encryption", "true",
        "--non-interactive",
    ], timeout=60)
    return name


def forget_crypt_remote(project_name: str):
    _rclone(["config", "delete", crypt_remote(project_name)],
            timeout=30, check_rc=False)


# --------------------------------------------------------------------------- #
# Encrypted transfer
# --------------------------------------------------------------------------- #
def push(project_path: str, project_name: str, secret: str, progress=None):
    """Upload the project to Drive through the crypt layer.

    The local folder is untouched; only the copy in Drive is encrypted.
    """
    from . import gdrive
    remote = ensure_crypt_remote(project_name, secret)
    args = ["sync", project_path, f"{remote}:", "--create-empty-src-dirs"]
    for pattern in gdrive.EXCLUDES:
        args += ["--exclude", pattern]
    try:
        if progress is None:
            _rclone(args, timeout=3600)
        else:
            _stream(args, progress)
    finally:
        # The remote holds the password, so it is not left lying around.
        forget_crypt_remote(project_name)
    return f"{gdrive.DRIVE_FOLDER}/{project_name}"


def pull(project_path: str, project_name: str, secret: str, progress=None):
    """Download and decrypt into a local folder — what lands on disk is
    plain, readable source, never an encrypted blob."""
    remote = ensure_crypt_remote(project_name, secret)
    os.makedirs(project_path, exist_ok=True)
    args = ["sync", f"{remote}:", project_path, "--create-empty-src-dirs"]
    try:
        if progress is None:
            _rclone(args, timeout=3600)
        else:
            _stream(args, progress)
    finally:
        forget_crypt_remote(project_name)
    return project_path


def can_open(project_name: str, secret: str) -> bool:
    """True when the password really decrypts the project on Drive."""
    remote = ensure_crypt_remote(project_name, secret)
    try:
        _rclone(["lsjson", f"{remote}:", "--max-depth", "1"], timeout=120)
        return True
    except CryptoError:
        return False
    finally:
        forget_crypt_remote(project_name)


def _stream(args, progress):
    args = list(args) + ["--stats", "500ms", "--stats-one-line", "-v"]
    try:
        proc = subprocess.Popen(["rclone"] + args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise CryptoError("rclone-missing")
    tail = []
    pct = re.compile(r"(\d{1,3})%")
    for line in proc.stderr:
        tail.append(line.strip())
        del tail[:-8]
        m = pct.search(line)
        if m:
            try:
                progress(min(100, int(m.group(1))))
            except Exception:
                pass
    proc.wait(timeout=60)
    if proc.returncode != 0:
        raise CryptoError("\n".join(tail)[-300:] or "rclone failed")
