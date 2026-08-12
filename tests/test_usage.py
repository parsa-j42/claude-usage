#!/usr/bin/env python3
"""Regression + logic tests for claude-usage.

Two things are checked:

  1. Rendering is deterministic. A fixed fixture is rendered across the full
     terminal-size matrix under a frozen clock; the output is asserted against
     an inline snapshot so refactors can be shown not to change any pixel.
  2. Organization resolution honours its precedence (explicit → discovery) and
     fails loudly when no org is available.

Run:  python3 tests/test_usage.py
No third-party test runner needed.
"""
import os
import sys
import time
from datetime import datetime, timezone

# Freezing the clock isn't enough to make rendering reproducible: fmt_reset()
# formats reset times in the LOCAL zone, so the same frozen instant renders
# "2:00pm" here and "6:00pm" on a UTC CI runner. Pin the zone as well, before
# anything imports the module, so byte-exact assertions hold everywhere.
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):  # POSIX only; the suite targets Linux/CI
    time.tzset()

# The runtime is now an importable module (claude_usage.py). Ensure the repo
# root is on sys.path so `import claude_usage` works whether or not the package
# is pip-installed.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir))


def load_module():
    """Import the module fresh and freeze its clock."""
    import importlib

    import claude_usage as mod
    importlib.reload(mod)

    fixed = datetime(2026, 8, 7, 14, 30, 15, tzinfo=timezone.utc)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    mod.datetime = _FrozenDT
    return mod


# When run under pytest, expose the loaded+frozen module as a `mod` fixture so
# the existing `test_*(mod)` functions are collected as-is. The standalone
# `python3 tests/test_usage.py` path (see main()) still injects `mod` itself.
try:
    import pytest

    @pytest.fixture
    def mod():
        return load_module()
except ImportError:  # pytest not installed — the __main__ path still works.
    pass


# A fixture broad enough to light up every panel section and both the
# normalized `limits` array and the legacy five_hour/seven_day fallback.
DATA = {
    "limits": [
        {"kind": "session", "percent": 42.5, "resets_at": "2026-08-07T18:00:00Z",
         "is_active": True, "severity": "normal"},
        {"kind": "weekly_all", "percent": 88.0, "resets_at": "2026-08-12T00:00:00Z",
         "is_active": True, "severity": "warning"},
    ],
    "spend": {"used": {"amount_minor": 1234, "exponent": 2, "currency": "USD"},
              "limit": {"amount_minor": 5000, "exponent": 2, "currency": "USD"},
              "enabled": True},
}
LEGACY = {"five_hour": {"utilization": 30.0, "resets_at": "2026-08-07T18:00:00Z"},
          "seven_day": {"utilization": 60.0, "resets_at": "2026-08-12T00:00:00Z"}}

SIZES = [(80, 24), (46, 20), (40, 5), (22, 12), (20, 20), (40, 1), (100, 40)]


def render_all(mod, data):
    out = []
    for cols, rows in SIZES:
        mdef = mod.primary_metrics(data)
        out.append(mod.render(data, cols, rows, 120, mdef))
    return out


def test_render_is_deterministic(mod):
    """Same input + frozen clock ⇒ identical output on every call."""
    first = render_all(mod, DATA)
    second = render_all(mod, DATA)
    assert first == second, "render output is not deterministic"
    # And every size produces exactly `rows` lines.
    for (cols, rows), lines in zip(SIZES, first):
        assert len(lines) == rows, f"{cols}x{rows} produced {len(lines)} lines"


def test_legacy_fallback_renders(mod):
    """The legacy five_hour/seven_day shape still yields two limits."""
    metrics = mod.primary_metrics(LEGACY)
    labels = {m[0] for m in metrics}
    assert {"5-hour", "7-day"} <= labels, labels


def test_org_discovery(mod):
    assert mod.discover_org_id(
        {"account": {"memberships": [{"organization": {"uuid": "U-1"}}]}}) == "U-1"
    assert mod.discover_org_id(
        {"account": {"memberships": [{"organization": {"id": "I-2"}}]}}) == "I-2"
    # first membership empty, second populated
    assert mod.discover_org_id({"account": {"memberships": [
        {"organization": {}}, {"organization": {"uuid": "U-3"}}]}}) == "U-3"
    assert mod.discover_org_id(None) is None
    assert mod.discover_org_id({}) is None


def test_org_discovery_prefers_subscription_over_api(mod):
    """An account with both an api-only Console org and a chat/Max org must
    pick the subscription org — the api-only one 403s on /usage."""
    boot = {"account": {"memberships": [
        {"organization": {"uuid": "API", "capabilities": ["api"]}},
        {"organization": {"uuid": "MAX", "capabilities": ["chat", "claude_max"]}},
    ]}}
    # No cookie: fall back to capability ranking → the subscription org.
    assert mod.discover_org_id(boot) == "MAX"
    # lastActiveOrg cookie wins outright, even when it's the api org.
    ck = "sessionKey=x; lastActiveOrg=API"
    assert mod.discover_org_id(boot, ck) == "API"


def test_org_resolution_precedence(mod):
    boot = {"account": {"memberships": [{"organization": {"uuid": "DISCOVERED"}}]}}
    assert mod.resolve_org("EXPLICIT", boot) == "EXPLICIT"  # explicit wins
    assert mod.resolve_org(None, boot) == "DISCOVERED"      # falls back to discovery
    try:
        mod.resolve_org(None, None)
    except SystemExit as e:
        assert "organization" in str(e).lower()
    else:
        raise AssertionError("resolve_org should exit when no org is available")


def test_usage_url(mod):
    assert mod.usage_url("ABC") == "https://claude.ai/api/organizations/ABC/usage"


# ── Phase 3: config file + setting precedence ─────────────────────────

def test_resolve_setting_precedence(mod):
    r = mod.resolve_setting
    assert r("cli", "env", "cfg", "def") == "cli"    # CLI wins
    assert r(None, "env", "cfg", "def") == "env"     # then env
    assert r(None, None, "cfg", "def") == "cfg"      # then config
    assert r(None, None, None, "def") == "def"       # then default
    # `is not None`, not truthiness: an explicit 0 must beat a later source.
    assert r(0, None, 300, 999) == 0
    assert r(None, None, 0, 999) == 0


def test_runtime_config_full_precedence(mod):
    rc = mod.resolve_runtime_config
    # Every source empty → built-in defaults.
    d = rc({}, {}, {})
    for k, v in {"interval": 120, "extras_interval": 300, "bootstrap_interval": 0,
                 "org": None, "cookie_source": "auto", "persist": True,
                 "history_max": 0}.items():
        assert d[k] == v, (k, d[k])
    assert d["history_path"].endswith("claude-usage/history.jsonl")
    # CLI beats env beats config, field by field.
    cli = {"interval": 10, "org": "CLI"}
    env = {"interval": 20, "org": "ENV", "cookie_source": "firefox"}
    cfg = {"interval": 30, "org": "CFG", "cookie_source": "chrome",
           "bootstrap_interval": 45}
    out = rc(cli, env, cfg)
    assert out["interval"] == 10           # CLI
    assert out["org"] == "CLI"             # CLI
    assert out["cookie_source"] == "firefox"  # env (no CLI)
    assert out["bootstrap_interval"] == 45    # config (no CLI/env)
    # Full fall-through table for org and cookie_source (not just CLI wins).
    assert rc({}, {"org": "ENV"}, {"org": "CFG"})["org"] == "ENV"
    assert rc({}, {}, {"org": "CFG"})["org"] == "CFG"
    assert rc({}, {}, {"cookie_source": "chrome"})["cookie_source"] == "chrome"


def test_runtime_config_zero_beats_config(mod):
    """An explicit --bootstrap-interval 0 must not be overridden by config."""
    out = mod.resolve_runtime_config(
        {"bootstrap_interval": 0}, {}, {"bootstrap_interval": 300})
    assert out["bootstrap_interval"] == 0


def test_runtime_config_coerces_and_validates(mod):
    rc = mod.resolve_runtime_config
    # String ints from env/config are coerced; garbage falls through to default.
    assert rc({}, {"interval": "30"}, {})["interval"] == 30
    assert rc({}, {}, {"interval": "not-a-number"})["interval"] == 120
    # extras_interval is floored at interval (a big -n implies laziness).
    assert rc({"interval": 500}, {}, {"extras_interval": 100})["extras_interval"] == 500
    # An unknown cookie-source is rejected back to 'auto'.
    assert rc({"cookie_source": "safari"}, {}, {})["cookie_source"] == "auto"


def test_load_config(mod):
    import tempfile
    # A well-formed file parses to a dict.
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write('interval = 42\ncookie_source = "firefox"\n')
        good = f.name
    try:
        # The env-marked `tomli` dep guarantees a parser on every supported
        # Python, so this is a hard gate — it's what makes the 3.9 CI leg prove
        # the backport (and the dependency marker) actually resolve.
        assert mod._toml is not None, "no TOML parser — check the tomli marker"
        assert mod.load_config(good) == {"interval": 42, "cookie_source": "firefox"}
    finally:
        os.unlink(good)
    # Missing file and malformed content both degrade to {}.
    assert mod.load_config(os.path.join(_HERE, "does-not-exist.toml")) == {}
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write("this is = = not valid toml\n")
        bad = f.name
    try:
        assert mod.load_config(bad) == {}
    finally:
        os.unlink(bad)


# ── Phase 4: snapshot persistence + --once ────────────────────────────

def test_persistence_settings_precedence(mod):
    rc = mod.resolve_runtime_config
    # persist: default on; env "false" turns it off; CLI False wins over config.
    assert rc({}, {}, {})["persist"] is True
    assert rc({}, {"persist": "false"}, {})["persist"] is False
    assert rc({"persist": False}, {}, {"persist": True})["persist"] is False
    assert rc({}, {}, {"persist": False})["persist"] is False
    # history_path override flows through CLI > env > config.
    assert rc({"history_path": "/a"}, {"history_path": "/b"}, {})["history_path"] == "/a"
    assert rc({}, {}, {"history_path": "/c"})["history_path"] == "/c"
    # A leading ~ in a config/env path is expanded.
    assert rc({}, {}, {"history_path": "~/h.jsonl"})["history_path"] == \
        os.path.expanduser("~/h.jsonl")
    # history_max coerces and floors at 0.
    assert rc({}, {}, {"history_max": "50"})["history_max"] == 50
    assert rc({"history_max": -5}, {}, {})["history_max"] == 0


def test_snapshot_shape(mod):
    """snapshot() captures the surfaced limits + spend, with a frozen ts."""
    snap = mod.snapshot(DATA)
    assert snap["ts"] == "2026-08-07T14:30:15+00:00"  # frozen clock
    labels = {l["label"] for l in snap["limits"]}
    assert {"5-hour", "7-day"} <= labels, labels
    # Percentages are carried through from the fixture's two limits.
    pcts = {l["label"]: l["pct"] for l in snap["limits"]}
    assert pcts["5-hour"] == 42.5 and pcts["7-day"] == 88.0
    # Spend is stored as numbers (major units), not display strings.
    assert snap["spend"]["used"] == 12.34
    assert snap["spend"]["limit"] == 50.0
    assert snap["spend"]["currency"] == "USD"
    # Must be JSON-serializable (it's written as a JSON line).
    import json
    assert json.loads(json.dumps(snap)) == snap


def test_history_roundtrip(mod):
    import tempfile
    d = tempfile.mkdtemp()
    path = os.path.join(d, "sub", "history.jsonl")  # parent dir auto-created
    try:
        s1 = mod.snapshot(DATA)
        s2 = mod.snapshot(LEGACY)
        mod.append_history(path, s1)
        mod.append_history(path, s2)
        recs = mod.read_history(path)
        assert recs == [s1, s2]
        # Missing file reads back empty, never raises.
        assert mod.read_history(os.path.join(d, "nope.jsonl")) == []
        # A corrupt line is skipped, not fatal.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not json}\n")
        assert mod.read_history(path) == [s1, s2]
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_history_retention_cap(mod):
    import tempfile, shutil
    d = tempfile.mkdtemp()
    path = os.path.join(d, "history.jsonl")
    try:
        for i in range(5):
            mod.append_history(path, {"ts": str(i), "limits": [], "spend": {}},
                               history_max=3)
        recs = mod.read_history(path)
        assert [r["ts"] for r in recs] == ["2", "3", "4"]  # only last 3 kept
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_same_reading_dedup(mod):
    a = mod.snapshot(DATA)
    b = mod.snapshot(DATA)  # identical numbers, same frozen ts
    assert mod.same_reading(a, b) is True
    c = mod.snapshot(LEGACY)
    assert mod.same_reading(a, c) is False
    assert mod.same_reading(a, None) is False


def test_append_history_bare_relative_path(mod):
    """A history_path with no directory component must not raise (os.makedirs('')
    would). Write into a temp cwd so we don't litter the repo."""
    import tempfile, shutil
    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(d)
        mod.append_history("history.jsonl", {"ts": "1", "limits": [], "spend": {}})
        assert mod.read_history("history.jsonl") == [
            {"ts": "1", "limits": [], "spend": {}}]
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


def test_run_once_snapshot_error_is_clean(mod):
    """A reading that breaks snapshot() yields exit 1, not a traceback."""
    import tempfile, shutil
    d = tempfile.mkdtemp()
    path = os.path.join(d, "history.jsonl")
    try:
        mod.fetch_bootstrap = lambda cookie: {"account": {"memberships": [
            {"organization": {"uuid": "ORG"}}]}}
        # `data` whose limits entry is a bare string breaks all_limits/primary.
        mod.fetch = lambda cookie, org: {"limits": ["not-a-dict"]}
        cfg = {"org": None, "persist": True, "history_path": path, "history_max": 0}
        assert mod.run_once("ck", cfg, print_snap=False) == 1
        assert mod.read_history(path) == []  # nothing written
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── Phase 5: trends & sparklines ──────────────────────────────────────

# History whose 5-hour/7-day labels match DATA's limit labels, so the panel can
# draw a trend line for each.
HISTORY = [
    {"ts": f"2026-08-07T1{i}:00:00+00:00",
     "limits": [{"label": "5-hour", "pct": p, "resets_at": None},
                {"label": "7-day", "pct": p / 2, "resets_at": None}],
     "spend": {}}
    for i, p in enumerate((0, 14, 28, 42, 57, 71, 85, 100))
]


def test_sparkline_exact(mod):
    # Fixed 0–100 scale maps evenly across the eight glyphs.
    assert mod.sparkline([0, 14, 28, 42, 57, 71, 85, 100]) == "▁▂▃▄▅▆▇█"
    # Clamping out-of-range, dropping non-numbers, and the empty case.
    assert mod.sparkline([-10, 200, 50]) == "▁█▅"
    assert mod.sparkline([]) == ""
    assert mod.sparkline([True, "x", None, 50]) == "▅"  # bool/str/None dropped
    # width keeps only the most recent samples.
    assert mod.sparkline([0, 0, 0, 100], width=2) == "▁█"


def test_trend_series(mod):
    assert mod.trend_series(HISTORY, "5-hour") == [0, 14, 28, 42, 57, 71, 85, 100]
    assert mod.trend_series(HISTORY, "7-day", n=3) == [35.5, 42.5, 50.0]  # last 3
    assert mod.trend_series(HISTORY, "nonexistent") == []
    assert mod.trend_series(None, "5-hour") == []


def test_history_index(mod):
    idx = mod.history_index(HISTORY)
    assert set(idx) == {"5-hour", "7-day"}
    assert idx["5-hour"] == [0, 14, 28, 42, 57, 71, 85, 100]
    assert mod.history_index(None) == {}
    # Non-numeric / bool pcts and unlabeled entries are dropped.
    assert mod.history_index([{"limits": [
        {"label": "x", "pct": True}, {"label": None, "pct": 5},
        {"label": "x", "pct": 5}]}]) == {"x": [5.0]}


def test_panel_trend_line_fits_width(mod):
    """With a long history the trend line must never exceed the panel width (a
    16-col prefix + capped glyphs), or it wraps and breaks the fixed layout."""
    long_hist = [{"limits": [{"label": "5-hour", "pct": (i * 7) % 100},
                             {"label": "7-day", "pct": (i * 3) % 100}]}
                 for i in range(60)]  # more than any width would show
    mdef = mod.primary_metrics(DATA)
    for cols in (46, 60, 100):
        lines = _strip(mod.render(DATA, cols, 40, 120, mdef, long_hist,
                                  None, True))
        assert max(len(l) for l in lines) <= cols, f"overflow at cols={cols}"
        assert any(l.strip().startswith("trend ") for l in lines)


def _strip(lines):
    import re
    return [re.sub(r"\033\[[0-9;]*m", "", l) for l in lines]


def test_panel_shows_sparkline(mod):
    """The big layout gains a `trend` line (with block glyphs) once ≥2 samples
    exist; output is identical across calls (deterministic)."""
    mdef = mod.primary_metrics(DATA)
    lines = mod.render(DATA, 100, 40, 120, mdef, HISTORY, None, True)
    trend_lines = [l for l in _strip(lines) if l.strip().startswith("trend ")]
    assert trend_lines, "no trend line in the panel"
    # The glyphs after the marker must be sparkline blocks.
    glyphs = trend_lines[0].strip()[len("trend "):]
    assert glyphs and all(g in mod._SPARK_GLYPHS for g in glyphs)
    assert mod.render(DATA, 100, 40, 120, mdef, HISTORY, None, True) == lines


def test_small_layouts_have_no_sparkline(mod):
    """Sparklines are panel-only. The `trend` marker (which the full block █ in
    a progress bar could otherwise be confused with) must be absent, and each
    layout still emits exactly `rows` lines."""
    mdef = mod.primary_metrics(DATA)
    for cols, rows in [(40, 5), (45, 20), (22, 12), (40, 1)]:
        lines = mod.render(DATA, cols, rows, 120, mdef, HISTORY)
        plain = "\n".join(_strip(lines))
        assert "trend " not in plain, f"{cols}x{rows}"
        assert len(lines) == rows


def test_panel_no_history_is_graceful(mod):
    """Empty/short history ⇒ no trend line, no crash."""
    import re
    mdef = mod.primary_metrics(DATA)
    for hist in (None, [], HISTORY[:1]):  # short = single sample
        lines = mod.render(DATA, 100, 40, 120, mdef, hist, None, True)
        plain = "\n".join(re.sub(r"\033\[[0-9;]*m", "", l) for l in lines)
        assert "trend " not in plain, f"history={hist}"


def test_run_once_persists(mod):
    """--once path: fetch (monkeypatched) -> snapshot -> append; returns 0."""
    import tempfile, shutil
    d = tempfile.mkdtemp()
    path = os.path.join(d, "history.jsonl")
    try:
        mod.fetch = lambda cookie, org: DATA
        mod.fetch_bootstrap = lambda cookie: {"account": {"memberships": [
            {"organization": {"uuid": "ORG"}}]}}
        cfg = {"org": None, "persist": True, "history_path": path,
               "history_max": 0}
        rc = mod.run_once("cookie", cfg, print_snap=False)
        assert rc == 0
        recs = mod.read_history(path)
        assert len(recs) == 1 and recs[0]["spend"]["used"] == 12.34
        # A fetch failure yields a nonzero exit and writes nothing new.
        def boom(cookie, org):
            raise RuntimeError("network down")
        mod.fetch = boom
        assert mod.run_once("cookie", cfg, print_snap=False) == 1
        assert len(mod.read_history(path)) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── Phase 6: threshold alerting + notifications ───────────────────────

def _snap(pct_5h, pct_7d, ts="2026-08-07T14:00:00+00:00"):
    """A minimal snapshot with just the two headline limits."""
    return {"ts": ts, "limits": [
        {"label": "5-hour", "pct": pct_5h, "resets_at": None},
        {"label": "7-day", "pct": pct_7d, "resets_at": None},
    ], "spend": {}}


def test_normalize_thresholds(mod):
    """Aliases fold onto real labels; junk is dropped; defaults fill the rest."""
    t = mod.normalize_thresholds({"5h": 70, "seven_day": 95, "Opus 7d": "50",
                                  "bogus": "not-a-number"})
    assert t["5-hour"] == 70
    assert t["7-day"] == 95
    assert t["Opus 7d"] == 50          # numeric string coerces
    assert "bogus" not in t            # unparseable value dropped entirely
    assert t["default"] == mod.DEFAULT_ALERT_THRESHOLDS["default"]
    # No config at all ⇒ exactly the built-in defaults.
    assert mod.normalize_thresholds(None) == mod.DEFAULT_ALERT_THRESHOLDS
    assert mod.normalize_thresholds({}) == mod.DEFAULT_ALERT_THRESHOLDS


def test_threshold_for(mod):
    t = {"default": 80, "7-day": 90}
    assert mod.threshold_for("7-day", t) == 90
    assert mod.threshold_for("Sonnet 7d", t) == 80   # falls back to default
    assert mod.threshold_for("x", {}) == mod.DEFAULT_ALERT_THRESHOLDS["default"]


def test_evaluate_alerts_fires_at_threshold(mod):
    """Below ⇒ silence. At/above ⇒ exactly one alert, with the right numbers."""
    t = {"default": 80, "5-hour": 80, "7-day": 90}
    alerts, state = mod.evaluate_alerts(_snap(42.5, 88.0), t, {})
    assert alerts == [] and state == {}          # 42.5<80 and 88<90
    alerts, state = mod.evaluate_alerts(_snap(80.0, 88.0), t, {})
    assert [a["label"] for a in alerts] == ["5-hour"]   # boundary is inclusive
    assert alerts[0]["threshold"] == 80 and alerts[0]["pct"] == 80.0
    assert state == {"5-hour": {"threshold": 80, "ts": "2026-08-07T14:00:00+00:00"}}
    # Both over ⇒ both fire, in limit order.
    alerts, _ = mod.evaluate_alerts(_snap(95.0, 99.0), t, {})
    assert [a["label"] for a in alerts] == ["5-hour", "7-day"]


def test_evaluate_alerts_does_not_refire(mod):
    """A crossing fires once and stays quiet until the limit drops back."""
    t = {"default": 80}
    alerts, state = mod.evaluate_alerts(_snap(85.0, 10.0), t, {})
    assert len(alerts) == 1
    # Still elevated (and climbing) ⇒ no second alert, state unchanged.
    again, state2 = mod.evaluate_alerts(_snap(91.0, 10.0), t, state)
    assert again == [] and state2 == state
    # Limit resets below the threshold ⇒ state clears (re-armed)…
    cleared, state3 = mod.evaluate_alerts(_snap(3.0, 10.0), t, state2)
    assert cleared == [] and state3 == {}
    # …and the next crossing fires again.
    refired, _ = mod.evaluate_alerts(_snap(80.0, 10.0), t, state3)
    assert len(refired) == 1


def test_evaluate_alerts_rearms_on_threshold_change(mod):
    """Lowering the threshold under a still-elevated limit fires afresh."""
    _, state = mod.evaluate_alerts(_snap(85.0, 0.0), {"default": 80}, {})
    alerts, state2 = mod.evaluate_alerts(_snap(85.0, 0.0), {"default": 70}, state)
    assert [a["threshold"] for a in alerts] == [70]
    assert state2["5-hour"]["threshold"] == 70


def test_evaluate_alerts_cooldown(mod):
    """cooldown>0 re-nags a still-elevated limit only after it has elapsed."""
    t = {"default": 80}
    _, state = mod.evaluate_alerts(_snap(85.0, 0.0, "2026-08-07T14:00:00+00:00"),
                                   t, {}, cooldown=3600)
    # 30 min later: inside the cooldown, silent.
    early, _ = mod.evaluate_alerts(_snap(86.0, 0.0, "2026-08-07T14:30:00+00:00"),
                                   t, state, cooldown=3600)
    assert early == []
    # 61 min later: re-fires, and the state timestamp advances.
    late, state2 = mod.evaluate_alerts(_snap(86.0, 0.0, "2026-08-07T15:01:00+00:00"),
                                       t, state, cooldown=3600)
    assert len(late) == 1
    assert state2["5-hour"]["ts"] == "2026-08-07T15:01:00+00:00"


def test_evaluate_alerts_ignores_junk(mod):
    """Malformed limit entries are skipped, not crashed on."""
    snap = {"ts": "2026-08-07T14:00:00+00:00", "limits": [
        {"label": None, "pct": 99}, {"label": "x"}, {"pct": 99},
        {"label": "5-hour", "pct": 99.0}]}
    alerts, _ = mod.evaluate_alerts(snap, {"default": 80}, {})
    assert [a["label"] for a in alerts] == ["5-hour"]
    # No snapshot / no limits at all ⇒ empty, and state is left untouched.
    assert mod.evaluate_alerts({}, {"default": 80}, {"a": 1}) == ([], {"a": 1})
    # The caller's state dict is never mutated in place.
    st = {}
    mod.evaluate_alerts(_snap(99.0, 0.0), {"default": 80}, st)
    assert st == {}


def test_alert_message(mod):
    a = {"label": "7-day", "pct": 91.4, "threshold": 90, "resets_at": None}
    assert mod.alert_message(a) == "7-day usage at 91% (threshold 90%)"
    # With a reset time, the message says when it clears (frozen clock).
    a2 = dict(a, resets_at="2026-08-12T00:00:00Z")
    assert mod.alert_message(a2).startswith(
        "7-day usage at 91% (threshold 90%) — resets in 4d")


def test_resolve_notifier(mod):
    assert mod.resolve_notifier("auto", which=lambda p: "/usr/bin/notify-send") \
        == "notify-send"
    assert mod.resolve_notifier("auto", which=lambda p: None) == "stdout"
    assert mod.resolve_notifier("stdout", which=lambda p: "/x") == "stdout"
    assert mod.resolve_notifier("none", which=lambda p: "/x") == "none"
    assert mod.resolve_notifier("garbage", which=lambda p: None) == "stdout"


def test_dispatch_uses_mocked_runner(mod):
    """dispatch never shells out in tests: the runner is injected."""
    import io, contextlib
    alert = {"label": "5-hour", "pct": 96.0, "threshold": 80, "resets_at": None}
    seen = []

    def ok(a, text):
        seen.append((a["label"], text))
        return True
    assert mod.dispatch(alert, "notify-send", runner=ok) == "notify-send"
    assert seen[0][0] == "5-hour" and "96%" in seen[0][1]

    # A broken notifier degrades to stdout rather than raising.
    def boom(a, text):
        raise OSError("no dbus")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert mod.dispatch(alert, "notify-send", runner=boom) == "stdout"
    assert "5-hour usage at 96%" in buf.getvalue()

    # …unless stdout is suppressed (the live dashboard's fixed-height redraw).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert mod.dispatch(alert, "notify-send", runner=boom,
                            allow_stdout=False) == "none"
        assert mod.dispatch_quiet(alert, "stdout") == "none"
    assert buf.getvalue() == ""

    # 'none' suppresses entirely and never touches the runner.
    seen.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert mod.dispatch(alert, "none", runner=ok) == "none"
    assert seen == [] and buf.getvalue() == ""


def test_alert_state_roundtrip(mod):
    import tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "sub", "alerts.json")
        assert mod.read_alert_state(path) == {}       # missing file
        state = {"5-hour": {"threshold": 80, "ts": "2026-08-07T14:00:00+00:00"}}
        mod.write_alert_state(path, state)            # creates parent dirs
        assert mod.read_alert_state(path) == state
        # A corrupt state file degrades to "nothing fired", not a crash.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert mod.read_alert_state(path) == {}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_default_alert_state_path(mod):
    assert mod.default_alert_state_path("/a/b/history.jsonl") == "/a/b/alerts.json"
    assert mod.default_alert_state_path("history.jsonl") == "alerts.json"


def test_alert_settings_precedence(mod):
    """CLI > env > config for every alert setting; thresholds come from
    the config's [alert] table, and a blanket override replaces it wholesale."""
    cfg = {"alerts": True, "alert_notifier": "stdout", "alert_cooldown": 60,
           "alert": {"5h": 70, "7-day": 95}}
    r = mod.resolve_runtime_config({}, {}, cfg)
    assert r["alerts"] is True
    assert r["alert_notifier"] == "stdout"
    assert r["alert_cooldown"] == 60
    assert r["alert_thresholds"]["5-hour"] == 70
    assert r["alert_thresholds"]["7-day"] == 95
    # Defaults with no config at all.
    d = mod.resolve_runtime_config({}, {}, {})
    assert d["alerts"] is True and d["alert_notifier"] == "auto"
    assert d["alert_cooldown"] == 0
    assert d["alert_thresholds"] == mod.DEFAULT_ALERT_THRESHOLDS
    assert d["alert_state_path"] == mod.default_alert_state_path(d["history_path"])
    # Env beats config; CLI beats env. An invalid notifier falls back to auto.
    e = mod.resolve_runtime_config({}, {"alerts": "false",
                                        "alert_notifier": "bogus"}, cfg)
    assert e["alerts"] is False and e["alert_notifier"] == "auto"
    c = mod.resolve_runtime_config({"alerts": True, "alert_notifier": "none",
                                    "alert_threshold": 50},
                                   {"alerts": "false"}, cfg)
    assert c["alerts"] is True and c["alert_notifier"] == "none"
    assert c["alert_thresholds"] == {"default": 50}   # blanket wipes per-limit
    # An explicit state path wins and is ~-expanded.
    p = mod.resolve_runtime_config({"alert_state_path": "~/s.json"}, {}, {})
    assert p["alert_state_path"] == os.path.expanduser("~/s.json")


def test_process_alerts_persists_state(mod):
    """End-to-end: fires once, persists the crossing, stays quiet after."""
    import tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        cfg = {"alerts": True, "alert_thresholds": {"default": 80},
               "alert_notifier": "stdout", "alert_cooldown": 0,
               "alert_state_path": os.path.join(d, "alerts.json")}
        sent = []
        fired = mod.process_alerts(_snap(85.0, 10.0), cfg,
                                   lambda a, n: sent.append((a["label"], n)))
        assert [a["label"] for a in fired] == ["5-hour"]
        assert sent == [("5-hour", "stdout")]
        assert "5-hour" in mod.read_alert_state(cfg["alert_state_path"])
        # A second process (fresh call, state read from disk) doesn't re-fire.
        sent.clear()
        assert mod.process_alerts(_snap(87.0, 10.0), cfg,
                                  lambda a, n: sent.append(a)) == []
        assert sent == []
        # Alerting off ⇒ nothing evaluated, nothing dispatched.
        off = dict(cfg, alerts=False)
        assert mod.process_alerts(_snap(99.0, 99.0), off,
                                  lambda a, n: sent.append(a)) == []
        assert sent == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_run_once_exit_code_on_alert(mod):
    """--once returns 2 when an alert fires, 0 once the crossing is recorded."""
    import tempfile, shutil, io, contextlib
    d = tempfile.mkdtemp()
    try:
        mod.fetch = lambda cookie, org: DATA          # 5-hour 42.5%, 7-day 88%
        mod.fetch_bootstrap = lambda cookie: {"account": {"memberships": [
            {"organization": {"uuid": "ORG"}}]}}
        cfg = {"org": None, "persist": True,
               "history_path": os.path.join(d, "history.jsonl"),
               "history_max": 0, "alerts": True,
               "alert_thresholds": {"default": 80}, "alert_notifier": "stdout",
               "alert_cooldown": 0,
               "alert_state_path": os.path.join(d, "alerts.json")}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.run_once("cookie", cfg)
        assert rc == 2, rc                            # 7-day 88% >= 80
        assert "7-day usage at 88%" in buf.getvalue()
        # Same reading again: the crossing already fired, so a clean exit.
        with contextlib.redirect_stdout(io.StringIO()):
            assert mod.run_once("cookie", cfg) == 0
        # A fetch failure is still 1 — distinguishable from an alert.
        def boom(cookie, org):
            raise RuntimeError("network down")
        mod.fetch = boom
        assert mod.run_once("cookie", cfg) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── Phase 7: selectable color themes ──────────────────────────────────

# Hash of the full render matrix (both fixtures × every size, with history and
# trends on) under the frozen clock AND the UTC zone pinned at the top of this
# file. The default theme must reproduce it byte for byte — this is the
# no-visual-regression gate. If a deliberate change to the default palette or
# the renderer ever makes this fail, re-capture it in the same commit and say
# so in the message.
#
# Re-captured twice so far, both deliberate:
#   d7536a3 → e22bb27d…  original, from before the theme system existed
#   (pace)  → fe38adcb…  the pace tick, which adds a ┃ cell to every limit bar
#                        with a known reset window, plus an "N× pace" gloss on
#                        the reset line when it fits.
GOLDEN_DEFAULT_RENDER = \
    "fe38adcba1f8e2cf6e975737dfdbf3bdd874c576485327c91beaecec88051d44"


def _render_matrix_hash(mod):
    import hashlib
    blob = []
    for data in (DATA, LEGACY):
        for cols, rows in SIZES:
            mdef = mod.primary_metrics(data)
            blob.append("\n".join(mod.render(data, cols, rows, 120, mdef, HISTORY,
                                             None, True)))
    return hashlib.sha256("\x00".join(blob).encode()).hexdigest()


def test_default_theme_is_byte_identical(mod):
    """The default palette reproduces the pre-theme output exactly."""
    mod.apply_theme("claude")
    assert _render_matrix_hash(mod) == GOLDEN_DEFAULT_RENDER, (
        "default theme changed the rendered output")
    # apply_theme() defaults to the default theme, and reports what it applied.
    assert mod.apply_theme() == "claude"
    assert _render_matrix_hash(mod) == GOLDEN_DEFAULT_RENDER


def test_every_theme_renders_stably(mod):
    """Each theme renders the whole size matrix: right line count, no crash,
    deterministic, and structurally identical to the default (themes change
    color only — never layout)."""
    import re
    ansi = re.compile(r"\033\[[0-9;]*m")
    baselines = {}
    mod.apply_theme("claude")
    for data_name, data in (("DATA", DATA), ("LEGACY", LEGACY)):
        for cols, rows in SIZES:
            mdef = mod.primary_metrics(data)
            lines = mod.render(data, cols, rows, 120, mdef, HISTORY, None, True)
            baselines[(data_name, cols, rows)] = [ansi.sub("", l) for l in lines]

    for name in mod.THEMES:
        mod.apply_theme(name)
        for data_name, data in (("DATA", DATA), ("LEGACY", LEGACY)):
            for cols, rows in SIZES:
                mdef = mod.primary_metrics(data)
                lines = mod.render(data, cols, rows, 120, mdef, HISTORY, None, True)
                assert len(lines) == rows, f"{name} {cols}x{rows}"
                again = mod.render(data, cols, rows, 120, mdef, HISTORY, None, True)
                assert lines == again, f"{name} {cols}x{rows} not deterministic"
                assert [ansi.sub("", l) for l in lines] == \
                    baselines[(data_name, cols, rows)], \
                    f"{name} changed layout at {cols}x{rows}"
    mod.apply_theme("claude")


def test_alternative_themes_actually_differ(mod):
    """Every non-default theme produces visibly different output — a theme that
    silently aliased the default would pass every other test here."""
    seen = {}
    for name in mod.THEMES:
        mod.apply_theme(name)
        seen[name] = _render_matrix_hash(mod)
    mod.apply_theme("claude")
    assert len(set(seen.values())) == len(seen), f"themes collide: {seen}"
    assert seen["claude"] == GOLDEN_DEFAULT_RENDER
    # At least one cool/mono/high-contrast alternative exists, per the phase.
    assert {"cool", "mono", "contrast"} <= set(mod.THEMES)


def test_theme_definitions_are_complete(mod):
    """Every theme defines every palette key, so switching can't leave a
    color unbound (which would render as a literal empty string)."""
    required = {"brand", "text", "muted", "track", "on", "error", "sev", "heat"}
    for name, t in mod.THEMES.items():
        assert set(t) == required, f"{name}: {set(t) ^ required}"
        for key in ("brand", "text", "muted", "track", "on", "error"):
            assert len(t[key]) == 3 and all(0 <= c <= 255 for c in t[key]), \
                f"{name}.{key}"
        assert set(t["sev"]) == {"normal", "warning", "critical"}, name
        # The heat ramp must span 0..1 in ascending order so heat() can
        # interpolate across it without falling off either end.
        stops = [s for s, _ in t["heat"]]
        assert stops[0] == 0.0 and stops[-1] == 1.0, f"{name}: {stops}"
        assert stops == sorted(stops), f"{name}: {stops}"
        for _, c in t["heat"]:
            assert len(c) == 3 and all(0 <= v <= 255 for v in c), name


def test_heat_follows_active_theme(mod):
    """heat() reads the active theme's ramp, at the endpoints and between."""
    for name, t in mod.THEMES.items():
        mod.apply_theme(name)
        assert mod.heat(0) == t["heat"][0][1], name
        assert mod.heat(100) == t["heat"][-1][1], name
        assert mod.heat(150) == t["heat"][-1][1], name   # clamped
        assert mod.heat(-10) == t["heat"][0][1], name    # clamped
        mid = mod.heat(65)
        assert all(0 <= c <= 255 for c in mid), name
    mod.apply_theme("claude")
    # The default ramp's exact interpolation is unchanged.
    assert mod.heat(0) == (226, 184, 142)
    assert mod.heat(100) == (205, 72, 56)


def test_apply_theme_rejects_unknown(mod):
    """An unknown theme falls back to the default instead of raising — a stale
    config value must not stop the dashboard from starting."""
    assert mod.apply_theme("no-such-theme") == "claude"
    assert mod.THEME == "claude"
    assert _render_matrix_hash(mod) == GOLDEN_DEFAULT_RENDER
    assert mod.apply_theme(None) == "claude"
    mod.apply_theme("claude")


def test_theme_setting_precedence(mod):
    """CLI > env > config for --theme, with validation at every layer."""
    assert mod.resolve_runtime_config({}, {}, {})["theme"] == "claude"
    assert mod.resolve_runtime_config({}, {}, {"theme": "mono"})["theme"] == "mono"
    assert mod.resolve_runtime_config({}, {"theme": "cool"},
                                      {"theme": "mono"})["theme"] == "cool"
    assert mod.resolve_runtime_config({"theme": "contrast"}, {"theme": "cool"},
                                      {"theme": "mono"})["theme"] == "contrast"
    # Garbage at any layer degrades to the default rather than raising.
    assert mod.resolve_runtime_config({}, {}, {"theme": "bogus"})["theme"] == "claude"
    assert mod.resolve_runtime_config({}, {}, {"theme": 42})["theme"] == "claude"


# ── Phase 8: configurable sections + keyboard navigation ──────────────

# Every headline the registry can emit, so a rendered panel can be read back as
# a section list. `additional`/`extra_usage` need data that DATA alone lacks.
SECTION_HEADERS = {
    "limits": "LIMITS", "additional": "ADDITIONAL", "credits": "CREDITS",
    "extra_usage": "EXTRA USAGE", "sessions": "SESSIONS",
    "recent": "RECENT CHATS", "account": "ACCOUNT",
    "connectors": "CONNECTORS", "cowork": "COWORK",
}

# DATA plus the two payload-driven optional sections.
RICH = dict(DATA, extra_usage={"is_enabled": True, "used_credits": 3},
            cowork_usage={"utilization": 12.0})


def _panel(mod, data, sections=None, cols=100, rows=40, history=None):
    """Render the big panel with the BOOT/LIVE-backed sections neutralized: the
    standalone runner shares one module instance across tests, so an earlier
    test's globals must not leak extra sections into this one."""
    mod.BOOT, mod.LIVE = None, {}
    mdef = mod.primary_metrics(data)
    return mod.render(data, cols, rows, 120, mdef,
                      HISTORY if history is None else history, sections)


def _headers(mod, lines):
    """The section names visible in a rendered panel, in render order."""
    seen = []
    for line in (l.strip() for l in _strip(lines)):
        for name, head in SECTION_HEADERS.items():
            # SESSIONS carries a live count — "SESSIONS (2)".
            if line == head or line.startswith(head + " ("):
                seen.append(name)
    return seen


def test_default_sections_render_like_before(mod):
    """Passing the default order explicitly is identical to passing nothing —
    the registry refactor is a pure restructure of the old hardcoded layout."""
    implicit = _panel(mod, RICH)
    explicit = _panel(mod, RICH, mod.DEFAULT_SECTIONS)
    assert implicit == explicit
    assert set(mod.DEFAULT_SECTIONS) == set(mod.PANEL_SECTIONS), (
        "every registered section must appear in the default order")


def test_sections_select_and_order_the_panel(mod):
    """A `sections` list produces exactly those sections, in that order."""
    for want in (["credits", "limits"],
                 ["limits"],
                 ["extra_usage", "credits", "additional", "limits"]):
        lines = _panel(mod, RICH, want)
        assert _headers(mod, lines) == want, want
        assert len(lines) == 40


def test_sections_omit_empty_and_unknown(mod):
    """An empty section (no data) contributes nothing — not a stray blank
    heading — and an unknown name is skipped rather than raising."""
    # DATA carries no extra_usage payload, and BOOT/LIVE are empty.
    lines = _panel(mod, DATA, ["limits", "extra_usage", "account", "credits"])
    assert _headers(mod, lines) == ["limits", "credits"]
    # An unrecognized name in the render list is ignored, not fatal.
    lines = _panel(mod, DATA, ["limits", "nope"])
    assert _headers(mod, lines) == ["limits"]


def test_normalize_sections(mod):
    d = tuple(mod.DEFAULT_SECTIONS)
    assert mod.normalize_sections(None) == d
    assert mod.normalize_sections([]) == d            # empty ⇒ fall back
    assert mod.normalize_sections("bogus, nope") == d  # all-unknown ⇒ fall back
    assert mod.normalize_sections(42) == d             # wrong type ⇒ fall back
    assert mod.normalize_sections("credits,limits") == ("credits", "limits")
    assert mod.normalize_sections(" Credits  limits ") == ("credits", "limits")
    assert mod.normalize_sections(["limits", "limits"]) == ("limits",)  # dedup
    assert mod.normalize_sections(["limits", "junk", 7]) == ("limits",)


def test_sections_setting_precedence(mod):
    assert mod.resolve_runtime_config({}, {}, {})["sections"] == \
        tuple(mod.DEFAULT_SECTIONS)
    assert mod.resolve_runtime_config({}, {}, {"sections": ["limits", "credits"]}
                                      )["sections"] == ("limits", "credits")
    assert mod.resolve_runtime_config({}, {"sections": "credits"},
                                      {"sections": ["limits"]}
                                      )["sections"] == ("credits",)
    assert mod.resolve_runtime_config({"sections": "account"},
                                      {"sections": "credits"},
                                      {"sections": ["limits"]}
                                      )["sections"] == ("account",)
    # Garbage anywhere degrades to the full default panel.
    assert mod.resolve_runtime_config({}, {}, {"sections": ["nope"]}
                                      )["sections"] == tuple(mod.DEFAULT_SECTIONS)


def test_initial_key_state(mod):
    cfg = mod.resolve_runtime_config({}, {}, {"sections": ["limits", "credits"],
                                              "theme": "mono"})
    st = mod.initial_key_state(cfg)
    assert st["running"] and st["force"]        # first tick fetches immediately
    assert st["theme"] == "mono"
    assert st["sections"] == ("limits", "credits")
    assert st["hidden"] == frozenset() and st["compact"] is False


def test_handle_key_refresh_and_quit(mod):
    st = mod.initial_key_state({})
    for key in ("r", "R", " "):
        assert mod.handle_key(st, key)["force"] is True, key
    for key in ("q", "Q", "\x03", "\x04"):
        assert mod.handle_key(st, key)["running"] is False, key
    # Unknown keys are inert, and clear a stale force flag.
    after = mod.handle_key(mod.handle_key(st, "r"), "z")
    assert after["force"] is False and after["running"] is True


def test_handle_key_is_pure(mod):
    st = mod.initial_key_state({})
    before = dict(st)
    for key in ("t", "s", "1", "q", " "):
        out = mod.handle_key(st, key)
        assert st == before, f"handle_key mutated its input on {key!r}"
        assert out is not st


def test_handle_key_cycles_theme(mod):
    order = sorted(mod.THEMES)
    st = mod.initial_key_state({"theme": order[0]})
    seen = []
    for _ in order:
        st = mod.handle_key(st, "t")
        seen.append(st["theme"])
    assert seen == order[1:] + [order[0]], seen  # wraps around
    # An unknown current theme starts the cycle rather than raising.
    assert mod.handle_key({"theme": "gone"}, "t")["theme"] == order[0]


def test_handle_key_toggles_sections(mod):
    st = mod.initial_key_state({"sections": ["limits", "credits", "account"]})
    assert mod.visible_sections(st) == ("limits", "credits", "account")

    st = mod.handle_key(st, "2")                     # hide the 2nd section
    assert st["hidden"] == frozenset({"credits"})
    assert mod.visible_sections(st) == ("limits", "account")
    st = mod.handle_key(st, "2")                     # …and show it again
    assert mod.visible_sections(st) == ("limits", "credits", "account")
    # Out-of-range digits are inert; the configured order is never rewritten.
    st = mod.handle_key(st, "9")
    assert st["hidden"] == frozenset()
    assert st["sections"] == ("limits", "credits", "account")


def test_handle_key_compact_toggle(mod):
    st = mod.initial_key_state({})
    st = mod.handle_key(st, "s")
    assert st["compact"] is True
    assert mod.visible_sections(st) == tuple(mod.CORE_SECTIONS)
    assert mod.handle_key(st, "s")["compact"] is False
    # Compact and per-section hiding compose.
    both = mod.handle_key(st, "1")
    assert "limits" not in mod.visible_sections(both)


def test_visible_sections_drives_the_panel(mod):
    """The whole key→panel path: toggles change which sections actually render."""
    st = mod.initial_key_state({"sections": ["limits", "credits", "additional"]})
    lines = _panel(mod, RICH, mod.visible_sections(st))
    assert _headers(mod, lines) == ["limits", "credits", "additional"]
    st = mod.handle_key(st, "2")
    lines = _panel(mod, RICH, mod.visible_sections(st))
    assert _headers(mod, lines) == ["limits", "additional"]
    assert len(lines) == 40


# ── Pace tick (replaces the trend sparkline as the default) ───────────

def _at(mod, iso):
    """Parse an ISO stamp with the module's frozen clock semantics."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_window_seconds(mod):
    assert mod.window_seconds("session") == 5 * 3600
    assert mod.window_seconds("five_hour") == 5 * 3600
    assert mod.window_seconds("weekly_all") == 7 * 86400
    assert mod.window_seconds("seven_day") == 7 * 86400
    # Unknown weekly_* buckets (the API keeps adding scoped ones) are weekly.
    assert mod.window_seconds("weekly_opus_4_5") == 7 * 86400
    assert mod.window_seconds("something_else") is None
    assert mod.window_seconds(None) is None


def test_pace_pct(mod):
    now = _at(mod, "2026-08-07T14:30:15Z")
    # 5h window resetting at 18:00 ⇒ 3h29m45s remain ⇒ 1h30m15s elapsed.
    p = mod.pace_pct("2026-08-07T18:00:00Z", "session", now)
    assert 30.0 < p < 30.1, p
    # Exactly one full window left ⇒ just started.
    assert mod.pace_pct("2026-08-07T19:30:15Z", "session", now) == 0.0
    # Unknowable cases all yield None rather than a bogus tick.
    assert mod.pace_pct(None, "session", now) is None
    assert mod.pace_pct("2026-08-07T18:00:00Z", "mystery", now) is None
    assert mod.pace_pct("not-a-date", "session", now) is None
    assert mod.pace_pct("2026-08-07T13:00:00Z", "session", now) is None  # past
    # A reset further out than a whole window means stale/mis-kinded data.
    assert mod.pace_pct("2026-08-09T00:00:00Z", "session", now) is None


def test_pace_note(mod):
    assert mod.pace_note(70.0, 35.0) == "2.0× pace"   # burning double
    assert mod.pace_note(20.0, 40.0) == "0.5× pace"   # coasting
    assert mod.pace_note(40.0, 40.0) == "on pace"
    assert mod.pace_note(43.0, 40.0) == "on pace"     # inside the dead band
    assert mod.pace_note(50.0, None) is None
    # Just after a reset the ratio is meaningless, so it's suppressed.
    assert mod.pace_note(2.0, 1.0) is None


def test_progress_bar_draws_the_tick(mod):
    plain = lambda s: _strip([s])[0]
    bare = plain(mod.progress_bar(50.0, 40))
    assert mod.PACE_GLYPH not in bare          # no mark ⇒ unchanged bar
    assert len(bare) == 40
    for mark in (0.0, 25.0, 50.0, 99.9, 100.0):
        marked = plain(mod.progress_bar(50.0, 40, mark))
        assert marked.count(mod.PACE_GLYPH) == 1, mark
        assert len(marked) == 40, mark        # the tick replaces a cell
    # The tick lands where the percentage says it should.
    assert plain(mod.progress_bar(50.0, 40, 25.0)).index(mod.PACE_GLYPH) == 10
    assert plain(mod.progress_bar(50.0, 40, 75.0)).index(mod.PACE_GLYPH) == 30
    # Out-of-range marks are clamped into the bar, never dropped or overflowed.
    for mark in (-10.0, 250.0):
        marked = plain(mod.progress_bar(50.0, 40, mark))
        assert marked.count(mod.PACE_GLYPH) == 1 and len(marked) == 40


def test_panel_shows_pace_by_default(mod):
    """No history, no config: the tick and its gloss are simply there."""
    lines = _panel(mod, DATA, history=[])
    plain = "\n".join(_strip(lines))
    assert mod.PACE_GLYPH in plain
    assert "pace" in plain
    assert "trend " not in plain, "sparkline should be off by default"


def test_pace_gloss_dropped_when_it_would_overflow(mod):
    """The gloss is the lowest-priority thing on the reset row: on a narrow
    panel it's dropped rather than wrapped (which would break the layout)."""
    mdef = mod.primary_metrics(DATA)
    for cols in (46, 52, 60, 80, 100):
        lines = _strip(mod.render(DATA, cols, 40, 120, mdef))
        assert max(len(l) for l in lines) <= cols, f"overflow at cols={cols}"


def test_pace_absent_when_unknowable(mod):
    """A limit with no reset time gets no tick — and nothing crashes."""
    data = {"limits": [{"kind": "session", "percent": 42.5, "is_active": True}],
            "spend": {}}
    plain = "\n".join(_strip(_panel(mod, data, history=[])))
    assert mod.PACE_GLYPH not in plain
    assert "pace" not in plain


def test_trends_setting_precedence(mod):
    rc = mod.resolve_runtime_config
    assert rc({}, {}, {})["trends"] is False          # off by default now
    assert rc({}, {}, {"trends": True})["trends"] is True
    assert rc({}, {"trends": "1"}, {"trends": False})["trends"] is True
    assert rc({"trends": True}, {"trends": "0"}, {})["trends"] is True
    assert rc({}, {}, {"trends": "garbage"})["trends"] is False


def test_primary_metrics_carries_kind(mod):
    """The small layouts need `kind` to place their tick, so it rides along in
    the metric tuple."""
    for m in mod.primary_metrics(DATA):
        assert len(m) == 5, m
    kinds = {m[4] for m in mod.primary_metrics(DATA)}
    assert kinds == {"session", "weekly_all"}, kinds
    assert {m[4] for m in mod.primary_metrics(LEGACY)} == {"five_hour",
                                                           "seven_day"}


# ── Context-window usage from local Claude Code transcripts ───────────

def _write_transcript(path, usage, cwd="/home/u/proj", branch="main",
                      sid="abcd1234-0000", pad=0):
    """A miniature Claude Code transcript: some noise, optional padding to push
    the head out of the tail window, then an assistant record carrying usage."""
    import json as _json
    with open(path, "w") as fh:
        fh.write(_json.dumps({"type": "custom-title", "sessionId": sid,
                              "customTitle": "Fancy Title"}) + "\n")
        for i in range(pad):
            fh.write(_json.dumps({"type": "system", "text": "x" * 400,
                                  "n": i}) + "\n")
        fh.write(_json.dumps({"type": "user", "cwd": cwd, "sessionId": sid,
                              "gitBranch": branch}) + "\n")
        if usage is not None:
            fh.write(_json.dumps({"type": "assistant", "cwd": cwd,
                                  "sessionId": sid, "gitBranch": branch,
                                  "message": {"model": "claude-opus-5",
                                              "usage": usage}}) + "\n")


USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 743,
         "cache_read_input_tokens": 159646, "output_tokens": 157}


def test_context_tokens(mod):
    """Cached tokens still occupy the window; output tokens do not."""
    assert mod.context_tokens(USAGE) == 2 + 743 + 159646
    assert mod.context_tokens({"input_tokens": 10}) == 10
    # Missing counters are tolerated; a block with none of them is unusable.
    assert mod.context_tokens({"output_tokens": 5}) is None
    assert mod.context_tokens({}) is None
    assert mod.context_tokens(None) is None
    assert mod.context_tokens("nope") is None
    assert mod.context_tokens({"input_tokens": True}) is None  # bool ≠ count


def test_read_session_context(mod):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "abcd1234-0000.jsonl")
        _write_transcript(path, USAGE)
        rec = mod.read_session_context(path, window=200_000)
        assert rec["tokens"] == 160391
        assert abs(rec["pct"] - 80.1955) < 0.01
        assert rec["label"] == "proj"          # cwd basename, not the title
        assert rec["branch"] == "main"
        assert rec["id"] == "abcd1234-0000"
        # A transcript with no usage record yields nothing rather than a zero.
        empty = os.path.join(d, "empty.jsonl")
        _write_transcript(empty, None)
        assert mod.read_session_context(empty) is None
        # Unreadable / absent files are skipped, not fatal.
        assert mod.read_session_context(os.path.join(d, "nope.jsonl")) is None
        # Garbage lines don't stop the scan.
        junk = os.path.join(d, "junk.jsonl")
        with open(junk, "a") as fh:
            fh.write("{not json\n")
        _write_transcript(junk, USAGE)
        assert mod.read_session_context(junk)["tokens"] == 160391


def test_read_session_context_tail_only(mod):
    """Transcripts run to megabytes, so only the tail is parsed — the reading
    must still be correct when the head is far outside the window."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "big.jsonl")
        _write_transcript(path, USAGE, pad=3000)   # ≫ CONTEXT_TAIL_BYTES
        assert os.path.getsize(path) > mod.CONTEXT_TAIL_BYTES
        rec = mod.read_session_context(path)
        assert rec is not None and rec["tokens"] == 160391
        assert rec["label"] == "proj"


def test_context_pct_is_capped(mod):
    """A window smaller than the reading can't produce a >100% bar."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.jsonl")
        _write_transcript(path, USAGE)
        assert mod.read_session_context(path, window=1000)["pct"] == 100.0


def test_scan_local_sessions(mod):
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        for i, name in enumerate(("proj-a", "proj-b")):
            os.makedirs(os.path.join(root, name))
            for j in range(2):
                pth = os.path.join(root, name, f"sess{i}{j}-1111.jsonl")
                _write_transcript(pth, dict(USAGE, input_tokens=i * 10 + j),
                                  cwd=f"/home/u/{name}", sid=f"sess{i}{j}-1111")
                os.utime(pth, (1_700_000_000 + i * 10 + j,) * 2)
        out = mod.scan_local_sessions(root, limit=10)
        assert len(out) == 4
        # Newest first.
        assert [r["mtime"] for r in out] == sorted(
            (r["mtime"] for r in out), reverse=True)
        assert {r["label"] for r in out} == {"proj-a", "proj-b"}
        # `limit` caps how many files are parsed at all.
        assert len(mod.scan_local_sessions(root, limit=2)) == 2
        # `max_age` drops stale transcripts (these are from 2023).
        assert mod.scan_local_sessions(root, limit=10, max_age=60) == []
        # A missing root is empty, not an error.
        assert mod.scan_local_sessions(os.path.join(root, "gone")) == []


def test_fmt_tokens(mod):
    assert mod.fmt_tokens(0) == "0"
    assert mod.fmt_tokens(999) == "999"
    assert mod.fmt_tokens(160391) == "160k"
    assert mod.fmt_tokens(1_240_000) == "1.2M"
    assert mod.fmt_tokens(None) == "0"


def test_context_section_renders(mod):
    mod.LIVE = {"context": [
        {"id": "aaaa1111", "label": "proj", "tokens": 160391, "window": 200000,
         "pct": 80.2, "branch": "main", "mtime": 0},
        {"id": "bbbb2222", "label": "proj", "tokens": 20000, "window": 200000,
         "pct": 10.0, "branch": "", "mtime": 0},
    ]}
    try:
        lines = _strip(mod.context_section(mod._panel_ctx({}, 72)))
        assert lines[0].strip() == "CONTEXT"
        assert "160k/200k" in lines[1] and "80%" in lines[2]
        # Identical project names are disambiguated by session id.
        assert lines[1].startswith("proj aaaa") and lines[3].startswith("proj bbbb")
        for l in lines:
            assert len(l) <= 72, l
        # Narrow panels drop the bar rather than overflowing.
        for cols in (46, 52, 60, 100):
            for l in _strip(mod.context_section(mod._panel_ctx({}, cols))):
                assert len(l) <= cols, (cols, l)
    finally:
        mod.LIVE = {}


def test_context_section_is_empty_without_live_data(mod):
    """Rendering never touches the disk — the panel redraws several times a
    second, so the scan happens on the extras clock in main() instead."""
    mod.LIVE = {}
    assert mod.context_section(mod._panel_ctx({}, 80)) == []
    mod.LIVE = {"context": []}
    assert mod.context_section(mod._panel_ctx({}, 80)) == []


def test_context_settings_precedence(mod):
    rc = mod.resolve_runtime_config
    d = rc({}, {}, {})
    assert d["context_window"] == mod.DEFAULT_CONTEXT_WINDOW
    assert d["context_sessions"] == 3
    assert d["context_max_age"] == 24 * 3600
    assert rc({}, {}, {"context_window": 1_000_000})["context_window"] == 1_000_000
    assert rc({}, {"context_sessions": "5"}, {})["context_sessions"] == 5
    # 0 sessions is a meaningful "off", not "unset".
    assert rc({}, {}, {"context_sessions": 0})["context_sessions"] == 0
    # Garbage falls through to the default rather than crashing the loop.
    assert rc({}, {}, {"context_window": "junk"})["context_window"] == \
        mod.DEFAULT_CONTEXT_WINDOW


def main():
    mod = load_module()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t(mod)
            print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001 — test harness reports all failures
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
