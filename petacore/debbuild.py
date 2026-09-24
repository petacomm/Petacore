"""Debian package builder for Petacore.

Packages the project tree into a .deb that installs to /opt/<name>, using
dpkg-deb. No terminal usage required from the user.
"""

import os
import re
import shutil
import subprocess
import tempfile

EXCLUDE = {".git", ".petacore", "__pycache__", "node_modules", ".venv"}

# Files that carry credentials. A package is meant to be handed to other
# people, so these are left out of it and the omission is reported back to
# the user rather than done silently — a project that keeps its API keys in
# .env should not publish them to everyone who installs the package.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*.ppk",
    "secring.*", "*.gpg", "*.asc",
    ".netrc", ".npmrc", ".pypirc", ".dockercfg",
    "credentials.json", "service-account*.json",
    ".aws", ".ssh", ".gnupg",
)
# Names that look alarming but are safe to ship.
SECRET_ALLOW = (".env.example", ".env.sample", ".env.template",
                "public.asc", "public-key.asc")


def find_secrets(project_path: str):
    """Every credential-looking file inside the project, as relative paths."""
    import fnmatch
    found = []
    from . import versions
    for base, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE
                   and not versions.hidden(base, d)]
        for entry in list(dirs) + files:
            if entry in SECRET_ALLOW:
                continue
            if any(fnmatch.fnmatch(entry, pattern)
                   for pattern in SECRET_PATTERNS):
                found.append(os.path.relpath(os.path.join(base, entry),
                                             project_path))
    return sorted(found)


def _package_ignore(directory, entries):
    """copytree filter: skip build noise and anything credential-shaped."""
    import fnmatch
    from . import versions
    skipped = set()
    for entry in entries:
        if entry in EXCLUDE or versions.hidden(directory, entry):
            # the release history: without this, every package would carry
            # every package built before it
            skipped.add(entry)
        elif entry in SECRET_ALLOW:
            continue
        elif any(fnmatch.fnmatch(entry, pattern)
                 for pattern in SECRET_PATTERNS):
            skipped.add(entry)
    return skipped


class DebError(Exception):
    pass


# Software centres (GNOME Software, KDE Discover, elementary AppCenter) do
# not read the Debian control file. They read AppStream metadata, which is
# why a package without it shows "Unknown publisher" and "License: unknown"
# no matter what the control file says.
# Where the licences are explained for people who are unsure which to pick.
LICENSE_GUIDE_URL = "https://kotechsoft.com/petacomm-licenses.html"

# Offered in the Package page as (SPDX id, short name). The names are kept
# bare so the list stays scannable; what each one means is explained on the
# licence page linked underneath the chooser.
LICENSE_CHOICES = [
    ("LicenseRef-proprietary", "Proprietary"),
    ("MIT", "MIT"),
    ("Apache-2.0", "Apache 2.0"),
    ("BSD-3-Clause", "BSD 3-Clause"),
    ("BSD-2-Clause", "BSD 2-Clause"),
    ("ISC", "ISC"),
    ("MPL-2.0", "Mozilla 2.0"),
    ("LGPL-3.0-or-later", "LGPL 3.0+"),
    ("GPL-3.0-only", "GPL 3.0"),
    ("GPL-2.0-only", "GPL 2.0"),
    ("AGPL-3.0-only", "AGPL 3.0"),
    ("Unlicense", "Unlicense"),
    ("CC0-1.0", "CC0 1.0"),
]

SPDX_HINTS = {
    "mit": "MIT", "apache": "Apache-2.0", "apache-2": "Apache-2.0",
    "apache 2.0": "Apache-2.0", "gpl": "GPL-3.0-or-later",
    "gplv3": "GPL-3.0-only", "gpl-3": "GPL-3.0-only",
    "gplv2": "GPL-2.0-only", "gpl-2": "GPL-2.0-only",
    "lgpl": "LGPL-3.0-or-later", "agpl": "AGPL-3.0-only",
    "bsd": "BSD-3-Clause", "bsd-3": "BSD-3-Clause", "bsd-2": "BSD-2-Clause",
    "mpl": "MPL-2.0", "isc": "ISC", "unlicense": "Unlicense",
    "cc0": "CC0-1.0", "zlib": "Zlib",
}


def _spdx(license_name: str) -> str:
    """Map a written licence name onto an SPDX identifier where possible.

    Software centres show the licence only when they recognise the SPDX form;
    anything else is displayed as proprietary, which for a closed-source
    project is the honest answer anyway."""
    raw = (license_name or "").strip()
    # A value picked from the list is already an SPDX identifier; pass it
    # through untouched rather than trying to re-interpret it.
    known = {spdx for spdx, _desc in LICENSE_CHOICES}
    if raw in known:
        return raw
    key = raw.lower()
    if key in SPDX_HINTS:
        return SPDX_HINTS[key]
    for hint, spdx in SPDX_HINTS.items():
        if key.startswith(hint):
            return spdx
    # Not a licence a centre would recognise: declare it proprietary rather
    # than claim an open licence the project may not carry.
    return "LicenseRef-proprietary"


def _developer_name(maintainer: str) -> str:
    """The human part of "Name <address>"."""
    name = (maintainer or "").split("<")[0].strip().strip(",")
    return name or "Unknown"


def _metainfo(app_id: str, pkg: str, name: str, summary: str,
              license_name: str, maintainer: str, version: str,
              homepage: str = "") -> str:
    """Software centres warn when there is no homepage, so one is always
    written: the project's own repository when it has one."""
    """AppStream metainfo — what a software centre actually reads."""
    import datetime
    today = datetime.date.today().isoformat()
    homepage = (homepage or "").strip() or "https://petacomm.com"
    url = f'  <url type="homepage">{_xml(homepage)}</url>\n'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<component type="desktop-application">\n'
        f'  <id>{_xml(app_id)}</id>\n'
        f'  <name>{_xml(name)}</name>\n'
        f'  <summary>{_xml(summary)}</summary>\n'
        f'  <metadata_license>CC0-1.0</metadata_license>\n'
        f'  <project_license>{_xml(_spdx(license_name))}</project_license>\n'
        f'  <developer id="io.petacore.packager">\n'
        f'    <name>{_xml(_developer_name(maintainer))}</name>\n'
        f'  </developer>\n'
        f'  <description>\n    <p>{_xml(summary)}</p>\n'
        f'    <p>Packaged with Petacore.</p>\n  </description>\n'
        f'  <launchable type="desktop-id">{_xml(pkg)}.desktop</launchable>\n'
        f'{url}'
        f'  <releases>\n'
        f'    <release version="{_xml(version)}" date="{today}"/>\n'
        f'  </releases>\n'
        f'  <content_rating type="oars-1.1"/>\n'
        f'</component>\n'
    )


def _desktop_entry(pkg: str, name: str, summary: str) -> str:
    """A desktop entry, so the launchable the metadata points at exists."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={summary}\n"
        f"Exec=/opt/{pkg}/{pkg}\n"
        f"Icon={pkg}\n"
        "Terminal=false\n"
        "Categories=Development;\n"
    )


def _xml(value: str) -> str:
    import html
    return html.escape(str(value or ""), quote=True)


def _safe_version(value: str) -> str:
    """A version string that is safe to put in a file name and in package
    metadata.

    The raw field reaches both a path and a control/spec file, so anything
    outside digits, dots and a short alphanumeric suffix is dropped: a value
    such as "../../tmp/x" would otherwise write the package outside the
    chosen folder, and an embedded newline would inject extra control
    fields."""
    cleaned = re.sub(r"[^A-Za-z0-9.+~]", "", (value or "").strip())
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip(".+~")
    # Debian requires a version to begin with a digit. Rather than hand the
    # user a puzzling dpkg error later, make it valid here.
    if not cleaned or not cleaned[0].isdigit():
        cleaned = "1.0.0" if not cleaned else "0." + cleaned
    return cleaned[:32]


def _safe_line(value: str, fallback: str = "") -> str:
    """One line of package metadata: no line breaks, no control characters.

    Both the Debian control file and the RPM spec are line-oriented, and the
    spec is executed by rpmbuild — a newline here would let a stray field or
    scriptlet be added to the package."""
    first = (value or "").replace("\r", "\n").split("\n")[0]
    first = "".join(ch for ch in first if ch.isprintable())
    return first.strip()[:200] or fallback


def _inside(directory: str, path: str) -> bool:
    """True when `path` really sits inside `directory`."""
    root = os.path.realpath(directory)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


def _pkg_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9+.-]+", "-", name.lower()).strip("-.")
    if not slug or not slug[0].isalnum():
        slug = "app-" + slug
    return slug[:60] or "petacore-app"


def repo_homepage(repo_url: str) -> str:
    """A browsable address from a git remote, for the package metadata.

    A software centre links this as the project's home page, so an ssh
    remote is rewritten to its https form and the .git suffix dropped."""
    url = (repo_url or "").strip()
    if not url:
        return ""
    if url.startswith("git@"):
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    elif url.startswith("ssh://git@"):
        url = "https://" + url[len("ssh://git@"):]
    if url.endswith(".git"):
        url = url[:-4]
    return url if url.startswith("http") else ""


def _write_appstream(staging: str, pkg: str, name: str, summary: str,
                     license_name: str, maintainer: str, version: str,
                     homepage: str = "") -> str:
    """Place the metainfo and desktop entry where a software centre looks.

    Both go under /usr/share so the centre indexes them; the application
    itself stays under /opt with the rest of the project."""
    app_id = f"io.petacore.{re.sub(r'[^A-Za-z0-9]', '', pkg) or 'app'}"

    meta_dir = os.path.join(staging, "usr", "share", "metainfo")
    os.makedirs(meta_dir, exist_ok=True)
    meta_path = os.path.join(meta_dir, f"{app_id}.metainfo.xml")
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(_metainfo(app_id, pkg, name, summary, license_name,
                          maintainer, version, homepage))

    desktop_dir = os.path.join(staging, "usr", "share", "applications")
    os.makedirs(desktop_dir, exist_ok=True)
    with open(os.path.join(desktop_dir, f"{pkg}.desktop"), "w",
              encoding="utf-8") as f:
        f.write(_desktop_entry(pkg, name, summary))
    return app_id


def build_deb(project_path: str, out_dir: str, name: str, version: str,
              maintainer: str, description: str,
              license_name: str = "Proprietary",
              homepage: str = "") -> str:
    project_path = os.path.realpath(project_path)
    pkg = _pkg_name(name)
    version = _safe_version(version)
    maintainer = _safe_line(maintainer, "Unknown <unknown@localhost>")
    description = _safe_line(description, "Packaged with Petacore")
    license_name = _safe_line(license_name, "Proprietary")

    if not shutil.which("dpkg-deb"):
        raise DebError("dpkg-deb is not installed (package: dpkg)")

    staging = tempfile.mkdtemp(prefix="petacore-deb-")
    try:
        debian_dir = os.path.join(staging, "DEBIAN")
        os.makedirs(debian_dir)

        install_root = os.path.join(staging, "opt", pkg)
        os.makedirs(os.path.dirname(install_root), exist_ok=True)
        shutil.copytree(
            project_path, install_root,
            ignore=_package_ignore,
            symlinks=True,
        )

        size_kb = 0
        for root, _dirs, files in os.walk(install_root):
            for f in files:
                try:
                    size_kb += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        size_kb //= 1024

        control = (
            f"Package: {pkg}\n"
            f"Version: {version}\n"
            f"Section: devel\n"
            f"Priority: optional\n"
            f"Architecture: all\n"
            f"Installed-Size: {max(size_kb, 1)}\n"
            f"Maintainer: {maintainer}\n"
            # Homepage is a real Debian field and is one of the few things a
            # software centre can read before the package is installed.
            + (f"Homepage: {homepage}\n" if homepage else "")
            # License is not part of the Debian format; it is written for the
            # benefit of tools that look for it, while the authoritative copy
            # lives in the AppStream metadata below.
            + f"License: {license_name}\n"
            f"Description: {description}\n"
            f" Built with Petacore.\n"
        )
        with open(os.path.join(debian_dir, "control"), "w", encoding="utf-8") as f:
            f.write(control)

        _write_appstream(staging, pkg, name, description, license_name,
                         maintainer, version, homepage)

        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, f"{pkg}_{version}_all.deb")
        if not _inside(out_dir, out_file):
            raise DebError("refusing to write outside the chosen folder")

        proc = subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", staging, out_file],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise DebError((proc.stderr or proc.stdout or "dpkg-deb failed").strip())
        return out_file
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_rpm(project_path: str, out_dir: str, name: str, version: str,
              maintainer: str, description: str,
              license_name: str = "Proprietary",
              homepage: str = "") -> str:
    """Build an .rpm that installs the project to /opt/<name>, via rpmbuild."""
    project_path = os.path.realpath(project_path)
    pkg = _pkg_name(name)
    version = _safe_version(version).replace("-", ".")
    description = _safe_line(description, "Packaged with Petacore")
    license_name = _safe_line(license_name, "Proprietary")

    if not shutil.which("rpmbuild"):
        raise DebError("rpmbuild is not installed (package: rpm)")

    top = tempfile.mkdtemp(prefix="petacore-rpm-")
    try:
        for d in ("SPECS", "SOURCES", "BUILD", "RPMS", "SRPMS", "BUILDROOT"):
            os.makedirs(os.path.join(top, d))
        payload = os.path.join(top, "SOURCES", "payload")
        shutil.copytree(project_path, payload,
                        ignore=_package_ignore,
                        symlinks=True)

        spec = os.path.join(top, "SPECS", f"{pkg}.spec")
        with open(spec, "w", encoding="utf-8") as f:
            f.write(
                "%define debug_package %{nil}\n"
                "%define __strip /bin/true\n"
                "%define _build_id_links none\n"
                f"Name: {pkg}\n"
                f"Version: {version}\n"
                "Release: 1\n"
                f"Summary: {description}\n"
                f"License: {license_name}\n"
                "AutoReqProv: no\n"
                "\n"
                f"%description\n{description}\nBuilt with Petacore.\n"
                "\n"
                "%install\n"
                f"mkdir -p %{{buildroot}}/opt/{pkg}\n"
                f"cp -a {payload}/. %{{buildroot}}/opt/{pkg}/\n"
                "\n"
                f"%files\n/opt/{pkg}\n"
            )

        proc = subprocess.run(
            ["rpmbuild", "-bb", "--define", f"_topdir {top}", spec],
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode != 0:
            raise DebError((proc.stderr or proc.stdout or "rpmbuild failed").strip()[-800:])

        built = []
        for root, _dirs, files in os.walk(os.path.join(top, "RPMS")):
            built += [os.path.join(root, f) for f in files if f.endswith(".rpm")]
        if not built:
            raise DebError("rpmbuild produced no package")

        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, os.path.basename(built[0]))
        shutil.copy2(built[0], out_file)
        return out_file
    finally:
        shutil.rmtree(top, ignore_errors=True)
