#!/usr/bin/env bash
# Petacore uninstaller.
#
# Removing the program and destroying your backups are two different
# decisions, so they are asked separately. Nothing is deleted until you say
# so, and your project folders are never touched by this script.
set -euo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

if [ "$(id -u)" -eq 0 ]; then
  echo "Do not run this with sudo or as root — Petacore lives in your own"
  echo "home directory. Run it again as yourself:   ./uninstall.sh"
  exit 1
fi

APP_DIR="$HOME/.local/share/petacore-app"
BIN_FILE="$HOME/.local/bin/petacore"
DESKTOP_FILE="$HOME/.local/share/applications/io.petacore.Petacore.desktop"
ICON_BASE="$HOME/.local/share/icons/hicolor"
CONFIG_DIR="$HOME/.config/petacore"
DATA_DIR="$HOME/.local/share/petacore"
SNAPSHOT_DIR="$DATA_DIR/snapshots"

ask() {
  # ask "question" -> 0 for yes, 1 for anything else. Defaults to no.
  local reply=""
  read -r -p "$1 [y/N]: " reply || true
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

echo "Petacore uninstaller"
echo

# -- 1. the program itself ---------------------------------------------------
echo "This will remove:"
echo "  $APP_DIR"
echo "  $BIN_FILE"
echo "  $DESKTOP_FILE"
echo "  application icons"
echo
if ! ask "Remove the Petacore application?"; then
  echo "Nothing was changed."
  exit 0
fi

rm -rf "$APP_DIR"
rm -f "$BIN_FILE" "$DESKTOP_FILE"
for size in 512x512 256x256 128x128 64x64 48x48 32x32 24x24 16x16; do
  rm -f "$ICON_BASE/$size/apps/io.petacore.Petacore.png"
done
rm -f "$ICON_BASE/scalable/apps/io.petacore.Petacore.svg"
gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Application removed."
echo

# -- 2. snapshots, asked separately -----------------------------------------
if [ -d "$SNAPSHOT_DIR" ]; then
  count="$(find "$SNAPSHOT_DIR" -name '*.tar.gz' 2>/dev/null | wc -l | tr -d ' ')"
  size="$(du -sh "$SNAPSHOT_DIR" 2>/dev/null | cut -f1)"
  echo "Your snapshots are still on disk:"
  echo "  $SNAPSHOT_DIR"
  echo "  $count snapshot(s), $size"
  echo
  echo "These are the only copies of earlier versions of your projects."
  echo "They are not backed up anywhere else and cannot be recovered once"
  echo "deleted. Keeping them costs nothing but disk space."
  echo
  if ask "Delete the snapshots as well?"; then
    rm -rf "$DATA_DIR"
    echo "Snapshots deleted."
  else
    echo "Snapshots kept at $SNAPSHOT_DIR"
  fi
  echo
fi

# -- 3. settings, asked separately ------------------------------------------
if [ -d "$CONFIG_DIR" ]; then
  echo "Settings and your project list are at:"
  echo "  $CONFIG_DIR"
  echo
  if ask "Delete settings too?"; then
    rm -rf "$CONFIG_DIR"
    echo "Settings deleted."
    echo
    echo "Note: if you signed in to GitHub, the credential was stored in"
    echo "your desktop keyring rather than in this folder. Remove it with"
    echo "your keyring application (Seahorse, KWalletManager) if you want"
    echo "it gone as well."
  else
    echo "Settings kept at $CONFIG_DIR"
  fi
  echo
fi

echo "Done. Your project folders were not touched."
