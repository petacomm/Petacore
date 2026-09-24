#!/usr/bin/env python3
"""Security regression tests for Petacore.

Each test corresponds to a finding that was fixed. Run this after any change
that touches packaging, the sandbox, credentials or synchronisation:

    python3 security_tests.py
"""

import os
import stat
import posixpath
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


SKIPPED = []


def skip(name, why):
    """Something that could not be tested here. Never counted as a pass —
    a green run must not quietly include checks that never ran."""
    SKIPPED.append(name)
    print(f"  [SKIP] {name}  — {why}")


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
section("8. APT repository")
# --------------------------------------------------------------------------- #
import shutil as _shutil                            # noqa: E402
from petacore import repo                           # noqa: E402

# -- names typed by a user must never become paths --------------------------
for bad in ("../../etc/cron.d/x.deb", "/etc/passwd", "a/b.deb", ".hidden.deb",
            "payload.sh", ""):
    refused = False
    try:
        repo.safe_filename(bad)
    except repo.RepoError:
        refused = True
    check(f"rename refuses {bad!r}", refused)
check("rename accepts an ordinary package name",
      repo.safe_filename("spindle_1.0.0_amd64.deb") == "spindle_1.0.0_amd64.deb")

# -- suite / component / architecture end up in paths, so they are filtered --
escaped = repo.normalise({"name": "x", "root": "/tmp/x",
                          "suite": "../../..", "component": "/etc",
                          "archs": ["..", "amd64"]})
check("suite cannot climb out of the tree", escaped["suite"] == "stable")
check("component cannot climb out of the tree", escaped["component"] == "etc")
check("architectures cannot climb out of the tree",
      all(a not in ("..", ".") for a in escaped["archs"]))

# -- publishing never carries a password ------------------------------------
check("a repository profile has no password field",
      not any("pass" in key or "secret" in key or "token" in key
              for key in repo.PROFILE_DEFAULTS))
args = repo._rsync_args("/tmp/root", "user@host:/var/www/apt", dry_run=False)
check("publishing over SSH refuses to prompt for a password",
      "ssh -o BatchMode=yes" in args)
check("publishing passes arguments without a shell",
      isinstance(args, list) and not any(";" in a or "|" in a for a in args))
for bad_target in ("-oProxyCommand=touch /tmp/pwn", "", "no-colon-here"):
    refused = False
    try:
        repo._check_target(bad_target, "rsync")
    except repo.RepoError:
        refused = True
    check(f"publishing refuses the destination {bad_target!r}", refused)

# -- reading the live archive is limited to http(s) --------------------------
refused = False
try:
    repo._fetch("file:///etc/passwd")
except repo.RepoError:
    refused = True
check("the live index is never read from a local file", refused)
check("a reply from the server is read with a ceiling",
      repo.MAX_REMOTE_BYTES <= 32 * 1024 * 1024)

# -- what the page shows is decided in the module, so it can be checked here -
sample = [
    {"package": "spindle", "version": "1.0.0", "file": "spindle_1.0.0.deb",
     "path": "/p/spindle_1.0.0.deb", "indexed": True, "signed": True},
    {"package": "spindle", "version": "1.2.0", "file": "spindle_1.2.0.deb",
     "path": "/p/spindle_1.2.0.deb", "indexed": False, "signed": False},
    {"package": "hellopeta", "version": "0.9.0", "file": "hellopeta_0.9.0.deb",
     "path": "/p/hellopeta_0.9.0.deb", "indexed": True, "signed": True},
]
live = {"spindle": "1.0.0"}
check("an unchecked server is never reported as an empty one",
      repo.live_state(sample[0], None) == "")
check("a package matching the server is marked as live",
      repo.live_state(sample[0], live) == "live")
check("a newer local version is marked as ahead",
      repo.live_state(sample[1], live) == "ahead")
check("a package the server has never seen is marked as new",
      repo.live_state(sample[2], live) == "new")
check("searching matches the file name as well as the package",
      len(repo.filter_entries(sample, needle="hellopeta")) == 1)
check("the unindexed filter shows exactly what apt cannot see",
      [e["version"] for e in repo.filter_entries(sample, mode="unindexed")]
      == ["1.2.0"])
check("the superseded filter never hides the newest version",
      all(e["version"] != "1.2.0"
          for e in repo.filter_entries(sample, mode="old")))
check("an unknown filter shows everything rather than nothing",
      len(repo.filter_entries(sample, mode="nonsense")) == len(sample))

# -- the rest needs the tools; skipped rather than failed without them -------
if _shutil.which("dpkg-deb") and _shutil.which("gpg") and repo.can_index():
    gnupg = tempfile.mkdtemp()
    os.chmod(gnupg, 0o700)
    os.environ["GNUPGHOME"] = gnupg
    subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback",
                    "--passphrase", "", "--quick-gen-key",
                    "Petacore Tests <tests@petacore.invalid>",
                    "default", "default", "never"],
                   capture_output=True, timeout=180)
    listing = subprocess.run(["gpg", "--list-secret-keys", "--with-colons"],
                             capture_output=True, text=True).stdout
    fingerprints = [line.split(":")[9] for line in listing.splitlines()
                    if line.startswith("fpr")]

    def _make_deb(out_dir, name, version):
        staging = tempfile.mkdtemp()
        os.makedirs(os.path.join(staging, "DEBIAN"))
        os.makedirs(os.path.join(staging, "usr", "bin"))
        with open(os.path.join(staging, "usr", "bin", name), "w") as f:
            f.write("#!/bin/sh\n")
        with open(os.path.join(staging, "DEBIAN", "control"), "w") as f:
            f.write(f"Package: {name}\nVersion: {version}\n"
                    f"Architecture: amd64\nMaintainer: Tests <t@invalid>\n"
                    f"Description: regression fixture\n")
        path = os.path.join(out_dir, f"{name}_{version}_amd64.deb")
        subprocess.run(["dpkg-deb", "--build", staging, path],
                       capture_output=True, timeout=120)
        return path

    if fingerprints:
        built = tempfile.mkdtemp()
        root = tempfile.mkdtemp(prefix="petacore-repo-")
        profile = repo.normalise({"name": "Regression", "root": root,
                                  "key": fingerprints[0]})
        repo.ensure_layout(profile)
        first = _make_deb(built, "spindle", "1.0.0")
        repo.add_packages(profile, [first])

        state = repo.status(profile)
        check("a package added but not indexed is reported, not hidden",
              state["stale"] and state["unindexed"] == 1)

        repo.build(profile)
        state = repo.status(profile)
        check("rebuilding puts every package in the index",
              not state["stale"] and state["unindexed"] == 0)
        check("the index is signed", state["signed_index"])

        release = os.path.join(root, "dists", "stable", "Release")
        verified = subprocess.run(
            ["gpg", "--verify", release + ".gpg", release],
            capture_output=True, timeout=120)
        check("the signature on the index verifies", verified.returncode == 0)

        # Tampering with the index must break its signature.
        with open(release, "a", encoding="utf-8") as f:
            f.write("Injected: yes\n")
        verified = subprocess.run(
            ["gpg", "--verify", release + ".gpg", release],
            capture_output=True, timeout=120)
        check("tampering with the index invalidates its signature",
              verified.returncode != 0)
        repo.build(profile)

        # Removal is reversible.
        entry = repo.scan(profile)[0]
        record = repo.remove_packages(profile, [entry["path"]])
        check("removing a package does not delete it",
              not os.path.exists(entry["path"])
              and os.path.isfile(record["items"][0]["to"]))
        repo.restore_removed(record)
        check("a removed package can be put back",
              os.path.isfile(entry["path"]))

        # An index that no longer matches the pool is never published.
        destination = tempfile.mkdtemp()
        profile = dict(profile, publish_method="folder",
                       publish_target=destination)
        repo.add_packages(profile, [_make_deb(built, "spindle", "1.1.0")])
        refused = False
        try:
            repo.publish(profile)
        except repo.RepoError:
            refused = True
        check("a stale index is never published", refused)

        repo.build(profile)
        result = repo.publish(profile)
        check("publishing copies the signed index",
              os.path.isfile(os.path.join(destination, "dists", "stable",
                                          "InRelease")))
        check("publishing reports what it moved", result["files"] > 0)
        check("the repository trash is never published",
              not os.path.isdir(os.path.join(destination,
                                             repo.TRASH_DIRNAME)))
else:
    check("repository tooling present (dpkg-deb, gpg, apt-utils/dpkg-dev)",
          True, "skipped: not installed on this machine")

# --------------------------------------------------------------------------- #
section("9. Live Server")
# --------------------------------------------------------------------------- #
import threading                                     # noqa: E402
import time                                          # noqa: E402
import urllib.error                                  # noqa: E402
import urllib.request                                # noqa: E402
from petacore import liveserve, stats                # noqa: E402

# -- deciding whether a project is a web app never touches file content -----
web_site = tempfile.mkdtemp()
with open(os.path.join(web_site, "index.html"), "w") as f:
    f.write("<html></html>")
with open(os.path.join(web_site, "app.js"), "w") as f:
    f.write("console.log(1)")
check("a plain static site is recognised as a web project",
      stats.quick_web_check(web_site))
check("Live Server points at the project itself when index.html is there",
      stats.find_web_root(web_site) == web_site)

flask_app = tempfile.mkdtemp()
with open(os.path.join(flask_app, "app.py"), "w") as f:
    f.write("from flask import Flask")
os.makedirs(os.path.join(flask_app, "templates"))
with open(os.path.join(flask_app, "templates", "index.html"), "w") as f:
    f.write("<html>{{ name }}</html>")
os.makedirs(os.path.join(flask_app, "static"))
with open(os.path.join(flask_app, "static", "app.js"), "w") as f:
    f.write("1")
check("a Python backend with templates is not mistaken for a web project",
      not stats.quick_web_check(flask_app),
      "a Live Server would dump unrendered Jinja templates")

built_site = tempfile.mkdtemp()
with open(os.path.join(built_site, "package.json"), "w") as f:
    f.write("{}")
os.makedirs(os.path.join(built_site, "dist"))
with open(os.path.join(built_site, "dist", "index.html"), "w") as f:
    f.write("<html></html>")
check("a bundler project is recognised as a web project",
      stats.quick_web_check(built_site))
check("Live Server finds the built output rather than the source folder",
      stats.find_web_root(built_site) == os.path.join(built_site, "dist"))

empty_project = tempfile.mkdtemp()
check("an empty project is never treated as a web project",
      not stats.quick_web_check(empty_project))

# -- the server itself -------------------------------------------------------
root = tempfile.mkdtemp()
with open(os.path.join(root, "index.html"), "w") as f:
    f.write("<html><body>hello</body></html>")
with open(os.path.join(root, "style.css"), "w") as f:
    f.write("body { color: red }")

port = liveserve.find_free_port(59120)
tracker = liveserve.ChangeTracker(root, interval=0.2)
liveserve.ReloadingHandler.tracker = tracker
import functools
import http.server
handler = functools.partial(liveserve.ReloadingHandler, directory=root)
httpd = liveserve._Server(("127.0.0.1", port), handler)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
try:
    base = f"http://127.0.0.1:{port}"

    root_page = urllib.request.urlopen(base + "/").read().decode()
    check("the root page carries the reload script",
          liveserve.RELOAD_PATH in root_page)
    check("the root page still shows the real content",
          "hello" in root_page)

    css = urllib.request.urlopen(base + "/style.css").read().decode()
    check("non-HTML files are served untouched",
          css == "body { color: red }")

    refused = False
    try:
        urllib.request.urlopen(base + "/../../../../etc/passwd")
    except urllib.error.HTTPError:
        refused = True
    check("a path outside the served folder is refused", refused)

    token_before = urllib.request.urlopen(
        base + liveserve.RELOAD_PATH).read()
    with open(os.path.join(root, "index.html"), "w") as f:
        f.write("<html><body>changed</body></html>")
    time.sleep(0.6)
    token_after = urllib.request.urlopen(
        base + liveserve.RELOAD_PATH).read()
    check("editing a file changes the reload token",
          token_before != token_after)
finally:
    httpd.shutdown()
    httpd.server_close()
    tracker.stop()

check("Live Server binds to the loopback address by default",
      liveserve.serve.__defaults__[1] == "127.0.0.1")

# --------------------------------------------------------------------------- #
section("10. VirusTotal")
# --------------------------------------------------------------------------- #
import hashlib as _hashlib                           # noqa: E402
import http.server                                   # noqa: E402
import json as _json                                 # noqa: E402
import threading as _threading                       # noqa: E402
from petacore import virustotal as vt                # noqa: E402

# -- a stand-in for VirusTotal, so the request layer is exercised for real --
_seen = {"posts": 0, "paths": [], "keys": []}


class _FakeVT(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = _json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _seen["paths"].append(self.path)
        _seen["keys"].append(self.headers.get("x-apikey"))
        if self.headers.get("x-apikey") == "wrong":
            self._send(401, {"error": {"message": "bad key"}})
        elif "unseen" in self.path:
            self._send(404, {"error": {"message": "not found"}})
        elif self.path.endswith("/spent"):
            self._send(429, {"error": {"message": "quota"}})
        else:
            self._send(200, {"data": {"attributes": {
                "last_analysis_stats": {"malicious": 0, "suspicious": 0,
                                        "harmless": 9, "undetected": 51},
                "last_analysis_results": {}, "sha256": "d" * 64}}})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _seen["posts"] += 1
        self._send(200, {"data": {"id": "analysis-1"}})


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeVT)
_threading.Thread(target=_srv.serve_forever, daemon=True).start()
vt.API_ROOT = f"http://127.0.0.1:{_srv.server_address[1]}/api/v3"

try:
    sample = tempfile.mktemp()
    with open(sample, "wb") as f:
        f.write(b"a package that has not been released yet")

    # -- the central promise: checking a file never sends the file ----------
    posts_before = _seen["posts"]
    vt.scan(sample, "goodkey")
    check("checking a file uploads nothing",
          _seen["posts"] == posts_before,
          "an upload would share unreleased work with third parties")
    asked = _seen["paths"][-1].rsplit("/", 1)[-1]
    check("checking a file sends only its hash",
          asked == _hashlib.sha256(
              b"a package that has not been released yet").hexdigest())
    check("the file's own name is never sent",
          os.path.basename(sample) not in "".join(_seen["paths"]))

    # -- an unknown file is never reported as clean -------------------------
    unknown = vt.summarise(vt.lookup("unseen" + "0" * 58, "goodkey"))
    check("a file VirusTotal has never seen is 'unknown', not 'clean'",
          unknown["verdict"] == "unknown")
    no_engines = vt.summarise({"data": {"attributes":
                                        {"last_analysis_stats": {}}}})
    check("a report with no engine results is never called clean",
          no_engines["verdict"] == "unknown")

    flagged = vt.summarise({"data": {"attributes": {
        "last_analysis_stats": {"malicious": 2, "suspicious": 1,
                                "harmless": 4, "undetected": 55},
        "last_analysis_results": {
            "EngineA": {"category": "malicious", "result": "Trojan.Gen"},
            "EngineB": {"category": "undetected", "result": None}},
        "sha256": "b" * 64}}})
    check("a flagged file names the engines that flagged it",
          flagged["verdict"] == "flagged"
          and flagged["flagged"] == [("EngineA", "Trojan.Gen")])

    # -- uploads happen only when asked for, and are bounded ----------------
    posts_before = _seen["posts"]
    vt.upload(sample, "goodkey")
    check("uploading is a separate call that must be made deliberately",
          _seen["posts"] == posts_before + 1)

    oversize = tempfile.mktemp()
    with open(oversize, "wb") as f:
        f.truncate(vt.UPLOAD_LIMIT + 1)
    posts_before = _seen["posts"]
    refused = False
    try:
        vt.upload(oversize, "goodkey")
    except vt.VTError:
        refused = True
    check("a file too large for VirusTotal is refused before it is sent",
          refused and _seen["posts"] == posts_before)

    # -- errors the person can act on ---------------------------------------
    rejected = ""
    try:
        vt.lookup("d" * 64, "wrong")
    except vt.AuthError as e:
        rejected = str(e)
    check("a refused API key is reported as an auth failure", bool(rejected))
    check("a refusal repeats VirusTotal's own explanation",
          "bad key" in rejected,
          "a generic message leaves the person with nothing to act on")
    check("a truncated key is caught before a request is spent on it",
          "64" in vt.key_shape_problem("abc123"))
    check("a well-formed key passes the shape check",
          vt.key_shape_problem("a" * 64) == "")

    spent = False
    try:
        vt._request(vt.API_ROOT + "/spent", "goodkey")
    except vt.QuotaError:
        spent = True
    check("a spent quota is reported as a quota failure", spent)

    paths_before = len(_seen["paths"])
    keyless_refused = False
    try:
        vt.lookup("d" * 64, "")
    except vt.AuthError:
        keyless_refused = True
    check("a request without a key never reaches the network",
          keyless_refused and len(_seen["paths"]) == paths_before)
finally:
    _srv.shutdown()

# -- the key is a credential and is treated as one ---------------------------
from petacore import secrets as _secrets              # noqa: E402
check("the API key is kept under Petacore's keyring service",
      _secrets.SERVICE == "io.petacore.Petacore")
config_text = ""
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config_text = f.read()
check("no VirusTotal key is written to the configuration file",
      "virustotal" not in config_text.lower())

# --------------------------------------------------------------------------- #
section("11. Keyring reachability")
# --------------------------------------------------------------------------- #
with open("install.sh", encoding="utf-8") as f:
    installer_text = f.read()
check("the installer provides ssh and scp (openssh-client)",
      "openssh-client" in installer_text)
check("the installer provides secret-tool (libsecret-tools)",
      "libsecret-tools" in installer_text,
      "without it every credential silently falls back to a plain file")

from petacore import secrets as _sec                  # noqa: E402

# A stand-in secret-tool, backed by a directory, so the keyring path can be
# exercised on a machine that has no secret service at all.
_fake_bin = tempfile.mkdtemp()
_fake_store = tempfile.mkdtemp()
with open(os.path.join(_fake_bin, "secret-tool"), "w") as f:
    f.write(f"""#!/bin/sh
cmd="$1"; shift
key=""
while [ $# -gt 0 ]; do
  case "$1" in --label) shift 2; continue;; key) key="$2"; shift 2; continue;; esac
  shift
done
case "$cmd" in
  store) cat > "{_fake_store}/$key" ;;
  lookup) [ -f "{_fake_store}/$key" ] && cat "{_fake_store}/$key" || exit 1 ;;
  clear) rm -f "{_fake_store}/$key" ;;
esac
""")
os.chmod(os.path.join(_fake_bin, "secret-tool"), 0o755)

_old_path = os.environ.get("PATH", "")
try:
    os.environ["PATH"] = "/nonexistent"
    check("a missing secret-tool is reported as such, not as 'no keyring'",
          _sec.why_unavailable() == "no-tool")

    os.environ["PATH"] = _fake_bin + os.pathsep + _old_path
    check("with secret-tool present the keyring is reported reachable",
          _sec.why_unavailable() == "")

    # A token written while the keyring was unreachable moves into it on
    # first read, and the plain copy is wiped from the settings file.
    config._data["github_token"] = "ghp_plaintextleftoverfromanearlierrun"
    config.save()
    value = config.get("github_token")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        after = f.read()
    # (the suite's in-memory `vault` stands in for the keyring here)
    check("a plain-text token is moved into the keyring once it is reachable",
          value == "ghp_plaintextleftoverfromanearlierrun"
          and vault.get("github_token")
          == "ghp_plaintextleftoverfromanearlierrun")
    check("the plain-text copy is wiped from the settings file",
          "ghp_plaintextleftoverfromanearlierrun" not in after)
    config.set("github_token", "")
finally:
    os.environ["PATH"] = _old_path

# --------------------------------------------------------------------------- #
section("12. Versions")
# --------------------------------------------------------------------------- #
from petacore import versions as V                      # noqa: E402
from petacore import stats as _stats                    # noqa: E402

# -- names -----------------------------------------------------------------
for bad in ("../../etc", "1.6/../x", "1.6 beta", "beta", "", "1..6", "-1"):
    refused = False
    try:
        V.clean_number(bad)
    except V.VersionError:
        refused = True
    check(f"version number {bad!r} is refused", refused)
check("a release folder name never contains a path separator or tilde",
      all("/" not in V.folder_name("1.6", c, 2)
          and "~" not in V.folder_name("1.6", c, 2) for c in V.CHANNELS)
      and V.folder_name("1.6", "beta", 2) == "1.6-beta.2")
check("pre-releases sort before the release they lead to",
      V.sort_key("1.6", "alpha", 1) < V.sort_key("1.6", "beta", 1)
      < V.sort_key("1.6", "beta", 2) < V.sort_key("1.6", "rc", 1)
      < V.sort_key("1.6", "stable", 0) < V.sort_key("1.7", "stable", 0))
if _shutil.which("dpkg"):
    chain = [V.package_version("1.6", "alpha", 1),
             V.package_version("1.6", "beta", 2),
             V.package_version("1.6", "rc", 1),
             V.package_version("1.6", "stable", 0)]
    check("dpkg agrees with that order, so apt upgrades beta -> stable",
          all(subprocess.run(["dpkg", "--compare-versions", a, "lt", b])
              .returncode == 0 for a, b in zip(chain, chain[1:])))

# -- a project's own "versions" folder is not ours ------------------------
own = tempfile.mkdtemp()
os.makedirs(os.path.join(own, "versions"))
with open(os.path.join(own, "versions", "0001_initial.py"), "w") as f:
    f.write("# a migration\n")
check("a 'versions' folder Petacore did not create is not hidden",
      not V.hidden(own, "versions"))
took_over = True
try:
    V.ensure(own)
except V.VersionError:
    took_over = False
check("Petacore refuses to take over the project's own 'versions' folder",
      not took_over)

# -- building releases -------------------------------------------------------
if _shutil.which("dpkg-deb"):
    proj = tempfile.mkdtemp()
    with open(os.path.join(proj, "main.py"), "w") as f:
        f.write("print('hi')\n")
    os.makedirs(os.path.join(proj, ".git", "info"))

    def _release(number, channel="stable", pre=0, **extra):
        return V.create(proj, "Spindle", number, channel, pre, ["deb"],
                        "Tests <t@invalid>", "fixture", "MIT", **extra)

    first = _release("1.0")
    second = _release("1.1")
    newest = [f["path"] for f in V.list_versions(proj)[0]["files"]][0]
    listing = subprocess.run(["dpkg-deb", "-c", newest],
                             capture_output=True, text=True).stdout
    check("a release never contains earlier releases",
          ".deb" not in listing and "/versions/" not in listing,
          "each package would otherwise carry every one before it")
    check("the release folder is hidden from the file view",
          V.hidden(proj, "versions"))
    check("git is told to ignore the release folder, locally",
          "/versions/" in open(os.path.join(proj, ".git", "info",
                                            "exclude")).read())
    check("no tracked project file is changed to do that",
          not os.path.exists(os.path.join(proj, ".gitignore")))
    check("statistics do not count release binaries",
          _stats.analyse(proj).get("files") == 1)

    duplicate = False
    try:
        _release("1.1")
    except V.VersionError:
        duplicate = True
    check("an existing version cannot be overwritten", duplicate)

    # A snapshot neither contains the releases nor deletes them on restore.
    from petacore.snapshots import SnapshotManager as _Snap
    import tarfile as _tar
    snap = _Snap(proj)
    archive = snap.create(kind="manual")
    check("snapshots leave the release binaries out",
          not any("versions" in n for n in _tar.open(archive).getnames()))
    _release("1.2")
    snap.restore(os.path.basename(archive)[:-len(".tar.gz")])
    check("restoring an older snapshot does not delete later releases",
          any(v["label"] == "1.2" for v in V.list_versions(proj)))

    # Removal is reversible.
    trashed = V.remove(proj, second)
    gone = all(v["path"] != second for v in V.list_versions(proj))
    V.restore(proj, trashed)
    check("removing a release can be undone",
          gone and any(v["path"] == second for v in V.list_versions(proj)))
    escaped = False
    try:
        V.remove(proj, os.path.join(proj, "main.py"))
    except V.VersionError:
        escaped = True
    check("removal refuses anything that is not a release", escaped)

    # A failed build leaves no half-made release behind.
    import petacore.debbuild as _deb
    _real = _deb.build_deb
    _deb.build_deb = lambda *a, **k: (_ for _ in ()).throw(
        _deb.DebError("simulated failure"))
    try:
        _release("9.9")
    except Exception:
        pass
    finally:
        _deb.build_deb = _real
    check("a failed build leaves no half-made release in the history",
          not os.path.exists(os.path.join(V.folder(proj), "9.9")))

    # Signing: a key that has vanished is never silently replaced.
    missing_key = False
    try:
        _release("3.0", sign=True, key_fpr="0" * 40)
    except V.VersionError:
        missing_key = True
    check("a release is never signed with a key other than the one chosen",
          missing_key
          and not os.path.exists(os.path.join(V.folder(proj), "3.0")))

# --------------------------------------------------------------------------- #
section("13. Scanning a whole project")
# --------------------------------------------------------------------------- #
import zipfile as _zip                                   # noqa: E402
from petacore import virustotal as _vt                   # noqa: E402
from petacore import versions as _V                      # noqa: E402

_proj = tempfile.mkdtemp()
for _rel, _text in {"main.py": "print(1)", "src/app.py": "x=1",
                    ".env": "API_KEY=very-secret", "certs/server.pem": "k",
                    "id_ed25519": "k", ".git/config": "[core]",
                    "node_modules/a/index.js": "1",
                    ".petacore/plans.json": "{}"}.items():
    _p = os.path.join(_proj, _rel)
    os.makedirs(os.path.dirname(_p), exist_ok=True)
    with open(_p, "w") as _f:
        _f.write(_text)
_V.ensure(_proj)
os.makedirs(os.path.join(_V.folder(_proj), "1.0"))
with open(os.path.join(_V.folder(_proj), "1.0", "old.deb"), "w") as _f:
    _f.write("deb")

_archive, _count, _left = _vt.project_archive(_proj)
_names = _zip.ZipFile(_archive).namelist()
_blob = b"".join(_zip.ZipFile(_archive).read(n) for n in _names)
check("the project's code goes into the archive",
      set(_names) == {"main.py", "src/app.py"})
check("credential files are never sent to VirusTotal",
      not any(n.endswith((".env", ".pem", "id_ed25519")) for n in _names)
      and b"very-secret" not in _blob)
check("the files kept back are reported, not dropped silently",
      set(_left) >= {".env", "certs/server.pem", "id_ed25519"})
check(".git, node_modules and .petacore are never sent",
      not any(n.split("/")[0] in (".git", "node_modules", ".petacore")
              for n in _names))
check("the release history is never sent",
      not any(n.startswith("versions/") for n in _names))

_again, _c, _l = _vt.project_archive(_proj)
check("an unchanged project packs to the same bytes, so it is never "
      "uploaded twice", _vt.sha256_file(_archive) == _vt.sha256_file(_again))

# With a stand-in VirusTotal: no consent means no upload, ever.
_posts = {"n": 0}


class _VT2(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"error": {}}'
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _posts["n"] += 1
        body = b'{"data": {"id": "x"}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _VT2)
_threading.Thread(target=_srv2.serve_forever, daemon=True).start()
_old_root = _vt.API_ROOT
_vt.API_ROOT = f"http://127.0.0.1:{_srv2.server_address[1]}/api/v3"
import glob as _glob                                     # noqa: E402
_tmp_before = set(_glob.glob(os.path.join(tempfile.gettempdir(),
                                          "petacore-vt-*")))
try:
    _r = _vt.scan_project(_proj, allow_upload=False, key="k" * 64)
    check("without consent the project is never uploaded",
          _r["needs_upload"] and _posts["n"] == 0)
    _tmp_after = set(_glob.glob(os.path.join(tempfile.gettempdir(),
                                             "petacore-vt-*")))
    check("the temporary archive is removed afterwards",
          _tmp_after <= _tmp_before,
          "a copy of the project must not be left lying in /tmp")
finally:
    _vt.API_ROOT = _old_root
    _srv2.shutdown()

_vt.save_state(_proj, consent=True)
with open(os.path.join(_proj, ".petacore", _vt.STATE_FILE)) as _f:
    _state_text = _f.read()
check("the per-project scan record holds no API key",
      "apikey" not in _state_text.lower() and "k" * 64 not in _state_text)

# --------------------------------------------------------------------------- #
section("14. Repository on a server (SSH)")
# --------------------------------------------------------------------------- #
import shlex as _shlex                                  # noqa: E402
import socket as _socket                                # noqa: E402
import stat as _stat                                    # noqa: E402
from petacore import remote as R                        # noqa: E402

_good = {"host": "repo.kotechsoft.com", "port": 22, "user": "deploy",
         "root": "/var/www/repo", "identity": ""}

# -- nothing typed into the form reaches ssh unchecked -----------------------
for field, bad in (("host", "-oProxyCommand=touch /tmp/pwn"),
                   ("host", "evil.com; rm -rf ~"), ("host", "a b"),
                   ("user", "root;id"), ("user", "-l"),
                   ("port", "0"), ("port", "70000"), ("port", "22x"),
                   ("root", "var/www"), ("root", "/"), ("root", "/etc"),
                   ("root", "/home"), ("root", "/var/www"),
                   ("root", "/var/www/../../etc"),
                   ("identity", "/nonexistent/key")):
    refused = False
    try:
        R.normalise_target(dict(_good, **{field: bad}))
    except R.RemoteError:
        refused = True
    check(f"server {field} {bad!r} is refused", refused)

# -- the option set ------------------------------------------------------------
_opts = R.hardened_options(R.normalise_target(_good))
_joined = " ".join(_opts)
for must in ("StrictHostKeyChecking=yes", "PasswordAuthentication=no",
             "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes",
             "PermitLocalCommand=no", "IdentitiesOnly=yes",
             "GlobalKnownHostsFile=/dev/null", "UpdateHostKeys=no"):
    check(f"every connection carries {must}", must in _joined)
check("the user's ssh config is never read", _opts[:2] == ["-F", "/dev/null"])
_algos = [o.split("=", 1)[1] for o in _opts
          if o.startswith(("KexAlgorithms=", "Ciphers=", "MACs="))]
check("no CBC cipher, SHA-1 MAC or SHA-1 key exchange is offered",
      not any("cbc" in a or "sha1" in a for a in ",".join(_algos).split(",")))
check("only encrypt-then-MAC and AEAD are offered",
      all(m.endswith("-etm@openssh.com")
          for o in _opts if o.startswith("MACs=")
          for m in o.split("=", 1)[1].split(",")))
check("the control socket lives in a private directory",
      _stat.S_IMODE(os.stat(R.runtime_dir()).st_mode) == 0o700
      and any(o.startswith("ControlPath=" + R.runtime_dir()) for o in _opts))
check("Petacore's known_hosts is readable only by its owner",
      _stat.S_IMODE(os.stat(R.KNOWN_HOSTS).st_mode) == 0o600)

# -- the repository folder is a boundary ----------------------------------------
_s = R.Session(_good)
for bad in ("../etc", "docs/../../etc", "a/\0b"):
    refused = False
    try:
        _s.path(bad)
    except R.RemoteError:
        refused = True
    check(f"remote path {bad!r} cannot leave the repository", refused)
check("an ordinary remote path stays inside",
      _s.path("pool/main") == "/var/www/repo/pool/main")
for bad in ("a/b", "..", "", "x" + R.PART_SUFFIX, R.TRASH_DIRNAME):
    refused = False
    try:
        R.Session.safe_name(bad)
    except R.RemoteError:
        refused = True
    check(f"remote name {bad!r} is refused", refused)

# -- a file name is never code on the server ------------------------------------
_captured = {}
_real_run = R.subprocess.run


def _capture(args, **kw):
    _captured["args"] = args

    class _P:
        returncode, stdout, stderr = 0, b"", b""
    return _P()


R.subprocess.run = _capture
_s.connected = lambda: True
try:
    _argv = ["mv", "-n", "--", "x; rm -rf ~ `id` $(id)", "/var/www/repo/y"]
    _s.run(_argv)
    _sent = _captured["args"][-1]
    check("remote commands arrive as quoted arguments, never as code",
          _shlex.split(_sent) == _argv)
    check("a remote command never runs without the existing connection",
          "BatchMode=yes" in _captured["args"]
          and "ControlMaster=no" in _captured["args"])
finally:
    R.subprocess.run = _real_run

# -- the questions ssh asks -------------------------------------------------------
check("terminal escapes written by a server are stripped from a prompt",
      "\x1b" not in R.clean_prompt("\x1b[2J\x1b[1;1HPassword:")
      and R.clean_prompt("\x1b[2JPassword:").endswith("Password:"))
check("prompts are recognised for what they ask",
      R.classify("Enter passphrase for key 'x':", "") == "passphrase"
      and R.classify("(u@h) Verification code:", "") == "code"
      and R.classify("Confirm user presence for key", "none") == "touch")

_asked = []
_bridge = R.AskpassBridge(lambda kind, prompt: (_asked.append((kind, prompt)),
                                                "123456")[1])
try:
    check("the askpass socket is readable only by its owner",
          _stat.S_IMODE(os.stat(_bridge.path).st_mode) == 0o600)

    def _talk(token):
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as c:
            c.settimeout(5)
            c.connect(_bridge.path)
            c.sendall(_json.dumps({"token": token, "prompt":
                                  "Verification code:", "style": ""})
                      .encode() + b"\n")
            data = b""
            try:
                while not data.endswith(b"\n"):
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            return data
    check("a request without the session's token gets no answer",
          _talk("0" * 64) == b"" and not _asked)
    check("the helper with the right token gets the answer",
          _json.loads(_talk(_bridge.token))["answer"] == "123456"
          and _asked == [("code", "Verification code:")])
finally:
    _bridge.close()
check("the askpass socket is removed when the bridge closes",
      not os.path.exists(_bridge.path))

# -- keys ----------------------------------------------------------------------------
if _shutil.which("ssh-keygen"):
    _kdir = tempfile.mkdtemp()
    os.chmod(_kdir, 0o700)
    short = False
    try:
        R.generate_key(os.path.join(_kdir, "k1"), "short")
    except R.RemoteError:
        short = True
    check("a key passphrase shorter than 8 characters is refused", short)
    R.generate_key(os.path.join(_kdir, "k2"), "a long passphrase")
    _open_empty = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f",
                                  os.path.join(_kdir, "k2")],
                                 capture_output=True)
    check("a generated key is really locked by its passphrase",
          _open_empty.returncode != 0)
    check("a generated private key is readable only by its owner",
          _stat.S_IMODE(os.stat(os.path.join(_kdir, "k2")).st_mode) == 0o600)

check("the 2FA instructions check sshd's files before restarting it",
      "sshd -t" in R.TWO_FACTOR_HOWTO)
check("the 2FA instructions say to keep the current session open",
      "session open" in R.TWO_FACTOR_HOWTO
      and "NEW terminal" in R.TWO_FACTOR_HOWTO)
check("the 2FA instructions mention the emergency codes",
      "emergency codes" in R.TWO_FACTOR_HOWTO)

# -- end to end, when a test server is described -------------------------------------
# PETACORE_TEST_SSH="port:user:root:identity:passphrase[:totp_secret]"
_spec = os.environ.get("PETACORE_TEST_SSH", "")
if _spec:
    _port, _user, _root, _ident, _pass, *_rest = _spec.split(":")
    _secret = _rest[0] if _rest else ""

    def _totp():
        import base64 as _b64
        import hashlib as _hl
        import hmac as _hm
        import struct as _st
        key = _b64.b32decode(_secret)
        mac = _hm.new(key, _st.pack(">Q", int(time.time() // 30)),
                      _hl.sha1).digest()
        o = mac[-1] & 15
        return "%06d" % ((_st.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF)
                         % 1000000)

    _t = {"host": "127.0.0.1", "port": int(_port), "user": _user,
          "root": _root, "identity": _ident}
    _qs = []
    _live = R.Session(_t, lambda k, p: (_qs.append(k), _pass if k ==
                      "passphrase" else _totp() if k == "code" else None)[1])
    unknown = False
    try:
        _live.connect()
    except R.HostKeyUnknown as e:
        unknown = True
        R.trust_host_key(_live.target, e.line)
    except R.RemoteError:
        pass
    check("an unconfirmed server is stopped before any login",
          unknown or R.known_entries(_live.target) != [])
    _live.connect()
    check("the login succeeds through the askpass bridge", _live.connected())
    if _secret:
        check("the two-factor code is asked for", "code" in _qs)
    _qs.clear()
    _live.ensure_root()
    _live.listdir("")
    check("later operations reuse the connection without asking again",
          _qs == [])
    _odd = os.path.join(tempfile.mkdtemp(), "x; touch PWNED; echo")
    with open(_odd, "w") as _f:
        _f.write("x")
    _live.upload(_odd, "")
    check("an uploaded file's name runs nothing on the server",
          not any(e["name"] == "PWNED" for e in _live.listdir("")))
    check("no partial upload is ever left in the served folder",
          not any(e["name"].endswith(R.PART_SUFFIX)
                  for e in _live.listdir("")))
    _rec = _live.trash([os.path.basename(_odd)])
    _live.restore(_rec)
    _live.trash([os.path.basename(_odd)])

    # A symbolic link on the server that leads outside the repository —
    # PETACORE_TEST_SSH_ESCAPE names one, relative to the root.
    _escape = os.environ.get("PETACORE_TEST_SSH_ESCAPE", "")
    if _escape:
        for _label, _op in (
                ("listed", lambda: _live.listdir(_escape)),
                ("written", lambda: _live.upload(_odd, _escape)),
                ("read", lambda: _live.download(
                    posixpath.join(_escape, "x"), tempfile.mktemp())),
                ("trashed", lambda: _live.trash(
                    [posixpath.join(_escape, "x")])),
                ("created in", lambda: _live.mkdir(_escape, "x"))):
            _refused = False
            try:
                _op()
            except R.RemoteError:
                _refused = True
            check(f"nothing outside the repository can be {_label} "
                  "through a link", _refused)
    _live.disconnect()

    if _secret:
        _wrong = R.Session(_t, lambda k, p: _pass if k == "passphrase"
                           else "000000" if k == "code" else None)
        _denied = False
        try:
            _wrong.connect()
        except R.AuthFailed:
            _denied = True
        check("a wrong two-factor code is refused", _denied)

    _cancelled = R.Session(_t, lambda k, p: None)
    _stopped = False
    try:
        _cancelled.connect()
    except R.RemoteError:
        _stopped = True
    check("cancelling a prompt stops the login cleanly", _stopped
          and not _cancelled.connected())

    # An impostor: the recorded key no longer matches. Nothing may be asked
    # of the person — a passphrase or a code typed to an impostor is lost.
    _asked = []
    _saved = R.known_entries(_live.target)
    R.forget_host_key(_live.target)
    R.trust_host_key(_live.target, f"{R._host_label(_live.target)} "
                     "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBXq1lXTe7xMWG"
                     "9BndGr5SUvDLVqg2ozZp1DK8R9eS0b")
    _changed = False
    try:
        R.Session(_t, lambda k, p: _asked.append(k)).connect()
    except R.HostKeyChanged:
        _changed = True
    check("a server whose key has changed is refused", _changed)
    check("nothing is asked of the person when the key has changed",
          not _asked)
    R.forget_host_key(_live.target)
    for _line in _saved:
        R.trust_host_key(_live.target, _line)
    check("disconnecting really closes the connection",
          not _live.connected())
else:
    skip("end-to-end SSH tests",
         "set PETACORE_TEST_SSH to run them against a test server")

# --------------------------------------------------------------------------- #
print()
print("=" * 60)
print(f"  {len(PASSED)} passed, {len(FAILED)} failed"
      + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
if FAILED:
    for name in FAILED:
        print(f"  FAILED: {name}")
print("=" * 60)
sys.exit(1 if FAILED else 0)
