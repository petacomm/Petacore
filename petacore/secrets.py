"""Storage for secrets that should not sit in a plain file.

The access token used to push to GitHub is a credential: anyone holding it
can act on the account. It therefore belongs in the desktop's own secret
store — GNOME Keyring on GNOME, KWallet on KDE — both of which are reached
through libsecret's `secret-tool`, and both of which keep the value
encrypted while the session is locked.

Where no secret service is running (a bare tty, a minimal container) the
value falls back to the configuration file, which is created with owner-only
permissions. `storage_kind()` reports which of the two is in use so the
interface can tell the user plainly.
"""

import shutil
import subprocess

SERVICE = "io.petacore.Petacore"
_TIMEOUT = 10


def _tool():
    return shutil.which("secret-tool")


def available() -> bool:
    """True when a working secret service is reachable."""
    if not _tool():
        return False
    try:
        # A lookup for a key that does not exist still proves the service
        # answers; a missing service exits non-zero with an error on stderr.
        proc = subprocess.run(
            [_tool(), "lookup", "service", SERVICE, "key", "__probe__"],
            capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    # exit 1 with empty stderr = "not found", which is a healthy service
    return proc.returncode in (0, 1) and "not available" not in proc.stderr


def store(key: str, value: str) -> bool:
    if not value or not available():
        return False
    try:
        proc = subprocess.run(
            [_tool(), "store", "--label", f"Petacore \u2014 {key}",
             "service", SERVICE, "key", key],
            input=value, capture_output=True, text=True, timeout=_TIMEOUT)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def retrieve(key: str) -> str:
    if not available():
        return ""
    try:
        proc = subprocess.run(
            [_tool(), "lookup", "service", SERVICE, "key", key],
            capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def clear(key: str) -> bool:
    if not available():
        return False
    try:
        subprocess.run([_tool(), "clear", "service", SERVICE, "key", key],
                       capture_output=True, text=True, timeout=_TIMEOUT)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def why_unavailable() -> str:
    """"", "no-tool" or "no-service" — the difference decides the advice.

    A missing `secret-tool` is by far the commoner case on a desktop that
    does have GNOME Keyring or KWallet running, and it is fixed by one
    package; a missing secret service is a different problem entirely.
    """
    if not _tool():
        return "no-tool"
    if not available():
        return "no-service"
    return ""


def storage_kind() -> str:
    """"keyring" or "file" — what the interface should tell the user."""
    return "keyring" if available() else "file"
