"""Tests for the JamBase adapter.

The fixture is a real, unedited slice of a JamBase response captured from the
live API. It was chosen to contain the cases that actually break naive
adapters: a cancelled show, a rescheduled show carrying a `previousStartDate`,
a date-only festival with no showtime, an event with no offers at all, and
offers whose prices arrive as strings.

No network access. The transport is stubbed with httpx.MockTransport.
"""

from __future__ import annotations

import json
from datetime import date, time
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
from app.providers.jambase import JamBaseProvider

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
    result = await events.fetch_events("Austin, TX", 7)
    assert len(result) == len(RAW_EVENTS)


@pytest.mark.asyncio
async def test_empty_string_previous_date_becomes_none(events):
    """JamBase sends "" rather than null. It must not become a date."""
    result = await events.fetch_events("Austin", 7)
    scheduled = [e for e in result if e.status is EventStatus.SCHEDULED]
    assert scheduled and all(e.previous_date is None for e in scheduled)


@pytest.mark.asyncio
async def test_rescheduled_event_keeps_original_date(events):
    result = await events.fetch_events("Austin", 7)
    moved = by_name(result, "Lefty Gunplay")
    assert moved.status is EventStatus.RESCHEDULED
    assert moved.previous_date == date(2026, 9, 2)


@pytest.mark.asyncio
async def test_cancelled_event_is_kept_not_dropped(events):
    """A cancelled show is the thing a user most needs to see, so it is
    surfaced and flagged rather than filtered out."""
    result = await events.fetch_events("Austin", 7)
    assert by_name(result, "Kill Bill").status is EventStatus.CANCELLED


@pytest.mark.asyncio
async def test_date_only_event_has_no_invented_time(events):
    result = await events.fetch_events("Austin", 7)
    festival = by_name(result, "Bat Fest")
    assert festival.event_date == date(2026, 9, 5)
    assert festival.event_time is None


@pytest.mark.asyncio
async def test_datetime_event_keeps_its_showtime(events):
    result = await events.fetch_events("Austin", 7)
    assert by_name(result, "Bob Schneider").event_time == time(20, 30)


@pytest.mark.asyncio
async def test_string_prices_are_coerced_to_decimal(events):
    """The spec declares `number`; the API sends "15.00"."""
    result = await events.fetch_events("Austin", 7)
    priced = by_name(result, "Bob Schneider").price_range
    assert priced is not None
    assert priced.min_price == Decimal("15.00")
    assert priced.currency == "USD"
    assert priced.min_price <= priced.max_price


@pytest.mark.asyncio
async def test_event_without_offers_has_no_ticket_link_or_price(events):
    result = await events.fetch_events("Austin", 7)
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
