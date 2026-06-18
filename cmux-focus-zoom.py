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
