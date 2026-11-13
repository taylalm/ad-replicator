"""Edit the sample: a finished ad from the client's own images in one Seedance video edit of the sample, without the agent.

The client gives the sample video, picture(s) of their personalized product and, optionally, the people who should
appear instead of the sample's (their own pictures, or new AI people the site makes). The site then:

1. cut      finds the sample's shot changes (ffmpeg scene score, as in ugc-video-analyzer) and saves one frame per shot;
2. plan     one call to a vision model (seed-2-0-lite, the quality gate's evaluator) reads those frames and the client's
            pictures and returns what to replace (the sample's product, its people), the sample's closing brand card to
            leave out, and new captions in the sample's voice; the script checks the answer and writes the Seedance prompt;
3. people   new AI people with Seedream when the client asked for them (one picture per person), else skipped;
4. pieces   cuts the sample at its own shot changes into pieces Seedance accepts (2.0 mini / fast: at most 15 s each,
            2.5: 30 s), keeps them in the bucket and adds each to the private asset library (a video with people is
            refused as a plain link: InputVideoSensitiveContentDetected); without an asset group, plain signed links;
5. edit     one Seedance edit per piece (omni_reference_task_type edit, ratio adaptive, duration -1, audio off), the
            client's pictures as @Image references and the piece as @Video1;
6. assemble joins the edited pieces (the first 0.4 s of every later piece left out: a dissolve in the sample shows the
            old product there), lays the captions and the end card on top (video-assembly, no agent);
7. check    the quality gate (C1-C8) on the finished ad.

Lessons from the 9 Nov 2026 tests (docs/EDIT_MODE_FINDINGS.md): cut only at shot changes, never inside a shot; name what
must change about a person (face, hair, glasses, clothes); an edit is billed on input + output seconds at the
with-video price. State lives in the job's job.json ("edit"); every step can be re-run (POST .../edit/retry).
"""
from __future__ import annotations

import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import store
from paths import REPO, ensure_ffmpeg

MODE = "sample_edit"
STEPS = [(1, "cut", "Find the shot changes"), (2, "plan", "Plan the edit"), (3, "people", "AI people"),
         (4, "pieces", "Prepare the pieces"), (5, "edit", "Seedance edit and shot check"), (6, "assemble", "Captions and end card"),
         (7, "check", "Quality check")]
STEP_N = {k: n for n, k, _ in STEPS}
PLANNER_MODEL = os.environ.get("EDIT_PLANNER_MODEL", "seed-2-0-lite-260428")
PIECE_MAX = {"2.5": 30.0, "2.0": 15.0, "2.0-fast": 15.0, "2.0-mini": 15.0}  # 2.0 series: input video at most 15.2 s
PIECE_MIN = 4.0
JOIN_TRIM_S = 0.4
OUTRO_MARGIN_S = 0.4
AUTO_REDO = int(os.environ.get("EDIT_AUTO_REDO", "1"))  # pieces re-edited after the shot check finds what was missed
SIZES = {"480p": [480, 864], "720p": [720, 1280]}
STALE_S = 240  # a run whose heartbeat is older than this is resumed by whichever instance opens the job
GATE = REPO / "skills" / "ugc-quality-gate" / "scripts" / "gate.py"
ASSEMBLE = REPO / "skills" / "video-assembly" / "scripts" / "assemble.py"
RUNS: Dict[str, threading.Thread] = {}
_RUN_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(f"[edit] {msg}", flush=True)


# ----------------------------------------------------------------------------- small helpers
def _ff() -> str:
    ensure_ffmpeg()
    exe = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if exe and os.path.exists(exe):
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: Path) -> dict:
    r = subprocess.run([_ff(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    t = r.stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", t)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    m = re.search(r"Video: .*?(\d{2,5})x(\d{2,5})", t)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    m = re.search(r"(\d+(?:\.\d+)?) fps", t)
    return {"duration": round(dur, 3) if dur else None, "width": w, "height": h, "fps": float(m.group(1)) if m else None}


def detect_shots(path: Path, duration: float, threshold: float = 0.3, min_len: float = 0.8) -> List[float]:
    """Shot start times (0 first). Two signals: ffmpeg's frame-to-frame scene score (hard cuts, the analyzer's method)
    and the colour change between frames half a second apart, which also finds dissolves (a 0.5 s dissolve never
    scores above 0.14 frame to frame: the 9 Nov dart board sample). A dissolve's cut time is its middle."""
    r = subprocess.run([_ff(), "-hide_banner", "-i", str(path), "-an", "-vf", f"scale=270:-2,select='gt(scene,{threshold})',showinfo",
                        "-f", "null", "-"], capture_output=True, text=True)
    cuts = [float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    fps, w, h, gap = 10, 36, 64, 5
    raw = subprocess.run([_ff(), "-v", "error", "-i", str(path), "-an", "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=600).stdout
    size = w * h * 3
    hists = []
    for i in range(len(raw) // size):
        f, hh = raw[i * size:(i + 1) * size], [0] * 64
        for j in range(0, size, 3):
            hh[(f[j] >> 6) * 16 + (f[j + 1] >> 6) * 4 + (f[j + 2] >> 6)] += 1
        hists.append(hh)
    score = [sum(abs(a - b) for a, b in zip(hists[i], hists[i + gap])) / (2 * w * h) for i in range(len(hists) - gap)]
    if score:
        med = sorted(score)[len(score) // 2]
        limit = max(0.18, 4 * med)
        i = 0
        while i < len(score):
            if score[i] <= limit:
                i += 1
                continue
            j = i
            while j + 1 < len(score) and score[j + 1] > limit:
                j += 1
            peak = max(range(i, j + 1), key=lambda k: score[k])
            cuts.append((peak + gap / 2) / fps)
            i = j + 1
    starts = [0.0]
    for t in sorted(cuts):
        if t - starts[-1] >= min_len and duration - t >= min_len:
            starts.append(round(t, 2))
    return starts


def grab(path: Path, t: float, dest: Path, width: int = 360) -> bool:
    subprocess.run([_ff(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, t):.2f}", "-i", str(path), "-frames:v", "1",
                    "-vf", f"scale={width}:-2", "-pix_fmt", "yuvj420p", "-q:v", "4", str(dest)], check=False)  # Seedance MP4s are limited-range
    return dest.is_file()


def small_jpeg(src: Path, dest: Path, width: int = 640) -> Path:
    subprocess.run([_ff(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", f"scale='min({width},iw)':-2", "-q:v", "4",
                    str(dest)], check=False)
    return dest if dest.is_file() else src


def b64(path: Path) -> str:
    mime = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def plan_pieces(starts: List[float], end: float, max_len: float, min_len: float = PIECE_MIN,
                prefer: Optional[List[float]] = None) -> List[Tuple[float, float]]:
    """Consecutive shots grouped into pieces of min_len..max_len s, cut only at shot changes unless one shot is longer
    than max_len. `prefer`: shot starts where a person to replace first comes into view. A new piece starts there when
    it can: Seedance replaces the people it sees from a piece's first shot, but kept the original couple when they came
    in at the end of a 12 s piece (9 Nov 2026 test), so the sample's own shot changes where people arrive are the cuts."""
    if end < min_len:
        raise ValueError(f"the sample is {end:.1f} s long; Seedance edits need at least {min_len:.0f} s")
    cuts = sorted(c for c in starts if 0 < c < end)
    want = sorted(c for c in prefer or [] if 0 < c < end)
    pieces, a = [], 0.0
    while True:
        fit = lambda c: a + min_len <= c <= a + max_len and end - c >= min_len  # noqa: E731
        early = [c for c in want if fit(c)]
        if early:
            b = early[0]
        elif end - a <= max_len:
            break
        else:
            ok = [c for c in cuts if fit(c)]
            b = max(ok) if ok else min(a + max_len, end - min_len)  # a shot longer than a piece: cut inside it
        pieces.append((round(a, 2), round(b, 2)))
        a = b
    pieces.append((round(a, 2), round(end, 2)))
    return pieces


def _the(what) -> str:
    """'blonde woman in a red sweater' -> 'the blonde woman in a red sweater' (the prompt reads 'Replace <what> with')."""
    w = str(what or "").strip().rstrip(".")[:160]
    return w if re.match(r"(?i)(the|a|an|her|his|their|both|each|every)\b", w) else f"the {w}"


def _shots(v, n: int) -> List[int]:
    return sorted({int(x) for x in v if isinstance(x, (int, float)) and 1 <= int(x) <= n}) if isinstance(v, list) else []


def _printed(words: List[str]) -> str:
    """['Leo', '20', '40'] -> "the numbers 20, 40 and the name 'Leo'"."""
    nums = [w for w in words if re.fullmatch(r"\d+", w)]
    names = [w for w in words if w not in nums]
    bits = ([f"the numbers {', '.join(nums)}"] if nums else []) + ([("the name " if len(names) == 1 else "the words ") +
                                                                   ", ".join(f"'{w}'" for w in names)] if names else [])
    return " and ".join(bits)


def tokens_per_s(resolution: str) -> float:
    """Seedance video tokens per second (width x height x 24 fps / 1024; 480p measured 9,687/s on 9 Nov 2026)."""
    w, h = {"480p": (480, 864), "720p": (720, 1280), "1080p": (1080, 1920)}[resolution]
    return w * h * 24 / 1024


def price_per_m(version: str, resolution: str) -> Optional[float]:
    import costs
    from generate import SEEDANCE_IDS
    try:
        return costs.seedance_price_per_m(SEEDANCE_IDS[version], resolution, True)
    except Exception:
        return None


def estimate(seconds: float, version: str, resolution: str, people_new: int = 0, image_model: str = "seedream-5.0-pro") -> dict:
    """Expected list-price cost of one run: input + output seconds at the with-video price, plus the small calls."""
    import costs
    p = price_per_m(version, resolution)
    seedance = round(2 * seconds * tokens_per_s(resolution) * p / 1e6, 2) if p else None
    img = costs.PRICES.get("seedream_flash_image" if "flash" in image_model else "seedream_image_small") or 0
    calls = 2 * (costs.PRICES.get("evaluator_call_estimate") or 0)
    total = round((seedance or 0) + people_new * img + calls, 2) if seedance is not None else None
    return {"seconds": round(seconds, 1), "seedance_usd": seedance, "seedream_usd": round(people_new * img, 3), "checks_usd": round(calls, 3),
            "total_usd": total, "price_per_m": p, "tokens_per_s": round(tokens_per_s(resolution))}


def copied(text: str, sample_text: List[str]) -> bool:
    """A caption that reuses the sample's words: 4+ words in a row or 60%+ similar (the gate's copied_caption rule),
    and, stricter than the gate, the sample's sentence re-worded: 50%+ similar or the same first four words in another
    order ("I finally found the ideal family toy..." for "Finally, I found the Christmas decor...")."""
    words = re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))
    grams = {" ".join(words[i:i + 4]) for i in range(len(words) - 3)}
    for s in sample_text:
        sw = re.findall(r"[a-z0-9']+", str(s).lower().replace("’", "'"))
        if grams & {" ".join(sw[i:i + 4]) for i in range(len(sw) - 3)}:
            return True
        if difflib.SequenceMatcher(None, text.lower(), str(s).lower()).ratio() >= 0.5:
            return True
        if len(words) >= 4 and len(sw) >= 4 and set(words[:4]) == set(sw[:4]):
            return True
    return False


def redo_note(issues: List[str], plan: dict) -> str:
    """The sentence added to a piece's prompt for its second edit: what the first one left unchanged."""
    bits = []
    for i, q in enumerate(plan.get("people") or [], 1):
        if any(x.startswith(f"original person {i} ") for x in issues):
            bits.append(f"{q['what']} kept the original face and hair in some shots: in EVERY shot, also where they come in later "
                        "or stand next to someone, the face and hair must be the new person's")
    if any(x.startswith("original product") for x in issues):
        bits.append("the original product was still visible in some shots: replace it everywhere")
    if any(x.startswith("caption left") for x in issues):
        bits.append("a caption of the original video was still visible: remove every caption and text overlay completely, leaving a "
                    "clean background where the text was")
    return ("This is a second attempt; in the first one " + "; ".join(bits) + ".") if bits else ""


def check_captions(items: list, sample_text: List[str], use_until: float) -> Tuple[List[dict], List[dict]]:
    """Captions in time order, inside the used part of the sample, not overlapping, at least 1.2 s, never the sample's
    words. Returns (kept, dropped with the reason)."""
    rows = []
    for c in items:
        try:
            rows.append((float(c.get("start")), float(c.get("end")), str(c.get("text") or "").strip()))
        except (TypeError, ValueError, AttributeError):
            continue
    kept, dropped, last = [], [], 0.0
    for a, b, txt in sorted(rows):
        a, b = max(a, last), min(b, use_until - 0.1)
        why = ("empty" if not txt else "too long" if len(txt) > 90 else "copies the sample" if copied(txt, sample_text)
               else "too short or after the end" if b - a < 1.2 else None)
        if why:
            dropped.append({"text": txt, "start": round(a, 2), "end": round(b, 2), "why": why})
            continue
        kept.append({"text": txt, "start": round(a, 2), "end": round(b, 2)})
        last = b
    return kept, dropped


# ----------------------------------------------------------------------------- job record
def new_steps() -> List[dict]:
    return [{"n": n, "key": k, "label": label, "status": "pending", "started_at": None, "finished_at": None, "message": None}
            for n, k, label in STEPS]


def derived_steps(job: dict, now_s: float) -> Tuple[dict, dict]:
    """(parsed, status) in the shape derive.parse_steps / derive_status return, from the job's own record."""
    from derive import parse_ts
    ed = job.get("edit") or {}
    steps = []
    for s in ed.get("steps") or new_steps():
        a, b = parse_ts(s.get("started_at")), parse_ts(s.get("finished_at"))
        elapsed = round((b - a).total_seconds()) if a and b else (round(now_s - a.timestamp()) if a and s["status"] == "running" else None)
        steps.append({"n": s["n"], "name": s["label"], "label": s["label"], "status": "done" if s["status"] == "skipped" else s["status"],
                      "skipped": s["status"] == "skipped", "started_at": s.get("started_at"), "finished_at": s.get("finished_at"),
                      "elapsed_s": max(0, elapsed) if elapsed is not None else None, "latest_message": s.get("message"),
                      "reason": s.get("message") if s["status"] == "failed" else None, "rounds": 1 if s["status"] != "pending" else 0})
    running = [s["n"] for s in steps if s["status"] == "running"]
    touched = [s["n"] for s in steps if s["status"] != "pending"]
    current = running[-1] if running else (touched[-1] if touched else None)
    status = ed.get("status") or "queued"
    if status == "running" and time.time() - float(ed.get("heartbeat") or 0) > STALE_S * 3 and not running:
        status = "failed"
    parsed = {"steps": steps, "current_step": current, "plan_text": None, "plan_at": None, "step_events": []}
    return parsed, {"status": status, "awaiting_approval": False, "awaiting_step": None, "phase": "running" if status == "running" else "idle"}


def public_state(job: dict) -> dict:
    """What the job page shows about the edit: options, the plan the model made, the pieces and the prompt."""
    ed = job.get("edit") or {}
    inp = job.get("inputs") or {}
    plan = ed.get("plan") or {}
    pieces = [{k: p.get(k) for k in ("n", "start", "end", "status", "tokens", "clip", "error")} for p in ed.get("pieces") or []]
    opts = inp.get("options") or {}
    secs = sum(p["end"] - p["start"] for p in pieces) if pieces else (plan.get("use_until") or (ed.get("sample") or {}).get("duration"))
    est = None
    if secs:
        try:
            est = estimate(secs, _version(opts), opts.get("resolution", "480p"),
                           len(plan.get("people") or []) if opts.get("people") == "generate" else 0, opts.get("image_model") or "seedream-5.0-pro")
            if os.environ.get("UI_SHOW_COST", "1") == "0":
                est = {k: v for k, v in est.items() if not k.endswith("_usd") and k != "price_per_m"}
        except Exception:
            est = None
    return {"status": ed.get("status"), "options": opts, "product_name": inp.get("product_name"), "product_text": inp.get("product_text"),
            "offer": inp.get("offer"), "notes": inp.get("notes"), "plan": {k: plan.get(k) for k in
            ("products", "people", "captions", "cta", "use_until", "printed_text", "new_product", "sample_text")} if plan else None,
            "shots": len(ed.get("shots") or []), "sample_s": (ed.get("sample") or {}).get("duration"), "pieces": pieces,
            "prompt": ed.get("prompt"), "via_assets": ed.get("via_assets"), "estimate": est, "error": ed.get("error"), "next": ed.get("next"),
            "running": is_running(job["id"])}


def errors_of(job: dict) -> List[dict]:
    ed = job.get("edit") or {}
    if ed.get("status") != "failed" or not ed.get("error"):
        return []
    return [{"kind": "edit_failed", "title": "The edit stopped", "text": ed["error"], "next": ed.get("next") or "Fix the cause, then press Try again.",
             "at": ed.get("failed_at"), "resolved": False}]


# ----------------------------------------------------------------------------- the run
class Stop(Exception):
    """A step failed in a way the client can fix (message for the page)."""

    def __init__(self, msg: str, next_hint: Optional[str] = None):
        super().__init__(msg)
        self.next_hint = next_hint


class Run:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.d = store.job_dir(job_id)
        if self.d is None:
            raise FileNotFoundError(job_id)
        self.files = store.files_dir(self.d)
        self.files.mkdir(parents=True, exist_ok=True)
        self._bodies: Dict[int, dict] = {}  # Seedance request bodies of this process (one automatic resend on an output refusal)

    # job record -------------------------------------------------------------
    def job(self) -> dict:
        return store.read_job(self.d)

    def update(self, fn: Callable[[dict], None], mirror: bool = False) -> dict:
        with store.lock_for(self.job_id):
            job = store.read_job(self.d)
            ed = job.setdefault("edit", {})
            fn(ed)
            ed["heartbeat"] = time.time()
            job["status"] = ed.get("status") or job.get("status")
            job["updated_at"] = _now()
            store.write_job(self.d, job)
        if mirror:
            self.mirror()
        return job

    def mirror(self) -> None:
        """Files and records into the bucket (other instances, and after a restart)."""
        try:
            import storage
            from sync import mirror_files, mirror_records
            if not storage.enabled():
                return
            with store.lock_for(self.job_id):
                job = store.read_job(self.d)
                mirror_files(None, job, self.d, limit=200)
                idx = job.setdefault("files_index", {})
                for name in job.get("tos_files") or {}:
                    idx.setdefault(name, {"bytes": job["tos_files"][name], "tos": True, "remote": False})
                store.write_job(self.d, job)
                mirror_records(job, self.d, force=True)
        except Exception as e:
            log(f"{self.job_id}: bucket copy: {type(e).__name__}: {str(e)[:160]}")

    def step(self, key: str, status: str, message: Optional[str] = None) -> None:
        def fn(ed):
            for s in ed["steps"]:
                if s["key"] == key:
                    s["status"] = status
                    if status == "running":
                        s["started_at"], s["finished_at"] = _now(), None
                    elif status in ("done", "failed", "skipped"):
                        s["finished_at"] = _now()
                        s["started_at"] = s.get("started_at") or s["finished_at"]
                    if message is not None:
                        s["message"] = message
        self.update(fn, mirror=status != "running")

    def done(self, key: str) -> bool:
        ed = self.job().get("edit") or {}
        return any(s["key"] == key and s["status"] in ("done", "skipped") for s in ed.get("steps") or [])

    def paid(self, entry: dict) -> None:
        self.update(lambda ed: ed.setdefault("paid", []).append({**entry, "at": _now()}))

    # inputs -----------------------------------------------------------------
    def fetch_input(self, ref: dict, dest: Path) -> Path:
        """An uploaded file (POST /api/uploads): this instance's copy, else the bucket's (uploads/files/<local_name>)."""
        if dest.is_file():
            return dest
        local = Path(str(ref.get("local_name") or "")).name
        src = store.DATA_DIR.parent / "uploads" / local if local else None
        if src and src.is_file():
            shutil.copyfile(src, dest)
            return dest
        import storage
        if local and storage.enabled():
            data = storage.get_raw(f"uploads/files/{local}")
            if data:
                dest.write_bytes(data)
                return dest
        if ref.get("url"):
            _download(ref["url"], dest)
            return dest
        raise Stop(f"The uploaded file {ref.get('filename') or local} is no longer on the server.", "Upload it again and start a new edit.")

    # steps ------------------------------------------------------------------
    def run(self) -> None:
        job = self.job()
        inp = job.get("inputs") or {}
        self.update(lambda ed: ed.update(status="running", error=None, next=None), mirror=True)
        try:
            for key, fn in (("cut", self.s_cut), ("plan", self.s_plan), ("people", self.s_people), ("pieces", self.s_pieces),
                            ("edit", self.s_edit), ("assemble", self.s_assemble), ("check", self.s_check)):
                if self.done(key):
                    continue
                self.step(key, "running")
                msg = fn(inp)
                if msg == "skipped":
                    self.step(key, "skipped", "Not needed for this video.")
                else:
                    self.step(key, "done", msg)
                self.write_costs()
            self.update(lambda ed: ed.update(status="done", finished_at=_now()), mirror=True)
            log(f"{self.job_id}: done")
        except Exception as e:
            hint = e.next_hint if isinstance(e, Stop) else None
            text = str(e) if isinstance(e, Stop) else f"{type(e).__name__}: {str(e)[:500]}"
            running = next((s["key"] for s in (self.job().get("edit") or {}).get("steps") or [] if s["status"] == "running"), None)
            if running:
                self.step(running, "failed", text[:600])
            self.update(lambda ed: ed.update(status="failed", error=text[:800], next=hint, failed_at=_now()), mirror=True)
            self.write_costs()
            log(f"{self.job_id}: failed: {text[:300]}")

    def s_cut(self, inp: dict) -> str:
        sample = self.files / "sample.mp4"
        if not sample.is_file():
            if inp.get("sample_upload"):
                self.fetch_input(inp["sample_upload"], sample)
            elif inp.get("sample_url"):
                _download(inp["sample_url"], sample)
            else:
                raise Stop("No sample video was given.")
        for refs, names in (("product_refs", "product_files"), ("person_refs", "person_files")):
            for ref, name in zip(inp.get(refs) or [], inp.get(names) or []):
                self.fetch_input(ref, self.files / name)
        info = probe(sample)
        if not info["duration"] or not info["width"]:
            raise Stop("The sample is not a video the site can read.", "Upload an MP4 file.")
        starts = detect_shots(sample, info["duration"])
        shots = []
        for i, a in enumerate(starts):
            b = starts[i + 1] if i + 1 < len(starts) else info["duration"]
            f = self.files / f"scene_{i + 1:02d}.jpg"
            grab(sample, (a + b) / 2, f)
            shots.append({"n": i + 1, "start": a, "end": round(b, 2), "frame": f.name})
        self.update(lambda ed: ed.update(sample=info, shots=shots))
        return f"{len(shots)} shots in {info['duration']:.1f} s."

    def s_plan(self, inp: dict) -> str:
        ed = self.job()["edit"]
        shots, info = ed["shots"], ed["sample"]
        opts = inp.get("options") or {}
        people_mode = opts.get("people", "keep")
        products = [self.files / n for n in inp.get("product_files") or []]
        people_imgs = [self.files / n for n in inp.get("person_files") or []] if people_mode == "upload" else []
        content: List[dict] = [{"type": "text", "text": f"The sample video is {info['duration']:.1f} s long. One frame per shot follows."}]
        tmp = Path(tempfile.mkdtemp(prefix="plan_"))
        pick = shots if len(shots) <= 14 else [shots[round(i * (len(shots) - 1) / 13)] for i in range(14)]
        for s in pick:
            content.append({"type": "text", "text": f"Shot {s['n']}: {s['start']:.1f}-{s['end']:.1f} s"})
            content.append({"type": "image_url", "image_url": {"url": b64(self.files / s["frame"])}})
        for i, p in enumerate(products, 1):
            content.append({"type": "text", "text": f"Product image {i} (the client's personalized product)"})
            content.append({"type": "image_url", "image_url": {"url": b64(small_jpeg(p, tmp / f"p{i}.jpg"))}})
        for i, p in enumerate(people_imgs, 1):
            content.append({"type": "text", "text": f"Person image {i} (a person the client wants in the ad)"})
            content.append({"type": "image_url", "image_url": {"url": b64(small_jpeg(p, tmp / f"h{i}.jpg"))}})
        people_rule = {
            "keep": "The people stay as they are: return \"people\": [].",
            "upload": f"The client gave {len(people_imgs)} person image(s). List every person visible in the sample and set \"image\" to the "
                      "number of the person image that replaces them (match gender and age where you can; use each image at most once), or "
                      "null to keep that person; \"new_look\": that image's person in words: hair colour and style, skin tone, glasses or "
                      "not, nothing about clothes.",
            "generate": "The client wants new AI people. List each person visible in the sample (at most 3, the most visible first) with "
                        "\"image\": null, \"new_person\": a description for a photo generator of a clearly DIFFERENT person of the same "
                        "gender, age range and role, wearing the SAME clothes as the original person: a very different hair colour and "
                        "hair style (e.g. dark hair in a bun for a blonde with loose hair), another skin tone or face, glasses only if the "
                        "original has none; and \"new_look\": the same person in a few words without clothes (e.g. 'dark brown hair "
                        "tied up in a bun, olive skin, no glasses').",
        }[people_mode]
        extra = (inp.get("notes") or "").strip()
        content.append({"type": "text", "text": (
            "Return only this JSON object:\n"
            "{\"sample_text\": [every caption or on-screen text you can read in the frames, verbatim],\n"
            " \"outro_start\": seconds where a closing brand, logo, website or text-only card starts (no scene, only a card), or null,\n"
            " \"setting\": \"one sentence: place, light, mood\",\n"
            " \"hook\": \"one sentence: what happens in the first 2-3 s\",\n"
            " \"products\": [{\"what\": \"how to recognise the sample's product in the video, e.g. 'the personalized wooden Christmas sign'\","
            " \"image\": number of the product image that replaces it, \"handling\": \"what happens to it in the sample, e.g. 'unwrapped from "
            "brown paper, then hung on the wall hook and on the front door'\"}],\n"
            " \"new_product\": \"what the client's product looks like in its image(s): type, shape, material, colours, size cues, printed design "
            "and every word printed on it\",\n"
            " \"printed_text\": [every name or word printed on the client's product, exactly as spelled],\n"
            " \"people\": [{\"what\": \"a short way to tell this person apart in the sample, at most 6 words, e.g. 'the blonde woman', 'the "
            "man with glasses'\", \"image\": number or null, \"new_person\": \"...\", \"new_look\": \"...\", \"shots\": [the numbers of the "
            "shots where this person is visible]}],\n"
            " \"captions\": [{\"text\": \"...\", \"start\": seconds, \"end\": seconds}],\n"
            " \"cta\": \"a short call to action for the end card, e.g. 'Personalize yours today'\"}\n\n"
            "Rules: list every distinct product of the sample that the client's product replaces (one entry each; when the client gave one "
            "image per product, map them in order). Hands alone are not people. " + people_rule + " Captions: new words in the SAME voice "
            "and pattern as the sample's text (first person, playful; the same number of lines and the same roles; if the sample holds one "
            "caption over the whole video, write one), never 4 or more words in a row from the sample and never the sample's sentence re-worded (do not open the way it opens), 1-2 short lines (at most 60 "
            "characters), not overlapping, each at least 1.5 s, all before outro_start; no prices, no offers, no emojis the sample does not "
            "use. If the sample has no text, write 2-3 short captions anyway."
            + (f"\n\nThe client's notes: {extra[:800]}" if extra else ""))})
        system = ("You plan a video edit. A client re-makes a sample ad by editing its footage with an AI video editor: the sample's "
                  "product (and, when asked, its people) are replaced with the client's pictures; camera, motion, timing and setting stay. "
                  "Answer with one JSON object and nothing else.")
        res = _chat(PLANNER_MODEL, [{"role": "system", "content": system}, {"role": "user", "content": content}])
        shutil.rmtree(tmp, ignore_errors=True)
        usage = res.get("usage") or {}
        self.paid({"kind": "planner", "model": PLANNER_MODEL, "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})
        text = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            plan = json.loads(m.group(0)) if m else None
        except ValueError:
            plan = None
        if not isinstance(plan, dict):
            raise Stop("The planning model did not return a plan.", "Press Try again.")
        (self.files / "edit_mode_plan_raw.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))  # the model's own answer
        plan = self.check_plan(plan, inp, info, shots, len(products), len(people_imgs))
        copies = [c for c in plan["dropped_captions"] if c["why"] == "copies the sample"]
        if copies and plan["sample_text"]:  # one text-only rewrite: the gate fails a caption that reuses the sample's words
            ask = ("Sample on-screen text:\n" + "\n".join(f"- {t}" for t in plan["sample_text"]) + "\n\nThese new captions reuse the "
                   "sample's words:\n" + "\n".join(f"- {c['text']} ({c['start']}-{c['end']} s)" for c in copies) + "\n\nRewrite each one in "
                   "completely new words: the same meaning, voice and times, never 4 or more words in a row from the sample text, at most 60 "
                   "characters. Return only {\"captions\": [{\"text\": \"...\", \"start\": seconds, \"end\": seconds}]}.")
            res2 = _chat(PLANNER_MODEL, [{"role": "user", "content": ask}], max_tokens=800)
            u2 = res2.get("usage") or {}
            self.paid({"kind": "planner", "model": PLANNER_MODEL, "input_tokens": u2.get("prompt_tokens"), "output_tokens": u2.get("completion_tokens")})
            m2 = re.search(r"\{.*\}", ((res2.get("choices") or [{}])[0].get("message") or {}).get("content") or "", re.S)
            try:
                again = json.loads(m2.group(0)).get("captions") if m2 else []
            except (ValueError, AttributeError):
                again = []
            keep = [c for c in plan["captions"]]
            new_caps, dropped2 = check_captions(keep + list(again or []), plan["sample_text"], plan["use_until"])
            plan["captions"], plan["dropped_captions"] = new_caps, [c for c in plan["dropped_captions"] if c not in copies] + dropped2
        (self.files / "edit_mode_plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
        self.update(lambda ed: ed.update(plan=plan))
        bits = [f"Replace {', '.join(p['what'] for p in plan['products'])}"]
        if plan["people"]:
            bits.append(f"{len([p for p in plan['people'] if p.get('image') or p.get('new_person')])} person(s)")
        if plan.get("use_until") and plan["use_until"] < info["duration"] - 0.2:
            bits.append(f"sample used to {plan['use_until']:.1f} s (closing card left out)")
        return "; ".join(bits) + f"; {len(plan['captions'])} new caption(s)."

    def check_plan(self, plan: dict, inp: dict, info: dict, shots: list, n_products: int, n_people: int) -> dict:
        """The model's answer made safe to use: indices in range, a closing card only near the end, captions original."""
        opts = inp.get("options") or {}
        dur = float(info["duration"])
        prods = [p for p in plan.get("products") or [] if isinstance(p, dict) and str(p.get("what") or "").strip()]
        if not prods:
            raise Stop("The planner could not find the product in the sample.", "Describe the sample's product in the notes and try again.")
        for i, p in enumerate(prods):
            k = p.get("image")
            p["image"] = int(k) if isinstance(k, (int, float)) and 1 <= int(k) <= n_products else min(i + 1, n_products)
            p["what"] = _the(p["what"])
            p["handling"] = str(p.get("handling") or "").strip().rstrip(".")[:240]
        people = []
        for p in plan.get("people") or []:
            if not isinstance(p, dict) or not str(p.get("what") or "").strip():
                continue
            mode = opts.get("people", "keep")
            if mode == "upload":
                k = p.get("image")
                if isinstance(k, (int, float)) and 1 <= int(k) <= n_people and int(k) not in [q.get("image") for q in people]:
                    people.append({"what": _the(p["what"]), "image": int(k), "new_look": str(p.get("new_look") or "").strip().rstrip(".")[:200],
                                   "shots": _shots(p.get("shots"), len(shots))})
            elif mode == "generate" and str(p.get("new_person") or "").strip() and len(people) < 3:
                people.append({"what": _the(p["what"]), "new_person": str(p["new_person"])[:400],
                               "new_look": str(p.get("new_look") or "").strip().rstrip(".")[:200], "shots": _shots(p.get("shots"), len(shots))})
        outro = plan.get("outro_start")
        use_until = dur
        if isinstance(outro, (int, float)) and dur * 0.6 <= float(outro) < dur - 0.3 and float(outro) >= PIECE_MIN:
            use_until = min((s["start"] for s in shots if abs(s["start"] - float(outro)) < 1.0), default=float(outro))
            # a shot start is the middle of a dissolve: stop before it, or the brand card shows through the last frames
            # (Seedance refused such a piece: OutputVideoSensitiveContentDetected "copyright", 9 Nov 2026)
            use_until -= OUTRO_MARGIN_S
        if opts.get("use_until"):
            use_until = min(use_until, float(opts["use_until"]))
        sample_text = [str(t)[:200] for t in plan.get("sample_text") or [] if str(t).strip()][:20]
        caps, dropped = check_captions(plan.get("captions") or [], sample_text, use_until)
        cta = str(plan.get("cta") or "").strip().rstrip(".!")
        if not cta or len(cta) > 28 or re.search(r"[$€£]|\d+\s*%|\bfree\b|\boff\b", cta, re.I):  # longer wraps off the end card
            cta = "Personalize yours today"
        printed = [t.strip() for t in str(inp.get("product_text") or "").split(",") if t.strip()]
        for t in plan.get("printed_text") or []:  # the client's names first, then what the model read (e.g. ring numbers)
            t = str(t).strip()[:40]
            if t and t.lower() not in [x.lower() for x in printed]:
                printed.append(t)
        printed = printed[:10]
        return {"sample_text": sample_text, "use_until": round(use_until, 2), "setting": str(plan.get("setting") or "")[:200],
                "hook": str(plan.get("hook") or "")[:200], "products": prods, "new_product": str(plan.get("new_product") or "")[:400],
                "printed_text": printed, "people": people, "captions": caps, "dropped_captions": dropped, "cta": cta}

    def s_people(self, inp: dict) -> str:
        opts = inp.get("options") or {}
        plan = self.job()["edit"]["plan"]
        if opts.get("people") != "generate" or not plan["people"]:
            return "skipped"
        from generate import SEEDREAM_IDS, _request
        from models import IMAGE_MODELS
        m = IMAGE_MODELS.get(opts.get("image_model") or "seedream-5.0-pro") or IMAGE_MODELS["seedream-5.0-pro"]
        made = []
        for i, p in enumerate(plan["people"], 1):
            dest = self.files / f"person_{i}.png"
            if not dest.is_file():
                prompt = (f"Photorealistic smartphone photo of {p['new_person']}. Upper body, facing the camera, relaxed natural expression, "
                          "plain light-grey background, soft daylight, sharp focus on the face. No text, no logo, no watermark.")
                body = {"model": m.get("endpoint") or SEEDREAM_IDS[m["version"]], "prompt": prompt, "size": "1.5K",
                        "response_format": "url", "watermark": False, "output_format": "png"}
                r = _request("POST", "/images/generations", body, timeout=240)
                url = ((r.get("data") or [{}])[0]).get("url")
                if not url:
                    raise Stop(f"Seedream returned no picture for person {i}: {json.dumps(r)[:200]}", "Press Try again.")
                _download(url, dest)
                self.paid({"kind": "seedream", "model": m["version"], "images": 1, "file": dest.name})
                p["prompt"] = prompt
            p["image_file"] = dest.name
            made.append(dest.name)
        self.update(lambda ed: ed["plan"].update(people=plan["people"]))
        (self.files / "people_prompts.json").write_text(json.dumps([{"name": f"Person {i}", "file": f, "prompt": p.get("prompt")}
                                                                   for i, (f, p) in enumerate(zip(made, plan["people"]), 1)], indent=1))
        return f"{len(made)} new AI person picture(s)."

    def s_pieces(self, inp: dict) -> str:
        import assets_lib
        opts = inp.get("options") or {}
        ed = self.job()["edit"]
        version = _version(opts)
        sample = self.files / "sample.mp4"
        if not sample.is_file():
            self.fetch_input(inp.get("sample_upload") or {"url": inp.get("sample_url")}, sample)
        by_n = {s["n"]: s["start"] for s in ed["shots"]}
        arrive = []  # shot starts where a person to replace comes into view (not in the shot before)
        for p in ed["plan"].get("people") or []:
            seen = set(p.get("shots") or [])
            arrive += [by_n[n] for n in sorted(seen) if n > 1 and n - 1 not in seen and n in by_n]
        cuts = plan_pieces([s["start"] for s in ed["shots"]], ed["plan"]["use_until"], PIECE_MAX[version], prefer=arrive)
        use_assets = assets_lib.status()["ready"]
        pieces = list(ed.get("pieces") or [])
        if [(p["start"], p["end"]) for p in pieces] != cuts:
            pieces = [{"n": i + 1, "start": a, "end": b} for i, (a, b) in enumerate(cuts)]
        for p in pieces:
            # the file name carries the cut: a piece re-planned with other bounds never reuses an older cut (it did once)
            f = self.files / f"sample_piece_{p['n']:02d}_{p['start']:.2f}-{p['end']:.2f}.mp4"
            if p.get("file") != f.name:
                for k in ("video", "asset_id", "task_id", "status", "clip", "tokens", "error"):
                    p.pop(k, None)
            if not f.is_file():
                subprocess.run([_ff(), "-v", "error", "-y", "-ss", f"{p['start']:.2f}", "-t", f"{p['end'] - p['start']:.2f}", "-i", str(sample),
                                "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                                "-an", str(f)], check=True, timeout=600)
            p["file"] = f.name
            if p.get("video"):
                continue
            url = _host(self.job_id, f)
            if use_assets:
                try:
                    p["asset_id"] = assets_lib.add_video(url, f"{self.job_id}-piece-{p['n']}")
                except Exception as e:
                    raise Stop(f"The asset library refused piece {p['n']}: {str(e)[:300]}",
                               "Check the asset library set-up (ASSET_GROUP_ID and its access key), then press Try again.")
                p["video"] = f"asset://{p['asset_id']}"
            else:
                p["video"] = url
            self.update(lambda ed, ps=json.loads(json.dumps(pieces)): ed.update(pieces=ps))
        self.update(lambda ed: ed.update(pieces=pieces, via_assets=use_assets), mirror=True)
        secs = sum(p["end"] - p["start"] for p in pieces)
        return (f"{len(pieces)} piece(s), {secs:.1f} s of the sample" + (", added to the asset library." if use_assets else
                ", as signed links (no asset library set up: samples with people may be refused)."))

    def prompt(self, inp: dict, plan: dict) -> Tuple[str, List[Path]]:
        """The Seedance edit prompt (the 9 Nov 2026 tested wording) and its reference pictures, @Image1.. in this order."""
        opts = inp.get("options") or {}
        refs: List[Path] = []
        parts = ["Edit the video @Video1."]
        for p in plan["people"]:
            img = self.files / (p.get("image_file") or (inp.get("person_files") or [None])[int(p.get("image") or 1) - 1] or "")
            if not img.is_file():
                continue
            refs.append(img)
            k = len(refs)
            # the 9 Nov wording that replaced the people in every shot: the new person described in words, the original clothes
            # kept (asked to change the clothes too, Seedance changed only the clothes and kept the original face)
            look = f"a different person with {p['new_look']} and their own face exactly as in @Image{k}" if p.get("new_look") else \
                f"a different person with the face, hair and hair colour exactly as in @Image{k}"
            parts.append(f"Replace {p['what']} everywhere they appear with the person of @Image{k}: {look}; they keep {p['what']}'s clothes, "
                         "pose, movements, timing and place in every shot.")
        prod_ref: Dict[int, int] = {}
        for p in plan["products"]:
            if p["image"] not in prod_ref:
                refs.append(self.files / inp["product_files"][p["image"] - 1])
                prod_ref[p["image"]] = len(refs)
            k = prod_ref[p["image"]]
            text = f", with {_printed(plan.get('printed_text') or [])} printed exactly as in @Image{k}" if plan.get("printed_text") else ""
            desc = f" ({plan['new_product'].strip().rstrip('.')})" if plan.get("new_product") and len(prod_ref) == 1 else ""
            size = f" {inp['size_note'].strip().rstrip('.')[:160]}." if inp.get("size_note") else ""
            parts.append(f"Replace {p['what']} everywhere it appears with the personalized product of @Image{k}{desc}{text}; the printed side faces "
                         f"the camera and is never mirrored or misspelled; it is handled exactly where and how the original is"
                         f"{': ' + p['handling'] if p.get('handling') else ''}.{size}")
        if opts.get("people", "keep") == "keep":
            parts.append("Keep the people, their faces, clothes and movements as in @Video1.")
        elif any(p.get("image_file") or p.get("image") for p in plan["people"]):
            parts.insert(1 + len([q for q in plan["people"] if q.get("image_file") or q.get("image")]), "Nobody from the original video stays.")
        parts.append("Remove all on-screen text, captions, subtitles, stickers, logos and watermarks. Keep the camera moves, lighting, setting "
                     "and timing of @Video1. No new text, no logos, no watermarks.")
        return " ".join(parts), refs

    def s_edit(self, inp: dict) -> str:
        """One Seedance edit per piece; then every shot of every piece is compared with the sample (verify_piece) and a
        piece that still shows an original person, the original product or the sample's captions is edited once more
        with a prompt that names what was missed (AUTO_REDO times)."""
        from models import VIDEO_MODELS
        opts = inp.get("options") or {}
        ed = self.job()["edit"]
        version, res = _version(opts), opts.get("resolution", "480p")
        m = VIDEO_MODELS[f"seedance-{version}"]
        prompt, refs = self.prompt(inp, ed["plan"])
        prompt = prompt[:2500]
        pieces = ed["pieces"]
        self.update(lambda e: e.update(prompt=prompt))
        todo = [p for p in pieces if not (p.get("task_id") and p.get("status") not in ("failed", "cancelled", "expired"))]
        self.submit(todo, pieces, prompt, refs, m, version, res)
        self.wait(pieces, version)
        redone, left = 0, {}
        if ed["plan"].get("products") and opts.get("verify", True):
            for rnd in range(AUTO_REDO + 1):
                for p in pieces:
                    if p.get("verified_for") != p.get("clip_task"):
                        p["issues"] = self.verify_piece(inp, ed, p)
                        p["verified_for"] = p.get("clip_task")
                        self.update(lambda e, ps=json.loads(json.dumps(pieces)): e.update(pieces=ps))
                bad = [p for p in pieces if p.get("issues")]
                if not bad or rnd == AUTO_REDO:
                    left = {p["n"]: p["issues"] for p in bad}
                    break
                for p in bad:
                    keep = self.files / f"clip_{p['n']:02d}_try{len(p.get('tries') or []) + 1}.mp4"
                    if p.get("clip") and (self.files / p["clip"]).is_file():
                        shutil.copyfile(self.files / p["clip"], keep)
                    p.setdefault("tries", []).append({"task_id": p.get("task_id"), "file": keep.name, "issues": p["issues"]})
                    p["extra"] = redo_note(p["issues"], ed["plan"])
                    for k in ("task_id", "status", "clip", "tokens", "error", "clip_for", "clip_task"):
                        p.pop(k, None)
                self.submit(bad, pieces, prompt, refs, m, version, res)
                self.wait(pieces, version)
                redone += len(bad)
        self.mirror()
        msg = f"{len(pieces)} piece(s) edited" + (f", {redone} redone after the shot check" if redone else "")
        if left:
            msg += "; still to look at: " + "; ".join(f"piece {n}: {', '.join(v)}" for n, v in left.items())
        elif opts.get("verify", True):
            msg += "; every shot checked against the sample"
        return msg + "."

    def submit(self, todo: List[dict], pieces: List[dict], prompt: str, refs: List[Path], m: dict, version: str, res: str) -> None:
        from generate import SEEDANCE_IDS, _request, data_url
        for p in todo:
            text = prompt + (f" {p['extra']}" if p.get("extra") else "")
            content = [{"type": "text", "text": text[:3000]}]
            content += [{"type": "image_url", "image_url": {"url": data_url(r)}, "role": "reference_image"} for r in refs]
            content.append({"type": "video_url", "video_url": {"url": p["video"] if p["video"].startswith("asset://") else _fresh_link(self.job_id, p)},
                            "role": "reference_video"})
            body = {"model": m.get("endpoint") or SEEDANCE_IDS[version], "content": content, "ratio": "adaptive", "duration": -1,
                    "resolution": res, "generate_audio": False, "omni_reference_task_type": "edit"}
            try:
                created = _request("POST", "/contents/generations/tasks", body)
            except RuntimeError as e:
                txt = str(e)
                if "PrivacyInformation" in txt or "real person" in txt:
                    raise Stop("Seedance refused the sample: it may show real people. Videos with people are accepted only through the "
                               "private asset library (with the people's verified consent for real people).",
                               "Set up the asset library (ASSET_GROUP_ID), then press Try again.")
                raise Stop(f"Seedance refused piece {p['n']}: {txt[:400]}", "Press Try again.")
            p.update(task_id=created.get("id"), status="queued", error=None)
            p.pop("auto_retried", None)
            self._bodies[p["n"]] = body
            (self.files / f"clip_{p['n']:02d}.prompt.txt").write_text(text)
            self.update(lambda e, ps=json.loads(json.dumps(pieces)): e.update(pieces=ps))

    def wait(self, pieces: List[dict], version: str) -> None:
        from generate import _request
        deadline = time.time() + 1800
        while True:
            busy = 0
            for p in pieces:
                if p.get("status") in ("succeeded", "failed", "cancelled", "expired"):
                    continue
                t = _request("GET", f"/contents/generations/tasks/{p['task_id']}")
                p["status"] = t.get("status")
                if p["status"] == "succeeded":
                    dest = self.files / f"clip_{p['n']:02d}.mp4"
                    _download(t["content"]["video_url"], dest)
                    p.update(tokens=(t.get("usage") or {}).get("completion_tokens"), clip=dest.name, clip_for=p["file"], clip_task=p["task_id"])
                    self.paid({"kind": "seedance", "model": version, "task_id": p["task_id"], "completion_tokens": p["tokens"],
                               "video_input": True, "file": dest.name})
                elif p["status"] in ("failed", "cancelled", "expired"):
                    p["error"] = json.dumps(t.get("error") or {})[:300]
                    body = self._bodies.get(p["n"])
                    if "OutputVideoSensitiveContentDetected" in p["error"] and not p.get("auto_retried") and body:
                        # the output check is not fully repeatable (the same piece passed in an earlier run): once more
                        created = _request("POST", "/contents/generations/tasks", body)
                        if created.get("id"):
                            p.update(task_id=created["id"], status="queued", auto_retried=True, first_error=p["error"], error=None)
                            busy += 1
                else:
                    busy += 1
            self.update(lambda e, ps=json.loads(json.dumps(pieces)): e.update(pieces=ps))
            if not busy:
                break
            if time.time() > deadline:
                raise Stop("Seedance is still working after 30 minutes.", "Press Try again later: the finished pieces are kept.")
            time.sleep(10)
        bad = [p for p in pieces if p.get("status") != "succeeded"]
        if bad:
            err = str(bad[0].get("error") or "")
            if "OutputVideoSensitiveContentDetected" in err:
                raise Stop(f"Seedance's own check refused the edited piece {bad[0]['n']} ({bad[0]['start']:.1f}-{bad[0]['end']:.1f} s of the "
                           "sample), twice: it may show a brand, a logo or other protected material.",
                           "Press Try again (only that piece is redone), or leave that part of the sample out (Leave out after, in the form).")
            raise Stop(f"Seedance could not edit piece {bad[0]['n']}: {err[:300]}", "Press Try again: only the failed pieces are redone.")

    def verify_piece(self, inp: dict, ed: dict, p: dict) -> List[str]:
        """Every shot of one edited piece next to the same moment of the sample (one picture per shot, original left,
        edited right), with the new people and product pictures: one vision call lists what the edit left unchanged.
        The full-ad gate missed exactly this (it passed an ad that still showed the sample's couple, 9 Nov 2026)."""
        plan, opts = ed["plan"], inp.get("options") or {}
        sample, clip = self.files / "sample.mp4", self.files / p["clip"]
        shots = [s for s in ed["shots"] if p["start"] <= (s["start"] + s["end"]) / 2 < p["end"]][:6]
        if not shots:
            return []
        # start, middle and end of every shot: an edit can cut away inside a shot and come back to the original people
        # (9 Nov 2026: the middle frame showed the new couple, the last second the old one)
        moments = []
        for s in shots:
            a0, b0 = max(s["start"], p["start"]) + 0.5, min(s["end"], p["end"]) - 0.4
            ts = [(a0 + b0) / 2] if b0 - a0 < 1.2 else [a0, (a0 + b0) / 2, b0]
            moments += [(s, t) for t in ts]
        moments = moments[:14]
        tmp = Path(tempfile.mkdtemp(prefix="verify_"))
        try:
            content: List[dict] = []
            for k, (s, t) in enumerate(moments):
                a, b, pair = tmp / f"a{k}.jpg", tmp / f"b{k}.jpg", tmp / f"pair{k}.jpg"
                if not (grab(sample, t, a) and grab(clip, t - p["start"], b)):
                    continue
                subprocess.run([_ff(), "-v", "error", "-y", "-i", str(a), "-i", str(b), "-filter_complex",
                                "[0]scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2[l];"
                                "[1]scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2[r];[l][r]hstack=2",
                                "-q:v", "4", str(pair)], check=False)
                if pair.is_file():
                    content.append({"type": "text", "text": f"Shot {s['n']} (at {t:.1f} s): LEFT original, RIGHT edited"})
                    content.append({"type": "image_url", "image_url": {"url": b64(pair)}})
            if not content:
                return []
            people = [] if opts.get("people", "keep") == "keep" else plan.get("people") or []
            for i, q in enumerate(people, 1):
                img = self.files / (q.get("image_file") or (inp.get("person_files") or [""] * 9)[int(q.get("image") or 1) - 1] or "")
                if img.is_file():
                    content.append({"type": "text", "text": f"New person {i}, who replaces original person {i} ({q['what']})"})
                    content.append({"type": "image_url", "image_url": {"url": b64(small_jpeg(img, tmp / f"h{i}.jpg", 384))}})
            for i, f in enumerate((inp.get("product_files") or [])[:1], 1):
                content.append({"type": "text", "text": f"The client's product {i}"})
                content.append({"type": "image_url", "image_url": {"url": b64(small_jpeg(self.files / f, tmp / f"p{i}.jpg", 384))}})
            tasks = [f"replace original person {i} ({q['what']}) with new person {i}" for i, q in enumerate(people, 1)]
            tasks += [f"replace {q['what']} with the client's product" for q in plan.get("products") or []]
            tasks.append("remove every caption, subtitle and text overlay")
            content.append({"type": "text", "text": (
                "The edit had to: " + "; ".join(tasks) + ". For every shot, report only what the RIGHT side still shows UNCHANGED from "
                "the original. People: judge by the FACE, HAIR COLOUR, HAIR STYLE and GLASSES only, never by the clothes (an edit can "
                "change the clothes and keep the original face). For each person on the RIGHT, compare hair colour, hair style and "
                "glasses with the new person's picture and with the LEFT side: if they match the LEFT (original) better than the new "
                "person, the original person is still there. Return only {\"shots\": [{\"shot\": n, \"people_right\": [{\"hair\": "
                "\"colour and style\", \"glasses\": true/false, \"is\": \"original 1\" or \"new 1\" ...}], \"original_people\": [numbers "
                "of the original people still visible on the RIGHT], \"original_product\": true if the RIGHT still shows the "
                "original product instead of the client's, \"text\": \"the words of any caption, subtitle or text overlay on the RIGHT "
                "that is not printed on a product or a sign in the scene, else empty\"}]}")})
            res = _chat(PLANNER_MODEL, [{"role": "system", "content": "You check an AI video edit frame by frame. Answer with JSON only."},
                                        {"role": "user", "content": content}], max_tokens=1200)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        u = res.get("usage") or {}
        self.paid({"kind": "verify", "model": PLANNER_MODEL, "input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")})
        mm = re.search(r"\{.*\}", ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or "", re.S)
        try:
            rows = json.loads(mm.group(0)).get("shots") or [] if mm else []
        except (ValueError, AttributeError):
            return []
        issues = []
        names = {i: q["what"] for i, q in enumerate(people, 1)}
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            sh = r.get("shot")
            for k in r.get("original_people") or []:
                if isinstance(k, (int, float)) and int(k) in names:
                    issues.append(f"original person {int(k)} ({names[int(k)]}) still in shot {sh}")
            if r.get("original_product") is True:
                issues.append(f"original product still in shot {sh}")
            txt = str(r.get("text") or "").strip()
            if len(txt) >= 4:
                issues.append(f"caption left in shot {sh}: \"{txt[:80]}\"")
        return list(dict.fromkeys(issues))

    def s_assemble(self, inp: dict) -> str:
        opts = inp.get("options") or {}
        ed = self.job()["edit"]
        plan, pieces = ed["plan"], ed["pieces"]
        res = opts.get("resolution", "480p")
        clips, trims, t_ad = [], [], 0.0
        for i, p in enumerate(pieces):
            if p.get("clip_for") != p.get("file"):
                raise Stop(f"Piece {p['n']} was edited from an older cut of the sample.", "Redo from Seedance edit.")
            f = self.files / p["clip"]
            length = probe(f)["duration"] or (p["end"] - p["start"])
            start = JOIN_TRIM_S if i > 0 and length > JOIN_TRIM_S + 1 else 0.0
            clips.append({"path": str(f), "start": start, "end": round(length, 2)})
            trims.append((p["start"], start))
            t_ad += length - start
        content_len = t_ad

        def ad_time(t: float) -> float:
            return max(0.0, t - sum(tr for a, tr in trims if a <= t))
        mode = opts.get("captions", "full")
        caps = []
        if mode == "full":
            for c in plan["captions"]:
                a, b = ad_time(c["start"]), min(ad_time(c["end"]), content_len - 0.1)
                if b - a >= 1.0:
                    caps.append({"text": c["text"], "start": round(a, 2), "end": round(b, 2), "style": "client"})
        name = (inp.get("product_name") or "").strip()[:60]
        lines = []
        if mode != "none":
            if name:
                lines.append({"text": name, "size": 40, "weight": "bold"})
            if (inp.get("offer") or "").strip():
                lines.append({"text": inp["offer"].strip()[:60], "size": 36, "weight": "bold"})
            lines.append({"text": plan["cta"], "size": 46, "weight": "bold", "pill": "#D62839"})
        out = self.files / "final_v1.mp4"
        edit_plan = {"output": str(out), "size": SIZES.get(res, SIZES["480p"]), "fps": 30, "keep_clip_audio": False, "clips": clips,
                     "captions": caps, "reference_captions": plan.get("sample_text") or [], "caption_animation": {"in": "none", "out": "cut"},
                     "end_card": {"duration": 3, "background": "#FFFFFF", "image": str(self.files / inp["product_files"][0]), "lines": lines},
                     "preview_frames": 6}
        if store.ai_label():
            edit_plan["disclaimer"] = {"text": "AI-generated video"}
        pf = self.files / "edit_plan_v1.json"
        pf.write_text(json.dumps(edit_plan, indent=1, ensure_ascii=False))
        r = subprocess.run([sys.executable, str(ASSEMBLE), "--plan", str(pf)], capture_output=True, text=True, timeout=1200)
        try:
            resj = json.loads(r.stdout[r.stdout.find("{"):])
        except ValueError:
            resj = {"ok": False, "error": (r.stderr or r.stdout)[-400:]}
        if not resj.get("ok"):
            raise Stop(f"Assembly failed: {str(resj.get('error'))[:400]}", "Press Try again.")
        # the plan on disk names files by their bare names, as the agent's plans do (the caption editor reads it)
        edit_plan["output"] = out.name
        edit_plan["clips"] = [{**c, "path": Path(c["path"]).name} for c in clips]
        edit_plan["end_card"]["image"] = inp["product_files"][0]
        pf.write_text(json.dumps(edit_plan, indent=1, ensure_ascii=False))
        self.update(lambda e: e.update(final=out.name, length_s=resj.get("duration_s")), mirror=True)
        return f"{resj.get('duration_s')} s ad, {len(caps)} caption(s) and the end card."

    def s_check(self, inp: dict) -> str:
        opts = inp.get("options") or {}
        if opts.get("check") is False:
            return "skipped"
        from generate import api_key
        ed = self.job()["edit"]
        plan = ed["plan"]
        ctx = {"product_name": inp.get("product_name") or "the personalized product",
               "people": [{"name": f"Person {i}", "image": str(self.files / (p.get("image_file") or (inp.get("person_files") or [""])[int(p.get("image") or 1) - 1])),
                           "role": p["what"]} for i, p in enumerate(plan["people"], 1)],
               "product_previews": [{"for": "the ad", "image": str(self.files / f), "expected": plan.get("new_product") or ""}
                                    for f in inp.get("product_files") or []],
               "expected_text": plan.get("printed_text") or [],
               "captions": [c["text"] for c in json.loads((self.files / "edit_plan_v1.json").read_text()).get("captions") or []],
               "captions_mode": opts.get("captions", "full"), "cta_text": plan["cta"] if opts.get("captions", "full") != "none" else "",
               "disclaimer_text": "AI-generated video" if store.ai_label() else "", "setting_brief": plan.get("setting") or "",
               "hook_brief": plan.get("hook") or "", "reference_captions": plan.get("sample_text") or [],
               "do_not_copy": ["the sample's captions and on-screen text", "the sample's brand, logo and end card", "watermarks"],
               "forbidden": ["profanity", "rude gestures", "other brands", "prices"], "audio_required": False}
        if opts.get("people", "keep") == "keep":
            ctx["people"] = []
        else:  # the gate passed an ad that still showed the sample's couple in one shot (9 Nov 2026): name them
            ctx["do_not_copy"] += [f"the sample's original person: {p['what']} (must not appear; replaced by Person {i})"
                                   for i, p in enumerate(plan["people"], 1)]
        if plan["people"]:
            ctx["cast_count_expected"] = len(plan["people"])
        for p in ctx["people"]:
            if not Path(p["image"]).is_file():
                p.pop("image")
        tmp = Path(tempfile.mkdtemp(prefix="gate_"))
        try:
            (tmp / "context.json").write_text(json.dumps(ctx, indent=1, ensure_ascii=False))
            out = tmp / "round_1"
            env = {**os.environ, "ARK_API_KEY": api_key()}
            r = subprocess.run([sys.executable, str(GATE), "--video", str(self.files / ed["final"]), "--context", str(tmp / "context.json"),
                                "--out", str(out)], capture_output=True, text=True, timeout=900, env=env)
            rep_f = out / "report_round_1.json"
            if not rep_f.is_file():
                raise Stop(f"The quality check could not run: {(r.stderr or r.stdout)[-300:]}", "Press Try again.")
            for f in out.iterdir():
                if f.is_file() and (f.name.startswith("report_round_1") or f.name.startswith("quality_round_1")):
                    shutil.copyfile(f, self.files / f.name)
            shutil.copyfile(tmp / "context.json", self.files / "context.json")
            rep = json.loads(rep_f.read_text())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.paid({"kind": "gate", "model": "seed-2-0-lite", "calls": 1 + int(rep.get("second_opinion_calls") or 0)})
        scores = rep.get("scores") or {}
        low = [k for k, v in scores.items() if isinstance(v, dict) and (v.get("score") or 5) < 4]
        return f"{rep.get('verdict')}" + (f": low {', '.join(low)}" if low else "") + "."

    # cost -------------------------------------------------------------------
    def write_costs(self) -> None:
        import costs
        from generate import SEEDANCE_IDS
        job = self.job()
        ed = job.get("edit") or {}
        opts = (job.get("inputs") or {}).get("options") or {}
        res = opts.get("resolution", "480p")
        P = costs.PRICES
        parts = {k: 0.0 for k in costs.PARTS}
        by_step = {n: {k: 0.0 for k in costs.PARTS} for n, _, _ in STEPS}
        counts = {"seedance_tasks": 0, "seedance_tokens": 0, "seedream_images": 0, "checks": 0}
        tokens = {"total": 0, "input": 0, "cached": 0, "output": 0}
        for c in ed.get("paid") or []:
            k = c.get("kind")
            if k in ("planner", "verify"):
                i, o = int(c.get("input_tokens") or 0), int(c.get("output_tokens") or 0)
                usd = i * P.get("evaluator_input_per_m", 0) / 1e6 + o * P.get("evaluator_output_per_m", 0) / 1e6
                parts["checks"] += usd
                by_step[2 if k == "planner" else 5]["checks"] += usd
                counts["checks"] += 1
                tokens["input"] += i
                tokens["output"] += o
            elif k == "seedream":
                usd = (P.get("seedream_flash_image") if "flash" in str(c.get("model")) else P.get("seedream_image_small")) or 0
                parts["seedream"] += usd
                by_step[3]["seedream"] += usd
                counts["seedream_images"] += 1
            elif k == "seedance":
                t = int(c.get("completion_tokens") or 0)
                usd = t * (costs.seedance_price_per_m(SEEDANCE_IDS[c.get("model") or "2.5"], res, True) or 0) / 1e6
                parts["seedance"] += usd
                by_step[5]["seedance"] += usd
                counts["seedance_tasks"] += 1
                counts["seedance_tokens"] += t
            elif k == "gate":
                usd = int(c.get("calls") or 1) * (P.get("evaluator_call_estimate") or 0)
                parts["checks"] += usd
                by_step[7]["checks"] += usd
                counts["checks"] += int(c.get("calls") or 1)
        tokens["total"] = tokens["input"] + tokens["output"]
        from derive import parse_ts
        steps_out, runtime = [], 0
        for s in ed.get("steps") or []:
            a, b = parse_ts(s.get("started_at")), parse_ts(s.get("finished_at"))
            sec = round((b - a).total_seconds()) if a and b else 0
            runtime += sec
            usd = round(sum(by_step[s["n"]].values()), 4)
            steps_out.append({"n": s["n"], "name": s["label"], "runtime_s": sec, "tokens": 0, "tool_calls": 0, "usd": usd,
                              "parts": {k: round(v, 4) for k, v in by_step[s["n"]].items()}})
        total = round(sum(parts.values()), 4)
        breakdown = {"mode": "full", "total_usd": total, "runtime_s": runtime, "tokens": tokens, "requests": counts["checks"],
                     "tool_calls": 0, "web_search": 0, "web_fetch": 0, "parts": {k: round(v, 4) for k, v in parts.items()},
                     "counts": counts, "steps": steps_out, "prices_source": P.get("source"), "runtime_per_h": 0, "web_search_per_call": 0}
        data = {"production_s": runtime, "cost_per_video_usd": total, "cost_total_usd": total,
                "per_video": {k: round(v, 4) for k, v in parts.items()},
                "steps": [{"n": s["n"], "active_s": s["runtime_s"]} for s in steps_out], "breakdown": breakdown}
        (self.d / "costs.json").write_text(json.dumps(data, indent=1))


# ----------------------------------------------------------------------------- outside helpers
def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _version(opts: dict) -> str:
    v = str(opts.get("video_model") or "seedance-2.0-mini").removeprefix("seedance-")
    return v if v in PIECE_MAX else "2.0-mini"


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)


def _chat(model: str, messages: list, max_tokens: int = 3000) -> dict:
    from generate import BASE_URL, api_key
    import urllib.error
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    last = None
    for attempt in range(3):
        req = urllib.request.Request(BASE_URL + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)[:300]
        time.sleep(8 * (attempt + 1))
    raise Stop(f"The planning model could not be reached: {last}", "Press Try again.")


def _fallback_tos():
    """Local runs without the site's bucket: EDIT_TOS_BUCKET with keys from the environment or the asset key file."""
    bucket = os.environ.get("EDIT_TOS_BUCKET", "").strip()
    if not bucket:
        return None, None
    import assets_lib
    import tos
    cred = assets_lib.credentials()
    if not cred:
        return None, None
    c = tos.TosClientV2(cred["ak"], cred["sk"], os.environ.get("TOS_ENDPOINT", "tos-ap-southeast-1.bytepluses.com"),
                        os.environ.get("TOS_REGION", "ap-southeast-1"), security_token=cred.get("token"), enable_crc=False)
    return c, bucket


def _host(job_id: str, path: Path, expires: int = 7200) -> str:
    """A signed https link to one of the job's files, for Seedance and the asset library to read."""
    import storage
    if storage.enabled():
        if not storage.put_file(job_id, f"files/{path.name}", path):
            raise Stop(f"The piece could not be stored in the bucket: {storage.status().get('last_error')}")
        url = storage.signed_url(job_id, f"files/{path.name}", expires)
        if url:
            return url
    c, bucket = _fallback_tos()
    if c is None:
        raise Stop("The pieces need a place Seedance can read them from, and this server has no bucket.",
                   "Set TOS_BUCKET (or EDIT_TOS_BUCKET for local runs), then press Try again.")
    import tos
    key = f"experiments/sample_edit/{job_id}/{path.name}"
    c.put_object_from_file(bucket, key, str(path))
    return c.pre_signed_url(tos.HttpMethodType.Http_Method_Get, bucket, key, expires=expires).signed_url


def _fresh_link(job_id: str, piece: dict) -> str:
    d = store.job_dir(job_id)
    return _host(job_id, store.files_dir(d) / piece["file"])


def start(job_id: str) -> bool:
    """Runs (or resumes) the edit in a background thread. False when it is already running in this process."""
    with _RUN_LOCK:
        t = RUNS.get(job_id)
        if t and t.is_alive():
            return False
        t = threading.Thread(target=lambda: Run(job_id).run(), name=f"edit-{job_id}", daemon=True)
        RUNS[job_id] = t
        t.start()
        return True


def is_running(job_id: str) -> bool:
    t = RUNS.get(job_id)
    return bool(t and t.is_alive())


def resume_if_stale(job_id: str) -> None:
    """A run another instance (or this one before a restart) left: its heartbeat stopped. Seedance tasks already sent
    are polled again, never sent twice."""
    d = store.job_dir(job_id)
    if d is None or is_running(job_id):
        return
    ed = store.read_job(d).get("edit") or {}
    if ed.get("status") == "running" and time.time() - float(ed.get("heartbeat") or 0) > STALE_S:
        log(f"{job_id}: resuming a stopped run")
        start(job_id)


def retry(job_id: str, from_step: Optional[str] = None) -> None:
    """Re-runs the failed step and everything after it (or from `from_step`)."""
    d = store.job_dir(job_id)
    if d is None:
        raise FileNotFoundError(job_id)
    if is_running(job_id):
        raise RuntimeError("the edit is already running")
    with store.lock_for(job_id):
        job = store.read_job(d)
        ed = job.setdefault("edit", {})
        steps = ed.get("steps") or new_steps()
        first = STEP_N.get(from_step) if from_step else next((s["n"] for s in steps if s["status"] in ("failed", "running", "pending")), None)
        if first is None:
            raise RuntimeError("nothing to redo")
        for s in steps:
            if s["n"] >= first:
                s.update(status="pending", started_at=None, finished_at=None, message=None)
        if from_step and first <= STEP_N["edit"]:  # an explicit redo makes new edits of every piece
            for p in ed.get("pieces") or []:
                for k in ("task_id", "status", "clip", "tokens", "error", "clip_for", "clip_task", "issues", "verified_for", "extra", "tries"):
                    p.pop(k, None)
        if from_step in ("plan", "cut"):
            ed.pop("pieces", None)
        ed.update(steps=steps, status="queued", error=None, next=None, heartbeat=time.time())
        job["status"] = "queued"
        store.write_job(d, job)
    start(job_id)
