# Direct report email — local implementation

## Manual project-adapter verification — 2026-10-21

The owner authorized one fixed manual test to the alternate mailbox referenced by the existing Databricks notification destination. That destination was read without modification. Connected Google/Gmail tooling does not provide the project's unattended OAuth credentials. Project authorization was initiated using the existing downloaded Desktop OAuth client; no client/API/resource was created. The optional `--expected-email` mode in `scripts/authorize-gmail.py` requests send access plus OpenID/email solely to verify the selected sender (no inbox access), stores credentials outside Git with mode 0600 and sends nothing.

`scripts/test-email.py --sender <authorized-sender> --recipient <approved-recipient>` prepares a fixed plain-text preview outside Git. Only `--send` invokes the existing `GmailProvider`, and it checks the project-authorized sender first. An exclusive local receipt prevents a second send of the same test; unknown outcomes are not retried. It does not generate a report, run tasks, activate email delivery or use the Codex connector to disguise a missing project integration. Authorization completion, API acceptance and actual delivery must be reported separately; preparation alone is not a successful send. Background `TASK_EMAIL_ENABLED` stays false.

Result: project OAuth completed with the expected sender, and one real send through the project's `GmailProvider` was accepted by Gmail with a provider message ID. The local receipt is kept outside Git. No report generation, task execution, deployment or scheduled email was used. Recipient inbox arrival still requires recipient confirmation. An offline check also verified that preview sends nothing and repeating the same fixed test cannot invoke the adapter twice.

The new delivery path is **report + approved CSV → HTML template → Gmail adapter**. It does not use a model to export CSV or send mail. Existing report prompts / Genie instructions guide analysis; HTML email templates only format delivery. Team-specific content and queries can be installed later.

## Available now

- Task drafts store explicit To/Cc addresses, an installed email template ID and selected CSV dataset IDs. The LLM cannot invent recipients or export SQL. Review shows these settings before activation. Saving a draft never sends email.
- The existing executor can select Gmail as a notification provider, independently of the legacy SQL Alert path. Legacy Outbox remains the default until an approved migration.
- Email templates are trusted, versioned Jinja files under the existing Task Manager directory. A default template is included. Report Markdown is rendered with raw HTML disabled; normal template values are escaped.
- CSV comes directly from curated SELECT files, using the report's saved execution-date context. No data rows pass through an agent. Exports preserve columns, Unicode, commas/quotes/newlines and decimal values; use UTF-8 BOM and escape formula-like text for spreadsheet use. CSV carries no Excel cell types; Excel can still infer types when opening it.
- Limits fail explicitly rather than silently truncating exports. Current application defaults: 100,000 rows per export (configurable up to 1,000,000), 12 MiB per CSV, five CSVs and 20 MiB for complete encoded MIME. Larger data delivery can be added later without changing agent logic.
- `EMAIL_ACCEPTED` records Gmail's returned message ID, not recipient delivery. Failed/uncertain/interrupted sends are not automatically replayed. Gmail Message-ID is for reconciliation, not an exactly-once guarantee. CSV metadata (row/column counts, size, hash) is recorded with the run.

## Interactive local testing

Run `python scripts/dev-local.py` from the repository and open **http://127.0.0.1:8765**. Unlike the UI-only preview, this mode saves actual drafts to `~/.local/share/business-ai-hub/dev.sqlite3`. It uses the existing App CRUD with a thin SQLite dialect adapter. Databricks persistence, Jobs and email remain disabled. The later [trigger SQL integration](CONDITION-SQL.md) enables explicit Genie/SELECT tools and local review state transitions; the BI chat/report Agent remains disabled in local mode. The test database is separate from the production Delta Registry.

Recurring requests such as “Every Friday, give me a competitor's weekly report” in the analysis composer now hand off to the task configuration form without calling Genie. There is also an explicit **设置自动任务** button. Generation fills an editable draft; only explicit Save creates the task, then the UI switches to **我的任务** and refreshes the list. This is intent routing, not fake AI configuration.

Model parsing is off by default. Following separate user authorization, opt in with `--parser-endpoint system.ai.gpt-oss-20b --profile business-ai-hub --model-limit 3`. Only explicit generation requests call the real adapter, capped at three attempts for that server process. This can incur inference charges; it never writes a Databricks draft or executes a task. Do not restart/re-enable to exceed an authorized limit.

## UI-only preview

Run `python scripts/preview-local.py` from the repository after installing backend development dependencies and building the frontend. Open **http://127.0.0.1:8765** → **我的任务 → 创建任务**. The yellow banner states the scope: local YAML only; no Registry saves, AI calls, Jobs or email. Form editing and preview work. CSV choices remain empty until the team installs approved datasets; no fake production datasets are included.

## Gmail authorization boundary

The personal Gmail connector profile was read successfully in Codex. Its send tool supports MIME attachments, but no message/draft was sent or created. The connector authorization is not a credential available to the deployed Databricks Job.

For unattended project sending, a separate Google OAuth grant is required with `gmail.send` only. See [Google Gmail sending](https://developers.google.com/workspace/gmail/api/guides/sending), [scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) and [offline OAuth access](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

One-time user setup, when ready:

1. Use a Google Cloud project with Gmail API enabled and a **Desktop OAuth client**; keep its downloaded client JSON outside this repository. API enablement alone does not authorize a sender.
2. Install `business-ai-hub/requirements-dev.txt`, then run `python scripts/authorize-gmail.py --client-secrets /absolute/private/path/client.json`. Choose the intended personal Gmail account. The helper requests send-only offline access, writes credentials outside Git with permissions 0600, and sends nothing. It has been prepared but not run.
3. Put `client_id`, `client_secret`, `refresh_token` in secret-backed Job configuration as `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`. Do not paste credentials into chat or source control. Google OAuth test-mode/token-expiry behavior must be reviewed before ongoing unattended use.

## Later approved rollout (not performed)

- Apply the extended proposed history schema (`database/business_task_runs_v1.sql`). It adds `provider_message_id`, `notification_error_code`, `artifacts_json`; there is no Registry DDL change. If an earlier history table has already been created separately, add those three STRING columns during the approved migration.
- Install the existing Task Manager requirements. Configure `notification.email`: sender, explicit allowed recipients, templates and curated exports. OAuth secrets are read from environment bindings, never from settings YAML.
- Export mapping example (team-owned SQL file, not an invented business query):

  ```yaml
  exports:
    sales_wide:
      label: 销售日报明细
      sql_file: exports/sales_wide.sql
      filename: 销售日报明细.csv
      max_rows: 100000
  ```

- App `TASK_EMAIL_ENABLED` remains false until this setup is verified. Activation checks recipients, installed templates/datasets and the project mail gate.
- Before choosing `notification.provider: gmail`, disable the existing Job's **send_alert** task during an approved rollout, then set `legacy_alert_disabled: true`. The executor refuses Gmail mode without this acknowledgement. This prevents running both email paths together; the setting does not modify the Job. The old Job and Alert are unchanged now.
- Leave `execution.enabled`, `notification.email.enabled` and App activation gates off until authorized acceptance. A real personal-mailbox test needs an explicit recipient/message and send authorization. Gmail acceptance, OAuth refresh, live CSV query and actual mailbox delivery are unverified.

## Verification

Offline tests parse real MIME/CSV content, preserve 500-column fixtures, enforce limits, check single Gmail POST behavior and unknown-outcome protection, and run the existing executor with test adapters. One Agent call produces the report; CSV/email do not add Agent calls. UI tests save explicit mail settings as a disabled draft without sending. No cloud model, SQL, Job or email was invoked for these tests.

Current local verification: **45 backend tests, 16 frontend tests and TypeScript/Vite build passed**. The local database tests use actual HTTP and SQLite persistence, not mock persistence. Cloud model/mail/Spark integrations retain the explicitly stated acceptance limits.
