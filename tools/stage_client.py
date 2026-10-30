#!/usr/bin/env python3
"""Stages the skills and the agent prompt for one client, ready to upload.

  python3 tools/stage_client.py                -> dist/stage/skills/<skill>/, dist/stage/agent/ (the code as it is)
  python3 tools/stage_client.py --client <name> -> dist/stage_<name>/... with clients/<name>/ laid over it

skills/ and agent/ hold the neutral version. A client's own files (brand outro, store examples, prompt wording, endpoint
ids) live in clients/<name>/, kept out of git, at the same paths as in the repo: a file there replaces the repo's file
or joins it. Upload from the staged folder, e.g.

  python3 tools/ma_client.py skill upload --skill-id <id> --dir dist/stage_<name>/skills/video-assembly
  python3 tools/ma_client.py agent set-system --file dist/stage_<name>/agent/system_prompt.md

Without --client, tools/leak_check.py checks the staged copy for client names and the script fails on a hit.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS = ("skills", "agent")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*_v[0-9]*.md", "*_v[0-9]*.json")  # old local versions


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", default="", help="a folder name in clients/ to lay over the code")
    ap.add_argument("--out", default="", help="default dist/stage or dist/stage_<client>")
    a = ap.parse_args()
    out = Path(a.out) if a.out else ROOT / "dist" / (f"stage_{a.client}" if a.client else "stage")
    if out.exists():
        shutil.rmtree(out)
    for part in PARTS:
        shutil.copytree(ROOT / part, out / part, ignore=IGNORE)

    laid = 0
    if a.client:
        cdir = ROOT / "clients" / a.client
        if not cdir.is_dir():
            sys.exit(f"[stage] no client folder {cdir}")
        for part in PARTS:
            for f in sorted((cdir / part).rglob("*")) if (cdir / part).is_dir() else []:
                if f.is_file() and f.name != ".DS_Store":
                    dst = out / f.relative_to(cdir)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    laid += 1

    skills = sorted(d.name for d in (out / "skills").iterdir() if (d / "SKILL.md").is_file())
    print(f"[stage] {len(skills)} skills and the agent prompt in {out.relative_to(ROOT)}"
          + (f" ({laid} files from clients/{a.client})" if a.client else ""))
    if not a.client:
        leak = subprocess.run([sys.executable, str(ROOT / "tools" / "leak_check.py"), str(out)])
        if leak.returncode:
            print("[stage] FAILED: client names found above")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
