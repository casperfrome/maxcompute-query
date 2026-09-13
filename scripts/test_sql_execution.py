"""Offline execution contracts; fake service objects never contact MaxCompute."""
import importlib
import io
import json
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from odps.errors import NoPermission, ODPSError, WaitTimeoutError


class Reader:
    def __init__(self, count=3, columns=('value',), batches=None):
        self.count = count
        self.schema = type('Schema', (), {'names': list(columns)})()
        self.columns = list(columns)
        self.preview_calls = []
        self.batch_calls = []
        self.batches = batches
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def to_pandas(self, **kwargs):
        self.preview_calls.append(kwargs)
        return pd.DataFrame({c: list(range(kwargs['count'])) for c in self.columns})

    def iter_pandas(self, **kwargs):
        self.batch_calls.append(kwargs)
        if self.batches is not None:
            for batch in self.batches:
                if isinstance(batch, BaseException):
                    raise batch
                yield batch
        else:
            for start in range(0, self.count, kwargs['batch_size']):
                yield pd.DataFrame({c: list(range(start, min(start + kwargs['batch_size'], self.count)))
                                    for c in self.columns})


class Instance:
    def __init__(self, reader=None, query='select 1'):
        self.id = 'instance-123'
        self.project = type('Project', (), {'name': 'project_a'})()
        self.reader = reader or Reader()
        self.query = query
        self.events = []
        self.wait_error = None
        self.success = True
        self.open_errors = []
        self.inspect_durable = None

    def get_logview_address(self):
        self.events.append('logview')
        if self.inspect_durable:
            self.inspect_durable()
        return 'https://example.test/log?Signature=private-signature'

    def wait_for_success(self, **kwargs):
        self.events.append(('wait', kwargs))
        if self.wait_error:
            raise self.wait_error

    def is_successful(self, **kwargs):
        self.events.append(('success', kwargs))
        return self.success

    def is_terminated(self, **kwargs):
        return True

    def open_reader(self, **kwargs):
        self.events.append(('open', kwargs))
        if self.open_errors:
            raise self.open_errors.pop(0)
        return self.reader

    def get_sql_query(self):
        return self.query

    def get_task_results(self):
        self.events.append('raw')
        return {'SQLTask': 'raw description'}


class Client:
    project = 'project_a'
    endpoint = 'https://service.test'

    def __init__(self, instance=None):
        self.instance = instance or Instance()
        self.submissions = []
        self.recoveries = []
        self.submit_error = None

    def run_sql(self, sql):
        self.submissions.append(sql)
        if self.submit_error:
            raise self.submit_error
        return self.instance

    def get_instance(self, instance_id, **kwargs):
        self.recoveries.append((instance_id, kwargs))
        return self.instance


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stderr = io.StringIO()
        try:
            self.backend = importlib.import_module('sql_execution')
        except ModuleNotFoundError:
            self.fail('Required sql_execution backend API has not been implemented')

    def execute(self, client=None, **kwargs):
        kwargs.setdefault('sql', 'select 1')
        return self.backend.execute_query(client or Client(), run_root=self.root / 'runs',
                                          stderr=self.stderr, **kwargs)

    def record(self):
        return json.loads(next((self.root / 'runs').glob('*/state.json')).read_text(encoding='utf-8'))

    def test_new_backend_api_exists(self):
        self.assertTrue(callable(self.backend.execute_query))

    def test_submission_is_once_and_id_durable_before_logview_wait(self):
        client = Client()
        def inspect():
            self.assertEqual(self.record()['instance_id'], client.instance.id)
            self.assertIn(client.instance.id, self.stderr.getvalue())
            self.assertIn('project_a', self.stderr.getvalue())
            self.assertIn('state.json', self.stderr.getvalue())
        client.instance.inspect_durable = inspect
        data, meta = self.execute(client)
        self.assertEqual(client.submissions, ['select 1'])
        self.assertEqual(meta['state'], 'succeeded')
        self.assertEqual(meta['project'], 'project_a')
        self.assertEqual(meta['endpoint'], client.endpoint)
        self.assertEqual(meta['downloaded_rows'], 3)
        self.assertEqual(len(data), 3)
        self.assertEqual(self.record()['sql_sha256'], meta['sql_sha256'])
        self.assertEqual((Path(meta['run_dir']) / 'query.sql').read_text(encoding='utf-8'), 'select 1')
        self.assertIn('private-signature', self.record()['logview'])
        self.assertNotIn('private-signature', self.stderr.getvalue())

    def test_timeout_keeps_id_and_recovery_does_not_submit(self):
        client = Client()
        client.instance.wait_error = WaitTimeoutError('timeout')
        with self.assertRaises(self.backend.ExecutionError) as ctx:
            self.execute(client, wait_timeout=7)
        self.assertEqual(ctx.exception.exit_code, 5)
        self.assertEqual(ctx.exception.meta['state'], 'wait_timeout')
        self.assertIn('--instance-id instance-123', str(ctx.exception))
        self.assertIn('--project project_a', str(ctx.exception))
        self.assertEqual(len(client.submissions), 1)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'open' for e in client.instance.events))
        client.instance.wait_error = None
        data, meta = self.execute(client, sql=None, instance_id='instance-123')
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.recoveries, [('instance-123', {'project': 'project_a'})])
        self.assertEqual(len(data), 3)

    def test_recovery_wrong_project_or_id_never_opens_reader(self):
        for mismatch in ('project', 'id'):
            with self.subTest(mismatch=mismatch):
                client = Client()
                if mismatch == 'project':
                    client.instance.project = 'other'
                else:
                    client.instance.id = 'other'
                with self.assertRaises(self.backend.ExecutionError) as ctx:
                    self.execute(client, sql=None, instance_id='instance-123')
                self.assertEqual(ctx.exception.exit_code, 4)
                self.assertEqual(client.submissions, [])
                self.assertFalse(any(isinstance(e, tuple) and e[0] == 'open' for e in client.instance.events))

    def test_uncertain_submission_is_not_retried_or_called_failed(self):
        client = Client()
        client.submit_error = ConnectionError('socket lost token=do-not-show')
        with self.assertRaises(self.backend.ExecutionError) as ctx:
            self.execute(client)
        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertEqual(ctx.exception.meta['state'], 'submission_unknown')
        self.assertIsNone(ctx.exception.meta['instance_id'])
        self.assertNotIn('do-not-show', str(ctx.exception))
        self.assertEqual(len(client.submissions), 1)

    def test_wait_network_uncertainty_and_remote_failure(self):
        for error, state in [(ConnectionError('lost'), 'wait_unknown'), (ODPSError('task failed'), 'failed')]:
            client = Client()
            client.instance.wait_error = error
            client.instance.success = False
            with self.subTest(state=state), self.assertRaises(self.backend.ExecutionError) as ctx:
                self.execute(client)
            self.assertEqual(ctx.exception.exit_code, 4)
            self.assertEqual(ctx.exception.meta['state'], state)

    def test_success_is_explicitly_verified_before_reading(self):
        client = Client()
        client.instance.success = False
        with self.assertRaises(self.backend.ExecutionError):
            self.execute(client)
        self.assertTrue(any(isinstance(e, tuple) and e[0] == 'success' for e in client.instance.events))
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'open' for e in client.instance.events))

    def test_interruption_is_recorded_and_reraised(self):
        client = Client()
        client.instance.wait_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.execute(client)
        self.assertEqual(self.record()['state'], 'interrupted')
        self.assertEqual(len(client.submissions), 1)

    def test_sdk_swallowed_sigint_is_still_recorded_and_never_reads(self):
        client = Client()
        original = signal.getsignal(signal.SIGINT)
        def sdk_wait(**kwargs):
            try:
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            except KeyboardInterrupt:
                pass
        client.instance.wait_for_success = sdk_wait
        with self.assertRaises(KeyboardInterrupt):
            self.execute(client)
        self.assertEqual(self.record()['state'], 'interrupted')
        self.assertIs(signal.getsignal(signal.SIGINT), original)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'open' for e in client.instance.events))

    def test_invalid_inputs_rejected_before_submission(self):
        invalid = [{'max_rows': 0}, {'wait_timeout': 0}, {'batch_size': 0}, {'save': 'bad.txt'},
                   {'max_rows': True}, {'sql': 'delete from t'}, {'sql': 'show tables', 'save': 'data.csv'},
                   {'instance_id': 'i'}, {'sql': None}]
        for kwargs in invalid:
            client = Client()
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.execute(client, **kwargs)
            self.assertEqual(client.submissions, [])

    def test_module_import_is_offline_without_sdk_pandas_or_config(self):
        code = """import sys, importlib.abc
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, *args):
  if fullname.split('.')[0] in {'odps', 'pandas', 'openpyxl', 'config'}: raise AssertionError(fullname)
sys.meta_path.insert(0, Guard())
import sql_execution
"""
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code],
                                cwd=Path(__file__).parent, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
