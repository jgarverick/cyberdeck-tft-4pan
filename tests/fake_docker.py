"""Tiny Docker Engine API stand-in on a unix socket, for the end-to-end test."""
import json, os, socketserver, sys, time
from http.server import BaseHTTPRequestHandler

SOCK = sys.argv[1]
LOG = sys.argv[2]
START = time.time()

CONTAINERS = [
    {"Id": "a1b2c3d4e5f60000", "Names": ["/pihole"], "State": "running", "Status": "Up 3 hours (healthy)"},
    {"Id": "d4e5f6a1b2c30000", "Names": ["/readsb-adsb"], "State": "running", "Status": "Up 2 hours (unhealthy)"},
    {"Id": "e5f6a1b2c3d40000", "Names": ["/minecraft-bedrock"], "State": "exited", "Status": "Exited (0) 2 days ago"},
]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        return "unix"

    def do_GET(self):
        p = self.path
        if p.startswith("/containers/json"):
            return self._json(CONTAINERS)
        if "/stats" in p:
            t = time.time() - START
            return self._json({"cpu_stats": {"cpu_usage": {"total_usage": int(t * 4e7)},
                                             "system_cpu_usage": int(t * 4e9), "online_cpus": 4},
                               "memory_stats": {"usage": 90 * 2**20, "stats": {"inactive_file": 6 * 2**20}}})
        if "/logs" in p:
            frames = b""
            for i, line in enumerate(["[readsb] 38 aircraft", "[health] check failed (exit 1)", "[readsb] 41 aircraft"]):
                data = f"2026-10-19 21:3{i}:00 {line}\n".encode()
                frames += bytes([1, 0, 0, 0]) + len(data).to_bytes(4, "big") + data
            self.send_response(200)
            self.send_header("Content-Length", str(len(frames)))
            self.end_headers()
            self.wfile.write(frames)
            return
        self._json({"message": "not found"}, 404)

    def do_POST(self):
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"op": "POST", "path": self.path}) + "\n")
        self.send_response(204)
        self.end_headers()


class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if os.path.exists(SOCK):
    os.unlink(SOCK)
S(SOCK, H).serve_forever()
