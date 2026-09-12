"""Agent Gateway — Dashboard (main page)."""

import pandas as pd
import plotly.express as px
import streamlit as st

import api

import os
if os.getenv("M3_ENABLED", "true").lower() != "true":
    st.warning("⚠️ Module 3 (Agent Gateway) is not enabled in this deployment.")
    st.stop()


st.sidebar.title("⚡ Agent Gateway")
api.sidebar_status()

st.title("⚡ Agent Gateway")
st.caption("Universal Agent control plane - routing, enforcement, caching, rate limiting, observability.")

# ── Window controls ───────────────────────────────────────────────────────────

_HOUR_OPTIONS = {"1 hr": 1, "6 hrs": 6, "12 hrs": 12, "24 hrs": 24, "48 hrs": 48, "72 hrs": 72, "1 week": 168}
wc1, wc2, wc3 = st.columns([2, 2, 8])
with wc1:
    _win_label = st.selectbox("Time window", list(_HOUR_OPTIONS.keys()), index=3, key="dash_hours")
    _win_hours = _HOUR_OPTIONS[_win_label]
with wc2:
    _win_rows = st.number_input("Max rows", min_value=10, max_value=2000, value=200, step=50, key="dash_rows")

# ── Fetch data ────────────────────────────────────────────────────────────────

status = api.get(f"/gateway/status?hours={_win_hours}")
if not status:
    st.warning("Cannot load dashboard. Is the gateway running?")
    st.stop()

stats      = status.get("call_stats_24h", {})
stats_hours = int(status.get("stats_hours", 24))
counts     = status.get("policy_counts",  {})
cache_info = status.get("cache", {})
auth_info  = status.get("auth",  {})

# Pull live governance state (fails silently if governance is down)
phase2_data = api.gov_get("/enforcement/phase2/status") or {}
cb_list     = api.gov_get("/enforcement/circuit-breakers") or []

# ── Auth / Security status bar ────────────────────────────────────────────────

auth_enabled  = auth_info.get("enabled",    False)
master_set    = auth_info.get("master_key", False)
cache_ttl     = float(cache_info.get("ttl_seconds", 0))
cache_live    = int(cache_info.get("live_entries",  0))
sem_cache_info    = status.get("semantic_cache", {})
sem_cache_ttl     = float(sem_cache_info.get("ttl_seconds",  0))
sem_cache_live    = int(sem_cache_info.get("live_entries",   0))
sem_threshold     = float(sem_cache_info.get("threshold",    0.92))
sem_embed_model   = sem_cache_info.get("embed_model", "text-embedding-3-small")

status_cols = st.columns(5)
with status_cols[0]:
    if master_set:
        st.success("Admin key set", icon="🔒")
    else:
        st.warning("No admin key", icon="⚠️")
with status_cols[1]:
    if auth_enabled:
        st.success("Auth: required", icon="🔑")
    else:
        st.info("Auth: open (dev)", icon="🔓")
with status_cols[2]:
    if cache_ttl > 0:
        st.success(f"Exact cache: ON  TTL {int(cache_ttl)}s  ({cache_live} live)", icon="⚡")
    else:
        st.info("Exact cache: disabled", icon="💤")
with status_cols[3]:
    if sem_cache_ttl > 0:
        st.success(f"Semantic cache: ON  ({sem_cache_live} live)", icon="🧠")
    else:
        st.info("Semantic cache: disabled", icon="💤")
with status_cols[4]:
    st.info("Change store refresh: 30 s", icon="🔄")

# ── Unified cache inspector ───────────────────────────────────────────────────
if cache_ttl > 0 or sem_cache_ttl > 0:
    total_live = cache_live + sem_cache_live
    expander_label = f"⚡ Cache Inspector — {total_live} live entr{'y' if total_live == 1 else 'ies'}"
    if sem_cache_ttl > 0:
        expander_label += f"  ·  semantic threshold {sem_threshold}  ·  embed: {sem_embed_model}"
    with st.expander(expander_label, expanded=True):
        btn_cols = st.columns([3, 1, 1])
        with btn_cols[1]:
            if st.button("🗑️ Flush Exact", type="secondary", use_container_width=True):
                if api.delete("/gateway/cache"):
                    st.success("Exact cache flushed.")
                    st.rerun()
        with btn_cols[2]:
            if st.button("🗑️ Flush Semantic", type="secondary", use_container_width=True):
                if api.delete("/gateway/semantic-cache"):
                    st.success("Semantic cache flushed.")
                    st.rerun()

        all_data = api.get("/gateway/cache/all-entries", show_error=False) or {}
        all_entries = all_data.get("entries", [])
        if not all_entries:
            st.caption("No cached responses yet. Send a request through the gateway to populate.")
        else:
            rows = [{
                "Type":             e.get("type", "").capitalize(),
                "Model":            e.get("model", ""),
                "Query":            e.get("query", "")[:120],
                "TTL (s)":          int(e.get("ttl_remaining_s", 0)),
                "Prompt tokens":    int(e.get("prompt_tokens", 0)),
                "Completion tokens":int(e.get("completion_tokens", 0)),
                "Response preview": e.get("response_preview", "")[:120],
            } for e in all_entries]
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Type":             st.column_config.TextColumn(width="small"),
                    "TTL (s)":          st.column_config.NumberColumn(format="%d s", width="small"),
                    "Prompt tokens":    st.column_config.NumberColumn(format="%d",   width="small"),
                    "Completion tokens":st.column_config.NumberColumn(format="%d",   width="small"),
                    "Query":            st.column_config.TextColumn(width="medium"),
                    "Response preview": st.column_config.TextColumn(width="large"),
                },
            )

st.divider()

# ── Live Enforcement Status ───────────────────────────────────────────────────

phase2_on   = phase2_data.get("phase2_enabled", False)
open_cbs    = [cb for cb in cb_list if cb.get("state") == "open"]
half_open   = [cb for cb in cb_list if cb.get("state") == "half_open"]
closed_cbs  = [cb for cb in cb_list if cb.get("state") == "closed"]

enf_col1, enf_col2 = st.columns([1, 3])

with enf_col1:
    st.subheader("Enforcement")
    if phase2_on:
        st.error("Phase 2 ACTIVE — gate checks enforced on every call", icon="🚨")
    else:
        st.info("Phase 2 OFF — observability only, calls pass through", icon="👁️")

with enf_col2:
    st.subheader("Circuit Breakers")
    if not cb_list:
        st.caption("Governance service unreachable — CB status unknown.")
    else:
        cb_cols = st.columns(max(len(cb_list), 1))
        for i, cb in enumerate(cb_list):
            state  = cb.get("state", "unknown")
            role   = cb.get("agent_role", "?")
            fails  = cb.get("failure_count", 0)
            thresh = cb.get("failure_threshold", 5)
            reason = cb.get("quarantine_reason", "")
            with cb_cols[i]:
                if state == "open":
                    st.error(f"**{role}**\n\n🔴 OPEN — blocked\n\n_{reason}_", icon="🚫")
                elif state == "half_open":
                    st.warning(f"**{role}**\n\n🟡 HALF-OPEN\n\n{fails}/{thresh} failures", icon="⚠️")
                else:
                    st.success(f"**{role}**\n\n🟢 closed\n\n{fails}/{thresh} failures", icon="✅")

if open_cbs:
    st.warning(
        f"**{len(open_cbs)} agent(s) currently blocked by circuit breaker:** "
        + ", ".join(f"`{cb['agent_role']}`" for cb in open_cbs)
        + ". These blocks fire when the agent calls the governance gate directly — "
        "they may not appear in the Gateway Call Log if the agent never reached `/v1/chat/completions`.",
        icon="⚠️",
    )

st.divider()

# ── 24-Hour Call Metrics ──────────────────────────────────────────────────────

st.subheader(f"Last {_win_label}")

m1, m2, m3, m4, m5, m6, m7, m8, m9 = st.columns(9)
m1.metric("Total Calls",     int(stats.get("total_calls",      0)))
m2.metric("Successful",      int(stats.get("ok_calls",         0)))
m3.metric("Errors",          int(stats.get("error_calls",      0)))
m4.metric("Blocked",         int(stats.get("blocked_calls",    0)))
m5.metric("Cache Hits",      int(stats.get("cache_hits",       0)))
m6.metric("Fallbacks Used",  int(stats.get("fallback_count",   0)))
m7.metric("Total Tokens",    f"{int(stats.get('total_tokens',  0)):,}")
m8.metric("Avg Latency",     f"{stats.get('avg_latency_ms',   0):.0f} ms")
cost = stats.get("estimated_cost_usd", 0.0)
m9.metric("Est. Cost",       f"${cost:.4f}")

st.divider()

# ── Active Policy Counts ──────────────────────────────────────────────────────

st.subheader("Active Policies & Controls")
p1, p2, p3, p4, p5, p6 = st.columns(6)
p1.metric("Routing Overrides",    counts.get("routing_policies", 0),
          help="Model/backend routing overrides — first match wins")
p2.metric("Fallback Routes",      sum(
              1 for _ in range(counts.get("routing_policies", 0))
          ),  # placeholder — actual count shown via routing policies detail
          help="See Routing page for policies with fallback configured")
p3.metric("Prompt Modifications", counts.get("prompt_mods",      0),
          help="Active system-prefix / suffix / few-shot injections")
p4.metric("Shadow Rules",         counts.get("shadow_rules",     0),
          help="Shadow-mode rules duplicating calls to a secondary model")
p5.metric("A/B Tests",            counts.get("ab_tests_running", 0),
          help="Live traffic-split A/B experiments")
p6.metric("API Keys",             counts.get("api_keys_active",  0),
          help="Active gateway virtual API keys")

st.divider()

# ── Recent calls + charts ─────────────────────────────────────────────────────

calls_data = api.get(f"/gateway/calls?limit={_win_rows}&hours={_win_hours}", show_error=False)
calls      = (calls_data or {}).get("calls", [])

# ── Row 1: Recent Calls | Calls by Agent Role ─────────────────────────────────

col_calls, col_role = st.columns([3, 2])

if calls:
    df = pd.DataFrame(calls)
    if "tokens_in" in df.columns and "tokens_out" in df.columns:
        df["cost_usd"] = (
            df["tokens_in"].fillna(0)  * 0.00000015 +
            df["tokens_out"].fillna(0) * 0.00000060
        ).round(6)
    df_chart = df.copy()
else:
    df = df_chart = None

with col_calls:
    st.subheader("Recent Calls")
    if df is not None:
        show_cols = [
            "created_at", "system_id", "agent_role",
            "model_requested", "model_used", "backend_used",
            "routing_reason", "cache_hit", "fallback_used",
            "tokens_in", "tokens_out", "cost_usd", "latency_ms",
            "enforcement_result", "status",
        ]
        st.dataframe(
            df[[c for c in show_cols if c in df.columns]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "latency_ms":         st.column_config.NumberColumn("Latency (ms)", format="%d"),
                "tokens_in":          st.column_config.NumberColumn("Tok In"),
                "tokens_out":         st.column_config.NumberColumn("Tok Out"),
                "cost_usd":           st.column_config.NumberColumn("Cost USD", format="$%.6f"),
                "cache_hit":          st.column_config.CheckboxColumn("Cached"),
                "fallback_used":      st.column_config.CheckboxColumn("Fallback"),
                "enforcement_result": st.column_config.TextColumn("Enforcement"),
                "created_at":         st.column_config.TextColumn("Time"),
            },
        )
    else:
        st.info("No calls yet. Send a request through the gateway to see it here.")
        st.code(
            'curl -X POST http://localhost:8080/v1/chat/completions \\\n'
            '  -H "Content-Type: application/json" \\\n'
            '  -H "X-Gateway-System-Id: my-system" \\\n'
            '  -H "X-Gateway-Agent-Role: searcher" \\\n'
            "  -d '{\"model\":\"gpt-4o-mini\","
            '"messages":[{"role":"user","content":"Hello"}]}\''
        )

with col_role:
    st.subheader("Calls by Agent Role")
    if df_chart is not None and "agent_role" in df_chart.columns:
        role_counts = (
            df_chart.groupby("agent_role")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=True)
        )
        fig = px.bar(
            role_counts, x="count", y="agent_role", orientation="h",
            color="agent_role",
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        fig.update_layout(
            showlegend=False,
            margin=dict(l=0, r=0, t=0, b=0),
            height=300,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font_color="#fafafa",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Charts appear after first calls flow through.")

st.divider()

# ── Row 2: Cost by Model | Enforcement Outcomes ───────────────────────────────

col_cost, col_enf = st.columns(2)

with col_cost:
    st.subheader("Cost by Model")
    if df_chart is not None and "cost_usd" in df_chart.columns and "model_used" in df_chart.columns:
        cost_by_model = (
            df_chart.groupby("model_used")["cost_usd"]
            .sum()
            .reset_index()
            .sort_values("cost_usd", ascending=False)
        )
        cost_by_model["cost_usd"] = cost_by_model["cost_usd"].round(6)
        fig3 = px.bar(
            cost_by_model, x="model_used", y="cost_usd",
            color="model_used",
            color_discrete_sequence=px.colors.qualitative.Pastel,
        )
        fig3.update_layout(
            showlegend=False,
            margin=dict(l=0, r=0, t=30, b=0),
            height=260,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font_color="#fafafa",
            xaxis_title="", yaxis_title="$ est. cost",
        )
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("Cost chart appears after first calls flow through.")

with col_enf:
    st.subheader("Enforcement Outcomes")
    if df_chart is not None and "enforcement_result" in df_chart.columns:
        enf = (
            df_chart.groupby("enforcement_result")
            .size()
            .reset_index(name="count")
        )
        color_map = {
            "pass":          "#4CAF50",
            "blocked":       "#f44336",
            "hitl_approved": "#2196F3",
            "hitl_rejected": "#FF9800",
            "hitl_timeout":  "#9E9E9E",
        }
        fig2 = px.pie(
            enf, values="count", names="enforcement_result",
            color="enforcement_result", color_discrete_map=color_map,
            hole=0.5,
        )
        fig2.update_layout(
            showlegend=True,
            margin=dict(l=0, r=0, t=30, b=0),
            height=260,
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#fafafa",
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Enforcement chart appears after first calls flow through.")

# ── Refresh ───────────────────────────────────────────────────────────────────

st.divider()
col_r, col_hint = st.columns([1, 5])
with col_r:
    if st.button("Refresh", type="secondary", use_container_width=True):
        st.rerun()
with col_hint:
    st.caption(
        f"Dashboard shows last {_win_rows} rows over {_win_label}. "
        "Use **Call Log** for full history with filters."
    )
