"""hermes-cloud-scratch dashboard backend — personal test scaffolding, do not use.

Mounted by the dashboard at /api/plugins/hermes-cloud-scratch/. HTTP routes are
behind the dashboard auth middleware; the WebSocket route reuses the dashboard's
own pre-accept gate (HTTP middleware does not run for WebSockets).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

_log = logging.getLogger("hermes-cloud-scratch")

router = APIRouter()

_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
_SCRATCH = _HOME / "scratch"


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


def _writable(path: Path) -> bool:
    return path.exists() and os.access(path, os.W_OK)


def _find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for root in (Path("/opt/hermes/tools"), Path("/opt/hermes")):
        if root.is_dir():
            for candidate in root.rglob("uv"):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    return None


@router.get("/probe")
async def probe():
    try:
        from hermes_cli import __version__ as hermes_version
    except Exception:
        hermes_version = None
    disk = shutil.disk_usage(_HOME) if _HOME.exists() else None
    mem_total = None
    with contextlib.suppress(Exception):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(("MemTotal", "MemAvailable")):
                mem_total = (mem_total or "") + line + "; "
    return {
        "hermes_version": hermes_version,
        "hermes_agent_dist": _version("hermes-agent"),
        "python": sys.version,
        "executable": sys.executable,
        "purelib": sysconfig.get_paths()["purelib"],
        "platform": platform.platform(),
        "uid": os.getuid(),
        "hermes_home": str(_HOME),
        "writable": {p: _writable(Path(p)) for p in ("/opt/hermes", "/opt/hermes/.venv", str(_HOME), "/tmp")},
        "which": {name: shutil.which(name) for name in ("hermes", "git", "bash", "node", "python3")},
        "uv": _find_uv(),
        "importable": {m: _importable(m) for m in ("fastapi", "starlette", "ptyprocess", "starhtml", "hermes_bridge")},
        "versions": {d: _version(d) for d in ("fastapi", "starlette", "uvicorn", "pydantic", "rich")},
        "disk_free_gb": round(disk.free / 1e9, 2) if disk else None,
        "meminfo": mem_total,
        "env_flags": {k: os.environ.get(k) for k in ("SCRATCH_ALLOW_SHELL", "HERMES_WRITE_SAFE_ROOT", "HERMES_RUNTIME_DIR")},
    }


@router.get("/sse")
async def sse(count: int = 5, interval: float = 1.0):
    count = max(1, min(count, 60))
    interval = max(0.1, min(interval, 10.0))

    async def events():
        for i in range(count):
            yield f"event: tick\ndata: {json.dumps({'i': i, 't': time.time()})}\n\n"
            await asyncio.sleep(interval)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _venv_test(package: str) -> dict:
    """Build $HERMES_HOME/scratch/venv that sees Hermes's sealed site-packages via a .pth,
    install ``package`` into it, and check both import. Nothing under /opt/hermes is touched."""
    steps: list[dict] = []

    def run(argv: list[str], timeout: int = 240) -> bool:
        started = time.time()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        steps.append({
            "argv": argv, "rc": proc.returncode, "secs": round(time.time() - started, 1),
            "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]})
        return proc.returncode == 0

    venv = _SCRATCH / "venv"
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    base_python = getattr(sys, "_base_executable", None) or sys.executable
    if not (venv / "bin" / "python").exists():
        if not run([base_python, "-m", "venv", "--without-pip", str(venv)]):
            return {"ok": False, "steps": steps}
    vpy = str(venv / "bin" / "python")
    site = subprocess.run(
        [vpy, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True).stdout.strip()
    # addsitedir (not a bare path) so the sealed venv's own .pth files run too — Hermes is an
    # editable install whose import hook lives in one. Appended after this venv's site-packages,
    # so packages installed here win on version conflicts.
    (Path(site) / "zz_hermes_sealed.pth").write_text(
        f"import site; site.addsitedir({sysconfig.get_paths()['purelib']!r})\n")
    steps.append({"pth": str(Path(site) / "zz_hermes_sealed.pth"), "points_to": sysconfig.get_paths()["purelib"]})

    uv = _find_uv()
    if uv:
        ok = run([uv, "pip", "install", "--python", vpy, package])
    else:
        ok = run([vpy, "-m", "ensurepip"]) and run([vpy, "-m", "pip", "install", package])
    mod = package.split("[")[0].split("=")[0].split("<")[0].split(">")[0].replace("-", "_")
    check = (
        "import importlib.metadata as m, json, hermes_cli, starlette\n"
        f"import {mod}\n"
        "print(json.dumps({'hermes_cli': hermes_cli.__file__, 'starlette': m.version('starlette'),"
        f" '{mod}': getattr({mod}, '__version__', None)}}))")
    imported = run([vpy, "-c", check], timeout=60)
    return {"ok": ok and imported, "venv": str(venv), "steps": steps}


@router.post("/venv-test")
async def venv_test(package: str = "starhtml"):
    try:
        return await asyncio.to_thread(_venv_test, package)
    except Exception as exc:  # report, don't 500 — this is a probe
        _log.exception("venv-test failed")
        return {"ok": False, "error": repr(exc)}


def _pty_argv(mode: str) -> list[str]:
    if mode == "shell":
        return [shutil.which("bash") or "/bin/sh", "-l"]
    hermes = shutil.which("hermes")
    base = [hermes] if hermes else [sys.executable, "-m", "hermes_cli.main"]
    return [*base, "chat", "--cli"]


@router.websocket("/pty")
async def pty(ws: WebSocket, mode: str = "cli", cols: int = 120, rows: int = 32):
    from hermes_cli.pty_bridge import PtyBridge
    from hermes_cli.web_routers.chat_ws import _ws_gate
    from hermes_cli.web_server_chat import _RESIZE_RE

    gate = await _ws_gate(ws, "pty")
    if gate is None:
        return
    if mode not in {"cli", "shell"}:
        await ws.close(code=4400, reason="mode must be cli or shell")
        return
    if mode == "shell" and os.environ.get("SCRATCH_ALLOW_SHELL") != "1":
        await ws.close(code=4403, reason="shell mode disabled (set SCRATCH_ALLOW_SHELL=1)")
        return
    await ws.accept()
    _log.info("scratch pty accepted mode=%s peer=%s", mode, gate[0])

    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_TUI")}
    env.update({"TERM": "xterm-256color", "HOME": str(_HOME), "COLUMNS": str(cols), "LINES": str(rows)})
    cwd = _HOME / "workspace"
    if not cwd.is_dir():
        cwd = _HOME
    bridge = await asyncio.to_thread(
        PtyBridge.spawn, _pty_argv(mode), cwd=str(cwd), env=env, cols=cols, rows=rows)
    loop = asyncio.get_running_loop()

    async def pty_to_ws() -> None:
        try:
            while True:
                chunk = await loop.run_in_executor(None, bridge.read, 0.2)
                if chunk is None:
                    return
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                await ws.send_bytes(chunk)
        except Exception:
            return
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    reader = asyncio.create_task(pty_to_ws())
    try:
        while True:
            try:
                msg = await ws.receive()
            except RuntimeError:
                break
            if msg.get("type") == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode() if isinstance(text, str) else b""
            if not raw:
                continue
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                bridge.resize(cols=int(match.group(1)), rows=int(match.group(2)))
                continue
            if not await bridge.write(raw):
                break
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(bridge.close)
