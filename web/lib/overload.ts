import type { LiftEntry, SetEntry } from "./data";

export type OverloadGoal = "strength" | "hypertrophy" | "peaking";

export type Suggestion = {
  prescription: string;
  rationale: string;
  nextSets: { reps: number; weight: number; rpe: number }[];
  delta: { weight: number; reps: number };
};

/**
 * Estimate 1RM via Epley.
 */
export function epley1RM(weight: number, reps: number) {
  if (reps <= 0) return 0;
  return weight * (1 + reps / 30);
}

/**
 * RPE-based load adjustment. Each 1 RPE ~= ~3% load shift.
 */
export function adjustForRPE(weight: number, lastRPE: number, targetRPE: number) {
  const diff = targetRPE - lastRPE;
  return Math.round((weight * (1 + diff * 0.03)) / 2.5) * 2.5;
}

/**
 * Get the "top set" — heaviest non-failed set as a representative.
 */
export function topSet(sets: SetEntry[]): SetEntry | null {
  if (!sets.length) return null;
  return sets.reduce((acc, s) => (s.weight > acc.weight ? s : acc), sets[0]);
}

/**
 * Generate next-session prescription for a given lift.
 */
export function suggestNext(lift: LiftEntry, goal: OverloadGoal = "hypertrophy"): Suggestion {
  const top = topSet(lift.sets);
  if (!top) {
    return {
      prescription: "Start with 3 sets at RPE 6 to calibrate.",
      rationale: "No prior data on this lift — establish a baseline before progression.",
      nextSets: [],
      delta: { weight: 0, reps: 0 },
    };
  }

  const targetReps = goal === "strength" ? 5 : goal === "peaking" ? 3 : 8;
  const targetRPE = goal === "peaking" ? 9 : 8;
  const targetSets = goal === "strength" ? 4 : goal === "peaking" ? 5 : 3;

  let nextWeight = adjustForRPE(top.weight, top.rpe, targetRPE);
  if (top.rpe <= 7 && top.reps >= targetReps) nextWeight = roundLoad(nextWeight + 5);
  if (top.rpe >= 9.5) nextWeight = roundLoad(top.weight - 5);

  const lastFailed = lift.sets.some((s) => s.rpe >= 9.5 && s.reps < targetReps);
  if (lastFailed) nextWeight = roundLoad(top.weight - 5);

  const nextSets = Array.from({ length: targetSets }, () => ({
    reps: targetReps,
    weight: nextWeight,
    rpe: targetRPE,
  }));

  const dWeight = nextWeight - top.weight;
  const rationale = buildRationale(lift, top, goal, dWeight, lastFailed);

  return {
    prescription: `${targetSets}×${targetReps} @ ${nextWeight}lb · target RPE ${targetRPE}`,
    rationale,
    nextSets,
    delta: { weight: dWeight, reps: targetReps - top.reps },
  };
}

function buildRationale(lift: LiftEntry, top: SetEntry, goal: OverloadGoal, dWeight: number, failed: boolean) {
  const lastE1RM = epley1RM(top.weight, top.reps).toFixed(0);
  if (failed) {
    return `Last session ended at RPE ≥ 9.5 with reps short of target — pull load back ${Math.abs(dWeight)}lb and re-bank a clean set. Estimated 1RM: ${lastE1RM}lb.`;
  }
  if (dWeight > 0) {
    return `Top set was ${top.weight}lb × ${top.reps} @ RPE ${top.rpe}. With ${goal} target RPE ${goal === "peaking" ? 9 : 8}, you have headroom to add ${dWeight}lb. Estimated 1RM: ${lastE1RM}lb.`;
  }
  if (dWeight < 0) {
    return `Top set was hot at RPE ${top.rpe}. Trimming ${Math.abs(dWeight)}lb to keep sets in the productive RPE band. Estimated 1RM: ${lastE1RM}lb.`;
  }
  return `Sitting in the productive band. Hold load, push reps or tempo. Estimated 1RM: ${lastE1RM}lb.`;
}

function roundLoad(w: number) {
  return Math.round(w / 2.5) * 2.5;
}
