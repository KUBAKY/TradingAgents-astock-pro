"""全局暗色橙主题：app.py 与短线页共用，避免两页风格漂移。"""

from __future__ import annotations

import streamlit as st

_THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap');

/* Hide Streamlit chrome for clean video recording.
   IMPORTANT: do NOT `display:none` the whole header OR the whole toolbar.
   In Streamlit >= 1.36 the "expand sidebar" button lives *inside* the
   toolbar (header > stToolbar > stExpandSidebarButton), so hiding either
   one makes a collapsed sidebar impossible to reopen (issue #36). Instead
   keep the header/toolbar in the DOM, make the header transparent, and
   hide only the individual chrome widgets we don't want on camera. */
#MainMenu,
footer,
div[data-testid="stDecoration"],
div[data-testid="stStatusWidget"],
div[data-testid="stToolbarActions"],
div[data-testid="stAppDeployButton"],
span[data-testid="stMainMenu"] { display: none !important; }
header[data-testid="stHeader"] {
    background: transparent !important;
    box-shadow: none !important;
}
/* Keep the sidebar collapse / expand controls always visible & clickable.
   Selector list spans multiple Streamlit versions. */
button[data-testid="stExpandSidebarButton"],
button[data-testid="stSidebarCollapseButton"],
button[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"] {
    display: flex !important;
    visibility: visible !important;
    opacity: 1 !important;
}

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, sans-serif;
}
.stApp {
    background: #0a0a0a;
}
section[data-testid="stSidebar"] {
    background: #0f0f0f;
    border-right: 1px solid #1a1a1a;
}
.stMetric label { color: #888 !important; font-size: 0.8rem !important; }
.stMetric [data-testid="stMetricValue"] {
    color: #ff5a1f !important;
    font-weight: 700 !important;
}
.stProgress > div > div > div {
    background: linear-gradient(90deg, #ff5a1f, #ff8c42) !important;
}
button[kind="primary"] {
    background: linear-gradient(135deg, #ff5a1f, #ff8c42) !important;
    border: none !important;
    font-weight: 700 !important;
    letter-spacing: 0.05em !important;
    box-shadow: 0 4px 15px rgba(255,90,31,0.3) !important;
    transition: all 0.2s ease !important;
}
button[kind="primary"]:hover {
    background: linear-gradient(135deg, #e04d15, #ff5a1f) !important;
    box-shadow: 0 6px 20px rgba(255,90,31,0.4) !important;
    transform: translateY(-1px) !important;
}
/* Secondary buttons (history items) */
button[kind="secondary"] {
    background: #161616 !important;
    border: 1px solid #2a2a2a !important;
    color: #ccc !important;
    transition: all 0.2s ease !important;
}
button[kind="secondary"]:hover {
    background: #1e1e1e !important;
    border-color: #ff5a1f !important;
    color: #ff5a1f !important;
}
.stExpander {
    border: 1px solid #222 !important;
    border-radius: 8px !important;
}
.stTabs [data-baseweb="tab"] {
    color: #888 !important;
}
.stTabs [aria-selected="true"] {
    color: #ff5a1f !important;
    border-bottom-color: #ff5a1f !important;
}
div[data-testid="stDownloadButton"] button {
    background: #1a1a2e !important;
    border: 1px solid #ff5a1f !important;
    color: #ff5a1f !important;
}
/* Text input styling */
input[data-testid="stTextInputRootElement"] input,
.stTextInput input {
    background: #161616 !important;
    border-color: #2a2a2a !important;
    color: #f5f1eb !important;
}
.stTextInput input:focus {
    border-color: #ff5a1f !important;
    box-shadow: 0 0 0 1px #ff5a1f !important;
}
/* Date input styling */
.stDateInput input {
    background: #161616 !important;
    border-color: #2a2a2a !important;
    color: #f5f1eb !important;
}
/* Number input styling */
.stNumberInput input {
    background: #161616 !important;
    border-color: #2a2a2a !important;
    color: #f5f1eb !important;
}
</style>
"""


def apply_theme() -> None:
    """注入全局暗色橙主题 CSS（每页 set_page_config 之后调用一次）。"""
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
