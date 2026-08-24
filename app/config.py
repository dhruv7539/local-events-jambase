"""Configuration, loaded from the environment / .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jambase_api_key: str
    jambase_base_url: str = "https://api.data.jambase.com/v3"

    # Explicit per-phase timeouts. A single blanket timeout would let a slow
    # upstream hold a connection far longer than the read budget suggests.
    # These bound one HTTP request each.
    connect_timeout: float = 5.0
    read_timeout: float = 10.0

    # Bounds the whole search — city resolution, the event fetch, and any
    # retries between them. Sits deliberately above read_timeout so a single
    # slow request still fails on its own timeout rather than this one, but
    # well below the ~20s that two retryable calls could otherwise reach.
    search_deadline: float = 12.0

    # Event results move (statuses change, offers appear), so they expire fast.
    event_cache_ttl: int = 300  # 5 minutes
    # City name -> city ID is effectively static; caching it hard is what keeps
    # the two-call resolve flow from doubling our upstream spend on a trial key.
    city_cache_ttl: int = 86_400  # 24 hours

    # One page only, at the maximum the JamBase OpenAPI spec documents for this
    # parameter (`perPage`: minimum 1, maximum 100, default 40). Taking the
    # documented ceiling reduces truncation; it does not eliminate it, which is
    # why SearchResult reports total_available. See WRITEUP.md.
    results_per_page: int = 100


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
