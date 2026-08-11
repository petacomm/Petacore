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


class DebError(Exception):
    pass


def _pkg_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9+.-]+", "-", name.lower()).strip("-.")
    if not slug or not slug[0].isalnum():
        slug = "app-" + slug
    return slug[:60] or "petacore-app"


def build_deb(project_path: str, out_dir: str, name: str, version: str,
              maintainer: str, description: str,
              license_name: str = "Proprietary") -> str:
    project_path = os.path.realpath(project_path)
    pkg = _pkg_name(name)
    version = version.strip() or "1.0.0"
    maintainer = maintainer.strip() or "Unknown <unknown@localhost>"
    description = (description.strip() or "Packaged with Petacore").splitlines()[0]

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
            ignore=shutil.ignore_patterns(*EXCLUDE),
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
            f"License: {license_name.strip() or 'Proprietary'}\n"
            f"Description: {description}\n"
            f" Built with Petacore.\n"
        )
        with open(os.path.join(debian_dir, "control"), "w", encoding="utf-8") as f:
            f.write(control)

        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, f"{pkg}_{version}_all.deb")

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
              license_name: str = "Proprietary") -> str:
    """Build an .rpm that installs the project to /opt/<name>, via rpmbuild."""
    project_path = os.path.realpath(project_path)
    pkg = _pkg_name(name)
    version = (version.strip() or "1.0.0").replace("-", ".")
    description = (description.strip() or "Packaged with Petacore").splitlines()[0]

    if not shutil.which("rpmbuild"):
        raise DebError("rpmbuild is not installed (package: rpm)")

    top = tempfile.mkdtemp(prefix="petacore-rpm-")
    try:
        for d in ("SPECS", "SOURCES", "BUILD", "RPMS", "SRPMS", "BUILDROOT"):
            os.makedirs(os.path.join(top, d))
        payload = os.path.join(top, "SOURCES", "payload")
        shutil.copytree(project_path, payload,
                        ignore=shutil.ignore_patterns(*EXCLUDE),
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
                f"License: {license_name.strip() or 'Proprietary'}\n"
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
