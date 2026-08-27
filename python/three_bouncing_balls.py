#!/usr/bin/env python3
"""
Three bouncing balls (red, green, blue) -> LED matrix, direct to shared memory.

Unlike bouncing_ball.py this does not go through jpeg_bridge.py -- it talks
straight to the daemon's shared memory framebuffer, the same way
jpeg_bridge.py's Matrix class does. That makes it a self-contained two-step
pipeline (daemon, then this script), which is what acronym-logo-animation.service
runs.

  cd daemon && ./Matrix config.txt   # must already be running as root
  ./python/three_bouncing_balls.py
"""

import argparse
import mmap
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

CMD_SEND_FRAME = 1
CMD_GET_ROWS = 3
CMD_GET_COLS = 4
CMD_SET_BRIGHTNESS = 5

BALLS = [
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
]


class Matrix:
    """The daemon's shared memory framebuffer. See jpeg_bridge.py for the
    layout notes -- byte 0 is the command register, bytes 4.. are rows*cols
    pixels of packed BGR (Matrix_RGB_t declares blue, green, red in that
    order)."""

    def __init__(self, channel):
        path = "/tmp/LED_Matrix-%d.mem" % channel
        try:
            self._file = open(path, "r+b")
        except FileNotFoundError:
            raise SystemExit(
                "%s does not exist -- is the daemon running?\n"
                "  cd daemon && ./Matrix config.txt" % path
            )
        self._mm = mmap.mmap(self._file.fileno(), 0)

        self.rows = self._query(CMD_GET_ROWS)
        self.cols = self._query(CMD_GET_COLS)

        expected = 4 + self.rows * self.cols * 3
        if len(self._mm) < expected:
            raise SystemExit(
                "shared memory is %d bytes but %dx%d needs %d -- stale file? "
                "stop the daemon, delete %s, start it again"
                % (len(self._mm), self.rows, self.cols, expected, path)
            )

        self.buf = np.ndarray(
            (self.rows, self.cols, 3), dtype=np.uint8, buffer=self._mm, offset=4
        )

    def _wait(self, timeout):
        deadline = time.monotonic() + timeout
        while self._mm[0] != 0:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.0002)
        return True

    def _query(self, cmd, timeout=2.0):
        self._mm[0] = cmd
        if not self._wait(timeout):
            raise SystemExit("daemon did not answer command %d -- is it alive?" % cmd)
        return (self._mm[2] << 8) | self._mm[3]

    def set_brightness(self, percent):
        self._mm[1] = percent & 0xFF
        self._mm[0] = CMD_SET_BRIGHTNESS
        self._wait(2.0)

    def send_frame(self, timeout=1.0):
        self._mm[0] = CMD_SEND_FRAME
        return self._wait(timeout)

    def close(self):
        self._mm.close()
        self._file.close()


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
    ap = argparse.ArgumentParser(description="Bounce three RGB balls directly on the LED matrix.")
    ap.add_argument("--channel", type=int, default=0, help="daemon channel (default 0)")
    ap.add_argument("--radius", type=int, default=16)
    ap.add_argument("--speed", type=float, default=3.0, help="pixels moved per frame")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--bg", default="0,0,0", help="R,G,B")
    ap.add_argument("--brightness", type=int, help="set panel brightness 0-100 at startup")
    args = ap.parse_args()

    bg = tuple(int(v) for v in args.bg.split(","))
    frame_dt = 1.0 / args.fps

    matrix = Matrix(args.channel)
    print("driving %dx%d directly via shared memory" % (matrix.cols, matrix.rows), file=sys.stderr)

    if args.brightness is not None:
        matrix.set_brightness(max(0, min(100, args.brightness)))

    w, h = matrix.cols, matrix.rows
    balls = make_balls(w, h, args.radius, args.speed)

    try:
        while True:
            t0 = time.monotonic()

            img = Image.new("RGB", (w, h), bg)
            draw = ImageDraw.Draw(img)
            for ball in balls:
                ball.step(w, h)
                ball.draw(draw)

            matrix.buf[:] = np.asarray(img)[:, :, ::-1]
            if not matrix.send_frame():
                print("daemon did not clear the command register", file=sys.stderr)

            elapsed = time.monotonic() - t0
            if elapsed < frame_dt:
                time.sleep(frame_dt - elapsed)
    except KeyboardInterrupt:
        pass
    finally:
        matrix.close()


if __name__ == "__main__":
    main()
