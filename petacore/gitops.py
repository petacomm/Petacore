"""Git operations for Petacore. All calls are plain subprocess wrappers so the
system git (and its config/credentials) is always respected."""

import contextlib
import os
import re
import shutil
import subprocess
import tempfile


class GitError(Exception):
    pass


def _run(args, cwd=None, env=None, timeout=300):
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise GitError("git is not installed")
    except subprocess.TimeoutExpired:
        raise GitError("git operation timed out")
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "git failed").strip())
    return proc.stdout.strip()


def _github_https(url: str) -> bool:
    """Only a github.com address over HTTPS ever gets the token. A clone
    from anywhere else runs without it, so a look-alike server asking for a
    password learns nothing."""
    from urllib.parse import urlsplit
    parts = urlsplit((url or "").strip())
    return parts.scheme == "https" and parts.hostname == "github.com"


@contextlib.contextmanager
def _auth(token: str, url: str = ""):
    """An environment in which git can authenticate to github.com with the
    token — without the token ever being written into the remote URL, the
    git config, a command line or a file.

    git asks a small askpass script for the username and password. The
    script holds no secret: it echoes the token from its own environment,
    and only when git is asking about github.com. It lives in a fresh
    directory only this user can enter, and is gone when the operation
    ends.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # never hand control of a clone to a helper program named by the remote
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh:git:file"
    if not token or not _github_https(url):
        yield env
        return
    folder = tempfile.mkdtemp(prefix="petacore-git-")
    try:
        askpass = os.path.join(folder, "askpass")
        fd = os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n"
                    "case \"$1\" in\n"
                    "  *\"//github.com'\"*|*\"@github.com'\"*) ;;\n"
                    "  *) exit 1 ;;\n"
                    "esac\n"
                    "case \"$1\" in\n"
                    "  [Uu]sername*) echo x-access-token ;;\n"
                    "  *) printf '%s\\n' \"$PETACORE_TOKEN\" ;;\n"
                    "esac\n")
        env["GIT_ASKPASS"] = askpass
        env["PETACORE_TOKEN"] = token
        # a credential helper from the person's own config could otherwise
        # answer first, or store the token somewhere on disk
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""
        yield env
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def is_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def repo_root(path: str):
    try:
        return _run(["rev-parse", "--show-toplevel"], cwd=path)
    except GitError:
        return None


def init(path: str, repo_url: str = ""):
    os.makedirs(path, exist_ok=True)
    _run(["init", "-b", "main"], cwd=path)
    if repo_url:
        _run(["remote", "add", "origin", repo_url], cwd=path)


def clone(repo_url: str, dest: str, token: str = ""):
    repo_url = (repo_url or "").strip()
    if not repo_url or repo_url.startswith("-"):
        raise GitError("not a repository address")
    with _auth(token, repo_url) as env:
        _run(["clone", "--", repo_url, dest], env=env)


def current_branch(path: str):
    try:
        return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    except GitError:
        return None


def last_commit(path: str):
    try:
        return _run(["log", "-1", "--format=%h · %s · %cr"], cwd=path)
    except GitError:
        return None


def has_changes(path: str) -> bool:
    try:
        return bool(_run(["status", "--porcelain"], cwd=path))
    except GitError:
        return False


def remote_url(path: str):
    try:
        return _run(["remote", "get-url", "origin"], cwd=path)
    except GitError:
        return None


def set_remote(path: str, url: str):
    """Point origin at a new URL (add it if missing, remove if url empty)."""
    try:
        if not url:
            _run(["remote", "remove", "origin"], cwd=path)
        elif remote_url(path):
            _run(["remote", "set-url", "origin", url], cwd=path)
        else:
            _run(["remote", "add", "origin", url], cwd=path)
    except GitError:
        pass


def ahead_behind(path: str):
    """Returns (ahead, behind) relative to upstream, or None."""
    try:
        out = _run(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=path)
        a, b = out.split()
        return int(a), int(b)
    except (GitError, ValueError):
        return None


def tracked_count(path: str):
    try:
        out = _run(["ls-files"], cwd=path)
        return len(out.splitlines()) if out else 0
    except GitError:
        return 0


def fetch(path: str, token: str = ""):
    with _auth(token, remote_url(path) or "") as env:
        _run(["fetch", "origin"], cwd=path, env=env)


def _identity(path: str):
    """Commit as the person when git knows who they are; otherwise as
    Petacore, without writing anything into their git config."""
    try:
        _run(["config", "user.email"], cwd=path)
        return []
    except GitError:
        return ["-c", "user.name=Petacore", "-c", "user.email=petacore@localhost"]


def _keep_secrets_out(path: str):
    """Stage everything except files that carry credentials.

    The same files packaging leaves out (.env, private keys, .netrc …) are
    kept out of every commit: added to .git/info/exclude so they are never
    picked up again, and taken out of the index if they had been committed
    before — the file stays on disk, it just stops being published.
    Returns the paths that were kept back.
    """
    from . import debbuild
    secrets = debbuild.find_secrets(path)
    if secrets:
        exclude = os.path.join(path, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        existing = ""
        if os.path.isfile(exclude):
            with open(exclude, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        lines = set(existing.splitlines())
        add = ["/" + p.replace(os.sep, "/") for p in secrets
               if "/" + p.replace(os.sep, "/") not in lines]
        if add:
            with open(exclude, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("# Petacore: credentials, never published\n")
                f.write("\n".join(add) + "\n")
    _run(["add", "-A"], cwd=path)
    if secrets:
        _run(["rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--"]
             + secrets, cwd=path)
    return secrets


def _has_head(path: str) -> bool:
    try:
        _run(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=path)
        return True
    except GitError:
        return False


def _staged(path: str) -> bool:
    try:
        _run(["diff", "--cached", "--quiet"], cwd=path)
        return False
    except GitError:
        return True


BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])$")


def push_branch(path: str, token: str, branch: str = "main",
                message: str = "Petacore"):
    """Commit what has changed and put it on `branch` at origin.

    Whatever is already on that branch on GitHub is kept: local work is
    replayed on top of it. Two unrelated histories are never forced
    together — that stops with an error instead of overwriting anything.
    Returns (commit, kept_back_paths).
    """
    if not BRANCH.match(branch or "") or branch.endswith(".lock"):
        raise GitError("bad-branch")
    url = remote_url(path)
    if not url:
        raise GitError("no-remote")
    kept = _keep_secrets_out(path)
    if _staged(path) or not _has_head(path):
        _run(_identity(path) + ["commit", "--allow-empty", "-m", message],
             cwd=path)
    with _auth(token, url) as env:
        remote = _run(["ls-remote", "--heads", "origin",
                       "refs/heads/" + branch], cwd=path, env=env)
        if remote:
            # Fetch exactly that branch and replay every local commit it
            # lacks on top of it. Not `git pull --rebase`: its fork-point
            # guess trusts the old remote-tracking ref, and after the
            # remote has changed it would drop local commits as if they
            # were already on GitHub.
            _run(["fetch", "--no-tags", "origin", "refs/heads/" + branch],
                 cwd=path, env=env)
            try:
                _run(_identity(path) + ["rebase", "--no-fork-point",
                                        "--no-autostash", "FETCH_HEAD"],
                     cwd=path)
            except GitError:
                try:
                    _run(["rebase", "--abort"], cwd=path)
                except GitError:
                    pass
                raise GitError("diverged")
        _run(["push", "origin", "HEAD:refs/heads/" + branch],
             cwd=path, env=env)
    try:
        _run(["branch", "--set-upstream-to", "origin/" + branch], cwd=path)
    except GitError:
        pass                     # a detached or differently named branch
    return _run(["rev-parse", "HEAD"], cwd=path), kept


def sync(path: str, token: str = "", message: str = "Petacore sync"):
    """Full synchronization: commit everything (credentials excepted),
    integrate the remote, and push to the current branch."""
    if not remote_url(path):
        raise GitError("no-remote")
    branch = current_branch(path)
    if not branch or branch == "HEAD":
        branch = "main"
    return push_branch(path, token, branch, message)[1]
