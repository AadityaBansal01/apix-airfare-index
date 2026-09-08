"""Raw HTTP transport, deliberately dumb.

Knows nothing about robots.txt, rate limits or auditing. It exists so that
`PolicyEnforcedSession` has one seam to stub in tests, and so the policy layer
above it is the only place a network decision is ever made.

Prefers httpx when installed (HTTP/2, connection reuse), falls back to urllib in
a thread so the project runs with no third-party HTTP dependency at all.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RawResponse:
    status: int | None
    body: bytes
    final_url: str
    error: str | None = None


class Transport(Protocol):
    async def get(self, url: str, headers: dict[str, str], timeout: float) -> RawResponse: ...
    async def aclose(self) -> None: ...


class UrllibTransport:
    """Standard-library transport, run off the event loop in a worker thread.

    `allow_legacy_tls` exists for one real endpoint: MoSPI's api.mospi.gov.in
    negotiates TLS in a way modern OpenSSL refuses by default, failing with
    "unsafe legacy renegotiation disabled". It is opt-in per transport, never
    global, so a workaround for one government endpoint cannot silently weaken
    every other connection the system makes.
    """

    #: ssl.OP_LEGACY_SERVER_CONNECT, named explicitly because it is absent from
    #: the ssl module on some Python builds.
    OP_LEGACY_SERVER_CONNECT = 0x4

    def __init__(self, allow_legacy_tls: bool = False) -> None:
        self._ctx = ssl.create_default_context()
        if allow_legacy_tls:
            self._ctx.options |= self.OP_LEGACY_SERVER_CONNECT

    def _get_sync(self, url: str, headers: dict[str, str], timeout: float) -> RawResponse:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                return RawResponse(status=r.status, body=r.read(), final_url=r.url)
        except urllib.error.HTTPError as e:
            # An HTTP error is a real response: keep the body, it often explains why.
            return RawResponse(status=e.code, body=e.read() or b"", final_url=url,
                               error=f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as e:
            return RawResponse(status=None, body=b"", final_url=url, error=str(e))

    async def get(self, url: str, headers: dict[str, str], timeout: float) -> RawResponse:
        return await asyncio.to_thread(self._get_sync, url, headers, timeout)

    async def aclose(self) -> None:
        return None


class HttpxTransport:
    """Used when httpx is installed."""

    def __init__(self, allow_legacy_tls: bool = False) -> None:
        import httpx  # noqa: PLC0415

        ctx = ssl.create_default_context()
        if allow_legacy_tls:
            ctx.options |= UrllibTransport.OP_LEGACY_SERVER_CONNECT
        # HTTP/2 needs the optional `h2` package. Fall back rather than fail:
        # the transport is plumbing and must never be the reason a run dies.
        try:
            self._client = httpx.AsyncClient(
                follow_redirects=True, verify=ctx, http2=True,
            )
        except ImportError:
            log.debug("h2 not installed; falling back to HTTP/1.1")
            self._client = httpx.AsyncClient(
                follow_redirects=True, verify=ctx, http2=False,
            )

    async def get(self, url: str, headers: dict[str, str], timeout: float) -> RawResponse:
        import httpx  # noqa: PLC0415

        try:
            r = await self._client.get(url, headers=headers, timeout=timeout)
            return RawResponse(status=r.status_code, body=r.content, final_url=str(r.url))
        except httpx.TimeoutException as e:
            return RawResponse(None, b"", url, error=f"timeout: {e}")
        except httpx.HTTPError as e:
            return RawResponse(None, b"", url, error=str(e))

    async def aclose(self) -> None:
        await self._client.aclose()


def default_transport(allow_legacy_tls: bool = False) -> Transport:
    try:
        import httpx  # noqa: F401,PLC0415
        return HttpxTransport(allow_legacy_tls)
    except Exception as exc:  # noqa: BLE001 - any httpx problem falls back
        log.debug("httpx unusable (%s); using urllib transport", exc)
        return UrllibTransport(allow_legacy_tls)
