"""Faugus X console-style launch screen.

Shown over the whole window when a game starts from Showcase:

    ┌──────────────────────────────────────────────┐
    │   (game banner, slowly zooming, darkened)     │
    │                                              │
    │                 ┌────────┐                   │
    │                 │ cover  │  ← glows, pops in │
    │                 └────────┘                   │
    │              Game Title                      │
    │          Updating Proton-CachyOS…            │
    │          ━━━━━━━━━━━━━━━━━━━━━━━             │
    │                          Esc / B to hide     │
    └──────────────────────────────────────────────┘

Status text and progress come from the runner via faugus/xbridge.py.
The screen fades out when the runner reports the game process has started.
"""

import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from faugus.path_manager import BANNERS_DIR
from faugus.utils import HiDpiPaintable, add_css_once, load_full_texture, new_picture
from faugus.xbridge import STATUS_DIR_ENV, read_status

COVER_W, COVER_H = 260, 390
LOGO_MAX_W, LOGO_MAX_H = 520, 150
ENTER_MS = 550
EXIT_MS = 600
ZOOM_SECONDS = 14.0
ZOOM_AMOUNT = 0.10
POLL_MS = 150
FALLBACK_TIMEOUT_S = 45
# After the runner reports the game process, keep covering the menu until the
# game window takes focus away from Faugus X (or this many seconds pass).
WINDOW_WAIT_TIMEOUT_S = 90
INACTIVE_HIDE_DELAY_MS = 250

LAUNCH_CSS = """
.launch-screen {
    background-color: black;
}
.launch-banner {
    opacity: 0.55;
}
.launch-scrim {
    background-image:
        radial-gradient(ellipse at center, alpha(black, 0.15) 0%, alpha(black, 0.75) 70%, black 100%);
}
.launch-cover {
    border-radius: 18px;
    box-shadow: 0 24px 60px alpha(black, 0.75),
                0 0 0 2px alpha(white, 0.08),
                0 0 60px 6px alpha(@accent_bg_color, 0.45);
}
.launch-title {
    color: white;
    font-size: 40px;
    font-weight: 800;
    text-shadow: 0 2px 10px alpha(black, 0.8);
}
.launch-status {
    color: alpha(white, 0.8);
    font-size: 17px;
    font-weight: 500;
}
progressbar.launch-progress > trough {
    min-height: 4px;
    min-width: 360px;
    border-radius: 2px;
    background: alpha(white, 0.15);
    border: none;
}
progressbar.launch-progress > trough > progress {
    min-height: 4px;
    border-radius: 2px;
    border: none;
    background-image: linear-gradient(to right, alpha(@accent_bg_color, 0.6), @accent_bg_color, white);
    box-shadow: 0 0 12px alpha(@accent_bg_color, 0.8);
}
.launch-hint {
    color: alpha(white, 0.45);
    font-size: 13px;
    letter-spacing: 1px;
}
"""


class LaunchScreen:
    def __init__(self, win):
        self.win = win
        self.game = None
        self.started_at = 0.0
        self._poll_id = None
        self._pulse_id = None
        self._zoom_tick = None
        self._anim = None
        self._closing = False
        self._busy = False
        self._waiting_window = False
        self._wait_timeout_id = None
        self._inactive_id = None
        win.connect("notify::is-active", self._on_active_changed)

        self.status_dir = os.path.join(GLib.get_user_runtime_dir(), "faugus-x")
        os.makedirs(self.status_dir, exist_ok=True)
        os.environ[STATUS_DIR_ENV] = self.status_dir

        add_css_once("launch_screen", LAUNCH_CSS, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self):
        self.root = Gtk.Overlay()
        self.root.add_css_class("launch-screen")
        self.root.set_visible(False)
        self.root.set_can_target(True)

        self.banner = Gtk.Picture()
        self.banner.add_css_class("launch-banner")
        self.banner.set_content_fit(Gtk.ContentFit.COVER)
        self.banner.set_can_shrink(True)
        self.banner.set_hexpand(True)
        self.banner.set_vexpand(True)
        self._banner_css = Gtk.CssProvider()
        self.banner.get_style_context().add_provider(self._banner_css, Gtk.STYLE_PROVIDER_PRIORITY_USER + 2)
        self.root.set_child(self.banner)

        scrim = Gtk.Box()
        scrim.add_css_class("launch-scrim")
        scrim.set_can_target(False)
        self.root.add_overlay(scrim)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        self.center = center

        self.cover = new_picture()
        self.cover.set_size_request(COVER_W, COVER_H)
        self.cover.set_content_fit(Gtk.ContentFit.COVER)
        cover_frame = Gtk.Box()
        cover_frame.add_css_class("launch-cover")
        cover_frame.set_overflow(Gtk.Overflow.HIDDEN)
        cover_frame.set_halign(Gtk.Align.CENTER)
        cover_frame.append(self.cover)
        self.cover_frame = cover_frame
        self._cover_css = Gtk.CssProvider()
        cover_frame.get_style_context().add_provider(self._cover_css, Gtk.STYLE_PROVIDER_PRIORITY_USER + 2)
        center.append(cover_frame)

        self.title = Gtk.Label()
        self.title.add_css_class("launch-title")
        self.title.set_margin_top(18)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_max_width_chars(40)
        center.append(self.title)

        self.logo = Gtk.Picture()
        self.logo.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.logo.set_halign(Gtk.Align.CENTER)
        self.logo.set_margin_top(18)
        self.logo.set_visible(False)
        center.append(self.logo)

        self.status = Gtk.Label()
        self.status.add_css_class("launch-status")
        center.append(self.status)

        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("launch-progress")
        self.progress.set_halign(Gtk.Align.CENTER)
        self.progress.set_pulse_step(0.08)
        center.append(self.progress)

        self.root.add_overlay(center)

        hint = Gtk.Label(label=_("Esc / B to hide"))
        hint.add_css_class("launch-hint")
        hint.set_halign(Gtk.Align.END)
        hint.set_valign(Gtk.Align.END)
        hint.set_margin_end(36)
        hint.set_margin_bottom(28)
        self.root.add_overlay(hint)

        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.hide())
        self.root.add_controller(click)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.win.add_controller(keys)
        self._hook_gamepad()

    def _hook_gamepad(self):
        """Let the controller's B button hide the screen (and ignore other buttons meanwhile)."""
        try:
            import faugus.gamepad as gamepad
        except Exception:
            return
        original = getattr(gamepad, "_handle_button_down", None)
        if original is None or getattr(original, "_faugus_x_wrapped", False):
            return

        def handle_button_down(window, button, win):
            screen = getattr(getattr(window, "showcase", None), "launch_screen", None)
            if screen is not None and screen.root.get_visible():
                if gamepad.BUTTON_ROLES.get(button) == "back":
                    screen.hide()
                return None
            return original(window, button, win)

        handle_button_down._faugus_x_wrapped = True
        gamepad._handle_button_down = handle_button_down

    def attach(self):
        """Wrap the window's content in an overlay so the screen covers everything."""
        win = self.win
        current = win.get_child()
        if current is None or getattr(self, "_host", None) is current:
            return
        host = Gtk.Overlay()
        win.set_child(None)
        host.set_child(current)
        host.add_overlay(self.root)
        win.set_child(host)
        self._host = host

    # ------------------------------------------------------------------ show / hide

    def show(self, game):
        self.attach()
        self._stop_timers()
        self.game = game
        self.started_at = time.time()
        self._closing = False
        self._busy = False
        self._waiting_window = False

        self.title.set_text(game.title)
        self._set_logo(game)
        self.status.set_text(_("Starting…"))
        self.progress.set_fraction(0.0)
        self._set_cover(game)
        self._set_banner(game)

        self.root.set_opacity(0.0)
        self.root.set_visible(True)
        self.root.grab_focus()

        def enter(value):
            self.root.set_opacity(min(1.0, value * 1.4))
            scale = 0.82 + 0.18 * value
            lift = (1.0 - value) * 40
            self._cover_css.load_from_data(
                f".launch-cover {{ transform: translateY({lift:.1f}px) scale({scale:.4f}); }}".encode()
            )
            self.title.set_opacity(max(0.0, (value - 0.35) / 0.65))
            self.logo.set_opacity(max(0.0, (value - 0.35) / 0.65))
            self.status.set_opacity(max(0.0, (value - 0.5) / 0.5))

        self._play(enter, ENTER_MS, Adw.Easing.EASE_OUT_BACK)
        self._zoom_tick = self.root.add_tick_callback(self._on_zoom_tick)
        self._pulse_id = GLib.timeout_add(110, self._on_pulse)
        self._poll_id = GLib.timeout_add(POLL_MS, self._on_poll)

    def hide(self, *_args, instant=False):
        if not self.root.get_visible() or (self._closing and not instant):
            return
        self._closing = True
        self._waiting_window = False
        self._stop_timers(keep_zoom=not instant)
        start = self.root.get_opacity()

        def done(*_):
            self._stop_timers()
            self.root.set_visible(False)
            self._closing = False
            fixed = getattr(self.win, "carrousel_fixed", None)
            if fixed is not None:
                fixed.grab_focus()

        if instant:
            # The game window is already on top: drop the overlay without
            # animating so the menu never flashes when the user returns.
            if self._anim is not None:
                self._anim.pause()
            done()
            return

        def leave(value):
            self.root.set_opacity(start * (1.0 - value))

        self._play(leave, EXIT_MS, Adw.Easing.EASE_IN_OUT_CUBIC, done)

    def _on_active_changed(self, *_args):
        if self.win.is_active() or not self.root.get_visible() or self._closing:
            return
        # Faugus X lost focus. If the game is no longer downloading updates,
        # this is the game window appearing: hide behind it.
        if self._busy and not self._waiting_window:
            return
        if self._inactive_id is not None:
            GLib.source_remove(self._inactive_id)

        def fire():
            self._inactive_id = None
            if not self.win.is_active() and self.root.get_visible():
                self.hide(instant=True)
            return False

        self._inactive_id = GLib.timeout_add(INACTIVE_HIDE_DELAY_MS, fire)

    def _wait_for_game_window(self):
        self._waiting_window = True
        self._determinate = False
        self.status.set_text(_("Starting game…"))
        if not self.win.is_active():
            self._on_active_changed()
            return

        def give_up():
            self._wait_timeout_id = None
            self.hide()
            return False

        self._wait_timeout_id = GLib.timeout_add_seconds(WINDOW_WAIT_TIMEOUT_S, give_up)

    def game_stopped(self, gameid):
        if self.game is not None and self.game.gameid == gameid and self.root.get_visible():
            self.hide()

    # ------------------------------------------------------------------ content

    def _set_cover(self, game):
        win = self.win
        self.cover.set_paintable(win.get_cover_placeholder(COVER_W, COVER_H))
        if os.path.isfile(game.cover):
            key = win.cover_texture_key(game.cover, win.is_game_installed(game))
            win.cover_textures.load(
                key, lambda texture: self.cover.set_paintable(HiDpiPaintable(texture, COVER_W, COVER_H))
            )
        elif game.icon and os.path.isfile(game.icon):
            try:
                self.cover.set_paintable(load_full_texture(game.icon))
            except GLib.Error:
                pass

    def _set_logo(self, game):
        from faugus.logos import cached_logo
        path = cached_logo(game.gameid)
        texture = None
        if path:
            try:
                texture = load_full_texture(path)
            except GLib.Error:
                texture = None
        if texture is None or not texture.get_width() or not texture.get_height():
            self.logo.set_visible(False)
            self.title.set_visible(True)
            return
        scale = min(LOGO_MAX_W / texture.get_width(), LOGO_MAX_H / texture.get_height())
        w, h = int(texture.get_width() * scale), int(texture.get_height() * scale)
        self.logo.set_paintable(HiDpiPaintable(texture, w, h))
        self.logo.set_size_request(w, h)
        self.logo.set_visible(True)
        self.title.set_visible(False)

    def _set_banner(self, game):
        self.banner.set_paintable(None)
        path = f"{BANNERS_DIR}/{game.gameid}.png"
        if not os.path.isfile(path):
            path = game.cover if os.path.isfile(game.cover) else None
        if path:
            try:
                self.banner.set_paintable(load_full_texture(path))
            except GLib.Error:
                pass

    # ------------------------------------------------------------------ timers

    def _on_zoom_tick(self, _widget, clock):
        elapsed = time.time() - self.started_at
        t = min(1.0, elapsed / ZOOM_SECONDS)
        scale = 1.0 + ZOOM_AMOUNT * (1 - (1 - t) ** 2)
        self._banner_css.load_from_data(f".launch-banner {{ transform: scale({scale:.4f}); }}".encode())
        return GLib.SOURCE_CONTINUE

    def _on_pulse(self):
        if getattr(self, "_determinate", False):
            return True
        self.progress.pulse()
        return True

    def _on_poll(self):
        if self.game is None:
            return False
        state = read_status(self.status_dir, self.game.gameid)
        fresh = state is not None and state.get("time", 0) >= self.started_at - 1.0
        elapsed = time.time() - self.started_at

        if fresh:
            label = state.get("label")
            if label:
                self.status.set_text(label)
            fraction = state.get("fraction")
            self._determinate = fraction is not None
            if fraction is not None:
                self.progress.set_fraction(fraction)
            if state.get("closed"):
                self._poll_id = None
                self._busy = False
                self._wait_for_game_window()
                return False
            busy = bool(state.get("pulse") or fraction is not None)
        else:
            busy = False
        self._busy = busy

        if elapsed > FALLBACK_TIMEOUT_S and not busy and not self.win.is_active():
            self._poll_id = None
            self.hide(instant=True)
            return False
        if elapsed > WINDOW_WAIT_TIMEOUT_S and not busy:
            self._poll_id = None
            self.hide()
            return False
        return True

    def _play(self, step, duration, easing, done=None):
        if self._anim is not None:
            self._anim.pause()
        target = Adw.CallbackAnimationTarget.new(step)
        anim = Adw.TimedAnimation.new(self.root, 0.0, 1.0, duration, target)
        anim.set_easing(easing)
        if done is not None:
            anim.connect("done", done)
        self._anim = anim
        anim.play()

    def _stop_timers(self, keep_zoom=False):
        for attr in ("_poll_id", "_pulse_id", "_wait_timeout_id", "_inactive_id"):
            source = getattr(self, attr)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, attr, None)
        if not keep_zoom and self._zoom_tick is not None:
            self.root.remove_tick_callback(self._zoom_tick)
            self._zoom_tick = None
        self._determinate = False

    def _on_key(self, _controller, keyval, _keycode, _state):
        if not self.root.get_visible():
            return False
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            self.hide()
            return True
        return True  # swallow input while the launch screen is up
