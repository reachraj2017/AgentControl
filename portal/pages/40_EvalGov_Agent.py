"""EvalGov Intelligence Agent — Chat UI with live system-state panel."""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import requests
import streamlit as st

if os.getenv("M4_ENABLED", "true").lower() != "true":
    st.warning("⚠️ Module 4 (EvalGov Intelligence) is not enabled in this deployment.")
    st.stop()

AGENT_URL = os.getenv("EVALGOV_AGENT_URL", "http://localhost:8003")


# ── Session state init ────────────────────────────────────────────────────────

if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "monitor_run_result" not in st.session_state:
    st.session_state.monitor_run_result = None

# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent_post(path: str, body: dict, timeout: int = 90) -> dict | None:
    try:
        r = requests.post(f"{AGENT_URL}{path}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return None
    except Exception as exc:
        st.error(f"Agent error: {exc}")
        return None


def _agent_get(path: str, timeout: int = 10) -> dict | None:
    try:
        r = requests.get(f"{AGENT_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return None
    except Exception:
        return None



# ── Layout ────────────────────────────────────────────────────────────────────

st.title("🤖 EvalGov Intelligence Agent")

# Check agent availability
agent_health = _agent_get("/health", timeout=3)
agent_ok = agent_health is not None

if not agent_ok:
    st.error(
        "⚠️ EvalGov Agent service is not reachable at `" + AGENT_URL + "`. "
        "Make sure the `evalgov-agent` container is running."
    )
    st.stop()

col_chat, col_findings = st.columns([3, 2], gap="large")

# ══════════════════════════════════════════════════════════════════════════════
# LEFT — Chat
# ══════════════════════════════════════════════════════════════════════════════

with col_chat:
    # Top controls
    ctrl1, ctrl2, ctrl3 = st.columns([4, 1, 1])
    ctrl1.markdown("**Chat with your AI governance system in plain English.**  \n"
                   "Ask about status, issues, costs, incidents, traces, or take actions like approving HITL requests.")
    if ctrl2.button("🗑️ Clear Chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()
    if ctrl3.button("↺ Refresh", use_container_width=True):
        st.rerun()

    st.markdown("---")

    # Message history display
    chat_container = st.container(height=520)
    with chat_container:
        if not st.session_state.messages:
            st.markdown(
                "<div style='color:#888;text-align:center;padding:40px 20px;'>"
                "Ask anything:<br><br>"
                "• <em>What's wrong right now?</em><br>"
                "• <em>Show me all pending HITL requests</em><br>"
                "• <em>What did it cost to run yesterday?</em><br>"
                "• <em>Why is agent X circuit breaker open?</em><br>"
                "• <em>Approve HITL request &lt;id&gt;</em><br>"
                "• <em>What's the trust score for analyzer?</em>"
                "</div>",
                unsafe_allow_html=True,
            )
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("tool_calls"):
                    with st.expander(f"🔧 {len(msg['tool_calls'])} tool(s) called", expanded=False):
                        for tc in msg["tool_calls"]:
                            inp = tc.get("inputs", {})
                            inp_str = ", ".join(f"{k}={v}" for k, v in inp.items()) if inp else "no args"
                            st.code(f"→ {tc['name']}({inp_str})")
                            result_preview = tc.get("result", "")[:300]
                            if result_preview:
                                st.caption(result_preview)

    # Chat input
    if prompt := st.chat_input("Ask anything about your AI systems..."):
        # Show user message immediately — before the API call
        with chat_container:
            with st.chat_message("user"):
                st.markdown(prompt)

        st.session_state.messages.append({"role": "user", "content": prompt})

        # Build history for stateless API (exclude last user msg we just added)
        history_for_api = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state.messages[:-1]
        ]

        # Show assistant bubble with spinner while waiting
        with chat_container:
            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    data = _agent_post(
                        "/chat",
                        {"message": prompt, "history": history_for_api},
                        timeout=300,
                    )

        if data:
            answer = data.get("response", "")
            tool_calls = data.get("tool_calls", [])
            st.session_state.messages.append({
                "role": "assistant",
                "content": answer,
                "tool_calls": tool_calls,
            })
        else:
            st.session_state.messages.append({
                "role": "assistant",
                "content": "⚠️ Agent service unavailable. Please try again.",
                "tool_calls": [],
            })
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# RIGHT — Live System State Panel (auto-refreshes every 60s)
# ══════════════════════════════════════════════════════════════════════════════

with col_findings:
    # ── Proactive Findings (governance + gateway + quality) ───────────────────
    st.markdown("### 🔍 System Findings")

    _GATEWAY_TYPES = {
        "gateway_routing_error_rate", "gateway_unknown_role",
        "gateway_shadow_winning", "gateway_ab_test_stale",
    }
    _QUALITY_TYPES = {"quality_regression", "cost_spike"}
    _SEV_COLOR = {
        "critical": "#ff4b4b",
        "high":     "#ff8800",
        "warning":  "#ffc107",
        "medium":   "#4b9eff",
        "info":     "#888888",
    }
    _SEV_BADGE = {
        "critical": "🔴",
        "high":     "🟠",
        "warning":  "🟡",
        "medium":   "🔵",
        "info":     "⚪",
    }

    @st.fragment(run_every=60)
    def _render_findings():
        data     = _agent_get("/findings/active?hours=24")
        findings = (data or {}).get("findings", [])

        # Controls
        ctrl_run, ctrl_ack, ctrl_ts = st.columns([2, 2, 3])
        if ctrl_run.button("▶ Run Checks", key="findings_run", use_container_width=True):
            with st.spinner("Running all checks — may take 30–60s if new findings trigger RCA…"):
                try:
                    r = requests.post(f"{AGENT_URL}/monitor/run", timeout=180)
                    result = r.json() if r.ok else {}
                    sources = result.get("sources", {})
                    total_new = sum(v.get("new_findings", 0) for v in sources.values() if isinstance(v, dict))
                    parts = []
                    for src, v in sources.items():
                        if not isinstance(v, dict):
                            continue
                        detected = v.get("detected", 0)
                        new      = v.get("new_findings", 0)
                        already  = detected - new
                        desc = f"{src}: {detected} detected"
                        if new:
                            desc += f", {new} new"
                        if already:
                            desc += f", {already} already acknowledged"
                        parts.append(desc)
                    st.session_state.monitor_run_result = ("ok", f"✅ Done — {total_new} new finding(s). " + ", ".join(parts))
                except Exception as exc:
                    st.session_state.monitor_run_result = ("error", f"Run failed: {exc}")
            st.rerun()

        if ctrl_ack.button("✓ Dismiss All", key="findings_ack_all", use_container_width=True):
            ids = [f.get("finding_id", "") for f in findings if f.get("finding_id")]
            if ids:
                try:
                    requests.post(
                        f"{AGENT_URL}/findings/acknowledge-bulk",
                        json={"ids": ids, "acknowledged_by": "operator"},
                        timeout=10,
                    )
                except Exception:
                    pass
            st.rerun()

        # Show result of last manual run (persists until next auto-refresh clears it)
        if st.session_state.monitor_run_result:
            kind, msg = st.session_state.monitor_run_result
            if kind == "ok":
                st.success(msg)
            else:
                st.error(msg)
            st.session_state.monitor_run_result = None

        active_count = sum(1 for f in findings if f.get("status") == "active")
        if not findings:
            st.success("✅ No findings — system is quiet.")
            ctrl_ts.caption(f"↺ {datetime.now().strftime('%H:%M:%S')}")
            return

        ack_count = len(findings) - active_count
        summary_parts = []
        if active_count:
            summary_parts.append(f"{active_count} active")
        if ack_count:
            summary_parts.append(f"{ack_count} seen")
        ctrl_ts.caption(f"↺ {datetime.now().strftime('%H:%M:%S')} · {', '.join(summary_parts)}")

        # Bucket by source
        gov_findings     = [f for f in findings if f.get("finding_type") not in _GATEWAY_TYPES and f.get("finding_type") not in _QUALITY_TYPES]
        gateway_findings = [f for f in findings if f.get("finding_type") in _GATEWAY_TYPES]
        quality_findings = [f for f in findings if f.get("finding_type") in _QUALITY_TYPES]

        def _finding_card(f: dict):
            fid    = f.get("finding_id", "")
            sev    = f.get("severity", "medium")
            title  = f.get("title", "")
            agent  = f.get("affected_agent", "")
            ts     = str(f.get("created_at", ""))[:16]
            status = f.get("status", "active")
            is_ack = status == "acknowledged"
            color  = _SEV_COLOR.get(sev, "#888") if not is_ack else "#444"
            badge  = _SEV_BADGE.get(sev, "⚪") if not is_ack else "⚫"
            agent_suffix = f" · {agent}" if agent and agent != "system" else ""
            seen_label   = " &nbsp;<span style='color:#666;font-size:0.8em'>(seen)</span>" if is_ack else ""
            opacity      = "opacity:0.5;" if is_ack else ""
            st.markdown(
                f"<div style='border-left:3px solid {color};padding:4px 10px;margin-bottom:2px;{opacity}'>"
                f"{badge} <strong>{title}</strong>{seen_label}<br>"
                f"<small style='color:#aaa'>{ts}{agent_suffix}</small></div>",
                unsafe_allow_html=True,
            )
            summary    = f.get("summary", "")
            rca        = f.get("rca", "")
            rec        = f.get("recommendation", "")
            signal_raw = f.get("signal_data") or "{}"
            if any([summary, rca, rec]):
                with st.expander("↳ RCA + Recommendation", expanded=False):
                    if summary:
                        st.markdown(f"**What happened:** {summary}")
                    if rca:
                        st.markdown(f"**Root cause:** {rca}")
                    if rec:
                        st.markdown(f"**Recommendation:** {rec}")
                    try:
                        sig = json.loads(signal_raw) if signal_raw else {}
                        if sig:
                            st.json(sig, expanded=False)
                    except Exception:
                        pass
                    if fid:
                        if is_ack:
                            if st.button("↩ Reopen", key=f"reopen_{fid}"):
                                try:
                                    requests.post(
                                        f"{AGENT_URL}/findings/{fid}/resolve",
                                        timeout=5,
                                    )
                                except Exception:
                                    pass
                                st.rerun()
                        else:
                            if st.button("✓ Dismiss", key=f"dismiss_{fid}"):
                                try:
                                    requests.post(
                                        f"{AGENT_URL}/findings/{fid}/acknowledge",
                                        json={"acknowledged_by": "operator"},
                                        timeout=5,
                                    )
                                except Exception:
                                    pass
                                st.rerun()

        with st.container(height=520):
            # 🏛️ Governance
            st.markdown(f"**🏛️ Governance** &nbsp;<span style='color:#888;font-size:0.85em'>({len(gov_findings)})</span>", unsafe_allow_html=True)
            if not gov_findings:
                st.caption("No governance findings.")
            else:
                for f in gov_findings:
                    _finding_card(f)

            st.markdown("<hr style='margin:6px 0;border-color:#333'>", unsafe_allow_html=True)

            # 🌐 Gateway
            st.markdown(f"**🌐 Gateway** &nbsp;<span style='color:#888;font-size:0.85em'>({len(gateway_findings)})</span>", unsafe_allow_html=True)
            if not gateway_findings:
                st.caption("No gateway findings.")
            else:
                for f in gateway_findings:
                    _finding_card(f)

            st.markdown("<hr style='margin:6px 0;border-color:#333'>", unsafe_allow_html=True)

            # 📊 Quality & Cost
            st.markdown(f"**📊 Quality & Cost** &nbsp;<span style='color:#888;font-size:0.85em'>({len(quality_findings)})</span>", unsafe_allow_html=True)
            if not quality_findings:
                st.caption("No quality or cost findings.")
            else:
                for f in quality_findings:
                    _finding_card(f)

    _render_findings()

    st.markdown("---")

    st.markdown("### 📡 Live System State")
    st.caption("Auto-refreshes every 60s · 24h window · scroll to see all · max 25 per section")

    @st.fragment(run_every=60)
    def _render_system_state():
        data = _agent_get("/system-state?hours=24")
        if data is None:
            st.error("Could not reach agent service.")
            return

        hitl      = data.get("hitl_queue", [])
        policy    = data.get("policy_violations", [])
        incidents = data.get("incidents", [])
        cbs       = data.get("circuit_breakers", [])
        key_evts  = data.get("key_events", [])
        proposals = data.get("pending_proposals", [])

        if not any([hitl, policy, incidents, cbs, key_evts, proposals]):
            st.success("✅ All clear — no active issues in the last 24h.")

        def _row(color: str, line1: str, line2: str) -> str:
            return (
                f"<div style='border-left:3px solid {color};padding:4px 10px;margin-bottom:4px;'>"
                f"{line1}<br><small style='color:#aaa'>{line2}</small></div>"
            )

        # ── HITL Queue ────────────────────────────────────────────────────────
        with st.expander(f"🔔 HITL Queue ({len(hitl)})", expanded=bool(hitl)):
            if not hitl:
                st.caption("No pending HITL requests in the last 24h.")
            else:
                now_utc = datetime.now(timezone.utc)
                with st.container(height=210):
                    for req in hitl:
                        action  = req.get("action_type", "unknown")
                        risk    = req.get("risk_tier", "")
                        trace   = str(req.get("trace_id", "") or "")
                        created = str(req.get("created_at", ""))[:16]
                        try:
                            payload = json.loads(req.get("payload") or "{}")
                        except Exception:
                            payload = {}
                        agent  = payload.get("agent_role", req.get("run_id", "—"))
                        ctx    = payload.get("context", {})
                        metric = ctx.get("metric", "")
                        score  = ctx.get("score", "")
                        thresh = ctx.get("threshold", "")
                        try:
                            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            if not dt.tzinfo:
                                dt = dt.replace(tzinfo=timezone.utc)
                            wait_str = f"{int((now_utc - dt).total_seconds() / 60)}m ago"
                        except Exception:
                            wait_str = created
                        color  = "#ff4b4b" if risk == "critical" else "#ff8800"
                        detail = f"{metric} {score}/{thresh}" if metric else ""
                        tid    = f" · trace:{trace[:8]}" if trace else ""
                        st.markdown(
                            _row(color,
                                 f"<strong>{action}</strong> · {agent}",
                                 f"{wait_str} · {risk}" + (f" · {detail}" if detail else "") + tid),
                            unsafe_allow_html=True,
                        )

        # ── Policy Violations ─────────────────────────────────────────────────
        with st.expander(f"🚫 Policy Violations ({len(policy)})", expanded=bool(policy)):
            if not policy:
                st.caption("No policy blocks in the last 24h.")
            else:
                with st.container(height=210):
                    for p in policy:
                        metric = p.get("metric", "—")
                        value  = p.get("value", "")
                        thresh = p.get("threshold", "")
                        msg    = p.get("message", "")
                        ts     = str(p.get("ts", ""))[:16]
                        trace  = str(p.get("trace_id", "") or "")
                        tid    = f" · trace:{trace[:8]}" if trace else ""
                        st.markdown(
                            _row("#ff4b4b",
                                 f"<strong>{metric}</strong> · {value} / {thresh}",
                                 ts + (f" · {msg[:60]}" if msg else "") + tid),
                            unsafe_allow_html=True,
                        )

        # ── Active Incidents ──────────────────────────────────────────────────
        _sev_c = {"p0": "#ff4b4b", "p1": "#ff8800", "p2": "#ffc107", "p3": "#4b9eff"}
        with st.expander(f"🚨 Active Incidents ({len(incidents)})", expanded=bool(incidents)):
            if not incidents:
                st.caption("No open incidents.")
            else:
                with st.container(height=210):
                    for inc in incidents:
                        sev    = inc.get("severity", "p2")
                        itype  = inc.get("incident_type", "Incident")
                        agent  = inc.get("agent_role", "system")
                        ts     = str(inc.get("opened_at", ""))[:16]
                        iid    = str(inc.get("incident_id", "") or "")
                        detail = (inc.get("detail", "") or inc.get("root_cause", "") or "")[:60]
                        ref    = f" · id:{iid[:8]}" if iid else ""
                        st.markdown(
                            _row(_sev_c.get(sev, "#888"),
                                 f"<strong>[{sev.upper()}] {itype}</strong> · {agent}",
                                 ts + (f" · {detail}" if detail else "") + ref),
                            unsafe_allow_html=True,
                        )

        # ── Circuit Breakers ──────────────────────────────────────────────────
        with st.expander(f"⚡ Circuit Breakers ({len(cbs)})", expanded=bool(cbs)):
            if not cbs:
                st.caption("All circuit breakers closed.")
            else:
                with st.container(height=210):
                    for cb in cbs:
                        agent  = cb.get("agent_role", "unknown")
                        state  = cb.get("state", "open")
                        fails  = cb.get("failure_count", 0)
                        thresh = cb.get("failure_threshold", "?")
                        opened = str(cb.get("updated_at", ""))[:16]
                        reason = (cb.get("quarantine_reason", "") or "")[:60]
                        color  = "#ff4b4b" if state.lower() == "open" else "#ff8800"
                        st.markdown(
                            _row(color,
                                 f"<strong>{agent}</strong> · {state.upper()}",
                                 f"{opened} · {fails}/{thresh} failures" + (f" · {reason}" if reason else "")),
                            unsafe_allow_html=True,
                        )

        # ── Gateway Key Events ────────────────────────────────────────────────
        _ke_colors = {"rate_limit": "#ff8800", "budget_alert": "#ffc107", "model_not_allowed": "#ff4b4b"}
        _ke_icons  = {"rate_limit": "🚦", "budget_alert": "💸", "model_not_allowed": "🚫"}
        with st.expander(f"🔑 Gateway Key Events ({len(key_evts)}, last 1h)", expanded=bool(key_evts)):
            if not key_evts:
                st.caption("No key rejection events in the last hour.")
            else:
                with st.container(height=210):
                    for ev in key_evts:
                        etype  = ev.get("event_type", "")
                        key    = ev.get("key_prefix", "—")
                        agent  = ev.get("agent_role", "")
                        model  = ev.get("model_requested", "")
                        ts     = str(ev.get("ts", ""))[:16]
                        detail = (ev.get("detail", "") or "")[:60]
                        icon   = _ke_icons.get(etype, "⚠️")
                        color  = _ke_colors.get(etype, "#888")
                        line2_parts = [ts]
                        if agent:
                            line2_parts.append(agent)
                        if model and etype == "model_not_allowed":
                            line2_parts.append(f"model:{model}")
                        if detail:
                            line2_parts.append(detail)
                        st.markdown(
                            _row(color,
                                 f"{icon} <strong>{etype.replace('_', ' ').title()}</strong> · key:{key}",
                                 " · ".join(line2_parts)),
                            unsafe_allow_html=True,
                        )

        # ── Pending Change Proposals ──────────────────────────────────────────
        with st.expander(f"📋 Pending Change Proposals ({len(proposals)})", expanded=bool(proposals)):
            if not proposals:
                st.caption("No pending change proposals.")
            else:
                with st.container(height=210):
                    for p in proposals:
                        ctype  = p.get("change_type", "change")
                        agent  = p.get("agent_role", "")
                        sys_id = p.get("system_id", "")
                        desc   = (p.get("description", "") or "")[:80]
                        by_    = p.get("proposed_by", "")
                        ts     = str(p.get("proposed_at", ""))[:16]
                        scope  = " · ".join(x for x in [agent, sys_id] if x and x != "*")
                        by_str = f" · by:{by_}" if by_ else ""
                        st.markdown(
                            _row("#4b9eff",
                                 f"<strong>{ctype.replace('_', ' ').title()}</strong>"
                                 + (f" · {scope}" if scope else ""),
                                 ts + by_str + (f" · {desc}" if desc else "")),
                            unsafe_allow_html=True,
                        )

        st.caption(f"↺ {datetime.now().strftime('%H:%M:%S')}")

    _render_system_state()

    # MCP connection info
    with st.expander("🔌 Connect via MCP (Claude Code / external agents)", expanded=False):
        mcp_sse_url = "http://localhost:8003/mcp/sse"
        st.markdown("**Add to Claude Code** (run once on your machine):")
        st.code(f"claude mcp add evalgov --transport sse {mcp_sse_url}", language="bash")
        st.markdown("**Verify connection:**")
        st.code("claude mcp list", language="bash")
        st.caption(
            "The MCP server is exposed on port 8003. Once connected, Claude Code can call all 39 EvalGov "
            "tools directly. Try: *'what's the system health?'* or *'approve HITL request X'* in your Claude Code session."
        )
        st.markdown("**Session note:** Chat context is maintained for the duration of your browser session. "
                    "Use 🗑️ Clear Chat to start a fresh conversation.")
