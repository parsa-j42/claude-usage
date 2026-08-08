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

# ── Cookie acquisition (Chrome/Chromium first) ────────────────────────
# claude.ai rotates sessionKey, cf_clearance and __cf_bm out from under a
# long-running process. The browser always has the fresh values, so the dash
# re-reads the cookie from Chrome on every fetch tick (see the main loop) and
# immediately on a 401/403. That's what keeps it alive for days without a
# manual re-login. read_chrome_cookie() is therefore loop-safe: it NEVER exits
# and returns None on any failure so the caller can keep its last-known-good.
def read_chrome_cookie():
    """Current claude.ai cookie header from Chrome/Chromium, or None. No exits."""
    try:
        import browser_cookie3 as bc
    except ImportError:
        return None
    for loader in (bc.chrome, bc.chromium):
        try:
            cj = loader(domain_name="claude.ai")
            cookies = {c.name: c.value for c in cj}
            if "sessionKey" in cookies:
                return "; ".join(f"{k}={v}" for k, v in cookies.items())
        except Exception:
            continue
    return None

def get_cookie(manual=None):
    """Startup cookie resolve. Exits with a helpful message if nothing is found
    — but only ever called once, before the loop. Inside the loop use
    read_chrome_cookie() so a transient keyring/DB lock can't kill the dash."""
    if manual:
        return manual
    ck = read_chrome_cookie()
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
    sys.exit(
        "No Claude session cookie found in Chrome/Chromium.\n"
        "Make sure you're logged in, or pass --cookie manually.\n"
        "(If your keyring is locked, that can block cookie decryption.)"
    )

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

def render_panel(data, cols, rows, interval):
    now = datetime.now()
    lines = []
    t = now.strftime("%-I:%M%p").lower()
    lines.append(join_lr(f"{STAR} Claude Usage", f"{REFRESH} {t}", cols))
    lines.append("")

    # ── Limits ────────────────────────────────────────────────────────
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
def render(data, cols, rows, interval, mdef):
    n = len(mdef)
    if rows < 7:
        return render_horizontal(data, cols, rows, interval, mdef)
    if cols < 22:  # narrow / portrait → vertical bar chart
        return (render_vbars if cols >= 3 * n else render_stacked)(
            data, cols, rows, interval, mdef)

    now = datetime.now()

    # Big window → full control panel with everything the API exposes.
    if rows >= 20 and cols >= 46:
        return render_panel(data, cols, rows, interval)

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--interval", type=int, default=120,
                    help="core usage refresh seconds (limits/credits)")
    # Panel extras change slowly, so they poll on a lazier clock by default.
    ap.add_argument("--extras-interval", type=int, default=300,
                    help="refresh seconds for live extras (sessions, chats, "
                         "cowork) — the big-panel data (default 300)")
    ap.add_argument("--bootstrap-interval", type=int, default=0,
                    help="re-fetch account/org config every N seconds "
                         "(default 0 = only once at startup)")
    ap.add_argument("--cookie", help="paste 'sessionKey=...' manually")
    ap.add_argument("--org", default=os.environ.get("CLAUDE_ORG_ID"),
                    help="organization UUID (default: $CLAUDE_ORG_ID, else "
                         "auto-discovered from /api/bootstrap)")
    args = ap.parse_args()
    # Never poll extras faster than the core clock — a big -n implies laziness.
    extras_interval = max(args.extras_interval, args.interval)

    cookie = get_cookie(args.cookie)
    global BOOT, LIVE
    BOOT = fetch_bootstrap(cookie)  # account/org config for the big panel
    org = resolve_org(args.org, BOOT, cookie)
    cache, err = None, None
    last_fetch = last_extra = last_boot = 0.0
    last_buf, force = "", True

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
            if force or now - last_fetch >= args.interval:
                # Re-sync the cookie from Chrome first: claude.ai rotates
                # sessionKey/cf_clearance/__cf_bm periodically, and the browser
                # holds the fresh values. Skipped when a manual cookie is set.
                if not args.cookie:
                    fresh = read_chrome_cookie()
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
                    if code in (401, 403):
                        # Cookie may have just rotated in Chrome — re-read and
                        # retry once before surfacing an error to the user.
                        if not args.cookie:
                            fresh = read_chrome_cookie()
                            if fresh and fresh != cookie:
                                cookie = fresh
                                try:
                                    cache, err = fetch(cookie, org), None
                                    last_extra = 0  # re-pull extras w/ new cookie
                                except Exception:
                                    pass
                    if cf_challenge:
                        # Cloudflare challenge, not an auth problem: the
                        # cf_clearance cookie is stale or _UA no longer matches
                        # the Chrome that earned it.
                        err = ("Cloudflare challenge (403).\nLoad claude.ai in "
                               "Chrome once to refresh cf_clearance "
                               "(UA is auto-detected from your Chrome).")
                    elif code in (401, 403):
                        err = f"Auth failed ({code}).\nRe-login in Chrome — cookie expired."
                    else:
                        err = f"HTTP {code}"
                except Exception as e:
                    err = f"Error: {e}"
                last_fetch = now
            # Live extras — the lazy clock; manual [r] forces them too.
            if force or now - last_extra >= extras_interval:
                LIVE = fetch_live(cookie, org)  # best-effort; may hold Nones
                last_extra = now
            # Bootstrap — once at startup unless a refresh interval was set.
            if args.bootstrap_interval and now - last_boot >= args.bootstrap_interval:
                BOOT = fetch_bootstrap(cookie) or BOOT
                last_boot = now
            force = False

            mdef = primary_metrics(cache) if cache else []
            cols, rows = shutil.get_terminal_size((80, 24))
            lines = (error_lines(err, cols, rows) if err
                     else render(cache, cols, rows, args.interval, mdef))
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
    main()
