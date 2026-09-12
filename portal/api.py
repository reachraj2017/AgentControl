"""Shared API client for all gateway UI pages."""

import os
import streamlit as st
import httpx

GATEWAY_URL     = os.getenv("GATEWAY_URL",             "http://agent-gateway:8080")
GOVERNANCE_URL  = os.getenv("GOVERNANCE_SERVICE_URL",  "http://governance-service:8002")
_MASTER_KEY     = os.getenv("GATEWAY_MASTER_KEY", "")
_TIMEOUT        = 8.0


def _admin_headers() -> dict:
    if _MASTER_KEY:
        return {"x-gateway-admin-key": _MASTER_KEY}
    return {}


def gateway_url() -> str:
    return GATEWAY_URL


def is_online() -> bool:
    try:
        r = httpx.get(f"{GATEWAY_URL}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def get(path: str, show_error: bool = True) -> dict | None:
    try:
        r = httpx.get(f"{GATEWAY_URL}{path}", headers=_admin_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        if show_error:
            st.error(f"Gateway error {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        if show_error:
            st.error(f"Cannot reach gateway at {GATEWAY_URL}: {e}")
    return None


def post(path: str, data: dict, show_error: bool = True) -> dict | None:
    try:
        r = httpx.post(f"{GATEWAY_URL}{path}", json=data, headers=_admin_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        if show_error:
            st.error(f"Gateway error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        if show_error:
            st.error(f"Request failed: {e}")
    return None


def put(path: str, data: dict, show_error: bool = True) -> dict | None:
    try:
        r = httpx.put(f"{GATEWAY_URL}{path}", json=data, headers=_admin_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        if show_error:
            st.error(f"Gateway error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        if show_error:
            st.error(f"Request failed: {e}")
    return None


def patch(path: str, data: dict, show_error: bool = True) -> dict | None:
    try:
        r = httpx.patch(f"{GATEWAY_URL}{path}", json=data, headers=_admin_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        if show_error:
            st.error(f"Gateway error {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        if show_error:
            st.error(f"Request failed: {e}")
    return None


def delete(path: str, show_error: bool = True) -> bool:
    try:
        r = httpx.delete(f"{GATEWAY_URL}{path}", headers=_admin_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        if show_error:
            st.error(f"Gateway error {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        if show_error:
            st.error(f"Request failed: {e}")
    return False


def gov_get(path: str) -> dict | list | None:
    """GET from governance service — returns None silently on failure."""
    try:
        r = httpx.get(f"{GOVERNANCE_URL}{path}", timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def sidebar_status() -> None:
    """Render gateway connection status in the sidebar."""
    st.sidebar.markdown("---")
    online = is_online()
    if online:
        st.sidebar.success("Gateway online", icon="✅")
    else:
        st.sidebar.error("Gateway offline", icon="🔴")
    st.sidebar.caption(f"`{GATEWAY_URL}`")
