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


if __name__ == "__main__":
    unittest.main()
