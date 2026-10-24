"""Автотести: python -m unittest test_crewhub -v"""
import os
import tempfile
import unittest
from datetime import date, timedelta, datetime

_tmp = tempfile.mkdtemp()
os.environ["CREWHUB_DEMO"] = "users"  # тестові акаунти без демо-історії
os.environ["CREWHUB_TRUST_PROXY"] = "1"  # тести імітують тунель: IP телефона в X-Forwarded-For
os.environ["CREWHUB_CSRF"] = "0"  # CSRF вимкнено для основних тестів; окремий тест вмикає його явно
os.environ["CREWHUB_DATA"] = _tmp  # settings.ini, журнали — у тимчасовій папці, а не в ~/CrewHub-data
os.environ["CREWHUB_DB"] = os.path.join(_tmp, "test.db")
os.environ["CREWHUB_BACKUP_DIR"] = os.path.join(_tmp, "backups")
os.environ["CREWHUB_PEOPLE_DIR"] = os.path.join(_tmp, "people")

import crewhub_core as core  # noqa: E402
import profiles  # noqa: E402
import recover_admin as recover  # noqa: E402
import settings_file  # noqa: E402

core.log.addHandler(__import__("logging").NullHandler())  # попередження сервера не засмічують вивід тестів

WORK_IP = "203.0.113.10"
HOME_IP = "8.8.8.8"


class CrewHubBase(unittest.TestCase):
    def setUp(self):
        import shutil
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(core.DB_PATH + suffix):
                os.remove(core.DB_PATH + suffix)
        shutil.rmtree(core.PEOPLE_DIR, ignore_errors=True)
        shutil.rmtree(core.BACKUP_DIR, ignore_errors=True)
        if os.path.exists(core.SETTINGS_FILE):
            os.remove(core.SETTINGS_FILE)  # кожен тест — з чистою папкою даних
        settings_file._state.update(file=None, db=None, server={})
        profiles._state.update(last_scan=0.0, warning=None)
        core._throttle.reset()  # ліміти спроб входу не переходять з тесту в тест
        core._stats_cache.update(at=0.0, data=None)  # кеші «Огляду» теж
        core._live_cache.update(at=0.0, data=None)
        core.init_db()
        with core.connect() as c:
            c.execute("insert into networks(location_id, cidr, label) values (1, '203.0.113.0/28', 'тест')")
            # Надбавки вимкнені, щоб суми в тестах не залежали від дня тижня і години запуску
            c.execute("update settings set value = '1' where key in ('weekend_multiplier', 'night_multiplier')")
            c.execute("update settings set value = '0' where key = 'auto_start'")  # автостарт перевіряє окремий тест
        self.client = core.app.test_client()

    def login(self, user, password="1234", client=None):
        client = client or self.client
        r = client.post("/login", data={"username": user, "password": password})
        self.assertEqual(r.status_code, 302)
        return client

    def clock(self, action, ip=WORK_IP, client=None, **body):
        r = (client or self.client).post(f"/api/clock/{action}", json=body, headers={"X-Forwarded-For": ip})
        return r.get_json()

    def sync(self):
        """Один прохід синхронізації папок (у сервері це робить фоновий потік)."""
        profiles._state["last_scan"] = 0.0
        with core.connect() as c:
            profiles.sync_once(c)

    def folder(self, username):
        return os.path.join(core.PEOPLE_DIR, self.sql("select folder from users where username = ?", username)[0][0])

    def sql(self, q, *args):
        with core.connect() as c:
            return c.execute(q, args).fetchall()


class CrewHubTest(CrewHubBase):
    def test_start_only_on_work_wifi(self):
        self.login("ivan")
        res = self.clock("start", ip=HOME_IP)
        self.assertFalse(res["ok"])
        self.assertIn("не в робочій мережі", res["error"])
        self.assertEqual(self.sql("select event from conn_log")[0][0], "denied")
        res = self.clock("start")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["location"], "Тестова точка")
        self.assertEqual(res["shift"]["hourly_rate"], 120)
        again = self.clock("start")  # подвійне натискання при лагах: та сама зміна, без помилки
        self.assertTrue(again["ok"] and again.get("already"))
        self.assertEqual(self.sql("select count(*) from shifts")[0][0], 1)

    def test_forwarded_header_not_trusted_from_network(self):
        # Телефон не з localhost не може підставити робочий IP через X-Forwarded-For
        self.login("ivan")
        r = self.client.post("/api/clock/start", json={}, headers={"X-Forwarded-For": WORK_IP},
                             environ_base={"REMOTE_ADDR": HOME_IP})
        self.assertFalse(r.get_json()["ok"])

    def test_csrf_protection(self):
        core.CSRF_ON = True
        try:
            c = core.app.test_client()
            uid = self.sql("select id from users where username = 'ivan'")[0][0]
            with c.session_transaction() as s:
                s["uid"] = uid
                s["csrf"] = "tok-123"
            host = "http://localhost"
            # HTML-форма без токена — відмова
            r = c.post("/notifications/read", data={"x": "1"}, headers={"Origin": host})
            self.assertEqual(r.status_code, 400)
            # Форма з чужого сайту (Origin) — відмова навіть із правильним токеном
            r = c.post("/notifications/read", data={"x": "1", "csrf": "tok-123"}, headers={"Origin": "http://evil.example"})
            self.assertEqual(r.status_code, 400)
            # Правильні Origin і токен — проходить
            r = c.post("/notifications/read", data={"x": "1", "csrf": "tok-123"}, headers={"Origin": host})
            self.assertIn(r.status_code, (200, 302))
            # API застосунку (JSON) працює без токена і без Origin — фонова служба не браузер
            r = c.post("/api/clock/check", json={}, headers={"X-Forwarded-For": WORK_IP})
            self.assertEqual(r.status_code, 200)
            # Але крос-сайтовий запит до API з браузера (чужий Origin) блокується
            r = c.post("/api/clock/check", json={}, headers={"Origin": "http://evil.example", "X-Forwarded-For": WORK_IP})
            self.assertEqual(r.status_code, 403)
        finally:
            core.CSRF_ON = False

    def test_break_end_requires_own_network(self):
        OTHER_WORK_IP = "198.51.100.5"
        with core.connect() as c:
            c.execute("insert into locations(id, name, address, is_active) values (2, 'Друга точка', '', 1)")
            c.execute("insert into networks(location_id, cidr, label) values (2, '198.51.100.0/28', 'точка2')")
        self.login("ivan")
        self.clock("start")
        self.clock("break_start", kind="lunch")
        self.assertFalse(self.clock("break_end", ip=OTHER_WORK_IP)["ok"])  # з іншої точки продовжити не можна
        self.assertTrue(self.clock("break_end")["ok"])  # зі своєї точки — можна

    def _set_cookie(self, client, key, value):
        try:
            client.set_cookie(key, value, domain="localhost")
        except TypeError:
            client.set_cookie("localhost", key, value)

    def test_remember_me_token(self):
        c = core.connect()
        uid = c.execute("select id from users where username = 'ivan'").fetchone()[0]
        val = core.issue_remember(c, uid, "web-test", "Тест", "1.2.3.4"); c.commit()
        self.assertEqual(core.remember_user(c, val), uid); c.commit()
        sel = val.split(":")[0]
        self.assertIsNone(core.remember_user(c, sel + ":wrong"))  # невірний validator
        c.execute("update remember_tokens set expires_at = ? where selector = ?", (core.now() - 10, sel)); c.commit()
        self.assertIsNone(core.remember_user(c, val)); c.commit()  # протермінований
        self.assertIsNone(c.execute("select 1 from remember_tokens where selector = ?", (sel,)).fetchone())  # видалено
        v2 = core.issue_remember(c, uid, None, None, None); c.commit()
        core.revoke_remember(c, uid); c.commit()
        self.assertIsNone(core.remember_user(c, v2))  # відкликано на всіх пристроях
        c.close()

    def test_remember_me_autologin(self):
        c = core.app.test_client()
        r = c.post("/login", data={"username": "ivan", "password": "1234"})
        self.assertEqual(r.status_code, 302)
        remember = None
        for h in r.headers.get_all("Set-Cookie"):
            if h.startswith("crewhub_remember="):
                remember = h.split("crewhub_remember=", 1)[1].split(";", 1)[0]
        self.assertTrue(remember)  # логін видає токен постійного входу
        # Новий клієнт лише з cookie постійного входу (без сесії) входить автоматично
        c2 = core.app.test_client()
        self._set_cookie(c2, "crewhub_remember", remember)
        self.assertEqual(c2.get("/me?tab=clock").status_code, 200)
        # Відкликання на всіх пристроях — автовхід більше не працює
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as conn:
            core.revoke_remember(conn, uid); conn.commit()
        c3 = core.app.test_client()
        self._set_cookie(c3, "crewhub_remember", remember)
        self.assertEqual(c3.get("/me?tab=clock").status_code, 302)  # викидає на /login

    def test_wifi_loss_pauses_and_restores(self):
        self.login("ivan")
        self.clock("start")
        self.sql("update shifts set last_seen_at = last_seen_at - 600")
        self.clock("heartbeat", ip=HOME_IP)  # пінг з чужої мережі: людина точно пішла — пауза з останнього пінгу з точки
        with core.connect() as c:
            self.assertEqual(core.sweep(c), 0)
        self.assertEqual(self.sql("select status from shifts")[0][0], "paused")
        self.assertEqual(self.sql("select count(*) from notifications where title like '%Wi-Fi%'")[0][0], 2)  # moder + admin
        res = self.clock("heartbeat")
        self.assertEqual(res["shift"]["status"], "active")
        self.assertGreaterEqual(res["shift"]["unpaid_break_seconds"], 590)

    def test_shift_pinned_to_its_location(self):
        # Зміну прив'язано до мережі точки, де її почали. Пінг з ІНШОЇ робочої точки (чи підміна IP
        # на чужу робочу мережу) не рахується як присутність: захист від підміни/зміни IP.
        OTHER_WORK_IP = "198.51.100.5"
        with core.connect() as c:
            c.execute("insert into locations(id, name, address, is_active) values (2, 'Друга точка', '', 1)")
            c.execute("insert into networks(location_id, cidr, label) values (2, '198.51.100.0/28', 'точка2')")
        self.login("ivan")
        self.assertTrue(self.clock("start")["ok"])  # старт на точці 1
        self.sql("update shifts set last_seen_at = last_seen_at - 600")
        res = self.clock("heartbeat", ip=OTHER_WORK_IP)  # пінг з іншої робочої точки
        self.assertFalse(res["on_work_network"])  # не рахується як «на своїй точці»
        self.assertEqual(self.sql("select status from shifts")[0][0], "paused")
        # Закриття з чужої мережі — часом останнього пінгу зі своєї точки (анти-фрод)
        res = self.clock("stop", ip=OTHER_WORK_IP)
        self.assertTrue(res["off_network"])

    def test_breaks_and_offnetwork_stop(self):
        self.login("ivan")
        self.clock("start")
        self.assertEqual(self.clock("break_start", kind="lunch")["shift"]["open_break"]["kind"], "lunch")
        self.assertFalse(self.clock("break_end", ip=HOME_IP)["ok"])
        self.assertTrue(self.clock("break_end")["ok"])
        self.sql("update shifts set started_at = started_at - 3600, last_seen_at = last_seen_at - 120")
        res = self.clock("stop", ip=HOME_IP)
        self.assertTrue(res["off_network"])
        ended, seen = self.sql("select ended_at, last_seen_at from shifts")[0]
        self.assertEqual(ended, seen)

    def test_employee_cannot_open_admin(self):
        self.login("ivan")
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.post("/admin/employee/3", data={"hourly_rate": "999"}).status_code, 403)

    def test_moderator_edits_are_audited(self):
        self.login("ivan")
        self.clock("start")
        self.sql("update shifts set started_at = started_at - 7200")
        self.clock("stop")
        mod = self.login("moder", "moder123", core.app.test_client())
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        r = mod.post(f"/admin/employee/{ivan}", data={"full_name": "Іван Петренко", "hourly_rate": "150", "status": "active",
                                                       "role": "admin"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sql("select role from users where id = ?", ivan)[0][0], "employee")  # роль лише адмін
        mod.post(f"/admin/employee/{ivan}", data={"full_name": "Іван Петренко", "hourly_rate": "150", "status": "active"})
        self.assertEqual(self.sql("select hourly_rate from users where id = ?", ivan)[0][0], 150)
        audit = self.sql("select changes from audit where entity = 'users' and target_user_id = ?", ivan)
        self.assertTrue(any("hourly_rate" in a[0] for a in audit))
        mod.post("/admin/adjustment", data={"user_id": ivan, "kind": "bonus", "amount": "100", "reason": "Вихідний"})
        rows = core.payroll(core.connect(), date.today().replace(day=1), date.today(), ivan)
        self.assertAlmostEqual(rows[0]["hours"], 2.0, places=1)
        self.assertAlmostEqual(rows[0]["total"], 2 * 120 + 100, delta=1)  # ставка зміни зафіксована на старті
        csv = mod.get("/admin/payroll.csv").data.decode("utf-8-sig")
        self.assertIn("Іван Петренко", csv)

    def test_bonus_delete_and_percent(self):
        self.login("ivan")
        self.clock("start")
        self.sql("update shifts set started_at = started_at - 7200")  # 2 години за ставкою 120 = 240
        self.clock("stop")
        mod = self.login("moder", "moder123", core.app.test_client())
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        today = date.today().isoformat()
        # Помилково доданий бонус можна скасувати кнопкою
        mod.post("/admin/adjustment", data={"user_id": ivan, "unit": "money", "kind": "bonus", "amount": "100", "reason": "Тест", "day": today})
        aid = self.sql("select id from adjustments where user_id = ?", ivan)[0][0]
        self.assertEqual(mod.post(f"/admin/adjustment/{aid}/delete").status_code, 302)
        self.assertEqual(self.sql("select count(*) from adjustments where user_id = ?", ivan)[0][0], 0)
        # Бонус у відсотках рахується від заробітку за місяць (10% від 240 = 24)
        mod.post("/admin/adjustment", data={"user_id": ivan, "unit": "pct", "kind": "bonus", "amount": "10", "reason": "Премія", "day": today})
        amount, reason = self.sql("select amount, reason from adjustments where user_id = ?", ivan)[0]
        self.assertAlmostEqual(amount, 24, delta=0.5)
        self.assertIn("%", reason)
        # Штраф у відсотках — від'ємний
        mod.post("/admin/adjustment", data={"user_id": ivan, "unit": "pct", "kind": "penalty", "amount": "5", "reason": "Запізнення", "day": today})
        self.assertAlmostEqual(self.sql("select amount from adjustments where user_id = ? order by id desc", ivan)[0][0], -12, delta=0.5)

    def test_dayoff_campaign(self):
        mod = self.login("moder", "moder123", core.app.test_client())
        emp = self.login("ivan")
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        # Поки збір закрито — кнопки на головному екрані немає
        self.assertNotIn("Запросити дей-офф", emp.get("/me?tab=clock").get_data(as_text=True))
        # Модератор відкриває збір заявок
        self.assertEqual(mod.post("/dayoffs/campaign", data={"action": "open"}).status_code, 302)
        self.assertGreater(int(core.meta_get(core.connect(), "dayoff_open_until", "0")), core.now())
        self.assertIn("Запросити дей-офф", emp.get("/me?tab=clock").get_data(as_text=True))
        # Працівник подає заявку з конкретним днем
        day = (date.today() + timedelta(days=5)).isoformat()
        emp.post("/me/request", data={"type": "day_off", "day": day, "back": "clock"})
        rid, starts = self.sql("select id, starts_on from requests where user_id = ? and type = 'day_off'", ivan)[0]
        self.assertEqual(starts, day)
        # Модератор погоджує саме цей день → статус approved
        mod.post("/team/dayoff", data={"rid": rid, "action": "approve"})
        self.assertEqual(self.sql("select status from requests where id = ?", rid)[0][0], "approved")
        # Збір закривається — кнопка зникає
        mod.post("/dayoffs/campaign", data={"action": "close"})
        self.assertEqual(int(core.meta_get(core.connect(), "dayoff_open_until", "0")), 0)
        self.assertNotIn("Запросити дей-офф", emp.get("/me?tab=clock").get_data(as_text=True))

    def test_month_dayoff_toggle(self):
        mod = self.login("moder", "moder123", core.app.test_client())
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        day = (date.today() + timedelta(days=7)).isoformat()
        # Клік по клітинці в режимі «Дей-офи» ставить дей-офф одразу (approved)
        r = mod.post("/schedule/dayoff", json={"user_id": ivan, "day": day}).get_json()
        self.assertTrue(r["ok"])
        self.assertFalse(r["off"])
        self.assertEqual(self.sql("select status from requests where user_id = ? and type = 'day_off' and starts_on = ?", ivan, day)[0][0], "approved")
        # Повторний клік знімає його
        self.assertTrue(mod.post("/schedule/dayoff", json={"user_id": ivan, "day": day}).get_json()["off"])
        self.assertEqual(self.sql("select status from requests where user_id = ? and starts_on = ?", ivan, day)[0][0], "cancelled")
        # Звичайний працівник не має доступу
        emp = self.login("ivan")
        self.assertEqual(emp.post("/schedule/dayoff", json={"user_id": ivan, "day": day}).status_code, 403)

    def test_avatar_stored_in_db_and_survives_folder_loss(self):
        import io
        import shutil
        emp = self.login("ivan")
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                            "0000000d49444154789c6360000002000154a24f990000000049454e44ae426082")
        r = emp.post(f"/photo/{uid}", data={"photo": (io.BytesIO(png), "a.png")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sql("select count(*) from avatars where user_id = ?", uid)[0][0], 1)
        self.assertEqual(emp.get(f"/avatar/{uid}").status_code, 200)
        # Папку втратили — аватарка лишається (роздається з бази), а файл у папці відтворюється
        folder = self.sql("select folder from users where id = ?", uid)[0][0]
        shutil.rmtree(os.path.join(core.PEOPLE_DIR, folder))
        self.sync()
        self.assertEqual(emp.get(f"/avatar/{uid}").status_code, 200)
        self.assertTrue(core.avatar_file(os.path.join(core.PEOPLE_DIR, folder)))

    def test_time_edit_request_flow(self):
        self.login("ivan")
        self.clock("start")
        shift_id = self.sql("select id from shifts")[0][0]
        r = self.client.post("/me/request", data={"type": "time_edit", "shift_id": shift_id,
                                                  "proposed_start": "2026-10-18T09:00", "proposed_end": "2026-10-18T18:00",
                                                  "comment": "Забув закрити"})
        self.assertEqual(r.status_code, 302)
        mod = self.login("moder", "moder123", core.app.test_client())
        rid = self.sql("select id from requests")[0][0]
        mod.post(f"/admin/request/{rid}/approve", data={"comment": "Ок"})
        status, edited, worked = self.sql("select status, is_edited, ended_at - started_at from shifts")[0]
        self.assertEqual((status, edited, worked), ("closed", 1, 9 * 3600))
        self.assertTrue(self.sql("select 1 from audit where entity = 'shifts'"))

    def test_live_monitor_late_and_pages_render(self):
        mod = self.login("admin", "admin123")
        rows = core.live_monitor(core.connect())
        olha = next(r for r in rows if r["user"]["full_name"] == "Ольга Коваль")
        self.assertEqual(olha["state"], "late")
        for tab in ["live", "employees", "requests", "schedule", "zones", "logs", "audit", "payroll", "settings"]:
            self.assertEqual(mod.get(f"/admin?tab={tab}").status_code, 200, tab)
        self.assertEqual(mod.get("/admin/employee/3").status_code, 200)
        for tab in ["clock", "timesheet", "schedule", "requests", "team", "profile"]:
            self.assertEqual(mod.get(f"/me?tab={tab}").status_code, 200, tab)

    def test_new_features(self):
        import io
        ivan = self.login("ivan")
        self.clock("start")
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        # фото: лише картинки, чужий профіль працівнику заборонено
        r = ivan.post(f"/photo/{uid}", data={"photo": (io.BytesIO(b"GIF89a"), "me.gif")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.sql("select photo_url from users where id = ?", uid)[0][0].startswith(f"/avatar/{uid}"))
        folder = self.sql("select folder from users where id = ?", uid)[0][0]
        self.assertTrue(os.path.exists(os.path.join(core.PEOPLE_DIR, folder, "avatar.gif")))  # аватарка в особовій папці
        self.assertEqual(ivan.get(f"/avatar/{uid}").data, b"GIF89a")
        self.assertEqual(ivan.post("/photo/1", data={}).status_code, 403)
        # зміна пароля з перевіркою поточного
        ivan.post("/me/password", data={"current": "bad", "new": "secret1"})
        self.assertEqual(core.app.test_client().post("/login", data={"username": "ivan", "password": "secret1"}).status_code, 200)
        ivan.post("/me/password", data={"current": "1234", "new": "secret1"})
        self.login("ivan", "secret1", client=core.app.test_client())
        self.assertEqual(ivan.get("/api/live").status_code, 403)

        mod = self.login("moder", "moder123", client=core.app.test_client())
        live = mod.get("/api/live").get_json()
        self.assertEqual(next(r for r in live["rows"] if r["id"] == uid)["state"], "on_site")
        self.assertEqual(live["counts"]["on_site"], 1)
        for path in ["/admin?tab=live", f"/admin/employee/{uid}", "/admin?tab=schedule&week=2026-10-12", "/admin?tab=payroll&month=2026-09"]:
            self.assertEqual(mod.get(path).status_code, 200, path)
        csv_ = mod.get(f"/admin/employee/{uid}/timesheet.csv")
        self.assertIn("Разом", csv_.get_data(as_text=True))
        # копіювання тижня без дублікатів
        monday = date.today().toordinal() - date.today().weekday()
        week = date.fromordinal(monday).isoformat()
        prev = len(self.sql("select 1 from schedule where starts_at >= ? and starts_at < ?",
                            *core.day_bounds(date.fromordinal(monday - 7), date.fromordinal(monday - 1))))
        before = len(self.sql("select 1 from schedule"))
        mod.post("/admin/schedule", data={"action": "copy_week", "week": week})
        after = len(self.sql("select 1 from schedule"))
        mod.post("/admin/schedule", data={"action": "copy_week", "week": week})
        self.assertLessEqual(after - before, prev)
        self.assertEqual(len(self.sql("select 1 from schedule")), after)

    def test_phone_login_and_registration(self):
        self.assertEqual(core.normalize_phone("067 123-45-67"), "+380671234567")
        self.assertEqual(core.normalize_phone("+38 (067) 123 45 67"), "+380671234567")
        self.assertEqual(core.normalize_phone("380671234567"), "+380671234567")
        self.assertIsNone(core.normalize_phone("12345"))
        # вхід за номером у будь-якому записі
        c = core.app.test_client()
        self.assertEqual(c.post("/login", data={"username": "(067) 123 45 67", "password": "1234"}).status_code, 302)
        # реєстрація: той самий номер в іншому форматі — збіг
        r = core.app.test_client().post("/register", data={"full_name": "Петро Іванов", "phone": "+380671234567",
                                                           "password": "secret1", "password2": "secret1"})
        self.assertIn("уже зареєстровано", r.get_data(as_text=True))
        new = core.app.test_client()
        r = new.post("/register", data={"full_name": "Петро Іванов", "phone": "063 111 22 33", "password": "secret1", "password2": "secret1"})
        self.assertIn("Заявку надіслано", r.get_data(as_text=True))
        uid, status = self.sql("select id, status from users where phone = '+380631112233'")[0]
        self.assertEqual(status, "pending")
        r = new.post("/login", data={"username": "0631112233", "password": "secret1"})
        self.assertEqual(r.status_code, 200)  # не пускає до підтвердження
        self.assertIn("ще не підтверджено", r.get_data(as_text=True))
        admin = self.login("admin", "admin123", client=core.app.test_client())
        self.assertIn("Петро Іванов", admin.get("/admin?tab=employees").get_data(as_text=True))
        self.assertIn("Петро Іванов", admin.get("/admin?tab=requests").get_data(as_text=True))
        self.assertIn("Нові реєстрації", admin.get("/admin?tab=live").get_data(as_text=True))
        # модератор теж може підтвердити, але роль не вибирає
        mc = self.login("moder", "moder123", client=core.app.test_client())
        mc.post(f"/admin/registration/{uid}/approve", data={"hourly_rate": "130", "location_id": "1", "role": "admin"})
        self.assertEqual(tuple(self.sql("select status, hourly_rate, role from users where id = ?", uid)[0]), ("active", 130, "employee"))
        self.login("0631112233", "secret1", client=core.app.test_client())
        # адмін не може дати двом людям один номер
        olha = self.sql("select id from users where username = 'olha'")[0][0]
        admin.post(f"/admin/employee/{olha}", data={"full_name": "Ольга Коваль", "phone": "0631112233", "hourly_rate": "110", "status": "active"})
        self.assertEqual(self.sql("select phone from users where id = ?", olha)[0][0], "+380937654321")

    def test_admin_rates_reset_delete_bind(self):
        admin = self.login("admin", "admin123")
        admin.post("/admin/rates", data={"rate": "150", "scope": "employees"})
        rates = dict(self.sql("select username, hourly_rate from users"))
        self.assertEqual((rates["ivan"], rates["olha"], rates["moder"], rates["admin"]), (150, 150, 180, 0))
        self.assertEqual(float(core.setting("default_hourly_rate", core.connect())), 150.0)
        # модератор загальну ставку не змінює
        mod = self.login("moder", "moder123", client=core.app.test_client())
        self.assertEqual(mod.post("/admin/rates", data={"rate": "1", "scope": "all"}).status_code, 403)
        # скидання пароля: тимчасовий показується один раз, потім обов'язкова зміна
        ivan_id = self.sql("select id from users where username = 'ivan'")[0][0]
        admin.post(f"/admin/employee/{ivan_id}/reset_password")
        page = admin.get(f"/admin/employee/{ivan_id}").get_data(as_text=True)
        import re
        temp = re.search(r'<b class="mono" style="font-size:18px">([^<]+)</b>', page).group(1)
        self.assertNotIn(temp, admin.get(f"/admin/employee/{ivan_id}").get_data(as_text=True))
        ivan = self.login("ivan", temp, client=core.app.test_client())
        self.assertIn("/password", ivan.get("/me").headers["Location"])
        self.assertEqual(ivan.post("/api/clock/check", json={}).status_code, 403)
        ivan.post("/password", data={"password": "newpass1", "password2": "newpass1"})
        self.assertEqual(ivan.get("/me").status_code, 200)
        # прив'язка поточної мережі
        admin.post("/admin/zones", data={"action": "bind_current", "location_id": "1"}, headers={"X-Forwarded-For": "93.184.216.34"})
        self.assertTrue(self.sql("select 1 from networks where cidr = '93.184.216.34/32'"))
        admin.post("/admin/zones", data={"action": "bind_current", "location_id": "1"}, headers={"X-Forwarded-For": "192.168.5.20"})
        self.assertTrue(self.sql("select 1 from networks where cidr = '192.168.5.0/24'"))
        # видалення: не себе, решту можна
        admin_id = self.sql("select id from users where username = 'admin'")[0][0]
        admin.post(f"/admin/employee/{admin_id}/delete")
        self.assertTrue(self.sql("select 1 from users where id = ?", admin_id))
        self.assertEqual(mod.post(f"/admin/employee/{ivan_id}/delete").status_code, 403)
        admin.post(f"/admin/employee/{ivan_id}/delete")
        self.assertFalse(self.sql("select 1 from users where id = ?", ivan_id))
        self.assertTrue(self.sql("select 1 from audit where entity = 'users' and action = 'DELETE'"))

    def test_apk_and_unknown_networks(self):
        self.assertEqual(self.client.get("/app").status_code, 404)  # застосунок ще не поклали поруч
        orig = core.APK_PATH
        core.APK_PATH = os.path.join(_tmp, "CrewHub.apk")
        with open(core.APK_PATH, "wb") as f:
            f.write(b"PK\x03\x04apk")
        self.addCleanup(setattr, core, "APK_PATH", orig)
        r = self.client.get("/app")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/vnd.android.package-archive")
        self.assertIn("Завантажити застосунок", self.client.get("/login").get_data(as_text=True))
        app_page = self.client.get("/login", headers={"User-Agent": "Mozilla/5.0 CrewHubApp/1.0"}).get_data(as_text=True)
        self.assertIn("crewhub://settings", app_page)
        # спроба з чужої мережі з'являється в «Точках і Wi-Fi» з готовою зоною
        self.login("ivan")
        self.clock("start", ip="192.168.44.17")
        admin = self.login("admin", "admin123", client=core.app.test_client())
        page = admin.get("/admin?tab=zones").get_data(as_text=True)
        self.assertIn("192.168.44.0/24", page)
        admin.post("/admin/zones", data={"action": "network", "cidr": "192.168.44.0/24", "location_id": "1"})
        self.assertTrue(self.clock("start", ip="192.168.44.17")["ok"])

    def ts(self, s):
        return int(datetime.fromisoformat(s).timestamp())

    def test_weekend_and_night_multipliers(self):
        rules = {"weekend": 2.0, "night": 3.0, "night_start": 22 * 60, "night_end": 6 * 60}
        # пт 20:00 – сб 02:00: 2 год ×1, 2 год ×3 (ніч пт), 2 год ×3 (ніч сб: більший з ×2 і ×3)
        units = core.weighted_seconds(self.ts("2026-10-16T20:00"), self.ts("2026-10-17T02:00"), rules)
        self.assertAlmostEqual(units / 3600, 2 + 6 + 6)
        # субота вдень — ×2
        self.assertAlmostEqual(core.weighted_seconds(self.ts("2026-10-17T10:00"), self.ts("2026-10-17T14:00"), rules) / 3600, 8)
        with core.connect() as c:
            c.execute("update settings set value = '2' where key = 'weekend_multiplier'")
            c.execute("update settings set value = '3' where key = 'night_multiplier'")
            uid = c.execute("select id from users where username = 'ivan'").fetchone()[0]
            # середа 9–17, обід 12–13 оплачується на 50%
            sid = c.execute("insert into shifts(user_id, location_id, started_at, ended_at, status, hourly_rate, last_seen_at)"
                            " values (?, 1, ?, ?, 'closed', 100, ?)",
                            (uid, self.ts("2026-10-14T09:00"), self.ts("2026-10-14T17:00"), self.ts("2026-10-14T17:00"))).lastrowid
            c.execute("insert into breaks(shift_id, kind, is_paid, rate_pct, started_at, ended_at) values (?, 'lunch', 1, 50, ?, ?)",
                      (sid, self.ts("2026-10-14T12:00"), self.ts("2026-10-14T13:00")))
            st = core.shift_stats(c, c.execute("select * from shifts where id = ?", (sid,)).fetchone())
        self.assertEqual(st["worked_seconds"], 8 * 3600)
        self.assertAlmostEqual(st["earnings"], 750)  # 7 год × 100 + 1 год × 50
        rows = core.payroll(core.connect(), date(2026, 10, 1), date(2026, 10, 31), uid)
        self.assertAlmostEqual(rows[0]["earned"], 750)

    def test_break_settings(self):
        with core.connect() as c:
            c.execute("update settings set value = '0' where key = 'rest_enabled'")
            c.execute("update settings set value = '1' where key = 'lunch_is_paid'")
            c.execute("update settings set value = '150' where key = 'lunch_rate_pct'")
        self.login("ivan")
        page = self.client.get("/me").get_data(as_text=True)
        self.assertIn('id="lunch"', page)
        self.assertNotIn('id="rest"', page)
        self.clock("start")
        self.assertFalse(self.clock("break_start", kind="rest")["ok"])
        res = self.clock("break_start", kind="lunch")
        self.assertTrue(res["ok"])
        b = res["shift"]["open_break"]
        self.assertEqual((b["is_paid"], b["rate_pct"]), (1, 150))
        self.assertAlmostEqual(res["shift"]["rate_now"], 180)  # 120 × 150%
        admin = self.login("admin", "admin123", client=core.app.test_client())
        admin.post("/admin/settings", data={"lunch_enabled": "on", "lunch_rate_pct": "abc", "weekend_multiplier": "2,5"})
        self.assertEqual(core.setting("lunch_rate_pct", core.connect()), "150")  # сміття не зберігається
        self.assertEqual(core.setting("weekend_multiplier", core.connect()), "2.5")
        self.assertEqual(core.setting("lunch_is_paid", core.connect()), "0")

    def test_absences(self):
        self.login("ivan")
        today = date.today().isoformat()
        self.client.post("/me/request", data={"type": "sick_leave", "starts_on": today, "ends_on": today, "back": "schedule"})
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        # працівник лише просить: усе йде тімліду, сам скасувати чи змінити не може
        rid = self.sql("select id, status from requests where user_id = ?", ivan)[0]
        self.assertEqual(rid[1], "pending")
        self.assertEqual(self.client.post(f"/me/request/{rid[0]}/cancel").status_code, 403)
        mod = self.login("moder", "moder123", client=core.app.test_client())
        mod.post(f"/admin/request/{rid[0]}/approve")
        self.assertEqual(self.sql("select status from requests where id = ?", rid[0])[0][0], "approved")
        self.client.post("/me/request", data={"type": "vacation", "starts_on": "2030-01-01", "ends_on": "2030-01-10"})
        self.assertEqual(self.sql("select status from requests where type = 'vacation'")[0][0], "pending")
        self.assertIn("Лікарняний", self.client.get("/me?tab=schedule").get_data(as_text=True))
        # лікарняний і відпустку колеги бачать лише модератор і адмін; дей-офф бачать усі
        week = "&weekend=1&week=" + today
        olha_c = self.login("olha", client=core.app.test_client())
        self.assertNotIn("a-sick_leave", olha_c.get("/me?tab=schedule" + week).get_data(as_text=True).split("legend")[0].split("<table")[-1])
        self.assertIn("a-sick_leave", mod.get("/admin?tab=schedule" + week).get_data(as_text=True))
        rows = core.live_monitor(core.connect())
        self.assertEqual([r["state"] for r in rows if r["user"]["id"] == ivan], ["absent"])
        admin = self.login("admin", "admin123", client=core.app.test_client())
        olha = self.sql("select id from users where username = 'olha'")[0][0]
        admin.post("/admin/absence", data={"user_id": olha, "type": "day_off", "starts_on": today})
        self.assertEqual(tuple(self.sql("select status, ends_on from requests where user_id = ?", olha)[0]), ("approved", today))
        self.assertFalse(self.sql("select 1 from schedule where user_id = ? and starts_at > ?", olha, core.now()))
        page = admin.get("/admin?tab=schedule&weekend=1").get_data(as_text=True)
        self.assertIn("a-day_off", page)
        self.assertIn("a-day_off", self.client.get("/me?tab=schedule" + week).get_data(as_text=True))
        rid = self.sql("select id from requests where user_id = ?", olha)[0][0]
        admin.post("/admin/absence", data={"cancel_id": rid})
        self.assertEqual(self.sql("select status from requests where id = ?", rid)[0][0], "cancelled")

    def test_dayoff_permission_and_teamlead(self):
        admin = self.login("admin", "admin123", client=core.app.test_client())
        olha = self.sql("select id from users where username = 'olha'")[0][0]
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        admin.post(f"/admin/employee/{olha}/role", json={"role": "teamlead"})
        self.assertEqual(self.sql("select role from users where id = ?", olha)[0][0], "teamlead")
        lead = self.login("olha", client=core.app.test_client())
        self.assertNotEqual(lead.get("/admin?tab=live").status_code, 200)  # тімлід не має повного керування
        emp = self.login("ivan")
        emp.post("/me/request", data={"type": "day_off", "comment": "весілля", "back": "schedule"})
        rid, status = self.sql("select id, status from requests where user_id = ? and type = 'day_off'", ivan)[0]
        self.assertEqual(status, "pending")
        # обрати день без дозволу не можна
        emp.post(f"/me/dayoff/{rid}", data={"day": date.today().isoformat()})
        self.assertEqual(self.sql("select status from requests where id = ?", rid)[0][0], "pending")
        self.assertIn("весілля", lead.get("/me?tab=dayoffs").get_data(as_text=True))
        lead.post("/team/dayoff", data={"rid": rid, "action": "approve"})
        status, until = self.sql("select status, grant_until from requests where id = ?", rid)[0]
        self.assertEqual(status, "granted")
        self.assertAlmostEqual(until - core.now(), 3 * 86400, delta=60)
        self.assertIn("Оберіть день до", emp.get("/me?tab=schedule").get_data(as_text=True))
        day = (date.today() + timedelta(days=5)).isoformat()
        emp.post(f"/me/dayoff/{rid}", data={"day": day})
        self.assertEqual(tuple(self.sql("select status, starts_on, ends_on from requests where id = ?", rid)[0]), ("approved", day, day))
        # тімлід переносить і скасовує
        day2 = (date.today() + timedelta(days=6)).isoformat()
        lead.post("/team/dayoff", data={"rid": rid, "action": "move", "day": day2})
        self.assertEqual(self.sql("select starts_on from requests where id = ?", rid)[0][0], day2)
        lead.post("/team/dayoff", data={"rid": rid, "action": "cancel"})
        self.assertEqual(self.sql("select status from requests where id = ?", rid)[0][0], "cancelled")
        # дозвіл сам, але за 3 дні день не обрано — згорає
        lead.post("/team/dayoff", data={"action": "grant", "user_id": ivan})
        rid2 = self.sql("select id from requests where user_id = ? and status = 'granted'", ivan)[0][0]
        self.sql("update requests set grant_until = ? where id = ?", core.now() - 1, rid2)
        emp.post(f"/me/dayoff/{rid2}", data={"day": day})
        self.assertEqual(self.sql("select status from requests where id = ?", rid2)[0][0], "expired")
        # звичайний працівник не керує дей-оффами
        self.assertEqual(emp.post("/team/dayoff", data={"action": "grant", "user_id": olha}).status_code, 403)

    def test_month_grid_and_teamlead_swap(self):
        admin = self.login("admin", "admin123", client=core.app.test_client())
        olha = self.sql("select id from users where username = 'olha'")[0][0]
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        admin.post(f"/admin/employee/{olha}/role", json={"role": "teamlead"})
        lead = self.login("olha", client=core.app.test_client())
        emp = self.login("ivan")
        # працівник сам не міняється і не редагує графік
        sc0 = self.sql("select id from schedule where user_id = ? limit 1", ivan)
        self.assertNotIn('data-dialog="swapDlg"', emp.get("/me?tab=schedule").get_data(as_text=True))
        if sc0:
            self.assertEqual(emp.post("/me/request", data={"type": "shift_swap", "schedule_id": sc0[0][0], "swap_with_id": olha}).status_code, 403)
        day = date.today() + timedelta(days=10)
        self.assertEqual(emp.post("/schedule/toggle", json={"user_id": ivan, "day": day.isoformat()}).status_code, 403)
        page = emp.get(f"/me?tab=schedule&view=month&month={day:%Y-%m}").get_data(as_text=True)
        self.assertIn('class="month mode-shift"', page)
        self.assertNotIn("data-day=", page)
        # тімлід ставить і прибирає зміну кліком по клітинці
        self.sql("delete from schedule where user_id = ?", ivan)
        r = lead.post("/schedule/toggle", json={"user_id": ivan, "day": day.isoformat()}).get_json()
        self.assertTrue(r["on"])
        self.assertEqual(r["label"], "9–18")
        self.assertEqual(len(self.sql("select 1 from schedule where user_id = ?", ivan)), 1)
        self.assertFalse(lead.post("/schedule/toggle", json={"user_id": ivan, "day": day.isoformat()}).get_json()["on"])
        self.assertIn("data-day=", lead.get(f"/me?tab=schedule&view=month&month={day:%Y-%m}").get_data(as_text=True))
        # «Розставити робочі дні» на наступний місяць: Пн–Пт кожному
        nxt = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
        self.sql("delete from schedule")
        lead.post("/schedule/fill", data={"month": nxt.strftime("%Y-%m")})
        users = self.sql("select count(*) from users where status not in ('dismissed', 'pending')")[0][0]
        mg = core.month_grid(core.connect(), nxt)
        workdays = sum(1 for d in mg["days"] if not d["weekend"])
        self.assertEqual(self.sql("select count(*) from schedule")[0][0], users * workdays)
        self.assertIn("Місяць", admin.get(f"/admin?tab=schedule&view=month&month={nxt:%Y-%m}").get_data(as_text=True))
        # тімлід передає зміну
        sc = self.sql("select id from schedule where user_id = ? order by starts_at limit 1", ivan)[0][0]
        lead.post("/team/swap", data={"schedule_id": sc, "user_id": olha})
        self.assertEqual(self.sql("select user_id from schedule where id = ?", sc)[0][0], olha)
        self.assertEqual(emp.post("/team/swap", data={"schedule_id": sc, "user_id": ivan}).status_code, 403)

    def test_device_binding_and_missed_shift(self):
        with core.connect() as c:
            c.execute("update settings set value = '1' where key = 'auto_start'")
            c.commit()
        phone_ua = {"User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-A515F Build/TP1A; wv) CrewHubApp/1.5 CrewHubDevice/abc123def456",
                    "X-Forwarded-For": "192.168.1.57"}
        new = core.app.test_client()
        new.post("/register", data={"full_name": "Тарас Шевчук", "phone": "063 222 33 44", "password": "secret1",
                                    "password2": "secret1"}, headers=phone_ua)
        uid = self.sql("select id from users where phone = '+380632223344'")[0][0]
        dev = self.sql("select device_key, name, first_ip from devices where user_id = ?", uid)
        self.assertEqual(tuple(dev[0]), ("app-abc123def456", "SM-A515F", "192.168.1.57"))
        admin = self.login("admin", "admin123", client=core.app.test_client())
        self.assertIn("SM-A515F · IP 192.168.1.57", admin.get("/admin?tab=requests").get_data(as_text=True))
        admin.post(f"/admin/registration/{uid}/approve", data={"hourly_rate": "120", "location_id": "1"})
        # автостарт: лише з телефона людини
        u = core.connect().execute("select * from users where id = ?", (uid,)).fetchone()
        with core.connect() as c:
            loc = c.execute("select * from locations where id = 1").fetchone()
            self.assertTrue(core.device_allowed(c, uid, "app-abc123def456", "192.168.1.90"))
            self.assertFalse(core.device_allowed(c, uid, "app-otherphone99", "192.168.1.91"))
            self.assertFalse(core.device_allowed(c, uid, None, "192.168.1.91"))
            c.commit()
        self.assertEqual(self.sql("select last_ip from devices where user_id = ?", uid)[0][0], "192.168.1.90")
        did = self.sql("select id from devices where user_id = ?", uid)[0][0]
        self.assertIn("SM-A515F", admin.get(f"/admin/employee/{uid}").get_data(as_text=True))
        admin.post(f"/admin/employee/{uid}/device/{did}/delete")
        with core.connect() as c:  # новий телефон прив'язується сам
            self.assertTrue(core.device_allowed(c, uid, "app-newphone777", "192.168.1.92"))
            c.commit()
        self.assertEqual(self.sql("select device_key from devices where user_id = ?", uid)[0][0], "app-newphone777")
        # пропущена зміна: запит → модератор підтверджує → зміна в табелі
        emp = self.login("ivan")
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        day = date.today() - timedelta(days=3)
        start, end = f"{day}T09:00", f"{day}T17:30"
        emp.post("/me/request", data={"type": "shift_add", "proposed_start": start, "proposed_end": end, "comment": "телефон розрядився"})
        rid = self.sql("select id from requests where type = 'shift_add' and user_id = ?", ivan)[0][0]
        self.assertIn("Пропущена зміна", admin.get("/admin?tab=requests").get_data(as_text=True))
        mod = self.login("moder", "moder123", client=core.app.test_client())
        mod.post(f"/admin/request/{rid}/approve")
        s = self.sql("select ended_at - started_at, status, is_edited from shifts where user_id = ? and note like 'Пропущена%'", ivan)
        self.assertEqual(tuple(s[0]), (8.5 * 3600, "closed", 1))
        # той самий час удруге не додати (перетин), і не в майбутньому
        emp.post("/me/request", data={"type": "shift_add", "proposed_start": start, "proposed_end": end, "comment": "ще раз"})
        future = date.today() + timedelta(days=1)
        emp.post("/me/request", data={"type": "shift_add", "proposed_start": f"{future}T09:00", "proposed_end": f"{future}T10:00", "comment": "x"})
        self.assertEqual(self.sql("select count(*) from requests where type = 'shift_add'")[0][0], 1)

    def test_live_shows_why_auto_start_did_not_happen(self):
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        self.sql("delete from schedule where user_id = ?", ivan)
        later = datetime.now() + timedelta(hours=3)
        with core.connect() as c:
            c.execute("update settings set value = '1' where key = 'auto_start'")
            c.execute("update settings set value = ? where key = 'work_start'", (later.strftime("%H:%M"),))
            c.execute("update settings set value = ? where key = 'work_end'", ((later + timedelta(hours=1)).strftime("%H:%M"),))
            c.execute("update settings set value = '1' where key = 'work_weekends'")
            c.commit()
        self.login("ivan")
        self.clock("auto")
        row = [r for r in core._live_payload(core.connect())["rows"] if r["id"] == ivan][0]
        self.assertEqual(row["state"], "idle_wifi")
        self.assertIn("не робочий час", row["why"])
        self.clock("auto", ip=HOME_IP)
        row = [r for r in core._live_payload(core.connect())["rows"] if r["id"] == ivan][0]
        self.assertEqual(row["state"], "off")
        self.assertIn("не робоча мережа", row["why"])
        # зареєструвався в браузері телефона, а працює в застосунку: застосунок стає його телефоном
        with core.connect() as c:
            core.remember_device(c, ivan, "web-1234abcd", WORK_IP)
            self.assertTrue(core.device_allowed(c, ivan, "app-phone12345", WORK_IP))
            self.assertFalse(core.device_allowed(c, ivan, "web-1234abcd", WORK_IP))
            c.commit()

    def test_sleeping_phone_is_not_a_wifi_loss(self):
        self.login("ivan")
        app = {"device_id": "x"}
        ua = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) CrewHubApp/1.6 background CrewHubDevice/sleepy0001",
              "X-Forwarded-For": WORK_IP}
        self.client.post("/api/clock/start", json=app, headers=ua)
        self.client.post("/api/clock/heartbeat", json=app, headers=ua)
        sid = self.sql("select id from shifts where device = 'app-sleepy0001'")[0][0]
        self.sql("update shifts set started_at = started_at - 7200 where id = ?", sid)
        # 10 хв без пінгу: для браузера це вже втрата, для застосунку — телефон спить
        self.sql("update shifts set last_seen_at = last_seen_at - 600 where id = ?", sid)
        with core.connect() as c:
            core.sweep(c)
        self.assertEqual(self.sql("select status from shifts where id = ?", sid)[0][0], "active")
        # 40 хв тиші: пауза «мовчання», але телефон озвався з точки без ознак виходу — час рахується
        self.sql("update shifts set last_seen_at = last_seen_at - 1800 where id = ?", sid)
        with core.connect() as c:
            core.sweep(c)
        self.assertEqual(self.sql("select status from shifts where id = ?", sid)[0][0], "paused")
        res = self.client.post("/api/clock/auto", json=app, headers=ua).get_json()
        self.assertEqual(res["shift"]["status"], "active")
        self.assertEqual(res["shift"]["unpaid_break_seconds"], 0)
        # справжній вихід: застосунок повідомляє, коли Wi-Fi зник — ця частина не оплачується
        away = core.now() - 900
        res = self.client.post("/api/clock/heartbeat", json={"away_since": away}, headers=ua).get_json()
        self.assertGreaterEqual(res["shift"]["unpaid_break_seconds"], 890)
        # «пішов» через мобільний інтернет → одразу пауза
        self.client.post("/api/clock/left", json={"away_since": core.now()}, headers=dict(ua, **{"X-Forwarded-For": HOME_IP}))
        self.assertEqual(self.sql("select status from shifts where id = ?", sid)[0][0], "paused")
        self.assertEqual(self.sql("select reason from breaks where ended_at is null")[0][0], "left")

    def test_wal_and_backup(self):
        with core.connect() as c:
            self.assertEqual(c.execute("pragma journal_mode").fetchone()[0], "wal")
            self.assertEqual(c.execute("pragma busy_timeout").fetchone()[0], 15000)
        mod = self.login("moder", "moder123")
        self.assertEqual(mod.get("/admin/backup.zip").status_code, 403)
        admin = self.login("admin", "admin123", client=core.app.test_client())
        self.sync()
        r = admin.get("/admin/backup.zip")
        self.assertEqual(r.status_code, 200)
        import io
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
        self.assertIn("crewhub.db", names)
        self.assertIn("people/Іван Петренко/profile.json", names)  # копія містить і особові папки
        self.assertTrue(os.listdir(core.MANUAL_BACKUP_DIR))  # копія на вимогу — окремо від щоденних
        self.assertIn("Резервні копії", admin.get("/admin?tab=settings").get_data(as_text=True))

    def test_server_downtime_does_not_pause_shifts(self):
        self.login("ivan")
        self.clock("start")
        with core.connect() as c:
            core.sweep(c)
            # сервер «лежав» 10 хвилин: ні пінгів, ні перевірок
            c.execute("update meta set value = value - 600 where key = 'alive_at'")
            c.execute("update shifts set last_seen_at = last_seen_at - 600")
            self.assertEqual(core.sweep(c), 0)
        self.assertEqual(self.sql("select status from shifts")[0][0], "active")
        self.assertTrue(self.sql("select 1 from conn_log where event = 'server_downtime'"))
        with core.connect() as c:  # а справжня втрата Wi-Fi без простою сервера так само ловиться
            c.execute("update shifts set last_seen_at = last_seen_at - 600")
            self.assertEqual(core.sweep(c), 1)

    def test_corrupted_db_is_restored_from_backup(self):
        core.backup_db()
        with open(core.DB_PATH, "r+b") as f:
            f.seek(0)
            f.write(b"garbage" * 100)
        msg = core.check_db()
        self.assertIn("відновлено з копії", msg)
        core.init_db()
        self.assertTrue(self.sql("select 1 from users where username = 'admin'"))
        self.assertIsNone(core.check_db())

    def test_health_errors_and_maintenance(self):
        self.assertTrue(self.client.get("/health").get_json()["ok"])
        self.login("ivan")
        with core.connect() as c:
            c.execute("insert into conn_log(user_id, event, allowed, created_at) values (1, 'start', 1, 1)")
        core.maintenance()
        self.assertFalse(self.sql("select 1 from conn_log where created_at = 1"))
        core.app.config["PROPAGATE_EXCEPTIONS"] = False
        orig = core.clock_action
        core.clock_action = lambda *a, **k: 1 / 0
        try:
            r = self.client.post("/api/clock/check", json={})
            self.assertEqual(r.status_code, 500)
            self.assertIn("спробуйте ще раз", r.get_json()["error"])
            self.assertEqual(self.client.get("/health").status_code, 200)  # сервер живий
        finally:
            core.clock_action = orig

    def test_person_folders(self):
        import json
        import shutil
        self.login("ivan")
        self.clock("start")
        self.clock("stop")
        self.client.post("/me/request", data={"type": "sick_leave", "starts_on": "2026-10-20", "ends_on": "2026-10-21"})
        self.sync()
        path = self.folder("ivan")
        self.assertEqual(os.path.basename(path), "Іван Петренко")
        for f in ("profile.json", "картка.txt", "shifts.csv", "shifts.json", "requests.json"):
            self.assertTrue(os.path.exists(os.path.join(path, f)), f)
        with open(os.path.join(path, "profile.json"), encoding="utf-8") as f:
            p = json.load(f)
        self.assertEqual((p["phone"], p["role"]), ("+380 67 123 45 67", "employee"))
        self.assertEqual(len(json.load(open(os.path.join(path, "shifts.json"), encoding="utf-8"))), 1)
        self.assertIn("Лікарняний", open(os.path.join(path, "картка.txt"), encoding="utf-8").read())
        self.assertEqual(len(os.listdir(core.PEOPLE_DIR)), 4)  # кожен у своїй папці

        # Правка profile.json вручну → у базі (посада); ставку файлом не змінити — лише адмін в адмінці
        p["job_title"] = "Старший оператор"
        open(os.path.join(path, "profile.json"), "w", encoding="utf-8").write(json.dumps(p, ensure_ascii=False))
        self.sync()
        self.assertEqual(tuple(self.sql("select job_title, hourly_rate from users where username = 'ivan'")[0]), ("Старший оператор", 120))
        p = json.load(open(os.path.join(path, "profile.json"), encoding="utf-8"))
        p["hourly_rate"], p["role"] = 150, "admin"
        open(os.path.join(path, "profile.json"), "w", encoding="utf-8").write(json.dumps(p, ensure_ascii=False))
        self.sync()
        self.assertEqual(tuple(self.sql("select role, hourly_rate from users where username = 'ivan'")[0]), ("employee", 120))
        self.assertIn("лише адміністратор", open(os.path.join(path, "ПОМИЛКА.txt"), encoding="utf-8").read())
        self.sync()  # папку переписано з бази: у файлі знову справжні роль і ставка
        # Невалідна правка не проходить, пояснення лежить у папці
        p = json.load(open(os.path.join(path, "profile.json"), encoding="utf-8"))
        p["phone"] = "+380500000001"  # номер адміна
        open(os.path.join(path, "profile.json"), "w", encoding="utf-8").write(json.dumps(p, ensure_ascii=False))
        self.sync()
        self.assertTrue(os.path.exists(os.path.join(path, "ПОМИЛКА.txt")))
        self.assertEqual(self.sql("select phone from users where username = 'ivan'")[0][0], "+380671234567")

        # Зміна імені в адмінці перейменовує папку
        admin = self.login("admin", "admin123", client=core.app.test_client())
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        admin.post(f"/admin/employee/{uid}", data={"full_name": "Іван Коваленко", "hourly_rate": "150", "status": "active", "phone": "0671234567"})
        self.sync()
        self.assertTrue(os.path.isdir(os.path.join(core.PEOPLE_DIR, "Іван Коваленко")))
        self.assertFalse(os.path.exists(path))

        # Видалили папку → людину НЕ видаляємо (база — джерело правди), папку відтворюємо з бази
        shutil.rmtree(os.path.join(core.PEOPLE_DIR, "Іван Коваленко"))
        self.sync()
        self.assertTrue(self.sql("select 1 from users where id = ?", uid))  # акаунт на місці
        self.assertEqual(self.sql("select count(*) from shifts where user_id = ?", uid)[0][0], 1)  # зміни на місці
        self.assertTrue(os.path.isdir(os.path.join(core.PEOPLE_DIR, "Іван Коваленко")))  # папку відтворено
        self.assertTrue(os.path.exists(os.path.join(core.PEOPLE_DIR, "Іван Коваленко", "profile.json")))

        # Стороння папка НЕ створює акаунт сама — лише через ручний CLI-імпорт
        dropped = os.path.join(core.PEOPLE_DIR, "Нова Людина")
        shutil.copytree(os.path.join(core.PEOPLE_DIR, "Іван Коваленко"), dropped)
        p2 = json.load(open(os.path.join(dropped, "profile.json"), encoding="utf-8"))
        p2["full_name"], p2["phone"], p2["username"] = "Нова Людина", "0991112233", "nova"
        p2.pop("_sync", None)
        open(os.path.join(dropped, "profile.json"), "w", encoding="utf-8").write(json.dumps(p2, ensure_ascii=False))
        self.sync()
        self.assertFalse(self.sql("select 1 from users where phone = '+380991112233'"))  # авто-імпорту немає
        self.assertTrue(os.path.exists(os.path.join(dropped, "ПОМИЛКА.txt")))  # підказка про ручний імпорт
        with core.connect() as c:
            new_id = profiles.import_folder(c, "Нова Людина")
            c.commit()
        self.assertTrue(new_id)  # ручний імпорт створив акаунт
        self.assertEqual(self.sql("select full_name from users where id = ?", new_id)[0][0], "Нова Людина")
        self.login("0671234567", "1234", client=core.app.test_client())

    def test_folder_safety(self):
        import shutil
        self.sync()
        # Зникла вся папка people (від'єднали диск) → нікого не видаляємо, папки відтворюються
        shutil.rmtree(core.PEOPLE_DIR)
        self.sync()
        self.sync()
        self.assertEqual(self.sql("select count(*) from users")[0][0], 4)
        self.assertTrue(os.path.exists(os.path.join(self.folder("ivan"), "profile.json")))
        # Папку єдиного адміна видалити не можна: її відтворено
        shutil.rmtree(self.folder("admin"))
        self.sync()
        self.sync()
        self.assertTrue(self.sql("select 1 from users where username = 'admin'"))
        self.assertTrue(os.path.isdir(self.folder("admin")))
        # Видалення в адмінці переносить папку в backups/видалені
        admin = self.login("admin", "admin123")
        olha = self.sql("select id from users where username = 'olha'")[0][0]
        admin.post(f"/admin/employee/{olha}/delete")
        self.assertTrue(os.listdir(os.path.join(core.BACKUP_DIR, "видалені")))
        self.assertFalse(os.path.exists(os.path.join(core.PEOPLE_DIR, "Ольга Коваль")))
        r = admin.get(f"/admin/employee/{self.sql('select id from users where username = %s' % repr('ivan'))[0][0]}/folder.zip")
        self.assertEqual(r.mimetype, "application/zip")

    def test_recovery_rebuilds_people_from_folders(self):
        self.login("ivan")
        core.backup_db()  # копія, де у Івана ще немає змін
        self.clock("start")
        self.clock("stop")
        self.sync()
        with open(core.DB_PATH, "r+b") as f:
            f.write(b"broken" * 200)
        self.assertIn("відновлено", core.check_db())
        core.init_db()
        with core.connect() as c:
            profiles.rebuild_from_folders(c)
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        self.assertEqual(self.sql("select count(*) from shifts where user_id = ?", uid)[0][0], 1)  # зміна з папки
        core.RECOVERED["from_backup"] = False

    def test_empty_db_recovers_from_folders_on_start(self):
        # Люди й папки є
        self.sync()
        folders = profiles.valid_folders()
        self.assertGreater(len(folders), 0)
        # База «спорожніла» після перезапуску (лишився тільки адмін)
        self.sql("delete from users where role != 'admin'")
        self.assertEqual(self.sql("select count(*) from users where role != 'admin'")[0][0], 0)
        # Старт відновлює працівників з особових папок
        with core.connect() as c:
            n = profiles.recover_if_empty(c)
        self.assertGreater(n, 0)
        self.assertGreater(self.sql("select count(*) from users where role != 'admin'")[0][0], 0)
        self.assertTrue(self.sql("select 1 from users where username = 'ivan'"))
        # Коли база не порожня — відновлення не чіпає нічого (не двостороння синхронізація)
        with core.connect() as c:
            self.assertEqual(profiles.recover_if_empty(c), 0)

    def test_auto_start_and_auto_close(self):
        with core.connect() as c:
            c.execute("update settings set value = '1' where key = 'auto_start'")
        real_now = core.now
        at = lambda d, hm: int(datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M").timestamp())
        wed, sat = "2026-10-21", "2026-10-24"

        def tick(d, hm):
            core.now = lambda: at(d, hm)

        def sweep():
            with core.connect() as c:
                core.meta_set(c, "alive_at", core.now() - 5)  # сервер працював безперервно
                core.sweep(c)
        try:
            self.login("ivan")
            tick(wed, "08:45")
            res = self.clock("auto")  # прийшов раніше: зміна ще не почалась, але присутність запам'ятали
            self.assertTrue(res["ok"] and res["shift"] is None and res["waiting_until"] == at(wed, "09:00"), res)
            tick(wed, "08:20")
            self.assertNotIn("waiting_until", self.clock("auto"))  # зарано, поза запасом 30 хв
            self.assertFalse(self.clock("auto", ip=HOME_IP).get("waiting_until"))
            tick(wed, "08:58")
            self.clock("auto")
            tick(wed, "09:01")
            res = self.clock("auto")
            self.assertTrue(res["auto_started"], res)
            self.assertEqual(res["shift"]["started_at"], at(wed, "09:00"))  # відлік з початку робочого часу
            self.assertEqual(self.sql("select event from conn_log order by id desc limit 1")[0][0], "auto_start")
            tick(wed, "12:00")
            self.assertTrue(self.clock("auto")["shift"]["status"] == "active")
            tick(wed, "18:10")
            self.clock("heartbeat")
            tick(wed, "18:15")
            sweep()  # пішов додому: за 2 хв без пінгу зміна на паузі
            self.assertEqual(self.sql("select status from shifts")[0][0], "paused")
            tick(wed, "18:50")
            sweep()  # після кінця зміни правило «30 хв паузи» не діє, чекаємо 1:30
            self.assertEqual(self.sql("select status from shifts")[0][0], "paused")
            tick(wed, "19:41")
            sweep()
            ended, status, reason = self.sql("select ended_at, status, close_reason from shifts")[0]
            self.assertEqual((status, reason), ("closed", "auto_close"))
            self.assertEqual(ended, at(wed, "18:10"))  # 1:30 очікування не оплачується
            tick(wed, "19:45")
            self.assertIsNone(self.clock("auto")["shift"])  # у тому ж вікні повторно не відкривається
            # Повернувся до закриття — зміна триває
            tick("2026-10-22", "09:05")
            self.assertTrue(self.clock("auto").get("auto_started"))
            tick("2026-10-22", "18:30")
            self.clock("heartbeat")
            tick("2026-10-22", "18:40")
            sweep()
            tick("2026-10-22", "19:30")
            self.assertEqual(self.clock("heartbeat")["shift"]["status"], "active")
            # Закрив сам — сьогодні вже не відкривається
            self.clock("stop")
            tick("2026-10-22", "19:31")
            self.assertIsNone(self.clock("check")["shift"])
            # Субота: без автостарту; відпустка: теж
            tick(sat, "10:00")
            self.assertIsNone(self.clock("auto")["shift"])
            with core.connect() as c:
                uid = c.execute("select id from users where username = 'ivan'").fetchone()[0]
                c.execute("insert into requests(user_id, type, status, starts_on, ends_on, created_at) values (?, 'vacation', 'approved', '2026-10-26', '2026-10-26', 0)", (uid,))
            tick("2026-10-26", "09:10")
            self.assertIsNone(self.clock("auto")["shift"])
            # Нічна зміна через північ
            with core.connect() as c:
                c.execute("update settings set value = '22:00' where key = 'work_start'")
                c.execute("update settings set value = '06:00' where key = 'work_end'")
            tick("2026-10-27", "23:30")
            self.assertTrue(self.clock("auto").get("auto_started"))
            self.clock("stop")
            tick("2026-10-28", "02:00")
            self.assertIsNone(self.clock("auto")["shift"])
        finally:
            core.now = real_now

    def test_roles_by_admin(self):
        moder = self.sql("select id from users where username = 'moder'")[0][0]
        admin = self.sql("select id from users where username = 'admin'")[0][0]
        c = self.login("moder", "moder123", core.app.test_client())
        self.assertEqual(c.post(f"/admin/employee/{moder}/role", json={"role": "admin"}).status_code, 403)
        self.login("admin", "admin123")
        r = self.client.post(f"/admin/employee/{moder}/role", json={"role": "employee"}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(self.sql("select role from users where id = ?", moder)[0][0], "employee")
        r = self.client.post(f"/admin/employee/{admin}/role", json={"role": "moderator"}).get_json()
        self.assertFalse(r["ok"])  # єдиного адміністратора не можна понизити
        self.assertEqual(self.sql("select role from users where id = ?", admin)[0][0], "admin")
        self.client.post(f"/admin/employee/{moder}/role", json={"role": "admin"})
        self.assertTrue(self.client.post(f"/admin/employee/{admin}/role", json={"role": "moderator"}).get_json()["ok"])
        self.assertIn("role-pick", self.login("moder", "moder123", core.app.test_client()).get("/admin?tab=employees").get_data(as_text=True))

    def test_work_version_has_only_admin(self):
        self.assertEqual(self.sql("select count(*) from users")[0][0], 4)
        old = core.DEMO_ACCOUNTS
        core.DEMO_ACCOUNTS = False
        try:
            core.init_db()  # оновлення робочої версії: тестові акаунти зникають один раз
            self.assertEqual([r[0] for r in self.sql("select username from users")], ["admin"])
            page = self.client.get("/login").get_data(as_text=True)
            self.assertNotIn("Тестові акаунти", page)
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(core.DB_PATH + suffix):
                    os.remove(core.DB_PATH + suffix)
            core.init_db()  # чиста інсталяція
            self.assertEqual([r[0] for r in self.sql("select username from users")], ["admin"])
        finally:
            core.DEMO_ACCOUNTS = old

    def test_tenure(self):
        self.assertEqual(core.tenure_text("2025-05-01", date(2026, 10, 18)), "В команді з травня 2025 (1 рік 5 міс.)")
        self.assertEqual(core.tenure_text("2023-03-10", date(2026, 3, 10)), "В команді з березня 2023 (3 роки)")

    def test_dayoff_limits_and_pay(self):
        admin = self.login("admin", "admin123", client=core.app.test_client())
        ivan, olha = (self.sql("select id from users where username = ?", n)[0][0] for n in ("ivan", "olha"))
        # дей-офф у минулому робочому дні цього місяця (або минулого, якщо місяць лише почався)
        d = date.today() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        day = d.isoformat()
        admin.post("/admin/absence", data={"user_id": ivan, "type": "day_off", "starts_on": day})
        self.assertEqual(len(self.sql("select 1 from requests where user_id = ? and status = 'approved'", ivan)), 1)
        # той самий день іншій людині — ліміт 1 на день
        r = admin.post("/admin/absence", data={"user_id": olha, "type": "day_off", "starts_on": day}, follow_redirects=True)
        self.assertIn("уже взяли", r.get_data(as_text=True))
        self.assertFalse(self.sql("select 1 from requests where user_id = ? and type = 'day_off'", olha))
        # другий дей-офф у тому ж місяці — ліміт 1 на місяць
        other = (d.replace(day=1) if d.day > 1 else d.replace(day=2)).isoformat()
        r = admin.post("/admin/absence", data={"user_id": ivan, "type": "day_off", "starts_on": other}, follow_redirects=True)
        self.assertIn("на місяць", r.get_data(as_text=True))
        # ліміт налаштовується: 2 людини на день
        self.sql("update settings set value = '2' where key = 'dayoff_per_day'")
        admin.post("/admin/absence", data={"user_id": olha, "type": "day_off", "starts_on": day})
        self.assertEqual(len(self.sql("select 1 from requests where user_id = ? and type = 'day_off'", olha)), 1)
        # оплата: повний робочий день 09:00–18:00 за ставкою (Іван 120 грн/год)
        rows = {r["user"]["username"]: r for r in core.payroll(core.connect(), d.replace(day=1), d)}
        self.assertEqual(rows["ivan"]["dayoff"], 9 * 120)
        self.assertEqual(rows["ivan"]["total"], 9 * 120)
        ts = core.timesheet(core.connect(), ivan, d.strftime("%Y-%m"))
        self.assertEqual(ts["totals"]["dayoff"], 9 * 120)
        self.sql("update settings set value = '0' where key = 'dayoff_paid'")
        self.assertEqual(core.payroll(core.connect(), d.replace(day=1), d, ivan)[0]["total"], 0)

    def test_offline_keeps_counting(self):
        self.sql("update settings set value = '1' where key = 'offline_keeps_counting'")
        self.sql("update settings set value = '1' where key = 'work_weekends'")
        self.sql("update settings set value = '00:00' where key = 'work_start'")
        self.sql("update settings set value = '23:59' where key = 'work_end'")
        self.login("ivan")
        self.clock("start")
        self.clock("heartbeat", ip=HOME_IP)  # пішов у іншу мережу — лічильник не зупиняється
        self.sql("update shifts set last_seen_at = last_seen_at - 3600")
        with core.connect() as c:
            core.sweep(c)
        self.assertEqual(self.sql("select status from shifts")[0][0], "active")
        self.assertFalse(self.sql("select 1 from breaks"))

    def test_unrestricted_app_silence_uses_sleep_grace(self):
        self.login("ivan")
        ua = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) CrewHubApp/1.8 background CrewHubDevice/fast0001",
              "X-Forwarded-For": WORK_IP}
        self.client.post("/api/clock/start", json={}, headers=ua)
        self.client.post("/api/clock/heartbeat", json={"unrestricted": True}, headers=ua)
        self.assertEqual(self.sql("select bg_ok from shifts")[0][0], 1)
        # Служба більше не тримає процесор увімкненим усю зміну (батарея): у сні пінг буває раз на 5–15 хв,
        # тож 4 хв тиші — ще сон. Вихід з Wi-Fi застосунок повідомляє сам (away_since)
        self.sql("update shifts set last_seen_at = last_seen_at - 240")
        with core.connect() as c:
            core.sweep(c)
        self.assertEqual(self.sql("select status from shifts")[0][0], "active")
        self.sql("update shifts set last_seen_at = last_seen_at - 1500")  # 25 хв тиші — телефон вимкнули
        with core.connect() as c:
            core.sweep(c)
        self.assertEqual(self.sql("select status from shifts")[0][0], "paused")

    def test_connect_qr_and_code(self):
        self.assertEqual(core.connect_code("http://192.168.1.23:5000/"), "GFD68-R2VZM")
        self.assertIsNone(core.connect_code("https://crewhub.example.com/"))
        admin = self.login("admin", "admin123")
        r = admin.post("/admin/zones", data={"action": "wifi", "location_id": 1, "wifi_ssid": "Office", "wifi_password": "secret123"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(tuple(self.sql("select wifi_ssid, wifi_password from locations where id = 1")[0]), ("Office", "secret123"))
        html = admin.get("/admin?tab=zones").get_data(as_text=True)
        self.assertIn("crewhub://connect?s=", html)
        lan = "http://" + core.get_local_ip() + "/"  # адмін відкрив як localhost — у QR адреса в локальній мережі
        self.assertIn(core.connect_code(lan) or "crewhub://", html)
        import base64
        from urllib.parse import parse_qs, urlsplit
        link = html.split('data-qr="')[1].split('"')[0].replace("&amp;", "&")
        q = parse_qs(urlsplit(link).query)
        self.assertEqual(base64.urlsafe_b64decode(q["s"][0] + "==").decode(), lan)
        self.assertEqual(base64.urlsafe_b64decode(q["w"][0] + "==").decode(), "Office")
        # працівник у застосунку бачить назву компанії (мета-тег), а не адресу
        self.assertIn('name="crewhub-company"', self.client.get("/login").get_data(as_text=True))

    def test_whatsapp_contact(self):
        self.assertEqual(core.wa_link("067 123 45 67"), "https://wa.me/380671234567")
        self.assertEqual(core.wa_link("+380501112233"), "https://wa.me/380501112233")
        self.assertIsNone(core.wa_link("123"))
        emp = self.login("ivan")
        emp.post("/me/profile", data={"telegram": "@ivan", "whatsapp": "067 123 45 67"})
        self.assertEqual(self.sql("select whatsapp from users where username = 'ivan'")[0][0], "067 123 45 67")
        self.assertIn("067 123 45 67", emp.get("/me?tab=profile").get_data(as_text=True))  # збережений номер у формі
        self.assertIn("wa.me/380671234567", emp.get("/me?tab=team").get_data(as_text=True))  # посилання в картці колеги


    def _run_reset(self, argv, answers, passwords):
        """Проганяє recover.main, підставляючи input()/getpass() зі списків."""
        import builtins
        import getpass as gp
        ai, pi = iter(answers), iter(passwords)
        orig_in, orig_pw = builtins.input, gp.getpass
        builtins.input = lambda *a: next(ai)
        gp.getpass = lambda *a: next(pi)
        try:
            return recover.main(argv)
        finally:
            builtins.input, gp.getpass = orig_in, orig_pw

    def test_admin_password_reset(self):
        # Скидання пароля адміна з сервера: питає новий пароль, хешує, ставить must_change_password
        stolen = self.login("admin", "admin123", client=core.app.test_client())  # чужий пристрій з «запам'ятати мене»
        self.assertTrue(self.sql("select 1 from remember_tokens"))
        rc = self._run_reset(["recover_admin.py", "admin"], answers=[], passwords=["newpass123", "newpass123"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.sql("select 1 from remember_tokens"))  # усі входи «запам'ятати мене» скасовано
        self.assertEqual(self.sql("select must_change_password from users where username = 'admin'")[0][0], 1)
        self.login("admin", "newpass123", client=core.app.test_client())  # новий пароль працює
        self.assertTrue(self.sql("select 1 from audit where action = 'PASSWORD_RESET'"))
        # старий токен входу на чужому пристрої більше не діє
        with stolen.session_transaction() as sess:
            sess.clear()
        self.assertEqual(stolen.get("/admin").status_code, 302)

    def test_reset_only_targets_admins(self):
        # Не адміну скинути не можна; паролі мають збігтися
        old = self.sql("select password_hash from users where username = 'ivan'")[0][0]
        self.assertEqual(self._run_reset(["recover_admin.py", "ivan"], [], []), 1)  # ivan — не адмін
        self.assertEqual(self.sql("select password_hash from users where username = 'ivan'")[0][0], old)
        admin_old = self.sql("select password_hash from users where username = 'admin'")[0][0]
        self.assertEqual(self._run_reset(["recover_admin.py", "admin"], [], ["abcdef", "zzzzzz"]), 1)  # паролі різні
        self.assertEqual(self.sql("select password_hash from users where username = 'admin'")[0][0], admin_old)



if __name__ == "__main__":
    unittest.main()
