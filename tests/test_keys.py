# /// script
# requires-python = ">=3.10"
# dependencies = ["pynput>=1.7"]
# ///
"""Check the event.code -> key map covers a real keyboard, and that every pynput
Key name we reference actually exists in pynput's WINDOWS backend -- which is the
platform that matters here, and which we cannot import from a Mac. So we read the
backend's source and extract its Key enum as text."""
import pathlib, re, sys
import pathlib as _p, sys
sys.path.insert(0, str(_p.Path(__file__).resolve().parent.parent / "host"))

from inputs import _SPECIAL, _CHARS, InputInjector

ok = True
def check(label, cond, detail=""):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))

# Every event.code a normal laptop keyboard emits.
EXPECTED = (
    [f"Key{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [f"Digit{d}" for d in range(10)]
    + [f"F{i}" for i in range(1, 13)]
    + [f"Numpad{d}" for d in range(10)]
    + ["Escape", "Backquote", "Minus", "Equal", "Backspace", "Tab",
       "BracketLeft", "BracketRight", "Backslash", "CapsLock", "Semicolon",
       "Quote", "Enter", "ShiftLeft", "Comma", "Period", "Slash", "ShiftRight",
       "ControlLeft", "AltLeft", "MetaLeft", "Space", "MetaRight", "AltRight",
       "ControlRight", "ContextMenu", "ArrowUp", "ArrowDown", "ArrowLeft",
       "ArrowRight", "Insert", "Delete", "Home", "End", "PageUp", "PageDown",
       "PrintScreen", "ScrollLock", "Pause", "NumLock", "NumpadDivide",
       "NumpadMultiply", "NumpadSubtract", "NumpadAdd", "NumpadDecimal",
       "NumpadEnter", "IntlBackslash", "IntlRo", "IntlYen"]
)
META = {"MetaLeft", "MetaRight"}

missing = [c for c in EXPECTED if c not in _SPECIAL and c not in _CHARS and c not in META]
check("every key on a real keyboard is mapped", not missing, f"{len(EXPECTED)} codes, missing: {missing}")

overlap = set(_SPECIAL) & set(_CHARS)
check("no code is in both tables", not overlap, str(overlap))

# --- do the pynput names we reference exist on WINDOWS? --------------------
import pynput
win32 = pathlib.Path(pynput.__file__).parent / "keyboard" / "_win32.py"
src = win32.read_text()
body = src.split("class Key(", 1)[1]
win_keys = set(re.findall(r"^\s{4}(\w+)\s*=\s*KeyCode", body, re.M))
check("read pynput's Windows Key enum", len(win_keys) > 30, f"{len(win_keys)} keys defined")

absent = sorted({name for name in _SPECIAL.values()} - win_keys)
check("every Key name we use exists on Windows", not absent, f"missing: {absent}")

for name in ("ctrl_l", "cmd_l", "cmd_r", "cmd"):
    check(f"Key.{name} exists on Windows", name in win_keys)

# --- resolution on this machine -------------------------------------------
inj = InputInjector(1920, 1080, meta_as_ctrl=True)     # constructed, never fired
unresolved = {c for c in EXPECTED if inj._resolve(c) is None}

# pynput's Key enum differs per platform. Anything unresolved on this Mac must be a
# key macOS genuinely lacks -- never a hole in our own tables.
darwin = pathlib.Path(pynput.__file__).parent / "keyboard" / "_darwin.py"
mac_keys = set(re.findall(r"^\s{4}(\w+)\s*=\s*KeyCode", darwin.read_text().split("class Key(", 1)[1], re.M))
absent_here = {c for c in EXPECTED if c in _SPECIAL and _SPECIAL[c] not in mac_keys}

check("nothing is unresolved except keys this platform lacks",
      unresolved == absent_here, f"unresolved {sorted(unresolved)}")
check("and every one of those DOES exist on Windows",
      all(_SPECIAL[c] in win_keys for c in unresolved),
      ", ".join(f"{c}={_SPECIAL[c]}" for c in sorted(unresolved)) or "none")
check("so on the Windows host, nothing is unresolved",
      not [c for c in EXPECTED if c not in META and c not in _CHARS
           and (c not in _SPECIAL or _SPECIAL[c] not in win_keys)])

check("letters map to lowercase, letting the host apply Shift",
      inj._resolve("KeyZ") == "z", repr(inj._resolve("KeyZ")))
check("Cmd becomes Ctrl by default", inj._resolve("MetaLeft") == inj._Key.ctrl_l)
inj.meta_as_ctrl = False
check("--win-key sends a real Cmd/Win key", inj._resolve("MetaLeft") != inj._Key.ctrl_l,
      str(inj._resolve("MetaLeft")))
check("an unknown code resolves to nothing rather than exploding",
      inj._resolve("LaunchMediaPlayer") is None)

# --- coordinate mapping ----------------------------------------------------
check("0,0 maps to the top-left pixel", inj._to_pixels(0.0, 0.0) == (0, 0))
check("1,1 maps to the last pixel", inj._to_pixels(1.0, 1.0) == (1919, 1079))
check("the centre maps to the centre", inj._to_pixels(0.5, 0.5) == (960, 540))
check("out-of-range input is clamped", inj._to_pixels(-3.0, 9.0) == (0, 1079))

print("\n  " + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
