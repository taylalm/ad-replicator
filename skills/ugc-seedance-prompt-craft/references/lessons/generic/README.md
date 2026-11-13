# Lessons: one rule per file

Rules learned from retakes (failed takes and users' change requests). Each file is one rule. `scripts/lessons.py select`
pulls the rules that match a shot; `craft.py validate` runs the lints named in them. Wording is generic: no client,
store or product names. Keep a rule short: what to write in the prompt, then why.

Header fields (one per line, between `---` lines):

| Field | Values |
|---|---|
| `id` | R01, R02 ... unique; client stores use C01 ... |
| `title` | one line |
| `status` | `active` (used), `proposed` (shown on the Lessons page, not used), `retired` (kept for history, not used) |
| `when` | shot tags, comma separated: `product` (the product is in the shot), `walking`, `hands_only`, `unboxing`, `multi_person`, `timed_beat` (an entrance, a reveal, a freeze), `flat_lay`, `text` (a name or words must read), `feet` (legs or feet shots), `still_object` (something printed or inanimate that must not move), `always` |
| `product` | product kinds, comma separated, or `any`: `flat_print` (printed knit, fabric, apparel, blankets), `flat_rigid` (metal, acrylic, wood signs and plaques, boards), `soft` (pillows, plush, cushions), `wearable`, `mounted` (hung, screwed or stuck on a wall or door), `lit` (anything that lights up), `photo` (a printed portrait or photo), `thick` (folded or stacked items) |
| `lint` | optional: the check in `lessons.py` that enforces the rule (`use_line`, `toward_camera`, `no_3d_for_flat`, `entrance_edge`, `hands_support`, `orientation_flat_lay`, `many_names`, `feet_face`, `size_line`, `first_frame_refs`) |
| `lint_level` | `error` (blocks render) or `warning` |
| `source` | where the rule came from (harvest date, counts); evidence job ids stay out of the generic store |

Below the header: the rule text (imperative, what to write), then a `Why:` line. `scripts/lessons.py select` prints the
rule text; keep it under about 60 words. Rules the store holds at `/mnt/memory/<store>/rules/` override a bundled rule
with the same id, so a rule can be edited in the store without a skill release.
