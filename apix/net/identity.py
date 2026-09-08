"""Crawler identity.

The problem statement asks for rotating realistic user-agents. That capability is
implemented here, and it is **off by default**, because the two things it is used
for are not the same:

  * Rotating UAs so a fleet of workers is not mistaken for one runaway client is
    ordinary hygiene.
  * Rotating UAs so a site cannot tell it is being scraped is evasion, and it is
    what makes a scraping programme indefensible in front of a regulator.

Default mode is IDENTIFIED: one honest, stable token with a contact URL, so an
operator can see exactly who we are, block us in one line of robots.txt if they
wish, and reach a human. A system built for the National Statistical Office
should be the easiest crawler on the internet to block.

ROTATING mode exists so the trade-off is a documented, deliberate configuration
change rather than a hidden default.
"""
from __future__ import annotations

import random
from enum import Enum

CONTACT_URL = "https://apix.mospi-sih.example/crawler"
BOT_TOKEN = "APIx-Research-Bot"

IDENTIFIED_UA = (
    f"{BOT_TOKEN}/1.0 (+{CONTACT_URL}; "
    "official-statistics research; SIH26056/MoSPI) "
    "Mozilla/5.0 (compatible) Chrome/124.0.0.0 Safari/537.36"
)

# Used only in ROTATING mode. Real, current desktop strings.
_REALISTIC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)


class IdentityMode(str, Enum):
    IDENTIFIED = "identified"
    ROTATING = "rotating"


class Identity:
    def __init__(self, mode: IdentityMode = IdentityMode.IDENTIFIED,
                 seed: int | None = None) -> None:
        self.mode = mode
        self._rng = random.Random(seed)

    @property
    def robots_token(self) -> str:
        """The token robots.txt rules are evaluated against.

        Always the bot token, even in ROTATING mode. Rotating the UA sent on the
        wire while still checking robots as ourselves keeps the compliance
        decision honest: we never gain access by pretending to be a browser.
        """
        return BOT_TOKEN

    def user_agent(self) -> str:
        if self.mode is IdentityMode.IDENTIFIED:
            return IDENTIFIED_UA
        return self._rng.choice(_REALISTIC)

    def headers(self) -> dict[str, str]:
        h = {
            "User-Agent": self.user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        if self.mode is IdentityMode.IDENTIFIED:
            h["From"] = "apix-crawler@mospi-sih.example"
            h["X-Crawler-Purpose"] = "official-statistics-research"
        return h
