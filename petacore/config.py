"""Configuration persistence for Petacore (~/.config/petacore/config.json)."""

import json
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "petacore"
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "petacore"
)

DEFAULTS = {
    "first_run": True,
    "language": "en",
    "theme": "system",          # system | light | dark
    "autosave_enabled": True,
    "autosave_minutes": 10,
    "github_token": "",
    "github_user": "",
    "github_client_id": "",
    "sign_packages": True,
    "gpg_key": "",
    "focus_mode": True,
    "gdrive_email": "",
    "ui": "",
    "esc_confirm": True,
    "projects": [],             # [{"name": ..., "path": ..., "repo_url": ...}]
    "active_project": "",       # path of the active project
}


class Config:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            self._data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o600)  # token lives here; keep it private
        except OSError:
            pass

    def get(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value
        self.save()

    # --- Projects -----------------------------------------------------------
    def add_project(self, name: str, path: str, repo_url: str = ""):
        projects = [p for p in self.get("projects") if p.get("path") != path]
        projects.append({"name": name, "path": path, "repo_url": repo_url})
        self.set("projects", projects)
        self.set("active_project", path)

    def remove_project(self, path: str):
        self.set("projects", [p for p in self.get("projects") if p.get("path") != path])
        if self.get("active_project") == path:
            projects = self.get("projects")
            self.set("active_project", projects[0]["path"] if projects else "")

    def set_project_field(self, path: str, key: str, value):
        projects = self.get("projects")
        for p in projects:
            if p.get("path") == path:
                p[key] = value
        self.set("projects", projects)

    def update_project(self, old_path: str, **fields):
        """Update a project entry; supports changing its path."""
        projects = self.get("projects")
        for p in projects:
            if p.get("path") == old_path:
                p.update({k: v for k, v in fields.items() if v is not None})
        self.set("projects", projects)
        new_path = fields.get("path")
        if new_path and self.get("active_project") == old_path:
            self.set("active_project", new_path)

    def active_project(self):
        path = self.get("active_project")
        for p in self.get("projects"):
            if p.get("path") == path:
                return p
        return None


config = Config()
