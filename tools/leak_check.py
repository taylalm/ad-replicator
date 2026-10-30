#!/usr/bin/env python3
"""Fails when a folder that will be shipped to another client still mentions a client's names.

  python3 tools/leak_check.py <folder> [<folder> ...] [--extra "name1|name2"]

Checks every file and folder name, and the text of every text file (code, JSON, HTML, CSS, Markdown...). Media and
binary files are checked by name only. The default names come from LEAK_PATTERNS in the environment, else from
deploy/profiles/leak_patterns.txt (one regular expression per line, kept out of git), so this file names nobody.
Exit code 1 and one line per hit when anything is found.
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".json", ".jsonl", ".html", ".htm", ".css", ".md", ".txt", ".sh",
        ".env", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".csv", ".svg", ".xml", ".map", ".srt", ".vtt", ""}
MAX_TEXT = 30 * 1024 * 1024


def patterns(extra: str) -> re.Pattern:
    parts = [p for p in (os.environ.get("LEAK_PATTERNS") or "").split("|") if p.strip()]
    f = ROOT / "deploy" / "profiles" / "leak_patterns.txt"
    if not parts and f.is_file():
        parts = [ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    parts += [p for p in (extra or "").split("|") if p.strip()]
    if not parts:
        sys.exit("leak_check: no names to look for (LEAK_PATTERNS or deploy/profiles/leak_patterns.txt)")
    return re.compile("|".join(f"(?:{p})" for p in parts), re.I)


def scan(folder: Path, pat: re.Pattern) -> list:
    hits = []
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            if pat.search(name):
                hits.append(f"{Path(dirpath, name)}: name")
        for name in filenames:
            p = Path(dirpath, name)
            if p.suffix.lower() not in TEXT and not name.startswith("."):
                continue
            try:
                if p.stat().st_size > MAX_TEXT:
                    continue
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                m = pat.search(line)
                if m:
                    start = max(0, m.start() - 40)
                    hits.append(f"{p}:{i}: …{line[start:m.end() + 40].strip()}…")
                    break  # one hit per file is enough to fail; keeps minified bundles readable
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--extra", default="", help="more names, separated by |")
    a = ap.parse_args()
    pat = patterns(a.extra)
    hits = [h for f in a.folders for h in scan(Path(f), pat)]
    for h in hits:
        print(h)
    print(f"[leak_check] {len(hits)} file(s) with client names" if hits else "[leak_check] clean")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
