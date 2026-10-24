"""Особові папки працівників: people/<Ім'я>/ — уся інформація про людину в одному місці.

  profile.json   ім'я, телефон, роль, посада, ставка, статус; ім'я, контакти й посаду можна правити руками
                 (роль, статус і ставку — лише в адмінці; хешу пароля в папці немає)
  картка.txt     те саме людською мовою + години і заробіток за місяць
  shifts.csv     усі зміни таблицею (відкривається в Excel)
  shifts.json    усі зміни з перервами (для відновлення)
  requests.json  дей-оффи, відпустки, лікарняні, обміни, правки часу
  adjustments.json, schedule.json — бонуси/штрафи і графік
  avatar.jpg     аватарка

Правила синхронізації (фоновий потік, кожні кілька секунд):
  * база — єдине джерело правди; папки people/ — це зручний експорт для перегляду й бекапу;
  * будь-яка зміна в базі → папка людини переписується (тригери dirty_users);
  * папку видалили чи вона зникла (від'єднали диск) → сервер відтворює її з бази, акаунт не чіпає;
  * profile.json поправили руками → ім'я, телефон, посада, відділ, контакти, дата прийому потрапляють у базу;
    роль, статус і ставку файлом змінити не можна (хто має доступ до папки чи диска, не стане адміном
    і не підніме собі ставку — це робить адмін в адмінці, з записом у журнал);
  * нову або повернуту папку імпортуємо в базу ЛИШЕ вручну: python profiles.py import "Ім'я"
    (щоб випадково покладена чи повернута папка не створювала й не видаляла акаунти сама).
Так збій з файловою системою ніколи не зносить працівників із бази.
"""
import hashlib
import io
import json
import os
import re
import time
from datetime import date, datetime

import crewhub_core as core

EDITABLE = ("full_name", "phone", "role", "job_title", "department", "telegram", "whatsapp", "hourly_rate", "hire_date", "status")
PROTECTED = ("role", "status", "hourly_rate")  # у файлі лише для читання: права й гроші змінює адмін в адмінці
SYSTEM_FILES = ("profile.json", "картка.txt", "shifts.csv", "shifts.json", "requests.json", "adjustments.json",
                "schedule.json", "ПОМИЛКА.txt")
_state = {"last_scan": 0.0, "warning": None, "exported": 0}


# ---------------------------------------------------------------------------
# Назви папок
# ---------------------------------------------------------------------------
def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name or "").strip().strip(".")
    return re.sub(r"\s+", " ", name)[:60] or "Без імені"


def _folder_owner_phone(path):
    try:
        with open(os.path.join(path, "profile.json"), encoding="utf-8") as f:
            return core.normalize_phone(json.load(f).get("phone") or "")
    except (OSError, ValueError):
        return None


def wanted_folder(conn, u):
    """Папка називається іменем людини. Якщо таке ім'я вже зайняте іншою людиною — додаємо номер телефону."""
    base = safe_name(u["full_name"])
    for candidate in (base, f"{base} ({(u['phone'] or '').lstrip('+') or u['id']})", f"{base} (id {u['id']})"):
        taken = conn.execute("select 1 from users where folder = ? and id != ?", (candidate, u["id"])).fetchone()
        path = os.path.join(core.PEOPLE_DIR, candidate)
        foreign = os.path.isdir(path) and candidate != u["folder"] and _folder_owner_phone(path) not in (None, u["phone"])
        if not taken and not foreign:
            return candidate
    return f"{base} (id {u['id']})"


def ensure_folder(conn, uid):
    """Шлях до особової папки; створює її, якщо людина ще без папки."""
    u = conn.execute("select * from users where id = ?", (uid,)).fetchone()
    if u["folder"]:
        path = os.path.join(core.PEOPLE_DIR, u["folder"])
        os.makedirs(path, exist_ok=True)
        return path
    name = wanted_folder(conn, u)
    os.makedirs(os.path.join(core.PEOPLE_DIR, name), exist_ok=True)
    conn.execute("update users set folder = ? where id = ?", (name, uid))
    return os.path.join(core.PEOPLE_DIR, name)


# ---------------------------------------------------------------------------
# Запис папки
# ---------------------------------------------------------------------------
def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)  # атомарно: ніхто не побачить напівзаписаний файл


def _iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def _sync_hash(values):
    return hashlib.sha1(json.dumps([str(values.get(k) if values.get(k) is not None else "") for k in EDITABLE],
                                   ensure_ascii=False).encode()).hexdigest()[:12]


def _profile(conn, u):
    loc = conn.execute("select name from locations where id = ?", (u["location_id"],)).fetchone() if u["location_id"] else None
    editable = {k: u[k] for k in EDITABLE}
    editable["phone"] = core.format_phone(u["phone"]) if u["phone"] else ""
    folder = os.path.join(core.PEOPLE_DIR, u["folder"])
    avatar = core.avatar_file(folder)
    return {
        "_як_редагувати": "Поля full_name, phone, job_title, department, telegram, whatsapp, hire_date можна змінити тут — "
                          "сервер підхопить за кілька секунд. role, status і hourly_rate лише для читання: їх змінює "
                          "адміністратор в адмінці. Видалите папку — сервер відтворить її з бази, дані не втрачаються. "
                          "Видалити працівника можна лише в адмінці.",
        **editable,
        "location": loc[0] if loc else None,
        "username": u["username"],
        "avatar": os.path.basename(avatar) if avatar else None,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "_sync": _sync_hash({**editable, "phone": u["phone"]}),
    }


def export_user(conn, uid, rules=None):
    """Переписує особову папку людини з бази. Повертає шлях або None (людини немає / папку видалили)."""
    with core._people_lock:
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone()
        if not u:
            return None
        if u["folder"]:
            path = os.path.join(core.PEOPLE_DIR, u["folder"])
            if not os.path.isdir(path):
                return None  # папку видалили: вирішує scan(), відтворювати її не можна
            want = wanted_folder(conn, u)
            if want != u["folder"] and not os.path.exists(os.path.join(core.PEOPLE_DIR, want)):
                os.rename(path, os.path.join(core.PEOPLE_DIR, want))  # змінили ім'я — перейменовуємо папку
                conn.execute("update users set folder = ? where id = ?", (want, uid))
                u = conn.execute("select * from users where id = ?", (uid,)).fetchone()
        path = ensure_folder(conn, uid)
        u = conn.execute("select * from users where id = ?", (uid,)).fetchone()
        rules = rules or core.pay_rules(conn)
        # Стара аватарка з static/uploads переїжджає в папку
        if u["photo_url"] and u["photo_url"].startswith("/static/uploads/"):
            old = os.path.join(core.UPLOAD_DIR, os.path.basename(u["photo_url"]))
            if os.path.exists(old):
                ext = old.rsplit(".", 1)[-1].lower()
                os.replace(old, os.path.join(path, f"avatar.{ext}"))
                conn.execute("update users set photo_url = ? where id = ?", (f"/avatar/{uid}?v={core.now()}", uid))
            else:
                conn.execute("update users set photo_url = null where id = ?", (uid,))
        # Аватарка живе в базі: якщо у відтвореній папці файлу немає, пишемо його з бази
        av = conn.execute("select data, ext from avatars where user_id = ?", (uid,)).fetchone()
        if av and not core.avatar_file(path):
            try:
                with open(os.path.join(path, f"avatar.{av['ext']}"), "wb") as fh:
                    fh.write(av["data"])
            except OSError:
                pass

        loc_names = {r[0]: r[1] for r in conn.execute("select id, name from locations")}
        shifts, csv_rows = [], []
        month_secs = month_money = 0.0
        month_start = core.day_bounds(date.today().replace(day=1), date.today())[0]
        all_shifts = conn.execute("select * from shifts where user_id = ? order by started_at", (uid,)).fetchall()
        brk = core.breaks_by_shift(conn, [s["id"] for s in all_shifts])  # усі перерви людини одним запитом
        for s in all_shifts:
            st = core.shift_stats(conn, s, rules=rules, breaks=brk.get(s["id"], []))
            breaks = [{"kind": b["kind"], "is_paid": b["is_paid"], "rate_pct": b["rate_pct"],
                       "started_at": b["started_at"], "ended_at": b["ended_at"], "reason": b["reason"]}
                      for b in brk.get(s["id"], [])]
            shifts.append({"started_at": s["started_at"], "ended_at": s["ended_at"], "start": _iso(s["started_at"]),
                           "end": _iso(s["ended_at"]), "status": s["status"], "close_reason": s["close_reason"],
                           "hourly_rate": s["hourly_rate"], "location": loc_names.get(s["location_id"]),
                           "start_ip": s["start_ip"], "end_ip": s["end_ip"], "last_seen_ip": s["last_seen_ip"],
                           "last_seen_at": s["last_seen_at"], "device": s["device"], "is_edited": s["is_edited"],
                           "note": s["note"], "bg_ok": s["bg_ok"], "breaks": breaks,
                           "worked_seconds": st["worked_seconds"], "earnings": st["earnings"]})
            csv_rows.append([_iso(s["started_at"])[:10], core.fmt_tm(s["started_at"]), core.fmt_tm(s["ended_at"]) if s["ended_at"] else "триває",
                             round(st["worked_seconds"] / 3600, 2), round(st["unpaid_break_seconds"] / 60), st["earnings"],
                             st["extra_earnings"], loc_names.get(s["location_id"]) or "", s["start_ip"] or "", s["close_reason"] or ""])
            if s["started_at"] >= month_start:
                month_secs += st["worked_seconds"]
                month_money += st["earnings"]
        requests = []
        for r in conn.execute("select r.*, s.started_at as shift_start, sc.starts_at as sched_start, w.phone as swap_phone,"
                              " rv.phone as reviewer_phone from requests r left join shifts s on s.id = r.shift_id"
                              " left join schedule sc on sc.id = r.schedule_id left join users w on w.id = r.swap_with_id"
                              " left join users rv on rv.id = r.reviewer_id where r.user_id = ? order by r.id", (uid,)):
            requests.append({"type": r["type"], "type_name": core.REQUEST_TYPES.get(r["type"]), "status": r["status"],
                             "starts_on": r["starts_on"], "ends_on": r["ends_on"], "comment": r["comment"],
                             "review_comment": r["review_comment"], "created_at": r["created_at"],
                             "proposed_start": r["proposed_start"], "proposed_end": r["proposed_end"],
                             "shift_started_at": r["shift_start"], "schedule_starts_at": r["sched_start"],
                             "swap_with_phone": r["swap_phone"], "swap_accepted": r["swap_accepted"],
                             "grant_until": r["grant_until"], "reviewer_phone": r["reviewer_phone"]})
        adjustments = [{"amount": a["amount"], "reason": a["reason"], "day": a["day"], "created_at": a["created_at"],
                        "created_by_phone": a["by_phone"]}
                       for a in conn.execute("select a.*, b.phone as by_phone from adjustments a left join users b on b.id = a.created_by"
                                             " where a.user_id = ? order by a.day", (uid,))]
        schedule = [{"starts_at": x["starts_at"], "ends_at": x["ends_at"], "start": _iso(x["starts_at"]),
                     "end": _iso(x["ends_at"]), "location": loc_names.get(x["location_id"]), "note": x["note"]}
                    for x in conn.execute("select * from schedule where user_id = ? order by starts_at", (uid,))]

        dump = lambda v: json.dumps(v, ensure_ascii=False, indent=1)  # noqa: E731
        _write(os.path.join(path, "profile.json"), dump(_profile(conn, u)))
        _write(os.path.join(path, "shifts.json"), dump(shifts))
        _write(os.path.join(path, "requests.json"), dump(requests))
        _write(os.path.join(path, "adjustments.json"), dump(adjustments))
        _write(os.path.join(path, "schedule.json"), dump(schedule))
        buf = io.StringIO()
        w = core.SafeCsv(buf)
        w.writerow(["Дата", "Початок", "Кінець", "Годин", "Перерви без оплати, хв", "Заробіток", "У т.ч. надбавки",
                    "Точка", "IP", "Як закрито"])
        w.writerows(reversed(csv_rows))
        _write(os.path.join(path, "shifts.csv"), "﻿" + buf.getvalue())
        _write(os.path.join(path, "картка.txt"), _card(u, loc_names, requests, month_secs, month_money, len(shifts)))
        _state["exported"] += 1
        return path


def _card(u, loc_names, requests, month_secs, month_money, total_shifts):
    absences = [r for r in requests if r["type"] in core.ABSENCE_TYPES and r["status"] in ("approved", "pending")]
    lines = [
        f"{u['full_name']}",
        "=" * max(len(u["full_name"]), 10),
        f"Роль:          {core.ROLES.get(u['role'], u['role'])}",
        f"Посада:        {u['job_title'] or '—'}",
        f"Відділ:        {u['department'] or '—'}",
        f"Точка:         {loc_names.get(u['location_id']) or '—'}",
        f"Телефон:       {core.format_phone(u['phone']) if u['phone'] else '—'}  (це логін)",
        f"Telegram:      {u['telegram'] or '—'}",
        f"WhatsApp:      {u['whatsapp'] or '—'}",
        f"Статус:        {core.STATUSES.get(u['status'], u['status'])}",
        f"Ставка:        {u['hourly_rate']:g} за годину",
        f"В команді:     {core.tenure_text(u['hire_date']) if u['hire_date'] else '—'}",
        "",
        f"Цього місяця:  {month_secs / 3600:.1f} год, {month_money:,.2f}".replace(",", " "),
        f"Усього змін:   {total_shifts}",
        "",
        "Відсутності:",
    ]
    lines += [f"  {r['starts_on']} – {r['ends_on']}  {r['type_name']}{' (чекає погодження)' if r['status'] == 'pending' else ''}"
              for r in absences[-15:]] or ["  немає"]
    lines += ["", f"Оновлено: {datetime.now():%d.%m.%Y %H:%M:%S}. Цей файл лише для читання; правити — у profile.json."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Відновлення з папки
# ---------------------------------------------------------------------------
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _location_id(conn, name):
    if not name:
        return None
    row = conn.execute("select id from locations where name = ?", (name,)).fetchone()
    return row[0] if row else None


def _user_by_phone(conn, phone):
    row = conn.execute("select id from users where phone = ?", (phone,)).fetchone() if phone else None
    return row[0] if row else None


def import_data(conn, uid, path):
    """Зміни, перерви, запити, бонуси і графік з папки → у базу (для людини uid). Без втрат: усе, що пише export_user."""
    shift_ids = {}
    for s in _read_json(os.path.join(path, "shifts.json"), []):
        status = s.get("status") or "closed"
        if status != "closed" and core.open_shift(conn, uid):
            status = "closed"  # відкритою може бути лише одна зміна
        cur = conn.execute(
            "insert into shifts(user_id, location_id, started_at, ended_at, status, close_reason, hourly_rate, last_seen_at,"
            " last_seen_ip, start_ip, end_ip, device, is_edited, note, bg_ok) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, _location_id(conn, s.get("location")), s["started_at"], s.get("ended_at"), status,
             s.get("close_reason"), s.get("hourly_rate") or 0, s.get("last_seen_at") or s.get("ended_at") or s["started_at"],
             s.get("last_seen_ip"), s.get("start_ip"), s.get("end_ip"), s.get("device"), s.get("is_edited") or 0, s.get("note"),
             s.get("bg_ok") or 0))
        shift_ids[s["started_at"]] = cur.lastrowid
        for b in s.get("breaks") or []:
            conn.execute("insert into breaks(shift_id, kind, is_paid, rate_pct, started_at, ended_at, reason) values (?, ?, ?, ?, ?, ?, ?)",
                         (cur.lastrowid, b["kind"], b.get("is_paid") or 0, b.get("rate_pct") or 0, b["started_at"], b.get("ended_at"),
                          b.get("reason")))
    sched_ids = {}
    for x in _read_json(os.path.join(path, "schedule.json"), []):
        cur = conn.execute("insert into schedule(user_id, location_id, starts_at, ends_at, note) values (?, ?, ?, ?, ?)",
                           (uid, _location_id(conn, x.get("location")), x["starts_at"], x["ends_at"], x.get("note")))
        sched_ids[x["starts_at"]] = cur.lastrowid
    for r in _read_json(os.path.join(path, "requests.json"), []):
        conn.execute("insert into requests(user_id, type, status, starts_on, ends_on, shift_id, proposed_start, proposed_end,"
                     " schedule_id, swap_with_id, swap_accepted, comment, review_comment, created_at, grant_until, reviewer_id)"
                     " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (uid, r["type"], r.get("status") or "pending", r.get("starts_on"), r.get("ends_on"),
                      shift_ids.get(r.get("shift_started_at")), r.get("proposed_start"), r.get("proposed_end"),
                      sched_ids.get(r.get("schedule_starts_at")), _user_by_phone(conn, r.get("swap_with_phone")), r.get("swap_accepted"),
                      r.get("comment"), r.get("review_comment"), r.get("created_at") or core.now(), r.get("grant_until"),
                      _user_by_phone(conn, r.get("reviewer_phone"))))
    for a in _read_json(os.path.join(path, "adjustments.json"), []):
        conn.execute("insert into adjustments(user_id, amount, reason, day, created_at, created_by) values (?, ?, ?, ?, ?, ?)",
                     (uid, a["amount"], a.get("reason") or "", a["day"], a.get("created_at") or core.now(),
                      _user_by_phone(conn, a.get("created_by_phone"))))
    return len(shift_ids)


def import_folder(conn, name):
    """Нова (або повернута з бекапу) папка → акаунт з усіма даними. Повертає id або None."""
    path = os.path.join(core.PEOPLE_DIR, name)
    p = _read_json(os.path.join(path, "profile.json"), None)
    if not isinstance(p, dict) or not p.get("full_name"):
        return None
    phone = core.normalize_phone(p.get("phone") or "")
    if phone and conn.execute("select 1 from users where phone = ?", (phone,)).fetchone():
        return None  # людина з таким номером уже є — не дублюємо акаунт
    username = p.get("username") or (phone or name)
    if conn.execute("select 1 from users where username = ?", (username,)).fetchone():
        username = f"{username}-{core.now()}"
    role = p.get("role") if p.get("role") in core.ROLES else "employee"
    status = p.get("status") if p.get("status") in core.STATUSES else "active"
    cur = conn.execute(
        "insert into users(username, password_hash, full_name, role, job_title, department, location_id, phone, telegram, whatsapp,"
        " hourly_rate, hire_date, status, must_change_password, folder) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        # Хешу пароля в папці немає: увійти можна буде після «Скинути пароль» в адмінці (старі папки — з хешем)
        (username, p.get("password_hash") or "!" + core.secrets.token_hex(16), p["full_name"], role, p.get("job_title"),
         p.get("department"), _location_id(conn, p.get("location")), phone, p.get("telegram"), p.get("whatsapp"),
         float(p.get("hourly_rate") or 0), p.get("hire_date"), status, 1, name))
    uid = cur.lastrowid
    af = core.avatar_file(path)
    if af:
        # Аватарку з папки заносимо в базу (джерело правди), щоб вона не залежала від файлу
        try:
            with open(af, "rb") as fh:
                blob = fh.read()
            ext = af.rsplit(".", 1)[-1].lower()
            if blob and ext in core.PHOTO_MIME:
                core.set_avatar_blob(conn, uid, blob, ext)
        except OSError:
            pass
        conn.execute("update users set photo_url = ? where id = ?", (f"/avatar/{uid}?v={core.now()}", uid))
    n = import_data(conn, uid, path)
    core.audit(conn, None, "INSERT", "users", uid, uid, {"folder": [None, f"відновлено з папки «{name}» ({n} змін)"]})
    core.notify_staff(conn, "Профіль відновлено з папки", f"{p['full_name']}: {n} змін")
    return uid


def _same(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a if a is not None else "") == str(b if b is not None else "")


def apply_manual_edit(conn, u, p, trusted=False):
    """profile.json поправили руками → перевіряємо і переносимо в базу. Повертає текст помилки або None.
    trusted — дані написав сам сервер (відновлення після збою бази): тоді переносимо й роль, статус, ставку."""
    if not trusted:
        changed = [k for k in PROTECTED if not _same(p.get(k), u[k])]
        if changed:
            return f"{', '.join(changed)} змінює лише адміністратор в адмінці (файл їх не міняє)"
    new = {}
    name = (p.get("full_name") or "").strip()
    if not name:
        return "Ім'я не може бути порожнім"
    new["full_name"] = name
    phone = core.normalize_phone(p.get("phone") or "")
    if not phone:
        return f"Невірний номер телефону: {p.get('phone')}"
    if core.phone_taken(conn, phone, u["id"]):
        return f"Номер {core.format_phone(phone)} уже має інший працівник"
    new["phone"] = phone
    if p.get("role") not in core.ROLES:
        return f"Невідома роль «{p.get('role')}». Можна: {', '.join(core.ROLES)}"
    if u["role"] == "admin" and p["role"] != "admin" and \
            conn.execute("select count(*) from users where role = 'admin' and id != ?", (u["id"],)).fetchone()[0] == 0:
        return "Це останній адміністратор, роль змінити не можна"
    new["role"] = p["role"]
    if p.get("status") not in core.STATUSES:
        return f"Невідомий статус «{p.get('status')}». Можна: {', '.join(core.STATUSES)}"
    new["status"] = p["status"]
    try:
        new["hourly_rate"] = max(float(str(p.get("hourly_rate") or 0).replace(",", ".")), 0)
    except ValueError:
        return f"Ставка має бути числом: {p.get('hourly_rate')}"
    hire = p.get("hire_date") or None
    if hire:
        try:
            date.fromisoformat(hire)
        except ValueError:
            return f"Дата прийому у форматі РРРР-ММ-ДД: {hire}"
    new["hire_date"] = hire
    for k in ("job_title", "department", "telegram", "whatsapp"):
        new[k] = (p.get(k) or "").strip() or None
    changes = core.diff(dict(u), new)
    if changes:
        core.follow_phone(conn, u, new["phone"])
        conn.execute(f"update users set {', '.join(k + ' = ?' for k in new)} where id = ?", (*new.values(), u["id"]))
        core.audit(conn, None, "UPDATE", "users", u["id"], u["id"], {**changes, "via": ["", "правка profile.json"]})
    return None


# ---------------------------------------------------------------------------
# Синхронізація
# ---------------------------------------------------------------------------
def flush_dirty(conn, limit=500):
    ids = [r[0] for r in conn.execute("select user_id from dirty_users limit ?", (limit,))]
    if not ids:
        return 0
    conn.execute(f"delete from dirty_users where user_id in ({','.join('?' * len(ids))})", ids)
    conn.commit()
    rules = core.pay_rules(conn)
    for i, uid in enumerate(ids):
        export_user(conn, uid, rules)
        conn.commit()  # не тримати базу заблокованою, поки пишуться папки інших людей
        if i + 1 < len(ids):
            time.sleep(0.03)  # ранковий наплив: папки пишуться фоном, відповіді телефонам мають пріоритет
    # Позначки, що з'явились під час запису, лишаються: ця зміна потрапить у папку наступним проходом
    # (перейменування папки дасть один зайвий прохід, без нескінченного циклу)
    return len(ids)


def scan(conn):
    """Звіряє папки з базою: видалені, повернуті, перейменовані і відредаговані вручну."""
    os.makedirs(core.PEOPLE_DIR, exist_ok=True)
    dirs = {d for d in os.listdir(core.PEOPLE_DIR)
            if os.path.isdir(os.path.join(core.PEOPLE_DIR, d)) and not d.startswith((".", "_"))}
    users = conn.execute("select * from users").fetchall()
    linked = {u["folder"]: u for u in users if u["folder"]}
    missing = [u for f, u in linked.items() if f not in dirs]
    changed = 0

    # 1) Папка без акаунта: перейменована вручну (той самий телефон) або повернута з бекапу
    for d in sorted(dirs - set(linked)):
        p = _read_json(os.path.join(core.PEOPLE_DIR, d, "profile.json"), None)
        if not isinstance(p, dict):
            continue
        phone = core.normalize_phone(p.get("phone") or "")
        owner = next((u for u in users if phone and u["phone"] == phone), None)
        if owner and owner in missing:
            conn.execute("update users set folder = ? where id = ?", (d, owner["id"]))  # просто перейменували
            missing.remove(owner)
            changed += 1
        elif owner:
            _write(os.path.join(core.PEOPLE_DIR, d, "ПОМИЛКА.txt"),
                   f"Номер {core.format_phone(phone)} уже належить «{owner['full_name']}» (папка «{owner['folder']}»).\n"
                   "Ця папка не підключена. Видаліть її або змініть номер у profile.json.\n")
        else:
            # Нову/невідому папку імпортуємо лише вручну, щоб вона не створювала акаунт сама.
            note = os.path.join(core.PEOPLE_DIR, d, "ПОМИЛКА.txt")
            if not os.path.exists(note):
                _write(note, "Ця папка не підключена до жодного акаунта.\n"
                             "Щоб створити з неї акаунт, виконайте на сервері:\n"
                             f'  python profiles.py import "{d}"\n')

    # 2) Папка зникла (видалили, від'єднали диск, перемістили) → акаунт НЕ чіпаємо.
    #    База — єдине джерело правди: просто відтворюємо папку з бази. Видалити людину
    #    можна лише в адмінці або командою CLI, ніколи не автоматично через файлову систему.
    if missing:
        _state["warning"] = (f"Папки {len(missing)} працівник(ів) зникли (напр. від'єднано диск) — "
                             "відтворюю з бази, акаунти збережено.")
        for u in missing:
            os.makedirs(os.path.join(core.PEOPLE_DIR, u["folder"]), exist_ok=True)
            conn.execute("insert or ignore into dirty_users values (?)", (u["id"],))
            changed += 1

    # 3) Ручні правки profile.json
    for u in conn.execute("select * from users where folder is not null").fetchall():
        path = os.path.join(core.PEOPLE_DIR, u["folder"])
        p = _read_json(os.path.join(path, "profile.json"), None)
        if not isinstance(p, dict) or "_sync" not in p:
            continue
        if _sync_hash({k: (core.normalize_phone(p.get("phone") or "") if k == "phone" else p.get(k)) for k in EDITABLE}) == p["_sync"]:
            continue
        err = apply_manual_edit(conn, u, p)
        if err:
            _write(os.path.join(path, "ПОМИЛКА.txt"), f"Правку profile.json не прийнято: {err}.\nПовернуто попередні дані.\n")
        elif os.path.exists(os.path.join(path, "ПОМИЛКА.txt")):
            os.remove(os.path.join(path, "ПОМИЛКА.txt"))
        conn.execute("insert or ignore into dirty_users values (?)", (u["id"],))  # перезаписати profile.json з новим _sync
        changed += 1

    # 4) Нові акаунти (реєстрація, адмінка) отримують папку
    conn.executemany("insert or ignore into dirty_users values (?)",
                     [(r[0],) for r in conn.execute("select id from users where folder is null")])
    conn.commit()
    _state["last_scan"] = time.time()
    return changed


def _exported_at(p):
    try:
        return datetime.fromisoformat(p.get("exported_at") or "").timestamp()
    except (TypeError, ValueError):
        return 0


def rebuild_from_folders(conn, since=None):
    """Після відновлення пошкодженої бази з копії: дані людей беремо з папок — вони свіжіші за копію.
    since — час копії: папку, записану раніше за неї (застаріла), не чіпаємо — у копії дані новіші."""
    for d in sorted(os.listdir(core.PEOPLE_DIR)) if os.path.isdir(core.PEOPLE_DIR) else []:
        path = os.path.join(core.PEOPLE_DIR, d)
        p = _read_json(os.path.join(path, "profile.json"), None)
        if not isinstance(p, dict):
            continue
        if since and _exported_at(p) < since:
            continue
        phone = core.normalize_phone(p.get("phone") or "")
        u = conn.execute("select * from users where phone = ? or folder = ?", (phone, d)).fetchone()
        if not u:
            import_folder(conn, d)
            continue
        for t in ("shifts", "schedule", "requests", "adjustments"):
            conn.execute(f"delete from {t} where user_id = ?", (u["id"],))
        conn.execute("update users set folder = ?, password_hash = ? where id = ?", (d, p.get("password_hash") or u["password_hash"], u["id"]))
        apply_manual_edit(conn, conn.execute("select * from users where id = ?", (u["id"],)).fetchone(), p, trusted=True)
        import_data(conn, u["id"], path)
    conn.commit()


def sync_once(conn):
    with core._lock:
        if time.time() - _state["last_scan"] > 5:
            scan(conn)
    return flush_dirty(conn)


def valid_folders(conn=None):
    """Папки з придатним profile.json (є ім'я) — кандидати на відновлення."""
    if not os.path.isdir(core.PEOPLE_DIR):
        return []
    out = []
    for d in sorted(os.listdir(core.PEOPLE_DIR)):
        if d.startswith((".", "_")) or not os.path.isdir(os.path.join(core.PEOPLE_DIR, d)):
            continue
        p = _read_json(os.path.join(core.PEOPLE_DIR, d, "profile.json"), None)
        if isinstance(p, dict) and p.get("full_name"):
            out.append(d)
    return out


def recover_if_empty(conn):
    """Відновлення після втрати бази: якщо в базі немає працівників, а особові папки є —
    відтворюємо людей з папок (лише на старті, лише коли база порожня). Це не двостороння
    синхронізація: під час звичайної роботи база лишається єдиним джерелом правди."""
    real = conn.execute("select count(*) from users where role != 'admin'").fetchone()[0]
    folders = valid_folders(conn)
    if real == 0 and folders:
        with core._lock:
            rebuild_from_folders(conn)
            conn.commit()
        _state["warning"] = (f"База була порожня після перезапуску — відновлено {len(folders)} "
                             "працівник(ів) з особових папок people/.")
        return len(folders)
    return 0


def start_folder_sync(interval=3):
    pending_recovery = [True]

    def step():
        conn = core.connect()
        try:
            if pending_recovery[0]:
                pending_recovery[0] = False  # відновлення пробуємо один раз при запуску
                try:
                    if core.RECOVERED.get("from_backup"):
                        with core._lock:
                            rebuild_from_folders(conn, core.RECOVERED.get("backup_at"))
                            conn.commit()
                        core.RECOVERED["from_backup"] = False
                    else:
                        recover_if_empty(conn)  # порожня база + наявні папки = втрата бази, відновлюємо
                except Exception as e:  # noqa: BLE001 — відновлення не має завалити запуск
                    _state["warning"] = f"Не вдалося відновити з папок: {e}"
                    core.log.exception("Відновлення з особових папок")
            try:
                sync_once(conn)
            except Exception as e:
                _state["warning"] = f"Помилка синхронізації папок: {e}"
                raise
        finally:
            conn.close()
    core.run_worker("people", interval, step)


def status():
    return {"dir": core.PEOPLE_DIR, "warning": _state["warning"], "exported": _state["exported"],
            "folders": len([d for d in os.listdir(core.PEOPLE_DIR) if not d.startswith((".", "_"))])
            if os.path.isdir(core.PEOPLE_DIR) else 0}


# ---------------------------------------------------------------------------
# Ручний імпорт папок у базу (CLI) — єдиний спосіб створити акаунт із папки.
# ---------------------------------------------------------------------------
def _list_folders(conn):
    os.makedirs(core.PEOPLE_DIR, exist_ok=True)
    linked = {u["folder"] for u in conn.execute("select folder from users where folder is not null")}
    rows = []
    for d in sorted(os.listdir(core.PEOPLE_DIR)):
        if d.startswith((".", "_")) or not os.path.isdir(os.path.join(core.PEOPLE_DIR, d)):
            continue
        rows.append((d, d in linked))
    return rows


def _cli(argv):
    core.init_db(seed=False)
    conn = core.connect()
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "list":
        for name, linked in _list_folders(conn):
            print(("[підключена]    " if linked else "[НЕ підключена] ") + name)
        return 0
    if cmd == "import" and len(argv) > 2:
        name = argv[2]
        with core._lock:
            uid = import_folder(conn, name)
            conn.commit()
        if uid:
            print(f"Імпортовано: «{name}» → акаунт id {uid}")
            return 0
        print(f"Не вдалося імпортувати «{name}»: у папці немає profile.json з іменем і паролем, "
              "або працівник із таким номером уже є.")
        return 1
    if cmd == "import-all":
        n = 0
        with core._lock:
            for name, linked in _list_folders(conn):
                if not linked and import_folder(conn, name):
                    n += 1
            conn.commit()
        print(f"Імпортовано нових акаунтів: {n}")
        return 0
    print("CrewHub — ручний імпорт особових папок people/ у базу.\n\n"
          "  python profiles.py list            показати папки й чи підключені вони до акаунтів\n"
          '  python profiles.py import "Ім\'я"   створити акаунт із конкретної папки\n'
          "  python profiles.py import-all      імпортувати всі ще не підключені папки\n\n"
          "Видалення папки акаунт НЕ видаляє — працівника прибирають лише в адмінці.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv))
