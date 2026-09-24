"""Managing an APT repository from inside Petacore.

A single .deb handed to someone is always treated as untrusted: the software
centre warns about it, and there is no way to push an update afterwards. A
repository fixes both. It is a plain directory of files served over HTTPS,
with an index that is signed by a GPG key, and once a user has added it the
package behaves like any other — it installs without a warning and updates
arrive through the normal system update.

The layout produced here is the standard one:

    <root>/
      pool/main/<letter>/<package>/<package>_<version>_<arch>.deb
      dists/<suite>/Release            index, signed
      dists/<suite>/InRelease          index with the signature inline
      dists/<suite>/Release.gpg        detached signature
      dists/<suite>/main/binary-<arch>/Packages{,.gz}
      <name>-archive-keyring.asc       the public key users must trust

Nothing here needs a server to run: the tree is built locally and can then
be copied anywhere that serves static files. Publishing that tree, and
reading back what is actually live on the server, is handled at the end of
this module — that is what makes the page in the application a view of the
repository rather than of a folder that happens to resemble one.

Three rules run through the whole file:

  * The pool is the truth. Every listing is produced by walking the pool on
    disk, never from a cached database, so what the user sees is what apt
    would see.
  * Nothing is deleted outright. Removing a package moves it into a dated
    folder inside the repository, so a mistake can be undone.
  * The indexes are never left half-written. They are regenerated in one
    pass and signed immediately; a repository whose Release file no longer
    matches its Packages file is refused by apt outright, which is a much
    worse failure than not publishing at all.
"""

import gzip
import hashlib
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_SUITE = "stable"
DEFAULT_COMPONENT = "main"
DEFAULT_ARCH = "amd64"
KEYRING_NAME = "petacore-archive-keyring.asc"
TRASH_DIRNAME = ".petacore-trash"

# Anything fetched from a server is read with a ceiling on it: a Packages
# index for a personal archive is a few kilobytes, and a reply that is
# hundreds of megabytes is a reason to stop rather than to keep reading.
MAX_REMOTE_BYTES = 8 * 1024 * 1024
REMOTE_TIMEOUT = 20


class RepoError(Exception):
    pass


def _run(args, cwd=None, timeout=300):
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise RepoError(f"missing tool: {args[0]}")
    except subprocess.TimeoutExpired:
        raise RepoError("timed out")
    if proc.returncode != 0:
        raise RepoError((proc.stderr or proc.stdout or "failed").strip()[-400:])
    return proc.stdout


def tools_available():
    """Which of the optional pieces are installed.

    None of these are needed to open the page. Each missing one disables
    exactly one thing and says so, which is the rule the rest of the
    application follows.
    """
    return {
        "dpkg-deb": shutil.which("dpkg-deb") is not None,
        "gpg": shutil.which("gpg") is not None,
        # Either of these can produce the package index.
        "apt-ftparchive": shutil.which("apt-ftparchive") is not None,
        "dpkg-scanpackages": shutil.which("dpkg-scanpackages") is not None,
        "rsync": shutil.which("rsync") is not None,
        "ssh": shutil.which("ssh") is not None,
    }


def can_index() -> bool:
    tools = tools_available()
    return tools["apt-ftparchive"] or tools["dpkg-scanpackages"]


# --------------------------------------------------------------------------- #
# Repository profiles
#
# A profile describes one archive: where its tree lives, what it calls
# itself, which key signs it and where it is published. They are kept in the
# ordinary settings file — none of it is secret, and the one thing that
# would be (an SSH password) is deliberately not supported: publishing
# authenticates with a key through the agent, never with a stored password.
# --------------------------------------------------------------------------- #
PROFILE_DEFAULTS = {
    "name": "",
    "root": "",
    "suite": DEFAULT_SUITE,
    "component": DEFAULT_COMPONENT,
    "archs": [DEFAULT_ARCH],
    "origin": "",
    "label": "",
    "description": "Packages built with Petacore",
    "base_url": "",
    "key": "",                 # signing key fingerprint
    "publish_method": "none",  # none | folder | rsync
    "publish_target": "",
    # The server the repository lives on, managed from the Server view.
    # Validated in remote.normalise_target before any of it reaches ssh.
    "server_host": "",
    "server_port": 22,
    "server_user": "",
    "server_root": "",
    "server_identity": "",
}


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return cleaned or "petacore"


def _safe_token(value: str, fallback: str) -> str:
    """A suite, component or architecture name: one path element, no tricks.

    These end up in file paths and in the Release file, so anything that
    could climb out of the tree or break the index format is refused rather
    than escaped.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", str(value or "").strip())
    if not cleaned or cleaned.startswith(".") or cleaned in (".", ".."):
        return fallback
    return cleaned


def _safe_line(value: str) -> str:
    """One line of a Debian control paragraph: no newlines, no leading space."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalise(profile: dict) -> dict:
    """Fill in whatever a stored profile is missing, and repair bad values.

    Old settings files, hand edits and half-finished dialogs all arrive
    here, so nothing downstream has to guess.
    """
    out = dict(PROFILE_DEFAULTS)
    out.update({k: v for k, v in (profile or {}).items()
                if k in PROFILE_DEFAULTS and v not in (None, [], "")})
    out["name"] = _safe_line(out["name"]) or "Repository"
    out["root"] = os.path.expanduser(str(out["root"] or "").strip())
    out["suite"] = _safe_token(out["suite"], DEFAULT_SUITE)
    out["component"] = _safe_token(out["component"], DEFAULT_COMPONENT)
    archs = out["archs"]
    if isinstance(archs, str):
        archs = [a for a in re.split(r"[,\s]+", archs) if a]
    seen, cleaned = set(), []
    for arch in archs or []:
        token = _safe_token(arch, "")
        if token and token not in seen:
            seen.add(token)
            cleaned.append(token)
    out["archs"] = cleaned or [DEFAULT_ARCH]
    out["origin"] = _safe_line(out["origin"]) or out["name"]
    out["label"] = _safe_line(out["label"]) or out["origin"]
    out["description"] = (_safe_line(out["description"])
                          or PROFILE_DEFAULTS["description"])
    out["base_url"] = str(out["base_url"] or "").strip().rstrip("/")
    if out["publish_method"] not in ("none", "folder", "rsync"):
        out["publish_method"] = "none"
    out["publish_target"] = str(out["publish_target"] or "").strip()
    for field in ("server_host", "server_user", "server_root",
                  "server_identity"):
        out[field] = str(out[field] or "").strip()
    try:
        out["server_port"] = int(out["server_port"] or 22)
    except (TypeError, ValueError):
        out["server_port"] = 22
    return out


def server_target(profile: dict) -> dict:
    """The profile's server fields, in the shape remote.Session expects."""
    profile = normalise(profile)
    return {"host": profile["server_host"], "port": profile["server_port"],
            "user": profile["server_user"], "root": profile["server_root"],
            "identity": profile["server_identity"]}


def keyring_filename(profile: dict) -> str:
    return f"{_slug(profile.get('name'))}-archive-keyring.asc"


def sources_name(profile: dict) -> str:
    return _slug(profile.get("name"))


# -- storage ---------------------------------------------------------------- #
def profiles():
    from .config import config
    return [normalise(p) for p in (config.get("apt_repos") or [])]


def active_profile():
    """The repository currently selected, or None when none is set up."""
    from .config import config
    stored = profiles()
    if not stored:
        return None
    wanted = config.get("active_apt_repo")
    for profile in stored:
        if profile["name"] == wanted:
            return profile
    return stored[0]


def set_active(name: str):
    from .config import config
    config.set("active_apt_repo", name or "")


def save_profile(profile: dict, previous_name: str = ""):
    """Add or update a repository. Returns the stored (normalised) copy."""
    from .config import config
    profile = normalise(profile)
    if not profile["root"]:
        raise RepoError("a repository needs a folder")
    drop = {previous_name or profile["name"], profile["name"]}
    stored = [p for p in (config.get("apt_repos") or [])
              if normalise(p)["name"] not in drop]
    stored.append(profile)
    config.set("apt_repos", stored)
    config.set("active_apt_repo", profile["name"])
    return profile


def remove_profile(name: str):
    """Forget a repository. The tree on disk is left exactly as it is."""
    from .config import config
    stored = [p for p in (config.get("apt_repos") or [])
              if normalise(p)["name"] != name]
    config.set("apt_repos", stored)
    if config.get("active_apt_repo") == name:
        config.set("active_apt_repo",
                   normalise(stored[0])["name"] if stored else "")


def ensure_layout(profile: dict):
    """Create the empty tree. Safe to call on an existing repository."""
    profile = normalise(profile)
    root = profile["root"]
    if not root:
        raise RepoError("a repository needs a folder")
    os.makedirs(os.path.join(root, "pool", profile["component"]),
                exist_ok=True)
    for arch in profile["archs"]:
        os.makedirs(os.path.join(root, "dists", profile["suite"],
                                 profile["component"], f"binary-{arch}"),
                    exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# Reading packages
# --------------------------------------------------------------------------- #
_field_cache = {}          # (path, mtime, size) -> fields


def package_fields(deb_path: str) -> dict:
    """Read the control fields out of a .deb.

    Cached on (path, mtime, size): the Repository page re-reads the pool on
    every refresh, and running dpkg-deb once per package per refresh is the
    difference between a list that appears and a list that crawls.
    """
    try:
        info = os.stat(deb_path)
    except OSError as e:
        raise RepoError(str(e))
    key = (deb_path, info.st_mtime_ns, info.st_size)
    cached = _field_cache.get(key)
    if cached is not None:
        return cached

    text = _run(["dpkg-deb", "-f", deb_path], timeout=60)
    fields = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(" "):
            name, _, value = line.partition(":")
            fields[name.strip()] = value.strip()
    if "Package" not in fields:
        raise RepoError("not a Debian package")
    if len(_field_cache) > 400:
        _field_cache.clear()
    _field_cache[key] = fields
    return fields


def is_signed(deb_path: str) -> bool:
    """Whether the package carries an embedded signature.

    This only looks for the `_gpgorigin` member — it does not verify it.
    Listing a hundred packages must not mean running gpg a hundred times, so
    verification is a separate, explicit action.
    """
    if not shutil.which("ar"):
        return False
    try:
        members = _run(["ar", "t", deb_path], timeout=30).split()
    except RepoError:
        return False
    return "_gpgorigin" in members


def verify(deb_path: str) -> bool:
    """Check the embedded signature properly. Slow; used on request only."""
    from . import gpgsign
    return gpgsign.verify_deb(deb_path)


def _pool_dir(root: str, component: str, package: str) -> str:
    # Debian groups by first letter, or by the "libx" prefix for libraries.
    letter = package[:4] if package.startswith("lib") else package[:1]
    return os.path.join(root, "pool", component, letter.lower(), package)


def list_packages(root: str):
    """Every package file currently in the pool, as absolute paths."""
    found = []
    pool = os.path.join(root, "pool")
    for base, dirs, files in os.walk(pool):
        dirs[:] = [d for d in dirs if d != TRASH_DIRNAME]
        for name in sorted(files):
            if name.endswith(".deb"):
                found.append(os.path.join(base, name))
    return sorted(found)


def _indexed_paths(profile: dict):
    """The Filename entries the current index claims to contain."""
    paths = set()
    for arch in profile["archs"]:
        index = os.path.join(profile["root"], "dists", profile["suite"],
                             profile["component"], f"binary-{arch}",
                             "Packages")
        try:
            with open(index, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("Filename:"):
                        paths.add(line.split(":", 1)[1].strip().lstrip("./"))
        except OSError:
            continue
    return paths


def scan(profile: dict):
    """Describe every package in the repository, for display.

    Sorted by package name and then version, each entry carrying what the
    row needs: where it is, what it is, how big, whether it is signed and
    whether the current index knows about it.
    """
    profile = normalise(profile)
    root = profile["root"]
    if not root or not os.path.isdir(root):
        return []
    indexed = _indexed_paths(profile)
    entries = []
    for path in list_packages(root):
        rel = os.path.relpath(path, root)
        try:
            fields = package_fields(path)
        except RepoError as e:
            entries.append({
                "path": path, "rel": rel, "file": os.path.basename(path),
                "package": os.path.basename(path), "version": "", "arch": "",
                "size": _size(path), "mtime": _mtime(path), "signed": False,
                "indexed": False, "broken": str(e), "summary": "",
                "installed": 0,
            })
            continue
        entries.append({
            "path": path,
            "rel": rel,
            "file": os.path.basename(path),
            "package": fields.get("Package", ""),
            "version": fields.get("Version", ""),
            "arch": fields.get("Architecture", ""),
            "size": _size(path),
            "mtime": _mtime(path),
            "signed": is_signed(path),
            "indexed": rel in indexed,
            "broken": "",
            "summary": fields.get("Description", "").split("\n")[0],
            "installed": _int(fields.get("Installed-Size", "0")),
        })
    entries.sort(key=lambda e: (e["package"].lower(),
                                _version_key(e["version"])))
    return entries


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _version_key(version: str):
    """Enough of Debian version ordering to sort a list by eye.

    Not a substitute for dpkg --compare-versions, which is used wherever the
    answer actually matters; this only decides display order.
    """
    parts = re.findall(r"\d+|[A-Za-z]+", version or "")
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def newer(version_a: str, version_b: str) -> bool:
    """True when A is a later version than B, asking dpkg when it can."""
    if not version_a:
        return False
    if not version_b:
        return True
    if shutil.which("dpkg"):
        try:
            proc = subprocess.run(
                ["dpkg", "--compare-versions", version_a, "gt", version_b],
                capture_output=True, timeout=15)
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return _version_key(version_a) > _version_key(version_b)


def group_by_package(entries):
    """{package: [entries, newest first]} — used to show and prune old copies."""
    grouped = {}
    for entry in entries:
        grouped.setdefault(entry["package"], []).append(entry)
    for versions in grouped.values():
        versions.sort(key=lambda e: _version_key(e["version"]), reverse=True)
    return grouped


# --------------------------------------------------------------------------- #
# What the list shows
#
# Which packages are visible, and how each one is labelled, is decided here
# rather than in the pages. Both interfaces then answer identically, and the
# rules can be tested without opening a window.
# --------------------------------------------------------------------------- #
FILTER_MODES = ("all", "unindexed", "unsigned", "unpublished", "old")


def live_state(entry, live):
    """Where one package stands against the copy on the server.

    `live` is {package: version} as read from the published index, or None
    when the server has not been checked. None is not the same as "nothing
    is published": the page says "not checked" rather than implying an empty
    archive, because guessing here would be a lie about someone's release.
    """
    if live is None:
        return ""
    version = live.get(entry["package"])
    if not version:
        return "new"
    if version == entry["version"]:
        return "live"
    return "ahead" if newer(entry["version"], version) else "behind"


def filter_entries(entries, needle: str = "", mode: str = "all", live=None):
    """The packages a page should show, given its search box and filter."""
    needle = (needle or "").strip().lower()
    if mode not in FILTER_MODES:
        mode = "all"
    newest = {name: versions[0]["path"]
              for name, versions in group_by_package(entries).items()}

    shown = []
    for entry in entries:
        haystack = (f'{entry["package"]} {entry["version"]} '
                    f'{entry["file"]}').lower()
        if needle and needle not in haystack:
            continue
        if mode == "unindexed" and entry["indexed"]:
            continue
        if mode == "unsigned" and entry["signed"]:
            continue
        if mode == "unpublished" and live_state(entry, live) in ("live", ""):
            continue
        if mode == "old" and newest.get(entry["package"]) == entry["path"]:
            continue
        shown.append(entry)
    return shown


# --------------------------------------------------------------------------- #
# Changing the pool
# --------------------------------------------------------------------------- #
def add_package(root, deb_path: str, component: str = DEFAULT_COMPONENT):
    """Copy a package into the pool. Returns its path inside the repository.

    `root` may be a profile or a plain path, so callers on either side of
    the interface can pass whichever they are holding.
    """
    if isinstance(root, dict):
        profile = normalise(root)
        root, component = profile["root"], profile["component"]
    if not os.path.isfile(deb_path):
        raise RepoError(f"no such file: {os.path.basename(deb_path)}")
    fields = package_fields(deb_path)
    destination = _pool_dir(root, component, fields["Package"])
    os.makedirs(destination, exist_ok=True)
    target = os.path.join(destination, os.path.basename(deb_path))
    if os.path.abspath(target) == os.path.abspath(deb_path):
        return target
    shutil.copy2(deb_path, target)
    return target


def add_packages(profile: dict, paths):
    """Add several packages, reporting each one separately.

    Returns (added, failures) where failures is [(filename, reason)] — one
    unreadable file in a dropped selection must not lose the rest.
    """
    profile = normalise(profile)
    ensure_layout(profile)
    added, failures = [], []
    for path in paths:
        try:
            added.append(add_package(profile, path))
        except (RepoError, OSError, shutil.Error) as e:
            failures.append((os.path.basename(path), str(e)))
    return added, failures


def _trash_dir(root: str) -> str:
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(root, TRASH_DIRNAME, stamp)
    os.makedirs(path, exist_ok=True)
    return path


def remove_packages(profile: dict, paths):
    """Take packages out of the pool.

    They are moved into a dated folder inside the repository rather than
    deleted, so the removal can be undone — and so a package pulled by
    mistake is still recoverable tomorrow. Returns the record needed to put
    them back.
    """
    profile = normalise(profile)
    root = profile["root"]
    record = []
    trash = ""
    for path in paths:
        if not os.path.isfile(path):
            continue
        if not trash:
            trash = _trash_dir(root)
        rel = os.path.relpath(path, root)
        target = os.path.join(trash, rel.replace(os.sep, "__"))
        shutil.move(path, target)
        record.append({"from": path, "to": target})
        _prune_empty(os.path.dirname(path), root)
    return {"trash": trash, "items": record}


def restore_removed(record: dict):
    """Undo remove_packages()."""
    restored = []
    for item in record.get("items", []):
        source, target = item["to"], item["from"]
        if not os.path.isfile(source):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(source, target)
        restored.append(target)
    trash = record.get("trash")
    if trash and os.path.isdir(trash) and not os.listdir(trash):
        try:
            os.rmdir(trash)
        except OSError:
            pass
    return restored


def _prune_empty(directory: str, root: str):
    """Remove pool folders left empty, without ever climbing past the pool."""
    pool = os.path.join(root, "pool")
    while (os.path.isdir(directory)
           and directory.startswith(pool)
           and directory != pool
           and not os.listdir(directory)):
        try:
            os.rmdir(directory)
        except OSError:
            return
        directory = os.path.dirname(directory)


def safe_filename(name: str) -> str:
    """Validate a name typed by a user before it becomes a file.

    A rename is a text field, and a text field that reached the file system
    unchecked could contain a path. This refuses rather than quietly
    correcting, so nothing is ever written somewhere the user did not look.
    """
    name = (name or "").strip()
    if not name:
        raise RepoError("the name is empty")
    if "/" in name or "\\" in name or os.sep in name or "\0" in name:
        raise RepoError("a file name cannot contain a path")
    if name.startswith(".") or name in (".", ".."):
        raise RepoError("that name is not allowed")
    if not name.endswith(".deb"):
        raise RepoError("the name must end in .deb")
    return name


def rename_package(path: str, new_name: str) -> str:
    """Rename a package file inside the pool.

    apt identifies a package by the fields inside it, not by its file name,
    so renaming is safe — but the index records the name, which is why the
    caller is expected to rebuild afterwards.
    """
    new_name = safe_filename(new_name)
    target = os.path.join(os.path.dirname(path), new_name)
    if (os.path.exists(target)
            and os.path.abspath(target) != os.path.abspath(path)):
        raise RepoError("a file with that name is already here")
    os.rename(path, target)
    return target


def move_package(profile: dict, path: str, package_name: str) -> str:
    """Move a package file into another package's pool folder.

    Used by drag and drop within the page. The pool layout is a convention
    rather than a rule — apt finds packages through the index — so this is
    about keeping the tree tidy, and it is undone by dragging it back.
    """
    profile = normalise(profile)
    destination = _pool_dir(profile["root"], profile["component"],
                            package_name)
    os.makedirs(destination, exist_ok=True)
    target = os.path.join(destination, os.path.basename(path))
    if os.path.abspath(target) == os.path.abspath(path):
        return path
    if os.path.exists(target):
        raise RepoError("a file with that name is already there")
    source_dir = os.path.dirname(path)
    shutil.move(path, target)
    _prune_empty(source_dir, profile["root"])
    return target


def prune(profile: dict, keep: int = 1):
    """Move all but the newest `keep` versions of each package to the trash.

    An archive that has been running a while accumulates every version ever
    published; apt only ever offers the newest, so the rest are weight.
    """
    keep = max(1, int(keep))
    grouped = group_by_package(scan(profile))
    doomed = []
    for versions in grouped.values():
        doomed.extend(e["path"] for e in versions[keep:])
    if not doomed:
        return {"trash": "", "items": []}
    return remove_packages(profile, doomed)


def trash_entries(profile: dict):
    """What is sitting in the repository's trash, newest first."""
    profile = normalise(profile)
    base = os.path.join(profile["root"], TRASH_DIRNAME)
    if not os.path.isdir(base):
        return []
    found = []
    for stamp in sorted(os.listdir(base), reverse=True):
        folder = os.path.join(base, stamp)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            found.append({"path": path, "file": name, "when": stamp,
                          "size": _size(path)})
    return found


def empty_trash(profile: dict):
    """Delete the trash for good. The only irreversible action in the file."""
    profile = normalise(profile)
    base = os.path.join(profile["root"], TRASH_DIRNAME)
    if not os.path.isdir(base):
        return 0
    count = len(trash_entries(profile))
    shutil.rmtree(base, ignore_errors=True)
    return count


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def _write_packages_index(root: str, suite: str, component: str, arch: str):
    index_dir = os.path.join(root, "dists", suite, component, f"binary-{arch}")
    os.makedirs(index_dir, exist_ok=True)

    if shutil.which("apt-ftparchive"):
        text = _run(["apt-ftparchive", "--arch", arch, "packages", "pool"],
                    cwd=root)
    elif shutil.which("dpkg-scanpackages"):
        text = _run(["dpkg-scanpackages", "--arch", arch, "--multiversion",
                     "pool"], cwd=root)
    else:
        raise RepoError("needs apt-utils (apt-ftparchive) or dpkg-dev")

    packages = os.path.join(index_dir, "Packages")
    with open(packages, "w", encoding="utf-8") as f:
        f.write(text)
    with open(packages, "rb") as src, gzip.open(packages + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    return packages


def _hash_file(path: str):
    md5, sha1, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest(), size


def _write_release(root: str, suite: str, component: str, archs,
                   origin: str, label: str, description: str):
    """Write dists/<suite>/Release.

    apt-ftparchive is used when it is installed. When it is not, the file is
    written here instead: it is a short, entirely specified format, and
    depending on an optional package for it would mean the feature
    disappearing on a machine without apt-utils.
    """
    dist_dir = os.path.join(root, "dists", suite)
    os.makedirs(dist_dir, exist_ok=True)
    release = os.path.join(dist_dir, "Release")

    if shutil.which("apt-ftparchive"):
        config_path = os.path.join(dist_dir, "apt-release.conf")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(
                f'APT::FTPArchive::Release::Origin "{origin}";\n'
                f'APT::FTPArchive::Release::Label "{label}";\n'
                f'APT::FTPArchive::Release::Suite "{suite}";\n'
                f'APT::FTPArchive::Release::Codename "{suite}";\n'
                f'APT::FTPArchive::Release::Architectures '
                f'"{" ".join(archs)}";\n'
                f'APT::FTPArchive::Release::Components "{component}";\n'
                f'APT::FTPArchive::Release::Description "{description}";\n')
        try:
            text = _run(["apt-ftparchive", "-c", config_path, "release", "."],
                        cwd=dist_dir)
        finally:
            try:
                os.remove(config_path)
            except OSError:
                pass
        with open(release, "w", encoding="utf-8") as f:
            f.write(text)
        return release

    # -- written here, when apt-utils is not installed ----------------------
    lines = [
        f"Origin: {origin}",
        f"Label: {label}",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Components: {component}",
        f"Architectures: {' '.join(archs)}",
        f"Description: {description}",
        "Date: " + time.strftime("%a, %d %b %Y %H:%M:%S UTC", time.gmtime()),
    ]
    collected = []
    for base, _dirs, files in os.walk(dist_dir):
        for name in sorted(files):
            if name in ("Release", "InRelease", "Release.gpg"):
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, dist_dir)
            collected.append((rel,) + _hash_file(full))
    collected.sort()

    for header, position in (("MD5Sum", 1), ("SHA1", 2), ("SHA256", 3)):
        lines.append(f"{header}:")
        for row in collected:
            lines.append(f" {row[position]} {row[4]:>16} {row[0]}")

    with open(release, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return release


def _sign_release(release_path: str, key_fingerprint: str):
    """Sign the index. Without this, apt refuses the repository outright."""
    dist_dir = os.path.dirname(release_path)
    inrelease = os.path.join(dist_dir, "InRelease")
    detached = os.path.join(dist_dir, "Release.gpg")
    for stale in (inrelease, detached):
        if os.path.exists(stale):
            os.remove(stale)

    _run(["gpg", "--batch", "--yes", "--default-key", key_fingerprint,
          "--clearsign", "-o", inrelease, release_path])
    _run(["gpg", "--batch", "--yes", "--default-key", key_fingerprint,
          "--armor", "--detach-sign", "-o", detached, release_path])
    return inrelease, detached


def export_key(root: str, key_fingerprint: str, filename: str = KEYRING_NAME):
    """Write the public key users will need in order to trust the archive."""
    path = os.path.join(root, filename)
    text = _run(["gpg", "--armor", "--export", key_fingerprint])
    if "BEGIN PGP PUBLIC KEY BLOCK" not in text:
        raise RepoError("could not export the public key")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def build(profile, key_fingerprint: str = "", suite: str = DEFAULT_SUITE,
          component: str = DEFAULT_COMPONENT, arch: str = DEFAULT_ARCH,
          origin: str = "Petacomm", label: str = "Petacomm",
          description: str = "Packages built with Petacore"):
    """Regenerate the indexes and sign them. Safe to run repeatedly.

    Accepts either a profile — the way the application calls it — or the
    older positional form, which existing scripts still use.
    """
    if isinstance(profile, dict):
        settings = normalise(profile)
        root = settings["root"]
        key_fingerprint = key_fingerprint or settings["key"]
        suite, component = settings["suite"], settings["component"]
        archs = settings["archs"]
        origin, label = settings["origin"], settings["label"]
        description = settings["description"]
        keyring = keyring_filename(settings)
    else:
        root, archs, keyring = profile, [arch], KEYRING_NAME

    if not root or not os.path.isdir(root):
        raise RepoError("the repository folder does not exist")
    if not list_packages(root):
        raise RepoError("the repository has no packages in it yet")
    if not key_fingerprint:
        raise RepoError("no signing key has been chosen")

    for one in archs:
        _write_packages_index(root, suite, component, one)
    release = _write_release(root, suite, component, archs, origin, label,
                             description)
    _sign_release(release, key_fingerprint)
    export_key(root, key_fingerprint, keyring)
    return root


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def status(profile: dict):
    """What state the repository is in, in the terms the page needs.

    The important field is `stale`: a package added but not indexed is
    invisible to apt, and nothing about the folder makes that obvious.
    """
    profile = normalise(profile)
    root = profile["root"]
    result = {
        "exists": bool(root) and os.path.isdir(root),
        "packages": 0, "unindexed": 0, "unsigned": 0, "broken": 0,
        "size": 0, "built": 0, "signed_index": False, "stale": False,
        "key": profile["key"], "key_present": False, "trash": 0,
    }
    if not result["exists"]:
        return result

    entries = scan(profile)
    result["packages"] = len(entries)
    result["unindexed"] = sum(1 for e in entries if not e["indexed"])
    result["unsigned"] = sum(1 for e in entries if not e["signed"])
    result["broken"] = sum(1 for e in entries if e["broken"])
    result["size"] = sum(e["size"] for e in entries)
    result["trash"] = len(trash_entries(profile))

    release = os.path.join(root, "dists", profile["suite"], "Release")
    inrelease = os.path.join(root, "dists", profile["suite"], "InRelease")
    result["built"] = _mtime(release)
    result["signed_index"] = os.path.isfile(inrelease)

    newest_package = max((e["mtime"] for e in entries), default=0)
    result["stale"] = bool(entries) and (
        not result["built"]
        or result["unindexed"] > 0
        or newest_package > result["built"])

    if profile["key"]:
        from . import gpgsign
        result["key_present"] = any(
            k["fpr"] == profile["key"] for k in gpgsign.list_keys_detailed())
    return result


# --------------------------------------------------------------------------- #
# What is actually live on the server
#
# This is the half that makes the page a view of the repository rather than
# of a folder: the same list, read back from the address users install from.
# --------------------------------------------------------------------------- #
def _fetch(url: str) -> bytes:
    if not url.startswith(("https://", "http://")):
        raise RepoError("only http and https addresses can be read")
    request = urllib.request.Request(url, headers={"User-Agent": "Petacore"})
    try:
        with urllib.request.urlopen(request, timeout=REMOTE_TIMEOUT) as reply:
            data = reply.read(MAX_REMOTE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise RepoError(f"{e.code} {e.reason}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RepoError(str(getattr(e, "reason", e)))
    if len(data) > MAX_REMOTE_BYTES:
        raise RepoError("the reply from the server was unreasonably large")
    return data


def parse_packages_index(text: str):
    """{package: {version, arch, filename, size}} from a Packages file."""
    found = {}
    for block in re.split(r"\n\s*\n", text):
        fields = {}
        for line in block.splitlines():
            if line[:1].isspace() or ":" not in line:
                continue
            name, _, value = line.partition(":")
            fields[name.strip()] = value.strip()
        if fields.get("Package"):
            found[fields["Package"]] = {
                "version": fields.get("Version", ""),
                "arch": fields.get("Architecture", ""),
                "filename": fields.get("Filename", ""),
                "size": _int(fields.get("Size", "0")),
            }
    return found


def remote_packages(profile: dict):
    """Read the live index from the published address.

    Raises RepoError with a plain reason when it cannot be read: an archive
    that has never been published looks exactly like one that is broken, and
    the page should be able to say which.
    """
    profile = normalise(profile)
    base = profile["base_url"]
    if not base:
        raise RepoError("no address has been set for this repository")
    arch = profile["archs"][0]
    stem = (f"{base}/dists/{profile['suite']}/{profile['component']}"
            f"/binary-{arch}/Packages")
    try:
        text = gzip.decompress(_fetch(stem + ".gz")).decode("utf-8", "replace")
    except (RepoError, OSError, EOFError, gzip.BadGzipFile):
        text = _fetch(stem).decode("utf-8", "replace")
    return parse_packages_index(text)


def remote_signed(profile: dict) -> bool:
    """Whether the published index carries a signature."""
    profile = normalise(profile)
    if not profile["base_url"]:
        return False
    try:
        data = _fetch(f"{profile['base_url']}/dists/{profile['suite']}"
                      "/InRelease")
    except RepoError:
        return False
    return b"BEGIN PGP SIGNED MESSAGE" in data


def compare(profile: dict):
    """Line the pool up against what is live, package by package.

    Each row carries a state: same, newer_local (the published copy is
    behind), newer_remote (the server has something this machine does not),
    only_local (never published), only_remote (removed here, still served).
    """
    local_newest = {}
    for package, versions in group_by_package(scan(profile)).items():
        local_newest[package] = versions[0]["version"]
    remote = remote_packages(profile)

    rows = []
    for package in sorted(set(local_newest) | set(remote)):
        here = local_newest.get(package, "")
        there = remote.get(package, {}).get("version", "")
        if here and not there:
            state = "only_local"
        elif there and not here:
            state = "only_remote"
        elif here == there:
            state = "same"
        elif newer(here, there):
            state = "newer_local"
        else:
            state = "newer_remote"
        rows.append({"package": package, "local": here, "remote": there,
                     "state": state})
    return rows


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def _check_target(target: str, method: str):
    if not target:
        raise RepoError("no destination has been set")
    if target.startswith("-"):
        raise RepoError("that destination is not allowed")
    if method == "folder":
        expanded = os.path.expanduser(target)
        if not os.path.isdir(expanded):
            raise RepoError("the destination folder does not exist")
        return expanded
    if ":" not in target:
        raise RepoError("expected something like user@host:/var/www/apt")
    return target


def _rsync_args(root: str, target: str, dry_run: bool):
    args = ["rsync", "-rlt", "--delete-after", "--chmod=D755,F644",
            "--itemize-changes",
            # Passwords are never stored or typed into Petacore. BatchMode
            # means an unattended run fails cleanly instead of blocking on a
            # prompt nobody can see, so publishing is key-based or not at all.
            "-e", "ssh -o BatchMode=yes",
            "--exclude", TRASH_DIRNAME + "/"]
    if dry_run:
        args.append("--dry-run")
    args.append(root.rstrip("/") + "/")
    args.append(target)
    return args


def publish_preview(profile: dict):
    """What publishing would change, without changing it.

    Publishing replaces what is on the server, so the list is shown first.
    With rsync this is its own dry run; with a folder the comparison is done
    here.
    """
    profile = normalise(profile)
    method = profile["publish_method"]
    if method == "none":
        raise RepoError("no destination has been set")
    target = _check_target(profile["publish_target"], method)
    root = profile["root"]

    if method == "rsync":
        if not shutil.which("rsync"):
            raise RepoError("rsync is not installed")
        out = _run(_rsync_args(root, target, dry_run=True), timeout=300)
        return [line for line in out.splitlines()
                if line and not line.startswith(("sending", "sent", "total"))]

    changes = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != TRASH_DIRNAME]
        for name in files:
            source = os.path.join(base, name)
            rel = os.path.relpath(source, root)
            destination = os.path.join(target, rel)
            if not os.path.exists(destination):
                changes.append(f"+ {rel}")
            elif (_mtime(source) > _mtime(destination)
                  or _size(source) != _size(destination)):
                changes.append(f"~ {rel}")
    return sorted(changes)


def publish(profile: dict, progress=None):
    """Copy the tree to its destination.

    Refuses to publish an index that is out of date: pushing a Release file
    that no longer matches the packages beside it makes apt reject the whole
    archive, which is worse than publishing nothing.
    """
    profile = normalise(profile)
    method = profile["publish_method"]
    if method == "none":
        raise RepoError("no destination has been set")
    target = _check_target(profile["publish_target"], method)
    root = profile["root"]

    state = status(profile)
    if state["stale"]:
        raise RepoError("rebuild and sign the index before publishing")
    if not state["signed_index"]:
        raise RepoError("the index is not signed")

    if method == "rsync":
        if not shutil.which("rsync"):
            raise RepoError("rsync is not installed")
        output = _run(_rsync_args(root, target, dry_run=False), timeout=1800)
        moved = [line for line in output.splitlines()
                 if line and not line.startswith(("sending", "sent", "total"))]
        return {"target": target, "files": len(moved)}

    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != TRASH_DIRNAME]
        for name in files:
            source = os.path.join(base, name)
            rel = os.path.relpath(source, root)
            destination = os.path.join(target, rel)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
            count += 1
            if progress:
                progress(count)
    return {"target": target, "files": count}


# --------------------------------------------------------------------------- #
# What users need to be told
# --------------------------------------------------------------------------- #
def install_instructions(profile, suite: str = DEFAULT_SUITE,
                         component: str = DEFAULT_COMPONENT,
                         name: str = "petacomm") -> str:
    """The two commands a user runs once to trust and add the archive."""
    if isinstance(profile, dict):
        settings = normalise(profile)
        base_url = settings["base_url"] or "https://example.com/apt"
        suite, component = settings["suite"], settings["component"]
        name = sources_name(settings)
        keyring = keyring_filename(settings)
    else:
        base_url = profile or "https://example.com/apt"
        keyring = KEYRING_NAME
    base_url = base_url.rstrip("/")
    return (
        f"# Trust the archive key\n"
        f"curl -fsSL {base_url}/{keyring} \\\n"
        f"  | sudo gpg --dearmor -o /usr/share/keyrings/{name}.gpg\n"
        f"\n"
        f"# Add the repository\n"
        f"echo \"deb [signed-by=/usr/share/keyrings/{name}.gpg] "
        f"{base_url} {suite} {component}\" \\\n"
        f"  | sudo tee /etc/apt/sources.list.d/{name}.list\n"
        f"\n"
        f"# Install\n"
        f"sudo apt update && sudo apt install <package>\n")


def install_command(package: str) -> str:
    """The single line that installs one package once the archive is added."""
    return f"sudo apt update && sudo apt install {package}"
