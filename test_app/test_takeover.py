"""Перехоплення вже запущеного CrewHub (start.py --takeover). python -m unittest test_takeover -v

Сервер власника не чіпаємо: юніт-тести з підробленим psutil + справжній тимчасовий «CrewHub» на вільному порту.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import takeover


class C:
    GREEN = RED = CYAN = YELLOW = RESET = ""


class FakeError(Exception):
    pass


class NoSuch(FakeError):
    pass


class Timeout(FakeError):
    pass


class FakeProc:
    def __init__(self, pid, name, cmd, cwd="/", ports=(), parent=None, dies_on_term=True):
        self.pid, self._name, self._cmd, self._cwd, self.ports = pid, name, cmd, cwd, set(ports)
        self._parent, self.dies_on_term = parent, dies_on_term
        self.alive, self.calls = True, []

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd

    def cwd(self):
        return self._cwd

    def parent(self):
        return self._parent

    def parents(self):
        return []

    def net_connections(self, kind="tcp"):
        return [SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=p), pid=self.pid) for p in self.ports]

    def terminate(self):
        self.calls.append("terminate")
        if self.dies_on_term:
            self.alive = False

    def kill(self):
        self.calls.append("kill")
        self.alive = False

    def wait(self, timeout=None):
        self.calls.append(f"wait{timeout}")
        if self.alive:
            raise Timeout()


def fake_psutil(procs, table=True):
    by_pid = {p.pid: p for p in procs}

    def net_connections(kind="tcp"):
        if not table:
            raise FakeError("access denied")
        return [c for p in procs if p.alive for c in p.net_connections()]

    def process(pid):
        return by_pid.get(pid) or FakeProc(pid, "python", ["python", "-m", "pytest"])
    return SimpleNamespace(Error=FakeError, NoSuchProcess=NoSuch, TimeoutExpired=Timeout, CONN_LISTEN="LISTEN",
                           net_connections=net_connections, Process=process, process_iter=lambda: list(procs))


def crewhub(pid=4242, ports=(5000, 5443), **kw):
    return FakeProc(pid, "python3", ["/usr/bin/python3", "start.py"], cwd="/home/u/CrewHUB/test_app", ports=ports, **kw)


class TakeoverUnit(unittest.TestCase):
    def setUp(self):
        self.out = []
        for target, value in (("port_busy", lambda *a, **k: False), ("system_service", lambda p: None)):
            patcher = mock.patch.object(takeover, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_handle(self, ps, **kw):
        kw.setdefault("interactive", True)
        with mock.patch("builtins.print", lambda *a, **k: self.out.append(" ".join(map(str, a)))):
            return takeover.handle_busy_port(5000, C, extra_ports=(5443,), ps=ps, **kw)

    def test_recognises_crewhub_but_not_strangers(self):
        ps = fake_psutil([])
        self.assertTrue(takeover.is_crewhub(crewhub(), ps))
        self.assertTrue(takeover.is_crewhub(FakeProc(1, "python.exe", ["C:\\Py\\python.exe", "C:\\CrewHUB\\test_app\\start.py"]), ps))
        self.assertTrue(takeover.is_crewhub(FakeProc(1, "CrewHub.exe", ["CrewHub.exe"]), ps))
        self.assertFalse(takeover.is_crewhub(FakeProc(1, "python3", ["python3", "start.py"], cwd="/home/u/other"), ps))
        self.assertFalse(takeover.is_crewhub(FakeProc(1, "nginx", ["nginx"], cwd="/home/u/CrewHUB/test_app"), ps))
        self.assertFalse(takeover.is_crewhub(FakeProc(1, "python3", ["python3", "-m", "http.server", "5000"]), ps))

    def test_enter_means_yes_and_old_server_is_stopped(self):
        old = crewhub()
        ok = self.run_handle(fake_psutil([old]), inp=lambda q: self.out.append(q) or "")
        self.assertTrue(ok)
        self.assertEqual(old.calls[0], "terminate")
        self.assertNotIn("kill", old.calls)
        self.assertTrue(any("CrewHub уже працює у фоні (PID 4242). Перезапустити його тут, у цьому вікні? [Т/н]" in s for s in self.out))

    def test_answer_no_keeps_old_server(self):
        old = crewhub()
        self.assertFalse(self.run_handle(fake_psutil([old]), inp=lambda q: "н"))
        self.assertEqual(old.calls, [])

    def test_not_a_tty_exits_as_before(self):
        old = crewhub()
        self.assertFalse(self.run_handle(fake_psutil([old]), interactive=False, inp=lambda q: self.fail("питання без tty")))
        self.assertEqual(old.calls, [])
        self.assertTrue(any("--takeover" in s for s in self.out))

    def test_takeover_flag_skips_question(self):
        old = crewhub()
        self.assertTrue(self.run_handle(fake_psutil([old]), force=True, interactive=False, inp=lambda q: self.fail("питання")))
        self.assertFalse(old.alive)

    def test_kill_after_timeout(self):
        old = crewhub(dies_on_term=False)
        self.assertTrue(self.run_handle(fake_psutil([old]), force=True))
        self.assertEqual(old.calls, ["terminate", "wait10", "kill", "wait5"])

    def test_foreign_program_is_not_touched(self):
        nginx = FakeProc(77, "nginx", ["nginx"], ports=(5000,))
        self.assertFalse(self.run_handle(fake_psutil([nginx]), force=True))
        self.assertEqual(nginx.calls, [])
        self.assertTrue(any("CrewHub (чи інша програма)" in s for s in self.out))

    def test_no_psutil_exits_as_before(self):
        with mock.patch.object(takeover, "_psutil", lambda: None):
            self.assertFalse(self.run_handle(None, force=True))

    def test_found_by_scanning_processes_when_table_denied(self):  # macOS без прав / Windows
        old = crewhub()
        ps = fake_psutil([FakeProc(5, "python3", ["python3", "x.py"]), old], table=False)
        self.assertIs(takeover.find_listener(5000, ps), old)
        self.assertIsNone(takeover.find_listener(6000, ps))

    def test_launcher_loop_stopped_first(self):
        loop = FakeProc(10, "bash", ["bash", "./start.sh"])
        old = crewhub(parent=loop)
        self.assertTrue(self.run_handle(fake_psutil([old, loop]), force=True))
        self.assertEqual(loop.calls, ["terminate"])
        self.assertFalse(old.alive)

    def test_systemd_service_is_not_fought(self):
        old = crewhub()
        with mock.patch.object(takeover, "system_service", lambda p: "crewhub.service"):
            self.assertFalse(self.run_handle(fake_psutil([old]), force=True))
        self.assertEqual(old.calls, [])
        self.assertTrue(any("systemctl restart crewhub" in s for s in self.out))

    def test_ports_to_wait(self):
        seen = []
        with mock.patch.object(takeover, "wait_free", lambda ports, timeout=10: seen.append(ports) or []):
            self.run_handle(fake_psutil([crewhub(ports=(5000, 5443))]), force=True)
            self.run_handle(fake_psutil([crewhub(pid=1, ports=(5000,))]), force=True)  # 5443 чужий — не чекаємо
        self.assertEqual(seen, [[5000, 5443], [5000]])

    def test_port_still_busy_aborts(self):
        with mock.patch.object(takeover, "wait_free", lambda ports, timeout=10: [5000]):
            self.assertFalse(self.run_handle(fake_psutil([crewhub()]), force=True))


SERVER = """import socket, signal, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(5)
if len(sys.argv) > 2: signal.signal(signal.SIGTERM, signal.SIG_IGN)  # «завислий» сервер — лише kill
print("ready", flush=True)
time.sleep(120)
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(takeover._psutil(), "немає psutil")
class TakeoverReal(unittest.TestCase):
    """Справжній тимчасовий процес на вільному порту (не 5000 — там може працювати сервер власника)."""

    def spawn(self, folder, *extra):
        d = os.path.join(self.tmp.name, folder)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "start.py"), "w") as f:
            f.write(SERVER)
        port = free_port()
        p = subprocess.Popen([sys.executable, "start.py", str(port), *extra], cwd=d, stdout=subprocess.PIPE)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), b"ready")
        return p, port

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def handle(self, port, **kw):
        with mock.patch("builtins.print"):
            return takeover.handle_busy_port(port, C, **kw)

    def test_takeover_real_process(self):
        p, port = self.spawn("CrewHUB/test_app")
        self.assertTrue(takeover.port_busy(port))
        self.assertTrue(self.handle(port, force=True))
        self.assertIsNotNone(p.poll())
        self.assertFalse(takeover.port_busy(port))

    def test_kill_when_terminate_ignored(self):
        if os.name == "nt":
            self.skipTest("на Windows terminate і так жорсткий")
        p, port = self.spawn("CrewHUB/test_app", "ignore-term")
        t = time.monotonic()
        self.assertTrue(self.handle(port, force=True))
        self.assertIsNotNone(p.poll())  # код не перевіряємо: дитину вже «забрав» psutil.wait
        self.assertGreater(time.monotonic() - t, 9)  # спершу чекали 10 с на terminate, потім kill
        self.assertFalse(takeover.port_busy(port))

    def test_foreign_real_process_untouched(self):
        p, port = self.spawn("other_app")
        self.assertFalse(self.handle(port, force=True))
        self.assertIsNone(p.poll())


if __name__ == "__main__":
    unittest.main()
