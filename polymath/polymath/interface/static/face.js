"use strict";
// Polymath's face for the browser: the same moods as the terminal (polymath/interface/face.py), animated here.
// The server says which mood the agent is in and which reaction the latest events call for; this file only
// keeps time: reactions last their seconds, frames advance every 0.5 s, the spark pulses, the eyes blink.
const PolyFace = (() => {
  let defs = null;
  let base = "connecting";
  let reaction = null; // {mood, until, priority}
  async function load() {
    const r = await fetch("/api/face", {cache: "no-store"});
    defs = await r.json();
  }
  function update(state) {
    if (!state) { base = "connecting"; return; }
    base = state.mood || base;
    const r = state.reaction;
    const now = Date.now() / 1000;
    if (r && (!reaction || now >= reaction.until || r.priority >= reaction.priority)) {
      reaction = {mood: r.mood, until: now + r.seconds, priority: r.priority};
    }
  }
  function offline() { base = "offline"; reaction = null; }
  function mood(now) {
    if (base === "offline" || base === "connecting") return base;
    return reaction && now < reaction.until ? reaction.mood : base;
  }
  // {spark, body, style, sparkStyle} for this moment
  function frame(now = Date.now() / 1000) {
    if (!defs) return {spark: " ", body: "[·_·]", style: "dim", sparkStyle: "dim", mood: "connecting"};
    const name = mood(now);
    const m = defs.moods[name] || defs.moods.connecting;
    const n = Math.floor(now / defs.frame_s);
    let face = m.frames[n % m.frames.length];
    if (m.blink && n % defs.blink_every === 0 && [...face].length === 3) face = "-" + [...face][1] + "-";
    const trail = m.trail[n % m.trail.length];
    let spark, sparkStyle;
    if (name === "offline") { spark = "○"; sparkStyle = "red"; }
    else if (name === "connecting") { spark = n % 2 ? "✧" : " "; sparkStyle = "dim"; }
    else { spark = n % 4 === 0 ? "✦" : "✧"; sparkStyle = n % 4 === 0 ? "byellow" : "yellow"; }
    return {spark, body: `[${face}]${trail}`, style: m.style || "", sparkStyle, mood: name};
  }
  function paint(el, now) {
    const f = frame(now);
    if (!el._spark) {
      el.textContent = "";
      el._spark = document.createElement("span");
      el._body = document.createElement("span");
      el.append(el._spark, el._body);
    }
    el._spark.textContent = f.spark;
    el._spark.className = "s-" + f.sparkStyle;
    el._body.textContent = f.body;
    el._body.className = "s-" + (f.style || "plain");
    el.title = f.mood;
  }
  return {load, update, offline, frame, paint};
})();
