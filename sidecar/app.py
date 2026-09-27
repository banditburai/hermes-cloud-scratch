"""Throwaway sidecar for hermes-cloud-scratch — personal test scaffolding, do not use.

Runs from $HERMES_HOME/scratch/venv, bound to 127.0.0.1 only; the plugin's /web/*
route proxies to it. It exists to answer two questions: does HTTP + SSE survive the
dashboard -> plugin -> sidecar hop, and what breaks when an app that assumes it
lives at "/" is served under /api/plugins/<name>/web/.
"""

import argparse
import asyncio
import html
import json
import os
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

CSS = "body{font-family:system-ui;margin:2rem;max-width:48rem} .ok{color:#1a7f37} .bad{color:#cf222e} pre{background:#f6f8fa;padding:1rem}"


def _prefix(request: Request) -> str:
    return html.escape(request.headers.get("x-forwarded-prefix", "").rstrip("/"))


async def index(request: Request):
    p = _prefix(request)
    return HTMLResponse(f"""<!doctype html><title>scratch sidecar</title>
<link rel="stylesheet" href="{p}/static/app.css">
<h1>scratch sidecar</h1>
<p>pid {os.getpid()} · forwarded prefix <code>{p or "(none)"}</code></p>
<ul>
  <li><a href="/whoami">absolute link /whoami</a> (expected to escape the prefix)</li>
  <li><a href="whoami">relative link whoami</a></li>
  <li><a href="{p}/whoami">prefixed link {p}/whoami</a></li>
</ul>
<h2>SSE</h2><pre id="log"></pre>
<script>
  const log = document.getElementById('log');
  const es = new EventSource('sse?count=5');
  es.addEventListener('tick', e => log.textContent += 'tick ' + e.data + '\\n');
  es.addEventListener('done', () => {{ log.textContent += 'done\\n'; es.close(); }});
  es.onerror = () => log.textContent += 'error\\n';
</script>""")


async def css(_request: Request):
    return Response(CSS, media_type="text/css")


async def whoami(request: Request):
    return JSONResponse({
        "path": request.url.path, "root_path": request.scope.get("root_path"),
        "headers": {k: v for k, v in request.headers.items() if k not in {"cookie", "authorization"}},
        "has_cookie": "cookie" in request.headers, "has_authorization": "authorization" in request.headers})


async def echo(request: Request):
    body = await request.body()
    return JSONResponse({"method": request.method, "len": len(body), "body": body.decode(errors="replace")[:2000]})


async def sse(request: Request):
    try:
        count = max(1, min(int(request.query_params.get("count", 5)), 120))
        interval = max(0.05, min(float(request.query_params.get("interval", 1.0)), 10.0))
    except ValueError:
        return JSONResponse({"error": "count and interval must be numbers"}, status_code=400)

    async def events():
        for i in range(count):
            if await request.is_disconnected():
                return
            yield f"event: tick\ndata: {json.dumps({'i': i, 't': time.time()})}\n\n"
            await asyncio.sleep(interval)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


app = Starlette(routes=[
    Route("/", index), Route("/static/app.css", css), Route("/whoami", whoami),
    Route("/echo", echo, methods=["POST", "PUT"]), Route("/sse", sse),
])

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
