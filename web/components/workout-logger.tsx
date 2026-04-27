"use client";

import { useEffect, useMemo, useState } from "react";
import { Pause, Play, Plus, RotateCcw, Save, Trash2 } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { AISuggestion } from "./ai-suggestion";
import { SyncBadge } from "./sync-badge";
import { suggestNext, type OverloadGoal } from "@/lib/overload";
import { SAMPLE_HISTORY, type LiftEntry, type SetEntry } from "@/lib/data";
import { useSyncedState } from "@/lib/sync";
import { cn } from "@/lib/utils";

type Draft = {
  liftId: string;
  name: string;
  muscle: string;
  sets: SetEntry[];
};

const DEFAULT_LIFT = SAMPLE_HISTORY[0].lifts[0];

export function WorkoutLogger() {
  const [goal, setGoal] = useState<OverloadGoal>("hypertrophy");
  const [activeIndex, setActiveIndex] = useState(0);
  const [draft, setDraft, syncStatus] = useSyncedState<Draft | null>("workout-draft", null);
  const [restSeconds, setRestSeconds] = useState(0);
  const [restRunning, setRestRunning] = useState(false);

  const liftPool: LiftEntry[] = useMemo(() => {
    const seen = new Map<string, LiftEntry>();
    SAMPLE_HISTORY.forEach((s) => s.lifts.forEach((l) => seen.set(l.id, l)));
    return Array.from(seen.values());
  }, []);

  const active = liftPool[activeIndex] ?? DEFAULT_LIFT;
  const suggestion = useMemo(() => suggestNext(active, goal), [active, goal]);

  useEffect(() => {
    if (!restRunning) return;
    const t = window.setInterval(() => setRestSeconds((s) => s + 1), 1000);
    return () => window.clearInterval(t);
  }, [restRunning]);

  const startDraft = () => {
    setDraft({
      liftId: active.id,
      name: active.name,
      muscle: active.muscle,
      sets: suggestion.nextSets.length
        ? suggestion.nextSets.map((s) => ({ ...s }))
        : [{ reps: 8, weight: 95, rpe: 7 }],
    });
    setRestSeconds(0);
    setRestRunning(false);
  };

  const updateSet = (i: number, patch: Partial<SetEntry>) => {
    setDraft((d: Draft | null) =>
      d ? { ...d, sets: d.sets.map((s, idx) => (idx === i ? { ...s, ...patch } : s)) } : d
    );
  };

  const addSet = () => {
    setDraft((d: Draft | null) => {
      if (!d) return d;
      const last = d.sets[d.sets.length - 1] ?? { reps: 8, weight: 95, rpe: 7 };
      return { ...d, sets: [...d.sets, { ...last }] };
    });
  };

  const completeSet = (i: number) => {
    setRestSeconds(0);
    setRestRunning(true);
    if (typeof window !== "undefined" && "vibrate" in navigator) {
      try {
        navigator.vibrate?.(30);
      } catch {
        /* ignore */
      }
    }
  };

  const removeSet = (i: number) => {
    setDraft((d: Draft | null) => (d ? { ...d, sets: d.sets.filter((_, idx) => idx !== i) } : d));
  };

  const clearDraft = () => {
    setDraft(null);
  };

  return (
    <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
      <div className="space-y-6">
        <header className="flex flex-col gap-4 border border-bone-50/10 bg-ink-900/60 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">Today's lift</p>
            <h2 className="mt-1 font-display text-3xl tracking-brutal sm:text-4xl">{active.name}</h2>
            <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">{active.muscle}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <SyncBadge status={syncStatus} />
            <select
              aria-label="Switch lift"
              value={activeIndex}
              onChange={(e) => setActiveIndex(parseInt(e.target.value))}
              className="border border-bone-50/15 bg-ink-900 px-3 py-2 font-mono text-[11px] uppercase tracking-widest text-bone-100"
            >
              {liftPool.map((l, i) => (
                <option key={l.id} value={i}>
                  {l.name}
                </option>
              ))}
            </select>
            <select
              aria-label="Training goal"
              value={goal}
              onChange={(e) => setGoal(e.target.value as OverloadGoal)}
              className="border border-bone-50/15 bg-ink-900 px-3 py-2 font-mono text-[11px] uppercase tracking-widest text-bone-100"
            >
              <option value="hypertrophy">Hypertrophy</option>
              <option value="strength">Strength</option>
              <option value="peaking">Peaking</option>
            </select>
          </div>
        </header>

        <AISuggestion s={suggestion} lift={active.name} />

        <section className="border border-bone-50/10 bg-ink-900/60 p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-display text-2xl tracking-brutal">Log this session</h3>
            {!draft ? (
              <button
                onClick={startDraft}
                className="inline-flex items-center gap-2 border border-maroon-500/60 bg-maroon-700 px-3 py-2 font-mono text-[10px] uppercase tracking-widest text-bone-50 hover:bg-maroon-600"
              >
                <Plus className="h-3.5 w-3.5" /> Start with coach plan
              </button>
            ) : (
              <button
                onClick={clearDraft}
                className="inline-flex items-center gap-2 border border-bone-50/15 bg-ink-900 px-3 py-2 font-mono text-[10px] uppercase tracking-widest text-bone-200 hover:border-bone-50/30"
              >
                <RotateCcw className="h-3.5 w-3.5" /> Reset
              </button>
            )}
          </div>

          <AnimatePresence>
            {draft && (
              <motion.ul
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="mt-5 space-y-2"
              >
                {draft.sets.map((s, i) => (
                  <li
                    key={i}
                    className="grid grid-cols-[auto_1fr_1fr_1fr_auto_auto] items-center gap-3 border border-bone-50/10 bg-ink-950 p-3"
                  >
                    <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                      Set {i + 1}
                    </span>

                    <NumberStepper
                      label="Weight (lb)"
                      value={s.weight}
                      step={2.5}
                      onChange={(v) => updateSet(i, { weight: v })}
                    />
                    <NumberStepper
                      label="Reps"
                      value={s.reps}
                      step={1}
                      min={1}
                      onChange={(v) => updateSet(i, { reps: v })}
                    />
                    <RPESlider value={s.rpe} onChange={(v) => updateSet(i, { rpe: v })} />

                    <button
                      aria-label={`Mark set ${i + 1} complete`}
                      onClick={() => completeSet(i)}
                      className="inline-flex h-9 w-9 items-center justify-center border border-maroon-500/40 bg-maroon-900/40 hover:border-maroon-400 hover:bg-maroon-700"
                    >
                      <Save className="h-3.5 w-3.5" />
                    </button>
                    <button
                      aria-label={`Remove set ${i + 1}`}
                      onClick={() => removeSet(i)}
                      className="inline-flex h-9 w-9 items-center justify-center border border-bone-50/15 bg-ink-900 text-bone-300 hover:border-bone-50/30 hover:text-bone-50"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </li>
                ))}
                <li>
                  <button
                    onClick={addSet}
                    className="inline-flex items-center gap-2 border border-bone-50/15 bg-ink-950 px-3 py-2 font-mono text-[10px] uppercase tracking-widest text-bone-200 hover:border-bone-50/30 hover:text-bone-50"
                  >
                    <Plus className="h-3.5 w-3.5" /> Add set
                  </button>
                </li>
              </motion.ul>
            )}
          </AnimatePresence>

          {!draft && (
            <p className="mt-5 max-w-md text-sm text-bone-300">
              Tap <em>Start with coach plan</em> to load the AI's prescription and start logging. Your draft
              auto-saves to this device.
            </p>
          )}
        </section>
      </div>

      <aside className="space-y-6">
        <div className="border border-bone-50/10 bg-ink-900/60 p-5">
          <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">Rest timer</p>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="font-display text-6xl tracking-brutal">
              {String(Math.floor(restSeconds / 60)).padStart(2, "0")}:
              {String(restSeconds % 60).padStart(2, "0")}
            </span>
            <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">mm:ss</span>
          </div>
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => setRestRunning((r) => !r)}
              className="inline-flex items-center gap-2 border border-bone-50/15 bg-ink-900 px-3 py-2 font-mono text-[10px] uppercase tracking-widest hover:border-bone-50/30"
            >
              {restRunning ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              {restRunning ? "Pause" : "Start"}
            </button>
            <button
              onClick={() => setRestSeconds(0)}
              className="inline-flex items-center gap-2 border border-bone-50/15 bg-ink-900 px-3 py-2 font-mono text-[10px] uppercase tracking-widest hover:border-bone-50/30"
            >
              <RotateCcw className="h-3.5 w-3.5" /> Reset
            </button>
          </div>
          <ul className="mt-4 grid grid-cols-3 gap-2">
            {[60, 120, 180].map((t) => (
              <li key={t}>
                <button
                  onClick={() => {
                    setRestSeconds(t);
                    setRestRunning(true);
                  }}
                  className={cn(
                    "w-full border border-bone-50/15 bg-ink-950 px-2 py-2 font-mono text-[10px] uppercase tracking-widest text-bone-200 hover:border-bone-50/30 hover:text-bone-50"
                  )}
                >
                  {t / 60}:00
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="border border-bone-50/10 bg-ink-900/60 p-5">
          <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">Last session</p>
          <h3 className="mt-1 font-display text-2xl tracking-brutal">{active.name}</h3>
          <ul className="mt-3 space-y-2 text-sm">
            {active.sets.map((s, i) => (
              <li key={i} className="flex items-center justify-between border-b border-bone-50/10 py-2">
                <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                  Set {i + 1}
                </span>
                <span className="font-display text-lg tracking-brutal">
                  {s.weight} × {s.reps}
                </span>
                <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                  RPE {s.rpe}
                </span>
              </li>
            ))}
          </ul>
          {active.notes && <p className="mt-3 text-xs italic text-bone-300">{active.notes}</p>}
        </div>
      </aside>
    </div>
  );
}

function NumberStepper({
  label,
  value,
  step = 1,
  min = 0,
  onChange,
}: {
  label: string;
  value: number;
  step?: number;
  min?: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="block">
      <span className="block font-mono text-[9px] uppercase tracking-widest text-bone-300">{label}</span>
      <div className="mt-1 inline-flex items-center border border-bone-50/15 bg-ink-900">
        <button
          type="button"
          aria-label={`Decrease ${label}`}
          onClick={() => onChange(Math.max(min, +(value - step).toFixed(2)))}
          className="px-2 py-1.5 font-mono text-bone-300 hover:text-bone-50"
        >
          −
        </button>
        <input
          type="number"
          aria-label={label}
          value={value}
          step={step}
          min={min}
          onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
          className="w-16 bg-transparent px-2 py-1.5 text-center font-display text-lg tracking-brutal outline-none"
        />
        <button
          type="button"
          aria-label={`Increase ${label}`}
          onClick={() => onChange(+(value + step).toFixed(2))}
          className="px-2 py-1.5 font-mono text-bone-300 hover:text-bone-50"
        >
          +
        </button>
      </div>
    </label>
  );
}

function RPESlider({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <label className="block">
      <span className="block font-mono text-[9px] uppercase tracking-widest text-bone-300">
        RPE <span className="text-bone-100">{value.toFixed(1)}</span>
      </span>
      <input
        type="range"
        min={5}
        max={10}
        step={0.5}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        aria-label="Rate of perceived exertion"
        className="mt-2 w-full accent-maroon-500"
      />
    </label>
  );
}
