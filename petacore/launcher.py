"""Chooses which Petacore build to start: the GNOME (GTK4/libadwaita) one or
the KDE Plasma (Qt/Breeze) one.

Order of decision:
  1. an explicit choice saved in the config ("ui" = "gnome" | "kde")
  2. --gnome / --kde on the command line
  3. the running desktop session (XDG_CURRENT_DESKTOP)
  4. whichever toolkit is actually installed
"""

import os
import shutil
import subprocess
import threading
import time
import sys

from .config import config


def detect_desktop() -> str:
    """'gnome', 'kde' or '' when it is neither."""
    raw = " ".join([
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        os.environ.get("XDG_SESSION_DESKTOP", ""),
        os.environ.get("DESKTOP_SESSION", ""),
    ]).lower()
    if "kde" in raw or "plasma" in raw:
        return "kde"
    if "gnome" in raw or "unity" in raw or "ubuntu" in raw:
        return "gnome"
    return ""


def gtk_available() -> bool:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        return True
    except Exception:
        return False


def qt_available() -> bool:
    try:
        import PySide6  # noqa: F401
        return True
    except Exception:
        return False


def choose() -> str:
    if "--kde" in sys.argv or "--qt" in sys.argv:
        return "kde"
    if "--gnome" in sys.argv or "--gtk" in sys.argv:
        return "gnome"

    saved = (config.get("ui") or "").strip()
    if saved in ("gnome", "kde"):
        return saved

    desktop = detect_desktop()
    if desktop == "kde" and qt_available():
        return "kde"
    if desktop == "gnome" and gtk_available():
        return "gnome"

    # Neither detected (or the matching toolkit is missing): take what exists.
    if gtk_available():
        return "gnome"
    if qt_available():
        return "kde"
    return "gnome"


GNOME_INSTALL_CMD = ("sudo apt install gir1.2-gtk-4.0 gir1.2-adw-1 "
                     "gir1.2-vte-3.91 gir1.2-gtksource-5")
KDE_INSTALL_CMD = "sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore"

GNOME_PACKAGES = ["gir1.2-gtk-4.0", "gir1.2-adw-1", "gir1.2-vte-3.91",
                 "gir1.2-gtksource-5"]
KDE_PACKAGES = ["python3-pyside6.qtwidgets", "python3-pyside6.qtgui",
               "python3-pyside6.qtcore"]


def pkexec_available() -> bool:
    return shutil.which("pkexec") is not None


def install_packages(packages):
    """Install packages from inside the running app: pkexec pops the
    normal GNOME/KDE password prompt, then apt runs with it. Returns
    (success, output) — never raises."""
    if not pkexec_available():
        return False, "pkexec-missing"
    try:
        subprocess.run(["pkexec", "apt-get", "update"],
                       capture_output=True, text=True, timeout=120)
        proc = subprocess.run(
            ["pkexec", "apt-get", "install", "-y"] + packages,
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "apt-get failed").strip()
    return True, proc.stdout


def relaunch_as(ui: str):
    """Save the chosen interface and restart Petacore with it.

    A detached child process is spawned and this one exits. Replacing the
    process in place (os.exec*) is unreliable when the current toolkit still
    holds a display connection — Qt in particular can die before the new
    process starts, which left the app simply closed instead of reopening.
    """
    config.set("ui", ui)
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [package_root] + ([existing] if existing else []))
    # Drop toolkit-specific variables so the new build starts clean.
    for var in ("QT_QPA_PLATFORM", "QT_STYLE_OVERRIDE", "QT_SCALE_FACTOR",
                "GTK_THEME", "GDK_BACKEND"):
        env.pop(var, None)

    log_file = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
        "petacore", "restart.log")
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    except OSError:
        log_file = os.devnull

    # A single-instance app (GTK's GApplication, and Qt with a shared name)
    # would hand the new process straight back to the still-running old one,
    # which then exits immediately — so the restart has to happen *after*
    # this instance is gone. A tiny detached helper waits for that.
    helper = (
        "import os, subprocess, sys, time\n"
        "parent, ui, logfile = int(sys.argv[1]), sys.argv[2], sys.argv[3]\n"
        "deadline = time.time() + 15\n"
        "while time.time() < deadline:\n"
        "    try:\n"
        "        os.kill(parent, 0)\n"
        "    except OSError:\n"
        "        break\n"
        "    time.sleep(0.15)\n"
        "time.sleep(0.4)\n"
        "log = open(logfile, 'w')\n"
        "subprocess.Popen([sys.executable, '-m', 'petacore.launcher',\n"
        "                  '--' + ui], stdout=log, stderr=log,\n"
        "                 start_new_session=True)\n"
    )

    try:
        subprocess.Popen(
            [sys.executable, "-c", helper, str(os.getpid()), ui, log_file],
            env=env, cwd=package_root, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False

    try:
        from . import sandbox
        sandbox.destroy_all()
    except Exception:
        pass
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return True


def relaunch_current():
    """Restart Petacore with the interface it is already running, used after
    a language change so every label is rebuilt from scratch."""
    return relaunch_as(choose())


def main():
    variant = choose()
    if variant == "kde":
        from .qt.main import main as qt_main
        return qt_main()
    from .main import main as gtk_main
    return gtk_main()


if __name__ == "__main__":
    sys.exit(main())
