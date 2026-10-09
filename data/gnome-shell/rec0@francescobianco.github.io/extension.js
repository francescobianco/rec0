// Tells rec0 which window has the focus, where it is and what it shows.
//
// Wayland does not let applications see other windows: this extension runs
// inside GNOME Shell and publishes the focused window on the session bus
// (io.github.francescobianco.Rec0.Shell). rec0 reads it to switch scenes on its
// own, as it does on X11 with Xlib (see rec0/shell.py).

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.francescobianco.Rec0.Shell';
const OBJECT_PATH = '/io/github/francescobianco/Rec0/Shell';
const VERSION = 2;

const IFACE = `<node>
  <interface name="io.github.francescobianco.Rec0.Shell">
    <method name="GetFocus">
      <arg type="a{sv}" direction="out" name="window"/>
    </method>
    <method name="GetWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="a{sv}" direction="out" name="window"/>
    </method>
    <signal name="FocusChanged">
      <arg type="a{sv}" name="window"/>
    </signal>
    <property name="Version" type="u" access="read"/>
  </interface>
</node>`;

// Changes of the focused window that make rec0 look again: title (browser
// tabs), moves, resizes, minimize, fullscreen and maximize.
const WINDOW_SIGNALS = [
    'notify::title', 'notify::wm-class', 'position-changed', 'size-changed',
    'notify::minimized', 'notify::fullscreen',
    'notify::maximized-horizontally', 'notify::maximized-vertically',
];

function describe(win) {
    if (!win)
        return {};
    const r = win.get_frame_rect();
    const b = win.get_buffer_rect();   // with the client-side shadows
    // is_maximized() replaces the two properties in newer Mutter.
    const maximized = win.is_maximized?.() ?? (win.maximized_horizontally && win.maximized_vertically);
    return {
        id: new GLib.Variant('t', win.get_id()),
        title: new GLib.Variant('s', win.get_title() ?? ''),
        wm_class: new GLib.Variant('s', win.get_wm_class() ?? ''),
        x: new GLib.Variant('i', r.x),
        y: new GLib.Variant('i', r.y),
        width: new GLib.Variant('i', r.width),
        height: new GLib.Variant('i', r.height),
        buffer_x: new GLib.Variant('i', b.x),
        buffer_y: new GLib.Variant('i', b.y),
        buffer_width: new GLib.Variant('i', b.width),
        buffer_height: new GLib.Variant('i', b.height),
        fullscreen: new GLib.Variant('b', win.is_fullscreen() || maximized),
        minimized: new GLib.Variant('b', win.minimized),
    };
}

export default class Rec0Extension extends Extension {
    enable() {
        this._window = null;
        this._windowIds = [];
        this._pending = 0;
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._owner = Gio.bus_own_name_on_connection(Gio.DBus.session, BUS_NAME,
            Gio.BusNameOwnerFlags.NONE, null, null);
        this._focusId = global.display.connect('notify::focus-window', () => this._follow());
        this._follow();
    }

    disable() {
        global.display.disconnect(this._focusId);
        this._unfollow();
        if (this._pending)
            GLib.source_remove(this._pending);
        Gio.bus_unown_name(this._owner);
        this._dbus.unexport();
        this._dbus = null;
    }

    get Version() {
        return VERSION;
    }

    GetFocus() {
        return describe(this._window);
    }

    // A window rec0 shows, followed while it is not focused (empty: closed).
    GetWindow(id) {
        const win = global.get_window_actors().map(a => a.meta_window).find(w => w.get_id() === id);
        return describe(win ?? null);
    }

    _follow() {
        this._unfollow();
        const win = global.display.focus_window;
        // Only real windows: no shell popups, tooltips or menus.
        const type = win?.get_window_type();
        if (type === Meta.WindowType.NORMAL || type === Meta.WindowType.DIALOG) {
            this._window = win;
            for (const signal of WINDOW_SIGNALS) {
                try {
                    this._windowIds.push(win.connect(signal, () => this._changed()));
                } catch {
                    // A property this Mutter does not have.
                }
            }
            this._windowIds.push(win.connect('unmanaged', () => {
                this._windowIds = [];
                this._window = null;
                this._changed();
            }));
        }
        this._changed();
    }

    _unfollow() {
        for (const id of this._windowIds)
            this._window.disconnect(id);
        this._windowIds = [];
        this._window = null;
    }

    // Coalesced: a drag moves the window on every frame, one signal per idle is enough.
    _changed() {
        if (this._pending)
            return;
        this._pending = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._pending = 0;
            this._dbus?.emit_signal('FocusChanged',
                new GLib.Variant('(a{sv})', [describe(this._window)]));
            return GLib.SOURCE_REMOVE;
        });
    }
}
