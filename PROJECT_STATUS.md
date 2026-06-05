# BT Monitor — Project Status & Roadmap

_Last updated: 2026-06-05_

A running narrative of what the project is, what's been built so far, what's
next, and what could still be improved. For day-to-day architecture see
[`CLAUDE.md`](CLAUDE.md); for usage see [`README.md`](README.md).

---

## 1. What this is

An AI-assisted website-health monitor aimed at being **sold to / operated for
Banca Transilvania**. It answers two questions continuously:

- **Is the site up right now?** (live uptime, ~30s)
- **Is the site healthy in depth?** (broken links, slow pages, missing assets,
  broken forms, leaked internal documents, accessibility, SEO, JS errors —
  every few hours, on demand, or scheduled)

Production target: `https://www.bancatransilvania.ro` (behind Akamai Bot
Manager — see §5). Dev target: `https://www.resetmedia.ro` + a local Flask
fixture with intentional bugs.

---

## 2. What we built (timeline)

### Phase 1 — MVP (deterministic checks)
- Crawler: `sitemap.xml` discovery + BFS fallback when no sitemap exists.
- Checks: broken links, performance (TTFB/FCP/load), missing assets (images/
  fonts/scripts), static form analysis, **document-leak scanner** (PDF/DOCX
  scanned for `Uz intern` / `Confidențial`).
- A local test fixture with 8 intentional defects to prove each check fires.
- Proved Akamai blocks naive crawling of the real BT site (expected for a bank).

### Phase 2 — Dashboard + live monitoring
- FastAPI dashboard; switch target from the UI.
- **Live watcher**: probes every ~30s, hysteresis to filter blips, desktop
  notifications + sparkline + uptime %.
- Multi-domain watching; one-file HTML report export.

### Phase 3 — Enterprise rewrite (v5)
- SQLAlchemy DB layer (`runs`, `incidents`, `incident_observations`,
  `uptime_checks`) with **fingerprint-based incident lifecycle**
  (new → still_open → resolved → reopened).
- **Two-tier monitoring**: root watcher (30s) + per-sitemap-page watcher (120s).
- 6-hour auto-scan scheduler; CSV / Excel / PDF audit exports; Romania-time
  formatting; sidebar dashboard (overview donut, incidents, history, live,
  reports, settings).
- Three checks ported from `flaviuzh/bt-monitor`: accessibility (axe-core),
  SEO, JS console errors.

### Phase 4 — Security & reliability hardening (audit 2026-06)
A formal 22-item audit (3 CRITICAL, 6 HIGH, 9 MEDIUM, 4 LOW) — **all closed**:

| Area | Fixes |
|---|---|
| Security | Basic Auth on all routes; SSRF host validation + allowlist; CSRF same-origin; CSV/formula-injection escaping; /run rate-limit + single-flight |
| Data | Single source of truth (removed `store.py`/`uptime.db`); SQLite WAL+busy_timeout; Postgres-ready; `alerts` audit table |
| Ops | env-driven structured logging + secret masking; graceful shutdown; configurable host/port; requirements pinned (dropped unused deps) |
| Quality | 35-test pytest suite; GitHub Actions CI; Dockerfile + compose (app + Postgres); modern Teams Adaptive Cards; real page→document mapping for leaks |

See commits `4d67abf` → `a676d02` on `main`.

---

## 3. Current capabilities (snapshot)

- **11 deep-scan checks** + **2-tier live uptime** + incident lifecycle.
- DB-backed history, SLA %, exports (CSV/Excel/PDF), Teams alerts.
- Secure by configuration: auth, anti-SSRF, anti-CSRF, rate-limit, audit trail.
- Tested (pytest) and CI-gated; containerized for deployment.

---

## 4. What's next (immediate, mostly infra)

1. **Flip to PostgreSQL for real.** The code, models, pool config, and
   `docker-compose.yml` are ready. Needs a Postgres instance, then
   `BT_MONITOR_DB_URL=postgresql+psycopg://…`. Recommended next step:
   add **Alembic** migrations instead of `create_all` before going to prod.
2. **Verify CI on GitHub.** The workflow runs on the next push — confirm green
   in the Actions tab.
3. **Build & smoke-test the Docker image** on a machine with Docker (not done
   locally — no Docker on the dev box).
4. **Secure BT access.** Akamai blocks external automation; pick one with BT:
   (a) allowlist a dedicated User-Agent, (b) run inside BT's network, or
   (c) get a staging URL (also unblocks real form-submission testing).

---

## 5. Roadmap (higher-value features)

From most to least immediately useful for the BT sale:

- **Email digest** (weekly PDF audit attached, SMTP) — exec-friendly.
- **Incident annotations** ("fixed by deploy X", "false positive") — ops maturity.
- **Performance regression detection** — alert when a page slows ≥50% vs a
  7-day baseline (more meaningful than a fixed threshold).
- **Lighthouse integration** — official Google PageSpeed/a11y scores.
- **Synthetic transactions** — real login + form submit on staging; directly
  answers the brief's "does the lead reach us?" question.
- **Public status page** — embeddable uptime widget for BT's customers.
- **Multi-tenant** — isolate data per monitored property / per bank.

---

## 6. Known limitations & possible improvements

- **Form check is static.** It verifies action URL reachability + fields +
  labels, but does **not** submit (would create real CRM leads). Needs a
  staging endpoint or an agreed test-lead convention. Documented in
  `checks/forms.py` (`submission_test = "not_run"`).
- **AI visual check was removed** (cost/noise). Could be re-added behind a flag
  for "any other visual issue" coverage (broken layout/fonts) on a small page
  sample.
- **Auth is Basic.** Fine behind a TLS reverse proxy; a future **login + RBAC**
  (admin/auditor/viewer) + an **audit log of who changed settings** would be
  expected for a bank.
- **Schema is `create_all`.** Move to **Alembic** before production so schema
  changes are versioned.
- **Live alert vs incident alert** can double-notify on uptime state changes
  (live `AlertHub` + incident hub). Consider routing all alerts through the
  incident hub only.
- **Secrets** come from env today; in production wire a **secret manager**
  (Vault / cloud KMS) and rotate the Teams webhook + DB password.
- **Observability**: add a `/metrics` (Prometheus) endpoint — probe latencies,
  failed-persist counter (already tracked internally), scan durations.
- **Deep-scan cadence** must stay ≥1h against the real WAF-protected target;
  live watchers (30s/120s) are the right tool for "is it up".

---

## 7. How to pick up the project

```bash
git clone https://github.com/Mikelcm/bt-monitor.git && cd bt-monitor
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python.exe -m patchright install chromium
pytest                                   # 35 tests should pass
# dashboard (local fixture):
set BT_MONITOR_ALLOW_PRIVATE_TARGETS=true
.venv\Scripts\python.exe -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
```
