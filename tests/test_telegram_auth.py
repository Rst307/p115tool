import sys
import threading
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from fastapi import FastAPI, HTTPException, Header
from fastapi.testclient import TestClient
from p115tool.native_ui import routes


class TelegramAuthenticationTests(unittest.TestCase):
    def test_telegram_routes_require_active_administrator_before_body(self):
        module=types.ModuleType('app.db.user_oper')
        async def authenticate(authorization: str=Header(default='')):
            if not authorization: raise HTTPException(401)
            return NS(is_active=authorization!='inactive',is_superuser=authorization!='ordinary')
        module.get_current_active_superuser_async=authenticate
        monitor=NS(snapshot=lambda **kwargs:{'state':'DISABLED','settings':{'api_hash_set':False}},
                   send_code=lambda phone:{'state':'CODE_SENT'},
                   check_network=lambda:{'state':'NETWORK_CHECK_SCHEDULED'},
                   retry_all=lambda:{'state':'RETRY_BATCH_QUEUED','queued':1,'remaining':0})
        plugin=NS(_lifecycle=threading.RLock(),_service=NS(telegram=monitor))
        app=FastAPI()
        with patch.dict(sys.modules,{'app.db.user_oper':module}):
            for route in routes(plugin):
                route=dict(route);route.pop('allow_anonymous');route.pop('auth');app.add_api_route(**route)
        with TestClient(app) as client:
            for auth,expected in [('',401),('ordinary',403),('inactive',403),('administrator',200)]:
                headers={'authorization':auth} if auth else {}
                self.assertEqual(client.post('/native/data',json={'kind':'telegram'},headers=headers).status_code,expected)
                self.assertEqual(client.post('/native/data',json={'kind':'telegram','page':1,'state':'ALL','search':''},headers=headers).status_code,expected)
                self.assertEqual(client.post('/native/action',json={'action':'telegram_send_code','phone':'+8613800000000'},headers=headers).status_code,expected)
                self.assertEqual(client.post('/native/action',json={'action':'telegram_check_network'},headers=headers).status_code,expected)
                self.assertEqual(client.post('/native/action',json={'action':'telegram_retry_all'},headers=headers).status_code,expected)
            self.assertEqual(client.post('/native/action',json={'action':'telegram_retry_all','ids':[1]},headers={'authorization':'administrator'}).status_code,422)
            self.assertEqual(client.post('/native/action',json={'action':'telegram_check_network','proxy':'http://evil.test:80'},headers={'authorization':'administrator'}).status_code,422)
            self.assertEqual(client.post('/native/action',content='invalid secret body').status_code,401)


if __name__=='__main__': unittest.main()
