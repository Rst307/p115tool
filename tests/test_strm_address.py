import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from p115tool.config import Config
from p115tool.models import RemoteFile
from p115tool.service import Service


class Client:
    def list_files(self, cid):
        yield RemoteFile('1', 'episode.mkv', 10, 'ABC', cid, 'pc')
    def close(self):
        pass


class AddressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config.from_dict({'data_dir': str(self.root/'data'),
            'strm_dir': str(self.root/'strm'), 'public_url': 'http://127.0.0.1:8000',
            'source_cids': [{'cid': '10', 'prefix': '/电视剧/欧美剧'}]})
        self.service = Service(self.config, Client())
        self.addCleanup(self.service.close)
        self.service.generate_all()
        self.path = next((self.root/'strm').rglob('*.strm'))
        self.old = self.path.read_text('utf-8')
        self.config.public_url = 'http://192.168.0.2:3000'

    def test_changed_address_updates_owned_output_preserving_token(self):
        self.service.generate_all()
        self.assertEqual(self.service.snapshot()['failed'], 0,
                         'Owned STRM still points at the inaccessible old address')
        self.assertEqual(self.path.read_text('utf-8'), self.old.replace('127.0.0.1:8000', '192.168.0.2:3000'))

    def test_legacy_loopback_output_can_be_upgraded(self):
        # A 0.2.5 database has no output-content ownership fingerprints.
        self.service.query('DROP TABLE IF EXISTS strm_output_proofs')
        self.service.close()
        self.service = Service(self.config, Client())
        self.addCleanup(self.service.close)
        self.service.generate_all()
        self.assertEqual(self.service.snapshot()['failed'], 0)
        self.assertTrue(self.path.read_text('utf-8').startswith('http://192.168.0.2:3000/'))

    def test_manually_edited_output_is_preserved(self):
        edited = self.old.replace('127.0.0.1:8000', 'manual.example:8000')
        self.path.write_text(edited, 'utf-8')
        self.service.generate_all()
        self.assertEqual(self.path.read_text('utf-8'), edited)
        self.assertEqual(self.service.snapshot()['failed'], 1)

    def test_previous_recorded_path_is_required_for_legacy_update(self):
        self.service.query('DROP TABLE IF EXISTS strm_output_proofs')
        self.service.query('UPDATE strm_files SET strm_path=NULL')
        self.service.close()
        self.service = Service(self.config, Client())
        self.addCleanup(self.service.close)
        self.service.generate_all()
        self.assertEqual(self.path.read_text('utf-8'), self.old)
        self.assertEqual(self.service.snapshot()['failed'], 1)

    def test_replace_is_checkpointed_and_unknown_is_not_retried(self):
        def failed_replace(*args):
            self.assertEqual(self.service.query('SELECT state FROM strm_address_updates', one=True)['state'], 'PENDING')
            raise OSError('Synthetic replacement failure')
        with patch('p115tool.strm.os.replace', side_effect=failed_replace) as replace:
            self.service.generate_all()
            self.service.generate_all()
            self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.path.read_text('utf-8'), self.old)
        self.assertEqual(self.service.query('SELECT state FROM strm_address_updates', one=True)['state'], 'UNKNOWN')

    def test_second_address_change_survives_restart(self):
        self.service.generate_all()
        self.service.close()
        self.config.public_url='http://moviepilot.example:3000'
        self.service=Service(self.config, Client())
        self.addCleanup(self.service.close)
        self.service.generate_all()
        self.assertEqual(self.service.snapshot()['failed'], 0)
        self.assertTrue(self.path.read_text('utf-8').startswith('http://moviepilot.example:3000/'))

    def test_legacy_crlf_and_modified_token(self):
        self.service.query('DELETE FROM strm_output_proofs')
        self.path.write_bytes(self.old.replace('\n', '\r\n').encode('utf-8'))
        self.service.generate_all()
        self.assertEqual(self.service.snapshot()['failed'], 0)
        self.service.query('DELETE FROM strm_output_proofs')
        edited=self.old.rstrip('\n')+'edited\n'
        self.path.write_text(edited,'utf-8')
        self.service.generate_all()
        self.assertEqual(self.path.read_text('utf-8'),edited)
        self.assertEqual(self.service.snapshot()['failed'],1)

    def test_edit_during_checkpoint_is_preserved(self):
        query=self.service.query
        def edit_after_checkpoint(sql,*args,**kwargs):
            result=query(sql,*args,**kwargs)
            if sql.startswith('INSERT INTO strm_address_updates'):
                self.path.write_text('manual edit during checkpoint','utf-8')
            return result
        with patch.object(self.service,'query',side_effect=edit_after_checkpoint):
            self.service.generate_all()
        self.assertEqual(self.path.read_text('utf-8'),'manual edit during checkpoint')
        self.assertEqual(self.service.snapshot()['failed'],1)

    def test_local_address_refresh_updates_actual_and_virtual_without_remote_calls(self):
        # Add a virtual resource STRM alongside the actual resource STRM using the old public_url.
        self.config.public_url = 'http://127.0.0.1:8000'
        self.service.query(
            "INSERT INTO strm_files VALUES('v1','vtoken_12345678901234567890','','demo.mkv',10,'DEF','0','电影/外语电影/demo.mkv',NULL)"
        )
        self.service.query(
            "INSERT INTO resource_storage(media_id,kind,stage,share_code,password,share_fid) VALUES('v1','VIRTUAL','READY','sc1','1234','sf1')"
        )
        vpath = self.service.write_output('v1', '电影/外语电影/demo.mkv', 'vtoken_12345678901234567890')
        vold = Path(vpath).read_text('utf-8')
        # Add a third record whose local file is missing: must be skipped, not recreated.
        missing_path = self.root / 'strm' / '电影' / '外语电影' / 'deleted.mkv.strm'
        self.service.query(
            "INSERT INTO strm_files VALUES('m1','mtoken_12345678901234567890','pc2','deleted.mkv',10,'GHI','0','电影/外语电影/deleted.mkv',?)",
            (str(missing_path),),
        )
        self.config.public_url = 'https://mp.example.com:8443'

        class ForbiddenClient:
            def close(self):
                pass
            def __getattr__(self, name):
                raise AssertionError(f'Remote 115 call forbidden during local refresh: {name}')

        self.service.client = ForbiddenClient()
        self.service.refresh_addresses()
        snap = self.service.snapshot()
        self.assertEqual(snap['state'], 'DONE')
        self.assertEqual(snap['mode'], 'refresh_addresses')
        self.assertEqual(snap['found'], 3)
        self.assertEqual(snap['generated'], 2)
        self.assertEqual(snap['skipped'], 1)
        self.assertEqual(snap['failed'], 0)
        self.assertEqual(self.path.read_text('utf-8'), self.old.replace('http://127.0.0.1:8000', 'https://mp.example.com:8443'))
        self.assertEqual(Path(vpath).read_text('utf-8'), vold.replace('http://127.0.0.1:8000', 'https://mp.example.com:8443'))
        self.assertFalse(missing_path.exists())

    def test_local_address_refresh_preserves_manual_edits_and_dispatches_via_native_ui(self):
        from p115tool.native_ui import dispatch
        edited = self.old.replace('127.0.0.1:8000', 'manual.example:8000')
        self.path.write_text(edited, 'utf-8')
        plugin = type('Plugin', (), {'_lifecycle': self.service._lock, '_service': self.service, '_config': self.config})()
        snap = dispatch(plugin, 'action', {'action': 'refresh_strm_addresses'})
        self.assertIn(snap['state'], ('RUNNING', 'PARTIAL', 'DONE'))
        if self.service._worker:
            self.service._worker.join(timeout=5)
        final = self.service.snapshot()
        self.assertEqual(final['state'], 'PARTIAL')
        self.assertEqual(final['mode'], 'refresh_addresses')
        self.assertEqual(final['failed'], 1)
        self.assertEqual(final['error'], 'OUTPUT_CONFLICT')
        self.assertEqual(self.path.read_text('utf-8'), edited)
