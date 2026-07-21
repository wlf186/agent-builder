"""Trusted provider/model catalog and immutable qualification contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Iterable


_SAFE_ID = re.compile(r"^[A-Za-z0-9._:/+-]{1,128}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_CAPABILITIES = frozenset({"completion", "tools", "streaming"})
MAX_CATALOG_MODELS = 16


class ModelCatalogError(ValueError):
    """A trusted catalog or selection violated its closed schema."""


@dataclass(frozen=True, slots=True)
class ModelTimeoutProfile:
    """Bounded provider timing policy selected with the trusted model."""

    queue_seconds: float = 30.0
    first_frame_seconds: float = 60.0
    stream_idle_seconds: float = 20.0
    turn_seconds: float = 120.0
    first_frame_attempts: int = 2

    def __post_init__(self) -> None:
        numeric = (
            self.queue_seconds,
            self.first_frame_seconds,
            self.stream_idle_seconds,
            self.turn_seconds,
        )
        if (
            any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0.01 <= value <= 300.0
                for value in numeric
            )
            or self.queue_seconds > 120.0
            or self.first_frame_seconds > 120.0
            or self.stream_idle_seconds > 120.0
            or self.turn_seconds > 180.0
            or self.first_frame_seconds >= self.turn_seconds
            or self.stream_idle_seconds >= self.turn_seconds
            or self.first_frame_attempts not in {1, 2}
        ):
            raise ModelCatalogError("invalid trusted model timeout profile")

    def canonical_manifest(self) -> dict[str, object]:
        return {
            "first_frame_attempts": self.first_frame_attempts,
            "first_frame_seconds": self.first_frame_seconds,
            "queue_seconds": self.queue_seconds,
            "stream_idle_seconds": self.stream_idle_seconds,
            "turn_seconds": self.turn_seconds,
        }


def _digest(domain: bytes, value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    endpoint_id: str
    provider: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if (
            _SAFE_ID.fullmatch(self.endpoint_id) is None
            or _SAFE_ID.fullmatch(self.provider) is None
            or _HOST.fullmatch(self.host) is None
            or not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65_535
        ):
            raise ModelCatalogError("invalid trusted provider endpoint")

    def canonical_manifest(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint_id,
            "host": self.host,
            "port": self.port,
            "provider": self.provider,
        }


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    model_id: str
    provider: str
    provider_model: str
    endpoint_id: str
    operational_context_cap: int
    output_token_cap: int
    required_capabilities: tuple[str, ...] = ("completion", "streaming", "tools")
    temperature: int = 0
    seed: int = 0
    keep_alive: str = "5m"
    timeouts: ModelTimeoutProfile = field(default_factory=ModelTimeoutProfile)

    def __post_init__(self) -> None:
        capabilities = tuple(sorted(set(self.required_capabilities)))
        if (
            _SAFE_ID.fullmatch(self.model_id) is None
            or _SAFE_ID.fullmatch(self.provider) is None
            or _SAFE_ID.fullmatch(self.provider_model) is None
            or _SAFE_ID.fullmatch(self.endpoint_id) is None
            or capabilities != self.required_capabilities
            or not capabilities
            or not set(capabilities).issubset(_CAPABILITIES)
            or "completion" not in capabilities
            or "streaming" not in capabilities
            or not isinstance(self.operational_context_cap, int)
            or isinstance(self.operational_context_cap, bool)
            or not 2_048 <= self.operational_context_cap <= 131_072
            or not isinstance(self.output_token_cap, int)
            or isinstance(self.output_token_cap, bool)
            or not 1 <= self.output_token_cap < self.operational_context_cap
            or self.temperature != 0
            or self.seed != 0
            or self.keep_alive != "5m"
            or not isinstance(self.timeouts, ModelTimeoutProfile)
        ):
            raise ModelCatalogError("invalid trusted model catalog entry")

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.required_capabilities

    @property
    def generation_options_digest(self) -> str:
        return _digest(
            b"agent-builder-generation-options-v1\0",
            {
                "keep_alive": self.keep_alive,
                "seed": self.seed,
                "temperature": self.temperature,
            },
        )

    def canonical_manifest(self) -> dict[str, object]:
        return {
            "endpoint_id": self.endpoint_id,
            "generation_options_digest": self.generation_options_digest,
            "model_id": self.model_id,
            "operational_context_cap": self.operational_context_cap,
            "output_token_cap": self.output_token_cap,
            "provider": self.provider,
            "provider_model": self.provider_model,
            "required_capabilities": list(self.required_capabilities),
            "timeouts": self.timeouts.canonical_manifest(),
        }


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    endpoints: tuple[ProviderEndpoint, ...]
    models: tuple[ModelCatalogEntry, ...]
    default_model_id: str
    revision: str = "operator-catalog-v1"

    @classmethod
    def create(
        cls,
        *,
        endpoints: Iterable[ProviderEndpoint],
        models: Iterable[ModelCatalogEntry],
        default_model_id: str,
        revision: str = "operator-catalog-v1",
    ) -> ModelCatalog:
        return cls(
            tuple(sorted(endpoints, key=lambda item: item.endpoint_id)),
            tuple(sorted(models, key=lambda item: item.model_id)),
            default_model_id,
            revision,
        )

    def __post_init__(self) -> None:
        endpoint_ids = tuple(item.endpoint_id for item in self.endpoints)
        model_ids = tuple(item.model_id for item in self.models)
        endpoints = {item.endpoint_id: item for item in self.endpoints}
        if (
            not self.endpoints
            or not self.models
            or len(self.models) > MAX_CATALOG_MODELS
            or endpoint_ids != tuple(sorted(set(endpoint_ids)))
            or model_ids != tuple(sorted(set(model_ids)))
            or self.default_model_id not in model_ids
            or _SAFE_ID.fullmatch(self.revision) is None
        ):
            raise ModelCatalogError("invalid trusted model catalog")
        for model in self.models:
            endpoint = endpoints.get(model.endpoint_id)
            if endpoint is None or endpoint.provider != model.provider:
                raise ModelCatalogError("model references an invalid trusted endpoint")
        # The first implementation deliberately permits only one qualified
        # endpoint per broker.  Multiple models may share it; adding another
        # network target requires a separate pinned client and qualification.
        if len(self.endpoints) != 1 or self.endpoints[0].provider != "ollama":
            raise ModelCatalogError("unsupported trusted provider topology")

    @property
    def digest(self) -> str:
        return _digest(b"agent-builder-model-catalog-v1\0", self.canonical_manifest())

    def canonical_manifest(self) -> dict[str, object]:
        return {
            "default_model_id": self.default_model_id,
            "endpoints": [item.canonical_manifest() for item in self.endpoints],
            "models": [item.canonical_manifest() for item in self.models],
            "revision": self.revision,
        }

    def select(self, model_id: str | None = None) -> ModelCatalogEntry:
        selected = self.default_model_id if model_id is None else model_id
        if not isinstance(selected, str) or _SAFE_ID.fullmatch(selected) is None:
            raise ModelCatalogError("invalid model selection")
        for model in self.models:
            if model.model_id == selected:
                return model
        raise ModelCatalogError("model is not in the trusted catalog")

    def endpoint_for(self, entry: ModelCatalogEntry) -> ProviderEndpoint:
        selected = self.select(entry.model_id)
        for endpoint in self.endpoints:
            if endpoint.endpoint_id == selected.endpoint_id:
                return endpoint
        raise ModelCatalogError("model endpoint is unavailable")

    def public_metadata(self) -> dict[str, object]:
        return {
            "catalog_digest": self.digest,
            "default_model_id": self.default_model_id,
            "models": [
                {
                    "model_id": item.model_id,
                    "provider": item.provider,
                    "provider_model": item.provider_model,
                    "supports_streaming": True,
                    "supports_tools": item.supports_tools,
                }
                for item in self.models
            ],
            "revision": self.revision,
        }


def default_model_catalog() -> ModelCatalog:
    return ModelCatalog.create(
        endpoints=(ProviderEndpoint("local-ollama", "ollama", "iollama", 11_434),),
        models=(
            ModelCatalogEntry(
                model_id="qwen3.5:2b",
                provider="ollama",
                provider_model="qwen3.5:2b",
                endpoint_id="local-ollama",
                operational_context_cap=32_768,
                output_token_cap=4_096,
            ),
        ),
        default_model_id="qwen3.5:2b",
    )


__all__ = [
    "MAX_CATALOG_MODELS",
    "ModelCatalog",
    "ModelCatalogEntry",
    "ModelCatalogError",
    "ModelTimeoutProfile",
    "ProviderEndpoint",
    "default_model_catalog",
]
