"""Web Push (17.19): системні сповіщення в браузер (Chrome, Firefox, Android) і на iPhone (iOS 16.4+, коли CrewHub
додано «На початковий екран»).

Кожне внутрішнє сповіщення notify() додатково йде пушем на всі пристрої людини, де вона натиснула
«Увімкнути сповіщення». Потрібна бібліотека pywebpush (pip install pywebpush) — без неї сервер працює як раніше,
пуші просто вимкнено. Ключі VAPID створюються один раз і лежать поруч із базою (vapid_private.pem), не в git:
якщо їх видалити, усім доведеться ввімкнути сповіщення заново.

Надсилання — в окремому фоновому потоці через чергу: запит адміна не чекає на сервери Google/Apple/Mozilla.
Підписки, які сервіс пушів назвав мертвими (404/410 — людина вимкнула сповіщення чи видалила застосунок), стираються.
"""
import base64
import json
import logging
import os
import queue
import threading
import time

log = logging.getLogger("crewhub.push")

try:  # необов'язкова залежність
    from pywebpush import WebPushException, webpush
except Exception:  # noqa: BLE001 — немає бібліотеки чи її залежностей
    webpush = None

    class WebPushException(Exception):
        def __init__(self, message="", response=None):
            super().__init__(message)
            self.response = response

KEY_FILE = "vapid_private.pem"
TTL = 24 * 3600            # скільки сервіс пушів тримає сповіщення, якщо телефон вимкнено
MAX_PER_USER = 10           # стільки пристроїв на людину; старіші підписки відкидаються
DEFAULT_SUBJECT = "https://github.com/Drartem34/CrewHUB"  # VAPID "sub": https-адреса або mailto (Apple перевіряє формат)

_state = {"folder": None, "connect": None, "lock": None, "subject": None, "pem": None, "public": None, "error": None}
_q = queue.Queue(maxsize=2000)
_worker = {"thread": None}
_worker_lock = threading.Lock()


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_keys(folder):
    """Шлях до приватного ключа і публічний ключ (base64url, для браузера). Створює пару, якщо її ще немає."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pem = os.path.join(folder, KEY_FILE)
    if os.path.exists(pem):
        with open(pem, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    else:
        os.makedirs(folder, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        tmp = pem + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, pem)
        log.info("Створено ключі VAPID для пушів: %s", pem)
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pem, _b64(raw)


def setup(folder, connect, lock, subject=None):
    """Викликається один раз після init_db. connect() — нове з'єднання з базою, lock — спільний замок записів,
    subject — функція, що повертає https-адресу сервера (або None)."""
    _state.update(folder=folder, connect=connect, lock=lock, subject=subject, pem=None, public=None, error=None)
    if webpush is None:
        _state["error"] = "немає бібліотеки pywebpush"
        return False
    try:
        _state["pem"], _state["public"] = load_keys(folder)
    except Exception as e:  # noqa: BLE001
        _state["error"] = f"ключі VAPID: {e}"
        log.warning("Пуші вимкнено: %s", _state["error"])
        return False
    return True


def enabled():
    return webpush is not None and bool(_state["public"])


def public_key():
    return _state["public"] if enabled() else None


def status_text():
    return "увімкнено" if enabled() else f"вимкнено ({_state['error'] or 'не налаштовано'})"


def valid_subscription(data):
    """(endpoint, p256dh, auth) з JSON PushSubscription браузера або None."""
    if not isinstance(data, dict):
        return None
    endpoint = data.get("endpoint")
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not all(isinstance(x, str) for x in (endpoint, p256dh, auth)):
        return None
    if not endpoint.startswith("https://") or len(endpoint) > 1000 or not (40 <= len(p256dh) <= 200) or not (8 <= len(auth) <= 100):
        return None
    ok = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    if not set(p256dh) <= ok or not set(auth) <= ok:
        return None
    return endpoint, p256dh, auth


def save_subscription(conn, user_id, data, t, ua=None):
    """Зберегти підписку за людиною (той самий браузер після входу в інший акаунт переходить до нового власника)."""
    sub = valid_subscription(data)
    if not sub:
        return False
    endpoint, p256dh, auth = sub
    conn.execute("insert into push_subscriptions(user_id, endpoint, p256dh, auth, ua, created_at) values (?, ?, ?, ?, ?, ?)"
                 " on conflict(endpoint) do update set user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth,"
                 " ua = excluded.ua, created_at = case when push_subscriptions.user_id = excluded.user_id"
                 " then push_subscriptions.created_at else excluded.created_at end",
                 (user_id, endpoint, p256dh, auth, (ua or "")[:300], t))
    conn.execute("delete from push_subscriptions where user_id = ? and id not in"
                 " (select id from push_subscriptions where user_id = ? order by created_at desc, id desc limit ?)",
                 (user_id, user_id, MAX_PER_USER))
    return True


def remove_subscription(conn, user_id, endpoint):
    return conn.execute("delete from push_subscriptions where user_id = ? and endpoint = ?", (user_id, endpoint)).rowcount


def send(user_id, title, body="", url="/", conn=None):
    """Поставити пуш у чергу (не блокує). False — пуші вимкнено або в людини немає жодної підписки
    (conn — з'єднання запиту: дешева перевірка за індексом, щоб не будити фоновий потік дарма)."""
    if not enabled() or not user_id:
        return False
    if conn is not None and not conn.execute("select 1 from push_subscriptions where user_id = ? limit 1", (user_id,)).fetchone():
        return False
    _ensure_worker()
    try:
        _q.put_nowait((int(user_id), str(title)[:200], str(body or "")[:500], url or "/"))
    except queue.Full:
        log.warning("Черга пушів переповнена — сповіщення «%s» лише в застосунку", title)
        return False
    return True


def flush(timeout=10):
    """Дочекатися, поки черга спорожніє (для тестів і перевірок)."""
    done = threading.Event()

    def wait():
        _q.join()
        done.set()
    threading.Thread(target=wait, daemon=True).start()
    return done.wait(timeout)


def _ensure_worker():
    with _worker_lock:
        t = _worker["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_loop, daemon=True, name="crewhub-push")
            _worker["thread"] = t
            t.start()


def _loop():
    while True:
        item = _q.get()
        try:
            deliver(*item)
        except Exception:  # noqa: BLE001 — один збій не зупиняє потік
            log.exception("Пуш не надіслано")
        finally:
            _q.task_done()


def _subject():
    fn = _state["subject"]
    try:
        s = fn() if callable(fn) else fn
    except Exception:  # noqa: BLE001
        s = None
    return s if isinstance(s, str) and s.startswith(("https://", "mailto:")) else DEFAULT_SUBJECT


def deliver(user_id, title, body="", url="/"):
    """Надіслати одразу (у фоновому потоці). Повертає кількість пристроїв, які сервіс пушів прийняв."""
    if not enabled():
        return 0
    conn = _state["connect"]()
    try:
        subs = conn.execute("select id, endpoint, p256dh, auth from push_subscriptions where user_id = ?", (user_id,)).fetchall()
    finally:
        conn.close()
    if not subs:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": f"crewhub-{user_id}-{abs(hash((title, body))) % 10**8}"},
                         ensure_ascii=False)
    sent, gone, ok_ids = 0, [], []
    for sid, endpoint, p256dh, auth in subs:
        try:
            webpush({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}, payload,
                    vapid_private_key=_state["pem"], vapid_claims={"sub": _subject()}, ttl=TTL, timeout=10)
            sent += 1
            ok_ids.append(sid)
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                gone.append(sid)
            else:
                log.warning("Пуш не прийнято (%s): %s", code, str(e)[:200])
        except Exception as e:  # noqa: BLE001 — мережа, таймаут
            log.warning("Пуш не надіслано: %s", str(e)[:200])
    if gone or ok_ids:
        lock = _state["lock"] or threading.Lock()
        with lock:
            conn = _state["connect"]()
            try:
                if gone:
                    conn.execute(f"delete from push_subscriptions where id in ({','.join('?' * len(gone))})", gone)
                if ok_ids:
                    conn.execute(f"update push_subscriptions set last_ok_at = ? where id in ({','.join('?' * len(ok_ids))})",
                                 [int(time.time())] + ok_ids)
                conn.commit()
            finally:
                conn.close()
    return sent
