"""ModelArk private asset library, for the site's "edit the sample" mode: a sample video with people in it is accepted
by Seedance only as an asset of the library (asset://<id>), not as a plain link (InputVideoSensitiveContentDetected).

Same calls and signature as tools/asset_client.py (docs read 25 Oct 2026: private-virtual-avatar-library-guide-preview,
create-asset-api, get-asset-api): POST https://ark.ap-southeast-1.byteplusapi.com/?Action=<Action>&Version=2024-01-01,
AK/SK signature (HMAC-SHA256, scope <date>/ap-southeast-1/ark/request). The API key does not work here.

Configuration: ASSET_GROUP_ID (the asset group, made once in the console or with asset_client.py create-group).
Credentials, in this order:
- BYTEPLUS_ACCESS_KEY / BYTEPLUS_SECRET_KEY (+ BYTEPLUS_SESSION_TOKEN) in the environment;
- the IAM role of the veFaaS function (the temporary keys the platform adds to every request, see storage.py); the role
  then needs the ModelArk asset permissions;
- local runs: the private key file ~/.byteplus/asset_keys.env (BYTEPLUS_KEY_FILE), read into this process only.
Keys are never logged, printed or written.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

HOST = "ark.ap-southeast-1.byteplusapi.com"
REGION, SERVICE, VERSION = "ap-southeast-1", "ark", "2024-01-01"
KEY_FILE = os.path.expanduser(os.environ.get("BYTEPLUS_KEY_FILE", "~/.byteplus/asset_keys.env"))


def group_id() -> Optional[str]:
    if not os.environ.get("ASSET_GROUP_ID"):
        try:  # the package's .env (tools/ma_client.py copies ASSET_GROUP_ID from it with the agent ids)
            from paths import ma_client
            ma_client().load_settings_file()
        except Exception:
            pass
    return (os.environ.get("ASSET_GROUP_ID") or "").strip() or None


def _key_file() -> dict:
    out = {}
    try:
        for line in open(KEY_FILE):
            k, sep, v = line.strip().removeprefix("export ").partition("=")
            if sep and not k.startswith("#") and v.strip():
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def credentials() -> Optional[dict]:
    ak = os.environ.get("BYTEPLUS_ACCESS_KEY") or os.environ.get("VOLC_ACCESSKEY")
    sk = os.environ.get("BYTEPLUS_SECRET_KEY") or os.environ.get("VOLC_SECRETKEY")
    if ak and sk:
        return {"ak": ak, "sk": sk, "token": os.environ.get("BYTEPLUS_SESSION_TOKEN") or None, "source": "environment"}
    try:
        import storage
        cred = storage._credentials()
        if cred and cred.get("source") == "function role":
            return {**cred, "source": "function role"}
    except Exception:
        pass
    f = _key_file()
    if f.get("BYTEPLUS_ACCESS_KEY") and f.get("BYTEPLUS_SECRET_KEY"):
        return {"ak": f["BYTEPLUS_ACCESS_KEY"], "sk": f["BYTEPLUS_SECRET_KEY"], "token": f.get("BYTEPLUS_SESSION_TOKEN") or None,
                "source": "key file"}
    return None


def status() -> dict:
    cred = credentials()
    return {"group": group_id(), "credentials": (cred or {}).get("source"), "ready": bool(group_id() and cred)}


def _h(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def call(action: str, body: dict) -> dict:
    cred = credentials()
    if not cred:
        raise RuntimeError("no access key for the asset library (BYTEPLUS_ACCESS_KEY / BYTEPLUS_SECRET_KEY, or the function role)")
    payload = json.dumps(body).encode()
    now = datetime.datetime.now(datetime.timezone.utc)
    x_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    body_hash = hashlib.sha256(payload).hexdigest()
    query = urllib.parse.urlencode(sorted({"Action": action, "Version": VERSION}.items()))
    token = cred.get("token")
    head = f"content-type:application/json\nhost:{HOST}\nx-content-sha256:{body_hash}\nx-date:{x_date}\n"
    signed = "content-type;host;x-content-sha256;x-date"
    if token:
        head += f"x-security-token:{token}\n"
        signed += ";x-security-token"
    canonical = "\n".join(["POST", "/", query, head, signed, body_hash])
    scope = f"{day}/{REGION}/{SERVICE}/request"
    to_sign = "\n".join(["HMAC-SHA256", x_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _h(_h(_h(_h(cred["sk"].encode(), day), REGION), SERVICE), "request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"https://{HOST}/?{query}", data=payload, method="POST", headers={
        "Content-Type": "application/json", "Host": HOST, "X-Date": x_date, "X-Content-Sha256": body_hash,
        "Authorization": f"HMAC-SHA256 Credential={cred['ak']}/{scope}, SignedHeaders={signed}, Signature={sig}",
        **({"X-Security-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:1000]
        try:
            out = json.loads(txt)
        except ValueError:
            out = {"ResponseMetadata": {"Error": {"Code": f"HTTP{e.code}", "Message": txt[:300]}}}
    text = json.dumps(out).replace(cred["ak"], "<access key>").replace(token or "\0", "<token>")
    out = json.loads(re.sub(r"\bAK[A-Za-z0-9]{8,}", lambda m: m.group(0)[:4] + "…", text))
    err = (out.get("ResponseMetadata") or {}).get("Error")
    if err:
        raise RuntimeError(f"asset library {action}: {err.get('Code')}: {str(err.get('Message'))[:300]}")
    return out.get("Result", out)


def add_video(url: str, name: str, timeout_s: int = 900) -> str:
    """Adds a video (public or presigned https link) to the asset group and waits until it is Active. Returns the id."""
    gid = group_id()
    if not gid:
        raise RuntimeError("ASSET_GROUP_ID is not set")
    res = call("CreateAsset", {"GroupId": gid, "URL": url, "AssetType": "Video", "ProjectName": "default", "Name": name[:64]})
    aid = res.get("Id")
    if not aid:
        raise RuntimeError(f"asset library CreateAsset returned no id: {json.dumps(res)[:200]}")
    end = time.time() + timeout_s
    while time.time() < end:
        g = call("GetAsset", {"Id": aid, "ProjectName": "default"})
        if g.get("Status") == "Active":
            return aid
        if g.get("Status") == "Failed":
            raise RuntimeError(f"asset library: the video was refused ({str(g.get('FailReason') or g.get('Message') or 'Failed')[:200]})")
        time.sleep(5)
    raise RuntimeError(f"asset library: {aid} still not Active after {timeout_s // 60} min")
