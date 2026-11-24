"""The whole review-email flow, without queues.

1. ``accept_webhook`` — called by the API. If Shiprocket says "delivered",
   save the order with ``due_at = delivery time + REVIEW_DELAY_SECONDS``.
   No outside API calls, so Shiprocket gets its 200 immediately.
2. ``send_due_reviews`` — called every few minutes by the background loop in
   ``main.py``. For each due order: look up the buyer's email and products
   (Shiprocket API), the exact product links (Shopify), and send the email
   (Resend). Temporary errors are retried later; hopeless orders are skipped.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from .config import Settings, get_settings
from .domain.models import LineItem, ReviewJob, TrackingEvent
from .domain.parser import (
    OrderExtractionError,
    TrackingPayloadError,
    extract_delivered_order,
    parse_tracking_event,
    slugify,
    valid_email,
)
from .domain.timing import parse_delivery_time
from .services.email import (
    EmailConfigurationError,
    PermanentEmailError,
    TransientEmailError,
    send_review_email,
)
from .services.shiprocket import (
    ShiprocketConfigurationError,
    ShiprocketOrder,
    ShiprocketTransientError,
    find_shiprocket_order,
)
from .services.shopify import (
    ShopifyClient,
    ShopifyConfigurationError,
    ShopifyTransientError,
)
from .storage import ReviewRow, ReviewStore, get_store

logger = logging.getLogger(__name__)

# Errors worth trying again later. Configuration errors are included so that
# fixing a wrong password/key does not lose the emails that were waiting.
_RETRYABLE = (
    ShiprocketTransientError,
    ShiprocketConfigurationError,
    ShopifyTransientError,
    TransientEmailError,
    EmailConfigurationError,
    httpx.HTTPError,
    ConnectionError,
    TimeoutError,
)


class SkipOrder(Exception):
    """This order can never get an email (no buyer email, no products, ...)."""


# ---------------------------------------------------------------------------
# 1. Webhook
# ---------------------------------------------------------------------------
def accept_webhook(
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
    store: ReviewStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    store = store or get_store()
    now = now or datetime.now(UTC)

    try:
        event = parse_tracking_event(payload)
    except TrackingPayloadError as exc:
        logger.warning("tracking_payload_invalid", extra={"reason": str(exc)})
        return {"status": "ignored", "reason": "invalid_payload"}

    log_context = _log_context(event)
    if not event.delivered:
        logger.info("tracking_update_ignored_not_delivered", extra=log_context)
        return {"status": "ignored", "reason": "not_delivered"}

    key = _order_key(event, payload)
    if key is None:
        logger.warning("delivered_event_without_order_id", extra=log_context)
        return {"status": "ignored", "reason": "no_order_id"}

    delivered_at = parse_delivery_time(event.delivered_at, settings.shiprocket_timezone)
    if delivered_at is not None and settings.review_max_delivery_age_seconds:
        age = (now - delivered_at).total_seconds()
        if age > settings.review_max_delivery_age_seconds:
            logger.warning("delivered_event_too_old", extra=log_context)
            return {"status": "ignored", "reason": "delivery_too_old"}

    due = due_time(delivered_at, now=now, delay_seconds=settings.review_delay_seconds)
    added = store.add(
        key,
        json.dumps(payload, separators=(",", ":")),
        delivered_at=delivered_at.isoformat() if delivered_at else None,
        due_at=due.timestamp(),
    )
    if not added:
        logger.info("tracking_update_duplicate", extra={**log_context, "key": key})
        return {"status": "duplicate", "key": key}

    logger.info(
        "review_request_scheduled",
        extra={**log_context, "key": key, "due_at": due.isoformat()},
    )
    return {"status": "scheduled", "key": key, "due_at": due.isoformat()}


def due_time(delivered_at: datetime | None, *, now: datetime, delay_seconds: int) -> datetime:
    """Count the delay from delivery, not from when the webhook arrived.

    An unknown or future delivery time counts from now. A due time in the past
    means "send on the next check".
    """
    start = delivered_at if delivered_at is not None and delivered_at <= now else now
    return start + timedelta(seconds=delay_seconds)


def _order_key(event: TrackingEvent, payload: dict[str, Any]) -> str | None:
    sr_order_id = payload.get("sr_order_id")
    for value in (event.order_id, sr_order_id, event.awb):
        if value not in (None, ""):
            return str(value).strip()
    return None


# ---------------------------------------------------------------------------
# 2. Sending
# ---------------------------------------------------------------------------
def send_due_reviews(
    *,
    settings: Settings | None = None,
    store: ReviewStore | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Send every review email whose time has come. Safe to call repeatedly."""
    settings = settings or get_settings()
    store = store or get_store()
    now = time.time() if now is None else now

    summary = {"sent": 0, "skipped": 0, "retry": 0, "failed": 0}
    for row in store.due(now=now, limit=settings.review_batch_size):
        outcome = _process(row, settings, store, now)
        summary[outcome] += 1
    if any(summary.values()):
        logger.info("review_batch_done", extra=summary)
    return summary


def _process(row: ReviewRow, settings: Settings, store: ReviewStore, now: float) -> str:
    log_context: dict[str, Any] = {"key": row.key, "attempt": row.attempts + 1}
    try:
        job = build_job(row, settings)
        sent = send_review_email(job, settings)
    except SkipOrder as exc:
        store.mark_skipped(row.key, str(exc))
        logger.warning("review_skipped", extra={**log_context, "reason": str(exc)})
        return "skipped"
    except _RETRYABLE as exc:
        return _retry(row, store, settings, now, exc, log_context)
    except PermanentEmailError as exc:
        store.mark_failed(row.key, str(exc))
        logger.error("review_email_failed", extra={**log_context, "error": str(exc)})
        return "failed"
    except Exception as exc:  # unexpected bug: keep the order, try again later
        logger.exception("review_unexpected_error", extra=log_context)
        return _retry(row, store, settings, now, exc, log_context)

    store.mark_sent(row.key, sent.id)
    logger.info(
        "review_email_sent",
        extra={**log_context, "email_id": sent.id, "order_name": job.order_name},
    )
    return "sent"


def _retry(
    row: ReviewRow,
    store: ReviewStore,
    settings: Settings,
    now: float,
    exc: BaseException,
    log_context: dict[str, Any],
) -> str:
    if row.attempts + 1 >= settings.review_max_attempts:
        store.mark_failed(row.key, f"gave up after {row.attempts + 1} attempts: {exc}")
        logger.error("review_gave_up", extra={**log_context, "error": str(exc)})
        return "failed"
    # 10 min, 20 min, 40 min ... capped at 6 hours.
    delay = min(6 * 3600, 600 * 2**row.attempts)
    store.retry_later(row.key, error=str(exc), next_due=now + delay)
    logger.warning(
        "review_retry_later",
        extra={**log_context, "error": str(exc), "retry_in_seconds": delay},
    )
    return "retry"


def build_job(row: ReviewRow, settings: Settings) -> ReviewJob:
    """Turn a stored webhook into a ready-to-send email job."""
    payload = json.loads(row.payload)
    event = parse_tracking_event(payload)
    log_context = {**_log_context(event), "key": row.key}

    order_name: str | None = None
    if settings.shiprocket_configured:
        event, order_name = _enrich_order(event, payload, settings, log_context)

    try:
        order = extract_delivered_order(event)
    except OrderExtractionError as exc:
        raise SkipOrder(str(exc)) from exc

    return ReviewJob(
        job_id=make_job_id(order.order_id, order.email),
        order_id=order.order_id,
        order_name=order_name,
        email=order.email,
        customer_name=order.customer_name,
        items=order.items,
        due_at=datetime.fromtimestamp(row.due_at, UTC).isoformat(),
        created_at=datetime.now(UTC).isoformat(),
        awb=order.awb,
        shipment_id=order.shipment_id,
        delivered_at=row.delivered_at,
        status_label=order.status_label,
    )


def make_job_id(order_id: str, email: str) -> str:
    """Stable id, used as the Resend idempotency key so a retry never double-sends."""
    return str(uuid5(NAMESPACE_URL, f"inkup-review:{order_id}:{email.lower()}"))


def _enrich_order(
    event: TrackingEvent,
    payload: dict[str, Any],
    settings: Settings,
    log_context: dict[str, Any],
) -> tuple[TrackingEvent, str | None]:
    """Fill in email, name and products from Shiprocket (+ Shopify handles)."""
    sr_order_id = payload.get("sr_order_id")
    sr_order = find_shiprocket_order(
        sr_order_id=str(sr_order_id) if sr_order_id not in (None, "") else None,
        awb=event.awb,
        order_id=event.order_id,
        settings=settings,
    )
    if sr_order is None:
        logger.warning("shiprocket_order_not_found", extra=log_context)
        return event, None

    order_number = sr_order.channel_order_id or event.order_id
    items, shopify_name = _resolve_items(sr_order, order_number, settings, log_context)

    email = valid_email(sr_order.email) or event.email
    customer_name = _first_name(sr_order.customer_name) or event.customer_name
    enriched = dataclasses.replace(
        event,
        email=email,
        customer_name=customer_name,
        items=items or event.items,
    )
    order_name = shopify_name or (f"#{order_number.lstrip('#')}" if order_number else None)
    return enriched, order_name


def _resolve_items(
    sr_order: ShiprocketOrder,
    order_number: str | None,
    settings: Settings,
    log_context: dict[str, Any],
) -> tuple[tuple[LineItem, ...], str | None]:
    """Exact Shopify handles: order line items, then SKUs, then name slugs."""
    if settings.shopify_configured:
        try:
            with ShopifyClient(settings) as shop:
                if order_number:
                    shop_order = shop.find_order(order_number)
                    if shop_order is not None and shop_order.items:
                        return shop_order.items, shop_order.name or None

                by_sku: list[LineItem] = []
                for product in sr_order.products:
                    item = shop.find_product_by_sku(product.sku) if product.sku else None
                    if item is not None and item.handle not in {i.handle for i in by_sku}:
                        by_sku.append(item)
                if by_sku and len(by_sku) == len(sr_order.products):
                    return tuple(by_sku), None
        except ShopifyConfigurationError as exc:
            # Still send the email; the guessed links below are usually right.
            logger.error("shopify_configuration_error", extra={**log_context, "error": str(exc)})

    # Last resort: Shopify's default handle is the slugified product title.
    guessed: list[LineItem] = []
    for product in sr_order.products:
        handle = slugify(product.name)
        if handle and handle not in {i.handle for i in guessed}:
            guessed.append(LineItem(name=product.name, handle=handle))
    if guessed:
        logger.warning(
            "product_handles_guessed_from_names",
            extra={**log_context, "handles": [i.handle for i in guessed]},
        )
    return tuple(guessed), None


def _first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    parts = full_name.split()
    return parts[0][:100] if parts else None


def _log_context(event: TrackingEvent) -> dict[str, Any]:
    return {
        "order_id": event.order_id,
        "awb": event.awb,
        "status_label": event.status_label,
        "status_code": event.status_code,
    }
