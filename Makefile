# rec0 — development and local installation.
#
#   make deps        install the system packages needed to run and test (apt, sudo)
#   make start       run from the source tree (development profile + dev API)
#   make api ARGS=…  talk to the running dev instance (see build-aux/devctl.py)
#   make shell-extension  install and enable the GNOME Shell extension (Wayland scene switching)
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
EXT_UUID   := rec0@francescobianco.github.io
EXT_DIR    := $(DATADIR)/gnome-shell/extensions/$(EXT_UUID)
MSGFMT     := $(shell command -v msgfmt >/dev/null && echo msgfmt || echo "$(PYTHON) build-aux/msgfmt.py")
MO_FILES   := $(foreach l,$(LINGUAS),$(BUILD)/locale/$(l)/LC_MESSAGES/rec0.mo)

.PHONY: all deps build start dev-desktop api shell-extension test install uninstall pot clean

# Runtime (same as build-aux/deb/control) plus what development needs:
# schema compiler, gettext, pytest.
DEPS := python3 python3-gi python3-gi-cairo python3-yaml python3-numpy \
	gir1.2-gtk-4.0 gir1.2-gtk-3.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 \
	gir1.2-gdkpixbuf-2.0 gir1.2-graphene-1.0 \
	gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
	gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-x gstreamer1.0-pipewire \
	ffmpeg libx11-6 libxfixes3 \
	libglib2.0-bin gettext python3-pytest

all: build

# Installs only the packages that are missing, so it is quick (and asks no
# password) when the environment is already ready.
deps:
	@missing=$$(for p in $(DEPS); do \
		dpkg-query -W -f='$${Status}' $$p 2>/dev/null | grep -q 'ok installed' || echo $$p; \
	done); \
	if [ -z "$$missing" ]; then echo "All dependencies are installed."; \
	else echo "Installing:" $$missing; sudo apt-get install -y $$missing; fi

build: $(BUILD)/schemas/gschemas.compiled $(MO_FILES)

$(BUILD)/schemas/gschemas.compiled: data/$(APP_ID).gschema.xml
	@mkdir -p $(BUILD)/schemas
	cp $< $(BUILD)/schemas/
	glib-compile-schemas --strict $(BUILD)/schemas

$(BUILD)/locale/%/LC_MESSAGES/rec0.mo: po/%.po
	@mkdir -p $(dir $@)
	$(MSGFMT) -o $@ $<

# Opens examples/dev.r0 (real webcam and microphone) unless ARGS says otherwise.
start: build dev-desktop
	REC0_PROFILE=development REC0_DEV_API=1 GSETTINGS_SCHEMA_DIR=$(BUILD)/schemas \
		$(PYTHON) -m rec0 $(or $(ARGS),examples/dev.r0)

# Lets the shell show rec0's icon (dock, Alt+Tab) for the development instance.
DEV_DESKTOP := $(HOME)/.local/share/applications/$(APP_ID).Devel.desktop

dev-desktop:
	@mkdir -p $(dir $(DEV_DESKTOP))
	@printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=rec0 (Development)' \
		'Exec=make -C $(CURDIR) start' 'Icon=$(CURDIR)/data/icons/hicolor/scalable/apps/$(APP_ID).svg' \
		'StartupWMClass=$(APP_ID).Devel' 'Categories=AudioVideo;Video;' 'NoDisplay=true' > $(DEV_DESKTOP)

api:
	@$(PYTHON) build-aux/devctl.py $(ARGS)

# On Wayland GNOME Shell loads a new extension only at the next login.
shell-extension:
	install -Dm 644 -t $(EXT_DIR) data/gnome-shell/$(EXT_UUID)/metadata.json data/gnome-shell/$(EXT_UUID)/extension.js
	-gnome-extensions enable $(EXT_UUID) 2>/dev/null || \
		gsettings set org.gnome.shell enabled-extensions \
		"$$(gsettings get org.gnome.shell enabled-extensions | $(PYTHON) -c 'import ast,sys; l=ast.literal_eval(sys.stdin.read().replace("@as ","")); print(l if "$(EXT_UUID)" in l else l+["$(EXT_UUID)"])')"
	@echo "Extension installed: log out and back in if rec0 does not see it yet."

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
	install -Dm 644 data/$(APP_ID).mime.xml $(DATADIR)/mime/packages/$(APP_ID).xml
	install -Dm 644 -t $(EXT_DIR) data/gnome-shell/$(EXT_UUID)/metadata.json data/gnome-shell/$(EXT_UUID)/extension.js
	-update-mime-database $(DATADIR)/mime
	glib-compile-schemas $(DATADIR)/glib-2.0/schemas
	@for l in $(LINGUAS); do \
		install -Dm 644 $(BUILD)/locale/$$l/LC_MESSAGES/rec0.mo $(LOCALEDIR)/$$l/LC_MESSAGES/rec0.mo; \
	done
	-gtk4-update-icon-cache -q -t -f $(DATADIR)/icons/hicolor
	-update-desktop-database -q $(DATADIR)/applications
	@echo "Done: run 'rec0' or find rec0 in the Activities overview."

uninstall:
	rm -rf $(PKGDATADIR) $(EXT_DIR)
	rm -f $(BINDIR)/rec0 \
		$(DATADIR)/applications/$(APP_ID).desktop \
		$(DATADIR)/metainfo/$(APP_ID).metainfo.xml \
		$(DATADIR)/dbus-1/services/$(APP_ID).service \
		$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg \
		$(DATADIR)/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg \
		$(DATADIR)/glib-2.0/schemas/$(APP_ID).gschema.xml \
		$(DATADIR)/mime/packages/$(APP_ID).xml
	-update-mime-database $(DATADIR)/mime
	@for l in $(LINGUAS); do rm -f $(LOCALEDIR)/$$l/LC_MESSAGES/rec0.mo; done
	-glib-compile-schemas $(DATADIR)/glib-2.0/schemas
	-gtk4-update-icon-cache -q -t -f $(DATADIR)/icons/hicolor
	-update-desktop-database -q $(DATADIR)/applications

clean:
	rm -rf $(BUILD) $(DEV_DESKTOP)
