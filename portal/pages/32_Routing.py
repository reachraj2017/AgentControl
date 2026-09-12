"""Routing Policies — model/backend routing overrides from the change store."""

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

st.title("🔀 Routing Policies")
st.caption(
    "Override which model and backend handles requests from specific agent roles. "
    "Policies are evaluated newest-first; first match wins. "
    "Wildcards (`*`) match any value. "
    "Set a **Fallback Model** to retry automatically if the primary fails."
)

# ── Current policies ──────────────────────────────────────────────────────────

data     = api.get("/gateway/routing")
policies = (data or {}).get("policies", [])

if policies:
    display_cols = [
        "agent_role", "system_id", "model_match",
        "target_model", "target_backend",
        "fallback_model", "fallback_backend",
        "reason", "policy_id",
    ]
    df = pd.DataFrame(policies)
    st.dataframe(
        df[[c for c in display_cols if c in df.columns]],
        use_container_width=True,
        hide_index=True,
    )

    pol_map = {
        f"{p['agent_role']} / {p['system_id']} → {p['target_model']} [{p['policy_id'][:8]}]": p
        for p in policies
    }

    st.divider()
    st.subheader("Manage Policies")

    sel_label = st.selectbox("Select policy to manage", list(pol_map.keys()), key="routing_sel")
    sel = pol_map[sel_label]

    tab_edit, tab_delete = st.tabs(["✏️ Edit", "🗑️ Delete"])

    with tab_edit:
        with st.form("edit_routing"):
            st.markdown(f"**Editing:** `{sel['policy_id'][:8]}` — role `{sel['agent_role']}` / system `{sel['system_id']}`")
            st.caption("Agent role and system ID are fixed. Edit routing target and match criteria below.")

            c1, c2 = st.columns(2)
            with c1:
                model_match = st.text_input(
                    "Model Match (optional)", value=sel.get("model_match", ""),
                    help="Only fire when agent requests exactly this model. Blank = match any.",
                )
                target_model = st.text_input(
                    "Target Model *", value=sel.get("target_model", ""),
                    help="e.g. anthropic/claude-haiku-4-5-20251001 or gpt-4o-mini",
                )
                backend_opts  = ["openai", "anthropic", "ollama", "gemini", "bedrock"]
                cur_backend   = sel.get("target_backend", "openai")
                target_backend = st.selectbox(
                    "Target Backend", backend_opts,
                    index=backend_opts.index(cur_backend) if cur_backend in backend_opts else 0,
                )
            with c2:
                fallback_model = st.text_input(
                    "Fallback Model (optional)", value=sel.get("fallback_model", ""),
                    help="If the primary target fails, retry with this model automatically.",
                )
                cur_fb_backend  = sel.get("fallback_backend", "openai")
                fallback_backend = st.selectbox(
                    "Fallback Backend", backend_opts,
                    index=backend_opts.index(cur_fb_backend) if cur_fb_backend in backend_opts else 0,
                    key="fb_backend_edit",
                )
                reason = st.text_input("Reason", value=sel.get("reason", ""))

            save = st.form_submit_button("Save Changes", type="primary")

        if save:
            if not target_model.strip():
                st.error("Target model is required.")
            else:
                result = api.put(f"/gateway/routing/{sel['policy_id']}", {
                    "model_match":      model_match,
                    "target_model":     target_model.strip(),
                    "target_backend":   target_backend,
                    "reason":           reason,
                    "fallback_model":   fallback_model.strip(),
                    "fallback_backend": fallback_backend,
                })
                if result:
                    st.success("Policy updated. Takes effect within 30 s.")
                    st.rerun()

    with tab_delete:
        st.warning(
            f"This will disable the policy for **{sel['agent_role']}** → "
            f"**{sel['target_model']}**. The record is kept in history but stops matching requests.",
            icon="⚠️",
        )
        if st.button("Delete Policy", type="secondary", key="delete_routing_btn"):
            if api.delete(f"/gateway/routing/{sel['policy_id']}"):
                st.success("Policy deleted. Takes effect within 30 s.")
                st.rerun()

else:
    st.info(
        "No routing policies. All requests pass through with the model the agent specified. "
        "Add a policy below to override."
    )

st.divider()

# ── Create new policy ─────────────────────────────────────────────────────────

with st.expander("➕ Add Routing Policy", expanded=not bool(policies)):
    st.markdown(
        "A routing policy rewrites `model` and `backend` before forwarding. "
        "The agent-requested model is preserved in `model_requested` in the call log. "
        "Set a **Fallback Model** to retry automatically if the primary fails."
    )
    with st.form("create_routing"):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Match criteria**")
            agent_role = st.selectbox(
                "Agent Role",
                _known_roles(),
                help="'*' matches any agent role.",
            )
            system_id = st.text_input(
                "System ID", "*",
                help="'*' matches any system. Use your X-Gateway-System-Id value to target a specific system.",
            )
            model_match = st.text_input(
                "Model Match (optional)", "",
                help="Only activate when the agent requests exactly this model. Leave blank to match any.",
            )

        with c2:
            st.markdown("**Primary target**")
            backend_opts   = ["openai", "anthropic", "ollama", "gemini", "bedrock"]
            target_model   = st.text_input(
                "Target Model *", "gpt-4o-mini",
                help="e.g. gpt-4o, anthropic/claude-haiku-4-5-20251001, ollama/llama3.2",
            )
            target_backend = st.selectbox("Target Backend", backend_opts)
            reason         = st.text_input("Reason", "", help="Human-readable note.")

        st.markdown("**Fallback (optional)** — used automatically if the primary fails")
        cf1, cf2 = st.columns(2)
        with cf1:
            fallback_model   = st.text_input(
                "Fallback Model", "",
                help="Leave blank for no fallback. e.g. anthropic/claude-haiku-4-5-20251001",
            )
        with cf2:
            fallback_backend = st.selectbox(
                "Fallback Backend", backend_opts,
                key="fb_backend_create",
            )

        submitted = st.form_submit_button("Create Policy", type="primary")

    if submitted:
        if not target_model.strip():
            st.error("Target model is required.")
        else:
            result = api.post("/gateway/routing", {
                "agent_role":       agent_role,
                "system_id":        system_id,
                "model_match":      model_match,
                "target_model":     target_model.strip(),
                "target_backend":   target_backend,
                "reason":           reason,
                "fallback_model":   fallback_model.strip(),
                "fallback_backend": fallback_backend,
            })
            if result:
                st.success("Policy created. Gateway picks it up within 30 s.")
                st.rerun()

# ── Help ──────────────────────────────────────────────────────────────────────

with st.expander("How routing works — provider prefix reference"):
    st.markdown("""
**Evaluation order**: newest policy wins (first match applied).

**Wildcards**: `*` in `agent_role` or `system_id` matches any value.

**Fallback**: if the primary target returns an error or times out, the gateway automatically retries
the fallback model. The call log shows `fallback_used=1` and `routing_reason=fallback_from_*`.

---

**Provider prefix — set this in Target Model or Fallback Model:**

| Provider | Target Model example | Requires |
|---|---|---|
| OpenAI | `gpt-4o-mini` or `openai/gpt-4o-mini` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| Anthropic | `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| Ollama (local) | `ollama/llama3.2` | Ollama running on host |
| Google Gemini | `gemini/gemini-1.5-pro` | `GEMINI_API_KEY` |
| AWS Bedrock | `bedrock/anthropic.claude-v2` | AWS credentials |

The agent always calls `POST /v1/chat/completions` — routing silently swaps the model.
""")
