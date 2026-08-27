#!/usr/bin/env python3
"""
Bouncing ball -> jpeg_bridge.py smoke test.

Draws a simple bouncing ball animation and streams it as back-to-back JPEGs
to the bridge's TCP socket, matching the default 192x192 canvas.

  ./python/jpeg_bridge.py &
  ./python/bouncing_ball.py
"""

import argparse
import io
import socket
import time

from PIL import Image, ImageDraw


def main():
    ap = argparse.ArgumentParser(description="Stream a bouncing ball to jpeg_bridge.py.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--canvas", default="192x192", help="WxH, must match the bridge's --canvas")
    ap.add_argument("--radius", type=int, default=16)
    ap.add_argument("--speed", type=int, default=4, help="pixels moved per frame")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--color", default="255,80,80", help="R,G,B")
    ap.add_argument("--bg", default="0,0,0", help="R,G,B")
    args = ap.parse_args()

    w, h = (int(v) for v in args.canvas.lower().split("x"))
    color = tuple(int(v) for v in args.color.split(","))
    bg = tuple(int(v) for v in args.bg.split(","))
    r = args.radius
    frame_dt = 1.0 / args.fps

    sock = socket.create_connection((args.host, args.port))
    print("connected to %s:%d, streaming %dx%d" % (args.host, args.port, w, h))

    x, y = float(r), float(r)
    vx, vy = float(args.speed), float(args.speed)

    try:
        while True:
            t0 = time.monotonic()

            x += vx
            y += vy
            if x - r < 0:
                x = r
                vx = abs(vx)
            elif x + r > w:
                x = w - r
                vx = -abs(vx)
            if y - r < 0:
                y = r
                vy = abs(vy)
            elif y + r > h:
                y = h - r
                vy = -abs(vy)

            img = Image.new("RGB", (w, h), bg)
            draw = ImageDraw.Draw(img)
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

            blob = io.BytesIO()
            img.save(blob, "JPEG", quality=85)
            sock.sendall(blob.getvalue())

            elapsed = time.monotonic() - t0
            if elapsed < frame_dt:
                time.sleep(frame_dt - elapsed)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        raise SystemExit("bridge closed the connection -- is it still running?")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
