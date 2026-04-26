import Link from "next/link";
import { Zap } from "lucide-react";

export function SiteFooter() {
  return (
    <footer className="relative border-t border-bone-50/10 bg-ink-950">
      <div className="container-x grid gap-12 py-16 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="inline-flex h-7 w-7 items-center justify-center border border-bone-50/20 bg-ink-900">
              <Zap className="h-3.5 w-3.5" />
            </span>
            <span className="font-display text-xl tracking-brutal">VOLTFORGE</span>
          </div>
          <p className="mt-4 max-w-sm text-sm leading-relaxed text-bone-300">
            An all-in-one training app for gym rats. Macros, sets, and an AI coach that calls your next jump.
            Built by people who lift.
          </p>
        </div>

        <div>
          <h3 className="font-mono text-[10px] uppercase tracking-widest text-bone-300">Product</h3>
          <ul className="mt-4 space-y-2 text-sm">
            <li><Link className="text-bone-200 hover:text-bone-50" href="/#features">Features</Link></li>
            <li><Link className="text-bone-200 hover:text-bone-50" href="/pricing">Pricing</Link></li>
            <li><Link className="text-bone-200 hover:text-bone-50" href="/app">Live preview</Link></li>
            <li><Link className="text-bone-200 hover:text-bone-50" href="/manifesto">Manifesto</Link></li>
          </ul>
        </div>

        <div>
          <h3 className="font-mono text-[10px] uppercase tracking-widest text-bone-300">Coaches</h3>
          <ul className="mt-4 space-y-2 text-sm">
            <li><Link className="text-bone-200 hover:text-bone-50" href="/manifesto#team">The team</Link></li>
            <li><Link className="text-bone-200 hover:text-bone-50" href="/manifesto#science">The science</Link></li>
            <li><Link className="text-bone-200 hover:text-bone-50" href="/manifesto#privacy">Your data</Link></li>
          </ul>
        </div>

        <div>
          <h3 className="font-mono text-[10px] uppercase tracking-widest text-bone-300">Reach us</h3>
          <ul className="mt-4 space-y-2 text-sm">
            <li><a className="text-bone-200 hover:text-bone-50" href="mailto:hello@voltforge.app">hello@voltforge.app</a></li>
            <li><a className="text-bone-200 hover:text-bone-50" href="mailto:support@voltforge.app">support@voltforge.app</a></li>
            <li className="text-bone-300">Built in Brooklyn, NY</li>
          </ul>
        </div>
      </div>

      <div className="border-t border-bone-50/10">
        <div className="container-x flex flex-col items-start justify-between gap-3 py-6 text-xs text-bone-300 sm:flex-row sm:items-center">
          <span className="font-mono uppercase tracking-widest">© {new Date().getFullYear()} Voltforge Labs, Inc.</span>
          <span className="font-mono uppercase tracking-widest">Forge under the storm.</span>
        </div>
      </div>
    </footer>
  );
}
