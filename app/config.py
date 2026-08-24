"""Configuration, loaded from the environment / .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jambase_api_key: str
    jambase_base_url: str = "https://api.data.jambase.com/v3"

    # Explicit per-phase timeouts. A single blanket timeout would let a slow
    # upstream hold a connection far longer than the read budget suggests.
    connect_timeout: float = 5.0
    read_timeout: float = 10.0

    # Event results move (statuses change, offers appear), so they expire fast.
    event_cache_ttl: int = 300  # 5 minutes
    # City name -> city ID is effectively static; caching it hard is what keeps
    # the two-call resolve flow from doubling our upstream spend on a trial key.
    city_cache_ttl: int = 86_400  # 24 hours

    # One page only. See README/WRITEUP for why this is a documented limit.
    results_per_page: int = 60


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
