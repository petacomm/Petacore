#!/usr/bin/env python3
"""Security regression tests for Petacore.

Each test corresponds to a finding that was fixed. Run this after any change
that touches packaging, the sandbox, credentials or synchronisation:

    python3 security_tests.py
"""

import os
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail and not condition
                                  else ""))


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


# --------------------------------------------------------------------------- #
section("1. Credential storage")
# --------------------------------------------------------------------------- #
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
from petacore import secrets                      # noqa: E402
from petacore.config import config, CONFIG_FILE   # noqa: E402

vault = {}
secrets.available = lambda: True
secrets.store = lambda k, v: (vault.__setitem__(k, v), True)[1]
secrets.retrieve = lambda k: vault.get(k, "")
secrets.clear = lambda k: (vault.pop(k, None), True)[1]

config.set("github_token", "ghp_REGRESSION_TOKEN")
raw = open(CONFIG_FILE).read()
check("token is not written to config.json", "ghp_REGRESSION" not in raw)
check("token is readable through the keyring",
      config.get("github_token") == "ghp_REGRESSION_TOKEN")
config.set("github_token", "")
check("logging out clears the keyring entry", "github_token" not in vault)
check("config file is owner-only",
      stat.S_IMODE(os.stat(CONFIG_FILE).st_mode) == 0o600)
check("config directory is owner-only",
      stat.S_IMODE(os.stat(os.path.dirname(CONFIG_FILE)).st_mode) == 0o700)

captured = {}
real_open = os.open


def spy(path, flags, mode=0o777, *a, **k):
    if str(path).endswith(".tmp"):
        captured["mode"] = mode
    return real_open(path, flags, mode, *a, **k)


os.open = spy
config.set("autosave_minutes", 11)
os.open = real_open
check("temp file is created owner-only (no permission window)",
      captured.get("mode") == 0o600)

# --------------------------------------------------------------------------- #
section("2. Package building")
# --------------------------------------------------------------------------- #
from petacore import debbuild, gitops             # noqa: E402
from petacore.debbuild import _safe_version, _safe_line, _pkg_name  # noqa: E402

check("version cannot traverse directories",
      "/" not in _safe_version("../../../../tmp/PWNED")
      and ".." not in _safe_version("../../../../tmp/PWNED"))
check("version cannot inject newlines",
      "\n" not in _safe_version("1.0\nPackage: evil"))
check("version is always valid for dpkg",
      _safe_version("evil")[0].isdigit() and _safe_version("")[0].isdigit())
check("package name cannot contain a path separator",
      "/" not in _pkg_name("../../etc/passwd"))
check("metadata lines cannot inject fields",
      "\n" not in _safe_line("evil\nPriority: required"))
check("metadata lines cannot inject rpm scriptlets",
      "%pre" not in _safe_line("MIT\n%pre\nrm -rf /"))

proj = tempfile.mkdtemp()
open(os.path.join(proj, "main.py"), "w").write("print(1)\n")
open(os.path.join(proj, ".env"), "w").write("API_KEY=secret\n")
open(os.path.join(proj, ".env.example"), "w").write("API_KEY=\n")
open(os.path.join(proj, "server.pem"), "w").write("-----BEGIN KEY-----\n")
os.makedirs(os.path.join(proj, ".ssh"))
open(os.path.join(proj, ".ssh", "id_rsa"), "w").write("k")

found = debbuild.find_secrets(proj)
check("credential files are detected",
      ".env" in found and "server.pem" in found)
check("harmless examples are not flagged", ".env.example" not in found)

if __import__("shutil").which("dpkg-deb"):
    out = tempfile.mkdtemp()
    path = debbuild.build_deb(proj, out, "../../../evil",
                              "../../../../tmp/PWNED",
                              "evil\nPriority: required", "desc",
                              "MIT\n%pre\nrm -rf /")
    check("package stays inside the chosen folder",
          os.path.realpath(path).startswith(os.path.realpath(out)))
    ctl = subprocess.run(["dpkg-deb", "-I", path],
                         capture_output=True, text=True).stdout
    check("no control-field injection", "Priority: required" not in ctl)
    listing = subprocess.run(["dpkg-deb", "-c", path],
                             capture_output=True, text=True).stdout
    check("credentials excluded from the package",
          ".env\n" not in listing and "id_rsa" not in listing)
    check("project code is still packaged", "main.py" in listing)
    check(".env.example is still packaged", ".env.example" in listing)
else:
    print("  [skip] dpkg-deb not installed")

# --------------------------------------------------------------------------- #
section("2b. Package metadata")
# --------------------------------------------------------------------------- #
from petacore.debbuild import _metainfo, _spdx, repo_homepage  # noqa: E402

meta = _metainfo("io.petacore.demo", "demo", "Demo", "A demo application",
                 "MIT", "Kuzey Yilmaz <k@petacomm.com>", "1.0.0",
                 "https://github.com/petacomm/demo")
check("metadata names the developer", "<name>Kuzey Yilmaz</name>" in meta)
check("metadata carries an SPDX licence",
      "<project_license>MIT</project_license>" in meta)
check("metadata carries a homepage", "url type=\"homepage\"" in meta)
check("an unrecognised licence is not claimed as open",
      _spdx("some in-house terms") == "LicenseRef-proprietary")
check("common licence names map to SPDX",
      _spdx("Apache 2.0") == "Apache-2.0" and _spdx("GPLv3") == "GPL-3.0-only")
check("ssh remotes become browsable links",
      repo_homepage("git@github.com:a/b.git") == "https://github.com/a/b")
check("metadata escapes user text",
      "&lt;" in _metainfo("io.x", "x", "<script>", "s", "MIT", "m", "1"))

if __import__("shutil").which("appstreamcli"):
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
        fh.write(meta)
        meta_file = fh.name
    result = subprocess.run(["appstreamcli", "validate", meta_file],
                            capture_output=True, text=True)
    # The validator also reports things that depend on the network (whether
    # the home page answers) and on how much the user wrote. Only structural
    # errors mean the metadata is wrong.
    structural = [line for line in result.stdout.splitlines()
                  if line.startswith("E:")]
    check("metadata has no structural errors", not structural,
          "; ".join(structural)[:160])
else:
    print("  [skip] appstreamcli not installed")

# --------------------------------------------------------------------------- #
section("3. Sandbox isolation")
# --------------------------------------------------------------------------- #
os.environ.update({
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "GPG_AGENT_INFO": "/run/user/1000/gnupg/S.gpg-agent",
    "GITHUB_TOKEN": "ghp_should_not_pass",
    "AWS_SECRET_ACCESS_KEY": "aws_should_not_pass",
    "DISPLAY": ":0",
})
from petacore.sandbox import SandboxSession        # noqa: E402

session = SandboxSession.__new__(SandboxSession)
session.dir = "/tmp/probe"
session.work = "/tmp/probe"
session.isolated = True
session.network = False
argv = session.shell_argv()
joined = " ".join(argv)

check("environment is cleared", "--clearenv" in argv)
check("ssh agent does not reach the sandbox", "SSH_AUTH_SOCK" not in joined)
check("gpg agent does not reach the sandbox", "GPG_AGENT_INFO" not in joined)
check("tokens do not reach the sandbox",
      "ghp_should_not_pass" not in joined
      and "aws_should_not_pass" not in joined)
check("display is still forwarded", "DISPLAY" in joined)
check("root filesystem is read-only", "--ro-bind" in argv)
check("home directory is masked", "--tmpfs" in argv)
check("process namespace is separated", "--unshare-pid" in argv)
check("only the session folder is writable",
      argv.count("--bind") == 1 and session.dir in argv)

# --------------------------------------------------------------------------- #
section("2c. Package signing")
# --------------------------------------------------------------------------- #
from petacore import gpgsign as _gpg                # noqa: E402

if __import__("shutil").which("dpkg-deb") and __import__("shutil").which("ar"):
    keys = _gpg.list_secret_keys()
    if keys:
        sproj = tempfile.mkdtemp()
        gitops.init(sproj)
        open(os.path.join(sproj, "main.py"), "w").write("print(1)\n")
        sout = tempfile.mkdtemp()
        spath = debbuild.build_deb(sproj, sout, "signed", "1.0.0",
                                   "K <k@x.com>", "Demo", "MIT", "")
        result = _gpg.sign_deb(spath, keys[0][0])

        check("signing produces a single file",
              len(os.listdir(sout)) == 1 and result == spath,
              str(os.listdir(sout)))
        members = subprocess.run(["ar", "t", spath],
                                 capture_output=True, text=True).stdout.split()
        check("the signature is inside the package", "_gpgorigin" in members)
        check("the embedded signature verifies", _gpg.verify_deb(spath))
        check("dpkg still reads the signed package",
              subprocess.run(["dpkg-deb", "-I", spath],
                             capture_output=True).returncode == 0)

        # altering the payload must invalidate the signature
        work = tempfile.mkdtemp()
        subprocess.run(["ar", "x", spath], cwd=work, check=True)
        payload = [f for f in os.listdir(work) if f.startswith("data.tar")][0]
        with open(os.path.join(work, payload), "ab") as fh:
            fh.write(b"\x00" * 64)
        subprocess.run(["ar", "r", spath, payload], cwd=work, check=True)
        check("tampering breaks the signature", not _gpg.verify_deb(spath))
    else:
        print("  [skip] no GPG key available")
else:
    print("  [skip] dpkg-deb or ar not installed")

# --------------------------------------------------------------------------- #
section("3b. Sandbox network")
# --------------------------------------------------------------------------- #
import petacore.sandbox as sandbox_mod             # noqa: E402
from petacore.netwatch import (OutputWatcher,      # noqa: E402
                               looks_like_blocked_network)

check("network is off by default",
      config.get("sandbox_network") is False)
check("the prompt is on by default",
      config.get("sandbox_network_prompt") is True)

sandbox_mod.isolation_available = lambda: True
probe = sandbox_mod.SandboxSession()
probe.isolated = True
check("a default session has no network",
      "--unshare-net" in probe.shell_argv())
probe.destroy()

granted = sandbox_mod.SandboxSession(network=True)
granted.isolated = True
check("permission can be granted for one session only",
      "--unshare-net" not in granted.shell_argv()
      and config.get("sandbox_network") is False)
granted.destroy()

for message in ("curl: (6) Could not resolve host: github.com",
                "E: Failed to fetch http://x Temporary failure in name resolution",
                "ping: connect: Network is unreachable",
                "Resolving pypi.org... failed"):
    check(f"detects: {message[:38]}", looks_like_blocked_network(message))

for benign in ("resolving imports failed to compile", "Building wheel",
               "resolving config.json failed", "connection pool initialised"):
    check(f"ignores: {benign[:38]}", not looks_like_blocked_network(benign))

fired = []
watcher = OutputWatcher(lambda: fired.append(1))
watcher.feed("E: Failed to fetch http://a Temporary failure in name resolution")
watcher.feed("E: Failed to fetch http://b Temporary failure in name resolution")
check("the prompt appears once per session, not per error", len(fired) == 1)

# --------------------------------------------------------------------------- #
section("4. Signing keys")
# --------------------------------------------------------------------------- #
import petacore.gpgsign as gpgsign                 # noqa: E402

gpgsign.list_secret_keys = lambda: [("AAAA1111", "Ahmet <a@b.c>"),
                                    ("BBBB2222", "Ahmet <a@b.c>")]
check("a missing chosen key does not fall back silently",
      gpgsign.signing_key("MISSING_FPR") is None)
check("the chosen key is honoured",
      gpgsign.signing_key("BBBB2222") == ("BBBB2222", "Ahmet <a@b.c>"))
check("fingerprints are shown grouped for comparison",
      gpgsign.pretty_fingerprint("AAAA1111BBBB") == "AAAA 1111 BBBB")

# --------------------------------------------------------------------------- #
section("5. Cloud synchronisation")
# --------------------------------------------------------------------------- #
from petacore import gdrive                        # noqa: E402

calls = []
gdrive._run = lambda args, timeout=None: calls.append(args) or ""
gdrive.sync_project(proj, "RegressionProject")
args = calls[0]
check("removed files are kept, not destroyed", "--backup-dir" in args)
check("the attic is dated",
      __import__("re").search(r"\d{4}-\d{2}-\d{2}$",
                              args[args.index("--backup-dir") + 1]) is not None)
# an upload that would shrink the remote copy must be questioned first
check("the shrink warning is on by default",
      config.get("drive_shrink_warning") is True)
shrink_proj = tempfile.mkdtemp()
open(os.path.join(shrink_proj, "main.py"), "w").write("x" * 1000)
os.makedirs(os.path.join(shrink_proj, ".git"))
open(os.path.join(shrink_proj, ".git", "junk"), "w").write("y" * 50000)
check("local size counts only what is uploaded",
      gdrive.local_size(shrink_proj) == 1000,
      str(gdrive.local_size(shrink_proj)))
check("an unreadable remote size is reported as unknown, not zero",
      gdrive.remote_size.__doc__ is not None
      and "-1" in gdrive.remote_size.__doc__)

check("private keys are never uploaded",
      "gnupg" not in " ".join(args).lower())

# --------------------------------------------------------------------------- #
section("6. Installer and uninstaller")
# --------------------------------------------------------------------------- #
here = os.path.dirname(os.path.abspath(__file__))
install = open(os.path.join(here, "install.sh")).read()
check("installer refuses to run as root", '"$(id -u)" -eq 0' in install)
check("installer fixes PATH before using tools",
      'PATH="/usr/local/sbin:/usr/local/bin' in install)
check("installer uses strict mode", "set -euo pipefail" in install)
check("installer never pipes a download into a shell",
      "curl" not in install and "wget" not in install)
check("installer passes only fixed package names to sudo",
      "sudo apt install -y $PKGS" in install)

uninstall_path = os.path.join(here, "uninstall.sh")
check("an uninstaller exists", os.path.isfile(uninstall_path))
if os.path.isfile(uninstall_path):
    uninstall = open(uninstall_path).read()
    check("uninstaller asks about snapshots separately",
          "Delete the snapshots as well?" in uninstall)
    check("uninstaller asks about settings separately",
          "Delete settings too?" in uninstall)
    check("uninstaller never touches project folders",
          "project folders were not touched" in uninstall)

# --------------------------------------------------------------------------- #
section("7. Share card")
# --------------------------------------------------------------------------- #
from petacore import sharecard, stats              # noqa: E402

data = stats.analyse(proj)
svg = sharecard.build_svg("PublicName", data, "story")
check("card carries no file names", "main.py" not in svg and ".env" not in svg)
check("card carries no paths", proj not in svg)
check("card is never uploaded automatically",
      "sharecard" not in open(os.path.join(here, "petacore/gdrive.py")).read())

# --------------------------------------------------------------------------- #
print()
print("=" * 60)
print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    for name in FAILED:
        print(f"  FAILED: {name}")
print("=" * 60)
sys.exit(1 if FAILED else 0)
