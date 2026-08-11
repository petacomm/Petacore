# Petacore

A native GNOME (GTK4 + libadwaita) codebase management platform for solo developers and indie studios building Linux software with AI tools such as Claude.

Petacore answers the questions that pile up in AI-assisted workflows: *Where is the latest code? Which folder is actually running? Is it synced with GitHub? How do I roll back?* — all from one native desktop app.

## Features

**Native GNOME design** — Adwaita headerbar, navigation sidebar, boxed lists, toasts, and status pages, following the same design language as GNOME Files, Settings, Console, and Builder. Light, dark, and follow-system appearance. No Electron, no web views.

**Five languages, switchable at runtime** — English, Türkçe, Deutsch, Français, 中文 (简体). A language chooser appears on first launch, and the language can be changed at any time from Settings without restarting; the whole UI rebuilds in place.

**New Project wizard** — asks for a project name and an optional GitHub repository URL. With a URL it clones the repository; without one it initializes a fresh local git repository. The project is then tracked by Petacore.

**GitHub integration** — log in with a GitHub Personal Access Token (validated against the GitHub API; used for HTTPS push/pull via a secure askpass helper, never written into remote URLs). The Sync button asks *"Do you want to synchronize this project?"*, then stages everything, commits (even when nothing changed, via `--allow-empty`), pulls with rebase, and pushes.

**Automatic codebase detection** — the Overview page shows the authoritative git root (the active codebase), branch, last commit, tracked file count, sync status (up to date / uncommitted changes / ahead-behind counts), and — by scanning `/proc` — every process currently running *from* the project folder, so you always know which code is live.

**Claude workflow** — paste code from Claude into the Apply Code page. If the first line is a marker like `# file: src/app.py`, the target file is detected automatically; otherwise type the path. Petacore snapshots the project, then writes the code straight into the project tree, immediately affecting the local project.

**Autosave & snapshots** — periodic automatic snapshots (interval configurable), manual snapshots, safety snapshots before every restore, and pre-apply snapshots before every code application. Any snapshot can be restored with one click; old autosaves are pruned automatically.

**Integrated terminal** — a real VTE terminal spawned inside the active project directory, so commands run in the project environment without leaving Petacore.

**Sandbox — temporary install simulation** — try an app without installing anything on the system. A new **Sandbox** page extracts a `.deb` (or copies the current project) into a private temporary folder and opens a session with its own terminal, where both GUI and terminal applications can be launched. Sessions are **multi-tab**: run several simulations side by side in native GNOME tabs. Pressing **End Simulation** stops every process started inside the sandbox and deletes all temporary files automatically — nothing is left on disk, no manual cleanup, and all sandboxes are wiped on exit as well.

**Package builder** — press **Export as .deb**, confirm, choose a folder, and Petacore stages the project under `/opt/<name>`, writes the Debian control file, and builds the package with `dpkg-deb`. No terminal commands needed.

## Requirements

Ubuntu 24.04+ / Debian 12+ / Fedora 39+ (or any distro with GTK 4.10+ and libadwaita 1.4+):

```
python3  python3-gi  gir1.2-gtk-4.0  gir1.2-adw-1  gir1.2-vte-3.91  git  dpkg
```

## Install

```bash
./install.sh
```

Then launch **Petacore** from the app grid, or run `petacore`.

To run directly from the source folder instead:

```bash
python3 -m petacore.main
```

## Project layout

```
petacore/
├── petacore/            # application package
│   ├── main.py          # Adw.Application entry point
│   ├── window.py        # headerbar + sidebar main window, sync flow, autosave timer
│   ├── pages.py         # Overview, Apply Code, Snapshots, Terminal, Package pages
│   ├── dialogs.py       # first-run language, New Project wizard, GitHub login, Settings
│   ├── i18n.py          # 5-language runtime string table
│   ├── config.py        # ~/.config/petacore/config.json
│   ├── gitops.py        # git wrapper (init/clone/sync/status, token askpass)
│   ├── github.py        # GitHub API token validation
│   ├── detect.py        # /proc scanning + git-root codebase detection
│   ├── snapshots.py     # tar.gz snapshot store + restore + pruning
│   ├── debbuild.py      # dpkg-deb package builder
│   ├── sandbox.py       # temporary-install simulation (multi-tab, auto-cleanup)
│   └── util.py          # thread helper, formatting
├── data/io.petacore.Petacore.desktop
├── install.sh
└── README.md
```

## Notes

- The GitHub token is stored in `~/.config/petacore/config.json` with `0600` permissions. For extra hardening, consider moving it to the system keyring (`python3-secretstorage`).
- Snapshots live in `~/.local/share/petacore/snapshots/` and exclude `.git`, `.petacore`, `node_modules`, `__pycache__`, and `.venv`.
- Sync commits as your configured git identity when one exists, falling back to a Petacore identity otherwise.
