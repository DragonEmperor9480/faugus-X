"""Faugus X: a "Logo" slot in the game edit window.

Sits under the banner preview, like Faugus' own cover/banner slots:
- click: pick a logo from SteamGridDB (searchable, like the banner picker)
- right-click: load from file, use the text title instead, or go back to
  the automatic logo

The choice is staged and only applied when the edit window is saved.
Installed from Showcase by wrapping AddGame.__init__, so Faugus' own files
stay untouched.
"""

import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from faugus.logos import (
    _api_key,
    cached_logo,
    forget_logo,
    install_logo,
    logo_disabled,
    resolve_sgdb_game_id,
    search_logos,
    set_logo_disabled,
)
from faugus.utils import (
    HiDpiPaintable,
    add_css_once,
    add_image_file_filters,
    apply_titlebar_preference,
    destroy_and_release,
    hide_dialog_action_area,
    is_valid_image,
    new_file_chooser,
    new_picture,
    run_in_background,
    set_file_chooser_start_folder,
    set_spinner_loading,
    show_invalid_image_dialog,
    verified_content,
    wrap_with_spinner,
)

SLOT_H = 84
PREVIEW_MAX_W, PREVIEW_MAX_H = 320, 64
THUMB_W, THUMB_H = 200, 90
MAX_RESULTS = 40

CSS = """
.faugus-x-logo-slot {
    background-color: #101218;
    background-image: linear-gradient(to bottom, #1a1d26, #0c0e13);
    border-radius: 8px;
    padding: 6px;
}
.faugus-x-logo-slot-hint {
    color: alpha(white, 0.55);
    font-size: 0.85em;
}
.faugus-x-logo-thumb {
    background-color: #12141a;
    border-radius: 8px;
    padding: 8px;
}
.faugus-x-logo-thumb:hover {
    background-color: #1f2330;
}
"""

# Staged choices: ("file", path) | ("none",) | ("auto",)
_KIND_FILE, _KIND_NONE, _KIND_AUTO = "file", "none", "auto"


def install(add_game_cls, on_logo_changed):
    """Wrap AddGame.__init__ so edit windows get a Logo slot."""
    if getattr(add_game_cls, "_faugus_x_logo_slot", False):
        return
    original_init = add_game_cls.__init__

    def __init__(self, parent, interface_mode, *args, **kwargs):
        original_init(self, parent, interface_mode, *args, **kwargs)
        game = getattr(parent, "_faugus_x_editing_game", None)
        parent._faugus_x_editing_game = None
        banner = getattr(self, "banner_preview1_overlay", None)
        if game is None or banner is None or banner.get_parent() is None:
            return
        try:
            _LogoSlot(self, game, on_logo_changed)
        except Exception as error:  # never break the edit window
            print(f"Faugus X: logo slot unavailable: {error}")

    add_game_cls.__init__ = __init__
    add_game_cls._faugus_x_logo_slot = True


class _LogoSlot:
    def __init__(self, dialog, game, on_logo_changed):
        add_css_once("faugus_x_logo_editor", CSS, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self.dialog = dialog
        self.game = game
        self.on_logo_changed = on_logo_changed
        self.staged = None
        self.tmpdir = tempfile.mkdtemp(prefix="faugus-x-logo-")
        self.closed = False

        self.picture = new_picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.title_label = Gtk.Label()
        self.title_label.add_css_class("title-4")
        self.title_label.set_ellipsize(3)
        # A Gtk.Fixed measures the picture at its own size; a picture given the
        # full dialog width would ask for a matching (huge) height.
        holder = Gtk.Fixed(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        holder.put(self.picture, 0, 0)
        self.stack = Gtk.Stack()
        self.stack.add_named(holder, "logo")
        self.stack.add_named(self.title_label, "text")

        hint = Gtk.Label(label=_("Logo"), xalign=0)
        hint.add_css_class("faugus-x-logo-slot-hint")
        hint.set_halign(Gtk.Align.START)
        hint.set_valign(Gtk.Align.START)

        inner = Gtk.Overlay(child=self.stack)
        inner.add_overlay(hint)

        self.button = Gtk.Button(child=inner)
        self.button.add_css_class("faugus-x-logo-slot")
        self.button.add_css_class("flat")
        self.button.set_size_request(-1, SLOT_H)
        self.button.set_tooltip_text(_("Click to choose a logo, right-click for more options"))
        self.button.connect("clicked", lambda _b: self.open_picker())

        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("released", self._on_right_click)
        self.button.add_controller(right)

        self.overlay, self.spinner = wrap_with_spinner(self.button)
        self.overlay.set_margin_top(8)
        for widget in (self.overlay, self.button):
            widget.set_vexpand(False)
            widget.set_valign(Gtk.Align.START)
        self.button.set_size_request(-1, SLOT_H)

        banner = dialog.banner_preview1_overlay
        parent = banner.get_parent()
        if isinstance(parent, Gtk.Box):
            parent.insert_child_after(self.overlay, banner)
        else:
            raise RuntimeError("unexpected banner container")

        self._build_menu()
        self._refresh_preview()
        # Faugus sets closed_event and destroys the window on a successful
        # Save (or Cancel) and keeps it open when validation fails. Its cleanup
        # also drops every signal handler, so instead of "destroy" we note the
        # button here and check closed_event once Faugus' handler has run.
        self.closed_event = getattr(dialog, "closed_event", None)
        dialog.connect("response", self._on_response)

    # ------------------------------------------------------------------ preview

    def _refresh_preview(self):
        path = None
        if self.staged is None:
            path = cached_logo(self.game.gameid)
        elif self.staged[0] == _KIND_FILE:
            path = self.staged[1]
        self.title_label.set_text(self.dialog.entry_title.get_text() or self.game.title)
        if self.staged is not None and self.staged[0] == _KIND_AUTO:
            self.title_label.set_text(_("Automatic (SteamGridDB)"))
            self.stack.set_visible_child_name("text")
            return
        if not path:
            self.stack.set_visible_child_name("text")
            return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, PREVIEW_MAX_W * 2, PREVIEW_MAX_H * 2, True)
        except GLib.Error:
            self.stack.set_visible_child_name("text")
            return
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        scale = min(PREVIEW_MAX_W / texture.get_width(), PREVIEW_MAX_H / texture.get_height())
        w, h = max(1, int(texture.get_width() * scale)), max(1, int(texture.get_height() * scale))
        self.picture.set_paintable(HiDpiPaintable(texture, w, h))
        self.picture.set_size_request(w, h)
        self.stack.set_visible_child_name("logo")

    def _set_loading(self, loading):
        if not self.closed:
            set_spinner_loading((self.spinner,), loading)
        return False

    # ------------------------------------------------------------------ menu

    def _build_menu(self):
        self.menu = Gtk.Popover()
        self.menu.set_has_arrow(False)
        self.menu.set_parent(self.button)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for margin in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{margin}")(6)
        self.menu.set_child(box)

        def item(label, callback):
            button = Gtk.Button(label=label)
            button.set_has_frame(False)
            button.get_child().set_halign(Gtk.Align.START)
            button.connect("clicked", lambda _b: (self.menu.popdown(), callback()))
            box.append(button)

        item(_("Choose from SteamGridDB"), self.open_picker)
        item(_("Load from file"), self._load_file)
        item(_("Use game title instead"), lambda: self._stage((_KIND_NONE,)))
        item(_("Automatic"), lambda: self._stage((_KIND_AUTO,)))

    def _on_right_click(self, _gesture, _n, x, y):
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.menu.set_pointing_to(rect)
        self.menu.popup()

    def _stage(self, choice):
        self.staged = choice
        self._refresh_preview()

    def _load_file(self):
        chooser = new_file_chooser(self.dialog, _("Select an image for the logo"), Gtk.FileChooserAction.OPEN)
        set_file_chooser_start_folder(chooser, "artwork_logo")
        add_image_file_filters(chooser, include_ico=False)

        def on_response(fc, response):
            if response == Gtk.ResponseType.ACCEPT:
                path = fc.get_file().get_path() if fc.get_file() else None
                if not path or not is_valid_image(path):
                    show_invalid_image_dialog()
                else:
                    staged = os.path.join(self.tmpdir, "file-" + os.path.basename(path))
                    shutil.copyfile(path, staged)
                    self._stage((_KIND_FILE, staged))
            destroy_and_release(fc)

        chooser.connect("response", on_response)
        chooser.present()

    # ------------------------------------------------------------------ picker

    def open_picker(self):
        api_key = _api_key()
        if not api_key:
            from faugus.utils import show_message_dialog
            show_message_dialog(
                _("SteamGridDB is not enabled."),
                _("Enable SteamGridDB and add your API key in Settings first."),
                parent=self.dialog,
            )
            return
        _LogoPicker(self, api_key)

    def picked_url(self, url):
        """Download a chosen SteamGridDB logo in the background and stage it."""
        self._set_loading(True)
        target = os.path.join(self.tmpdir, f"sgdb-{abs(hash(url))}.png")

        def work():
            from faugus.core import get_steamgriddb_session
            ok = False
            try:
                content = verified_content(get_steamgriddb_session().get(url, timeout=20))
                with open(target, "wb") as f:
                    f.write(content)
                ok = True
            except Exception as error:
                print(f"Faugus X: logo download failed: {error}")

            def done():
                self._set_loading(False)
                if ok and not self.closed:
                    self._stage((_KIND_FILE, target))
                return False

            GLib.idle_add(done)

        run_in_background(work)

    # ------------------------------------------------------------------ apply

    def _on_response(self, _dialog, response_id):
        def after_faugus():
            if self.closed_event is not None and not self.closed_event.is_set():
                return False  # validation failed: window stays open
            self.closed = True
            try:
                if response_id == Gtk.ResponseType.OK and self.staged is not None:
                    self._apply()
            finally:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
            return False

        GLib.idle_add(after_faugus)

    def _apply(self):
        game, kind = self.game, self.staged[0]
        try:
            if kind == _KIND_FILE:
                install_logo(game.gameid, self.staged[1])
            elif kind == _KIND_NONE:
                set_logo_disabled(game.gameid)
            elif kind == _KIND_AUTO:
                forget_logo(game.gameid)
        except Exception as error:
            print(f"Faugus X: could not apply logo: {error}")
            return
        callback = self.on_logo_changed
        GLib.idle_add(lambda: (callback(game), False)[1])


class _LogoPicker:
    """Grid of SteamGridDB logos for a search term, shown on a dark background."""

    def __init__(self, slot, api_key):
        self.slot = slot
        self.api_key = api_key
        self.closed = False
        self.token = 0

        dialog = Gtk.Dialog(title=_("Choose a logo"), transient_for=slot.dialog)
        apply_titlebar_preference(dialog)
        hide_dialog_action_area(dialog)
        dialog.set_modal(True)
        dialog.set_default_size(760, 520)
        dialog.connect("close-request", self._on_close)
        self.dialog = dialog

        self.search = Gtk.SearchEntry()
        self.search.set_text(slot.dialog.entry_title.get_text().strip() or slot.game.title)
        for margin in ("start", "end", "top"):
            getattr(self.search, f"set_margin_{margin}")(10)
        self.search.connect("activate", lambda e: self._start(e.get_text().strip(), None))

        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_halign(Gtk.Align.CENTER)
        self.flow.set_min_children_per_line(2)
        self.flow.set_max_children_per_line(6)
        self.flow.set_row_spacing(8)
        self.flow.set_column_spacing(8)
        for margin in ("start", "end", "top", "bottom"):
            getattr(self.flow, f"set_margin_{margin}")(10)
        self.flow.connect("child-activated", lambda _f, child: self._pick(child.url))

        scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=self.flow)
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        self.message = Gtk.Label(vexpand=True)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(spinner, "loading")
        self.stack.add_named(scrolled, "content")
        self.stack.add_named(self.message, "message")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.search)
        box.append(self.stack)
        dialog.get_content_area().append(box)
        dialog.present()

        sgdb_id = getattr(slot.dialog, "_steamgriddb_suggestion_id", None) or getattr(slot.game, "steamgriddb_id", "")
        self._start(self.search.get_text().strip(), sgdb_id)

    def _on_close(self, *_args):
        self.closed = True
        return False

    def _start(self, term, sgdb_id):
        if not term or self.closed:
            return
        self.token += 1
        token = self.token
        self.stack.set_visible_child_name("loading")
        run_in_background(self._fetch, term, sgdb_id, token)

    def _fetch(self, term, sgdb_id, token):
        from faugus.core import get_steamgriddb_session
        session = get_steamgriddb_session()
        headers = {"Authorization": f"Bearer {self.api_key}"}
        results, error = [], None
        try:
            game_id = resolve_sgdb_game_id(session, headers, term, sgdb_id or "")
            logos = search_logos(session, headers, game_id)[:MAX_RESULTS] if game_id else []

            def thumb(item):
                try:
                    data = verified_content(session.get(item.get("thumb") or item["url"], timeout=15))
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(data)
                    loader.close()
                    return item, loader.get_pixbuf()
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = [r for r in pool.map(thumb, logos) if r]
        except Exception as exc:
            error = str(exc)
        GLib.idle_add(self._populate, results, error, token)

    def _populate(self, results, error, token):
        if self.closed or token != self.token:
            return False
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        if error or not results:
            self.message.set_text(_("Could not reach SteamGridDB.") if error else _("No logos found. Try another name."))
            self.stack.set_visible_child_name("message")
            return False
        for item, pixbuf in results:
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            scale = min(THUMB_W / texture.get_width(), THUMB_H / texture.get_height())
            w, h = max(1, int(texture.get_width() * scale)), max(1, int(texture.get_height() * scale))
            picture = new_picture(HiDpiPaintable(texture, w, h))
            picture.set_halign(Gtk.Align.CENTER)
            picture.set_valign(Gtk.Align.CENTER)
            frame = Gtk.Box()
            frame.add_css_class("faugus-x-logo-thumb")
            frame.set_size_request(THUMB_W + 16, THUMB_H + 16)
            frame.append(picture)
            picture.set_hexpand(True)
            fb_child = Gtk.FlowBoxChild(child=frame)
            fb_child.url = item["url"]
            fb_child.gamepad_activate = lambda u=item["url"]: self._pick(u)
            fb_child.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            tip = " · ".join(x for x in (item.get("style"), f"{item.get('width')}×{item.get('height')}") if x)
            fb_child.set_tooltip_text(tip)
            self.flow.append(fb_child)
        self.stack.set_visible_child_name("content")
        return False

    def _pick(self, url):
        self.slot.picked_url(url)
        self.closed = True
        destroy_and_release(self.dialog)
