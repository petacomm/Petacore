"""Isolated sandbox for Petacore.

Commands run in a real container-style jail built with bubblewrap (bwrap):

  * the whole system is mounted READ-ONLY — nothing can modify the machine
  * /tmp and the home directory are throwaway tmpfs — writes vanish
  * only the sandbox folder (a copy of the project) is writable
  * the graphics socket is shared, so GUI apps still open real windows
  * everything dies with Petacore, and the folder is deleted at the end

If bubblewrap is missing, the session falls back to a plain temporary folder
(no isolation) and says so, instead of silently pretending to be safe.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time

EXCLUDE = (".git", ".petacore", "__pycache__", "node_modules", ".venv")

_active_sessions = []


class SandboxError(Exception):
    pass


def isolation_available() -> bool:
    return shutil.which("bwrap") is not None


class SandboxSession:
    """One throwaway, isolated workspace."""

    def __init__(self, title: str = "sandbox"):
        self.title = title
        self.dir = tempfile.mkdtemp(prefix="petacore-sandbox-")
        self.work = self.dir
        self.shell_pid = None
        self.isolated = isolation_available()
        self.alive = True
        _active_sessions.append(self)

    # -- filling the sandbox --------------------------------------------------
    def copy_project(self, project_path: str) -> str:
        """Copy the project in so it can be run and modified freely — the
        real project on disk is never touched."""
        dest = os.path.join(self.dir, os.path.basename(
            os.path.realpath(project_path)) or "app")
        shutil.copytree(project_path, dest,
                        ignore=shutil.ignore_patterns(*EXCLUDE),
                        symlinks=True)
        self.work = dest
        return dest

    def unpack_deb(self, deb_path: str) -> str:
        """Extract a .deb into the sandbox — a simulated install."""
        if not shutil.which("dpkg-deb"):
            raise SandboxError("dpkg-deb is not installed")
        proc = subprocess.run(["dpkg-deb", "-x", deb_path, self.dir],
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise SandboxError((proc.stderr or "dpkg-deb -x failed").strip())
        d = self.dir
        for _ in range(8):
            try:
                entries = os.listdir(d)
            except OSError:
                break
            if len(entries) == 1:
                child = os.path.join(d, entries[0])
                if os.path.isdir(child) and not os.path.islink(child):
                    d = child
                    continue
            break
        self.work = d
        return d

    # -- the isolated command line ---------------------------------------------
    def shell_argv(self):
        """Argv that opens a shell inside the jail (or a plain shell when
        bubblewrap is unavailable)."""
        shell = os.environ.get("SHELL", "/bin/bash")
        if not self.isolated:
            return [shell]

        argv = [
            "bwrap",
            "--ro-bind", "/", "/",                 # whole system, read-only
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",                     # throwaway /tmp
            "--tmpfs", os.path.expanduser("~"),    # throwaway home
            "--bind", self.dir, self.dir,          # only writable place
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--die-with-parent",
            "--new-session",
            "--chdir", self.work,
        ]
        # Let GUI apps open real windows: share the display sockets if present.
        for var in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
                    "XDG_RUNTIME_DIR"):
            if os.environ.get(var):
                argv += ["--setenv", var, os.environ[var]]
        argv += ["--setenv", "PETACORE_SANDBOX", "1"]
        argv += [shell]
        return argv

    # -- teardown ----------------------------------------------------------------
    def _kill(self):
        if self.shell_pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(self.shell_pid), sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(self.shell_pid, sig)
                    except OSError:
                        pass
                time.sleep(0.15)

        for _attempt in range(2):
            killed = False
            try:
                pids = [p for p in os.listdir("/proc") if p.isdigit()]
            except OSError:
                return
            for pid in pids:
                if int(pid) == os.getpid():
                    continue
                match = False
                for link in ("cwd", "exe"):
                    try:
                        target = os.readlink(f"/proc/{pid}/{link}")
                    except OSError:
                        continue
                    if target.startswith(self.dir):
                        match = True
                        break
                if match:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                        killed = True
                    except OSError:
                        pass
            if not killed:
                break
            time.sleep(0.2)

    def destroy(self):
        """End the session: stop everything, delete every temporary file."""
        if not self.alive:
            return
        self.alive = False
        self._kill()
        shutil.rmtree(self.dir, ignore_errors=True)
        if self in _active_sessions:
            _active_sessions.remove(self)


def destroy_all():
    for session in list(_active_sessions):
        session.destroy()
