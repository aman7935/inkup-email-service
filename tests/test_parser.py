from __future__ import annotations

from typing import Any

import pytest

from inkup_email_service.domain.models import LineItem
from inkup_email_service.domain.parser import (
    OrderExtractionError,
    OrderNotDeliveredError,
    TrackingPayloadError,
    extract_delivered_order,
    normalise_handle,
    parse_tracking_event,
    slugify,
)


def test_parses_native_shiprocket_payload_by_status_id() -> None:
    payload: dict[str, Any] = {
        "awb": "19041424751540",
        "current_status": "IN TRANSIT",
        "shipment_status": "IN TRANSIT",
        "shipment_status_id": 18,
        "order_id": "1373900_150876814",
        "scans": [
            {
                "date": "2023-05-20 10:27:56",
                "status": "X-DLL2F",
                "activity": "In Transit",
                "sr-status": "18",
                "sr-status-label": "IN TRANSIT",
            }
        ],
    }
    event = parse_tracking_event(payload)
    assert event.delivered is False
    assert event.status_code == 18
    assert event.order_id == "1373900_150876814"


def test_status_id_seven_wins_even_if_label_is_stale() -> None:
    event = parse_tracking_event(
        {
            "current_status": "IN TRANSIT",
            "shipment_status_id": 7,
            "order_id": "ORDER-1",
        }
    )
    assert event.delivered is True
    assert event.status_code == 7


@pytest.mark.parametrize(
    "label",
    ["Delivered", "DELIVERED", "DLVD", "Delivered successfully", "Delivered - Signed"],
)
def test_delivered_labels(label: str) -> None:
    event = parse_tracking_event({"current_status": label, "order_id": "ORDER-1"})
    assert event.delivered is True


@pytest.mark.parametrize(
    "label",
    ["IN TRANSIT", "OUT FOR DELIVERY", "UNDELIVERED", "NOT DELIVERED", "Delivery failed"],
)
def test_non_delivered_labels(label: str) -> None:
    event = parse_tracking_event({"current_status": label, "order_id": "ORDER-1"})
    assert event.delivered is False


def test_extracts_email_order_name_and_handles() -> None:
    payload = {
        "order_id": "1001",
        "customer_email": "Asha@Example.com ",
        "customer_name": "Asha Rao",
        "current_status": "Delivered",
        "line_items": [
            {"name": "Classic Tee", "handle": "classic-tee"},
            {
                "title": "Red Hoodie",
                "product_url": "https://aman-testing-store.myshopify.com/products/red-hoodie?x=1",
            },
            {"product_name": "Café Shirt"},
            {"product_name": "Café Shirt", "handle": "cafe-shirt"},
        ],
    }
    event = parse_tracking_event(payload)
    order = extract_delivered_order(event)

    assert order.order_id == "1001"
    # email-validator lowercases the domain part
    assert order.email == "Asha@example.com"
    assert order.customer_name == "Asha Rao"
    assert order.items == (
        LineItem(name="Classic Tee", handle="classic-tee"),
        LineItem(name="Red Hoodie", handle="red-hoodie"),
        LineItem(name="Café Shirt", handle="cafe-shirt"),
    )


def test_native_products_string_falls_back_to_slugified_names() -> None:
    event = parse_tracking_event(
        {
            "current_status": "DELIVERED",
            "order_id": "1002",
            "customer_email": "buyer@example.com",
            "products": "Classic Tee, Red Hoodie",
        }
    )
    assert event.items == (
        LineItem(name="Classic Tee", handle="classic-tee"),
        LineItem(name="Red Hoodie", handle="red-hoodie"),
    )


def test_nested_wrapped_payload() -> None:
    event = parse_tracking_event(
        {
            "event": "tracking_update",
            "data": {
                "shipment": {
                    "shipment_status_id": "7",
                    "order_id": 555,
                    "customer": {
                        "email": "nested@example.com",
                        "first_name": "Nina",
                        "last_name": "Patel",
                    },
                    "items": [{"name": "Silk Scarf", "slug": "silk-scarf"}],
                }
            },
        }
    )
    order = extract_delivered_order(event)
    assert order.email == "nested@example.com"
    assert order.customer_name == "Nina Patel"
    assert order.items[0].handle == "silk-scarf"


def test_scan_fallback_when_top_level_status_missing() -> None:
    event = parse_tracking_event(
        {
            "order_id": "1003",
            "scans": [
                {"sr-status": "18", "sr-status-label": "IN TRANSIT"},
                {
                    "sr-status": "7",
                    "sr-status-label": "DELIVERED",
                    "date": "2026-11-07 10:00:00",
                },
            ],
        }
    )
    assert event.delivered is True
    assert event.status_code == 7
    assert event.delivered_at == "2026-11-07 10:00:00"


def test_requires_email_and_items() -> None:
    event = parse_tracking_event({"current_status": "DELIVERED", "order_id": "1004"})
    with pytest.raises(OrderExtractionError, match="customer_email"):
        extract_delivered_order(event)

    event = parse_tracking_event(
        {
            "current_status": "DELIVERED",
            "order_id": "1004",
            "customer_email": "buyer@example.com",
        }
    )
    with pytest.raises(OrderExtractionError, match="line_items"):
        extract_delivered_order(event)


def test_rejects_not_delivered_extraction() -> None:
    event = parse_tracking_event({"current_status": "IN TRANSIT", "order_id": "1005"})
    with pytest.raises(OrderNotDeliveredError):
        extract_delivered_order(event)


def test_invalid_email_is_treated_as_missing() -> None:
    event = parse_tracking_event(
        {
            "current_status": "DELIVERED",
            "order_id": "1006",
            "customer_email": "not-an-email",
        }
    )
    assert event.email is None


def test_rejects_non_mapping_payload() -> None:
    with pytest.raises(TrackingPayloadError):
        parse_tracking_event([])  # type: ignore[arg-type]


def test_handle_normalisation() -> None:
    assert normalise_handle("Classic-Tee") == "classic-tee"
    assert normalise_handle("/products/classic-tee") == "classic-tee"
    assert (
        normalise_handle(
            "https://aman-testing-store.myshopify.com/products/classic-tee?utm_source=email#x"
        )
        == "classic-tee"
    )
    assert normalise_handle("https://example.com/products/cafe%20shirt") == "cafe-shirt"
    assert normalise_handle("collections/all") == "all"
    assert normalise_handle("") == ""


def test_slugify_transliterates() -> None:
    assert slugify("Café T-Shirt") == "cafe-t-shirt"
    assert slugify("  ") == ""
