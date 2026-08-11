"""Git operations for Petacore. All calls are plain subprocess wrappers so the
system git (and its config/credentials) is always respected."""

import os
import shutil
import stat
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


def _auth_env(token: str):
    """Environment that lets git authenticate over HTTPS with a token,
    without ever writing the token to disk or to the remote URL."""
    env = dict(os.environ)
    if token:
        askpass = os.path.join(tempfile.gettempdir(), f".petacore-askpass-{os.getpid()}")
        with open(askpass, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\ncase \"$1\" in\n"
                    "*[Uu]sername*) echo x-access-token ;;\n"
                    "*) echo \"$PETACORE_TOKEN\" ;;\nesac\n")
        os.chmod(askpass, stat.S_IRWXU)
        env["GIT_ASKPASS"] = askpass
        env["PETACORE_TOKEN"] = token
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


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
    _run(["clone", repo_url, dest], env=_auth_env(token))


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
    _run(["fetch", "origin"], cwd=path, env=_auth_env(token))


def sync(path: str, token: str = "", message: str = "Petacore sync"):
    """Full synchronization: stage everything, commit (even when empty),
    integrate the remote, and push."""
    env = _auth_env(token)
    ident = ["-c", "user.name=Petacore", "-c", "user.email=petacore@localhost"]
    # Only force the identity when the user has none configured.
    try:
        _run(["config", "user.email"], cwd=path)
        ident = []
    except GitError:
        pass

    _run(["add", "-A"], cwd=path)
    _run(ident + ["commit", "--allow-empty", "-m", message], cwd=path)

    branch = current_branch(path) or "main"
    if remote_url(path):
        try:
            _run(["pull", "--rebase", "origin", branch], cwd=path, env=env)
        except GitError:
            pass  # first push: the remote branch may not exist yet
        _run(["push", "-u", "origin", branch], cwd=path, env=env)
    else:
        raise GitError("no-remote")
