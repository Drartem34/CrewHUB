"""Сценарії з життя: кілька телефонів в одній Wi-Fi точки, повний робочий день, усі сторінки для всіх ролей.
Ловить поломки на стиках функцій (нова функція тихо зламала стару), яких не видно в тестах окремих функцій.
Мережа несправжня: таблиця сусідів сервера (ARP) — це словник телефонів, що зараз «у Wi-Fi»."""
import re
import unittest
from datetime import datetime
from unittest import mock

from test_crewhub import CrewHubBase, core  # noqa: F401 — test_crewhub налаштовує оточення
import lan

NET = "192.168.1.0/24"
CHROME = "Mozilla/5.0 (Linux; Android 13; {model}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
IVAN, OLHA = "0671234567", "0937654321"


class Phone:
    """Телефон у Wi-Fi точки: свій IP і MAC, браузер (код у localStorage, відбиток) або застосунок (код у User-Agent)."""

    def __init__(self, net, n, model="SM-A515F", app=None, ua=None):
        self.net, self.ip, self.mac, self.fp = net, f"192.168.1.{n}", f"da:00:00:00:00:{n:02x}", f"{n:016x}"
        self.ua = ua or CHROME.format(model=model)
        if app:
            self.ua = f"Mozilla/5.0 (Linux; Android 14; {model}; wv) CrewHubApp/2.3 CrewHubDevice/{app}"
            self.fp = f"{n + 0x100:016x}"  # WebView малює інакше, ніж Chrome: інший відбиток
        self.key = f"web-{n:08x}"
        self.online = True
        net[self.ip] = self
        self.c = core.app.test_client()

    def req(self, method, url, headers=None, **kw):
        core._mac_cache["at"] = 0.0  # сервер перечитує таблицю сусідів (у житті — раз на хвилину)
        return getattr(self.c, method)(url, headers={"User-Agent": self.ua, **(headers or {})},
                                       environ_base={"REMOTE_ADDR": self.ip}, **kw)

    def login(self, user, pw="1234"):
        r = self.req("post", "/login", data={"username": user, "password": pw, "device_id": self.key, "fp": self.fp})
        assert r.status_code == 302 and "/login" not in r.headers.get("Location", ""), r.status_code
        return self

    def logout(self):
        page = self.req("get", "/me").get_data(as_text=True)
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]
        self.req("post", "/logout", data={"csrf": csrf}, headers={"Origin": "http://localhost"})
        self.c = core.app.test_client()

    def clear_browser(self):
        self.key = "web-" + format(int(self.key[4:], 16) + 0x1000, "08x")
        self.c = core.app.test_client()

    def clock(self, action="auto", **body):
        return self.req("post", f"/api/clock/{action}", json={"device_id": self.key, "fp": self.fp, **body}).get_json()


class ScenarioBase(CrewHubBase):
    DAY = datetime(2026, 10, 21)  # середа; робочий час 09:00–18:00

    def setUp(self):
        super().setUp()
        self.net = {}
        core._mac_cache.update(at=0.0, table={})
        core._early_seen.clear()
        core._presence.clear()
        table = lambda: {ip: p.mac for ip, p in self.net.items() if p.online}  # noqa: E731
        for name, fn in (("neighbours", table), ("present", lambda ips, wait=0: table())):
            patcher = mock.patch.object(lan, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        real_now = core.now
        self.addCleanup(setattr, core, "now", real_now)
        with core.connect() as c:
            c.execute("insert into networks(location_id, cidr, label) values (1, ?, 'Wi-Fi точки')", (NET,))
            c.execute("update settings set value = '1' where key in ('auto_start', 'device_lock')")
            c.commit()

    def at(self, hh, mm=0):
        return int(self.DAY.replace(hour=hh, minute=mm).timestamp())

    def tick(self, hh, mm=0):
        core.now = (lambda x: lambda: x)(self.at(hh, mm))

    def background(self):
        """Що робить сервер сам: фонова перевірка Wi-Fi і перевірка телефонів у мережі."""
        with core.connect() as c:
            core.sweep(c)
            c.commit()
        core.presence_step()

    def live_until(self, hh, mm=0, every=60, hooks=None):
        """Сервер працює від поточної миті до hh:mm: фон щохвилини, hooks — {(год, хв): дія} у свій час."""
        t, end = core.now(), self.at(hh, mm)
        while t < end:
            t = min(t + every, end)
            core.now = (lambda x: lambda: x)(t)
            for when, act in (hooks or {}).items():
                if self.at(*when) == t:
                    act()
            self.background()

    def events(self, username, event=None):
        return [tuple(r) for r in self.sql(
            "select c.event, c.details from conn_log c join users u on u.id = c.user_id where u.username = ?"
            + (" and c.event = ?" if event else "") + " order by c.id", *((username, event) if event else (username,)))]

    def shift(self, username):
        return self.sql("select s.* from shifts s join users u on u.id = s.user_id where u.username = ? order by s.id desc", username)[0]

    def worked(self, username):
        with core.connect() as c:
            s = c.execute("select s.* from shifts s join users u on u.id = s.user_id where u.username = ? order by s.id desc",
                          (username,)).fetchone()
            return core.shift_stats(c, s)["worked_seconds"]


class WhosePhoneTest(ScenarioBase):
    """«Зайшов не зі свого телефона в чужий акаунт» — у всіх життєвих варіантах."""

    def test_matrix(self):
        ivan_phone, olha_phone = Phone(self.net, 11), Phone(self.net, 12, model="SM-S911B")
        ivan_phone.login(IVAN)
        olha_phone.login(OLHA)
        with core.connect() as c:  # Ольга реєструвалась з цього телефона: він прив'язаний як її «свій»
            core.remember_device(c, c.execute("select id from users where username = 'olha'").fetchone()[0], olha_phone.key, olha_phone.ip)
            c.commit()
        self.assertEqual(self.sql("select count(*) from conn_log where allowed = 0")[0][0], 0)  # кожен зі свого — тихо

        # 1. Іван вийшов зі свого акаунта і зайшов в акаунт Ольги на своєму телефоні
        ivan_phone.logout()
        ivan_phone.login(OLHA)
        other = self.events("olha", "login_other_device")
        self.assertEqual(len(other), 1)
        self.assertIn("Іван Петренко", other[0][1])

        # 2. Такий самий телефон (та сама модель — той самий відбиток), але інший — це новий пристрій, не Іванів
        twin = Phone(self.net, 13)
        twin.fp = ivan_phone.fp
        twin.login(OLHA)
        self.assertEqual(len(self.events("olha", "login_new_device")), 1)
        self.assertEqual(len(self.events("olha", "login_other_device")), 1)

        # 3. Іван очистив браузер (новий код), але телефон той самий — MAC і відбиток видають
        ivan_phone.logout()
        ivan_phone.clear_browser()
        ivan_phone.login(OLHA)
        self.assertEqual(len(self.events("olha", "login_other_device")), 2)

        # 4. Сам Іван на своєму телефоні (і після очищення браузера) — тихо
        ivan_phone.logout()
        ivan_phone.login(IVAN)
        self.assertEqual([e for e in self.events("ivan") if e[0].startswith(("login_", "shift_other"))], [])

        # 5. Застосунок на тому самому телефоні після браузера — той самий телефон, тихо
        Phone(self.net, 11, app="ivanapp001").login(IVAN)
        self.assertEqual([e for e in self.events("ivan") if e[0].startswith(("login_", "shift_other"))], [])

        # 6. Комп'ютер, яким користувався Іван: Ольга зайшла з нього у свій акаунт — як у версії 17, підозріло
        pc = Phone(self.net, 50, ua=PC)
        pc.login(IVAN)
        pc.logout()
        pc.login(OLHA)
        self.assertEqual(len(self.events("olha", "login_other_device")), 3)
        self.assertEqual(len(self.events("olha", "login_new_device")), 1)

        # Керівники бачать: журнал «Лише підозрілі», «Безпека сьогодні», сповіщення
        admin = Phone(self.net, 60, ua=PC).login("admin", "admin123")
        logs = admin.req("get", "/admin?tab=logs&only=bad").get_data(as_text=True)
        self.assertIn("Вхід у чужий акаунт", logs)
        self.assertIn("чий телефон: Іван Петренко", logs)
        live = admin.req("get", "/admin?tab=live").get_data(as_text=True)
        self.assertIn("Вхід у чужий акаунт", live.split("Безпека сьогодні")[1])
        self.assertTrue(self.sql("select 1 from notifications n join users u on u.id = n.user_id"
                                 " where u.username = 'moder' and n.title = 'Підозріло: чужий телефон'"))

    def test_shift_from_colleagues_phone(self):
        self.tick(10)
        ivan_phone, olha_phone = Phone(self.net, 11), Phone(self.net, 12, model="SM-S911B")
        ivan_phone.login(IVAN)
        olha_phone.login(OLHA)
        self.assertTrue(olha_phone.clock("auto")["shift"])  # автостарт зі свого
        # Ольга пішла, а її зміну «тримає» Іванів телефон: увійшли в її акаунт на ньому
        olha_phone.online = False
        ivan_phone.logout()
        ivan_phone.login(OLHA)
        ivan_phone.clock("heartbeat")
        self.assertEqual(len(self.events("olha", "shift_other_device")), 1)
        # телефон Івана «своїм» Ользі так і не став
        self.assertEqual([r[0] for r in self.sql("select d.device_key from devices d join users u on u.id = d.user_id"
                                                 " where u.username = 'olha'")], [olha_phone.key])


class PhoneHandoverTest(ScenarioBase):
    """Телефон налаштовував керівник; телефон звільненого перейшов новій людині."""

    def test_manager_set_up_the_phone(self):
        phone = Phone(self.net, 31)
        phone.login("admin", "admin123")  # адмін поставив застосунок і перевірив вхід
        phone.logout()
        phone.login(IVAN)
        self.assertEqual(self.sql("select count(*) from conn_log where allowed = 0")[0][0], 0)
        with core.connect() as c:
            uid = c.execute("select id from users where username = 'ivan'").fetchone()[0]
            self.assertTrue(core.device_allowed(c, uid, phone.key, phone.ip))  # і стає «своїм» Івану

    def test_admin_phone_into_account_with_own_phone(self):
        # 17.6: у Івана вже є свій телефон, а в його акаунт зайшли з телефона адміна — це чужий акаунт
        Phone(self.net, 30).login(IVAN)
        phone = Phone(self.net, 31)
        phone.login("admin", "admin123")
        phone.logout()
        phone.login(IVAN)
        self.assertEqual(len(self.events("ivan", "login_other_device")), 1)
        self.assertIn("Ви в чужому акаунті", phone.req("get", "/me").get_data(as_text=True))  # плашка
        # Адмін у картці Івана: «Пристрої, з яких заходив» → «Прив'язати» — телефон стає Івановим
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        admin = Phone(self.net, 60, ua=PC).login("admin", "admin123")
        page = admin.req("get", f"/admin/employee/{uid}").get_data(as_text=True)
        self.assertIn("Пристрої, з яких заходив", page)
        hid = self.sql("select id from device_history where user_id = ? and device_key = ?", uid, phone.key)[0][0]
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]
        r = admin.req("post", f"/admin/employee/{uid}/history/{hid}/bind", data={"csrf": csrf}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 302)
        phone.logout()
        phone.login(IVAN)
        self.assertEqual(len(self.events("ivan", "login_other_device")), 1)  # більше не позначає
        self.assertNotIn("Ви в чужому акаунті", phone.req("get", "/me").get_data(as_text=True))
        with core.connect() as c:
            self.assertTrue(core.device_allowed(c, uid, phone.key, phone.ip))

    def test_phone_of_dismissed_or_unbound(self):
        phone = Phone(self.net, 32)
        phone.login(IVAN)
        with core.connect() as c:
            uid = c.execute("select id from users where username = 'ivan'").fetchone()[0]
            core.remember_device(c, uid, phone.key, phone.ip)
            core.note_device(c, uid, phone.key, phone.fp, phone.mac)
            did = c.execute("select id from devices where user_id = ?", (uid,)).fetchone()[0]
            c.commit()
        phone.logout()
        admin = Phone(self.net, 60, ua=PC).login("admin", "admin123")
        page = admin.req("get", f"/admin/employee/{uid}").get_data(as_text=True)
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]
        r = admin.req("post", f"/admin/employee/{uid}/device/{did}/delete", data={"csrf": csrf}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 302)
        phone.login(OLHA)  # Іван віддав телефон Ользі — вона не «на чужому телефоні»
        self.assertEqual(self.events("olha", "login_other_device"), [])


class WorkdayTest(ScenarioBase):
    """Повний робочий день: застосунок, браузер без застосунку, вихід з Wi-Fi, розряджений телефон."""

    def test_day(self):
        ivan = Phone(self.net, 11, app="ivanphone01")  # застосунок
        olha = Phone(self.net, 12, model="SM-S911B")    # лише браузер, сторінку закрила
        self.tick(8, 40)
        ivan.login(IVAN)
        self.assertEqual(ivan.clock("auto", battery=80)["waiting_until"], self.at(9))  # прийшов раніше
        olha.login(OLHA)
        olha.clock("check")

        def app_ping(battery):
            return lambda: ivan.clock("auto", battery=battery, charging=False)

        hooks = {(9, 0): app_ping(79)}
        for h in range(9, 14):
            for m in (10, 20, 30, 40, 50):
                hooks[(h, m)] = app_ping(max(3, 79 - (h - 9) * 15 - m // 10 * 2))
        def died():
            ivan.clock("auto", battery=3, charging=False)  # останній пінг: 3 %, далі телефон сів —
            ivan.online = False                             # і в мережі його вже не видно
        hooks[(14, 0)] = died
        hooks[(12, 0)] = lambda: setattr(olha, "online", False)  # Ольга пішла з точки
        self.live_until(18, 30, hooks=hooks)

        iv = self.shift("ivan")
        self.assertEqual((iv["started_at"], iv["status"], iv["ended_at"]), (self.at(9), "closed", self.at(18)))
        self.assertTrue(self.events("ivan", "battery_dead"))
        self.assertEqual(self.worked("ivan"), 9 * 3600)

        ol = self.shift("olha")
        self.assertEqual(ol["note"], "Автостарт по Wi-Fi")  # почалась сама: телефон у Wi-Fi, сторінка закрита
        self.assertEqual(ol["started_at"], self.at(9))
        self.assertEqual(ol["status"], "closed")
        self.assertLessEqual(abs(ol["ended_at"] - self.at(12)), 60)  # закрито часом, коли телефон зник з мережі
        self.assertEqual(self.sql("select count(*) from conn_log where allowed = 0 and event like '%device%'")[0][0], 0)


class OutageDayTest(ScenarioBase):
    """Зникло світло 11:00–12:30: сервер і роутер вимкнулись, потім усе піднялось само."""

    def test_outage(self):
        ivan = Phone(self.net, 11, app="ivanphone01")
        olha = Phone(self.net, 12, model="SM-S911B")
        self.tick(8, 55)
        ivan.login(IVAN)
        olha.login(OLHA)
        hooks = {(9, 0): lambda: ivan.clock("auto")}
        for h, m in ((9, 30), (10, 0), (10, 30), (10, 55)):
            hooks[(h, m)] = (lambda: ivan.clock("auto"))
        self.live_until(11, 0, hooks=hooks)
        self.assertEqual({self.shift("ivan")["status"], self.shift("olha")["status"]}, {"active"})

        for p in (ivan, olha):  # світла немає: сервер не працює, Wi-Fi теж
            p.online = False
        self.tick(12, 30)
        for p in (ivan, olha):
            p.online = True
        self.background()  # сервер запустився
        self.assertEqual(self.sql("select ended_at - started_at from outages")[0][0], 90 * 60)
        self.tick(12, 33)  # застосунок: «Wi-Fi зник об 11:00» — це роутер згас, не людина пішла
        ivan.clock("auto", away_since=self.at(11))
        self.live_until(15)
        for name in ("ivan", "olha"):
            self.assertEqual(self.shift(name)["status"], "active", name)
            self.assertEqual(self.worked(name), 6 * 3600, name)  # 09:00–15:00 без пауз
            self.assertTrue(self.events(name, "outage_credit"), name)


class AllPagesTest(ScenarioBase):
    """Кожна сторінка для кожної ролі відкривається (жодної 500) на живих даних з усіма видами подій."""

    def test_pages(self):
        with core.connect() as c:
            core.seed_demo_history(c)
            c.execute("update users set role = 'teamlead' where username = 'olha'")
            c.commit()
        # Події всіх видів: підозрілий вхід, розряджений телефон, відключення світла, свій телефон
        WhosePhoneTest.test_matrix(self)
        self.tick(10)
        ivan = Phone(self.net, 21, app="ivanphone09").login(IVAN)
        ivan.clock("auto", battery=4)
        self.tick(13)
        self.background()
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        pages = {
            "ivan": (IVAN, "1234", ["/me?tab=" + t for t in ("clock", "timesheet", "schedule", "requests", "team", "profile", "app")]),
            "olha": (OLHA, "1234", ["/me?tab=dayoffs", "/me?tab=team", "/me?tab=schedule&view=month"]),
            "moder": ("moder", "moder123", ["/admin?tab=" + t for t in ("live", "employees", "requests", "schedule", "zones", "logs", "audit", "payroll")]
                      + ["/admin?tab=logs&only=bad", "/admin?tab=schedule&view=month", f"/admin/employee/{uid}", "/api/live"]),
            "admin": ("admin", "admin123", ["/admin?tab=" + t for t in ("live", "employees", "requests", "schedule", "zones", "logs", "audit", "payroll", "settings")]
                      + [f"/admin/employee/{uid}", "/admin/payroll.csv", f"/admin/employee/{uid}/timesheet.csv", "/api/live"]),
        }
        for n, (who, (login, pw, urls)) in enumerate(pages.items()):
            p = Phone(self.net, 70 + n, ua=PC if who in ("moder", "admin") else None).login(login, pw)
            for url in urls:
                with self.subTest(who=who, url=url):
                    r = p.req("get", url)
                    self.assertEqual(r.status_code, 200)
                    self.assertNotIn("Traceback", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
