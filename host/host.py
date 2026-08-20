#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "aiohttp>=3.9",
#     "numpy>=1.24",
#     "opencv-python>=4.8",
#     "mss>=9.0",
#     "pynput>=1.7",
# ]
# ///
"""LANplay -- share this screen, and this machine's mouse and keyboard, to a
browser on your own WiFi.

    uv run host/host.py            share the real screen
    uv run host/host.py --fake     synthetic screen, input only logged

Then open the printed URL in Chrome on the other laptop.
"""

from __future__ import annotations

import argparse
import ctypes
import secrets
import socket
import sys

from encode import FrameEncoder
from server import serve


def enable_dpi_awareness() -> None:
    """Tell Windows we speak in real pixels.

    Windows laptops usually run at 125% or 150% scaling. A process that has not said
    otherwise is handed a *virtualized* smaller desktop: captures come back at the
    wrong size and SetCursorPos lands in the wrong place, so clicks drift -- slightly
    near the top-left, badly near the bottom-right. This one call avoids an afternoon
    of confusion.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # pre-8.1 fallback
    except (AttributeError, OSError):
        print("! could not set DPI awareness -- clicks may drift on a scaled display")


def lan_address() -> str:
    """The address the other laptop should dial.

    Opening a UDP socket toward an arbitrary address makes the OS pick the interface
    it would actually route through, which is a far better guess than the hostname
    on a machine with VPN, Thunderbolt bridge and virtual adapters. No packet is sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def print_banner(url: str, pin: str, source: str, stream: str, fake: bool) -> None:
    pretty_pin = f"{pin[:3]} {pin[3:]}" if len(pin) == 6 else pin
    line = "-" * 58
    print(f"\n  {line}")
    print("   LANplay" + ("   [--fake: synthetic screen, input only logged]" if fake else ""))
    print(f"  {line}")
    print("\n   Open this in Chrome on the other laptop:\n")
    print(f"       {url}\n")
    print(f"   PIN     {pretty_pin}")
    print(f"   Screen  {source}  ->  {stream}")
    print(f"   Input   {'logged to this terminal' if fake else 'mouse + keyboard go to THIS machine'}")
    print(f"\n  {line}")
    print("   Ctrl-C to stop.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lanplay",
        description="Share this screen and its input to a browser on your LAN.",
    )
    parser.add_argument("--port", type=int, default=8000, help="default 8000")
    parser.add_argument("--pin", help="use a fixed PIN instead of a fresh random one")
    parser.add_argument("--fake", action="store_true",
                        help="synthetic screen and logged-only input, for testing the pipe")
    parser.add_argument("--fps", type=int, default=30, help="capture rate cap, default 30")
    parser.add_argument("--quality", type=int, default=65, help="JPEG quality 20-95, default 65")
    parser.add_argument("--height", type=int, default=900,
                        help="downscale to this many pixels tall; 0 for native, default 900")
    parser.add_argument("--monitor", type=int, default=1, help="which screen, 1 = primary")
    parser.add_argument("--refresh", type=float, default=2.0,
                        help="resend an unchanged screen this often, seconds")
    parser.add_argument("--diff-stride", type=int, default=8,
                        help="change-detection sampling grid; smaller catches more, costs more")
    parser.add_argument("--tile", type=int, default=128,
                        help="dirty-rectangle grid in pixels; 0 sends whole frames")
    parser.add_argument("--win-key", action="store_true",
                        help="send Cmd as the Windows key rather than as Ctrl")
    args = parser.parse_args()

    # Line-buffer stdout: piped to a file or a log, block buffering would swallow
    # the banner -- and the banner is where the PIN is.
    sys.stdout.reconfigure(line_buffering=True)

    if args.pin is not None and len(args.pin) < 4:
        parser.error("--pin should be at least 4 characters")

    enable_dpi_awareness()
    pin = args.pin or f"{secrets.randbelow(1_000_000):06d}"

    if args.fake:
        from capture import FakeCapture
        from inputs import LoggingInjector

        capture = FakeCapture()
        injector = LoggingInjector(capture.width, capture.height, meta_as_ctrl=not args.win_key)
    else:
        from capture import ScreenCapture
        from inputs import InputInjector

        capture = ScreenCapture(args.monitor)
        try:
            injector = InputInjector(capture.width, capture.height, meta_as_ctrl=not args.win_key)
        except Exception as exc:
            raise SystemExit(
                f"could not open the input devices: {exc}\n"
                "On macOS grant Accessibility to your terminal in System Settings > "
                "Privacy & Security."
            ) from exc

    encoder = FrameEncoder(
        quality=args.quality,
        target_height=args.height,
        diff_stride=max(1, args.diff_stride),
        tile=args.tile,
    )

    source = f"{capture.width}x{capture.height}"
    if args.height and args.height < capture.height:
        width = int(round(capture.width * args.height / capture.height))
        stream = f"{width}x{args.height} @ {args.fps}fps q{args.quality}"
    else:
        stream = f"native @ {args.fps}fps q{args.quality}"

    print_banner(f"http://{lan_address()}:{args.port}", pin, source, stream, args.fake)

    try:
        serve(
            capture=capture,
            encoder=encoder,
            injector=injector,
            pin=pin,
            port=args.port,
            fps=max(1, args.fps),
            refresh=args.refresh,
        )
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        raise SystemExit(
            f"could not listen on port {args.port}: {exc}\nTry --port 8001."
        ) from exc
    finally:
        print("\n  stopped.\n")


if __name__ == "__main__":
    main()
