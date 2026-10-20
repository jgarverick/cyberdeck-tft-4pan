#!/usr/bin/env python3
"""deckd: the Pi side of the cyd-deck control surface.

Talks to the CYD over USB serial (one JSON object per line) and feeds its four pages:
  0 MIX   PipeWire/PulseAudio mixer (pulsectl)
  1 DECK  buttons from ~/.config/deckd/deck.json
  2 SYS   psutil system stats and top processes
  3 DOCK  Docker Engine API over /var/run/docker.sock

Only the page that's on screen is polled quickly; the others refresh slowly so the
CYD's alert LED stays meaningful. See PROTOCOL.md for the message format.
"""
import glob, http.client, json, logging, os, signal, socket, subprocess, sys, threading, time
from pathlib import Path

import psutil
import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from themes import THEMES

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
        self._meter_thread = None
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

    # Level meters run in their own thread with their own connection, because
    # get_peak_sample blocks briefly and pulsectl connections aren't thread-safe.
    def start_meters(self, active):
        if not self.cfg["meters"] or pulsectl is None or self._meter_thread:
            return
        self._meter_thread = threading.Thread(target=self._meter_loop, args=(active,), daemon=True)
        self._meter_thread.start()

    def _meter_loop(self, active):
        pulse = None
        while True:
            if not active():
                time.sleep(0.3)
                continue
            try:
                if pulse is None:
                    pulse = pulsectl.Pulse("deckd-meters")
                info = pulse.server_info()
                sinks = {s.name: s for s in pulse.sink_list()}
                with self._lock:
                    ids = list(self.strip_ids)
                out = []
                for sid in ids:
                    kind, _, key = sid.partition(":")
                    try:
                        if kind == "sink" and key in sinks:
                            peak = pulse.get_peak_sample(sinks[key].monitor_source_name, 0.08)
                        elif kind == "in":
                            si = next((s for s in pulse.sink_input_list() if str(s.index) == key), None)
                            sink = next((s for s in sinks.values() if si and s.index == si.sink), None)
                            peak = pulse.get_peak_sample(sink.monitor_source_name, 0.08, int(key)) if sink else 0
                        elif kind == "src":
                            peak = pulse.get_peak_sample(key, 0.08)
                        else:
                            peak = 0
                    except Exception:
                        peak = 0
                    out.append(int(min(1.0, peak) * 100))
                with self._lock:
                    self.levels = out
                _ = info
            except Exception as e:
                log.debug("meter loop: %s", e)
                try:
                    if pulse:
                        pulse.close()
                except Exception:
                    pass
                pulse = None
                time.sleep(2)

    def take_levels(self):
        with self._lock:
            return list(self.levels)


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
    def __init__(self):
        self.path = CFG_DIR / "deck.json"
        self.mtime = 0
        self.banks = []

    def reload_if_changed(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
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
        return True

    def message(self):
        keep = ("label", "icon", "color", "type", "page", "bank")
        return {"t": "deck", "banks": [
            {"name": b.get("name", "BANK")[:10],
             "buttons": [{k: v for k, v in btn.items() if k in keep} for btn in b.get("buttons", [])[:12]]}
            for b in self.banks]}

    def toggle_states(self, bank):
        if not 0 <= bank < len(self.banks):
            return None
        on = []
        for btn in self.banks[bank].get("buttons", [])[:12]:
            if btn.get("type") == "toggle" and btn.get("state"):
                code, _ = run_shell(btn["state"], timeout=3)
                on.append(code == 0)
            else:
                on.append(False)
        return {"t": "dstate", "bank": bank, "on": on}

    def press(self, bank, idx):
        try:
            btn = self.banks[bank]["buttons"][idx]
        except (IndexError, KeyError):
            return None
        label = btn.get("label", "button")
        if btn.get("type") == "toggle":
            code, _ = run_shell(btn.get("state", "false"), timeout=3)
            cmd = btn.get("off") if code == 0 else btn.get("on")
            if cmd:
                rc, out = run_shell(cmd, timeout=15)
                return (f"{label}: {'off' if code == 0 else 'on'}", "ok") if rc == 0 else (f"{label} failed: {out[:40]}", "bad")
            return None
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

class DeckD:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ser = None
        self.page = 0
        self.theme = detect_theme(cfg)
        self.mixer = Mixer(cfg)
        self.sys = SystemStats()
        self.docker = Docker(cfg["docker_socket"])
        self.deck = Deck()
        self.deck.reload_if_changed()
        self.dock_sel = ""
        self.next_due = {}
        self.rx = b""
        self.bank_shown = 0
        self.last_full = 0.0

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
        s.dtr = False  # don't hold the ESP32 in reset or boot mode
        s.rts = False
        try:
            s.open()
        except (OSError, serial.SerialException) as e:
            log.warning("can't open %s: %s", port, e)
            return False
        self.ser = s
        self.last_full = 0.0
        log.info("connected to %s", port)
        self.send({"t": "hello"})
        return True

    def send(self, msg):
        if not self.ser:
            return
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
        st = self.deck.toggle_states(self.bank_shown)
        if st:
            self.send(st)
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
            log.info("CYD ready (fw %s, %s KB free)", m.get("fw"), int(m.get("heap", 0)) // 1024)
            self.last_full = time.monotonic()
            self.send_all(with_theme=True)
        elif t == "resync":
            self.send_all(with_theme=False)
        elif t == "page":
            self.page = int(m.get("p", 0))
            self.next_due.clear()
        elif t == "bank":
            self.bank_shown = int(m.get("b", 0))
            st = self.deck.toggle_states(self.bank_shown)
            if st:
                self.send(st)
        elif t == "vol":
            self.mixer.set_volume(m.get("id", ""), m.get("v", 0))
            self.next_due["mix"] = time.monotonic() + 0.4
        elif t == "mute":
            self.mixer.toggle_mute(m.get("id", ""))
            self.send(self.mixer.state())
        elif t == "out":
            name = self.mixer.set_output(int(m.get("i", 0)))
            if name:
                self.send({"t": "toast", "msg": f"Output: {name[:30]}", "level": "ok"})
            self.send(self.mixer.state())
        elif t == "btn":
            bank, idx = int(m.get("bank", 0)), int(m.get("i", 0))
            self.bank_shown = bank
            result = self.deck.press(bank, idx)
            if result:
                self.send({"t": "toast", "msg": result[0][:48], "level": result[1]})
            st = self.deck.toggle_states(bank)
            if st:
                self.send(st)
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
            self.next_due["dock"] = 0
        elif t == "dsel":
            self.dock_sel = m.get("id", "")
            self.next_due["logs"] = 0

    # -- periodic work
    def due(self, key, interval):
        now = time.monotonic()
        if now >= self.next_due.get(key, 0):
            self.next_due[key] = now + interval
            return True
        return False

    def tick(self):
        p = self.page
        if self.due("mix", 0.5 if p == PAGE_MIX else 4):
            self.send(self.mixer.state())
        if p == PAGE_MIX and self.cfg["meters"] and self.due("lvl", 0.1):
            lv = self.mixer.take_levels()
            if lv:
                self.send({"t": "lvl", "v": lv})
        if self.due("sys", 1 if p == PAGE_SYS else 5):
            self.send(self.sys.sample())
        if p == PAGE_SYS and self.due("procs", 2):
            self.send(self.sys.top())
        if self.due("dock", 3 if p == PAGE_DOCK else 15):
            self.send(self.docker.state(with_stats=(p == PAGE_DOCK)))
        if p == PAGE_DOCK and self.dock_sel and self.due("logs", 3):
            try:
                self.send({"t": "logs", "id": self.dock_sel, "lines": self.docker.logs(self.dock_sel)})
            except (OSError, RuntimeError) as e:
                self.send({"t": "logs", "id": self.dock_sel, "lines": f"(logs unavailable: {e})"})
        if p == PAGE_DECK and self.due("dstate", 2):
            st = self.deck.toggle_states(self.bank_shown)
            if st:
                self.send(st)
        if self.due("files", 2):
            if self.deck.reload_if_changed():
                self.send(self.deck.message())
            theme = detect_theme(self.cfg)
            if theme != self.theme:
                self.theme = theme
                self.send(dict(THEMES[theme], t="theme"))

    def run(self):
        self.mixer.start_meters(lambda: self.page == PAGE_MIX and self.ser is not None)
        while True:
            if not self.ser:
                if not self.open():
                    time.sleep(2)
                    continue
            for line in self.read_lines():
                self.handle(line)
            if self.ser:
                self.tick()
            time.sleep(0.02)


def main():
    logging.basicConfig(level=os.environ.get("DECKD_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if len(sys.argv) > 2 and sys.argv[1] == "--port":
        cfg["port"] = sys.argv[2]
    os.environ.setdefault("DISPLAY", ":0")  # so deck buttons can open windows
    DeckD(cfg).run()


if __name__ == "__main__":
    main()
