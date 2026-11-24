"""Fetch buyer details for a delivered shipment from the Shiprocket API.

The tracking webhook has no email or products. Shiprocket's order API does:
``customer_email``, ``customer_name``, ``channel_order_id`` (the Shopify order
number for Shopify-channel orders) and ``products``. This matters on Shopify's
Basic plan, where Shopify withholds customer email from custom apps.

Auth: an API user (Shiprocket -> Settings -> API -> Configure -> Create an API
User) logs in with email + password and receives a bearer token valid for 10
days. The token is cached in memory and refreshed automatically.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..cache import TokenCache
from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

# Shiprocket tokens last 240 hours; refresh well before that.
_TOKEN_TTL_SECONDS = 9 * 24 * 3600


class ShiprocketError(RuntimeError):
    """Base class for Shiprocket API failures."""


class ShiprocketTransientError(ShiprocketError):
    """Network error, rate limit or Shiprocket outage; safe to retry."""


class ShiprocketConfigurationError(ShiprocketError):
    """Bad API user credentials or permissions; retrying will not help."""


@dataclass(frozen=True, slots=True)
class ShiprocketProduct:
    name: str
    sku: str | None


@dataclass(frozen=True, slots=True)
class ShiprocketOrder:
    sr_order_id: str
    channel_order_id: str | None
    email: str | None
    customer_name: str | None
    products: tuple[ShiprocketProduct, ...]


def find_shiprocket_order(
    *,
    sr_order_id: str | None,
    awb: str | None = None,
    order_id: str | None = None,
    settings: Settings | None = None,
) -> ShiprocketOrder | None:
    with ShiprocketClient(settings) as client:
        return client.find_order(sr_order_id=sr_order_id, awb=awb, order_id=order_id)


class ShiprocketClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http: httpx.Client | None = None,
        redis: TokenCache | None = None,
    ):
        self.settings = settings or get_settings()
        if not self.settings.shiprocket_configured:
            raise ShiprocketConfigurationError(
                "Shiprocket API is not configured: set SHIPROCKET_API_EMAIL and "
                "SHIPROCKET_API_PASSWORD"
            )
        self._base = self.settings.shiprocket_api_base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=self.settings.shiprocket_timeout_seconds)
        self._redis = redis

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ShiprocketClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def find_order(
        self,
        *,
        sr_order_id: str | None,
        awb: str | None = None,
        order_id: str | None = None,
    ) -> ShiprocketOrder | None:
        """Look the order up by Shiprocket id, falling back to an AWB/order search."""
        if sr_order_id:
            body = self._get(f"/v1/external/orders/show/{sr_order_id}")
            data = body.get("data") if isinstance(body, Mapping) else None
            if isinstance(data, Mapping) and data:
                return _parse_order(data)

        for term in (awb, order_id):
            if not term:
                continue
            body = self._get("/v1/external/orders", params={"search": term})
            rows = body.get("data") if isinstance(body, Mapping) else None
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, Mapping) and _matches(row, awb=awb, order_id=order_id):
                        return _parse_order(row)
        return None

    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        for attempt in (1, 2):
            token = self._token()
            try:
                response = self._http.get(
                    self._base + path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise ShiprocketTransientError(f"Shiprocket request failed: {exc}") from exc

            status = response.status_code
            if status == 401 and attempt == 1:
                # Token expired or was revoked: log in again once.
                self._forget_token()
                continue
            if status == 404:
                return {}
            if status in (401, 403):
                raise ShiprocketConfigurationError(
                    f"Shiprocket rejected the API user (HTTP {status}); check the API "
                    "user's permissions (it needs order and buyer details access)"
                )
            if status == 429 or status >= 500:
                raise ShiprocketTransientError(f"Shiprocket unavailable (HTTP {status})")
            if status >= 400:
                raise ShiprocketConfigurationError(f"Shiprocket request rejected (HTTP {status})")
            try:
                return response.json()
            except ValueError as exc:
                raise ShiprocketTransientError("Shiprocket returned a non-JSON response") from exc
        raise ShiprocketTransientError("Shiprocket token refresh failed")

    def _token_key(self) -> str:
        return f"inkup:shiprocket:token:{(self.settings.shiprocket_api_email or '').lower()}"

    def _get_redis(self) -> TokenCache:
        if self._redis is None:
            from ..cache import get_cache

            self._redis = get_cache()
        return self._redis

    def _forget_token(self) -> None:
        self._get_redis().delete(self._token_key())

    def _token(self) -> str:
        redis = self._get_redis()
        cached = redis.get(self._token_key())
        if cached:
            return str(cached)

        password = self.settings.shiprocket_api_password
        assert password is not None
        try:
            response = self._http.post(
                self._base + "/v1/external/auth/login",
                json={
                    "email": self.settings.shiprocket_api_email,
                    "password": password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:
            raise ShiprocketTransientError(f"Shiprocket login failed: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise ShiprocketTransientError(
                f"Shiprocket login unavailable (HTTP {response.status_code})"
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        token = body.get("token") if isinstance(body, Mapping) else None
        if response.status_code >= 400 or not token:
            message = body.get("message") if isinstance(body, Mapping) else None
            raise ShiprocketConfigurationError(
                f"Shiprocket login refused (HTTP {response.status_code}"
                f"{': ' + str(message)[:200] if message else ''}); check SHIPROCKET_API_EMAIL "
                "and SHIPROCKET_API_PASSWORD (use an API user, not your main login)"
            )
        redis.set(self._token_key(), str(token), ex=_TOKEN_TTL_SECONDS)
        logger.info("shiprocket_token_refreshed")
        return str(token)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _matches(row: Mapping[str, Any], *, awb: str | None, order_id: str | None) -> bool:
    if order_id and order_id in {
        _clean(row.get("channel_order_id")),
        _clean(row.get("id")),
    }:
        return True
    if awb:
        for shipment in row.get("shipments") or []:
            if isinstance(shipment, Mapping) and _clean(shipment.get("awb")) == awb:
                return True
        if _clean(row.get("awb_code")) == awb or _clean(row.get("awb")) == awb:
            return True
    return False


def _parse_order(data: Mapping[str, Any]) -> ShiprocketOrder:
    products: list[ShiprocketProduct] = []
    raw_products = data.get("products") or data.get("order_items") or []
    if isinstance(raw_products, list):
        for product in raw_products:
            if not isinstance(product, Mapping):
                continue
            name = _clean(product.get("name"))
            if not name:
                continue
            sku = _clean(product.get("channel_sku")) or _clean(product.get("sku"))
            products.append(ShiprocketProduct(name=name[:300], sku=sku))

    email = _clean(data.get("customer_email")) or _clean(data.get("billing_email"))
    name = _clean(data.get("customer_name")) or _clean(data.get("billing_customer_name"))
    return ShiprocketOrder(
        sr_order_id=str(data.get("id") or ""),
        channel_order_id=_clean(data.get("channel_order_id")),
        email=email,
        customer_name=name,
        products=tuple(products),
    )
