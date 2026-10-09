project('rec0',
  version: '0.1.0',
  license: 'MIT',
  meson_version: '>= 0.62.0',
)

gnome = import('gnome')
i18n = import('i18n')
python = import('python')

app_id = 'io.github.francescobianco.Rec0'
py = python.find_installation('python3', modules: ['yaml', 'gi', 'cairo'])

prefix = get_option('prefix')
bindir = prefix / get_option('bindir')
datadir = prefix / get_option('datadir')
localedir = prefix / get_option('localedir')
pkgdatadir = datadir / meson.project_name()

msgfmt = find_program('msgfmt', required: false)

subdir('data')
subdir('rec0')
if msgfmt.found()
  subdir('po')
endif

subdir('build-aux')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
  update_mime_database: true,
)
