"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { FAQ as FAQ_DATA } from "@/lib/data";

export function FAQ() {
  const [open, setOpen] = useState<number | null>(0);

  return (
    <section className="container-x relative py-24 sm:py-32">
      <div className="grid gap-12 lg:grid-cols-[1fr_2fr]">
        <div>
          <p className="eyebrow">/ 05 — Questions</p>
          <h2 className="mt-3 font-display text-5xl tracking-brutal sm:text-6xl">
            Honest answers.
          </h2>
          <p className="mt-4 max-w-sm text-sm leading-relaxed text-bone-200">
            Built by lifters, for lifters. If you have a question we haven't answered, write{" "}
            <a className="text-maroon-300 hover:underline" href="mailto:hello@voltforge.app">
              hello@voltforge.app
            </a>
            .
          </p>
        </div>

        <ul className="divide-y divide-bone-50/10 border-y border-bone-50/10">
          {FAQ_DATA.map((item, i) => {
            const isOpen = open === i;
            return (
              <li key={item.q}>
                <button
                  onClick={() => setOpen(isOpen ? null : i)}
                  aria-expanded={isOpen}
                  className="group flex w-full items-center justify-between gap-6 py-6 text-left"
                >
                  <span className="font-display text-2xl tracking-brutal sm:text-3xl">{item.q}</span>
                  <span
                    className={`inline-flex h-8 w-8 shrink-0 items-center justify-center border border-bone-50/20 transition-transform duration-300 group-hover:border-maroon-400 ${
                      isOpen ? "rotate-45 bg-maroon-700 border-maroon-500/60" : ""
                    }`}
                  >
                    <Plus className="h-3.5 w-3.5" />
                  </span>
                </button>
                <AnimatePresence initial={false}>
                  {isOpen && (
                    <motion.div
                      key="content"
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.25 }}
                      className="overflow-hidden"
                    >
                      <p className="pb-6 pr-12 text-sm leading-relaxed text-bone-200">{item.a}</p>
                    </motion.div>
                  )}
                </AnimatePresence>
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}
