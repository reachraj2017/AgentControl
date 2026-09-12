"""A/B Testing — split production traffic between model/prompt variants."""

import json
import sys
import os

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import api
from db import db

@st.cache_data(ttl=60)
def _known_roles():
    return db.get_known_agent_roles()

if os.getenv("M3_ENABLED", "true").lower() != "true":
    st.warning("⚠️ Module 3 (Agent Gateway) is not enabled in this deployment.")
    st.stop()


st.sidebar.title("⚡ Agent Gateway")
api.sidebar_status()

st.title("⚗️ A/B Testing")
st.caption(
    "Split production traffic between two model or prompt variants. "
    "Each request is routed to Variant A or B based on the split ratio. "
    "Both variants receive real responses — no deferred scoring. "
    "Promote the winner to a permanent routing policy when you have enough data."
)

# ── Status colors ─────────────────────────────────────────────────────────────

_STATUS_ICON = {
    "draft":     "🔵",
    "running":   "🟢",
    "paused":    "🟡",
    "completed": "⚪",
    "deleted":   "🔴",
}

_BACKEND_OPTS = ["openai", "anthropic", "ollama", "gemini", "bedrock"]

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_active, tab_create, tab_all = st.tabs(["🟢 Active Tests", "➕ Create Test", "📋 All Tests"])


# ══ ACTIVE TESTS ═════════════════════════════════════════════════════════════

with tab_active:
    data  = api.get("/gateway/ab-tests?status=running", show_error=False) or {}
    tests = data.get("tests", [])

    if not tests:
        st.info("No A/B tests are currently running. Create one in the 'Create Test' tab.", icon="ℹ️")
    else:
        for test in tests:
            test_id   = test["test_id"]
            test_name = test.get("test_name", test_id[:8])

            with st.expander(f"🟢 **{test_name}** — `{test_id[:8]}`", expanded=True):
                c1, c2, c3 = st.columns([2, 2, 1])

                with c1:
                    st.markdown("**Variant A**")
                    st.code(f"Model: {test.get('variant_a_model', '(default)')}\nBackend: {test.get('variant_a_backend', 'openai')}")
                    if test.get("variant_a_prompt"):
                        st.caption(f"Prompt override: _{test['variant_a_prompt'][:120]}_")

                with c2:
                    st.markdown("**Variant B**")
                    st.code(f"Model: {test.get('variant_b_model', '(default)')}\nBackend: {test.get('variant_b_backend', 'openai')}")
                    if test.get("variant_b_prompt"):
                        st.caption(f"Prompt override: _{test['variant_b_prompt'][:120]}_")

                with c3:
                    split = float(test.get("split_ratio", 0.5))
                    st.metric("Split Ratio (→B)", f"{split:.0%}")
                    st.caption(f"Scope: `{test.get('agent_role','*')}` / `{test.get('system_id','*')}`")

                st.divider()
                st.subheader("Live Results")

                results = api.get(f"/gateway/ab-tests/{test_id}/results", show_error=False) or {}
                call_stats  = results.get("call_stats", [])
                eval_scores = results.get("eval_scores", [])

                if not call_stats:
                    st.info("No traffic recorded yet. Send some requests to see results.", icon="⏳")
                else:
                    cs_df = pd.DataFrame(call_stats)
                    cs_df.rename(columns={
                        "ab_variant":    "Variant",
                        "call_count":    "Calls",
                        "avg_latency_ms":"Avg Latency (ms)",
                        "avg_tokens_in": "Avg Tokens In",
                        "avg_tokens_out":"Avg Tokens Out",
                        "total_tokens":  "Total Tokens",
                    }, inplace=True)

                    m1, m2 = st.columns(2)
                    for _, row in cs_df.iterrows():
                        col = m1 if row["Variant"] == "A" else m2
                        with col:
                            st.markdown(f"**Variant {row['Variant']}** — {int(row['Calls'])} calls")
                            st.metric("Avg Latency", f"{int(row['Avg Latency (ms)'])} ms")
                            sub1, sub2 = st.columns(2)
                            sub1.metric("Avg Tokens In",  int(row["Avg Tokens In"]))
                            sub2.metric("Avg Tokens Out", int(row["Avg Tokens Out"]))

                    if eval_scores:
                        st.markdown("**Eval Scores** _(LLM judge — via shadow eval pipeline)_")
                        es_df = pd.DataFrame(eval_scores)
                        es_df.rename(columns={
                            "ab_variant":        "Variant",
                            "avg_faithfulness":  "Faithfulness",
                            "avg_relevance":     "Relevance",
                            "avg_instruction":   "Instruction Following",
                            "eval_count":        "Scored Calls",
                        }, inplace=True)
                        st.dataframe(es_df, use_container_width=True, hide_index=True,
                                     column_config={
                                         "Faithfulness":          st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1),
                                         "Relevance":             st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1),
                                         "Instruction Following": st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1),
                                     })

                        # Determine winner
                        es_map = {r["ab_variant"]: r for r in eval_scores}
                        cs_map = {r["ab_variant"]: r for r in call_stats}
                        a_score = (
                            float(es_map.get("A", {}).get("avg_faithfulness", 0))
                            + float(es_map.get("A", {}).get("avg_relevance", 0))
                            + float(es_map.get("A", {}).get("avg_instruction", 0))
                        ) / 3
                        b_score = (
                            float(es_map.get("B", {}).get("avg_faithfulness", 0))
                            + float(es_map.get("B", {}).get("avg_relevance", 0))
                            + float(es_map.get("B", {}).get("avg_instruction", 0))
                        ) / 3
                        a_calls = int(cs_map.get("A", {}).get("call_count", 0))
                        b_calls = int(cs_map.get("B", {}).get("call_count", 0))
                        min_calls = min(a_calls, b_calls)

                        winner_variant = "A" if a_score >= b_score else "B"
                        winner_model   = test.get(f"variant_{winner_variant.lower()}_model", "")
                        winner_backend = test.get(f"variant_{winner_variant.lower()}_backend", "openai")
                        margin = abs(a_score - b_score)

                        if min_calls >= 30:
                            sig_label = "Sufficient data (≥30 calls per variant)"
                            sig_icon  = "✅"
                        else:
                            sig_label = f"Gathering data ({min_calls}/30 calls per variant)"
                            sig_icon  = "⏳"

                        st.info(
                            f"{sig_icon} **{sig_label}** — "
                            f"Variant {winner_variant} leads by {margin:.3f} avg quality score.",
                            icon=None,
                        )

                        if winner_model:
                            if st.button(
                                f"🏆 Promote Variant {winner_variant} ({winner_model})",
                                key=f"promote_{test_id}",
                                type="primary",
                            ):
                                payload = json.dumps({
                                    "target_model":   winner_model,
                                    "target_backend": winner_backend,
                                    "model_match":    "",
                                    "reason":         f"A/B test '{test_name}' winner: variant {winner_variant}",
                                })
                                result = api.post("/gateway/changes", {
                                    "change_type": "routing_override",
                                    "agent_role":  test.get("agent_role", "*"),
                                    "system_id":   test.get("system_id",  "*"),
                                    "description": (
                                        f"Promote A/B test winner: Variant {winner_variant} "
                                        f"({winner_model}) scored {b_score if winner_variant == 'B' else a_score:.3f} "
                                        f"vs {a_score if winner_variant == 'B' else b_score:.3f}"
                                    ),
                                    "payload":     payload,
                                    "evidence":    f"A/B test {test_id}, {a_calls + b_calls} total calls",
                                    "proposed_by": "ab_testing",
                                })
                                if result:
                                    st.success(
                                        f"Change proposed: route `{test.get('agent_role','*')}` → "
                                        f"`{winner_model}`. Approve it in the **Gateway Changes** page.",
                                        icon="✅",
                                    )

                # Controls
                st.divider()
                ctrl1, ctrl2, ctrl3 = st.columns(3)
                with ctrl1:
                    if st.button("⏸️ Pause", key=f"pause_{test_id}"):
                        api.put(f"/gateway/ab-tests/{test_id}", {"status": "paused"})
                        st.success("Test paused.")
                        st.rerun()
                with ctrl2:
                    if st.button("✅ Complete", key=f"complete_{test_id}"):
                        api.put(f"/gateway/ab-tests/{test_id}", {"status": "completed"})
                        st.success("Test marked as completed.")
                        st.rerun()
                with ctrl3:
                    if st.button("🗑️ Delete", key=f"delete_{test_id}", type="secondary"):
                        api.delete(f"/gateway/ab-tests/{test_id}")
                        st.success("Test deleted.")
                        st.rerun()


# ══ CREATE TEST ══════════════════════════════════════════════════════════════

with tab_create:
    st.subheader("Create A/B Test")
    st.markdown(
        "Define two variants — each can differ in model, backend, or prompt prefix. "
        "Traffic is split by the ratio you set. Start the test when ready."
    )

    with st.form("create_ab_test"):
        test_name = st.text_input("Test Name *", placeholder="e.g. gpt-4o vs claude-haiku translator")

        c1, c2 = st.columns(2)
        with c1:
            agent_role = st.selectbox(
                "Agent Role",
                _known_roles(),
                help="'*' matches all agent roles.",
            )
        with c2:
            system_id = st.text_input("System ID", value="*", help="e.g. opt-demo — use * to match all")

        split_ratio = st.slider(
            "Variant B Traffic Split",
            min_value=0.05, max_value=0.95,
            value=0.50, step=0.05, format="%.0f%%",
            help="Fraction of requests routed to Variant B. Variant A gets the remainder.",
        )
        st.caption(
            f"Variant A: **{(1 - split_ratio):.0%}** of traffic — "
            f"Variant B: **{split_ratio:.0%}** of traffic"
        )

        st.divider()
        va_col, vb_col = st.columns(2)

        with va_col:
            st.markdown("### Variant A")
            va_model   = st.text_input("Model A", value="", placeholder="e.g. gpt-4o-mini (blank = use requested)", key="va_model")
            va_backend = st.selectbox("Backend A", _BACKEND_OPTS, key="va_backend")
            va_prompt  = st.text_area(
                "System Prompt Prefix A",
                value="",
                placeholder="Optional — prepended to the system prompt for every request in this variant",
                height=100,
                key="va_prompt",
            )

        with vb_col:
            st.markdown("### Variant B")
            vb_model   = st.text_input("Model B", value="", placeholder="e.g. anthropic/claude-haiku-4-5-20251001", key="vb_model")
            vb_backend = st.selectbox("Backend B", _BACKEND_OPTS, key="vb_backend")
            vb_prompt  = st.text_area(
                "System Prompt Prefix B",
                value="",
                placeholder="Optional — prepended to the system prompt for every request in this variant",
                height=100,
                key="vb_prompt",
            )

        submitted = st.form_submit_button("Create Test (draft)", type="primary")

    if submitted:
        if not test_name.strip():
            st.error("Test name is required.")
        elif not va_model.strip() and not vb_model.strip() and not va_prompt.strip() and not vb_prompt.strip():
            st.error("At least one variant must specify a model or a prompt prefix.")
        else:
            result = api.post("/gateway/ab-tests", {
                "test_name":         test_name.strip(),
                "agent_role":        agent_role.strip() or "*",
                "system_id":         system_id.strip()  or "*",
                "variant_a_model":   va_model.strip(),
                "variant_a_backend": va_backend,
                "variant_a_prompt":  va_prompt.strip(),
                "variant_b_model":   vb_model.strip(),
                "variant_b_backend": vb_backend,
                "variant_b_prompt":  vb_prompt.strip(),
                "split_ratio":       split_ratio,
            })
            if result:
                test_id = result.get("test_id", "")
                st.success(f"Test created in **draft** state (ID: `{test_id[:8]}`). Start it below.", icon="✅")
                if st.button("▶️ Start Now", type="primary", key="start_after_create"):
                    api.put(f"/gateway/ab-tests/{test_id}", {"status": "running"})
                    st.success("Test is now running. Traffic will be split immediately.", icon="🟢")
                    st.rerun()

    # Show draft tests so the user can start them
    draft_data  = api.get("/gateway/ab-tests?status=draft", show_error=False) or {}
    draft_tests = draft_data.get("tests", [])
    if draft_tests:
        st.divider()
        st.subheader("Draft Tests — Ready to Start")
        for t in draft_tests:
            col1, col2 = st.columns([4, 1])
            col1.markdown(f"🔵 **{t.get('test_name', t['test_id'][:8])}** `{t['test_id'][:8]}` — `{t.get('agent_role','*')}` / `{t.get('system_id','*')}`")
            with col2:
                if st.button("▶️ Start", key=f"start_draft_{t['test_id']}"):
                    api.put(f"/gateway/ab-tests/{t['test_id']}", {"status": "running"})
                    st.success("Test started.")
                    st.rerun()

    # Show paused tests
    paused_data  = api.get("/gateway/ab-tests?status=paused", show_error=False) or {}
    paused_tests = paused_data.get("tests", [])
    if paused_tests:
        st.divider()
        st.subheader("Paused Tests")
        for t in paused_tests:
            col1, col2 = st.columns([4, 1])
            col1.markdown(f"🟡 **{t.get('test_name', t['test_id'][:8])}** `{t['test_id'][:8]}`")
            with col2:
                if st.button("▶️ Resume", key=f"resume_{t['test_id']}"):
                    api.put(f"/gateway/ab-tests/{t['test_id']}", {"status": "running"})
                    st.success("Test resumed.")
                    st.rerun()


# ══ ALL TESTS ════════════════════════════════════════════════════════════════

with tab_all:
    st.subheader("All A/B Tests")

    all_data  = api.get("/gateway/ab-tests", show_error=False) or {}
    all_tests = all_data.get("tests", [])

    if not all_tests:
        st.info("No A/B tests created yet.", icon="ℹ️")
    else:
        rows = []
        for t in all_tests:
            rows.append({
                "Status":     _STATUS_ICON.get(t.get("status", ""), "?") + " " + t.get("status", ""),
                "Name":       t.get("test_name", ""),
                "Role":       t.get("agent_role", "*"),
                "System":     t.get("system_id", "*"),
                "Variant A":  t.get("variant_a_model", "(default)"),
                "Variant B":  t.get("variant_b_model", "(default)"),
                "Split → B":  f"{float(t.get('split_ratio', 0.5)):.0%}",
                "ID":         t.get("test_id", "")[:8],
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("View Results for a Completed Test")

        completed = [t for t in all_tests if t.get("status") in ("completed", "running", "paused")]
        if not completed:
            st.info("No completed or running tests to inspect.")
        else:
            sel_map = {f"{t.get('test_name', t['test_id'][:8])} [{t['test_id'][:8]}]": t for t in completed}
            sel_label = st.selectbox("Select test", list(sel_map.keys()), key="history_sel")
            sel_test  = sel_map[sel_label]
            sel_id    = sel_test["test_id"]

            results     = api.get(f"/gateway/ab-tests/{sel_id}/results", show_error=True) or {}
            call_stats  = results.get("call_stats", [])
            eval_scores = results.get("eval_scores", [])

            if not call_stats:
                st.info("No traffic data recorded for this test yet.")
            else:
                st.markdown("#### Call Statistics per Variant")
                cs_df = pd.DataFrame(call_stats)
                cs_df.rename(columns={
                    "ab_variant":    "Variant",
                    "call_count":    "Calls",
                    "avg_latency_ms":"Avg Latency (ms)",
                    "avg_tokens_in": "Avg Tokens In",
                    "avg_tokens_out":"Avg Tokens Out",
                    "total_tokens":  "Total Tokens",
                }, inplace=True)
                st.dataframe(cs_df, use_container_width=True, hide_index=True)

            if eval_scores:
                st.markdown("#### Eval Scores per Variant")
                es_df = pd.DataFrame(eval_scores)
                es_df.rename(columns={
                    "ab_variant":        "Variant",
                    "avg_faithfulness":  "Faithfulness",
                    "avg_relevance":     "Relevance",
                    "avg_instruction":   "Instruction Following",
                    "eval_count":        "Scored Calls",
                }, inplace=True)
                st.dataframe(es_df, use_container_width=True, hide_index=True,
                             column_config={
                                 "Faithfulness":          st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1),
                                 "Relevance":             st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1),
                                 "Instruction Following": st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1),
                             })
