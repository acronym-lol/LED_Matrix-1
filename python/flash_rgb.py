#!/usr/bin/env python3
"""
Flash the whole panel red -> green -> blue -> repeat, as a bring-up smoke
test for jpeg_bridge.py.

  ./python/jpeg_bridge.py &
  ./python/flash_rgb.py
"""

import argparse
import io
import socket
import time

from PIL import Image

COLORS = [
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
]


def main():
    ap = argparse.ArgumentParser(description="Flash the panel red/green/blue via jpeg_bridge.py.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--canvas", default="192x192", help="WxH, must match the bridge's --canvas")
    ap.add_argument("--hold", type=float, default=1.0, help="seconds to hold each color")
    args = ap.parse_args()

    w, h = (int(v) for v in args.canvas.lower().split("x"))

    sock = socket.create_connection((args.host, args.port))
    print("connected to %s:%d, flashing %dx%d" % (args.host, args.port, w, h))

    try:
        while True:
            for name, rgb in COLORS:
                img = Image.new("RGB", (w, h), rgb)
                blob = io.BytesIO()
                img.save(blob, "JPEG", quality=95)
                data = blob.getvalue()

                print(name)
                deadline = time.monotonic() + args.hold
                while time.monotonic() < deadline:
                    sock.sendall(data)
                    time.sleep(1 / 30)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        raise SystemExit("bridge closed the connection -- is it still running?")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
