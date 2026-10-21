#!/usr/bin/env python3
"""deckd: the Pi side of the cyd-deck control surface.

Talks to the CYD over USB serial (one JSON object per line) and feeds its four pages:
  0 MIX   PipeWire/PulseAudio mixer (pulsectl)
  1 DECK  buttons from ~/.config/deckd/deck.json
  2 SYS   psutil system stats and top processes
  3 DOCK  Docker Engine API over /var/run/docker.sock

Everything that can be event-driven is: audio changes (pactl subscribe), container
lifecycle and health (Docker events), container logs (followed while open), toggle
buttons (D-Bus watch commands or MQTT). Only CPU, memory, temperature, processes, and
container CPU/memory are sampled, mostly while their page is on screen.
See PROTOCOL.md for the message format and events.py for the event sources.
"""
import glob, http.client, json, logging, os, shutil, signal, socket, subprocess, sys, termios, threading, time, traceback
from pathlib import Path

import psutil
import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from themes import THEMES
import events

try:
    import pulsectl
except ImportError:  # mixer page shows "no audio" instead of failing
    pulsectl = None

log = logging.getLogger("deckd")
CFG_DIR = Path(os.environ.get("DECKD_CONFIG", Path.home() / ".config" / "deckd"))
DEFAULTS = {
    "port": "auto",          # or e.g. /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
    "baud": 921600,
    "theme": "auto",         # auto | neon | cmp
    "brightness": 220,       # CYD backlight 0-255
    "meters": True,          # audio level meters (uses a little CPU)
    "docker_socket": "/var/run/docker.sock",
    "max_app_strips": 3,
    "mqtt": {"host": "localhost", "port": 1883},  # used only if deck.json has MQTT buttons
}
PAGE_MIX, PAGE_DECK, PAGE_SYS, PAGE_DOCK = range(4)


def load_config():
    cfg = dict(DEFAULTS)
    path = CFG_DIR / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text()))
        except (OSError, ValueError) as e:
            log.warning("config.json ignored: %s", e)
    return cfg


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def run_shell(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, timeout=timeout, capture_output=True, text=True)
        return r.returncode, (r.stdout or r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def spawn(cmd):
    """Launch a command detached from deckd (GUI apps, long-running tools)."""
    subprocess.Popen(cmd, shell=True, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ----------------------------------------------------------------------------- theme

def detect_theme(cfg):
    if cfg["theme"] in THEMES:
        return cfg["theme"]
    rofi = Path.home() / ".config" / "rofi" / "config.rasi"
    try:
        if "cmp.rasi" in rofi.read_text():
            return "cmp"
    except OSError:
        pass
    return "neon"


# ----------------------------------------------------------------------------- mixer

class Mixer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pulse = None
        self.strip_ids = []
        self.levels = []
        self._lock = threading.Lock()
        self.proc = None
        self.started_at = 0.0
        self.retry_at = 0.0
        self.crashes = []
        self.meters_disabled = False
        self.sent_ids = None
        self.notice = ""
        self.error = ""

    def _connect(self):
        if pulsectl is None:
            self.error = "python3-pulsectl is not installed"
            return False
        if self.pulse is None:
            try:
                self.pulse = pulsectl.Pulse("deckd")
            except Exception as e:  # pulsectl raises its own error types
                self.error = f"audio server not reachable: {e}"
                self.pulse = None
                return False
        return True

    def _reset(self):
        try:
            if self.pulse:
                self.pulse.close()
        except Exception:
            pass
        self.pulse = None

    @staticmethod
    def _pct(obj):
        return int(round(obj.volume.value_flat * 100))

    def state(self):
        if not self._connect():
            return {"t": "mix", "out": [], "out_sel": 0, "strips": []}
        try:
            p = self.pulse
            info = p.server_info()
            sinks = p.sink_list()
            default_sink = next((s for s in sinks if s.name == info.default_sink_name), sinks[0] if sinks else None)
            strips, ids = [], []
            if default_sink:
                strips.append({"id": f"sink:{default_sink.name}", "name": "MASTER",
                               "vol": self._pct(default_sink), "mute": bool(default_sink.mute)})
            for si in p.sink_input_list()[: self.cfg["max_app_strips"]]:
                name = (si.proplist.get("application.name") or si.proplist.get("media.name") or si.name or "App")
                strips.append({"id": f"in:{si.index}", "name": name[:16],
                               "vol": self._pct(si), "mute": bool(si.mute)})
            sources = [s for s in p.source_list() if not s.name.endswith(".monitor")]
            src = next((s for s in sources if s.name == info.default_source_name), sources[0] if sources else None)
            if src:
                strips.append({"id": f"src:{src.name}", "name": "MIC", "vol": self._pct(src), "mute": bool(src.mute)})
            with self._lock:
                self.strip_ids = [s["id"] for s in strips]
            out_sel = sinks.index(default_sink) if default_sink in sinks else 0
            return {"t": "mix", "out": [(s.description or s.name)[:40] for s in sinks],
                    "out_sel": out_sel, "strips": strips}
        except Exception as e:
            log.warning("mixer read failed: %s", e)
            self._reset()
            return {"t": "mix", "out": [], "out_sel": 0, "strips": []}

    def _find(self, sid):
        kind, _, key = sid.partition(":")
        p = self.pulse
        if kind == "sink":
            return next((s for s in p.sink_list() if s.name == key), None)
        if kind == "in":
            return next((s for s in p.sink_input_list() if str(s.index) == key), None)
        if kind == "src":
            return next((s for s in p.source_list() if s.name == key), None)
        return None

    def set_volume(self, sid, v):
        if not self._connect():
            return
        obj = self._find(sid)
        if obj:
            self.pulse.volume_set_all_chans(obj, max(0, min(100, int(v))) / 100.0)

    def toggle_mute(self, sid):
        if not self._connect():
            return
        obj = self._find(sid)
        if obj:
            self.pulse.mute(obj, not obj.mute)

    def set_output(self, index):
        if not self._connect():
            return None
        sinks = self.pulse.sink_list()
        if not 0 <= index < len(sinks):
            return None
        sink = sinks[index]
        self.pulse.default_set(sink)
        for si in self.pulse.sink_input_list():
            try:
                self.pulse.sink_input_move(si.index, sink.index)
            except Exception:
                pass
        return sink.description or sink.name

    # Level meters run in a separate helper process (deckd.py --meters). Peak sampling goes
    # through native PulseAudio code; if that crashes, only the helper dies, deckd keeps
    # running, and the serial port (and the CYD) are left alone.
    def meters_wanted(self, on):
        if not self.cfg["meters"] or pulsectl is None or self.meters_disabled:
            on = False
        if on and self.proc is None:
            now = time.monotonic()
            if now < self.retry_at:
                return
            env = dict(os.environ)
            self.proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--meters"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
            self.started_at = now
            self.sent_ids = None
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        elif not on and self.proc is not None:
            self._stop()

    def _reader(self, proc):
        for line in proc.stdout:
            try:
                vals = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                self.levels = vals
        code = proc.wait()
        if code not in (0, -signal.SIGTERM) and proc is self.proc:
            self._on_crash(code)

    def _on_crash(self, code):
        now = time.monotonic()
        self.crashes = [t for t in self.crashes if now - t < 120] + [now]
        log.warning("level meter helper exited with code %s (%d crash(es) in 2 minutes)", code, len(self.crashes))
        self.proc = None
        if len(self.crashes) >= 3:
            self.meters_disabled = True
            self.notice = "Level meters off: audio sampling keeps crashing"
            log.error("level meters disabled until deckd restarts")
        else:
            self.retry_at = now + 5 * len(self.crashes)

    def _stop(self):
        proc, self.proc = self.proc, None
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        with self._lock:
            self.levels = []

    def push_strip_ids(self):
        if self.proc is None:
            return
        with self._lock:
            ids = list(self.strip_ids)
        if ids != self.sent_ids:
            try:
                self.proc.stdin.write(json.dumps(ids) + "\n")
                self.proc.stdin.flush()
                self.sent_ids = ids
            except (OSError, ValueError):
                pass

    def take_levels(self):
        with self._lock:
            return list(self.levels)


def meter_helper():
    """Runs as `deckd.py --meters`: reads strip id lists on stdin, prints levels on stdout."""
    import select
    pulse = pulsectl.Pulse("deckd-meters")
    ids = []
    while True:
        if select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if not line:
                return  # deckd went away
            try:
                ids = json.loads(line)
            except ValueError:
                pass
        if not ids:
            time.sleep(0.2)
            continue
        sinks = {s.name: s for s in pulse.sink_list()}
        inputs = {str(si.index): si for si in pulse.sink_input_list()}
        out = []
        for sid in ids:
            kind, _, key = sid.partition(":")
            peak = 0.0
            try:
                if kind == "sink" and key in sinks:
                    peak = pulse.get_peak_sample(sinks[key].monitor_source_name, 0.05)
                elif kind == "in" and key in inputs:
                    sink = next((s for s in sinks.values() if s.index == inputs[key].sink), None)
                    if sink:
                        peak = pulse.get_peak_sample(sink.monitor_source_name, 0.05, int(key))
                elif kind == "src":
                    peak = pulse.get_peak_sample(key, 0.05)
            except Exception:
                peak = 0.0
            out.append(int(min(1.0, peak) * 100))
        print(json.dumps(out), flush=True)
        time.sleep(0.1)


# ----------------------------------------------------------------------------- system

class SystemStats:
    def __init__(self):
        self.last_net = psutil.net_io_counters()
        self.last_t = time.monotonic()
        self.procs = {}
        psutil.cpu_percent(percpu=True)

    @staticmethod
    def _temp():
        try:
            return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
        except (OSError, ValueError):
            return 0.0

    @staticmethod
    def _throttled():
        code, out = run_shell("vcgencmd get_throttled", timeout=2)
        if code != 0 or "=" not in out:
            return False
        try:
            return bool(int(out.split("=")[1], 16) & 0x4)  # bit 2: throttled right now
        except ValueError:
            return False

    def sample(self):
        cores = psutil.cpu_percent(percpu=True)
        cpu = sum(cores) / len(cores) if cores else 0
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt = max(0.001, now - self.last_t)
        up = (net.bytes_sent - self.last_net.bytes_sent) / dt
        down = (net.bytes_recv - self.last_net.bytes_recv) / dt
        self.last_net, self.last_t = net, now
        freq = psutil.cpu_freq()
        secs = int(time.time() - psutil.boot_time())
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        uptime = f"{d}d{h}h" if d else f"{h}h{m:02d}m"
        return {"t": "sys", "cpu": int(round(cpu)), "cores": [int(round(c)) for c in cores[:4]],
                "mem": int(round(vm.percent)), "mem_used": human(vm.total - vm.available),
                "mem_total": human(vm.total), "disk": int(round(du.percent)),
                "temp": round(self._temp(), 1), "throttled": self._throttled(),
                "up": human(up), "down": human(down), "uptime": uptime,
                "freq": int(freq.current) if freq else 0}

    def top(self, n=5):
        seen = set()
        rows = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            pid = p.info["pid"]
            seen.add(pid)
            proc = self.procs.get(pid)
            if proc is None:
                self.procs[pid] = p
                p.cpu_percent(None)  # prime; real value on the next pass
                continue
            try:
                cpu = proc.cpu_percent(None)
                mem = proc.memory_percent()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            name = p.info["name"] or "?"
            cmd = p.info.get("cmdline") or []
            if name.startswith("python") and len(cmd) > 1:
                name = f"{name} {os.path.basename(cmd[1])}"
            rows.append({"pid": pid, "name": name[:22], "cpu": round(cpu, 1), "mem": round(mem, 1)})
        for pid in list(self.procs):
            if pid not in seen:
                del self.procs[pid]
        rows.sort(key=lambda r: r["cpu"], reverse=True)
        return {"t": "procs", "p": rows[:n]}


# ----------------------------------------------------------------------------- docker

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.prev = {}  # container id -> (cpu_total, system_total)

    def _req(self, method, path, timeout=5):
        conn = UnixHTTPConnection(self.sock_path, timeout)
        try:
            conn.request(method, path, headers={"Host": "docker"})
            r = conn.getresponse()
            body = r.read()
            if r.status >= 400:
                try:
                    msg = json.loads(body).get("message", body.decode(errors="replace"))
                except ValueError:
                    msg = body.decode(errors="replace")
                raise RuntimeError(f"{r.status}: {msg}")
            return body
        finally:
            conn.close()

    def _cpu_mem(self, cid):
        s = json.loads(self._req("GET", f"/containers/{cid}/stats?stream=false&one-shot=true", timeout=3))
        cpu_total = s.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
        sys_total = s.get("cpu_stats", {}).get("system_cpu_usage", 0)
        ncpu = s.get("cpu_stats", {}).get("online_cpus") or psutil.cpu_count() or 1
        cpu = 0.0
        if cid in self.prev:
            pc, ps = self.prev[cid]
            if sys_total > ps:
                cpu = (cpu_total - pc) / (sys_total - ps) * ncpu * 100
        self.prev[cid] = (cpu_total, sys_total)
        mem = s.get("memory_stats", {})
        used = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
        return round(max(0.0, cpu), 1), human(max(0, used))

    def state(self, with_stats=True):
        try:
            items = json.loads(self._req("GET", "/containers/json?all=1"))
        except (OSError, RuntimeError, ValueError) as e:
            err = str(e)
            if isinstance(e, PermissionError):
                err = "permission denied on docker.sock (add your user to the docker group)"
            elif isinstance(e, FileNotFoundError):
                err = "Docker isn't installed or isn't running"
            return {"t": "dock", "ok": False, "err": err[:120], "c": []}
        items.sort(key=lambda c: (c.get("State") != "running", (c.get("Names") or ["?"])[0]))
        out = []
        for c in items[:16]:
            cid = c["Id"][:12]
            status = c.get("Status", "")
            health = "unhealthy" if "(unhealthy)" in status else "healthy" if "(healthy)" in status else ""
            row = {"id": cid, "name": (c.get("Names") or ["?"])[0].lstrip("/")[:30],
                   "state": c.get("State", "?"), "status": status[:40], "health": health, "cpu": 0, "mem": ""}
            if with_stats and row["state"] == "running":
                try:
                    row["cpu"], row["mem"] = self._cpu_mem(cid)
                except (OSError, RuntimeError, ValueError):
                    pass
            out.append(row)
        return {"t": "dock", "ok": True, "c": out}

    def action(self, cid, action):
        if action not in ("start", "stop", "restart"):
            raise RuntimeError(f"unknown action {action}")
        self._req("POST", f"/containers/{cid}/{action}", timeout=30)

    def logs(self, cid, tail=14):
        raw = self._req("GET", f"/containers/{cid}/logs?stdout=1&stderr=1&tail={tail}")
        # Non-TTY containers multiplex stdout/stderr with an 8-byte header per frame.
        lines, i = [], 0
        if raw[:1] in (b"\x00", b"\x01", b"\x02") and len(raw) >= 8:
            while i + 8 <= len(raw):
                size = int.from_bytes(raw[i + 4:i + 8], "big")
                lines.append(raw[i + 8:i + 8 + size].decode(errors="replace"))
                i += 8 + size
            text = "".join(lines)
        else:
            text = raw.decode(errors="replace")
        return "\n".join(l[:120] for l in text.splitlines()[-tail:])


# ----------------------------------------------------------------------------- deck

class Deck:
    """deck.json plus a cache of toggle states, kept current by events instead of polling."""

    def __init__(self):
        self.path = CFG_DIR / "deck.json"
        self.mtime = 0
        self.banks = []
        self.state = {}  # (bank, index) -> bool

    def reload_if_changed(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
            if self.mtime != -1:
                log.warning("no deck.json at %s; the DECK page stays empty until it exists", self.path)
                self.mtime = -1
            return False
        if m == self.mtime:
            return False
        self.mtime = m
        try:
            self.banks = json.loads(self.path.read_text()).get("banks", [])[:6]
            log.info("loaded %d banks from %s", len(self.banks), self.path)
        except (OSError, ValueError) as e:
            log.error("deck.json is invalid: %s", e)
            return False
        self.state = {}
        return True

    def message(self):
        keep = ("label", "icon", "color", "type", "page", "bank")
        return {"t": "deck", "banks": [
            {"name": b.get("name", "BANK")[:10],
             "buttons": [{k: v for k, v in btn.items() if k in keep} for btn in b.get("buttons", [])[:12]]}
            for b in self.banks]}

    def buttons(self):
        for b, bank in enumerate(self.banks):
            for i, btn in enumerate(bank.get("buttons", [])[:12]):
                yield b, i, btn

    def toggles(self):
        return [(b, i, btn) for b, i, btn in self.buttons() if btn.get("type") == "toggle"]

    def dstate(self, bank):
        if not 0 <= bank < len(self.banks):
            return None
        n = len(self.banks[bank].get("buttons", [])[:12])
        return {"t": "dstate", "bank": bank, "on": [bool(self.state.get((bank, i))) for i in range(n)]}

    def run_action(self, btn):
        """Runs a non-toggle button. Returns (toast text, level) or None."""
        label = btn.get("label", "button")
        if btn.get("i3"):
            rc, out = run_shell(f"i3-msg {json.dumps(btn['i3'])}", timeout=5)
            return None if rc == 0 else (f"i3: {out[:40]}", "bad")
        if btn.get("run"):
            if btn.get("wait"):
                rc, out = run_shell(btn["run"], timeout=30)
                return (f"{label}: done", "ok") if rc == 0 else (f"{label} failed: {out[:40]}", "bad")
            spawn(btn["run"])
            return (f"{label}", "info") if btn.get("type") == "confirm" else None
        return None


# ----------------------------------------------------------------------------- daemon

# How often the few things with no change events are sampled (seconds).
SYS_VISIBLE, SYS_HIDDEN = 1, 10          # CPU, memory, temperature (hidden: keeps the alert LED honest)
PROCS_VISIBLE = 2
DOCK_STATS_VISIBLE = 3                   # container CPU and memory while DOCK is on screen
FALLBACK = {"mix": 5, "dock": 30, "toggle": 10}   # only when an event source is unavailable
HEARTBEAT = 3                            # the CYD treats 6 s of silence as a lost link


class DeckD:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ser = None
        self.page = 0
        self.theme = detect_theme(cfg)
        self.bus = events.EventBus()
        self.mixer = Mixer(cfg)
        self.sys = SystemStats()
        self.docker = Docker(cfg["docker_socket"])
        self.deck = Deck()
        self.dock_sel = ""
        self.next_due = {}
        self.rx = b""
        self.bank_shown = 0
        self.last_full = 0.0
        self.last_lvl = None
        self.last_tx = 0.0
        self.watchers = {}       # watch command -> CommandWatcher
        self.logs = None         # DockerLogs for the open container
        self.mqtt = None
        self.mqtt_warned = False

        self.mix_events = events.pactl_watcher(self.bus).start() if shutil.which("pactl") else None
        if self.mix_events is None:
            log.warning("pactl not found (install pulseaudio-utils); mixer falls back to polling")
        self.dock_events = events.DockerEvents(cfg["docker_socket"], self.bus).start()
        if self.deck.reload_if_changed():
            self.on_deck_loaded()

    # -- deck and toggles
    def on_deck_loaded(self):
        wanted = {btn["watch"] for _, _, btn in self.deck.toggles() if btn.get("watch")}
        for cmd in list(self.watchers):
            if cmd not in wanted:
                self.watchers.pop(cmd).stop()
        for cmd in wanted - set(self.watchers):
            self.watchers[cmd] = events.CommandWatcher(cmd, self.bus, ("watch", cmd)).start()
        topics = [btn["mqtt"]["state_topic"] for _, _, btn in self.deck.buttons()
                  if isinstance(btn.get("mqtt"), dict) and btn["mqtt"].get("state_topic")]
        if topics or any(isinstance(btn.get("mqtt"), dict) for _, _, btn in self.deck.buttons()):
            if self.mqtt is None:
                self.mqtt = events.MqttBridge(self.cfg.get("mqtt"), self.bus).start()
                if self.mqtt.error:
                    log.warning("MQTT buttons unavailable: %s", self.mqtt.error)
            self.mqtt.set_topics(topics)
        self.refresh_toggles(lambda b, i, btn: True)

    def refresh_toggles(self, which):
        """Re-reads the toggles matched by which(bank, index, button); returns banks that changed."""
        changed = set()
        for b, i, btn in self.deck.toggles():
            if not which(b, i, btn):
                continue
            m = btn.get("mqtt")
            if isinstance(m, dict) and m.get("state_topic"):
                val = self.mqtt.values.get(m["state_topic"]) if self.mqtt else None
                if val is None:
                    continue
                on = val == str(m.get("payload_on", "ON"))
            elif btn.get("state"):
                code, _ = run_shell(btn["state"], timeout=3)
                on = code == 0
            else:
                continue
            if self.deck.state.get((b, i)) != on:
                self.deck.state[(b, i)] = on
                changed.add(b)
        return changed

    def watched(self, btn):
        """True when a toggle is kept current by an event source (no polling needed)."""
        m = btn.get("mqtt")
        if isinstance(m, dict) and m.get("state_topic"):
            return True
        w = self.watchers.get(btn.get("watch"))
        return bool(w and w.alive)

    def press(self, bank, idx):
        try:
            btn = self.deck.banks[bank]["buttons"][idx]
        except (IndexError, KeyError):
            return None
        label = btn.get("label", "button")
        m = btn.get("mqtt") if isinstance(btn.get("mqtt"), dict) else None
        if btn.get("type") == "toggle":
            on = bool(self.deck.state.get((bank, idx)))
            if m:
                payload = m.get("payload_off", "OFF") if on else m.get("payload_on", "ON")
                if not (self.mqtt and self.mqtt.publish(m["command_topic"], payload)):
                    return (f"{label}: MQTT not connected", "bad")
                if not m.get("state_topic"):  # no feedback topic: assume it worked
                    self.deck.state[(bank, idx)] = not on
                return None
            cmd = btn.get("off") if on else btn.get("on")
            if not cmd:
                return None
            rc, out = run_shell(cmd, timeout=15)
            self.refresh_toggles(lambda b, i, _: (b, i) == (bank, idx))
            return (f"{label}: {'off' if on else 'on'}", "ok") if rc == 0 else (f"{label} failed: {out[:40]}", "bad")
        if m and m.get("command_topic"):
            ok = self.mqtt and self.mqtt.publish(m["command_topic"], str(m.get("payload", "")))
            return None if ok else (f"{label}: MQTT not connected", "bad")
        return self.deck.run_action(btn)

    def send_dstate(self, banks=None):
        if banks is None or self.bank_shown in banks:
            st = self.deck.dstate(self.bank_shown)
            if st:
                self.send(st)

    # -- docker logs follow the open detail view
    def follow_logs(self, cid):
        if self.logs and self.logs.cid == cid:
            return
        if self.logs:
            self.logs.stop()
            self.logs = None
        if cid:
            self.logs = events.DockerLogs(self.cfg["docker_socket"], self.bus, cid).start()

    # -- serial
    def find_port(self):
        if self.cfg["port"] != "auto":
            return self.cfg["port"]
        for pattern in ("/dev/serial/by-id/*1a86*", "/dev/serial/by-id/*CH34*", "/dev/serial/by-id/*CP210*",
                        "/dev/ttyUSB*", "/dev/ttyACM*"):
            hits = sorted(glob.glob(pattern))
            if hits:
                return hits[0]
        return None

    def open(self):
        port = self.find_port()
        if not port:
            return False
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, self.cfg["baud"], 0
        # Leave DTR and RTS at their defaults (both asserted). The CYD's auto-flash circuit
        # resets the ESP32 when RTS is asserted and DTR isn't, so changing them here would
        # reboot the board every time the port opens.
        try:
            s.open()
        except (OSError, serial.SerialException) as e:
            log.warning("can't open %s: %s", port, e)
            return False
        try:
            attrs = termios.tcgetattr(s.fd)
            attrs[2] &= ~termios.HUPCL  # keep DTR/RTS steady on close so the CYD doesn't reset
            termios.tcsetattr(s.fd, termios.TCSANOW, attrs)
        except (OSError, AttributeError, termios.error) as e:
            log.debug("couldn't clear HUPCL: %s", e)
        self.ser = s
        self.last_full = 0.0
        log.info("connected to %s", port)
        self.send({"t": "hello"})
        return True

    def send(self, msg):
        if not self.ser:
            return
        self.last_tx = time.monotonic()
        try:
            self.ser.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except (OSError, serial.SerialException) as e:
            log.warning("serial write failed: %s", e)
            self.close()

    def close(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def read_lines(self):
        try:
            data = self.ser.read(4096)
        except (OSError, serial.SerialException) as e:
            log.warning("serial read failed: %s", e)
            self.close()
            return []
        self.rx += data
        lines = []
        while b"\n" in self.rx:
            line, self.rx = self.rx.split(b"\n", 1)
            lines.append(line.strip())
        if len(self.rx) > 16384:
            self.rx = b""
        return lines

    # -- full state
    def send_all(self, with_theme=True):
        if with_theme:
            self.send(dict(THEMES[self.theme], t="theme"))
            self.send({"t": "bl", "v": int(self.cfg["brightness"])})
        self.send(self.deck.message())
        self.send_dstate()
        self.send(self.mixer.state())
        self.send(self.sys.sample())
        self.send(self.sys.top())
        self.send(self.docker.state(with_stats=False))
        self.next_due.clear()

    # -- incoming
    def handle(self, raw):
        if not raw.startswith(b"{"):
            return  # boot ROM chatter after a reset
        try:
            m = json.loads(raw)
        except ValueError:
            return
        t = m.get("t")
        if t == "ready":
            self.page = int(m.get("page", 0))
            if time.monotonic() - self.last_full < 1.5:
                return  # the CYD's boot announcement and our hello crossed; one full sync is enough
            log.info("CYD ready (fw %s, %s KB free, last reset: %s)", m.get("fw"),
                     int(m.get("heap", 0)) // 1024, m.get("reset", "?"))
            if m.get("reset") in ("brownout", "panic", "task watchdog", "interrupt watchdog", "watchdog"):
                log.warning("the CYD rebooted because of a %s", m.get("reset"))
            self.last_full = time.monotonic()
            self.send_all(with_theme=True)
        elif t == "resync":
            self.send_all(with_theme=False)
        elif t == "page":
            self.page = int(m.get("p", 0))
            self.next_due.clear()
            if self.page != PAGE_DOCK:
                self.follow_logs("")
            elif self.dock_sel:
                self.follow_logs(self.dock_sel)
        elif t == "bank":
            self.bank_shown = int(m.get("b", 0))
            self.send_dstate()
        elif t == "vol":
            self.mixer.set_volume(m.get("id", ""), m.get("v", 0))
        elif t == "mute":
            self.mixer.toggle_mute(m.get("id", ""))
            if not (self.mix_events and self.mix_events.alive):
                self.send(self.mixer.state())
        elif t == "out":
            name = self.mixer.set_output(int(m.get("i", 0)))
            if name:
                self.send({"t": "toast", "msg": f"Output: {name[:30]}", "level": "ok"})
            if not (self.mix_events and self.mix_events.alive):
                self.send(self.mixer.state())
        elif t == "btn":
            bank, idx = int(m.get("bank", 0)), int(m.get("i", 0))
            self.bank_shown = bank
            result = self.press(bank, idx)
            if result:
                self.send({"t": "toast", "msg": result[0][:48], "level": result[1]})
            self.send_dstate()
        elif t == "kill":
            pid = int(m.get("pid", 0))
            if pid <= 1 or pid == os.getpid():
                self.send({"t": "toast", "msg": "Not killing that one", "level": "bad"})
                return
            try:
                os.kill(pid, signal.SIGTERM)
                self.send({"t": "toast", "msg": f"SIGTERM sent to {pid}", "level": "ok"})
            except PermissionError:
                self.send({"t": "toast", "msg": f"No permission to end {pid}", "level": "bad"})
            except ProcessLookupError:
                self.send({"t": "toast", "msg": f"{pid} already exited", "level": "info"})
        elif t == "dact":
            cid, action = m.get("id", ""), m.get("a", "")
            try:
                self.docker.action(cid, action)
                self.send({"t": "toast", "msg": f"{action}: done", "level": "ok"})
            except (OSError, RuntimeError) as e:
                self.send({"t": "toast", "msg": f"{action} failed: {str(e)[:30]}", "level": "bad"})
            if not self.dock_events.alive:
                self.bus.post("dock")
        elif t == "dsel":
            self.dock_sel = m.get("id", "")
            self.follow_logs(self.dock_sel if self.page == PAGE_DOCK else "")

    # -- events
    def on_event(self, key):
        if key == "mix":
            self.send(self.mixer.state())
        elif key == "dock":
            self.send(self.docker.state(with_stats=(self.page == PAGE_DOCK)))
        elif isinstance(key, tuple) and key[0] == "watch":
            cmd = key[1]
            self.send_dstate(self.refresh_toggles(lambda b, i, btn: btn.get("watch") == cmd))
        elif isinstance(key, tuple) and key[0] == "mqtt":
            topic = key[1]
            self.send_dstate(self.refresh_toggles(
                lambda b, i, btn: isinstance(btn.get("mqtt"), dict) and btn["mqtt"].get("state_topic") == topic))
        elif isinstance(key, tuple) and key[0] == "logs":
            if self.logs and self.logs.cid == key[1] == self.dock_sel:
                self.send({"t": "logs", "id": key[1], "lines": self.logs.text() or "(no output yet)"})

    # -- the few things that have to be sampled
    def due(self, key, interval):
        now = time.monotonic()
        if now >= self.next_due.get(key, 0):
            self.next_due[key] = now + interval
            return True
        return False

    def tick(self):
        p = self.page
        for key in self.bus.take():
            self.on_event(key)

        # Fallbacks, only while an event source is unavailable
        if not (self.mix_events and self.mix_events.alive) and self.due("mix", FALLBACK["mix"]):
            self.send(self.mixer.state())
        if not self.dock_events.alive and self.due("dock", FALLBACK["dock"]):
            self.send(self.docker.state(with_stats=(p == PAGE_DOCK)))
        if p == PAGE_DECK and self.due("toggle", FALLBACK["toggle"]):
            self.send_dstate(self.refresh_toggles(lambda b, i, btn: b == self.bank_shown and not self.watched(btn)))
        if self.mqtt and self.mqtt.error and not self.mqtt_warned:
            self.mqtt_warned = True
            self.send({"t": "toast", "msg": "MQTT buttons offline", "level": "bad"})

        # Inherently sampled data
        self.mixer.meters_wanted(p == PAGE_MIX)
        self.mixer.push_strip_ids()
        if p == PAGE_MIX and self.due("lvl", 0.2):
            lv = self.mixer.take_levels()
            if lv and lv != self.last_lvl:
                self.last_lvl = lv
                self.send({"t": "lvl", "v": lv})
        if self.mixer.notice:
            self.send({"t": "toast", "msg": self.mixer.notice, "level": "bad"})
            self.mixer.notice = ""
        if self.due("sys", SYS_VISIBLE if p == PAGE_SYS else SYS_HIDDEN):
            self.send(self.sys.sample())
        if p == PAGE_SYS and self.due("procs", PROCS_VISIBLE):
            self.send(self.sys.top())
        if p == PAGE_DOCK and self.dock_events.alive and self.due("dockstats", DOCK_STATS_VISIBLE):
            self.send(self.docker.state(with_stats=True))

        # With nothing to report the line can go quiet; a heartbeat keeps the CYD's
        # link watchdog (6 s) from deciding deckd is gone.
        if time.monotonic() - self.last_tx > HEARTBEAT:
            self.send({"t": "hb"})

        # Config files: a stat() call, no process
        if self.due("files", 2):
            if self.deck.reload_if_changed():
                self.on_deck_loaded()
                self.send(self.deck.message())
                self.send_dstate()
            theme = detect_theme(self.cfg)
            if theme != self.theme:
                self.theme = theme
                self.send(dict(THEMES[theme], t="theme"))

    def run(self):
        while True:
            if not self.ser:
                if not self.open():
                    time.sleep(2)
                    continue
            for line in self.read_lines():
                try:
                    self.handle(line)
                except Exception:
                    # One bad message must not take the service down.
                    log.error("error handling %r\n%s", line[:200], traceback.format_exc())
            if self.ser:
                try:
                    self.tick()
                except Exception:
                    log.error("error in update\n%s", traceback.format_exc())
                    time.sleep(1)
            time.sleep(0.02)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--meters":
        meter_helper()
        return
    logging.basicConfig(level=os.environ.get("DECKD_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if len(sys.argv) > 2 and sys.argv[1] == "--port":
        cfg["port"] = sys.argv[2]
    os.environ.setdefault("DISPLAY", ":0")  # so deck buttons can open windows
    DeckD(cfg).run()


if __name__ == "__main__":
    main()
