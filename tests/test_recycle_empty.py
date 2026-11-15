import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
if (ROOT/'plugins.v2').is_dir(): sys.path.insert(0,str(ROOT/'plugins.v2'))
from p115tool import P115Tool
from p115tool.client import P115ClientManager
from p115tool.config import Config
from p115tool.models import RemoteError, SafetyError, RemoteFile
from p115tool.native_ui import dispatch
from p115tool.storage import StorageManager
from p115tool.service import Service

PRIVATE='PRIVATE_TEST_RESPONSE'


class FakeDrive:
    def __init__(self):
        self.calls=[]
        self.files={'1':RemoteFile('1','fixture.mkv',100,'A'*40,'10','fixture')}
    def close(self): pass


class RecycleEmptyTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        self.config=Config(enabled=True,data_dir=str(root/'data'),strm_dir=str(root/'strm'))
        self.drive=FakeDrive();self.service=Service(self.config,self.drive)
        self.addCleanup(self.service.close);self.storage=self.service.storage

    def enable(self, count=2):
        self.config.recycle_empty_scheduled=True;self.config.recycle_password='123456'
        # Web recycle metadata lacks original file ID/SHA1. Whole-bin opt-in
        # deliberately includes historical files, unlike targeted purge.
        self.bin={str(n):{'rid':str(n),'name':'old'} for n in range(1,count+1)}
        self.drive.recycle_entries=lambda:list(self.bin.values())
        def purge(rids):
            run=self.storage.recycle.pending()
            self.assertEqual(run['stage'],'PURGING')
            self.assertEqual(json.loads(run['inflight']),rids)
            self.assertTrue(set(rids)<=set(json.loads(run['targets'])))
            self.drive.calls.append(('empty',list(rids)))
            for rid in rids:self.bin.pop(rid,None)
        self.drive.purge_recycle_batch=purge

    def run_empty(self, action='empty_recycle'):
        result=self.storage.start(action)
        worker=self.storage._thread
        if worker:worker.join(5)
        self.assertFalse(self.storage.busy)
        return result

    def test_default_off_and_stale_callbacks_do_no_remote_work(self):
        self.drive.recycle_entries=lambda:self.fail('Disabled must not read bin')
        self.run_empty()
        plugin=P115Tool();plugin._config=self.config;plugin._service=self.service
        self.assertNotIn('p115tool_recycle_empty',[job['id'] for job in plugin.get_service()])
        with patch.object(self.storage,'start') as start:
            plugin.run_recycle_empty();start.assert_not_called()
        self.assertEqual(self.storage.recycle.status()['stage'],'IDLE')

    def test_whole_snapshot_deletes_history_in_batches_and_keeps_new_entries(self):
        self.enable(205);original=self.drive.purge_recycle_batch
        source_files=dict(self.drive.files)
        def purge(rids):
            original(rids);self.bin['999']={'rid':'999','name':'arrived later'}
        self.drive.purge_recycle_batch=purge
        self.run_empty()
        self.assertEqual([len(c[1]) for c in self.drive.calls],[100,100,5])
        self.assertEqual(set(self.bin),{'999'})
        self.assertEqual(self.drive.files,source_files)
        status=self.storage.recycle.status()
        self.assertEqual((status['stage'],status['done'],status['total']),('DONE',205,205))

    def test_empty_bin_never_sends_empty_target(self):
        self.enable(0);self.run_empty()
        self.assertEqual(self.drive.calls,[])
        self.assertEqual(self.storage.recycle.status()['stage'],'DONE')

    def test_timeout_blocks_retries_across_restart_and_recheck_is_read_only(self):
        self.enable()
        def timeout(rids):
            self.drive.calls.append(('empty',rids));raise RemoteError(PRIVATE)
        self.drive.purge_recycle_batch=timeout
        self.run_empty();self.assertEqual(self.storage.recycle.status()['stage'],'UNKNOWN')
        self.storage=StorageManager(self.service);self.service.storage=self.storage
        self.run_empty();self.run_empty('reconcile_empty_recycle')
        self.assertEqual(len(self.drive.calls),1)
        self.assertTrue(self.storage.recycle.status()['blocked'])
        self.assertNotIn(PRIVATE,str(self.storage.job()))
        self.assertNotIn('123456',str(self.storage.recycle.status()))
        # Even after all unknown targets disappear, this timer tick only reads.
        self.bin.clear();self.bin['999']={'rid':'999'}
        self.run_empty()
        self.assertEqual(len(self.drive.calls),1)
        self.assertEqual(self.storage.recycle.status()['stage'],'VERIFIED')
        self.assertEqual(set(self.bin),{'999'})

    def test_success_response_with_remaining_target_stays_unknown(self):
        self.enable();self.drive.purge_recycle_batch=lambda rids:self.bin.pop(rids[0])
        self.run_empty()
        self.assertEqual(self.storage.recycle.status()['stage'],'UNKNOWN')
        self.run_empty('reconcile_empty_recycle')
        self.assertTrue(self.storage.recycle.status()['blocked'])

    def test_post_delete_read_failure_keeps_inflight_checkpoint(self):
        self.enable();original=self.drive.purge_recycle_batch
        def purge(rids):
            original(rids)
            self.drive.recycle_entries=lambda:(_ for _ in ()).throw(RemoteError(PRIVATE))
        self.drive.purge_recycle_batch=purge
        self.run_empty()
        pending=self.storage.recycle.pending()
        self.assertEqual(set(json.loads(pending['inflight'])),{'1','2'})
        self.run_empty()
        self.assertEqual(len(self.drive.calls),1)
        self.assertNotIn(PRIVATE,str(self.storage.job()))

    def test_failure_saving_verified_batch_does_not_lose_unknown_targets(self):
        self.enable();query=self.service.query;failed=False
        def failing(sql,args=(),one=False):
            nonlocal failed
            if sql.startswith('INSERT OR REPLACE INTO recycle_empty_runs') and args[1]=='PREPARED' and args[5]>0 and not failed:
                failed=True;raise OSError('Simulated disk failure')
            return query(sql,args,one)
        with patch.object(self.service,'query',side_effect=failing):self.run_empty()
        self.assertEqual(self.storage.recycle.pending()['stage'],'UNKNOWN')
        self.assertEqual(set(json.loads(self.storage.recycle.pending()['inflight'])),{'1','2'})
        self.run_empty('reconcile_empty_recycle')
        self.assertEqual(self.storage.recycle.status()['done'],2)

    def test_restart_purging_is_unknown_and_prepared_is_cancelled(self):
        self.enable()
        for key,stage in [('old','PREPARED'),('active','PURGING')]:
            self.service.query('INSERT INTO recycle_empty_runs VALUES(?,?,?,?,?,?,?,?)',
                (key,stage,'["1"]','["1"]',1,0,1,''))
        self.storage=StorageManager(self.service)
        self.assertEqual(self.service.query("SELECT stage FROM recycle_empty_runs WHERE id='old'",one=True)['stage'],'CANCELLED')
        self.assertEqual(self.storage.recycle.pending()['stage'],'UNKNOWN')
        self.storage.recycle.empty({'done':0,'failed':0})
        self.assertEqual(self.drive.calls,[])

    def test_checkpoint_failure_before_request_never_deletes(self):
        self.enable();save=self.storage.recycle.save
        def fail(run,stage,error=''):
            if stage=='PURGING':raise OSError('Disk unavailable')
            return save(run,stage,error)
        with patch.object(self.storage.recycle,'save',side_effect=fail):self.run_empty()
        self.assertEqual(self.drive.calls,[])

    def test_listing_failure_never_deletes_and_status_reads_only_local_data(self):
        self.enable()
        self.drive.recycle_entries=lambda:(_ for _ in ()).throw(RemoteError(PRIVATE))
        self.run_empty()
        self.assertEqual(self.storage.recycle.status()['stage'],'FAILED')
        plugin=P115Tool();plugin._service=self.service
        self.assertNotIn(PRIVATE,str(dispatch(plugin,'data',{'kind':'recycle_empty'})))
        self.assertEqual(self.drive.calls,[])
        # No unaudited management route for immediate whole-bin clearing.
        with self.assertRaises(ValueError):dispatch(plugin,'action',{'action':'empty_recycle'})

    def test_targeted_pending_checkpoint_blocks_empty_and_unknown_empty_blocks_targeted_purge(self):
        self.enable();self.service.query("INSERT INTO recycle_purge_intents VALUES('old','1','','UNKNOWN')")
        self.run_empty();self.assertEqual(self.drive.calls,[])
        self.assertEqual(self.storage.recycle.status()['stage'],'BLOCKED')
        self.service.query('DELETE FROM recycle_purge_intents')
        self.service.query("INSERT INTO recycle_empty_runs VALUES('pending','UNKNOWN','[\"1\"]','[\"1\"]',1,0,9999999999,'')")
        self.config.recycle_purge=True
        with self.assertRaises(SafetyError):self.storage.prepare_purge(self.drive.files['1'])

    def test_busy_scan_skips_timer_without_queuing_destructive_work(self):
        self.enable()
        fake=Mock();fake.is_alive.return_value=True
        self.storage._thread=fake
        self.storage._job({'state':'RUNNING','action':'scan'})
        self.assertEqual(self.storage.start('empty_recycle'),{'state':'BUSY'})
        self.assertIsNone(self.storage._pending_action)
        self.storage._thread=None

    def test_stop_between_batches_preserves_history_without_submitting_more(self):
        self.enable(101);original=self.drive.purge_recycle_batch
        def purge(rids):original(rids);self.service._stop.set()
        self.drive.purge_recycle_batch=purge
        self.run_empty()
        self.assertEqual(len(self.drive.calls),1)
        self.assertEqual(self.storage.recycle.status()['stage'],'CANCELLED')
        self.assertEqual(self.storage.job()['state'],'INTERRUPTED')
        self.assertEqual(len(self.bin),1)


class RecycleEmptyConfigAndSDKTests(unittest.TestCase):
    def test_strict_config_key_and_time(self):
        self.assertFalse(Config.from_dict({}).recycle_empty_scheduled)
        for value in ('true',1,None):
            with self.assertRaises(ValueError):Config.from_dict({'recycle_empty_scheduled':value})
        for value in ('4:00','24:00','04:60',None):
            with self.assertRaises(ValueError):Config.from_dict({'recycle_empty_time':value})
        with self.assertRaises(ValueError):Config.from_dict({'recycle_empty_scheduled':True})
        cfg=Config.from_dict({'recycle_empty_scheduled':True,'recycle_password':'123456','recycle_empty_time':'23:59'})
        plugin=P115Tool();plugin._config=cfg;plugin._config.enabled=True;plugin._service=Mock()
        job=next(job for job in plugin.get_service() if job['id']=='p115tool_recycle_empty')
        self.assertEqual(str(job['trigger'].timezone),'Asia/Shanghai')
        self.assertIn("hour='23', minute='59'",str(job['trigger']))
        plugin.run_recycle_empty();plugin._service.storage.start.assert_called_once_with('empty_recycle')
        plugin._config.enabled=False;plugin.run_recycle_empty()
        plugin._service.storage.start.assert_called_once()

    def test_sdk_explicit_bounded_targets_key_and_no_retry(self):
        cfg=Config(recycle_password='123456');sdk=Mock()
        sdk.recyclebin_clean.return_value={'state':True}
        manager=P115ClientManager(cfg,sdk)
        for rids in ([],[''],['0'],['1','1'],['1,2'],['1']*101,None,[1]):
            with self.assertRaises((SafetyError,TypeError)):manager.purge_recycle_batch(rids)
        sdk.recyclebin_clean.assert_not_called()
        manager.purge_recycle_batch(['1','2'])
        self.assertEqual(sdk.recyclebin_clean.call_args.args,({'tid':'1,2'},))
        self.assertEqual(sdk.recyclebin_clean.call_args.kwargs['password'],'123456')
        sdk.recyclebin_clean.side_effect=RuntimeError(PRIVATE)
        with self.assertRaises(RemoteError) as error:manager.purge_recycle_batch(['3'])
        self.assertNotIn(PRIVATE,str(error.exception))
        self.assertEqual(sdk.recyclebin_clean.call_count,2)

    def test_changing_pagination_count_is_rejected(self):
        sdk=Mock();sdk.recyclebin_list.side_effect=[
            {'state':True,'data':[{'id':'1'}],'count':2},
            {'state':True,'data':[{'id':'2'},{'id':'3'}],'count':3}]
        manager=P115ClientManager(Config(),sdk)
        with self.assertRaises(SafetyError):manager.recycle_entries()
