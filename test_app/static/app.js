// Спільна поведінка: тема, тости, сповіщення, діалоги.
(function () {
  const root = document.documentElement;
  // Шапка сторінки (тема, дзвіночок). Після переходу без перезавантаження (nav.js) шапка нова — прив'язуємо знову.
  function bindChrome() {
    const themeBtn = document.getElementById("themeBtn");
    const paintThemeIcon = () => {
      const i = themeBtn && themeBtn.querySelector("[data-fb]");
      if (!i) return;
      i.dataset.fb = root.dataset.theme === "dark" ? "sun" : "moon";
      const use = i.querySelector("use");
      if (use) use.setAttribute("href", "#i-" + i.dataset.fb);
    };
    paintThemeIcon();
    if (themeBtn) themeBtn.onclick = () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("crewhub_theme", root.dataset.theme); } catch (e) {}
      paintThemeIcon();
    };
    const bell = document.getElementById("bellBtn"), panel = document.getElementById("notifPanel");
    if (bell && panel) bell.onclick = (e) => { e.stopPropagation(); panel.classList.toggle("open"); };

    // Вкладка «Налаштування додатку»: вибір теми (Як у телефоні / Світла / Темна)
    const themePick = document.querySelector(".theme-pick");
    if (themePick) {
      let saved = null;
      try { saved = localStorage.getItem("crewhub_theme"); } catch (e) {}
      const current = saved || "auto";
      themePick.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("on", b.dataset.themeMode === current);
        b.onclick = () => {
          const m = b.dataset.themeMode;
          try {
            if (m === "auto") localStorage.removeItem("crewhub_theme");
            else localStorage.setItem("crewhub_theme", m);
          } catch (e) {}
          root.dataset.theme = m === "auto"
            ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : m;
          themePick.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
          paintThemeIcon();
        };
      });
    }

    // Скинути клієнтські налаштування (тема, анімації) — акаунт і зміна не чіпаються
    const reset = document.getElementById("resetApp");
    if (reset) reset.onclick = () => {
      if (!confirm("Повернути тему й анімації до стандартних?")) return;
      try {
        ["crewhub_theme", "crewhub_motion", "crewhub_lite"].forEach((k) => localStorage.removeItem(k));
      } catch (e) {}
      location.reload();
    };
  }
  window.bindChrome = bindChrome;
  bindChrome();
  document.addEventListener("click", (e) => {
    const panel = document.getElementById("notifPanel");
    if (panel && !panel.contains(e.target)) panel.classList.remove("open");
  });

  window.toast = function (text) {
    const box = document.getElementById("toasts");
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = text; box.appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, 4000);
  };
  document.querySelectorAll("#toasts .toast").forEach((t) => setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, 4500));

  // <button data-dialog="id"> відкриває <dialog id="id">; data-fill='{"name":"value"}' заповнює поля
  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-dialog]");
    if (opener) {
      const d = document.getElementById(opener.dataset.dialog);
      if (opener.dataset.fill) {
        const data = JSON.parse(opener.dataset.fill);
        for (const [k, v] of Object.entries(data)) {
          const el = d.querySelector(`[name="${k}"]`);
          if (el) el.value = v;
          d.querySelectorAll(`[data-text="${k}"]`).forEach((x) => (x.textContent = v));
        }
      }
      d.showModal();
    }
    if (e.target.closest("[data-close]")) e.target.closest("dialog").close();
  });

  // Перемикач режиму місячної сітки: «Зміни» або «Дей-офи»
  document.addEventListener("click", (e) => {
    const m = e.target.closest(".mode-switch a[data-mode]");
    if (!m) return;
    e.preventDefault();
    m.parentElement.querySelectorAll("a").forEach((a) => a.classList.toggle("on", a === m));
    const tbl = document.querySelector("table.month.editable");
    if (tbl) { tbl.classList.toggle("mode-shift", m.dataset.mode === "shift"); tbl.classList.toggle("mode-dayoff", m.dataset.mode === "dayoff"); }
  });

  // Місячна сітка графіка: натиснули клітинку — залежно від режиму ставимо зміну або дей-офф
  document.addEventListener("click", async (e) => {
    const c = e.target.closest("table.month.editable .mcell[data-day]");
    if (!c || c.classList.contains("busy")) return;
    const dayoffMode = c.closest("table.month").classList.contains("mode-dayoff");
    const ab = c.dataset.ab || "";
    if (dayoffMode) {
      if (ab && ab !== "day_off") { window.toast && window.toast("Цей день зайнято відпусткою або лікарняним"); return; }
    } else if (ab) { return; }  // у режимі «Зміни» клітинки з відсутністю не чіпаємо
    c.classList.add("busy");
    try {
      const url = dayoffMode ? "/schedule/dayoff" : "/schedule/toggle";
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: c.dataset.uid, day: c.dataset.day }) });
      const d = await r.json();
      if (!d.ok && d.error) { window.toast && window.toast(d.error); c.classList.remove("busy"); return; }
      const row = c.parentElement, cnt = row.querySelector("td.who small");
      if (dayoffMode) {
        const on = !d.off;  // поставили дей-офф
        c.classList.toggle("ab", on); c.classList.toggle("a-day_off", on);
        c.classList.remove("on", "pending"); c.dataset.ab = on ? "day_off" : "";
        c.textContent = on ? "Д" : "";
        c.title = c.title.split(" · ").slice(0, 2).join(" · ") + (on ? " · Дей-офф" : "");
      } else {
        c.classList.toggle("on", d.on);
        c.textContent = d.on ? d.label.split("–")[0].split(":")[0] : "";
        c.title = c.title.split(" · ").slice(0, 2).join(" · ") + (d.on ? " · " + d.label : "");
        if (cnt) cnt.textContent = row.querySelectorAll(".mcell.on").length + " дн.";
      }
    } catch (err) { window.toast && window.toast("Не вдалося зберегти, перевірте зв'язок"); }
    c.classList.remove("busy");
  });

  // CSRF-токен зі сторінки додається в кожну POST-форму перед відправкою (захист від підробки запитів)
  const CSRF = (document.querySelector('meta[name="crewhub-csrf"]') || {}).content || "";
  document.addEventListener("submit", (e) => {
    const f = e.target;
    if (CSRF && f && (f.getAttribute("method") || "").toLowerCase() === "post" && !f.querySelector('input[name="csrf"]')) {
      const h = document.createElement("input");
      h.type = "hidden"; h.name = "csrf"; h.value = CSRF;
      f.appendChild(h);
    }
    // data-confirm-btn на кнопці: питаємо лише для цієї кнопки (напр. «Відхилити» поруч із «Підтвердити»)
    const msg = (e.submitter && e.submitter.dataset.confirmBtn) || e.target.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });

  window.iconFallback = function () {};  // сумісність: іконки тепер завжди вбудовані

})();
