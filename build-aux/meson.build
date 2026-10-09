# The `rec0` launcher (kept here: the build root already has a rec0/ directory).
configure_file(
  input: 'rec0.in',
  output: 'rec0',
  configuration: {'PYTHON': py.full_path(), 'pkgdatadir': pkgdatadir},
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)
