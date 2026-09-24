"""A project's release history: every .deb and .rpm it has shipped.

Each release lives in a folder of its own inside the project:

    <project>/versions/
      .petacore-versions            marks the folder as Petacore's
      1.0/
        release.json                what, when, which files, signed by whom
        myapp_1.0_all.deb
        myapp-1.0-1.noarch.rpm
      1.6-beta.2/
        release.json
        myapp_1.6~beta2_all.deb

Releases are created by hand, never automatically — a build made to try
something out is not a release, and a history cluttered with them stops
being a history.

The folder sits inside the project so that the releases travel with it:
copy the project, and its history comes along. That convenience brings two
dangers, and most of this module exists to deal with them:

  * Packaging walks the project folder. Without care, the next .deb would
    contain every previous .deb, and each release would carry all the ones
    before it. The folder is therefore left out of every package, every
    snapshot, the statistics and the file view.
  * A project may have its own folder called "versions" — a migrations
    directory, a Django app. Treating that as ours would leave real code
    out of the package without a word. So the folder is recognised by the
    marker file inside it, not by its name alone, and a "versions" folder
    Petacore did not create is left exactly as it is.
"""

import hashlib
import json
import os
import re
import shutil
import time

DIRNAME = "versions"
MARKER = ".petacore-versions"
MANIFEST = "release.json"
TRASH = ".trash"

# Ordered from least to most finished. The Debian and RPM version strings
# built from these rely on that order: "~" sorts before anything, so
# 1.6~alpha1 < 1.6~beta1 < 1.6~rc1 < 1.6 — exactly how a person reads them.
CHANNELS = ("alpha", "beta", "rc", "stable")
CHANNEL_LABELS = {"alpha": "Alpha", "beta": "Beta", "rc": "RC",
                  "stable": ""}
PACKAGE_SUFFIXES = (".deb", ".rpm")


class VersionError(Exception):
    pass


# --------------------------------------------------------------------------- #
# The folder
# --------------------------------------------------------------------------- #
def folder(project_path: str) -> str:
    return os.path.join(project_path, DIRNAME)


def is_ours(project_path: str) -> bool:
    """Whether <project>/versions is Petacore's, and not the project's own."""
    return os.path.isfile(os.path.join(folder(project_path), MARKER))


def hidden(parent_path: str, name: str) -> bool:
    """True for an entry that the file view and every walker should skip.

    Called with the directory being listed and the name of one entry in it,
    which is the shape every walker in Petacore already has to hand.
    """
    return name == DIRNAME and os.path.isfile(
        os.path.join(parent_path, name, MARKER))


def ensure(project_path: str):
    """Create the folder, mark it, and keep it out of git.

    Git is told through .git/info/exclude rather than .gitignore: that file
    is local to this clone and never committed, so nothing the project
    tracks is changed — the design rule is to leave the project alone.
    """
    root = folder(project_path)
    if os.path.isdir(root) and not is_ours(project_path) \
            and any(not n.startswith(".") for n in os.listdir(root)):
        raise VersionError(
            "this project already has its own 'versions' folder; "
            "Petacore will not take it over")
    os.makedirs(root, exist_ok=True)
    marker = os.path.join(root, MARKER)
    if not os.path.isfile(marker):
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"created": time.time()}, f)
    _exclude_from_git(project_path)
    return root


def _exclude_from_git(project_path: str):
    git_dir = os.path.join(project_path, ".git")
    if not os.path.isdir(git_dir):
        return
    info = os.path.join(git_dir, "info")
    os.makedirs(info, exist_ok=True)
    exclude = os.path.join(info, "exclude")
    line = f"/{DIRNAME}/"
    existing = ""
    if os.path.isfile(exclude):
        with open(exclude, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    if line in existing.splitlines():
        return
    with open(exclude, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("# Petacore release history — binaries, kept out of git\n")
        f.write(line + "\n")


# -- defaults remembered per project, in the marker ---------------------------
def defaults(project_path: str) -> dict:
    """Maintainer, description and so on, as last used for this project."""
    try:
        with open(os.path.join(folder(project_path), MARKER),
                  encoding="utf-8") as f:
            data = json.load(f)
        return data.get("defaults", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_defaults(project_path: str, values: dict):
    marker = os.path.join(folder(project_path), MARKER)
    try:
        with open(marker, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["defaults"] = {k: v for k, v in values.items()
                        if isinstance(v, (str, bool, int))}
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
def clean_number(number: str) -> str:
    """"1.6", "2.0.1" — digits and dots only, starting with a digit.

    The number becomes a folder name and part of a package version, so it
    is refused rather than repaired: a silently rewritten version is worse
    than being asked to type it again.
    """
    number = (number or "").strip().lstrip("vV")
    if not re.fullmatch(r"\d+(\.\d+){0,3}", number):
        raise VersionError("a version is numbers and dots, like 1.6 or 2.0.1")
    return number


def _check(channel: str, pre: int):
    if channel not in CHANNELS:
        raise VersionError("unknown release channel")
    if channel != "stable" and int(pre) < 1:
        raise VersionError("a pre-release needs a number, starting at 1")


def label(number: str, channel: str = "stable", pre: int = 0) -> str:
    """What a person reads: "1.6", "1.6 Beta 2", "2.0 RC 1"."""
    if channel == "stable":
        return number
    return f"{number} {CHANNEL_LABELS[channel]} {int(pre)}"


def package_version(number: str, channel: str = "stable",
                    pre: int = 0) -> str:
    """What dpkg and rpm compare: "1.6", "1.6~beta2", "2.0~rc1"."""
    if channel == "stable":
        return number
    return f"{number}~{channel}{int(pre)}"


def folder_name(number: str, channel: str = "stable", pre: int = 0) -> str:
    """The release's folder: "1.6", "1.6-beta.2" — no tilde, no spaces."""
    if channel == "stable":
        return number
    return f"{number}-{channel}.{int(pre)}"


def _parse_folder(name: str):
    match = re.fullmatch(r"(\d+(?:\.\d+){0,3})(?:-(alpha|beta|rc)\.(\d+))?",
                         name)
    if not match:
        return None
    number, channel, pre = match.groups()
    return number, channel or "stable", int(pre or 0)


def sort_key(number: str, channel: str, pre: int):
    """Newest-first ordering that agrees with how dpkg would compare them."""
    parts = tuple(int(p) for p in number.split("."))
    parts += (0,) * (4 - len(parts))
    return parts + (CHANNELS.index(channel), int(pre))


# --------------------------------------------------------------------------- #
# Reading the history
# --------------------------------------------------------------------------- #
def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _describe_file(path: str, signed=None):
    return {
        "name": os.path.basename(path),
        "path": path,
        "kind": os.path.splitext(path)[1].lstrip("."),
        "size": os.path.getsize(path),
        "signed": signed,
    }


def list_versions(project_path: str):
    """Every release, newest first.

    Read from the folders on disk, with release.json filling in what the
    files alone cannot say. A folder someone made by hand — dropped in
    from a file manager, with no manifest — still appears, as long as its
    name reads as a version.
    """
    root = folder(project_path)
    if not is_ours(project_path):
        return []
    found = []
    for name in os.listdir(root):
        release_dir = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(release_dir):
            continue
        manifest = {}
        try:
            with open(os.path.join(release_dir, MANIFEST),
                      encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            pass

        parsed = _parse_folder(name)
        if not manifest and not parsed:
            continue
        number, channel, pre = parsed or (
            manifest.get("number", "0"), manifest.get("channel", "stable"),
            manifest.get("pre", 0))
        number = manifest.get("number", number)
        channel = manifest.get("channel", channel)
        pre = int(manifest.get("pre", pre))

        signed_by_name = {f.get("name"): f.get("signed")
                          for f in manifest.get("files", [])}
        files = [_describe_file(os.path.join(release_dir, n),
                                signed_by_name.get(n))
                 for n in sorted(os.listdir(release_dir))
                 if n.endswith(PACKAGE_SUFFIXES)
                 and os.path.isfile(os.path.join(release_dir, n))]

        found.append({
            "label": label(number, channel, pre),
            "number": number,
            "channel": channel,
            "pre": pre,
            "package_version": package_version(number, channel, pre),
            "folder": name,
            "path": release_dir,
            "created": manifest.get("created")
            or os.path.getmtime(release_dir),
            "notes": manifest.get("notes", ""),
            "signed_by": manifest.get("signed_by", ""),
            "source": manifest.get("source", "built"),
            "files": files,
        })
    found.sort(key=lambda v: sort_key(v["number"], v["channel"], v["pre"]),
               reverse=True)
    return found


def suggest_next(project_path: str) -> str:
    """The number most likely to come next: the last stable one, bumped."""
    history = list_versions(project_path)
    if not history:
        return "1.0"
    latest = history[0]
    if latest["channel"] != "stable":
        return latest["number"]          # still working towards that one
    parts = latest["number"].split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def suggest_pre(project_path: str, number: str, channel: str) -> int:
    """Beta 1 if this is the first beta of 1.6, otherwise one more."""
    taken = [v["pre"] for v in list_versions(project_path)
             if v["number"] == number and v["channel"] == channel]
    return (max(taken) + 1) if taken else 1


# --------------------------------------------------------------------------- #
# Writing the history
# --------------------------------------------------------------------------- #
def _write_manifest(release_dir: str, data: dict):
    with open(os.path.join(release_dir, MANIFEST), "w",
              encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _new_release_dir(project_path, number, channel, pre):
    number = clean_number(number)
    _check(channel, pre)
    ensure(project_path)
    release_dir = os.path.join(folder(project_path),
                               folder_name(number, channel, pre))
    if os.path.exists(release_dir):
        raise VersionError(f"{label(number, channel, pre)} already exists")
    os.makedirs(release_dir)
    return number, release_dir


def create(project_path: str, name: str, number: str, channel: str,
           pre: int, formats, maintainer: str, description: str,
           license_name: str, homepage: str = "", notes: str = "",
           sign: bool = False, key_fpr: str = "", progress=None):
    """Build a release from the project as it is now, and record it.

    Every requested format is built before anything is recorded; if one
    fails, the half-made release folder is removed, so the history never
    shows a release that is missing half its files.
    """
    from . import debbuild, gpgsign

    formats = [f for f in formats if f in ("deb", "rpm")]
    if not formats:
        raise VersionError("choose at least one package format")

    key = None
    if sign:
        key = gpgsign.signing_key(key_fpr)
        if not key or (key_fpr and key[0] != key_fpr):
            # The chosen key is gone. Signing with some other key would put
            # a different identity on the release without saying so.
            raise VersionError("the chosen signing key is not on this "
                               "machine any more")

    number, release_dir = _new_release_dir(project_path, number, channel,
                                           pre)
    version = package_version(number, channel, pre)
    produced = []
    try:
        for fmt in formats:
            if progress:
                progress(fmt)
            builder = debbuild.build_deb if fmt == "deb" \
                else debbuild.build_rpm
            path = builder(project_path, release_dir, name, version,
                           maintainer, description, license_name, homepage)
            signed = None
            if key:
                if fmt == "rpm":
                    gpgsign.sign_rpm(path, key[1])
                else:
                    gpgsign.sign_deb(path, key[0])
                signed = key[0]
            produced.append((path, signed))
    except Exception:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise

    manifest = {
        "number": number,
        "channel": channel,
        "pre": int(pre),
        "label": label(number, channel, pre),
        "package_version": version,
        "created": time.time(),
        "notes": notes.strip(),
        "source": "built",
        "signed_by": key[0] if key else "",
        "files": [{"name": os.path.basename(p), "signed": s,
                   "sha256": _sha256(p)} for p, s in produced],
    }
    _write_manifest(release_dir, manifest)
    remember_defaults(project_path, {
        "maintainer": maintainer, "description": description,
        "license": license_name, "sign": bool(sign), "key": key_fpr,
        "formats": ",".join(formats)})
    return release_dir


def import_files(project_path: str, number: str, channel: str, pre: int,
                 paths, notes: str = ""):
    """Record packages built somewhere else as a release — to bring an
    existing history in, one release at a time. The files are copied, never
    moved, so the originals stay where they were.
    """
    paths = [p for p in paths if p.endswith(PACKAGE_SUFFIXES)
             and os.path.isfile(p)]
    if not paths:
        raise VersionError("only .deb and .rpm files can be added")
    number, release_dir = _new_release_dir(project_path, number, channel,
                                           pre)
    copied = []
    try:
        for path in paths:
            target = os.path.join(release_dir, os.path.basename(path))
            shutil.copy2(path, target)
            copied.append(target)
    except Exception:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise
    _write_manifest(release_dir, {
        "number": number, "channel": channel, "pre": int(pre),
        "label": label(number, channel, pre),
        "package_version": package_version(number, channel, pre),
        "created": time.time(), "notes": notes.strip(),
        "source": "imported", "signed_by": "",
        "files": [{"name": os.path.basename(p), "signed": None,
                   "sha256": _sha256(p)} for p in copied],
    })
    return release_dir


def set_notes(release_dir: str, notes: str):
    path = os.path.join(release_dir, MANIFEST)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data["notes"] = (notes or "").strip()
    _write_manifest(release_dir, data)


def remove(project_path: str, release_dir: str) -> str:
    """Move a release into versions/.trash rather than deleting it.

    A release is the one thing that cannot be rebuilt identically later —
    the code has moved on — so removing one is reversible.
    """
    root = os.path.realpath(folder(project_path))
    release_dir = os.path.realpath(release_dir)
    if os.path.dirname(release_dir) != root:
        raise VersionError("that is not a release of this project")
    trash = os.path.join(root, TRASH, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(trash, exist_ok=True)
    target = os.path.join(trash, os.path.basename(release_dir))
    shutil.move(release_dir, target)
    return target


def restore(project_path: str, trashed_dir: str) -> str:
    target = os.path.join(folder(project_path),
                          os.path.basename(trashed_dir))
    if os.path.exists(target):
        raise VersionError("a release with that name exists again")
    shutil.move(trashed_dir, target)
    parent = os.path.dirname(trashed_dir)
    if os.path.isdir(parent) and not os.listdir(parent):
        os.rmdir(parent)
    return target
