# -*- coding: utf-8 -*-
#
# Copyright (C) 2018-2020 Matthias Klumpp <matthias@tenstral.net>
#
# Licensed under the GNU Lesser General Public License Version 3
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the license, or
# (at your option) any later version.
#
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this software.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import logging as log
from argparse import ArgumentParser, HelpFormatter
from .config import GlobalConfig
from .utils.env import set_unicode_allowed, set_owning_user
from .osbase import OSBase


def init_config(options):
    '''
    Create a new :GlobalConfig from command-line options.
    '''
    gconf = GlobalConfig()
    gconf.load(options.config)

    # check if we are forbidden from using unicode - otherwise we build
    # with unicode enabled by default
    if options.no_unicode:
        set_unicode_allowed(False)
    else:
        if 'utf-8' not in os.environ.get('LANG', 'utf-8').lower():
            log.warning('Building with unicode support, but your environment does not seem to support unicode.')
        set_unicode_allowed(True)

    if options.owner:
        info = options.owner.split(':')
        if len(info) > 2:
            print('You can only use one colon to split user:group when using the --owner flag.')
            sys.exit(1)
        if len(info) == 2:
            user = info[0]
            group = info[1]
        else:
            user = info[0]
            group = None
        set_owning_user(user, group)

    return gconf


def check_print_version(options):
    if options.show_version:
        from . import __version__
        print(__version__)
        sys.exit(0)


def command_create(options):
    ''' Create new container image '''

    check_print_version(options)
    if not options.suite:
        print('Need at least a suite name to bootstrap!')
        sys.exit(1)
    gconf = init_config(options)

    components = None
    if options.components:
        components = options.components.split(',')

    extra_suites = []
    if options.extra_suites:
        extra_suites = options.extra_suites.strip().split(' ')

    osbase = OSBase(gconf,
                    options.suite,
                    options.arch,
                    variant=options.variant,
                    base_suite=options.base_suite)
    r = osbase.create(options.mirror,
                      components,
                      extra_suites,
                      options.extra_source_lines)
    if not r:
        sys.exit(2)


def command_delete(options):
    ''' Delete container image '''

    check_print_version(options)
    if not options.suite:
        print('No suite name was specified!')
        sys.exit(1)
    gconf = init_config(options)
    osbase = OSBase(gconf, options.suite, options.arch, options.variant)
    r = osbase.delete()
    if not r:
        sys.exit(2)


def command_update(options):
    ''' Update container image '''

    check_print_version(options)
    if not options.suite:
        print('Need at least a suite name for update!')
        sys.exit(1)
    gconf = init_config(options)
    osbase = OSBase(gconf, options.suite, options.arch, options.variant)
    if options.recreate:
        r = osbase.recreate()
    else:
        r = osbase.update()
    if not r:
        sys.exit(2)


def command_list(options):
    ''' List container images '''

    from .osbase import print_container_base_image_info

    check_print_version(options)
    gconf = init_config(options)
    print_container_base_image_info(gconf)


def command_build(options):
    ''' Build a package in a new volatile container '''

    from .build import Build

    check_print_version(options)
    if not options.suite:
        print('Need at least a suite name for building!')
        sys.exit(1)
    gconf = init_config(options)
    osbase = OSBase(gconf, options.suite, options.arch, options.variant)

    if not Build(options, osbase).run():
        sys.exit(2)


def command_login(options):
    ''' Open interactive session in a container '''

    check_print_version(options)
    if not options.suite:
        print('Need at least a suite name!')
        sys.exit(1)
    gconf = init_config(options)
    osbase = OSBase(gconf, options.suite, options.arch, options.variant)

    allowed = []
    if options.allow:
        allowed = [s.strip() for s in options.allow.split(',')]
    r = osbase.login(options.persistent, allowed)
    if not r:
        sys.exit(2)


def command_run(options, custom_command):
    ''' Run arbitrary command in container session '''

    check_print_version(options)
    if not options.suite:
        print('Need at least a suite name!')
        sys.exit(1)
    gconf = init_config(options)
    osbase = OSBase(gconf,
                    options.suite,
                    options.arch,
                    options.variant,
                    cachekey=options.cachekey)

    allowed = []
    if options.allow:
        allowed = [s.strip() for s in options.allow.split(',')]

    r = osbase.run(custom_command,
                   options.build_dir,
                   options.artifacts_dir,
                   init_command=options.init_command,
                   copy_command=options.external_commad,
                   header_msg=options.header,
                   allowed=allowed)
    if not r:
        sys.exit(2)


class CustomArgparseFormatter(HelpFormatter):

    def _split_lines(self, text, width):
        if text.startswith('CF|'):
            return text[3:].splitlines()
        return HelpFormatter._split_lines(self, text, width)


def add_container_select_arguments(parser):
    parser.add_argument('--variant', action='store', dest='variant', default=None,
                        help='Set the bootstrap script variant.')
    parser.add_argument('-a', '--arch', action='store', dest='arch', default=None,
                        help='The architecture of the container.')
    parser.add_argument('suite', action='store', nargs='?', default=None,
                        help='The suite name of the container.')


def create_parser(formatter_class=None):
    ''' Create debspawn CLI argument parser '''

    if not formatter_class:
        formatter_class = CustomArgparseFormatter

    parser = ArgumentParser(description='Build in nspawn containers', formatter_class=formatter_class)
    subparsers = parser.add_subparsers(dest='sp_name', title='subcommands')

    # generic arguments
    parser.add_argument('-c', '--config', action='store', dest='config', default=None,
                        help='Path to the global config file.')
    parser.add_argument('--verbose', action='store_true', dest='verbose',
                        help='Enable debug messages.')
    parser.add_argument('--no-unicode', action='store_true', dest='no_unicode',
                        help='Disable unicode support.')
    parser.add_argument('--version', action='store_true', dest='show_version',
                        help='Display the version of debspawn itself.')

    parser.add_argument('--owner', action='store', dest='owner', default=None,
                        help=('Set the user name/uid and group/gid separated by a colon '
                              'whose behalf we are acting.'))

    # 'create' command
    sp = subparsers.add_parser('create', help='Create new container image')
    add_container_select_arguments(sp)
    sp.add_argument('--mirror', action='store', dest='mirror', default=None,
                    help='Set a specific mirror to bootstrap from.')
    sp.add_argument('--components', action='store', dest='components', default=None,
                    help='A comma-separated list of archive components to enable in the newly created image.')
    sp.add_argument('--base-suite', action='store', dest='base_suite', default=None,
                    help='A full suite that forms the base of the selected partial suite (e.g. for -updates and -backports).')
    sp.add_argument('--extra-suites', action='store', dest='extra_suites', default=None,
                    help='Space-separated list of additional suites that should also be added to the sources.list file.')
    sp.add_argument('--extra-sourceslist-lines', action='store', dest='extra_source_lines', default=None,
                    help='Lines that should be added to the build environments source.list verbatim. Separate lines by linebreaks.')
    sp.set_defaults(func=command_create)

    # 'delete' command
    sp = subparsers.add_parser('delete', help='Remove a container image')
    add_container_select_arguments(sp)
    sp.set_defaults(func=command_delete)

    # 'update' command
    sp = subparsers.add_parser('update', help='Update a container image')
    add_container_select_arguments(sp)
    sp.add_argument('--recreate', action='store_true', dest='recreate',
                    help='Re-create the container image from scratch using the settings used to create it previously, instead of just updating it.')
    sp.set_defaults(func=command_update)

    # 'list' command
    sp = subparsers.add_parser('list', help='List available container images', aliases=['ls'])
    sp.set_defaults(func=command_list)

    # 'build' command
    sp = subparsers.add_parser('build', help='Build a package in an isolated environment', formatter_class=formatter_class, aliases=['b'])
    add_container_select_arguments(sp)
    sp.add_argument('-s', '--sign', action='store_true', dest='sign',
                    help='Sign the resulting package.')
    sp.add_argument('--only', choices=['binary', 'arch', 'indep', 'source'], dest='build_only',
                    help=('CF|Select only a specific set of packages to be built. Choices are:\n'
                          'binary: Build only binary packages, no source files are to be built and/or distributed.\n'
                          'arch: Build only architecture-specific binary packages.\n'
                          'indep: Build only architecture-independent (arch:all) binary packages.\n'
                          'source: Do a source-only build, no binary packages are made.'))
    sp.add_argument('--include-orig', action='store_true', dest='include_orig',
                    help='Forces the inclusion of the original source.')
    sp.add_argument('--buildflags', action='store', dest='buildflags',
                    help='Set flags passed through to dpkg-buildpackage as semicolon-separated list.')
    sp.add_argument('--results-dir', action='store', dest='results_dir',
                    help='Override the configured results directory and return artifacts at a custom location.')
    sp.add_argument('--maintainer', action='store', dest='maintainer',
                    help=('Set the name and email address of the maintainer for this package and upload, rather than using '
                          'the information from the source tree\'s control file or changelog.'))
    sp.add_argument('--clean-source', action='store_true', dest='clean_source',
                    help=('Run the d/rules clean target outside of the container. This means the package build dependencies need to be '
                          'installed on the host system when building from a source directory.'))
    sp.add_argument('--lintian', action='store_true', dest='lintian',
                    help='Run the Lintian static analysis tool for Debian packages after the package is built.')
    sp.add_argument('--no-buildlog', action='store_true', dest='no_buildlog',
                    help='Do not write a build log.')
    sp.add_argument('-i', '--interact', action='store_true', dest='interact',
                    help='Run an interactive shell in the build environment after build. This implies `--no-buildlog` and disables the log.')
    sp.add_argument('target', action='store', nargs='?', default=None,
                    help='The source package file or source directory to build.')
    sp.set_defaults(func=command_build)

    # 'login' command
    sp = subparsers.add_parser('login', help='Open interactive session in a container')
    add_container_select_arguments(sp)
    sp.add_argument('--persistent', action='store_true', dest='persistent',
                    help='Make changes done in the session persistent.')
    sp.add_argument('--allow', action='store', dest='allow',
                    help='List one or more additional permissions to grant the container. Takes a comma-separated list of capability names.')
    sp.set_defaults(func=command_login)

    # 'run' command
    sp = subparsers.add_parser('run', help='Run arbitrary command in an ephemeral container')
    add_container_select_arguments(sp)
    sp.add_argument('--artifacts-out', action='store', dest='artifacts_dir', default=None,
                    help='Directory on the host where artifacts can be stored. Mounted to /srv/artifacts in the guest.')
    sp.add_argument('--build-dir', action='store', dest='build_dir', default=None,
                    help='Select a host directory that gets bind mounted to /srv/build.')
    sp.add_argument('--cachekey', action='store', dest='cachekey', default=None,
                    help=('If set, use the specified cache-ID to store an initialized container image for faster initialization times.\n'
                          'This may mean that the command passed in `--init-command` is skipped if the cache already existed.'))
    sp.add_argument('--init-command', action='store', dest='init_command', default=None,
                    help='The command or command script used to set up the container.')
    sp.add_argument('-x', '--external-command', action='store_true', dest='external_commad',
                    help='If set, the command script(s) will be copied from the host to the container and then executed.')
    sp.add_argument('--header', action='store', dest='header', default=None,
                    help='Name of the task that is run, will be printed as header.')
    sp.add_argument('--allow', action='store', dest='allow',
                    help='List one or more additional permissions to grant the container. Takes a comma-separated list of capability names.')
    sp.add_argument('command', action='store', nargs='*', default=None,
                    help='The command to run.')

    return parser


def run(mainfile, args):
    if len(args) == 0:
        print('Need a subcommand to proceed!')
        sys.exit(1)

    parser = create_parser()

    # special case, so 'run' can understand which arguments are for debspawn and which are
    # for the command to be executed
    custom_command = None
    if args[0] == 'run':
        for i, arg in enumerate(args):
            if arg == '---':
                if i + 1 == len(args):
                    print('No command was given after "---", can not continue.')
                    sys.exit(1)
                custom_command = args[i + 1:]
                args = args[:i]
                break

    args = parser.parse_args(args)
    check_print_version(args)
    if args.sp_name == 'run':
        if not custom_command:
            custom_command = args.command
        command_run(args, custom_command)
    else:
        if not hasattr(args, 'func'):
            print('Unknown or no subcommand was provided. Can not proceed.')
            sys.exit(1)
        args.func(args)
