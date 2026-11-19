(() => {
  const form = document.getElementById("compose");
  const text = document.getElementById("text");
  const counter = document.getElementById("counter");
  const sendBtn = document.getElementById("send");
  const feedback = document.getElementById("feedback");
  const devicesEl = document.getElementById("devices");
  const summaryEl = document.getElementById("online-summary");
  const messagesEl = document.getElementById("messages");

  const MAX_LEN = text.maxLength; // from the template's maxlength attribute
  let deviceCount = 0;

  function updateCounter() {
    counter.textContent = `${text.value.length} / ${MAX_LEN}`;
  }

  function el(tag, cls, content) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (content !== undefined) e.textContent = content;
    return e;
  }

  function fmtTime(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    const sameDay = d.toDateString() === new Date().toDateString();
    return sameDay ? d.toLocaleTimeString() : d.toLocaleString();
  }

  function render(data) {
    deviceCount = data.device_count;

    devicesEl.replaceChildren();
    const online = data.devices.filter((d) => d.online).length;
    summaryEl.textContent = `(${online} of ${data.devices.length} online)`;
    if (!data.devices.length) {
      devicesEl.append(el("li", "muted", "No devices registered. Add one with: python manage.py add-device <name>"));
    }
    for (const d of data.devices) {
      const li = el("li");
      li.append(el("span", d.online ? "dot on" : "dot off"));
      li.append(el("span", "name", d.name));
      li.append(el("span", "muted", d.online ? "online" : `last seen ${fmtTime(d.last_seen)}`));
      devicesEl.append(li);
    }

    messagesEl.replaceChildren();
    if (!data.messages.length) messagesEl.append(el("li", "muted", "No pages sent yet."));
    for (const m of data.messages) {
      const li = el("li");
      const meta = el("div", "meta");
      meta.append(el("strong", null, m.sender));
      meta.append(el("span", "muted", ` · ${fmtTime(m.created_at)} · `));
      meta.append(el("span", "muted", `received by ${m.acks}/${deviceCount}`));
      li.append(meta);
      li.append(el("div", "body", m.body));
      messagesEl.append(li);
    }
  }

  async function refresh() {
    try {
      const res = await fetch("/api/status", { headers: { Accept: "application/json" } });
      if (res.status === 401) { location.href = "/login"; return; }
      if (res.ok) render(await res.json());
    } catch (_) { /* server temporarily unreachable; try again next tick */ }
  }

  async function send(ev) {
    ev.preventDefault();
    const value = text.value.trim();
    if (!value) return;
    sendBtn.disabled = true;
    feedback.className = "";
    feedback.textContent = "Sending…";
    try {
      const res = await fetch("/api/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: value }),
      });
      if (res.status === 401) { location.href = "/login"; return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      feedback.className = data.sent_to ? "ok" : "warn";
      feedback.textContent = data.sent_to
        ? `Sent to ${data.sent_to} pager${data.sent_to === 1 ? "" : "s"}.`
        : "Saved, but no pagers are online. They will get it when they reconnect and sync.";
      text.value = "";
      updateCounter();
      refresh();
    } catch (err) {
      feedback.className = "error";
      feedback.textContent = `Failed: ${err.message}`;
    } finally {
      sendBtn.disabled = false;
      text.focus();
    }
  }

  text.addEventListener("input", updateCounter);
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) form.requestSubmit();
  });
  form.addEventListener("submit", send);

  updateCounter();
  refresh();
  setInterval(refresh, 3000);
})();
