#!/usr/bin/env python3
"""
Flash the whole panel red -> green -> blue -> repeat, as a bring-up smoke
test for bridge.py.

  sudo ./python/bridge.py --iface eth0 &
  ./python/flash_rgb.py
"""

import argparse
import socket
import time

import numpy as np
from PIL import Image

COLORS = [
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
]


def main():
    ap = argparse.ArgumentParser(description="Flash the panel red/green/blue via bridge.py.")
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
                data = np.asarray(img, dtype=np.uint8).tobytes()

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
