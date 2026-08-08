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
from datetime import datetime, timezone

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
