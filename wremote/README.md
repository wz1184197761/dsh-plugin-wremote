# wremote — single-window capture + input service

The Python service behind the DSH panel. It captures **one** window with Windows Graphics Capture,
encodes each frame as JPEG, and serves it over HTTP + WebSocket; clicks and keys coming back over the
same socket are replayed into that window with `SendInput`.

You do not need to touch this directory unless the service misbehaves — `setup.cmd` in the repository
root creates the virtualenv and starts it.

## Run

```bat
.venv\Scripts\python.exe launch.py --port 8098 --fps 20 --quality 85
```

`launch.py` is idempotent: if `127.0.0.1:<port>` already accepts connections it prints
`[launch] port N already listening, skip` and exits 0, so it can be scheduled every minute.

To run the server directly (foreground, with logs on the console):

```bat
.venv\Scripts\python.exe server.py --port 8098 --fps 20 --quality 85 --follow weixin
```

| flag | default | meaning |
| --- | --- | --- |
| `hwnd` | – | positional; target window handle. Omit to locate by title |
| `--title` | `微信` | window title to locate when no `hwnd` is given |
| `--port` | `8098` | HTTP / WebSocket port |
| `--fps` | `20` | target frame rate |
| `--quality` | `70` | JPEG quality |
| `--mode` | `mon` | capture backend: `mon` (whole monitor + crop), `wgc` (window capture), `pw` (PrintWindow), `auto` |
| `--no-autosize` | off | do not resize the source window to match the viewer |
| `--follow` | `weixin` | follow the foreground window: `weixin` (WeChat family only), `all`, `off` |
| `--show` | off | `ShowWindow` the target first (WGC cannot capture a hidden window) |

## Endpoints

| route | what |
| --- | --- |
| `GET /` | the standalone viewer page (`client.html`) — use it to check the picture without DSH |
| `WS /` | binary JPEG frames down, JSON messages up (`geom`, `m`, `k`, `text`, `ping`) |

The DSH plugin only uses the WebSocket; the page exists so a human can verify the service alone.

## Protocol

Down: one JPEG per WebSocket message, occasionally one `{"t":"state","src":"mon","hint":"…"}`.

Up:

```json
{"t":"geom","w":1920,"h":1080}
{"t":"geom","w":1920,"h":1080,"take":true}
{"t":"m","a":"down|up|move|wheel|rdown|rup|md|mu","x":0,"y":0,"d":0}
{"t":"k","vk":13,"up":false}
{"t":"text","s":"你好"}
{"t":"ping"}
```

`x` / `y` are **frame pixels**; the server converts them to screen coordinates itself and raises the
target window before every press.

### Geometry negotiation

`state` is shared by every connection, so only one connection — the `geom_owner` — may drive the
real window resize. The first connection to send `geom` becomes the owner; it is released when that
connection closes. Sending `take: true` **seizes** ownership, which is what the DSH panel does when it
goes fullscreen (otherwise a leftover browser tab on `http://127.0.0.1:8098/` keeps the ownership
forever and the panel cannot resize anything).

Two log lines are the debugging handle:

```
[geom] recv 1920x1080 take=True owner=self
[geom] resize client 1876x1032 -> 1920x1080 (want 1920x1080, window 1936x1088)
```

## Requirements

Windows 10 1809 or newer (Windows Graphics Capture), Python 3.10+, and the packages in
`requirements.txt`. `SendInput` and the window enumeration are Win32, so there is no portable path.

## Notes

- The service binds `0.0.0.0` on the chosen port so a remote machine can view it. It has **no
  authentication** — do not expose the port to an untrusted network; keep it on loopback or a private
  tunnel.
- `--follow weixin` only switches the capture source when the foreground window belongs to
  `weixin.exe` / `wechat.exe` / `wechatappex.exe`, so unrelated windows never end up on screen.
