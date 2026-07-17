"""Optional, configuration-driven LLM cost estimation.

Prices are deliberately not hard-coded because provider prices change often.
Set ``OBSERVABILITY_PRICING_JSON`` to a mapping such as::

    {"model-name": {"input_per_million": 1.0, "output_per_million": 2.0}}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float


class PricingCatalog:
    def __init__(self, prices: Optional[Mapping[str, ModelPrice]] = None):
        self._prices = {key.lower(): value for key, value in (prices or {}).items()}

    @classmethod
    def from_env(cls) -> "PricingCatalog":
        raw = os.environ.get("OBSERVABILITY_PRICING_JSON", "").strip()
        if not raw:
            return cls()
        try:
            parsed = json.loads(raw)
            prices = {
                str(model): ModelPrice(
                    input_per_million=float(values["input_per_million"]),
                    output_per_million=float(values["output_per_million"]),
                )
                for model, values in parsed.items()
                if isinstance(values, dict)
            }
            return cls(prices)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("OBSERVABILITY_PRICING_JSON is invalid") from exc

    def estimate(
        self,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
    ) -> Optional[Dict[str, float]]:
        if not model:
            return None
        price = self._prices.get(model.lower())
        if price is None:
            return None
        input_cost = max(input_tokens, 0) * price.input_per_million / 1_000_000
        output_cost = max(output_tokens, 0) * price.output_per_million / 1_000_000
        return {
            "input": input_cost,
            "output": output_cost,
            "total": input_cost + output_cost,
        }
