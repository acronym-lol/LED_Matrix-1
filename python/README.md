# JPEG Bridge

Stream JPEG frames over a socket to a ColorLight 5A-75B / 5A-75E receiver card.

`jpeg_bridge.py` sits between your animation program and the daemon in this
repo. You send it JPEGs; it decodes them, arranges the pixels the way the
receiver card expects, and hands them to the daemon, which does the actual
Ethernet work.

```
your program ──JPEG over TCP──► jpeg_bridge.py ──shared memory──► daemon ──Ethernet──► panels
                                    (you)          /tmp/...mem      (root)
```

Anything that can write to a socket can drive the display: ffmpeg, a Python
script, a game engine, a shell pipeline. The bridge does not need root.

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

Whatever number you set here **must** match the daemon config in the next step.
There is no negotiation and no error if they disagree — you just get garbage.

## 2. Configure and start the daemon

Build it:

```bash
cd daemon
g++ -O3 PIC32MZ_NetCard.cpp Linux_NetCard.cpp main.cpp network.cpp -o Matrix -lpthread -lusb-1.0
```

Edit `daemon/config.txt`. Four lines per channel, no blank lines:

```
0 8080
eth0
192 192
0 0
```

| Line | Meaning |
|---|---|
| `0 8080` | channel number, then the daemon's own TCP port (unrelated to the bridge) |
| `eth0` | the NIC wired to the receiver card — check yours with `ip link` |
| `192 192` | rows then columns, matching the receiver card exactly |
| `0 0` | VLAN off, VLAN id unused |

> **End the file with a newline.** The config parser drops the last channel if
> the file ends without one — see the `!cfg.eof()` check in `main.cpp`.

Start it (root is required for raw Ethernet frames):

```bash
sudo ./Matrix config.txt
```

It daemonizes immediately and creates `/tmp/LED_Matrix-0.mem`, the shared
framebuffer. The bridge will not start until this file exists.

## 3. Install the bridge's dependencies

```bash
sudo apt install python3-numpy python3-pil
```

Pillow is doing the JPEG decoding through libjpeg-turbo — the same C library a
C++ version would use, so decoding runs at full native speed. numpy keeps the
pixel rearranging out of the Python interpreter. Both matter; see
[Performance](#performance).

## 4. Start the bridge

```bash
./python/jpeg_bridge.py
```

The defaults are already 192x192 with no remapping. It prints what it negotiated and waits:

```
receiver 192x192, canvas 192x192, mapping none
listening on 127.0.0.1:9000
```

The receiver size is read from the daemon, not from your flags, so if this line
does not say `192x192` your `config.txt` is wrong.

## 5. Send it frames

**From ffmpeg** — no code at all. Scale to the panel size in ffmpeg, not in the
bridge (this matters a lot, see [Performance](#performance)):

```bash
# a video file
ffmpeg -re -i clip.mp4 -vf scale=192:192 -f mjpeg -q:v 5 tcp://127.0.0.1:9000

# a webcam, cropped square
ffmpeg -f v4l2 -i /dev/video0 -vf "crop=min(iw\,ih):min(iw\,ih),scale=192:192" \
       -f mjpeg tcp://127.0.0.1:9000

# a still image, held on screen
ffmpeg -loop 1 -r 1 -i logo.png -vf scale=192:192 -f mjpeg tcp://127.0.0.1:9000
```

`-re` throttles playback to real time. Without it ffmpeg pushes frames as fast
as it can decode them.

**From Python** — send whole JPEGs back to back:

```python
import io, socket
from PIL import Image

sock = socket.create_connection(("127.0.0.1", 9000))

def show(img):                       # img is a 192x192 PIL Image
    blob = io.BytesIO()
    img.save(blob, "JPEG", quality=85)
    sock.sendall(blob.getvalue())

while True:
    show(render_next_frame())
```

**From a pipe** — skip the socket entirely:

```bash
my_animation | ./python/jpeg_bridge.py --stdin
```

---

## Options

| Flag | Default | Notes |
|---|---|---|
| `--canvas WxH` | `192x192` | size of the frames you are sending |
| `--mapping` | `none` | `none` sends pixels straight through. See below. |
| `--channel N` | `0` | which daemon channel, i.e. which `/tmp/LED_Matrix-N.mem` |
| `--port N` | `9000` | TCP port to listen on |
| `--bind ADDR` | `127.0.0.1` | use `0.0.0.0` to accept frames from other machines |
| `--stdin` | off | read the stream from stdin instead of a socket |
| `--fit` | `stretch` | `contain` preserves aspect ratio and letterboxes with black |
| `--brightness N` | unset | 0–100, applied once at startup |
| `--quiet` | off | suppress the throughput line printed every 5s |

### Framing

Auto-detected per connection, so you do not configure it:

- **Back-to-back JPEGs** — each frame starts `FF D8` and ends `FF D9`. This is
  what `ffmpeg -f mjpeg` emits and what the Python snippet above sends.
- **Length-prefixed** — a 4-byte big-endian length before each JPEG. Slightly
  more robust: a JPEG carrying an EXIF thumbnail can contain an early `FF D9`,
  which costs one dropped frame in the first mode before it resynchronizes.

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

Measured on Apple Silicon. A Pi 4 is roughly 3–5x slower, so divide.

| Workload | Decode | Map + copy | Total | Ceiling |
|---|---|---|---|---|
| **192x192 source → 192x192** | 0.203 ms | 0.071 ms | **0.274 ms** | ~3,650 fps |
| 128x16 source → 128x16 | 0.052 ms | 0.008 ms | 0.060 ms | ~16,700 fps |
| **1920x1080 source → 128x16** | **7.69 ms** | 0.005 ms | 7.695 ms | ~130 fps |

That third row is the one to pay attention to. Decoding a 1080p JPEG costs
about 150x more than everything else in the pipeline combined, because
entropy-decoding two million pixels is unavoidable work even when you throw
almost all of them away. On a Pi that is the difference between comfortable and
struggling. **Always scale to 192x192 in ffmpeg**, so the bridge only ever sees
small JPEGs.

The bridge is not your bottleneck. Two other things are:

- **The daemon polls at 60 Hz.** `const int FPS = 60` in `daemon/main.cpp` means
  every frame waits up to 16.6 ms for the daemon to notice it. This caps you
  near 60fps no matter how fast anything else is. Lowering that sleep is a
  one-line change.
- **Ethernet.** At 192x192 each frame is 192 packets of 597 bytes, about
  112 KB, so 60fps is **55 Mbit/s** and 30fps is **27 Mbit/s**. Fine on a Pi 4
  or 5 with real gigabit. A Pi 3 or earlier puts Ethernet behind USB2 at
  100 Mbit, where 60fps would be uncomfortably close to the ceiling — use 30fps
  there, or use a newer Pi.

### Wiring

These are raw L2 frames with hardcoded MAC addresses and non-IP ethertypes. A
normal switch cannot learn them and will flood the segment. In practice the
Pi's Ethernet port goes to the receiver card and nothing else, and you use WiFi
for everything else. If you must share the link, use the VLAN fields in
`config.txt`.

---

## Troubleshooting

**`/tmp/LED_Matrix-0.mem does not exist -- is the daemon running?`**
Start the daemon first; it creates the file. Check it survived startup with
`pgrep Matrix` — it daemonizes silently and exits silently on a bad config.

**`shared memory is N bytes but 192x192 needs 110596`**
The file is left over from a run at a different resolution. Stop the daemon,
`rm /tmp/LED_Matrix-0.mem`, start it again.

**`daemon did not answer command 3`**
The file exists but nothing is servicing it — a dead daemon from a previous
run. Same fix as above.

**The bridge prints a different receiver size than 192x192.**
`config.txt` disagrees with what you think it says. Remember it is
rows-then-columns, and that a missing trailing newline silently drops the last
channel.

**Nothing on the panels, but the bridge reports frames flowing.**
The daemon is sending to the wrong NIC, or the cable is on the wrong port. Check
the interface name in `config.txt` against `ip link`, and confirm packets are
actually leaving:
`sudo tcpdump -i eth0 -c 10 'ether proto 0x0107 or ether[12:2] > 0x5500'`

**The image is scrambled, sliced or offset.**
Receiver card configuration, not this software. The card's resolution in
LEDVision must match `config.txt`, and the panel chain layout in LEDVision must
match how the panels are physically wired.

**Occasional torn frames.** The framebuffer is single-buffered — the daemon can
be reading it while the bridge writes the next frame. The bridge minimizes the
window by decoding into scratch memory and doing one copy in, but eliminating
it needs double buffering in the daemon.

**Random flickering on PWM/MM panels.** Not a bug in the bridge. The main
README documents that MBI5153-class panels glitch when frames change
continuously from userspace Linux, because the timing is not tight enough. The
bridge calls `gc.disable()` to remove one source of pauses, but the underlying
issue is in how Linux paces the packets. Non-PWM panels are unaffected.

---

## Quick reference

```bash
# terminal 1
cd daemon && sudo ./Matrix config.txt

# terminal 2
./python/jpeg_bridge.py

# terminal 3
ffmpeg -re -i clip.mp4 -vf scale=192:192 -f mjpeg tcp://127.0.0.1:9000
```
