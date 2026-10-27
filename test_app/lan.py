"""Пристрої в локальній мережі сервера — усі, і зареєстровані в CrewHub, і чужі.

Як це працює (без прав адміністратора й сторонніх бібліотек):
  1. Раз на 5 хвилин сервер шле крихітний UDP-запит на кожну адресу своєї підмережі (до 1022 адрес).
     Щоб його відправити, система спершу питає «хто має цей IP?» (ARP) — так живі пристрої потрапляють
     у таблицю сусідів системи. Її й читаємо: /proc/net/arp (Linux) або `arp -a` (Windows, macOS).
  2. Ті самі запити — це питання імені: NetBIOS (порт 137, відповідають комп'ютери з Windows) і
     mDNS (порт 5353: iPhone, Mac, Linux, принтери, частина Android). Ще спитаємо DNS роутера
     (зворотний запит): багато роутерів знають ім'я, яке телефон назвав при підключенні до Wi-Fi.
  3. Модель і назва телефона — якщо пристрій відкривав CrewHub: з User-Agent браузера, а застосунок
     CrewHub сам передає модель і назву телефона з налаштувань («Galaxy S23 Артема»).
  4. Виробник — за першою половиною MAC-адреси (база OUI). Сучасні телефони в Wi-Fi показують
     випадковий («приватний») MAC: за ним виробника не визначити, і це видно в списку.
Сервер бачить лише ту мережу, в якій стоїть сам: пристрої інших точок (через інтернет) сюди не потрапляють.
"""
import ipaddress
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import unquote

import crewhub_core as core

SCAN_EVERY = 300          # секунд між скануваннями
KEEP_DAYS = 30            # стільки пам'ятаємо пристрій, якого більше не видно
MAX_HOSTS = 1022          # більші мережі скануємо лише навколо адреси сервера (/24)
WAIT = 1.5                # скільки чекаємо відповідей на запити імен
OUI_FILES = ("oui.txt", "/usr/share/ieee-data/oui.txt", "/usr/share/misc/oui.txt", "/usr/share/hwdata/oui.txt",
             "/usr/share/nmap/nmap-mac-prefixes", "/usr/share/wireshark/manuf")
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".localdomain", ".domain", ".router", ".box", ".intra")
MAC_RE = re.compile(r"\b([0-9a-f]{1,2}(?:[:-][0-9a-f]{1,2}){5})\b", re.I)
IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

_state = {"scan_at": 0, "took": 0.0, "error": None, "nets": []}
_oui_cache = {}


# ── Підмережі сервера ──────────────────────────────────────────────────────
def subnets():
    """Локальні підмережі сервера (IPv4, приватні). Справжню маску беремо з `ip addr` (Linux), інакше /24.
    CREWHUB_LAN_NETS="192.168.1.0/24,192.168.2.0/24" задає мережі явно (кілька VLAN, нестандартні інтерфейси)."""
    if os.environ.get("CREWHUB_LAN_NETS"):
        out = []
        for part in os.environ["CREWHUB_LAN_NETS"].split(","):
            try:
                net = ipaddress.ip_network(part.strip(), strict=False)
            except ValueError:
                continue
            if net.version == 4 and net.num_addresses - 2 <= MAX_HOSTS:
                out.append((net, None))
        return out
    nets = []
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.run(["ip", "-o", "-f", "inet", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
            for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out):
                nets.append(ipaddress.ip_interface(m[1]))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    if not nets:
        nets = [ipaddress.ip_interface(f"{ip}/24") for ip in core.local_ips() if ip != "127.0.0.1"]
    out = []
    for iface in nets:
        ip = iface.ip
        if ip.is_loopback or ip.is_link_local or not ip.is_private:
            continue
        net = iface.network if iface.network.num_addresses - 2 <= MAX_HOSTS else ipaddress.ip_network(f"{ip}/24", strict=False)
        if net.prefixlen < 31 and (net, ip) not in out:
            out.append((net, ip))
    return out


# ── Таблиця сусідів (ARP) ─────────────────────────────────────────────────
def norm_mac(mac):
    parts = re.split(r"[:-]", mac.lower())
    return ":".join(p.zfill(2) for p in parts) if len(parts) == 6 else None


def usable_mac(mac):
    """Не порожній, не широкомовний і не груповий (multicast) MAC."""
    return bool(mac) and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") and not int(mac[:2], 16) & 1


def private_mac(mac):
    """Випадковий MAC (Android 10+, iOS 14+ у Wi-Fi): другий біт першого байта — «адреса задана локально»."""
    return bool(int(mac[:2], 16) & 2)


def parse_proc_arp(text):
    """/proc/net/arp → {ip: mac} (лише завершені записи, прапорець 0x2)."""
    out = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 4 and f[2] != "0x0":
            mac = norm_mac(f[3])
            if usable_mac(mac):
                out[f[0]] = mac
    return out


def parse_arp_a(text):
    """Вивід `arp -a` (Windows: «192.168.1.5  aa-bb-..», macOS: «? (192.168.1.5) at a:b:.. on en0») → {ip: mac}."""
    out = {}
    for line in text.splitlines():
        ip, mac = IP_RE.search(line), MAC_RE.search(line)
        if ip and mac:
            m = norm_mac(mac[1])
            if usable_mac(m):
                out[ip[1]] = m
    return out


def neighbours():
    if os.path.exists("/proc/net/arp"):
        try:
            with open("/proc/net/arp", encoding="ascii", errors="replace") as f:
                return parse_proc_arp(f.read())
        except OSError:
            pass
    try:
        cmd = ["arp", "-a"] if os.name == "nt" else ["arp", "-an"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: без чорного вікна консолі
        # Байти, а не text=True: на українській/російській Windows `arp -a` пише заголовки кирилицею в кодуванні
        # консолі (cp1125, cp866), і суворе декодування (UTF-8 чи cp1251) падало. IP і MAC — ASCII, решту замінюємо.
        out = subprocess.run(cmd, capture_output=True, timeout=5, creationflags=flags).stdout
        return parse_arp_a(out.decode("ascii", errors="replace"))
    except (OSError, subprocess.SubprocessError):
        return {}


# ── Чи телефон зараз у мережі (присутність без пінгу) ─────────────────────
# Таблиця ARP пам'ятає пристрій ще довго після того, як він пішов (запис «застарілий»), тож просто прочитати її
# мало. Стукаємо в адресу (UDP) — система сама перепитує ARP, — і за кілька секунд беремо лише ті записи,
# що відповіли. Wi-Fi-чип телефона відповідає на ARP навіть коли телефон спить з вимкненим екраном.
PRESENCE_WAIT = 7  # с: Linux перепитує застарілий запис за 5 с (delay_first_probe_time) + ~1 с на відповідь


def parse_ip_neigh(text):
    """Linux `ip -4 neigh show` → {ip: (mac, стан)}: «192.168.1.23 dev wlan0 lladdr da:a1:.. REACHABLE»."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if "lladdr" in f and f.index("lladdr") + 1 < len(f):
            mac = norm_mac(f[f.index("lladdr") + 1])
            if mac and usable_mac(mac):
                out[f[0]] = (mac, f[-1].upper())
    return out


def parse_net_neighbor(text):
    """Windows PowerShell Get-NetNeighbor, рядки «IP MAC Стан» (стан — назва англійською, не перекладається)."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 3 and IP_RE.fullmatch(f[0]):
            mac = norm_mac(f[1])
            if mac and usable_mac(mac):
                out[f[0]] = (mac, f[2].upper())
    return out


def _run(cmd, timeout=5):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags).stdout.decode("ascii", "replace")


def neighbour_states():
    """{ip: (mac, стан)} зі станами записів (REACHABLE — щойно відповів), або None, якщо система їх не дає."""
    try:
        if sys.platform.startswith("linux"):
            return parse_ip_neigh(_run(["ip", "-4", "neigh", "show"], 3))
        if os.name == "nt":
            return parse_net_neighbor(_run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                            "Get-NetNeighbor -AddressFamily IPv4 | ForEach-Object "
                                            "{ $_.IPAddress + ' ' + $_.LinkLayerAddress + ' ' + $_.State }"], 15))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def knock(ips):
    """Порожній UDP-пакет на кожну адресу: система мусить перевірити ARP (застарілий запис — перепитати)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for ip in ips:
                try:
                    s.sendto(b"", (ip, 9))  # порт 9 — «discard»: нікого не турбує
                except OSError:
                    pass
    except OSError:
        pass


def present(ips, wait=PRESENCE_WAIT):
    """Які пристрої зараз справді в мережі: {ip: mac}. Стукаємо в ips, чекаємо, читаємо таблицю.
    Де система дає стан запису (Linux, Windows), беремо лише REACHABLE — «щойно відповів»; інакше всю таблицю."""
    knock(ips)
    time.sleep(wait)
    states = neighbour_states()
    if states is None:
        return neighbours()
    return {ip: mac for ip, (mac, state) in states.items() if state == "REACHABLE"}


# ── Імена: NetBIOS, mDNS, DNS роутера ─────────────────────────────────────
def nbstat_query(tid):
    """Запит NetBIOS «Node Status» на ім'я «*»: Windows відповідає своєю назвою комп'ютера."""
    name = b"*" + b"\x00" * 15
    encoded = bytes(c for b in name for c in (0x41 + (b >> 4), 0x41 + (b & 0x0F)))
    return struct.pack(">HHHHHH", tid, 0, 1, 0, 0, 0) + b"\x20" + encoded + b"\x00" + struct.pack(">HH", 0x21, 1)


def parse_nbstat(data):
    """Відповідь NBSTAT → назва комп'ютера (перше унікальне ім'я з суфіксом 0x00) або None."""
    try:
        count = data[56]
        for i in range(count):
            off = 57 + i * 18
            raw, suffix, flags = data[off:off + 15], data[off + 15], struct.unpack(">H", data[off + 16:off + 18])[0]
            if suffix == 0 and not flags & 0x8000:
                name = raw.decode("cp866", "replace").strip()
                return name or None
    except (IndexError, struct.error):
        return None
    return None


def ptr_name(ip):
    return ".".join(reversed(ip.split("."))) + ".in-addr.arpa"


def dns_query(tid, qname, qtype=12):
    q = b"".join(bytes([len(p)]) + p.encode() for p in qname.split(".")) + b"\x00"
    return struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + q + struct.pack(">HH", qtype, 1)


def _read_name(data, off, depth=0):
    labels = []
    while depth < 20:
        n = data[off]
        if n == 0:
            return ".".join(labels), off + 1
        if n & 0xC0 == 0xC0:  # стиснення: вказівник на ім'я раніше в пакеті
            ptr = struct.unpack(">H", data[off:off + 2])[0] & 0x3FFF
            rest, _ = _read_name(data, ptr, depth + 1)
            return ".".join(labels + ([rest] if rest else [])), off + 2
        labels.append(data[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    raise ValueError("loop")


def parse_ptr(data):
    """DNS/mDNS-відповідь → ім'я з першого запису PTR або None."""
    try:
        _tid, _flags, qd, an = struct.unpack(">HHHH", data[:8])
        off = 12
        for _ in range(qd):
            _, off = _read_name(data, off)
            off += 4
        for _ in range(an):
            _, off = _read_name(data, off)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            if rtype == 12:
                return _read_name(data, off)[0] or None
            off += rdlen
    except (IndexError, struct.error, ValueError):
        return None
    return None


def short_host(name):
    """«Galaxy-S23.lan» → «Galaxy-S23»; повне ім'я з доменом лишаємо як є."""
    name = (name or "").rstrip(".")
    low = name.lower()
    for suf in LOCAL_SUFFIXES:
        if low.endswith(suf):
            return name[:-len(suf)]
    return name if "." not in name else name.split(".")[0] if low.endswith(".arpa") else name


def nameservers():
    """DNS-сервери, яких питати про імена в мережі: з /etc/resolv.conf і шлюз за замовчуванням."""
    out = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            out += re.findall(r"^nameserver\s+(\d+\.\d+\.\d+\.\d+)", f.read(), re.M)
    except OSError:
        pass
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if len(p) > 2 and p[1] == "00000000":
                    out.append(socket.inet_ntoa(struct.pack("<I", int(p[2], 16))))
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys(out))


def probe(nets, known_ips=()):
    """Стукає в кожну адресу (заповнює ARP) і збирає імена. Повертає {ip: ім'я}."""
    names = {}
    socks = []
    try:
        nb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        md = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        socks = [nb, md]
        for s in socks:
            s.setblocking(False)
        tid = secrets.randbelow(0xFFFF)
        sent = 0
        for net, own in nets:
            for host in net.hosts():
                ip = str(host)
                if host == own:
                    continue
                for s, packet, port in ((nb, nbstat_query(tid), 137), (md, dns_query(tid, ptr_name(ip)), 5353)):
                    try:
                        s.sendto(packet, (ip, port))
                    except OSError:
                        pass  # мережа зайнята чи адреса недосяжна — ARP однаково спробував
                sent += 1
                if sent % 32 == 0:
                    time.sleep(0.003)  # не засипаємо Wi-Fi пачкою з тисячі пакетів
        dns = []
        servers = nameservers()
        if servers and known_ips:
            ds = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ds.setblocking(False)
            socks.append(ds)
            for i, ip in enumerate(known_ips):
                for srv in servers[:2]:
                    try:
                        ds.sendto(dns_query((tid + i + 1) & 0xFFFF, ptr_name(ip)), (srv, 53))
                    except OSError:
                        pass
                dns.append(ip)
        by_tid = {(tid + i + 1) & 0xFFFF: ip for i, ip in enumerate(dns)}
        end = time.time() + WAIT
        while time.time() < end:
            got = False
            for s in socks:
                try:
                    data, (src, port) = s.recvfrom(2048)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    continue  # ICMP «порт закритий» від пристроїв без NetBIOS
                got = True
                if port == 137:
                    n = parse_nbstat(data)
                    if n:
                        names[src] = n
                elif port == 5353:
                    n = short_host(parse_ptr(data))
                    if n:
                        names.setdefault(src, n)
                elif port == 53 and len(data) > 2:
                    ip = by_tid.get(struct.unpack(">H", data[:2])[0])
                    n = short_host(parse_ptr(data))
                    if ip and n and not n.endswith(".in-addr.arpa"):
                        names.setdefault(ip, n)
            if not got:
                time.sleep(0.05)
    except OSError as e:
        _state["error"] = f"мережа недоступна: {e}"
    finally:
        for s in socks:
            s.close()
    return names


# ── Виробник за MAC ───────────────────────────────────────────────────────
def vendors(macs):
    """{oui: виробник} для потрібних MAC: шукаємо в базі OUI (якщо вона є на сервері або в папці даних)."""
    want = {m[:8].replace(":", "").upper() for m in macs if not private_mac(m)} - set(_oui_cache)
    if want:
        for path in OUI_FILES:
            path = path if os.path.isabs(path) else os.path.join(core.DATA_DIR, path)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = re.match(r"\s*([0-9A-Fa-f]{2})[-:]?([0-9A-Fa-f]{2})[-:]?([0-9A-Fa-f]{2})(?:\s+\(hex\))?\s+(.+)", line)
                        if m:
                            key = (m[1] + m[2] + m[3]).upper()
                            if key in want:
                                _oui_cache[key] = m[4].split("\t")[-1].strip()[:60]
            except OSError:
                continue
            break
        for k in want:
            _oui_cache.setdefault(k, None)
    return {m: _oui_cache.get(m[:8].replace(":", "").upper()) for m in macs}


# ── Модель і назва з User-Agent ───────────────────────────────────────────
def ua_info(ua):
    """(модель, назва телефона) з User-Agent. Застосунок CrewHub шле CrewHubModel/… і CrewHubName/… (URL-кодовано)."""
    ua = ua or ""

    def token(key):
        m = re.search(key + r"/(\S+)", ua)
        return re.sub(r"[\x00-\x1f]", "", unquote(m[1]))[:60].strip() if m else None

    model, name = token("CrewHubModel"), token("CrewHubName")
    if not model:
        m = re.search(r"Android [\d.]+;(?: [a-z]{2}[-_][A-Z]{2};)? ([^;)]+?)(?: Build/[^;)]*)?[;)]", ua)
        if m and m[1].strip() not in ("K", "wv"):
            model = m[1].strip()
        else:
            for k, v in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows ПК"),
                         ("Macintosh", "Mac"), ("CrOS", "Chromebook"), ("Linux", "Linux")):
                if k in ua:
                    model = v
                    break
    return model, name


# ── Сканування ────────────────────────────────────────────────────────────
def scan(conn=None):
    """Одне сканування: оновлює таблицю lan_devices. Повертає кількість пристроїв, видимих зараз."""
    t0 = time.time()
    nets = subnets()
    _state.update(nets=[str(n) for n, _ in nets], error=None if nets else "сервер не в локальній мережі (немає приватної IPv4)")
    ours = lambda ip: any(ipaddress.ip_address(ip) in n for n, _ in nets)  # noqa: E731
    before = [ip for ip in neighbours() if ours(ip)]
    names = probe(nets, before) if nets else {}
    seen = {ip: mac for ip, mac in neighbours().items() if ours(ip)}
    if nets:
        ips_wo_dns = [ip for ip in seen if ip not in names and ip not in before]
        if ips_wo_dns:  # пристрої, що з'явились лише після «стуку», — питаємо DNS роутера про них окремо
            names.update({k: v for k, v in probe([], ips_wo_dns).items() if k not in names})
    own = conn is None
    conn = conn or core.connect()
    try:
        record(conn, seen, names)
    finally:
        if own:
            conn.close()
    _state.update(scan_at=int(time.time()), took=round(time.time() - t0, 1))
    return len(seen)


def record(conn, seen, names):
    """Записує знайдене: {ip: mac}, {ip: ім'я}. Прив'язує пристрій до працівника, якщо той заходив з цього IP."""
    t = core.now()
    visits = dict(core._visits)
    dev_ips = {r["last_ip"]: r for r in conn.execute(
        "select d.last_ip, d.user_id, d.name from devices d where d.last_seen_at > ?", (t - 3 * 86400,))}
    vend = vendors(seen.values())
    with core._lock:
        for ip, mac in seen.items():
            v = visits.get(ip)
            model, phone_name = ua_info(v["ua"]) if v else (None, None)
            uid = (v or {}).get("uid") or (dev_ips[ip]["user_id"] if ip in dev_ips else None)
            name = phone_name or names.get(ip)
            row = conn.execute("select * from lan_devices where mac = ?", (mac,)).fetchone()
            if row:
                conn.execute("update lan_devices set ip = ?, name = coalesce(?, name), model = coalesce(?, model),"
                             " vendor = coalesce(?, vendor), user_id = coalesce(?, user_id), last_seen = ? where mac = ?",
                             (ip, name, model, vend.get(mac), uid, t, mac))
            else:
                conn.execute("insert into lan_devices(mac, ip, name, model, vendor, user_id, first_seen, last_seen)"
                             " values (?, ?, ?, ?, ?, ?, ?, ?)", (mac, ip, name, model, vend.get(mac), uid, t, t))
            conn.execute("update lan_devices set ip = '' where ip = ? and mac != ?", (ip, mac))  # IP перейшов іншому
        conn.execute("delete from lan_devices where last_seen < ?", (t - KEEP_DAYS * 86400,))
        conn.commit()


def devices(conn):
    """Список для «Огляду»: спершу ті, що в мережі зараз, далі нещодавні."""
    t = core.now()
    fresh = (_state["scan_at"] or t) - 60  # видно в останньому скануванні
    rows = conn.execute("select ld.*, u.full_name, u.status as user_status from lan_devices ld left join users u on u.id = ld.user_id"
                        " order by ld.last_seen >= ? desc, ld.user_id is null desc, ld.last_seen desc limit 200", (fresh,)).fetchall()
    out = []
    for r in rows:
        out.append({**dict(r), "online": r["last_seen"] >= fresh, "private": private_mac(r["mac"]),
                    "blocked": bool(core.device_blocked(conn, None, r["mac"])),
                    "label": r["name"] or r["model"] or r["vendor"] or ("Телефон чи ноутбук" if private_mac(r["mac"]) else "Невідомий пристрій")})
    return out


def summary(conn):
    rows = devices(conn)
    online = [r for r in rows if r["online"]]
    return {"rows": rows, "online": len(online), "unknown": sum(1 for r in online if not r["user_id"]),
            "scan_at": _state["scan_at"], "took": _state["took"], "error": _state["error"], "nets": _state["nets"]}


def step():
    conn = core.connect()
    try:
        if core.setting("lan_scan", conn) == "1":
            scan(conn)
    finally:
        conn.close()


def start(interval=SCAN_EVERY):
    core.run_worker("lan", interval, step)
