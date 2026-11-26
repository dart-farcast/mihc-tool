"""Background worker. The app starts it; it keeps running if the browser is closed.

python worker.py batch <run_dir>   run every job of <run_dir>/spec.json (jobs side by side, each job as its own process)
python worker.py job <job.json>    run one job (also: <out_dir> <slide1> <slide2> <name>)

<run_dir>/spec.json   {"jobs": [{"name","slide1","slide2","out_dir"}], "settings": {...}, "runner": optional test stand-in}
<run_dir>/status.json written after every change, <run_dir>/logs/<n>_<name>.log one log per job,
create <run_dir>/stop.flag to stop (running jobs are killed, queued jobs are cancelled).
"""
import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coreg_core as C

now = lambda: datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_one(argv):
    if len(argv) == 1:
        with open(argv[0], encoding="utf-8") as fh:
            job = json.load(fh)
    elif len(argv) == 4:
        job = {"out_dir": argv[0], "slide1": argv[1], "slide2": argv[2], "name": argv[3]}
    else:
        print(__doc__)
        return 2
    try:
        out = C.run_job(job, log=lambda m: print(m, flush=True))
        print(f"[DONE] {job['name']} -> {out}", flush=True)
        return 0
    except Exception as exc:
        traceback.print_exc()
        print(f"[ERROR] {job.get('name', '?')} failed: {exc}", flush=True)
        return 1


def run_batch(run_dir):
    with open(os.path.join(run_dir, "spec.json"), encoding="utf-8") as fh:
        spec = json.load(fh)
    settings = spec.get("settings", {})
    runner = [sys.executable, "-u", spec.get("runner") or os.path.abspath(__file__)] + ([] if spec.get("runner") else ["job"])
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "jobs"), exist_ok=True)
    stop_flag = os.path.join(run_dir, "stop.flag")

    jobs = [{"name": j["name"], "out_dir": j["out_dir"], "status": "queued", "log": os.path.join("logs", f"{i:03d}_{j['name']}.log"),
             "started": None, "ended": None, "error": None, "output": None} for i, j in enumerate(spec["jobs"])]
    state = {"pid": os.getpid(), "started": now(), "ended": None, "state": "running", "jobs": jobs}
    lock = threading.Lock()
    procs = {}

    def save():
        tmp = os.path.join(run_dir, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        for _ in range(100):             # Windows refuses to replace a file another process is reading (the page does)
            try:
                os.replace(tmp, os.path.join(run_dir, "status.json"))
                return
            except PermissionError:
                time.sleep(0.05)         # still locked: skip this update, the next save() writes the full state anyway

    def update(i, **kw):
        with lock:
            jobs[i].update(kw)
            save()

    q = queue.Queue()
    for i in range(len(jobs)):
        q.put(i)

    def worker():
        while not os.path.exists(stop_flag):
            try:
                i = q.get_nowait()
            except queue.Empty:
                return
            job = {**settings, **spec["jobs"][i]}
            job_file = os.path.join(run_dir, "jobs", f"{i:03d}_{jobs[i]['name']}.json")
            with open(job_file, "w", encoding="utf-8") as fh:
                json.dump(job, fh, indent=1)
            update(i, status="running", started=now())
            with open(os.path.join(run_dir, jobs[i]["log"]), "w", encoding="utf-8", buffering=1) as lf:
                p = C.popen(runner + [job_file], stdout=lf, stderr=subprocess.STDOUT, cwd=C.APP_DIR)
                procs[i] = p
                rc = p.wait()
            procs.pop(i, None)
            if os.path.exists(stop_flag) and rc != 0:
                update(i, status="cancelled", ended=now())
            elif rc == 0:
                update(i, status="done", ended=now(), output=C.job_dirs(job)[2])
            else:
                update(i, status="failed", ended=now(), error=f"exit code {rc} - see the log")

    with lock:
        save()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, int(settings.get("workers", 1))))]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        if os.path.exists(stop_flag):
            for p in list(procs.values()):
                C.kill_tree(p)
        time.sleep(1)
    with lock:
        for j in jobs:
            if j["status"] == "queued":
                j["status"] = "cancelled"
        failed = any(j["status"] == "failed" for j in jobs)
        state["state"] = "stopped" if os.path.exists(stop_flag) else ("finished with errors" if failed else "finished")
        state["ended"] = now()
        save()
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "batch":
        sys.exit(run_batch(sys.argv[2]))
    sys.exit(run_one(sys.argv[2:] if len(sys.argv) > 1 and sys.argv[1] == "job" else sys.argv[1:]))
