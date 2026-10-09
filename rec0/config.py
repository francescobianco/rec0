"""Build configuration. `make install` (or meson) rewrites the paths below;
when they are None rec0 runs from the source tree."""

import os

BASE_ID = "io.github.francescobianco.Rec0"
VERSION = "0.1.0"
GETTEXT_PACKAGE = "rec0"
WEBSITE = "https://github.com/francescobianco/rec0"
ISSUE_URL = "https://github.com/francescobianco/rec0/issues"

LOCALEDIR = None
PKGDATADIR = None

# The development profile (`make start`) gets its own application ID, so it can
# run next to the installed rec0, and the striped "devel" header bar.
PROFILE = os.environ.get("REC0_PROFILE", "default")
APP_ID = BASE_ID + ".Devel" if PROFILE == "development" else BASE_ID
SCHEMA_ID = BASE_ID
ICON_NAME = BASE_ID
