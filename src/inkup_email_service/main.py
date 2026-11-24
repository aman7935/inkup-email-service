from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .api.middleware import RequestContextMiddleware
from .api.routes import health_router, webhook_router
from .config import get_settings
from .logging_config import configure_logging
from .reviews import send_due_reviews
from .storage import get_store

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    if settings.webhook_token is None:
        logger.warning("webhook_token_not_configured")
    if settings.resend_api_key is None:
        logger.warning("resend_api_key_not_configured")
    if settings.resend_from_email is None:
        logger.warning("resend_from_email_not_configured")

    for missing in settings.production_configuration_errors():
        logger.error("production_configuration_missing", extra={"setting": missing})

    # Opens (and if needed creates) the database before the first webhook.
    await run_in_threadpool(get_store)

    sender: asyncio.Task[None] | None = None
    if settings.sender_enabled:
        sender = asyncio.create_task(_sender_loop(settings.review_check_interval_seconds))

    logger.info(
        "service_started",
        extra={
            "environment": settings.environment,
            "storefront_url": settings.storefront_url,
            "review_delay_seconds": settings.review_delay_seconds,
            "database": get_store().backend,
            "sender_enabled": settings.sender_enabled,
        },
    )
    yield
    if sender is not None:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
    get_store().close()
    logger.info("service_stopped")


async def _sender_loop(interval_seconds: int) -> None:
    """Every few minutes, send the review emails that are due."""
    while True:
        try:
            await run_in_threadpool(send_due_reviews)
        except Exception:
            logger.exception("review_sender_error")
        await asyncio.sleep(interval_seconds)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    docs_url = "/docs" if settings.enable_docs else None
    openapi_url = "/openapi.json" if settings.enable_docs else None

    app = FastAPI(
        title="Inkup Email Service",
        version=__version__,
        description=(
            "Shiprocket tracking webhook receiver. Delivered orders are saved, and a "
            "background loop sends a Judge.me review-request email via Resend 3 days later."
        ),
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
    )

    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(webhook_router)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_exception",
            extra={"method": request.method, "path": request.url.path},
        )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Internal server error"},
        )

    return app


app = create_app()
