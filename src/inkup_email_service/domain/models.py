from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class LineItem:
    name: str
    handle: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "handle": self.handle}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LineItem:
        return cls(name=str(value["name"]), handle=str(value["handle"]))


@dataclass(frozen=True, slots=True)
class TrackingEvent:
    """The parts of a Shiprocket tracking webhook we care about.

    All fields are optional because Shiprocket's webhook is not a formal schema
    and different sources (tracking webhook, order webhook, enriched Shopify
    forwarders) send slightly different shapes.
    """

    delivered: bool
    status_label: str | None
    status_code: int | None
    order_id: str | None
    email: str | None
    customer_name: str | None
    items: tuple[LineItem, ...]
    awb: str | None = None
    shipment_id: str | None = None
    delivered_at: str | None = None
    all_status_labels: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "status_label": self.status_label,
            "status_code": self.status_code,
            "order_id": self.order_id,
            "email": self.email,
            "customer_name": self.customer_name,
            "items": [item.to_dict() for item in self.items],
            "awb": self.awb,
            "shipment_id": self.shipment_id,
            "delivered_at": self.delivered_at,
            "all_status_labels": list(self.all_status_labels),
        }


@dataclass(frozen=True, slots=True)
class DeliveredOrder:
    """A validated order that is safe to schedule a review request for."""

    order_id: str
    email: str
    items: tuple[LineItem, ...]
    customer_name: str | None = None
    awb: str | None = None
    shipment_id: str | None = None
    delivered_at: str | None = None
    status_label: str | None = None
    order_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "order_name": self.order_name,
            "email": self.email,
            "customer_name": self.customer_name,
            "items": [item.to_dict() for item in self.items],
            "awb": self.awb,
            "shipment_id": self.shipment_id,
            "delivered_at": self.delivered_at,
            "status_label": self.status_label,
        }


@dataclass(frozen=True, slots=True)
class ReviewJob:
    """A persisted, delayed review-request job.

    ``job_id`` is deterministic (UUID5 of order + email) so retries and
    duplicate webhooks converge on the same email idempotency key.
    """

    job_id: str
    order_id: str
    email: str
    items: tuple[LineItem, ...]
    due_at: str
    created_at: str
    customer_name: str | None = None
    awb: str | None = None
    shipment_id: str | None = None
    delivered_at: str | None = None
    status_label: str | None = None
    order_name: str | None = None

    @property
    def order_label(self) -> str:
        """Order number for display, without a leading '#' (the template adds it)."""
        return (self.order_name or self.order_id).lstrip("#").strip() or self.order_id

    @property
    def due_timestamp(self) -> float:
        return datetime.fromisoformat(self.due_at).timestamp()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "order_id": self.order_id,
            "email": self.email,
            "customer_name": self.customer_name,
            "items": [item.to_dict() for item in self.items],
            "due_at": self.due_at,
            "created_at": self.created_at,
            "awb": self.awb,
            "shipment_id": self.shipment_id,
            "delivered_at": self.delivered_at,
            "status_label": self.status_label,
            "order_name": self.order_name,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReviewJob:
        return cls(
            job_id=str(value["job_id"]),
            order_id=str(value["order_id"]),
            email=str(value["email"]),
            customer_name=value.get("customer_name"),
            items=tuple(LineItem.from_dict(item) for item in value.get("items", [])),
            due_at=str(value["due_at"]),
            created_at=str(value["created_at"]),
            awb=value.get("awb"),
            shipment_id=value.get("shipment_id"),
            delivered_at=value.get("delivered_at"),
            status_label=value.get("status_label"),
            order_name=value.get("order_name"),
        )

    def with_due_at(self, value: datetime) -> ReviewJob:
        return ReviewJob(
            job_id=self.job_id,
            order_id=self.order_id,
            email=self.email,
            items=self.items,
            due_at=value.astimezone(UTC).isoformat(),
            created_at=self.created_at,
            customer_name=self.customer_name,
            awb=self.awb,
            shipment_id=self.shipment_id,
            delivered_at=self.delivered_at,
            status_label=self.status_label,
            order_name=self.order_name,
        )
