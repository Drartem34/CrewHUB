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
        self.key_app = f"app-{app}" if app else None  # код, під яким сервер знає застосунок (з User-Agent)
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


class RegularAccountSwapTest(ScenarioBase):
    """Звичайний акаунт А на телефоні P → вихід → вхід у звичайний акаунт Б на тому самому телефоні → зміна.
    Адмінка має це показати: червона зміна в «Огляді» й табелі, «Підключення», сповіщення; людині — плашка."""

    def make_a(self, p, how):
        if how == "register":  # А сам зареєструвався з телефона P, адмін підтвердив
            p.req("post", "/register", data={"full_name": "Андрій Звичайний", "phone": "0991112233", "password": "123456",
                                             "password2": "123456", "device_id": p.key, "fp": p.fp})
            with core.connect() as c:
                c.execute("update users set status = 'active' where phone = '+380991112233'")
                c.commit()
        else:  # А створив адмін, А лише заходив з P
            with core.connect() as c:
                c.execute("insert into users(username, password_hash, full_name, role, hourly_rate, phone, status, location_id)"
                          " values ('andriy', ?, 'Андрій Звичайний', 'employee', 100, '+380991112233', 'active', 1)",
                          (core.hash_password("123456"),))
                c.commit()
        p.login("0991112233", "123456")
        if how == "worked":  # А ще й відпрацював з P зміну (телефон прив'язався автостартом)
            self.tick(9, 5)
            p.clock("auto")
            p.clock("stop")
        p.logout()

    def check(self, p):
        self.tick(10)
        p.login(OLHA)
        self.assertIn("Ви в чужому акаунті", p.req("get", "/me").get_data(as_text=True))
        p.clock("auto")
        self.assertTrue(p.clock("start")["ok"])
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        row = [r for r in admin.req("get", "/api/live").get_json()["rows"] if r["name"] == "Ольга Коваль"][0]
        self.assertEqual(row["device_flag"], "other:Андрій Звичайний")
        self.assertTrue(row["device"])
        self.assertIn('id="liveAlarm"', admin.req("get", "/admin?tab=live").get_data(as_text=True))
        self.assertIn("Вхід у чужий акаунт", admin.req("get", "/admin?tab=logs").get_data(as_text=True))
        uid = self.sql("select id from users where username = 'olha'")[0][0]
        card = admin.req("get", f"/admin/employee/{uid}").get_data(as_text=True)
        self.assertIn("чужий акаунт — телефон Андрій Звичайний", card)
        self.assertIn("розпочата з:", card)
        self.assertIn("Підозріло: чужий телефон",
                      [r[0] for r in self.sql("select n.title from notifications n join users u on u.id = n.user_id where u.username = 'admin'")])

    def test_browser_registered(self):
        p = Phone(self.net, 21)
        self.tick(8)
        self.make_a(p, "register")
        self.check(p)

    def test_registered_in_browser_then_app(self):
        b, p = Phone(self.net, 22), Phone(self.net, 22, app="phone22app")  # той самий телефон: браузер і застосунок
        self.tick(8)
        self.make_a(b, "register")
        p.login("0991112233", "123456")
        p.logout()
        self.check(p)

    def test_app_account_made_by_admin(self):
        p = Phone(self.net, 23, app="phone23app")
        self.tick(8)
        self.make_a(p, "admin")
        self.check(p)

    def test_app_after_a_worked_a_shift(self):
        p = Phone(self.net, 24, app="phone24app")
        self.tick(8)
        self.make_a(p, "worked")
        self.check(p)

    def test_b_has_own_phone(self):
        Phone(self.net, 26, model="SM-S911B").login(OLHA)  # в Ольги свій телефон
        p = Phone(self.net, 25)
        self.tick(8)
        self.make_a(p, "admin")
        self.check(p)

    def test_foreign_phone_never_becomes_hers(self):
        # Іван лише заходив зі свого телефона (не прив'язаний). Ольга на ньому: автостарт не починає зміну і не
        # прив'язує телефон Івана до неї (раніше прив'язував — і в картці Івана його телефон ставав «чужим: Ольга»)
        p = Phone(self.net, 27)
        self.tick(8)
        p.login(IVAN)
        p.logout()
        self.tick(10)
        p.login(OLHA)
        p.clock("auto")
        self.assertEqual(self.sql("select count(*) from shifts")[0][0], 0)
        self.assertEqual(self.sql("select count(*) from devices where device_key = ?", p.key)[0][0], 0)
        self.assertTrue(p.clock("start")["ok"])  # вручну — можна, з позначкою
        self.assertEqual(self.shift("olha")["device_flag"], "other:Іван Петренко")
        # новачок реєструється з телефона Івана (Іван допомагав) — телефон не стає телефоном новачка
        q = Phone(self.net, 28)
        q.login(IVAN)
        q.logout()
        q.req("post", "/register", data={"full_name": "Новий Новачок", "phone": "0991112244", "password": "123456",
                                         "password2": "123456", "device_id": q.key, "fp": q.fp})
        self.assertEqual(self.sql("select count(*) from devices where device_key = ?", q.key)[0][0], 0)


class DeviceRewriteTest(ScenarioBase):
    """«Перезаписати код пристрою»: у людини новий телефон — адмін ставить його код замість старого."""

    def setUp(self):
        super().setUp()
        self.old, self.new = Phone(self.net, 41, app="ivanold01"), Phone(self.net, 42, app="ivannew01", model="SM-S911B")
        self.tick(8)
        self.old.login(IVAN)
        self.uid = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            core.remember_device(c, self.uid, self.old.key_app, self.old.ip)
            c.commit()
        self.admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")

    def rewrite(self, **form):
        page = self.admin.req("get", f"/admin/employee/{self.uid}").get_data(as_text=True)
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]
        self.admin.req("post", f"/admin/employee/{self.uid}/device/rewrite", data={"csrf": csrf, **form},
                       headers={"Origin": "http://localhost"})
        return self.admin.req("get", f"/admin/employee/{self.uid}").get_data(as_text=True)

    def bound(self):
        return [r[0] for r in self.sql("select device_key from devices where user_id = ?", self.uid)]

    def test_pick_new_phone_from_history(self):
        self.new.login(IVAN)  # Іван зайшов з нового телефона
        page = self.admin.req("get", f"/admin/employee/{self.uid}").get_data(as_text=True)
        self.assertIn("Перезаписати код пристрою", page)
        self.assertIn(self.new.key_app, page)
        page = self.rewrite(pick=self.new.key_app)
        self.assertIn("Код пристрою перезаписано", page)
        self.assertEqual(self.bound(), [self.new.key_app])
        self.assertEqual(self.sql("select count(*) from device_history where user_id = ? and device_key = ?",
                                  self.uid, self.old.key_app)[0][0], 0)  # старий телефон більше не Іванів
        with core.connect() as c:
            self.assertTrue(core.device_allowed(c, self.uid, self.new.key_app, self.new.ip))
            self.assertFalse(core.device_allowed(c, self.uid, self.old.key_app, self.old.ip))

    def test_typed_code_and_errors(self):
        self.assertIn("Вкажіть код пристрою", self.rewrite(code="абв"))
        self.assertEqual(self.bound(), [self.old.key_app])
        olha = Phone(self.net, 43, app="olhaphone1")
        olha.login(OLHA)
        with core.connect() as c:
            core.remember_device(c, self.sql("select id from users where username = 'olha'")[0][0], olha.key_app, olha.ip)
            c.commit()
        self.assertIn("прив&#39;язаний до Ольга Коваль", self.rewrite(code=olha.key_app))
        self.assertEqual(self.bound(), [self.old.key_app])
        self.assertIn("Код пристрою перезаписано", self.rewrite(code="app-ivannew01"))  # код ще не бачений сервером
        self.assertEqual(self.bound(), ["app-ivannew01"])

    def test_person_sees_own_code(self):
        self.assertIn("Код цього пристрою", self.old.req("get", "/me?tab=app").get_data(as_text=True))


class DhcpNewIpTest(ScenarioBase):
    """Роутер видав телефону новий IP: сервер не стукає вічно в стару адресу, а шукає телефон за MAC у всій мережі
    точки — навіть коли загальний пошук пристроїв (lan_scan) вимкнено."""

    def setUp(self):
        super().setUp()
        core._presence_sweep["at"] = 0.0
        self.knocked = []

        def present(ips, wait=0):  # як у житті: у таблиці сусідів з'являється лише той, у кого постукали
            self.knocked.append(list(ips))
            return {ip: p.mac for ip, p in self.net.items() if p.online and ip in ips}
        patcher = mock.patch.object(lan, "present", present)
        patcher.start()
        self.addCleanup(patcher.stop)
        with core.connect() as c:
            c.execute("update settings set value = '0' where key = 'lan_scan'")
            c.commit()

    def test_new_ip_next_morning(self):
        self.tick(17)
        phone = Phone(self.net, 11)  # учора: телефон Івана мав .11
        phone.login(IVAN)
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            core.remember_device(c, uid, phone.key, phone.ip)
            core.note_device(c, uid, phone.key, phone.fp, phone.mac)
            c.commit()
        del self.net[phone.ip]  # уранці роутер дав йому .77 (.11 зайняв хтось інший)
        phone.ip = "192.168.1.77"
        self.net[phone.ip] = phone
        self.tick(9, 1)
        core.presence_step()
        self.assertEqual(self.knocked[0], ["192.168.1.11"])  # спершу — стара адреса
        self.assertIn("192.168.1.77", self.knocked[1])      # потім уся мережа точки
        s = self.shift("ivan")
        self.assertEqual((s["status"], s["start_ip"], s["mac"]), ("active", "192.168.1.77", phone.mac))

    def test_whole_network_not_more_often_than_every_90s(self):
        self.tick(9, 1)
        phone = Phone(self.net, 12)
        phone.login(OLHA)
        uid = self.sql("select id from users where username = 'olha'")[0][0]
        with core.connect() as c:
            core.remember_device(c, uid, phone.key, phone.ip)
            core.note_device(c, uid, phone.key, phone.fp, phone.mac)
            c.commit()
        phone.online = False  # Ольги ще немає
        core.presence_step()
        core.presence_step()
        self.assertEqual(sum(len(k) > 1 for k in self.knocked), 1)  # уся мережа — один раз


class BrowserPhoneSleepTest(ScenarioBase):
    """Браузер на телефоні без застосунку (iPhone): у кишені сторінка засинає. Зміна не стає на паузу від
    короткої тиші (browser_phone_grace_minutes), але пішов насправді — пауза з останнього пінгу."""
    IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"

    def setUp(self):
        super().setUp()
        with core.connect() as c:
            c.execute("update settings set value = '0' where key = 'lan_presence'")  # сонний iPhone не відповідає на ARP
            c.commit()

    def until(self, mm):
        self.live_until(10, mm)  # сервер працює: фонова перевірка щохвилини
        return self.shift("ivan")

    def test_iphone_in_pocket(self):
        self.tick(10)
        phone = Phone(self.net, 15, ua=self.IPHONE)
        phone.login(IVAN)
        self.assertTrue(phone.clock("start")["ok"])
        self.assertEqual(self.until(8)["status"], "active")   # 8 хв тиші — не пауза
        phone.clock("heartbeat")                              # дістав телефон о 10:08
        self.assertEqual(self.until(17)["status"], "active")
        self.assertEqual(self.until(20)["status"], "paused")  # понад 10 хв тиші — пауза...
        b = self.sql("select started_at from breaks where kind = 'wifi_loss'")[0][0]
        self.assertEqual(b, self.at(10, 8))                  # ...з останнього пінгу: цей час не оплачується

    def test_computer_browser_unchanged(self):
        self.tick(10)
        pc = Phone(self.net, 16, ua=PC)
        pc.login(IVAN)
        self.assertTrue(pc.clock("start")["ok"])
        self.assertEqual(self.until(4)["status"], "paused")  # як і раніше: браузер на комп'ютері — 2 хв


class SoftCheckTest(ScenarioBase):
    """М'який облік (soft_check_minutes = 15): телефон зміни перевіряють у випадковий момент раз на 15 хв, а не
    щопівхвилини; коротка відлучка — не пауза; довга — пауза з останньої ознаки телефона."""

    def setUp(self):
        super().setUp()
        core._soft_checked.clear()
        with core.connect() as c:
            c.execute("insert or replace into settings(key, value) values ('soft_check_minutes', '15')")
            c.commit()
        self.knocks = []
        table = lambda: {ip: p.mac for ip, p in self.net.items() if p.online}  # noqa: E731

        def present(ips, wait=0):
            self.knocks.append(list(ips))
            return table()
        patcher = mock.patch.object(lan, "present", present)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tick(10)
        self.phone = Phone(self.net, 31)  # браузер на телефоні, сторінку потім закрили
        self.phone.login(OLHA)
        self.assertTrue(self.phone.clock("start")["ok"])
        self.phone.clock("heartbeat")  # з робочої мережі: сервер запам'ятав MAC телефона зміни

    def breaks(self):
        return self.sql("select started_at, ended_at from breaks where kind = 'wifi_loss'")

    def test_phone_in_wifi_two_hours_rare_checks(self):
        self.live_until(12)
        self.assertEqual(self.shift("olha")["status"], "active")
        self.assertEqual(self.breaks(), [])
        mine = sum(self.phone.ip in k for k in self.knocks)
        self.assertGreaterEqual(mine, 7)   # раз на 15 хв...
        self.assertLessEqual(mine, 9)      # ...а не щохвилини (було б ~120)

    def test_short_absence_is_not_a_pause(self):
        off, on = (lambda: setattr(self.phone, "online", False)), (lambda: setattr(self.phone, "online", True))
        self.live_until(11, hooks={(10, 20): off, (10, 40): on})
        self.assertEqual(self.shift("olha")["status"], "active")
        self.assertEqual(self.breaks(), [])

    def test_short_absence_any_moment(self):
        # відлучка до 15 хв — не пауза, хоч би як випали випадкові перевірки
        for mm in range(5, 60, 7):
            core._soft_checked.clear()
            core._soft_missed.clear()
            self.setUp()
            self.live_until(10, mm, hooks={})
            self.phone.online = False
            back = mm + 14
            self.live_until(10 + back // 60, back % 60)
            self.phone.online = True
            self.live_until(11, 30)
            self.assertEqual(self.breaks(), [], mm)

    def test_long_absence_ends_at_last_sign(self):
        self.live_until(12, hooks={(10, 20): lambda: setattr(self.phone, "online", False)})
        s = self.shift("olha")
        self.assertIn(s["status"], ("paused", "closed"))  # пауза довша за wifi_pause_close_minutes — закрито
        end = s["ended_at"] or self.breaks()[0][0]
        self.assertGreaterEqual(end, self.at(10))
        self.assertLessEqual(end, self.at(10, 20))  # з останньої ознаки — час без людини не оплачується

    def test_off_by_default(self):
        self.assertEqual(core.DEFAULT_SETTINGS["soft_check_minutes"], "0")


class DeviceSecurityTest(ScenarioBase):
    """17.11: блокування пристрою «по залізу» (MAC і код), підбір пароля, вхід у різні акаунти з одного пристрою."""

    def setUp(self):
        super().setUp()
        core._blocked["at"] = 0.0  # справжній час: «Безпека сьогодні» рахує за сьогоднішнім днем
        self.admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")

    def post_admin(self, url, **data):
        page = self.admin.req("get", "/admin?tab=logs").get_data(as_text=True)
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]
        return self.admin.req("post", url, data={"csrf": csrf, **data}, headers={"Origin": "http://localhost"})

    def notes(self, title):
        return self.sql("select count(*) from notifications n join users u on u.id = n.user_id where u.username = 'admin' and n.title = ?", title)[0][0]

    def test_block_by_hardware_and_unblock(self):
        phone = Phone(self.net, 50)
        phone.login(IVAN)
        self.assertEqual(phone.req("get", "/me").status_code, 200)
        r = self.post_admin("/admin/devices/block", mac=phone.mac, label="Телефон Івана", back="/admin?tab=logs")
        self.assertEqual(r.status_code, 302)
        r = phone.req("get", "/me")  # був в акаунті — вилітає на наступній сторінці
        self.assertIn("/login", r.headers.get("Location", ""))
        phone.c = core.app.test_client()
        phone.clear_browser()  # новий код браузера не рятує: MAC той самий
        r = phone.req("post", "/login", data={"username": OLHA, "password": "1234", "device_id": phone.key, "fp": phone.fp})
        self.assertEqual(r.status_code, 403)  # і в інший акаунт теж ні
        self.assertIn("заблоковано адміністратором", r.get_data(as_text=True))
        self.assertTrue(self.events("ivan", "blocked_device") or self.sql("select 1 from conn_log where event = 'blocked_device'"))
        self.assertEqual(self.notes("Заблокований пристрій пробує зайти"), 1)
        logs = self.admin.req("get", "/admin?tab=logs").get_data(as_text=True)
        self.assertIn("Заблоковані пристрої · 1", logs)
        bid = self.sql("select id from blocked_devices")[0][0]
        self.post_admin(f"/admin/devices/unblock/{bid}")
        phone.login(OLHA)  # розблоковано — входить

    def test_block_app_by_code_and_clock_refused(self):
        app = Phone(self.net, 51, app="appcode51")
        app.login(IVAN)
        self.post_admin("/admin/devices/block", device_key="app-appcode51", label="Застосунок")
        r = app.req("post", "/api/clock/start", json={"device_id": app.key})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.sql("select count(*) from shifts")[0][0], 0)

    def test_cannot_block_own_device(self):
        self.post_admin("/admin/devices/block", mac=self.admin.mac, label="мій ПК")
        self.assertEqual(self.sql("select count(*) from blocked_devices")[0][0], 0)
        self.assertIn("не можна заблокувати", self.admin.req("get", "/admin?tab=logs").get_data(as_text=True))

    def test_password_guessing(self):
        bad = Phone(self.net, 52, model="SM-G991B")
        for who in (IVAN, OLHA, IVAN, "0500000001", OLHA):
            r = bad.req("post", "/login", data={"username": who, "password": "wrong", "device_id": bad.key, "fp": bad.fp})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sql("select count(*) from conn_log where event = 'login_failed'")[0][0], 5)
        row = self.sql("select details, device from conn_log where event = 'brute_force'")
        self.assertEqual(len(row), 1)
        self.assertIn("+380 67 123 45 67", row[0][0])
        self.assertIn(bad.mac, row[0][0])
        self.assertEqual(self.notes("Підозріло: підбір пароля"), 1)
        live = self.admin.req("get", "/admin?tab=live").get_data(as_text=True)
        security = live.split("<h3>Безпека сьогодні</h3>")[1]
        self.assertIn("Підбір пароля", security)
        # 17.14: кнопки «Заблокувати» біля подій немає — блокують в одному місці, «Підключення» → «Заблоковані пристрої»,
        # і цей пристрій там першим у списку
        self.assertNotIn("/admin/devices/block", security.split('id="lan"')[0])
        logs = self.admin.req("get", "/admin?tab=logs").get_data(as_text=True)
        self.assertIn(f"{bad.key}|{bad.mac}||⚠ Підбір пароля", logs)

    def test_many_accounts_from_one_device(self):
        with core.connect() as c:
            c.execute("update users set password_hash = ? where username = 'moder'", (core.hash_password("1234"),))
            c.commit()
        kiosk = Phone(self.net, 53)
        for who in (IVAN, OLHA, "0500000002"):
            kiosk.login(who)
            kiosk.logout()
        row = self.sql("select details from conn_log where event = 'account_hopping'")
        self.assertEqual(len(row), 1)
        self.assertIn("Іван Петренко", row[0][0])
        self.assertEqual(self.notes("Підозріло: вхід у різні акаунти"), 1)


class DhcpShuffleAfterOutageTest(ScenarioBase):
    """17.12: світло вибило, роутер роздав IP наново — IP телефона Івана дістався телефону Ольги, а таблиця сусідів
    сервера ще пам'ятає стару пару «IP → MAC Івана». Ольга не отримує MAC Івана і не стає «на чужому телефоні»."""

    def setUp(self):
        super().setUp()
        self.states = {}  # ip → (mac, стан), як `ip neigh` / Get-NetNeighbor
        patcher = mock.patch.object(lan, "neighbour_states", lambda: dict(self.states))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tick(9)
        self.ivan = Phone(self.net, 20)
        self.states[self.ivan.ip] = (self.ivan.mac, "REACHABLE")
        self.ivan.login(IVAN)
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            core.remember_device(c, uid, self.ivan.key, self.ivan.ip)
            core.note_device(c, uid, self.ivan.key, self.ivan.fp, self.ivan.mac)
            c.commit()

    def shuffle(self):
        """Після відключення: телефон Ольги отримав .20; таблиця сусідів ще має .20 → MAC Івана, але запис застарілий."""
        self.ivan.online = False
        del self.net[self.ivan.ip]
        self.olha = Phone(self.net, 21)
        del self.net[self.olha.ip]
        self.olha.ip = self.ivan.ip
        self.net[self.olha.ip] = self.ivan  # ARP-таблиця ще віддає MAC Івана
        self.ivan.online = True
        self.states[self.olha.ip] = (self.ivan.mac, "STALE")

    def test_stale_pair_is_not_used(self):
        self.shuffle()
        self.tick(10)
        self.olha.login(OLHA)
        self.assertEqual(self.events("olha", "login_other_device"), [])  # не «чужий телефон»
        self.assertNotIn(self.ivan.mac, [r[0] for r in self.sql(
            "select h.mac from device_history h join users u on u.id = h.user_id where u.username = 'olha'")])
        # система перепитала: тепер у таблиці правильний MAC Ольги
        self.net[self.olha.ip] = self.olha
        self.olha.online = True
        self.states[self.olha.ip] = (self.olha.mac, "REACHABLE")
        self.olha.clock("check")
        macs = [r[0] for r in self.sql("select h.mac from device_history h join users u on u.id = h.user_id where u.username = 'olha'")]
        self.assertIn(self.olha.mac, macs)
        self.assertEqual(core.mac_conflicts(core.connect()), [])

    def test_client_mac_rules(self):
        ip = "192.168.1.40"
        for neigh, state, want in ((self.ivan.mac, (self.ivan.mac, "REACHABLE"), self.ivan.mac),
                                   (self.ivan.mac, (self.ivan.mac, "STALE"), None),        # не підтверджений щойно
                                   (self.ivan.mac, (self.ivan.mac, "DELAY"), None),
                                   (self.ivan.mac, ("da:00:00:00:00:99", "REACHABLE"), None)):  # таблиці розходяться
            core._mac_cache.update(at=0.0, table={})
            self.states[ip] = state
            with mock.patch.object(lan, "neighbours", return_value={ip: neigh}), mock.patch.object(lan, "knock") as knock:
                self.assertEqual(core.client_mac(ip), want, state)
                self.assertEqual(knock.called, want is None)  # сумнівний запис — стукаємо, щоб система перепитала

    def test_conflict_banner_and_relearn(self):
        olha_id = self.sql("select id from users where username = 'olha'")[0][0]
        with core.connect() as c:  # стара біда: MAC Івана записався й Ользі
            core.remember_device(c, olha_id, "web-0000aaaa", "192.168.1.20")
            c.execute("update devices set mac = ? where user_id = ?", (self.ivan.mac, olha_id))
            c.execute("insert into lan_devices(mac, ip, name, model, vendor, user_id, first_seen, last_seen) values (?, ?, null, null, null, ?, 0, 0)",
                      (self.ivan.mac, self.ivan.ip, olha_id))
            c.commit()
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        live = admin.req("get", "/admin?tab=live").get_data(as_text=True)
        self.assertIn("прив'язаний до кількох людей", live)
        card = admin.req("get", f"/admin/employee/{olha_id}").get_data(as_text=True)
        self.assertIn("Перезаписати IP і MAC", card)
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', card)[1]
        admin.req("post", "/admin/devices/relearn", data={"csrf": csrf, "user_id": olha_id}, headers={"Origin": "http://localhost"})
        self.assertEqual(self.sql("select mac, last_ip from devices where user_id = ?", olha_id)[0][:], (None, None))
        self.assertEqual(self.sql("select count(*) from devices where mac is not null")[0][0], 1)  # Іванів лишився
        self.assertEqual(self.sql("select count(*) from devices where user_id = ?", olha_id)[0][0], 1)  # прив'язка лишилась
        self.assertNotIn("кількох людей", admin.req("get", "/admin?tab=live").get_data(as_text=True))
        admin.req("post", "/admin/devices/relearn", data={"csrf": csrf}, headers={"Origin": "http://localhost"})  # усі
        self.assertEqual(self.sql("select count(*) from devices where mac is not null")[0][0], 0)
        self.assertEqual(self.sql("select count(*) from lan_devices")[0][0], 0)

    def test_lan_list_by_mac_not_old_ip(self):
        olha_id = self.sql("select id from users where username = 'olha'")[0][0]
        with core.connect() as c:  # учора телефон Ольги мав .30 — сьогодні в нього інший IP, а .30 у телефона Івана
            core.remember_device(c, olha_id, "web-0000bbbb", "192.168.1.30")
            c.commit()
        with core.connect() as c:
            lan.record(c, {"192.168.1.30": self.ivan.mac}, {})
        self.assertEqual(self.sql("select u.username from lan_devices l join users u on u.id = l.user_id where l.mac = ?",
                                  self.ivan.mac)[0][0], "ivan")


class SharedMacTest(ScenarioBase):
    """17.13: за одним MAC кілька телефонів (другий роутер на точці — однакові IP і MAC; підсилювач Wi-Fi — однаковий MAC,
    різні IP). Сервер помічає це сам і розрізняє людей за кодом пристрою й відбитком, а MAC ігнорує."""

    def setUp(self):
        super().setUp()
        core._mac_seen.clear()
        core._shared.update(at=0.0, set=set())
        self.addCleanup(core._shared.update, at=0.0, set=set())
        self.tick(10)

    def behind(self, first, n, app=None):
        """Телефон за тим самим роутером, що й first: той самий IP і MAC, свій код і відбиток."""
        p = Phone(self.net, n, app=app)
        del self.net[p.ip]
        p.ip, p.mac = first.ip, first.mac
        return p

    def shared(self):
        return [r[0] for r in self.sql("select mac from shared_macs")]

    def test_two_phones_behind_a_router(self):
        ivan = Phone(self.net, 60)
        ivan.login(IVAN)
        olha = self.behind(ivan, 61)
        olha.login(OLHA)
        self.assertEqual(self.shared(), [ivan.mac])
        self.assertEqual(self.events("olha", "login_other_device"), [])  # Ольга не «на телефоні Івана»
        self.assertEqual(self.sql("select count(*) from device_history where mac = ?", ivan.mac)[0][0], 0)  # MAC нічий
        self.assertIn("Спільний MAC: кілька телефонів",
                      [r[0] for r in self.sql("select n.title from notifications n join users u on u.id = n.user_id where u.username = 'admin'")])
        # кожна людина — свій пристрій за кодом
        keys = dict(self.sql("select u.username, h.device_key from device_history h join users u on u.id = h.user_id"))
        self.assertEqual((keys["ivan"], keys["olha"]), (ivan.key, olha.key))
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        self.assertIn("Спільний MAC", admin.req("get", "/admin?tab=live").get_data(as_text=True))

    def test_repeater_same_mac_different_ips(self):
        a = Phone(self.net, 62, app="phonea0001")
        a.login(IVAN)
        b = Phone(self.net, 63, app="phoneb0001")
        b.mac = a.mac
        self.net[b.ip] = b
        b.login(OLHA)
        self.assertEqual(self.shared(), [a.mac])

    def test_one_phone_two_accounts_is_not_shared(self):
        phone = Phone(self.net, 64)
        phone.login(IVAN)
        phone.logout()
        phone.login(OLHA)  # той самий браузер — той самий код: це один телефон
        self.assertEqual(self.shared(), [])
        self.assertEqual(len(self.events("olha", "login_other_device")), 1)  # і далі «чужий акаунт»

    def test_app_and_browser_on_one_phone_is_not_shared(self):
        web = Phone(self.net, 65)
        web.login(IVAN)
        app = self.behind(web, 66, app="ivanapp001")
        app.login(IVAN)
        self.assertEqual(self.shared(), [])

    def test_iphone_and_android_same_ip_and_mac(self):
        # Випадок з життя: два профілі — один з iPhone, другий з Android, а MAC і IP однакові. Навіть з однаковим
        # відбитком (чи без нього) iPhone і Android — точно два різні телефони
        iphone = Phone(self.net, 69, ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
                                           "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1")
        iphone.login(IVAN)
        android = self.behind(iphone, 70)
        android.fp = iphone.fp  # відбиток не допомагає — допомагає система
        android.login(OLHA)
        self.assertEqual(self.shared(), [iphone.mac])
        self.assertEqual(self.events("olha", "login_other_device"), [])

    def test_android_app_and_iphone(self):
        app = Phone(self.net, 71, app="olhaapp001")
        app.login(OLHA)
        iphone = self.behind(app, 72)
        iphone.ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) Version/17.4 Mobile/15E148 Safari/604.1"
        iphone.login(IVAN)
        self.assertEqual(self.shared(), [app.mac])

    def test_two_android_models(self):
        a = Phone(self.net, 73, model="SM-A515F")
        a.login(IVAN)
        b = self.behind(a, 74)
        b.ua = CHROME.format(model="Pixel 7")
        b.fp = a.fp
        b.login(OLHA)
        self.assertEqual(self.shared(), [a.mac])

    def test_same_phone_app_and_chrome_and_desktop_mode(self):
        # Один телефон: застосунок пише модель «samsung SM-A515F», Chrome — «SM-A515F», а в режимі «версія для ПК»
        # браузер показує себе як Linux — це не різні пристрої
        web = Phone(self.net, 75, model="SM-A515F")
        web.login(IVAN)
        app = self.behind(web, 76, app="ivanapp075")
        app.ua += " CrewHubModel/samsung%20SM-A515F"
        app.login(IVAN)
        desk = self.behind(web, 77)
        desk.ua, desk.fp = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36", None
        desk.login(OLHA)
        self.assertEqual(self.shared(), [])

    def test_shared_mac_is_not_blocked(self):
        ivan = Phone(self.net, 67)
        ivan.login(IVAN)
        self.behind(ivan, 68).login(OLHA)
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        page = admin.req("get", "/admin?tab=logs").get_data(as_text=True)
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]
        admin.req("post", "/admin/devices/block", data={"csrf": csrf, "mac": ivan.mac}, headers={"Origin": "http://localhost"})
        self.assertEqual(self.sql("select count(*) from blocked_devices")[0][0], 0)  # заблокувало б усіх за роутером
        admin.req("post", "/admin/devices/block", data={"csrf": csrf, "mac": ivan.mac, "device_key": ivan.key}, headers={"Origin": "http://localhost"})
        self.assertEqual(tuple(self.sql("select device_key, mac from blocked_devices")[0]), (ivan.key, None))


class DeviceIdentity1714Test(ScenarioBase):
    """17.14: блокування — в одному місці; свій телефон упізнається, навіть коли змінились і код, і MAC;
    «Пристрої в мережі» не видають Android за iPhone за спільним MAC."""
    IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.4 Mobile/15E148 Safari/604.1")

    def setUp(self):
        super().setUp()
        self.admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")

    def test_block_buttons_moved_to_one_place(self):
        phone = Phone(self.net, 80)
        phone.login(IVAN)
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        self.assertNotIn("/admin/devices/block", self.admin.req("get", f"/admin/employee/{uid}").get_data(as_text=True))
        self.assertNotIn("/admin/devices/block", self.admin.req("get", "/admin?tab=live").get_data(as_text=True))
        logs = self.admin.req("get", "/admin?tab=logs").get_data(as_text=True)
        self.assertIn("— виберіть пристрій —", logs)
        pick = re.search(r'<option value="([^"]*' + re.escape(phone.key) + r'[^"]*)">Іван Петренко', logs)[1]
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', logs)[1]
        self.admin.req("post", "/admin/devices/block", data={"csrf": csrf, "pick": pick.replace("&#39;", "'")},
                       headers={"Origin": "http://localhost"})
        self.assertEqual(tuple(self.sql("select device_key, user_id from blocked_devices")[0]), (phone.key, uid))

    def test_iphone_new_code_and_new_mac_still_his(self):
        iphone = Phone(self.net, 81, ua=self.IPHONE)
        iphone.login(IVAN)
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            core.remember_device(c, uid, iphone.key, iphone.ip)
            core.note_device(c, uid, iphone.key, iphone.fp, iphone.mac)
            c.commit()
        self.assertEqual(self.sql("select model from device_history where device_key = ?", iphone.key)[0][0], "iPhone")
        iphone.logout()
        # Safari сам почистив дані сайту (новий код), iPhone змінив «приватну адресу Wi-Fi» (новий MAC) і отримав новий IP
        del self.net[iphone.ip]
        iphone.key, iphone.ip, iphone.mac = "web-0000f00d", "192.168.1.181", "da:00:00:00:01:81"
        self.net[iphone.ip] = iphone
        iphone.login(IVAN)
        self.assertEqual(self.events("ivan", "login_new_device"), [])
        self.assertEqual(self.sql("select device_key from devices where user_id = ?", uid)[0][0], "web-0000f00d")  # прив'язка перейшла
        self.assertTrue(self.events("ivan", "device_recognised"))

    def test_same_model_twins_are_not_guessed(self):
        # Такий самий відбиток є в колеги (однакові iPhone) — за відбитком не вгадуємо: новий пристрій
        olha_phone = Phone(self.net, 82, ua=self.IPHONE)
        iphone = Phone(self.net, 83, ua=self.IPHONE)
        olha_phone.fp = iphone.fp
        olha_phone.login(OLHA)
        iphone.login(IVAN)
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        with core.connect() as c:
            core.remember_device(c, uid, iphone.key, iphone.ip)
            c.commit()
        iphone.logout()
        iphone.key, iphone.mac = "web-0000beef", "da:00:00:00:01:83"
        self.net[iphone.ip] = iphone
        iphone.login(IVAN)
        self.assertEqual(len(self.events("ivan", "login_new_device")), 1)

    def test_shared_mac_row_is_a_router_not_an_iphone(self):
        core._mac_seen.clear()
        iphone = Phone(self.net, 84, ua=self.IPHONE)
        iphone.login(IVAN)
        android = Phone(self.net, 85, model="SM-A515F")
        del self.net[android.ip]
        android.ip, android.mac = iphone.ip, iphone.mac
        android.login(OLHA)
        with core.connect() as c:
            lan.record(c, {iphone.ip: iphone.mac}, {})
            rows = lan.devices(c)
        row = [r for r in rows if r["mac"] == iphone.mac][0]
        self.assertEqual(row["label"], "Роутер чи підсилювач Wi-Fi (спільний MAC)")
        self.assertIsNone(row["user_id"])
        self.assertIn("Іван Петренко (iPhone)", row["model"])
        self.assertIn("Ольга Коваль (SM-A515F)", row["model"])



class IPhoneWebPresence1715Test(ScenarioBase):
    """17.15: iPhone без застосунку (браузер) не відвалюється. Сервер записує MAC, навіть якщо людина почала зміну й
    одразу заблокувала екран; після «Перезаписати IP і MAC» сам знаходить MAC змін, що йдуть; сонний iPhone, що
    відповів не з першого разу, — на місці; iPhone з двома браузерами — не «роутер зі спільним MAC»."""
    IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/18.6 Mobile/15E148 Safari/604.1")
    VIBER = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Mobile/15E148 Viber/25.6.0")

    def setUp(self):
        super().setUp()
        core._mac_seen.clear()
        core._shared.update(at=0.0, set=set())
        self.addCleanup(core._shared.update, at=0.0, set=set())
        self.states = {}  # ip → (mac, стан), як `ip neigh`: без запису — система нічого не каже
        patcher = mock.patch.object(lan, "neighbour_states", lambda: dict(self.states))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tick(10)
        self.iphone = Phone(self.net, 40, ua=self.IPHONE)

    def stale(self, phone):
        """Запис таблиці сусідів не підтверджено щойно (iPhone тільки прокинувся): MAC під час запиту невідомий."""
        self.states[phone.ip] = (phone.mac, "STALE")

    def mac_of(self, username):
        return [r[0] for r in self.sql("select h.mac from device_history h join users u on u.id = h.user_id where u.username = ?", username)]

    def admin(self):
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        page = admin.req("get", "/admin?tab=live").get_data(as_text=True)
        return admin, re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]

    def test_started_and_locked_at_once(self):
        # Увійшов, почав зміну і заблокував екран за кілька секунд — раніше MAC так і лишався невідомим, і за 10 хв пауза
        self.stale(self.iphone)
        self.iphone.login(IVAN)
        self.assertTrue(self.iphone.clock("start")["ok"])
        self.assertIsNone(self.shift("ivan")["mac"])
        self.live_until(10, 40)  # телефон у кишені 40 хв, сторінка спить
        s = self.shift("ivan")
        self.assertEqual((s["status"], s["mac"]), ("active", self.iphone.mac))
        self.assertEqual(self.mac_of("ivan"), [self.iphone.mac])
        self.assertEqual(self.sql("select count(*) from breaks")[0][0], 0)
        # пішов з Wi-Fi — як і раніше, пауза
        self.iphone.online = False
        self.live_until(10, 55)
        self.assertEqual(self.shift("ivan")["status"], "paused")

    def test_known_mac_is_used_at_once(self):
        # Учора MAC уже підтверджено; сьогодні запит прийшов, коли запис ще «застарілий» — зміна одразу знає MAC
        self.states[self.iphone.ip] = (self.iphone.mac, "REACHABLE")
        self.iphone.login(IVAN)
        self.stale(self.iphone)
        core._mac_cache.update(at=0.0, table={})
        self.assertTrue(self.iphone.clock("start")["ok"])
        self.assertEqual(self.shift("ivan")["mac"], self.iphone.mac)

    def test_relearn_button_does_not_lose_iphone(self):
        self.states[self.iphone.ip] = (self.iphone.mac, "REACHABLE")
        self.iphone.login(IVAN)
        self.assertTrue(self.iphone.clock("start")["ok"])
        self.live_until(10, 10)
        self.assertEqual(self.shift("ivan")["status"], "active")
        admin, csrf = self.admin()
        admin.req("post", "/admin/devices/relearn", data={"csrf": csrf}, headers={"Origin": "http://localhost"})
        self.assertIsNone(self.shift("ivan")["mac"])  # «перезаписати» стерло MAC — а iPhone спить і сам не озветься
        self.live_until(10, 45)
        s = self.shift("ivan")
        self.assertEqual((s["status"], s["mac"]), ("active", self.iphone.mac))  # сервер сам знайшов MAC за адресою зміни
        self.assertEqual(self.mac_of("ivan"), [self.iphone.mac])

    def test_relearn_after_outage_does_not_guess(self):
        # Після відключення світла роутер міг роздати IP наново — MAC за старою адресою зміни не вгадуємо
        self.iphone.login(IVAN)
        self.assertTrue(self.iphone.clock("start")["ok"])
        with core.connect() as c:
            c.execute("update shifts set mac = null")
            core.meta_set(c, "outage_end", self.at(10, 5))
            c.commit()
        olha = Phone(self.net, 41)
        del self.net[olha.ip]
        olha.ip = self.iphone.ip
        self.net[olha.ip] = olha  # за адресою Івана тепер телефон Ольги
        self.tick(10, 6)
        core.presence_step()
        self.assertIsNone(self.shift("ivan")["mac"])

    def test_sleepy_iphone_answers_on_second_ask(self):
        self.states[self.iphone.ip] = (self.iphone.mac, "REACHABLE")
        self.iphone.login(IVAN)
        self.assertTrue(self.iphone.clock("start")["ok"])
        asked = []

        def present(ips, wait=0):  # з першого разу сонний iPhone не відповідає, з другого — так
            asked.append(wait)
            return {ip: p.mac for ip, p in self.net.items() if p.online and (wait or p is not self.iphone)}
        with mock.patch.object(lan, "present", present):
            self.live_until(10, 40)
        self.assertEqual(self.shift("ivan")["status"], "active")
        self.assertIn(core.PRESENCE_RETRY[0], asked)

    def test_iphone_with_two_browsers_is_not_a_router(self):
        # Safari і браузер усередині Viber (чи іконка на екрані «Додому»): у кожного свій код і відбиток, а Safari ще й
        # сам трохи змінює відбиток — це один iPhone, а не два телефони за роутером
        self.states[self.iphone.ip] = (self.iphone.mac, "REACHABLE")
        self.iphone.login(IVAN)
        viber = Phone(self.net, 42, ua=self.VIBER)
        del self.net[viber.ip]
        viber.ip, viber.mac, viber.fp = self.iphone.ip, self.iphone.mac, "00000000deadbeef"
        viber.login(IVAN)
        home = Phone(self.net, 43, ua=self.IPHONE)
        del self.net[home.ip]
        home.ip, home.mac, home.fp = self.iphone.ip, self.iphone.mac, "00000000feedf00d"
        home.login(IVAN)
        self.assertEqual(self.sql("select count(*) from shared_macs")[0][0], 0)
        self.assertEqual(self.mac_of("ivan"), [self.iphone.mac] * 3)

    def test_android_chrome_and_samsung_internet_is_not_a_router(self):
        chrome = Phone(self.net, 44, model="SM-A515F")
        chrome.login(IVAN)
        samsung = Phone(self.net, 45)
        del self.net[samsung.ip]
        samsung.ip, samsung.mac = chrome.ip, chrome.mac
        samsung.ua = ("Mozilla/5.0 (Linux; Android 13; SM-A515F) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "SamsungBrowser/23.0 Chrome/115.0 Mobile Safari/537.36")
        samsung.login(IVAN)
        self.assertEqual(self.sql("select count(*) from shared_macs")[0][0], 0)

    def test_iphone_and_android_behind_a_router_still_shared(self):
        android = Phone(self.net, 46)
        del self.net[android.ip]
        android.ip, android.mac = self.iphone.ip, self.iphone.mac
        self.iphone.login(IVAN)
        android.login(OLHA)
        self.assertEqual([r[0] for r in self.sql("select why from shared_macs")], ["iPhone і Android"])

    def test_old_mark_without_reason_lasts_an_hour(self):
        with core.connect() as c:  # позначка 17.13–17.14 з однією людиною: помилкова (один iPhone з двома браузерами)
            c.execute("insert into shared_macs(mac, first_at, last_at, note) values (?, ?, ?, 'Іван Петренко')",
                      (self.iphone.mac, self.at(8), self.at(8)))
            c.execute("insert into shared_macs(mac, first_at, last_at, note, why) values ('da:00:00:00:00:77', ?, ?, 'x', 'iPhone і Android')",
                      (self.at(8), self.at(8)))
            # позначка 17.13–17.14 з різними людьми — справжній роутер: як і раніше, 7 днів
            c.execute("insert into shared_macs(mac, first_at, last_at, note) values ('da:00:00:00:00:78', ?, ?, 'Іван Петренко, Ольга Коваль')",
                      (self.at(8), self.at(8)))
            c.commit()
        core._shared.update(at=0.0)
        self.assertEqual(core.shared_macs(), {"da:00:00:00:00:77", "da:00:00:00:00:78"})
        self.tick(8, 30)
        core._shared.update(at=0.0)
        self.assertEqual(core.shared_macs(), {"da:00:00:00:00:77", "da:00:00:00:00:78", self.iphone.mac})

    def test_not_a_router_button(self):
        android = Phone(self.net, 47)
        del self.net[android.ip]
        android.ip, android.mac = self.iphone.ip, self.iphone.mac
        self.iphone.login(IVAN)
        android.login(OLHA)
        admin, csrf = self.admin()
        self.assertIn("Це не роутер", admin.req("get", "/admin?tab=live").get_data(as_text=True))
        admin.req("post", "/admin/devices/unshare", data={"csrf": csrf, "mac": self.iphone.mac}, headers={"Origin": "http://localhost"})
        self.assertEqual(self.sql("select count(*) from shared_macs")[0][0], 0)
        self.assertNotIn("Це не роутер", admin.req("get", "/admin?tab=live").get_data(as_text=True))
        self.assertTrue(self.sql("select 1 from audit where entity = 'shared_macs'"))

    def test_overview_says_why_iphone_is_not_seen(self):
        self.stale(self.iphone)
        self.iphone.login(IVAN)
        self.assertTrue(self.iphone.clock("start")["ok"])
        admin, _ = self.admin()
        row = [r for r in admin.req("get", "/api/live").get_json()["rows"] if r["name"] == "Іван Петренко"][0]
        self.assertIn("MAC телефона ще не підтверджено", row["net_note"])
        self.live_until(10, 2)
        core._live_cache.update(at=0.0)
        row = [r for r in admin.req("get", "/api/live").get_json()["rows"] if r["name"] == "Іван Петренко"][0]
        self.assertIsNone(row["net_note"])  # MAC підтверджено — сервер бачить телефон

class IPhoneSeenWithoutSite1715Test(ScenarioBase):
    """17.15: iPhone один раз увійшов — далі сервер бачить його в мережі сам, сайт відкривати не треба: за MAC, який
    запам'ятав разом з кодом пристрою й відбитком (питає мережу, як роутер, — не пінгом). Змінив MAC — сервер упізнає
    його за назвою в мережі, або адмін вказує «Це телефон працівника?» у «Пристроях у мережі»."""
    IPHONE = IPhoneWebPresence1715Test.IPHONE

    def setUp(self):
        super().setUp()
        core._mac_seen.clear()
        core._shared.update(at=0.0, set=set())
        self.tick(8)
        self.iphone = Phone(self.net, 50, ua=self.IPHONE)
        self.iphone.login(IVAN)  # один раз, о 8:00; далі сторінку не відкривають

    def rotate(self, n):
        """iPhone змінив «приватну адресу Wi-Fi»: новий MAC і новий IP."""
        del self.net[self.iphone.ip]
        self.iphone.ip, self.iphone.mac = f"192.168.1.{n}", f"da:00:00:00:01:{n:02x}"
        self.net[self.iphone.ip] = self.iphone

    def record(self, names=None):
        with core.connect() as c:
            lan.record(c, {ip: p.mac for ip, p in self.net.items() if p.online}, names or {})

    def history_macs(self):
        return [r[0] for r in self.sql("select mac from device_history h join users u on u.id = h.user_id where u.username = 'ivan'")]

    def test_shift_without_opening_site(self):
        self.live_until(12)  # жодного запиту з iPhone після входу
        s = self.shift("ivan")
        self.assertEqual(s["status"], "active")
        self.assertLessEqual(s["started_at"], self.at(9, 1))  # почалась сама
        self.assertEqual(self.sql("select count(*) from breaks")[0][0], 0)

    def test_device_list_shows_code_and_fingerprint(self):
        self.record()
        with core.connect() as c:
            row = [r for r in lan.devices(c) if r["mac"] == self.iphone.mac][0]
        self.assertEqual((row["full_name"], row["dev_key"], row["dev_fp"]), ("Іван Петренко", self.iphone.key, self.iphone.fp[:8]))
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        self.assertIn(f"код {self.iphone.key}", admin.req("get", "/admin?tab=live").get_data(as_text=True))

    def test_admin_says_whose_phone_after_mac_change(self):
        self.rotate(60)
        self.record()
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        page = admin.req("get", "/admin?tab=live").get_data(as_text=True)
        self.assertIn("Це телефон працівника?", page)
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', page)[1]
        uid = self.sql("select id from users where username = 'ivan'")[0][0]
        admin.req("post", "/admin/lan/assign", data={"csrf": csrf, "mac": self.iphone.mac, "user_id": uid},
                  headers={"Origin": "http://localhost"})
        self.assertEqual(self.history_macs(), [self.iphone.mac])
        self.assertEqual(self.sql("select how from lan_devices where mac = ?", self.iphone.mac)[0][0], "вручну")
        self.assertTrue(self.events("ivan", "device_recognised"))
        self.live_until(10)  # зміна почалась сама за новим MAC, без сайту
        self.assertEqual(self.shift("ivan")["status"], "active")

    def test_assign_needs_a_phone_login_once(self):
        stranger = Phone(self.net, 61)
        self.record()
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        csrf = re.search(r'name="crewhub-csrf" content="([^"]+)"', admin.req("get", "/admin?tab=live").get_data(as_text=True))[1]
        olha = self.sql("select id from users where username = 'olha'")[0][0]  # Ольга ще не заходила з телефона
        r = admin.req("post", "/admin/lan/assign", data={"csrf": csrf, "mac": stranger.mac, "user_id": olha},
                      headers={"Origin": "http://localhost"}, follow_redirects=True)
        self.assertIn("ще жодного разу не заходила", r.get_data(as_text=True))
        self.assertIsNone(self.sql("select user_id from lan_devices where mac = ?", stranger.mac)[0][0])

    def test_recognised_by_name_after_mac_change(self):
        self.record({self.iphone.ip: "Ivans-iPhone"})
        old = self.iphone.mac
        self.rotate(62)
        self.record({self.iphone.ip: "Ivans-iPhone"})
        self.assertEqual(self.history_macs(), [self.iphone.mac])
        row = self.sql("select user_id, how from lan_devices where mac = ?", self.iphone.mac)[0]
        self.assertEqual(row["how"], "за назвою в мережі")
        details = self.events("ivan", "device_recognised")[-1][1]
        self.assertIn(old, details)
        self.live_until(10)
        self.assertEqual(self.shift("ivan")["status"], "active")

    def test_generic_name_is_not_a_proof(self):
        self.record({self.iphone.ip: "iPhone"})
        self.rotate(63)
        self.record({self.iphone.ip: "iPhone"})  # «iPhone» — у всіх однаково: не вгадуємо
        self.assertNotIn(self.iphone.mac, self.history_macs())
        self.assertIsNone(self.sql("select user_id from lan_devices where mac = ?", self.iphone.mac)[0][0])

    def test_same_name_while_old_phone_online_is_other_device(self):
        self.record({self.iphone.ip: "Ivans-iPhone"})
        other = Phone(self.net, 64)  # старий телефон Івана теж у мережі — це інший пристрій з такою ж назвою
        self.record({self.iphone.ip: "Ivans-iPhone", other.ip: "Ivans-iPhone"})
        self.assertIsNone(self.sql("select user_id from lan_devices where mac = ?", other.mac)[0][0])

    def test_safari_wiped_site_storage_keeps_device_code(self):
        # Safari стер сховище сайту (тиждень без відвідин): автовхід за cookie — і сторінка повертає той самий код пристрою
        remember = self.iphone.c.get_cookie("crewhub_remember").value
        fresh = core.app.test_client()
        fresh.set_cookie("crewhub_remember", remember, domain="localhost")
        page = fresh.get("/me?tab=clock", headers={"User-Agent": self.IPHONE},
                         environ_base={"REMOTE_ADDR": self.iphone.ip}).get_data(as_text=True)
        self.assertIn(f'device: "{self.iphone.key}"', page)

class LeftWorkBehindRouter1715Test(ScenarioBase):
    """17.15: люди пішли з роботи, а зміни не закрились. Другий роутер чи підсилювач Wi-Fi відповідає за всіх, хто за
    ним, і завжди «в мережі» — за його MAC сервер вважав на місці людей, які вже пішли. Тепер такий MAC не тримає зміну:
    різні люди з різних телефонів за ним, кілька людей на зміні з одним MAC, один MAC на кількох IP — MAC спільний."""
    IPHONE = IPhoneWebPresence1715Test.IPHONE

    def setUp(self):
        super().setUp()
        core._mac_seen.clear()
        core._shared.update(at=0.0, set=set())
        self.addCleanup(core._shared.update, at=0.0, set=set())
        # таблиця сусідів зі станами, як `ip neigh`: хто в мережі — щойно підтверджений
        states = lambda: {ip: (p.mac, "REACHABLE") for ip, p in self.net.items() if p.online}  # noqa: E731
        patcher = mock.patch.object(lan, "neighbour_states", states)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tick(17, 50)

    def behind(self, first, n, ua=None):
        p = Phone(self.net, n, ua=ua)
        del self.net[p.ip]
        p.ip, p.mac = first.ip, first.mac
        return p

    def leave_and_wait(self, *phones):
        """Пінг о 17:59, далі люди йдуть додому; роутер (його IP і MAC) лишається в мережі."""
        self.tick(17, 59)
        for p in phones:
            p.clock("heartbeat")
        self.live_until(20)

    def assert_closed(self, *users):
        for u in users:
            s = self.shift(u)
            self.assertEqual(s["status"], "closed", u)
            self.assertLessEqual(s["ended_at"], self.at(18, 10), u)  # оплата — до останнього зв'язку, не до 20:00

    def test_two_iphones_behind_router(self):
        ivan = Phone(self.net, 80, ua=self.IPHONE)
        olha = self.behind(ivan, 81, ua=self.IPHONE)
        ivan.login(IVAN)
        olha.login(OLHA)
        self.assertTrue(ivan.clock("start")["ok"])
        self.assertTrue(olha.clock("start")["ok"])
        self.assertEqual([r[0] for r in self.sql("select why from shared_macs")], ["телефони різних людей"])
        self.leave_and_wait(ivan, olha)
        self.assert_closed("ivan", "olha")

    def test_same_phones_behind_router_both_on_shift(self):
        # двоє однакових телефонів (той самий відбиток) — за запитами не розрізнити; але одночасно на зміні двоє людей
        # з одним MAC — це не телефон
        ivan = Phone(self.net, 82)
        olha = self.behind(ivan, 83)
        olha.fp = ivan.fp
        ivan.login(IVAN)
        olha.login(OLHA)
        self.assertTrue(ivan.clock("start")["ok"])
        self.assertTrue(olha.clock("start")["ok"])
        self.tick(17, 52)
        core.presence_step()
        self.assertEqual([r[0] for r in self.sql("select why from shared_macs")], ["на зміні кілька людей з одним MAC"])
        self.leave_and_wait(ivan, olha)
        self.assert_closed("ivan", "olha")

    def test_repeater_same_mac_on_two_ips(self):
        # підсилювач Wi-Fi: у кожного телефона свій IP, але MAC підсилювача; Іван пішов, а за підсилювачем ще хтось
        ivan = Phone(self.net, 84, ua=self.IPHONE)
        stranger = Phone(self.net, 85)
        stranger.mac = ivan.mac
        ivan.login(IVAN)
        self.assertTrue(ivan.clock("start")["ok"])
        self.tick(17, 59)
        ivan.clock("heartbeat")
        ivan.online = False  # Іван пішов; підсилювач і далі відповідає за телефон незнайомця
        self.live_until(20)
        self.assert_closed("ivan")
        self.assertEqual([r[0] for r in self.sql("select why from shared_macs")], ["один MAC на кількох IP (підсилювач Wi-Fi)"])

    def test_own_phone_left_is_closed_and_overview_says_when(self):
        ivan = Phone(self.net, 86, ua=self.IPHONE)
        ivan.login(IVAN)
        self.assertTrue(ivan.clock("start")["ok"])
        self.tick(17, 59)
        ivan.clock("heartbeat")
        ivan.online = False
        self.live_until(18, 30)
        self.assertEqual(self.shift("ivan")["status"], "paused")
        admin = Phone(self.net, 99, ua=PC).login("admin", "admin123")
        row = [r for r in admin.req("get", "/api/live").get_json()["rows"] if r["name"] == "Іван Петренко"][0]
        self.assertIn("сервер закриє зміну о", row["close_note"])
        self.live_until(20)
        self.assert_closed("ivan")

if __name__ == "__main__":
    unittest.main()
