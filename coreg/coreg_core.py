"""Core of the coregistration pipeline (no UI). Used by pipeline_runner.py, batch_run.py and streamlit_app.py.

One job = one explant (e.g. LA2):  VALIS registration of Slide I + Slide II  ->  merged 10-channel OME-TIFF
           -> average DAPI / PanCK, reorder channels  ->  <name>_merged_avg.ome.tiff (pyramidal, for QuPath)
The old separate 'channel split' step is gone: channels are read straight from the VALIS output page by page.
"""
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

APP_DIR = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)       # Windows-only flag, 0 elsewhere
MIN_BYTES = 10 * 1024 * 1024                                   # smaller than this = not a finished image

# Folders the old scripts put on PATH (Java for Bio-Formats, libvips). Only added when they exist on this machine.
LEGACY_BIN_DIRS = [
    r"\\172.16.38.21\desktop\imaging\Desktop\COREGISTRATION\Fiji.app\java\win64\zulu8.60.0.21-ca-fx-jdk8.0.322-win_x64\jre\bin",
    r"C:\Users\imaging\Downloads\vips-dev-w64-all-8.17.3\vips-dev-8.17\bin",
]


# ── setup check / sensible defaults ──────────────────────────────────────────
def total_ram_gb():
    try:
        if os.name == "nt":
            import ctypes

            class MEM(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                            ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]
            m = MEM()
            m.l = ctypes.sizeof(MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.total / 2 ** 30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except Exception:
        return 16.0


def suggest_workers():
    """Each VALIS job needs roughly 24 GB of RAM for whole-slide images: 1 job per 24 GB, at most 4."""
    return int(max(1, min(4, total_ram_gb() // 24)))


def check_setup():
    """[(what, ok, message)] - what has to be installed for a run. Nothing heavy is imported."""
    import importlib.util
    out = []
    for mod, pip in (("valis", "valis-wsi"), ("tifffile", "tifffile"), ("jpype", "jpype1")):
        found = importlib.util.find_spec(mod) is not None
        out.append((mod, found, "installed" if found else f"missing - run:  pip install {pip}"))
    try:
        import pyvips
        out.append(("pyvips / libvips", True, f"libvips {pyvips.version(0)}.{pyvips.version(1)}"))
    except Exception as e:
        out.append(("pyvips / libvips", False, f"not working ({str(e).splitlines()[0][:90]}) - install pyvips and libvips"))
    env = child_env()
    java = env.get("JAVA_HOME") or next((d for d in env.get("PATH", "").split(os.pathsep)
                                         if os.path.isfile(os.path.join(d, "java.exe")) or os.path.isfile(os.path.join(d, "java"))), "")
    out.append(("Java", True, f"found: {java}" if java else "not found - VALIS downloads its own Java the first time (internet needed once)"))
    out.append(("Memory", True, f"{total_ram_gb():.0f} GB RAM, so {suggest_workers()} job(s) at a time is suggested"))
    return out


def default_output(folder1):
    """Next to the slides, unless the user types another place."""
    return os.path.join(os.path.dirname(os.path.abspath(folder1.rstrip("\\/"))), "Coregistration")


# ── settings ─────────────────────────────────────────────────────────────────
def load_config():
    """Optional config.json next to the app: {"bin_dirs": [...], "runs_dir": "..."}."""
    try:
        with open(os.path.join(APP_DIR, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def runs_dir():
    d = os.environ.get("COREG_RUNS_DIR") or load_config().get("runs_dir") or os.path.join(APP_DIR, "runs")
    os.makedirs(d, exist_ok=True)
    return d


def child_env():
    """Environment for child processes: Java / libvips folders on PATH (COREG_BIN_DIRS, config.json "bin_dirs", or the old
    imaging-PC folders when they exist) and JAVA_HOME derived from the Java found, because JPype (used by VALIS) looks there."""
    extra = [d for d in os.environ.get("COREG_BIN_DIRS", "").split(os.pathsep) if d]
    dirs = [d for d in extra + load_config().get("bin_dirs", []) + LEGACY_BIN_DIRS if os.path.isdir(d)]
    env = os.environ.copy()
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    for d in dirs:
        if os.path.isfile(os.path.join(d, "java.exe")) or os.path.isfile(os.path.join(d, "java")):
            env.setdefault("JAVA_HOME", os.path.dirname(d))
            break
    return env


# ── files ────────────────────────────────────────────────────────────────────
def file_ok(path, min_bytes=1024):
    """True if the file exists and is big enough. os.stat (not isfile) because isfile can say False on SMB shares; falls back to
    a directory listing for WinError 1816 (not enough quota)."""
    try:
        return os.stat(path).st_size >= min_bytes
    except OSError:
        try:
            d, f = os.path.split(path)
            return f in os.listdir(d)
        except OSError:
            return False


DEFAULT_PAIR_PATTERN = r"(?<![A-Za-z0-9])(L[A-Z]\d+)(?!\d)"     # ..._LA2_...  -> LA2 (editable in the app)
SLIDE_EXTS = (".czi", ".tif", ".tiff")


def _natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def pair_slides(dir1, dir2, pattern=DEFAULT_PAIR_PATTERN):
    """Pair the slide files of two folders by the part of the file name that `pattern` matches (group 1 if the pattern has a
    group, else the whole match), e.g. LA1 or LC3. Returns (pairs, warnings). If a code occurs twice in a folder (e.g. a rescan)
    the newest file is used and a warning is returned."""
    rx = re.compile(pattern)

    def scan(d):
        found, warn = {}, []
        for f in sorted(os.listdir(d)):
            m = rx.search(f)
            if not (f.lower().endswith(SLIDE_EXTS) and m):
                continue
            p = os.path.join(d, f)
            code = m.group(1) if rx.groups else m.group(0)
            if code in found:
                newest = max(found[code], p, key=os.path.getmtime)
                warn.append(f"{os.path.basename(d)}: {code} appears twice, using the newest ({os.path.basename(newest)})")
                found[code] = newest
            else:
                found[code] = p
        return found, warn
    a, wa = scan(dir1)
    b, wb = scan(dir2)
    warnings = wa + wb
    warnings += [f"{c}: only in the first folder" for c in sorted(set(a) - set(b), key=_natural)]
    warnings += [f"{c}: only in the second folder" for c in sorted(set(b) - set(a), key=_natural)]
    pairs = [{"name": c, "slide1": a[c], "slide2": b[c]} for c in sorted(set(a) & set(b), key=_natural)]
    if not pairs and not warnings:
        warnings.append("no slide files matched the pairing pattern - check the pattern and the folders")
    return pairs, warnings


def resolve_inputs(p1, p2, pattern=None):
    """What did the user enter? Two folders -> every sample found in them (paired by explant code); two slide files -> that one
    sample. Returns {'pairs', 'warnings', 'error', 'out_default'}; nothing is raised, the message is for the page."""
    pattern = pattern or DEFAULT_PAIR_PATTERN
    p1, p2 = (p1 or "").strip().strip('"'), (p2 or "").strip().strip('"')
    res = {"pairs": [], "warnings": [], "error": None, "out_default": None}
    if not (p1 and p2):
        return res
    for label, p in (("Slide 1", p1), ("Slide 2", p2)):
        if not os.path.exists(p):
            res["error"] = f"{label} not found: {p}"
            return res
    d1, d2 = os.path.isdir(p1), os.path.isdir(p2)
    if d1 != d2:
        res["error"] = "Give either two folders (every sample inside is used) or two slide files (one sample), not one of each."
        return res
    try:
        rx = re.compile(pattern)
    except re.error as e:
        res["error"] = f"The pairing pattern is not valid: {e}"
        return res
    if d1:
        res["pairs"], res["warnings"] = pair_slides(p1, p2, pattern)
        res["out_default"] = default_output(p1)
    else:
        for p in (p1, p2):
            if not p.lower().endswith(SLIDE_EXTS):
                res["error"] = f"Not a slide file ({', '.join(SLIDE_EXTS)}): {os.path.basename(p)}"
                return res
        m = rx.search(os.path.basename(p1))
        name = (m.group(1) if rx.groups else m.group(0)) if m else os.path.splitext(os.path.basename(p1))[0]
        res["pairs"] = [{"name": name, "slide1": p1, "slide2": p2}]
        res["out_default"] = default_output(os.path.dirname(p1))
    return res


# ── process helpers ──────────────────────────────────────────────────────────
def popen(cmd, **kw):
    """Child process in its own process group (so a whole job tree can be killed), no console window on Windows."""
    if os.name == "nt":
        kw["creationflags"] = kw.get("creationflags", 0) | NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, env=child_env(), **kw)


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def run_streaming(cmd, log, cwd=None):
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=cwd)
    for line in iter(proc.stdout.readline, ""):
        log(line.rstrip())
    proc.stdout.close()
    return proc.wait()


def pick_path(kind, title):
    """Native file/folder dialog. Opens on the PC the app runs on, so it is only useful when the browser is on that PC."""
    try:
        out = subprocess.run([sys.executable, os.path.join(APP_DIR, "file_picker.py"), kind, title], capture_output=True,
                             text=True, creationflags=NO_WINDOW, timeout=600)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# ── OME-TIFF ─────────────────────────────────────────────────────────────────
_UNIT_UM = {"µm": 1.0, "um": 1.0, "nm": 1e-3, "mm": 1e3, "m": 1e6, "Å": 1e-4}
_PTYPE = {"uchar": "uint8", "char": "int8", "ushort": "uint16", "short": "int16", "uint": "uint32", "int": "int32", "float": "float"}


def pixel_size_from_ome(xml_text):
    """(x, y) pixel size in micrometres from an OME-XML string, or None."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError):
        return None
    for el in root.iter():
        if el.tag.split("}")[-1] == "Pixels" and el.get("PhysicalSizeX") and el.get("PhysicalSizeY"):
            f = _UNIT_UM.get(el.get("PhysicalSizeXUnit", "µm"), 1.0)
            return float(el.get("PhysicalSizeX")) * f, float(el.get("PhysicalSizeY")) * f
    return None


def build_ome_xml(width, height, channel_names, name="merged", pixel_size_um=None, ptype="uint16"):
    """Minimal OME-XML, strictly ASCII (the micro sign is written as a character reference)."""
    esc = lambda s: str(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    phys = ""
    if pixel_size_um:
        phys = (f' PhysicalSizeX="{pixel_size_um[0]:.6g}" PhysicalSizeXUnit="&#181;m"'
                f' PhysicalSizeY="{pixel_size_um[1]:.6g}" PhysicalSizeYUnit="&#181;m"')
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">',
             f'  <Image ID="Image:0" Name="{esc(name)}">',
             f'    <Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="{ptype}" SizeX="{width}" SizeY="{height}"'
             f' SizeC="{len(channel_names)}" SizeZ="1" SizeT="1"{phys}>']
    for i, n in enumerate(channel_names):
        lines.append(f'      <Channel ID="Channel:0:{i}" Name="{esc(n)}" SamplesPerPixel="1"/>')
        lines.append(f'      <TiffData FirstC="{i}" FirstZ="0" FirstT="0" IFD="{i}" PlaneCount="1"/>')
    lines += ["    </Pixels>", "  </Image>", "</OME>"]
    return "\n".join(lines).encode("ascii", errors="replace").decode("ascii")


def merge_step(merged_file, avg_file, panel, name, pixel_size_um=None, log=print):
    """Read the channels of the VALIS merged OME-TIFF page by page (streaming, no big temp files), average the multi-page
    channels, and write the final pyramidal OME-TIFF. Written under a .partial name and renamed when complete."""
    import pyvips
    import tifffile
    need = max(p for ch in panel for p in ch["pages"]) + 1
    with tifffile.TiffFile(merged_file) as tf:
        n_pages = len(tf.pages)
        found_px = pixel_size_from_ome(tf.ome_metadata) if tf.is_ome else None
    if n_pages < need:
        raise RuntimeError(f"{os.path.basename(merged_file)} has {n_pages} channels but the panel needs {need}. The file is "
                           f"truncated (still being written?) or the panel does not match this run.")
    px = pixel_size_um or found_px
    log(f"[MERGE] {n_pages} channels in the VALIS output; pixel size: "
        f"{'%.4g x %.4g um' % px if px else 'NOT FOUND (set it in QuPath, or enter it in the app)'}")

    def page(i):
        return pyvips.Image.new_from_file(merged_file, page=i, access="sequential")

    chans = []
    for ch in panel:
        imgs = [page(i) for i in ch["pages"]]
        if len(imgs) == 1:
            chans.append(imgs[0])
        else:
            total = imgs[0].cast("uint")
            for im in imgs[1:]:
                total = total + im.cast("uint")
            chans.append((total / len(imgs)).cast(imgs[0].format))
        log(f"[MERGE] {ch['name']}: pages {ch['pages']}" + (" (averaged)" if len(imgs) > 1 else ""))
    w, h = chans[0].width, chans[0].height
    stacked = pyvips.Image.arrayjoin(chans, across=1)
    xml = build_ome_xml(w, h, [c["name"] for c in panel], name, px, _PTYPE.get(chans[0].format, "uint16"))
    stacked.set_type(pyvips.GValue.gstr_type, "image-description", xml)

    tmp = avg_file + ".partial"
    stacked.tiffsave(tmp, compression="lzw", bigtiff=True, tile=True, tile_width=512, tile_height=512, pyramid=True, page_height=h)
    if not file_ok(tmp, MIN_BYTES // 100):
        raise RuntimeError(f"Merge produced an empty file: {tmp}")
    os.replace(tmp, avg_file)
    log(f"[MERGE] Done: {avg_file} ({os.path.getsize(avg_file) // 1024 // 1024} MB)")
    return avg_file


# ── channels: detected from the slides, named by the user ───────────────────
def read_czi_channels(path):
    """Channel names, dyes, wavelengths and pixel size from a .czi file. Only the metadata XML is read (a few hundred KB at a
    known offset), so it takes milliseconds even for a multi-GB slide."""
    import struct
    with open(path, "rb") as fh:
        head = fh.read(160)
        if head[:10] != b"ZISRAWFILE":
            raise ValueError(f"not a CZI file: {path}")
        meta_pos = struct.unpack_from("<q", head, 32 + 60)[0]                    # FileHeader.MetadataPosition
        fh.seek(meta_pos)
        seg = fh.read(40)
        if seg[:14] != b"ZISRAWMETADATA":
            raise ValueError(f"CZI metadata segment not found in {path}")
        xml_size = struct.unpack_from("<i", seg, 32)[0]
        fh.seek(meta_pos + 32 + 256)
        root = ET.fromstring(fh.read(xml_size).decode("utf-8", errors="replace"))
    chans = root.find(".//Information/Image/Dimensions/Channels")
    out = []
    for c in (chans if chans is not None else []):
        out.append({"name": c.get("Name") or c.get("Id") or "", "dye": c.findtext("Fluor") or c.findtext("DyeName") or "",
                    "emission": c.findtext("EmissionWavelength") or "", "excitation": c.findtext("ExcitationWavelength") or ""})
    px = root.findtext(".//Scaling/Items/Distance[@Id='X']/Value")
    return {"channels": out, "pixel_um": float(px) * 1e6 if px else None}


def list_channels(path):
    """Channels of a slide file: .czi, or an (OME-)TIFF with channel names in its OME-XML."""
    if path.lower().endswith(".czi"):
        return read_czi_channels(path)
    import tifffile
    with tifffile.TiffFile(path) as tf:
        xml = tf.ome_metadata if tf.is_ome else ""
    names = re.findall(r'<Channel[^>]*\sName="([^"]*)"', xml or "")
    if not names:
        raise ValueError(f"no channel names found in {path}")
    return {"channels": [{"name": n, "dye": "", "emission": "", "excitation": ""} for n in names], "pixel_um": pixel_size_from_ome(xml or "")}


def _is_nuclear(ch):
    text = f"{ch.get('name', '')} {ch.get('dye', '')}".lower()
    try:
        em = float(ch.get("emission") or 0)
    except ValueError:
        em = 0
    return any(k in text for k in ("dapi", "hoechst", "nuclear")) or (0 < em <= 480)


def default_rows(chans1, chans2):
    """Starting table: every channel of both slides. The final name is the detected name, except that the nuclear stain gets one
    shared name (so it is averaged) and a name that occurs on both slides is made slide-specific, because the same dye is
    usually a different marker on the second slide. The user then types the real names."""
    both = {c["name"] for c in chans1} & {c["name"] for c in chans2}
    rows, order = [], 1
    for slide, chans in ((1, chans1), (2, chans2)):
        for c in chans:
            final = "DAPI" if _is_nuclear(c) else (f"{c['name']} (slide {slide})" if c["name"] in both else c["name"])
            rows.append({"Keep": True, "Slide": slide, "Channel": c["name"], "Dye": c.get("dye", ""),
                         "Emission (nm)": c.get("emission", ""), "Final name": final, "Order": order})
            order += 1
    return rows


def panel_from_rows(rows):
    """Table rows -> panel: [{'name', 'sources': [{'slide', 'channel'}]}]. Rows with the same final name are averaged into one
    channel; the output order follows the 'Order' column (a merged channel takes its first row's position)."""
    kept = [r for r in rows if r.get("Keep", True)]
    if not kept:
        raise ValueError("no channel is selected")
    groups = {}
    for r in sorted(kept, key=lambda r: float(r.get("Order", 0))):
        name = str(r.get("Final name", "")).strip()
        if not name:
            raise ValueError(f"channel '{r.get('Channel')}' (slide {r.get('Slide')}) has no final name - type one or untick Keep")
        groups.setdefault(name, []).append({"slide": int(r["Slide"]), "channel": str(r["Channel"])})
    return [{"name": n, "sources": s} for n, s in groups.items()]


def _split_merged_name(label):
    """VALIS names merged channels 'CHANNEL (SLIDE FILE NAME)'."""
    name, sep, stem = str(label).rpartition(" (")
    return (name, stem.rstrip(")")) if sep else (str(label), "")


def resolve_panel(panel, merged_names, slide1, slide2):
    """Pages of the VALIS merged image for every source of the panel. Channels are found by slide + name, not by a fixed page
    number, so it does not matter which slide VALIS put first. A source may also be given directly as {'pages': [...]}."""
    stem = lambda p: os.path.splitext(os.path.basename(p))[0]
    parsed = [_split_merged_name(n) for n in merged_names]
    slide_of = {stem(slide1): 1, stem(slide2): 2}
    if not all(s in slide_of for _, s in parsed):                                    # names carry no slide: VALIS sorts alphabetically
        first = sorted([stem(slide1), stem(slide2)])[0]
        per = len(parsed) // 2
        slide_of = {stem(slide1): 1 if first == stem(slide1) else 2, stem(slide2): 1 if first == stem(slide2) else 2}
        parsed = [(n, first if i < per else (stem(slide2) if first == stem(slide1) else stem(slide1))) for i, (n, _) in enumerate(parsed)]
    out = []
    for ch in panel:
        pages = list(ch.get("pages", []))
        for src in ch.get("sources", []):
            hits = [i for i, (n, s) in enumerate(parsed) if n == src["channel"] and slide_of.get(s) == src["slide"]]
            if len(hits) != 1:
                have = ", ".join(f"slide {slide_of.get(s, '?')}: {n}" for n, s in parsed)
                raise RuntimeError(f"channel '{src['channel']}' of slide {src['slide']} (for '{ch['name']}') was found {len(hits)} times in the "
                                   f"VALIS output. Available: {have}")
            pages.append(hits[0])
        out.append({"name": ch["name"], "pages": pages})
    return out


def auto_panel(merged_names, slide1, slide2):
    """No names given: keep every channel under its detected name (nuclear stain averaged, duplicate names made slide-specific)."""
    stem = lambda p: os.path.splitext(os.path.basename(p))[0]
    parsed = [_split_merged_name(n) for n in merged_names]
    c1 = [{"name": n} for n, s in parsed if s == stem(slide1)]
    c2 = [{"name": n} for n, s in parsed if s == stem(slide2)]
    if not (c1 or c2):
        c1 = [{"name": n} for n, _ in parsed]
    return panel_from_rows(default_rows(c1, c2))


def merged_channel_names(merged_file):
    import tifffile
    with tifffile.TiffFile(merged_file) as tf:
        return re.findall(r'<Channel[^>]*\sName="([^"]*)"', tf.ome_metadata if tf.is_ome else "")


# ── checkpoints ──────────────────────────────────────────────────────────────
class Checkpoint:
    """<VALIS_name_FINAL>/checkpoint.json records which phases of a job are finished and which files each produced.
    A phase only counts as done while all of its files still exist at (at least) the recorded size, so a truncated file never
    passes as finished work. A re-run skips finished phases and continues at the first unfinished one.
    Phases: register (VALIS registration, saved registrar), aligned (optional QC copies), merge (VALIS merged image), final."""

    def __init__(self, vdir):
        self.path = os.path.join(vdir, "checkpoint.json")
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def done(self, phase):
        rec = self.data.get(phase)
        if not rec:
            return False
        for f, size in rec["files"].items():
            try:
                if os.stat(f).st_size < size:
                    return False
            except OSError:
                return False
        return True

    def mark(self, phase, files=(), **extra):
        self.data[phase] = {"finished": time.strftime("%Y-%m-%d %H:%M:%S"), "files": {f: os.path.getsize(f) for f in files}, **extra}
        self._write()

    def clear(self):
        self.data = {}
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        for attempt in range(200):       # Windows refuses to replace a file that another process (antivirus, the page) has open
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 199:
                    raise
                time.sleep(0.05)


def job_dirs(job):
    vdir = os.path.join(job["out_dir"], f"VALIS_{job['name']}_FINAL")
    return vdir, os.path.join(vdir, f"merged_{job['name']}.ome.tiff"), os.path.join(vdir, f"{job['name']}_merged_avg.ome.tiff")


def phase_status(job):
    """{'register': bool, 'merge': bool, 'final': bool} for the Monitor tab."""
    vdir, merged, avg = job_dirs(job)
    ck = Checkpoint(vdir)
    return {"register": ck.done("register"), "merge": ck.done("merge") or (not ck.data and file_ok(merged, MIN_BYTES)),
            "final": ck.done("final") or (not ck.data and file_ok(avg, MIN_BYTES))}


# ── one job ──────────────────────────────────────────────────────────────────
def run_job(job, log=print):
    """job: name, slide1, slide2, out_dir, panel (list), non_rigid, crop ('auto'|'overlap'|'all'), pixel_size_um,
    save_aligned, force (= ignore checkpoints and redo everything).
    Phases are checkpointed: if one fails, running the job again continues from that phase."""
    name = job["name"]
    vdir, merged, avg = job_dirs(job)
    ck = Checkpoint(vdir)
    if job.get("force"):
        ck.clear()
    elif job.get("rebuild_final"):
        ck.data.pop("final", None)                       # redo only the final image, e.g. after renaming channels
        ck._write()                                      # the old file stays until the new one is complete (atomic swap)
    for line in (f"=== {name}", f"  Slide 1: {job['slide1']}", f"  Slide 2: {job['slide2']}", f"  Output : {job['out_dir']}"):
        log(line)
    phase = "start"
    try:
        explicit = job.get("panel")
        sig = json.dumps(explicit, sort_keys=True) if explicit else None          # which channel names / selection built the final image
        if ck.done("final") and file_ok(avg, MIN_BYTES):
            if sig and ck.data["final"].get("panel") != sig:
                log("[CHECKPOINT] the channel names / selection changed since the last final image: rebuilding only the final image")
            else:
                log(f"[CHECKPOINT] final image already done - nothing to do: {avg}")
                return avg
        elif not ck.data and not job.get("force") and not job.get("rebuild_final") and file_ok(avg, MIN_BYTES):
            log(f"[CHECKPOINT] a final image from an earlier run exists and is left as it is: {avg}")
            if sig:
                log("[CHECKPOINT] (tick 'Rebuild only the final image' to rebuild it with these channel names)")
            return avg
        if not job.get("force") and not ck.data and file_ok(merged, MIN_BYTES):
            log(f"[CHECKPOINT] VALIS output from an earlier run found, using it: {merged}")
            ck.mark("merge", [merged])
        phase = "VALIS (registration / merged image)"
        if ck.done("merge") and file_ok(merged, MIN_BYTES):
            log("[CHECKPOINT] registration and merged image already done - skipped")
        else:
            cmd = [sys.executable, "-u", os.path.join(APP_DIR, "valis_step.py"), job["out_dir"], job["slide1"], job["slide2"], name,
                   str(bool(job.get("non_rigid", False))), job.get("crop", "auto"), str(bool(job.get("save_aligned", False))),
                   str(bool(job.get("force", False)))]
            if job.get("valis_script"):                                    # test hook: stand-in for valis_runner.py
                cmd[2] = job["valis_script"]
            rc = run_streaming(cmd, log, cwd=APP_DIR)
            if rc != 0:
                raise RuntimeError(f"VALIS failed with exit code {rc}")
            if not file_ok(merged, MIN_BYTES):
                raise RuntimeError(f"VALIS finished but the merged file is missing or too small: {merged}")
            ck = Checkpoint(vdir)
            ck.mark("merge", [merged])
        phase = "final image (average / merge channels)"
        px = job.get("pixel_size_um")
        names = merged_channel_names(merged)
        panel = job.get("panel") or auto_panel(names, job["slide1"], job["slide2"])
        merge_step(merged, avg, resolve_panel(panel, names, job["slide1"], job["slide2"]), name, (px, px) if px else None, log)
        ck.mark("final", [avg], panel=sig)
        return avg
    except Exception as exc:
        log(f"[FAILED] phase '{phase}': {exc}")
        log("[FAILED] Finished phases are saved. Run this job again and it continues from here.")
        raise
