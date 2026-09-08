"""PolicyEnforcedSession: the only door to the network.

Every request a source makes passes through `_guarded`, which runs a fixed
sequence and cannot be reordered or short-circuited by a caller:

    1. robots + intent check   -> refuse and audit if denied
    2. rate-limit wait          -> honour max(our floor, site Crawl-delay) + jitter
    3. fetch                    -> transport or browser
    4. audit row                -> written in `finally`, so a crash still records

The audit row is written even when step 1 refuses, which is the point: the trail
shows what we declined to fetch, not merely what we fetched.

RENDERING AND SUBRESOURCES
--------------------------
A browser render fires many requests beyond the one we asked for. Each is
intercepted:

*   **robots.txt is checked for every one of them.** A disallowed subresource is
    aborted, not merely unrecorded.
*   **Third-party trackers and ad beacons are blocked outright.** We have no use
    for them, and firing a site's analytics while scraping pollutes their own
    numbers. Declining to do that is part of being a good guest.
*   **Rate limiting applies to navigations and data requests, not to static
    assets.** Throttling every image would make one page take minutes while
    imposing no less load. Our footprint is one human page view per cell, spaced
    by the configured delay, which is the honest thing to size the limit against.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apix.ethics.audit import AuditRecord, AuditWriter, NullAuditWriter, SnapshotStoreAdapter
from apix.ethics.robots import RobotsGate
from apix.net.identity import Identity, IdentityMode
from apix.net.throttle import Throttler, ThrottlePolicy
from apix.net.transport import Transport, default_transport
from apix.settings import settings as default_settings

log = logging.getLogger(__name__)


class PolicyRefused(Exception):
    """robots.txt or an intent rule refused this URL. Carries the audit id."""

    def __init__(self, message: str, *, request_id: int, intent_denied: bool) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.intent_denied = intent_denied


class FetchFailed(Exception):
    def __init__(self, message: str, *, request_id: int, status: int | None) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.status = status


#: Hosts we refuse to contact during a render. Not a robots decision, a footprint
#: decision: none of these serve content we need.
TRACKER_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "facebook.com", "connect.facebook.net",
    "hotjar.com", "clarity.ms", "segment.io", "segment.com",
    "mixpanel.com", "amplitude.com", "branch.io", "adsrvr.org",
    "criteo.com", "taboola.com", "outbrain.com", "moengage.com",
    "clevertap.com", "webengage.com", "newrelic.com", "nr-data.net",
    "sentry.io", "bugsnag.com", "optimizely.com", "quantserve.com",
)

#: Resource types that count as "a request we chose to make" and are therefore
#: rate limited. Static assets are part of rendering one page, not extra load.
THROTTLED_RESOURCE_TYPES = frozenset({"document", "xhr", "fetch"})


@dataclass(slots=True)
class Response:
    """Implements the FetchResult protocol from apix.sources.base."""
    url: str
    status: int
    body: bytes
    request_id: int
    json: Any = None
    elapsed_ms: int = 0
    captured: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class SourcePolicy:
    """Per-source overrides, loaded from config/sources.yaml."""
    code: str
    min_delay_seconds: float = 6.0
    jitter_seconds: float = 2.0
    intent_denied_paths: tuple[str, ...] = ()

    @property
    def throttle(self) -> ThrottlePolicy:
        return ThrottlePolicy(
            min_delay_seconds=self.min_delay_seconds,
            jitter_seconds=self.jitter_seconds,
        )


class PolicyEnforcedSession:
    def __init__(
        self,
        audit: AuditWriter | None = None,
        *,
        identity: Identity | None = None,
        throttler: Throttler | None = None,
        transport: Transport | None = None,
        policies: dict[str, SourcePolicy] | None = None,
        settings=default_settings,
        clock=time.monotonic,
    ) -> None:
        self.settings = settings
        self.audit = audit if audit is not None else NullAuditWriter()
        self.identity = identity or Identity(IdentityMode(settings.identity_mode))
        self.throttler = throttler or Throttler(
            ThrottlePolicy(
                min_delay_seconds=settings.min_delay_seconds,
                jitter_seconds=settings.jitter_seconds,
            )
        )
        self.transport = transport if transport is not None else default_transport()
        self.policies = policies or {}
        self._clock = clock
        self._gate = RobotsGate(
            fetcher=self._fetch_robots_txt,
            store=SnapshotStoreAdapter(self.audit),
            user_agent=self.identity.robots_token,
            ttl=dt.timedelta(hours=settings.robots_ttl_hours),
        )
        self._browser = None
        self._playwright = None

    # -- policy helpers ----------------------------------------------------

    def _policy(self, source_code: str) -> SourcePolicy:
        return self.policies.get(
            source_code,
            SourcePolicy(
                code=source_code,
                min_delay_seconds=self.settings.min_delay_seconds,
                jitter_seconds=self.settings.jitter_seconds,
            ),
        )

    async def _fetch_robots_txt(self, url: str) -> tuple[int | None, str]:
        """Fetch robots.txt itself.

        Exempt from the robots check for the obvious reason, but still throttled
        and still audited. `robots_allowed=True` is correct rather than a fudge:
        RFC 9309 places robots.txt outside its own rules.
        """
        waited = await self.throttler.wait(url)
        started = self._clock()
        raw = await self.transport.get(
            url, self.identity.headers(), self.settings.http_timeout_seconds
        )
        elapsed_ms = int((self._clock() - started) * 1000)
        self.audit.record(AuditRecord(
            source_code="__robots__",
            url=url,
            user_agent=self.identity.user_agent(),
            robots_allowed=True,
            delay_applied_s=waited,
            outcome="ok" if raw.status == 200 else (
                "http_error" if raw.status else "timeout"),
            http_status=raw.status,
            bytes_returned=len(raw.body),
            duration_ms=elapsed_ms,
            error_detail=raw.error,
        ))
        return raw.status, raw.body.decode("utf-8", errors="replace")

    async def _check(self, url: str, source_code: str):
        pol = self._policy(source_code)
        return await self._gate.check(
            url, source_code=source_code, intent_denied_paths=pol.intent_denied_paths
        )

    def _refuse(self, url: str, source_code: str, decision, method: str = "GET") -> int:
        """Record a refusal. The audit row is the only artifact a denial produces."""
        rec = AuditRecord(
            source_code=source_code,
            url=url,
            method=method,
            user_agent=self.identity.user_agent(),
            robots_allowed=False,
            robots_rule=decision.rule,
            robots_snapshot_id=decision.snapshot_id,
            intent_denied=decision.intent_denied,
            delay_applied_s=0.0,
            outcome=decision.outcome_if_denied,
            error_detail=f"refused by {decision.rule}",
        )
        return self.audit.record(rec)

    def _persist_body(self, source_code: str, url: str, body: bytes) -> None:
        """Keep the raw response so a parser bug is fixable without re-scraping."""
        if not self.settings.store_raw_bodies or not body:
            return
        digest = hashlib.sha256(body).hexdigest()[:16]
        host = urllib.parse.urlsplit(url).netloc.replace(":", "_")
        day = dt.date.today().isoformat()
        out = Path(self.settings.raw_body_dir) / source_code / day / host
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{digest}.bin").write_bytes(body)
        except OSError as exc:  # never let archiving break collection
            log.warning("could not persist raw body for %s: %s", url, exc)

    # -- the guarded fetch -------------------------------------------------

    async def get(self, url: str, *, source_code: str, **kw) -> Response:
        """Plain HTTP GET, fully guarded. Retries transient failures.

        Each retry is a separate request and gets its own audit row, so the trail
        shows retry traffic rather than hiding it inside one record.
        """
        decision = await self._check(url, source_code)
        if not decision.allowed:
            rid = self._refuse(url, source_code, decision)
            raise PolicyRefused(
                f"refused {url}: {decision.rule}",
                request_id=rid, intent_denied=decision.intent_denied,
            )

        pol = self._policy(source_code)
        attempts = max(1, self.settings.max_retries + 1)
        last: Exception | None = None

        for attempt in range(attempts):
            waited = await self.throttler.wait(
                url, decision.crawl_delay, pol.throttle
            )
            if attempt:
                # Backoff is additive to the politeness delay, never a substitute.
                backoff = pol.min_delay_seconds * (2 ** (attempt - 1))
                await asyncio.sleep(backoff)
                waited += backoff

            started = self._clock()
            outcome, status, nbytes, err = "ok", None, 0, None
            raw = None
            try:
                raw = await self.transport.get(
                    url, self.identity.headers(), self.settings.http_timeout_seconds
                )
                status, nbytes, err = raw.status, len(raw.body), raw.error
                if status is None:
                    outcome = "timeout"
                elif status == 429:
                    outcome = "rate_limited"
                elif status >= 400:
                    outcome = "http_error"
            except Exception as exc:  # noqa: BLE001 - must still audit
                outcome, err = "http_error", repr(exc)
                last = exc
            finally:
                rid = self.audit.record(AuditRecord(
                    source_code=source_code,
                    url=url,
                    user_agent=self.identity.user_agent(),
                    robots_allowed=True,
                    robots_rule=decision.rule,
                    robots_snapshot_id=decision.snapshot_id,
                    delay_applied_s=waited,
                    outcome=outcome,
                    http_status=status,
                    bytes_returned=nbytes,
                    duration_ms=int((self._clock() - started) * 1000),
                    error_detail=err,
                ))

            if outcome == "ok" and raw is not None:
                # Re-check the FINAL url after redirects.
                #
                # The transport follows redirects, so a robots-allowed URL can
                # land on a disallowed one and hand back its body. Checking only
                # the requested URL made that content retrievable and, worse,
                # recorded it in the audit log as a clean fetch of the allowed
                # path, hiding which URL was actually read. That defeats the
                # entire compliance claim, so the landing URL is checked too and
                # its body discarded on a denial.
                final = raw.final_url or url
                if final != url:
                    landing = await self._check(final, source_code)
                    if not landing.allowed:
                        rid = self._refuse(final, source_code, landing)
                        raise PolicyRefused(
                            f"{url} redirected to {final}, which is disallowed: "
                            f"{landing.rule}",
                            request_id=rid,
                            intent_denied=landing.intent_denied,
                        )
                self._persist_body(source_code, final, raw.body)
                return Response(
                    url=final,
                    status=raw.status or 0,
                    body=raw.body,
                    request_id=rid,
                    json=_maybe_json(raw.body),
                    elapsed_ms=int((self._clock() - started) * 1000),
                )

            # 4xx other than 429 will not improve on retry.
            if status is not None and 400 <= status < 500 and status != 429:
                raise FetchFailed(f"{url} -> HTTP {status}", request_id=rid, status=status)
            last = last or FetchFailed(
                f"{url} -> {outcome}", request_id=rid, status=status
            )

        raise FetchFailed(
            f"{url} failed after {attempts} attempts: {last}",
            request_id=getattr(last, "request_id", 0),
            status=getattr(last, "status", None),
        )

    # -- browser rendering -------------------------------------------------

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run `make init`, which also "
                "downloads the Chromium build it needs."
            ) from exc
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    async def render(
        self,
        url: str,
        *,
        source_code: str,
        wait_for: str | None = None,
        capture_json: str | None = None,
        **kw,
    ) -> Response:
        """Render with a real browser, intercepting every subresource."""
        decision = await self._check(url, source_code)
        if not decision.allowed:
            rid = self._refuse(url, source_code, decision)
            raise PolicyRefused(
                f"refused {url}: {decision.rule}",
                request_id=rid, intent_denied=decision.intent_denied,
            )

        pol = self._policy(source_code)
        waited = await self.throttler.wait(url, decision.crawl_delay, pol.throttle)

        browser = await self._ensure_browser()
        context = await browser.new_context(
            user_agent=self.identity.user_agent(),
            extra_http_headers={
                k: v for k, v in self.identity.headers().items() if k != "User-Agent"
            },
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()
        captured: list[dict[str, Any]] = []

        await page.route("**/*", lambda route, request:
                         asyncio.ensure_future(
                             self._route_handler(route, request, source_code)))

        if capture_json:
            async def _on_response(resp):
                if capture_json not in resp.url:
                    return
                try:
                    captured.append(await resp.json())
                except Exception:  # noqa: BLE001 - non-JSON or already consumed
                    pass
            page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

        started = self._clock()
        outcome, status, body, err = "ok", None, b"", None
        try:
            nav = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.render_timeout_seconds * 1000,
            )
            status = nav.status if nav else None
            if wait_for:
                try:
                    await page.wait_for_selector(
                        wait_for, timeout=self.settings.render_timeout_seconds * 1000
                    )
                except Exception:  # noqa: BLE001
                    # Selector never appeared. Not fatal: the page may show a
                    # sold-out or no-flights state the parser recognises.
                    outcome = "no_flights"
            body = (await page.content()).encode()
            if status is not None and status >= 400:
                outcome = "http_error"
        except Exception as exc:  # noqa: BLE001 - must still audit
            outcome, err = "timeout", repr(exc)
        finally:
            elapsed_ms = int((self._clock() - started) * 1000)
            rid = self.audit.record(AuditRecord(
                source_code=source_code,
                url=url,
                user_agent=self.identity.user_agent(),
                robots_allowed=True,
                robots_rule=decision.rule,
                robots_snapshot_id=decision.snapshot_id,
                delay_applied_s=waited,
                outcome=outcome,
                http_status=status,
                bytes_returned=len(body),
                duration_ms=elapsed_ms,
                error_detail=err,
            ))
            await context.close()

        if outcome in ("timeout", "http_error"):
            raise FetchFailed(f"render {url} -> {outcome}: {err}",
                              request_id=rid, status=status)

        self._persist_body(source_code, url, body)
        if captured:
            self._persist_body(
                source_code, url + "#captured",
                json.dumps(captured).encode(),
            )
        return Response(
            url=url, status=status or 0, body=body, request_id=rid,
            json=captured[0] if len(captured) == 1 else (captured or None),
            elapsed_ms=elapsed_ms, captured=captured,
        )

    async def _route_handler(self, route, request, source_code: str) -> None:
        """Robots-check and footprint-filter every request the browser makes."""
        url = request.url
        host = urllib.parse.urlsplit(url).netloc.lower()

        if any(t in host for t in TRACKER_HOSTS):
            await route.abort()
            return

        try:
            decision = await self._check(url, source_code)
        except Exception:  # noqa: BLE001 - a gate failure must not hang the page
            await route.abort()
            return

        if not decision.allowed:
            self._refuse(url, source_code, decision, method=request.method)
            await route.abort()
            return

        if request.resource_type in THROTTLED_RESOURCE_TYPES:
            await self.throttler.wait(url, decision.crawl_delay,
                                      self._policy(source_code).throttle)
        await route.continue_()

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        await self.transport.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


def _maybe_json(body: bytes) -> Any:
    if not body:
        return None
    head = body.lstrip()[:1]
    if head not in (b"{", b"["):
        return None
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None


def load_policies(config_path: str | Path = "config/sources.yaml") -> dict[str, SourcePolicy]:
    """Build per-source policies from config, for enabled tier 1 and 2 sources only.

    A source at tier 3 or 4 gets no policy, so an attempt to fetch it falls back
    to defaults and is then refused by `FareSource.__init__` anyway. Two
    independent guards, because re-enabling an excluded source by accident is the
    single worst failure this system could have.
    """
    import yaml  # noqa: PLC0415

    raw = yaml.safe_load(Path(config_path).read_text())
    defaults = raw.get("defaults", {})
    out: dict[str, SourcePolicy] = {}
    for s in raw.get("sources", []):
        if not s.get("enabled") or s.get("tier", 4) > 2:
            continue
        out[s["code"]] = SourcePolicy(
            code=s["code"],
            min_delay_seconds=float(
                s.get("min_delay_seconds", defaults.get("min_delay_seconds", 6.0))
            ),
            jitter_seconds=float(
                s.get("jitter_seconds", defaults.get("jitter_seconds", 2.0))
            ),
            intent_denied_paths=tuple(s.get("intent_denied_paths", ())),
        )
    return out
