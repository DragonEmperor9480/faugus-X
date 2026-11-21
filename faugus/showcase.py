"""Showcase interface mode for Faugus X.

A console-style home screen layered on top of the Carrousel engine:

    ┌───────────────────────────────────────────────┐
    │ FAUGUS X                               12:34  │  top bar
    │                                               │
    │  Game Title                                   │  hero (animated on change)
    │  Category • Playtime • Last played            │
    │  [ ▶ Play ]  [ ⋯ ]                            │
    │                                               │
    │     ▢  ▢  ▣  ▢  ▢     (existing carrousel)    │
    │                 ⌄ Library                     │
    ├───────────────────────────────────────────────┤  scroll down
    │  Your Library · 10 games                      │
    │  ▢ ▢ ▢ ▢ ▢ ▢ ▢                                │  cover grid
    └───────────────────────────────────────────────┘

Everything here lives in this file so upstream Faugus changes rarely conflict.
The window keeps interface_mode == "Carrousel" internally; `showcase_enabled`
marks that this layer is active. See the small hooks in launcher.py.
"""

import os
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from faugus.utils import HiDpiPaintable, add_css_once, new_picture, play_ui_sound

# `_` (gettext) is installed into builtins by launcher.py's setup_gettext().

HERO_SLIDE_PX = 48
HERO_ANIM_MS = 420
SCROLL_ANIM_MS = 520
BANNER_FADE_MS = 450
TILE_W, TILE_H = 168, 252

SHOWCASE_CSS = """
.showcase-hero {
    background-image: linear-gradient(to right, alpha(black, 0.55), alpha(black, 0.15) 45%, transparent 70%);
}
.showcase-brand {
    font-weight: 800;
    font-size: 15px;
    letter-spacing: 4px;
    opacity: 0.85;
}
.showcase-clock {
    font-size: 20px;
    font-weight: 600;
    font-feature-settings: "tnum";
    opacity: 0.9;
}
.showcase-brand, .showcase-clock, .showcase-title, .showcase-meta {
    color: white;
    text-shadow: 0 2px 6px alpha(black, 0.7);
}
.showcase-title {
    font-size: 64px;
    font-weight: 800;
    letter-spacing: -1px;
}
.showcase-meta {
    font-size: 18px;
    opacity: 0.85;
}
button.showcase-play {
    min-height: 52px;
    min-width: 170px;
    padding: 0 28px;
    border-radius: 26px;
    border: none;
    background: white;
    color: black;
    font-size: 19px;
    font-weight: 800;
    box-shadow: 0 6px 20px alpha(black, 0.45);
    transition: transform 180ms ease, box-shadow 180ms ease, background 180ms ease;
}
button.showcase-play:hover,
button.showcase-play:focus-visible {
    transform: scale(1.05);
    box-shadow: 0 8px 28px alpha(black, 0.5), 0 0 0 3px alpha(@accent_bg_color, 0.9);
}
button.showcase-play.running {
    background: #e5484d;
    color: white;
}
button.showcase-round {
    min-width: 52px;
    min-height: 52px;
    padding: 0;
    border-radius: 26px;
    border: none;
    background: alpha(white, 0.16);
    color: white;
    transition: transform 180ms ease, background 180ms ease;
}
button.showcase-round:hover,
button.showcase-round:focus-visible {
    background: alpha(white, 0.28);
    transform: scale(1.06);
}
button.showcase-round.showcase-small {
    min-width: 40px;
    min-height: 40px;
    border-radius: 20px;
}
button.showcase-hint {
    border: none;
    background: none;
    box-shadow: none;
    color: white;
    opacity: 0.7;
    font-weight: 600;
    letter-spacing: 1px;
    transition: opacity 200ms ease;
}
button.showcase-hint:hover { opacity: 1; }
.showcase-library {
    background-image: linear-gradient(to bottom, transparent, alpha(@window_bg_color, 0.94) 140px);
    padding-bottom: 40px;
}
.showcase-library-title {
    font-size: 28px;
    font-weight: 800;
}
.showcase-library-count {
    font-size: 16px;
    opacity: 0.6;
}
flowbox.showcase-grid > flowboxchild {
    padding: 0;
    margin: 0;
    background: none;
    border-radius: 14px;
    outline: none;
}
.showcase-tile {
    border-radius: 14px;
    box-shadow: 0 6px 16px alpha(black, 0.45);
    transition: transform 200ms ease, box-shadow 200ms ease;
}
flowboxchild:hover .showcase-tile {
    transform: translateY(-6px) scale(1.04);
    box-shadow: 0 14px 28px alpha(black, 0.55);
}
flowboxchild:focus-visible .showcase-tile,
flowboxchild.showcase-current .showcase-tile {
    transform: translateY(-6px) scale(1.04);
    box-shadow: 0 14px 28px alpha(black, 0.55), 0 0 0 3px @accent_bg_color, 0 0 24px alpha(@accent_bg_color, 0.6);
}
.showcase-tile-label {
    font-size: 14px;
    font-weight: 600;
    opacity: 0.85;
}
"""


class Showcase:
    def __init__(self, win):
        self.win = win
        self._hero_gameid = None
        self._hero_anim = None
        self._scroll_anim = None
        self._library_key = None
        self._tiles = {}

        add_css_once("showcase", SHOWCASE_CSS, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self._build()
        self._install_hooks()
        self._tick_clock()
        GLib.timeout_add_seconds(15, self._tick_clock)

    # ------------------------------------------------------------------ build

    def _build(self):
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)
        self.scroller.set_kinetic_scrolling(True)
        self.vadj = self.scroller.get_vadjustment()
        self.vadj.connect("notify::page-size", lambda *_: self._fit_hero())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.scroller.set_child(content)

        # ---- hero page: fills exactly one screen
        self.hero_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.hero_page.add_css_class("showcase-hero")
        content.append(self.hero_page)

        top = Gtk.CenterBox()
        top.set_margin_top(22)
        top.set_margin_start(48)
        top.set_margin_end(48)
        brand = Gtk.Label(label="FAUGUS X")
        brand.add_css_class("showcase-brand")
        self.clock = Gtk.Label()
        self.clock.add_css_class("showcase-clock")

        self.fullscreen_button = Gtk.Button()
        self.fullscreen_button.add_css_class("showcase-round")
        self.fullscreen_button.add_css_class("showcase-small")
        self.fullscreen_button.set_valign(Gtk.Align.CENTER)
        self.fullscreen_button.connect("clicked", lambda *_: self.toggle_fullscreen())
        self.win.connect("notify::fullscreened", lambda *_: self._sync_fullscreen_button())
        self._sync_fullscreen_button()

        top_end = Gtk.Box(spacing=18)
        top_end.append(self.clock)
        top_end.append(self.fullscreen_button)

        top.set_start_widget(brand)
        top.set_end_widget(top_end)
        self.hero_page.append(top)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        self.hero_page.append(spacer)

        self.hero_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.hero_info.set_halign(Gtk.Align.START)
        self.hero_info.set_margin_start(80)
        self.hero_info.set_margin_end(80)

        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("showcase-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_max_width_chars(40)
        self.hero_info.append(self.title)

        self.meta = Gtk.Label(xalign=0)
        self.meta.add_css_class("showcase-meta")
        self.hero_info.append(self.meta)

        actions = Gtk.Box(spacing=14)
        actions.set_margin_top(12)
        self.play_button = Gtk.Button()
        self.play_button.add_css_class("showcase-play")
        self.play_button.connect("clicked", self._on_play)
        actions.append(self.play_button)

        self.more_button = Gtk.Button.new_from_icon_name("view-more-horizontal-symbolic")
        self.more_button.add_css_class("showcase-round")
        self.more_button.set_tooltip_text(_("Options"))
        self.more_button.connect("clicked", self._on_more)
        actions.append(self.more_button)
        self.hero_info.append(actions)

        self.hero_page.append(self.hero_info)

        self.carrousel_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.carrousel_slot.set_margin_top(8)
        self.hero_page.append(self.carrousel_slot)

        hint = Gtk.Button()
        hint.add_css_class("showcase-hint")
        hint_box = Gtk.Box(spacing=8)
        hint_box.append(Gtk.Label(label=_("Library")))
        hint_box.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        hint.set_child(hint_box)
        hint.set_halign(Gtk.Align.CENTER)
        hint.set_margin_bottom(6)
        hint.set_focusable(False)
        hint.connect("clicked", lambda *_: self.scroll_to_library())
        self.hero_page.append(hint)

        # ---- library section
        library = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        library.add_css_class("showcase-library")
        library.set_margin_top(10)
        self.library_box = library

        header = Gtk.Box(spacing=14)
        header.set_margin_start(80)
        header.set_margin_end(80)
        header.set_margin_top(90)
        lib_title = Gtk.Label(label=_("Your Library"), xalign=0)
        lib_title.add_css_class("showcase-library-title")
        self.library_count = Gtk.Label(xalign=0)
        self.library_count.add_css_class("showcase-library-count")
        self.library_count.set_valign(Gtk.Align.BASELINE)
        header.append(lib_title)
        header.append(self.library_count)
        library.append(header)

        self.grid = Gtk.FlowBox()
        self.grid.add_css_class("showcase-grid")
        self.grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.grid.set_homogeneous(True)
        self.grid.set_row_spacing(28)
        self.grid.set_column_spacing(24)
        self.grid.set_min_children_per_line(2)
        self.grid.set_max_children_per_line(12)
        self.grid.set_margin_start(80)
        self.grid.set_margin_end(80)
        self.grid.set_activate_on_single_click(False)
        self.grid.connect("child-activated", self._on_tile_activated)

        grid_keys = Gtk.EventControllerKey()
        grid_keys.connect("key-pressed", self._on_grid_key)
        self.grid.add_controller(grid_keys)
        library.append(self.grid)

        content.append(library)

    def attach_carrousel(self, carrousel_box):
        parent = carrousel_box.get_parent()
        if parent is not None and parent is not self.carrousel_slot:
            parent.remove(carrousel_box)
        if carrousel_box.get_parent() is None:
            self.carrousel_slot.append(carrousel_box)
        carrousel_box.set_valign(Gtk.Align.END)

        parent = self.scroller.get_parent()
        if parent is not None:
            parent.remove(self.scroller)

        stack_banner = getattr(self.win, "stack_banner", None)
        if stack_banner is not None:
            stack_banner.set_transition_duration(BANNER_FADE_MS)

        return self.scroller

    def _fit_hero(self):
        page = int(self.vadj.get_page_size())
        if page > 0:
            self.hero_page.set_size_request(-1, page)

    # ------------------------------------------------------------------ hooks

    def _install_hooks(self):
        """Wrap a few window methods on this instance only (no class patching)."""
        win = self.win

        from faugus.launch_screen import LaunchScreen
        self.launch_screen = LaunchScreen(win)
        self._known_running = set(win.running)

        original_update_icon = win.update_icon

        def update_icon(*args, **kwargs):
            original_update_icon(*args, **kwargs)
            self._sync_running()
            self.refresh_hero()

        win.update_icon = update_icon

        original_render = win.render_carrousel

        def render_carrousel(*args, **kwargs):
            original_render(*args, **kwargs)
            self.refresh_library()
            self.refresh_hero()

        win.render_carrousel = render_carrousel

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_window_key)
        win.add_controller(keys)
        win.set_title("Faugus X")

        # Showcase is a console-style home screen: open fullscreen by default.
        win.fullscreen_activated = True
        win.fullscreen()

        # Developer aid: preview the launch screen without starting a game.
        if os.environ.get("FAUGUS_X_DEMO_LAUNCH"):
            def demo():
                game = win.selected()
                if game is not None:
                    self.launch_screen.show(game)
                return False
            GLib.timeout_add(2500, demo)

    def _sync_running(self):
        """Start the launch screen when a game appears in `running`, hide it when it leaves."""
        running = set(self.win.running)
        started = running - self._known_running
        stopped = self._known_running - running
        self._known_running = running

        for gameid in stopped:
            self.launch_screen.game_stopped(gameid)

        if started:
            gameid = next(iter(started))
            game = next((g for g in self.win.games if g.gameid == gameid), None)
            if game is not None:
                self.launch_screen.show(game)

    # ------------------------------------------------------------------ hero

    def refresh_hero(self, force=False):
        game = self.win.selected()
        if game is None:
            self.hero_info.set_visible(False)
            self._hero_gameid = None
            return
        self.hero_info.set_visible(True)

        running = game.gameid in self.win.running
        self.play_button.set_label(_("Stop") if running else _("Play"))
        if running:
            self.play_button.add_css_class("running")
        else:
            self.play_button.remove_css_class("running")

        self._mark_current_tile(game.gameid)

        if game.gameid == self._hero_gameid and not force:
            self.meta.set_text(self._meta_text(game))
            return

        self._hero_gameid = game.gameid
        self.title.set_text(game.title)
        self.meta.set_text(self._meta_text(game))
        self._animate_hero_in()

    def _meta_text(self, game):
        win = self.win
        parts = []
        categories = game.category if isinstance(game.category, list) else ([game.category] if game.category else [])
        categories = [c for c in categories if c and c != _("None")]
        if categories:
            parts.append(", ".join(sorted(categories, key=str.lower)))

        if game.gameid in win.running:
            session_start, _baseline = win.get_play_session(game)
            playing = win.format_playing_for(session_start)
            parts.append(_("Playing for: {}").format(playing) if playing else _("Playing"))
        else:
            playtime = win.format_playtime_display(game.playtime)
            if playtime:
                parts.append(_("Playtime: {}").format(playtime))
            last = win.format_last_played(game.last_played)
            parts.append(_("Last played: {}").format(last) if last else _("Never played"))
        return "  •  ".join(parts)

    def _animate_hero_in(self):
        if self._hero_anim is not None:
            self._hero_anim.skip()

        base = 80

        def step(value):
            self.hero_info.set_opacity(value)
            self.hero_info.set_margin_start(int(base + (1.0 - value) * HERO_SLIDE_PX))

        target = Adw.CallbackAnimationTarget.new(step)
        anim = Adw.TimedAnimation.new(self.hero_info, 0.0, 1.0, HERO_ANIM_MS, target)
        anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._hero_anim = anim
        anim.play()

    def _on_play(self, _button):
        game = self.win.selected()
        if game is None:
            return
        self.win.on_button_play_clicked()
        GLib.timeout_add(300, lambda: self.refresh_hero() or False)

    def _on_more(self, button):
        game = self.win.selected()
        if game is None:
            return
        rect = Gdk.Rectangle()
        rect.x, rect.y = 0, 0
        rect.width, rect.height = button.get_width() or 1, button.get_height() or 1
        self.win.open_context_menu(game, button, rect)
        self.win.context_menu.popup()

    # ------------------------------------------------------------------ library

    def refresh_library(self):
        games = self.win.carrousel_visible_games()
        key = tuple((g.gameid, g.title, g.cover, self._mtime(g.cover)) for g in games)
        if key == self._library_key:
            return
        self._library_key = key

        self.grid.remove_all()
        self._tiles = {}
        count = len(games)
        self.library_count.set_text(
            _("1 game") if count == 1 else _("{} games").format(count)
        )
        for game in games:
            self.grid.append(self._build_tile(game))
        if self._hero_gameid:
            self._mark_current_tile(self._hero_gameid)

    @staticmethod
    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except (OSError, TypeError):
            return 0

    def _build_tile(self, game):
        win = self.win
        picture = new_picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(TILE_W, TILE_H)
        picture.set_paintable(win.get_cover_placeholder(TILE_W, TILE_H))

        frame = Gtk.Box()
        frame.add_css_class("showcase-tile")
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.set_halign(Gtk.Align.CENTER)
        frame.append(picture)

        label = Gtk.Label(label=game.title)
        label.add_css_class("showcase-tile-label")
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(1)
        label.set_size_request(TILE_W, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(frame)
        box.append(label)

        child = Gtk.FlowBoxChild()
        child.set_child(box)
        child.game = game
        child.set_tooltip_text(game.title)

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", lambda g, n, x, y, c=child: self._on_tile_click(c, n))
        child.add_controller(click)

        right = Gtk.GestureClick()
        right.set_button(Gdk.BUTTON_SECONDARY)
        right.connect("released", lambda g, n, x, y, c=child: self._on_tile_menu(c, x, y))
        child.add_controller(right)

        if os.path.isfile(game.cover):
            key = win.cover_texture_key(game.cover, win.is_game_installed(game))

            def apply(texture, picture=picture):
                picture.set_paintable(HiDpiPaintable(texture, TILE_W, TILE_H))

            win.cover_textures.load(key, apply)

        self._tiles[game.gameid] = child
        return child

    def _mark_current_tile(self, gameid):
        for gid, child in self._tiles.items():
            if gid == gameid:
                child.add_css_class("showcase-current")
            else:
                child.remove_css_class("showcase-current")

    def select_game(self, game):
        win = self.win
        games = win.carrousel_visible_games()
        ids = [g.gameid for g in games]
        if game.gameid not in ids:
            return
        n = len(ids)
        target = ids.index(game.gameid)
        delta = (target - win.carrousel_index) % n
        if delta > n // 2:
            delta -= n
        if delta == 0:
            return
        if abs(delta) <= 3:
            win.carrousel_move(delta)
        else:
            win.carrousel_index = target
            win.render_carrousel()
            play_ui_sound("navigate")

    def _on_tile_click(self, child, n_press):
        if n_press == 2:
            self.select_game(child.game)
            self.win.play_or_notify(child.game)
            return
        self.select_game(child.game)
        self.scroll_to_top(focus_carrousel=True)

    def _on_tile_activated(self, _flowbox, child):
        self.select_game(child.game)
        self.scroll_to_top(focus_carrousel=True)

    def _on_tile_menu(self, child, x, y):
        self.select_game(child.game)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.win.open_context_menu(child.game, child, rect)
        self.win.context_menu.popup()

    def _on_grid_key(self, _controller, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            self.scroll_to_top(focus_carrousel=True)
            return True
        if keyval == Gdk.KEY_Up:
            focused = self.win.get_focus()
            first_row_y = None
            for child in self._tiles.values():
                ok, bounds = child.compute_bounds(self.grid)
                if ok:
                    y = bounds.origin.y
                    first_row_y = y if first_row_y is None else min(first_row_y, y)
            if isinstance(focused, Gtk.FlowBoxChild) and first_row_y is not None:
                ok, bounds = focused.compute_bounds(self.grid)
                if ok and bounds.origin.y <= first_row_y + 1:
                    self.scroll_to_top(focus_carrousel=True)
                    return True
        return False

    # ------------------------------------------------------------------ scrolling

    def _animate_scroll(self, target, on_done=None):
        if self._scroll_anim is not None:
            self._scroll_anim.pause()
        upper = self.vadj.get_upper() - self.vadj.get_page_size()
        target = max(0.0, min(target, upper))
        start = self.vadj.get_value()
        if abs(target - start) < 1:
            if on_done:
                on_done()
            return
        anim_target = Adw.CallbackAnimationTarget.new(self.vadj.set_value)
        anim = Adw.TimedAnimation.new(self.scroller, start, target, SCROLL_ANIM_MS, anim_target)
        anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        if on_done:
            anim.connect("done", lambda *_: on_done())
        self._scroll_anim = anim
        anim.play()

    def scroll_to_library(self):
        ok, bounds = self.library_box.compute_bounds(self.scroller.get_child())
        target = bounds.origin.y + 60 if ok else self.vadj.get_page_size()

        def focus_first():
            current = self._tiles.get(self._hero_gameid)
            child = current or self.grid.get_child_at_index(0)
            if child is not None:
                child.grab_focus()

        self._animate_scroll(target, focus_first)

    def scroll_to_top(self, focus_carrousel=False):
        def done():
            if focus_carrousel and getattr(self.win, "carrousel_fixed", None) is not None:
                self.win.carrousel_fixed.grab_focus()
                self.win.on_carrousel_focus_changed(self.win.carrousel_fixed, None)

        self._animate_scroll(0, done)

    # ------------------------------------------------------------------ fullscreen

    def toggle_fullscreen(self):
        win = self.win
        if win.is_fullscreen():
            win.fullscreen_activated = False
            win.unfullscreen()
        else:
            win.fullscreen_activated = True
            win.fullscreen()
        play_ui_sound("confirm")

    def _sync_fullscreen_button(self):
        is_full = self.win.is_fullscreen()
        self.win.fullscreen_activated = is_full
        self.fullscreen_button.set_icon_name(
            "view-restore-symbolic" if is_full else "view-fullscreen-symbolic"
        )
        self.fullscreen_button.set_tooltip_text(
            _("Exit fullscreen (F11)") if is_full else _("Fullscreen (F11)")
        )

    def _on_window_key(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        return False

    # ------------------------------------------------------------------ misc

    def _tick_clock(self):
        self.clock.set_text(datetime.now().strftime("%H:%M"))
        return True


def build_showcase(win, carrousel_box):
    """Return the Showcase root widget, creating it once per window."""
    showcase = getattr(win, "showcase", None)
    if showcase is None:
        showcase = Showcase(win)
        win.showcase = showcase
    root = showcase.attach_carrousel(carrousel_box)
    showcase.refresh_library()
    showcase.refresh_hero(force=True)
    return root
