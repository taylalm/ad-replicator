#!/usr/bin/env python3
"""One-command setup of the Ad Replicator in YOUR BytePlus ModelArk account (region ap-southeast-1).

  export ARK_API_KEY=...                      # your key; or leave it unset and paste it when asked (not shown)
  python3 tools/setup_account.py              # create everything (or update it, when run again)
  python3 tools/setup_account.py --check      # also start one short test session (a few cents of agent tokens)
  python3 tools/setup_account.py --build      # also build the web app package for veFaaS (needs Node 20+)

What it does, in your account, with your key:
  1. checks the key works and that the models the tool uses are offered in the region (whether each one is
     activated for your account is not visible through the API: activate them in the console, guide Part A1);
  2. creates the sandbox environment (cloud, open network, nothing pre-installed);
  3. creates a vault with one credential: your key as the environment variable ARK_API_KEY, usable only towards
     ark.ap-southeast.bytepluses.com (the skills call Seedream, Seedance and the evaluator with it);
  4. uploads the seven skills in skills/ (a new version of each when it already exists);
  5. creates a memory store with the lessons in skills/ugc-seedance-prompt-craft/references/lessons/generic;
  6. creates the agent from agent/agent_config.json and agent/system_prompt.md with those skill versions (or updates it);
  7. writes the ids into .env (never the key), so tools/ma_client.py, the local web app and the package use them.

Run it again after pulling a new version of this repository: it reuses the ids in .env and only uploads new skill
versions and updates the agent. API reference read 13 Nov 2026: create-environment-api, create-vault-api,
create-credential-api, create-skill-api, create-agent-api (docs.byteplus.com/en/docs/modelark/...).

Your key is read from ARK_API_KEY or typed at the prompt; it is sent only to the ModelArk API (to call it, and once into
the vault credential) and never printed or written to a file. Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import ma_client  # noqa: E402

ENV_FILE = ROOT / ".env"
SKILLS = ["ugc-video-analyzer", "product-personalizer", "generate-with-seedream", "ugc-seedance-prompt-craft",
          "generate-with-seedance", "video-assembly", "ugc-quality-gate"]
LESSONS = ROOT / "skills" / "ugc-seedance-prompt-craft" / "references" / "lessons" / "generic"
API_HOST = "ark.ap-southeast.bytepluses.com"
# the models the agent and the New video form's settings use (ui/server/models.py, agent/agent_config.json)
MODELS = {
    "glm-5-3-flash-260828": "agent (required)",
    "seed-2-0-lite-260428": "clip checks and the quality gate (required)",
    "dola-seedream-5-0-pro-260628": "images, Balanced and Best quality",
    "dola-seedream-5-0-flash-260915": "images, Economy",
    "dreamina-seedance-2-5-260628": "video, Best quality",
    "dreamina-seedance-2-0-fast-260128": "video, Balanced (the default)",
    "dreamina-seedance-2-0-mini-260615": "video, Economy",
}


def say(msg: str) -> None:
    print(msg, flush=True)


def stop(msg: str) -> None:
    sys.exit(f"\nSTOPPED: {msg}")


# --------------------------------------------------------------------------- .env (ids only)
def read_env() -> dict:
    out = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and m.group(2) and not m.group(2).endswith("..."):
                out[m.group(1)] = m.group(2).strip("'\"")
    return out


def write_env(updates: dict) -> None:
    """Sets KEY=value lines in .env (created from .env.example), keeping every other line. Never called with the key."""
    assert not any(k == "ARK_API_KEY" for k in updates)
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.is_file() else \
        ((ROOT / ".env.example").read_text().splitlines() if (ROOT / ".env.example").is_file() else [])
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            done.add(m.group(1))
    extra = [k for k in updates if k not in done]
    if extra:
        if not any(l.startswith("# Skill ids") for l in lines) and any(k.startswith("SKILL_ID_") for k in extra):
            lines += ["", "# Skill ids in your account (written by tools/setup_account.py; not secrets)"]
        lines += [f"{k}={updates[k]}" for k in extra]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def skill_entries(items: list) -> list:
    """skill_id=version strings -> the agent's skills list (docs: update-agent-api)."""
    out = []
    for item in items:
        sid, _, ver = item.partition("=")
        out.append({"type": "custom", "skill_id": sid, **({"version": ver} if ver else {})})
    return out


def skill_var(name: str) -> str:
    return "SKILL_ID_" + name.upper().replace("-", "_")


# --------------------------------------------------------------------------- steps
def check_key(c: ma_client.Client) -> None:
    try:
        c.get("/agents", limit=1)
    except ma_client.ApiError as e:
        if e.status in (401, 403):
            stop("the API key was refused (HTTP %d). Check that it is active and belongs to an account with "
                 "Managed Agents (ModelArk console > API keys)." % e.status)
        raise
    say("  key works for Managed Agents")


def check_models(key: str) -> list:
    """The models the tool uses that the region's model list (GET /models) does not offer. No inference, no cost."""
    import urllib.request
    req = urllib.request.Request(f"https://{API_HOST}/api/v3/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            offered = {m.get("id") for m in json.load(r).get("data") or []}
    except Exception as e:
        say(f"  model list not readable ({str(e)[:80]}); skipped")
        return []
    missing = [(m, use) for m, use in MODELS.items() if m not in offered]
    for model, use in missing:
        say(f"  NOT OFFERED: {model} ({use})")
    if not missing:
        say("  every model the tool uses is offered; make sure each is activated in the console (Model activation)")
    return missing


def ensure_environment(c, ids: dict, name: str) -> str:
    if ids.get("MA_ENV_ID"):
        c.get(f"/environments/{ids['MA_ENV_ID']}")
        say(f"  environment {ids['MA_ENV_ID']} (existing)")
        return ids["MA_ENV_ID"]
    # nothing pre-installed: every skill installs what it needs at run time (scripts/ensure-deps.sh)
    env = c.post("/environments", {"name": name, "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
                                   "description": "Ad Replicator sandbox (skills install their own tools)"})
    write_env({"MA_ENV_ID": env["id"]})
    say(f"  environment {env['id']} created")
    return env["id"]


def ensure_vault(c, ids: dict, key: str, name: str, replace_key: bool) -> str:
    vault_id = ids.get("MA_VAULT_ID")
    if vault_id:
        c.get(f"/vaults/{vault_id}")
        say(f"  vault {vault_id} (existing)")
    else:
        vault_id = c.post("/vaults", {"display_name": name})["id"]
        write_env({"MA_VAULT_ID": vault_id})
        say(f"  vault {vault_id} created")
    creds = (c.get(f"/vaults/{vault_id}/credentials") or {}).get("data") or []
    have = next((x for x in creds if (x.get("auth") or {}).get("secret_name") == "ARK_API_KEY"), None)
    if have and not replace_key:
        say(f"  vault credential ARK_API_KEY (existing; --replace-key puts the current key in)")
        return vault_id
    if have:
        c.delete(f"/vaults/{vault_id}/credentials/{have['id']}")
    c.post(f"/vaults/{vault_id}/credentials", {
        "display_name": "ark_api_key",
        "auth": {"type": "environment_variable", "secret_name": "ARK_API_KEY", "secret_value": key,
                 "networking": {"type": "limited", "allowed_hosts": [API_HOST]}}})
    say(f"  vault credential ARK_API_KEY {'replaced' if have else 'created'} (usable only towards {API_HOST})")
    return vault_id


def _latest_version(c, skill_id: str) -> str:
    data = (c.skill_versions(skill_id) or {}).get("data") or []
    nums = [int(v["version"]) for v in data if str(v.get("version", "")).isdigit()]
    if not nums:
        stop(f"skill {skill_id} has no versions")
    return str(max(nums))


def ensure_skills(c, ids: dict) -> list:
    entries = []
    for name in SKILLS:
        folder = ROOT / "skills" / name
        if not (folder / "SKILL.md").is_file():
            stop(f"{folder} is missing; run this from a complete copy of the repository")
        for cache in folder.rglob("__pycache__"):
            subprocess.run(["rm", "-rf", str(cache)], check=False)
        sid = ids.get(skill_var(name))
        if sid:
            c.create_skill_version(sid, folder)
            how = "new version"
        else:
            d = c.create_skill(folder, name)
            sid = d.get("skill_id") or (d.get("id") if str(d.get("id", "")).startswith("skill-") else None)
            if not sid:
                stop(f"skill {name} was created but the answer has no skill id: {json.dumps(d)[:300]}")
            write_env({skill_var(name): sid})
            how = "created"
        version = _latest_version(c, sid)
        entries.append(f"{sid}={version}")
        say(f"  skill {name}: {sid} v{version} ({how})")
    return entries


def ensure_lessons(c, ids: dict, skip: bool) -> str | None:
    if skip:
        return ids.get("MA_MEMORY_STORE_IDS")
    store = (ids.get("MA_MEMORY_STORE_IDS") or "").split(",")[0].strip()
    if not store:
        store = c.create_memory_store("ugc-lessons", "Lessons learned from retakes, read by every new session")["id"]
        write_env({"MA_MEMORY_STORE_IDS": store})
        say(f"  lessons store {store} created")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "lessons_publish.py"), "publish", "--store", store,
                        "--dir", str(LESSONS)], capture_output=True, text=True, env=os.environ)
    if r.returncode != 0:
        say(f"  lessons not published ({(r.stderr or r.stdout).strip()[-200:]}); the agent still works without them")
    else:
        say(f"  lessons published to {store}")
    return store


def ensure_agent(c, ids: dict, skills: list) -> str:
    system = (ROOT / "agent" / "system_prompt.md").read_text()
    entries = skill_entries(skills)
    if ids.get("MA_AGENT_ID"):
        d = c.update_agent(ids["MA_AGENT_ID"], system=system, skills=entries)
        say(f"  agent {d['id']} updated to version {d['version']}")
        return d["id"]
    body = json.loads((ROOT / "agent" / "agent_config.json").read_text())
    body = {k: v for k, v in body.items() if k not in ("id", "version", "created_at", "updated_at", "skills", "system")}
    body.update(system=system, skills=entries)
    d = c.create_agent(body)
    write_env({"MA_AGENT_ID": d["id"]})
    say(f"  agent {d['id']} created (version {d['version']}, model {(d.get('model') or {}).get('id')})")
    return d["id"]


def smoke_test(c, agent_id: str, env_id: str, vault_id: str, store: str | None) -> bool:
    """One short session: the skills are mounted and the vault key reaches the sandbox. No image or video calls."""
    prompt = ("Purpose: a setup check right after installation (no ad project, no paid image or video calls, no STEP "
              "lines). Run in ONE bash call: `ls /mnt/skills; echo; curl -s -o /dev/null -w 'key:%{http_code}' -H "
              "\"Authorization: Bearer $ARK_API_KEY\" https://ark.ap-southeast.bytepluses.com/api/v3/models; echo; "
              "ls /mnt/memory 2>/dev/null | head -3` and reply with exactly one line: SKILLS=<number of skill folders> "
              "KEY=<http code> MEMORY=<yes|no>. Never print the key.")
    s = c.create_session(agent_id, env_id, [vault_id], title="setup check (tools/setup_account.py)",
                         memory_store_ids=[store] if store else None)
    try:
        c.send_message(s["id"], prompt)
        c.wait_idle(s["id"], poll=5, timeout=600)
        text = ""
        for e in c.all_events(s["id"]):
            if e.get("type") == "agent.message":
                text = ma_client.text_of(e.get("content")) or text
        m = re.search(r"SKILLS=(\d+)\s+KEY=(\d+)", text)
        say(f"  test session answered: {text.strip()[:200]}")
        return bool(m and int(m.group(1)) >= len(SKILLS) and m.group(2) == "200")
    finally:
        try:
            c.delete(f"/sessions/{s['id']}")
        except Exception:
            pass


def build_package() -> None:
    web = ROOT / "ui" / "web"
    if not (web / "node_modules").is_dir():
        say("  installing the web app's packages (npm ci)")
        subprocess.run(["npm", "ci"], cwd=web, check=True)
    subprocess.run(["bash", str(ROOT / "deploy" / "vefaas" / "build_package.sh")], cwd=ROOT, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="ad-replicator", help="name of the environment and the vault (default ad-replicator)")
    ap.add_argument("--replace-key", action="store_true", help="put the current key into the vault (e.g. after rotating it)")
    ap.add_argument("--no-lessons", action="store_true", help="skip the lessons memory store")
    ap.add_argument("--check", action="store_true", help="start one short test session afterwards (a few cents)")
    ap.add_argument("--build", action="store_true", help="build deploy/vefaas/dist/ad-replicator.zip afterwards")
    ap.add_argument("--env-file", help="where the ids are kept (default .env at the repository root)")
    a = ap.parse_args()
    global ENV_FILE
    if a.env_file:
        ENV_FILE = Path(a.env_file).resolve()
        if a.build:
            stop("--build reads the ids from .env at the repository root; leave out --env-file")

    key = os.environ.get("ARK_API_KEY") or getpass.getpass("ModelArk API key (not shown): ").strip()
    if not key:
        stop("no API key")
    os.environ["ARK_API_KEY"] = key  # for this process and the lessons publisher it starts; never written anywhere
    c = ma_client.Client(api_key=key)
    ids = read_env()

    say("1/7 API key and models")
    check_key(c)
    missing = check_models(key)
    if any("required" in use for _, use in missing):
        stop("activate the required models first, then run this again")
    say("2/7 sandbox environment")
    env_id = ensure_environment(c, ids, a.name)
    say("3/7 vault with your key")
    vault_id = ensure_vault(c, read_env(), key, a.name, a.replace_key)
    say("4/7 skills")
    skills = ensure_skills(c, read_env())
    say("5/7 lessons")
    store = ensure_lessons(c, read_env(), a.no_lessons)
    say("6/7 agent")
    agent_id = ensure_agent(c, read_env(), skills)
    say("7/7 ids written to .env (no key)")
    if a.check:
        say("check: one short test session")
        if not smoke_test(c, agent_id, env_id, vault_id, store):
            stop("the test session did not see all skills or the key; see its answer above")
        say("  ok")
    if a.build:
        say("build: web app package")
        build_package()

    say(f"""
Done. Your ids (not secrets) are in .env:
  MA_AGENT_ID={agent_id}
  MA_ENV_ID={env_id}
  MA_VAULT_ID={vault_id}
  MA_MEMORY_STORE_IDS={store or ''}

Next: the web app.
  Try it on this computer:  ui/start.sh   (http://localhost:3000; keep ARK_API_KEY exported)
  Host it on veFaaS:        python3 tools/setup_account.py --build, then upload deploy/vefaas/dist/ad-replicator.zip
                            and set ARK_API_KEY and UI_BASIC_AUTH on the function (guide/BUILD_GUIDE.md, Part C);
                            bucket storage for the projects: Part D.""" +
        ("\n\nModels not activated yet (the matching settings fail until you activate them):\n" +
         "\n".join(f"  {m} ({u})" for m, u in missing) if missing else ""))


if __name__ == "__main__":
    try:
        main()
    except ma_client.ApiError as e:
        stop(f"ModelArk answered HTTP {e.status} for {e.url}: {e.body[:400]}")
    except ma_client.MissingSetting as e:
        stop(str(e))
