"""gettext setup. Source strings are English; translations live in po/."""

from __future__ import annotations

import gettext
import locale
from pathlib import Path

from . import config

SOURCE_ROOT = Path(__file__).resolve().parent.parent


def localedir() -> str:
    return config.LOCALEDIR or str(SOURCE_ROOT / "_build" / "locale")


def setup():
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    for mod in (gettext, locale):
        if hasattr(mod, "bindtextdomain"):
            mod.bindtextdomain(config.GETTEXT_PACKAGE, localedir())
        if hasattr(mod, "textdomain"):
            mod.textdomain(config.GETTEXT_PACKAGE)


setup()
_ = gettext.gettext
ngettext = gettext.ngettext


def pkgdata(name: str) -> Path:
    """Path of a bundled data file (e.g. the default background)."""
    if config.PKGDATADIR:
        return Path(config.PKGDATADIR) / name
    return SOURCE_ROOT / "assets" / name
