# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy>=1.24", "opencv-python>=4.8", "mss>=9.0"]
# ///
"""Validate how we parse an mss ScreenShot into a BGRA array, without actually
capturing the screen (which on macOS would raise a Screen Recording prompt).
We build a ScreenShot from known bytes and check the array that comes back."""
import sys, warnings
import pathlib as _p, sys
sys.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "host"))
warnings.simplefilter("error", DeprecationWarning)   # the fix must actually hold

import numpy as np
import mss
from mss.screenshot import ScreenShot
from capture import _open_mss, ScreenCapture, FakeCapture

ok = True
def check(label, cond, detail=""):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))

check("mss version", True, mss.__version__)

# The constructor no longer warns.
try:
    with _open_mss() as sct:
        mon = sct.monitors[1]
    check("_open_mss() is warning-free", True, f"primary {mon['width']}x{mon['height']}")
except DeprecationWarning as exc:
    check("_open_mss() is warning-free", False, str(exc))

# --- BGRA parsing, with a hand-built ScreenShot ----------------------------
W, H = 4, 3
raw = bytearray()
for y in range(H):
    for x in range(W):
        raw += bytes((x * 10, y * 20, 200, 255))          # B, G, R, A
shot = ScreenShot(raw, {"left": 0, "top": 0, "width": W, "height": H})

buf = getattr(shot, "raw", None)
arr = np.frombuffer(buf, dtype=np.uint8).reshape(shot.height, shot.width, 4)
check("shape is (h, w, 4)", arr.shape == (H, W, 4), str(arr.shape))
check("blue channel is index 0", arr[2, 3, 0] == 30, str(arr[2, 3, 0]))
check("green channel is index 1", arr[2, 3, 1] == 40, str(arr[2, 3, 1]))
check("red channel is index 2", arr[2, 3, 2] == 200, str(arr[2, 3, 2]))
check("dropping alpha leaves BGR", arr[:, :, :3].shape == (H, W, 3))

import cv2
bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
check("BGRA2BGR keeps the channel order", tuple(bgr[2, 3]) == (30, 40, 200), str(tuple(bgr[2, 3])))

# --- the probe reports this Mac's real geometry ----------------------------
cap = ScreenCapture(1)
check("ScreenCapture reads the real screen size", cap.width > 0 and cap.height > 0,
      f"{cap.width}x{cap.height}")
try:
    ScreenCapture(99)
    check("a bad --monitor is rejected clearly", False)
except SystemExit as exc:
    check("a bad --monitor is rejected clearly", "no monitor 99" in str(exc), str(exc).split("\n")[0])

# --- the fake screen matches the real interface ----------------------------
fake = FakeCapture()
f = fake.grab()
check("FakeCapture returns BGRA too", f.shape == (720, 1280, 4) and f.dtype == np.uint8, str(f.shape))
check("FakeCapture changes every frame", not np.array_equal(f, fake.grab()))
check("both backends expose the same interface",
      all(hasattr(fake, a) and hasattr(cap, a) for a in ("grab", "close", "width", "height")))

print("\n  " + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
