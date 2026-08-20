"""Turning a captured frame into bytes on the wire -- or into nothing at all.

The whole design rests on one observation: a visual novel screen is static most of
the time. So the first thing encode() does is ask "did anything change?", and the
answer is usually no, in which case we return None and send zero bytes. That single
check is what keeps the link idle at 0 KB/s while you read, and it is why this needs
no video codec.

When something *has* changed, the second observation takes over: usually only a small
part of the screen changed. A line of dialogue types out inside the text box and the
artwork behind it is identical. So the changed frame is diffed again, this time on a
grid of tiles, and only the rectangles that actually differ are encoded and sent. On
a text box repaint that is a tenth of the bytes of a full frame.

Frame format on the wire, little-endian:

    [u16 count][u16 stream_w][u16 stream_h]              6-byte frame header
    count x:
        [u16 x][u16 y][u16 w][u16 h][u32 len][ ...JPEG... ]   12-byte tile header

One WebSocket message carries one whole update, so a frame is never torn across
messages and the viewer can acknowledge exactly one thing per message. The stream
size travels in the frame header because with partial updates the viewer can no
longer infer it from the payload it happens to receive.
"""

from __future__ import annotations

import struct

import cv2
import numpy as np

FRAME = struct.Struct("<HHH")     # count, stream width, stream height
TILE = struct.Struct("<HHHHI")    # x, y, w, h, JPEG length


def dirty_rects(cur: np.ndarray, prev: np.ndarray, tile: int) -> list[tuple[int, int, int, int]]:
    """Tile-aligned rectangles covering every pixel that differs.

    Runs of horizontally adjacent dirty tiles are merged, and a run is merged with an
    identical one directly above it. That matters more than it looks: every JPEG
    carries its own ~600 bytes of Huffman tables, so a hundred separate tiles would
    spend more on headers than a single full frame costs in pixels. A text box comes
    out of this as one rectangle.

    Tiles are kept a multiple of 16 (a 4:2:0 MCU) by the caller, so rectangle edges
    land on encoder block boundaries and do not produce visible seams.
    """
    height, width = cur.shape[:2]
    diff = np.any(cur != prev, axis=2)
    rows = (height + tile - 1) // tile
    cols = (width + tile - 1) // tile

    rects: list[list[int]] = []
    carried: dict[tuple[int, int], int] = {}   # column span -> rect still growing downwards

    for r in range(rows):
        y0, y1 = r * tile, min((r + 1) * tile, height)
        band = diff[y0:y1]

        runs: list[list[int]] = []
        for c in range(cols):
            if band[:, c * tile : min((c + 1) * tile, width)].any():
                if runs and runs[-1][1] == c:
                    runs[-1][1] = c + 1
                else:
                    runs.append([c, c + 1])

        nxt: dict[tuple[int, int], int] = {}
        for c0, c1 in runs:
            idx = carried.get((c0, c1))
            if idx is None:
                rects.append([c0 * tile, y0, min(c1 * tile, width), y1])
                idx = len(rects) - 1
            else:
                rects[idx][3] = y1          # extend the one above instead of starting new
            nxt[(c0, c1)] = idx
        carried = nxt

    return [(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in rects]


class FrameEncoder:
    """Change detection, downscale, tile diff, JPEG.

    `configure` is called from the asyncio thread when a viewer moves a slider while
    `encode` runs on the capture thread. The only shared state is a few ints and a
    flag, and rebinding one of those is atomic in CPython, so no lock is needed --
    the worst case is that a settings change lands one frame later than it could.
    """

    def __init__(
        self,
        quality: int = 65,
        target_height: int = 900,
        diff_stride: int = 8,
        tile: int = 128,
    ) -> None:
        self.quality = quality
        self.target_height = target_height
        self.diff_stride = diff_stride
        # Rounded to a multiple of 16 to stay MCU-aligned; 0 disables tiling and
        # goes back to sending whole frames, which is the useful comparison when
        # you want to know what the tiling is actually buying you.
        self.tile = max(16, (int(tile) // 16) * 16) if tile else 0
        self._prev: np.ndarray | None = None
        self._prev_scaled: np.ndarray | None = None
        self._force = True
        # stats, read by the /ws status line
        self.frames = 0
        self.skipped = 0
        self.tiles_out = 0
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
        """Framed message for this screenful, or None if nothing changed."""
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

        prev = self._prev_scaled
        if force or prev is None or not self.tile or prev.shape != out.shape:
            # A refresh, a first frame, or a resolution change: send the lot. This is
            # also what makes the stream self-healing -- any tile the viewer somehow
            # missed is corrected within --refresh seconds.
            rects = [(0, 0, out.shape[1], out.shape[0])]
        else:
            rects = dirty_rects(out, prev, self.tile)
            if not rects:
                # The raw frame moved but the change did not survive the downscale.
                self.skipped += 1
                return None
        # Only worth keeping if the next frame will actually be diffed against it.
        self._prev_scaled = out.copy() if self.tile else None

        tiles = []
        for x, y, w, h in rects:
            patch = np.ascontiguousarray(out[y : y + h, x : x + w])
            ok, buf = cv2.imencode(".jpg", patch, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
            if not ok:
                return None
            jpeg = buf.tobytes()
            tiles.append(TILE.pack(x, y, w, h, len(jpeg)) + jpeg)

        payload = FRAME.pack(len(tiles), out.shape[1], out.shape[0]) + b"".join(tiles)
        self.last_encode_ms = (cv2.getTickCount() - t0) / cv2.getTickFrequency() * 1000.0
        self.frames += 1
        self.tiles_out += len(tiles)
        self.bytes_out += len(payload)
        return payload
