#!/usr/bin/env python3
"""deck-keys: show your keybindings in a searchable rofi menu (like Omarchy's Super+K).

Reads the bindings for the session you're in:
  i3       ~/.config/i3/config (and anything it includes)
  Openbox  ~/.config/openbox/rc.xml
  Plasma   ~/.config/kglobalshortcutsrc
  sxhkd    ~/.config/sxhkd/sxhkdrc (added to any session where sxhkd is running)

Pick a row and press Enter to run it (i3 and Openbox launch commands).

Usage:
  deck-keys                 show the menu for the current session
  deck-keys --print         print the list to the terminal instead
  deck-keys --session i3    force a session: i3 | openbox | plasma
  deck-keys --install       copy to ~/.local/bin and bind Super+? in i3 and Openbox

Descriptions are generated from each command. To set your own, put a
comment ending in the description on the line above a binding in the i3
config or sxhkdrc:
    ## Open the file manager
    bindsym $mod+e exec thunar
"""
import argparse, configparser, glob, os, re, shlex, shutil, subprocess, sys
import xml.etree.ElementTree as ET

HOME = os.path.expanduser("~")
CFG = os.path.join(HOME, ".config")

# --------------------------------------------------------------------------- formatting

MOD_NAMES = {
    "mod4": "Super", "super": "Super", "meta": "Super",
    "mod1": "Alt", "alt": "Alt",
    "control": "Ctrl", "ctrl": "Ctrl",
    "shift": "Shift",
    "mod5": "AltGr", "mod3": "Hyper",
}
# Openbox writes modifiers as single letters (W-S-q)
OB_MODS = dict(MOD_NAMES, w="Super", a="Alt", c="Ctrl", s="Shift", h="Hyper", m="Alt")
KEY_NAMES = {
    "return": "Enter", "escape": "Esc", "space": "Space", "tab": "Tab",
    "minus": "-", "equal": "=", "plus": "+", "slash": "/", "question": "?",
    "backspace": "Backspace", "delete": "Delete", "print": "PrtSc",
    "left": "←", "right": "→", "up": "↑", "down": "↓",
    "prior": "PgUp", "next": "PgDn", "page_up": "PgUp", "page_down": "PgDn",
    "home": "Home", "end": "End", "grave": "`",
    "xf86audioraisevolume": "Vol+", "xf86audiolowervolume": "Vol-",
    "xf86audiomute": "Mute", "xf86monbrightnessup": "Bright+",
    "xf86monbrightnessdown": "Bright-",
}
MOD_ORDER = ["Super", "Ctrl", "Alt", "AltGr", "Hyper", "Shift"]


def pretty_key(parts, mods):
    """parts: list of key tokens; returns 'Super+Shift+Q' style text."""
    found_mods, keys = [], []
    parts = [p for p in parts if p.strip()]
    for i, p in enumerate(parts):
        lp = p.strip().lower()
        # The last token is always the key itself, even if it looks like a modifier
        if i < len(parts) - 1 and lp in mods:
            found_mods.append(mods[lp])
        else:
            k = KEY_NAMES.get(lp, p.strip())
            keys.append(k.upper() if len(k) == 1 else k)
    found_mods.sort(key=lambda m: MOD_ORDER.index(m) if m in MOD_ORDER else 99)
    if keys == ["/"] and "Shift" in found_mods:  # Shift+/ is the ? key
        found_mods.remove("Shift")
        keys = ["?"]
    return "+".join(found_mods + keys)


def describe(cmd):
    """Turn a WM command into a short human description."""
    c = cmd.strip()
    lc = c.lower()
    exact = {
        "kill": "Close window",
        "fullscreen toggle": "Toggle fullscreen",
        "floating toggle": "Toggle floating",
        "focus mode_toggle": "Toggle focus: tiled / floating",
        "split h": "Split horizontally",
        "split v": "Split vertically",
        "layout stacking": "Layout: stacking",
        "layout tabbed": "Layout: tabbed",
        "layout toggle split": "Layout: toggle split",
        "reload": "Reload config",
        "restart": "Restart window manager",
        "move scratchpad": "Minimize to scratchpad",
        "scratchpad show": "Show scratchpad",
    }
    if lc in exact:
        return exact[lc]
    m = re.match(r"focus (left|right|up|down)$", lc)
    if m:
        return f"Focus {m.group(1)}"
    m = re.match(r"move (left|right|up|down)$", lc)
    if m:
        return f"Move window {m.group(1)}"
    m = re.match(r'mode "?([^"]+)"?$', c)
    if m:
        return f"Enter {m.group(1)} mode" if m.group(1) != "default" else "Exit mode"
    m = re.match(r"workspace (?:number )?(.+)$", c)
    if m:
        return f"Go to workspace {m.group(1).strip()}"
    m = re.match(r"move container to workspace (?:number )?(.+)$", c)
    if m:
        return f"Move window to workspace {m.group(1).strip()}"
    m = re.match(r"resize (shrink|grow) (width|height)", lc)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}"
    if lc.startswith("exec"):
        return describe_exec(re.sub(r"^exec(_always)?\s+(--no-startup-id\s+)?", "", c))
    return c


def describe_exec(cmd):
    lc = cmd.lower()
    rules = [
        (r"i3-nagbar.*exit|i3-msg exit", "Exit i3 / log out"),
        (r"^alacritty -e btop|btop", "System monitor"),
        (r"^alacritty|^kitty|^xterm|^konsole|^foot", "Terminal"),
        (r"cool-retro-term", "Retro CRT terminal"),
        (r"rofi -show drun", "App launcher"),
        (r"rofi -show window", "Window switcher"),
        (r"rofi -show run", "Run a command"),
        (r"deck-keys", "Show keybindings"),
        (r"i3lock|xsecurelock|loginctl lock", "Lock screen"),
        (r"quips\.txt", "Status update (random quip)"),
        (r"blueman", "Bluetooth manager"),
        (r"pavucontrol|wpctl|pactl", "Audio"),
        (r"scrot|maim|flameshot", "Screenshot"),
    ]
    for pat, text in rules:
        if re.search(pat, lc):
            return text
    prog = shlex.split(cmd)[0] if cmd.strip() else cmd
    return f"Run {os.path.basename(prog)}"


# --------------------------------------------------------------------------- parsers

def parse_i3(path):
    rows, variables, seen = [], {}, set()
    mods = dict(MOD_NAMES)

    def read(p, out):
        p = os.path.expanduser(p)
        for f in sorted(glob.glob(p)):
            if f in seen:
                continue
            seen.add(f)
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    s = line.strip()
                    m = re.match(r"include\s+(.+)$", s)
                    if m:
                        read(m.group(1).strip().strip('"'), out)
                        continue
                    out.append(s)

    lines = []
    if not os.path.exists(path):
        return rows
    read(path, lines)

    group, note, depth_mode = "General", None, []
    for s in lines:
        if not s:
            note = None
            continue
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            if s.startswith("##") and text:
                note = text
            continue
        m = re.match(r"set\s+(\$\S+)\s+(.+)$", s)
        if m:
            variables[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r'mode\s+"?([^"{]+)"?\s*\{', s)
        if m:
            depth_mode.append(m.group(1).strip())
            continue
        if s == "}" and depth_mode:
            depth_mode.pop()
            continue
        m = re.match(r"bind(sym|code)\s+((?:--\S+\s+)*)(\S+)\s+(.+)$", s)
        if not m:
            continue
        combo, cmd = m.group(3), m.group(4)
        for var, val in sorted(variables.items(), key=lambda kv: -len(kv[0])):
            combo = combo.replace(var, val)
            cmd = cmd.replace(var, val.strip('"'))
        key = pretty_key(combo.split("+"), mods)
        if m.group(1) == "code":
            key += " (keycode)"
        grp = f"{depth_mode[-1]} mode" if depth_mode else group_for(cmd)
        rows.append(dict(group=grp, key=key, desc=note or describe(cmd),
                         run=("i3", cmd)))
        note = None
    return rows


def group_for(cmd):
    lc = cmd.lower()
    if lc.startswith("exec"):
        return "Launch"
    if "workspace" in lc:
        return "Workspaces"
    if lc.startswith(("focus", "move", "kill", "fullscreen", "floating", "split", "layout", "resize", "scratchpad")):
        return "Windows"
    if lc.startswith("mode"):
        return "Modes"
    return "Session"


OB_ACTIONS = {
    "Close": ("Windows", "Close window"),
    "Iconify": ("Windows", "Minimize window"),
    "ToggleMaximize": ("Windows", "Maximize / restore"),
    "UnmaximizeFull": ("Windows", None),
    "ToggleFullscreen": ("Windows", "Toggle fullscreen"),
    "NextWindow": ("Windows", "Next window"),
    "PreviousWindow": ("Windows", "Previous window"),
    "Reconfigure": ("Session", "Reload Openbox"),
    "Exit": ("Session", "Log out"),
}


def parse_openbox(path):
    rows = []
    if not os.path.exists(path):
        return rows
    ns = {"ob": "http://openbox.org/3.4/rc"}
    root = ET.parse(path).getroot()
    mods = OB_MODS
    for kb in root.iterfind(".//ob:keyboard/ob:keybind", ns):
        combo = kb.get("key", "")
        key = pretty_key(combo.split("-"), mods)
        actions = kb.findall("ob:action", ns)
        if not actions:
            continue
        names = [a.get("name") for a in actions]
        first = actions[0]
        name = first.get("name")
        run = None
        if name == "Execute":
            cmd = (first.findtext("ob:command", default="", namespaces=ns)
                   or first.findtext("ob:execute", default="", namespaces=ns)).strip()
            grp, desc, run = "Launch", describe_exec(cmd), ("exec", cmd)
        elif name in ("GoToDesktop", "SendToDesktop"):
            to = first.findtext("ob:to", default="?", namespaces=ns)
            grp = "Workspaces"
            desc = f"Go to desktop {to}" if name == "GoToDesktop" else f"Move window to desktop {to}"
        elif name == "ShowMenu":
            menu = first.findtext("ob:menu", default="", namespaces=ns)
            grp = "Session"
            desc = {"power-menu": "Power menu", "root-menu": "Desktop menu",
                    "client-list-combined-menu": "Window list"}.get(menu, f"Show {menu}")
        elif "MoveResizeTo" in names:
            x = kb.find("ob:action[@name='MoveResizeTo']/ob:x", ns)
            w = kb.find("ob:action[@name='MoveResizeTo']/ob:width", ns)
            xv = x.text if x is not None else ""
            grp = "Windows"
            if w is not None and w.text == "50%":
                desc = "Snap left half" if xv == "0" else "Snap right half"
            else:
                desc = "Center window"
        else:
            grp, desc = OB_ACTIONS.get(name, ("Other", name))
            desc = desc or name
        rows.append(dict(group=grp, key=key, desc=desc, run=run))
    return rows


def parse_plasma(path):
    rows = []
    if not os.path.exists(path):
        return rows
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    with open(path, encoding="utf-8", errors="replace") as fh:
        cp.read_file(fh)
    for section in cp.sections():
        if section.startswith("$"):
            continue
        comp = cp.get(section, "_k_friendly_name", fallback=section)
        comp = re.sub(r"\.desktop$", "", comp)
        for action, value in cp.items(section):
            if action.startswith("_k_"):
                continue
            parts = value.split(",")
            current = parts[0].strip() if parts else ""
            friendly = parts[2].strip() if len(parts) > 2 else action
            if not current or current.lower() == "none":
                continue
            for combo in re.split(r"\t|\\t", current):
                combo = combo.strip()
                if not combo or combo.lower() == "none":
                    continue
                key = pretty_key(combo.split("+"), MOD_NAMES)
                rows.append(dict(group="Windows" if comp.lower() == "kwin" else comp,
                                 key=key, desc=friendly or action, run=None))
    return rows


def parse_sxhkd(path):
    rows = []
    if not os.path.exists(path):
        return rows
    note, combo = None, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.rstrip("\n")
            if not s.strip():
                note = None
                continue
            if s.lstrip().startswith("#"):
                text = s.lstrip("# ").strip()
                if s.lstrip().startswith("##") and text:
                    note = text
                continue
            if not s.startswith((" ", "\t")):
                combo = s.strip()
                continue
            if combo:
                cmd = s.strip()
                key = pretty_key(re.split(r"\s*\+\s*", combo), MOD_NAMES)
                rows.append(dict(group="Launch (sxhkd)", key=key,
                                 desc=note or describe_exec(cmd), run=("exec", cmd)))
                combo, note = None, None
    return rows


# --------------------------------------------------------------------------- session

def running(name):
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


def detect_session():
    desk = (os.environ.get("XDG_CURRENT_DESKTOP", "") + os.environ.get("DESKTOP_SESSION", "")).lower()
    if "kde" in desk or "plasma" in desk:
        return "plasma"
    if os.environ.get("I3SOCK") or "i3" in desk or running("i3"):
        return "i3"
    if "openbox" in desk or running("openbox"):
        return "openbox"
    return "i3"


def collect(session):
    if session == "i3":
        rows = parse_i3(os.path.join(CFG, "i3", "config"))
    elif session == "openbox":
        rows = parse_openbox(os.path.join(CFG, "openbox", "rc.xml"))
    else:
        rows = parse_plasma(os.path.join(CFG, "kglobalshortcutsrc"))
    if running("sxhkd"):
        rows += parse_sxhkd(os.path.join(CFG, "sxhkd", "sxhkdrc"))
    order = ["Launch", "Launch (sxhkd)", "Windows", "Workspaces", "Modes", "Session"]
    rows.sort(key=lambda r: (order.index(r["group"]) if r["group"] in order else 50 + (r["group"] == "Other"),
                             r["group"]))
    return rows


# --------------------------------------------------------------------------- output

def theme_colors():
    rofi_cfg = os.path.join(CFG, "rofi", "config.rasi")
    text = open(rofi_cfg).read() if os.path.exists(rofi_cfg) else ""
    if "cmp.rasi" in text:
        return dict(key="#EE9C23", group="#6e6578", title="CONTROLS // ALL PARTIES MUST SIGN OFF")
    return dict(key="#05d9e8", group="#5a5a78", title="KEYBINDINGS")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def show_rofi(rows, session):
    colors = theme_colors()
    width = max((len(r["key"]) for r in rows), default=10) + 2
    lines = []
    for r in rows:
        lines.append(
            f"<span foreground='{colors['key']}'><b>{esc(r['key'].ljust(width))}</b></span>"
            f"{esc(r['desc'])}  <span foreground='{colors['group']}' size='small'>{esc(r['group'])}</span>")
    proc = subprocess.run(
        ["rofi", "-dmenu", "-i", "-markup-rows", "-format", "i",
         "-p", "KEYS", "-mesg", f"{colors['title']}  ·  {session}  ·  Enter runs the selected binding",
         "-theme-str", "window { width: 52%; } listview { lines: 14; } "
         "mainbox { children: [ inputbar, message, listview ]; } "
         f"message {{ padding: 8px 12px 0; }} textbox {{ text-color: {colors['group']}; }}"],
        input="\n".join(lines), capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return
    row = rows[int(proc.stdout.strip())]
    run = row.get("run")
    if not run:
        return
    kind, cmd = run
    if kind == "i3":
        subprocess.Popen(["i3-msg", cmd], stdout=subprocess.DEVNULL)
    else:
        subprocess.Popen(["sh", "-c", cmd], start_new_session=True)


def print_rows(rows):
    width = max((len(r["key"]) for r in rows), default=10) + 2
    current = None
    for r in rows:
        if r["group"] != current:
            current = r["group"]
            print(f"\n{current}")
        print(f"  {r['key'].ljust(width)}{r['desc']}")


# --------------------------------------------------------------------------- install

def install():
    target_dir = os.path.join(HOME, ".local", "bin")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, "deck-keys")
    me = os.path.realpath(__file__)
    if os.path.realpath(target) != me:
        shutil.copy2(me, target)
    os.chmod(target, 0o755)
    print(f"Installed {target}")

    i3cfg = os.path.join(CFG, "i3", "config")
    if os.path.exists(i3cfg):
        text = open(i3cfg).read()
        if "deck-keys" not in text:
            with open(i3cfg, "a") as fh:
                fh.write("\n## Show keybindings\nbindsym $mod+Shift+slash exec --no-startup-id ~/.local/bin/deck-keys\n")
            print("Bound Super+? in i3 (reload with Super+Shift+C)")

    obcfg = os.path.join(CFG, "openbox", "rc.xml")
    if os.path.exists(obcfg):
        text = open(obcfg).read()
        if "deck-keys" not in text and "</keyboard>" in text:
            bind = ('    <keybind key="W-S-slash"><action name="Execute">'
                    '<command>~/.local/bin/deck-keys</command></action></keybind>\n  </keyboard>')
            with open(obcfg, "w") as fh:
                fh.write(text.replace("  </keyboard>", bind, 1) if "  </keyboard>" in text
                         else text.replace("</keyboard>", bind, 1))
            subprocess.run(["openbox", "--reconfigure"], capture_output=True)
            print("Bound Super+? in Openbox")

    print("Plasma: add it under System Settings > Keyboard > Shortcuts > Add New > "
          "Command or Script, with the command ~/.local/bin/deck-keys")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Show your keybindings in a rofi menu.")
    ap.add_argument("--print", action="store_true", help="print to the terminal instead of rofi")
    ap.add_argument("--session", choices=["i3", "openbox", "plasma"])
    ap.add_argument("--install", action="store_true", help="install and bind Super+?")
    args = ap.parse_args()

    if args.install:
        install()
        return
    session = args.session or detect_session()
    rows = collect(session)
    if not rows:
        print(f"No keybindings found for the {session} session.", file=sys.stderr)
        sys.exit(1)
    if args.print or not shutil.which("rofi") or not os.environ.get("DISPLAY"):
        print_rows(rows)
    else:
        show_rofi(rows, session)


if __name__ == "__main__":
    main()
