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


if __name__ == "__main__":
    unittest.main()
