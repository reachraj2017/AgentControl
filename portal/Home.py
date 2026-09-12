"""AI Control Plane — navigation router."""

import streamlit as st

st.set_page_config(
    page_title="AI Control Plane",
    page_icon="🎛️",
    layout="wide",
)

st.markdown("""
<style>
/* Remove default side padding so content fills the full browser width */
.block-container {
    padding-left: 1.5rem !important;
    padding-right: 1.5rem !important;
    max-width: 100% !important;
}
</style>
""", unsafe_allow_html=True)

pg = st.navigation(
    {
        "": [
            st.Page("home_page.py", title="Home", icon="🏠", default=True),
        ],
        "M1 · Observability & Evaluation": [
            st.Page("pages/11_Eval_Testing.py",    title="Eval Testing",    icon="🧪"),
            st.Page("pages/12_Eval_Measurements.py", title="Eval Measurements", icon="📏"),
        ],
        "M2 · Governance & Enforcement": [
            st.Page("pages/21_AI_Governance.py", title="AI Governance", icon="🛡️"),
            st.Page("pages/22_Enforcement.py",   title="Enforcement",   icon="⚖️"),
        ],
        "M3 · Agent Gateway": [
            st.Page("pages/30_Gateway_Dashboard.py", title="Gateway Dashboard", icon="📡"),
            st.Page("pages/31_Call_Log.py",          title="Call Log",          icon="📋"),
            st.Page("pages/32_Routing.py",           title="Routing",           icon="🔀"),
            st.Page("pages/33_Prompt_Mods.py",       title="Prompt Mods",       icon="✏️"),
            st.Page("pages/34_Shadow_Mode.py",       title="Shadow Mode",       icon="👥"),
            st.Page("pages/35_AB_Testing.py",        title="A/B Testing",       icon="🔬"),
            st.Page("pages/36_Changes.py",           title="Changes",           icon="📝"),
            st.Page("pages/37_API_Keys.py",          title="API Keys",          icon="🔑"),
            st.Page("pages/38_Traffic_Management.py", title="Traffic Management", icon="🔁"),
        ],
        "M4 · EvalGov Intelligence": [
            st.Page("pages/40_EvalGov_Agent.py", title="EvalGov Agent", icon="🤖"),
        ],
    }
)

pg.run()
