"""Faugus X: holographic tilt for the focused carousel cover.

Part of the controller tilt effect. The cover in the middle of the carousel
leans slightly in 3D with the controller, and a soft band of light with a
faint rainbow sheen slides across it, like tilting a shiny trading card.

Faugus places each carousel card in a Gtk.Fixed (move()) and slides/scales
it with CSS. The 3D lean goes in the Fixed's child transform around the
card's visual centre, so Faugus' own animation is untouched; the light is an
extra overlay inside the card, clipped by its rounded corners. Nothing in
launcher.py changes: one window method is wrapped on the instance to learn
each card's base position.
"""

import math

from gi.repository import Gdk, Graphene, Gsk, Gtk

MAX_LEAN_Y = 7.0      # degrees around the vertical axis at full tilt
MAX_LEAN_X = 6.0      # degrees around the horizontal axis
PERSPECTIVE = 1100
SHINE_ALPHA = 0.34
RAINBOW_ALPHA = 0.08


def _rgba(spec):
    color = Gdk.RGBA()
    color.parse(spec)
    return color


def _stops(spec):
    stops = []
    for offset, color in spec:
        stop = Gsk.ColorStop()
        stop.offset = offset
        stop.color = _rgba(color)
        stops.append(stop)
    return stops


class _Sheen(Gtk.Widget):
    """The light band, drawn over the cover picture only."""

    __gtype_name__ = "FaugusXHoloSheen"

    def __init__(self, picture):
        super().__init__()
        self.set_can_target(False)
        self.set_focusable(False)
        self.picture = picture
        self._state = (0.0, 0.0, 0.0)

    def set_state(self, x, y, amount):
        state = (round(x, 3), round(y, 3), round(amount, 3))
        if state != self._state:
            self._state = state
            self.queue_draw()

    def do_snapshot(self, snapshot):
        x, y, amount = self._state
        if amount < 0.01:
            return
        ok, bounds = self.picture.compute_bounds(self)
        if not ok:
            return
        rx, ry = bounds.get_x(), bounds.get_y()
        w, h = bounds.get_width(), bounds.get_height()
        if w <= 1 or h <= 1:
            return
        rect = Graphene.Rect().init(rx, ry, w, h)
        diag = math.hypot(w, h)
        dx, dy = w / diag, h / diag                 # top-left -> bottom-right
        # Right side down slides the light right; top away slides it up.
        shift = max(-1.2, min(1.2, x - y)) * 0.25 * diag
        cx, cy = rx + w / 2 + dx * shift, ry + h / 2 + dy * shift

        def line(length, centre_shift=0.0):
            px, py = cx + dx * centre_shift, cy + dy * centre_shift
            half = length / 2
            return (Graphene.Point().init(px - dx * half, py - dy * half),
                    Graphene.Point().init(px + dx * half, py + dy * half))

        a = SHINE_ALPHA * amount
        start, end = line(diag * 0.9)
        snapshot.append_linear_gradient(rect, start, end, _stops((
            (0.0, "rgba(255,255,255,0)"),
            (0.3, f"rgba(255,255,255,{a * 0.2:.3f})"),
            (0.45, f"rgba(255,255,255,{a * 0.8:.3f})"),
            (0.5, f"rgba(255,255,255,{a:.3f})"),
            (0.55, f"rgba(255,255,255,{a * 0.8:.3f})"),
            (0.7, f"rgba(255,255,255,{a * 0.2:.3f})"),
            (1.0, "rgba(255,255,255,0)"),
        )))
        r = RAINBOW_ALPHA * amount
        start, end = line(diag * 0.8, centre_shift=shift * 1.6)
        snapshot.append_repeating_linear_gradient(rect, start, end, _stops((
            (0.0, f"rgba(255,80,200,{r:.3f})"),
            (0.25, f"rgba(80,200,255,{r:.3f})"),
            (0.5, f"rgba(120,255,160,{r:.3f})"),
            (0.75, f"rgba(255,230,90,{r:.3f})"),
            (1.0, f"rgba(255,80,200,{r:.3f})"),
        )))


class Holo:
    def __init__(self, win):
        self.win = win
        self.slots = list(getattr(win, "carrousel_slots", []))
        for slot in self.slots:
            self._attach(slot)
        self._wrap_placement()

    # ------------------------------------------------------------- setup

    def _attach(self, slot):
        card_overlay = slot["box"].get_first_child()
        if not isinstance(card_overlay, Gtk.Overlay) or "_faugus_x_sheen" in slot:
            return
        sheen = _Sheen(slot["picture"])
        card_overlay.add_overlay(sheen)
        slot["_faugus_x_sheen"] = sheen

    def _wrap_placement(self):
        win = self.win
        original = getattr(win, "place_carrousel_slot_base", None)
        if original is None or getattr(original, "_faugus_x_holo", False):
            return

        def place_carrousel_slot_base(slot, *args, **kwargs):
            result = original(slot, *args, **kwargs)
            slot.pop("_faugus_x_base", None)   # move() reset the transform; re-measured below
            slot.pop("_faugus_x_lean", None)
            return result

        place_carrousel_slot_base._faugus_x_holo = True
        win.place_carrousel_slot_base = place_carrousel_slot_base

    def _base(self, slot):
        base = slot.get("_faugus_x_base")
        if base is None:
            box = slot["box"]
            _, natural_w, _, _ = box.measure(Gtk.Orientation.HORIZONTAL, -1)
            _, natural_h, _, _ = box.measure(Gtk.Orientation.VERTICAL, natural_w)
            base = (self.win.carrousel_center_x - natural_w / 2,
                    self.win.carrousel_center_y - natural_h / 2)
            slot["_faugus_x_base"] = base
        return base

    # ------------------------------------------------------------- per frame

    def update(self, x, y):
        current = None
        best = 0.5
        for slot in self.slots:
            if getattr(slot["box"], "game", None) is None:
                continue
            offset = abs(slot.get("visual_offset", 99))
            if offset < best:
                best, current = offset, slot
        amount = min(1.0, math.hypot(x, y) * 1.4)
        for slot in self.slots:
            if slot is current and amount >= 0.01:
                self._lean(slot, x, y, amount)
            else:
                self._flat(slot)

    def reset(self):
        for slot in self.slots:
            self._flat(slot)

    def _lean(self, slot, x, y, amount):
        sheen = slot.get("_faugus_x_sheen")
        if sheen is not None:
            sheen.set_state(x, y, amount)
        lean = (round(x * MAX_LEAN_Y, 2), round(y * MAX_LEAN_X, 2), round(slot.get("visual_offset", 0), 3))
        if slot.get("_faugus_x_lean") == lean:
            return
        box = slot["box"]
        w, h = box.get_width(), box.get_height()
        if w <= 0 or h <= 0:
            return
        bx, by = self._base(slot)
        # Faugus slides the card with a CSS translate: pivot on where it is drawn.
        ox = w / 2 + lean[2] * getattr(self.win, "carrousel_step", 0)
        oy = h / 2
        transform = (Gsk.Transform.new()
                     .translate(Graphene.Point().init(bx + ox, by + oy))
                     .perspective(PERSPECTIVE)
                     .rotate_3d(-lean[0], Graphene.Vec3.y_axis())
                     .rotate_3d(-lean[1], Graphene.Vec3.x_axis())
                     .translate(Graphene.Point().init(-ox, -oy)))
        self.win.carrousel_fixed.set_child_transform(box, transform)
        slot["_faugus_x_lean"] = lean

    def _flat(self, slot):
        sheen = slot.get("_faugus_x_sheen")
        if sheen is not None:
            sheen.set_state(0, 0, 0)
        if slot.pop("_faugus_x_lean", None) is not None:
            bx, by = self._base(slot)
            self.win.carrousel_fixed.move(slot["box"], bx, by)
