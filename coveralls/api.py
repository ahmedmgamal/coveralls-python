# coding: utf-8
import codecs
import json
import logging
import os
import re
import subprocess

import coverage
import requests

from .exception import CoverallsException
from .reporter import CoverallReporter


log = logging.getLogger('coveralls')


class Coveralls(object):
    config_filename = '.coveralls.yml'
    api_endpoint = 'https://coveralls.io/api/v1/jobs'

    def __init__(self, token_required=True, **kwargs):
        """ Coveralls!

        * repo_token
          The secret token for your repository, found at the bottom of your
          repository's page on Coveralls.

        * service_name
          The CI service or other environment in which the test suite was run.
          This can be anything, but certain services have special features
          (travis-ci, travis-pro, or coveralls-ruby).

        * [service_job_id]
          A unique identifier of the job on the service specified by
          service_name.
        """
        self._data = None
        self._token_required = token_required

        self.config = self.load_config_from_file()
        self.config.update(kwargs)

        self.load_config_from_environment()

        name, job, pr = self.load_config_from_ci_environment()
        self.config['service_name'] = self.config.get('service_name', name)
        if job:
            self.config['service_job_id'] = job
        if pr:
            self.config['service_pull_request'] = pr

        self.ensure_token()

    def ensure_token(self):
        if self.config.get('repo_token') or not self._token_required:
            return

        raise CoverallsException(
            'Not on Travis or CircleCI. You have to provide either repo_token '
            'in {} or set the COVERALLS_REPO_TOKEN env var.'.format(
                self.config_filename))

    @staticmethod
    def load_config_from_appveyor():
        pr = os.environ.get('APPVEYOR_PULL_REQUEST_NUMBER')
        return 'appveyor', os.environ.get('APPVEYOR_BUILD_ID'), pr

    @staticmethod
    def load_config_from_buildkite():
        return 'buildkite', os.environ.get('BUILDKITE_JOB_ID'), None

    @staticmethod
    def load_config_from_circle():
        pr = os.environ.get('CI_PULL_REQUEST', '').split('/')[-1] or None
        return 'circle-ci', os.environ.get('CIRCLE_BUILD_NUM'), pr

    @staticmethod
    def load_config_from_travis():
        return 'travis-ci', os.environ.get('TRAVIS_JOB_ID'), None

    @staticmethod
    def load_config_from_unknown():
        return 'coveralls-python', None, None

    def load_config_from_ci_environment(self):
        if os.environ.get('APPVEYOR'):
            return self.load_config_from_appveyor()
        if os.environ.get('BUILDKITE'):
            return self.load_config_from_buildkite()
        if os.environ.get('CIRCLECI'):
            self._token_required = False
            return self.load_config_from_circle()
        if os.environ.get('TRAVIS'):
            self._token_required = False
            return self.load_config_from_travis()

        return self.load_config_from_unknown()

    def load_config_from_environment(self):
        repo_token = os.environ.get('COVERALLS_REPO_TOKEN')
        if repo_token:
            self.config['repo_token'] = repo_token

        if os.environ.get('COVERALLS_PARALLEL', '').lower() == 'true':
            self.config['parallel'] = True

    def load_config_from_file(self):
        try:
            with open(os.path.join(os.getcwd(),
                                   self.config_filename)) as config:
                try:
                    import yaml
                    return yaml.safe_load(config)
                except ImportError:
                    log.warning('PyYAML is not installed, skipping %s.',
                                self.config_filename)
        except (OSError, IOError):
            log.debug('Missing %s file. Using only env variables.',
                      self.config_filename)

        return {}

    def merge(self, path):
        reader = codecs.getreader('utf-8')
        with open(path, 'rb') as fh:
            extra = json.load(reader(fh))
            self.create_data(extra)

    def wear(self, dry_run=False):
        """ run! """
        try:
            json_string = self.create_report()
        except coverage.CoverageException as e:
            return {'message': 'Failure to gather coverage: {}'.format(e)}

        if dry_run:
            return {}

        response = requests.post(self.api_endpoint,
                                 files={'json_file': json_string})
        try:
            return response.json()
        except ValueError:
            return {
                'message': 'Failure to submit data. Response [{}]: {}'.format(
                    response.status_code, response.text)}

    def create_report(self):
        """Generate json dumped report for coveralls api."""
        data = self.create_data()
        try:
            json_string = json.dumps(data)
        except UnicodeDecodeError as e:
            log.error('ERROR: While preparing JSON:')
            log.exception(e)
            self.debug_bad_encoding(data)
            raise

        log_string = re.sub(r'"repo_token": "(.+?)"',
                            '"repo_token": "[secure]"', json_string)
        log.debug(log_string)
        log.debug('==\nReporting %s files\n==\n', len(data['source_files']))
        for source_file in data['source_files']:
            log.debug('%s - %s/%s', source_file['name'],
                      sum(filter(None, source_file['coverage'])),
                      len(source_file['coverage']))
        return json_string

    def save_report(self, file_path):
        """Write coveralls report to file."""
        with open(file_path, 'w') as report_file:
            try:
                report = self.create_report()
            except coverage.CoverageException as e:
                logging.error('Failure to gather coverage:')
                logging.exception(e)
            else:
                report_file.write(report)

    def create_data(self, extra=None):
        """ Generate object for api.
            Example json:
            {
                "service_job_id": "1234567890",
                "service_name": "travis-ci",
                "source_files": [
                    {
                        "name": "example.py",
                        "source": "def four\n  4\nend",
                        "coverage": [null, 1, null]
                    },
                    {
                        "name": "two.py",
                        "source": "def seven\n  eight\n  nine\nend",
                        "coverage": [null, 1, 0, null]
                    }
                ],
                "parallel": True
            }
        """
        if self._data:
            return self._data

        self._data = {'source_files': self.get_coverage()}
        self._data.update(self.git_info())
        self._data.update(self.config)
        if extra:
            if 'source_files' in extra:
                self._data['source_files'].extend(extra['source_files'])
            else:
                log.warning(
                    'No data to be merged; does the json file contain '
                    '"source_files" data?')

        return self._data

    def get_coverage(self):
        config_file = self.config.get('config_file', True)
        workman = coverage.coverage(config_file=config_file)
        workman.load()

        if hasattr(workman, '_harvest_data'):
            workman._harvest_data()  # pylint: disable=W0212
        else:
            workman.get_data()

        return CoverallReporter(workman, workman.config).report()

    @staticmethod
    def git_info():
        """ A hash of Git data that can be used to display more information to
            users.

            Example:
            "git": {
                "head": {
                    "id": "5e837ce92220be64821128a70f6093f836dd2c05",
                    "author_name": "Wil Gieseler",
                    "author_email": "wil@example.com",
                    "committer_name": "Wil Gieseler",
                    "committer_email": "wil@example.com",
                    "message": "depend on simplecov >= 0.7"
                },
                "branch": "master",
                "remotes": [{
                    "name": "origin",
                    "url": "https://github.com/lemurheavy/coveralls-ruby.git"
                }]
            }
        """
        rev = run_command('git', 'rev-parse', '--abbrev-ref', 'HEAD').strip()
        remotes = run_command('git', 'remote', '-v').splitlines()

        return {
            'git': {
                'head': {
                    'id': gitlog('%H'),
                    'author_name': gitlog('%aN'),
                    'author_email': gitlog('%ae'),
                    'committer_name': gitlog('%cN'),
                    'committer_email': gitlog('%ce'),
                    'message': gitlog('%s'),
                },
                'branch': (os.environ.get('CIRCLE_BRANCH') or
                           os.environ.get('APPVEYOR_REPO_BRANCH') or
                           os.environ.get('BUILDKITE_BRANCH') or
                           os.environ.get('CI_BRANCH') or
                           os.environ.get('TRAVIS_BRANCH', rev)),
                'remotes': [{'name': line.split()[0], 'url': line.split()[1]}
                            for line in remotes if '(fetch)' in line]
            }
        }

    @staticmethod
    def debug_bad_encoding(data):
        """ Let's try to help user figure out what is at fault """
        at_fault_files = set()
        for source_file_data in data['source_files']:
            for value in source_file_data.values():
                try:
                    json.dumps(value)
                except UnicodeDecodeError:
                    at_fault_files.add(source_file_data['name'])

        if at_fault_files:
            log.error('HINT: Following files cannot be decoded properly into '
                      'unicode. Check their content: %s',
                      ', '.join(at_fault_files))


def gitlog(fmt):
    glog = run_command('git', '--no-pager', 'log', '-1',
                       '--pretty=format:{}'.format(fmt))

    try:
        return str(glog)
    except UnicodeEncodeError:
        return unicode(glog)  # pylint: disable=undefined-variable


def run_command(*args):
    cmd = subprocess.Popen(list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    stdout, stderr = cmd.communicate()

    if cmd.returncode != 0:
        raise CoverallsException(
            'command return code {}, STDOUT: "{}"\nSTDERR: "{}"'.format(
                cmd.returncode, stdout, stderr))

    try:
        return stdout.decode()
    except UnicodeDecodeError:
        return stdout.decode('utf-8')
