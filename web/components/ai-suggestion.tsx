"use client";

import { Zap } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";
import type { Suggestion } from "@/lib/overload";

export function AISuggestion({ s, lift }: { s: Suggestion; lift: string }) {
  const reduced = useReducedMotion();
  return (
    <motion.aside
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45 }}
      className="relative overflow-hidden border border-maroon-500/40 bg-gradient-to-br from-maroon-900/40 to-ink-900 p-5"
      aria-live="polite"
    >
      <div className="flex items-center gap-2">
        <span className="inline-flex h-6 w-6 items-center justify-center border border-maroon-500/60 bg-maroon-700">
          <Zap className="h-3 w-3" />
        </span>
        <span className="font-mono text-[10px] uppercase tracking-widest text-maroon-200">
          AI Overload Coach · {lift}
        </span>
      </div>
      <div className="mt-3 font-display text-2xl tracking-brutal sm:text-3xl">{s.prescription}</div>
      <p className="mt-3 max-w-prose text-sm text-bone-200">{s.rationale}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {s.nextSets.map((set, i) => (
          <span
            key={i}
            className="border border-bone-50/15 bg-ink-900/80 px-2.5 py-1 font-mono text-[10px] uppercase tracking-widest text-bone-200"
          >
            Set {i + 1} · {set.weight}lb × {set.reps} @ RPE {set.rpe}
          </span>
        ))}
      </div>
      {!reduced && (
        <motion.span
          aria-hidden
          className="pointer-events-none absolute inset-y-0 left-0 w-px bg-gradient-to-b from-transparent via-bone-50 to-transparent"
          initial={{ x: 0, opacity: 0 }}
          animate={{ x: ["0%", "100%", "100%"], opacity: [0, 1, 0] }}
          transition={{ duration: 2.6, repeat: Infinity, ease: "easeInOut" }}
        />
      )}
    </motion.aside>
  );
}
