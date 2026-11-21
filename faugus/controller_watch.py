"""Faugus X: open Faugus X when a controller connects.

A small background process (no window, no tray icon) that opens Faugus X,
or brings it to the front if it is already open, when:
- a controller is plugged in or connects over Bluetooth, or
- the PS / Home / Guide button is pressed on a controller that was off or
  idle for a minute. Controllers with a USB wireless dongle never "connect"
  (the dongle stays plugged in), but pressing PS is how they're turned on.

It does nothing while a game started from Faugus is running, so a
controller that wakes up or reconnects mid-game never pulls the launcher
over the game. Steam's virtual controller is ignored for the same reason.

Turned on with Settings → Miscellaneous → "Open when a controller
connects". When on, it starts with Faugus X and at login (its own
autostart entry), and quits by itself when the option is turned off.

    python3 -m faugus.controller_watch
"""

import os
import subprocess
import sys
import time

import gi
gi.require_version("Manette", "0.2")
from gi.repository import Gio, GLib, Manette

from faugus.core import load_json_file
from faugus.path_manager import APP_ID, CONFIG_FILE_DIR, LAUNCHER_MODULE_ARGS, LAUNCHER_PATH, PathManager, RUNNING_GAMES, subprocess_env

CONFIG_KEY = "controller-open"
BUS_NAME = f"{APP_ID}.ControllerWatch"
AUTOSTART_FILE = PathManager.user_home(".config/autostart/faugus-x-controller.desktop")

STARTUP_GRACE = 5      # seconds: controllers already on at start don't count
COOLDOWN = 15          # seconds between two openings (flaky reconnects)
IDLE = 60              # seconds without input before the Home button counts as "turned on"
BTN_MODE = 316         # PS / Home / Guide (Linux input code)
STEAM_VIRTUAL = {(0x28de, 0x11ff)}  # Steam Input's virtual gamepad


# ---------------------------------------------------------------- setting

def is_enabled():
    return load_json_file(CONFIG_FILE_DIR, default={}).get(CONFIG_KEY, "False") == "True"


def _launch_command():
    if LAUNCHER_PATH and not LAUNCHER_MODULE_ARGS:
        return [LAUNCHER_PATH]
    return [sys.executable, "-m", "faugus.tray_only"]


def apply_setting(enabled):
    """Write or remove the login entry and start the watcher now if needed."""
    if enabled:
        os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
        if LAUNCHER_PATH and not LAUNCHER_MODULE_ARGS:
            exec_cmd = f"{LAUNCHER_PATH} --controller-watch"
        else:
            exec_cmd = f"{sys.executable} -m faugus.controller_watch"
        with open(AUTOSTART_FILE, "w") as f:
            f.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Faugus X controller watch\n"
                "Comment=Opens Faugus X when a controller connects\n"
                f"Exec={exec_cmd}\n"
                f"Icon={APP_ID}\n"
                "NoDisplay=true\n"
                "X-KDE-autostart-phase=2\n"
            )
        start()
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)
    # When turned off, a running watcher notices on the next connection and quits.


def start():
    """Start the watcher in the background unless the option is off (it exits if one already runs)."""
    if not is_enabled():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "faugus.controller_watch"],
            env=subprocess_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        pass


# ---------------------------------------------------------------- watcher

def _ids(device):
    """(vendor, product) from the SDL GUID, or None."""
    guid = device.get_guid() or ""
    if len(guid) < 20:
        return None
    try:
        return (int(guid[10:12] + guid[8:10], 16), int(guid[18:20] + guid[16:18], 16))
    except ValueError:
        return None


def _game_running():
    for value in load_json_file(RUNNING_GAMES, default={}).values():
        pid = next(iter(value.values()), None) if isinstance(value, dict) else value
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, TypeError, ValueError):
            continue
    return False


def main():
    if not is_enabled():
        return
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    reply = connection.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
        GLib.Variant("(su)", (BUS_NAME, 0x4)), GLib.VariantType.new("(u)"),
        Gio.DBusCallFlags.NONE, -1, None,
    )
    if reply.unpack()[0] != 1:  # another watcher already runs
        return

    loop = GLib.MainLoop()
    started = time.monotonic()
    last_open = [0.0]
    last_input = {}

    def try_open(device):
        now = time.monotonic()
        if now - last_open[0] < COOLDOWN:
            return
        if not is_enabled():
            loop.quit()
            return
        if _ids(device) in STEAM_VIRTUAL or _game_running():
            return
        last_open[0] = now
        subprocess.Popen(
            _launch_command(), env=subprocess_env(),
            stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        )

    def on_event(device, event):
        now = time.monotonic()
        idle = now - last_input.get(device, 0.0)
        last_input[device] = now
        if event.get_event_type() != Manette.EventType.EVENT_BUTTON_PRESS or idle < IDLE:
            return
        ok, button = event.get_button()
        if (ok and button == BTN_MODE) or event.get_hardware_code() == BTN_MODE:
            try_open(device)

    def watch(device):
        device.connect("event", on_event)

    def on_connected(_monitor, device):
        watch(device)
        last_input[device] = time.monotonic()
        if time.monotonic() - started >= STARTUP_GRACE:
            try_open(device)

    monitor = Manette.Monitor.new()
    monitor.connect("device-connected", on_connected)
    devices = monitor.iterate()
    while True:
        ok, device = devices.next()
        if not ok:
            break
        watch(device)
    loop.run()


if __name__ == "__main__":
    main()
