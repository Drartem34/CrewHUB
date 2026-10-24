"""CrewHub Test — запуск одним файлом: python start.py

Піднімає сервер, фонову перевірку Wi-Fi і живий монітор у консолі.
Телефони в тій самій Wi-Fi мережі відкривають адресу «Локальна мережа».
"""
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import settings_file
from crewhub_core import (DATA_DIR, DB_PATH, LOG_DIR, app, check_db, get_local_ip, init_db, local_ips, migrate_legacy_data,
                          start_backups, start_sweeper)

# Порт: змінна середовища → [server] port у settings.ini (папка даних) → 5000
PORT = int(os.environ.get("CREWHUB_PORT") or settings_file.port() or settings_file.DEFAULT_PORT)


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"


# Красива панель (rich), якщо бібліотека є і це справжнє вікно терміналу
try:
    import tui
    RICH = sys.stdout.isatty() and os.environ.get("CREWHUB_PLAIN") != "1"
except ImportError:
    tui = None
    RICH = False
console = tui.Console() if RICH else None


def print_status(name, ok, note="", optional=False):
    if RICH:
        tui.step(console, name, ok, note, optional)
        return
    dots = "." * max(35 - len(name), 2)
    print(f" {name} {dots} [{Colors.GREEN + ' OK ' if ok else Colors.RED + ' NO '}{Colors.RESET}]")


def visible_len(s):
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


# === ЛОГ ===
# Один файл logs/crewhub.log у папці даних з ротацією (5 × 1 МБ): не росте безкінечно і не множиться з кожним
# перезапуском. Сюди пишуть помилки запитів, фонових потоків (контроль Wi-Fi, бекап, папки) і запуску.
os.makedirs(LOG_DIR, exist_ok=True)
log_path = os.path.join(LOG_DIR, "crewhub.log")
_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
master_log = logging.getLogger("crewhub")
for _lg in (master_log, app.logger):
    _lg.addHandler(_handler)
    _lg.setLevel(logging.INFO)
master_log.info("=== Запуск CrewHub ===")
for _old in os.listdir(LOG_DIR):  # журнали старих версій (новий файл на кожен запуск): прибираємо старші за 30 днів
    _p = os.path.join(LOG_DIR, _old)
    if _old.startswith("crewhub_") and _old.endswith(".txt") and time.time() - os.path.getmtime(_p) > 30 * 86400:
        os.remove(_p)


def excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    master_log.critical("Необроблена помилка", exc_info=(exc_type, exc, tb))
    print(f"\n{Colors.RED}[!] Помилка. Лог: {log_path}{Colors.RESET}\n")


sys.excepthook = excepthook

# === НЕОБОВ'ЯЗКОВЕ: захист від спаму і тунель ===
limiter_ok = False
try:
    from flask_limiter import Limiter

    import crewhub_core
    # Ключ — той самий IP, що бачить облік змін (заголовок клієнта сам по собі не враховується)
    Limiter(crewhub_core.client_ip, app=app, default_limits=["300 per minute", "20 per second"])
    limiter_ok = True
except ImportError:
    pass

OTHER_IPS = []
PUBLIC_URL = "вимкнено (pip install pinggy, щоб відкрити з інтернету)"
INFO = {"other_ips": OTHER_IPS, "public_url": "вимкнено"}  # для панелі rich


def start_tunnel():
    global PUBLIC_URL
    try:
        import pinggy
    except ImportError:
        return
    PUBLIC_URL = "Очікування підключення..."
    import crewhub_core
    try:
        time.sleep(2)
        try:
            # xff: тунель сам дописує справжній IP телефона в X-Forwarded-For — лише тоді йому можна вірити
            tunnel = pinggy.start_tunnel(forwardto=f"localhost:{PORT}", xff=True)
            crewhub_core.TRUST_PROXY = True
        except TypeError:  # стара версія pinggy без xff: через тунель можна дивитися, але не відмічатись
            tunnel = pinggy.start_tunnel(forwardto=f"localhost:{PORT}")
        urls = getattr(tunnel, "urls", [])
        PUBLIC_URL = urls[0] if urls else "тунель створено (лінк не розпізнано)"
    except Exception as e:  # noqa: BLE001
        PUBLIC_URL = f"помилка тунелю: {e}"
    INFO["public_url"] = PUBLIC_URL


def live_monitor(url):
    try:
        import psutil
    except ImportError:
        psutil = None
    from crewhub_core import connect, live_monitor as monitor_rows

    width = 85
    drawn = 0
    proc = psutil.Process(os.getpid()) if psutil else None
    labels = {"on_site": "на точці", "on_break": "перерва", "wifi_lost": "без Wi-Fi", "late": "спізнюється",
              "scheduled": "скоро зміна", "off": "не на зміні"}
    colors = {"on_site": Colors.GREEN, "on_break": Colors.YELLOW, "wifi_lost": Colors.RED, "late": Colors.RED}

    def line(text):
        return f"{Colors.CYAN}│{Colors.RESET}{text}{' ' * max(width - visible_len(text) - 2, 0)}{Colors.CYAN}│{Colors.RESET}\n"

    while True:
        try:
            conn = connect()
            rows = monitor_rows(conn)
            conn.close()
            box = f"{Colors.CYAN}┌{'─' * (width - 2)}┐{Colors.RESET}\n"
            box += line(" CrewHub Test · монітор змін")
            box += line(f" Локальна мережа: {url}")
            box += line(f" Глобальна мережа: {PUBLIC_URL}")
            box += line(f" Android-застосунок: {url}/app  (адреса в ньому: {url[7:]})")
            if OTHER_IPS:
                box += line(f" Не відкривається з телефона? Спробуйте: {', '.join(f'{i}:{PORT}' for i in OTHER_IPS)}"[:width - 2])
            box += f"{Colors.CYAN}├{'─' * (width - 2)}┤{Colors.RESET}\n"
            for r in rows:
                st = r["state"]
                extra = ""
                if r["shift"]:
                    w = r["shift"]["worked_seconds"]
                    extra = f" {w // 3600}:{w % 3600 // 60:02d} · IP {r['shift']['last_seen_ip'] or ''}"
                box += line(f" {r['user']['full_name'][:28]:<28} {colors.get(st, '')}{labels[st]:<12}{Colors.RESET}{extra}")
            box += f"{Colors.CYAN}├{'─' * (width - 2)}┤{Colors.RESET}\n"
            if proc:
                box += line(f" Сервер: CPU {proc.cpu_percent(interval=None):.1f}% | RAM {proc.memory_info().rss / 1048576:.1f} MB")
            box += line(" Ctrl+C — зупинити")
            box += f"{Colors.CYAN}└{'─' * (width - 2)}┘{Colors.RESET}"
            if drawn:
                sys.stdout.write(f"\033[{drawn}A\033[J\r")
            sys.stdout.write(box + "\n")
            sys.stdout.flush()
            drawn = box.count("\n") + 1
        except Exception as e:  # noqa: BLE001
            master_log.warning("Монітор у консолі: %s", e)
        time.sleep(3)


if __name__ == "__main__":
    os.system("cls" if os.name == "nt" else "clear")
    if os.name == "nt":
        os.system("")  # вмикає кольори ANSI у Windows-консолі
    if RICH:
        tui.print_logo(console, "облік змін з перевіркою робочого Wi-Fi · запуск сервера")
    else:
        print(f"""{Colors.CYAN}
   ____                    _   _       _
  / ___|_ __ _____      __| | | |_   _| |__
 | |   | '__/ _ \\ \\ /\\ / /| |_| | | | | '_ \\
 | |___| | |  __/\\ V  V / |  _  | |_| | |_) |
  \\____|_|  \\___| \\_/\\_/  |_| |_|\\__,_|_.__/   TEST
{Colors.RESET}""")
        print(f"{Colors.YELLOW} Ініціалізація...{Colors.RESET}\n")

    # Другий запуск на тому ж порту: два «контролери Wi-Fi» і два бекапи на одній базі заважали б один одному.
    # Питаємо, чи хтось уже слухає порт (а не пробуємо його зайняти: після падіння старі з'єднання ще хвилину
    # висять у TIME_WAIT, і тоді автоперезапуск помилково вирішив би, що сервер уже працює)
    import socket
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=2).close()
    except OSError:
        pass  # порт вільний — запускаємось
    else:
        print(f"{Colors.RED} Порт {PORT} уже зайнято: CrewHub (чи інша програма) вже працює. Другий запуск не потрібен.{Colors.RESET}")
        sys.exit(0)  # 0 — щоб start.sh/start.bat не перезапускали його по колу

    try:
        moved = migrate_legacy_data()
    except Exception as e:  # noqa: BLE001 — перенесення не має заважати запуску: стара база лишається на місці
        master_log.exception("Перенесення даних: %s", e)
        moved = f"Не вдалося перенести дані зі старої папки ({e}). Подробиці в {log_path}"
    if moved:
        master_log.warning(moved)
        print_status("Перенесення даних у папку даних", not moved.startswith("Не вдалося"), moved)
    print_status("Папка даних", True, DATA_DIR)
    problem = check_db()
    if problem:
        print_status("Перевірка бази", False, problem) if RICH else print(f"{Colors.YELLOW} {problem}{Colors.RESET}")
        master_log.warning("База: %s", problem)
    fresh = not os.path.exists(DB_PATH)
    try:
        init_db()
        print_status("База SQLite" + (" (нова, тестові дані)" if fresh else ""), True)
    except Exception as e:  # noqa: BLE001
        master_log.exception("База не запустилась: %s", e)
        print_status("База SQLite", False)
        raise
    import crewhub_core
    try:
        settings_file.sync()
        print_status("Правила й точки (settings.ini)", True, crewhub_core.SETTINGS_FILE)
    except Exception as e:  # noqa: BLE001
        master_log.exception("settings.ini: %s", e)
        print_status("Правила й точки (settings.ini)", False, str(e))
    settings_file.start_sync()
    print_status("Часовий пояс", True, crewhub_core.SERVER_TZ)
    start_sweeper()
    print_status("Контроль Wi-Fi (фон)", True)
    import profiles
    profiles.start_folder_sync()
    print_status("Особові папки працівників", True, profiles.core.PEOPLE_DIR)
    start_backups()
    print_status("Автобекап бази (щодня)", True)
    try:
        import waitress
    except ImportError:
        waitress = None
    print_status("Сервер Waitress (багато потоків)", waitress is not None, "" if waitress else "pip install waitress")
    print_status("Захист від спаму (flask-limiter)", limiter_ok, "" if limiter_ok else "необов'язково", optional=True)
    INFO["waitress"] = waitress is not None
    threading.Thread(target=start_tunnel, daemon=True).start()

    ip = get_local_ip()
    OTHER_IPS[:] = [i for i in local_ips() if i != ip]
    url = f"http://{ip}:{PORT}"
    from crewhub_core import DEMO_ACCOUNTS
    demo = (" адмін 0500000001/admin123 · модератор 0500000002/moder123 · Іван 0671234567/1234" if DEMO_ACCOUNTS
            else " адмін 050 000 00 01 / admin123 — лише для першого входу, далі система попросить свій пароль")
    if RICH:
        console.print(tui.Text.assemble(("\n Сайт ", "dim"), (url, f"bold {tui.ACCENT}"), ("   вхід:", "dim"), (demo, ""), "\n"))
        time.sleep(1.5)
        requests_meter = tui.Requests()
        tui.attach_request_counter(requests_meter)
        dashboard = tui.Dashboard(url, INFO, requests_meter, log_path)
        dashboard.start()
        import atexit
        atexit.register(dashboard.stop)  # повертаємо звичайний екран терміналу після Ctrl+C
    elif sys.stdout.isatty():
        print(f"\n Акаунти:{demo}\n")
        time.sleep(1.5)
        threading.Thread(target=live_monitor, args=(url,), daemon=True).start()
    else:  # служба systemd: журнал без перемальовування екрана
        print(f" Сервер працює: {url}", flush=True)

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        import flask.cli
        flask.cli.show_server_banner = lambda *a, **k: None
    except Exception:  # noqa: BLE001
        pass
    if waitress:
        # Продакшн-сервер: один процес, багато потоків. Один процес — щоб фонова перевірка Wi-Fi і бекап
        # не запускались у кількох копіях (як було б з gunicorn -w 4); для 100+ телефонів цього з запасом.
        logging.getLogger("waitress").setLevel(logging.ERROR)
        # asyncore_use_poll: без ліміту select() на 1024 з'єднання; backlog: черга під ранковий наплив
        try:
            waitress.serve(app, host="0.0.0.0", port=PORT, threads=int(os.environ.get("CREWHUB_THREADS", "48")),
                           connection_limit=4000, backlog=2048, channel_timeout=60, asyncore_use_poll=True, ident="CrewHub")
        except KeyboardInterrupt:
            sys.exit(0)  # зупинили вручну: скрипт запуску не перезапускає
    else:
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
