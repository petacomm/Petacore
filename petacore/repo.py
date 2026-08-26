"""Building an APT repository out of the packages Petacore produces.

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
      petacore-archive-keyring.asc     the public key users must trust

Nothing here needs a server to run: the tree is built locally and can then
be copied anywhere that serves static files.
"""

import gzip
import os
import shutil
import subprocess

DEFAULT_SUITE = "stable"
DEFAULT_COMPONENT = "main"
DEFAULT_ARCH = "amd64"
KEYRING_NAME = "petacore-archive-keyring.asc"


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
    """Which of the optional pieces are installed."""
    return {
        "apt-ftparchive": shutil.which("apt-ftparchive") is not None,
        "gpg": shutil.which("gpg") is not None,
        "dpkg-deb": shutil.which("dpkg-deb") is not None,
    }


# --------------------------------------------------------------------------- #
# Placing packages
# --------------------------------------------------------------------------- #
def package_fields(deb_path: str) -> dict:
    """Read Package, Version and Architecture out of a .deb."""
    text = _run(["dpkg-deb", "-f", deb_path])
    fields = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    if "Package" not in fields:
        raise RepoError("not a Debian package")
    return fields


def _pool_dir(root: str, component: str, package: str) -> str:
    # Debian groups by first letter, or by the "libx" prefix for libraries.
    letter = package[:4] if package.startswith("lib") else package[:1]
    return os.path.join(root, "pool", component, letter.lower(), package)


def add_package(root: str, deb_path: str, component: str = DEFAULT_COMPONENT):
    """Copy a package into the pool. Returns its path inside the repository."""
    fields = package_fields(deb_path)
    destination = _pool_dir(root, component, fields["Package"])
    os.makedirs(destination, exist_ok=True)
    target = os.path.join(destination, os.path.basename(deb_path))
    shutil.copy2(deb_path, target)
    return target


def list_packages(root: str):
    """Every package currently in the repository."""
    found = []
    pool = os.path.join(root, "pool")
    for base, _dirs, files in os.walk(pool):
        for name in sorted(files):
            if name.endswith(".deb"):
                found.append(os.path.join(base, name))
    return sorted(found)


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def _write_packages_index(root: str, suite: str, component: str, arch: str):
    index_dir = os.path.join(root, "dists", suite, component, f"binary-{arch}")
    os.makedirs(index_dir, exist_ok=True)

    if shutil.which("apt-ftparchive"):
        text = _run(["apt-ftparchive", "packages", "pool"], cwd=root)
    elif shutil.which("dpkg-scanpackages"):
        text = _run(["dpkg-scanpackages", "--multiversion", "pool"], cwd=root)
    else:
        raise RepoError("needs apt-utils (apt-ftparchive) or dpkg-dev")

    packages = os.path.join(index_dir, "Packages")
    with open(packages, "w", encoding="utf-8") as f:
        f.write(text)
    with open(packages, "rb") as src, gzip.open(packages + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    return packages


def _write_release(root: str, suite: str, component: str, arch: str,
                   origin: str, label: str, description: str):
    dist_dir = os.path.join(root, "dists", suite)
    if not shutil.which("apt-ftparchive"):
        raise RepoError("needs apt-utils (apt-ftparchive)")

    config = os.path.join(dist_dir, "apt-release.conf")
    with open(config, "w", encoding="utf-8") as f:
        f.write(
            f'APT::FTPArchive::Release::Origin "{origin}";\n'
            f'APT::FTPArchive::Release::Label "{label}";\n'
            f'APT::FTPArchive::Release::Suite "{suite}";\n'
            f'APT::FTPArchive::Release::Codename "{suite}";\n'
            f'APT::FTPArchive::Release::Architectures "{arch}";\n'
            f'APT::FTPArchive::Release::Components "{component}";\n'
            f'APT::FTPArchive::Release::Description "{description}";\n')

    text = _run(["apt-ftparchive", "-c", config, "release", "."], cwd=dist_dir)
    release = os.path.join(dist_dir, "Release")
    with open(release, "w", encoding="utf-8") as f:
        f.write(text)
    os.remove(config)
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


def export_key(root: str, key_fingerprint: str) -> str:
    """Write the public key users will need in order to trust the archive."""
    path = os.path.join(root, KEYRING_NAME)
    text = _run(["gpg", "--armor", "--export", key_fingerprint])
    if "BEGIN PGP PUBLIC KEY BLOCK" not in text:
        raise RepoError("could not export the public key")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def build(root: str, key_fingerprint: str, suite: str = DEFAULT_SUITE,
          component: str = DEFAULT_COMPONENT, arch: str = DEFAULT_ARCH,
          origin: str = "Petacomm", label: str = "Petacomm",
          description: str = "Packages built with Petacore"):
    """Regenerate the indexes and sign them. Safe to run repeatedly."""
    if not list_packages(root):
        raise RepoError("the repository has no packages in it yet")
    _write_packages_index(root, suite, component, arch)
    release = _write_release(root, suite, component, arch, origin, label,
                             description)
    _sign_release(release, key_fingerprint)
    export_key(root, key_fingerprint)
    return root


# --------------------------------------------------------------------------- #
# What users need to be told
# --------------------------------------------------------------------------- #
def install_instructions(base_url: str, suite: str = DEFAULT_SUITE,
                         component: str = DEFAULT_COMPONENT,
                         name: str = "petacomm") -> str:
    """The two commands a user runs once to trust and add the archive."""
    base_url = (base_url or "https://example.com/apt").rstrip("/")
    return (
        f"# Trust the archive key\n"
        f"curl -fsSL {base_url}/{KEYRING_NAME} \\\n"
        f"  | sudo gpg --dearmor -o /usr/share/keyrings/{name}.gpg\n"
        f"\n"
        f"# Add the repository\n"
        f"echo \"deb [signed-by=/usr/share/keyrings/{name}.gpg] "
        f"{base_url} {suite} {component}\" \\\n"
        f"  | sudo tee /etc/apt/sources.list.d/{name}.list\n"
        f"\n"
        f"# Install\n"
        f"sudo apt update && sudo apt install <package>\n")
