"""hermes-cloud-scratch dashboard backend — personal test scaffolding, do not use.

Mounted by the dashboard at /api/plugins/hermes-cloud-scratch/. HTTP routes are
behind the dashboard auth middleware; the WebSocket route reuses the dashboard's
own pre-accept gate (HTTP middleware does not run for WebSockets).
"""

import asyncio
import contextlib
import functools
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anyio
import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

_log = logging.getLogger("hermes-cloud-scratch")

router = APIRouter()

_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
_SCRATCH = _HOME / "scratch"
_VENV_PY = _SCRATCH / "venv" / "bin" / "python"


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


@functools.cache
def _find_uv() -> str | None:
    if found := shutil.which("uv"):
        return found
    roots = (Path("/opt/hermes/tools"), Path("/opt/hermes"))
    candidates = (c for r in roots if r.is_dir() for c in r.rglob("uv"))
    return next((str(c) for c in candidates if c.is_file() and os.access(c, os.X_OK)), None)


@router.get("/probe")
async def probe():
    try:
        from hermes_cli import __version__ as hermes_version
    except ImportError:
        hermes_version = None
    disk = shutil.disk_usage(_HOME) if _HOME.exists() else None
    meminfo = Path("/proc/meminfo")
    return {
        "hermes_version": hermes_version,
        "hermes_agent_dist": _version("hermes-agent"),
        "python": sys.version,
        "executable": sys.executable,
        "purelib": sysconfig.get_path("purelib"),
        "platform": platform.platform(),
        "uid": os.getuid(),
        "hermes_home": str(_HOME),
        "writable": {p: os.access(p, os.W_OK) for p in ("/opt/hermes", "/opt/hermes/.venv", str(_HOME), "/tmp")},
        "which": {name: shutil.which(name) for name in ("hermes", "git", "bash", "node", "python3")},
        "uv": _find_uv(),
        "importable": {
            m: importlib.util.find_spec(m) is not None
            for m in ("fastapi", "starlette", "ptyprocess", "starhtml", "hermes_bridge")
        },
        "versions": {d: _version(d) for d in ("fastapi", "starlette", "uvicorn", "pydantic", "rich")},
        "disk_free_gb": round(disk.free / 1e9, 2) if disk else None,
        "meminfo": "; ".join(
            line for line in meminfo.read_text().splitlines() if line.startswith(("MemTotal", "MemAvailable"))
        ) if meminfo.exists() else None,
    }


@router.get("/sse")
async def sse(count: int = 5, interval: float = 1.0):
    count, interval = max(1, min(count, 60)), max(0.1, min(interval, 10.0))

    async def events():
        for i in range(count):
            yield f"event: tick\ndata: {json.dumps({'i': i, 't': time.time()})}\n\n"
            await asyncio.sleep(interval)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# --- scratch venv ------------------------------------------------------------------


def _venv_test(package: str) -> dict:
    """Build $HERMES_HOME/scratch/venv that sees Hermes's sealed site-packages, install
    ``package`` into it, and check both import. Nothing under /opt/hermes is touched."""
    steps: list[dict] = []

    def run(argv: list[str], timeout: int = 240) -> bool:
        started = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        steps.append({
            "argv": argv, "rc": proc.returncode, "secs": round(time.monotonic() - started, 1),
            "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:],
        })  # fmt: skip
        return proc.returncode == 0

    if not (mod := re.fullmatch(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?(?:[<>=!~].*)?", package)):
        return {"ok": False, "error": f"not a requirement: {package!r}"}
    venv = _SCRATCH / "venv"
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    if not _VENV_PY.exists() and not run([sys._base_executable, "-m", "venv", "--without-pip", str(venv)]):
        return {"ok": False, "steps": steps}
    # Same interpreter version, so the venv's site dir follows from the scheme. addsitedir (not a
    # bare path) so the sealed venv's own .pth files run too — Hermes is an editable install whose
    # import hook lives in one. Appended after this venv's site-packages, so ours win on conflicts.
    sealed = sysconfig.get_path("purelib")
    site = Path(sysconfig.get_path("purelib", vars={"base": str(venv), "platbase": str(venv)}))
    (site / "zz_hermes_sealed.pth").write_text(f"import site; site.addsitedir({sealed!r})\n")

    ok = run([uv, "pip", "install", "--python", str(_VENV_PY), package]) if (uv := _find_uv()) else (
        run([str(_VENV_PY), "-m", "ensurepip"]) and run([str(_VENV_PY), "-m", "pip", "install", package])
    )
    name = mod[1].replace("-", "_")  # dist name; usually also the import name
    check = f"import json, hermes_cli, {name}; print(json.dumps([hermes_cli.__file__, {name}.__file__]))"
    return {"ok": ok and run([str(_VENV_PY), "-c", check], timeout=60), "venv": str(venv), "steps": steps}


@router.post("/venv-test")
async def venv_test(package: str = "starhtml"):
    try:
        return await asyncio.to_thread(_venv_test, package)
    except Exception as exc:  # report, don't 500 — this is a probe
        _log.exception("venv-test failed")
        return {"ok": False, "error": repr(exc)}


_MAX_WHEEL = 100 << 20


@router.post("/wheel")
async def upload_wheel(request: Request, filename: str, deps: bool = True):
    """Install an uploaded wheel into the scratch venv — keeps private packages off GitHub.

    ``deps=false`` installs it alone: e.g. hermes-bridge must use the instance's own
    hermes-agent (visible through the sealed-venv .pth), never a second copy from PyPI.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", filename):
        return JSONResponse({"ok": False, "error": "filename must be a bare *.whl name"}, status_code=400)
    if not _VENV_PY.exists():
        return JSONResponse({"ok": False, "error": "venv missing; POST /venv-test first"}, status_code=409)
    if int(request.headers.get("content-length") or 0) > _MAX_WHEEL:
        return JSONResponse({"ok": False, "error": "wheel larger than 100 MB"}, status_code=413)
    target = _SCRATCH / "wheels" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with target.open("wb") as f:
        async for chunk in request.stream():
            if (size := size + len(chunk)) > _MAX_WHEEL:
                break
            f.write(chunk)
    if size > _MAX_WHEEL:
        target.unlink()
        return JSONResponse({"ok": False, "error": "wheel larger than 100 MB"}, status_code=413)
    no_deps = [] if deps else ["--no-deps"]
    argv = (
        [uv, "pip", "install", "--python", str(_VENV_PY), *no_deps, "--reinstall-package", filename.split("-")[0]]
        if (uv := _find_uv())
        else [str(_VENV_PY), "-m", "pip", "install", "--force-reinstall", *no_deps]
    )
    proc = await asyncio.to_thread(subprocess.run, [*argv, str(target)], capture_output=True, text=True, timeout=600)
    return {"ok": proc.returncode == 0, "saved": str(target), "bytes": target.stat().st_size,
            "stdout": proc.stdout[-3000:], "stderr": proc.stderr[-3000:]}  # fmt: skip


# --- sidecar process + reverse proxy -------------------------------------------------
#
# The only thing reachable from outside the container is the dashboard, so a separate
# web process (hermes-web) is served through this plugin's /web routes. It is same-origin
# with the dashboard: anything it serves runs with the dashboard's cookies in scope.

App = Literal["demo", "hermes-web"]
_WEB_PREFIX = "/api/plugins/hermes-cloud-scratch/web"
_ENTRY = "/dashboard-plugins/hermes-cloud-scratch/web.html"  # dashboard/web.html: entry + re-auth
_APP_COOKIE_STEM = "hermes_web"  # the sidecar only sees (and sets) its own cookies
_SIDECAR_STATE = _SCRATCH / "sidecar.json"  # last started {app, pid}: survives restarts
_SIDECAR_LOG = _SCRATCH / "sidecar.log"
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
})  # fmt: skip
# Never forwarded from the client: the dashboard's credentials, and forwarding headers the
# proxy sets itself (a client's own X-Forwarded-* would otherwise come first and win).
_DROP = _HOP_BY_HOP | {"host", "authorization", "cookie", "forwarded"}


@dataclass(slots=True)
class _Sidecar:
    app: App | None = None
    proc: subprocess.Popen | None = None
    port: int | None = None
    stopping: asyncio.Task | None = None  # a stop letting running turns finish

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def draining(self) -> bool:
        return self.stopping is not None and not self.stopping.done()


# On SIGTERM hermes-web lets running turns finish for up to 180 s before it exits
# (a second SIGTERM exits it at once); a stop waits that long, and a little more.
_DRAIN_S = 200


_sidecar = _Sidecar()
_sidecar_lock = asyncio.Lock()


@functools.cache
def _http() -> httpx.AsyncClient:
    # trust_env=False: an HTTP(S)_PROXY in the container must not capture loopback dials.
    return httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(10.0, read=None))


def _public_url() -> str | None:
    """The dashboard's configured public URL — never from request headers."""
    from hermes_cli.dashboard_auth.prefix import resolve_public_url

    base = resolve_public_url() or os.environ.get("SCRATCH_PUBLIC_URL", "")
    return base.rstrip("/") or None


def _argv(app: App) -> list[str]:
    if app == "demo":
        return [str(Path(__file__).resolve().parents[1] / "sidecar" / "app.py")]
    if not (url := _public_url()):
        raise ValueError("no public URL: set dashboard.public_url or SCRATCH_PUBLIC_URL")
    # web.html is outside /api/: an expired session there gets the dashboard's silent SSO,
    # where /web (an API route) only gets a 401 that hermes-web answers by going there.
    return ["-m", "hermes_web", "--external-url", f"{url}{_WEB_PREFIX}", "--reauth-url", f"{url}{_ENTRY}"]


def _is_app_cookie(pair: str) -> bool:
    name = pair.strip().split("=", 1)[0].removeprefix("__Secure-").removeprefix("__Host-")
    return name.startswith(_APP_COOKIE_STEM)


def _saved_state() -> dict:
    with contextlib.suppress(FileNotFoundError, ValueError):
        return json.loads(_SIDECAR_STATE.read_text())
    return {}


def _is_sidecar(pid: int) -> bool:
    """A saved pid may since have been reused by an unrelated process: check it is ours."""
    with contextlib.suppress(OSError):
        return str(_VENV_PY).encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    return False


def _killpg(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _kill(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


async def _wait_exit(pid: int, proc: subprocess.Popen | None, timeout: float) -> None:
    if proc is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            await asyncio.to_thread(proc.wait, timeout)
        return
    deadline = time.monotonic() + timeout
    while _is_sidecar(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.5)


async def _terminate(pid: int, proc: subprocess.Popen | None = None, *, drain: bool = True) -> None:
    """Stop the sidecar. SIGTERM goes to the app alone, so hermes-web can let its
    running turns finish (its profile workers keep serving them) — or twice, to
    exit at once. Then the rest of its process group goes: SIGTERM, then SIGKILL."""
    _kill(pid, signal.SIGTERM)
    if not drain:
        await asyncio.sleep(0.2)  # two signals too close together can arrive as one
        _kill(pid, signal.SIGTERM)
    await _wait_exit(pid, proc, _DRAIN_S if drain else 10)
    _killpg(pid, signal.SIGTERM)
    await asyncio.sleep(1)
    _killpg(pid, signal.SIGKILL)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _log_tail(n: int = 1500) -> str:
    with contextlib.suppress(FileNotFoundError), _SIDECAR_LOG.open("rb") as f:
        f.seek(max(0, f.seek(0, os.SEEK_END) - n))
        return f.read().decode(errors="replace")
    return ""


def _status() -> dict:
    proc = _sidecar.proc
    return {"running": _sidecar.running, "stopping": _sidecar.draining, "app": _sidecar.app, "pid": proc and proc.pid,
            "returncode": proc and proc.poll(), "port": _sidecar.port, "argv": proc and proc.args,
            "log_tail": _log_tail()}  # fmt: skip


async def _stop_locked(*, drain: bool = True) -> None:
    if _sidecar.running:
        await _terminate(_sidecar.proc.pid, _sidecar.proc, drain=drain)
    _sidecar.proc = None


async def _stop(*, drain: bool) -> None:
    async with _sidecar_lock:
        await _stop_locked(drain=drain)  # the saved app stays: /web resumes it after a restart


async def _start_locked(app: App) -> dict:
    if _sidecar.running:
        if _sidecar.app == app:
            return _status()
        await _stop_locked(drain=False)  # switching apps: the caller is waiting on this request
    if (stale := _saved_state().get("pid")) and _sidecar.proc is None and _is_sidecar(stale):
        await _terminate(stale, drain=False)  # orphan from before a dashboard restart
    argv = [str(_VENV_PY), *_argv(app), "--port", str(port := _free_port())]
    with _SIDECAR_LOG.open("ab") as log:
        proc = subprocess.Popen(argv, cwd=_SCRATCH, stdout=log, stderr=log, start_new_session=True,
                                env=os.environ | {"PYTHONUNBUFFERED": "1"})  # fmt: skip
    _sidecar.app, _sidecar.proc, _sidecar.port = app, proc, port
    _SIDECAR_STATE.write_text(json.dumps({"app": app, "pid": proc.pid}))
    for _ in range(100):  # up to ~20s for the port to accept
        if proc.poll() is not None:
            break
        with contextlib.suppress(OSError):
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            break
        await asyncio.sleep(0.2)
    return _status()


@router.get("/sidecar")
async def sidecar_status():
    return _status()


@router.post("/sidecar/start")
async def sidecar_start(app: App = "demo"):
    if _sidecar.draining:
        return JSONResponse({**_status(), "error": "still stopping: running turns are finishing; retry shortly"},
                            status_code=409)  # fmt: skip
    if not _VENV_PY.exists():
        return JSONResponse({"running": False, "error": "venv missing; POST /venv-test first"}, status_code=409)
    async with _sidecar_lock:
        try:
            return await _start_locked(app)
        except ValueError as exc:
            return JSONResponse({"running": False, "error": str(exc)}, status_code=400)


@router.post("/sidecar/stop")
async def sidecar_stop(drain: bool = True):
    """Stop the sidecar. By default running turns finish first (up to ``_DRAIN_S``):
    the stop carries on in the background and ``GET /sidecar`` reports ``stopping``
    until it's done. ``drain=false`` stops at once (and cuts a drain short)."""
    if _sidecar.draining:
        if not drain and _sidecar.proc is not None:
            _kill(_sidecar.proc.pid, signal.SIGTERM)  # hermes-web's second signal: exit now
    else:
        _sidecar.stopping = asyncio.create_task(_stop(drain=drain))
        _sidecar.stopping.add_done_callback(_log_stop_failure)
    if not drain:
        # shielded: a client that hangs up mustn't cancel the stop half-way
        await asyncio.shield(_sidecar.stopping)
    return _status()


def _log_stop_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        _log.error("sidecar stop failed: %s", exc, exc_info=exc)


@router.api_route("/web", methods=["GET", "HEAD"])
async def web_root(request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"{request.url.path}/{query}", status_code=307)  # path-only: keeps the public origin


@router.api_route("/web/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def web_proxy(request: Request, path: str):
    if not _sidecar.running:
        # Only resume an app that was explicitly started before; a GET never builds venvs.
        if not (app := _saved_state().get("app")) or not _VENV_PY.exists():
            return JSONResponse({"error": "no sidecar; POST /sidecar/start?app=…"}, status_code=502)
        async with _sidecar_lock:
            status = _status() if _sidecar.running else await _start_locked(app)
        if not status["running"]:
            return JSONResponse({"error": "sidecar failed to start", "status": status}, status_code=502)

    prefix = request.url.path.removesuffix(path).rstrip("/")
    # Forward the raw (still percent-encoded) path so %2F, %3F and %23 survive.
    raw_path = request.scope["raw_path"].removeprefix(prefix.encode()) or b"/"
    if query := request.scope["query_string"]:
        raw_path += b"?" + query
    headers = [(k, v) for k, v in request.headers.items() if k not in _DROP and not k.startswith("x-forwarded-")]
    if cookies := "; ".join(p.strip() for p in request.headers.get("cookie", "").split(";") if _is_app_cookie(p)):
        headers.append(("cookie", cookies))
    headers += [
        ("x-forwarded-for", request.client.host if request.client else ""),
        ("x-forwarded-prefix", prefix),
        ("x-forwarded-host", request.headers.get("host", "")),
        ("x-forwarded-proto", request.headers.get("x-forwarded-proto", request.url.scheme)),
    ]
    upstream_req = _http().build_request(
        request.method,
        httpx.URL(f"http://127.0.0.1:{_sidecar.port}").copy_with(raw_path=raw_path),
        headers=headers,
        content=request.stream() if request.method not in {"GET", "HEAD"} else None,
    )
    try:
        upstream = await _http().send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"upstream: {exc!r}"}, status_code=502)

    out = []
    for k, v in upstream.headers.raw:
        if k in _HOP_BY_HOP or (k == b"set-cookie" and not _is_app_cookie(v.decode("latin-1"))):
            continue
        # Prefix-unaware apps get their redirects mapped; base-path-aware ones are left alone.
        if k == b"location" and v.startswith(b"/") and not (v == prefix.encode() or v.startswith(f"{prefix}/".encode())):
            v = prefix.encode() + v
        out.append((k, v))
    if upstream.headers.get("content-type", "").startswith("text/event-stream"):
        out.append((b"x-accel-buffering", b"no"))

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            with anyio.CancelScope(shield=True):  # close even when the client went away
                await upstream.aclose()

    response = StreamingResponse(body(), status_code=upstream.status_code)
    response.raw_headers = out
    return response


# --- terminal ------------------------------------------------------------------------


def _pty_argv(mode: Literal["cli", "shell"]) -> list[str]:
    if mode == "shell":
        return [shutil.which("bash") or "/bin/sh", "-l"]
    hermes = [h] if (h := shutil.which("hermes")) else [sys.executable, "-m", "hermes_cli.main"]
    return [*hermes, "chat", "--cli"]


@router.websocket("/pty")
async def pty(ws: WebSocket, mode: Literal["cli", "shell"] = "cli", cols: int = 120, rows: int = 32):
    """A terminal on the instance: ``hermes chat --cli`` (the classic CLI) or a login shell.

    Same trust as the rest of this plugin (and as the CLI agent's own shell tools)."""
    from hermes_cli.pty_bridge import PtyBridge
    from hermes_cli.web_routers.chat_ws import _ws_gate
    from hermes_cli.web_server_chat import _legacy_pump

    if await _ws_gate(ws, "pty") is None:
        return
    await ws.accept()
    # No COLUMNS/LINES: the PTY winsize is authoritative, so resizes reflow.
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_TUI")}
    env |= {"TERM": "xterm-256color", "HOME": str(_HOME)}
    cwd = next((d for d in (_HOME / "workspace", _HOME) if d.is_dir()), _HOME)
    bridge = await asyncio.to_thread(PtyBridge.spawn, _pty_argv(mode), cwd=str(cwd), env=env, cols=cols, rows=rows)
    await _legacy_pump(ws, bridge)
