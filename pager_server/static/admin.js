(() => {
  const $ = (id) => document.getElementById(id);
  const statusEl = $("status");
  let me = "";

  // ------------------------------------------------------------ helpers

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function fmtTime(iso) {
    return iso ? new Date(iso).toLocaleString() : "never";
  }

  function showStatus(text, kind = "ok") {
    statusEl.className = `banner ${kind}`;
    statusEl.textContent = text;
    clearTimeout(showStatus.timer);
    showStatus.timer = setTimeout(() => statusEl.classList.add("hidden"), 6000);
  }

  class Cancelled extends Error {}

  // Ask for password + 2FA code; resolves once the server accepted them.
  function reauth() {
    const dialog = $("reauth-dialog");
    const form = $("reauth-form");
    const errorEl = $("reauth-error");
    form.reset();
    errorEl.textContent = "";
    dialog.showModal();
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        form.onsubmit = null;
        $("reauth-cancel").onclick = null;
        dialog.oncancel = null;
      };
      const cancel = () => { cleanup(); dialog.close(); reject(new Cancelled()); };
      $("reauth-cancel").onclick = cancel;
      dialog.oncancel = (e) => { e.preventDefault(); cancel(); };
      form.onsubmit = async (e) => {
        e.preventDefault();
        const res = await fetch("/api/reauth", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: form.password.value, code: form.code.value }),
        });
        if (res.ok) { cleanup(); dialog.close(); resolve(); return; }
        const data = await res.json().catch(() => ({}));
        errorEl.textContent = data.error || `HTTP ${res.status}`;
        form.code.value = "";
      };
    });
  }

  async function api(method, url, body) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      if (res.status === 401) { location.href = "/login"; throw new Cancelled(); }
      const data = await res.json().catch(() => ({}));
      if (res.status === 403 && data.error === "reauth_required" && attempt === 0) {
        await reauth();
        continue;
      }
      if (res.status === 403 && data.error === "account_setup_required") {
        location.href = "/account"; throw new Cancelled();
      }
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    }
    throw new Error("Confirmation failed");
  }

  function showSecret(title, text, value) {
    $("secret-title").textContent = title;
    $("secret-text").textContent = text;
    $("secret-value").textContent = value;
    $("secret-copy").textContent = "Copy";
    $("secret-dialog").showModal();
  }
  $("secret-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText($("secret-value").textContent);
      $("secret-copy").textContent = "Copied";
    } catch (_) {
      $("secret-copy").textContent = "Select and copy manually";
    }
  };
  $("secret-close").onclick = () => {
    $("secret-value").textContent = "";
    $("secret-dialog").close();
  };

  // Run an admin action: optional confirmation, then refresh everything.
  async function act(confirmText, fn) {
    if (confirmText && !confirm(confirmText)) return;
    try {
      const message = await fn();
      if (message) showStatus(message);
      await loadAll();
    } catch (err) {
      if (!(err instanceof Cancelled)) showStatus(err.message, "error");
    }
  }

  function button(label, cls, onClick) {
    const b = el("button", `small ${cls || ""}`, label);
    b.type = "button";
    b.onclick = onClick;
    return b;
  }

  function replaceRows(table, rows, emptyText, cols) {
    const tbody = $(table).querySelector("tbody");
    tbody.replaceChildren(...rows);
    if (!rows.length) {
      const td = el("td", "muted", emptyText);
      td.colSpan = cols;
      const tr = el("tr");
      tr.append(td);
      tbody.append(tr);
    }
  }

  const enc = encodeURIComponent;

  // -------------------------------------------------------------- users

  function renderUsers(users) {
    const rows = users.map((u) => {
      const self = u.username.toLowerCase() === me.toLowerCase();
      const tr = el("tr");
      const name = el("td", null, u.username);
      if (self) name.append(el("span", "muted", " (you)"));
      tr.append(name);
      tr.append(el("td", null, u.is_admin ? "Admin" : "User"));
      tr.append(el("td", null, u.has_2fa ? "On" : "Off"));
      tr.append(el("td", u.must_change_password ? "warn" : "muted",
        u.must_change_password ? "Temporary password" : "Active"));
      tr.append(el("td", null, u.sessions));
      tr.append(el("td", "muted", fmtTime(u.created_at)));
      const actions = el("td", "actions");
      const path = `/api/admin/users/${enc(u.username)}`;
      actions.append(button("Rename", "secondary", () => {
        const next = prompt(`New username for ${u.username} (letters, digits, . - _ only):`, u.username);
        if (next === null || next.trim() === u.username) return;
        act(null, async () => {
          const r = await api("POST", `${path}/rename`, { new_username: next.trim() });
          return `${u.username} is now called ${r.username}. They sign in with the new name.`;
        });
      }));
      if (self) {
        actions.append(el("a", null, "Account page"));
        actions.lastChild.href = "/account";
      } else {
        actions.append(button("Reset password", "secondary", () => act(
          `Reset the password of ${u.username}? They will be signed out and need the new temporary password.`,
          async () => {
            const r = await api("POST", `${path}/reset-password`);
            showSecret("Temporary password", `Give this to ${r.username}. They must change it at sign-in.`, r.temp_password);
          })));
        actions.append(button(u.is_admin ? "Remove admin" : "Make admin", "secondary", () => act(
          u.is_admin ? `Remove admin rights from ${u.username}?`
            : `Make ${u.username} an admin? They will need to set up two-factor authentication.`,
          async () => {
            await api("POST", `${path}/role`, { is_admin: !u.is_admin });
            return `${u.username} is now ${u.is_admin ? "a regular user" : "an admin"}.`;
          })));
        if (u.has_2fa) {
          actions.append(button("Reset 2FA", "secondary", () => act(
            `Remove two-factor authentication for ${u.username}? Use this if they lost their phone.`,
            async () => { await api("POST", `${path}/reset-2fa`); return `2FA reset for ${u.username}.`; })));
        }
        if (u.sessions > 0) {
          actions.append(button("Sign out", "secondary", () => act(
            `Sign ${u.username} out everywhere?`,
            async () => { const r = await api("POST", `${path}/logout`); return `Ended ${r.ended} session(s).`; })));
        }
        actions.append(button("Delete", "danger", () => act(
          `Delete user ${u.username}? This cannot be undone.`,
          async () => { await api("DELETE", path); return `Deleted ${u.username}.`; })));
      }
      tr.append(actions);
      return tr;
    });
    replaceRows("users", rows, "No users.", 7);
  }

  $("add-user").onsubmit = (e) => {
    e.preventDefault();
    const form = e.target;
    act(null, async () => {
      const r = await api("POST", "/api/admin/users", {
        username: form.username.value.trim(),
        is_admin: form.is_admin.checked,
      });
      form.reset();
      showSecret("Temporary password", `User ${r.username} was created. Give them this password; they must change it at first sign-in.`, r.temp_password);
    });
  };

  // ------------------------------------------------------------ devices

  function renderDevices(devices) {
    const rows = devices.map((d) => {
      const tr = el("tr");
      tr.append(el("td", null, d.name));
      const st = el("td");
      st.append(el("span", d.online ? "dot on" : "dot off"), document.createTextNode(d.online ? " Online" : " Offline"));
      tr.append(st);
      tr.append(el("td", "muted", d.online ? "now" : fmtTime(d.last_seen)));
      tr.append(el("td", "muted", fmtTime(d.created_at)));
      const actions = el("td", "actions");
      const path = `/api/admin/devices/${enc(d.name)}`;
      actions.append(button("New token", "secondary", () => act(
        `Issue a new token for ${d.name}? The old token stops working immediately.`,
        async () => {
          const r = await api("POST", `${path}/rotate`);
          showSecret("Device token", `New token for ${r.name}. Put it in the device's configuration.`, r.token);
        })));
      actions.append(button("Delete", "danger", () => act(
        `Delete device ${d.name}? It will be disconnected immediately.`,
        async () => { await api("DELETE", path); return `Deleted ${d.name}.`; })));
      tr.append(actions);
      return tr;
    });
    replaceRows("devices", rows, "No devices yet.", 5);
  }

  $("add-device").onsubmit = (e) => {
    e.preventDefault();
    const form = e.target;
    act(null, async () => {
      const r = await api("POST", "/api/admin/devices", { name: form.name.value.trim() });
      form.reset();
      showSecret("Device token", `Device ${r.name} was created. Put this token in the device's configuration.`, r.token);
    });
  };

  // --------------------------------------------------------- phone line

  function renderPhone(p) {
    const state = $("phone-state");
    if (!p.token_set) {
      state.textContent = "Not connected: run manage.py phone-token on the server";
      state.className = "warn small";
    } else if (!p.pin_set) {
      state.textContent = "Off: set a PIN to turn it on";
      state.className = "warn small";
    } else {
      state.textContent = "On";
      state.className = "ok small";
    }
    $("phone-off").classList.toggle("hidden", !p.pin_set);
    $("phone-pin").pin.pattern = `[0-9]{${p.pin_min_length},12}`;
    $("phone-pin").pin.placeholder = `${p.pin_min_length}-12 digits`;
    $("add-preset").text.maxLength = p.max_length;

    replaceRows("presets", p.presets.map((pr) => {
      const tr = el("tr");
      tr.append(el("td", null, pr.digit), el("td", "body", pr.body));
      const actions = el("td", "actions");
      actions.append(button("Edit", "secondary", () => {
        const form = $("add-preset");
        form.digit.value = String(pr.digit);
        form.text.value = pr.body;
        form.text.focus();
      }));
      actions.append(button("Delete", "danger", () => act(
        `Delete preset ${pr.digit}? Callers pressing ${pr.digit} will hear an error.`,
        async () => { await api("DELETE", `/api/admin/phone/presets/${pr.digit}`); return `Deleted preset ${pr.digit}.`; })));
      tr.append(actions);
      return tr;
    }), "No presets yet.", 3);
  }

  $("add-preset").onsubmit = (e) => {
    e.preventDefault();
    const form = e.target;
    act(null, async () => {
      const r = await api("PUT", `/api/admin/phone/presets/${form.digit.value}`, { text: form.text.value });
      form.text.value = "";
      return `Saved preset ${r.digit}.`;
    });
  };

  $("phone-pin").onsubmit = (e) => {
    e.preventDefault();
    const form = e.target;
    act(null, async () => {
      await api("PUT", "/api/admin/phone/pin", { pin: form.pin.value });
      form.reset();
      return "Phone PIN set. Share it only with people who may page by phone.";
    });
  };

  $("phone-off").onclick = () => act(
    "Turn off phone paging? The PIN is removed; set a new one to turn it back on.",
    async () => { await api("DELETE", "/api/admin/phone/pin"); return "Phone paging is off."; });

  // ------------------------------------------------------ audit/history

  function renderAudit(entries) {
    replaceRows("audit", entries.map((a) => {
      const tr = el("tr");
      [fmtTime(a.ts), a.actor, a.action, a.target || "", a.details || "", a.ip || ""]
        .forEach((v, i) => tr.append(el("td", i === 0 || i === 5 ? "muted" : null, v)));
      return tr;
    }), "Nothing logged yet.", 6);
  }

  function renderHistory(messages, deviceCount) {
    replaceRows("history", messages.map((m) => {
      const tr = el("tr");
      const body = el("td", "body", m.body);
      if (m.has_image) body.prepend(window.PagerDrawing.thumbnail(m.id));
      tr.append(el("td", "muted", m.id), el("td", "muted", fmtTime(m.created_at)),
        el("td", null, m.sender), body, el("td", null, `${m.acks}/${deviceCount}`));
      return tr;
    }), "No pages sent yet.", 5);
  }

  $("wipe-history").onclick = () => act(
    "Delete the entire page history? This cannot be undone. Pagers keep the pages they already received.",
    async () => {
      const r = await api("DELETE", "/api/admin/messages");
      return `Deleted ${r.deleted} page(s) from the history.`;
    });

  async function loadAll() {
    try {
      const [users, devices, audit, history, phone] = await Promise.all([
        api("GET", "/api/admin/users"),
        api("GET", "/api/admin/devices"),
        api("GET", "/api/admin/audit"),
        api("GET", "/api/admin/messages"),
        api("GET", "/api/admin/phone"),
      ]);
      me = users.me;
      renderUsers(users.users);
      renderDevices(devices.devices);
      renderPhone(phone);
      renderAudit(audit.entries);
      renderHistory(history.messages, devices.devices.length);
    } catch (err) {
      if (!(err instanceof Cancelled)) showStatus(`Could not load data: ${err.message}`, "error");
    }
  }

  async function refreshDevices() {
    if (document.querySelector("dialog[open]")) return;
    try { renderDevices((await api("GET", "/api/admin/devices")).devices); } catch (_) { /* next tick */ }
  }

  loadAll();
  setInterval(refreshDevices, 10000);
})();
