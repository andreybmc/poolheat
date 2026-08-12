"""
Catalog of official Whatsminer public API commands (V1 + V2 + V3).

Sources:
  - Whatsminer API V1.3.8 PDF (TCP 4028) — docs/WhatsminerAPIV1.3.8.pdf
  - API User's Manual V2.2.2 (TCP 4028) — docs/API_Manual_V2.2.2.txt
  - Online API V3 docs — https://apidoc.whatsminer.com/ (3.0.x, TCP 4433)

Private WhatsMinerTool protocol (:8889 NetPacket) is separate — see netpacket.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .v1 import V1_ONLY_CMDS, V1_READ_CMDS, V1_WRITE_CMDS
from .v2 import V2_READ_CMDS, V2_WRITE_CMDS
from .v3 import V3_ENCRYPTED_PARAMS, V3_GET_CMDS, V3_SET_CMDS

ApiFamily = Literal["v1", "v2", "v3"]
Access = Literal["read", "write"]


@dataclass(frozen=True)
class ApiCommand:
    family: ApiFamily
    name: str
    access: Access
    port: int
    notes: str = ""
    encrypt_param: bool = False


def _v1_notes(cmd: str) -> str:
    hints = {
        "ssh_open": "V1-only: enable SSH",
        "ssh_close": "V1-only: disable SSH",
        "pre_power_on": "V1-only: preheat before power_on / query status",
        "enable_cgminer_fast_boot": "V1 legacy name (cgminer era)",
        "disable_cgminer_fast_boot": "V1 legacy name",
        "set_power_pct": "range 0–100 (no v2 dual mode)",
        "set_target_freq": "percent range -10..100",
        "update_pwd": "max password length 8 bytes (V1 note)",
        "download_logs": "stream: JSON then binary",
        "update_firmware": "stream: ready then u32le+blob",
        "get_token": "plaintext only; 16 clients × 32 tokens; 30 min TTL",
    }
    return hints.get(cmd, "")


def _v2_notes(cmd: str) -> str:
    hints = {
        "download_logs": "stream: JSON ack then binary log archive",
        "update_firmware": "stream: ready then u32le+blob",
        "get_token": "salt/newsalt for write AES key",
        "set_zone": "timezone + zonename; network restart may be required",
        "set_fast_mining": "alias set_fast_hash on VK/VL/VM",
        "set_fast_hash": "same as set_fast_mining",
        "set_power_pct": "fast mode (~1s)",
        "set_power_pct_v2": "normal mode",
        "set_heat_mode": "anti-icing|heating (hydro)",
        "net_config": "param=dhcp or static fields",
        "set_led": "param=auto or color/period/duration/start",
    }
    return hints.get(cmd, "")


def _v3_notes(cmd: str) -> str:
    hints = {
        "get.device.info": "includes salt for write tokens",
        "get.miner.status": "param filter pools|summary|edevs (+ combinable)",
        "get.log.download": "JSON then .tgz binary stream",
        "set.system.update_firmware": "multi-phase firmware upload",
        "set.miner.pools": "param AES-encrypted",
        "set.user.change_passwd": "param AES-encrypted",
        "set.miner.heat_mode": "heating|normal|anti-freezing",
        "set.miner.power_percent": "param {percent, mode:fast|normal}",
        "set.system.webpools": "enable|disable web pool config",
        "set.miner.fast_hash": "added apidoc 3.0.2",
        "set.device.custom_data": "CustomerSn|msg0..msg9; apidoc 3.0.1+",
        "get.device.custom_data": "apidoc 3.0.1+",
        "set.user.permission": "super only; comma-separated cmd allow-list",
        "set.system.time_randomized": "random start/stop delays",
        "set.system.ntp_server": "comma-separated NTP hosts",
    }
    return hints.get(cmd, "")


def all_commands() -> list[ApiCommand]:
    out: list[ApiCommand] = []
    for c in sorted(V1_READ_CMDS):
        out.append(
            ApiCommand(
                family="v1",
                name=c,
                access="read",
                port=4028,
                notes=_v1_notes(c),
            )
        )
    for c in sorted(V1_WRITE_CMDS):
        note = _v1_notes(c)
        if c in V1_ONLY_CMDS and not note:
            note = "V1-only"
        out.append(
            ApiCommand(
                family="v1",
                name=c,
                access="write",
                port=4028,
                notes=note,
            )
        )
    for c in sorted(V2_READ_CMDS):
        out.append(
            ApiCommand(
                family="v2",
                name=c,
                access="read",
                port=4028,
                notes=_v2_notes(c),
            )
        )
    for c in sorted(V2_WRITE_CMDS):
        out.append(
            ApiCommand(
                family="v2",
                name=c,
                access="write",
                port=4028,
                notes=_v2_notes(c),
            )
        )
    for c in sorted(V3_GET_CMDS):
        out.append(
            ApiCommand(
                family="v3",
                name=c,
                access="read",
                port=4433,
                notes=_v3_notes(c),
            )
        )
    for c in sorted(V3_SET_CMDS):
        out.append(
            ApiCommand(
                family="v3",
                name=c,
                access="write",
                port=4433,
                notes=_v3_notes(c),
                encrypt_param=c in V3_ENCRYPTED_PARAMS,
            )
        )
    return out


def commands_by_family(family: ApiFamily) -> list[ApiCommand]:
    return [c for c in all_commands() if c.family == family]


def summary_table() -> str:
    """Human-readable catalog for CLI / docs."""
    lines = [
        f"{'family':<4} {'access':<5} {'port':<5} {'command':<32} notes",
        "-" * 90,
    ]
    for c in all_commands():
        flag = " [enc]" if c.encrypt_param else ""
        lines.append(
            f"{c.family:<4} {c.access:<5} {c.port:<5} {c.name:<32} {c.notes}{flag}"
        )
    return "\n".join(lines)


# Method name hints for WhatsminerV1 / V2 / V3 helpers
V1_METHOD_MAP: dict[str, str] = {
    "summary": "summary",
    "pools": "pools",
    "edevs": "edevs",
    "devs": "devs",
    "devdetails": "devdetails",
    "get_psu": "get_psu",
    "get_version": "get_version",
    "get_token": "get_token / refresh_token",
    "status": "status",
    "update_pools": "update_pools",
    "restart_btminer": "restart_btminer",
    "power_off": "power_off",
    "power_on": "power_on",
    "set_led": "set_led_auto / set_led_manual",
    "set_low_power": "set_power_mode('low')",
    "set_normal_power": "set_power_mode('normal')",
    "set_high_power": "set_power_mode('high')",
    "update_firmware": "update_firmware",
    "reboot": "reboot",
    "factory_reset": "factory_reset",
    "ssh_open": "ssh_open / set_ssh(True)",
    "ssh_close": "ssh_close / set_ssh(False)",
    "update_pwd": "update_password",
    "net_config": "net_config_dhcp / net_config_static",
    "download_logs": "download_logs",
    "set_target_freq": "set_target_freq",
    "enable_cgminer_fast_boot": "set_fast_boot(True)",
    "disable_cgminer_fast_boot": "set_fast_boot(False)",
    "enable_web_pools": "enable_web_pools",
    "disable_web_pools": "disable_web_pools",
    "set_hostname": "set_hostname",
    "set_zone": "set_zone / set_timezone",
    "load_log": "load_log",
    "set_power_pct": "set_power_pct",
    "pre_power_on": "pre_power_on",
}

V2_METHOD_MAP: dict[str, str] = {
    "summary": "summary",
    "pools": "pools",
    "edevs": "edevs",
    "devs": "devs",
    "devdetails": "devdetails",
    "get_psu": "get_psu",
    "get_version": "get_version",
    "get_token": "get_token / refresh_token",
    "status": "status",
    "get_miner_info": "get_miner_info",
    "get_error_code": "get_error_code",
    "get_customer_msg": "get_customer_msg",
    "update_pools": "update_pools",
    "restart_btminer": "restart_btminer",
    "power_off": "power_off",
    "power_on": "power_on",
    "set_led": "set_led_auto / set_led_manual",
    "set_low_power": "set_power_mode('low')",
    "set_normal_power": "set_power_mode('normal')",
    "set_high_power": "set_power_mode('high')",
    "update_firmware": "update_firmware",
    "reboot": "reboot",
    "factory_reset": "factory_reset",
    "update_pwd": "update_password",
    "net_config": "net_config_dhcp / net_config_static",
    "download_logs": "download_logs",
    "set_target_freq": "set_target_freq",
    "enable_btminer_fast_boot": "set_fast_boot(True)",
    "disable_btminer_fast_boot": "set_fast_boot(False)",
    "enable_web_pools": "enable_web_pools",
    "disable_web_pools": "disable_web_pools",
    "set_hostname": "set_hostname",
    "set_zone": "set_zone / set_timezone",
    "load_log": "load_log",
    "set_power_pct": "set_power_pct(..., fast=True)",
    "set_power_pct_v2": "set_power_pct / set_power_pct_v2",
    "set_power": "set_power",
    "restore_power_pct": "restore_power_pct",
    "set_temp_offset": "set_temp_offset",
    "adjust_power_limit": "set_power_limit",
    "adjust_upfreq_speed": "adjust_upfreq_speed",
    "set_poweroff_cool": "set_poweroff_cool",
    "set_fan_zero_speed": "set_fan_zero_speed",
    "set_heat_mode": "set_heat_mode",
    "disable_btminer_init": "disable_btminer_init",
    "enable_btminer_init": "enable_btminer_init",
    "set_customer_msg": "set_customer_msg",
    "set_fast_mining": "set_fast_mining",
    "set_fast_hash": "set_fast_hash",
}

V3_METHOD_MAP: dict[str, str] = {
    "get.device.info": "device_info",
    "get.device.custom_data": "get_device_custom_data",
    "get.fan.setting": "fan_setting",
    "get.log.download": "download_logs",
    "get.miner.history": "miner_history",
    "get.miner.report": "miner_report",
    "get.miner.setting": "miner_setting",
    "get.miner.status": "miner_status",
    "get.system.setting": "system_setting",
    "set.device.custom_data": "set_device_custom_data",
    "set.fan.poweroff_cool": "set_fan_poweroff_cool",
    "set.fan.temp_offset": "set_fan_temp_offset",
    "set.fan.zero_speed": "set_fan_zero_speed",
    "set.log.upload": "set_log_upload",
    "set.miner.cointype": "set_cointype",
    "set.miner.fast_hash": "set_fast_hash",
    "set.miner.fastboot": "set_fastboot",
    "set.miner.heat_mode": "set_heat_mode",
    "set.miner.pools": "set_pools",
    "set.miner.power": "set_power",
    "set.miner.power_limit": "set_power_limit",
    "set.miner.power_mode": "set_power_mode",
    "set.miner.power_percent": "set_power_percent",
    "set.miner.report": "set_report",
    "set.miner.restore_setting": "restore_setting",
    "set.miner.service": "set_service",
    "set.miner.target_freq": "set_target_freq",
    "set.miner.upfreq_speed": "set_upfreq_speed",
    "set.system.factory_reset": "factory_reset",
    "set.system.hostname": "set_hostname",
    "set.system.led": "set_led_auto / set_led_manual",
    "set.system.net_config": "net_config_dhcp / net_config_static",
    "set.system.ntp_server": "set_ntp_server",
    "set.system.reboot": "reboot",
    "set.system.time_randomized": "set_time_randomized",
    "set.system.timezone": "set_timezone",
    "set.system.update_firmware": "update_firmware",
    "set.system.webpools": "set_webpools",
    "set.user.change_passwd": "change_password",
    "set.user.permission": "set_user_permission",
}
