# hermes-cloud-scratch

> **⚠️ Personal test scaffolding. Do not use.**
>
> This repo is a throwaway experiment for probing what a user plugin can do
> inside one Hermes Cloud instance. It is not a product, not supported, not
> reviewed for anyone else's threat model, and may be deleted or rewritten at
> any time. It is not affiliated with or endorsed by Nous Research. If you
> found it, please don't install it.

## What it is

A Hermes Agent plugin (installed into `$HERMES_HOME/plugins/`) whose dashboard
backend adds a few routes under `/api/plugins/hermes-cloud-scratch/`:

| Route | Purpose |
|---|---|
| `GET  /probe` | Report Hermes version, Python, paths, writable dirs, available tools |
| `GET  /sse` | Emit a few Server-Sent Events to check streaming survives the edge proxy |
| `POST /venv-test` | Build a venv under `$HERMES_HOME` that can import Hermes, add a third-party package, and report whether both import |
| `WS   /pty` | Terminal running `hermes chat --cli` (classic CLI, not the TUI), or `?mode=shell` |
| `GET  /sidecar`, `POST /sidecar/start?app=demo\|hermes-web`, `POST /sidecar/stop` | Manage the web process bound to 127.0.0.1 inside the container; the last started app resumes on the next `/web` request after a restart |
| `ANY  /web/*` | Streaming reverse proxy to that process (HTTP + SSE); sends `X-Forwarded-Prefix` |
| `/dashboard-plugins/hermes-cloud-scratch/web.html?next=…` (static plugin asset) | Entry and re-auth page for `/web`: redirects to `next` (a path under `/web/`, else its root). Not under `/api/`, so an expired session gets the dashboard's silent Portal sign-in (under `/api/` it only gets a 401 JSON, which hermes-web, started with `--reauth-url` pointing here, turns into a trip through this page); not a dashboard SPA route, so none of the dashboard is drawn. Bookmark this. The hidden tab `/hermes-cloud-scratch` just forwards here |
| `POST /wheel?filename=…` | Install an uploaded wheel into the scratch venv (keeps private packages off GitHub). Behind the dashboard login; it installs and therefore runs arbitrary code, by design |

All routes sit behind the dashboard's own auth gate (HTTP) or reuse the
dashboard's WebSocket ticket gate (`/pty`). They run code on the instance by
design (`/pty?mode=shell`, `/wheel`, `/venv-test`, and the CLI agent's own shell
tools) — the same trust as the dashboard login itself.

The `/web` sidecar is same-origin with the dashboard: the proxy only forwards and
accepts the app's own `hermes_web*` cookies, but any script the sidecar serves runs
with the dashboard's cookies in scope, so treat an XSS there as a dashboard compromise.

`client/scratch.py` is a local CLI (run with `uv run`) that logs in through the
dashboard's native-app OAuth flow, runs `/api/console` commands, calls the
probe routes, and attaches a local terminal to `/pty` (or the stock `/api/pty`).

No secrets live in this repo. Tokens the client obtains are stored locally
under `~/.config/hermes-cloud-scratch/` with `0600` permissions.

## License

Apache-2.0
