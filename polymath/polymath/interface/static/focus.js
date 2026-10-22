"use strict";
// What it is learning and how far along: the LEARNING card of the dashboard and the live page. The same data as
// the terminal's LEARNING lines (status.focus from /api/feed, made by polymath/interface/focus.py).
const PolyFocus = (() => {
  const pct = (x) => Math.round(100 * x) + "%";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function stepText(s) {
    const done = Number(s.done || 0), total = s.total ? Number(s.total) : 0;
    if (s.unit === "bytes") return total ? `${pct(done / total)} of the file` : "";
    return total ? `${done.toLocaleString()} of ${total.toLocaleString()} ${s.unit}` : `${done.toLocaleString()} ${s.unit} so far`;
  }

  function basis(t) {
    if (t.based_on === "quiz") return `${t.right} of ${t.asked} test questions right`;
    if (t.linked) return `read about ${Number(t.read).toLocaleString()} of ${Number(t.linked).toLocaleString()} things it mentions`;
    return "";
  }

  function row(label, frac, value, cls, note) {
    const r = el("div", "focus-row");
    r.append(el("div", "focus-label", label));
    const track = el("div", "focus-bar " + cls);
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", label);
    if (frac !== null) {
      track.setAttribute("aria-valuemin", "0");
      track.setAttribute("aria-valuemax", "100");
      track.setAttribute("aria-valuenow", String(Math.round(100 * frac)));
      const fill = el("div", "focus-fill");
      fill.style.width = (100 * Math.max(0, Math.min(1, frac))).toFixed(1) + "%";  // CSSOM: allowed by the CSP
      track.append(fill);
    } else {
      track.classList.add("open-ended");
    }
    r.append(track);
    const v = el("div", "focus-val");
    for (const [text, c] of value) v.append(el("span", c, text));
    r.append(v);
    if (note) r.append(el("div", "focus-note", note));
    return r;
  }

  function render(root, st) {
    if (!root) return;
    root.replaceChildren();
    if (!st || !st.online) { root.hidden = true; return; }
    root.hidden = false;
    const f = st.focus;
    const head = el("div", "focus-head");
    if (!f) {
      root.className = "card focus-card between";
      head.append(el("span", "focus-tag", "LEARNING"), el("span", "focus-what muted", "between topics"));
      root.append(head);
      const prev = st.last_focus;
      if (prev && prev.what) {
        const known = prev.topic && prev.topic.known != null ? ` (knows ${pct(prev.topic.known)})` : "";
        root.append(el("div", "focus-item", `last: ${prev.what}${known}`));
      }
      if (st.curious && st.curious.length) root.append(el("div", "focus-item", "next: " + st.curious.slice(0, 3).join(" · ")));
      return;
    }
    root.className = "card focus-card" + (f.finished ? " finished" : "");
    head.append(el("span", "focus-tag", f.finished ? "LEARNED" : "LEARNING"), el("span", "focus-what", f.what || ""));
    if (f.why) head.append(el("span", "focus-why", f.why));
    root.append(head);
    if (f.item) {
      const verb = f.kind === "wikidata.dump" ? "just learned about" : "just read";
      root.append(el("div", "focus-item", `${verb} “${f.item}”` + (f.source ? ` · ${f.source}` : "")));
    } else if (f.source && !String(f.what || "").includes(f.source)) {
      root.append(el("div", "focus-item", `from ${f.source}`));
    }
    const t = f.topic || {};
    if (t.known != null) {
      const gain = t.start != null ? t.known - t.start : 0;
      const value = [[pct(t.known), "focus-pct"]];
      if (gain >= 0.005) value.push([` +${pct(gain)} this round`, "focus-gain"]);
      const label = t.name && t.name !== f.what ? `Knows ${t.name}` : "Knows the topic";
      root.append(row(label, t.known, value, "known", basis(t) + (t.hourly ? " (updated hourly)" : "")));
    }
    if (f.step) {
      const total = f.step.total ? Number(f.step.total) : 0;
      const frac = total ? Math.max(0, Math.min(1, Number(f.step.done || 0) / total)) : null;
      root.append(row("This round", frac, [[stepText(f.step) + (f.finished ? " ✓" : ""), ""]], "round", ""));
    }
  }

  return {render};
})();
