"""HTTPS у локальній мережі без нічого на телефонах (17.18): безкоштовна назва DuckDNS + безкоштовний сертифікат
Let's Encrypt. Тоді все, що телефон і сервер передають одне одному, зашифровано, і браузер не показує жодних
попереджень. Ключ шифрування для кожного з'єднання браузер і сервер домовляються самі — у коді сайту його немає.

Як це працює:
  1. Адмін реєструє назву на duckdns.org (напр. crewhub.duckdns.org) і вписує її й токен у «Правила».
  2. Сервер сам каже DuckDNS свою адресу в локальній мережі (192.168.0.9) — і при кожній її зміні. Тоді назва в
     робочому Wi-Fi веде прямо на сервер; дані CrewHub через DuckDNS не йдуть.
  3. Сервер сам отримує сертифікат у Let's Encrypt (ACME, RFC 8555): доводить, що назва його, записом TXT у DuckDNS
     (перевірка dns-01 — серверу не треба бути доступним з інтернету). Продовжує сам, коли до кінця менше 30 днів.
  4. Поруч зі звичайним http://IP:5000 відкривається https://назва (порт 443, якщо зайнятий — 5443). Сторінка, відкрита
     по http, сама переходить на https, якщо він з цього телефона відкривається (див. app.js).
Потрібні бібліотеки cryptography і cheroot (requirements.txt) та інтернет на сервері. Без них — як раніше, по http.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import crewhub_core as core

ACME_DIR = os.environ.get("CREWHUB_ACME_DIR", "https://acme-v02.api.letsencrypt.org/directory")
DUCKDNS_URL = "https://www.duckdns.org/update"
RENEW_DAYS = 30        # продовжуємо сертифікат, коли до кінця менше стільки днів (Let's Encrypt дає 90)
TXT_WAIT = 60          # с: поки запис TXT у DuckDNS розійдеться, перш ніж просити Let's Encrypt перевірити
RETRY_AFTER = 3600     # с: не вдалося отримати сертифікат — наступна спроба не раніше
IP_REFRESH = 6 * 3600  # с: навіть без зміни адреси раз на 6 год нагадуємо DuckDNS (раптом хтось переписав)
FALLBACK_PORT = 5443   # порт 443 зайнятий чи без прав (Linux не від root) — працюємо тут
DOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.duckdns\.org")

_state = {"error": None, "info": None, "url": None, "port": None, "expires": None, "ip": None, "ip_at": 0.0,
          "next_try": 0.0, "busy": False, "server": None, "adapter": None, "domain": None}
_lock = threading.Lock()


class TlsError(Exception):
    pass


def available():
    """Чи є бібліотеки для HTTPS (cryptography — сертифікат, cheroot — сервер з шифруванням)."""
    try:
        import cheroot  # noqa: F401
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def normalize_domain(value):
    """«crewhub» чи «https://Crewhub.duckdns.org/» → «crewhub.duckdns.org»; неправильна назва — None."""
    v = (value or "").strip().lower()
    v = re.sub(r"^https?://", "", v).split("/")[0].split(":")[0]
    if v and "." not in v:
        v += ".duckdns.org"
    return v if DOMAIN_RE.fullmatch(v) else None


def config(conn):
    token = os.environ.get("CREWHUB_DUCKDNS_TOKEN") or core.meta_get(conn, "duckdns_token")
    return {"enabled": core.setting("https_enabled", conn) == "1", "domain": normalize_domain(core.setting("https_domain", conn)),
            "token": token, "port": core.setting_int("https_port", conn) or 443}


def tls_dir():
    d = os.path.join(core.DATA_DIR, "tls")
    os.makedirs(d, exist_ok=True)
    return d


def paths():
    d = tls_dir()
    return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem"), os.path.join(d, "domain.txt")


# ── DuckDNS ───────────────────────────────────────────────────────────────
def duckdns(domain, token, **params):
    """Запит до DuckDNS: ip=… — куди веде назва, txt=… — запис для перевірки Let's Encrypt, clear=true — прибрати TXT.
    True — DuckDNS відповів OK."""
    sub = domain[: -len(".duckdns.org")] if domain.endswith(".duckdns.org") else domain
    q = urllib.parse.urlencode({"domains": sub, "token": token, **params})
    try:
        with urllib.request.urlopen(f"{DUCKDNS_URL}?{q}", timeout=20) as r:
            return r.read().decode("ascii", "replace").strip().startswith("OK")
    except (urllib.error.URLError, OSError, ValueError):
        raise TlsError("немає зв'язку з DuckDNS (сервер без інтернету?)") from None


# ── Let's Encrypt (ACME) ──────────────────────────────────────────────────
def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sleep(sec):
    time.sleep(sec)


class Acme:
    """Мінімальний клієнт ACME v2 (RFC 8555): обліковий запис, замовлення, перевірка dns-01, сертифікат. Ключі — EC P-256."""

    def __init__(self, key, directory=ACME_DIR):
        self.key, self.nonce, self.kid = key, None, None
        self.dir = self._json(self._http(directory))

    def _http(self, url, data=None, method=None, headers=None):
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"), headers=headers or {})
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            r = e
        except (urllib.error.URLError, OSError, ValueError):
            raise TlsError("немає зв'язку з Let's Encrypt (сервер без інтернету?)") from None
        with r:
            body = r.read()
            if r.headers.get("Replay-Nonce"):
                self.nonce = r.headers["Replay-Nonce"]
            return getattr(r, "status", None) or r.code, r.headers, body

    @staticmethod
    def _json(resp):
        status, _h, body = resp
        try:
            return json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            raise TlsError(f"Let's Encrypt відповів незрозуміло (HTTP {status})") from None

    def jwk(self):
        n = self.key.public_key().public_numbers()
        return {"crv": "P-256", "kty": "EC", "x": b64(n.x.to_bytes(32, "big")), "y": b64(n.y.to_bytes(32, "big"))}

    def thumbprint(self):
        return b64(hashlib.sha256(json.dumps(self.jwk(), sort_keys=True, separators=(",", ":")).encode()).digest())

    def _sign(self, data):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        r, s = decode_dss_signature(self.key.sign(data, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def post(self, url, payload=None, retry=True):
        """Підписаний запит (payload None — «POST-as-GET»). Повертає (статус, заголовки, тіло)."""
        if not self.nonce:
            self._http(self.dir["newNonce"], method="HEAD")
        protected = {"alg": "ES256", "nonce": self.nonce, "url": url, **({"kid": self.kid} if self.kid else {"jwk": self.jwk()})}
        self.nonce = None
        p64 = b64(json.dumps(protected).encode())
        b = "" if payload is None else b64(json.dumps(payload).encode())
        body = json.dumps({"protected": p64, "payload": b, "signature": b64(self._sign(f"{p64}.{b}".encode()))}).encode()
        status, headers, raw = self._http(url, data=body, headers={"Content-Type": "application/jose+json"})
        if status >= 400:
            err = self._json((status, headers, raw)) if raw else {}
            if retry and err.get("type", "").endswith(":badNonce"):
                return self.post(url, payload, retry=False)
            raise TlsError(f"Let's Encrypt: {err.get('detail') or f'HTTP {status}'}")
        return status, headers, raw

    def post_json(self, url, payload=None):
        return self._json(self.post(url, payload))

    def account(self):
        _s, headers, _b = self.post(self.dir["newAccount"], {"termsOfServiceAgreed": True})
        self.kid = headers.get("Location")
        if not self.kid:
            raise TlsError("Let's Encrypt не створив обліковий запис")

    def poll(self, url, done=("valid",), fail=("invalid",), tries=60, every=3):
        for _ in range(tries):
            obj = self.post_json(url)
            if obj.get("status") in done:
                return obj
            if obj.get("status") in fail:
                return obj
            _sleep(every)
        raise TlsError("Let's Encrypt надто довго перевіряє — спробуємо пізніше")


def _load_or_create_key(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    if os.path.exists(path):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    with open(path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return key


def obtain(domain, token, wait=TXT_WAIT, directory=None):
    """Отримати сертифікат на domain: перевірка dns-01 через TXT у DuckDNS. Повертає (сертифікат PEM, ключ PEM)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    acme = Acme(_load_or_create_key(os.path.join(tls_dir(), "account.key")), directory or ACME_DIR)
    acme.account()
    status, headers, raw = acme.post(acme.dir["newOrder"], {"identifiers": [{"type": "dns", "value": domain}]})
    order, order_url = acme._json((status, headers, raw)), headers.get("Location")
    try:
        for authz_url in order.get("authorizations", []):
            authz = acme.post_json(authz_url)
            if authz.get("status") == "valid":
                continue
            ch = next((c for c in authz.get("challenges", []) if c.get("type") == "dns-01"), None)
            if not ch:
                raise TlsError("Let's Encrypt не запропонував перевірку dns-01")
            txt = b64(hashlib.sha256(f"{ch['token']}.{acme.thumbprint()}".encode()).digest())
            if not duckdns(domain, token, txt=txt, verbose="true"):
                raise TlsError("DuckDNS не прийняв назву чи токен — перевірте їх у «Правилах»")
            _sleep(wait)
            acme.post_json(ch["url"], {})
            authz = acme.poll(authz_url)
            if authz.get("status") != "valid":
                detail = next((c.get("error", {}).get("detail") for c in authz.get("challenges", []) if c.get("error")), None)
                raise TlsError(f"Let's Encrypt не підтвердив назву: {detail or 'перевірка не пройшла'}")
    finally:
        try:
            duckdns(domain, token, txt="removed", clear="true")
        except TlsError:
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False).sign(key, hashes.SHA256()))
    acme.post_json(order["finalize"], {"csr": b64(csr.public_bytes(serialization.Encoding.DER))})
    order = acme.poll(order_url)
    if order.get("status") != "valid" or not order.get("certificate"):
        raise TlsError("Let's Encrypt не видав сертифікат")
    cert_pem = acme.post(order["certificate"])[2]
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return cert_pem, key_pem


def cert_expires(path):
    """Коли закінчується сертифікат (datetime UTC) або None."""
    try:
        from cryptography import x509
        with open(path, "rb") as f:
            c = x509.load_pem_x509_certificate(f.read())
        return getattr(c, "not_valid_after_utc", None) or c.not_valid_after.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, ImportError):
        return None


def need_cert(domain):
    cert, _key, dom = paths()
    try:
        with open(dom, encoding="utf-8") as f:
            same = f.read().strip() == domain
    except OSError:
        same = False
    exp = cert_expires(cert)
    return not same or not exp or (exp - datetime.now(timezone.utc)).days < RENEW_DAYS


def save_cert(domain, cert_pem, key_pem):
    cert, key, dom = paths()
    for path, data in ((key, key_pem), (cert, cert_pem)):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        if path == key:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)
    with open(dom, "w", encoding="utf-8") as f:
        f.write(domain)


# ── Сервер з шифруванням ──────────────────────────────────────────────────
def start_server(port, app=None):
    """HTTPS-сервер (cheroot) поруч зі звичайним: порт port, якщо не вийде — FALLBACK_PORT. Повертає фактичний порт."""
    from cheroot import wsgi
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    cert, key, _dom = paths()
    last = None
    for p in dict.fromkeys((port, FALLBACK_PORT)):
        srv = wsgi.Server(("0.0.0.0", p), app or core.app, numthreads=16, server_name="crewhub")
        srv.ssl_adapter = BuiltinSSLAdapter(cert, key)
        try:
            srv.prepare()
        except OSError as e:  # зайнятий чи без прав (Linux: порти до 1024 — лише root)
            last = e
            continue
        threading.Thread(target=srv.serve, daemon=True, name="crewhub-https").start()
        _state.update(server=srv, adapter=srv.ssl_adapter, port=p)
        return p
    raise TlsError(f"не вдалося відкрити порт {port} чи {FALLBACK_PORT}: {last}")


def stop_server():
    srv = _state.get("server")
    if srv:
        try:
            srv.stop()
        except Exception:  # noqa: BLE001
            pass
    _state.update(server=None, adapter=None, port=None, url=None)


def reload_cert():
    """Новий сертифікат — для нових з'єднань, без перезапуску."""
    ad = _state.get("adapter")
    if ad is not None and getattr(ad, "context", None) is not None:
        cert, key, _dom = paths()
        ad.context.load_cert_chain(cert, key)


def https_url():
    """Адреса з шифруванням, якщо HTTPS зараз працює, інакше None."""
    return _state["url"] if _state.get("server") else None


def step(force=False):
    """Фон (раз на 5 хв): адреса сервера в DuckDNS, сертифікат (отримати / продовжити), HTTPS-сервер."""
    if not _lock.acquire(blocking=False):
        return  # уже працює (отримання сертифіката — до кількох хвилин)
    try:
        conn = core.connect()
        try:
            cfg = config(conn)
        finally:
            conn.close()
        if not cfg["enabled"]:
            if _state.get("server"):
                stop_server()
            _state.update(error=None, info=None)
            return
        if not available():
            _state["error"] = "немає бібліотек cryptography і cheroot — перезапустіть сервер з інтернетом (поставляться самі)"
            return
        if not cfg["domain"] or not cfg["token"]:
            _state["error"] = "впишіть назву DuckDNS (напр. crewhub) і токен"
            return
        if cfg["domain"] != _state.get("domain") and _state.get("server"):
            stop_server()
        _state["domain"] = cfg["domain"]
        ip = core.get_local_ip()
        if ip and (ip != _state["ip"] or time.time() - _state["ip_at"] > IP_REFRESH or force):
            if not duckdns(cfg["domain"], cfg["token"], ip=ip):
                raise TlsError("DuckDNS не прийняв назву чи токен — перевірте їх у «Правилах»")
            _state.update(ip=ip, ip_at=time.time())
        if need_cert(cfg["domain"]):
            if not force and time.time() < _state["next_try"]:
                return
            _state.update(busy=True, info="отримую сертифікат у Let's Encrypt (1–2 хв)…")
            try:
                cert_pem, key_pem = obtain(cfg["domain"], cfg["token"])
            except TlsError:
                _state["next_try"] = time.time() + RETRY_AFTER
                raise
            save_cert(cfg["domain"], cert_pem, key_pem)
            reload_cert()
            core.log.warning("HTTPS: отримано сертифікат на %s", cfg["domain"])
        if not _state.get("server"):
            port = start_server(cfg["port"])
        else:
            port = _state["port"]
        _state.update(url=f"https://{cfg['domain']}" + ("" if port == 443 else f":{port}"), expires=cert_expires(paths()[0]),
                      error=None, info=None if port == cfg["port"] else f"порт {cfg['port']} зайнятий чи без прав — працює на {port}")
    except TlsError as e:
        _state["error"] = str(e)
    except Exception as e:  # noqa: BLE001 — фон не падає
        core.log.exception("HTTPS")
        _state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _state["busy"] = False
        _lock.release()


def status():
    """Для «Правил»: що зараз з HTTPS."""
    return {"url": https_url(), "error": _state["error"], "info": _state["info"], "busy": _state["busy"],
            "expires": _state["expires"], "ip": _state["ip"], "available": available()}


def start(interval=300):
    core.run_worker("tls", interval, step)
