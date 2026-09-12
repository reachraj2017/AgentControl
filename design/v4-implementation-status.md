# v4 Implementation & Validation Status

**Status:** Implemented and validated end-to-end against the live docker-compose stack
**Date:** 2026-09-10
**Author:** Raj Ramanujam
**Scope:** Record of what was built, what was actually run and verified (not just read/reviewed), and every bug found and fixed in the process. This is the authoritative status doc for v4 — [`v2-gateway-capture-m1-ingest.md`](./v2-gateway-capture-m1-ingest.md) and [`checkpoint-handoff-ingest.md`](./checkpoint-handoff-ingest.md) are the design rationale that preceded it and remain accurate as architecture references; their "Draft" status headers are superseded by this doc.

---

## 1. What this covers

v4 (`/Users/rajlearn/optimize-project/opt-eval-git-test/v4`) is a full rebuild of the AI Control Plane that reuses v2's working logic (68 eval metrics, 13 governance categories, gateway routing/caching/A-B/traffic, EvalGov coordinator + sub-agents, portal — all carried over unchanged) with a new gateway-primary, standards-based ingestion architecture applied on top, per the two design docs above.

This doc records the validation pass that followed implementation: standing up the real docker-compose stack, driving real LLM traffic through it, and fixing what broke. Four real, previously-undetected bugs were found this way — none of them visible from reading the code alone.

---

## 2. What was built (recap)

| Area | Change |
|---|---|
| M3 gateway | Protocol completeness: `/v1/responses`, `/v1/messages`, Gemini `generateContent`, `/v1/embeddings`. Extended `gateway_call_log` (`conversation_id`, `protocol`, `started_at`/`ended_at`, `messages_json`, `response_tool_calls_json`). New `/v1/checkpoint`, `/v1/handoff`, `/v1/tool-span` endpoints + `gateway_structural_events` table. New `agent_gateway/protocol_adapters.py` for dialect translation. |
| M1 eval-runner | `GatewayIngestPipeline` (generalizes `ShadowEvalPipeline` to all gateway traffic). `EvalPipeline.run_from_targets()` refactor. New `ingestion/semconv_mapping.py` — normalization layer recognizing OTel GenAI, OpenInference, and OpenLLMetry span dialects alongside ACP's native shape. New `gateway_call_eval_state` / `gateway_structural_events` repository methods. |
| SDK | New `acp-signals` package: `checkpoint()`/`handoff()`/`tool_span()` + `context` propagation. Adapters for OpenAI Agents SDK, Google ADK, LangChain, CrewAI. `acp_tracing.otel_ecosystem` — OTLP-exporter bootstrap alternative to the passive in-process tracer. |

---

## 3. Validation performed

All of the following were done against the actual running stack (`docker compose -f docker-compose.yml up -d --build`, full stack: gateway, eval-runner, governance-service, evalgov-agent + sub-agents, ClickHouse, otel-collector, Jaeger, portal), not inferred from code review:

1. **Schema cross-check** — verified the `gateway_call_log` / `gateway_call_eval_state` / `gateway_structural_events` schemas the M3 gateway actually created in ClickHouse match exactly what the M1 pipeline and the `acp-signals` SDK client independently assumed (these were built in parallel by three separate work-streams against the design docs' schemas, without seeing each other's code).
2. **Protocol completeness, live** — created a real virtual gateway key and sent real requests with real API keys to `/v1/chat/completions`, `/v1/responses` (OpenAI), and `/v1/messages` (Anthropic). All three returned correct real model responses through the gateway.
3. **Checkpoint / handoff / tool-span, live** — sent real requests to all three new endpoints; confirmed correct rows in `gateway_structural_events` with the right `call_type` and payload.
4. **Gateway-only eval coverage, live** — confirmed that a zero-SDK client hitting the gateway directly (the central claim of the whole rebuild) produces full eval coverage: 37 metrics scored per call, including `handoff_fidelity` and `tool_selection_accuracy` derived from the explicit `/v1/handoff`/`/v1/tool-span` calls, with **zero ACP tracer involved**.
5. **In-process MERGE-mode regression, live** — ran `opt-demo` (real ADK multi-agent pipeline, in-process ACP tracer + gateway routing both active) and confirmed: `otel_traces` got the real in-process spans, `gateway_call_log` also captured the same call via `trace_id` correlation, and `GatewayIngestPipeline` correctly detected the existing in-process trace and **merged instead of re-scoring** — no duplicate `prompt_evals`/`eval_scores` rows from the gateway side.
6. **Framework-adapter signatures, verified against real packages** — installed (or, for CrewAI, downloaded the real published wheel directly since this environment's Python was too new for a normal install) `openai-agents==0.22.2`, `google-adk==2.8.0`, `crewai==1.15.21`, and `langchain-core==1.6.2`, and checked every adapter's assumed hook signature against the actual library source.

---

## 4. Bugs found and fixed during validation

None of these were visible from reading the code — all four were only discoverable by actually running the system.

### 4.1 Critical — gateway-ingest pipeline was completely non-functional

**File:** `modules/m1-observability-eval/eval_runner/db/repository.py`, `get_pending_gateway_calls()`

**What was wrong:** The query used `LEFT JOIN ... WHERE st.state IS NULL OR st.state = 'pending'` to find gateway calls not yet processed. ClickHouse's `LEFT JOIN`, by default, fills unmatched rows with the column's **type default** (empty string for `String`), not SQL `NULL`. So `st.state IS NULL` was never true for any row, and `get_pending_gateway_calls()` silently returned zero rows, always — meaning the entire gateway-ingest pipeline (the central new capability of this rebuild) was dead on arrival, with no error, no log line, nothing to indicate it wasn't working.

**Fix:** Changed the condition to `st.state = '' OR st.state = 'pending'`.

**How it was found:** Sent real traffic through the gateway, waited for `GatewayIngestPipeline`'s poll loop, and found `gateway_call_eval_state` stayed empty indefinitely despite `gateway_call_log` filling up. Confirmed by running the exact SQL directly against ClickHouse and observing `state=''`, `is_null=0` for unmatched rows.

### 4.2 Critical — native protocol endpoints didn't route to the right provider

**File:** `modules/m3-agent-gateway/agent_gateway/protocol_adapters.py`

**What was wrong:** `/v1/messages` (native Anthropic) and Gemini `generateContent` passed the bare model name straight through (e.g. `claude-3-5-haiku-20241022`, with no prefix) to the internal chat-completions path, which follows the pre-existing convention "no prefix → OpenAI." Real Anthropic/Gemini client SDKs have no reason to know ACP's `anthropic/`/`gemini/` prefix convention — that's not part of the real API contract for those endpoints. Result: every native Anthropic/Gemini call was misrouted to OpenAI (or failed), returning a 502 with a misleadingly normal-looking empty response body.

**Fix:** Added `_ensure_backend_prefix(model, backend)`, applied in `anthropic_request_to_chat` and `gemini_request_to_chat`, which prefixes a bare model name with the endpoint's own known provider — since these are protocol-specific endpoints, the provider is unambiguous, unlike the generic `/v1/chat/completions` path.

**How it was found:** Sent a real `/v1/messages` request; got a 502 with empty content. Gateway logs showed `litellm.BadRequestError: LLM Provider NOT provided`.

### 4.3 Real, pre-existing bug — opt-demo's intent classifier (unrelated to v4, but blocks the demo)

**File:** `opt-demo/runner.py`, `_classify_query()`

**What was wrong:** Confirmed byte-identical to the original v2 repo — not introduced by this rebuild. Every regex used to extract the target language and inline text for translate/summarize requests anchored on end-of-string (`\s*$`), but ordinary sentences end in punctuation (`"...to Japanese."`). The trailing period silently broke every match, every time, for every translate request. The classifier fell back to its "no explicit text" default (`"Translate the previous response to {lang}"`) even for a message that plainly contained the text to translate, and separately `language` extraction also failed for the same reason, defaulting to English regardless of what was actually requested. The model, given only that fallback instruction with no real prior turn, just echoed its own system prompt back — a confusing failure mode with no error anywhere.

**Fix:** Strip trailing sentence punctuation before classification; loosened the inline-phrase regex to tolerate quoted/apostrophe'd text between "translate" and "to `<lang>`" (matching `[^\s]+` instead of `\w+` for the filler-word group).

**How it was found:** Ran opt-demo's actual `/chat` endpoint with the exact example query from its own README (`"Translate 'Good morning, how are you?' to Japanese."`) and got the agent's system prompt back verbatim instead of a translation.

### 4.4 Real bug — LangChain adapter recorded the wrong tool name for every tool span

**File:** `sdk/packages/acp-signals/acp_signals/adapters/langchain.py`

**What was wrong:** `on_tool_end`/`on_tool_error` read `kwargs.get("name")` / `kwargs.get("inputs")`, assuming LangChain passes the tool's name and input to those callbacks. Verified against the actual `langchain-core==1.6.2` source (`callbacks/base.py`): it doesn't — the tool name and input only arrive in `on_tool_start`'s own parameters (`serialized['name']`, `input_str`). Every tool span was silently being recorded with the placeholder `tool_name="tool"` instead of the real tool name.

**Fix:** Capture `serialized.get("name")` and `input_str` in `on_tool_start`, keyed by `run_id`, and look them up in the paired `on_tool_end`/`on_tool_error` call instead of reading nonexistent kwargs.

**How it was found:** Not from running LangChain live — from reading the actual installed `langchain-core` source after downloading it specifically to verify this adapter's assumptions (see §5).

### 4.5 Real bug — additive standards-recognition split one agent invocation into two Prompt Analysis rows

**File:** `modules/m1-observability-eval/eval_runner/ingestion/trace_assembler.py`, `extract_eval_targets()`

**What was wrong:** The additive standards-based recognition described in §"Standards-based span recognition" (the whole point of which is letting a raw external OTel/OpenInference/OpenLLMetry trace trigger evaluation without ACP's own tracer) unconditionally added *any* span `semconv_mapping.normalize_span()` classified as `KIND_AGENT_TASK`. For a framework that is both manually instrumented (opt-demo's own `agent.task` spans) **and** auto-instrumented (ADK's own native `invoke_agent <agent>` span, which carries `gen_ai.operation.name=invoke_agent`), this double-counted the same invocation: ADK's `invoke_agent translator` span sits *nested inside* opt-demo's own `agent.task` span for that same translator call — not a second, distinct agent step. With both recognized as task spans, the LLM call's token-attribution walk (`_nearest_task_ancestor` in `eval_pipeline.py`, pre-existing v2 logic, unmodified) stopped at the *nearer* one — the content-less `invoke_agent` span — leaving the real span (which has the actual `task.input`/`task.output`) with zero tokens, while a second, spurious Prompt Analysis row appeared for the inner span with the tokens but `agent_name="unknown"` and empty prompt/response (no ACP-native `agent.role`/`task.input` attributes). This is exactly the "shows twice — once with content and no cost, once as 'unknown' with cost and no content" symptom.

**Fix:** Before adding a dialect-recognized `KIND_AGENT_TASK` span, walk its ancestor chain (via `parent_span_id`) and skip it if any ancestor is already a recognized task span (native or dialect) — only the outermost task-level span per branch counts as one invocation.

**How it was found:** User-reported symptom in opt-demo's Prompt Analysis view, reproduced directly: sent a real translate request, inspected the raw span tree in `otel_traces` for that trace, found `invoke_agent translator` nested one level inside opt-demo's own `agent.task` span for the same call — confirming the double-recognition. Fixed, rebuilt `eval-runner`, re-ran the same request: exactly 2 correct rows (orchestrator + translator), both with real prompt/response and correct, non-zero token counts, no third "unknown" row.

### 4.6 Real bug — same additive-recognition gap, one layer down: token counts inflated up to 3x

**File:** `modules/m1-observability-eval/eval_runner/ingestion/trace_assembler.py`, `extract_eval_targets()`

**What was wrong:** Same class of bug as §4.5, found immediately after from a user-reported follow-up (Prompt Analysis token counts running ~3x what `gateway_call_log` showed for the same call). A single real LLM call is represented by a *chain* of nested spans at different instrumentation layers — ADK's `call_llm` wrapper → its `generate_content <model>` span → `opentelemetry-instrumentation-openai`'s `openai.chat` child of that. The pre-existing native construction of `llm_spans` already knew this and deliberately picked exactly one (`generate_content`, explicitly excluding `call_llm` and, when `generate_content` exists, `openai.chat`) to avoid double-counting. The additive dialect probes had no knowledge of that exclusion list — `_try_otel_genai`'s generic "does this span carry any `gen_ai.*` attribute" check matched all three layers — so the additive loop silently re-added the two spans the native logic had deliberately excluded, tripling every token/cost figure for the trace.

**Fix:** Same principle as §4.5, applied to `KIND_LLM_CALL`: before adding a dialect-recognized LLM span, check whether it's an ancestor *or* descendant of an already-recognized LLM span (native ones are already in the id set before the additive loop starts, so this holds regardless of span iteration order) — skip it if so, since it's the same underlying call at a different layer, not a second call.

**How it was found:** User-reported, immediately following the §4.5 fix. Reproduced directly: sent a real translate request, compared `gateway_call_log.tokens_in/tokens_out` (115/9) against `prompt_evals.prompt_tokens/completion_tokens` for the same trace — before the fix these would have run ~3x that; after the fix both rows show the exact same 115/9 the gateway itself logged.

### 4.7 Real, pre-existing bug — multi-hop conversations evaluated multiple times (cooldown, not a debounce)

**File:** `modules/m1-observability-eval/eval_runner/main.py`

**What was wrong:** Already flagged as an unfixed "known limitation" in §6 of an earlier version of this doc, then reported again by the user on a genuinely multi-hop query ("search for movie X then translate to French" — orchestrator → searcher (two LLM calls, one search tool call) → translator). The existing mechanism (`_eval_triggered` + `EVAL_DEDUP_SECONDS = 30`) fired evaluation on the *first* completed agent-task span seen for a trace, then suppressed any further firing for a flat 30-second cooldown from that first sighting. A multi-agent trace's OTel spans export incrementally — one OTLP batch per sub-agent as it finishes — so a conversation with tool-call latency routinely takes longer than 30 seconds end-to-end. When it does, the cooldown expires mid-conversation and a *later* sub-agent's completion (e.g. translator's, arriving well after searcher's) fires a second, independent full-trace evaluation on top of the first — each one capturing whatever partial span state existed at that moment, with nothing ever superseding or cleaning up the earlier partial rows. Reproduced concretely: a 3-agent trace (orchestrator, searcher, translator) produced 5 `prompt_evals` rows instead of 3 — 2 stale partial rows from early firings, plus the 3 correct ones from a later, complete pass.

**Fix:** Replaced the fixed-cooldown dedup (`_eval_triggered: dict[str, float]`, timestamp-based) with a genuine debounce (`_pending_eval: dict[str, asyncio.Task]`, cancellation-based). Every new completed agent-task span for a trace now **cancels** whatever evaluation task was already scheduled for it and schedules a fresh one; the pipeline only actually runs `EVAL_DEBOUNCE_SECONDS` (10s — the same window previously used just to let ClickHouse's batch flush) after the *last* completion, not the first. Exactly one evaluation pass happens per conversation regardless of how many hops it takes or how long it runs, as long as consecutive agent completions are within 10s of each other (true for essentially all real agent turns; a conversation with a multi-minute gap between two hops is an accepted edge case, same class of tradeoff as the pre-existing 10s ClickHouse-flush wait it piggybacks on).

**How it was found:** User-reported on the exact multi-hop query above. Reproduced directly: watched `prompt_evals` row count for the trace grow 1 → 2 → 5 over ~75 seconds before the fix. After the fix, rebuilt `eval-runner`, re-ran the identical query: row count went 0 → 3 once (at ~60s) and stayed at exactly 3 for the following 36 seconds of observation. Verified the math independently — searcher's root-aggregated and per-call token counts, and translator's, both sum and match `gateway_call_log`'s ground-truth values exactly, with no duplication and no inflation.

**Caveat that turned out to matter:** this fix addressed one real cause of multi-firing (the in-process trigger's own timestamp-cooldown), but the user correctly pushed back that duplication was still happening on a longer query and that hop count shouldn't matter at all — which led directly to §4.8, a second, independent, and more fundamental cause of the same symptom.

### 4.8 Real, general bug — a structural race between gateway-ingest and in-process evaluation, independent of hop count

**File:** `modules/m1-observability-eval/eval_runner/pipeline/gateway_ingest_pipeline.py`, `run_batch()`

**What was wrong:** opt-demo's calls are captured twice by design: once by the in-process OTel tracer (Layer 2) and once by the gateway itself, since opt-demo also routes through it (Layer 1). `GatewayIngestPipeline` polls `gateway_call_log` every 5 seconds; for a row carrying a real (propagated) `trace_id`, it checks `trace_has_inprocess_spans(trace_id)` — if that's not yet true, it assumed "gateway-only" and synthesised its own evaluation immediately from whatever gateway rows existed at that moment. But the in-process trigger has its own debounce (§4.7, 10s) plus a full sequential LLM-judge cascade that routinely takes 30-90+ seconds for a multi-agent conversation — so the 5-second poller almost always sees "no in-process spans yet" several times before the real one lands, and each time it did, it committed to a premature, partial SYNTHESISE pass under its own `run_id`, completely bypassing the debounced trigger (§4.7's fix does not apply here, since this path never calls `EvalTrigger` at all). Confirmed directly from logs: two of the three writes to one trace's `prompt_evals` carried run_ids that never appeared in an `eval_triggered`/`pipeline_start` log line — proof they came from this path, not the in-process one. Critically, this is **not a function of hop count**: a single-agent, single-call conversation races the exact same way, just with lower odds of losing, since there's less to wait for.

**Fix:** A real propagated `trace_id` existing on a gateway row at all is itself strong evidence an in-process tracer is active and will eventually write spans for it — a synthesized (non-"trace"-kind) grouping key never has this ambiguity and is left untouched. For "trace"-kind groups specifically: if no in-process spans exist yet, don't synthesise — leave the rows pending (skip marking them, so they're picked up again next poll) and check the group's oldest row's age; only fall back to SYNTHESISE once `_INPROCESS_GRACE_SECONDS` (120s, comfortably longer than the in-process pipeline's own debounce + judge cascade) has elapsed with still no in-process trace. This preserves the "zero-SDK gateway-only integration" promise untouched (those rows never carry a real trace_id, so they're never delayed) while giving a genuinely dual-instrumented call like opt-demo's real room to land its own correct, complete pass first.

**How it was found:** User pushed back explicitly that the duplication "should not matter whether the query is single or multiple step" — correctly rejecting the hop-count-based framing in §4.7 as incomplete. Re-examined the full (untruncated) log history for the 4-hop reproduction trace and found two premature `pipeline_complete` events with run_ids absent from any `eval_triggered` log line — the tell that a second, unrelated code path was involved. Traced it to `GatewayIngestPipeline` racing the in-process trigger. Fixed, rebuilt `eval-runner`, re-ran the same 4-hop query (search + summarize + translate): watched `prompt_evals` for the full 176 seconds this time — stayed at 0 until a single write at ~80s, producing exactly 4 rows (orchestrator, searcher, summarizer, translator), stable for the remaining 96 seconds observed. Verified every token count against `gateway_call_log` exactly: searcher's two real calls sum to its row, summarizer and translator match their own calls exactly, and the orchestrator's root row is the exact sum of all three sub-agents — no duplication, no premature partial rows, regardless of this being a 4-hop conversation.

### 4.9 Critical, general bug — a synchronous evaluation call blocked the entire process's event loop

**File:** `modules/m1-observability-eval/eval_runner/pipeline/trigger.py` (`trigger_evaluation`), `main.py` (`_run_conversation_eval`, `_evaluate_trace_background`)

**What was wrong:** `pipeline.run()` — the actual evaluation work, a sequential cascade of LLM-judge HTTP calls routinely taking 15-90+ seconds — was called as a plain synchronous call directly inside an `async def` function, with no `asyncio.to_thread()`. Python's asyncio event loop is single-threaded: while that call was running, it blocked *everything else in the same process* — new incoming OTLP trace ingestion, health checks, and every other background loop (including the conversation-idle loop's own `await asyncio.sleep(30)`, which simply couldn't wake up on schedule while the loop was frozen). Confirmed directly from the OTel Collector's own logs: its forward of spans to eval-runner's `/v1/traces` was timing out — `"context deadline exceeded (Client.Timeout exceeded while awaiting headers)"` — and the collector went completely silent for a 17-minute stretch, exactly the signature of the receiving process being unresponsive for extended periods. Notably, two *other* background loops in the same file (`_shadow_eval_loop`, `_gateway_ingest_loop`) already correctly wrap their equivalent calls in `asyncio.to_thread()` — this was an inconsistency against the codebase's own established pattern, not a new one.

**Fix:** Wrapped all three direct `pipeline.run()`/`ConversationEvalPipeline.run()` call sites in `asyncio.to_thread(...)`, matching the pattern the other two loops already use.

**How it was found:** Investigating "why do I see no conversation scores" for a real 34-turn conversation that had been idle for 30+ minutes — well past the 60-second threshold — with zero related log lines anywhere in the container's multi-hour history. Reproduced live (not from stale historical state) by running a fresh single-turn conversation directly through `runner.run_agent()` and watching: before the fix, in-process eval scoring itself was taking 60-90+ seconds to even *start* (versus the true ~10s debounce), and the OTel Collector's own logs showed active delivery timeouts to eval-runner during that stretch.

### 4.10 Critical, general bug — per-batch task-span dedup could silently pick the wrong span, breaking conversation tracking and risking wrong run attribution

**File:** `modules/m1-observability-eval/eval_runner/main.py` (`_process_spans_background`)

**What was wrong:** Even after §4.9's fix, conversation-level scores still never appeared. A temporary diagnostic (`conversation_eval_loop_tick`, kept as a permanent low-noise debug heartbeat) showed the idle-loop ticking correctly every 30 seconds but the conversation tracker was *always empty* — the tracking write was never happening. Root cause: when multiple task-shaped spans exist for one trace_id in a single OTLP batch — exactly opt-demo's dual-instrumentation case (§4.5): opt-demo's own ACP-native `agent.task` span *and* ADK's own native `invoke_agent <agent>` span both independently satisfy `is_completed_task()` — the code picked whichever span happened to be first in the batch, with no preference. `invoke_agent <agent>` carries the OTel GenAI dialect's namespaced `gen_ai.conversation.id` attribute, not the bare `conversation.id` key this code (and `EvalTrigger.get_run_id()`) actually read — and it carries **no run-id equivalent attribute at all**. So whenever that span won the race (confirmed via direct diagnostic logging to be the span actually arriving first for opt-demo's traces), conversation tracking silently got nothing, *and* the eval's `run_id` silently fell back to the generic "default" run instead of the real one — a second, more consequential effect of the same root cause that isn't limited to conversation scoring at all.

**Fix:** Group `completed_task_spans` by `trace_id` per batch and explicitly prefer the ACP-native span (`span_name == "agent.task"`) as the representative for both debounce scheduling and conversation tracking; fall back to whichever was recognized only when no native span exists in the batch (a framework-native-only integration with no ACP tracer at all, where there is nothing to prefer). Also widened the conversation-id read to accept the namespaced `gen_ai.conversation.id` as a fallback, for defense in depth.

**How it was found:** Added a targeted diagnostic log at the exact read point (`span.get("attributes")` keys and values) rather than continuing to reason about it statically. It showed, unambiguously, that the span reaching this code for opt-demo's traces was `invoke_agent translator` with attribute keys `["gen_ai.operation.name", "gen_ai.agent.description", "gen_ai.agent.name", "gen_ai.conversation.id"]` — no bare `conversation.id`, confirming the dialect mismatch directly rather than by inference. Fixed, rebuilt, re-ran a fresh test conversation: the tracker correctly populated (`tracked: 1`), correctly aged out after the 60s idle threshold, `conversation_eval_triggered` fired, all four multi-turn judges ran, and `eval_scores` (queried directly) shows all four conversation-level scores (`knowledge_retention`, `role_adherence`, `conversation_completeness`, `conversation_relevancy`) present and correct.

---

## 5. Framework-adapter verification detail

The original adapter docstrings said "best-effort / unverified — check against your installed version." This pass replaced that with an actual check:

| Adapter | Package checked | Method | Result |
|---|---|---|---|
| OpenAI Agents SDK | `openai-agents==0.22.2` | Installed with `--no-deps` (pulling the full dependency tree caused pip's resolver to backtrack for 20+ minutes trying to jointly satisfy CrewAI + Google ADK's constraints — installing each target package alone, undeps'd, avoids that entirely since only the package's own source is needed to check its public hook signatures) and read `agents/lifecycle.py` directly | **Correct as written** |
| Google ADK | `google-adk==2.8.0` | Same `--no-deps` approach; read the `BeforeToolCallback`/`AfterToolCallback`/`BeforeAgentCallback` type aliases in `llm_agent.py`/`base_agent.py` | **Correct as written** |
| CrewAI | `crewai==1.15.21` (the real latest) | This sandbox's Python (3.14) is newer than CrewAI's declared support ceiling (`<3.14`), so a normal install silently fell back to an ancient `0.11.2`. Used `pip download --no-deps --only-binary=:all: --python-version 3.12 --implementation py --abi none` to fetch the actual current wheel without needing a matching interpreter, then inspected it directly | **Correct as written** — `TaskOutput.agent` changed type (object → plain `str`) between versions, but the adapter's existing fallback chain already handles both |
| LangChain | `langchain-core==1.6.2` | Installed with `--no-deps`; read `callbacks/base.py` directly | **Bug found and fixed** — see §4.4 |

All four adapter docstrings now say "verified against `<package>==<version>`" with the specifics, instead of "best-effort."

---

## 6. Known limitations (honest, not fixed in this pass)

- **Streaming pass-through is incomplete** for `/v1/responses` and Gemini `:streamGenerateContent` — both currently pass through the underlying chat-completions SSE shape rather than re-dialecting each chunk into the caller's native streaming format. Documented as a TODO in `modules/m3-agent-gateway/README.md`.
- **`/v1/embeddings` bypasses routing/governance/cache** — uses the lean auth-only path since embeddings don't carry generation-quality/prompt-injection surface, but this means A/B tests and routing policies don't apply to them.
- **`/v1/checkpoint` blocks synchronously on HITL** rather than returning `hitl_pending` immediately with separate polling — simpler for this pass, a real behavioral simplification versus the design doc's stated intent.
- **Gemini generateContent was not validated against a real Gemini API key** — no `GEMINI_API_KEY`/`GOOGLE_API_KEY` was available in this environment. The routing/model-prefix fix (§4.2) was confirmed correct via LiteLLM's own error message changing from a routing error to a credentials error, but no successful real Gemini response was observed.
- ~~The double-fire eval trigger for in-process multi-agent traces~~ — **fixed, see §4.7.** (Was: each new `agent.task` span re-triggered a full re-scan once the old fixed-cooldown dedup window expired mid-conversation, re-scoring all currently-visible task spans and producing 2-3x redundant `prompt_evals`/`eval_scores` rows. Replaced with a genuine cancel-and-reschedule debounce.)
- **`gateway_structural_events` and `messages_json` retention** — no TTL policy yet, same open question already tracked in `v2-gateway-capture-m1-ingest.md` §10.4 and `checkpoint-handoff-ingest.md` §9.3.
- **`backend_used` can be cosmetically wrong in `gateway_call_log`** for a passthrough-routed call whose model already carries a `provider/` prefix (e.g. shows `"openai"` for an Anthropic call) — the actual LLM dispatch is unaffected (it reads the prefix from `target_model` directly), only the logged/displayed backend field is wrong. Confirmed pre-existing in v2, not introduced by this rebuild. Not fixed — would need a `proxy.py` change to `resolve_routing`'s passthrough default, out of scope for this validation pass.

---

## 7. Net result

The three-layer capture stack described in `v2-gateway-capture-m1-ingest.md` and `checkpoint-handoff-ingest.md` is not just designed — it is running, and its central claim (gateway-only traffic, zero ACP SDK, produces full eval coverage; in-process traces are never double-scored) is confirmed against a real stack with real LLM calls. All four bugs found in this pass are fixed in the repository, not just noted.
