#!/usr/bin/env python3
"""
claude-usage — live Claude.ai usage dashboard for a small terminal.

Responsive: full vertical layout shrinks to a compact stack, then flips to a
horizontal strip for very short windows — down to a 40×5 (even 40×1) cell box.
Grow the window (≥46×20) and it expands into a full control panel: every limit,
credits/spend, extra usage, plus account, connectors/MCP and cowork status
(pulled from /api/bootstrap).

Keys:  r / space — refresh now      q / Ctrl-C — quit

Run:
  claude-usage            # live dashboard, refresh every 2 min
  claude-usage -n 30      # refresh every 30s
  claude-usage --cookie 'sessionKey=...'   # manual cookie fallback
  claude-usage --org <uuid>                # pin the organization

Setup:
  pip install --user requests browser-cookie3

The organization is normally discovered automatically from /api/bootstrap, so
no configuration is needed. To pin it explicitly (or if bootstrap is
unreachable), pass --org or set the CLAUDE_ORG_ID environment variable.
"""

import argparse
import json
import math
import os
import select
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install --user requests")

# TOML config parsing: stdlib `tomllib` on 3.11+, the `tomli` backport below.
# The 3.9/3.10 CI leg is what proves this shim (and the dep marker) actually
# work. Absent even the backport, config support degrades to "no config file".
try:
    import tomllib as _toml
except ModuleNotFoundError:  # 3.9 / 3.10
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        _toml = None


def usage_url(org: str) -> str:
    """The core limits/credits endpoint for an organization."""
    return f"https://claude.ai/api/organizations/{org}/usage"

# ── ANSI helpers ──────────────────────────────────────────────────────
R = "\033[0m"; DIM = "\033[2m"; BOLD = "\033[1m"
def rgb(r, g, b): return f"\033[38;2;{r};{g};{b}m"
CLAUDE = rgb(217, 119, 87)
CREAM  = rgb(240, 237, 230)
GREY   = rgb(138, 136, 128)
TRACK  = rgb(62, 60, 56)
STAR   = "✳"
REFRESH = "⟳"
RESET_GLYPH = "↺"
GAUGE = "○◔◑◕●"  # circular fill gauge: 0 / 25 / 50 / 75 / 100%

def gauge_glyph(pct):
    return GAUGE[int(round(max(0.0, min(pct, 100.0)) / 100.0 * 4))]

# Warm "heat" ramp — stays in Claude's palette while encoding severity:
# low usage = soft tan, climbing through Claude orange to a rust red.
HEAT = [
    (0.00, (226, 184, 142)),
    (0.50, (217, 132,  80)),
    (0.80, (210,  98,  58)),
    (1.00, (205,  72,  56)),
]

def heat(pct):
    t = max(0.0, min(pct / 100.0, 1.0))
    for (a, ca), (b, cb) in zip(HEAT, HEAT[1:]):
        if t <= b:
            f = (t - a) / (b - a) if b > a else 0.0
            return tuple(int(ca[i] + (cb[i] - ca[i]) * f) for i in range(3))
    return HEAT[-1][1]

EIGHTHS = " ▏▎▍▌▋▊▉█"  # horizontal: filled eighths from the left (0..8)
VBLOCKS = " ▁▂▃▄▅▆▇█"  # vertical:   filled eighths from the bottom (0..8)

def _center(s, w):
    s = s[:w]
    pad = w - len(s)
    return " " * (pad // 2) + s + " " * (pad - pad // 2)

def sheen(base, i, n):
    # subtle left-dark → right-bright sheen across the filled run
    t = i / max(n - 1, 1)
    f = 0.62 + 0.38 * t
    return rgb(*[min(255, int(c * f)) for c in base])

def progress_bar(pct, width):
    if width < 1:
        return ""
    pct = max(0.0, min(pct, 100.0))
    base = heat(pct)
    filled = pct / 100.0 * width
    full = int(filled)
    eighth = int(round((filled - full) * 8))
    if eighth == 8:
        full += 1; eighth = 0
    fill_cells = full + (1 if eighth else 0)
    parts = []
    for i in range(full):
        parts.append(sheen(base, i, max(fill_cells, 1)) + "█")
    if eighth:
        parts.append(sheen(base, full, max(fill_cells, 1)) + EIGHTHS[eighth])
    empty = width - full - (1 if eighth else 0)
    if empty > 0:
        parts.append(TRACK + "░" * empty)
    return "".join(parts) + R

def fmt_pct(pct, decimals=False):
    if decimals and pct % 1:
        return f"{pct:.1f}%"
    return f"{pct:.0f}%"

def fmt_interval(s):
    if s >= 60:
        m, sec = divmod(s, 60)
        return f"{m}m" if sec == 0 else f"{m}m{sec}s"
    return f"{s}s"

def fmt_ago(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    diff = (datetime.now(timezone.utc) - d).total_seconds()
    if diff < 60:
        return "just now"
    m = int(diff // 60); h = m // 60; days = h // 24
    if days >= 1:
        return f"{days}d ago"
    if h >= 1:
        return f"{h}h ago"
    return f"{m}m ago"

def fmt_reset(iso):
    if not iso:
        return "no reset"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    diff = (d - datetime.now(timezone.utc)).total_seconds()
    local = d.astimezone()
    tstr = local.strftime("%-I:%M%p").lower()
    if diff <= 0:
        return "resetting…"
    mins = int(diff // 60); hrs = mins // 60; days = hrs // 24
    if days >= 1:
        return f"{days}d {hrs % 24}h · {local.strftime('%a')} {tstr}"
    if hrs >= 1:
        return f"{hrs}h {mins % 60}m · {tstr}"
    return f"{mins}m · {tstr}"

# ── Cookie acquisition ────────────────────────────────────────────────
# claude.ai rotates sessionKey, cf_clearance and __cf_bm out from under a
# long-running process. The browser always has the fresh values, so the dash
# re-reads the cookie from the browser on every fetch tick (see the main loop)
# and immediately on a 401/403. That's what keeps it alive for days without a
# manual re-login. read_browser_cookie() is therefore loop-safe: it NEVER exits
# and returns None on any failure so the caller can keep its last-known-good.
#
# Source selection (`--cookie-source`):
#   auto     — try Chrome/Chromium, then Firefox (default)
#   chrome   — Chrome/Chromium only
#   firefox  — Firefox only
# NOTE: the outbound User-Agent is a Chrome UA (see _UA); Cloudflare binds
# cf_clearance to that UA, so a Firefox-sourced cf_clearance can still draw a
# challenge. sessionKey-only access generally works; see the ledger (D-06).
COOKIE_SOURCES = ("auto", "chrome", "firefox")


def _cookie_loaders(source):
    """browser_cookie3 loader callables for a source, or None if unavailable."""
    try:
        import browser_cookie3 as bc
    except ImportError:
        return None
    chrome = (bc.chrome, bc.chromium)
    firefox = (bc.firefox,)
    return {"auto": chrome + firefox, "chrome": chrome,
            "firefox": firefox}.get(source, chrome + firefox)


def read_browser_cookie(source="auto"):
    """Current claude.ai cookie header from the browser, or None. No exits."""
    loaders = _cookie_loaders(source)
    if not loaders:
        return None
    for loader in loaders:
        try:
            cj = loader(domain_name="claude.ai")
            cookies = {c.name: c.value for c in cj}
            if "sessionKey" in cookies:
                return "; ".join(f"{k}={v}" for k, v in cookies.items())
        except Exception:
            continue
    return None

def get_cookie(manual=None, source="auto"):
    """Startup cookie resolve. Exits with a helpful message if nothing is found
    — but only ever called once, before the loop. Inside the loop use
    read_browser_cookie() so a transient keyring/DB lock can't kill the dash."""
    if manual:
        return manual
    ck = read_browser_cookie(source)
    if ck:
        return ck
    try:
        import browser_cookie3  # noqa: F401
    except ImportError:
        sys.exit(
            "Could not auto-read cookies. Either:\n"
            "  pip install --user browser-cookie3\n"
            "or pass manually:  claude-usage --cookie 'sessionKey=...'"
        )
    where = {"chrome": "Chrome/Chromium", "firefox": "Firefox"}.get(
        source, "Chrome/Chromium or Firefox")
    sys.exit(
        f"No Claude session cookie found in {where}.\n"
        "Make sure you're logged in, or pass --cookie manually.\n"
        "(If your keyring is locked, that can block cookie decryption.)"
    )


# ── Config file + setting precedence ──────────────────────────────────
# Config lives at $XDG_CONFIG_HOME/claude-usage/config.toml (default
# ~/.config/...). Keys mirror the CLI flags EXCEPT --cookie: a session cookie
# is a secret and is never read from, or written to, the config file (D-05).
# Precedence for every setting: CLI flag > env var > config file > default.
def config_path():
    """Path to the TOML config file (XDG-aware). Read at call time, not import."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "claude-usage", "config.toml")


def load_config(path=None):
    """Parse the TOML config into a dict. Best-effort: a missing file, a parse
    error, or no TOML parser available all yield {} rather than raising —
    config is a convenience, never a hard dependency."""
    if _toml is None:
        return {}
    path = path or config_path()
    try:
        with open(path, "rb") as fh:
            data = _toml.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _coerce_int(value):
    """Best-effort int, or None for missing/garbage (so precedence falls
    through instead of crashing the loop on a string like '30')."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value):
    """Best-effort bool for CLI/env/config. None for missing/unrecognized so
    precedence falls through. Accepts real bools and the usual string spellings
    (config TOML gives a real bool; env vars arrive as strings)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def resolve_setting(cli, env, cfg, default):
    """First non-None of (cli, env, cfg), else default. `is not None` (not
    truthiness) so a meaningful 0 — e.g. --bootstrap-interval 0 — wins over a
    config value instead of being treated as 'unset'."""
    for v in (cli, env, cfg):
        if v is not None:
            return v
    return default


def resolve_runtime_config(cli, env, cfg):
    """Pure resolver for every runtime setting. `cli`/`env`/`cfg` are plain
    dicts; returns the fully-resolved settings dict that main() binds locals
    from. Kept pure and side-effect-free so the whole precedence table is
    unit-tested offline without invoking main() or touching argparse/os.environ.

    Ints are coerced at the env/config boundary (env vars and TOML values may
    arrive as strings); cookie-source is validated against COOKIE_SOURCES with
    an 'auto' fallback so a stray config/env value can't wedge the reader."""
    def i(key, default):
        return resolve_setting(_coerce_int(cli.get(key)),
                               _coerce_int(env.get(key)),
                               _coerce_int(cfg.get(key)), default)

    interval = i("interval", 120)
    extras_interval = i("extras_interval", 300)
    bootstrap_interval = i("bootstrap_interval", 0)
    org = resolve_setting(cli.get("org"), env.get("org"), cfg.get("org"), None)
    source = resolve_setting(cli.get("cookie_source"), env.get("cookie_source"),
                             cfg.get("cookie_source"), "auto")
    if source not in COOKIE_SOURCES:
        source = "auto"
    persist = resolve_setting(_coerce_bool(cli.get("persist")),
                              _coerce_bool(env.get("persist")),
                              _coerce_bool(cfg.get("persist")), True)
    history_path = resolve_setting(cli.get("history_path"), env.get("history_path"),
                                   cfg.get("history_path"), None) or default_history_path()
    history_path = os.path.expanduser(history_path)  # honor a ~ in config/env
    # Alerting. Thresholds come from the config's `[alert]` table only (there's
    # no sensible flat CLI/env spelling for a per-limit map); a single
    # --alert-threshold / env value overrides every label at once by moving the
    # "default" and dropping the per-label entries.
    alerts_on = resolve_setting(_coerce_bool(cli.get("alerts")),
                                _coerce_bool(env.get("alerts")),
                                _coerce_bool(cfg.get("alerts")), True)
    thresholds = normalize_thresholds(cfg.get("alert"))
    blanket = resolve_setting(_coerce_int(cli.get("alert_threshold")),
                              _coerce_int(env.get("alert_threshold")), None, None)
    if blanket is not None:
        thresholds = {"default": blanket}
    notifier = resolve_setting(cli.get("alert_notifier"), env.get("alert_notifier"),
                               cfg.get("alert_notifier"), "auto")
    if notifier not in ALERT_NOTIFIERS:
        notifier = "auto"
    alert_state_path = resolve_setting(
        cli.get("alert_state_path"), env.get("alert_state_path"),
        cfg.get("alert_state_path"), None) or default_alert_state_path(history_path)
    return {
        "interval": interval,
        # Never poll extras faster than the core clock — a big -n implies
        # laziness. (Preserves the pre-config behavior.)
        "extras_interval": max(extras_interval, interval),
        "bootstrap_interval": bootstrap_interval,
        "org": org,
        "cookie_source": source,
        "persist": persist,
        "history_path": history_path,
        # retention cap: max snapshots kept (0 = unlimited).
        "history_max": max(0, i("history_max", 0)),
        "alerts": alerts_on,
        "alert_thresholds": thresholds,
        "alert_notifier": notifier,
        # 0 = fire once per crossing (never re-nag until the limit resets).
        "alert_cooldown": max(0, i("alert_cooldown", 0)),
        "alert_state_path": os.path.expanduser(alert_state_path),
    }

# ── Endpoints ─────────────────────────────────────────────────────────
# ╔══════════════════════════════════════════════════════════════════════╗
# ║ HOW TO UPDATE THIS SCRIPT WHEN THE RESPONSES CHANGE (read me!)         ║
# ╠══════════════════════════════════════════════════════════════════════╣
# ║ 1. Open claude.ai in Chrome (logged in), open DevTools → Network tab,  ║
# ║    filter by "Fetch/XHR", and click around the UI (Settings, Cowork,   ║
# ║    Usage popover, the Code sessions list, etc.). Every row is a live   ║
# ║    endpoint — that's the goldmine. Right-click → Copy → Copy as cURL   ║
# ║    to grab the exact URL + headers.                                    ║
# ║ 2. The single richest one is /api/bootstrap (and the newer            ║
# ║    /edge-api/bootstrap/<ORG>/app_start): it dumps account + org        ║
# ║    settings, feature flags, connectors and cowork config in one shot.  ║
# ║    Dump it and grep the flattened key paths for what you want, e.g.:   ║
# ║      grep -iE 'mcp|cowork|host|connector|tier|billing'                 ║
# ║ 3. To inspect any endpoint's shape, reuse fetch_json() below with the  ║
# ║    URL. Auth is just the browser session cookie (get_cookie()).        ║
# ║    Gotcha: /v1/* endpoints (like code/sessions) are API-style and      ║
# ║    REQUIRE an `anthropic-version` header or they 400.                  ║
# ║ 4. Add/point a fetch_* helper at the new URL, stash it in LIVE (for    ║
# ║    per-refresh data) or BOOT (for once-at-startup config), then render ║
# ║    a section in render_panel(). Keep every fetch best-effort (return   ║
# ║    None on failure) so one dead endpoint never blanks the panel.       ║
# ║ Known-good endpoints (all GET, cookie auth) as of 2026-07:             ║
# ║   /api/organizations/<ORG>/usage            — limits/credits (core)    ║
# ║      NB: 403 "Invalid authorization for organization" ≠ bad cookie —   ║
# ║      it means <ORG> is the wrong org (e.g. an api-only Console org).    ║
# ║      resolve_org() ranks by lastActiveOrg cookie + chat/claude_* caps.  ║
# ║   /api/bootstrap                            — account/org/connectors   ║
# ║   /v1/code/sessions?statuses=active&statuses=paused  — live sessions   ║
# ║      (needs anthropic-version header)                                  ║
# ║   /api/organizations/<ORG>/chat_conversations_v2     — recent chats    ║
# ║   /api/organizations/<ORG>/activation/cowork/tasks   — cowork setup    ║
# ║   /api/account_profile                      — work fn / avatar         ║
# ╚══════════════════════════════════════════════════════════════════════╝
BOOTSTRAP_URL = "https://claude.ai/api/bootstrap"
CODE_SESSIONS_URL = ("https://claude.ai/v1/code/sessions"
                     "?statuses=active&statuses=paused&limit=50")
def CONVERSATIONS_URL(org):
    return (f"https://claude.ai/api/organizations/{org}"
            "/chat_conversations_v2?limit=8&offset=0&consistency=eventual")
def COWORK_TASKS_URL(org):
    return f"https://claude.ai/api/organizations/{org}/activation/cowork/tasks"

# Cloudflare binds the cf_clearance cookie to the exact User-Agent that earned
# it. A generic "Mozilla/5.0" makes the clearance cookie invalid and claude.ai
# answers the bot challenge page with 403 — which looks exactly like an expired
# session but isn't. So we derive the UA's Chrome major from the *installed*
# Chrome at runtime, rather than hardcoding a version that drifts on every
# Chrome auto-update. Falls back to a recent literal if detection fails.
def _detect_chrome_major():
    import re, subprocess
    for exe in ("google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser"):
        try:
            out = subprocess.run([exe, "--version"], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r"\b(\d+)\.\d+\.\d+", out)
            if m:
                return m.group(1)
        except Exception:
            continue
    return "151"  # keep in step with a recent Chrome as a last resort

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       f"Chrome/{_detect_chrome_major()}.0.0.0 Safari/537.36")
_BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": _UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://claude.ai/",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}

def fetch_json(url, cookie, extra_headers=None):
    """GET a claude.ai JSON endpoint with the session cookie. Best-effort:
    returns None on any failure. Pass extra_headers={'anthropic-version': ...}
    for /v1/* API-style endpoints. This is the workhorse for adding new panels."""
    try:
        h = dict(_BASE_HEADERS, Cookie=cookie)
        if extra_headers:
            h.update(extra_headers)
        r = requests.get(url, headers=h, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def fetch(cookie, org):
    """The core per-refresh call: limits and credits. Raises on HTTP error so
    main() can surface auth/Cloudflare problems distinctly (see the loop)."""
    r = requests.get(usage_url(org), headers=dict(_BASE_HEADERS, Cookie=cookie),
                     timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_bootstrap(cookie):
    """Account/org config: plan tier, connectors, cowork, MCP tools. Static-ish,
    fetched once at startup. Best-effort — None on failure so the panel omits it."""
    return fetch_json(BOOTSTRAP_URL, cookie)

def _cookie_value(cookie, name):
    """Pluck a single cookie value out of the joined 'k=v; k=v' header."""
    for part in (cookie or "").split("; "):
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return None

def discover_org_id(boot, cookie=None):
    """Pick the organization UUID to query from a /api/bootstrap payload.

    An account often has MORE than one membership — e.g. a chat/Max
    subscription org AND an `api`-only Console org. The /usage endpoint only
    authorizes the subscription org; querying the api-only one returns
    403 'Invalid authorization for organization', which used to masquerade as
    an expired cookie. So don't just grab the first membership — rank them:
      1. the org matching the `lastActiveOrg` cookie (what the browser uses),
      2. an org whose capabilities include chat/claude_* over an api-only org,
      3. otherwise the first membership.
    Accepts either `uuid` or `id` since the field name has drifted historically.
    Returns None if bootstrap is absent or carries no membership."""
    account = (boot or {}).get("account") or {}
    orgs = []
    for membership in account.get("memberships") or []:
        org = membership.get("organization") or {}
        org_id = org.get("uuid") or org.get("id")
        if org_id:
            orgs.append((org_id, org.get("capabilities") or []))
    if not orgs:
        return None
    active = _cookie_value(cookie, "lastActiveOrg")
    for org_id, _ in orgs:
        if org_id == active:
            return org_id
    for org_id, caps in orgs:
        if any(c == "chat" or c.startswith("claude_") for c in caps):
            return org_id
    return orgs[0][0]

def resolve_org(explicit, boot, cookie=None):
    """Decide which organization to query, most-trusted source first:
    explicit --org / $CLAUDE_ORG_ID, then discovery from bootstrap. Exits with
    an actionable message if none is available — the usage URL can't be built
    without it. Keeping the flag/env ahead of discovery means the tool still
    works when bootstrap is down, as long as the org is supplied."""
    if explicit:
        return explicit
    org = discover_org_id(boot, cookie)
    if org:
        return org
    sys.exit(
        "Could not determine your organization ID.\n"
        "Bootstrap was unreachable or returned no membership. Pass it manually:\n"
        "  claude-usage --org <uuid>\n"
        "or set CLAUDE_ORG_ID. Find it in claude.ai DevTools → Network → any\n"
        "/api/organizations/<uuid>/... request.")

def fetch_live(cookie, org):
    """Dynamic data refreshed each cycle: live sessions, recent chats, cowork
    setup progress. Returns a dict of best-effort results (any value may be None)."""
    return {
        "sessions": fetch_json(CODE_SESSIONS_URL, cookie,
                               {"anthropic-version": "2023-06-01"}),
        "conversations": fetch_json(CONVERSATIONS_URL(org), cookie),
        "cowork_tasks": fetch_json(COWORK_TASKS_URL(org), cookie),
        "profile": fetch_json("https://claude.ai/api/account_profile", cookie),
    }

# ── Metric extraction ─────────────────────────────────────────────────
# The API moved to a normalized `limits` array (kind = session / weekly_all /
# weekly_scoped). We drive everything off that, falling back to the legacy
# top-level five_hour / seven_day objects if `limits` is ever absent.

# kind → (full label, compact label)
KIND_LABELS = {
    "session":     ("5-hour", "5h"),
    "weekly_all":  ("7-day",  "7d"),
}

def limit_label(lim):
    kind = lim.get("kind") or ""
    if kind in KIND_LABELS:
        return KIND_LABELS[kind]
    scope = lim.get("scope") or {}
    model = (scope.get("model") or {}).get("display_name")
    if model:
        return (f"{model} 7d", model[:3])
    surface = scope.get("surface")
    if surface:
        return (str(surface), str(surface)[:3])
    pretty = kind.replace("_", " ").title() or "Limit"
    return (pretty, (kind[:3] or "?"))

def all_limits(data):
    """Every limit as a dict: full, compact, pct, iso, active, severity, group."""
    out = []
    for lim in data.get("limits") or []:
        full, comp = limit_label(lim)
        out.append({
            "full": full, "compact": comp,
            "pct": float(lim.get("percent") or 0.0),
            "iso": lim.get("resets_at"),
            "active": bool(lim.get("is_active")),
            "severity": lim.get("severity") or "normal",
            "group": lim.get("group") or "",
            "kind": lim.get("kind") or "",
        })
    if out:
        return out
    # legacy fallback
    for full, comp, key in (("5-hour", "5h", "five_hour"),
                            ("7-day", "7d", "seven_day")):
        m = data.get(key) or {}
        out.append({"full": full, "compact": comp,
                    "pct": float(m.get("utilization") or 0.0),
                    "iso": m.get("resets_at"), "active": True,
                    "severity": "normal", "group": "", "kind": key})
    return out

# Model/surface-specific buckets that show up as top-level keys when active.
# Friendly labels where the mapping is known; unknown codenames are prettified
# from the raw key so they're at least visible and honestly named.
EXTRA_LABELS = {
    "seven_day_opus":       "Opus 7d",
    "seven_day_sonnet":     "Sonnet 7d",
    "seven_day_cowork":     "Cowork 7d",
    "seven_day_omelette":   "Design 7d",
    "seven_day_oauth_apps": "OAuth apps 7d",
    "omelette_promotional": "Design promo",
}
# Keys handled explicitly elsewhere (limits array / spend / meta) — don't sweep.
_SWEEP_SKIP = {"five_hour", "seven_day", "limits", "spend", "extra_usage",
               "member_dashboard_available"}

def extra_metrics(data, seen_labels=()):
    """Every top-level usage-shaped bucket that's actually active (utilization
    is non-null), as dicts. Dead/null buckets are omitted so the panel only
    grows sections for things you're really consuming."""
    out = []
    seen = set(seen_labels)
    for key, val in (data or {}).items():
        if key in _SWEEP_SKIP or not isinstance(val, dict):
            continue
        pct = val.get("utilization")
        if pct is None:
            continue
        label = EXTRA_LABELS.get(key) or key.replace("_", " ").title()
        if label in seen:
            continue
        seen.add(label)
        out.append({"full": label, "pct": float(pct), "iso": val.get("resets_at"),
                    "used": val.get("used_dollars"),
                    "limit": val.get("limit_dollars")})
    return out

def primary_metrics(data):
    """The key usage limits for the small views: session + weekly, plus any
    active scoped limit that's actually being consumed. Returned as
    (full, compact, pct, iso) tuples so the compact renderers stay simple."""
    keep = []
    for lim in all_limits(data):
        if lim["kind"] in ("session", "weekly_all", "five_hour", "seven_day"):
            keep.append(lim)
        elif lim["active"] or lim["pct"] > 0:
            keep.append(lim)  # a scoped limit that's live/consumed
    return [(l["full"], l["compact"], l["pct"], l["iso"]) for l in keep]

# ── Shared bits ───────────────────────────────────────────────────────
def brand_left(text):
    # `text` starts with STAR; colour the star Claude-orange, rest cream.
    return f"{BOLD}{CLAUDE}{STAR}{CREAM}{text[1:]}{R}"

def join_lr(left, right, cols):
    gap = max(1, cols - len(left) - len(right))
    return brand_left(left) + " " * gap + f"{DIM}{GREY}{right}{R}"

# ── Vertical layout pieces ────────────────────────────────────────────
def header_full(cols):
    if cols >= 15:
        return f"{BOLD}{CLAUDE}{STAR} {CREAM}Claude {DIM}{GREY}Usage{R}"
    if cols >= 9:
        return f"{BOLD}{CLAUDE}{STAR} {CREAM}Claude{R}"
    return f"{BOLD}{CLAUDE}{STAR}{R}"

def header_compact(cols, now):
    t = now.strftime("%-I:%M%p").lower()
    left, right = f"{STAR} Claude", f"{REFRESH} {t}"
    if len(left) + 1 + len(right) > cols:
        left = STAR
    if len(left) + 1 + len(right) > cols:
        right = t
    if len(left) + 1 + len(right) > cols:
        return brand_left(left)
    return join_lr(left, right, cols)

def footer_line(now, interval, cols):
    t = now.strftime("%-I:%M:%S%p").lower()
    iv = fmt_interval(interval)
    for s in (f"{REFRESH} {t} · every {iv} · [r]efresh [q]uit",
              f"{REFRESH} {t} · [r]efresh [q]uit",
              f"{REFRESH} {t} · [r] [q]",
              "[r]efresh [q]uit",
              "[r] [q]",
              t):
        if len(s) <= cols:
            return f"{DIM}{GREY}{s}{R}"
    return f"{DIM}{GREY}{t[:cols]}{R}"

def compact_line(label, pct, cols):
    lw, pw = 3, 4
    pcell = f"{rgb(*heat(pct))}{BOLD}{fmt_pct(pct):>{pw}}{R}"
    lab = f"{CREAM}{label:<{lw}}{R}"
    bar_w = cols - lw - 1 - 1 - pw
    if bar_w < 3:
        return f"{lab} {pcell}"
    return f"{lab} {progress_bar(pct, bar_w)} {pcell}"

def bar_line_full(label, pct, cols):
    lw, pw = 7, 6
    pcell = f"{rgb(*heat(pct))}{BOLD}{fmt_pct(pct, decimals=True):>{pw}}{R}"
    lab = f"{CREAM}{label:<{lw}}{R}"
    bar_w = cols - lw - 1 - 1 - pw
    if bar_w < 3:
        return f"{lab} {pcell}"
    return f"{lab} {progress_bar(pct, bar_w)} {pcell}"

def reset_line(iso, cols):
    s = f"        {RESET_GLYPH} {fmt_reset(iso)}"
    return f"{DIM}{GREY}{s[:cols]}{R}"

# ── Horizontal layout (short windows): circular gauge, not bars ───────
def header_h(cols, now):
    t = now.strftime("%-I:%M%p").lower()
    left = f"{STAR} Claude"
    for right in (f"{REFRESH} {t} · [r]efresh [q]uit",
                  f"{REFRESH} {t} · [r] [q]",
                  f"{REFRESH} {t}", "[r][q]"):
        if len(left) + 1 + len(right) <= cols:
            return join_lr(left, right, cols)
    if len(left) <= cols:
        return brand_left(left)
    return f"{BOLD}{CLAUDE}{STAR}{R}"

def h_cell(label, pct, w):
    pcol = rgb(*heat(pct))
    pstr = fmt_pct(pct)
    right = f"{pcol}{gauge_glyph(pct)}{R} {pcol}{BOLD}{pstr:>4}{R}"  # gauge + % = 6 wide
    if w < 8:
        return f"{pcol}{gauge_glyph(pct)} {BOLD}{pstr}{R}"
    label = label[:max(1, w - 6 - 1)]
    gap = max(1, w - len(label) - 6)
    return f"{CREAM}{label}{R}" + " " * gap + right

def h_strip(metrics, cell_w, gutter):
    # uniform label style across the row: full labels only if every one fits
    use_full = all(len(fl) + 1 + 6 <= cell_w for fl, _, _ in metrics)
    cells = [h_cell(fl if use_full else cl, pc, cell_w) for fl, cl, pc in metrics]
    return (" " * gutter).join(cells)

def render_horizontal(data, cols, rows, interval, mdef):
    # The layout renderers (render_horizontal / render_vbars / render_stacked)
    # share one signature so render() can dispatch them interchangeably; each
    # takes (data, cols, rows, interval, mdef) even where it needs only a subset.
    now = datetime.now()
    metrics = [(fl, cl, pct) for fl, cl, pct, _iso in mdef]
    n = len(metrics)
    gutter, cell_min = 2, 10
    has_header = rows >= 2
    avail = max(1, rows - (1 if has_header else 0))

    divs = [d for d in range(n, 0, -1) if n % d == 0]  # balanced grids only
    chosen = None
    for opt in divs:
        cw = (cols - (opt - 1) * gutter) // opt
        if cw >= cell_min and math.ceil(n / opt) <= avail:
            chosen = (opt, cw); break
    if not chosen:
        for opt in divs:
            if math.ceil(n / opt) <= avail:
                chosen = (opt, max(7, (cols - (opt - 1) * gutter) // opt)); break
    if not chosen:
        chosen = (n, max(7, (cols - (n - 1) * gutter) // n))
    ncols, cell_w = chosen

    if not has_header:  # rows == 1: keep branding inline, all metrics in a strip
        pf = f"{STAR} Claude " if cols >= 32 else f"{STAR} "
        cw = max(7, (cols - len(pf) - (n - 1) * gutter) // n)
        return [brand_left(pf.rstrip()) + " " + h_strip(metrics, cw, gutter)]

    nrows = math.ceil(n / ncols)
    grid = [h_strip(metrics[ri * ncols:(ri + 1) * ncols], cell_w, gutter)
            for ri in range(nrows)]

    top = max(0, (rows - 1 - nrows) // 2)
    lines = [header_h(cols, now)] + [""] * top + grid
    while len(lines) < rows:
        lines.append("")
    return lines[:rows]

# ── Narrow + tall: vertical bar chart (columns rise the window height) ─
def render_vbars(data, cols, rows, interval, mdef):
    now = datetime.now()
    n = len(mdef)
    fw = max(1, cols // n)              # field width per column
    bw = max(1, fw - 1)                 # bar thickness (1-col gutter)
    lpad = (fw - bw) // 2
    rpad = fw - bw - lpad
    pre = " " * max(0, (cols - fw * n) // 2)
    H = max(1, rows - 4)               # header + labels + pcts + footer = 4

    metrics = [(cl, pct) for _, cl, pct, _iso in mdef]
    geom = []
    for _, p in metrics:
        filled = max(0.0, min(p, 100.0)) / 100.0 * H
        full = int(filled)
        eighth = int(round((filled - full) * 8))
        if eighth == 8:
            full += 1; eighth = 0
        geom.append((heat(p), full, eighth))

    bar_rows = []
    for r in range(H):
        b = H - 1 - r                   # distance from the bottom
        cells = []
        for base, full, eighth in geom:
            cap = max(full + (1 if eighth else 0), 1)
            if b < full:
                ch = sheen(base, b, cap) + "█" * bw
            elif b == full and eighth:
                ch = sheen(base, b, cap) + VBLOCKS[eighth] * bw
            else:
                ch = TRACK + "░" * bw
            cells.append(" " * lpad + ch + R + " " * rpad)
        bar_rows.append(pre + "".join(cells))

    labels = pre + "".join(f"{CREAM}{_center(cl, fw)}{R}" for cl, _ in metrics)
    pcts = pre + "".join(
        f"{rgb(*heat(p))}{BOLD}{_center(fmt_pct(p) if fw >= 4 else f'{p:.0f}', fw)}{R}"
        for _, p in metrics)

    lines = [header_full(cols), labels] + bar_rows + [pcts, footer_line(now, interval, cols)]
    return lines[:rows]

# ── Ultra-narrow fallback: stacked label over a horizontal bar ────────
def render_stacked(data, cols, rows, interval, mdef):
    now = datetime.now()
    pw = 4
    core = [header_full(cols)]
    for _, cl, pct, _iso in mdef:
        core.append(f"{CREAM}{cl}{R}")
        bar_w = cols - 1 - pw
        pcell = f"{rgb(*heat(pct))}{BOLD}{fmt_pct(pct):>{pw}}{R}"
        core.append(f"{progress_bar(pct, bar_w)} {pcell}" if bar_w >= 2
                    else f"{rgb(*heat(pct))}{BOLD}{fmt_pct(pct)}{R}")
    if rows > len(core):
        return core + [""] * (rows - len(core) - 1) + [footer_line(now, interval, cols)]
    return core[:rows]

# ── Big control panel (large windows) ─────────────────────────────────
SEV_COL = {
    "normal": rgb(138, 136, 128),
    "warning": rgb(217, 132, 80),
    "critical": rgb(205, 72, 56),
}
ON_COL = rgb(217, 132, 80)

# Account/org config from /api/bootstrap, fetched once at startup. The panel
# reads it for the ACCOUNT / CONNECTORS / COWORK sections; None → sections skip.
BOOT = None
# Dynamic data refreshed each cycle (see fetch_live): sessions / chats / cowork.
LIVE = {}

def fmt_money(m):
    if not m:
        return "—"
    amt = m.get("amount_minor")
    if amt is None:
        return "—"
    exp = m.get("exponent", 2)
    cur = m.get("currency") or "USD"
    val = amt / (10 ** exp)
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur, cur + " ")
    return f"{sym}{val:.{exp}f}"

def _section(title):
    return f"{BOLD}{CLAUDE}{title}{R}"

def _kv(label, value, cols, vcol=CREAM):
    lab = f"{DIM}{GREY}{label}{R}"
    val = f"{vcol}{value}{R}"
    gap = max(1, cols - len(label) - len(str(value)))
    return lab + " " * gap + val

def _kv_raw(label, value_str, disp_len, cols):
    """Like _kv but `value_str` is pre-colored; `disp_len` is its visible width."""
    gap = max(1, cols - len(label) - disp_len)
    return f"{DIM}{GREY}{label}{R}" + " " * gap + value_str

def _kv_onoff(label, v, cols):
    txt = "on" if v else "off"
    col = ON_COL if v else GREY
    return _kv_raw(label, f"{col}{txt}{R}", len(txt), cols)

def account_section(cols):
    a = (BOOT or {}).get("account") or {}
    if not a:
        return []
    out = [_section("ACCOUNT")]
    out.append(_kv("Name", a.get("full_name") or a.get("display_name") or "—", cols))
    if a.get("email_address"):
        out.append(_kv("Email", a["email_address"], cols))
    org = ((a.get("memberships") or [{}])[0]).get("organization") or {}
    if org.get("name"):
        out.append(_kv("Org", org["name"], cols))
    if org.get("rate_limit_tier"):
        out.append(_kv("Rate tier", org["rate_limit_tier"], cols))
    if org.get("billing_type"):
        out.append(_kv("Billing", org["billing_type"], cols))
    if org.get("capabilities"):
        out.append(_kv("Capabilities", ", ".join(org["capabilities"]), cols))
    wf = (LIVE.get("profile") or {}).get("work_function")
    if wf:
        out.append(_kv("Work function", wf, cols))
    out.append(_kv("Verified", "yes" if a.get("is_verified") else "no", cols))
    return out

# (label, settings-key) — surfaced as on/off when present in account.settings
CONNECTOR_TOGGLES = [
    ("Web search",   "enabled_web_search"),
    ("Google Drive", "enabled_gdrive"),
    ("Geolocation",  "enabled_geolocation"),
    ("CLI ops",      "enabled_cli_ops"),
    ("Full thinking","enabled_full_thinking"),
    ("Connector suggestions", "enabled_connector_suggestions"),
]

def connectors_section(cols):
    s = (((BOOT or {}).get("account") or {}).get("settings")) or {}
    if not s:
        return []
    out = [_section("CONNECTORS")]
    for lab, key in CONNECTOR_TOGGLES:
        v = s.get(key)
        if v is None:
            continue
        out.append(_kv_onoff(lab, v, cols))
    mt = s.get("enabled_mcp_tools") or {}
    on = sum(1 for v in mt.values() if v)
    servers = {k.split(":")[0] for k in mt}
    if mt:
        out.append(_kv("MCP tools", f"{on} on · {len(servers)} server(s)", cols))
    return out

def cowork_section(cols):
    s = (((BOOT or {}).get("account") or {}).get("settings")) or {}
    onboarded = s.get("cowork_onboarding_completed_at")
    trial_end = s.get("internal_cowork_trial_ends_at")
    tasks = (LIVE.get("cowork_tasks") or {}).get("tasks") or []
    if not (onboarded or trial_end or tasks
            or s.get("cowork_sms_enabled") is not None):
        return []
    out = [_section("COWORK")]
    out.append(_kv("Onboarded", "yes" if onboarded else "no", cols))
    if trial_end:
        out.append(_kv("Trial ends", fmt_reset(trial_end), cols))
    if tasks:
        done = sum(1 for t in tasks if t.get("completed"))
        out.append(_kv("Setup tasks", f"{done}/{len(tasks)} done", cols))
    if s.get("cowork_sms_enabled") is not None:
        out.append(_kv_onoff("SMS", s["cowork_sms_enabled"], cols))
    return out

# status_bucket / worker_status → dot colour signalling liveness at a glance
def _session_dot(sess):
    conn = sess.get("connection_status")
    worker = sess.get("worker_status")
    if conn != "connected":
        return GREY               # offline/disconnected
    if worker and worker != "idle":
        return ON_COL             # actively working
    return CREAM                  # connected, idle

def sessions_section(cols):
    data = LIVE.get("sessions")
    if not data:
        return []
    sessions = data.get("data") if isinstance(data, dict) else data
    if not sessions:
        return []
    # live/connected first, then most-recently-active
    sessions = sorted(
        sessions,
        key=lambda s: (s.get("connection_status") == "connected",
                       s.get("last_event_at") or ""),
        reverse=True)
    out = [_section(f"SESSIONS ({len(sessions)})")]
    for s in sessions[:6]:
        dot = _session_dot(s)
        title = (s.get("title") or "untitled")[:cols - 2]
        out.append(f"{dot}●{R} {CREAM}{title}{R}")
        bits = []
        bucket = (s.get("status_bucket") or s.get("status") or "").replace("_", " ")
        if bucket:
            bits.append(bucket)
        conn = s.get("connection_status")
        bits.append("connected" if conn == "connected" else "offline")
        if s.get("worker_status"):
            bits.append(s["worker_status"])
        origin = (s.get("config") or {}).get("origin") or s.get("environment_kind")
        if origin:
            bits.append(origin)
        ago = fmt_ago(s.get("last_event_at"))
        if ago:
            bits.append(ago)
        out.append(f"  {DIM}{GREY}{' · '.join(bits)[:cols - 2]}{R}")
    if len(sessions) > 6:
        out.append(f"  {DIM}{GREY}+{len(sessions) - 6} more{R}")
    return out

def recent_section(cols):
    data = LIVE.get("conversations")
    convos = (data or {}).get("data") if isinstance(data, dict) else None
    if not convos:
        return []
    out = [_section("RECENT CHATS")]
    for c in convos[:5]:
        name = c.get("name") or "(untitled)"
        model = (c.get("model") or "").replace("claude-", "")
        ago = fmt_ago(c.get("updated_at"))
        meta = " · ".join(x for x in (model, ago) if x)
        star = f"{CLAUDE}★ {R}" if c.get("is_starred") else ""
        avail = max(6, cols - len(meta) - 1 - (2 if star else 0))
        nm = name[:avail]
        gap = max(1, cols - (2 if star else 0) - len(nm) - len(meta))
        out.append(f"{star}{CREAM}{nm}{R}" + " " * gap + f"{DIM}{GREY}{meta}{R}")
    return out

# ── Trends / sparklines (read-only over history) ──────────────────────
# Eight block glyphs give a compact per-metric trend. The scale is FIXED at
# 0–100 (these are utilization percentages), not min/max-normalized, so the
# sparkline is honestly comparable frame to frame and a flat-but-high metric
# doesn't look like a flat-but-low one.
_SPARK_GLYPHS = "▁▂▃▄▅▆▇█"


def sparkline(values, width=None):
    """A block-glyph sparkline for a series of percentages (0–100, fixed scale).
    Non-numeric entries are dropped; an empty series yields "". When `width` is
    given, only the most recent `width` samples are shown."""
    vals = [v for v in (values or []) if isinstance(v, (int, float))
            and not isinstance(v, bool)]
    if not vals:
        return ""
    if width and width > 0:
        vals = vals[-width:]
    top = len(_SPARK_GLYPHS) - 1
    return "".join(
        _SPARK_GLYPHS[round(min(100.0, max(0.0, float(v))) / 100.0 * top)]
        for v in vals)


def history_index(history):
    """One pass over the history records → {label: [pct, ...]} in chronological
    order. Non-numeric pcts are skipped. Built once per render frame so the panel
    doesn't rescan the whole buffer for every metric."""
    out = {}
    for rec in history or []:
        for lim in (rec.get("limits") or []):
            label, pct = lim.get("label"), lim.get("pct")
            if label and isinstance(pct, (int, float)) and not isinstance(pct, bool):
                out.setdefault(label, []).append(float(pct))
    return out


def trend_series(history, label, n=None):
    """The percentage series for one limit label across the history records, in
    chronological order. `n` keeps only the last n samples. (For the render path,
    prefer history_index() once per frame over calling this per metric.)"""
    out = history_index(history).get(label, [])
    if n and n > 0:
        return out[-n:]
    return out


def render_panel(data, cols, rows, interval, history=None):
    now = datetime.now()
    lines = []
    t = now.strftime("%-I:%M%p").lower()
    lines.append(join_lr(f"{STAR} Claude Usage", f"{REFRESH} {t}", cols))
    lines.append("")

    # ── Limits ────────────────────────────────────────────────────────
    # Build the label → pct-series map once per frame (not once per metric).
    trend_map = history_index(history)
    # The trend line's visible prefix is exactly 16 cols ("          trend ");
    # cap the sparkline so the row never exceeds the panel width and wraps.
    spark_w = max(1, cols - 16)

    def bar_row(label, pct, iso, sev="normal"):
        scol = SEV_COL.get(sev, GREY)
        lab = label[:9]
        pcell = f"{rgb(*heat(pct))}{BOLD}{fmt_pct(pct, decimals=True):>6}{R}"
        bar_w = max(6, cols - 9 - 1 - 1 - 6)
        lines.append(f"{CREAM}{lab:<9}{R} {progress_bar(pct, bar_w)} {pcell}")
        if iso:
            r = f"{RESET_GLYPH} {fmt_reset(iso)}"
            badge = "" if sev == "normal" else f"  {scol}({sev}){R}"
            lines.append(f"          {DIM}{GREY}{r}{R}{badge}")
        # Trend sparkline — only once at least two samples exist for this metric,
        # so it fills in as history accrues and never shows a lone dot.
        series = trend_map.get(label, ())
        if len(series) >= 2:
            spark = sparkline(series, width=spark_w)
            lines.append(f"          {DIM}{GREY}trend {CREAM}{spark}{R}")

    limits = all_limits(data)
    lines.append(_section("LIMITS"))
    for lim in limits:
        if not lim["active"] and lim["pct"] == 0:
            lines.append(f"{CREAM}{lim['full'][:9]:<9}{R} {DIM}{GREY}idle{R}")
            continue
        bar_row(lim["full"], lim["pct"], lim["iso"], lim["severity"])

    # ── Additional buckets (cowork, host, model-scoped, …) ────────────
    extras = extra_metrics(data, seen_labels=[l["full"] for l in limits])
    if extras:
        lines.append("")
        lines.append(_section("ADDITIONAL"))
        for ex in extras:
            bar_row(ex["full"], ex["pct"], ex["iso"])
    lines.append("")

    # ── Credits / spend ───────────────────────────────────────────────
    sp = data.get("spend") or {}
    lines.append(_section("CREDITS"))
    used = fmt_money(sp.get("used"))
    limit = fmt_money(sp.get("limit"))
    lines.append(_kv("Spent", f"{used} of {limit}", cols))
    if sp.get("balance") is not None:
        lines.append(_kv("Balance", fmt_money(sp.get("balance")), cols))
    if sp.get("cap") is not None:
        lines.append(_kv("Cap", fmt_money(sp.get("cap")), cols))
    enabled = sp.get("enabled")
    ecol = rgb(217, 132, 80) if enabled else GREY
    lines.append(_kv("Credits", "enabled" if enabled else "off", cols, vcol=ecol))
    if sp.get("auto_reload") is not None:
        lines.append(_kv("Auto-reload",
                         "on" if sp.get("auto_reload") else "off", cols))
    if sp.get("can_purchase_credits") is not None:
        lines.append(_kv("Can purchase", "yes" if sp.get("can_purchase_credits")
                         else "no", cols))

    # ── Extra usage ───────────────────────────────────────────────────
    eu = data.get("extra_usage") or {}
    if eu:
        lines.append("")
        lines.append(_section("EXTRA USAGE"))
        if eu.get("is_enabled"):
            util = eu.get("utilization")
            lines.append(_kv("Status", "enabled", cols,
                             vcol=rgb(217, 132, 80)))
            if eu.get("monthly_limit") is not None:
                lines.append(_kv("Monthly limit",
                                 fmt_money(eu.get("monthly_limit"))
                                 if isinstance(eu.get("monthly_limit"), dict)
                                 else str(eu.get("monthly_limit")), cols))
            if eu.get("used_credits") is not None:
                lines.append(_kv("Used credits", str(eu.get("used_credits")), cols))
            if util is not None:
                lines.append(_kv("Utilization", fmt_pct(float(util)), cols))
        else:
            reason = ("user disabled" if eu.get("user_disabled")
                      else eu.get("disabled_reason") or "off")
            lines.append(_kv("Status", reason, cols))
            if eu.get("spend_limit_reached"):
                lines.append(_kv("Spend limit",
                                 "reached", cols, vcol=SEV_COL["critical"]))

    # ── Live sessions / recent chats / account / connectors / cowork ──
    for section in (sessions_section(cols), recent_section(cols),
                    account_section(cols), connectors_section(cols),
                    cowork_section(cols)):
        if section:
            lines.append("")
            lines.extend(section)

    # Footer is always the last visible row, even if content overflows.
    body = lines[:rows - 1]
    while len(body) < rows - 1:
        body.append("")
    body.append(footer_line(now, interval, cols))
    return body

# ── Top-level renderer ────────────────────────────────────────────────
def render(data, cols, rows, interval, mdef, history=None):
    n = len(mdef)
    if rows < 7:
        return render_horizontal(data, cols, rows, interval, mdef)
    if cols < 22:  # narrow / portrait → vertical bar chart
        return (render_vbars if cols >= 3 * n else render_stacked)(
            data, cols, rows, interval, mdef)

    now = datetime.now()

    # Big window → full control panel with everything the API exposes. This is
    # the only layout roomy enough for trend sparklines; smaller ones ignore
    # `history` and degrade cleanly to bars-only.
    if rows >= 20 and cols >= 46:
        return render_panel(data, cols, rows, interval, history)

    if rows >= 12 and cols >= 22:
        lines = [header_full(cols), ""]
        for flab, _clab, pct, iso in mdef:
            lines.append(bar_line_full(flab, pct, cols))
            lines.append(reset_line(iso, cols))
        while len(lines) < rows - 1:
            lines.append("")
        lines.append(footer_line(now, interval, cols))
        return lines[:rows]

    # Compact vertical: one line per metric, with spacers + footer.
    core = [compact_line(clab, pct, cols) for _flab, clab, pct, _iso in mdef]
    has_footer = rows >= len(core) + 4
    remaining = rows - len(core) - 1 - has_footer  # header always present here

    lines = [header_compact(cols, now)]
    if remaining > 0:
        lines.append(""); remaining -= 1
    lines += core
    while remaining > 0:
        lines.append(""); remaining -= 1
    if has_footer:
        lines.append(footer_line(now, interval, cols))
    return lines[:rows]

def error_lines(msg, cols, rows):
    return [f"{rgb(205, 72, 56)}{ln[:cols]}{R}" for ln in msg.split("\n")][:rows]

def draw(lines, last):
    buf = "\033[H"
    for i, ln in enumerate(lines):
        buf += ln + "\033[K"
        if i < len(lines) - 1:
            buf += "\r\n"
    buf += "\033[J"
    if buf != last:
        sys.stdout.write(buf)
        sys.stdout.flush()
    return buf


# ── Snapshot persistence (history) ────────────────────────────────────
# Each core refresh can be recorded as one line of JSON in an append-only
# history file (JSONL) under $XDG_DATA_HOME/claude-usage/history.jsonl. We store
# ONLY the numbers already shown on screen — limit %s + resets, and spend —
# never raw endpoint payloads (privacy + size, D-03). JSONL was chosen over
# SQLite (Q-02): it's greppable, append-only-cheap, trivially unit-testable
# offline, and Phase 5's trends only need a sequential read. Revisit if querying
# ever needs indexed access.
def default_history_path():
    """$XDG_DATA_HOME/claude-usage/history.jsonl (default ~/.local/share/...)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "claude-usage", "history.jsonl")


def _money_value(m):
    """Minor-units money dict -> float major units, or None. Mirrors fmt_money's
    arithmetic but returns a number for storage instead of a display string."""
    if not isinstance(m, dict):
        return None
    amt = m.get("amount_minor")
    if amt is None:
        return None
    return amt / (10 ** m.get("exponent", 2))


def snapshot(data):
    """A serializable record of the current reading: timestamp + the surfaced
    limits (label/pct/reset) + spend. Pure aside from the clock, which the tests
    freeze via the module-level `datetime`, so output is deterministic."""
    limits = [{"label": full, "pct": pct, "resets_at": iso}
              for (full, _compact, pct, iso) in primary_metrics(data)]
    sp = data.get("spend") or {}
    used, limit = sp.get("used") or {}, sp.get("limit") or {}
    currency = (used.get("currency") if isinstance(used, dict) else None) or \
               (limit.get("currency") if isinstance(limit, dict) else None) or "USD"
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "limits": limits,
        "spend": {
            "used": _money_value(sp.get("used")),
            "limit": _money_value(sp.get("limit")),
            "balance": _money_value(sp.get("balance")),
            "currency": currency,
        },
    }


def same_reading(a, b):
    """True if two snapshots carry the same numbers (ignoring the timestamp), so
    the live loop can skip re-appending an identical reading every refresh."""
    if not a or not b:
        return False
    return a.get("limits") == b.get("limits") and a.get("spend") == b.get("spend")


def append_history(path, snap, history_max=0):
    """Append one snapshot as a JSON line, creating parent dirs as needed. When
    history_max > 0, keep only the most recent `history_max` records (cheap
    tail-trim; the file stays small because snapshots are tiny and deduped)."""
    parent = os.path.dirname(path)
    if parent:  # empty for a bare relative filename — cwd already exists
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
    if history_max and history_max > 0:
        records = read_history(path)
        if len(records) > history_max:
            tail = records[-history_max:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in tail:
                    fh.write(json.dumps(r, separators=(",", ":")) + "\n")
            os.replace(tmp, path)


def read_history(path):
    """All snapshots from the history file as a list of dicts. Missing file -> [].
    Corrupt/blank lines are skipped so a half-written line (e.g. a crash mid-
    append) can't make the whole history unreadable."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except (FileNotFoundError, OSError):
        return []
    return out


# ── Threshold alerting ────────────────────────────────────────────────
# Warn *before* a limit is hit. The decision is a pure function over
# (snapshot, thresholds, previous state) so every firing rule is provable
# offline; dispatch is a thin, mockable shell-out that can never take the poll
# down with it. Firing state persists next to the history file so a stateless
# cron `--once` still fires once per crossing instead of on every run.

# Per-limit percentages that trigger an alert. Keys are limit labels as they
# appear on screen / in a snapshot ("5-hour", "7-day", "Opus 7d", …); "default"
# covers every label without an explicit entry. Config overrides these.
DEFAULT_ALERT_THRESHOLDS = {"default": 80, "5-hour": 80, "7-day": 90}

# Config/CLI spellings the user may reasonably reach for, mapped onto the real
# labels, so `[alert] "5h" = 70` works as well as `"5-hour" = 70`.
ALERT_LABEL_ALIASES = {
    "5h": "5-hour", "five_hour": "5-hour", "session": "5-hour",
    "7d": "7-day", "seven_day": "7-day", "weekly": "7-day", "weekly_all": "7-day",
}

ALERT_NOTIFIERS = ("auto", "notify-send", "stdout", "none")


def normalize_thresholds(raw):
    """Config `[alert]` table -> {label: int}. Aliases are folded onto the real
    labels, non-numeric values are dropped (a typo shouldn't wedge alerting),
    and the built-in defaults fill in whatever the user didn't set."""
    out = dict(DEFAULT_ALERT_THRESHOLDS)
    for key, val in (raw or {}).items():
        pct = _coerce_int(val)
        if pct is None:
            continue
        label = ALERT_LABEL_ALIASES.get(str(key).strip().lower(), str(key))
        out[label] = pct
    return out


def threshold_for(label, thresholds):
    """The threshold that applies to one limit label, falling back to
    'default' and finally to the built-in default."""
    if label in thresholds:
        return thresholds[label]
    return thresholds.get("default", DEFAULT_ALERT_THRESHOLDS["default"])


def _parse_ts(iso):
    """ISO timestamp -> epoch seconds, or None. Tolerates a trailing 'Z'."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def evaluate_alerts(snap, thresholds, state, cooldown=0):
    """Pure alert decision. Returns (alerts, new_state).

    An alert fires when a limit's percentage first reaches its threshold. It
    does NOT re-fire while the limit stays above that threshold — the crossing
    is recorded in `state` and only cleared when the limit drops back below
    (a reset), or when the configured threshold itself changes. A non-zero
    `cooldown` (seconds) re-arms a still-elevated limit after that long, for
    people who want to be nagged; the default 0 means once per crossing.

    `state` is a {label: {"threshold": int, "ts": iso}} dict and is never
    mutated in place — the caller persists the returned copy."""
    state = dict(state or {})
    now = _parse_ts(snap.get("ts")) if isinstance(snap, dict) else None
    alerts = []
    for lim in (snap or {}).get("limits") or []:
        label = lim.get("label")
        pct = lim.get("pct")
        if label is None or pct is None:
            continue
        thr = threshold_for(label, thresholds)
        prev = state.get(label) or {}
        if pct < thr:
            state.pop(label, None)  # dropped back below (or reset): re-arm
            continue
        # At or above the threshold. Fire unless we already did for this same
        # threshold and the cooldown (if any) hasn't elapsed.
        fired_thr = prev.get("threshold")
        if fired_thr == thr:
            if not cooldown:
                continue
            since = _parse_ts(prev.get("ts"))
            if now is None or since is None or now - since < cooldown:
                continue
        alerts.append({
            "label": label,
            "pct": pct,
            "threshold": thr,
            "resets_at": lim.get("resets_at"),
            "ts": snap.get("ts"),
        })
        state[label] = {"threshold": thr, "ts": snap.get("ts")}
    return alerts, state


def alert_message(alert):
    """One-line human text for an alert. Shared by every notifier so the
    desktop popup and the stdout line always say the same thing."""
    msg = (f"{alert['label']} usage at {alert['pct']:.0f}% "
           f"(threshold {alert['threshold']}%)")
    iso = alert.get("resets_at")
    return f"{msg} — resets in {fmt_reset(iso)}" if iso else msg


def resolve_notifier(choice, which=shutil.which):
    """'auto' -> 'notify-send' when libnotify is installed, else 'stdout'.
    `which` is injectable so the tests don't depend on the host's PATH."""
    if choice not in ALERT_NOTIFIERS:
        choice = "auto"
    if choice != "auto":
        return choice
    return "notify-send" if which("notify-send") else "stdout"


def dispatch(alert, notifier="auto", runner=None, allow_stdout=True):
    """Deliver one alert. Best-effort by design: a missing or broken notifier
    must never raise into — or hang — the poll loop, so the subprocess is
    time-boxed and every failure degrades to the stdout line. Returns the
    notifier that actually delivered it ('none' if suppressed).

    `allow_stdout=False` is what the live dashboard uses: printing a line into
    a fixed-height redraw would corrupt the frame, and the panel is already
    showing the percentage that triggered the alert."""
    kind = resolve_notifier(notifier)
    text = alert_message(alert)
    if kind == "none":
        return "none"
    if kind == "notify-send":
        run = runner or _run_notify_send
        try:
            if run(alert, text):
                return "notify-send"
        except Exception:  # noqa: BLE001 — notifying is never worth a crash
            pass
    if not allow_stdout:
        return "none"
    print(f"claude-usage: {text}")
    return "stdout"


def dispatch_quiet(alert, notifier="auto"):
    """`dispatch` with the stdout fallback disabled — for the live dashboard."""
    return dispatch(alert, notifier, allow_stdout=False)


def _run_notify_send(alert, text):
    """Shell out to libnotify. Urgency escalates once a limit is nearly spent.
    Time-boxed so a wedged notification daemon can't stall the dashboard."""
    import subprocess
    urgency = "critical" if alert["pct"] >= 95 else "normal"
    subprocess.run(
        ["notify-send", "-a", "claude-usage", "-u", urgency,
         "Claude usage warning", text],
        check=True, timeout=5,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def default_alert_state_path(history_path):
    """Firing state lives beside the history file (same dir, alerts.json) so
    the two move together when `history_path` is overridden."""
    parent = os.path.dirname(history_path)
    # A bare relative history path keeps a bare state path (no "./" prefix),
    # matching append_history's handling of the same case.
    return os.path.join(parent, "alerts.json") if parent else "alerts.json"


def read_alert_state(path):
    """Persisted firing state, or {} for a missing/corrupt file — a bad state
    file must degrade to 'nothing has fired yet', never to a crash."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def write_alert_state(path, state):
    """Persist firing state atomically (write + rename), so a crash mid-write
    can't leave a truncated file that re-fires every alert."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, separators=(",", ":"))
    os.replace(tmp, path)


def process_alerts(snap, cfg, dispatcher=dispatch):
    """Evaluate + deliver + persist, in one best-effort call shared by the live
    loop and `--once`. Returns the list of alerts that fired (empty when
    alerting is off or anything went wrong)."""
    if not cfg.get("alerts"):
        return []
    path = cfg["alert_state_path"]
    try:
        state = read_alert_state(path)
        alerts, new_state = evaluate_alerts(
            snap, cfg["alert_thresholds"], state, cfg["alert_cooldown"])
        if new_state != state:
            write_alert_state(path, new_state)
    except OSError:  # state unreadable/unwritable — don't take the poll down
        return []
    for a in alerts:
        dispatcher(a, cfg["alert_notifier"])
    return alerts


def run_once(cookie, cfg, print_snap=False):
    """Headless single-shot: fetch one reading, append a snapshot to history,
    optionally echo it, and return a process exit code. Silent by default so
    it's clean in a crontab; --print echoes the JSON.

    Exit codes: 0 = fine, 1 = fetch/persist failed, 2 = a threshold alert
    fired. A cron wrapper can act on 2 alone without treating a transient
    network blip as a usage warning."""
    global BOOT
    BOOT = fetch_bootstrap(cookie)
    org = resolve_org(cfg["org"], BOOT, cookie)
    try:
        data = fetch(cookie, org)
        snap = snapshot(data)
    except Exception as e:  # noqa: BLE001 — cron wants a nonzero exit, not a trace
        print(f"claude-usage --once: fetch failed: {e}", file=sys.stderr)
        return 1
    if cfg["persist"]:
        try:
            append_history(cfg["history_path"], snap, cfg["history_max"])
        except OSError as e:
            print(f"claude-usage --once: could not write history: {e}",
                  file=sys.stderr)
            return 1
    if print_snap:
        print(json.dumps(snap, separators=(",", ":")))
    return 2 if process_alerts(snap, cfg) else 0


def main():
    ap = argparse.ArgumentParser()
    # Flag defaults are None so the resolver can tell "unset" from an explicit
    # value; the real defaults live in resolve_runtime_config().
    ap.add_argument("-n", "--interval", type=int, default=None,
                    help="core usage refresh seconds (limits/credits) [120]")
    # Panel extras change slowly, so they poll on a lazier clock by default.
    ap.add_argument("--extras-interval", type=int, default=None,
                    help="refresh seconds for live extras (sessions, chats, "
                         "cowork) — the big-panel data [300]")
    ap.add_argument("--bootstrap-interval", type=int, default=None,
                    help="re-fetch account/org config every N seconds "
                         "[0 = only once at startup]")
    ap.add_argument("--cookie", help="paste 'sessionKey=...' manually")
    ap.add_argument("--cookie-source", choices=COOKIE_SOURCES, default=None,
                    help="which browser to read cookies from [auto]")
    ap.add_argument("--org", default=None,
                    help="organization UUID (default: auto-discovered from "
                         "/api/bootstrap)")
    ap.add_argument("--once", action="store_true",
                    help="fetch one snapshot, append it to history, exit "
                         "(cron-friendly; silent unless --print)")
    ap.add_argument("--print", dest="print_snap", action="store_true",
                    help="with --once, also print the snapshot JSON to stdout")
    ap.add_argument("--no-persist", dest="persist", action="store_const",
                    const=False, default=None,
                    help="don't write snapshots to the history file")
    ap.add_argument("--history-path", default=None,
                    help="override the history file location")
    ap.add_argument("--no-alerts", dest="alerts", action="store_const",
                    const=False, default=None,
                    help="don't warn when a limit crosses its threshold")
    ap.add_argument("--alert-threshold", type=int, default=None,
                    help="warn at this percent for EVERY limit, overriding "
                         "the per-limit [alert] config table")
    ap.add_argument("--alert-notifier", choices=ALERT_NOTIFIERS, default=None,
                    help="how to deliver alerts [auto: notify-send if present, "
                         "else stdout]")
    ap.add_argument("--alert-state-path", default=None,
                    help="override where once-per-crossing state is stored")
    args = ap.parse_args()

    # Precedence: CLI flag > env var > config file > built-in default.
    cli = {"interval": args.interval, "extras_interval": args.extras_interval,
           "bootstrap_interval": args.bootstrap_interval, "org": args.org,
           "cookie_source": args.cookie_source, "persist": args.persist,
           "history_path": args.history_path, "alerts": args.alerts,
           "alert_threshold": args.alert_threshold,
           "alert_notifier": args.alert_notifier,
           "alert_state_path": args.alert_state_path}
    env = {"interval": os.environ.get("CLAUDE_USAGE_INTERVAL"),
           "extras_interval": os.environ.get("CLAUDE_USAGE_EXTRAS_INTERVAL"),
           "bootstrap_interval": os.environ.get("CLAUDE_USAGE_BOOTSTRAP_INTERVAL"),
           "org": os.environ.get("CLAUDE_ORG_ID"),
           "cookie_source": os.environ.get("CLAUDE_USAGE_COOKIE_SOURCE"),
           "persist": os.environ.get("CLAUDE_USAGE_PERSIST"),
           "history_path": os.environ.get("CLAUDE_USAGE_HISTORY_PATH"),
           "alerts": os.environ.get("CLAUDE_USAGE_ALERTS"),
           "alert_threshold": os.environ.get("CLAUDE_USAGE_ALERT_THRESHOLD"),
           "alert_notifier": os.environ.get("CLAUDE_USAGE_ALERT_NOTIFIER"),
           "alert_state_path": os.environ.get("CLAUDE_USAGE_ALERT_STATE_PATH")}
    cfg = resolve_runtime_config(cli, env, load_config())
    interval = cfg["interval"]
    extras_interval = cfg["extras_interval"]
    bootstrap_interval = cfg["bootstrap_interval"]
    cookie_source = cfg["cookie_source"]
    persist = cfg["persist"]
    history_path = cfg["history_path"]
    history_max = cfg["history_max"]

    cookie = get_cookie(args.cookie, cookie_source)

    # ── Headless one-shot: fetch once, persist, exit. Cron-friendly. ──
    if args.once:
        return run_once(cookie, cfg, args.print_snap)

    global BOOT, LIVE
    BOOT = fetch_bootstrap(cookie)  # account/org config for the big panel
    org = resolve_org(cfg["org"], BOOT, cookie)
    cache, err = None, None
    last_fetch = last_extra = last_boot = 0.0
    last_buf, force = "", True
    # Recent history for trend sparklines: seed from disk (so prior runs / cron
    # samples show up immediately), then extend live. Read-only view; capped so
    # a long-lived process doesn't grow it unbounded.
    HIST_KEEP = 64
    hist_recent = read_history(history_path)[-HIST_KEEP:]
    # Seed dedup state from the last stored reading so the first live fetch of an
    # unchanged reading isn't re-appended (a duplicate point / history line).
    last_snap = hist_recent[-1] if hist_recent else None

    is_tty = sys.stdin.isatty()
    old_term = None
    if is_tty:
        import termios, tty
        old_term = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    sys.stdout.write("\033[?25l")  # hide cursor
    try:
        while True:
            now = time.time()
            # Core usage — the fast clock (also refreshed on manual [r]/force).
            if force or now - last_fetch >= interval:
                # Re-sync the cookie from the browser first: claude.ai rotates
                # sessionKey/cf_clearance/__cf_bm periodically, and the browser
                # holds the fresh values. Skipped when a manual cookie is set.
                if not args.cookie:
                    fresh = read_browser_cookie(cookie_source)
                    if fresh:
                        cookie = fresh
                try:
                    cache, err = fetch(cookie, org), None
                except requests.HTTPError as e:
                    code = e.response.status_code
                    body = (e.response.text or "")[:400]
                    hdrs = e.response.headers
                    cf_challenge = code == 403 and (
                        e.response.headers.get("cf-mitigated") == "challenge"
                        or "Just a moment" in body
                        or "Enable JavaScript and cookies" in body
                        or (bool(hdrs.get("cf-ray"))
                            and "cloudflare" in hdrs.get("Server", "").lower()
                            and not body.lstrip().startswith("{")))
                    recovered = False
                    if code in (401, 403):
                        # Cookie may have just rotated in Chrome — re-read and
                        # retry once before surfacing an error to the user.
                        if not args.cookie:
                            fresh = read_browser_cookie(cookie_source)
                            if fresh and fresh != cookie:
                                cookie = fresh
                                try:
                                    cache, err = fetch(cookie, org), None
                                    last_extra = 0  # re-pull extras w/ new cookie
                                    recovered = True
                                except Exception:
                                    pass
                    # Only surface an error if the retry did NOT recover — a
                    # successful re-fetch must not be masked by the (first-
                    # response) challenge/auth message, which would also drop
                    # the good snapshot below.
                    if recovered:
                        pass
                    elif cf_challenge:
                        # Cloudflare challenge, not an auth problem: the
                        # cf_clearance cookie is stale or _UA no longer matches
                        # the Chrome that earned it.
                        err = ("Cloudflare challenge (403).\nLoad claude.ai in "
                               "Chrome once to refresh cf_clearance "
                               "(UA is auto-detected from your Chrome).")
                    elif code in (401, 403):
                        err = f"Auth failed ({code}).\nRe-login in your browser — cookie expired."
                    else:
                        err = f"HTTP {code}"
                except Exception as e:
                    err = f"Error: {e}"
                last_fetch = now
                # Snapshot this reading on a clean fetch, only when the numbers
                # changed since the last one — a 2-min refresh of an unchanged
                # reading shouldn't bloat history or the trend buffer. The
                # in-memory buffer feeds sparklines even when file persistence is
                # off; the file write is gated on `persist`. Guarded broadly:
                # neither a malformed reading nor a write error kills the dash.
                if cache and not err:
                    try:
                        snap = snapshot(cache)
                        # Alerts are evaluated on every clean reading (not only
                        # changed ones): re-firing is already suppressed by the
                        # persisted crossing state, and this way a threshold
                        # edited mid-run takes effect on the next tick.
                        process_alerts(snap, cfg, dispatch_quiet)
                        if not same_reading(snap, last_snap):
                            last_snap = snap
                            hist_recent.append(snap)
                            del hist_recent[:-HIST_KEEP]  # keep the tail bounded
                            if persist:
                                append_history(history_path, snap, history_max)
                    except Exception:  # noqa: BLE001 — persistence is best-effort
                        pass
            # Live extras — the lazy clock; manual [r] forces them too.
            if force or now - last_extra >= extras_interval:
                LIVE = fetch_live(cookie, org)  # best-effort; may hold Nones
                last_extra = now
            # Bootstrap — once at startup unless a refresh interval was set.
            if bootstrap_interval and now - last_boot >= bootstrap_interval:
                BOOT = fetch_bootstrap(cookie) or BOOT
                last_boot = now
            force = False

            mdef = primary_metrics(cache) if cache else []
            cols, rows = shutil.get_terminal_size((80, 24))
            lines = (error_lines(err, cols, rows) if err
                     else render(cache, cols, rows, interval, mdef, hist_recent))
            last_buf = draw(lines, last_buf)

            if is_tty:
                ready, _, _ = select.select([sys.stdin], [], [], 0.3)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch in ("r", "R", " "):
                        force = True
                    elif ch in ("q", "Q", "\x03"):
                        break
            else:
                time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")  # restore cursor
        if old_term is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_term)

if __name__ == "__main__":
    # Propagate main()'s return value as the exit code so `python claude_usage.py
    # --once` reports fetch/persist failures to cron, matching the console-script
    # wrapper (which does sys.exit(main())).
    sys.exit(main())
