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
    const [o, ts, tp, ag] = await Promise.all([get("/api/overview"), get("/api/timeseries"), get("/api/topics"),
      get("/api/agents")]);
    const hl = $("#health");
    const state = o.health.state || "unknown";
    hl.textContent = o.health.ok ? `running · cycle ${fmt(o.health.cycle)}` : `${state}${o.paused_reason ? ": " + o.paused_reason : ""}`;
    hl.className = "pill " + (o.health.ok ? "ok" : state === "paused" ? "warn" : "bad");
    $("#meta").textContent = `heartbeat ${o.health.heartbeat_age_s ?? "–"} s · v${o.health.version}`;
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

refresh();
setInterval(refresh, 10000);
window.addEventListener("resize", refresh);
