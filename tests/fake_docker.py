"""Tiny Docker Engine API stand-in on a unix socket, for the end-to-end test.

Supports: container list, one-shot stats, logs (plain and follow=1), start/stop/restart,
and a streaming /events endpoint that announces each action. Every request is logged so
the test can prove what was (and wasn't) polled.
"""
import json, os, queue, socketserver, sys, threading, time
from http.server import BaseHTTPRequestHandler

SOCK = sys.argv[1]
LOG = sys.argv[2]
START = time.time()
SUBSCRIBERS = []
LOCK = threading.Lock()

CONTAINERS = [
    {"Id": "a1b2c3d4e5f60000", "Names": ["/pihole"], "State": "running", "Status": "Up 3 hours (healthy)"},
    {"Id": "d4e5f6a1b2c30000", "Names": ["/readsb-adsb"], "State": "running", "Status": "Up 2 hours (unhealthy)"},
    {"Id": "e5f6a1b2c3d40000", "Names": ["/minecraft-bedrock"], "State": "exited", "Status": "Exited (0) 2 days ago"},
]


def record(**kw):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(dict(kw, t=round(time.time() - START, 2))) + "\n")


def frame(text):
    data = text.encode()
    return bytes([1, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def address_string(self):
        return "unix"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, data):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _start_stream(self):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def do_GET(self):
        p = self.path
        record(op="GET", path=p.split("?")[0], follow="follow=1" in p)
        if p.startswith("/events"):
            q = queue.Queue()
            with LOCK:
                SUBSCRIBERS.append(q)
            self._start_stream()
            try:
                while True:
                    ev = q.get()
                    self._chunk((json.dumps(ev) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with LOCK:
                    SUBSCRIBERS.remove(q)
            return
        if p.startswith("/containers/json"):
            return self._json(CONTAINERS)
        if "/stats" in p:
            t = time.time() - START
            return self._json({"cpu_stats": {"cpu_usage": {"total_usage": int(t * 4e7)},
                                             "system_cpu_usage": int(t * 4e9), "online_cpus": 4},
                               "memory_stats": {"usage": 90 * 2**20, "stats": {"inactive_file": 6 * 2**20}}})
        if "/logs" in p:
            first = b"".join(frame(f"21:3{i}:00 [readsb] line {i}\n") for i in range(3))
            if "follow=1" not in p:
                self.send_response(200)
                self.send_header("Content-Length", str(len(first)))
                self.end_headers()
                self.wfile.write(first)
                return
            self._start_stream()
            try:
                self._chunk(first)
                n = 0
                while True:  # a new log line every second, like a live container
                    time.sleep(1)
                    n += 1
                    self._chunk(frame(f"live line {n}\n"))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        self._json({"message": "not found"}, 404)

    def do_POST(self):
        record(op="POST", path=self.path)
        cid = self.path.split("/")[2]
        action = self.path.rsplit("/", 1)[-1]
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()
        with LOCK:
            for q in SUBSCRIBERS:
                q.put({"Type": "container", "Action": action, "Actor": {"ID": cid}})


class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if os.path.exists(SOCK):
    os.unlink(SOCK)
S(SOCK, H).serve_forever()
