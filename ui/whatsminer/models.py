from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PoolInfo:
    index: int
    url: str = ""
    user: str = ""
    password: str | None = None  # only when include_passwords via LuCI
    status: str = ""
    accepted: int = 0
    rejected: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class BoardInfo:
    slot: int
    temperature: float | None = None
    chip_frequency: float | None = None
    hashrate_mhs: float | None = None
    factory_ghs: float | None = None
    pcb_sn: str = ""
    effective_chips: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ErrorCode:
    code: str
    timestamp: str = ""
    description: str = ""
    cause: str = ""  # i18n cause text (same as description when filled)
    lang: str = ""

    def with_i18n(self, lang: str = "en") -> "ErrorCode":
        """Fill description/cause from WhatsMinerTool i18n tables."""
        from .support.error_codes import describe_error

        msg = describe_error(self.code, lang=lang)
        self.description = msg
        self.cause = msg
        self.lang = lang
        return self


@dataclass
class MinerStatus:
    ip: str
    api: str  # "v1" | "v2" | "v3" | "netpacket"
    online: bool = True
    miner_type: str = ""
    firmware: str = ""
    platform: str = ""
    power_mode: str = ""
    power_w: float | None = None
    power_limit: float | None = None
    hashrate_ths: float | None = None  # TH/s
    hashrate_rt_ths: float | None = None
    temp_chip_avg: float | None = None
    temp_chip_max: float | None = None
    fan_in: int | None = None
    fan_out: int | None = None
    uptime_s: int | None = None
    elapsed_s: float | None = None
    mac: str = ""
    hostname: str = ""
    pools: list[PoolInfo] = field(default_factory=list)
    boards: list[BoardInfo] = field(default_factory=list)
    errors: list[ErrorCode] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @staticmethod
    def mhs_to_ths(mhs: float | None) -> float | None:
        if mhs is None:
            return None
        return mhs / 1_000_000.0
