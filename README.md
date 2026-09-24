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

**APT repository** — a **Repository** page manages a real, signed archive without a terminal. Drop `.deb` files onto the page (or let the Package page put each build there), then press **Rebuild & Sign**: Petacore lays out `pool/` and `dists/`, writes the `Packages` index, signs `Release` into `InRelease` and `Release.gpg`, and exports the public key users have to trust. **Publish** copies the tree to a folder or sends it to a server over SSH with rsync — after showing exactly what will change, and never while the index is out of step with the packages. **Check Server** reads the live index back from the published address, so every package is labelled with where it actually stands: in the pool, in the index, and on the server. Renaming, removing (reversible, with Ctrl+Z), pruning superseded versions and copying the two commands your users run are all in the same window.

**Live Server** — for a project Petacore recognises as a web app (a static site, or a bundler project like Vite/Next/Astro — never a Python/Ruby/PHP backend just because it renders some HTML), the **Sandbox** page is replaced by **Live Server**, and **Package**/**Keys** drop out of the sidebar entirely, since a web project is never built into a `.deb` or signed. Live Server serves the project's files exactly as they are — no build step — and any page open in the browser reloads itself within a second or two of a file changing. The site is drawn inside the window itself, not a terminal log — **F11** moves the site into a window of its own and fills the screen with it — no sidebar, no header bar, just the page at the full resolution of your display — and **Esc** brings it back into Petacore with its state intact. It binds to `127.0.0.1` only by default; a "Share on the local network" switch opens it up for testing from another device, such as a phone, on the same network. Without WebKitGTK installed the server still runs and offers the address for your own browser. Everything else — Windows, Linux, macOS, or anything Petacore doesn't recognise — keeps the full page set unchanged.

**Virus scan** — a **Virus Scan** page checks the whole project with VirusTotal in one step, no file picking. The project is packed into a single archive, which VirusTotal unpacks and scans file by file — one request instead of hundreds, which is what makes it workable on a free key. The archive is built byte-for-byte the same whenever the project is unchanged, so Petacore first asks about its hash and uploads nothing if VirusTotal already knows it. What packaging keeps back is kept back here too: credential files such as `.env` and private keys, `.git`, `node_modules` and the `versions` folder are never sent, and the page says how many were held back. Nothing is uploaded until you have agreed once for that project, in a prompt that says plainly that VirusTotal shares what it receives with the security companies it works with. A result is shown as how many engines flagged it and which ones — a project VirusTotal has never seen is *unknown*, never *clean*. The API key is yours (free, from virustotal.com) and lives in the desktop keyring, never in a settings file.

**Versions** — a **Versions** page keeps the project's release history: every `.deb` and `.rpm` it has shipped, newest first — 1.0, 1.6 Beta 1, 1.6 Beta 2, 1.6 RC 1, 1.6. Releases are made by hand from the form at the bottom of the page (number, release type, formats, package details, signing key and release notes), never automatically. Pre-releases get package versions such as `1.6~beta2`, which dpkg and rpm both sort before `1.6`, so a user on the beta is upgraded to the final release by `apt upgrade`. Older releases built elsewhere can be dropped onto the page to bring them into the history. Everything lives in the project's `versions/` folder, which is kept out of every package (so a release never contains the releases before it), out of snapshots (and never deleted when one is restored), out of the file view and statistics, and out of git through `.git/info/exclude` — no tracked file is changed. A project's own folder that happens to be called `versions` is recognised and left alone.

**Repository on a server** — the **Repository** page has a *Server* view that manages the repository folder on your server directly, over SSH: browse it, upload `.deb` packages (placed in the pool automatically) and files such as `install.sh`, create folders, rename, download, and move things to a trash on the server that can be undone. **Rebuild & Sign** has the server list its own packages, writes and signs the index on this machine, and uploads the result — the signing key never leaves your computer.

The connection is made by the system's own OpenSSH, with a hardened option set instead of whatever `~/.ssh/config` says: the server's host key must be confirmed once by fingerprint and is then checked on every connection (a changed key stops everything, before anything is asked of you); only modern algorithms are offered, including post-quantum hybrid key exchange where the installed OpenSSH has it; key login only, no passwords sent, no agent, X11 or port forwarding. Two-factor login is supported — authenticator codes (TOTP) and FIDO2 security keys: a passphrase or a code is asked for in a dialog, handed to ssh in memory and never stored, and later operations reuse the one authenticated connection, which closes itself after ten idle minutes. Every operation is confined to the chosen folder: `..` is refused, and symbolic links on the server that lead elsewhere are shown as links and never followed. Uploads travel under a temporary name and are moved into place only when complete, so a web server never serves half a package. The page also shows, step by step, how to turn two-factor login on at the server without locking yourself out.

## Requirements

Ubuntu 24.04+ / Debian 12+ / Fedora 39+ (or any distro with GTK 4.10+ and libadwaita 1.4+):

```
python3  python3-gi  gir1.2-gtk-4.0  gir1.2-adw-1  gir1.2-vte-3.91  git  dpkg

# optional — each one enables a single feature and nothing else:
gnupg  apt-utils (or dpkg-dev)  rsync  bubblewrap  rclone  librsvg2-bin  gh
libsecret-tools   # secret-tool: how credentials reach GNOME Keyring / KWallet
openssh-client    # ssh, scp: the Server view of the Repository page
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
