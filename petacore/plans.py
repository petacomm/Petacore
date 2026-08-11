"""Next Updates plans: storage model shared by both builds, and the
Google Drive synchronisation for them.

On disk each plan is stored as:

    {
      "title":    "<free text>",      # the heading
      "detail":   "<free text>",      # the details
      "priority": 1 | 2 | 3,          # 1 = high, 2 = normal, 3 = someday
      "done":     true | false,
      "created":  <epoch seconds>     # stable id
    }

Older files used "note" and string priorities; they are migrated on load so
nothing is lost.
"""

import json
import os
import time

FILENAME = "updates.json"
REMOTE_NAME = "petacore-updates.json"

# UI code <-> stored number
PRIORITY_TO_CODE = {"high": 1, "normal": 2, "low": 3}
CODE_TO_PRIORITY = {1: "high", 2: "normal", 3: "low"}


def store_path(project_path: str) -> str:
    return os.path.join(project_path, ".petacore", FILENAME)


def _normalise(entry: dict) -> dict:
    """Accept both the new and the legacy shape."""
    priority = entry.get("priority", 2)
    if isinstance(priority, str):
        priority = PRIORITY_TO_CODE.get(priority, 2)
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 2
    if priority not in (1, 2, 3):
        priority = 2
    return {
        "title": (entry.get("title") or "").strip(),
        # "note" is the legacy key for the details field
        "detail": (entry.get("detail") or entry.get("note") or "").strip(),
        "priority": priority,
        "done": bool(entry.get("done", False)),
        "created": entry.get("created") or time.time(),
    }


def load(project_path: str):
    try:
        with open(store_path(project_path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):            # a synced payload
        data = data.get("plans", [])
    return [_normalise(e) for e in data if isinstance(e, dict)]


def save(project_path: str, plans):
    path = store_path(project_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_normalise(p) for p in plans], f,
                  ensure_ascii=False, indent=1)


def sort_key(plan: dict):
    """Done last, then by priority number (1 first)."""
    return (plan.get("done", False), plan.get("priority", 2))


def merge(local, remote):
    """Combine two lists, newest edit wins per plan (matched on `created`)."""
    by_id = {}
    for plan in list(local) + list(remote):
        plan = _normalise(plan)
        key = plan["created"]
        existing = by_id.get(key)
        if existing is None or plan.get("updated", 0) >= existing.get("updated", 0):
            by_id[key] = plan
    return sorted(by_id.values(), key=sort_key)


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #
def push_to_drive(project_path: str, project_name: str):
    """Upload this project's plans to Drive."""
    from . import gdrive
    payload = {
        "version": 1,
        "project": project_name,
        "updated": time.time(),
        "plans": load(project_path),
    }
    gdrive.put_json(project_name, REMOTE_NAME, payload)
    return len(payload["plans"])


def pull_from_drive(project_path: str, project_name: str):
    """Fetch plans from Drive and merge them into the local list."""
    from . import gdrive
    payload = gdrive.get_json(project_name, REMOTE_NAME)
    if not payload:
        return 0
    remote = payload.get("plans", []) if isinstance(payload, dict) else []
    merged = merge(load(project_path), remote)
    save(project_path, merged)
    return len(merged)


def sync_with_drive(project_path: str, project_name: str):
    """Two-way: merge what is on Drive with what is local, then upload."""
    pull_from_drive(project_path, project_name)
    return push_to_drive(project_path, project_name)
