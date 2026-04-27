"use client";

import { useMemo } from "react";
import { cn } from "@/lib/utils";

export type DayMark = {
  date: string; // YYYY-MM-DD
  workout?: boolean;
  macros?: boolean; // hit at least 80% of macro targets
  intensity?: 0 | 1 | 2 | 3;
};

export function CalendarGrid({
  marks,
  monthOffset = 0,
}: {
  marks: Record<string, DayMark>;
  monthOffset?: number;
}) {
  const today = useMemo(() => new Date(), []);
  const month = useMemo(() => {
    const d = new Date(today.getFullYear(), today.getMonth() + monthOffset, 1);
    return d;
  }, [today, monthOffset]);

  const days = useMemo(() => {
    const first = new Date(month.getFullYear(), month.getMonth(), 1);
    const last = new Date(month.getFullYear(), month.getMonth() + 1, 0);
    const lead = first.getDay();
    const cells: (Date | null)[] = [];
    for (let i = 0; i < lead; i++) cells.push(null);
    for (let i = 1; i <= last.getDate(); i++) cells.push(new Date(month.getFullYear(), month.getMonth(), i));
    while (cells.length % 7 !== 0) cells.push(null);
    return cells;
  }, [month]);

  const todayKey = today.toISOString().slice(0, 10);

  return (
    <div className="border border-bone-50/10 bg-ink-900/60 p-5">
      <div className="flex items-center justify-between">
        <h3 className="font-display text-2xl tracking-brutal">
          {month.toLocaleDateString("en-US", { month: "long", year: "numeric" })}
        </h3>
        <div className="flex items-center gap-3 font-mono text-[10px] uppercase tracking-widest text-bone-300">
          <span className="flex items-center gap-1.5"><span className="h-2 w-2 bg-maroon-500" /> Lift</span>
          <span className="flex items-center gap-1.5"><span className="h-2 w-2 border border-bone-50/40" /> Macros</span>
        </div>
      </div>

      <div className="mt-5 grid grid-cols-7 gap-1.5 font-mono text-[10px] uppercase tracking-widest text-bone-300">
        {["S", "M", "T", "W", "T", "F", "S"].map((d, i) => (
          <div key={`h-${i}`} className="px-1 py-1">{d}</div>
        ))}
      </div>

      <div className="mt-1 grid grid-cols-7 gap-1.5">
        {days.map((d, i) => {
          if (!d) return <div key={`e-${i}`} className="aspect-square" />;
          const key = d.toISOString().slice(0, 10);
          const m = marks[key];
          const intensity = m?.intensity ?? 0;
          const bg = ["bg-ink-800", "bg-maroon-900", "bg-maroon-700", "bg-maroon-500"][intensity] ?? "bg-ink-800";
          const isToday = key === todayKey;
          return (
            <div
              key={key}
              className={cn(
                "relative aspect-square border border-bone-50/10 p-1.5 text-[10px]",
                bg,
                isToday && "outline outline-1 outline-bone-50"
              )}
              aria-label={`${d.toLocaleDateString()}${m?.workout ? ", workout logged" : ""}${m?.macros ? ", macros on target" : ""}`}
            >
              <span className="font-mono text-bone-300">{d.getDate()}</span>
              <div className="absolute bottom-1.5 left-1.5 flex items-center gap-1">
                {m?.workout && <span className="h-1.5 w-1.5 rounded-full bg-bone-50" />}
                {m?.macros && <span className="h-1.5 w-1.5 rounded-full border border-bone-50/70" />}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
