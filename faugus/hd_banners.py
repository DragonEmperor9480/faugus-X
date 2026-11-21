"""Faugus X: full-resolution banners for the full-screen backdrop.

Faugus stores banners at 1920x620. Filling a 16:9 screen with that means
cropping it and stretching it ~2x, which looks soft. SteamGridDB usually has
the same banner at 3840x1240, which only needs to be scaled *down*.

To respect the banner the user picked in Faugus, we only use a SteamGridDB
banner that visually matches the local one (compared on small thumbnails).
If nothing matches, the normal banner keeps being used.

HD copies live in ~/.local/share/faugus-launcher/banners-hd/<gameid>.jpg with
a small "<gameid>.src" sidecar recording which local banner they belong to,
so picking a new banner in Faugus makes Faugus X look again.
"""

import io
import os
import time
from concurrent.futures import ThreadPoolExecutor

from gi.repository import GLib

from faugus.logos import _api_key, resolve_sgdb_game_id
from faugus.path_manager import BANNERS_DIR, PathManager

HD_DIR = PathManager.user_data("faugus-launcher/banners-hd")
MIN_HD_WIDTH = 2560
MAX_HD_W, MAX_HD_H = 3840, 1240
MATCH_THRESHOLD = 5.0       # mean abs difference (0-255) of 96x31 grey thumbnails;
                            # the same banner scores <1, different ones >10
MAX_CANDIDATES = 12
MISS_TTL_SECONDS = 7 * 24 * 3600
API = "https://www.steamgriddb.com/api/v2"

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="faugus-x-hd-banners")
_inflight = set()


def banner_path(gameid):
    return f"{BANNERS_DIR}/{gameid}.png"


def hd_path(gameid):
    return os.path.join(HD_DIR, f"{gameid}.jpg")


def _src_path(gameid):
    return os.path.join(HD_DIR, f"{gameid}.src")


def _miss_path(gameid):
    return os.path.join(HD_DIR, f"{gameid}.miss")


def _banner_signature(gameid):
    try:
        st = os.stat(banner_path(gameid))
    except OSError:
        return None
    return f"{st.st_size}:{int(st.st_mtime)}"


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def cached_hd(gameid):
    """Path of an HD banner that still matches the game's current banner, else None."""
    sig = _banner_signature(gameid)
    if sig is None or not os.path.isfile(hd_path(gameid)):
        return None
    return hd_path(gameid) if _read(_src_path(gameid)) == sig else None


def _recent_miss(gameid, sig):
    try:
        fresh = time.time() - os.path.getmtime(_miss_path(gameid)) < MISS_TTL_SECONDS
    except OSError:
        return False
    return fresh and _read(_miss_path(gameid)) == sig


def request_hd(game, callback=None):
    """Fetch an HD banner in the background; `callback(path)` on the main loop."""
    gameid = game.gameid
    path = cached_hd(gameid)
    if path:
        return path
    sig = _banner_signature(gameid)
    if sig is None or gameid in _inflight or _recent_miss(gameid, sig):
        return None
    api_key = _api_key()
    if not api_key:
        return None

    _inflight.add(gameid)

    def work():
        result = None
        try:
            result = _fetch(gameid, game.title, getattr(game, "steamgriddb_id", ""), api_key, sig)
        except Exception:
            result = None

        def finish():
            _inflight.discard(gameid)
            if result and callback is not None:
                callback(result)
            return False

        GLib.idle_add(finish)

    _executor.submit(work)
    return None


def _thumb_vector(image):
    from PIL import Image
    return list(image.convert("L").resize((96, 31), Image.BILINEAR).getdata())


def _difference(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _fetch(gameid, title, sgdb_id, api_key, sig):
    import requests
    from PIL import Image
    from faugus.core import get_steamgriddb_session

    session = get_steamgriddb_session()
    headers = {"Authorization": f"Bearer {api_key}"}

    with Image.open(banner_path(gameid)) as local:
        local_vec = _thumb_vector(local)

    try:
        game_id = resolve_sgdb_game_id(session, headers, title, sgdb_id)
        if game_id is None:
            _mark_miss(gameid, sig)
            return None
        response = session.get(
            f"{API}/heroes/game/{game_id}",
            headers=headers,
            params={"types": "static", "nsfw": "false", "humor": "false"},
            timeout=10,
        )
        response.raise_for_status()
        heroes = [
            h for h in (response.json().get("data") or [])
            if h.get("url") and h.get("thumb") and (h.get("width") or 0) >= MIN_HD_WIDTH
        ][:MAX_CANDIDATES]

        best, best_diff = None, None
        for hero in heroes:
            try:
                thumb = session.get(hero["thumb"], timeout=10)
                thumb.raise_for_status()
                with Image.open(io.BytesIO(thumb.content)) as image:
                    diff = _difference(local_vec, _thumb_vector(image))
            except (requests.RequestException, OSError):
                continue
            if best_diff is None or diff < best_diff:
                best, best_diff = hero, diff

        if best is None or best_diff > MATCH_THRESHOLD:
            _mark_miss(gameid, sig)
            return None

        full = session.get(best["url"], timeout=30)
        full.raise_for_status()
    except requests.RequestException:
        return None  # network trouble: try again next time

    try:
        with Image.open(io.BytesIO(full.content)) as image:
            image = image.convert("RGB")
            if image.width > MAX_HD_W or image.height > MAX_HD_H:
                image.thumbnail((MAX_HD_W, MAX_HD_H), Image.LANCZOS)
            os.makedirs(HD_DIR, exist_ok=True)
            tmp = hd_path(gameid) + ".tmp"
            image.save(tmp, "JPEG", quality=92)
        os.replace(tmp, hd_path(gameid))
        with open(_src_path(gameid), "w", encoding="utf-8") as f:
            f.write(sig)
    except OSError:
        return None
    try:
        os.remove(_miss_path(gameid))
    except OSError:
        pass
    return hd_path(gameid)


def _mark_miss(gameid, sig):
    try:
        os.makedirs(HD_DIR, exist_ok=True)
        with open(_miss_path(gameid), "w", encoding="utf-8") as f:
            f.write(sig)
    except OSError:
        pass
