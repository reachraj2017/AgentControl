"""Call Log — every LLM call that has flowed through the gateway."""

import pandas as pd
import plotly.express as px
import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import api

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
try:
    from db import db as _db
    def _token_rates() -> tuple[float, float]:
        rows = _db._execute(
            "SELECT config_key, value FROM otel.gov_threshold_config FINAL "
            "WHERE config_key IN ('budget.input_token_cost_per_1m','budget.output_token_cost_per_1m')"
        )
        m = {r["config_key"]: float(r["value"]) for r in (rows or [])}
        return (
            m.get("budget.input_token_cost_per_1m",  0.15) / 1_000_000,
            m.get("budget.output_token_cost_per_1m", 0.60) / 1_000_000,
        )
    _in_rate, _out_rate = _token_rates()
except Exception:
    _in_rate, _out_rate = 0.15 / 1_000_000, 0.60 / 1_000_000

if os.getenv("M3_ENABLED", "true").lower() != "true":
    st.warning("⚠️ Module 3 (Agent Gateway) is not enabled in this deployment.")
    st.stop()


st.sidebar.title("⚡ Agent Gateway")
api.sidebar_status()

st.title("📋 Call Log")
st.caption("Every LLM call intercepted by the gateway — primary, shadow, blocked, and errors.")

# ── Filters ───────────────────────────────────────────────────────────────────

_HOUR_OPTIONS = {"1 hr": 1, "6 hrs": 6, "12 hrs": 12, "24 hrs": 24, "48 hrs": 48, "72 hrs": 72, "1 week": 168}

with st.expander("Filters", expanded=True):
    fc1, fc2, fc3, fc4, fc5, fc6 = st.columns(6)
    with fc1:
        filter_system = st.text_input("System ID", "", placeholder="opt-demo")
    with fc2:
        filter_role = st.selectbox(
            "Agent Role",
            ["(all)", "orchestrator", "searcher", "summarizer", "translator", "unknown"],
        )
    with fc3:
        filter_status = st.selectbox("Status", ["(all)", "ok", "error", "blocked"])
    with fc4:
        filter_hours_label = st.selectbox("Time window", list(_HOUR_OPTIONS.keys()), index=3)
        filter_hours = _HOUR_OPTIONS[filter_hours_label]
    with fc5:
        limit = st.number_input("Max rows", min_value=10, max_value=2000, value=200, step=50)

# Build URL
url = f"/gateway/calls?limit={limit}&hours={filter_hours}"
if filter_system.strip():
    url += f"&system_id={filter_system.strip()}"
if filter_role != "(all)":
    url += f"&agent_role={filter_role}"

data  = api.get(url)
calls = (data or {}).get("calls", [])

if not calls:
    st.info("No calls match the current filters.")
    st.stop()

df = pd.DataFrame(calls)

# Client-side status filter (API doesn't expose it yet)
if filter_status != "(all)" and "status" in df.columns:
    df = df[df["status"] == filter_status]


def _trunc(text, n=120):
    s = str(text or "")
    return s[:n] + "…" if len(s) > n else s


# Truncated prompt/response columns for the main table — same convention as
# Prompt Analysis (M1 Eval Measurements) — full untruncated text is still
# available below in Call Detail for any selected row.
if "prompt_text" in df.columns:
    df["prompt"] = df["prompt_text"].apply(lambda x: _trunc(x, 120))
if "response_text" in df.columns:
    df["response"] = df["response_text"].apply(lambda x: _trunc(x, 150))

# Compute cost_usd from token counts using governance config rates
if "tokens_in" in df.columns and "tokens_out" in df.columns:
    df["cost_usd"] = (
        df["tokens_in"].fillna(0)  * _in_rate +
        df["tokens_out"].fillna(0) * _out_rate
    ).round(6)

# ── Summary strip ─────────────────────────────────────────────────────────────

s1, s2, s3, s4, s5, s6, s7, s8 = st.columns(8)
s1.metric("Showing", len(df))
s2.metric("Errors",    int((df["status"] == "error").sum())  if "status"             in df.columns else 0)
s3.metric("Blocked",   int((df["enforcement_result"] == "blocked").sum()) if "enforcement_result" in df.columns else 0)
s4.metric("Shadow",    int((df["is_shadow"] == 1).sum())     if "is_shadow"          in df.columns else 0)
s5.metric("Cache Hits",   int((df["cache_hit"] == 1).sum())     if "cache_hit"    in df.columns else 0)
s6.metric("Fallbacks",    int((df["fallback_used"] == 1).sum()) if "fallback_used" in df.columns else 0)
if "tokens_in" in df.columns and "tokens_out" in df.columns:
    total_tok  = int(df["tokens_in"].fillna(0).sum() + df["tokens_out"].fillna(0).sum())
    total_cost = df["cost_usd"].sum() if "cost_usd" in df.columns else 0.0
    s7.metric("Total Tokens", f"{total_tok:,}")
    s8.metric("Est. Cost", f"${total_cost:.4f}")

# ── Charts ────────────────────────────────────────────────────────────────────

if len(df) > 1:
    ch1, ch2 = st.columns(2)
    with ch1:
        if "model_used" in df.columns:
            model_counts = df.groupby("model_used").size().reset_index(name="calls")
            fig = px.bar(
                model_counts, x="model_used", y="calls",
                title="Calls by Model Used",
                color="model_used",
                color_discrete_sequence=px.colors.qualitative.Pastel,
            )
            fig.update_layout(
                showlegend=False, height=240,
                margin=dict(l=0, r=0, t=30, b=0),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#fafafa",
            )
            st.plotly_chart(fig, use_container_width=True)
    with ch2:
        if "latency_ms" in df.columns:
            fig2 = px.histogram(
                df[df["latency_ms"] > 0],
                x="latency_ms",
                nbins=30,
                title="Latency Distribution (ms)",
                color_discrete_sequence=["#7c83fd"],
            )
            fig2.update_layout(
                height=240,
                margin=dict(l=0, r=0, t=30, b=0),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font_color="#fafafa",
            )
            st.plotly_chart(fig2, use_container_width=True)

# ── Main table ────────────────────────────────────────────────────────────────

ordered_cols = [
    "created_at", "system_id", "agent_role",
    "model_requested", "model_used", "backend_used",
    "prompt", "response",
    "routing_reason", "cache_hit", "fallback_used",
    "tokens_in", "tokens_out", "cost_usd", "latency_ms",
    "mods_applied", "enforcement_result", "status",
    "is_shadow", "run_id", "call_id",
]
df_show = df[[c for c in ordered_cols if c in df.columns]].copy()

st.dataframe(
    df_show,
    use_container_width=True,
    hide_index=True,
    column_config={
        "prompt":             st.column_config.TextColumn("Prompt", width="medium"),
        "response":           st.column_config.TextColumn("Response", width="medium"),
        "latency_ms":         st.column_config.NumberColumn("Latency ms", format="%d"),
        "tokens_in":          st.column_config.NumberColumn("Tok In"),
        "tokens_out":         st.column_config.NumberColumn("Tok Out"),
        "cost_usd":           st.column_config.NumberColumn("Cost USD", format="$%.6f"),
        "is_shadow":          st.column_config.CheckboxColumn("Shadow"),
        "cache_hit":          st.column_config.CheckboxColumn("Cached"),
        "fallback_used":      st.column_config.CheckboxColumn("Fallback"),
        "enforcement_result": st.column_config.TextColumn("Enforcement"),
    },
)

# ── Call detail ───────────────────────────────────────────────────────────────

if "call_id" in df.columns:
    st.divider()
    st.subheader("Call Detail")
    call_options = {
        f"{r.get('created_at','')[:19]}  {r.get('agent_role','')}  →  {r.get('model_used','')}  [{r.get('call_id','')[:8]}]": r.get("call_id")
        for _, r in df.iterrows()
    }
    selected_label = st.selectbox("Select a call to inspect", list(call_options.keys()))
    if selected_label:
        selected_id = call_options[selected_label]
        row = df[df["call_id"] == selected_id].iloc[0].to_dict()
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Prompt (last user message)**")
            st.text_area("", value=row.get("prompt_text", ""), height=200, disabled=True, label_visibility="collapsed")
        with c2:
            st.markdown("**Response**")
            st.text_area("", value=row.get("response_text", ""), height=200, disabled=True, label_visibility="collapsed")
        st.json({k: v for k, v in row.items() if k not in ("prompt_text", "response_text")})
