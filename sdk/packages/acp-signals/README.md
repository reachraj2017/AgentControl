# acp-signals

Explicit `checkpoint()` / `handoff()` / `tool_span()` calls for the AI Control Plane — the signals
that **never cross the LLM wire** and so can never be captured by gateway wire capture (M3) or by
any OTel/OpenInference/OpenLLMetry instrumentor, no matter how complete: pre-action governance
gates, sub-agent handoffs, and non-LLM tool executions.

These are **explicit, developer-added calls**, not passive instrumentation. Passive interception
(patching a framework's client, registering a competing global tracer) is fragile against a
framework's internal changes in a way an explicit function call at a point you already control is
not — the call site is yours, so it doesn't rot when the framework's internals change underneath it.

## Installation

Not published to PyPI — install by path, pointed at wherever you cloned the control-plane repo (see its `docs/instrumentation-guide.md` "Prerequisites" section):

```bash
export ACP_REPO=/path/to/the/control-plane-repo
pip install "$ACP_REPO/sdk/packages/acp-signals"
# optional, per framework adapter:
pip install "$ACP_REPO/sdk/packages/acp-signals[openai-agents]"
pip install "$ACP_REPO/sdk/packages/acp-signals[adk]"
pip install "$ACP_REPO/sdk/packages/acp-signals[langchain]"
pip install "$ACP_REPO/sdk/packages/acp-signals[crewai]"
```

## Quick start

```python
from acp_signals import context, checkpoint, handoff, tool_span

# Set once per request/turn — reused by all three calls below unless overridden.
context.set(conversation_id="conv-123", system_id="my-product", agent_role="orchestrator")

# Pre-action gate — BLOCKS until the gateway (forwarding to M2 governance-service) decides.
decision = checkpoint("send_email", risk_level="high", metadata={"to": "user@example.com"})
if decision.decision == "block":
    raise PermissionError(decision.reason)
elif decision.decision == "hitl_pending":
    ...  # your app's own wait/poll logic — see design doc for the pattern

# Sub-agent handoff — fire-and-forget.
handoff("orchestrator", "summarizer", context_summary="user asked for a 30-word summary")

# Non-LLM tool execution — fire-and-forget.
tool_span("web_search", input={"query": "quantum computing"}, output={"hits": 5}, latency_ms=210)
```

By default the client reads the gateway URL and key from the same env vars `acp-gateway` and
`opt-demo` already use: `ACP_GATEWAY_URL` (default `http://localhost:8080`) and `GATEWAY_API_KEY`.
Call `acp_signals.configure(gateway_url=..., api_key=...)` to override, or construct a
`SignalsClient(...)` directly for multiple independent clients in one process.

## What each call does

| Call | Blocks? | Gateway endpoint | Purpose |
|---|---|---|---|
| `checkpoint(action, risk_level, metadata)` | **Yes** | `POST /v1/checkpoint` | Pre-action HITL/policy gate. Fails open (`decision="allow"`, `error` set) on network failure, matching the gateway's documented fail-open enforcement posture — check `decision.error` if you need fail-closed behavior for a specific action. |
| `handoff(from_agent, to_agent, context_summary)` | No | `POST /v1/handoff` | Marks a sub-agent transition, for `handoff_fidelity` scoring. |
| `tool_span(tool_name, input, output, status, latency_ms)` | No | `POST /v1/tool-span` | Marks a non-LLM tool execution, for `tool_selection_accuracy` / `tool_argument_accuracy` / `tool_error_rate` scoring. |

`handoff()` and `tool_span()` run on a small background thread pool — they never block or raise
into the caller, the same "must never crash the host application" posture as `acp-tracing`.

## Framework adapters

Each adapter translates ONE framework's own officially-documented callback/hook/plugin interface
into the three calls above — never undocumented internals. Import the one you need directly (they
are not auto-imported, so installing `acp-signals` alone never requires any of these frameworks):

| Adapter | Confidence | Framework extension point used |
|---|---|---|
| `acp_signals.adapters.langchain.ACPCallbackHandler` | **Verified** against `langchain-core==1.6.2` source | `on_tool_start`/`on_tool_end`/`on_tool_error` — the tool name/input is captured in `on_tool_start` and looked up by `run_id` in the paired end/error callback, since `on_tool_end`/`on_tool_error`'s own signatures don't carry a `name`/`inputs` kwarg; `on_chain_start` name-change heuristic for handoffs is best-effort by design (LangChain has no first-class handoff event) — see module docstring |
| `acp_signals.adapters.openai_agents.ACPRunHooks` | **Verified** against `openai-agents==0.22.2` source (`agents/lifecycle.py`) | `agents.RunHooks` (`on_handoff`, `on_tool_start`/`on_tool_end`) — signatures match exactly, no changes needed |
| `acp_signals.adapters.google_adk` (`acp_before_agent_callback`, `acp_before_tool_callback`, `acp_after_tool_callback`) | **Verified** against `google-adk==2.8.0` source | `before_agent_callback` / `before_tool_callback` / `after_tool_callback` — signatures match exactly, no changes needed |
| `acp_signals.adapters.crewai` (`acp_step_callback`, `acp_task_callback`) | **Verified** against the actual latest published wheel, `crewai==1.15.21` | `Crew(step_callback=..., task_callback=...)` — still current; `TaskOutput.agent` changed type (object → plain `str`) across versions but this adapter's fallback chain already handles both correctly |

**Read each adapter's module docstring before deploying it** — it states exactly which package
version was verified against and how (each adapter was checked by installing or downloading the
real package and reading its source directly, not from documentation alone). If a framework changes
its hook signature in a future release, only that one file needs to change; the
`handoff()`/`tool_span()` calls it makes are stable.

### Example — LangChain

```python
from acp_signals.adapters.langchain import ACPCallbackHandler

chain.invoke(input, config={"callbacks": [ACPCallbackHandler()]})
```

### Example — OpenAI Agents SDK

```python
from agents import Runner
from acp_signals.adapters.openai_agents import ACPRunHooks

result = await Runner.run(starting_agent, input=user_message, hooks=ACPRunHooks())
```

### No framework at all (raw/custom agent loop)

Call `checkpoint()` / `handoff()` / `tool_span()` directly at the relevant lines in your own code —
see `sdk/examples/custom_agent_signals.py`.

## Context propagation

`acp_signals.context` is a plain `contextvars.ContextVar` holder, not a global OTel tracer — it
does not compete with a framework's own tracing (the failure mode named above). Set it once per
request/turn; adapters and application code can partially update it at finer granularity (e.g. a
handoff hook re-setting `agent_role` when the active agent changes) without needing to know or
re-supply the other fields.
