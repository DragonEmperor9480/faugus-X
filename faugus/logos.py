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
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from gi.repository import GLib

from faugus.config_manager import ConfigManager
from faugus.path_manager import PathManager

LOGOS_DIR = PathManager.user_data("faugus-launcher/logos")
MISS_TTL_SECONDS = 7 * 24 * 3600
STYLE_PREFERENCE = ("official", "white", "custom", "black")
API = "https://www.steamgriddb.com/api/v2"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="faugus-x-logos")
_inflight = set()


def logo_path(gameid):
    return os.path.join(LOGOS_DIR, f"{gameid}.png")


def cached_logo(gameid):
    path = logo_path(gameid)
    return path if os.path.isfile(path) else None


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


def _download_logo(gameid, title, sgdb_id, api_key):
    import requests
    from faugus.core import get_steamgriddb_session

    session = get_steamgriddb_session()
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        game_id = int(sgdb_id) if str(sgdb_id).strip() else None
    except ValueError:
        game_id = None

    try:
        if game_id is None:
            search = session.get(f"{API}/search/autocomplete/{quote(title)}", headers=headers, timeout=10)
            search.raise_for_status()
            results = search.json().get("data") or []
            if not results:
                _mark_miss(gameid)
                return None
            game_id = results[0]["id"]

        response = session.get(
            f"{API}/logos/game/{game_id}",
            headers=headers,
            params={"types": "static", "mimes": "image/png", "nsfw": "false", "humor": "false"},
            timeout=10,
        )
        response.raise_for_status()
        logos = [item for item in (response.json().get("data") or []) if item.get("url")]
        if not logos:
            _mark_miss(gameid)
            return None

        def rank(item):
            style = item.get("style", "")
            return STYLE_PREFERENCE.index(style) if style in STYLE_PREFERENCE else len(STYLE_PREFERENCE)

        best = sorted(logos, key=rank)[0]
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
    for path in (logo_path(gameid), _miss_path(gameid)):
        try:
            os.remove(path)
        except OSError:
            pass
