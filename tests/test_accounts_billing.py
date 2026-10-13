import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import accounts
import payments
import server

ORIGIN = "http://testserver"


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "APP_ORIGIN": ORIGIN, "AUTH_SECRET": "test-only-" + "x" * 40,
            "ACCOUNT_DB_PATH": self.tmp.name + "/accounts.db", "ADMIN_USERS": "operator",
            "TURNSTILE_SITE_KEY": "site-test", "TURNSTILE_SECRET_KEY": "secret-test",
            "NOWPAYMENTS_API_KEY": "test-key", "NOWPAYMENTS_IPN_SECRET": "test-ipn-key",
            "NOWPAYMENTS_ENABLED": "1", "TRUSTED_PROXY_CIDRS": "", "APP_ENV": "test",
        })
        self.env.start()
        self.store = accounts.Store(os.getenv("TEST_DATABASE_URL", ""))
        self.saved = accounts.store
        accounts.store = self.store
        self.store.initialize()
        if self.store.pg:
            with self.store.transaction() as db:
                for table in ["payment_receipts", "issued_addresses", "orders", "sessions", "account_hits", "trial_claims", "rate_windows", "users"]:
                    db.execute("DELETE FROM " + table)
        server._runs.clear()
        self.captcha = patch("accounts.captcha", new=AsyncMock())
        self.mock_captcha = self.captcha.start()
        self.client = TestClient(server.app, client=('127.0.0.1', 50000))
        self.headers = {"Origin": ORIGIN}

    def tearDown(self):
        server._runs.clear()
        self.client.close()
        self.captcha.stop()
        accounts.store = self.saved
        self.env.stop()
        self.tmp.cleanup()

    def signup(self, name="firstuser", client=None):
        c = client or self.client
        r = c.post("/auth/signup", headers=self.headers, json={"username": name, "password": "correct horse 42", "captcha_token": "test"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def csrf(self, data):
        return {**self.headers, "X-CSRF-Token": data["csrf"]}

    def test_signed_out_denied_and_no_secret_source_routes(self):
        for path in ["/runs", "/hits", "/proxies", "/settings", "/auth/me", "/billing/orders", "/openapi.json", "/accounts.py", "/.env"]:
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/ui/auth.js").status_code, 200)

    def test_signup_hash_cookie_trial_and_login_logout(self):
        data = self.signup()
        self.assertTrue(data["user"]["access"])
        self.assertLessEqual(data["user"]["remaining_seconds"], 60)
        cookie = self.client.cookies.get(accounts.COOKIE)
        with self.store.transaction() as db:
            user = db.one("SELECT * FROM users WHERE id=?", (data["user"]["id"],))
            session = db.one("SELECT * FROM sessions WHERE user_id=?", (data["user"]["id"],))
        self.assertTrue(user["password_hash"].startswith("$argon2id$"))
        self.assertNotEqual(cookie, session["digest"])
        r = self.client.post("/auth/logout", headers=self.csrf(data))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        bad = self.client.post("/auth/login", headers=self.headers, json={"username":"firstuser", "password":"wrong password value", "captcha_token":"test"})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post("/auth/login", headers=self.headers, json={"username":"firstuser", "password":"correct horse 42", "captcha_token":"test"})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["user"]["trial_until"], user["trial_until"])

    def test_same_ip_and_rotated_ip_device_cannot_reset_trial(self):
        first = self.signup()
        second = self.signup("seconduser")
        self.assertFalse(second["user"]["access"])
        # Change IP alone: device is signed and still consumed.
        with patch("accounts.client_ip", return_value="198.51.100.99"):
            third = self.signup("thirduser")
        self.assertFalse(third["user"]["access"])
        # Clear device cookie but reuse original IP: still consumed.
        self.client.cookies.clear()
        fourth = self.signup("fourthuser")
        self.assertFalse(fourth["user"]["access"])
        self.assertGreater(first["user"]["trial_until"], 0)

    def test_expiry_csrf_origin_and_operator_boundary(self):
        data=self.signup()
        self.assertEqual(self.client.post("/hits", json={}).status_code, 403)
        self.assertEqual(self.client.delete("/hits", headers={"Origin":"https://foreign.example", "X-CSRF-Token":data["csrf"]}).status_code, 403)
        self.assertEqual(self.client.delete("/hits", headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post("/proxies/clear", headers=self.csrf(data)).status_code, 403)
        self.assertEqual(self.client.get("/proxies").json()["proxies"], [])
        with self.store.transaction() as db:
            db.execute("UPDATE users SET trial_until=0 WHERE id=?", (data["user"]["id"],))
        self.assertEqual(self.client.post("/scan", headers=self.csrf(data), json={}).status_code, 402)
        self.assertEqual(self.client.get("/snipe?username=foo").status_code, 402)
        self.assertEqual(self.client.get("/auth/me").json()["user"]["plan"], "expired")

    def test_run_hit_and_settings_are_private(self):
        first=self.signup()
        user=accounts.authenticate(self.client.cookies.get(accounts.COOKIE))
        token=accounts.CURRENT_USER.set(user)
        try:
            run=server.RunHandle("scan"); server._runs[run.id]=run
            server._record_hit("uniquehandle", "discord", "private detail", True, False, run.id)
        finally:
            accounts.CURRENT_USER.reset(token)
        self.assertEqual(len(self.client.get("/hits").json()["hits"]),1)
        response=self.client.post("/settings", headers=self.csrf(first), json={"webhook":"https://discord.com/api/webhooks/123/abcd", "enabled":True})
        self.assertEqual(response.status_code,200,response.text)
        other=TestClient(server.app, client=('127.0.0.1', 50000))
        second=self.signup("seconduser",other)
        self.assertEqual(other.get("/runs").json()["runs"],[])
        self.assertEqual(other.get("/runs/"+run.id).status_code,404)
        self.assertEqual(other.get("/runs/"+run.id+"/stream").status_code,404)
        self.assertEqual(other.get("/hits").json()["hits"],[])
        self.assertEqual(other.get("/settings").json()["alerts"]["webhook"],"")
        self.assertEqual(other.post("/runs/stop",headers=self.csrf(second)).json()["stopped"],[])
        self.assertFalse(run.stop.is_set())
        other.close()

    def test_reserved_operator_username_cannot_be_registered(self):
        r=self.client.post('/auth/signup',headers=self.headers,json={'username':'operator','password':'correct horse 42','captcha_token':'test'})
        self.assertEqual(r.status_code,409)

    def test_forwarded_ip_is_ignored_unless_peer_is_explicitly_trusted(self):
        from starlette.requests import Request
        scope={'type':'http','client':('127.0.0.1',50000),'headers':[(b'x-forwarded-for',b'198.51.100.20')],'method':'GET','path':'/','query_string':b''}
        req=Request(scope)
        self.assertEqual(accounts.client_ip(req),'127.0.0.1')
        with patch.dict(os.environ,{'TRUSTED_PROXY_CIDRS':'127.0.0.0/8'}):
            self.assertEqual(accounts.client_ip(req),'198.51.100.20')

    def test_body_limit_and_persistent_rate_limit(self):
        self.assertEqual(self.client.post('/auth/signup',content=b'x'*(1024*1024+1),headers=self.headers).status_code,413)
        accounts.rate_limit('unit-key',1,60)
        with self.assertRaises(Exception) as caught: accounts.rate_limit('unit-key',1,60)
        self.assertEqual(caught.exception.status_code,429)

    def order(self, data, oid="order1", pid="payment1", plan="monthly", currency="SOL", address="FreshSolanaAddress"):
        now=time.time()
        with self.store.transaction() as db:
            db.execute("INSERT INTO orders(id,user_id,plan,currency,price,created,expires,watch_until,provider_id,address,amount,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (oid,data['user']['id'],plan,currency,payments.PLANS[plan],now,now+3600,now+10800,pid,address,'0.1','waiting'))
        return {"payment_id":pid,"order_id":oid,"price_currency":"usd","price_amount":payments.PLANS[plan],"pay_currency":payments.COINS[currency][0],"pay_address":address,"payment_status":"finished","actually_paid":"0.1"}

    def callback(self, payload, signed=True):
        raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        sig=hmac.new(b'test-ipn-key',raw.encode(),hashlib.sha512).hexdigest()
        return self.client.post('/billing/webhook',content=raw,headers={'Content-Type':'application/json','x-nowpayments-sig':sig if signed else 'fake'})

    def test_forged_partial_unconfirmed_and_duplicate_payment(self):
        data=self.signup(); payload=self.order(data)
        self.assertEqual(self.callback(payload,False).status_code,401)
        self.assertEqual(self.callback({**payload,'payment_status':'confirming'}).status_code,200)
        self.assertEqual(self.client.get('/auth/me').json()['user']['paid_until'],0)
        self.assertEqual(self.callback({**payload,'actually_paid':'0.01'}).status_code,200)
        self.assertEqual(self.client.get('/auth/me').json()['user']['paid_until'],0)
        self.assertEqual(self.callback(payload).status_code,200)
        until=self.client.get('/auth/me').json()['user']['paid_until']
        self.assertGreater(until,time.time()+27*86400)
        self.assertEqual(self.callback(payload).status_code,200)
        self.assertEqual(self.client.get('/auth/me').json()['user']['paid_until'],until)
        self.assertEqual(self.callback({**payload,'price_amount':'0.01'}).status_code,400)

    def test_late_and_forever_payments_and_ownership(self):
        data=self.signup(); payload=self.order(data,plan='forever')
        with self.store.transaction() as db: db.execute('UPDATE orders SET expires=? WHERE id=?',(time.time()-1,'order1'))
        self.assertEqual(self.callback(payload).status_code,200)
        self.assertTrue(self.client.get('/auth/me').json()['user']['lifetime'])
        second=self.order(data,'order2','payment2')
        with self.store.transaction() as db: db.execute('UPDATE orders SET watch_until=? WHERE id=?',(time.time()-1,'order2'))
        self.assertEqual(self.callback(second).status_code,200)
        self.assertEqual(payments.get_order('order2')['status'],'review')
        other=TestClient(server.app, client=('127.0.0.1', 50000)); self.signup('seconduser',other)
        self.assertEqual(other.get('/billing/orders/order1').status_code,404)
        other.close()

    def test_payment_seen_in_window_can_finish_confirming_after_window(self):
        data=self.signup();payload=self.order(data)
        self.assertEqual(self.callback({**payload,'payment_status':'confirming'}).status_code,200)
        with self.store.transaction() as db:
            db.execute('UPDATE orders SET watch_until=? WHERE id=?',(time.time()-0.001,'order1'))
        self.assertEqual(self.callback(payload).status_code,200)
        self.assertEqual(payments.get_order('order1')['granted'],1)

    def test_provider_address_reuse_is_withheld_across_assets_and_expiry(self):
        data=self.signup()
        now=time.time()
        with self.store.transaction() as db:
            db.execute('INSERT INTO issued_addresses VALUES(?,?,?,?)',('solana','DuplicateAddress','old-expired-order',now-86400))
        async def fake(method,path,**kwargs):
            if path=='/currencies': return {'currencies':['usdtsol']}
            if path=='/min-amount': return {'min_amount':'0.01'}
            if path=='/estimate': return {'estimated_amount':'9'}
            return {'payment_id':'newpayment','pay_address':'DuplicateAddress','pay_amount':'9'}
        with patch('payments.provider',new=fake):
            r=self.client.post('/billing/checkout',headers=self.csrf(data),json={'plan':'monthly','currency':'USDT'})
        self.assertEqual(r.status_code,502,r.text)
        with self.store.transaction() as db:
            row=db.one('SELECT * FROM orders WHERE user_id=?',(data['user']['id'],))
            self.assertEqual(row['address'],'')
            self.assertEqual(row['status'],'review')
            self.assertEqual(len(db.all('SELECT * FROM issued_addresses')),1)

    def test_payments_fail_closed_without_explicit_opt_in(self):
        data=self.signup()
        with patch.dict(os.environ,{'NOWPAYMENTS_ENABLED':'0'}):
            r=self.client.post('/billing/checkout',headers=self.csrf(data),json={'plan':'monthly','currency':'SOL'})
        self.assertEqual(r.status_code,503)


class CaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_verifies_action_and_hostname_and_fails_closed(self):
        class Response:
            def __init__(self,data):self.data=data
            def raise_for_status(self):pass
            def json(self):return self.data
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,*args,**kwargs):return Response(result)
        result={'success':True,'hostname':'testserver','action':'signup'}
        with patch.dict(os.environ,{'APP_ORIGIN':ORIGIN,'TURNSTILE_SECRET_KEY':'test','TURNSTILE_SITE_KEY':'test'}),patch('accounts.httpx.AsyncClient',return_value=Client()):
            await accounts.captcha('token','signup','127.0.0.1')
            for bad in [{'success':False},{'success':True,'hostname':'evil.example','action':'signup'},{'success':True,'hostname':'testserver','action':'login'}]:
                result=bad
                with self.assertRaises(Exception) as caught:await accounts.captcha('token','signup','127.0.0.1')
                self.assertEqual(caught.exception.status_code,400)

    async def test_trial_expiry_cancels_a_detached_worker(self):
        user={'id':'guard-test','trial_until':time.time()+.1,'paid_until':0,'lifetime':0}
        token=accounts.CURRENT_USER.set(user)
        try:
            run=server.RunHandle('scan')
            async def lines():
                while True:
                    await asyncio.sleep(.02)
                    yield '{}\n'
            with patch.object(server.daily_stats,'record'), patch('accounts.access_deadline',return_value=0):
                server._detach(run,lines())
                await asyncio.sleep(.25)
                self.assertTrue(run.stop.is_set())
                self.assertTrue(run.task.done())
                self.assertFalse(run.active)
        finally: accounts.CURRENT_USER.reset(token)


if __name__=='__main__':unittest.main()
