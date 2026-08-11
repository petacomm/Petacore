"""Snapshot and autosave system for Petacore.

Snapshots are tar.gz archives of the project tree (excluding .git and
.petacore) stored under ~/.local/share/petacore/snapshots/<project-slug>/,
each with a JSON sidecar describing when and why it was taken.
"""

import json
import os
import re
import tarfile
import time

from .config import DATA_DIR

EXCLUDE = {".git", ".petacore", "__pycache__", "node_modules", ".venv"}
MAX_AUTOSAVES = 40


def _slug(path: str) -> str:
    base = os.path.basename(os.path.realpath(path)) or "project"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-").lower() or "project"


class SnapshotManager:
    def __init__(self, project_path: str):
        self.project = os.path.realpath(project_path)
        self.dir = os.path.join(DATA_DIR, "snapshots", _slug(project_path))
        os.makedirs(self.dir, exist_ok=True)

    # -- creation -------------------------------------------------------------
    def create(self, kind: str = "manual", label: str = "") -> str:
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = f"{ts}-{kind}"
        archive = os.path.join(self.dir, name + ".tar.gz")

        def _filter(tarinfo):
            parts = tarinfo.name.split("/")
            if any(p in EXCLUDE for p in parts):
                return None
            return tarinfo

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(self.project, arcname=".", filter=_filter)

        meta = {
            "time": time.time(),
            "kind": kind,               # manual | auto | safety | apply
            "label": label,
            "project": self.project,
        }
        with open(os.path.join(self.dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)

        if kind == "auto":
            self._prune()
        return archive

    def _prune(self):
        autos = [s for s in self.list() if s["kind"] == "auto"]
        for snap in autos[MAX_AUTOSAVES:]:
            self.delete(snap["name"])

    # -- listing --------------------------------------------------------------
    def list(self):
        """Newest first: [{name, time, kind, label, size}]"""
        snaps = []
        try:
            files = os.listdir(self.dir)
        except OSError:
            return snaps
        for fn in files:
            if not fn.endswith(".tar.gz"):
                continue
            name = fn[:-7]
            meta_path = os.path.join(self.dir, name + ".json")
            meta = {}
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                pass
            try:
                size = os.path.getsize(os.path.join(self.dir, fn))
            except OSError:
                size = 0
            snaps.append({
                "name": name,
                "time": meta.get("time", 0),
                "kind": meta.get("kind", "manual"),
                "label": meta.get("label", ""),
                "size": size,
            })
        snaps.sort(key=lambda s: s["time"], reverse=True)
        return snaps

    # -- restore / delete -------------------------------------------------------
    def restore(self, name: str):
        """Restore a snapshot over the project after taking a safety snapshot."""
        archive = os.path.join(self.dir, name + ".tar.gz")
        if not os.path.isfile(archive):
            raise FileNotFoundError(archive)

        self.create(kind="safety")

        # Remove current tree (keeping .git, .petacore and other excluded dirs)
        for entry in os.listdir(self.project):
            if entry in EXCLUDE:
                continue
            full = os.path.join(self.project, entry)
            if os.path.isdir(full) and not os.path.islink(full):
                import shutil
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass

        with tarfile.open(archive, "r:gz") as tar:
            # Python 3.12: safe extraction filter
            try:
                tar.extractall(self.project, filter="data")
            except TypeError:
                tar.extractall(self.project)

    def delete(self, name: str):
        for ext in (".tar.gz", ".json"):
            try:
                os.remove(os.path.join(self.dir, name + ext))
            except OSError:
                pass
