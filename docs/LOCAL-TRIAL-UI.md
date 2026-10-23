# Local report and email trials

**2026-10-22 update:** canonical trial/preview/send APIs are now registered by FastAPI independently of this launcher. Run state and MIME previews persist in the existing run ledger; the old local routes remain as history adapters. Per-task schedule planning is available without starting a timer. See [current APIs, recovery rules and remaining integration](RUNS-AND-SCHEDULING.md). The historical launcher-only description below applies to the original trial implementation.

The local platform now supports: save a task → review/activate locally → **试运行一次** → inspect the condition and report → **预览邮件** → explicitly **确认发送一次**. Trial buttons are on saved conversation cards and Registry tasks. Report history links to a preview of persisted manual reports, including the two already mailed reports; accepted/unknown sends cannot be repeated. Trials also work on complete draft configurations without starting the scheduler. They do not bypass the activation checks for scheduled execution.

Start the existing loopback launcher with `--manual-trials --trial-recipient <authorized address>` in addition to the normal parser/analysis flags. Sender identity is read from the existing project Gmail authorization outside Git. No Google token is sent to the browser. Without this opt-in the deployed App has no trial/send routes and the frontend hides the controls. Current single-user testing uses the CLI identity, not App-SP/OBO authorization.

## Boundaries and recovery

- A trial snapshots the task version/date and persists its state in local SQLite before starting a local worker. There are at most two concurrent trials. This is a user-requested local worker, not a Databricks Job or recurring scheduler.
- Repeated requests for the same version/date return the existing run. Failed trials require an explicit retry; one retry request cannot create multiple child attempts. Task changes produce a version conflict rather than running an outdated form.
- False conditions do not call the report Agent or mail provider. Unknown/null conditions fail visibly. Report generation reuses the existing Task Manager report adapter; primary SQL conditions reuse the existing bounded read-only query implementation. Qualitative conditions reuse the strict Genie boolean contract.
- A report never sends itself. Preview creates stable MIME bytes. Explicit send validates the preview fingerprint and atomically claims the run before one Gmail POST. Interrupted or uncertain sends become `EMAIL_UNKNOWN` and cannot be automatically replayed. Gmail acceptance is not confirmation of inbox delivery.
- Startup marks interrupted trial workers as failed without replay. Condition/report/mail elapsed times remain on the record; reported provider use is recorded by the existing telemetry. Unknown Genie-internal usage and actual cost remain unknown.
- The current previous-observed-day adaptation is confined to the local adapter and test task definitions. Formal calendar-day/window logic is unchanged. The trial adapter checks the selected date and both GMV amounts against independent evidence, then forwards the actual comparison date into the report/email context. Original task snapshots can be restored as documented in [the SQL trial notes](CONDITION-SQL.md).
- Artifacts, MIME previews, run state and Gmail authorization stay outside Git. The browser polls only local status while the trial dialog is open; Codex does not monitor the website or run scheduled follow-ups.

## Validation and remaining integration

Two real local SQL tasks each passed two business dates, producing two non-triggered outcomes and two Genie-generated reports accepted by Gmail. See [live trial evidence](validation/local-task-observed-day-tests-2026-10-21.json). The new UI/state machine has focused offline tests for explicit confirmation, request deduplication, false/failed conditions, explicit retry, preview integrity and uncertain-mail replay protection. Existing report previews were checked live without another inference or send.

Company Workspace migration, creator-scoped durable authorization, business metrics/templates, App-SP permissions and cloud scheduler/deployment acceptance remain separate integration work. These features must not be treated as implemented multiuser security or production execution merely because local trials work.
