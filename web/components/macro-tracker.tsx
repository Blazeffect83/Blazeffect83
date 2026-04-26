"use client";

import { useEffect, useMemo, useState } from "react";
import { Plus, Search, Trash2 } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { MacroRing } from "./macro-ring";
import { MACRO_TARGETS, SAMPLE_MEALS, type MealEntry } from "@/lib/data";
import { loadJSON, saveJSON } from "@/lib/storage";

const MEAL_KEY = "vf:meals";

const FOOD_DB: { name: string; per: string; kcal: number; protein: number; carbs: number; fat: number }[] = [
  { name: "Chicken breast", per: "100g", kcal: 165, protein: 31, carbs: 0, fat: 3.6 },
  { name: "White rice, cooked", per: "100g", kcal: 130, protein: 2.7, carbs: 28, fat: 0.3 },
  { name: "Whole egg", per: "1 large", kcal: 72, protein: 6.3, carbs: 0.4, fat: 4.8 },
  { name: "Greek yogurt 2%", per: "170g", kcal: 150, protein: 17, carbs: 9, fat: 4 },
  { name: "Whey protein", per: "1 scoop", kcal: 120, protein: 24, carbs: 3, fat: 1.5 },
  { name: "Banana", per: "1 medium", kcal: 105, protein: 1.3, carbs: 27, fat: 0.4 },
  { name: "Olive oil", per: "1 tbsp", kcal: 119, protein: 0, carbs: 0, fat: 13.5 },
  { name: "Sweet potato", per: "200g", kcal: 172, protein: 3.2, carbs: 40, fat: 0.2 },
  { name: "Salmon", per: "150g", kcal: 311, protein: 32, carbs: 0, fat: 19 },
  { name: "Almonds", per: "30g", kcal: 174, protein: 6.4, carbs: 6.1, fat: 15 },
  { name: "Oats, dry", per: "60g", kcal: 228, protein: 8.1, carbs: 41, fat: 4.0 },
  { name: "Spinach", per: "100g", kcal: 23, protein: 2.9, carbs: 3.6, fat: 0.4 },
];

export function MacroTracker() {
  const [meals, setMeals] = useState<MealEntry[]>([]);
  const [search, setSearch] = useState("");
  const [addOpen, setAddOpen] = useState(false);

  useEffect(() => {
    setMeals(loadJSON<MealEntry[]>(MEAL_KEY, SAMPLE_MEALS));
  }, []);
  useEffect(() => {
    if (meals.length) saveJSON(MEAL_KEY, meals);
  }, [meals]);

  const totals = useMemo(
    () =>
      meals.reduce(
        (acc, m) => ({
          kcal: acc.kcal + m.kcal,
          protein: acc.protein + m.protein,
          carbs: acc.carbs + m.carbs,
          fat: acc.fat + m.fat,
        }),
        { kcal: 0, protein: 0, carbs: 0, fat: 0 }
      ),
    [meals]
  );

  const remove = (id: string) => setMeals((m) => m.filter((x) => x.id !== id));
  const addFood = (food: (typeof FOOD_DB)[number]) => {
    setMeals((m) => [
      {
        id: `m${Date.now()}`,
        name: food.name,
        qty: food.per,
        kcal: Math.round(food.kcal),
        protein: Math.round(food.protein),
        carbs: Math.round(food.carbs),
        fat: Math.round(food.fat),
        meal: "Snack",
      },
      ...m,
    ]);
    setAddOpen(false);
    setSearch("");
  };

  const filtered = FOOD_DB.filter((f) => f.name.toLowerCase().includes(search.toLowerCase())).slice(0, 8);

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-widest text-bone-300">/ Macros</p>
          <h1 className="font-display text-5xl tracking-brutal sm:text-7xl">Today's fuel</h1>
        </div>
        <button
          onClick={() => setAddOpen(true)}
          className="inline-flex items-center gap-2 border border-maroon-500/60 bg-maroon-700 px-4 py-3 font-mono text-[11px] uppercase tracking-widest text-bone-50 hover:bg-maroon-600"
        >
          <Plus className="h-3.5 w-3.5" /> Log food
        </button>
      </header>

      <section className="grid grid-cols-2 gap-6 border border-bone-50/10 bg-ink-900/60 p-6 sm:grid-cols-4">
        <MacroRing value={totals.kcal} target={MACRO_TARGETS.kcal} label="Calories" unit="" />
        <MacroRing value={totals.protein} target={MACRO_TARGETS.protein} label="Protein" />
        <MacroRing value={totals.carbs} target={MACRO_TARGETS.carbs} label="Carbs" />
        <MacroRing value={totals.fat} target={MACRO_TARGETS.fat} label="Fat" />
      </section>

      <section>
        <h2 className="font-display text-3xl tracking-brutal">Logged today</h2>
        <ul className="mt-4 divide-y divide-bone-50/10 border-y border-bone-50/10">
          {meals.length === 0 && (
            <li className="py-8 text-center text-bone-300">No meals logged yet. Tap <em>Log food</em>.</li>
          )}
          {meals.map((m) => (
            <li key={m.id} className="grid grid-cols-[auto_1fr_auto] items-center gap-4 py-4">
              <span className="border border-bone-50/15 bg-ink-900 px-2.5 py-1 font-mono text-[10px] uppercase tracking-widest text-bone-300">
                {m.meal}
              </span>
              <div>
                <div className="font-display text-xl tracking-brutal">{m.name}</div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                  {m.qty} · {m.kcal} kcal · P{m.protein} / C{m.carbs} / F{m.fat}
                </div>
              </div>
              <button
                aria-label={`Remove ${m.name}`}
                onClick={() => remove(m.id)}
                className="inline-flex h-9 w-9 items-center justify-center border border-bone-50/15 text-bone-300 hover:border-maroon-400 hover:text-bone-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="border border-maroon-500/30 bg-gradient-to-br from-maroon-900/30 to-ink-900 p-6">
        <p className="font-mono text-[10px] uppercase tracking-widest text-maroon-200">/ Macro AI</p>
        <h3 className="mt-1 font-display text-2xl tracking-brutal">
          {totals.kcal < MACRO_TARGETS.kcal * 0.85
            ? "Under-fuelled for tomorrow's pull day."
            : totals.kcal > MACRO_TARGETS.kcal * 1.1
              ? "Over by a hair. Pull 100kcal at dinner if you'd like to stay on cut."
              : "On target. Hold the plan."}
        </h3>
        <p className="mt-3 max-w-prose text-sm text-bone-200">
          Targets recalibrate weekly from your logged bodyweight and 7-day training tonnage. Your maintenance
          estimate this week is{" "}
          <span className="font-mono text-bone-50">2,755 kcal</span> — a +180 kcal bump from last week's heavy
          tonnage block.
        </p>
      </section>

      <AnimatePresence>
        {addOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-end justify-center bg-ink-950/80 p-4 backdrop-blur sm:items-center"
            role="dialog"
            aria-modal
          >
            <motion.div
              initial={{ y: 30, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              exit={{ y: 30, opacity: 0 }}
              className="w-full max-w-md border border-bone-50/15 bg-ink-900 p-6"
            >
              <h3 className="font-display text-3xl tracking-brutal">Find a food</h3>
              <label className="mt-4 flex items-center gap-2 border border-bone-50/15 bg-ink-950 px-3 py-2">
                <Search className="h-4 w-4 text-bone-300" />
                <input
                  autoFocus
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Chicken, rice, oats..."
                  className="w-full bg-transparent text-sm outline-none placeholder:text-bone-300"
                  aria-label="Search foods"
                />
              </label>
              <ul className="mt-4 max-h-72 divide-y divide-bone-50/10 overflow-y-auto border-y border-bone-50/10">
                {filtered.map((f) => (
                  <li key={f.name}>
                    <button
                      onClick={() => addFood(f)}
                      className="flex w-full items-center justify-between gap-4 py-3 text-left hover:bg-ink-950/50"
                    >
                      <span>
                        <span className="block font-display text-lg tracking-brutal">{f.name}</span>
                        <span className="font-mono text-[10px] uppercase tracking-widest text-bone-300">
                          {f.per} · {f.kcal}kcal · P{f.protein} C{f.carbs} F{f.fat}
                        </span>
                      </span>
                      <Plus className="h-4 w-4 text-bone-300" />
                    </button>
                  </li>
                ))}
                {filtered.length === 0 && (
                  <li className="py-6 text-center text-sm text-bone-300">No matches. Try another term.</li>
                )}
              </ul>
              <div className="mt-4 flex justify-end">
                <button
                  onClick={() => setAddOpen(false)}
                  className="border border-bone-50/15 bg-ink-950 px-4 py-2 font-mono text-[10px] uppercase tracking-widest"
                >
                  Close
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
