// Годинник зміни: кнопки, кругова шкала, живий лічильник і фоновий пінг (поки сторінка відкрита).
(function () {
  const $ = (id) => document.getElementById(id);
  const C = 2 * Math.PI * 110; // довжина кола шкали
  let state = null, fetchedAt = Date.now(), hbTimer = null, busy = false, lastClosed = null;
  const deviceId = (() => {
    try {
      let id = localStorage.getItem("crewhub_device");
      if (!id) { id = "web-" + Math.random().toString(16).slice(2, 10); localStorage.setItem("crewhub_device", id); }
      return id;  // той самий id, що надіслала реєстрація: за ним сервер упізнає телефон людини
    } catch (e) { return "web"; }
  })();
  const money = (v) => v.toLocaleString("uk-UA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " " + CREWHUB.currency;
  const pad = (n) => String(n).padStart(2, "0");
  const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });

  // Запит з таймаутом: зависле з'єднання не блокує кнопки назавжди
  function post(action, extra) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    return fetch("/api/clock/" + action, {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctl.signal,
      body: JSON.stringify(Object.assign({ device_id: deviceId }, extra || {})),
    }).finally(() => clearTimeout(t));
  }

  const quiet = (a) => a === "heartbeat" || a === "auto";  // фонові запити не блокують кнопки
  let alive = true;
  async function call(action, extra, attempt) {
    if (!alive) return;
    attempt = attempt || 0;
    if (busy && !quiet(action) && !attempt) return;
    if (!quiet(action)) { busy = true; setDisabled(true); }
    let retrying = false;
    try {
      const r = await post(action, extra);
      if (r.status >= 502) throw new Error("server " + r.status); // сервер перезапускається

      if (r.status === 401) { location.href = "/login"; return; }
      const data = await r.json();
      if (!data.ok) {
        $("error").textContent = data.error;
        if (!quiet(action) && navigator.vibrate) navigator.vibrate(150);
        busy = false;
        // Відмова (напр. зміну вже закрив сервер): беремо свіжий стан, щоб не показувати й не пінгувати стару зміну
        if (action !== "check") await call("check");
        return;
      }
      $("error").textContent = "";
      const before = state && state.shift && state.shift.status;
      state = data; fetchedAt = Date.now();
      if (data.closed_shift) {
        lastClosed = data.closed_shift;
        CREWHUB.todayClosed += data.closed_shift.earnings;
        CREWHUB.monthClosed += data.closed_shift.earnings;
        toast(data.off_network ? "Зміну закрито часом останнього підключення до точки" : "Зміну закрито. Заробіток: " + money(data.closed_shift.earnings));
      } else if (action === "start") toast("Зміну розпочато. Гарного дня!");
      else if (data.auto_started) toast("Ви в робочому Wi-Fi: зміну розпочато автоматично");
      else if (action === "heartbeat" && before === "paused" && data.shift && data.shift.status === "active") toast("Ви знову в робочій мережі, зміну продовжено");
      render();
      if (!quiet(action) && !data.already) { const inner = document.querySelector(".ring .inner"); if (inner) { inner.classList.remove("bump"); void inner.offsetWidth; inner.classList.add("bump"); } }
    } catch (e) {
      // Немає зв'язку або сервер перезапускається. Дії безпечно повторювати: сервер не відкриє другу зміну
      // і не закриє зміну двічі, тому пробуємо ще 4 рази з наростаючою паузою.
      if (!quiet(action) && attempt < 4) {
        retrying = true;
        $("error").textContent = "Немає зв'язку з сервером, пробую ще раз…";
        setTimeout(() => call(action, extra, attempt + 1), 1500 * Math.pow(2, attempt));
      } else {
        $("error").textContent = "Немає зв'язку з сервером. Зміна на сервері продовжується, сторінка підключиться сама";
        if (!quiet(action)) setTimeout(() => call("check"), 30000);  // і справді підключається: пробуємо раз на 30 с
      }
    } finally {
      if (!quiet(action) && !retrying) { busy = false; if (state) render(); }
    }
  }

  // «Обід» і «Пауза» адмін може вимкнути, тоді кнопок на сторінці немає
  function setDisabled(v) { ["start", "lunch", "rest", "resume", "stop"].forEach((id) => { if ($(id)) $(id).disabled = v; }); }

  function liveWorked() {
    const s = state && state.shift;
    if (!s) return 0;
    let secs = s.worked_seconds;
    const ticking = s.status === "active" || (s.open_break && s.open_break.is_paid);
    if (ticking) secs += Math.floor((Date.now() - fetchedAt) / 1000);
    return Math.max(secs, 0);
  }

  function render() {
    if (!alive) return;
    const s = state.shift;
    const lost = s && s.open_break && s.open_break.kind === "wifi_loss";
    const net = $("net");
    net.className = "banner " + (state.on_work_network ? "ok" : "bad");
    // Назву точки вводять люди: лише як текст, ніколи як HTML (інакше назва з <script> виконалась би в адміна)
    const span = net.querySelector("span"), b = document.createElement("b");
    b.textContent = state.on_work_network ? (state.location || "Робоча мережа") : "Не в робочій мережі";
    span.textContent = "";
    span.appendChild(b);
    if (state.on_work_network) span.appendChild(document.createTextNode(" · робоча мережа"));
    const ic = net.querySelector("[data-fb]");
    if (ic) {
      ic.dataset.fb = state.on_work_network ? "wifi" : "wifi-off";
      const use = ic.querySelector("use"); if (use) use.setAttribute("href", "#i-" + ic.dataset.fb);
    }

    let status = state.waiting_until ? "Зміна почнеться сама о " + hhmm(state.waiting_until)
      : state.other_device ? "Автозміна вмикається лише з вашого телефона" : "Зміна не розпочата", pulse = "off";
    const why = document.getElementById("autoWhy");
    if (why) { why.textContent = !s && state.why ? "Чому не почалась сама: " + state.why : ""; why.hidden = !why.textContent; }
    if (s) {
      status = "На зміні з " + hhmm(s.started_at); pulse = "";
      if (s.open_break) {
        const b = s.open_break;
        status = { lunch: "Обідня перерва", rest: "Пауза", wifi_loss: "Пауза: втрачено робочий Wi-Fi" }[b.kind] + " з " + hhmm(b.started_at);
        if (b.is_paid) status += b.rate_pct == 100 ? " · оплачується" : " · оплата " + b.rate_pct + "%";
        pulse = lost ? "bad" : "warn";
      }
    }
    $("status").textContent = status;
    $("pulse").className = "pulse " + pulse;
    const paidBreak = s && s.open_break && s.open_break.is_paid;
    $("ring").className = "ring" + (lost ? " lost" : s && s.status === "paused" && !paidBreak ? " paused" : "");

    const show = (id, v) => ($(id).style.display = v ? "" : "none");
    show("start", !s); if ($("breakRow")) show("breakRow", s && s.status === "active"); show("resume", s && s.status === "paused"); show("stop", !!s);
    setDisabled(false);
    // Кнопку не блокуємо: сервер сам відмовить поза робочою мережею і запише спробу в журнал,
    // а адмін побачить цю мережу в «Точках і Wi-Fi» і зможе її прив'язати.
    $("start").classList.toggle("muted-btn", !state.on_work_network);
    $("resume").disabled = lost;
    $("resume").querySelector("span").textContent = lost ? "Поверніться в робочий Wi-Fi" : "Завершити перерву";

    tick();
    scheduleHeartbeat();
  }

  // Пінг з випадковим зсувом ±15%: 200 телефонів не стукають на сервер в ту саму секунду
  function scheduleHeartbeat() {
    clearTimeout(hbTimer);
    if (!state) return;
    if (!state.shift) {  // автостарт: поки сторінка відкрита, раз на хвилину питаємо, чи не час починати
      if (state.auto_start && state.on_work_network) {
        // після помилки мережі теж плануємо наступну спробу (раніше автостарт тоді замовкав до перезавантаження)
        hbTimer = setTimeout(async () => { await call("auto"); scheduleHeartbeat(); }, 60000 * (0.9 + Math.random() * 0.2));
      }
      return;
    }
    const ms = (state.heartbeat_seconds || 20) * 1000 * (0.85 + Math.random() * 0.3);
    hbTimer = setTimeout(async () => {
      const hadError = $("error").textContent.startsWith("Немає зв'язку");
      await call(hadError ? "check" : "heartbeat");
      if (hadError) call("heartbeat");
      scheduleHeartbeat();
    }, ms);
  }

  function tick() {
    if (!state || !alive) return;
    const w = liveWorked();
    $("timer").textContent = Math.floor(w / 3600) + ":" + pad(Math.floor(w / 60) % 60) + ":" + pad(w % 60);
    const pct = Math.min(w / (CREWHUB.target || 28800), 1);
    $("ringBar").style.strokeDashoffset = C * (1 - pct);
    $("ringSub").textContent = state.shift ? Math.round(pct * 100) + "% з " + (CREWHUB.target / 3600).toFixed(1).replace(".0", "") + " год" : "готові до зміни";
    // Сервер рахує заробіток з урахуванням ×2 за вихідні, ×3 за ніч і % за перерви; між запитами додаємо поточну ставку
    const s = state.shift;
    const earned = s ? s.earnings + ((Date.now() - fetchedAt) / 1000) * (s.rate_now || 0) / 3600 : 0;
    $("e-shift").textContent = money(earned);
    $("e-today").textContent = money(CREWHUB.todayClosed + earned);
    $("e-month").textContent = money(CREWHUB.monthClosed + earned);
  }

  $("start").onclick = () => call("start");
  if ($("lunch")) $("lunch").onclick = () => call("break_start", { kind: "lunch" });
  if ($("rest")) $("rest").onclick = () => call("break_start", { kind: "rest" });
  $("resume").onclick = () => call("break_end");
  // Попап при закритті зміни: показує відпрацьований час, кнопки «Підтвердити» і «Редагувати»
  const workedText = (secs) => {
    const h = Math.floor(secs / 3600), m = Math.floor(secs / 60) % 60;
    return h ? h + " год " + pad(m) + " хв" : m + " хв " + pad(secs % 60) + " с";
  };
  function openCloseDialog() {
    const dlg = $("closeConfirm");
    if (!dlg || !state || !state.shift) { if (confirm("Закрити зміну?")) call("stop"); return; }
    const s = state.shift;
    $("ccWorked").textContent = workedText(liveWorked());
    const earned = s.earnings + ((Date.now() - fetchedAt) / 1000) * (s.rate_now || 0) / 3600;
    $("ccMeta").textContent = "З " + hhmm(s.started_at) + " до " + hhmm(Date.now() / 1000) + " · " + money(earned)
      + (s.unpaid_break_seconds ? " · перерви " + Math.round(s.unpaid_break_seconds / 60) + " хв" : "");
    if ($("ccOff")) $("ccOff").style.display = state.on_work_network ? "none" : "flex";
    dlg.showModal();
  }
  // datetime-local у локальному часі телефона
  const toInput = (ts) => { const d = new Date(ts * 1000); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
  async function closeThenEdit() {
    lastClosed = null;
    await call("stop");
    const c = lastClosed;
    const dlg = $("editReq");
    if (!c || !dlg) { if (!c) return; location.href = "/me?tab=timesheet"; return; }
    dlg.querySelector("[name=shift_id]").value = c.id;
    dlg.querySelector("[name=proposed_start]").value = toInput(c.started_at);
    if (c.ended_at) dlg.querySelector("[name=proposed_end]").value = toInput(c.ended_at);
    dlg.querySelectorAll("[data-text=when]").forEach((x) => (x.textContent = new Date(c.started_at * 1000).toLocaleDateString("uk-UA")));
    const cm = dlg.querySelector("textarea[name=comment]"); if (cm) cm.value = "";
    dlg.showModal();
  }
  $("stop").onclick = openCloseDialog;
  if ($("ccConfirm")) $("ccConfirm").onclick = () => { $("closeConfirm").close(); call("stop"); };
  if ($("ccEdit")) $("ccEdit").onclick = () => { $("closeConfirm").close(); closeThenEdit(); };
  $("refresh").onclick = () => call("check");
  const onVisible = () => { if (!document.hidden) call("check"); };
  document.addEventListener("visibilitychange", onVisible);
  const ticker = setInterval(tick, 1000);
  // Перехід на іншу вкладку без перезавантаження (nav.js): прибираємо таймери цієї сторінки
  addEventListener("crewhub:leave", () => {
    alive = false; clearInterval(ticker); clearTimeout(hbTimer);
    document.removeEventListener("visibilitychange", onVisible);
  }, { once: true });
  call("check");
})();
