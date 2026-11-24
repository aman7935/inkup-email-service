from __future__ import annotations

import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool

from ..config import Settings, get_settings
from ..reviews import accept_webhook
from ..storage import get_store
from .schemas import HealthResponse, WebhookAck

logger = logging.getLogger(__name__)

webhook_router = APIRouter(tags=["webhooks"])
health_router = APIRouter(tags=["health"])


@webhook_router.post(
    "/webhook/tracking-update",
    response_model=WebhookAck,
    status_code=status.HTTP_200_OK,
    summary="Receive a Shiprocket tracking update",
    description=(
        "Authenticates the request and, for a delivered order, saves it so the review "
        "email goes out 3 days after delivery. No outside APIs are called here."
    ),
    responses={
        400: {"description": "Body is not a JSON object"},
        401: {"description": "Missing or invalid x-api-key token"},
        413: {"description": "Body is too large"},
        503: {"description": "Storage unavailable; Shiprocket should retry"},
    },
)
async def receive_tracking_update(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
) -> WebhookAck:
    settings = get_settings()
    _verify_token(x_api_key, settings)

    raw_body = await _read_limited_body(request, settings.webhook_max_body_bytes)
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must be valid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must be a JSON object",
        )

    try:
        result = await run_in_threadpool(accept_webhook, payload)
    except Exception as exc:
        logger.exception("webhook_store_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage unavailable",
            headers={"Retry-After": "30"},
        ) from exc

    logger.info("webhook_accepted", extra={"result": result.get("status")})
    return WebhookAck(status="received")


def _verify_token(provided: str | None, settings: Settings) -> None:
    expected = settings.webhook_token
    if expected is None:
        if settings.environment == "production":
            # Refuse to run an unauthenticated production webhook.
            logger.error("webhook_token_missing_in_production")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Webhook authentication is not configured",
            )
        return

    expected_value = expected.get_secret_value()
    if not hmac.compare_digest(expected_value.encode("utf-8"), (provided or "").encode("utf-8")):
        logger.warning("webhook_token_rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook token",
        )


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length",
            ) from exc
        if declared_size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Body is too large",
            )

    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Body is too large",
        )
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must not be empty",
        )
    return body


@health_router.get("/health/live", response_model=HealthResponse, include_in_schema=False)
def live() -> HealthResponse:
    return HealthResponse(status="ok", checks={})


@health_router.get("/health/ready", response_model=HealthResponse, include_in_schema=False)
def ready(response: Response) -> HealthResponse:
    settings = get_settings()
    checks: dict[str, str] = {}

    try:
        get_store().counts()
        checks["database"] = "up"
    except Exception:
        checks["database"] = "down"

    if settings.environment == "production":
        for name in settings.production_configuration_errors():
            checks[name.lower()] = "missing"

    unavailable = any(value != "up" for value in checks.values())
    if unavailable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="unavailable", checks=checks)
    return HealthResponse(status="ok", checks=checks)
