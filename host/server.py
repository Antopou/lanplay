"""The HTTP + WebSocket server, and the capture thread that feeds it.

Capture and JPEG encoding happen on a dedicated thread, not on the event loop.
cv2.imencode is ~5ms of solid CPU per frame; run that on the loop and every send
to every viewer stalls behind it. The thread hands finished frames to the loop
through FrameHub.

FrameHub is also where lag is prevented. It is a *slot*, not a queue: it holds only
the most recent frame. A viewer that cannot keep up therefore skips frames and stays
current, instead of accumulating a backlog and drifting further behind real time --
which is the usual way a homemade screen streamer ends up feeling terrible.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import threading
import time
from pathlib import Path

from aiohttp import WSMsgType, web

STATIC = Path(__file__).resolve().parent / "static"
AUTH_TIMEOUT = 10.0


class FrameHub:
    """Latest-frame slot shared between the capture thread and the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: bytes | None = None
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: set[asyncio.Event] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, payload: bytes) -> None:
        """Called from the capture thread."""
        with self._lock:
            self._payload = payload
            self._seq += 1
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._wake)

    def _wake(self) -> None:
        for event in list(self._waiters):
            event.set()

    def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        self._waiters.add(event)
        with self._lock:
            if self._payload is not None:
                event.set()  # a viewer joining a still screen still gets a picture
        return event

    def unsubscribe(self, event: asyncio.Event) -> None:
        self._waiters.discard(event)

    def take(self, last_seq: int) -> tuple[int, bytes | None]:
        with self._lock:
            if self._seq == last_seq:
                return last_seq, None
            return self._seq, self._payload


def capture_loop(capture, encoder, hub: FrameHub, fps: int, refresh: float, stop: threading.Event) -> None:
    interval = 1.0 / fps
    next_tick = time.perf_counter()
    last_sent = 0.0

    while not stop.is_set():
        try:
            frame = capture.grab()
        except Exception as exc:  # a display mode change can invalidate the handle
            print(f"! capture failed ({exc}); retrying")
            time.sleep(0.5)
            continue

        now = time.perf_counter()
        payload = encoder.encode(frame, force=(now - last_sent) >= refresh)
        if payload is not None:
            hub.publish(payload)
            last_sent = now

        next_tick += interval
        delay = next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            next_tick = time.perf_counter()  # fell behind; drop the debt, don't sprint

    capture.close()


def _is_lan(remote: str | None) -> bool:
    """Only private and loopback addresses may connect.

    This server injects keystrokes, so it should be unreachable from anything routed
    in from outside even if the machine ends up on a network you did not expect.
    """
    if not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


async def _authenticate(ws: web.WebSocketResponse, pin: str, remote: str) -> bool:
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        await ws.close(code=4008, message=b"auth timeout")
        return False

    if msg.type is not WSMsgType.TEXT:
        await ws.close(code=4003, message=b"auth required")
        return False
    try:
        data = json.loads(msg.data)
    except ValueError:
        await ws.close(code=4003, message=b"auth required")
        return False

    supplied = str(data.get("pin", ""))
    if data.get("t") != "auth" or not secrets.compare_digest(supplied, pin):
        print(f"! wrong PIN from {remote}")
        await asyncio.sleep(1.0)  # make brute forcing tedious
        await ws.close(code=4003, message=b"bad pin")
        return False
    return True


async def _send_frames(ws: web.WebSocketResponse, hub: FrameHub) -> None:
    event = hub.subscribe()
    seq = 0
    try:
        while not ws.closed:
            await event.wait()
            event.clear()
            seq, payload = hub.take(seq)
            if payload is None:
                continue
            # While this await is in flight the hub keeps replacing its slot, so a
            # slow viewer simply misses those frames rather than queueing them.
            await ws.send_bytes(payload)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        hub.unsubscribe(event)


async def _handle_input(data: dict, injector, encoder, ws: web.WebSocketResponse) -> None:
    kind = data.get("t")

    if kind == "m":
        injector.move(float(data["x"]), float(data["y"]))

    elif kind in ("d", "u"):
        x, y = data.get("x"), data.get("y")
        injector.button(
            int(data.get("b", 0)),
            kind == "d",
            None if x is None else float(x),
            None if y is None else float(y),
        )

    elif kind == "w":
        injector.wheel(int(data.get("dx", 0)), int(data.get("dy", 0)))

    elif kind == "k":
        code = data.get("c")
        if isinstance(code, str):
            injector.key(code, bool(data.get("d")))

    elif kind == "cfg":
        encoder.configure(quality=data.get("q"), target_height=data.get("h"))
        if "meta" in data:
            injector.meta_as_ctrl = bool(data["meta"])

    elif kind == "ping":
        await ws.send_json({"t": "pong", "ts": data.get("ts")})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    app = request.app
    remote = request.remote or ""

    if not _is_lan(remote):
        print(f"! refused {remote}: not a private address")
        raise web.HTTPForbidden(text="LANplay only accepts connections from your local network")

    ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=64 * 1024)
    await ws.prepare(request)

    if not await _authenticate(ws, app["pin"], remote):
        return ws

    await ws.send_json({"t": "ready", "w": app["width"], "h": app["height"]})
    app["clients"] += 1
    print(f"+ viewer connected from {remote}  ({app['clients']} watching)")

    sender = asyncio.create_task(_send_frames(ws, app["hub"]))
    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                await _handle_input(json.loads(msg.data), app["injector"], app["encoder"], ws)
            except Exception as exc:
                print(f"! ignoring bad input message: {exc}")
    finally:
        sender.cancel()
        try:
            await sender
        except asyncio.CancelledError:
            pass
        # Whatever this viewer was holding down, let go of it -- a tab closed
        # mid-keypress must not leave Shift stuck on the host.
        app["injector"].release_all()
        app["clients"] -= 1
        print(f"- viewer from {remote} disconnected  ({app['clients']} watching)")

    return ws


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-store"},  # so edits show up on reload
    )


def serve(*, capture, encoder, injector, pin: str, port: int, fps: int, refresh: float) -> None:
    hub = FrameHub()
    stop = threading.Event()
    thread = threading.Thread(
        target=capture_loop,
        args=(capture, encoder, hub, fps, refresh, stop),
        name="capture",
        daemon=True,
    )

    app = web.Application()
    app["hub"] = hub
    app["encoder"] = encoder
    app["injector"] = injector
    app["pin"] = pin
    app["width"] = capture.width
    app["height"] = capture.height
    app["clients"] = 0

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC)

    async def on_startup(_: web.Application) -> None:
        hub.bind(asyncio.get_running_loop())
        thread.start()

    async def on_cleanup(_: web.Application) -> None:
        stop.set()
        thread.join(timeout=2.0)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    web.run_app(app, host="0.0.0.0", port=port, print=None, access_log=None)
