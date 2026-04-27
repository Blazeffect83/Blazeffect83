# VOLTFORGE

> Forge under the storm.

An all-in-one training app for gym rats — macros, set-by-set logging, AI-driven
progressive overload, recovery-aware programming, a consistency calendar, and a
progress photo journal. Dark / bone / maroon aesthetic with lightning effects.

## Stack

- **Next.js 14** (App Router) + **TypeScript**
- **Tailwind CSS** for the design system
- **Framer Motion** for the animation layer (hand-ported patterns from the
  21st.dev / Magic UI / Aceternity registries — no MCP tooling required)
- **Lucide React** for icons
- Mobile-first, accessible, `prefers-reduced-motion` honored across the app
- Live-preview app routes use `localStorage` for persistence (no backend)

## Routes

| Route            | What it is                                                |
| ---------------- | --------------------------------------------------------- |
| `/`              | Marketing landing — hero, features, showcase, pricing, FAQ |
| `/manifesto`     | Why VOLTFORGE exists, the science, privacy, the team      |
| `/pricing`       | Three-tier pricing + monthly/annual toggle                |
| `/app`           | Dashboard (today's lift, macros, body, calendar, recovery)|
| `/app/workout`   | Set logger + AI Overload Coach + rest timer               |
| `/app/macros`    | Macro tracker w/ ring viz + 12-item food DB demo          |
| `/app/calendar`  | Consistency calendar + 90-day heatmap + insights          |
| `/app/progress`  | Bodyweight + photo journal + side-by-side compare         |

## Run it (3 ways, easiest first)

### 1. GitHub Pages — zero local setup

Pushing to the `claude/gym-app-dark-aesthetic-RuGh1` branch (or `main`)
automatically builds and publishes the static export via the workflow at
`.github/workflows/voltforge-pages.yml`. You'll need to enable Pages once:
**Repo → Settings → Pages → Source = "GitHub Actions"**.

### 2. Static preview (one Node.js install required)

Builds the production export and serves it from a tiny zero-dep Node script.
This is the most reliable path locally — no dev server, no HMR weirdness.

```bash
cd web
npm install        # one-time
npm run build      # outputs to web/out
npm run preview    # serves web/out at http://localhost:3000
```

### 3. Live dev server (with HMR)

```bash
cd web
npm install
npm run dev        # http://localhost:3000
```

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `npm: command not found` | Install Node.js 20+ from https://nodejs.org |
| `Cannot find module 'next'` | You skipped `npm install`, or you're not in `web/` |
| `EADDRINUSE :::3000` | Something else is on 3000. `npm run preview -- 3001`, or for dev: `PORT=3001 npm run dev` |
| Blank page from `out/index.html` opened via `file://` | Browsers refuse to load JS from file:// for security. Use `npm run preview` (option 2). |
| Pages workflow doesn't run | Enable Pages in repo Settings, then re-run the workflow from the Actions tab. |

## Quality checks

```bash
npm run typecheck
npm run lint
npm run build
```

## Notes on the AI Coach

The "AI Overload Coach" is a deterministic, rule-based engine in
`lib/overload.ts`. It is honest about what it does — given a top set's
load × reps × RPE, the chosen training goal (strength / hypertrophy / peaking),
and the rep target, it computes:

1. An RPE-adjusted next-session load (~3% per RPE point).
2. Failure detection (RPE ≥ 9.5 with reps short of target → roll back 5lb).
3. Headroom detection (RPE ≤ 7 with reps in the bank → +5lb on top of the
   adjustment).
4. A rationale built from the Epley 1RM estimate.

Swapping in a model-backed coach later means replacing the body of
`suggestNext()` — no caller changes.

## Privacy & data in this preview

Workout drafts, macro logs, and progress entries are stored in your browser's
`localStorage` only. Photos are kept as object URLs that live until you close
the tab — they never leave your device.

## Accessibility

- Skip-to-content link
- Visible focus rings (`outline: 2px solid var(--maroon)`)
- ARIA labels on interactive controls + dialogs
- Color contrast ≥ AA on all text against bone/maroon backgrounds
- `prefers-reduced-motion` disables lightning loops, hero stagger, marquee
  scans, and magnetic button physics

## Aesthetic targets

- Color: black (`#07060a`) / bone (`#f7f5f1`) / maroon (`#9a1c2e`)
- Typography: Bebas Neue (display), Inter (sans), JetBrains Mono (mono)
- Lightning: midpoint-displacement SVG bolts with stroke-length animation, hue
  glow filters, and a maroon underglow
- Background: storm gradient + 6% noise + animated horizontal scan line

## License

UNLICENSED — internal preview build.
