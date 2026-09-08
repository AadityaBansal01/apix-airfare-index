"""Cleaning pipeline: raw quotes in, index-ready fares out.

Stages, in order. Each is a pure function over a list of quotes so the whole
pipeline is testable without a database and every stage can be inspected alone.

    1. plausibility   reject quotes that cannot be a domestic economy fare
    2. deduplicate    collapse byte-identical repeats of the same itinerary
    3. decompose      verify base / taxes / UDF / convenience reconcile
    4. outliers       flag data errors within same-day comparison groups
    5. select         choose the one price per (route, window, carrier) the
                      index will consume

A `CleaningReport` records what happened at every stage. That report is a
deliverable, not debug output: a statistical office needs to know how many
observations were discarded and why before it will trust the index.

NOTHING IS EVER DELETED. Rejected and flagged quotes are written to the database
with their flags and excluded at query time. Every exclusion is reversible and
countable, which is what makes the discard rate auditable.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from apix.pipeline.outliers import Method, Verdict, detect
from apix.sources.base import FareQuote

log = logging.getLogger(__name__)

CLEANER_VERSION = "clean-v1"

# ---------------------------------------------------------------------------
# Plausibility bounds
#
# These are judgment calls and are stated as such. They exist to catch parse
# failures with a characteristic signature, not to express a view about what a
# ticket should cost.
# ---------------------------------------------------------------------------

#: A domestic one-way economy total below this cannot be real: the User
#: Development Fee and statutory charges alone are typically 400-700 INR. A quote
#: under 500 almost always means a component was captured instead of the total,
#: or a currency/paise mix-up.
MIN_PLAUSIBLE_FARE = Decimal("500")

#: Above this, the parser has almost certainly picked up a package, a multi-city
#: itinerary, a business-cabin fare, or a price in paise. Genuine domestic
#: economy fares on these trunk routes do not reach it even at T+1 in peak season.
MAX_PLAUSIBLE_FARE = Decimal("200000")

#: Sanity bound on the advance-purchase window.
MAX_WINDOW_DAYS = 365


class RejectReason(str):
    pass


REJECT_BELOW_MIN = "implausible_below_minimum"
REJECT_ABOVE_MAX = "implausible_above_maximum"
REJECT_BAD_WINDOW = "window_out_of_range"
REJECT_NONPOSITIVE = "non_positive_total"
REJECT_OD_SAME = "origin_equals_destination"


@dataclass(slots=True)
class Rejection:
    quote: FareQuote
    reason: str
    detail: str = ""


@dataclass(slots=True)
class CleaningReport:
    """What the pipeline did. Published alongside the index."""
    scrape_date: dt.date | None = None
    input_quotes: int = 0
    rejected: list[Rejection] = field(default_factory=list)
    exact_duplicates: int = 0
    sold_out: int = 0
    outliers: int = 0
    components_complete: int = 0
    components_missing: int = 0
    output_quotes: int = 0
    index_cells: int = 0
    comparison_groups: int = 0
    groups_too_small_to_assess: int = 0

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def discard_rate(self) -> float:
        """Share of input discarded before the index sees it.

        Worth watching: a rate that jumps between days usually means a source
        changed its markup, not that the market changed.
        """
        if not self.input_quotes:
            return 0.0
        discarded = self.rejected_count + self.exact_duplicates + self.outliers
        return discarded / self.input_quotes

    @property
    def component_completeness(self) -> float:
        total = self.components_complete + self.components_missing
        return self.components_complete / total if total else 0.0

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for r in self.rejected:
            counts[r.reason] += 1
        return dict(counts)

    def summary(self) -> str:
        return (
            f"{self.input_quotes} in -> {self.output_quotes} out "
            f"({self.discard_rate:.1%} discarded): "
            f"{self.rejected_count} implausible, {self.exact_duplicates} duplicates, "
            f"{self.outliers} outliers, {self.sold_out} sold out; "
            f"component split on {self.component_completeness:.0%}; "
            f"{self.index_cells} index cells"
        )


# ---------------------------------------------------------------------------
# Stage 1: plausibility
# ---------------------------------------------------------------------------

def check_plausible(q: FareQuote) -> str | None:
    """Return a rejection reason, or None if the quote could be a real fare."""
    if q.origin == q.destination:
        return REJECT_OD_SAME
    if q.total_fare is None or q.total_fare <= 0:
        return REJECT_NONPOSITIVE
    if not (0 < q.window_days <= MAX_WINDOW_DAYS):
        return REJECT_BAD_WINDOW
    if q.total_fare < MIN_PLAUSIBLE_FARE:
        return REJECT_BELOW_MIN
    if q.total_fare > MAX_PLAUSIBLE_FARE:
        return REJECT_ABOVE_MAX
    return None


# ---------------------------------------------------------------------------
# Stage 2: deduplication
# ---------------------------------------------------------------------------

def dedup_key(q: FareQuote) -> tuple:
    """Identity of an observation.

    Deliberately includes `total_fare`: two genuinely different fare brands on
    the same flight are two observations, not a duplicate. It also includes the
    source, because the same flight seen through two sources is corroboration
    and collapsing it would silently discard evidence.
    """
    return (
        q.source_code, q.scrape_date, q.carrier, q.flight_number,
        q.departure_date, q.fare_class, q.fare_brand, q.total_fare,
    )


def deduplicate(quotes: Sequence[FareQuote]) -> tuple[list[FareQuote], int]:
    seen: set[tuple] = set()
    out: list[FareQuote] = []
    dropped = 0
    for q in quotes:
        k = dedup_key(q)
        if k in seen:
            dropped += 1
            continue
        seen.add(k)
        out.append(q)
    return out, dropped


# ---------------------------------------------------------------------------
# Stage 3: fare decomposition
# ---------------------------------------------------------------------------

def verify_decomposition(q: FareQuote) -> None:
    """Confirm the base / taxes / UDF / convenience split reconciles.

    Mutates only the quality flags. Where the split does not reconcile, the
    components are cleared and the total is kept: a total we observed directly is
    trustworthy, a decomposition we could not verify is not. Publishing an
    unverifiable split would be worse than publishing none, because a downstream
    user would reasonably assume it was checked.
    """
    if q.base_fare is None and q.taxes is None:
        # Clear the remaining components too. Without this a quote flagged
        # "no_component_split" could still carry a UDF and a convenience fee,
        # so "absent" would mean "mostly absent" and a consumer summing what
        # survived would get a number that is not the fare. Absent means all
        # five are absent.
        q.udf = None
        q.convenience_fee = None
        q.other_charges = None
        if "no_component_split" not in q.quality_flags:
            q.quality_flags.append("no_component_split")
        return

    if q.components_complete:
        return

    # Parsed something, but it does not add up.
    q.quality_flags.append("components_did_not_reconcile")
    q.base_fare = None
    q.taxes = None
    q.udf = None
    q.convenience_fee = None
    q.other_charges = None


# ---------------------------------------------------------------------------
# Stage 4: outlier flagging
# ---------------------------------------------------------------------------

def comparison_key(q: FareQuote) -> tuple:
    """The group within which a quote is judged.

    Confined to one scrape date and one carrier on one route and window. Nothing
    is ever compared across days, so a genuine price surge cannot be flagged as
    an outlier. See apix/pipeline/outliers.py for the full argument.
    """
    return (q.origin, q.destination, q.carrier, q.window_days, q.scrape_date)


def flag_outliers(
    quotes: Sequence[FareQuote],
    method: Method | str = Method.MAD_LOG,
    **kwargs,
) -> tuple[dict[int, Verdict], int, int]:
    """Flag data errors group by group.

    Returns verdicts keyed by `id()` of the quote, the number of groups examined,
    and the number too small to assess.
    """
    groups: dict[tuple, list[FareQuote]] = defaultdict(list)
    for q in quotes:
        groups[comparison_key(q)].append(q)

    verdicts: dict[int, Verdict] = {}
    too_small = 0
    for members in groups.values():
        fares = [q.total_fare for q in members]
        results = detect(fares, method, **kwargs)
        if results and results[0].reason.startswith("sample of"):
            too_small += 1
        for q, v in zip(members, results):
            verdicts[id(q)] = v
    return verdicts, len(groups), too_small


# ---------------------------------------------------------------------------
# Stage 5: index price selection
# ---------------------------------------------------------------------------

IndexCell = tuple[str, str, int, str]   # origin, destination, window, carrier


def select_index_prices(quotes: Sequence[FareQuote]) -> dict[IndexCell, Decimal]:
    """The lowest available total fare per route, window and carrier.

    This is the price concept in METHODOLOGY.md section 2: what a traveller
    faces, which is the cheapest purchasable seat, not an average over booking
    classes the traveller cannot buy. Sold-out and flagged quotes never reach
    here.
    """
    best: dict[IndexCell, Decimal] = {}
    for q in quotes:
        key = (q.origin, q.destination, q.window_days, q.carrier)
        if key not in best or q.total_fare < best[key]:
            best[key] = q.total_fare
    return best


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CleaningResult:
    clean: list[FareQuote]
    flagged: list[FareQuote]
    rejected: list[Rejection]
    index_prices: dict[IndexCell, Decimal]
    report: CleaningReport

    @property
    def persistable(self) -> list[FareQuote]:
        """Everything that should reach the database: clean plus flagged.

        Rejected quotes are not persisted as fares, because they failed a basic
        plausibility test and are not fares. Their count and reasons live in the
        report.
        """
        return self.clean + self.flagged


def clean_quotes(
    quotes: Iterable[FareQuote],
    *,
    method: Method | str = Method.MAD_LOG,
    outlier_kwargs: dict | None = None,
) -> CleaningResult:
    quotes = list(quotes)
    report = CleaningReport(input_quotes=len(quotes))
    if quotes:
        report.scrape_date = quotes[0].scrape_date

    # 1. plausibility
    survivors: list[FareQuote] = []
    for q in quotes:
        reason = check_plausible(q)
        if reason:
            report.rejected.append(Rejection(q, reason, f"total={q.total_fare}"))
        else:
            survivors.append(q)

    # 2. deduplicate
    survivors, dupes = deduplicate(survivors)
    report.exact_duplicates = dupes

    # 3. decomposition
    for q in survivors:
        verify_decomposition(q)
        if q.components_complete:
            report.components_complete += 1
        else:
            report.components_missing += 1

    # Sold-out quotes are real information but are not prices. They are set
    # aside before outlier assessment so they cannot distort a group's median.
    sold_out = [q for q in survivors if q.is_sold_out]
    priced = [q for q in survivors if not q.is_sold_out]
    report.sold_out = len(sold_out)

    # 4. outliers
    verdicts, n_groups, too_small = flag_outliers(
        priced, method, **(outlier_kwargs or {})
    )
    report.comparison_groups = n_groups
    report.groups_too_small_to_assess = too_small

    clean: list[FareQuote] = []
    flagged: list[FareQuote] = []
    for q in priced:
        v = verdicts.get(id(q))
        if v and v.is_outlier:
            q.quality_flags.append(f"outlier:{v.method}:{v.score}")
            flagged.append(q)
        else:
            clean.append(q)
    report.outliers = len(flagged)
    flagged.extend(sold_out)

    # 5. index prices
    index_prices = select_index_prices(clean)
    report.index_cells = len(index_prices)
    report.output_quotes = len(clean)

    return CleaningResult(
        clean=clean,
        flagged=flagged,
        rejected=report.rejected,
        index_prices=index_prices,
        report=report,
    )


def outlier_verdict_for(q: FareQuote) -> tuple[bool, str | None, float | None]:
    """Read back the flag the pipeline attached, for persistence."""
    for f in q.quality_flags:
        if f.startswith("outlier:"):
            _, method, score = f.split(":", 2)
            return True, method, float(score)
    return False, None, None
