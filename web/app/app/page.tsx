import Link from "next/link";
import { ArrowUpRight, Dumbbell, Flame, ImagePlus } from "lucide-react";
import { CalendarGrid, type DayMark } from "@/components/calendar-grid";
import { MacroRing } from "@/components/macro-ring";
import { MACRO_TARGETS, SAMPLE_HISTORY } from "@/lib/data";
import { dateKey } from "@/lib/utils";

export default function DashboardPage() {
  const today = new Date();
  const marks: Record<string, DayMark> = {};
  // Build last 60 days of mock activity for visual continuity.
  for (let i = 0; i < 60; i++) {
    const d = new Date();
    d.setDate(today.getDate() - i);
    const k = dateKey(d);
    const hash = (d.getDate() * 7 + d.getMonth() * 11) % 5;
    if (hash !== 0) {
      marks[k] = {
        date: k,
        workout: hash > 1,
        macros: hash !== 2,
        intensity: ((hash > 3 ? 3 : hash) as 0 | 1 | 2 | 3) || 1,
      };
    }
  }

  const todaysSession = SAMPLE_HISTORY[0];

  return (
    <div className="container-x py-8 lg:py-12">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
            {today.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" })}
          </p>
          <h1 className="mt-1 font-display text-5xl tracking-brutal sm:text-7xl">Today</h1>
        </div>
        <div className="flex gap-2">
          <span className="pill"><Flame className="h-3 w-3" /> 12-day streak</span>
          <span className="pill"><Dumbbell className="h-3 w-3" /> 4 lifts queued</span>
        </div>
      </header>

      <section className="mt-10 grid gap-5 lg:grid-cols-3">
        <Link
          href="/app/workout"
          className="group relative overflow-hidden border border-bone-50/10 bg-ink-900/60 p-6 transition-colors hover:bg-ink-900"
        >
          <div className="flex items-start justify-between">
            <div>
              <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">/ Today's lift</p>
              <h2 className="mt-1 font-display text-3xl tracking-brutal">{todaysSession.title}</h2>
              <p className="mt-1 font-mono text-[10px] uppercase tracking-widest text-bone-300">
                {todaysSession.lifts.length} lifts · ~{todaysSession.durationMin}m
              </p>
            </div>
            <ArrowUpRight className="h-5 w-5 text-bone-300 transition-colors group-hover:text-bone-50" />
          </div>
          <ul className="mt-5 space-y-2 text-sm">
            {todaysSession.lifts.slice(0, 3).map((l) => (
              <li key={l.id} className="flex items-center justify-between border-b border-bone-50/10 py-2">
                <span className="text-bone-200">{l.name}</span>
                <span className="font-mono text-[11px] uppercase tracking-widest text-bone-300">
                  {l.sets.length} sets
                </span>
              </li>
            ))}
          </ul>
          <span className="mt-4 inline-flex font-mono text-[10px] uppercase tracking-widest text-bone-100">
            Open logger →
          </span>
        </Link>

        <Link
          href="/app/macros"
          className="group relative border border-bone-50/10 bg-ink-900/60 p-6 transition-colors hover:bg-ink-900"
        >
          <div className="flex items-start justify-between">
            <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">/ Macros</p>
            <ArrowUpRight className="h-5 w-5 text-bone-300 transition-colors group-hover:text-bone-50" />
          </div>
          <h2 className="mt-1 font-display text-3xl tracking-brutal">2,210 / {MACRO_TARGETS.kcal} kcal</h2>
          <div className="mt-5 grid grid-cols-3 gap-2 text-xs">
            {[
              { l: "P", v: 171, t: MACRO_TARGETS.protein },
              { l: "C", v: 242, t: MACRO_TARGETS.carbs },
              { l: "F", v: 62, t: MACRO_TARGETS.fat },
            ].map((m) => (
              <div key={m.l} className="border border-bone-50/10 bg-ink-950 p-3">
                <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">{m.l}</span>
                <div className="mt-1 font-display text-2xl tracking-brutal">{m.v}</div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-bone-300">/ {m.t}g</div>
              </div>
            ))}
          </div>
          <span className="mt-4 inline-flex font-mono text-[10px] uppercase tracking-widest text-bone-100">
            Log a meal →
          </span>
        </Link>

        <Link
          href="/app/progress"
          className="group relative border border-bone-50/10 bg-ink-900/60 p-6 transition-colors hover:bg-ink-900"
        >
          <div className="flex items-start justify-between">
            <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">/ Body</p>
            <ArrowUpRight className="h-5 w-5 text-bone-300 transition-colors group-hover:text-bone-50" />
          </div>
          <h2 className="mt-1 font-display text-3xl tracking-brutal">183.4 lb</h2>
          <p className="mt-1 font-mono text-[10px] uppercase tracking-widest text-bone-300">−1.2lb / 4 weeks</p>
          <div className="mt-5 flex aspect-[3/2] items-center justify-center border border-bone-50/10 bg-gradient-to-br from-ink-800 to-ink-900 text-bone-300">
            <ImagePlus className="h-6 w-6" />
          </div>
          <span className="mt-4 inline-flex font-mono text-[10px] uppercase tracking-widest text-bone-100">
            Log weight + photo →
          </span>
        </Link>
      </section>

      <section className="mt-10 grid gap-5 lg:grid-cols-[1.5fr_1fr]">
        <CalendarGrid marks={marks} />
        <div className="border border-bone-50/10 bg-ink-900/60 p-6">
          <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">/ Recovery radar</p>
          <h2 className="mt-1 font-display text-3xl tracking-brutal">Green light. Push the pull.</h2>
          <ul className="mt-5 space-y-3 text-sm">
            {[
              ["7-day tonnage", "+8.4%", "vs prior week"],
              ["Sleep avg", "7h 24m", "rolling 7d"],
              ["RPE drift", "+0.4", "fine — under +1.0"],
              ["Soreness", "Low (legs)", "self-reported"],
            ].map(([l, v, n]) => (
              <li key={l} className="flex items-center justify-between border-b border-bone-50/10 pb-3">
                <span className="text-bone-200">{l}</span>
                <span className="text-right">
                  <span className="block font-display text-xl tracking-brutal">{v}</span>
                  <span className="block font-mono text-[10px] uppercase tracking-widest text-bone-300">
                    {n}
                  </span>
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-5 text-sm text-bone-200">
            No deload signal this week. AI Coach holds prescribed jumps. Hydrate before the squat session.
          </p>
        </div>
      </section>
    </div>
  );
}
