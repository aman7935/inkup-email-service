from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from email_validator import EmailNotValidError, validate_email

from .models import DeliveredOrder, LineItem, TrackingEvent

MAX_ITEMS = 50

# Shiprocket's tracking webhook is flat, but many integrations wrap it or
# forward an enriched Shopify order payload. We scan nested dicts so all of
# those shapes work.
_STATUS_LABEL_KEYS = (
    "current_status",
    "shipment_status",
    "tracking_status",
    "delivery_status",
    "current_status_label",
    "shipment_status_label",
    "sr_status_label",
    "status_label",
)
_GENERIC_STATUS_KEYS = ("status",)
_STATUS_ID_KEYS = (
    "shipment_status_id",
    "shipment_status_code",
    "current_status_id",
    "current_status_code",
    "status_id",
)
_ORDER_ID_KEYS = (
    "order_id",
    "channel_order_id",
    "order_number",
    "channel_order_number",
    "merchant_order_id",
    "reference_order_id",
    "shopify_order_id",
)
_EMAIL_KEYS = (
    "customer_email",
    "email",
    "email_address",
    "contact_email",
    "buyer_email",
    "consignee_email",
    "shipping_email",
    "billing_email",
    "recipient_email",
)
_CUSTOMER_NAME_KEYS = (
    "customer_name",
    "consignee_name",
    "buyer_name",
    "recipient_name",
    "full_name",
)
_AWB_KEYS = ("awb", "awb_code", "tracking_number", "tracking_id", "airwaybill")
_SHIPMENT_ID_KEYS = ("shipment_id", "shipment_number")
_DELIVERED_AT_KEYS = (
    "delivered_date",
    "delivery_date",
    "delivered_at",
    "current_timestamp",
    "scan_date",
)
_ITEM_KEYS = (
    "line_items",
    "order_items",
    "order_line_items",
    "items",
    "products",
    "cart_items",
    "product_details",
    "product_info",
)
_SCAN_KEYS = (
    "scans",
    "shipment_track_activities",
    "tracking_activities",
    "activities",
    "scan_history",
)
_SCAN_LABEL_KEYS = ("sr-status-label", "sr_status_label", "status_label", "status", "activity")
_SCAN_CODE_KEYS = ("sr-status", "sr_status", "status_id")
_ITEM_NAME_KEYS = (
    "name",
    "product_name",
    "title",
    "product_title",
    "item_name",
    "variant_title",
)
_ITEM_HANDLE_KEYS = (
    "handle",
    "product_handle",
    "shopify_handle",
    "slug",
    "url",
    "product_url",
    "permalink",
    "link",
    "product_link",
    "shopify_url",
    "path",
    "href",
)
_CONTAINER_KEYS = ("order", "channel_order", "shopify_order", "order_details", "data")
_CONTACT_CONTAINER_KEYS = ("customer", "billing_address", "shipping_address", "consignee", "buyer")

_DELIVERED_LABELS = {
    "delivered",
    "dlvd",
    "delivered successfully",
    "delivery successful",
    "delivery completed",
    "delivered to customer",
    "shipment delivered",
    "order delivered",
}
_NOT_DELIVERED_PREFIXES = (
    "not delivered",
    "undelivered",
    "delivery failed",
    "delivery exception",
    "delivery attempted",
    "out for delivery",
    # Return-to-origin: the parcel went back to the seller, not to the buyer.
    "rto",
    "return",
    "returned",
)
_EMPTY_VALUES = {"", "none", "null", "nil", "na", "n/a", "-"}


class TrackingPayloadError(ValueError):
    """The webhook body could not be understood at all."""


class OrderExtractionError(ValueError):
    """A delivered event was missing data required to schedule an email."""


class OrderNotDeliveredError(OrderExtractionError):
    """The event is valid but its status is not Delivered."""

    def __init__(self) -> None:
        super().__init__("status is not delivered")


@dataclass(frozen=True, slots=True)
class _ScanStatus:
    label: str | None
    code: int | None
    timestamp: str | None


def parse_tracking_event(payload: Mapping[str, Any]) -> TrackingEvent:
    """Parse a Shiprocket-style tracking payload into a normalized event.

    Delivered is true when either the status label is a delivered variant or
    the status id is in ``DELIVERED_STATUS_IDS`` (Shiprocket id ``7`` by
    default). The numeric check is authoritative because Shiprocket's
    ``current_status_id`` and ``shipment_status_id`` use different code spaces
    in some payloads.
    """
    if not isinstance(payload, Mapping):
        raise TrackingPayloadError("payload must be a JSON object")

    mappings = list(_iter_mappings(payload))

    label = _first_value(mappings, _STATUS_LABEL_KEYS)
    if label is None:
        label = _first_value(mappings, _GENERIC_STATUS_KEYS)

    code = _first_int(mappings, _STATUS_ID_KEYS)
    scan = _latest_scan_status(mappings)

    effective_label = label if label is not None else (scan.label if scan else None)
    effective_code = code if code is not None else (scan.code if scan else None)

    delivered_by_code = effective_code is not None and effective_code in _delivered_ids()
    delivered_by_label = effective_label is not None and _is_delivered_label(effective_label)
    delivered = bool(delivered_by_code or delivered_by_label)
    if delivered and _is_return_shipment(mappings, effective_label):
        delivered = False

    labels: list[str] = []
    for mapping in mappings:
        for key in _STATUS_LABEL_KEYS:
            value = _clean_scalar(mapping.get(key))
            if value and value not in labels:
                labels.append(value)
    if scan and scan.label and scan.label not in labels:
        labels.append(scan.label)

    order_id = _first_value(mappings, _ORDER_ID_KEYS)
    if order_id is None:
        order_id = _nested_value(
            mappings, _CONTAINER_KEYS, ("id", "order_id", "order_number", "name")
        )

    email = _first_value(mappings, _EMAIL_KEYS)
    email = _valid_email(email)

    customer_name = _extract_customer_name(mappings)
    items = _extract_items(mappings)

    delivered_at = _first_value(mappings, _DELIVERED_AT_KEYS)
    if delivered_at is None and scan:
        delivered_at = scan.timestamp

    return TrackingEvent(
        delivered=delivered,
        status_label=effective_label,
        status_code=effective_code,
        order_id=_truncate(order_id, 128),
        email=email,
        customer_name=_truncate(customer_name, 200),
        items=items,
        awb=_truncate(_first_value(mappings, _AWB_KEYS), 128),
        shipment_id=_truncate(_first_value(mappings, _SHIPMENT_ID_KEYS), 128),
        delivered_at=_truncate(delivered_at, 128),
        all_status_labels=tuple(labels),
    )


def extract_delivered_order(event: TrackingEvent) -> DeliveredOrder:
    """Validate an event and return the data needed to schedule a review email."""
    if not event.delivered:
        raise OrderNotDeliveredError()

    missing: list[str] = []
    if not event.order_id:
        missing.append("order_id")
    if not event.email:
        missing.append("customer_email")
    if not event.items:
        missing.append("line_items")
    if missing:
        raise OrderExtractionError(f"missing required fields: {', '.join(missing)}")

    assert event.order_id is not None  # narrowed by the check above
    assert event.email is not None
    return DeliveredOrder(
        order_id=event.order_id,
        email=event.email,
        items=event.items,
        customer_name=event.customer_name,
        awb=event.awb,
        shipment_id=event.shipment_id,
        delivered_at=event.delivered_at,
        status_label=event.status_label,
    )


# ---------------------------------------------------------------------------
# Delivered-status helpers
# ---------------------------------------------------------------------------
def _delivered_ids() -> frozenset[int]:
    # Imported lazily to keep this module pure/testable without constructing
    # Settings (which reads the environment).
    from ..config import get_settings

    return get_settings().delivered_status_id_set


def _normalise_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _is_delivered_label(value: str) -> bool:
    normalised = _normalise_label(value)
    if not normalised:
        return False
    if normalised in _DELIVERED_LABELS:
        return True
    if normalised.startswith(_NOT_DELIVERED_PREFIXES):
        return False
    words = set(normalised.split())
    return "delivered" in words or "dlvd" in words


def _is_return_shipment(mappings: Sequence[Mapping[str, Any]], label: str | None) -> bool:
    """True for RTO / reverse shipments, whose "delivered" means back at the seller."""
    if label and _normalise_label(label).startswith(("rto", "return")):
        return True
    for mapping in mappings[:1]:  # only the top-level Shiprocket flag
        flag = mapping.get("is_return")
        if _to_int(flag) == 1 or flag is True:
            return True
    return False


# ---------------------------------------------------------------------------
# Mapping traversal
# ---------------------------------------------------------------------------
def _normalise_mapping(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(key, str):
            normalized[key.lower()] = value
        else:
            normalized[str(key)] = value
    return normalized


def _iter_mappings(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 6,
    seen: set[int] | None = None,
) -> Iterator[dict[str, Any]]:
    if seen is None:
        seen = set()
    if depth > max_depth:
        return

    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        normalized = _normalise_mapping(value)
        yield normalized
        for child in normalized.values():
            if isinstance(child, Mapping):
                yield from _iter_mappings(child, depth=depth + 1, max_depth=max_depth, seen=seen)
            elif isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
                for item in list(child)[:MAX_ITEMS]:
                    if isinstance(item, Mapping):
                        yield from _iter_mappings(
                            item, depth=depth + 1, max_depth=max_depth, seen=seen
                        )


def _clean_scalar(value: Any) -> str | None:
    if value is None or isinstance(value, (bool, Mapping, list, tuple, set)):
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if not text or text.lower() in _EMPTY_VALUES:
        return None
    return text


def _first_value(mappings: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> str | None:
    for mapping in mappings:
        for key in keys:
            cleaned = _clean_scalar(mapping.get(key))
            if cleaned is not None:
                return cleaned
    return None


def _first_int(mappings: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> int | None:
    for mapping in mappings:
        for key in keys:
            value = _to_int(mapping.get(key))
            if value is not None:
                return value
    return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
            return int(number) if number.is_integer() else None
    return None


def _nested_value(
    mappings: Sequence[Mapping[str, Any]],
    containers: Sequence[str],
    keys: Sequence[str],
) -> str | None:
    for mapping in mappings:
        for container in containers:
            node = mapping.get(container)
            if isinstance(node, Mapping):
                normalized = _normalise_mapping(node)
                value = _first_value([normalized], keys)
                if value is not None:
                    return value
    return None


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


# ---------------------------------------------------------------------------
# Field-specific extraction
# ---------------------------------------------------------------------------
def valid_email(value: str | None) -> str | None:
    """Return a normalised email address, or ``None`` if it is missing/invalid."""
    return _valid_email(value)


def _valid_email(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def _extract_customer_name(mappings: Sequence[Mapping[str, Any]]) -> str | None:
    direct = _first_value(mappings, _CUSTOMER_NAME_KEYS)
    if direct:
        return direct

    for mapping in mappings:
        for container in _CONTACT_CONTAINER_KEYS:
            node = mapping.get(container)
            if not isinstance(node, Mapping):
                continue
            normalized = _normalise_mapping(node)
            full = _first_value([normalized], ("name", "full_name"))
            if full:
                return full
            first = _first_value([normalized], ("first_name",))
            last = _first_value([normalized], ("last_name",))
            if first or last:
                return " ".join(part for part in (first, last) if part)
    return None


def _latest_scan_status(mappings: Sequence[Mapping[str, Any]]) -> _ScanStatus | None:
    scans: Any = None
    for mapping in mappings:
        for key in _SCAN_KEYS:
            candidate = mapping.get(key)
            if isinstance(candidate, Sequence) and not isinstance(
                candidate, (str, bytes, bytearray)
            ):
                scans = candidate
                break
        if scans is not None:
            break

    if not isinstance(scans, Sequence):
        return None

    for entry in reversed(list(scans)[-MAX_ITEMS:]):
        if not isinstance(entry, Mapping):
            continue
        normalized = _normalise_mapping(entry)
        label = _first_value([normalized], _SCAN_LABEL_KEYS)
        code = _first_int([normalized], _SCAN_CODE_KEYS)
        if label is None and code is None:
            continue
        timestamp = _first_value([normalized], ("date", "timestamp", "event_time"))
        return _ScanStatus(label=label, code=code, timestamp=timestamp)
    return None


def _extract_items(mappings: Sequence[Mapping[str, Any]]) -> tuple[LineItem, ...]:
    raw: Any = None
    for mapping in mappings:
        for key in _ITEM_KEYS:
            candidate = mapping.get(key)
            if candidate not in (None, "", [], {}):
                raw = candidate
                break
        if raw is not None:
            break

    if raw is None:
        return ()

    entries = _item_entries(raw)
    items: list[LineItem] = []
    seen_handles: set[str] = set()

    for index, entry in enumerate(entries[:MAX_ITEMS]):
        item = _parse_item(entry, index)
        if item is None:
            continue
        if item.handle in seen_handles:
            continue
        seen_handles.add(item.handle)
        items.append(item)

    return tuple(items)


def _item_entries(raw: Any) -> list[Any]:
    if isinstance(raw, Mapping):
        normalized = _normalise_mapping(raw)
        if any(key in normalized for key in _ITEM_NAME_KEYS + _ITEM_HANDLE_KEYS):
            return [normalized]
        # Support {"classic-tee": "Classic Tee"} style maps.
        entries: list[Any] = []
        for handle, value in normalized.items():
            if isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("handle", handle)
                entries.append(item)
            elif _clean_scalar(value) is not None:
                entries.append({"handle": handle, "name": value})
        return entries

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith(("[", "{")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, (list, Mapping)):
                return _item_entries(parsed)
        # Shiprocket's native `products` field is a comma-separated string.
        return [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]

    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return list(raw)

    return [raw]


def _parse_item(entry: Any, index: int) -> LineItem | None:
    if isinstance(entry, str):
        name = _clean_scalar(entry)
        handle = slugify(name or "")
        if not name and not handle:
            return None
        return LineItem(
            name=_truncate(name, 300) or _title_from_handle(handle) or "Product",
            handle=handle or f"product-{index + 1}",
        )

    if not isinstance(entry, Mapping):
        return None

    normalized = _normalise_mapping(entry)
    product = normalized.get("product")
    product_map = _normalise_mapping(product) if isinstance(product, Mapping) else {}

    name = _first_value([normalized], _ITEM_NAME_KEYS)
    if name is None and product_map:
        name = _first_value([product_map], _ITEM_NAME_KEYS)

    handle = _first_value([normalized], _ITEM_HANDLE_KEYS)
    if handle is None and product_map:
        handle = _first_value([product_map], _ITEM_HANDLE_KEYS)

    cleaned_handle = normalise_handle(handle or "")
    if not cleaned_handle and name:
        cleaned_handle = slugify(name)
    if not cleaned_handle:
        cleaned_handle = f"product-{index + 1}"

    if not name:
        name = _title_from_handle(cleaned_handle)

    if not name:
        return None

    return LineItem(name=_truncate(name, 300) or "Product", handle=cleaned_handle)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def normalise_handle(value: str) -> str:
    """Normalize a Shopify handle or product URL to a bare handle."""
    text = value.strip()
    if not text:
        return ""

    if "/products/" in text.lower() or text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        path = parsed.path if (parsed.scheme or parsed.netloc) else text
    else:
        path = text

    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"\.(?:js|json)$", "", path, flags=re.IGNORECASE)

    marker = "/products/"
    lower_path = path.lower()
    if marker in lower_path:
        after = path[lower_path.index(marker) + len(marker) :]
        candidate = after.split("/", 1)[0]
    else:
        candidate = path.strip("/").split("/")[-1] if path.strip("/") else ""

    candidate = unquote(candidate).strip().strip("/").strip("-.")
    if not candidate:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._~-]+", candidate):
        candidate = slugify(candidate)
    return candidate.lower().strip("-.")


def _title_from_handle(handle: str) -> str:
    return handle.replace("-", " ").replace("_", " ").strip().title()
