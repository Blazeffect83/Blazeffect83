import type { Metadata, Viewport } from "next";
import { Inter, Bebas_Neue, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const sans = Inter({ subsets: ["latin"], variable: "--font-sans", display: "swap" });
const display = Bebas_Neue({ subsets: ["latin"], weight: "400", variable: "--font-display", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = {
  metadataBase: new URL("https://voltforge.app"),
  title: { default: "VOLTFORGE — Forge under the storm.", template: "%s · VOLTFORGE" },
  description:
    "An all-in-one training app for gym rats: macro tracking, set-by-set logging, AI-driven progressive overload, recovery-aware programming and a consistency calendar.",
  applicationName: "VOLTFORGE",
  keywords: [
    "gym app",
    "macro tracker",
    "workout logger",
    "progressive overload",
    "AI training coach",
    "hypertrophy",
    "powerlifting",
  ],
  openGraph: {
    title: "VOLTFORGE — Forge under the storm.",
    description: "Macros, sets, and an AI coach that calls your next jump. Built for gym rats.",
    type: "website",
  },
  twitter: { card: "summary_large_image", title: "VOLTFORGE", description: "Forge under the storm." },
};

export const viewport: Viewport = {
  themeColor: "#07060a",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${display.variable} ${mono.variable}`}>
      <body className="relative min-h-screen bg-ink-950 text-bone-50 antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-maroon-600 focus:px-3 focus:py-2 focus:text-bone-50"
        >
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
