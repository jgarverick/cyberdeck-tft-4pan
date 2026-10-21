# cyd-deck serial protocol

The CYD and `deckd` exchange one JSON object per line over USB serial at 921600 baud.
Every message has a `t` field that names its type. Unknown types are ignored, so either
side can add messages without breaking the other.

## CYD to deckd

| `t` | Fields | Meaning |
|---|---|---|
| `ready` | `fw`, `w`, `h`, `page`, `heap` | The CYD booted, or it's waiting for a link. `deckd` replies with the full state. Repeats every 2 seconds until `deckd` answers. |
| `resync` | same as `ready` | The UI was rebuilt after a theme change. `deckd` resends everything except the theme. |
| `page` | `p` (0-3) | The visible page changed. `deckd` polls that page quickly. |
| `vol` | `id`, `v` (0-100) | A fader moved. Sent at most every 50 ms while dragging, plus once on release. |
| `mute` | `id` | A mute button was tapped. |
| `out` | `i` | An output device was chosen (index into the `mix.out` list). |
| `bank` | `b` | A deck bank was selected. |
| `btn` | `bank`, `i` | A deck button was pressed. Confirm buttons send this only after a hold. |
| `kill` | `pid` | End a process (SIGTERM). |
| `dsel` | `id` | A container was opened in the detail view (empty `id` when closed). |
| `dact` | `id`, `a` | Container action: `start`, `stop`, or `restart`. |

## deckd to CYD

| `t` | Fields | Meaning |
|---|---|---|
| `hello` | | Sent when `deckd` opens the port. The CYD answers with `ready`. |
| `theme` | `name`, `bg`, `panel`, `line`, `fg`, `dim`, `accent`, `accent2`, `hi`, `ok`, `bad`, `tabs`, `title` | Palette as `#rrggbb` strings and the four tab labels. The CYD rebuilds its UI only when something changed. |
| `bl` | `v` (0-255) | Backlight level. |
| `mix` | `out` (list of names), `out_sel`, `strips` (`id`, `name`, `vol`, `mute`) | Up to 5 strips: master, up to 3 apps, and the mic. |
| `lvl` | `v` (list of 0-100) | Level meters, in strip order. Sent about 10 times a second while the mixer is visible. |
| `deck` | `banks` (`name`, `buttons`: `label`, `icon`, `color`, `type`, `page`, `bank`) | Button layout from `deck.json`. Commands stay on the Pi. |
| `dstate` | `bank`, `on` (list of booleans) | Toggle states for one bank. |
| `sys` | `cpu`, `cores`, `mem`, `mem_used`, `mem_total`, `disk`, `temp`, `throttled`, `up`, `down`, `uptime`, `freq` | System stats. |
| `procs` | `p` (`pid`, `name`, `cpu`, `mem`) | Top 5 processes by CPU. |
| `dock` | `ok`, `err`, `c` (`id`, `name`, `state`, `status`, `health`, `cpu`, `mem`) | Containers. When `ok` is false, `err` explains why. |
| `logs` | `id`, `lines` | Recent log lines for the selected container. |
| `toast` | `msg`, `level` (`ok`, `bad`, or `info`) | A short message above the tab bar. |
| `page` | `p` | Switch the CYD to a page. |
| `hb` | | Heartbeat, sent after 3 seconds without other traffic so the CYD's 6-second link watchdog stays happy. |

## Where updates come from

`deckd` is event-driven wherever the Pi offers events. Each event source is a supervised
watcher in `events.py` that reconnects with a backoff; while a source is unavailable, `deckd`
falls back to slow polling for just that data.

| Data | Source | Fallback while the source is down |
|---|---|---|
| Mixer (devices, volumes, mutes, streams) | `pactl subscribe` | Poll every 5 s |
| Containers (start, stop, die, health) | Docker `/events` stream | Poll every 30 s |
| Container logs | Docker log stream (`follow=1`) while the detail view is open | None |
| Toggle buttons with `watch` | The watch command's output (for example, `gdbus monitor`) | Poll every 10 s while DECK is visible |
| Toggle buttons with `mqtt` | MQTT state topic | None (shows the last known state) |
| Toggle buttons without either | Polled every 10 s while DECK is visible, and right after a press | |
| `deck.json` and theme | File `stat()` every 2 s (no process) | |

A few things have no change events and are sampled:

| Data | Page visible | Page hidden |
|---|---|---|
| CPU, memory, temperature, network | 1 s | 10 s (keeps the alert LED current) |
| Processes | 2 s | off |
| Container CPU and memory | 3 s | off |
| Level meters | 5 per second | off |

The CYD's RGB LED turns red when the SoC is at 75 °C or above, the Pi is throttling, or a
container is unhealthy or restarting.
