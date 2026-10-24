"""Тести на виправлені дірки й баги (по одному на кожен пункт аудиту): python -m unittest test_hardening -v"""
import unittest

from test_crewhub import HOME_IP, WORK_IP, CrewHubBase, core


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


if __name__ == "__main__":
    unittest.main()
