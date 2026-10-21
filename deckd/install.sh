#!/usr/bin/env bash
# Installs deckd for the current user on the Pi.
# Run as your normal user (it calls sudo where it needs to):
#   bash install.sh                 # deckd only
#   bash install.sh --with-broker   # also installs Mosquitto as a local MQTT broker
set -euo pipefail

WITH_BROKER=0
[[ "${1:-}" == "--with-broker" ]] && WITH_BROKER=1

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/deckd"
CFG="$HOME/.config/deckd"

log() { printf '\e[1;35m[deckd]\e[0m %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, not with sudo." >&2
  exit 1
fi

log "Installing packages..."
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 python3-serial python3-psutil pipewire pipewire-pulse wireplumber playerctl \
  pulseaudio-utils libglib2.0-bin
if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-pulsectl; then
  log "python3-pulsectl isn't in your repositories; installing it with pip instead."
  pip install --user --break-system-packages pulsectl
fi

if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-paho-mqtt; then
  log "python3-paho-mqtt isn't in your repositories; installing it with pip instead."
  pip install --user --break-system-packages paho-mqtt
fi

if [[ $WITH_BROKER -eq 1 ]]; then
  log "Installing Mosquitto (listens on localhost only)..."
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends mosquitto mosquitto-clients
  sudo systemctl enable --now mosquitto
fi

log "Adding $USER to the dialout (serial) and docker groups..."
sudo usermod -aG dialout "$USER"
getent group docker >/dev/null && sudo usermod -aG docker "$USER" || log "No docker group yet (install Docker first for the DOCK page)."

log "Copying files..."
mkdir -p "$DEST" "$CFG" "$HOME/.config/systemd/user"
cp "$HERE/deckd.py" "$HERE/themes.py" "$HERE/events.py" "$DEST/"
[[ -f "$CFG/deck.json" ]] || cp "$HERE/deck.json" "$CFG/deck.json"
[[ -f "$CFG/config.json" ]] || cat > "$CFG/config.json" <<'EOF'
{
  "port": "auto",
  "baud": 921600,
  "theme": "auto",
  "brightness": 220,
  "meters": true,
  "mqtt": { "host": "localhost", "port": 1883 }
}
EOF
cp "$HERE/deckd.service" "$HOME/.config/systemd/user/deckd.service"

log "Enabling the service..."
systemctl --user daemon-reload
systemctl --user enable deckd.service
systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || true

log "Done. Log out and back in (for the new groups), then check it with:"
log "  systemctl --user status deckd"
log "  journalctl --user -u deckd -f"
log "Edit buttons in $CFG/deck.json; the CYD updates within a few seconds of saving."
