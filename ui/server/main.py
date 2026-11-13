"""FastAPI backend for the Ad Replicator web app (port 8787).

The ARK_API_KEY never leaves this process: it is read by tools/ma_client.py from the environment
(or arkcli's stored key) and used only for calls to the Managed Agents API.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sys
import time
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import File, Form, UploadFile, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import store  # noqa: E402
from compose import compose_message  # noqa: E402
from derive import compact_event, derive_job, job_summary, now_iso, ts_seconds  # noqa: E402
from paths import DATA_DIR, RUNS_DIR, TOOLS_DIR, ensure_ffmpeg, load_module, ma_client  # noqa: E402
from sync import SYNCER, agent_recut  # noqa: E402
from validate import is_https_url, social_platform, validate_sample  # noqa: E402
import social  # noqa: E402

app = FastAPI(title="Ad Replicator", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("UI_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")],
    allow_methods=["*"], allow_headers=["*"],
)

MAX_BUDGET = {"seedream": 24, "seedance": 16, "evaluator": 12}


# ----------------------------------------------------------------------------- models
class Budget(BaseModel):
    seedream: int = Field(2, ge=0, le=MAX_BUDGET["seedream"])
    seedance: int = Field(4, ge=0, le=MAX_BUDGET["seedance"])
    evaluator: int = Field(3, ge=0, le=MAX_BUDGET["evaluator"])


class Options(BaseModel):
    length_s: int = Field(20, ge=5, le=60)
    resolution: str = "480p"
    captions: str = Field("full", pattern="^(full|cta_only|none)$")  # on-screen text: all captions, only the call to action, or none
    # the effect at every cut (video-assembly `transitions`): sample = as the sample ad does it
    transitions: str = Field("mix", pattern="^(sample|cut|mix|dissolve)$")
    # new takes one scene may get after its first take fails its check; 0 = no limit
    retakes: int = Field(1, ge=0, le=3)
    video_model: str = Field("seedance-2.5", pattern="^seedance-(2\\.5|2\\.0|2\\.0-fast|2\\.0-mini)$")
    image_model: str = Field("seedream-5.0-pro", pattern="^seedream-5\\.0-(pro|flash)$")
    # optional: this job's agent-context compaction threshold (tokens, >= 50000); None = the agent's setting (cost tests)
    compact_threshold: Optional[int] = Field(None, ge=50000, le=190000)


class NewJob(BaseModel):
    title: Optional[str] = None
    sample_url: str = ""
    sample_upload: Optional[dict] = None  # {file_id, filename, local_name} from POST /api/uploads (an MP4 the user uploaded)
    product_url: str
    requirements: str = ""
    budget: Budget = Budget()
    options: Options = Options()
    auto_approve: bool = False
    step_approve: bool = False
    references: List[dict] = []


class UrlBody(BaseModel):
    url: str


class MessageBody(BaseModel):
    text: str


class ImportBody(BaseModel):
    session_id: str
    title: Optional[str] = None
    demo: bool = False
    dest: Optional[str] = None


# ----------------------------------------------------------------------------- helpers
_MISS_DISCOVER = [0.0]


def _load(job_id: str):
    if job_id in store.deleted_ids():  # deleted on any instance: never served from a stale copy or rebuilt
        import shutil
        d = store.job_dir(job_id)
        if d is not None and d.parent == store.DATA_DIR:
            shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(404, f"job {job_id} was deleted")
    d = store.job_dir(job_id)
    if d is None and not SHOWCASE and re.fullmatch(r"job-[0-9]{8}-[0-9]{6}-[a-z0-9]{4}", job_id or "") and time.time() - _MISS_DISCOVER[0] > 10:
        # a fresh (or second) serverless instance knows only the jobs it has seen: look the session up before saying 404
        _MISS_DISCOVER[0] = time.time()
        try:
            from sync import restore_from_bucket
            if restore_from_bucket(job_id) is not None:
                SYNCER.start(job_id)
            elif SYNCER.discover():
                d = store.job_dir(job_id)
                if d is not None:
                    SYNCER.start(job_id)
                    SYNCER.sync_once(job_id)  # events and files now, so the page opens complete
        except Exception as e:
            print(f"[ui] discover for {job_id}: {type(e).__name__}: {str(e)[:160]}", flush=True)
        d = store.job_dir(job_id)
    if d is None:
        raise HTTPException(404, f"job {job_id} not found")
    job = store.read_job(d)
    events = store.read_events(d)
    return d, job, events


def _derived(job_id: str, replay_t: Optional[float] = None) -> dict:
    d, job, events = _load(job_id)
    out = derive_job(job, events, store.files_dir(d), replay_t=replay_t)
    out["syncing"] = SYNCER.is_running(job_id) or SE.is_running(job_id)
    return out


def _client():
    try:
        return SYNCER.client()
    except RuntimeError as e:
        raise HTTPException(503, f"The backend has no API key: {e}. Set ARK_API_KEY in the environment of the backend process.")


# ----------------------------------------------------------------------------- endpoints
@app.middleware("http")
async def _role_credentials(request: Request, call_next):
    """veFaaS adds the bound IAM role's temporary keys to each request: keep the newest for the bucket (storage.py)."""
    import storage
    if storage.remember_request_credentials(request.headers):
        # first keys of this process: the startup discovery could not reach the bucket, catch up with it now
        from sync import bucket_catchup
        threading.Thread(target=bucket_catchup, daemon=True, name="bucket-catchup").start()
    try:
        ensure_ffmpeg()
    except OSError as e:
        print(f"[ui] ffmpeg copy failed: {e}", flush=True)
    return await call_next(request)


SHOWCASE = os.environ.get("UI_SHOWCASE") == "1"  # view-only site: example projects only, no agent, no changes
_SHOWCASE_POSTS = re.compile(r"^/api/jobs/[\w-]+/captions/preview$")  # renders a caption picture; changes nothing


@app.middleware("http")
async def _showcase_only(request: Request, call_next):
    if SHOWCASE and request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") \
            and not _SHOWCASE_POSTS.match(request.url.path):
        return JSONResponse({"detail": "This is a view-only showcase: projects cannot be started or changed here."}, status_code=403)
    return await call_next(request)


@app.get("/api/storage")
def storage_check():
    """Writes, reads, signs and deletes one small object in the bucket: shows whether project files are being kept."""
    import storage
    return storage.self_test()


@app.get("/api/health")
def health():
    key_present = bool(os.environ.get("ARK_API_KEY")) or (Path.home() / ".arkcli-bp" / "config.yaml").exists()
    return {"ok": True, "time": now_iso(), "api_key_present": key_present, "data_dir": str(DATA_DIR),
            "demo_jobs": [d.name for d in store.demo_dirs()], "agent": ma_client().configured_id("MA_AGENT_ID"),
            "missing_settings": [] if SHOWCASE else ma_client().missing_ids(), "showcase": SHOWCASE,
            "syncing": [k for k, t in SYNCER.threads.items() if t.is_alive()]}


_LAST_DISCOVER = [0.0]


@app.get("/api/jobs")
def list_jobs(demo: Optional[bool] = None):
    if not SHOWCASE and time.time() - _LAST_DISCOVER[0] > 60:  # serverless: pick up jobs started on other instances
        _LAST_DISCOVER[0] = time.time()
        try:
            if SYNCER.discover():
                threading.Thread(target=SYNCER.resume_all, daemon=True).start()
        except Exception:
            pass
    rows = []
    store.drop_deleted_copies()  # deleted on another instance: drop this instance's copy too
    for d in store.all_job_dirs():
        try:
            job = store.read_job(d)
            if demo is not None and bool(job.get("demo")) != demo:
                continue
            events = store.read_events(d)
            derived = derive_job(job, events, store.files_dir(d))
            rows.append(job_summary(job, derived))
        except Exception as e:  # a broken folder must not hide the others
            rows.append({"id": d.name, "title": d.name, "created_at": None, "status": "failed", "demo": False,
                         "thumbnail": None, "error": f"{type(e).__name__}: {e}"})
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    where = store.folders()
    for r in rows:
        r["folder"] = where.get(r["id"])
    live_running = any(r["status"] in ("running", "awaiting_approval", "queued") and not r["demo"] for r in rows)
    return {"jobs": rows, "live_running": live_running}


@app.post("/api/validate-sample")
def validate_sample_ep(body: UrlBody):
    return validate_sample(body.url)


@app.post("/api/sample-link")
def sample_link_start(body: UrlBody):
    """Downloads a public TikTok / Facebook / Instagram / YouTube / X video for a new job (poll GET /api/sample-link/{id}).
    The finished file goes to the Files API like an upload; the answer's `ref` is passed as sample_upload."""
    v = validate_sample(body.url)
    if not v.get("social"):
        raise HTTPException(422, v.get("reason") or "Not a Facebook, Instagram, YouTube or X video link.")
    updir = _upload_dir(None)
    return social.start(body.url.strip(), updir, lambda path, safe, note: _send_upload(path, safe, note))


@app.get("/api/sample-link/{dl_id}")
def sample_link_status(dl_id: str):
    st = social.get(dl_id)
    if st is None:  # not in this instance nor (yet) in the bucket: the page keeps asking for a while
        raise HTTPException(404, "This download is not known here any more. Paste the link again.")
    return st


@app.post("/api/jobs")
def create_job(body: NewJob):
    missing = ma_client().missing_ids()
    if missing:  # no built-in agent: the function must name its own agent, environment and vault
        raise HTTPException(503, f"This site is not set up yet: set {', '.join(missing)} in the function's environment "
                                 "variables and release it.")
    upload = body.sample_upload  # an empty object is an upload that never finished, not "no upload"
    if upload is not None:
        if not upload.get("file_id"):
            raise HTTPException(422, "The uploaded sample video is missing; upload it again.")
        if not re.search(r"\.(mp4|mov|m4v|webm)$", str(upload.get("filename", "")), re.I):
            raise HTTPException(422, "The sample video must be an MP4, MOV or WebM file.")
    else:
        if not is_https_url(body.sample_url):
            raise HTTPException(422, "The sample video link must be an https:// link.")
        if social_platform(body.sample_url):  # the agent cannot open social pages: the site downloads them first
            raise HTTPException(422, "Social-media links are downloaded on this page first (POST /api/sample-link); send the result as sample_upload.")
        v = validate_sample(body.sample_url)
        if not v["ok"]:
            raise HTTPException(422, v["reason"])
    if not is_https_url(body.product_url):
        raise HTTPException(422, "The product page link must be an https:// link.")
    if body.options.resolution not in ("480p", "720p"):
        raise HTTPException(422, "Resolution must be 480p or 720p.")
    client = _client()
    job_id = store.new_job_id()
    title = (body.title or "").strip() or f"New ad {time.strftime('%d %b %H:%M')}"
    sample_mount = None
    if upload:  # the agent reads the uploaded file from the session's uploads folder
        ext = (re.search(r"\.(\w+)$", upload["filename"]) or [None, "mp4"])[1].lower()
        sample_mount = f"sample_video.{ext}"
        body.sample_url = f"/mnt/session/uploads/{sample_mount}"
    text = compose_message(body.sample_url, body.product_url, body.requirements, body.budget.model_dump(),
                           body.options.model_dump(), body.auto_approve, body.step_approve, body.references)
    try:
        # lesson stores (MA_MEMORY_STORE_IDS) mount read-only at /mnt/memory/<store>/ for this session only
        session = client.create_session(title=f"{title} [{job_id}]", memory_store_ids=ma_client().configured_memory_stores(),
                                        compact_threshold=body.options.compact_threshold,
                                        compact_instructions=COMPACT_INSTRUCTIONS if body.options.compact_threshold else None)
    except Exception as e:
        raise HTTPException(502, f"Could not create the agent session: {str(e)[:300]}")
    session_id = session.get("id")
    if not session_id:
        raise HTTPException(502, f"Unexpected session response: {json.dumps(session)[:300]}")
    if upload:
        try:
            client.add_resource(session_id, upload["file_id"], sample_mount)
        except Exception as e:
            # no job folder exists yet: drop the empty session, or every failed start comes back as a "Starting" project
            try:
                client.delete(f"/sessions/{session_id}")
            except Exception:
                pass
            if "unsafe characters" in str(e):  # a file uploaded before names were cleaned (e.g. "clip_1......mp4")
                raise HTTPException(422, "The sample video's file name has characters the platform refuses (such as \"..\"). "
                                         "Upload the video again (it now gets a safe name) and start.")
            raise HTTPException(502, f"The uploaded sample video could not be attached to the session: {str(e)[:300]}")
    for r in body.references:
        try:
            client.add_resource(session_id, r["file_id"], r.get("mount_name") or r.get("filename"))
        except Exception as e:  # a missing reference must not kill the job; the agent sees it in the message
            r["mount_error"] = str(e)[:200]
    d = store.create_job_dir(job_id)
    job = {
        "id": job_id, "title": title, "created_at": now_iso(), "status": "queued", "demo": False,
        "session_id": session_id, "inputs": {
            "sample_url": body.sample_url.strip(), "product_url": body.product_url.strip(),
            "requirements": body.requirements, "budget": body.budget.model_dump(), "options": body.options.model_dump(),
            "auto_approve": body.auto_approve, "step_approve": body.step_approve, "references": body.references, "message": text,
        }, "summary": {}, "files_index": {},
    }
    if upload and upload.get("local_name"):  # local copy for the side-by-side player
        src = DATA_DIR.parent / "uploads" / Path(upload["local_name"]).name
        if src.exists():
            fd = store.files_dir(d)
            fd.mkdir(parents=True, exist_ok=True)
            import shutil
            ext_m = re.search(r"[.](\w+)$", src.name)
            shutil.copyfile(src, fd / ("sample." + (ext_m.group(1).lower() if ext_m else "mp4")))
        job["inputs"]["sample_upload"] = {"filename": upload.get("filename"), "file_id": upload.get("file_id"),
                                          **({"source_url": upload["source_url"]} if upload.get("source_url") else {})}
    store.write_job(d, job)
    store.write_events(d, [])
    try:
        client.send_message(session_id, text)
    except Exception as e:
        job["status"] = "failed"
        job["start_error"] = str(e)[:500]
        store.write_job(d, job)
        raise HTTPException(502, f"The session was created but the brief could not be sent: {str(e)[:300]}")
    SYNCER.start(job_id)
    return _derived(job_id)


# ----------------------------------------------------------------------------- edit the sample (no agent)
import sample_edit as SE  # noqa: E402

IMAGE_FILE = re.compile(r"\.(png|jpe?g|webp)$", re.I)


class EditOptions(BaseModel):
    video_model: str = Field("seedance-2.0-mini", pattern="^seedance-(2\\.5|2\\.0|2\\.0-fast|2\\.0-mini)$")
    resolution: str = Field("480p", pattern="^(480p|720p)$")
    captions: str = Field("full", pattern="^(full|cta_only|none)$")
    people: str = Field("keep", pattern="^(keep|upload|generate)$")  # keep the sample's, the client's pictures, or new AI people
    image_model: str = Field("seedream-5.0-pro", pattern="^seedream-5\\.0-(pro|flash)$")
    use_until: Optional[float] = Field(None, ge=4, le=180)  # leave the rest of the sample out (e.g. its closing card)
    check: bool = True


class EditJob(BaseModel):
    title: Optional[str] = None
    sample_url: str = ""
    sample_upload: Optional[dict] = None  # {file_id, filename, local_name} from POST /api/uploads
    product_images: List[dict] = Field(default_factory=list)  # uploads: the client's personalized product picture(s), 1-4
    person_images: List[dict] = Field(default_factory=list)  # uploads: the people to show instead of the sample's (people=upload)
    product_name: str = Field("", max_length=80)
    product_text: str = Field("", max_length=160)  # names / words printed on the product, comma separated
    size_note: str = Field("", max_length=200)
    offer: str = Field("", max_length=80)  # approved offer wording for the end card, as given
    notes: str = Field("", max_length=1000)
    options: EditOptions = EditOptions()
    folder: Optional[str] = None


class EstimateBody(BaseModel):
    seconds: float = Field(..., gt=0, le=600)
    video_model: str = "seedance-2.0-mini"
    resolution: str = "480p"
    people_new: int = Field(0, ge=0, le=3)
    image_model: str = "seedream-5.0-pro"


class RetryBody(BaseModel):
    from_step: Optional[str] = Field(None, pattern="^(cut|plan|people|pieces|edit|assemble|check)$")


@app.get("/api/edit-mode")
def edit_mode_info():
    """What the "edit the sample" mode needs on this site: a bucket (the pieces Seedance reads) and, for samples with
    people, the asset library."""
    import assets_lib
    import storage
    return {"assets": assets_lib.status(), "bucket": storage.enabled() or bool(os.environ.get("EDIT_TOS_BUCKET")),
            "piece_max_s": SE.PIECE_MAX, "join_trim_s": SE.JOIN_TRIM_S}


@app.post("/api/edit-mode/estimate")
def edit_mode_estimate(body: EstimateBody):
    v = body.video_model.removeprefix("seedance-")
    if v not in SE.PIECE_MAX or body.resolution not in ("480p", "720p"):
        raise HTTPException(422, "Unknown model or resolution")
    out = SE.estimate(body.seconds, v, body.resolution, body.people_new, body.image_model)
    if os.environ.get("UI_SHOW_COST", "1") == "0":
        out = {k: v for k, v in out.items() if not k.endswith("_usd") and k != "price_per_m"}
    return out


def _upload_ref(ref: dict, what: str) -> dict:
    if not isinstance(ref, dict) or not ref.get("local_name"):
        raise HTTPException(422, f"The {what} upload is missing; upload it again.")
    return {k: ref.get(k) for k in ("file_id", "filename", "local_name", "source_url") if ref.get(k)}


@app.post("/api/edit-jobs")
def create_edit_job(body: EditJob):
    """A new ad made by editing the sample itself with the client's pictures (sample_edit.py), no agent session."""
    if body.sample_upload:
        sample = _upload_ref(body.sample_upload, "sample video")
        if not re.search(r"\.(mp4|mov|m4v|webm)$", str(sample.get("filename") or sample["local_name"]), re.I):
            raise HTTPException(422, "The sample video must be an MP4, MOV or WebM file.")
    else:
        sample = None
        if not is_https_url(body.sample_url) or social_platform(body.sample_url):
            raise HTTPException(422, "Upload the sample video, or give a direct https:// link to the MP4 file.")
        v = validate_sample(body.sample_url)
        if not v["ok"]:
            raise HTTPException(422, v["reason"])
    if not 1 <= len(body.product_images) <= 4:
        raise HTTPException(422, "Add 1 to 4 pictures of the personalized product.")
    if body.options.people == "upload" and not 1 <= len(body.person_images) <= 3:
        raise HTTPException(422, "Add 1 to 3 pictures of the people to show, or choose another people option.")
    prods = [_upload_ref(r, "product picture") for r in body.product_images]
    people = [_upload_ref(r, "person picture") for r in body.person_images] if body.options.people == "upload" else []
    for r in prods + people:
        if not IMAGE_FILE.search(str(r.get("filename") or r["local_name"])):
            raise HTTPException(422, f"{r.get('filename') or r['local_name']}: use PNG, JPG or WebP pictures.")
    ext = lambda r: IMAGE_FILE.search(str(r.get("filename") or r["local_name"])).group(1).lower().replace("jpeg", "jpg")  # noqa: E731
    job_id = store.new_job_id()
    title = (body.title or "").strip()[:80] or f"Sample edit {time.strftime('%d %b %H:%M')}"
    d = store.create_job_dir(job_id)
    store.files_dir(d).mkdir(parents=True, exist_ok=True)
    inputs = {
        "sample_url": body.sample_url.strip(), "sample_upload": sample, "product_refs": prods, "person_refs": people,
        "product_files": [f"product_{i}.{ext(r)}" for i, r in enumerate(prods, 1)],
        "person_files": [f"person_{i}.{ext(r)}" for i, r in enumerate(people, 1)],
        "product_name": body.product_name.strip(), "product_text": body.product_text.strip(), "size_note": body.size_note.strip(),
        "offer": body.offer.strip(), "notes": body.notes.strip(), "options": body.options.model_dump(),
        "requirements": body.notes.strip(), "product_url": "",
    }
    job = {"id": job_id, "title": title, "created_at": now_iso(), "status": "queued", "demo": False, "mode": SE.MODE,
           "session_id": None, "inputs": inputs, "summary": {}, "files_index": {}, "edit": {"status": "queued", "steps": SE.new_steps()}}
    store.write_job(d, job)
    store.write_events(d, [])
    if body.folder:
        name = _folder_name(body.folder)
        if name:
            store.set_folders([job_id], name)
    SE.start(job_id)
    return _derived(job_id)


@app.post("/api/jobs/{job_id}/edit/retry")
def retry_edit_job(job_id: str, body: RetryBody):
    d, job, _ = _load(job_id)
    if job.get("mode") != SE.MODE:
        raise HTTPException(400, "Only sample edits can be retried this way.")
    try:
        SE.retry(job_id, body.from_step)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return _derived(job_id)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, replay_t: Optional[float] = None):
    d0 = store.job_dir(job_id)
    if d0 is not None and replay_t is None and store.read_job(d0).get("mode") == SE.MODE:
        SE.resume_if_stale(job_id)  # the instance that ran it stopped: carry on here
    if replay_t is None and not SYNCER.is_running(job_id):  # serverless: the instance may have restarted
        d = store.job_dir(job_id)
        jj = store.read_job(d) if d is not None else {}
        if d is not None and not jj.get("demo") and not jj.get("frozen") and not jj.get("never_started") and jj.get("session_id"):
            SYNCER.start(job_id)
    return _derived(job_id, replay_t)


@app.get("/api/jobs/{job_id}/events")
def get_events(job_id: str, after: int = 0, replay_t: Optional[float] = None, limit: int = 2000):
    d, job, events = _load(job_id)
    from derive import replay_cut
    cut = replay_cut(events, replay_t) if replay_t is not None else events
    after = max(0, after)
    chunk = cut[after:after + limit]
    return {"events": [dict(compact_event(e), index=after + i) for i, e in enumerate(chunk)],
            "total": len(cut), "next": after + len(chunk), "has_more": after + len(chunk) < len(cut)}


class RenameBody(BaseModel):
    title: str


@app.post("/api/jobs/{job_id}/rename")  # POST: the cloud API Gateway does not route PATCH (404 before the app)
@app.patch("/api/jobs/{job_id}")
def rename_job(job_id: str, body: RenameBody):
    """Renames a job. Live jobs: job.json and the ModelArk session title (keeping its [job-...] suffix, so a fresh
    instance rebuilds the job under the new name). Shipped read-only examples: an override in the writable data folder."""
    d, job, _ = _load(job_id)
    title = " ".join((body.title or "").split())
    if not 1 <= len(title) <= 120:
        raise HTTPException(422, "The name must be 1 to 120 characters.")
    if job.get("demo") or job.get("example") or job.get("frozen"):
        store.set_title_override(job_id, title)
        return {"ok": True, "title": title}
    job["title"] = title
    store.write_job(d, job)
    sid = job.get("session_id")
    if sid:
        try:
            _client().post(f"/sessions/{sid}", {"title": f"{title} [{job_id}]"})
        except Exception as e:  # the name is saved here; only the copy on ModelArk could not be updated
            return {"ok": True, "title": title, "warning": f"Saved here, but the agent session title was not updated: {str(e)[:200]}"}
    return {"ok": True, "title": title}


class FolderBody(BaseModel):
    folder: Optional[str] = None  # None or "" = back to the main list
    job_ids: List[str] = []       # bulk move (POST /api/folders); ignored by the per-job route


def _folder_name(raw: Optional[str]) -> Optional[str]:
    name = " ".join((raw or "").split())
    if len(name) > 60:
        raise HTTPException(422, "A folder name is at most 60 characters.")
    return name or None


@app.post("/api/jobs/{job_id}/folder")
def move_job(job_id: str, body: FolderBody):
    """Puts one project in a folder (or back in the main list). Only the list grouping changes: the project, its
    agent session and its files stay as they are."""
    _load(job_id)
    folder = _folder_name(body.folder)
    store.set_folders([job_id], folder)
    return {"ok": True, "id": job_id, "folder": folder}


@app.post("/api/folders")
def move_jobs(body: FolderBody):
    """Bulk move: {"job_ids": [...], "folder": "Cost tests"}; unknown ids are skipped and reported."""
    folder = _folder_name(body.folder)
    known = {d.name for d in store.all_job_dirs()}
    try:
        import storage
        known |= set(storage.list_jobs())
    except Exception:
        pass
    known -= set(store.deleted_ids())
    ids = [j for j in dict.fromkeys(body.job_ids) if j in known]
    if ids:
        store.set_folders(ids, folder)
    return {"ok": True, "folder": folder, "moved": ids, "unknown": [j for j in body.job_ids if j not in known]}


@app.get("/api/folders")
def list_folders():
    """Folders with the number of projects that still exist in each; entries of deleted projects are pruned."""
    where = store.folders(fresh=True)
    alive = {d.name for d in store.all_job_dirs()}
    try:
        import storage
        alive |= set(storage.list_jobs())
    except Exception:
        pass
    alive -= set(store.deleted_ids())
    stale = [j for j in where if j not in alive and j.startswith("job-")]
    if stale and alive:  # an empty view (bucket not reachable yet) never prunes
        where = store.set_folders(stale, None)
    counts: Dict[str, int] = {}
    for j, f in where.items():
        if j in alive:
            counts[f] = counts.get(f, 0) + 1
    return {"folders": [{"name": k, "count": v} for k, v in sorted(counts.items())]}


# ----------------------------------------------------------------------------- caption editor
import captions as CAP  # noqa: E402


def _remote_for(job: dict):
    index = job.get("files_index") or {}

    def get(name: str) -> Optional[str]:
        meta = index.get(name) or {}
        if meta.get("tos"):  # kept in the bucket: a signed link that does not expire with the session
            import storage
            url = storage.signed_url(job["id"], f"files/{name}")
            if url:
                return url
        return _remote_url(job.get("session_id"), meta.get("id")) if meta.get("id") else None
    return get


def _persist_to_session(job: dict):
    def put(path: Path, mount: str) -> None:
        if not job.get("session_id"):
            return
        client = _client()
        if path.suffix.lower() == ".json":  # the Files API refuses JSON: keep it as a packed PNG (webfiles.py)
            import webfiles
            path, mount = webfiles.pack(path), webfiles.mount_for(mount)
        up = client.upload_file(str(path))
        client.wait_file_active(up["id"])
        client.add_resource(job["session_id"], up["id"], mount)
    return put


def _editor_context(job_id: str):
    d, job, events = _load(job_id)
    derived = derive_job(job, events, store.files_dir(d))
    names = {f["name"] for f in derived.get("files", [])} | set((job.get("files_index") or {}).keys())
    final = (derived.get("finals") or [{}])[-1].get("file") if derived.get("finals") else None
    return d, job, names, final


class CaptionDoc(BaseModel):
    doc: Dict[str, Any]


class CaptionPreview(BaseModel):
    kind: str = "caption"
    item: Dict[str, Any]
    size: Optional[List[int]] = None


@app.get("/api/jobs/{job_id}/captions")
def get_captions(job_id: str):
    d, job, names, final = _editor_context(job_id)
    doc = CAP.load_doc(d, names, final)
    exp = CAP.EXPORTS.get(job_id)
    edits = sorted((n for n in names if re.match(r"^final_edit_v\d+\.mp4$", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
    return {"doc": doc, "base_url": f"/api/jobs/{job_id}/files/{doc['base']}" if doc.get("base") else None,
            "edits": [{"name": n, "url": f"/api/jobs/{job_id}/files/{n}"} for n in edits], "export": exp,
            "read_only": bool(job.get("demo") or job.get("frozen"))}


@app.put("/api/jobs/{job_id}/captions")
def put_captions(job_id: str, body: CaptionDoc):
    d, job, _, _ = _editor_context(job_id)
    if job.get("demo") or job.get("frozen"):
        raise HTTPException(409, "This example is read-only.")
    return {"ok": True, "doc": CAP.save_doc(d, body.doc)}


@app.post("/api/jobs/{job_id}/captions/preview")
def preview_caption(job_id: str, body: CaptionPreview):
    import hashlib
    import tempfile
    d, job, names, final = _editor_context(job_id)
    doc = CAP.load_doc(d, names, final)
    if body.size:
        doc["size"] = body.size
    key = hashlib.sha1(json.dumps([body.kind, body.item, doc.get("size")], sort_keys=True).encode()).hexdigest()[:16]
    out = Path(tempfile.gettempdir()) / f"capprev_{job_id}_{key}.png"
    if not out.exists():
        try:
            CAP.preview_png(d, doc, body.kind, body.item, out, _remote_for(job))
        except SystemExit as e:  # assemble.fail(): e.g. the text wraps to too many lines
            raise HTTPException(422, "This caption does not fit: shorten it or use fewer lines.") from e
        except FileNotFoundError:
            raise HTTPException(404, "The inset picture is not available.")
    box = out.with_suffix(".box").read_text() if out.with_suffix(".box").exists() else "0,0,0,0"
    return FileResponse(str(out), media_type="image/png", headers={"Cache-Control": "max-age=3600", "X-Box": box,
                                                                     "Access-Control-Expose-Headers": "X-Box"})


@app.post("/api/jobs/{job_id}/captions/export")
def export_captions(job_id: str, body: CaptionDoc):
    d, job, _, _ = _editor_context(job_id)
    if job.get("demo") or job.get("frozen"):
        raise HTTPException(409, "This example is read-only.")
    doc = CAP.save_doc(d, body.doc)
    return CAP.start_export(job_id, d, doc, _remote_for(job), _persist_to_session(job))


@app.get("/api/jobs/{job_id}/captions/export")
def export_status(job_id: str):
    return CAP.EXPORTS.get(job_id) or {"state": "idle"}


# ----------------------------------------------------------------------------- direct generation and drafts
import generate as GEN  # noqa: E402


class GenerateBody(BaseModel):
    kind: str
    clip: str
    prompt: str
    inputs: List[Dict[str, Any]] = []
    model: Optional[str] = None
    duration: Optional[int] = None
    resolution: Optional[str] = None
    audio: bool = True
    payoff: Optional[str] = None


class AcceptBody(BaseModel):
    file: str
    checker_wrong: Optional[bool] = None  # when the automatic check failed the take: True = the check was wrong


COMPACT_INSTRUCTIONS = ("Keep: the brief (inputs, budget, options, model flags), the plan, product.spec and product.use word for word, "
                        "every file path written so far, paid_calls counts, the current STEP and what is left in it, open user requests, "
                        "and every lesson or check finding that is not yet fixed.")
FEEDBACK_REASONS = ("product", "person", "motion", "timing", "text", "setting", "taste")  # chips on the site


class TrimBody(BaseModel):
    clip: str
    file: str
    start: float
    end: float


class DraftBody(BaseModel):
    clip: str
    target: str = "clip"
    feedback: str
    model: Optional[str] = None
    attachments: List[Dict[str, Any]] = []
    reasons: List[str] = []  # what kind of problem the user saw (FEEDBACK_REASONS); "taste" is a preference, not a mistake


def _writable(job: dict):
    if job.get("demo") or job.get("frozen") or job.get("example"):
        raise HTTPException(409, "This example is read-only.")


@app.post("/api/jobs/{job_id}/generate")
def generate_take(job_id: str, body: GenerateBody):
    d, job, names, final = _editor_context(job_id)
    _writable(job)
    derived = derive_job(job, store.read_events(d), store.files_dir(d))
    remote = _remote_for(job)
    resolve = lambda n: CAP._local(d, n, remote) if n else None  # noqa: E731
    products = [p for p in (resolve(Path(str(pr.get("file") or pr.get("image") or "")).name) for pr in derived.get("products", [])) if p]
    try:
        return GEN.start(job_id, d, body.model_dump(), resolve, products, _persist_to_session(job))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(409, str(e))


@app.get("/api/jobs/{job_id}/generate")
def generate_status(job_id: str):
    d, job, _ = _load(job_id)
    run = GEN.RUNS.get(job_id)
    return {"run": {k: v for k, v in run.items() if k != "started"} if run else None, "takes": GEN.read_takes(d),
            "models": {"video": {k: v["label"] for k, v in GEN.VIDEO_MODELS.items()}, "image": {k: v["label"] for k, v in GEN.IMAGE_MODELS.items()}}}


@app.post("/api/jobs/{job_id}/takes/accept")
def accept_take(job_id: str, body: AcceptBody):
    d, job, _ = _load(job_id)
    _writable(job)
    try:
        take = GEN.accept(d, body.file)
        if body.checker_wrong is not None:
            take = GEN.mark_checker(d, body.file, body.checker_wrong)
            with store.lock_for(job_id):
                jj = store.read_job(d)
                jj.setdefault("calibration", []).append({"file": body.file, "clip": take.get("clip"), "verdict": (take.get("check") or {}).get("verdict"),
                                                         "checker_wrong": body.checker_wrong, "at": now_iso()})
                store.write_job(d, jj)
    except FileNotFoundError:
        raise HTTPException(404, "take not found")
    if take.get("kind") == "clip":  # "Use this take" and the scene picker are the same choice
        import scenes as SCN
        picks = SCN.load_picks(d)
        picks[take["clip"]] = {"file": take["file"], "include": True}
        SCN.save_picks(d, picks)
    rebuild = None
    if take.get("kind") == "clip" and not _delivered(d, job):
        # the agent has not assembled the ad yet: tell it to use this take for the clip (the file is in its uploads)
        msg = (f"USE TAKE: the user chose a take made on the site for {take['clip']}: /mnt/session/uploads/web_{take['file']} "
               f"(clip check {((take.get('check') or {}).get('verdict') or 'not run')}). Copy it into your clips folder as the current "
               f"take of {take['clip']} (keep the older takes as _vN), record it in clips.json, do not regenerate {take['clip']}, and "
               "continue the workflow where you left off. Answer in English.")
        try:
            _client().send_message(job["session_id"], msg)
            SYNCER.start(job_id)
            rebuild = {"state": "sent_to_agent"}
        except Exception as e:
            rebuild = {"state": "failed", "error": f"could not tell the agent: {str(e)[:200]}"}
        return {"ok": True, "take": take, "rebuild": rebuild}
    if take.get("kind") == "clip":  # the chosen take goes straight into the ad: rebuild the final with it
        _, _, names, final = _editor_context(job_id)
        remote = _remote_for(job)
        try:
            rebuild = GEN.rebuild(job_id, d, lambda n: CAP._local(d, n, remote) if n else None, CAP.load_doc(d, names, final),
                                  _persist_to_session(job), CAP.EXPORTS)
        except RuntimeError as e:
            rebuild = {"state": "failed", "error": str(e)}
    return {"ok": True, "take": take, "rebuild": rebuild}


@app.post("/api/jobs/{job_id}/takes/trim")
def trim_take(job_id: str, body: TrimBody):
    """Saves a part of a take as a new take of the scene (no generation, no cost). It is picked like any other take."""
    d, job, _ = _load(job_id)
    _writable(job)
    source = CAP._local(d, Path(body.file).name, _remote_for(job))
    if not source:
        raise HTTPException(404, "take not found")
    try:
        take = GEN.trim(d, body.clip, source, body.start, body.end, _persist_to_session(job))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    SYNCER.start(job_id)  # the next pass copies the new take to the bucket
    return {"ok": True, "take": take}


def _delivered(d: Path, job: dict) -> bool:
    """True once the agent has assembled the ad (an edit plan exists): only then can the site rebuild it."""
    if any(store.files_dir(d).glob("edit_plan*.json")):
        return True
    return any(re.match(r"edit_plan.*\.json$", n) for n in (job.get("files_index") or {}))


class ScenePicks(BaseModel):
    picks: Dict[str, Dict[str, Any]] = Field(default_factory=dict)  # {clip_01: {file, include}}
    extras: Optional[List[Dict[str, Any]]] = None  # the user's own clips: [{id, after, start, end, mute, include}]
    transitions: Optional[str] = None  # keep | cut | mix | a transition name
    apply: bool = False


class OwnClip(BaseModel):
    file_id: str = Field(..., max_length=120)
    filename: str = Field(..., max_length=300)


def _resolver(job_id: str, d: Path, job: dict):
    """A job file on this instance's disk for a rebuild: local, else its session or bucket link (a file made on
    another server instance, such as an uploaded own clip, is only in the bucket or the session at first)."""
    remote = _remote_for(job)
    return lambda n: CAP._local(d, n, lambda x: remote(x) or _fallback_url(job_id, job.get("session_id"), x)) if n else None


def _extras_out(job_id: str, d: Path) -> List[dict]:
    import scenes as SCN
    return [{**x, "url": f"/api/jobs/{job_id}/files/{x['file']}"} for x in SCN.load_extras(d)]


@app.get("/api/jobs/{job_id}/scenes")
def get_scenes(job_id: str):
    """Every scene with all its takes (agent's cut, its earlier takes, site takes) and the saved choice."""
    import scenes as SCN
    d, job, events = _load(job_id)
    derived = derive_job(job, events, store.files_dir(d))
    rows = SCN.options(d, store.files_dir(d), {**derived, "id": job_id}, GEN.read_takes(d))
    return {"scenes": rows, "assembled": _delivered(d, job), "export": CAP.EXPORTS.get(job_id), "extras": _extras_out(job_id, d),
            "transitions": SCN.load_transitions(d), "transition_names": SCN.transition_names()}


OWN_MAX_S = 120  # an intro, an outro or a scene: short pieces


@app.post("/api/jobs/{job_id}/own-clips")
def add_own_clip(job_id: str, body: OwnClip):
    """Registers a file the user uploaded for this job (POST .../references) as their own clip for the ad: a video is
    saved as H.264 MP4 in the job's files (own_<time>_<name>.mp4), a picture as it is. It appears in the scene list,
    where the user says where it goes; nothing is generated and nothing is paid."""
    import scenes as SCN
    d, job, _ = _load(job_id)
    _writable(job)
    safe = _safe_name(body.filename, "clip")
    ext = Path(safe).suffix.lower()
    kind = "image" if IMAGE_FILE.search(safe) else "video" if ext in (".mp4", ".mov", ".m4v", ".webm") else None
    if not kind:
        raise HTTPException(422, "Your own clip must be a video (MP4, MOV, WebM) or a picture (PNG, JPG, WebP).")
    updir = d / "uploads"
    local = sorted(updir.glob(f"*_{safe}"), key=lambda p: p.stat().st_mtime) if updir.is_dir() else []
    files = store.files_dir(d)
    files.mkdir(parents=True, exist_ok=True)
    stem = f"own_{int(time.time())}_{re.sub(r'[^A-Za-z0-9]+', '_', Path(safe).stem)[:40].strip('_') or 'clip'}"
    try:
        if local:
            src = local[-1]
        else:  # the upload reached another server instance: take it from the Files API
            url = (_client().get(f"/files/{body.file_id}") or {}).get("download_url")
            if not url:
                raise RuntimeError("the uploaded file is not available any more; upload it again")
            src = updir / f"{int(time.time())}_{safe}"
            updir.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=300) as r, open(src, "wb") as out:
                shutil.copyfileobj(r, out)
        if kind == "image":
            dest = files / f"{stem}{ext}"
            shutil.copyfile(src, dest)
            length = None
        else:
            import imageio_ffmpeg
            import subprocess
            ensure_ffmpeg()
            dest = files / f"{stem}.mp4"
            r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-t", str(OWN_MAX_S),
                                "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale='min(1080,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                                "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0 or not dest.exists():
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"this video could not be read ({r.stderr.strip()[-160:] or 'unknown format'})")
            length = GEN._duration(dest)
            if not length or length < 0.3:
                dest.unlink(missing_ok=True)
                raise RuntimeError("this video is too short or has no picture")
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    item = SCN.add_extra(d, dest.name, kind, length, body.filename)
    try:  # every server instance and a later session restore find it: bucket copy and a file in the agent session
        import storage
        if storage.enabled():
            storage.put_file(job_id, f"files/{dest.name}", dest)
        put = _persist_to_session(job)
        put(dest, f"web_{dest.name}")
        put(d / SCN.PICKS, f"web_{SCN.PICKS}")
    except Exception as e:
        print(f"[ui] {job_id}: own clip not kept in the session: {str(e)[:160]}", flush=True)
    SYNCER.start(job_id)
    return {"ok": True, "extra": {**item, "url": f"/api/jobs/{job_id}/files/{dest.name}"}}


@app.post("/api/jobs/{job_id}/own-clips/{extra_id}/remove")  # POST as well: some gateways do not route DELETE
@app.delete("/api/jobs/{job_id}/own-clips/{extra_id}")
def remove_own_clip(job_id: str, extra_id: str):
    """Takes an own clip off the list (its file stays stored; the next rebuild leaves it out)."""
    import scenes as SCN
    d, job, _ = _load(job_id)
    _writable(job)
    if not SCN.remove_extra(d, extra_id):
        raise HTTPException(404, "No such clip")
    try:
        _persist_to_session(job)(d / SCN.PICKS, f"web_{SCN.PICKS}")
    except Exception:
        pass
    return {"ok": True, "extras": _extras_out(job_id, d)}


@app.post("/api/jobs/{job_id}/scenes")
def set_scenes(job_id: str, body: ScenePicks):
    """Saves the picks; with apply, rebuilds the ad with them (or, before the agent's first cut, tells the agent)."""
    import scenes as SCN
    d, job, events = _load(job_id)
    _writable(job)
    path = SCN.save_picks(d, body.picks, body.extras, body.transitions)
    try:
        _persist_to_session(job)(path, f"web_{path.name}")
    except Exception as e:
        print(f"[ui] {job_id}: scene picks not kept in the session: {str(e)[:160]}", flush=True)
    if not body.apply:
        return {"ok": True, "state": "saved"}
    picks, extras, transitions = SCN.load_picks(d), SCN.load_extras(d), SCN.load_transitions(d)
    if _delivered(d, job):
        _, _, names, final = _editor_context(job_id)
        try:
            return GEN.rebuild(job_id, d, _resolver(job_id, d, job), CAP.load_doc(d, names, final),
                               _persist_to_session(job), CAP.EXPORTS, picks=picks, extras=extras, transitions=transitions)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
    derived = derive_job(job, events, store.files_dir(d))
    msg = SCN.agent_message(SCN.options(d, store.files_dir(d), {**derived, "id": job_id}, GEN.read_takes(d)), picks, extras, transitions)
    if not msg:
        return {"ok": True, "state": "unchanged"}
    try:
        _client().send_message(job["session_id"], msg)
    except Exception as e:
        raise HTTPException(502, f"Could not tell the agent: {str(e)[:200]}")
    SYNCER.start(job_id)
    return {"ok": True, "state": "sent_to_agent"}


@app.post("/api/jobs/{job_id}/rebuild")
def rebuild_final(job_id: str):
    d, job, names, final = _editor_context(job_id)
    _writable(job)
    if not _delivered(d, job):
        raise HTTPException(409, "The agent has not assembled this ad yet. Chosen takes are passed to the agent, which uses them when it builds the ad.")
    doc = CAP.load_doc(d, names, final)
    import scenes as SCN
    try:  # the user's own clips and transition choice stay in every rebuild
        return GEN.rebuild(job_id, d, _resolver(job_id, d, job), doc, _persist_to_session(job), CAP.EXPORTS,
                           picks=SCN.load_picks(d), extras=SCN.load_extras(d), transitions=SCN.load_transitions(d))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/jobs/{job_id}/draft")
def request_draft(job_id: str, body: DraftBody):
    """Asks the agent to turn short feedback (any language, optional pictures/videos) into a full English prompt and
    tagged inputs for one clip or still, WITHOUT generating: the draft file appears with the next sync."""
    d, job, _ = _load(job_id)
    _writable(job)
    if not job.get("session_id"):
        raise HTTPException(409, "This video has no agent session.")
    clip = re.sub(r"[^A-Za-z0-9_-]", "", body.clip)[:60]
    target = body.target if body.target in ("still", "clip", "both") else "clip"
    fb = " ".join(body.feedback.split())[:2000]
    if not fb:
        raise HTTPException(422, "Write what should change.")
    reasons = [r for r in body.reasons if r in FEEDBACK_REASONS]
    reason_part = f" | REASON: {', '.join(reasons)}" if reasons else ""
    lines = [f"DRAFT: clip {clip} | target: {target} | MODEL: {body.model or 'same as before'}{reason_part} | FEEDBACK: {fb}"]
    if not _delivered(d, job):
        lines.append("NOTE: the ad is not delivered yet. Write the draft, then CONTINUE THE WORKFLOW where you left off (same step, "
                     "no restart) without waiting for a reply. Only the draft's short summary is in the user's language; every "
                     "STEP line and other message stays in English.")
    atts = [a for a in body.attachments if str(a.get("path", "")).startswith("/mnt/session/uploads/")]
    if atts:
        lines.append("ATTACHMENTS:")
        lines += [f"- {a['path']} | {' '.join(str(a.get('note', '')).split())[:200]}" for a in atts]
    try:
        _client().send_message(job["session_id"], "\n".join(lines))
    except Exception as e:
        raise HTTPException(502, f"The agent could not be reached: {str(e)[:300]}")
    job["status"] = "running"
    # kept for the lessons harvest: which clip, what kind of problem, in the user's words
    job.setdefault("feedback", []).append({"clip": clip, "target": target, "reasons": reasons, "text": fb[:500], "at": now_iso()})
    store.write_job(d, job)
    SYNCER.start(job_id)
    return {"ok": True, "sent": lines[0]}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Deletes a job everywhere: stops its agent session if it is working, deletes the session on ModelArk (with its
    files), and removes the job from this site. Curated examples and demos are read-only and cannot be deleted."""
    d, job, _ = _load(job_id)
    if job.get("demo") or job.get("example") or job.get("frozen"):
        raise HTTPException(409, "This example is part of the app and cannot be deleted.")
    sid = job.get("session_id")
    if sid:
        client = _client()
        try:
            status = (client.get_session(sid) or {}).get("status")
            if status not in ("idle", "terminated"):
                client.interrupt(sid)  # the session must be idle before it can be deleted
                deadline = time.time() + 60
                while time.time() < deadline and (client.get_session(sid) or {}).get("status") not in ("idle", "terminated"):
                    time.sleep(3)
            client.delete(f"/sessions/{sid}")
        except Exception as e:
            if getattr(e, "status", None) != 404:  # already gone on ModelArk: still remove it here
                raise HTTPException(502, f"The agent session could not be deleted: {str(e)[:300]}")
    import shutil
    import storage
    store.mark_deleted([job_id])  # first, so no instance rebuilds it while the copies go
    removed = storage.delete_job(job_id)  # the bucket copy goes too: deleting means everywhere
    shutil.rmtree(d, ignore_errors=True)
    if job_id in store.folders(fresh=True):
        store.set_folders([job_id], None)
    return {"ok": True, "deleted": job_id, "bucket_objects": removed}


class PruneBody(BaseModel):
    """What to take out of a project after a wrong start. The caller reads the job first and names everything."""
    drop_events: List[str] = Field(default_factory=list)  # event ids
    edit_events: Dict[str, str] = Field(default_factory=dict)  # event id -> new text
    add_events: List[Dict[str, Any]] = Field(default_factory=list)  # e.g. status events that close a kept stretch of the log
    drop_file_ids: List[str] = Field(default_factory=list)  # session file ids
    drop_calls: List[str] = Field(default_factory=list)  # paid_calls.json ids (the first copies of each go)
    keep_messages_after: Optional[str] = None  # ISO time: the job's sent messages before it go
    sample_url: Optional[str] = None  # the session path of the right sample
    recut: bool = False  # the agent's newest cut is the ad, not the site's older edits
    edits_after: Optional[int] = None  # site versions up to this number are hidden


@app.post("/api/jobs/{job_id}/prune")
def prune_job(job_id: str, body: PruneBody):
    """Removes part of a project: the named log events, files (here, in the bucket, and from later syncs) and paid
    calls, so the page, tokens and cost show only what is left. Safe to repeat. The session must be idle."""
    d, job, _ = _load(job_id)
    if job.get("demo") or job.get("example") or job.get("frozen") or not job.get("session_id"):
        raise HTTPException(409, "This example is read-only.")
    if (_client().get_session(job["session_id"]) or {}).get("status") != "idle":
        raise HTTPException(409, "The project is still running. Try again when it has finished.")
    import storage
    with store.lock_for(job_id):
        job, events = store.read_job(d), store.read_events(d)
        gone, seen = set(body.drop_events), set()
        kept = [e for e in events if e.get("id") not in gone]
        for e in kept:
            seen.add(e.get("id"))
            if e.get("id") in body.edit_events:
                e["content"] = [{"type": "text", "text": body.edit_events[e["id"]]}]
        for a in body.add_events:
            if a.get("id") not in seen:
                t = ts_seconds(a) or 0
                kept.insert(next((i for i, e in enumerate(kept) if (ts_seconds(e) or 0) > t), len(kept)), a)
        store.write_events(d, kept)
        idx, stored = job.setdefault("files_index", {}), job.setdefault("tos_files", {})
        ids, fd = {*(job.get("dropped_file_ids") or ()), *body.drop_file_ids}, store.files_dir(d)
        names = [n for n, meta in idx.items() if meta.get("id") in ids]
        for name in names:
            del idx[name]
            stored.pop(name, None)
            (fd / name).unlink(missing_ok=True)
        job["dropped_file_ids"] = sorted(ids)
        if storage.enabled():
            storage.delete_keys([storage.key(job_id, f"files/{n}") for n in names] + [storage.key(job_id, "captions_doc.json")])
        if body.drop_calls:
            (d / "dropped_calls.json").write_text(json.dumps(body.drop_calls))
        if body.keep_messages_after:
            job["messages"] = [m for m in job.get("messages") or [] if m.get("at", "") >= body.keep_messages_after]
        if body.sample_url:
            job.setdefault("inputs", {})["sample_url"] = body.sample_url
            job["inputs"].pop("sample_upload", None)
        if body.recut:
            agent_recut(job, d)
        if body.edits_after is not None:
            job["edits_after"] = body.edits_after
        store.write_job(d, job)
        for stale in ("costs.json", "captions_doc.json"):  # worked out again from what is left
            (d / stale).unlink(missing_ok=True)
    r = SYNCER.sync_once(job_id)
    return {"ok": True, "events": len(kept), "files_dropped": len(names), "status": r.get("status")}


@app.post("/api/jobs/{job_id}/message")
def post_message(job_id: str, body: MessageBody):
    d, job, events = _load(job_id)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(422, "The message is empty.")
    if job.get("demo"):
        # demo jobs have no live session: the UI handles approval locally in replay mode
        return {"ok": True, "demo": True, "text": text}
    if job.get("frozen"):
        raise HTTPException(409, "This is a read-only copy of a delivered video. Start a new video to make changes.")
    if not job.get("session_id"):
        raise HTTPException(409, "This video was made without the agent, so there is no one to message. Use the caption editor or Try again.")
    client = _client()
    try:
        r = client.send_message(job["session_id"], text)
    except Exception as e:
        raise HTTPException(502, f"The message could not be sent: {str(e)[:300]}")
    with store.lock_for(job_id):
        job = store.read_job(d)
        job.setdefault("messages", []).append({"text": text, "at": now_iso()})
        job["status"] = "running"
        store.write_job(d, job)
    SYNCER.start(job_id)
    return {"ok": True, "accepted": [e.get("id") for e in (r or {}).get("data", [])], "status": "running"}


def _range_response(path: Path, request: Request) -> Response:
    size = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if ctype.startswith("image/"):  # showcase copies keep the name but may hold JPEG data: send the real type
        with open(path, "rb") as fh:
            head = fh.read(12)
        if head.startswith(b"\xff\xd8\xff"):
            ctype = "image/jpeg"
        elif head.startswith(b"\x89PNG"):
            ctype = "image/png"
        elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            ctype = "image/webp"
    rng = request.headers.get("range")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if not rng:
        return FileResponse(str(path), media_type=ctype, headers=headers)
    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    if not m:
        raise HTTPException(416, "bad range")
    start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2) or 0))
    end = int(m.group(2)) if m.group(2) and m.group(1) else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1

    def body():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)})
    return StreamingResponse(body(), status_code=206, media_type=ctype, headers=headers)


@app.api_route("/api/jobs/{job_id}/files/{name}", methods=["GET", "HEAD"])
def get_file(job_id: str, name: str, request: Request):
    d = store.job_dir(job_id)
    if d is None:
        raise HTTPException(404, "job not found")
    if "/" in name or name.startswith(".") or ".." in name:
        raise HTTPException(400, "bad file name")
    path = store.files_dir(d) / name
    if not path.is_file():
        meta = (store.read_job(d).get("files_index") or {}).get(name) or {}
        if meta.get("tos"):
            import storage
            url = storage.signed_url(job_id, f"files/{name}")
            if url:
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url, status_code=307)
        if meta.get("remote"):
            url = _remote_url(store.read_job(d).get("session_id"), meta.get("id"))
            if url:
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url, status_code=307)
        # several server instances: the page (or job list) may come from one that already has this file while this one
        # has not downloaded it yet. Serve it from the agent session or the bucket instead of a 404 the browser keeps.
        url = _fallback_url(job_id, store.read_job(d).get("session_id"), name)
        if url:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url, status_code=307)
        raise HTTPException(404, "file not found")
    return _range_response(path, request)


PART_GROUPS = ("final", "clips", "stills", "people", "products", "plan")


def _job_parts(job_id: str) -> List[dict]:
    """The pieces a client may want to re-edit with: final (with and without text), current clip takes, first-frame
    stills, AI people, product previews and the edit plan. Each: {group, name, url}."""
    d = store.job_dir(job_id)
    if d is None:
        raise HTTPException(404, "job not found")
    job = store.read_job(d)
    derived = derive_job(job, store.read_events(d), store.files_dir(d))
    names = {f["name"] for f in derived.get("files", [])}
    url = lambda n: f"/api/jobs/{job_id}/files/{n}"  # noqa: E731
    parts, seen = [], set()

    def add(group, name):
        if name and name in names and name not in seen:
            seen.add(name)
            parts.append({"group": group, "name": name, "url": url(name)})

    final = (derived.get("finals") or [{}])[-1].get("file") if derived.get("finals") else None
    add("final", "final.mp4" if "final.mp4" in names else final)
    for n in sorted(x for x in names if re.match(r"^final_edit_v\d+\.mp4$", x)):  # versions exported from the caption editor
        add("final", n)
    if "final_clean.mp4" in names:  # the delivered text-free master; older takes' clean copies are left out
        add("final", "final_clean.mp4")
    elif final and final.replace(".mp4", "_clean.mp4") in names:
        add("final", final.replace(".mp4", "_clean.mp4"))
    for c in derived.get("clips", []):
        add("clips", c.get("file"))
        for r in c.get("references") or []:
            if r.get("kind") == "first_frame":
                add("stills", r.get("file"))
    for n in sorted(names):
        if n.startswith("still_") and n.lower().endswith((".png", ".jpg", ".jpeg")):
            add("stills", n)
    for p in derived.get("people", []):
        add("people", p.get("file") or Path(str(p.get("image") or "")).name)
    for pr in derived.get("products", []):
        add("products", pr.get("file") or Path(str(pr.get("image") or "")).name)
    for n in ("edit_plan.json",):
        add("plan", n)
    return parts


@app.get("/api/jobs/{job_id}/parts")
def job_parts(job_id: str):
    return {"parts": _job_parts(job_id)}


@app.get("/api/jobs/{job_id}/bundle.zip")
def job_bundle(job_id: str):
    """Every part in one ZIP, in folders by kind (final/, clips/, stills/, people/, products/, plan/)."""
    import tempfile
    import urllib.request
    import zipfile
    d = store.job_dir(job_id)
    job = store.read_job(d) if d else {}
    parts = _job_parts(job_id)
    tmp = tempfile.NamedTemporaryFile(prefix=f"{job_id}_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_STORED) as zf:
        for part in parts:
            local = store.files_dir(d) / part["name"]
            arc = f"{part['group']}/{part['name']}"
            if local.is_file():
                zf.write(local, arc)
                continue
            meta = (job.get("files_index") or {}).get(part["name"]) or {}
            link = _remote_url(job.get("session_id"), meta.get("id")) if meta.get("remote") else None
            if link:
                try:
                    with urllib.request.urlopen(link, timeout=120) as r:
                        zf.writestr(arc, r.read())
                except Exception:
                    continue
    from starlette.background import BackgroundTask
    return FileResponse(tmp.name, media_type="application/zip", filename=f"{job_id}_parts.zip",
                        background=BackgroundTask(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name)))


_REMOTE: Dict[str, tuple] = {}


def _remote_url(session_id: Optional[str], file_id: Optional[str]) -> Optional[str]:
    """Fresh presigned download link of a session file (links last 24 h; the listing is cached for 10 minutes)."""
    if not session_id or not file_id:
        return None
    t, urls = _REMOTE.get(session_id, (0, {}))
    if time.time() - t > 600 or file_id not in urls:
        try:
            urls = {f["id"]: f.get("download_url") for f in _client().session_files(session_id)}
            _REMOTE[session_id] = (time.time(), urls)
        except Exception:
            return None
    return urls.get(file_id)


_BY_NAME: Dict[str, tuple] = {}  # session id -> (listed at, {file name: download link of its newest copy})


def _fallback_url(job_id: str, session_id: Optional[str], name: str) -> Optional[str]:
    """A link for a job file this instance has not synced yet: the agent session's newest file of that name (listing
    cached 10 minutes, refreshed at most every 30 s for an unknown name), else the bucket copy when one exists."""
    if session_id:
        t, urls = _BY_NAME.get(session_id, (0, {}))
        age = time.time() - t
        if age > 600 or (name not in urls and age > 30):
            try:
                newest: Dict[str, tuple] = {}
                for f in _client().session_files(session_id):
                    n = Path(f.get("filename") or "").name
                    if n and f.get("download_url") and f.get("status") in (None, "active") and (f.get("created_at") or 0) >= newest.get(n, (-1, ""))[0]:
                        newest[n] = (f.get("created_at") or 0, f["download_url"])
                urls = {n: u for n, (_, u) in newest.items()}
                _BY_NAME[session_id] = (time.time(), urls)
            except Exception:
                pass
        if urls.get(name):
            return urls[name]
    try:
        import storage
        if storage.enabled() and any(o["key"] == storage.key(job_id, f"files/{name}") for o in storage.list_keys(storage.key(job_id, f"files/{name}"))):
            return storage.signed_url(job_id, f"files/{name}")
    except Exception:
        pass
    return None


@app.post("/api/jobs/import")
def import_job(body: ImportBody):
    client = _client()
    export = load_module("export_job", TOOLS_DIR / "export_job.py")
    sid = body.session_id.strip()
    if not re.match(r"^[A-Za-z0-9_-]+$", sid):
        raise HTTPException(422, "bad session id")
    from sync import own_session
    if not own_session(client.get_session(sid) or {}):
        raise HTTPException(404, "session not found")  # another site's agent: not visible here
    if body.dest:
        dest = Path(body.dest)
        if not dest.is_absolute():
            dest = (RUNS_DIR.parent / dest).resolve()
    else:
        dest = (RUNS_DIR / f"case_{sid}") if body.demo else (DATA_DIR / sid)
    job = export.export_job(client, sid, dest, title=body.title, demo=body.demo)
    return {"ok": True, "job": job, "dest": str(dest)}


@app.post("/api/jobs/{job_id}/sync")
def sync_now(job_id: str):
    d, job, _ = _load(job_id)
    if job.get("demo"):
        return {"ok": True, "demo": True}
    _client()
    r = SYNCER.sync_once(job_id)
    SYNCER.start(job_id)  # make sure the job's watcher is alive and awake
    return r


@app.on_event("startup")
def _startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SHOWCASE:  # a showcase has no agent: its projects ship in the package
        threading.Thread(target=SYNCER.resume_all, daemon=True, name="resume").start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=os.environ.get("UI_HOST", "127.0.0.1"), port=int(os.environ.get("UI_PORT", "8787")),
                reload=False)


def _send_upload(dest: Path, safe: str, note: str, job_id: Optional[str] = None) -> dict:
    """Send a saved upload to the Files API; for an existing job, also mount it in its session under /mnt/session/uploads/."""
    client = _client()
    if job_id is None:
        try:  # several server instances: the edit mode reads uploads back from the bucket (sample_edit.fetch_input)
            import storage
            if storage.enabled() and dest.stat().st_size <= 300 * 1024 * 1024:
                storage.put_raw(f"uploads/files/{dest.name}", dest.read_bytes())
        except Exception as e:
            print(f"[ui] upload copy to the bucket: {type(e).__name__}: {str(e)[:160]}", flush=True)
        try:
            up = client.upload_file(str(dest))
            info = client.wait_file_active(up["id"])
        except Exception as e:
            raise HTTPException(502, f"Upload to ModelArk failed: {str(e)[:300]}")
        return {"file_id": up["id"], "filename": safe, "mount_name": safe, "mime_type": info.get("mime_type"), "bytes": info.get("bytes"),
                "note": note, "local_name": dest.name}
    job = store.read_job(store.job_dir(job_id))
    try:
        up = client.upload_file(str(dest))
        client.wait_file_active(up["id"])
        mount = f"ref_{int(time.time())}_{safe}"
        client.add_resource(job["session_id"], up["id"], mount)
    except Exception as e:
        raise HTTPException(502, f"Upload to the agent session failed: {str(e)[:300]}")
    return {"ok": True, "path": f"/mnt/session/uploads/{mount}", "filename": safe, "note": note, "file_id": up["id"]}


def _upload_dir(job_id: Optional[str]) -> Path:
    if job_id is None:
        return DATA_DIR.parent / "uploads"
    d = store.job_dir(job_id)
    if d is None:
        raise HTTPException(404, "Job not found")
    job = store.read_job(d)
    if job.get("demo") or not job.get("session_id"):
        raise HTTPException(400, "Demo jobs cannot take new references")
    return d / "uploads"


def _safe_name(name: Optional[str], default: str = "upload") -> str:
    """Letters, digits, dot, dash and underscore only. ModelArk refuses to mount a file whose name holds ".." ("file name
    contains unsafe characters"), so runs of dots become one dot and the name never starts with a dot."""
    safe = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "_", name or default)).lstrip(".")
    stem, dot, ext = safe.rpartition(".")
    if dot and len(safe) > 80:  # keep the extension when shortening
        safe = f"{stem[:79 - len(ext)]}.{ext}"
    return safe[:80] or default


@app.post("/api/uploads")
async def upload_reference(file: UploadFile = File(...), note: str = Form("")):
    """Uploads a reference image/video/audio to the Files API; the id is passed back in POST /api/jobs references."""
    if not file.filename:
        raise HTTPException(422, "No file")
    safe = _safe_name(file.filename)
    updir = _upload_dir(None)
    updir.mkdir(parents=True, exist_ok=True)
    dest = updir / f"{int(time.time())}_{safe}"
    with open(dest, "wb") as fh:
        fh.write(await file.read())
    return _send_upload(dest, safe, note)


@app.post("/api/uploads/chunk")
async def upload_chunk(chunk: UploadFile = File(...), upload_id: str = Form(...), index: int = Form(...), total: int = Form(...),
                       filename: str = Form(...), note: str = Form(""), job_id: str = Form("")):
    """One piece of a large file (gateways cap request bodies, e.g. 16 MB on API Gateway). Pieces are kept until all `total`
    have arrived; the last one joins them and sends the whole file on, answering like POST /api/uploads (or the job's
    references endpoint when job_id is set). Needs every piece to reach the same server instance."""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", upload_id) or not (0 <= index < total <= 400):
        raise HTTPException(422, "Bad upload_id, index or total")
    jid = job_id or None
    import storage
    safe = _safe_name(filename)
    if storage.enabled():  # several server instances: the pieces meet in the bucket, whichever instance gets each one
        prefix = f"uploads/parts_{upload_id}/"
        if not storage.put_raw(f"{prefix}{index:05d}", await chunk.read()):
            raise HTTPException(502, f"Upload piece {index + 1} of {total} could not be stored: {storage.status().get('last_error')}")
        keys = sorted(o["key"] for o in storage.list_keys(prefix))
        if len(keys) < total:
            return {"received": len(keys), "total": total}
        updir = _upload_dir(jid)
        updir.mkdir(parents=True, exist_ok=True)
        dest = updir / f"{int(time.time())}_{safe}"
        with open(dest, "wb") as out:
            for k in keys:
                data = storage.get_raw(k)
                if data is None:
                    raise HTTPException(502, "A piece of the upload could not be read back; upload the file again.")
                out.write(data)
        storage.delete_keys(keys)
        return _send_upload(dest, safe, note, jid)
    parts = _upload_dir(jid) / f"parts_{upload_id}"
    parts.mkdir(parents=True, exist_ok=True)
    with open(parts / f"{index:05d}", "wb") as fh:
        fh.write(await chunk.read())
    have = sorted(parts.iterdir())
    if len(have) < total:
        return {"received": len(have), "total": total}
    dest = parts.parent / f"{int(time.time())}_{safe}"
    with open(dest, "wb") as out:
        for part in have:
            out.write(part.read_bytes())
    shutil.rmtree(parts, ignore_errors=True)
    return _send_upload(dest, safe, note, jid)


@app.post("/api/jobs/{job_id}/references")
async def add_job_reference(job_id: str, file: UploadFile = File(...), note: str = Form("")):
    """Upload a file for an existing job and mount it in its session under /mnt/session/uploads/ (for regenerations)."""
    safe = _safe_name(file.filename, "reference")
    updir = _upload_dir(job_id)
    updir.mkdir(parents=True, exist_ok=True)
    dest = updir / f"{int(time.time())}_{safe}"
    with open(dest, "wb") as fh:
        fh.write(await file.read())
    return _send_upload(dest, safe, note, job_id)


# ----------------------------------------------------------------------------- web pages (static export) and sign-in
STATIC_DIR = Path(os.environ.get("UI_STATIC_DIR") or (Path(__file__).resolve().parent.parent / "web" / "out"))


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Password gate for shared deployments: UI_BASIC_AUTH="user:password" (unset = open, for local use)."""
    expected = os.environ.get("UI_BASIC_AUTH")
    if not expected or request.url.path == "/api/health":
        return await call_next(request)
    import base64
    import hmac
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            given = base64.b64decode(header[6:]).decode("utf-8", "replace")
            if hmac.compare_digest(given, expected):
                return await call_next(request)
        except Exception:
            pass
    return Response("Sign-in required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Ad Replicator", charset="UTF-8"'})


@app.get("/jobs/{job_id}")
def old_job_link(job_id: str, request: Request):
    from fastapi.responses import RedirectResponse
    q = str(request.url.query)
    return RedirectResponse(f"/job?id={job_id}" + (f"&{q}" if q else ""), status_code=307)


@app.get("/{path:path}", include_in_schema=False)
def web_page(path: str):
    """Serves the exported web app: exact file, <path>.html, <path>/index.html, else the 404 page."""
    if path.startswith("api/"):
        raise HTTPException(404, "Not found")
    root = STATIC_DIR.resolve()
    clean = path.strip("/")
    for cand in ([root / "index.html"] if not clean else [root / clean, root / f"{clean}.html", root / clean / "index.html"]):
        try:
            c = cand.resolve()
        except OSError:
            continue
        if c.is_file() and root in c.parents or c == root / "index.html" and c.is_file():
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if "/_next/static/" in f"/{clean}" else {"Cache-Control": "no-cache"}
            return FileResponse(c, headers=headers)
    nf = root / "404.html"
    if nf.is_file():
        return FileResponse(nf, status_code=404)
    raise HTTPException(404, "Web app not built: run npm run build in ui/web")
