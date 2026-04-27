"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { LightningBolt } from "./lightning-bolt";

type Props = {
  href?: string;
  children: React.ReactNode;
  variant?: "primary" | "ghost" | "outline";
  className?: string;
  onClick?: () => void;
  type?: "button" | "submit";
  ariaLabel?: string;
};

export function MagneticButton({
  href,
  children,
  variant = "primary",
  className,
  onClick,
  type = "button",
  ariaLabel,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState(false);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const reduced = useReducedMotion();

  const onMove = (e: React.MouseEvent) => {
    if (reduced || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    const dx = e.clientX - (rect.left + rect.width / 2);
    const dy = e.clientY - (rect.top + rect.height / 2);
    setPos({ x: dx * 0.18, y: dy * 0.28 });
  };
  const onLeave = () => {
    setHover(false);
    setPos({ x: 0, y: 0 });
  };

  const base =
    "relative inline-flex select-none items-center justify-center gap-2 overflow-hidden border px-6 py-3.5 font-mono text-[12px] uppercase tracking-widest transition-colors duration-200";
  const variants = {
    primary: "border-maroon-500/60 bg-maroon-700 text-bone-50 hover:bg-maroon-600 hover:border-maroon-300",
    ghost: "border-bone-50/15 bg-transparent text-bone-50 hover:border-bone-50/40 hover:bg-ink-900",
    outline: "border-bone-50/25 bg-ink-900/40 text-bone-50 hover:border-bone-50/60",
  } as const;

  const inner = (
    <motion.div
      ref={ref}
      className={cn(base, variants[variant], className)}
      onMouseMove={onMove}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={onLeave}
      animate={{ x: pos.x, y: pos.y }}
      transition={{ type: "spring", stiffness: 220, damping: 18, mass: 0.4 }}
    >
      <span className="relative z-10 flex items-center gap-2">{children}</span>

      {/* Hover glow */}
      <motion.span
        aria-hidden
        className="pointer-events-none absolute inset-0"
        animate={{ opacity: hover ? 1 : 0 }}
        transition={{ duration: 0.25 }}
        style={{
          background:
            "radial-gradient(60% 80% at 50% 50%, rgba(247,245,241,0.18), transparent 70%)",
        }}
      />

      {/* Bolt overlay on hover */}
      {hover && !reduced && (
        <span className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-80 mix-blend-screen">
          <LightningBolt
            width={240}
            height={70}
            detail={5}
            displacement={20}
            seed={Math.floor(Math.random() * 1000)}
            branches={1}
            loop={false}
            duration={0.8}
          />
        </span>
      )}

      {/* Top + bottom traces */}
      <span className="pointer-events-none absolute left-0 right-0 top-0 h-px bg-gradient-to-r from-transparent via-bone-50/70 to-transparent opacity-60" />
      <span className="pointer-events-none absolute bottom-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-bone-50/30 to-transparent" />
    </motion.div>
  );

  if (href)
    return (
      <Link href={href} aria-label={ariaLabel} className="inline-block">
        {inner}
      </Link>
    );

  return (
    <button type={type} onClick={onClick} aria-label={ariaLabel} className="inline-block">
      {inner}
    </button>
  );
}
