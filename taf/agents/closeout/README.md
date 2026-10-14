# Agent 5 - Closeout (optional n8n integration)

Posts the verdict, the report and the Xray payloads to an n8n webhook, which can
then update Jira and import into Xray.

**Not used by local runs.** The dashboard, `python -m taf run` and the scripts in
`scripts/` stop after the report; they never call n8n. Closeout only runs in the
full LangGraph pipeline started with `POST /qa/runs`, and only does something when
that request includes `n8n_callback_url`. Without one, it records "no
n8n_callback_url provided" and the run ends normally.

It stays as an alternative way to wire the service into Jira. The current flow
instead keeps Xray test cases in the test repo for review, and exports them with
`python -m taf xray export`.

| | |
|---|---|
| Runs on | HTTP, no model |
| Reads | `final_report`, `execution_passed`, `xray_csv_content`, `xray_tests_json`, run metadata |
| Writes | `callback_delivered` |
| Settings | `CALLBACK_TIMEOUT_SECONDS` |

## Files

| File | What it is |
|---|---|
| `agent.py` | Builds the callback payload and POSTs it, with retries on gateway errors (never on 500, to avoid duplicate Jira comments). |

The payload format and an outline of the n8n workflows are in the main README,
under "Optional: n8n integration".
