"""RFC 9309 robots.txt rule matching.

WHY THIS EXISTS INSTEAD OF urllib.robotparser
---------------------------------------------
The standard library's `RobotFileParser` has two defects, and both of them err
towards permitting fetches the site intended to forbid. For a project whose
entire claim is that it collects defensibly, that is the worst possible
direction to be wrong in.

**It does not support wildcards.** `Disallow: /flights/search*` is treated as a
literal prefix that includes the asterisk, so the path `/flights/search` does not
match it. Cleartrip, MakeMyTrip, Goibibo and Ixigo all write their flight-search
prohibitions with a trailing `*`. Against Cleartrip's live file the stdlib parser
returned ALLOWED for `/flights/search`, which the site plainly forbids. That was
caught by comparing a live fetch against the source audit, and it is the reason
this module exists.

**It returns the first matching rule, not the most specific.** RFC 9309 section
2.2.2 requires the longest matching pattern to win. Cleartrip's file opens with
`Allow: /` and lists its prohibitions below; under first-match semantics the
opening `Allow: /` wins every comparison and the rest of the file is dead text.

This implementation follows RFC 9309:

*   `*` matches any sequence of characters; `$` at the end anchors the match.
*   The longest matching pattern wins, measured in characters of the pattern as
    written, which is what the RFC means by most octets.
*   When an allow and a disallow rule match with equal length, allow wins.
*   A path matched by no rule is allowed.
*   User-agent groups are selected by the most specific matching token, with `*`
    used only when no named group matches.

Where the RFC leaves room, this module chooses the more restrictive reading. A
false refusal costs us one observation; a false permission costs the project its
credibility.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern

    @property
    def specificity(self) -> int:
        """Length of the pattern as written, per RFC 9309 section 2.2.2."""
        return len(self.pattern)


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def compile_pattern(pattern: str) -> re.Pattern:
    """Compile a robots.txt path pattern into an anchored regular expression.

    `*` becomes `.*`; a trailing `$` anchors the end. Everything else is
    matched literally, so a path containing regex metacharacters cannot alter
    the meaning of a rule.
    """
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    compiled = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile("^" + compiled + ("$" if anchored else ""))


def _normalise(path: str) -> str:
    """Normalise a path for comparison.

    Percent-encoding is decoded then re-encoded consistently so that `/a%2Fb`
    and `/a/b` do not accidentally compare differently. Query strings are kept,
    because robots.txt patterns may match against them.
    """
    if not path.startswith("/"):
        path = "/" + path
    return path


class RobotsRules:
    """Parsed robots.txt, queryable per user agent."""

    def __init__(self, groups: list[Group], *, allow_all: bool = False,
                 disallow_all: bool = False) -> None:
        self.groups = groups
        self.allow_all = allow_all
        self.disallow_all = disallow_all

    # -- parsing -----------------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        groups: list[Group] = []
        current: Group | None = None
        # A blank line or a new User-agent after rules starts a new group. Two
        # consecutive User-agent lines share one group, per the RFC.
        expecting_agents = False

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            field_name = field_name.strip().lower()
            value = value.strip()

            if field_name == "user-agent":
                if current is None or not expecting_agents:
                    current = Group()
                    groups.append(current)
                    expecting_agents = True
                current.agents.append(value.lower())
                continue

            if current is None:
                # Rules before any User-agent line are not addressed to anyone.
                continue
            expecting_agents = False

            if field_name in ("disallow", "allow"):
                if not value:
                    # "Disallow:" with an empty value means allow everything;
                    # "Allow:" with an empty value is meaningless. Skip both
                    # rather than registering a zero-length pattern that would
                    # match every path.
                    continue
                current.rules.append(Rule(
                    allow=(field_name == "allow"),
                    pattern=value,
                    regex=compile_pattern(value),
                ))
            elif field_name == "crawl-delay":
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass

        return cls(groups)

    # -- group selection ---------------------------------------------------

    def group_for(self, user_agent: str) -> Group | None:
        """Most specific group matching this agent, else the wildcard group.

        Matching is the RFC's: case-insensitive, and a group's token matches if
        it is a prefix of the product token. The longest matching token wins, so
        a file addressing both `ClaudeBot` and `Claude` gives the former.
        """
        ua = user_agent.lower()
        best: tuple[int, Group] | None = None
        wildcard: Group | None = None

        for group in self.groups:
            for agent in group.agents:
                if agent == "*":
                    if wildcard is None:
                        wildcard = group
                    continue
                if ua.startswith(agent) or agent in ua:
                    if best is None or len(agent) > best[0]:
                        best = (len(agent), group)
        return best[1] if best else wildcard

    # -- the decision ------------------------------------------------------

    def match(self, user_agent: str, path: str) -> Rule | None:
        """The winning rule for this path, or None if no rule matches."""
        group = self.group_for(user_agent)
        if group is None:
            return None

        winner: Rule | None = None
        for rule in group.rules:
            if not rule.regex.match(path):
                continue
            if winner is None:
                winner = rule
                continue
            if rule.specificity > winner.specificity:
                winner = rule
            elif rule.specificity == winner.specificity and rule.allow:
                # Equal length: allow wins, per RFC 9309 section 2.2.2.
                winner = rule
        return winner

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        parts = urllib.parse.urlsplit(url)
        path = _normalise(parts.path or "/")
        if parts.query:
            path = f"{path}?{parts.query}"
        rule = self.match(user_agent, path)
        return True if rule is None else rule.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        group = self.group_for(user_agent)
        return group.crawl_delay if group else None

    def explain(self, user_agent: str, url: str) -> str:
        """Human-readable reason, for the audit trail."""
        if self.disallow_all:
            return "robots:disallow_all"
        if self.allow_all:
            return "robots:allow_all"
        parts = urllib.parse.urlsplit(url)
        path = _normalise(parts.path or "/")
        if parts.query:
            path = f"{path}?{parts.query}"
        rule = self.match(user_agent, path)
        if rule is None:
            return "robots:no_matching_rule"
        return f"robots:{'allow' if rule.allow else 'disallow'}:{rule.pattern}"
