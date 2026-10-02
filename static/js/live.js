/* Live punches: polls /dashboard/live.json and updates feed, counters, device status and toasts. */
(function () {
  const root = document.querySelector("[data-live-feed]");
  if (!root) return;
  const url = root.dataset.liveFeed;
  const max = Number(root.dataset.max || 15);
  const showToasts = root.dataset.toasts === "1";
  const list = root.querySelector(".punch-list");
  const empty = root.querySelector(".punch-empty");
  let cursor = 0, first = true, failures = 0;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hue = (s) => { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
  const mins = (m) => `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;

  function item(p, fresh) {
    const div = document.createElement("div");
    div.className = "punch-item" + (fresh ? " fresh" : "");
    div.dataset.ts = p.ts;
    div.dataset.id = p.id;
    const kind = p.kind ? `<span class="punch-kind ${p.kind}">${p.kind === "in" ? "IN" : "OUT"}</span>` : "";
    const late = p.late ? `<span class="punch-late" title="Late by ${mins(p.late_minutes)}">Late +${mins(p.late_minutes)}</span>` : "";
    const name = p.url ? `<a href="${esc(p.url)}">${esc(p.name)}</a>` : `<span class="text-secondary">${esc(p.name)}</span>`;
    div.innerHTML = `
      <div class="punch-avatar" style="background:hsl(${hue(p.name)} 45% 88%);color:hsl(${hue(p.name)} 50% 28%)">${esc(p.initials)}</div>
      <div class="punch-who">${name}<div class="punch-meta">#${esc(p.user_id)}${p.department ? " · " + esc(p.department) : ""}</div></div>
      <div class="punch-tags">${kind}${late}</div>
      <div class="punch-when"><div class="punch-time">${esc(p.time)}</div><div class="punch-meta">${esc(p.device)} · ${esc(p.verify)}</div></div>`;
    return div;
  }

  function insertSorted(node) {
    for (const child of list.children) {
      if (child.dataset.ts < node.dataset.ts) { list.insertBefore(node, child); return; }
    }
    list.appendChild(node);
  }

  function toast(p) {
    let box = document.getElementById("liveToasts");
    if (!box) { box = document.createElement("div"); box.id = "liveToasts"; document.body.appendChild(box); }
    const t = document.createElement("div");
    t.className = "live-toast " + (p.kind || "");
    t.innerHTML = `<i class="bi bi-fingerprint"></i><div><strong>${esc(p.name)}</strong>
      <div class="small">${p.kind ? (p.kind === "in" ? "Checked in" : "Checked out") : "Punched"} at ${esc(p.time)} · ${esc(p.device)}${p.late ? ` · <span class="text-warning-emphasis">late ${mins(p.late_minutes)}</span>` : ""}</div></div>`;
    box.prepend(t);
    while (box.children.length > 4) box.lastChild.remove();
    setTimeout(() => { t.classList.add("hide"); setTimeout(() => t.remove(), 400); }, 6000);
  }

  function setCounts(counts) {
    document.querySelectorAll("[data-count]").forEach((el) => {
      const v = String(counts[el.dataset.count] || 0);
      if (el.textContent.trim() !== v) {
        el.textContent = v;
        el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
      }
    });
  }

  function setDevices(devices) {
    const labels = { connected: "Live", connecting: "Connecting", error: "Disconnected", waiting: "Starting", off: "Live off" };
    document.querySelectorAll("[data-live-devices]").forEach((box) => {
      box.innerHTML = devices.map((d) => `<span class="live-chip ${d.status}" title="${d.last_event ? "Last live punch " + esc(d.last_event) : ""}">
        <span class="live-dot"></span>${esc(d.name)} · ${labels[d.status] || d.status}</span>`).join("");
    });
    const anyLive = devices.some((d) => d.status === "connected");
    document.querySelectorAll("[data-live-indicator]").forEach((el) => {
      el.classList.toggle("on", anyLive);
      el.querySelector(".label").textContent = anyLive ? "LIVE" : "Auto-refresh";
    });
  }

  async function poll() {
    try {
      const q = first ? `?limit=${max}` : `?after=${cursor}`;
      const r = await fetch(url + q, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      failures = 0;
      cursor = data.cursor;
      const recentLimit = Date.now() - 10 * 60 * 1000;
      let toasted = 0;
      for (const p of data.punches) {
        if (list.querySelector(`[data-id="${p.id}"]`)) continue;
        insertSorted(item(p, !first));
        if (!first && showToasts && toasted < 4 && new Date(p.ts).getTime() > recentLimit) { toast(p); toasted++; }
      }
      while (list.children.length > max) list.lastChild.remove();
      if (empty) empty.classList.toggle("d-none", list.children.length > 0);
      setCounts(data.counts);
      setDevices(data.devices);
      document.querySelectorAll("[data-server-time]").forEach((el) => el.textContent = data.server_time);
      first = false;
    } catch (e) {
      failures++;
    }
    const delay = document.hidden ? 15000 : Math.min(3000 * (failures + 1), 30000);
    setTimeout(poll, delay);
  }
  poll();
})();
