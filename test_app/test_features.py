"""Тести версії 17: папка даних, settings.ini, пристрої в мережі, інтерфейс без інтернету.
python -m unittest test_features -v"""
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest

from test_crewhub import CrewHubBase, core, settings_file  # noqa: F401 — test_crewhub налаштовує оточення


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class SettingsFileTest(CrewHubBase):
    def edit_file(self, change):
        """Правка settings.ini «людиною»: файл змінено, і він уже 3 с як збережений."""
        text = change(read(core.SETTINGS_FILE))
        with open(core.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        old = time.time() - 3
        os.utime(core.SETTINGS_FILE, (old, old))

    def test_admin_changes_go_to_file(self):
        admin = self.login("admin", "admin123")
        admin.post("/admin/settings", data={"company_name": "Кав'ярня Ранок", "late_threshold_minutes": "7"})
        text = read(core.SETTINGS_FILE)
        self.assertIn("company_name = Кав'ярня Ранок", text)
        self.assertIn("late_threshold_minutes = 7", text)
        admin.post("/admin/zones", data={"action": "location", "name": "Склад", "address": "вул. Лісова, 1"})
        admin.post("/admin/zones", data={"action": "network", "location_id": "2", "cidr": "10.20.0.0/24", "label": "Склад Wi-Fi"})
        text = read(core.SETTINGS_FILE)
        self.assertIn("[point 2]\nname = Склад\naddress = вул. Лісова, 1", text)
        self.assertIn("    10.20.0.0/24 | Склад Wi-Fi", text)

    def test_fresh_database_gets_rules_and_points_back(self):
        admin = self.login("admin", "admin123")
        admin.post("/admin/settings", data={"company_name": "Точка Б", "wifi_grace_minutes": "5"})
        admin.post("/admin/zones", data={"action": "location", "name": "Склад"})
        admin.post("/admin/zones", data={"action": "network", "location_id": "2", "cidr": "10.20.0.0/24"})
        admin.post("/admin/zones", data={"action": "wifi", "location_id": "2", "wifi_ssid": "Sklad", "wifi_password": "12345678"})
        # Програму перевстановили / базу втратили: нова база, той самий settings.ini
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(core.DB_PATH + suffix):
                os.remove(core.DB_PATH + suffix)
        settings_file._state.update(file=None, db=None)
        core.init_db()
        settings_file.sync()
        with core.connect() as c:
            self.assertEqual(core.setting("company_name", c), "Точка Б")
            self.assertEqual(core.setting("wifi_grace_minutes", c), "5")
            loc = c.execute("select * from locations where id = 2").fetchone()
            self.assertEqual((loc["name"], loc["wifi_ssid"], loc["wifi_password"]), ("Склад", "Sklad", "12345678"))
            self.assertEqual([r[0] for r in c.execute("select cidr from networks where location_id = 2")], ["10.20.0.0/24"])

    def test_hand_edit_applied_and_bad_values_reported(self):
        settings_file.sync()
        self.edit_file(lambda t: t.replace("late_threshold_minutes = 10", "late_threshold_minutes = 25")
                       .replace("wifi_grace_minutes = 2", "wifi_grace_minutes = багато")
                       .replace("auto_start = 0", "auto_start = так")
                       .replace("name = Тестова точка", "name = Кафе на розі"))
        settings_file.sync()
        with core.connect() as c:
            self.assertEqual(core.setting("late_threshold_minutes", c), "25")
            self.assertEqual(core.setting("wifi_grace_minutes", c), "2")  # неправильне — лишилось попереднє
            self.assertEqual(core.setting("auto_start", c), "1")
            self.assertEqual(c.execute("select name from locations where id = 1").fetchone()[0], "Кафе на розі")
            self.assertIn("settings.ini", c.execute("select changes from audit where entity = 'settings' order by id desc").fetchone()[0])
        text = read(core.SETTINGS_FILE)
        self.assertIn("УВАГА", text.split("[server]")[0])
        self.assertIn("wifi_grace_minutes = «багато»", text)
        self.assertIn("wifi_grace_minutes = 2", text)  # файл переписано правильним значенням
        self.assertIn("auto_start = 1", text)

    def test_networks_list_is_complete(self):
        settings_file.sync()
        self.edit_file(lambda t: re.sub(r"networks =\n(    .*\n)+",
                                        "networks =\n    203.0.113.0/28 | каса\n    198.51.100.7 | роутер\n    0.0.0.0/0 | усе\n", t))
        settings_file.sync()
        nets = dict(self.sql("select cidr, label from networks where location_id = 1"))
        self.assertEqual(nets, {"203.0.113.0/28": "каса", "198.51.100.7/32": "роутер"})  # локальна мережа ПК прибрана
        self.assertIn("0.0.0.0/0", read(core.SETTINGS_FILE).split("[server]")[0])

    def test_point_cannot_be_deleted_by_file_only_disabled(self):
        settings_file.sync()
        self.edit_file(lambda t: t.split("[point 1]")[0] + "[point 7]\nname = Нова\nactive = 0\nnetworks =\n    10.7.0.0/24\n")
        settings_file.sync()
        self.assertEqual(self.sql("select count(*) from locations where id = 1")[0][0], 1)
        self.assertEqual(tuple(self.sql("select name, is_active from locations where id = 7")[0]), ("Нова", 0))
        self.assertIn("[point 1]", read(core.SETTINGS_FILE))  # точка 1 повернулась у файл

    def test_file_being_saved_is_not_read_half_written(self):
        settings_file.sync()
        with open(core.SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write("[point 1]\nname = Обрі")  # «зберігається» просто зараз
        settings_file.sync()
        self.assertEqual(self.sql("select name from locations where id = 1")[0][0], "Тестова точка")

    def test_unreadable_file_is_rewritten_from_database(self):
        settings_file.sync()
        with open(core.SETTINGS_FILE, "wb") as f:
            f.write(b"\xff\xfe\x00garbage[[[")
        old = time.time() - 3
        os.utime(core.SETTINGS_FILE, (old, old))
        settings_file.sync()
        self.assertIn("company_name = CrewHub Test", read(core.SETTINGS_FILE))

    def test_port_and_windows_bom(self):
        settings_file.sync()
        self.assertIsNone(settings_file.port()) if "port = 5000" not in read(core.SETTINGS_FILE) else None
        self.edit_file(lambda t: "﻿" + t.replace("port = 5000", "port = 8080"))
        self.assertEqual(settings_file.port(), 8080)
        settings_file.sync()
        self.assertIn("port = 8080", read(core.SETTINGS_FILE))  # порт не губиться при перезаписі файлу

    def test_wifi_password_hidden_in_audit(self):
        settings_file.sync()
        self.edit_file(lambda t: t.replace("wifi_ssid = \n", "wifi_ssid = Cafe\n").replace("wifi_password = \n", "wifi_password = secret123\n"))
        settings_file.sync()
        self.assertEqual(self.sql("select wifi_password from locations where id = 1")[0][0], "secret123")
        self.assertNotIn("secret123", self.sql("select changes from audit order by id desc")[0][0])


class DataDirTest(CrewHubBase):
    def test_legacy_data_moves_once(self):
        tmp = tempfile.mkdtemp()
        code, data = os.path.join(tmp, "test_app"), os.path.join(tmp, "CrewHub-data")
        os.makedirs(os.path.join(code, "people", "Іван Петренко"))
        with open(os.path.join(code, "people", "Іван Петренко", "profile.json"), "w") as f:
            f.write("{}")
        os.makedirs(os.path.join(code, "backups"))
        with open(os.path.join(code, "server_id.txt"), "w") as f:
            f.write("0123456789abcdef")
        legacy = os.path.join(code, "crewhub_test.db")
        c = sqlite3.connect(legacy)
        c.execute("pragma journal_mode=wal")
        c.execute("create table t(x)")
        c.execute("insert into t values (42)")
        c.commit()  # дані лежать у WAL — перенесення має їх не загубити
        saved = {k: getattr(core, k) for k in ("BASE_DIR", "DATA_DIR", "LEGACY_DB", "DB_PATH")}
        env_db = os.environ.pop("CREWHUB_DB")
        try:
            core.BASE_DIR, core.DATA_DIR, core.LEGACY_DB = code, data, legacy
            core.DB_PATH = os.path.join(data, "crewhub.db")
            msg = core.migrate_legacy_data()
            c.close()
            self.assertIn(data, msg)
            self.assertEqual(sqlite3.connect(core.DB_PATH).execute("select x from t").fetchone()[0], 42)
            self.assertTrue(os.path.exists(os.path.join(data, "people", "Іван Петренко", "profile.json")))
            self.assertEqual(read(os.path.join(data, "server_id.txt")), "0123456789abcdef")
            self.assertFalse(os.path.exists(legacy))
            self.assertTrue(any(n.startswith("crewhub_test.db.перенесено") for n in os.listdir(code)))
            self.assertIn(data, read(os.path.join(code, "ДЕ_ДАНІ.txt")))
            self.assertIsNone(core.migrate_legacy_data())  # другий запуск нічого не робить
        finally:
            os.environ["CREWHUB_DB"] = env_db
            for k, v in saved.items():
                setattr(core, k, v)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_data_lives_outside_code(self):
        self.assertFalse(core.DATA_DIR.startswith(core.BASE_DIR + os.sep))
        for path in (core.SETTINGS_FILE, core.LOG_DIR):
            self.assertTrue(path.startswith(core.DATA_DIR))


class LanTest(CrewHubBase):
    import lan as _lan  # noqa: E402 — після налаштування оточення
    lan = _lan

    def test_neighbour_tables_of_all_systems(self):
        proc = ("IP address       HW type     Flags       HW address            Mask     Device\n"
                "192.168.1.1      0x1         0x2         a4:2b:b0:11:22:33     *        wlan0\n"
                "192.168.1.23     0x1         0x2         da:a1:19:00:00:01     *        wlan0\n"
                "192.168.1.40     0x1         0x0         00:00:00:00:00:00     *        wlan0\n")
        self.assertEqual(self.lan.parse_proc_arp(proc), {"192.168.1.1": "a4:2b:b0:11:22:33", "192.168.1.23": "da:a1:19:00:00:01"})
        win = ("Interface: 192.168.1.5 --- 0x7\n  Internet Address      Physical Address      Type\n"
               "  192.168.1.1           a4-2b-b0-11-22-33     dynamic\n  192.168.1.255         ff-ff-ff-ff-ff-ff     static\n"
               "  224.0.0.22            01-00-5e-00-00-16     static\n")
        self.assertEqual(self.lan.parse_arp_a(win), {"192.168.1.1": "a4:2b:b0:11:22:33"})
        mac = "? (192.168.1.7) at 0:1e:c2:a:b:c on en0 ifscope [ethernet]\n? (192.168.1.9) at (incomplete) on en0 ifscope [ethernet]\n"
        self.assertEqual(self.lan.parse_arp_a(mac), {"192.168.1.7": "00:1e:c2:0a:0b:0c"})
        self.assertTrue(self.lan.private_mac("da:a1:19:00:00:01"))
        self.assertFalse(self.lan.private_mac("a4:2b:b0:11:22:33"))

    def test_windows_name_from_netbios(self):
        import struct
        q = self.lan.nbstat_query(7)
        self.assertEqual(len(q), 50)
        head = q[:12] + q[12:46] + struct.pack(">HHIH", 0x21, 1, 0, 0)
        entry = lambda n, sfx, fl: n.ljust(15).encode() + bytes([sfx]) + struct.pack(">H", fl)  # noqa: E731
        resp = head + bytes([2]) + entry("WORKGROUP", 0, 0x8400) + entry("ARTEM-PC", 0, 0x0400)
        self.assertEqual(self.lan.parse_nbstat(resp), "ARTEM-PC")
        self.assertIsNone(self.lan.parse_nbstat(b"\x00" * 20))  # обрізаний пакет — без падіння

    def test_router_and_mdns_names(self):
        import struct
        q = self.lan.dns_query(9, self.lan.ptr_name("192.168.1.23"))
        rdata = b"".join(bytes([len(p)]) + p.encode() for p in "Galaxy-S23.lan".split(".")) + b"\x00"
        resp = (struct.pack(">HHHHHH", 9, 0x8180, 1, 1, 0, 0) + q[12:] + b"\xc0\x0c"
                + struct.pack(">HHIH", 12, 1, 60, len(rdata)) + rdata)
        self.assertEqual(self.lan.parse_ptr(resp), "Galaxy-S23.lan")
        self.assertEqual(self.lan.short_host("Galaxy-S23.lan"), "Galaxy-S23")
        self.assertEqual(self.lan.short_host("Artems-iPhone.local."), "Artems-iPhone")
        self.assertIsNone(self.lan.parse_ptr(resp[:30]))

    def test_model_and_name_from_user_agent(self):
        app_ua = ("Mozilla/5.0 (Linux; Android 14; SM-S911B Build/UP1A; wv) Chrome/120 CrewHubApp/1.9 CrewHubDevice/abc123def"
                  " CrewHubModel/samsung%20SM-S911B CrewHubName/Galaxy%20S23%20%D0%90%D1%80%D1%82%D0%B5%D0%BC%D0%B0")
        self.assertEqual(self.lan.ua_info(app_ua), ("samsung SM-S911B", "Galaxy S23 Артема"))
        self.assertEqual(self.lan.ua_info("Mozilla/5.0 (Linux; Android 13; Pixel 7) Chrome/119")[0], "Pixel 7")
        self.assertEqual(self.lan.ua_info("Mozilla/5.0 (Linux; Android 10; K) Chrome/124")[0], "Android")
        self.assertEqual(self.lan.ua_info("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")[0], "iPhone")
        with core.app.test_request_context(headers={"User-Agent": app_ua}):
            self.assertEqual(core.device_name(), "Galaxy S23 Артема (samsung SM-S911B)")

    def fake_scan(self, seen, names=None):
        self.lan._state.update(scan_at=0)
        saved = (self.lan.subnets, self.lan.neighbours, self.lan.probe)
        import ipaddress
        self.lan.subnets = lambda: [(ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_address("192.168.1.2"))]
        self.lan.neighbours = lambda: dict(seen)
        self.lan.probe = lambda nets, ips=(): dict(names or {})
        try:
            return self.lan.scan()
        finally:
            self.lan.subnets, self.lan.neighbours, self.lan.probe = saved

    def test_all_devices_listed_and_employees_recognised(self):
        ivan = self.login("ivan")
        ivan.get("/me", headers={"X-Forwarded-For": "192.168.1.23",
                                 "User-Agent": "Mozilla/5.0 (Linux; Android 14; wv) CrewHubModel/samsung%20SM-A525F CrewHubName/Artem"})
        found = self.fake_scan({"192.168.1.23": "da:a1:19:00:00:01", "192.168.1.50": "a4:2b:b0:11:22:33",
                                "10.0.0.5": "a4:2b:b0:99:99:99"},  # чужа підмережа — не наша мережа, не показуємо
                               {"192.168.1.50": "ARTEM-PC"})
        self.assertEqual(found, 2)
        rows = {r["ip"]: r for r in self.sql("select * from lan_devices")}
        self.assertEqual((rows["192.168.1.23"]["name"], rows["192.168.1.23"]["model"]), ("Artem", "samsung SM-A525F"))
        self.assertEqual(rows["192.168.1.23"]["user_id"], self.sql("select id from users where username = 'ivan'")[0][0])
        self.assertIsNone(rows["192.168.1.50"]["user_id"])
        admin = self.login("admin", "admin123", client=core.app.test_client())
        page = admin.get("/admin?tab=live").get_data(as_text=True)
        self.assertIn("Пристрої в мережі", page)
        self.assertIn("ARTEM-PC", page)
        self.assertIn("не в CrewHub", page)
        self.assertIn(">приватний</span>", page)
        self.assertIn("Іван Петренко", page.split('id="lan"')[1])

    def test_device_keeps_name_when_ip_changes(self):
        self.fake_scan({"192.168.1.50": "a4:2b:b0:11:22:33"}, {"192.168.1.50": "ARTEM-PC"})
        self.fake_scan({"192.168.1.51": "a4:2b:b0:11:22:33", "192.168.1.50": "a4:2b:b0:44:55:66"})
        rows = {r["mac"]: r for r in self.sql("select * from lan_devices")}
        self.assertEqual((rows["a4:2b:b0:11:22:33"]["ip"], rows["a4:2b:b0:11:22:33"]["name"]), ("192.168.1.51", "ARTEM-PC"))
        self.assertIsNone(rows["a4:2b:b0:44:55:66"]["name"])

    def test_scan_can_be_switched_off(self):
        admin = self.login("admin", "admin123")
        admin.post("/admin/settings", data={"company_name": "X"})  # чекбокс не передано — вимкнено
        self.assertEqual(core.setting("lan_scan", core.connect()), "0")
        self.assertNotIn('id="lan"', admin.get("/admin?tab=live").get_data(as_text=True))

    def test_scan_button_runs_real_probe_safely(self):
        # Справжні сокети й таблиця ARP, але «мережа» — лише 127.0.0.0/30: тест нікого назовні не сканує
        import ipaddress
        admin = self.login("admin", "admin123")
        saved = (self.lan.WAIT, self.lan.subnets, self.lan.nameservers)
        self.lan.WAIT = 0.2
        self.lan.subnets = lambda: [(ipaddress.ip_network("127.0.0.0/30"), ipaddress.ip_address("127.0.0.1"))]
        self.lan.nameservers = lambda: []
        try:
            r = admin.post("/admin/lan/scan")
        finally:
            self.lan.WAIT, self.lan.subnets, self.lan.nameservers = saved
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.lan._state["error"])
        ivan = self.login("ivan", client=core.app.test_client())
        self.assertEqual(ivan.post("/admin/lan/scan").status_code, 403)

    def test_no_private_network_is_explained(self):
        saved = self.lan.subnets
        self.lan.subnets = lambda: []
        try:
            self.assertEqual(self.lan.scan(), 0)
        finally:
            self.lan.subnets = saved
        self.assertIn("не в локальній мережі", self.lan._state["error"])


class OfflineUiTest(CrewHubBase):
    """Інтерфейс не тягне нічого з інтернету: іконки, стилі, скрипти — свої. Сервер без інтернету виглядає так само."""
    PAGES = {"admin": ["/admin?tab=" + t for t in ("live", "employees", "requests", "schedule", "zones", "logs", "audit", "payroll", "settings")]
                      + ["/admin?tab=schedule&view=month", "/admin/employee/2"],
             "ivan": ["/me?tab=" + t for t in ("clock", "timesheet", "schedule", "requests", "team", "profile", "app")]}

    def pages(self):
        yield "/login", core.app.test_client().get("/login").get_data(as_text=True)
        for user, urls in self.PAGES.items():
            c = self.login(user, "admin123" if user == "admin" else "1234", client=core.app.test_client())
            for url in urls:
                r = c.get(url)
                self.assertEqual(r.status_code, 200, url)
                yield url, r.get_data(as_text=True)

    def test_no_external_resources_and_every_icon_exists(self):
        for url, html in self.pages():
            for tag in re.findall(r"<(?:script|link|img|iframe|source)\b[^>]*>", html):
                self.assertNotRegex(tag, r"(?:src|href)=[\"']?(?:https?:)?//", f"{url}: зовнішній ресурс {tag}")
            symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', html))
            used = set(re.findall(r'<use href="#i-([\w-]+)"', html))
            self.assertTrue(symbols, url)
            self.assertFalse(used - symbols, f"{url}: немає іконок {used - symbols}")

    def test_icons_set_from_javascript_exist(self):
        sprite = read(os.path.join(core.BASE_DIR, "templates", "_ui.html"))
        for name in ("wifi", "wifi-off", "sun", "moon", "left", "right"):
            self.assertIn(f'<symbol id="i-{name}"', sprite)
        for path in ("static/style.css", "static/motion.css"):
            self.assertNotRegex(read(os.path.join(core.BASE_DIR, path)), r"url\(\s*['\"]?(?:https?:)?//|@import", path)

    def test_browser_blocks_anything_external(self):
        csp = self.client.get("/login").headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)

    def test_super_animation_parts_in_sprite(self):
        sprite = read(os.path.join(core.BASE_DIR, "templates", "_ui.html"))
        css = read(os.path.join(core.BASE_DIR, "static", "motion.css"))
        for name in re.findall(r'<symbol id="i-([\w-]+)"', sprite):
            self.assertIn(f'data-anim="s-{name}"', css, f"іконка {name} без супер-анімації")


class MaintenanceTest(CrewHubBase):
    def test_busy_database_postpones_optimize_instead_of_failing(self):
        # Інший потік щойно записав — pragma optimize отримує SQLITE_BUSY без очікування. Автобекап не має «падати».
        real = core.connect

        class Busy:
            def __init__(self):
                self.c = real()

            def execute(self, q, *a):
                if q.startswith("pragma optimize"):
                    raise sqlite3.OperationalError("database is locked")
                return self.c.execute(q, *a)

            def __getattr__(self, k):
                return getattr(self.c, k)

        core.connect = Busy
        try:
            core.maintenance()  # без винятку
        finally:
            core.connect = real


if __name__ == "__main__":
    unittest.main()
