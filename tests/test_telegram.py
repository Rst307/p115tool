import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, AsyncMock

from p115tool.config import Config
from p115tool.models import RemoteFile
from p115tool.service import Service
from p115tool.telegram import extract_shares, normalize_channel, proxy_options, private_write


def message(text='', ident=1, **kwargs):
    return NS(message=text, id=ident, entities=[], reply_markup=None, **kwargs)


class ParserTests(unittest.TestCase):
    def test_visible_links_and_inline_password(self):
        self.assertEqual(extract_shares(message('https://115.com/s/abcdef?password=a1b2')), ([('abcdef', 'a1b2')], 0))

    def test_multiple_links_keep_own_codes(self):
        value = 'https://115.com/s/abcdef\n提取码：aaaa\nhttps://115cdn.com/s/ghijkl\n密码: bbbb'
        self.assertEqual(extract_shares(message(value)), ([('abcdef', 'aaaa'), ('ghijkl', 'bbbb')], 0))

    def test_code_before_single_link(self):
        self.assertEqual(extract_shares(message('访问码 ab12\nhttps://115.com/s/abcdef')), ([('abcdef', 'ab12')], 0))

    def test_ambiguous_password_is_not_guessed(self):
        self.assertEqual(extract_shares(message('https://115.com/s/abcdef 提取码 aaaa 密码 bbbb')), ([], 1))

    def test_hidden_utf16_link_and_button(self):
        item = message('😀点击链接 提取码：ab12')
        item.entities = [NS(offset=2, length=4, url='https://115.com/s/abcdef')]
        self.assertEqual(extract_shares(item), ([('abcdef', 'ab12')], 0))
        item = message('资源')
        item.reply_markup = NS(rows=[NS(buttons=[NS(url='https://115.com/s/abcdef?password=a1b2')])])
        self.assertEqual(extract_shares(item), ([('abcdef', 'a1b2')], 0))

    def test_host_spoofing_and_redirects_rejected(self):
        item = message('https://115.com.evil/s/abcdef https://other.test/?url=https://115.com/s/abcdef')
        self.assertEqual(extract_shares(item), ([], 0))
        item.message = '点击领取'; item.entities = [NS(offset=0,length=4,url='https://115.com@evil.test/s/abcdef')]
        self.assertEqual(extract_shares(item), ([], 0))

    def test_duplicate_links_and_passwords(self):
        item = message('https://115.com/s/abcdef?password=abcd\nhttps://115.com/s/abcdef?password=abcd')
        self.assertEqual(extract_shares(item), ([('abcdef', 'abcd')], 0))

    def test_channels_and_proxy_validation(self):
        self.assertEqual(normalize_channel('https://t.me/Example/'), '@example')
        self.assertEqual(normalize_channel('-100123456789'), '-100123456789')
        for value in ['https://t.me/+invite', 'https://evil.test/channel', '@a', '1234', 'https://t.me/name/42']:
            with self.assertRaises(ValueError): normalize_channel(value)
        self.assertEqual(proxy_options('socks5://user:p%40ss@localhost:1080')['password'], 'p@ss')
        for value in ['https://localhost:1080', 'socks5://localhost', 'http://localhost:8080/path']:
            with self.assertRaises(ValueError): proxy_options(value)


class Drive:
    def __init__(self): self.calls = []
    def close(self): pass
    def share_files(self, code, password, cid):
        self.calls.append(('share_files', code, password, cid))
        return [RemoteFile('42', 'Example.S01E01.mkv', 100, 'A' * 40)]


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = Config(data_dir=str(root/'data'), strm_dir=str(root/'strm'))
        self.drive = Drive()
        self.service = Service(self.cfg, client=self.drive, virtual_organizer=lambda p:'电视剧/Example/Season 01/'+Path(p).name)
        self.monitor = self.service.telegram

    def tearDown(self):
        self.service.close(); self.temp.cleanup()

    def rows(self): return self.service.query('SELECT * FROM telegram_imports ORDER BY id')

    def test_disabled_starts_no_thread_or_network(self):
        self.assertIsNone(self.monitor._thread)
        self.assertEqual(self.monitor.snapshot()['state'], 'DISABLED')
        self.assertEqual(self.drive.calls, [])

    def test_settings_secrets_preserved_and_not_returned(self):
        settings = {'enabled':False,'api_id':123,'api_hash':'a'*32,'channels':['@example'], 'proxy':'socks5://user:secret@localhost:1080'}
        self.assertEqual(self.monitor.configure(settings)['state'], 'SAVED')
        settings['api_hash']=''; settings['proxy']=''
        self.monitor.configure(settings)
        snapshot=json.dumps(self.monitor.snapshot())
        self.assertNotIn('a'*32,snapshot);self.assertNotIn('secret',snapshot)
        self.assertEqual(self.monitor.settings['api_hash'],'a'*32)
        self.assertEqual(self.monitor.settings['proxy'],'socks5://user:secret@localhost:1080')
        self.assertEqual(os.stat(self.monitor.settings_path).st_mode & 0o777,0o600)
        self.monitor.configure({**settings,'clear_proxy':True})
        self.assertEqual(self.monitor.settings['proxy'],'')

    def test_invalid_settings_leave_file_unchanged(self):
        for data in ({'enabled':True}, {'history_limit':501}, {'interval':5}, {'channels':['https://t.me/+invite']}, {'api_hash':'secret'}):
            with self.assertRaises(ValueError): self.monitor.configure(data)
        self.assertFalse(self.monitor.settings_path.exists())

    def test_duplicate_reposts_across_channels_and_restart(self):
        item=message('https://115.com/s/abcdef?password=abcd')
        self.monitor.ingest('-1001',item); self.monitor.ingest('-1002',item)
        self.assertEqual(len(self.rows()),1)
        self.service.close()
        self.service=Service(self.cfg,client=self.drive)
        self.monitor=self.service.telegram
        self.monitor.ingest('-1001',item)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.rows()[0]['state'],'PENDING')

    def test_backpressure_does_not_advance_cursor(self):
        self.monitor._cursor('-1001',10)
        with patch('p115tool.telegram.BACKLOG_LIMIT',0):
            self.assertFalse(self.monitor.ingest('-1001',message('https://115.com/s/abcdef',11)))
        self.assertEqual(self.service.query('SELECT message_id FROM telegram_cursors',one=True)['message_id'],10)

    def test_new_channel_default_ignores_history(self):
        class Client:
            async def get_messages(self, entity, limit): return [message('https://115.com/s/abcdef',50)]
        asyncio.run(self.monitor._read_channel(Client(),object(),'-1001'))
        self.assertEqual(self.rows(),[])
        self.assertEqual(self.service.query('SELECT message_id FROM telegram_cursors',one=True)['message_id'],50)

    def test_history_and_incremental_read_oldest_first(self):
        self.monitor.settings['history_limit']=2
        calls=[]
        class Client:
            async def get_messages(self,entity,limit):
                return [message('https://115.com/s/ghijkl',12),message('https://115.com/s/abcdef',11)]
            async def iter_messages(self,entity,**kwargs):
                calls.append(kwargs);yield message('https://115.com/s/mnopqr',13)
        client=Client()
        asyncio.run(self.monitor._read_channel(client,object(),'-1001'))
        asyncio.run(self.monitor._read_channel(client,object(),'-1001'))
        self.assertEqual([r['message_id'] for r in self.rows()],[11,12,13])
        self.assertEqual(calls,[{'min_id':12,'reverse':True,'limit':100}])

    def test_backfill_boundary_survives_full_backlog(self):
        self.monitor.settings['history_limit']=2
        class Client:
            async def get_messages(self,entity,limit):
                return [message('https://115.com/s/ghijkl',12),message('https://115.com/s/abcdef',11)]
        with patch('p115tool.telegram.BACKLOG_LIMIT',0):
            asyncio.run(self.monitor._read_channel(Client(),object(),'-1001'))
        self.assertEqual(self.service.query('SELECT message_id FROM telegram_cursors',one=True)['message_id'],10)

    def test_virtual_pipeline_never_transfers_or_deletes(self):
        self.monitor.ingest('-1001',message('https://115.com/s/abcdef?password=abcd'))
        self.monitor._drain()
        if self.service.storage._thread: self.service.storage._thread.join(5)
        row=self.rows()[0]
        self.assertEqual(row['state'],'DONE');self.assertEqual(row['done'],1)
        resources=self.service.query('SELECT kind,stage FROM resource_storage')
        self.assertEqual(resources,[{'kind':'VIRTUAL','stage':'READY'}])
        self.assertEqual(len(list(Path(self.cfg.strm_dir).rglob('*.strm'))),1)
        self.assertEqual([c[0] for c in self.drive.calls],['share_files'])
        self.assertNotIn('abcdef',json.dumps(self.monitor.snapshot()))
        self.assertNotIn('abcd',json.dumps(self.monitor.snapshot()))

    def test_full_storage_queue_keeps_pending(self):
        self.monitor.ingest('-1001',message('https://115.com/s/abcdef'))
        with patch.object(self.service.storage,'start',return_value={'state':'QUEUE_FULL'}): self.monitor._drain()
        self.assertEqual(self.rows()[0]['state'],'PENDING')

    def test_final_status_survives_storage_history_trimming(self):
        self.monitor.ingest('-1001',message('https://115.com/s/abcdef'))
        self.service.query("UPDATE telegram_imports SET state='RUNNING',task_id='test-task'")
        self.service.storage._import_progress({'task_id':'test-task','state':'PARTIAL','done':3,'failed':1,'error':'VIRTUAL_ORGANIZE_FAILED'})
        self.monitor._sync_tasks()
        self.assertEqual(self.rows()[0]['state'],'PARTIAL');self.assertEqual(self.rows()[0]['done'],3)

    def test_running_progress_is_live_without_per_file_database_writes(self):
        self.monitor.ingest('-1001',message('https://115.com/s/abcdef'))
        self.service.query("UPDATE telegram_imports SET state='RUNNING',task_id='live-task'")
        task={'id':'live-task','state':'RUNNING','done':5,'failed':0,'error':None}
        self.service.storage._import_tasks.append(task)
        with patch.object(self.service,'query',wraps=self.service.query) as query:
            self.monitor.import_progress({**task,'task_id':'live-task'})
            query.assert_not_called()
        self.assertEqual(self.monitor.snapshot()['items'][0]['done'],5)
        self.assertEqual(self.rows()[0]['done'],0)

    def test_failed_entries_do_not_auto_retry(self):
        self.monitor.ingest('-1001',message('https://115.com/s/abcdef'))
        self.service.query("UPDATE telegram_imports SET state='FAILED',error='STORAGE_FAILED'")
        with patch.object(self.service.storage,'start') as start:
            self.monitor._drain();start.assert_not_called()
        self.assertEqual(self.monitor.retry(self.rows()[0]['id'])['state'],'RETRY_QUEUED')
        self.assertEqual(self.rows()[0]['state'],'PENDING')

    def test_interrupted_inflight_does_not_replay_on_restart(self):
        self.monitor.ingest('-1001',message('https://115.com/s/abcdef'))
        self.service.query("UPDATE telegram_imports SET state='RUNNING',task_id='old'")
        self.service.close();self.service=Service(self.cfg,client=self.drive);self.monitor=self.service.telegram
        self.assertEqual(self.rows()[0]['state'],'INTERRUPTED')

    def test_login_codes_and_two_factor_not_persisted(self):
        client=NS(is_user_authorized=AsyncMock(return_value=False),send_code_request=AsyncMock(return_value=NS(phone_code_hash='temporary-hash')),
                  sign_in=AsyncMock(return_value=NS(bot=False)),session=NS(save=lambda:'saved-session'))
        self.monitor._connect=AsyncMock(return_value=client)
        self.assertEqual(self.monitor.send_code('+8613800000000')['state'],'CODE_SENT')
        self.assertEqual(self.monitor.send_code('+8613800000000')['state'],'CODE_COOLDOWN')
        self.assertEqual(self.monitor.sign_in('12345','')['state'],'AUTHORIZED')
        snapshot=json.dumps(self.monitor.snapshot())
        for secret in ('13800000000','temporary-hash','12345','saved-session'): self.assertNotIn(secret,snapshot)
        self.assertEqual(self.monitor._phone,'');self.assertEqual(self.monitor._phone_hash,'')
        self.assertEqual(os.stat(self.monitor.session_path).st_mode & 0o777,0o600)

    def test_two_factor_error_is_safe_and_keeps_login_context(self):
        error=type('SessionPasswordNeededError',(Exception,),{})('secret raw error')
        client=NS(is_user_authorized=AsyncMock(return_value=False),send_code_request=AsyncMock(return_value=NS(phone_code_hash='temporary-hash')),sign_in=AsyncMock(side_effect=error))
        self.monitor._connect=AsyncMock(return_value=client)
        self.monitor.send_code('+8613800000000')
        self.assertEqual(self.monitor.sign_in('12345','')['state'],'PASSWORD_REQUIRED')
        self.assertEqual(self.monitor._phone_hash,'temporary-hash')
        self.assertNotIn('secret raw error',json.dumps(self.monitor.snapshot()))

    def test_flood_wait_is_respected(self):
        error=type('FloodWaitError',(Exception,),{} )('do not expose')
        error.seconds=3600
        self.monitor._failure(error)
        until=self.monitor._next_poll
        self.monitor.poll_now()
        self.assertEqual(self.monitor._next_poll,until)
        self.assertEqual(self.monitor.snapshot()['error'],'RATE_LIMITED')

    def test_flood_wait_also_blocks_login_requests(self):
        self.monitor._rate_until=time.time()+3600
        self.monitor._connect=AsyncMock()
        self.assertEqual(self.monitor.send_code('+8613800000000')['state'],'RATE_LIMITED')
        self.assertEqual(self.monitor.sign_in('12345','')['state'],'RATE_LIMITED')
        self.monitor._connect.assert_not_awaited()

    def test_logged_in_session_does_not_send_new_code(self):
        client=NS(is_user_authorized=AsyncMock(return_value=True),send_code_request=AsyncMock())
        self.monitor._connect=AsyncMock(return_value=client)
        self.assertEqual(self.monitor.send_code('+8613800000000')['state'],'ALREADY_AUTHORIZED')
        client.send_code_request.assert_not_awaited()

    def test_shutdown_cancels_inflight_channel_poll(self):
        entered=threading.Event()
        async def connect():
            entered.set()
            await asyncio.Event().wait()
        self.monitor.settings['enabled']=True
        self.monitor._connect=connect
        self.monitor._ensure_thread()
        self.assertTrue(entered.wait(3))
        self.monitor.close()
        self.assertFalse(self.monitor._thread.is_alive())

    def test_only_joined_broadcast_channels_are_read(self):
        from telethon.tl.types import Channel, Chat
        from datetime import datetime, timezone
        entity=Channel(id=123,title='Example',photo=None,date=datetime.now(timezone.utc),broadcast=True,username='example')
        client=NS(is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(bot=False)))
        async def dialogs(**kwargs):
            yield NS(entity=entity)
            yield NS(entity=NS(broadcast=False,username='group'))
        client.iter_dialogs=dialogs
        self.monitor._connect=AsyncMock(return_value=client)
        self.monitor._read_channel=AsyncMock()
        self.monitor.settings['channels']=['@example','@other','@group']
        asyncio.run(self.monitor._poll())
        self.monitor._read_channel.assert_awaited_once()
        self.assertEqual([r['state'] for r in self.monitor.snapshot()['channels']],['WATCHING','CHANNEL_NOT_JOINED','CHANNEL_NOT_JOINED'])

    def test_native_dispatch_validation_and_redaction(self):
        from p115tool.native_ui import dispatch
        plugin=NS(_lifecycle=threading.RLock(),_service=self.service)
        result=dispatch(plugin,'data',{'kind':'telegram'})
        self.assertNotIn('api_hash',result['settings'])
        with self.assertRaises(ValueError):dispatch(plugin,'action',{'action':'telegram_settings','settings':{'enabled':True}})
        with self.assertRaises(ValueError):dispatch(plugin,'action',{'action':'telegram_send_code','phone':'secret'})


if __name__=='__main__': unittest.main()
