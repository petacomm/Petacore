#!/usr/bin/env bash
# Petacore installer — picks the GNOME or KDE Plasma build for this desktop.
set -e
cd "$(dirname "$0")"

# --------------------------------------------------------------------------
# 1. Which desktop are we on?
# --------------------------------------------------------------------------
RAW="$(echo "${XDG_CURRENT_DESKTOP}:${XDG_SESSION_DESKTOP}:${DESKTOP_SESSION}" | tr '[:upper:]' '[:lower:]')"
VARIANT=""
case "$RAW" in
  *kde*|*plasma*) VARIANT="kde" ;;
  *gnome*|*unity*|*ubuntu*) VARIANT="gnome" ;;
esac

if [ -n "$VARIANT" ]; then
  echo "==> Detected desktop: $VARIANT"
else
  echo "==> Could not detect GNOME or KDE Plasma on this system."
  echo "    Which version of Petacore would you like to install?"
  echo "      1) GNOME  (GTK4 / libadwaita — looks like GNOME Files & Settings)"
  echo "      2) KDE Plasma  (Qt / Breeze — looks like Dolphin & System Settings)"
  while [ -z "$VARIANT" ]; do
    read -r -p "    Choice [1/2]: " ans
    case "$ans" in
      1) VARIANT="gnome" ;;
      2) VARIANT="kde" ;;
      *) echo "    Please type 1 or 2." ;;
    esac
  done
fi

# --------------------------------------------------------------------------
# 2. Dependencies (shared + toolkit specific)
# --------------------------------------------------------------------------
echo "==> Installing dependencies (needs sudo)…"
sudo apt update || echo "   (apt update reported errors — continuing anyway)"

COMMON="python3 python3-gi git dpkg rpm gnupg gh rclone bubblewrap fonts-ubuntu"
GNOME_PKGS="gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91 gir1.2-gtksource-5"
KDE_PKGS="python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore fonts-noto"

if [ "$VARIANT" = "kde" ]; then
  PKGS="$COMMON $KDE_PKGS"
else
  PKGS="$COMMON $GNOME_PKGS"
fi

if ! sudo apt install -y $PKGS; then
  echo "   (bulk install failed — trying packages one by one)"
  MISSING=""
  for p in $PKGS; do
    sudo apt install -y "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
  done
  if [ -n "$MISSING" ]; then
    echo "   Could not install:$MISSING"
  fi
fi

# PySide6 may not be packaged on every release — fall back to pip for KDE.
if [ "$VARIANT" = "kde" ]; then
  if ! python3 -c "import PySide6" >/dev/null 2>&1; then
    echo "==> Installing PySide6 via pip…"
    pip3 install --user PySide6-Essentials 2>/dev/null \
      || pip3 install --user --break-system-packages PySide6-Essentials \
      || echo "   Could not install PySide6 — install it manually for the KDE build."
  fi
fi

# --------------------------------------------------------------------------
# 3. Install the application
# --------------------------------------------------------------------------
APP_DIR="$HOME/.local/share/petacore-app"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "==> Installing Petacore ($VARIANT build) to $APP_DIR…"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
rm -rf "$APP_DIR/petacore"
cp -r petacore "$APP_DIR/"

# remember the choice so the launcher never has to guess
mkdir -p "$HOME/.config/petacore"
python3 - "$VARIANT" <<'PYEOF'
import json, os, sys
variant = sys.argv[1]
path = os.path.expanduser("~/.config/petacore/config.json")
data = {}
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    pass
data["ui"] = variant
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
PYEOF

cat > "$BIN_DIR/petacore" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$APP_DIR"
exec python3 -m petacore.launcher "\$@"
EOF
chmod +x "$BIN_DIR/petacore"

echo "==> Installing icons…"
ICON_BASE="$HOME/.local/share/icons/hicolor"
for size in 512x512 256x256 128x128 64x64 48x48 32x32 24x24 16x16; do
  if [ -f "data/icons/$size/io.petacore.Petacore.png" ]; then
    mkdir -p "$ICON_BASE/$size/apps"
    cp "data/icons/$size/io.petacore.Petacore.png" "$ICON_BASE/$size/apps/"
  fi
done
rm -f "$ICON_BASE/scalable/apps/io.petacore.Petacore.svg"
gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null || true

sed "s|Exec=petacore|Exec=$BIN_DIR/petacore|" \
    data/io.petacore.Petacore.desktop > "$DESKTOP_DIR/io.petacore.Petacore.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo
echo "Petacore ($VARIANT build) installed. Launch it from your app grid, or run: petacore"
echo "To switch builds later:  petacore --gnome   or   petacore --kde"
echo "(Make sure $BIN_DIR is on your PATH.)"
