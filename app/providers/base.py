"""The provider contract.

Routes depend on `EventProvider` and on nothing else. Adding a second upstream
source means writing a new class that satisfies this Protocol and changing one
line of wiring in `app.main` — no route, model, or UI change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import SearchResult


class ProviderError(Exception):
    """Base for every failure originating upstream.

    Carries a message safe to show a caller. Upstream response bodies are
    logged, never propagated, so provider internals and credentials cannot
    leak through an error path.
    """

    status_code = 502
    default_message = "The event data provider is unavailable."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class ProviderTimeout(ProviderError):
    status_code = 504
    default_message = "The event data provider did not respond in time."


class ProviderUnavailable(ProviderError):
    status_code = 502
    default_message = "The event data provider is temporarily unavailable."


class ProviderAuthError(ProviderError):
    """Our credentials are bad or expired. This is our bug, not the caller's,
    so it surfaces as a 500 with no detail about the upstream service."""

    status_code = 500
    default_message = "The service is misconfigured."


class LocationNotFound(ProviderError):
    """The caller's location string matched no known place."""

    status_code = 404
    default_message = "No matching location was found."


@runtime_checkable
class EventProvider(Protocol):
    """Anything that can turn a human location string into normalised events."""

    async def fetch_events(self, location: str, days: int) -> SearchResult:
        """Return events near `location` starting within the next `days` days.

        Returns a `SearchResult` rather than a bare list so callers can tell a
        complete answer from a truncated one.

        Raises:
            LocationNotFound: `location` matched no known place.
            ProviderTimeout: upstream exceeded the configured timeout.
            ProviderAuthError: our credentials were rejected.
            ProviderUnavailable: any other upstream failure.
        """
        ...
