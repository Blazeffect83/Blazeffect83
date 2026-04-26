"use client";

import Link from "next/link";
import { motion } from "framer-motion";
import { ArrowUpRight } from "lucide-react";

const PANELS = [
  {
    eyebrow: "/ logger",
    title: "Set, RPE, rest.",
    body:
      "Tap to log. Hold to autofill last session's weight. Rest timer with a kettlebell-on-platform haptic.",
    href: "/app/workout",
    accent: "Top set: 235lb × 5 @ RPE 8",
    figure: (
      <div className="grid grid-cols-3 gap-2">
        {[
          ["Set 1", "225 × 5", "RPE 7"],
          ["Set 2", "235 × 5", "RPE 8"],
          ["Set 3", "235 × 5", "RPE 8.5"],
          ["Set 4", "235 × 4", "RPE 9.5"],
        ].map(([a, b, c]) => (
          <div key={a} className="border border-bone-50/10 bg-ink-900 p-3 text-xs">
            <div className="font-mono text-[10px] uppercase tracking-widest text-bone-300">{a}</div>
            <div className="mt-1 font-display text-xl tracking-brutal">{b}</div>
            <div className="mt-1 text-[10px] text-bone-300">{c}</div>
          </div>
        ))}
      </div>
    ),
  },
  {
    eyebrow: "/ overload coach",
    title: "Next-set prescription.",
    body:
      "Reads RPE and rep history, fits a per-lift performance curve, prescribes the next jump. Shows the math.",
    href: "/app/workout",
    accent: "Prescribed: 4×5 @ 240lb · RPE 8",
    figure: (
      <div className="space-y-2 text-sm">
        <div className="border border-maroon-500/40 bg-maroon-900/30 p-3">
          <div className="font-mono text-[10px] uppercase tracking-widest text-maroon-200">Coach says</div>
          <div className="mt-1 font-display text-2xl tracking-brutal">+5 lb. 4 × 5 @ RPE 8.</div>
          <div className="mt-2 text-xs text-bone-200">
            Last top set finished @ RPE 8.5 with reps in the bank. e1RM ↑ 4lb week-over-week. Headroom present.
          </div>
        </div>
      </div>
    ),
  },
  {
    eyebrow: "/ macros",
    title: "Targets that move with you.",
    body: "Carbs shift up on heavy days, down on rest. Logged weight tunes your weekly maintenance estimate.",
    href: "/app/macros",
    accent: "Today: 2,210 / 2,750 kcal",
    figure: (
      <div className="grid grid-cols-3 gap-2">
        {[
          { l: "Protein", v: "171", t: "195g" },
          { l: "Carbs", v: "242", t: "320g" },
          { l: "Fat", v: "62", t: "75g" },
        ].map((m) => (
          <div key={m.l} className="border border-bone-50/10 bg-ink-900 p-3">
            <div className="font-mono text-[10px] uppercase tracking-widest text-bone-300">{m.l}</div>
            <div className="mt-1 font-display text-2xl tracking-brutal">{m.v}</div>
            <div className="text-[10px] text-bone-300">/ {m.t}</div>
          </div>
        ))}
      </div>
    ),
  },
  {
    eyebrow: "/ calendar",
    title: "Consistency, on display.",
    body: "Every logged session and every macro day fills a cell. Streaks compound. Misses look you in the eye.",
    href: "/app/calendar",
    accent: "21 active days this month",
    figure: (
      <div className="grid grid-cols-7 gap-1">
        {Array.from({ length: 35 }).map((_, i) => {
          const lvl = [0, 1, 2, 3, 0, 1, 2, 2, 3, 3, 1, 0, 2, 3, 1, 2, 3, 3, 2, 1, 0, 2, 3, 1, 2, 3, 3, 1, 2, 3, 0, 1, 2, 3, 2][i] ?? 0;
          const bg = ["bg-ink-800", "bg-maroon-900", "bg-maroon-700", "bg-maroon-500"][lvl];
          return <div key={i} className={`aspect-square ${bg} border border-bone-50/5`} />;
        })}
      </div>
    ),
  },
];

export function Showcase() {
  return (
    <section className="relative py-24 sm:py-32">
      <div className="container-x">
        <div className="flex items-end justify-between gap-6">
          <div>
            <p className="eyebrow">/ 02 — Inside the app</p>
            <h2 className="mt-3 max-w-3xl font-display text-5xl tracking-brutal sm:text-7xl">
              Every screen earns its rent.
            </h2>
          </div>
          <Link
            href="/app"
            className="hidden font-mono text-[11px] uppercase tracking-widest text-bone-300 hover:text-bone-50 sm:inline-flex"
          >
            Open live preview <ArrowUpRight className="ml-1 h-3 w-3" />
          </Link>
        </div>

        <div className="mt-12 grid gap-px overflow-hidden border border-bone-50/10 bg-bone-50/10 lg:grid-cols-2">
          {PANELS.map((p, i) => (
            <motion.div
              key={p.title}
              initial={{ opacity: 0, y: 12 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{ duration: 0.45, delay: i * 0.05 }}
              className="bg-ink-950 p-8"
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">{p.eyebrow}</p>
                  <h3 className="mt-2 font-display text-3xl tracking-brutal sm:text-4xl">{p.title}</h3>
                </div>
                <Link
                  href={p.href}
                  className="inline-flex h-8 w-8 items-center justify-center border border-bone-50/15 transition-colors hover:border-maroon-400 hover:bg-maroon-900/40"
                  aria-label={`Open ${p.title}`}
                >
                  <ArrowUpRight className="h-3.5 w-3.5" />
                </Link>
              </div>
              <p className="mt-3 max-w-md text-sm leading-relaxed text-bone-200">{p.body}</p>
              <div className="mt-6">{p.figure}</div>
              <div className="mt-6 inline-flex items-center gap-2 border border-bone-50/10 bg-ink-900 px-3 py-1.5 font-mono text-[10px] uppercase tracking-widest text-bone-200">
                {p.accent}
              </div>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}
