# Raw Bridge

Stream raw RGB pixel buffers over a socket to a ColorLight 5A-75B / 5A-75E
receiver card.

`bridge.py` sits between your animation program and the receiver card. You
send it raw pixels; it arranges them the way the receiver card expects and
sends them straight out over a raw Ethernet socket. No JPEG, no compression,
no daemon, no shared memory -- `bridge.py` is the only thing that touches the
NIC, and there is no encode/decode step anywhere in the pipeline.

```
your program ──raw RGB over TCP──► bridge.py ──raw Ethernet──► panels
                                     (root)
```

Anything that can write to a socket can drive the display: ffmpeg, a Python
script, a game engine, a shell pipeline. Only `bridge.py` itself needs root
(or `CAP_NET_RAW`) -- your program does not.

---

## 1. Configure the receiver card

Use ColorLight's **LEDVision** software (Windows) to tell the card how your
panels are physically wired, and set the card's resolution to match your
display. For a 192x192 wall, configure the card as **192 rows x 192 columns**.

192x192 is 36,864 pixels. That fits comfortably on one card:

| Card | Max resolution | 192x192 fits? |
|---|---|---|
| 5A-75B | 256 x 256 | yes, 56% used |
| 5A-75E | 512 x 256 | yes, 28% used |

How you divide 192x192 across the card's HUB75 output connectors (say nine
64x64 panels as three chains of three) is LEDVision's job, not this software's.
Once the card is configured, it presents a single flat 192x192 raster and the
bridge writes to that.

Whatever number you set here **must** match `bridge.py`'s `--receiver` flag
(or `--canvas`, if you leave `--receiver` unset). There is no negotiation and
no error if they disagree -- you just get garbage.

## 2. Install the bridge's dependencies

```bash
sudo apt install python3-numpy python3-pil
```

numpy does the pixel rearranging (block mapping) out of the Python
interpreter. Pillow is only needed by the example senders (`bouncing_ball.py`
etc.) to draw frames -- `bridge.py` itself doesn't touch it, since there's no
image format to decode.

## 3. Start the bridge

```bash
sudo ./python/bridge.py --iface eth0
```

`--iface` is the NIC wired to the receiver card -- check yours with `ip link`.
This needs root because it opens a raw `AF_PACKET` socket.

The defaults are already 192x192 with no remapping. It prints what it's using
and waits:

```
receiver 192x192, canvas 192x192 (110592 bytes/frame, raw RGB), mapping none, iface eth0
listening on 127.0.0.1:9000
```

## 4. Send it frames

Every frame is **exactly** `canvas_w * canvas_h * 3` bytes: row-major, 3 bytes
per pixel, RGB. There is no header and no delimiter -- the bridge just reads
that many bytes, sends them, and reads that many again. **Every frame must be
exactly that size.** A wrongly-sized frame desyncs the stream permanently
(every following frame gets torn across the wrong byte boundaries) until you
reconnect.

**From ffmpeg** -- no code at all. Scale to the panel size in ffmpeg, and
output `rawvideo` in `rgb24`:

```bash
# a video file
ffmpeg -re -i clip.mp4 -vf scale=192:192 -pix_fmt rgb24 -f rawvideo tcp://127.0.0.1:9000

# a webcam, cropped square
ffmpeg -f v4l2 -i /dev/video0 -vf "crop=min(iw\,ih):min(iw\,ih),scale=192:192" \
       -pix_fmt rgb24 -f rawvideo tcp://127.0.0.1:9000

# a still image, held on screen
ffmpeg -loop 1 -r 30 -i logo.png -vf scale=192:192 -pix_fmt rgb24 -f rawvideo tcp://127.0.0.1:9000
```

`-re` throttles playback to real time. Without it ffmpeg pushes frames as fast
as it can decode them.

**From Python** -- send raw pixels back to back:

```python
import socket
import numpy as np

sock = socket.create_connection(("127.0.0.1", 9000))

def show(img):                       # img is a 192x192 PIL Image, mode "RGB"
    sock.sendall(np.asarray(img, dtype=np.uint8).tobytes())

while True:
    show(render_next_frame())
```

`bouncing_ball.py`, `flash_rgb.py` and `three_bouncing_balls.py` in this
directory are working examples of this.

**From a pipe** -- skip the socket entirely:

```bash
my_animation | sudo ./python/bridge.py --iface eth0 --stdin
```

---

## Options

| Flag | Default | Notes |
|---|---|---|
| `--iface IFACE` | *(required)* | NIC wired to the receiver card, e.g. `eth0` |
| `--canvas WxH` | `192x192` | exact size of every frame you send; each frame is `W*H*3` raw RGB bytes |
| `--receiver WxH` | same as `--canvas` | the receiver card's actual configured resolution |
| `--mapping` | `none` | `none` sends pixels straight through. See below. |
| `--port N` | `9000` | TCP port to listen on |
| `--bind ADDR` | `127.0.0.1` | use `0.0.0.0` to accept frames from other machines |
| `--stdin` | off | read the stream from stdin instead of a socket |
| `--brightness N` | `100` | 0-100, fixed for the life of the process |
| `--quiet` | off | suppress the throughput line printed every 5s |

### `--mapping none` vs `blocks`

This is the default. Your canvas is 192x192 and the card is configured 192x192, so
pixels go straight through.

`blocks` exists for the stock configuration this repo was built around: a long
thin 16x128 sign driven by a card configured as 64x32, because splitting the
sign across four short parallel chains gives better refresh than one long one.
It slices a wide canvas into receiver-width blocks and stacks them vertically,
reproducing `Matrix::map_pixel` in `C++/Matrix.cpp`. You would only want it if
you deliberately configure the card to a different shape than your artwork.

Mismatched geometry is rejected at startup with the actual numbers, rather than
silently displaying nonsense.

---

## Performance

There is no decode step -- a raw frame goes straight from the socket into a
numpy array. The only per-frame cost is the block-mapping copy (a no-op memcpy
in `--mapping none`) and building/sending the Ethernet packets, which
`bridge.py` batches into a single `sendmmsg()` syscall per frame so the kernel
transmits every packet in a still frame back-to-back.

The practical ceiling is then almost entirely the wire:

- **Ethernet.** At 192x192 each frame is 192 packets of 597 bytes, about
  112 KB, so 60fps is **55 Mbit/s** and 30fps is **27 Mbit/s**. Fine on a Pi 4
  or 5 with real gigabit. A Pi 3 or earlier puts Ethernet behind USB2 at
  100 Mbit, where 60fps would be uncomfortably close to the ceiling -- use 30fps
  there, or use a newer Pi.
- **TCP throughput for the raw stream itself.** At 192x192 and 60fps the
  raw pixel stream between your program and `bridge.py` is about **53 Mbit/s**
  (110,592 bytes/frame). That's trivial over loopback; only matters if you
  push frames over a real network with `--bind 0.0.0.0`.

### Wiring

These are raw L2 frames with hardcoded MAC addresses and non-IP ethertypes. A
normal switch cannot learn them and will flood the segment. In practice the
Pi's Ethernet port goes to the receiver card and nothing else, and you use WiFi
for everything else.

---

## Troubleshooting

**`PermissionError` / `Operation not permitted` on startup.**
`bridge.py` needs root (or `CAP_NET_RAW`) to open a raw socket. Run it with
`sudo`.

**The bridge prints a different receiver size than you expected.**
`--receiver` (or `--canvas`, if `--receiver` is unset) disagrees with what you
think it says. Remember it's WxH, and it must match what LEDVision has the
card configured to.

**The image tears, smears or scrolls sideways after the first frame or two.**
Your sender is emitting the wrong number of bytes per frame -- check it's
sending exactly `canvas_w * canvas_h * 3` bytes every time (no JPEG encoding,
no extra header). Once the stream desyncs like this the only fix is to
reconnect.

**Nothing on the panels, but the bridge reports frames flowing.**
Check the interface name against `ip link`, and confirm packets are actually
leaving:
`sudo tcpdump -i eth0 -c 10 'ether proto 0x0107 or ether[12:2] > 0x5500'`

**The image is scrambled, sliced or offset.**
Receiver card configuration, not this software. The card's resolution in
LEDVision must match `--receiver`, and the panel chain layout in LEDVision must
match how the panels are physically wired.

**Random flickering on PWM/MM panels.** Not a bug in the bridge. The main
README documents that MBI5153-class panels glitch when frames change
continuously from userspace Linux, because the timing is not tight enough.
`bridge.py` calls `gc.disable()` and batches every frame into one `sendmmsg()`
call to remove two sources of jitter, but the underlying issue is in how Linux
paces the packets. Non-PWM panels are unaffected.

---

## Quick reference

```bash
# terminal 1
sudo ./python/bridge.py --iface eth0

# terminal 2
ffmpeg -re -i clip.mp4 -vf scale=192:192 -pix_fmt rgb24 -f rawvideo tcp://127.0.0.1:9000
```
