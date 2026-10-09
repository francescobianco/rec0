#!/bin/sh
# Builds dist/rec0_VERSION_all.deb from the source tree, through meson.
#
#   build-aux/deb/build-deb.sh [VERSION]
#
# VERSION defaults to the one in meson.build. A copy without the version,
# dist/rec0_all.deb, is what the "latest release" download link points to.
set -eu

cd "$(dirname "$0")/../.."
VERSION=${1:-$(sed -n "s/^  version: '\(.*\)',$/\1/p" meson.build)}
BUILD=_build-deb
ROOT=$BUILD/root

# Without msgfmt meson would quietly leave the translations out.
command -v msgfmt >/dev/null || { echo "msgfmt is required (apt install gettext)" >&2; exit 1; }

rm -rf "$BUILD"
meson setup "$BUILD" --prefix=/usr >/dev/null
# With DESTDIR meson skips its post-install hooks: schemas, icon cache, desktop
# and MIME databases are refreshed by dpkg triggers on the target system.
DESTDIR="$PWD/$ROOT" meson install -C "$BUILD" --skip-subprojects >/dev/null

mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/share/doc/rec0"
cp LICENSE "$ROOT/usr/share/doc/rec0/copyright"
gzip -9n -c CHANGELOG.md > "$ROOT/usr/share/doc/rec0/changelog.gz"

SIZE=$(du -sk "$ROOT/usr" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" build-aux/deb/control > "$ROOT/DEBIAN/control"

mkdir -p dist
fakeroot dpkg-deb --build -Zxz "$ROOT" "dist/rec0_${VERSION}_all.deb" >/dev/null
cp "dist/rec0_${VERSION}_all.deb" dist/rec0_all.deb
echo "dist/rec0_${VERSION}_all.deb"
