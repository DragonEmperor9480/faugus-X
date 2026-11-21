"""Faugus X: game logos from SteamGridDB.

Faugus downloads covers, banners and icons; Showcase also shows each game's
transparent logo in place of its text title. Logos are cached next to the
other artwork (~/.local/share/faugus-launcher/logos/<gameid>.png). A failed
lookup leaves a small "<gameid>.miss" marker so we don't ask again for a week.

Uses the user's own SteamGridDB API key and only runs when SteamGridDB is
enabled in Faugus settings.
"""

import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from gi.repository import Gdk, GdkPixbuf, GLib

from faugus.config_manager import ConfigManager
from faugus.path_manager import PathManager

LOGOS_DIR = PathManager.user_data("faugus-launcher/logos")
MISS_TTL_SECONDS = 7 * 24 * 3600
STYLE_PREFERENCE = ("official", "white", "custom", "black")
API = "https://www.steamgriddb.com/api/v2"

# SteamGridDB logos can be enormous (one was 9650x4578: ~250 ms to decode and
# ~170 MB of texture memory). Store them at most this big — plenty for a hero
# logo at 2x scale — and never decode them on the UI thread.
MAX_LOGO_W, MAX_LOGO_H = 1200, 400
TEXTURE_CACHE_SIZE = 48

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="faugus-x-logos")
_texture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="faugus-x-logo-tex")
_inflight = set()
_textures = OrderedDict()   # gameid -> (mtime, Gdk.Texture)
_texture_waiters = {}       # gameid -> [callbacks]


def logo_path(gameid):
    return os.path.join(LOGOS_DIR, f"{gameid}.png")


def cached_logo(gameid):
    if logo_disabled(gameid):
        return None
    path = logo_path(gameid)
    return path if os.path.isfile(path) else None


def _none_path(gameid):
    return logo_path(gameid) + ".none"


def logo_disabled(gameid):
    """True when the user chose to show the text title instead of a logo."""
    return os.path.isfile(_none_path(gameid))


def set_logo_disabled(gameid):
    forget_logo(gameid)
    try:
        os.makedirs(LOGOS_DIR, exist_ok=True)
        with open(_none_path(gameid), "w"):
            pass
    except OSError:
        pass


def install_logo(gameid, source_path):
    """Use `source_path` (a PNG/WebP/… image) as the game's logo."""
    from PIL import Image
    forget_logo(gameid)
    os.makedirs(LOGOS_DIR, exist_ok=True)
    tmp = logo_path(gameid) + ".tmp.png"
    with Image.open(source_path) as image:
        image.convert("RGBA").save(tmp, "PNG")
    os.replace(tmp, logo_path(gameid))
    _shrink_file(logo_path(gameid))


def _miss_path(gameid):
    return logo_path(gameid) + ".miss"


def _recent_miss(gameid):
    try:
        return time.time() - os.path.getmtime(_miss_path(gameid)) < MISS_TTL_SECONDS
    except OSError:
        return False


def _api_key():
    cfg = ConfigManager()
    if cfg.config.get("steamgriddb-enabled", "False") != "True":
        return None
    key = cfg.config.get("steamgriddb-api-key", "").strip('"').strip()
    return key or None


def request_logo(game, callback=None):
    """Call `callback(path)` on the main loop once a logo is available.

    Returns the path immediately if it is already cached.
    """
    path = cached_logo(game.gameid)
    if path:
        return path
    if logo_disabled(game.gameid):
        return None
    if game.gameid in _inflight or _recent_miss(game.gameid):
        return None
    api_key = _api_key()
    if not api_key:
        return None

    _inflight.add(game.gameid)
    _executor.submit(_fetch, game.gameid, game.title, getattr(game, "steamgriddb_id", ""), api_key, callback)
    return None


def _fetch(gameid, title, sgdb_id, api_key, callback):
    path = None
    try:
        path = _download_logo(gameid, title, sgdb_id, api_key)
    except Exception:
        path = None
    finally:
        def finish():
            _inflight.discard(gameid)
            if path and callback is not None:
                callback(path)
            return False

        GLib.idle_add(finish)


def resolve_sgdb_game_id(session, headers, title, sgdb_id):
    """The game's SteamGridDB id: Faugus' stored one, else the first name match.

    Raises requests.RequestException on network errors.
    """
    try:
        if str(sgdb_id).strip():
            return int(sgdb_id)
    except ValueError:
        pass
    search = session.get(f"{API}/search/autocomplete/{quote(title)}", headers=headers, timeout=10)
    search.raise_for_status()
    results = search.json().get("data") or []
    return results[0]["id"] if results else None


def search_logos(session, headers, game_id):
    """All suitable SteamGridDB logos for a SteamGridDB game id, best style first."""
    response = session.get(
        f"{API}/logos/game/{game_id}",
        headers=headers,
        params={"types": "static", "mimes": "image/png", "nsfw": "false", "humor": "false"},
        timeout=10,
    )
    response.raise_for_status()
    logos = [item for item in (response.json().get("data") or []) if item.get("url")]

    def rank(item):
        style = item.get("style", "")
        return STYLE_PREFERENCE.index(style) if style in STYLE_PREFERENCE else len(STYLE_PREFERENCE)

    return sorted(logos, key=rank)


def _download_logo(gameid, title, sgdb_id, api_key):
    import requests
    from faugus.core import get_steamgriddb_session

    session = get_steamgriddb_session()
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        game_id = resolve_sgdb_game_id(session, headers, title, sgdb_id)
        if game_id is None:
            _mark_miss(gameid)
            return None

        logos = search_logos(session, headers, game_id)
        if not logos:
            _mark_miss(gameid)
            return None

        best = logos[0]
        image = session.get(best["url"], timeout=20)
        image.raise_for_status()
        if not image.content.startswith(b"\x89PNG"):
            _mark_miss(gameid)
            return None
    except requests.RequestException:
        return None  # network trouble: try again next time, no miss marker

    os.makedirs(LOGOS_DIR, exist_ok=True)
    path = logo_path(gameid)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(image.content)
    os.replace(tmp, path)
    _shrink_file(path)
    try:
        os.remove(_miss_path(gameid))
    except OSError:
        pass
    return path


def _mark_miss(gameid):
    try:
        os.makedirs(LOGOS_DIR, exist_ok=True)
        with open(_miss_path(gameid), "w"):
            pass
    except OSError:
        pass


def forget_logo(gameid):
    """Delete a cached logo (and miss marker) so it is fetched again."""
    _textures.pop(gameid, None)
    for path in (logo_path(gameid), _miss_path(gameid), _none_path(gameid)):
        try:
            os.remove(path)
        except OSError:
            pass


def _shrink_file(path):
    """Downscale an oversized logo file in place (worker thread only)."""
    try:
        from PIL import Image
        with Image.open(path) as image:
            width, height = image.size
            if width <= MAX_LOGO_W and height <= MAX_LOGO_H:
                return
            ratio = min(MAX_LOGO_W / width, MAX_LOGO_H / height)
            size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
            small = image.convert("RGBA").resize(size, Image.LANCZOS)
        tmp = path + ".tmp.png"
        small.save(tmp, "PNG", optimize=True)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------- textures

def logo_texture(gameid):
    """Return the logo texture if it is already decoded in memory, else None."""
    entry = _textures.get(gameid)
    if entry is None:
        return None
    path = logo_path(gameid)
    try:
        if os.path.getmtime(path) != entry[0]:
            _textures.pop(gameid, None)
            return None
    except OSError:
        _textures.pop(gameid, None)
        return None
    _textures.move_to_end(gameid)
    return entry[1]


def load_logo_texture(gameid, callback=None):
    """Decode a cached logo off the UI thread; `callback(texture)` on the main loop.

    Returns the texture immediately when it is already in memory.
    """
    texture = logo_texture(gameid)
    if texture is not None:
        return texture
    if cached_logo(gameid) is None:
        return None

    waiters = _texture_waiters.get(gameid)
    if waiters is not None:
        if callback is not None:
            waiters.append(callback)
        return None
    _texture_waiters[gameid] = [callback] if callback is not None else []

    def work():
        path = logo_path(gameid)
        texture, mtime = None, 0
        try:
            _shrink_file(path)  # migrates logos cached before shrinking existed
            mtime = os.path.getmtime(path)
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, MAX_LOGO_W, MAX_LOGO_H, True)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        except (GLib.Error, OSError):
            texture = None

        def finish():
            callbacks = _texture_waiters.pop(gameid, [])
            if texture is not None:
                _textures[gameid] = (mtime, texture)
                _textures.move_to_end(gameid)
                while len(_textures) > TEXTURE_CACHE_SIZE:
                    _textures.popitem(last=False)
                for cb in callbacks:
                    cb(texture)
            return False

        GLib.idle_add(finish)

    _texture_executor.submit(work)
    return None
