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

1. **Licensed GDS/aggregator APIs** (Amadeus Self-Service, Sabre, Travelport). These
   return a native `base` / `taxes` / `total` breakdown, removing the hardest parsing
   problem in the project, and carry an explicit commercial licence. This is Tier 2 and
   the correct long-term answer for full-market coverage.
2. **A MoSPI/DGCA data-sharing request.** DGCA's own Tariff Monitoring Unit already
   collects fares from airline websites on 78 routes monthly. A statistical system built
   for the ministry should ask for that feed rather than re-scrape around it.

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
