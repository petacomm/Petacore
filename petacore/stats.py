"""Project statistics for the Overview page.

Walks the project once and reports what it is made of: how many files, how
many lines and characters of code, which languages (weighted by file size,
as requested) and which operating system it appears to target.
"""

import os

SKIP_DIRS = {".git", ".petacore", "__pycache__", "node_modules", ".venv",
             "venv", "dist", "build", ".idea", ".vscode", ".mypy_cache"}

# extension -> display name
LANGUAGES = {
    ".py": "Python", ".pyw": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".html": "HTML", ".htm": "HTML",
    ".css": "CSS", ".scss": "CSS", ".sass": "CSS",
    ".c": "C", ".h": "C",
    ".cpp": "C++", ".hpp": "C++", ".cc": "C++", ".hh": "C++", ".cxx": "C++",
    ".cs": "C#", ".java": "Java", ".rs": "Rust", ".go": "Go",
    ".php": "PHP", ".rb": "Ruby", ".swift": "Swift",
    ".kt": "Kotlin", ".kts": "Kotlin", ".lua": "Lua",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".sql": "SQL", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML",
    ".toml": "TOML", ".xml": "XML", ".md": "Markdown", ".markdown": "Markdown",
    ".vala": "Vala", ".dart": "Dart", ".r": "R", ".pl": "Perl",
    ".ex": "Elixir", ".exs": "Elixir", ".hs": "Haskell", ".scala": "Scala",
}

TEXT_EXT = set(LANGUAGES) | {".txt", ".cfg", ".ini", ".conf", ".desktop"}
MAX_READ = 2 * 1024 * 1024          # don't read enormous files line by line

# Signals that a project targets a particular platform.
LINUX_HINTS = (".desktop", "/usr/share", "/opt/", "xdg-open", "gi.repository",
               "PyQt", "PySide", "gtk", "dpkg", "apt-get", "systemd",
               "#!/bin/bash", "#!/usr/bin/env python3")
WINDOWS_HINTS = (".bat", ".ps1", ".exe", "win32", "winreg", "windll",
                 "C:\\\\", "System32", "pywin32", ".msi")
# A site or web application does not target an operating system at all, so
# reporting one is misleading. These are the languages that make a project a
# web project, and the files that confirm it.
WEB_LANGUAGES = {"HTML", "CSS", "JavaScript", "TypeScript"}
WEB_MARKERS = ("index.html", "package.json", "vite.config.js",
               "vite.config.ts", "next.config.js", "webpack.config.js",
               "tailwind.config.js", "svelte.config.js", "nuxt.config.js",
               "angular.json", "gatsby-config.js", "astro.config.mjs")
WEB_SHARE = 60.0          # per cent of the code that must be web languages

MAC_HINTS = (".plist", "NSApplication", "objc", "/Applications/",
             "darwin", ".dmg")


def _walk(project_path):
    for base, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            yield os.path.join(base, name), name


def analyse(project_path: str) -> dict:
    """Everything the Overview page and the share card need."""
    files = 0
    total_bytes = 0
    code_files = 0
    lines = 0
    characters = 0
    by_language = {}          # language -> bytes  (size-weighted, as asked)
    hints = {"linux": 0, "windows": 0, "mac": 0}
    seen_files = []           # names, used to recognise a web project

    for path, name in _walk(project_path):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        files += 1
        total_bytes += size
        if len(seen_files) < 4000:
            seen_files.append(name)

        ext = os.path.splitext(name)[1].lower()
        language = LANGUAGES.get(ext)
        if language:
            by_language[language] = by_language.get(language, 0) + size
            code_files += 1

        # platform hints from the file name itself
        lowered = name.lower()
        if lowered.endswith((".bat", ".ps1", ".exe", ".msi")):
            hints["windows"] += 3
        if lowered.endswith(".desktop"):
            hints["linux"] += 3
        if lowered.endswith(".plist") or lowered.endswith(".dmg"):
            hints["mac"] += 3

        if ext in TEXT_EXT and size <= MAX_READ:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue
            if language:
                lines += content.count("\n") + (1 if content else 0)
                characters += len(content)
            for hint in LINUX_HINTS:
                if hint in content:
                    hints["linux"] += 1
            for hint in WINDOWS_HINTS:
                if hint in content:
                    hints["windows"] += 1
            for hint in MAC_HINTS:
                if hint in content:
                    hints["mac"] += 1

    total_lang_bytes = sum(by_language.values()) or 1
    breakdown = sorted(
        ({"language": lang,
          "bytes": size,
          "percent": round(size * 100.0 / total_lang_bytes, 1)}
         for lang, size in by_language.items()),
        key=lambda item: item["bytes"], reverse=True)

    return {
        "files": files,
        "code_files": code_files,
        "bytes": total_bytes,
        "lines": lines,
        "characters": characters,
        "languages": breakdown,
        "platform": detect_platform(hints, breakdown, seen_files),
        "hints": hints,
    }


def detect_platform(hints: dict, languages=None, files=None) -> str:
    """A readable target-platform label such as 'Linux' or 'Linux / Windows'.

    Web projects are reported as such rather than being attributed to an
    operating system. For native projects a single passing mention is not
    enough: a page that merely links to `setup.exe` is not a Windows
    application, so a platform needs more than one piece of evidence before
    it is named.
    """
    if is_web_project(languages, files):
        return "Web"

    ranked = [(name, score) for name, score in hints.items() if score >= 2]
    if not ranked:
        return "Cross-platform"
    ranked.sort(key=lambda item: item[1], reverse=True)
    best = ranked[0][1]
    names = {"linux": "Linux", "windows": "Windows", "mac": "macOS"}
    # anything within half of the leader counts as a co-target
    chosen = [names[name] for name, score in ranked if score >= best * 0.5]
    return " / ".join(chosen[:3])


def is_web_project(languages=None, files=None) -> bool:
    """True when the project is a site or web application."""
    names = {os.path.basename(f).lower() for f in (files or [])}
    has_marker = any(marker in names for marker in WEB_MARKERS)

    share = 0.0
    for item in (languages or []):
        if item.get("language") in WEB_LANGUAGES:
            share += item.get("percent", 0.0)

    # Either the code is mostly web languages, or it is substantially web
    # code sitting next to an unmistakable marker such as index.html.
    return share >= WEB_SHARE or (share >= 25.0 and has_marker)


def human_count(value: int) -> str:
    """12345 -> '12.3K', 1234567 -> '1.2M' — for the share card."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)
