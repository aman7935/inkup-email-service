from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inkup_email_service.cache import get_cache
from inkup_email_service.config import get_settings
from inkup_email_service.main import create_app
from inkup_email_service.storage import ReviewStore, get_store

WEBHOOK_TOKEN = "test-token"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # Never let the developer's real .env (keys, passwords) leak into tests.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SENDER_ENABLED", "false")
    monkeypatch.setenv("WEBHOOK_TOKEN", WEBHOOK_TOKEN)
    get_settings.cache_clear()
    get_store.cache_clear()
    get_cache.cache_clear()
    yield
    get_settings.cache_clear()
    get_store.cache_clear()
    get_cache.cache_clear()


@pytest.fixture
def store() -> ReviewStore:
    return get_store()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def delivered_payload() -> dict[str, Any]:
    return {
        "awb": "19041424751540",
        "courier_name": "Delhivery Surface",
        "current_status": "DELIVERED",
        "current_status_id": 7,
        "shipment_status": "DELIVERED",
        "shipment_status_id": 7,
        "current_timestamp": "07 11 2026 11:43:52",
        "order_id": "1373900_150876814",
        "customer_name": "Asha Rao",
        "customer_email": "asha@example.com",
        "line_items": [
            {"name": "Classic Tee", "handle": "classic-tee"},
            {
                "name": "Red Hoodie",
                "product_url": (
                    "https://aman-testing-store.myshopify.com/products/red-hoodie?utm=email"
                ),
            },
        ],
    }
