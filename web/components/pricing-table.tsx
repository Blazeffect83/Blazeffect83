"use client";

import { useState } from "react";
import Link from "next/link";
import { Check, Zap } from "lucide-react";
import { motion } from "framer-motion";
import { TIERS } from "@/lib/data";
import { cn } from "@/lib/utils";

export function PricingTable() {
  const [annual, setAnnual] = useState(true);

  return (
    <section id="pricing" className="container-x relative py-24 sm:py-32">
      <div className="flex flex-col items-start justify-between gap-6 sm:flex-row sm:items-end">
        <div>
          <p className="eyebrow">/ 03 — Pricing</p>
          <h2 className="mt-3 max-w-3xl font-display text-5xl tracking-brutal sm:text-7xl">
            One price. No coach upsells.
          </h2>
        </div>

        <div
          role="tablist"
          aria-label="Billing period"
          className="inline-flex items-center border border-bone-50/15 bg-ink-900 p-1 font-mono text-[10px] uppercase tracking-widest"
        >
          <button
            role="tab"
            aria-selected={!annual}
            onClick={() => setAnnual(false)}
            className={cn("px-3 py-2 transition-colors", !annual ? "bg-maroon-700 text-bone-50" : "text-bone-300 hover:text-bone-50")}
          >
            Monthly
          </button>
          <button
            role="tab"
            aria-selected={annual}
            onClick={() => setAnnual(true)}
            className={cn("px-3 py-2 transition-colors", annual ? "bg-maroon-700 text-bone-50" : "text-bone-300 hover:text-bone-50")}
          >
            Annual <span className="ml-1 text-maroon-300">−25%</span>
          </button>
        </div>
      </div>

      <div className="mt-12 grid gap-px overflow-hidden border border-bone-50/10 bg-bone-50/10 lg:grid-cols-3">
        {TIERS.map((t, i) => (
          <motion.article
            key={t.id}
            initial={{ opacity: 0, y: 14 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-60px" }}
            transition={{ duration: 0.45, delay: i * 0.05 }}
            className={cn(
              "relative flex flex-col bg-ink-950 p-8",
              t.highlight && "bg-gradient-to-b from-maroon-900/30 to-ink-950"
            )}
          >
            {t.highlight && (
              <span className="absolute right-6 top-6 inline-flex items-center gap-1 border border-maroon-500/60 bg-maroon-700 px-2 py-1 font-mono text-[10px] uppercase tracking-widest text-bone-50">
                <Zap className="h-3 w-3" /> Most lifters
              </span>
            )}

            <div>
              <h3 className="font-display text-4xl tracking-brutal">{t.name}</h3>
              <p className="mt-2 text-sm text-bone-300">{t.tagline}</p>
            </div>

            <div className="mt-6 flex items-baseline gap-2">
              <span className="font-display text-6xl tracking-brutal">
                {t.monthly === 0 ? "Free" : `$${annual ? Math.round((t.annual ?? 0) / 12) : t.monthly}`}
              </span>
              {t.monthly !== 0 && (
                <span className="font-mono text-[11px] uppercase tracking-widest text-bone-300">
                  / mo {annual && "· billed yearly"}
                </span>
              )}
            </div>
            {t.monthly !== 0 && annual && (
              <div className="mt-1 font-mono text-[10px] uppercase tracking-widest text-maroon-300">
                ${t.annual} / yr
              </div>
            )}

            <ul className="mt-8 space-y-3 text-sm">
              {t.bullets.map((b) => (
                <li key={b} className="flex gap-3">
                  <Check className="mt-0.5 h-4 w-4 shrink-0 text-maroon-300" aria-hidden />
                  <span className="text-bone-200">{b}</span>
                </li>
              ))}
            </ul>

            <div className="mt-10">
              <Link
                href="/app"
                className={cn(
                  "inline-flex w-full items-center justify-center gap-2 border px-4 py-3 font-mono text-[11px] uppercase tracking-widest transition-colors",
                  t.highlight
                    ? "border-maroon-500/60 bg-maroon-700 text-bone-50 hover:bg-maroon-600"
                    : "border-bone-50/20 bg-ink-900 text-bone-50 hover:border-bone-50/40"
                )}
              >
                {t.cta}
              </Link>
            </div>
          </motion.article>
        ))}
      </div>

      <p className="mt-6 text-center font-mono text-[10px] uppercase tracking-widest text-bone-300">
        Prices in USD. Cancel any time. 14-day refund window on paid plans.
      </p>
    </section>
  );
}
