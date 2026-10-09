# rec0 — development and local installation.
#
#   make start       run from the source tree (development profile + dev API)
#   make api ARGS=…  talk to the running dev instance (see build-aux/devctl.py)
#   make test        run the test suite
#   make install     install for this user (PREFIX=~/.local, no root needed)
#   make uninstall   remove what `make install` installed
#   make pot         refresh po/rec0.pot from the sources
#
# Distribution packages and Flatpak use meson (see meson.build).

APP_ID     := io.github.francescobianco.Rec0
PYTHON     ?= /usr/bin/python3
PREFIX     ?= $(HOME)/.local
BINDIR     := $(PREFIX)/bin
DATADIR    := $(PREFIX)/share
PKGDATADIR := $(DATADIR)/rec0
LOCALEDIR  := $(DATADIR)/locale
BUILD      := _build
LINGUAS    := $(shell cat po/LINGUAS)
MSGFMT     := $(shell command -v msgfmt >/dev/null && echo msgfmt || echo "$(PYTHON) build-aux/msgfmt.py")
MO_FILES   := $(foreach l,$(LINGUAS),$(BUILD)/locale/$(l)/LC_MESSAGES/rec0.mo)

.PHONY: all build start api test install uninstall pot clean

all: build

build: $(BUILD)/schemas/gschemas.compiled $(MO_FILES)

$(BUILD)/schemas/gschemas.compiled: data/$(APP_ID).gschema.xml
	@mkdir -p $(BUILD)/schemas
	cp $< $(BUILD)/schemas/
	glib-compile-schemas --strict $(BUILD)/schemas

$(BUILD)/locale/%/LC_MESSAGES/rec0.mo: po/%.po
	@mkdir -p $(dir $@)
	$(MSGFMT) -o $@ $<

start: build
	REC0_PROFILE=development REC0_DEV_API=1 GSETTINGS_SCHEMA_DIR=$(BUILD)/schemas \
		$(PYTHON) -m rec0 $(ARGS)

api:
	@$(PYTHON) build-aux/devctl.py $(ARGS)

test:
	$(PYTHON) -m pytest -q

pot:
	pygettext3 -k _ -k N_ -o po/rec0.pot $$(grep '\.py$$' po/POTFILES)

install: build
	@echo "Installing rec0 into $(PREFIX)"
	install -d $(PKGDATADIR)/rec0 $(BINDIR)
	install -m 644 rec0/*.py $(PKGDATADIR)/rec0/
	sed -e 's|^LOCALEDIR = None|LOCALEDIR = "$(LOCALEDIR)"|' \
	    -e 's|^PKGDATADIR = None|PKGDATADIR = "$(PKGDATADIR)"|' \
	    rec0/config.py > $(PKGDATADIR)/rec0/config.py
	install -m 644 assets/background.jpg $(PKGDATADIR)/background.jpg
	install -Dm 644 assets/rnnoise/sh.rnnn $(PKGDATADIR)/rnnoise/sh.rnnn
	sed -e 's|@PYTHON@|$(PYTHON)|' -e 's|@pkgdatadir@|$(PKGDATADIR)|' build-aux/rec0.in > $(BINDIR)/rec0
	chmod 755 $(BINDIR)/rec0
	install -Dm 644 data/$(APP_ID).desktop.in $(DATADIR)/applications/$(APP_ID).desktop
	install -Dm 644 data/$(APP_ID).metainfo.xml.in $(DATADIR)/metainfo/$(APP_ID).metainfo.xml
	sed 's|@bindir@|$(BINDIR)|' data/$(APP_ID).service.in > $(BUILD)/$(APP_ID).service
	install -Dm 644 $(BUILD)/$(APP_ID).service $(DATADIR)/dbus-1/services/$(APP_ID).service
	install -Dm 644 data/icons/hicolor/scalable/apps/$(APP_ID).svg \
		$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	install -Dm 644 data/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg \
		$(DATADIR)/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg
	install -Dm 644 data/$(APP_ID).gschema.xml $(DATADIR)/glib-2.0/schemas/$(APP_ID).gschema.xml
	glib-compile-schemas $(DATADIR)/glib-2.0/schemas
	@for l in $(LINGUAS); do \
		install -Dm 644 $(BUILD)/locale/$$l/LC_MESSAGES/rec0.mo $(LOCALEDIR)/$$l/LC_MESSAGES/rec0.mo; \
	done
	-gtk4-update-icon-cache -q -t -f $(DATADIR)/icons/hicolor
	-update-desktop-database -q $(DATADIR)/applications
	@echo "Done: run 'rec0' or find rec0 in the Activities overview."

uninstall:
	rm -rf $(PKGDATADIR)
	rm -f $(BINDIR)/rec0 \
		$(DATADIR)/applications/$(APP_ID).desktop \
		$(DATADIR)/metainfo/$(APP_ID).metainfo.xml \
		$(DATADIR)/dbus-1/services/$(APP_ID).service \
		$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg \
		$(DATADIR)/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg \
		$(DATADIR)/glib-2.0/schemas/$(APP_ID).gschema.xml
	@for l in $(LINGUAS); do rm -f $(LOCALEDIR)/$$l/LC_MESSAGES/rec0.mo; done
	-glib-compile-schemas $(DATADIR)/glib-2.0/schemas
	-gtk4-update-icon-cache -q -t -f $(DATADIR)/icons/hicolor
	-update-desktop-database -q $(DATADIR)/applications

clean:
	rm -rf $(BUILD)
