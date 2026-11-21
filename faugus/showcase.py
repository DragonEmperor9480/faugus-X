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

import math
import os
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango

from faugus.backdrop import Backdrop
from faugus.hd_banners import request_hd
from faugus.logos import cached_logo, load_logo_texture, logo_texture, request_logo
from faugus.utils import HiDpiPaintable, add_css_once, load_full_texture, new_icon_image, new_picture, play_ui_sound

# `_` (gettext) is installed into builtins by launcher.py's setup_gettext().

HERO_SLIDE_PX = 48
HERO_ANIM_MS = 420
ART_SETTLE_MS = 140  # wait this long after the last move before swapping art
SCROLL_ANIM_MS = 520
TILE_W, TILE_H = 188, 282
LOGO_MAX_W, LOGO_MAX_H = 540, 150
CAPTION_LOGO_MAX_W, CAPTION_LOGO_MAX_H = 420, 130
IDLE_SECONDS = int(os.environ.get("FAUGUS_X_IDLE_SECONDS", "45"))  # env override for testing
ATTRACT_SLIDE_SECONDS = 12
ATTRACT_FADE_OUT_MS = 1500
ATTRACT_FADE_IN_MS = 350

SHOWCASE_CSS = """
.showcase-attract-hint {
    color: alpha(white, 0.55);
    font-size: 14px;
    letter-spacing: 2px;
    text-shadow: 0 1px 4px alpha(black, 0.8);
}
.showcase-attract-title {
    color: white;
    font-size: 36px;
    font-weight: 800;
    text-shadow: 0 2px 8px alpha(black, 0.8);
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
entry.showcase-search {
    border-radius: 20px;
    border: none;
    background: alpha(white, 0.16);
    color: white;
    padding: 0 14px;
}
entry.showcase-search:focus-within {
    background: alpha(white, 0.24);
    box-shadow: 0 0 0 2px alpha(@accent_bg_color, 0.9);
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
/* Faugus gives every FlowBoxChild the CSS name "entry", so library cells
   would be drawn as grey text fields: strip that, the cover is the tile. */
flowbox.showcase-grid > .showcase-cell,
flowbox.showcase-grid > .showcase-cell:focus,
flowbox.showcase-grid > .showcase-cell:focus-within,
flowbox.showcase-grid > .showcase-cell:hover,
flowbox.showcase-grid > .showcase-cell:selected {
    min-height: 0;
    padding: 0;
    margin: 0;
    border: none;
    border-radius: 0;
    background: none;
    box-shadow: none;
    outline: none;
}
.showcase-tile {
    border-radius: 12px;
    box-shadow: 0 8px 20px alpha(black, 0.5);
    transition: transform 200ms ease, box-shadow 200ms ease;
}
.showcase-cell:hover .showcase-tile {
    transform: translateY(-6px) scale(1.03);
    box-shadow: 0 16px 30px alpha(black, 0.6);
}
.showcase-cell:focus-visible .showcase-tile,
.showcase-cell.showcase-current .showcase-tile {
    transform: translateY(-6px) scale(1.03);
    box-shadow: 0 16px 30px alpha(black, 0.6), 0 0 0 3px @accent_bg_color, 0 0 26px alpha(@accent_bg_color, 0.55);
}
.showcase-tile-label {
    color: alpha(white, 0.6);
    font-size: 14px;
    font-weight: 600;
    transition: color 200ms ease;
}
.showcase-cell:hover .showcase-tile-label,
.showcase-cell:focus-visible .showcase-tile-label,
.showcase-cell.showcase-current .showcase-tile-label {
    color: white;
}
"""


KWIN_ACTIVATE_JS = """
const list = workspace.stackingOrder;
for (let i = list.length - 1; i >= 0; i--) {
    const w = list[i];
    if (w.pid === %d && w.normalWindow) {
        w.minimized = false;
        workspace.activeWindow = w;
        break;
    }
}
"""


def _kwin_bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _kwin_activate_own_window():
    """Raise and focus this process' window through KWin scripting (KDE Plasma only)."""
    import tempfile
    try:
        bus = _kwin_bus()
        owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", ("org.kde.KWin",)),
                              GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
        if not owner.unpack()[0]:
            return
        name = f"faugus-x-activate-{os.getpid()}"
        with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="faugus-x-", delete=False,
                                         dir=GLib.get_user_runtime_dir()) as script:
            script.write(KWIN_ACTIVATE_JS % os.getpid())
        try:
            call = lambda path, iface, method, args=None, reply=None: bus.call_sync(
                "org.kde.KWin", path, iface, method, args, reply, Gio.DBusCallFlags.NONE, 2000, None)
            call("/Scripting", "org.kde.kwin.Scripting", "unloadScript", GLib.Variant("(s)", (name,)))
            script_id = call("/Scripting", "org.kde.kwin.Scripting", "loadScript",
                             GLib.Variant("(ss)", (script.name, name)), GLib.VariantType.new("(i)")).unpack()[0]
            if script_id >= 0:
                call(f"/Scripting/Script{script_id}", "org.kde.kwin.Script", "run")
            call("/Scripting", "org.kde.kwin.Scripting", "unloadScript", GLib.Variant("(s)", (name,)))
        finally:
            os.unlink(script.name)
    except (GLib.Error, OSError) as error:
        print(f"Faugus X: could not raise the window through KWin: {error}", flush=True)


HERO_BUTTONS_KEY = "showcase-hero-buttons"


def hero_buttons_enabled():
    from faugus.config_manager import ConfigManager
    return ConfigManager().config.get(HERO_BUTTONS_KEY, "True") != "False"


def _fixed_holder(picture):
    """Hold a picture at its exact size.

    A Gtk.Picture answers height-for-width by keeping its aspect ratio, so a
    vertical box measuring it at full screen width gets a huge height and the
    hero layout overflows. Gtk.Fixed measures children without a for-size.
    """
    holder = Gtk.Fixed()
    holder.set_halign(Gtk.Align.START)
    holder.put(picture, 0, 0)
    return holder


class Showcase:
    def __init__(self, win):
        self.win = win
        self._hero_gameid = None
        self._hero_anim = None
        self._art_source = None
        self._scroll_anim = None
        self._library_key = None
        self._tiles = {}
        self.attracting = False
        self._last_activity = time.monotonic()
        self._last_pointer = None
        self._attract_slide_id = None
        self._attract_anim = None
        self._attract_index = 0

        add_css_once("showcase", SHOWCASE_CSS, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self.backdrop = Backdrop()
        self.backdrop.pause_when = self._foreground_animating
        self._build()
        self._install_hooks()
        self._setup_attract()
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
        self.vadj.connect("value-changed", lambda *_: self._on_scrolled())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.scroller.set_child(content)

        # Root: animated banner backdrop, the scrolling UI on top, and the
        # attract-mode caption above everything.
        self.root = Gtk.Overlay()
        self.root.set_child(self.backdrop.widget)
        self.root.add_overlay(self.scroller)
        self.root.add_overlay(self._build_attract_caption())

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

        top_end = Gtk.Box(spacing=12)
        top_end.append(self._build_toolbar())
        self.clock.set_margin_start(10)
        self.clock.set_margin_end(4)
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
        self.hero_info.add_css_class("showcase-hero-info")
        self._hero_css = Gtk.CssProvider()
        self.hero_info.get_style_context().add_provider(self._hero_css, Gtk.STYLE_PROVIDER_PRIORITY_USER + 2)

        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("showcase-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_max_width_chars(40)

        self.logo = Gtk.Picture()
        self.logo.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.logo.set_can_shrink(True)
        self.logo.set_halign(Gtk.Align.START)

        # The game's SteamGridDB logo when we have one, its text title otherwise.
        self.title_stack = Gtk.Stack()
        self.title_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.title_stack.set_transition_duration(300)
        self.title_stack.set_hhomogeneous(False)
        self.title_stack.set_vhomogeneous(False)
        self.title_stack.set_margin_bottom(6)
        self.title_stack.set_halign(Gtk.Align.START)
        self.title_stack.add_named(self.title, "text")
        self.title_stack.add_named(_fixed_holder(self.logo), "logo")
        self.hero_info.append(self.title_stack)

        self.meta = Gtk.Label(xalign=0)
        self.meta.add_css_class("showcase-meta")
        self.hero_info.append(self.meta)

        actions = Gtk.Box(spacing=14)
        actions.set_margin_top(12)
        self.hero_actions = actions
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
        self.set_hero_buttons(hero_buttons_enabled())

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

        # Clicks live on the FlowBox itself (like Faugus' own grid): the FlowBox
        # claims pointer presses, which would cancel gestures on its children.
        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_grid_click)
        self.grid.add_controller(click)

        right = Gtk.GestureClick()
        right.set_button(Gdk.BUTTON_SECONDARY)
        right.connect("pressed", self._on_grid_right_click)
        self.grid.add_controller(right)

        grid_keys = Gtk.EventControllerKey()
        grid_keys.connect("key-pressed", self._on_grid_key)
        self.grid.add_controller(grid_keys)
        library.append(self.grid)

        content.append(library)

    def _build_toolbar(self):
        """Console-style top-right controls replacing Faugus' bottom bar."""
        win = self.win
        bar = Gtk.Box(spacing=10)
        bar.set_valign(Gtk.Align.CENTER)

        # Search: an icon that slides open into Faugus' own search entry.
        self.search_revealer = Gtk.Revealer()
        self.search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.search_revealer.set_transition_duration(250)
        self.search_slot = Gtk.Box()
        self.search_revealer.set_child(self.search_slot)
        bar.append(self.search_revealer)

        def tool(icon, tooltip, callback, themed=False):
            button = Gtk.Button()
            button.add_css_class("showcase-round")
            button.add_css_class("showcase-small")
            if themed:
                button.set_icon_name(icon)
            else:
                button.set_child(new_icon_image(f"{icon}.svg"))
            button.set_tooltip_text(tooltip)
            button.connect("clicked", callback)
            bar.append(button)
            return button

        self.search_button = tool("system-search-symbolic", _("Search (Ctrl+F)"),
                                  lambda *_: self.toggle_search(), themed=True)
        tool("faugus-add-symbolic", _("Add game"), lambda b: win.on_button_add_clicked(b))
        tool("faugus-settings-symbolic", _("Settings"), lambda b: win.on_button_settings_clicked(b))
        tool("faugus-kill-symbolic", _("Kill all running games"), lambda b: win.on_button_kill_clicked(b))
        self.power_menu = None
        self.power_button = tool("system-shutdown-symbolic", _("Power"), self._on_power, themed=True)
        return bar

    def _on_power(self, button):
        if self.power_menu is None:
            from faugus.power_menu import PowerMenu
            self.power_menu = PowerMenu(self.win, button)
        self.power_menu.popup()

    def _adopt_search_entry(self):
        entry = getattr(self.win, "entry_search", None)
        if entry is None or entry.get_parent() is self.search_slot:
            return
        parent = entry.get_parent()
        if parent is not None:
            parent.remove(entry)
        entry.set_hexpand(False)
        entry.set_size_request(260, 40)
        entry.add_css_class("showcase-search")
        self.search_slot.append(entry)

    def toggle_search(self, open_=None):
        entry = self.win.entry_search
        if open_ is None:
            open_ = not self.search_revealer.get_reveal_child()
        if open_:
            self.search_revealer.set_reveal_child(True)
            GLib.idle_add(lambda: entry.grab_focus() and False)
        else:
            if entry.get_text():
                self.win.leave_search()
            self.search_revealer.set_reveal_child(False)
            fixed = getattr(self.win, "carrousel_fixed", None)
            if fixed is not None:
                fixed.grab_focus()

    def attach_carrousel(self, carrousel_box):
        self._adopt_search_entry()
        wrapper = getattr(self.win, "bottom_bar_wrapper", None)
        if wrapper is not None:
            wrapper.set_visible(False)

        parent = carrousel_box.get_parent()
        if parent is not None and parent is not self.carrousel_slot:
            parent.remove(carrousel_box)
        if carrousel_box.get_parent() is None:
            self.carrousel_slot.append(carrousel_box)
        carrousel_box.set_valign(Gtk.Align.END)

        parent = self.root.get_parent()
        if parent is not None:
            parent.remove(self.root)

        return self.root

    def set_hero_buttons(self, visible):
        """Show or hide the hero's Play and ⋯ buttons (the carousel does both anyway)."""
        self.hero_actions.set_visible(visible)

    def _fit_hero(self):
        page = int(self.vadj.get_page_size())
        if page > 0:
            self.hero_page.set_size_request(-1, page)

    # ------------------------------------------------------------------ hooks

    def _install_hooks(self):
        """Wrap a few window methods on this instance only (no class patching)."""
        win = self.win

        # Logo slot in the game edit window (Faugus X file, no official changes).
        # Faugus usually runs as `python -m faugus.launcher`, so its classes live
        # in __main__, not faugus.launcher: take AddGame from the namespace the
        # window's own code uses.
        from faugus.logo_editor import install as install_logo_editor
        add_game_cls = type(win).on_button_edit_clicked.__globals__.get("AddGame")
        if add_game_cls is not None:
            install_logo_editor(add_game_cls, self.on_logo_edited)
        original_edit = win.on_button_edit_clicked

        def on_button_edit_clicked(*args, **kwargs):
            win._faugus_x_editing_game = win.selected()
            try:
                return original_edit(*args, **kwargs)
            finally:
                win._faugus_x_editing_game = None

        win.on_button_edit_clicked = on_button_edit_clicked

        # "Download missing artwork" in Settings (Faugus X file, no official changes).
        from faugus.artwork_refresh import install as install_artwork_refresh
        settings_cls = type(win).on_button_edit_clicked.__globals__.get("Settings")
        if settings_cls is not None:
            install_artwork_refresh(settings_cls, lambda: list(win.games), self.on_artwork_refreshed)

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

        # Showcase draws its own full-screen backdrop; skip Faugus' banner work.
        win.update_background = lambda *args, **kwargs: None

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

        if stopped and not running:
            GLib.timeout_add(300, self.return_home, next(iter(stopped)))

        if started:
            gameid = next(iter(started))
            game = next((g for g in self.win.games if g.gameid == gameid), None)
            if game is not None:
                self.launch_screen.show(game)

    def return_home(self, gameid=None):
        """A game just closed: bring Faugus X back, on the game you played, like a console."""
        win = self.win
        if win.running:
            return False
        self.note_activity()
        game = next((g for g in win.games if g.gameid == gameid), None)
        if game is not None:
            self.select_game(game)
        if self.vadj.get_value() > 0:
            self.scroll_to_top(focus_carrousel=True)
        elif getattr(win, "carrousel_fixed", None) is not None:
            win.carrousel_fixed.grab_focus()
        if getattr(win, "fullscreen_activated", False) and not win.is_fullscreen():
            win.fullscreen()
        win.present()
        # Wayland compositors may ignore present() from an app that isn't focused
        # (focus-stealing prevention). On KDE Plasma, ask KWin directly.
        _kwin_activate_own_window()
        return False

    # ------------------------------------------------------------------ hero

    def refresh_hero(self, force=False):
        game = self.win.selected()
        if game is None:
            self.hero_info.set_visible(False)
            self._hero_gameid = None
            self.backdrop.show_game(None)
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
        self._show_title_or_logo(game)
        self._animate_hero_in()
        self._schedule_art()

    # ------------------------------------------------------------------ deferred art
    #
    # Swapping the backdrop (texture upload) or decoding a logo while the
    # carrousel slides makes the slide stutter. Text updates instantly; the
    # heavy art waits until the carrousel animation has settled.

    def _foreground_animating(self):
        hero = self._hero_anim
        return self._carrousel_sliding() or (hero is not None and hero.get_state() == Adw.AnimationState.PLAYING)

    def _carrousel_sliding(self):
        return getattr(self.win, "_carrousel_anim_id", None) is not None

    def _schedule_art(self, delay=ART_SETTLE_MS):
        if self._art_source is not None:
            GLib.source_remove(self._art_source)
        self._art_source = GLib.timeout_add(delay, self._apply_art)

    def _apply_art(self):
        if self._carrousel_sliding():
            self._art_source = GLib.timeout_add(60, self._apply_art)
            return False
        self._art_source = None

        game = self.win.selected()
        if game is None:
            return False
        if not self.attracting:
            self.backdrop.show_game(game)
            self.backdrop.prefetch(self._neighbours(game))
        if logo_texture(game.gameid) is None:
            self._fetch_logo(game, lambda texture, gid=game.gameid: self._on_logo_ready(gid, texture))
        return False

    def _neighbours(self, game, radius=2):
        games = self.win.carrousel_visible_games()
        ids = [g.gameid for g in games]
        if game.gameid not in ids or not games:
            return []
        i, n = ids.index(game.gameid), len(games)
        return [games[(i + d) % n] for d in range(-radius, radius + 1) if d]

    # ------------------------------------------------------------------ logos

    @staticmethod
    def _fit(texture, max_w, max_h):
        w, h = texture.get_width(), texture.get_height()
        if not w or not h:
            return 0, 0
        scale = min(max_w / w, max_h / h)
        return int(w * scale), int(h * scale)

    @staticmethod
    def _fetch_logo(game, on_texture):
        """Decode (or first download) a logo off the UI thread."""
        if cached_logo(game.gameid):
            load_logo_texture(game.gameid, on_texture)
        else:
            request_logo(game, lambda _path, gid=game.gameid: load_logo_texture(gid, on_texture))

    def _show_title_or_logo(self, game):
        texture = logo_texture(game.gameid)  # in-memory only: never decode here
        if texture is None:
            self.title_stack.set_visible_child_name("text")
        else:
            self._apply_logo_texture(texture)

    def _apply_logo_texture(self, texture):
        w, h = self._fit(texture, LOGO_MAX_W, LOGO_MAX_H)
        if not w:
            self.title_stack.set_visible_child_name("text")
            return
        # HiDpiPaintable reports w x h as the natural size, so GTK doesn't
        # lay the logo out at its native resolution.
        self.logo.set_paintable(HiDpiPaintable(texture, w, h))
        self.logo.set_size_request(w, h)
        self.title_stack.set_visible_child_name("logo")

    def refresh_library_art(self):
        """A SteamGridDB key was just added: fetch logos and HD banners for everything."""
        self._library_key = None
        self.refresh_library()
        self.on_artwork_refreshed()

    def on_artwork_refreshed(self):
        """New banners/logos were downloaded: show them for the current game."""
        game = self.win.selected()
        if game is None:
            return
        self.backdrop._current_path = None
        if not self.attracting:
            self.backdrop.show_game(game)
        self._fetch_logo(game, lambda texture, gid=game.gameid: self._on_logo_ready(gid, texture))

    def on_logo_edited(self, game):
        """The user changed a logo in the edit window: show it right away."""
        if game.gameid != self._hero_gameid:
            return
        if cached_logo(game.gameid) is None:
            self.title_stack.set_visible_child_name("text")
        # Loads the new file, or (for "Automatic") downloads the default again.
        self._fetch_logo(game, lambda texture, gid=game.gameid: self._on_logo_ready(gid, texture))

    def _on_logo_ready(self, gameid, texture):
        if gameid == self._hero_gameid:
            self._apply_logo_texture(texture)

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

        # Slide with a CSS transform rather than a margin: a margin change
        # re-lays-out the whole hero page every frame, mid carrousel slide.
        def step(value):
            self.hero_info.set_opacity(value)
            offset = (1.0 - value) * HERO_SLIDE_PX
            self._hero_css.load_from_data(
                f".showcase-hero-info {{ transform: translateX({offset:.1f}px); }}".encode()
            )

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
            # Warm the logo cache (download and/or decode) in the background,
            # so the hero can show logos instantly while browsing.
            if logo_texture(game.gameid) is None:
                self._fetch_logo(game, lambda _texture: None)
            request_hd(game)  # full-resolution backdrop, downloaded once
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

        label = Gtk.Label(label=game.title, xalign=0)
        label.add_css_class("showcase-tile-label")
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(1)
        label.set_size_request(TILE_W, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        box.append(frame)
        box.append(label)

        child = Gtk.FlowBoxChild()
        child.add_css_class("showcase-cell")
        child.set_child(box)
        child.game = game
        child.set_tooltip_text(game.title)

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

    def _on_grid_click(self, _gesture, n_press, x, y):
        child = self.grid.get_child_at_pos(int(x), int(y))
        if child is not None and hasattr(child, "game"):
            self._on_tile_click(child, n_press)

    def _on_grid_right_click(self, gesture, _n_press, x, y):
        child = self.grid.get_child_at_pos(int(x), int(y))
        if child is None or not hasattr(child, "game"):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        ok, point = self.grid.compute_point(child, Graphene.Point().init(x, y))
        cx, cy = (point.x, point.y) if ok else (0, 0)
        self._on_tile_menu(child, cx, cy)

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

    def _on_window_key(self, _controller, keyval, _keycode, state):
        if self.note_activity():
            return True  # the key that wakes attract mode does nothing else
        if keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        if keyval == Gdk.KEY_f and state & Gdk.ModifierType.CONTROL_MASK:
            self.toggle_search(True)
            return True
        if keyval == Gdk.KEY_Escape and self.search_revealer.get_reveal_child():
            focus = self.win.get_focus()
            entry = self.win.entry_search
            if focus is not None and (focus is entry or focus.is_ancestor(entry)):
                self.toggle_search(False)
                return True
        return self._wasd(keyval, state)

    WASD = {
        Gdk.KEY_w: Gtk.DirectionType.UP,
        Gdk.KEY_a: Gtk.DirectionType.LEFT,
        Gdk.KEY_s: Gtk.DirectionType.DOWN,
        Gdk.KEY_d: Gtk.DirectionType.RIGHT,
    }

    def _wasd(self, keyval, state):
        """W A S D move like the arrow keys (and the D-pad), unless the user is typing."""
        direction = self.WASD.get(Gdk.keyval_to_lower(keyval))
        if direction is None:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        focus = self.win.get_focus()
        if isinstance(focus, Gtk.Editable) or (focus is not None and focus.get_ancestor(Gtk.Editable) is not None):
            return False
        from faugus.main_screen_nav import navigate_focus
        return navigate_focus(direction)

    # ------------------------------------------------------------------ attract mode

    def _build_attract_caption(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.END)
        box.set_margin_start(80)
        box.set_margin_bottom(70)
        box.set_can_target(False)
        box.set_opacity(0.0)

        self.caption_logo = Gtk.Picture()
        self.caption_logo.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.caption_logo.set_halign(Gtk.Align.START)
        self.caption_title = Gtk.Label(xalign=0)
        self.caption_title.add_css_class("showcase-attract-title")
        self.caption_stack = Gtk.Stack()
        self.caption_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.caption_stack.set_transition_duration(700)
        self.caption_stack.set_hhomogeneous(False)
        self.caption_stack.set_vhomogeneous(False)
        self.caption_stack.set_halign(Gtk.Align.START)
        self.caption_stack.add_named(self.caption_title, "text")
        self.caption_stack.add_named(_fixed_holder(self.caption_logo), "logo")
        box.append(self.caption_stack)

        hint = Gtk.Label(label=_("PRESS ANY BUTTON"), xalign=0)
        hint.add_css_class("showcase-attract-hint")
        box.append(hint)

        self.attract_caption = box
        return box

    def _setup_attract(self):
        win = self.win

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_pointer_motion)
        win.add_controller(motion)

        scroll = Gtk.EventControllerScroll()
        scroll.set_flags(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", lambda *_: self.note_activity())
        win.add_controller(scroll)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_any_click)
        win.add_controller(click)

        self._hook_gamepad_activity()
        GLib.timeout_add_seconds(2, self._check_idle)

    def _hook_gamepad_activity(self):
        try:
            import faugus.gamepad as gamepad
            from gi.repository import Manette
        except Exception:
            return
        original = getattr(gamepad, "_on_device_event", None)
        if original is None or getattr(original, "_faugus_x_wrapped", False):
            return

        def on_device_event(window, event):
            showcase = getattr(window, "showcase", None)
            if showcase is not None and showcase.note_gamepad_event(event, Manette):
                return None  # swallow the input that wakes attract mode
            return original(window, event)

        on_device_event._faugus_x_wrapped = True
        gamepad._on_device_event = on_device_event

    def note_gamepad_event(self, event, Manette):
        kind = event.get_event_type()
        if kind == Manette.EventType.EVENT_BUTTON_RELEASE:
            return False
        if kind == Manette.EventType.EVENT_ABSOLUTE:
            ok, _axis, value = event.get_absolute()
            if not ok or abs(value) < 0.5:
                return False  # ignore stick noise
        return self.note_activity()

    def note_activity(self):
        """Record user input. Returns True if this input woke attract mode."""
        self._last_activity = time.monotonic()
        if self.attracting:
            self.exit_attract()
            return True
        return False

    def _on_pointer_motion(self, _controller, x, y):
        last = self._last_pointer
        self._last_pointer = (x, y)
        if last is not None and math.hypot(x - last[0], y - last[1]) < 4:
            return  # synthetic or jitter motion
        if last is not None:
            self.note_activity()

    def _on_any_click(self, gesture, *_args):
        if self.note_activity():
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _attract_allowed(self):
        win = self.win
        if not win.is_active() or self.launch_screen.root.get_visible():
            return False
        if self.search_revealer.get_reveal_child():
            return False
        menu = getattr(win, "context_menu", None)
        if menu is not None and menu.get_visible():
            return False
        app = win.get_application()
        if app is not None and len([w for w in app.get_windows() if w.get_visible()]) > 1:
            return False  # a dialog (settings, edit game, ...) is open
        return True

    def _check_idle(self):
        if not self.attracting and time.monotonic() - self._last_activity >= IDLE_SECONDS:
            if self._attract_allowed():
                self.enter_attract()
            else:
                self._last_activity = time.monotonic()
        return True

    def _fade_ui(self, target, duration, done=None):
        if self._attract_anim is not None:
            self._attract_anim.pause()
        start = self.scroller.get_opacity()
        caption_start = self.attract_caption.get_opacity()
        caption_target = 1.0 - target

        def step(value):
            self.scroller.set_opacity(start + (target - start) * value)
            self.attract_caption.set_opacity(caption_start + (caption_target - caption_start) * value)

        anim = Adw.TimedAnimation.new(self.root, 0.0, 1.0, duration, Adw.CallbackAnimationTarget.new(step))
        anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        if done is not None:
            anim.connect("done", lambda *_: done())
        self._attract_anim = anim
        anim.play()

    def enter_attract(self):
        self.attracting = True
        self.scroller.set_can_target(False)
        self.win.set_cursor_from_name("none")

        games = [g for g in self.win.games if Backdrop.has_art(g)]
        selected = self.win.selected()
        ids = [g.gameid for g in games]
        self._attract_index = ids.index(selected.gameid) if selected and selected.gameid in ids else 0
        self._set_caption(games[self._attract_index] if games else selected)
        self._fade_ui(0.0, ATTRACT_FADE_OUT_MS)

        if len(games) > 1:
            def next_slide():
                if not self.attracting:
                    return False
                current = [g for g in self.win.games if Backdrop.has_art(g)]
                if not current:
                    return True
                self._attract_index = (self._attract_index + 1) % len(current)
                game = current[self._attract_index]
                self.backdrop.show_game(game)
                self._set_caption(game)
                return True

            self._attract_slide_id = GLib.timeout_add_seconds(ATTRACT_SLIDE_SECONDS, next_slide)

    def exit_attract(self):
        self.attracting = False
        if self._attract_slide_id is not None:
            GLib.source_remove(self._attract_slide_id)
            self._attract_slide_id = None
        self.win.set_cursor(None)
        self.scroller.set_can_target(True)
        game = self.win.selected()
        if game is not None:
            self.backdrop.show_game(game)
        self._fade_ui(1.0, ATTRACT_FADE_IN_MS)

    def _set_caption(self, game):
        if game is None:
            return
        self.caption_title.set_text(game.title)
        texture = logo_texture(game.gameid)
        w, h = self._fit(texture, CAPTION_LOGO_MAX_W, CAPTION_LOGO_MAX_H) if texture is not None else (0, 0)
        if w:
            self.caption_logo.set_paintable(HiDpiPaintable(texture, w, h))
            self.caption_logo.set_size_request(w, h)
            self.caption_stack.set_visible_child_name("logo")
        else:
            self.caption_stack.set_visible_child_name("text")

    def _on_scrolled(self):
        page = self.vadj.get_page_size() or 1
        self.backdrop.set_dim(0.6 * min(1.0, self.vadj.get_value() / page))

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
