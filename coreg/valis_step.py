"""The VALIS step, run as its own process (its own Java engine). Called by coreg_core.run_job.

python valis_step.py <base_dir> <slide_1> <slide_2> <name> [non_rigid] [crop: auto|overlap|all] [save_aligned] [force]
Phases (registration, merged image, optional aligned slides) are saved in checkpoint.json: a re-run continues where it stopped.
"""
from valis import registration
import os
import time
import skimage.io as io
import pathlib

# --- Monkey-patch skimage.io.imread to handle Windows SMB caching delays ---
_orig_imread = io.imread

# --- Monkey-patch VALIS get_sorted_img_f_list to avoid NoneType sorting crash ---
def _safe_get_sorted(self):
    if self.imgs_ordered:
        return self.original_img_list
    img_idx = [slide_obj.stack_idx for slide_obj in self.slide_dict.values()]
    # If any idx is None, fallback to original list
    if any(idx is None for idx in img_idx):
        return self.original_img_list
    import numpy as np
    img_order = np.argsort(img_idx)
    return [self.original_img_list[i] for i in img_order]

registration.Valis.get_sorted_img_f_list = _safe_get_sorted


def _retry_imread(fname, *args, **kwargs):
    for attempt in range(25):
        try:
            return _orig_imread(fname, *args, **kwargs)
        except FileNotFoundError:
            time.sleep(1)
    return _orig_imread(fname, *args, **kwargs)

io.imread = _retry_imread

# --- Monkey-patch pathlib.Path.resolve to bypass Windows SMB WinError 1816 ---
# When dealing with UNC paths, _getfinalpathname can hit network quota limits.
_orig_resolve = pathlib.Path.resolve

def _safe_resolve(self, strict=False):
    try:
        return _orig_resolve(self, strict=strict)
    except OSError as e:
        # WinError 1816: Not enough quota is available to process this command
        if getattr(e, 'winerror', None) == 1816:
            # Fallback: just return the absolute path without deep SMB resolution
            return self.absolute()
        raise

pathlib.Path.resolve = _safe_resolve

def run_valis_pipeline(
    base_dir,
    slide_i,
    slide_ii,
    output_name,
    non_rigid=True,
    crop="auto",         # 'auto' = 'overlap', falling back to 'all' on "bad extract area"
    save_aligned=False,  # optional QC copies of the aligned slides; the final image does not need them
    force=False          # ignore checkpoints and redo everything
):
    """Phases, each saved in <output_dir>/checkpoint.json:
         register  VALIS registration (the saved registrar is reloaded on a re-run instead of registering again)
         merge     merged multi-channel OME-TIFF (this is what the final image is built from)
         aligned   optional aligned slides
    A re-run skips every finished phase and continues at the first unfinished one."""
    import glob
    import coreg_core as C

    output_dir = os.path.join(base_dir, f"VALIS_{output_name}_FINAL")
    os.makedirs(output_dir, exist_ok=True)
    ck = C.Checkpoint(output_dir)
    if force:
        ck.clear()
    merged_path = os.path.join(output_dir, f"merged_{output_name}.ome.tiff")
    saved = lambda: sorted(glob.glob(os.path.join(output_dir, "*", "data", "*_registrar.pickle")))

    # ── phase 1: registration (reload a saved one if there is one) ──
    registrar = None
    if saved() and not force:
        try:
            from valis import slide_io
            slide_io.init_jvm()          # the saved registrar refers to Java classes: the JVM must be running to unpickle it
            registrar = registration.load_registrar(saved()[0])
            print(f"[CHECKPOINT] registration already done - loaded {saved()[0]}", flush=True)
            ck.mark("register", saved()[:1])
        except Exception as e:
            print(f"[CHECKPOINT] saved registration could not be loaded ({e}) - registering again", flush=True)
    if registrar is None:
        print("[PHASE] registration", flush=True)
        registrar = registration.Valis(
            src_dir=base_dir,
            dst_dir=output_dir,
            img_list=[slide_i, slide_ii],
            imgs_ordered=True,
            reference_img_f=slide_i
        )
        registrar.register()
        ck.mark("register", saved()[:1])
        print("[CHECKPOINT] registration saved", flush=True)

    # ── phase 2: merged image ──
    if ck.done("merge") and C.file_ok(merged_path, C.MIN_BYTES):
        print("[CHECKPOINT] merged image already done - skipped", flush=True)
    else:
        print("[PHASE] merged image", flush=True)
        registrar.warp_and_merge_slides(
            merged_path,
            drop_duplicates=True,
            non_rigid=non_rigid,
            interp_method="bilinear"
        )
        ck.mark("merge", [merged_path])
        print("[CHECKPOINT] merged image saved", flush=True)

    # ── phase 3 (optional): aligned slides ──
    if save_aligned and not ck.done("aligned"):
        print("[PHASE] aligned slides (optional QC copies)", flush=True)
        aligned_dir = os.path.join(output_dir, "aligned_slides")
        os.makedirs(aligned_dir, exist_ok=True)

        def _warp_and_save(crop_mode):
            registrar.warp_and_save_slides(
                aligned_dir,
                crop=crop_mode,
                non_rigid=non_rigid,
                interp_method="bilinear"
            )

        if crop == "auto":
            try:
                _warp_and_save("overlap")
            except Exception as e:
                if "bad extract area" not in str(e):
                    raise
                print("crop='overlap' failed (bad extract area) - retrying with crop='all'", flush=True)
                _warp_and_save("all")
        else:
            _warp_and_save(crop)
        ck.mark("aligned", glob.glob(os.path.join(aligned_dir, "*.ome.tiff")))
        print("[CHECKPOINT] aligned slides saved", flush=True)

    # DO NOT kill the JVM here when using subprocess
    return merged_path, output_dir

# --- Monkey-patch os.stat to handle WinError 1816 globally ---
import os
_orig_stat = os.stat
def _retry_stat(p, *args, **kwargs):
    for attempt in range(25):
        try:
            return _orig_stat(p, *args, **kwargs)
        except OSError as e:
            if getattr(e, 'winerror', None) == 1816:
                time.sleep(0.5)
            else:
                raise
    return _orig_stat(p, *args, **kwargs)
os.stat = _retry_stat

# --- Monkey-patch pathlib.Path.stat to handle WinError 1816 globally ---
_orig_path_stat = pathlib.Path.stat
def _retry_path_stat(self, follow_symlinks=True):
    for attempt in range(25):
        try:
            return _orig_path_stat(self, follow_symlinks=follow_symlinks)
        except OSError as e:
            if getattr(e, 'winerror', None) == 1816:
                time.sleep(0.5)
            else:
                raise
    return _orig_path_stat(self, follow_symlinks=follow_symlinks)
pathlib.Path.stat = _retry_path_stat

# --- Monkey-patch pathlib.Path.is_dir to bypass WinError 1816 ---
_orig_is_dir = pathlib.Path.is_dir
def _safe_is_dir(self):
    try:
        return _orig_is_dir(self)
    except OSError as e:
        if getattr(e, 'winerror', None) == 1816:
            # We know it's a file if it has a file extension
            if self.suffix:
                return False
        raise
pathlib.Path.is_dir = _safe_is_dir


if __name__ == "__main__":
    import sys
    base_dir, slide_i, slide_ii, output_name = sys.argv[1:5]
    run_valis_pipeline(
        base_dir=base_dir,
        slide_i=slide_i,
        slide_ii=slide_ii,
        output_name=output_name,
        non_rigid=len(sys.argv) > 5 and sys.argv[5].lower() == "true",
        crop=sys.argv[6] if len(sys.argv) > 6 else "auto",
        save_aligned=len(sys.argv) > 7 and sys.argv[7].lower() == "true",
        force=len(sys.argv) > 8 and sys.argv[8].lower() == "true",
    )
