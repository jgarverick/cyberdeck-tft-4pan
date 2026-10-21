"""Event sources for deckd.

Each watcher runs in a daemon thread, restarts itself with a backoff when its source
goes away, and reports changes by posting a key to the shared EventBus. The main loop
drains the bus; posts with a delay coalesce, so a burst of signals becomes one update.

  CommandWatcher  any long-running command; each output line is an event
                  (dbus-monitor for D-Bus signals, pactl subscribe for audio)
  DockerEvents    the Docker Engine /events stream (container lifecycle and health)
  DockerLogs      follows one container's logs while its detail view is open
  MqttBridge      MQTT state topics in, command topics out (paho-mqtt)
"""
import collections, http.client, json, logging, os, re, socket, subprocess, threading, time
from urllib.parse import quote

log = logging.getLogger("deckd.events")


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._due = {}

    def post(self, key, delay=0.0):
        """Schedule `key`. Repeated posts before it fires are merged into one."""
        at = time.monotonic() + delay
        with self._lock:
            if key not in self._due:
                self._due[key] = at

    def take(self):
        now = time.monotonic()
        with self._lock:
            ready = [k for k, at in self._due.items() if at <= now]
            for k in ready:
                del self._due[k]
        return ready


class Supervised:
    """Runs self._run() in a thread and restarts it with exponential backoff."""
    name = "watcher"

    def __init__(self):
        self.alive = False      # True while the source is connected and delivering
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._interrupt()

    def _interrupt(self):
        pass

    def _loop(self):
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._run()
            except Exception as e:  # any failure just means "reconnect later"
                log.debug("%s: %s", self.name, e)
            self.alive = False
            if self._stop.is_set():
                break
            failures = 0 if time.monotonic() - started > 60 else failures + 1
            wait = min(60, 2 ** min(failures, 6))
            if failures in (1, 4):
                log.warning("%s stopped; retrying in %ds", self.name, wait)
            self._stop.wait(wait)

    def _run(self):
        raise NotImplementedError


class CommandWatcher(Supervised):
    def __init__(self, cmd, bus, key, delay=0.25, line_filter=None):
        super().__init__()
        self.cmd, self.bus, self.key, self.delay = cmd, bus, key, delay
        self.line_filter = line_filter
        self.name = f"watch[{cmd[:40]}]"
        self.proc = None

    def _run(self):
        self.proc = subprocess.Popen(self.cmd, shell=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                     start_new_session=True)
        self.alive = True
        self.bus.post(self.key)  # (re)connected: refresh once
        for line in self.proc.stdout:
            if self._stop.is_set():
                break
            if self.line_filter is None or self.line_filter(line):
                self.bus.post(self.key, self.delay)
        code = self.proc.wait()
        if not self._stop.is_set():
            raise RuntimeError(f"exited with code {code}")

    def _interrupt(self):
        try:
            if self.proc and self.proc.poll() is None:
                os.killpg(self.proc.pid, 15)
        except (ProcessLookupError, PermissionError):
            pass


# pactl subscribe prints e.g.  Event 'change' on sink-input #42
# Only changes to outputs, inputs, streams, the server (default device), and cards matter.
# Client and source-output events come from deckd's own connections and the level meters.
_PACTL_RELEVANT = re.compile(r"on (sink|source|sink-input|server|card)\b")


def pactl_watcher(bus):
    return CommandWatcher("LC_ALL=C pactl subscribe", bus, "mix", delay=0.15,
                          line_filter=lambda l: bool(_PACTL_RELEVANT.search(l)))


# ------------------------------------------------------------------------------- docker

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


DOCKER_EVENTS = ["start", "die", "stop", "restart", "pause", "unpause", "create", "destroy",
                 "rename", "health_status", "oom"]


class DockerEvents(Supervised):
    name = "docker events"

    def __init__(self, sock_path, bus):
        super().__init__()
        self.sock_path, self.bus = sock_path, bus
        self.conn = None

    def _run(self):
        filters = json.dumps({"type": ["container"], "event": DOCKER_EVENTS})
        self.conn = _UnixHTTPConnection(self.sock_path)
        self.conn.request("GET", "/events?filters=" + quote(filters), headers={"Host": "docker"})
        resp = self.conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        self.alive = True
        self.bus.post("dock")  # (re)connected: refresh once
        for line in resp:
            if self._stop.is_set():
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            action = (ev.get("Action") or ev.get("status") or "").split(":")[0]
            if action in DOCKER_EVENTS:
                self.bus.post("dock", 0.3)
        raise RuntimeError("event stream ended")

    def _interrupt(self):
        try:
            if self.conn and self.conn.sock:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class DockerLogs(Supervised):
    """Follows one container's log output; keeps the last N lines."""

    def __init__(self, sock_path, bus, cid, tail=14):
        super().__init__()
        self.sock_path, self.bus, self.cid = sock_path, bus, cid
        self.lines = collections.deque(maxlen=tail)
        self.tail = tail
        self.name = f"logs[{cid}]"
        self.conn = None
        self._lock = threading.Lock()

    def text(self):
        with self._lock:
            return "\n".join(l[:120] for l in self.lines)

    def _add(self, chunk):
        with self._lock:
            for l in chunk.splitlines():
                self.lines.append(l)
        self.bus.post(("logs", self.cid), 0.3)

    def _run(self):
        with self._lock:
            self.lines.clear()
        self.conn = _UnixHTTPConnection(self.sock_path)
        self.conn.request("GET", f"/containers/{self.cid}/logs?stdout=1&stderr=1&follow=1&tail={self.tail}",
                          headers={"Host": "docker"})
        resp = self.conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        self.alive = True
        self.bus.post(("logs", self.cid))
        first = resp.read(8)
        if not first:
            raise RuntimeError("log stream ended")
        # Containers without a TTY multiplex stdout/stderr: 8-byte header, then the payload.
        multiplexed = len(first) == 8 and first[0] in (0, 1, 2) and first[1:4] == b"\x00\x00\x00"
        if multiplexed:
            header = first
            while header and len(header) == 8 and not self._stop.is_set():
                size = int.from_bytes(header[4:8], "big")
                self._add(resp.read(size).decode(errors="replace"))
                header = resp.read(8)
        else:
            buf = first.decode(errors="replace")
            for raw in resp:
                buf += raw.decode(errors="replace")
                if "\n" in buf:
                    done, _, buf = buf.rpartition("\n")
                    self._add(done)
                if self._stop.is_set():
                    break
        if not self._stop.is_set():
            raise RuntimeError("log stream ended")  # container stopped; retry with backoff

    def _interrupt(self):
        try:
            if self.conn and self.conn.sock:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


# --------------------------------------------------------------------------------- mqtt

class MqttBridge:
    """Keeps the latest payload of every subscribed state topic. Reconnects on its own."""

    def __init__(self, cfg, bus):
        self.bus = bus
        self.cfg = cfg or {}
        self.values = {}
        self.topics = set()
        self.connected = False
        self.client = None
        self.error = ""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "python3-paho-mqtt is not installed"
            return
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=self.cfg.get("client_id", "deckd"))
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client(client_id=self.cfg.get("client_id", "deckd"))
        if self.cfg.get("username"):
            self.client.username_pw_set(self.cfg["username"], self.cfg.get("password"))
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 30)

    def start(self):
        if not self.client:
            return self
        host, port = self.cfg.get("host", "localhost"), int(self.cfg.get("port", 1883))
        try:
            self.client.connect_async(host, port, keepalive=30)
            self.client.loop_start()
        except Exception as e:
            self.error = str(e)
        return self

    def _on_connect(self, client, userdata, flags, rc, *props):
        ok = (rc == 0) if isinstance(rc, int) else not getattr(rc, "is_failure", False)
        self.connected = ok
        if ok:
            log.info("MQTT connected")
            for t in self.topics:
                client.subscribe(t)
        else:
            log.warning("MQTT connection refused: %s", rc)

    def _on_disconnect(self, client, userdata, *args):
        if self.connected:
            log.warning("MQTT disconnected; reconnecting")
        self.connected = False

    def _on_message(self, client, userdata, msg):
        self.values[msg.topic] = msg.payload.decode(errors="replace").strip()
        self.bus.post(("mqtt", msg.topic), 0.05)

    def set_topics(self, topics):
        topics = set(topics)
        if not self.client:
            self.topics = topics
            return
        for t in self.topics - topics:
            if self.connected:
                self.client.unsubscribe(t)
        for t in topics - self.topics:
            if self.connected:
                self.client.subscribe(t)
        self.topics = topics

    def publish(self, topic, payload, retain=False):
        if not self.client or not self.connected:
            return False
        self.client.publish(topic, payload, retain=retain)
        return True
