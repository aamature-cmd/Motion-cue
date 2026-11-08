"""
Motion-cue overlay for Windows: an automotive ceramic-frit edge pattern that drifts with the motion
of a phone. Motion arrives as UDP datagrams from the Android app in android/.

  pip install PyQt5
  python overlay.py                          # listen on UDP 8765
  python overlay.py --dots 240 --stats       # tune density, print packet statistics
  python overlay.py --lateral x --forward z- # phone upright in a car mount, screen facing you

Windows Firewall must allow inbound UDP on the port (see README.md).
"""
import argparse
import ctypes
import math
import random
import signal
import socket
import struct
import sys
import threading
import time

# ---- Wire format (little-endian). Must match StreamService.kt ---------------------------------
HEADER = struct.Struct("<2sBBBBI")  # magic, version, flags, frame count, reserved, sequence (10 bytes)
FRAME = struct.Struct("<Q10f")      # sensor ts (ns), accel xyz, gyro xyz, rotation quaternion xyzw (48 bytes)
MAGIC, VERSION = b"MO", 1
FLAG_GYRO, FLAG_ROT = 1, 2
GRAVITY = 9.80665

# ---- Frit pattern -------------------------------------------------------------------------------
FRIT_DOTS = 300            # 150-300 reads as a fine speckle on 1080p; raise it for a denser band
FRIT_BAND_FRACTION = 0.085  # depth of the pattern as a fraction of the shorter screen side
FRIT_RGB = (0, 0, 0)       # real frit is black
EDGE_POWER = 2.2           # >1 pushes dots toward the screen edge
ALPHA_MAX = 190            # opacity (0-255) of a dot sitting on the edge
ALPHA_FALLOFF = 1.6        # how fast opacity fades toward the interior
R_MIN, R_MAX = 1.0, 3.0    # dot radius in pixels; larger near the edge, smaller toward the interior
FRIT_SEED = 7              # fixed seed: the same pattern every launch

# ---- Motion mapping -----------------------------------------------------------------------------
ACCEL_GAIN = 25.0     # pixels of drift per m/s^2
MAX_OFFSET = 110.0    # clamp on drift (pixels)
SMOOTH_TAU = 0.08     # lerp time constant in seconds: lower = snappier, higher = smoother
SIGN_LATERAL, SIGN_FORWARD = -1, +1  # dots drift opposite to the vehicle's acceleration
DEADZONE = 0.12       # m/s^2 ignored as sensor noise
STALE_S = 0.5         # no datagram for this long -> dots glide back to rest


# ---- Protocol -----------------------------------------------------------------------------------
def decode(data):
    """Return (seq, flags, frames) for a valid datagram, otherwise None."""
    if len(data) < HEADER.size:
        return None
    magic, version, flags, count, _reserved, seq = HEADER.unpack_from(data, 0)
    if magic != MAGIC or version != VERSION or count == 0:
        return None
    if len(data) < HEADER.size + count * FRAME.size:
        return None
    frames = [FRAME.unpack_from(data, HEADER.size + i * FRAME.size) for i in range(count)]
    return seq, flags, frames


def to_linear(ax, ay, az, qx, qy, qz, qw):
    """Remove gravity from the accelerometer using the rotation-vector quaternion.

    The third row of the device-to-world rotation matrix is gravity's direction in the device frame.
    """
    gx = 2 * (qx * qz - qw * qy)
    gy = 2 * (qy * qz + qw * qx)
    gz = 1 - 2 * (qx * qx + qy * qy)
    return ax - GRAVITY * gx, ay - GRAVITY * gy, az - GRAVITY * gz


class Motion:
    """Latest sample. Written by the receiver thread, read by the Qt timer (latest value wins)."""

    def __init__(self):
        self.lin = (0.0, 0.0, 0.0)  # linear acceleration in the device frame (raw accel if no rotation vector)
        self.have_rot = False
        self.stamp = 0.0
        self.last_seq = None
        self.lost = 0


motion = Motion()


def receiver(port, show_stats):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        print(f"cannot listen on UDP {port}: {exc}")
        return
    sock.settimeout(1.0)
    win_start, win_count, win_lost = time.time(), 0, 0
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except (socket.timeout, ConnectionResetError):
            continue
        except OSError:
            return
        decoded = decode(data)
        if decoded is None:
            continue
        seq, flags, frames = decoded
        now = time.time()
        if motion.last_seq is None or now - motion.stamp > 2.0:
            print(f"phone streaming from {addr[0]}")  # new session: the sequence may have restarted
        else:
            delta = (seq - motion.last_seq) & 0xFFFFFFFF
            if delta == 0 or delta >= 0x80000000:
                continue  # duplicate or out of order
            motion.lost += delta - 1
            win_lost += delta - 1
        motion.last_seq = seq

        _ts, ax, ay, az, _gx, _gy, _gz, qx, qy, qz, qw = frames[-1]
        norm2 = qx * qx + qy * qy + qz * qz + qw * qw
        have_rot = bool(flags & FLAG_ROT) and 0.9 < norm2 < 1.1
        motion.lin = to_linear(ax, ay, az, qx, qy, qz, qw) if have_rot else (ax, ay, az)
        motion.have_rot = have_rot
        motion.stamp = now

        win_count += 1
        if show_stats and now - win_start >= 5.0:
            total = win_count + win_lost
            print(f"{win_count / (now - win_start):.0f} datagrams/s, lost {win_lost} ({100 * win_lost / max(total, 1):.1f}%)")
            win_start, win_count, win_lost = now, 0, 0


# ---- Frit pattern (pure Python, no Qt) ----------------------------------------------------------
def generate_frit(width, height, count, band, seed=FRIT_SEED):
    """Return [(side, along, fixed, parallax, radius, alpha)] for dots scattered around the screen border.

    side: 0 left, 1 right, 2 top, 3 bottom. `fixed` is the dot's coordinate across the band; `along` is
    its coordinate along the edge, the only one that changes when the overlay reacts to motion.
    """
    rng = random.Random(seed)
    lengths = (height, height, width, width)
    dots = []
    for _ in range(count):
        side = rng.choices(range(4), weights=lengths)[0]  # uniform density around the perimeter
        along = rng.uniform(0, lengths[side])
        depth = band * rng.random() ** EDGE_POWER           # clustered at the edge
        t = 1.0 - depth / band                              # 1 on the edge, 0 at the inner boundary
        radius = R_MIN + (R_MAX - R_MIN) * t ** 0.7 * rng.uniform(0.75, 1.0)
        radius = min(R_MAX, max(R_MIN, radius + rng.uniform(-0.2, 0.2)))
        radius = round(radius * 4) / 4                      # quantised so sprites can be shared
        alpha = ALPHA_MAX * t ** ALPHA_FALLOFF * rng.uniform(0.7, 1.0)
        alpha = min(255, max(12, int(round(alpha / 8)) * 8))
        fixed = (depth, width - depth, depth, height - depth)[side]
        dots.append((side, along, fixed, rng.uniform(0.6, 1.2), radius, alpha))
    return dots


def pick_axis(vec, spec):
    """spec is x, y or z; a trailing '-' flips it (for example z-)."""
    return (-1 if spec.endswith("-") else 1) * vec["xyz".index(spec[0])]


def deadzone(v):
    return math.copysign(max(0.0, abs(v) - DEADZONE), v)


def clamp(v):
    return max(-MAX_OFFSET, min(MAX_OFFSET, v))


# ---- Win32 click-through ------------------------------------------------------------------------
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000


def make_click_through(hwnd):
    if sys.platform != "win32":
        return
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


# ---- Qt overlay ---------------------------------------------------------------------------------
def build_overlay_class():
    from PyQt5.QtCore import QPointF, QRect, Qt, QTimer
    from PyQt5.QtGui import QBrush, QColor, QPainter, QPixmap, QRadialGradient
    from PyQt5.QtWidgets import QWidget

    class Overlay(QWidget):
        def __init__(self, screen, args):
            super().__init__()
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                                | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.setGeometry(screen.geometry())  # screen-sized window, not exclusive fullscreen

            w, h = self.width(), self.height()
            band = args.band or max(60, int(FRIT_BAND_FRACTION * min(w, h)))
            self._sprites = {}
            self.dots = []
            for side, along, fixed, parallax, radius, alpha in generate_frit(w, h, args.dots, band):
                sprite, half = self._sprite(radius, alpha)
                self.dots.append((side, along, fixed, parallax, sprite, half))

            # Dots only ever move inside the band, so only the four edge strips need repainting.
            pad = int(R_MAX) + 3
            self.strips = [QRect(0, 0, band + pad, h), QRect(w - band - pad, 0, band + pad, h),
                           QRect(0, 0, w, band + pad), QRect(0, h - band - pad, w, band + pad)]

            self.args = args
            self.dots_visible = True
            self.ox = self.oy = 0.0  # smoothed drift: ox along the top/bottom edges, oy along the left/right edges
            self.base = None         # slow gravity estimate, used only when no rotation vector is available
            self.last = time.perf_counter()
            self.timer = QTimer(self)
            self.timer.setTimerType(Qt.PreciseTimer)
            self.timer.timeout.connect(self.step)
            self.timer.start(16)  # ~60 Hz

        def _sprite(self, radius, alpha):
            key = (radius, alpha)
            cached = self._sprites.get(key)
            if cached is None:
                size = int(math.ceil(radius * 2)) + 3
                c = size / 2
                pixmap = QPixmap(size, size)
                pixmap.fill(Qt.transparent)
                p = QPainter(pixmap)
                p.setRenderHint(QPainter.Antialiasing)
                r, g_, b = FRIT_RGB
                grad = QRadialGradient(c, c, radius + 0.5)  # soft edge: dots blend instead of reading as markers
                grad.setColorAt(0.0, QColor(r, g_, b, alpha))
                grad.setColorAt(0.6, QColor(r, g_, b, int(alpha * 0.75)))
                grad.setColorAt(1.0, QColor(r, g_, b, 0))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(grad))
                p.drawEllipse(QPointF(c, c), radius + 0.5, radius + 0.5)
                p.end()
                cached = self._sprites[key] = (pixmap, c)
            return cached

        def step(self):
            now = time.perf_counter()
            dt = min(now - self.last, 0.1)
            self.last = now

            lat = fwd = 0.0
            if time.time() - motion.stamp < STALE_S:
                v = list(motion.lin)
                if not motion.have_rot:  # raw accelerometer includes gravity: high-pass it
                    if self.base is None:
                        self.base = v[:]
                    k = 1 - math.exp(-dt / 1.0)
                    self.base = [b + (x - b) * k for b, x in zip(self.base, v)]
                    v = [x - b for x, b in zip(v, self.base)]
                lat = deadzone(pick_axis(v, self.args.lateral))
                fwd = deadzone(pick_axis(v, self.args.forward))

            target_x = clamp(SIGN_LATERAL * lat * ACCEL_GAIN)
            target_y = clamp(SIGN_FORWARD * fwd * ACCEL_GAIN)
            a = 1 - math.exp(-dt / SMOOTH_TAU)  # frame-rate independent lerp factor
            nx = self.ox + (target_x - self.ox) * a
            ny = self.oy + (target_y - self.oy) * a
            if abs(nx - self.ox) > 0.05 or abs(ny - self.oy) > 0.05:  # no repaint while nothing moves
                self.ox, self.oy = nx, ny
                for strip in self.strips:
                    self.update(strip)

        def paintEvent(self, _event):
            if not self.dots_visible:
                return
            w, h = self.width(), self.height()
            p = QPainter(self)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            for side, along, fixed, parallax, sprite, half in self.dots:
                if side < 2:   # left/right edges slide up and down with forward/back acceleration
                    x, y = fixed, (along + self.oy * parallax) % h
                else:          # top/bottom edges slide sideways with lateral acceleration
                    x, y = (along + self.ox * parallax) % w, fixed
                p.drawPixmap(QPointF(x - half, y - half), sprite)

    return Overlay


def make_tray(app, overlay):
    from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QAction, QMenu, QSystemTrayIcon

    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#4C8DFF"))
    painter.drawEllipse(6, 6, 20, 20)
    painter.end()

    tray = QSystemTrayIcon(QIcon(pixmap), app)
    tray.setToolTip("Motion overlay")
    menu = QMenu()
    toggle = QAction("Show pattern", menu, checkable=True, checked=True)
    toggle.toggled.connect(lambda on: (setattr(overlay, "dots_visible", on), overlay.update()))
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(toggle)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.show()
    return tray, menu, toggle, quit_action  # keep references alive


def local_ipv4s():
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    return sorted({i[4][0] for i in infos if not i[4][0].startswith("127.")})


def main():
    axes = ["x", "y", "z", "x-", "y-", "z-"]
    parser = argparse.ArgumentParser(description="Phone-driven ceramic-frit motion overlay")
    parser.add_argument("--port", type=int, default=8765, help="UDP port to listen on")
    parser.add_argument("--dots", type=int, default=FRIT_DOTS, help="number of frit dots")
    parser.add_argument("--band", type=int, default=0, help="band depth in pixels (default: automatic)")
    parser.add_argument("--lateral", choices=axes, default="x",
                        help="phone axis that points to the vehicle's right (default x)")
    parser.add_argument("--forward", choices=axes, default="y",
                        help="phone axis that points forward (default y; z- for an upright phone facing you)")
    parser.add_argument("--stats", action="store_true", help="print packet rate and loss every 5 s")
    args = parser.parse_args()

    threading.Thread(target=receiver, args=(args.port, args.stats), daemon=True).start()
    addresses = ", ".join(local_ipv4s()) or "unknown"
    print(f"Listening on UDP {args.port}. This PC's addresses: {addresses}")
    print("Point the phone at the address on the same network as the phone (hotspot or USB tether).")
    print("Quit from the tray icon or with Ctrl+C.")

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    overlay = build_overlay_class()(app.primaryScreen(), args)
    overlay.show()
    make_click_through(int(overlay.winId()))
    tray_refs = make_tray(app, overlay)  # noqa: F841

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
