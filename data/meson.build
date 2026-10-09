desktop_in = app_id + '.desktop.in'
metainfo_in = app_id + '.metainfo.xml.in'

if msgfmt.found()
  desktop = i18n.merge_file(input: desktop_in, output: app_id + '.desktop', type: 'desktop',
    po_dir: '../po', install: true, install_dir: get_option('datadir') / 'applications')
  metainfo = i18n.merge_file(input: metainfo_in, output: app_id + '.metainfo.xml',
    po_dir: '../po', install: true, install_dir: get_option('datadir') / 'metainfo')
else
  desktop = configure_file(input: desktop_in, output: app_id + '.desktop', copy: true,
    install: true, install_dir: get_option('datadir') / 'applications')
  metainfo = configure_file(input: metainfo_in, output: app_id + '.metainfo.xml', copy: true,
    install: true, install_dir: get_option('datadir') / 'metainfo')
endif

configure_file(
  input: app_id + '.service.in',
  output: app_id + '.service',
  configuration: {'bindir': bindir},
  install: true,
  install_dir: get_option('datadir') / 'dbus-1' / 'services',
)

install_data(app_id + '.mime.xml', rename: app_id + '.xml',
  install_dir: get_option('datadir') / 'mime' / 'packages')
install_data(app_id + '.gschema.xml', install_dir: get_option('datadir') / 'glib-2.0' / 'schemas')
install_data('icons/hicolor/scalable/apps/' + app_id + '.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps')
install_data('icons/hicolor/symbolic/apps/' + app_id + '-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'symbolic' / 'apps')
install_data('../assets/background.jpg', install_dir: pkgdatadir)
install_data('../assets/rnnoise/sh.rnnn', install_dir: pkgdatadir / 'rnnoise')

desktop_validate = find_program('desktop-file-validate', required: false)
if desktop_validate.found()
  test('validate-desktop', desktop_validate, args: [desktop])
endif
appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('validate-metainfo', appstreamcli, args: ['validate', '--no-net', '--explain', metainfo])
endif
test('validate-schema', find_program('glib-compile-schemas'),
  args: ['--strict', '--dry-run', meson.current_source_dir()])
