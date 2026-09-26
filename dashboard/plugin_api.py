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

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

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
    # A previous run may have written the old bare-path .pth; always rewrite below.
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


# --- sidecar process + reverse proxy ---------------------------------------------
#
# The only thing reachable from outside the container is the dashboard, so a separate
# web process (eventually hermes-web) has to be served through this plugin's routes.

_VENV_PY = _SCRATCH / "venv" / "bin" / "python"
_SIDECAR_APP = Path(__file__).resolve().parent.parent / "sidecar" / "app.py"
_SIDECAR_LOG = _SCRATCH / "sidecar.log"
_sidecar: dict = {"proc": None, "port": None, "argv": None, "app": None}
_sidecar_lock = asyncio.Lock()
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host", "content-length"}
# Never forwarded from the client: credentials for the dashboard, and forwarding headers the
# proxy sets itself (a client's own X-Forwarded-* would otherwise come first and win).
_DROP = _HOP_BY_HOP | {"authorization", "cookie", "forwarded"}
_WEB_PREFIX = "/api/plugins/hermes-cloud-scratch/web"
_APP_COOKIE_STEM = "hermes_web"  # the sidecar only sees its own cookies, not the dashboard's


def _public_web_url() -> str | None:
    """Public URL of /web, from the dashboard's configured public URL — never from request headers."""
    from hermes_cli.dashboard_auth.prefix import resolve_public_url

    base = resolve_public_url() or os.environ.get("SCRATCH_PUBLIC_URL", "")
    return f"{base.rstrip('/')}{_WEB_PREFIX}" if base else None


def _sidecar_argv(app: str) -> list[str]:
    match app:
        case "demo":
            return [str(_SIDECAR_APP)]
        case "hermes-web":
            if not (url := _public_web_url()):
                raise ValueError("no public URL: set dashboard.public_url or SCRATCH_PUBLIC_URL")
            return ["-m", "hermes_web", "--external-url", url]
    raise ValueError(f"unknown app {app!r}; expected 'demo' or 'hermes-web'")


def _app_cookies(header: str) -> str:
    def name(pair: str) -> str:
        return pair.strip().split("=", 1)[0].removeprefix("__Secure-").removeprefix("__Host-")

    return "; ".join(p.strip() for p in header.split(";") if name(p).startswith(_APP_COOKIE_STEM))


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sidecar_running() -> bool:
    proc = _sidecar["proc"]
    return proc is not None and proc.poll() is None


def _sidecar_status() -> dict:
    proc = _sidecar["proc"]
    return {"running": _sidecar_running(), "app": _sidecar["app"], "pid": proc.pid if proc else None,
            "returncode": proc.poll() if proc else None, "port": _sidecar["port"], "argv": _sidecar["argv"],
            "log_tail": _SIDECAR_LOG.read_text()[-1500:] if _SIDECAR_LOG.exists() else ""}


async def _start_sidecar(app: str = "demo") -> dict:
    argv_tail = _sidecar_argv(app)
    async with _sidecar_lock:
        if _sidecar_running():
            return _sidecar_status()
        if not _VENV_PY.exists():
            result = await asyncio.to_thread(_venv_test, "starhtml")
            if not result.get("ok"):
                return {"running": False, "error": "venv setup failed", "venv": result}
        port = _free_port()
        argv = [str(_VENV_PY), *argv_tail, "--port", str(port)]
        log = open(_SIDECAR_LOG, "ab")
        proc = subprocess.Popen(argv, cwd=str(_SCRATCH), stdout=log, stderr=log, start_new_session=True,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        _sidecar.update(proc=proc, port=port, argv=argv, app=app)
        for _ in range(100):  # up to ~20s for the port to accept
            if proc.poll() is not None:
                break
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                break
            except OSError:
                await asyncio.sleep(0.2)
        return _sidecar_status()


@router.get("/sidecar")
async def sidecar_status():
    return _sidecar_status()


@router.post("/sidecar/start")
async def sidecar_start(app: str = "demo"):
    try:
        return await _start_sidecar(app)
    except ValueError as exc:
        return JSONResponse({"running": False, "error": str(exc)}, status_code=400)


@router.post("/sidecar/stop")
async def sidecar_stop():
    proc = _sidecar["proc"]
    if _sidecar_running():
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            await asyncio.to_thread(proc.wait, 10)
        if proc.poll() is None:
            proc.kill()
    return _sidecar_status()


@router.post("/wheel")
async def upload_wheel(request: Request, filename: str):
    """Install an uploaded wheel into the scratch venv — keeps private packages off GitHub."""
    if not filename.endswith(".whl") or "/" in filename or filename.startswith("."):
        return JSONResponse({"ok": False, "error": "filename must be a bare *.whl name"}, status_code=400)
    if not _VENV_PY.exists():
        return JSONResponse({"ok": False, "error": "venv missing; POST /venv-test first"}, status_code=409)
    wheels = _SCRATCH / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    target = wheels / filename
    data = await request.body()
    if len(data) > 100 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "wheel larger than 100 MB"}, status_code=413)
    target.write_bytes(data)
    uv = _find_uv()
    argv = [uv, "pip", "install", "--python", str(_VENV_PY), "--reinstall-package",
            filename.split("-")[0].replace("_", "-"), str(target)] if uv else [str(_VENV_PY), "-m", "pip", "install", str(target)]
    proc = await asyncio.to_thread(subprocess.run, argv, capture_output=True, text=True, timeout=600)
    return {"ok": proc.returncode == 0, "saved": str(target), "bytes": target.stat().st_size,
            "stdout": proc.stdout[-3000:], "stderr": proc.stderr[-3000:]}


@router.api_route("/web", methods=["GET", "HEAD"])
async def web_root(request: Request):
    return RedirectResponse(str(request.url.path) + "/", status_code=307)


@router.api_route("/web/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def web_proxy(request: Request, path: str):
    import httpx

    if not _sidecar_running():
        status = await _start_sidecar()
        if not status.get("running"):
            return JSONResponse({"error": "sidecar not running", "status": status}, status_code=502)
    full = request.url.path
    prefix = full[: len(full) - len(path)].rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    headers = [(k, v) for k, v in request.headers.items()
               if k.lower() not in _DROP and not k.lower().startswith("x-forwarded-")]
    if cookies := _app_cookies(request.headers.get("cookie", "")):
        headers.append(("cookie", cookies))
    headers += [("x-forwarded-for", request.client.host if request.client else ""),
                ("x-forwarded-prefix", prefix), ("x-forwarded-host", request.headers.get("host", "")),
                ("x-forwarded-proto", proto)]
    url = f"http://127.0.0.1:{_sidecar['port']}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
    upstream_req = client.build_request(request.method, url, headers=headers, content=request.stream())
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse({"error": f"upstream: {exc!r}"}, status_code=502)
    out_headers = []
    for k, v in upstream.headers.multi_items():
        if k.lower() in _HOP_BY_HOP:
            continue
        # Prefix-unaware apps get their redirects mapped; base-path-aware ones are left alone.
        if k.lower() == "location" and v.startswith("/") and not (v == prefix or v.startswith(prefix + "/")):
            v = prefix + v
        out_headers.append((k, v))

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response = StreamingResponse(body(), status_code=upstream.status_code)
    response.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in out_headers]
    if "text/event-stream" in upstream.headers.get("content-type", ""):
        response.raw_headers.append((b"x-accel-buffering", b"no"))
    return response


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
