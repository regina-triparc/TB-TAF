# Setup Guide — Ollama only (no Claude / no Anthropic key)

Runs the **whole** QA pipeline on local Ollama models: every agent (triage, test design, Playwright code, report) uses Ollama. There is no Anthropic account, no API key and no cloud cost.

This is an **overlay** on the normal guides. Follow [SETUP-windows.md](SETUP-windows.md) (or [SETUP.md](SETUP.md) on macOS) phase by phase, and apply the changes below where they say so. Everything that isn't mentioned (Playwright repo, Paperclip, n8n, Jira, Xray) is identical.

> **Trade-off:** small local models write weaker test plans and Playwright code than Claude. Expect more specs that don't load, and review the generated specs before trusting a `QA Passed`. Use this mode for dry runs, demos and offline work, not as a replacement for the Claude setup.

## Sizing for this laptop (i7-1355U, 16 GB RAM, no discrete GPU)

Ollama runs on the CPU here, so the model has to be small or the laptop will crawl.

| Model | Download | RAM while running | Speed | Use |
|---|---|---|---|---|
| `qwen2.5-coder:3b` | 1.9 GB | 2.4 GB | **10.5 tokens/s** | **Default.** Keeps the machine usable |
| `qwen2.5-coder:7b` | ~4.7 GB | ~6 GB | roughly half that | Better code, but won't fit beside a browser in 16 GB |
| 14b and larger | — | 10 GB+ | — | Don't: leaves too little RAM for Chrome, Node and Paperclip |

The 3b row is measured on the hardware above (i7-1355U, 16 GB, CPU only); the others are estimates. A triage call takes **~40 seconds** and produces ~370 output tokens. Use one model for all agents so only one is ever loaded in RAM.

> **Free RAM is the thing that matters, not CPU.** The model needs 2.4 GB resident. With less than that free, Ollama thrashes against the pagefile and a 40-second call turns into one that never finishes — a 1800-second timeout is not enough. On the test machine a browser holding 4 GB was the difference between a 37-second triage call and a complete hang. Close your browser before a run, and check free RAM if anything seems stuck.

---

## Changes to Phase 2 — Local model

Set the memory limits **first**, so the server picks them up when it starts. On Windows, set these once, then **quit Ollama from the tray icon and start it again**:

```powershell
setx OLLAMA_CONTEXT_LENGTH 8192
setx OLLAMA_NUM_PARALLEL 1
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_KEEP_ALIVE 60m
```

On macOS/Linux, export the same variables before `ollama serve`.

| Variable | Why |
|---|---|
| `OLLAMA_CONTEXT_LENGTH=8192` | Ollama's default context is small and silently cuts long prompts (ticket + diff). 8192 tokens costs well under 1 GB extra. Needs a recent Ollama; run `winget upgrade Ollama.Ollama` if it's ignored |
| `OLLAMA_NUM_PARALLEL=1` | The pipeline makes one call at a time; more slots only waste RAM |
| `OLLAMA_MAX_LOADED_MODELS=1` | Never keep two models in memory |
| `OLLAMA_KEEP_ALIVE=60m` | Keeps the model resident between agents. **Don't shorten this.** Re-loading the model is what goes catastrophically slow when RAM is tight, and a QA run idles for minutes during `pnpm install` and Playwright. Set `-1` to keep it loaded indefinitely, or a few minutes if you need the RAM back promptly and have plenty free |

**If the tray app doesn't start the server**, run it yourself and leave it in its own tab. On the machine this guide was tested on, `ollama app.exe` ran but never spawned the server, and nothing listened on 11434 until this was run directly:

```powershell
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
```

Check with `Invoke-RestMethod http://localhost:11434/api/version` before moving on.

**2.2** Download the model (about 1.9 GB):

```powershell
ollama pull qwen2.5-coder:3b
```

**If the download stalls**, press `Ctrl+C` and run the same command again. It resumes from the bytes already on disk, so nothing is lost. On a throttled corporate network this took several attempts, each gaining a few hundred MB; a stalled pull sits at the same byte count with no CPU use, which is how you tell it apart from a merely slow one. This loop retries until it succeeds:

```powershell
while (-not (ollama list | Select-String 'qwen2.5-coder:3b')) { ollama pull qwen2.5-coder:3b }
```

✅ **Check:** the endpoint lists the model.

```powershell
curl.exe -s http://localhost:11434/v1/models
```

---

## Changes to Phase 4 — QA service `.env`

You don't need `ANTHROPIC_API_KEY`. In step 4.4, set these in `.env` **instead of** the Anthropic row:

| Variable | Value |
|---|---|
| `LLM_PROVIDER` | `ollama` |
| `OLLAMA_MODEL` | `qwen2.5-coder:3b` (or `qwen2.5-coder:7b`) |
| `OLLAMA_TIMEOUT_SECONDS` | `1800`. CPU generation is slow, and the default 300 s will time out |
| `OLLAMA_MAX_TOKENS` | `4096` (output cap per call; stops small models from rambling) |
| `MAX_DIFF_CHARS` | `12000`. The prompt must fit the 8192-token context |
| `MAX_ENV_LOG_CHARS` | `4000` |
| `MAX_EXEC_LOG_CHARS_FOR_LLM` | `8000` |
| `SPEC_REPAIR_ATTEMPTS` | `1`. Each repair is another slow CPU call |
| `MAX_CLOUD_TOKENS_PER_RUN` / `MAX_CLOUD_TOKENS_PER_DAY` | `0` (tokens are free; the caps would only stop runs early) |

`ANTHROPIC_API_KEY`, `CLAUDE_*` and `PROMPT_CACHE_*` are ignored in this mode. Leave them as they are in the template.

Optional: give an agent a different model with `OLLAMA_TRIAGE_MODEL`, `OLLAMA_ARCHITECT_MODEL` or `OLLAMA_AUTOMATION_MODEL`. With `OLLAMA_MAX_LOADED_MODELS=1`, Ollama swaps models between agents, which adds a load delay each time.

✅ **Check:** start the service (step 4.5) and read `/health`. `llm_provider` must be `ollama`, every entry in `claude_routes` must show your Ollama model with `"prompt_cache": false`, and `tools.git` and `tools.pnpm` must be `True`. `claude_configured` is `False`, and that's expected.

```powershell
Invoke-RestMethod http://localhost:8000/health | ConvertTo-Json -Depth 5
```

---

## Changes to Phase 5 — Paperclip

No Claude cost is booked: every call is recorded as provider `ollama` with a cost of 0, so the agent's monthly budget never trips. Keep the budget anyway; it's harmless.

## Phases 6–9 — n8n, Jira, Xray, end-to-end

Unchanged. Expect a run to take longer than the 3–10 minutes quoted in SETUP.md. With a measured ~40 seconds per agent call on the 3b model, the three LLM agents add roughly 2–5 minutes (more if the Automation Engineer needs a repair turn); `pnpm install` and Playwright dominate the rest.

**Close your browser before a run.** That single step is the difference between the numbers above and a run that never finishes.

---

## Ollama-specific troubleshooting

| Symptom | Cause / fix |
|---|---|
| Report says `Triage ... could not parse structured output` | The model returned invalid JSON for the schema. Retry; if it repeats, move to `qwen2.5-coder:7b`. 3b is the likeliest to fail here |
| A call hangs for many minutes, then times out — twice | **Almost always free RAM, not model speed.** Check it in Task Manager: below the model's ~2.4 GB the machine pages on every token. Close your browser and re-run; don't just raise the timeout. Confirm the model is behaving with `ollama run qwen2.5-coder:3b "say ok"` — if that is quick, the model is fine and RAM is the problem |
| Run fails with a timeout | Raise `OLLAMA_TIMEOUT_SECONDS`, or use the 3b model |
| `output hit max_tokens=4096` | The model needed a longer answer. Raise `OLLAMA_MAX_TOKENS` (to 6000 at most with an 8192 context) |
| Specs rarely load / `Spec still fails to load` | Expected with small models. Raise `SPEC_REPAIR_ATTEMPTS` to 2, or try the 7b for `OLLAMA_AUTOMATION_MODEL` |
| Model seems to ignore the end of the ticket or diff | The context is too small. Check `OLLAMA_CONTEXT_LENGTH` is set and Ollama was restarted; lower `MAX_DIFF_CHARS` |
| Laptop freezes during a run | Model too big for the RAM left over. Use 3b, close other apps, keep `OLLAMA_NUM_PARALLEL=1` |
| `/health` shows `llm_provider: claude` | `LLM_PROVIDER=ollama` isn't in `.env`, or the service wasn't restarted after editing it |
| `ollama pull` sits at the same size, no CPU use | The connection dropped. Ctrl+C and re-run; it resumes. Use the retry loop in Phase 2 |
| Nothing listens on port 11434 | The tray app didn't start the server. Run `ollama serve` yourself (see Phase 2) |
| `ollama list` is empty although a big file sits in `.ollama\models\blobs` | The pull never finished: that file is the incomplete `-partial` blob. Re-run the pull |
