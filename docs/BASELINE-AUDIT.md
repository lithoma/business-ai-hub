# Baseline audit — 2026-10-20

## Scope

Inspected local Databricks exports and the Registry DDL supplied in chat. No Databricks resource was changed, SQL executed, Job started, schedule enabled, email sent, or App deployed. Export hashes establish local preservation, not equivalence with the live Workspace or successful runtime acceptance.

## Milestone 2A evidence

- UI: create task, preview configuration, save draft, list YAML/Registry sources and lifecycle status, confirm draft deletion.
- Backend: `GET /api/tasks`, `POST /api/tasks/create`, `PUT /api/tasks/{task_id}`, `DELETE /api/tasks/{task_id}`. Updates and deletes check for draft status. There is no UI for editing previously saved drafts.
- Registry DDL matches the 12 backend-selected columns, including JSON configuration strings, BOOLEAN enabled, lifecycle status, and timestamps. Required fields are supplied by the create handler.
- The supplied DDL is a snapshot of existing table metadata. It is not an idempotent migration, has no uniqueness constraint on task_id, and includes Delta runtime/protocol properties. It has not been validated on another runtime.

## Dependencies and deployment

- Hub: React 18, TypeScript 5.7, Vite 6, react-markdown, remark-gfm, Lucide; FastAPI, Uvicorn, Databricks SDK, PyYAML, Pydantic.
- Task Manager: Databricks SDK, PyYAML, requests; PySpark supplied by the compute runtime.
- No dependency lockfiles, CI workflows, Databricks Asset Bundle, Job JSON, Alert configuration export, or resource grant declarations are included.
- App identity authentication uses `WorkspaceClient()`; Supervisor token is read from an environment variable. Warehouse and Genie resource IDs and user Workspace paths are committed configuration, not credentials.

## Findings for subsequent development

| Finding | Consequence / next step |
| --- | --- |
| Five Cockpit snapshot files are zero bytes | Actual metric-view DDL, A1/A2 configuration, benchmark results, and snapshot summary remain missing; re-export populated content |
| Executor only loads `tasks/*/config.yaml` | Registry draft activation and execution require a later integration; drafts currently remain unexecuted |
| UI accepts interval scheduling and inline SQL/prompts; executor supports daily/weekly and file references | Define an explicit Registry-to-executor contract before enabling Registry tasks |
| `sales_anomaly/trigger.sql` intentionally raises an error | Verified KPI logic is not included; task is disabled |
| `daily_sales` is enabled, other three tasks disabled | Original README's claim that all tasks are disabled is stale; running the executor can generate a report and write Outbox |
| `business_date_offset_days` is 487 | Preserved simulated-data setting; do not assume previous-day processing or automatically change it to 1 |
| Supervisor URL and route are empty | Optional Supervisor integration remains unconfigured |
| Registry load exceptions are swallowed | Registry failure can appear as an empty list; introduce explicit degraded-state reporting in a later change |
| Backend converts `enabled` using Python `bool()` | String `"false"` becomes true; verify Statement Execution result representation and normalize explicitly later |
| UI list/delete timeout is 30s; SQL wait can reach 50s | Requests may time out while backend work continues; align timeouts later |
| Task ID uniqueness and draft checks precede separate mutations | Concurrency safety needs review before broader use |
| Hub deployment README still describes Milestone 1 overlay | Use root README/audit for current repository orientation; update operational documentation in a later change |

## Source hygiene

All original export text files were inspected with redacted credential-pattern checks. No matching Databricks PAT, GitHub token, AWS access key, private key, or literal credential assignment was found. Pattern checks are not proof that no possible secret exists. Business data is simulated per the owner; authentication credentials would still be excluded if discovered.

Generated dependencies/builds/caches are absent from the original exports. `.DS_Store` is excluded; `.gitignore` also excludes common build outputs, environment files, and key files. Databricks manifests and the already-applied frontend patch are retained as original export evidence.

## Verification

- All five Python source files parse successfully without executing their modules.
- Hub JSON configuration files parse and local frontend source imports resolve.
- The 45 original export files are recorded in `export-sha256.json` for byte-level verification.
- Frontend passed `npm install` and `npm run build` in an isolated temporary copy, including TypeScript checks and Vite production output (Vite 6.4.4 resolved). Generated dependencies, lockfile, and build output were not copied into this baseline.
- Backend dependencies installed in an isolated Python 3.14 virtual environment. Offline FastAPI checks passed for module import, health, built frontend serving, API docs, invalid-agent rejection, combined task listing, draft create/update/delete, and active-task update/delete rejection. SQL and task loaders were replaced by in-memory test doubles, and the Workspace client was blocked from remote calls. These checks establish local routing/contracts, not live database behavior or frontend browser interaction.
- No verification should be interpreted as live Genie, SQL, Job, Alert, or delivery acceptance. Dependency ranges remain unpinned; this build does not establish reproducibility on all future resolutions.

## Recommended next development order

1. Recover the nonempty Cockpit artifacts and export Job/Alert configuration without changing those resources.
2. Add dependency locks and meaningful offline contract checks in a separate development commit.
3. Address Registry boolean conversion, degraded-state reporting, timeouts, and mutation consistency.
4. Define lifecycle transitions and execution compatibility before integrating Registry tasks into the executor.
5. Run Databricks integration acceptance only when explicitly authorized.
