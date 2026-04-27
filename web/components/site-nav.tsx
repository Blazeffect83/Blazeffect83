"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { Menu, X, Zap } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/manifesto", label: "Manifesto" },
  { href: "/#features", label: "Features" },
  { href: "/pricing", label: "Pricing" },
  { href: "/app", label: "Try the app" },
];

export function SiteNav() {
  const [open, setOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const path = usePathname();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => setOpen(false), [path]);

  return (
    <header
      className={cn(
        "fixed inset-x-0 top-0 z-40 transition-colors duration-300",
        scrolled ? "border-b border-bone-50/10 bg-ink-950/80 backdrop-blur" : "bg-transparent"
      )}
    >
      <nav className="container-x flex h-16 items-center justify-between">
        <Link href="/" className="group flex items-center gap-2.5" aria-label="VOLTFORGE home">
          <span className="relative inline-flex h-7 w-7 items-center justify-center border border-bone-50/20 bg-ink-900">
            <Zap className="h-3.5 w-3.5 text-bone-50 transition-colors group-hover:text-maroon-300" />
            <span className="absolute -inset-px border border-maroon-500/0 transition-colors group-hover:border-maroon-500/40" />
          </span>
          <span className="font-display text-xl tracking-brutal text-bone-50">VOLTFORGE</span>
        </Link>

        <ul className="hidden items-center gap-8 md:flex">
          {LINKS.map((l) => (
            <li key={l.href}>
              <Link
                href={l.href}
                className="group relative inline-block font-mono text-[11px] uppercase tracking-widest text-bone-200 transition-colors hover:text-bone-50"
              >
                {l.label}
                <span className="absolute -bottom-1 left-0 h-px w-0 bg-maroon-400 transition-all duration-300 group-hover:w-full" />
              </Link>
            </li>
          ))}
        </ul>

        <div className="hidden md:block">
          <Link
            href="/app"
            className="inline-flex items-center gap-2 border border-maroon-500/60 bg-maroon-700 px-4 py-2 font-mono text-[11px] uppercase tracking-widest text-bone-50 transition-colors hover:bg-maroon-600"
          >
            <Zap className="h-3 w-3" /> Open app
          </Link>
        </div>

        <button
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex h-9 w-9 items-center justify-center border border-bone-50/15 bg-ink-900 md:hidden"
        >
          {open ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
        </button>
      </nav>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="overflow-hidden border-t border-bone-50/10 bg-ink-950/95 backdrop-blur md:hidden"
          >
            <ul className="container-x flex flex-col py-4">
              {LINKS.map((l) => (
                <li key={l.href}>
                  <Link
                    href={l.href}
                    className="block py-3 font-mono text-[12px] uppercase tracking-widest text-bone-200"
                  >
                    {l.label}
                  </Link>
                </li>
              ))}
              <li className="mt-2">
                <Link
                  href="/app"
                  className="inline-flex w-full items-center justify-center gap-2 border border-maroon-500/60 bg-maroon-700 px-4 py-3 font-mono text-[12px] uppercase tracking-widest text-bone-50"
                >
                  <Zap className="h-3 w-3" /> Open app
                </Link>
              </li>
            </ul>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  );
}
