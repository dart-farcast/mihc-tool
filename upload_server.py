"""Lightweight chunked file upload server for large (5 GB+) slides.

Receives 20 MB chunks via POST /upload/chunk and streams them directly to disk
so neither RAM nor Railway's 300s single-request timeout is hit.
"""
import os
import re
import shutil
import tempfile

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

UPLOADS = os.environ.get("COREG_UPLOAD_DIR") or os.path.join(tempfile.gettempdir(), "coreg_uploads")


def _safe(name, fallback="item"):
    clean = os.path.basename((name or "").strip())
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", clean)
    return clean if clean and clean not in (".", "..") else fallback


async def clear_slot(request):
    upload_id = _safe(request.query_params.get("upload_id"), "")
    slot = _safe(request.query_params.get("slot"), "")
    if not upload_id or not slot:
        return JSONResponse({"error": "missing upload_id or slot"}, status_code=400)
    dest = os.path.join(UPLOADS, upload_id, slot)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    return JSONResponse({"ok": True})


async def upload_chunk(request):
    h = request.headers
    upload_id = _safe(h.get("x-upload-id"), "")
    slot = _safe(h.get("x-slot"), "")
    filename = _safe(h.get("x-filename"), "")
    if not upload_id or not slot or not filename:
        return JSONResponse({"error": "missing headers"}, status_code=400)

    try:
        offset = int(h.get("x-offset", "0"))
    except ValueError:
        return JSONResponse({"error": "invalid offset"}, status_code=400)
    done = h.get("x-done") == "1"

    dest = os.path.join(UPLOADS, upload_id, slot)
    os.makedirs(dest, exist_ok=True)
    part_path = os.path.join(dest, filename + ".part")
    final_path = os.path.join(dest, filename)

    mode = "r+b" if os.path.exists(part_path) and offset > 0 else "wb"
    written = 0
    with open(part_path, mode) as fh:
        fh.seek(offset)
        async for chunk in request.stream():
            if chunk:
                fh.write(chunk)
                written += len(chunk)

    if done:
        os.replace(part_path, final_path)

    return JSONResponse({"ok": True, "written": written, "done": done, "filename": filename})


async def upload_status(request):
    upload_id = _safe(request.query_params.get("upload_id"), "")
    slot = _safe(request.query_params.get("slot"), "")
    dest = os.path.join(UPLOADS, upload_id, slot)
    if not upload_id or not slot or not os.path.isdir(dest):
        return JSONResponse({"files": []})
    files = []
    for name in sorted(os.listdir(dest)):
        p = os.path.join(dest, name)
        if os.path.isfile(p) and not name.endswith(".part"):
            files.append({"name": name, "size": os.path.getsize(p)})
    return JSONResponse({"files": files})


app = Starlette(
    routes=[
        Route("/upload/clear", clear_slot, methods=["POST"]),
        Route("/upload/chunk", upload_chunk, methods=["POST"]),
        Route("/upload/status", upload_status, methods=["GET"]),
    ]
)
