"""Tests for the JamBase adapter.

The fixture is a real, unedited slice of a JamBase response captured from the
live API. It was chosen to contain the cases that actually break naive
adapters: a cancelled show, a rescheduled show carrying a `previousStartDate`,
a date-only festival with no showtime, an event with no offers at all, and
offers whose prices arrive as strings.

No network access. The transport is stubbed with httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from datetime import time as time_of_day
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models import EventStatus
from app.providers.base import (
    LocationNotFound,
    ProviderAuthError,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.jambase import (
    MAX_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    JamBaseProvider,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "jambase_events.json").read_text()
)
RAW_EVENTS = FIXTURE["events"]

CITIES = {
    "cities": [
        {
            "identifier": "jambase:4238533",
            "name": "Austintown",
            "address": {"addressRegion": "US-OH"},
            "x-numUpcomingEvents": 5,
        },
        {
            "identifier": "jambase:4218489",
            "name": "Austin",
            "address": {"addressRegion": "US-TX"},
            "x-numUpcomingEvents": 761,
        },
    ]
}


def make_settings(**overrides) -> Settings:
    base = dict(
        jambase_api_key="test-key",
        event_cache_ttl=300,
        city_cache_ttl=86_400,
    )
    return Settings(**(base | overrides))


def make_provider(handler) -> JamBaseProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JamBaseProvider(client, make_settings())


def happy_handler(request: httpx.Request) -> httpx.Response:
    if "/geographies/cities" in request.url.path:
        return httpx.Response(200, json=CITIES)
    return httpx.Response(200, json=FIXTURE)


def by_name(events, fragment):
    return next(e for e in events if fragment in e.name)


# --------------------------------------------------------------- normalising


@pytest.fixture
def events():
    provider = make_provider(happy_handler)
    return provider


@pytest.mark.asyncio
async def test_every_fixture_event_normalises(events):
    result = (await events.fetch_events("Austin, TX", 7)).events
    assert len(result) == len(RAW_EVENTS)


@pytest.mark.asyncio
async def test_empty_string_previous_date_becomes_none(events):
    """JamBase sends "" rather than null. It must not become a date."""
    result = (await events.fetch_events("Austin", 7)).events
    scheduled = [e for e in result if e.status is EventStatus.SCHEDULED]
    assert scheduled and all(e.previous_date is None for e in scheduled)


@pytest.mark.asyncio
async def test_rescheduled_event_keeps_original_date(events):
    result = (await events.fetch_events("Austin", 7)).events
    moved = by_name(result, "Lefty Gunplay")
    assert moved.status is EventStatus.RESCHEDULED
    assert moved.previous_date == date(2026, 9, 2)


@pytest.mark.asyncio
async def test_cancelled_event_is_kept_not_dropped(events):
    """A cancelled show is the thing a user most needs to see, so it is
    surfaced and flagged rather than filtered out."""
    result = (await events.fetch_events("Austin", 7)).events
    assert by_name(result, "Kill Bill").status is EventStatus.CANCELLED


@pytest.mark.asyncio
async def test_date_only_event_has_no_invented_time(events):
    result = (await events.fetch_events("Austin", 7)).events
    festival = by_name(result, "Bat Fest")
    assert festival.event_date == date(2026, 9, 5)
    assert festival.event_time is None


@pytest.mark.asyncio
async def test_datetime_event_keeps_its_showtime(events):
    result = (await events.fetch_events("Austin", 7)).events
    assert by_name(result, "Bob Schneider").event_time == time_of_day(20, 30)


@pytest.mark.asyncio
async def test_string_prices_are_coerced_to_decimal(events):
    """The spec declares `number`; the API sends "15.00"."""
    result = (await events.fetch_events("Austin", 7)).events
    priced = by_name(result, "Bob Schneider").price_range
    assert priced is not None
    assert priced.min_price == Decimal("15.00")
    assert priced.currency == "USD"
    assert priced.min_price <= priced.max_price


@pytest.mark.asyncio
async def test_event_without_offers_has_no_ticket_link_or_price(events):
    result = (await events.fetch_events("Austin", 7)).events
    bare = by_name(result, "Tommy Emmanuel")
    assert bare.ticket_url is None
    assert bare.price_range is None


# ------------------------------------------------------------ unit behaviour


def test_primary_ticket_link_is_preferred_over_secondary():
    offers = [
        {"category": "ticketingLinkSecondary", "url": "https://resale.example"},
        {"category": "ticketingLinkPrimary", "url": "https://official.example"},
    ]
    assert JamBaseProvider._pick_ticket_url(offers) == "https://official.example"


def test_falls_back_to_any_ticket_link_when_no_primary():
    offers = [{"category": "ticketingLinkSecondary", "url": "https://resale.example"}]
    assert JamBaseProvider._pick_ticket_url(offers) == "https://resale.example"


def test_mixed_currencies_produce_no_price_range():
    """$15-€80 is worse than saying nothing, so nothing is what we say."""
    offers = [
        {"priceSpecification": {"price": "15.00", "priceCurrency": "USD"}},
        {"priceSpecification": {"price": "80.00", "priceCurrency": "EUR"}},
    ]
    assert JamBaseProvider._build_price_range(offers) is None


def test_single_currency_spans_all_priced_offers():
    offers = [
        {"priceSpecification": {"minPrice": "20", "maxPrice": "45", "priceCurrency": "USD"}},
        {"priceSpecification": {"price": "12", "priceCurrency": "usd"}},
        {"priceSpecification": {}},
    ]
    price = JamBaseProvider._build_price_range(offers)
    assert price is not None
    assert (price.min_price, price.max_price, price.currency) == (
        Decimal("12"),
        Decimal("45"),
        "USD",
    )


def test_unknown_status_degrades_to_unknown_not_scheduled():
    assert JamBaseProvider._normalise_status("teleported") is EventStatus.UNKNOWN
    assert JamBaseProvider._normalise_status(None) is EventStatus.UNKNOWN


def test_malformed_event_is_skipped_not_raised():
    provider = make_provider(happy_handler)
    assert provider._normalise_event({"name": "No date", "identifier": "x"}) is None


# ----------------------------------------------------------- city resolution


def test_exact_city_name_beats_a_busier_fuzzy_match():
    """'Austin' must not resolve to Austintown, OH."""
    picked = JamBaseProvider._pick_city(CITIES["cities"], "Austin", None)
    assert picked["identifier"] == "jambase:4218489"


def test_region_hint_selects_the_right_state():
    picked = JamBaseProvider._pick_city(CITIES["cities"], "Austin", "OH")
    assert picked["name"] == "Austintown"


def test_region_hint_matching_nothing_returns_none():
    """Better an honest miss than events from the wrong state."""
    assert JamBaseProvider._pick_city(CITIES["cities"], "Austin", "ZZ") is None


@pytest.mark.asyncio
async def test_unknown_location_raises_location_not_found():
    def handler(request):
        return httpx.Response(200, json={"cities": []})

    with pytest.raises(LocationNotFound):
        await make_provider(handler).fetch_events("Nowhereville", 7)


# ------------------------------------------------------- failure translation


@pytest.mark.asyncio
async def test_timeout_becomes_provider_timeout():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ProviderTimeout):
        await make_provider(handler).fetch_events("Austin", 7)


@pytest.mark.asyncio
async def test_401_becomes_auth_error():
    def handler(request):
        return httpx.Response(401, json={"detail": "Access token is not active."})

    with pytest.raises(ProviderAuthError):
        await make_provider(handler).fetch_events("Austin", 7)


@pytest.mark.asyncio
async def test_upstream_500_becomes_provider_unavailable():
    def handler(request):
        return httpx.Response(500, text="upstream exploded")

    with pytest.raises(ProviderUnavailable):
        await make_provider(handler).fetch_events("Austin", 7)


@pytest.mark.asyncio
async def test_upstream_error_body_never_reaches_the_caller():
    def handler(request):
        return httpx.Response(503, text="secret internal hostname db-07.internal")

    with pytest.raises(ProviderUnavailable) as excinfo:
        await make_provider(handler).fetch_events("Austin", 7)
    assert "db-07.internal" not in str(excinfo.value)


# ------------------------------------------------------------------- caching


@pytest.mark.asyncio
async def test_repeat_query_is_served_from_cache():
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return happy_handler(request)

    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    first = len(calls)
    await provider.fetch_events("Austin, TX", 7)
    assert len(calls) == first, "second identical query should make no upstream calls"


@pytest.mark.asyncio
async def test_different_day_range_is_a_separate_cache_entry():
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return happy_handler(request)

    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    await provider.fetch_events("Austin, TX", 30)
    event_calls = [c for c in calls if "/events" in c]
    assert len(event_calls) == 2
    # City resolution is cached separately and for longer, so it happens once.
    assert len([c for c in calls if "/geographies" in c]) == 1


# ------------------------------------------------------- completeness metadata


@pytest.mark.asyncio
async def test_result_reports_total_available_and_resolved_location():
    def handler(request):
        if "/geographies/cities" in request.url.path:
            return httpx.Response(200, json=CITIES)
        return httpx.Response(200, json=FIXTURE | {"pagination": {"totalItems": 268}})

    result = await make_provider(handler).fetch_events("Austin", 7)
    assert result.total_available == 268
    assert result.returned_count == len(result.events) == len(RAW_EVENTS)
    assert result.resolved_location == "Austin, TX"


@pytest.mark.asyncio
async def test_missing_pagination_reports_unknown_total_not_a_guess():
    result = await make_provider(happy_handler).fetch_events("Austin", 7)
    assert result.total_available is None


@pytest.mark.asyncio
async def test_total_smaller_than_page_is_discarded_as_untrustworthy():
    """A total below what we just received is internally inconsistent, so it is
    reported as unknown rather than passed through."""

    def handler(request):
        if "/geographies/cities" in request.url.path:
            return httpx.Response(200, json=CITIES)
        return httpx.Response(200, json=FIXTURE | {"pagination": {"totalItems": 2}})

    result = await make_provider(handler).fetch_events("Austin", 7)
    assert result.total_available is None


def test_returned_count_cannot_disagree_with_events():
    from app.models import SearchResult

    result = SearchResult(events=[], total_available=99, resolved_location="X")
    assert result.returned_count == 0
    assert "returned_count" in result.model_dump()


# --------------------------------------------------------------------- retry


@pytest.fixture
def no_delay(monkeypatch):
    """Keep the retry policy intact but remove the wall-clock cost."""
    monkeypatch.setattr("app.providers.jambase.RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.providers.jambase.MAX_RETRY_AFTER_SECONDS", 0)


def counting_handler(responses):
    """Serve `responses` in order, recording how many attempts were made."""
    attempts: list[httpx.Request] = []

    def handler(request):
        attempts.append(request)
        item = responses[min(len(attempts) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_transient_status_is_retried_once_then_succeeds(status, no_delay):
    handler, attempts = counting_handler(
        [httpx.Response(status), httpx.Response(200, json=CITIES)]
    )
    provider = make_provider(handler)
    await provider._resolve_city("Austin")
    assert len(attempts) == 2, "expected exactly one retry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        pytest.param(httpx.ConnectError("refused"), id="ConnectError"),
        pytest.param(httpx.ConnectTimeout("connect timed out"), id="ConnectTimeout"),
    ],
)
async def test_connection_level_error_is_retried_once_then_succeeds(
    transport_error, no_delay
):
    """Both connection-level failures are retried once and then succeed.

    ConnectTimeout is covered explicitly rather than left to inheritance
    reasoning: it is a TimeoutException, so it would be easy to assume it
    follows the same no-retry path as ReadTimeout. It does not — it is named
    in the retryable tuple, and the retry clause is ordered ahead of the
    TimeoutException clause that would otherwise catch it.
    """
    handler, attempts = counting_handler(
        [transport_error, httpx.Response(200, json=CITIES)]
    )
    city = await make_provider(handler)._resolve_city("Austin")

    assert len(attempts) == 2, "expected exactly one retry"
    # The second attempt genuinely succeeded rather than failing differently.
    assert city["id"] == "jambase:4218489"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_deterministic_4xx_is_never_retried(status, no_delay):
    handler, attempts = counting_handler([httpx.Response(status, text="nope")])
    with pytest.raises((ProviderUnavailable, ProviderAuthError)):
        await make_provider(handler)._resolve_city("Austin")
    assert len(attempts) == 1, "a deterministic client error must not be retried"


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried(no_delay):
    """The request already spent its full budget; retrying doubles the wait."""
    handler, attempts = counting_handler([httpx.ReadTimeout("slow")])
    with pytest.raises(ProviderTimeout):
        await make_provider(handler)._resolve_city("Austin")
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_exhausted_retry_still_produces_a_clean_provider_error(no_delay):
    handler, attempts = counting_handler([httpx.Response(503, text="still down")])
    with pytest.raises(ProviderUnavailable) as excinfo:
        await make_provider(handler)._resolve_city("Austin")
    assert len(attempts) == MAX_ATTEMPTS
    assert "still down" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_exhausted_connect_retry_maps_to_unavailable(no_delay):
    handler, attempts = counting_handler([httpx.ConnectError("refused")])
    with pytest.raises(ProviderUnavailable):
        await make_provider(handler)._resolve_city("Austin")
    assert len(attempts) == MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_long_retry_after_fails_cleanly_instead_of_blocking(no_delay):
    """A provider asking for a 300s wait must not stall an interactive request."""
    handler, attempts = counting_handler(
        [httpx.Response(429, headers={"Retry-After": "300"})]
    )
    with pytest.raises(ProviderUnavailable):
        await make_provider(handler)._resolve_city("Austin")
    assert len(attempts) == 1, "should fail immediately, not retry or sleep"


@pytest.mark.asyncio
async def test_unparseable_retry_after_fails_cleanly(no_delay):
    handler, attempts = counting_handler(
        [httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})]
    )
    with pytest.raises(ProviderUnavailable):
        await make_provider(handler)._resolve_city("Austin")
    assert len(attempts) == 1


def test_short_retry_after_is_honoured():
    response = httpx.Response(429, headers={"Retry-After": "1"})
    assert JamBaseProvider._retry_delay(response) == 1.0


def test_absent_retry_after_uses_our_own_short_delay():
    assert JamBaseProvider._retry_delay(httpx.Response(503)) == RETRY_DELAY_SECONDS


# --------------------------------------------------------------------- cache


class FakeClock:
    """Deterministic monotonic clock, so TTL expiry is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr("app.cache.time.monotonic", fake)
    return fake


def tracking_handler():
    """Handler that records every upstream path it is asked for."""
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return happy_handler(request)

    return handler, calls


def counts(calls: list[str]) -> tuple[int, int]:
    """(city-resolution calls, event calls)"""
    return (
        len([c for c in calls if "geographies" in c]),
        len([c for c in calls if c.endswith("/events")]),
    )


@pytest.mark.asyncio
async def test_identical_query_reuses_the_cached_result(clock):
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    await provider.fetch_events("Austin, TX", 7)
    assert counts(calls) == (1, 1)


@pytest.mark.asyncio
async def test_different_location_is_a_separate_cache_entry(clock):
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    await provider.fetch_events("Austintown, OH", 7)
    cities, events = counts(calls)
    assert cities == 2 and events == 2


@pytest.mark.asyncio
async def test_different_days_is_a_separate_cache_entry(clock):
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    await provider.fetch_events("Austin, TX", 30)
    cities, events = counts(calls)
    assert events == 2, "a different window must not reuse the cached page"
    assert cities == 1, "city resolution should still be cached"


@pytest.mark.asyncio
async def test_expired_event_entry_triggers_a_fresh_request(clock):
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    clock.advance(301)  # event TTL is 300s
    await provider.fetch_events("Austin, TX", 7)
    assert counts(calls)[1] == 2


@pytest.mark.asyncio
async def test_event_entry_still_valid_just_before_expiry(clock):
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    clock.advance(299)
    await provider.fetch_events("Austin, TX", 7)
    assert counts(calls)[1] == 1


@pytest.mark.asyncio
async def test_longer_city_ttl_avoids_repeating_location_resolution(clock):
    """The point of the two-TTL design: stale events are refetched, but the
    city lookup that resolved them is not repeated."""
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)

    clock.advance(3600)  # past the 300s event TTL, well inside the 24h city TTL
    await provider.fetch_events("Austin, TX", 7)

    cities, events = counts(calls)
    assert events == 2, "expired events should be refetched"
    assert cities == 1, "city resolution should NOT be repeated"


@pytest.mark.asyncio
async def test_city_entry_does_eventually_expire(clock):
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    clock.advance(86_401)  # past the 24h city TTL
    await provider.fetch_events("Austin, TX", 7)
    assert counts(calls)[0] == 2


@pytest.mark.asyncio
async def test_cache_key_is_case_insensitive_for_location(clock):
    handler, calls = tracking_handler()
    provider = make_provider(handler)
    await provider.fetch_events("Austin, TX", 7)
    await provider.fetch_events("austin, tx", 7)
    assert counts(calls)[0] == 1, "location casing should not split the city cache"


# ----------------------------------------------------------- global deadline


class SlowTransport(httpx.AsyncBaseTransport):
    """Upstream that never answers in time.

    The pending sleep is cancelled by the deadline, so tests using this finish
    in milliseconds despite naming a long delay — there is no real wait.
    """

    def __init__(self, delay: float = 30.0) -> None:
        self.delay = delay
        self.attempts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        await asyncio.sleep(self.delay)
        return httpx.Response(200, json=CITIES)


def provider_with_deadline(transport: httpx.AsyncBaseTransport, deadline: float):
    client = httpx.AsyncClient(transport=transport)
    return JamBaseProvider(client, make_settings(search_deadline=deadline))


@pytest.mark.asyncio
async def test_overall_deadline_surfaces_as_provider_timeout():
    provider = provider_with_deadline(SlowTransport(), deadline=0.01)
    with pytest.raises(ProviderTimeout):
        await provider.fetch_events("Austin, TX", 7)


@pytest.mark.asyncio
async def test_deadline_actually_bounds_elapsed_time():
    """Proves the cap is enforced, not merely configured."""
    transport = SlowTransport(delay=30.0)
    provider = provider_with_deadline(transport, deadline=0.05)

    started = time.monotonic()
    with pytest.raises(ProviderTimeout):
        await provider.fetch_events("Austin, TX", 7)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"deadline did not bound the search (took {elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_deadline_covers_the_whole_workflow_not_one_call():
    """The city call succeeds; the deadline still fires during the event call,
    which is the case per-request timeouts could not bound."""

    class TwoStageTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.paths: list[str] = []

        async def handle_async_request(self, request):
            self.paths.append(request.url.path)
            if "geographies" in request.url.path:
                return httpx.Response(200, json=CITIES)
            await asyncio.sleep(30.0)
            return httpx.Response(200, json=FIXTURE)

    transport = TwoStageTransport()
    provider = provider_with_deadline(transport, deadline=0.05)
    with pytest.raises(ProviderTimeout):
        await provider.fetch_events("Austin, TX", 7)

    assert any("geographies" in p for p in transport.paths)
    assert any(p.endswith("/events") for p in transport.paths)


@pytest.mark.asyncio
async def test_a_fast_search_is_unaffected_by_the_deadline():
    provider = provider_with_deadline(httpx.MockTransport(happy_handler), deadline=12.0)
    result = await provider.fetch_events("Austin, TX", 7)
    assert result.returned_count == len(RAW_EVENTS)


@pytest.mark.asyncio
async def test_per_request_timeout_still_maps_to_provider_timeout(no_delay):
    """The two layers are distinct: this one fires inside a single request and
    must not be mistaken for the overall deadline."""
    handler, attempts = counting_handler([httpx.ReadTimeout("slow")])
    with pytest.raises(ProviderTimeout):
        await make_provider(handler).fetch_events("Austin", 7)
    assert attempts, "the request was actually attempted"


def test_deadline_sits_above_the_single_request_read_timeout():
    """Otherwise read_timeout would be unreachable dead configuration."""
    settings = make_settings()
    assert settings.search_deadline > settings.read_timeout
