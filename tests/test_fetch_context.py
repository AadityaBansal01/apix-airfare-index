"""Tests for the guarded fetch path.

The claim under test is the one the whole project's defensibility rests on:
a source cannot reach a URL that robots.txt forbids, and everything it does
reach leaves an audit row.
"""
from __future__ import annotations

import asyncio

import pytest

from apix.ethics.audit import AuditRecord, NullAuditWriter
from apix.net.identity import Identity, IdentityMode
from apix.net.session import (
    FetchFailed,
    PolicyEnforcedSession,
    PolicyRefused,
    SourcePolicy,
    load_policies,
)
from apix.net.throttle import ThrottlePolicy
from apix.settings import Settings

pytestmark = pytest.mark.asyncio

AKASA_ROBOTS = b"""User-Agent: *
Sitemap: https://www.akasaair.com/sitemap.xml
"""

INDIGO_ROBOTS = b"""User-Agent: *
Disallow: /booking/
Disallow: /book/
"""

YATRA_ROBOTS = b"""User-agent: *
Allow: /

User-agent: APIx-Research-Bot
Allow: /
Crawl-delay: 5
"""


def build(transport, throttler, *, policies=None, settings=None):
    audit = NullAuditWriter()
    s = PolicyEnforcedSession(
        audit,
        identity=Identity(IdentityMode.IDENTIFIED),
        throttler=throttler,
        transport=transport,
        policies=policies or {},
        settings=settings or Settings(store_raw_bodies=False, max_retries=0),
    )
    return s, audit


# --------------------------------------------------------------------------
# The core guarantee
# --------------------------------------------------------------------------

async def test_allowed_url_is_fetched_and_audited(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/flight-booking/delhi-to-mumbai",
                       body=b"<html>ok</html>")
    s, audit = build(fake_transport, instant_throttler)

    r = await s.get("https://www.akasaair.com/flight-booking/delhi-to-mumbai",
                    source_code="akasa")
    assert r.status == 200
    assert "ok" in r.text

    fetches = [a for a in audit.records if a.source_code == "akasa"]
    assert len(fetches) == 1
    assert fetches[0].outcome == "ok"
    assert fetches[0].robots_allowed is True
    assert fetches[0].bytes_returned == len(b"<html>ok</html>")


async def test_disallowed_url_is_never_sent(fake_transport, instant_throttler):
    """The load-bearing test: refusal must prevent egress, not just log it."""
    fake_transport.add("https://www.goindigo.in/robots.txt", body=INDIGO_ROBOTS)
    fake_transport.add("https://www.goindigo.in/booking/search", body=b"fares!")
    s, audit = build(fake_transport, instant_throttler)

    with pytest.raises(PolicyRefused) as exc:
        await s.get("https://www.goindigo.in/booking/search", source_code="indigo")
    assert exc.value.intent_denied is False

    # The forbidden URL must not appear in transport egress at all.
    assert "https://www.goindigo.in/booking/search" not in fake_transport.requests
    assert fake_transport.requests == ["https://www.goindigo.in/robots.txt"]

    blocked = [a for a in audit.records if a.outcome == "blocked_by_robots"]
    assert len(blocked) == 1
    assert blocked[0].robots_allowed is False


async def test_intent_denied_path_blocked_even_when_robots_allows(
    fake_transport, instant_throttler
):
    """SpiceJet's malformed absolute-URL Disallow lines are honoured anyway."""
    fake_transport.add("https://www.spicejet.com/robots.txt",
                       body=b"User-agent: *\nDisallow:\n")
    fake_transport.add("https://www.spicejet.com/api/v1/search", body=b"{}")
    policies = {"spicejet": SourcePolicy("spicejet", 6.0, 2.0, ("/api/v1", "/public/"))}
    s, audit = build(fake_transport, instant_throttler, policies=policies)

    with pytest.raises(PolicyRefused) as exc:
        await s.get("https://www.spicejet.com/api/v1/search", source_code="spicejet")
    assert exc.value.intent_denied is True

    assert "https://www.spicejet.com/api/v1/search" not in fake_transport.requests
    rec = [a for a in audit.records if a.outcome == "blocked_by_intent"][0]
    assert rec.intent_denied is True
    assert "intent_denied:/api/v1" in rec.robots_rule


async def test_intent_check_runs_without_fetching_robots(
    fake_transport, instant_throttler
):
    """An intent-denied path is refused before robots.txt is even requested."""
    policies = {"spicejet": SourcePolicy("spicejet", 6.0, 2.0, ("/api/v1",))}
    s, _ = build(fake_transport, instant_throttler, policies=policies)
    with pytest.raises(PolicyRefused):
        await s.get("https://www.spicejet.com/api/v1/x", source_code="spicejet")
    assert fake_transport.requests == []


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------

async def test_unreachable_robots_fails_closed(fake_transport, instant_throttler):
    """No robots.txt means no crawling. The opposite default is the common one
    and it is wrong for a system whose defensibility is the point."""
    fake_transport.add("https://www.example.com/robots.txt", status=None,
                       error="connection reset")
    s, _ = build(fake_transport, instant_throttler)
    with pytest.raises(PolicyRefused):
        await s.get("https://www.example.com/flights", source_code="akasa")
    assert "https://www.example.com/flights" not in fake_transport.requests


async def test_server_error_on_robots_fails_closed(fake_transport, instant_throttler):
    fake_transport.add("https://www.example.com/robots.txt", status=503)
    s, _ = build(fake_transport, instant_throttler)
    with pytest.raises(PolicyRefused):
        await s.get("https://www.example.com/x", source_code="akasa")


async def test_missing_robots_is_permissive_per_rfc(fake_transport, instant_throttler):
    """RFC 9309: 404 means no restrictions. Distinct from unreachable."""
    fake_transport.add("https://www.example.com/robots.txt", status=404)
    fake_transport.add("https://www.example.com/x", body=b"page")
    s, _ = build(fake_transport, instant_throttler)
    r = await s.get("https://www.example.com/x", source_code="akasa")
    assert r.status == 200


async def test_403_on_robots_disallows_everything(fake_transport, instant_throttler):
    fake_transport.add("https://www.example.com/robots.txt", status=403)
    s, _ = build(fake_transport, instant_throttler)
    with pytest.raises(PolicyRefused):
        await s.get("https://www.example.com/x", source_code="akasa")


# --------------------------------------------------------------------------
# Politeness arithmetic
# --------------------------------------------------------------------------

async def test_crawl_delay_raises_our_floor_but_never_lowers_it(
    fake_transport, instant_throttler
):
    """Yatra publishes Crawl-delay: 5. Our floor is 6, so 6 wins."""
    fake_transport.add("https://www.yatra.com/robots.txt", body=YATRA_ROBOTS)
    fake_transport.add("https://www.yatra.com/x", body=b"ok")
    s, _ = build(fake_transport, instant_throttler)
    await s.get("https://www.yatra.com/x", source_code="yatra")

    page_wait = [w for u, w in instant_throttler.waits if u.endswith("/x")][0]
    assert page_wait >= 6.0


async def test_site_crawl_delay_above_our_floor_is_honoured():
    from apix.net.throttle import ThrottlePolicy
    import random
    pol = ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0)
    assert pol.effective_delay(30.0, random.Random(1)) == 30.0
    assert pol.effective_delay(2.0, random.Random(1)) == 6.0
    assert pol.effective_delay(None, random.Random(1)) == 6.0


async def test_jitter_only_adds_never_subtracts():
    from apix.net.throttle import ThrottlePolicy
    import random
    pol = ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=2.0)
    rng = random.Random(0)
    for _ in range(200):
        assert 6.0 <= pol.effective_delay(None, rng) <= 8.0


async def test_per_source_delay_override_is_used(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/x", body=b"ok")
    policies = {"akasa": SourcePolicy("akasa", min_delay_seconds=8.0, jitter_seconds=0.0)}
    s, _ = build(fake_transport, instant_throttler, policies=policies)
    await s.get("https://www.akasaair.com/x", source_code="akasa")
    page_wait = [w for u, w in instant_throttler.waits if u.endswith("/x")][0]
    assert page_wait == 8.0


# --------------------------------------------------------------------------
# Auditing completeness
# --------------------------------------------------------------------------

async def test_robots_fetch_is_itself_audited(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/x", body=b"ok")
    s, audit = build(fake_transport, instant_throttler)
    await s.get("https://www.akasaair.com/x", source_code="akasa")

    robots_rows = [a for a in audit.records if a.source_code == "__robots__"]
    assert len(robots_rows) == 1
    assert robots_rows[0].url.endswith("/robots.txt")
    assert robots_rows[0].robots_allowed is True


async def test_snapshot_saved_with_hash(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/x", body=b"ok")
    s, audit = build(fake_transport, instant_throttler)
    await s.get("https://www.akasaair.com/x", source_code="akasa")

    assert len(audit.snapshots) == 1
    code, snap = audit.snapshots[0]
    assert code == "akasa"
    assert snap.body.startswith("User-Agent: *")
    assert len(snap.sha256) == 32          # sha256 digest
    assert snap.snapshot_id is not None


async def test_http_error_is_audited_then_raised(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/gone", status=404, body=b"nope")
    s, audit = build(fake_transport, instant_throttler)

    with pytest.raises(FetchFailed):
        await s.get("https://www.akasaair.com/gone", source_code="akasa")
    errs = [a for a in audit.records if a.outcome == "http_error"]
    assert len(errs) == 1
    assert errs[0].http_status == 404


async def test_every_retry_gets_its_own_audit_row(fake_transport, instant_throttler):
    """Retry traffic is visible in the trail rather than hidden in one record."""
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/flaky", status=503, body=b"")
    s, audit = build(fake_transport, instant_throttler,
                     settings=Settings(store_raw_bodies=False, max_retries=2,
                                       min_delay_seconds=0.0))
    with pytest.raises(FetchFailed):
        await s.get("https://www.akasaair.com/flaky", source_code="akasa")

    rows = [a for a in audit.records if a.url.endswith("/flaky")]
    assert len(rows) == 3          # initial + 2 retries
    assert all(r.outcome == "http_error" for r in rows)


async def test_4xx_is_not_retried(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/missing", status=404, body=b"")
    s, audit = build(fake_transport, instant_throttler,
                     settings=Settings(store_raw_bodies=False, max_retries=3,
                                       min_delay_seconds=0.0))
    with pytest.raises(FetchFailed):
        await s.get("https://www.akasaair.com/missing", source_code="akasa")
    rows = [a for a in audit.records if a.url.endswith("/missing")]
    assert len(rows) == 1


async def test_429_is_recorded_as_rate_limited(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/busy", status=429, body=b"")
    s, audit = build(fake_transport, instant_throttler,
                     settings=Settings(store_raw_bodies=False, max_retries=0,
                                       min_delay_seconds=0.0))
    with pytest.raises(FetchFailed):
        await s.get("https://www.akasaair.com/busy", source_code="akasa")
    assert "rate_limited" in [a.outcome for a in audit.records]


# --------------------------------------------------------------------------
# Robots caching
# --------------------------------------------------------------------------

async def test_robots_fetched_once_per_origin(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    for i in range(4):
        fake_transport.add(f"https://www.akasaair.com/p{i}", body=b"ok")
    s, _ = build(fake_transport, instant_throttler)
    for i in range(4):
        await s.get(f"https://www.akasaair.com/p{i}", source_code="akasa")

    robots_hits = [u for u in fake_transport.requests if u.endswith("/robots.txt")]
    assert len(robots_hits) == 1


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

async def test_identified_mode_sends_contactable_ua(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/x", body=b"ok")
    s, audit = build(fake_transport, instant_throttler)
    await s.get("https://www.akasaair.com/x", source_code="akasa")
    ua = [a for a in audit.records if a.source_code == "akasa"][0].user_agent
    assert "APIx-Research-Bot" in ua
    assert "+https://" in ua


async def test_rotating_mode_still_checks_robots_as_the_bot():
    """We never gain access by pretending to be a browser."""
    ident = Identity(IdentityMode.ROTATING, seed=1)
    assert ident.robots_token == "APIx-Research-Bot"
    assert "APIx-Research-Bot" not in ident.user_agent()


# --------------------------------------------------------------------------
# Config wiring
# --------------------------------------------------------------------------

async def test_load_policies_excludes_disabled_and_high_tier_sources():
    pol = load_policies("config/sources.yaml")
    assert "akasa" in pol
    for excluded in ("indigo", "makemytrip", "airindia", "yatra", "goibibo"):
        assert excluded not in pol, f"{excluded} must not receive a fetch policy"


async def test_akasa_policy_matches_config():
    pol = load_policies("config/sources.yaml")["akasa"]
    assert pol.min_delay_seconds == 8.0
    assert "/payment" in pol.intent_denied_paths


# --------------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------------

async def test_redirect_to_a_disallowed_url_is_refused(fake_transport, instant_throttler):
    """Regression. The transport follows redirects, so a robots-allowed URL
    could land on a disallowed one and hand back its body, while the audit log
    recorded a clean fetch of the allowed path. That hid which URL was actually
    read and defeated the compliance guarantee entirely."""
    from apix.net.transport import RawResponse

    fake_transport.add("https://www.akasaair.com/robots.txt",
                       body=b"User-agent: *\nAllow: /start\nDisallow: /forbidden\n")
    # The transport reports a different final_url, as a real redirect would.
    fake_transport.routes["https://www.akasaair.com/start"] = RawResponse(
        status=200, body=b"SECRET", final_url="https://www.akasaair.com/forbidden")

    s, audit = build(fake_transport, instant_throttler)
    with pytest.raises(PolicyRefused, match="redirected"):
        await s.get("https://www.akasaair.com/start", source_code="akasa")

    blocked = [a for a in audit.records if a.outcome == "blocked_by_robots"]
    assert len(blocked) == 1
    assert blocked[0].url.endswith("/forbidden"), (
        "the audit must name the URL actually landed on, not the one requested")


async def test_redirect_within_allowed_paths_still_succeeds(
    fake_transport, instant_throttler
):
    from apix.net.transport import RawResponse

    fake_transport.add("https://www.akasaair.com/robots.txt",
                       body=b"User-agent: *\nDisallow: /forbidden\n")
    fake_transport.routes["https://www.akasaair.com/a"] = RawResponse(
        status=200, body=b"ok", final_url="https://www.akasaair.com/b")

    s, _ = build(fake_transport, instant_throttler)
    r = await s.get("https://www.akasaair.com/a", source_code="akasa")
    assert r.status == 200
    assert r.url.endswith("/b"), "the response must report the URL actually read"
