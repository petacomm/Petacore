"""GitHub authentication for Petacore.

Uses a Personal Access Token, validated against the GitHub REST API. The token
is used by gitops for HTTPS pushes/pulls via GIT_ASKPASS.
"""

import json
import urllib.error
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    pass


def _request(path: str, token: str):
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Petacore/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GitHubError(f"GitHub API error {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise GitHubError(f"Network error: {e}")


def validate_token(token: str) -> str:
    """Returns the GitHub username for a valid token, raises otherwise."""
    data = _request("/user", token)
    login = data.get("login")
    if not login:
        raise GitHubError("Invalid token")
    return login


# --------------------------------------------------------------------------- #
# GitHub CLI integration — the zero-friction path. If the developer is already
# logged in with `gh`, Petacore reuses that session with one click.
# --------------------------------------------------------------------------- #
import shutil  # noqa: E402
import subprocess  # noqa: E402


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_token():
    """Token from an existing `gh auth login` session, or None."""
    if not gh_available():
        return None
    try:
        proc = subprocess.run(["gh", "auth", "token"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


TOKEN_URL = ("https://github.com/settings/tokens/new"
             "?scopes=repo&description=Petacore")


# --------------------------------------------------------------------------- #
# OAuth Device Flow — true "Sign in with GitHub".
# The user clicks a button, the browser opens, they approve once, done.
# Requires a (free) OAuth App Client ID registered by the app author at
# github.com/settings/developers with "Enable Device Flow" checked.
# --------------------------------------------------------------------------- #
import time  # noqa: E402
import urllib.parse  # noqa: E402

DEVICE_CODE_URL = "https://github.com/login/device/code"
DEVICE_POLL_URL = "https://github.com/login/oauth/access_token"


def _post(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Petacore/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
            msg = payload.get("error_description") or payload.get("error")
        except ValueError:
            msg = raw.strip()
        raise GitHubError(msg or f"GitHub error {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise GitHubError(f"Network error: {e}")


def device_start(client_id: str) -> dict:
    """Begin the device flow. Returns user_code, verification_uri,
    device_code, interval, expires_in."""
    data = _post(DEVICE_CODE_URL, {"client_id": client_id, "scope": "repo"})
    if "user_code" not in data:
        raise GitHubError(data.get("error_description",
                                    "Could not start GitHub sign-in"))
    return data


def device_poll(client_id: str, device_code: str,
                interval: int = 5, expires_in: int = 900) -> str:
    """Poll until the user approves in the browser. Returns the token."""
    deadline = time.time() + min(expires_in, 900)
    wait = max(int(interval), 5)
    while time.time() < deadline:
        time.sleep(wait)
        data = _post(DEVICE_POLL_URL, {
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if "access_token" in data:
            return data["access_token"]
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            wait += 5
            continue
        raise GitHubError(data.get("error_description",
                                   error or "Sign-in failed"))
    raise GitHubError("Sign-in timed out")
