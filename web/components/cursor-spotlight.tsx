"use client";

import { useEffect, useRef } from "react";
import { useReducedMotion } from "framer-motion";

export function CursorSpotlight({ size = 520 }: { size?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    if (reduced) return;
    let raf = 0;
    let tx = 0;
    let ty = 0;
    let cx = 0;
    let cy = 0;
    const onMove = (e: MouseEvent) => {
      tx = e.clientX;
      ty = e.clientY;
    };
    const tick = () => {
      cx += (tx - cx) * 0.12;
      cy += (ty - cy) * 0.12;
      if (ref.current) ref.current.style.transform = `translate3d(${cx - size / 2}px, ${cy - size / 2}px, 0)`;
      raf = requestAnimationFrame(tick);
    };
    window.addEventListener("mousemove", onMove);
    raf = requestAnimationFrame(tick);
    return () => {
      window.removeEventListener("mousemove", onMove);
      cancelAnimationFrame(raf);
    };
  }, [reduced, size]);

  if (reduced) return null;
  return (
    <div
      ref={ref}
      aria-hidden
      className="pointer-events-none fixed left-0 top-0 z-0 hidden md:block"
      style={{
        width: size,
        height: size,
        background:
          "radial-gradient(circle at center, rgba(154,28,46,0.18), rgba(154,28,46,0.05) 40%, transparent 70%)",
        filter: "blur(8px)",
        mixBlendMode: "screen",
      }}
    />
  );
}
