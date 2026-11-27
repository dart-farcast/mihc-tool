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
import streamlit.components.v1 as components

import coreg_core as C

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
CHUNKED = os.environ.get("COREG_CHUNKED_UPLOAD") == "1"


def sync_chunked_upload(key):
    """Fill the path box from files uploaded via the chunked upload server."""
    upid = st.session_state.setdefault("upid", uuid.uuid4().hex[:8])
    dest = os.path.join(UPLOADS, upid, key)
    if not os.path.isdir(dest):
        return
    files = sorted(f for f in os.listdir(dest) if os.path.isfile(os.path.join(dest, f)) and not f.endswith(".part"))
    if len(files) == 1:
        st.session_state[key] = os.path.join(dest, files[0])
    elif len(files) > 1:
        st.session_state[key] = dest


def _chunked_uploader_widget(key, btn_label, exts):
    upid = st.session_state.setdefault("upid", uuid.uuid4().hex[:8])
    accept = ",".join(exts)
    html = f"""
    <div style="font-family:sans-serif;font-size:13px;padding:6px 10px;border:1px solid #d0d7de;border-radius:8px;background:#f8fafc;">
      <label style="font-weight:600;display:block;margin-bottom:6px;">Upload from your PC (supports 5 GB+ files)</label>
      <input id="f_{key}" type="file" multiple accept="{accept}" style="width:100%;margin-bottom:6px;" />
      <div id="pwrap_{key}" style="display:none;margin-top:4px;">
        <div style="background:#e2e8f0;border-radius:4px;height:8px;overflow:hidden;">
          <div id="pbar_{key}" style="background:#2563eb;height:100%;width:0%;transition:width 0.15s;"></div>
        </div>
        <div id="pmsg_{key}" style="margin-top:4px;color:#334155;font-size:12px;"></div>
      </div>
    </div>
    <script>
    (function() {{
      const input = document.getElementById("f_{key}");
      const wrap = document.getElementById("pwrap_{key}");
      const bar = document.getElementById("pbar_{key}");
      const msg = document.getElementById("pmsg_{key}");
      const CHUNK = 20 * 1024 * 1024;
      const UPID = {json.dumps(upid)};
      const SLOT = {json.dumps(key)};
      const BTN = {json.dumps(btn_label)};

      function fmtBytes(b) {{
        if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
        if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
        return Math.round(b / 1e3) + " KB";
      }}

      async function sendChunk(url, headers, blob) {{
        for (let attempt = 0; attempt < 4; attempt++) {{
          try {{
            const r = await fetch(url, {{ method: "POST", headers, body: blob }});
            if (r.ok) return await r.json();
          }} catch (e) {{}}
          await new Promise(res => setTimeout(res, 1000 * (attempt + 1)));
        }}
        throw new Error("Chunk upload failed after retries");
      }}

      input.addEventListener("change", async () => {{
        const files = Array.from(input.files || []);
        if (!files.length) return;
        wrap.style.display = "block";
        input.disabled = true;
        try {{
          msg.textContent = "Preparing upload...";
          await fetch(`/upload/clear?upload_id=${{encodeURIComponent(UPID)}}&slot=${{encodeURIComponent(SLOT)}}`, {{ method: "POST" }});
          const totalAll = files.reduce((a, f) => a + f.size, 0);
          let sentAll = 0;
          const t0 = Date.now();
          for (let fi = 0; fi < files.length; fi++) {{
            const file = files[fi];
            const total = file.size;
            let offset = 0;
            while (offset < total || total === 0) {{
              const end = Math.min(offset + CHUNK, total);
              const blob = file.slice(offset, end);
              const done = end >= total ? "1" : "0";
              await sendChunk("/upload/chunk", {{
                "X-Upload-Id": UPID,
                "X-Slot": SLOT,
                "X-Filename": file.name,
                "X-Offset": String(offset),
                "X-Total-Size": String(total),
                "X-Done": done
              }}, blob);
              sentAll += (end - offset);
              offset = end;
              const pct = totalAll > 0 ? Math.min(100, Math.round(sentAll * 100 / totalAll)) : 100;
              const elapsed = Math.max(0.1, (Date.now() - t0) / 1000);
              const mbps = (sentAll / 1e6 / elapsed).toFixed(1);
              bar.style.width = pct + "%";
              msg.textContent = `(${{fi + 1}}/${{files.length}}) ${{file.name}}: ${{pct}}% · ${{fmtBytes(sentAll)}} / ${{fmtBytes(totalAll)}} · ${{mbps}} MB/s`;
              if (total === 0) break;
            }}
          }}
          bar.style.background = "#16a34a";
          msg.textContent = `Uploaded ${{files.length}} file(s) (${{fmtBytes(totalAll)}}). Loading into app...`;
          for (const b of window.parent.document.querySelectorAll("button")) {{
            if (b.innerText && b.innerText.includes(BTN)) {{
              b.click();
              break;
            }}
          }}
        }} catch (err) {{
          bar.style.background = "#dc2626";
          msg.textContent = "Upload error: " + err.message;
        }} finally {{
          input.disabled = false;
        }}
      }});
    }})();
    </script>
    """
    components.html(html, height=96)


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
    if LOCAL:                                         # on the PC that runs the app: the normal Windows dialog
        c = st.columns([8, 1.2, 1.2], vertical_alignment="bottom")
        c[0].text_input(label, key=key)
        c[1].button("Folder", key=f"bf_{key}", on_click=browse, args=(key, "dir", label))
        if not folder_only:
            c[2].button("File", key=f"bi_{key}", on_click=browse, args=(key, "file", label))
    elif folder_only:                                 # website: results folder on the server
        st.text_input(label, key=key)
    else:                                             # website: path box on the left, upload from the PC on the right
        c = st.columns([5, 6], vertical_alignment="top")
        c[0].text_input(label, key=key, help="Filled in by itself after an upload. You can also type a path on the server.")
        if CHUNKED:
            with c[1]:
                btn_label = f"Load uploaded ({key})"
                _chunked_uploader_widget(key, btn_label, C.SLIDE_EXTS)
                st.button(btn_label, key=f"sync_{key}", on_click=sync_chunked_upload, args=(key,))
        else:
            c[1].file_uploader("Upload from your PC", accept_multiple_files=True, key=f"up_{key}", on_change=save_upload, args=(key,),
                               type=[e.lstrip(".") for e in C.SLIDE_EXTS],
                               help="Opens the Windows window: pick one slide, or several to pair by explant code. Large slides take a while.")


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
if LOCAL:
    path_input("Results folder (leave empty: next to the slides)", "outdir", folder_only=True)
else:
    st.caption("Results are saved on the server next to the slides. When a job is done, a **Download** button appears under *Progress*.")

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
