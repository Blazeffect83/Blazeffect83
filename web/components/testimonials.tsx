"use client";

import { motion } from "framer-motion";
import { TESTIMONIALS } from "@/lib/data";

export function Testimonials() {
  return (
    <section className="container-x relative py-24 sm:py-32">
      <div className="flex items-end justify-between gap-6">
        <div>
          <p className="eyebrow">/ 04 — Field reports</p>
          <h2 className="mt-3 max-w-3xl font-display text-5xl tracking-brutal sm:text-7xl">
            From the early-access cohort.
          </h2>
          <p className="mt-4 max-w-xl text-sm text-bone-300">
            Real quotes from beta lifters. Names abbreviated for privacy. We don't pay for reviews — we don't
            run ads either.
          </p>
        </div>
      </div>

      <div className="mt-12 grid gap-px overflow-hidden border border-bone-50/10 bg-bone-50/10 md:grid-cols-2">
        {TESTIMONIALS.map((t, i) => (
          <motion.figure
            key={t.name}
            initial={{ opacity: 0, y: 10 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-60px" }}
            transition={{ duration: 0.4, delay: i * 0.05 }}
            className="relative bg-ink-950 p-8"
          >
            <span className="absolute left-6 top-4 font-display text-7xl text-maroon-700/60 leading-none">
              &ldquo;
            </span>
            <blockquote className="relative mt-6 text-base leading-relaxed text-bone-100">
              {t.quote}
            </blockquote>
            <figcaption className="mt-6 flex items-center gap-3 border-t border-bone-50/10 pt-4">
              <span className="inline-flex h-9 w-9 items-center justify-center border border-bone-50/15 bg-ink-900 font-display text-lg">
                {t.name.charAt(0)}
              </span>
              <span className="text-sm">
                <span className="block font-medium text-bone-100">{t.name}</span>
                <span className="block font-mono text-[10px] uppercase tracking-widest text-bone-300">
                  {t.role}
                </span>
              </span>
            </figcaption>
          </motion.figure>
        ))}
      </div>
    </section>
  );
}
