"""Shared test fixtures.

DATABASE ISOLATION
------------------
The integration tests truncate `fare`, `raw_quote` and `request_audit` between
cases. Pointed at the working database, that quietly destroys whatever dataset
was there, and it destroys it in the background: a test run triggered by another
editor session, a watcher, or a colleague's terminal leaves the demo looking
empty with nothing to explain why. That happened during development and cost
real time to diagnose.

So the tests get their own database, `apix_test`, created on demand. The
override is set before any `apix` module is imported, because `apix.settings`
reads the environment once at import and freezes it.

Point the tests at something else with APIX_TEST_DATABASE_URL. Never point it at
the database holding data you want to keep.
"""
import os

_DEFAULT_TEST_DB = "postgresql://apix:apix@localhost:5433/apix_test"
os.environ["DATABASE_URL"] = os.environ.get(
    "APIX_TEST_DATABASE_URL", _DEFAULT_TEST_DB)

import asyncio  # noqa: E402
import pytest  # noqa: E402

from apix.net.transport import RawResponse  # noqa: E402


def _ensure_test_database() -> bool:
    """Create the test database and schema if missing. False if unreachable."""
    try:
        import psycopg
    except ModuleNotFoundError:
        return False

    url = os.environ["DATABASE_URL"]
    target = url.rsplit("/", 1)[-1]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{target}"')
    except Exception:
        return False

    root = os.path.join(os.path.dirname(__file__), "..")
    try:
        with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'apix' AND table_name = 'fare'")
            if cur.fetchone() is None:
                with open(os.path.join(root, "db", "schema.sql")) as fh:
                    cur.execute(fh.read())
            # Reference data too. The integration tests skip themselves when
            # routes and sources are absent, so a schema-only database would
            # silently reduce the suite to unit tests and still report green.
            cur.execute("SELECT count(*) FROM apix.route")
            if cur.fetchone()[0] == 0:
                _load_reference(root)
        return True
    except Exception:
        return False


def _load_reference(root: str) -> None:
    import sys

    sys.path.insert(0, os.path.abspath(root))
    from apix import db
    from scripts.load_reference import load

    with db.connection() as conn:
        load(conn,
             os.path.join(root, "config", "basket.yaml"),
             os.path.join(root, "config", "sources.yaml"),
             os.path.join(root, "data", "reference", "route_weights.csv"))


_ensure_test_database()


class FakeTransport:
    """Scripted transport. Records every request so tests can assert on egress.

    Crucially it records requests the session should NEVER have made, which is
    how we prove a disallowed URL was not merely unrecorded but actually not sent.
    """

    def __init__(self, routes: dict[str, RawResponse] | None = None):
        self.routes = routes or {}
        self.requests: list[str] = []
        self.closed = False

    def add(self, url: str, status: int = 200, body: bytes = b"",
            error: str | None = None) -> None:
        self.routes[url] = RawResponse(status=status, body=body,
                                       final_url=url, error=error)

    async def get(self, url, headers, timeout):
        self.requests.append(url)
        if url in self.routes:
            return self.routes[url]
        return RawResponse(status=404, body=b"not found", final_url=url,
                           error="HTTP 404")

    async def aclose(self):
        self.closed = True


class InstantThrottler:
    """Records the delay that WOULD have been applied without sleeping.

    Lets the tests assert the politeness arithmetic (floor vs Crawl-delay vs
    jitter) without spending real seconds.
    """

    def __init__(self, policy=None, seed: int = 7):
        import random
        from apix.net.throttle import ThrottlePolicy
        self.policy = policy or ThrottlePolicy()
        self.waits: list[tuple[str, float]] = []
        self._rng = random.Random(seed)

    async def wait(self, url, crawl_delay=None, policy=None):
        pol = policy or self.policy
        d = pol.effective_delay(crawl_delay, self._rng)
        self.waits.append((url, d))
        return d


@pytest.fixture
def fake_transport():
    return FakeTransport()


@pytest.fixture
def instant_throttler():
    return InstantThrottler()
