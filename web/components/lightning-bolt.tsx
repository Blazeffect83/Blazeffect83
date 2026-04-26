"use client";

import { useMemo } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";

type Point = { x: number; y: number };

function midpointDisplace(start: Point, end: Point, displacement: number, detail: number): Point[] {
  if (detail <= 0) return [start, end];
  const mid: Point = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const len = Math.hypot(dx, dy) || 1;
  const px = -dy / len;
  const py = dx / len;
  const offset = (Math.random() - 0.5) * displacement;
  const newMid: Point = { x: mid.x + px * offset, y: mid.y + py * offset };
  const left = midpointDisplace(start, newMid, displacement / 2, detail - 1);
  const right = midpointDisplace(newMid, end, displacement / 2, detail - 1);
  return [...left, ...right.slice(1)];
}

function toPath(points: Point[]) {
  return points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
}

export function LightningBolt({
  className,
  width = 300,
  height = 600,
  detail = 6,
  displacement = 80,
  seed = 1,
  branches = 2,
  loop = true,
  duration = 1.6,
}: {
  className?: string;
  width?: number;
  height?: number;
  detail?: number;
  displacement?: number;
  seed?: number;
  branches?: number;
  loop?: boolean;
  duration?: number;
}) {
  const reduced = useReducedMotion();

  const { mainPath, branchPaths } = useMemo(() => {
    let s = seed * 9301 + 49297;
    const rand = () => {
      s = (s * 9301 + 49297) % 233280;
      return s / 233280;
    };
    const origRand = Math.random;
    Math.random = rand;
    try {
      const start: Point = { x: width / 2, y: 0 };
      const end: Point = { x: width / 2 + (rand() - 0.5) * width * 0.35, y: height };
      const main = midpointDisplace(start, end, displacement, detail);
      const branchList: string[] = [];
      for (let b = 0; b < branches; b++) {
        const idx = Math.floor(rand() * (main.length - 2)) + 1;
        const origin = main[idx];
        const target: Point = {
          x: origin.x + (rand() - 0.5) * width * 0.6,
          y: origin.y + height * (0.18 + rand() * 0.22),
        };
        const branch = midpointDisplace(origin, target, displacement / 2, detail - 1);
        branchList.push(toPath(branch));
      }
      return { mainPath: toPath(main), branchPaths: branchList };
    } finally {
      Math.random = origRand;
    }
  }, [width, height, detail, displacement, seed, branches]);

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className={cn("pointer-events-none", className)}
      aria-hidden
    >
      <defs>
        <filter id={`glow-${seed}`} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <g filter={`url(#glow-${seed})`}>
        {branchPaths.map((d, i) => (
          <motion.path
            key={`b-${i}`}
            d={d}
            fill="none"
            stroke="rgba(247,245,241,0.55)"
            strokeWidth={1.2}
            strokeLinecap="round"
            initial={reduced ? { pathLength: 1, opacity: 0.35 } : { pathLength: 0, opacity: 0 }}
            animate={
              reduced
                ? { pathLength: 1, opacity: 0.35 }
                : { pathLength: [0, 1, 1, 0], opacity: [0, 0.8, 0.8, 0] }
            }
            transition={{
              duration,
              delay: 0.05 + i * 0.06,
              repeat: loop ? Infinity : 0,
              repeatDelay: 1.4 + Math.random(),
              times: [0, 0.4, 0.7, 1],
            }}
          />
        ))}
        <motion.path
          d={mainPath}
          fill="none"
          stroke="white"
          strokeWidth={1.6}
          strokeLinecap="round"
          initial={reduced ? { pathLength: 1, opacity: 0.6 } : { pathLength: 0, opacity: 0 }}
          animate={
            reduced
              ? { pathLength: 1, opacity: 0.6 }
              : { pathLength: [0, 1, 1, 0], opacity: [0, 1, 1, 0] }
          }
          transition={{
            duration,
            repeat: loop ? Infinity : 0,
            repeatDelay: 1.2 + Math.random(),
            times: [0, 0.35, 0.7, 1],
          }}
        />
        <motion.path
          d={mainPath}
          fill="none"
          stroke="rgba(154,28,46,0.7)"
          strokeWidth={4}
          strokeLinecap="round"
          initial={reduced ? { opacity: 0.25 } : { opacity: 0 }}
          animate={reduced ? { opacity: 0.25 } : { opacity: [0, 0.7, 0.7, 0] }}
          transition={{
            duration,
            repeat: loop ? Infinity : 0,
            repeatDelay: 1.2 + Math.random(),
            times: [0, 0.35, 0.7, 1],
          }}
        />
      </g>
    </svg>
  );
}
