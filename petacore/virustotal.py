"""Checking a file against VirusTotal.

Two things happen here, and the difference between them matters more than
anything else in this file:

  * A **lookup** sends nothing but a SHA-256. The file never leaves the
    machine. If VirusTotal has seen that exact file before — because
    someone else scanned it, or because you uploaded it earlier — the
    stored report comes back. Otherwise the answer is simply "unknown".

  * An **upload** sends the file itself. On the free public API, anything
    uploaded is shared with VirusTotal's customers and partners, who can
    download it. For a developer that can mean handing an unreleased
    package, or whatever configuration is baked into it, to strangers.

So nothing is ever uploaded by this module unless the caller asks for it
explicitly, with `upload()`, and the interface asks the person first in
plain words. `scan()` — the ordinary path — only ever looks up a hash.

The API key belongs to the person, not to Petacore: it is theirs to create
at virustotal.com and it is kept in the desktop keyring beside the GitHub
token, never in a configuration file.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import secrets

KEY_NAME = "virustotal_api_key"
API_ROOT = "https://www.virustotal.com/api/v3"
KEY_PAGE = "https://www.virustotal.com/gui/my-apikey"

# The public API takes files up to 32 MB directly; larger ones need a
# one-time upload URL. Above this second limit VirusTotal refuses outright,
# so there is no point starting.
DIRECT_UPLOAD_LIMIT = 32 * 1024 * 1024
UPLOAD_LIMIT = 650 * 1024 * 1024

TIMEOUT = 60
# The free key allows 4 requests a minute. Polling an analysis faster than
# this would spend the whole quota waiting for one file.
POLL_INTERVAL = 16
POLL_ATTEMPTS = 12


class VTError(Exception):
    """A failure worth showing the person, in words they can act on."""


class QuotaError(VTError):
    pass


class AuthError(VTError):
    pass


# --------------------------------------------------------------------------- #
# The key
# --------------------------------------------------------------------------- #
def get_key() -> str:
    return secrets.retrieve(KEY_NAME)


def set_key(value: str) -> bool:
    """Store the key. Returns False when there is nowhere safe to put it.

    A key that cannot be stored in the keyring is not quietly written to a
    file here: the caller decides whether that trade is acceptable and says
    so, the same way the GitHub token is handled.
    """
    value = (value or "").strip()
    if not value:
        return False
    return secrets.store(KEY_NAME, value)


def clear_key() -> bool:
    return secrets.clear(KEY_NAME)


def key_present() -> bool:
    return bool(get_key())


def storage_kind() -> str:
    return secrets.storage_kind()


def why_unstored() -> str:
    return secrets.why_unavailable()


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def sha256_file(path: str) -> str:
    """The file's SHA-256, computed here. This is all a lookup ever sends."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Talking to the API
# --------------------------------------------------------------------------- #
def _request(url: str, key: str, method: str = "GET", body: bytes = None,
             content_type: str = None, timeout: int = TIMEOUT):
    if not key:
        raise AuthError("no API key has been set")
    request = urllib.request.Request(url, method=method, data=body)
    request.add_header("x-apikey", key)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "Petacore")
    if content_type:
        request.add_header("Content-Type", content_type)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None                      # "not seen", not a failure
        # Whatever VirusTotal says about the refusal is more useful than
        # anything invented here: "Wrong API key" and "quota exceeded" and
        # "user not active" all arrive as the same status code otherwise.
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")) \
                .get("error", {}).get("message", "")
        except (ValueError, OSError):
            pass
        suffix = f": {detail}" if detail else ""
        if e.code in (401, 403):
            raise AuthError(f"{e.code} {e.reason}{suffix}")
        if e.code == 429:
            raise QuotaError(f"the key's quota is spent{suffix}")
        raise VTError(f"{e.code} {e.reason}{suffix}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise VTError(str(getattr(e, "reason", e)))


def lookup(sha256: str, key: str = ""):
    """Ask what VirusTotal already knows about this hash. Uploads nothing.

    Returns the raw report, or None when the file has never been seen.
    """
    key = key or get_key()
    return _request(f"{API_ROOT}/files/{sha256}", key)


def scan(path: str, key: str = ""):
    """The ordinary path: hash the file locally and look the hash up.

    Nothing leaves the machine but 64 characters of hexadecimal.
    """
    return lookup(sha256_file(path), key or get_key())


# --------------------------------------------------------------------------- #
# Uploading — only ever on an explicit, informed request
# --------------------------------------------------------------------------- #
def _multipart(path: str):
    boundary = "----PetacoreBoundary" + hashlib.sha256(
        str(time.time()).encode()).hexdigest()[:16]
    name = os.path.basename(path)
    with open(path, "rb") as f:
        content = f.read()
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def upload(path: str, key: str = ""):
    """Send the file to VirusTotal. Returns an analysis id.

    Never called except from a place where the person has just been told,
    in plain words, that the file will be shared. Refuses a file too large
    for the public API rather than failing halfway through the transfer.
    """
    key = key or get_key()
    size = os.path.getsize(path)
    if size > UPLOAD_LIMIT:
        raise VTError("the file is larger than VirusTotal accepts")

    target = f"{API_ROOT}/files"
    if size > DIRECT_UPLOAD_LIMIT:
        reply = _request(f"{API_ROOT}/files/upload_url", key)
        target = (reply or {}).get("data", "")
        if not target:
            raise VTError("could not get an upload address")

    body, content_type = _multipart(path)
    reply = _request(target, key, method="POST", body=body,
                     content_type=content_type, timeout=600)
    analysis_id = (reply or {}).get("data", {}).get("id", "")
    if not analysis_id:
        raise VTError("the upload was not accepted")
    return analysis_id


def analysis(analysis_id: str, key: str = ""):
    return _request(f"{API_ROOT}/analyses/{analysis_id}", key or get_key())


def wait_for_analysis(analysis_id: str, key: str = "", on_wait=None):
    """Poll until the queued analysis finishes, then return the file report.

    Slowly on purpose: the free key allows four requests a minute, and a
    tight poll loop would burn the day's quota on a single file.
    """
    key = key or get_key()
    for attempt in range(POLL_ATTEMPTS):
        reply = analysis(analysis_id, key)
        data = (reply or {}).get("data", {})
        status = data.get("attributes", {}).get("status", "")
        if status == "completed":
            sha256 = (reply or {}).get("meta", {}) \
                .get("file_info", {}).get("sha256", "")
            return lookup(sha256, key) if sha256 else reply
        if on_wait:
            on_wait(attempt + 1, POLL_ATTEMPTS)
        time.sleep(POLL_INTERVAL)
    raise VTError("VirusTotal is still working on it — check again later")


# --------------------------------------------------------------------------- #
# Making a report readable
# --------------------------------------------------------------------------- #
def summarise(report, sha256: str = ""):
    """Turn a raw report into the handful of facts a person needs.

    `verdict` is deliberately three-valued. "unknown" is not "clean": a
    package built five minutes ago is unknown to VirusTotal, and calling
    that clean would be the single most misleading thing this feature
    could do.
    """
    empty = {"verdict": "unknown", "malicious": 0, "suspicious": 0,
             "harmless": 0, "undetected": 0, "engines": 0, "flagged": [],
             "when": 0, "permalink": "", "sha256": sha256, "name": ""}
    if not report:
        return empty

    data = report.get("data", report)
    attributes = data.get("attributes", {})
    stats = attributes.get("last_analysis_stats", {})

    malicious = int(stats.get("malicious", 0) or 0)
    suspicious = int(stats.get("suspicious", 0) or 0)
    harmless = int(stats.get("harmless", 0) or 0)
    undetected = int(stats.get("undetected", 0) or 0)
    engines = malicious + suspicious + harmless + undetected

    flagged = []
    for engine, result in (attributes.get("last_analysis_results")
                           or {}).items():
        if result.get("category") in ("malicious", "suspicious"):
            flagged.append((engine, result.get("result") or
                            result.get("category", "")))
    flagged.sort()

    digest = attributes.get("sha256") or sha256 or data.get("id", "")
    if engines == 0:
        verdict = "unknown"
    elif malicious or suspicious:
        verdict = "flagged"
    else:
        verdict = "clean"

    return {
        "verdict": verdict,
        "malicious": malicious,
        "suspicious": suspicious,
        "harmless": harmless,
        "undetected": undetected,
        "engines": engines,
        "flagged": flagged,
        "when": int(attributes.get("last_analysis_date", 0) or 0),
        "permalink": f"https://www.virustotal.com/gui/file/{digest}"
        if digest else "",
        "sha256": digest,
        "name": attributes.get("meaningful_name", ""),
    }


def key_shape_problem(key: str) -> str:
    """Why this cannot be a VirusTotal key, or "" if it might be.

    A key is 64 hexadecimal characters. Checked before any request because
    the usual reason a key is refused is a paste that lost its end, and
    "that is 48 characters, not 64" is a far more useful thing to be told
    than "refused".
    """
    key = (key or "").strip()
    if not key:
        return "the key is empty"
    if len(key) != 64:
        return (f"a VirusTotal key is 64 characters; this one is "
                f"{len(key)} — check the paste is complete")
    if any(c not in "0123456789abcdefABCDEF" for c in key):
        return "a VirusTotal key is made only of the digits 0-9 and a-f"
    return ""


def check_key(key: str) -> str:
    """Confirm a key works before storing it. Returns "" when it is good.

    The hash asked about is the SHA-256 of the empty file, which every
    installation of VirusTotal has already seen — so this proves the key
    without spending anything interesting and without sending any of the
    person's data.
    """
    problem = key_shape_problem(key)
    if problem:
        return problem
    empty_file_hash = hashlib.sha256(b"").hexdigest()
    try:
        lookup(empty_file_hash, key.strip())
        return ""
    except AuthError as e:
        return str(e)

# --------------------------------------------------------------------------- #
# Checking a whole project
#
# Sending files one by one would take the free key an hour for a project of
# a few hundred files. The project is sent instead as one zip, which
# VirusTotal unpacks and scans file by file — one request, not hundreds.
#
# The zip is built to be byte-for-byte the same whenever the project is:
# fixed timestamps, fixed permissions, a fixed order. So an unchanged
# project has an unchanged hash, the lookup finds the earlier result, and
# nothing is uploaded a second time.
#
# What goes in follows the packaging rules exactly: whatever Petacore would
# leave out of a .deb — credentials, .git, node_modules, the release
# history — is left out here too. A project's .env is never sent anywhere.
# --------------------------------------------------------------------------- #
import zipfile as _zipfile

STATE_FILE = "virustotal.json"
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _state_path(project_path: str) -> str:
    return os.path.join(project_path, ".petacore", STATE_FILE)


def load_state(project_path: str) -> dict:
    """The last result and the consent for this project. No key in here."""
    try:
        with open(_state_path(project_path), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(project_path: str, **values):
    state = load_state(project_path)
    state.update(values)
    path = _state_path(project_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def project_archive(project_path: str, out_dir: str = ""):
    """Zip the project for scanning. Returns (path, files, left_out).

    `left_out` lists the credential-shaped files that were kept back, so
    the interface can say so — the same promise packaging makes.
    """
    from . import debbuild
    project_path = os.path.realpath(project_path)
    out_dir = out_dir or tempfile.mkdtemp(prefix="petacore-vt-")
    name = os.path.basename(project_path.rstrip(os.sep)) or "project"
    archive = os.path.join(out_dir, f"{name}.zip")

    chosen = []
    for base, dirs, files in os.walk(project_path):
        skipped = debbuild._package_ignore(base, list(dirs) + list(files))
        dirs[:] = sorted(d for d in dirs if d not in skipped
                         and not os.path.islink(os.path.join(base, d)))
        for entry in sorted(files):
            full = os.path.join(base, entry)
            if entry in skipped or os.path.islink(full) \
                    or not os.path.isfile(full):
                continue
            chosen.append(full)

    with _zipfile.ZipFile(archive, "w", _zipfile.ZIP_DEFLATED) as zf:
        for full in chosen:
            info = _zipfile.ZipInfo(os.path.relpath(full, project_path),
                                    date_time=_FIXED_TIME)
            info.compress_type = _zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(full, "rb") as f:
                zf.writestr(info, f.read())

    return archive, len(chosen), debbuild.find_secrets(project_path)


def scan_project(project_path: str, allow_upload: bool, key: str = "",
                 on_status=None):
    """Check the project. Uploads only when `allow_upload` is True.

    Returns a dict: the summary, plus what was sent and whether an upload
    is still needed. With `allow_upload` False this never sends anything
    but a hash — the caller asks the person, then calls again.
    """
    key = key or get_key()

    def status(stage, *extra):
        if on_status:
            on_status(stage, *extra)

    status("packing")
    archive, count, left_out = project_archive(project_path)
    try:
        size = os.path.getsize(archive)
        if size > UPLOAD_LIMIT:
            raise VTError("the project is larger than VirusTotal accepts")
        digest = sha256_file(archive)
        result = {"sha256": digest, "files": count, "size": size,
                  "left_out": left_out, "uploaded": False,
                  "needs_upload": False}

        status("lookup")
        report = lookup(digest, key)
        if report is None and not allow_upload:
            result["needs_upload"] = True
            result["summary"] = summarise(None, digest)
            return result

        if report is None:
            status("uploading")
            analysis_id = upload(archive, key)
            result["uploaded"] = True
            report = wait_for_analysis(
                analysis_id, key,
                on_wait=lambda n, total: status("waiting", n, total))

        summary = summarise(report, digest)
        result["summary"] = summary
        save_state(project_path, sha256=digest, summary=summary,
                   scanned=time.time(), files=count, size=size)
        return result
    finally:
        shutil.rmtree(os.path.dirname(archive), ignore_errors=True)


# --------------------------------------------------------------------------- #
# Diagnosis from a terminal
#
# When the interface says a key was refused, the useful question is what
# exactly refused it: VirusTotal itself, or something between here and
# VirusTotal. This prints the raw answer to both an authenticated and an
# unauthenticated request, which separates the two cases, and never prints
# the key.
# --------------------------------------------------------------------------- #
def _diagnose(key: str):
    empty_hash = hashlib.sha256(b"").hexdigest()
    url = f"{API_ROOT}/files/{empty_hash}"

    problem = key_shape_problem(key)
    print(f"key shape       : {problem or 'looks like a VirusTotal key'}")
    print(f"key length      : {len(key.strip())} characters")
    print()

    for label, headers in (("with your key", {"x-apikey": key.strip()}),
                           ("with no key", {})):
        request = urllib.request.Request(url)
        for name, value in headers.items():
            request.add_header(name, value)
        request.add_header("User-Agent", "Petacore")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
                code, body = reply.status, reply.read(400)
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read(400)
        except (urllib.error.URLError, OSError) as e:
            print(f"{label:16}: could not reach VirusTotal — {e}")
            continue

        text = body.decode("utf-8", "replace").strip()
        kind = "JSON from VirusTotal" if text.startswith("{") \
            else "an HTML page, not the API"
        print(f"{label:16}: HTTP {code}  ({kind})")
        print(f"                  {text[:220]}")
        print()

    print("How to read this:")
    print("  200 with your key            the key works")
    print("  401 with your key            the key is wrong")
    print("  403 + JSON UserNotActive     confirm your VirusTotal e-mail")
    print("  403 + HTML, and 403 with no key")
    print("                               the block is on the network, not")
    print("                               the key — try another connection")
    print("  401 with no key              normal; the API is reachable")


if __name__ == "__main__":
    import getpass

    stored = get_key()
    entered = stored or getpass.getpass("VirusTotal API key (not echoed): ")
    if stored:
        print("Using the key already stored in the keyring.\n")
    _diagnose(entered)
