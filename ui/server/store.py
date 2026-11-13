"""Job folders on disk. Live jobs live in ui/data/jobs/<id>/, demo jobs in runs/case*/ and runs/demo_*/.

Folder format (identical for live, imported and demo jobs):
  job.json     {id, title, created_at, status, demo, session_id, inputs, summary, updated_at}
  events.json  {"events": [...]}       raw Managed Agents events, oldest first
  files/       every session output file, flat, newest copy of a repeated name wins
"""
from __future__ import annotations

import json
import os
import random
import re
import string
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from paths import DATA_DIR, RUNS_DIR

_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(job_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        if job_id not in _LOCKS:
            _LOCKS[job_id] = threading.RLock()
        return _LOCKS[job_id]


def new_job_id() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return time.strftime("job-%Y%m%d-%H%M%S-") + suffix


def demo_dirs() -> List[Path]:
    out = []
    if RUNS_DIR.exists():
        for p in sorted(RUNS_DIR.iterdir()):
            if p.is_dir() and (p.name.startswith("case") or p.name.startswith("demo_")) and (p / "job.json").exists():
                out.append(p)
    return out


def showcase_dirs() -> List[Path]:
    """Read-only copies of delivered jobs shipped with a deployment (runs/showcase/<job-id>/, job.json "frozen": true).
    ModelArk keeps session files only for about a day, so a cloud instance cannot rebuild older jobs from the API."""
    root = RUNS_DIR / "showcase"
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "job.json").exists()) if root.exists() else []


def job_dir(job_id: str) -> Optional[Path]:
    if not job_id or "/" in job_id or job_id.startswith("."):
        return None
    local = DATA_DIR / job_id
    if (local / "job.json").exists():
        return local
    for d in showcase_dirs():
        if d.name == job_id:
            return d
    for d in demo_dirs():
        if d.name == job_id:
            return d
    return None


def all_job_dirs() -> List[Path]:
    out = []
    if DATA_DIR.exists():
        for p in DATA_DIR.iterdir():
            if p.is_dir() and (p / "job.json").exists():
                out.append(p)
    names = {p.name for p in out}
    out.extend(d for d in showcase_dirs() if d.name not in names)
    out.extend(demo_dirs())
    return out


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


TITLE_OVERRIDES = DATA_DIR.parent / "title_overrides.json"  # renames of read-only (shipped) jobs, writable folder


def title_overrides() -> dict:
    try:
        return json.loads(TITLE_OVERRIDES.read_text())
    except (OSError, ValueError):
        return {}


def set_title_override(job_id: str, title: str) -> None:
    data = title_overrides()
    data[job_id] = title
    TITLE_OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    TITLE_OVERRIDES.write_text(json.dumps(data, indent=1, ensure_ascii=False))


# ----------------------------------------------------------------------------- shared lists (folders, deletions)
# The cloud site runs on several instances, each with its own disk. Lists every instance must agree on live in the TOS
# bucket under meta/ (a small JSON object each); locally they are files next to the data dir.
_SHARED: Dict[str, dict] = {}
SHARED_TTL_S = 15


def _shared_get(key: str, local: Path, fresh: bool = False) -> dict:
    import time as _t
    hit = _SHARED.get(key)
    if not fresh and hit and _t.time() - hit["at"] < SHARED_TTL_S:
        return hit["data"]
    data = None
    try:
        import storage
        if storage.enabled():
            raw = storage.get_raw(key)
            data = json.loads(raw) if raw else {}
    except Exception:
        data = None
    if data is None:
        try:
            data = json.loads(local.read_text())
        except (OSError, ValueError):
            data = {}
    data = data if isinstance(data, dict) else {}
    _SHARED[key] = {"data": data, "at": _t.time()}
    return data


def _shared_put(key: str, local: Path, data: dict) -> None:
    import time as _t
    blob = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True).encode()
    saved = False
    try:
        import storage
        if storage.enabled():
            saved = storage.put_raw(key, blob)
    except Exception:
        saved = False
    if not saved:
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, local)
    _SHARED[key] = {"data": data, "at": _t.time()}


# Which folder each project is in ({job_id: folder name}); only the list grouping.
FOLDERS_FILE = DATA_DIR.parent / "folders.json"
FOLDERS_KEY = "meta/folders.json"


def folders(fresh: bool = False) -> Dict[str, str]:
    return {k: str(v) for k, v in _shared_get(FOLDERS_KEY, FOLDERS_FILE, fresh).items() if isinstance(k, str) and v}


def set_folders(job_ids, folder: Optional[str]) -> Dict[str, str]:
    """Moves the jobs into `folder` (None or "" = back to the main list). Returns the new map."""
    data = dict(folders(fresh=True))
    for j in job_ids:
        if folder:
            data[j] = folder
        else:
            data.pop(j, None)
    _shared_put(FOLDERS_KEY, FOLDERS_FILE, data)
    return data


# Projects deleted on any instance ({job_id: deleted_at}): every instance drops its own copy and never rebuilds them
# from the bucket or from ModelArk sessions (without this a deleted card came back from the other instances).
DELETED_FILE = DATA_DIR.parent / "deleted.json"
DELETED_KEY = "meta/deleted.json"


def deleted_ids(fresh: bool = False) -> Dict[str, str]:
    return {k: str(v) for k, v in _shared_get(DELETED_KEY, DELETED_FILE, fresh).items() if isinstance(k, str)}


def mark_deleted(job_ids) -> None:
    import time as _t
    data = dict(deleted_ids(fresh=True))
    for j in job_ids:
        data[j] = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    _shared_put(DELETED_KEY, DELETED_FILE, data)


def drop_deleted_copies() -> List[str]:
    """Removes this instance's local copy of every project deleted elsewhere. Returns the ids removed here."""
    import shutil
    gone = deleted_ids()
    removed = []
    if gone and DATA_DIR.exists():
        for p in DATA_DIR.iterdir():
            if p.name in gone and p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(p.name)
    return removed


def read_job(d: Path) -> dict:
    job = json.loads((d / "job.json").read_text())
    job.setdefault("id", d.name)
    job.setdefault("demo", False)
    job.setdefault("inputs", {})
    renamed = title_overrides().get(job["id"])
    if renamed:
        job["title"] = renamed
    return job


def write_job(d: Path, job: dict) -> None:
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write(d / "job.json", json.dumps(job, indent=1, ensure_ascii=False))


def ai_label() -> bool:
    """False when this site's ads carry no "AI-generated video" pill (UI_AI_LABEL=0, set by the build profile)."""
    return os.environ.get("UI_AI_LABEL", "1") != "0"


def last_edit_version(files_dir: Path) -> int:
    """The highest final_edit_v<n>.mp4 of the job: most are only in the bucket, so the job's index counts too."""
    try:
        job = json.loads((files_dir.parent / "job.json").read_text())
    except (OSError, ValueError):
        job = {}
    names = {p.name for p in files_dir.glob("final_edit_v*.mp4")} | set(job.get("files_index") or {}) | set(job.get("tos_files") or {})
    return max((int(m.group(1)) for n in names if (m := re.fullmatch(r"final_edit_v(\d+)\.mp4", n))), default=0)


def edit_plans(files_dir: Path) -> List[Path]:
    """The agent's edit plans, oldest first. Disk times mean nothing after a restart (files come back in any order):
    the session's creation time decides, then the version in the name."""
    try:
        index = json.loads((files_dir.parent / "job.json").read_text()).get("files_index") or {}
    except (OSError, ValueError):
        index = {}

    def order(p: Path):
        v = re.search(r"_v(\d+)", p.stem)
        return (index.get(p.name) or {}).get("created_at") or 0, int(v.group(1)) if v else 0, p.stat().st_mtime
    return sorted(files_dir.glob("edit_plan*.json"), key=order)


def set_final_source(d: Path, source: str) -> None:
    """Which cut is the ad now: "agent" (its newest final) or "edit" (the newest version made on the site)."""
    with lock_for(d.name):
        job = read_job(d)
        job["final_source"] = source
        write_job(d, job)


def read_events(d: Path) -> List[dict]:
    p = d / "events.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return data.get("events") or data.get("data") or []
    return data if isinstance(data, list) else []


def write_events(d: Path, events: List[dict]) -> None:
    _atomic_write(d / "events.json", json.dumps({"events": events}, ensure_ascii=False))


def files_dir(d: Path) -> Path:
    f = d / "files"
    f.mkdir(parents=True, exist_ok=True)
    return f


def create_job_dir(job_id: str) -> Path:
    d = DATA_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "files").mkdir(exist_ok=True)
    return d
