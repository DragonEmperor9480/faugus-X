"""Faugus X: controller tilt for Showcase's parallax effect.

Reads the motion sensors of controllers that have them (DualShock 4,
DualSense, Switch Pro and compatible) straight from their evdev "Motion
Sensors" device. Only the accelerometer is used: it tells which way gravity
points, i.e. how the controller is tilted, without the drift you get from
integrating the gyro.

"Neutral" is a slowly moving average of how the controller is held, so a
controller resting at any angle settles back to the centre; only tilting it
moves the picture. Values are smoothed on the main loop.

The sensors send ~1000 reports a second. A background thread reads them in
bulk and keeps only the latest one, and it only runs while Showcase asks
for it (Faugus X focused, option on), so it costs nothing during games.

Reading the sensors needs permission: see packaging/faugus-x/ for the udev
rule. Without it the effect simply stays still.
"""

import fcntl
import glob
import math
import os
import select
import struct
import threading
import time

EVENT = struct.Struct("llHHi")      # struct input_event on 64-bit Linux
EV_ABS = 3
ABS_Z = 2
INPUT_PROP_ACCELEROMETER = 1 << 6
ABSINFO = struct.Struct("6i")


def _eviocgabs(code):
    return (2 << 30) | (ABSINFO.size << 16) | (ord("E") << 8) | (0x40 + code)

FULL_TILT = math.radians(22)        # tilt that reaches the full effect
NEUTRAL_SECONDS = 4.0               # how fast "neutral" follows the way you hold it
RESCAN_SECONDS = 3.0
CONFIG_KEY = "showcase-tilt"


def is_enabled():
    from faugus.config_manager import ConfigManager
    return ConfigManager().config.get(CONFIG_KEY, "False") == "True"
# Linux gamepad motion axes: X to the right, Y out of the face buttons, Z
# towards the player. Lying flat, gravity reads as +Y.


def motion_devices():
    """(readable, unreadable) evdev paths of motion sensor devices."""
    readable, unreadable = [], []
    for sys_path in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(f"{sys_path}/device/properties") as f:
                props = int(f.read().split()[-1], 16)
        except (OSError, ValueError, IndexError):
            continue
        if not props & INPUT_PROP_ACCELEROMETER:
            continue
        dev = "/dev/input/" + os.path.basename(sys_path)
        (readable if os.access(dev, os.R_OK) else unreadable).append(dev)
    return readable, unreadable


class Tilt:
    def __init__(self):
        self._lock = threading.Lock()
        self._wanted = threading.Event()
        self._stop = False
        self._target = (0.0, 0.0)
        self._smooth = [0.0, 0.0]
        self._last_main = None
        self._thread = None

    # ---------------------------------------------------------------- main loop

    def set_active(self, active):
        if active:
            self._wanted.set()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="faugus-x-tilt", daemon=True)
                self._thread.start()
        else:
            self._wanted.clear()
            with self._lock:
                self._target = (0.0, 0.0)

    def active(self):
        return self._wanted.is_set()

    def stop(self):
        self._stop = True
        self._wanted.set()

    def offset(self):
        """Smoothed tilt in -1..1 (x: right side down, y: top away). Call once per frame."""
        now = time.monotonic()
        dt = 0.016 if self._last_main is None else min(0.1, now - self._last_main)
        self._last_main = now
        with self._lock:
            tx, ty = self._target
        k = 1.0 - math.exp(-dt * 7.0)
        self._smooth[0] += (tx - self._smooth[0]) * k
        self._smooth[1] += (ty - self._smooth[1]) * k
        return self._smooth[0], self._smooth[1]

    # ---------------------------------------------------------------- reader thread

    def _run(self):
        fds = {}            # fd -> [x, y, z]
        last_scan = 0.0
        neutral = None
        last_sample = None
        while not self._stop:
            if not self._wanted.is_set():
                for fd in fds:
                    os.close(fd)
                fds.clear()
                neutral = None
                self._wanted.wait()
                continue

            now = time.monotonic()
            if not fds and now - last_scan > RESCAN_SECONDS:
                last_scan = now
                for path in motion_devices()[0]:
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    except OSError:
                        continue
                    try:
                        # Events only report changes: start from the current values.
                        fds[fd] = [ABSINFO.unpack(fcntl.ioctl(fd, _eviocgabs(c), bytes(ABSINFO.size)))[0]
                                   for c in range(3)]
                    except OSError:
                        fds[fd] = [0, 0, 0]
            if not fds:
                time.sleep(0.5)
                continue

            try:
                ready, _, _ = select.select(list(fds), [], [], 0.5)
            except (OSError, ValueError):
                ready = []
            newest = None
            for fd in ready:
                try:
                    data = os.read(fd, EVENT.size * 512)
                except BlockingIOError:
                    continue
                except OSError:          # unplugged
                    os.close(fd)
                    fds.pop(fd, None)
                    continue
                axes = fds[fd]
                usable = len(data) - len(data) % EVENT.size
                for _s, _us, ev_type, code, value in EVENT.iter_unpack(data[:usable]):
                    if ev_type == EV_ABS and code <= ABS_Z:
                        axes[code] = value
                newest = axes
            if newest is None:
                continue

            ax, ay, az = newest
            norm = math.sqrt(ax * ax + ay * ay + az * az)
            if norm == 0:
                continue
            # Angles of the "up" vector the accelerometer reads. Tilting the
            # right side down moves it towards -X; the top away, towards +Z.
            roll = math.atan2(-ax, ay)
            pitch = math.atan2(az, ay)
            now = time.monotonic()
            if neutral is None:
                neutral = [roll, pitch]
            else:
                dt = min(0.5, now - last_sample)
                k = 1.0 - math.exp(-dt / NEUTRAL_SECONDS)
                neutral[0] += _wrap(roll - neutral[0]) * k
                neutral[1] += _wrap(pitch - neutral[1]) * k
            last_sample = now
            x = max(-1.0, min(1.0, _wrap(roll - neutral[0]) / FULL_TILT))
            y = max(-1.0, min(1.0, _wrap(pitch - neutral[1]) / FULL_TILT))
            with self._lock:
                self._target = (x, y)

        for fd in fds:
            os.close(fd)


def _wrap(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi
