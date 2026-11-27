"""In-page folder / file browser for Streamlit. Lists the folders of the machine that runs the app (PC or server), so it works
the same locally and in Docker / Railway, where a desktop dialog cannot open.
Use: st.button("Browse", on_click=server_picker.pick, args=(target_key, "dir" | "file", "Title")). The chosen path is put in
st.session_state[target_key] (a text_input with that key is filled in, a plain key is just set)."""
import os
import string

import streamlit as st


def roots():
    if os.name == "nt":
        return [f"{d}:{os.sep}" for d in string.ascii_uppercase if os.path.exists(f"{d}:{os.sep}")]
    return [p for p in ("/data", "/") if os.path.isdir(p)]


def _set(key, path):
    st.session_state[key] = path           # runs as a button callback: the only moment a widget's value may be changed


def _body(key, kind, exts):
    home = (roots() or [os.path.expanduser("~")])[0]
    ck = f"pk_cur_{key}"
    cur = st.session_state.get(ck) or st.session_state.get(key) or home
    if kind == "file" and os.path.isfile(cur):
        cur = os.path.dirname(cur)
    if not os.path.isdir(cur):
        cur = home
    jump = st.text_input("Jump to a path (optional)", placeholder="e.g. a network path", key=f"pk_jump_{key}")
    if jump and os.path.isdir(jump) and jump != st.session_state.get(f"pk_last_{key}"):
        st.session_state[f"pk_last_{key}"] = st.session_state[ck] = cur = jump
    st.markdown(f"**{cur}**")
    top = st.columns(3)
    parent = os.path.dirname(cur.rstrip("/" + os.sep)) or cur
    if top[0].button("Up", disabled=parent == cur, use_container_width=True, key=f"pk_up_{key}"):
        st.session_state[ck] = parent
        st.rerun(scope="fragment")
    if os.name == "nt":
        drive = top[1].selectbox("Drive", roots(), index=None, placeholder="Drive...", label_visibility="collapsed", key=f"pk_dr_{key}")
        if drive and drive != st.session_state.get(f"pk_drl_{key}"):
            st.session_state[f"pk_drl_{key}"] = st.session_state[ck] = drive
            st.rerun(scope="fragment")
    try:
        ents = list(os.scandir(cur))
    except OSError:
        ents = []
        st.warning("Cannot open this folder.")
    subs = sorted((e.name for e in ents if e.is_dir() and not e.name.startswith((".", "$"))), key=str.lower)
    cols = st.columns(3)
    for i, name in enumerate(subs[:300]):
        if cols[i % 3].button("📁 " + name, key=f"pk_{key}_{i}", use_container_width=True):
            st.session_state[ck] = os.path.join(cur, name)
            st.rerun(scope="fragment")
    if len(subs) > 300:
        st.caption(f"... {len(subs) - 300} more folders not shown; use 'Jump to a path'.")
    if kind == "file":
        files = sorted((e.name for e in ents if e.is_file() and (not exts or e.name.lower().endswith(exts))), key=str.lower)
        st.markdown("**Files**" if files else "_No matching files in this folder._")
        for i, name in enumerate(files[:300]):
            if st.button(name, key=f"pf_{key}_{i}", use_container_width=True, on_click=_set, args=(key, os.path.join(cur, name))):
                st.rerun()
    else:
        if st.button("Select this folder", type="primary", key=f"pk_ok_{key}", on_click=_set, args=(key, cur)):
            st.rerun()


def pick(key, kind="dir", title="Select", exts=None):
    st.dialog(title, width="large")(_body)(key, kind, tuple(exts) if exts else None)
