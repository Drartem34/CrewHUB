"""Web Push (17.19): підписка, notify → пуш, мертві підписки. python -m unittest test_push -v"""
import json
import os
import unittest
from unittest import mock

from test_crewhub import CrewHubBase, core  # noqa: F401 — test_crewhub налаштовує оточення
import push

SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/test-endpoint-1",
       "keys": {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
                "auth": "tBHItJI5svbpez7KI4CCXg"}}


class FakeResponse:
    def __init__(self, code):
        self.status_code = code
        self.text = ""


class PushTest(CrewHubBase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.fail_code = None

        def fake_webpush(subscription_info, data, **kw):
            self.calls.append((subscription_info, json.loads(data), kw))
            if self.fail_code:
                raise push.WebPushException("gone", response=FakeResponse(self.fail_code))
            return FakeResponse(201)
        # Тести не залежать від того, чи стоїть pywebpush: підміняємо відправку і вмикаємо модуль
        p = mock.patch.object(push, "webpush", fake_webpush)
        p.start()
        self.addCleanup(p.stop)
        core.setup_push()
        self.assertTrue(push.enabled(), push.status_text())

    def subscribe(self, client, sub=SUB):
        return client.post("/push/subscribe", json=sub)

    def test_vapid_key_created_once_outside_repo(self):
        key = self.client.get("/push/vapid-public-key").get_json()
        self.assertTrue(key["enabled"])
        self.assertEqual(len(key["key"]), 87)  # 65 байт несжатої точки P-256 у base64url
        pem = os.path.join(os.path.dirname(core.DB_PATH), push.KEY_FILE)
        self.assertTrue(os.path.exists(pem))
        self.assertNotIn(core.BASE_DIR, os.path.abspath(pem))
        core.setup_push()  # перезапуск — той самий ключ, інакше всі підписки зламались би
        self.assertEqual(self.client.get("/push/vapid-public-key").get_json()["key"], key["key"])

    def test_subscribe_needs_login_and_valid_data(self):
        self.assertEqual(self.subscribe(self.client).status_code, 401)
        self.login("ivan")
        self.assertEqual(self.subscribe(self.client, {"endpoint": "http://evil/x", "keys": SUB["keys"]}).status_code, 400)
        self.assertEqual(self.subscribe(self.client, {"endpoint": SUB["endpoint"], "keys": {}}).status_code, 400)
        r = self.subscribe(self.client)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.subscribe(self.client)  # повторно — без дубля
        rows = self.sql("select u.username, s.endpoint from push_subscriptions s join users u on u.id = s.user_id")
        self.assertEqual([tuple(x) for x in rows], [("ivan", SUB["endpoint"])])

    def test_same_browser_moves_to_new_account(self):
        self.login("ivan")
        self.subscribe(self.client)
        self.client.post("/logout")
        self.login("olha")
        self.subscribe(self.client)
        self.assertEqual(self.sql("select u.username from push_subscriptions s join users u on u.id = s.user_id")[0][0], "olha")

    def test_unsubscribe(self):
        self.login("ivan")
        self.subscribe(self.client)
        r = self.client.post("/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
        self.assertEqual(r.get_json()["removed"], 1)
        self.assertEqual(self.sql("select count(*) from push_subscriptions")[0][0], 0)

    def test_vacation_approve_sends_push(self):
        self.login("ivan")
        self.subscribe(self.client)
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            rid = c.execute("insert into requests(user_id, type, starts_on, ends_on, created_at) values (?, 'vacation', '2026-12-01',"
                            " '2026-12-05', ?)", (ivan, core.now())).lastrowid
        admin = self.login("admin", "admin123", client=core.app.test_client())
        admin.post(f"/admin/request/{rid}/approve", data={})
        self.assertTrue(push.flush(5))
        self.assertEqual(len(self.calls), 1)
        info, payload, kw = self.calls[0]
        self.assertEqual(info["endpoint"], SUB["endpoint"])
        self.assertEqual(info["keys"], SUB["keys"])
        self.assertEqual(payload["title"], "Запит схвалено")
        self.assertIn("Відпустка 01.12–05.12.2026", payload["body"])
        self.assertEqual(payload["url"], "/me?tab=requests")
        self.assertTrue(kw["vapid_claims"]["sub"].startswith(("https://", "mailto:")))
        self.assertTrue(os.path.exists(kw["vapid_private_key"]))

    def test_penalty_and_bonus_notify_and_push(self):
        self.login("ivan")
        self.subscribe(self.client)
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        admin = self.login("admin", "admin123", client=core.app.test_client())
        admin.post("/admin/adjustment", data={"user_id": ivan, "amount": "150", "reason": "Запізнення", "kind": "penalty", "day": "2026-11-16"})
        admin.post("/admin/adjustment", data={"user_id": ivan, "amount": "300", "reason": "Найкращий місяць", "kind": "bonus", "day": "2026-11-16"})
        self.assertTrue(push.flush(5))
        titles = [r[0] for r in self.sql("select title from notifications where user_id = ? order by id", ivan)]
        self.assertEqual(titles[-2:], ["Вам виписано штраф", "Вам нараховано бонус"])
        self.assertEqual([c[1]["title"] for c in self.calls], ["Вам виписано штраф", "Вам нараховано бонус"])
        self.assertIn("150.00", self.calls[0][1]["body"])
        self.assertIn("Запізнення", self.calls[0][1]["body"])

    def test_dead_subscription_removed(self):
        self.login("ivan")
        self.subscribe(self.client)
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        self.fail_code = 410
        with core.connect() as c:
            core.notify(c, ivan, "Перевірка")
            c.commit()
        self.assertTrue(push.flush(5))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sql("select count(*) from push_subscriptions")[0][0], 0)

    def test_no_subscription_no_work(self):
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            core.notify(c, ivan, "Без підписки")
            c.commit()
        self.assertTrue(push.flush(5))
        self.assertEqual(self.calls, [])

    def test_without_library_server_works_as_before(self):
        with mock.patch.object(push, "webpush", None):
            core.setup_push()
            self.assertFalse(push.enabled())
            self.assertFalse(self.client.get("/push/vapid-public-key").get_json()["enabled"])
            self.login("ivan")
            self.assertEqual(self.subscribe(self.client).status_code, 503)
            ivan = self.sql("select id from users where username = 'ivan'")[0][0]
            with core.connect() as c:
                core.notify(c, ivan, "Як раніше")
                c.commit()
            self.assertEqual(self.sql("select count(*) from notifications where title = 'Як раніше'")[0][0], 1)
        core.setup_push()

    def test_service_worker_and_pwa_bits(self):
        r = self.client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Service-Worker-Allowed"], "/")
        self.assertIn("javascript", r.headers["Content-Type"])
        self.assertIn("showNotification", r.get_data(as_text=True))
        r.close()
        m = self.client.get("/static/manifest.json")
        manifest = json.loads(m.get_data(as_text=True))
        m.close()
        self.assertEqual(manifest["display"], "standalone")
        sizes = {i["sizes"] for i in manifest["icons"] if i["type"] == "image/png"}
        self.assertTrue({"180x180", "192x192", "512x512"} <= sizes)
        self.login("ivan")
        page = self.client.get("/me?tab=app").get_data(as_text=True)
        self.assertIn("apple-mobile-web-app-capable", page)
        self.assertIn("icon-180.png", page)
        self.assertIn("Увімкнути сповіщення", page)
        self.assertIn("На початковий екран", page)
        app_page = self.client.get("/me?tab=app", headers={"User-Agent": "Mozilla/5.0 CrewHubApp/2.4"}).get_data(as_text=True)
        self.assertNotIn("Увімкнути сповіщення", app_page)  # в Android-застосунку свої сповіщення


if __name__ == "__main__":
    unittest.main()
