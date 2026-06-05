# CLAUDE.md — context for AI assistants

This file briefs Claude (or any AI coding agent) on the project state. Read
this *before* taking any action so you don't redo work or break decisions.

## What this project is

**BT Monitor** — a website health-monitoring tool aimed at being sold to
**Banca Transilvania** (BT). It monitors uptime + content health of a target
website (default: `https://www.resetmedia.ro` for local dev; production target
is `https://www.bancatransilvania.ro`, behind Akamai Bot Manager).

Originated as an MVP from `https://github.com/flaviuzh/bt-monitor` (parts ported).
The current iteration is much more enterprise-oriented: persistent DB, audit
reports, two-tier monitoring, multi-page tracking, RO-localized dashboard.

## The user's goal

Cristi (the owner of this repo) wants a product that's **sellable to a bank's
IT department**. Translation: must look professional, must produce auditor-grade
exports (PDF/Excel), must be operationally sane (cron-like scheduler, no
flooding the target), must use Romania-time formatting, must distinguish *site
outage* from *individual page outage*.

## Two-tier monitoring architecture (important — don't break this)

| Tier | Cadence | What it watches | Incident category |
|---|---|---|---|
| **Live watcher** ([`monitoring/watcher.py`](monitoring/watcher.py)) | 30s | The root URL + user-added "critical pages" | `site_down` (critical) / `site_slow` (warn) |
| **Pages watcher** ([`monitoring/pages_watcher.py`](monitoring/pages_watcher.py)) | 120s | Every URL in `data/sitemap_pages.json` (from the last deep scan) | `page_down` (serious) / `page_slow` (moderate) |
| **Deep scan** ([`runner.py`](runner.py)) | 6h auto + on-demand | Full crawl: links, perf, assets, forms, docs, a11y, seo, console | many categories (see `db/persist.py`) |

The 6h auto-scan is run by a scheduler embedded in the FastAPI dashboard process
(`dashboard/app.py:_scheduler_loop`). Set `BT_MONITOR_SCAN_INTERVAL_H=0` to
disable it during testing.

**Don't lower the deep-scan cadence below 1h.** README and config warn that
sub-hourly will look like an attack to Akamai. Live watchers are cheap
(HEAD-style probes) so 30s/120s is fine.

## What we built and verified end-to-end

- ✅ **SQLAlchemy DB layer** (`db/`): `Run`, `Incident`, `IncidentObservation`,
  `UptimeCheck`. SQLite at `data/bt_monitor.db` by default; swap to Postgres
  with `BT_MONITOR_DB_URL=postgresql+psycopg://...`.
- ✅ **Incident lifecycle with fingerprints**: new → still_open → resolved →
  reopened. Each fingerprint is `category|page_url|details` (see
  `db/persist.py:_findings_from_reports`). Re-appearing fingerprint reopens
  the *same* incident row (history preserved).
- ✅ **Auto incident generation from watchers**: down/up state transitions
  create/resolve `site_down` / `page_down` incidents
  (`monitoring/uptime_persist.handle_state_change`).
- ✅ **Incident alerts** (`monitoring/incident_alerts.py`):
  Teams webhook (env `BT_MONITOR_TEAMS_WEBHOOK`). Severity-filtered (default
  `critical,serious`). Audit log persisted to `data/_incident_alerts.jsonl`.
- ✅ **Dashboard rewritten from scratch** (`dashboard/` v5):
  - Sidebar dark + light content, monochromatic cool palette (slate + blue)
  - Inter font via Google Fonts
  - 25 inline SVG icons (`templates/_icons.html`)
  - Persistent global status bar with site state + critical incidents link
  - Pages: `/`, `/incidents`, `/history`, `/history/{id}`, `/live`, `/reports`, `/settings`
  - Animated donut chart for health score on `/`
  - Activity feed (recent incidents + scans)
- ✅ **Exports** (`dashboard/exports.py`):
  - CSV: `/export/incidents.csv`
  - Excel: `/export/bt-monitor.xlsx` (6 sheets: **Indisponibilitati** first,
    Incidents, Runs, Uptime SLA, Uptime probes, Alerts)
  - PDF: `/export/audit.pdf` (executive summary, reportlab, navy branding)
- ✅ **Romania time everywhere**: `utils/time_ro.py` + Jinja filters `dt_ro`,
  `dt_ro_short`, `humanize_duration`. DB stores UTC, UI/exports show
  `DD.MM.YYYY HH:MM:SS Europe/Bucharest`. Needs `tzdata` pip dep on Windows.
- ✅ **No diacritics in PDF/Excel**: `utils/text.py:strip_diacritics` applied
  in `dashboard/exports.py`. Helvetica font in reportlab can't render ăâîșț.
  HTML stays full UTF-8.
- ✅ **Atomic target switch**: `config.set_base_url` writes via tmp+rename,
  validates URL, stamps `changed_at`. Dashboard shows confirmation banner.
- ✅ **Cleanup**: removed `checks/visual.py` (Claude vision — cost+noise),
  `checks/content.py` (Lorem Ipsum — no value for bank), Slack webhook,
  legacy templates (`index.html`, `report.html`), `style.css` (replaced by
  `app.css`).

## Tech stack

- Python 3.11+ (developed on 3.13)
- FastAPI + Jinja2 + Uvicorn
- SQLAlchemy 2.x (SQLite default, Postgres-ready)
- Patchright/Playwright (Chromium, for deep scan only)
- httpx (live probes)
- pypdf + python-docx (doc leak scanner)
- openpyxl + reportlab (exports)
- tzdata (Windows time zone db)
- anthropic SDK is in requirements but unused after visual.py removal — can be
  cut from requirements.txt if you want a leaner image

## File layout (current)

```
bt-monitor/
├── README.md
├── CLAUDE.md                       ← this file
├── requirements.txt
├── config.py                       central settings + atomic target switch
├── runner.py                       orchestrator (deep scan, 9 steps)
├── crawler/sitemap.py              sitemap discovery + BFS fallback
├── checks/                         (per-step deep-scan modules)
│   ├── links.py
│   ├── performance.py
│   ├── assets.py
│   ├── forms.py
│   ├── docs.py
│   ├── accessibility.py
│   ├── seo.py
│   └── console_errors.py
├── monitoring/
│   ├── watcher.py                  live root-URL watcher (30s)
│   ├── pages_watcher.py            sitemap pages watcher (120s)  ★ NEW
│   ├── alerts.py                   live uptime alert hub (Teams)
│   ├── incident_alerts.py          deep-scan incident alert hub  ★ NEW
│   ├── store.py                    in-memory uptime ring buffer
│   └── uptime_persist.py           watcher → DB bridge           ★ NEW
├── db/
│   ├── models.py                   SQLAlchemy models             ★ NEW
│   └── persist.py                  scan-result → incident upsert ★ NEW
├── dashboard/
│   ├── app.py                      FastAPI app (rewritten)
│   ├── exports.py                  CSV / Excel / PDF builders    ★ NEW
│   ├── static/app.css              design system v5              ★ NEW
│   └── templates/
│       ├── _base.html              sidebar layout + status bar   ★ NEW
│       ├── _icons.html             25 SVG icons macro            ★ NEW
│       ├── overview.html           donut + activity feed         ★ NEW
│       ├── incidents.html
│       ├── history.html
│       ├── history_detail.html
│       ├── live.html
│       ├── reports.html
│       └── settings.html
├── helpers/cookies.py              cookie-banner dismisser
├── utils/                                                        ★ NEW
│   ├── time_ro.py                  Romania time helpers
│   └── text.py                     strip_diacritics
├── test_fixture/server.py          Flask app with intentional bugs
└── data/                           runtime output (gitignored)
    ├── bt_monitor.db               SQLite DB (gitignored)
    ├── sitemap_pages.json          from last crawl
    ├── *_report.json               per-step deep-scan reports
    ├── summary.json                aggregate for dashboard
    ├── _run_state.json             current run state (live polling)
    ├── _target.json                active target + changed_at
    ├── _recent_targets.json        for the recent-list UI
    ├── _live_targets.json          extra URLs the watchers ping
    └── _incident_alerts.jsonl      alert audit log
```

## Environment variables (all optional)

| Var | Default | Effect |
|---|---|---|
| `BT_MONITOR_BASE_URL` | `https://www.resetmedia.ro` | Initial target (overridden by `data/_target.json` if it exists) |
| `BT_MONITOR_DB_URL` | `sqlite:///data/bt_monitor.db` | SQLAlchemy URL — swap to Postgres for prod |
| `BT_MONITOR_SCAN_INTERVAL_H` | `6` | Deep scan auto-cadence in hours. `0` disables. |
| `BT_MONITOR_PAGES_INTERVAL_S` | `120` | Pages watcher cadence |
| `BT_MONITOR_PAGES_HYSTERESIS` | `2` | Consecutive probes before flipping state |
| `BT_MONITOR_PAGES_CONCURRENCY` | `10` | Parallel probes for sitemap pages |
| `BT_MONITOR_WATCHER_INTERVAL_S` | `30` | Live root-watcher cadence |
| `BT_MONITOR_WATCHER_SLOW_MS` | `3000` | Threshold for `slow` classification |
| `BT_MONITOR_WATCHER_HYSTERESIS` | `3` | Consecutive probes before flipping |
| `BT_MONITOR_ALERT_SEVERITIES` | `critical,serious` | Which severities trigger alerts |
| `BT_MONITOR_ALERT_ON_RESOLVE` | `true` | Send "resolved" alert when an incident closes |
| `BT_MONITOR_TEAMS_WEBHOOK` | (unset) | MS Teams Incoming Webhook URL |
| `BT_MONITOR_DASHBOARD_URL` | (unset) | Public dashboard URL — used to build links in alerts |
| `BT_MONITOR_UPTIME_RETENTION_DAYS` | `30` | How long to keep `uptime_checks` rows |
| `BT_MONITOR_PRUNE_INTERVAL_H` | `24` | How often the dashboard prunes old uptime rows |

## How to run locally (Windows-friendly setup we use)

```powershell
# 1. Clone + venv (only first time)
git clone https://github.com/Mikelcm/bt-monitor.git
cd bt-monitor
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m patchright install chromium

# 2. Optional: test fixture in another shell
.\.venv\Scripts\python.exe test_fixture\server.py     # → http://127.0.0.1:8765

# 3. Dashboard (which also starts watchers + scheduler)
$env:BT_MONITOR_BASE_URL = "http://127.0.0.1:8765"
.\.venv\Scripts\python.exe -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000

# 4. Open http://127.0.0.1:8000 and click "Rulează scan complet"
#    First deep scan against fixture takes ~3 min.
```

## Key design decisions you should respect

1. **DB is the source of truth** for incidents + uptime probes. JSONs in `data/`
   are scratch output of each scan (regenerated). Don't try to make `data/*.json`
   authoritative.
2. **Romania time only in display layers**. Models store UTC. Convert in
   templates via `| dt_ro` filter or in exports via `format_ro()`.
3. **Strip diacritics only for PDF/Excel** (Helvetica limitation). Don't strip
   in DB or HTML — UTF-8 there is fine.
4. **Sub-hourly deep scans are forbidden** for production targets. WAF will
   block. Live watcher (30s) is the right tool for "is it up right now".
5. **Single dashboard process** owns the watchers + scheduler. For real BT
   deployment, switch to systemd or cron kicking `runner.py` directly +
   dashboard runs separate read-only.
6. **Sidebar is dark navy**, content is light, palette is monochromatic cool
   (no gold, no warm tones). User explicitly rejected the gold accent.
7. **Site outage vs page outage** must remain distinct — they're tracked as
   separate categories (`site_down` vs `page_down`) with different severities.
   Auditors must see this distinction in reports.
8. **Schema is owned by Alembic on Postgres, `create_all` on SQLite.** See below.

## Database schema & migrations (Alembic)

- **SQLite (dev/test default)**: `db.models.init_db()` runs `create_all`. No
  migration tooling needed — fast local runs, the 35-test suite uses this.
- **Postgres (production)**: `init_db()` is a **no-op**; the schema is owned by
  **Alembic**. The Docker entrypoint (`docker-entrypoint.sh`) runs
  `alembic upgrade head` before uvicorn starts.
- **Migration files** live in `alembic/versions/`. The env (`alembic/env.py`)
  resolves the DB URL from `BT_MONITOR_DB_URL` (same as the app), targets
  `db.models.Base.metadata`, and uses `render_as_batch` for SQLite ALTERs.
- **When you change a model**, you MUST add a migration or CI fails:
  ```
  alembic revision --autogenerate -m "describe change"   # creates the migration
  alembic upgrade head                                     # apply it
  ```
  CI runs `alembic upgrade head` + `alembic check` (the latter fails if models
  drift from migrations — i.e. you forgot to autogenerate).
- **Do NOT reintroduce `create_all` for Postgres** — it would race with Alembic
  and leave `alembic_version` inconsistent. Keep the dialect guard in `init_db()`.

## Pending / recommended work (TODO when user asks)

### Tier 1 (must-have for selling to BT)
1. **Auth + RBAC** — login form, bcrypt passwords, sessions, 3 roles
   (admin/auditor/viewer). User has NOT picked this yet; was a recommendation.
2. **Audit log** — track who changed target/alerts/settings. Auditor requirement.
3. **Deployment artifacts** — Dockerfile + docker-compose, healthcheck, reverse
   proxy config (Caddy/nginx). BT will deploy on their VM.

### Tier 1.5 (high value)
4. **Email digest weekly** with PDF audit attached. SMTP integration, scheduler
   weekly. User said "do it" but we never started (#52 task pending).
5. **Annotations on incidents** — operator note "fixed by deploy X" or
   "false positive". Marks maturity.
6. **Performance regression detection** — alert if a page slows by 50% vs 7-day
   baseline. More valuable than threshold-based "slow".
7. **Lighthouse integration** — official Google PageSpeed scores.

### Tier 2 (differentiators)
8. **Multi-tenant** — N banks, isolated data per tenant.
9. **Public status page** — embeddable widget for the bank's clients.
10. **Synthetic transactions** — real login + form submit on staging. Answers
    the brief's "lead-ul ajunge la noi" question directly.

## Known issues / gotchas

- **`_target.json` race**: in earlier testing, multiple uvicorn instances in
  parallel test sessions overwrote each other. Fixed atomic-write but if you
  see weird target switches, kill all uvicorn processes first.
- **Playwright Chromium** is downloaded to `%LOCALAPPDATA%\ms-playwright\` on
  Windows — not in `.venv`. Re-install with `patchright install chromium` if
  it ever disappears.
- **axe-core `axe_not_loaded`** is logged as `[info]` (not error) on minimal
  HTML pages from the test fixture. Real bank pages should load axe fine.
- **`anthropic` SDK** is in `requirements.txt` but no code imports it after
  visual.py removal. Safe to drop from requirements if shipping.
- **CI/CD doesn't exist yet** — no GitHub Actions, no tests in pytest. Smoke
  tests in this session are manual.

## How conversations with Cristi go (style notes)

- Cristi writes in Romanian; respond in Romanian. Code stays English.
- He's the product owner, not the day-to-day coder. He thinks in features
  and outcomes (vandabil la BT), not implementation details.
- He's been opinionated about UX: "redesign de la 0" once, "ai fost lazy"
  once. Don't just do cosmetic refresh — change layout/structure properly.
- He prefers brief, decision-oriented questions over open-ended ones. Use
  AskUserQuestion with 2-4 concrete options + "(Recommended)" on your pick.
- He wants to see things work end-to-end. After implementing, validate via
  curl/UI before claiming done.
- The fixture in `test_fixture/server.py` is what we test against locally.
  It's intentionally broken in 8 ways and gives predictable results.

## Last session summary

Session of 2026-05-28 (UTC) added: DB layer, incident alerting, 2-tier
monitoring (root + sitemap pages), 6h scheduler, exports (CSV/XLSX/PDF),
Romania timezone, diacritic stripping for PDF/Excel, complete dashboard rewrite
(sidebar + Inter + SVG icons + donut chart + activity feed), atomic target
switch with audit, monochromatic cool palette unification. Pending: email
digest, auth, deployment, BT staging connection.
