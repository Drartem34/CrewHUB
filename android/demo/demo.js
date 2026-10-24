// CrewHub Демо: сторінки — знімки з тестовими даними, сервера немає. Цей скрипт підміняє запити до сервера:
// зміна рахується тут, на телефоні (localStorage), форми нічого не зберігають, а посилання ведуть на збережені сторінки.
(function () {
  window.DEMO = true;
  const ROLE = (location.pathname.match(/^\/p\/([ae])\//) || [])[1] || null;  // e — працівник, a — адмін
  const KEY = "crewhub_demo_shift";
  const RATE = 120, LOCATION = "Центр", IP = "192.168.1.23";
  const now = () => Math.floor(Date.now() / 1000);
  const toast = (t) => (window.toast ? window.toast(t) : null);
  const NOTE = "Це демо: дані не зберігаються. Повна версія працює з вашим сервером.";

  function load() { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { return null; } }
  function save(s) { try { s ? localStorage.setItem(KEY, JSON.stringify(s)) : localStorage.removeItem(KEY); } catch (e) {} }

  // Стан зміни в тому вигляді, як його віддає справжній сервер (/api/clock/*)
  function stats(s) {
    if (!s) return null;
    const t = now(), end = s.ended_at || t;
    let unpaid = 0, paid = 0, openBreak = null;
    for (const b of s.breaks) {
      const d = Math.max((b.ended_at || end) - b.started_at, 0);
      if (b.is_paid) paid += d; else unpaid += d;
      if (!b.ended_at) openBreak = b;
    }
    const worked = Math.max(end - s.started_at - unpaid, 0);
    const earnings = Math.round(worked / 3600 * RATE * 100) / 100;
    return { id: 1, started_at: s.started_at, ended_at: s.ended_at || null, status: s.status, hourly_rate: RATE,
      location_id: 1, gross_seconds: end - s.started_at, unpaid_break_seconds: unpaid, paid_break_seconds: paid,
      worked_seconds: worked, earnings, base_earnings: earnings, extra_earnings: 0,
      rate_now: s.status === "active" ? RATE : (openBreak && openBreak.is_paid ? RATE : 0), open_break: openBreak };
  }
  function state(extra) {
    return Object.assign({ ok: true, on_work_network: true, location: LOCATION, ip: IP, shift: stats(load()),
      heartbeat_seconds: 20, auto_start: false, why: null, next_check: null }, extra || {});
  }
  function clock(action, body) {
    let s = load();
    const t = now();
    if (action === "start") {
      if (!s) save({ started_at: t, status: "active", breaks: [] });
      return state(s ? { already: true } : null);
    }
    if (!s) return ["check", "auto", "heartbeat"].includes(action) ? state() : { ok: false, error: "Немає відкритої зміни" };
    if (action === "break_start" && s.status === "active") {
      const kind = body.kind === "rest" ? "rest" : "lunch";
      s.breaks.push({ kind, is_paid: kind === "rest" ? 1 : 0, rate_pct: 100, started_at: t, ended_at: null });
      s.status = "paused";
    } else if (action === "break_end" && s.status === "paused") {
      s.breaks.forEach((b) => { if (!b.ended_at) b.ended_at = t; });
      s.status = "active";
    } else if (action === "stop") {
      s.breaks.forEach((b) => { if (!b.ended_at) b.ended_at = t; });
      s.ended_at = t; s.status = "closed";
      const closed = stats(s);
      save(null);
      return state({ closed_shift: closed });
    }
    save(s);
    return state();
  }

  const json = (data, status) => Promise.resolve(new Response(JSON.stringify(data), { status: status || 200, headers: { "Content-Type": "application/json" } }));
  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin !== location.origin) return realFetch(input, init);
    const p = url.pathname;
    let body = {};
    try { body = init && init.body ? JSON.parse(init.body) : {}; } catch (e) {}
    if (p.startsWith("/api/clock/")) return json(clock(p.slice(11), body));
    if (p === "/api/live") return realFetch("/demo/live.json");
    if (p === "/schedule/toggle") {  // клітинка місячного графіка: показуємо, ніби зберегли
      const c = document.querySelector(`.mcell[data-uid="${body.user_id}"][data-day="${body.day}"]`);
      return json({ ok: true, on: !(c && c.classList.contains("on")), label: "09:00–18:00" });
    }
    if (p === "/schedule/dayoff") {
      const c = document.querySelector(`.mcell[data-uid="${body.user_id}"][data-day="${body.day}"]`);
      return json({ ok: true, off: !!(c && c.dataset.ab === "day_off") });
    }
    if (p.startsWith("/p/") || p.startsWith("/static/") || p.startsWith("/demo/") || p === "/" || p === "/index.html") return realFetch(input, init);
    return json({ ok: false, error: NOTE });
  };

  // Адреса сервера → збережена сторінка (те саме правило, що в build_demo.py). Потрібне для посилань, які
  // сторінка малює сама (список «Хто зараз де» оновлюється скриптом і веде на /admin/employee/N)
  let pages = new Set();
  realFetch("/demo/pages.json").then((r) => r.json()).then((l) => { pages = new Set(l); }).catch(() => {});
  function saved(href) {
    const u = new URL(href, location.href), q = u.searchParams;
    if ([...q.keys()].some((k) => !["tab", "view", "archive", "only"].includes(k))) return null;
    const sfx = (q.get("view") === "month" ? "_month" : "") + (q.get("archive") === "1" ? "_archive" : "") + (q.get("only") === "bad" ? "_bad" : "");
    let k = null, m;
    if (u.pathname === "/me") k = "me_" + (q.get("tab") || "clock") + sfx;
    else if (u.pathname === "/admin") k = (q.get("tab") || "live") + sfx;
    else if ((m = u.pathname.match(/^\/admin\/employee\/(\d+)$/))) k = "emp_" + m[1];
    const path = k && ROLE ? `/p/${ROLE}/${k}.html` : null;
    return path && pages.has(path) ? path : null;
  }

  // Посилання на те, чого в демо немає (інший місяць, CSV, завантаження), і форми — пояснюємо замість переходу
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a) return;
    const href = a.getAttribute("href");
    if (!href || href[0] !== "/" || href.startsWith("/p/") || href === "/" ) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const to = saved(href);
    if (to) { if (window.crewNavGo) window.crewNavGo(to); else location.href = to; return; }
    toast("У демо цієї сторінки немає. " + NOTE);
  }, true);

  document.addEventListener("submit", (e) => {
    const f = e.target;
    const action = (f.getAttribute("action") || "").split("?")[0];
    e.preventDefault();
    e.stopImmediatePropagation();
    if (action === "/logout") { location.href = "/"; return; }
    if (f.id === "loginForm" || action === "/login") {
      const who = (f.username && f.username.value || "").replace(/\D/g, "");
      // 050 000 00 01 (адмін) і 050 000 00 02 (модератор) — керування; решта тестових акаунтів — працівник
      if (["500000001", "500000002"].includes(who.slice(-9))) location.href = "/p/a/live.html";
      else if (who) location.href = "/p/e/me_clock.html";
      else toast("Натисніть тестовий акаунт нижче");
      return;
    }
    const d = f.closest("dialog");
    if (d && d.open) d.close();
    toast(NOTE);
  }, true);

  document.addEventListener("DOMContentLoaded", () => {
    // Посилання на завантаження застосунку й зміну сервера в демо не потрібні
    document.querySelectorAll('a[href="/app"], a[href^="crewhub://"]').forEach((a) => a.remove());
  });
})();
