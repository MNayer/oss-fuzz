#!/usr/bin/env python
# Copyright 2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################
"""Helper script for OSS-Fuzz users. Can do common tasks like building
projects/fuzzers, running them etc."""

from __future__ import print_function
from multiprocessing.dummy import Pool as ThreadPool
import argparse
import datetime
import errno
import logging
import os
import pipes
import re
import subprocess
import sys
import templates
import time

import constants

DOCKER_TIMEOUT = 4          # Timeout value
DOCKER_TIMEOUT_UNIT = "h"   # Timeout unit
DOCKER_MEMLIMIT = "4g"     # Memory limit for each docker container
OSS_FUZZ_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BUILD_DIR = os.path.join(OSS_FUZZ_DIR, 'build')

BASE_RUNNER_IMAGE = 'gcr.io/oss-fuzz-base/base-runner'

BASE_IMAGES = {
    'generic': [
        'gcr.io/oss-fuzz-base/base-image',
        'gcr.io/oss-fuzz-base/base-clang',
        'gcr.io/oss-fuzz-base/base-builder',
        BASE_RUNNER_IMAGE,
        'gcr.io/oss-fuzz-base/base-runner-debug',
    ],
    'go': ['gcr.io/oss-fuzz-base/base-builder-go'],
    'jvm': ['gcr.io/oss-fuzz-base/base-builder-jvm'],
    'python': ['gcr.io/oss-fuzz-base/base-builder-python'],
    'rust': ['gcr.io/oss-fuzz-base/base-builder-rust'],
    'swift': ['gcr.io/oss-fuzz-base/base-builder-swift'],
}

VALID_PROJECT_NAME_REGEX = re.compile(r'^[a-zA-Z0-9_-]+$')
MAX_PROJECT_NAME_LENGTH = 26

CORPUS_URL_FORMAT = (
    'gs://{project_name}-corpus.clusterfuzz-external.appspot.com/libFuzzer/'
    '{fuzz_target}/')
CORPUS_BACKUP_URL_FORMAT = (
    'gs://{project_name}-backup.clusterfuzz-external.appspot.com/corpus/'
    'libFuzzer/{fuzz_target}/')

LANGUAGE_REGEX = re.compile(r'[^\s]+')
PROJECT_LANGUAGE_REGEX = re.compile(r'\s*language\s*:\s*([^\s]+)')

WORKDIR_REGEX = re.compile(r'\s*WORKDIR\s*([^\s]+)')

LANGUAGES_WITH_BUILDER_IMAGES = {'go', 'jvm', 'python', 'rust', 'swift'}

if sys.version_info[0] >= 3:
  raw_input = input  # pylint: disable=invalid-name

# pylint: disable=too-many-lines


class Project:
  """Class representing a project that is in OSS-Fuzz or an external project
  (ClusterFuzzLite user)."""

  def __init__(
      self,
      project_name_or_path,
      commit='',
      is_external=False,
      build_integration_path=constants.DEFAULT_EXTERNAL_BUILD_INTEGRATION_PATH):
    self.is_external = is_external
    self.commit = commit
    self.out_directory = None
    if self.is_external:
      self.path = os.path.abspath(project_name_or_path)
      self.name = os.path.basename(self.path)
      self.build_integration_path = os.path.join(self.path,
                                                 build_integration_path)
    else:
      self.name = project_name_or_path
      self.path = os.path.join(OSS_FUZZ_DIR, 'projects', self.name)
      self.build_integration_path = self.path

  @property
  def dockerfile_path(self):
    """Returns path to the project Dockerfile."""
    return os.path.join(self.build_integration_path, 'Dockerfile')

  @property
  def language(self):
    """Returns project language."""
    if self.is_external:
      # TODO(metzman): Handle this properly.
      return constants.DEFAULT_LANGUAGE

    project_yaml_path = os.path.join(self.path, 'project.yaml')
    with open(project_yaml_path) as file_handle:
      content = file_handle.read()
      for line in content.splitlines():
        match = PROJECT_LANGUAGE_REGEX.match(line)
        if match:
          return match.group(1)

    logging.warning('Language not specified in project.yaml.')
    return None

  @property
  def out(self):
    """Returns the out dir for the project. Creates it if needed."""
    return self.out_directory if self.out_directory != None else _get_out_dir(self.name, self.commit)

  @property
  def work(self):
    """Returns the out dir for the project. Creates it if needed."""
    return _get_project_build_subdir(self.name, 'work')

  @property
  def corpus(self):
    """Returns the out dir for the project. Creates it if needed."""
    return _get_project_build_subdir(self.name, 'corpus')


def main():  # pylint: disable=too-many-branches,too-many-return-statements
  """Gets subcommand from program arguments and does it. Returns 0 on success 1
  on error."""
  logging.basicConfig(level=logging.INFO)

  parser = get_parser()
  args = parse_args(parser)

  # Need to do this before chdir.
  # TODO(https://github.com/google/oss-fuzz/issues/6758): Get rid of chdir.
  if hasattr(args, 'testcase_path'):
    args.testcase_path = _get_absolute_path(args.testcase_path)
  # Note: this has to happen after parse_args above as parse_args needs to know
  # the original CWD for external projects.
  os.chdir(OSS_FUZZ_DIR)
  if not os.path.exists(BUILD_DIR):
    os.mkdir(BUILD_DIR)

  # We have different default values for `sanitizer` depending on the `engine`.
  # Some commands do not have `sanitizer` argument, so `hasattr` is necessary.
  if hasattr(args, 'sanitizer') and not args.sanitizer:
    if args.engine == 'dataflow':
      args.sanitizer = 'dataflow'
    else:
      args.sanitizer = constants.DEFAULT_SANITIZER

  if args.command == 'generate':
    result = generate(args)
  elif args.command == 'build_image':
    result = build_image(args)
  elif args.command == 'build_fuzzers':
    result = build_fuzzers(args)
  elif args.command == 'check_build':
    result = check_build(args)
  elif args.command == 'download_corpora':
    result = download_corpora(args)
  elif args.command == 'run_fuzzer':
    result = run_fuzzer(args)
  elif args.command == 'coverage':
    result = coverage(args)
  elif args.command == 'reproduce':
    result = reproduce(args)
  elif args.command == 'shell':
    result = shell(args)
  elif args.command == 'pull_images':
    result = pull_images()
  else:
    # Print help string if no arguments provided.
    parser.print_help()
    result = False
  return bool_to_retcode(result)


def bool_to_retcode(boolean):
  """Returns 0 if |boolean| is Truthy, 0 is the standard return code for a
  successful process execution. Returns 1 otherwise, indicating the process
  failed."""
  return 0 if boolean else 1


def parse_args(parser, args=None):
  """Parses |args| using |parser| and returns parsed args. Also changes
  |args.build_integration_path| to have correct default behavior."""
  # Use default argument None for args so that in production, argparse does its
  # normal behavior, but unittesting is easier.
  parsed_args = parser.parse_args(args)
  project = getattr(parsed_args, 'project', None)
  if not project:
    return parsed_args

  # Use hacky method for extracting attributes so that ShellTest works.
  # TODO(metzman): Fix this.
  is_external = getattr(parsed_args, 'external', False)
  parsed_args.project = Project(parsed_args.project, parsed_args.commit, is_external)
  return parsed_args


def _add_external_project_args(parser):
  parser.add_argument(
      '--external',
      help='Is project external?',
      default=False,
      action='store_true',
  )


def get_parser():  # pylint: disable=too-many-statements
  """Returns an argparse parser."""
  parser = argparse.ArgumentParser('helper.py', description='oss-fuzz helpers')
  subparsers = parser.add_subparsers(dest='command')

  generate_parser = subparsers.add_parser(
      'generate', help='Generate files for new project.')
  generate_parser.add_argument('project')
  generate_parser.add_argument(
      '--language',
      default=constants.DEFAULT_LANGUAGE,
      choices=['c', 'c++', 'rust', 'go', 'jvm', 'swift', 'python'],
      help='Project language.')
  _add_external_project_args(generate_parser)

  build_image_parser = subparsers.add_parser('build_image',
                                             help='Build an image.')
  build_image_parser.add_argument('project')
  build_image_parser.add_argument('--pull',
                                  action='store_true',
                                  help='Pull latest base image.')
  build_image_parser.add_argument('--commit',
                                    help='project commit to rollback to',
                                    default="")
  build_image_parser.add_argument('--cache',
                                  action='store_true',
                                  default=False,
                                  help='Use docker cache when building image.')
  build_image_parser.add_argument('--no-pull',
                                  action='store_true',
                                  help='Do not pull latest base image.')
  _add_external_project_args(build_image_parser)

  build_fuzzers_parser = subparsers.add_parser(
      'build_fuzzers', help='Build fuzzers for a project.')
  _add_architecture_args(build_fuzzers_parser)
  _add_engine_args(build_fuzzers_parser)
  _add_sanitizer_args(build_fuzzers_parser)
  _add_environment_args(build_fuzzers_parser)
  _add_external_project_args(build_fuzzers_parser)
  build_fuzzers_parser.add_argument('project')
  build_fuzzers_parser.add_argument('source_path',
                                    help='path of local source',
                                    nargs='?')
  build_fuzzers_parser.add_argument('--commit',
                                    help='project commit to rollback to',
                                    default='')
  build_fuzzers_parser.add_argument('--aflgo_mode',
                                    help='aflgo\'s operation mode, if engine "aflgo" has been selected',
                                    default='',
                                    choices=constants.AFLGO_MODES)
  build_fuzzers_parser.add_argument('--aflgo_targets',
                                    help='if alfgo "target" mode has been selected, this comma seperated list of targets will be used. Example "src/readelf.c:25,src/parse.c:133"',
                                    default='')
  build_fuzzers_parser.add_argument('--fuzztarget',
                                    help='fuzzing target to be built',
                                    default='')
  build_fuzzers_parser.add_argument('--noinst',
                                    dest='noinst',
                                    action='store_true',
                                    default=False,
                                    help='build target without sanitizers and fuzzing instrumentation (libfuzzer, C/C++)')
  build_fuzzers_parser.add_argument('--savesource',
                                    dest='savesource',
                                    action='store_true',
                                    default=False,
                                    help='save the source code file (C/C++) in output directory')
  build_fuzzers_parser.add_argument('--savetemps',
                                    dest='savetemps',
                                    action='store_true',
                                    default=False,
                                    help='save the source code file (C/C++) in output directory')
  build_fuzzers_parser.add_argument('--coverage',
                                    dest='coverage',
                                    action='store_true',
                                    default=False,
                                    help='build fuzzing target with source coverage support')
  build_fuzzers_parser.add_argument('--cpus',
                                    dest='cpus',
                                    type=float,
                                    default=0,
                                    help='maximum number of cpu cores to use for building the fuzzers')
  build_fuzzers_parser.add_argument('--dwarf',
                                    dest='dwarf_version',
                                    type=int,
                                    default=5,
                                    help='build target using DWARFx debugging information')
  build_fuzzers_parser.add_argument('--graph',
                                    dest='graph_plugin',
                                    action='store_true',
                                    default=False,
                                    help='enable GraphExtractionPlugin when building the target')
  build_fuzzers_parser.add_argument('--out_directory',
                                    dest='out_directory',
                                    default=None,
                                    help='overwrite default out directory')
  build_fuzzers_parser.add_argument('--debug',
                                    dest='debug',
                                    action='store_true',
                                    default=False,
                                    help='enable debug mode (bash instead of compile as docker CMD)')
  build_fuzzers_parser.add_argument('--mount_path',
                                    dest='mount_path',
                                    help='path to mount local source in '
                                    '(defaults to WORKDIR)')
  build_fuzzers_parser.add_argument('--clean',
                                    dest='clean',
                                    action='store_true',
                                    help='clean existing artifacts.')
  build_fuzzers_parser.add_argument('--no-clean',
                                    dest='clean',
                                    action='store_false',
                                    help='do not clean existing artifacts '
                                    '(default).')
  build_fuzzers_parser.set_defaults(clean=False)

  check_build_parser = subparsers.add_parser(
      'check_build', help='Checks that fuzzers execute without errors.')
  _add_architecture_args(check_build_parser)
  _add_engine_args(check_build_parser, choices=constants.ENGINES)
  _add_sanitizer_args(check_build_parser, choices=constants.SANITIZERS)
  _add_environment_args(check_build_parser)
  check_build_parser.add_argument('project',
                                  help='name of the project or path (external)')
  check_build_parser.add_argument('fuzzer_name',
                                  help='name of the fuzzer',
                                  nargs='?')
  _add_external_project_args(check_build_parser)

  run_fuzzer_parser = subparsers.add_parser(
      'run_fuzzer', help='Run a fuzzer in the emulated fuzzing environment.')
  _add_engine_args(run_fuzzer_parser)
  _add_sanitizer_args(run_fuzzer_parser)
  _add_environment_args(run_fuzzer_parser)
  _add_external_project_args(run_fuzzer_parser)
  run_fuzzer_parser.add_argument('--commit',
                                    help='project commit to rollback to',
                                    default="")
  run_fuzzer_parser.add_argument('--timeout',
                                    help='time to fuzz the target (e. g. 30s, 60m, 2h, etc.)',
                                    default="60m")
  run_fuzzer_parser.add_argument('--aflgo_exploitation',
                                    help='time to exploitation for aflgo (e. g. 30s, 60m, 2h, etc.)',
                                    default="")
  run_fuzzer_parser.add_argument('--aflgo_disable_directed',
                                    action='store_true',
                                    help='disable directed fuzzing (use coverage guided fuzzing only)',
                                    default=False)
  run_fuzzer_parser.add_argument('--apptainer',
                                    help='use apptainer and the specifiec apptainer image instead of docker',
                                    default="")
  run_fuzzer_parser.add_argument('--debug',
                                    dest='debug',
                                    action='store_true',
                                    default=False,
                                    help='enable debug mode (bash instead of run_fuzzer as docker CMD)')
  run_fuzzer_parser.add_argument('--out_directory',
                                    dest='out_directory',
                                    default=None,
                                    help='overwrite default out directory')
  run_fuzzer_parser.add_argument('--fuzzer_out_directory',
                                    dest='fuzzer_out_directory',
                                    default=None,
                                    help='overwrite default fuzzer out directory')
  run_fuzzer_parser.add_argument('--functions_directory',
                                    dest='functions_directory',
                                    default=None,
                                    help='functions directory')
  run_fuzzer_parser.add_argument('--const_vscore',
                                    dest='const_vscore',
                                    action='store_true',
                                    default=False,
                                    help='use constant vscore (if vscore is used)')
  run_fuzzer_parser.add_argument('--vscore_reduce',
                                    dest='vscore_reduce',
                                    default=None,
                                    help='vscore reduce function (e. g. mean, max, q75)')
  run_fuzzer_parser.add_argument('--mem_limit',
                                    help='memory limit (in mb) for each run',
                                    default="")
  run_fuzzer_parser.add_argument(
      '--corpus-dir', help='directory to store corpus for the fuzz target')
  run_fuzzer_parser.add_argument('project',
                                 help='name of the project or path (external)')
  run_fuzzer_parser.add_argument('fuzzer_name', help='name of the fuzzer')
  run_fuzzer_parser.add_argument('fuzzer_args',
                                 help='arguments to pass to the fuzzer',
                                 nargs='*')

  coverage_parser = subparsers.add_parser(
      'coverage', help='Generate code coverage report for the project.')
  coverage_parser.add_argument('--no-corpus-download',
                               action='store_true',
                               help='do not download corpus backup from '
                               'OSS-Fuzz; use corpus located in '
                               'build/corpus/<project>/<fuzz_target>/')
  coverage_parser.add_argument('--port',
                               default='8008',
                               help='specify port for'
                               ' a local HTTP server rendering coverage report')
  coverage_parser.add_argument('--out_directory',
                                dest='out_directory',
                                default=None,
                                help='overwrite default out directory')
  coverage_parser.add_argument('--commit',
                                help='project commit to rollback to',
                                default="")
  coverage_parser.add_argument('--debug',
                                dest='debug',
                                action='store_true',
                                default=False,
                                help='enable debug mode (bash instead of coverage as docker CMD)')
  coverage_parser.add_argument('--fuzz-target',
                               help='specify name of a fuzz '
                               'target to be run for generating coverage '
                               'report')
  coverage_parser.add_argument('--corpus-dir',
                               help='specify location of corpus'
                               ' to be used (requires --fuzz-target argument)')
  coverage_parser.add_argument('project',
                               help='name of the project or path (external)')
  coverage_parser.add_argument('extra_args',
                               help='additional arguments to '
                               'pass to llvm-cov utility.',
                               nargs='*')
  _add_external_project_args(coverage_parser)

  download_corpora_parser = subparsers.add_parser(
      'download_corpora', help='Download all corpora for a project.')
  download_corpora_parser.add_argument('--fuzz-target',
                                       help='specify name of a fuzz target')
  download_corpora_parser.add_argument(
      'project', help='name of the project or path (external)')

  reproduce_parser = subparsers.add_parser('reproduce',
                                           help='Reproduce a crash.')
  reproduce_parser.add_argument('--valgrind',
                                action='store_true',
                                help='run with valgrind')
  reproduce_parser.add_argument('--debug',
                                dest='debug',
                                action='store_true',
                                default=False,
                                help='enable debug mode (bash instead of compile as docker CMD)')
  reproduce_parser.add_argument('--out_directory',
                                    dest='out_directory',
                                    default=None,
                                    help='overwrite default out directory')
  reproduce_parser.add_argument('--commit',
                                    help='project commit to rollback to',
                                    default="")
  reproduce_parser.add_argument('project',
                                help='name of the project or path (external)')
  reproduce_parser.add_argument('fuzzer_name', help='name of the fuzzer')
  reproduce_parser.add_argument('testcase_path', help='path of local testcase')
  reproduce_parser.add_argument('fuzzer_args',
                                help='arguments to pass to the fuzzer',
                                nargs='*')
  _add_environment_args(reproduce_parser)
  _add_external_project_args(reproduce_parser)

  shell_parser = subparsers.add_parser(
      'shell', help='Run /bin/bash within the builder container.')
  shell_parser.add_argument('--commit',
                                    help='project commit to rollback to',
                                    default="")
  shell_parser.add_argument('project',
                            help='name of the project or path (external)')
  shell_parser.add_argument('source_path',
                            help='path of local source',
                            nargs='?')
  _add_architecture_args(shell_parser)
  _add_engine_args(shell_parser)
  _add_sanitizer_args(shell_parser)
  _add_environment_args(shell_parser)
  _add_external_project_args(shell_parser)

  subparsers.add_parser('pull_images', help='Pull base images.')
  return parser


def is_base_image(image_name):
  """Checks if the image name is a base image."""
  return os.path.exists(os.path.join('infra', 'base-images', image_name))


def check_project_exists(project):
  """Checks if a project exists."""
  if os.path.exists(project.path):
    return True

  if project.is_external:
    descriptive_project_name = project.path
  else:
    descriptive_project_name = project.name

  logging.error('"%s" does not exist.', descriptive_project_name)
  return False


def _check_fuzzer_exists(project, fuzzer_name):
  """Checks if a fuzzer exists."""
  fuzzer_path = os.path.join(project.out, fuzzer_name)
  if os.path.exists(fuzzer_path) and os.path.isfile(fuzzer_path):
    return True
  logging.error('%s does not seem to exist. Please run build_fuzzers first.',
                fuzzer_name)
  return False
  #command = ['docker', 'run', '--rm']
  #command.extend(['-v', '%s:/out' % project.out])
  #command.append(BASE_RUNNER_IMAGE)
  #command.extend(['/bin/bash', '-c', 'test -f /out/%s' % fuzzer_name])

  #try:
  #  subprocess.check_call(command)
  #except subprocess.CalledProcessError:
  #  logging.error('%s does not seem to exist. Please run build_fuzzers first.',
  #                fuzzer_name)
  #  return False

  #return True


def _get_absolute_path(path):
  """Returns absolute path with user expansion."""
  return os.path.abspath(os.path.expanduser(path))


def _get_command_string(command):
  """Returns a shell escaped command string."""
  return ' '.join(pipes.quote(part) for part in command)


def _get_project_build_subdir(project, subdir_name, commit=''):
  """Creates the |subdir_name| subdirectory of the |project| subdirectory in
  |BUILD_DIR| and returns its path."""
  if commit == '':
    directory = os.path.join(BUILD_DIR, subdir_name, project)
  else:
    directory = os.path.join(BUILD_DIR, subdir_name, '%s_%s' % (project, commit))
  if not os.path.exists(directory):
    os.makedirs(directory)

  return directory


def _get_out_dir(project='', commit=''):
  """Creates and returns path to /out directory for the given project (if
  specified)."""
  return _get_project_build_subdir(project, 'out', commit)


def _add_architecture_args(parser, choices=None):
  """Adds common architecture args."""
  if choices is None:
    choices = constants.ARCHITECTURES
  parser.add_argument('--architecture',
                      default=constants.DEFAULT_ARCHITECTURE,
                      choices=choices)


def _add_engine_args(parser, choices=None):
  """Adds common engine args."""
  if choices is None:
    choices = constants.ENGINES
  parser.add_argument('--engine',
                      default=constants.DEFAULT_ENGINE,
                      choices=choices)


def _add_sanitizer_args(parser, choices=None):
  """Adds common sanitizer args."""
  if choices is None:
    choices = constants.SANITIZERS
  parser.add_argument(
      '--sanitizer',
      default=None,
      choices=choices,
      help='the default is "address"; "dataflow" for "dataflow" engine')


def _add_environment_args(parser):
  """Adds common environment args."""
  parser.add_argument('-e',
                      action='append',
                      help="set environment variable e.g. VAR=value")


def build_image_impl(project, commit, cache=True, pull=False):
  """Builds image."""
  image_name = project.name

  if is_base_image(image_name):
    image_project = 'oss-fuzz-base'
    docker_build_dir = os.path.join(OSS_FUZZ_DIR, 'infra', 'base-images',
                                    image_name)
    dockerfile_path = os.path.join(docker_build_dir, 'Dockerfile')
  else:
    if not check_project_exists(project):
      return False
    dockerfile_path = project.dockerfile_path
    docker_build_dir = project.path
    image_project = 'oss-fuzz'

  if pull and not pull_images(project.language):
    return False

  build_args = ['--network', 'host']
  if not cache:
    build_args.append('--no-cache')

  # Get fully qualified project name
  if commit == '':
    fq_project_name = 'gcr.io/%s/%s' % (image_project, image_name)
  else:
    fq_project_name = 'gcr.io/%s/%s_%s' % (image_project, image_name, commit)

  build_args += [
      '-t',
      fq_project_name, '--file', dockerfile_path
  ]
  build_args.append(docker_build_dir)
  return docker_build(build_args)


def _env_to_docker_args(env_list):
  """Turns envirnoment variable list into docker arguments."""
  return sum([['-e', v] for v in env_list], [])


def _env_to_apptainer_args(env_list):
  """Turns envirnoment variable list into apptainer arguments."""
  return sum([['--env', v] for v in env_list], [])


def workdir_from_lines(lines, default='/src'):
  """Gets the WORKDIR from the given lines."""
  for line in reversed(lines):  # reversed to get last WORKDIR.
    match = re.match(WORKDIR_REGEX, line)
    if match:
      workdir = match.group(1)
      workdir = workdir.replace('$SRC', '/src')

      if not os.path.isabs(workdir):
        workdir = os.path.join('/src', workdir)

      return os.path.normpath(workdir)

  return default


def _workdir_from_dockerfile(project):
  """Parses WORKDIR from the Dockerfile for the given project."""
  with open(project.dockerfile_path) as file_handle:
    lines = file_handle.readlines()

  return workdir_from_lines(lines, default=os.path.join('/src', project.name))


def docker_run(run_args, print_output=True, with_timeout=False):
  """Calls `docker run`."""
  command = ['docker', 'run', '--rm', '--privileged']

  # Support environments with a TTY.
  if sys.stdin.isatty():
    command.append('-i')

  command.extend(run_args)

  logging.info('Running: %s.', _get_command_string(command))
  stdout = None
  if not print_output:
    stdout = open(os.devnull, 'w')

  try:
    subprocess.check_call(command, stdout=stdout, stderr=subprocess.STDOUT)
  except subprocess.CalledProcessError as err:
    if with_timeout and err.returncode == 124:
        # exit code 124 means command did run, but timed out
        return True
    return False

  return True


def apptainer_run(run_args, print_output=True, with_timeout=False):
  """Calls `apptainer run`."""
  command = ['apptainer', 'run']

  command.extend(run_args)

  logging.info('Running: %s.', _get_command_string(command))
  stdout = None
  if not print_output:
    stdout = open(os.devnull, 'w')

  try:
    subprocess.check_call(command, stdout=stdout, stderr=subprocess.STDOUT)
  except subprocess.CalledProcessError as err:
    if with_timeout and err.returncode == 124:
        # exit code 124 means command did run, but timed out
        return True
    return False

  return True


def docker_build(build_args):
  """Calls `docker build`."""
  command = ['docker', 'build']
  command.extend(build_args)
  logging.info('Running: %s.', _get_command_string(command))

  try:
    subprocess.check_call(command)
  except subprocess.CalledProcessError:
    logging.error('Docker build failed.')
    return False

  return True


def docker_pull(image):
  """Call `docker pull`."""
  command = ['docker', 'pull', image]
  logging.info('Running: %s', _get_command_string(command))

  try:
    subprocess.check_call(command)
  except subprocess.CalledProcessError:
    logging.error('Docker pull failed.')
    return False

  return True


def build_image(args):
  """Builds docker image."""
  if args.pull and args.no_pull:
    logging.error('Incompatible arguments --pull and --no-pull.')
    return False

  if args.pull:
    pull = True
  elif args.no_pull:
    pull = False
  else:
    y_or_n = raw_input('Pull latest base images (compiler/runtime)? (y/N): ')
    pull = y_or_n.lower() == 'y'

  if pull:
    logging.info('Pulling latest base images...')
  else:
    logging.info('Using cached base images...')

  # If build_image is called explicitly, don't use cache.
  if build_image_impl(args.project, args.commit, cache=args.cache, pull=pull):
    return True

  return False


def build_fuzzers_impl(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
    project,
    clean,
    engine,
    sanitizer,
    architecture,
    env_to_add,
    source_path,
    commit,
    fuzztarget,
    aflgo_mode,
    aflgo_targets,
    noinst,
    savesource,
    savetemps,
    coverage,
    cpus,
    dwarf_version,
    graph_plugin,
    debug,
    out_directory,
    mount_path=None):
  """Builds fuzzers."""
  if out_directory:
      project.out_directory = out_directory
  if not build_image_impl(project, commit):
    return False

  if engine == 'aflgo':
      assert(aflgo_mode in constants.AFLGO_MODES)
      if aflgo_mode == 'targets':
          assert(isinstance(aflgo_targets, str) and len(aflgo_targets) > 0)

  if clean:
    logging.info('Cleaning existing build artifacts.')
  
    # Get fully qualified project name
    if commit == '':
      fq_project_name = 'gcr.io/oss-fuzz/%s' % project.name
    else:
      fq_project_name = 'gcr.io/oss-fuzz/%s_%s' % (project.name, commit)

    # Clean old and possibly conflicting artifacts in project's out directory.
    docker_run([
        '-m', DOCKER_MEMLIMIT,
        '-v',
        '%s:/out' % project.out, '-t',
        fq_project_name, 'timeout', '-k', '120', f'{DOCKER_TIMEOUT}{DOCKER_TIMEOUT_UNIT}', '/bin/bash', '-c', 'rm -rf /out/*'
    ])

    docker_run([
        '-m', DOCKER_MEMLIMIT,
        '-v',
        '%s:/work' % project.work, '-t',
        fq_project_name, 'timeout', '-k', '120', f'{DOCKER_TIMEOUT}{DOCKER_TIMEOUT_UNIT}', '/bin/bash', '-c', 'rm -rf /work/*'
    ])

  else:
    logging.info('Keeping existing build artifacts as-is (if any).')

  env = [
      'FUZZING_ENGINE=' + engine,
      'SANITIZER=' + sanitizer,
      'ARCHITECTURE=' + architecture,
      'PROJECT=' + project.name,
      'COMMIT=' + commit,
      'FUZZTARGET=' + fuzztarget,
      'AFLGO_MODE=' + aflgo_mode,
      'TARGETS=' + aflgo_targets,
      'NOINST=' + ("1" if noinst else ""),
      'SAVESOURCE=' + ("1" if savesource else ""),
      'SAVETEMPS=' + ("1" if savetemps else ""),
      'COVERAGE=' + ("1" if coverage else ""),
      'DWARF=%d' % dwarf_version,
      'GRAPHPLUGIN=' + ("1" if graph_plugin else ""),
  ]

  _add_oss_fuzz_ci_if_needed(env)

  if project.language:
    env.append('FUZZING_LANGUAGE=' + project.language)

  if env_to_add:
    env += env_to_add

  command = _env_to_docker_args(env)
  if source_path:
    workdir = _workdir_from_dockerfile(project)
    if mount_path:
      command += [
          '-v',
          '%s:%s' % (_get_absolute_path(source_path), mount_path),
      ]
    else:
      if workdir == '/src':
        logging.error('Cannot use local checkout with "WORKDIR: /src".')
        return False

      command += [
          '-v',
          '%s:%s' % (_get_absolute_path(source_path), workdir),
      ]

  # Get fully qualified project name
  if commit == '':
    fq_project_name = 'gcr.io/oss-fuzz/%s' % project.name
  else:
    fq_project_name = 'gcr.io/oss-fuzz/%s_%s' % (project.name, commit)

  if debug:
    command += ['-ti']

  if cpus > 0:
    command += ['--cpus', '%f' % cpus]

  command += [
      '-m', DOCKER_MEMLIMIT,
      '--network', 'host',
      '-v',
      '%s:/out' % out_directory, '-v',
      '%s:/work' % project.work, '-t',
      fq_project_name,
      'timeout', '-k', '120', '-s', 'KILL', f'{DOCKER_TIMEOUT}{DOCKER_TIMEOUT_UNIT}',
      'compile' if not debug else 'bash',
  ]
  print(command)

  print("compile", time.time())
  result = docker_run(command)
  print("compile", time.time())
  if not result:
    logging.error('Building fuzzers failed.')
    return False

  return True


def build_fuzzers(args):
  """Builds fuzzers."""
  return build_fuzzers_impl(args.project,
                            args.clean,
                            args.engine,
                            args.sanitizer,
                            args.architecture,
                            args.e,
                            args.source_path,
                            args.commit,
                            args.fuzztarget,
                            args.aflgo_mode,
                            args.aflgo_targets,
                            args.noinst,
                            args.savesource,
                            args.savetemps,
                            args.coverage,
                            args.cpus,
                            args.dwarf_version,
                            args.graph_plugin,
                            args.debug,
                            args.out_directory,
                            mount_path=args.mount_path)


def _add_oss_fuzz_ci_if_needed(env):
  """Adds value of |OSS_FUZZ_CI| environment variable to |env| if it is set."""
  oss_fuzz_ci = os.getenv('OSS_FUZZ_CI')
  if oss_fuzz_ci:
    env.append('OSS_FUZZ_CI=' + oss_fuzz_ci)


def check_build(args):
  """Checks that fuzzers in the container execute without errors."""
  if not check_project_exists(args.project):
    return False

  if (args.fuzzer_name and
      not _check_fuzzer_exists(args.project, args.fuzzer_name)):
    return False

  fuzzing_language = args.project.language
  if not fuzzing_language:
    fuzzing_language = constants.DEFAULT_LANGUAGE
    logging.warning('Language not specified in project.yaml. Defaulting to %s.',
                    fuzzing_language)

  env = [
      'FUZZING_ENGINE=' + args.engine,
      'SANITIZER=' + args.sanitizer,
      'ARCHITECTURE=' + args.architecture,
      'FUZZING_LANGUAGE=' + fuzzing_language,
  ]
  _add_oss_fuzz_ci_if_needed(env)
  if args.e:
    env += args.e

  run_args = _env_to_docker_args(env) + [
      '-m', DOCKER_MEMLIMIT,
      '-v',
      '%s:/out' % args.project.out, '-t', 'gcr.io/oss-fuzz-base/base-runner',
      'timeout', '-k', '120', f'{DOCKER_TIMEOUT}{DOCKER_TIMEOUT_UNIT}',
  ]

  if args.fuzzer_name:
    run_args += ['test_one.py', args.fuzzer_name]
  else:
    run_args.append('test_all.py')

  result = docker_run(run_args)
  if result:
    logging.info('Check build passed.')
  else:
    logging.error('Check build failed.')

  return result


def _get_fuzz_targets(project):
  """Returns names of fuzz targest build in the project's /out directory."""
  fuzz_targets = []
  for name in os.listdir(project.out):
    if name.startswith('afl-'):
      continue
    if name.startswith('jazzer_'):
      continue
    if name == 'llvm-symbolizer':
      continue

    path = os.path.join(project.out, name)
    # Python and JVM fuzz targets are only executable for the root user, so
    # we can't use os.access.
    if os.path.isfile(path) and (os.stat(path).st_mode & 0o111):
      fuzz_targets.append(name)

  return fuzz_targets


def _get_latest_corpus(project, fuzz_target, base_corpus_dir):
  """Downloads the latest corpus for the given fuzz target."""
  corpus_dir = os.path.join(base_corpus_dir, fuzz_target)
  if not os.path.exists(corpus_dir):
    os.makedirs(corpus_dir)

  if not fuzz_target.startswith(project.name + '_'):
    fuzz_target = '%s_%s' % (project.name, fuzz_target)

  corpus_backup_url = CORPUS_BACKUP_URL_FORMAT.format(project_name=project.name,
                                                      fuzz_target=fuzz_target)
  command = ['gsutil', 'ls', corpus_backup_url]

  # Don't capture stderr. We want it to print in real time, in case gsutil is
  # asking for two-factor authentication.
  corpus_listing = subprocess.Popen(command, stdout=subprocess.PIPE)
  output, _ = corpus_listing.communicate()

  # Some fuzz targets (e.g. new ones) may not have corpus yet, just skip those.
  if corpus_listing.returncode:
    logging.warning('Corpus for %s not found:\n', fuzz_target)
    return

  if output:
    latest_backup_url = output.splitlines()[-1]
    archive_path = corpus_dir + '.zip'
    command = ['gsutil', '-q', 'cp', latest_backup_url, archive_path]
    subprocess.check_call(command)

    command = ['unzip', '-q', '-o', archive_path, '-d', corpus_dir]
    subprocess.check_call(command)
    os.remove(archive_path)
  else:
    # Sync the working corpus copy if a minimized backup is not available.
    corpus_url = CORPUS_URL_FORMAT.format(project_name=project.name,
                                          fuzz_target=fuzz_target)
    command = ['gsutil', '-m', '-q', 'rsync', '-R', corpus_url, corpus_dir]
    subprocess.check_call(command)


def download_corpora(args):
  """Downloads most recent corpora from GCS for the given project."""
  if not check_project_exists(args.project):
    return False

  try:
    with open(os.devnull, 'w') as stdout:
      subprocess.check_call(['gsutil', '--version'], stdout=stdout)
  except OSError:
    logging.error('gsutil not found. Please install it from '
                  'https://cloud.google.com/storage/docs/gsutil_install')
    return False

  if args.fuzz_target:
    fuzz_targets = [args.fuzz_target]
  else:
    fuzz_targets = _get_fuzz_targets(args.project)

  corpus_dir = args.project.corpus

  def _download_for_single_target(fuzz_target):
    try:
      _get_latest_corpus(args.project, fuzz_target, corpus_dir)
      return True
    except Exception as error:  # pylint:disable=broad-except
      logging.error('Corpus download for %s failed: %s.', fuzz_target,
                    str(error))
      return False

  logging.info('Downloading corpora for %s project to %s.', args.project.name,
               corpus_dir)
  thread_pool = ThreadPool()
  return all(thread_pool.map(_download_for_single_target, fuzz_targets))


def coverage(args):
  """Generates code coverage using clang source based code coverage."""
  if args.corpus_dir and not args.fuzz_target:
    logging.error(
        '--corpus-dir requires specifying a particular fuzz target using '
        '--fuzz-target')
    return False

  if args.out_directory:
    args.project.out_directory = args.out_directory

  if not check_project_exists(args.project):
    return False

  if args.project.language not in constants.LANGUAGES_WITH_COVERAGE_SUPPORT:
    logging.error(
        'Project is written in %s, coverage for it is not supported yet.',
        args.project.language)
    return False

  if (not args.no_corpus_download and not args.corpus_dir and
      not args.project.is_external):
    if not download_corpora(args):
      return False

  env = [
      'FUZZING_ENGINE=libfuzzer',
      'FUZZING_LANGUAGE=%s' % args.project.language,
      'PROJECT=%s' % args.project.name,
      'SANITIZER=coverage',
      'HTTP_PORT=%s' % args.port,
      'COVERAGE_EXTRA_ARGS=%s' % ' '.join(args.extra_args),
  ]

  run_args = _env_to_docker_args(env)

  if args.port:
    run_args.extend([
        '-p',
        '%s:%s' % (args.port, args.port),
    ])

  if args.corpus_dir:
    if not os.path.exists(args.corpus_dir):
      logging.error('The path provided in --corpus-dir argument does not '
                    'exist.')
      return False
    corpus_dir = os.path.realpath(args.corpus_dir)
    run_args.extend(['-v', '%s:/corpus/%s' % (corpus_dir, args.fuzz_target)])
  else:
    run_args.extend(['-v', '%s:/corpus' % args.project.corpus])

  run_args.extend([
      '-m', DOCKER_MEMLIMIT,
      '-v',
      '%s:/out' % args.project.out,
      '-t' if not args.debug else '-ti',
      'gcr.io/oss-fuzz-base/base-runner',
      #'timeout', '-k', '120', f'{DOCKER_TIMEOUT}{DOCKER_TIMEOUT_UNIT}',
  ])

  if not args.debug:
    run_args.append('coverage')
    if args.fuzz_target:
      run_args.append(args.fuzz_target)
  else:
    run_args.append('bash')

  result = docker_run(run_args)
  if result:
    logging.info('Successfully generated clang code coverage report.')
  else:
    logging.error('Failed to generate clang code coverage report.')

  return result


def docker_run_fuzzer(env, args):
  run_args = _env_to_docker_args(env)

  if args.corpus_dir:
    if not os.path.exists(args.corpus_dir):
      logging.error('The path provided in --corpus-dir argument does not exist')
      return False
    corpus_dir = os.path.realpath(args.corpus_dir)
    run_args.extend([
        '-v',
        '{corpus_dir}:/tmp/{fuzzer}_corpus'.format(corpus_dir=corpus_dir,
                                                   fuzzer=args.fuzzer_name)
    ])

  if args.functions_directory != None:
    run_args.extend([
      '-v',
      '%s:/functions:ro' % args.functions_directory,
    ])

  if args.fuzzer_out_directory != None:
    run_args.extend([
      '-v',
      '%s:/fuzzer_out' % args.fuzzer_out_directory,
    ])

  run_args.extend([
      '-m', DOCKER_MEMLIMIT,
      '-v',
      '%s:/out' % args.project.out,
      '-t' if not args.debug else '-ti',
      'gcr.io/oss-fuzz-base/base-runner',
      'run_fuzzer' if not args.debug else 'bash'])
  if not args.debug:
    run_args.extend([args.fuzzer_name] + args.fuzzer_args)
  res = docker_run(run_args, with_timeout=True)

  # Change build directory permission recursively
  docker_run([
      '-v', '%s:/out' % args.project.out,
      'gcr.io/oss-fuzz-base/base-runner',
      'chmod', '-R', '777', '/out'
  ])

  return res


def apptainer_run_fuzzer(env, args):
  env.extend(['AFL_NO_AFFINITY=1'])
  run_args = _env_to_apptainer_args(env)

  if args.corpus_dir:
    if not os.path.exists(args.corpus_dir):
      logging.error('The path provided in --corpus-dir argument does not exist')
      return False
    corpus_dir = os.path.realpath(args.corpus_dir)
    run_args.extend([
        '--bind',
        '{corpus_dir}:/tmp/{fuzzer}_corpus'.format(corpus_dir=corpus_dir,
                                                   fuzzer=args.fuzzer_name)
    ])

  if args.fuzzer_out_directory != None:
    run_args.extend([
      '--bind',
      '%s:/fuzzer_out' % args.fuzzer_out_directory,
    ])

  run_args.extend([
      '--memory', DOCKER_MEMLIMIT, # TODO Causes errors on cluster
      '--bind',
      '%s:/out' % args.project.out,
      args.apptainer,
#      'docker-daemon:gcr.io/oss-fuzz-base/base-runner:latest',
      'run_fuzzer'])
  run_args.extend([args.fuzzer_name] + args.fuzzer_args)
  res = apptainer_run(run_args, with_timeout=True)

  return res


def run_fuzzer(args):
  """Runs a fuzzer in the container."""
  if args.apptainer != None and args.apptainer != "":
    service = 'apptainer'
  else:
    service = 'docker'
  if args.out_directory:
    args.project.out_directory = args.out_directory
  if not check_project_exists(args.project):
    return False

  if not _check_fuzzer_exists(args.project, args.fuzzer_name):
    return False

  env = [
      'FUZZING_ENGINE=' + args.engine,
      'SANITIZER=' + args.sanitizer,
      'FUZZER_NAME=' + args.fuzzer_name,
      'AFLGO_EXPLOITATION=' + args.aflgo_exploitation,
      'AFLGO_DISABLE_DIRECTED=%s' % ('1' if args.aflgo_disable_directed else ''),
      'TIMEOUT=%s' % args.timeout,
      'MEM_LIMIT=' + args.mem_limit,
      'RUN_FUZZER_MODE=interactive',
      'CONTAINER=' + service,
      'PROJECT=' + args.project.name,
  ]

  if args.fuzzer_out_directory != None:
      env.append('FUZZER_OUT=/fuzzer_out')

  if args.const_vscore:
      env.append('CONST_VSCORE=1')

  if args.vscore_reduce != None:
      env.append('VSCORE_REDUCE=%s' % args.vscore_reduce)

  if args.e:
    env += args.e

  if service == 'docker':
    res = docker_run_fuzzer(env, args)
  elif service == 'apptainer':
    res = apptainer_run_fuzzer(env, args)
  else:
    raise ValueError(f'Invalid service: {service}')

  return res

def reproduce(args):
  """Reproduces a specific test case from a specific project."""
  return reproduce_impl(args.project, args.fuzzer_name, args.valgrind, args.debug, args.out_directory, args.e,
                        args.fuzzer_args, args.testcase_path)


def reproduce_impl(  # pylint: disable=too-many-arguments
    project,
    fuzzer_name,
    valgrind,
    debug,
    out_directory,
    env_to_add,
    fuzzer_args,
    testcase_path,
    run_function=docker_run,
    err_result=False):
  """Reproduces a testcase in the container."""
  if out_directory:
      project.out_directory = out_directory
  if not check_project_exists(project):
    return err_result

  if not _check_fuzzer_exists(project, fuzzer_name):
    return err_result

  debugger = ''
  env = [
    'PROJECT=' + project.name,
  ]
  image_name = 'base-runner'

  if valgrind:
    debugger = 'valgrind --tool=memcheck --track-origins=yes --leak-check=full'

  if debugger:
    image_name = 'base-runner-debug'
    env += ['DEBUGGER=' + debugger]

  if env_to_add:
    env += env_to_add

  run_args = _env_to_docker_args(env) + [
      '-v',
      '%s:/out' % project.out,
      '-v',
      '%s:/testcase' % _get_absolute_path(testcase_path),
      '-t',
      'gcr.io/oss-fuzz-base/%s' % image_name,
  ]
  reproduce_args = [
      'timeout', '-k', '120', f'{DOCKER_TIMEOUT}{DOCKER_TIMEOUT_UNIT}',
      'reproduce',
      fuzzer_name,
      '-runs=100',
  ] + fuzzer_args
  if debug:
      print('fuzzer args: %s' % " ".join(reproduce_args))
      run_args += ['bash']
  else:
      run_args += reproduce_args
  return run_function(run_args)


def _validate_project_name(project_name):
  """Validates |project_name| is a valid OSS-Fuzz project name."""
  if len(project_name) > MAX_PROJECT_NAME_LENGTH:
    logging.error(
        'Project name needs to be less than or equal to %d characters.',
        MAX_PROJECT_NAME_LENGTH)
    return False

  if not VALID_PROJECT_NAME_REGEX.match(project_name):
    logging.info('Invalid project name: %s.', project_name)
    return False

  return True


def _validate_language(language):
  if not LANGUAGE_REGEX.match(language):
    logging.error('Invalid project language %s.', language)
    return False

  return True


def _create_build_integration_directory(directory):
  """Returns True on successful creation of a build integration directory.
  Suitable for OSS-Fuzz and external projects."""
  try:
    os.makedirs(directory)
  except OSError as error:
    if error.errno != errno.EEXIST:
      raise
    logging.error('%s already exists.', directory)
    return False
  return True


def _template_project_file(filename, template, template_args, directory):
  """Templates |template| using |template_args| and writes the result to
  |directory|/|filename|. Sets the file to executable if |filename| is
  build.sh."""
  file_path = os.path.join(directory, filename)
  with open(file_path, 'w') as file_handle:
    file_handle.write(template % template_args)

  if filename == 'build.sh':
    os.chmod(file_path, 0o755)


def generate(args):
  """Generates empty project files."""
  return _generate_impl(args.project, args.language)


def _get_current_datetime():
  """Returns this year. Needed for mocking."""
  return datetime.datetime.now()


def _base_builder_from_language(language):
  """Returns the base builder for the specified language."""
  if language not in LANGUAGES_WITH_BUILDER_IMAGES:
    return 'base-builder'
  return 'base-builder-{language}'.format(language=language)


def _generate_impl(project, language):
  """Implementation of generate(). Useful for testing."""
  if project.is_external:
    # External project.
    project_templates = templates.EXTERNAL_TEMPLATES
  else:
    # Internal project.
    if not _validate_project_name(project.name):
      return False
    project_templates = templates.TEMPLATES

  if not _validate_language(language):
    return False

  directory = project.build_integration_path
  if not _create_build_integration_directory(directory):
    return False

  logging.info('Writing new files to: %s.', directory)

  template_args = {
      'project_name': project.name,
      'base_builder': _base_builder_from_language(language),
      'language': language,
      'year': _get_current_datetime().year
  }
  for filename, template in project_templates.items():
    _template_project_file(filename, template, template_args, directory)
  return True


def shell(args):
  """Runs a shell within a docker image."""
  if not build_image_impl(args.project):
    return False

  env = [
      'FUZZING_ENGINE=' + args.engine,
      'SANITIZER=' + args.sanitizer,
      'ARCHITECTURE=' + args.architecture,
  ]

  if args.project.name != 'base-runner-debug':
    env.append('FUZZING_LANGUAGE=' + args.project.language)

  if args.e:
    env += args.e

  if is_base_image(args.project.name):
    image_project = 'oss-fuzz-base'
    out_dir = _get_out_dir()
  else:
    image_project = 'oss-fuzz'
    out_dir = args.project.out

  run_args = _env_to_docker_args(env)
  if args.source_path:
    run_args.extend([
        '-v',
        '%s:%s' % (_get_absolute_path(args.source_path), '/src'),
    ])

  # Get fully qualified project name
  if commit == '':
    fq_project_name = 'gcr.io/%s/%s' % (image_project, args.project.name)
  else:
    fq_project_name = 'gcr.io/%s/%s_%s' % (image_project, args.project.name, args.commit)

  run_args.extend([
      '-v',
      '%s:/out' % out_dir, '-v',
      '%s:/work' % args.project.work, '-t',
      fq_project_name, '/bin/bash'
  ])

  docker_run(run_args)
  return True


def pull_images(language=None):
  """Pulls base images used to build projects in language lang (or all if lang
  is None)."""
  for base_image_lang, base_images in BASE_IMAGES.items():
    if (language is None or base_image_lang == 'generic' or
        base_image_lang == language):
      for base_image in base_images:
        if not docker_pull(base_image):
          return False

  return True


if __name__ == '__main__':
  sys.exit(main())
