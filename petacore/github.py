"""GitHub for Petacore: signing in, and publishing releases.

Signing in is one button. Behind it, the quickest route that works on this
computer is taken:

  1. the GitHub CLI is already signed in       → nothing to do at all
  2. Petacore has an OAuth client ID           → GitHub's device sign-in
  3. the GitHub CLI is installed               → the CLI's own browser sign-in
  4. none of these                             → a token, pasted by hand

Routes 2 and 3 end the same way for the person: the browser opens on
github.com, the one-time code is already on the clipboard, they paste it and
press Authorize. No password ever passes through Petacore, and the token that
comes back is kept in the system keyring (see config.SECRET_KEYS), never in
a file, a URL or a command line.

The token only ever goes to api.github.com, uploads.github.com and — through
git's askpass, see gitops — github.com. Anything a server hands back that
points elsewhere is refused rather than followed.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
USER_AGENT = "Petacore/" + __version__.replace(" ", "-")

# The OAuth App that "Sign in with GitHub" talks to. A client ID is public by
# design (it is not a secret, and device sign-in needs no secret at all), so
# it is safe to ship inside the app. Register one at
# github.com/settings/applications/new, tick "Enable Device Flow", and put
# its Client ID here: every Petacore then signs in with the Petacore name on
# GitHub's consent page. Left empty, the GitHub CLI route is used instead.
BUILTIN_CLIENT_ID = ""

DEVICE_PAGE = "https://github.com/login/device"
TOKEN_URL = ("https://github.com/settings/tokens/new"
             "?scopes=repo&description=Petacore")


class GitHubError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Cancelled(GitHubError):
    pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _allowed(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme == "https"
            and parts.hostname in ("api.github.com", "uploads.github.com",
                                   "github.com"))


def _call(method, url, token="", body=None, data=None,
          content_type="application/json", timeout=30):
    """One request; JSON back. `body` is sent as JSON, `data` as raw bytes."""
    if not _allowed(url):
        raise GitHubError("refused to send a request outside github.com")
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        payload = data
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=payload, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        message = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
            message = detail.get("message", "")
            errors = detail.get("errors") or []
            codes = [x.get("code", "") for x in errors if isinstance(x, dict)]
            if "already_exists" in codes:
                message = "already_exists"
        except (ValueError, AttributeError):
            pass
        raise GitHubError(message or f"GitHub error {e.code}", e.code)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GitHubError(f"Network error: {getattr(e, 'reason', e)}")


def _request(path: str, token: str):
    return _call("GET", API + path, token)


def validate_token(token: str) -> str:
    """The GitHub username for a valid token; raises otherwise."""
    data = _request("/user", token)
    login = data.get("login")
    if not login:
        raise GitHubError("Invalid token")
    return login


# --------------------------------------------------------------------------- #
# GitHub CLI
# --------------------------------------------------------------------------- #
def gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh_env():
    env = dict(os.environ)
    # A token in the environment would make gh ignore its own sign-in.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["NO_COLOR"] = "1"
    return env


def gh_token():
    """The token of an existing `gh auth login`, or None."""
    if not gh_available():
        return None
    try:
        proc = subprocess.run(["gh", "auth", "token", "-h", "github.com"],
                              capture_output=True, text=True, timeout=10,
                              env=_gh_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


_GH_CODE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")


def gh_web_login(on_code, cancel=None, timeout=900):
    """Sign the GitHub CLI in through the browser and return its token.

    gh prints a one-time code and waits while the person approves it on
    github.com; the code is handed to `on_code` so the window can show it,
    copy it and open the page.
    """
    if not gh_available():
        raise GitHubError("gh-missing")
    try:
        proc = subprocess.Popen(
            ["gh", "auth", "login", "--web", "-h", "github.com",
             "-p", "https", "--skip-ssh-key"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=_gh_env())
    except OSError as e:
        raise GitHubError(str(e))

    seen = []
    told = threading.Event()

    def read():
        for line in proc.stdout:
            seen.append(line.strip())
            match = _GH_CODE.search(line)
            if match and not told.is_set():
                told.set()
                on_code(match.group(1), DEVICE_PAGE)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.time() + timeout
    while proc.poll() is None:
        if (cancel is not None and cancel.is_set()) or time.time() > deadline:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise Cancelled("cancelled")
        time.sleep(0.2)
    reader.join(2)
    if proc.returncode != 0:
        tail = next((s for s in reversed(seen) if s), "")
        raise GitHubError(tail or "GitHub CLI sign-in failed")
    token = gh_token()
    if not token:
        raise GitHubError("GitHub CLI sign-in failed")
    return token


# --------------------------------------------------------------------------- #
# OAuth device flow
# --------------------------------------------------------------------------- #
DEVICE_CODE_URL = "https://github.com/login/device/code"
DEVICE_POLL_URL = "https://github.com/login/oauth/access_token"


def client_id():
    """The OAuth client ID to sign in with: one set in Settings wins over
    the one built into Petacore."""
    try:
        from .config import config
        configured = (config.get("github_client_id") or "").strip()
    except Exception:            # noqa: BLE001 - config is optional here
        configured = ""
    return configured or BUILTIN_CLIENT_ID


def _post(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
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
        raise GitHubError(msg or f"GitHub error {e.code}", e.code)
    except (urllib.error.URLError, TimeoutError) as e:
        raise GitHubError(f"Network error: {e}")


def device_start(cid: str) -> dict:
    """Begin the device flow: user_code, verification_uri, device_code,
    interval, expires_in."""
    data = _post(DEVICE_CODE_URL, {"client_id": cid, "scope": "repo"})
    if "user_code" not in data:
        raise GitHubError(data.get("error_description",
                                   "Could not start GitHub sign-in"))
    return data


def device_poll(cid: str, device_code: str, interval: int = 5,
                expires_in: int = 900, cancel=None) -> str:
    """Wait until the person approves in the browser; the token."""
    deadline = time.time() + min(int(expires_in), 900)
    wait = max(int(interval), 5)
    while time.time() < deadline:
        for _ in range(wait * 5):
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled")
            time.sleep(0.2)
        data = _post(DEVICE_POLL_URL, {
            "client_id": cid,
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


def sign_in_route():
    """Which route the one button will take: "ready", "device", "gh" or
    "manual". Cheap enough to call when a dialog opens."""
    if client_id():
        return "device"
    if gh_available():
        return "gh"
    return "manual"


def sign_in(on_code, cancel=None):
    """The one button. Returns (token, username).

    `on_code(code, url)` is called once a code needs approving in the
    browser; it is called from a worker thread.
    """
    token = gh_token()
    if token:
        try:
            return token, validate_token(token)
        except GitHubError:
            pass                # a stale CLI session: sign in afresh
    cid = client_id()
    if cid:
        data = device_start(cid)
        uri = data.get("verification_uri") or DEVICE_PAGE
        if not _allowed(uri):
            uri = DEVICE_PAGE
        on_code(data["user_code"], uri)
        token = device_poll(cid, data["device_code"],
                            data.get("interval", 5),
                            data.get("expires_in", 900), cancel)
    elif gh_available():
        token = gh_web_login(on_code, cancel)
    else:
        raise GitHubError("no-route")
    return token, validate_token(token)


# --------------------------------------------------------------------------- #
# Repositories and releases
# --------------------------------------------------------------------------- #
_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:"
    r"|ssh://git@github\.com/)"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$")
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def parse_remote(url):
    """(owner, repo) for a github.com remote, None for anything else."""
    match = _REMOTE.match((url or "").strip())
    if not match:
        return None
    owner, repo = match.groups()
    if repo in (".", ".."):
        return None
    return owner, repo


def repo_slug(name):
    """A folder name made acceptable as a repository name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    return slug[:100] or "project"


def _path(*parts):
    return "/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)


def get_repo(token, owner, repo):
    return _call("GET", API + _path("repos", owner, repo), token)


def create_repo(token, name, private=True, description=""):
    """A new repository under the signed-in account. If the account already
    has one by that name, that one is used — never a fresh one with a
    number stuck on, which would scatter the project over several repos."""
    if not REPO_NAME.match(name or ""):
        raise GitHubError("bad-repo-name")
    try:
        return _call("POST", API + "/user/repos", token, body={
            "name": name, "private": bool(private),
            "description": description[:300], "auto_init": False,
        })
    except GitHubError as e:
        if e.status != 422:
            raise
        owner = validate_token(token)
        return get_repo(token, owner, name)


def release_by_tag(token, owner, repo, tag):
    try:
        return _call("GET", API + _path("repos", owner, repo, "releases",
                                        "tags", tag), token)
    except GitHubError as e:
        if e.status == 404:
            return None
        raise


def release_tags(token, owner, repo, limit=30):
    """Tags of the latest releases, newest first ([] if there are none)."""
    try:
        data = _call("GET", API + _path("repos", owner, repo, "releases")
                     + f"?per_page={int(limit)}", token)
    except GitHubError as e:
        if e.status == 404:
            return []
        raise
    return [r.get("tag_name", "") for r in data if isinstance(r, dict)]


def create_release(token, owner, repo, tag, title, target, prerelease,
                   notes=""):
    body = {"tag_name": tag, "target_commitish": target, "name": title,
            "prerelease": bool(prerelease), "draft": False}
    if notes.strip():
        body["body"] = notes
    else:
        body["generate_release_notes"] = True
    return _call("POST", API + _path("repos", owner, repo, "releases"),
                 token, body=body)


def upload_asset(token, release, file_path):
    """Attach one file to a release. The upload address comes from GitHub;
    it is checked to really be GitHub's before the token goes with it."""
    template = release.get("upload_url", "")
    base = template.split("{", 1)[0]
    if not base.startswith(UPLOADS + "/"):
        raise GitHubError("unexpected upload address")
    name = os.path.basename(file_path)
    url = base + "?" + urllib.parse.urlencode({"name": name})
    with open(file_path, "rb") as f:
        data = f.read()
    kind = {"deb": "application/vnd.debian.binary-package",
            "rpm": "application/x-rpm"}.get(name.rsplit(".", 1)[-1],
                                            "application/octet-stream")
    return _call("POST", url, token, data=data, content_type=kind,
                 timeout=600)
