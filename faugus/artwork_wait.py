"""Faugus X: wait for artwork downloads before adding or saving a game.

Faugus starts downloading the cover, banner and icon from SteamGridDB as
soon as a title is entered, and on OK copies whatever has finished. Press
OK while they are still downloading and the game is saved without them:
a blank card in the carousel and library.

With this, OK waits for downloads still in progress: the button turns into
a spinner with "Getting artwork…", and the game is added as soon as they
finish (at most MAX_WAIT_SECONDS, so a slow network never blocks it).
Cancel still works while waiting. Nothing changes when no download is
running, so artwork that wasn't found or that you removed stays that way.

Logos and HD banners are fetched by Showcase right after the game appears.
"""

from gi.repository import GLib, GObject, Gtk

MAX_WAIT_SECONDS = 20
POLL_MS = 150
SPINNERS = ("spinner_cover1", "spinner_cover2", "spinner_banner1", "spinner_icon")


def _downloading(dialog):
    return any(getattr(dialog, name, None) is not None and getattr(dialog, name).get_spinning()
               for name in SPINNERS)


def install(add_game_cls):
    if add_game_cls is None or getattr(add_game_cls, "_faugus_x_artwork_wait", False):
        return
    original_init = add_game_cls.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # Connected during construction, so it runs before Faugus' own response handler.
        self.connect("response", _on_response)

    add_game_cls.__init__ = __init__
    add_game_cls._faugus_x_artwork_wait = True


def _on_response(dialog, response_id):
    state = getattr(dialog, "_faugus_x_wait", None)
    if response_id != Gtk.ResponseType.OK:
        if state is not None:
            state["cancelled"] = True
        return
    if state is not None and state.get("done"):
        return                      # our own re-sent OK: let Faugus save now
    if state is not None:
        GObject.signal_stop_emission_by_name(dialog, "response")
        return                      # already waiting; ignore repeated OK presses
    if not _downloading(dialog):
        return

    GObject.signal_stop_emission_by_name(dialog, "response")
    state = {"cancelled": False, "done": False, "waited": 0}
    dialog._faugus_x_wait = state
    _show_waiting(dialog, True)

    def poll():
        if state["cancelled"]:
            return False
        state["waited"] += POLL_MS
        if _downloading(dialog) and state["waited"] < MAX_WAIT_SECONDS * 1000:
            return True
        # Downloads land on disk through an idle callback: give them one more turn.
        GLib.idle_add(finish)
        return False

    def finish():
        if state["cancelled"]:
            return False
        _show_waiting(dialog, False)
        state["done"] = True
        dialog.response(Gtk.ResponseType.OK)
        if getattr(dialog, "_faugus_x_wait", None) is state and dialog.get_visible():
            # Faugus kept the dialog open (e.g. a field needs fixing): allow another try.
            dialog._faugus_x_wait = None
        return False

    GLib.timeout_add(POLL_MS, poll)


def _show_waiting(dialog, waiting):
    button = getattr(dialog, "button_ok", None)
    if button is None:
        return
    if waiting:
        dialog._faugus_x_ok_child = button.get_child()
        if dialog._faugus_x_ok_child is not None:
            button.set_child(None)
        box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.start()
        box.append(spinner)
        box.append(Gtk.Label(label=_("Getting artwork…")))
        button.set_child(box)
        button.set_sensitive(False)
    else:
        button.set_child(getattr(dialog, "_faugus_x_ok_child", None) or Gtk.Label(label=_("Ok")))
        button.set_sensitive(True)
