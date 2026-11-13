"""Scene picker: which take each scene of the ad uses, and whether the scene is in the ad at all.

Choices live in <job>/web_scene_picks.json ({clip_id: {"file": take file, "include": bool}}), kept in the agent session
like the other site records. Every take of a scene can be chosen: the agent's current take (the one in its cut), its
earlier takes (clip_01_v2, ...) and takes made on the site (web_take_clip_01_1.mp4).

The same file holds the user's own clips (`extras`: an intro, an outro, a scene they filmed; files/own_<n>_<name>) with
where each one goes, and the transition between scenes (`transitions`: keep = as the agent cut it, cut, mix or a name).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import store

PICKS = "web_scene_picks.json"


def _pull(d: Path) -> None:
    """The site runs on several server instances: the choices (and the user's own clips) saved on another one are in
    the bucket (jobs/<id>/web_scene_picks.json). Takes that copy when it is newer than this instance's."""
    try:
        import storage
        if not storage.enabled():
            return
        raw = storage.get_bytes(d.name, PICKS)
        if not raw:
            return
        remote = json.loads(raw)
        local = _read(d / PICKS)
        if not isinstance(local, dict) or float(remote.get("saved_at") or 0) > float(local.get("saved_at") or 0):
            (d / PICKS).write_text(json.dumps(remote, indent=1))
    except Exception:
        pass


def _store(d: Path, raw: dict) -> Path:
    """Writes the choices here and to the bucket, stamped, so every instance reads the newest."""
    import time
    raw["saved_at"] = time.time()
    path = d / PICKS
    path.write_text(json.dumps(raw, indent=1))
    try:
        import storage
        if storage.enabled():
            storage.put_raw(storage.key(d.name, PICKS), path.read_bytes())
    except Exception:
        pass
    return path


def _read(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def load_picks(d: Path) -> Dict[str, dict]:
    _pull(d)
    raw = _read(d / PICKS)
    return raw.get("scenes", {}) if isinstance(raw, dict) else {}


def load_extras(d: Path) -> List[dict]:
    _pull(d)
    raw = _read(d / PICKS)
    return [x for x in (raw.get("extras") or []) if isinstance(x, dict) and x.get("file")] if isinstance(raw, dict) else []


def load_transitions(d: Path) -> str:
    _pull(d)
    raw = _read(d / PICKS)
    return str(raw.get("transitions") or "keep") if isinstance(raw, dict) else "keep"


def transition_names() -> List[str]:
    """What the scene list offers: keep (the agent's cut), then mix and the assembler's own names."""
    try:
        from captions import assemble
        return ["keep", "cut", "mix"] + [n for n in assemble().TRANSITIONS if n != "cut"]
    except Exception:
        return ["keep", "cut", "mix", "dissolve"]


def _clean_extras(extras, known: List[dict]) -> List[dict]:
    """The user's placement of their own clips: only files that were uploaded for this job, sane times and places."""
    by_id = {x["id"]: x for x in known}
    out, seen = [], set()
    for x in extras or []:
        base = by_id.get(str((x or {}).get("id")))
        if not base or base["id"] in seen:
            continue
        seen.add(base["id"])
        length = float(base.get("length") or 0)
        after = str(x.get("after") or base.get("after") or "end")
        if after not in ("start", "end", "ending") and not re.fullmatch(r"clip_\d+[A-Za-z0-9_-]*", after):
            after = "end"
        start = max(0.0, float(x.get("start") or 0))
        end = float(x.get("end") or 0) or (length or 2.5)
        if base.get("kind") == "image":
            start, end = 0.0, min(8.0, max(0.5, end - start))
        else:
            end = min(end, length) if length else end
            if end - start < 0.3:
                start, end = 0.0, length or end
        out.append({**base, "after": after, "start": round(start, 2), "end": round(end, 2), "mute": bool(x.get("mute")),
                    "include": bool(x.get("include", True))})
    out += [x for i, x in by_id.items() if i not in seen]  # uploaded but not in this save: kept as it was
    return out


def save_picks(d: Path, picks: Dict[str, dict], extras=None, transitions: Optional[str] = None) -> Path:
    """Saves the scene choices; extras / transitions are left as they were when not given."""
    clean = {}
    for clip, p in (picks or {}).items():
        if re.fullmatch(r"clip_\d+[A-Za-z0-9_-]*", str(clip)) and isinstance(p, dict):
            f = Path(str(p.get("file") or "")).name
            clean[clip] = {"file": f or None, "include": bool(p.get("include", True))}
    known = load_extras(d)
    tr = load_transitions(d) if transitions is None else str(transitions)
    if tr not in transition_names():
        tr = "keep"
    return _store(d, {"scenes": clean, "extras": known if extras is None else _clean_extras(extras, known), "transitions": tr})


def add_extra(d: Path, file: str, kind: str, length: Optional[float], label: str) -> dict:
    """Registers an uploaded own clip (already saved as files/<file>); it starts out at the end of the scenes."""
    _pull(d)
    raw = _read(d / PICKS)
    raw = raw if isinstance(raw, dict) else {"scenes": {}}
    extras = [x for x in raw.get("extras") or [] if isinstance(x, dict)]
    n = 1 + max([int(m.group(1)) for x in extras for m in [re.match(r"own_(\d+)$", str(x.get("id")))] if m] or [0])
    item = {"id": f"own_{n}", "file": file, "kind": kind, "length": round(float(length), 2) if length else None, "label": label[:80],
            "after": "end", "start": 0.0, "end": round(float(length), 2) if length else 2.5, "mute": False, "include": True}
    raw["extras"] = extras + [item]
    raw.setdefault("transitions", "keep")
    _store(d, raw)
    return item


def remove_extra(d: Path, extra_id: str) -> Optional[dict]:
    _pull(d)
    raw = _read(d / PICKS)
    if not isinstance(raw, dict):
        return None
    hit = next((x for x in raw.get("extras") or [] if x.get("id") == extra_id), None)
    if hit:
        raw["extras"] = [x for x in raw["extras"] if x is not hit]
        _store(d, raw)
    return hit


def _check(files_dir: Path, stem: str) -> dict:
    c = _read(files_dir / f"check_{stem}.json") or {}
    return {"verdict": c.get("verdict"), "summary": (c.get("summary") or "")[:400] or None, "payoff_window": c.get("payoff_window")}


def _window(files_dir: Path, d: Path, name: str, take: dict) -> dict:
    """Which part of the take the agent's cut plays, its length, and how far the payoff runs when the cut stops short."""
    plans = store.edit_plans(files_dir)
    plan = _read(plans[-1]) if plans else None
    clip = next((c for c in (plan or {}).get("clips") or [] if Path(str(c.get("path") or "")).name == name), None)
    if not clip:
        return {}
    length = ((_read(d / "cost_cache.json") or {}).get("_durations") or {}).get(name)
    start, end = float(clip.get("start") or 0), float(clip.get("end") or length or 0)
    out = {"window": [start, end], "length": length}
    win = take.get("payoff_window")
    if isinstance(win, list) and len(win) == 2 and not clip.get("cut_payoff_ok"):
        must_end = float(length) if length and float(length) - float(win[1]) <= 1.0 else float(win[1])
        if end and (end < must_end - 0.1 or start > float(win[0]) + 0.1):
            out["cuts_payoff"] = round(must_end, 1)
    return out


def options(d: Path, files_dir: Path, job: dict, web_takes: List[dict]) -> List[dict]:
    """One row per scene of the agent's clips: its takes (newest first after the one in the cut) and the saved choice."""
    picks = load_picks(d)
    cj = _read(files_dir / "clips.json")
    cj = cj.get("clips") if isinstance(cj, dict) and isinstance(cj.get("clips"), dict) else {}
    from costs import delivered_clips
    used = set(delivered_clips(files_dir))  # stray copies and failed attempts are not scenes of the ad
    rows = []
    for c in job.get("clips") or []:
        if used and not used & {c["file"], *(t["file"] for t in c.get("takes") or [])}:
            continue
        cid = c["id"]
        takes = []
        agent = [{"file": c["file"], "url": c["url"]}] + list(c.get("takes") or [])
        cut_file = next((t["file"] for t in agent if t["file"] in used), c["file"])  # the agent's edit plan decides, not the highest _vN
        agent = sorted(({**t, "current": t["file"] == cut_file} for t in agent), key=lambda t: not t["current"])
        rec = cj.get(cid) if isinstance(cj.get(cid), dict) else {}
        site_src = str(rec.get("source") or "")
        for n, t in enumerate(agent):
            stem = Path(t["file"]).stem
            v = re.search(r"_v(\d+)$", stem)
            label = "In the agent's cut" if t["current"] else (f"Agent take {v.group(1)}" if v else "Agent's first take")
            info = {**_check(files_dir, stem), "model": c.get("model") if t["current"] else None, "source": "agent"}
            if t["current"] and (rec.get("site_take") or "/uploads/web_" in site_src):
                # the agent copied a take made on the site over this name: its check is the site take's, not the old file's
                site_file = Path(site_src).name.replace("web_", "", 1) if site_src else None
                w = next((x for x in web_takes if x.get("file") == site_file), {})
                label = "Your site take, in the cut"
                info = {"verdict": (w.get("check") or {}).get("verdict"), "summary": None, "payoff_window": (w.get("check") or {}).get("payoff_window"),
                        "model": w.get("model"), "source": "site"}
            takes.append({"file": t["file"], "url": t["url"], "label": label, "current": t["current"], **info})
        for w in web_takes:
            if w.get("kind") == "clip" and w.get("clip") == cid and w.get("file"):
                if any(t["file"] == w["file"] for t in takes):
                    continue
                chk = w.get("check") or {}
                takes.append({"file": w["file"], "url": f"/api/jobs/{job['id']}/files/{w['file']}", "source": "site",
                              "label": f"{'Trimmed' if w.get('trim') else 'Site'} take {w.get('n')}", "current": False, "verdict": chk.get("verdict"),
                              "summary": (chk.get("summary") or "")[:400] or None, "payoff_window": chk.get("payoff_window"),
                              "model": w.get("model")})
        pick = picks.get(cid) or {}
        accepted = next((w["file"] for w in web_takes if w.get("kind") == "clip" and w.get("clip") == cid and w.get("accepted")), None)
        chosen = pick.get("file") if any(t["file"] == pick.get("file") for t in takes) else (accepted or cut_file)
        rows.append({"id": cid, "n": len(rows) + 1, "takes": takes, "chosen": chosen, "include": pick.get("include", True),
                     "in_cut": cut_file, **_window(files_dir, d, cut_file, next((t for t in takes if t["file"] == cut_file), {})),
                     })
    return rows


def agent_message(rows: List[dict], picks: Dict[str, dict], extras: Optional[List[dict]] = None, transitions: str = "keep") -> Optional[str]:
    """What to tell the agent when the ad is not assembled yet; None when nothing differs from its own choice."""
    lines = []
    places = {"start": "as the very first clip of the ad (an intro)", "end": "after the last scene, before the end card",
              "ending": "as the very last thing in the ad, after the end card (set it as `outro.path`)"}
    for x in extras or []:
        if x.get("include") is False:
            continue
        where = places.get(x.get("after")) or f"right after scene {x.get('after')}"
        part = (f"shown for {x['end'] - x['start']:.1f} s as a still" if x.get("kind") == "image"
                else f"seconds {x['start']:.1f} to {x['end']:.1f} of it" + (", without its sound" if x.get("mute") else ", with its own sound"))
        lines.append(f"- OWN CLIP /mnt/session/uploads/web_{x['file']}: the user's own footage; put it in the edit plan {where}, {part}. "
                     "Never regenerate, edit or check it; time the captions around it.")
    if transitions and transitions != "keep":
        lines.append(f"- TRANSITIONS: {transitions} (set `transitions` in the edit plan to this).")
    for r in rows:
        p = picks.get(r["id"]) or {}
        if p.get("include") is False:
            lines.append(f"- {r['id']}: leave this scene OUT of the ad (keep the other scenes' order and timing).")
            continue
        f = p.get("file")
        if not f or f == r["in_cut"]:
            continue
        take = next((t for t in r["takes"] if t["file"] == f), None)
        if not take:
            continue
        where = f"/mnt/session/uploads/web_{f}" if take["source"] == "site" else f"your own take {f}"
        lines.append(f"- {r['id']}: use {where} as the current take (copy it into your clips folder; keep the others as _vN).")
    if not lines:
        return None
    return ("USE TAKES: the user picked the takes for the ad on the site:\n" + "\n".join(lines) +
            "\nRecord this in clips.json, do not regenerate these scenes, and continue the workflow where you left off. "
            "Write every message and STEP line in English.")
