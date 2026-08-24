# Writeup

**Time spent: _[TODO: fill in]_**

## Technology choices

**FastAPI + Pydantic v2.** Required by the brief, but it also earns its place:
the same type annotations that validate `days` as an integer in 1–90 also
generate the OpenAPI schema, so the request contract is declared once rather
than validated in one place and documented in another. Pydantic is doing real
work here, not decoration — the upstream data needs coercion (see below), and
doing that in model validators keeps it out of the route handlers.

**httpx.AsyncClient.** Async because this service is I/O-bound on a third-party
API and does nothing else; one upstream call blocking a worker thread would be
the entire cost model. A single client is created in the app's lifespan and
reused, so connection pooling and TLS session reuse survive across requests —
which matters when a cold search costs two upstream calls.

**No database.** A deliberate scope decision, not an oversight. Nothing in this
app needs durable state: results are derived from an upstream API, and the only
state worth keeping is a short-lived cache. Adding Postgres would have meant
migrations, a connection pool, and a docker-compose file, all to store data
whose authoritative copy lives elsewhere and goes stale in minutes.

**Vanilla JS, no framework.** The UI is one form and one list. React would have
added a build step and a `node_modules` directory to a page that renders a list.

**Dependency versions are lower-bounded, not pinned.** Exact pins broke the
install on Python 3.14, where the pinned `pydantic-core` had no wheel and tried
to compile from source. A production service would pin exactly via a lockfile;
for something a reviewer clones and runs on an unknown interpreter, lower bounds
are the safer default. That tradeoff is noted in `requirements.txt` itself.

## Backend / API design

**The Protocol boundary is the load-bearing decision.** `EventProvider` declares
`fetch_events(location, days) -> SearchResult` and the error types it may raise.
Routes depend on that Protocol via `Depends`; `app/routes.py` never imports
`JamBaseProvider`. The concrete class is named in exactly one place —
`app/main.py`, inside the lifespan — so swapping the upstream source is a
one-line change. `JamBaseProvider` satisfies the Protocol *structurally* and
does not inherit from it, which keeps it a contract rather than a base class.

**Results are an envelope, not a bare list.** `fetch_events` returns a
`SearchResult` carrying `events`, `total_available`, `returned_count` and
`resolved_location`. This started as a bare `list[Event]` and was changed after
looking at real output: JamBase reports 262 matching events for a 30-day Dallas
search and we return one page of 100, so a list alone presented a truncated
slice as though it were the whole answer. `returned_count` is derived from
`len(events)` rather than stored, so the two cannot disagree. `total_available`
is `None` when the provider reported no trustworthy total — including when it
reports a total *smaller* than the page it just sent, which is discarded rather
than passed through. The UI says "Showing the first 100 of 262" when truncated,
a plain count when complete, and states explicitly that the total is unknown
when it is. Upstream `pagination` never leaves the adapter.

**`Event` is our model, not JamBase's.** This is the difference between an
adapter and a passthrough, and the JamBase response makes the case concretely.
Profiling a real 100-event response turned up three quirks that would otherwise
leak into routes and UI:

1. **Prices arrive as strings.** The OpenAPI spec declares
   `priceSpecification.minPrice` as `type: number`; the API sends `"15.00"`.
   `_parse_price` coerces to `Decimal`.
2. **Absent values are empty strings, not null.** `previousStartDate` and
   `doorTime` come back as `""`. Without normalisation, `previous_date` would be
   a truthy empty string and the UI would render "Moved from " on every event.
   `_clean` collapses `""` to `None`.
3. **Date granularity is mixed.** `startDate` is usually
   `2026-08-24T19:00:00` but is sometimes a bare `2026-08-24`. Modelling this as
   a single datetime would force a fake midnight. `Event` therefore carries
   `event_date: date` and `event_time: time | None`.

A fourth turned up while building: `addressRegion` is an **object** on
`/events` but a **plain string** on `/geographies/cities`. One helper absorbs
both.

**Location requires two upstream calls, and that shaped the cache.** `GET
/events` has no city-name parameter — its geo filters are `geoCityId`,
`geoMetroId`, lat/lng radius, `geoStateIso`, `geoCountryIso2/3`, `geoIp` — and
it rejects unknown parameters with a hard `400` rather than ignoring them
(`?geoCityName=Austin` returns `Unknown Query Parameter`). So a free-text
location must first be resolved through `/geographies/cities`. That is why
there are two caches with different TTLs: events expire in 5 minutes because
statuses and offers move, while city name→ID is effectively permanent and is
cached for 24 hours. On a trial key that halves the cost of every repeat search
against a new date range.

City search is a fuzzy keyword match — "Austin" also returns Austintown, OH — so
`_pick_city` sorts deterministically: a caller-supplied region wins, then an
exact name match, then most upcoming events, then lowest ID for stability. A
region hint that matches nothing returns 404 rather than falling back to a city
in the wrong state.

**Failures are translated at the boundary.** The provider converts
`httpx.TimeoutException` → `ProviderTimeout` (504), 401 → `ProviderAuthError`
(500, because bad credentials are our bug, not the caller's), and any other
upstream failure → `ProviderUnavailable` (502). A registered exception handler
turns these into `{"detail": "..."}`. Upstream response bodies are logged but
never re-raised, so provider internals can't leak through an error path — there
is a test asserting exactly that. Verified live: with the upstream blackholed,
`/events` returns a clean 504 and no stack trace, while `/health` still returns
200.

**Cache, retry and rate limiting are three different things, and only two are
implemented.** The **cache** avoids unnecessary upstream requests. The **retry**
handles transient failures: exactly one extra attempt on 429/502/503/504 and on
connection-level errors, after a 250ms delay. Neither is a **rate limiter** —
there is no token bucket and no client-side quota throttling in this code at all.
JamBase returns no `X-RateLimit-*` headers on any response I inspected, so there
is no documented budget to throttle against and I did not invent one; given a
documented budget I would add a token bucket in front of the provider.

The retry is bounded at one attempt on purpose. This is interactive
request/response traffic, so every additional attempt spends the user's latency
and our provider quota to buy a shrinking increment of recovery probability.
Two deliberate exclusions: deterministic 4xx responses (400/401/403/404) are
never retried because the answer will not change, and read/write timeouts are
not retried either — unlike a refused connection, a read timeout means the
request already consumed its entire budget, so a second attempt would double an
interactive user's wait on an upstream already known to be slow. A `Retry-After`
is honoured only when it is a small number of seconds; a long or unparseable
value fails cleanly rather than stalling the request.

## UI design decisions

**A static page that calls the JSON API, not a server-rendered template.** The
consequence is that `GET /events` is the only data path — the UI exercises it
over `fetch` exactly the way any external client would, so the API can't quietly
grow a shape that only works for our own template.

**Three signals beyond name/date/venue**, chosen for someone deciding which show
to attend:

1. **Event status.** Cancelled, postponed and rescheduled shows are badged, and
   cancelled titles are struck through. They are never filtered out — a
   cancellation is the single most important thing a would-be attendee can learn,
   so hiding it would be the worst possible behaviour. Rescheduled events also
   print "Moved from {original date}" using `previousStartDate`, because knowing
   a show moved is more actionable than knowing only that it did.
2. **Artist context.** The billed headliner and up to three of their genres. In
   the sample data 67 of 100 events had a single performer and the rest had
   multi-act bills, so identifying who is actually headlining is genuine
   information, and genre is the fastest way to decide whether an unfamiliar
   name is worth clicking.
3. **Tickets and best-effort price.** A ticket link, preferring the official
   primary link over resale. Price shows as a range when available and
   **"Price not published"** when not — never `$0`, never a guess. Only ~24% of
   offers in the sample carried a price, so absence is the common case and had to
   be designed for rather than treated as an edge case. Prices are never
   aggregated across currencies: if priced offers disagree on currency, the event
   reports no price rather than emitting something like "$15–€80".

**Deliberately left out:** genre and date filtering, artist images and bios,
festival lineups, venue capacity in the UI, maps, sorting controls, and
`/streams`. Each is defensible; none helps more than the three above, and every
one of them is more UI to get wrong.

Errors and loading states are handled in the JS, so a provider failure renders
the API's message as a readable sentence rather than an empty page. All
provider-supplied strings are inserted with `textContent`, never `innerHTML`.

## Tradeoffs made to keep it simple

- **One page of results.** A single upstream page of up to 100 events — the
  maximum the JamBase spec documents for `perPage` — and no pagination. Dallas
  over 30 days has 262 matching events; this app returns 100. It now *says* so,
  in both the API response and the UI, which converts a silent correctness bug
  into a disclosed limitation. It does not make the result complete.
- **In-memory cache, single process.** No Redis. The cache is per-process, so it
  is cold on every deploy and useless behind more than one worker.
- **No background expiry.** Entries are evicted on read. A key never queried
  again holds its memory until the process exits.
- **No auth, no rate limiting on our own endpoint.** Anyone who can reach this
  service can spend our upstream quota.
- **No linter or type-checker.** 72 tests cover normalisation, city selection,
  failure translation, retry policy, caching and the HTTP routes, but nothing
  enforces style or checks the type annotations that the design leans on.
- **The UI was verified late, and looking at it found three defects.** I built
  the whole UI from `curl` output before ever seeing it render. When I finally
  screenshotted it (headless Chrome, light and dark), three problems were
  immediately visible that no amount of JSON inspection would have caught:
  JamBase names events `"{artist} at {venue}"`, so every card printed the artist
  twice and the venue twice; the artist pill was styled identically to genre
  pills, so the two were indistinguishable; and cancelled shows still displayed
  a price range. A fourth appeared after the first fix — a tribute act rendered
  as a full-width pill because the title used a hyphen where the performer name
  used an en-dash, so my substring de-duplication missed it, and a
  `text-transform: capitalize` intended for lowercase genre slugs was mangling
  proper nouns. A fifth surfaced only once I opened it in a *real* browser
  rather than headless: this machine reports `navigator.language` as `en-US` but
  resolves its format locale to `en-GB`, so passing `undefined` to `Intl`
  followed the OS region and rendered every showtime as "19:00" and every price
  as "US$15". I had written that off as a headless artifact that would resolve
  on a normal machine; it was not. All five are fixed. The lesson is the process
  one: I should have looked at the page hours earlier, and "it'll be fine in a
  real browser" was an assumption I should have tested rather than asserted.

## What I'd change or add with more time

1. **Actual pagination.** The truncation is now *disclosed* ("showing the first
   100 of 262"), which is the correctness fix, but the missing events are still
   missing. Following `pagination.nextPage` with a page cap is the next step,
   and the reason it wasn't done is quota, not difficulty: five pages per search
   is five times the upstream spend on a trial key.
2. **A stale-while-revalidate cache**: serve slightly stale results when the
   upstream is down instead of returning 504 on a cache miss during an outage.
3. **Jittered backoff**, if a second retry were ever justified — the current
   policy is a single fixed 250ms delay, which is fine for one attempt but would
   synchronise clients if it ever became a loop.
4. **Structured logging** with a request ID threaded through to the provider
   call, so an upstream failure can be traced to a specific user request.
5. **Filters that matter**: date range beyond the presets, and genre — the data
   is already normalised for both.

## How I used AI

I used Claude Code throughout, and the work split unevenly.

Where it was most valuable was **API reconnaissance**. The published docs page is
a JavaScript single-page app that yields nothing to a fetcher; the model located
the real OpenAPI spec at `data.jambase.com/openapi.json` and, more usefully,
pulled a live 100-event sample and profiled field density before any code was
written. That profiling is what produced the string-prices, empty-string-null and
mixed-date findings, and the fact that only 24% of offers carry a price. Those
shaped the model and the UI copy. I would not have found them by reading the
schema, because the schema is wrong about the first one.

It also wrote most of the boilerplate — models, cache, error hierarchy, tests —
faster than I would have.

Where I had to steer it was **scope and product judgment**. Left alone it
proposed a second location input mode (lat/lng radius), which was cut as scope
creep, and a richer return type than the `list[Event]` I had specified, which I
also cut at the time — correctly, because the argument for it was aesthetic and
I had been explicit about the contract. What makes that one worth recording is
what happened next: after seeing real output showing 60 events returned against
268 available, I reversed my own constraint and adopted the envelope. The right
call twice, on different evidence — rejecting it as unjustified architecture,
then adopting it once there was a concrete correctness failure to point at.

I kept `AI_LOG.md` during the build recording each override, flag and judgment
call as it happened rather than reconstructing it afterwards.

[VERIFY] — this section is written in my voice from the log; check it reads like
you before submitting.

## One specific thing AI suggested that I changed

It proposed making **resale market depth** — the count of distinct secondary
marketplaces listing each event — a headline UI signal. The supporting data was
real: 181 of 278 ticket offers in the sample were secondary listings, spread
across Viagogo, StubHub, Vivid Seats, SeatGeek, TickPick and others, which is a
reasonable proxy for liquidity.

I rejected it. The reasoning behind the suggestion was that the employer is a
ticket-resale trading desk — but the product requirement is a normal person
deciding which event to attend, and that person does not care how many
marketplaces are listing a show. The model had optimised for who would be
*reading* the case study rather than who would be *using* the product, which is a
subtle enough mistake that it produced a genuinely well-argued feature nobody
asked for. Replaced with headliner and genre, which is what actually helps
someone choose a show.

I also corrected its justification for the UI architecture. It argued for
server-rendered templates on the grounds that this avoids drift between the HTML
and JSON paths — but both routes already awaited the same provider call, so
there was no drift to avoid. The real argument runs the other way: a static page
consuming `fetch` makes the JSON API the only data path and exercises it the way
an external client would. Same decision space, but the stated reason was wrong,
and a wrong reason survives into the next decision.

## The biggest technical limitation

**The results ceiling.** The app fetches one upstream page — now 100 events, the
maximum `perPage` the JamBase spec documents — and a 30-day Dallas search has
262 matching events. A user searching a busy city over a month sees under half
of what exists, and because results are date-ordered, the missing ones are
systematically the later ones.

This is not theoretical. Before the page size was raised, rendering a 30-day
Austin search produced exactly 60 cards, and neither the rescheduled show on
1 September nor the festival on 5 September was among them — both existed in the
data and both were silently dropped. The app's headline feature is surfacing
status changes, and the truncation was hiding them.

What changed during this pass is honesty, not completeness. `SearchResult` now
reports `total_available` alongside `returned_count`, and the UI says "Showing
the first 100 of 262" instead of implying it has everything. That converts a
silent correctness bug into a stated limitation — a real improvement, and not
the same thing as fixing it. **The missing events are still missing.** Following
`pagination.nextPage` is a small loop; it was left out because on a trial key
each additional page multiplies quota spend per search, which is a cost decision
rather than an engineering one.

Second place, and closer than I'd like: the in-memory cache means the quota
protection this app relies on disappears the moment it runs more than one
worker.

## Evolving to 10 providers

The Protocol boundary is the part that already works, and I'd expect it to hold.
`fetch_events(location, days) -> SearchResult` is a shape any ticketing API can
satisfy, and this is now demonstrated rather than asserted: the HTTP tests drive
the real FastAPI routes with a `FakeEventProvider` that shares no code with
`JamBaseProvider`, injected through `dependency_overrides`. If the boundary
leaked a JamBase detail, those tests could not pass. What follows are the things
that would *actually* break at ten:

**1. Fan-out and merge.** A `CompositeProvider` that itself satisfies
`EventProvider`, wrapping the ten and calling them with `asyncio.gather`. Routes
would not know the difference. Total latency becomes the slowest provider, so it
needs a per-provider timeout budget shorter than the overall one.

**2. Partial failure becomes the normal case.** With ten upstreams, one being
down is routine, and the current error model is all-or-nothing: today any
`ProviderError` fails the whole request. A composite would need to return partial
results — which sources answered, which failed, and a degraded flag — rather
than a 502.

`SearchResult` is the right *shape* for that and is why the change is now
plumbing rather than a contract break: a bare list can only say "here is the
answer", whereas an envelope has room for completeness metadata to grow into. To
be precise about what exists today, though: `SearchResult` currently carries
`total_available`, `returned_count` and `resolved_location` for a **single**
provider. It implements no multi-provider partial-failure handling, no
per-provider status, and no warnings collection. Those are fields and logic that
would need to be added; the envelope just means adding them wouldn't force every
caller to change.

**3. Deduplication, which is the genuinely hard part.** Ten providers will list
the same concert. There is no shared ID, so matching means fuzzy-matching on
(normalised artist, venue, date) with venue aliasing — "Madison Square Garden"
vs "MSG" — and a merge policy deciding which source wins per field. I would not
attempt this generically; I'd build a venue alias table and accept a false-merge
rate, measured against a hand-labelled sample. Everything else on this list is
plumbing. This one is a product problem wearing an engineering costume.
[VERIFY] — that last line is a flourish; cut it if it doesn't sound like you.

**4. Per-provider isolation.** Independent timeouts, an independent cache
namespace, and a circuit breaker so a provider that is timing out stops being
called for a cooldown rather than adding its full timeout to every request.

**5. Configuration over code.** Ten providers means credentials, base URLs,
enable/disable flags and rate budgets per source — a registry built from config
at startup, rather than ten constructor calls in `main.py`.

**6. Shared caching.** Per-process caching is already the weak point at one
provider; at ten it stops being defensible. This is where Redis earns its place,
and where the "no database" call would be revisited.

## Self-assessment

| Area | Grade | Reasoning |
|---|---|---|
| **Code quality** | **B+** | Clean layering, honest naming, and 72 tests covering normalisation, retry policy, cache expiry and the HTTP routes, built on real captured data — but no linter, type-checker or CI is configured, so the type annotations the design leans on are never actually verified. |
| **Work product** | **B+** | Every requirement is verified live, including all failure paths; opening the UI caught five real rendering defects, now fixed; and the truncation is disclosed rather than silent. Still short of the mark: I built the UI blind for far too long, and a busy 30-day search returns 100 of 262 events — disclosed, but still incomplete. |
| **Extensibility** | **A−** | The boundary is proven, not asserted: the real routes run against a non-JamBase provider, and the result envelope leaves room for completeness and per-provider metadata to grow into. Short of an A because it still has exactly one real implementation — the abstraction has never met a second API's shape, which is the only test that truly counts. |

Not straight A's, and it shouldn't be: this is a timeboxed build with a results
ceiling it discloses but does not fix, no linting or type-checking, a UI I
didn't look at until after it was written, and an abstraction that hasn't yet
met its second case.
