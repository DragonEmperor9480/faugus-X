"""Faugus X: "Download missing artwork" in Settings.

Walks every game in the library and fills the gaps, never overwriting
anything that already exists (so banners/logos the user picked are safe):
- a banner, if the game has none (same SteamGridDB pick Faugus makes)
- the full-resolution HD copy of the banner, for the Showcase backdrop
- a logo, unless the user chose "Use game title instead"

Earlier "not found" results are forgotten first, so it also retries those.
The job runs in the background and keeps going if Settings is closed.

Installed from Showcase by wrapping Settings.__init__ (no official changes).
"""

import os
import tempfile
import threading

from gi.repository import GLib, Gtk

from faugus import hd_banners, logos
from faugus.path_manager import BANNERS_DIR


class _Job:
    def __init__(self):
        self.running = False
        self.cancel = threading.Event()
        self.listeners = []  # callables(state dict), called on the main loop
        self.state = {}

    def emit(self, **state):
        self.state = state

        def notify():
            for listener in list(self.listeners):
                try:
                    listener(dict(self.state))
                except Exception:
                    pass
            return False

        GLib.idle_add(notify)


JOB = _Job()


def start(games, on_finished=None):
    """Start downloading missing artwork for `games` (a list of Faugus Game objects)."""
    if JOB.running:
        return
    api_key = logos._api_key()
    if not api_key:
        JOB.emit(phase="error", message=_("Enable SteamGridDB and add your API key first."))
        return
    JOB.running = True
    JOB.cancel.clear()
    games = list(games)

    def work():
        counts = {"banners": 0, "hd": 0, "logos": 0}
        try:
            for index, game in enumerate(games):
                if JOB.cancel.is_set():
                    break
                JOB.emit(phase="running", done=index, total=len(games), title=game.title, counts=dict(counts))
                _fill_game(game, api_key, counts)
        finally:
            JOB.running = False
            JOB.emit(
                phase="cancelled" if JOB.cancel.is_set() else "finished",
                done=len(games), total=len(games), counts=dict(counts),
            )
            if on_finished is not None:
                GLib.idle_add(lambda: (on_finished(), False)[1])

    threading.Thread(target=work, name="faugus-x-artwork-refresh", daemon=True).start()


def _fill_game(game, api_key, counts):
    gameid = game.gameid
    banner = f"{BANNERS_DIR}/{gameid}.png"

    if not os.path.isfile(banner):
        if _download_banner(game, api_key, banner):
            counts["banners"] += 1

    if os.path.isfile(banner) and hd_banners.cached_hd(gameid) is None:
        _forget(hd_banners._miss_path(gameid))
        try:
            if hd_banners._fetch(gameid, game.title, getattr(game, "steamgriddb_id", ""),
                                 api_key, hd_banners._banner_signature(gameid)):
                counts["hd"] += 1
        except Exception as error:
            print(f"Faugus X: HD banner for {game.title}: {error}")

    if logos.cached_logo(gameid) is None and not logos.logo_disabled(gameid):
        _forget(logos._miss_path(gameid))
        try:
            if logos._download_logo(gameid, game.title, getattr(game, "steamgriddb_id", ""), api_key):
                counts["logos"] += 1
        except Exception as error:
            print(f"Faugus X: logo for {game.title}: {error}")


def _download_banner(game, api_key, dest):
    """The same pick Faugus makes when adding a game: SteamGridDB's first hero."""
    import requests
    from faugus.core import fetch_steamgriddb_candidates, get_steamgriddb_session
    from faugus.utils import resize_image_file, verified_content

    sgdb_id = str(getattr(game, "steamgriddb_id", "") or "").strip() or None
    steam_appid = game.path if getattr(game, "runner", "") == "Steam" else None
    try:
        candidates = fetch_steamgriddb_candidates(
            api_key, game.title, limit=1, game_id=sgdb_id, steam_appid=steam_appid
        )
        heroes = candidates.get("heroes") or []
        if not heroes:
            return False
        content = verified_content(get_steamgriddb_session().get(heroes[0]["url"], timeout=20))
    except requests.RequestException as error:
        print(f"Faugus X: banner for {game.title}: {error}")
        return False

    fd, tmp = tempfile.mkstemp(suffix=".img", prefix="faugus-x-banner-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.makedirs(BANNERS_DIR, exist_ok=True)
        resize_image_file(tmp, dest, 1920, 620)
        return os.path.isfile(dest)
    except Exception as error:
        print(f"Faugus X: saving banner for {game.title}: {error}")
        return False
    finally:
        _forget(tmp)


def _forget(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------- UI

def install(settings_cls, get_games, on_finished):
    """Wrap Settings.__init__ to add the "Download missing artwork" row."""
    if getattr(settings_cls, "_faugus_x_artwork", False):
        return
    original_init = settings_cls.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            _SettingsRow(self, get_games, on_finished)
        except Exception as error:  # never break Settings
            print(f"Faugus X: artwork refresh row unavailable: {error}")

    settings_cls.__init__ = __init__
    settings_cls._faugus_x_artwork = True


class _SettingsRow:
    def __init__(self, settings, get_games, on_finished):
        self.get_games = get_games
        self.on_finished = on_finished

        anchor = settings.entry_steamgriddb_key.get_parent()  # the SteamGridDB grid
        container = anchor.get_parent()
        if not isinstance(container, Gtk.Box):
            raise RuntimeError("unexpected SteamGridDB container")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        # Line up with the SteamGridDB key field above.
        box.set_margin_start(anchor.get_margin_start())
        box.set_margin_end(anchor.get_margin_end())

        self.button = Gtk.Button(label=_("Download missing artwork"))
        self.button.set_tooltip_text(_(
            "Fetches banners, HD banners and logos for every game that is missing them. "
            "Artwork you already have or picked yourself is never replaced."
        ))
        self.button.connect("clicked", self._on_clicked)

        self.progress = Gtk.ProgressBar()
        self.progress.set_visible(False)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.add_css_class("dim-label")
        self.status.set_visible(False)

        box.append(self.button)
        box.append(self.progress)
        box.append(self.status)
        container.insert_child_after(box, anchor)

        JOB.listeners.append(self._on_state)
        settings.connect("destroy", lambda *_a: self._detach())
        settings.connect("close-request", lambda *_a: (self._detach(), False)[1])
        if JOB.running:
            self._on_state(JOB.state)

    def _detach(self):
        if self._on_state in JOB.listeners:
            JOB.listeners.remove(self._on_state)

    def _on_clicked(self, _button):
        if JOB.running:
            JOB.cancel.set()
            self.button.set_sensitive(False)
            return
        start(self.get_games(), self.on_finished)

    def _on_state(self, state):
        phase = state.get("phase")
        if phase == "error":
            self.status.set_text(state.get("message", ""))
            self.status.set_visible(True)
            return
        counts = state.get("counts", {})
        summary = _("{banners} banners, {hd} HD banners, {logos} logos added").format(
            banners=counts.get("banners", 0), hd=counts.get("hd", 0), logos=counts.get("logos", 0)
        )
        total = max(1, state.get("total", 1))
        if phase == "running":
            self.button.set_label(_("Stop"))
            self.button.set_sensitive(True)
            self.progress.set_visible(True)
            self.progress.set_fraction(state.get("done", 0) / total)
            self.status.set_text(_("Checking {title}… ({n} of {total})").format(
                title=state.get("title", ""), n=state.get("done", 0) + 1, total=total))
            self.status.set_visible(True)
        else:
            self.button.set_label(_("Download missing artwork"))
            self.button.set_sensitive(True)
            self.progress.set_visible(False)
            if phase == "cancelled":
                text = _("Stopped: {}").format(summary)
            elif not any(counts.values()):
                text = _("All {} games are up to date. Nothing was missing.").format(state.get("total", 0))
            else:
                text = _("Done: {}").format(summary)
            self.status.set_text(text)
            self.status.set_visible(True)
