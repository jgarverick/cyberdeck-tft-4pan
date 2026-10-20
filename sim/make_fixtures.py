#!/usr/bin/env python3
"""Generate simulator scripts that render every page in both themes."""
import json, math, random, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deckd"))
from themes import THEMES  # same palettes deckd sends

random.seed(4)
OUT = sys.argv[1] if len(sys.argv) > 1 else "shots"

DECK = {"t": "deck", "banks": [
    {"name": "MAIN", "buttons": [
        {"label": "Terminal", "icon": "terminal"},
        {"label": "Browser", "icon": "eye"},
        {"label": "Files", "icon": "folder"},
        {"label": "Keys", "icon": "keyboard", "color": "accent2"},
        {"label": "Bluetooth", "icon": "bluetooth", "type": "toggle"},
        {"label": "Wi-Fi", "icon": "wifi", "type": "toggle"},
        {"label": "Do not disturb", "icon": "bell", "type": "toggle"},
        {"label": "Screenshot", "icon": "image"},
        {"label": "Lock", "icon": "eye_off"},
        {"label": "Theme swap", "icon": "shuffle", "color": "hi"},
        {"label": "Reboot", "icon": "refresh", "type": "confirm"},
        {"label": "Shut down", "icon": "power", "type": "confirm"}]},
    {"name": "LAB", "buttons": [{"label": "SSH node1", "icon": "SSH"}]},
    {"name": "MEDIA", "buttons": [{"label": "Play/Pause", "icon": "play"}]},
]}

MIX = {"t": "mix", "out": ["Built-in audio (HDMI)", "WH-1000XM5 (Bluetooth)", "USB DAC"], "out_sel": 1,
       "strips": [
           {"id": "sink:main", "name": "MASTER", "vol": 72, "mute": False},
           {"id": "in:1", "name": "Firefox", "vol": 100, "mute": False},
           {"id": "in:2", "name": "Spotify", "vol": 58, "mute": False},
           {"id": "in:3", "name": "Discord", "vol": 80, "mute": True},
           {"id": "src:mic", "name": "MIC", "vol": 64, "mute": False}]}

PROCS = {"t": "procs", "p": [
    {"pid": 711, "name": "Xorg", "cpu": 6.1, "mem": 2.1},
    {"pid": 2311, "name": "picom", "cpu": 3.4, "mem": 0.8},
    {"pid": 1401, "name": "dockerd", "cpu": 2.2, "mem": 1.9},
    {"pid": 1263, "name": "python3 deckd.py", "cpu": 1.1, "mem": 0.6},
    {"pid": 480, "name": "alacritty", "cpu": 0.9, "mem": 1.2}]}

DOCK = {"t": "dock", "ok": True, "c": [
    {"id": "a1b2c3d4e5f6", "name": "pihole", "state": "running", "status": "Up 3 hours (healthy)",
     "cpu": 1.2, "mem": "84M", "health": "healthy"},
    {"id": "b2c3d4e5f6a1", "name": "homeassistant", "state": "running", "status": "Up 3 hours",
     "cpu": 4.8, "mem": "312M", "health": ""},
    {"id": "c3d4e5f6a1b2", "name": "mosquitto", "state": "running", "status": "Up 3 hours",
     "cpu": 0.1, "mem": "6M", "health": ""},
    {"id": "d4e5f6a1b2c3", "name": "readsb-adsb", "state": "running", "status": "Up 2 hours (unhealthy)",
     "cpu": 7.9, "mem": "41M", "health": "unhealthy"},
    {"id": "e5f6a1b2c3d4", "name": "minecraft-bedrock", "state": "exited", "status": "Exited (0) 2 days ago",
     "cpu": 0, "mem": "", "health": ""}]}

LOGS = {"t": "logs", "id": "d4e5f6a1b2c3", "lines": "\n".join([
    "2026-10-19 21:31:02 [readsb] Using rtlsdr device 0",
    "2026-10-19 21:31:02 [readsb] Gain set to 49.6 dB",
    "2026-10-19 21:31:44 [readsb] Lost 412 samples",
    "2026-10-19 21:32:10 [health] decoder rate below threshold",
    "2026-10-19 21:32:40 [readsb] 38 aircraft, 1241 msgs/s",
    "2026-10-19 21:33:10 [health] check failed (exit 1)",
    "2026-10-19 21:33:40 [readsb] 41 aircraft, 1302 msgs/s"])}

TAB_Y = 303
TABS = [60, 180, 300, 420]


def sys_msg(i):
    load = 22 + 18 * math.sin(i / 6) + random.uniform(-6, 6) + (40 if 38 < i < 46 else 0)
    load = max(2, min(98, load))
    cores = [max(0, min(100, int(load + random.uniform(-15, 15)))) for _ in range(4)]
    return {"t": "sys", "cpu": int(load), "cores": cores, "mem": 41, "mem_used": "3.1G",
            "mem_total": "7.6G", "disk": 23, "temp": 61.4, "throttled": False,
            "up": "18.2K", "down": "1.4M", "uptime": "3h41m", "freq": 1800}


def script(theme):
    th = THEMES[theme]
    lines = [json.dumps(dict(th, t="theme")), "tick 100", json.dumps(DECK),
             json.dumps({"t": "dstate", "bank": 0, "on": [0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0]}),
             json.dumps(MIX), json.dumps({"t": "lvl", "v": [64, 71, 38, 0, 22]}),
             json.dumps(PROCS), json.dumps(DOCK)]
    for i in range(60):
        lines += [json.dumps(sys_msg(i)), "tick 20"]
    shots = os.path.join(OUT, theme)
    lines += [f"touch {TABS[0]} {TAB_Y}", "tick 300", f"snap {shots}-1-mix.ppm",
              f"touch {TABS[1]} {TAB_Y}", "tick 300", f"snap {shots}-2-deck.ppm",
              f"touch {TABS[2]} {TAB_Y}", "tick 300", f"snap {shots}-3-sys.ppm",
              f"touch {TABS[3]} {TAB_Y}", "tick 300", f"snap {shots}-4-dock.ppm",
              "touch 120 202", "tick 200", json.dumps(LOGS), "tick 300", f"snap {shots}-5-dock-detail.ppm",
              "touch 270 77", "tick 300", f"snap {shots}-6-hold-hint.ppm"]
    return "\n".join(lines) + "\n"


os.makedirs(OUT, exist_ok=True)
for name in THEMES:
    with open(os.path.join(OUT, f"{name}.txt"), "w") as fh:
        fh.write(script(name))
print("wrote scripts for", ", ".join(THEMES))
