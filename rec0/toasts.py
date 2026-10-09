"""In-app notifications shown at the top of the window.

Adw.ToastOverlay always places toasts at the bottom, where they would cover
rec0's control bar: this is the same idea, slid in from the top.
"""

from __future__ import annotations

from collections import deque

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from .i18n import _  # noqa: E402

CSS = b"""
.top-toast { border-radius: 999px; padding: 4px 4px 4px 18px; margin-top: 10px; }
.top-toast label { font-weight: 600; }
"""


class TopToastOverlay(Gtk.Overlay):
    def __init__(self, child: Gtk.Widget):
        super().__init__(child=child)
        self._queue: deque = deque()
        self._timer = None
        self._label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=70, xalign=0)
        self._button = Gtk.Button(css_classes=["flat"], use_underline=True, visible=False)
        self._button.connect("clicked", lambda *_: self._dismiss())
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                           tooltip_text=_("Close"))
        close.connect("clicked", lambda *_: self._dismiss())
        box = Gtk.Box(spacing=6, css_classes=["osd", "top-toast"])
        box.append(self._label)
        box.append(self._button)
        box.append(close)
        self._revealer = Gtk.Revealer(child=box, halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                                      transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                      transition_duration=200)
        self._revealer.connect("notify::child-revealed", self._on_revealed)
        self.add_overlay(self._revealer)

    def add(self, title: str, button_label: str | None = None, action_name: str | None = None,
            timeout: int = 4):
        self._queue.append((title, button_label, action_name, timeout))
        if not self._revealer.get_reveal_child() and self._timer is None:
            self._show_next()

    def _show_next(self):
        if not self._queue:
            return
        title, button_label, action_name, timeout = self._queue.popleft()
        self._label.set_label(title)
        self._button.set_visible(bool(button_label))
        if button_label:
            self._button.set_label(button_label)
            self._button.set_action_name(action_name)
        self._revealer.set_reveal_child(True)
        self._timer = GLib.timeout_add_seconds(timeout, self._expire)

    def _expire(self):
        self._timer = None
        self._revealer.set_reveal_child(False)
        return False

    def _dismiss(self):
        if self._timer:
            GLib.source_remove(self._timer)
        self._expire()

    def _on_revealed(self, revealer, _pspec):
        # Once hidden, show what is waiting.
        if not revealer.get_child_revealed() and self._timer is None:
            self._show_next()
