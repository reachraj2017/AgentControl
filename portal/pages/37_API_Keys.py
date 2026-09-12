"""API Key Management — create and revoke gateway API keys."""

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import api
from db import db

if os.getenv("M3_ENABLED", "true").lower() != "true":
    st.warning("⚠️ Module 3 (Agent Gateway) is not enabled in this deployment.")
    st.stop()


st.sidebar.title("⚡ Agent Gateway")
api.sidebar_status()

st.title("🔑 API Key Management")

_AUTH_ENABLED = os.getenv("GATEWAY_AUTH_ENABLED", "false").lower() == "true"
_MASTER_SET   = bool(os.getenv("GATEWAY_MASTER_KEY", ""))

if not _MASTER_SET:
    st.warning(
        "**GATEWAY_MASTER_KEY** is not set — admin endpoints are open to anyone on the network. "
        "Set it in your `.env` file and restart the stack to enable protection.",
        icon="⚠️",
    )
else:
    if _AUTH_ENABLED:
        st.success("Auth enabled — `/v1/chat/completions` requires a valid gateway API key.", icon="🔒")
    else:
        st.info(
            "**GATEWAY_AUTH_ENABLED=false** — agents can call `/v1/chat/completions` without a key. "
            "Admin write endpoints are protected by the master key. "
            "Set `GATEWAY_AUTH_ENABLED=true` to require keys on agent calls too.",
            icon="ℹ️",
        )

st.caption(
    "Virtual keys let agents call the gateway without holding real provider API keys. "
    "Each key can be scoped to an agent role, a model allowlist, and a daily token budget."
)

tab_keys, tab_create, tab_events = st.tabs(["🗝️ Active Keys", "➕ Create Key", "🚨 Key Events"])


# ══ ACTIVE KEYS ══════════════════════════════════════════════════════════════

with tab_keys:
    data = api.get("/gateway/keys", show_error=True) or {}
    keys = data.get("keys", [])

    if not keys:
        st.info("No API keys created yet. Use the 'Create Key' tab to issue your first key.", icon="ℹ️")
    else:
        rows = []
        for k in keys:
            rows.append({
                "Prefix":         k.get("key_prefix", ""),
                "Description":    k.get("description", ""),
                "Role":           k.get("agent_role", "*"),
                "System":         k.get("system_id", "*"),
                "Admin":          "✅" if k.get("is_admin") else "—",
                "RPM Limit":      int(k.get("rate_limit_rpm", 0)) or "∞",
                "Daily Tokens":   f"{k['daily_token_limit']:,}" if k.get("daily_token_limit") else "∞",
                "Budget Alert $": f"${k['budget_alert_usd']:.2f}" if k.get("budget_alert_usd") else "—",
                "Calls Today":    int(k.get("calls_today",  0)),
                "Tokens Today":   int(k.get("tokens_today", 0)),
                "Allowed Models": ", ".join(k.get("allowed_models") or []) or "all",
                "key_id":         k.get("key_id", ""),
            })

        df = pd.DataFrame(rows)
        st.dataframe(
            df.drop(columns=["key_id"]),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Tokens Today": st.column_config.NumberColumn(format="%d"),
                "Calls Today":  st.column_config.NumberColumn(format="%d"),
            },
        )

        st.divider()

        # ── Edit Key ──────────────────────────────────────────────────────────
        st.subheader("Edit a Key")
        edit_key_map = {
            f"{k.get('key_prefix','')}… — {k.get('description','')} [{k.get('key_id','')[:8]}]": k
            for k in keys
        }
        edit_label = st.selectbox("Select key to edit", list(edit_key_map.keys()), key="edit_sel")
        edit_k = edit_key_map[edit_label]

        with st.form("edit_key_form"):
            ec1, ec2 = st.columns(2)
            with ec1:
                new_description = st.text_input(
                    "Description", value=edit_k.get("description", ""))
                new_agent_role = st.text_input(
                    "Agent Role Binding", value=edit_k.get("agent_role", "*"),
                    help="'*' = any role")
                new_system_id = st.text_input(
                    "System ID Binding", value=edit_k.get("system_id", "*"),
                    help="'*' = any system")
            with ec2:
                new_allowed_models_raw = st.text_input(
                    "Allowed Models",
                    value=", ".join(edit_k.get("allowed_models") or []),
                    placeholder="gpt-4o-mini, anthropic/claude-haiku-4-5-20251001 (blank = all)",
                )
                new_daily_token_limit = st.number_input(
                    "Daily Token Limit", min_value=0,
                    value=int(edit_k.get("daily_token_limit") or 0), step=10000,
                    help="0 = unlimited")
            ec3, ec4 = st.columns(2)
            with ec3:
                new_rate_limit_rpm = st.number_input(
                    "Rate Limit (req/min)", min_value=0,
                    value=int(edit_k.get("rate_limit_rpm") or 0), step=10,
                    help="0 = unlimited")
            with ec4:
                new_budget_alert_usd = st.number_input(
                    "Budget Alert (USD/day)", min_value=0.0,
                    value=float(edit_k.get("budget_alert_usd") or 0.0),
                    step=1.0, format="%.2f")
            new_webhook = st.text_input(
                "Alert Webhook URL", value=edit_k.get("alert_webhook_url") or "")

            save_clicked = st.form_submit_button("💾 Save Changes", type="primary")

        if save_clicked:
            new_models = [m.strip() for m in new_allowed_models_raw.split(",") if m.strip()]
            result = api.patch(f"/gateway/keys/{edit_k['key_id']}", {
                "description":       new_description.strip(),
                "agent_role":        new_agent_role.strip() or "*",
                "system_id":         new_system_id.strip()  or "*",
                "allowed_models":    new_models,
                "daily_token_limit": int(new_daily_token_limit),
                "rate_limit_rpm":    int(new_rate_limit_rpm),
                "budget_alert_usd":  float(new_budget_alert_usd),
                "alert_webhook_url": new_webhook.strip(),
            })
            if result:
                st.success("Key updated. Changes take effect within 60 seconds (cache TTL).", icon="✅")
                st.rerun()

        st.divider()

        # ── Revoke Key ────────────────────────────────────────────────────────
        st.subheader("Revoke a Key")
        key_map = {
            f"{k.get('key_prefix','')}… — {k.get('description','')} [{k.get('key_id','')[:8]}]": k
            for k in keys
        }
        sel_label = st.selectbox("Select key to revoke", list(key_map.keys()), key="revoke_sel")
        sel = key_map[sel_label]

        st.warning(
            f"Revoking **{sel.get('key_prefix','')}…** ({sel.get('description','')}) will "
            "immediately block any agent using this key. This cannot be undone.",
            icon="⚠️",
        )
        if st.button("🗑️ Revoke Key", type="secondary", key="revoke_btn"):
            result = api.delete(f"/gateway/keys/{sel['key_id']}")
            if result:
                st.success("Key revoked. Any agent using it will receive 401 within 60 seconds.", icon="✅")
                st.rerun()


# ══ CREATE KEY ════════════════════════════════════════════════════════════════

with tab_create:
    st.subheader("Issue a New API Key")
    st.markdown(
        "The full key is shown **once** after creation — save it immediately. "
        "Agents pass it as `Authorization: Bearer <key>`."
    )

    with st.form("create_key_form"):
        description = st.text_input("Description *", placeholder="e.g. opt-demo translator agent")

        c1, c2 = st.columns(2)
        with c1:
            agent_role = st.text_input(
                "Agent Role Binding",
                value="*",
                help="Lock this key to a specific role. '*' = any role.",
            )
            system_id = st.text_input(
                "System ID Binding",
                value="*",
                help="Lock this key to a specific system. '*' = any system.",
            )
        with c2:
            allowed_models_raw = st.text_input(
                "Allowed Models",
                value="",
                placeholder="gpt-4o-mini, anthropic/claude-haiku-4-5-20251001 (blank = all)",
                help="Comma-separated. Leave blank to allow all models.",
            )
            daily_token_limit = st.number_input(
                "Daily Token Limit",
                min_value=0, value=0, step=10000,
                help="0 = unlimited. Blocks the key when daily usage hits this.",
            )

        c3, c4 = st.columns(2)
        with c3:
            rate_limit_rpm = st.number_input(
                "Rate Limit (req/min)",
                min_value=0, value=0, step=10,
                help="0 = unlimited. Sliding window — returns 429 when exceeded.",
            )
        with c4:
            budget_alert_usd = st.number_input(
                "Budget Alert (USD/day)",
                min_value=0.0, value=0.0, step=1.0, format="%.2f",
                help="0 = no alert. Posts to webhook when daily cost estimate crosses this.",
            )

        alert_webhook_url = st.text_input(
            "Alert Webhook URL",
            value="",
            placeholder="https://hooks.slack.com/services/... (optional)",
            help="Receives a POST with JSON payload when budget or rate thresholds are crossed.",
        )

        is_admin = st.checkbox(
            "Admin key (can create/revoke keys and manage all gateway policies)",
            value=False,
        )

        submitted = st.form_submit_button("Create Key", type="primary")

    if submitted:
        if not description.strip():
            st.error("Description is required.")
        else:
            allowed_models = [m.strip() for m in allowed_models_raw.split(",") if m.strip()]
            result = api.post("/gateway/keys", {
                "description":       description.strip(),
                "agent_role":        agent_role.strip() or "*",
                "system_id":         system_id.strip()  or "*",
                "allowed_models":    allowed_models,
                "daily_token_limit": int(daily_token_limit),
                "is_admin":          is_admin,
                "rate_limit_rpm":    int(rate_limit_rpm),
                "budget_alert_usd":  float(budget_alert_usd),
                "alert_webhook_url": alert_webhook_url.strip(),
            })
            if result:
                raw_key = result.get("key", "")
                st.success("Key created — copy it now, it will not be shown again.", icon="✅")
                st.code(raw_key, language=None)

                st.markdown("**Configure your agent:**")
                st.code(
                    f'GATEWAY_API_KEY="{raw_key}"\n\n'
                    f'# In your LLM client:\n'
                    f'client = OpenAI(\n'
                    f'    base_url="http://localhost:8080/v1",\n'
                    f'    api_key="{raw_key[:12]}…",\n'
                    f')',
                    language="python",
                )


# ══ KEY EVENTS ════════════════════════════════════════════════════════════════

_EVENT_LABELS = {
    "invalid_key":  ("🔴 Invalid Key",      "Authentication failed — key not recognised"),
    "missing_key":  ("🔴 Missing Key",       "No Authorization header sent"),
    "key_revoked":  ("🔴 Revoked",           "Key has been revoked"),
    "role_mismatch":("🟠 Role Mismatch",     "Key not authorised for the requested agent role"),
    "model_blocked":("🟠 Model Blocked",     "Requested model not in key allowlist"),
    "token_limit":  ("🟡 Token Limit",       "Daily token budget exhausted"),
    "rate_limit":   ("🟡 Rate Limited",      "Requests-per-minute cap hit"),
}

with tab_events:
    st.subheader("Key Rejection & Block Events")
    st.caption(
        "Every 401 / 403 / 429 returned by the gateway is recorded here — "
        "these are calls that never reached the LLM."
    )

    # Build key filter from active keys
    ev_data = api.get("/gateway/keys", show_error=False) or {}
    ev_keys = ev_data.get("keys", [])
    key_filter_options = {"(all keys)": ""} | {
        f"{k.get('key_prefix','')}… — {k.get('description','')}": k.get("key_id", "")
        for k in ev_keys
    }
    _EV_HOUR_OPTIONS = {"1 hr": 1, "6 hrs": 6, "12 hrs": 12, "24 hrs": 24, "48 hrs": 48, "72 hrs": 72, "1 week": 168}
    ef1, ef2, ef3, ef4, ef5 = st.columns(5)
    with ef1:
        sel_key_label = st.selectbox("Filter by key", list(key_filter_options.keys()), key="ev_key")
        sel_key_id = key_filter_options[sel_key_label]
    with ef2:
        ev_type_opts = ["(all)"] + list(_EVENT_LABELS.keys())
        sel_ev_type = st.selectbox("Event type", ev_type_opts, key="ev_type")
    with ef3:
        ev_hours_label = st.selectbox("Time window", list(_EV_HOUR_OPTIONS.keys()), index=3, key="ev_hours")
        ev_hours = _EV_HOUR_OPTIONS[ev_hours_label]
    with ef4:
        ev_limit = st.number_input("Max rows", min_value=10, max_value=2000, value=200, step=50, key="ev_limit")

    try:
        raw_events = db.get_key_events(key_id=sel_key_id, limit=int(ev_limit), hours=ev_hours)
    except Exception as exc:
        st.error(f"Could not load events: {exc}")
        raw_events = []

    if sel_ev_type != "(all)":
        raw_events = [e for e in raw_events if e.get("event_type") == sel_ev_type]

    if not raw_events:
        st.info("No rejection events recorded yet.", icon="✅")
    else:
        # Summary counts
        from collections import Counter
        type_counts = Counter(e.get("event_type", "") for e in raw_events)
        sc = st.columns(len(_EVENT_LABELS))
        for i, (etype, (label, _)) in enumerate(_EVENT_LABELS.items()):
            sc[i].metric(label, type_counts.get(etype, 0))

        st.divider()

        rows = []
        for e in raw_events:
            etype = e.get("event_type", "")
            label, desc = _EVENT_LABELS.get(etype, (etype, ""))
            rows.append({
                "Time":            str(e.get("ts", ""))[:19],
                "Event":           label,
                "Description":     desc,
                "Detail":          e.get("detail", ""),
                "Key Prefix":      e.get("key_prefix", "") or "—",
                "Agent Role":      e.get("agent_role", "") or "—",
                "System":          e.get("system_id",  "") or "—",
                "Model Requested": e.get("model_requested", "") or "—",
                "HTTP":            int(e.get("http_status", 0)),
            })

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "HTTP": st.column_config.NumberColumn("HTTP", format="%d"),
            },
        )
