#!/usr/bin/env python3
"""
Three bouncing balls (red, green, blue) -> bridge.py -> LED matrix.

No daemon, no shared memory. This streams back-to-back JPEGs over TCP to
bridge.py, which decodes them and sends them straight to the receiver card
over a raw Ethernet socket. This is what acronym-logo-animation.service runs
as the second half of its two-script pipeline.

  sudo ./python/bridge.py --iface eth0   # must already be running
  ./python/three_bouncing_balls.py
"""

import argparse
import io
import socket
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

BALLS = [
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
]


class Ball:
    def __init__(self, x, y, vx, vy, radius, color):
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.radius = radius
        self.color = color

    def step(self, w, h):
        self.x += self.vx
        self.y += self.vy
        r = self.radius
        if self.x - r < 0:
            self.x = r
            self.vx = abs(self.vx)
        elif self.x + r > w:
            self.x = w - r
            self.vx = -abs(self.vx)
        if self.y - r < 0:
            self.y = r
            self.vy = abs(self.vy)
        elif self.y + r > h:
            self.y = h - r
            self.vy = -abs(self.vy)

    def draw(self, draw):
        r = self.radius
        draw.ellipse(
            [self.x - r, self.y - r, self.x + r, self.y + r], fill=self.color
        )


def make_balls(w, h, radius, speed):
    balls = []
    for i, (_, rgb) in enumerate(BALLS):
        x = radius + (i + 1) * (w - 2 * radius) / (len(BALLS) + 1)
        y = radius + (i + 1) * (h - 2 * radius) / (len(BALLS) + 1)
        angle = (2 * np.pi / len(BALLS)) * i
        vx = speed * np.cos(angle) or speed
        vy = speed * np.sin(angle) or speed
        balls.append(Ball(x, y, vx, vy, radius, rgb))
    return balls


def main():
    ap = argparse.ArgumentParser(description="Bounce three RGB balls, streamed to bridge.py.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--canvas", default="192x192", help="WxH, must match bridge.py's --canvas")
    ap.add_argument("--radius", type=int, default=16)
    ap.add_argument("--speed", type=float, default=3.0, help="pixels moved per frame")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--bg", default="0,0,0", help="R,G,B")
    args = ap.parse_args()

    w, h = (int(v) for v in args.canvas.lower().split("x"))
    bg = tuple(int(v) for v in args.bg.split(","))
    frame_dt = 1.0 / args.fps

    sock = socket.create_connection((args.host, args.port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("connected to %s:%d, streaming %dx%d" % (args.host, args.port, w, h), file=sys.stderr)

    balls = make_balls(w, h, args.radius, args.speed)

    try:
        while True:
            t0 = time.monotonic()

            img = Image.new("RGB", (w, h), bg)
            draw = ImageDraw.Draw(img)
            for ball in balls:
                ball.step(w, h)
                ball.draw(draw)

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
