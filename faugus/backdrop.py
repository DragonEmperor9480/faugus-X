"""Faugus X: full-screen animated banner backdrop for Showcase.

Two picture layers crossfade when the game changes. The visible layer drifts
and zooms very slowly (a "Ken Burns" effect) so the home screen feels alive.
A new banner arrives slightly zoomed in and settles into the drift.
Games without a banner fall back to their cover, blurred and enlarged.
"""

import math
import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk

from faugus.hd_banners import cached_hd, request_hd

from faugus.path_manager import BANNERS_DIR
from faugus.utils import add_css_once, load_full_texture

CROSSFADE_MS = 900
SETTLE_SECONDS = 1.6
SETTLE_ZOOM = 0.06
BASE_ZOOM = 1.07
DRIFT_ZOOM = 0.05
DRIFT_X_PX = 22
DRIFT_Y_PX = 12
BLUR_RADIUS = 28
CACHE_SIZE = 7  # HD banners are ~19 MB each in memory

BACKDROP_CSS = """
.backdrop {
    background-color: #07080c;
}
.backdrop-scrim {
    background-image:
        linear-gradient(to right, alpha(black, 0.62) 0%, alpha(black, 0.25) 42%, transparent 70%),
        linear-gradient(to top, alpha(black, 0.88) 0%, alpha(black, 0.35) 38%, transparent 62%);
}
.backdrop-top-scrim {
    background-image: linear-gradient(to bottom, alpha(black, 0.45), transparent 160px);
}
.backdrop-dim {
    background-color: #07080c;
}
"""

_loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="faugus-x-backdrop")


class _Layer(Gtk.Widget):
    """A full-size picture drawn with a transform applied in snapshot().

    Animating via a CSS transform meant re-parsing CSS and restyling every
    frame, which cost ~20 fps and made the carrousel slide stutter. Drawing
    the transform directly only needs a redraw.
    """

    __gtype_name__ = "FaugusXBackdropLayer"

    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_target(False)
        self.paintable = None
        self.age = 0.0  # seconds of drift since this layer was shown
        self.blur = False
        self.seed = 0.0
        self._transform = (1.0, 0.0, 0.0)

    def set_paintable(self, paintable):
        self.paintable = paintable
        self.queue_draw()

    def set_transform(self, scale, tx, ty):
        transform = (round(scale, 5), round(tx, 2), round(ty, 2))
        if transform != self._transform:
            self._transform = transform
            self.queue_draw()

    def do_snapshot(self, snapshot):
        paintable = self.paintable
        width, height = self.get_width(), self.get_height()
        if paintable is None or width <= 0 or height <= 0:
            return
        iw, ih = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
        if iw <= 0 or ih <= 0:
            return
        cover = max(width / iw, height / ih)
        dw, dh = iw * cover, ih * cover
        scale, tx, ty = self._transform

        snapshot.save()
        snapshot.translate(Graphene.Point().init(width / 2 + tx, height / 2 + ty))
        snapshot.scale(scale, scale)
        snapshot.translate(Graphene.Point().init(-dw / 2, -dh / 2))
        if self.blur:
            snapshot.push_blur(BLUR_RADIUS)
        if isinstance(paintable, Gdk.Texture):
            # Trilinear (mipmapped) filtering keeps HD banners crisp when
            # they're scaled down to the screen.
            snapshot.append_scaled_texture(paintable, Gsk.ScalingFilter.TRILINEAR,
                                           Graphene.Rect().init(0, 0, dw, dh))
        else:
            paintable.snapshot(snapshot, dw, dh)
        if self.blur:
            snapshot.pop()
            shade = Gdk.RGBA()
            shade.parse("rgba(0,0,0,0.2)")
            snapshot.append_color(shade, Graphene.Rect().init(0, 0, dw, dh))
        snapshot.restore()


class Backdrop:
    def __init__(self):
        add_css_once("faugus_x_backdrop", BACKDROP_CSS, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

        self.widget = Gtk.Overlay()
        self.widget.add_css_class("backdrop")
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)
        self.widget.set_overflow(Gtk.Overflow.HIDDEN)

        base = Gtk.Box()
        base.set_hexpand(True)
        base.set_vexpand(True)
        self.widget.set_child(base)

        self.layers = [_Layer(), _Layer()]
        for layer in self.layers:
            layer.set_opacity(0.0)
            self.widget.add_overlay(layer)
            self.widget.set_measure_overlay(layer, False)
            self.widget.set_clip_overlay(layer, True)
        self.front = 0

        for css_class in ("backdrop-scrim", "backdrop-top-scrim"):
            scrim = Gtk.Box()
            scrim.add_css_class(css_class)
            scrim.set_can_target(False)
            self.widget.add_overlay(scrim)

        self.dim = Gtk.Box()
        self.dim.add_css_class("backdrop-dim")
        self.dim.set_can_target(False)
        self.dim.set_opacity(0.0)
        self.widget.add_overlay(self.dim)

        self.scrims_visible = True
        self._cache = OrderedDict()
        self._prefetching = set()
        self._current_gameid = None
        self._request = 0
        self._current_path = None
        self._fade = None
        self._last_tick = None
        # Callable returning True while something else is animating (e.g. the
        # carrousel sliding). The drift then holds still so GTK only redraws
        # the moving part instead of the whole screen — a big saving on iGPUs.
        self.pause_when = None
        self.widget.add_tick_callback(self._on_tick)

    # ------------------------------------------------------------------ public

    def show_game(self, game, instant=False):
        self._current_gameid = game.gameid if game is not None else None
        if game is not None and os.path.isfile(f"{BANNERS_DIR}/{game.gameid}.png") and cached_hd(game.gameid) is None:
            # Fetch the full-resolution banner; switch to it if still showing this game.
            request_hd(game, lambda _p, g=game: self._current_gameid == g.gameid and self.show_game(g))
        path, blur = self._source_for(game)
        if path is None:
            self._swap(None, False, instant)
            return
        if path == self._current_path:
            return
        self._current_path = path
        self._request += 1
        request = self._request

        texture = self._cache_get(path)
        if texture is not None:
            self._swap(texture, blur, instant)
            return

        def load():
            try:
                tex = load_full_texture(path)
            except GLib.Error:
                tex = None

            def apply():
                if tex is not None:
                    self._cache_put(path, tex)
                if request == self._request:
                    self._swap(tex, blur, instant)
                return False

            GLib.idle_add(apply)

        _loader.submit(load)

    def prefetch(self, games):
        """Decode neighbouring banners in the background so swaps are instant."""
        for game in games:
            path, _blur = self._source_for(game)
            if path is None or self._cache_get(path) is not None or path in self._prefetching:
                continue
            self._prefetching.add(path)

            def load(path=path):
                try:
                    tex = load_full_texture(path)
                except GLib.Error:
                    tex = None

                def store():
                    self._prefetching.discard(path)
                    if tex is not None:
                        self._cache_put(path, tex)
                    return False

                GLib.idle_add(store)

            _loader.submit(load)

    def set_dim(self, amount):
        self.dim.set_opacity(max(0.0, min(0.7, amount)))

    @staticmethod
    def has_art(game):
        return os.path.isfile(f"{BANNERS_DIR}/{game.gameid}.png")

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _source_for(game):
        if game is None:
            return None, False
        hd = cached_hd(game.gameid)
        if hd:
            return hd, False
        banner = f"{BANNERS_DIR}/{game.gameid}.png"
        if os.path.isfile(banner):
            return banner, False
        if game.cover and os.path.isfile(game.cover):
            return game.cover, True
        return None, False

    def _swap(self, texture, blur, instant):
        incoming = self.layers[1 - self.front]
        outgoing = self.layers[self.front]
        incoming.set_paintable(texture)
        incoming.blur = blur
        incoming.age = 0.0
        incoming.seed = (incoming.seed + 1.618) % (2 * math.pi)
        self.front = 1 - self.front

        if self._fade is not None:
            self._fade.skip()

        if instant:
            incoming.set_opacity(1.0)
            outgoing.set_opacity(0.0)
            return

        start_out = outgoing.get_opacity()

        def step(value):
            incoming.set_opacity(value)
            outgoing.set_opacity(start_out * (1.0 - value))

        target = Adw.CallbackAnimationTarget.new(step)
        fade = Adw.TimedAnimation.new(self.widget, 0.0, 1.0, CROSSFADE_MS, target)
        fade.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        self._fade = fade
        fade.play()

    def _on_tick(self, _widget, _clock):
        now = time.monotonic()
        dt = 0.0 if self._last_tick is None else min(0.1, now - self._last_tick)
        self._last_tick = now
        if self.pause_when is not None and self.pause_when():
            return GLib.SOURCE_CONTINUE
        for layer in self.layers:
            if layer.get_opacity() <= 0.0:
                continue
            layer.age += dt
            t = layer.age
            s = layer.seed
            drift = 0.5 - 0.5 * math.cos(2 * math.pi * t / 44.0 + s)
            settle = 1.0 - min(1.0, t / SETTLE_SECONDS)
            settle = settle * settle * settle
            scale = BASE_ZOOM + DRIFT_ZOOM * drift + SETTLE_ZOOM * settle
            tx = DRIFT_X_PX * math.sin(2 * math.pi * t / 58.0 + s)
            ty = DRIFT_Y_PX * math.sin(2 * math.pi * t / 47.0 + s * 0.7)
            layer.set_transform(scale, tx, ty)
        return GLib.SOURCE_CONTINUE

    def _cache_get(self, path):
        entry = self._cache.get(path)
        if entry is None:
            return None
        mtime, texture = entry
        try:
            if os.path.getmtime(path) != mtime:
                return None
        except OSError:
            return None
        self._cache.move_to_end(path)
        return texture

    def _cache_put(self, path, texture):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        self._cache[path] = (mtime, texture)
        self._cache.move_to_end(path)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
