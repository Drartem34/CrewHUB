"""Тести на виправлені дірки й баги (по одному на кожен пункт аудиту): python -m unittest test_hardening -v"""
import unittest

from test_crewhub import HOME_IP, WORK_IP, CrewHubBase, core, profiles


class HardeningTest(CrewHubBase):
    # --- 🔴 1. Підробка IP -------------------------------------------------------------------
    def test_forwarded_for_takes_address_added_by_tunnel(self):
        # Клієнт сам дописав робочий IP лівіше, тунель додав справжній домашній — вірити можна лише останньому
        self.login("ivan")
        r = self.client.post("/api/clock/start", json={}, headers={"X-Forwarded-For": f"{WORK_IP}, {HOME_IP}"}).get_json()
        self.assertFalse(r["ok"])
        self.assertEqual(r["ip"], HOME_IP)

    def test_forwarded_for_ignored_without_trusted_tunnel(self):
        self.login("ivan")
        core.TRUST_PROXY = False
        try:
            r = self.client.post("/api/clock/start", json={}, headers={"X-Forwarded-For": WORK_IP}).get_json()
        finally:
            core.TRUST_PROXY = True
        self.assertFalse(r["ok"])

    def test_server_itself_is_never_a_work_network(self):
        # Тунель без X-Forwarded-For: усі запити з 127.0.0.1 — це не «робочий Wi-Fi»
        self.login("ivan")
        r = self.client.post("/api/clock/check", json={}).get_json()
        self.assertFalse(r["on_work_network"])
        self.assertFalse(self.client.post("/api/clock/start", json={}).get_json()["ok"])
        admin = self.login("admin", "admin123", client=core.app.test_client())
        for cidr in ("127.0.0.0/8", "0.0.0.0/0", "8.0.0.0/8"):
            admin.post("/admin/zones", data={"action": "network", "cidr": cidr, "location_id": "1"})
            self.assertFalse(self.sql("select 1 from networks where cidr = ?", cidr), cidr)
        admin.post("/admin/zones", data={"action": "bind_current", "location_id": "1"})
        self.assertFalse(self.sql("select 1 from networks where cidr like '127.%'"))

    def test_old_loopback_zone_removed_on_start(self):
        with core.connect() as c:
            c.execute("insert into networks(location_id, cidr, label) values (1, '127.0.0.0/8', 'Цей комп''ютер')")
        core.init_db()
        self.assertFalse(self.sql("select 1 from networks where cidr = '127.0.0.0/8'"))

    # --- 🔴 2. «Прощення тиші» ----------------------------------------------------------------
    APP = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) CrewHubApp/1.9 background CrewHubDevice/phone0001"}

    def _silent(self, seconds, ua=None, body=None):
        """Година роботи, потім `seconds` тиші, sweep, телефон знову озивається з точки. Повертає оплачені секунди."""
        headers = dict(ua or {}, **{"X-Forwarded-For": WORK_IP})
        self.client.post("/api/clock/start", json=body or {}, headers=headers)
        self.client.post("/api/clock/heartbeat", json=body or {}, headers=headers)
        self.sql("update shifts set started_at = started_at - ? - 3600, last_seen_at = last_seen_at - ?", seconds, seconds)
        with core.connect() as c:
            core.sweep(c)
            self.client.post("/api/clock/heartbeat", json=body or {}, headers=headers)
            return core.shift_stats(c, c.execute("select * from shifts").fetchone())["worked_seconds"]

    def test_browser_absence_is_not_forgiven(self):
        self.login("ivan")
        self.assertLess(self._silent(20 * 60), 3600 + 60)       # пауза ще відкрита: 20 хв не оплачено
        self.sql("delete from shifts")
        self.assertLess(self._silent(3 * 3600), 3600 + 60)      # зміну закрито часом останнього пінгу

    def test_app_long_silence_is_not_forgiven(self):
        self.login("ivan")
        self.assertLess(self._silent(3 * 3600, self.APP), 3600 + 60)

    def test_app_short_sleep_is_forgiven(self):
        self.login("ivan")
        self.assertGreaterEqual(self._silent(30 * 60, self.APP), 3600 + 30 * 60 - 5)

    def test_unrestricted_app_silence_is_not_forgiven(self):
        self.login("ivan")
        self.assertLess(self._silent(30 * 60, self.APP, {"unrestricted": True}), 3600 + 60)

    def test_fake_away_since_cannot_shrink_long_absence(self):
        self.login("ivan")
        headers = dict(self.APP, **{"X-Forwarded-For": WORK_IP})
        self.client.post("/api/clock/start", json={}, headers=headers)
        sid = self.sql("select id from shifts")[0][0]
        t = core.now()
        # 3 год «мовчання», sweep ще не встиг закрити зміну; застосунок «каже», що вийшов лише хвилину тому
        self.sql("update shifts set started_at = ?, last_seen_at = ?, status = 'paused' where id = ?", t - 4 * 3600, t - 3 * 3600, sid)
        self.sql("insert into breaks(shift_id, kind, is_paid, rate_pct, started_at, reason) values (?, 'wifi_loss', 0, 0, ?, 'silence')",
                 sid, t - 3 * 3600)
        res = self.client.post("/api/clock/heartbeat", json={"away_since": t - 60}, headers=headers).get_json()
        self.assertGreaterEqual(res["shift"]["unpaid_break_seconds"], 3 * 3600 - 5)

    def test_old_forgive_default_lowered(self):
        self.sql("update settings set value = '240' where key = 'silence_forgive_minutes'")
        self.sql("delete from meta where key = 'forgive_v2'")
        core.init_db()
        self.assertEqual(self.sql("select value from settings where key = 'silence_forgive_minutes'")[0][0], "45")

    # --- 🔴 3. Модератор і власні записи -------------------------------------------------------
    def test_moderator_cannot_touch_own_money_or_time(self):
        mod = self.login("moder", "moder123")
        mid = self.sql("select id from users where username = 'moder'")[0][0]
        rate = self.sql("select hourly_rate from users where id = ?", mid)[0][0]
        mod.post(f"/admin/employee/{mid}", data={"full_name": "Марія", "hourly_rate": "5000", "status": "active"})
        self.assertEqual(self.sql("select hourly_rate from users where id = ?", mid)[0][0], rate)
        mod.post("/admin/adjustment", data={"user_id": mid, "amount": "99999", "reason": "собі", "kind": "bonus"})
        self.assertFalse(self.sql("select 1 from adjustments where user_id = ?", mid))
        mod.post("/admin/shift/save", data={"user_id": mid, "started_at": "2026-10-01T00:00", "ended_at": "2026-10-01T23:00"})
        self.assertFalse(self.sql("select 1 from shifts where user_id = ?", mid))
        mod.post("/me/request", data={"type": "vacation", "starts_on": "2026-12-01", "ends_on": "2026-12-20", "comment": "x"})
        rid = self.sql("select id from requests where user_id = ?", mid)[0][0]
        mod.post(f"/admin/request/{rid}/approve")
        self.assertEqual(self.sql("select status from requests where id = ?", rid)[0][0], "pending")
        mod.post("/admin/absence", data={"user_id": mid, "type": "day_off", "starts_on": "2026-12-24"})
        self.assertEqual(self.sql("select count(*) from requests where user_id = ?", mid)[0][0], 1)
        r = mod.post("/schedule/dayoff", json={"user_id": mid, "day": "2026-12-25"}).get_json()
        self.assertFalse(r["ok"])
        # а щодо інших модератор працює як раніше, і адмін може погодити запит модератора
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        mod.post("/admin/adjustment", data={"user_id": ivan, "amount": "100", "reason": "молодець", "kind": "bonus"})
        self.assertTrue(self.sql("select 1 from adjustments where user_id = ?", ivan))
        admin = self.login("admin", "admin123", client=core.app.test_client())
        admin.post(f"/admin/request/{rid}/approve")
        self.assertEqual(self.sql("select status from requests where id = ?", rid)[0][0], "approved")

    # --- 🔴 4. Останній адміністратор ---------------------------------------------------------
    def test_last_admin_cannot_be_dismissed(self):
        admin = self.login("admin", "admin123")
        aid = self.sql("select id from users where username = 'admin'")[0][0]
        form = {"full_name": "Адмін", "hourly_rate": "0", "status": "dismissed", "phone": "+380500000001"}
        admin.post(f"/admin/employee/{aid}", data=form)
        self.assertEqual(self.sql("select status from users where id = ?", aid)[0][0], "active")
        # з другим адміністратором — можна
        mid = self.sql("select id from users where username = 'moder'")[0][0]
        self.sql("update users set role = 'admin' where id = ?", mid)
        admin.post(f"/admin/employee/{aid}", data=form)
        self.assertEqual(self.sql("select status from users where id = ?", aid)[0][0], "dismissed")
        # тепер єдиний діючий — колишній модератор: звільненого адміна не рахуємо
        other = self.login("moder", "moder123", client=core.app.test_client())
        other.post(f"/admin/employee/{mid}", data=dict(form, phone="+380500000002"))
        self.assertEqual(self.sql("select status from users where id = ?", mid)[0][0], "active")

    # --- 🔴 5. Стандартний пароль admin123 ---------------------------------------------------
    def test_default_admin_password_must_be_changed(self):
        orig = core.DEMO_ACCOUNTS
        core.DEMO_ACCOUNTS = False
        self.addCleanup(setattr, core, "DEMO_ACCOUNTS", orig)
        core.init_db()  # стара база: адмін досі з admin123 → при вході вимога змінити
        self.assertEqual(self.sql("select must_change_password from users where username = 'admin'")[0][0], 1)
        admin = self.login("admin", "admin123")
        r = admin.get("/admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/password", r.headers["Location"])
        admin.post("/password", data={"password": "admin123", "password2": "admin123"})
        self.assertEqual(self.sql("select must_change_password from users where username = 'admin'")[0][0], 1)
        admin.post("/password", data={"password": "Nokia-3310", "password2": "Nokia-3310"})
        self.assertEqual(admin.get("/admin").status_code, 200)
        core.init_db()  # свій пароль — більше не чіпаємо
        self.assertEqual(self.sql("select must_change_password from users where username = 'admin'")[0][0], 0)

    # --- 🔴 6. Перебір паролів ----------------------------------------------------------------
    def test_password_guessing_is_throttled(self):
        c = core.app.test_client()
        for i in range(5):
            self.assertEqual(c.post("/login", data={"username": "067 123 45 67", "password": f"x{i}"}).status_code, 200)
        # шоста спроба навіть з правильним паролем — відмова; інший запис того ж номера — той самий акаунт
        r = c.post("/login", data={"username": "+380671234567", "password": "1234"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Забагато невдалих спроб", r.get_data(as_text=True))
        # з іншого IP людина входить
        other = core.app.test_client()
        r = other.post("/login", data={"username": "0671234567", "password": "1234"}, headers={"X-Forwarded-For": "10.0.0.7"})
        self.assertEqual(r.status_code, 302)

    def test_password_spraying_from_one_ip_is_throttled(self):
        c = core.app.test_client()
        h = {"X-Forwarded-For": "10.9.9.9"}
        for i in range(20):
            c.post("/login", data={"username": f"06700000{i:02d}", "password": "123456"}, headers=h)
        self.assertEqual(c.post("/login", data={"username": "0671234567", "password": "1234"}, headers=h).status_code, 429)

    def test_registration_spam_is_throttled(self):
        c = core.app.test_client()
        codes = [c.post("/register", data={"full_name": "Тест Тестенко", "phone": f"06711100{i:02d}", "password": "123456",
                                           "password2": "123456"}).status_code for i in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])

    # --- 🔴 7. Застосунок звіряє сервер перед тим, як віддати cookie ----------------------------
    def test_hello_gives_stable_server_id_without_session(self):
        r = self.client.get("/api/hello")
        data = r.get_json()
        self.assertEqual(data["app"], "crewhub")
        self.assertRegex(data["server_id"], r"^[0-9a-f]{16}$")
        self.assertNotIn("Set-Cookie", r.headers)  # без сесії: телефон не отримує зайвих cookie
        core.init_db()
        self.assertEqual(self.client.get("/api/hello").get_json()["server_id"], data["server_id"])
        admin = self.login("admin", "admin123")
        self.assertIn("&amp;i=" + data["server_id"], admin.get("/admin?tab=zones").get_data(as_text=True))

    # --- 🔴 10. XSS і заголовки безпеки --------------------------------------------------------
    def test_security_headers(self):
        r = self.client.get("/login")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_clock_page_never_inserts_location_as_html(self):
        with open(core.os.path.join(core.BASE_DIR, "static", "clock.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn("${state.location}", js)

    # --- 🔴 11. «Аватарка» /logout і вихід через GET ----------------------------------------------
    def test_photo_url_cannot_be_set_from_form(self):
        mod = self.login("moder", "moder123")
        ivan = self.sql("select id from users where username = 'ivan'")[0][0]
        mod.post(f"/admin/employee/{ivan}", data={"full_name": "Іван Петренко", "hourly_rate": "120", "status": "active",
                                                  "phone": "0671234567", "photo_url": "/logout"})
        self.assertIsNone(self.sql("select photo_url from users where id = ?", ivan)[0][0])
        # стара база з підміненим photo_url: на сторінках і в live — без картинки
        self.sql("update users set photo_url = '/logout' where id = ?", ivan)
        self.assertNotIn('src="/logout"', mod.get("/admin?tab=employees").get_data(as_text=True))
        self.assertNotIn("/logout", str([r["photo"] for r in mod.get("/api/live").get_json()["rows"]]))
        self.assertEqual(core.safe_photo("/avatar/5?v=123"), "/avatar/5?v=123")
        self.assertIsNone(core.safe_photo("https://evil.example/x.png"))

    def test_logout_requires_post(self):
        ivan = self.login("ivan")
        self.assertEqual(ivan.get("/logout").status_code, 200)  # сторінка підтвердження
        self.assertEqual(ivan.get("/me").status_code, 200)       # усе ще в акаунті
        r = ivan.post("/logout")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ivan.get("/me").status_code, 302)

    # --- 🔴 12. Хеші паролів і права через файли ------------------------------------------------
    def test_no_password_hashes_in_folders_or_zip(self):
        import io
        import zipfile
        self.sync()
        admin = self.login("admin", "admin123")
        mod = self.login("moder", "moder123", client=core.app.test_client())
        aid = self.sql("select id from users where username = 'admin'")[0][0]
        z = zipfile.ZipFile(io.BytesIO(mod.get(f"/admin/employee/{aid}/folder.zip").data))
        everything = b"".join(z.read(n) for n in z.namelist())
        self.assertNotIn(b"password_hash", everything)
        self.assertNotIn(b"scrypt:", everything)
        for root, _dirs, files in core.os.walk(core.PEOPLE_DIR):
            for f in files:
                with open(core.os.path.join(root, f), "rb") as fh:
                    self.assertNotIn(b"scrypt:", fh.read(), f)
        self.assertTrue(admin)

    def test_old_folders_rewritten_without_hash(self):
        import json
        self.sync()
        path = self.folder("ivan")
        p = json.load(open(core.os.path.join(path, "profile.json"), encoding="utf-8"))
        p["password_hash"] = "scrypt:old"
        open(core.os.path.join(path, "profile.json"), "w", encoding="utf-8").write(json.dumps(p, ensure_ascii=False))
        self.sql("delete from meta where key = 'profiles_v2'")
        core.init_db()
        self.sync()
        self.assertNotIn("scrypt:", open(core.os.path.join(path, "profile.json"), encoding="utf-8").read())

    # --- 🟠 1. Налаштування не можуть «вбити» сервер ---------------------------------------------
    def test_bad_settings_are_rejected_on_save(self):
        admin = self.login("admin", "admin123")
        form = {k: v for k, v in self.sql("select key, value from settings")}
        form.update(wifi_grace_minutes="2 хв", max_shift_hours="0", weekend_multiplier="nan", night_start="25:99",
                    heartbeat_seconds="inf", company_name="Моя кава", auto_close_minutes="1:30")
        form = {k: v for k, v in form.items() if v != "0" or k == "max_shift_hours"}
        r = admin.post("/admin/settings", data=form, follow_redirects=True)
        self.assertIn("крім неправильних", r.get_data(as_text=True))
        got = dict(self.sql("select key, value from settings"))
        self.assertEqual(got["wifi_grace_minutes"], "2")
        self.assertEqual(got["max_shift_hours"], "16")
        self.assertEqual(got["weekend_multiplier"], "1")   # у тестах вимкнено — так і лишилось
        self.assertEqual(got["night_start"], "22:00")
        self.assertEqual(got["heartbeat_seconds"], "20")
        self.assertEqual(got["company_name"], "Моя кава")  # правильні значення збережено
        self.assertEqual(got["auto_close_minutes"], "90")

    def test_garbage_in_db_falls_back_to_defaults(self):
        for k, v in (("wifi_grace_minutes", "2 хв"), ("heartbeat_seconds", "abc"), ("weekend_multiplier", "nan"),
                     ("max_shift_hours", "0"), ("night_start", "xx"), ("lunch_rate_pct", "inf")):
            self.sql("update settings set value = ? where key = ?", v, k)
        self.login("ivan")
        r = self.client.post("/api/clock/start", json={}, headers={"X-Forwarded-For": WORK_IP})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["heartbeat_seconds"], 20)
        self.assertEqual(self.clock("break_start", kind="lunch")["shift"]["status"], "paused")
        with core.connect() as c:
            core.sweep(c)  # не падає
            self.assertEqual(core.pay_rules(c)["weekend"], 2.0)
        self.assertEqual(self.sql("select status from shifts")[0][0], "paused")  # max_shift_hours=0 не закрив зміну

    # --- 🟠 2. Сміття у формах не дає помилки 500 ----------------------------------------------
    FIELDS = ("user_id", "day", "starts_on", "ends_on", "type", "hourly_rate", "location_id", "hire_date", "started_at", "ended_at",
              "amount", "unit", "kind", "reason", "week", "starts_at", "ends_at", "cidr", "network_id", "rid", "month", "phone",
              "password", "password2", "full_name", "rate", "scope", "role", "comment", "proposed_start", "proposed_end", "shift_id",
              "schedule_id", "swap_with_id", "back", "cancel_id", "delete_id", "label", "name", "wifi_ssid", "wifi_password",
              "telegram", "whatsapp", "current", "new", "status", "job_title", "department", "note", "address")
    ACTIONS = ("location", "toggle", "network", "bind_current", "wifi", "delete_network", "copy_week", "approve", "reject",
               "grant", "move", "cancel", "open", "close")
    TYPES = ("time_edit", "shift_add", "shift_swap", "day_off", "vacation", "sick_leave")
    GARBAGE = ("zz", "", "9999-99-99", "0001-01-01", "nan", "1e309", "-1", "2026-13", "99999999999999999999")

    def test_garbage_input_never_gives_500(self):
        core.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.addCleanup(core.app.config.pop, "PROPAGATE_EXCEPTIONS", None)
        ivan_id = self.sql("select id from users where username = 'ivan'")[0][0]
        ivan = self.login("ivan")
        ivan.post("/me/request", data={"type": "vacation", "starts_on": "2026-12-01", "ends_on": "2026-12-03"})
        ivan.post("/me/request", data={"type": "day_off"})
        self.clock("start", client=ivan)
        self.clock("stop", client=ivan)
        # «Зіпсовані» дані зі старої бази теж не мають валити сторінки
        self.sql("insert into requests(user_id, type, status, starts_on, ends_on, created_at) values (?, 'sick_leave', 'pending', 'zz', 'zz', 1)", ivan_id)
        self.sql("update users set hire_date = '01.05.2025' where id = ?", ivan_id)
        rid = self.sql("select max(id) from requests")[0][0]
        sid = self.sql("select id from shifts")[0][0]
        admin = self.login("admin", "admin123", client=core.app.test_client())
        skip = {"/admin/backup_mirror", "/logout", "/login", "/register", "/password", "/admin/employee/<int:uid>/delete"}
        bad = []
        for rule in core.app.url_map.iter_rules():
            if "POST" not in rule.methods or rule.rule in skip:
                continue
            url = rule.rule
            for arg, val in (("<int:uid>", ivan_id), ("<int:rid>", rid), ("<int:aid>", 1), ("<int:did>", 1), ("<action>", "cancel"),
                             ("<decision>", "approve"), ("<int:uid2>", ivan_id)):
                url = url.replace(arg, str(val))
            for v in self.GARBAGE:
                base = {f: v for f in self.FIELDS}
                tries = [(c, kw) for c in (admin, ivan) for kw in ({"data": base}, {"json": base})]
                if v in ("zz", "9999-99-99", "nan", "99999999999999999999"):  # дії й типи запитів — з найзлішим сміттям
                    tries += [(admin, {"data": dict(base, action=a)}) for a in self.ACTIONS]
                    tries += [(ivan, {"data": dict(base, type=t)}) for t in self.TYPES]
                for c, kw in tries:
                    r = c.post(url, headers={"X-Forwarded-For": WORK_IP}, **kw)
                    if r.status_code >= 500:
                        data = kw.get("data") or kw.get("json")
                        bad.append((url, v, data.get("action"), data.get("type"), "json" in kw))
        for url in ("/me?tab=schedule&week=0001-01-01", "/me?tab=timesheet&month=2026-13", "/admin?tab=payroll&month=0001-01",
                    "/admin?tab=schedule&week=zz&view=month&month=9999-99", "/admin/payroll.csv?month=2026-13",
                    f"/admin/employee/{ivan_id}?month=zz", f"/admin/employee/{ivan_id}/timesheet.csv?month=-1", "/admin?tab=requests",
                    "/me?tab=schedule&view=month&month=0000-00", "/admin?tab=live", "/admin?tab=employees"):
            for c in (admin, ivan):
                if c.get(url).status_code >= 500:
                    bad.append((url, "GET"))
        self.assertEqual(sorted(set(bad))[:40], [], f"{len(bad)} запитів дали 500")

    # --- 🟠 3. Фонові потоки під наглядом ------------------------------------------------------
    def test_dead_worker_is_visible_in_health_and_admin(self):
        import threading
        import time
        self.addCleanup(core.WORKERS.clear)
        self.assertEqual(self.client.get("/health").status_code, 200)
        fail = threading.Event()
        fail.set()

        def step():
            if fail.is_set():
                raise RuntimeError("диск переповнено")
        core.log.disabled = True  # навмисні помилки не засмічують вивід тестів
        self.addCleanup(setattr, core.log, "disabled", False)
        self.addCleanup(core.run_worker("sweep", 0.05, step).set)
        time.sleep(0.2)
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 503)
        self.assertIn("Контроль Wi-Fi: RuntimeError: диск переповнено", r.get_json()["error"])
        admin = self.login("admin", "admin123")
        self.assertIn("Сервер потребує уваги", admin.get("/admin").get_data(as_text=True))
        ivan = self.login("ivan", client=core.app.test_client())
        self.assertNotIn("Сервер потребує уваги", ivan.get("/me").get_data(as_text=True))  # працівникам не показуємо
        fail.clear()
        time.sleep(0.2)
        self.assertEqual(self.client.get("/health").status_code, 200)
        # потік, що замовк (завис), теж помітно
        core.WORKERS["sweep"].update(ok_at=time.time() - 3600, started=time.time() - 3600, every=15)
        fail.set()
        self.assertIn("не працює", core.workers_problem() or "")

    # --- 🟠 4. Бекапи -----------------------------------------------------------------------------
    def test_manual_downloads_do_not_push_out_daily_backups(self):
        core.backup_db()
        daily = core.list_backups()
        admin = self.login("admin", "admin123")
        orig = core.BACKUP_KEEP
        core.BACKUP_KEEP = 1  # навіть з найменшою чергою щоденна копія лишається
        self.addCleanup(setattr, core, "BACKUP_KEEP", orig)
        for _ in range(5):
            r = admin.get("/admin/backup.zip")
            self.assertEqual(r.status_code, 200)
            r.close()
        self.assertEqual([b["name"] for b in core.list_backups()], [b["name"] for b in daily])
        self.assertLessEqual(len(core.os.listdir(core.MANUAL_BACKUP_DIR)), core.MANUAL_KEEP)

    def test_restore_skips_broken_newest_backup(self):
        import time
        core.backup_db()
        good = core.list_backups()[0]["name"]
        time.sleep(1.1)
        bad = core.backup_db()
        with open(bad, "wb") as f:
            f.write(b"not a zip at all")
        with open(core.DB_PATH, "r+b") as f:
            f.write(b"broken" * 200)
        msg = core.check_db()
        self.assertIn(good, msg)
        self.assertIn("пропущено", msg)
        self.assertTrue(core._db_ok(core.DB_PATH))
        core.RECOVERED["from_backup"] = False

    # --- 🟠 5. Особові папки без втрат ------------------------------------------------------------
    def test_folder_export_import_is_lossless(self):
        ivan = self.login("ivan")
        ua = {"User-Agent": "CrewHubApp/1.9 CrewHubDevice/loss0001", "X-Forwarded-For": WORK_IP}
        ivan.post("/api/clock/start", json={}, headers=ua)
        ivan.post("/api/clock/heartbeat", json={"unrestricted": True}, headers=ua)
        ivan.post("/api/clock/left", json={"away_since": core.now() - 120}, headers=dict(ua, **{"X-Forwarded-For": HOME_IP}))
        self.clock("stop", client=ivan)
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        ivan.post("/me/request", data={"type": "day_off"})
        mod = self.login("moder", "moder123", client=core.app.test_client())
        rid = self.sql("select id from requests where type = 'day_off'")[0][0]
        mod.post("/team/dayoff", data={"rid": rid, "action": "approve"})
        mod.post("/admin/adjustment", data={"user_id": uid, "amount": "300", "reason": "премія", "kind": "bonus"})
        q = {"shifts": "select bg_ok, status, close_reason from shifts where user_id = ?",
             "breaks": "select b.kind, b.reason, b.started_at, b.ended_at from breaks b join shifts s on s.id = b.shift_id where s.user_id = ?",
             "requests": "select type, status, grant_until, reviewer_id from requests where user_id = ?",
             "adjustments": "select amount, reason, created_by from adjustments where user_id = ?"}
        before = {k: [tuple(r) for r in self.sql(v, uid)] for k, v in q.items()}
        self.assertEqual(before["breaks"][0][1], "left")
        self.assertEqual(before["shifts"][0][0], 1)
        self.sync()
        with core.connect() as c:
            for t in ("shifts", "requests", "adjustments"):
                c.execute(f"delete from {t} where user_id = ?", (uid,))
            profiles.import_data(c, uid, self.folder("ivan"))
            c.commit()
        after = {k: [tuple(r) for r in self.sql(v, uid)] for k, v in q.items()}
        self.assertEqual(after, before)

    def test_rebuild_keeps_backup_data_newer_than_folder(self):
        self.login("ivan")
        self.sync()  # у папці ще немає змін
        self.clock("start")
        self.clock("stop")
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            profiles.rebuild_from_folders(c, since=core.now() + 60)  # копія новіша за папку
            c.commit()
        self.assertEqual(self.sql("select count(*) from shifts where user_id = ?", uid)[0][0], 1)

    # --- 🟠 6. Часовий пояс і перехід на літній/зимовий час -----------------------------------------
    def test_default_slot_keeps_wall_clock_on_dst_days(self):
        from datetime import date, datetime
        with core.connect() as c:
            for d in (date(2026, 10, 25), date(2027, 3, 28)):  # перехід на зимовий і на літній час у Києві
                start, end, _ = core.default_slot(c, 1, d)
                self.assertEqual((datetime.fromtimestamp(start).hour, datetime.fromtimestamp(end).hour), (9, 18), d)

    def test_copy_week_across_dst_keeps_hours(self):
        from datetime import datetime
        admin = self.login("admin", "admin123")
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        start = int(datetime(2026, 10, 19, 9, 0).timestamp())
        self.sql("insert into schedule(user_id, location_id, starts_at, ends_at) values (?, 1, ?, ?)", uid, start, start + 9 * 3600)
        admin.post("/admin/schedule", data={"action": "copy_week", "week": "2026-10-26"})
        copied = self.sql("select starts_at from schedule where user_id = ? and starts_at > ?", uid, start)[0][0]
        self.assertEqual(datetime.fromtimestamp(copied), datetime(2026, 10, 26, 9, 0))

    def test_utc_server_uses_kyiv_time(self):
        if core.os.environ.get("CREWHUB_TZ") is None and core.os.path.exists("/usr/share/zoneinfo/Europe/Kyiv"):
            self.assertIn(core.SERVER_TZ, ("Europe/Kyiv", "EET", "EEST"))

    # --- 🟠 8. Одна відкрита зміна на людину — правило бази -----------------------------------------
    def test_database_allows_one_open_shift_per_person(self):
        import sqlite3
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        t = core.now()
        ins = "insert into shifts(user_id, location_id, started_at, hourly_rate, last_seen_at) values (?, 1, ?, 100, ?)"
        self.sql(ins, uid, t - 7200, t - 3600)
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql(ins, uid, t - 600, t)
        # стара база з дублями: при запуску старший закривається, сервер стартує
        self.sql("drop index shifts_one_open")
        self.sql(ins, uid, t - 600, t)
        core.init_db()
        rows = self.sql("select status, close_reason from shifts where user_id = ? order by id", uid)
        self.assertEqual([tuple(r) for r in rows], [("closed", "duplicate"), ("active", None)])


if __name__ == "__main__":
    unittest.main()
