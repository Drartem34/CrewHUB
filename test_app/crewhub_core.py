"""CrewHub (тестова версія): облік змін з перевіркою робочого Wi-Fi за IP.

Один файл логіки: база SQLite, годинник зміни, контроль Wi-Fi, табелі, запити,
журнали аудиту, payroll. Сторінки лежать у templates/.
"""
import base64
import csv
import hashlib
import io
import ipaddress
import json
import os
import random
import re
import secrets
import socket
import sqlite3
import zipfile
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlencode, urlsplit

from flask import (Flask, Response, abort, flash, g, has_request_context, jsonify, redirect, render_template, send_file,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CREWHUB_DB", os.path.join(BASE_DIR, "crewhub_test.db"))
# Через тунель (Pinggy, ngrok) справжній IP клієнта лежить у X-Forwarded-For. Заголовку віримо лише тоді,
# коли його дописує наш тунель (start.py вмикає це сам для pinggy з xff, або CREWHUB_TRUST_PROXY=1),
# і лише для з'єднань з цього ж комп'ютера. Беремо ОСТАННЮ адресу: її дописав тунель, усе лівіше
# міг написати сам клієнт. Телефон у Wi-Fi не зможе підробити свій IP, дописавши заголовок.
TRUST_PROXY = os.environ.get("CREWHUB_TRUST_PROXY", "0") == "1"

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))
app.secret_key = os.environ.get("CREWHUB_SECRET", "crewhub-test-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # фото до 5 МБ
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)  # телефон не перелогінюється щоранку
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400  # css/js кешуються телефоном (версія в ?v=)
# Захист від CSRF: кука сесії не їде на чужі сайти; явно, не покладаючись на дефолт браузера.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
CSRF_ON = os.environ.get("CREWHUB_CSRF", "1") != "0"  # вимикається лише в автотестах
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
APK_PATH = os.path.join(BASE_DIR, "static", "CrewHub.apk")  # Android-застосунок, роздається з сервера

DEFAULT_SETTINGS = {
    "company_name": "CrewHub Test",
    "currency": "грн",
    "wifi_grace_minutes": "2",       # скільки хвилин без пінгу з робочої мережі допускається (браузер)
    "sleep_grace_minutes": "20",     # те саме для застосунку: телефон, який ніхто не чіпає, засинає і пінгує рідше
    "silence_forgive_minutes": "45",  # застосунок «мовчав» (телефон спав), але знову озвався з робочого Wi-Fi —
                                     # цей час рахується. Лише для застосунку: браузер не спить
    "wifi_loss_action": "pause",     # pause | close
    "offline_keeps_counting": "0",   # 1 = втрата зв'язку не ставить зміну на паузу: вона рахується далі до кінця робочого часу
    "wifi_pause_close_minutes": "30",
    "heartbeat_seconds": "20",
    "lunch_enabled": "1",            # показувати працівникам кнопку «Обід»
    "lunch_is_paid": "0",            # 1 = обід не ставить лічильник на паузу
    "lunch_rate_pct": "100",         # оплата обіду у % від ставки (якщо оплачується)
    "rest_enabled": "1",
    "rest_is_paid": "1",
    "rest_rate_pct": "100",
    "weekend_multiplier": "2",       # множник оплати за суботу і неділю (1 = вимкнено)
    "night_multiplier": "3",         # множник за нічні години (1 = вимкнено)
    "night_start": "22:00",
    "night_end": "06:00",
    "sick_auto_approve": "1",
    "auto_start": "1",
    "device_lock": "1",               # зміна починається сама, коли телефон з'являється в робочому Wi-Fi
    "work_start": "09:00",           # звичайні години зміни (якщо в «Графіку» на цей день нічого немає)
    "work_end": "18:00",             # кінець раніше за початок = нічна зміна через північ
    "work_weekends": "0",            # автостарт і в суботу/неділю
    "auto_early_minutes": "30",      # прийшов раніше: зміна все одно рахується з початку робочого часу
    "auto_close_minutes": "90",      # після кінця зміни: стільки без робочого Wi-Fi — зміна закривається
        # лікарняний діє одразу, дей-офф і відпустку погоджує модератор
    "late_threshold_minutes": "10",
    "max_shift_hours": "16",
    "default_hourly_rate": "100",    # загальна ставка: для нових працівників і масового оновлення
    "dayoff_per_day": "1",           # скільки людей максимум можуть мати дей-офф в один день (0 = без обмеження)
    "dayoff_per_month": "1",         # скільки дей-оффів на людину за місяць (0 = без обмеження)
    "dayoff_paid": "1",              # дей-офф оплачується як повний робочий день за ставкою людини
    "registration_open": "1",        # чи можна зареєструватися самому (акаунт чекає підтвердження)
}

ROLES = {"employee": "Працівник", "teamlead": "Тімлід", "moderator": "Модератор", "admin": "Адміністратор"}
STAFF_ROLES = ("moderator", "admin")                # повне керування (вкладки «Керування»)
DAYOFF_ROLES = ("teamlead", "moderator", "admin")   # дають і редагують дей-оффи
DAYOFF_PICK_DAYS = 3                                # стільки днів після дозволу людина може обрати день
STATUSES = {"active": "Активний", "vacation": "У відпустці", "sick_leave": "На лікарняному", "dismissed": "Звільнений",
            "pending": "Чекає підтвердження"}
REQUEST_TYPES = {"day_off": "Дей-офф", "vacation": "Відпустка", "sick_leave": "Лікарняний",
                 "shift_swap": "Обмін змінами", "time_edit": "Редагування часу",
                 "shift_add": "Пропущена зміна"}
REQUEST_STATUSES = {"pending": "Очікує", "approved": "Схвалено", "rejected": "Відхилено", "cancelled": "Скасовано",
                    "granted": "Оберіть день", "expired": "Строк минув"}
ABSENCE_TYPES = ("day_off", "vacation", "sick_leave")
LIVE_STATES = ("on_site", "on_break", "wifi_lost", "idle_wifi", "late", "scheduled", "absent", "off")
EVENTS = {"start": "Почав зміну", "stop": "Закрив зміну", "break_start": "Почав перерву",
          "break_end": "Завершив перерву", "heartbeat_foreign_ip": "Пінг з чужої мережі",
          "wifi_lost": "Втрата Wi-Fi", "wifi_restored": "Wi-Fi відновлено", "denied": "Відмова",
          "server_downtime": "Сервер не працював", "auto_start": "Зміна почалась автоматично",
          "auto_close": "Зміна закрилась автоматично"}

SCHEMA = """
create table if not exists settings (key text primary key, value text not null);
create table if not exists locations (id integer primary key, name text not null unique, address text,
  is_active integer not null default 1, wifi_ssid text, wifi_password text);
create table if not exists networks (id integer primary key, location_id integer not null references locations(id) on delete cascade,
  cidr text not null, label text, unique(location_id, cidr));
create table if not exists users (id integer primary key, username text not null unique, password_hash text not null,
  full_name text not null, role text not null default 'employee', job_title text, department text,
  location_id integer references locations(id), phone text, telegram text, whatsapp text, photo_url text,
  hourly_rate real not null default 0, hire_date text, status text not null default 'active',
  must_change_password integer not null default 0);
create table if not exists schedule (id integer primary key, user_id integer not null references users(id) on delete cascade,
  location_id integer references locations(id), starts_at integer not null, ends_at integer not null, note text);
create table if not exists shifts (id integer primary key, user_id integer not null references users(id) on delete cascade,
  location_id integer references locations(id), started_at integer not null, ended_at integer,
  status text not null default 'active', close_reason text, hourly_rate real not null,
  last_seen_at integer not null, last_seen_ip text, start_ip text, end_ip text, device text,
  is_edited integer not null default 0, note text, bg_ok integer not null default 0);
create table if not exists breaks (id integer primary key, shift_id integer not null references shifts(id) on delete cascade,
  kind text not null, is_paid integer not null, started_at integer not null, ended_at integer,
  rate_pct real not null default 100, reason text);
create table if not exists adjustments (id integer primary key, user_id integer not null references users(id) on delete cascade,
  amount real not null, reason text not null, day text not null, created_by integer, created_at integer not null);
create table if not exists requests (id integer primary key, user_id integer not null references users(id) on delete cascade,
  type text not null, status text not null default 'pending', starts_on text, ends_on text,
  shift_id integer references shifts(id) on delete cascade, proposed_start integer, proposed_end integer,
  schedule_id integer references schedule(id) on delete cascade, swap_with_id integer references users(id),
  swap_accepted integer, comment text, reviewer_id integer, review_comment text, created_at integer not null,
  grant_until integer);
create table if not exists conn_log (id integer primary key, user_id integer, shift_id integer, event text not null,
  ip text, location_id integer, allowed integer not null, device text, details text, created_at integer not null);
create table if not exists audit (id integer primary key, actor_id integer, action text not null, entity text not null,
  entity_id integer, target_user_id integer, changes text, created_at integer not null);
create table if not exists notifications (id integer primary key, user_id integer not null, title text not null,
  body text, created_at integer not null, is_read integer not null default 0);
create table if not exists meta (key text primary key, value text not null);
-- Телефони людей: запам'ятовуються під час реєстрації (або перший, з якого прийшов автопінг).
-- Автозміна вмикається лише з телефона, прив'язаного до людини. IP у Wi-Fi змінюється, тож ключ — ідентифікатор пристрою
create table if not exists devices (id integer primary key, user_id integer not null references users(id) on delete cascade,
  device_key text not null, name text, first_ip text, last_ip text, created_at integer not null, last_seen_at integer,
  unique(user_id, device_key));
-- Довгоживучий токен «запам'ятати мене»: видається після входу, лежить у cookie на телефоні/в браузері.
-- За ним людина входить автоматично, навіть якщо IP телефона чи сервера змінився. Не прив'язаний до IP (IP ненадійний).
create table if not exists remember_tokens (id integer primary key, user_id integer not null references users(id) on delete cascade,
  selector text not null unique, validator_hash text not null, name text, device text,
  created_at integer not null, last_used_at integer, last_ip text, expires_at integer not null);
-- Аватарки лежать у базі (єдине джерело правди): переживають видалення чи відтворення особової папки.
-- Копія avatar.jpg у папці — лише для зручності перегляду, сервер роздає зображення саме з бази.
create table if not exists avatars (user_id integer primary key references users(id) on delete cascade,
  data blob not null, mime text not null, ext text not null, updated_at integer not null);
-- Чия особова папка застаріла. Тригери нижче ловлять будь-яку зміну даних людини,
-- фоновий потік profiles.py переписує її папку (пінги last_seen_at не рахуються)
create table if not exists dirty_users (user_id integer primary key);
create trigger if not exists dirty_users_ins after insert on users begin insert or ignore into dirty_users values (new.id); end;
create trigger if not exists dirty_users_upd after update on users begin insert or ignore into dirty_users values (new.id); end;
create trigger if not exists dirty_shifts_ins after insert on shifts begin insert or ignore into dirty_users values (new.user_id); end;
create trigger if not exists dirty_shifts_upd after update of user_id, location_id, started_at, ended_at, status, close_reason,
  hourly_rate, is_edited, note on shifts begin insert or ignore into dirty_users values (new.user_id); insert or ignore into dirty_users values (old.user_id); end;
create trigger if not exists dirty_shifts_del after delete on shifts begin insert or ignore into dirty_users values (old.user_id); end;
create trigger if not exists dirty_breaks_ins after insert on breaks begin
  insert or ignore into dirty_users select user_id from shifts where id = new.shift_id; end;
create trigger if not exists dirty_breaks_upd after update on breaks begin
  insert or ignore into dirty_users select user_id from shifts where id = new.shift_id; end;
create trigger if not exists dirty_breaks_del after delete on breaks begin
  insert or ignore into dirty_users select user_id from shifts where id = old.shift_id; end;
create trigger if not exists dirty_requests_ins after insert on requests begin insert or ignore into dirty_users values (new.user_id); end;
create trigger if not exists dirty_requests_upd after update on requests begin insert or ignore into dirty_users values (new.user_id); end;
create trigger if not exists dirty_requests_del after delete on requests begin insert or ignore into dirty_users values (old.user_id); end;
create trigger if not exists dirty_adj_ins after insert on adjustments begin insert or ignore into dirty_users values (new.user_id); end;
create trigger if not exists dirty_adj_upd after update on adjustments begin insert or ignore into dirty_users values (new.user_id); end;
create trigger if not exists dirty_adj_del after delete on adjustments begin insert or ignore into dirty_users values (old.user_id); end;
create trigger if not exists dirty_sched_ins after insert on schedule begin insert or ignore into dirty_users values (new.user_id); end;
create trigger if not exists dirty_sched_upd after update on schedule begin
  insert or ignore into dirty_users values (new.user_id); insert or ignore into dirty_users values (old.user_id); end;
create trigger if not exists dirty_sched_del after delete on schedule begin insert or ignore into dirty_users values (old.user_id); end;
-- Індекси: без них кожен пінг і кожна сторінка перебирають усі зміни за всі роки
create index if not exists shifts_user_start on shifts(user_id, started_at);
create index if not exists shifts_start on shifts(started_at);
create index if not exists shifts_open on shifts(status) where status != 'closed';
create index if not exists breaks_shift on breaks(shift_id);
create index if not exists schedule_user_start on schedule(user_id, starts_at);
create index if not exists schedule_start on schedule(starts_at);
create index if not exists requests_user on requests(user_id, type, status);
create index if not exists requests_status on requests(status);
create index if not exists adjustments_user_day on adjustments(user_id, day);
create index if not exists conn_log_created on conn_log(created_at);
create index if not exists conn_log_user on conn_log(user_id, id);
create index if not exists notifications_user on notifications(user_id, is_read);
create index if not exists audit_target on audit(target_user_id, id);
"""

BACKUP_DIR = os.environ.get("CREWHUB_BACKUP_DIR", os.path.join(BASE_DIR, "backups"))
# Особові папки: people/<Ім'я>/ з профілем, змінами, відсутностями, аватаркою. Синхронізує profiles.py
PEOPLE_DIR = os.environ.get("CREWHUB_PEOPLE_DIR", os.path.join(BASE_DIR, "people"))
RECOVERED = {"from_backup": False}  # check_db відновив базу з копії: profiles.py підтягне свіжі дані з папок
BACKUP_KEEP = int(os.environ.get("CREWHUB_BACKUP_KEEP", "30"))
LOG_KEEP_DAYS = int(os.environ.get("CREWHUB_LOG_KEEP_DAYS", "180"))  # журнал підключень; зміни і зарплата не видаляються

_lock = threading.RLock()  # SQLite + фоновий потік: серіалізуємо записи
# Перевірка пароля (scrypt) бере ~32 МБ пам'яті і ~0,1 с процесора. 200 входів одночасно з'їли б кілька ГБ,
# тому одночасно перевіряємо не більше 4 паролів, решта чекає в черзі.
_people_lock = threading.RLock()  # запис особових папок і їх бекап не перетинаються
_hash_slots = threading.BoundedSemaphore(int(os.environ.get("CREWHUB_HASH_SLOTS", "4")))


class Throttle:
    """Обмеження спроб (вхід, реєстрація, перевірка пароля) без сторонніх бібліотек: ковзне вікно в пам'яті.
    Перезапуск сервера скидає лічильники — це прийнятно, перебір однаково впреться в ліміт знову."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits = {}

    def recent(self, key, window):
        """Скільки спроб за останні window секунд і через скільки секунд найстаріша з них «згорить»."""
        t = time.time()
        with self.lock:
            q = [x for x in self.hits.get(key, ()) if t - x < window]
            if q:
                self.hits[key] = q
            else:
                self.hits.pop(key, None)
            return len(q), (int(window - (t - q[0])) + 1 if q else 0)

    def add(self, key):
        t = time.time()
        with self.lock:
            self.hits.setdefault(key, []).append(t)
            if len(self.hits) > 20000:  # не даємо лічильникам з'їсти пам'ять: прибираємо тих, хто давно мовчить
                for k in [k for k, q in self.hits.items() if t - q[-1] > 3600]:
                    del self.hits[k]

    def clear(self, key):
        with self.lock:
            self.hits.pop(key, None)

    def reset(self):
        with self.lock:
            self.hits.clear()


_throttle = Throttle()
# (ключ, кількість, вікно в секундах): перевищив хоч одне — чекає, пароль навіть не перевіряється
LOGIN_LIMITS = (("acct-ip", 5, 900), ("ip", 20, 900), ("acct", 50, 3600))


def login_blocked(acct, ip):
    """Секунд до наступної дозволеної спроби входу, або 0."""
    keys = {"acct-ip": f"login:{acct}:{ip}", "ip": f"login-ip:{ip}", "acct": f"login:{acct}"}
    wait = 0
    for kind, limit, window in LOGIN_LIMITS:
        n, left = _throttle.recent(keys[kind], window)
        if n >= limit:
            wait = max(wait, left)
    return wait


def login_failed(acct, ip):
    for key in (f"login:{acct}:{ip}", f"login-ip:{ip}", f"login:{acct}"):
        _throttle.add(key)


def wait_text(seconds):
    m = max(1, (seconds + 59) // 60)
    return f"Забагато невдалих спроб. Спробуйте через {m} хв"


def verify_password(pw_hash, password):
    with _hash_slots:
        return check_password_hash(pw_hash, password)


def hash_password(password):
    with _hash_slots:
        return generate_password_hash(password)


def meta_get(conn, key, default=None):
    row = conn.execute("select value from meta where key = ?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute("insert into meta(key, value) values (?, ?) on conflict(key) do update set value = excluded.value", (key, str(value)))


# ---------------------------------------------------------------------------
# База
# ---------------------------------------------------------------------------
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    # WAL: читання не блокують запис, тож одночасні пінги 100 телефонів не ловлять «database is locked»;
    # busy_timeout: другий запис в ту саму мить чекає до 15 с замість помилки «database is locked»
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma busy_timeout = 15000")
    conn.execute("pragma synchronous = normal")
    return conn


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now():
    return int(time.time())


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def local_ips():
    """Усі приватні IPv4 цього комп'ютера (Wi-Fi, кабель, VPN), основна першою."""
    found = [get_local_ip()]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private and not addr.is_loopback and not addr.is_link_local and ip not in out:
            out.append(ip)
    return out or ["127.0.0.1"]


# CREWHUB_DEMO: "1" — тестові акаунти й історія змін (демо-застосунок), "users" — лише тестові акаунти (автотести),
# інакше — робоча версія: тільки адміністратор.
DEMO_MODE = os.environ.get("CREWHUB_DEMO", "0")
DEFAULT_ADMIN_PASSWORD = "admin123"  # лише для першого входу: далі система вимагає свій пароль
DEMO_ACCOUNTS = DEMO_MODE in ("1", "users")
DEMO_PEOPLE = [
    ("moder", "moder123", "Марія Модератор", "moderator", "Менеджер зміни", 180, "2024-09-01", "+380500000002"),
    ("ivan", "1234", "Іван Петренко", "employee", "Оператор", 120, "2025-05-01", "+380671234567"),
    ("olha", "1234", "Ольга Коваль", "employee", "Спеціаліст", 110, "2026-02-10", "+380937654321"),
]


def init_db(seed=True):
    """Створює таблиці і, якщо база порожня, тестові дані: акаунти, точку і Wi-Fi зону вашої локальної мережі."""
    conn = connect()
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("pragma table_info(users)")]
    if "must_change_password" not in cols:  # база від попередньої версії
        conn.execute("alter table users add column must_change_password integer not null default 0")
    if "folder" not in cols:
        conn.execute("alter table users add column folder text")
    if "whatsapp" not in cols:
        conn.execute("alter table users add column whatsapp text")
    if "grant_until" not in [r[1] for r in conn.execute("pragma table_info(requests)")]:
        conn.execute("alter table requests add column grant_until integer")
    bcols = [r[1] for r in conn.execute("pragma table_info(breaks)")]
    if "rate_pct" not in bcols:
        conn.execute("alter table breaks add column rate_pct real not null default 100")
    if "reason" not in bcols:  # для wifi_loss: 'silence' — телефон замовк (міг заснути), 'left' — справді вийшов з Wi-Fi
        conn.execute("alter table breaks add column reason text")
    if "bg_ok" not in [r[1] for r in conn.execute("pragma table_info(shifts)")]:
        # 1 = застосунок працює без обмежень батареї і пінгує щохвилини навіть у сні: тиша = справжня втрата Wi-Fi
        conn.execute("alter table shifts add column bg_ok integer not null default 0")
    lcols = [r[1] for r in conn.execute("pragma table_info(locations)")]
    for c in ("wifi_ssid", "wifi_password"):  # Wi-Fi, прикріплений до точки (назва й пароль для QR підключення)
        if c not in lcols:
            conn.execute(f"alter table locations add column {c} text")
    # Старі аватарки лежали лише файлом у папці — переносимо їх у базу, щоб вони не зникали разом з папкою.
    # Усе в try: міграція аватарок ніколи не має завадити запуску сервера.
    try:
        for row in conn.execute("select id, folder from users where photo_url is not null and photo_url != ''"
                                " and id not in (select user_id from avatars)").fetchall():
            f = avatar_file(os.path.join(PEOPLE_DIR, row["folder"])) if row["folder"] else None
            if not f:
                continue
            try:
                with open(f, "rb") as fh:
                    blob = fh.read()
                ext = f.rsplit(".", 1)[-1].lower()
                if blob and ext in PHOTO_MIME:
                    conn.execute("insert or ignore into avatars(user_id, data, mime, ext, updated_at) values (?, ?, ?, ?, ?)",
                                 (row["id"], blob, PHOTO_MIME[ext], "jpg" if ext == "jpeg" else ext, now()))
            except OSError:
                pass
    except sqlite3.Error:
        pass
    try:
        # Номер телефону — це логін, тому двох однакових бути не може
        conn.execute("create unique index if not exists users_phone_uq on users(phone) where phone is not null and phone != ''")
    except sqlite3.IntegrityError:
        print("Увага: у базі є однакові номери телефону, вхід за номером для них не працюватиме")
    # Старі версії додавали 127.0.0.0/8 («Цей комп'ютер») як робочу мережу: через тунель це робило «робочим»
    # будь-який запит з інтернету. Такі адреси більше ніколи не вважаються робочими — прибираємо і запис.
    conn.execute("delete from networks where cidr in ('127.0.0.0/8', '127.0.0.1/32', '::1/128')")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("insert or ignore into settings(key, value) values (?, ?)", (k, v))
    if not meta_get(conn, "forgive_v2"):
        # Стара поблажка (4 год) дозволяла піти з роботи на кілька годин і отримати за них оплату
        conn.execute("update settings set value = ? where key = 'silence_forgive_minutes' and value = '240'",
                     (DEFAULT_SETTINGS["silence_forgive_minutes"],))
        meta_set(conn, "forgive_v2", 1)
    if not os.environ.get("CREWHUB_SECRET"):
        # Свій випадковий ключ сесій для кожної інсталяції; зберігається в базі, тож після перезапуску
        # сервера ніхто не вилітає з акаунта
        if not meta_get(conn, "secret_key"):
            meta_set(conn, "secret_key", secrets.token_hex(32))
        app.secret_key = meta_get(conn, "secret_key")
    if seed and not conn.execute("select 1 from users").fetchone():
        lan = ipaddress.ip_network(f"{get_local_ip()}/24", strict=False)
        conn.execute("insert into locations(id, name, address) values (1, 'Тестова точка', 'Ваша локальна мережа')")
        conn.execute("insert into networks(location_id, cidr, label) values (1, ?, 'Локальна мережа цього ПК')", (str(lan),))
        people = [("admin", DEFAULT_ADMIN_PASSWORD, "Адмін Адмінович", "admin", "Директор", 0, "2024-01-15", "+380500000001")]
        if DEMO_ACCOUNTS:
            people += DEMO_PEOPLE
        for u, p, name, role, title, rate, hired, phone in people:
            conn.execute(
                "insert into users(username, password_hash, full_name, role, job_title, department, location_id, hourly_rate,"
                " hire_date, phone) values (?, ?, ?, ?, ?, 'Основний відділ', 1, ?, ?, ?)",
                (u, hash_password(p), name, role, title, rate, hired, phone))
        if not DEMO_ACCOUNTS:
            # Стандартний пароль надруковано в README і в консолі: при першому вході адмін задає свій
            conn.execute("update users set must_change_password = 1 where username = 'admin'")
        if DEMO_ACCOUNTS:
            # Ольга за графіком мала вийти 20 хв тому: в моніторингу вона буде «спізнюється».
            olha = conn.execute("select id from users where username = 'olha'").fetchone()[0]
            conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at) values (?, 1, ?, ?)",
                         (olha, now() - 20 * 60, now() + 7 * 3600))
        if DEMO_MODE == "1":
            seed_demo_history(conn)
    elif not DEMO_ACCOUNTS and not meta_get(conn, "demo_removed"):
        # Робоча версія: тестові акаунти з попередніх версій прибираються один раз (папки переїжджають у backups/видалені)
        meta_set(conn, "demo_removed", now())
        conn.commit()
        for username, *_rest, phone in DEMO_PEOPLE:
            u = conn.execute("select * from users where username = ? and phone = ?", (username, phone)).fetchone()
            if u:
                delete_user(conn, u, None)
    if not DEMO_ACCOUNTS:
        # База від старої версії: адмін досі з паролем admin123 — при наступному вході система попросить змінити
        a = conn.execute("select id, password_hash from users where username = 'admin' and must_change_password = 0").fetchone()
        if a and verify_password(a["password_hash"], DEFAULT_ADMIN_PASSWORD):
            conn.execute("update users set must_change_password = 1 where id = ?", (a["id"],))
    conn.commit()
    conn.close()


def seed_demo_history(conn):
    """Демо-історія, щоб графіки, табель і зарплата не були порожні: 2 тижні змін і графік на тиждень вперед."""
    rnd = random.Random(7)
    ids = {r["username"]: (r["id"], r["hourly_rate"]) for r in conn.execute("select id, username, hourly_rate from users")}
    plan = {"ivan": (9, 18), "olha": (14, 22), "moder": (10, 19)}
    today = date.today()
    for name, (h_from, h_to) in plan.items():
        uid, rate = ids[name]
        for back in range(13, 0, -1):
            d = today - timedelta(days=back)
            if (d.weekday() + uid) % 7 in (5, 6):  # два вихідні на тиждень у кожного свої
                continue
            start = int(datetime.combine(d, datetime.min.time()).timestamp()) + h_from * 3600 + rnd.randint(-10, 12) * 60
            end = int(datetime.combine(d, datetime.min.time()).timestamp()) + h_to * 3600 + rnd.randint(-15, 25) * 60
            cur = conn.execute("insert into shifts(user_id, location_id, started_at, ended_at, status, close_reason, hourly_rate,"
                               " last_seen_at, last_seen_ip, start_ip, end_ip, device) values (?, 1, ?, ?, 'closed', 'manual', ?, ?,"
                               " '192.168.1.23', '192.168.1.23', '192.168.1.23', 'демо')", (uid, start, end, rate, end))
            day0 = int(datetime.combine(d, datetime.min.time()).timestamp())
            conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at) values (?, 1, ?, ?)",
                         (uid, day0 + h_from * 3600, day0 + h_to * 3600))
            lunch = start + 4 * 3600
            conn.execute("insert into breaks(shift_id, kind, started_at, ended_at, is_paid) values (?, 'lunch', ?, ?, 0)",
                         (cur.lastrowid, lunch, lunch + rnd.choice([30, 40, 45]) * 60))
        for fwd in range(1, 8):
            d = today + timedelta(days=fwd)
            if (d.weekday() + uid) % 7 in (5, 6):
                continue
            base = int(datetime.combine(d, datetime.min.time()).timestamp())
            conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at) values (?, 1, ?, ?)",
                         (uid, base + h_from * 3600, base + h_to * 3600))
    conn.execute("insert into adjustments(user_id, amount, reason, day, created_by, created_at) values (?, 500, ?, ?, ?, ?)",
                 (ids["ivan"][0], "Найкращі відгуки гостей", (today - timedelta(days=3)).isoformat(), ids["admin"][0], now()))


def setting(key, conn=None):
    row = (conn or db()).execute("select value from settings where key = ?", (key,)).fetchone()
    return row[0] if row else DEFAULT_SETTINGS[key]


def setting_int(key, conn=None):
    return int(float(setting(key, conn)))


def audit(conn, actor_id, action, entity, entity_id, target_user_id, changes):
    conn.execute("insert into audit(actor_id, action, entity, entity_id, target_user_id, changes, created_at)"
                 " values (?, ?, ?, ?, ?, ?, ?)",
                 (actor_id, action, entity, entity_id, target_user_id, json.dumps(changes, ensure_ascii=False), now()))


def diff(old, new):
    """Лише змінені поля: {'hourly_rate': [120, 150]}"""
    return {k: [old.get(k), v] for k, v in new.items() if str(old.get(k)) != str(v)}


def notify(conn, user_id, title, body=""):
    conn.execute("insert into notifications(user_id, title, body, created_at) values (?, ?, ?, ?)",
                 (user_id, title, body, now()))


def notify_staff(conn, title, body="", roles=STAFF_ROLES):
    for r in conn.execute(f"select id from users where role in ({','.join('?' * len(roles))}) and status not in ('dismissed', 'pending')",
                          roles):
        notify(conn, r[0], title, body)


def expire_grants(conn):
    """Дозволи на дей-офф, за які людина не обрала день вчасно, згорають."""
    if conn.execute("update requests set status = 'expired' where status = 'granted' and grant_until < ?", (now(),)).rowcount:
        conn.commit()


def dayoff_campaign_until(conn):
    """Відкритий збір заявок на дей-офф: поки не мине цей час, у кожного на головному екрані є кнопка «Запросити дей-офф». 0 = збір закрито."""
    try:
        until = int(meta_get(conn, "dayoff_open_until", "0") or "0")
    except (TypeError, ValueError):
        until = 0
    return until if until > now() else 0


def grant_dayoff(conn, r, actor, comment=None):
    """Схвалений запит на дей-офф без дати = дозвіл: людина сама обирає день протягом DAYOFF_PICK_DAYS днів."""
    until = now() + DAYOFF_PICK_DAYS * 86400
    conn.execute("update requests set status = 'granted', grant_until = ?, reviewer_id = ?, review_comment = ? where id = ?",
                 (until, actor["id"], comment, r["id"]))
    audit(conn, actor["id"], "UPDATE", "requests", r["id"], r["user_id"], {"status": [r["status"], "granted"]})
    notify(conn, r["user_id"], "Дей-офф дозволено", f"Оберіть день до {fmt_dt(until)} у «Мій графік»")


def log_clock(conn, user_id, shift_id, event, ip, location_id, allowed, device=None, details=None):
    conn.execute("insert into conn_log(user_id, shift_id, event, ip, location_id, allowed, device, details, created_at)"
                 " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (user_id, shift_id, event, ip, location_id, int(allowed), device,
                  json.dumps(details, ensure_ascii=False) if details else None, now()))


# ---------------------------------------------------------------------------
# IP і Wi-Fi зони
# ---------------------------------------------------------------------------
def client_ip():
    ip = (request.remote_addr or "").removeprefix("::ffff:")
    if TRUST_PROXY and ip in ("127.0.0.1", "::1"):
        last = (request.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if last:
            ip = last
    return ip.removeprefix("::ffff:")


def work_ip(ip):
    """Адреса, яка взагалі може бути робочою мережею. Сам сервер (127.x, ::1) — ні: через тунель без
    X-Forwarded-For усі запити приходять з localhost, і тоді будь-хто з інтернету був би «на точці»."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.is_loopback or addr.is_unspecified:
        return None
    return addr


def zone_error(net):
    """Чому цю підмережу не можна зробити робочою зоною (None — можна)."""
    if net.prefixlen == 0 or any(net.overlaps(ipaddress.ip_network(x)) for x in ("127.0.0.0/8", "::1/128")):
        return "Адреси самого сервера (127.x) і «увесь інтернет» не можуть бути робочою мережею"
    if net.version == 4 and (net.prefixlen < 8 or (not net.is_private and net.prefixlen < 16)):
        return "Надто широка мережа: вкажіть IP роутера точки або її локальну підмережу"
    if net.version == 6 and net.prefixlen < 32:
        return "Надто широка мережа: вкажіть префікс точки"
    return None


def device_key(sent=None):
    """Стабільний ідентифікатор телефона. Застосунок CrewHub додає його до User-Agent (CrewHubDevice/...),
    браузер — випадковий id з localStorage. IP для цього не годиться: у Wi-Fi він щоразу інший."""
    ua = request.headers.get("User-Agent") or ""
    m = re.search(r"CrewHubDevice/([\w-]{6,64})", ua)
    if m:
        return "app-" + m.group(1)
    sent = (sent or "").strip()
    return sent[:80] if re.fullmatch(r"[\w .:-]{4,80}", sent or "") else None


def device_name():
    ua = request.headers.get("User-Agent") or ""
    m = re.search(r"Android [\d.]+;(?: [a-z]{2}[-_][A-Z]{2};)? ([^;)]+?)(?: Build/[^;)]*)?[;)]", ua)
    if m and m.group(1).strip() not in ("K", "wv"):
        return m.group(1).strip()
    for k, v in (("iPhone", "iPhone"), ("Android", "Android"), ("Windows", "Windows ПК"), ("Mac OS", "Mac")):
        if k in ua:
            return v
    return "Пристрій"


def remember_device(conn, user_id, key, ip, bind=True):
    """Оновлює останній IP пристрою; якщо bind — прив'язує новий пристрій до людини."""
    if not key:
        return False
    t = now()
    cur = conn.execute("update devices set last_ip = ?, last_seen_at = ? where user_id = ? and device_key = ?", (ip, t, user_id, key))
    if cur.rowcount:
        return True
    if bind:
        conn.execute("insert or ignore into devices(user_id, device_key, name, first_ip, last_ip, created_at, last_seen_at)"
                     " values (?, ?, ?, ?, ?, ?, ?)", (user_id, key, device_name() if has_request_context() else None, ip, ip, t, t))
        return True
    return False


def device_allowed(conn, user_id, key, ip):
    """Автозміна лише з телефона людини. Нічого не прив'язано — перший телефон, з якого прийшов пінг, стає її."""
    if setting("device_lock", conn) != "1":
        return True
    keys = [r[0] for r in conn.execute("select device_key from devices where user_id = ?", (user_id,))]
    if not key:
        return not keys  # старий клієнт без id: поки телефон не прив'язано, перевіряти нема з чим
    if keys and key.startswith("app-") and all(k.startswith("web-") for k in keys):
        # Зареєструвався в браузері телефона, а працює в застосунку: застосунок стає «своїм» телефоном
        conn.execute("delete from devices where user_id = ?", (user_id,))
        keys = []
    return remember_device(conn, user_id, key, ip, bind=not keys)


REMEMBER_COOKIE = "crewhub_remember"
REMEMBER_DAYS = 365


def _remember_hash(validator):
    return hashlib.sha256(validator.encode()).hexdigest()


def issue_remember(conn, user_id, device=None, name=None, ip=None):
    """Створює довгоживучий токен входу і повертає значення для cookie (selector:validator).
    Токен не прив'язаний до IP: IP у Wi-Fi і в мобільній мережі змінюється, тож людина лишається
    в системі, навіть якщо роутер дав інший IP телефону чи серверу."""
    selector, validator = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
    t = now()
    conn.execute("insert into remember_tokens(user_id, selector, validator_hash, name, device, created_at, last_used_at, last_ip, expires_at)"
                 " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (user_id, selector, _remember_hash(validator), name, device, t, t, ip, t + REMEMBER_DAYS * 86400))
    return f"{selector}:{validator}"


def remember_user(conn, cookie_value, ip=None):
    """Перевіряє cookie «запам'ятати мене» (selector:validator). Повертає user_id дійсного токена і
    продовжує його (ковзний термін), або None. Протухлий/невідомий токен видаляється."""
    if not cookie_value or ":" not in cookie_value:
        return None
    selector, validator = cookie_value.split(":", 1)
    row = conn.execute("select * from remember_tokens where selector = ?", (selector,)).fetchone()
    if not row:
        return None
    t = now()
    if row["expires_at"] < t or not secrets.compare_digest(row["validator_hash"], _remember_hash(validator)):
        if row["expires_at"] < t:
            conn.execute("delete from remember_tokens where id = ?", (row["id"],))
        return None
    if not conn.execute("select 1 from users where id = ? and status not in ('dismissed', 'pending')", (row["user_id"],)).fetchone():
        conn.execute("delete from remember_tokens where id = ?", (row["id"],))
        return None
    conn.execute("update remember_tokens set last_used_at = ?, last_ip = ?, expires_at = ? where id = ?",
                 (t, ip or row["last_ip"], t + REMEMBER_DAYS * 86400, row["id"]))
    return row["user_id"]


def forget_remember(conn, cookie_value):
    """Видаляє один токен (вихід на цьому пристрої)."""
    if cookie_value and ":" in cookie_value:
        conn.execute("delete from remember_tokens where selector = ?", (cookie_value.split(":", 1)[0],))


def revoke_remember(conn, user_id):
    """Відкликає всі токени людини (вихід на всіх пристроях)."""
    conn.execute("delete from remember_tokens where user_id = ?", (user_id,))


def set_remember_cookie(resp, value):
    resp.set_cookie(REMEMBER_COOKIE, value, max_age=REMEMBER_DAYS * 86400, httponly=True,
                    samesite="Lax", secure=app.config.get("SESSION_COOKIE_SECURE", False), path="/")
    return resp


def clear_remember_cookie(resp):
    resp.delete_cookie(REMEMBER_COOKIE, path="/")
    return resp


def normalize_phone(raw):
    """Приводить номер до одного вигляду, щоб 067…, 38067… і +38 (067) … були одним логіном.
    Повертає '+380671234567' або None, якщо це не схоже на номер."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if len(digits) == 10 and digits.startswith("0"):       # 0671234567
        digits = "38" + digits
    elif len(digits) == 9:                                   # 671234567
        digits = "380" + digits
    if digits.startswith("380") and len(digits) != 12:
        return None
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


def format_phone(p):
    """+380671234567 -> +380 67 123 45 67"""
    if p and p.startswith("+380") and len(p) == 13:
        return f"+380 {p[4:6]} {p[6:9]} {p[9:11]} {p[11:]}"
    return p or ""


def phone_taken(conn, phone, except_id=None):
    return conn.execute("select 1 from users where phone = ? and id != ?", (phone, except_id or 0)).fetchone() is not None


def temp_password():
    """Тимчасовий пароль для скидання: легко продиктувати, працівник змінить його при вході."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice("23456789") for _ in range(4))


def suggest_cidr(ip):
    """Яку зону додати для IP: локальна адреса → вся підмережа /24 цього Wi-Fi, публічна → точний IP роутера."""
    addr = work_ip(ip)
    if addr is None:
        return None
    if addr.version == 4 and addr.is_private and not addr.is_loopback:
        return str(ipaddress.ip_network(f"{addr}/24", strict=False))
    return str(ipaddress.ip_network(addr))


# ---------------------------------------------------------------------------
# Швидке підключення телефона: QR-код або код підключення замість IP сервера.
# Працівник бачить у застосунку назву компанії, а адреса сервера в коді перемішана (не читається як 192.168…)
# ---------------------------------------------------------------------------
CONNECT_KEY = b"CrewHub!"
CONNECT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # без I, L, O, U, щоб не плутати при введенні


def server_url():
    """Адреса сервера для телефонів: якщо адмін відкрив сторінку як localhost, підставляємо IP у локальній мережі."""
    host = request.host
    name, port = (host.rsplit(":", 1) if ":" in host and not host.endswith("]") else (host, ""))
    if name in ("localhost", "127.0.0.1", "[::1]"):
        name = get_local_ip()
    return f"{request.scheme}://{name}{':' + port if port else ''}/"


def connect_code(url):
    """http://192.168.1.23:5000/ → «K7QM2-D9FXA» (IPv4 і порт, перемішані). Для інших адрес коду немає — лише QR."""
    u = urlsplit(url)
    try:
        ip = ipaddress.IPv4Address(u.hostname or "")
    except ValueError:
        return None
    if u.scheme != "http" or u.path not in ("", "/"):
        return None
    raw = bytes(b ^ k for b, k in zip(ip.packed + (u.port or 80).to_bytes(2, "big"), CONNECT_KEY))
    n = int.from_bytes(raw, "big") << 2  # 48 біт → 50 біт = 10 символів
    code = "".join(CONNECT_ALPHABET[(n >> (5 * i)) & 31] for i in range(9, -1, -1))
    return code[:5] + "-" + code[5:]


def connect_link(url, company, loc=None):
    """Вміст QR: адреса сервера (base64), назва компанії і, якщо прикріплено, Wi-Fi точки."""
    b64 = lambda v: base64.urlsafe_b64encode(v.encode()).decode().rstrip("=")  # noqa: E731
    q = {"s": b64(url), "n": company}
    if loc is not None and loc["wifi_ssid"]:
        q.update(w=b64(loc["wifi_ssid"]), p=b64(loc["wifi_password"] or ""))
    if loc is not None:
        q["l"] = loc["name"]
    return "crewhub://connect?" + urlencode(q)


def match_location(conn, ip):
    """Точка, до Wi-Fi якої належить IP (найточніша підмережа), або None."""
    addr = work_ip(ip)
    if addr is None:
        return None
    best = None
    for r in conn.execute("select n.cidr, l.id, l.name from networks n join locations l on l.id = n.location_id"
                          " where l.is_active = 1"):
        try:
            net = ipaddress.ip_network(r["cidr"], strict=False)
        except ValueError:
            continue
        if addr.version == net.version and addr in net and (best is None or net.prefixlen > best[0]):
            best = (net.prefixlen, r["id"], r["name"])
    return {"id": best[1], "name": best[2]} if best else None


def ip_in_location(conn, ip, location_id):
    """Чи належить IP до Wi-Fi саме цієї точки (будь-якій з її підмереж).
    Зміну прив'язано до мережі, де її почали: під час пінгу й закриття перевіряємо IP проти
    цієї ж точки, а не будь-якої робочої мережі — щоб зміну не можна було «перенести» в іншу
    мережу чи підмінити IP на чужу робочу точку."""
    addr = work_ip(ip)
    if not location_id or addr is None:
        return False
    for r in conn.execute("select cidr from networks where location_id = ?", (location_id,)):
        try:
            net = ipaddress.ip_network(r["cidr"], strict=False)
        except ValueError:
            continue
        if addr.version == net.version and addr in net:
            return True
    return False


# ---------------------------------------------------------------------------
# Підрахунок годин і грошей
# ---------------------------------------------------------------------------
def pay_rules(conn):
    """Множники оплати з налаштувань: вихідні, нічні години."""
    st = {r[0]: r[1] for r in conn.execute("select key, value from settings")}

    def num(k):
        try:
            return max(float(st.get(k, DEFAULT_SETTINGS[k]).replace(",", ".")), 0)
        except ValueError:
            return 1.0

    def minutes(k):
        try:
            h, m = (st.get(k) or DEFAULT_SETTINGS[k]).split(":")
            return (int(h) * 60 + int(m)) % 1440
        except ValueError:
            return 0
    return {"weekend": num("weekend_multiplier"), "night": num("night_multiplier"),
            "night_start": minutes("night_start"), "night_end": minutes("night_end")}


def time_factor(ts, rules):
    """Множник ставки в момент ts. Якщо збігаються вихідний і ніч, береться більший."""
    d = datetime.fromtimestamp(ts)
    f = 1.0
    if d.weekday() >= 5:
        f = max(f, rules["weekend"])
    m, ns, ne = d.hour * 60 + d.minute, rules["night_start"], rules["night_end"]
    if ns != ne and ((ns <= m < ne) if ns < ne else (m >= ns or m < ne)):
        f = max(f, rules["night"])
    return f


def weighted_seconds(a, b, rules):
    """Секунди між a і b, помножені на множник часу (ріжемо інтервал по півночі і межах ночі)."""
    if b <= a:
        return 0.0
    cuts = {a, b}
    day = datetime.fromtimestamp(a).date() - timedelta(days=1)
    last = datetime.fromtimestamp(b).date()
    while day <= last:
        base = datetime.combine(day, datetime.min.time())
        for mins in (0, rules["night_start"], rules["night_end"]):
            x = int((base + timedelta(minutes=mins)).timestamp())
            if a < x < b:
                cuts.add(x)
        day += timedelta(days=1)
    pts = sorted(cuts)
    return sum((q - p) * time_factor((p + q) // 2, rules) for p, q in zip(pts, pts[1:]))


def shift_stats(conn, shift, at=None, rules=None):
    at = at or now()
    rules = rules or pay_rules(conn)
    end = shift["ended_at"] or at
    gross = max(end - shift["started_at"], 0)
    unpaid = paid_break = 0
    pay_units = weighted_seconds(shift["started_at"], end, rules)
    open_break = None
    for b in conn.execute("select * from breaks where shift_id = ?", (shift["id"],)):
        b_end = b["ended_at"] or end
        dur = max(b_end - b["started_at"], 0)
        w = weighted_seconds(b["started_at"], b_end, rules)
        if b["is_paid"]:
            paid_break += dur
            pay_units -= w * (1 - b["rate_pct"] / 100)  # обід за 50% чи 150% від ставки
        else:
            unpaid += dur
            pay_units -= w
        if b["ended_at"] is None:
            open_break = dict(b)
    worked = max(gross - unpaid, 0)
    rate = shift["hourly_rate"]
    earnings = round(max(pay_units, 0) / 3600 * rate, 2)
    # Скільки зараз нараховується за годину (для живого лічильника на телефоні)
    rate_now = 0.0
    if not shift["ended_at"]:
        if shift["status"] == "active":
            rate_now = rate * time_factor(at, rules)
        elif open_break and open_break["is_paid"]:
            rate_now = rate * time_factor(at, rules) * open_break["rate_pct"] / 100
    return {
        **dict(shift),
        "gross_seconds": gross,
        "unpaid_break_seconds": unpaid,
        "paid_break_seconds": paid_break,
        "worked_seconds": worked,
        "earnings": earnings,
        "base_earnings": round(worked / 3600 * rate, 2),
        "extra_earnings": round(earnings - worked / 3600 * rate, 2),
        "rate_now": round(rate_now, 4),
        "open_break": open_break,
    }


def open_shift(conn, user_id):
    return conn.execute("select * from shifts where user_id = ? and status != 'closed'", (user_id,)).fetchone()


def close_shift(conn, shift_id, end, reason, ip=None):
    row = conn.execute("select started_at from shifts where id = ?", (shift_id,)).fetchone()
    end = max(end, row["started_at"])
    conn.execute("update breaks set ended_at = max(?, started_at) where shift_id = ? and ended_at is null", (end, shift_id))
    conn.execute("update shifts set ended_at = ?, status = 'closed', close_reason = ?, end_ip = coalesce(?, end_ip)"
                 " where id = ?", (end, reason, ip, shift_id))


def day_bounds(d_from, d_to):
    start = int(datetime.combine(d_from, datetime.min.time()).timestamp())
    end = int(datetime.combine(d_to + timedelta(days=1), datetime.min.time()).timestamp())
    return start, end


def payroll(conn, d_from, d_to, user_id=None):
    start, end = day_bounds(d_from, d_to)
    q = "select * from users where (status not in ('dismissed', 'pending') or id in (select user_id from shifts where started_at >= ? and started_at < ?))"
    args = [start, end]
    if user_id:
        q += " and id = ?"
        args.append(user_id)
    rows = []
    rules = pay_rules(conn)
    for u in conn.execute(q + " order by full_name", args):
        shifts = conn.execute("select * from shifts where user_id = ? and started_at >= ? and started_at < ?",
                              (u["id"], start, end)).fetchall()
        stats = [shift_stats(conn, s, rules=rules) for s in shifts]
        adj = conn.execute("select amount from adjustments where user_id = ? and day between ? and ?",
                           (u["id"], d_from.isoformat(), d_to.isoformat())).fetchall()
        bonuses = sum(a[0] for a in adj if a[0] > 0)
        penalties = sum(a[0] for a in adj if a[0] < 0)
        earned = round(sum(s["earnings"] for s in stats), 2)
        dpay = dayoff_pay(conn, u, d_from, d_to)
        dayoff = round(sum(x["amount"] for x in dpay), 2)
        rows.append({
            "user": u, "shifts": len(stats),
            "hours": round(sum(s["worked_seconds"] for s in stats) / 3600, 2),
            "earned": earned, "extra": round(sum(s["extra_earnings"] for s in stats), 2), "bonuses": bonuses, "penalties": penalties,
            "dayoffs": len(dpay), "dayoff": dayoff,
            "total": round(earned + dayoff + bonuses + penalties, 2),
        })
    return rows


def dayoff_days(conn, user_id, d_from, d_to, except_rid=0):
    """Дні схвалених дей-оффів людини в періоді."""
    out = set()
    for r in conn.execute("select starts_on, ends_on from requests where user_id = ? and type = 'day_off' and status = 'approved'"
                          " and starts_on <= ? and ends_on >= ? and id != ?",
                          (user_id, d_to.isoformat(), d_from.isoformat(), except_rid or 0)):
        d, last = max(date.fromisoformat(r[0]), d_from), min(date.fromisoformat(r[1]), d_to)
        while d <= last:
            out.add(d)
            d += timedelta(days=1)
    return sorted(out)


def dayoff_pay(conn, user, d_from, d_to):
    """Дей-офф оплачується як повний робочий день (робочі години з «Правил») за ставкою людини."""
    if setting("dayoff_paid", conn) != "1":
        return []
    rows = []
    for d in dayoff_days(conn, user["id"], d_from, d_to):
        s0, e0, _ = default_slot(conn, user["id"], d)
        hours = (e0 - s0) / 3600
        rows.append({"day": d, "hours": round(hours, 2), "amount": round(hours * user["hourly_rate"], 2)})
    return rows


def dayoff_full_days(conn, user_id, d_from, d_to):
    """Дні, на які дей-офф уже не взяти: ліміт людей на день вичерпано."""
    per_day = setting_int("dayoff_per_day", conn)
    if not per_day:
        return []
    count = {}
    for r in conn.execute("select user_id, starts_on, ends_on from requests where type = 'day_off' and status = 'approved'"
                          " and starts_on <= ? and ends_on >= ? and user_id != ?", (d_to.isoformat(), d_from.isoformat(), user_id)):
        d, last = max(date.fromisoformat(r[1]), d_from), min(date.fromisoformat(r[2]), d_to)
        while d <= last:
            count.setdefault(d, set()).add(r[0])
            d += timedelta(days=1)
    return sorted(d for d, who in count.items() if len(who) >= per_day)


def dayoff_limit_error(conn, user_id, d_from, d_to=None, except_rid=0):
    """Ліміти дей-оффів: не більше N людей в один день і M дей-оффів на людину за місяць. Текст помилки або None."""
    d_to = d_to or d_from
    per_day, per_month = setting_int("dayoff_per_day", conn), setting_int("dayoff_per_month", conn)
    if per_day:
        for d in dayoff_full_days(conn, user_id, d_from, d_to):
            if not conn.execute("select 1 from requests where id = ? and starts_on <= ? and ends_on >= ?",
                                (except_rid or 0, d.isoformat(), d.isoformat())).fetchone():
                return f"На {d:%d.%m} дей-офф уже взяли інші (максимум {per_day} на день). Оберіть інший день"
    if per_month:
        days = []
        d = d_from
        while d <= d_to:
            days.append(d)
            d += timedelta(days=1)
        for m in sorted({x.replace(day=1) for x in days}):
            first, last = month_range(m.strftime("%Y-%m"))
            used = dayoff_days(conn, user_id, first, last, except_rid)
            new = [x for x in days if first <= x <= last and x not in used]
            if len(used) + len(new) > per_month:
                return (f"У {MONTHS_NOM[m.month - 1].lower()} вже є дей-офф: не більше {per_month} на місяць"
                        if used else f"Не більше {per_month} дей-офф(ів) на місяць")
    return None


# ---------------------------------------------------------------------------
# Годинник зміни
# ---------------------------------------------------------------------------
def hhmm_minutes(v, default):
    try:
        h, m = str(v).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return default


def work_window(conn, user_id, t):
    """Робочий час, у який потрапляє мить t (з запасом на ранній прихід): (початок, кінець) або None.
    Спершу особистий «Графік» людини; якщо на цей день графіка немає — загальні години з «Правил» (Пн–Пт)."""
    early = setting_int("auto_early_minutes", conn) * 60
    sc = conn.execute("select starts_at, ends_at from schedule where user_id = ? and starts_at - ? <= ? and ends_at > ?"
                      " order by starts_at limit 1", (user_id, early, t, t)).fetchone()
    if sc:
        return sc["starts_at"], sc["ends_at"]
    ws, we = hhmm_minutes(setting("work_start", conn), 540), hhmm_minutes(setting("work_end", conn), 1080)
    weekends = setting("work_weekends", conn) == "1"
    today = datetime.fromtimestamp(t).date()
    for d in (today, today - timedelta(days=1), today + timedelta(days=1)):  # нічна зміна могла початися вчора
        if d.weekday() >= 5 and not weekends:
            continue
        day0 = datetime.combine(d, datetime.min.time())
        a = int((day0 + timedelta(minutes=ws)).timestamp())
        b = int((day0 + timedelta(minutes=we if we > ws else we + 1440)).timestamp())
        if a - early <= t < b:
            if absence_on(conn, user_id, d):
                return None
            day_start = int(day0.timestamp())
            if conn.execute("select 1 from schedule where user_id = ? and starts_at >= ? and starts_at < ?",
                            (user_id, day_start, day_start + 86400)).fetchone():
                return None  # на цей день є власний графік в інший час
            return a, b
    return None


# Остання присутність людини без зміни: коли телефон пінгував і чому зміна сама не почалась (для «Огляду» адміна)
_presence = {}


def _why(user, ip, loc, t, text):
    _presence[user["id"]] = {"t": t, "ip": ip, "loc": loc["name"] if loc else None, "why": text}
    return None


def work_hours_text(conn):
    days = "щодня" if setting("work_weekends", conn) == "1" else "Пн–Пт"
    return f"{setting('work_start', conn)}–{setting('work_end', conn)} {days}"


def try_auto_start(conn, user, loc, ip, device, t):
    """Автостарт: людина в робочому Wi-Fi, у свій робочий час, і сьогодні в цьому вікні ще не працювала
    (якщо закрила зміну сама — повторно не відкриваємо). Прийшов раніше — відлік з початку робочого часу."""
    if not loc:
        return _why(user, ip, loc, t, f"телефон у мережі з IP {ip}, а це не робоча мережа: додайте її в «Точки і Wi-Fi»")
    if user["status"] != "active":
        return _why(user, ip, loc, t, "статус не «Активний»")
    if setting("auto_start", conn) != "1":
        return _why(user, ip, loc, t, "автостарт вимкнено в «Правилах»")
    w = work_window(conn, user["id"], t)
    if not w:
        if absence_on(conn, user["id"], datetime.fromtimestamp(t).date()):
            return _why(user, ip, loc, t, "сьогодні відсутність (дей-офф, відпустка чи лікарняний)")
        return _why(user, ip, loc, t, f"зараз не робочий час (загальні години {work_hours_text(conn)}, або за особистим графіком)")
    start, end = w
    early = setting_int("auto_early_minutes", conn) * 60
    if conn.execute("select 1 from shifts where user_id = ? and started_at >= ? and started_at < ?",
                    (user["id"], start - early, end)).fetchone():
        return _why(user, ip, loc, t, "зміну на цей робочий час уже закрито, вдруге сама не починається")
    if not device_allowed(conn, user["id"], device, ip):
        _why(user, ip, loc, t, "пінг з іншого телефона, ніж прив'язаний до людини (див. картку працівника)")
        if not conn.execute("select 1 from conn_log where user_id = ? and event = 'denied' and details like '%other_device%'"
                            " and created_at > ?", (user["id"], t - 1800)).fetchone():  # у журнал не частіше раз на 30 хв
            log_clock(conn, user["id"], None, "denied", ip, loc["id"], False, device, {"reason": "other_device"})
        return {"other_device": True}
    if t < start:
        _early_seen[user["id"]] = t  # уже на місці, але робочий час ще не почався
        _why(user, ip, loc, t, f"прийшов раніше, зміна почнеться сама о {datetime.fromtimestamp(start):%H:%M}")
        return {"waiting_until": start}
    _presence.pop(user["id"], None)
    seen = _early_seen.pop(user["id"], 0)
    grace = max(setting_int("wifi_grace_minutes", conn) * 60, 180)
    began = start if seen >= start - early and t - start <= grace else t
    cur = conn.execute("insert into shifts(user_id, location_id, started_at, hourly_rate, last_seen_at, last_seen_ip, start_ip, device, note)"
                       " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (user["id"], loc["id"], began, user["hourly_rate"], t, ip, ip, device, "Автостарт по Wi-Fi"))
    log_clock(conn, user["id"], cur.lastrowid, "auto_start", ip, loc["id"], True, device)
    notify(conn, user["id"], "Зміну розпочато автоматично", f"{loc['name']}, з {datetime.fromtimestamp(began):%H:%M}")
    return {"auto_started": True}


_early_seen = {}


def _away(conn, shift, t, extra):
    """Застосунок повідомив, коли телефон справді втратив робочий Wi-Fi (away_since) — це перерва, навіть якщо
    телефон уже повернувся. Повертає мить початку відсутності або None."""
    try:
        since = int((extra or {}).get("away_since") or 0)
    except (TypeError, ValueError):
        return None
    if not since or since > t or since < shift["started_at"]:
        return None
    return since


def clock_action(conn, user, action, ip, device=None, kind=None, extra=None):
    """Усі дії зміни. Повертає dict {ok, error?, ...}; відмови теж пишуться в журнал підключень."""
    loc = match_location(conn, ip)
    shift = open_shift(conn, user["id"])
    t = now()
    # Зміну прив'язано до точки, де її почали: присутність під час зміни перевіряємо проти неї.
    same_net = bool(shift) and ip_in_location(conn, ip, shift["location_id"])

    def state(extra=None):
        s = open_shift(conn, user["id"])
        on_net = ip_in_location(conn, ip, s["location_id"]) if s else (loc is not None)
        return {"ok": True, "on_work_network": on_net, "location": loc["name"] if loc else None,
                "ip": ip, "shift": shift_stats(conn, s) if s else None,
                "heartbeat_seconds": setting_int("heartbeat_seconds", conn), "auto_start": setting("auto_start", conn) == "1",
                "why": None if s or user["id"] not in _presence or _presence[user["id"]]["t"] != t else _presence[user["id"]]["why"],
                **(extra or {})}

    if action in ("check", "auto", "heartbeat") and not shift:
        res = try_auto_start(conn, user, loc, ip, device, t)
        if action == "heartbeat" and not res:
            return {"ok": False, "error": "Немає відкритої зміни"}
        return state(res)

    if action in ("check", "auto"):
        if action == "auto" and shift:  # фоновий пінг застосунку = звичайний пінг присутності
            action = "heartbeat"
        else:
            return state()

    if action == "start":
        if user["status"] != "active":
            log_clock(conn, user["id"], None, "denied", ip, loc and loc["id"], False, device, {"reason": "status"})
            return {"ok": False, "error": "Ваш статус не дозволяє почати зміну"}
        if loc is None:
            log_clock(conn, user["id"], None, "denied", ip, None, False, device, {"reason": "not_on_work_network"})
            return {"ok": False, "error": f"Ви не в робочій мережі Wi-Fi (ваш IP {ip})", "ip": ip}
        if shift:
            return state({"already": True})  # повторне натискання при лагах не ламає нічого
        cur = conn.execute("insert into shifts(user_id, location_id, started_at, hourly_rate, last_seen_at, last_seen_ip, start_ip, device)"
                           " values (?, ?, ?, ?, ?, ?, ?, ?)",
                           (user["id"], loc["id"], t, user["hourly_rate"], t, ip, ip, device))
        log_clock(conn, user["id"], cur.lastrowid, "start", ip, loc["id"], True, device)
        return state()

    keep = setting("offline_keeps_counting", conn) == "1"  # втрата зв'язку не зупиняє зміну
    if action == "left":
        # Телефон вийшов з робочого Wi-Fi (застосунок повідомляє через мобільний інтернет): пауза з цієї миті
        since = (_away(conn, shift, t, extra) if shift else None) or t
        if shift and shift["status"] == "active" and not keep:
            start_wifi_pause(conn, shift, max(since, shift["started_at"]), "left", ip)
        return state()

    if not shift:
        if action == "stop":
            return state({"already": True})
        return {"ok": False, "error": "Немає відкритої зміни"}

    if action == "stop":
        # Анти-фрод: поза мережею точки (інша мережа чи підміна IP) зміна закривається часом останнього пінгу з точки.
        end = t if same_net else shift["last_seen_at"]
        close_shift(conn, shift["id"], end, "manual", ip)
        log_clock(conn, user["id"], shift["id"], "stop", ip, shift["location_id"], same_net, device,
                  None if same_net else {"ended_at_last_seen": end})
        closed = shift_stats(conn, conn.execute("select * from shifts where id = ?", (shift["id"],)).fetchone())
        return state({"closed_shift": closed, "off_network": not same_net})

    if action == "break_start":
        if shift["status"] != "active":
            return {"ok": False, "error": "Зміна не активна"}
        kind = "rest" if kind == "rest" else "lunch"
        if setting(kind + "_enabled", conn) != "1":
            return {"ok": False, "error": "Цей вид перерви вимкнено адміністратором"}
        paid = setting_int(kind + "_is_paid", conn)
        try:
            pct = max(float(setting(kind + "_rate_pct", conn).replace(",", ".")), 0)
        except ValueError:
            pct = 100.0
        conn.execute("insert into breaks(shift_id, kind, is_paid, rate_pct, started_at) values (?, ?, ?, ?, ?)",
                     (shift["id"], kind, paid, pct if paid else 0, t))
        conn.execute("update shifts set status = 'paused' where id = ?", (shift["id"],))
        log_clock(conn, user["id"], shift["id"], "break_start", ip, loc and loc["id"], True, device, {"kind": kind})
        return state()

    if action == "break_end":
        if shift["status"] != "paused":
            return state({"already": True})
        if not same_net:  # продовжити зміну можна лише з мережі своєї точки (консистентно зі stop/heartbeat)
            log_clock(conn, user["id"], shift["id"], "denied", ip, None, False, device, {"reason": "not_on_work_network", "action": "break_end"})
            return {"ok": False, "error": "Щоб продовжити зміну, підключіться до робочого Wi-Fi", "ip": ip}
        conn.execute("update breaks set ended_at = ? where shift_id = ? and ended_at is null", (t, shift["id"]))
        conn.execute("update shifts set status = 'active', last_seen_at = ?, last_seen_ip = ? where id = ?", (t, ip, shift["id"]))
        log_clock(conn, user["id"], shift["id"], "break_end", ip, shift["location_id"], True, device)
        return state()

    if action == "heartbeat":
        if not same_net:
            # Пінг з іншої мережі (зміна прив'язана до своєї точки, IP поза нею або підмінений)
            log_clock(conn, user["id"], shift["id"], "heartbeat_foreign_ip", ip, None, False, device)
            if shift["status"] == "active" and not keep:
                # Людина точно не на своїй точці: пауза з останнього пінгу з точки
                start_wifi_pause(conn, shift, shift["last_seen_at"], "left", ip)
            return state()
        away = _away(conn, shift, t, extra)
        b = conn.execute("select * from breaks where shift_id = ? and ended_at is null", (shift["id"],)).fetchone()
        if b and b["kind"] == "wifi_loss":
            forgive = setting_int("silence_forgive_minutes", conn) * 60
            # Простити «мовчання» можна лише застосунку: його телефон справді засинає і пінгує рідше.
            # Браузер не спить — закрита вкладка означає, що людина пішла. Застосунок без обмежень батареї
            # пінгує і в сні, тож його тиша — теж справжня втрата.
            sleepy = sleepy_shift(shift) and (device or "").startswith("app-")
            if sleepy and b["reason"] == "silence" and not away and t - b["started_at"] <= forgive:
                # Телефон лише спав (Wi-Fi не втрачав) і знову озвався з точки: людина весь час була тут
                conn.execute("delete from breaks where id = ?", (b["id"],))
                log_clock(conn, user["id"], shift["id"], "wifi_restored", ip, shift["location_id"], True, device, {"slept_seconds": t - b["started_at"]})
            else:
                # Застосунок каже, коли телефон справді вийшов: до цієї миті він спав на точці (в межах тієї ж поблажки)
                since = away if sleepy and away and away - b["started_at"] <= forgive else b["started_at"]
                conn.execute("update breaks set ended_at = ?, started_at = max(started_at, ?), reason = 'left' where id = ?",
                             (t, since, b["id"]))
                log_clock(conn, user["id"], shift["id"], "wifi_restored", ip, shift["location_id"], True, device)
                notify(conn, user["id"], "Зміну відновлено", "Ви знову в робочій мережі")
                notify_staff(conn, "Wi-Fi відновлено", f"{user['full_name']} знову на точці")
            conn.execute("update shifts set status = 'active' where id = ?", (shift["id"],))
        elif away and shift["status"] == "active" and t - away >= 60 and not keep:
            # Вийшов і повернувся, поки сервер чекав: відсутність — неоплачувана перерва
            conn.execute("insert into breaks(shift_id, kind, is_paid, rate_pct, started_at, ended_at, reason)"
                         " values (?, 'wifi_loss', 0, 0, ?, ?, 'left')", (shift["id"], away, t))
            log_clock(conn, user["id"], shift["id"], "wifi_lost", ip, shift["location_id"], False, device, {"away_since": away, "back": t})
        app_dev = (device or "").startswith("app-")
        conn.execute("update shifts set last_seen_at = ?, last_seen_ip = ?, device = coalesce(?, device), bg_ok = ? where id = ?",
                     (t, ip, device if app_dev else None,
                      int(bool(extra["unrestricted"])) if app_dev and "unrestricted" in (extra or {}) else shift["bg_ok"], shift["id"]))
        return state()

    return {"ok": False, "error": "Невідома дія"}


def sleepy_shift(shift):
    """Зміна з застосунку, телефон якого може заснути (з обмеженнями батареї): його тишу можна простити."""
    return (shift["device"] or "").startswith("app-") and not shift["bg_ok"]


def start_wifi_pause(conn, shift, since, reason, ip=None, log=True):
    """Пауза через Wi-Fi: 'left' — телефон точно поза точкою, 'silence' — просто довго не озивався."""
    conn.execute("insert into breaks(shift_id, kind, is_paid, rate_pct, started_at, reason) values (?, 'wifi_loss', 0, 0, ?, ?)",
                 (shift["id"], since, reason))
    conn.execute("update shifts set status = 'paused' where id = ?", (shift["id"],))
    who = conn.execute("select full_name from users where id = ?", (shift["user_id"],)).fetchone()
    notify(conn, shift["user_id"], "Зміну поставлено на паузу", "Поверніться в робочий Wi-Fi, щоб продовжити")
    notify_staff(conn, "Втрата Wi-Fi", f"{who[0] if who else ''}: зміна на паузі")
    if log:
        log_clock(conn, shift["user_id"], shift["id"], "wifi_lost", ip, shift["location_id"], False, None, {"reason": reason, "since": since})


def sweep(conn):
    """Фонова перевірка (кожні 15 с): втрата Wi-Fi, затягнута Wi-Fi пауза, забуті зміни."""
    t = now()
    grace = setting_int("wifi_grace_minutes", conn) * 60
    action = setting("wifi_loss_action", conn)
    changed = 0
    # Якщо сервер не працював (перезапуск, збій, сон комп'ютера), телефони не могли пінгувати.
    # Не карає людей паузою: хто був на зв'язку до простою, отримує новий відлік з цієї миті.
    alive = int(meta_get(conn, "alive_at", 0))
    if alive and t - alive > grace:
        n = conn.execute("update shifts set last_seen_at = ? where status = 'active' and last_seen_at >= ?",
                         (t, alive - grace)).rowcount
        if n:
            log_clock(conn, None, None, "server_downtime", None, None, True, None, {"seconds": t - alive, "shifts": n})
    meta_set(conn, "alive_at", t)
    sleep = max(setting_int("sleep_grace_minutes", conn) * 60, grace)
    pause_limit = setting_int("wifi_pause_close_minutes", conn) * 60
    after_end = setting_int("auto_close_minutes", conn) * 60
    keep = setting("offline_keeps_counting", conn) == "1"
    for s in conn.execute("select s.*, u.full_name from shifts s join users u on u.id = s.user_id"
                          " where s.status = 'active' and s.last_seen_at < ?", (t - grace,)).fetchall():
        if (s["device"] or "").startswith("app-") and s["last_seen_at"] >= t - (max(grace, 180) if s["bg_ok"] else sleep):
            # застосунок: телефон міг просто заснути, фонова служба пінгує рідше. Без обмежень батареї
            # застосунок пінгує щохвилини і в сні, тож 3 хв тиші — це вже вимкнений чи втрачений Wi-Fi
            continue
        w = work_window(conn, s["user_id"], s["started_at"]) if keep else None
        if w:
            # «Зміна рахується без зв'язку»: не ставимо на паузу, після кінця робочого часу закриваємо кінцем робочого часу
            if t < max(w[1], s["last_seen_at"]) + after_end:
                continue
            changed += 1
            end = max(w[1], s["last_seen_at"])
            close_shift(conn, s["id"], end, "auto_close")
            log_clock(conn, s["user_id"], s["id"], "auto_close", s["last_seen_ip"], s["location_id"], True, None,
                      {"ended_at": end, "offline_keeps_counting": True})
            notify(conn, s["user_id"], "Зміну закрито автоматично", f"Кінець о {datetime.fromtimestamp(end):%H:%M}")
            continue
        changed += 1
        if action == "close":
            close_shift(conn, s["id"], s["last_seen_at"], "wifi_lost")
            notify(conn, s["user_id"], "Зміну закрито", "Телефон відключився від робочого Wi-Fi")
            notify_staff(conn, "Зміну закрито: втрата Wi-Fi", s["full_name"])
        else:
            start_wifi_pause(conn, s, s["last_seen_at"], "silence", s["last_seen_ip"], log=False)
        log_clock(conn, s["user_id"], s["id"], "wifi_lost", s["last_seen_ip"], s["location_id"], False, None,
                  {"action": action})
    forgive = setting_int("silence_forgive_minutes", conn) * 60
    for b in conn.execute("select b.id, b.shift_id, b.started_at, b.reason, s.user_id, s.started_at as shift_start, s.last_seen_ip,"
                          " s.location_id, s.device, s.bg_ok from breaks b join shifts s on s.id = b.shift_id"
                          " where b.kind = 'wifi_loss' and b.ended_at is null and s.status = 'paused' and b.started_at < ?",
                          (t - min(pause_limit, after_end),)).fetchall():
        # Пішов після кінця зміни: чекаємо auto_close_minutes (раптом повернеться), у робочий час — wifi_pause_close_minutes.
        w = work_window(conn, b["user_id"], b["shift_start"])
        ended = w is not None and b["started_at"] >= w[1] - 15 * 60
        # Телефон лише замовк (міг заснути) — чекаємо довше: озветься з точки, і час зарахується
        limit = after_end if ended else (max(pause_limit, forgive) if b["reason"] == "silence" and sleepy_shift(b) else pause_limit)
        if t - b["started_at"] < limit:
            continue
        changed += 1
        conn.execute("delete from breaks where id = ?", (b["id"],))
        # Закривається часом, коли телефон востаннє був у робочому Wi-Fi: час очікування не оплачується
        close_shift(conn, b["shift_id"], b["started_at"], "auto_close" if ended else "wifi_lost")
        if ended:
            log_clock(conn, b["user_id"], b["shift_id"], "auto_close", b["last_seen_ip"], b["location_id"], True, None,
                      {"ended_at": b["started_at"]})
            notify(conn, b["user_id"], "Зміну закрито автоматично", f"Кінець о {datetime.fromtimestamp(b['started_at']):%H:%M}, коли телефон вийшов з робочого Wi-Fi")
        else:
            notify(conn, b["user_id"], "Зміну закрито", "Довго не було зв'язку з робочим Wi-Fi")
    max_len = setting_int("max_shift_hours", conn) * 3600
    for s in conn.execute("select * from shifts where status != 'closed' and started_at < ?", (t - max_len,)).fetchall():
        changed += 1
        close_shift(conn, s["id"], s["last_seen_at"], "auto_max_length")
        notify(conn, s["user_id"], "Зміну закрито автоматично", "Подайте запит на редагування, якщо час неправильний")
    conn.commit()
    return changed


def backup_mirror_dir():
    """Друга папка для копій (Google Диск, флешка). Задається в «Правилах» або змінною CREWHUB_BACKUP_MIRROR."""
    if os.environ.get("CREWHUB_BACKUP_MIRROR"):
        return os.environ["CREWHUB_BACKUP_MIRROR"]
    try:
        conn = connect()
        try:
            return meta_get(conn, "backup_mirror") or None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _prune(folder):
    files = sorted(f for f in os.listdir(folder) if f.startswith("crewhub_") and f.endswith((".zip", ".db")))
    for old in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass


def backup_db(dest=None):
    """Повна копія: база (цілісний знімок, працює і під час запису) + усі особові папки, одним zip
    у backups/crewhub_РРРР-ММ-ДД_ГГХХ.zip. Копія дублюється в другу папку (Google Диск), якщо вона задана."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = dest or os.path.join(BACKUP_DIR, f"crewhub_{datetime.now():%Y-%m-%d_%H%M%S}.zip")
    snap = dest + ".db.tmp"
    src = connect()
    out = sqlite3.connect(snap)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    try:
        with zipfile.ZipFile(dest + ".tmp", "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "crewhub.db")
            if os.path.isdir(PEOPLE_DIR):
                for root, _dirs, files in os.walk(PEOPLE_DIR):
                    for f in files:
                        if not f.endswith(".tmp"):
                            full = os.path.join(root, f)
                            z.write(full, os.path.join("people", os.path.relpath(full, PEOPLE_DIR)))
        os.replace(dest + ".tmp", dest)
    finally:
        if os.path.exists(snap):
            os.remove(snap)
    _prune(BACKUP_DIR)
    mirror = backup_mirror_dir()
    if mirror:
        try:
            import shutil
            os.makedirs(mirror, exist_ok=True)
            shutil.copyfile(dest, os.path.join(mirror, os.path.basename(dest)))
            _prune(mirror)
            RECOVERED["mirror_error"] = None
        except OSError as e:
            RECOVERED["mirror_error"] = str(e)  # показується в «Правилах»; локальна копія все одно є
    return dest


def maintenance():
    """Раз на добу: чистимо старі журнали, оновлюємо статистику індексів, стискаємо WAL-файл."""
    with _lock:
        conn = connect()
        try:
            conn.execute("delete from conn_log where created_at < ?", (now() - LOG_KEEP_DAYS * 86400,))
            conn.execute("delete from notifications where is_read = 1 and created_at < ?", (now() - 60 * 86400,))
            conn.commit()
            conn.execute("pragma optimize")
            conn.execute("pragma wal_checkpoint(truncate)")
        finally:
            conn.close()


def check_db():
    """Перевірка бази перед запуском. Пошкоджену відкладає вбік і відновлює з останньої копії.
    Повертає None, якщо все добре, або текст, що сталося."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        ok = conn.execute("pragma quick_check").fetchone()[0] == "ok"
        conn.close()
    except sqlite3.DatabaseError:
        ok = False
    if ok:
        return None
    broken = f"{DB_PATH}.broken-{datetime.now():%Y%m%d_%H%M%S}"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            os.replace(DB_PATH + suffix, broken + suffix)
    copies = list_backups()
    RECOVERED["from_backup"] = True  # людей і їхні зміни profiles.py візьме з особових папок — вони свіжіші за копію
    if not copies:
        return f"База пошкоджена і відкладена в {broken}. Копій немає: створено нову базу, люди відновлюються з папок."
    path = os.path.join(BACKUP_DIR, copies[0]["name"])
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z, z.open("crewhub.db") as src, open(DB_PATH, "wb") as out:
            out.write(src.read())
    else:
        import shutil
        shutil.copyfile(path, DB_PATH)
    return f"База була пошкоджена (збережена як {broken}). Налаштування відновлено з копії {copies[0]['name']}, люди — з їхніх папок."


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.startswith("crewhub_") and f.endswith((".zip", ".db")):
            st = os.stat(os.path.join(BACKUP_DIR, f))
            out.append({"name": f, "size": f"{st.st_size / 1048576:.1f} МБ" if st.st_size > 1048576 else f"{st.st_size // 1024} КБ",
                        "mtime": int(st.st_mtime)})
    return out


def start_backups(every_hours=24):
    """Копія при запуску і далі раз на добу (перевіряємо щогодини, чи сьогоднішня вже є)."""
    def loop():
        while True:
            try:
                today = f"crewhub_{date.today():%Y-%m-%d}"
                if not any(b["name"].startswith(today) for b in list_backups()):
                    with _people_lock:
                        backup_db()
                    maintenance()
            except Exception as e:  # noqa: BLE001
                print("backup error:", e)
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True, name="crewhub-backup").start()


def start_sweeper(interval=15):
    def loop():
        while True:
            try:
                with _lock:
                    conn = connect()
                    sweep(conn)
                    conn.close()
            except Exception as e:  # noqa: BLE001 — фоновий потік не має падати
                print("sweep error:", e)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="crewhub-sweeper").start()


def live_monitor(conn):
    t = now()
    late = setting_int("late_threshold_minutes", conn) * 60
    rows = []
    for u in conn.execute("select * from users where status not in ('dismissed', 'pending') order by full_name"):
        s = open_shift(conn, u["id"])
        st = shift_stats(conn, s) if s else None
        sched = None
        if not s:
            sched = conn.execute(
                "select sc.*, l.name as location from schedule sc left join locations l on l.id = sc.location_id"
                " where sc.user_id = ? and ? between sc.starts_at - 3600 and sc.ends_at"
                " and not exists (select 1 from shifts x where x.user_id = sc.user_id and x.started_at >= sc.starts_at - 7200)"
                " order by sc.starts_at limit 1", (u["id"], t)).fetchone()
        if st and st["open_break"] and st["open_break"]["kind"] == "wifi_loss":
            state = "wifi_lost"
        elif st and st["status"] == "paused":
            state = "on_break"
        elif st:
            state = "on_site"
        elif absence_on(conn, u["id"], date.today()):
            state = "absent"
        elif u["id"] in _presence and t - _presence[u["id"]]["t"] < 300 and _presence[u["id"]]["loc"]:
            state = "idle_wifi"  # телефон у робочому Wi-Fi, але зміни немає (причина — у рядку)
        elif sched and t > sched["starts_at"] + late:
            state = "late"
        elif sched:
            state = "scheduled"
        else:
            state = "off"
        loc = None
        if st and st["location_id"]:
            loc = conn.execute("select name from locations where id = ?", (st["location_id"],)).fetchone()[0]
        elif sched:
            loc = sched["location"]
        pr = _presence.get(u["id"]) if not st else None
        if pr and t - pr["t"] > 900:
            pr = None
        if state == "idle_wifi":
            loc = pr["loc"]
        rows.append({"user": u, "state": state, "shift": st, "schedule": sched, "location": loc, "presence": pr})
    order = ["wifi_lost", "late", "idle_wifi", "on_break", "on_site", "scheduled", "absent", "off"]
    rows.sort(key=lambda r: order.index(r["state"]))
    return rows


def absence_on(conn, user_id, day, pending=False):
    """Лікарняний, дей-офф чи відпустка людини на цей день (схвалені; з pending=True і ті, що чекають)."""
    statuses = ("approved", "pending") if pending else ("approved",)
    return conn.execute(
        f"select * from requests where user_id = ? and type in {ABSENCE_TYPES} and status in ({','.join('?' * len(statuses))})"
        " and starts_on <= ? and ends_on >= ? order by id desc limit 1",
        (user_id, *statuses, day.isoformat(), day.isoformat())).fetchone()


def apply_absence(conn, r):
    """Схвалена відсутність прибирає заплановані зміни людини на ці дні."""
    start, end = day_bounds(date.fromisoformat(r["starts_on"]), date.fromisoformat(r["ends_on"]))
    conn.execute("delete from schedule where user_id = ? and starts_at >= ? and starts_at < ?", (r["user_id"], start, end))


DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS_NOM = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень",
              "Вересень", "Жовтень", "Листопад", "Грудень"]


def hours_by_day(conn, user_id=None, days=7):
    """Години по днях для міні-графіка (останні N днів, зміни рахуються за днем початку)."""
    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        start, end = day_bounds(d, d)
        q, args = "select * from shifts where started_at >= ? and started_at < ?", [start, end]
        if user_id:
            q += " and user_id = ?"
            args.append(user_id)
        secs = sum(shift_stats(conn, x)["worked_seconds"] for x in conn.execute(q, args))
        out.append({"label": DAY_SHORT[d.weekday()], "date": d.strftime("%d.%m"), "hours": round(secs / 3600, 1),
                    "today": d == today})
    top = max([x["hours"] for x in out] + [1])
    for x in out:
        x["pct"] = round(x["hours"] / top * 100)
    return out


def month_range(month):
    """'2026-10' -> (date(2026,10,1), date(2026,10,31))"""
    try:
        d_from = date.fromisoformat((month or "") + "-01")
    except ValueError:
        d_from = date.today().replace(day=1)
    d_to = (d_from.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return d_from, d_to


def month_nav(d_from):
    prev = (d_from - timedelta(days=1)).strftime("%Y-%m")
    nxt = (d_from + timedelta(days=32)).strftime("%Y-%m")
    return {"month": d_from.strftime("%Y-%m"), "title": f"{MONTHS_NOM[d_from.month - 1]} {d_from.year}", "prev": prev, "next": nxt}


def timesheet(conn, user_id, month):
    d_from, d_to = month_range(month)
    start, end = day_bounds(d_from, d_to)
    shifts = [shift_stats(conn, s) for s in conn.execute(
        "select s.*, l.name as location from shifts s left join locations l on l.id = s.location_id"
        " where s.user_id = ? and s.started_at >= ? and s.started_at < ? order by s.started_at desc", (user_id, start, end))]
    adj = conn.execute("select * from adjustments where user_id = ? and day between ? and ? order by day desc",
                       (user_id, d_from.isoformat(), d_to.isoformat())).fetchall()
    totals = {"shifts": len(shifts), "seconds": sum(s["worked_seconds"] for s in shifts),
              "earned": round(sum(s["earnings"] for s in shifts), 2), "adjust": round(sum(a["amount"] for a in adj), 2)}
    u = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
    dayoffs = dayoff_pay(conn, u, d_from, d_to) if u else []
    totals["dayoff"] = round(sum(x["amount"] for x in dayoffs), 2)
    totals["total"] = round(totals["earned"] + totals["dayoff"] + totals["adjust"], 2)
    return {"shifts": shifts, "adjustments": adj, "dayoffs": dayoffs, "totals": totals, "nav": month_nav(d_from)}


def default_week_day():
    """У суботу й неділю графік одразу показує наступний робочий тиждень."""
    today = date.today()
    return today + timedelta(days=7 - today.weekday()) if today.weekday() >= 5 and request.args.get("weekend") != "1" else today


PRIVATE_ABSENCES = ("vacation", "sick_leave")  # бачать лише модератори й адміністратор (і сама людина)


MONTH_DAY_LETTER = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def month_grid(conn, first, viewer=None):
    """Сітка на місяць: люди × дні. Клітинка — зміна за графіком (години) або відсутність; щоб швидко розставити дні всій команді."""
    first = first.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    days = [first + timedelta(days=i) for i in range((nxt - first).days)]
    start, end = day_bounds(days[0], days[-1])
    users = conn.execute("select id, full_name, job_title, photo_url from users where status not in ('dismissed', 'pending') order by full_name").fetchall()
    cells = {}
    for sc in conn.execute("select * from schedule where starts_at >= ? and starts_at < ? order by starts_at", (start, end)):
        d = datetime.fromtimestamp(sc["starts_at"])
        e = datetime.fromtimestamp(sc["ends_at"])
        hm = lambda t: str(t.hour) + (f":{t:%M}" if t.minute else "")  # noqa: E731
        cells[(sc["user_id"], d.date().isoformat())] = f"{hm(d)}–{hm(e)}"
    absences = {}
    for r in conn.execute(f"select * from requests where type in {ABSENCE_TYPES} and status in ('approved', 'pending')"
                          " and starts_on <= ? and ends_on >= ? order by status = 'approved'", (days[-1].isoformat(), days[0].isoformat())):
        if viewer and r["user_id"] != viewer["id"] and r["type"] in PRIVATE_ABSENCES:
            continue
        for d in days:
            if r["starts_on"] <= d.isoformat() <= r["ends_on"]:
                absences[(r["user_id"], d.isoformat())] = r
    count = {u["id"]: sum(1 for (uid, _) in cells if uid == u["id"]) for u in users}
    return {"days": [{"date": d, "iso": d.isoformat(), "n": d.day, "label": MONTH_DAY_LETTER[d.weekday()],
                      "weekend": d.weekday() >= 5, "today": d == date.today()} for d in days],
            "users": users, "cells": cells, "absences": absences, "count": count, "month": first.strftime("%Y-%m"),
            "title": f"{MONTHS_NOM[first.month - 1]} {first.year}",
            "prev": (first - timedelta(days=1)).strftime("%Y-%m"), "next": nxt.strftime("%Y-%m")}


def parse_month(value):
    try:
        return date.fromisoformat((value or "") + "-01")
    except ValueError:
        return date.today().replace(day=1)


def default_slot(conn, user_id, day):
    """Зміна за замовчуванням на день: робочі години з налаштувань, нічна переходить через північ."""
    ws, we = hhmm_minutes(setting("work_start", conn), 540), hhmm_minutes(setting("work_end", conn), 1080)
    base = int(datetime.combine(day, datetime.min.time()).timestamp())
    start = base + ws * 60
    end = base + we * 60 + (86400 if we <= ws else 0)
    u = conn.execute("select location_id from users where id = ?", (user_id,)).fetchone()
    loc = (u and u["location_id"]) or (conn.execute("select id from locations where is_active = 1 order by id limit 1").fetchone() or [None])[0]
    return start, end, loc


def week_grid(conn, monday, weekend=False, viewer=None):
    """viewer — працівник без прав модератора: чужі відпустки й лікарняні йому не показуються, дей-оффи бачать усі."""
    days = [monday + timedelta(days=i) for i in range(7 if weekend else 5)]
    start, end = day_bounds(days[0], days[-1])
    users = conn.execute("select id, full_name, job_title, photo_url from users where status not in ('dismissed', 'pending') order by full_name").fetchall()
    cells = {}
    for sc in conn.execute("select sc.*, l.name as location from schedule sc left join locations l on l.id = sc.location_id"
                           " where sc.starts_at >= ? and sc.starts_at < ? order by sc.starts_at", (start, end)):
        d = datetime.fromtimestamp(sc["starts_at"]).date().isoformat()
        cells.setdefault((sc["user_id"], d), []).append(sc)
    absences = {}
    for r in conn.execute(f"select * from requests where type in {ABSENCE_TYPES} and status in ('approved', 'pending')"
                          " and starts_on <= ? and ends_on >= ? order by status = 'approved'",
                          (days[-1].isoformat(), days[0].isoformat())):
        if viewer and r["user_id"] != viewer["id"] and r["type"] in PRIVATE_ABSENCES:
            continue
        for d in days:
            if r["starts_on"] <= d.isoformat() <= r["ends_on"]:
                absences[(r["user_id"], d.isoformat())] = r
    hours = {u["id"]: round(sum((x["ends_at"] - x["starts_at"]) for (uid, _), xs in cells.items() if uid == u["id"] for x in xs) / 3600, 1)
             for u in users}
    return {"days": [{"date": d, "iso": d.isoformat(), "label": DAY_SHORT[d.weekday()], "today": d == date.today(),
                      "weekend": d.weekday() >= 5} for d in days],
            "users": users, "cells": cells, "hours": hours, "absences": absences, "weekend": weekend,
            "prev": (monday - timedelta(days=7)).isoformat(), "next": (monday + timedelta(days=7)).isoformat(),
            "title": f"{days[0]:%d.%m} – {days[-1]:%d.%m.%Y}"}


def next_scheduled(conn, user_id):
    return conn.execute("select sc.*, l.name as location from schedule sc left join locations l on l.id = sc.location_id"
                        " where sc.user_id = ? and sc.ends_at > ? order by sc.starts_at limit 1", (user_id, now())).fetchone()


def person_dir(conn, uid):
    """Особова папка людини (створюється, якщо її ще немає)."""
    import profiles
    return profiles.ensure_folder(conn, uid)


def avatar_file(folder_path):
    for ext in PHOTO_EXT:
        p = os.path.join(folder_path, f"avatar.{ext}")
        if os.path.exists(p):
            return p
    return None


PHOTO_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
MAX_PHOTO_BYTES = 8 * 1024 * 1024


def set_avatar_blob(conn, uid, data, ext):
    """Зберегти аватарку в базі (єдине джерело правди) і продублювати файлом у папку людини."""
    ext = "jpg" if ext == "jpeg" else ext
    conn.execute("insert into avatars(user_id, data, mime, ext, updated_at) values (?, ?, ?, ?, ?)"
                 " on conflict(user_id) do update set data = excluded.data, mime = excluded.mime, ext = excluded.ext, updated_at = excluded.updated_at",
                 (uid, data, PHOTO_MIME[ext], ext, now()))
    try:
        folder = person_dir(conn, uid)
        for old in (avatar_file(folder),) if avatar_file(folder) else ():
            os.remove(old)
        with open(os.path.join(folder, f"avatar.{ext}"), "wb") as f:
            f.write(data)
    except OSError:
        pass  # копія у папці не критична — база лишається джерелом правди


def save_photo(conn, actor, uid, file):
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in PHOTO_EXT:
        return "Підтримуються JPG, PNG, WEBP або GIF"
    data = file.read()
    if not data:
        return "Порожній файл"
    if len(data) > MAX_PHOTO_BYTES:
        return "Файл завеликий (до 8 МБ)"
    set_avatar_blob(conn, uid, data, ext)
    url = f"/avatar/{uid}?v={now()}"
    old = conn.execute("select photo_url from users where id = ?", (uid,)).fetchone()
    conn.execute("update users set photo_url = ? where id = ?", (url, uid))
    audit(conn, actor["id"], "UPDATE", "users", uid, uid, {"photo_url": [old[0] if old else None, url]})
    return None


# ---------------------------------------------------------------------------
# Форматування для шаблонів
# ---------------------------------------------------------------------------
MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


def _plural(n, one, few, many):
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def tenure_text(hire_iso, today=None):
    """«В команді з травня 2025 (1 рік 5 міс.)»"""
    if not hire_iso:
        return ""
    h = date.fromisoformat(hire_iso)
    today = today or date.today()
    months = max((today.year - h.year) * 12 + today.month - h.month - (1 if today.day < h.day else 0), 0)
    y, m = divmod(months, 12)
    parts = ([f"{y} {_plural(y, 'рік', 'роки', 'років')}"] if y else []) + ([f"{m} міс."] if m or not y else [])
    return f"В команді з {MONTHS_GEN[h.month - 1]} {h.year} ({' '.join(parts)})"


@app.template_filter("dt")
def fmt_dt(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


@app.template_filter("tm")
def fmt_tm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "…"


@app.template_filter("dtinput")
def fmt_dtinput(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M") if ts else ""


@app.template_filter("dur")
def fmt_dur(secs):
    secs = int(secs or 0)
    return f"{secs // 3600} год {secs % 3600 // 60:02d} хв"


@app.template_filter("money")
def fmt_money(v):
    return f"{(v or 0):,.2f}".replace(",", " ") + " " + setting("currency")


@app.template_filter("fromjson")
def fmt_fromjson(v):
    return json.loads(v) if v else {}


def parse_dtinput(v):
    return int(datetime.strptime(v, "%Y-%m-%dT%H:%M").timestamp()) if v else None


@app.context_processor
def _globals():
    u = current_user()
    unread = []
    pending = 0
    if u:
        unread = db().execute("select * from notifications where user_id = ? and is_read = 0 order by id desc limit 5",
                              (u["id"],)).fetchall()
        if u["role"] == "teamlead":
            pending = db().execute("select count(*) from requests where status = 'pending' and type = 'day_off'").fetchone()[0]
        elif u["role"] in STAFF_ROLES:
            # Лічильник на «Запитах»: заявки працівників + нові реєстрації, що чекають підтвердження
            pending = db().execute("select (select count(*) from requests where status = 'pending')"
                                   " + (select count(*) from users where status = 'pending')").fetchone()[0]
    return {"me": u, "ROLES": ROLES, "STAFF_ROLES": STAFF_ROLES, "DAYOFF_ROLES": DAYOFF_ROLES, "STATUSES": STATUSES, "REQUEST_TYPES": REQUEST_TYPES,
            "REQUEST_STATUSES": REQUEST_STATUSES, "EVENTS": EVENTS, "tenure_text": tenure_text,
            "company": setting("company_name"), "currency": setting("currency"), "notifications": unread,
            "pending_count": pending, "endpoint": request.endpoint, "now_ts": now(),
            "default_rate": float(setting("default_hourly_rate")),
            "in_app": "CrewHubApp" in (request.headers.get("User-Agent") or ""),
            "apk_ready": os.path.exists(APK_PATH), "demo_accounts": DEMO_ACCOUNTS,
            "csrf_token": session.get("csrf", "")}


# ---------------------------------------------------------------------------
# Авторизація
# ---------------------------------------------------------------------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if "user" not in g:
        g.user = db().execute("select * from users where id = ? and status not in ('dismissed', 'pending')", (uid,)).fetchone()
    return g.user


def login_required(staff=False, admin=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "Потрібно увійти"}), 401
                return redirect(url_for("login"))
            if staff and u["role"] not in STAFF_ROLES:
                abort(403)
            if admin and u["role"] != "admin":
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def own_record_blocked(actor, uid):
    """Розділення обов'язків: модератор не змінює власні зміни, ставку, бонуси й не погоджує свої запити —
    це робить адміністратор (або інший модератор щодо нього)."""
    try:
        return actor["role"] != "admin" and int(uid) == actor["id"]
    except (TypeError, ValueError):
        return False


OWN_RECORD_MSG = "Свої зміни, ставку, бонуси й запити погоджує адміністратор"


@app.template_filter("walink")
def wa_link(v):
    """Номер WhatsApp → посилання wa.me (лише цифри). Український 0XX… перетворюємо на 380XX…."""
    digits = re.sub(r"\D", "", v or "")
    if digits.startswith("0"):
        digits = "38" + digits
    return "https://wa.me/" + digits if len(digits) >= 10 else None


@app.template_filter("phone")
def fmt_phone(v):
    return format_phone(v)


def find_login(conn, ident):
    """Вхід за номером телефону; службові логіни (admin, moder) теж працюють."""
    ident = (ident or "").strip()
    phone = normalize_phone(ident) if not ident.isalpha() else None
    if phone:
        u = conn.execute("select * from users where phone = ?", (phone,)).fetchone()
        if u:
            return u
    return conn.execute("select * from users where username = ?", (ident,)).fetchone()


SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


@app.before_request
def _csrf_guard():
    """Захист від CSRF. Три рівні:
      1) кука сесії — SameSite=Lax (налаштовано вище), не їде на сторонні сайти;
      2) на кожен небезпечний метод звіряємо Origin/Referer із нашим хостом;
      3) для звичайних HTML-форм ще й перевіряємо токен session['csrf'].
    API застосунку (/api/*, JSON) токен не вимагає: фонова служба телефона не браузер і куку
    чужого сайту не підставить, а свій веб-клієнт шле запити лише зі свого ж Origin."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    if not CSRF_ON or request.method in SAFE_METHODS:
        return
    host = request.host
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    # Крос-сайтовий POST з браузера завжди несе Origin (а старіші — Referer); нема жодного — це не браузер
    # (застосунок, curl): куку чужого сайту такий клієнт не підставить, тож пропускаємо далі.
    bad = False
    if origin:
        bad = origin == "null" or urlsplit(origin).netloc != host
    elif referer:
        bad = urlsplit(referer).netloc != host
    if bad:
        return _csrf_fail()
    ctype = (request.content_type or "").split(";")[0].strip().lower()
    if not request.path.startswith("/api/") and ctype in FORM_TYPES:
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
        if not secrets.compare_digest(str(sent), str(session.get("csrf") or "")):
            return _csrf_fail()


def _csrf_fail():
    if request.path.startswith("/api/") or request.headers.get("X-CrewHub-Nav"):
        return jsonify({"ok": False, "error": "Перевірка безпеки не пройдена. Оновіть сторінку."}), 403
    return "Перевірка безпеки не пройдена. Оновіть сторінку і спробуйте ще раз.", 400


@app.before_request
def _remember_login():
    # Немає активної сесії, але є cookie «запам'ятати мене» → входимо автоматично (не за IP).
    if session.get("uid") or request.endpoint == "static":
        return
    cookie = request.cookies.get(REMEMBER_COOKIE)
    if not cookie:
        return
    with _lock:
        conn = db()
        uid = remember_user(conn, cookie, client_ip())
        conn.commit()
    if uid:
        session["uid"] = uid
        session.permanent = True
        g.pop("user", None)


@app.before_request
def _force_password_change():
    # Після скидання паролю адміном працівник спершу задає свій
    u = current_user() if session.get("uid") else None
    if u and u["must_change_password"] and request.endpoint not in ("password_change", "logout", "static"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Спершу змініть тимчасовий пароль"}), 403
        return redirect(url_for("password_change"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ident = (request.form.get("username") or "").strip()
        u = find_login(db(), ident)
        acct = f"u{u['id']}" if u else (normalize_phone(ident) or ident.lower())[:40]
        ip = client_ip()
        if wait := login_blocked(acct, ip):
            flash(wait_text(wait))
            return render_template("login.html", ip=ip, registration_open=setting("registration_open") == "1"), 429
        if not (u and verify_password(u["password_hash"], request.form.get("password", ""))):
            login_failed(acct, ip)
            u = None
        if u:
            _throttle.clear(f"login:{acct}:{ip}")
            if u["status"] == "pending":
                flash("Акаунт ще не підтверджено: заявка чекає на перевірку адміністратора або модератора")
            elif u["status"] == "dismissed":
                flash("Акаунт вимкнено")
            else:
                session.clear()
                session["uid"] = u["id"]
                session.permanent = True
                resp = redirect(url_for("index"))
                # «Запам'ятати мене»: один раз увійшов — далі входить сам, навіть якщо IP зміниться
                with _lock:
                    conn = db()
                    val = issue_remember(conn, u["id"], device_key(), device_name(), client_ip())
                    conn.commit()
                return set_remember_cookie(resp, val)
        else:
            flash("Невірний номер телефону або пароль")
    return render_template("login.html", ip=client_ip(), registration_open=setting("registration_open") == "1")


@app.route("/register", methods=["GET", "POST"])
def register():
    if setting("registration_open") != "1":
        flash("Реєстрацію вимкнено. Акаунт створює адміністратор")
        return redirect(url_for("login"))
    form = request.form
    if request.method == "POST":
        key = f"register:{client_ip()}"
        if _throttle.recent(key, 3600)[0] >= 5:
            flash("Забагато реєстрацій з цієї мережі. Спробуйте пізніше або попросіть адміністратора")
            return render_template("register.html", done=False, form=form), 429
        _throttle.add(key)
        name = " ".join(form.get("full_name", "").split())
        phone = normalize_phone(form.get("phone"))
        pw = form.get("password", "")
        error = None
        if len(name) < 3 or len(name.split()) < 2:
            error = "Вкажіть ім'я і прізвище"
        elif not phone:
            error = "Невірний номер телефону. Приклад: 067 123 45 67"
        elif len(pw) < 6:
            error = "Пароль має бути не коротший за 6 символів"
        elif pw != form.get("password2"):
            error = "Паролі не збігаються"
        with _lock:
            conn = db()
            if not error and phone_taken(conn, phone):
                error = "Цей номер уже зареєстровано. Увійдіть або попросіть адміністратора скинути пароль"
            if not error:
                try:
                    cur = conn.execute("insert into users(username, password_hash, full_name, phone, hourly_rate, status, hire_date)"
                                       " values (?, ?, ?, ?, ?, 'pending', ?)",
                                       (phone, hash_password(pw), name, phone, float(setting("default_hourly_rate", conn)),
                                        date.today().isoformat()))
                except sqlite3.IntegrityError:
                    error = "Цей номер уже зареєстровано"
                else:
                    audit(conn, None, "INSERT", "users", cur.lastrowid, cur.lastrowid,
                          {"registration": [None, f"{name} {format_phone(phone)}"], "ip": [None, client_ip()]})
                    # Телефон, з якого зареєструвались, стає телефоном людини: з нього вмикається автозміна
                    remember_device(conn, cur.lastrowid, device_key(form.get("device_id")), client_ip())
                    notify_staff(conn, "Нова реєстрація", f"{name}, {format_phone(phone)} чекає підтвердження")
                    conn.commit()
                    return render_template("register.html", done=True, name=name)
        flash(error)
    return render_template("register.html", done=False, form=form)


@app.route("/password", methods=["GET", "POST"])
@login_required()
def password_change():
    u = current_user()
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) < 6:
            flash("Пароль має бути не коротший за 6 символів")
        elif pw != request.form.get("password2"):
            flash("Паролі не збігаються")
        elif verify_password(u["password_hash"], pw):
            flash("Новий пароль має відрізнятися від тимчасового")
        else:
            with _lock:
                conn = db()
                conn.execute("update users set password_hash = ?, must_change_password = 0 where id = ?",
                             (hash_password(pw), u["id"]))
                revoke_remember(conn, u["id"])
                val = issue_remember(conn, u["id"], device_key(), device_name(), client_ip())
                audit(conn, u["id"], "UPDATE", "users", u["id"], u["id"], {"password": ["тимчасовий", "***"]})
                conn.commit()
            flash("Пароль змінено")
            return set_remember_cookie(redirect(url_for("index")), val)
    return render_template("password.html", guest_page=True)


@app.route("/app")
def download_apk():
    """Сторінка входу дає посилання сюди: телефон завантажує застосунок прямо з сервера."""
    if not os.path.exists(APK_PATH):
        abort(404)
    return send_file(APK_PATH, mimetype="application/vnd.android.package-archive", as_attachment=True,
                     download_name="CrewHub.apk")


@app.route("/app/demo")
def download_demo_apk():
    """Демо-застосунок (без сервера), якщо його поклали поруч: static/CrewHub_Demo.apk."""
    path = os.path.join(BASE_DIR, "static", "CrewHub_Demo.apk")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/vnd.android.package-archive", as_attachment=True,
                     download_name="CrewHub_Demo.apk")


@app.route("/logout")
def logout():
    resp = redirect(url_for("login"))
    cookie = request.cookies.get(REMEMBER_COOKIE)
    if cookie:
        with _lock:
            conn = db()
            forget_remember(conn, cookie)
            conn.commit()
    session.clear()
    return clear_remember_cookie(resp)


@app.route("/")
@login_required()
def index():
    return redirect(url_for("admin" if current_user()["role"] != "employee" else "me"))


@app.post("/notifications/read")
@login_required()
def notifications_read():
    with _lock:
        db().execute("update notifications set is_read = 1 where user_id = ?", (current_user()["id"],))
        db().commit()
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------------------
# Працівник
# ---------------------------------------------------------------------------
@app.post("/api/clock/<action>")
@login_required()
def api_clock(action):
    body = request.get_json(silent=True) or {}
    with _lock:
        conn = db()
        res = clock_action(conn, current_user(), action, client_ip(), device_key(body.get("device_id")), body.get("kind"), body)
        conn.commit()
    return jsonify(res)


@app.route("/me")
@login_required()
def me():
    conn = db()
    u = current_user()
    tab = request.args.get("tab", "clock")
    today = date.today()
    ctx = {"tab": tab}
    with _lock:
        expire_grants(conn)
    if tab == "clock":
        start, _ = day_bounds(today, today)
        closed = conn.execute("select * from shifts where user_id = ? and status = 'closed' and started_at >= ?",
                              (u["id"], start)).fetchall()
        ctx["today_closed"] = round(sum(shift_stats(conn, s)["earnings"] for s in closed), 2)
        month = payroll(conn, today.replace(day=1), today, u["id"])
        s = open_shift(conn, u["id"])
        open_earn = shift_stats(conn, s)["earnings"] if s else 0
        ctx["month_closed"] = round((month[0]["total"] if month else 0) - open_earn, 2)
        ctx["week"] = hours_by_day(conn, u["id"])
        ctx["next_shift"] = next_scheduled(conn, u["id"])
        ctx["shift_target"] = 8 * 3600
        if ctx["next_shift"]:
            ctx["shift_target"] = ctx["next_shift"]["ends_at"] - ctx["next_shift"]["starts_at"]
        ctx["absence_today"] = absence_on(conn, u["id"], today)
        ctx["break_rules"] = {k: {"enabled": setting(k + "_enabled", conn) == "1", "paid": setting(k + "_is_paid", conn) == "1",
                                  "pct": setting(k + "_rate_pct", conn)} for k in ("lunch", "rest")}
        ctx["pay_rules"] = pay_rules(conn)
        ctx["settings_auto"] = setting("auto_start", conn) == "1"
        ctx["today_iso"] = today.isoformat()
        ctx["dayoff_open_until"] = dayoff_campaign_until(conn)
        ctx["dayoff_req"] = conn.execute("select * from requests where user_id = ? and type = 'day_off' and status in ('pending', 'granted')"
                                         " order by id desc limit 1", (u["id"],)).fetchone()
    elif tab == "timesheet":
        ctx.update(timesheet(conn, u["id"], request.args.get("month")))
    elif tab == "schedule":
        try:
            ref = date.fromisoformat(request.args.get("week", ""))
        except ValueError:
            ref = default_week_day()
        ctx["grid"] = week_grid(conn, ref - timedelta(days=ref.weekday()), weekend=request.args.get("weekend") == "1",
                                viewer=None if u["role"] in STAFF_ROLES else u)
        if request.args.get("view") == "month":
            ctx["mgrid"] = month_grid(conn, parse_month(request.args.get("month")), viewer=None if u["role"] in STAFF_ROLES else u)
        ctx["today_iso"] = today.isoformat()
        ctx["dayoff_full"] = dayoff_full_days(conn, u["id"], today, today + timedelta(days=45))
        ctx["dayoff_limits"] = (setting_int("dayoff_per_day", conn), setting_int("dayoff_per_month", conn), setting("dayoff_paid", conn) == "1")
        ctx["dayoff_req"] = conn.execute("select * from requests where user_id = ? and type = 'day_off' and status in ('pending', 'granted')"
                                         " order by id desc limit 1", (u["id"],)).fetchone()
        ctx["my_absences"] = conn.execute(
            f"select * from requests where user_id = ? and type in {ABSENCE_TYPES} and status in ('approved', 'pending')"
            " and ends_on >= ? order by starts_on", (u["id"], today.isoformat())).fetchall()
        ctx["schedule"] = [
            {**dict(r), "day": f"{DAY_SHORT[datetime.fromtimestamp(r['starts_at']).weekday()]}, {fmt_dt(r['starts_at'])[:10]}"}
            for r in conn.execute(
                "select sc.*, u.full_name, u.photo_url, l.name as location from schedule sc join users u on u.id = sc.user_id"
                " left join locations l on l.id = sc.location_id where sc.ends_at > ? and sc.starts_at < ? order by sc.starts_at",
                (now() - 12 * 3600, now() + 14 * 86400))]
        ctx["colleagues"] = conn.execute("select id, full_name from users where status not in ('dismissed', 'pending') and id != ? order by full_name",
                                         (u["id"],)).fetchall()
    elif tab == "requests":
        ctx["requests"] = conn.execute(
            "select r.*, u.full_name as author from requests r join users u on u.id = r.user_id"
            " where r.user_id = ? or r.swap_with_id = ? order by r.id desc", (u["id"], u["id"])).fetchall()
    elif tab == "dayoffs" and u["role"] in DAYOFF_ROLES:
        ctx["today_iso"] = today.isoformat()
        ctx["dayoff_open_until"] = dayoff_campaign_until(conn)
        q = ("select r.*, u.full_name as author, u.photo_url from requests r join users u on u.id = r.user_id"
             " where r.type = 'day_off' and r.user_id != ? and ")
        ctx["dayoff_pending"] = conn.execute(q + "r.status = 'pending' order by r.id", (u["id"],)).fetchall()
        ctx["dayoff_granted"] = conn.execute(q + "r.status = 'granted' order by r.grant_until", (u["id"],)).fetchall()
        ctx["dayoff_upcoming"] = conn.execute(q + "r.status = 'approved' and r.ends_on >= ? order by r.starts_on",
                                              (u["id"], today.isoformat())).fetchall()
        ctx["people"] = conn.execute("select id, full_name from users where status not in ('dismissed', 'pending') and id != ?"
                                     " order by full_name", (u["id"],)).fetchall()
        ctx["everyone"] = conn.execute("select id, full_name from users where status not in ('dismissed', 'pending') order by full_name").fetchall()
        ctx["upcoming_shifts"] = conn.execute(
            "select sc.*, u.full_name from schedule sc join users u on u.id = sc.user_id where sc.starts_at > ? and sc.starts_at < ?"
            " order by sc.starts_at", (now(), now() + 21 * 86400)).fetchall()
    elif tab == "team":
        ctx["team"] = conn.execute("select u.*, l.name as location from users u left join locations l on l.id = u.location_id"
                                   " where u.status not in ('dismissed', 'pending') order by u.full_name").fetchall()
    return render_template("me.html", **ctx)


@app.post("/me/request")
@login_required()
def me_request():
    u = current_user()
    f = request.form
    rtype = f.get("type")
    with _lock:
        conn = db()
        if rtype == "time_edit":
            shift = conn.execute("select * from shifts where id = ? and user_id = ?", (f.get("shift_id"), u["id"])).fetchone()
            ps, pe = parse_dtinput(f.get("proposed_start")), parse_dtinput(f.get("proposed_end"))
            if not shift or not ps or (pe and pe < ps) or not f.get("comment", "").strip():
                flash("Перевірте час і додайте коментар для модератора")
                return redirect(url_for("me", tab="timesheet"))
            conn.execute("insert into requests(user_id, type, shift_id, proposed_start, proposed_end, comment, created_at)"
                         " values (?, 'time_edit', ?, ?, ?, ?, ?)", (u["id"], shift["id"], ps, pe, f["comment"].strip(), now()))
        elif rtype == "shift_add":
            # Система не помітила, що людина працювала: просить додати зміну, модератор перевіряє
            ps, pe = parse_dtinput(f.get("proposed_start")), parse_dtinput(f.get("proposed_end"))
            comment = f.get("comment", "").strip()
            if not ps or not pe or pe <= ps or pe > now() + 300 or pe - ps > 16 * 3600 or not comment:
                flash("Вкажіть початок і кінець (до 16 год, не в майбутньому) і поясніть, що сталося")
                return redirect(url_for("me", tab="timesheet"))
            if conn.execute("select 1 from shifts where user_id = ? and started_at < ? and coalesce(ended_at, ?) > ?",
                            (u["id"], pe, now(), ps)).fetchone():
                flash("На цей час у вас уже є зміна. Щоб змінити її час, натисніть олівець біля неї")
                return redirect(url_for("me", tab="timesheet"))
            conn.execute("insert into requests(user_id, type, proposed_start, proposed_end, comment, created_at)"
                         " values (?, 'shift_add', ?, ?, ?, ?)", (u["id"], ps, pe, comment, now()))
        elif rtype == "shift_swap":
            if u["role"] not in DAYOFF_ROLES:
                abort(403)  # змінами міняє тімлід через свій акаунт
            sc = conn.execute("select * from schedule where id = ? and user_id = ?", (f.get("schedule_id"), u["id"])).fetchone()
            other = conn.execute("select * from users where id = ? and id != ?", (f.get("swap_with_id"), u["id"])).fetchone()
            if not sc or not other:
                abort(400)
            conn.execute("insert into requests(user_id, type, schedule_id, swap_with_id, comment, created_at)"
                         " values (?, 'shift_swap', ?, ?, ?, ?)", (u["id"], sc["id"], other["id"], f.get("comment"), now()))
            notify(conn, other["id"], "Пропозиція обміну змінами", f"{u['full_name']} пропонує обмін")
        elif rtype == "day_off":
            # Спершу дозвіл від тімліда, потім людина сама обирає день.
            # Під час збору заявок (кампанії) людина одразу вказує бажаний день — тімлід його лише погоджує.
            if conn.execute("select 1 from requests where user_id = ? and type = 'day_off' and status in ('pending', 'granted')",
                            (u["id"],)).fetchone():
                flash("Запит на дей-офф уже є: дочекайтесь дозволу або оберіть день")
                return redirect(url_for("me", tab=f.get("back") or "schedule"))
            day = f.get("day", "")
            if day and day >= date.today().isoformat():
                if err := dayoff_limit_error(conn, u["id"], date.fromisoformat(day)):
                    flash(err)
                    return redirect(url_for("me", tab=f.get("back") or "clock"))
                conn.execute("insert into requests(user_id, type, starts_on, ends_on, comment, created_at)"
                             " values (?, 'day_off', ?, ?, ?, ?)", (u["id"], day, day, f.get("comment"), now()))
                notify_staff(conn, "Заявка на дей-офф", f"{u['full_name']}: {day}", DAYOFF_ROLES)
                conn.commit()
                flash(f"Заявку на дей-офф {day} надіслано тімліду")
                return redirect(url_for("me", tab=f.get("back") or "clock"))
            conn.execute("insert into requests(user_id, type, comment, created_at) values (?, 'day_off', ?, ?)",
                         (u["id"], f.get("comment"), now()))
            notify_staff(conn, "Запит на дей-офф", u["full_name"], DAYOFF_ROLES)
            conn.commit()
            flash("Запит на дей-офф надіслано тімліду")
            return redirect(url_for("me", tab=f.get("back") or "schedule"))
        elif rtype in ("vacation", "sick_leave"):
            s, e = f.get("starts_on"), f.get("ends_on") or f.get("starts_on")
            if not s or e < s:
                flash("Вкажіть правильні дати")
                return redirect(url_for("me", tab=f.get("back") or "requests"))
            # Відсутність працівник лише просить: погоджує, змінює чи знімає тімлід (модератор) або адміністратор
            conn.execute("insert into requests(user_id, type, status, starts_on, ends_on, comment, created_at)"
                         " values (?, ?, 'pending', ?, ?, ?, ?)", (u["id"], rtype, s, e, f.get("comment"), now()))
        else:
            abort(400)
        notify_staff(conn, "Новий запит", f"{u['full_name']}: {REQUEST_TYPES[rtype]}")
        conn.commit()
    flash("Запит надіслано модератору")
    return redirect(url_for("me", tab=f.get("back") or "requests"))


@app.post("/me/dayoff/<int:rid>")
@login_required()
def me_dayoff_pick(rid):
    """Людина з дозволом обирає день дей-оффу (лише протягом DAYOFF_PICK_DAYS днів після дозволу)."""
    u = current_user()
    day = request.form.get("day", "")
    with _lock:
        conn = db()
        expire_grants(conn)
        r = conn.execute("select * from requests where id = ? and user_id = ? and type = 'day_off'", (rid, u["id"])).fetchone() or abort(404)
        if r["status"] != "granted":
            flash("Дозвіл на дей-офф уже недійсний: попросіть тімліда ще раз")
        elif not day or day < date.today().isoformat():
            flash("Оберіть сьогоднішній або майбутній день")
        elif err := dayoff_limit_error(conn, u["id"], date.fromisoformat(day), except_rid=rid):
            flash(err)
        else:
            conn.execute("update requests set status = 'approved', starts_on = ?, ends_on = ? where id = ?", (day, day, rid))
            apply_absence(conn, conn.execute("select * from requests where id = ?", (rid,)).fetchone())
            audit(conn, u["id"], "UPDATE", "requests", rid, u["id"], {"day": [None, day]})
            notify_staff(conn, "Дей-офф обрано", f"{u['full_name']}: {day}", DAYOFF_ROLES)
            conn.commit()
            flash(f"Дей-офф на {day} позначено")
    return redirect(url_for("me", tab="schedule"))


@app.post("/admin/employee/<int:uid>/device/<int:did>/delete")
@login_required(staff=True)
def admin_device_delete(uid, did):
    """Відв'язати телефон (людина змінила телефон): наступний телефон, з якого прийде автопінг, прив'яжеться сам."""
    with _lock:
        conn = db()
        d = conn.execute("select * from devices where id = ? and user_id = ?", (did, uid)).fetchone() or abort(404)
        conn.execute("delete from devices where id = ?", (did,))
        audit(conn, current_user()["id"], "DELETE", "devices", did, uid, {"device": [f"{d['name']} ({d['device_key']})", None]})
        conn.commit()
    flash("Телефон відв'язано")
    return redirect(url_for("admin_employee", uid=uid))


@app.post("/admin/employee/<int:uid>/logout_all")
@login_required(staff=True)
def admin_logout_all(uid):
    """Відкликати постійний вхід на всіх пристроях людини: наступного разу знадобиться логін і пароль."""
    with _lock:
        conn = db()
        u = conn.execute("select full_name from users where id = ?", (uid,)).fetchone() or abort(404)
        n = conn.execute("select count(*) from remember_tokens where user_id = ?", (uid,)).fetchone()[0]
        revoke_remember(conn, uid)
        audit(conn, current_user()["id"], "DELETE", "remember_tokens", uid, uid, {"sessions": [f"{n} пристр.", "відкликано"]})
        conn.commit()
    flash(f"Вихід виконано на всіх пристроях: {u['full_name']}")
    return redirect(url_for("admin_employee", uid=uid))


@app.post("/team/swap")
@login_required()
def team_swap():
    """Тімлід передає зміну за графіком іншій людині (працівники самі не міняються)."""
    actor = current_user()
    if actor["role"] not in DAYOFF_ROLES:
        abort(403)
    with _lock:
        conn = db()
        sc = conn.execute("select * from schedule where id = ?", (request.form.get("schedule_id"),)).fetchone() or abort(404)
        to = conn.execute("select * from users where id = ? and status not in ('dismissed', 'pending')",
                          (request.form.get("user_id"),)).fetchone() or abort(400)
        if to["id"] != sc["user_id"]:
            conn.execute("update schedule set user_id = ? where id = ?", (to["id"], sc["id"]))
            audit(conn, actor["id"], "UPDATE", "schedule", sc["id"], sc["user_id"], {"user_id": [sc["user_id"], to["id"]]})
            when = fmt_dt(sc["starts_at"])
            notify(conn, sc["user_id"], "Зміну передано", f"{when} → {to['full_name']}")
            notify(conn, to["id"], "Вам передали зміну", when)
            conn.commit()
            flash(f"Зміну {when} передано: {to['full_name']}")
    return redirect(url_for("me", tab="dayoffs"))


@app.post("/schedule/toggle")
@login_required()
def schedule_toggle():
    """Клітинка місячної сітки: є зміна в цей день — прибрати, немає — поставити стандартну."""
    actor = current_user()
    if actor["role"] not in DAYOFF_ROLES:
        abort(403)
    data = request.get_json(silent=True) or request.form
    try:
        uid, day = int(data.get("user_id")), date.fromisoformat(data.get("day", ""))
    except (TypeError, ValueError):
        abort(400)
    start, end = day_bounds(day, day)
    with _lock:
        conn = db()
        conn.execute("select id from users where id = ?", (uid,)).fetchone() or abort(404)
        old = conn.execute("select * from schedule where user_id = ? and starts_at >= ? and starts_at < ?", (uid, start, end)).fetchall()
        if old:
            for o in old:
                conn.execute("delete from schedule where id = ?", (o["id"],))
                audit(conn, actor["id"], "DELETE", "schedule", o["id"], uid, {"starts_at": [o["starts_at"], None]})
            label = ""
        else:
            s0, e0, loc = default_slot(conn, uid, day)
            cur = conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at) values (?, ?, ?, ?)", (uid, loc, s0, e0))
            audit(conn, actor["id"], "INSERT", "schedule", cur.lastrowid, uid, {"starts_at": [None, s0], "ends_at": [None, e0]})
            label = month_grid(conn, day)["cells"].get((uid, day.isoformat()), "")
        conn.commit()
    if request.is_json:
        return jsonify(ok=True, on=bool(label), label=label)
    return redirect(request.referrer or url_for("me", tab="schedule", view="month", month=day.strftime("%Y-%m")))


@app.post("/schedule/dayoff")
@login_required()
def schedule_dayoff():
    """Клітинка місячної сітки в режимі «Дей-офи»: поставити або зняти дей-офф людині на день (тімлід ставить одразу, без заявки)."""
    actor = current_user()
    if actor["role"] not in DAYOFF_ROLES:
        abort(403)
    data = request.get_json(silent=True) or request.form
    try:
        uid, day = int(data.get("user_id")), date.fromisoformat(data.get("day", ""))
    except (TypeError, ValueError):
        abort(400)
    with _lock:
        conn = db()
        conn.execute("select id from users where id = ?", (uid,)).fetchone() or abort(404)
        if own_record_blocked(actor, uid):
            return jsonify(ok=False, error="Свій дей-офф попросіть через «Мій графік»: погоджує інша людина")
        iso = day.isoformat()
        existing = conn.execute("select * from requests where user_id = ? and type = 'day_off'"
                                " and status in ('approved', 'pending', 'granted') and starts_on = ? and ends_on = ?",
                                (uid, iso, iso)).fetchone()
        if existing:
            conn.execute("update requests set status = 'cancelled', reviewer_id = ? where id = ?", (actor["id"], existing["id"]))
            audit(conn, actor["id"], "UPDATE", "requests", existing["id"], uid, {"status": [existing["status"], "cancelled"]})
            conn.commit()
            return jsonify(ok=True, off=True)
        other = absence_on(conn, uid, day, pending=True)
        if other:
            return jsonify(ok=False, error=f"Цей день уже зайнято: {REQUEST_TYPES[other['type']]}")
        if err := dayoff_limit_error(conn, uid, day):
            return jsonify(ok=False, error=err)
        cur = conn.execute("insert into requests(user_id, type, status, starts_on, ends_on, reviewer_id, created_at)"
                           " values (?, 'day_off', 'approved', ?, ?, ?, ?)", (uid, iso, iso, actor["id"], now()))
        apply_absence(conn, conn.execute("select * from requests where id = ?", (cur.lastrowid,)).fetchone())
        audit(conn, actor["id"], "INSERT", "requests", cur.lastrowid, uid, {"day_off": [None, iso]})
        notify(conn, uid, "Вам поставили дей-офф", iso)
        conn.commit()
    return jsonify(ok=True, off=False)


@app.post("/schedule/fill")
@login_required()
def schedule_fill():
    """Розставити всім робочі дні місяця (Пн–Пт, або й вихідні, якщо так у правилах), крім днів відсутності і вже заповнених."""
    actor = current_user()
    if actor["role"] not in DAYOFF_ROLES:
        abort(403)
    first = parse_month(request.form.get("month"))
    added = 0
    with _lock:
        conn = db()
        mg = month_grid(conn, first)
        weekends = setting("work_weekends", conn) == "1"
        for u in mg["users"]:
            for d in mg["days"]:
                if d["date"] < date.today() or (d["weekend"] and not weekends):
                    continue
                if (u["id"], d["iso"]) in mg["cells"] or (u["id"], d["iso"]) in mg["absences"]:
                    continue
                s0, e0, loc = default_slot(conn, u["id"], d["date"])
                conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at) values (?, ?, ?, ?)", (u["id"], loc, s0, e0))
                added += 1
        audit(conn, actor["id"], "INSERT", "schedule", None, None, {"fill_month": [None, f"{mg['month']} ({added})"]})
        conn.commit()
    flash(f"Розставлено змін: {added}")
    return redirect(request.referrer or url_for("me", tab="schedule", view="month", month=first.strftime("%Y-%m")))


@app.post("/team/dayoff")
@login_required()
def team_dayoff():
    """Тімлід (і модератор/адмін): дозволити чи відхилити запит, дати дозвіл сам, перенести або зняти дей-офф."""
    actor = current_user()
    if actor["role"] not in DAYOFF_ROLES:
        abort(403)
    f = request.form
    action = f.get("action")
    with _lock:
        conn = db()
        expire_grants(conn)
        r = None
        if f.get("rid"):
            r = conn.execute("select * from requests where id = ? and type = 'day_off' and user_id != ?",
                             (f["rid"], actor["id"])).fetchone() or abort(404)
        if action in ("approve", "reject") and r and r["status"] == "pending":
            if action == "approve" and r["starts_on"]:
                # Заявка з конкретним днем (із кампанії) — погоджуємо саме цей день
                if err := dayoff_limit_error(conn, r["user_id"], date.fromisoformat(r["starts_on"]), except_rid=r["id"]):
                    flash(err)
                else:
                    conn.execute("update requests set status = 'approved', reviewer_id = ? where id = ?", (actor["id"], r["id"]))
                    apply_absence(conn, conn.execute("select * from requests where id = ?", (r["id"],)).fetchone())
                    audit(conn, actor["id"], "UPDATE", "requests", r["id"], r["user_id"], {"status": ["pending", "approved"]})
                    notify(conn, r["user_id"], "Дей-офф погоджено", r["starts_on"])
            elif action == "approve":
                grant_dayoff(conn, r, actor)
            else:
                conn.execute("update requests set status = 'rejected', reviewer_id = ? where id = ?", (actor["id"], r["id"]))
                audit(conn, actor["id"], "UPDATE", "requests", r["id"], r["user_id"], {"status": ["pending", "rejected"]})
                notify(conn, r["user_id"], "Дей-офф не дозволено", f.get("comment") or "")
        elif action == "grant":
            uid = conn.execute("select id from users where id = ? and id != ? and status not in ('dismissed', 'pending')",
                               (f.get("user_id"), actor["id"])).fetchone() or abort(400)
            if conn.execute("select 1 from requests where user_id = ? and type = 'day_off' and status in ('pending', 'granted')",
                            (uid[0],)).fetchone():
                flash("У людини вже є запит або дозвіл на дей-офф")
            else:
                cur = conn.execute("insert into requests(user_id, type, comment, created_at) values (?, 'day_off', ?, ?)",
                                   (uid[0], f.get("comment") or None, now()))
                grant_dayoff(conn, conn.execute("select * from requests where id = ?", (cur.lastrowid,)).fetchone(), actor)
        elif action == "move" and r and r["status"] == "approved" and f.get("day", "") >= date.today().isoformat() \
                and (err := dayoff_limit_error(conn, r["user_id"], date.fromisoformat(f["day"]), except_rid=r["id"])):
            flash(err)
        elif action == "move" and r and r["status"] == "approved" and f.get("day", "") >= date.today().isoformat():
            day = f["day"]
            conn.execute("update requests set starts_on = ?, ends_on = ?, reviewer_id = ? where id = ?", (day, day, actor["id"], r["id"]))
            apply_absence(conn, conn.execute("select * from requests where id = ?", (r["id"],)).fetchone())
            audit(conn, actor["id"], "UPDATE", "requests", r["id"], r["user_id"], {"day": [r["starts_on"], day]})
            notify(conn, r["user_id"], "Дей-офф перенесено", f"{r['starts_on']} → {day}")
        elif action == "cancel" and r and r["status"] in ("approved", "granted"):
            conn.execute("update requests set status = 'cancelled', reviewer_id = ? where id = ?", (actor["id"], r["id"]))
            audit(conn, actor["id"], "UPDATE", "requests", r["id"], r["user_id"], {"status": [r["status"], "cancelled"]})
            notify(conn, r["user_id"], "Дей-офф скасовано", r["starts_on"] or "дозвіл знято")
        else:
            flash("Не вдалося: перевірте дату чи стан запиту")
        conn.commit()
    return redirect(url_for("me", tab="dayoffs"))


@app.post("/dayoffs/campaign")
@login_required()
def dayoff_campaign():
    """Модератор відкриває збір заявок на дей-офф: у всіх на головному екрані з'являється кнопка «Запросити дей-офф» на DAYOFF_PICK_DAYS днів."""
    actor = current_user()
    if actor["role"] not in DAYOFF_ROLES:
        abort(403)
    with _lock:
        conn = db()
        if request.form.get("action") == "open":
            until = now() + DAYOFF_PICK_DAYS * 86400
            meta_set(conn, "dayoff_open_until", until)
            for row in conn.execute("select id from users where status not in ('dismissed', 'pending') and id != ?", (actor["id"],)):
                notify(conn, row[0], "Збір заявок на дей-офф", f"Оберіть бажаний день до {fmt_dt(until)} на головному екрані")
            audit(conn, actor["id"], "UPDATE", "meta", None, None, {"dayoff_open_until": [None, until]})
            flash(f"Збір заявок відкрито до {fmt_dt(until)}")
        else:
            meta_set(conn, "dayoff_open_until", 0)
            audit(conn, actor["id"], "UPDATE", "meta", None, None, {"dayoff_open_until": ["open", None]})
            flash("Збір заявок закрито")
        conn.commit()
    return redirect(url_for("me", tab="dayoffs"))


@app.post("/me/request/<int:rid>/<action>")
@login_required()
def me_request_action(rid, action):
    u = current_user()
    with _lock:
        conn = db()
        r = conn.execute("select * from requests where id = ? and status = 'pending'", (rid,)).fetchone()
        if not r:
            abort(404)
        if action == "cancel" and r["user_id"] == u["id"] and r["type"] not in ABSENCE_TYPES:
            conn.execute("update requests set status = 'cancelled' where id = ?", (rid,))
        elif action in ("accept", "decline") and r["swap_with_id"] == u["id"]:
            if action == "accept":
                conn.execute("update requests set swap_accepted = 1 where id = ?", (rid,))
                notify_staff(conn, "Обмін змінами готовий до розгляду", u["full_name"])
            else:
                conn.execute("update requests set swap_accepted = 0, status = 'rejected' where id = ?", (rid,))
            notify(conn, r["user_id"], "Колега " + ("погодився на обмін" if action == "accept" else "відмовився від обміну"))
        else:
            abort(403)
        conn.commit()
    return redirect(url_for("me", tab="requests"))


@app.post("/me/profile")
@login_required()
def me_profile():
    u = current_user()
    # Номер телефону — це логін, його змінює адміністратор
    new = {"telegram": request.form.get("telegram", "").strip(), "whatsapp": request.form.get("whatsapp", "").strip()}
    with _lock:
        conn = db()
        conn.execute("update users set telegram = ?, whatsapp = ? where id = ?", (new["telegram"], new["whatsapp"], u["id"]))
        audit(conn, u["id"], "UPDATE", "users", u["id"], u["id"], diff(dict(u), new))
        conn.commit()
    flash("Контакти збережено")
    return redirect(url_for("me", tab="profile"))


@app.post("/me/password")
@login_required()
def me_password():
    u = current_user()
    new = request.form.get("new", "")
    key = f"pw:{u['id']}"
    if _throttle.recent(key, 900)[0] >= 5:
        flash(wait_text(_throttle.recent(key, 900)[1]))
    elif not verify_password(u["password_hash"], request.form.get("current", "")):
        _throttle.add(key)
        flash("Поточний пароль неправильний")
    elif len(new) < 6:
        flash("Новий пароль закороткий (мінімум 6 символів)")
    else:
        with _lock:
            conn = db()
            conn.execute("update users set password_hash = ? where id = ?", (hash_password(new), u["id"]))
            revoke_remember(conn, u["id"])  # зміна пароля вихід на інших пристроях
            val = issue_remember(conn, u["id"], device_key(), device_name(), client_ip())
            audit(conn, u["id"], "UPDATE", "users", u["id"], u["id"], {"password": ["***", "***"]})
            conn.commit()
        flash("Пароль змінено. На інших пристроях знадобиться новий вхід")
        return set_remember_cookie(redirect(url_for("me", tab="profile")), val)
    return redirect(url_for("me", tab="profile"))


# ---------------------------------------------------------------------------
# Адмінка
# ---------------------------------------------------------------------------
def dashboard_stats(conn):
    today = date.today()
    start, end = day_bounds(today, today)
    today_secs = sum(shift_stats(conn, s)["worked_seconds"]
                     for s in conn.execute("select * from shifts where started_at >= ? and started_at < ?", (start, end)))
    month = payroll(conn, today.replace(day=1), today)
    return {
        "today_hours": round(today_secs / 3600, 1),
        "month_total": round(sum(r["total"] for r in month), 2),
        "month_hours": round(sum(r["hours"] for r in month), 1),
        "pending": conn.execute("select count(*) from requests where status = 'pending'").fetchone()[0],
        "denied_today": conn.execute("select count(*) from conn_log where allowed = 0 and created_at >= ?", (start,)).fetchone()[0],
    }


_live_cache = {"at": 0.0, "data": None}
_live_lock = threading.Lock()


@app.route("/health")
def health():
    """Для моніторингу і скрипта автозапуску: сервер живий і база відповідає."""
    try:
        db().execute("select 1").fetchone()
        return jsonify({"ok": True, "time": now()})
    except sqlite3.Error as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.errorhandler(500)
def _server_error(e):
    # Помилка в одному запиті не валить сервер; людина бачить зрозумілий текст замість трасування
    app.logger.exception(e)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Помилка сервера, спробуйте ще раз"}), 500
    return render_template("error.html"), 500


@app.route("/api/live")
@login_required(staff=True)
def api_live():
    """Дані для live-моніторингу без перезавантаження сторінки. Кешуємо на 3 с: скільки б модераторів
    не тримали відкритий «Огляд», база рахує його не частіше."""
    with _live_lock:
        if time.time() - _live_cache["at"] < 3 and _live_cache["data"]:
            return jsonify(_live_cache["data"])
        data = _live_payload(db())
        _live_cache.update(at=time.time(), data=data)
    return jsonify(data)


def _live_payload(conn):
    rows = live_monitor(conn)
    out = []
    for r in rows:
        s, sc = r["shift"], r["schedule"]
        out.append({
            "id": r["user"]["id"], "name": r["user"]["full_name"], "job": r["user"]["job_title"], "photo": r["user"]["photo_url"],
            "state": r["state"], "location": r["location"],
            "started_at": s["started_at"] if s else None, "worked": s["worked_seconds"] if s else None,
            "status": s["status"] if s else None, "break_paid": bool(s["open_break"]["is_paid"]) if s and s["open_break"] else False,
            "last_seen": s["last_seen_at"] if s else None, "ip": s["last_seen_ip"] if s else None,
            "sched_start": sc["starts_at"] if sc else None, "sched_end": sc["ends_at"] if sc else None,
            "seen": r["presence"]["t"] if r.get("presence") else None, "seen_ip": r["presence"]["ip"] if r.get("presence") else None,
            "why": r["presence"]["why"] if r.get("presence") else None,
        })
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in LIVE_STATES}
    return {"rows": out, "counts": counts, "stats": dashboard_stats(conn), "now": now()}


@app.post("/photo/<int:uid>")
@login_required()
def upload_photo(uid):
    actor = current_user()
    if uid != actor["id"] and actor["role"] == "employee":
        abort(403)
    f = request.files.get("photo")
    if not f or not f.filename:
        flash("Оберіть фото")
    else:
        with _lock:
            conn = db()
            err = save_photo(conn, actor, uid, f)
            conn.commit()
        flash(err or "Фото оновлено")
    return redirect(request.referrer or url_for("index"))


@app.route("/avatar/<int:uid>")
@login_required()
def avatar(uid):
    # Спершу з бази (джерело правди), далі — старий файл у папці (сумісність зі старими аватарками)
    row = db().execute("select data, mime, ext from avatars where user_id = ?", (uid,)).fetchone()
    if row:
        return send_file(io.BytesIO(row["data"]), mimetype=row["mime"], max_age=86400,
                         download_name=f"avatar-{uid}.{row['ext']}")
    u = db().execute("select folder from users where id = ?", (uid,)).fetchone()
    path = avatar_file(os.path.join(PEOPLE_DIR, u["folder"])) if u and u["folder"] else None
    if not path:
        abort(404)
    return send_file(path, max_age=86400)


@app.route("/admin/employee/<int:uid>/folder.zip")
@login_required(staff=True)
def admin_employee_folder(uid):
    """Особова папка людини одним архівом."""
    import profiles
    with _lock:
        conn = db()
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
        profiles.export_user(conn, uid)
        conn.commit()
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone()
    buf = io.BytesIO()
    root = os.path.join(PEOPLE_DIR, u["folder"])
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(root)):
            if os.path.isfile(os.path.join(root, name)):
                z.write(os.path.join(root, name), f"{u['folder']}/{name}")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{u['folder']}.zip", mimetype="application/zip")


@app.route("/admin")
@login_required(staff=True)
def admin():
    conn = db()
    tab = request.args.get("tab", "live")
    ctx = {"tab": tab}
    ctx["pending_users"] = conn.execute(
        "select u.*, (select d.name || ' · IP ' || coalesce(d.first_ip, '?') from devices d where d.user_id = u.id order by d.id limit 1)"
        " as device from users u where u.status = 'pending' order by u.id").fetchall()
    if tab == "live":
        ctx["rows"] = live_monitor(conn)
        ctx["counts"] = {k: sum(1 for r in ctx["rows"] if r["state"] == k)
                         for k in LIVE_STATES}
        ctx["stats"] = dashboard_stats(conn)
        ctx["week"] = hours_by_day(conn)
    elif tab == "employees":
        archive = request.args.get("archive") == "1"
        ctx["archive"] = archive
        ctx["users"] = conn.execute("select u.*, l.name as location from users u left join locations l on l.id = u.location_id"
                                    " where (u.status = 'dismissed') = ? and u.status != 'pending' order by u.full_name",
                                    (archive,)).fetchall()
        ctx["locations"] = conn.execute("select * from locations order by name").fetchall()
    elif tab == "requests":
        ctx["locations"] = conn.execute("select * from locations order by name").fetchall()
        ctx["requests"] = conn.execute(
            "select r.*, u.full_name as author, w.full_name as swap_name, s.started_at as s_start, s.ended_at as s_end,"
            " sc.starts_at as sc_start, sc.ends_at as sc_end from requests r join users u on u.id = r.user_id"
            " left join users w on w.id = r.swap_with_id left join shifts s on s.id = r.shift_id"
            " left join schedule sc on sc.id = r.schedule_id"
            " order by (r.status = 'pending') desc, r.id desc limit 200").fetchall()
    elif tab == "schedule":
        try:
            ref = date.fromisoformat(request.args.get("week", ""))
        except ValueError:
            ref = default_week_day()
        ctx["grid"] = week_grid(conn, ref - timedelta(days=ref.weekday()), weekend=request.args.get("weekend") == "1")
        if request.args.get("view") == "month":
            ctx["mgrid"] = month_grid(conn, parse_month(request.args.get("month")))
        ctx["users"] = conn.execute("select id, full_name from users where status not in ('dismissed', 'pending') order by full_name").fetchall()
        ctx["locations"] = conn.execute("select * from locations where is_active = 1 order by name").fetchall()
    elif tab == "zones":
        locs = conn.execute("select * from locations order by name").fetchall()
        ctx["zones"] = [(l, conn.execute("select * from networks where location_id = ?", (l["id"],)).fetchall()) for l in locs]
        ctx["your_ip"] = client_ip()
        ctx["server_ip"] = get_local_ip()
        ctx["your_zone"] = match_location(conn, ctx["your_ip"])
        ctx["bind_cidr"] = suggest_cidr(ctx["your_ip"])
        url, company = server_url(), setting("company_name", conn)
        ctx["connect"] = {"url": url, "code": connect_code(url), "company": company,
                          "links": {l["id"]: connect_link(url, company, l) for l in locs}}
        # Телефони, які пробували почати зміну або пінгували з мереж, яких немає в жодній точці
        unknown = {}
        for c in conn.execute("select c.ip, c.created_at, u.full_name from conn_log c left join users u on u.id = c.user_id"
                              " where c.allowed = 0 and c.ip is not null and c.created_at > ? order by c.id desc",
                              (now() - 3 * 86400,)):
            if match_location(conn, c["ip"]):
                continue
            x = unknown.setdefault(c["ip"], {"ip": c["ip"], "cidr": suggest_cidr(c["ip"]), "who": c["full_name"],
                                             "last": c["created_at"], "count": 0})
            x["count"] += 1
        ctx["unknown"] = list(unknown.values())[:10]
    elif tab == "logs":
        ctx["logs"] = conn.execute(
            "select c.*, u.full_name, l.name as location from conn_log c left join users u on u.id = c.user_id"
            " left join locations l on l.id = c.location_id order by c.id desc limit 300").fetchall()
    elif tab == "audit":
        ctx["audit"] = conn.execute(
            "select a.*, x.full_name as actor, t.full_name as target from audit a left join users x on x.id = a.actor_id"
            " left join users t on t.id = a.target_user_id order by a.id desc limit 300").fetchall()
    elif tab == "payroll":
        d_from, d_to = month_range(request.args.get("month"))
        ctx.update(nav=month_nav(d_from), month=d_from.strftime("%Y-%m"), rows=payroll(conn, d_from, d_to), d_from=d_from, d_to=d_to)
    elif tab == "settings":
        ctx["settings"] = {k: setting(k) for k in DEFAULT_SETTINGS}
        ctx.update(backups=list_backups()[:10], backup_dir=BACKUP_DIR, backup_keep=BACKUP_KEEP, people_dir=PEOPLE_DIR,
                   backup_mirror=backup_mirror_dir() or "", mirror_error=RECOVERED.get("mirror_error"),
                   mirror_env=bool(os.environ.get("CREWHUB_BACKUP_MIRROR")))
        import profiles
        ctx["people_warning"] = profiles.status()["warning"]
    return render_template("admin.html", **ctx)


@app.route("/admin/employee/<int:uid>")
@login_required(staff=True)
def admin_employee(uid):
    conn = db()
    u = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
    ts = timesheet(conn, uid, request.args.get("month"))
    locations = conn.execute("select * from locations order by name").fetchall()
    open_s = open_shift(conn, uid)
    temp = session.pop("temp_pw", None)
    return render_template("employee.html", u=u, temp_pw=temp[1] if temp and temp[0] == uid else None, locations=locations, today=date.today().isoformat(),
                           week=hours_by_day(conn, uid), open_shift=shift_stats(conn, open_s) if open_s else None,
                           logs=conn.execute("select * from conn_log where user_id = ? order by id desc limit 15", (uid,)).fetchall(),
                           devices=conn.execute("select * from devices where user_id = ? order by id", (uid,)).fetchall(),
                           remember_count=conn.execute("select count(*) from remember_tokens where user_id = ?", (uid,)).fetchone()[0],
                           **ts)


@app.route("/admin/employee/<int:uid>/timesheet.csv")
@login_required(staff=True)
def admin_employee_csv(uid):
    conn = db()
    u = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
    ts = timesheet(conn, uid, request.args.get("month"))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Початок", "Кінець", "Точка", "Годин", "Перерви (хв)", "Ставка", "Сума", "Примітка"])
    for s in reversed(ts["shifts"]):
        w.writerow([fmt_dt(s["started_at"])[:10], fmt_tm(s["started_at"]), fmt_tm(s["ended_at"]), s["location"] or "",
                    round(s["worked_seconds"] / 3600, 2), s["unpaid_break_seconds"] // 60, s["hourly_rate"], s["earnings"],
                    ("відредаговано " if s["is_edited"] else "") + (s["note"] or "")])
    for a in ts["adjustments"]:
        w.writerow([a["day"], "", "", "", "", "", "", a["amount"], ("Бонус: " if a["amount"] > 0 else "Штраф: ") + a["reason"]])
    for x in ts["dayoffs"]:
        w.writerow([x["day"].isoformat(), "", "", "", x["hours"], "", u["hourly_rate"], x["amount"], "Дей-офф (оплачується повний день)"])
    t = ts["totals"]
    w.writerow(["Разом", "", "", "", round(t["seconds"] / 3600, 2), "", "", t["total"], ""])
    name = f"timesheet_{u['username']}_{ts['nav']['month']}.csv"
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


EDITABLE_USER_FIELDS = ["full_name", "job_title", "department", "location_id", "phone", "telegram", "whatsapp", "photo_url",
                        "hourly_rate", "hire_date", "status"]


@app.post("/admin/employee/<int:uid>")
@login_required(staff=True)
def admin_employee_save(uid):
    actor = current_user()
    f = request.form
    with _lock:
        conn = db()
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
        if u["role"] == "admin" and actor["role"] != "admin" and u["id"] != actor["id"]:
            abort(403)
        if own_record_blocked(actor, uid):
            flash(OWN_RECORD_MSG + ". Свої контакти змініть у «Профілі»")
            return redirect(url_for("admin_employee", uid=uid))
        new = {k: (f.get(k, "").strip() or None) for k in EDITABLE_USER_FIELDS}
        new["hourly_rate"] = float((new["hourly_rate"] or "0").replace(",", "."))
        new["location_id"] = int(new["location_id"]) if new["location_id"] else None
        new["full_name"] = new["full_name"] or u["full_name"]
        new["status"] = new["status"] if new["status"] in STATUSES else u["status"]
        if new["phone"]:
            new["phone"] = normalize_phone(new["phone"])
            if not new["phone"]:
                flash("Невірний номер телефону")
                return redirect(url_for("admin_employee", uid=uid))
            if phone_taken(conn, new["phone"], uid):
                flash("Цей номер уже має інший працівник")
                return redirect(url_for("admin_employee", uid=uid))
        if f.get("role") and f["role"] != u["role"]:
            if actor["role"] != "admin":
                flash("Роль змінює лише адміністратор")
                return redirect(url_for("admin_employee", uid=uid))
            new["role"] = f["role"] if f["role"] in ROLES else u["role"]
            if u["role"] == "admin" and new["role"] != "admin" and not admins_left(conn, uid):
                flash("Це єдиний адміністратор. Спершу призначте іншого адміністратора")
                return redirect(url_for("admin_employee", uid=uid))
        if (new.get("role") or u["role"]) == "admin" and new["status"] in ("dismissed", "pending") and not admins_left(conn, uid):
            flash("Це єдиний адміністратор: звільнити його не можна. Спершу призначте іншого адміністратора")
            return redirect(url_for("admin_employee", uid=uid))
        if f.get("password"):
            conn.execute("update users set password_hash = ? where id = ?", (hash_password(f["password"]), uid))
        changes = diff(dict(u), new)
        if f.get("password"):
            changes["password"] = ["***", "***"]
        sets = ", ".join(f"{k} = ?" for k in new)
        conn.execute(f"update users set {sets} where id = ?", (*new.values(), uid))
        if new["status"] == "dismissed" or f.get("password"):
            revoke_remember(conn, uid)  # звільнення чи новий пароль → постійний вхід відкликається
        if changes:
            audit(conn, actor["id"], "UPDATE", "users", uid, uid, changes)
        conn.commit()
    flash("Збережено. Зміни записано в журнал аудиту")
    return redirect(url_for("admin_employee", uid=uid))


@app.post("/admin/employee/new")
@login_required(staff=True)
def admin_employee_new():
    f = request.form
    phone = normalize_phone(f.get("phone"))
    if not phone or len(f.get("password", "")) < 6 or not f.get("full_name", "").strip():
        flash("Вкажіть ПІБ, правильний номер телефону і пароль від 6 символів")
        return redirect(url_for("admin", tab="employees"))
    with _lock:
        conn = db()
        if phone_taken(conn, phone):
            flash(f"Номер {format_phone(phone)} уже зареєстровано")
            return redirect(url_for("admin", tab="employees"))
        rate = f.get("hourly_rate") or setting("default_hourly_rate", conn)
        try:
            role = f.get("role") if f.get("role") in ROLES and current_user()["role"] == "admin" else "employee"
            cur = conn.execute("insert into users(username, password_hash, full_name, job_title, hourly_rate, hire_date, location_id,"
                               " phone, role) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (phone, hash_password(f["password"]), f["full_name"].strip(), f.get("job_title") or None,
                                float(str(rate).replace(",", ".")), f.get("hire_date") or None,
                                int(f["location_id"]) if f.get("location_id") else None, phone, role))
        except sqlite3.IntegrityError:
            flash("Цей номер уже зареєстровано")
            return redirect(url_for("admin", tab="employees"))
        username = phone
        audit(conn, current_user()["id"], "INSERT", "users", cur.lastrowid, cur.lastrowid,
              {"username": [None, username], "full_name": [None, f["full_name"].strip()]})
        conn.commit()
    flash(f"Працівника {f['full_name']} додано")
    return redirect(url_for("admin_employee", uid=cur.lastrowid))


def admins_left(conn, except_id):
    """Скільки ще діючих адміністраторів, крім except_id (звільнені й непідтверджені не рахуються)."""
    return conn.execute("select count(*) from users where role = 'admin' and status not in ('dismissed', 'pending') and id != ?",
                        (except_id,)).fetchone()[0]


@app.post("/admin/employee/<int:uid>/role")
@login_required(admin=True)
def admin_employee_role(uid):
    """Адмін сам вирішує, хто адміністратор, модератор чи працівник (швидкий вибір у списку людей)."""
    role = request.form.get("role") or (request.get_json(silent=True) or {}).get("role")
    wants_json = request.is_json or request.headers.get("X-CrewHub-Nav")
    with _lock:
        conn = db()
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
        err = None
        if role not in ROLES:
            err = "Невідома роль"
        elif u["role"] == "admin" and role != "admin" and not admins_left(conn, uid):
            err = "Це єдиний адміністратор. Спершу призначте іншого адміністратора"
        if err:
            if wants_json:
                return jsonify({"ok": False, "error": err, "role": u["role"]})
            flash(err)
            return redirect(url_for("admin", tab="employees"))
        if role != u["role"]:
            conn.execute("update users set role = ? where id = ?", (role, uid))
            audit(conn, current_user()["id"], "UPDATE", "users", uid, uid, {"role": [u["role"], role]})
            notify(conn, uid, "Вашу роль змінено", f"Тепер ви: {ROLES[role]}")
            conn.commit()
    msg = f"{u['full_name']}: {ROLES[role]}"
    if wants_json:
        return jsonify({"ok": True, "role": role, "message": msg})
    flash(msg)
    return redirect(url_for("admin", tab="employees"))


@app.post("/admin/registration/<int:uid>/<decision>")
@login_required(staff=True)
def admin_registration(uid, decision):
    actor = current_user()
    with _lock:
        conn = db()
        u = conn.execute("select * from users where id = ? and status = 'pending'", (uid,)).fetchone() or abort(404)
        if decision == "approve":
            f = request.form
            rate = float((f.get("hourly_rate") or setting("default_hourly_rate", conn)).replace(",", "."))
            loc = int(f["location_id"]) if f.get("location_id") else None
            role = f.get("role") if f.get("role") in ROLES and actor["role"] == "admin" else "employee"
            conn.execute("update users set status = 'active', hourly_rate = ?, location_id = ?, job_title = ?, hire_date = ?, role = ?"
                         " where id = ?", (rate, loc, f.get("job_title") or None, date.today().isoformat(), role, uid))
            audit(conn, actor["id"], "UPDATE", "users", uid, uid,
                  {"status": ["pending", "active"], "hourly_rate": [None, rate], "role": [None, role]})
            notify(conn, uid, "Акаунт підтверджено", "Ласкаво просимо в команду!")
            flash(f"{u['full_name']} тепер у команді")
        else:
            conn.execute("delete from users where id = ?", (uid,))
            audit(conn, actor["id"], "DELETE", "users", uid, None, {"registration": [f"{u['full_name']} {format_phone(u['phone'])}", None]})
            flash("Реєстрацію відхилено")
        conn.commit()
    return redirect(request.referrer or url_for("admin", tab="requests"))


@app.post("/admin/employee/<int:uid>/reset_password")
@login_required(admin=True)
def admin_reset_password(uid):
    actor = current_user()
    pw = temp_password()
    with _lock:
        conn = db()
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
        conn.execute("update users set password_hash = ?, must_change_password = 1 where id = ?", (hash_password(pw), uid))
        revoke_remember(conn, uid)  # старі токени «запам'ятати мене» більше не діють
        audit(conn, actor["id"], "UPDATE", "users", uid, uid, {"password": ["***", "скинуто адміністратором"]})
        conn.commit()
    session["temp_pw"] = [uid, pw]  # покажемо один раз на картці працівника
    return redirect(url_for("admin_employee", uid=uid))


@app.post("/admin/employee/<int:uid>/delete")
@login_required(admin=True)
def admin_employee_delete(uid):
    actor = current_user()
    if uid == actor["id"]:
        flash("Не можна видалити власний акаунт")
        return redirect(url_for("admin_employee", uid=uid))
    with _lock:
        conn = db()
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
        if u["role"] == "admin" and not admins_left(conn, uid):
            flash("Це останній адміністратор, його не можна видалити")
            return redirect(url_for("admin_employee", uid=uid))
        shifts = delete_user(conn, u, actor["id"])
        conn.commit()
    flash(f"Акаунт {u['full_name']} видалено разом з {shifts} змінами")
    return redirect(url_for("admin", tab="employees"))


def delete_user(conn, u, actor_id, via_folder=False):
    """Видаляє акаунт з усіма змінами. Особова папка переїжджає в backups/видалені (на випадок помилки);
    якщо людину видалили саме видаленням папки, її вже немає. Повертає кількість змін."""
    uid = u["id"]
    shifts = conn.execute("select count(*) from shifts where user_id = ?", (uid,)).fetchone()[0]
    conn.execute("update requests set swap_with_id = null where swap_with_id = ?", (uid,))
    conn.execute("delete from users where id = ?", (uid,))
    conn.execute("delete from notifications where user_id = ?", (uid,))
    conn.execute("delete from dirty_users where user_id = ?", (uid,))
    audit(conn, actor_id, "DELETE", "users", uid, None,
          {"full_name": [u["full_name"], None], "phone": [format_phone(u["phone"]), None], "shifts": [shifts, None],
           **({"folder": [u["folder"], "папку видалено"]} if via_folder else {})})
    if u["photo_url"] and u["photo_url"].startswith("/static/uploads/"):
        try:
            os.remove(os.path.join(UPLOAD_DIR, os.path.basename(u["photo_url"])))
        except OSError:
            pass
    if not via_folder and u["folder"] and os.path.isdir(os.path.join(PEOPLE_DIR, u["folder"])):
        trash = os.path.join(BACKUP_DIR, "видалені", f"{u['folder']} {datetime.now():%Y-%m-%d %H%M}")
        os.makedirs(os.path.dirname(trash), exist_ok=True)
        import shutil
        shutil.move(os.path.join(PEOPLE_DIR, u["folder"]), trash)
    return shifts


@app.post("/admin/rates")
@login_required(admin=True)
def admin_rates():
    """Загальна ставка: зберігається для нових працівників і за бажанням застосовується до всіх."""
    actor = current_user()
    try:
        rate = round(float(request.form.get("rate", "").replace(",", ".")), 2)
        assert rate >= 0
    except (ValueError, AssertionError):
        flash("Вкажіть ставку числом")
        return redirect(request.referrer or url_for("admin", tab="employees"))
    scope = request.form.get("scope", "default")  # default | all | employees
    with _lock:
        conn = db()
        old = setting("default_hourly_rate", conn)
        conn.execute("update settings set value = ? where key = 'default_hourly_rate'", (str(rate),))
        audit(conn, actor["id"], "UPDATE", "settings", None, None, {"default_hourly_rate": [old, str(rate)]})
        changed = 0
        if scope in ("all", "employees"):
            roles = ("employee", "teamlead") if scope == "employees" else ("employee", "teamlead", "moderator")
            for u in conn.execute(f"select id, hourly_rate from users where role in ({','.join('?' * len(roles))})"
                                  " and status not in ('dismissed', 'pending') and hourly_rate != ?", (*roles, rate)).fetchall():
                conn.execute("update users set hourly_rate = ? where id = ?", (rate, u["id"]))
                audit(conn, actor["id"], "UPDATE", "users", u["id"], u["id"], {"hourly_rate": [u["hourly_rate"], rate]})
                changed += 1
        conn.commit()
    flash(f"Загальна ставка {rate} {setting('currency')}/год" + (f", оновлено працівників: {changed}. Відкриті зміни дорахуються за старою ставкою"
                                                               if scope != "default" else ". Її отримають нові працівники"))
    return redirect(request.referrer or url_for("admin", tab="employees"))


@app.post("/admin/shift/save")
@login_required(staff=True)
def admin_shift_save():
    """Ручне редагування або додавання зміни модератором (з аудитом)."""
    actor = current_user()
    f = request.form
    uid = int(f["user_id"])
    start, end = parse_dtinput(f.get("started_at")), parse_dtinput(f.get("ended_at"))
    if not start or not end or end < start:
        flash("Перевірте початок і кінець зміни")
        return redirect(url_for("admin_employee", uid=uid))
    if own_record_blocked(actor, uid):
        flash(OWN_RECORD_MSG)
        return redirect(url_for("admin_employee", uid=uid))
    with _lock:
        conn = db()
        user = conn.execute("select * from users where id = ?", (uid,)).fetchone() or abort(404)
        rate = float(f["hourly_rate"].replace(",", ".")) if f.get("hourly_rate") else user["hourly_rate"]
        if f.get("shift_id"):
            s = conn.execute("select * from shifts where id = ? and user_id = ?", (f["shift_id"], uid)).fetchone() or abort(404)
            new = {"started_at": start, "ended_at": end, "hourly_rate": rate, "note": f.get("note") or s["note"]}
            conn.execute("update breaks set ended_at = min(coalesce(ended_at, ?), ?) where shift_id = ?", (end, end, s["id"]))
            conn.execute("update shifts set started_at = ?, ended_at = ?, hourly_rate = ?, note = ?, status = 'closed',"
                         " close_reason = coalesce(close_reason, 'moderator'), is_edited = 1 where id = ?",
                         (start, end, rate, new["note"], s["id"]))
            audit(conn, actor["id"], "UPDATE", "shifts", s["id"], uid, diff(dict(s), new))
        else:
            cur = conn.execute("insert into shifts(user_id, location_id, started_at, ended_at, status, close_reason, hourly_rate,"
                               " last_seen_at, is_edited, note) values (?, ?, ?, ?, 'closed', 'moderator', ?, ?, 1, ?)",
                               (uid, user["location_id"], start, end, rate, end, f.get("note")))
            audit(conn, actor["id"], "INSERT", "shifts", cur.lastrowid, uid,
                  {"started_at": [None, start], "ended_at": [None, end], "hourly_rate": [None, rate]})
        conn.commit()
    flash("Зміну збережено")
    return redirect(url_for("admin_employee", uid=uid))


@app.post("/admin/adjustment")
@login_required(staff=True)
def admin_adjustment():
    f = request.form
    uid = int(f["user_id"])
    day = f.get("day") or date.today().isoformat()
    reason = f.get("reason", "").strip()
    try:
        value = abs(float((f.get("amount") or "0").replace(",", ".")))
    except ValueError:
        value = 0
    if not value or not reason:
        flash("Вкажіть суму і причину")
        return redirect(url_for("admin_employee", uid=uid))
    if own_record_blocked(current_user(), uid):
        flash(OWN_RECORD_MSG)
        return redirect(url_for("admin_employee", uid=uid))
    with _lock:
        conn = db()
        conn.execute("select 1 from users where id = ?", (uid,)).fetchone() or abort(404)
        # Відсоток рахується від заробітку людини за той місяць, у якому стоїть бонус (години + дей-офи, без інших бонусів/штрафів).
        if f.get("unit") == "pct":
            try:
                md = date.fromisoformat(day)
            except ValueError:
                md = date.today()
            m_end = (md.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            row = payroll(conn, md.replace(day=1), m_end, uid)
            base = round((row[0]["earned"] + row[0]["dayoff"]) if row else 0, 2)
            amount = round(base * value / 100, 2)
            if not amount:
                flash("Відсоток не нараховано: у людини ще немає заробітку за цей місяць")
                return redirect(url_for("admin_employee", uid=uid))
            reason = f"{value:g}% від заробітку ({base:g}): {reason}"
        else:
            amount = value
        if f.get("kind") == "penalty":
            amount = -amount
        cur = conn.execute("insert into adjustments(user_id, amount, reason, day, created_by, created_at) values (?, ?, ?, ?, ?, ?)",
                           (uid, amount, reason, day, current_user()["id"], now()))
        audit(conn, current_user()["id"], "INSERT", "adjustments", cur.lastrowid, uid,
              {"amount": [None, amount], "reason": [None, reason]})
        conn.commit()
    return redirect(url_for("admin_employee", uid=uid))


@app.post("/admin/adjustment/<int:aid>/delete")
@login_required(staff=True)
def admin_adjustment_delete(aid):
    """Скасувати (видалити) помилково доданий бонус або штраф."""
    with _lock:
        conn = db()
        a = conn.execute("select * from adjustments where id = ?", (aid,)).fetchone() or abort(404)
        if own_record_blocked(current_user(), a["user_id"]):
            flash(OWN_RECORD_MSG)
            return redirect(url_for("admin_employee", uid=a["user_id"]))
        conn.execute("delete from adjustments where id = ?", (aid,))
        audit(conn, current_user()["id"], "DELETE", "adjustments", aid, a["user_id"],
              {"amount": [a["amount"], None], "reason": [a["reason"], None]})
        conn.commit()
    flash("Скасовано")
    return redirect(url_for("admin_employee", uid=a["user_id"]))


@app.post("/admin/request/<int:rid>/<decision>")
@login_required(staff=True)
def admin_request(rid, decision):
    actor = current_user()
    approve = decision == "approve"
    with _lock:
        conn = db()
        r = conn.execute("select * from requests where id = ? and status = 'pending'", (rid,)).fetchone() or abort(404)
        if own_record_blocked(actor, r["user_id"]):
            flash(OWN_RECORD_MSG)
            return redirect(url_for("admin", tab="requests"))
        if approve:
            if r["type"] == "time_edit":
                s = conn.execute("select * from shifts where id = ?", (r["shift_id"],)).fetchone()
                end = r["proposed_end"] or s["ended_at"]
                if end:
                    conn.execute("update breaks set ended_at = min(coalesce(ended_at, ?), ?) where shift_id = ?", (end, end, s["id"]))
                    conn.execute("update shifts set started_at = ?, ended_at = ?, status = 'closed',"
                                 " close_reason = coalesce(close_reason, 'moderator'), is_edited = 1 where id = ?",
                                 (r["proposed_start"], end, s["id"]))
                else:
                    conn.execute("update shifts set started_at = ?, is_edited = 1 where id = ?", (r["proposed_start"], s["id"]))
                audit(conn, actor["id"], "UPDATE", "shifts", s["id"], r["user_id"],
                      diff(dict(s), {"started_at": r["proposed_start"], "ended_at": end}) | {"request": [None, rid]})
            elif r["type"] == "shift_swap":
                if r["swap_accepted"] != 1:
                    flash("Колега ще не погодився на обмін")
                    return redirect(url_for("admin", tab="requests"))
                conn.execute("update schedule set user_id = ? where id = ?", (r["swap_with_id"], r["schedule_id"]))
                audit(conn, actor["id"], "UPDATE", "schedule", r["schedule_id"], r["user_id"], {"user_id": [r["user_id"], r["swap_with_id"]]})
            elif r["type"] == "shift_add":
                who = conn.execute("select * from users where id = ?", (r["user_id"],)).fetchone()
                cur = conn.execute("insert into shifts(user_id, location_id, started_at, ended_at, status, close_reason, hourly_rate,"
                                   " last_seen_at, is_edited, note) values (?, ?, ?, ?, 'closed', 'moderator', ?, ?, 1, ?)",
                                   (who["id"], who["location_id"], r["proposed_start"], r["proposed_end"], who["hourly_rate"],
                                    r["proposed_end"], "Пропущена зміна: " + (r["comment"] or "")))
                audit(conn, actor["id"], "INSERT", "shifts", cur.lastrowid, who["id"],
                      {"started_at": [None, r["proposed_start"]], "ended_at": [None, r["proposed_end"]], "request": [None, rid]})
            elif r["type"] == "day_off" and not r["starts_on"]:
                grant_dayoff(conn, r, actor, request.form.get("comment", "").strip() or None)
                conn.commit()
                return redirect(request.referrer or url_for("admin", tab="requests"))
            else:
                if r["type"] == "day_off" and (err := dayoff_limit_error(conn, r["user_id"], date.fromisoformat(r["starts_on"]),
                                                                        date.fromisoformat(r["ends_on"]), except_rid=r["id"])):
                    flash(err)
                    return redirect(url_for("admin", tab="requests"))
                apply_absence(conn, r)
        comment = request.form.get("comment", "").strip() or None
        conn.execute("update requests set status = ?, reviewer_id = ?, review_comment = ? where id = ?",
                     ("approved" if approve else "rejected", actor["id"], comment, rid))
        audit(conn, actor["id"], "UPDATE", "requests", rid, r["user_id"], {"status": ["pending", "approved" if approve else "rejected"]})
        notify(conn, r["user_id"], "Запит схвалено" if approve else "Запит відхилено", comment or REQUEST_TYPES[r["type"]])
        conn.commit()
    return redirect(url_for("admin", tab="requests"))


@app.post("/admin/absence")
@login_required(staff=True)
def admin_absence():
    """Модератор сам позначає лікарняний, дей-офф чи відпустку (одразу схвалено) або знімає позначку."""
    actor = current_user()
    f = request.form
    back = {"tab": "schedule", "week": f.get("week"), "weekend": f.get("weekend") or None}
    with _lock:
        conn = db()
        if f.get("cancel_id"):
            r = conn.execute(f"select * from requests where id = ? and type in {ABSENCE_TYPES}", (f["cancel_id"],)).fetchone() or abort(404)
            if own_record_blocked(actor, r["user_id"]):
                flash(OWN_RECORD_MSG)
                return redirect(url_for("admin", **back))
            conn.execute("update requests set status = 'cancelled', reviewer_id = ? where id = ?", (actor["id"], r["id"]))
            audit(conn, actor["id"], "UPDATE", "requests", r["id"], r["user_id"], {"status": [r["status"], "cancelled"]})
            notify(conn, r["user_id"], "Відсутність знято", f"{REQUEST_TYPES[r['type']]}: {r['starts_on']} – {r['ends_on']}")
        else:
            s, e = f.get("starts_on"), f.get("ends_on") or f.get("starts_on")
            if f.get("type") not in ABSENCE_TYPES or not s or e < s:
                flash("Вкажіть тип і правильні дати")
                return redirect(url_for("admin", **back))
            uid = int(f["user_id"])
            if own_record_blocked(actor, uid):
                flash(OWN_RECORD_MSG)
                return redirect(url_for("admin", **back))
            if f["type"] == "day_off" and (err := dayoff_limit_error(conn, uid, date.fromisoformat(s), date.fromisoformat(e))):
                flash(err)
                return redirect(url_for("admin", **back))
            cur = conn.execute("insert into requests(user_id, type, status, starts_on, ends_on, comment, reviewer_id, created_at)"
                               " values (?, ?, 'approved', ?, ?, ?, ?, ?)", (uid, f["type"], s, e, f.get("comment") or None, actor["id"], now()))
            apply_absence(conn, conn.execute("select * from requests where id = ?", (cur.lastrowid,)).fetchone())
            audit(conn, actor["id"], "INSERT", "requests", cur.lastrowid, uid, {"type": [None, f["type"]], "dates": [None, f"{s} – {e}"]})
            notify(conn, uid, REQUEST_TYPES[f["type"]] + " позначено", f"{s} – {e}")
        conn.commit()
    return redirect(url_for("admin", **back))


@app.post("/admin/schedule")
@login_required(staff=True)
def admin_schedule():
    f = request.form
    with _lock:
        conn = db()
        if f.get("action") == "copy_week":
            # Копіює графік попереднього тижня на поточний (без дублікатів)
            monday = date.fromisoformat(f["week"])
            start, end = day_bounds(monday - timedelta(days=7), monday - timedelta(days=1))
            copied = 0
            for sc in conn.execute("select * from schedule where starts_at >= ? and starts_at < ?", (start, end)).fetchall():
                ns, ne = sc["starts_at"] + 7 * 86400, sc["ends_at"] + 7 * 86400
                if not conn.execute("select 1 from schedule where user_id = ? and starts_at = ?", (sc["user_id"], ns)).fetchone():
                    conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at, note) values (?, ?, ?, ?, ?)",
                                 (sc["user_id"], sc["location_id"], ns, ne, sc["note"]))
                    copied += 1
            audit(conn, current_user()["id"], "INSERT", "schedule", None, None, {"copy_week": [None, f"{monday} ({copied})"]})
            conn.commit()
            flash(f"Скопійовано змін: {copied}")
            return redirect(url_for("admin", tab="schedule", week=f["week"]))
        if f.get("delete_id"):
            old = conn.execute("select * from schedule where id = ?", (f["delete_id"],)).fetchone()
            if old:
                conn.execute("delete from schedule where id = ?", (old["id"],))
                audit(conn, current_user()["id"], "DELETE", "schedule", old["id"], old["user_id"],
                      {"starts_at": [old["starts_at"], None], "ends_at": [old["ends_at"], None]})
        else:
            start, end = parse_dtinput(f.get("starts_at")), parse_dtinput(f.get("ends_at"))
            if not start or not end or end <= start:
                flash("Перевірте час зміни")
                return redirect(url_for("admin", tab="schedule", week=f.get("week")))
            cur = conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at, note) values (?, ?, ?, ?, ?)",
                               (int(f["user_id"]), int(f["location_id"]), start, end, f.get("note") or None))
            audit(conn, current_user()["id"], "INSERT", "schedule", cur.lastrowid, int(f["user_id"]),
                  {"starts_at": [None, start], "ends_at": [None, end]})
        conn.commit()
    return redirect(url_for("admin", tab="schedule", week=f.get("week")))


@app.post("/admin/zones")
@login_required(staff=True)
def admin_zones():
    actor = current_user()
    f = request.form
    with _lock:
        conn = db()
        if f.get("action") == "location":
            if not f.get("name", "").strip():
                flash("Вкажіть назву точки")
            else:
                try:
                    cur = conn.execute("insert into locations(name, address) values (?, ?)", (f["name"].strip(), f.get("address")))
                    audit(conn, actor["id"], "INSERT", "locations", cur.lastrowid, None, {"name": [None, f["name"].strip()]})
                except sqlite3.IntegrityError:
                    flash("Така точка вже є")
        elif f.get("action") == "toggle":
            conn.execute("update locations set is_active = 1 - is_active where id = ?", (f["location_id"],))
            audit(conn, actor["id"], "UPDATE", "locations", int(f["location_id"]), None, {"is_active": ["перемкнено", ""]})
        else:
            # Wi-Fi зони змінює лише адмін
            if actor["role"] != "admin":
                abort(403)
            if f.get("action") == "network":
                try:
                    net = ipaddress.ip_network(f.get("cidr", "").strip(), strict=False)
                except ValueError:
                    flash("Невірний формат IP або підмережі")
                    return redirect(url_for("admin", tab="zones"))
                if err := zone_error(net):
                    flash(err)
                    return redirect(url_for("admin", tab="zones"))
                cidr = str(net)
                try:
                    cur = conn.execute("insert into networks(location_id, cidr, label) values (?, ?, ?)",
                                       (int(f["location_id"]), cidr, f.get("label")))
                    audit(conn, actor["id"], "INSERT", "networks", cur.lastrowid, None, {"cidr": [None, cidr]})
                except sqlite3.IntegrityError:
                    flash("Ця мережа вже додана")
            elif f.get("action") == "bind_current":
                # Прив'язка мережі, з якої адмін зараз відкрив сторінку: у локальній мережі вся підмережа /24,
                # в інтернеті — точний публічний IP роутера
                cidr = suggest_cidr(client_ip())
                if not cidr:
                    flash("Цю сторінку відкрито на самому сервері. Відкрийте її з телефона чи ПК у Wi-Fi точки")
                    return redirect(url_for("admin", tab="zones"))
                try:
                    cur = conn.execute("insert into networks(location_id, cidr, label) values (?, ?, ?)",
                                       (int(f["location_id"]), cidr, f.get("label") or "Прив'язано з пристрою адміна"))
                    audit(conn, actor["id"], "INSERT", "networks", cur.lastrowid, None, {"cidr": [None, cidr]})
                    flash(f"Мережу {cidr} прив'язано. Тепер з неї можна вмикати таймер")
                except sqlite3.IntegrityError:
                    flash("Ця мережа вже прив'язана до точки")
            elif f.get("action") == "wifi":
                # Wi-Fi точки: назва й пароль потрапляють у QR, і телефон підключається до мережі сам
                old = conn.execute("select * from locations where id = ?", (f.get("location_id"),)).fetchone() or abort(404)
                ssid = f.get("wifi_ssid", "").strip()[:32] or None
                pwd = (f.get("wifi_password", "") if ssid else "")[:63] or None
                if pwd and len(pwd) < 8:
                    flash("Пароль Wi-Fi має бути щонайменше 8 символів")
                    return redirect(url_for("admin", tab="zones"))
                conn.execute("update locations set wifi_ssid = ?, wifi_password = ? where id = ?", (ssid, pwd, old["id"]))
                audit(conn, actor["id"], "UPDATE", "locations", old["id"], None, {"wifi_ssid": [old["wifi_ssid"], ssid]})
                flash(f"Wi-Fi «{ssid}» прикріплено до точки {old['name']}" if ssid else "Wi-Fi від'єднано від точки")
            elif f.get("action") == "delete_network":
                old = conn.execute("select * from networks where id = ?", (f["network_id"],)).fetchone()
                if old:
                    conn.execute("delete from networks where id = ?", (old["id"],))
                    audit(conn, actor["id"], "DELETE", "networks", old["id"], None, {"cidr": [old["cidr"], None]})
        conn.commit()
    return redirect(url_for("admin", tab="zones"))


NUMBER_SETTINGS = ("weekend_multiplier", "night_multiplier", "lunch_rate_pct", "rest_rate_pct", "auto_early_minutes",
                   "sleep_grace_minutes", "silence_forgive_minutes",
                   "dayoff_per_day", "dayoff_per_month")
CHECKBOX_SETTINGS = ("lunch_enabled", "lunch_is_paid", "rest_enabled", "rest_is_paid", "registration_open",
                     "auto_start", "work_weekends", "device_lock",
                     "offline_keeps_counting", "dayoff_paid")
TIME_SETTINGS = ("night_start", "night_end", "work_start", "work_end")


@app.post("/admin/backup_mirror")
@login_required(admin=True)
def admin_backup_mirror():
    path = request.form.get("path", "").strip().strip('"')
    with _lock:
        conn = db()
        old = meta_get(conn, "backup_mirror") or ""
        if path:
            try:
                os.makedirs(path, exist_ok=True)
                probe = os.path.join(path, ".crewhub_probe")
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
            except OSError as e:
                flash(f"Не вдалося записати в «{path}»: {e.strerror or e}")
                return redirect(url_for("admin", tab="settings"))
        meta_set(conn, "backup_mirror", path)
        audit(conn, current_user()["id"], "UPDATE", "settings", None, None, {"backup_mirror": [old, path]})
        conn.commit()
    if path:
        with _people_lock:
            backup_db()
        flash(f"Друга копія налаштована: {path}. Першу копію вже записано")
    else:
        flash("Другу копію вимкнено")
    return redirect(url_for("admin", tab="settings"))


@app.post("/admin/settings")
@login_required(admin=True)
def admin_settings():
    with _lock:
        conn = db()
        old = {k: setting(k, conn) for k in DEFAULT_SETTINGS}
        new = {}
        for k in DEFAULT_SETTINGS:
            if k in CHECKBOX_SETTINGS:
                new[k] = "1" if request.form.get(k) else "0"
            elif k in request.form:
                v = request.form[k].strip().replace(",", ".") if k in NUMBER_SETTINGS else request.form[k].strip()
                if k in NUMBER_SETTINGS:
                    try:
                        v = f"{max(float(v), 0):g}"
                    except ValueError:
                        v = ""
                elif k in TIME_SETTINGS and not re.fullmatch(r"\d{1,2}:\d{2}", v):
                    v = ""
                elif k == "auto_close_minutes":  # вводиться як 1:30 (год:хв) або хвилинами
                    m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
                    v = str(int(m[1]) * 60 + int(m[2])) if m else (v if v.isdigit() else "")
                    v = "" if v == "0" else v
                new[k] = v or old[k]
        for k, v in new.items():
            conn.execute("update settings set value = ? where key = ?", (v, k))
        changes = diff(old, new)
        if changes:
            audit(conn, current_user()["id"], "UPDATE", "settings", None, None, changes)
        conn.commit()
    flash("Правила збережено")
    return redirect(url_for("admin", tab="settings"))


@app.route("/admin/backup.zip")
@login_required(admin=True)
def admin_backup_download():
    with _people_lock:  # щоб у знімок не потрапила напівзаписана папка
        path = backup_db()
    audit(db(), current_user()["id"], "EXPORT", "backup", None, None, {"file": [None, os.path.basename(path)]})
    db().commit()
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/admin/payroll.csv")
@login_required(staff=True)
def admin_payroll_csv():
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    d_from = date.fromisoformat(month + "-01")
    d_to = (d_from.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ПІБ", "Посада", "Ставка", "Змін", "Годин", "За години", "У т.ч. надбавки (вихідні, ніч)", "Дей-офф", "Бонуси", "Штрафи", "До виплати"])
    for r in payroll(db(), d_from, d_to):
        w.writerow([r["user"]["full_name"], r["user"]["job_title"] or "", r["user"]["hourly_rate"], r["shifts"],
                    r["hours"], r["earned"], r["extra"], r["dayoff"], r["bonuses"], r["penalties"], r["total"]])
    # BOM, щоб Excel правильно відкрив кирилицю
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=payroll_{month}.csv"})
