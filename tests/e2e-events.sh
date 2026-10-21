#!/usr/bin/env bash
# Event-driven behavior test: real Mosquitto, fake Docker (with /events and log follow),
# fake pactl subscribe, and a D-Bus-style watch command. Then checks the idle period for polling.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"
OUT="${1:-$ROOT/tests/out-events}"
mkdir -p "$OUT" "$W/cfg"
PORT=$((18000 + RANDOM % 1000))

cat > "$W/mosq.conf" <<EOF
listener $PORT 127.0.0.1
allow_anonymous true
persistence false
EOF
mosquitto -c "$W/mosq.conf" >/dev/null 2>&1 &
MOSQ=$!
sleep 0.5
mosquitto_pub -p $PORT -r -t test/lamp/state -m OFF
# A pretend smart device: echoes commands back as retained state.
(mosquitto_sub -p $PORT -t test/lamp/set | while read -r v; do mosquitto_pub -p $PORT -r -t test/lamp/state -m "$v"; done) &
DEVICE=$!
mosquitto_sub -p $PORT -v -t 'test/#' > "$W/mqtt.log" &
MQSUB=$!

cat > "$W/cfg/config.json" <<EOF
{"port": "$W/pty-deckd", "theme": "neon", "meters": false,
 "docker_socket": "$W/docker.sock", "mqtt": {"host": "127.0.0.1", "port": $PORT}}
EOF
cat > "$W/cfg/deck.json" <<EOF
{"banks": [{"name": "TEST", "buttons": [
  {"label": "Watched", "icon": "bell", "type": "toggle",
   "state": "echo x >> $W/state-watched.log; test -f $W/flag",
   "on": "touch $W/flag", "off": "rm -f $W/flag",
   "watch": "tail -n0 -F $W/signals.log"},
  {"label": "Lamp", "icon": "charge", "type": "toggle",
   "mqtt": {"state_topic": "test/lamp/state", "command_topic": "test/lamp/set"}},
  {"label": "Movie", "icon": "video", "mqtt": {"command_topic": "test/scene", "payload": "movie"}},
  {"label": "Polled", "icon": "eye", "type": "toggle",
   "state": "echo x >> $W/state-polled.log; test -f $W/flag2", "on": "touch $W/flag2", "off": "rm -f $W/flag2"}
]}]}
EOF

cat > "$W/script.txt" <<EOF
@2500 touch 180 303
@4000 touch 60 77
@5000 touch 180 77
@6000 touch 300 77
@8500 snap $OUT/ev-1-deck.ppm
@9000 touch 60 303
@9500 touch 137 110
@11000 touch 420 303
@12000 touch 240 101
@15500 snap $OUT/ev-2-logs.ppm
@15700 hold 273 77 1000
@18000 touch 180 303
@32000 snap $OUT/ev-3-idle.ppm
EOF

touch "$W/signals.log"   # dbus-monitor would already be running; tail needs the file to exist
socat pty,raw,echo=0,link="$W/pty-deckd" pty,raw,echo=0,link="$W/pty-cyd" 2>/dev/null &
SOCAT=$!
python3 "$ROOT/tests/fake_docker.py" "$W/docker.sock" "$W/docker.log" &
DOCKER=$!
sleep 0.5
PATH="$ROOT/tests/fakes:$PATH" DECKD_CONFIG="$W/cfg" FAKE_PULSE_LOG="$W/pulse.log" \
  PYTHONPATH="$ROOT/tests/fakes" DECKD_LOG=INFO python3 "$ROOT/deckd/deckd.py" > "$W/deckd.log" 2>&1 &
DECKD=$!
sleep 0.3
T0=$(date +%s.%N)
# Something outside the deck turns the watched toggle off at ~7 s (no button press).
(sleep 7; rm -f "$W/flag"; echo "PropertiesChanged" >> "$W/signals.log") &
"$ROOT/sim/sim" --serial "$W/pty-cyd" --run 33 --script "$W/script.txt" > "$W/sim.log" 2>&1

kill $DECKD $DOCKER $SOCAT $MQSUB $DEVICE $MOSQ 2>/dev/null
pkill -f "mosquitto_sub -p $PORT" 2>/dev/null
pkill -f "tail -n0 -F $W" 2>/dev/null
wait 2>/dev/null
for f in "$OUT"/ev-*.ppm; do convert "$f" "${f%.ppm}.png" && rm "$f"; done
cp "$W"/*.log "$OUT/" 2>/dev/null
echo "$W" > "$OUT/workdir"
echo "results in $OUT"
