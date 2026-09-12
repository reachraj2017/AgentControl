"""Traffic Management — endpoint pools and selection strategies for M3 gateway."""

import os

import requests
import streamlit as st

_GW  = os.getenv("GATEWAY_URL", "http://agent-gateway:8080")
_KEY = os.getenv("GATEWAY_MASTER_KEY", "")

_STRATEGY_HELP = {
    "round_robin":    "Rotate through endpoints evenly",
    "weighted":       "Probabilistic selection by endpoint weight",
    "least_latency":  "Pick lowest avg latency (last 5 min)",
    "performance":    "Pick highest faithfulness score (last 1h)",
    "cost_optimized": "Pick cheapest model (estimated cost rank)",
    "fallback_chain": "Try endpoints in priority order (1 = first)",
}
_STRATEGIES = list(_STRATEGY_HELP.keys())

_STRATEGY_ICON = {
    "round_robin":    "🔄",
    "weighted":       "⚖️",
    "least_latency":  "⚡",
    "performance":    "🏆",
    "cost_optimized": "💰",
    "fallback_chain": "🔗",
}

_BACKENDS = ["openai", "ollama", "anthropic"]


def _headers():
    h = {"Content-Type": "application/json"}
    if _KEY:
        h["X-Gateway-Admin-Key"] = _KEY
    return h


def _get(path: str) -> dict:
    try:
        r = requests.get(f"{_GW}/gateway{path}", headers=_headers(), timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _post(path: str, body: dict) -> dict:
    try:
        r = requests.post(f"{_GW}/gateway{path}", json=body, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _delete(path: str) -> dict:
    try:
        r = requests.delete(f"{_GW}/gateway{path}", headers=_headers(), timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ── Session state for new-pool endpoint rows ──────────────────────────────────

if "new_ep_rows" not in st.session_state:
    st.session_state.new_ep_rows = [
        {"model": "", "backend": "openai", "weight": 1.0, "priority": 1}
    ]


def _add_ep_row():
    n = len(st.session_state.new_ep_rows)
    st.session_state.new_ep_rows.append(
        {"model": "", "backend": "openai", "weight": 1.0, "priority": n + 1}
    )


def _remove_ep_row(i: int):
    st.session_state.new_ep_rows.pop(i)
    if not st.session_state.new_ep_rows:
        st.session_state.new_ep_rows = [
            {"model": "", "backend": "openai", "weight": 1.0, "priority": 1}
        ]


# ── Load data ─────────────────────────────────────────────────────────────────

pools_data    = _get("/traffic/pools")
policies_data = _get("/traffic/policies")

pools    = pools_data.get("pools", [])
policies = policies_data.get("policies", [])

# index for quick lookup
pool_map      = {p["pool_id"]: p for p in pools}
# which policy points to each pool_id
policy_by_pool: dict[str, dict] = {}
for _pol in policies:
    _pid = _pol.get("pool_id", "")
    if _pid not in policy_by_pool:
        policy_by_pool[_pid] = _pol


# ── Page ──────────────────────────────────────────────────────────────────────

st.title("🔁 Traffic Management")
st.caption("Create pools of LLM endpoints and route agent traffic using load balancing strategies.")

tab_pools, tab_new, tab_stats = st.tabs(["🗂️ Pools", "➕ New Pool", "📊 Live Stats"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Pools (consolidated view: pool + endpoints + policy together)
# ══════════════════════════════════════════════════════════════════════════════

with tab_pools:
    if not pools:
        st.info("No pools yet — use the **New Pool** tab to create one.")
    else:
        for pool in pools:
            pool_id   = pool.get("pool_id", "")
            name      = pool.get("name", "unnamed")
            strategy  = pool.get("strategy", "round_robin")
            icon      = _STRATEGY_ICON.get(strategy, "🔀")
            endpoints = pool.get("endpoints", [])
            policy    = policy_by_pool.get(pool_id)

            with st.expander(
                f"{icon} **{name}** — `{strategy}` — "
                f"{len(endpoints)} endpoint(s)"
                + (" — 🎯 policy active" if policy else " — ⚠️ no policy"),
                expanded=True,
            ):

                # ── Pool header row ───────────────────────────────────────────
                h1, h2 = st.columns([9, 1])
                with h1:
                    st.caption(
                        f"{_STRATEGY_HELP.get(strategy, strategy)}"
                        + (f"  ·  {pool.get('description')}" if pool.get("description") else "")
                    )
                with h2:
                    if st.button("🗑️ Pool", key=f"del_pool_{pool_id}",
                                 help="Delete pool (also deletes its policy)"):
                        if policy:
                            _delete(f"/traffic/policies/{policy['policy_id']}")
                        res = _delete(f"/traffic/pools/{pool_id}")
                        if "error" in res:
                            st.error(res["error"])
                        else:
                            st.rerun()

                # ── Policy row ────────────────────────────────────────────────
                if policy:
                    p1, p2 = st.columns([9, 1])
                    role  = policy.get("agent_role", "*")
                    sid   = policy.get("system_id", "*")
                    sticky = " 🔒 sticky" if policy.get("sticky") else ""
                    p1.markdown(
                        f"🎯 **Policy:** `{role}` / `{sid}`{sticky}"
                    )
                    if p2.button("🗑️ Policy", key=f"del_pol_{policy['policy_id']}"):
                        res = _delete(f"/traffic/policies/{policy['policy_id']}")
                        if "error" in res:
                            st.error(res["error"])
                        else:
                            st.rerun()
                else:
                    st.warning("No traffic policy linked to this pool — traffic won't route here.")

                st.divider()

                # ── Endpoints list ────────────────────────────────────────────
                if endpoints:
                    hc = st.columns([4, 2, 1, 1, 1])
                    hc[0].markdown("**Model**")
                    hc[1].markdown("**Backend**")
                    hc[2].markdown("**Weight**")
                    hc[3].markdown("**Priority**")
                    hc[4].markdown("")
                    for ep in endpoints:
                        ec = st.columns([4, 2, 1, 1, 1])
                        ec[0].code(ep.get("model", ""), language=None)
                        ec[1].write(ep.get("backend", "openai"))
                        ec[2].write(f"{float(ep.get('weight', 1.0)):.1f}")
                        ec[3].write(str(ep.get("priority", 1)))
                        if ec[4].button("✕", key=f"del_ep_{ep['endpoint_id']}",
                                        help="Remove endpoint"):
                            res = _delete(f"/traffic/endpoints/{ep['endpoint_id']}")
                            if "error" in res:
                                st.error(res["error"])
                            else:
                                st.rerun()
                else:
                    st.warning("No endpoints — add one below.")

                # ── Add endpoint inline ───────────────────────────────────────
                st.caption("＋ Add endpoint")
                ac = st.columns([4, 2, 1, 1, 1])
                add_model   = ac[0].text_input("Model", key=f"add_m_{pool_id}",
                                               placeholder="anthropic/claude-haiku-4-5-20251001",
                                               label_visibility="collapsed")
                add_backend = ac[1].selectbox("Backend", _BACKENDS,
                                              key=f"add_b_{pool_id}",
                                              label_visibility="collapsed")
                add_weight  = ac[2].number_input("Weight", 0.1, 10.0, 1.0, 0.1,
                                                 key=f"add_w_{pool_id}",
                                                 label_visibility="collapsed")
                add_prio    = ac[3].number_input("Priority", 1, 99,
                                                 len(endpoints) + 1,
                                                 key=f"add_p_{pool_id}",
                                                 label_visibility="collapsed")
                if ac[4].button("Add", key=f"add_ep_{pool_id}", type="primary"):
                    if not add_model.strip():
                        st.warning("Model is required")
                    elif "," in add_model:
                        st.error("One model per row — add a separate row for each model.")
                    else:
                        res = _post(f"/traffic/pools/{pool_id}/endpoints", {
                            "model":    add_model.strip(),
                            "backend":  add_backend,
                            "weight":   add_weight,
                            "priority": add_prio,
                        })
                        if "error" in res:
                            st.error(res["error"])
                        else:
                            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — New Pool (unified: pool + endpoints + policy in one form)
# ══════════════════════════════════════════════════════════════════════════════

with tab_new:
    st.subheader("Create pool")
    st.caption("Fill in pool settings, add at least one endpoint, then set the traffic policy — all saved together.")

    # ── Pool settings ─────────────────────────────────────────────────────────
    np1, np2 = st.columns(2)
    np_name  = np1.text_input("Pool name *", placeholder="my-llm-pool")
    np_strat = np2.selectbox(
        "Strategy *",
        _STRATEGIES,
        format_func=lambda s: f"{_STRATEGY_ICON.get(s,'')}  {s}  —  {_STRATEGY_HELP[s]}",
    )
    np_desc = st.text_input("Description (optional)")

    st.divider()

    # ── Endpoints (dynamic rows) ──────────────────────────────────────────────
    st.markdown("**Endpoints** *(one model per row)*")

    col_headers = st.columns([4, 2, 1, 1, 1])
    col_headers[0].caption("Model")
    col_headers[1].caption("Backend")
    col_headers[2].caption("Weight")
    col_headers[3].caption("Priority")
    col_headers[4].caption("")

    for i, row in enumerate(st.session_state.new_ep_rows):
        rc = st.columns([4, 2, 1, 1, 1])
        st.session_state.new_ep_rows[i]["model"] = rc[0].text_input(
            f"model_{i}", value=row["model"],
            placeholder="anthropic/claude-haiku-4-5-20251001",
            label_visibility="collapsed", key=f"nep_model_{i}",
        )
        st.session_state.new_ep_rows[i]["backend"] = rc[1].selectbox(
            f"backend_{i}", _BACKENDS,
            index=_BACKENDS.index(row["backend"]) if row["backend"] in _BACKENDS else 0,
            label_visibility="collapsed", key=f"nep_backend_{i}",
        )
        st.session_state.new_ep_rows[i]["weight"] = rc[2].number_input(
            f"weight_{i}", 0.1, 10.0, float(row["weight"]), 0.1,
            label_visibility="collapsed", key=f"nep_weight_{i}",
        )
        st.session_state.new_ep_rows[i]["priority"] = rc[3].number_input(
            f"priority_{i}", 1, 99, int(row["priority"]),
            label_visibility="collapsed", key=f"nep_prio_{i}",
        )
        if len(st.session_state.new_ep_rows) > 1:
            if rc[4].button("✕", key=f"nep_del_{i}"):
                _remove_ep_row(i)
                st.rerun()

    if st.button("＋ Add another endpoint"):
        _add_ep_row()
        st.rerun()

    st.divider()

    # ── Traffic policy ────────────────────────────────────────────────────────
    st.markdown("**Traffic policy** *(which agents use this pool)*")
    pp1, pp2, pp3 = st.columns(3)
    np_role   = pp1.text_input("Agent role", value="*", help="'*' matches all roles")
    np_sysid  = pp2.text_input("System ID",  value="*", help="'*' matches all systems")
    np_sticky = pp3.checkbox("Sticky sessions",
                             help="Pin a conversation (trace_id) to the same endpoint")

    st.divider()

    # ── Submit ────────────────────────────────────────────────────────────────
    if st.button("🚀 Create pool", type="primary", use_container_width=True):
        errors = []
        if not np_name.strip():
            errors.append("Pool name is required.")
        ep_rows = [r for r in st.session_state.new_ep_rows if r["model"].strip()]
        if not ep_rows:
            errors.append("Add at least one endpoint (fill in the Model field).")
        for r in ep_rows:
            if "," in r["model"]:
                errors.append(f"'{r['model']}' contains a comma — one model per row.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            progress = st.progress(0, text="Creating pool…")

            # 1. Create pool
            pool_res = _post("/traffic/pools", {
                "name":        np_name.strip(),
                "strategy":    np_strat,
                "description": np_desc.strip(),
            })
            if "error" in pool_res:
                st.error(f"Pool creation failed: {pool_res['error']}")
                st.stop()

            new_pool_id = pool_res["pool_id"]
            progress.progress(33, text=f"Pool created. Adding {len(ep_rows)} endpoint(s)…")

            # 2. Create endpoints
            ep_errors = []
            for ep in ep_rows:
                ep_res = _post(f"/traffic/pools/{new_pool_id}/endpoints", {
                    "model":    ep["model"].strip(),
                    "backend":  ep["backend"],
                    "weight":   ep["weight"],
                    "priority": ep["priority"],
                })
                if "error" in ep_res:
                    ep_errors.append(f"{ep['model']}: {ep_res['error']}")

            if ep_errors:
                for e in ep_errors:
                    st.warning(f"Endpoint error — {e}")

            progress.progress(66, text="Creating traffic policy…")

            # 3. Create traffic policy
            pol_res = _post("/traffic/policies", {
                "agent_role": np_role.strip() or "*",
                "system_id":  np_sysid.strip() or "*",
                "pool_id":    new_pool_id,
                "sticky":     np_sticky,
            })
            if "error" in pol_res:
                st.warning(f"Policy creation failed: {pol_res['error']}")

            progress.progress(100, text="Done.")

            # Reset endpoint rows for next use
            st.session_state.new_ep_rows = [
                {"model": "", "backend": "openai", "weight": 1.0, "priority": 1}
            ]
            st.success(
                f"✅ Pool **{np_name.strip()}** created with "
                f"{len(ep_rows)} endpoint(s) and a traffic policy."
            )
            st.rerun()

    st.divider()
    with st.expander("ℹ️ Strategy guide"):
        for s, desc in _STRATEGY_HELP.items():
            st.markdown(f"**`{_STRATEGY_ICON.get(s,'')} {s}`** — {desc}")
        st.markdown("""
**Routing priority** (first match wins on each request):

1. **Traffic policy** → pool selection ← this feature
2. A/B test (if a running test matches)
3. Routing policy (static model override)
4. Passthrough (model_requested unchanged)

**Weight** is only used by `weighted` strategy.
**Priority** is only used by `fallback_chain` strategy.
""")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Live Stats
# ══════════════════════════════════════════════════════════════════════════════

with tab_stats:
    _HOUR_OPTIONS = {"1 hr": 1, "6 hrs": 6, "12 hrs": 12, "24 hrs": 24, "48 hrs": 48, "1 week": 168}
    sc1, sc2, sc3 = st.columns([3, 6, 1])
    _win_label = sc1.selectbox("Time window", list(_HOUR_OPTIONS.keys()), index=0, key="stats_hours")
    _win_hours = _HOUR_OPTIONS[_win_label]
    sc3.write("")
    if sc3.button("🔄", key="refresh_stats", help="Refresh"):
        st.rerun()

    stats_data = _get(f"/traffic/stats?hours={_win_hours}")
    stats_rows = stats_data.get("stats", [])
    stats      = {r["model_used"]: r for r in stats_rows}

    if not pools:
        st.info("No pools configured yet.")
    elif not stats_rows:
        st.info(f"No pool-routed calls in the last {_win_label}. Traffic routed via pools will appear here.")
        for pool in pools:
            strategy = pool.get("strategy", "round_robin")
            icon     = _STRATEGY_ICON.get(strategy, "🔀")
            eps      = pool.get("endpoints", [])
            st.markdown(f"**{icon} {pool.get('name','')}** — {len(eps)} endpoint(s) configured, no traffic yet")
    else:
        active_models = set(stats.keys())

        for pool in pools:
            pool_id   = pool.get("pool_id", "")
            strategy  = pool.get("strategy", "round_robin")
            icon      = _STRATEGY_ICON.get(strategy, "🔀")
            endpoints = pool.get("endpoints", [])

            st.markdown(f"**{icon} {pool.get('name', '')}** `{strategy}`")

            hc = st.columns([4, 2, 1, 1, 1, 1])
            hc[0].caption("Model (used)")
            hc[1].caption("Routing reason")
            hc[2].caption(f"Calls ({_win_label})")
            hc[3].caption("Avg latency")
            hc[4].caption("Tokens")
            hc[5].caption("Error rate")

            pool_prefix = f"pool:{pool_id[:8]}"
            pool_rows = [r for r in stats_rows if r.get("routing_reason", "").startswith(pool_prefix)]

            if pool_rows:
                for row in pool_rows:
                    calls   = int(row.get("calls", 0))
                    latency = int(row.get("avg_latency_ms", 0))
                    tokens  = int(row.get("total_tokens", 0))
                    err_pct = float(row.get("error_rate", 0)) * 100
                    ec = st.columns([4, 2, 1, 1, 1, 1])
                    ec[0].code(row.get("model_used", ""), language=None)
                    ec[1].caption(row.get("routing_reason", ""))
                    ec[2].write(str(calls))
                    ec[3].write(f"{latency} ms")
                    ec[4].write(f"{tokens:,}")
                    color = "🔴" if err_pct > 5 else "🟢"
                    ec[5].write(f"{color} {err_pct:.1f}%")
            else:
                st.caption(f"No calls routed through this pool in the last {_win_label}.")

            idle = [ep for ep in endpoints if ep.get("model", "") not in active_models]
            if idle:
                for ep in idle:
                    ec = st.columns([4, 2, 1, 1, 1, 1])
                    ec[0].code(ep.get("model", ""), language=None)
                    ec[1].caption("idle")
                    for col in ec[2:]:
                        col.write("—")

            st.divider()
