#!/usr/bin/env python3
"""
JPEG -> LED_Matrix bridge.

Accepts a stream of JPEG frames (TCP socket or stdin), decodes them, maps them
onto the receiver card's pixel layout and hands them to the daemon through the
shared memory framebuffer at /tmp/LED_Matrix-<channel>.mem.

The daemon must already be running -- it is what creates the shared memory
file. This program does not need root.

Framing is auto-detected from the first bytes of the stream:
  * back-to-back JPEGs (starts with FFD8) -- what "ffmpeg -f mjpeg" produces
  * 4-byte big-endian length prefix before each JPEG

Example:
  ./jpeg_bridge.py &
  ffmpeg -re -i clip.mp4 -vf scale=192:192 -f mjpeg tcp://127.0.0.1:9000
"""

import argparse
import gc
import io
import mmap
import socket
import sys
import time

import numpy as np
from PIL import Image

# Command register values understood by the daemon (see daemon/main.cpp).
CMD_SEND_FRAME = 1
CMD_GET_ROWS = 3
CMD_GET_COLS = 4
CMD_SET_BRIGHTNESS = 5

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

MAX_FRAME_BYTES = 32 << 20


class Matrix:
    """The daemon's shared memory framebuffer.

    Layout: byte 0 is the command register, bytes 4.. are rows*cols pixels of
    packed BGR (Matrix_RGB_t declares blue, green, red in that order).
    """

    def __init__(self, channel):
        path = "/tmp/LED_Matrix-%d.mem" % channel
        try:
            self._file = open(path, "r+b")
        except FileNotFoundError:
            raise SystemExit(
                "%s does not exist -- is the daemon running?\n"
                "  cd daemon && sudo ./Matrix config.txt" % path
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
        """Ask the daemon to push the framebuffer out over Ethernet."""
        self._mm[0] = CMD_SEND_FRAME
        return self._wait(timeout)

    def close(self):
        self._mm.close()
        self._file.close()


def build_mapper(matrix, canvas_w, canvas_h, mode):
    """Return f(rgb_array) that writes a canvas-shaped RGB frame into the
    receiver-card-shaped BGR framebuffer.

    'blocks' reproduces Matrix::map_pixel from C++/Matrix.cpp: a wide canvas is
    cut into full-width-of-the-receiver blocks which stack vertically, last
    block on top. The stock 16x128 sign on a 64x32 receiver is 4 such blocks.
    """
    rows, cols, dst = matrix.rows, matrix.cols, matrix.buf

    if mode == "none":
        if (canvas_h, canvas_w) != (rows, cols):
            raise SystemExit(
                "--mapping none needs the canvas to equal the receiver: "
                "canvas is %dx%d, receiver is %dx%d (WxH)"
                % (canvas_w, canvas_h, cols, rows)
            )

        def apply(src):
            dst[:] = src[:, :, ::-1]

        return apply

    if canvas_w % cols:
        raise SystemExit(
            "canvas width %d is not a multiple of the receiver width %d"
            % (canvas_w, cols)
        )
    nblocks = canvas_w // cols
    if canvas_h * nblocks != rows:
        raise SystemExit(
            "canvas %dx%d splits into %d blocks of height %d = %d rows, but the "
            "receiver has %d rows"
            % (canvas_w, canvas_h, nblocks, canvas_h, canvas_h * nblocks, rows)
        )

    # Precompute the slice pairs so the per-frame cost is nblocks memcpys.
    moves = []
    for b in range(nblocks):
        y0 = (nblocks - 1 - b) * canvas_h
        moves.append((slice(y0, y0 + canvas_h), slice(b * cols, (b + 1) * cols)))

    def apply(src):
        for dst_rows, src_cols in moves:
            dst[dst_rows, :, :] = src[:, src_cols, ::-1]

    return apply


def decode(payload, canvas_w, canvas_h, fit):
    img = Image.open(io.BytesIO(payload))
    # Lets libjpeg decode straight to 1/2, 1/4 or 1/8 scale -- a big win when
    # the source is 1080p and the panel is a few thousand pixels.
    img.draft("RGB", (canvas_w, canvas_h))
    img = img.convert("RGB")

    if img.size != (canvas_w, canvas_h):
        if fit == "contain":
            scale = min(canvas_w / img.width, canvas_h / img.height)
            new = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img = img.resize(new, Image.BILINEAR)
            padded = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
            padded.paste(img, ((canvas_w - new[0]) // 2, (canvas_h - new[1]) // 2))
            img = padded
        else:
            img = img.resize((canvas_w, canvas_h), Image.BILINEAR)

    return np.asarray(img)


class JpegStream:
    """Pulls whole JPEGs out of a byte stream, auto-detecting the framing."""

    def __init__(self, recv):
        self._recv = recv
        self._buf = bytearray()
        self._mode = None
        self._scan = 0

    def _fill(self):
        chunk = self._recv(65536)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def _need(self, n):
        while len(self._buf) < n:
            if not self._fill():
                return False
        return True

    def _take(self, n):
        payload = bytes(self._buf[:n])
        del self._buf[:n]
        return payload

    def frames(self):
        while True:
            if self._mode is None:
                if not self._need(4):
                    return
                self._mode = "mjpeg" if self._buf[:2] == SOI else "length"

            if self._mode == "length":
                if not self._need(4):
                    return
                size = int.from_bytes(self._buf[:4], "big")
                if size == 0 or size > MAX_FRAME_BYTES:
                    raise SystemExit("bogus frame length %d -- framing lost" % size)
                if not self._need(4 + size):
                    return
                del self._buf[:4]
                yield self._take(size)
                continue

            # Back-to-back JPEGs: drop anything before SOI, then hunt for EOI.
            start = self._buf.find(SOI)
            while start < 0:
                del self._buf[:-1]
                if not self._fill():
                    return
                start = self._buf.find(SOI)
            if start:
                del self._buf[:start]
                self._scan = 0

            end = self._buf.find(EOI, max(2, self._scan))
            while end < 0:
                self._scan = max(2, len(self._buf) - 1)
                if not self._fill():
                    return
                end = self._buf.find(EOI, self._scan)
            self._scan = 0
            yield self._take(end + 2)


def run(stream, matrix, mapper, args):
    frames = dropped = 0
    last = time.monotonic()

    for payload in stream.frames():
        try:
            src = decode(payload, args.canvas_w, args.canvas_h, args.fit)
        except Exception as exc:
            # In mjpeg mode a JPEG carrying an EXIF thumbnail can trip an early
            # EOI. We resync on the next SOI, costing one frame.
            dropped += 1
            if dropped < 5:
                print("skipped a frame: %s" % exc, file=sys.stderr)
            continue

        mapper(src)
        if not matrix.send_frame():
            print("daemon did not clear the command register", file=sys.stderr)
        frames += 1

        if not args.quiet:
            now = time.monotonic()
            if now - last >= 5.0:
                print(
                    "%.1f fps (%d frames, %d dropped)"
                    % (frames / (now - last), frames, dropped),
                    file=sys.stderr,
                )
                frames = 0
                last = now


def main():
    ap = argparse.ArgumentParser(description="Stream JPEG frames to a ColorLight receiver card.")
    ap.add_argument("--channel", type=int, default=0, help="daemon channel (default 0)")
    ap.add_argument("--port", type=int, default=9000, help="TCP port to listen on (default 9000)")
    ap.add_argument("--bind", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    ap.add_argument("--stdin", action="store_true", help="read the stream from stdin instead of a socket")
    ap.add_argument("--canvas", default="192x192", help="canvas size as WxH (default 192x192)")
    ap.add_argument("--mapping", choices=["blocks", "none"], default="none",
                    help="'blocks' matches Matrix::map_pixel; 'none' writes straight through")
    ap.add_argument("--fit", choices=["stretch", "contain"], default="stretch",
                    help="how to fit a mismatched source (default stretch)")
    ap.add_argument("--brightness", type=int, help="set panel brightness 0-100 at startup")
    ap.add_argument("--quiet", action="store_true", help="do not print throughput")
    args = ap.parse_args()

    try:
        args.canvas_w, args.canvas_h = (int(v) for v in args.canvas.lower().split("x"))
    except ValueError:
        raise SystemExit("--canvas wants WxH, e.g. 128x16")

    matrix = Matrix(args.channel)
    mapper = build_mapper(matrix, args.canvas_w, args.canvas_h, args.mapping)

    if args.brightness is not None:
        matrix.set_brightness(max(0, min(100, args.brightness)))

    print(
        "receiver %dx%d, canvas %dx%d, mapping %s"
        % (matrix.cols, matrix.rows, args.canvas_w, args.canvas_h, args.mapping),
        file=sys.stderr,
    )

    # The README warns that timing hiccups glitch PWM/MM panels. A GC pause is
    # exactly such a hiccup, and this loop allocates nothing worth collecting.
    gc.disable()

    try:
        if args.stdin:
            run(JpegStream(sys.stdin.buffer.read), matrix, mapper, args)
            return

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.bind, args.port))
        server.listen(1)
        print("listening on %s:%d" % (args.bind, args.port), file=sys.stderr)

        while True:
            conn, peer = server.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("connected: %s:%d" % peer, file=sys.stderr)
            try:
                run(JpegStream(conn.recv), matrix, mapper, args)
            finally:
                conn.close()
                print("disconnected", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        matrix.close()


if __name__ == "__main__":
    main()
