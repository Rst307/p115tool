import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import sys

from p115tool.config import Config
from p115tool.models import RemoteFile, DownloadLink, MissingFile, SafetyError, ShareRejected, RemoteError
from p115tool.quality import quality, better, edition
from p115tool.service import Service
from p115tool.storage import parse_share
from p115tool.virtual_organize import VirtualPath, media_identity, organize_virtual


def organize(path):
    # Simulate the host recognizing the same series, but preserve filename quality.
    episode = '2' if 'E02' in path else '1'
    return VirtualPath('电视剧/Example/Season 01/' + Path(path).name,
                       'tv:123:1:' + episode if 'Unknown' not in path else '')


class Drive:
    def __init__(self):
        self.shares = {}; self.bad = set(); self.calls = []
    def close(self): pass
    def share_files(self, code, password, cid):
        self.calls.append(('list', code))
        if code in self.bad: raise ShareRejected('private payload')
        return self.shares[code] if cid == '0' else []
    def share_link(self, code, password, fid, ua):
        self.calls.append(('link', code, fid))
        return DownloadLink('https://example.invalid/private', {})
    def probe_range(self, link, ua, size): self.calls.append(('probe', size))
    def normal_link(self, pickcode, ua): return DownloadLink('https://example.invalid/' + pickcode, {})


class QualityTests(unittest.TestCase):
    def test_organizer_captures_host_identity_before_range_normalization(self):
        media=NS(tmdb_id=123,type=NS(name='TV'),category='国产剧')
        meta=NS(begin_season=1,begin_episode=1,end_season=None,end_episode=3,total_episode=3)
        context=NS(media_info=media,meta_info=meta)
        class Handler:
            def get_naming_dict(self,**kwargs): return {}
            def get_rename_path(self,path,**kwargs): return path/'Example'/'Season 01'/'Example.S01E01.mkv'
        modules={
            'app.chain.media':NS(MediaChain=lambda:NS(recognize_by_path=lambda **kw:context)),
            'app.core.config':NS(settings=NS(RENAME_FORMAT=lambda kind:'test')),
            'app.modules.filemanager.transhandler':NS(TransHandler=Handler),
            'app.schemas':NS(FileItem=NS),
        }
        with patch.dict(sys.modules,modules):
            result=organize_virtual('Example.S01E01-E03.1080p.WEB-DL.mkv')
            self.assertEqual(result.identity,'')
            meta.end_episode=1;meta.total_episode=1
            self.assertEqual(organize_virtual('Example.S01E01.1080p.WEB-DL.mkv').identity,'tv:123:1:1')

    def test_resolution_and_source_order(self):
        ordered = ['720p.HDTV','1080i.HDTV','1080p.HDTV','1080p.WEBRip','1080p.WEB-DL','1080p.BluRay','1080p.REMUX','2160p.WEB-DL']
        for left, right in zip(ordered, ordered[1:]): self.assertTrue(better(quality(right), quality(left)), (left, right))
        self.assertEqual(quality('4K.WEB-DL'), quality('2160p.WEB-DL'))
        self.assertFalse(better(quality('1080p.H265.100GB'), quality('1080p.H264.1GB')))
        self.assertFalse(better(quality('1080p.REMUX'), quality('Unknown')))
        self.assertFalse(better(quality('1080p.REMUX'), quality('1080p')))
        for value in ['4K.CAM', '2160p.sample', '4K/Film.1080p.mkv', '2160p.预告']:
            self.assertEqual(quality(value)[0], 0)

    def test_variants_and_ranges_are_distinct(self):
        self.assertNotEqual(edition('Extended.HDR.DV.国语'), edition('HDR'))
        self.assertNotEqual(edition('1080p'), edition('2160p.HDR'))
        media = NS(tmdb_id=123)
        self.assertEqual(media_identity(media, NS(), 'MOVIE'), 'movie:123')
        self.assertEqual(media_identity(media, NS(begin_season=1, begin_episode=2), 'TV'), 'tv:123:1:2')
        self.assertEqual(media_identity(media, NS(begin_season=1, begin_episode=2,end_episode=3), 'TV'), '')
        self.assertEqual(media_identity(media, NS(begin_season=1, begin_episode=2,total_episode=3), 'TV'), '')
        self.assertEqual(media_identity(media, NS(begin_season=1, begin_episode=None), 'TV'), '')
        self.assertEqual(media_identity(NS(tmdb_id=None), NS(), 'MOVIE'), '')

    def test_share_fragment_is_ignored_but_host_and_password_are_checked(self):
        self.assertEqual(parse_share('https://115cdn.com/s/abcdef?password=abcd#Film.2160p', ''), ('abcdef','abcd'))
        for url in ['https://115cdn.com.evil/s/abcdef#x','https://115cdn.com@evil/s/abcdef#x','https://115.com/s/abcdef?password=abcd&password=efgh#x']:
            with self.assertRaises(ValueError): parse_share(url, '')


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=self.temp.name+'/data', strm_dir=self.temp.name+'/strm')
        self.drive = Drive()
        self.service = Service(self.cfg, client=self.drive, virtual_organizer=organize)
        self.storage = self.service.storage

    def tearDown(self): self.service.close(); self.temp.cleanup()

    def add(self, code, quality='1080p.WEB-DL', episode='01'):
        file = RemoteFile('42', f'Example.S01E{episode}.{quality}.mkv', 100, hashlib.sha1(code.encode()).hexdigest().upper())
        self.drive.shares[code] = [file]
        return 'v_'+hashlib.sha256(code.encode()).hexdigest()[:24]+'_42'

    def run_import(self, code, enabled=True):
        job = {'state':'RUNNING', 'action':'import','done':0,'failed':0,'error':None}
        self.storage.import_share(code, 'abcd', job, auto_upgrade=enabled)
        return job

    def selection(self): return self.service.query('SELECT * FROM virtual_selections', one=True)

    def upgrade_pair(self):
        old = self.add('oldabc'); self.run_import('oldabc')
        row = self.storage.row(old); content = Path(row['strm_path']).read_bytes()
        new = self.add('newabc', '2160p.REMUX')
        job = self.run_import('newabc')
        self.assertEqual((job['done'],job['failed'],job['upgraded']), (1,0,1))
        return old,new,row,content

    def test_upgrade_preserves_token_strm_and_hides_candidate(self):
        old,new,row,content = self.upgrade_pair()
        self.assertEqual(self.selection()['selected_id'], new)
        self.assertEqual(self.selection()['previous_id'], old)
        self.assertEqual(self.storage.row(old)['token'], row['token'])
        self.assertEqual(Path(row['strm_path']).read_bytes(), content)
        self.assertIsNone(self.storage.row(new)['strm_path'])
        self.assertEqual(len(list(Path(self.cfg.strm_dir).rglob('*.strm'))), 1)
        self.assertEqual(self.storage.listing(kind='VIRTUAL')['total'], 1)
        self.assertEqual(self.service.snapshot()['count'], 1)
        self.assertEqual(len(self.storage.virtual_rows()), 1)
        self.assertEqual([x[0] for x in self.drive.calls if x[0] in ('link','probe')], ['link','probe'])
        self.service.generate_all()
        self.assertEqual(self.service.snapshot()['failed'], 0)
        self.assertEqual(len(list(Path(self.cfg.strm_dir).rglob('*.strm'))), 1)

    def test_lower_equal_and_unknown_never_downgrade(self):
        old,new,row,_ = self.upgrade_pair()
        for index, rank in enumerate(['720p.REMUX','2160p.REMUX','H265.100GB','1080p.REMUX']):
            code = 'lower'+str(index); self.add(code, rank)
            job = self.run_import(code)
            self.assertEqual((job['done'],job['skipped'],job['upgraded']), (1,1,0))
            self.assertEqual(self.selection()['selected_id'], new)
        self.assertEqual(self.service.query('SELECT count(*) n FROM strm_files',one=True)['n'],2)

    def test_other_episode_and_variant_remain_separate(self):
        self.add('oldabc');self.run_import('oldabc')
        for code, rank, ep in [('newep2','2160p.REMUX','02'), ('newhdr','2160p.HDR.REMUX','01'), ('newcut','2160p.Extended.REMUX','01')]:
            self.add(code,rank,ep);self.assertEqual(self.run_import(code)['upgraded'],0)
        self.assertIsNone(self.selection())
        self.assertEqual(len(self.storage.virtual_rows()),4)

    def test_invalid_share_does_not_change_mapping(self):
        old = self.add('oldabc');self.run_import('oldabc')
        self.add('newabc','2160p.REMUX')
        with patch.object(self.drive,'probe_range',side_effect=ShareRejected('private payload')):
            job = self.run_import('newabc')
        self.assertEqual(job['failed'],1);self.assertIsNone(self.selection())
        self.assertEqual(len(self.storage.virtual_rows()),1)
        self.assertNotIn('private payload',json.dumps(job))

    def test_edited_strm_and_unknown_copy_checkpoint_block_switch(self):
        old = self.add('oldabc');self.run_import('oldabc')
        row = self.storage.row(old); path = Path(row['strm_path']); content=path.read_bytes()
        self.add('newabc','2160p.REMUX');path.write_text('edited\n')
        self.assertEqual(self.run_import('newabc')['failed'],1)
        self.assertEqual(path.read_text(),'edited\n');self.assertIsNone(self.selection())
        path.write_bytes(content)
        self.service.query("INSERT INTO resource_copies VALUES(?,?,?,'','','RECEIVE_UNKNOWN',0,0)", (old,'1','test'))
        self.assertEqual(self.run_import('newabc')['failed'],1)
        self.assertIsNone(self.selection())

    def test_copies_keep_original_identity_and_play_routes_to_new(self):
        old,new,row,_ = self.upgrade_pair()
        self.service.query("INSERT INTO resource_copies VALUES(?,?,?,'2','3','READY',1,9999999999)", (old,'1','old'))
        with patch.object(self.storage,'ensure_copy',return_value=RemoteFile('99','new.mkv',100,'A'*40,pickcode='new-pc')) as ensure:
            url = self.service.play(row['token'],'test-ua')
        ensure.assert_called_once_with(new)
        self.assertTrue(url.endswith('/new-pc'))
        self.assertEqual(self.storage.copy(old)['file_id'],'3')
        self.assertEqual(self.storage.copy(old)['lease_until'],9999999999)

    def test_preflight_failure_uses_previous_but_transfer_failure_does_not(self):
        old,new,row,_ = self.upgrade_pair();self.drive.bad.add('newabc')
        with patch.object(self.storage,'ensure_copy',return_value=RemoteFile('99','old.mkv',100,'A'*40,pickcode='old-pc')) as ensure:
            self.assertTrue(self.service.play(row['token'],'ua').endswith('/old-pc'))
        ensure.assert_called_once_with(old)
        self.drive.bad.clear()
        with patch.object(self.storage,'ensure_copy',side_effect=SafetyError('Temporary transfer outcome unknown')) as ensure:
            with self.assertRaises(SafetyError): self.service.play(row['token'],'ua')
        ensure.assert_called_once_with(new)

    def test_session_failure_never_attempts_previous(self):
        old,new,row,_ = self.upgrade_pair()
        error = RemoteError('private');error.upstream_code=990001
        with patch.object(self.storage,'verify_share',side_effect=error) as verify:
            with self.assertRaises(RemoteError): self.service.play(row['token'],'ua')
        self.assertEqual(verify.call_count,1)

    def test_existing_copy_skips_share_preflight(self):
        old,new,row,_ = self.upgrade_pair()
        self.service.query("INSERT INTO resource_copies VALUES(?,?,?,'2','3','READY',1,0)", (new,'1','new'))
        self.drive.bad.add('newabc')
        with patch.object(self.storage,'ensure_copy',return_value=RemoteFile('99','new.mkv',100,'A'*40,pickcode='new-pc')):
            self.assertTrue(self.service.play(row['token'],'ua').endswith('/new-pc'))

    def test_reimport_hidden_edition_does_not_emit_duplicate(self):
        old,new,row,content=self.upgrade_pair()
        self.assertEqual(self.run_import('newabc',enabled=False)['skipped'],1)
        self.assertEqual(len(list(Path(self.cfg.strm_dir).rglob('*.strm'))),1)

    def test_disabled_uses_original_import_behavior(self):
        self.add('oldabc');self.run_import('oldabc')
        self.add('newabc','2160p.REMUX')
        self.assertEqual(self.run_import('newabc',enabled=False)['upgraded'],0)
        self.assertIsNone(self.selection())
        self.assertEqual(len(self.storage.virtual_rows()),2)

    def test_legacy_index_and_restart_persist_selection(self):
        old = self.add('oldabc');self.run_import('oldabc')
        self.service.query('DELETE FROM virtual_editions')
        new = self.add('newabc','2160p.REMUX');self.assertEqual(self.run_import('newabc')['upgraded'],1)
        self.service.close()
        self.service = Service(self.cfg,client=self.drive,virtual_organizer=organize);self.storage=self.service.storage
        self.assertEqual(self.selection()['selected_id'],new)
        self.assertEqual(len(self.storage.virtual_rows()),1)

    def test_sequential_upgrades_compare_with_selected_quality(self):
        old=self.add('oldabc','720p.WEB-DL');self.run_import('oldabc')
        middle=self.add('middle','1080p.WEB-DL');self.assertEqual(self.run_import('middle')['upgraded'],1)
        new=self.add('newabc','2160p.REMUX');self.assertEqual(self.run_import('newabc')['upgraded'],1)
        self.assertEqual(self.selection()['previous_id'],middle)
        self.assertEqual(self.selection()['selected_id'],new)
        self.assertEqual(self.selection()['stable_id'],old)
        self.assertEqual(len(self.storage.virtual_rows()),1)

    def test_season_upgrades_enumerate_share_once_and_validate_each_member(self):
        self.add('oldabc');self.run_import('oldabc')
        self.add('olddef',episode='02');self.run_import('olddef')
        self.add('newabc','2160p.REMUX')
        self.drive.shares['newabc'].append(RemoteFile('43','Example.S01E02.2160p.REMUX.mkv',200,'B'*40))
        self.drive.calls.clear()
        self.assertEqual(self.run_import('newabc')['upgraded'],2)
        self.assertEqual([x for x in self.drive.calls if x==('list','newabc')],[('list','newabc')])
        self.assertEqual(len([x for x in self.drive.calls if x[0]=='probe']),2)

    def test_backup_ready_copy_can_play_when_both_shares_are_unavailable(self):
        old,new,row,_=self.upgrade_pair()
        self.service.query("INSERT INTO resource_copies VALUES(?,?,?,'2','3','READY',1,0)",(old,'1','old'))
        self.drive.bad.update(('oldabc','newabc'))
        with patch.object(self.storage,'ensure_copy',return_value=RemoteFile('99','old.mkv',100,'A'*40,pickcode='backup')) as ensure:
            self.assertTrue(self.service.play(row['token'],'ua').endswith('/backup'))
        ensure.assert_called_once_with(old)

    def test_upgrade_setting_is_strict_and_preserved_by_old_forms(self):
        monitor=self.service.telegram
        settings=monitor._validate({'auto_upgrade':False})
        monitor.settings=settings
        self.assertFalse(monitor._validate({})['auto_upgrade'])
        self.assertFalse(monitor.snapshot()['settings']['auto_upgrade'])
        with self.assertRaises(ValueError):monitor._validate({'auto_upgrade':'false'})

    def test_ambiguous_existing_duplicates_are_not_arbitrarily_replaced(self):
        self.add('oldabc');self.run_import('oldabc',enabled=False)
        self.add('olddef','1080p.BluRay');self.run_import('olddef',enabled=False)
        self.add('newabc','2160p.REMUX');self.assertEqual(self.run_import('newabc')['upgraded'],0)
        self.assertIsNone(self.selection())

    def test_sql_failure_rolls_back_entire_switch(self):
        self.add('oldabc');self.run_import('oldabc')
        self.service.query("CREATE TRIGGER fail_selection BEFORE INSERT ON virtual_selections BEGIN SELECT RAISE(ABORT,'test'); END")
        new=self.add('newabc','2160p.REMUX')
        self.assertEqual(self.run_import('newabc')['failed'],1)
        self.assertIsNone(self.storage.row(new));self.assertIsNone(self.selection())

    def test_import_queue_passes_upgrade_option_and_counts_to_telegram(self):
        old=self.add('oldabc');self.run_import('oldabc')
        self.add('newabc','2160p.REMUX')
        monitor=self.service.telegram
        monitor.ingest('@example',NS(message='https://115.com/s/newabc?password=abcd#Example',id=42,entities=[],reply_markup=None))
        monitor._drain()
        worker=self.storage._thread
        if worker: worker.join(5)
        monitor._sync_tasks()
        item=monitor.snapshot()['items'][0]
        self.assertEqual((item['state'],item['upgraded'],item['skipped']),('DONE',1,0))
        self.assertEqual(self.selection()['stable_id'],old)
