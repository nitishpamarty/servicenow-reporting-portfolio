# ServiceNow Reporting Analyst Portfolio — Project Brief

## Goal

Build a self-directed portfolio project to support an application for a "Reporting Analyst – ServiceNow" role. The role emphasizes: ServiceNow Reporting module, List reports, Database views, Performance Analytics (PA), homepage/dashboard design, SQL/data modeling basics, Advanced Excel, Power BI or Tableau, ITIL familiarity (Incident, Problem, Change, SLA), and CMDB/Asset data quality. The job description weights native ServiceNow reporting skills first, with Tableau/Power BI as a secondary requirement.

The deliverable is: (1) a populated Personal Developer Instance (PDI) with realistic ITSM + CMDB data, (2) native ServiceNow dashboards/homepages built with Performance Analytics, (3) a Tableau Public dashboard, and (4) a small personal website that showcases both, to link from a resume/cover letter.

All data is synthetic. Do not describe this as real client/production experience — frame it as a self-directed demo project.

## Status so far

- Registered for a ServiceNow Developer Portal account and claimed a free PDI (instance URL + admin username/password recorded).
- Installed Tableau Desktop (trial) and successfully connected to the PDI via the ServiceNow JDBC connector — tables are visible in Tableau's Data Source canvas. No extract or worksheet built yet.
- Reviewed and corrected the original build plan. Key corrections made:
  - The legacy Tableau "ServiceNow ITSM" connector was retired July 2025; must use the newer ServiceNow JDBC connector (extract-only, no live connection, no incremental refresh) — already in use.
  - Tableau Desktop is a 14-day trial, not free indefinitely; Tableau Public's own free desktop app does not support JDBC/database connectors, so the extract must be built in Tableau Desktop within the trial window (or CSV export can be used as a fallback into Power BI Desktop, which is free forever).
  - ServiceNow's Performance Analytics historic data collector backfills existing records — it does not invent history. Synthetic tickets need randomized `opened_at` / `resolved_at` / `closed_at` values spread across the past 60–90 days *before* running the collector, otherwise trend charts will just show a spike on today's date.
  - ServiceNow's "Build Agent" (the new Studio/Cursor/Claude Code integration) is an app-development/governance tool, and its native MCP integration requires a Now Assist / AI-native SKU enabled by an admin — not available on a free PDI. It's not the right mechanism for bulk data seeding.
  - The practical mechanism for programmatic data creation is the ServiceNow REST Table API (`/api/now/table/<table_name>`), which works on every instance including free PDIs via basic auth — no special license required.

## Plan for the next phase (in Claude Code)

### 1. Project setup
- Create a project folder, e.g. `servicenow-portfolio/`.
- Subfolders: `scripts/` (data generation + API calls), `data/` (generated CSVs, for reference/debugging), `site/` (personal website), `docs/` (screenshots, writeups).
- Add a `.env` file for credentials (`SNOW_INSTANCE_URL`, `SNOW_USERNAME`, `SNOW_PASSWORD`) and a `.gitignore` that excludes `.env` and any credential files. Never commit credentials, even for a personal dev instance — a public repo is part of this project's plan (the website), so hygiene matters here more than usual.
- Recommended: create a dedicated non-admin ServiceNow user with a scoped role (e.g. `itil` + `personalize_dashboard` + table write access) for API calls, rather than using the admin account long-term. Not required for a PDI, but cleaner if the repo or write-up is ever shared.

### 2. Data generation and load (scripted)
- Write a Python script using `requests` (or `pandas` + `requests`) that:
  - Generates synthetic records for `incident`, `problem`, `change_request`, and CMDB tables (`cmdb_ci_server`, `cmdb_ci_business_app`, `cmdb_ci_service`, plus relationships in `cmdb_rel_ci`).
  - Explicitly randomizes business date fields relevant to ITSM metrics: `opened_at`, `resolved_at`, `closed_at` (incident), `priority`, `category`, `assignment_group`, `caller_id`. Spread these across the past 60–90 days so MTTR/SLA/backlog trends look real. (Note: `sys_created_on` is normally read-only via the API — don't rely on it; the business date fields above are what PA indicators and reports key off.)
  - Uses realistic distributions, not uniform randomness: e.g. skew Priority 2 incidents toward one or two specific `cmdb_ci` values (so there's a real "finding" to surface later — this maps directly to the job description's example: "a disproportionate share of P2 incidents originate from a specific application").
  - Posts records via `POST /api/now/table/<table>`, handles auth, retries, and rate limits gracefully, and logs created `sys_id`s for verification.
  - Includes a small "data quality gap" on purpose — a handful of records with missing category, blank assignment group, or an orphaned CI reference — since the job explicitly calls out identifying data quality issues as a responsibility. This gives you something concrete to write about.
- Run the script, then spot-check a sample of records in the ServiceNow UI.
- Once base data exists, run Performance Analytics historic data collection (`Performance Analytics > Data Collector > Jobs` → the relevant `[PA ITSM]` historic job → Execute Now) to backfill indicator scores against the now-realistic date spread.

### 3. Native ServiceNow reporting build (do this manually in the UI, not via API)
Dashboard/widget layout in ServiceNow's Reporting/PA modules doesn't automate well through the REST API — build this by hand once data is in place:
- 2–3 List Reports (e.g. open incidents by priority, SLA breach list, change success/failure by CI).
- 1 Database View joining incident + CI + assignment group for a cross-table report.
- 1–2 Performance Analytics dashboards using indicators/breakdowns for MTTR, SLA compliance %, backlog by priority, and P2-incidents-by-application (the "finding" you seeded above).
- A homepage tailored to a specific audience (e.g. an "IT Director" homepage vs. a "Service Desk Manager" homepage) — this directly matches the job description's "design intuitive homepage and dashboard layouts for different stakeholder audiences."
- Take clean screenshots of each as you go (these are your only way to show this work publicly, since the PDI itself is behind login).

### 4. Tableau
- Finish the Data Source join/relationships (incident, task_sla, sys_user_group already visible).
- Switch to Extract, build the KPI dashboard(s): MTTR, SLA breach compliance rate, open backlog by priority, change success/failure ratio. Add reopen rate and first-contact resolution if time allows.
- Before publishing, rename the data source connection so the PDI hostname doesn't linger in the packaged workbook's metadata.
- Publish via `Server > Tableau Public > Save to Tableau Public`. Tableau Public gives you an embed snippet for the website.

### 5. Personal website
- Simplest option: a static site on GitHub Pages (free, no backend needed) — Claude Code can scaffold this directly.
- Structure: a short intro tying the project to the job description's responsibilities, an embedded Tableau Public dashboard (using Tableau's embed code), a screenshot/GIF gallery of the native ServiceNow dashboards and homepages (can't embed these live since they're behind PDI auth), and a short "what I'd do with real data" section connecting your synthetic findings to the kind of recommendations the role expects (e.g. the seeded P2-incident pattern).
- Keep the write-up honest: synthetic data, personal dev instance, self-directed project.

### 6. Verification pass before calling it done
- Confirm PA trend charts actually show variation over time (not a flat line/spike).
- Confirm the seeded data-quality gaps and the P2-application pattern are visible in at least one report each — these are your "talking points" for the cover letter/interview.
- Confirm no credentials are committed anywhere in the repo (`git log -p | grep -i password` as a final check before pushing).
- Click through the published website and Tableau Public embed as a first-time visitor would.

## Reference: ServiceNow Table API basics for the script

- Base URL: `https://<instance>.service-now.com/api/now/table/<table_name>`
- Auth: HTTP Basic (username/password), fine for a personal PDI over HTTPS.
- Create: `POST` with a JSON body of field values.
- Read/verify: `GET` with sysparm_query filters.
- Useful tables: `incident`, `problem`, `change_request`, `cmdb_ci_server`, `cmdb_ci_business_app`, `cmdb_ci_service`, `cmdb_rel_ci`, `sys_user`, `sys_user_group`.
