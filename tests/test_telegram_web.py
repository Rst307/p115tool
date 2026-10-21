import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from p115tool.config import Config
from p115tool.service import Service
from p115tool.telegram import extract_shares, private_write
from p115tool.telegram_web import PublicChannelError, PublicChannelReader, PublicPage, parse_preview, MAX_PAGE_BYTES
from test_telegram import Drive, message


def preview(posts, *, username='example', more=False):
    older = '<a class="tme_messages_more" data-before="10" href="http://evil.test/"></a>' if more else ''
    messages = ''.join(f'<div class="tgme_widget_message" data-post="{username}/{ident}">'
        f'<div class="tgme_widget_message_text">{text}</div>{extra}</div>' for ident, text, extra in posts)
    return f'<html><main data-url="/{username}"><section class="tgme_channel_history">{older}{messages}</section></main></html>'


class PreviewParserTests(unittest.TestCase):
    def test_visible_hidden_utf16_and_button_links(self):
        html = preview([(10, '😀<a href="https://115.com/s/abcdef">点击链接</a><br/>提取码：ab12', ''),
            (11, 'https://115.com/s/ghijkl?password=cd34&amp;other=invalid', ''),
            (12, '资源', '<a class="tgme_widget_message_inline_button" href="https://115cdn.com/s/mnopqr?password=ef56">获取</a>')])
        page = parse_preview(html, 'example')
        self.assertEqual([m.id for m in page.messages], [10, 11, 12])
        self.assertEqual(extract_shares(page.messages[0]), ([('abcdef', 'ab12')], 0))
        self.assertEqual(extract_shares(page.messages[1]), ([], 0))
        self.assertEqual(extract_shares(page.messages[2]), ([('mnopqr', 'ef56')], 0))

    def test_unrelated_links_and_script_are_ignored(self):
        html = '<a href="https://115.com/s/aaaaaa">outside</a>' + preview([(10,
            '内容<script>https://115.com/s/bbbbbb</script><style>https://115.com/s/cccccc</style>', '')])
        self.assertEqual(extract_shares(parse_preview(html, 'example').messages[0]), ([], 0))

    def test_messages_sorted_and_empty_valid_feed(self):
        self.assertEqual([m.id for m in parse_preview(preview([(12,'',''),(10,'','')]),'example').messages], [10,12])
        self.assertEqual(parse_preview(preview([]),'example').messages, [])

    def test_wrong_channel_landing_page_and_truncation_rejected(self):
        for html in (preview([(1,'','')],username='other'), '<html>Open in Telegram</html>',
                     preview([(1,'hello','')]).split('hello')[0],
                     '<main data-url="/example"><section class="tgme_channel_history">'):
            with self.assertRaises(PublicChannelError): parse_preview(html,'example')

    def test_duplicate_ids_and_oversized_message_rejected(self):
        for html in (preview([(1,'',''),(1,'','')]), preview([(1,'a'*65537,'')])):
            with self.assertRaises(PublicChannelError): parse_preview(html,'example')


class PublicHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, **kwargs):
        reader=PublicChannelReader()
        reader.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
        try: return await reader.page('@example',**kwargs)
        finally: await reader.close()

    async def test_fixed_origin_after_cursor_and_no_auth(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,text=preview([(43,'','')]),headers={'content-type':'text/html'})
        result=await self.request(handler,after=42)
        self.assertEqual(result.messages[0].id,43)
        self.assertEqual(str(seen[0].url),'https://t.me/s/example?after=42')
        self.assertNotIn('authorization',seen[0].headers);self.assertNotIn('cookie',seen[0].headers)

    async def test_redirect_and_non_html_are_rejected(self):
        for response in (httpx.Response(302,headers={'location':'http://127.0.0.1/secret'}),
                         httpx.Response(200,json={'secret':'not html'})):
            calls=[]
            def handler(request): calls.append(request);return response
            with self.assertRaises(PublicChannelError): await self.request(handler)
            self.assertEqual(len(calls),1)

    async def test_response_size_bound(self):
        with self.assertRaises(PublicChannelError) as caught:
            await self.request(lambda request:httpx.Response(200,content=b'a'*(MAX_PAGE_BYTES+1),headers={'content-type':'text/html'}))
        self.assertEqual(caught.exception.code,'WEB_PAGE_TOO_LARGE')

    async def test_rate_limit_and_transport_errors_are_safe(self):
        with self.assertRaises(PublicChannelError) as caught:
            await self.request(lambda request:httpx.Response(429,headers={'retry-after':'3600'}))
        self.assertEqual(caught.exception.retry_after,3600)
        def fail(request): raise httpx.ConnectError('socks5://user:secret@proxy:1080')
        with self.assertRaises(PublicChannelError) as caught: await self.request(fail)
        self.assertEqual(str(caught.exception),'WEB_REQUEST_FAILED')

    async def test_http_date_retry_after_is_respected(self):
        with patch('p115tool.telegram_web.time.time',return_value=0):
            with self.assertRaises(PublicChannelError) as caught:
                await self.request(lambda request:httpx.Response(429,headers={'retry-after':'Thu, 01 Jan 1970 01:00:00 GMT'}))
        self.assertEqual(caught.exception.retry_after,3601)

    async def test_untrusted_channel_and_boundaries_fail_before_request(self):
        reader=PublicChannelReader()
        for ref,params in (('@example/../../secret',{}),('@example',{'before':1,'after':2}),('@example',{'after':-1})):
            with self.assertRaises(ValueError): await reader.page(ref,**params)
        self.assertIsNone(reader.client)


class PublicMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.config=Config(data_dir=str(root/'data'),strm_dir=str(root/'strm'))
        self.drive=Drive()
        self.service=Service(self.config,client=self.drive,virtual_organizer=lambda p:'电视剧/Example/Season 01/'+p)
        self.monitor=self.service.telegram

    def tearDown(self): self.service.close();self.temp.cleanup()
    def rows(self): return self.service.query('SELECT * FROM telegram_imports ORDER BY id')
    def cursor(self): return self.service.query("SELECT message_id FROM telegram_cursors WHERE channel='web:@example'",one=True)

    def test_default_web_mode_accepts_no_api_and_rejects_private_ids(self):
        self.assertEqual(self.monitor.snapshot()['settings']['mode'],'web')
        candidate=self.monitor._validate({'mode':'web','enabled':True,'channels':['https://t.me/s/Example']})
        self.assertEqual(candidate['channels'],['@example']);self.assertEqual(candidate['api_id'],0)
        with self.assertRaises(ValueError):self.monitor._validate({'mode':'web','channels':['-100123456']})
        with self.assertRaises(ValueError):self.monitor._validate({'mode':'account','enabled':True,'channels':['@example']})
        with self.assertRaises(ValueError):self.monitor.send_code('+8613800000000')

    def test_legacy_settings_stay_in_account_mode(self):
        private_write(self.monitor.settings_path,json.dumps({'enabled':False,'api_id':123,'api_hash':'a'*32,'channels':['-100123456']}))
        self.service.close();self.service=Service(self.config,client=self.drive);self.monitor=self.service.telegram
        self.assertEqual(self.monitor.settings['mode'],'account')
        self.assertEqual(self.monitor.settings['channels'],['-100123456'])

    def test_initial_boundary_skips_old_messages_without_import(self):
        self.monitor._web=NS(page=AsyncMock(return_value=PublicPage([message('',10),message('',12)])),close=AsyncMock())
        asyncio.run(self.monitor._read_public_channel('@example'))
        self.assertEqual(self.cursor()['message_id'],12);self.assertEqual(self.rows(),[])

    def test_incremental_pages_never_jump_over_large_backlog(self):
        self.monitor._cursor('web:@example',10)
        self.monitor._web=NS(page=AsyncMock(side_effect=[PublicPage([message('https://115.com/s/abcdef',11),message('',12)]),
            PublicPage([message('https://115.com/s/ghijkl',13),message('',14)])]),close=AsyncMock())
        asyncio.run(self.monitor._read_public_channel('@example'))
        asyncio.run(self.monitor._read_public_channel('@example'))
        self.assertEqual([r['message_id'] for r in self.rows()],[11,13]);self.assertEqual(self.cursor()['message_id'],14)
        self.assertEqual([call.kwargs for call in self.monitor._web.page.await_args_list],[{'after':10},{'after':12}])

    def test_first_history_walks_back_then_imports_oldest_first(self):
        self.monitor.settings['history_limit']=3
        self.monitor._web=NS(page=AsyncMock(side_effect=[PublicPage([message('',12),message('',13)],True),
            PublicPage([message('',10),message('https://115.com/s/abcdef',11)],True)]),close=AsyncMock())
        captured=[]
        with patch.object(self.monitor,'ingest',side_effect=lambda c,m:captured.append(m.id) or True):
            asyncio.run(self.monitor._read_public_channel('@example'))
        self.assertEqual(captured,[11,12,13]);self.assertEqual(self.cursor()['message_id'],13)
        self.assertEqual(self.monitor._web.page.await_args_list[1].kwargs,{'before':12})

    def test_backpressure_leaves_initial_boundary_for_next_poll(self):
        self.monitor.settings['history_limit']=2
        self.monitor._web=NS(page=AsyncMock(return_value=PublicPage([message('https://115.com/s/abcdef',11),message('',12)])),close=AsyncMock())
        with patch('p115tool.telegram.BACKLOG_LIMIT',0):asyncio.run(self.monitor._read_public_channel('@example'))
        self.assertEqual(self.cursor()['message_id'],10)

    def test_invalid_page_keeps_cursor_and_reports_channel(self):
        self.monitor._cursor('web:@example',10);self.monitor.settings['channels']=['@example']
        self.monitor._web=NS(page=AsyncMock(side_effect=PublicChannelError('WEB_CHANNEL_UNAVAILABLE')),close=AsyncMock())
        asyncio.run(self.monitor._poll())
        self.assertEqual(self.cursor()['message_id'],10)
        self.assertEqual(self.monitor.snapshot()['channels'],[{'channel':'@example','state':'WEB_CHANNEL_UNAVAILABLE'}])

    def test_no_progressing_history_is_not_silently_skipped(self):
        self.monitor.settings['history_limit']=3
        self.monitor._web=NS(page=AsyncMock(return_value=PublicPage([message('',12)],True)),close=AsyncMock())
        with self.assertRaises(PublicChannelError):asyncio.run(self.monitor._read_public_channel('@example'))
        self.assertIsNone(self.cursor())

    def test_web_poll_never_connects_telegram_account_and_generates_virtual_strm(self):
        self.monitor.settings['channels']=['@example'];self.monitor._cursor('web:@example',10)
        self.monitor._connect=AsyncMock(side_effect=AssertionError('Account must not be used'))
        self.monitor._web=NS(page=AsyncMock(return_value=PublicPage([message('https://115.com/s/abcdef?password=abcd',11)])),close=AsyncMock())
        asyncio.run(self.monitor._poll())
        if self.service.storage._thread:self.service.storage._thread.join(5)
        self.monitor._connect.assert_not_awaited()
        self.assertEqual(self.rows()[0]['state'],'DONE')
        self.assertEqual(self.service.query('SELECT kind FROM resource_storage'),[{'kind':'VIRTUAL'}])
        self.assertEqual([c[0] for c in self.drive.calls],['share_files'])
        self.assertEqual(len(list(Path(self.config.strm_dir).rglob('*.strm'))),1)

    def test_web_rate_wait_blocks_manual_poll_and_hides_secrets(self):
        self.monitor._failure(PublicChannelError('RATE_LIMITED',3600))
        until=self.monitor._next_poll
        self.monitor.poll_now()
        self.assertEqual(self.monitor._next_poll,until)
        self.assertEqual(self.monitor.snapshot()['error'],'RATE_LIMITED')


if __name__=='__main__':unittest.main()
