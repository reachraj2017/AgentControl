"""Proposed Changes — approve, reject, or propose changes to gateway behaviour."""

import json
import pandas as pd
import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import api

import os
if os.getenv("M3_ENABLED", "true").lower() != "true":
    st.warning("⚠️ Module 3 (Agent Gateway) is not enabled in this deployment.")
    st.stop()


st.sidebar.title("⚡ Agent Gateway")
api.sidebar_status()

st.title("🔄 Proposed Changes")
st.caption(
    "Review, approve, or reject changes proposed by the reflection agent or operators. "
    "Approved changes take effect immediately via the change store — no restart needed."
)

# ── Pending changes ───────────────────────────────────────────────────────────

pending_data = api.get("/gateway/changes?status=pending", show_error=False)
pending      = (pending_data or {}).get("changes", [])

if pending:
    st.subheader(f"⏳ Pending Approval ({len(pending)})")
    for ch in pending:
        with st.container(border=True):
            hc1, hc2, hc3 = st.columns([3, 1, 1])
            with hc1:
                st.markdown(
                    f"**{ch.get('change_type', '?')}** "
                    f"— role `{ch.get('agent_role','*')}` / system `{ch.get('system_id','*')}`"
                )
                desc = ch.get("description", "")
                if desc:
                    st.caption(desc)

                evidence = ch.get("evidence", "")
                payload  = ch.get("payload",  "{}")

                col_ev, col_pay = st.columns(2)
                with col_ev:
                    if evidence:
                        with st.expander("📊 Evidence"):
                            st.text(evidence)
                with col_pay:
                    if payload and payload not in ("{}", ""):
                        with st.expander("📦 Payload"):
                            try:
                                st.json(json.loads(payload))
                            except Exception:
                                st.text(payload)

                proposed_by = ch.get("proposed_by", "?")
                proposed_at = ch.get("proposed_at", "")
                st.caption(f"Proposed by **{proposed_by}** at {proposed_at}")

            with hc2:
                if st.button(
                    "✅ Approve",
                    key=f"approve_{ch['change_id']}",
                    type="primary",
                    use_container_width=True,
                ):
                    result = api.put(
                        f"/gateway/changes/{ch['change_id']}",
                        {"status": "approved", "approved_by": "operator"},
                    )
                    if result:
                        applied = result.get("applied") or {}
                        apply_type = applied.get("type", "")
                        if apply_type == "routing_policy":
                            st.success(
                                f"Approved — routing policy created for "
                                f"`{applied.get('target_model','?')}`. "
                                f"{applied.get('replaced', 0)} old policy/policies disabled."
                            )
                        elif apply_type == "shadow_rule":
                            st.success(
                                f"Approved — shadow rule created for `{applied.get('shadow_model','?')}`."
                            )
                        elif apply_type == "prompt_mod":
                            st.success(
                                f"Approved — prompt mod created (`{applied.get('mod_type','?')}`)."
                            )
                        elif apply_type == "no_action":
                            st.warning(
                                f"Approved in queue, but **no policy was created** — "
                                f"change_type `{applied.get('change_type','?')}` is not "
                                f"auto-applied, or the payload was missing required fields "
                                f"(e.g. `target_model` for routing_override)."
                            )
                        elif apply_type == "error":
                            st.error(f"Approved but auto-apply failed: {applied.get('detail','?')}")
                        else:
                            st.success("Approved and applied to change store.")
                        st.rerun()

            with hc3:
                if st.button(
                    "❌ Reject",
                    key=f"reject_{ch['change_id']}",
                    type="secondary",
                    use_container_width=True,
                ):
                    result = api.put(
                        f"/gateway/changes/{ch['change_id']}",
                        {"status": "rejected", "approved_by": "operator"},
                    )
                    if result:
                        st.rerun()

    st.divider()
else:
    st.info("No changes pending approval.")
    st.divider()

# ── History tabs ──────────────────────────────────────────────────────────────

tab_approved, tab_rejected, tab_all = st.tabs(["✅ Approved", "❌ Rejected", "All"])


def _render_history(status: str):
    data    = api.get(f"/gateway/changes?status={status}", show_error=False)
    changes = (data or {}).get("changes", [])
    if not changes:
        st.info(f"No {status} changes.")
        return
    for ch in changes:
        icon = "✅" if status == "approved" else "❌"
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown(
                    f"{icon} **{ch.get('change_type','?')}** "
                    f"— `{ch.get('agent_role','*')}` / `{ch.get('system_id','*')}`"
                )
                if ch.get("description"):
                    st.caption(ch["description"])
                by = ch.get("approved_by") or ch.get("proposed_by", "?")
                at = ch.get("decided_at", "")
                if at and at != "1970-01-01 00:00:00":
                    st.caption(f"Decided by **{by}** at {at}")
            with c2:
                if ch.get("payload") and ch["payload"] not in ("{}", ""):
                    with st.expander("Payload"):
                        try:
                            st.json(json.loads(ch["payload"]))
                        except Exception:
                            st.text(ch["payload"])


with tab_approved:
    _render_history("approved")

with tab_rejected:
    _render_history("rejected")

with tab_all:
    all_data = api.get("/gateway/changes?status=", show_error=False)
    all_ch   = (all_data or {}).get("changes", [])
    if all_ch:
        df = pd.DataFrame(all_ch)
        show = ["proposed_at","change_type","agent_role","system_id","description","status","proposed_by","decided_at"]
        st.dataframe(df[[c for c in show if c in df.columns]], use_container_width=True, hide_index=True)
    else:
        st.info("No changes in history.")

st.divider()

# ── Propose a new change ──────────────────────────────────────────────────────

with st.expander("➕ Propose a New Change"):
    st.caption(
        "Submit a change for operator review. Once approved it is applied to the gateway "
        "immediately — no restart or redeploy needed."
    )
    with st.form("propose_change_form"):
        pc1, pc2 = st.columns(2)
        with pc1:
            change_type = st.selectbox(
                "Change Type",
                [
                    "routing_override",
                    "prompt_modification",
                    "shadow_rule",
                    "model_downgrade",
                    "model_upgrade",
                    "prompt_prefix_add",
                    "prompt_prefix_remove",
                    "few_shot_add",
                    "other",
                ],
                help="Descriptive label for the type of change being proposed.",
            )
            agent_role = st.selectbox(
                "Agent Role",
                ["*", "orchestrator", "searcher", "summarizer", "translator"],
            )
            system_id   = st.text_input("System ID", "*")
            proposed_by = st.text_input("Proposed By", "operator",
                                        help="Identifies the proposer (operator name, agent name, etc.)")

        with pc2:
            description = st.text_area(
                "Description",
                placeholder="What change is being proposed and why?",
                height=100,
            )
            evidence = st.text_area(
                "Evidence",
                placeholder="Eval score delta, sample count, comparison results…",
                height=100,
            )
            _PAYLOAD_TEMPLATES = {
                "routing_override":   '{"target_model": "anthropic/claude-haiku-4-5-20251001", "target_backend": "openai", "model_match": "", "reason": ""}',
                "model_downgrade":    '{"target_model": "gpt-4o-mini", "target_backend": "openai", "reason": ""}',
                "model_upgrade":      '{"target_model": "gpt-4o", "target_backend": "openai", "reason": ""}',
                "shadow_rule":        '{"shadow_model": "gpt-4o", "shadow_backend": "openai", "sample_rate": 0.1}',
                "prompt_modification":'{"mod_type": "system_prefix", "content": "Always cite sources.", "evidence_delta": 0.0}',
                "prompt_prefix_add":  '{"mod_type": "system_prefix", "content": "", "evidence_delta": 0.0}',
                "few_shot_add":       '{"mod_type": "few_shot", "content": "[{\\"role\\":\\"user\\",\\"content\\":\\"Example input\\"}, {\\"role\\":\\"assistant\\",\\"content\\":\\"Example output\\"}]"}',
                "other":              "{}",
            }
            payload = st.text_area(
                "Payload (JSON)",
                value=_PAYLOAD_TEMPLATES.get(change_type, "{}"),
                height=100,
                help="Required fields depend on change_type — see the 'How the change lifecycle works' section below.",
            )

        submitted = st.form_submit_button("Submit Proposal", type="primary")

    if submitted:
        errs = []
        if not description.strip():
            errs.append("Description is required.")
        try:
            json.loads(payload)
        except json.JSONDecodeError as e:
            errs.append(f"Payload must be valid JSON: {e}")

        if errs:
            for e in errs:
                st.error(e)
        else:
            result = api.post("/gateway/changes", {
                "change_type":  change_type,
                "agent_role":   agent_role,
                "system_id":    system_id,
                "description":  description.strip(),
                "evidence":     evidence.strip(),
                "payload":      payload,
                "proposed_by":  proposed_by.strip() or "operator",
            })
            if result:
                cid = result.get("change_id", "")[:8]
                st.success(f"Proposal submitted (ID: {cid}…). It appears in Pending above.")
                st.rerun()

# ── Help ──────────────────────────────────────────────────────────────────────

with st.expander("How the change lifecycle works"):
    st.markdown("""
**Proposal → Review → Apply (no restart)**

```
Reflection Agent / Operator
        │
        │ POST /gateway/changes
        ▼
  gateway_proposed_changes (ClickHouse)
        │
        │ Operator reviews in this UI
        ▼
  PUT /gateway/changes/{id}  {status: approved}
        │
        │ Gateway change store refreshes (30 s TTL)
        ▼
  New behaviour applied to all subsequent requests
```

**Change types and required payload fields:**

| Change Type | Required payload keys | Example |
|---|---|---|
| `routing_override` | `target_model`, optionally `target_backend`, `model_match`, `reason` | `{"target_model": "anthropic/claude-haiku-4-5-20251001", "target_backend": "openai"}` |
| `model_downgrade` | same as routing_override | `{"target_model": "gpt-4o-mini"}` |
| `model_upgrade` | same as routing_override | `{"target_model": "gpt-4o"}` |
| `shadow_rule` | `shadow_model`, optionally `shadow_backend`, `sample_rate` | `{"shadow_model": "gpt-4o", "sample_rate": 0.1}` |
| `prompt_modification` | `mod_type`, `content`, optionally `evidence_delta` | `{"mod_type": "system_prefix", "content": "Always cite sources."}` |
| `prompt_prefix_add` | same as prompt_modification | `{"mod_type": "system_prefix", "content": "Be concise."}` |
| `few_shot_add` | `mod_type`, `content` (JSON array) | `{"mod_type": "few_shot", "content": "[{\"role\":\"user\",\"content\":\"...\"}]"}` |
| `other` | not auto-applied — for record-keeping only | `{}` |

> **Important:** If the payload is missing required fields, the change is marked as approved in the queue but **no policy is created**. You will see a warning after approval. Fix the payload by rejecting and re-proposing.

**The reflection loop:**
The `evalgov-agent` can be extended to periodically analyse call scores in ClickHouse,
identify underperforming agent roles, and automatically submit proposals here.
An operator (or a governance policy) reviews and approves, closing the self-improvement loop.
""")
