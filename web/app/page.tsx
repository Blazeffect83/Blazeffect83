import { CursorSpotlight } from "@/components/cursor-spotlight";
import { FAQ } from "@/components/faq";
import { FeatureGrid } from "@/components/feature-grid";
import { Hero } from "@/components/hero";
import { PricingTable } from "@/components/pricing-table";
import { Showcase } from "@/components/showcase";
import { SiteFooter } from "@/components/site-footer";
import { SiteNav } from "@/components/site-nav";
import { Testimonials } from "@/components/testimonials";
import { MagneticButton } from "@/components/magnetic-button";
import { Zap } from "lucide-react";

export default function HomePage() {
  return (
    <>
      <CursorSpotlight />
      <SiteNav />
      <main id="main" className="relative z-10">
        <Hero />
        <FeatureGrid />
        <Showcase />
        <Testimonials />
        <PricingTable />
        <FAQ />

        <section className="relative overflow-hidden border-t border-bone-50/10 bg-ink-900 py-28">
          <div
            aria-hidden
            className="absolute inset-0 bg-storm opacity-80"
          />
          <div className="container-x relative z-10 text-center">
            <p className="eyebrow justify-center">/ Last call</p>
            <h2 className="mx-auto mt-4 max-w-4xl font-display text-6xl tracking-brutal sm:text-8xl">
              The bar will be there. So will we.
            </h2>
            <p className="mx-auto mt-5 max-w-xl text-bone-200">
              Free forever on Apprentice. 14 days on Forged. No credit card to start.
            </p>
            <div className="mt-10 flex justify-center">
              <MagneticButton href="/app" variant="primary">
                <Zap className="h-3.5 w-3.5" /> Open the app
              </MagneticButton>
            </div>
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
