from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import resend
from resend.exceptions import NoContentError, ResendError

from ..config import Settings, get_settings
from ..domain.models import ReviewJob
from .renderer import render_review_email

logger = logging.getLogger(__name__)

_PERMANENT_CODES = {"400", "401", "403", "404", "422"}
_TRANSIENT_CODES = {"408", "425", "429"}


class EmailError(RuntimeError):
    """Base class for email delivery failures."""


class PermanentEmailError(EmailError):
    """Retrying will not help (bad config, validation error, invalid key)."""


class EmailConfigurationError(PermanentEmailError):
    """Resend is not configured correctly."""


class TransientEmailError(EmailError):
    """Network, rate-limit, or Resend-side failure; safe to retry."""


@dataclass(frozen=True, slots=True)
class SentEmail:
    id: str | None


def send_review_email(job: ReviewJob, settings: Settings | None = None) -> SentEmail:
    """Render and send a review request through the Resend SDK.

    The Resend idempotency key is derived from the deterministic job id, so a
    retry after a crash or timeout cannot create a second email within
    Resend's idempotency window.
    """
    settings = settings or get_settings()

    api_key = settings.resend_api_key.get_secret_value() if settings.resend_api_key else ""
    from_address = settings.from_address
    if not api_key or not from_address:
        raise EmailConfigurationError(
            "RESEND_API_KEY and RESEND_FROM_EMAIL must be configured before sending"
        )

    try:
        rendered = render_review_email(job, settings)
    except Exception as exc:  # template or job data problem
        raise PermanentEmailError(f"could not render review email: {exc}") from exc

    resend.api_key = api_key
    if getattr(resend.default_http_client, "_timeout", None) != settings.resend_timeout_seconds:
        resend.default_http_client = resend.RequestsClient(timeout=settings.resend_timeout_seconds)

    params: dict[str, Any] = {
        "from": from_address,
        "to": [job.email],
        "subject": rendered.subject,
        "html": rendered.html,
        "text": rendered.text,
        "headers": {"X-Entity-Ref-ID": job.job_id},
        "tags": [
            {"name": "type", "value": "review_request"},
            {"name": "order_id", "value": _tag_value(job.order_id)},
        ],
    }
    if settings.resend_reply_to:
        params["reply_to"] = str(settings.resend_reply_to)

    try:
        response = resend.Emails.send(
            params,  # type: ignore[arg-type]
            options={"idempotency_key": f"review-request/{job.job_id}"},
        )
    except ResendError as exc:
        if _is_transient(exc):
            raise TransientEmailError(_format_error(exc)) from exc
        raise PermanentEmailError(_format_error(exc)) from exc
    except NoContentError as exc:
        raise TransientEmailError("Resend returned an empty response") from exc
    except Exception as exc:  # network failure / SDK safety net
        raise TransientEmailError(f"Resend request failed: {exc}") from exc

    email_id: str | None = None
    if isinstance(response, Mapping):
        raw_id = response.get("id")
        email_id = str(raw_id) if raw_id else None
    else:
        raw_id = getattr(response, "id", None)
        email_id = str(raw_id) if raw_id else None

    logger.info(
        "resend_email_accepted",
        extra={
            "job_id": job.job_id,
            "order_id": job.order_id,
            "email_id": email_id,
        },
    )
    return SentEmail(id=email_id)


def _is_transient(exc: ResendError) -> bool:
    code = str(exc.code)
    if code in _TRANSIENT_CODES or code.startswith("5"):
        return True
    if code in _PERMANENT_CODES:
        return False
    # Unknown errors are retried conservatively; max attempts and the outbox
    # prevent an infinite loop.
    return True


def _format_error(exc: ResendError) -> str:
    return f"Resend error {exc.code} ({exc.error_type}): {exc.message}"


def _tag_value(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return cleaned[:256] or "unknown"
