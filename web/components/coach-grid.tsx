"use client";

import { motion } from "framer-motion";
import { COACHES } from "@/lib/data";

export function CoachGrid() {
  return (
    <section id="team" className="container-x relative py-24 sm:py-32">
      <p className="eyebrow">/ Coaches</p>
      <h2 className="mt-3 max-w-3xl font-display text-5xl tracking-brutal sm:text-7xl">
        Built by people who lift.
      </h2>
      <p className="mt-4 max-w-2xl text-base text-bone-200">
        Every algorithm in VOLTFORGE was tuned by a working coach with a barbell in their hand. The AI is
        opinionated because the people behind it are.
      </p>

      <div className="mt-12 grid gap-px overflow-hidden border border-bone-50/10 bg-bone-50/10 sm:grid-cols-2 lg:grid-cols-4">
        {COACHES.map((c, i) => (
          <motion.article
            key={c.name}
            initial={{ opacity: 0, y: 10 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-60px" }}
            transition={{ duration: 0.4, delay: i * 0.05 }}
            className="group relative flex flex-col bg-ink-950 p-7"
          >
            <div className="aspect-square w-full border border-bone-50/10 bg-gradient-to-br from-ink-800 to-ink-900">
              <div className="flex h-full w-full items-center justify-center font-display text-7xl tracking-brutal text-bone-50/30">
                {c.name.split(" ").map((p) => p[0]).join("")}
              </div>
            </div>
            <h3 className="mt-5 font-display text-2xl tracking-brutal">{c.name}</h3>
            <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">{c.role}</p>
            <p className="mt-3 text-sm text-bone-200">{c.bio}</p>
            <ul className="mt-5 flex flex-wrap gap-1.5">
              {c.creds.map((cr) => (
                <li key={cr} className="border border-bone-50/10 bg-ink-900 px-2 py-1 font-mono text-[10px] uppercase tracking-widest text-bone-200">
                  {cr}
                </li>
              ))}
            </ul>
          </motion.article>
        ))}
      </div>
    </section>
  );
}
