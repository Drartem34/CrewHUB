"""Знімки сторінок CrewHub для демо-застосунку (без сервера).

Запускає сервер у цьому ж процесі з тестовими даними (CREWHUB_DEMO=1) у тимчасовій папці, відкриває всі сторінки
від імені працівника (Іван) і адміністратора, переписує посилання між ними на збережені файли й складає все в
assets/www: index.html (вхід), p/e/*.html (працівник), p/a/*.html (адмін), static/ (стилі, скрипти, іконка),
demo/ (demo.js, дані «Огляду»). Справжні дані з робочої бази сюди не потрапляють.

    python3 build_demo.py            (потрібен Flask, як для сервера)
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.normpath(os.path.join(HERE, "..", "..", "test_app"))
OUT = os.path.join(HERE, "assets", "www")
TMP = tempfile.mkdtemp(prefix="crewhub-demo-")
os.environ.update(CREWHUB_DATA=TMP, CREWHUB_DEMO="1", CREWHUB_CSRF="0", CREWHUB_LAN_NETS="192.168.1.0/30")
sys.path.insert(0, SERVER)

import crewhub_core as core  # noqa: E402
import lan  # noqa: E402

PHONE_IP = "192.168.1.23"  # «телефон» у робочій мережі точки
ROLES = {"e": ("0671234567", "1234", "/me?tab=clock"), "a": ("0500000001", "admin123", "/admin?tab=live")}
MAX_PAGES = 120


def key(url, role):
    """Адреса сторінки сервера → ім'я збереженого файлу (лише літери, цифри, «_» — так їх упізнає nav.js), або None."""
    u = urlsplit(url)
    if u.scheme or u.netloc:
        return None
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    extra = set(q) - {"tab", "view", "archive", "only"}
    if extra:
        return None  # інший місяць, тиждень тощо — у демо лише поточні
    suffix = ("_month" if q.get("view") == "month" else "") + ("_archive" if q.get("archive") == "1" else "") \
        + ("_bad" if q.get("only") == "bad" else "")
    if u.path == "/":
        return "me_clock" if role == "e" else "live"
    if u.path == "/me":
        return "me_" + re.sub(r"\W", "", q.get("tab", "clock")) + suffix
    if u.path == "/admin" and role == "a":
        return re.sub(r"\W", "", q.get("tab", "live")) + suffix
    if (m := re.fullmatch(r"/admin/employee/(\d+)", u.path)) and role == "a":
        return f"emp_{m[1]}"
    return None


def prepare():
    core.init_db()
    with core.connect() as c:
        c.execute("update locations set name = 'Центр', address = 'вул. Хрещатик, 1' where id = 1")
        c.execute("update networks set cidr = '192.168.1.0/24' where location_id = 1")
        c.execute("update settings set value = 'CrewHub Демо' where key = 'company_name'")
        t = int(time.time())
        for mac, ip, name, model, vendor, uid in [
                ("da:a1:19:00:00:01", PHONE_IP, "Galaxy A52 Івана", "samsung SM-A525F", None, 3),
                ("a4:2b:b0:11:22:33", "192.168.1.50", "KASA-PC", "Windows ПК", None, None),
                ("3e:22:fa:10:00:07", "192.168.1.61", "Olenas-iPhone", "iPhone", None, None),
                ("6a:00:11:22:33:44", "192.168.1.77", None, None, None, None)]:
            c.execute("insert or replace into lan_devices values (?, ?, ?, ?, ?, ?, ?, ?)", (mac, ip, name, model, vendor, uid, t - 86400, t))
        c.commit()
    lan._state.update(scan_at=int(time.time()), took=2.1, error=None, nets=["192.168.1.0/24"])


def snapshot():
    pages = {}
    live_json = None
    for role, (user, pw, start) in ROLES.items():
        client = core.app.test_client()
        env = {"REMOTE_ADDR": PHONE_IP}
        r = client.post("/login", data={"username": user, "password": pw}, environ_base=env)
        assert r.status_code == 302, f"вхід {user}: {r.status_code}"
        queue, seen = [start], set()
        while queue and len(seen) < MAX_PAGES:
            url = queue.pop(0)
            k = key(url, role)
            if not k or k in seen:
                continue
            seen.add(k)
            r = client.get(url, environ_base=env)
            if r.status_code != 200 or "text/html" not in r.content_type:
                continue
            html = r.get_data(as_text=True)
            pages[(role, k)] = html
            for href in re.findall(r'href="(/[^"#]*)"', html):
                href = href.replace("&amp;", "&")
                if key(href, role) and key(href, role) not in seen:
                    queue.append(href)
        if role == "a":
            live_json = client.get("/api/live", environ_base=env).get_data(as_text=True)
    guest = core.app.test_client().get("/login", environ_base={"REMOTE_ADDR": PHONE_IP}).get_data(as_text=True)
    return pages, guest, live_json


def rewrite(html, role, have):
    def fix(m):
        url = m[2].replace("&amp;", "&")
        k = key(url, role) if role else None
        return f'{m[1]}="/p/{role}/{k}.html"' if k and (role, k) in have else m[0]
    html = re.sub(r'(href)="(/[^"#]*)"', fix, html)
    note = ("Демо-версія: можна все подивитися й понатискати, але нічого не зберігається."
            + (" Зміну можна почати й закрити — вона рахується на цьому телефоні." if role == "e" else ""))
    banner = f'<div class="banner info demo-note" style="margin-bottom:14px">{note}</div>'
    html = html.replace('<main class="content">', '<main class="content">' + banner, 1)
    html = html.replace('<div class="card login-card">', '<div class="card login-card">' + banner, 1)
    # Демо-скрипт — першим: він має підмінити запити до сервера ще до того, як їх зроблять сторінки
    return html.replace("<head>", '<head>\n  <script src="/demo/demo.js"></script>', 1)


def main():
    prepare()
    pages, guest, live_json = snapshot()
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "static"))
    for f in os.listdir(os.path.join(SERVER, "static")):
        src = os.path.join(SERVER, "static", f)
        if os.path.isfile(src) and f.endswith((".css", ".js", ".svg", ".json")):
            shutil.copy(src, os.path.join(OUT, "static", f))
    os.makedirs(os.path.join(OUT, "demo"))
    shutil.copy(os.path.join(HERE, "demo.js"), os.path.join(OUT, "demo", "demo.js"))
    with open(os.path.join(OUT, "demo", "live.json"), "w", encoding="utf-8") as f:
        f.write(live_json or "{}")
    have = set(pages)
    for (role, k), html in pages.items():
        path = os.path.join(OUT, "p", role, k + ".html")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(rewrite(html, role, have))
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(rewrite(guest, None, have))
    with open(os.path.join(OUT, "demo", "pages.json"), "w", encoding="utf-8") as f:
        json.dump(sorted(f"/p/{r}/{k}.html" for r, k in have), f)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"Демо: {len(pages)} сторінок → {OUT}")


if __name__ == "__main__":
    main()
