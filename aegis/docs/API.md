# API

Base URL is the dashboard origin (default `http://127.0.0.1:8600`). Interactive OpenAPI docs live at `/api/docs` (login required for the calls themselves).

## Authentication

There are two ways to authenticate:

* **Bearer token** (scripts, iOS Shortcuts). Create one with `aegis create-token` and send `Authorization: Bearer <token>`. CSRF is not needed.
* **Session** (browser). Call `POST /api/login` with `{"username","password"}`. It sets the `aegis_session` cookie and returns `csrf_token`. Every non-GET request authenticated by cookie must send `X-CSRF-Token: <csrf_token>`.

Unauthenticated requests get `401`, and a bad or missing CSRF token gets `403`. Logins are limited to 5 failures per 15 minutes per client (`429`).

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | public liveness: `{"ok": bool}` (503 if DB/worker unhealthy) |
| POST | `/api/login`, `/api/logout` | session management |
| GET | `/api/status` | overview: agent, counts, active/waiting/recent, metrics, model, usage, research, sandbox |
| GET | `/api/health` | detailed health checks |
| POST | `/api/objectives` | create: `goal`, `kind` (general/research/coding/monitor), `priority` 1–9, `success_criteria[]`, `constraints[]`, `allowed_tools[]`, `deadline`, `budget_tokens`, `budget_usd`, `time_budget_minutes` → `{"id"}` |
| GET | `/api/objectives?status=` | list |
| GET | `/api/objectives/{id}` | full detail: plan, tasks (args/result/verification), events, approvals |
| POST | `/api/objectives/{id}/pause\|resume\|cancel\|retry` | lifecycle |
| POST | `/api/objectives/{id}/clarify` | `{"answer"}` for a pending clarification question |
| GET | `/api/approvals?status=pending` | approval requests |
| POST | `/api/approvals/{id}/approve\|reject` | decide one exact action |
| GET/POST | `/api/topics` | list / create research topic (`name`, `query`, `feeds[]`, `seed_urls[]`, `allowed_domains[]`, `interval_minutes` ≥ 15, `max_docs_per_run`, `freshness_days`, `priority`, `enabled`) |
| PATCH | `/api/topics/{id}` | update fields |
| POST | `/api/topics/{id}/run` | run at next worker cycle |
| POST | `/api/research/pause\|resume` | global research switch |
| GET | `/api/search?q=&kind=documents\|claims\|experiments&topic=&domain=&min_quality=&limit=` | FTS5 search |
| GET/DELETE | `/api/documents/{id}` | document with quality breakdown, claims, related docs / delete it |
| GET | `/api/claims/{id}` | claim with evidence passages, history, contradictions |
| GET | `/api/contradictions` | open contradictions |
| POST | `/api/contradictions/{id}/resolve` | `{"keep_claim": id\|null, "reason"}` |
| GET | `/api/reports/daily?hours=24&format=json\|markdown` | research report from DB counts |
| GET | `/api/export/knowledge` | full knowledge export (JSON download) |
| GET | `/api/metrics?hours=` | resource history |
| GET | `/api/usage?days=` | model usage + cost by objective/purpose |
| GET/POST | `/api/budgets` | read / update `daily_spend_limit_usd`, `research_daily_spend_limit_usd`, `max_requests_per_hour`, `max_tokens_per_objective` (persisted) |
| GET | `/api/logs?lines=` | redacted log tail |
| GET | `/api/audit?limit=&objective_id=` | audit events + `chain_ok` |
| GET | `/api/tools` | tool registry contracts |
| GET | `/api/config` | effective configuration (secrets shown as `***set***`) |
| GET | `/api/notifications?unread=`, POST `/api/notifications/read` | notifications |
| GET/POST | `/api/backups` | list / create + verify backup |
| GET | `/api/backups/{id}/download` | download a backup |
| POST | `/api/shutdown` | graceful shutdown |

## Example

```bash
T=aegis_xxx
curl -s -H "Authorization: Bearer $T" -H 'content-type: application/json' \
  -d '{"goal":"Research Raspberry Pi 5 NVMe boot using https://www.raspberrypi.com/documentation/computers/raspberry-pi.html","kind":"research"}' \
  http://127.0.0.1:8600/api/objectives
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8600/api/objectives/obj_… | jq .status
```
