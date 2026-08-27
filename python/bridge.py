#!/usr/bin/env python3
"""
Raw pixel buffer -> ColorLight receiver card bridge, no daemon.

Accepts a stream of fixed-size raw RGB frames (TCP socket or stdin) and sends
them straight to the card over a raw AF_PACKET Ethernet socket. No JPEG, no
compression, no decode step -- each frame is exactly canvas_w * canvas_h * 3
bytes, row-major, 3 bytes/pixel RGB (e.g. straight from
`np.asarray(pil_image, dtype=np.uint8).tobytes()`). There is no daemon and no
shared memory -- this process alone owns the NIC and speaks the wire protocol.

Because there's no header and no delimiter, framing is just "read exactly
canvas_w * canvas_h * 3 bytes, repeat" -- the sender MUST emit exactly that
many bytes per frame, every frame. A wrongly-sized frame desyncs the stream
permanently (every frame after it gets torn across the wrong boundaries)
until the connection is dropped and reconnected.

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

Example:
  sudo ./bridge.py --iface eth0 &
  ffmpeg -re -i clip.mp4 -vf scale=192:192 -pix_fmt rgb24 -f rawvideo tcp://127.0.0.1:9000
"""

import argparse
import ctypes
import gc
import os
import socket
import struct
import sys
import time

import numpy as np

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


class RawMatrix:
    """Talks directly to a 5A-75-style receiver card over a raw Ethernet
    socket. No daemon, no shared memory.

    Mirrors daemon/Linux_NetCard.cpp's send_frame(): that C++ code builds a
    2-iovec sendmmsg() scatter/gather per row (a tiny header buffer plus a
    pointer straight into the mmap'd shared-memory framebuffer for the pixel
    bytes) and does zero per-frame allocation. Our first pass at this instead
    rebuilt every packet and every ctypes structure from scratch each call
    (~1.6ms just for packet construction, ~5.9ms per send_frame() total on a
    Pi) -- slow enough, held open long enough on a still frame, that it could
    race the receiver card's own scan-out and tear.

    So: everything that doesn't change frame to frame -- header bytes, the
    ctypes iovec/mmsghdr arrays -- is built once here at construction time.
    self._bgr is a persistent buffer that the row-packet iovecs point into
    directly; send_frame() only overwrites it in place and fires two
    sendmmsg() syscalls. No per-frame allocation, no per-packet Python object
    construction.
    """

    def __init__(self, iface, rows, cols, brightness=100):
        self.rows = rows
        self.cols = cols
        self._raw_brightness = 0
        self._gamma_brightness = 0

        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        self.sock.bind((iface, 0))

        # Overwritten in place every send_frame() call -- never reassigned,
        # so its underlying memory address (and the iovec pointers into it
        # built below) stay valid for the life of this object.
        self._bgr = np.zeros((rows, cols, 3), dtype=np.uint8)
        bgr_base = self._bgr.ctypes.data

        chunks = []
        for row in range(rows):
            offset = 0
            remaining = cols
            while remaining > 0:
                chunk = min(COLS_PER_PKT, remaining)
                chunks.append((row, offset, chunk))
                offset += chunk
                remaining -= chunk
        n = len(chunks)

        # 2 iovecs per row packet (header, pixels-straight-from-self._bgr),
        # plus 1 each for the enable/brightness packets at the tail.
        self._iovecs = (_Iovec * (2 * n + 2))()
        self._row_msgs = (_Mmsghdr * n)()
        self._tail_msgs = (_Mmsghdr * 2)()
        # ctypes.create_string_buffer copies own their memory -- keep them
        # alive here, since the iovecs above only borrow pointers into them.
        self._header_bufs = []

        for i, (row, offset, chunk) in enumerate(chunks):
            header = self._eth_header(0x5500 | (row >> 8)) + bytes((
                row & 0xFF,
                (offset >> 8) & 0xFF, offset & 0xFF,
                (chunk >> 8) & 0xFF, chunk & 0xFF,
                0x08, 0x88,
            ))
            buf = ctypes.create_string_buffer(header, len(header))
            self._header_bufs.append(buf)

            self._iovecs[2 * i].iov_base = ctypes.cast(buf, ctypes.c_void_p)
            self._iovecs[2 * i].iov_len = len(header)
            self._iovecs[2 * i + 1].iov_base = bgr_base + (row * cols + offset) * 3
            self._iovecs[2 * i + 1].iov_len = chunk * 3

            self._row_msgs[i].msg_hdr.msg_iov = ctypes.pointer(self._iovecs[2 * i])
            self._row_msgs[i].msg_hdr.msg_iovlen = 2

        self._tail_iov_base = 2 * n
        for i in range(2):
            self._tail_msgs[i].msg_hdr.msg_iov = ctypes.pointer(self._iovecs[self._tail_iov_base + i])
            self._tail_msgs[i].msg_hdr.msg_iovlen = 1

        self.set_brightness(brightness)

    def set_brightness(self, percent):
        percent = max(0, min(100, percent)) / 100.0
        self._raw_brightness = round(percent * 255)
        self._gamma_brightness = round((percent ** 0.405) * 255)

        # Fixed for the life of the process in normal use, but rebuildable if
        # something calls this again later -- cheap either way, only 2 packets.
        enable = self._enable_frame()
        brightness_pkt = self._brightness_frame()
        self._enable_buf = ctypes.create_string_buffer(enable, len(enable))
        self._brightness_buf = ctypes.create_string_buffer(brightness_pkt, len(brightness_pkt))

        base = self._tail_iov_base
        self._iovecs[base].iov_base = ctypes.cast(self._enable_buf, ctypes.c_void_p)
        self._iovecs[base].iov_len = len(enable)
        self._iovecs[base + 1].iov_base = ctypes.cast(self._brightness_buf, ctypes.c_void_p)
        self._iovecs[base + 1].iov_len = len(brightness_pkt)

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

    def send_frame(self, rgb):
        """rgb: (rows, cols, 3) uint8 array in RGB order (e.g. from PIL)."""
        self._bgr[:] = rgb[:, :, ::-1]
        fd = self.sock.fileno()

        n = len(self._row_msgs)
        sent = _libc.sendmmsg(fd, self._row_msgs, n, 0)
        if sent != n:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))

        sent = _libc.sendmmsg(fd, self._tail_msgs, 2, 0)
        if sent != 2:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))

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


class RawFrameStream:
    """Pulls fixed-size raw RGB frames out of a byte stream. No header, no
    delimiter -- the frame size is known up front from --canvas."""

    def __init__(self, recv, frame_bytes):
        self._recv = recv
        self._frame_bytes = frame_bytes
        self._buf = bytearray()

    def _fill(self):
        chunk = self._recv(65536)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def frames(self):
        while True:
            while len(self._buf) < self._frame_bytes:
                if not self._fill():
                    return
            payload = bytes(self._buf[:self._frame_bytes])
            del self._buf[:self._frame_bytes]
            yield payload


def run(stream, matrix, mapper, canvas_w, canvas_h, quiet, min_frame_interval):
    frames = 0
    last = time.monotonic()
    last_send = 0.0

    for payload in stream.frames():
        if min_frame_interval:
            wait = min_frame_interval - (time.monotonic() - last_send)
            if wait > 0:
                time.sleep(wait)

        src = np.frombuffer(payload, dtype=np.uint8).reshape(canvas_h, canvas_w, 3)
        matrix.send_frame(mapper(src))
        last_send = time.monotonic()
        frames += 1

        if not quiet:
            now = time.monotonic()
            if now - last >= 5.0:
                print("%.1f fps (%d frames)" % (frames / (now - last), frames), file=sys.stderr)
                frames = 0
                last = now


def main():
    ap = argparse.ArgumentParser(description="Stream raw RGB pixel buffers straight to a ColorLight receiver card.")
    ap.add_argument("--iface", required=True, help="NIC wired to the receiver card, e.g. eth0")
    ap.add_argument("--port", type=int, default=9000, help="TCP port to listen on (default 9000)")
    ap.add_argument("--bind", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    ap.add_argument("--stdin", action="store_true", help="read the stream from stdin instead of a socket")
    ap.add_argument("--canvas", default="192x192",
                    help="exact size of every frame you send, as WxH (default 192x192). "
                         "Frames are raw RGB, canvas_w*canvas_h*3 bytes each, no header.")
    ap.add_argument("--receiver", default=None,
                    help="receiver card resolution as WxH (default: same as --canvas). "
                         "Must match how the card is configured in LEDVision.")
    ap.add_argument("--mapping", choices=["blocks", "none"], default="none",
                    help="'blocks' matches Matrix::map_pixel; 'none' writes straight through")
    ap.add_argument("--brightness", type=int, default=100, help="panel brightness 0-100 (default 100)")
    ap.add_argument("--max-fps", type=float, default=60.0,
                    help="cap on frames sent per second (default 60, 0 = unlimited). "
                         "Protects the receiver card from frames landing faster than it can "
                         "latch them, which is what causes tearing.")
    ap.add_argument("--quiet", action="store_true", help="do not print throughput")
    args = ap.parse_args()

    try:
        canvas_w, canvas_h = (int(v) for v in args.canvas.lower().split("x"))
    except ValueError:
        raise SystemExit("--canvas wants WxH, e.g. 128x16")

    if args.receiver is None:
        recv_w, recv_h = canvas_w, canvas_h
    else:
        try:
            recv_w, recv_h = (int(v) for v in args.receiver.lower().split("x"))
        except ValueError:
            raise SystemExit("--receiver wants WxH, e.g. 192x192")

    matrix = RawMatrix(args.iface, rows=recv_h, cols=recv_w, brightness=args.brightness)
    mapper = build_mapper(canvas_w, canvas_h, recv_w, recv_h, args.mapping)
    frame_bytes = canvas_w * canvas_h * 3
    min_frame_interval = 1.0 / args.max_fps if args.max_fps > 0 else 0.0

    print(
        "receiver %dx%d, canvas %dx%d (%d bytes/frame, raw RGB), mapping %s, iface %s, max %s fps"
        % (recv_w, recv_h, canvas_w, canvas_h, frame_bytes, args.mapping, args.iface,
           args.max_fps if args.max_fps > 0 else "unlimited"),
        file=sys.stderr,
    )

    # A GC pause mid-frame is exactly the kind of timing hiccup that glitches
    # PWM/MM panels, and this loop allocates nothing worth collecting.
    gc.disable()

    try:
        if args.stdin:
            run(RawFrameStream(sys.stdin.buffer.read, frame_bytes), matrix, mapper, canvas_w, canvas_h,
                args.quiet, min_frame_interval)
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
                run(RawFrameStream(conn.recv, frame_bytes), matrix, mapper, canvas_w, canvas_h,
                    args.quiet, min_frame_interval)
            finally:
                conn.close()
                print("disconnected", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        matrix.close()


if __name__ == "__main__":
    main()
