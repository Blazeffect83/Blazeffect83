"use strict";
// The live feed on a phone: the face, what it is doing, and what it learns as it happens (newest first).
const $ = (s) => document.querySelector(s);
const MAX_LINES = 300;
let cursor = null;
let failures = 0;
const compact = (n) => n >= 1e9 ? (n / 1e9).toFixed(2) + "B" : n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : Number(n || 0).toLocaleString();

function addLine(segs) {
  const li = document.createElement("li");
  for (const [text, style] of segs) {
    const span = document.createElement("span");
    span.textContent = text;
    if (style) span.className = "s-" + style;
    li.append(span);
  }
  const feed = $("#feed");
  feed.prepend(li);
  while (feed.children.length > MAX_LINES) feed.lastElementChild.remove();
}

function setNow(banner, el, st) {
  // what it is doing right now: large, high-contrast, coloured by state
  let text = "waiting for the agent…", cls = "off";
  if (st && !st.online) { text = "not running"; cls = "off"; }
  else if (st && (st.state === "paused" || st.mode === "pause")) { text = "paused" + (st.paused_reason ? ` — ${st.paused_reason}` : ""); cls = "warn"; }
  else if (st) {
    const a = st.activity || "starting up";
    text = a.charAt(0).toUpperCase() + a.slice(1) + (st.mode === "throttle" ? " (slowed: running hot)" : st.mode === "yield" ? " (slowed: low on disk)" : "");
    cls = "on";
  }
  el.textContent = text;
  banner.className = "now-banner " + cls;
}

function status(st, badge) {
  const s = $("#state");
  if (!st || !st.online) { s.textContent = st ? (st.state === "stopped" ? "agent stopped" : "agent offline") : "connecting…"; s.className = "s-red"; }
  else if (st.state === "paused" || ["pause", "throttle", "yield"].includes(st.mode)) { s.textContent = {pause: "paused", throttle: "throttled", yield: "slowed"}[st.mode] || "paused"; s.className = "s-yellow"; }
  else { s.textContent = "● learning"; s.className = "s-green"; }
  setNow($("#now-banner"), $("#now"), st);
  const c = (st && st.counts) || {};
  const q = c.quiz ? ` · quiz ${Math.round(c.quiz.accuracy * 100)}%` : "";
  $("#counts").textContent = st ? `${compact(c.documents)} docs · ${compact(c.facts)} facts · ${c.rules || 0} rules${q}` : "";
  const b = $("#badge");
  b.textContent = badge ? badge.text : "";
  b.className = "badge " + (badge && badge.current === false ? "s-byellow" : "muted");
}

async function poll() {
  try {
    const r = await fetch("/api/feed?render=1" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""), {cache: "no-store"});
    if (!r.ok) throw new Error(String(r.status));
    const d = await r.json();
    cursor = d.cursor || cursor;
    status(d.status, d.badge);
    PolyFace.update(d.face);
    if (d.status && !d.status.online) PolyFace.offline();
    for (const segs of [...(d.lines || [])].reverse()) addLine(segs);  // newest first; a block keeps its order
    failures = 0;
  } catch (e) {
    failures += 1;
    status(null, null);
    PolyFace.update(null);
    if (failures === 1) addLine([["waiting for the agent…", "yellow"]]);
  }
}

PolyFace.load().catch(() => {});
poll();
setInterval(poll, 1500);
setInterval(() => PolyFace.paint($("#face")), 250);
