"""Resolve the exact Shopify product handles for a delivered order.

The Judge.me review links need real product handles. This module reads them
from the Shopify Admin GraphQL API: the order's line items (by order number),
or a variant SKU. It deliberately requests no customer fields — on Shopify's
Basic plan custom apps cannot read customer PII, so the buyer's email comes
from Shiprocket instead (see ``services/shiprocket.py``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..cache import TokenCache
from ..config import Settings, get_settings
from ..domain.models import LineItem

logger = logging.getLogger(__name__)

MAX_LINE_ITEMS = 50

_ORDER_FIELDS = """
fragment ReviewOrder on Order {
  id
  name
  lineItems(first: MAX_LINE_ITEMS) {
    nodes {
      title
      product { handle title }
    }
  }
}
""".replace("MAX_LINE_ITEMS", str(MAX_LINE_ITEMS))

_ORDER_BY_ID = "query OrderById($id: ID!) { order(id: $id) { ...ReviewOrder } }" + _ORDER_FIELDS
_ORDER_BY_QUERY = (
    "query OrderByName($q: String!) { orders(first: 5, query: $q) { nodes { ...ReviewOrder } } }"
    + _ORDER_FIELDS
)

_VARIANT_BY_SKU = """
query VariantBySku($q: String!) {
  productVariants(first: 5, query: $q) { nodes { sku product { handle title } } }
}
"""

# Shopify order ids are 64-bit integers, far longer than order numbers.
_MIN_ORDER_ID_DIGITS = 10


class ShopifyError(RuntimeError):
    """Base class for Shopify lookup failures."""


class ShopifyTransientError(ShopifyError):
    """Network error, rate limit or Shopify outage; the lookup can be retried."""


class ShopifyConfigurationError(ShopifyError):
    """Bad token, missing scope or wrong store; retrying will not help."""


@dataclass(frozen=True, slots=True)
class ShopifyOrder:
    """Non-PII order data: readable on every Shopify plan, including Basic."""

    gid: str
    name: str
    items: tuple[LineItem, ...]


def order_lookup_candidates(order_id: str) -> list[str]:
    """Return the identifiers worth trying for a Shiprocket ``order_id``.

    Shiprocket shows the Shopify order number for channel orders (e.g. ``1001``
    or ``#1001``), but some accounts send a composite like ``1373900_150876814``.
    Each part is tried in order; the exact mapping should be confirmed against
    a real order from the store.
    """
    raw = order_id.strip()
    candidates: list[str] = []

    def add(value: str) -> None:
        value = value.strip().lstrip("#").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(raw)
    for part in re.split(r"[_\-/|\s]+", raw):
        add(part)
    return candidates


class ShopifyClient:
    """Minimal Shopify Admin GraphQL client.

    Two ways to authenticate:

    * ``SHOPIFY_CLIENT_ID`` + ``SHOPIFY_CLIENT_SECRET`` (apps made in the Dev
      Dashboard — the only option for apps created after 2026-01-01). The
      client credentials grant returns a token valid for 24 hours; it is cached
      in memory and refreshed before it expires.
    * ``SHOPIFY_ADMIN_API_TOKEN`` (``shpat_...``) from a legacy admin-created
      custom app, used as-is.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http: httpx.Client | None = None,
        redis: TokenCache | None = None,
    ):
        self.settings = settings or get_settings()
        if not self.settings.shopify_configured:
            raise ShopifyConfigurationError(
                "Shopify is not configured: set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET "
                "(or SHOPIFY_ADMIN_API_TOKEN for a legacy custom app)"
            )
        domain = self.settings.shopify_store_domain
        self._endpoint = (
            f"https://{domain}/admin/api/{self.settings.shopify_api_version}/graphql.json"
        )
        self._token_endpoint = f"https://{domain}/admin/oauth/access_token"
        self._http = http or httpx.Client(timeout=self.settings.shopify_timeout_seconds)
        self._redis = redis

    # ------------------------------------------------------------------
    # Access token
    # ------------------------------------------------------------------
    @property
    def _uses_client_credentials(self) -> bool:
        return self.settings.shopify_admin_api_token is None

    def _token_cache_key(self) -> str:
        client_id = self.settings.shopify_client_id or ""
        return f"inkup:shopify:token:{self.settings.shopify_store_domain}:{client_id}"

    def _get_redis(self) -> TokenCache:
        if self._redis is None:
            from ..cache import get_cache

            self._redis = get_cache()
        return self._redis

    def _access_token(self) -> str:
        static = self.settings.shopify_admin_api_token
        if static is not None:
            return static.get_secret_value()

        redis = self._get_redis()
        cached = redis.get(self._token_cache_key())
        if cached:
            return str(cached)

        token, expires_in = self._request_client_credentials_token()
        # Refresh 10 minutes early so a token never expires mid-request.
        ttl = max(60, expires_in - 600)
        redis.set(self._token_cache_key(), token, ex=ttl)
        logger.info("shopify_access_token_refreshed", extra={"expires_in": expires_in})
        return token

    def _forget_token(self) -> None:
        if self._uses_client_credentials:
            self._get_redis().delete(self._token_cache_key())

    def _request_client_credentials_token(self) -> tuple[str, int]:
        secret = self.settings.shopify_client_secret
        assert secret is not None and self.settings.shopify_client_id
        try:
            response = self._http.post(
                self._token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.shopify_client_id,
                    "client_secret": secret.get_secret_value(),
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ShopifyTransientError(f"Shopify token request failed: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise ShopifyTransientError(
                f"Shopify token endpoint unavailable (HTTP {response.status_code})"
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, Mapping) else None
        if response.status_code >= 400 or not token:
            detail = ""
            if isinstance(body, Mapping):
                detail = str(body.get("error_description") or body.get("error") or "")
            raise ShopifyConfigurationError(
                f"Shopify refused the client credentials (HTTP {response.status_code}"
                f"{': ' + detail[:300] if detail else ''}); check SHOPIFY_CLIENT_ID, "
                "SHOPIFY_CLIENT_SECRET, SHOPIFY_STORE_DOMAIN and that the app is installed "
                "on this store"
            )
        try:
            expires_in = int(body.get("expires_in") or 86_399)
        except (TypeError, ValueError):
            expires_in = 86_399
        scopes = str(body.get("scope") or "")
        missing = {"read_orders", "read_products"} - set(scopes.split(","))
        if scopes and missing:
            logger.warning(
                "shopify_token_missing_scopes",
                extra={"missing": sorted(missing), "granted": scopes},
            )
        return str(token), expires_in

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ShopifyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def find_order(self, shiprocket_order_id: str) -> ShopifyOrder | None:
        """Find the Shopify order for a Shiprocket order id, or ``None``."""
        for candidate in order_lookup_candidates(shiprocket_order_id):
            if candidate.isdigit() and len(candidate) >= _MIN_ORDER_ID_DIGITS:
                order = self._order_by_id(candidate)
                if order is not None:
                    return order
            order = self._order_by_name(candidate)
            if order is not None:
                return order
        return None

    def find_product_by_sku(self, sku: str) -> LineItem | None:
        """Resolve a variant SKU (as Shiprocket stores it) to its product handle."""
        sku = sku.strip()
        if not sku:
            return None
        escaped = sku.replace("\\", "\\\\").replace('"', '\\"')
        data = self._graphql(_VARIANT_BY_SKU, {"q": f'sku:"{escaped}"'})
        nodes = (data.get("productVariants") or {}).get("nodes") or []
        for node in nodes:
            if not isinstance(node, Mapping) or str(node.get("sku") or "").strip() != sku:
                continue
            product = node.get("product")
            if isinstance(product, Mapping) and product.get("handle"):
                handle = str(product["handle"]).strip().lower()
                return LineItem(name=str(product.get("title") or handle)[:300], handle=handle)
        return None

    # ------------------------------------------------------------------
    def _order_by_id(self, numeric_id: str) -> ShopifyOrder | None:
        data = self._graphql(_ORDER_BY_ID, {"id": f"gid://shopify/Order/{numeric_id}"})
        node = data.get("order")
        return _parse_order(node) if isinstance(node, Mapping) else None

    def _order_by_name(self, number: str) -> ShopifyOrder | None:
        # `name` is the "#1001" label shown in the admin. Search can be fuzzy,
        # so only accept an exact match.
        query = f'name:"#{number}" OR name:"{number}"'
        data = self._graphql(_ORDER_BY_QUERY, {"q": query})
        nodes = (data.get("orders") or {}).get("nodes") or []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            name = str(node.get("name") or "").lstrip("#").strip()
            if name.lower() == number.lower():
                return _parse_order(node)
        return None

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "X-Shopify-Access-Token": self._access_token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = self._http.post(
                self._endpoint,
                headers=headers,
                json={"query": query, "variables": variables},
            )
        except httpx.HTTPError as exc:
            raise ShopifyTransientError(f"Shopify request failed: {exc}") from exc

        status = response.status_code
        if status == 401 and self._uses_client_credentials:
            # Cached token was revoked or expired early: drop it and retry later
            # with a fresh one.
            self._forget_token()
            raise ShopifyTransientError("Shopify access token rejected (HTTP 401); refreshing")
        if status in (401, 403):
            raise ShopifyConfigurationError(
                f"Shopify rejected the request (HTTP {status}); check the credentials "
                "and that the app has the read_orders and read_products scopes"
            )
        if status == 404:
            raise ShopifyConfigurationError(
                f"Shopify endpoint not found (HTTP 404): check SHOPIFY_STORE_DOMAIN "
                f"and SHOPIFY_API_VERSION ({self._endpoint})"
            )
        if status == 429 or status >= 500:
            raise ShopifyTransientError(f"Shopify temporarily unavailable (HTTP {status})")
        if status >= 400:
            raise ShopifyConfigurationError(f"Shopify request rejected (HTTP {status})")

        try:
            body = response.json()
        except ValueError as exc:
            raise ShopifyTransientError("Shopify returned a non-JSON response") from exc

        errors = body.get("errors") if isinstance(body, Mapping) else None
        if errors:
            if _is_throttled(errors):
                raise ShopifyTransientError("Shopify GraphQL query was throttled")
            raise ShopifyConfigurationError(f"Shopify GraphQL error: {_error_text(errors)}")

        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, dict):
            raise ShopifyTransientError("Shopify response had no data")
        return data


def _parse_order(node: Mapping[str, Any]) -> ShopifyOrder:
    items: list[LineItem] = []
    seen: set[str] = set()
    line_items = (node.get("lineItems") or {}).get("nodes") or []
    for line in line_items:
        if not isinstance(line, Mapping):
            continue
        product = line.get("product")
        # Deleted products and custom line items have no product page to review.
        if not isinstance(product, Mapping) or not product.get("handle"):
            continue
        handle = str(product["handle"]).strip().lower()
        if handle in seen:
            continue
        seen.add(handle)
        name = str(product.get("title") or line.get("title") or handle).strip()
        items.append(LineItem(name=name[:300], handle=handle))

    return ShopifyOrder(
        gid=str(node.get("id") or ""),
        name=str(node.get("name") or ""),
        items=tuple(items),
    )


def _is_throttled(errors: Any) -> bool:
    for error in _iter_errors(errors):
        code = ((error.get("extensions") or {}) if isinstance(error, Mapping) else {}).get("code")
        if code == "THROTTLED":
            return True
    return False


def _error_text(errors: Any) -> str:
    messages = [
        str(error.get("message")) if isinstance(error, Mapping) else str(error)
        for error in _iter_errors(errors)
    ]
    return "; ".join(messages)[:500] or str(errors)[:500]


def _iter_errors(errors: Any) -> Iterable[Any]:
    if isinstance(errors, list):
        return errors
    return [errors]
