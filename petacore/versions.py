"""A project's release history: every .deb and .rpm it has shipped.

Each release lives in a folder of its own inside the project:

    <project>/versions/
      .petacore-versions            marks the folder as Petacore's, and
                                    holds what each release is: when it was
                                    made, its notes, who signed it
      1.0/
        myapp_1.0_all.deb
        myapp-1.0-1.noarch.rpm
      1.6-beta.2/
        myapp_1.6~beta2_all.deb

A release folder holds the packages and nothing else. What the files
themselves cannot say is kept in the one hidden index beside them, so
whatever is handed on from a release folder is exactly what was shipped.

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
# Older versions of Petacore kept a release.json beside the packages. It is
# read once, moved into the index, and deleted — see _absorb_legacy().
LEGACY_MANIFEST = "release.json"
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


# -- the index: one hidden file for everything that is not a package ----------
def _load_index(root: str) -> dict:
    try:
        with open(os.path.join(root, MARKER), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(root: str, data: dict):
    """Write the index so that it is never half-written.

    Every release's notes and dates live in this one file, so a crash or a
    power cut in the middle of writing it must not cost the whole history:
    the new content goes to a temporary file, which then replaces the old
    one in a single step.
    """
    path = os.path.join(root, MARKER)
    temporary = path + ".writing"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


# -- defaults remembered per project, in the index --------------------------------
def defaults(project_path: str) -> dict:
    """Maintainer, description and so on, as last used for this project."""
    data = _load_index(folder(project_path)).get("defaults", {})
    return data if isinstance(data, dict) else {}


def remember_defaults(project_path: str, values: dict):
    root = folder(project_path)
    data = _load_index(root)
    data["defaults"] = {k: v for k, v in values.items()
                        if isinstance(v, (str, bool, int))}
    _save_index(root, data)


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


def channel_name(channel: str) -> str:
    """The release type as the person's language writes it: "Beta",
    "Бета", "Alfa". "RC" stays RC everywhere — it is short, and it is what
    developers call it in every language."""
    if channel == "rc":
        return "RC"
    try:
        from .i18n import translator
        return translator(f"ver_ch_{channel}")
    except Exception:            # noqa: BLE001 - never let a label fail
        return CHANNEL_LABELS.get(channel, channel)


def label(number: str, channel: str = "stable", pre: int = 0) -> str:
    """What a person reads: "1.6", "1.6 Beta 2", "2.0 RC 1"."""
    if channel == "stable":
        return number
    return f"{number} {channel_name(channel)} {int(pre)}"


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

    Read from the folders on disk, with the index filling in what the
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
        manifest = _manifest(release_dir)

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
def _manifest(release_dir: str) -> dict:
    """What the index knows about one release — {} for a folder someone
    made by hand, which still appears in the history by its name."""
    root, name = os.path.split(release_dir.rstrip(os.sep))
    entry = _load_index(root).get("releases", {}).get(name)
    if isinstance(entry, dict):
        return entry
    return _absorb_legacy(release_dir) or {}


def _write_manifest(release_dir: str, data: dict):
    root, name = os.path.split(release_dir.rstrip(os.sep))
    index = _load_index(root)
    releases = index.get("releases")
    if not isinstance(releases, dict):
        releases = index["releases"] = {}
    releases[name] = data
    _save_index(root, index)


def _absorb_legacy(release_dir: str):
    """Move an old release.json into the index and delete it.

    Done the first time a release is read, so a project made with an older
    Petacore is tidied without the person doing anything, and nothing it
    recorded — notes, dates, signatures — is lost on the way. A file that
    cannot be read is left alone rather than deleted: it may be the only
    copy of someone's notes.
    """
    path = os.path.join(release_dir, LEGACY_MANIFEST)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    _write_manifest(release_dir, data)
    try:
        os.remove(path)
    except OSError:
        pass
    return data


def tidy(project_path: str) -> int:
    """Absorb every leftover release.json at once. Returns how many."""
    if not is_ours(project_path):
        return 0
    root = folder(project_path)
    count = 0
    for name in os.listdir(root):
        release_dir = os.path.join(root, name)
        if not name.startswith(".") and os.path.isdir(release_dir) \
                and _absorb_legacy(release_dir) is not None:
            count += 1
    return count


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
    data = dict(_manifest(release_dir))
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


# --------------------------------------------------------------------------- #
# Reading a release's code
#
# A release's code is read from its own package, not from a copy kept beside
# it: what is shown is exactly what was shipped, byte for byte, and the
# release folder still holds nothing but packages.
#
# Nothing is ever extracted to disk. The package's file archive is read in
# memory, member by member, so a package with hostile paths inside it
# ("../../.bashrc", an absolute path, a link pointing outside) can at worst
# be listed oddly — it cannot write anything anywhere.
# --------------------------------------------------------------------------- #
import io as _io
import posixpath as _posix
import subprocess as _sp
import tarfile as _tar

MAX_PACKAGE = 512 * 1024 * 1024      # refuse to load larger archives
MAX_VIEW = 2 * 1024 * 1024           # larger files are listed, not shown
_SOURCE_CACHE = {}                   # (path, mtime, size) -> ReleaseSource


class ReleaseSource:
    """The files of one package: a sorted listing, and their contents.

    `files` holds dicts {path, size, link} with paths relative to the
    project root inside the package (/opt/<name>/ for Petacore's own
    packages), so the listing reads like the project did on the day.
    """

    def __init__(self, package, root, members, blobs):
        self.package = package
        self.root = root
        self.files = members
        self._blobs = blobs

    def read(self, rel_path):
        """(text, None) for a readable file, (None, reason) otherwise."""
        entry = next((f for f in self.files if f["path"] == rel_path), None)
        if entry is None:
            return None, "missing"
        if entry["link"] is not None:
            return None, "link"
        if entry["size"] > MAX_VIEW:
            return None, "too-big"
        data = self._blobs.get(rel_path, b"")
        if b"\0" in data[:8192]:
            return None, "binary"
        try:
            return data.decode("utf-8"), None
        except UnicodeDecodeError:
            try:
                return data.decode("latin-1"), None
            except UnicodeDecodeError:          # pragma: no cover
                return None, "binary"


def _clean_member(name):
    """A member name as a safe relative path, or None to skip it.

    Leading "./" and "/" are dropped; anything that would climb out of the
    package ("..") is refused outright rather than repaired.
    """
    name = (name or "").replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    name = name.lstrip("/")
    if not name:
        return None
    parts = name.split("/")
    if any(p in ("..",) for p in parts) or "\0" in name:
        return None
    return _posix.normpath(name)


def _deb_tar_bytes(path):
    """The uncompressed file archive (data.tar) of a .deb, as bytes."""
    import shutil as _shutil
    if _shutil.which("dpkg-deb"):
        proc = _sp.run(["dpkg-deb", "--fsys-tarfile", path],
                       capture_output=True, timeout=300)
        if proc.returncode == 0:
            return proc.stdout
    # dpkg-deb missing or unhappy: read the ar archive ourselves.
    with open(path, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise VersionError("not a Debian package")
        while True:
            header = f.read(60)
            if len(header) < 60:
                break
            name = header[:16].decode("ascii", "replace").strip().rstrip("/")
            size = int(header[48:58].decode("ascii").strip() or 0)
            body = f.read(size)
            if size % 2:
                f.read(1)
            if name.startswith("data.tar"):
                if name.endswith(".zst"):
                    try:
                        from compression import zstd   # Python 3.14+
                        return zstd.decompress(body)
                    except ImportError:
                        raise VersionError(
                            "this package is zstd-compressed — install dpkg "
                            "to read it")
                with _tar.open(fileobj=_io.BytesIO(body)) as inner:
                    out = _io.BytesIO()
                    with _tar.open(fileobj=out, mode="w") as plain:
                        for m in inner.getmembers():
                            fo = inner.extractfile(m) if m.isfile() else None
                            plain.addfile(m, fo)
                    return out.getvalue()
    raise VersionError("the package has no file archive")


def _read_tar(data):
    members, blobs = [], {}
    with _tar.open(fileobj=_io.BytesIO(data)) as archive:
        for m in archive:
            name = _clean_member(m.name)
            if name is None or m.isdir():
                continue
            if m.issym() or m.islnk():
                members.append({"path": name, "size": 0,
                                "link": m.linkname})
                continue
            if not m.isfile():
                continue
            members.append({"path": name, "size": m.size, "link": None})
            if m.size <= MAX_VIEW:
                fo = archive.extractfile(m)
                blobs[name] = fo.read() if fo else b""
    return members, blobs


def _read_cpio(data):
    """Parse a "newc" cpio stream, as rpm2cpio produces."""
    members, blobs, pos = [], {}, 0
    while pos + 110 <= len(data):
        if data[pos:pos + 6] not in (b"070701", b"070702"):
            break
        head = data[pos:pos + 110]
        field = lambda i: int(head[6 + i * 8:14 + i * 8], 16)  # noqa: E731
        mode, filesize, namesize = field(1), field(6), field(11)
        pos += 110
        raw_name = data[pos:pos + namesize - 1].decode("utf-8", "replace")
        pos = (pos + namesize + 3) & ~3
        body = data[pos:pos + filesize]
        pos = (pos + filesize + 3) & ~3
        if raw_name == "TRAILER!!!":
            break
        name = _clean_member(raw_name)
        kind = mode & 0o170000
        if name is None or kind == 0o040000:
            continue
        if kind == 0o120000:
            members.append({"path": name, "size": 0,
                            "link": body.decode("utf-8", "replace")})
        elif kind == 0o100000:
            members.append({"path": name, "size": filesize, "link": None})
            if filesize <= MAX_VIEW:
                blobs[name] = bytes(body)
    return members, blobs


def _project_root(members):
    """opt/<name> when the package installs a project there, else ""."""
    tops = {m["path"].split("/")[1] for m in members
            if m["path"].startswith("opt/") and m["path"].count("/") >= 2}
    return f"opt/{tops.pop()}" if len(tops) == 1 else ""


def read_release_code(package_path):
    """The code inside one .deb or .rpm, as a ReleaseSource.

    Cached by path, size and modification time, so moving between files of
    a release does not unpack the package again.
    """
    st = os.stat(package_path)
    if st.st_size > MAX_PACKAGE:
        raise VersionError("the package is too large to open here")
    key = (os.path.realpath(package_path), st.st_mtime_ns, st.st_size)
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]

    if package_path.endswith(".deb"):
        members, blobs = _read_tar(_deb_tar_bytes(package_path))
    elif package_path.endswith(".rpm"):
        import shutil as _shutil
        if not _shutil.which("rpm2cpio"):
            raise VersionError("rpm2cpio is not installed "
                               "(sudo apt install rpm2cpio)")
        proc = _sp.run(["rpm2cpio", package_path], capture_output=True,
                       timeout=300)
        if proc.returncode != 0:
            raise VersionError("the .rpm could not be read")
        members, blobs = _read_cpio(proc.stdout)
    else:
        raise VersionError("only .deb and .rpm packages can be opened")

    root = _project_root(members)
    if root:
        prefix = root + "/"
        kept = []
        for m in members:
            if m["path"].startswith(prefix):
                rel = m["path"][len(prefix):]
                kept.append(dict(m, path=rel))
                if m["path"] in blobs:
                    blobs[rel] = blobs.pop(m["path"])
        members = kept
    members.sort(key=lambda m: m["path"].lower())
    source = ReleaseSource(package_path, root, members, blobs)
    if len(_SOURCE_CACHE) > 8:
        _SOURCE_CACHE.pop(next(iter(_SOURCE_CACHE)))
    _SOURCE_CACHE[key] = source
    return source


def code_package(release):
    """The package whose code a release shows: the .deb if there is one."""
    files = release.get("files") or []
    for kind in ("deb", "rpm"):
        for f in files:
            if f["kind"] == kind:
                return f["path"]
    return ""
