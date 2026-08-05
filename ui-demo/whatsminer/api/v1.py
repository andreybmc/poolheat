"""
Whatsminer API V1.3.8 (TCP 4028) — early public JSON API.

Wire protocol is the same generation as V2 (port 4028, AES-256-ECB write +
md5_crypt token). Command surface is smaller and uses some legacy names
(cgminer fast boot, ssh_open/close, pre_power_on).

Reference: docs/WhatsminerAPIV1.3.8.pdf → docs/API_Manual_V1.3.8.txt

For modern firmwares prefer :class:`whatsminer.api.v2.WhatsminerV2` (manual 2.2.2)
or :class:`whatsminer.api.v3.WhatsminerV3` (port 4433).
"""

from __future__ import annotations

from typing import Any

from .v2 import WhatsminerV2

DEFAULT_PORT = 4028

# ── command catalog (Whatsminer API V1.3.8) ──────────────────────────────────

V1_READ_CMDS: frozenset[str] = frozenset(
    {
        "summary",
        "pools",
        "edevs",
        "devs",
        "devdetails",
        "get_psu",
        "get_version",
        "get_token",
        "status",
    }
)

V1_WRITE_CMDS: frozenset[str] = frozenset(
    {
        "update_pools",
        "restart_btminer",
        "power_off",
        "power_on",
        "set_led",
        "set_low_power",
        "set_normal_power",  # implied by mode switch section (doc shows set_low_power)
        "set_high_power",
        "update_firmware",
        "reboot",
        "factory_reset",
        "ssh_open",
        "ssh_close",
        "update_pwd",
        "net_config",
        "download_logs",
        "set_target_freq",
        "enable_cgminer_fast_boot",
        "disable_cgminer_fast_boot",
        "enable_web_pools",
        "disable_web_pools",
        "set_hostname",
        "set_zone",
        "load_log",
        "set_power_pct",
        "pre_power_on",
    }
)

# V1-only (not in V2.2.2 manual)
V1_ONLY_CMDS: frozenset[str] = frozenset(
    {
        "ssh_open",
        "ssh_close",
        "pre_power_on",
        "enable_cgminer_fast_boot",
        "disable_cgminer_fast_boot",
    }
)


class WhatsminerV1(WhatsminerV2):
    """
    Client scoped to API V1.3.8 commands.

    Inherits transport/token/AES from :class:`WhatsminerV2` (identical wire format
    on TCP 4028). Extra helpers cover V1-only ops; ``set_fast_boot`` uses the
    legacy ``enable_cgminer_fast_boot`` / ``disable_cgminer_fast_boot`` names.
    """

    API_VERSION = "1.3.8"

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        password: str = "admin",
        *,
        timeout: float = 10.0,
    ):
        super().__init__(host, port=port, password=password, timeout=timeout)

    # ── V1-only writes ───────────────────────────────────────────────────────

    def ssh_open(self) -> dict[str, Any]:
        """Enable SSH service on the miner (V1.3.8 §2.10)."""
        return self.write("ssh_open")

    def ssh_close(self) -> dict[str, Any]:
        """Disable SSH service on the miner (V1.3.8 §2.10)."""
        return self.write("ssh_close")

    def set_ssh(self, enable: bool) -> dict[str, Any]:
        return self.ssh_open() if enable else self.ssh_close()

    def pre_power_on(
        self,
        complete: bool | str | None = None,
        msg: str | None = None,
    ) -> dict[str, Any]:
        """
        Preheat / query pre-power-on state before power_on (V1.3.8 §2.23).

        - complete: true/false (string) — start or query completion
        - msg: "wait for adjust temp" | "adjust complete" | "adjust continue"

        Call only after power_off. Speeds reaching full power after power_on.
        """
        params: dict[str, Any] = {}
        if complete is not None:
            if isinstance(complete, bool):
                params["complete"] = "true" if complete else "false"
            else:
                params["complete"] = str(complete)
        if msg is not None:
            params["msg"] = msg
        return self.write("pre_power_on", **params)

    def enable_cgminer_fast_boot(self) -> dict[str, Any]:
        """Legacy name (cgminer era); V1.3.8 §2.15."""
        return self.write("enable_cgminer_fast_boot")

    def disable_cgminer_fast_boot(self) -> dict[str, Any]:
        """V1.3.8 §2.16."""
        return self.write("disable_cgminer_fast_boot")

    def set_fast_boot(self, enable: bool) -> dict[str, Any]:
        """Override V2 helper: use cgminer_* command names from V1 manual."""
        return (
            self.enable_cgminer_fast_boot()
            if enable
            else self.disable_cgminer_fast_boot()
        )

    def set_power_pct(self, percent: int | str, *, fast: bool = True) -> dict[str, Any]:
        """
        V1 only documents ``set_power_pct`` (range 0–100).

        ``fast`` is accepted for API compatibility with V2 but is ignored —
        there is no set_power_pct_v2 in V1.3.8.
        """
        _ = fast
        return self.write("set_power_pct", percent=str(percent))

    # V2-only helpers that do not exist on V1: keep inherited methods available
    # for experimentation on mixed firmwares, but catalog marks them as V2.

    @classmethod
    def list_commands(cls) -> dict[str, list[str]]:
        return {
            "read": sorted(V1_READ_CMDS),
            "write": sorted(V1_WRITE_CMDS),
            "v1_only": sorted(V1_ONLY_CMDS),
        }
