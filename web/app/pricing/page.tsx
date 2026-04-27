import type { Metadata } from "next";
import { CursorSpotlight } from "@/components/cursor-spotlight";
import { FAQ } from "@/components/faq";
import { PricingTable } from "@/components/pricing-table";
import { SiteFooter } from "@/components/site-footer";
import { SiteNav } from "@/components/site-nav";

export const metadata: Metadata = {
  title: "Pricing",
  description: "Three plans. No coach upsells. Free forever on Apprentice.",
};

export default function PricingPage() {
  return (
    <>
      <CursorSpotlight />
      <SiteNav />
      <main id="main" className="relative z-10 pt-28">
        <div className="container-x">
          <p className="eyebrow">/ Pricing</p>
          <h1 className="mt-3 max-w-4xl font-display text-6xl tracking-brutal sm:text-8xl">
            Pay for the app. Not for the marketing.
          </h1>
          <p className="mt-5 max-w-xl text-bone-200">
            Apprentice covers most lifters indefinitely. Forged adds the AI Overload Coach and unlimited
            history. Legendary adds a human in the loop.
          </p>
        </div>
        <PricingTable />
        <FAQ />
      </main>
      <SiteFooter />
    </>
  );
}
