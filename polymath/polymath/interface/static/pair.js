"use strict";
// Pairing page: trade a one-time code from the Pi for a device key (an HttpOnly cookie the page never sees).
const $ = (s) => document.querySelector(s);

async function me() {
  try {
    const r = await fetch("/api/access/me", {cache: "no-store"});
    const d = await r.json();
    if (d.device) { location.replace("/"); return; }  // already paired
    if (d.fingerprint) {
      $("#fp-sha1").textContent = d.fingerprint.sha1;
      $("#fp-sha256").textContent = d.fingerprint.sha256;
    }
    if (!d.remote) $("#pair-msg").textContent = "This is the Pi itself: it needs no pairing. Pair your other devices at https://<this Pi>:8443.";
  } catch (e) { /* the form still works */ }
}

$("#code").addEventListener("input", (e) => {
  const raw = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  e.target.value = raw.length > 4 ? raw.slice(0, 4) + "-" + raw.slice(4) : raw;
});

$("#pair-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#pair-msg");
  const btn = $("#pair-go");
  btn.disabled = true;
  msg.className = "pair-msg";
  msg.textContent = "pairing…";
  try {
    const r = await fetch("/api/access/pair", {
      method: "POST", headers: {"Content-Type": "application/json"}, cache: "no-store",
      body: JSON.stringify({code: $("#code").value, name: $("#name").value}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "pairing failed");
    msg.className = "pair-msg ok";
    msg.textContent = `Paired as “${d.device.name}”. Opening Polymath…`;
    setTimeout(() => location.replace("/"), 800);
  } catch (err) {
    msg.className = "pair-msg bad";
    msg.textContent = err.message;
    btn.disabled = false;
  }
});

me();
