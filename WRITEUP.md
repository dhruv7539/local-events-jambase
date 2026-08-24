# Local Events — Technical Writeup

**Time spent:** 2 Hours

## Technology choices

I used **FastAPI** for the backend and **Pydantic v2** for request validation and domain models. FastAPI was required by the prompt, and Pydantic was also useful for normalizing some inconsistencies in the JamBase data before exposing it to the rest of the application.

For calls to JamBase, I used a single **`httpx.AsyncClient`** created during the FastAPI lifespan. The application is mostly waiting on an external API, so async I/O is a good fit. Reusing one client also allows connection pooling across requests.

The frontend is plain **HTML, CSS, and vanilla JavaScript**. The UI only needs a location search and a list of event cards, so adding React or another frontend framework would have added complexity without solving a problem I had in this scope.

I deliberately did **not** add a database. The application does not own any durable data: event information comes from JamBase and changes over time. A small in-memory cache was enough for this version.

## Backend / API design

The main backend design decision was keeping JamBase behind an `EventProvider` Protocol.

```text
FastAPI routes
      |
EventProvider
      |
JamBaseProvider
```

The routes depend on `EventProvider`, not directly on `JamBaseProvider`. JamBase-specific authentication, request parameters, parsing, and normalization stay inside the adapter.

I also test the FastAPI routes using a `FakeEventProvider`. That was important because it proves that the route layer can work with something other than JamBase without changing the route logic.

### Normalized event model

The API returns my own `Event` model instead of passing JamBase responses directly to the frontend.

Looking at real JamBase responses exposed several cases where that separation was useful:

- Prices can arrive as strings even though the OpenAPI schema describes them as numbers.
- Some missing values arrive as empty strings instead of `null`.
- `startDate` can be either a full local datetime or only a date.
- Region information is shaped differently between the events and geography endpoints.

The JamBase adapter handles those differences before creating an `Event`.

For time specifically, I use:

```text
event_date: date
event_time: time | None
```

If JamBase only provides a date, the application shows the time as unavailable instead of inventing a midnight start time.

### Location resolution

JamBase's events endpoint does not accept a free-text city name directly, so searching for a location such as `Austin, TX` requires two upstream steps:

1. Resolve the city through the JamBase geography endpoint.
2. Fetch events using the resolved JamBase city ID.

City search can be fuzzy. For example, searching for `Austin` can also return places such as Austintown. When a region is supplied, the resolver filters to that region first, then prefers an exact city-name match, followed by a deterministic fallback.

Because city IDs are much more stable than event data, I use two cache lifetimes:

- **City resolution:** 24 hours
- **Event results:** 5 minutes

This reduces unnecessary API calls while keeping event information reasonably fresh.

### Search result envelope

`fetch_events()` returns a `SearchResult` rather than only `list[Event]`.

It contains:

```text
events
total_available
returned_count
resolved_location
```

I originally used a bare list. After testing against real data, I found that a busy 30-day search could have more events than JamBase returns on one page. Returning only the list made a partial result look complete.

The API and UI now make that explicit. For example:

> Showing the first 100 of 262 upcoming events

`returned_count` is computed from `len(events)` so those values cannot disagree. If JamBase does not provide a trustworthy total, the application does not invent one.

### Error handling and reliability

Provider failures are translated into a small set of application-level errors before reaching the route layer.

Examples:

- location not found → `404`
- provider unavailable → `502`
- provider timeout → `504`
- provider authentication/configuration failure → sanitized `500`

Raw upstream response bodies and authentication details are not returned to the client.

The JamBase client also retries selected transient failures exactly once. The retry applies to connection-level errors and HTTP `429`, `502`, `503`, and `504` responses. Deterministic client errors are not retried.

I intentionally do not retry read timeouts. A read timeout can already consume most of the latency budget for an interactive request, so immediately trying another full read can make the user wait much longer without much remaining time to recover.

There are two timeout levels:

- `httpx` timeouts bound an individual upstream request.
- A **12-second overall search deadline** bounds the full city-resolution + event-fetch workflow, including retries.

The overall deadline was not part of the original design. I added it after implementing retries and noticing that multiple retryable upstream calls could make the total user-visible wait much larger than any individual request timeout. The overall deadline keeps that combined workflow bounded.

HTTP `500` is not retried in this implementation. I kept the retry policy deliberately narrow. A `500` can also be transient, so excluding it is a judgment call rather than a protocol guarantee. With production telemetry from the provider, I would revisit which responses are worth retrying.

Caching, retries, and rate limiting are separate concerns:

- the cache reduces unnecessary upstream requests;
- retries handle selected transient failures;
- this implementation does **not** have client-side quota throttling or a token bucket.

I did not see a documented request budget in the JamBase responses I inspected, so I did not invent one.

`GET /health` is intentionally a **liveness** endpoint for this application. It does not call JamBase. A third-party outage should not make the FastAPI process appear dead, and health checks should not consume upstream quota.

## UI design decisions

I used a static page that calls `GET /events` with `fetch()`. This keeps the JSON endpoint as the single data path and makes the frontend consume the API the same way another client would.

Beyond event name, date, venue, and location, I chose three signals that I thought would actually help someone decide whether to attend an event.

### 1. Event status

Cancelled, postponed, and rescheduled events are clearly marked.

Cancelled events remain visible rather than disappearing from the results, but their ticket link and pricing are removed. Rescheduled events also show the previous date when JamBase provides it.

### 2. Artist context

Cards show the headliner and a small amount of genre information when available. This gives the user some context about an unfamiliar event without turning the page into an artist-detail application.

### 3. Tickets and pricing

The app provides a ticket link, preferring the primary ticket link when one is available.

Pricing is best-effort because JamBase does not publish it consistently. When there is no usable price, the UI says:

> Price not published

I do not convert missing prices to `$0`, and prices from different currencies are not combined into one range.

The UI also has explicit loading and error states so an upstream failure produces a readable message instead of an empty page.

I deliberately left out maps, recommendations, accounts, favorites, artist biographies, sorting controls, and additional filters. They could be useful later, but they were less important than getting the core event-discovery flow and backend behavior right.

## Tradeoffs made to keep it simple

- **One JamBase page per search.** I request up to 100 events, the maximum documented `perPage`, but do not fetch additional pages. The UI clearly tells the user when the result is incomplete.
- **In-memory caching.** It works for a single process but is not shared between workers or instances.
- **No database.** There is no durable application state in the current product.
- **No authentication or API-side rate limiting.** A public deployment could consume the upstream quota.
- **No frontend framework.** Vanilla JavaScript is enough for the current interface.
- **No full production toolchain.** I focused the timebox on application behavior and tests rather than adding CI, a linter, type-checking, or a full observability stack.

One process decision I would change is doing visual verification earlier. The API and automated tests were passing while the first version of the UI still had presentation problems, including duplicated information and pricing being shown on cancelled events. Rendering the application in a browser exposed those issues quickly, and I fixed them before finishing.

## What I would change or add with more time

The first thing I would add is **bounded pagination**. The application now tells the user when results are incomplete, but the additional events are still missing. I would follow JamBase's pagination with a reasonable page or event limit instead of automatically retrieving every result.

After that, I would consider:

1. **Stale-while-revalidate caching** so recently fetched events could still be shown during a temporary JamBase outage.
2. **Structured request logging** with request IDs to make provider failures easier to trace.
3. **Provider-aware rate limiting** if JamBase provides a documented quota.
4. **Useful filters**, especially genre and a custom date range.
5. **A shared cache such as Redis** if the application needed multiple workers or instances.

## How I used AI

I used **Claude Code** throughout development for API research, implementation, testing, and code review.

The most useful part was checking the real JamBase API before building around assumptions. I used it to inspect the OpenAPI specification and sample real responses. That exposed differences between the documented schema and actual data, including string prices, empty strings for missing values, and mixed date/date-time fields. Those findings directly affected the normalization layer.

AI also helped scaffold parts of the provider, models, cache, tests, and UI. I treated that output as a starting point rather than assuming it was correct. I checked the implementation against the requirements, real API responses, automated tests, and the rendered application.

I also kept `AI_LOG.md` during development to record meaningful decisions, corrections, and changes in direction as they happened.

## One specific thing AI suggested that I changed

Claude initially suggested showing **resale market depth** — roughly, how many secondary marketplaces were listing an event — as a major UI signal.

The reasoning made sense in the context of a ticket-resale company, but it did not fit the actual product in the prompt. The user of this application is someone deciding which event to attend, not a ticket trader. That user is more likely to care about whether the event was cancelled or rescheduled, what type of artist is performing, whether tickets are available, and what they may cost.

I rejected the resale-market-depth feature and instead prioritized event status, headliner/genre context, and ticket availability with best-effort pricing.

That was a useful example of why I still needed to make the product decisions even when AI could produce a reasonable technical argument for a feature.

## Biggest technical limitation

The biggest limitation is **pagination**.

The application fetches one JamBase page of up to 100 events. A 30-day search in a busy city can have substantially more results, so the user may only see the earliest part of the requested range.

This caused a real issue during development. Before increasing the page size, a 30-day Austin search returned the first 60 events while later events, including a rescheduled event, were outside the returned page.

I changed the response contract so `SearchResult` includes both `returned_count` and `total_available`. The UI now makes truncation explicit instead of presenting a partial result as complete.

That improves correctness, but it does not fix completeness. The missing events are still missing.

With more time, I would follow JamBase's pagination using a bounded page limit. I left that out because every additional page consumes another request on the trial API key.

A second limitation is the in-memory cache. It is per-process, so it would need to move to shared storage if the application ran across multiple workers or instances.

## How I would evolve the architecture for 10 event providers

I would keep the existing `EventProvider` interface and implement one adapter for each provider.

Each adapter would be responsible for:

- authentication;
- provider-specific request parameters;
- pagination;
- retry and timeout behavior;
- provider-specific schema handling;
- normalization into the common `Event` model.

Above those adapters, I would introduce an aggregation layer:

```text
                    Event search service
                           |
          -------------------------------------
          |             |          |          |
       JamBase       Provider B  Provider C  ...
          |             |          |          |
          -------------------------------------
                           |
                    Canonical events
                           |
                         API
```

The providers could be queried concurrently, with separate timeout budgets so one slow provider does not block the entire search.

### Partial failures

With ten providers, it would be normal for one source to be unavailable occasionally. The current application uses an all-or-nothing provider error model, which would no longer be appropriate.

I would extend `SearchResult` with provider-status or warning metadata so the API could return useful results from the providers that succeeded while clearly stating that the result is partial.

The current `SearchResult` does **not** implement that behavior. It only provides completeness information for the single JamBase provider, but using an envelope gives that metadata somewhere to grow without changing the whole API contract again.

### Cross-provider event matching

The hardest part would likely be **entity resolution**.

Multiple providers may describe the same concert differently:

```text
Coldplay at Madison Square Garden
Coldplay - New York
COLDPLAY: Music of the Spheres
```

Simply concatenating provider results would create duplicates.

I would introduce canonical event IDs and map provider-specific event IDs to them using signals such as:

- performer identity;
- venue identity;
- local start time;
- normalized names;
- provider-specific external IDs where available.

Ambiguous matches should use confidence thresholds rather than being merged automatically.

At that scale I would also move the cache to something shared such as Redis and give each provider its own credentials, timeout policy, retry policy, cache namespace, and request budget.

## Self-assessment

| Area | Grade | Reasoning |
|---|---|---|
| **Code quality** | **A−** | The code has clear separation between the API, domain models, and provider adapter, with **80 tests** covering normalization, HTTP behavior, provider substitution, retries, caching, and timeouts. I did not add CI, linting, or static type checking within the timebox. |
| **Work product** | **A−** | The application works end to end against real JamBase data, handles important event and failure states, makes incomplete results explicit, and was manually verified in a real browser. The main gap is that busy searches are still limited to the first 100 events. |
| **Extensibility** | **A−** | JamBase-specific behavior is isolated behind `EventProvider`, and the route layer is tested with a separate fake implementation. Supporting multiple real providers would still require aggregation, partial-failure handling, and cross-provider event matching. |
