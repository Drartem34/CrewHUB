"""Панель сервера в терміналі (бібліотека rich): хто на зміні, події, навантаження, адреси.

start.py вмикає її, якщо rich встановлено і вікно — справжній термінал; інакше лишається простий монітор.
"""
import os
import threading
import time
from collections import deque
from datetime import datetime

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from crewhub_core import (BACKUP_DIR, DB_PATH, EVENTS, app, connect, dashboard_stats, list_backups, live_monitor, server_network,
                          setting)

ACCENT = "#8b7cf6"
STATES = {
    "on_site": ("На точці", "#22c55e"),
    "on_break": ("Перерва", "#f59e0b"),
    "wifi_lost": ("Без Wi-Fi", "#ef4444"),
    "late": ("Спізнюється", "#f97316"),
    "scheduled": ("Скоро зміна", "#38bdf8"),
    "absent": ("Відсутній", "#a1a1aa"),
    "off": ("Не на зміні", "#52525b"),
}
SPARK = "▁▂▃▄▅▆▇█"

LOGO = [
    "  ██████ ██████  ███████ ██     ██ ██   ██ ██    ██ ██████  ",
    " ██      ██   ██ ██      ██     ██ ██   ██ ██    ██ ██   ██ ",
    " ██      ██████  █████   ██  █  ██ ███████ ██    ██ ██████  ",
    " ██      ██   ██ ██      ██ ███ ██ ██   ██ ██    ██ ██   ██ ",
    "  ██████ ██   ██ ███████  ███ ███  ██   ██  ██████  ██████  ",
]
GRADIENT = ["#6366f1", "#7c6cf4", "#8b5cf6", "#a855f7", "#c084fc"]


class Requests:
    """Лічильник запитів до сервера для графіка навантаження (точка раз на 2 с, хвилина історії)."""

    def __init__(self):
        self.count = 0
        self.lock = threading.Lock()
        self.history = deque([0] * 30, maxlen=30)

    def hit(self):
        with self.lock:
            self.count += 1

    def roll(self):
        with self.lock:
            self.history.append(self.count)
            self.count = 0

    @property
    def per_minute(self):
        return sum(self.history)


def print_logo(console, subtitle):
    console.print()
    for line, color in zip(LOGO, GRADIENT):
        console.print(Text(line, style=f"bold {color}"))
    console.print(Text(f"  {subtitle}", style="dim"))
    console.print()


def step(console, name, ok, note="", optional=False):
    if ok:
        mark, style = Text(" ✔ ", style="bold #22c55e"), "bold"
    elif optional:
        mark, style = Text(" ○ ", style="dim"), "dim"
    else:
        mark, style = Text(" ✖ ", style="bold #ef4444"), "bold #ef4444"
    console.print(Text.assemble(mark, (name, style), (f"  {note}" if note else "", "dim")))


def _bar(pct, width=18, color="#22c55e"):
    pct = max(0.0, min(pct, 100.0))
    full = int(round(pct / 100 * width))
    if pct > 85:
        color = "#ef4444"
    elif pct > 60:
        color = "#f59e0b"
    return Text.assemble(("━" * full, color), ("━" * (width - full), "#3f3f46"))


def _spark(values):
    top = max(values) or 1
    return "".join(SPARK[min(int(v / top * (len(SPARK) - 1)), len(SPARK) - 1)] for v in values)


def _dur(secs):
    secs = int(secs or 0)
    return f"{secs // 3600}:{secs % 3600 // 60:02d}"


def _hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""


def _money(v):
    return f"{v:,.0f}".replace(",", " ")


class Dashboard:
    def __init__(self, url, info, requests, log_path):
        self.url = url
        self.info = info  # dict, яку start.py оновлює: public_url, other_ips, waitress, limiter
        self.requests = requests
        self.log_path = log_path
        self.started = time.time()
        self.console = Console()
        try:
            import psutil
            self.proc = psutil.Process(os.getpid())
            self.psutil = psutil
            self.proc.cpu_percent(None)
        except ImportError:
            self.proc = self.psutil = None
        self.live = None
        self.error = None

    # ---- дані ---------------------------------------------------------------
    def collect(self):
        conn = connect()
        try:
            rows = live_monitor(conn)
            stats = dashboard_stats(conn)
            events = conn.execute(
                "select c.created_at, c.event, c.allowed, c.ip, u.full_name from conn_log c"
                " left join users u on u.id = c.user_id order by c.id desc limit 9").fetchall()
            company = setting("company_name", conn)
            currency = setting("currency", conn)
        finally:
            conn.close()
        return rows, stats, events, company, currency

    # ---- частини екрана ------------------------------------------------------
    def header(self, company):
        up = int(time.time() - self.started)
        left = Text.assemble((" ◆ CrewHub ", f"bold white on {ACCENT}"), ("  ", ""), (company, "bold"))
        right = Text.assemble(("● ONLINE", "bold #22c55e"), ("   працює ", "dim"), (f"{up // 3600}:{up % 3600 // 60:02d}:{up % 60:02d}", "bold"),
                              ("   ", ""), (datetime.now().strftime("%d.%m.%Y  %H:%M:%S"), "bold"))
        t = Table.grid(expand=True)
        t.add_column(justify="left")
        t.add_column(justify="right")
        t.add_row(left, right)
        return Panel(t, box=box.HEAVY, border_style=ACCENT, padding=(0, 1))

    def kpis(self, rows, stats, currency, narrow=False):
        counts = {k: 0 for k in STATES}
        for r in rows:
            counts[r["state"]] += 1
        cards = []
        for key in ("on_site", "on_break", "wifi_lost", "late", "absent"):
            label, color = STATES[key]
            cards.append(Panel(Align.center(Text.assemble((f"{counts[key]}\n", f"bold {color}"), (label, "dim"))),
                               box=box.ROUNDED, border_style=color if counts[key] and key != "absent" else "#3f3f46", padding=(0, 1)))
        if narrow:  # вузьке вікно: лише стани і фонд
            cards = cards[:4]
        else:
            cards.append(Panel(Align.center(Text.assemble((f"{stats['today_hours']}\n", "bold"), ("годин сьогодні", "dim"))),
                               box=box.ROUNDED, border_style="#3f3f46", padding=(0, 1)))
        cards.append(Panel(Align.center(Text.assemble((f"{_money(stats['month_total'])}\n", "bold #c084fc"), (f"фонд, {currency}" if narrow else f"фонд за місяць, {currency}", "dim"))),
                           box=box.ROUNDED, border_style="#3f3f46", padding=(0, 1)))
        grid = Table.grid(expand=True, padding=(0, 1))
        for _ in cards:
            grid.add_column(ratio=1)
        grid.add_row(*cards)
        return grid

    def people(self, rows, height):
        t = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold dim", row_styles=["", "on #18181b"], pad_edge=False)
        t.add_column("Працівник", ratio=3, min_width=14, no_wrap=True, overflow="ellipsis")
        t.add_column("Стан", width=13, no_wrap=True)
        t.add_column("Точка", ratio=2, no_wrap=True, overflow="ellipsis")
        t.add_column("З", justify="right", width=5, no_wrap=True)
        t.add_column("Час", justify="right", width=5, no_wrap=True)
        t.add_column("IP · пінг", width=21, no_wrap=True, overflow="ellipsis")
        limit = max(height - 5, 3)
        for r in rows[:limit]:
            label, color = STATES[r["state"]]
            s = r["shift"]
            name_style = "bold" if s else ("" if r["state"] != "off" else "dim")
            ip = ""
            if s:
                ago = int(time.time() - s["last_seen_at"])
                ip = f"{s['last_seen_ip'] or ''} · {ago} с" if ago < 120 else f"{s['last_seen_ip'] or ''} · {ago // 60} хв"
            elif r["schedule"]:
                ip = f"графік {_hm(r['schedule']['starts_at'])}–{_hm(r['schedule']['ends_at'])}"
            t.add_row(Text(r["user"]["full_name"], style=name_style),
                      Text.assemble(("● ", color), (label, color if r["state"] != "off" else "dim")),
                      Text(r["location"] or "", style="dim"),
                      _hm(s["started_at"]) if s else "",
                      _dur(s["worked_seconds"]) if s else "",
                      Text(ip, style="dim"))
        title = f"[bold white]Команда[/]  [#a1a1aa]{len(rows)} осіб · оновлення кожні 2 с[/]"
        more = len(rows) - limit
        sub = f"[#a1a1aa]+ ще {more} (повний список у веб-панелі)[/]" if more > 0 else None
        return Panel(t, title=title, title_align="left", subtitle=sub, subtitle_align="right", box=box.ROUNDED, border_style="#3f3f46")

    def network(self):
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(no_wrap=True, overflow="ellipsis")
        t.add_row("Сайт", Text(self.url, style=f"bold {ACCENT}"))
        t.add_row("Адмінка", Text(self.url + "/admin", style="bold"))
        t.add_row("Застосунок", Text(f"{self.url}/app", style="bold"))
        t.add_row("У телефоні", Text(self.url[7:], style="bold #22c55e"))
        if self.info.get("other_ips"):
            t.add_row("Інші адреси", Text(", ".join(self.info["other_ips"]), style="#f59e0b"))
        t.add_row("Інтернет", Text(self.info.get("public_url") or "вимкнено", style="dim"))
        net = self.server_net()
        if net:  # сервер в одній мережі з телефонами — чи вона прив'язана до точки
            t.add_row("Wi-Fi точки", Text(f"{net['cidr']} → {net['location']['name']}", style="#22c55e") if net["location"] else
                      Text(f"{net['cidr']} не додано: телефони «не в робочій мережі» — «Точки і Wi-Fi»", style="bold #ef4444"))
        return Panel(t, title="[bold white]Підключення[/]", title_align="left", box=box.ROUNDED, border_style="#3f3f46")

    def server_net(self):
        try:
            conn = connect()
            try:
                return server_network(conn)
            finally:
                conn.close()
        except Exception:  # консоль не має падати через базу
            return None

    def system(self):
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(no_wrap=True)
        t.add_column(justify="right", no_wrap=True)
        if self.proc:
            cpu = self.proc.cpu_percent(None) / max(self.psutil.cpu_count() or 1, 1)
            ram = self.proc.memory_info().rss / 1048576
            t.add_row("CPU", _bar(cpu), f"{cpu:4.1f}%")
            t.add_row("RAM", _bar(ram / 1024 * 100, color="#38bdf8"), f"{ram:4.0f} МБ")  # шкала до 1 ГБ
            t.add_row("Потоки", Text(""), str(self.proc.num_threads()))
        t.add_row("Запити", Text(_spark(list(self.requests.history)), style=ACCENT), f"{self.requests.per_minute}/хв")
        size = sum(os.path.getsize(DB_PATH + s) for s in ("", "-wal") if os.path.exists(DB_PATH + s)) / 1048576
        t.add_row("База", Text("SQLite · WAL", style="#22c55e"), f"{size:.1f} МБ")
        backups = list_backups()
        last = datetime.fromtimestamp(backups[0]["mtime"]).strftime("%d.%m %H:%M") if backups else "ще немає"
        t.add_row("Бекап", Text(f"{len(backups)} копій", style="#22c55e" if backups else "#f59e0b"), last)
        try:
            import profiles
            ps = profiles.status()
            t.add_row("Папки", Text(f"{ps['folders']} людей" if not ps["warning"] else "увага, див. «Правила»",
                                    style="#22c55e" if not ps["warning"] else "#f59e0b"), os.path.relpath(ps["dir"]))
        except Exception:  # noqa: BLE001
            pass
        t.add_row("Сервер", Text("Waitress" if self.info.get("waitress") else "Flask (dev)",
                                 style="#22c55e" if self.info.get("waitress") else "#f59e0b"), "")
        return Panel(t, title="[bold white]Сервер[/]", title_align="left", box=box.ROUNDED, border_style="#3f3f46")

    def events(self, events):
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(style="dim", no_wrap=True)
        t.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
        t.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
        for e in events:
            color = "#22c55e" if e["allowed"] else "#ef4444"
            if e["event"] in ("break_start", "break_end", "wifi_restored"):
                color = "#f59e0b" if e["event"] == "break_start" else "#38bdf8"
            t.add_row(_hm(e["created_at"]), Text(e["full_name"] or "сервер", style="bold" if e["full_name"] else "dim"),
                      Text(EVENTS.get(e["event"], e["event"]), style=color))
        if not events:
            t.add_row("", Text("Подій ще немає", style="dim"), "")
        return Panel(t, title="[bold white]Останні події[/]", title_align="left", box=box.ROUNDED, border_style="#3f3f46")

    def footer(self):
        return Text.assemble((" Ctrl+C ", "bold black on #e4e4e7"), (" зупинити   ", "dim"),
                             (" Лог ", "bold black on #e4e4e7"), (f" {self.log_path}   ", "dim"),
                             (" Копії ", "bold black on #e4e4e7"), (f" {os.path.relpath(BACKUP_DIR)}", "dim"))

    def render(self):
        rows, stats, events, company, currency = self.collect()
        height = self.console.size.height
        layout = Layout()
        narrow = self.console.size.width < 110
        layout.split_column(Layout(self.header(company), size=3), Layout(self.kpis(rows, stats, currency, narrow), size=4),
                            Layout(name="body"), Layout(self.footer(), size=1))
        body_h = max(height - 8, 10)
        if not narrow:
            right = Layout()
            right.split_column(Layout(self.network(), size=9 if not self.info.get("other_ips") else 10),
                               Layout(self.system(), size=11), Layout(self.events(events)))
            layout["body"].split_row(Layout(self.people(rows, body_h), ratio=3), Layout(right, ratio=2))
        else:  # вузьке вікно: усе одною колонкою
            layout["body"].split_column(Layout(self.network(), size=9), Layout(self.people(rows, body_h - 9)))
        return layout

    # ---- цикл ----------------------------------------------------------------
    def loop(self):
        while True:
            try:
                self.requests.roll()
                self.live.update(self.render(), refresh=True)
            except Exception as e:  # noqa: BLE001 — панель ніколи не валить сервер
                self.error = e
            time.sleep(2)

    def start(self):
        self.live = Live(self.render(), console=self.console, screen=True, auto_refresh=False, redirect_stderr=False)
        self.live.start()
        threading.Thread(target=self.loop, daemon=True, name="crewhub-tui").start()

    def stop(self):
        if self.live:
            self.live.stop()


def attach_request_counter(requests):
    @app.before_request
    def _count():
        requests.hit()
