// Live-моніторинг: оновлює список кожні 10 с без перезавантаження сторінки.
(function () {
  const LABELS = { on_site: "На точці", on_break: "На перерві", wifi_lost: "Втрачено Wi-Fi", idle_wifi: "У Wi-Fi, без зміни", late: "Спізнюється", scheduled: "Скоро зміна", absent: "Відсутній", off: "Не на зміні" };
  let filter = null, data = null, fetchedAt = Date.now();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });
  const dur = (s) => Math.floor(s / 3600) + " год " + String(Math.floor(s / 60) % 60).padStart(2, "0") + " хв";
  const money = (v) => v.toLocaleString("uk-UA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function avatar(r) {
    return r.photo ? `<img class="avatar" src="${esc(r.photo)}" alt="">` : `<span class="avatar">${esc((r.name || "?")[0])}</span>`;
  }
  function details(r) {
    const p = [];
    if (r.location) p.push(esc(r.location));
    if (r.started_at) {
      let w = r.worked;
      if (r.status === "active" || r.break_paid) w += Math.floor((Date.now() - fetchedAt) / 1000);
      p.push("з " + hhmm(r.started_at) + " · " + dur(w));
    }
    if (r.last_seen) p.push("пінг " + hhmm(r.last_seen) + " · " + esc(r.ip));
    // З якого пристрою почали зміну; чужий акаунт (телефон іншої людини) — червоним окремим першим рядком, щоб не обрізався
    const dev = r.device ? " (" + esc(r.device) + ")" : "";
    let alarm = "";
    if (r.device_flag && r.device_flag.startsWith("other:")) alarm = '<span style="display:block;white-space:normal;color:var(--bad-text, #c62828)">⚠ Зміна в чужому акаунті — з телефона ' + esc(r.device_flag.slice(6)) + dev + "</span>";
    else if (r.device_flag === "new") alarm = '<span style="display:block;white-space:normal;color:var(--warn-text)">⚠ Зміна з нового пристрою' + dev + "</span>";
    else if (r.device) p.push("з пристрою: " + esc(r.device));
    if (r.power_lost) p.push('<span style="color:var(--warn-text)">телефон розрядився о ' + hhmm(r.power_lost) + ", зміна рахується</span>");
    else if (r.battery != null) p.push("заряд " + r.battery + "%" + (r.charging ? " ⚡" : ""));
    if (r.sched_start) p.push("за графіком " + hhmm(r.sched_start) + "–" + hhmm(r.sched_end));
    if (r.seen && !r.last_seen) p.push("пінг " + hhmm(r.seen) + " · " + esc(r.seen_ip));
    // Чому сервер не бачить телефон зміни, коли сторінку закрито (iPhone без застосунку): MAC невідомий чи спільний
    if (r.net_note) p.push('<span style="color:var(--warn-text)">' + esc(r.net_note) + "</span>");
    // Пішов з Wi-Fi: коли сервер закриє зміну і до котрої її оплачено (зміна на паузі ще не закрита — це видно)
    if (r.close_note) p.push('<span style="color:var(--warn-text)">' + esc(r.close_note) + "</span>");
    const line = alarm + p.join(" · ");  // alarm — блок: сам переносить рядок
    // Чому зміна не почалась сама — окремим рядком, щоб адмін одразу бачив, що виправити
    return r.why ? (line ? line + "<br>" : "") + '<span style="color:var(--warn-text)">Автозміна не почалась: ' + esc(r.why) + "</span>" : line;
  }
  function alarm() {
    // Хтось зараз на зміні в чужому акаунті (з телефона іншої людини) — велика червона плашка вгорі «Огляду»
    const box = document.getElementById("liveAlarm");
    if (!box) return;
    box.innerHTML = data.rows.filter((r) => r.started_at && r.device_flag && r.device_flag.startsWith("other:")).map((r) => `
      <a class="banner bad" href="/admin/employee/${r.id}">⚠<div><b>Чужий акаунт на зміні: ${esc(r.name)}</b> · зміну розпочато з телефона
        ${esc(r.device_flag.slice(6))}${r.device ? " (" + esc(r.device) + ")" : ""}, з ${hhmm(r.started_at)}. Відкрийте картку, щоб розібратись</div></a>`).join("");
  }
  function render() {
    if (!data) return;
    alarm();
    const rows = data.rows.filter((r) => !filter || r.state === filter);
    document.getElementById("liveList").innerHTML = rows.map((r) => `
      <div class="row" style="padding:10px 0;border-bottom:1px solid var(--line)">
        <a class="person" href="/admin/employee/${r.id}">${avatar(r)}<div><b>${esc(r.name)}</b><small>${details(r) || esc(r.job || "")}</small></div></a>
        <span class="spacer"></span><span class="pill s-${r.state}">${LABELS[r.state]}</span>
      </div>`).join("") || '<div class="empty">Нікого в цьому стані</div>';
    for (const [k, v] of Object.entries(data.counts)) document.getElementById("c-" + k).textContent = v;
    document.getElementById("k-active").textContent = data.counts.on_site + data.counts.on_break + data.counts.wifi_lost;
    document.getElementById("k-hours").textContent = data.stats.today_hours;
    document.getElementById("k-month").textContent = money(data.stats.month_total) + " " + (window.CURRENCY || "грн");
    document.getElementById("k-pending").textContent = data.stats.pending;
    document.getElementById("k-denied").textContent = data.stats.denied_today;
  }
  let alive = true;
  async function load() {
    if (!alive) return;
    try {
      const r = await fetch("/api/live");
      if (r.ok && alive) { data = await r.json(); fetchedAt = Date.now(); render(); }
    } catch (e) {}
  }
  document.querySelectorAll(".chip").forEach((b) => (b.onclick = () => {
    filter = filter === b.dataset.state ? null : b.dataset.state;
    document.querySelectorAll(".chip").forEach((x) => (x.style.borderColor = x.dataset.state === filter ? "var(--brand)" : ""));
    render();
  }));
  load();
  const t1 = setInterval(load, 10000), t2 = setInterval(render, 30000);
  // Перехід на іншу вкладку без перезавантаження (nav.js): зупиняємо оновлення цієї сторінки
  addEventListener("crewhub:leave", () => { alive = false; clearInterval(t1); clearInterval(t2); }, { once: true });
})();
