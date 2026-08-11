"""GPG package signing for Petacore.

A GPG key is the developer's digital signature: it proves a package really
came from them and was not modified. Petacore can create the key with one
click and then signs exported packages automatically:
 - .rpm  → signed inside the file (rpm --addsign), falling back to a
           detached .asc signature if rpm signing tools are unavailable
 - .deb  → detached armored .asc signature next to the file
"""

import os
import subprocess
import tempfile


class GpgError(Exception):
    pass


def _run(args, input_text=None, timeout=120):
    try:
        proc = subprocess.run(args, input=input_text, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise GpgError(f"{args[0]} is not installed")
    except subprocess.TimeoutExpired:
        raise GpgError("gpg operation timed out")
    if proc.returncode != 0:
        raise GpgError((proc.stderr or proc.stdout or "gpg failed").strip())
    return proc.stdout


def list_secret_keys():
    """[(fingerprint, uid)] of available signing keys."""
    return [(k["fpr"], k["uid"]) for k in list_keys_detailed()]


_ALGO = {"1": "RSA", "17": "DSA", "19": "ECDSA", "22": "Ed25519"}


def list_keys_detailed():
    """Full details for every secret key:
    [{fpr, uid, algo, bits, created, expires}] (timestamps as epoch or 0)."""
    try:
        out = _run(["gpg", "--batch", "--list-secret-keys", "--with-colons"])
    except GpgError:
        return []
    keys, cur = [], None
    for line in out.splitlines():
        f = line.split(":")
        if f[0] == "sec":
            cur = {"fpr": "", "uid": "",
                   "algo": _ALGO.get(f[3], f"algo{f[3]}"),
                   "bits": f[2],
                   "created": int(f[5]) if f[5].isdigit() else 0,
                   "expires": int(f[6]) if f[6].isdigit() else 0}
            keys.append(cur)
        elif f[0] == "fpr" and cur is not None and not cur["fpr"]:
            cur["fpr"] = f[9]
        elif f[0] == "uid" and cur is not None and not cur["uid"]:
            cur["uid"] = f[9]
    return [k for k in keys if k["fpr"]]


def default_key():
    keys = list_secret_keys()
    return keys[0] if keys else None


def signing_key(preferred_fpr: str = ""):
    """The key that will actually sign packages: the preferred one if it
    still exists, otherwise the first available. Returns (fpr, uid) or None."""
    keys = list_secret_keys()
    if not keys:
        return None
    for fpr, uid in keys:
        if preferred_fpr and fpr == preferred_fpr:
            return (fpr, uid)
    return keys[0]


def delete_key(fingerprint: str):
    """Remove a key pair permanently."""
    _run(["gpg", "--batch", "--yes",
          "--delete-secret-and-public-key", fingerprint])


def create_key(name: str, email: str, comment: str = "",
               key_type: str = "rsa3072", expire: str = "0",
               passphrase: str = "") -> str:
    """Create a signing key with full control over the details.

    key_type: rsa2048 | rsa3072 | rsa4096 | ed25519
    expire:   GnuPG format — "0" (never), "1y", "2y", "5y", …
    passphrase: empty → unprotected (fully automatic signing);
                set → GPG asks for it whenever a package is signed.
    """
    name = name.strip() or "Petacore Developer"
    email = email.strip() or "developer@localhost"

    if key_type == "ed25519":
        algo = "Key-Type: EDDSA\nKey-Curve: ed25519\n"
    else:
        bits = {"rsa2048": 2048, "rsa4096": 4096}.get(key_type, 3072)
        algo = f"Key-Type: RSA\nKey-Length: {bits}\n"

    lines = []
    if passphrase:
        lines.append(f"Passphrase: {passphrase}")
    else:
        lines.append("%no-protection")
    lines.append(algo.rstrip())
    lines.append("Key-Usage: sign")
    lines.append(f"Name-Real: {name}")
    if comment.strip():
        lines.append(f"Name-Comment: {comment.strip()}")
    lines.append(f"Name-Email: {email}")
    lines.append(f"Expire-Date: {expire or '0'}")
    lines.append("%commit")
    params = "\n".join(lines) + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".gpg", delete=False) as f:
        f.write(params)
        path = f.name
    try:
        _run(["gpg", "--batch", "--gen-key", path], timeout=300)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return f"{name} <{email}>"


def export_public(fingerprint: str, out_path: str) -> str:
    """Write the armored public key to out_path — this is the file the
    developer shares so users can verify signed packages."""
    out = _run(["gpg", "--batch", "--armor", "--export", fingerprint])
    if "BEGIN PGP PUBLIC KEY BLOCK" not in out:
        raise GpgError("Could not export public key")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    return out_path


def sign_detached(file_path: str, key: str) -> str:
    """Armored detached signature (.asc) next to the file. Passphrase-
    protected keys trigger the system GPG passphrase prompt."""
    asc = file_path + ".asc"
    _run(["gpg", "--yes", "--armor", "--detach-sign",
          "-u", key, "-o", asc, file_path])
    return asc


def sign_rpm(file_path: str, key_uid: str) -> str:
    """Embed the signature into the .rpm itself. Falls back to a detached
    .asc when rpm signing tooling is unavailable."""
    for tool in (["rpmsign", "--addsign"], ["rpm", "--addsign"]):
        try:
            _run(tool + [
                "--define", f"_gpg_name {key_uid}",
                "--define", "_gpg_path " + os.path.expanduser("~/.gnupg"),
                file_path,
            ])
            return file_path
        except GpgError:
            continue
    return sign_detached(file_path, key_uid)
