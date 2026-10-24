// CrewHub Motion: рух іконок при дотику, каскад появи, лічильники, діалоги і сторож плавності.
// Режим зберігається в localStorage "crewhub_motion": auto | full | lite | off; "crewhub_super" — супер-анімації іконок
// (обидва — у «Налаштуваннях додатку»).
(function () {
  const root = document.documentElement;
  const off = () => root.classList.contains("motion-off") || matchMedia("(prefers-reduced-motion: reduce)").matches;
  const lite = () => root.classList.contains("lite");
  let mode = "auto";
  try { mode = localStorage.getItem("crewhub_motion") || "auto"; } catch (e) {}

  // Яка іконка як рухається. Ключі — наші назви (data-fb) і назви Font Awesome (data-icon після заміни на SVG).
  const ANIM = {
    bell: "ring", phone: "ring", refresh: "spin", rotate: "spin", moon: "spin", sun: "spin", settings: "gear", gear: "gear",
    clock: "tick", x: "wiggle", xmark: "wiggle", alert: "wiggle", "triangle-exclamation": "wiggle", stop: "wiggle", edit: "wiggle", pen: "wiggle",
    plus: "pop", check: "pop", copy: "pop", camera: "pop", shield: "pop", "shield-halved": "pop", play: "pop", pause: "pop",
    send: "fly", "paper-plane": "fly", right: "right", "chevron-right": "right", logout: "right", "right-from-bracket": "right",
    left: "left", "chevron-left": "left", download: "drop", inbox: "drop", wallet: "shake", coffee: "shake", "mug-hot": "shake",
    utensils: "shake", menu: "squish", bars: "squish", calendar: "flip", "calendar-days": "flip", file: "flip", "file-pen": "flip",
    wifi: "wave", "plug-circle-xmark": "wiggle",
  };
  function iconName(el) {
    return el.dataset.fb || el.getAttribute("data-icon") || "";
  }

  // Супер-анімації: іконка розгортається з <use> у власні частини (p-* у спрайті), і кожна рухається по-своєму.
  // localStorage "crewhub_super": "1" / "0"; без вибору — увімкнено, крім слабких телефонів (режим «Легкі»).
  let superPref = null;
  try { superPref = localStorage.getItem("crewhub_super"); } catch (e) {}
  const superOn = () => !off() && (superPref === "1" || (superPref === null && !lite()));
  function inflate(el, name) {
    if (el.dataset.inflated === name) return true;
    const sym = document.getElementById("i-" + name);
    if (!sym) return false;
    el.setAttribute("viewBox", sym.getAttribute("viewBox") || "0 0 24 24");
    el.innerHTML = sym.innerHTML;
    el.dataset.inflated = name;
    return true;
  }
  function playSuper(el) {
    const name = iconName(el);
    if (!inflate(el, name)) return false;
    el.classList.add("sup");
    el.dataset.anim = "s-" + name;
    restart(el, "anim");
    clearTimeout(el._supT);
    el._supT = setTimeout(() => el.classList.remove("anim"), 1600);  // найдовша супер-анімація ~1,2 с
    return true;
  }
  // Перезапуск анімації без примусового перерахунку сторінки (offsetWidth/getBoundingClientRect гальмують слабкі телефони):
  // якщо клас уже стоїть — знімаємо зараз і ставимо через два кадри.
  function restart(el, cls) {
    if (!el.classList.contains(cls)) { el.classList.add(cls); return; }
    el.classList.remove(cls);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add(cls)));
  }
  function play(el) {
    if (off()) return;
    if (superOn() && playSuper(el)) return;
    el.classList.remove("sup");
    el.dataset.anim = ANIM[iconName(el)] || "jump";
    restart(el, "anim");
  }
  // Іконку змінили (тема, Wi-Fi): повертаємо звичайний <use> з новою назвою
  window.crewSetIcon = function (el, name) {
    el.dataset.fb = name;
    delete el.dataset.inflated;
    el.classList.remove("sup", "anim");
    el.removeAttribute("viewBox");
    el.innerHTML = '<use href="#i-' + name + '"/>';
  };
  document.addEventListener("animationend", (e) => {
    const t = e.target;
    if (t.classList && t.classList.contains("anim")) t.classList.remove("anim");
    if (t.classList && t.classList.contains("jelly")) t.classList.remove("jelly");
  });

  // pointerdown, а не click: рух стартує в момент дотику, ще до переходу на іншу сторінку
  // Натиснута кнопка «вдавлюється» одразу, не чекаючи, поки WebView вирішить, що це не прокрутка
  let pressed = null;
  const release = () => { if (pressed) { const p = pressed; pressed = null; setTimeout(() => p.classList.remove("pressed"), 90); } };
  ["pointerup", "pointercancel", "dragstart"].forEach((t) => document.addEventListener(t, release, { passive: true }));
  document.addEventListener("scroll", release, { passive: true, capture: true });
  document.addEventListener("pointerdown", (e) => {
    const host = e.target.closest("button, .btn, a, label, summary, [data-dialog]");
    if (!host) return;
    release();
    if (!host.disabled) { pressed = host; host.classList.add("pressed"); }
    const ic = host.querySelector(".icon");
    if (ic) play(ic);
  }, { passive: true });
  document.addEventListener("click", (e) => {
    const big = e.target.closest("button.xl");
    if (big && !off()) restart(big, "jelly");
  });
  // ПК: іконка оживає, коли на кнопку чи пункт меню наводять мишку (один раз на кожне наведення)
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType !== "mouse" || !superOn()) return;
    const host = e.target.closest(".nav a, button, .btn, .kpi, a.card");
    if (!host || (e.relatedTarget && host.contains(e.relatedTarget))) return;
    const ic = host.querySelector(".icon");
    if (ic && !ic.classList.contains("anim")) play(ic);
  }, { passive: true });

  // Нова сторінка в режимі «Супер»: оживають активна вкладка, іконки показників і заголовків — по черзі
  function greet() {
    if (!superOn()) return;
    const list = document.querySelectorAll(".bottombar a.on .icon, .nav a.on .icon, .content .kpi .ic .icon, .content .card-head h3 > .icon");
    let i = 0;
    list.forEach((ic) => {
      if (i >= 8) return;  // лише перший екран; далі — без зайвої роботи
      setTimeout(() => play(ic), 120 + i++ * 90);
    });
  }

  // Перемикач у «Налаштуваннях додатку»: <input id="superAnim">, приклади в .super-demo
  function bindSuper() {
    const sw = document.getElementById("superAnim");
    if (!sw) return;
    sw.checked = superOn();
    sw.disabled = off();
    sw.onchange = () => {
      superPref = sw.checked ? "1" : "0";
      try { localStorage.setItem("crewhub_super", superPref); } catch (e) {}
      if (sw.checked) document.querySelectorAll(".super-demo .icon").forEach((ic, i) => setTimeout(() => play(ic), i * 110));
    };
  }

  // Каскад: перші картки сторінки виринають по черзі (далі за екраном — без анімації, щоб не витрачати кадри)
  function cascade() {
    if (off() || lite()) return;
    const items = document.querySelectorAll(".content > .card, .content > .grid > *, .content > .grid-2 > *, .content > .banner, .content > form > .card, .content > .grid-2 > * > .card");
    let i = 0;
    items.forEach((el) => {
      if (i >= 6) return;  // лише те, що на першому екрані; вимірювань не робимо — вони гальмують
      el.style.setProperty("--i", i++);
      el.classList.add("rise");
      el.addEventListener("animationend", () => el.classList.remove("rise"), { once: true });
    });
  }

  // Лічильники в KPI «набігають» від нуля до значення. Це перерахунок сторінки на кожному кадрі,
  // тому лише в режимі «Повні» (у «Авто» сторінка має з'являтися якнайлегше).
  function countUp() {
    if (off() || lite() || mode !== "full") return;
    document.querySelectorAll(".kpi b").forEach((b) => {
      const text = b.textContent;
      if (text.includes(":")) return;
      const m = text.match(/^(\D*?)(\d[\d\s ]*)([.,]\d+)?(.*)$/);
      if (!m) return;
      const intPart = m[2].replace(/[\s ]/g, "");
      const frac = m[3] ? m[3].slice(1) : "";
      const target = parseFloat(intPart + (frac ? "." + frac : ""));
      if (!isFinite(target) || target === 0) return;
      const sep = /[\s ]/.test(m[2]) ? m[2].match(/[\s ]/)[0] : "";
      const fmt = (v) => {
        let s = frac ? v.toFixed(frac.length) : String(Math.round(v));
        let [a, d] = s.split(".");
        if (sep) a = a.replace(/\B(?=(\d{3})+(?!\d))/g, sep);
        return m[1] + a + (d ? (m[3][0] + d) : "") + m[4];
      };
      const t0 = performance.now(), dur = 800;
      (function step(now) {
        const p = Math.min(1, (now - t0) / dur), k = 1 - Math.pow(2, -10 * p);
        b.textContent = p < 1 ? fmt(target * k) : text;
        if (p < 1) requestAnimationFrame(step);
      })(t0);
    });
  }

  // Діалоги закриваються з анімацією
  const nativeClose = HTMLDialogElement && HTMLDialogElement.prototype.close;
  if (nativeClose) {
    HTMLDialogElement.prototype.close = function (v) {
      if (off() || !this.open || this.classList.contains("closing")) return nativeClose.call(this, v);
      this.classList.add("closing");
      setTimeout(() => { this.classList.remove("closing"); nativeClose.call(this, v); }, 170);
    };
  }

  // При переході поточна сторінка пригасає, щоб дотик відчувався одразу
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || off() || e.defaultPrevented || a.target || a.hasAttribute("download") || e.metaKey || e.ctrlKey) return;
    const href = a.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("javascript") || a.origin !== location.origin) return;
    document.body.classList.add("leaving");
  });
  addEventListener("pageshow", () => document.body.classList.remove("leaving"));

  // Сторож плавності: міряє кадри, поки грає поява сторінки. Менше ~45 кадрів/с або багато ривків —
  // одразу легкий режим (запам'ятовується), щоб далі анімації встигали відігравати повністю.
  function goLite() {
    root.classList.add("lite");
    try { localStorage.setItem("crewhub_lite", "1"); } catch (e) {}
  }
  function watchdog() {
    if (mode !== "auto" || lite() || off() || document.hidden) return;
    let frames = 0, slow = 0, last = 0, start = 0;
    function f(now) {
      if (!start) { start = last = now; return requestAnimationFrame(f); }
      const dt = now - last; last = now; frames++;
      if (dt > 30) slow++;
      if (dt > 250) return goLite();  // заморозка на чверть секунди — далі можна не міряти
      if (now - start < 1200) return requestAnimationFrame(f);
      if (frames < 54 || slow / frames > 0.2) goLite();
    }
    requestAnimationFrame(f);
  }

  // Перемикач у профілі: <div class="motion-pick">
  const bindPick = () => document.querySelectorAll(".motion-pick").forEach((box) => {
    box.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", b.dataset.mode === mode);
      b.onclick = () => {
        mode = b.dataset.mode;
        try { localStorage.setItem("crewhub_motion", mode); localStorage.removeItem("crewhub_lite"); } catch (e) {}
        root.classList.toggle("lite", mode === "lite");
        root.classList.toggle("motion-off", mode === "off");
        box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
        if (mode === "full" || mode === "auto") { cascade(); }
        bindSuper();
      };
    });
  });

  // Нова сторінка (і після переходу без перезавантаження з nav.js)
  window.crewMotion = { enter() { cascade(); countUp(); bindPick(); bindSuper(); greet(); }, play };
  window.crewMotion.enter();
  watchdog();
})();
