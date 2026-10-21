# Workspace migration reference — 2026-10-21

The current runnable application is `business-ai-hub/`; its Task Manager source is
`business-task-manager-v1/business-task-manager-v1/`. Deploy those maintained roots.
`workspace-reference-2026-10-21/` is an archived, read-only capture of older cloud
sources and resource definitions, **not a second application to deploy or execute**.
Old Agent IDs, URLs, enabled schedules and recipients in this archive are historical.
Do not import the archived Job/Alert settings into the company Workspace as-is.

The manifest records each original Workspace path, SHA-256, byte size, exclusions,
and metadata errors. Capture used only Workspace/Genie/Jobs/Catalog metadata APIs:
no inference, business SQL submission, data-row export, deployment or cloud writes.
Notebook SOURCE exports omit execution outputs. Archived AGENTS/CLAUDE documents
are renamed `.reference.md`; they are source evidence, not repository instructions.
`.env`, credentials, runtime outputs, caches and deployment caches are excluded.

Included:

- Hub and Task Manager cloud source snapshots, task prompts, SQL, templates.
- Previous cockpit / deployment / probe source and data-generation notebooks.
- Data dictionary and synthetic-data documentation.
- The former two Genie Agents' serialized instructions, examples/benchmarks and
  data-source references, for the team maintaining the new Genie One knowledge.
- Metadata for 81 catalog tables/views, including **9 Metric View definitions**.
  These are definitions/references, **not a copy of table data**.
- Existing Task Manager Job configuration, as reference only.

Still needed in the company environment:

- Actual company tables/data pipelines, refreshed Metric Views, functions, masks,
  row filters, groups and grants. Definitions can be adapted by the metrics team;
  this repository does not recreate their production data or identities.
- Genie One knowledge/ontology configuration in the new Workspace, MCP access,
  user SQL Warehouse access and individual Unity Catalog grants.
- A company OAuth application and HTTPS callback, metadata storage identity,
  network access, mail authorization and durable creator delegation for scheduling.
- Existing Alert/resource subscriptions, secrets and account integrations are not
  portable login keys. The old Alert sender is not required by the new email path.

Local SQLite drafts, report history, email receipts and OAuth credentials stay
outside Git. The identity migration script backs up the local database and binds
its single-user drafts explicitly; it does not upload them to the company.

To capture again on an approved machine:

```bash
python scripts/export-workspace-reference.py --profile SOURCE_PROFILE \
  --destination migration/workspace-reference-YYYY-MM-DD
```

Review the manifest, credential scan and exported source before committing. Never
run archived provisioning notebooks as part of application startup.
