"""Managing a repository that lives on a server, over SSH.

This is the most sensitive thing Petacore does: it holds a connection that
can write to a public web server. The design follows from that.

**OpenSSH does the cryptography, not Petacore.** Every connection is made
by the system's own ssh and scp. They are audited, they already understand
known_hosts, the SSH agent, FIDO2 security keys and two-factor login, and
re-implementing any of that here would only make it weaker.

**Petacore narrows what ssh will accept.** Every call carries an explicit,
hardened set of options instead of whatever a config file says:
  * the user's ~/.ssh/config is not read (-F /dev/null), so no ProxyCommand,
    forwarding or weak setting reaches these connections by accident;
  * the server's host key must already be trusted, in Petacore's own
    known_hosts file; a changed key stops everything;
  * only modern algorithms are offered — post-quantum hybrid key exchange
    where the installed OpenSSH has it, AEAD ciphers, encrypt-then-MAC;
  * only the chosen key is offered, and no password is ever sent;
  * no agent, X11 or port forwarding, no local commands.

**Nothing secret is stored.** A key's passphrase and a two-factor code are
asked for through SSH_ASKPASS (see askpass.py), answered in the window,
handed to ssh in memory and forgotten. The person authenticates once; the
rest of the session runs over one multiplexed connection whose control
socket sits in a directory only they can enter, and which closes itself
after ten idle minutes.

**Everything stays inside the repository folder.** Remote paths are built
here, never taken from a text field whole: ".." is refused, names cannot
contain a slash, and every command is passed as quoted arguments, so a file
called "x; rm -rf ~" is a file with an odd name and nothing more.
"""

import hmac
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

from .config import CONFIG_DIR

KNOWN_HOSTS = os.path.join(CONFIG_DIR, "known_hosts")
TRASH_DIRNAME = ".petacore-trash"
# Folders that can never be a repository's root: every operation is confined
# to the root, so choosing one of these would put the server itself in reach
# of a rename or a trash.
SYSTEM_DIRS = {"/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32",
               "/lib64", "/opt", "/proc", "/root", "/run", "/sbin", "/srv",
               "/sys", "/tmp", "/usr", "/var", "/var/www", "/var/lib",
               "/var/log", "/snap", "/mnt", "/media"}
PART_SUFFIX = ".petacore-part"
IDLE_SECONDS = 600
CONNECT_TIMEOUT = 15

# Strongest first. Each list is intersected with what the installed ssh
# actually supports (ssh -Q), since naming an algorithm it does not know is
# a hard error — mlkem768x25519 only arrived in OpenSSH 9.9.
POLICY = {
    "kex": ["mlkem768x25519-sha256",
            "sntrup761x25519-sha512",
            "sntrup761x25519-sha512@openssh.com",
            "curve25519-sha256", "curve25519-sha256@libssh.org"],
    "cipher": ["chacha20-poly1305@openssh.com",
               "aes256-gcm@openssh.com", "aes128-gcm@openssh.com"],
    "mac": ["hmac-sha2-512-etm@openssh.com",
            "hmac-sha2-256-etm@openssh.com"],
    "key-sig": ["ssh-ed25519", "sk-ssh-ed25519@openssh.com",
                "rsa-sha2-512", "rsa-sha2-256",
                "ecdsa-sha2-nistp521", "ecdsa-sha2-nistp384",
                "ecdsa-sha2-nistp256"],
}
POST_QUANTUM = {"mlkem768x25519-sha256", "sntrup761x25519-sha512",
                "sntrup761x25519-sha512@openssh.com"}


# The steps shown to the person for turning two-factor login on. Kept here,
# once, so both interfaces show the same text. Written for someone doing it
# on a live server: the part that matters most is not locking yourself out.
TWO_FACTOR_HOWTO = """# 0. Keep this SSH session open until step 5 works in a second window.
#    A mistake in these files locks SSH logins out; the open session is
#    how you undo it.

sudo apt install libpam-google-authenticator
google-authenticator     # as the user Petacore logs in as: scan the QR
                         # code, and write the emergency codes down somewhere
                         # that is not this phone

# /etc/pam.d/sshd: add this line, and comment out "@include common-auth"
auth required pam_google_authenticator.so

# /etc/ssh/sshd_config (and check /etc/ssh/sshd_config.d/ does not undo it)
KbdInteractiveAuthentication yes
AuthenticationMethods publickey,keyboard-interactive

sudo sshd -t && sudo systemctl restart ssh    # -t checks the files first

# 5. In a NEW terminal: ssh user@server - it must ask for the code.
#    Only when that works, close the old session."""


class RemoteError(Exception):
    pass


class HostKeyUnknown(RemoteError):
    """The server has never been trusted. Carries what to show the person."""

    def __init__(self, fingerprint, key_type, line):
        super().__init__("the server's identity has not been confirmed")
        self.fingerprint, self.key_type, self.line = \
            fingerprint, key_type, line


class HostKeyChanged(RemoteError):
    pass


class AuthFailed(RemoteError):
    pass


class NotConnected(RemoteError):
    pass


# --------------------------------------------------------------------------- #
# The places this module writes to, all private to the user
# --------------------------------------------------------------------------- #
def _private_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    info = os.stat(path)
    if info.st_uid != os.getuid():
        raise RemoteError(f"{path} belongs to another user")
    return path


def runtime_dir() -> str:
    """Where the control socket and the askpass socket live.

    $XDG_RUNTIME_DIR is a tmpfs owned by the user and emptied at logout,
    which is exactly right for sockets. The path is kept short on purpose:
    a Unix socket path is limited to about a hundred bytes.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not os.path.isdir(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return _private_dir(os.path.join(base, "petacore"))


def _known_hosts() -> str:
    _private_dir(os.path.dirname(KNOWN_HOSTS))
    if not os.path.exists(KNOWN_HOSTS):
        with open(KNOWN_HOSTS, "a", encoding="utf-8"):
            pass
    os.chmod(KNOWN_HOSTS, 0o600)
    return KNOWN_HOSTS


# --------------------------------------------------------------------------- #
# Who we are talking to
# --------------------------------------------------------------------------- #
_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*"
                   r"|\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]+)$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


def normalise_target(values: dict) -> dict:
    """Validate a server description before any of it reaches a command.

    Each field ends up as an argument to ssh, so each is checked against
    what it can legitimately look like; anything else is refused, never
    trimmed into shape.
    """
    host = str(values.get("host", "")).strip()
    user = str(values.get("user", "")).strip()
    root = str(values.get("root", "")).strip()
    identity = os.path.expanduser(str(values.get("identity", "")).strip())
    try:
        port = int(values.get("port") or 22)
    except (TypeError, ValueError):
        raise RemoteError("the port must be a number")

    if not host or not _HOST.match(host) or host.startswith("-"):
        raise RemoteError("that is not a server name or address")
    if not _USER.match(user):
        raise RemoteError("that is not a valid user name")
    if not 1 <= port <= 65535:
        raise RemoteError("the port must be between 1 and 65535")
    if not root.startswith("/") or "\0" in root:
        raise RemoteError("the repository folder must be an absolute path")
    # Checked before normalising, not after: "/var/www/../../etc" must be
    # refused as written, not quietly turned into "/etc".
    if ".." in root.split("/"):
        raise RemoteError("the repository folder cannot contain '..'")
    root = posixpath.normpath(root)
    if root == "/" or root in SYSTEM_DIRS:
        raise RemoteError("choose a folder of its own, not a system folder")
    if identity and not os.path.isfile(identity):
        raise RemoteError("the chosen SSH key does not exist")
    return {"host": host, "user": user, "port": port, "root": root,
            "identity": identity}


def _host_label(target) -> str:
    """How known_hosts names this server: bare for port 22, else [h]:p."""
    if target["port"] == 22:
        return target["host"]
    return f'[{target["host"]}]:{target["port"]}'


# --------------------------------------------------------------------------- #
# The option set every connection carries
# --------------------------------------------------------------------------- #
_supported_cache = {}


def supported(kind: str):
    if kind not in _supported_cache:
        try:
            out = subprocess.run(["ssh", "-Q", kind], capture_output=True,
                                 text=True, timeout=10).stdout
            _supported_cache[kind] = set(out.split())
        except (OSError, subprocess.SubprocessError):
            _supported_cache[kind] = set()
    return _supported_cache[kind]


def _choose(kind):
    available = supported(kind)
    return [a for a in POLICY[kind] if a in available]


def _option_supported(option: str) -> bool:
    key = f"opt:{option}"
    if key not in _supported_cache:
        proc = subprocess.run(["ssh", "-F", "/dev/null", "-G", "-o", option,
                               "localhost"], capture_output=True, text=True,
                              timeout=10)
        _supported_cache[key] = proc.returncode == 0
    return _supported_cache[key]


def hardened_options(target) -> list:
    """The -o options for every ssh and scp call. Nothing is inherited."""
    opts = [
        "-F", "/dev/null",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={_known_hosts()}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "CheckHostIP=no",
        "-o", "UpdateHostKeys=no",
        "-o", "HashKnownHosts=no",
        "-o", "IdentitiesOnly=yes",
        "-o", "PubkeyAuthentication=yes",
        "-o", "KbdInteractiveAuthentication=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "PreferredAuthentications=publickey,keyboard-interactive",
        "-o", "GSSAPIAuthentication=no",
        "-o", "HostbasedAuthentication=no",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "PermitLocalCommand=no",
        "-o", "Tunnel=no",
        "-o", "ConnectTimeout=%d" % CONNECT_TIMEOUT,
        "-o", "ServerAliveInterval=20",
        "-o", "ServerAliveCountMax=3",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "LogLevel=ERROR",
        "-o", f"ControlPath={os.path.join(runtime_dir(), 'cm-%C')}",
    ]
    for kind, name in (("kex", "KexAlgorithms"), ("cipher", "Ciphers"),
                       ("mac", "MACs"), ("key-sig", "HostKeyAlgorithms")):
        chosen = _choose(kind)
        if chosen:
            opts += ["-o", f"{name}={','.join(chosen)}"]
    # Refuse RSA keys too short to be trusted, where ssh knows how to.
    if _option_supported("RequiredRSASize=3072"):
        opts += ["-o", "RequiredRSASize=3072"]
    if target.get("identity"):
        opts += ["-o", f"IdentityFile={target['identity']}"]
    return opts


def security_summary():
    """What the connection will use, in words the settings page can show."""
    kex = _choose("kex")
    ciphers = _choose("cipher")
    return {
        "post_quantum": bool(kex) and kex[0] in POST_QUANTUM,
        "kex": kex[0] if kex else "",
        "cipher": ciphers[0] if ciphers else "",
    }


# --------------------------------------------------------------------------- #
# The server's identity
# --------------------------------------------------------------------------- #
def _fingerprint(line: str):
    proc = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", "-"],
                          input=line + "\n", capture_output=True, text=True,
                          timeout=10)
    parts = proc.stdout.split()
    if proc.returncode != 0 or len(parts) < 2:
        return "", ""
    key_type = parts[-1].strip("()") if parts[-1].startswith("(") else ""
    return parts[1], key_type


def scan_host_key(target):
    """Ask the server for its public key, preferring Ed25519.

    This conversation is not yet authenticated — which is precisely why the
    fingerprint is shown to the person to compare against the server itself
    before anything is trusted.
    """
    for key_type in ("ed25519", "ecdsa", "rsa"):
        try:
            proc = subprocess.run(
                ["ssh-keyscan", "-T", "10", "-p", str(target["port"]),
                 "-t", key_type, "--", target["host"]],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            raise RemoteError(f"could not reach the server: {e}")
        for line in proc.stdout.splitlines():
            if line and not line.startswith("#"):
                fields = line.split()
                if len(fields) >= 3:
                    entry = f"{_host_label(target)} {fields[1]} {fields[2]}"
                    fingerprint, shown = _fingerprint(entry)
                    if fingerprint:
                        return fingerprint, shown or key_type.upper(), entry
    raise RemoteError("could not reach the server, or it offered no key")


def known_entries(target):
    try:
        proc = subprocess.run(["ssh-keygen", "-F", _host_label(target),
                               "-f", _known_hosts()],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in proc.stdout.splitlines()
            if line and not line.startswith("#")]


def trust_host_key(target, line: str):
    """Record a key the person has confirmed. Only ever called after they
    have compared the fingerprint themselves."""
    fields = line.split()
    if len(fields) != 3 or fields[0] != _host_label(target):
        raise RemoteError("that is not a key for this server")
    with open(_known_hosts(), "a", encoding="utf-8") as f:
        f.write(line.strip() + "\n")


def forget_host_key(target):
    """Remove a server's recorded key — only on an explicit request, after
    the person has been told why a changed key is alarming."""
    subprocess.run(["ssh-keygen", "-R", _host_label(target), "-f",
                    _known_hosts()], capture_output=True, timeout=10)
    backup = _known_hosts() + ".old"
    if os.path.exists(backup):
        os.remove(backup)


def check_host(target):
    """"known", or raise HostKeyUnknown / HostKeyChanged."""
    fingerprint, key_type, line = scan_host_key(target)
    known = known_entries(target)
    if not known:
        raise HostKeyUnknown(fingerprint, key_type, line)
    offered = line.split()[1:]
    for entry in known:
        if entry.split()[1:] == offered:
            return "known"
    raise HostKeyChanged(
        "the server's key is not the one you trusted before — this is "
        "what an interception attack looks like")


# --------------------------------------------------------------------------- #
# The bridge between ssh's questions and the window
# --------------------------------------------------------------------------- #
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x1b]")


def clean_prompt(text: str) -> str:
    """A prompt as it may be shown. Keyboard-interactive prompts are written
    by the server, so terminal escapes and control characters are stripped
    and the length is bounded: a hostile server must not be able to redraw
    the question into something else."""
    return _CONTROL.sub("", text or "").strip()[:300]


def classify(prompt: str, style: str) -> str:
    lower = prompt.lower()
    if style == "none" or "user presence" in lower or "touch" in lower:
        return "touch"
    if style == "confirm":
        return "confirm"
    if "passphrase" in lower:
        return "passphrase"
    if any(w in lower for w in ("verification", "one-time", "otp",
                                "authenticator", "token", "code")):
        return "code"
    if "password" in lower:
        return "password"
    return "other"


class AskpassBridge:
    """A private socket the askpass helper uses to reach the window.

    `handler(kind, prompt)` is called on a background thread and returns the
    answer, or None to cancel. It is the only place an answer exists.
    """

    def __init__(self, handler):
        self.handler = handler
        self.token = secrets.token_hex(32)
        self.path = os.path.join(runtime_dir(), f"ap-{secrets.token_hex(6)}")
        self.helper = self._write_helper()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(4)
        self._closed = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _write_helper(self):
        helper = os.path.join(runtime_dir(), "askpass")
        body = ("#!/bin/sh\n"
                f"exec {shlex.quote(sys.executable)} -m petacore.askpass "
                '"$@"\n')
        current = ""
        if os.path.exists(helper):
            with open(helper, encoding="utf-8") as f:
                current = f.read()
        if current != body:
            fd = os.open(helper + ".new",
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(helper + ".new", helper)
        os.chmod(helper, 0o700)
        return helper

    def env(self) -> dict:
        package_root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        env = dict(os.environ)
        env.update({
            "SSH_ASKPASS": self.helper,
            "SSH_ASKPASS_REQUIRE": "force",
            "PETACORE_ASKPASS_SOCK": self.path,
            "PETACORE_ASKPASS_TOKEN": self.token,
            "PYTHONPATH": package_root + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH")
                else ""),
        })
        env.setdefault("DISPLAY", ":0")
        return env

    def _peer_is_us(self, conn) -> bool:
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            return uid == os.getuid()
        except (OSError, AttributeError):
            return False

    def _serve(self):
        while not self._closed:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,),
                             daemon=True).start()

    def _answer(self, conn):
        with conn:
            if not self._peer_is_us(conn):
                return
            conn.settimeout(300)
            data = b""
            try:
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.decode("utf-8", "replace"))
            except (OSError, ValueError):
                return
            if not hmac.compare_digest(str(request.get("token", "")),
                                       self.token):
                return
            prompt = clean_prompt(request.get("prompt", ""))
            style = request.get("style", "")
            kind = classify(prompt, style)
            try:
                answer = self.handler(kind, prompt)
            except Exception:  # noqa: BLE001 - a broken dialog cancels
                answer = None
            reply = {"answer": answer}
            try:
                conn.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass
            answer = reply = None        # not kept a moment longer

    def close(self):
        self._closed = True
        try:
            self.sock.close()
        finally:
            if os.path.exists(self.path):
                os.remove(self.path)


# --------------------------------------------------------------------------- #
# A session: one authentication, many operations
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, target: dict, handler=None):
        self.target = normalise_target(target)
        self.root = self.target["root"]
        self.handler = handler or (lambda kind, prompt: None)

    # -- ssh invocation ---------------------------------------------------------
    def _ssh_base(self):
        return (["ssh"] + hardened_options(self.target)
                + ["-p", str(self.target["port"]), "-l", self.target["user"]])

    def _destination(self):
        host = self.target["host"]
        return f"[{host}]" if ":" in host else host

    def connect(self):
        """Authenticate once and leave a master connection running.

        The host key is checked first, separately, so an unknown or changed
        key is reported as exactly that rather than as a failed login.
        """
        check_host(self.target)
        if self.connected():
            return
        bridge = AskpassBridge(self.handler)
        errors = tempfile.TemporaryFile(mode="w+")
        try:
            # -f returns once authentication has succeeded and leaves the
            # master in the background; stderr goes to a file because the
            # backgrounded process keeps it open, and a pipe would never
            # reach end-of-file.
            proc = subprocess.run(
                self._ssh_base() + [
                    "-o", "ControlMaster=yes",
                    "-o", f"ControlPersist={IDLE_SECONDS}",
                    "-N", "-f", "--", self._destination()],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=errors, env=bridge.env(), timeout=600)
            errors.seek(0)
            message = errors.read().strip()
        finally:
            bridge.close()
            errors.close()
        if proc.returncode != 0 or not self.connected():
            lower = message.lower()
            if "host key" in lower and "changed" in lower:
                raise HostKeyChanged(message)
            if "permission denied" in lower or "authentication" in lower:
                raise AuthFailed(message or "the server refused the login")
            raise RemoteError(message or "could not connect")

    def connected(self) -> bool:
        proc = subprocess.run(
            self._ssh_base() + ["-O", "check", "--", self._destination()],
            capture_output=True, timeout=10)
        return proc.returncode == 0

    def disconnect(self):
        subprocess.run(
            self._ssh_base() + ["-O", "exit", "--", self._destination()],
            capture_output=True, timeout=10)

    def run(self, argv, input_bytes=None, timeout=300) -> bytes:
        """Run one command on the server over the existing connection.

        Arguments are quoted here, one by one; nothing from a file name or a
        text field is ever read by the remote shell as code. BatchMode means
        a dead connection fails rather than quietly asking to log in again.
        """
        if not self.connected():
            raise NotConnected("the connection has closed — connect again")
        command = " ".join(shlex.quote(str(a)) for a in argv)
        proc = subprocess.run(
            self._ssh_base() + ["-o", "ControlMaster=no",
                                "-o", "BatchMode=yes",
                                "--", self._destination(), command],
            input=input_bytes, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RemoteError(proc.stderr.decode("utf-8", "replace")
                              .strip()[-400:] or "the command failed")
        return proc.stdout

    def _scp(self, source, destination, timeout=1800):
        if not self.connected():
            raise NotConnected("the connection has closed — connect again")
        # -s: SFTP rather than the old scp protocol, so the remote path is
        # never handed to a shell on the other side.
        cmd = (["scp", "-s", "-q", "-P", str(self.target["port"])]
               + hardened_options(self.target)
               + ["-o", "ControlMaster=no", "-o", "BatchMode=yes",
                  "--", source, destination])
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RemoteError(proc.stderr.decode("utf-8", "replace")
                              .strip()[-400:] or "the transfer failed")

    def _remote_spec(self, path):
        return f'{self.target["user"]}@{self._destination()}:{path}'

    # -- paths ------------------------------------------------------------------
    def path(self, rel: str = "") -> str:
        """An absolute path on the server that is certain to be inside the
        repository folder."""
        parts = []
        for part in (rel or "").replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == ".." or "\0" in part:
                raise RemoteError("that path leaves the repository folder")
            parts.append(part)
        return posixpath.join(self.root, *parts)

    def confine(self, *paths):
        """Refuse any path whose real location is outside the repository.

        path() only looks at the text, so a symbolic link on the server — a
        "pool" that points at /etc — would pass it while every write through
        it lands somewhere else entirely. So the server resolves the paths
        itself, links and all, in one call over the existing connection,
        and each one must still be under the resolved root. Missing parts
        are allowed (an upload creates them) but resolved as far as they go.
        """
        if not paths:
            return
        out = self.run(["realpath", "-m", "--", self.root] + list(paths))
        resolved = out.decode("utf-8", "replace").split("\n")
        real_root = resolved[0].rstrip("/") or "/"
        for original, real in zip(paths, resolved[1:]):
            if real != real_root and not real.startswith(real_root + "/"):
                raise RemoteError(f"{original} leads outside the repository "
                                  "folder (through a symbolic link)")

    @staticmethod
    def safe_name(name: str) -> str:
        name = (name or "").strip()
        if not name or name in (".", "..") or "/" in name or "\0" in name:
            raise RemoteError("that is not a usable file name")
        if name.endswith(PART_SUFFIX) or name == TRASH_DIRNAME:
            raise RemoteError("that name is reserved")
        return name

    # -- reading ----------------------------------------------------------------
    def listdir(self, rel: str = ""):
        directory = self.path(rel)
        self.confine(directory)
        out = self.run(["find", directory, "-mindepth", "1", "-maxdepth", "1",
                        "-printf", r"%y\t%s\t%T@\t%f\0"])
        entries = []
        for record in out.decode("utf-8", "replace").split("\0"):
            fields = record.split("\t", 3)
            if len(fields) != 4:
                continue
            kind, size, mtime, name = fields
            if name == TRASH_DIRNAME or name.endswith(PART_SUFFIX):
                continue
            entries.append({
                "name": name,
                "rel": posixpath.join(rel.strip("/"), name) if rel.strip("/")
                else name,
                "dir": kind == "d",
                "link": kind == "l",
                "size": int(size) if size.isdigit() else 0,
                "mtime": float(mtime) if mtime else 0.0,
            })
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return entries

    def ensure_root(self):
        self.run(["mkdir", "-p", "--", self.root])

    def tools(self):
        out = self.run(["sh", "-c",
                        "for t in apt-ftparchive dpkg-scanpackages; do "
                        "command -v \"$t\" >/dev/null 2>&1 && echo \"$t\"; "
                        "done; exit 0"])
        return set(out.decode().split())

    # -- changing -----------------------------------------------------------------
    def mkdir(self, rel_dir: str, name: str):
        target = self.path(posixpath.join(rel_dir, self.safe_name(name)))
        self.confine(target)
        self.run(["mkdir", "--", target])

    def rename(self, rel: str, new_name: str):
        source = self.path(rel)
        target = posixpath.join(posixpath.dirname(source),
                                self.safe_name(new_name))
        if source == self.root:
            raise RemoteError("the repository folder itself cannot be renamed")
        # the folder the entry sits in, not the entry: renaming a link
        # renames the link, but a linked folder above it would move things
        # somewhere else
        self.confine(posixpath.dirname(source), target)
        self.run(["mv", "-n", "--", source, target])

    def upload(self, local_path: str, rel_dir: str = "", name: str = ""):
        """Copy a file up, then move it into place in one step.

        It travels under a hidden temporary name and is renamed only when
        complete, so a web server never hands anyone half a package.
        """
        if not os.path.isfile(local_path):
            raise RemoteError(f"no such file: {local_path}")
        name = self.safe_name(name or os.path.basename(local_path))
        directory = self.path(rel_dir)
        final = posixpath.join(directory, name)
        part = posixpath.join(directory, f".{name}{PART_SUFFIX}")
        self.confine(directory, final)
        self.run(["mkdir", "-p", "--", directory])
        self._scp(local_path, self._remote_spec(part))
        self.run(["sh", "-c", 'chmod 644 -- "$1" && mv -f -- "$1" "$2"',
                  "sh", part, final])
        return posixpath.relpath(final, self.root)

    def upload_package(self, local_deb: str, component: str = "main"):
        """Put a .deb where apt expects it: pool/<component>/<l>/<name>/."""
        from . import repo
        fields = repo.package_fields(local_deb)
        package = fields["Package"]
        letter = package[:4] if package.startswith("lib") else package[:1]
        rel_dir = posixpath.join("pool", component, letter.lower(), package)
        return self.upload(local_deb, rel_dir)

    def download(self, rel: str, local_path: str):
        source = self.path(rel)
        # resolved in full: downloading through a link must not hand over
        # a file from elsewhere on the server
        self.confine(source)
        self._scp(self._remote_spec(source), local_path)

    def trash(self, rels):
        """Move entries into a dated folder inside the repository."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bin_dir = self.path(posixpath.join(TRASH_DIRNAME, stamp))
        sources = [self.path(rel) for rel in rels]
        if self.root in sources:
            raise RemoteError("the repository folder itself cannot be "
                              "removed")
        self.confine(bin_dir, *[posixpath.dirname(s) for s in sources])
        self.run(["mkdir", "-p", "--", bin_dir])
        record = []
        for rel in rels:
            source = self.path(rel)
            target = posixpath.join(bin_dir, rel.strip("/").replace("/", "__"))
            self.run(["mv", "-n", "--", source, target])
            record.append((source, target))
        return record

    def restore(self, record):
        for source, target in record:
            self.run(["sh", "-c", 'mkdir -p -- "$(dirname -- "$1")" && '
                      'mv -n -- "$2" "$1"', "sh", source, target])

    # -- the index, built there and signed here -----------------------------------
    def rebuild_index(self, profile):
        """Regenerate and sign the server's index without the key leaving
        this machine.

        The server lists its own pool (it has the packages; this machine
        may not); only that listing comes down. The Release file is written
        and signed here, and the finished index goes back up — Packages
        first and the signed Release last, so a client updating in the
        middle never sees a Release that describes files not there yet.
        """
        from . import repo
        profile = repo.normalise(profile)
        if not profile["key"]:
            raise RemoteError("no signing key has been chosen")
        tools = self.tools()
        if not tools:
            raise RemoteError("the server needs dpkg-dev to list its packages "
                              "(sudo apt install dpkg-dev)")

        work = tempfile.mkdtemp(prefix="petacore-remote-")
        try:
            suite, component = profile["suite"], profile["component"]
            uploads = []
            for arch in profile["archs"]:
                listing = self.run(["sh", "-c",
                    'cd -- "$1" || exit 1; mkdir -p pool; '
                    'if command -v apt-ftparchive >/dev/null 2>&1; then '
                    'apt-ftparchive --arch "$2" packages pool; '
                    'else dpkg-scanpackages --arch "$2" --multiversion pool '
                    '2>/dev/null; fi', "sh", self.root, arch])
                rel_dir = posixpath.join("dists", suite, component,
                                         f"binary-{arch}")
                local_dir = os.path.join(work, *rel_dir.split("/"))
                os.makedirs(local_dir, exist_ok=True)
                packages = os.path.join(local_dir, "Packages")
                with open(packages, "wb") as f:
                    f.write(listing)
                import gzip
                with open(packages, "rb") as src, \
                        gzip.GzipFile(packages + ".gz", "wb", mtime=0) as dst:
                    shutil.copyfileobj(src, dst)
                uploads += [(packages, rel_dir), (packages + ".gz", rel_dir)]

            release = repo._write_release(
                work, suite, component, profile["archs"], profile["origin"],
                profile["label"], profile["description"])
            repo._sign_release(release, profile["key"])
            keyring = repo.export_key(work, profile["key"],
                                      repo.keyring_filename(profile))
            dist_rel = posixpath.join("dists", suite)
            for local, rel_dir in uploads:
                self.upload(local, rel_dir)
            for name in ("Release", "Release.gpg", "InRelease"):
                self.upload(os.path.join(work, "dists", suite, name),
                            dist_rel)
            self.upload(keyring, "")
        finally:
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def list_identities():
    """Private keys in ~/.ssh that have a public half beside them."""
    ssh_dir = os.path.expanduser("~/.ssh")
    found = []
    if os.path.isdir(ssh_dir):
        for name in sorted(os.listdir(ssh_dir)):
            if name.endswith(".pub"):
                private = os.path.join(ssh_dir, name[:-4])
                if os.path.isfile(private):
                    found.append(private)
    return found


def generate_key(path: str, passphrase: str, comment: str = "petacore"):
    """Create an Ed25519 key protected by a passphrase.

    ssh-keygen reads the passphrase from its terminal, so it is given one —
    a pseudo-terminal — rather than the passphrase being put on its command
    line, where any user on the machine could read it from /proc.
    """
    import pty
    path = os.path.expanduser(path)
    if os.path.exists(path) or os.path.exists(path + ".pub"):
        raise RemoteError("a key with that name already exists")
    if len(passphrase) < 8:
        raise RemoteError("use a passphrase of at least 8 characters")
    _private_dir(os.path.dirname(path))

    pid, fd = pty.fork()
    if pid == 0:                                    # the child
        os.execvp("ssh-keygen", ["ssh-keygen", "-q", "-t", "ed25519",
                                 "-a", "100", "-f", path, "-C", comment])
    buffer, answered = b"", 0
    deadline = time.time() + 60
    try:
        while time.time() < deadline:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while answered < 2 and buffer.lower().count(b"passphrase") \
                    > answered:
                os.write(fd, passphrase.encode() + b"\n")
                answered += 1
    finally:
        os.close(fd)
        _pid, status = os.waitpid(pid, 0)
    if status != 0 or not os.path.exists(path + ".pub"):
        raise RemoteError("the key could not be created")
    with open(path + ".pub", encoding="utf-8") as f:
        return f.read().strip()
