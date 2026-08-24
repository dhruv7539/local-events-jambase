"""Our own event domain model.

Deliberately *not* JamBase's shape. Every upstream quirk is absorbed by the
provider adapter before it reaches these types, so routes and the UI never see
a vendor field name, an empty-string-for-null, or a price expressed as a string.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class EventStatus(str, Enum):
    """Lifecycle status of an event.

    Mirrors JamBase's vocabulary because it is the schema.org vocabulary, not
    because we are coupling to the provider. UNKNOWN exists so an unrecognised
    upstream value degrades instead of failing the whole request.
    """

    SCHEDULED = "scheduled"
    POSTPONED = "postponed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def needs_attention(self) -> bool:
        """True when the status is something a ticket buyer must not miss."""
        return self in {
            EventStatus.POSTPONED,
            EventStatus.RESCHEDULED,
            EventStatus.CANCELLED,
        }


class PriceRange(BaseModel):
    """A single-currency price range across an event's priced ticket offers.

    Only ever constructed when every priced offer agrees on currency; see
    `JamBaseProvider._build_price_range`. Mixed-currency events expose no
    price at all rather than a meaningless span.
    """

    min_price: Decimal
    max_price: Decimal
    currency: str


class Performer(BaseModel):
    name: str
    is_headliner: bool = False
    genres: list[str] = Field(default_factory=list)


class Venue(BaseModel):
    name: str
    city: str | None = None
    region: str | None = None
    street_address: str | None = None
    capacity: int | None = None


class Event(BaseModel):
    """A live music event, normalised."""

    id: str
    name: str
    url: str | None = None

    status: EventStatus = EventStatus.SCHEDULED
    # Populated only for rescheduled/postponed events: the date this event was
    # originally meant to happen, so a buyer can spot a moved show.
    previous_date: date | None = None

    # JamBase always knows the day; it does not always know the showtime.
    # event_time stays None rather than defaulting to midnight.
    event_date: date
    event_time: time | None = None

    venue: Venue
    performers: list[Performer] = Field(default_factory=list)

    ticket_url: str | None = None
    price_range: PriceRange | None = None

    @property
    def _headliner(self) -> Performer | None:
        for performer in self.performers:
            if performer.is_headliner:
                return performer
        return self.performers[0] if self.performers else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def headliner(self) -> str | None:
        """Name of the billed headliner, falling back to the first performer."""
        performer = self._headliner
        return performer.name if performer else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def genres(self) -> list[str]:
        """Headliner's genres, de-duplicated, order preserved."""
        performer = self._headliner
        return list(dict.fromkeys(performer.genres)) if performer else []


class SearchResult(BaseModel):
    """The outcome of one event search, including how complete it is.

    A bare `list[Event]` cannot express whether it is the whole answer. JamBase
    routinely reports far more matching events than one page returns (268 for a
    30-day Austin search against the 100 we request), and a list alone presents
    that truncated slice as if it were everything — which is the same class of
    error as showing $0 for an unpublished price.

    `total_available` is `None` when the provider did not report a trustworthy
    total. Absent metadata is reported as absent, never as a number we do not
    trust.
    """

    events: list[Event]
    total_available: int | None = None
    resolved_location: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def returned_count(self) -> int:
        """Always equals len(events).

        Derived rather than stored so the invariant cannot be violated by a
        caller: there is no way to construct a SearchResult whose count
        disagrees with its events.
        """
        return len(self.events)
