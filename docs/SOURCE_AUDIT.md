# Source Audit — robots.txt, bot management, and the sourcing decision

**Audit date:** 2026-09-04. **Method:** direct `GET /robots.txt` per origin plus one
homepage request for response-header/cookie fingerprinting. Raw responses are committed
verbatim under `data/raw/robots_snapshot/` so any claim below can be re-checked.

This file is evidence, not narrative. The scraping engine reads
`config/sources.yaml`, whose `tier` values are set by the verdicts here.

---

## 1. Findings

| Source | Flight-search path | Search allowed for `*`? | Bot management | Tier |
|---|---|---|---|---|
| **Akasa Air** | `/flight-booking/...` | **Yes** — zero `Disallow` rules | none detected | **1 — primary** |
| **SpiceJet** | `/` booking flow | **Yes** — `Disallow:` empty | none detected | **1 — primary** |
| **Air India** | `/in/en/book-flights/...` | **Yes** — search not listed | **Akamai Bot Manager** | **3 — excluded** |
| **Yatra** | domestic flight search | **Yes** — `*` is `Allow: /` | **Akamai Bot Manager** | **3 — excluded** |
| IndiGo | `/booking/*`, `/book/*` | **No** — explicitly disallowed | Akamai Bot Manager | 4 — excluded |
| Air India Express | `/flight-availability` | **No** — explicitly disallowed | none detected | 4 — excluded |
| MakeMyTrip | `/flight/search*`, `/air/*` | **No** — explicitly disallowed | Akamai Bot Manager | 4 — excluded |
| Cleartrip | `/flights/search*` | **No** — explicitly disallowed | not probed | 4 — excluded |
| EaseMyTrip | `/flight-search/listing*` | **No** — explicitly disallowed | not probed | 4 — excluded |
| Ixigo | `/flights/search` | **No** — explicitly disallowed | not probed | 4 — excluded |
| Goibibo | `/flights/*?*` | **No** — all query URLs disallowed | not probed | 4 — excluded |

**Tier 2** is reserved for licensed API sources (see §4); no scraped source occupies it.

### Headline result

**Every OTA in the problem statement except Yatra forbids automated access to flight
search in its own robots.txt.** The excluded set is not a capability limit — those
sites are scrapable — it is a compliance decision. Seven of the eleven audited
sources are out on robots.txt grounds alone: IndiGo, Air India Express,
MakeMyTrip, Cleartrip, EaseMyTrip, Ixigo and Goibibo.

---

## 2. Verbatim evidence

**Akasa Air** — the entire file. No `Disallow` directive exists:

```
User-Agent: *
Sitemap: https://www.akasaair.com/sitemap.xml
Sitemap: https://www.akasaair.com/book-flight-tickets/sitemap_index.xml
```

**SpiceJet** — `Disallow:` with an empty value means *allow everything*:

```
User-agent: *
Disallow: 
Disallow: /cgi-bin/
Disallow: https://www.spicejet.com/api/v1
Disallow: https://www.spicejet.com/public/
```

The last three lines are malformed: RFC 9309 requires a path, not an absolute URL, so a
literal parser ignores them. **We honour their evident intent anyway** and never request
`/api/v1`, `/public/` or `/externalBooking`. Honouring intent over letter is the whole
posture of this project, and `apix/ethics/robots.py` implements it as an explicit
`intent_denied` rule set rather than a comment.

**IndiGo** — flight booking is disallowed outright:

```
Disallow: /bookings/*
Disallow: /book/*
Disallow: /booking/*
Disallow: /search.html
```

**MakeMyTrip**, **Cleartrip**, **EaseMyTrip**, **Ixigo**, **Goibibo** — the operative lines:

```
MakeMyTrip   Disallow: /flight/search*      Disallow: /air/*      Disallow: /flight/fis*
Cleartrip    Disallow: /flights/search*     Disallow: /m/flights/search*
EaseMyTrip   Disallow: /flight-search/listing*
Ixigo        Disallow: /flights/search      Disallow: /api/
Goibibo      Disallow: /flights/*?*         Disallow: /flights/*?mode=*
```

Goibibo's rule deserves a note: `/flights/*?*` disallows every `/flights/` URL carrying a
query string. Since a fare search is unrepresentable without query parameters, the rule is
a complete prohibition on fare scraping even though the word "search" never appears.

**Yatra** — the one OTA whose `*` group permits domestic flight search. It also names
crawlers individually, and one of those groups is directly relevant:

```
User-agent: *
Allow: /
...
User-agent: ClaudeBot
Allow: /
Crawl-delay: 5
```

---

## 3. Bot-management fingerprinting

Four origins resolve to Akamai edge nodes and set Akamai Bot Manager cookies. Observed
directly in response headers:

| Host | DNS CNAME chain | Cookies set on first response |
|---|---|---|
| `www.goindigo.in` | `→ edgesuite.net → a1993.dscr.akamai.net` | `ak_bmsc`, `AKA_A2` |
| `www.airindia.com` | `→ edgekey.net → e175785.dsca.akamaiedge.net` | `ak_bmsc`, `bm_s`, `bm_so` |
| `www.makemytrip.com` | `→ edgekey.net → e38444.dscj.akamaiedge.net` | `bm_s`, `bm_so` |
| `www.yatra.com` | `23.208.178.25` (Akamai range) | `ak_bmsc`, `ak_time` |

`ak_bmsc` / `bm_s` / `bm_so` are Akamai Bot Manager's device-fingerprint and
session-integrity cookies. Their presence means these origins actively score automated
traffic and will challenge or throttle it.

Akasa Air and SpiceJet returned no such cookies. Akasa's response header is a bare
`server: Webserver` with no CDN bot layer in front of it.

---

## 4. The sourcing decision, and why coverage loses to defensibility

**Air India and Yatra are the hard cases.** Both permit flight search in robots.txt, so
robots is not the obstacle. Both sit behind Akamai Bot Manager, which means a scraper only
succeeds by defeating device fingerprinting — rotating residential proxies, spoofed TLS
fingerprints, randomised user-agents chosen to look human.

That technique is available and it is the reason we are excluding them. robots.txt states
a site's automation policy declaratively; Bot Manager enforces it. Complying with the first
while defeating the second is not compliance, and a system built for the National
Statistical Office cannot rest on a control the operator is visibly trying to enforce. So:

> **Rule: where a site deploys active bot management, we do not scrape it, even when
> robots.txt would permit us to.** Being blocked is an answer, not an obstacle.

This costs real coverage. Air India and IndiGo together carry the majority of Indian
domestic passengers, and excluding them means APIx is computed from a **carrier-restricted
sample**. That bias is quantified, not hidden — see `docs/METHODOLOGY.md` §6, which
reports the observed-carrier share of each route's traffic alongside every index value.

**Legitimate routes to the missing carriers,** to be evaluated before scraping is ever
reconsidered:

1. **Licensed GDS/aggregator APIs** (Sabre, Travelport, Amadeus Enterprise). These
   return a native `base` / `taxes` / `total` breakdown, removing the hardest parsing
   problem in the project, and carry an explicit commercial licence. This is Tier 2.
   Amadeus **Self-Service** was the obvious candidate and has been evaluated and
   rejected — see §4A, which also explains why the Tier-2 route is narrower than it
   looks.
2. **A MoSPI/DGCA data-sharing request.** DGCA's own Tariff Monitoring Unit already
   collects fares from airline websites on 78 routes monthly. A statistical system built
   for the ministry should ask for that feed rather than re-scrape around it.

---

## 4A. Amadeus Self-Service evaluated as a Tier-2 source — rejected

Evaluated 8–9 September 2026. **Verdict: not viable, on two independent grounds,
either of which is sufficient.** No account was created and no credentials were
entered; this is a documentary evaluation.

### Ground one: the product no longer exists

Amadeus decommissioned the Self-Service tier on **17 July 2026**, seven weeks
before this evaluation. The homepage of the developer portal now carries:

> "Amadeus for Developers self-service portal has been decommissioned on July
> 17th, this website is for Amadeus Enterprise API Portal only"
> — [developers.amadeus.com](https://developers.amadeus.com/)

The timeline, reported 9 February 2026: registration for new users paused in
March 2026, the portal decommissioned and **all Self-Service API keys disabled**
on 17 July ([PhocusWire](https://www.phocuswire.com/amadeus-shut-down-self-service-apis-portal-developers)).

Verified independently at the DNS layer on 9 September 2026 from this machine:

```
$ dig +short A api.amadeus.com          (no answer)
$ dig +short A test.api.amadeus.com     (no answer)
$ dig +short A developers.amadeus.com   l4e2jxx.x.incapdns.net. 45.60.126.249
$ dig +short A amadeus.com              107.154.251.68
```

The two API hostnames return NOERROR with zero A records while the corporate
hosts resolve normally, so this is the endpoints being withdrawn rather than a
local network restriction — a distinction worth making explicitly, because one
of the checks behind this section initially could not resolve *any* Amadeus host
and correctly declined to treat that as evidence.

There is therefore no key to obtain and no contract to sign. The only remaining
Amadeus route is the Enterprise portal: a negotiated commercial contract, sales-led
pricing, an account manager, and IATA/ARC licensing to ticket. That is a
procurement exercise, not a signup form, and it is a different question from the
one this section was asked.

### Ground two: it would not have closed the coverage gap anyway

This matters more than the shutdown, because it is the part that generalises to
the other GDSs.

**Amadeus Self-Service categorically excluded low-cost-carrier content.** Its own
documentation: *"LCC content is not available through the Self-Service APIs"*, and
*"Flights from low-cost carriers, American Airlines, Delta and British Airways are
unavailable."* That is a content-class rule, not a per-airline gap that a
conversation could have closed.

Four of the five Indian domestic carriers are LCCs — IndiGo, SpiceJet, Akasa and
Air India Express. So:

| Carrier | Domestic share | Available via Amadeus Self-Service? |
|---|---:|---|
| IndiGo | 63.9% | **No.** LCC, and reaches Amadeus only through NDC (agreement 16 Sept 2024, live UAE and Singapore first). Amadeus: *"NDC content is available only through the Enterprise framework."* IndiGo has never filed traditional GDS fares in any GDS. |
| Air India | 15.2% | Yes — mainline only. The carrier we already cannot scrape for a *different* reason (bot management). |
| Air India Express | 11.5% | Not confirmed. No primary source shows an Amadeus agreement; the October 2023 Amadeus domestic-content release covers Air India mainline only. |
| Akasa Air | 5.3% | **No.** Its first and only global distribution agreement (28 February 2026) is with **Travelport**, which also holds preferred distribution status for Akasa in India. |
| SpiceJet | 3.0% | **No.** LCC. |

The realistic yield was **Air India mainline alone, roughly a quarter of the
market** — and it would have missed IndiGo's 63.9% entirely. The Tier-2 route was
never the answer to the coverage problem it was filed under. Correcting that
claim is the most useful thing this evaluation produced.

### Ground three, had the first two not applied: the licence

The published terms would have prohibited this project's use. The
*Amadeus for Developers Portal Terms of Use* (version dated 28 June 2019):

| Clause | Effect |
|---|---|
| §3.1.1 | Use permitted *"for testing and prototyping purposes only"* |
| §3.3(a) | No commercial use without a separate agreement |
| §3.3(b) | No publishing or making the data available to third parties |
| §3.3(f) | No *"scrape, data mine, build databases, or otherwise create permanent copies"* |
| §5.1(a) | The data itself is Confidential Information, surviving termination |

§3.3(f) is the one that bites hardest and most unambiguously: a price index *is* a
retained time series, so accumulating one is exactly the prohibited act.

**Two honest caveats on this ground.** First, every publicly retrievable Amadeus
developer terms document is scoped by §1.1(b) to the **test environment only**;
§1.2 says production is governed by separate terms *"which we will communicate to
you when you enter such sections"*, and those were signed individually by DocuSign
and never published. **What Self-Service *production* actually permitted cannot be
established from public documents and is not asserted here.** Second, the terms
nowhere address aggregated or derived statistics and contain no aggregate carve-out,
so whether a published index *number* counts as "the Materials, in whole or in
part" is genuinely undetermined by the text. §3.3(f) makes that ambiguity moot,
which is why it is the clause named.

Note for anyone re-checking: `developers.amadeus.com/legal/terms-of-use` now
301-redirects to `amadeus.com/en/policies/terms-of-use`, which is the generic
corporate **website** policy and self-describes as covering "the use of the Site".
It is not the API licence and must not be cited as one.

### Two findings worth keeping regardless

**The sandbox never served live fares.** Amadeus documented test data as
*"Limited, cached"* against production's *"Unlimited, real-time"*; Flight Offers
Search in test returned *"Cached data including most origin and destination
cities/airports"*, and some endpoints were frozen fixtures dated **November 2021** —
about five years stale at shutdown. Any accuracy work done in the sandbox would
have been measuring a fixture. This is a general warning about GDS sandboxes, not
a fact about Amadeus alone, and it is the first thing to check about Sabre or
Travelport.

**GDS fares are not website fares.** Self-Service returned *"published GDS rates
only"*, with negotiated and special rates excluded, and GDS-channel bookings carry
per-segment distribution fees a direct consumer never pays. For an index whose
price concept is *the lowest total fare a traveller actually pays* (§3.1 of
METHODOLOGY.md), a GDS feed is **a different product**, not a cheaper way to
observe the same one. Mixing it into the existing series without a level
adjustment would introduce a step change indistinguishable from a real price
movement.

For the record, the final published Self-Service limits: 2,000 free Flight Offers
Search calls per month, 10 TPS in test and 40 TPS in production, and €0.025 per
call in production above the free quota. Recovered from Wayback captures of
Amadeus's own quota endpoint and docs (31 May and 5 July 2026); the currency is
inferred from the published "€0.0008 to €0.025" range rather than read off a
rendered page. They describe a discontinued product and are recorded only so the
order of magnitude is on file.

### What this changes

`config/sources.yaml` gains no Amadeus entry and no `FareSource` stub is written.
Writing an adapter against an API that cannot be called would be scaffolding
pretending to be capability.

The honest state of the Tier-2 route is that **it has not been established that any
GDS can supply IndiGo's 63.9%**, and the evidence points the other way: IndiGo
distributes through NDC into enterprise channels only, and has never filed classic
GDS fares. Akasa's agreement is with Travelport. Any future evaluation should start
by asking a GDS the direct question — *can you return IndiGo domestic fares at an
Indian point of sale?* — because that single answer determines whether the Tier-2
route is worth pursuing at all.

### Not evaluated

The following were in scope and were **not** completed, and are listed rather than
quietly dropped:

- **Sabre Dev Studio and Travelport** — free tiers, Indian domestic coverage,
  licence posture. Travelport is now the most promising of the three, since it
  holds Akasa's distribution agreement and Akasa is a carrier we currently scrape.
- **Indian aggregator APIs** (TripJack, Verteil, and similar) that resell airline
  content under a partner agreement.
- **Airline direct / NDC partner programmes** for IndiGo, Air India and Akasa.
- **DGCA Tariff Monitoring Unit** — whether its 78-route monthly fare collection
  can be shared with another ministry, and whether any airfare dataset exists on
  data.gov.in.

---

## 5. Reference sources (non-fare)

| Source | Purpose | robots.txt | Access notes |
|---|---|---|---|
| `esankhyiki.mospi.gov.in` | Official CPI series for the overlay | `User-agent: * / Allow: /` | Fully permissive |
| `api.mospi.gov.in/api/esankhyiki/` | CPI JSON endpoints | — | **Requires `ssl.OP_LEGACY_SERVER_CONNECT`**; modern OpenSSL rejects its renegotiation and fails with `unsafe legacy renegotiation disabled`. Handled in `apix/reference/mospi_cpi.py`. |
| DGCA city-pair traffic (via `Vonter/india-aviation-traffic`) | Route weights | — | ODbL-1.0, sourced from DGCA monthly statistics, 2015→2026-05. Attribution carried in `data/reference/`. |
| DGCA Tariff Monitoring Unit | Back-test ground truth | — | 78 routes, ~27% of domestic traffic, monthly |

Note on `api.mospi.gov.in`: its `/api-docs` endpoint serves a Swagger UI for an internal
**CMS admin** API, including a login route and an internal `10.24.89.x` server address.
That surface is out of scope and is not touched; we use only the public data endpoints
referenced by the eSankhyiki front-end.

---

## Addendum: a compliance bug found by cross-checking, 5 September 2026

Running a live evidence pass against this audit surfaced a disagreement. The
audit recorded Cleartrip as `do_not_scrape` because its robots.txt contains
`Disallow: /flights/search*`, yet the fetch layer allowed `/flights/search` and
actually sent the request.

The cause was `urllib.robotparser`, which the gate used at the time. It has two
defects, and both permit fetches a site intended to forbid:

1. **No wildcard support.** `/flights/search*` is treated as a literal prefix
   including the asterisk, so `/flights/search` does not match it. Cleartrip,
   MakeMyTrip, Goibibo and Ixigo all write their flight-search prohibitions with
   a trailing `*`.
2. **First match, not most specific.** RFC 9309 section 2.2.2 requires the
   longest matching pattern to win. Cleartrip opens with `Allow: /` and lists its
   prohibitions below, so under first-match semantics the opening line wins every
   comparison and the rest of the file is dead text.

The gate now uses `apix/ethics/matcher.py`, an RFC 9309 implementation covering
wildcards, `$` anchoring, longest-match precedence, allow-wins-ties, and
most-specific user-agent group selection. `tests/test_robots_matcher.py` pins the
behaviour against each source's real robots.txt, and one test asserts the
divergence from the standard library so the fix cannot be silently reverted.

The audit trail now records the deciding rule rather than only the verdict, so a
reviewer can see which line of which file permitted or refused each request.

**One request was sent to Cleartrip that should not have been.** It was a single
GET to a search URL, which returned 404, and no fare data was collected from it.
It is recorded in `request_audit`. The rate limit meant the exposure was one
request rather than a campaign, which is the argument for keeping such limits
low independent of any belief that the compliance logic is correct.
