import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from p115client import P115OpenClient

from p115tool import P115Tool
from p115tool.client import P115ClientManager
from p115tool.config import Config
from p115tool.models import RemoteError, RemoteFile, SafetyError
from p115tool.native_ui import dispatch
from p115tool.service import Service


class MoviePilotAuthorizationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = Config.from_dict({
            'enabled': True, 'use_moviepilot_auth': True,
            'data_dir': str(self.root / 'data'), 'strm_dir': str(self.root / 'strm'),
            'source_cids': [{'cid': '10', 'prefix': '/电影'}],
        })
        self.host = SimpleNamespace(access_token='fake-native-authorization')
        name = 'app.modules.filemanager.storages.u115'
        module = ModuleType(name)
        module.U115Pan = Mock(return_value=self.host)
        self.enterContext(patch.dict(sys.modules, {name: module}))
        self.cookie_client = self.enterContext(patch('p115client.P115Client'))
        self.refresh = self.enterContext(patch.object(P115OpenClient, 'refresh_access_token'))
        self.transport = self.enterContext(patch('urllib3_future_request.request', side_effect=self.request))
        self.manager = P115ClientManager(self.config)
        self.addCleanup(self.manager.close)
        self.errors = {}
        self.link = 'https://cdn.115.com/example'
        self.link_id = '20'
        self.extra_root = [{'fc': '0', 'fid': '11', 'pid': '10', 'fn': 'Empty'}]
        self.copy_present = True

    def request(self, *, url, parse, **kwargs):
        path = urlsplit(url).path
        if path in self.errors:
            resp = self.errors[path]
        elif path == '/open/ufile/files':
            cid = kwargs['params']['cid']
            trail = [{'cid': 0, 'name': 'root'}, {'cid': 10, 'name': '电影'}]
            if cid == '11':
                trail.append({'cid': 11, 'name': 'Empty'})
                files = []
            else:
                files = list(self.extra_root)
                if self.copy_present:
                    files.append({'fc': '1', 'fid': '20', 'pid': '10', 'fn': 'Film.mkv',
                                  'fs': '8', 'sha': 'A' * 40, 'pc': 'pc20'})
            resp = {'state': True, 'count': len(files), 'path': trail,
                    'data': files[kwargs['params']['offset']:][:kwargs['params']['limit']]}
        elif path == '/open/folder/get_info':
            fid = kwargs['params']['file_id']
            if fid == '10':
                resp = {'state': True, 'data': {'file_id': '10', 'file_name': '电影',
                                                'file_category': '0', 'category_id': '1'}}
            else:
                self.assertEqual(fid, '20')
                resp = {'state': True, 'data': {'file_id': '20', 'file_name': 'Film.mkv', 'file_category': '1',
                                               'parent_id': '10', 'file_size': '8', 'pick_code': 'pc20', 'sha1': 'A' * 40}}
        elif path == '/open/ufile/downurl':
            self.assertEqual(kwargs['data'], {'pick_code': 'pc20'})
            resp = {'state': True, 'data': {self.link_id: {'pick_code': 'pc20', 'url': {'url': self.link}}}}
        else:
            self.fail(f'Unexpected request: {path}')
        return parse(None, json.dumps(resp, ensure_ascii=False).encode())

    def test_native_authorization_drives_the_picker_scan_and_existing_302_route(self):
        service = Service(self.config, self.manager)
        self.addCleanup(service.close)
        plugin = P115Tool()
        plugin._service = service
        plugin._config = self.config
        folders = dispatch(plugin, 'data', {'kind': 'folders', 'cid': '10'})
        self.assertEqual(folders, {'items': [{'cid': '11', 'name': 'Empty'}]})
        self.assertEqual(self.manager.stat('20').pickcode, 'pc20')
        service.generate_all()
        self.assertEqual(service.snapshot()['generated'], 1)
        output = self.root / 'strm/电影/Film.mkv.strm'
        content = output.read_text('utf-8').strip()

        self.host.access_token = 'fake-rotated-authorization'
        app = FastAPI()
        play = plugin.get_api()[0]
        app.add_api_route(play['path'], play['endpoint'], methods=play['methods'])
        with TestClient(app) as client:
            response = client.get('/play/' + content.split('/')[-1], headers={'User-Agent': 'Emby-test'},
                                  follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['location'], self.link)
        self.assertNotIn('authorization', response.headers)
        self.assertNotIn('set-cookie', response.headers)
        kwargs = self.transport.call_args.kwargs
        self.assertEqual(kwargs['headers']['authorization'], 'Bearer fake-rotated-authorization')
        self.assertEqual(kwargs['headers']['user-agent'], 'Emby-test')
        self.assertFalse(kwargs['follow_redirects'])
        self.cookie_client.assert_not_called()
        self.refresh.assert_not_called()

    def test_missing_or_rejected_host_auth_uses_no_cookie_and_never_refreshes_in_sdk(self):
        self.config.cookie = 'fake-cookie-for-existing-settings'
        self.host.access_token = None
        with self.assertRaisesRegex(RemoteError, 'authorization unavailable'):
            list(self.manager.list_files('10'))
        self.transport.assert_not_called()
        self.host.access_token = 'fake-native-authorization'
        self.errors['/open/ufile/files'] = {'state': False, 'errno': 40140125, 'error': 'private server value'}
        with self.assertRaises(RemoteError) as failure:
            list(self.manager.list_files('10'))
        self.assertEqual(failure.exception.upstream_code, 40140125)
        self.assertNotIn('private server value', str(failure.exception))
        self.transport.assert_called_once()
        self.cookie_client.assert_not_called()
        self.refresh.assert_not_called()

    def test_wrong_file_and_cookie_bound_links_never_redirect(self):
        self.link_id = '21'
        with self.assertRaisesRegex(SafetyError, 'identity mismatch'):
            self.manager.normal_link('pc20', 'Emby-test', file_id='20')
        self.link_id = '20'
        self.link += '?f=3'
        with self.assertRaisesRegex(SafetyError, 'requires cookies'):
            self.manager.normal_link('pc20', 'Emby-test', file_id='20')

    def virtual_service(self, stage='READY', file_id='20'):
        service = Service(self.config, self.manager)
        self.addCleanup(service.close)
        self.extra_root = []
        media_id = 'v_native_7'
        token = service.register(RemoteFile(media_id, 'Film.mkv', 8, 'A' * 40), '电影/Film.mkv')
        service.query("INSERT INTO resource_storage(media_id,kind,stage,share_fid) VALUES(?, 'VIRTUAL', 'READY', '7')",
                      (media_id,))
        service.query('INSERT INTO resource_copies(media_id,root_cid,folder_name,folder_cid,file_id,stage) VALUES(?,?,?,?,?,?)',
                      (media_id, '1', '电影', '10', file_id, stage))
        return service, media_id, token

    def test_native_302_reuses_a_verified_virtual_copy_with_the_existing_strm_token(self):
        service, media_id, token = self.virtual_service()
        plugin = P115Tool()
        plugin._service = service
        app = FastAPI()
        play = plugin.get_api()[0]
        app.add_api_route(play['path'], play['endpoint'], methods=play['methods'])
        with TestClient(app) as client:
            response = client.get('/play/' + token, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['location'], self.link)
        self.assertGreater(service.storage.copy(media_id)['lease_until'], 0)
        self.cookie_client.assert_not_called()

    def test_native_reuses_late_verified_copy_without_replaying_receive(self):
        service, media_id, token = self.virtual_service('RECEIVE_UNKNOWN', '')
        self.assertEqual(service.play(token, 'Emby-test'), self.link)
        self.assertEqual(service.storage.copy(media_id)['stage'], 'RECEIVE_UNKNOWN')
        self.cookie_client.assert_not_called()

    def test_native_no_live_virtual_copy_reports_transfer_requirement_without_writes(self):
        service, media_id, token = self.virtual_service()
        self.copy_present = False
        before = service.storage.copy(media_id)
        with self.assertRaisesRegex(SafetyError, 'Temporary copy must be transferred in 115 Cookie mode'):
            service.play(token, 'Emby-test')
        self.assertEqual(service.storage.copy(media_id), before)
        self.cookie_client.assert_not_called()

    def test_native_missing_folder_requires_transfer_and_keeps_checkpoint(self):
        service, media_id, token = self.virtual_service('RECEIVE_UNKNOWN', '')
        self.errors['/open/folder/get_info'] = {'state': False, 'code': 20004}
        before = service.storage.copy(media_id)
        with self.assertRaisesRegex(SafetyError, 'Temporary copy must be transferred in 115 Cookie mode'):
            service.play(token, 'Emby-test')
        self.transport.assert_called_once()
        self.assertEqual(service.storage.copy(media_id), before)

    def test_native_uses_verified_previous_edition_when_current_has_no_copy(self):
        service, media_id, token = self.virtual_service()
        selected = 'v_native_8'
        service.register(RemoteFile(selected, 'Film-4K.mkv', 9, 'B' * 40), '电影/Film.mkv')
        service.query("INSERT INTO resource_storage(media_id,kind,stage,share_fid) VALUES(?, 'VIRTUAL', 'READY', '8')",
                      (selected,))
        service.query('INSERT INTO virtual_selections VALUES(?,?,?,?)', (media_id, selected, media_id, 1))
        self.assertEqual(service.play(token, 'Emby-test'), self.link)
        self.assertIsNone(service.storage.copy(selected))
        self.assertGreater(service.storage.copy(media_id)['lease_until'], 0)

    def test_native_does_not_play_while_copy_deletion_is_unresolved(self):
        service, media_id, token = self.virtual_service('DELETE_UNKNOWN')
        with self.assertRaisesRegex(SafetyError, 'Temporary write outcome unresolved'):
            service.play(token, 'Emby-test')
        self.transport.assert_not_called()
        self.assertEqual(service.storage.copy(media_id)['stage'], 'DELETE_UNKNOWN')

    def test_native_does_not_play_a_replacement_for_a_recorded_ready_copy(self):
        service, media_id, token = self.virtual_service(file_id='21')
        with self.assertRaisesRegex(SafetyError, 'Temporary contents changed'):
            service.play(token, 'Emby-test')
        self.assertEqual(service.storage.copy(media_id)['lease_until'], 0)
        self.cookie_client.assert_not_called()

    def test_native_mode_pauses_cookie_jobs_and_preserves_checkpoints(self):
        self.config.recycle_empty_scheduled = True
        self.config.recycle_password = '000000'
        self.config.temp_cid = '99'
        directory = Path(self.config.data_dir)
        directory.mkdir()
        (directory / 'telegram-settings.json').write_text(
            json.dumps({'mode': 'web', 'enabled': True, 'channels': ['@example']}), 'utf-8')
        service = Service(self.config, self.manager)
        self.addCleanup(service.close)
        saved = {'id': 'batch', 'targets': '["31"]', 'inflight': '["31"]',
                 'total': 1, 'done': 0, 'updated_at': 1}
        service.storage.recycle.save(saved, 'UNKNOWN')
        for action, payload in (
            ('import', {'link': 'https://115.com/s/example', 'password': 'abcd'}),
            ('virtualize_all', {'delete_source': True}), ('cleanup', None),
            ('empty_recycle', None), ('reconcile_empty_recycle', None), ('reconcile_purge', None),
        ):
            with self.subTest(action=action):
                self.assertEqual(service.storage.start(action, payload), {'state': 'COOKIE_MODE_REQUIRED'})
        plugin = P115Tool()
        plugin._service = service
        plugin._config = self.config
        self.assertEqual([job['id'] for job in plugin.get_service()], ['p115tool_storage_refresh'])
        self.assertIsNone(plugin.run_recycle_empty())
        self.assertIsNone(plugin.run_cleanup())
        self.assertEqual(service.storage.recycle.pending()['inflight'], '["31"]')
        self.assertIsNone(service.telegram._thread)
        self.assertEqual(service.telegram.snapshot()['state'], 'COOKIE_MODE_REQUIRED')
        self.assertEqual(service.telegram.poll_now(), {'state': 'COOKIE_MODE_REQUIRED'})
        self.transport.assert_not_called()

    def test_existing_config_defaults_to_cookie_and_older_form_preserves_selected_mode(self):
        self.assertFalse(Config.from_dict({'cookie': 'fake-legacy-cookie'}).use_moviepilot_auth)
        with self.assertRaises(ValueError):
            Config.from_dict({'use_moviepilot_auth': 'true'})
        plugin = P115Tool()
        with patch.object(plugin, 'get_data_path', return_value=self.root), patch.object(plugin, 'update_config'):
            plugin.init_plugin({'use_moviepilot_auth': True})
            plugin.init_plugin({'enabled': False})
            self.assertTrue(plugin._config.use_moviepilot_auth)
