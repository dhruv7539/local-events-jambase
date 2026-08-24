# AI_LOG

Running log of overrides, flags, and judgment calls. Written as they happened, not summarised afterward.

## Research

- **[AI flag]** The `/api/docs` page is a JS single-page app and returns no usable content to a fetcher. Found the real OpenAPI spec at `data.jambase.com/openapi.json` (v3.1.0) and worked from that instead of the rendered docs.
- **[AI flag]** `GET /events` has **no city-name parameter**, and it rejects unknown params with a hard `400` rather than ignoring them (`?geoCityName=Austin` → `Unknown Query Parameter`). Free-text location therefore *requires* a two-call flow: resolve the name via `/geographies/cities`, then query events by `geoCityId`. This is the single fact that shaped the provider's design.
- **[AI flag]** Profiled a real 100-event sample before choosing what to surface. Three spec-vs-reality mismatches found: prices arrive as **strings** (`"15.00"`) though the spec declares `type: number`; absent `previousStartDate`/`doorTime` are **empty strings**, not null; and `startDate` is a datetime while `endDate` is sometimes date-only. These are the concrete justification for normalising in the adapter.
- **[AI flag]** No `X-RateLimit-*` headers on any response, so there is no documented budget to back off against. Cache is the only quota defence available.
- **[AI flag]** Pricing is **not** reliably available — only 67 of 278 offers (24%) carried a price. Flagged that the brief's assumption of pricing data was optimistic, and that this had to ship as a best-effort signal with visible absence rather than a claimed feature.

## Product design

- **[USER OVERRIDE]** I proposed making **resale market depth** (count of distinct secondary marketplaces per event) a headline UI signal, reasoning from the employer being a ticket-resale trading desk — 181 of 278 offers in my sample were secondary listings, which is a genuine liquidity proxy. Dhruv rejected it: the product requirement is a *normal user deciding which event to attend*, not the employer's internal trading workflow, and the UI should be optimised for that user. Replaced with headliner + genre artist context. This was optimising for who was reading the case study rather than who was using the product, and the correction was right.
- **[USER OVERRIDE]** I offered `lat,lng` radius input as a second location mode to show the adapter handling multiple upstream strategies. Cut as scope creep — one location input mode only.

## Build

- **[USER OVERRIDE]** I planned a server-rendered Jinja2 UI, justifying it as avoiding drift between the HTML and JSON paths. Dhruv corrected both the choice and my reasoning: a static page + `fetch` makes the JSON API the *only* data path, exercised the way any external client would use it — and my stated justification was weak, since both routes already awaited the same provider call. Dropped the Jinja dependency.
- **[USER OVERRIDE]** I planned `start_time_known: bool` alongside a datetime. Replaced with `event_date: date` + `event_time: time | None`, which represents what JamBase actually knows instead of encoding it in a side flag.
- **[USER DIRECTION]** Never aggregate prices across currencies. A range is computed only when all priced offers for an event agree on currency; conflicting currencies expose pricing as unavailable rather than emitting something like `$15–€80`.
- **[Judgment call]** I started writing `fetch_events` to return a wrapper object carrying the *resolved* location, so the UI could show that "Austin" resolved to Austin, TX rather than Austintown, OH. That contradicts the specified signature `fetch_events(location, days) -> list[Event]`, which is explicitly part of what's being graded, so I reverted to the exact signature. The disambiguation need is still met: every `Event` carries its venue's city and region, so the resolved place is visible in the results themselves without widening the contract.
- **[Judgment call]** Checked whether JamBase uses `T00:00:00` as a "time unknown" sentinel before writing the date parser. It does not — across 100 real events, 99 carried a genuine showtime and 1 was date-only, with zero midnights. So date-only means unknown and midnight is treated as a real showtime. Guessing either way without checking would have silently mangled late shows.
- **[Judgment call]** Found a fourth upstream inconsistency while writing the adapter: `addressRegion` is an **object** on `/events` but a **plain string** on `/geographies/cities`. Handled by one `_region_code` helper rather than two code paths.
- **[Judgment call]** A region hint that matches no city (e.g. "Austin, ZZ") returns "not found" rather than falling back to the best city in the wrong state. Silently returning Austin, TX events for a query the user scoped elsewhere is worse than an honest miss.
- **[Judgment call]** A single malformed event record is skipped and logged, not raised. One bad row upstream should not blank an entire search.
- **[Judgment call]** An unrecognised `eventStatus` degrades to `UNKNOWN`, not to `SCHEDULED`. Defaulting to "scheduled" would assert a show is on when the provider is telling us something we don't understand.
