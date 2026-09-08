"""Runtime configuration, loaded from environment with .env fallback.

Deliberately plain: no pydantic-settings dependency, because this module is
imported by the ethics layer and the ethics layer must not fail to load because
of a third-party version skew.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Never overrides a real environment variable."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(REPO_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://apix:apix@localhost:5433/apix"
        )
    )
    use_seed_data: bool = field(default_factory=lambda: _bool("APIX_USE_SEED_DATA", True))
    seed_dir: Path = field(
        default_factory=lambda: REPO_ROOT / os.getenv("APIX_SEED_DIR", "data/seed")
    )

    identity_mode: str = field(
        default_factory=lambda: os.getenv("APIX_IDENTITY_MODE", "identified")
    )
    min_delay_seconds: float = field(
        default_factory=lambda: _float("APIX_MIN_DELAY_SECONDS", 6.0)
    )
    jitter_seconds: float = field(
        default_factory=lambda: _float("APIX_JITTER_SECONDS", 2.0)
    )
    max_concurrent_per_origin: int = field(
        default_factory=lambda: _int("APIX_MAX_CONCURRENT_PER_ORIGIN", 1)
    )
    fail_closed_on_robots_error: bool = field(
        default_factory=lambda: _bool("APIX_FAIL_CLOSED_ON_ROBOTS_ERROR", True)
    )
    robots_ttl_hours: int = field(default_factory=lambda: _int("APIX_ROBOTS_TTL_HOURS", 12))

    #: Seconds to wait for a database connection before giving up. Deliberately
    #: short: when Postgres is not running, saying so at once is more useful
    #: than a 30-second stall, and the test suite skips its integration tests
    #: on this check.
    db_connect_timeout: float = field(
        default_factory=lambda: _float("APIX_DB_CONNECT_TIMEOUT", 3.0)
    )

    http_timeout_seconds: float = field(
        default_factory=lambda: _float("APIX_HTTP_TIMEOUT_SECONDS", 30.0)
    )
    render_timeout_seconds: float = field(
        default_factory=lambda: _float("APIX_RENDER_TIMEOUT_SECONDS", 60.0)
    )
    max_retries: int = field(default_factory=lambda: _int("APIX_MAX_RETRIES", 2))

    #: Persist every response body for replay. Disk-hungry but it is what makes
    #: a parser bug fixable without re-scraping the source.
    store_raw_bodies: bool = field(
        default_factory=lambda: _bool("APIX_STORE_RAW_BODIES", True)
    )
    raw_body_dir: Path = field(
        default_factory=lambda: REPO_ROOT / os.getenv("APIX_RAW_BODY_DIR", "data/raw/pages")
    )

    mospi_api_base: str = field(
        default_factory=lambda: os.getenv(
            "MOSPI_API_BASE", "https://api.mospi.gov.in/api/esankhyiki"
        )
    )
    mospi_allow_legacy_tls: bool = field(
        default_factory=lambda: _bool("MOSPI_ALLOW_LEGACY_TLS", True)
    )


settings = Settings()
