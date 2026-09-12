"""Shadow Mode — run requests against an alternate model for comparison."""

import pandas as pd
import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import api
from db import db

@st.cache_data(ttl=60)
def _known_roles():
    return db.get_known_agent_roles()

import os
if os.getenv("M3_ENABLED", "true").lower() != "true":
    st.warning("⚠️ Module 3 (Agent Gateway) is not enabled in this deployment.")
    st.stop()


st.sidebar.title("⚡ Agent Gateway")
api.sidebar_status()

st.title("👥 Shadow Mode")
st.caption(
    "Duplicate a configurable sample of requests to an alternate model. "
    "The caller always receives the primary response. The shadow call runs async in the background. "
    "Both calls appear in the Call Log — use `is_shadow=true` to filter them."
)

# ── Current rules ─────────────────────────────────────────────────────────────

data  = api.get("/gateway/shadow")
rules = (data or {}).get("rules", [])

if rules:
    df = pd.DataFrame(rules)
    st.dataframe(
        df[[c for c in ["agent_role", "system_id", "shadow_model",
                        "shadow_backend", "sample_rate", "rule_id"] if c in df.columns]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "sample_rate": st.column_config.NumberColumn("Sample Rate", format="%.0%%"),
        },
    )

    rule_map = {
        f"{r['agent_role']} → {r['shadow_model']} @ {r['sample_rate']:.0%} [{r['rule_id'][:8]}]": r
        for r in rules
    }

    st.divider()
    st.subheader("Manage Shadow Rules")

    sel_label = st.selectbox("Select rule to manage", list(rule_map.keys()), key="shadow_sel")
    sel = rule_map[sel_label]

    tab_edit, tab_delete = st.tabs(["✏️ Edit", "🗑️ Delete"])

    with tab_edit:
        st.markdown(
            f"**Editing:** `{sel['rule_id'][:8]}` — role `{sel['agent_role']}` / "
            f"system `{sel['system_id']}`"
        )
        st.caption("Agent role and system ID are fixed. Edit shadow target and sample rate below.")

        c1, c2 = st.columns(2)
        with c1:
            edit_shadow_model = st.text_input(
                "Shadow Model", value=sel.get("shadow_model", ""),
                key="edit_shadow_model",
                help="Model to send shadow requests to.",
            )
            backend_opts   = ["openai", "ollama"]
            cur_backend    = sel.get("shadow_backend", "openai")
            edit_shadow_backend = st.selectbox(
                "Shadow Backend", backend_opts,
                index=backend_opts.index(cur_backend) if cur_backend in backend_opts else 0,
                key="edit_shadow_backend",
            )
        with c2:
            edit_sample_rate = st.slider(
                "Sample Rate",
                min_value=0.01, max_value=1.0,
                value=float(sel.get("sample_rate", 0.1)),
                step=0.01, format="%.0f%%",
                key="edit_sample_rate",
            )
            st.info(
                f"At {edit_sample_rate:.0%}, roughly **{edit_sample_rate * 100:.0f}** "
                "shadow calls per 100 primary calls."
            )

        if st.button("Save Changes", type="primary", key="save_shadow_btn"):
            if not edit_shadow_model.strip():
                st.error("Shadow model is required.")
            else:
                result = api.put(f"/gateway/shadow/{sel['rule_id']}", {
                    "shadow_model":   edit_shadow_model.strip(),
                    "shadow_backend": edit_shadow_backend,
                    "sample_rate":    edit_sample_rate,
                })
                if result:
                    st.success("Shadow rule updated. Takes effect within 30 s.")
                    st.rerun()

    with tab_delete:
        st.warning(
            f"This will disable the shadow rule for **{sel['agent_role']}** → "
            f"**{sel['shadow_model']}**. Shadow calls will stop immediately after the next refresh.",
            icon="⚠️",
        )
        if st.button("Delete Rule", type="secondary", key="delete_shadow_btn"):
            if api.delete(f"/gateway/shadow/{sel['rule_id']}"):
                st.success("Shadow rule deleted. Takes effect within 30 s.")
                st.rerun()

else:
    st.info("No shadow rules active.")

st.divider()

# ── Create new rule ───────────────────────────────────────────────────────────

with st.expander("➕ Add Shadow Rule", expanded=not bool(rules)):
    with st.form("create_shadow"):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Which requests to shadow**")
            agent_role = st.selectbox(
                "Agent Role",
                _known_roles(),
                help="'*' shadows all roles",
            )
            system_id = st.text_input("System ID", "*",
                                      help="'*' matches any system")
            sample_rate = st.slider(
                "Sample Rate",
                min_value=0.01, max_value=1.0, value=0.10, step=0.01,
                format="%.0f%%",
                help=(
                    "Fraction of matching requests to shadow. "
                    "Start low to limit cost; raise once you want statistical significance."
                ),
            )
        with c2:
            st.markdown("**Shadow target**")
            shadow_model = st.text_input(
                "Shadow Model", "gpt-4o",
                help="Model to send the shadow request to.",
            )
            shadow_backend = st.selectbox(
                "Shadow Backend", ["openai", "ollama"],
                help="openai → OPENAI_BASE_URL | ollama → OLLAMA_BASE_URL",
            )
            st.info(
                f"At {sample_rate:.0%} sample rate, roughly "
                f"**{sample_rate * 100:.0f}** shadow calls per 100 primary calls. "
                "Shadow calls are fire-and-forget — they never delay the primary response."
            )

        submitted = st.form_submit_button("Create Shadow Rule", type="primary")

    if submitted:
        if not shadow_model.strip():
            st.error("Shadow model is required.")
        else:
            result = api.post("/gateway/shadow", {
                "agent_role":     agent_role,
                "system_id":      system_id,
                "shadow_model":   shadow_model.strip(),
                "shadow_backend": shadow_backend,
                "sample_rate":    sample_rate,
            })
            if result:
                st.success("Shadow rule created. Takes effect within 30 s.")
                st.rerun()

# ── Shadow call comparison ────────────────────────────────────────────────────

st.divider()
st.subheader("Shadow vs Primary Comparison")
st.caption("Filter the call log to compare shadow and primary responses for the same run.")

all_calls = api.get("/gateway/calls?limit=500", show_error=False)
calls     = (all_calls or {}).get("calls", [])

if calls:
    df_all = pd.DataFrame(calls)
    if "is_shadow" in df_all.columns and df_all["is_shadow"].any():
        shadow_runs  = set(df_all[df_all["is_shadow"]  == 1]["run_id"].dropna())
        primary_runs = set(df_all[df_all["is_shadow"]  == 0]["run_id"].dropna())
        paired_runs  = shadow_runs & primary_runs

        if paired_runs:
            sel_run = st.selectbox(
                "Select a run ID to compare",
                sorted(paired_runs, reverse=True)[:50],
                format_func=lambda r: r[:16] + "…",
            )
            pair_df = df_all[df_all["run_id"] == sel_run].sort_values("is_shadow")
            for _, row in pair_df.iterrows():
                label  = "👥 Shadow" if row.get("is_shadow") else "✅ Primary"
                model  = row.get("model_used", "?")
                tokens = int(row.get("tokens_out", 0))
                lat    = int(row.get("latency_ms", 0))
                with st.container(border=True):
                    st.markdown(f"**{label}** — `{model}` | {tokens} output tokens | {lat} ms")
                    st.text_area(
                        "Response",
                        value=row.get("response_text", ""),
                        height=150,
                        disabled=True,
                        key=f"shadow_resp_{row.get('call_id','')[:8]}",
                        label_visibility="collapsed",
                    )
        else:
            st.info("Shadow calls exist but none are paired with a primary call in the same run yet.")
    else:
        st.info("No shadow calls recorded yet. Create a shadow rule above, then run some queries through the gateway.")
else:
    st.info("No calls in the log yet.")

# ── Shadow eval scores ────────────────────────────────────────────────────────

st.divider()
st.subheader("Shadow Eval Scores")
st.caption(
    "The eval-runner scores shadow responses in the background (faithfulness, relevance, "
    "instruction_following). Scores appear here once the pipeline has run. "
    "Shadow scores are stored in `gateway_shadow_evals`, separate from `prompt_evals`."
)

def _ch_query(sql: str) -> list[dict]:
    """Run a raw ClickHouse query via HTTP interface."""
    import os, json, httpx
    host = os.getenv("CLICKHOUSE_HOST", "clickhouse")
    port = int(os.getenv("CLICKHOUSE_HTTP_PORT", "8123"))
    user = os.getenv("CLICKHOUSE_USER", "default")
    pw   = os.getenv("CLICKHOUSE_PASSWORD", "")
    url  = f"http://{host}:{port}/"
    resp = httpx.post(
        url,
        content=(sql + " FORMAT JSONEachRow").encode(),
        params={"user": user, "password": pw},
        timeout=15.0,
    )
    resp.raise_for_status()
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


try:
    shadow_score_rows = _ch_query("""
        SELECT
            agent_role,
            model_used,
            round(avgIf(scores['faithfulness'],          scores['faithfulness']          > 0), 3) AS avg_faithfulness,
            round(avgIf(scores['relevance'],             scores['relevance']             > 0), 3) AS avg_relevance,
            round(avgIf(scores['instruction_following'], scores['instruction_following'] > 0), 3) AS avg_instruction,
            count() AS scored_calls,
            max(scored_at) AS last_scored
        FROM otel.gateway_shadow_evals
        WHERE scored_at >= now() - INTERVAL 7 DAY
        GROUP BY agent_role, model_used
        ORDER BY agent_role, scored_calls DESC
    """)
except Exception:
    shadow_score_rows = []

if shadow_score_rows:
    score_df = pd.DataFrame(shadow_score_rows)
    st.dataframe(
        score_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "avg_faithfulness": st.column_config.NumberColumn("Faithfulness", format="%.3f"),
            "avg_relevance":    st.column_config.NumberColumn("Relevance",    format="%.3f"),
            "avg_instruction":  st.column_config.NumberColumn("Instruction",  format="%.3f"),
            "scored_calls":     st.column_config.NumberColumn("Scored Calls"),
            "last_scored":      st.column_config.DatetimeColumn("Last Scored"),
        },
    )

    # Side-by-side comparison against primary eval scores
    try:
        primary_score_rows = _ch_query("""
            SELECT
                pe.agent_name AS agent_role,
                gw.model_used,
                round(avgIf(pe.scores['faithfulness'],          pe.scores['faithfulness']          > 0), 3) AS avg_faithfulness,
                round(avgIf(pe.scores['relevance'],             pe.scores['relevance']             > 0), 3) AS avg_relevance,
                round(avgIf(pe.scores['instruction_following'], pe.scores['instruction_following'] > 0), 3) AS avg_instruction,
                count() AS eval_count
            FROM otel.prompt_evals pe
            LEFT JOIN (
                SELECT trace_id, model_used
                FROM otel.gateway_call_log
                WHERE is_shadow = 0 AND trace_id != ''
            ) gw ON pe.trace_id = gw.trace_id
            WHERE pe.created_at >= now() - INTERVAL 7 DAY
              AND gw.model_used != ''
            GROUP BY pe.agent_name, gw.model_used
        """)
    except Exception:
        primary_score_rows = []

    if primary_score_rows:
        st.markdown("**Primary vs Shadow — score comparison (last 7 days)**")
        p_df = pd.DataFrame(primary_score_rows).rename(columns={
            "avg_faithfulness": "pri_faith",
            "avg_relevance":    "pri_rel",
            "avg_instruction":  "pri_inst",
            "eval_count":       "pri_count",
        })
        s_df = score_df.rename(columns={
            "avg_faithfulness": "shad_faith",
            "avg_relevance":    "shad_rel",
            "avg_instruction":  "shad_inst",
            "scored_calls":     "shad_count",
        })[["agent_role", "model_used", "shad_faith", "shad_rel", "shad_inst", "shad_count"]]
        merged = p_df.merge(s_df, on=["agent_role", "model_used"], how="outer")
        st.dataframe(merged.round(3), use_container_width=True, hide_index=True)
else:
    st.info(
        "No shadow eval scores yet. "
        "The eval-runner background task runs every 60 s — scores appear once shadow calls accumulate. "
        "Check eval-runner logs for `shadow_eval_scored` to confirm the pipeline is running."
    )

# ── Pipeline Shadow Evals ─────────────────────────────────────────────────────

st.divider()
st.subheader("Pipeline Shadow Evals")
st.caption(
    "Full task-level A/B comparison: the shadow model runs the **entire agent pipeline** "
    "(search → summarize → translate) with the same inputs and tools, not just a single LLM call. "
    "Scores reflect how each model performs on the complete task."
)

_hours_pipe = st.selectbox(
    "Window", [24, 48, 168, 336, 720],
    index=2, format_func=lambda h: f"Last {h}h",
    key="pipe_shadow_hours",
)

summary_data = api.get(
    f"/gateway/pipeline-shadow-evals?summary=true&hours={_hours_pipe}",
    show_error=False,
)
summary_rows = (summary_data or {}).get("summary", [])

if summary_rows:
    s_df = pd.DataFrame(summary_rows)

    st.markdown("**Aggregate scores — primary vs shadow (task level)**")
    st.dataframe(
        s_df.rename(columns={
            "agent_role":         "Agent",
            "primary_model":      "Primary Model",
            "shadow_model":       "Shadow Model",
            "evals":              "Tasks",
            "avg_pri_faith":      "Primary Faith",
            "avg_pri_rel":        "Primary Rel",
            "avg_pri_inst":       "Primary Instr",
            "avg_shad_faith":     "Shadow Faith",
            "avg_shad_rel":       "Shadow Rel",
            "avg_shad_inst":      "Shadow Instr",
            "avg_pri_latency_ms": "Primary Latency (ms)",
            "avg_shad_latency_ms":"Shadow Latency (ms)",
        }),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Primary Faith":  st.column_config.NumberColumn(format="%.3f"),
            "Primary Rel":    st.column_config.NumberColumn(format="%.3f"),
            "Primary Instr":  st.column_config.NumberColumn(format="%.3f"),
            "Shadow Faith":   st.column_config.NumberColumn(format="%.3f"),
            "Shadow Rel":     st.column_config.NumberColumn(format="%.3f"),
            "Shadow Instr":   st.column_config.NumberColumn(format="%.3f"),
        },
    )

    # Verdict per row
    for row in summary_rows:
        if row.get("evals", 0) < 5:
            continue
        metrics = ["faith", "rel", "inst"]
        wins = sum(
            1 for m in metrics
            if row.get(f"avg_shad_{m}", 0) - row.get(f"avg_pri_{m}", 0) > 0.05
        )
        role   = row.get("agent_role", "?")
        shm    = row.get("shadow_model", "?")
        ntasks = row.get("evals", 0)
        if wins >= 2:
            st.success(
                f"**{role}** — shadow model `{shm}` outperforms primary on {wins}/3 metrics "
                f"over {ntasks} tasks. Consider promoting via a routing_override change."
            )
        elif wins == 0 and all(
            row.get(f"avg_pri_{m}", 0) - row.get(f"avg_shad_{m}", 0) > 0.05
            for m in metrics
        ):
            st.warning(
                f"**{role}** — shadow model `{shm}` underperforms primary on all metrics "
                f"over {ntasks} tasks. Consider disabling this shadow rule."
            )

    # Side-by-side response comparison for recent evals
    st.markdown("---")
    st.markdown("**Recent task comparisons**")
    recent_data = api.get(
        f"/gateway/pipeline-shadow-evals?hours={_hours_pipe}&limit=20",
        show_error=False,
    )
    recent_rows = (recent_data or {}).get("evals", [])
    if recent_rows:
        sel_idx = st.selectbox(
            "Select task to inspect",
            range(len(recent_rows)),
            format_func=lambda i: (
                f"[{recent_rows[i].get('created_at','')!s:.16}] "
                f"{recent_rows[i].get('agent_role','?')} — "
                f"{recent_rows[i].get('user_input','')[:60]}…"
            ),
            key="pipe_shadow_sel",
        )
        row = recent_rows[sel_idx]
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                f"**Primary** — `{row.get('primary_model','?')}` | "
                f"{row.get('primary_latency_ms', 0)} ms"
            )
            st.markdown(
                f"Faith: **{row.get('primary_faithfulness', 0):.2f}** | "
                f"Rel: **{row.get('primary_relevance', 0):.2f}** | "
                f"Instr: **{row.get('primary_instruction', 0):.2f}**"
            )
            st.text_area(
                "Primary Response",
                value=row.get("primary_response", ""),
                height=200, disabled=True,
                key=f"pipe_pri_{sel_idx}",
                label_visibility="collapsed",
            )
        with c2:
            st.markdown(
                f"**Shadow** — `{row.get('shadow_model','?')}` | "
                f"{row.get('shadow_latency_ms', 0)} ms"
            )
            st.markdown(
                f"Faith: **{row.get('shadow_faithfulness', 0):.2f}** | "
                f"Rel: **{row.get('shadow_relevance', 0):.2f}** | "
                f"Instr: **{row.get('shadow_instruction', 0):.2f}**"
            )
            st.text_area(
                "Shadow Response",
                value=row.get("shadow_response", ""),
                height=200, disabled=True,
                key=f"pipe_shad_{sel_idx}",
                label_visibility="collapsed",
            )
else:
    st.info(
        "No pipeline shadow evals yet. "
        "Create a shadow rule above, then run queries through the agent. "
        "Each query fires the shadow pipeline in the background — results appear here "
        "after scoring completes (typically 10–30 s per task)."
    )

# ── Help ──────────────────────────────────────────────────────────────────────

with st.expander("How shadow mode enables self-improvement"):
    st.markdown("""
**Two levels of shadow evaluation:**

| | Call-level (gateway shadow) | Pipeline-level (shadow pipeline) |
|---|---|---|
| **What runs** | Single LLM call intercepted by gateway | Full agent pipeline: search → summarize → translate |
| **Tool calls** | Never executed for shadow | Re-executed with same inputs |
| **What's measured** | One prompt → one response | Complete task from user input to final answer |
| **Scores** | faithfulness, relevance, instruction_following | Same 3 metrics, but at task level |
| **Cost** | Low — one extra LLM call per shadowed request | Higher — full pipeline re-execution |
| **Accuracy** | Limited — misses multi-step reasoning | True comparison — model does the whole job |

**The pipeline shadow experiment loop:**
1. Create a shadow rule for the agent role (e.g. `searcher → gpt-4o` at 10%)
2. Run queries through the agent — pipeline shadow fires automatically in the background
3. Each shadowed query: shadow model runs search + synthesis (same search results, different model)
4. Both primary and shadow responses are scored at task level by LLM judges
5. Compare scores in **Pipeline Shadow Evals** above
6. If shadow wins on 2+ metrics over ≥10 tasks → propose a `routing_override` change
7. Approve the change — all subsequent requests route to the better model

**Call-level shadow scores** (in the section above) are still useful for fast iteration when you
just want to check if a model can handle single-step synthesis before committing to full pipeline runs.

**Cost control:** pipeline shadow doubles inference cost for shadowed requests. At 10% sample rate,
that's +10% total cost. Raise to 50% only once you want statistical significance faster.
Shadow never affects user latency — the background thread starts after the primary response returns.
""")
