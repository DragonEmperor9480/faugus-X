"""Faugus X: first-run defaults and the SteamGridDB requirement.

- Showcase is the default interface. Users coming from Faugus are switched
  to Showcase once, on Faugus X's first start; they can change it later.
- Faugus X needs a SteamGridDB API key (banners, HD backdrops, logos).
  Without one, a window that can't be skipped asks for it: paste a key
  (checked online) or quit. It is checked again after Settings closes.

Called from two small hooks in FaugusApp.do_activate (launcher.py).
"""

from gi.repository import GLib, Gtk

from faugus.config_manager import ConfigManager

FIRST_RUN_KEY = "faugus-x-initialized"
DEFAULT_INTERFACE = "Showcase"
KEY_URL = "https://www.steamgriddb.com/profile/preferences/api"
API = "https://www.steamgriddb.com/api/v2"

CSS = """
.faugus-x-gate-title { font-size: 1.5em; font-weight: 800; }
.faugus-x-gate-error { color: #ff7b72; }
"""


# ---------------------------------------------------------------- defaults

def prepare_first_run():
    """Make Showcase the default interface (once). Run before the window is built."""
    cfg = ConfigManager()
    if cfg.config.get(FIRST_RUN_KEY) == "True":
        return
    cfg.config["interface-mode"] = DEFAULT_INTERFACE
    cfg.config[FIRST_RUN_KEY] = "True"
    cfg.save_config()


# ---------------------------------------------------------------- SteamGridDB gate

def has_steamgriddb():
    cfg = ConfigManager().config
    key = cfg.get("steamgriddb-api-key", "").strip('"').strip()
    return cfg.get("steamgriddb-enabled", "False") == "True" and bool(key)


_gate = None


def require_steamgriddb(app, win):
    """Show the SteamGridDB window if Faugus X can't work without it."""
    _watch_settings(app, win)
    if has_steamgriddb() or _gate is not None or win is None:
        return
    _Gate(app, win)


def _watch_settings(app, win):
    """Re-check after Settings closes, so the key can't be removed there."""
    if win is None:
        return
    settings_cls = type(win).on_button_edit_clicked.__globals__.get("Settings")
    if settings_cls is None or getattr(settings_cls, "_faugus_x_gate", False):
        return
    original_init = settings_cls.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        def recheck(*_a):
            GLib.timeout_add(400, lambda: (require_steamgriddb(app, app.get_active_window() or win), False)[1])

        self.connect("destroy", recheck)
        self.connect("close-request", lambda *_a: (recheck(), False)[1])
        self.connect("response", recheck)

    settings_cls.__init__ = __init__
    settings_cls._faugus_x_gate = True


def _check_key(key):
    """Returns (ok, message). Network trouble counts as ok so offline users aren't locked out."""
    import requests
    from faugus.core import get_steamgriddb_session
    try:
        response = get_steamgriddb_session().get(
            f"{API}/search/autocomplete/portal",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
    except requests.RequestException:
        return True, "offline"
    if response.status_code in (401, 403):
        return False, _("SteamGridDB rejected this key. Check that you copied all of it.")
    if response.ok:
        return True, ""
    return True, "offline"


class _Gate:
    def __init__(self, app, win):
        global _gate
        _gate = self
        from faugus.utils import add_css_once, apply_titlebar_preference
        add_css_once("faugus_x_gate", CSS, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

        self.app = app
        self.win = win
        self.done = False

        dialog = Gtk.Window(title=_("SteamGridDB required"), transient_for=win, modal=True)
        apply_titlebar_preference(dialog)
        dialog.set_resizable(False)
        dialog.set_deletable(True)
        dialog.connect("close-request", self._on_close)
        self.dialog = dialog

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for margin in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{margin}")(24)
        box.set_size_request(460, -1)

        title = Gtk.Label(label=_("Faugus X needs SteamGridDB"), xalign=0)
        title.add_css_class("faugus-x-gate-title")

        body = Gtk.Label(xalign=0, wrap=True, max_width_chars=52)
        body.set_text(_(
            "Faugus X uses SteamGridDB for game banners, sharp full-screen backgrounds and logos. "
            "Add your free SteamGridDB API key to continue."
        ))

        steps = Gtk.Label(xalign=0, wrap=True, max_width_chars=52)
        steps.add_css_class("dim-label")
        steps.set_text(_(
            "1. Sign in at steamgriddb.com (a Steam login works)\n"
            "2. Open Preferences → API and generate a key\n"
            "3. Paste it below"
        ))

        link = Gtk.LinkButton(uri=KEY_URL, label=_("Get a free API key"))
        link.set_halign(Gtk.Align.START)

        self.entry = Gtk.Entry(placeholder_text=_("API key"))
        self.entry.set_hexpand(True)
        self.entry.connect("activate", lambda *_a: self._on_continue())
        self.entry.connect("changed", lambda *_a: self.error.set_visible(False))

        self.error = Gtk.Label(xalign=0, wrap=True, max_width_chars=52)
        self.error.add_css_class("faugus-x-gate-error")
        self.error.set_visible(False)

        self.spinner = Gtk.Spinner()

        quit_button = Gtk.Button(label=_("Quit"))
        quit_button.set_hexpand(True)
        quit_button.connect("clicked", lambda *_a: self._quit())
        self.continue_button = Gtk.Button(label=_("Continue"))
        self.continue_button.set_hexpand(True)
        self.continue_button.add_css_class("suggested-action")
        self.continue_button.connect("clicked", lambda *_a: self._on_continue())

        buttons = Gtk.Box(spacing=10, homogeneous=True)
        buttons.append(quit_button)
        buttons.append(self.continue_button)

        entry_row = Gtk.Box(spacing=8)
        entry_row.append(self.entry)
        entry_row.append(self.spinner)

        for widget in (title, body, steps, link, entry_row, self.error, buttons):
            box.append(widget)
        dialog.set_child(box)

        win.set_sensitive(False)
        dialog.present()
        self.entry.grab_focus()

    def _on_close(self, *_args):
        if not self.done:
            self._quit()
        return False

    def _quit(self):
        self.done = True
        self._finish()
        self.app.quit()

    def _on_continue(self):
        key = self.entry.get_text().strip().strip('"')
        if not key:
            self._show_error(_("Paste your SteamGridDB API key first."))
            return
        self.continue_button.set_sensitive(False)
        self.entry.set_sensitive(False)
        self.spinner.start()

        def work():
            ok, message = _check_key(key)
            GLib.idle_add(self._checked, key, ok, message)

        from faugus.utils import run_in_background
        run_in_background(work)

    def _checked(self, key, ok, message):
        self.spinner.stop()
        self.continue_button.set_sensitive(True)
        self.entry.set_sensitive(True)
        if not ok:
            self._show_error(message)
            return False
        if message == "offline":
            print("Faugus X: couldn't reach SteamGridDB to verify the key; saved it anyway.")
        cfg = ConfigManager()
        cfg.config["steamgriddb-api-key"] = key
        cfg.config["steamgriddb-enabled"] = "True"
        cfg.save_config()
        self.done = True
        self._finish()
        showcase = getattr(self.win, "showcase", None)
        if showcase is not None:
            showcase.refresh_library_art()
        return False

    def _show_error(self, text):
        self.error.set_text(text)
        self.error.set_visible(True)

    def _finish(self):
        global _gate
        _gate = None
        try:
            self.win.set_sensitive(True)
        except Exception:
            pass
        self.dialog.destroy()
