# LANplay

Watch one laptop's screen from the other, on your own WiFi, and drive it with the
second laptop's keyboard and trackpad. Built for playing a visual novel that lives on
the Windows machine while sitting at the Mac.

No accounts, no cloud relay, nothing to install on the machine you sit at — the host
serves the viewer to a browser tab.

```
  Windows laptop  ──── JPEG frames ────▶  Chrome on the Mac
   (has the game)  ◀──── mouse/keys ────   (nothing installed)
```

## Setup

**On the Windows laptop** (the one with the game), once:

```powershell
winget install astral-sh.uv
```

Copy this folder across, then every time you want to play:

```powershell
uv run host\host.py
```

`uv` reads the dependency list from the top of `host.py` and builds its own
environment — there is no venv to activate and nothing to `pip install`. The first
run takes a minute; after that it starts instantly.

Windows will ask whether to allow Python through the firewall. **Allow it on Private
networks.** If you miss that prompt the Mac will simply never connect, and it looks
exactly like a broken program.

**On the Mac**: open the URL the host prints, in **Chrome**. Enter the PIN. That's it.

```
   Open this in Chrome on the other laptop:

       http://192.168.0.102:8000

   PIN     481 920
```

If `192.168.0.102` does not work, run `ipconfig` on Windows and look for the IPv4
address on your WiFi adapter. `http://<pc-name>.local:8000` usually works too.

## Using it

Click the screen to start sending keys to the other machine. The bar at the bottom
fades away while input is captured.

| | |
|---|---|
| **Click the screen** | start forwarding keyboard input |
| **Ctrl+Alt+Shift+Q** | stop forwarding, get your Mac back |
| **Fullscreen** button | fullscreen + Keyboard Lock, so Esc and Tab reach the game |
| Switching apps | releases input automatically |

The readout shows **fps · KB/s · round-trip ms**. On a still screen the fps should
drop to almost nothing — that is the whole trick, and it is how you know things are
working.

Your Mac's own cursor is the pointer. The remote cursor is not drawn, because neither
Windows' `BitBlt` nor macOS' equivalent captures it — but since your cursor is what
drives the remote one, they are always in the same place. The one thing you lose is
seeing the Windows cursor change shape over a clickable area.

The PIN is new on every start. `--pin 123456` fixes it so you can bookmark
`http://…:8000/?pin=123456` and skip the prompt.

## What it actually costs

Measured on an M5, 1080p source downscaled to 900p at quality 65, against a synthetic
visual-novel screen:

| | frames/sec | bandwidth | |
|---|---|---|---|
| Sitting on a line of dialogue | 0.6 | **20 KB/s** | 0.16 Mbit/s |
| Text typing out, changing every frame | 30 | **790 KB/s** | 6.5 Mbit/s |

A 40× difference, and the reason this works without a video codec. Idle is not
literally zero only because of the periodic refresh (one frame every `--refresh`
seconds, 2 by default); raise that and it goes to nothing.

Dirty rectangles then take most of what is left. Measured against `--fake` at 720p
quality 65, which is a *harsher* test than a visual novel — a box sweeping across the
whole screen with a ticking clock, so a good deal is dirty every frame:

| | frames/sec | bandwidth | per frame |
|---|---|---|---|
| `--tile 0`, whole frames | 31.0 | 774 KB/s | 25 KB |
| `--tile 128`, dirty rectangles | 30.8 | **170 KB/s** | 6 KB |

**4.6× for free**, at the same framerate. A real visual novel does better than this,
because the artwork behind the text box genuinely does not move.

Per-frame CPU cost:

| | |
|---|---|
| unchanged frame — the idle path | **0.07 ms** (0.2% of a core at 30fps) |
| changed frame → 900p | 3.6 ms (11% of a core at 30fps) |
| changed frame → native 1080p | 2.2 ms |

Encoding is not the bottleneck; your WiFi is. Note the last row: **native is cheaper
than 900p**, because it skips the resize. If the Windows laptop is CPU-starved,
`--height 0` costs less CPU — at the price of more bandwidth.

## When it is not fast enough

Start with the **size** dropdown, not quality — halving the height cuts roughly four
times the bytes, and a visual novel stays perfectly readable at 720p.

```powershell
uv run host\host.py --height 720 --quality 55      # lighter
uv run host\host.py --height 0 --quality 85        # native resolution, sharper
uv run host\host.py --fps 15                       # visual novels rarely need more
uv run host\host.py --tile 64                      # finer dirty rectangles
uv run host\host.py --tile 0                       # whole frames, to compare against
```

`--tile 0` is worth knowing about: it turns the dirty-rectangle path off, so you can
measure what it is actually saving you on your own content.

`--help` lists the rest.

## Trouble

**The Mac cannot reach it at all.** Nine times out of ten this is the Windows
firewall. Check that both machines are on the same WiFi network — and that it is not
a "guest" network, which usually blocks devices from seeing each other entirely.

**Clicks land slightly off, worse toward the bottom-right.** That is display scaling,
and `host.py` sets DPI awareness at startup to prevent it. If you see it anyway, say
so — it means the call did not take on your Windows build.

**The game window is black but the desktop is fine.** `mss` cannot capture
exclusive-fullscreen. Set the game to borderless windowed. (`dxcam` would fix this
properly; see below.)

**The game ignores keys but Notepad works.** A few engines read DirectInput scancodes
and ignore injected virtual keys. Swapping `pynput` for `pydirectinput` in
`host/inputs.py` fixes it. Ren'Py and KiriKiri do not need this.

**Something changes on screen but does not update.** Change detection samples an 8px
grid, so a change smaller than that in *both* directions — a blinking text caret —
can slip through until the next refresh. `--diff-stride 4` tightens it; `--refresh 0.5`
resends more often.

## How it works

The host grabs the screen 30×/sec and, before doing anything expensive, checks
whether it differs from the previous frame at all. Usually it does not, and nothing
is sent. When it does, the frame is downscaled and diffed a second time on a grid of
128px tiles, and only the rectangles that actually changed are JPEG-encoded and
pushed over a WebSocket:

```
[u16 count][u16 stream_w][u16 stream_h]
count ×  [u16 x][u16 y][u16 w][u16 h][u32 len][ …JPEG… ]
```

Adjacent dirty tiles are merged into runs before encoding, because every JPEG carries
its own ~600 bytes of Huffman tables — a hundred loose tiles would spend more on
headers than a whole frame costs in pixels. A repainting text box comes out of this
as a single rectangle.

That is why there is no H.264, no WebRTC, no hardware encoder — a visual novel is a
static picture most of the time, so the compression that matters is *not sending
anything*, and the second-best is not sending the nine tenths that did not move.

Capture and encoding run on their own thread. The event loop only sees a single
"latest frame" slot, never a queue, so a viewer that falls behind skips frames and
stays current instead of drifting further into the past.

That slot alone is not enough, because a frame handed to the socket is not a frame on
the wire -- it sits in the send buffer and the WiFi driver's queue, a few hundred KB
of picture the program cannot see. On a saturated link that buffer *is* the lag, and
it is why a stream can run most of a second behind while keystrokes stay instant. So
the viewer acknowledges each frame once it has drawn it, and the host will not commit
more than two unacknowledged frames at a time. The stream then clocks itself to the
real speed of the link, and because the frame is chosen after that wait rather than
before it, what goes out is always the newest one.

| file | |
|---|---|
| `host/host.py` | entry point, DPI fix, PIN, banner |
| `host/capture.py` | screen → BGRA (and the `--fake` synthetic screen) |
| `host/encode.py` | change detection, downscale, dirty rectangles, JPEG |
| `host/inputs.py` | `event.code` → Windows keys, pointer injection |
| `host/server.py` | HTTP + WebSocket, capture thread, frame hub |
| `host/static/` | the viewer page |

## Security

This program types on your computer, so treat the PIN as real:

- A random 6-digit PIN each start; the first WebSocket message must match it or the
  socket is closed. Nothing is processed before that.
- Connections are refused outright unless they come from a private LAN address.
- Plain HTTP by design, for a network you own. **Do not port-forward it.**

## Testing

```sh
uv run tests/test_encode.py       # change detection, scaling, the wire header
uv run tests/test_capture.py      # BGRA parsing, without touching the real screen
uv run tests/test_keys.py         # key map, checked against pynput's Windows backend
node tests/test_viewer.mjs        # viewer/server consistency, static
node tests/test_viewer_ack.mjs    # runs viewer.js on a stub DOM: frame acks
```

And end-to-end, against a running host, on either machine:

```sh
uv run host/host.py --fake --pin 123456     # synthetic screen, input only logged
uv run tests/test_protocol.py               # in another terminal
```

`--fake` is also the fastest way to check a network problem is the network: if the
bouncing box shows up, everything except screen capture is fine.

## Not included

Audio (the game's sound comes out of the Windows laptop's own speakers), gamepads,
file transfer, clipboard sharing, and the reverse direction — the Mac cannot share
its screen to Windows.

Worth knowing about: **Sunshine + Moonlight** is a free, open-source pair that does
all of the above with hardware H.264 and much lower latency. This exists because it
is small enough to read and change.

Natural next steps, roughly in order of payoff:

1. **`dxcam` backend** — Desktop Duplication instead of GDI: faster, near-zero CPU,
   and it can see exclusive-fullscreen games. The biggest remaining win if the host
   is the bottleneck rather than the network.
2. **Quality that adapts to motion** — encode at a lower quality while the screen is
   changing and send one sharp frame once it settles. Roughly halves the peak bitrate,
   and covers the case tiles cannot help with: a scene transition, where everything
   is dirty at once.
3. **Audio** — WASAPI loopback via `pyaudiowpatch` on a second WebSocket. Much the
   hardest of the three.
