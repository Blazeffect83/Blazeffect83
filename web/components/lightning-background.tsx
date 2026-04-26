"use client";

import { motion, useReducedMotion } from "framer-motion";
import { LightningBolt } from "./lightning-bolt";

export function LightningBackground() {
  const reduced = useReducedMotion();
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden>
      <div className="absolute inset-0 bg-storm" />
      <div className="absolute inset-0 bg-noise opacity-[0.04] mix-blend-overlay" />

      <div className="absolute -left-10 top-0 opacity-60 glow-bolt">
        <LightningBolt width={260} height={520} seed={3} branches={2} />
      </div>
      <div className="absolute right-0 -top-8 opacity-40 glow-bolt">
        <LightningBolt width={300} height={620} seed={11} branches={3} duration={2.2} />
      </div>
      <div className="absolute left-1/3 top-24 opacity-25 glow-bolt">
        <LightningBolt width={220} height={420} seed={29} branches={1} duration={2.6} />
      </div>

      {!reduced && (
        <motion.div
          className="absolute inset-x-0 h-px bg-gradient-to-r from-transparent via-bone-50/30 to-transparent"
          initial={{ y: "-10%" }}
          animate={{ y: "110%" }}
          transition={{ duration: 9, repeat: Infinity, ease: "linear" }}
        />
      )}
    </div>
  );
}
