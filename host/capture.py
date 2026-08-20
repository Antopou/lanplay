"""Grabbing pixels off the host screen.

`grab()` hands back a raw BGRA array exactly as the OS gave it to us -- no colour
conversion, no resize, no copy beyond what the platform forces. That is deliberate.
Most frames of a visual novel are byte-identical to the one before, and we want to
find that out as cheaply as possible (see encode.py) before paying for the expensive
part. Converting first would burn ~2ms per frame on images we throw away.

mss is the default backend because it is ctypes-only and installs anywhere with no
build step. On Windows `dxcam` is faster and can also see exclusive-fullscreen games,
which GDI cannot; it would slot in behind this same two-method interface.

Neither backend captures the mouse cursor -- BitBlt on Windows and CGDisplayCreateImage
on macOS both leave it out. The viewer shows your own local cursor instead, which is
in sync by construction since it is what drives the remote pointer.
"""

from __future__ import annotations

import math
import time

import numpy as np


def _open_mss():
    """mss.mss() is deprecated in favour of mss.MSS(); accept whichever this
    version has so the console stays free of warnings either way."""
    import mss

    return (getattr(mss, "MSS", None) or mss.mss)()


class ScreenCapture:
    """One monitor, grabbed on demand.

    The mss handle is created lazily on the first grab() rather than in __init__,
    because mss instances are bound to the thread that made them and grab() runs on
    the capture thread while __init__ runs on the main one.
    """

    def __init__(self, monitor: int = 1) -> None:
        self.monitor = monitor
        self._sct = None
        self._region = None
        self.width, self.height = self._probe()

    def _probe(self) -> tuple[int, int]:
        """Read the monitor size up front with a throwaway handle."""
        with _open_mss() as sct:
            if self.monitor >= len(sct.monitors):
                raise SystemExit(
                    f"no monitor {self.monitor}; this machine has "
                    f"{len(sct.monitors) - 1} (use --monitor 1..{len(sct.monitors) - 1})"
                )
            region = sct.monitors[self.monitor]
            return region["width"], region["height"]

    def grab(self) -> np.ndarray:
        """A (height, width, 4) BGRA array. May alias an internal buffer -- copy
        anything you intend to keep past the next grab()."""
        if self._sct is None:
            self._sct = _open_mss()
            self._region = self._sct.monitors[self.monitor]

        shot = self._sct.grab(self._region)
        try:
            buf = shot.raw
        except AttributeError:  # older mss
            buf = shot.bgra
        return np.frombuffer(buf, dtype=np.uint8).reshape(shot.height, shot.width, 4)

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


class FakeCapture:
    """A synthetic screen, for `--fake`.

    Lets you build and debug the entire pipe -- websocket, auth, binary framing,
    canvas drawing, input capture -- on whichever laptop you happen to be sitting at,
    with no screen-recording permission and no platform-specific code in the way.
    The running clock doubles as a rough latency check: photograph both screens at
    once and compare.
    """

    def __init__(self, width: int = 1280, height: int = 720) -> None:
        self.width, self.height = width, height
        self._n = 0
        base = np.zeros((height, width, 3), np.uint8)
        base[:, :, 0] = (np.linspace(40, 150, width, dtype=np.uint8))[None, :]   # B
        base[:, :, 1] = 24                                                        # G
        base[:, :, 2] = (np.linspace(20, 90, height, dtype=np.uint8))[:, None]    # R
        self._base = base

    def grab(self) -> np.ndarray:
        import cv2

        frame = self._base.copy()
        n = self._n
        self._n += 1

        bw, bh = 200, 150
        x = int((self.width - bw) * (0.5 + 0.5 * math.sin(n * 0.031)))
        y = int((self.height - bh) * (0.5 + 0.5 * math.sin(n * 0.047)))
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (230, 230, 230), -1)
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (60, 60, 60), 3)

        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(frame, "LANplay  --fake", (40, 80), font, 1.5, (255, 255, 255), 3)
        cv2.putText(frame, time.strftime("%H:%M:%S"), (40, 150), font, 1.2, (200, 255, 200), 2)
        cv2.putText(frame, f"frame {n}", (40, 210), font, 0.9, (180, 180, 180), 2)
        cv2.putText(
            frame,
            "move the mouse / press keys - they log in the host terminal",
            (40, self.height - 40),
            font,
            0.7,
            (170, 170, 170),
            2,
        )
        return cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)

    def close(self) -> None:
        pass
