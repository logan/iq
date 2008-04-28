#!/usr/bin/env PYTHONPATH=. python

import logging
from optparse import OptionParser
import os
import subprocess
import sys

IQ_DEPENDENCIES = [
  'minifb.py',
  'louie',
  'simplejson',
]

GOOGLE_DEPENDENCIES = [
  # Empty string for dev_appserver.py
  '',

  # YAML for generating configuration files
  'lib/yaml/lib',
]

APP_CONFIG_TEMPLATE = {
  'runtime': 'python',
  'api_version': 1,
  'handlers': [
    {
      'url': r'/admin.*',
      'script': 'admin.py',
    },
    {
      'url': r'/facebook.*',
      'script': 'facebook.py',
    },
    {
      'url': r'/json.*',
      'script': 'json.py',
    },
    {
      'url': r'/legacy.*',
      'script': 'legacy.py',
    },
    {
      'url': r'/(.*\.(css|js))',
      'static_files': r'\2/\1',
      'upload': r'(css|js)/(.*)',
      'expiration': '1d',
    },
    {
      'url': r'/(small|medium|large)/(.*\.(png))',
      'static_files': r'icons/\1/\2',
      'upload': r'icons/(small|medium|large)/(.*\.(png))',
      'expiration': '1d',
    },
    {
      'url': r'/.*',
      'script': 'ui.py',
    },
  ],
}

def IndexSet(*kinds):
  indices = []
  for kind in kinds:
    indices.extend(kind)
  return {'indexes': indices}


def Kind(name, *indices):
  for ancestor, properties in indices:
    index = {'kind': name}
    if properties:
      index['properties'] = properties
    if ancestor:
      index['ancestor'] = 'yes'
    yield index


def Index(*properties, **kwargs):
  ancestor = kwargs.get('ancestor', False)
  return ancestor, list(properties)


def Property(name):
  property = {'name': name}
  if name.startswith('-'):
    property['name'] = name[1:]
    property['direction'] = 'desc'
  return property


INDEX_CONFIG = IndexSet(
    Kind('Quote',
         Index(Property('__searchable_text_index'),
               Property('draft'),
              ),

         Index(Property('submitted')),

         Index(Property('-submitted')),

         Index(Property('draft'),
               Property('submitted'),
              ),

         Index(Property('draft'),
               Property('-submitted'),
              ),

         Index(Property('submitted'),
               ancestor=True,
              ),

         Index(Property('-submitted'),
               ancestor=True,
              ),

         Index(Property('draft'),
               Property('submitted'),
               ancestor=True,
              ),

         Index(Property('draft'),
               Property('-submitted'),
               ancestor=True,
              ),

        ),
)

def saveYaml(data, path):
  import yaml

  logging.debug('Dumping object to %s: %r', path, data)
  f = file(path, 'w')
  yaml.dump(data, f)
  f.close()


def generateYaml(options):
  app_config = APP_CONFIG_TEMPLATE.copy()
  if options.app:
    app_config['application'] = options.app
  else:
    app_config['application'] = 'iq-dev'
  app_config['version'] = options.version
  saveYaml(app_config, os.path.join(options.srcdir, 'app.yaml'))

  index_path = os.path.join(options.srcdir, 'index.yaml')
  if options.app:
    index_config = INDEX_CONFIG
    saveYaml(index_config, index_path)
    f = file(index_path, 'a')
    f.write('# AUTOGENERATED\n')
    f.close()


def resolveGoogleBase(options):
  base = None
  if options.googlebase:
    base = options.googlebase
  else:
    for path in os.environ['PATH'].split(os.pathsep):
      bin = os.path.join(path, 'dev_appserver.py')
      if os.path.exists(bin):
        logging.debug('Found dev_appserver.py at %s', bin)
        base = os.path.dirname(os.path.abspath(os.path.realpath(bin)))
        break
  logging.debug('base = %r', base)
  if base and os.path.isdir(base):
    logging.info('Located Google SDK at %s', base)
    for entry in GOOGLE_DEPENDENCIES:
      logging.debug('Adding Google dependency to PYTHONPATH: %s', entry)
      path = os.path.join(base, entry)
      if not os.path.isdir(path):
        logging.error('Expected to be a directory: %s', path)
        return None
      sys.path.append(path)
    return base


def resolveIqDependencies(options):
  for dependency in IQ_DEPENDENCIES:
    logging.info('Making sure %r is in the right place', dependency)
    if not os.path.exists(os.path.join(options.srcdir, dependency)):
      # TODO: automate this instead of failing
      logging.fatal('A symlink to module %r needs to be added to %s',
                    dependency, options.srcdir)
      sys.exit(1)


def runGoogleSdkTool(googlebase, *args):
  args = map(str, args)
  args[0] = os.path.join(googlebase, args[0])
  logging.info('Running: %s', ' '.join(args))
  subprocess.call(args)


def deployOnAppSpot(appbase, googlebase, args):
  runGoogleSdkTool(googlebase, 'appcfg.py', 'update', appbase, *args)


def runDevAppServer(appbase, googlebase, port, args):
  logging.debug('appbase=%r, googlebase=%r, port=%r, args=%r',
                appbase, googlebase, port, args)
  runGoogleSdkTool(googlebase, 'dev_appserver.py', appbase, '-a', '', '-p', port, *args)


def main():
  parser = OptionParser(usage='usage: %prog [options] [-- sdkoptions]')
  parser.add_option('-A', '--appspot', dest='app',
                    help='Deploy to this appspot.com subdomain (non-qualified name).',
                   )
  parser.add_option('', '--appspot-version', type=int, dest='version', default=1,
                    help='Version of application to deploy on appspot.com.',
                   )
  parser.add_option('-a', '--appbase', dest='srcdir', default='iq',
                    help='Location of application modules.',
                   )
  parser.add_option('-D', '--devmode', type=int, dest='port', default=8080,
                    help='Run the application locally using dev_appserver.py',
                   )
  parser.add_option('-G', '--googlebase', dest='googlebase',
                    help='Path to the Google SDK.  If not given, will be determined'
                         ' by looking at dev_appserver.py in PATH.',
                   )
  parser.add_option('-v', '--verbose', action='store_true', dest='verbose',
                    help="Enable info-level logging.")
  parser.add_option('-V', '--very-verbose', action='store_true', dest='very_verbose',
                    help="Enable debug-level logging.")

  options, args = parser.parse_args()

  if options.very_verbose:
    logging.basicConfig(level=logging.DEBUG)
    logging.debug('very verbose logging ON')
  elif options.verbose:
    logging.basicConfig(level=logging.INFO)
    logging.info('verbose logging ON')

  if not options.app and not options.port:
    parser.error('Either --appspot (-A) or --devmode (-D) must be given')

  googlebase = resolveGoogleBase(options)
  if not googlebase:
    parser.error('Could not determine location of the Google SDK.  Make sure'
                 ' dev_appserver.py is in your PATH, or specify --googlebase (-G).')

  if not os.path.isdir(options.srcdir):
    parser.error('Could not locate the application source code (--appbase=%s)',
                 options.srcdir)

  generateYaml(options)

  if options.app:
    resolveIqDependencies(options)
    deployOnAppSpot(options.srcdir, googlebase, args)
  else:
    runDevAppServer(options.srcdir, googlebase, options.port, args)


if __name__ == '__main__':
  main()
