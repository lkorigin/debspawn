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
import subprocess
import shutil
import platform
from glob import glob
from collections.abc import Iterable
from .utils.env import ensure_root, switch_unprivileged, get_owner_uid_gid, get_free_space, get_tree_size
from .utils.misc import listify, temp_dir, cd, format_filesize, version_noepoch
from .utils.log import print_header, print_section, print_info, print_warn, print_error, \
    capture_console_output, save_captured_console_output
from .utils.command import safe_run
from .nspawn import nspawn_run_helper_persist, nspawn_run_persist
from .injectpkg import PackageInjector


def interact_with_build_environment(osbase, instance_dir, machine_name, *,
                                    pkg_dir_root, source_pkg_dir, aptcache_tmp, pkginjector, prev_exitcode):
    ''' Launch an interactive shell in the build environment '''

    # find the right directory to switch to
    pkg_dir = pkg_dir_root
    for f in glob(os.path.join(pkg_dir, '*')):
        if os.path.isdir(f):
            pkg_dir = f
            break

    print()
    print_info('Launching interactive shell in build environment.')
    if prev_exitcode != 0:
        print_info('The previous build step failed with exit code {}'.format(prev_exitcode))
    else:
        print_info('The previous build step was successful.')
    print_info('Temporary location of package files on the host:\n  => file://{}'.format(pkg_dir))
    print_info('Press CTL+D to exit the interactive shell.')
    print()

    nspawn_flags = ['--bind={}:/srv/build/'.format(pkg_dir_root)]
    nspawn_run_persist(osbase,
                       instance_dir,
                       machine_name,
                       chdir=os.path.join('/srv/build', os.path.basename(pkg_dir)),
                       flags=nspawn_flags,
                       tmp_apt_cache_dir=aptcache_tmp,
                       pkginjector=pkginjector,
                       syscall_filter=osbase.global_config.syscall_filter,
                       verbose=True)

    if source_pkg_dir:
        print()
        while True:
            try:
                copy_changes = input(('Should changes to the debian/ directory be copied back to the host?\n'
                                      'This will OVERRIDE all changes made on files on the host. [y/N]: '))
            except EOFError:
                copy_changes = 'n'
            if copy_changes == 'y' or copy_changes == 'Y':
                copy_changes = True
                break
            elif copy_changes == 'n' or copy_changes == 'N':
                copy_changes = False
                break
            elif not copy_changes:
                copy_changes = False
                break

        if copy_changes:
            print_info('Cleaning up...')
            # clean the source tree. we intentionally ignore errors here.
            nspawn_run_persist(osbase,
                               instance_dir,
                               machine_name,
                               chdir=os.path.join('/srv/build', os.path.basename(pkg_dir)),
                               flags=nspawn_flags,
                               command=['dpkg-buildpackage', '-T', 'clean'],
                               tmp_apt_cache_dir=aptcache_tmp,
                               pkginjector=pkginjector)

            print()
            print_info('Copying back changes...')
            known_files = {}
            dest_debian_dir = os.path.join(source_pkg_dir, 'debian')
            src_debian_dir = os.path.join(pkg_dir, 'debian')

            # get uid/gid of the user who invoked us
            o_uid, o_gid = get_owner_uid_gid()

            # collect list of existing packages
            for sdir, _, files in os.walk(dest_debian_dir):
                for f in files:
                    fname = os.path.join(sdir, f)
                    known_files[os.path.relpath(fname, dest_debian_dir)] = fname

            # walk through the source files, copying everything to the destination
            for sdir, _, files in os.walk(src_debian_dir):
                for f in files:
                    fname = os.path.join(sdir, f)
                    rel_fname = os.path.relpath(fname, src_debian_dir)
                    dest_fname = os.path.normpath(os.path.join(dest_debian_dir, rel_fname))
                    dest_dir = os.path.dirname(dest_fname)
                    if rel_fname in known_files:
                        del known_files[rel_fname]

                    if os.path.isdir(fname):
                        print('New dir: {}'.format(rel_fname))
                        with switch_unprivileged():
                            os.makedirs(dest_fname, exist_ok=True)
                        continue
                    if not os.path.isdir(dest_dir):
                        print('New dir: {}'.format(os.path.relpath(dest_dir, dest_debian_dir)))
                        with switch_unprivileged():
                            os.makedirs(dest_dir, exist_ok=True)

                    print('Copy: {}'.format(rel_fname))
                    shutil.copy2(fname,
                                 dest_fname,
                                 follow_symlinks=False)
                    os.chown(dest_fname, o_uid, o_gid, follow_symlinks=False)

            for rel_fname, fname in known_files.items():
                print('Delete: {}'.format(rel_fname))
                os.remove(fname)
            print()
        else:
            print_info('Discarding build environment.')
    else:
        print_info('Can not copy back changes as original package directory is unknown.')


def internal_execute_build(osbase, pkg_dir, build_only=None, *,
                           qa_lintian=False, interact=False, source_pkg_dir=None, buildflags=[]):
    ''' Perform the actual build on an extracted package directory '''
    assert not build_only or isinstance(build_only, str)
    if not pkg_dir:
        raise Exception('Package directory is missing!')
    pkg_dir = os.path.normpath(pkg_dir)

    with osbase.new_instance() as (instance_dir, machine_name):
        # first, check basic requirements

        # instance dir and pkg dir are both temporary directories, so they
        # will be on the same filesystem configured as workspace for debspawn.
        # therefore we only check on directory.
        free_space = get_free_space(instance_dir)
        print_info('Free space in workspace: {}'.format(format_filesize(free_space)))

        # check for at least 512MiB - this is a ridiculously small amount, so the build will likely fail.
        # but with even less, even attempting a build is pointless.
        if (free_space / 2048) < 512:
            print_error('Not enough free space available in workspace.')
            return 8

        # prepare the build. At this point, we only run trusted code and the container
        # has network access
        with temp_dir('pkgsync-' + machine_name) as pkgsync_tmp:
            # create temporary locations set up and APT cache sharing and package injection
            aptcache_tmp = os.path.join(pkgsync_tmp, 'aptcache')
            pkginjector = PackageInjector(osbase)
            if pkginjector.has_injectables():
                pkginjector.create_instance_repo(os.path.join(pkgsync_tmp, 'pkginject'))

            # set up the build environment
            nspawn_flags = ['--bind={}:/srv/build/'.format(pkg_dir)]
            prep_flags = ['--build-prepare']
            if build_only == 'arch':
                prep_flags.append('--arch-only')
            r = nspawn_run_helper_persist(osbase,
                                          instance_dir,
                                          machine_name,
                                          prep_flags,
                                          '/srv',
                                          nspawn_flags=nspawn_flags,
                                          tmp_apt_cache_dir=aptcache_tmp,
                                          pkginjector=pkginjector)
            if r != 0:
                print_error('Build environment setup failed.')
                return False

            # run the actual build. At this point, code is less trusted, and we disable network access.
            nspawn_flags = ['--bind={}:/srv/build/'.format(pkg_dir),
                            '-u', 'builder',
                            '--private-network']
            helper_flags = ['--build-run']
            helper_flags.extend(['--suite', osbase.suite])
            if buildflags:
                helper_flags.append('--buildflags={}'.format(';'.join(buildflags)))
            r = nspawn_run_helper_persist(osbase,
                                          instance_dir,
                                          machine_name,
                                          helper_flags,
                                          '/srv',
                                          nspawn_flags=nspawn_flags,
                                          tmp_apt_cache_dir=aptcache_tmp,
                                          pkginjector=pkginjector,
                                          syscall_filter=osbase.global_config.syscall_filter)
            # exit, unless we are in interactive mode
            if r != 0 and not interact:
                return False

            if qa_lintian and r == 0:
                # running Lintian was requested, so do so.
                # we use Lintian from the container, so we validate with the validator from
                # the OS the package was actually built against
                nspawn_flags = ['--bind={}:/srv/build/'.format(pkg_dir)]
                r = nspawn_run_helper_persist(osbase,
                                              instance_dir,
                                              machine_name,
                                              ['--run-qa', '--lintian'],
                                              '/srv',
                                              nspawn_flags=nspawn_flags,
                                              tmp_apt_cache_dir=aptcache_tmp,
                                              pkginjector=pkginjector)
                if r != 0:
                    print_error('QA failed.')
                    return False
                print()  # extra blank line after Lintian output

            if interact:
                interact_with_build_environment(osbase,
                                                instance_dir,
                                                machine_name,
                                                pkg_dir_root=pkg_dir,
                                                source_pkg_dir=source_pkg_dir,
                                                aptcache_tmp=aptcache_tmp,
                                                pkginjector=pkginjector,
                                                prev_exitcode=r)
                # exit with status of previous exist code
                if r != 0:
                    return False

            build_dir_size = get_tree_size(pkg_dir)
            print_info('This build required {} of dedicated disk space.'.format(format_filesize(build_dir_size)))

    return True


def print_build_detail(osbase, pkgname, version):
    print_info('Package: {}'.format(pkgname))
    print_info('Version: {}'.format(version))
    print_info('Distribution: {}'.format(osbase.suite))
    print_info('Architecture: {}'.format(osbase.arch))
    print_info()


def _read_source_package_details():
    out, err, ret = safe_run(['dpkg-parsechangelog'])
    if ret != 0:
        raise Exception('Running dpkg-parsechangelog failed: {}{}'.format(out, err))

    pkg_sourcename = None
    pkg_version = None
    for line in out.split('\n'):
        if line.startswith('Source: '):
            pkg_sourcename = line[8:].strip()
        elif line.startswith('Version: '):
            pkg_version = line[9:].strip()

    if not pkg_sourcename or not pkg_version:
        print_error('Unable to determine source package name or source package version. Can not continue.')
        return None, None, None

    pkg_version_dsc = version_noepoch(pkg_version)
    dsc_fname = '{}_{}.dsc'.format(pkg_sourcename, pkg_version_dsc)

    return pkg_sourcename, pkg_version, dsc_fname


def _get_build_flags(build_only=None, include_orig=False, maintainer=None,
                     extra_flags: Iterable[str] = None):
    import shlex
    buildflags = []
    extra_flags = listify(extra_flags)

    if build_only:
        if build_only == 'binary':
            buildflags.append('-b')
        elif build_only == 'arch':
            buildflags.append('-B')
        elif build_only == 'indep':
            buildflags.append('-A')
        elif build_only == 'source':
            buildflags.append('-S')
        else:
            print_error('Invalid build-only flag "{}". Can not continue.'.format(build_only))
            return False, []

    if include_orig:
        buildflags.append('-sa')
    if maintainer:
        buildflags.append('-m{}'.format(maintainer.replace(';', ',')))
        buildflags.append('-e{}'.format(maintainer.replace(';', ',')))
    for flag_raw in extra_flags:
        buildflags.extend(shlex.split(flag_raw))

    return True, buildflags


def _retrieve_artifacts(osbase, tmp_dir):
    print_section('Retrieving build artifacts')

    o_uid, o_gid = get_owner_uid_gid()
    acount = 0
    for f in glob(os.path.join(tmp_dir, '*.*')):
        if os.path.isfile(f):
            target_fname = os.path.join(osbase.results_dir, os.path.basename(f))
            shutil.copy2(f, target_fname)
            os.chown(target_fname, o_uid, o_gid, follow_symlinks=False)
            acount += 1
    print_info('Copied {} files.'.format(acount))


def _sign_result(results_dir, spkg_name, spkg_version, build_arch):
    print_section('Signing Package')
    spkg_version_noepoch = version_noepoch(spkg_version)
    changes_basename = '{}_{}_{}.changes'.format(spkg_name, spkg_version_noepoch, build_arch)

    with switch_unprivileged():
        proc = subprocess.run(['debsign', os.path.join(results_dir, changes_basename)], check=False)
        if proc.returncode != 0:
            print_error('Signing failed.')
            return False
    return True


def _print_system_info():
    from . import __version__
    from .utils.misc import current_time_string
    print_info('debspawn {version} on {host} at {time}'.format(version=__version__,
                                                               host=platform.node(),
                                                               time=current_time_string()))


def build_from_directory(osbase, pkg_dir, *,
                         sign=False, build_only=None, include_orig=False, maintainer=None,
                         clean_source=False, qa_lintian=False, interact=False, log_build=True,
                         extra_dpkg_flags: list[str] = None):
    ensure_root()
    osbase.ensure_exists()
    extra_dpkg_flags = listify(extra_dpkg_flags)

    if interact and log_build:
        print_warn('Build log and interactive mode can not be enabled at the same time. Disabling build log.')
        print()
        log_build = False

    # capture console output if we should log the build
    if log_build:
        capture_console_output()

    if not pkg_dir:
        pkg_dir = os.getcwd()
    pkg_dir = os.path.abspath(pkg_dir)

    r, buildflags = _get_build_flags(build_only, include_orig, maintainer, extra_dpkg_flags)
    if not r:
        return False

    _print_system_info()
    print_header('Package build (from directory)')

    print_section('Creating source package')
    with cd(pkg_dir):
        with switch_unprivileged():
            deb_files_fname = os.path.join(pkg_dir, 'debian', 'files')
            if os.path.isfile(deb_files_fname):
                deb_files_fname = None  # the file already existed, we don't need to clean it up later

            pkg_sourcename, pkg_version, dsc_fname = _read_source_package_details()
            if not pkg_sourcename:
                return False

            cmd = ['dpkg-buildpackage', '-S', '--no-sign']
            # d/rules clean requires build dependencies installed if run on the host
            # we avoid that by default, unless explicitly requested
            if not clean_source:
                cmd.append('-nc')

            proc = subprocess.run(cmd, check=False)
            if proc.returncode != 0:
                return False

            # remove d/files file that was created when generating the source package.
            # we only clean up the file if it didn't exist prior to us running the command.
            if deb_files_fname:
                try:
                    os.remove(deb_files_fname)
                except OSError:
                    pass

    print_header('Package build')
    print_build_detail(osbase, pkg_sourcename, pkg_version)

    with temp_dir(pkg_sourcename) as pkg_tmp_dir:
        with cd(pkg_tmp_dir):
            cmd = ['dpkg-source',
                   '-x', os.path.join(pkg_dir, '..', dsc_fname)]
            proc = subprocess.run(cmd, check=False)
            if proc.returncode != 0:
                return False

        ret = internal_execute_build(osbase,
                                     pkg_tmp_dir,
                                     build_only,
                                     qa_lintian=qa_lintian,
                                     interact=interact,
                                     source_pkg_dir=pkg_dir,
                                     buildflags=buildflags)
        if not ret:
            return False

        # copy build results
        _retrieve_artifacts(osbase, pkg_tmp_dir)

    # save buildlog, if we generated one
    log_fname = os.path.join(osbase.results_dir, '{}_{}_{}.buildlog'.format(pkg_sourcename,
                                                                            version_noepoch(pkg_version),
                                                                            osbase.arch))
    save_captured_console_output(log_fname)

    # sign the resulting package
    if sign:
        r = _sign_result(osbase.results_dir, pkg_sourcename, pkg_version, osbase.arch)
        if not r:
            return False

    print_info('Done.')

    return True


def build_from_dsc(osbase, dsc_fname, *,
                   sign=False, build_only=None, include_orig=False, maintainer=None,
                   qa_lintian=False, interact=False, log_build=True,
                   extra_dpkg_flags: Iterable[str] = None):
    ensure_root()
    osbase.ensure_exists()
    extra_dpkg_flags = listify(extra_dpkg_flags)

    if interact and log_build:
        print_warn('Build log and interactive mode can not be enabled at the same time. Disabling build log.')
        print()
        log_build = False

    # capture console output if we should log the build
    if log_build:
        capture_console_output()

    r, buildflags = _get_build_flags(build_only, include_orig, maintainer, extra_dpkg_flags)
    if not r:
        return False

    _print_system_info()

    dsc_fname = os.path.abspath(os.path.normpath(dsc_fname))
    tmp_prefix = os.path.basename(dsc_fname).replace('.dsc', '').replace(' ', '-')
    with temp_dir(tmp_prefix) as pkg_tmp_dir:
        with cd(pkg_tmp_dir):
            cmd = ['dpkg-source',
                   '-x', dsc_fname]
            proc = subprocess.run(cmd, check=False)
            if proc.returncode != 0:
                return False

            pkg_srcdir = None
            for f in glob('./*'):
                if os.path.isdir(f):
                    pkg_srcdir = f
                    break
            if not pkg_srcdir:
                print_error('Unable to find source directory of extracted package.')
                return False

            with cd(pkg_srcdir):
                pkg_sourcename, pkg_version, dsc_fname = _read_source_package_details()
                if not pkg_sourcename:
                    return False

            print_header('Package build')
            print_build_detail(osbase, pkg_sourcename, pkg_version)

        ret = internal_execute_build(osbase,
                                     pkg_tmp_dir,
                                     build_only,
                                     qa_lintian=qa_lintian,
                                     interact=interact,
                                     buildflags=buildflags)
        if not ret:
            return False

        # copy build results
        _retrieve_artifacts(osbase, pkg_tmp_dir)

    # save buildlog, if we generated one
    log_fname = os.path.join(osbase.results_dir, '{}_{}_{}.buildlog'.format(pkg_sourcename,
                                                                            version_noepoch(pkg_version),
                                                                            osbase.arch))
    save_captured_console_output(log_fname)

    # sign the resulting package
    if sign:
        r = _sign_result(osbase.results_dir, pkg_sourcename, pkg_version, osbase.arch)
        if not r:
            return False

    print_info('Done.')
    return True
