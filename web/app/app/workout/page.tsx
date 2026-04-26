import { WorkoutLogger } from "@/components/workout-logger";

export default function WorkoutPage() {
  return (
    <div className="container-x py-8 lg:py-12">
      <header className="mb-8">
        <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">/ Workout logger</p>
        <h1 className="mt-1 font-display text-5xl tracking-brutal sm:text-7xl">Lift & log</h1>
        <p className="mt-3 max-w-2xl text-bone-200">
          The AI Overload Coach reads your last session and prescribes the next one. Override anything you want
          — it won't take it personally.
        </p>
      </header>
      <WorkoutLogger />
    </div>
  );
}
