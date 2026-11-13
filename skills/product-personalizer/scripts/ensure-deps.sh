#!/usr/bin/env bash
# Install Playwright + Chromium + Chromium's system libraries, only what is missing. About 1 minute on a fresh
# sandbox; instant once done. Safe to start in the background at the beginning of a run and to call again later: a
# second call waits for the first one instead of installing twice.
#
# What the sandbox needs (seen in sessions of 10 Nov 2026):
# - its outbound gateway re-signs HTTPS with its own CA (/usr/local/share/ca-certificates/egress-ca.crt). Playwright's
#   own downloader (Node) does not trust it and fails with "Download failure", so Node is pointed at that CA, and when
#   the download still fails the same Chrome for Testing build is fetched with curl from Google's public bucket;
# - plain-HTTP Ubuntu mirrors answer 503, so apt is pointed at an HTTPS mirror before anything is installed;
# - only a handful of libraries are missing for Chromium: they are installed by name (seconds) instead of
#   `playwright install-deps` (minutes), which stays as the last resort.
# - Chromium must TRUST that CA (added to its NSS store), not just ignore the error: Chrome turns off service workers
#   and other features on pages with certificate errors, and a store's photo-to-cartoon preview then never renders
#   (13 Nov 2026: five attempts on a sock page with ignore-errors; trusted CA worked on 10 Nov).
# Every step has a timeout.
set -u
log() { echo "[ensure-deps] $*" >&2; }

CA=/usr/local/share/ca-certificates/egress-ca.crt
[ -f "$CA" ] && export NODE_EXTRA_CA_CERTS="$CA"

can_launch() {
  timeout 60 python3 - <<'PY' >/dev/null 2>&1
from playwright.sync_api import sync_playwright
p = sync_playwright().start(); b = p.chromium.launch(); b.close(); p.stop()
PY
}

NSSDB="$HOME/.pki/nssdb"
ca_trusted() { [ ! -f "$CA" ] || [ -f "$NSSDB/.egress_ca_added" ]; }

trust_gateway_ca() {
  [ -f "$CA" ] || return 0
  ca_trusted && return 0
  if ! command -v certutil >/dev/null 2>&1; then
    log "install certutil (libnss3-tools) to trust the sandbox gateway CA"
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
      [ -f "$f" ] && sed -i 's|http://archive.ubuntu.com|https://mirrors.edge.kernel.org|g; s|http://security.ubuntu.com|https://mirrors.edge.kernel.org|g' "$f"
    done
    DEBIAN_FRONTEND=noninteractive timeout 120 apt-get install -y -qq --no-install-recommends libnss3-tools </dev/null >/dev/null 2>&1 \
      || { timeout 120 apt-get -o Acquire::https::Timeout=20 -qq update </dev/null >/dev/null 2>&1; \
           DEBIAN_FRONTEND=noninteractive timeout 120 apt-get install -y -qq --no-install-recommends libnss3-tools </dev/null >/dev/null 2>&1; }
  fi
  command -v certutil >/dev/null 2>&1 || { log "certutil not available: Chromium will only ignore the certificate errors"; return 1; }
  mkdir -p "$NSSDB"
  certutil -d "sql:$NSSDB" -L >/dev/null 2>&1 || certutil -d "sql:$NSSDB" -N --empty-password >/dev/null 2>&1
  certutil -d "sql:$NSSDB" -A -t "C,," -n egress-ca -i "$CA" >/dev/null 2>&1 && touch "$NSSDB/.egress_ca_added"
}

if python3 -c "import playwright" 2>/dev/null && can_launch && ca_trusted; then
  echo '{"ok": true, "installed": false, "message": "Playwright and Chromium already working"}'
  exit 0
fi

# one installer at a time (a run may start this in the background and call it again at the product step)
exec 9>/tmp/personalizer-deps.lock
if command -v flock >/dev/null 2>&1; then
  flock -w 420 9 || log "another install is still running after 7 minutes; continuing anyway"
  if python3 -c "import playwright" 2>/dev/null && can_launch && ca_trusted; then
    echo '{"ok": true, "installed": false, "message": "Playwright and Chromium ready (installed by an earlier call)"}'
    exit 0
  fi
fi

if ! python3 -c "import playwright" 2>/dev/null; then
  log "pip install playwright"
  timeout 180 pip install --quiet --root-user-action=ignore playwright </dev/null \
    || { echo '{"ok": false, "error": "pip install playwright failed or timed out"}'; exit 1; }
fi

# The browser for headless runs is the "headless shell" build. Fetch the exact build this Playwright version expects
# straight from the Chrome for Testing bucket (curl trusts the sandbox gateway; Playwright's downloader may not).
direct_download() {
  timeout 300 python3 - "$@" <<'PY'
import json, os, pathlib, subprocess, sys, zipfile
import playwright
names = set(sys.argv[1:]) or {"chromium-headless-shell"}
want = {"chromium": ("chrome-linux64.zip", "chromium-{rev}"),
        "chromium-headless-shell": ("chrome-headless-shell-linux64.zip", "chromium_headless_shell-{rev}")}
spec = json.load(open(pathlib.Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"))
cache = pathlib.Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or pathlib.Path.home() / ".cache" / "ms-playwright")
ok = True
for b in spec["browsers"]:
    if b["name"] not in names or b["name"] not in want:
        continue
    if not b.get("browserVersion"):
        ok = False
        continue
    zip_name, folder = want[b["name"]]
    dest = cache / folder.format(rev=b["revision"])
    if (dest / "INSTALLATION_COMPLETE").exists():
        continue
    url = f"https://storage.googleapis.com/chrome-for-testing-public/{b['browserVersion']}/linux64/{zip_name}"
    tmp = f"/tmp/{zip_name}"
    if subprocess.run(["curl", "-fsSL", "--retry", "2", "--max-time", "240", "-o", tmp, url]).returncode != 0:
        print(f"download failed: {url}", file=sys.stderr)
        ok = False
        continue
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp) as z:
        for info in z.infolist():
            out = z.extract(info, dest)
            mode = info.external_attr >> 16
            if mode:
                os.chmod(out, mode)  # zipfile drops the execute bit
    (dest / "INSTALLATION_COMPLETE").touch()
    os.remove(tmp)
sys.exit(0 if ok else 1)
PY
}

browser_present() {
  python3 - <<'PY' >/dev/null 2>&1
import glob, os, pathlib, sys
cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(pathlib.Path.home() / ".cache" / "ms-playwright")
sys.exit(0 if glob.glob(cache + "/chromium_headless_shell-*/INSTALLATION_COMPLETE") or glob.glob(cache + "/chromium-*/INSTALLATION_COMPLETE") else 1)
PY
}

if ! browser_present; then
  log "download Chromium (Playwright's downloader)"
  if ! timeout 150 python3 -m playwright install chromium </dev/null >/dev/null 2>&1; then
    log "that failed; fetching the same build from the Chrome for Testing bucket"
    direct_download chromium-headless-shell \
      || { echo '{"ok": false, "error": "Chromium could not be downloaded (Playwright CDN and the Chrome for Testing bucket both failed)"}'; exit 1; }
  fi
fi

if ! can_launch; then
  log "install Chromium system libraries from an HTTPS apt mirror"
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$f" ] && sed -i 's|http://archive.ubuntu.com|https://mirrors.edge.kernel.org|g; s|http://security.ubuntu.com|https://mirrors.edge.kernel.org|g' "$f"
  done
  if ! timeout 120 apt-get -o Acquire::https::Timeout=20 -o Acquire::Retries=1 -qq update </dev/null >/dev/null 2>&1; then
    log "HTTPS mirror failed; trying it over plain HTTP"
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
      [ -f "$f" ] && sed -i 's|https://mirrors.edge.kernel.org|http://mirrors.edge.kernel.org|g' "$f"
    done
    timeout 120 apt-get -o Acquire::http::Timeout=20 -o Acquire::Retries=1 -qq update </dev/null >/dev/null 2>&1 \
      || { echo '{"ok": false, "error": "apt-get update failed on the kernel.org mirror (HTTPS and HTTP)"}'; exit 1; }
  fi
  LIBS="libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxkbcommon0 libgbm1 libnss3 libnspr4 libcups2 libdrm2 libpango-1.0-0 libcairo2 libasound2"
  DEBIAN_FRONTEND=noninteractive timeout 240 apt-get install -y -qq --no-install-recommends $LIBS </dev/null >/dev/null 2>&1 \
    || log "some libraries could not be installed by name"
  if ! can_launch; then
    log "still not launching; fetching the full Chromium build and Playwright's own dependency list"
    direct_download chromium >/dev/null 2>&1 || true
    can_launch || DEBIAN_FRONTEND=noninteractive timeout 420 python3 -m playwright install-deps chromium </dev/null >/dev/null 2>&1 || true
  fi
fi

trust_gateway_ca || true

if can_launch; then
  if ca_trusted; then
    echo '{"ok": true, "installed": true, "message": "Playwright, Chromium and system libraries ready"}'
  else
    echo '{"ok": true, "installed": true, "message": "Chromium ready, but the sandbox gateway CA is not trusted: photo previews may not render", "ca_trusted": false}'
  fi
else
  MISSING=$(ldd "$(ls -d "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null | head -1)" 2>/dev/null | awk '/not found/ {printf "%s ", $1}')
  echo "{\"ok\": false, \"error\": \"Chromium still cannot launch after installing dependencies\", \"missing_libraries\": \"${MISSING}\"}"
  exit 1
fi
