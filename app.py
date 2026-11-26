"""Unified mIHC Analysis & Coregistration Streamlit application."""
import os
import sys

import streamlit as st

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
for sub in ("mihc", "coreg"):
    p = os.path.join(ROOT_DIR, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["UNIFIED_APP"] = "1"

st.set_page_config(page_title="mIHC & Coregistration Suite", page_icon="🔬", layout="wide")

pg = st.navigation(
    [
        st.Page(os.path.join(ROOT_DIR, "mihc", "easy_app.py"), title="mIHC Analysis", icon="📊", default=True),
        st.Page(os.path.join(ROOT_DIR, "coreg", "easy_app.py"), title="Coregistration", icon="🔬"),
    ]
)
pg.run()
