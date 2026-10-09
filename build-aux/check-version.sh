#!/bin/sh
# Checks that VERSION is the one declared everywhere, and that CHANGELOG.md
# has a section for it. Run before tagging a release:
#
#   build-aux/check-version.sh 1.2.3
set -eu

cd "$(dirname "$0")/.."
VERSION=${1:?usage: $0 VERSION}
fail=0

check() {   # file, version found in it
    if [ "$2" != "$VERSION" ]; then
        echo "$1: version is '$2', expected '$VERSION'" >&2
        fail=1
    fi
}

check meson.build "$(sed -n "s/^  version: '\(.*\)',$/\1/p" meson.build)"
check rec0/__init__.py "$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' rec0/__init__.py)"
check pyproject.toml "$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)"
check data/io.github.francescobianco.Rec0.metainfo.xml.in \
    "$(sed -n 's/.*<release version="\([^"]*\)".*/\1/p' data/io.github.francescobianco.Rec0.metainfo.xml.in | head -1)"
grep -q "^## \[$VERSION\]" CHANGELOG.md || { echo "CHANGELOG.md: no section for $VERSION" >&2; fail=1; }

[ "$fail" = 0 ] && echo "version $VERSION: consistent"
exit "$fail"
