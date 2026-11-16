/* CrewHub: системні сповіщення (Web Push, 17.19). Кнопка «Увімкнути сповіщення» в картці [data-push-card].
   Notification.requestPermission() — лише по кліку (вимога iOS). На iPhone пуші працюють тільки з іконки
   на екрані «Додому» (PWA), iOS 16.4+. Без https браузер пуші не дає зовсім. */
(function () {
  const cards = Array.from(document.querySelectorAll('[data-push-card]'));
  if (!cards.length) return;
  const csrf = (document.querySelector('meta[name="crewhub-csrf"]') || {}).content || '';
  const ua = navigator.userAgent;
  const isIOS = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  let reg = null, key = null;

  const q = (c, s) => c.querySelector(s);
  function each(fn) { cards.forEach(fn); }
  function show(sel, on) { each((c) => { const el = q(c, sel); if (el) el.hidden = !on; }); }
  function text(t) { each((c) => { const el = q(c, '[data-push-state]'); if (el && t) el.textContent = t; }); }
  function reveal(onlyFull) {
    each((c) => {
      const compact = c.dataset.compact === '1';
      if (compact && (onlyFull || later())) { c.hidden = true; return; }
      c.hidden = false;
    });
  }
  function later() { try { return Number(localStorage.getItem('crewhub_push_later') || 0) > Date.now(); } catch (e) { return false; } }
  function toast(t) {
    const box = document.getElementById('toasts');
    if (!box) return alert(t);
    const d = document.createElement('div'); d.className = 'toast'; d.textContent = t; box.appendChild(d);
    setTimeout(() => d.remove(), 5000);
  }
  function b64(s) {
    const raw = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4));
    return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
  }
  async function post(url, body) {
    const r = await fetch(url, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body || {})});
    let j = {}; try { j = await r.json(); } catch (e) { /* не JSON */ }
    if (!r.ok || j.ok === false) throw new Error(j.error || ('Помилка ' + r.status));
    return j;
  }
  function onState() {
    text('Сповіщення ввімкнено на цьому пристрої. Сюди прийде відповідь на відпустку чи дей-офф, штраф або бонус.');
    show('[data-push-on]', false); show('[data-push-test]', true); show('[data-push-off]', true);
    reveal(true);  // коротка картка на «Зміні» більше не потрібна
  }
  function offState() {
    show('[data-push-on]', true); show('[data-push-test]', false); show('[data-push-off]', false);
    reveal(false);
  }

  async function init() {
    if (!window.isSecureContext) { show('[data-push-http]', true); reveal(false); return; }
    if (isIOS && !standalone) { show('[data-push-ios]', true); reveal(false); return; }
    if (!supported) {
      text(isIOS ? 'Щоб отримувати сповіщення, оновіть iPhone до iOS 16.4 або новішої і відкрийте CrewHub з іконки на екрані «Додому».'
                 : 'Цей браузер не підтримує системні сповіщення. Спробуйте Chrome, Firefox, Edge або Safari.');
      each((c) => { if (c.dataset.compact !== '1') c.hidden = false; });
      return;
    }
    let k;
    try { k = await (await fetch('/push/vapid-public-key', {credentials: 'same-origin'})).json(); } catch (e) { return; }
    if (!k.enabled) return;  // на сервері пуші вимкнено — картку не показуємо
    key = k.key;
    try { reg = await navigator.serviceWorker.register('/sw.js', {scope: '/'}); } catch (e) { return; }
    const sub = await reg.pushManager.getSubscription();
    if (sub && Notification.permission === 'granted') {
      onState();
      post('/push/subscribe', sub.toJSON()).catch(() => {});  // прив'язати до того, хто зараз в акаунті
      return;
    }
    if (Notification.permission === 'denied') {
      text('Сповіщення заблоковано в налаштуваннях браузера чи телефона. Дозвольте їх для цього сайту і оновіть сторінку.');
      show('[data-push-on]', false);
      each((c) => { if (c.dataset.compact !== '1') c.hidden = false; });
      return;
    }
    offState();
  }

  async function turnOn(btn) {
    btn.disabled = true;
    try {
      const perm = await Notification.requestPermission();  // перший await — ще в межах натискання (iOS)
      if (perm !== 'granted') { toast('Без дозволу сповіщення не приходитимуть'); return; }
      reg = reg || await navigator.serviceWorker.register('/sw.js', {scope: '/'});
      await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(key)});
      await post('/push/subscribe', sub.toJSON());
      onState();
      toast('Сповіщення ввімкнено');
    } catch (e) {
      toast('Не вдалося ввімкнути: ' + (e && e.message ? e.message : e));
    } finally { btn.disabled = false; }
  }

  async function turnOff(btn) {
    btn.disabled = true;
    try {
      const sub = reg && await reg.pushManager.getSubscription();
      if (sub) {
        await post('/push/unsubscribe', {endpoint: sub.endpoint}).catch(() => {});
        await sub.unsubscribe();
      }
      text('Сповіщення на цьому пристрої вимкнено.');
      offState();
    } finally { btn.disabled = false; }
  }

  async function test(btn) {
    btn.disabled = true;
    try { await post('/push/test'); toast('Надіслано — сповіщення має з\'явитися за кілька секунд'); }
    catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  }

  each((c) => {
    const on = q(c, '[data-push-on]'), off = q(c, '[data-push-off]'), t = q(c, '[data-push-test]'), l = q(c, '[data-push-later]');
    if (on) on.addEventListener('click', () => turnOn(on));
    if (off) off.addEventListener('click', () => turnOff(off));
    if (t) t.addEventListener('click', () => test(t));
    if (l) l.addEventListener('click', () => {
      try { localStorage.setItem('crewhub_push_later', String(Date.now() + 7 * 864e5)); } catch (e) { /* приватний режим */ }
      c.hidden = true;
    });
  });
  init();
})();
