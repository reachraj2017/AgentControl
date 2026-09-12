"""Prompt Modifications — inject approved text into LLM prompts at the gateway."""

import json
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

st.title("✏️ Prompt Modifications")
st.caption(
    "Inject approved text into prompts at the gateway layer — no agent code changes required. "
    "Changes take effect within 30 s for all subsequent calls matching the criteria."
)

MOD_HELP = {
    "system_prefix": (
        "Prepends text to the system message (or creates one if none exists). "
        "Use this to add universal instructions, personas, or constraints."
    ),
    "system_suffix": (
        "Appends text to the existing system message. "
        "Use this to add reminders or guardrails without replacing the original system prompt."
    ),
    "few_shot": (
        "Inserts example turns before the last user message. "
        "Content must be a JSON array of `{role, content}` objects."
    ),
}

# ── Current mods ──────────────────────────────────────────────────────────────

data = api.get("/gateway/mods")
mods = (data or {}).get("mods", [])

if mods:
    df = pd.DataFrame(mods)
    st.dataframe(
        df[[c for c in ["agent_role", "system_id", "mod_type", "content",
                        "evidence_delta", "mod_id"] if c in df.columns]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "content":        st.column_config.TextColumn("Content", width="large"),
            "evidence_delta": st.column_config.NumberColumn("Eval Δ", format="%.3f"),
        },
    )

    mod_map = {
        f"{m['agent_role']} [{m['mod_type']}] [{m['mod_id'][:8]}]": m
        for m in mods
    }

    st.divider()
    st.subheader("Manage Modifications")

    sel_label = st.selectbox("Select modification to manage", list(mod_map.keys()), key="mod_sel")
    sel = mod_map[sel_label]

    tab_edit, tab_delete = st.tabs(["✏️ Edit", "🗑️ Delete"])

    with tab_edit:
        st.markdown(
            f"**Editing:** `{sel['mod_id'][:8]}` — role `{sel['agent_role']}` / "
            f"system `{sel['system_id']}`"
        )
        st.caption("Agent role and system ID are fixed. Edit content and type below.")

        mod_type_opts = ["system_prefix", "system_suffix", "few_shot"]
        cur_mod_type  = sel.get("mod_type", "system_prefix")
        edit_mod_type = st.selectbox(
            "Modification Type", mod_type_opts,
            index=mod_type_opts.index(cur_mod_type) if cur_mod_type in mod_type_opts else 0,
            key="edit_mod_type",
        )
        st.caption(MOD_HELP[edit_mod_type])

        edit_evidence = st.number_input(
            "Evidence Delta (eval score Δ)",
            min_value=-1.0, max_value=1.0,
            value=float(sel.get("evidence_delta", 0.0)),
            step=0.005, format="%.3f",
            key="edit_evidence",
        )

        edit_content = st.text_area(
            "Content",
            value=sel.get("content", ""),
            height=250,
            key="edit_content",
        )

        if edit_mod_type == "few_shot":
            try:
                parsed = json.loads(edit_content)
                if isinstance(parsed, list):
                    st.success(f"Valid JSON — {len(parsed)} message(s)")
                else:
                    st.warning("Must be a JSON array")
            except json.JSONDecodeError as e:
                st.error(f"JSON parse error: {e}")

        if st.button("Save Changes", type="primary", key="save_mod_btn"):
            errors = []
            if not edit_content.strip():
                errors.append("Content is required.")
            if edit_mod_type == "few_shot":
                try:
                    parsed = json.loads(edit_content)
                    if not isinstance(parsed, list):
                        errors.append("few_shot content must be a JSON array.")
                except json.JSONDecodeError as e:
                    errors.append(f"Invalid JSON: {e}")
            if errors:
                for e in errors:
                    st.error(e)
            else:
                result = api.put(f"/gateway/mods/{sel['mod_id']}", {
                    "mod_type":       edit_mod_type,
                    "content":        edit_content,
                    "evidence_delta": edit_evidence,
                })
                if result:
                    st.success("Modification updated. Takes effect within 30 s.")
                    st.rerun()

    with tab_delete:
        st.warning(
            f"This will disable the **{sel['mod_type']}** modification for "
            f"**{sel['agent_role']}**. Content will no longer be injected into prompts.",
            icon="⚠️",
        )
        if st.button("Delete Modification", type="secondary", key="delete_mod_btn"):
            if api.delete(f"/gateway/mods/{sel['mod_id']}"):
                st.success("Modification deleted. Takes effect within 30 s.")
                st.rerun()

else:
    st.info(
        "No prompt modifications active. "
        "Add one below to start injecting text into agent prompts."
    )

st.divider()

# ── Create new mod ────────────────────────────────────────────────────────────

with st.expander("➕ Add Prompt Modification", expanded=not bool(mods)):
    c1, c2 = st.columns([1, 2])
    with c1:
        agent_role = st.selectbox(
            "Agent Role",
            _known_roles(),
            key="mod_role",
        )
        system_id = st.text_input("System ID", "*", key="mod_sys",
                                  help="'*' matches any system")
        mod_type = st.selectbox(
            "Modification Type",
            ["system_prefix", "system_suffix", "few_shot"],
            key="mod_type_create",
        )
        evidence_delta = st.number_input(
            "Evidence Delta (eval score Δ)",
            min_value=-1.0, max_value=1.0, value=0.0, step=0.005,
            format="%.3f",
            help="Record the eval score improvement this modification produced.",
            key="mod_evidence_create",
        )
        st.caption(MOD_HELP[mod_type])

    with c2:
        if mod_type == "few_shot":
            default_few_shot = json.dumps([
                {"role": "user",      "content": "Example user input"},
                {"role": "assistant", "content": "Example assistant output"},
            ], indent=2)
            content = st.text_area(
                "Content (JSON array of message dicts)",
                value=default_few_shot,
                height=300,
                key="mod_content_create",
            )
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    st.success(f"Valid JSON — {len(parsed)} message(s)")
                else:
                    st.warning("Must be a JSON array")
            except json.JSONDecodeError as e:
                st.error(f"JSON parse error: {e}")
        else:
            placeholder = {
                "system_prefix": "Always cite your sources. Be concise and factual.\n\n",
                "system_suffix": "\n\nKeep responses under 200 words unless explicitly asked.",
            }[mod_type]
            content = st.text_area(
                "Content (text to inject)",
                value="",
                height=300,
                placeholder=placeholder,
                key="mod_content_create",
            )

    create_btn = st.button("Create Modification", type="primary", key="create_mod_btn")

if create_btn:
    errors = []
    if not content.strip():
        errors.append("Content is required.")
    if mod_type == "few_shot":
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, list):
                errors.append("few_shot content must be a JSON array.")
        except json.JSONDecodeError as e:
            errors.append(f"Invalid JSON: {e}")

    if errors:
        for e in errors:
            st.error(e)
    else:
        result = api.post("/gateway/mods", {
            "agent_role":     agent_role,
            "system_id":      system_id,
            "mod_type":       mod_type,
            "content":        content,
            "evidence_delta": evidence_delta,
        })
        if result:
            st.success("Modification created. Takes effect within 30 s.")
            st.rerun()

# ── Help ──────────────────────────────────────────────────────────────────────

with st.expander("How prompt modifications work"):
    st.markdown("""
**Injection order**: all mods matching `(agent_role, system_id)` are applied in insertion order.

**system_prefix** — inserted before existing system message content:
```
[your prefix]

[original system message]
```

**system_suffix** — appended after existing system message content:
```
[original system message]

[your suffix]
```

**few_shot** — example turns inserted before the last user message:
```
[system message]
[few-shot user turn]
[few-shot assistant turn]
[actual user message]   ← last user message stays last
```

**Evidence delta**: when you measure the eval score improvement from a modification,
record it here. The reflection agent uses it to rank modifications and decide
which ones to propose keeping vs removing.
""")
