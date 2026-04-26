"use client";

import { motion } from "framer-motion";
import { FEATURES } from "@/lib/data";

export function FeatureGrid() {
  return (
    <section id="features" className="container-x relative py-24 sm:py-32">
      <div className="flex items-end justify-between gap-6">
        <div>
          <p className="eyebrow">/ 01 — Capabilities</p>
          <h2 className="mt-3 max-w-3xl font-display text-5xl tracking-brutal sm:text-7xl">
            Six tools. One app. Zero spreadsheets.
          </h2>
        </div>
      </div>

      <div className="mt-16 grid grid-cols-1 gap-px overflow-hidden border border-bone-50/10 bg-bone-50/10 md:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((f, i) => (
          <motion.article
            key={f.title}
            initial={{ opacity: 0, y: 12 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-80px" }}
            transition={{ duration: 0.45, delay: i * 0.04 }}
            className="group relative bg-ink-950 p-7 transition-colors duration-300 hover:bg-ink-900"
          >
            <span className="absolute right-7 top-7 font-mono text-[10px] uppercase tracking-widest text-bone-300">
              0{i + 1}
            </span>
            <h3 className="font-display text-3xl tracking-brutal text-bone-50">{f.title}</h3>
            <p className="mt-4 max-w-md text-sm leading-relaxed text-bone-200">{f.body}</p>
            <div className="mt-8 flex items-baseline gap-3 border-t border-bone-50/10 pt-5">
              <span className="font-display text-3xl tracking-brutal text-maroon-300">{f.metric}</span>
              <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                {f.metricLabel}
              </span>
            </div>

            <span className="pointer-events-none absolute inset-x-0 -top-px h-px bg-gradient-to-r from-transparent via-maroon-500/0 to-transparent transition-all duration-500 group-hover:via-maroon-500/80" />
          </motion.article>
        ))}
      </div>
    </section>
  );
}
