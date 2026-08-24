# AI Log

Short log of decisions where AI surfaced something useful, I changed its suggestion, or real data changed the design.

## Research

- **AI finding:** JamBase's `/events` endpoint does not accept a free-text city name. I changed the design to resolve the city first through `/geographies/cities`, then query events using the returned city ID.
- **AI finding:** Real responses differed from the OpenAPI schema in a few important ways: some prices arrived as strings, missing values appeared as empty strings, and dates were not always full datetimes. I kept those provider-specific quirks inside the JamBase adapter rather than exposing them to the API or UI.
- **AI finding:** Pricing was present on only **67 of 278 offers in a 100-event sample**. I treated pricing as best-effort and made the missing state explicit with "Price not published" instead of assuming `$0`.
- **AI finding:** I did not observe rate-limit headers or a documented request budget in the responses I checked. I used caching to reduce unnecessary calls rather than inventing a client-side quota policy.



## Product decisions

- **I overrode AI:** AI suggested surfacing the **count of secondary marketplaces listing each event** because the company reviewing the project is a ticket-resale business. I rejected it because the actual user in the prompt is someone deciding which event to attend. I prioritized event status, artist context, and ticket availability/pricing instead.
- **I cut scope:** AI suggested supporting a second lat/lng radius-search input. I kept one location-search flow because the additional mode did not earn its place in the timebox.
- **I changed the UI approach:** AI initially suggested server-rendered Jinja templates. I chose a static page calling `/events` with `fetch()` so the JSON API stays the single data path and the UI consumes it the same way an external client would.



## Build and verification

- **Design changed after real data:** I initially required `fetch_events()` to return `list[Event]` and rejected a result wrapper as unnecessary. After a real search returned only **60 of 268 available events**, I reversed that decision and introduced `SearchResult` so partial results could be disclosed honestly. The two events missing from that page were a **rescheduled show and a date-only festival**, exactly the kinds of signals the app was built to surface.
- **Browser verification caught issues tests did not:** Rendering the UI exposed duplicated artist/venue information, unclear artist/genre styling, and pricing still being shown on cancelled events. I fixed those before submission.
- **AI assumption corrected:** A formatting issue was initially dismissed as a headless-browser artifact. Real-browser testing showed the machine reported `navigator.language` as **en-US** but resolved its format locale to **en-GB**, so showtimes were rendering as **19:00** instead of the expected 12-hour format. I corrected the locale handling after verifying it in the real browser.



## Reliability

- **Retry policy:** I kept one retry for selected connection-level failures and HTTP `429`, `502`, `503`, and `504` responses. I did not retry read timeouts because they can already consume most of the latency budget for an interactive request.
- **AI/self-review caught a regression:** Adding retries increased worst-case search latency. A cold search requires **two upstream calls**, so once each became retryable the worst case reached **four requests and roughly 20 seconds**. I added a **12-second overall search deadline** to bound the complete workflow rather than relying only on per-request timeouts.
- **Testing the abstraction:** I added HTTP-level tests using a `FakeEventProvider`, so the provider boundary is exercised through the real FastAPI routes rather than existing only as a type annotation.
- **Final checks:** I ran the full test suite, compared README and writeup claims against the implementation, and scanned both the working tree and git history to confirm the API key was never committed.

