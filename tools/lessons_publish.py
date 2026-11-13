#!/usr/bin/env python3
"""Publish lesson files to a ModelArk memory store (docs: persistent-memory), so every new agent session reads them at
/mnt/memory/<store id>/rules/.

  python3 tools/lessons_publish.py stores                       list the account's memory stores
  python3 tools/lessons_publish.py create --name ugc-lessons-generic [--description "..."]
  python3 tools/lessons_publish.py publish --store memstore-... --dir skills/ugc-seedance-prompt-craft/references/lessons/generic [--prune] [--dry-run]
  python3 tools/lessons_publish.py show --store memstore-...    list the store's memories
  python3 tools/lessons_publish.py pull --store memstore-... --dir <folder>   download the store's rules into a folder

publish creates or updates one memory per *.md file (README.md included) under /rules/<file>; --prune deletes store
memories under /rules/ that have no file. Only active, proposed and retired rule files are sent as they are: review
happens in the files (or on the site's Lessons page), not here. The generic store must hold no client, store or product
names: --leak-check <patterns file> refuses to publish a file that mentions one.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ma_client  # noqa: E402

PREFIX = "/rules/"


def leak_hits(text, patterns):
    """patterns: regular expressions (deploy/profiles/leak_patterns.txt), matched case-insensitively."""
    return [p for p in patterns if p and re.search(p, text, re.I)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stores")
    c = sub.add_parser("create"); c.add_argument("--name", required=True); c.add_argument("--description", default="Lessons learned from earlier ads: prompt rules that prevented paid retakes. Read /rules/README.md, then use lessons.py select to pull the rules for each shot.")
    p = sub.add_parser("publish"); p.add_argument("--store", required=True); p.add_argument("--dir", required=True); p.add_argument("--prune", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--leak-check", help="file with one forbidden name per line (deploy/profiles/leak_patterns.txt)")
    s = sub.add_parser("show"); s.add_argument("--store", required=True)
    g = sub.add_parser("pull"); g.add_argument("--store", required=True); g.add_argument("--dir", required=True)
    a = ap.parse_args()
    cl = ma_client.Client()

    if a.cmd == "stores":
        for st in cl.list_memory_stores().get("data", []):
            print(st.get("id"), st.get("name"), f"{st.get('memory_count')} memories", f"{st.get('session_count')} sessions", "|", (st.get("description") or "")[:80])
    elif a.cmd == "create":
        st = cl.create_memory_store(a.name, a.description)
        print(st.get("id"), st.get("name"))
    elif a.cmd == "show":
        for m in cl.list_memories(a.store):
            print(m.get("type"), m.get("path"), m.get("content_size_bytes"), m.get("updated_at"))
    elif a.cmd == "pull":
        out = Path(a.dir); out.mkdir(parents=True, exist_ok=True)
        for m in cl.list_memories(a.store, path_prefix=PREFIX):
            if m.get("type") == "directory":
                continue
            full = cl.get_memory(a.store, m["id"])
            (out / Path(m["path"]).name).write_text(full.get("content", ""), encoding="utf-8")
            print("pulled", m["path"])
    elif a.cmd == "publish":
        folder = Path(a.dir)
        files = sorted(p for p in folder.glob("*.md"))
        if not files:
            sys.exit(f"no .md files in {folder}")
        patterns = []
        if a.leak_check:
            patterns = [l.strip() for l in Path(a.leak_check).read_text().splitlines() if l.strip() and not l.startswith("#")]
        existing = {m["path"]: m for m in cl.list_memories(a.store, path_prefix=PREFIX) if m.get("type") != "directory"}
        seen, report = set(), []
        for f in files:
            text = f.read_text(encoding="utf-8")
            if len(text.encode()) > 102400:
                sys.exit(f"{f.name} is over the 100 KiB memory limit")
            hits = leak_hits(text, patterns)
            if hits:
                sys.exit(f"{f.name} mentions {hits}: a generic store must hold no client, store or product names")
            path = PREFIX + f.name
            seen.add(path)
            if a.dry_run:
                cur = existing.get(path)
                import hashlib
                how = "create" if not cur else ("unchanged" if cur.get("content_sha256") == hashlib.sha256(text.encode()).hexdigest() else "update")
            else:
                _, how = cl.put_memory(a.store, path, text, existing)
            report.append((f.name, how))
        for path, m in existing.items():
            if path not in seen:
                if a.prune and not a.dry_run:
                    cl.delete_memory(a.store, m["id"])
                report.append((Path(path).name, "deleted" if a.prune else "in store, no file (use --prune to delete)"))
        for name, how in report:
            print(f"{how:12} {name}")
        print(json.dumps({"store": a.store, "files": len(files), "dry_run": a.dry_run}))


if __name__ == "__main__":
    main()
