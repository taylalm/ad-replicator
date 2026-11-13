"""Filesystem locations and the ma_client import (by path, so the repo layout stays as is)."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
UI_DIR = SERVER_DIR.parent
REPO = UI_DIR.parent
RUNS_DIR = REPO / "runs"
# UI_DATA_DIR: writable job cache (serverless: /tmp/...); jobs are rebuilt from the ModelArk sessions when it is empty
DATA_DIR = Path(os.environ.get("UI_DATA_DIR") or (UI_DIR / "data" / "jobs"))
TOOLS_DIR = REPO / "tools"


def ensure_ffmpeg() -> None:
    """The code folder cannot run the bundled ffmpeg (read-only, ZIP extraction drops the execute bit), so it runs from
    /tmp. run.sh copies it there, but that copy can fail (e.g. "text file busy" while an older process still runs it)
    and a serverless /tmp can be cleared while the process lives on. Without a usable copy, ad rebuilds, trims and
    caption exports all stopped with "No ffmpeg exe could be found": copy it again and point IMAGEIO_FFMPEG_EXE at it,
    which every ffmpeg call and the assembler subprocess read."""
    exe = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
        return
    spec = importlib.util.find_spec("imageio_ffmpeg")
    found = sorted(Path(list(spec.submodule_search_locations)[0], "binaries").glob("ffmpeg-linux-*")) if spec and spec.submodule_search_locations else []
    if not found:
        return  # no bundled Linux build (local runs): imageio_ffmpeg finds its own binary or the system ffmpeg
    import shutil
    dest = exe or "/tmp/ffmpeg"
    part = f"{dest}.{os.getpid()}.part"
    shutil.copy(found[0], part)
    os.chmod(part, 0o755)
    os.replace(part, dest)  # a rename works even while another process still runs the old file
    os.environ["IMAGEIO_FFMPEG_EXE"] = dest


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ma_client = None


def ma_client():
    """tools/ma_client.py, loaded lazily (it only needs the API key when a Client is built)."""
    global _ma_client
    if _ma_client is None:
        _ma_client = load_module("ma_client", TOOLS_DIR / "ma_client.py")
    return _ma_client
