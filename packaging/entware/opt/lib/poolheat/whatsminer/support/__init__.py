"""Helpers: power models, miner error i18n."""

from __future__ import annotations

from .error_codes import (
    available_languages,
    describe_error,
    enrich_error_codes,
    list_codes,
    resolve_error,
    resolve_errors,
)
from .power_model import (
    DEFAULT_M60_J_PER_TH,
    compare_reported_vs_model_power,
    estimate_dual_wall_power,
    estimate_history_wall_power,
    estimate_power_from_hashrate,
    parse_agp_preset_label,
    resolve_model_efficiency,
)

__all__ = [
    "describe_error",
    "resolve_error",
    "resolve_errors",
    "enrich_error_codes",
    "available_languages",
    "list_codes",
    "estimate_power_from_hashrate",
    "estimate_dual_wall_power",
    "compare_reported_vs_model_power",
    "estimate_history_wall_power",
    "parse_agp_preset_label",
    "resolve_model_efficiency",
    "DEFAULT_M60_J_PER_TH",
]
