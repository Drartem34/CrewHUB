"""Перехоплення вже запущеного CrewHub замість «порт зайнято».

Сервер міг лишитися працювати у фоні (після оновлення, з іншого вікна, з автозапуску). Тоді `python start.py`
пропонує зупинити старий процес і запуститись у цьому вікні. Ключ сесій і push-підписки лежать у базі —
пристрої лишаються підключеними, тут їх не чіпаємо. Чужу програму на порту не чіпаємо ніколи.
"""
import os
import socket
import sys
import time

YES = ("", "т", "так", "y", "yes", "д", "да", "t")


def port_busy(port, host="127.0.0.1", timeout=1.0):
    """Хтось слухає порт (з'єднання вдалося)."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _norm(s):
    return (s or "").lower().replace("\\", "/")


def is_crewhub(proc, ps):
    """Процес — це CrewHub: python зі start.py (чи crewhub) у папці test_app / CrewHUB, або зібраний crewhub.exe."""
    try:
        name = _norm(proc.name())
        cmd = [_norm(a) for a in proc.cmdline()]
    except (ps.Error, OSError):
        return False
    if name.startswith("crewhub"):
        return True
    exe = os.path.basename(cmd[0]) if cmd else ""
    if "python" not in name and "python" not in exe:
        return False
    joined = " ".join(cmd)
    if not any(os.path.basename(a) == "start.py" for a in cmd) and "crewhub" not in joined:
        return False
    try:
        cwd = _norm(proc.cwd())
    except (ps.Error, OSError):
        cwd = ""
    hay = joined + " " + cwd
    return "test_app" in hay or "crewhub" in hay


def _conns(proc, ps):
    get = getattr(proc, "net_connections", None) or proc.connections  # psutil < 6: connections()
    return get(kind="tcp")


def listen_ports(proc, ps):
    """Порти, які процес слухає; None — ОС не дала подивитись."""
    try:
        return {c.laddr.port for c in _conns(proc, ps) if c.status == ps.CONN_LISTEN and c.laddr}
    except (ps.Error, OSError):
        return None


def find_listener(port, ps):
    """Процес, що слухає порт. Спершу загальна таблиця з'єднань (на Linux без root бачить свої процеси,
    на macOS може відмовити), потім — перебір процесів CrewHub (Windows / macOS без прав)."""
    try:
        for c in ps.net_connections(kind="tcp"):
            if c.status == ps.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return ps.Process(c.pid)
    except (ps.Error, OSError):
        pass
    unsure = []
    for p in ps.process_iter():
        if p.pid == os.getpid() or not is_crewhub(p, ps):
            continue
        ports = listen_ports(p, ps)
        if ports is None:
            unsure.append(p)
        elif port in ports:
            return p
    return unsure[0] if len(unsure) == 1 else None  # не змогли перевірити, але CrewHub рівно один — це він


def system_service(proc):
    """Ім'я служби systemd, якщо процес запущено нею (Restart=always підніме його знову після зупинки)."""
    try:
        with open(f"/proc/{proc.pid}/cgroup", encoding="utf-8") as f:
            for line in f:
                path = line.strip().split(":", 2)[-1]
                if path.startswith("/system.slice/") and path.endswith(".service"):
                    return path.rsplit("/", 1)[-1]
    except OSError:
        pass
    return None


def launcher_loop(proc, ps):
    """Батьківський start.sh / start.bat з автоперезапуском: його треба зупинити першим, інакше він
    підніме старий сервер знову (на Windows terminate дає код 1 — це для start.bat «падіння»)."""
    try:
        parent = proc.parent()
        if parent is None:
            return None
        cmd = " ".join(_norm(a) for a in parent.cmdline())
        mine = {p.pid for p in ps.Process(os.getpid()).parents()} | {os.getpid()}
    except (ps.Error, OSError):
        return None
    if parent.pid in mine:
        return None
    return parent if ("start.sh" in cmd or "start.bat" in cmd) else None


def stop(proc, ps, timeout=10):
    """М'яко зупиняє процес: terminate, до timeout с очікування, потім kill. True — процес завершився."""
    loop = launcher_loop(proc, ps)
    for p in ([loop] if loop else []) + [proc]:
        try:
            p.terminate()
        except ps.NoSuchProcess:
            continue
        except (ps.Error, OSError):
            if p is proc:
                return False
    try:
        proc.wait(timeout)
    except ps.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(5)
        except ps.NoSuchProcess:
            pass
        except (ps.Error, OSError):
            return False
    except ps.NoSuchProcess:
        pass
    return True


def wait_free(ports, timeout=10):
    """Чекає, поки порти звільняться. Повертає ті, що досі зайняті."""
    end = time.monotonic() + timeout
    busy = [p for p in ports if port_busy(p)]
    while busy and time.monotonic() < end:
        time.sleep(0.25)
        busy = [p for p in busy if port_busy(p)]
    return busy


def ask(question, inp=input):
    try:
        return inp(question).strip().lower() in YES
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def handle_busy_port(port, colors, extra_ports=(), force=False, interactive=None, inp=input, ps=None):
    """Порт зайнятий. True — старий CrewHub зупинено, можна запускатись; False — вийти, як раніше."""
    C = colors
    ps = ps or _psutil()
    proc = find_listener(port, ps) if ps else None
    if proc is None or not is_crewhub(proc, ps):
        print(f"{C.RED} Порт {port} уже зайнято: CrewHub (чи інша програма) вже працює. Другий запуск не потрібен.{C.RESET}")
        return False
    service = system_service(proc)
    if service:
        print(f"{C.YELLOW} CrewHub уже працює як служба {service} (PID {proc.pid}). Її перезапускає systemd —"
              f" керуйте нею так: sudo systemctl restart {service.removesuffix('.service')}{C.RESET}")
        return False
    if not force:
        if interactive is None:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        if not interactive:
            print(f"{C.RED} Порт {port} уже зайнято: CrewHub уже працює (PID {proc.pid}). Другий запуск не потрібен."
                  f" Перехопити його: python start.py --takeover{C.RESET}")
            return False
        if not ask(f"{C.YELLOW} CrewHub уже працює у фоні (PID {proc.pid}). Перезапустити його тут, у цьому вікні? [Т/н] {C.RESET}",
                   inp):
            print(f"{C.CYAN} Гаразд, старий сервер працює далі.{C.RESET}")
            return False
    held = listen_ports(proc, ps)
    ports = [port] + [p for p in extra_ports if p != port and (held is None or p in held)]
    ports += sorted(p for p in (held or ()) if p not in ports)
    print(f"{C.CYAN} Зупиняю старий сервер (PID {proc.pid})...{C.RESET}", flush=True)
    if not stop(proc, ps):
        print(f"{C.RED} Не вдалося зупинити процес {proc.pid} (немає прав?). Зупиніть його вручну.{C.RESET}")
        return False
    busy = wait_free(ports)
    if port in busy:
        print(f"{C.RED} Старий сервер зупинено, але порт {port} досі зайнятий. Спробуйте ще раз за хвилину.{C.RESET}")
        return False
    if busy:
        print(f"{C.YELLOW} Порт {', '.join(map(str, busy))} ще зайнятий іншою програмою — HTTPS може не запуститись.{C.RESET}")
    print(f"{C.GREEN} Старий сервер зупинено. Пристрої лишаються підключеними — запускаюсь тут.{C.RESET}\n", flush=True)
    return True
