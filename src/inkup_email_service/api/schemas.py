from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WebhookAck(BaseModel):
    """Shiprocket only needs a fast 2xx; this is the entire response body."""

    model_config = ConfigDict(frozen=True)

    status: Literal["received"] = "received"


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "unavailable"]
    checks: dict[str, str] = Field(default_factory=dict)
