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
  red as a limit fills, so severity reads at a glance — or pick another of the
  four themes (`cool`, `mono`, `contrast`) with `--theme`.
- **Threshold alerts.** Configurable per-limit warning levels, delivered by
  `notify-send` (or stdout), fired once per crossing — from the live dashboard
  or a cron `--once` run.
- **Resilient.** Every secondary endpoint is best-effort — one dead endpoint
  never blanks the panel. Cloudflare challenges and expired sessions are
  reported distinctly.

## Install

Requires Python 3.9+.

Install as a command with [pipx](https://pipx.pypa.io):

```bash
pipx install git+https://github.com/parsaj-dev/claude-usage-dashboard
# or from a local checkout:
pipx install .
```

This puts a `claude-usage` command on your PATH. To also read cookies from the
browser automatically, install the optional extra:

```bash
pipx install "claude-usage[cookies] @ git+https://github.com/parsaj-dev/claude-usage-dashboard"
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
| `--cookie-source`      | `auto`  | Read cookies from `chrome`, `firefox`, or `auto`.   |
| `--org`                | —       | Organization UUID (see below).                      |
| `--once`               | —       | Fetch one snapshot, append to history, exit.        |
| `--print`              | —       | With `--once`, also print the snapshot JSON.        |
| `--no-persist`         | —       | Don't write snapshots to the history file.          |
| `--history-path`       | —       | Override the history file location.                 |
| `--no-alerts`          | —       | Don't warn when a limit crosses its threshold.       |
| `--alert-threshold`    | —       | Warn at this percent for *every* limit.             |
| `--alert-notifier`     | `auto`  | `auto`, `notify-send`, `stdout`, or `none`.         |
| `--alert-state-path`   | —       | Override where once-per-crossing state is stored.   |
| `--theme`              | `claude`| Color theme: `claude`, `cool`, `mono`, `contrast`.  |

## History & headless mode

Each core refresh records a small snapshot — the limit percentages and their
reset times, plus spend — as one line of JSON in an append-only history file at
`$XDG_DATA_HOME/claude-usage/history.jsonl`
(`~/.local/share/claude-usage/history.jsonl`). Only the numbers already on
screen are stored; never raw API payloads. In the **live dashboard**, a refresh
whose numbers match the last stored reading is skipped, so an idle dashboard
doesn't bloat the file.

`--once` is a headless, cron-friendly mode: it fetches a single snapshot,
appends it, and exits silently (add `--print` to echo the JSON). Each `--once`
run writes exactly one record — it does **not** dedup — so you control the
sample cadence via cron. A crontab line that records your usage every 15
minutes:

```cron
*/15 * * * * claude-usage --once
```

Use `history_max` to cap how many records are kept if you sample often.
Persistence is on by default; disable it with `--no-persist` (or `persist =
false` in config). This history file is the foundation for the trends and
alerting features that follow.

### Trend sparklines

In the full panel layout (large windows), each limit gets a small block-glyph
`trend` sparkline under its bar — the last several readings of that limit's
usage percentage, on a fixed 0–100 scale:

```
5-hour    ██████████████▊░░░░░░░░░░░░   60%
          ⭮ 3h
          trend ▂▂▃▅▄▅
```

Trends are seeded from the history file at startup (so prior runs and cron
`--once` samples show up right away) and fill in further as the dashboard runs.
They appear only in the full panel; smaller layouts show bars alone. The trend
buffer works even with `--no-persist` (it just isn't written to disk).

## Themes

`--theme NAME` (or `theme = "…"` in the config) picks the palette. Themes change
**color only** — never layout, glyphs, or widths — so every window size renders
the same shape whichever you pick.

| Theme      | Look                                                              |
| ---------- | ----------------------------------------------------------------- |
| `claude`   | **Default.** Warm: soft tan → Claude orange → rust red.           |
| `cool`     | Pale cyan → blue → violet → hot magenta. Same reading, cool side. |
| `mono`     | Greyscale; severity reads as brightness. Good for screenshots.    |
| `contrast` | High-contrast green → yellow → orange → red on pure white text.   |

```bash
claude-usage --theme contrast
```

The default is unchanged from before themes existed — byte for byte, and there's
a test pinning it to a golden hash so it stays that way. An unrecognized theme
name falls back to the default rather than refusing to start.

## Threshold alerts

The point of watching a limit is to act *before* it runs out, so a limit that
reaches its threshold raises an alert: a desktop notification via `notify-send`
if libnotify is installed, otherwise a line on stdout.

Defaults are 80% for the 5-hour limit, 90% for the 7-day one, and 80% for
anything else. Set your own per limit under `[alert]` in the config file, or
override every limit at once with `--alert-threshold`:

```toml
[alert]
"5-hour" = 70     # "5h" and "7d" work as aliases
"7-day"  = 85
"Opus 7d" = 90
default  = 80     # anything without an entry of its own
```

An alert fires **once per crossing**. It won't repeat while the limit stays
elevated; it re-arms when the limit drops back under its threshold (a reset),
or when you change the threshold. Set `alert_cooldown` to be re-nagged every N
seconds while a limit is still over.

That state lives in `alerts.json` next to the history file, which is what lets
a stateless cron `--once` run stay quiet after the first warning. `--once`
exits **2** when an alert fired (**1** is a fetch/persist failure, **0** is
fine), so a wrapper can act on the warning alone:

```cron
*/15 * * * * claude-usage --once || [ $? -ne 2 ] || echo "claude usage high" | mail -s alert me
```

In the live dashboard, alerts go to `notify-send` only — printing into the
fixed-height redraw would corrupt the frame, and the panel is already showing
the percentage that triggered it. Notification failures are always swallowed:
a missing or wedged notification daemon can never stall or crash a poll.

Turn the whole thing off with `--no-alerts` (or `alerts = false`).

## Configuration

Every setting except `--cookie` (a secret, never read from disk) can be set in a
config file so you don't retype it. Values are resolved with this precedence:

**CLI flag → environment variable → config file → built-in default.**

The config file lives at `$XDG_CONFIG_HOME/claude-usage/config.toml` (i.e.
`~/.config/claude-usage/config.toml`). Keys mirror the flag names with
underscores:

```toml
# ~/.config/claude-usage/config.toml
interval = 60
extras_interval = 300
bootstrap_interval = 0
cookie_source = "firefox"
org = "your-org-uuid"
theme = "claude"         # claude | cool | mono | contrast

# History / persistence
persist = true
history_path = "~/.local/share/claude-usage/history.jsonl"
history_max = 0          # cap on records kept (0 = unlimited)

# Alerting
alerts = true
alert_notifier = "auto"  # auto | notify-send | stdout | none
alert_cooldown = 0       # 0 = fire once per crossing; N = re-nag every N s

[alert]                  # per-limit thresholds, in percent
"5-hour" = 70
"7-day"  = 85
```

Matching environment variables: `CLAUDE_USAGE_INTERVAL`,
`CLAUDE_USAGE_EXTRAS_INTERVAL`, `CLAUDE_USAGE_BOOTSTRAP_INTERVAL`,
`CLAUDE_USAGE_COOKIE_SOURCE`, `CLAUDE_USAGE_PERSIST`,
`CLAUDE_USAGE_HISTORY_PATH`, `CLAUDE_USAGE_ALERTS`,
`CLAUDE_USAGE_ALERT_THRESHOLD`, `CLAUDE_USAGE_ALERT_NOTIFIER`,
`CLAUDE_USAGE_ALERT_STATE_PATH`, `CLAUDE_USAGE_THEME`, and `CLAUDE_ORG_ID`
(for `org`).

The `[alert]` threshold table is config-file-only — there's no sensible flat
spelling for a per-limit map on the command line or in the environment.
`--alert-threshold` / `CLAUDE_USAGE_ALERT_THRESHOLD` replaces the whole table
with one blanket number.

## Authentication

The tool needs your logged-in `claude.ai` session cookie.

1. **Automatic (default).** With `browser-cookie3` installed, the cookie is read
   from your browser — Chrome/Chromium first, then Firefox (`--cookie-source`
   pins one). If your keyring is locked, decryption can fail — unlock it or use
   the manual path.
2. **Manual.** Copy the `sessionKey` cookie from DevTools and pass it:
   ```bash
   claude-usage --cookie 'sessionKey=sk-ant-...'
   ```

> **Firefox note:** the outbound request uses a Chrome User-Agent (Cloudflare
> binds its `cf_clearance` cookie to the UA that earned it). A Firefox-sourced
> `sessionKey` generally works, but a Firefox `cf_clearance` may still trigger a
> Cloudflare challenge. If you hit one, load `claude.ai` in Chrome once, or use
> `--cookie` manually.

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
