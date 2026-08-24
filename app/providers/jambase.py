"""JamBase adapter: the only module in the app that knows JamBase exists.

Everything vendor-shaped is absorbed here — the two-call location flow, the
schema.org field names, and three concrete data-quality quirks found by
profiling a real 100-event response:

1. Prices arrive as strings ("15.00") though the spec declares `type: number`.
2. Absent `previousStartDate` / `doorTime` are empty strings, not null.
3. `startDate` is usually a datetime but is sometimes date-only, and
   `addressRegion` is an object on /events but a plain string on
   /geographies/cities.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.cache import TTLCache
from app.config import Settings
from app.models import Event, EventStatus, Performer, PriceRange, SearchResult, Venue
from app.providers.base import (
    LocationNotFound,
    ProviderError,
    ProviderAuthError,
    ProviderTimeout,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

PRIMARY_TICKET_CATEGORY = "ticketingLinkPrimary"

# --- Retry policy -----------------------------------------------------------
# Exactly one retry. This is interactive request/response traffic: every extra
# attempt spends the user's latency and our provider quota to buy a shrinking
# increment of recovery probability. One retry catches the common case (a single
# transient blip) without turning a slow upstream into a slow page.
#
# This is not rate limiting, and it is not the cache. The cache avoids
# unnecessary requests; the retry handles transient failures. Neither throttles
# our own request rate — no token bucket is implemented (see WRITEUP.md).
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.25
# The longest upstream-requested wait we will honour before failing cleanly.
MAX_RETRY_AFTER_SECONDS = 2.0

# Transient by nature: the request never got a considered answer.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

# Connection-level failures, which typically fail fast and often succeed on a
# second attempt. Deliberately EXCLUDES ReadTimeout/WriteTimeout: those mean the
# request already consumed its entire time budget, so retrying would double an
# interactive user's wait to buy one more chance at a slow upstream.
RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


def _clean(value: Any) -> str | None:
    """JamBase uses "" where JSON null is meant. Collapse both to None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_date(value: Any) -> date | None:
    """Parse a JamBase date or datetime string down to a calendar date."""
    text = _clean(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_date_and_time(value: Any) -> tuple[date | None, time | None]:
    """Split a JamBase `startDate` into a date and an optional time.

    JamBase omits the time component entirely when the showtime is unknown
    (verified against a 100-event sample: not one event used T00:00:00 as a
    sentinel). So a date-only string means "time unknown" and midnight is
    treated as a genuine showtime, never invented and never discarded.

    Note these timestamps are venue-local with no UTC offset; we preserve them
    as written rather than shifting them into a timezone JamBase did not state.
    """
    text = _clean(value)
    if text is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _parse_date(text), None
    if "T" not in text:
        return parsed.date(), None
    return parsed.date(), parsed.time()


def _parse_price(value: Any) -> Decimal | None:
    """Coerce a price that the API sends as a string despite declaring number."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _region_code(region: Any) -> str | None:
    """`addressRegion` is an object on /events and a plain string on /cities."""
    if isinstance(region, dict):
        return _clean(region.get("alternateName")) or _clean(region.get("identifier"))
    return _clean(region)


class JamBaseProvider:
    """Fetches and normalises events from the JamBase v3 API.

    Satisfies `EventProvider` structurally — it deliberately does not inherit
    from it, so the Protocol stays a contract rather than a base class.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._events_cache: TTLCache[SearchResult] = TTLCache(settings.event_cache_ttl)
        self._city_cache: TTLCache[dict[str, str]] = TTLCache(settings.city_cache_ttl)

    # ---------------------------------------------------------------- public

    async def fetch_events(self, location: str, days: int) -> SearchResult:
        city = await self._resolve_city(location)

        cache_key = f"{city['id']}:{days}"
        cached = await self._events_cache.get(cache_key)
        if cached is not None:
            logger.info("events cache hit for %s", cache_key)
            return cached

        today = date.today()
        payload = await self._get(
            "/events",
            {
                "geoCityId": city["id"],
                "eventDateFrom": today.isoformat(),
                # Inclusive of today, so days=1 means "today only".
                "eventDateTo": (today + timedelta(days=days - 1)).isoformat(),
                "perPage": self._settings.results_per_page,
            },
        )

        events = []
        for raw in payload.get("events") or []:
            event = self._normalise_event(raw)
            if event is not None:
                events.append(event)

        result = SearchResult(
            events=events,
            total_available=self._total_available(payload, len(events)),
            resolved_location=city["label"],
        )
        await self._events_cache.set(cache_key, result)
        return result

    @staticmethod
    def _total_available(payload: dict[str, Any], returned: int) -> int | None:
        """Normalise JamBase's pagination block into a total we trust, or None.

        The upstream `pagination` shape stops here; callers see only an integer
        or None. A total smaller than the page we just received is internally
        inconsistent, so it is discarded rather than reported — an absent total
        is honest, a wrong one is worse than nothing.
        """
        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            return None
        total = pagination.get("totalItems")
        if not isinstance(total, int) or isinstance(total, bool) or total < returned:
            logger.info("discarding untrustworthy totalItems %r", total)
            return None
        return total

    # ------------------------------------------------------------- location

    async def _resolve_city(self, location: str) -> dict[str, str]:
        """Resolve a free-text location to a JamBase city.

        `/events` has no city-name filter and rejects unknown parameters with a
        400, so a name must be resolved to a `geoCityId` first. Cached for a
        day because city IDs never move.
        """
        query = location.strip()
        if not query:
            raise LocationNotFound("Please enter a location.")

        cache_key = query.casefold()
        cached = await self._city_cache.get(cache_key)
        if cached is not None:
            return cached

        # Accept "Austin, TX" as well as "Austin".
        name, _, region_hint = query.partition(",")
        name = name.strip()
        region_hint = region_hint.strip().upper() or None

        payload = await self._get(
            "/geographies/cities", {"geoCityName": name, "perPage": 30}
        )
        candidates = payload.get("cities") or []
        if not candidates:
            raise LocationNotFound(f"No location matching {query!r} was found.")

        best = self._pick_city(candidates, name, region_hint)
        if best is None:
            raise LocationNotFound(f"No location matching {query!r} was found.")

        city_name = best.get("name") or name
        region = _region_code((best.get("address") or {}).get("addressRegion")) or ""
        # "US-TX" -> "TX"; the API is inconsistent about which form it sends.
        short_region = region.split("-")[-1] if region else ""
        city = {
            "id": best["identifier"],
            "name": city_name,
            "region": short_region,
            # Pre-formatted for display so the place we actually searched is
            # visible to the caller, e.g. Austin, TX rather than Austintown, OH.
            "label": ", ".join(part for part in (city_name, short_region) if part),
        }
        await self._city_cache.set(cache_key, city)
        return city

    @staticmethod
    def _pick_city(
        candidates: list[dict[str, Any]], name: str, region_hint: str | None
    ) -> dict[str, Any] | None:
        """Deterministically choose one city from a fuzzy name search.

        JamBase's city search is a keyword match — "Austin" also returns
        Austintown, OH. Rather than building a location-picker product, we sort
        by explicit rules: a caller-supplied region wins, then an exact name
        match, then the city with the most upcoming events, then the lowest ID
        so the choice is stable across identical requests.
        """
        pool = candidates
        if region_hint:
            in_region = [
                c
                for c in candidates
                if (_region_code((c.get("address") or {}).get("addressRegion")) or "")
                .upper()
                .endswith(region_hint)
            ]
            # A region hint that matches nothing is treated as a filter that
            # failed, not as a reason to return a city in the wrong state.
            if not in_region:
                return None
            pool = in_region

        def sort_key(city: dict[str, Any]) -> tuple[int, int, str]:
            exact = (city.get("name") or "").casefold() == name.casefold()
            upcoming = city.get("x-numUpcomingEvents") or 0
            return (0 if exact else 1, -int(upcoming), str(city.get("identifier") or ""))

        return sorted(pool, key=sort_key)[0]

    # ------------------------------------------------------------ transport

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue one upstream request, retrying once on transient failure.

        Upstream bodies are logged but never re-raised to the caller, so
        provider internals cannot leak through an error response.
        """
        url = f"{self._settings.jambase_base_url}{path}"
        headers = {"Authorization": f"Bearer {self._settings.jambase_api_key}"}

        for attempt in range(1, MAX_ATTEMPTS + 1):
            final_attempt = attempt == MAX_ATTEMPTS
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except RETRYABLE_TRANSPORT_ERRORS as exc:
                if final_attempt:
                    logger.warning("jambase transport error on %s: %s", path, exc)
                    raise self._transport_failure(exc) from exc
                logger.info(
                    "jambase transport error on %s (attempt %d), retrying: %s",
                    path, attempt, exc,
                )
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            except httpx.TimeoutException as exc:
                # Read/write timeout: the budget is already spent. Not retried.
                logger.warning("jambase timeout on %s: %s", path, exc)
                raise ProviderTimeout() from exc
            except httpx.HTTPError as exc:
                logger.warning("jambase transport error on %s: %s", path, exc)
                raise ProviderUnavailable() from exc

            if response.status_code in RETRYABLE_STATUSES and not final_attempt:
                delay = self._retry_delay(response)
                if delay is None:
                    logger.warning(
                        "jambase returned %s on %s and asked for a wait too long "
                        "for an interactive request; failing",
                        response.status_code, path,
                    )
                    raise ProviderUnavailable()
                logger.info(
                    "jambase returned %s on %s (attempt %d), retrying in %.2fs",
                    response.status_code, path, attempt, delay,
                )
                await asyncio.sleep(delay)
                continue

            return self._read_payload(response, path)

        # Unreachable: the final attempt always returns or raises above.
        raise ProviderUnavailable()

    @staticmethod
    def _transport_failure(exc: Exception) -> ProviderError:
        """Connect timeouts are still timeouts; everything else is unavailability."""
        if isinstance(exc, httpx.TimeoutException):
            return ProviderTimeout()
        return ProviderUnavailable()

    @staticmethod
    def _retry_delay(response: httpx.Response) -> float | None:
        """How long to wait before the retry, or None to fail immediately.

        Honours `Retry-After` only when it is a small number of seconds. A long
        or unparseable value (including the HTTP-date form) means we fail
        cleanly rather than either ignoring the provider's instruction or
        blocking an interactive request for an unbounded time.
        """
        raw = response.headers.get("Retry-After")
        if raw is None:
            return RETRY_DELAY_SECONDS
        try:
            requested = float(raw.strip())
        except (AttributeError, ValueError):
            return None
        if requested > MAX_RETRY_AFTER_SECONDS:
            return None
        return max(requested, 0.0)

    def _read_payload(self, response: httpx.Response, path: str) -> dict[str, Any]:
        """Translate a final (non-retryable) response into a payload or error."""
        if response.status_code == 401:
            logger.error("jambase rejected our credentials on %s", path)
            raise ProviderAuthError()
        if response.status_code >= 400:
            logger.warning(
                "jambase returned %s on %s: %s",
                response.status_code, path, response.text[:500],
            )
            raise ProviderUnavailable()

        try:
            return response.json()
        except ValueError as exc:
            logger.warning("jambase returned non-JSON on %s", path)
            raise ProviderUnavailable() from exc

    # ---------------------------------------------------------- normalising

    def _normalise_event(self, raw: dict[str, Any]) -> Event | None:
        """Map one JamBase event onto our model.

        Returns None for an event we cannot place in time or name, rather than
        raising: one malformed record should not fail an entire search.
        """
        event_date, event_time = _parse_date_and_time(raw.get("startDate"))
        name = _clean(raw.get("name"))
        identifier = _clean(raw.get("identifier"))
        if event_date is None or name is None or identifier is None:
            logger.info("skipping unusable event record %r", identifier)
            return None

        offers = [o for o in (raw.get("offers") or []) if isinstance(o, dict)]

        return Event(
            id=identifier,
            name=name,
            url=_clean(raw.get("url")),
            status=self._normalise_status(raw.get("eventStatus")),
            previous_date=_parse_date(raw.get("previousStartDate")),
            event_date=event_date,
            event_time=event_time,
            venue=self._normalise_venue(raw.get("location") or {}),
            performers=self._normalise_performers(raw.get("performer") or []),
            ticket_url=self._pick_ticket_url(offers),
            price_range=self._build_price_range(offers),
        )

    @staticmethod
    def _normalise_status(value: Any) -> EventStatus:
        try:
            return EventStatus(str(value).strip().lower())
        except ValueError:
            # An unrecognised status must not fail the request, but silently
            # calling it "scheduled" would be a lie about a possibly cancelled
            # show, so it degrades to UNKNOWN.
            logger.info("unrecognised eventStatus %r", value)
            return EventStatus.UNKNOWN

    @staticmethod
    def _normalise_venue(raw: dict[str, Any]) -> Venue:
        address = raw.get("address") or {}
        capacity = raw.get("maximumAttendeeCapacity")
        return Venue(
            name=_clean(raw.get("name")) or "Venue to be announced",
            city=_clean(address.get("addressLocality")),
            region=_region_code(address.get("addressRegion")),
            street_address=_clean(address.get("streetAddress")),
            capacity=int(capacity) if isinstance(capacity, (int, float)) else None,
        )

    @staticmethod
    def _normalise_performers(raw: list[Any]) -> list[Performer]:
        performers = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = _clean(item.get("name"))
            if name is None:
                continue
            genres = [
                g.replace("-", " ")
                for g in (item.get("genre") or [])
                if isinstance(g, str) and g.strip()
            ]
            performers.append(
                Performer(
                    name=name,
                    is_headliner=bool(item.get("x-isHeadliner")),
                    genres=genres,
                )
            )
        return performers

    @staticmethod
    def _pick_ticket_url(offers: list[dict[str, Any]]) -> str | None:
        """Prefer the official/primary ticket link, else any link at all."""
        for offer in offers:
            if offer.get("category") == PRIMARY_TICKET_CATEGORY:
                url = _clean(offer.get("url"))
                if url:
                    return url
        for offer in offers:
            url = _clean(offer.get("url"))
            if url:
                return url
        return None

    @staticmethod
    def _build_price_range(offers: list[dict[str, Any]]) -> PriceRange | None:
        """Best-effort price range across an event's priced offers.

        Only ~24% of offers carry a price at all, so absence is the common
        case and is represented as None — never as zero or an invented range.
        Prices are only aggregated when every priced offer shares one currency;
        a mixed-currency event reports no price rather than "$15-€80".
        """
        prices: list[Decimal] = []
        currencies: set[str] = set()

        for offer in offers:
            spec = offer.get("priceSpecification") or {}
            if not isinstance(spec, dict):
                continue
            values = [
                p
                for p in (
                    _parse_price(spec.get("minPrice")),
                    _parse_price(spec.get("maxPrice")),
                    _parse_price(spec.get("price")),
                )
                if p is not None
            ]
            currency = _clean(spec.get("priceCurrency"))
            if not values or currency is None:
                continue
            prices.extend(values)
            currencies.add(currency.upper())

        if not prices or len(currencies) != 1:
            return None

        return PriceRange(
            min_price=min(prices), max_price=max(prices), currency=currencies.pop()
        )
