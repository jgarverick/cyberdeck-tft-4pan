#!/usr/bin/env bash
# End-to-end test: deckd <-> virtual serial cable <-> simulator running the real ui.cpp.
# Stand-ins: tests/fakes/pulsectl.py for PipeWire, tests/fake_docker.py for the Docker socket.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"
OUT="${1:-$ROOT/tests/out}"
mkdir -p "$OUT"
rm -f /tmp/deck-flag /tmp/deck-ran /tmp/deck-confirm

# deckd config
mkdir -p "$W/cfg"
cat > "$W/cfg/config.json" <<EOF
{"port": "$W/pty-deckd", "baud": 921600, "theme": "cmp", "brightness": 200, "meters": true,
 "docker_socket": "$W/docker.sock"}
EOF
cat > "$W/cfg/deck.json" <<'EOF'
{"banks": [{"name": "TEST", "buttons": [
  {"label": "Flag", "icon": "bell", "type": "toggle",
   "state": "test -f /tmp/deck-flag", "on": "touch /tmp/deck-flag", "off": "rm -f /tmp/deck-flag"},
  {"label": "Run", "icon": "play", "wait": true, "run": "touch /tmp/deck-ran"},
  {"label": "Danger", "icon": "power", "type": "confirm", "run": "echo yes > /tmp/deck-confirm"}
]}, {"name": "OTHER", "buttons": [{"label": "Nothing", "icon": "ok"}]}]}
EOF

cat > "$W/script.txt" <<EOF
@2500 snap $OUT/e2e-1-mix.ppm
@3000 touch 137 110
@3600 touch 49 232
@4200 touch 180 303
@4700 touch 60 77
@6200 snap $OUT/e2e-2-deck.ppm
@6400 touch 180 77
@6900 hold 300 77 1000
@8200 touch 300 303
@11000 snap $OUT/e2e-3-sys.ppm
@11200 touch 420 303
@12500 snap $OUT/e2e-4-dock.ppm
@12700 touch 240 101
@14500 snap $OUT/e2e-5-detail.ppm
@14700 hold 273 77 1000
@16500 snap $OUT/e2e-6-after.ppm
EOF

socat pty,raw,echo=0,link="$W/pty-deckd" pty,raw,echo=0,link="$W/pty-cyd" 2>/dev/null &
SOCAT=$!
python3 "$ROOT/tests/fake_docker.py" "$W/docker.sock" "$W/docker.log" &
DOCKER=$!
sleep 1

DECKD_CONFIG="$W/cfg" FAKE_PULSE_LOG="$W/pulse.log" PYTHONPATH="$ROOT/tests/fakes" DECKD_LOG=INFO \
  python3 "$ROOT/deckd/deckd.py" > "$W/deckd.log" 2>&1 &
DECKD=$!
sleep 0.5
"$ROOT/sim/sim" --serial "$W/pty-cyd" --run 18 --script "$W/script.txt" > "$W/sim.log" 2>&1

kill $DECKD $DOCKER $SOCAT 2>/dev/null
wait 2>/dev/null
for f in "$OUT"/e2e-*.ppm; do convert "$f" "${f%.ppm}.png" && rm "$f"; done
cp "$W"/*.log "$OUT/"
echo "logs and screenshots in $OUT"
