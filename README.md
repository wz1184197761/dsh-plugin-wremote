# dsh-plugin-wremote

Mirror a **single window** of a Windows desktop app (built for WeChat) into a DSH sidebar panel,
with mouse, wheel, keyboard and text going back to the real window — plus six agent tools so the
model can look at the screen and act on it.

The screen is not re-rendered, re-encoded or proxied through a third-party service: the panel shows
the same MJPEG frames the standalone `wremote` page shows, and clicks are replayed into the real
window through `SendInput`.

> **Windows only.** Capture uses Windows Graphics Capture and input goes through `user32!SendInput`.
> There is no macOS or Linux path.

---

## What it looks like

- Sidebar icon **WeChat mirror** → main-column panel with the live window.
- Click / double click / right click / drag / wheel act on the remote window.
- Whitelisted keys (arrows, Enter, Esc, Tab, Backspace, Delete, PgUp/PgDn, Home/End, F1–F12) pass through.
- Text box sends unicode directly — Chinese and emoji work without an IME.
- Fullscreen uses the **browser Fullscreen API** and renegotiates the source window to the size of
  your screen, so the picture is 1:1 instead of letterboxed.
- Save the current frame as an image file from the panel.

## Install

### 1. The upstream capture service

The plugin is a bridge; it does not capture anything by itself. It talks to a small Python service
(`wremote`) over HTTP + WebSocket on `127.0.0.1:8098`.

From a clone of this repository:

```bat
setup.cmd
```

It creates `.venv`, installs `opencv-python`, `numpy`, `windows-capture`, `websockets`, and starts
the service on port 8098. WeChat must be running — the service locates its main window by title.

To start it later without reinstalling:

```bat
.venv\Scripts\python.exe wremote\launch.py --port 8098 --fps 20 --quality 85
```

`wremote\launch.py` is idempotent: if the port is already listening it exits immediately, so it is
safe to call from a scheduled task or a shortcut.

### 2. The DSH plugin

```bat
dsh plugin add <path-to-this-repo>
```

Then reload the profile (or restart DSH) and open the **WeChat mirror** icon in the sidebar.

If you see *"upstream not connected"*, the service from step 1 is not running — run `setup.cmd`
again, it will detect the port and only report status.

## Configuration

Everything lives in the `config` block of this plugin's entry (`cordis.patch.yml`):

| key | default | meaning |
| --- | --- | --- |
| `host` | `127.0.0.1` | where the wremote service runs |
| `port` | `8098` | its port |
| `path` | `/` | websocket path (the service ignores it) |
| `autoConnect` | `true` | connect as soon as the plugin loads |
| `reconnectMs` | `4000` | reconnect interval after a drop |
| `streamRetryMs` | `1200` | how often the `<img>` re-attaches after a stream error |
| `firstFrameTimeoutMs` | `6000` | how long a tool waits for the first frame |
| `idleGraceMs` | `30000` | drop the upstream connection this long after the last viewer leaves |
| `sendGeom` | `true` | renegotiate the source window size when the panel goes fullscreen |

## Tools

| tool | what it does |
| --- | --- |
| `wremote_status` | upstream connection, frame sequence, fps, frame size, last server hint |
| `wremote_screenshot` | grab the current frame and return it as an attachment |
| `wremote_click` | click at frame pixel `x`/`y`; `double` for double click, `button` for left/right/middle |
| `wremote_scroll` | wheel at `x`/`y`; positive `delta` scrolls up, one notch is 120 |
| `wremote_type` | type text into whatever currently has focus (unicode, no IME needed) |
| `wremote_key` | press one virtual key, e.g. 13=Enter, 27=Esc, 116=F5 |

The tools are registered in **agent scope**, so a session that does not use them does not pay for them.

## How it works

```
DSH panel  <-- MJPEG over HTTP -->  DSH host (this plugin)  <-- WebSocket -->  wremote service
   |                                                                              |
   +------------------- POST /api/wremote/input ----------------------------------+
                                     SendInput into the real window
```

The plugin owns exactly one upstream WebSocket, shared by every viewer, and drops it after
`idleGraceMs` when nobody is watching. Frames are passed through untouched — no re-encode.

Details, including the geometry negotiation protocol and the debugging checklist, are in
[docs/DETAIL.md](docs/DETAIL.md).

## License

MIT — see [LICENSE](LICENSE).
