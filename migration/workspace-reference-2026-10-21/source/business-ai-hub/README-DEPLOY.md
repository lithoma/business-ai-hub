# Business AI Hub — Milestone 1 (React UI)

## Included
- **root `package.json`**: tells Databricks Apps to install npm dependencies and run frontend build automatically on deployment.
- `frontend/src`: React 18 + TypeScript SPA; 3 responsive Chinese-language pages.
- `patches/APP_PY_APPEND.txt`: exact tiny change to **append** to your existing `app.py`.
- Does **NOT** include or replace existing `app.py`, `app.yaml`, `requirements.txt`, Genie logic, or Task Manager files.

## Recommended: import through Databricks browser UI — no local Node/npm needed

1. Open Databricks **Workspace** → `/Workspace/Users/lithomas@wustl.edu/business-ai-hub/`.
2. Use the folder’s **⋯ → Import** action to import the accompanying `business-ai-hub-frontend-overlay.zip`. Databricks automatically extracts imported ZIP contents.
3. IMPORTANT: verify `package.json` is **at the same level as `app.py`**, and there is a `frontend/src/App.tsx` underneath it. If a nested `business-ai-hub-...` directory was created, move/import the contents into the app root instead.
4. Open the existing, proven `app.py`, **append** the content of `patches/APP_PY_APPEND.txt` **at its end**. Do not replace or alter existing API handlers.
5. Keep existing `app.yaml` Python startup command and `requirements.txt` unchanged. Deploy **the same existing `business-ai-hub` App** using the same Workspace directory.
6. Databricks deployment detects root `package.json`, runs `npm install` and `npm run build`, producing `frontend/dist`, then uses the existing `app.yaml` command to run FastAPI. If build fails, inspect **deployment build logs** (NOT Genie API).
7. Open `https://business-ai-hub-7474644941313823.aws.databricksapps.com/`. The React app should appear. `/docs` and `/api/*` should continue to work.

**Databricks docs:** https://docs.databricks.com/aws/en/dev-tools/databricks-apps/deploy

## Alternative: Databricks CLI (only if UI import is inconvenient)

Extract the ZIP somewhere locally, then import that extracted folder into the EXISTING app root:

```bash
databricks workspace import-dir ./extracted-folder /Workspace/Users/lithomas@wustl.edu/business-ai-hub --overwrite
```

Then append `APP_PY_APPEND.txt` manually and deploy the existing app in the browser UI.

## Pages and API contracts

- **AI 分析**: switch A1/A2; real POST `/api/chat` with `{"question":"...","agent":"genie_a1"}`; markdown display, suggestion prompts, errors.
- **我的任务**: real GET `/api/tasks` list; manual refresh; read-only.
- **分析报告**: labeled as upcoming; no fake records.

## Verification and scope

- `/` → three UI tabs.
- `/api/health` → `{ "status": "ok" }`.
- `/api/tasks` → existing live config list; task count/fields depend on original YAML.
- `/api/chat` → already verified Genie A1 backend; make **one** query from the frontend.
- Browser requests use same-origin App login; tokens remain backend-only. No OBO or unnecessary Auth work.
- Original Task Manager job, files, and scheduling are unchanged.
- Browser chat history is in memory only, not persisted.
- Frontend code is prepared, **not compiled or deployed from this environment**; the deployment build will install dependencies and run TypeScript validation.
