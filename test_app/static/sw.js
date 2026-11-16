/* CrewHub service worker (17.19): системні сповіщення (Web Push) у браузері й на iPhone.
   Роздається з /sw.js (scope «/»). Нічого не кешує — сторінки завжди свіжі з сервера. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {title: 'CrewHub', body: e.data ? e.data.text() : ''}; }
  const title = d.title || 'CrewHub';
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: d.tag || undefined,
    data: {url: d.url || '/'},
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin && 'focus' in c) {
        try { await c.navigate(url); } catch (err) { /* iOS може не дозволити — просто відкриваємо застосунок */ }
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});

/* Браузер сам оновив підписку (рідко): передаємо нову серверу */
self.addEventListener('pushsubscriptionchange', (e) => {
  e.waitUntil((async () => {
    try {
      const r = await fetch('/push/vapid-public-key', {credentials: 'same-origin'});
      const k = await r.json();
      if (!k.enabled) return;
      const raw = atob(k.key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - k.key.length % 4) % 4));
      const key = Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
      const sub = await self.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
      await fetch('/push/subscribe', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(sub)});
    } catch (err) { /* людина ввімкне заново кнопкою */ }
  })());
});
