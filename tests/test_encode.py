# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy>=1.24", "opencv-python>=4.8", "mss>=9.0"]
# ///
"""Unit checks for the piece the whole design rests on: encode.py deciding when
NOT to send anything, and getting the header right when it does."""
import struct, sys
import pathlib as _p, sys
sys.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "host"))

import numpy as np, cv2
from encode import FrameEncoder, FRAME, TILE

ok = True
def check(label, cond, detail=""):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))

def screen(h=1080, w=1920, seed=0):
    rng = np.random.default_rng(seed)
    f = np.zeros((h, w, 4), np.uint8)
    f[:, :, :3] = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    return f

def unpack(payload):
    """[(x, y, w, h, jpeg), ...] plus the stream size the frame header declares."""
    count, sw, sh = FRAME.unpack_from(payload, 0)
    tiles, at = [], FRAME.size
    for _ in range(count):
        x, y, w, h, n = TILE.unpack_from(payload, at)
        at += TILE.size
        tiles.append((x, y, w, h, payload[at : at + n]))
        at += n
    assert at == len(payload), f"{at} != {len(payload)}: tile lengths do not tile the payload"
    return (sw, sh), tiles

def dims(payload):
    return unpack(payload)[0]

# --- the idle path ---------------------------------------------------------
e = FrameEncoder(quality=65, target_height=900)
a = screen()
check("first frame is sent", e.encode(a) is not None)
check("an identical frame sends NOTHING", e.encode(a) is None)
check("still nothing after many identical frames",
      all(e.encode(a) is None for _ in range(30)), f"skipped={e.skipped}")
check("a changed frame is sent", e.encode(screen(seed=1)) is not None)

# --- forced refresh --------------------------------------------------------
b = screen(seed=2)
e.encode(b)
check("force=True resends an unchanged frame", e.encode(b, force=True) is not None)
check("...and does not stick on", e.encode(b) is None)
e.configure(quality=40)
check("configure() triggers a resend", e.encode(b) is not None)

# --- geometry --------------------------------------------------------------
e = FrameEncoder(quality=65, target_height=900)
p = e.encode(screen(1080, 1920))
size, tiles = unpack(p)
img = cv2.imdecode(np.frombuffer(tiles[0][4], np.uint8), cv2.IMREAD_COLOR)
check("1080p downscales to 900p keeping aspect", size == (1600, 900), str(size))
check("the first frame is one whole-screen tile",
      len(tiles) == 1 and tiles[0][:4] == (0, 0, 1600, 900), str(tiles[0][:4]))
check("header matches the actual JPEG", (img.shape[1], img.shape[0]) == size,
      f"{img.shape[1]}x{img.shape[0]}")

e = FrameEncoder(target_height=0)
p = e.encode(screen(1080, 1920))
check("target_height=0 streams native", dims(p) == (1920, 1080), str(dims(p)))

e = FrameEncoder(target_height=2160)
p = e.encode(screen(1080, 1920))
check("a target taller than the screen never upscales", dims(p) == (1920, 1080), str(dims(p)))

e = FrameEncoder(target_height=900)
p = e.encode(screen(1200, 1920))       # 16:10, an odd aspect
check("16:10 keeps its aspect ratio", dims(p) == (1440, 900), str(dims(p)))

# --- configure clamps and re-scales ----------------------------------------
e = FrameEncoder(quality=65, target_height=900)
e.encode(screen())
e.configure(target_height=720)
p = e.encode(screen())
check("cfg actually changes the stream size", dims(p) == (1280, 720), str(dims(p)))
e.configure(quality=999)
check("quality is clamped to a sane maximum", e.quality == 95, str(e.quality))
e.configure(quality=1)
check("quality is clamped to a sane minimum", e.quality == 20, str(e.quality))

# --- dirty rectangles ------------------------------------------------------
# The point of the whole exercise: a text box repaint must not cost a screenful.
def artwork(h=1080, w=1920):
    """Something that costs what a real screen costs.

    A flat colour is the wrong yardstick here: JPEG squeezes it to nearly nothing, so
    a full frame looks cheap and the tiling looks pointless. A visual novel background
    is smooth painted artwork with detail in it, which is what makes a whole frame
    expensive and skipping most of it worth doing.
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    f = np.zeros((h, w, 4), np.uint8)
    f[:, :, 0] = (120 + 60 * np.sin(xx / 90) + 40 * np.cos(yy / 130)).clip(0, 255)
    f[:, :, 1] = (110 + 50 * np.sin((xx + yy) / 70)).clip(0, 255)
    f[:, :, 2] = (130 + 45 * np.cos(xx / 50) * np.sin(yy / 160)).clip(0, 255)
    return f

e = FrameEncoder(quality=65, target_height=0, diff_stride=8, tile=128)
base = artwork()
full = e.encode(base)
check("the first frame is whole", len(unpack(full)[1]) == 1)

box = base.copy()
box[800:900, 200:1700, :3] = 255            # a dialogue box repainting
p = e.encode(box)
size, tiles = unpack(p)
covered = sum(w * h for _, _, w, h, _ in tiles)
check("only the changed band is sent", len(tiles) == 1, f"{len(tiles)} tile(s)")
check("the rectangle covers the change",
      tiles[0][0] <= 200 and tiles[0][1] <= 800
      and tiles[0][0] + tiles[0][2] >= 1700 and tiles[0][1] + tiles[0][3] >= 900,
      f"{tiles[0][:4]} for 200,800 1500x100")
check("the rectangle is tile-aligned",
      all(v % 128 == 0 for v in tiles[0][:2]), str(tiles[0][:4]))
check("a text box costs a fraction of a full frame", len(p) < len(full) / 4,
      f"{len(p)/1024:.0f} KB vs {len(full)/1024:.0f} KB full "
      f"({len(full)/len(p):.1f}x saving)")
check("it covers far less than the screen", covered < 1920 * 1080 / 4,
      f"{100*covered/(1920*1080):.0f}% of the screen")

# Two separate changes must not be merged into one screen-sized bounding box.
two = box.copy()
two[10:20, 10:20, :3] = 1
two[1000:1010, 1900:1910, :3] = 1
p = e.encode(two)
_, tiles = unpack(p)
check("distant changes stay separate rectangles", len(tiles) == 2, f"{len(tiles)} tile(s)")

# A tall thin change should merge vertically rather than emit one rect per row.
e2 = FrameEncoder(quality=65, target_height=0, tile=128)
e2.encode(base)
tall = base.copy(); tall[0:1080, 500:520, :3] = 255
_, tiles = unpack(e2.encode(tall))
check("a vertical run merges into one rectangle", len(tiles) == 1, f"{len(tiles)} tile(s)")

# Redrawing the same pixels changes nothing, so nothing may be sent.
check("re-sending identical pixels sends nothing", e2.encode(tall) is None)

# tile=0 opts out entirely.
e3 = FrameEncoder(quality=65, target_height=0, tile=0)
e3.encode(base)
_, tiles = unpack(e3.encode(box))
check("--tile 0 goes back to whole frames",
      len(tiles) == 1 and tiles[0][:4] == (0, 0, 1920, 1080), str(tiles[0][:4]))

# Every tile must decode, and land where the header says.
e4 = FrameEncoder(quality=80, target_height=0, tile=128)
e4.encode(base)
_, tiles = unpack(e4.encode(box))
good = all(
    (lambda im: im is not None and (im.shape[1], im.shape[0]) == (w, h))(
        cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR))
    for _, _, w, h, j in tiles)
check("every tile decodes at its declared size", good, f"{len(tiles)} tile(s)")

# --- quality has the expected effect ---------------------------------------
frame = screen(720, 1280, seed=7)
lo = FrameEncoder(quality=25, target_height=0).encode(frame)
hi = FrameEncoder(quality=90, target_height=0).encode(frame)
check("lower quality means fewer bytes", len(lo) < len(hi),
      f"q25 {len(lo)//1024} KB vs q90 {len(hi)//1024} KB")

# --- the documented blind spot in change detection -------------------------
base = np.zeros((1080, 1920, 4), np.uint8)
e = FrameEncoder(diff_stride=8, target_height=0)
e.encode(base)
tiny = base.copy(); tiny[101:104, 101:104, :3] = 255      # 3px dot, misses the grid
check("a sub-stride dot can slip past stride 8 (known, covered by --refresh)",
      e.encode(tiny) is None)
e2 = FrameEncoder(diff_stride=1, target_height=0)
e2.encode(base)
check("...and stride 1 catches it", e2.encode(tiny) is not None)

big = base.copy(); big[100:140, 100:400, :3] = 255        # a line of text-sized change
e3 = FrameEncoder(diff_stride=8, target_height=0)
e3.encode(base)
check("a text-sized change is always caught", e3.encode(big) is not None)

# --- what mss actually hands back ------------------------------------------
import mss
with mss.mss() as sct:
    mons = sct.monitors
    check("mss enumerates this Mac's displays", len(mons) >= 2,
          f"{len(mons) - 1} display(s), primary {mons[1]['width']}x{mons[1]['height']}")
    check("ScreenShot exposes .raw as we assume",
          hasattr(mss.screenshot.ScreenShot, "raw"))

print("\n  " + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
