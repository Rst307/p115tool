import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from p115tool.config import Config
from p115tool.service import Service
from p115tool.telegram import BACKLOG_LIMIT
from test_telegram import Drive


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.drive = Drive()
        self.service = Service(Config(data_dir=str(root/'data'), strm_dir=str(root/'strm')), client=self.drive)
        self.monitor = self.service.telegram

    def tearDown(self):
        self.service.close()
        self.assertEqual(self.drive.calls, [])
        self.temp.cleanup()

    def seed(self, state, *, code='abcdef', error='STORAGE_FAILED', count=1):
        start = self.service.query('SELECT coalesce(max(id),0) n FROM telegram_imports', one=True)['n'] + 1
        self.service.query('''WITH RECURSIVE ids(n) AS (SELECT ? UNION ALL SELECT n+1 FROM ids WHERE n<?)
            INSERT INTO telegram_imports(id,fingerprint,channel,message_id,share_code,password,state,error,created_at,task_id,done,failed,upgraded,skipped)
            SELECT n,'entry-'||n,'@example',n,?,'private-code',?,?,0,'old-'||n,3,1,2,1 FROM ids''',
            (start, start + count - 1, code, state, error))
        return start

    def test_batch_selects_only_retryable_records_and_resets_attempt_counts(self):
        selected = [self.seed(state) for state in ('FAILED','PARTIAL','INTERRUPTED')]
        others = [self.seed(state) for state in ('DONE','PENDING','QUEUED','RUNNING')]
        others += [self.seed('FAILED', code=''), self.seed('FAILED', error='EXTRACTION_AMBIGUOUS')]
        before = {row['id']: row for row in self.service.query('SELECT * FROM telegram_imports')}
        self.assertEqual(self.monitor.snapshot()['retryable'], 3)
        result = self.monitor.retry_all()
        self.assertEqual(result, {'state':'RETRY_BATCH_QUEUED','queued':3,'remaining':0,'subscription_enabled':False})
        for row in self.service.query('SELECT * FROM telegram_imports'):
            if row['id'] in selected:
                self.assertEqual(row['state'], 'PENDING')
                self.assertIsNone(row['task_id']); self.assertIsNone(row['error'])
                self.assertEqual([row[k] for k in ('done','failed','upgraded','skipped')], [0,0,0,0])
                for key in ('fingerprint','channel','message_id','share_code','password','created_at'):
                    self.assertEqual(row[key], before[row['id']][key])
            else:
                self.assertIn(row['id'], others)
                self.assertEqual(row, before[row['id']])
        self.assertEqual(self.monitor.snapshot()['retryable'], 0)
        self.assertNotIn('private-code', json.dumps(result))
        self.assertNotIn('private-code', json.dumps(self.monitor.snapshot()))

    def test_retry_covers_history_older_than_fifty_displayed_items(self):
        self.seed('FAILED', count=65)
        self.assertEqual(len(self.monitor.snapshot()['items']), 50)
        self.assertEqual(self.monitor.snapshot()['retryable'], 65)
        self.assertEqual(self.monitor.retry_all()['queued'], 65)
        self.assertEqual(self.monitor.snapshot()['pending'], 65)

    def test_capacity_is_bounded_and_oldest_failures_are_retried_first(self):
        self.seed('PENDING', count=BACKLOG_LIMIT - 2)
        first = self.seed('FAILED', count=4)
        result = self.monitor.retry_all()
        self.assertEqual((result['queued'], result['remaining']), (2, 2))
        self.assertEqual(self.monitor.snapshot()['pending'], BACKLOG_LIMIT)
        self.assertEqual(self.monitor.snapshot()['retryable'], 2)
        self.assertEqual(self.service.query("SELECT id FROM telegram_imports WHERE state='FAILED' ORDER BY id"), [{'id':first+2},{'id':first+3}])
        self.assertEqual(self.monitor.retry_all()['state'], 'RETRY_QUEUE_FULL')
        self.service.query("UPDATE telegram_imports SET state='DONE' WHERE id<=2")
        self.assertEqual(self.monitor.retry_all()['queued'], 2)
        self.assertEqual(self.monitor.snapshot()['retryable'], 0)

    def test_repeated_batch_clicks_do_not_requeue_pending_records(self):
        self.seed('PARTIAL')
        self.assertEqual(self.monitor.retry_all()['queued'], 1)
        self.assertEqual(self.monitor.retry_all()['state'], 'RETRY_UNAVAILABLE')
        self.assertEqual(self.monitor.snapshot()['pending'], 1)

    def test_empty_history_does_not_schedule_imports(self):
        self.monitor._next_poll = 123
        self.assertEqual(self.monitor.retry_all()['queued'], 0)
        self.assertEqual(self.monitor._next_poll, 123)

    def test_single_retry_uses_same_capacity_and_ambiguity_rules(self):
        entry = self.seed('FAILED')
        ambiguous = self.seed('FAILED', error='EXTRACTION_AMBIGUOUS')
        self.seed('PENDING', count=BACKLOG_LIMIT)
        self.assertEqual(self.monitor.retry(entry)['state'], 'RETRY_QUEUE_FULL')
        self.assertEqual(self.monitor.retry(ambiguous)['state'], 'RETRY_UNAVAILABLE')
        self.service.query("UPDATE telegram_imports SET state='DONE' WHERE id=3")
        self.assertEqual(self.monitor.retry(entry)['state'], 'RETRY_QUEUED')

    def test_queue_full_after_retry_keeps_pending_and_upstream_wait_is_preserved(self):
        self.seed('FAILED')
        self.monitor._rate_until = 9999999999
        self.monitor.retry_all()
        self.assertEqual(self.monitor._rate_until, 9999999999)
        with patch.object(self.service.storage, 'start', return_value={'state':'QUEUE_FULL'}):
            self.monitor._drain()
        self.assertEqual(self.monitor.snapshot()['pending'], 1)
        self.assertFalse(self.monitor.settings['enabled'])


if __name__ == '__main__': unittest.main()
