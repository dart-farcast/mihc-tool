"""mIHC Easy Analysis - counts / Log2FC / Prism / % pairs / distance / proximity for a set of samples.
Launch with `Launch Easy mIHC App.bat`.  Engine: mihc_engine.py"""
import io, os, shutil, tempfile, zipfile
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import mihc_engine as E
import server_picker

if not os.environ.get("UNIFIED_APP"):                 # the unified app (app.py) sets the page config once
    st.set_page_config(page_title="mIHC Easy Analysis", layout="wide")
st.title("mIHC Analysis")


@st.cache_data(show_spinner="Reading the CSV...")
def cached_detect(path, mtime):
    return E.detect(path)


def detect_block(path, key):
    """Arms + markers section. Returns what the engine needs."""
    info = cached_detect(path, os.path.getmtime(path))
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.subheader("Arms")
        arms_df = info["arms"].reset_index().rename(columns={"arm": "Code", "images": "Explants found"})
        arms_df.insert(1, "Arm name", ["Rx" + c[1] for c in arms_df["Code"]])
        ed = st.data_editor(arms_df[["Code", "Arm name", "Explants found"]], hide_index=True, use_container_width=True,
                            disabled=["Code", "Explants found"], key=f"{key}_arms")
        arm_names = dict(zip(ed["Code"], ed["Arm name"]))
        ref = st.selectbox("Control / reference arm (for Log2FC)", list(arm_names.values()), key=f"{key}_ref")
        n_exp = int(st.number_input("Explants per arm", 1, 40, int(info["arms"]["max_explant"].max()), key=f"{key}_nexp"))
    with c2:
        st.subheader("Markers")
        tok = info["tokens"].rename("Cells").reset_index().rename(columns={"index": "Name in CSV"})
        tok.insert(0, "Count it", True)
        tok.insert(2, "Marker name", tok["Name in CSV"])
        if any(E.DYE_RE.search(t) for t in tok["Name in CSV"]):
            st.warning("Some names look like fluorescent channels. Type the protein in 'Marker name' (e.g. APC op690 → FoxP3). "
                       "If one channel means different proteins on different slides, export marker names from QuPath instead - "
                       "channel names alone cannot tell them apart.")
        tok_ed = st.data_editor(tok, hide_index=True, use_container_width=True, disabled=["Name in CSV", "Cells"], key=f"{key}_tok")
    rename = {r["Name in CSV"]: (r["Marker name"].strip() or r["Name in CSV"]) for _, r in tok_ed.iterrows()}
    singles = list(dict.fromkeys(rename[r["Name in CSV"]] for _, r in tok_ed[tok_ed["Count it"]].iterrows()))

    combos = {}                      # most frequent multi-marker labels, in marker-name space
    for label, n in info["labels"].items():
        names = tuple(dict.fromkeys(rename.get(t, t) for t in E._tokens(label)))
        if len(names) > 1 and all(x in singles for x in names):
            combos[names] = combos.get(names, 0) + n
    top = [k for k, _ in sorted(combos.items(), key=lambda kv: -kv[1])[:12]]
    fmt = lambda k: "+".join(k) + "+"
    picked = st.multiselect("Also count these double / triple positives as their own marker (optional)", top, key=f"{key}_combo",
                            format_func=lambda k: f"{fmt(k)}   ({combos[k]:,} cells)")
    markers = {m: [m] for m in singles}
    markers.update({fmt(k): list(k) for k in picked})
    st.caption("A cell counts toward every marker it contains, so a CD8+GzmB+ cell is also counted under CD8 and GzmB.")
    return {"info": info, "arm_names": arm_names, "ref": ref, "n_exp": n_exp, "rename": rename, "markers": markers}


def pair_editor(key, left, right, cols):
    df = st.data_editor(pd.DataFrame({cols[0]: pd.Series(dtype=str), cols[1]: pd.Series(dtype=str)}), num_rows="dynamic",
                        column_config={cols[0]: st.column_config.SelectboxColumn(options=left),
                                       cols[1]: st.column_config.SelectboxColumn(options=right)},
                        use_container_width=True, key=key)
    return [(r[cols[0]], r[cols[1]]) for _, r in df.dropna().iterrows()]


def analyze():
    st.caption("Counts per zone · Log2FC · Prism export · % pairs · cell-cell distance · proximity. Five steps, top to bottom.")
    st.header("1 · Your data")
    local = server_picker.is_local()
    opts = ["Folder on this PC", "Upload files"] if local else ["Folder on my PC", "Files from my PC"]
    src = st.radio("Where are the QuPath measurement CSVs?", opts, horizontal=True)
    folder_mode = src in ("Folder on this PC", "Folder on my PC")
    src = "Folder" if local and folder_mode else "Upload"
    found, out_default = {}, ""
    if src == "Folder":
        c1, c2 = st.columns([1, 5], vertical_alignment="center")
        c1.button("Browse...", on_click=server_picker.pick, args=("folder", "dir", "Select the project folder"), key="browse_btn")
        folder = st.session_state.get("folder", "")
        c2.text_input("Project folder - one sub-folder per sample, each holding its CSV", value=folder, disabled=True,
                      placeholder="Click Browse... and pick the folder (on this PC or the server)")
        if folder:
            if os.path.isdir(folder):
                with st.spinner("Looking for measurement CSVs..."):
                    found = E.find_samples(folder)
            else:
                st.error("That folder does not exist.")
            out_default = os.path.join(folder, "Results")
    else:
        # Streamlit's uploader only picks files; this turns its Browse button into the PC's folder window (same page, so the script may reach it)
        components.html("<script>const f=()=>{for(const i of parent.document.querySelectorAll('input[type=file]'))"
                        "{i.webkitdirectory=%s;}};f();new MutationObserver(f).observe(parent.document.body,{childList:true,subtree:true});</script>"
                        % ("true" if folder_mode else "false"), height=0)
        ups = st.file_uploader(("Click Browse files and choose the folder on your PC - its CSVs are uploaded" if folder_mode else "Click Browse files and select the CSVs on your PC"), type="csv", accept_multiple_files=True)
        tmpdir = st.session_state.setdefault("tmpdir", tempfile.mkdtemp())
        for u in ups or []:
            p = os.path.join(tmpdir, u.name)
            if not os.path.exists(p) or os.path.getsize(p) != u.size:
                with open(p, "wb") as fh:
                    fh.write(u.getbuffer())
            found[os.path.splitext(u.name)[0]] = p
        out_default = os.path.join(tmpdir, "Results")
    if not found:
        st.info("Pick a folder or upload CSVs to start.")
        return

    tab = st.data_editor(
        pd.DataFrame({"Use": True, "Sample": list(found), "Label": [E.short_label(k) for k in found], "Response (optional)": "",
                      "File": [os.path.basename(p) for p in found.values()]}),
        column_config={"Use": st.column_config.CheckboxColumn(), "Sample": st.column_config.TextColumn(disabled=True),
                       "File": st.column_config.TextColumn(disabled=True),
                       "Label": st.column_config.TextColumn(help="Short name used in the cohort sheets, e.g. S43"),
                       "Response (optional)": st.column_config.SelectboxColumn(options=["", "R", "NR"], help="R = responder, NR = non-responder")},
        hide_index=True, use_container_width=True, key="samples")
    use = tab[tab["Use"]]
    if use.empty:
        st.warning("Tick at least one sample.")
        return
    response = {r["Sample"]: r["Response (optional)"] for _, r in use.iterrows() if r["Response (optional)"]}
    labels = {r["Sample"]: ((r["Label"] or "").strip() or r["Sample"]) for _, r in use.iterrows()}
    first = found[use.iloc[0]["Sample"]]

    st.header("2 · What we found in the CSV")
    st.caption(f"Read from {use.iloc[0]['Sample']} - the other samples are assumed to use the same names.")
    d = detect_block(first, "an")
    markers, info = d["markers"], d["info"]
    names = list(markers)

    st.subheader("Percentage pairs (optional)")
    st.caption("e.g. Numerator CD8+GzmB+ , Denominator CD8  →  sheet 'CD8+GzmB+ % of CD8' (median and sum).")
    pct_pairs = pair_editor("pct", names, names, ("Numerator", "Denominator"))

    st.header("3 · Spatial analysis (optional)")
    pairs, prox_pairs, thr = [], [], 20.0
    sp = E.spatial_cols(first)                              # header only: which 'distance to annotation with X' columns exist
    ann = sp["ann"]
    if len(sp["candidates"]) > 1:
        ann = st.selectbox("Tumour annotation to use (zones + distance to tumour)", sp["candidates"],
                           index=sp["candidates"].index(sp["ann"]), key="ann")
    sp = E.spatial_cols(first, ann)
    has_sd, has_td = bool(sp["sd"]), bool(sp["td"] or sp["sd"])
    if ann:
        st.caption(f"Tumour annotation: **{ann}** - signed distance {'found' if sp['sd'] else 'NOT found'}, "
                   f"distance {'found' if sp['td'] else ('derived from the signed distance' if sp['sd'] else 'NOT found')}.")
    else:
        dist_cols = [c for c in sp["cols"] if "distance" in c.lower() and "detection" not in c.lower()]
        st.caption("No 'Distance to annotation with <tumour>' column in this CSV" + (f" (only: {', '.join(dist_cols)})." if dist_cols else "."))
    if not info["has_xy"]:
        st.info("This CSV has no Centroid X/Y columns, so distance and proximity are not available.")
    else:
        st.subheader("Distance (cell-cell interaction)")
        st.caption("For every 'From' cell, the distance to the nearest 'To' cell (or to the tumour). Median per explant, then median per arm.")
        pairs = pair_editor("pairs", names, names + (["Tumor"] if has_td else []), ("From", "To"))
        st.subheader("Proximity (cells close to another cell type)")
        thr = st.number_input("Neighbour distance threshold (µm)", 1.0, 500.0, 20.0)
        st.caption("Per zone: how many 'Source' cells have a 'Neighbour' cell within the threshold, as a count and as % of the source cells.")
        prox_pairs = pair_editor("prox", names, names, ("Source", "Neighbour")) if has_sd else []
        if not has_sd:
            st.info("Proximity needs the signed distance to the tumour (for zones).")

    st.header("4 · Run")
    with st.expander("Advanced settings (the defaults are the lab standard)"):
        circ = st.slider("Minimum nucleus circularity (0 = off)", 0.0, 1.0, 0.7, 0.05)
        peri = st.number_input("Peri-tumor zone: up to X µm outside the tumour", 1.0, 500.0, 50.0) if has_sd else 50.0
        cohort_name = st.text_input("Name of the cohort file", "Cohort_Analysis", help="Saved as <name>.xlsx when you analyse more than one sample").strip() or "Cohort_Analysis"
        as_zip = st.checkbox("Always download everything as one .zip", False, help="Otherwise up to 3 files get their own download button")
    if not has_sd:
        st.info("No signed distance to the tumour in this CSV: counts per marker only (no In-Tumor / Peri-Tumor / TILs zones)" + (" - distance to the tumour is still available above." if has_td else "."))

    sig = repr((tab.fillna("").values.tolist(), list(map(str, markers.items())), pct_pairs, pairs, prox_pairs, thr, circ, peri, ann, d["ref"],
                d["n_exp"], d["arm_names"], cohort_name))                 # everything the results depend on
    if st.button("Run analysis", type="primary"):
        out_dir = tempfile.mkdtemp()                     # results are only handed to the browser as downloads
        codes, arm_order = list(d["arm_names"]), list(d["arm_names"].values())
        results, files, bar = {}, [], st.progress(0.0)
        for k, (_, row) in enumerate(use.iterrows()):
            sid = row["Sample"]
            bar.progress(k / len(use), text=f"{sid} ({k + 1}/{len(use)})")
            sp_i = E.spatial_cols(found[sid], ann)
            if bool(sp_i["sd"]) != has_sd:
                st.warning(f"{sid}: {'has no' if has_sd else 'has a'} signed distance to '{ann}' unlike the first sample - zone columns are "
                           f"{'left blank' if has_sd else 'ignored'} for it.")
            df = E.load_cells(found[sid], circ, ann)
            df = df[df["arm"].isin(codes)].assign(arm=lambda x: x["arm"].map(d["arm_names"]))
            cls = E.marker_classes(df["Classification"].unique(), d["rename"], markers)
            counts = E.compute_counts(df, cls, peri, E.ZONES if has_sd else ["Total"])
            inter = E.compute_interactions(df, cls, pairs) if pairs else None
            prox = E.compute_proximity(df, cls, prox_pairs, thr, peri) if prox_pairs else None
            results[sid] = {"counts": counts, "inter": inter, "prox": prox}
            note = (f"circularity > {circ}; peri-tumor <= {peri} um; proximity threshold {thr} um; "
                    "a cell is never its own neighbour; "
                    f"markers: {', '.join(markers)}")
            files.append(E.write_sample_workbook(os.path.join(out_dir, f"{sid}_counts.xlsx"), sid, counts, inter, arm_order,
                                                 d["ref"], d["n_exp"], note, prox))
            miss = [m for m in markers if counts[(m, "Total")].sum() == 0]
            if miss:
                st.warning(f"{sid}: no cells found for {', '.join(miss)}")
        if len(results) > 1:
            files.append(E.write_cohort_workbook(os.path.join(out_dir, f"{cohort_name}.xlsx"), results, arm_order, d["ref"], response, pct_pairs,
                                                labels, {v: k for k, v in d["arm_names"].items()}))
        bar.progress(1.0, text="Done")
        st.session_state["out_sig"] = sig
        st.session_state["out"] = [(os.path.basename(f), open(f, "rb").read()) for f in files]
        shutil.rmtree(out_dir, ignore_errors=True)

    out = st.session_state.get("out")
    if out and st.session_state.get("out_sig") != sig:
        st.warning("You changed something (labels, markers, pairs or settings) after the last run - press **Run analysis** again to get updated files.")
        out = None
    if out:
        st.success(f"Done - {len(out)} file(s) ready.")
        if len(out) <= 3 and not as_zip:                                 # a few files: download each directly; many: one zip
            for name, data in out:
                st.download_button(f"Download {name}", data, name, key=f"dl_{name}",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name, data in out:
                    z.writestr(name, data)
            st.download_button(f"Download all {len(out)} files (.zip)", buf.getvalue(), "mIHC_results.zip", "application/zip", type="primary")


analyze()
