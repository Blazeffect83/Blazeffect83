export type Tier = {
  id: "apprentice" | "forged" | "legendary";
  name: string;
  tagline: string;
  monthly: number | null;
  annual: number | null;
  bullets: string[];
  cta: string;
  highlight?: boolean;
};

export const TIERS: Tier[] = [
  {
    id: "apprentice",
    name: "Apprentice",
    tagline: "Pick up the bar.",
    monthly: 0,
    annual: 0,
    bullets: [
      "Unlimited workout logging — sets, reps, RPE, tempo",
      "Macro & calorie tracker with a 2,000-food starter library",
      "7-day rolling history",
      "1 AI program template (Push/Pull/Legs or Upper/Lower)",
      "Consistency calendar for the last 30 days",
    ],
    cta: "Start free",
  },
  {
    id: "forged",
    name: "Forged",
    tagline: "Train with current.",
    monthly: 9.99,
    annual: 89,
    highlight: true,
    bullets: [
      "Everything in Apprentice",
      "AI Overload Coach — next-set load, reps, and rest, tuned to your last RPE",
      "Recovery-aware deload detection (volume, RPE, sleep input)",
      "Unlimited progress photos with side-by-side compare",
      "Full-history calendar + streak heatmap",
      "Macro AI: targets recalibrate weekly from logged weight & training load",
      "Apple Health / Google Fit / Garmin import",
    ],
    cta: "Start 14-day trial",
  },
  {
    id: "legendary",
    name: "Legendary",
    tagline: "Forge what doesn't exist yet.",
    monthly: 19.99,
    annual: 179,
    bullets: [
      "Everything in Forged",
      "Custom AI program builder (block periodization, conjugate, RP-style hypertrophy)",
      "Form-check video review — frame-by-frame bar path overlay",
      "1-on-1 monthly check-in with a human coach",
      "Priority support, beta features, and multi-device sync",
      "Export to CSV / PDF for coaches and physios",
    ],
    cta: "Go Legendary",
  },
];

export const FEATURES = [
  {
    title: "AI Overload Coach",
    body: "Logs your set, reads the RPE, and prescribes the next jump. No spreadsheets, no guessing the 2.5lb plate game.",
    metric: "+1.8% strength / week",
    metricLabel: "median, beta cohort",
  },
  {
    title: "Macro Tracker, Lifted",
    body: "Targets shift with your training week. Hard pull day moves carbs; deload moves them back. Scan, log, done.",
    metric: "11s",
    metricLabel: "median time to log a meal",
  },
  {
    title: "Set-by-Set Logger",
    body: "Tap to log. Hold to autofill last session. RPE slider, tempo, rest timer with a haptic that hits like a bell.",
    metric: "0 ads",
    metricLabel: "ever",
  },
  {
    title: "Consistency Calendar",
    body: "Every logged session and every macro day fills a cell. Miss a week — see it. Stack a streak — feel it.",
    metric: "67-day",
    metricLabel: "longest current streak",
  },
  {
    title: "Progress Journal",
    body: "Snap a photo when you log bodyweight. Compare any two weeks side-by-side. Mirror flex optional.",
    metric: "Private",
    metricLabel: "end-to-end encrypted",
  },
  {
    title: "Recovery Radar",
    body: "Sleep, soreness, and rolling tonnage feed a deload signal. The bar will still be there tomorrow.",
    metric: "Auto-deload",
    metricLabel: "every 4–6 weeks",
  },
];

export const COACHES = [
  {
    name: "Marcus Vale",
    role: "Strength & Powerlifting",
    bio: "10 years coaching raw lifters. 705 squat, 525 bench, 745 deadlift. Tunes the AI's strength block.",
    creds: ["USAPL coach", "PhD-c biomechanics"],
  },
  {
    name: "Naomi Reyes",
    role: "Hypertrophy & Physique",
    bio: "Prepped 40+ amateur and pro physique athletes. Owns the volume curves under the hood.",
    creds: ["IFBB pro coach", "RDN"],
  },
  {
    name: "Devon Cross",
    role: "Mobility & Recovery",
    bio: "Former D1 strength assistant. Designs the deload detector and warm-up flows.",
    creds: ["DPT", "FRCms"],
  },
  {
    name: "Imani Ojo",
    role: "Nutrition & Macros",
    bio: "Sport dietitian. Built the macro AI's autoregulation around real-world adherence, not lab math.",
    creds: ["MSc Sports Nutrition", "CISSN"],
  },
];

export const FAQ = [
  {
    q: "Is my data actually private?",
    a: "Yes. Workout logs and progress photos are end-to-end encrypted with a key derived from your passphrase. We can't read them, and neither can a subpoena to us. Macros use server-side aggregation only for the food database — your daily logs stay client-encrypted.",
  },
  {
    q: "How does the AI Overload Coach actually work?",
    a: "It's not a chatbot. We fit a per-lift performance curve from your logged sets (load × reps × RPE), detect plateaus with a rolling slope test, and prescribe the next session based on your stated goal (strength / hypertrophy / peaking). You see the math — the prescription, why it changed, and what would change if you swapped goals.",
  },
  {
    q: "Will it work offline at the gym?",
    a: "Yes. Logging, rest timer, AI suggestions for already-loaded lifts, macros, and calendar are fully offline. Sync happens on next connection. The app weighs ~3MB after first install.",
  },
  {
    q: "Do I need a smartwatch?",
    a: "No. A watch (Apple, Garmin, Whoop, Pixel) makes recovery signals sharper, but nothing in the app is gated behind one. You can also self-report sleep and soreness in 5 seconds.",
  },
  {
    q: "What if I follow a custom program from a coach?",
    a: "Import it as CSV or paste it in. The AI Coach steps back into 'logger mode' but still flags fatigue and macro mismatches. Your coach can pull a CSV or PDF export anytime.",
  },
  {
    q: "Is there a free plan?",
    a: "Yes. Apprentice is free forever — full logging, basic macros, 7 days of history, and one AI template. Most lifters can run on it for months before needing Forged.",
  },
  {
    q: "Refunds?",
    a: "Cancel any time. If you cancel within 14 days of a paid charge we refund it, no email-tag.",
  },
];

export const TESTIMONIALS = [
  {
    quote:
      "I've been on the same 315 bench for two years. Three weeks of letting the coach prescribe my jumps and I hit 325 for a clean triple. Not a hype pump — a real PR.",
    name: "Jamie T.",
    role: "Early access, hypertrophy block",
  },
  {
    quote:
      "The macro tracker is the first one I've actually kept open past day 4. The fact that it shifts my carbs on heavy days without me asking is the whole game.",
    name: "Priya M.",
    role: "Early access, cut phase",
  },
  {
    quote:
      "Saw the calendar fill up for 41 straight days and realized I haven't been that consistent since college ball.",
    name: "Will K.",
    role: "Early access, general strength",
  },
  {
    quote:
      "Logging takes 9 seconds a set. Rest timer with the haptic is unreasonably satisfying. Built by people who actually lift.",
    name: "Sasha R.",
    role: "Early access, powerlifting",
  },
];

export const STATS = [
  { num: "11s", label: "median set log time" },
  { num: "1.8%", label: "median weekly strength gain (beta)" },
  { num: "94%", label: "of users hit ≥4 sessions / week" },
  { num: "0", label: "ads, trackers, or sold data" },
];

// Sample workout data for the app preview.
export type SetEntry = { reps: number; weight: number; rpe: number };
export type LiftEntry = {
  id: string;
  name: string;
  muscle: string;
  sets: SetEntry[];
  notes?: string;
};
export type SessionEntry = {
  date: string; // YYYY-MM-DD
  title: string;
  durationMin: number;
  lifts: LiftEntry[];
};

export const SAMPLE_HISTORY: SessionEntry[] = [
  {
    date: offsetDate(-2),
    title: "Push A — Heavy",
    durationMin: 64,
    lifts: [
      {
        id: "bb-bench",
        name: "Barbell Bench Press",
        muscle: "Chest",
        sets: [
          { reps: 5, weight: 225, rpe: 7 },
          { reps: 5, weight: 235, rpe: 8 },
          { reps: 5, weight: 235, rpe: 8.5 },
          { reps: 4, weight: 235, rpe: 9.5 },
        ],
        notes: "Bar path drifted on set 4 — drop 5lb if RPE 9+ next time.",
      },
      {
        id: "ohp",
        name: "Standing Overhead Press",
        muscle: "Shoulders",
        sets: [
          { reps: 6, weight: 135, rpe: 7 },
          { reps: 6, weight: 140, rpe: 8 },
          { reps: 5, weight: 140, rpe: 9 },
        ],
      },
      {
        id: "dips",
        name: "Weighted Dips",
        muscle: "Chest/Tri",
        sets: [
          { reps: 10, weight: 45, rpe: 7 },
          { reps: 9, weight: 45, rpe: 8 },
          { reps: 8, weight: 45, rpe: 9 },
        ],
      },
    ],
  },
  {
    date: offsetDate(-4),
    title: "Pull A — Volume",
    durationMin: 71,
    lifts: [
      {
        id: "bb-row",
        name: "Pendlay Row",
        muscle: "Back",
        sets: [
          { reps: 8, weight: 185, rpe: 7 },
          { reps: 8, weight: 195, rpe: 8 },
          { reps: 8, weight: 195, rpe: 8.5 },
        ],
      },
      {
        id: "pullups",
        name: "Weighted Pull-ups",
        muscle: "Back",
        sets: [
          { reps: 8, weight: 25, rpe: 7 },
          { reps: 7, weight: 25, rpe: 8 },
          { reps: 6, weight: 25, rpe: 9 },
        ],
      },
    ],
  },
  {
    date: offsetDate(-7),
    title: "Legs — Squat focus",
    durationMin: 78,
    lifts: [
      {
        id: "bb-squat",
        name: "Back Squat",
        muscle: "Quads",
        sets: [
          { reps: 5, weight: 295, rpe: 7 },
          { reps: 5, weight: 315, rpe: 8 },
          { reps: 4, weight: 315, rpe: 9 },
        ],
      },
      {
        id: "rdl",
        name: "Romanian Deadlift",
        muscle: "Hams",
        sets: [
          { reps: 8, weight: 245, rpe: 7 },
          { reps: 8, weight: 245, rpe: 8 },
        ],
      },
    ],
  },
];

export const MACRO_TARGETS = { kcal: 2750, protein: 195, carbs: 320, fat: 75 };

export type MealEntry = {
  id: string;
  name: string;
  qty: string;
  kcal: number;
  protein: number;
  carbs: number;
  fat: number;
  meal: "Breakfast" | "Lunch" | "Dinner" | "Snack";
};

export const SAMPLE_MEALS: MealEntry[] = [
  { id: "m1", name: "Greek yogurt, blueberries, oats", qty: "1 bowl", kcal: 480, protein: 38, carbs: 62, fat: 8, meal: "Breakfast" },
  { id: "m2", name: "Chicken thigh rice bowl", qty: "1 bowl", kcal: 720, protein: 55, carbs: 80, fat: 18, meal: "Lunch" },
  { id: "m3", name: "Whey + banana", qty: "1 shake", kcal: 320, protein: 30, carbs: 40, fat: 4, meal: "Snack" },
  { id: "m4", name: "Salmon, sweet potato, spinach", qty: "1 plate", kcal: 690, protein: 48, carbs: 70, fat: 22, meal: "Dinner" },
];

function offsetDate(days: number) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}
