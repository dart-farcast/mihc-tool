"""Coregistration - one page, top to bottom: 1 slides, 2 channels, 3 run (with live progress underneath).
Give two folders (every sample inside is found) or two slide files (one sample): the samples and the channels are detected by
themselves. Runs are background processes (worker.py): closing or refreshing the browser never stops a job, and every job
saves its progress, so after an error you press Resume and only the unfinished work is done."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

import pandas as pd
import streamlit as st

import coreg_core as C
import server_picker

if not os.environ.get("UNIFIED_APP"):                 # the unified app (app.py) sets the page config once
    st.set_page_config(page_title="Coregistration", page_icon="🔬", layout="wide")
RUNS = C.runs_dir()
ICON = {"queued": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "cancelled": "⏹"}


def is_local():
    """Browse dialogs open on the PC that runs the app, so they only make sense when the browser is on that PC."""
    try:
        return st.context.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1", "[::1]")
    except Exception:
        return True


LOCAL = is_local() and not os.environ.get("COREG_NO_BROWSE")      # Docker sets COREG_NO_BROWSE: no desktop to open a dialog on
st.session_state.setdefault("notice", None)
st.session_state.setdefault("current", [])           # the samples that will be coregistered (set while the page is drawn)
st.session_state.setdefault("chan_rows", None)
st.session_state.setdefault("chan_sig", None)


@st.cache_data(ttl=120, show_spinner=False)
def setup_report():
    return C.check_setup()


@st.cache_data(ttl=30, show_spinner="Looking for the samples ...")
def find_samples(p1, p2, pattern):
    return C.resolve_inputs(p1, p2, pattern)


# ── actions (callbacks run before the page is drawn: the only moment session state may be changed) ──
def browse(key, kind, title):
    p = C.pick_path(kind, title)
    if p:
        st.session_state[key] = p


def reread_channels():
    st.session_state.chan_sig = None                  # the page reads them again on the next draw


def launch(spec):
    """Start worker.py in the background for this spec and return the run id."""
    rid = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rd = os.path.join(RUNS, rid)
    os.makedirs(rd)
    with open(os.path.join(rd, "spec.json"), "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=1)
    detached = 0x00000008 if os.name == "nt" else 0                 # DETACHED_PROCESS: survives closing this page
    C.popen([sys.executable, "-u", os.path.join(C.APP_DIR, "worker.py"), "batch", rd], cwd=C.APP_DIR, creationflags=detached,
            stdout=open(os.path.join(rd, "worker.log"), "w"), stderr=subprocess.STDOUT)
    st.session_state.run_pick = rid
    return rid


def start_run():
    s = st.session_state
    samples, errors = s.current, []
    if not samples:
        errors.append("No samples to coregister: enter the slides in step 1.")
    out = (s.get("outdir") or "").strip().strip('"') or s.get("out_default") or ""
    if not out:
        errors.append("No results folder.")
    for p in samples:
        for k in ("slide1", "slide2"):
            if not C.file_ok(p[k]):
                errors.append(f"{p['name']}: file not found: {p[k]}")
    panel = None                                    # None = automatic: every channel under its detected name
    if s.chan_rows is not None:
        try:
            panel = C.panel_from_rows(s.chan_rows.to_dict("records"))
        except (ValueError, KeyError) as e:
            errors.append(f"Channel table: {e}")
    if errors:
        s.notice = ("error", "Cannot start:" + "".join(f"\n- {e}" for e in errors[:15]))
        return
    spec = {"jobs": [{"name": p["name"], "slide1": p["slide1"], "slide2": p["slide2"], "out_dir": out} for p in samples],
            "settings": {"workers": int(s.workers), "non_rigid": bool(s.non_rigid), "crop": s.crop, "panel": panel,
                         "pixel_size_um": float(s.pixel_size) or None, "force": bool(s.force), "save_aligned": bool(s.save_aligned),
                         "rebuild_final": bool(s.rebuild_final)}}
    rid = launch(spec)
    s.notice = ("success", f"Started run {rid} with {len(samples)} sample(s), results in `{out}`. Follow it under step 3.")


def resume_run(rd):
    """New run with only the failed / cancelled jobs of an old run. Finished phases are skipped."""
    with open(os.path.join(rd, "status.json"), encoding="utf-8") as fh:
        stt = json.load(fh)
    with open(os.path.join(rd, "spec.json"), encoding="utf-8") as fh:
        spec = json.load(fh)
    todo = [j for j, sj in zip(spec["jobs"], stt["jobs"]) if sj["status"] in ("failed", "cancelled")]
    rid = launch({"jobs": todo, "settings": {**spec["settings"], "force": False}})
    st.toast(f"Resuming {len(todo)} unfinished job(s) as run {rid}. Finished phases are skipped.")


def stop_run(rd):
    open(os.path.join(rd, "stop.flag"), "w").close()


UPLOADS = os.environ.get("COREG_UPLOAD_DIR") or os.path.join(tempfile.gettempdir(), "coreg_uploads")


def save_upload(key):
    """Uploaded slides go to the server's disk; the path box is then filled with that folder (several files) or file (one)."""
    ups = st.session_state.get(f"up_{key}") or []
    if not ups:
        return
    dest = os.path.join(UPLOADS, st.session_state.setdefault("upid", uuid.uuid4().hex[:8]), key)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    for u in ups:
        with open(os.path.join(dest, os.path.basename(u.name)), "wb") as fh:
            shutil.copyfileobj(u, fh, 1 << 24)
    st.session_state[key] = dest if len(ups) > 1 else os.path.join(dest, os.path.basename(ups[0].name))


def path_input(label, key, folder_only=False):
    c = st.columns([8, 1.2, 1.2], vertical_alignment="bottom")
    c[0].text_input(label, key=key)
    if LOCAL:                                         # on the PC that runs the app: the normal Windows dialog
        c[1].button("Folder", key=f"bf_{key}", on_click=browse, args=(key, "dir", label))
        if not folder_only:
            c[2].button("File", key=f"bi_{key}", on_click=browse, args=(key, "file", label))
    else:                                             # server / Docker: browse the server's folders inside the page
        c[1].button("Folder", key=f"bf_{key}", on_click=server_picker.pick, args=(key, "dir", label))
        if not folder_only:
            c[2].button("File", key=f"bi_{key}", on_click=server_picker.pick, args=(key, "file", label, C.SLIDE_EXTS))
    if not LOCAL and not folder_only:                 # website: upload the slides from the user's PC (Explorer window)
        st.file_uploader(f"...or upload from your PC: {label}", accept_multiple_files=True, key=f"up_{key}",
                         type=[e.lstrip(".") for e in C.SLIDE_EXTS], on_change=save_upload, args=(key,),
                         help="Pick the slide file(s) in the Windows window. Large slides take a while to upload.")


def tail(path, n=60, size=200_000):
    """Last lines of a log. VALIS prints its progress bar as hundreds of lines: only the newest bar line is kept."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - size))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    bar = lambda l: l.startswith("[=") or "|#" in l or l.startswith(("Extract", "Download"))
    keep = [l for l in lines if not bar(l)][-n:]
    last_bar = next((l for l in reversed(lines) if bar(l)), None)
    return "\n".join(keep + ([last_bar] if last_bar else []))


# ── page ─────────────────────────────────────────────────────────────────────
s = st.session_state
st.title("🔬 Coregistration")
st.caption("Two scans of the same section  →  registered  →  one multi-channel image for QuPath.  Three steps, top to bottom.")
if s.notice:
    getattr(st, s.notice[0])(s.notice[1])
    s.notice = None

report = setup_report()
with st.expander("Setup check" + ("" if all(ok for _, ok, _ in report) else "  -  something is missing"),
                 expanded=not all(ok for _, ok, _ in report)):
    for what, ok, msg in report:
        st.markdown(f"{'✅' if ok else '❌'} **{what}**: {msg}")

st.header("1 · Slides")
st.caption("Give the first scans (slide 1) and the second scans (slide 2) as **two folders** (every sample inside is found and paired by "
           "the explant code in the file names, e.g. LA1, LC3) or as **two slide files** (one sample). Everything is detected by itself.")
path_input("Slide 1 - folder or file", "src1")
path_input("Slide 2 - folder or file", "src2")
path_input("Results folder (leave empty: next to the slides)", "outdir", folder_only=True)

found = find_samples(s.get("src1", ""), s.get("src2", ""), s.get("pair_pattern") or C.DEFAULT_PAIR_PATTERN)
pairs = found["pairs"]
s.out_default = found["out_default"]
if found["error"]:
    st.error(found["error"])
elif not pairs and not (s.get("src1") and s.get("src2")):
    st.info("Enter slide 1 and slide 2 and the samples appear here.")
elif not pairs:
    st.warning("No samples found - check the folders and the pairing pattern under Advanced.")
for w in found["warnings"]:
    st.warning(w)

chosen = pairs
if pairs:
    names = [p["name"] for p in pairs]
    if len(pairs) == 1:
        st.success(f"Found 1 sample: **{names[0]}**")
    else:
        st.success(f"Found {len(pairs)} samples: {', '.join(names)}")
        sig = hash(tuple(names))
        mode = st.radio("What do you want to coregister?", [f"Complete folder (all {len(pairs)} samples)", "Choose samples"],
                        horizontal=True, key=f"mode_{sig}")
        if mode == "Choose samples":
            picked = st.multiselect("Samples", names, default=names[:1], key=f"picked_{sig}")
            chosen = [p for p in pairs if p["name"] in picked]
    st.caption(f"Will be coregistered: {', '.join(p['name'] for p in chosen) or 'nothing selected'}"
               f"   ·   results in `{(s.get('outdir') or '').strip() or found['out_default']}`")
s.current = chosen

# channels are read from the first sample as soon as it is known (the panel is the same for all samples)
chan_sig = (pairs[0]["slide1"], pairs[0]["slide2"]) if pairs else None
if chan_sig != s.chan_sig:
    s.chan_sig, s.chan_rows, chan_err = chan_sig, None, None
    s.pop("chan_ed", None)
    if chan_sig:
        try:
            i1, i2 = C.list_channels(chan_sig[0]), C.list_channels(chan_sig[1])
            s.chan_rows = pd.DataFrame(C.default_rows(i1["channels"], i2["channels"]))
        except Exception as e:
            chan_err = f"Could not read the channels from the slides: {e}"
    s.chan_err = chan_err

st.header("2 · Channels")
st.caption("The channels are read from the slides. Type the name you want for each one. The same name on both slides means they are "
           "averaged into one channel (use it for DAPI, or for a marker stained on both slides). Untick Keep to leave one out. "
           "You can rename later and rebuild only the final image.")
if s.get("chan_err"):
    st.error(s.chan_err)
if s.chan_rows is None:
    st.info("The channels appear here once the slides are known. If you skip the naming, every channel keeps the name found in the slide.")
else:
    s.chan_rows = st.data_editor(
        s.chan_rows, hide_index=True, use_container_width=True, key="chan_ed",
        disabled=["Slide", "Channel", "Dye", "Emission (nm)"],
        column_order=["Keep", "Slide", "Channel", "Final name", "Order", "Dye", "Emission (nm)"],
        column_config={"Order": st.column_config.NumberColumn(help="Position in the final image (smaller = earlier)")})
    try:
        panel = C.panel_from_rows(s.chan_rows.to_dict("records"))
        st.caption("Final image, in order:  " + "  ·  ".join(p["name"] + (f" (average of {len(p['sources'])})" if len(p["sources"]) > 1 else "")
                                                           for p in panel))
    except (ValueError, KeyError) as e:
        st.warning(str(e))
    st.button("Read the channels again", on_click=reread_channels)

st.header("3 · Run")
st.button("🚀 Start", type="primary", on_click=start_run, disabled=not chosen)
with st.expander("Advanced (the defaults are the lab standard)"):
    c1, c2, c3 = st.columns(3)
    c1.number_input("Jobs at the same time", 1, 8, C.suggest_workers(), key="workers",
                    help="Each job runs its own VALIS and needs a lot of RAM (about 24 GB). Suggested from this PC's memory.")
    c1.text_input("Pairing pattern (regular expression)", C.DEFAULT_PAIR_PATTERN, key="pair_pattern",
                  help="The part of the file name that identifies the explant. Files of the two folders with the same match are paired.")
    c2.checkbox("Redo everything (ignore saved progress)", False, key="force",
                help="Normally a re-run skips the phases that already finished.")
    c2.checkbox("Rebuild only the final image", False, key="rebuild_final",
                help="Not needed after renaming channels - that is noticed automatically.")
    c2.checkbox("Also save aligned slides (QC copies, slow)", False, key="save_aligned",
                help="Not needed for the final image. Adds about 15 minutes per explant.")
    c3.selectbox("Crop", ["auto", "overlap", "all"], key="crop", help="auto = 'overlap', and 'all' when VALIS reports 'bad extract area'")
    c3.checkbox("Non-rigid registration (can distort cells - leave off)", False, key="non_rigid")
    c3.number_input("Pixel size (µm per pixel), 0 = read from the slides", 0.0, 10.0, 0.0, 0.001, format="%.4f", key="pixel_size")


@st.fragment(run_every=5)
def progress():
    runs = sorted((d for d in os.listdir(RUNS) if os.path.isfile(os.path.join(RUNS, d, "status.json"))), reverse=True)
    if not runs:
        st.caption("Progress appears here once you start a run.")
        return
    if st.session_state.get("run_pick") not in runs:         # e.g. a run that has just been started and has no status yet
        st.session_state.run_pick = runs[0]
    rid = st.session_state.run_pick if len(runs) == 1 else st.selectbox("Run", runs, key="run_pick")
    rd = os.path.join(RUNS, rid)
    try:
        with open(os.path.join(rd, "status.json"), encoding="utf-8") as fh:
            stt = json.load(fh)
    except (OSError, ValueError):
        st.caption("Updating ...")
        return
    counts = pd.Series([j["status"] for j in stt["jobs"]]).value_counts()
    st.markdown(f"**{stt['state'].capitalize()}** · started {stt['started']}" + (f" · ended {stt['ended']}" if stt["ended"] else "")
                + " · " + ", ".join(f"{ICON.get(k, '')} {v} {k}" for k, v in counts.items()))
    st.dataframe(pd.DataFrame([{"": ICON.get(j["status"], ""), "Name": j["name"], "Status": j["status"],
                                "Progress": " · ".join(f"{k} {'✅' if v else '⬜'}" for k, v in C.phase_status(j).items()),
                                "Result": j["output"] or j["error"]} for j in stt["jobs"]]), hide_index=True, use_container_width=True)
    if stt["state"] == "running":
        st.button("⏹ Stop", on_click=stop_run, args=(rd,), help="Kills running jobs and cancels the queued ones.")
    elif any(j["status"] in ("failed", "cancelled") for j in stt["jobs"]):
        st.button("▶ Resume unfinished jobs", on_click=resume_run, args=(rd,), type="primary",
                  help="Starts the failed / cancelled jobs again. Phases that already finished are not repeated.")
    if not LOCAL:                                            # website: the results are on the server, so offer them for download
        for j in stt["jobs"]:
            f = os.path.join(j["output"] or "", f"{j['name']}_merged_avg.ome.tiff")        # the final image of the job (output = its VALIS folder)
            if j["status"] == "done" and os.path.isfile(f):
                with open(f, "rb") as fh:
                    st.download_button(f"⬇ Download {j['name']} ({os.path.getsize(f) / 1e9:.1f} GB)", fh,
                                       file_name=os.path.basename(f), key=f"dl_{rid}_{j['name']}")
    names = [j["name"] for j in stt["jobs"]]
    pick = st.selectbox("Log of", range(len(names)), key=f"log_{rid}", format_func=lambda i: f"{ICON.get(stt['jobs'][i]['status'], '')} {names[i]}",
                        index=next((i for i, j in enumerate(stt["jobs"]) if j["status"] in ("running", "failed")), 0))
    st.code(tail(os.path.join(rd, stt["jobs"][pick]["log"])) or "(no output yet)", language=None)


progress()
