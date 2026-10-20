# cyd-deck

A touchscreen control surface for the cyberdeck, running on an ESP32-3248S035R
(3.5" resistive "Cheap Yellow Display") connected to the Raspberry Pi over USB.

The CYD shows four pages, chosen with the tab bar or the BOOT button:

| Page | What it does |
|---|---|
| **MIX** | Output device selector, plus faders, mute buttons, and level meters for master, up to three apps, and the mic |
| **DECK** | A 4 x 3 Stream Deck-style button grid with banks, configured in `deck.json` on the Pi |
| **SYS** | btop-style CPU history, per-core bars, memory, disk, temperature, network, and the top 5 processes (tap one to end it) |
| **DOCK** | Docker containers with state, CPU, and memory; tap one for start, stop, restart, and recent logs |

The colors follow your desktop theme (neon or CMP) and switch automatically when the theme changes.

## Project layout

| Folder | Contents |
|---|---|
| `firmware/` | PlatformIO project for the CYD (LVGL 8.3, LovyanGFX, ArduinoJson) |
| `deckd/` | `deckd`, the Python service on the Pi, with `deck.json`, a systemd unit, and an installer |
| `sim/` | A desktop simulator that runs the real `ui.cpp`, for screenshots and testing without the board |
| `tests/` | An end-to-end test: `deckd` and the simulator over a virtual serial cable, with fake PipeWire and Docker |

## Set up the CYD

1. Open the `firmware` folder in VS Code with the PlatformIO extension.
2. Connect the CYD over USB and select **Upload** in the PlatformIO toolbar.
3. On first boot, the board asks you to calibrate the touchscreen. Touch the arrow in each
   corner with a stylus or fingernail. The calibration is saved to the board.

To recalibrate later, hold the **BOOT** button while you power on the board.

If the picture is mirrored or the colors look wrong (red and blue swapped), change
`LCD_ROTATION`, `LCD_INVERT`, or `LCD_RGB_ORDER` near the top of `firmware/src/main.cpp`
and upload again.

## Set up the Pi

1. Copy the `deckd` folder to the Pi.
2. Run the installer as your normal user, not with sudo:

   ```bash
   bash deckd/install.sh
   ```

   It installs the dependencies, adds you to the `dialout` and `docker` groups, and enables
   `deckd` as a user service.

3. Log out and back in so the new group memberships take effect.
4. Plug in the CYD. Within a few seconds the **NO LINK** indicator disappears and the pages fill in.

To check on the service:

```bash
systemctl --user status deckd
journalctl --user -u deckd -f
```

## Configure the buttons

Edit `~/.config/deckd/deck.json`. The CYD picks up changes within a couple of seconds of
saving, with no reflashing. Each bank holds up to 12 buttons, and you can have up to 6 banks.

| Field | Meaning |
|---|---|
| `label` | Text on the button |
| `icon` | An icon name (`terminal`, `power`, `wifi`, `bluetooth`, `play`, `bell`, `folder`, `image`, `trash`, and so on; see `icon_symbol()` in `ui.cpp`) or a short literal like `"SSH"` |
| `color` | `accent`, `accent2`, `hi`, `ok`, `bad`, `dim`, or a `#rrggbb` value |
| `type` | `exec` (default), `toggle`, `confirm` (hold to run), or `page` |
| `run` | Shell command to start. Runs detached unless `wait` is `true`, in which case the CYD shows whether it succeeded |
| `i3` | An i3 command, such as `"workspace number 2"` |
| `state`, `on`, `off` | For toggles: `state` exits with 0 when the toggle is on |
| `page` or `bank` | For `page` buttons: the page (0-3) or bank to jump to |

Example toggle:

```json
{ "label": "Wi-Fi", "icon": "wifi", "type": "toggle",
  "state": "[ \"$(nmcli radio wifi)\" = enabled ]",
  "on": "nmcli radio wifi on", "off": "nmcli radio wifi off" }
```

The default `deck.json` assumes a few things you may want to change: Chromium as the
browser, `~/stacks` as your Docker Compose folder, and `playerctl` for media keys.

## Settings

`~/.config/deckd/config.json`:

| Setting | Default | Meaning |
|---|---|---|
| `port` | `auto` | Serial port. `auto` picks the first CH340 or CP210x adapter |
| `theme` | `auto` | `auto` follows the desktop theme, or set `neon` or `cmp` |
| `brightness` | `220` | CYD backlight, 0-255 |
| `meters` | `true` | Audio level meters. Turn them off to save a little CPU |

## Run the simulator and tests

Build the firmware once first (`pio run` in `firmware/`) so PlatformIO downloads the libraries.
Then:

```bash
make -C sim
python3 sim/make_fixtures.py sim/shots        # write the sample scripts
./sim/sim sim/shots/cmp.txt                   # render every page as .ppm screenshots
bash tests/e2e.sh                             # deckd + simulator end to end
```

The serial protocol is documented in `deckd/PROTOCOL.md`.
