"""HTTP-layer tests.

These exist to prove provider substitutability rather than assert it. Every
test here drives the real FastAPI routes with `FakeEventProvider` injected
through `app.dependency_overrides` — a class that has never heard of JamBase
and shares no code with it.

    production:  FastAPI -> JamBaseProvider
    these tests: FastAPI -> FakeEventProvider

The route logic is identical in both cases. If the Protocol boundary were
leaking a JamBase detail, these tests could not pass.
"""

from __future__ import annotations

import pathlib
from datetime import date, time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Event, SearchResult, Venue
from app.providers.base import (
    LocationNotFound,
    ProviderAuthError,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.routes import get_provider

SAMPLE = Event(
    id="fake:1",
    name="Test Band at Test Hall",
    event_date=date(2026, 9, 1),
    event_time=time(20, 0),
    venue=Venue(name="Test Hall", city="Austin", region="TX"),
)


class FakeEventProvider:
    """An EventProvider with no relationship to JamBase.

    Either returns a canned SearchResult or raises a provider error, so the
    route's translation of each failure mode can be exercised directly.
    """

    def __init__(self, result: SearchResult | None = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[tuple[str, int]] = []

    async def fetch_events(self, location: str, days: int) -> SearchResult:
        self.calls.append((location, days))
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]


def client_for(provider: FakeEventProvider) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_provider] = lambda: provider
    # No context manager: lifespan never runs, so no real HTTP client is built
    # and no API key is required to exercise the routes.
    return TestClient(app)


def ok_provider(events: list[Event] | None = None, total: int | None = None):
    return FakeEventProvider(
        SearchResult(
            events=SAMPLE if events is None else events,  # type: ignore[arg-type]
            total_available=total,
            resolved_location="Austin, TX",
        )
        if events is not None
        else SearchResult(
            events=[SAMPLE], total_available=total, resolved_location="Austin, TX"
        )
    )


# ---------------------------------------------------------------------- health


def test_health_returns_ok():
    response = client_for(ok_provider()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_call_the_provider():
    """A third-party outage must not make this process look dead."""
    provider = ok_provider()
    client_for(provider).get("/health")
    assert provider.calls == []


# ---------------------------------------------------------------- happy path


def test_events_returns_search_result_metadata():
    provider = ok_provider(total=268)
    response = client_for(provider).get("/events?location=Austin&days=7")
    assert response.status_code == 200

    body = response.json()
    assert body["location"] == "Austin"
    assert body["resolved_location"] == "Austin, TX"
    assert body["days"] == 7
    assert body["total_available"] == 268
    assert body["returned_count"] == 1
    assert body["returned_count"] == len(body["events"])
    assert body["events"][0]["name"] == "Test Band at Test Hall"


def test_returned_count_matches_events_for_a_larger_result():
    provider = ok_provider(events=[SAMPLE, SAMPLE, SAMPLE], total=99)
    body = client_for(provider).get("/events?location=Austin").json()
    assert body["returned_count"] == len(body["events"]) == 3


def test_unknown_total_is_reported_as_null_not_zero():
    body = client_for(ok_provider(total=None)).get("/events?location=Austin").json()
    assert body["total_available"] is None


def test_days_defaults_to_seven():
    provider = ok_provider()
    client_for(provider).get("/events?location=Austin")
    assert provider.calls == [("Austin", 7)]


# ---------------------------------------------------------------- validation


def test_missing_location_is_rejected():
    assert client_for(ok_provider()).get("/events").status_code == 422


def test_empty_location_is_rejected():
    assert client_for(ok_provider()).get("/events?location=").status_code == 422


def test_days_zero_is_rejected():
    assert client_for(ok_provider()).get("/events?location=A&days=0").status_code == 422


def test_days_above_ninety_is_rejected():
    assert client_for(ok_provider()).get("/events?location=A&days=91").status_code == 422


def test_days_boundaries_are_accepted():
    client = client_for(ok_provider())
    assert client.get("/events?location=A&days=1").status_code == 200
    assert client.get("/events?location=A&days=90").status_code == 200


def test_validation_failure_never_reaches_the_provider():
    provider = ok_provider()
    client_for(provider).get("/events?location=Austin&days=0")
    assert provider.calls == []


# --------------------------------------------------------- failure translation


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (LocationNotFound("No matching location was found."), 404),
        (ProviderTimeout(), 504),
        (ProviderUnavailable(), 502),
        (ProviderAuthError(), 500),
    ],
)
def test_provider_errors_map_to_clean_status_codes(error, expected_status):
    response = client_for(FakeEventProvider(error=error)).get("/events?location=A")
    assert response.status_code == expected_status
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"]


def test_auth_failure_response_leaks_nothing():
    """Bad credentials are our problem. The caller learns only that the service
    is misconfigured — not which upstream failed, nor anything key-shaped."""
    response = client_for(FakeEventProvider(error=ProviderAuthError())).get(
        "/events?location=A"
    )
    assert response.status_code == 500
    body = response.json()
    assert body == {"detail": "The service is misconfigured."}

    blob = response.text.lower()
    for leak in ("jambase", "bearer", "authorization", "api_key", "apikey", "token", "401"):
        assert leak not in blob


def test_provider_failure_is_not_an_unhandled_500_traceback():
    response = client_for(FakeEventProvider(error=ProviderTimeout())).get(
        "/events?location=A"
    )
    assert response.status_code == 504
    assert "Traceback" not in response.text


# ------------------------------------------------------------ substitutability


def test_routes_never_import_the_concrete_provider():
    """The architectural claim, asserted against the module's real imports.

    Checks the import graph rather than the text, so a mention in a docstring
    doesn't pass or fail the test for the wrong reason.
    """
    import ast

    import app.routes as routes_module

    tree = ast.parse(pathlib.Path(routes_module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)

    assert not any("jambase" in name.lower() for name in imported), imported
    assert "EventProvider" in imported, "routes should depend on the Protocol"
