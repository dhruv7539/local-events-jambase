"""Application wiring.

This is the only module that names a concrete provider. Routes, models and the
UI are all provider-agnostic.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.providers.base import ProviderError
from app.providers.jambase import JamBaseProvider
from app.routes import STATIC_DIR, router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own one HTTP client for the process lifetime.

    A client per request would discard connection pooling and TLS session
    reuse, which matters when every user-facing search can cost two upstream
    calls.
    """
    settings = get_settings()
    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.connect_timeout,
        pool=settings.connect_timeout,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        # The single line that binds a concrete provider to the app.
        app.state.provider = JamBaseProvider(client, settings)
        yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Live Events",
        description="Find live music events near a location.",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(ProviderError)
    async def handle_provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        """Turn any upstream failure into a clean status code and message.

        Without this, a provider timeout would surface as a 500 with a stack
        trace. The message is the one the provider chose to expose; upstream
        bodies are logged inside the provider and never reach the client.
        """
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message}
        )

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
