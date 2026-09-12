# User Guide — AI Control Plane Portal

This guide walks through every part of the portal: how to navigate it, what each page shows, what actions you can take, and how to read what you see. It assumes the stack is running (`make up`) and you have the portal open at http://localhost:8888.

---

## First time in the portal

The portal is a single Streamlit app with all pages registered under one navigation router (`portal/Home.py`). Two things to know before you start clicking around:

- **The left sidebar is grouped by module** — "M1 · Observability & Evaluation", "M2 · Governance & Enforcement", "M3 · Agent Gateway", "M4 · EvalGov Intelligence" — each a section header over its own pages. The Home page sits above all four groups, ungrouped.
- **Every page uses the full browser width** for its dashboard area (a wide-layout override), not the narrower centered layout Streamlit uses by default.

If you don't see the grouped sidebar or full-width layout, hard-refresh the browser tab (Cmd+Shift+R) — it's almost always a stale cached page, not a real problem.

**Where to start:** the Home page shows a live status panel for all services. Once you've instrumented an agent (pointed it at the gateway — see `docs/instrumentation-guide.md`), the first places you'll see its data appear are **M3 → Call Log** (every LLM call, within seconds) and **M1 → Eval Measurements** / **Prompt Analysis** (scores, typically ~10-90 seconds later depending on how many LLM judges ran).

### First time setup checklist

1. Run `make up` and wait for all services to report healthy (`docker compose ps`).
2. Open the portal — confirm the Home page shows all services green.
3. Go to **M3 → API Keys** and create a virtual gateway key (`gw-sk-*`) for each agent system you want to connect.
4. If `GATEWAY_AUTH_ENABLED=true` (recommended), every call to the gateway must carry one of these keys — set it as your agent's `OPENAI_API_KEY` (or equivalent).
5. Point your agent at the gateway (`http://localhost:8080/v1`, or the protocol-specific path — see the instrumentation guide) — calls will now flow through the control plane.

---

## M1 — Observability & Evaluation

### Eval Testing

This page manages **benchmark runs** — not an ad-hoc "paste a prompt, get a score" tool. It has four tabs:

**Runs tab**
- Create a run: name, suite (`unit`/`integration`/`collaboration`/`production`), agent version, run type, an **Agent Endpoint** URL (your running agent's HTTP endpoint), and which benchmark test cases to include.
- Execute a run — the portal sends each benchmark task to your agent endpoint through the full pipeline (including the gateway), one at a time, with inline progress.
- Mark any run as the ⭐ baseline for regression comparison.
- Tag runs with a **Run Group** to run/compare a whole suite together.
- Trigger an offline re-evaluation to re-score a run's existing traces (e.g. after changing evaluator config).

**Benchmarks & Review tab** (two sub-tabs)
- **Benchmark Test Cases** — create/manage test cases: task input, expected output, a JSON rubric for custom LLM-judge scoring, suite, difficulty, dataset version, tags.
- **Human Review Queue** — review auto-scored evals, override a score, add a note.

**Scores tab**
- Per-metric average score bar chart and score distribution histogram for any run.
- Score trend lines over the last 7 days.
- Individual score table with evaluator name and LLM-judge reasoning. Filter by metric or by `eval_type`.

**Regression tab**
- Pick any two runs and compare per-metric scores side by side: Regressed 🔴 / Improved 🟢 / Unchanged ⚪, plus a radar chart overlay against the baseline.

---

### Eval Measurements

Time-series view of eval scores across all agents and metrics, independent of any specific benchmark run — this is what live production traffic populates.

**What you can do:**
- Select an agent role, metric, and time range using the filters at the top.
- Switch between trend view (line chart over time) and distribution view (histogram of scores).
- Enable regression detection to see automatically flagged score drops compared to the rolling baseline.

**Reading the results:**
- A downward trend in correctness or faithfulness after a deployment is a signal to roll back or investigate the prompt.
- The regression detector flags when the last 2 hours drops more than 10% below the prior 6-hour baseline.
- Filter by `agent_role` to isolate one agent's performance from the system average.

**What to watch for:**
- Sudden drops in `task_success` or `tool_selection_accuracy` after a model change.
- Rising `hallucination_score` or `toxicity_score` — these also trigger M2 safety alerts.
- `cost_usd` trending upward without a corresponding quality improvement.
- If you just instrumented a new agent and expect scores but see none yet: check **M3 → Call Log** first to confirm calls are even reaching the gateway, then give it up to ~90 seconds (LLM-judge evaluation is a sequential cascade, not instant) before assuming something's wrong.

---

## M2 — Governance & Enforcement

### AI Governance

Governance state across all agents, organized as 13 category tabs (matching M2's 13-category framework) plus a Summary and a Thresholds tab:

`Summary` · `1 · Auditability` · `2 · Identity & Access` · `3 · Data & Privacy` · `4 · Safety & Guardrails` · `5 · Policy Engine` · `6 · Reliability / SRE` · `7 · Behavior` · `8 · Compliance Fit` · `9 · Budget & Cost` · `10 · Explainability` · `11 · Lifecycle` · `12 · Incidents` · `13 · Regulatory` · `⚙ Thresholds`

**Summary tab** is the place to start: governance overview, policy gate summary, compliance scorecard, and governance metric trends at a glance.

A few tabs worth knowing about specifically:
- **Auditability** — trace lineage lookup (paste a `trace_id` to see its full span-by-span lineage, policy decisions, and PII events for that call) and the raw audit log.
- **Incidents** — open/resolved incidents by severity (P0 critical → P3 low); resolving one here is the same action as resolving it from EvalGov.
- **Budget & Cost** — per-agent spend tracking and budget threshold configuration.

**Trust scores:** each agent role carries a rolling trust score (0–1), updated after each governance evaluation based on policy adherence, safety events, and incident history. Below 0.5 is a warning state that generates an EvalGov finding; below 0.4 triggers stricter enforcement on the agent's next gateway call.

---

### Enforcement

Real-time enforcement controls, in three tabs:

**⚡ Agent Pre-Execution Enforcement**
- Toggle the enforcement mode (fail-open vs fail-closed — see the note on this in `modules/m2-governance-enforcement/README.md`; know which posture you're running in, since fail-open lets a policy failure through rather than blocking it).
- **Agent Trust Scores**, **Burn Rate Alerts** (how fast an agent is accumulating policy violations), **Rogue Agent Detection** (agents flagged for quarantine), and **Circuit Breakers & Kill Switch** (per-agent state: `CLOSED`/`OPEN`/`HALF_OPEN`, with a manual **Reset**).
- An on-demand enforcement cycle you can trigger manually.

**🔬 Content Quality Enforcement**
- Quality gate mode, configured gates (add a new gate: metric + threshold + action), and the decision log (holds vs. blocks and why).

**🧑‍⚖️ HITL Approvals**
- Pending approvals: agent, requested action, context, and wait time — **Approve**/**Reject** buttons that resume or stop the agent immediately.
- Full decision history.
- Requests waiting longer than `HITL_TIMEOUT_MINUTES` (default 15) generate an EvalGov finding.

---

## M3 — Agent Gateway

### Gateway Dashboard

Live traffic view for the gateway.

**Header metrics row:** total calls, cache hit rate, error rate, average latency, total cost — for the selected time window (1 hr / 6 hrs / 12 hrs / 24 hrs / 48 hrs / 1 week).

**Sections:** Enforcement (pass/block summary) and Circuit Breakers state; Active Policies & Controls; Recent Calls; Calls by Agent Role; Cost by Model; Enforcement Outcomes.

There's currently no per-protocol breakdown on this page — every protocol-complete call (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`, Gemini `generateContent`) is logged with a `protocol` column in `gateway_call_log`, but neither this page nor Call Log currently filters or displays it. If you need to distinguish traffic by protocol today, query ClickHouse directly.

---

### Call Log

Full request-level history — every LLM call that passed through the gateway, across all four protocols.

**Filters:** agent role, status (`ok`/`error`/`blocked`), time window.

**Reading each row:**
- `routing_reason` — why this call was routed the way it was (e.g. `pool:abc:def` for pool routing, `passthrough` for unmatched).
- `tokens_in` / `tokens_out` — real, provider-reported token counts.
- `latency_ms` — end-to-end wall-clock time.
- `cache_hit` — whether the exact-match or semantic cache served this call instead of the upstream LLM.

**Drilling into a call:** click a row (via the "Select a call to inspect" picker) to expand the full request/response and gateway metadata. Use the `trace_id` to find the corresponding trace in Jaeger (http://localhost:16686).

**Not currently visible in the portal:** checkpoint/handoff/tool-span structural signals (the `gateway_structural_events` table, written by `/v1/checkpoint`, `/v1/handoff`, `/v1/tool-span`) have no dedicated page yet. Inspect them via a direct ClickHouse query (`SELECT * FROM otel.gateway_structural_events WHERE conversation_id = '...'`) or via each endpoint's own response — there's no portal UI for them today.

---

### Routing

Model routing policies — rules that determine which upstream model handles calls for a given agent role.

**What you can do:**
- Create a routing policy: agent role, target model, target backend, optional fallback model/backend.
- Delete a policy — the gateway reverts to passthrough for that role.

**How routing priority works** (first match wins):
1. Traffic policy (if the agent role is bound to a pool)
2. A/B test (if an active test matches the role)
3. Routing policy (model-level rule)
4. Passthrough (forward as requested)

---

### Prompt Mods

System prompt modifications injected by the gateway — prefix, suffix, or few-shot text added to every call for a given agent role or system ID, without the agent's own code knowing.

**What you can do:**
- Create a mod: scope (agent role and/or system ID), mod type, content.
- Enable/disable without deleting.
- See which calls a mod affected via the call log's `mods_applied` field.

---

### Shadow Mode

Duplicate traffic to a secondary model without affecting the response returned to the caller.

**How it works:** a call matching a shadow rule is sent to both the primary model (response returned) and the shadow model (response discarded after scoring). The shadow response is evaluated and stored for comparison.

**What you can do:**
- Create a shadow rule: source agent role → shadow model + backend, sample rate.
- View the shadow-vs-primary comparison.

**Reading the comparison:** EvalGov generates a finding if the shadow model wins by more than 10% faithfulness across 20+ evals in 24 hours.

---

### A/B Testing

Traffic-split experiments across model variants.

**What you can do:**
- Create a test: name, agent role, variant A model/backend/prompt, variant B model/backend/prompt, split ratio.
- Start/stop/delete a test.

**Reading results:** per-variant call counts, latency, tokens, and eval scores (faithfulness, relevance, instruction-following). EvalGov flags a test as stale if it's been running longer than 24 hours.

---

### Changes

Proposed change log — configuration changes proposed by an agent (usually via EvalGov) awaiting operator approval before they take effect.

**What you can do:** review a pending change (what it will do and the evidence behind it), **Approve** to apply it immediately, **Reject** with a reason, or browse the history of prior decisions.

---

### API Keys

Virtual gateway keys (`gw-sk-*`), scoped per agent system.

**What you can do:**
- Create a key: description, agent role scope, system ID scope, allowed models, daily token limit, rate limit (requests/minute), budget alert.
- Update a key's limits without revoking it.
- Revoke a key — immediate effect.
- View per-key usage.

**Best practice:** one key per agent system; set a budget alert; use a model allowlist to keep an agent from silently calling a more expensive model than intended.

---

### Traffic Management

Endpoint pools and traffic policies — load-balancing across multiple LLM endpoints, in three tabs:

**🗂️ Pools** — view all pools, their endpoints, and any linked policy; delete a pool/policy, or add an endpoint inline.

**➕ New Pool** — one form: pool name + strategy, endpoint rows (model, backend, weight, priority), and the traffic policy binding it to an agent role, all in one submit.

**6 load-balancing strategies:**

| Strategy | When to use |
|---|---|
| `round_robin` | Even distribution across endpoints |
| `weighted` | Proportional split — e.g. 70% one model, 30% another |
| `least_latency` | Always route to the currently fastest endpoint |
| `performance` | Route to the endpoint with the highest recent faithfulness score |
| `cost_optimized` | Prefer the cheapest endpoint; overflow to others under load |
| `fallback_chain` | Try endpoints in priority order; move on only on failure |

**📊 Live Stats** — per-endpoint call counts, average latency, tokens, and error rate, for a selectable time window (1 hr up to 1 week).

**A real gotcha to know about:** a `weighted` pool with a local/small model mixed in alongside hosted models will occasionally route production-looking traffic to that local model — and if it's a reasoning-style model, its reported token usage can include internal reasoning tokens that never appear in the visible response, making cost/token figures for that endpoint look wildly disproportionate to what you can see. If a call's token count looks inexplicably high, check `model_used`/`backend_used` in Call Log for that call before assuming it's a bug.

Enable `sticky=true` on a traffic policy to pin a whole conversation to the same endpoint (needed for stateful models).

---

## M4 — EvalGov Agent

The conversational interface to the entire control plane. Ask anything in natural language.

**What the page shows:**
- **Chat panel** — send messages to the EvalGov coordinator, which routes to the right M1/M2/M3 sub-agent (or chains several) and synthesizes one answer.
- **System Findings** — live proactive findings from the background monitor (15 checks, every 60s), grouped by Governance / Gateway / Quality & Cost.
- **System State** — live snapshot: pending HITL count, open circuit breakers, recent policy violations.

**System Findings panel:**
- Each finding shows severity, affected agent, summary, and timestamp.
- Expand **↳ RCA + Recommendation** for the AI-generated root cause analysis and recommended action.
- **Dismiss** to acknowledge (stays visible at 50% opacity; underlying condition still tracked).
- **↩ Reopen** to mark resolved and let the monitor re-detect it next cycle.
- **▶ Run Checks** triggers all 15 monitor checks immediately (can take 30–60s if new findings need RCA generation).

**Asking questions:** see [`docs/evalgov-playbook.md`](evalgov-playbook.md) for a full playbook of example questions by scenario.

---

## opt-demo — the pre-instrumented demo

A working multi-agent system (orchestrator → searcher, summarizer, translator) that routes through the gateway and is fully instrumented — a live example of everything above.

```bash
cd opt-demo
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 server.py            # REST API on :8090 — used by the Eval Testing "Agent Endpoint" field
# or
streamlit run chat_ui.py     # interactive chat UI on :8501
```

`make demo` (from the repo root) is a shortcut for the Streamlit chat UI specifically; there's no single `make demo-up` that starts everything — `make up` starts the control plane, and opt-demo is a separate, ordinary Python app you run alongside it as shown above. See `opt-demo/README.md` for full setup, including the virtual-key step required when `GATEWAY_AUTH_ENABLED=true`.

All demo agent calls appear in the gateway Call Log, get evaluated by M1, and are governed by M2. Ask EvalGov about the demo's agents (`orchestrator`, `searcher`, `summarizer`, `translator`) to see end-to-end integration live.
