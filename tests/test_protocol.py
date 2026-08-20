# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp>=3.9", "numpy>=1.24", "opencv-python>=4.8"]
# ///
"""End-to-end check of the LANplay wire protocol against a running host."""
import asyncio, json, struct, sys, time
import aiohttp, numpy as np, cv2

URL = "http://127.0.0.1:8000/ws"
PIN = "123456"
ok = True

def check(label, cond, detail=""):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))

async def main():
    async with aiohttp.ClientSession() as s:
        # --- wrong PIN must be refused --------------------------------------
        async with s.ws_connect(URL) as ws:
            await ws.send_json({"t": "auth", "pin": "000000"})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            check("wrong PIN is rejected", msg.type is aiohttp.WSMsgType.CLOSE,
                  f"close code {ws.close_code}")
            check("rejection uses code 4003", ws.close_code == 4003)

        # --- no auth at all: the socket must not stream ----------------------
        async with s.ws_connect(URL) as ws:
            await ws.send_json({"t": "m", "x": 0.5, "y": 0.5})   # skip the handshake
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            check("unauthenticated input is refused", msg.type is aiohttp.WSMsgType.CLOSE)

        # --- correct PIN ----------------------------------------------------
        async with s.ws_connect(URL) as ws:
            await ws.send_json({"t": "auth", "pin": PIN})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            ready = json.loads(msg.data) if msg.type is aiohttp.WSMsgType.TEXT else {}
            check("handshake returns ready", ready.get("t") == "ready",
                  f"screen {ready.get('w')}x{ready.get('h')}")

            # --- frames -----------------------------------------------------
            sizes, dims, t0 = [], None, time.perf_counter()
            while len(sizes) < 45 and time.perf_counter() - t0 < 6:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                if msg.type is not aiohttp.WSMsgType.BINARY:
                    continue
                x, y, w, h = struct.unpack("<HHHH", msg.data[:8])
                img = cv2.imdecode(np.frombuffer(msg.data[8:], np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    check("JPEG payload decodes", False)
                    break
                if dims is None:
                    dims = (w, h)
                    check("header matches decoded JPEG", (img.shape[1], img.shape[0]) == (w, h),
                          f"{w}x{h}, origin {x},{y}")
                sizes.append(len(msg.data))
            elapsed = time.perf_counter() - t0
            fps = len(sizes) / elapsed
            check("frames arrive at roughly the fps cap", 20 <= fps <= 34, f"{fps:.1f} fps")
            check("frame size is sane", 5_000 < sum(sizes) / len(sizes) < 400_000,
                  f"avg {sum(sizes) / len(sizes) / 1024:.0f} KB -> {sum(sizes) / elapsed / 1024:.0f} KB/s")

            # --- ping / pong ------------------------------------------------
            sent = time.perf_counter()
            await ws.send_json({"t": "ping", "ts": 1234.5})
            pong = None
            while time.perf_counter() - sent < 3:
                msg = await asyncio.wait_for(ws.receive(), timeout=3)
                if msg.type is aiohttp.WSMsgType.TEXT:
                    pong = json.loads(msg.data)
                    break
            check("ping is echoed with its timestamp", pong == {"t": "pong", "ts": 1234.5}, str(pong))

            # --- input ------------------------------------------------------
            for m in ({"t": "m", "x": 0.25, "y": 0.75},
                      {"t": "d", "b": 0, "x": 0.25, "y": 0.75},
                      {"t": "u", "b": 0, "x": 0.25, "y": 0.75},
                      {"t": "w", "dx": 0, "dy": -3},
                      {"t": "k", "c": "KeyA", "d": 1},
                      {"t": "k", "c": "KeyA", "d": 0},
                      {"t": "k", "c": "ShiftLeft", "d": 1}):
                await ws.send_json(m)

            # --- a malformed message must not kill the session --------------
            await ws.send_str("{not json")
            await ws.send_json({"t": "m"})            # missing coordinates
            await ws.send_json({"t": "zzz"})          # unknown type
            await asyncio.sleep(0.4)
            await ws.send_json({"t": "cfg", "q": 40, "h": 720})
            await asyncio.sleep(0.5)

            newdims = None
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 3:
                msg = await asyncio.wait_for(ws.receive(), timeout=3)
                if msg.type is aiohttp.WSMsgType.BINARY:
                    newdims = struct.unpack("<HHHH", msg.data[:8])[2:]
                    break
            check("session survives malformed input", newdims is not None)
            check("cfg resizes the stream", newdims == (1280, 720), f"{dims} -> {newdims}")

        # --- two viewers at once ----------------------------------------
        # FrameHub fans one encoded frame out to every waiter; nothing else
        # exercises that path.
        async def watch(label):
            async with s.ws_connect(URL) as w:
                await w.send_json({"t": "auth", "pin": PIN})
                got, t0 = 0, time.perf_counter()
                while got < 10 and time.perf_counter() - t0 < 6:
                    msg = await asyncio.wait_for(w.receive(), timeout=5)
                    if msg.type is aiohttp.WSMsgType.BINARY:
                        got += 1
                return got

        a, b = await asyncio.gather(watch("a"), watch("b"))
        check("two viewers both get frames", a >= 10 and b >= 10, f"{a} and {b} frames")

    print("\n  " + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1

sys.exit(asyncio.run(main()))
