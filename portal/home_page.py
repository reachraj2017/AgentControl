"""AI Control Plane — Home dashboard (content only, no set_page_config)."""

import os
import requests
import streamlit as st

M1_ENABLED = os.getenv("M1_ENABLED", "true").lower() == "true"
M2_ENABLED = os.getenv("M2_ENABLED", "true").lower() == "true"
M3_ENABLED = os.getenv("M3_ENABLED", "true").lower() == "true"
M4_ENABLED = os.getenv("M4_ENABLED", "true").lower() == "true"

EVAL_RUNNER_URL   = os.getenv("EVAL_RUNNER_URL",       "http://localhost:8000")
GOVERNANCE_URL    = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
GATEWAY_URL       = os.getenv("GATEWAY_URL",            "http://localhost:8001")
EVALGOV_AGENT_URL = os.getenv("EVALGOV_AGENT_URL",      "http://localhost:8003")

CLICKHOUSE_HOST     = os.getenv("CLICKHOUSE_HOST",     "localhost")
CLICKHOUSE_PORT     = int(os.getenv("CLICKHOUSE_PORT", "9000"))
CLICKHOUSE_DB       = os.getenv("CLICKHOUSE_DB",       "otel")
CLICKHOUSE_USER     = os.getenv("CLICKHOUSE_USER",     "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")

st.title("🎛️ AI Control Plane")
st.caption("Unified observability, evaluation, governance, and intelligence for AI agent systems")
st.divider()

# ── Module status tiles ────────────────────────────────────────────────────────

st.subheader("Module Status")
col1, col2, col3, col4 = st.columns(4)

def _status_tile(col, label, enabled, pages):
    with col:
        st.metric(
            label=label,
            value="Enabled" if enabled else "Disabled",
            delta=pages,
            delta_color="normal" if enabled else "off",
        )

_status_tile(col1, "M1 · Observability & Evaluation", M1_ENABLED, "Eval Testing · Measurements")
_status_tile(col2, "M2 · Governance & Enforcement",   M2_ENABLED, "AI Governance · Enforcement")
_status_tile(col3, "M3 · Agent Gateway",              M3_ENABLED, "Dashboard · Call Log · Routing…")
_status_tile(col4, "M4 · EvalGov Intelligence",       M4_ENABLED, "EvalGov Agent")

st.divider()

# ── System Health ──────────────────────────────────────────────────────────────

st.subheader("System Health")

def _ping_http(url):
    try:
        return requests.get(url, timeout=3).status_code < 500
    except Exception:
        return False

def _ping_clickhouse():
    try:
        import clickhouse_driver
        c = clickhouse_driver.Client(
            host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT,
            database=CLICKHOUSE_DB, user=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD, connect_timeout=3,
        )
        c.execute("SELECT 1")
        return True
    except Exception:
        return False

services = [
    ("Eval Runner",        f"{EVAL_RUNNER_URL}/health"),
    ("Governance Service", f"{GOVERNANCE_URL}/health"),
    ("Agent Gateway",      f"{GATEWAY_URL}/health"),
    ("EvalGov Agent",      f"{EVALGOV_AGENT_URL}/health"),
]
h_cols = st.columns(len(services) + 1)
for idx, (name, url) in enumerate(services):
    h_cols[idx].metric(label=name, value="UP" if _ping_http(url) else "DOWN")
h_cols[-1].metric(label="ClickHouse", value="UP" if _ping_clickhouse() else "DOWN")

st.divider()

# ── 24 h at a Glance ──────────────────────────────────────────────────────────

st.subheader("24h at a Glance")

def _ch_count(query):
    try:
        import clickhouse_driver
        c = clickhouse_driver.Client(
            host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT,
            database=CLICKHOUSE_DB, user=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD, connect_timeout=3,
        )
        result = c.execute(query)
        return str(result[0][0]) if result else "0"
    except Exception:
        return "unavailable"

g1, g2, g3, g4 = st.columns(4)
with g1:
    st.metric("Total Traces",  _ch_count("SELECT count() FROM otel.otel_traces WHERE Timestamp >= now() - INTERVAL 24 HOUR"))
with g2:
    st.metric("Eval Scores",   _ch_count("SELECT count() FROM otel.prompt_evals WHERE created_at >= now() - INTERVAL 24 HOUR"))
with g3:
    st.metric("Policy Events", _ch_count("SELECT count() FROM otel.gov_policy_decisions WHERE ts >= now() - INTERVAL 24 HOUR"))
with g4:
    st.metric("Gateway Calls", _ch_count("SELECT count() FROM otel.gateway_call_log WHERE created_at >= now() - INTERVAL 24 HOUR"))

st.divider()

# ── Navigation Guide ───────────────────────────────────────────────────────────

st.subheader("Navigation Guide")
nav1, nav2, nav3, nav4 = st.columns(4)

with nav1:
    st.markdown("**M1 · Observability & Evaluation**")
    st.markdown(
        "- **Eval Testing** — Run prompt evaluations and inspect results\n"
        "- **Eval Measurements** — Trends, scores, and quality metrics over time"
    )
with nav2:
    st.markdown("**M2 · Governance & Enforcement**")
    st.markdown(
        "- **AI Governance** — 13-category compliance framework dashboard\n"
        "- **Enforcement** — Circuit breakers, trust scores, quality gates, HITL"
    )
with nav3:
    st.markdown("**M3 · Agent Gateway**")
    st.markdown(
        "- **Gateway Dashboard** — Real-time traffic overview\n"
        "- **Call Log** — Every LLM call through the gateway\n"
        "- **Routing** — Model routing policy editor\n"
        "- **Prompt Mods** — Inject text into prompts at the gateway\n"
        "- **Shadow Mode** — Duplicate requests to alternate models\n"
        "- **A/B Testing** — Split traffic between model variants\n"
        "- **Changes** — Review and approve proposed changes\n"
        "- **API Keys** — Manage gateway API keys"
    )
with nav4:
    st.markdown("**M4 · EvalGov Intelligence**")
    st.markdown("- **EvalGov Agent** — Conversational AI agent with live system state panel")

st.divider()

if st.button("🔄 Refresh"):
    st.rerun()
