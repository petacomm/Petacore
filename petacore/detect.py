"""Codebase detection for Petacore.

Answers, without any manual searching by the user:
- which processes are currently running *from* the project folder,
- where the running code is actually executing from,
- which folder is the active codebase (the git root).
"""

import os

from . import gitops


def _read_link(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = f.read().split(b"\0")
        return " ".join(p.decode("utf-8", "replace") for p in parts if p)[:120]
    except OSError:
        return ""


def running_processes(project_path: str):
    """Processes whose working directory, executable, or command line lives
    inside the project folder. Returns [{pid, name, via, cmd}]."""
    project_path = os.path.realpath(project_path)
    results = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return results

    for pid in pids:
        if int(pid) == os.getpid():
            continue
        via = None
        cwd = _read_link(f"/proc/{pid}/cwd")
        exe = _read_link(f"/proc/{pid}/exe")
        if cwd and (cwd == project_path or cwd.startswith(project_path + os.sep)):
            via = "cwd"
        elif exe and exe.startswith(project_path + os.sep):
            via = "exe"
        else:
            cmd = _cmdline(pid)
            if project_path in cmd:
                via = "cmd"
        if via:
            try:
                with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
                    name = f.read().strip()
            except OSError:
                name = "?"
            results.append({
                "pid": int(pid),
                "name": name,
                "via": via,
                "cmd": _cmdline(pid),
                "cwd": cwd or "",
            })
    return results


def active_codebase(project_path: str):
    """The authoritative root of the active codebase: the git toplevel if the
    folder is (inside) a repository, otherwise the folder itself."""
    root = gitops.repo_root(project_path)
    return root or os.path.realpath(project_path)


def project_summary(project_path: str):
    """One-shot summary used by the Overview page (runs in a worker thread)."""
    root = active_codebase(project_path)
    return {
        "root": root,
        "branch": gitops.current_branch(root),
        "last_commit": gitops.last_commit(root),
        "dirty": gitops.has_changes(root),
        "remote": gitops.remote_url(root),
        "ahead_behind": gitops.ahead_behind(root),
        "tracked": gitops.tracked_count(root),
        "processes": running_processes(root),
    }
