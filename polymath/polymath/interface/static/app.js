"use strict";
// Polymath dashboard: plain JS, canvas charts, no external libraries.
const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
const fmt = (n) => (n === null || n === undefined) ? "–" : Number(n).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; };

async function get(path) {
  const r = await fetch(path, {cache: "no-store"});
  if (!r.ok && r.status !== 503) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.height / (canvas._dpr || 1);
  canvas._dpr = dpr;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return {ctx, w, h};
}

function axes(ctx, w, h, pad, ymax, ylabel) {
  ctx.strokeStyle = css("--line"); ctx.fillStyle = css("--muted"); ctx.lineWidth = 1; ctx.font = "11px system-ui";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (h - pad.t - pad.b) * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    const v = ymax * (1 - i / 4);
    ctx.fillText(ylabel ? ylabel(v) : (v >= 100 ? Math.round(v) : v.toFixed(1)), 2, y + 4);
  }
}

function lineChart(canvas, series, opts = {}) {
  const {ctx, w, h} = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const pad = {l: 42, r: 10, t: 10, b: 22};
  const pts = series.flatMap((s) => s.points);
  if (!pts.length) { ctx.fillStyle = css("--muted"); ctx.fillText("no data yet", w / 2 - 30, h / 2); return; }
  const xmin = Math.min(...pts.map((p) => p[0])), xmax = Math.max(...pts.map((p) => p[0]), xmin + 1);
  const ymax = opts.ymax ?? Math.max(1e-9, ...pts.map((p) => p[1])) * 1.1;
  axes(ctx, w, h, pad, ymax, opts.ylabel);
  const X = (x) => pad.l + (w - pad.l - pad.r) * (x - xmin) / (xmax - xmin);
  const Y = (y) => h - pad.b - (h - pad.t - pad.b) * y / ymax;
  series.forEach((s, i) => {
    ctx.strokeStyle = s.color || css(SERIES[i % SERIES.length]); ctx.lineWidth = 2;
    if (s.dashed) ctx.setLineDash([5, 4]); else ctx.setLineDash([]);
    ctx.beginPath();
    s.points.forEach((p, j) => (j ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
    ctx.stroke();
    if (s.points.length === 1) { ctx.fillStyle = ctx.strokeStyle; ctx.beginPath(); ctx.arc(X(s.points[0][0]), Y(s.points[0][1]), 3, 0, 7); ctx.fill(); }
  });
  ctx.setLineDash([]);
  ctx.fillStyle = css("--muted");
  ctx.fillText(new Date(xmin * 1000).toLocaleDateString(), pad.l, h - 6);
  const end = new Date(xmax * 1000).toLocaleDateString();
  ctx.fillText(end, w - pad.r - ctx.measureText(end).width, h - 6);
}

function barChart(canvas, items) {
  const {ctx, w, h} = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  if (!items.length) { ctx.fillStyle = css("--muted"); ctx.fillText("no data yet", w / 2 - 30, h / 2); return; }
  const max = Math.max(...items.map((i) => i[1]), 1e-9);
  const rowH = Math.min(26, (h - 4) / items.length);
  ctx.font = "12px system-ui";
  items.forEach(([label, v], i) => {
    const y = 2 + i * rowH, bw = (w - 170) * v / max;
    ctx.fillStyle = css(SERIES[i % SERIES.length]);
    ctx.fillRect(110, y + 3, Math.max(2, bw), rowH - 8);
    ctx.fillStyle = css("--text");
    ctx.fillText(String(label).slice(0, 16), 0, y + rowH / 2 + 4);
    ctx.fillStyle = css("--muted");
    ctx.fillText(fmt(v), 116 + bw, y + rowH / 2 + 4);
  });
}

function legend(el, names) {
  // CSSOM, not style="" attributes: the dashboard's CSP forbids inline styles
  el.replaceChildren(...names.map((n, i) => {
    const span = document.createElement("span");
    span.textContent = n;
    span.style.setProperty("--c", `var(${SERIES[i % SERIES.length]})`);
    return span;
  }));
}

function table(el, head, rows) {
  el.innerHTML = `<tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>` +
    (rows.length ? rows.map((r) => `<tr>${r.map((c, i) => `<td class="${typeof c === "number" && i ? "num" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${head.length}" class="muted">nothing yet</td></tr>`);
}

function agentsTable(ag) {
  const rows = ag.agents.filter((a) => a.status !== "retired");
  const el = $("#t-agents");
  if (!rows.length) { table(el, ["agent", "directive"], []); }
  else {
    el.innerHTML = "<tr><th>agent</th><th>directive</th><th>level</th><th class=num>XP</th><th class=num>net reward</th>" +
      "<th class=num>right / wrong</th><th>status</th></tr>" + rows.map((a) =>
      `<tr><td>${esc(a.name)}${a.origin === "evolved" ? ` <span class="muted">gen ${a.generation}</span>` : ""}</td>` +
      `<td>${esc(a.directive)}</td><td><span class="lvl">${esc(a.level)}</span></td><td class="num">${esc(a.xp)}</td>` +
      `<td class="num">${esc(a.reward)}</td><td class="num">${esc(a.correct)} / ${esc(a.wrong)}</td><td>${esc(a.status)}</td></tr>`
    ).join("");
  }
  table($("#t-rewards"), ["when", "agent", "reward", "why"], ag.rewards.slice(0, 10).map((r) => [ago(r.at), r.name,
    (r.amount > 0 ? "+" : "") + Number(r.amount).toFixed(2), r.reason]));
}

function kpis(o) {
  const q = o.quiz;
  const items = [
    ["Documents", fmt(o.documents_total)], ["Entities", fmt(o.entities)],
    ["Facts", fmt((o.triples.sourced || 0) + (o.triples.inferred || 0))], ["Inferred", fmt(o.triples.inferred || 0)],
    ["Disputed", fmt(o.triples.disputed || 0)],
    ["Quiz", q ? `${Math.round(q.accuracy * 100)}% (chance ${Math.round(q.chance * 100)}%)` : "–"],
    ["Linker precision", o.linker ? o.linker.precision : "–"],
    ["CPU temp", o.vitals && o.vitals.temp_c != null ? `${o.vitals.temp_c.toFixed(1)} °C` : "–"],
    ["Jobs queued", fmt((o.queue.queued || 0))],
  ];
  $("#kpis").innerHTML = items.map(([k, v]) => `<div class="kpi"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
}

async function refresh() {
  try {
    const [o, ts, tp, ag, dg] = await Promise.all([get("/api/overview"), get("/api/timeseries"), get("/api/topics"),
      get("/api/agents"), get("/api/digest")]);
    $("#digest-title").textContent = `What I learned — ${dg.day}`;
    const ul = $("#digest");
    ul.replaceChildren(...dg.lines.map((line) => { const li = document.createElement("li"); li.textContent = line; return li; }));
    table($("#t-sites"), ["site", "verdict", "cited by", "why"], dg.sites.map((x) => [x.site, x.status, x.citations, x.reason]));
    const hl = $("#health");
    const state = o.health.state || "unknown";
    hl.textContent = o.health.ok ? `running · cycle ${fmt(o.health.cycle)}` : `${state}${o.paused_reason ? ": " + o.paused_reason : ""}`;
    hl.className = "pill " + (o.health.ok ? "ok" : state === "paused" ? "warn" : "bad");
    $("#meta").textContent = `heartbeat ${o.health.heartbeat_age_s ?? "–"} s`;
    kpis(o);
    const sources = Object.keys(ts.documents_per_day);
    lineChart($("#c-docs"), sources.map((s) => ({points: ts.documents_per_day[s]})));
    legend($("#l-docs"), sources);
    lineChart($("#c-quiz"), [{points: ts.quiz.map((q) => [q[0], q[1] * 100])},
      {points: ts.quiz.map((q) => [q[0], q[2] * 100]), dashed: true}], {ymax: 100, ylabel: (v) => `${Math.round(v)}%`});
    legend($("#l-quiz"), ["accuracy", "chance"]);
    barChart($("#c-effort"), Object.entries(ts.effort_24h).sort((a, b) => b[1] - a[1]).slice(0, 8));
    lineChart($("#c-vitals"), [{points: ts.vitals_24h.filter((v) => v[1] != null).map((v) => [v[0], v[1]])},
      {points: ts.vitals_24h.map((v) => [v[0], (v[2] || 0) * 10])}]);
    legend($("#l-vitals"), ["CPU °C", "load × 10"]);
    table($("#t-top"), ["topic", "priority", "gap", "importance"], tp.top.map((t) => [t.name, t.priority, t.gap, t.importance]));
    table($("#t-weak"), ["topic", "gap", "importance"], tp.weakest.map((t) => [t.name, t.gap, t.importance]));
    table($("#t-decisions"), ["when", "chose", "why"], tp.decisions.map((d) => [ago(d.at), d.chosen, d.reason]));
    agentsTable(ag);
    table($("#t-cycles"), ["#", "job", "status", "CPU s", "value"], o.recent_cycles.map((c) => [c.id, c.action, c.status, c.cpu, c.value]));
  } catch (e) {
    $("#health").textContent = "dashboard cannot reach the database";
    $("#health").className = "pill bad";
  }
}

$("#ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("#question").value.trim();
  if (!q) return;
  const out = $("#answer");
  out.textContent = "thinking…";
  try {
    const r = await fetch("/api/ask", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({question: q})});
    const a = await r.json();
    if (!r.ok) { out.textContent = a.error || "error"; return; }
    if (!a.statements.length) { out.textContent = `I don't know yet. ${a.note || ""}`; return; }
    const refs = [];
    const html = a.statements.map((s) => {
      const marks = s.citations.map((c) => { let k = refs.findIndex((x) => x.title === c.title && x.url === c.url); if (k < 0) { refs.push(c); k = refs.length - 1; } return k + 1; });
      return `<div class="stmt">${esc(s.text)}<span class="kind">${esc(s.kind)} · ${s.confidence.toFixed(2)}</span> <span class="muted">[${marks.join(", ")}]</span></div>`;
    }).join("");
    out.innerHTML = html + `<div class="refs">${refs.map((c, i) => `[${i + 1}] ${esc(c.title)} — ${c.url ? `<a href="${esc(c.url)}" rel="noreferrer">${esc(c.url)}</a>` : esc(c.source)} — ${esc(c.license)}`).join("<br>")}</div>`;
  } catch (e) { out.textContent = "the dashboard could not answer"; }
});

$("#kg-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("#kg-q").value.trim();
  const d = await get(`/api/knowledge?q=${encodeURIComponent(q)}`);
  $("#kg").innerHTML = d.entities.length ? d.entities.map((e) => `<h3>${esc(e.label)}</h3><p class="muted">${esc(e.description || "")}</p><table>${
    e.facts.map((f) => `<tr><td>${esc(f.direction === "out" ? f.predicate : "← " + f.predicate)}</td><td>${esc(f.other)}</td><td>${esc(f.status)}</td><td class="num">${f.confidence}</td></tr>`).join("")}</table>`).join("")
    : `<p class="muted">No entity called “${esc(q)}” yet.</p>`;
});

// ------------------------------------------------------------------ face, version, new panels
let faceCursor = null;
async function pollFace() {
  try {
    const d = await get("/api/feed" + (faceCursor ? "?cursor=" + encodeURIComponent(faceCursor) : ""));
    faceCursor = d.cursor || faceCursor;
    PolyFace.update(d.face);
    if (d.status && !d.status.online) PolyFace.offline();
    setNow(d.status);
    const b = $("#badge");
    b.textContent = d.badge ? d.badge.text : "";
    b.className = "badge " + (d.badge && d.badge.current === false ? "s-byellow" : "muted");
  } catch (e) { PolyFace.update(null); setNow(null); }
}

function setNow(st) {
  let text = "waiting for the agent…", cls = "off";
  if (st && !st.online) { text = "not running"; }
  else if (st && (st.state === "paused" || st.mode === "pause")) { text = "paused" + (st.paused_reason ? ` — ${st.paused_reason}` : ""); cls = "warn"; }
  else if (st) {
    const a = st.activity || "starting up";
    text = a.charAt(0).toUpperCase() + a.slice(1) + (st.mode === "throttle" ? " (slowed: running hot)" : st.mode === "yield" ? " (slowed: low on disk)" : "");
    cls = "on";
  }
  $("#now").textContent = text;
  $("#now-banner").className = "now-banner " + cls;
}

function list(el, lines, empty) {
  el.replaceChildren(...(lines.length ? lines : [empty]).map((t) => { const li = document.createElement("li"); li.textContent = t; return li; }));
  if (!lines.length) el.firstElementChild.className = "muted";
}

async function insights() {
  try {
    const d = await get("/api/insights");
    $("#recap-title").textContent = `The week in review — ${d.recap.week}`;
    list($("#recap"), d.recap.lines, "nothing yet");
    $("#pred-line").textContent = d.predictions.made ? d.predictions.line : "No guesses yet: it predicts facts it has not read once it knows enough similar ones.";
    list($("#pred-hits"), d.predictions.recent_hits.map((h) => `✓ ${h.subject} → ${h.relation} → ${h.guess} (${Math.round(h.confidence * 100)}% sure)`), "no confirmed guesses yet");
    list($("#dyk"), d.didyouknow, "nothing surprising yet");
    const wearLines = d.wear.map((w) => w.line + (w.status && w.status !== "ok" ? ` — ${w.status === "critical" ? "far " : ""}over budget` : ""));
    if (d.home) wearLines.unshift(`The brain lives on the drive ${d.home.name || d.home.id}; the SD card only boots the Pi.`);
    list($("#wear"), wearLines, "measuring (every 15 minutes)…");
  } catch (e) { /* the main refresh reports database problems */ }
}

// ------------------------------------------------------------------ the knowledge map
const MAP = {data: null, frame: 0, playing: false, timer: null, hover: null, pos: []};
const CLUSTER = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];

async function loadMap() {
  try {
    MAP.data = await get("/api/map");
    const n = MAP.data.times.length;
    const slider = $("#map-time");
    slider.max = String(Math.max(0, n - 1));
    if (!MAP.playing) { MAP.frame = n - 1; slider.value = String(n - 1); }
    drawMap();
  } catch (e) { /* drawn as empty */ }
}

function sizeAt(node, frame) {
  const h = MAP.data.history[String(node.id)];
  return h && h.length ? h[Math.min(frame, h.length - 1)] : node.docs;
}

function drawMap() {
  const canvas = $("#c-map");
  const {ctx, w, h} = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const d = MAP.data;
  if (!d || !d.nodes.length) { ctx.fillStyle = css("--muted"); ctx.fillText("no topics yet", w / 2 - 35, h / 2); return; }
  const f = Math.max(0, MAP.frame);
  const last = d.times.length - 1;
  const max = Math.max(...d.nodes.map((n) => n.docs), 1);
  const R = (v) => v > 0 ? 3 + 22 * Math.sqrt(v / max) : 0;
  const X = (x) => 20 + (w - 40) * x, Y = (y) => 16 + (h - 32) * y;
  const byId = new Map(d.nodes.map((n) => [n.id, n]));
  ctx.lineWidth = 1;
  for (const e of d.edges) {
    const a = byId.get(e.a), b = byId.get(e.b);
    if (!a || !b || sizeAt(a, f) <= 0 || sizeAt(b, f) <= 0) continue;
    ctx.strokeStyle = css("--line");
    ctx.globalAlpha = Math.min(0.9, 0.25 + e.w);
    ctx.beginPath(); ctx.moveTo(X(a.x), Y(a.y)); ctx.lineTo(X(b.x), Y(b.y)); ctx.stroke();
  }
  ctx.globalAlpha = 1;
  MAP.pos = [];
  const ordered = [...d.nodes].sort((a, b) => sizeAt(a, f) - sizeAt(b, f));
  for (const n of ordered) {
    const v = sizeAt(n, f);
    if (v <= 0) continue;
    const r = R(v), x = X(n.x), y = Y(n.y);
    const color = css(CLUSTER[n.group % CLUSTER.length]);
    const prev = f > 0 ? sizeAt(n, f - 1) : 0;
    const growing = f === last ? n.day > 0 : v > prev;  // lit up: read about in the last day (or this step)
    if (growing) {
      const g = ctx.createRadialGradient(x, y, r * 0.4, x, y, r * 2.4);
      g.addColorStop(0, color); g.addColorStop(1, "transparent");
      ctx.globalAlpha = 0.35; ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r * 2.4, 0, 7); ctx.fill();
    }
    ctx.globalAlpha = growing ? 1 : 0.75;
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill();
    if (MAP.hover === n.id) { ctx.globalAlpha = 1; ctx.strokeStyle = css("--text"); ctx.lineWidth = 2; ctx.stroke(); ctx.lineWidth = 1; }
    MAP.pos.push({n, x, y, r, v});
  }
  ctx.globalAlpha = 1;
  ctx.font = "12px system-ui"; ctx.fillStyle = css("--text");
  const boxes = [];  // labels for the biggest topics, skipping any that would overlap one already drawn
  for (const p of [...MAP.pos].sort((a, b) => b.v - a.v)) {
    if (boxes.length >= 22) break;
    const text = p.n.name.slice(0, 26);
    const bw = ctx.measureText(text).width;
    let x = p.x + p.r + 3;
    if (x + bw > w - 4) x = p.x - p.r - 3 - bw;
    const box = [x - 2, p.y - 9, x + bw + 2, p.y + 6];
    if (boxes.some((o) => box[0] < o[2] && box[2] > o[0] && box[1] < o[3] && box[3] > o[1])) continue;
    boxes.push(box);
    ctx.fillText(text, x, p.y + 4);
  }
  const t = d.times[f];
  $("#map-when").textContent = t ? new Date(t * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) +
    (f === last ? " (now)" : "") : "";
}

function mapHit(ev) {
  const rect = $("#c-map").getBoundingClientRect();
  const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
  let best = null;
  for (const p of MAP.pos) { const d2 = (p.x - mx) ** 2 + (p.y - my) ** 2; if (d2 <= (p.r + 4) ** 2 && (!best || p.r < best.r)) best = p; }
  return {best, mx, my};
}

$("#c-map").addEventListener("mousemove", (ev) => {
  const {best, mx, my} = mapHit(ev);
  const tip = $("#map-tip");
  if (!best) { tip.hidden = true; if (MAP.hover !== null) { MAP.hover = null; drawMap(); } return; }
  tip.hidden = false;
  tip.textContent = `${best.n.name} — ${fmt(best.v)} documents` + (best.n.week ? ` · +${fmt(best.n.week)} this week` : "") + ` · ${best.n.kind}`;
  tip.style.left = `${Math.min(mx + 14, $("#c-map").clientWidth - 270)}px`;
  tip.style.top = `${my + 12}px`;
  if (MAP.hover !== best.n.id) { MAP.hover = best.n.id; drawMap(); }
});
$("#c-map").addEventListener("mouseleave", () => { $("#map-tip").hidden = true; MAP.hover = null; drawMap(); });
$("#c-map").addEventListener("click", (ev) => {
  const {best} = mapHit(ev);
  if (!best) return;
  $("#question").value = `Tell me about ${best.n.name}`;
  $("#ask-form").requestSubmit();
  $("#question").scrollIntoView({behavior: "smooth", block: "center"});
});
$("#map-time").addEventListener("input", (ev) => { MAP.frame = Number(ev.target.value); stopMap(); drawMap(); });
function stopMap() { MAP.playing = false; clearInterval(MAP.timer); $("#map-play").textContent = "▶ time-lapse"; }
$("#map-play").addEventListener("click", () => {
  if (MAP.playing) { stopMap(); return; }
  if (!MAP.data || MAP.data.times.length < 2) return;
  MAP.playing = true; $("#map-play").textContent = "⏸ pause";
  if (MAP.frame >= MAP.data.times.length - 1) MAP.frame = 0;
  MAP.timer = setInterval(() => {
    MAP.frame += 1;
    $("#map-time").value = String(MAP.frame);
    drawMap();
    if (MAP.frame >= MAP.data.times.length - 1) stopMap();
  }, Math.max(60, 6000 / MAP.data.times.length));
});

PolyFace.load().catch(() => {});
refresh();
insights();
loadMap();
pollFace();
setInterval(refresh, 10000);
setInterval(insights, 60000);
setInterval(loadMap, 120000);
setInterval(pollFace, 2000);
setInterval(() => PolyFace.paint($("#face")), 250);
window.addEventListener("resize", () => { refresh(); drawMap(); });
