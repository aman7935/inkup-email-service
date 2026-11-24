from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shiprocket's shipment status id for "Delivered". See:
# https://docs.rs/shiprocket/latest/shiprocket/webhook/enum.ShipmentStatus.html
DEFAULT_DELIVERED_STATUS_IDS = "7"

# Exactly 3 days. This is the default for REVIEW_DELAY_SECONDS.
REVIEW_DELAY_SECONDS = 259_200


class Settings(BaseSettings):
    """Runtime configuration.

    Every value can be supplied as an environment variable or in a local `.env`
    file. Secrets are wrapped in :class:`SecretStr` so they are not accidentally
    written to logs or tracebacks.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "inkup-email-service"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = True
    enable_docs: bool = True

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # SQLite file holding delivered orders until their review email is sent.
    # On Render, point this at the persistent disk (e.g. /var/data/inkup.db).
    database_path: str = "data/inkup.db"

    # Webhook receiver
    webhook_token: SecretStr | None = None
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1)
    delivered_status_ids: str = DEFAULT_DELIVERED_STATUS_IDS

    # Delayed review request
    review_delay_seconds: int = Field(default=REVIEW_DELAY_SECONDS, ge=0)
    review_max_attempts: int = Field(default=24, ge=1)
    # How often the background loop looks for due emails, and how many it sends per check.
    review_check_interval_seconds: int = Field(default=300, ge=10)
    review_batch_size: int = Field(default=50, ge=1)
    # Turn the background sender off (tests, or a second read-only instance).
    sender_enabled: bool = True
    # Ignore "delivered" events whose delivery time is older than this, so a
    # replayed or very late webhook cannot email a customer weeks later.
    review_max_delivery_age_seconds: int = Field(default=1_209_600, ge=0)  # 14 days
    # Shiprocket timestamps have no timezone; they are Indian Standard Time.
    shiprocket_timezone: str = "Asia/Kolkata"

    # Shiprocket API user (buyer email/name + products for a delivered order).
    shiprocket_api_email: str | None = None
    shiprocket_api_password: SecretStr | None = None
    shiprocket_api_base_url: str = "https://apiv2.shiprocket.in"
    shiprocket_timeout_seconds: int = Field(default=15, ge=1)

    # Shopify Admin API (exact product handles; no customer data is requested).
    # Dev Dashboard apps (all apps created since 2026-01-01): client id + secret.
    shopify_client_id: str | None = None
    shopify_client_secret: SecretStr | None = None
    # Legacy admin-created custom app token (shpat_...); used instead if set.
    shopify_admin_api_token: SecretStr | None = None
    shopify_api_version: str = "2026-10"
    shopify_timeout_seconds: int = Field(default=10, ge=1)

    # Storefront / email content
    shopify_store_domain: str = "aman-testing-store.myshopify.com"
    storefront_base_url: str | None = None
    store_name: str = "Aman Testing Store"
    support_email: EmailStr | None = None
    review_email_subject: str = "How was your order?"

    # Resend
    resend_api_key: SecretStr | None = None
    resend_from_email: EmailStr | None = None
    resend_from_name: str = "Aman Testing Store"
    resend_reply_to: EmailStr | None = None
    resend_timeout_seconds: int = Field(default=15, ge=1)

    @field_validator(
        "storefront_base_url",
        "support_email",
        "resend_from_email",
        "resend_reply_to",
        "shopify_client_id",
        "shiprocket_api_email",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "webhook_token",
        "resend_api_key",
        "shopify_admin_api_token",
        "shopify_client_secret",
        "shiprocket_api_password",
        mode="before",
    )
    @classmethod
    def _empty_secret_to_none(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return level

    @field_validator("shopify_store_domain")
    @classmethod
    def _normalise_domain(cls, value: str) -> str:
        domain = value.strip().removeprefix("https://").removeprefix("http://").strip("/")
        if not domain:
            raise ValueError("SHOPIFY_STORE_DOMAIN must not be empty")
        return domain

    @field_validator("storefront_base_url")
    @classmethod
    def _normalise_storefront_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("STOREFRONT_BASE_URL must start with http:// or https://")
        return url

    @field_validator("delivered_status_ids", mode="before")
    @classmethod
    def _normalise_status_ids(cls, value: Any) -> str:
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(part) for part in value)
        if isinstance(value, int):
            return str(value)
        raw = str(value).strip()
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        if not parts:
            raise ValueError("DELIVERED_STATUS_IDS must contain at least one numeric id")
        for part in parts:
            try:
                int(part)
            except ValueError as exc:
                raise ValueError(f"Invalid delivered status id: {part!r}") from exc
        return ",".join(parts)

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------
    @property
    def delivered_status_id_set(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.delivered_status_ids.split(",") if part)

    @property
    def shiprocket_configured(self) -> bool:
        return bool(self.shiprocket_api_email and self.shiprocket_api_password is not None)

    @property
    def shopify_configured(self) -> bool:
        if self.shopify_admin_api_token is not None:
            return True
        return bool(self.shopify_client_id and self.shopify_client_secret is not None)

    @property
    def storefront_url(self) -> str:
        base = self.storefront_base_url or f"https://{self.shopify_store_domain}"
        return base.rstrip("/")

    @property
    def from_address(self) -> str | None:
        if self.resend_from_email is None:
            return None
        name = self.resend_from_name.strip()
        email = str(self.resend_from_email)
        return f"{name} <{email}>" if name else email

    def production_configuration_errors(self) -> list[str]:
        """Return configuration problems that must block production readiness."""
        if self.environment != "production":
            return []

        problems: list[str] = []
        if self.webhook_token is None:
            problems.append("WEBHOOK_TOKEN")
        if self.resend_api_key is None:
            problems.append("RESEND_API_KEY")
        if self.resend_from_email is None:
            problems.append("RESEND_FROM_EMAIL")
        if not self.shiprocket_configured:
            problems.append("SHIPROCKET_API_EMAIL/SHIPROCKET_API_PASSWORD")
        if not self.shopify_configured:
            problems.append("SHOPIFY_CLIENT_ID/SHOPIFY_CLIENT_SECRET")
        if self.review_delay_seconds != REVIEW_DELAY_SECONDS:
            problems.append("REVIEW_DELAY_SECONDS")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
