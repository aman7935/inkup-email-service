from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from inkup_email_service import reviews
from inkup_email_service.cache import MemoryCache
from inkup_email_service.config import get_settings
from inkup_email_service.domain.models import LineItem, ReviewJob
from inkup_email_service.domain.parser import parse_tracking_event
from inkup_email_service.domain.timing import parse_delivery_time
from inkup_email_service.services.email import (
    PermanentEmailError,
    SentEmail,
    TransientEmailError,
)
from inkup_email_service.services.renderer import render_review_email
from inkup_email_service.services.shiprocket import (
    ShiprocketClient,
    ShiprocketConfigurationError,
    ShiprocketOrder,
    ShiprocketProduct,
)
from inkup_email_service.services.shopify import (
    ShopifyClient,
    ShopifyConfigurationError,
    ShopifyOrder,
    ShopifyTransientError,
    order_lookup_candidates,
)
from inkup_email_service.storage import ReviewStore

WEBHOOK_TOKEN = "test-token"  # same value conftest puts in the environment

IST = timezone(timedelta(hours=5, minutes=30))
THREE_DAYS = 259_200


def ist(dt: datetime, fmt: str = "%d %m %Y %H:%M:%S") -> str:
    return dt.astimezone(IST).strftime(fmt)


def real_shiprocket_payload(delivered_at: datetime, **overrides: Any) -> dict[str, Any]:
    """Shape of the documented Shiprocket tracking webhook (no email, no products)."""
    payload: dict[str, Any] = {
        "awb": "19041424751540",
        "courier_name": "Delhivery Surface",
        "current_status": "DELIVERED",
        "current_status_id": 7,
        "shipment_status": "DELIVERED",
        "shipment_status_id": 7,
        "current_timestamp": ist(delivered_at),
        "order_id": "1001",
        "sr_order_id": 348456385,
        "etd": "2023-05-23 15:40:19",
        "scans": [
            {
                "date": ist(delivered_at, "%Y-%m-%d %H:%M:%S"),
                "status": "DLVD",
                "activity": "Delivered",
                "location": "Mumbai (Maharashtra)",
                "sr-status": "7",
                "sr-status-label": "DELIVERED",
            }
        ],
        "is_return": 0,
        "channel_id": 3422553,
        "pod_status": "OTP Based Delivery",
        "pod": "Not Available",
    }
    payload.update(overrides)
    return payload


SHOP_ORDER = ShopifyOrder(
    gid="gid://shopify/Order/5550001112223",
    name="#1001",
    items=(
        LineItem(name="Classic Tee", handle="classic-tee"),
        LineItem(name="Red Hoodie", handle="red-hoodie"),
    ),
)

SR_ORDER = ShiprocketOrder(
    sr_order_id="348456385",
    channel_order_id="1001",
    email="Buyer@Example.com",
    customer_name="Asha Rao",
    products=(
        ShiprocketProduct(name="Classic Tee - M", sku="TEE-M"),
        ShiprocketProduct(name="Red Hoodie", sku="HOOD-1"),
    ),
)


# ---------------------------------------------------------------------------
# Shopify client
# ---------------------------------------------------------------------------
def _order_node(name: str = "#1001") -> dict[str, Any]:
    return {
        "id": "gid://shopify/Order/5550001112223",
        "name": name,
        "lineItems": {
            "nodes": [
                {
                    "title": "Classic Tee - M",
                    "product": {"handle": "classic-tee", "title": "Classic Tee"},
                },
                {
                    "title": "Classic Tee - L",
                    "product": {"handle": "classic-tee", "title": "Classic Tee"},
                },
                {"title": "Gift wrap", "product": None},
                {"title": "Red Hoodie", "product": {"handle": "Red-Hoodie", "title": "Red Hoodie"}},
            ]
        },
    }


def _client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> ShopifyClient:
    monkeypatch.setenv("SHOPIFY_ADMIN_API_TOKEN", "shpat_test")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "aman-testing-store.myshopify.com")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ShopifyClient(get_settings(), http=http, redis=MemoryCache())


def test_lookup_candidates() -> None:
    assert order_lookup_candidates("#1001") == ["1001"]
    assert order_lookup_candidates("1373900_150876814") == [
        "1373900_150876814",
        "1373900",
        "150876814",
    ]


def test_find_order_by_name_parses_items(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Shopify-Access-Token"] == "shpat_test"
        assert request.url.path.endswith("/graphql.json")
        body = json.loads(request.content)
        seen.append(body)
        # A fuzzy search hit for another order must be ignored.
        return httpx.Response(
            200, json={"data": {"orders": {"nodes": [_order_node("#10012"), _order_node()]}}}
        )

    order = _client(monkeypatch, handler).find_order("#1001")
    assert order is not None
    assert order.name == "#1001"
    # duplicate handles collapsed, product-less line skipped, handle lowercased
    assert order.items == (
        LineItem(name="Classic Tee", handle="classic-tee"),
        LineItem(name="Red Hoodie", handle="red-hoodie"),
    )
    assert seen[0]["variables"] == {"q": 'name:"#1001" OR name:"1001"'}


def test_find_order_by_long_numeric_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["variables"] == {"id": "gid://shopify/Order/5550001112223"}
        return httpx.Response(200, json={"data": {"order": _order_node()}})

    order = _client(monkeypatch, handler).find_order("5550001112223")
    assert order is not None and order.name == "#1001"


def test_find_order_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orders": {"nodes": []}}})

    assert _client(monkeypatch, handler).find_order("1373900_150876814") is None


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json={"errors": "Invalid API key"}), ShopifyConfigurationError),
        (httpx.Response(429), ShopifyTransientError),
        (httpx.Response(503), ShopifyTransientError),
        (
            httpx.Response(
                200, json={"errors": [{"message": "x", "extensions": {"code": "THROTTLED"}}]}
            ),
            ShopifyTransientError,
        ),
        (
            httpx.Response(200, json={"errors": [{"message": "Access denied for lineItems"}]}),
            ShopifyConfigurationError,
        ),
    ],
)
def test_shopify_errors(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response, error: type[Exception]
) -> None:
    client = _client(monkeypatch, lambda request: response)
    with pytest.raises(error):
        client.find_order("1001")


def _cc_client(monkeypatch: pytest.MonkeyPatch, handler: Any, redis: MemoryCache) -> ShopifyClient:
    monkeypatch.delenv("SHOPIFY_ADMIN_API_TOKEN", raising=False)
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "aman-testing-store.myshopify.com")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ShopifyClient(get_settings(), http=http, redis=redis)


def test_client_credentials_token_is_fetched_once_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"token": 0, "graphql": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/oauth/access_token":
            calls["token"] += 1
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            assert form == {
                "grant_type": "client_credentials",
                "client_id": "cid",
                "client_secret": "csecret",
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "tok123",
                    "scope": "read_orders,read_products",
                    "expires_in": 86399,
                },
            )
        calls["graphql"] += 1
        assert request.headers["X-Shopify-Access-Token"] == "tok123"
        return httpx.Response(200, json={"data": {"orders": {"nodes": [_order_node()]}}})

    redis = MemoryCache()
    client = _cc_client(monkeypatch, handler, redis)
    assert client.find_order("1001") is not None
    assert client.find_order("1001") is not None
    assert calls == {"token": 1, "graphql": 2}
    ttl = redis.ttl("inkup:shopify:token:aman-testing-store.myshopify.com:cid")
    assert 86399 - 600 - 5 <= ttl <= 86399 - 600


def test_client_credentials_rejected_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "shop_not_permitted", "error_description": "not in org"}
        )

    client = _cc_client(monkeypatch, handler, MemoryCache())
    with pytest.raises(ShopifyConfigurationError, match="not in org"):
        client.find_order("1001")


def test_revoked_cached_token_is_dropped_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = MemoryCache()
    key = "inkup:shopify:token:aman-testing-store.myshopify.com:cid"
    redis.set(key, "stale")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": "Invalid API key or access token"})

    client = _cc_client(monkeypatch, handler, redis)
    with pytest.raises(ShopifyTransientError):
        client.find_order("1001")
    assert redis.get(key) is None


# ---------------------------------------------------------------------------
# Parser: returns and delivery time
# ---------------------------------------------------------------------------
def test_rto_delivered_is_not_a_customer_delivery() -> None:
    now = datetime.now(UTC)
    rto = real_shiprocket_payload(
        now,
        current_status="RTO DELIVERED",
        shipment_status="RTO DELIVERED",
        current_status_id=10,
        shipment_status_id=10,
    )
    assert parse_tracking_event(rto).delivered is False

    flagged = real_shiprocket_payload(now, is_return=1)
    assert parse_tracking_event(flagged).delivered is False


@pytest.mark.parametrize(
    "raw",
    ["23 05 2023 11:43:52", "2023-05-23 11:43:52", "2023-05-23T11:43:52+05:30"],
)
def test_parse_delivery_time_is_ist(raw: str) -> None:
    assert parse_delivery_time(raw) == datetime(2023, 5, 23, 6, 13, 52, tzinfo=UTC)


def test_parse_delivery_time_garbage() -> None:
    assert parse_delivery_time("not a date") is None
    assert parse_delivery_time(None) is None


def test_due_time_counts_from_delivery() -> None:
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    due = reviews.due_time(now - timedelta(hours=5), now=now, delay_seconds=THREE_DAYS)
    assert due == now + timedelta(days=3) - timedelta(hours=5)

    # Unknown or future delivery time: count from now.
    assert reviews.due_time(None, now=now, delay_seconds=THREE_DAYS) == now + timedelta(days=3)
    future = reviews.due_time(now + timedelta(days=1), now=now, delay_seconds=THREE_DAYS)
    assert future == now + timedelta(days=3)


def test_email_shows_single_hash_for_shopify_order_name() -> None:
    job = ReviewJob(
        job_id="j",
        order_id="1001",
        order_name="#1001",
        email="a@b.com",
        items=(LineItem("Classic Tee", "classic-tee"),),
        due_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
    )
    rendered = render_review_email(job)
    assert "order #1001 " in rendered.text and "##" not in rendered.text
    assert "/products/classic-tee#judgeme_product_reviews" in rendered.html


# ---------------------------------------------------------------------------
# Webhook -> SQLite -> background sender
# ---------------------------------------------------------------------------
class FakeShop:
    """Stands in for ShopifyClient inside the review flow."""

    order: ShopifyOrder | None = SHOP_ORDER
    skus: dict[str, LineItem] = {}  # noqa: RUF012
    calls: list[str] = []  # noqa: RUF012

    def __init__(self, settings: Any = None) -> None:
        pass

    def __enter__(self) -> FakeShop:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def find_order(self, number: str) -> ShopifyOrder | None:
        FakeShop.calls.append(f"order:{number}")
        return FakeShop.order

    def find_product_by_sku(self, sku: str) -> LineItem | None:
        FakeShop.calls.append(f"sku:{sku}")
        return FakeShop.skus.get(sku)


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch, store: ReviewStore) -> dict[str, Any]:
    monkeypatch.setenv("SHIPROCKET_API_EMAIL", "api@example.com")
    monkeypatch.setenv("SHIPROCKET_API_PASSWORD", "pw")
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("REVIEW_DELAY_SECONDS", str(THREE_DAYS))
    get_settings.cache_clear()
    FakeShop.order, FakeShop.skus, FakeShop.calls = SHOP_ORDER, {}, []
    env: dict[str, Any] = {
        "sr_lookups": [],
        "sr_order": SR_ORDER,
        "sent": [],
        "email_error": None,
        "store": store,
    }

    def fake_sr(**kwargs: Any) -> ShiprocketOrder | None:
        env["sr_lookups"].append({k: v for k, v in kwargs.items() if k != "settings"})
        if isinstance(env["sr_order"], Exception):
            raise env["sr_order"]
        return env["sr_order"]

    def fake_send(job: ReviewJob, settings: Any = None) -> SentEmail:
        if env["email_error"] is not None:
            error, env["email_error"] = env["email_error"], None
            raise error
        env["sent"].append(job)
        return SentEmail(id=f"email-{len(env['sent'])}")

    monkeypatch.setattr(reviews, "find_shiprocket_order", fake_sr)
    monkeypatch.setattr(reviews, "ShopifyClient", FakeShop)
    monkeypatch.setattr(reviews, "send_review_email", fake_send)
    return env


def _accept(payload: dict[str, Any]) -> dict[str, Any]:
    return reviews.accept_webhook(payload)


def _due_ts(result: dict[str, Any]) -> float:
    return datetime.fromisoformat(result["due_at"]).timestamp()


def test_webhook_saves_order_due_three_days_after_delivery(flow: dict[str, Any]) -> None:
    delivered = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    result = _accept(real_shiprocket_payload(delivered))

    assert result["status"] == "scheduled"
    assert result["key"] == "1001"
    assert result["due_at"] == (delivered + timedelta(days=3)).isoformat()
    assert flow["sr_lookups"] == []  # no outside calls while Shiprocket waits

    again = _accept(real_shiprocket_payload(delivered))
    assert again == {"status": "duplicate", "key": "1001"}


def test_nothing_is_sent_before_it_is_due(flow: dict[str, Any]) -> None:
    _accept(real_shiprocket_payload(datetime.now(UTC)))
    assert reviews.send_due_reviews()["sent"] == 0
    assert flow["sent"] == []


def test_due_order_is_sent_once_with_shopify_links(flow: dict[str, Any]) -> None:
    result = _accept(real_shiprocket_payload(datetime.now(UTC) - timedelta(hours=1)))
    summary = reviews.send_due_reviews(now=_due_ts(result) + 1)

    assert summary == {"sent": 1, "skipped": 0, "retry": 0, "failed": 0}
    assert flow["sr_lookups"] == [
        {"sr_order_id": "348456385", "awb": "19041424751540", "order_id": "1001"}
    ]
    assert FakeShop.calls == ["order:1001"]
    job = flow["sent"][0]
    assert job.email == "Buyer@example.com"  # from Shiprocket
    assert job.customer_name == "Asha"
    assert job.order_name == "#1001"
    assert [item.handle for item in job.items] == ["classic-tee", "red-hoodie"]
    assert job.job_id == reviews.make_job_id("1001", "Buyer@example.com")

    row = flow["store"].get("1001")
    assert row is not None and row.status == "sent"
    assert reviews.send_due_reviews(now=_due_ts(result) + 10_000)["sent"] == 0


def test_falls_back_to_sku_then_to_name_slugs(flow: dict[str, Any]) -> None:
    FakeShop.order = None
    FakeShop.skus = {
        "TEE-M": LineItem("Classic Tee", "classic-tee"),
        "HOOD-1": LineItem("Red Hoodie", "red-hoodie-2024"),
    }
    first = _accept(real_shiprocket_payload(datetime.now(UTC), order_id="A1"))
    reviews.send_due_reviews(now=_due_ts(first) + 1)
    assert [i.handle for i in flow["sent"][0].items] == ["classic-tee", "red-hoodie-2024"]
    assert flow["sent"][0].order_name == "#1001"

    # Shopify knows neither the order nor the SKUs: guess from product names.
    FakeShop.skus = {}
    second = _accept(real_shiprocket_payload(datetime.now(UTC), order_id="A2"))
    reviews.send_due_reviews(now=_due_ts(second) + 1)
    assert [i.handle for i in flow["sent"][1].items] == ["classic-tee-m", "red-hoodie"]


def test_old_delivery_is_ignored(flow: dict[str, Any]) -> None:
    result = _accept(real_shiprocket_payload(datetime.now(UTC) - timedelta(days=30)))
    assert result == {"status": "ignored", "reason": "delivery_too_old"}
    assert flow["store"].counts() == {}


def test_in_transit_is_not_saved(flow: dict[str, Any]) -> None:
    payload = real_shiprocket_payload(
        datetime.now(UTC),
        current_status="IN TRANSIT",
        shipment_status="IN TRANSIT",
        current_status_id=20,
        shipment_status_id=18,
        scans=[],
    )
    assert _accept(payload) == {"status": "ignored", "reason": "not_delivered"}
    assert flow["store"].counts() == {}


def test_order_missing_in_shiprocket_is_skipped(flow: dict[str, Any]) -> None:
    flow["sr_order"] = None
    result = _accept(real_shiprocket_payload(datetime.now(UTC)))
    summary = reviews.send_due_reviews(now=_due_ts(result) + 1)
    assert summary["skipped"] == 1
    row = flow["store"].get("1001")
    assert row is not None and row.status == "skipped"
    assert "customer_email" in (row.last_error or "")


def test_temporary_email_error_is_retried_later(flow: dict[str, Any]) -> None:
    result = _accept(real_shiprocket_payload(datetime.now(UTC)))
    now = _due_ts(result) + 1
    flow["email_error"] = TransientEmailError("Resend 503")

    assert reviews.send_due_reviews(now=now)["retry"] == 1
    row = flow["store"].get("1001")
    assert row is not None and row.status == "pending" and row.attempts == 1
    assert row.due_at == now + 600

    assert reviews.send_due_reviews(now=now + 1)["sent"] == 0  # not yet
    assert reviews.send_due_reviews(now=now + 601)["sent"] == 1


def test_wrong_shiprocket_password_does_not_lose_the_email(flow: dict[str, Any]) -> None:
    flow["sr_order"] = ShiprocketConfigurationError("Invalid email and password")
    result = _accept(real_shiprocket_payload(datetime.now(UTC)))
    assert reviews.send_due_reviews(now=_due_ts(result) + 1)["retry"] == 1

    flow["sr_order"] = SR_ORDER  # password fixed
    assert reviews.send_due_reviews(now=_due_ts(result) + 700)["sent"] == 1


def test_permanent_email_error_marks_failed(flow: dict[str, Any]) -> None:
    flow["email_error"] = PermanentEmailError("Resend 422: invalid to address")
    result = _accept(real_shiprocket_payload(datetime.now(UTC)))
    assert reviews.send_due_reviews(now=_due_ts(result) + 1)["failed"] == 1
    row = flow["store"].get("1001")
    assert row is not None and row.status == "failed"


def test_gives_up_after_max_attempts(flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    result = _accept(real_shiprocket_payload(datetime.now(UTC)))
    now = _due_ts(result) + 1

    flow["email_error"] = TransientEmailError("down")
    assert reviews.send_due_reviews(now=now)["retry"] == 1
    flow["email_error"] = TransientEmailError("still down")
    assert reviews.send_due_reviews(now=now + 10_000)["failed"] == 1
    row = flow["store"].get("1001")
    assert row is not None and row.status == "failed"


def test_webhook_endpoint_requires_token(client: TestClient) -> None:
    response = client.post("/webhook/tracking-update", json={"awb": "1"})
    assert response.status_code == 401
    response = client.post(
        "/webhook/tracking-update", json={"awb": "1"}, headers={"x-api-key": "wrong"}
    )
    assert response.status_code == 401


def test_webhook_endpoint_saves_delivered_order(client: TestClient, store: ReviewStore) -> None:
    payload = real_shiprocket_payload(datetime.now(UTC))
    response = client.post(
        "/webhook/tracking-update", json=payload, headers={"x-api-key": WEBHOOK_TOKEN}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert store.counts() == {"pending": 1}
    assert client.get("/health/ready").status_code == 200


# ---------------------------------------------------------------------------
# Shiprocket client
# ---------------------------------------------------------------------------
SR_SHOW = {
    "data": {
        "id": 348456385,
        "channel_order_id": "1001",
        "customer_name": "Asha Rao",
        "customer_email": "asha@example.com",
        "products": [
            {"name": "Classic Tee - M", "channel_sku": "TEE-M"},
            {"name": "Red Hoodie", "channel_sku": "", "sku": "HOOD-1"},
        ],
    }
}


def _sr_client(
    monkeypatch: pytest.MonkeyPatch, handler: Any, redis: MemoryCache
) -> ShiprocketClient:
    monkeypatch.setenv("SHIPROCKET_API_EMAIL", "api@example.com")
    monkeypatch.setenv("SHIPROCKET_API_PASSWORD", "pw")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ShiprocketClient(get_settings(), http=http, redis=redis)


def test_shiprocket_login_cached_and_order_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/external/auth/login":
            assert json.loads(request.content) == {"email": "api@example.com", "password": "pw"}
            return httpx.Response(200, json={"token": "srtok"})
        assert request.headers["Authorization"] == "Bearer srtok"
        assert request.url.path == "/v1/external/orders/show/348456385"
        return httpx.Response(200, json=SR_SHOW)

    client = _sr_client(monkeypatch, handler, MemoryCache())
    order = client.find_order(sr_order_id="348456385")
    client.find_order(sr_order_id="348456385")

    assert order == ShiprocketOrder(
        sr_order_id="348456385",
        channel_order_id="1001",
        email="asha@example.com",
        customer_name="Asha Rao",
        products=(
            ShiprocketProduct("Classic Tee - M", "TEE-M"),
            ShiprocketProduct("Red Hoodie", "HOOD-1"),
        ),
    )
    assert calls.count("/v1/external/auth/login") == 1


def test_shiprocket_expired_token_relogs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = MemoryCache()
    redis.set("inkup:shiprocket:token:api@example.com", "old")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/external/auth/login":
            return httpx.Response(200, json={"token": "new"})
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401, json={"message": "Token has expired"})
        return httpx.Response(200, json=SR_SHOW)

    order = _sr_client(monkeypatch, handler, redis).find_order(sr_order_id="348456385")
    assert order is not None
    assert seen == ["Bearer old", "Bearer new"]


def test_shiprocket_bad_login_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid email and password combination"})

    client = _sr_client(monkeypatch, handler, MemoryCache())
    with pytest.raises(ShiprocketConfigurationError, match="Invalid email"):
        client.find_order(sr_order_id="1")


def test_shiprocket_search_by_awb_when_no_sr_order_id(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {**SR_SHOW["data"], "shipments": [{"awb": "AWB123"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/external/auth/login":
            return httpx.Response(200, json={"token": "t"})
        assert request.url.path == "/v1/external/orders"
        assert request.url.params["search"] == "AWB123"
        return httpx.Response(200, json={"data": [row]})

    client = _sr_client(monkeypatch, handler, MemoryCache())
    order = client.find_order(sr_order_id=None, awb="AWB123")
    assert order is not None and order.email == "asha@example.com"


def test_shopify_variant_sku_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["variables"] == {"q": 'sku:"TEE-M"'}
        return httpx.Response(
            200,
            json={
                "data": {
                    "productVariants": {
                        "nodes": [
                            {"sku": "TEE-MX", "product": {"handle": "wrong", "title": "W"}},
                            {
                                "sku": "TEE-M",
                                "product": {"handle": "classic-tee", "title": "Classic Tee"},
                            },
                        ]
                    }
                }
            },
        )

    item = _client(monkeypatch, handler).find_product_by_sku("TEE-M")
    assert item == LineItem("Classic Tee", "classic-tee")
