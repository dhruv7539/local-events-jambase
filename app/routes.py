"""HTTP surface.

The route handlers depend on the `EventProvider` Protocol via `Depends`. The
concrete JamBase class is never imported here — swapping providers is a change
to `app.main` alone.
"""

from __future__ import annotations

from typing import Annotated

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from app.models import SearchResult
from app.providers.base import EventProvider

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter()


def get_provider(request: Request) -> EventProvider:
    """Resolve the provider bound to the app at startup."""
    return request.app.state.provider


ProviderDep = Annotated[EventProvider, Depends(get_provider)]


@router.get("/health")
async def health() -> dict[str, str]:
    """Application liveness only.

    Deliberately does not call JamBase. A health check that depends on a third
    party turns their outage into our restart loop, and probes fire constantly,
    so calling the provider would consume quota indefinitely for no diagnostic
    gain. Provider availability is surfaced on /events instead.
    """
    return {"status": "ok"}


@router.get("/events")
async def list_events(
    provider: ProviderDep,
    location: Annotated[
        str, Query(min_length=1, max_length=100, description="City name, e.g. 'Austin' or 'Austin, TX'")
    ],
    days: Annotated[int, Query(ge=1, le=90, description="Days ahead to search, inclusive of today")] = 7,
) -> dict[str, object]:
    result: SearchResult = await provider.fetch_events(location, days)
    # `location` echoes what was asked for; `resolved_location` reports what was
    # actually searched, which can differ ("Austin" -> "Austin, TX").
    return {"location": location, "days": days, **result.model_dump(mode="json")}


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the single-page UI. It consumes GET /events like any other client."""
    return FileResponse(STATIC_DIR / "index.html")
