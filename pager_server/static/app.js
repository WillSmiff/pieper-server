(() => {
  const form = document.getElementById("compose");
  const text = document.getElementById("text");
  const counter = document.getElementById("counter");
  const sendBtn = document.getElementById("send");
  const feedback = document.getElementById("feedback");
  const hint = document.getElementById("hint");
  const devicesEl = document.getElementById("devices");
  const summaryEl = document.getElementById("online-summary");
  const messagesEl = document.getElementById("messages");

  const MAX_LEN = text.maxLength; // from the template's maxlength attribute
  let deviceCount = 0;
  let mode = "text";

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

  // ---------------------------------------------------------------- drawing pad
  const pad = (() => {
    const canvas = document.getElementById("pad");
    const W = canvas.width, H = canvas.height;
    const penBtn = document.getElementById("tool-pen");
    const eraserBtn = document.getElementById("tool-eraser");
    const sizeSel = document.getElementById("brush-size");
    const undoBtn = document.getElementById("undo");
    const clearBtn = document.getElementById("clear-pad");
    const caption = document.getElementById("caption");
    const MAX_UNDO = 30;

    let pixels = new Uint8Array(W * H);
    const undo = [];
    let ink = 1;          // 1 = pen, 0 = eraser
    let last = null;      // last pointer position while drawing
    let pointerId = null;
    let dirty = false;    // repaint scheduled

    function repaint() {
      if (dirty) return;
      dirty = true;
      requestAnimationFrame(() => { dirty = false; window.PagerDrawing.paint(canvas, pixels, W, H); });
    }

    function stamp(cx, cy) {
      // Filled disc of diameter ~size around (cx, cy).
      const r = +sizeSel.value / 2, r2 = r * r, n = Math.floor(r);
      for (let dy = -n; dy <= n; dy++) {
        const y = cy + dy;
        if (y < 0 || y >= H) continue;
        for (let dx = -n; dx <= n; dx++) {
          const x = cx + dx;
          if (x >= 0 && x < W && (dx * dx + dy * dy <= r2 || n === 0)) pixels[y * W + x] = ink;
        }
      }
    }

    function line(x0, y0, x1, y1) {  // Bresenham
      const dx = Math.abs(x1 - x0), dy = -Math.abs(y1 - y0);
      const sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
      let err = dx + dy;
      for (;;) {
        stamp(x0, y0);
        if (x0 === x1 && y0 === y1) break;
        const e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
      }
    }

    function toPixel(ev) {
      const rect = canvas.getBoundingClientRect();
      return [
        Math.floor((ev.clientX - rect.left) / rect.width * W),
        Math.floor((ev.clientY - rect.top) / rect.height * H),
      ];
    }

    function pushUndo() {
      undo.push(pixels.slice());
      if (undo.length > MAX_UNDO) undo.shift();
      undoBtn.disabled = false;
    }

    canvas.addEventListener("pointerdown", (ev) => {
      if (pointerId !== null || ev.button > 0) return;
      ev.preventDefault();
      pointerId = ev.pointerId;
      canvas.setPointerCapture(ev.pointerId);
      pushUndo();
      last = toPixel(ev);
      stamp(last[0], last[1]);
      repaint();
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (ev.pointerId !== pointerId) return;
      const events = ev.getCoalescedEvents ? ev.getCoalescedEvents() : [ev];
      for (const e of events.length ? events : [ev]) {
        const p = toPixel(e);
        line(last[0], last[1], p[0], p[1]);
        last = p;
      }
      repaint();
    });
    const end = (ev) => {
      if (ev.pointerId !== pointerId) return;
      pointerId = null;
      last = null;
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);

    function setTool(pen) {
      ink = pen ? 1 : 0;
      penBtn.setAttribute("aria-pressed", String(pen));
      eraserBtn.setAttribute("aria-pressed", String(!pen));
    }
    penBtn.addEventListener("click", () => setTool(true));
    eraserBtn.addEventListener("click", () => setTool(false));
    undoBtn.addEventListener("click", () => {
      if (!undo.length) return;
      pixels = undo.pop();
      undoBtn.disabled = !undo.length;
      repaint();
    });
    clearBtn.addEventListener("click", () => {
      if (isEmpty()) return;
      pushUndo();
      pixels.fill(0);
      repaint();
    });

    function isEmpty() { return !pixels.some((p) => p); }

    function reset() {
      pixels = new Uint8Array(W * H);
      undo.length = 0;
      undoBtn.disabled = true;
      caption.value = "";
      repaint();
    }

    repaint();
    return {
      isEmpty, reset,
      encode: () => window.PagerDrawing.encode(pixels, W, H),
      caption: () => caption.value.trim(),
      focus: () => caption.focus(),
    };
  })();

  // ---------------------------------------------------------------- tabs
  const tabs = {
    text: [document.getElementById("tab-text"), document.getElementById("panel-text")],
    drawing: [document.getElementById("tab-drawing"), document.getElementById("panel-drawing")],
  };
  function setMode(m) {
    mode = m;
    for (const [name, [tab, panel]] of Object.entries(tabs)) {
      tab.setAttribute("aria-selected", String(name === m));
      panel.classList.toggle("hidden", name !== m);
    }
    hint.textContent = m === "drawing" ? "Pagers show the drawing full-screen." : "";
    feedback.textContent = "";
    if (m === "text") text.focus();
  }
  tabs.text[0].addEventListener("click", () => setMode("text"));
  tabs.drawing[0].addEventListener("click", () => setMode("drawing"));

  // ---------------------------------------------------------------- status
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
      if (m.has_image) li.append(window.PagerDrawing.thumbnail(m.id));
      if (m.body) li.append(el("div", "body", m.body));
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

  // ---------------------------------------------------------------- send
  async function send(ev) {
    ev.preventDefault();
    let payload;
    if (mode === "drawing") {
      if (pad.isEmpty()) {
        feedback.className = "warn";
        feedback.textContent = "Draw something first.";
        return;
      }
      payload = { text: pad.caption(), drawing: pad.encode() };
    } else {
      const value = text.value.trim();
      if (!value) { text.focus(); return; }
      payload = { text: value };
    }
    sendBtn.disabled = true;
    feedback.className = "";
    feedback.textContent = "Sending…";
    try {
      const res = await fetch("/api/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.status === 401) { location.href = "/login"; return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      feedback.className = data.sent_to ? "ok" : "warn";
      feedback.textContent = data.sent_to
        ? `Sent to ${data.sent_to} pager${data.sent_to === 1 ? "" : "s"}.`
        : "Saved, but no pagers are online. They will get it when they reconnect and sync.";
      if (mode === "drawing") pad.reset();
      else { text.value = ""; updateCounter(); }
      refresh();
    } catch (err) {
      feedback.className = "error";
      feedback.textContent = `Failed: ${err.message}`;
    } finally {
      sendBtn.disabled = false;
      if (mode === "text") text.focus();
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
