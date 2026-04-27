"use client";

import { motion, useReducedMotion } from "framer-motion";
import { ArrowRight, Zap } from "lucide-react";
import { LightningBackground } from "./lightning-background";
import { MagneticButton } from "./magnetic-button";

export function Hero() {
  const reduced = useReducedMotion();
  return (
    <section className="relative isolate overflow-hidden pt-28 sm:pt-36">
      <LightningBackground />
      <div className="container-x relative z-10">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
          className="eyebrow"
        >
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-maroon-400 animate-pulse-ring" />
          v0.1 · Now in private beta
        </motion.div>

        <h1 className="mt-6 font-display text-[14vw] leading-[0.85] tracking-brutal sm:text-[120px] lg:text-[160px]">
          {"Forge".split("").map((c, i) => (
            <motion.span
              key={`a-${i}`}
              initial={{ opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5, delay: 0.05 * i }}
              className="inline-block"
            >
              {c}
            </motion.span>
          ))}{" "}
          <span className="relative inline-block text-maroon-400">
            under
            {!reduced && (
              <motion.span
                aria-hidden
                className="absolute -inset-1 -z-10 rounded-sm"
                initial={{ opacity: 0 }}
                animate={{ opacity: [0, 0.6, 0] }}
                transition={{ duration: 2.4, repeat: Infinity, ease: "easeInOut" }}
                style={{ background: "radial-gradient(closest-side, rgba(154,28,46,0.45), transparent)" }}
              />
            )}
          </span>
          <br />
          the{" "}
          <span className="relative inline-block">
            storm.
            <span className="absolute -bottom-2 left-0 h-1 w-full bg-maroon-500" />
          </span>
        </h1>

        <p className="mt-8 max-w-xl text-base leading-relaxed text-bone-200 sm:text-lg">
          Macros, sets, and an AI coach that calls your next jump. VOLTFORGE is the all-in-one app for gym
          rats who want progress they can see — not another habit tracker dressed in red.
        </p>

        <div className="mt-10 flex flex-wrap items-center gap-3">
          <MagneticButton href="/app" variant="primary">
            <Zap className="h-3.5 w-3.5" /> Try the app
          </MagneticButton>
          <MagneticButton href="/pricing" variant="ghost">
            See pricing <ArrowRight className="h-3.5 w-3.5" />
          </MagneticButton>
        </div>

        <div className="mt-16 grid grid-cols-2 gap-px overflow-hidden border border-bone-50/10 bg-bone-50/10 sm:grid-cols-4">
          {[
            { n: "11s", l: "median set log" },
            { n: "+1.8%", l: "weekly strength gain" },
            { n: "67d", l: "longest streak (beta)" },
            { n: "0", l: "ads, ever" },
          ].map((s) => (
            <div key={s.l} className="bg-ink-950 p-5 sm:p-6">
              <div className="font-display text-3xl tracking-brutal sm:text-4xl">{s.n}</div>
              <div className="mt-1 font-mono text-[10px] uppercase tracking-widest text-bone-300">{s.l}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="container-x relative z-10 mt-20 sm:mt-28">
        <div className="hairline" />
      </div>
    </section>
  );
}
