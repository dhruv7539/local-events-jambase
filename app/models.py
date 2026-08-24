"""Our own event domain model.

Deliberately *not* JamBase's shape. Every upstream quirk is absorbed by the
provider adapter before it reaches these types, so routes and the UI never see
a vendor field name, an empty-string-for-null, or a price expressed as a string.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


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
    def headliner(self) -> Performer | None:
        for performer in self.performers:
            if performer.is_headliner:
                return performer
        return self.performers[0] if self.performers else None

    @property
    def genres(self) -> list[str]:
        """Genres of the headliner, de-duplicated, order preserved."""
        headliner = self.headliner
        return list(dict.fromkeys(headliner.genres)) if headliner else []
