"""Documents attached to Next Updates plans.

Every plan can carry its own files (.odt, .pdf, notes …). They live inside the
project so they travel with it:

    <project>/.petacore/plan-docs/<plan-id>/<filename>

Where they are served from depends on the machine:

  * more than 8 GB of RAM  → the files are read into memory once, shortly
    after the app starts, and opened from a RAM-backed folder (/dev/shm).
    Nothing has to touch the disk again for the rest of the session.
  * 8 GB or less           → nothing is cached; files are read from the SSD
    on demand, so a smaller machine keeps its memory for the editor and the
    build tools.

The threshold is checked once at import and can be inspected in the UI.
"""

import os
import shutil
import threading

RAM_THRESHOLD_GB = 8
DOCS_DIRNAME = "plan-docs"
# Anything bigger than this is always streamed from disk, whatever the RAM.
MAX_CACHE_BYTES = 64 * 1024 * 1024          # per file
MAX_TOTAL_CACHE_BYTES = 512 * 1024 * 1024   # all files together

_cache = {}            # abs path -> bytes
_cache_bytes = 0
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Machine capability
# --------------------------------------------------------------------------- #
def total_ram_gb() -> float:
    """Physical RAM in GB (0.0 when it can't be determined)."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def prefer_ram() -> bool:
    """True when this machine has enough memory to keep documents resident."""
    return total_ram_gb() > RAM_THRESHOLD_GB


def _ram_dir() -> str:
    """A RAM-backed directory, falling back to /tmp if /dev/shm is absent."""
    base = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"
    path = os.path.join(base, "petacore-docs")
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def docs_root(project_path: str) -> str:
    return os.path.join(project_path, ".petacore", DOCS_DIRNAME)


def plan_dir(project_path: str, plan_id) -> str:
    return os.path.join(docs_root(project_path), str(plan_id).replace("/", "_"))


def list_docs(project_path: str, plan_id):
    """[{name, path, size}] for one plan, newest names sorted."""
    folder = plan_dir(project_path, plan_id)
    out = []
    try:
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                out.append({"name": name, "path": full,
                            "size": os.path.getsize(full)})
    except OSError:
        pass
    return out


def attach(project_path: str, plan_id, source_path: str) -> dict:
    """Copy a document into the plan's folder and cache it when appropriate."""
    folder = plan_dir(project_path, plan_id)
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(source_path)
    dest = os.path.join(folder, name)
    if os.path.exists(dest):                      # keep both, don't overwrite
        stem, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(os.path.join(folder, f"{stem} ({i}){ext}")):
            i += 1
        name = f"{stem} ({i}){ext}"
        dest = os.path.join(folder, name)
    shutil.copy2(source_path, dest)
    if prefer_ram():
        _cache_file(dest)
    return {"name": name, "path": dest, "size": os.path.getsize(dest)}


def detach(project_path: str, plan_id, name: str):
    path = os.path.join(plan_dir(project_path, plan_id), name)
    with _lock:
        global _cache_bytes
        if path in _cache:
            _cache_bytes -= len(_cache[path])
            del _cache[path]
    try:
        os.remove(path)
    except OSError:
        pass


def remove_all(project_path: str, plan_id):
    """Called when a plan is deleted."""
    folder = plan_dir(project_path, plan_id)
    with _lock:
        global _cache_bytes
        for path in [p for p in _cache if p.startswith(folder + os.sep)]:
            _cache_bytes -= len(_cache[path])
            del _cache[path]
    shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- #
# RAM cache
# --------------------------------------------------------------------------- #
def _cache_file(path: str) -> bool:
    global _cache_bytes
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size > MAX_CACHE_BYTES:
        return False
    with _lock:
        if path in _cache:
            return True
        if _cache_bytes + size > MAX_TOTAL_CACHE_BYTES:
            return False
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return False
    with _lock:
        _cache[path] = data
        _cache_bytes += len(data)
    return True


def preload(project_path: str):
    """Read every attached document into memory. Call this shortly after
    startup, off the UI thread. Does nothing on smaller machines."""
    if not prefer_ram():
        return 0
    loaded = 0
    root = docs_root(project_path)
    for base, _dirs, files in os.walk(root):
        for name in files:
            if _cache_file(os.path.join(base, name)):
                loaded += 1
    return loaded


def is_cached(path: str) -> bool:
    with _lock:
        return path in _cache


def cache_stats():
    with _lock:
        return {"files": len(_cache), "bytes": _cache_bytes,
                "ram_gb": round(total_ram_gb(), 1),
                "using_ram": prefer_ram()}


def clear_cache():
    global _cache_bytes
    with _lock:
        _cache.clear()
        _cache_bytes = 0
    shutil.rmtree(_ram_dir(), ignore_errors=True)


# --------------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------------- #
def resolve_for_open(path: str) -> str:
    """The path to hand to the document viewer.

    Cached files are materialised in a RAM-backed folder so opening them
    never hits the disk; otherwise the original path on the SSD is used.
    """
    with _lock:
        data = _cache.get(path)
    if data is None:
        return path
    target = os.path.join(_ram_dir(), os.path.basename(path))
    try:
        if not os.path.exists(target) or os.path.getsize(target) != len(data):
            with open(target, "wb") as f:
                f.write(data)
        return target
    except OSError:
        return path
