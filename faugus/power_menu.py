"""Faugus X: power menu for Showcase (sleep, restart, shut down, log out, exit).

A console needs a way to switch off without a mouse, so Showcase's top bar
gets a power button whose menu works with a controller like the game menu.

Restart, shut down and log out ask for a second press ("Press again to …")
so a stray button can't switch the PC off. They go through the desktop's
session manager when there is one (KDE Plasma, GNOME), so apps close
normally, and fall back to systemd-logind otherwise (e.g. gamescope).
"""

from gi.repository import Gio, GLib, Gtk

from faugus.utils import play_ui_sound

CONFIRM_SECONDS = 4


def N_(text):
    """Mark for translation; translated when shown."""
    return text


LOGIND = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
KDE = ("org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown")
GNOME = ("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager")

# action -> list of (bus, (name, path, interface), method, args), tried in order
ROUTES = {
    "suspend": [(Gio.BusType.SYSTEM, LOGIND, "Suspend", GLib.Variant("(b)", (True,)))],
    "reboot": [
        (Gio.BusType.SESSION, KDE, "logoutAndReboot", None),
        (Gio.BusType.SESSION, GNOME, "Reboot", None),
        (Gio.BusType.SYSTEM, LOGIND, "Reboot", GLib.Variant("(b)", (True,))),
    ],
    "poweroff": [
        (Gio.BusType.SESSION, KDE, "logoutAndShutdown", None),
        (Gio.BusType.SESSION, GNOME, "Shutdown", None),
        (Gio.BusType.SYSTEM, LOGIND, "PowerOff", GLib.Variant("(b)", (True,))),
    ],
    "logout": [
        (Gio.BusType.SESSION, KDE, "logout", None),
        (Gio.BusType.SESSION, GNOME, "Logout", GLib.Variant("(u)", (1,))),
    ],
}

# logind "Can…" check per action; actions it says no to are hidden.
CAN = {"suspend": "CanSuspend", "reboot": "CanReboot", "poweroff": "CanPowerOff"}


def _has_name(bus_type, name):
    try:
        bus = Gio.bus_get_sync(bus_type, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", (name,)), GLib.VariantType.new("(b)"),
                              Gio.DBusCallFlags.NONE, 1000, None)
        return reply.unpack()[0]
    except GLib.Error:
        return False


def available(action):
    if action in CAN:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            reply = bus.call_sync(*LOGIND, CAN[action], None, GLib.VariantType.new("(s)"),
                                  Gio.DBusCallFlags.NONE, 1000, None)
            if reply.unpack()[0] not in ("yes", "challenge"):
                return False
        except GLib.Error:
            return False
    if action == "logout":
        return any(_has_name(bus, target[0]) for bus, target, _m, _a in ROUTES["logout"])
    return True


def run(action):
    """Ask the session manager (or logind) to do it. Returns True if a route accepted it."""
    for bus_type, (name, path, iface), method, args in ROUTES[action]:
        if bus_type == Gio.BusType.SESSION and not _has_name(bus_type, name):
            continue
        try:
            bus = Gio.bus_get_sync(bus_type, None)
            bus.call_sync(name, path, iface, method, args, None,
                          Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 10000, None)
            return True
        except GLib.Error as error:
            print(f"Faugus X: {action} via {name} failed: {error.message}", flush=True)
    return False


class PowerMenu:
    """Popover attached to Showcase's power button."""

    ITEMS = [
        # action, icon, label, confirm label
        ("suspend", "weather-clear-night-symbolic", N_("Sleep"), None),
        ("reboot", "system-reboot-symbolic", N_("Restart"), N_("Press again to restart")),
        ("poweroff", "system-shutdown-symbolic", N_("Shut down"), N_("Press again to shut down")),
        ("logout", "system-log-out-symbolic", N_("Log out"), N_("Press again to log out")),
        ("exit", "application-exit-symbolic", N_("Exit Faugus X"), None),
    ]

    def __init__(self, win, button):
        self.win = win
        self._armed = None      # (action, row label) waiting for the second press
        self._disarm_id = None
        self.popover = Gtk.Popover()
        self.popover.set_parent(button)
        self.popover.set_has_arrow(True)
        self.popover.set_position(Gtk.PositionType.BOTTOM)
        self.popover.connect("closed", lambda *_: self._disarm())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        self.rows = {}
        for action, icon, label, confirm in self.ITEMS:
            if action != "exit" and not available(action):
                continue
            row = Gtk.Button()
            row.add_css_class("flat")
            content = Gtk.Box(spacing=12)
            content.set_margin_start(6)
            content.set_margin_end(12)
            content.append(Gtk.Image.new_from_icon_name(icon))
            text = Gtk.Label(label=_(label), xalign=0)
            content.append(text)
            row.set_child(content)
            row.connect("clicked", self._on_item, action, text, _(label), _(confirm) if confirm else None)
            box.append(row)
            self.rows[action] = row
        self.popover.set_child(box)

    def popup(self):
        self._disarm()
        self.popover.popup()
        first = next(iter(self.rows.values()), None)
        if first is not None:
            GLib.idle_add(lambda: (first.grab_focus(), False)[1])

    def _disarm(self):
        if self._disarm_id is not None:
            GLib.source_remove(self._disarm_id)
            self._disarm_id = None
        if self._armed is not None:
            _action, text, label = self._armed
            text.set_label(label)
            text.remove_css_class("error")
            self._armed = None

    def _on_item(self, _button, action, text, label, confirm):
        if confirm and (self._armed is None or self._armed[0] != action):
            self._disarm()
            text.set_label(confirm)
            text.add_css_class("error")
            self._armed = (action, text, label)
            self._disarm_id = GLib.timeout_add_seconds(CONFIRM_SECONDS, self._on_timeout)
            play_ui_sound("warning")
            return
        self._disarm()
        self.popover.popdown()
        play_ui_sound("confirm")
        if action == "exit":
            app = self.win.get_application()
            if app is not None:
                app.activate_action("quit", None)
            return
        run(action)

    def _on_timeout(self):
        self._disarm_id = None
        self._disarm()
        return False

