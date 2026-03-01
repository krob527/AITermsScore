# GitHub Copilot Usage — getAITermsScore

This document summarises how **GitHub Copilot** (via the VS Code Copilot Chat agent) was used throughout the development and deployment of getAITermsScore, submitted to the **Microsoft Agent League** competition.

---

## Summary

GitHub Copilot acted as a continuous pair programmer across the full development lifecycle — from debugging a broken UI, to architecting an Azure deployment, to diagnosing live production failures and cleaning up the codebase. It was used interactively in VS Code, combining code edits, terminal commands, file reads, and Azure CLI operations in the same conversation thread.

---

## What Copilot Did

### 0. Initial Setup, SDK Integration & Local Debugging

This phase covers the work done before Azure deployment — getting the tool running locally from a fresh `.env` configuration.

**Environment setup guidance**
- Read `.env` and `README.md` to verify configuration, identified that `AZURE_SUBSCRIPTION_ID` still contained a placeholder (optional field, noted but left).
- Provided step-by-step onboarding: `az login` → create/activate `.venv` → `pip install -r requirements.txt` → first `python main.py score` run.

**SDK import fix — `ThreadMessageRole` → `MessageRole`**
- The initial run failed with `ImportError: cannot import name 'ThreadMessageRole'`. Inspected the installed `azure.ai.agents.models` module at runtime to enumerate available names, identified the rename, and updated all three references in `agent/runner.py`.

**Endpoint placeholder fix — `override=True` + embedded placeholder detection**
- Second run failed because a system-level environment variable `AZURE_AI_PROJECT_ENDPOINT` containing the literal placeholder value was overriding the `.env` file (`load_dotenv(..., override=False)` was the default). Two fixes applied to `config.py`: changed to `override=True` so `.env` always wins; improved `_require()` to catch embedded placeholders like `https://<hub-name>...` (not just bare `<` values).

**SDK architecture refactor — `AIProjectClient` → standalone `AgentsClient`**
- After fixing the endpoint, the run failed with `'AgentsClient' object has no attribute 'create_thread'`. The SDK had changed its API surface. Ran multiple introspection commands (`dir()`, `inspect.getsource`, `inspect.signature`) to map all available methods on the new client. Rewrote both `agent/setup.py` and `agent/runner.py` to use the new sub-operation groups: `client.threads.create()`, `client.messages.create()`, `client.runs.create_and_process()`, and `client.messages.list()`.

**`MessageRole.ASSISTANT` → `MessageRole.AGENT`**
- After the refactor, a run failed with error string `"ASSISTANT"` — an `AttributeError` at runtime. Inspected `MessageRole` enum values, found the correct attribute is `MessageRole.AGENT`, and updated both usages.

**Bing Grounding diagnosis and replacement with DuckDuckGo**
- The agent produced `server_error` with zero run steps — indicating a backend rejection before any tool call. Ran targeted diagnostic agents: confirmed model-only call works fine; confirmed Bing-attached call fails immediately. Tried all `BingGroundingTool` payload variants (connection name, full resource ID, manual tool definition) — all failed identically.
- Listed all AI Foundry project connections via `AIProjectClient.connections.list()` to verify the connection ID and type (`ApiKey`, `pocnonprofit`). Fetched Microsoft documentation confirming Bing Grounding is incompatible with `gpt-5` models; updated `.env` to `gpt-4.1`.
- After the model switch, a new `server_error` revealed the `update_agent` call was not updating the model on the existing agent. Fixed `setup.py` to pass `model=cfg.model_deployment` in `update_agent`.
- Subsequent 401 from Bing API (`"Access token is missing or invalid"`) confirmed the Bing API key in the connection was invalid. The user confirmed both keys failed and key regeneration also failed.
- **Replaced Bing Grounding entirely with DuckDuckGo** via the `ddgs` package wrapped as a `FunctionTool`. Installed `ddgs`, defined a `web_search(query)` -> `str` function, registered it as a `FunctionTool`, called `client.enable_auto_function_calls(toolset)` so the SDK auto-executes tool calls during `create_and_process`. Added `ddgs>=1.0.0` to `requirements.txt`. Verified with a live search before deploying.

**Flask web application — initial creation**
- Created `app.py` (Flask server with SSE streaming endpoint) and `templates/index.html` (full single-page UI) from scratch, including: radar/spider chart via Chart.js, per-dimension score rows with colour-coded fill bars, letter grade badge, full Markdown report pane, real-time progress log, and reset button. Added `flask` and `gunicorn` to `requirements.txt`.

**Parse error fix — list vs dict from agent**
- After the first web UI run, `output_writer.py` crashed with `'list object' has no attribute 'items'` — the agent returned a JSON array instead of a dict. Fixed `parse_scorecard()` to return `{}` when the parsed JSON is not a dict, and fixed `output_writer.py` to guard the template call. Also noted the root issue: the old system prompt instructed clause extraction (list output), which conflicted with the UI's expected numeric scorecard format.

---

### 1. Bug Fixes — Web UI

- Diagnosed why the **overall score was displaying "—"** in the browser. Traced the `NaN` chain through JavaScript, identified the root cause as an absent `overall` field being computed client-side from a missing path, and fixed it by computing `overall_score` server-side in Flask and sending it as a dedicated SSE payload field.
- Removed a stray ` ```json ``` ` block that was appearing in the full report pane — identified that `marked.parse()` was receiving the raw Markdown including the trailing JSON block, and stripped it before rendering.
- Fixed the browser incorrectly detecting the page language as French by adding a `Content-Language: en` HTTP response header via a Flask `@app.after_request` hook.

### 2. System Prompt Rewrite

- Rewrote `prompt/system_prompt.md` from scratch. The original prompt was producing clause-extraction JSON; Copilot rewrote it to instruct the agent to produce a structured Markdown scorecard with per-dimension scores (0–5), rationale, key findings, and a trailing machine-readable JSON block — matching what the parser and UI expected.

### 3. Azure Deployment — Infrastructure as Code

- Designed and created the full Azure deployment from scratch:
  - `infra/main.bicep` — App Service Plan (Linux), Web App with System-Assigned Managed Identity, Application Insights, Log Analytics Workspace
  - `infra/main.parameters.json` — azd parameter bindings
  - `azure.yaml` — Azure Developer CLI service definition
  - `startup.sh` — App Service startup script activating the Oryx virtualenv before launching gunicorn
  - `.python-version` — pins Python 3.12
- Iterated through two `azd up` failures: added a missing `azd-env-name` tag required by azd 1.17 to target the correct Web App resource.
- Updated Bicep from F1 Free to **B1 Basic** default after the user upgraded the plan, and added `alwaysOn: true` to prevent cold-start recycles.

### 4. Permission / IAM Debugging

- Diagnosed a cascade of `PermissionDenied` errors from the Azure AI Agents API (`agents/read`, `agents/write`). Traced each error to specific API calls (`list_agents`, `update_agent`) and worked through incremental role assignments (Azure AI Developer at project, account, and resource group scope; Cognitive Services Contributor; Azure AI User).
- Final resolution: rewrote `agent/setup.py` to use a **zero-API-call fast path** — when `AGENT_ID` is set as an App Service environment variable, a `SimpleNamespace(id=agent_id_env)` stub is returned immediately, bypassing all agent management API calls entirely.

### 5. Cold-Start / Container Timeout Diagnosis

- Downloaded Docker container logs via the Kudu REST API using publishing credentials, parsed them, and identified the root cause: F1's shared CPU throttle causing gunicorn's Python import time to vary from 25s to 182s — occasionally crossing the 230s `ContainerTimeout` hard limit.
- Recommended and applied the fix: upgrade to **B1** (dedicated vCore) and set `WEBSITES_CONTAINER_START_TIME_LIMIT`.

### 6. Code Cleanup

- Scanned all source files and removed:
  - Unused `import tempfile` in `config.py`
  - Dead `html_file` field from the SSE payload in `app.py` (never read by the JavaScript)
  - Dead list-reshaping fallback in `agent/runner.py`'s `parse_scorecard()` — obsolete since the system prompt rewrite; the agent now always returns a dict
  - Incorrect score badge thresholds in `output_writer.py` (were using a 0–10 scale; corrected to match the actual 0–5 rubric)

### 7. UI Enhancement — Disclaimer

- Added a styled legal disclaimer card to the bottom of `templates/index.html`, above the footer, using the site's existing CSS variables for consistent styling.

### 8. Documentation

- Rewrote `README.md` from scratch to accurately reflect the current codebase — removing stale references to Bing Grounding and `rubric.md`, and adding: architecture diagram, Azure deployment walkthrough, App Settings reference table, IAM role assignment commands, and an updated troubleshooting guide.
- Created this `copilot.md` file.

### 9. Overall Score Display Fix (Intermittent "–" Bug)

- Diagnosed an intermittent bug where the **Overall Score** panel displayed "–" even after a successful run.
- Root cause: the AI agent occasionally returns the `overall` field as a string (e.g. `"3.5"`) rather than a number. Three independent code paths all used strict `isinstance(..., (int, float))` or `typeof ... === "number"` checks, which silently rejected string numerics and left the value as `null`/`None`.
- Fixed across three files simultaneously:
  - `agent/runner.py` — added `float()` coercion for string `overall`; extended dimension score extraction to also accept string digit values
  - `app.py` — same coercion in the server-side `overall_val` fallback computation
  - `templates/index.html` — replaced all `typeof === "number"` checks with `parseFloat()`, which correctly handles numbers, numeric strings, `null`, and `undefined`; updated the final display guard from `!== null` to `!isNaN()`

### 10. Browser French Language / Translate Bar Fix

- Diagnosed why Chrome repeatedly offered to translate the page from French despite `lang="en"` being set on `<html>` and `Content-Language: en` being sent in the HTTP response header.
- Root cause: Chrome's ML-based language detector analyses visible page text, not the `lang` attribute. Because the AI-generated report body contains cited legal clauses that include French words (e.g. from GDPR-referencing sections of vendor policies), the detector classified the page as French and showed the translate bar.
- Applied the two directives Chrome actually respects:
  - `<meta name="google" content="notranslate">` — Chrome-specific tag that unconditionally suppresses the translate bar
  - `translate="no"` on the `<html>` element — W3C standard attribute honoured by all compliant browsers

---

## Copilot Capabilities Used

| Capability | How it was used |
|---|---|
| **Code editing** | Multi-file simultaneous edits (`multi_replace_string_in_file`) for atomic changes across Python, Bicep, HTML, and shell scripts |
| **Terminal execution** | Running `azd`, `az`, `gunicorn`, and PowerShell commands directly, reading output, and iterating |
| **File reading** | Reading source files, log files, and config to inform decisions before editing |
| **Log analysis** | Fetching and parsing Azure App Service Docker logs via the Kudu API to diagnose container failures |
| **Infrastructure authoring** | Writing Bicep templates, parameter files, and azd config from scratch |
| **Iterative debugging** | Multi-turn diagnosis loops — read error → hypothesise → apply fix → verify — across both local code and live Azure resources |
| **Azure CLI operations** | Role assignments, app settings, webapp restarts, log downloads |

---

*Generated with GitHub Copilot — Claude Sonnet 4.6*
