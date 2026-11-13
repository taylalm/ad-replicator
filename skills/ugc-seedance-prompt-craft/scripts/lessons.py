#!/usr/bin/env python3
"""Lessons learned from retakes: load the rule files, select the rules that match a shot, lint prompts against them.

  select   Print the rules for a shot. Tags from --shot / --product, or inferred from --plan --clip (prompt, references,
           product.spec, analysis scene).
  lint     Run the rule lints on every clip of a plan (craft.py validate does this too).
  list     Every rule with its status, tags and where it was loaded from.

Rules are one markdown file each (format: references/lessons/generic/README.md). They are read from every mounted
memory store (/mnt/memory/<store>/rules/*.md, written by the site) and from the copy bundled with this skill; a store
rule overrides a bundled rule with the same id. No model or network calls.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLED = os.path.join(HERE, "..", "references", "lessons", "generic")
MEMORY_GLOB = os.environ.get("LESSONS_MEMORY_GLOB", "/mnt/memory/*/rules")
SHOT_TAGS = ("product", "walking", "hands_only", "unboxing", "multi_person", "timed_beat", "flat_lay", "text", "feet", "still_object", "always")
PRODUCT_KINDS = ("flat_print", "flat_rigid", "soft", "wearable", "mounted", "lit", "photo", "thick")
KIND_WORDS = {  # product kind inferred from product.spec / product.name when the plan does not set product.kind
    "flat_rigid": r"\b(metal|steel|aluminium|aluminum|acrylic|wood(en)?|plaque|sign|plate|cut-?out)\b",  # not "board": padded boards exist (cost test 9 Nov)
    "flat_print": r"\b(knit|sock|blanket|shirt|t-shirt|hoodie|sweat|fabric|fleece|sherpa|tote|towel|apron|sweater|textile)\b",
    "soft": r"\b(pillow|cushion|plush|stuffed|pad(ded)?|quilted)\b",
    "wearable": r"\b(sock|shirt|hoodie|hat|cap|balaclava|mask|sweater|apron|glove|scarf)\b",
    "mounted": r"\b(hang|hung|mount(ed)?|screw(ed)?|wall|door|nail|hook)\b",
    "lit": r"\b(lamp|light-?up|led|glow|bulb|night ?light)\b",
    "photo": r"\b(portrait|photo|picture|avatar|cartoon)\b",
    "thick": r"\b(folded|stack(ed)?|layers?)\b",
}

HEADER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


def parse_rule(path, origin):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    m = HEADER.match(text.strip())
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if _:
            meta[k.strip().lower()] = v.strip()
    if not meta.get("id"):
        return None
    body = m.group(2).strip()
    rule_text, _, why = body.partition("\nWhy:")
    return {"id": meta["id"], "title": meta.get("title", ""), "status": meta.get("status", "active").lower(),
            "when": [t.strip() for t in meta.get("when", "always").split(",") if t.strip()],
            "product": [t.strip() for t in meta.get("product", "any").split(",") if t.strip()],
            "lint": meta.get("lint") or None, "lint_level": meta.get("lint_level", "warning").lower(),
            "source": meta.get("source", ""), "text": rule_text.strip(), "why": why.strip(), "origin": origin, "path": path}


def load_rules(include_inactive=False):
    rules = {}
    for d in [BUNDLED] + sorted(glob.glob(MEMORY_GLOB)):  # stores come after the bundle and win
        origin = "bundled" if d == BUNDLED else d
        for p in sorted(glob.glob(os.path.join(d, "*.md"))):
            if os.path.basename(p).lower() == "readme.md":
                continue
            r = parse_rule(p, origin)
            if r:
                rules[r["id"]] = r
    out = sorted(rules.values(), key=lambda r: r["id"])
    return out if include_inactive else [r for r in out if r["status"] == "active"]


# ----------------------------------------------------------------------------- tags
def product_kinds(plan):
    kinds = plan.get("product", {}).get("kind")
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",")]
    kinds = [k for k in (kinds or []) if k in PRODUCT_KINDS]
    if kinds:
        return kinds
    text = " ".join(str(plan.get("product", {}).get(k) or "") for k in ("name", "spec", "personalization")).lower()
    return [k for k, rx in KIND_WORDS.items() if re.search(rx, text)]


def shot_tags(plan, clip, analysis=None):
    refs = {r.get("id"): r for r in plan.get("references", []) if isinstance(r, dict)}
    cref = clip.get("references") or []
    kinds = [refs.get(r, {}).get("kind") for r in cref]
    low = (clip.get("prompt") or "").lower()
    tags = set(t for t in (clip.get("shot_tags") or []) if t in SHOT_TAGS)
    if "product" in kinds or clip.get("shows_product"):
        tags.add("product")
    if re.search(r"\bonly (her|his|their|the)? ?hands\b|hands[- ]only|just (her|his|their) hands", low):
        tags.add("hands_only")
    if re.search(r"\bunbox|tissue paper|opens? the (box|package|parcel)|gift ?box", low):
        tags.add("unboxing")
    scene_text = ""
    if analysis and clip.get("scenes"):
        for x in analysis.get("scenes", []):
            if x.get("index") in clip["scenes"]:
                scene_text += " " + " ".join(str(x.get(k) or "") for k in ("motion", "setting", "camera_position", "pose"))
                if len(x.get("reactions") or []) >= 1:
                    tags.add("multi_person")
    if re.search(r"\bwalk|stroll|jog|runs? (past|along|toward)|passer", low + scene_text.lower()):
        tags.add("walking")
    people = sum(1 for k in kinds if k == "person")
    cast = plan.get("cast_count")
    if people >= 2 or (isinstance(cast, int) and cast >= 2) or re.search(r"\b(two|three|four|both|couple|family|friends|passers?-?by) (people|women|men|kids|children|friends|adults)?\b", low):
        tags.add("multi_person")
    if re.search(r"\benters?\b|\bjoins?\b|walks? in\b|\breveal|\bfreez|\bappears?\b|comes? into (the )?(frame|shot|view)", low):
        tags.add("timed_beat")
    if re.search(r"flat[- ]lay|laid flat|lies? flat|lying flat|spread (out )?on the (table|bed|floor)|top-?down", low):
        tags.add("flat_lay")
    names = [n for n in plan.get("product", {}).get("names", []) if n and not str(n).startswith("<")]
    if names or re.search(r"\b(name|text|letters?|words?|quote|writing)\b", low):
        tags.add("text")
    if re.search(r"\b(feet|foot|legs?|ankles?|socks?|slippers?|shoes?)\b", low) and not re.search(r"\bface", low):
        tags.add("feet")
    if re.search(r"\b(portrait|printed (face|picture|photo)|pillow|cushion|plush|photo on)\b", low):
        tags.add("still_object")
    tags.add("always")
    return sorted(tags)


def select_rules(rules, shots, products):
    shots, products = set(shots) | {"always"}, set(products)
    hits = []
    for r in rules:
        if not (set(r["when"]) & shots):
            continue
        if "any" not in r["product"] and products and not (set(r["product"]) & products):
            continue
        if "any" not in r["product"] and not products and "product" in shots:
            pass  # unknown product kind: keep product-specific rules, the writer decides
        hits.append(r)
    return hits


# ----------------------------------------------------------------------------- lints (code; a rule turns one on by name)
def _product_shot(plan, clip):
    refs = {r.get("id"): r for r in plan.get("references", []) if isinstance(r, dict)}
    return any(refs.get(r, {}).get("kind") == "product" for r in (clip.get("references") or [])) or bool(clip.get("shows_product"))


def lint_use_line(plan, clip, low, tags, kinds):
    if "product" not in tags:
        return None
    use = str(plan.get("product", {}).get("use") or "")
    if not use or use.startswith("<"):
        return ("warning", "product.use is empty: write how the product is mounted, worn, carried or lit and what never shows, then paste it in every product shot")
    if " ".join(use.lower().split()) not in " ".join(low.split()):
        return (None, f"product.use is not in this product shot word for word (\"{use[:70]}...\")")
    return None


def lint_toward_camera(plan, clip, low, tags, kinds):
    steady = re.search(r"same size|constant distance|same distance|camera (walks|moves|tracks|pulls) back|stays the same size|chest height|"
                       r"does not (move|come) (closer|toward)|never (closer|toward)", low)
    if re.search(r"toward(s)? the (camera|lens|viewer)|up to the (camera|lens)|into the (camera|lens)|closer to the camera", low) and not steady:
        return (None, "movement toward the camera: keep the subject the same size (camera walks backward at the same pace) or move across the frame; lift the product to chest height, not to the lens")
    # a product lifted, raised or thrown with no distance clause grows or shrinks between frames (cost test 9 Nov: board lifted, ball thrown)
    if "product" in tags and re.search(r"\b(lifts?|lifting|raises?|raising|holds? (it |the \w+ )?up|picks? (it |the \w+ )?up|throws?|tosses?|pulls? (it |the \w+ )?out)\b", low) and not steady:
        return (None, "the product is lifted, raised or thrown: say it stays at the same distance from the camera (\"lifts it to chest height, no closer\"; \"the ball keeps its size in flight\"), or the size jumps between frames")
    return None


def lint_no_3d_for_flat(plan, clip, low, tags, kinds):
    if "product" not in tags or not ({"flat_print", "flat_rigid"} & set(kinds)):
        return None
    hit = re.search(r"\b3-?d\b|sculpt|embossed|raised (artwork|design|print|figure)|plush|figurine|clay", low)
    if hit:
        return (None, f"a flat product is described with volume (\"{hit.group(0)}\"); state the material and thickness and forbid raised parts")
    return None


def lint_entrance_edge(plan, clip, low, tags, kinds):
    if re.search(r"\benters?\b|walks? in\b|comes? in(to)?\b|\bjoins?\b|\bappears?\b|\bleaves?\b|walks? out\b|\bexits?\b", low) \
            and not re.search(r"(left|right|top|bottom) edge|from the (left|right|top|bottom)|at the (left|right) (side|edge)|out of frame|off-?screen", low):
        return (None, "someone enters or leaves but no frame edge is named; write 'enters from the right edge at 3 s'")
    return None


def lint_hands_support(plan, clip, low, tags, kinds):
    if "hands_only" in tags and not re.search(r"forearm|wrist|rests? on|resting on|on the (table|desk|counter|bed|floor|lap|duvet|rug)|held above the (table|counter)|from the bottom edge", low):
        return (None, "hands-only shot: say the forearms enter from an edge or that the object rests on a surface, or it floats")
    return None


def lint_orientation_flat_lay(plan, clip, low, tags, kinds):
    if "flat_lay" in tags and "text" in tags and not re.search(r"left to right|upright|not mirrored|never mirrored|reading side|faces the camera", low):
        return (None, "a flat-lay shot of an item with text: say the text reads left to right, upright, facing the camera (never mirrored or upside down)")
    return None


def lint_many_names(plan, clip, low, tags, kinds):
    names = [str(n).lower() for n in plan.get("product", {}).get("names", []) if n and not str(n).startswith("<")]
    if len(names) >= 2 and "product" in tags and sum(1 for n in names if n in low) >= 2 and re.search(r"close-?up|macro|fills the frame|push-?in", low):
        return (None, "two or more names in one close shot garble: show one name per close shot, or let the image reference carry them")
    if re.search(r"note ?card|handwritten|hand-written|a letter (reads|says)|a card (reads|says)|label (reads|says)", low):
        return (None, "a prop with its own writing (card, letter, label): the model invents letters; remove it or make it blank")
    return None


def lint_feet_face(plan, clip, low, tags, kinds):
    if "feet" in tags and not re.search(r"no face|face (is )?out of (the )?frame|from the (waist|knees|hips) down|whole (head|body)|head to toe|full body|face (stays |is )?(fully )?visible", low):
        return (None, "legs or feet shot: frame below the shoulders on purpose ('from the waist down, no face in the shot') or wide enough for the whole head")
    return None


def lint_size_line(plan, clip, low, tags, kinds):
    return None  # enforced by craft.py validate (product.size_statement dimensions in every product shot)


def lint_first_frame_refs(plan, clip, low, tags, kinds):
    refs = {r.get("id"): r for r in plan.get("references", []) if isinstance(r, dict)}
    images = [r for r in (clip.get("references") or []) if refs.get(r, {}).get("kind") != "video"]
    if clip.get("first_frame") and images:
        return (None, f"a first frame together with {len(images)} image reference(s): Seedance refuses the task (InvalidParameter, 19 refusals in the harvest). Drop the references (the still already shows people and product) or drop the first frame")
    return None


LINTS = {"use_line": lint_use_line, "toward_camera": lint_toward_camera, "no_3d_for_flat": lint_no_3d_for_flat,
         "entrance_edge": lint_entrance_edge, "hands_support": lint_hands_support, "orientation_flat_lay": lint_orientation_flat_lay,
         "many_names": lint_many_names, "feet_face": lint_feet_face, "size_line": lint_size_line, "first_frame_refs": lint_first_frame_refs}


def lint_clip(plan, clip, analysis=None, rules=None):
    """Returns (errors, warnings, applied_rule_ids) for one clip. Messages start with the rule id."""
    rules = load_rules() if rules is None else rules
    tags = shot_tags(plan, clip, analysis)
    kinds = product_kinds(plan)
    low = (clip.get("prompt") or "").lower()
    cid = clip.get("id", "?")
    errors, warnings, done = [], [], set()
    for r in select_rules(rules, tags, kinds):
        fn = LINTS.get(r["lint"] or "")
        if not fn or r["lint"] in done:
            continue
        done.add(r["lint"])
        res = fn(plan, clip, low, tags, kinds)
        if not res:
            continue
        level, msg = res
        level = level or r["lint_level"]
        (errors if level == "error" else warnings).append(f"{cid}: [{r['id']}] {msg}")
    # the first-frame rule is a tool fact, not a lesson: always on, even with no rule file for it
    if "first_frame_refs" not in done:
        res = lint_first_frame_refs(plan, clip, low, tags, kinds)
        if res:
            errors.append(f"{cid}: [tool] {res[1]}")
    return errors, warnings, [r["id"] for r in select_rules(rules, tags, kinds)]


# ----------------------------------------------------------------------------- CLI
def _load(path):
    with open(path) as fh:
        return json.load(fh)


def cmd_select(a):
    rules = load_rules()
    if a.plan:
        plan = _load(a.plan)
        analysis = _load(a.analysis) if a.analysis else None
        clip = next((c for c in plan.get("clips", []) if c.get("id") == a.clip), None) if a.clip else None
        if a.clip and not clip:
            print(json.dumps({"ok": False, "error": f"clip {a.clip} not in plan"})); sys.exit(2)
        shots = shot_tags(plan, clip, analysis) if clip else ["product", "always"]
        products = product_kinds(plan)
    else:
        shots = [t.strip() for t in (a.shot or "").split(",") if t.strip()]
        products = [t.strip() for t in (a.product or "").split(",") if t.strip()]
    bad = [t for t in shots if t not in SHOT_TAGS] + [p for p in products if p not in PRODUCT_KINDS]
    if bad:
        print(json.dumps({"ok": False, "error": f"unknown tags {bad}", "shot_tags": SHOT_TAGS, "product_kinds": PRODUCT_KINDS})); sys.exit(2)
    hits = select_rules(rules, shots, products)
    text = "\n".join(f"[{r['id']}] {r['title']}: {r['text']}" for r in hits)
    print(json.dumps({"ok": True, "shot_tags": shots, "product_kinds": products, "rules": [r["id"] for r in hits],
                      "text": text, "stores": sorted({r["origin"] for r in rules if r["origin"] != "bundled"})}, indent=2, ensure_ascii=False))


def cmd_lint(a):
    plan = _load(a.plan)
    analysis = _load(a.analysis) if a.analysis else None
    rules = load_rules()
    errors, warnings, applied = [], [], {}
    for c in plan.get("clips", []):
        e, w, ids = lint_clip(plan, c, analysis, rules)
        errors += e; warnings += w; applied[c.get("id")] = ids
    print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings, "rules_per_clip": applied}, indent=2, ensure_ascii=False))
    sys.exit(0 if not errors else 1)


def cmd_list(a):
    for r in load_rules(include_inactive=True):
        print(f"{r['id']}  {r['status']:8} when={','.join(r['when'])} product={','.join(r['product'])} lint={r['lint'] or '-'}  {r['title']}  <{r['origin']}>")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select"); s.add_argument("--shot"); s.add_argument("--product"); s.add_argument("--plan"); s.add_argument("--clip"); s.add_argument("--analysis")
    l = sub.add_parser("lint"); l.add_argument("--plan", required=True); l.add_argument("--analysis")
    sub.add_parser("list")
    a = ap.parse_args()
    {"select": cmd_select, "lint": cmd_lint, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    main()
