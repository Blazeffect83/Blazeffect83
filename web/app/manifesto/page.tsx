import type { Metadata } from "next";
import { CursorSpotlight } from "@/components/cursor-spotlight";
import { CoachGrid } from "@/components/coach-grid";
import { SiteFooter } from "@/components/site-footer";
import { SiteNav } from "@/components/site-nav";
import { StormDivider } from "@/components/storm-divider";

export const metadata: Metadata = {
  title: "Manifesto",
  description: "Why VOLTFORGE exists, who builds it, and what we won't do.",
};

export default function ManifestoPage() {
  return (
    <>
      <CursorSpotlight />
      <SiteNav />
      <main id="main" className="relative z-10 pt-28">
        <article className="container-x">
          <p className="eyebrow">/ Manifesto</p>
          <h1 className="mt-3 max-w-4xl font-display text-6xl tracking-brutal sm:text-8xl">
            Train heavy. Eat on purpose. Recover like it matters.
          </h1>

          <div className="mt-12 grid gap-12 lg:grid-cols-[2fr_1fr]">
            <div className="prose prose-invert max-w-none text-base leading-relaxed text-bone-200">
              <p>
                Most fitness apps are built by product managers who saw a TikTok. They reward streaks for
                opening the app. They sell habit theatre. They cap your history behind a paywall and call it a
                <em> coach</em>.
              </p>
              <p className="mt-5">
                VOLTFORGE is built by people with calluses. We log every set we lift. We've coached real
                athletes through real plateaus. The app does what we'd do for you in person: read what you
                did last session, decide what to do next, then get out of your way until the bar is loaded.
              </p>
              <h2 id="science" className="mt-12 font-display text-4xl tracking-brutal">
                The science, plainly.
              </h2>
              <p className="mt-5">
                We fit a Hill-style fatigue-recovery model per lift, calibrated against your logged
                load × reps × RPE. The estimated 1RM curve drives prescriptions; the rolling slope of that
                curve drives deloads. Macros use a 14-day rolling maintenance estimate from logged weight and
                training tonnage — no calculator that pretends a 5-rep deadlift day burns the same as a rest
                day.
              </p>
              <p className="mt-5">
                The AI is opinionated. It will tell you to drop weight when you push too hard. It will tell
                you to add carbs when you under-fuel a heavy session. You can override it any time — but you
                will see the rationale, every time.
              </p>
              <h2 id="privacy" className="mt-12 font-display text-4xl tracking-brutal">
                Your data.
              </h2>
              <p className="mt-5">
                Workout logs and progress photos are end-to-end encrypted with a key derived from your
                passphrase. We can't read them. We don't sell anything to insurers, advertisers, or
                wholesalers of "wellness data." We don't ship trackers in the binary. We will publish our
                cryptography review when Forged exits beta.
              </p>
              <h2 className="mt-12 font-display text-4xl tracking-brutal">What we won't do.</h2>
              <ul className="mt-5 space-y-3">
                <li>Sell ads, ever.</li>
                <li>Gamify your bodyweight.</li>
                <li>Email you a streak guilt-trip.</li>
                <li>Pretend a chatbot is a coach.</li>
                <li>Charge for exporting your own data.</li>
              </ul>
            </div>

            <aside className="border border-bone-50/10 bg-ink-900/60 p-8">
              <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                / The principles
              </p>
              <ul className="mt-5 space-y-5">
                {[
                  ["Earned, not gamified", "Progress comes from load on the bar, not badges."],
                  ["Specific, not generic", "Per-lift curves. No global percentages applied to your back squat."],
                  ["Visible, not magical", "Every prescription shows its math."],
                  ["Private by default", "If we get hacked, the attackers get noise."],
                  ["Local-first", "The gym has bad wifi. The app doesn't care."],
                ].map(([t, b]) => (
                  <li key={t}>
                    <div className="font-display text-2xl tracking-brutal">{t}</div>
                    <p className="mt-1 text-sm text-bone-300">{b}</p>
                  </li>
                ))}
              </ul>
            </aside>
          </div>
        </article>

        <StormDivider label="The team" />
        <CoachGrid />
      </main>
      <SiteFooter />
    </>
  );
}
