"use strict";
// Who can see this: the signed-in badge, the paired devices, and making a pairing code (interface/access.py).
const PolyAccess = (() => {
  const $ = (s) => document.querySelector(s);
  const post = (path, body) => fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                                            cache: "no-store", body: JSON.stringify(body || {})});
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 120 ? "just now" : s < 7200 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`; };
  let state = null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function badge() {
    const b = $("#access");
    if (!b || !state) return;
    b.replaceChildren();
    if (state.remote && state.device) {
      b.append(el("span", "access-lock", "🔒"), el("span", "", ` ${state.device.name} · `));
      const out = el("button", "linkish", "sign out");
      out.addEventListener("click", async () => {
        if (!confirm("Sign this browser out? You will need a new pairing code to come back.")) return;
        await post("/api/access/logout");
        location.replace("/pair");
      });
      b.append(out);
    } else if (!state.remote) {
      b.append(el("span", "muted", "on the Pi"));
    }
  }

  async function devices() {
    const box = $("#devices");
    if (!box) return;
    try {
      const r = await fetch("/api/access/devices", {cache: "no-store"});
      const d = await r.json();
      box.replaceChildren();
      if (!d.devices || !d.devices.length) { box.append(el("p", "muted", "No browser is paired yet: only the Pi itself can see Polymath.")); return; }
      const ul = el("ul", "devices");
      for (const dev of d.devices) {
        const li = el("li");
        li.append(el("strong", "", dev.name + (dev.this ? " (this browser)" : "")),
                  el("span", "muted", ` · paired ${ago(dev.created)} · last seen ${ago(dev.last_seen)} from ${dev.last_ip || "?"}`));
        const x = el("button", "linkish danger", "revoke");
        x.addEventListener("click", async () => {
          if (!confirm(`Revoke “${dev.name}”? It will have to be paired again.`)) return;
          await post("/api/access/revoke", {id: dev.id});
          if (dev.this) location.replace("/pair"); else devices();
        });
        li.append(" ", x);
        ul.append(li);
      }
      box.append(ul);
    } catch (e) { box.textContent = "could not list the paired browsers"; }
  }

  async function pairCode() {
    const out = $("#pair-out");
    if (!out) return;
    out.textContent = "making a code…";
    try {
      const r = await post("/api/access/code");
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "failed");
      out.replaceChildren(el("div", "pair-code", d.code),
        el("p", "muted", `Open ${d.urls.join(" or ")} on the other device and type this code within ${Math.round(d.expires_in / 60)} minutes. It works once.`));
    } catch (e) { out.textContent = `could not make a code: ${e.message}`; }
  }

  async function load() {
    try {
      const r = await fetch("/api/access/me", {cache: "no-store"});
      if (!r.ok) return;
      state = await r.json();
      badge();
      devices();
      const b = $("#pair-btn");
      if (b) b.addEventListener("click", pairCode);
    } catch (e) { /* an older Pi without access control: nothing to show */ }
    if ("serviceWorker" in navigator && window.isSecureContext && location.protocol === "https:") {
      navigator.serviceWorker.register("/sw.js").catch(() => {});  // lets Chrome install it as an app window
    }
  }

  return {load};
})();
PolyAccess.load();
