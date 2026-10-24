// Миттєві переходи між вкладками без перезавантаження сторінки.
// Натиснули на пункт меню — сторінка вже вантажиться (з моменту дотику), потім міняється лише вміст:
// стилі, іконки, шапка й нижня панель не перемальовуються заново, анімації не обриваються.
// Будь-яка помилка → звичайний перехід, тож гірше, ніж було, не стане.
(function () {
  if (!window.fetch || !window.DOMParser || !history.pushState || !document.querySelector(".main")) return;
  const SWAP = /^\/(me|admin)$|^\/admin\/employee\/\d+$|^\/p\/[ae]\/\w+\.html$/;  // останнє — демо-застосунок
  const cache = new Map(); // url → Promise<{url, html}|null>, живе 15 с
  let navId = 0;

  function target(a) {
    if (!a || a.target || a.hasAttribute("download") || a.dataset.reload !== undefined) return null;
    const href = a.getAttribute("href");
    if (!href || href[0] === "#" || href.startsWith("javascript")) return null;
    const u = new URL(a.href, location.href);
    if (u.origin !== location.origin || !SWAP.test(u.pathname)) return null;
    if (window.DEMO && !u.pathname.startsWith("/p/")) return null;  // у демо посилання сервера перекладає demo.js
    return u;
  }

  function load(url) {
    const hit = cache.get(url);
    if (hit && Date.now() - hit.at < 15000) return hit.p;
    const p = fetch(url, { credentials: "same-origin", headers: { "X-CrewHub-Nav": "1" } })
      .then((r) => (r.ok && SWAP.test(new URL(r.url).pathname) ? r.text().then((html) => ({ url: r.url, html })) : null))
      .catch(() => null);
    cache.set(url, { p, at: Date.now() });
    return p;
  }

  function runScripts(nodes) {
    // Скрипти сторінки виконуються по черзі (clock.js чекає на window.CREWHUB з попереднього inline-скрипта)
    return nodes.reduce((chain, old) => chain.then(() => new Promise((done) => {
      const s = document.createElement("script");
      s.dataset.page = "1";
      for (const at of old.attributes) s.setAttribute(at.name, at.value);
      if (old.src) { s.onload = s.onerror = done; } else s.textContent = old.textContent;
      // isConnected тут не підходить: вузол розібраного документа «підключений» до свого документа
      if (document.contains(old)) old.replaceWith(s); else document.body.appendChild(s);
      if (!old.src) done();
    })), Promise.resolve());
  }

  async function go(url, push) {
    const my = ++navId;
    document.body.classList.add("leaving");
    document.body.classList.remove("menu-open");
    const res = await load(url);
    if (my !== navId) return; // поки вантажилось, натиснули інше
    cache.delete(url);
    if (!res) { location.href = url; return; }
    const doc = new DOMParser().parseFromString(res.html, "text/html");
    const main = doc.querySelector(".main");
    if (!main) { location.href = url; return; }

    dispatchEvent(new Event("crewhub:leave")); // старі таймери (годинник, жива панель) зупиняються
    document.querySelectorAll("dialog[open]").forEach((d) => d.close());
    document.title = doc.title;
    // Меню не перемальовуємо (інакше обірветься рух іконки, яку щойно натиснули): лише активний пункт і лічильники
    const swap = (sel) => {
      const a = document.querySelector(sel), b = doc.querySelector(sel);
      if (!a || !b) return;
      const la = a.querySelectorAll("a"), lb = b.querySelectorAll("a");
      if (la.length !== lb.length) { a.replaceWith(document.adoptNode(b)); return; }
      la.forEach((x, i) => {
        const y = lb[i];
        x.classList.toggle("on", y.classList.contains("on"));
        x.setAttribute("href", y.getAttribute("href"));
        const ob = x.querySelector(".badge"), nb = y.querySelector(".badge");
        if (ob && !nb) ob.remove();
        else if (nb && !ob) x.appendChild(document.adoptNode(nb));
        else if (ob && nb && ob.textContent !== nb.textContent) ob.textContent = nb.textContent;
      });
    };
    swap(".sidebar .nav");
    swap(".bottombar");
    const fresh = document.adoptNode(main);
    document.querySelector(".main").replaceWith(fresh);
    doc.querySelectorAll("#toasts .toast").forEach((t) => window.toast && window.toast(t.textContent));
    if (push) history.pushState({ crewhub: 1 }, "", res.url);
    else if (res.url !== location.href) history.replaceState({ crewhub: 1 }, "", res.url);
    scrollTo(0, 0);
    if (window.crewUndim) window.crewUndim(); else document.body.classList.remove("leaving");

    // Скрипти: ті, що всередині вмісту, і скрипти сторінки після app.js / motion.js / nav.js
    const inMain = Array.from(fresh.querySelectorAll("script"));
    const all = Array.from(doc.body.querySelectorAll(":scope > script"));
    document.querySelectorAll("body > script[data-page]").forEach((s) => s.remove());
    const page = all.filter((s) => !/\/static\/(app|motion|nav)\.js|\/demo\//.test(s.getAttribute("src") || ""));
    if (window.bindChrome) window.bindChrome();
    if (window.crewMotion) window.crewMotion.enter();
    await runScripts(inMain.concat(page));
  }

  // Дотик: сторінку починаємо вантажити ще до того, як палець відпустили
  document.addEventListener("pointerdown", (e) => {
    const u = target(e.target.closest("a[href]"));
    if (u) load(u.href);
  }, { passive: true });

  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const u = target(e.target.closest("a[href]"));
    if (!u) return;
    e.preventDefault();
    if (u.href === location.href && !u.hash) {  // та сама сторінка: лише вгору, без «пригасання»
      scrollTo({ top: 0, behavior: "smooth" });
      document.body.classList.remove("menu-open", "leaving");
      return;
    }
    go(u.href, true).catch(() => { location.href = u.href; });
  });

  window.crewNavGo = (url) => go(new URL(url, location.href).href, true).catch(() => { location.href = url; });
  window.crewNavPrefetch = (url) => load(new URL(url, location.href).href);
  history.replaceState({ crewhub: 1 }, "", location.href);
  addEventListener("popstate", (e) => {
    if (!e.state || !e.state.crewhub || !SWAP.test(location.pathname)) { location.reload(); return; }
    go(location.href, false).catch(() => location.reload());
  });
})();
