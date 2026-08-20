"""Turning a captured frame into bytes on the wire -- or into nothing at all.

The whole design rests on one observation: a visual novel screen is static most of
the time. So the first thing encode() does is ask "did anything change?", and the
answer is usually no, in which case we return None and send zero bytes. That single
check is what keeps the link idle at 0 KB/s while you read, and it is why this needs
no video codec.

Frame format on the wire:

    [u16 x][u16 y][u16 w][u16 h][ ...JPEG... ]      little-endian, 8-byte header

v1 always sends whole frames, so x and y are 0 and w/h are the stream size. The
header is here from the start anyway: the tile/dirty-rect upgrade is then a change
to this file alone, because the viewer already draws each payload at its stated
offset.
"""

from __future__ import annotations

import struct

import cv2
import numpy as np

HEADER = struct.Struct("<HHHH")


class FrameEncoder:
    """Change detection, downscale, JPEG.

    `configure` is called from the asyncio thread when a viewer moves a slider while
    `encode` runs on the capture thread. The only shared state is a few ints and a
    flag, and rebinding one of those is atomic in CPython, so no lock is needed --
    the worst case is that a settings change lands one frame later than it could.
    """

    def __init__(self, quality: int = 65, target_height: int = 900, diff_stride: int = 8) -> None:
        self.quality = quality
        self.target_height = target_height
        self.diff_stride = diff_stride
        self._prev: np.ndarray | None = None
        self._force = True
        # stats, read by the /ws status line
        self.frames = 0
        self.skipped = 0
        self.bytes_out = 0
        self.last_encode_ms = 0.0

    def configure(self, quality: int | None = None, target_height: int | None = None) -> None:
        if quality is not None:
            self.quality = max(20, min(95, int(quality)))
        if target_height is not None:
            self.target_height = max(0, min(4320, int(target_height)))
        # Resend immediately so the viewer sees the new setting (and, if the scale
        # changed, learns the new canvas size) without waiting for the screen to move.
        self._force = True

    def encode(self, bgra: np.ndarray, force: bool = False) -> bytes | None:
        """Framed message for this screenful, or None if it is unchanged."""
        force = force or self._force
        self._force = False

        # Point-sample the raw frame on a grid and compare with last time. On 1080p
        # at stride 8 that is ~97k byte comparisons, tens of microseconds, and it
        # runs before any conversion or resize. A change smaller than the stride in
        # both axes (a blinking text caret, say) can slip through; the periodic
        # refresh in server.py mops that up, and --diff-stride tightens it.
        sample = bgra[:: self.diff_stride, :: self.diff_stride, :3]
        if not force and self._prev is not None and np.array_equal(sample, self._prev):
            self.skipped += 1
            return None
        self._prev = sample.copy()  # must copy: the capture buffer gets reused

        t0 = cv2.getTickCount()

        height, width = bgra.shape[:2]
        if self.target_height and self.target_height < height:
            scale = self.target_height / height
            # INTER_AREA is the right filter for shrinking -- it averages, so text
            # stays legible instead of dropping strokes the way point sampling does.
            out = cv2.resize(
                bgra,
                (int(round(width * scale)), self.target_height),
                interpolation=cv2.INTER_AREA,
            )
        else:
            out = bgra

        out = cv2.cvtColor(out, cv2.COLOR_BGRA2BGR)
        ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        if not ok:
            return None

        jpeg = buf.tobytes()
        self.last_encode_ms = (cv2.getTickCount() - t0) / cv2.getTickFrequency() * 1000.0
        self.frames += 1
        self.bytes_out += len(jpeg)
        return HEADER.pack(0, 0, out.shape[1], out.shape[0]) + jpeg
