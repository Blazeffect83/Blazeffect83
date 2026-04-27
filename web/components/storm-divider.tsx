"use client";

import { motion, useReducedMotion } from "framer-motion";

export function StormDivider({ label }: { label?: string }) {
  const reduced = useReducedMotion();
  return (
    <div className="relative my-16 flex items-center gap-4 sm:my-24" aria-hidden={!label}>
      <span className="h-px flex-1 bg-gradient-to-r from-transparent to-bone-50/15" />
      {label && (
        <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">{label}</span>
      )}
      <motion.span
        className="relative inline-block h-1.5 w-1.5 rounded-full bg-maroon-400"
        animate={reduced ? undefined : { boxShadow: ["0 0 0 rgba(154,28,46,0)", "0 0 18px rgba(154,28,46,0.9)", "0 0 0 rgba(154,28,46,0)"] }}
        transition={{ duration: 2.4, repeat: Infinity }}
      />
      <span className="h-px flex-1 bg-gradient-to-l from-transparent to-bone-50/15" />
    </div>
  );
}
