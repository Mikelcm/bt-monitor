# BT Monitor

Continuous website health monitor for **Banca Transilvania**
(`https://www.bancatransilvania.ro`).

Two-tier architecture: a cheap live-uptime watcher catches outages within ~90
seconds, while a heavier deep scan runs every 6 hours to find content-level
issues (broken links, missing assets, accessibility violations, internal-doc
leaks, etc.). Everything is persisted to a SQLite/Postgres DB with stable
incident identity, so you get audit-grade timelines, SLA reports, and
exportable PDF/Excel/CSV for compliance.

> **AI assistants:** see [CLAUDE.md](CLAUDE.md) for the full project briefing
> (architecture, decisions, pending work, conversation style notes).

## What it detects

### Tier 1 — live monitoring (real-time)
| # | Check | What it catches | Cadence |
|---|---|---|---|
| 1 | **Site uptime** | Root URL returns non-2xx or fails to respond | 30s |
| 2 | **Page uptime** (every sitemap URL) | Individual page returns 4xx/5xx or times out | 120s |
| 3 | **Slow response** | Latency > 3s consistently | 30s / 120s |

Live findings generate incidents with severity:
- `site_down` (critical) — site root unreachable
- `site_slow` (warn) — site root responds slowly
- `page_down` (serious) — specific page from sitemap unreachable
- `page_slow` (moderate) — specific page responds slowly

### Tier 2 — deep scan (every 6h, or on-demand)
| # | Check | What it catches |
|---|---|---|
| 4 | **Broken links** | 404s, redirect loops, dead external links |
| 5 | **Slow pages** | TTFB / FCP / load time over a configurable threshold |
| 6 | **Missing assets** | Images, fonts, scripts or stylesheets that fail to load |
| 7 | **Form issues** | Broken `action` URLs, no submit button, missing labels |
| 8 | **Internal-doc leaks** | PDFs / DOCX containing "Uz intern" / "Confidențial" (**critical**) |
| 9 | **Accessibility (WCAG 2 AA)** | axe-core scan, critical + serious violations only |
| 10 | **SEO health** | Missing title / description / canonical / viewport, multiple H1s |
| 11 | **JS errors & failed reqs** | Uncaught exceptions, `console.error()`, broken fetches |

Checks 9, 10, 11 are ported from [flaviuzh/bt-monitor](https://github.com/flaviuzh/bt-monitor).

A FastAPI dashboard renders everything with a health score, donut chart,
activity feed, filterable incident list, run history, and export center.
A test fixture with **intentional issues** ships with the project so
reviewers can verify every check actually detects what it claims to.

---

## Requirements

- Windows / Linux / macOS
- **Python 3.11+** (developed on 3.13)
- ~250 MB free disk (Playwright Chromium download)
- Outbound HTTPS to the target site

## Install

```powershell
:: 1. Clone the repo
git clone https://github.com/Mikelcm/bt-monitor.git
cd bt-monitor

:: 2. Create a virtual env
py -3.13 -m venv .venv

:: 3. Install Python deps
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 4. Download Chromium for Patchright
.venv\Scripts\python.exe -m patchright install chromium
```

(On Linux/macOS replace `.venv\Scripts\python.exe` with `.venv/bin/python`.)

> **Note for new contributors:** the `data/` folder is empty in git — every
> JSON / SQLite file there is generated. After install, start the dashboard
> and click "Rulează scan complet" to populate it.

---

## Usage

### Recommended: run the dashboard (it owns the watchers + scheduler)

```powershell
.venv\Scripts\python.exe -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The dashboard:

- Starts the live watcher (30s probes on the root URL)
- Starts the pages watcher (120s probes on every URL from the latest sitemap)
- Starts the scheduler (auto deep-scan every 6h — configurable)
- Lets you click "Rulează scan complet" for an on-demand scan
- Shows everything on 6 sections in the sidebar

### One-shot deep scan from the command line

```powershell
.venv\Scripts\python.exe runner.py
```

Each step prints its own summary, writes a JSON report under `data/`, and
persists incidents to the DB. A consolidated `data/summary.json` is written
at the end.

### Targeting

```powershell
:: Local test fixture (every issue type intentionally present)
.venv\Scripts\python.exe test_fixture\server.py     :: in another shell
$env:BT_MONITOR_BASE_URL = "http://127.0.0.1:8765"

:: Or just change the target from /settings in the dashboard.
```

### Production target

```powershell
$env:BT_MONITOR_BASE_URL = "https://www.bancatransilvania.ro"
:: see "Deployment to Banca Transilvania production" section below
```

---

## Dashboard sections

| Section | What you see |
|---|---|
| **Privire generală** | Donut health score, severity counts, trend sparkline, live status, top incidents, activity feed |
| **Incidente** | Filterable list (status × category × severity), severity cards, trend |
| **Istoric scan-uri** | Every deep-scan run with health/duration/status, drill-down to per-run incidents |
| **Monitorizare live** | Real-time watcher state, SLA 24h/7d/30d from DB, critical-page add/remove |
| **Rapoarte / Export** | CSV (incidents), Excel (6 sheets including **Indisponibilitati**), PDF audit (executive summary) |
| **Setări** | Change target, alert config status, DB stats, pages-watcher status, scheduler config |

A persistent **status bar** sits above every page showing site state + open
critical incidents.

---

## Output

```
data/
├── bt_monitor.db                # SQLite — runs, incidents, observations, uptime_checks
├── sitemap_pages.json           # discovered URLs (from last crawl)
├── links_report.json            # broken / redirected links per page
├── performance_report.json      # TTFB, FCP, LCP, load time per page
├── assets_report.json           # 4xx/5xx + network failures for images/fonts/etc.
├── forms_report.json            # every form + static health issues
├── docs_report.json             # PDFs/DOCX scanned + sensitive markers
├── accessibility_report.json    # axe-core violations
├── seo_report.json              # SEO heuristics
├── console_report.json          # JS errors + failed requests
├── summary.json                 # top-level aggregate
├── _run_state.json              # current run state (for dashboard polling)
├── _target.json                 # active target + changed_at
├── _recent_targets.json         # for the dashboard recent-targets UI
├── _live_targets.json           # extra URLs the live watchers ping
└── _incident_alerts.jsonl       # alert audit log (persisted across restarts)
```

Reports are stable JSON — easy to diff between runs. The DB is the source of
truth for everything you need for audit.

---

## Exports (for compliance / audit)

| Format | Endpoint | Content |
|---|---|---|
| **CSV** | `/export/incidents.csv?status=all` | Flat incidents list, UTF-8 BOM for Excel |
| **Excel** | `/export/bt-monitor.xlsx` | 6 sheets: **Indisponibilitati** (uptime outages first), Incidents, Runs, Uptime SLA, Uptime probes, Alerts |
| **PDF** | `/export/audit.pdf` | Executive summary, navy branding, sections for site/page outages, critical incidents, recent runs |

All exports use Romania timezone (`DD.MM.YYYY HH:MM:SS`) and ASCII-only text
(diacritics stripped) so they render correctly in any environment.

---

## Security hardening (audit 2026-06)

The dashboard ships with the security controls a bank's IT review expects:

| Control | How |
|---|---|
| **Authentication** | HTTP Basic on every route except `/static` + `/healthz`. Enabled when `BT_MONITOR_AUTH_USER` + `BT_MONITOR_AUTH_PASS` are set (constant-time compare). Front with a TLS reverse proxy in production. |
| **SSRF protection** | The scan target is resolved and rejected if it points at private / loopback / link-local / reserved IPs (blocks cloud metadata, internal hosts). `BT_MONITOR_ALLOWED_HOSTS` pins production to BT domains; `BT_MONITOR_ALLOW_PRIVATE_TARGETS=true` re-enables the local fixture in dev. |
| **CSRF** | Same-origin check (Origin/Referer) on all state-changing requests. |
| **Spreadsheet injection** | CSV/XLSX exports neutralize cells starting with `= + - @` (CWE-1236). |
| **Rate limiting** | One deep scan at a time + cooldown (`BT_MONITOR_SCAN_MIN_INTERVAL_S`). |
| **Audit trail** | Every alert persisted to the `alerts` DB table (kind, severity, channel, delivery status). |
| **Secrets** | Webhooks/credentials come only from env; `mask_secret()` keeps them out of logs. |

### Security-related env vars

| Var | Default | Effect |
|---|---|---|
| `BT_MONITOR_AUTH_USER` / `_PASS` | (unset) | Enable Basic Auth |
| `BT_MONITOR_ALLOWED_HOSTS` | (unset) | Comma list; only these hosts (+subdomains) may be scanned |
| `BT_MONITOR_ALLOW_PRIVATE_TARGETS` | `false` | Permit loopback/private targets (dev fixture) |
| `BT_MONITOR_SCAN_MIN_INTERVAL_S` | `60` | Min seconds between deep-scan starts |
| `BT_MONITOR_TEAMS_FORMAT` | `messagecard` | `adaptive` for Power Automate Workflows |
| `BT_MONITOR_LOG_LEVEL` / `_FORMAT` | `INFO` / `text` | Logging verbosity / `json` |
| `BT_MONITOR_HOST` / `_PORT` | `127.0.0.1` / `8000` | Bind address |
| `BT_MONITOR_DB_URL` | SQLite (WAL) | `postgresql+psycopg://…` for production |

## Tests & CI

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest                       # 35 tests: SSRF, CSV, auth, CSRF, DB/WAL, alerts, watcher…
ruff check --select E9,F63,F7,F82 .
```

GitHub Actions (`.github/workflows/ci.yml`) runs lint + tests on every push/PR to `main`.

## Docker (production)

```bash
cp .env.example .env          # fill POSTGRES_PASSWORD, BT_MONITOR_AUTH_*, etc.
docker compose up -d --build  # app + dedicated Postgres, exclusive Postgres in prod
```

The image installs Chromium for the deep scan, runs as a non-root user, and
exposes `/healthz` for healthchecks. SQLite (with WAL) remains the zero-config
default for local dev; production uses Postgres via `BT_MONITOR_DB_URL`.

## Deployment to Banca Transilvania production

`bancatransilvania.ro` is protected by **Akamai Bot Manager**. Akamai inspects
TLS / HTTP/2 fingerprints and injects sensor-data challenges. **The tool
will not function against the real site without explicit allowance from
the BT infrastructure team.** This is by design, and the correct thing for a
bank to have in place.

Three viable paths — pick whichever BT's infosec team prefers:

1. **Allowlist a dedicated User-Agent in Akamai.** We adopt a clear identifier
   such as `BT-Monitor/1.0 (contact: <your-team-email>)`. BT adds a Bot
   Manager rule that allows this UA. No bypass tricks, fully audit-able.

2. **Run from inside BT's network.** Akamai exempts BT's own egress IPs
   from Bot Manager. We deploy the tool on a small VM / container in BT's
   infra and run it from there.

3. **Provide a staging URL.** A pre-prod or canary environment without
   the production WAF is by far the easiest path for the form-submission
   check (which needs to actually post real test leads).

When BT grants any of the above, just point the dashboard at the URL via
`BT_MONITOR_BASE_URL` or the in-dashboard `/settings` form. Everything else
adapts.

---

## Configuration

`config.py` and environment variables (all optional):

| Var | Default | Purpose |
|---|---|---|
| `BT_MONITOR_BASE_URL` | `https://www.resetmedia.ro` | Initial target (data/_target.json overrides) |
| `BT_MONITOR_DB_URL` | `sqlite:///data/bt_monitor.db` | SQLAlchemy URL (use `postgresql+psycopg://...` for prod) |
| `BT_MONITOR_SCAN_INTERVAL_H` | `6` | Deep scan auto-cadence. `0` disables. |
| `BT_MONITOR_PAGES_INTERVAL_S` | `120` | Sitemap-pages watcher cadence |
| `BT_MONITOR_WATCHER_INTERVAL_S` | `30` | Live root-watcher cadence |
| `BT_MONITOR_WATCHER_SLOW_MS` | `3000` | `slow` threshold |
| `BT_MONITOR_ALERT_SEVERITIES` | `critical,serious` | Which severities trigger Teams alerts |
| `BT_MONITOR_TEAMS_WEBHOOK` | (unset) | MS Teams Incoming Webhook URL |
| `BT_MONITOR_DASHBOARD_URL` | (unset) | Public URL — used to build links in alerts |
| `BT_MONITOR_UPTIME_RETENTION_DAYS` | `30` | How long to keep uptime probe rows |

A full list lives in [CLAUDE.md](CLAUDE.md).

---

## Scheduling continuous runs

The dashboard process **already runs the scheduler** (every 6h by default).
For production you may prefer to run `runner.py` directly from cron / Task
Scheduler so deep scans aren't tied to the dashboard process lifetime:

**Windows Task Scheduler:**
```
Action:    .venv\Scripts\python.exe
Argument:  runner.py
Start in:  C:\path\to\bt-monitor
Trigger:   every 6 hours
```

**Linux cron:**
```cron
0 */6 * * * cd /opt/bt-monitor && /opt/bt-monitor/.venv/bin/python runner.py >> /var/log/bt-monitor.log 2>&1
```

Anything sub-hourly risks looking like a stress-test to Akamai — keep deep
scans at 6h or longer.

---

## Test fixture — proving every check works

`test_fixture/server.py` is a small Flask app that serves 8 pages, each
broken in a specific way:

| URL | Intentional issue |
|---|---|
| `/` | Healthy homepage |
| `/clean-page` | Healthy (sanity baseline) |
| `/broken-links` | 4 dead anchors + a redirect loop |
| `/missing-assets` | Missing image + missing font |
| `/slow` | 5-second server-side delay |
| `/broken-form` | Form posts to `/this-endpoint-does-not-exist` |
| `/good-form` | Well-formed form (control) |
| `/docs` + `/static/uz-intern.pdf` | PDF containing "Uz intern" + "Confidențial" |

Start it, point the tool at it, and confirm every issue is reported:

```powershell
.venv\Scripts\python.exe test_fixture\server.py
:: in another shell:
$env:BT_MONITOR_BASE_URL = "http://127.0.0.1:8765"
.venv\Scripts\python.exe runner.py
```

Expected: 5 broken links · 1 slow page · 3 asset failures · 1 form with
3 issues · 1 internal-doc leak.

---

## File layout

See [CLAUDE.md](CLAUDE.md) for the complete annotated layout. Top-level:

```
bt-monitor/
├── README.md
├── CLAUDE.md                       briefing for AI assistants
├── requirements.txt
├── config.py
├── runner.py                       deep-scan orchestrator
├── crawler/                        sitemap discovery
├── checks/                         per-step deep-scan modules
├── monitoring/                     live watchers + alerts + uptime persist
├── db/                             SQLAlchemy models + incident persistence
├── dashboard/                      FastAPI app + templates + exports
├── helpers/                        cookie banner dismissal
├── utils/                          time + text helpers
├── test_fixture/                   Flask app with intentional issues
└── data/                           runtime output (gitignored)
```

---

## Collaborating

Two-person workflow:

```powershell
:: pull latest changes before you start work
git pull

:: do your changes, then
git add <files-you-touched>
git commit -m "what you changed and why"
git push
```

**What's safe to commit:**
- Anything under `crawler/`, `checks/`, `dashboard/`, `monitoring/`, `db/`,
  `utils/`, `helpers/`, `test_fixture/`, plus the top-level `config.py`,
  `runner.py`, `requirements.txt`, `README.md`, `CLAUDE.md`.

**What is NOT committed (and shouldn't be):**
- `data/*` — every JSON / SQLite / log there is per-run output.
- `.venv/` — each person creates their own.
- `.env` — secrets. Share via 1Password / Bitwarden, never git.

## Next iterations

See the *Pending / recommended work* section in [CLAUDE.md](CLAUDE.md) for the
prioritized list. Top items: auth + RBAC, audit log, deployment artifacts,
weekly email digest, performance regression detection.
