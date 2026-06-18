# Replace Terminal + VS Code with cmux

A practical setup guide for an agent-first macOS dev environment, plus a small daemon that auto-zooms the focused pane.

cmux is a native macOS terminal built on [Ghostty](https://ghostty.org/), designed for running AI coding agents in parallel. For an agent-driven workflow, it does the job that most people split across Terminal (or iTerm) and VS Code, in a single native app.

## Why it replaces Terminal + VS Code

When the agent writes most of the code, what you actually need is orchestration, file context, a browser for previews, and many shells side by side. That is the gap between the two tools people normally run:

| Need | Terminal / iTerm | VS Code | cmux |
| --- | --- | --- | --- |
| Fast native shells | Yes | Integrated panel only | Yes (GPU-accelerated, Ghostty) |
| File explorer | No | Yes | Yes (right sidebar) |
| Built-in browser for localhost / previews | No | Extension | Yes (browser surfaces) |
| Many agents in parallel | Tab soup | One terminal panel, clumsy | Vertical tabs, one workspace per agent |
| Git branch / PR / CWD at a glance | No | Partial | Yes, in the tab sidebar |
| Knows when an agent needs you | No | No | Yes (blue ring + notification) |
| Scriptable from the outside | tmux | No | Yes (Unix-socket CLI + RPC) |
| Native, lightweight | Yes | Electron | Yes (Swift / AppKit) |

The honest caveat: cmux is not a full IDE. There is no language server or inline IntelliSense. If your editing is done by an agent plus an occasional terminal editor, you stop needing VS Code's editor surface, and cmux covers everything around it natively. If you live in the editor by hand, keep VS Code for that and use cmux as the terminal.

cmux is intentionally not an AI orchestrator. It is a primitive that runs any agent (Claude Code, Codex, Gemini, OpenCode, Amp, Cursor CLI, Copilot, and more) side by side without locking you into one workflow.

## Prerequisites

- macOS 14.0 (Sonoma) or later. Apple Silicon or Intel (cmux ships as a universal binary).
- An AI coding agent CLI if you want the agent workflow (for example Claude Code or Codex). Optional, but it is the point.
- For the auto-zoom daemon below: Python 3.9+, which ships with the macOS Command Line Tools (`xcode-select --install`).
- Optional: an existing Ghostty config at `~/.config/ghostty/config` carries over automatically (font, theme, keybinds, transparency).

## Install cmux

Homebrew (recommended, gives you upgrades in one command):

```sh
brew tap manaflow-ai/cmux
brew install --cask cmux
```

Update later with:

```sh
brew upgrade --cask cmux
```

Or download the DMG directly from [cmux.com](https://cmux.com/) (or the [latest release](https://github.com/manaflow-ai/cmux/releases/latest/download/cmux-macos.dmg)) and drag cmux into Applications. Either way, on first launch macOS may ask you to confirm opening an app from an identified developer. Click Open.

The control CLI ships inside the app bundle:

```
/Applications/cmux.app/Contents/Resources/bin/cmux
```

Put it on your PATH so you can call `cmux` from anywhere:

```sh
ln -s /Applications/cmux.app/Contents/Resources/bin/cmux /usr/local/bin/cmux   # Intel / manual prefix
# or, on Apple Silicon with a writable Homebrew prefix:
ln -s /Applications/cmux.app/Contents/Resources/bin/cmux /opt/homebrew/bin/cmux
```

Verify:

```sh
cmux version
cmux --help
```

## The 5-minute tour

- Workspaces are the vertical tabs down the side. Run one agent or one task per workspace. `cmux new-workspace --name "feature-x" --cwd ~/code/project`.
- Inside a workspace, split into panes: `cmux new-split right` / `down`. Multiple shells, one screen.
- The right sidebar replaces the VS Code explorer. Toggle it and switch its mode (files, find, sessions, feed): `cmux right-sidebar files` / `find` / `hide`.
- A pane can be a browser instead of a shell, for localhost and previews: `cmux new-pane --type browser --url http://localhost:3000`.
- The tab sidebar shows live git branch, PR number, CWD, and agent status with zero config. When an agent is waiting on you, its pane gets a blue ring and the tab lights up. Notifications ride on terminal sequences (OSC 9/99/777) and a `cmux notify` CLI you can wire into agent hooks.
- Everything above is scriptable over a Unix socket: `cmux <command>` for high-level actions, `cmux rpc <method> <json>` for the full API, and `cmux events` for a live event stream. That last one is what makes the daemon below possible.

## Bonus: auto-zoom the focused pane

A 2x2 mosaic is great for watching several agents at once, but the pane you are actually typing in is too small. This daemon keeps the mosaic but enlarges whichever pane is focused to 72% of the window, so the other three stay visible but shrink. It also hides the file sidebar in agent sessions. The big pane follows your focus automatically.

It listens to the cmux event stream and reacts to `workspace.created`, `workspace.selected`, and `pane.focused`.

The full source is below and is also shipped as [`cmux-focus-zoom.py`](cmux-focus-zoom.py) in this repo (the LaunchAgent template is [`com.example.cmux-focus-zoom.plist`](com.example.cmux-focus-zoom.plist)). Save the script as `~/bin/cmux-focus-zoom.py`:

```python
#!/usr/bin/env python3
"""cmux focus-zoom: keep a tiling mosaic but enlarge the focused pane.

On a managed workspace the daemon hides the right sidebar and grows the
focused pane to RATIO of the window while the others stay visible. The big
pane follows focus. Targets panes by UUID (refs renumber over time). The
resize `--amount` is in points (pixels), linear, no clamp.
"""
import json
import os
import subprocess
import sys
import time

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
RATIO = 0.72             # share of the window the focused pane takes, per axis
MARKER = "✳"        # only manage workspaces whose title starts with this
                         # ("✳" is cmux's agent/cloud marker). Set MARKER = ""
                         # to manage every multi-pane workspace.
HIDE_SIDEBAR = True
MIN_PANES = 2            # never resize a single-pane workspace
DEBOUNCE_S = 1.2
SETTLE_S = 0.18
LOG_PATH = os.path.expanduser("~/Library/Logs/cmux-focus-zoom.log")
ENV = {"PATH": "/Applications/cmux.app/Contents/Resources/bin:/usr/bin:/bin",
       "CMUX_QUIET": "1"}

_titles = {}
_managed = set()
_last_apply = {}


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


def cmux(*args, timeout=8):
    try:
        r = subprocess.run([CMUX, *args], capture_output=True, text=True,
                           env=ENV, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        log("cmux %s -> error %s" % (" ".join(args), e))
        return ""


def rpc(method, params=None):
    try:
        return json.loads(cmux("rpc", method, json.dumps(params or {})))
    except (ValueError, TypeError):
        return None


def is_managed_title(title):
    return bool(title) and title.lstrip().startswith(MARKER)


def sidebar_mode():
    try:
        return json.loads(cmux("right-sidebar", "mode"))
    except (ValueError, TypeError):
        return None


def set_sidebar(hidden):
    mode = sidebar_mode()
    if mode is not None and mode.get("visible") == (not hidden):
        return  # already in the wanted state
    cmux("right-sidebar", "hide" if hidden else "show")
    log("sidebar %s" % ("hidden" if hidden else "shown"))


def enlarge_focused(ws_id):
    d = rpc("pane.list", {"workspace": ws_id})
    if not d or "panes" not in d:
        return
    panes = d["panes"]
    if len(panes) < MIN_PANES:
        return
    cont = d["container_frame"]
    foc = next((p for p in panes if p.get("focused")), None)
    if not foc:
        return
    uid = foc["id"]

    rpc("workspace.equalize_splits", {})  # acts on the selected workspace
    time.sleep(SETTLE_S)

    fc = foc["pixel_frame"]
    cx_pane = fc["x"] + fc["width"] / 2.0
    cy_pane = fc["y"] + fc["height"] / 2.0
    cx_win = cont.get("x", 0) + cont["width"] / 2.0
    cy_win = cont.get("y", 0) + cont["height"] / 2.0
    hdir = "-R" if cx_pane < cx_win else "-L"
    vdir = "-D" if cy_pane < cy_win else "-U"
    target_w = RATIO * cont["width"]
    target_h = RATIO * cont["height"]

    for _ in range(2):  # two corrective passes; amount is in points, linear
        d = rpc("pane.list", {"workspace": ws_id})
        if not d:
            return
        cur = next((p for p in d["panes"] if p["id"] == uid), None)
        if not cur:
            return
        f = cur["pixel_frame"]
        dw = round(target_w - f["width"])
        dh = round(target_h - f["height"])
        if dw < 2 and dh < 2:
            break
        if dw >= 2:
            cmux("resize-pane", "--pane", uid, "--workspace", ws_id, hdir, "--amount", str(dw))
        if dh >= 2:
            cmux("resize-pane", "--pane", uid, "--workspace", ws_id, vdir, "--amount", str(dh))
        time.sleep(SETTLE_S)
    log("zoom focused pane %s in ws %s" % (uid[:8], ws_id[:8]))


def enter_workspace(ws_id, title):
    if title is not None:
        _titles[ws_id] = title
    if ws_id in _managed or is_managed_title(_titles.get(ws_id)):
        _managed.add(ws_id)
        if HIDE_SIDEBAR:
            set_sidebar(True)
        enlarge_focused(ws_id)


def pane_focused(ws_id, pane_id):
    if ws_id not in _managed:
        if not _titles.get(ws_id):
            for w in (rpc("workspace.list", {}) or {}).get("workspaces", []):
                _titles[w["id"]] = w.get("title")
        if not is_managed_title(_titles.get(ws_id)):
            return
        _managed.add(ws_id)
    prev = _last_apply.get(ws_id)
    now = time.monotonic()
    if prev and prev[0] == pane_id and (now - prev[1]) < DEBOUNCE_S:
        return
    _last_apply[ws_id] = (pane_id, now)
    if HIDE_SIDEBAR:
        set_sidebar(True)
    enlarge_focused(ws_id)


def handle(ev):
    name = ev.get("name", "")
    payload = ev.get("payload") or {}
    ws_id = ev.get("workspace_id") or payload.get("workspace_id")
    if name == "workspace.created":
        _titles[ws_id] = payload.get("title")
        enter_workspace(ws_id, payload.get("title"))
    elif name == "workspace.selected":
        enter_workspace(ws_id, payload.get("title"))
    elif name == "workspace.closed":
        _managed.discard(ws_id)
        _last_apply.pop(ws_id, None)
    elif name == "pane.focused":
        pane_id = payload.get("pane_id") or ev.get("pane_id")
        if ws_id and pane_id:
            pane_focused(ws_id, pane_id)


def run():
    log("daemon started (RATIO=%.2f, marker=%r)" % (RATIO, MARKER))
    for w in (rpc("workspace.list", {}) or {}).get("workspaces", []):
        _titles[w["id"]] = w.get("title")
    backoff = 1
    while True:
        proc = subprocess.Popen([CMUX, "events", "--no-heartbeat", "--no-ack"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=ENV, text=True)
        backoff = 1
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    handle(ev)
                except Exception as e:  # never kill the loop on one bad event
                    log("handle error: %r" % e)
        finally:
            try:
                proc.terminate()
            except OSError:
                pass
        log("event stream closed, reconnecting in %ss" % backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
```

Tune it from the top of the file: raise `RATIO` for a bigger focused pane, or set `MARKER = ""` to apply the zoom to every multi-pane workspace instead of only agent sessions.

### Run it at login

Run it under launchd so it starts at login and restarts if it dies. Save this as `~/Library/LaunchAgents/com.example.cmux-focus-zoom.plist` (replace `YOURNAME` with your macOS short username, `id -un`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.cmux-focus-zoom</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOURNAME/bin/cmux-focus-zoom.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Applications/cmux.app/Contents/Resources/bin:/usr/bin:/bin</string>
        <key>CMUX_QUIET</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ProcessType</key><string>Background</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOURNAME/Library/Logs/cmux-focus-zoom.err.log</string>
</dict>
</plist>
```

Load and start it:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.cmux-focus-zoom.plist
launchctl kickstart -k gui/$(id -u)/com.example.cmux-focus-zoom
```

Manage it later:

```sh
launchctl print  gui/$(id -u)/com.example.cmux-focus-zoom   # status
launchctl kickstart -k gui/$(id -u)/com.example.cmux-focus-zoom   # restart (after editing the script)
launchctl bootout gui/$(id -u)/com.example.cmux-focus-zoom   # stop and unload
```

Logs land in `~/Library/Logs/cmux-focus-zoom.log` (actions) and `~/Library/Logs/cmux-focus-zoom.err.log` (stderr, including any traceback).

### Things worth knowing (learned the hard way)

- Target panes by UUID, never by ref. The `workspace:N` / `pane:N` short refs renumber over time. Resolve the UUID fresh with `cmux rpc pane.list` on every action.
- `focus-pane` on a background (non-selected) workspace does not emit `pane.focused`. Only the foreground workspace emits, which is exactly what you want in normal use. To test by hand, select the workspace first, then change panes.
- `workspace.equalize_splits` acts on the currently selected workspace and ignores the param. That is fine here, because the focus events that trigger the daemon make the target workspace current.
- The resize `--amount` is in points (pixels), not cells. Compute the pixel delta and pass it straight through.

## Daily workflow

One workspace per agent or per task. Splits for the shells you watch together. Sidebar in files mode when you are navigating, hidden when an agent is driving (the daemon does this for you). Browser panes for previews instead of alt-tabbing to Chrome. Let the blue ring and notifications tell you which agent needs a decision, instead of polling tabs yourself.

## Sources

- [cmux (official site)](https://cmux.com/)
- [manaflow-ai/cmux (GitHub)](https://github.com/manaflow-ai/cmux)
- [cmux agent integrations docs](https://cmux.com/docs/agent-integrations/claude-code-teams)
- [Ghostty](https://ghostty.org/)
