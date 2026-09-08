"""RFC 9309 matcher tests.

The regression at the centre of this file is real. Against Cleartrip's live
robots.txt, `urllib.robotparser` returned ALLOWED for `/flights/search`, a path
the file plainly disallows, because the standard library ignores wildcards and
takes the first matching rule rather than the most specific one. Both mistakes
permit fetches the site forbids.

Every source's actual robots.txt content is pinned below. If one of these files
changes, the audit should be redone rather than the expectation quietly edited.
"""
from __future__ import annotations

import pytest

from apix.ethics.matcher import RobotsRules, compile_pattern

UA = "APIx-Research-Bot"


# --------------------------------------------------------------------------
# The regression
# --------------------------------------------------------------------------

CLEARTRIP = """User-agent: *
Allow: /

Disallow: /cgi-bin/
Disallow: /flights/search*
Disallow: /trains/results*
Disallow: /m/flights/search*
Disallow: /hotels/info*
Disallow: /users/
Disallow: /signin/
"""


@pytest.mark.parametrize("path,allowed", [
    ("/flights/search", False),
    ("/flights/search?from=DEL&to=BOM", False),
    ("/flights/searchresults", False),   # the wildcard covers this too
    ("/m/flights/search", False),
    ("/hotels/info", False),
    ("/hotels/info/mumbai", False),
    ("/users/", False),
    ("/flights", True),
    ("/", True),
    ("/about", True),
])
def test_cleartrip_wildcards_are_honoured(path, allowed):
    r = RobotsRules.parse(CLEARTRIP)
    assert r.can_fetch(UA, f"https://www.cleartrip.com{path}") is allowed


def test_leading_allow_slash_does_not_defeat_later_disallows():
    """Cleartrip opens with `Allow: /`. Under first-match semantics that wins
    every comparison and the rest of the file becomes dead text."""
    r = RobotsRules.parse(CLEARTRIP)
    assert r.can_fetch(UA, "https://www.cleartrip.com/flights/search") is False
    rule = r.match(UA, "/flights/search")
    assert rule.pattern == "/flights/search*"
    assert rule.allow is False


def test_we_are_stricter_than_the_standard_library_here():
    """Documents the specific divergence, so nobody 'simplifies' this back."""
    import urllib.robotparser

    stdlib = urllib.robotparser.RobotFileParser()
    stdlib.parse(CLEARTRIP.splitlines())
    url = "https://www.cleartrip.com/flights/search"
    assert stdlib.can_fetch(UA, url) is True, "stdlib is expected to be wrong here"
    assert RobotsRules.parse(CLEARTRIP).can_fetch(UA, url) is False


# --------------------------------------------------------------------------
# RFC 9309 semantics
# --------------------------------------------------------------------------

def test_longest_match_wins_not_first():
    r = RobotsRules.parse(
        "User-agent: *\nDisallow: /a/\nAllow: /a/b/\nDisallow: /a/b/c/\n")
    assert r.can_fetch(UA, "https://x/a/") is False
    assert r.can_fetch(UA, "https://x/a/b/") is True
    assert r.can_fetch(UA, "https://x/a/b/c/") is False


def test_allow_wins_a_tie():
    """RFC 9309 section 2.2.2: equivalent rules resolve to allow."""
    r = RobotsRules.parse("User-agent: *\nDisallow: /x\nAllow: /x\n")
    assert r.can_fetch(UA, "https://x/x") is True


def test_dollar_anchors_the_end():
    r = RobotsRules.parse("User-agent: *\nDisallow: /search$\n")
    assert r.can_fetch(UA, "https://x/search") is False
    assert r.can_fetch(UA, "https://x/search/deep") is True
    assert r.can_fetch(UA, "https://x/searching") is True


def test_star_matches_any_sequence():
    r = RobotsRules.parse("User-agent: *\nDisallow: /a/*/c\n")
    assert r.can_fetch(UA, "https://x/a/b/c") is False
    assert r.can_fetch(UA, "https://x/a/anything/at/all/c") is False
    assert r.can_fetch(UA, "https://x/a/b/d") is True
    # The slashes around the star are literal, so /a/c has nothing for the
    # star to sit between and is not matched.
    assert r.can_fetch(UA, "https://x/a/c") is True


def test_star_may_match_an_empty_string():
    r = RobotsRules.parse("User-agent: *\nDisallow: /a/*c\n")
    assert r.can_fetch(UA, "https://x/a/c") is False
    assert r.can_fetch(UA, "https://x/a/bbbc") is False


def test_empty_disallow_means_allow_everything():
    r = RobotsRules.parse("User-agent: *\nDisallow:\n")
    assert r.can_fetch(UA, "https://x/anything") is True


def test_unmatched_path_is_allowed():
    r = RobotsRules.parse("User-agent: *\nDisallow: /private/\n")
    assert r.can_fetch(UA, "https://x/public") is True
    assert r.match(UA, "/public") is None


def test_query_string_is_matched():
    """Goibibo forbids `/flights/*?*`, which only bites if the query is matched."""
    r = RobotsRules.parse("User-agent: *\nDisallow: /flights/*?*\n")
    assert r.can_fetch(UA, "https://x/flights/search?from=DEL") is False
    assert r.can_fetch(UA, "https://x/flights/info") is True


def test_regex_metacharacters_in_a_path_are_literal():
    """A pattern must never be able to act as a regular expression."""
    r = RobotsRules.parse("User-agent: *\nDisallow: /a.b/\n")
    assert r.can_fetch(UA, "https://x/a.b/") is False
    assert r.can_fetch(UA, "https://x/axb/") is True


def test_rules_before_any_user_agent_are_ignored():
    r = RobotsRules.parse("Disallow: /everything\nUser-agent: *\nDisallow: /x\n")
    assert r.can_fetch(UA, "https://x/everything") is True
    assert r.can_fetch(UA, "https://x/x") is False


def test_comments_are_stripped():
    r = RobotsRules.parse("User-agent: *  # everyone\nDisallow: /x  # secret\n")
    assert r.can_fetch(UA, "https://x/x") is False


# --------------------------------------------------------------------------
# Group selection
# --------------------------------------------------------------------------

NAMED = """User-agent: *
Disallow: /

User-agent: APIx-Research-Bot
Allow: /
Disallow: /private/
Crawl-delay: 5
"""


def test_named_group_beats_wildcard():
    r = RobotsRules.parse(NAMED)
    assert r.can_fetch(UA, "https://x/public") is True
    assert r.can_fetch(UA, "https://x/private/") is False


def test_other_agents_still_get_the_wildcard_group():
    r = RobotsRules.parse(NAMED)
    assert r.can_fetch("SomeOtherBot", "https://x/public") is False


def test_consecutive_user_agent_lines_share_one_group():
    r = RobotsRules.parse(
        "User-agent: alpha\nUser-agent: beta\nDisallow: /x\n")
    assert r.can_fetch("alpha", "https://x/x") is False
    assert r.can_fetch("beta", "https://x/x") is False


def test_crawl_delay_read_from_the_matching_group():
    assert RobotsRules.parse(NAMED).crawl_delay(UA) == 5.0


def test_crawl_delay_absent_is_none():
    assert RobotsRules.parse("User-agent: *\nDisallow: /x\n").crawl_delay(UA) is None


# --------------------------------------------------------------------------
# Whole-site states
# --------------------------------------------------------------------------

def test_disallow_all_and_allow_all():
    assert RobotsRules([], disallow_all=True).can_fetch(UA, "https://x/") is False
    assert RobotsRules([], allow_all=True).can_fetch(UA, "https://x/") is True


def test_empty_file_allows_everything():
    assert RobotsRules.parse("").can_fetch(UA, "https://x/anything") is True


# --------------------------------------------------------------------------
# Real files from the source audit
# --------------------------------------------------------------------------

INDIGO = """User-agent: *
Disallow: /booking/
Disallow: /book/
Disallow: /bookings/
Disallow: /search.html
"""

AKASA = """User-Agent: *
Sitemap: https://www.akasaair.com/sitemap.xml
"""


def test_indigo_booking_paths_are_refused():
    r = RobotsRules.parse(INDIGO)
    for path in ("/booking/", "/book/", "/bookings/", "/search.html"):
        assert r.can_fetch(UA, f"https://www.goindigo.in{path}") is False
    assert r.can_fetch(UA, "https://www.goindigo.in/about") is True


def test_akasa_permits_everything_it_does_not_mention():
    r = RobotsRules.parse(AKASA)
    assert r.can_fetch(UA, "https://www.akasaair.com/") is True
    assert r.can_fetch(UA, "https://www.akasaair.com/flight-booking/delhi-to-mumbai") is True


# --------------------------------------------------------------------------
# The explanation string that lands in the audit trail
# --------------------------------------------------------------------------

def test_explain_names_the_deciding_rule():
    r = RobotsRules.parse(CLEARTRIP)
    assert r.explain(UA, "https://x/flights/search") == "robots:disallow:/flights/search*"
    assert r.explain(UA, "https://x/about") == "robots:allow:/"
    assert RobotsRules.parse(INDIGO).explain(UA, "https://x/nothing") == \
        "robots:no_matching_rule"


def test_compile_pattern_is_anchored_at_the_start():
    assert compile_pattern("/a").match("/a/b")
    assert not compile_pattern("/a").match("/b/a")
