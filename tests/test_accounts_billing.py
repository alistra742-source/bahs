import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import accounts
import payments
import server
import wallets

ORIGIN = "http://testserver"


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "APP_ORIGIN": ORIGIN, "AUTH_SECRET": "test-only-" + "x" * 40,
            "ACCOUNT_DB_PATH": self.tmp.name + "/accounts.db", "ADMIN_USERS": "operator",
            "NOWPAYMENTS_API_KEY": "test-key", "NOWPAYMENTS_IPN_SECRET": "test-ipn-key",
            "NOWPAYMENTS_ENABLED": "1", "TRUSTED_PROXY_CIDRS": "", "APP_ENV": "test",
            # One payout wallet per coin, exactly as the Railway variables would
            # supply them. Literal addresses here; the recovery-phrase path has
            # its own test below.
            "BTC": "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
            "LTC": "ltc1quxjdrku0dg9mrm96rczdh55cdltf5z2psa79cp",
            "ETH": "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            "USDC": "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            "SOL": "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk",
            "USDT": "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk",
        })
        wallets._cache.clear()
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
        self.client = TestClient(server.app, client=('127.0.0.1', 50000))
        self.headers = {"Origin": ORIGIN}

    def tearDown(self):
        server._runs.clear()
        self.client.close()
        accounts.store = self.saved
        self.env.stop()
        self.tmp.cleanup()

    def solve(self, client, action):
        # Read the answer the way a test harness can: from the server-side challenge store.
        ch = client.get("/auth/challenge?action=" + action).json()
        with accounts.store.transaction() as db:
            entry = db.one("SELECT word,target FROM challenges WHERE id=?", (ch["id"],))
        return {"challenge_id": ch["id"], "drag": entry["target"], "word": entry["word"]}

    def signup(self, name="firstuser", client=None):
        c = client or self.client
        r = c.post("/auth/signup", headers=self.headers, json={"username": name, "password": "correct horse 42", **self.solve(c, "signup")})
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
        bad = self.client.post("/auth/login", headers=self.headers, json={"username":"firstuser", "password":"wrong password value", **self.solve(self.client, "login")})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post("/auth/login", headers=self.headers, json={"username":"firstuser", "password":"correct horse 42", **self.solve(self.client, "login")})
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
        # The first account holds its trial, unstarted: nothing has run yet.
        self.assertTrue(first["user"]["trial_pending"])
        self.assertEqual(first["user"]["trial_until"], 0)

    def test_trial_starts_on_the_first_run_not_at_signup(self):
        data = self.signup()
        uid = data["user"]["id"]
        self.assertTrue(data["user"]["access"], "an unstarted trial still gets in")
        self.assertTrue(data["user"]["trial_pending"])
        self.assertEqual(data["user"]["plan"], "trial")
        self.assertEqual(data["user"]["remaining_seconds"], accounts.TRIAL_SECONDS)
        with self.store.transaction() as db:
            row = db.one("SELECT trial_until,trial_started,trial_available FROM users WHERE id=?", (uid,))
        self.assertEqual((row["trial_until"], row["trial_started"]), (0.0, 0.0))

        user = accounts.authenticate(self.client.cookies.get(accounts.COOKIE))
        token = accounts.CURRENT_USER.set(user)
        try:
            asyncio.run(server._open_run("scan"))
        finally:
            accounts.CURRENT_USER.reset(token)
        with self.store.transaction() as db:
            started = db.one("SELECT trial_until,trial_started,trial_available FROM users WHERE id=?", (uid,))
        self.assertGreater(started["trial_started"], 0, "the first run starts the clock")
        self.assertAlmostEqual(started["trial_until"] - started["trial_started"], accounts.TRIAL_SECONDS, places=2)

        # A second run reuses the running clock instead of extending it.
        token = accounts.CURRENT_USER.set(accounts.authenticate(self.client.cookies.get(accounts.COOKIE)))
        try:
            asyncio.run(server._open_run("snipe"))
        finally:
            accounts.CURRENT_USER.reset(token)
        with self.store.transaction() as db:
            again = db.one("SELECT trial_until,trial_started FROM users WHERE id=?", (uid,))
        self.assertEqual(again["trial_started"], started["trial_started"])
        self.assertEqual(again["trial_until"], started["trial_until"])
        server._runs.clear()

    def test_checkout_pays_out_to_the_wallet_in_the_environment(self):
        phrase = " ".join(["abandon"] * 11 + ["about"])
        captured = {}
        data = self.signup()
        with patch.dict(os.environ, {"BTC": phrase}):
            wallets._cache.clear()
            async def fake(method, path, **kwargs):
                if path == "/currencies": return {"currencies": ["btc"]}
                if path == "/min-amount": return {"min_amount": "0.01"}
                if path == "/estimate": return {"estimated_amount": "9"}
                captured.update(kwargs.get("json", {}))
                return {"payment_id": "p1", "pay_address": "bc1qdepositaddress", "pay_amount": "9"}
            with patch("payments.provider", new=fake):
                r = self.client.post("/billing/checkout", headers=self.csrf(data), json={"plan": "monthly", "currency": "BTC"})
            self.assertEqual(r.status_code, 200, r.text)
            listed = {c["symbol"]: c["ready"] for c in self.client.get("/billing/plans").json()["coins"]}
        wallets._cache.clear()
        # The recovery phrase became the address the provider forwards to.
        self.assertEqual(captured["payout_address"], "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")
        self.assertEqual(captured["pay_currency"], "btc")
        self.assertEqual(listed, {"SOL": True, "LTC": True, "ETH": True, "BTC": True, "USDT": True, "USDC": True})

    def test_checkout_without_a_wallet_names_the_missing_variable(self):
        data = self.signup()
        with patch.dict(os.environ, {"SOL": ""}):
            wallets._cache.clear()
            r = self.client.post("/billing/checkout", headers=self.csrf(data), json={"plan": "monthly", "currency": "SOL"})
        wallets._cache.clear()
        self.assertEqual(r.status_code, 503)
        self.assertIn("SOL", r.json()["detail"])

    def test_expiry_csrf_origin_and_operator_boundary(self):
        data=self.signup()
        self.assertEqual(self.client.post("/hits", json={}).status_code, 403)
        self.assertEqual(self.client.delete("/hits", headers={"Origin":"https://foreign.example", "X-CSRF-Token":data["csrf"]}).status_code, 403)
        self.assertEqual(self.client.delete("/hits", headers=self.headers).status_code, 403)
        # Managing the stored proxy list is ordinary use of a paid tool, not an
        # operator-only action -- but a write still needs the CSRF token.
        self.assertEqual(self.client.post("/proxies/clear", headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post("/proxies/clear", headers=self.csrf(data)).status_code, 200)
        self.assertEqual(self.client.get("/proxies").json()["proxies"], [])
        with self.store.transaction() as db:
            # Expire the trial the way the clock does: started, then elapsed.
            db.execute("UPDATE users SET trial_available=0,trial_started=?,trial_until=? WHERE id=?",
                       (time.time() - accounts.TRIAL_SECONDS, time.time() - 1, data["user"]["id"]))
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
        r=self.client.post('/auth/signup',headers=self.headers,json={'username':'operator','password':'correct horse 42',**self.solve(self.client,'signup')})
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


class MigrationTests(unittest.TestCase):
    """A database created before the trial columns existed must still upgrade.

    The Railway deployment already has the old ``users`` table; CREATE TABLE IF
    NOT EXISTS never touches it, so this is the path every real upgrade takes.
    """

    def test_initialize_adds_the_trial_columns_to_an_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/old.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,"
                " password_hash TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,"
                " trial_until DOUBLE PRECISION NOT NULL, paid_until DOUBLE PRECISION NOT NULL DEFAULT 0,"
                " lifetime INTEGER NOT NULL DEFAULT 0, settings TEXT NOT NULL DEFAULT '{}')"
            )
            conn.commit()
            conn.close()
            with patch.dict(os.environ, {"ACCOUNT_DB_PATH": path, "APP_ENV": "test"}):
                store = accounts.Store("")
                with store.transaction() as db:
                    self.assertEqual(accounts.store_columns(db, "users") & {"trial_available", "trial_started"}, set())
                store.initialize()
                store.initialize()  # a restart must not be an error
                with store.transaction() as db:
                    columns = accounts.store_columns(db, "users")
                    self.assertLessEqual({"trial_available", "trial_started"}, columns)
                    # Old rows default to no trial rather than to a free window.
                    db.execute("INSERT INTO users(id,username,password_hash,created,trial_until) VALUES('x','legacy','h',0,0)")
                    row = db.one("SELECT trial_available,trial_started FROM users WHERE id='x'")
                self.assertEqual((row["trial_available"], row["trial_started"]), (0, 0.0))
                self.assertFalse(accounts.trial_pending(row))


class CaptchaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ACCOUNT_DB_PATH": self.tmp.name + "/accounts.db", "APP_ENV": "test"})
        self.env.start()
        self.saved = accounts.store
        accounts.store = accounts.Store(os.getenv("TEST_DATABASE_URL", ""))
        accounts.store.initialize()
        if accounts.store.pg:
            with accounts.store.transaction() as db:
                db.execute("DELETE FROM challenges")

    def tearDown(self):
        accounts.store = self.saved
        self.env.stop()
        self.tmp.cleanup()

    def entry(self, challenge_id):
        with accounts.store.transaction() as db:
            return db.one("SELECT action,word,target,expires FROM challenges WHERE id=?", (challenge_id,))

    def solve(self,action='signup'):
        ch=accounts.new_challenge(action)
        entry=self.entry(ch['id'])
        return SimpleNamespace(challenge_id=ch['id'],drag=entry['target'],word=entry['word'])

    def assert_rejected(self,body,action='signup'):
        with self.assertRaises(Exception) as caught:accounts.captcha(body,action)
        self.assertEqual(caught.exception.status_code,400)

    async def test_challenge_survives_a_new_store_instance(self):
        # Challenges live in the database, so another process or a restart can still verify them.
        ch=accounts.new_challenge('signup')
        restarted=accounts.Store(os.getenv("TEST_DATABASE_URL", ""))
        accounts.store=restarted
        entry=self.entry(ch['id'])
        accounts.captcha(SimpleNamespace(challenge_id=ch['id'],drag=entry['target'],word=entry['word']),'signup')

    async def test_drag_and_word_challenge_accepts_only_the_solved_answer(self):
        ch=accounts.new_challenge('signup')
        self.assertTrue(ch['word_image'].startswith('data:image/svg+xml;base64,'))
        self.assertEqual(len(self.entry(ch['id'])['word']),6)
        body=self.solve()
        accounts.captcha(body,'signup')
        self.assert_rejected(body)  # single use: replay fails

        wrong_drag=self.solve(); wrong_drag.drag=(wrong_drag.drag+30)%100
        self.assert_rejected(wrong_drag)

        wrong_word=self.solve(); wrong_word.word=('A' if wrong_word.word[0]!='A' else 'B')+wrong_word.word[1:]
        self.assert_rejected(wrong_word)

        lenient=self.solve(); lenient.word=lenient.word.lower()+' '
        accounts.captcha(lenient,'signup')

        self.assert_rejected(self.solve(),action='login')  # bound to action
        # Not bound to IP: a proxy can change the client address between GET and POST.
        moved=self.solve(); accounts.captcha(moved,'signup')

        expired=self.solve()
        with accounts.store.transaction() as db:
            db.execute("UPDATE challenges SET expires=0 WHERE id=?", (expired.challenge_id,))
        self.assert_rejected(expired)

        self.assert_rejected(SimpleNamespace(challenge_id='',drag=-1,word=''))

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
