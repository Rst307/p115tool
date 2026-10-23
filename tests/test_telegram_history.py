import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
import threading

from p115tool.config import Config
from p115tool.service import Service
from p115tool.import_history import message_title
from p115tool.native_ui import dispatch
from test_telegram import Drive, message


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.config=Config(data_dir=str(root/'data'),strm_dir=str(root/'strm'))
        self.drive=Drive()
        self.service=Service(self.config,client=self.drive,
            virtual_organizer=lambda p:'电视剧/示例剧 (2026)/Season 01/'+Path(p).name)
        self.monitor=self.service.telegram

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def ingest(self, ident=1):
        self.monitor.ingest('@example',message('【示例剧】更新 S01E01\nhttps://115.com/s/abcdef?password=abcd\n提取码 abcd',ident))

    def test_title_is_bounded_and_secrets_are_removed(self):
        self.assertEqual(message_title(message('密码 abcd\nhttps://115.com/s/abcdef\n【示例剧】 S01E01')), '【示例剧】 S01E01')
        self.assertEqual(message_title(message('abcd\n示例剧'),['abcd']), '示例剧')
        self.assertEqual(len(message_title(message('剧'*1000))),240)
        self.ingest()
        item=self.monitor.snapshot()['items'][0]
        self.assertEqual(item['title'],'【示例剧】更新 S01E01')
        for secret in ('abcdef','abcd','password','fingerprint','task_id'):
            self.assertNotIn(secret,json.dumps(item))
        self.assertEqual(item['message_url'],'https://t.me/example/1')
        self.assertIsNone(item['started_at'])
        self.assertEqual(item['attempts'],0)

    def test_names_times_counts_and_association_survive_restart(self):
        self.ingest()
        self.monitor._drain()
        if self.service.storage._thread: self.service.storage._thread.join(5)
        item=self.monitor.snapshot()['items'][0]
        self.assertEqual(item['state'],'DONE')
        self.assertEqual(item['total'],1)
        self.assertIn('示例剧 (2026)/Season 01/Example.S01E01.mkv',item['media_names'][0])
        self.assertEqual(item['attempts'],1)
        self.assertGreaterEqual(item['finished_at'],item['started_at'])
        self.assertGreaterEqual(item['started_at'],item['created_at'])
        self.service.close()
        self.service=Service(self.config,client=self.drive)
        restored=self.service.telegram.snapshot()['items'][0]
        self.assertEqual(restored,item)
        self.assertEqual(self.service.telegram.snapshot()['counts']['DONE'],1)

    def test_full_history_search_status_pagination_and_clamping(self):
        self.service.query('''WITH RECURSIVE ids(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM ids WHERE n<65)
            INSERT INTO telegram_imports(fingerprint,channel,message_id,share_code,password,state,created_at,title)
            SELECT 'seed-'||n,'@example',n,'abcdef','abcd','FAILED',0,'剧集 '||n FROM ids''')
        first=self.monitor.snapshot()
        self.assertEqual((first['total'],len(first['items'])),(65,50))
        second=self.monitor.snapshot(page=2)
        self.assertEqual(len(second['items']),15)
        self.assertEqual(second['items'][0]['message_id'],15)
        result=self.monitor.snapshot(search='剧集 1',state_filter='FAILED')
        self.assertEqual(result['total'],11)
        self.assertEqual(self.monitor.snapshot(search="%' OR 1=1 --")['total'],0)
        self.assertEqual(self.monitor.snapshot(state_filter='DONE')['total'],0)
        self.assertEqual(self.monitor.snapshot(page=100)['page'],2)
        self.assertEqual(self.monitor.snapshot()['retryable'],65)

    def test_retry_keeps_names_and_identity_resets_attempt_times(self):
        self.ingest()
        self.service.query("UPDATE telegram_imports SET state='PARTIAL',media_names=?,total=4,attempts=2,started_at=1,finished_at=2",(json.dumps(['示例剧 S01E01']),))
        self.monitor._queue_retries(1)
        item=self.monitor.snapshot()['items'][0]
        self.assertEqual(item['attempts'],2)
        self.assertEqual(item['media_names'],['示例剧 S01E01'])
        self.assertEqual(item['state'],'PENDING')
        self.assertIsNone(item['started_at']);self.assertIsNone(item['finished_at']);self.assertIsNone(item['total'])
        self.assertFalse(item['retryable'])

    def test_private_message_url_and_missing_share_retry_rules(self):
        self.monitor.ingest('-1001234567890',message('https://115.com/s/abcdef'))
        self.assertEqual(self.monitor.snapshot()['items'][0]['message_url'],'https://t.me/c/1234567890/1')
        self.service.query("UPDATE telegram_imports SET state='FAILED',share_code=''")
        self.assertFalse(self.monitor.snapshot()['items'][0]['retryable'])

    def test_legacy_names_are_read_only_local_and_searchable(self):
        self.ingest()
        self.monitor._drain()
        if self.service.storage._thread: self.service.storage._thread.join(5)
        self.service.query("UPDATE telegram_imports SET media_names='[]',title=''")
        calls=list(self.drive.calls)
        item=self.monitor.snapshot(search='S01E01')['items'][0]
        self.assertIn('Season 01',item['media_names'][0])
        self.assertEqual(self.drive.calls,calls)
        self.assertEqual(self.service.query('SELECT media_names FROM telegram_imports',one=True)['media_names'],'[]')

    def test_bounded_progress_and_terminal_persistence_after_queue_trim(self):
        self.ingest()
        self.service.query("UPDATE telegram_imports SET state='RUNNING',task_id='task'")
        self.monitor.import_progress({'task_id':'task','state':'PARTIAL','done':11,'failed':1,'total':12,
            'media_names':['剧集 S01E%02d'%i for i in range(12)],'started_at':100,'error':'VIRTUAL_ORGANIZE_FAILED'})
        self.monitor._sync_tasks()
        item=self.monitor.snapshot()['items'][0]
        self.assertEqual(len(item['media_names']),10)
        self.assertEqual(item['total'],12)
        self.assertEqual(item['state'],'PARTIAL')

    def test_native_filter_validation(self):
        plugin=NS(_lifecycle=threading.RLock(),_service=self.service)
        self.assertEqual(dispatch(plugin,'data',{'kind':'telegram','page':1,'state':'ALL','search':''})['total'],0)
        for patch in ({'page':True},{'page':0},{'state':'bad'},{'search':'x'*101},{'extra':1}):
            with self.assertRaises(ValueError):
                dispatch(plugin,'data',{'kind':'telegram','page':1,'state':'ALL','search':'',**patch})

    def test_original_schema_migrates_and_interrupts_inflight_without_replay(self):
        self.service.close()
        with sqlite3.connect(self.service.path) as db:
            db.execute('DROP TABLE telegram_imports')
            db.execute('''CREATE TABLE telegram_imports (
                id INTEGER PRIMARY KEY,fingerprint TEXT UNIQUE NOT NULL,channel TEXT NOT NULL,
                message_id INTEGER NOT NULL,share_code TEXT NOT NULL,password TEXT NOT NULL,
                state TEXT NOT NULL,task_id TEXT,done INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,error TEXT,created_at REAL NOT NULL)''')
            db.execute("INSERT INTO telegram_imports(fingerprint,channel,message_id,share_code,password,state,created_at) VALUES('old','@example',1,'abcdef','abcd','QUEUED',1)")
        self.service=Service(self.config,client=self.drive)
        item=self.service.telegram.snapshot()['items'][0]
        self.assertEqual(item['state'],'INTERRUPTED')
        self.assertEqual(item['error'],'RESTARTED')
        self.assertEqual(item['media_names'],[])
        self.assertIsNone(item['started_at'])
        self.assertEqual(item['attempts'],0)
        self.assertEqual(self.drive.calls,[])


if __name__=='__main__': unittest.main()
