"""settings.ini — правила, точки й Wi-Fi зони звичайним текстовим файлом у папці даних.

Навіщо: файл лежить окремо від програми (див. crewhub_core.DATA_DIR) і переживає будь-яке оновлення чи
заміну файлів коду. Якщо базу створено наново (нова інсталяція, відновлення), правила й точки
повертаються з цього файлу самі.

Як синхронізується (sync() — при запуску, після кожної зміни в адмінці і щокілька секунд у фоні):
  * змінили в адмінці → файл переписується одразу;
  * змінили файл руками → сервер застосовує його протягом ~10 с (файл має бути «спокійним» 2 с,
    щоб не прочитати його напівзаписаним). Неправильні значення не застосовуються: лишаються попередні,
    а причина пишеться в шапку файлу й у журнал;
  * при запуску файл важливіший за базу (свіжа база отримує правила й точки з нього).
Точки з файлу додаються й змінюються, але не видаляються: щоб вимкнути точку — active = 0.
Список networks точки — повний: прибраний рядок прибирає Wi-Fi зону.
"""
import configparser
import hashlib
import ipaddress
import json
import os
import re
import time
from datetime import datetime

import crewhub_core as core

SECTION_ALIASES = {"server": "server", "сервер": "server", "rules": "rules", "правила": "rules"}
POINT_RE = re.compile(r"(?:point|точка)\s*(\d{1,6})", re.I)
TRUE, FALSE = ("1", "yes", "true", "on", "так", "y"), ("0", "no", "false", "off", "ні", "n")
UNITS = (("_minutes", "хв"), ("_hours", "год"), ("_seconds", "с"), ("_pct", "%"))
CHOICE_HELP = {"pause": "пауза", "close": "закрити зміну", "flag": "лише позначати",
               "block_shift": "не дати почати чи вести зміну в чужому акаунті (з чужого телефона)", "block_login": "ще й не пустити в чужий акаунт"}
DEFAULT_PORT = 5000
# Правила, які лишились у базі від старих версій, але ні на що не впливають: у файл не пишемо, щоб не плутали
UNUSED = {"sick_auto_approve"}

# Підписи файлу й бази на момент останньої синхронізації і base — файл, яким ми його записали востаннє:
# з ручної правки застосовуємо лише те, що людина справді змінила відносно base (інакше старе значення з файлу
# перекрило б зміну, зроблену в адмінці за ці кілька секунд)
_state = {"file": None, "db": None, "server": {}, "base": None}


def _bool(v):
    v = str(v).strip().lower()
    return "1" if v in TRUE else "0" if v in FALSE else None


def _hint(key):
    rule = core.SETTING_RULES.get(key)
    if not rule:
        return key
    kind, label = rule[0], rule[-1]
    unit = next((u for suffix, u in UNITS if key.endswith(suffix) and not label.endswith(u)), "")
    if kind == "bool":
        return f"{label} (1 — так, 0 — ні)"
    if kind == "choice":
        return f"{label}: " + " або ".join(f"{c} — {CHOICE_HELP.get(c, c)}" for c in rule[1])
    if kind == "time":
        return f"{label} (ГГ:ХХ)"
    if kind in ("int", "float"):
        return f"{label} ({'ціле ' if kind == 'int' else ''}число {core.num_text(rule[1])}–{core.num_text(rule[2])}{' ' + unit if unit else ''})"
    return label


def snapshot(conn):
    """Те, що лежить у файлі, як словник — для підпису й запису."""
    points = []
    nets = conn.execute("select location_id, cidr, label from networks order by location_id, id").fetchall()
    for loc in conn.execute("select * from locations order by id"):
        points.append({"id": loc["id"], "name": loc["name"], "address": loc["address"] or "", "active": loc["is_active"],
                       "wifi_ssid": loc["wifi_ssid"] or "", "wifi_password": loc["wifi_password"] or "",
                       "networks": [[n["cidr"], n["label"] or ""] for n in nets if n["location_id"] == loc["id"]]})
    return {"rules": {k: core.setting(k, conn) for k in core.DEFAULT_SETTINGS},
            "server": {"backup_mirror": core.meta_get(conn, "backup_mirror") or ""},
            "points": points}


def _sig(snap):
    return hashlib.sha1(json.dumps(snap, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _file_sig():
    try:
        st = os.stat(core.SETTINGS_FILE)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _one_line(v):
    return str(v).replace("\r", " ").replace("\n", " ").strip()


def render(snap, notes=(), port=None):
    out = [
        "; ─── CrewHub: налаштування сервера ───",
        "; Файл лежить у папці даних і переживає оновлення програми: правила й точки не скидаються,",
        "; навіть якщо програму розпакували наново чи замінили її файли.",
        "; Зміни в адмінці записуються сюди самі. Можна правити й вручну (Блокнот): сервер підхопить файл",
        "; протягом 10 секунд. Неправильне значення не застосовується — лишається попереднє, а причина",
        "; з'являється тут, на початку файлу. Рядки з «;» на початку — коментарі.",
    ]
    if notes:
        out.append(f"; УВАГА {datetime.now():%d.%m.%Y %H:%M} — не застосовано:")
        out += [f";   • {_one_line(n)}" for n in notes]
    out += ["", "[server]",
            "; Порт веб-сервера (діє після перезапуску; змінна середовища CREWHUB_PORT важливіша)",
            f"port = {port or DEFAULT_PORT}",
            "; Друга копія щоденних бекапів — папка на іншому диску чи флешці. Порожньо — вимкнено",
            f"backup_mirror = {_one_line(snap['server']['backup_mirror'])}", "", "[rules]"]
    for k, v in snap["rules"].items():
        if k not in UNUSED:
            out += [f"; {_hint(k)}", f"{k} = {_one_line(v)}"]
    out += ["", "; ─── Точки ───",
            "; Кожна точка — розділ [point N], де N — її номер. Нова точка — новий розділ з новим номером.",
            "; active: 1 — працює, 0 — вимкнена (так точку «видаляють»: зміни й історія лишаються).",
            "; wifi_ssid / wifi_password — Wi-Fi точки для QR-коду підключення (пароль щонайменше 8 символів).",
            "; networks — робочі мережі: підмережа (192.168.1.0/24) чи публічний IP роутера, по одній у рядку",
            ";   з відступом; після «|» — підпис. Прибраний рядок прибирає мережу."]
    for p in snap["points"]:
        out += ["", f"[point {p['id']}]", f"name = {_one_line(p['name'])}", f"address = {_one_line(p['address'])}",
                f"active = {1 if p['active'] else 0}", f"wifi_ssid = {_one_line(p['wifi_ssid'])}",
                f"wifi_password = {_one_line(p['wifi_password'])}", "networks ="]
        out += [f"    {cidr}" + (f" | {_one_line(label)}" if label else "") for cidr, label in p["networks"]]
    return "\n".join(out) + "\n"


def write(conn, notes=()):
    text = render(snapshot(conn), notes, _state["server"].get("port"))
    os.makedirs(os.path.dirname(core.SETTINGS_FILE), exist_ok=True)
    tmp = core.SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as f:
        f.write(text)
    os.replace(tmp, core.SETTINGS_FILE)  # атомарно: сервер чи людина ніколи не побачать півфайлу
    _state.update(file=_file_sig(), db=_sig(snapshot(conn)), base=parse(text))


def parse(text):
    cp = configparser.ConfigParser(interpolation=None, comment_prefixes=(";", "#"), inline_comment_prefixes=None,
                                   strict=False, empty_lines_in_values=False, default_section="\x00")
    cp.optionxform = str.lower
    cp.read_string(text.lstrip("﻿"))  # Блокнот Windows дописує BOM
    return cp


def _networks(raw, sec, notes):
    """Рядки «cidr | підпис» → {cidr: підпис}; неправильні рядки — у notes і пропускаються."""
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        cidr, _, label = line.partition("|")
        try:
            net = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError:
            notes.append(f"[{sec}] networks: «{cidr.strip()}» — не IP і не підмережа")
            continue
        if err := core.zone_error(net):
            notes.append(f"[{sec}] networks: {net} — {err}")
            continue
        out[str(net)] = label.strip()[:80]
    return out


def apply(conn, cp, base=None):
    """Застосовує файл до бази. base — файл, яким сервер записав його востаннє (None — застосувати все, як при
    запуску): значення, що збігаються з base, людина не чіпала, і вони не перекривають новіші з бази.
    Повертає (зміни для журналу, список проблем)."""
    changes, notes = {}, []
    sections = {}
    for sec in cp.sections():
        name = SECTION_ALIASES.get(sec.strip().lower())
        if name:
            sections[name] = cp[sec]
        elif not POINT_RE.fullmatch(sec.strip()):
            notes.append(f"невідомий розділ [{sec}]")

    def edited(d, key):
        sec = d.name
        return not (base is not None and base.has_option(sec, key) and base.get(sec, key) == d[key])

    rules = sections.get("rules", {})
    for key in [k for k in rules if edited(rules, k)]:
        if key in UNUSED:
            continue
        if key not in core.DEFAULT_SETTINGS:
            notes.append(f"[rules] {key}: такого правила немає")
            continue
        raw = rules[key]
        if core.SETTING_RULES.get(key, ("",))[0] == "bool":
            raw = _bool(raw) or raw
        v = core.clean_setting(key, raw)
        if v is None:
            notes.append(f"[rules] {key} = «{_one_line(rules[key])}» — неправильне значення. {_hint(key)}")
            continue
        old = core.setting(key, conn)
        if v != old:
            conn.execute("insert into settings(key, value) values (?, ?) on conflict(key) do update set value = excluded.value", (key, v))
            changes[key] = [old, v]

    server = sections.get("server", {})
    port = server.get("port", "").strip()
    if port:
        if port.isdigit() and 1 <= int(port) <= 65535:
            _state["server"]["port"] = int(port)
        else:
            notes.append(f"[server] port = «{port}»: має бути число 1–65535")
    if "backup_mirror" in server and edited(server, "backup_mirror"):
        mirror, old = server["backup_mirror"].strip().strip('"'), core.meta_get(conn, "backup_mirror") or ""
        if mirror != old:
            core.meta_set(conn, "backup_mirror", mirror)
            changes["backup_mirror"] = [old, mirror]

    for sec in cp.sections():
        m = POINT_RE.fullmatch(sec.strip())
        if m:
            known = conn.execute("select 1 from locations where id = ?", (int(m[1]),)).fetchone()
            # точки ще немає в базі — беремо з файлу все; є — лише те, що людина змінила
            d = {k: v for k, v in cp[sec].items() if not known or edited(cp[sec], k)}
            _apply_point(conn, int(m[1]), sec, d, changes, notes)
    return changes, notes


def _apply_point(conn, pid, sec, d, changes, notes):
    name = _one_line(d.get("name", ""))[:80]
    row = conn.execute("select * from locations where id = ?", (pid,)).fetchone()
    taken = conn.execute("select id from locations where name = ? and id != ?", (name, pid)).fetchone() if name else None
    if not row:
        if not name:
            notes.append(f"[{sec}]: немає name — точку не створено")
            return
        if taken:
            notes.append(f"[{sec}]: точка «{name}» уже є під номером {taken[0]}")
            return
        conn.execute("insert into locations(id, name) values (?, ?)", (pid, name))
        changes[f"точка {pid}"] = [None, name]
        row = conn.execute("select * from locations where id = ?", (pid,)).fetchone()
    new = {}
    if name and name != row["name"]:
        if taken:
            notes.append(f"[{sec}] name: «{name}» уже має точка {taken[0]}")
        else:
            new["name"] = name
    if "address" in d:
        new["address"] = _one_line(d["address"])[:200] or None
    if "active" in d:
        b = _bool(d["active"])
        if b is None:
            notes.append(f"[{sec}] active = «{_one_line(d['active'])}»: має бути 1 або 0")
        else:
            new["is_active"] = int(b)
    if "wifi_ssid" in d or "wifi_password" in d:
        ssid = _one_line(d.get("wifi_ssid", row["wifi_ssid"] or ""))[:32] or None
        pwd = (d.get("wifi_password", row["wifi_password"] or "").strip()[:63] or None) if ssid else None
        if pwd and len(pwd) < 8:
            notes.append(f"[{sec}] wifi_password: щонайменше 8 символів")
        else:
            new.update(wifi_ssid=ssid, wifi_password=pwd)
    new = {k: v for k, v in new.items() if row[k] != v}
    if new:
        conn.execute(f"update locations set {', '.join(f'{k} = ?' for k in new)} where id = ?", (*new.values(), pid))
        for k, v in new.items():
            changes[f"точка {pid}: {k}"] = ["***" if k == "wifi_password" else row[k], "***" if k == "wifi_password" else v]
    if "networks" not in d:
        return  # ключа немає взагалі — мережі точки не чіпаємо
    want = _networks(d["networks"], sec, notes)
    have = {n["cidr"]: n for n in conn.execute("select * from networks where location_id = ?", (pid,))}
    for cidr, n in have.items():
        if cidr not in want:
            conn.execute("delete from networks where id = ?", (n["id"],))
            changes[f"точка {pid}: мережа {cidr}"] = [cidr, None]
        elif (n["label"] or "") != want[cidr]:
            conn.execute("update networks set label = ? where id = ?", (want[cidr] or None, n["id"]))
    for cidr, label in want.items():
        if cidr not in have:
            conn.execute("insert into networks(location_id, cidr, label) values (?, ?, ?)", (pid, cidr, label or None))
            changes[f"точка {pid}: мережа {cidr}"] = [None, cidr]


def sync(conn=None):
    """Одна синхронізація файлу й бази. Безпечно викликати з будь-якого потоку й будь-коли."""
    with core._lock:  # той самий замок, що й у записів адмінки: файл і база змінюються по черзі
        own = conn is None
        conn = conn or core.connect()
        try:
            fsig = _file_sig()
            if fsig and fsig != _state["file"]:
                if _state["file"] is not None and time.time() - fsig[0] / 1e9 < 2:
                    return  # файл саме зберігають — прочитаємо наступного разу, вже цілим
                try:
                    with open(core.SETTINGS_FILE, encoding="utf-8") as f:
                        cp = parse(f.read())
                except (OSError, UnicodeDecodeError, configparser.Error) as e:
                    core.log.warning("settings.ini не прочитано: %s", e)
                    write(conn, [f"файл не вдалося прочитати ({type(e).__name__}: {e}); записано поточні налаштування з бази"])
                    return
                # при запуску (файл ще не читали) файл важливіший за базу цілком; далі — лише змінене людиною
                changes, notes = apply(conn, cp, _state["base"] if _state["file"] is not None else None)
                if changes:
                    core.audit(conn, None, "UPDATE", "settings", None, None, {"джерело": [None, "settings.ini"], **changes})
                conn.commit()
                if core.has_app_context():
                    core.g.pop("settings_cache", None)
                for n in notes:
                    core.log.warning("settings.ini: %s", n)
                write(conn, notes)  # переписуємо в повному вигляді: з усіма ключами й поясненнями
            elif fsig is None or _sig(snapshot(conn)) != _state["db"]:
                write(conn)
        finally:
            if own:
                conn.close()


def port():
    """Порт з [server] port або None (для start.py, до запуску сервера)."""
    try:
        with open(core.SETTINGS_FILE, encoding="utf-8") as f:
            cp = parse(f.read())
    except (OSError, UnicodeDecodeError, configparser.Error):
        return None
    for sec in cp.sections():
        if SECTION_ALIASES.get(sec.strip().lower()) == "server":
            v = cp[sec].get("port", "").strip()
            return int(v) if v.isdigit() and 1 <= int(v) <= 65535 else None
    return None


def start_sync(interval=10):
    core.run_worker("config", interval, sync)
