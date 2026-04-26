"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, CalendarDays, Dumbbell, Flame, ImagePlus, Zap } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/app", label: "Today", icon: Activity },
  { href: "/app/workout", label: "Workout", icon: Dumbbell },
  { href: "/app/macros", label: "Macros", icon: Flame },
  { href: "/app/calendar", label: "Calendar", icon: CalendarDays },
  { href: "/app/progress", label: "Progress", icon: ImagePlus },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  return (
    <div className="relative flex min-h-screen flex-col bg-ink-950 lg:flex-row">
      {/* Sidebar (desktop) */}
      <aside className="sticky top-0 z-30 hidden h-screen w-64 shrink-0 flex-col border-r border-bone-50/10 bg-ink-950/80 px-6 py-7 backdrop-blur lg:flex">
        <Link href="/" className="flex items-center gap-2.5">
          <span className="inline-flex h-7 w-7 items-center justify-center border border-bone-50/20 bg-ink-900">
            <Zap className="h-3.5 w-3.5" />
          </span>
          <span className="font-display text-xl tracking-brutal">VOLTFORGE</span>
        </Link>
        <p className="mt-1 font-mono text-[10px] uppercase tracking-widest text-bone-300">
          Live preview · mock data
        </p>
        <nav className="mt-10 flex flex-1 flex-col gap-1" aria-label="App sections">
          {NAV.map((n) => {
            const active = path === n.href || (n.href !== "/app" && path.startsWith(n.href));
            return (
              <Link
                key={n.href}
                href={n.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative flex items-center gap-3 border border-transparent px-3 py-2.5 font-mono text-[12px] uppercase tracking-widest transition-colors",
                  active
                    ? "border-maroon-500/40 bg-maroon-900/30 text-bone-50"
                    : "text-bone-300 hover:border-bone-50/15 hover:bg-ink-900 hover:text-bone-50"
                )}
              >
                <n.icon className="h-3.5 w-3.5" />
                {n.label}
                {active && <span className="absolute -left-px top-0 h-full w-px bg-maroon-400" />}
              </Link>
            );
          })}
        </nav>

        <Link
          href="/pricing"
          className="mt-6 border border-bone-50/15 bg-ink-900 p-4 transition-colors hover:border-maroon-500/60"
        >
          <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-300">
            You're on Apprentice
          </p>
          <p className="mt-1 font-display text-lg tracking-brutal">Forge unlocks the AI Coach.</p>
          <span className="mt-3 inline-flex font-mono text-[10px] uppercase tracking-widest text-bone-200">
            Upgrade →
          </span>
        </Link>
      </aside>

      {/* Mobile top bar */}
      <header className="sticky top-0 z-30 flex items-center justify-between border-b border-bone-50/10 bg-ink-950/85 px-5 py-3 backdrop-blur lg:hidden">
        <Link href="/" className="flex items-center gap-2">
          <span className="inline-flex h-6 w-6 items-center justify-center border border-bone-50/20 bg-ink-900">
            <Zap className="h-3 w-3" />
          </span>
          <span className="font-display text-base tracking-brutal">VOLTFORGE</span>
        </Link>
        <span className="pill">Live preview</span>
      </header>

      <main className="flex-1 pb-28 lg:pb-12">{children}</main>

      {/* Mobile bottom nav */}
      <nav
        className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-bone-50/10 bg-ink-950/95 backdrop-blur lg:hidden"
        aria-label="App sections"
      >
        {NAV.map((n) => {
          const active = path === n.href || (n.href !== "/app" && path.startsWith(n.href));
          return (
            <Link
              key={n.href}
              href={n.href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "relative flex flex-col items-center justify-center gap-1 py-3 font-mono text-[9px] uppercase tracking-widest",
                active ? "text-bone-50" : "text-bone-300"
              )}
            >
              <n.icon className="h-4 w-4" />
              {n.label}
              {active && <span className="absolute inset-x-6 top-0 h-px bg-maroon-400" />}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
