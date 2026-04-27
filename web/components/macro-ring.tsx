"use client";

import { motion, useReducedMotion } from "framer-motion";
import { clamp } from "@/lib/utils";

export function MacroRing({
  value,
  target,
  label,
  unit = "g",
  size = 140,
  strokeWidth = 10,
}: {
  value: number;
  target: number;
  label: string;
  unit?: string;
  size?: number;
  strokeWidth?: number;
}) {
  const reduced = useReducedMotion();
  const r = (size - strokeWidth) / 2;
  const c = 2 * Math.PI * r;
  const pct = clamp(value / target, 0, 1.2);
  const dash = c * Math.min(pct, 1);
  const over = pct > 1;

  return (
    <div className="flex flex-col items-center" role="img" aria-label={`${label}: ${value}${unit} of ${target}${unit}`}>
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} className="rotate-[-90deg]">
          <circle cx={size / 2} cy={size / 2} r={r} stroke="rgba(247,245,241,0.08)" strokeWidth={strokeWidth} fill="none" />
          <motion.circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            stroke={over ? "#c63d57" : "#9a1c2e"}
            strokeWidth={strokeWidth}
            fill="none"
            strokeLinecap="round"
            strokeDasharray={c}
            initial={{ strokeDashoffset: c }}
            animate={{ strokeDashoffset: c - dash }}
            transition={reduced ? { duration: 0 } : { duration: 0.9, ease: "easeOut" }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <div className="font-display text-3xl tracking-brutal">{value}</div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-bone-300">/ {target}{unit}</div>
        </div>
      </div>
      <div className="mt-3 font-mono text-[10px] uppercase tracking-widest text-bone-300">{label}</div>
    </div>
  );
}
