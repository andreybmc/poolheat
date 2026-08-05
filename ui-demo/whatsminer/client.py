"""
Unified miner client.

:class:`MinerClient` is an alias of :class:`whatsminer.universal.UniversalMiner`
(auto-detect public API V1/V2/V3, fall back to private NetPacket :8889 / LuCI).

Prefer importing :class:`UniversalMiner` for new code.
"""

from __future__ import annotations

from .universal import (
    Capabilities,
    PublicApi,
    Transport,
    UniversalMiner,
    detect_api,
    probe_capabilities,
)

# Back-compat name used throughout the codebase / examples
MinerClient = UniversalMiner

# historical type alias
ApiKind = PublicApi

__all__ = [
    "MinerClient",
    "UniversalMiner",
    "Capabilities",
    "PublicApi",
    "ApiKind",
    "Transport",
    "detect_api",
    "probe_capabilities",
]
