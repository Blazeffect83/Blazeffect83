"use client";

import { useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { CalendarGrid, type DayMark } from "@/components/calendar-grid";
import { dateKey } from "@/lib/utils";

function buildMarks(monthOffset: number): Record<string, DayMark> {
  const map: Record<string, DayMark> = {};
  const today = new Date();
  for (let i = 0; i < 90; i++) {
    const d = new Date();
    d.setDate(today.getDate() - i);
    const k = dateKey(d);
    const hash = (d.getDate() * 7 + d.getMonth() * 11) % 5;
    if (hash !== 0) {
      map[k] = {
        date: k,
        workout: hash > 1,
        macros: hash !== 2,
        intensity: ((hash > 3 ? 3 : hash) as 0 | 1 | 2 | 3) || 1,
      };
    }
  }
  return map;
}

export default function CalendarPage() {
  const [offset, setOffset] = useState(0);
  const marks = buildMarks(offset);

  const totals = Object.values(marks);
  const liftDays = totals.filter((d) => d.workout).length;
  const macroDays = totals.filter((d) => d.macros).length;

  return (
    <div className="container-x py-8 lg:py-12">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">/ Consistency</p>
          <h1 className="font-display text-5xl tracking-brutal sm:text-7xl">The receipts</h1>
          <p className="mt-3 max-w-xl text-bone-200">
            Every cell is a day you showed up. Lifts solid-fill. Macro-on-target rings the cell. Misses look
            at you, and that's the point.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setOffset((o) => o - 1)}
            className="inline-flex h-9 w-9 items-center justify-center border border-bone-50/15 hover:border-bone-50/30"
            aria-label="Previous month"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button
            onClick={() => setOffset(0)}
            className="border border-bone-50/15 bg-ink-900 px-3 py-2 font-mono text-[10px] uppercase tracking-widest hover:border-bone-50/30"
          >
            This month
          </button>
          <button
            onClick={() => setOffset((o) => o + 1)}
            className="inline-flex h-9 w-9 items-center justify-center border border-bone-50/15 hover:border-bone-50/30"
            aria-label="Next month"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </header>

      <section className="mt-8 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat n={liftDays} label="Lift days (90d)" />
        <Stat n={macroDays} label="Macro days (90d)" />
        <Stat n="12" label="Current streak" />
        <Stat n="41" label="Longest streak" />
      </section>

      <div className="mt-8">
        <CalendarGrid marks={marks} monthOffset={offset} />
      </div>

      <section className="mt-10 grid gap-5 lg:grid-cols-2">
        <div className="border border-bone-50/10 bg-ink-900/60 p-6">
          <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">/ Heatmap</p>
          <h3 className="mt-1 font-display text-2xl tracking-brutal">Last 90 days, at a glance</h3>
          <Heatmap />
        </div>
        <div className="border border-bone-50/10 bg-ink-900/60 p-6">
          <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">/ Patterns</p>
          <h3 className="mt-1 font-display text-2xl tracking-brutal">What the data says</h3>
          <ul className="mt-4 space-y-3 text-sm">
            <li className="border-b border-bone-50/10 pb-3">
              You miss <span className="font-display text-bone-50">Wednesdays</span> 3× more often than any
              other day. Schedule a lighter session?
            </li>
            <li className="border-b border-bone-50/10 pb-3">
              Macro adherence dips on lift days you log <em>after</em> 9pm. Pre-log lunch the night before.
            </li>
            <li>
              Your streaks reliably break after travel weeks. Try a 25-min hotel-room template?
            </li>
          </ul>
        </div>
      </section>
    </div>
  );
}

function Stat({ n, label }: { n: string | number; label: string }) {
  return (
    <div className="border border-bone-50/10 bg-ink-900/60 p-5">
      <div className="font-display text-4xl tracking-brutal">{n}</div>
      <div className="mt-1 font-mono text-[10px] uppercase tracking-widest text-bone-300">{label}</div>
    </div>
  );
}

function Heatmap() {
  const cells = Array.from({ length: 91 }, (_, i) => {
    const v = (i * 13 + 7) % 5;
    const lvl = v === 0 ? 0 : v;
    return ["bg-ink-800", "bg-maroon-900", "bg-maroon-700", "bg-maroon-500", "bg-maroon-300"][lvl];
  });
  return (
    <div className="mt-4 grid grid-rows-7 grid-flow-col gap-1.5" style={{ gridAutoColumns: "minmax(10px, 1fr)" }}>
      {cells.map((bg, i) => (
        <span key={i} className={`aspect-square ${bg} border border-bone-50/5`} />
      ))}
    </div>
  );
}
