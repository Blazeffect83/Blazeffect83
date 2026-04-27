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

## Run it

### Raspberry Pi 24/7 (recommended)

One command, idempotent. Installs Node 20, build deps, clones the repo,
builds, creates `/var/lib/voltforge` for the SQLite DB and photo storage,
installs a systemd unit (`voltforge.service`), and drops the `voltforge`
operator CLI in `/usr/local/bin`.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Blazeffect83/Blazeffect83/claude/gym-app-dark-aesthetic-RuGh1/deploy/install-on-pi.sh)
```

After it finishes:

```bash
voltforge url           # show LAN + Tailscale URLs
voltforge status        # systemd status
voltforge logs          # tail journalctl
voltforge update        # git pull + npm ci + build + restart
voltforge backup        # snapshot the SQLite db
voltforge db            # open the db in sqlite3
```

For HTTPS reachable from any device, add Tailscale:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg http://localhost:3000
```

You'll then reach the app at `https://<pi-name>.<your-tailnet>.ts.net`.

### Local development (dev server with HMR)

```bash
cd web
npm install
npm run dev        # http://localhost:3000
```

### Static-only build (GitHub Pages, no API routes)

The Pages workflow at `.github/workflows/voltforge-pages.yml` builds with
`NEXT_PUBLIC_DEPLOY_TARGET=static` and publishes the export. Enable Pages once
in **Repo → Settings → Pages → Source = "GitHub Actions"**.

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

## Data model

VOLTFORGE has two storage modes that work together:

- **Server mode (default, what the Pi installer uses):** workout drafts,
  meals, weight entries, and progress photos sync through `/api/state` and
  `/api/photos` to a SQLite database at `/var/lib/voltforge/data.db`. Every
  device on your tailnet sees the same data. The client still keeps a
  `localStorage` mirror so it works while offline; on reconnect the server
  copy is authoritative.

- **Static mode (`NEXT_PUBLIC_DEPLOY_TARGET=static`):** there's no server, so
  data lives only in the browser's `localStorage` for that device + browser.
  Used for GitHub Pages.

Photos are stored as raw bytes in the SQLite `photos` table, served via
`/api/photos/<id>`.

There is no auth — the server is single-tenant. Put it behind Tailscale and
your tailnet ACL is your auth.

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
