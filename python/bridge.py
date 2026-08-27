#!/usr/bin/env python3
"""
JPEG -> ColorLight receiver card bridge, no daemon.

Accepts a stream of JPEG frames (TCP socket or stdin), decodes them, maps
them onto the receiver card's pixel layout, and sends them straight to the
card over a raw AF_PACKET Ethernet socket. There is no daemon and no shared
memory -- this process alone owns the NIC and speaks the wire protocol.

Wire protocol (reverse engineered from daemon/Linux_NetCard.cpp's
send_frame(); dst MAC 11:22:33:44:55:66, src MAC 22:22:33:44:55:66, plain
Ethernet frames, no IP/UDP, no 802.1Q):

  1. Row-data frames, one or more per row (split into chunks of at most
     COLS_PER_PKT columns):
       ethertype = 0x5500 | (row >> 8)
       payload   = row & 0xFF
                 | col_offset (u16 BE)
                 | chunk_len  (u16 BE)
                 | 0x08 0x88
                 | chunk_len * 3 bytes of BGR pixel data
  2. An "enable"/latch frame, ethertype 0x0107, 98-byte zero payload except:
       byte[21] = byte[24] = byte[25] = byte[26] = raw brightness (0-255)
       byte[22] = 0x05
  3. A brightness frame, ethertype 0x0A00 + gamma_brightness, 63-byte zero
     payload except:
       byte[0] = byte[1] = gamma-corrected brightness (0-255)
       byte[2] = 0xFF

Frames 2 and 3 must be sent after all row data for a frame -- they act as
the vsync/latch + brightness commit. All packets belonging to one still
frame are handed to the kernel in a single sendmmsg() batch (via ctypes --
Python's socket module doesn't expose it) rather than one send() per packet,
so the scheduler cannot preempt mid-frame and mix old/new row data.

Requires root (or CAP_NET_RAW) to open the raw socket -- run this as root,
or as the only network-facing half of the pipeline; the sender half
(three_bouncing_balls.py etc.) does not need any privilege.

Framing on the TCP/stdin side is auto-detected from the first bytes of the
stream:
  * back-to-back JPEGs (starts with FFD8) -- what "ffmpeg -f mjpeg" produces
  * 4-byte big-endian length prefix before each JPEG

Example:
  sudo ./bridge.py --iface eth0 &
  ffmpeg -re -i clip.mp4 -vf scale=192:192 -f mjpeg tcp://127.0.0.1:9000
"""

import argparse
import ctypes
import gc
import io
import os
import socket
import struct
import sys
import time

import numpy as np
from PIL import Image

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

MAX_FRAME_BYTES = 32 << 20

DST_MAC = bytes.fromhex("112233445566")
SRC_MAC = bytes.fromhex("222233445566")
COLS_PER_PKT = 497  # keeps a single row-chunk frame under ~1.5KB


class _Iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


class _Msghdr(ctypes.Structure):
    _fields_ = [
        ("msg_name", ctypes.c_void_p),
        ("msg_namelen", ctypes.c_uint32),
        ("msg_iov", ctypes.POINTER(_Iovec)),
        ("msg_iovlen", ctypes.c_size_t),
        ("msg_control", ctypes.c_void_p),
        ("msg_controllen", ctypes.c_size_t),
        ("msg_flags", ctypes.c_int),
    ]


class _Mmsghdr(ctypes.Structure):
    _fields_ = [("msg_hdr", _Msghdr), ("msg_len", ctypes.c_uint)]


_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.sendmmsg.restype = ctypes.c_int
_libc.sendmmsg.argtypes = [ctypes.c_int, ctypes.POINTER(_Mmsghdr), ctypes.c_uint, ctypes.c_uint]


def _sendmmsg_batch(fd, frames):
    """Hand every frame (a list of bytes objects) to the kernel in one syscall."""
    n = len(frames)
    if n == 0:
        return

    # ctypes.create_string_buffer copies do own the memory, so these can be
    # discarded once sendmmsg() returns -- keep them alive until then.
    bufs = [ctypes.create_string_buffer(data, len(data)) for data in frames]
    iovecs = (_Iovec * n)()
    mmsgs = (_Mmsghdr * n)()
    for i, buf in enumerate(bufs):
        iovecs[i].iov_base = ctypes.cast(buf, ctypes.c_void_p)
        iovecs[i].iov_len = len(frames[i])
        mmsgs[i].msg_hdr.msg_iov = ctypes.pointer(iovecs[i])
        mmsgs[i].msg_hdr.msg_iovlen = 1

    sent = _libc.sendmmsg(fd, mmsgs, n, 0)
    if sent != n:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


class RawMatrix:
    """Talks directly to a 5A-75-style receiver card over a raw Ethernet
    socket. No daemon, no shared memory."""

    def __init__(self, iface, rows, cols, brightness=100):
        self.rows = rows
        self.cols = cols
        self._raw_brightness = 0
        self._gamma_brightness = 0
        self.set_brightness(brightness)

        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        self.sock.bind((iface, 0))

    def set_brightness(self, percent):
        percent = max(0, min(100, percent)) / 100.0
        self._raw_brightness = round(percent * 255)
        self._gamma_brightness = round((percent ** 0.405) * 255)

    @staticmethod
    def _eth_header(ethertype):
        return DST_MAC + SRC_MAC + struct.pack(">H", ethertype & 0xFFFF)

    def _enable_frame(self):
        payload = bytearray(98)
        payload[21] = self._raw_brightness
        payload[22] = 0x05
        payload[24] = payload[25] = payload[26] = self._raw_brightness
        return self._eth_header(0x0107) + bytes(payload)

    def _brightness_frame(self):
        payload = bytearray(63)
        payload[0] = payload[1] = self._gamma_brightness
        payload[2] = 0xFF
        return self._eth_header(0x0A00 + self._gamma_brightness) + bytes(payload)

    def _row_frames(self, bgr):
        """bgr: (rows, cols, 3) uint8 array, already in blue/green/red order."""
        frames = []
        for row in range(self.rows):
            offset = 0
            remaining = self.cols
            while remaining > 0:
                chunk = min(COLS_PER_PKT, remaining)
                header = bytes((
                    row & 0xFF,
                    (offset >> 8) & 0xFF, offset & 0xFF,
                    (chunk >> 8) & 0xFF, chunk & 0xFF,
                    0x08, 0x88,
                ))
                pixels = bgr[row, offset:offset + chunk].tobytes()
                frames.append(self._eth_header(0x5500 | (row >> 8)) + header + pixels)
                offset += chunk
                remaining -= chunk
        return frames

    def send_frame(self, rgb):
        """rgb: (rows, cols, 3) uint8 array in RGB order (e.g. from PIL)."""
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        fd = self.sock.fileno()
        _sendmmsg_batch(fd, self._row_frames(bgr))
        _sendmmsg_batch(fd, [self._enable_frame(), self._brightness_frame()])

    def close(self):
        self.sock.close()


def build_mapper(canvas_w, canvas_h, recv_w, recv_h, mode):
    """Return f(rgb_array) -> (recv_h, recv_w, 3) RGB array ready to send.

    'blocks' reproduces Matrix::map_pixel from C++/Matrix.cpp: a wide canvas
    is cut into full-width-of-the-receiver blocks which stack vertically,
    last block on top. The stock 16x128 sign on a 64x32 receiver is 4 such
    blocks.
    """
    if mode == "none":
        if (canvas_w, canvas_h) != (recv_w, recv_h):
            raise SystemExit(
                "--mapping none needs the canvas to equal the receiver: "
                "canvas is %dx%d, receiver is %dx%d (WxH)"
                % (canvas_w, canvas_h, recv_w, recv_h)
            )
        return lambda src: src

    if canvas_w % recv_w:
        raise SystemExit(
            "canvas width %d is not a multiple of the receiver width %d"
            % (canvas_w, recv_w)
        )
    nblocks = canvas_w // recv_w
    if canvas_h * nblocks != recv_h:
        raise SystemExit(
            "canvas %dx%d splits into %d blocks of height %d = %d rows, but the "
            "receiver has %d rows"
            % (canvas_w, canvas_h, nblocks, canvas_h, canvas_h * nblocks, recv_h)
        )

    buf = np.empty((recv_h, recv_w, 3), dtype=np.uint8)
    moves = []
    for b in range(nblocks):
        y0 = (nblocks - 1 - b) * canvas_h
        moves.append((slice(y0, y0 + canvas_h), slice(b * recv_w, (b + 1) * recv_w)))

    def apply(src):
        for dst_rows, src_cols in moves:
            buf[dst_rows, :, :] = src[:, src_cols, :]
        return buf

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

        matrix.send_frame(mapper(src))
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
    ap = argparse.ArgumentParser(description="Stream JPEG frames straight to a ColorLight receiver card.")
    ap.add_argument("--iface", required=True, help="NIC wired to the receiver card, e.g. eth0")
    ap.add_argument("--port", type=int, default=9000, help="TCP port to listen on (default 9000)")
    ap.add_argument("--bind", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    ap.add_argument("--stdin", action="store_true", help="read the stream from stdin instead of a socket")
    ap.add_argument("--canvas", default="192x192", help="size of the frames you send, as WxH (default 192x192)")
    ap.add_argument("--receiver", default=None,
                    help="receiver card resolution as WxH (default: same as --canvas). "
                         "Must match how the card is configured in LEDVision.")
    ap.add_argument("--mapping", choices=["blocks", "none"], default="none",
                    help="'blocks' matches Matrix::map_pixel; 'none' writes straight through")
    ap.add_argument("--fit", choices=["stretch", "contain"], default="stretch",
                    help="how to fit a mismatched source (default stretch)")
    ap.add_argument("--brightness", type=int, default=100, help="panel brightness 0-100 (default 100)")
    ap.add_argument("--quiet", action="store_true", help="do not print throughput")
    args = ap.parse_args()

    try:
        args.canvas_w, args.canvas_h = (int(v) for v in args.canvas.lower().split("x"))
    except ValueError:
        raise SystemExit("--canvas wants WxH, e.g. 128x16")

    if args.receiver is None:
        recv_w, recv_h = args.canvas_w, args.canvas_h
    else:
        try:
            recv_w, recv_h = (int(v) for v in args.receiver.lower().split("x"))
        except ValueError:
            raise SystemExit("--receiver wants WxH, e.g. 192x192")

    matrix = RawMatrix(args.iface, rows=recv_h, cols=recv_w, brightness=args.brightness)
    mapper = build_mapper(args.canvas_w, args.canvas_h, recv_w, recv_h, args.mapping)

    print(
        "receiver %dx%d, canvas %dx%d, mapping %s, iface %s"
        % (recv_w, recv_h, args.canvas_w, args.canvas_h, args.mapping, args.iface),
        file=sys.stderr,
    )

    # A GC pause mid-frame is exactly the kind of timing hiccup that glitches
    # PWM/MM panels, and this loop allocates nothing worth collecting.
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
