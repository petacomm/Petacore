"""The helper OpenSSH runs when it needs to ask the person something.

OpenSSH never reads a passphrase or a verification code from anywhere but
the person: it either asks on a terminal, or runs the program named in
SSH_ASKPASS and reads the answer from that program's output. Petacore has no
terminal, so this is that program. It does nothing itself — it passes the
question to the Petacore window over a private socket, and passes the
answer back to ssh.

Nothing here is written anywhere. The answer travels from the window to
this process to ssh, in memory, and is gone when ssh has read it.

The socket lives in a directory only this user can enter, and the request
carries a random token known only to the Petacore process that started ssh;
the window side also checks the connecting process belongs to this user.
"""

import json
import os
import socket
import sys


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    # "confirm" asks yes/no; "none" is a notice (touch your security key)
    # that ssh shows and then takes away itself; anything else wants text.
    style = os.environ.get("SSH_ASKPASS_PROMPT", "")
    path = os.environ.get("PETACORE_ASKPASS_SOCK", "")
    token = os.environ.get("PETACORE_ASKPASS_TOKEN", "")
    if not path or not token:
        sys.exit(1)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(300)
            sock.connect(path)
            sock.sendall(json.dumps({"token": token, "prompt": prompt,
                                     "style": style}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n") and len(data) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        sys.exit(1)

    if style == "none":
        sys.exit(0)
    try:
        reply = json.loads(data.decode("utf-8", "replace") or "{}")
    except ValueError:
        sys.exit(1)
    answer = reply.get("answer")
    if answer is None:
        sys.exit(1)                     # cancelled: ssh gives up cleanly
    if style == "confirm":
        sys.exit(0 if answer == "yes" else 1)
    sys.stdout.write(answer + "\n")
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
