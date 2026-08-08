# claude-usage

A live **Claude.ai usage dashboard** for the terminal — every rate limit,
credit balance, and account detail, rendered as a responsive panel that reflows
from a full control panel down to a single 40×1 status strip.

```
✳ Claude Usage                              ⟳ 2:30pm

LIMITS
5-hour   ██████████░░░░░░░░░░░░░░░░░░░░  42.5%
          ↺ 3h 29m · 6:00pm
7-day    ██████████████████████████░░░  88.0%  (warning)
          ↺ 4d 9h · Wed 12:00am

CREDITS
Spent                             $12.34 of $50.00
Balance                                    $37.66
```

It reads the same private endpoints the claude.ai web app uses, authenticating
with your existing browser session cookie — no API key, nothing to configure.

> **Unofficial.** This project is not affiliated with or endorsed by Anthropic.
> It relies on undocumented `claude.ai` endpoints that can change at any time;
> the script is written to degrade gracefully when they do.

## Features

- **Responsive layout.** One renderer picks the right form for the window:
  a full control panel (≥46×20), a compact vertical stack, a horizontal gauge
  strip for short windows, or a vertical bar chart for narrow/tall ones — down
  to a 40×5 (even 40×1) cell.
- **Everything the API exposes.** Session/weekly limits, model-scoped limits,
  credits and spend, extra usage, plus account, connectors/MCP, cowork status,
  live code sessions, and recent chats (large windows only).
- **Warm "heat" gauge.** Bars shift from soft tan through Claude orange to rust
  red as a limit fills, so severity reads at a glance.
- **Resilient.** Every secondary endpoint is best-effort — one dead endpoint
  never blanks the panel. Cloudflare challenges and expired sessions are
  reported distinctly.

## Install

Requires Python 3.9+.

Install as a command with [pipx](https://pipx.pypa.io):

```bash
pipx install git+https://github.com/parsa-j42/claude-usage
# or from a local checkout:
pipx install .
```

This puts a `claude-usage` command on your PATH. To also read cookies from the
browser automatically, install the optional extra:

```bash
pipx install "claude-usage[cookies] @ git+https://github.com/parsa-j42/claude-usage"
```

`browser-cookie3` (the `cookies` extra) is optional; without it, supply the
cookie manually (below).

## Usage

```bash
claude-usage              # live dashboard, refresh every 2 min
claude-usage -n 30        # refresh core usage every 30s
```

Keys, while running:

| Key            | Action        |
| -------------- | ------------- |
| `r` / `space`  | refresh now   |
| `q` / `Ctrl-C` | quit          |

### Options

| Flag                   | Default | Meaning                                             |
| ---------------------- | ------- | --------------------------------------------------- |
| `-n, --interval`       | `120`   | Core usage refresh interval (seconds).              |
| `--extras-interval`    | `300`   | Refresh interval for panel extras (sessions, chats).|
| `--bootstrap-interval` | `0`     | Re-fetch account/org config every N s (0 = once).   |
| `--cookie`             | —       | Session cookie, e.g. `'sessionKey=...'`.            |
| `--org`                | —       | Organization UUID (see below).                      |

## Authentication

The tool needs your logged-in `claude.ai` session cookie.

1. **Automatic (default).** With `browser-cookie3` installed, the cookie is read
   from Chrome/Chromium. If your keyring is locked, decryption can fail — unlock
   it or use the manual path.
2. **Manual.** Copy the `sessionKey` cookie from DevTools and pass it:
   ```bash
   claude-usage --cookie 'sessionKey=sk-ant-...'
   ```

Your cookie is used only to call `claude.ai` directly. Nothing is stored or sent
anywhere else.

## Organization ID

The dashboard queries a per-organization usage endpoint, so it needs your org's
UUID. It is **discovered automatically** from `/api/bootstrap` — normally you
never touch this. To pin it explicitly (or if bootstrap is unreachable):

```bash
claude-usage --org <uuid>
# or
export CLAUDE_ORG_ID=<uuid>
```

Find the UUID in claude.ai DevTools → Network → any
`/api/organizations/<uuid>/...` request.

## How it works

`claude.ai` exposes rich JSON endpoints behind your session cookie. The core one
is `/api/organizations/<org>/usage` (limits + credits); the big panel also pulls
`/api/bootstrap` (account/org config), live code sessions, recent chats, and
cowork setup. Each is fetched best-effort, on its own clock.

**Extending it:** the script has a detailed in-file guide (see the banner
comment above `BOOTSTRAP_URL`) on discovering new endpoints from DevTools and
wiring them into a panel section. The rendering pipeline is a set of pure
functions — `render(data, cols, rows, interval, mdef)` dispatches to a
layout-specific renderer based on the terminal size — which makes the layout
easy to reason about and test.

## Development

```bash
pip install -e ".[dev]"         # install with dev deps (pytest)
pytest -q                        # run the test suite
python3 tests/test_usage.py     # or run the same tests without pytest
```

The tests render a fixed fixture across the full size matrix under a frozen
clock and assert the org-discovery precedence, so refactors can be verified not
to change any user-visible output.

## License

[MIT](LICENSE)
