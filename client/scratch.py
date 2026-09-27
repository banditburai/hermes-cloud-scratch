# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets>=13", "httpx>=0.27"]
# ///
"""Local client for one Hermes Cloud instance — personal test scaffolding, do not use.

    uv run client/scratch.py --url https://<name>.agents.nousresearch.com login
    uv run client/scratch.py console "plugins list"
    uv run client/scratch.py http GET probe
    uv run client/scratch.py http GET sse --stream
    uv run client/scratch.py http POST "sidecar/start?app=hermes-web"
    uv run client/scratch.py term            # classic `hermes chat --cli` via the scratch plugin
    uv run client/scratch.py term --tui      # stock /api/pty (hermes --tui)
    uv run client/scratch.py term --shell    # a login shell on the instance

The URL is remembered after the first `--url`. Tokens live in
~/.config/hermes-cloud-scratch/ (0600).
"""

import argparse
import asyncio
import base64
import hashlib
import http.server
import json
import os
import secrets
import shutil
import signal
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
import websockets

PLUGIN = "hermes-cloud-scratch"
CONFIG_DIR = Path.home() / ".config" / PLUGIN
STATE_FILE = CONFIG_DIR / "state.json"


# --- local state -------------------------------------------------------------

def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_FILE.with_suffix(".tmp")
    # Created 0600 from the start: tokens are never world-readable, even briefly.
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def _base_url(args, state: dict) -> str:
    url = (args.url or state.get("url") or "").rstrip("/")
    if not url:
        sys.exit("No instance URL yet — pass --url https://<name>.agents.nousresearch.com")
    if args.url and url != state.get("url"):
        _save({"url": url})  # new instance: drop tokens bound to the old one
    return url


# --- native-app OAuth (RFC 8252 loopback + PKCE) ---------------------------

def login(base: str) -> None:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state_param = secrets.token_urlsafe(16)
    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if not self.path.startswith("/callback"):
                return self.send_error(404)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and return to the terminal.")
            done.set()

        def log_message(self, *_):
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            query = urllib.parse.urlencode({
                "provider": "nous", "code_challenge": challenge, "code_challenge_method": "S256",
                "redirect_uri": f"http://127.0.0.1:{server.server_port}/callback", "state": state_param})
            auth_url = f"{base}/auth/native/authorize?{query}"
            print(f"Opening browser for Nous Portal sign-in…\n  {auth_url}")
            webbrowser.open(auth_url)
            if not done.wait(timeout=300):
                sys.exit("Timed out waiting for the sign-in callback.")
        finally:
            server.shutdown()
    if result.get("state") != state_param or "code" not in result:
        sys.exit(f"Bad callback: {result}")
    resp = httpx.post(f"{base}/auth/native/token", json={"code": result["code"], "code_verifier": verifier}, timeout=30)
    resp.raise_for_status()
    tokens = resp.json()
    state = _load()
    state.update({"url": base, "tokens": tokens})
    _save(state)
    print(f"Signed in as user {tokens.get('user_id')} (provider {tokens.get('provider')}).")


def _access_token(base: str, *, force_refresh: bool = False) -> str:
    state = _load()
    tokens = state.get("tokens")
    if not tokens:
        sys.exit("Not signed in — run `login` first.")
    expires_at = float(tokens.get("expires_at") or 0)
    if force_refresh or (expires_at and expires_at - time.time() < 60):
        resp = httpx.post(f"{base}/auth/native/refresh", json={
            "refresh_token": tokens["refresh_token"], "provider": tokens.get("provider", "")}, timeout=30)
        if resp.status_code == 401:
            sys.exit("Session expired — run `login` again.")
        resp.raise_for_status()
        tokens = resp.json()
        state["tokens"] = tokens
        _save(state)
    return tokens["access_token"]


def _request(base: str, method: str, path: str, *, timeout: float = 60, **kw) -> httpx.Response:
    for attempt in range(2):
        headers = {"Authorization": f"Bearer {_access_token(base, force_refresh=attempt == 1)}"}
        resp = httpx.request(method, f"{base}{path}", headers=headers, timeout=timeout, **kw)
        if resp.status_code != 401:
            return resp
    return resp


def _ws_ticket(base: str) -> str:
    resp = _request(base, "POST", "/api/auth/ws-ticket")
    resp.raise_for_status()
    return resp.json()["ticket"]


def _ws_url(base: str, path: str, **params) -> str:
    params["ticket"] = _ws_ticket(base)
    return f"{base.replace('http', 'ws', 1)}{path}?{urllib.parse.urlencode(params)}"


# --- /api/console --------------------------------------------------------------

async def console(base: str, line: str, yes: bool) -> int:
    async with websockets.connect(_ws_url(base, "/api/console"), max_size=None) as ws:
        await ws.send(json.dumps({"type": "command", "line": line}))
        rc = 0
        async for raw in ws:
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "ready":
                continue
            if kind == "output":
                print(frame.get("data", ""), end="" if frame.get("data", "").endswith("\n") else "\n")
            elif kind == "error":
                print(f"error: {frame.get('message')}", file=sys.stderr)
                rc = 1
            elif kind == "confirm_required":
                print(f"confirm: {frame.get('message')}")
                if yes:
                    continue  # confirm after the matching `complete`
                print("(re-run with --yes to confirm)")
            elif kind == "complete":
                if frame.get("status") == "confirm_required" and yes:
                    yes = False  # confirm exactly once
                    await ws.send(json.dumps({"type": "confirm", "command": frame.get("command")}))
                    continue
                if frame.get("status") not in {"ok", "idle", "clear"}:
                    rc = rc or 1
                return rc
        return rc


# --- terminal attach --------------------------------------------------------------

async def term(base: str, path: str, params: dict) -> None:
    import termios
    import tty

    cols, rows = shutil.get_terminal_size((120, 32))
    params = {**params, "cols": cols, "rows": rows}
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    loop = asyncio.get_running_loop()
    async with websockets.connect(_ws_url(base, path, **params), max_size=None, ping_interval=20) as ws:
        tty.setraw(fd)
        try:
            await ws.send(f"\x1b[RESIZE:{cols};{rows}]")

            pending: set[asyncio.Task] = set()  # keep resize sends referenced until done

            def on_resize(*_):
                c, r = shutil.get_terminal_size((120, 32))
                pending.add(task := loop.create_task(ws.send(f"\x1b[RESIZE:{c};{r}]")))
                task.add_done_callback(pending.discard)

            loop.add_signal_handler(signal.SIGWINCH, on_resize)
            queue: asyncio.Queue[bytes] = asyncio.Queue()
            loop.add_reader(fd, lambda: queue.put_nowait(os.read(fd, 4096)))

            async def pump_in():
                while True:
                    data = await queue.get()
                    if not data:
                        return
                    await ws.send(data)

            async def pump_out():
                async for msg in ws:
                    if isinstance(msg, str):
                        msg = msg.encode()
                    os.write(sys.stdout.fileno(), msg)

            tasks = [asyncio.create_task(pump_in()), asyncio.create_task(pump_out())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
        finally:
            loop.remove_reader(fd)
            loop.remove_signal_handler(signal.SIGWINCH)
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    code, reason = ws.close_code, ws.close_reason
    print(f"\r\n[disconnected: {code} {reason or ''}]")


# --- plugin HTTP -----------------------------------------------------------------

def http_cmd(base: str, args) -> None:
    body = None
    if args.data:
        body = Path(args.data[1:]).read_bytes() if args.data.startswith("@") else args.data.encode()
    started = time.monotonic()
    with httpx.stream(args.method.upper(), f"{base}/api/plugins/{PLUGIN}/{args.path.lstrip('/')}",
                      headers={"Authorization": f"Bearer {_access_token(base)}"}, content=body,
                      timeout=None, follow_redirects=False) as resp:
        print(resp.status_code, resp.headers.get("content-type"), resp.headers.get("location") or "")
        if args.stream:
            for line in filter(None, resp.iter_lines()):
                print(f"+{time.monotonic() - started:5.2f}s  {line}")
        elif resp.read() and resp.headers.get("content-type", "").startswith("application/json"):
            print(json.dumps(resp.json(), indent=2))
        else:
            print(resp.text[:3000])


# --- main -----------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="instance dashboard URL (remembered)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    c = sub.add_parser("console")
    c.add_argument("line")
    c.add_argument("--yes", action="store_true", help="auto-confirm a mutating command")
    h = sub.add_parser("http", help="authed request to a plugin path, e.g. `http POST sidecar/start`")
    h.add_argument("method")
    h.add_argument("path", help="relative to /api/plugins/hermes-cloud-scratch/")
    h.add_argument("--data", help="request body, or @file to send a file")
    h.add_argument("--stream", action="store_true", help="print the body as it arrives (SSE)")
    t = sub.add_parser("term")
    g = t.add_mutually_exclusive_group()
    g.add_argument("--tui", action="store_true", help="stock /api/pty (hermes --tui)")
    g.add_argument("--shell", action="store_true", help="bash via the scratch plugin")
    args = p.parse_args()

    base = _base_url(args, _load())
    match args.cmd:
        case "login":
            login(base)
        case "console":
            sys.exit(asyncio.run(console(base, args.line, args.yes)))
        case "http":
            http_cmd(base, args)
        case "term" if args.tui:
            asyncio.run(term(base, "/api/pty", {}))
        case "term":
            asyncio.run(term(base, f"/api/plugins/{PLUGIN}/pty", {"mode": "shell" if args.shell else "cli"}))

if __name__ == "__main__":
    main()
