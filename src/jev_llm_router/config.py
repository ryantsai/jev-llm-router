"""Validated, secret-free TOML configuration. Credentials come from the environment."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

TIERS = ("luna", "terra", "sol", "astra")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
Tier = Literal["luna", "terra", "sol", "astra"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelSpec(StrictModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:/-]+$")
    effort: Effort
    supported_efforts: list[Effort] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_effort(self) -> ModelSpec:
        if self.effort not in self.supported_efforts:
            raise ValueError("default effort must be in supported_efforts")
        return self


def default_models() -> dict[str, ModelSpec]:
    return {
        tier: ModelSpec(
            id=f"gpt-5.6-{tier}" if tier != "astra" else "gpt-6-astra",
            effort={"luna": "low", "terra": "medium", "sol": "high", "astra": "medium"}[tier],
            supported_efforts=list(EFFORTS if tier != "astra" else EFFORTS[1:]),
        )
        for tier in TIERS
    }


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    token_env: str = "JEV_ROUTER_TOKEN"
    max_body_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    max_inflight: int = Field(default=16, ge=1, le=1000)
    state_path: str = ".jev-router/affinity.sqlite3"
    affinity_ttl_seconds: int = Field(default=86400, ge=60, le=2592000)
    affinity_max_entries: int = Field(default=10000, ge=100, le=1000000)


class UpstreamConfig(StrictModel):
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "JEV_UPSTREAM_API_KEY"
    organization: str | None = None
    project: str | None = None
    connect_timeout: float = Field(default=10, gt=0, le=120)
    read_timeout: float = Field(default=600, gt=0, le=3600)
    trust_env: bool = False
    allow_loopback_http: bool = False

    @model_validator(mode="after")
    def validate_url(self) -> UpstreamConfig:
        url = urlsplit(self.base_url)
        local_http = (
            self.allow_loopback_http
            and url.scheme == "http"
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if not url.hostname or (url.scheme != "https" and not local_http):
            raise ValueError("upstream requires HTTPS (or explicitly allowed loopback HTTP)")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("upstream URL cannot contain credentials, query, or fragment")
        self.base_url = self.base_url.rstrip("/")
        return self


class RoutingConfig(StrictModel):
    mode: Literal["rules", "hybrid"] = "rules"
    auto_models: list[str] = Field(default_factory=lambda: ["auto", "jev-auto", "gpt-5.6-terra"])
    default_tier: Tier = "terra"
    max_tier: Tier = "astra"
    max_effort: Effort = "high"
    respect_client_effort: bool = False
    classifier_tier: Tier = "luna"
    classifier_timeout: float = Field(default=5, gt=0, le=60)
    classifier_max_chars: int = Field(default=6000, ge=256, le=32000)


class Config(StrictModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    models: dict[str, ModelSpec] = Field(default_factory=default_models)

    @model_validator(mode="after")
    def validate_models(self) -> Config:
        if set(self.models) != set(TIERS):
            raise ValueError("models must define exactly luna, terra, sol, astra")
        if len({model.id for model in self.models.values()}) != len(TIERS):
            raise ValueError("each model tier must have a distinct upstream ID")
        for model in self.models.values():
            if not any(EFFORTS.index(e) <= EFFORTS.index(self.routing.max_effort)
                       for e in model.supported_efforts):
                raise ValueError("max_effort leaves a model with no permitted effort")
        return self

    def credentials(self) -> tuple[str, str]:
        token = os.environ.get(self.server.token_env, "")
        key = os.environ.get(self.upstream.api_key_env, "")
        if len(token) < 24 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError(f"{self.server.token_env} must contain a random ASCII token (24+ characters)")
        if not key or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError(f"{self.upstream.api_key_env} must contain an upstream API credential")
        if token == key:
            raise ValueError("gateway token and upstream API credential must be different")
        return token, key


def load_config(path: str | Path | None = None) -> Config:
    selected = path or os.environ.get("JEV_ROUTER_CONFIG")
    if selected is None:
        return Config()
    with Path(selected).open("rb") as stream:
        return Config.model_validate(tomllib.load(stream))
