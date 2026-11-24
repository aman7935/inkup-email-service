from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Settings, get_settings
from ..domain.models import ReviewJob

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def review_url(storefront_url: str, handle: str) -> str:
    """Build the exact Judge.me review-widget URL for a product."""
    safe_handle = quote(handle.strip().lower(), safe="-._~")
    return f"{storefront_url.rstrip('/')}/products/{safe_handle}#judgeme_product_reviews"


def render_review_email(job: ReviewJob, settings: Settings | None = None) -> RenderedEmail:
    settings = settings or get_settings()
    items = [
        {
            "name": item.name,
            "handle": item.handle,
            "review_url": review_url(settings.storefront_url, item.handle),
        }
        for item in job.items
    ]
    if not items:
        raise ValueError("cannot render a review email without line items")

    context = {
        "subject": settings.review_email_subject,
        "store_name": settings.store_name,
        "storefront_url": settings.storefront_url,
        "customer_name": job.customer_name,
        "order_id": job.order_id,
        "order_label": job.order_label,
        "items": items,
        "support_email": str(settings.support_email) if settings.support_email else None,
        "year": datetime.now(UTC).year,
    }
    html = _env.get_template("review_request.html").render(**context)
    text = _env.get_template("review_request.txt").render(**context)
    return RenderedEmail(subject=settings.review_email_subject, html=html, text=text)
