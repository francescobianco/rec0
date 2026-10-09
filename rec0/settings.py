"""GSettings access that also works from the source tree and without a schema."""

from __future__ import annotations

from gi.repository import Gio

from . import config
from .i18n import SOURCE_ROOT

DEFAULTS = {
    "window-width": 860,
    "window-height": 720,
    "window-maximized": False,
    "last-project": "",
    "reopen-last-project": True,
    "show-bubble": True,
    "countdown": 3,
    "process-audio": True,
    "camera-device": "",
    "microphone-device": "",
}


class _Memory:
    """Stand-in when the schema is not installed (e.g. `python3 -m rec0`)."""

    def __init__(self):
        self._values = dict(DEFAULTS)

    def get_value(self, key):
        return self._values[key]

    get_int = get_boolean = get_string = get_value

    def set_value(self, key, value):
        self._values[key] = value

    set_int = set_boolean = set_string = set_value

    def bind(self, *_args):
        pass


_settings = None


def get():
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


def _load():
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup(config.SCHEMA_ID, True) if source else None
    if schema is None:
        build = SOURCE_ROOT / "_build" / "schemas"
        if (build / "gschemas.compiled").exists():
            local = Gio.SettingsSchemaSource.new_from_directory(str(build), source, False)
            schema = local.lookup(config.SCHEMA_ID, False)
    if schema is None:
        return _Memory()
    return Gio.Settings.new_full(schema, None, None)
