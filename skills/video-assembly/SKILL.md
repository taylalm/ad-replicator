---
name: video-assembly
description: Assemble the final vertical (9:16) ad from generated clips and stills in the Managed Agents sandbox - trim, scale, concatenate, burn in captions in the sample's caption style (1-2 short lines in the top band), overlay a photo inset with an arrow, cut in a montage of product-preview stills, mix music / sound effects / the clips' own audio, and close on a generated end card. Use after all clips are generated and the edit plan is agreed. No billable API calls.
---

# Video assembly

Use `scripts/assemble.py` with a JSON edit plan. Do not write ad-hoc ffmpeg commands.

## Workflow

1. Install dependencies once per session (skips if present, about 10 s the first time):

```bash
bash /mnt/skills/video-assembly/scripts/ensure-deps.sh
```

2. Write the edit plan to `/mnt/session/outputs/08_assembly/edit_plan.json` (schema below). Base it on the sample analysis: same scene order, pacing and caption roles, captions **rewritten** in new words, 1-2 short lines each. Use only facts from the product page and the brief for offers and product names; never invent them.
3. Validate without rendering and show the steps to the user:

```bash
python3 /mnt/skills/video-assembly/scripts/assemble.py --plan /mnt/session/outputs/08_assembly/edit_plan.json --dry-run
```

4. Render:

```bash
python3 /mnt/skills/video-assembly/scripts/assemble.py --plan /mnt/session/outputs/08_assembly/edit_plan.json
```

5. Report the output path, duration, and the preview frame paths from the JSON result. Look at the preview frames before calling the video done: captions readable and above the faces, insets not covering the product, end card present, and no AI disclaimer unless the brief asked for one.

## Edit plan schema

```json
{
  "output": "/mnt/session/outputs/08_assembly/final_v1.mp4",
  "size": [1080, 1920],
  "fps": 30,
  "transitions": "mix",
  "clips": [
    {"path": "/mnt/session/outputs/07_clips/clip_01.mp4", "start": 0, "end": 4.5},
    {"image": "/mnt/session/outputs/05_personalize/variant_kim_dialog.png", "duration": 1.2, "zoom": 1.06},
    {"image": "/mnt/session/outputs/05_personalize/variant_loran_dialog.png", "duration": 1.2, "zoom": 1.06},
    {"path": "/mnt/session/outputs/07_clips/clip_02.mp4"}
  ],
  "captions": [
    {"text": "POV: you asked her a question mid chapter", "start": 0, "end": 4.5, "style": "client"},
    {"text": "Each one is customized with their photo and name", "start": 4.5, "end": 9, "style": "client"},
    {"text": "It comes in different sizes & custom options", "start": 9, "end": 13, "style": "pill"},
    {"text": "Buy 2 and save 15%", "start": 13, "end": 17, "style": "offer"},
    {"text": "Limited time offer. Shop now", "start": 17, "end": 20, "style": "cta"}
  ],
  "insets": [
    {"image": "/mnt/session/outputs/04_people/person_harper.png", "start": 4.5, "end": 9, "position": "top-right", "width": 0.34, "top": 0.24, "arrow_to": [0.45, 0.55]}
  ],
  "end_card": {"duration": 3, "image": "/mnt/session/outputs/05_personalize/product_harper.png",
               "lines": [{"text": "Personalized Kids Blanket", "size": 54}, {"text": "Personalize yours today", "size": 60, "pill": "#D62839"}]},
  "music": {"path": "/mnt/session/uploads/track.mp3", "volume": 0.7, "fade_out": 1.0},
  "sfx": [{"path": "/mnt/session/uploads/kids_giggle.wav", "start": 0.5, "volume": 0.9}],
  "keep_clip_audio": true,
  "preview_frames": 6
}
```

- `clips` (required): played in order. Video entries (`path`, optional `start`/`end` trim) are scaled and centre-cropped to fill `size`. Still entries (`image`, `duration`, optional `zoom` for a slow push-in, `fit`: `contain` (default; the whole image inside the frame over a blurred copy of itself) or `cover`) cut product-preview screenshots or product-page images into the edit, for example the "Looks Good to Go?" dialog of three other personalized variants.
- Video clips take `start`/`end` (trim, seconds in the clip) and optional `zoom` (e.g. 1.35, crops the centre for a tighter close-up; `zoom_anchor` [x, y] 0-1 moves the crop, e.g. [0.5, 0.3] to keep faces near the top).
- Each clip's `start`/`end` must keep its payoff: the `payoff_window` in `check_<clip>.json` (beside the clip or in `checks/`), and the clip's end too when the payoff lands in its last second. A window that cuts it warns in a dry run and fails the render. Work the windows out again for every new take: never reuse them from an earlier edit plan. Only for a real reason (a long held shot, for example) add `"cut_payoff_ok": "<reason>"` to that clip.
- `transitions` (optional; default hard cuts): the effect at every cut. A name, `"mix"` (a different simple effect at each cut: dissolve, slide, zoom, wipe, fade to white, push, circle; a dissolve into the ending), or `{"type": "dissolve", "duration": 0.35}` (0.1-1.0 s). Names: `cut`, `dissolve`, `fade_black`, `fade_white`, `slide_left`, `slide_right`, `slide_up`, `slide_down`, `wipe_left`, `wipe_right`, `push_left`, `push_right`, `zoom`, `circle`, `blur`, `pixel`. One cut can differ: `clips[i].transition` (the effect INTO that clip), `end_card.transition`, `outro.transition`; `"cut"` keeps that cut hard. A transition is centred on the cut and blends about 0.2 s of each side, so the ad's length and every caption time stay the same; keep a payoff at least 0.3 s away from a cut that has a transition. The result lists the transitions used.
- The user's own footage (an intro, an outro, a single scene they filmed) is an ordinary `clips` entry with its `path` under /mnt/session/uploads/ (or `outro.path` for a closing clip that plays last); add `cut_payoff_ok: "user footage"` and `fit: "contain"` (footage of another shape keeps its whole picture over a blurred copy instead of being cropped; `outro.fit` too), `mute: true` to drop its sound, never regenerate or re-check it, and time the captions around it.
- `reference_captions`: the sample's on-screen text (and any reference ad's). Every caption must be NEW wording: one that is 60%+ similar to a reference or shares 4+ words in a row fails validation (dry-run too). Only a client-approved offer line keeps its wording, marked `"approved_wording": true`.
- `captions`: times on the final timeline. `style` presets: `client` (small white bold, thin dark outline, top band; default), `pill` (white on a dark pill, feature lines), `offer` (yellow bold with dark outline), `cta` (large white with red outline, final shop-now line), plus plain `outline` / `box`; and the styles of typical finished ads: `offer_italic` (heavy italic yellow with dark outline, e.g. "Buy 2 and get the third one free!"), `shop_now` (big heavy italic white with red outline, two lines "Limited Time Offer" / "Shop Now"), `promo_pink` (white bold in a pink rounded box, offer lines), `pov_box` (white bold in a black rounded box, POV hooks). Caption sizes are fractions of the frame width with a floor of 4.4% of the width so 480p renders stay readable (about 21 px at 480p, 48 px at 1080p); override with `size` in pixels. Captions are limited to 2 lines and about 70 characters; a caption that wraps to a third line fails. Positions: `top` (default, above faces), `upper`, `middle`, `lower`, `bottom`, or a number 0.04-0.80; anything below 0.30 of the frame warns unless `allow_over_subject` is set. Emoji render with Noto Color Emoji (installed by ensure-deps.sh); copy the sample's emoji use (one per caption at most, at the end of the line).
- `insets`: a photo (the AI person's original picture) in a white-bordered box, top-right by default, with a white curved arrow to `arrow_to` (frame fractions; aim at the printed portrait). With face avoidance on, the tip is placed just above the head of the portrait nearest `arrow_to`, pointing down at it, and the curve is routed so it never crosses a face.
- Face avoidance (default on, `"avoid_faces": false` to disable): for every caption and inset the script asks the vision model where faces are (real faces and printed portraits) in the frame at the middle of the interval and moves the caption to the nearest free band (top, upper, lower, bottom), moves the inset to a free side/height, lands the arrow tip just above the target portrait's head and routes the curve around every face. The JSON result lists what moved under `face_avoidance`; check the preview frames anyway. Use it for the "photo becomes the cartoon" shot.
- `outro`: an outro video appended after the last clip, scaled to `size`. No outro ships with this skill: set `path` only to a video the user uploaded and asked for (`true` or `{}` fails). When that outro carries the AI line, drop `end_card` and `disclaimer`.
- `end_card` (the normal ending): a still card after the last clip. `duration` (seconds, default 3), `background` (default `#FFFFFF`), an optional `image` (the product preview, scaled to fit) and `lines` centred under it: strings or `{"text", "size" (px, default 64), "weight", "color", "pill"}` (`pill` = a box colour behind white text). Put the product name and the CTA line on it; no prices unless approved. The card uses the text font only, so write its lines without emoji.
- Audio: `keep_clip_audio` keeps the clips' own sound (for example natural sound generated with the clips); `music` is looped and faded out before the outro; `sfx` entries are placed at `start` seconds. All present sources are mixed. Without any source the track is silent, which the quality gate treats as a failure, so always provide at least music from the uploads.
- `disclaimer` (optional, off by default): a small pill over the whole video, end card included (it stops before an outro), e.g. `{"text": "AI-generated video"}` (optional `position`, `size`). Set it ONLY when the brief (REQUIREMENTS) asks for an AI label, with its wording; otherwise leave it out. Ending on an `end_card` is no reason to add one.
- `preview_frames`: number of JPG stills saved next to the output for review and the quality gate.
- `clean_output`: a second render of the same cut with no captions, insets, arrows or disclaimer (sound and outro kept), for the client to re-edit with their own footage and text. Default `<output>_clean.mp4`; `false` skips it.

## Guardrails

- Only use media files that exist in the sandbox: generated clips, uploads, product-page images downloaded by the personalizer probe, previews captured by the personalizer, and an outro video the user uploaded.
- Music and sound effects must come from the user's uploads or a licence the user confirms. Never download audio from the web.
- Keep captions inside the top band and away from faces and from the bottom 15% where TikTok places its own UI.
- Offers and discounts only when the brief provides the approved wording.
- If rendering fails, report the error from the JSON output; do not retry with different settings without telling the user.
- Text is rendered with the bundled Poppins font (SIL Open Font Licence, `assets/fonts/OFL.txt`).

## Caption effects and position (added 26 Oct 2026)

- `captions[].animation`: `{"in": "none|fade|pop|slide_up|typewriter|word_by_word|blink", "out": "cut|fade", "in_s": 0.6, "out_s": 0.15}`; `plan.caption_animation` sets the default for every caption. Copy the sample's `analysis.caption_style.animation`. Typewriter reveals letter by letter, line by line, in the final layout (nothing jumps); word_by_word word by word; blink alternates the box colour (box styles) or the text colour (outline styles) every 0.25 s (`blink_color`, `blink_s`). A caption split at the cuts animates in once and out once.
- `captions[].x`: horizontal centre 0.15-0.85 (default 0.5), with `position` as the vertical place; used by the caption editor.
- Insets pop in over 0.25 s (`"animate": false` to switch off).
- Effects are drawn as per-frame stills (no looped inputs), so a 20-30 s ad still renders in seconds.
