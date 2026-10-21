# Milestone 2B — Natural-language draft creation

This records the 2B implementation and checks. Subsequent local execution integration is documented in [Milestone 2C](MILESTONE-2C.md).

## Implemented locally

The existing task modal now accepts a natural-language requirement. `POST /api/tasks/generate` makes one structured-output inference request, validates it, and returns configuration without writing to Delta. The user can edit the populated form, inspect the preview and original requirements, then explicitly save with the existing `POST /api/tasks/create` endpoint.

- Model output is a suggestion, not an execution plan that has passed business validation. Assumptions, warnings, and missing information remain visible during editing/preview and after saving, under `report_config.draft_review`.
- Conditional requirements cannot be accepted as `always` triggers. Ambiguous conditions can be saved as `unconfigured` drafts. The model has no schema tools and cannot invent SQL; nonempty proposed SQL must be present in the user's input, and is still marked unverified. This is conservative validation, not proof that all natural-language intent has been preserved. Review is required.
- All new and updated drafts are forced to `enabled=false`. Existing records are not migrated. Dashboard counts exclude drafts from enabled/disabled and show a separate draft count. SQL string booleans are decoded explicitly.
- Schedule controls cover daily, weekly (one weekday), and interval (days plus optional start date), with time and IANA timezone. The chosen schedule is included in preview and persisted as JSON. Daily/weekly cron is derived for compatibility; interval has no misleading cron. This does not create or change a scheduler. Unsupported monthly/quarterly/yearly/weekday-only requests are explicitly flagged for review; other unsupported requirements must also be surfaced by the model.
- No Registry schema change is required: review metadata and new schedule fields use existing JSON string columns. Existing Task Manager files and external resources are unchanged. Integration of these fields with the executor belongs to 2C.

## Required real model configuration

| Item | Required value / action |
| --- | --- |
| `TASK_PARSER_ENDPOINT` | Existing Unity Gateway service FQN, for example `system.ai.gpt-oss-20b`, or a legacy endpoint name; not a Genie Space ID, URL, or token |
| Model interface | FQN selects `/ai-gateway/mlflow/v1/chat/completions` with `model`; a legacy name selects `/serving-endpoints/{name}/invocations`. Both use chat `messages`, nonstreaming `response_format.type=json_schema` |
| App identity | Unity Gateway: `USE CATALOG`, `USE SCHEMA`, `EXECUTE`, with no effective denial. Legacy custom endpoint: **CAN QUERY**, plus applicable model access |
| Authentication | Existing SDK `WorkspaceClient().config.authenticate()` and its configured Workspace host; credentials stay server-side |
| Local authentication | SDK default credential chain, configured by the developer outside source control; tests do not need it |

Read-only discovery on 2026-10-20 confirmed `system.ai.gpt-oss-20b` exists and advertises Chat Completions, and its legacy endpoint is READY and not route optimized. The App principal `fbc62528-8dda-4dad-8835-c2c22016b422` (`app-164q7w business-ai-hub`) belongs to `account users`. That group has `USE CATALOG` on `system`, and `USE SCHEMA` and `EXECUTE` on `system.ai`. The principal-specific effective permissions query was denied for lack of READ METADATA; a real App-identity inference acceptance check remains pending. The discovery user's own CAN_MANAGE result is not evidence of App permissions. No endpoint has been created, permission changed, App deployed, Job started, or notification sent as part of 2B. One explicitly authorized CLI-user inference was performed on 2026-10-20, as recorded below.

`task_llm.generate_configuration(workspace, requirements)` is the small provider adapter. The handler consumes its chat-completion-shaped response; another provider can replace that function and normalize its response while retaining schema validation and the UI workflow. SDK authentication is used directly, with a single `requests.post` call, no inference retry or repair loop, no redirect following, connect timeout 5s and read timeout 60s. The browser timeout is 75s. Errors are sanitized; missing configuration returns HTTP 503 with `Model not configured`. There is no runtime mock/fallback configuration.

`.env.example` documents the variable; the app does not load `.env` automatically. To run with a real endpoint later, set the variable in the process/App environment. The configured Unity Gateway route and one representative proposal have passed a real CLI-user inference check. App-identity permissions and broader business acceptance remain pending. Some Databricks model families require a different native API and will need adapter changes; do not assume every available endpoint supports this contract.

The adapter selects between Unity Gateway and the standard legacy Workspace invocation route solely from the configured identifier. It does not use route-optimized endpoint URLs. A route-optimized endpoint requires a later adapter change and endpoint-scoped OAuth handling; no automatic discovery or fallback requests are performed.

References: [Databricks structured outputs](https://docs.databricks.com/aws/en/machine-learning/model-serving/structured-outputs), [Foundation Model REST API](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference), [Serving endpoint access control](https://docs.databricks.com/aws/en/security/auth/access-control/#serving-endpoint-acls), [App model-serving resources](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/model-serving).

## Local verification

Run from the application directory, `business-ai-hub/` inside the repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
npm ci
npm run test --workspace business-ai-hub-frontend
npm run build
```

Automated model and persistence tests use explicit fixtures/mocks only. They cover structured-output failures, no configured model, timeout/permission failure sanitization, conditional downgrade rejection, draft-only saving, user edits, scheduling, statistics, and late response handling. They do not establish real model quality, live Delta behavior, or production deployment readiness. Frontend dependencies are locked in `business-ai-hub/package-lock.json`; Python requirements retain the original version-range approach.

For UI inspection, a temporary local preview used exported YAML files, disabled all remote clients/persistence, and left the real model unconfigured. The browser showed the explicit configuration error and the still-editable manual form. No fake AI configuration was shown as a real model result.

Initial verification on 2026-10-20: all 16 backend tests and 9 frontend tests passed; TypeScript validation and Vite production build passed. Two additional backend tests cover Unity Gateway routing, preserved legacy behavior, and invalid identifier rejection. One authorized real model parser check is documented below; live Registry persistence, App identity, Genie evaluation, and execution integration remain untested.

## Ownership boundary

The application team owns task configuration/lifecycle, scheduling and execution integration, run/report history, notifications, and failure handling. A separate team owns metric views, metric definitions, data installation, Genie A1/A2 instructions and examples. Empty Cockpit snapshot exports do not block framework development. The system must accept configured Genie Spaces and task conditions without hardcoding metric-view names or inventing business SQL. Business correctness acceptance depends on that team's installed definitions; local infrastructure tests use explicit fixtures only.

Read-only discovery confirmed existing Job `997638013141610` remains PAUSED, uses one concurrent run, and invokes the existing Task Manager Workspace entry point. Do not change its schedule or run it without explicit authorization.

## Authorized live parser check — 2026-10-20

[Machine-readable evidence](validation/milestone-2b-live-parser-2026-10-20.json).

- Existing service: `system.ai.gpt-oss-20b`; authenticated as the current CLI user, not the App Service Principal.
- Exactly one inference POST, no retry, maximum output 3000 tokens; completed in 7.38 seconds with finish reason `stop`.
- Usage reported: 1036 prompt tokens + 896 completion tokens = 1932 total tokens. No monetary cost was calculated.
- Pydantic validation and eight local acceptance checks passed. The proposal preserved weekly Monday scheduling, the brand, previous-week comparison, strict decline threshold over 15%, an agent condition instead of `always`, and `enabled=false`. It generated no SQL.
- Suggested 09:00 / Asia/Shanghai were disclosed as assumptions. The model also listed a start date as missing for this weekly task; this is not required by the current weekly schedule schema and illustrates why generated review notes still need human review. Metric availability was not checked.
- The returned task ID `local_validation_only` was assigned locally for validation. No Registry record was created and no condition/report executed. No deployment, Job run, notification, or resource change occurred.
- This confirms one parser inference under CLI-user authentication. It does not establish App identity access, general model reliability, installed metric correctness, or an operational scheduled workflow.


## Latest App-identity integration boundary — 2026-10-20

The owner checked Catalog Explorer and confirmed `EXECUTE` for **All account users**, inherited from `system.ai`, on Model Service `system.ai.gpt-oss-20b`. Existing read-only discovery also showed group-level `USE CATALOG` on `system` and `USE SCHEMA` on `system.ai`; the App principal's final effective parent grants have **not** been independently established. Missing personal READ METADATA does not block local implementation.

`TASK_PARSER_ENDPOINT=system.ai.gpt-oss-20b` is the selected example configuration; it remains an environment override and uses the existing Unity Gateway adapter. `.env.example` is not automatically loaded, and the currently running local test server keeps real parsing disabled.

The owner's later instruction supersedes the unused three-call CLI-user test allowance: no further personal-user inference is enabled. Exactly one App-principal inference may be performed when an App-authenticated integration environment is available. No such request has yet been made. The local environment currently has only the personal `business-ai-hub` CLI profile, no matching App OAuth client/secret environment or M2M profile. Do not copy credentials into chat, create another principal, provision resources, or fall back to the CLI user.

`scripts/check-app-parser.py` is prepared for that acceptance step. It requires the existing App client ID and OAuth M2M authentication, checks the authenticated SCIM ID before inference, and accepts `--infer` only as an explicit one-request action. A personal profile fails the identity gate before inference. Run inside an already authorized App-authenticated environment or with an existing approved App M2M profile; do not deploy merely to run this check.

Permission failures now preserve only safe provider evidence: HTTP status, error code and named missing privileges. HTTP 401/403 causes one request and an immediate error; no retry, repair, draft write or provisioning. A generic denial is explicitly described as not naming a privilege; it is not assumed to mean missing EXECUTE. Raw error messages and credential-like text are not echoed to the browser.

Latest offline verification: 51 backend tests pass, including USE CATALOG / USE SCHEMA / EXECUTE errors, generic denials, structured privilege evidence and non-JSON authentication failures. The unchanged frontend's previous 16 tests and production build passed. App identity inference, effective parent access, deployment and cloud execution remain pending.
