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
from encode import FrameEncoder

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

def dims(payload):
    return struct.unpack("<HHHH", payload[:8])[2:]

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
img = cv2.imdecode(np.frombuffer(p[8:], np.uint8), cv2.IMREAD_COLOR)
check("1080p downscales to 900p keeping aspect", dims(p) == (1600, 900), str(dims(p)))
check("header matches the actual JPEG", (img.shape[1], img.shape[0]) == dims(p),
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
