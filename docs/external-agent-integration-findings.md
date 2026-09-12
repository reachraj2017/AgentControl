# External Agent Integration — Findings & Analysis

> **Historical document — describes the pre-v4 system.** Every issue below was the direct motivation for the v4 rearchitecture (see `design/v2-gateway-capture-m1-ingest.md` and `design/checkpoint-handoff-ingest.md`) and has since been addressed — see `design/v4-implementation-status.md` for what was actually fixed and verified. Kept here as the record of *why* v4 exists, not as a description of current behavior. If you're instrumenting a new system today, use `docs/instrumentation-guide.md` instead.

**Date:** 2026-08-26
**Test system:** openai-cs-agents-demo (OpenAI multi-agent customer service demo)
**Instrumentation guide used at the time:** the pre-v4 `docs/instrumentation-guide.md` and task spec

---

## User concern

> "My concern here is that our current system cannot support any external agent system and seems to break based on what I tried here."

The concern is valid. A real-world instrumentation test against the openai-cs-agents-demo — following the instrumentation guide and task spec — surfaced six distinct issues before a single eval score appeared in the portal. None of these were user error. All required either code changes to the repository or non-trivial framework-specific workarounds that the documentation did not anticipate.

---

## Full issue trail — as encountered

### Issue 1 — Wrong redirect method in the bootstrap

**What happened:**
`acp_bootstrap.py` set `openai.base_url` and `openai.api_key` as module-level attributes on the `openai` library. The openai-agents SDK internally creates its own `AsyncOpenAI()` client, which reads `os.environ["OPENAI_BASE_URL"]` and `os.environ["OPENAI_API_KEY"]` — it does not read those module attributes. As a result, every LLM call silently bypassed the gateway and went directly to real OpenAI. No error was raised, no warning was logged. The gateway call log showed zero entries.

**Fix applied in test:**
Set `os.environ["OPENAI_BASE_URL"]` and `os.environ["OPENAI_API_KEY"]` instead of module attributes. Additionally called `agents.set_default_openai_client()` to register an explicitly-configured gateway client, ensuring the SDK used it for all agent calls.

**What this reveals:**
The bootstrap was written against how the `openai` library worked in older SDK versions. The openai-agents SDK wraps it differently. This is not an edge case — it affects every user of the openai-agents SDK, one of the most widely-used agent frameworks. The failure mode (silent bypass) is the worst kind: instrumentation appears to succeed but produces no data.

**Severity:** Critical. The entire gateway integration was invisible until this was found.

---

### Issue 2 — One role header shared by all agents

**What happened:**
`acp_bootstrap.py` created one gateway client with one static `X-Gateway-Agent-Role` header applied globally. In a multi-agent system (Orchestrator, Searcher, Summarizer, Translator, guardrails), every call appeared as the same role in the gateway's call log and in all portal dashboards. The agents were indistinguishable.

**Fix applied in test:**
Gave each agent its own `ModelSettings(extra_headers={"X-Gateway-Agent-Role": "<agent-specific-role>"})` in `assistant/agents.py` and `assistant/guardrails.py`. This overrides the role header per call while all agents still share the same underlying gateway client.

**What this reveals:**
The spec assumes one role per system. Multi-agent systems with distinct agents require per-agent role tagging. Achieving this requires touching the framework's agent constructor — a change the spec explicitly said would not be needed ("you will not rewrite the target agent's logic"). In practice, for multi-agent systems it is required to get meaningful data.

**Severity:** Medium. Instrumentation works but produces indistinct, unactionable data for multi-agent systems.

---

### Issue 3 — Gateway had no Responses API endpoint

**What happened:**
The openai-agents SDK defaults to OpenAI's Responses API (`/v1/responses`). The ACP gateway only implemented `/v1/chat/completions`. `WebSearchTool` (used by the Searcher agent) requires the Responses API — it is a hosted tool that executes on OpenAI's infrastructure. Once the gateway redirect was active, Searcher calls began failing. Adding a global `OPENAI_BASE_URL` redirect without this endpoint broke the demo's search functionality entirely.

**Fix applied in test:**
Added a full `/v1/responses` endpoint to the gateway (`agent_gateway/main.py` + `proxy.py`) — including auth, routing, governance enforcement, logging, streaming support, and fallback — forwarding via `litellm.aresponses`. Rebuilt and restarted the gateway container.

**What this reveals:**
This is a fundamental architectural gap. The Responses API is the default transport for the openai-agents SDK (not an edge case). Any agent using `WebSearchTool`, `FileSearchTool`, or `ComputerTool` hits this wall immediately. The fix required modifying gateway source code — it was not a configuration issue. Any user following the current instrumentation guide with an openai-agents system would hit this without recourse.

**Severity:** Critical for any openai-agents SDK user. Affects the majority of OpenAI agent systems built in 2024–2025 that use hosted tools.

---

### Issue 4 — Span name mismatch between gateway (M3) and eval-runner (M1)

**What happened:**
The gateway emitted spans named `gateway.llm_call`. The eval-runner's trigger logic, trace assembler, eval pipeline, and every portal dashboard query are all hardcoded to look for spans named exactly `agent.task`. Gateway calls were correctly logged in ClickHouse but silently dropped from all evaluation processing — no eval scores, no portal Prompt Analysis data, no Eval Measurements rows. This persisted even after the gateway redirect was fixed and calls were confirmed flowing through the gateway.

**Fix applied in test:**
Renamed the span from `gateway.llm_call` to `agent.task` in `telemetry.py` (used in exactly one place). Verified that `eval_scores` rows began appearing in ClickHouse.

**What this reveals:**
M1 (eval-runner) and M3 (gateway) were never tested together end-to-end against a real external agent system. The gateway was added after the eval-runner and used its own internal naming convention that M1 never knew about. The two modules have an implicit contract about span naming that was never documented or enforced. This is an internal integration gap — the modules don't speak the same internal language by default.

**Severity:** Critical. In its pre-fix state, gateway mode produced zero eval data. The core value proposition of routing through the gateway (automatic evaluation) was silently non-functional.

---

### Issue 5 — OpenAI SDK's own tracing conflicts with gateway

**What happened:**
The openai-agents SDK has its own built-in tracing that uploads to OpenAI's platform, authenticating with `os.environ["OPENAI_API_KEY"]`. After the gateway redirect was applied, that environment variable held the gateway placeholder key, not a real OpenAI key. Every trace upload got a 401 response. The demo's existing `os.environ.setdefault("OPENAI_TRACING_DISABLED", "1")` was dead code — this version of the SDK does not read that variable.

**Fix applied in test:**
Called `agents.set_tracing_disabled(True)` in `acp_bootstrap.py`. Removed the dead `os.environ.setdefault` line and unused `os` import from `main.py`.

**What this reveals:**
Framework-specific SDK behaviors that conflict with instrumentation are not anticipated in the spec or bootstrap template. The openai-agents SDK ships with its own telemetry that competes with ACP's. This class of conflict — framework-owned telemetry versus ACP telemetry — will recur with other frameworks (LangSmith in LangChain, Weights & Biases in various ML frameworks, etc.).

**Severity:** Low (non-fatal, just noisy 401 errors). Representative of a broader class of framework-specific conflicts not yet handled.

---

### Issue 6 — Three separate Prompt Analysis data gaps

Even after data was confirmed flowing through the gateway and eval scores were appearing, the portal's Prompt Analysis view showed blank fields for model, token counts, processing time, and conversation ID. Three separate root causes:

**6a — Model and token counts missing:**
The eval pipeline's extraction logic only reads model and token attributes from child spans whose name contains "llm", "chat", or "generate_content". The gateway emitted a single flat `agent.task` span with all attributes on it — no child spans. This span never qualified for extraction even though it carried the correct `gen_ai.request.model` and token attributes.

Fix: added a nested `llm_call` child span under `agent.task` carrying `gen_ai.request.model` and token usage attributes.

**6b — Processing time near-zero:**
Spans were emitted asynchronously after the call completed (async logging design). The span's own OTel-measured duration reflected only the time to emit, not the actual LLM call latency — typically a few milliseconds regardless of real call duration.

Fix: explicitly backdated `start_time` and `end_time` from the recorded `latency_ms` value so the span duration matched real latency.

**6c — Conversation ID missing, every call a separate conversation:**
Conversation ID was never set on spans. Additionally, every gateway call was assigned a fresh random `run_id` rather than one shared ID per conversation session. This meant multi-turn conversations appeared as a series of isolated single calls with no thread connecting them.

Fix: added a `conversation.id` span attribute, and propagated the real ChatKit thread ID from `server.py` → `acp_bootstrap.py` via a Python context variable plus an `httpx` event hook. The hook merges the thread ID additively with each agent's own role header rather than replacing it, sent as `X-Gateway-Run-Id`.

**What Issue 6 reveals collectively:**
The eval pipeline has an undocumented implicit contract about span shape: a parent `agent.task` span with a nested `llm_call` child span carrying specific attribute names, with explicit timestamps, and a conversation ID attribute. This contract was never written down. Any gateway integration that does not reproduce this exact shape produces empty dashboard fields. The contract was only discoverable by reading the eval-runner source code and tracing the extraction logic through ClickHouse queries.

**Severity:** High. The portal appeared functional (data flowing, some scores present) but every per-call detail metric was blank or wrong.

---

## Pre-existing issues flagged but not fixed

These were identified during the instrumentation test but were out of scope for the integration task. They affect production readiness independently of the external agent integration.

| Issue | Location | Impact |
|---|---|---|
| `eval_tracer.py:28` points to hostname `aieval-otelcol` which does not exist in the compose network (should be `otel-collector`) | `modules/m1-observability-eval/eval_runner/eval_tracer.py` | Noisy connection errors in eval-runner logs. Non-blocking. |
| `ANTHROPIC_API_KEY` missing or invalid in eval-runner container | `.env` / `docker-compose.yml` | LLM-judged eval metrics (faithfulness, relevance, correctness) fall back to a neutral 0.5 for every call. Only deterministic metrics score correctly. This is the most impactful pre-existing gap — the LLM judge is the core of the eval pipeline. |
| Governance enforcement is fail-open by default | M2 governance config | Policy checks run but blocking/HITL are not activated. Not a bug by design, but not surfaced clearly to operators who expect enforcement to be live. |

---

## Root cause analysis — why these issues exist together

The system was built with two integration paths — **direct tracing (Mode 2)** and **gateway routing (Mode 1)** — but the eval pipeline was only fully validated with direct tracing. Gateway routing was added later and inherited assumptions that held true for direct tracing but were never tested against a real external agent system:

| Assumption | Reality |
|---|---|
| One role per system | Multi-agent systems have per-agent roles |
| Chat Completions only | openai-agents SDK defaults to Responses API |
| Flat `agent.task` span carries all data | Eval pipeline expects a nested child span for model/token extraction |
| `gateway.llm_call` span name | Eval pipeline hardcodes `agent.task` span name |
| `openai.base_url` module attribute works | openai-agents SDK reads `OPENAI_BASE_URL` env var only |
| Bootstrap sets conversation ID | Conversation ID requires framework-specific propagation |

The instrumentation spec and bootstrap template described what *should* work based on how the pieces were designed. The test against the openai-cs-agents-demo is the first time these assumptions were validated against reality, and all of them required correction.

---

## What needs to change in the repository

The fixes applied during the test live in the test environment only. For the repository to honestly support external agent instrumentation, the following changes need to land in the main repo:

**Gateway (`modules/m3-agent-gateway/agent_gateway/`):**
1. Add `/v1/responses` endpoint (proxy to upstream with auth, logging, governance, streaming)
2. Rename span from `gateway.llm_call` to `agent.task` in `telemetry.py`
3. Add nested `llm_call` child span with `gen_ai.request.model` and token attributes
4. Backdate span timestamps from recorded `latency_ms`
5. Add `conversation.id` attribute support via `X-Gateway-Run-Id` header

**Task spec and bootstrap template (`docs/ai-agent-task-spec.md`):**
6. Fix redirect method: use `os.environ["OPENAI_BASE_URL"]` not `openai.base_url`
7. Add `agents.set_default_openai_client()` call for openai-agents framework
8. Add `agents.set_tracing_disabled(True)` to suppress SDK-owned telemetry
9. Add detection for `WebSearchTool` / Responses API usage
10. Add per-agent role header pattern for multi-agent systems
11. Fix gateway verification endpoint from `/admin/calls` to `/gateway/calls`

**Pre-existing fixes:**
12. Fix `eval_tracer.py:28` hostname from `aieval-otelcol` to `otel-collector`
13. Ensure `ANTHROPIC_API_KEY` is valid in the eval-runner container (gating LLM judge metrics)

---

## Conclusion

The test confirmed the concern: in its current repository state, the system does not reliably support external agent instrumentation out of the box. A user following the published guide would encounter Issues 1–6 in sequence, each requiring either source code changes or undocumented framework-specific knowledge to resolve.

The good news is that all six issues are fixable, the fixes are understood, and once applied the system works end-to-end — data flows, evals score, and the portal populates correctly. The gap is between the current repo state and the state required to make the published instrumentation workflow actually work.
