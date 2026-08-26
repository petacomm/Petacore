"""Noticing when something in the Sandbox wanted the internet.

The Sandbox runs with its own empty network namespace, so a program inside it
simply fails to connect. bubblewrap gives no callback for the attempt — the
kernel refuses it and the program prints an error — so the attempt is
recognised from that error instead.

This is deliberately a reading of the symptom rather than a packet filter.
It catches the cases that matter in practice (a package manager, a fetch, a
clone) without pretending to be a firewall: a program that fails silently
will not be noticed, and the module says so rather than implying otherwise.
"""

import re

# Phrases printed by the usual tools when there is no route out. Kept
# specific enough that ordinary output does not trip them.
SIGNATURES = (
    r"network is unreachable",
    r"temporary failure in name resolution",
    r"could not resolve host",
    r"name or service not known",
    r"failed to connect to .* port",
    r"couldn't connect to server",
    r"connection refused.*(http|https|:80|:443)",
    r"could not connect to .*\.(com|org|net|io|dev)",
    r"unable to access '.*': could not resolve",
    r"failed to establish a new connection",
    r"nodename nor servname provided",
    r"no address associated with hostname",
    r"proxy connect aborted",
    r"err:1 http",                       # apt
    r"could not fetch url",              # pip
    r"getaddrinfo failed",
    r"dns lookup failed",
    r"network unreachable",
    r"unable to resolve host",           # wget
    # wget prints the host name and a dotted domain, which keeps this
    # away from compiler messages such as "resolving imports failed".
    # Restricted to things that look like real host names, so that file
    # names ("resolving config.json failed") do not match.
    r"resolving [\w.-]+\.(com|org|net|io|dev|edu|gov|co|uk|de|tr|"
    r"ubuntu\.com|debian\.org)\b[^\n]{0,60}failed",
    r"connect to .* failed.*unreachable",
    r"ssl.*handshake.*(timed out|failed to connect)",
)

_COMPILED = [re.compile(pattern, re.I) for pattern in SIGNATURES]

# How much recent output to keep when scanning, so a message split across
# reads is still matched.
WINDOW = 4000


def looks_like_blocked_network(text: str) -> bool:
    """True when this output suggests something tried to reach the network."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _COMPILED)


class OutputWatcher:
    """Feeds terminal output through the detector, firing once per session.

    The prompt should interrupt at most once: a failed `apt update` prints
    the same complaint for every mirror, and asking six times would train
    the user to dismiss it without reading.
    """

    def __init__(self, on_detected):
        self._on_detected = on_detected
        self._buffer = ""
        self.fired = False

    def feed(self, chunk: str):
        if self.fired or not chunk:
            return False
        self._buffer = (self._buffer + chunk)[-WINDOW:]
        if looks_like_blocked_network(self._buffer):
            self.fired = True
            self._buffer = ""
            try:
                self._on_detected()
            except Exception:
                pass
            return True
        return False

    def reset(self):
        self._buffer = ""
        self.fired = False
