"""Replaying the viewer's mouse and keyboard onto this machine.

pynput wraps Win32 SendInput, which is what ordinary windowed apps -- Ren'Py,
KiriKiri, browsers, Notepad -- read their input from. A handful of engines read raw
DirectInput scancodes and ignore this; `pydirectinput` is the drop-in swap if you
ever hit one, but visual novels do not need it.

Keys arrive as DOM `event.code` values -- KeyZ, ShiftLeft, Digit4 -- which name the
*physical* key rather than the character it produces, so they survive any keyboard
layout in one piece. We translate those to characters and let the host OS apply its
own layout and modifier state, which means typing produces the letter you meant even
if the two laptops disagree about where the keys live.

Coordinates arrive normalized to 0..1 and get scaled against the real screen size --
never the downscaled stream size. Resolution changes, viewer window resizes and
Retina scaling therefore cannot desynchronize the pointer.
"""

from __future__ import annotations

# event.code -> pynput Key attribute name. Anything pynput lacks on this platform is
# dropped when the table is built rather than exploding on first press.
_SPECIAL = {
    "Escape": "esc", "Tab": "tab", "CapsLock": "caps_lock", "Enter": "enter",
    "NumpadEnter": "enter", "Space": "space", "Backspace": "backspace",
    "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "PageUp": "page_up", "PageDown": "page_down",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "ShiftLeft": "shift_l", "ShiftRight": "shift_r",
    "ControlLeft": "ctrl_l", "ControlRight": "ctrl_r",
    "AltLeft": "alt_l", "AltRight": "alt_r",
    "PrintScreen": "print_screen", "ScrollLock": "scroll_lock", "Pause": "pause",
    "NumLock": "num_lock", "ContextMenu": "menu",
}
_SPECIAL.update({f"F{i}": f"f{i}" for i in range(1, 21)})

# event.code -> the character to type.
_CHARS = {
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]",
    "Backslash": "\\", "Semicolon": ";", "Quote": "'", "Backquote": "`",
    "Comma": ",", "Period": ".", "Slash": "/",
    "IntlBackslash": "\\", "IntlRo": "/", "IntlYen": "\\",
    "NumpadDivide": "/", "NumpadMultiply": "*", "NumpadSubtract": "-",
    "NumpadAdd": "+", "NumpadDecimal": ".",
}
_CHARS.update({f"Key{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
_CHARS.update({f"Digit{d}": str(d) for d in range(10)})
_CHARS.update({f"Numpad{d}": str(d) for d in range(10)})


class InputInjector:
    """Applies input events to the real desktop."""

    def __init__(self, width: int, height: int, meta_as_ctrl: bool = True) -> None:
        from pynput import keyboard, mouse

        self._mouse = mouse.Controller()
        self._kb = keyboard.Controller()
        self._Button = mouse.Button
        self._Key = keyboard.Key

        self.width, self.height = width, height
        self.meta_as_ctrl = meta_as_ctrl

        self._keys = {code: getattr(keyboard.Key, name, None) for code, name in _SPECIAL.items()}
        self._keys = {code: key for code, key in self._keys.items() if key is not None}

        # DOM order: MouseEvent.button is 0 left, 1 middle, 2 right.
        self._buttons = [mouse.Button.left, mouse.Button.middle, mouse.Button.right]
        self._held_keys: set[str] = set()
        self._held_buttons: set[int] = set()

    # -- pointer ---------------------------------------------------------------

    def _to_pixels(self, nx: float, ny: float) -> tuple[int, int]:
        x = min(max(nx, 0.0), 1.0) * (self.width - 1)
        y = min(max(ny, 0.0), 1.0) * (self.height - 1)
        return int(round(x)), int(round(y))

    def move(self, nx: float, ny: float) -> None:
        self._mouse.position = self._to_pixels(nx, ny)

    def button(self, index: int, down: bool, nx: float | None = None, ny: float | None = None) -> None:
        if not 0 <= index < len(self._buttons):
            return
        # Press events carry their own position so a click always lands where the
        # viewer saw it, even if the move that preceded it was dropped.
        if nx is not None and ny is not None:
            self.move(nx, ny)
        button = self._buttons[index]
        if down:
            self._mouse.press(button)
            self._held_buttons.add(index)
        else:
            self._mouse.release(button)
            self._held_buttons.discard(index)

    def wheel(self, dx: int, dy: int) -> None:
        # The wire uses the browser's convention (dy > 0 means scrolling down);
        # pynput's is the opposite.
        self._mouse.scroll(int(dx), -int(dy))

    # -- keyboard --------------------------------------------------------------

    def _resolve(self, code: str):
        if code in ("MetaLeft", "MetaRight"):
            # A Mac user's muscle memory says Cmd+C, so Cmd becomes Ctrl by default.
            # The viewer's "Cmd = Win key" toggle turns that off.
            if self.meta_as_ctrl:
                return self._Key.ctrl_l
            return getattr(self._Key, "cmd_l" if code == "MetaLeft" else "cmd_r", self._Key.cmd)
        if code in self._keys:
            return self._keys[code]
        return _CHARS.get(code)

    def key(self, code: str, down: bool) -> None:
        key = self._resolve(code)
        if key is None:
            return
        if down:
            self._kb.press(key)
            self._held_keys.add(code)
        else:
            self._kb.release(key)
            self._held_keys.discard(code)

    def release_all(self) -> None:
        """Let go of everything still held.

        Called when a viewer disconnects. Without it, closing the tab mid-keypress
        leaves Shift or Ctrl stuck down on the host, and every subsequent keystroke
        on the host's own keyboard behaves bizarrely.
        """
        for code in list(self._held_keys):
            try:
                self.key(code, False)
            except Exception:
                pass
        for index in list(self._held_buttons):
            try:
                self.button(index, False)
            except Exception:
                pass
        self._held_keys.clear()
        self._held_buttons.clear()


class LoggingInjector:
    """Prints what it would have done. Used by --fake so the input path can be
    exercised without granting accessibility permissions or moving a real cursor."""

    def __init__(self, width: int, height: int, meta_as_ctrl: bool = True) -> None:
        self.width, self.height = width, height
        self.meta_as_ctrl = meta_as_ctrl
        self._moves = 0

    def move(self, nx: float, ny: float) -> None:
        self._moves += 1
        if self._moves % 30 == 0:  # one line a second at 30fps, not a firehose
            print(f"  move  {nx:.3f},{ny:.3f}  ->  {int(nx * self.width)},{int(ny * self.height)}px")

    def button(self, index: int, down: bool, nx: float | None = None, ny: float | None = None) -> None:
        name = ("left", "middle", "right")[index] if 0 <= index < 3 else f"#{index}"
        where = f" at {nx:.3f},{ny:.3f}" if nx is not None else ""
        print(f"  {'press ' if down else 'release'} {name}{where}")

    def wheel(self, dx: int, dy: int) -> None:
        print(f"  wheel  dx={dx} dy={dy}")

    def key(self, code: str, down: bool) -> None:
        print(f"  key    {code} {'down' if down else 'up'}")

    def release_all(self) -> None:
        print("  release all")
