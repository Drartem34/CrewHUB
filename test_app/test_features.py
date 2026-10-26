"""Тести версії 17: папка даних, settings.ini, пристрої в мережі, інтерфейс без інтернету.
python -m unittest test_features -v"""
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

from test_crewhub import WORK_IP, CrewHubBase, core, settings_file  # noqa: F401 — test_crewhub налаштовує оточення


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

    def test_admin_change_survives_unrelated_hand_edit(self):
        # Людина правила файл (інший ключ), а адмін за ці секунди змінив правило в адмінці:
        # старе значення з файлу не має перекрити свіжу зміну
        settings_file.sync()
        self.edit_file(lambda t: t.replace("currency = грн", "currency = ₴"))
        admin = self.login("admin", "admin123")
        admin.post("/admin/settings", data={"company_name": "CrewHub Test", "late_threshold_minutes": "33", "currency": "грн"})
        with core.connect() as c:
            self.assertEqual(core.setting("late_threshold_minutes", c), "33")
            self.assertEqual(core.setting("currency", c), "₴")  # ручна правка теж застосована
        self.assertIn("late_threshold_minutes = 33", read(core.SETTINGS_FILE))

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

    def test_any_entry_point_migrates_before_creating_new_db(self):
        # profiles.py чи recover_admin.py, запущені до сервера, не мають створити порожню базу поверх перенесення
        tmp = tempfile.mkdtemp()
        code, data = os.path.join(tmp, "test_app"), os.path.join(tmp, "CrewHub-data")
        os.makedirs(code)
        legacy = os.path.join(code, "crewhub_test.db")
        c = sqlite3.connect(legacy)
        c.execute("create table marker(x)")
        c.commit()
        c.close()
        saved = {k: getattr(core, k) for k in ("BASE_DIR", "DATA_DIR", "LEGACY_DB", "DB_PATH")}
        env_db = os.environ.pop("CREWHUB_DB")
        try:
            core.BASE_DIR, core.DATA_DIR, core.LEGACY_DB = code, data, legacy
            core.DB_PATH = os.path.join(data, "crewhub.db")
            core.init_db(seed=False)
            names = [r[0] for r in sqlite3.connect(core.DB_PATH).execute("select name from sqlite_master")]
            self.assertIn("marker", names)  # стара база перенесена, а не створена нова
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

    def test_ukrainian_windows_arp_output(self):
        # Українська Windows: заголовки кирилицею в кодуванні консолі (cp1125) — не UTF-8 і не cp1251; читання не має падати
        out = ("\r\nІнтерфейс: 192.168.1.5 --- 0x7\r\n  Адреса в Інтернеті      Фізична адреса      Тип\r\n"
               "  192.168.1.1           a4-2b-b0-11-22-33     динамічний\r\n  192.168.1.23          da-a1-19-00-00-01     динамічний\r\n"
               "  Шлюз\r\n").encode("cp1125")
        for strict in ("utf-8", "cp1251"):
            with self.assertRaises(UnicodeDecodeError):
                out.decode(strict)
        done = subprocess.CompletedProcess(["arp", "-a"], 0, stdout=out, stderr=b"")
        with mock.patch.object(self.lan.os.path, "exists", return_value=False), \
                mock.patch.object(self.lan.subprocess, "run", return_value=done):
            self.assertEqual(self.lan.neighbours(), {"192.168.1.1": "a4:2b:b0:11:22:33", "192.168.1.23": "da:a1:19:00:00:01"})
        # навіть якщо таблиця сусідів зламалась зовсім, вхід працює: MAC просто невідомий
        core._mac_cache.update(at=0.0, table={})
        with mock.patch("lan.neighbours", side_effect=RuntimeError("arp зламався")):
            self.assertIsNone(core.client_mac("192.168.1.23"))
        core._mac_cache.update(at=0.0, table={})

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


class ForeignDeviceTest(CrewHubBase):
    """Хтось входить у чужий акаунт не з телефона власника — сервер позначає це підозрілим."""
    IVAN_PHONE = "Mozilla/5.0 (Linux; Android 14; wv) CrewHubApp/2.0 CrewHubDevice/ivanphone001 CrewHubModel/samsung%20SM-A525F"

    def bind(self, username, key):
        with core.connect() as c:
            uid = c.execute("select id from users where username = ?", (username,)).fetchone()[0]
            core.remember_device(c, uid, key, WORK_IP)
            c.commit()
        return uid

    def login_as(self, phone, password="1234", ua=None, device_id=None):
        c = core.app.test_client()
        data = {"username": phone, "password": password}
        if device_id:
            data["device_id"] = device_id
        r = c.post("/login", data=data, headers={"User-Agent": ua} if ua else {})
        self.assertEqual(r.status_code, 302)
        return c

    def events(self, username):
        return [tuple(r) for r in self.sql("select c.event, c.allowed from conn_log c join users u on u.id = c.user_id"
                                           " where u.username = ?", username)]

    def notes(self, who):
        return [r[0] for r in self.sql("select n.title from notifications n join users u on u.id = n.user_id where u.username = ?", who)]

    def test_login_from_colleagues_phone_is_suspicious(self):
        self.bind("ivan", "app-ivanphone001")
        self.bind("olha", "web-0aa11bb2")
        self.login_as("0937654321", ua=self.IVAN_PHONE)  # Ольгин акаунт на Івановому телефоні
        self.assertIn(("login_other_device", 0), self.events("olha"))
        self.assertIn("Ваш акаунт відкрили на чужому телефоні", self.notes("olha"))
        self.assertIn("Підозріло: чужий телефон", self.notes("moder"))
        details = self.sql("select details from conn_log where event = 'login_other_device'")[0][0]
        self.assertIn("Іван Петренко", details)
        admin = self.login("admin", "admin123", client=core.app.test_client())
        page = admin.get("/admin?tab=logs&only=bad").get_data(as_text=True)
        self.assertIn("Вхід у чужий акаунт", page)
        self.assertIn("чий телефон: Іван Петренко", page)
        live = admin.get("/admin?tab=live").get_data(as_text=True)
        self.assertIn("Вхід у чужий акаунт", live.split("Безпека сьогодні")[1])
        self.assertEqual(admin.get("/api/live").status_code, 200)

    def test_device_that_logged_into_another_account_counts_as_its(self):
        self.bind("olha", "web-0aa11bb2")
        self.login_as("0671234567", device_id="web-5555aaaa")    # Іван зі свого браузера (не прив'язаний)
        self.login_as("0937654321", device_id="web-5555aaaa")    # той самий браузер — в акаунт Ольги
        self.assertIn(("login_other_device", 0), self.events("olha"))

    def test_new_device_flagged_but_only_owner_told(self):
        self.bind("olha", "web-0aa11bb2")
        self.login_as("0937654321", device_id="web-77778888")
        self.assertIn(("login_new_device", 0), self.events("olha"))
        self.assertIn("Вхід з нового пристрою", self.notes("olha"))
        self.assertNotIn("Підозріло: чужий телефон", self.notes("moder"))

    def test_own_device_and_app_after_browser_are_fine(self):
        self.bind("olha", "web-0aa11bb2")
        self.login_as("0937654321", device_id="web-0aa11bb2")
        self.login_as("0937654321", ua="Mozilla/5.0 (Linux; Android 14) CrewHubApp/2.0 CrewHubDevice/olhaphone0001")
        self.login_as("0671234567", device_id="web-12121212")    # у Івана ще немає свого телефона — нема з чим звіряти
        self.assertEqual(self.sql("select count(*) from conn_log where allowed = 0")[0][0], 0)

    def test_repeated_login_notifies_once(self):
        self.bind("ivan", "app-ivanphone001")
        self.bind("olha", "web-0aa11bb2")
        for _ in range(3):
            self.login_as("0937654321", ua=self.IVAN_PHONE)
        self.assertEqual(self.sql("select count(*) from conn_log where event = 'login_other_device'")[0][0], 3)
        self.assertEqual(self.notes("olha").count("Ваш акаунт відкрили на чужому телефоні"), 1)

    def test_shift_started_from_colleagues_phone(self):
        self.bind("ivan", "app-ivanphone001")
        self.bind("olha", "web-0aa11bb2")
        olha = self.login_as("0937654321", device_id="web-0aa11bb2")
        r = olha.post("/api/clock/start", json={}, headers={"X-Forwarded-For": WORK_IP, "User-Agent": self.IVAN_PHONE})
        self.assertTrue(r.get_json()["ok"])  # зміну не блокуємо (могла позичити телефон), але позначаємо
        self.assertIn(("shift_other_device", 0), self.events("olha"))
        self.assertIn("Підозріло: чужий телефон", self.notes("admin"))

    # ── Відбиток пристрою і MAC ──
    FP_OLHA, FP_IVAN = "0123456789abcdef", "fedcba9876543210"
    MAC_OLHA, MAC_IVAN = "a2:11:22:33:44:55", "5e:66:77:88:99:00"

    def login_mac(self, phone, mac, **kw):
        with mock.patch.object(core, "client_mac", lambda ip, cached_only=False: mac):
            fp = kw.pop("fp", None)
            c = core.app.test_client()
            data = {"username": phone, "password": "1234", "device_id": kw.get("device_id", ""), "fp": fp or ""}
            self.assertEqual(c.post("/login", data=data).status_code, 302)
            return c

    def mark(self, username, key, fp, mac):
        uid = self.bind(username, key)
        with core.connect() as c:
            core.note_device(c, uid, key, fp, mac)
            c.commit()

    def test_fingerprint_and_mac_saved_for_own_device(self):
        self.bind("olha", "web-0aa11bb2")
        self.login_mac("0937654321", self.MAC_OLHA, device_id="web-0aa11bb2", fp=self.FP_OLHA)
        self.assertEqual(tuple(self.sql("select fingerprint, mac from devices where device_key = 'web-0aa11bb2'")[0]),
                         (self.FP_OLHA, self.MAC_OLHA))
        self.assertEqual(tuple(self.sql("select fingerprint, mac from remember_tokens order by id desc limit 1")[0]),
                         (self.FP_OLHA, self.MAC_OLHA))
        admin = self.login("admin", "admin123", client=core.app.test_client())
        uid = self.sql("select id from users where username = 'olha'")[0][0]
        page = admin.get(f"/admin/employee/{uid}").get_data(as_text=True)
        self.assertIn("MAC " + self.MAC_OLHA, page)
        self.assertIn("відбиток " + self.FP_OLHA, page)

    def test_same_phone_with_cleared_browser_is_recognised(self):
        # Ольга очистила браузер: код пристрою новий, але MAC і відбиток ті самі — це її телефон
        self.mark("olha", "web-0aa11bb2", self.FP_OLHA, self.MAC_OLHA)
        self.login_mac("0937654321", self.MAC_OLHA, device_id="web-99990000", fp=self.FP_OLHA)
        self.assertEqual(self.sql("select count(*) from conn_log where allowed = 0")[0][0], 0)
        # прив'язка переїхала на новий код — автозміна впізнає телефон і далі
        self.assertEqual([r[0] for r in self.sql("select d.device_key from devices d join users u on u.id = d.user_id"
                                                  " where u.username = 'olha'")], ["web-99990000"])
        self.assertIn(("device_recognised", 1), self.events("olha"))
        # застосунок на тому самому телефоні не забирає прив'язку браузера (інший вид пристрою)
        self.login_mac("0937654321", self.MAC_OLHA, device_id="app-x", fp=self.FP_OLHA)
        self.assertEqual(self.sql("select count(*) from devices where device_key like 'app-%'")[0][0], 0)

    def test_colleagues_mac_means_colleagues_phone(self):
        # Іванів телефон з новим кодом (очистили браузер) — MAC видає, чий це телефон
        self.mark("ivan", "app-ivanphone001", self.FP_IVAN, self.MAC_IVAN)
        self.bind("olha", "web-0aa11bb2")
        self.login_mac("0937654321", self.MAC_IVAN, device_id="web-31313131")
        self.assertIn(("login_other_device", 0), self.events("olha"))
        details = self.sql("select details from conn_log where event = 'login_other_device'")[0][0]
        self.assertIn("Іван Петренко", details)
        self.assertIn(self.MAC_IVAN, details)

    def test_shared_mac_behind_extender_is_not_an_accusation(self):
        # Підсилювач Wi-Fi підставляє свій MAC за всіх: MAC той самий, відбитки різні — це не Іванів телефон
        self.mark("ivan", "app-ivanphone001", self.FP_IVAN, self.MAC_IVAN)
        self.bind("olha", "web-0aa11bb2")
        self.login_mac("0937654321", self.MAC_IVAN, device_id="web-31313131", fp=self.FP_OLHA)
        self.assertNotIn(("login_other_device", 0), self.events("olha"))
        self.assertIn(("login_new_device", 0), self.events("olha"))

    def test_fingerprint_alone_is_only_a_hint(self):
        # Однакові телефони мають однаковий відбиток: без MAC це не «чужий телефон», а лише новий пристрій з підказкою
        self.mark("ivan", "app-ivanphone001", self.FP_IVAN, self.MAC_IVAN)
        self.mark("olha", "web-0aa11bb2", self.FP_OLHA, self.MAC_OLHA)
        self.login_mac("0937654321", None, device_id="web-42424242", fp=self.FP_IVAN)
        self.assertIn(("login_new_device", 0), self.events("olha"))
        self.assertNotIn(("login_other_device", 0), self.events("olha"))
        self.assertIn("схожий відбиток", self.sql("select details from conn_log where event = 'login_new_device'")[0][0])
        # той самий відбиток, але інший MAC — теж не впізнаємо як свій
        self.login_mac("0937654321", "02:00:00:00:00:01", device_id="web-42424243", fp=self.FP_OLHA)
        self.assertEqual(self.events("olha").count(("login_new_device", 0)), 2)

    def test_clock_ping_carries_fingerprint(self):
        self.bind("olha", "web-0aa11bb2")
        olha = self.login_as("0937654321", device_id="web-0aa11bb2")
        with mock.patch.object(core, "client_mac", lambda ip, cached_only=False: self.MAC_OLHA):
            olha.post("/api/clock/heartbeat", json={"device_id": "web-0aa11bb2", "fp": self.FP_OLHA.upper()},
                      headers={"X-Forwarded-For": WORK_IP})
        self.assertEqual(tuple(self.sql("select fingerprint, mac from devices where device_key = 'web-0aa11bb2'")[0]),
                         (self.FP_OLHA, self.MAC_OLHA))

    def test_clean_fp_and_client_mac(self):
        self.assertEqual(core.clean_fp(" ABCDEF0123456789 "), "abcdef0123456789")
        for bad in (None, "", "xyz", "12'; drop", "a" * 40):
            self.assertIsNone(core.clean_fp(bad))
        self.assertIsNone(core.client_mac("8.8.8.8"))      # через інтернет MAC не видно
        self.assertIsNone(core.client_mac("127.0.0.1"))
        self.assertIsNone(core.client_mac("не ip"))
        core._mac_cache.update(at=0.0, table={})
        with mock.patch("lan.neighbours", return_value={"192.168.1.23": self.MAC_OLHA}):
            self.assertEqual(core.client_mac("192.168.1.23"), self.MAC_OLHA)
        self.assertEqual(core.client_mac("192.168.1.23", cached_only=True), self.MAC_OLHA)
        core._mac_cache.update(at=0.0, table={})

    def test_hello_proves_server_knows_the_token(self):
        # Застосунок шукає сервер на новому IP і віддає токен лише тому, хто доведе, що знає його хеш
        import hashlib
        import hmac
        c = self.login_as("0937654321", device_id="web-0aa11bb2")
        token = c.get_cookie("crewhub_remember").value
        sel, validator = token.split(":", 1)
        nonce = "0123456789abcdef0123456789abcdef"
        hello = core.app.test_client().get(f"/api/hello?n={nonce}&s={sel}").get_json()
        key = hashlib.sha256(validator.encode()).hexdigest().encode()
        self.assertEqual(hello["proof"], hmac.new(key, f"{nonce}:{hello['server_id']}".encode(), hashlib.sha256).hexdigest())
        self.assertNotIn("proof", core.app.test_client().get(f"/api/hello?n={nonce}&s=unknownselector").get_json())
        self.assertNotIn("proof", core.app.test_client().get(f"/api/hello?n=xyz&s={sel}").get_json())
        self.assertNotIn("proof", core.app.test_client().get("/api/hello").get_json())
        # з токеном, виданим для старої адреси, сервер на новій адресі впускає без пароля
        other = core.app.test_client()
        other.set_cookie("crewhub_remember", token, domain="192.168.0.77")
        r = other.get("/me", base_url="http://192.168.0.77:5000")
        self.assertEqual(r.status_code, 200)

    def test_log_details_readable(self):
        self.assertEqual(core.log_details('{"reason": "other_device"}'), "причина: інший телефон")
        self.assertEqual(core.log_details("не json"), "не json")
        self.assertEqual(core.log_details(None), "")


class PresenceTest(CrewHubBase):
    """Браузер на телефоні з вимкненим екраном не пінгує, але телефон у Wi-Fi: сервер бачить його MAC сам."""
    MAC = "da:a1:19:00:00:01"
    APP_UA = "Mozilla/5.0 (Linux; Android 14; wv) CrewHubApp/2.2 CrewHubDevice/ivanphone001"

    def start(self, headers=None):
        self.login("ivan")
        with mock.patch.object(core, "client_mac", lambda ip, cached_only=False: self.MAC):
            r = self.client.post("/api/clock/start", json={"device_id": "web-0aa11bb2"},
                                 headers={"X-Forwarded-For": WORK_IP, **(headers or {})})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.sql("select mac from shifts")[0][0], self.MAC)  # MAC з мережі точки запам'ятовано

    def silent(self, sec):
        with core.connect() as c:
            c.execute("update shifts set last_seen_at = last_seen_at - ?", (sec,))
            c.commit()

    def step(self, seen):
        with mock.patch("lan.present", return_value=seen) as p:
            core.presence_step()
        return p

    def sweep(self):
        with core.connect() as c:
            n = core.sweep(c)
            c.commit()
        return n

    def status(self):
        return self.sql("select status from shifts")[0][0]

    def test_sleeping_browser_phone_in_wifi_keeps_shift(self):
        self.start()
        self.silent(300)  # екран вимкнено 5 хв — браузер мовчить
        p = self.step({WORK_IP: self.MAC})
        p.assert_called_once_with([WORK_IP])
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.status(), "active")
        self.assertLess(core.now() - self.sql("select last_seen_at from shifts")[0][0], 5)

    def test_phone_left_wifi_pauses_and_comes_back(self):
        self.start()
        self.silent(300)
        self.step({})  # телефон не відповів — його немає в мережі
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.status(), "paused")
        self.step({WORK_IP: self.MAC})  # повернувся в Wi-Fi, сторінку не відкривав
        self.assertEqual(self.status(), "active")
        self.assertEqual(self.sql("select count(*) from breaks where kind = 'wifi_loss' and ended_at is not null")[0][0], 1)

    def test_other_phone_or_other_network_does_not_count(self):
        self.start()
        self.silent(300)
        self.step({WORK_IP: "02:00:00:00:00:99"})   # за цим IP тепер інший телефон
        self.step({"192.168.50.7": self.MAC})       # той самий телефон, але не в мережі точки
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.status(), "paused")

    def test_nothing_to_check(self):
        self.start()
        self.step({}).assert_not_called()        # щойно пінгував — перевіряти нема чого
        self.silent(300)
        with core.connect() as c:
            c.execute("insert or replace into settings(key, value) values ('lan_presence', '0')")
            c.commit()
        self.step({WORK_IP: self.MAC}).assert_not_called()  # вимкнено в «Правилах»

    def test_app_pause_is_left_to_the_app(self):
        self.start(headers={"User-Agent": self.APP_UA})
        with core.connect() as c:
            s = c.execute("select * from shifts").fetchone()
            core.start_wifi_pause(c, s, core.now() - 120, "left")  # застосунок повідомив: телефон вийшов
            c.commit()
        self.step({WORK_IP: self.MAC}).assert_not_called()
        self.assertEqual(self.status(), "paused")

    def test_phone_in_wifi_starts_shift_without_app_or_page(self):
        real_now = core.now
        wed10 = int(datetime(2026, 10, 21, 10, 0).timestamp())  # середа, робочий час 09:00–18:00
        core.now = lambda: wed10
        try:
            with core.connect() as c:
                c.execute("update settings set value = '1' where key = 'auto_start'")
                uid = c.execute("select id from users where username = 'ivan'").fetchone()[0]
                core.remember_device(c, uid, "web-0aa11bb2", WORK_IP)
                core.note_device(c, uid, "web-0aa11bb2", None, self.MAC)
                c.commit()
            p = self.step({WORK_IP: self.MAC})  # телефон у Wi-Fi точки, нічого не відкривали
            p.assert_called_once_with([WORK_IP])
            row = self.sql("select status, note, mac, device from shifts where user_id = ?", uid)[0]
            self.assertEqual(tuple(row), ("active", "Автостарт по Wi-Fi", self.MAC, "web-0aa11bb2"))
            self.assertIn("Зміну розпочато автоматично", [r[0] for r in self.sql("select title from notifications where user_id = ?", uid)])
            core.now = lambda: wed10 + 300  # телефон пішов з мережі
            self.step({})
            with core.connect() as c:
                core.meta_set(c, "alive_at", core.now() - 5)
                core.sweep(c)
                c.commit()
            self.assertEqual(self.status(), "paused")
            # поза робочим часом сервер навіть не стукає
            with core.connect() as c:
                c.execute("delete from breaks")
                c.execute("delete from shifts")
                c.commit()
            core.now = lambda: int(datetime(2026, 10, 21, 21, 0).timestamp())
            self.step({WORK_IP: self.MAC}).assert_not_called()
            self.assertEqual(self.sql("select count(*) from shifts")[0][0], 0)
        finally:
            core.now = real_now

    def test_present_reads_only_fresh_entries(self):
        lan = __import__("lan")
        neigh = ("192.168.1.23 dev wlan0 lladdr da:a1:19:00:00:01 REACHABLE\n"
                 "192.168.1.40 dev wlan0 lladdr 5e:66:77:88:99:00 STALE\n"
                 "192.168.1.41 dev wlan0  FAILED\n")
        self.assertEqual(lan.parse_ip_neigh(neigh)["192.168.1.40"], ("5e:66:77:88:99:00", "STALE"))
        win = "192.168.1.23 DA-A1-19-00-00-01 Reachable\r\n192.168.1.40 5E-66-77-88-99-00 Stale\r\n192.168.1.41 00-00-00-00-00-00 Unreachable\r\n"
        self.assertEqual(lan.parse_net_neighbor(win), {"192.168.1.23": ("da:a1:19:00:00:01", "REACHABLE"),
                                                        "192.168.1.40": ("5e:66:77:88:99:00", "STALE")})
        with mock.patch.object(lan, "knock") as knock, mock.patch.object(lan, "neighbour_states", return_value=lan.parse_ip_neigh(neigh)):
            self.assertEqual(lan.present(["192.168.1.23", "192.168.1.40"], wait=0), {"192.168.1.23": "da:a1:19:00:00:01"})
        knock.assert_called_once_with(["192.168.1.23", "192.168.1.40"])
        # система не дає станів (macOS) — уся таблиця
        with mock.patch.object(lan, "knock"), mock.patch.object(lan, "neighbour_states", return_value=None), \
                mock.patch.object(lan, "neighbours", return_value={"192.168.1.40": "5e:66:77:88:99:00"}):
            self.assertEqual(lan.present(["192.168.1.40"], wait=0), {"192.168.1.40": "5e:66:77:88:99:00"})


class BatteryTest(CrewHubBase):
    """Телефон розрядився — людина нікуди не ділась: зміна не стає на паузу і не закривається одразу."""
    WED10 = int(datetime(2026, 10, 21, 10, 0).timestamp())  # середа, робочий час 09:00–18:00

    def setUp(self):
        super().setUp()
        self._real_now = core.now
        core.now = lambda: self.WED10
        self.addCleanup(setattr, core, "now", self._real_now)

    def start(self, **battery):
        self.login("ivan")
        self.assertTrue(self.clock("start", device_id="web-0aa11bb2", **battery)["ok"])

    def silent(self, sec):
        with core.connect() as c:
            c.execute("update shifts set started_at = started_at - ? - 600, last_seen_at = last_seen_at - ?,"
                      " battery_at = battery_at - ?", (sec, sec, sec))
            c.commit()

    def sweep(self):
        with core.connect() as c:
            core.meta_set(c, "alive_at", core.now() - 5)
            n = core.sweep(c)
            c.commit()
        return n

    def shift(self):
        return self.sql("select * from shifts")[0]

    def test_dead_phone_keeps_shift_counting(self):
        self.start(battery=4, charging=False)
        self.assertEqual((self.shift()["battery"], self.shift()["charging"]), (4, 0))
        self.silent(600)  # 10 хв тиші після 4 %
        self.sweep()
        self.assertEqual(self.shift()["status"], "active")
        self.assertIsNotNone(self.shift()["power_lost_at"])
        self.assertIn(("battery_dead",), [tuple(r) for r in self.sql("select event from conn_log")])
        self.assertIn("Телефон розрядився", [r[0] for r in self.sql("select title from notifications")])
        self.sweep()  # повторно не пишемо
        self.assertEqual(self.sql("select count(*) from conn_log where event = 'battery_dead'")[0][0], 1)
        admin = self.login("admin", "admin123", client=core.app.test_client())
        row = [r for r in admin.get("/api/live").get_json()["rows"] if r["name"] == "Іван Петренко"][0]
        self.assertTrue(row["power_lost"])
        # зарядив і відкрив сторінку — далі як завжди
        self.assertTrue(self.clock("heartbeat", device_id="web-0aa11bb2", battery=31, charging=True)["ok"])
        self.assertEqual((self.shift()["power_lost_at"], self.shift()["battery"], self.shift()["status"]), (None, 31, "active"))

    def test_dead_phone_shift_closes_after_limit(self):
        self.start(battery=2)
        with core.connect() as c:
            c.execute("update settings set value = '60' where key = 'battery_keep_minutes'")
            c.commit()
        self.silent(2 * 3600)
        last = self.shift()["last_seen_at"]
        self.sweep()
        s = self.shift()
        self.assertEqual(s["status"], "closed")
        self.assertLessEqual(s["ended_at"], last + 3600)
        self.assertIn("battery_dead", self.sql("select details from conn_log where event = 'auto_close'")[0][0])

    def test_dead_after_work_hours_closes_at_work_end(self):
        core.now = lambda: self.WED10 + 7 * 3600 + 1800  # 17:30
        self.start(battery=3)
        core.now = lambda: self.WED10 + 9 * 3600  # 19:00, телефон мовчить з 17:30
        self.sweep()
        s = self.shift()
        self.assertEqual((s["status"], s["ended_at"]), ("closed", self.WED10 + 8 * 3600))  # кінець робочого часу 18:00

    def test_not_dead_means_normal_pause(self):
        for battery, charging, extra in ((3, True, 0), (40, False, 0), (3, False, 3600)):
            with self.subTest(battery=battery, charging=charging, stale=extra):
                with core.connect() as c:
                    c.execute("delete from breaks")
                    c.execute("delete from shifts")
                    c.commit()
                self.start(battery=battery, charging=charging)
                if extra:  # заряд повідомлено задовго до останнього зв'язку — його могли зарядити
                    with core.connect() as c:
                        c.execute("update shifts set battery_at = battery_at - ?", (extra,))
                        c.commit()
                self.silent(600)
                self.sweep()
                self.assertEqual(self.shift()["status"], "paused")

    def test_setting_off_and_bad_values(self):
        self.start(battery="багато")  # сміття замість числа — ігноруємо
        self.assertIsNone(self.shift()["battery"])
        self.assertTrue(self.clock("heartbeat", device_id="web-0aa11bb2", battery=150)["ok"])
        self.assertIsNone(self.shift()["battery"])
        self.assertTrue(self.clock("heartbeat", device_id="web-0aa11bb2", battery=1, shutdown=True)["ok"])  # застосунок вимикається
        self.assertEqual(self.shift()["battery"], 1)
        with core.connect() as c:
            c.execute("update settings set value = '0' where key = 'battery_dead_pct'")
            c.commit()
        self.silent(600)
        self.sweep()
        self.assertEqual(self.shift()["status"], "paused")


if __name__ == "__main__":
    unittest.main()
