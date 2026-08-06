r"""
WhatsMinerTool private NetPacket on TCP **8889**.

Recovered from WMT 9.2.4 static RE + live PCAP/M63 (FW 20250915).

## Wire format (LE)

```
u32 magic = 0x7F7F5A5A
u32 cmd
u16 len_text
u16 len_bin
u32 crc     = crc32_raw(text || bin)   # zlib.crc32 ^ 0xFFFFFFFF
u8  text[len_text]
u8  bin[len_bin]
pad to 16 with NUL
if size > 16: AES-256-ECB(KEY) over entire frame
```

## Keys (static in WMT / AirToLiquid .data)

| Key   | Source / VA              | Use                          |
|-------|--------------------------|------------------------------|
| KEY_A | ATL before KEY0; WMT    | **not** NetPacket wire yet (PSU/upgrade/FTP candidate) |
| KEY_B | ATL before KEY0; WMT    | same                         |
| KEY0  | WMT 0x7204C0             | cmd 0 (handshake)            |
| KEY1  | WMT 0x7204E0             | cmd >= 2 (auth + control)    |
| KEY2  | WMT 0x720500             | reserved (obj+0x20)          |

AirToLiquid table layout: ``KEY_A ‖ KEY_B ‖ KEY0 ‖ KEY1 ‖ KEY2`` (@ file ``0x78bc20`` region).

## Auth text

```
{miner_ip}|{unix_ts}|{account}|{password}           # cmd 0
{miner_ip}|{unix_ts}|{account}|{password}|{token}   # other cmds
```

Default WMT Remote account: ``super`` / ``super``.

## Commands (cmd field)

| cmd | Name           | Binary                         | Source   |
|----:|----------------|--------------------------------|----------|
|   0 | HANDSHAKE      | empty -> 24 B challenge/token  | PCAP+live|
|   2 | SET_POOLS      | ``i,url,user,FAILOVER,,pw|``… (all pools in one frame) | WMT lab Peak |
|   4 | SET_PASSWORD   | ``lenA,lenO,lenN,{account}{old}{new}`` | lab Peak password UI |
|   5 | SET_PERFORMANCE| WMT SET wire ``1``/``0``/``2`` = Low/Normal/High (≠ MinerInfo) | lab Peak |
|   6 | SET_COIN       | ``HC`` / coin code             | PCAP     |
|   7 | UPDATE_FIRMWARE| empty auth, then raw stream    | lab Peak WMT firmware upgrade |
|   8 | REBOOT         | empty (+auth text)             | lab Peak |
|  10 | FACTORY_RESET  | empty                          | PCAP + lab Peak Factory Settings |
|  11 | RESTORE_DHCP   | empty (+auth text)             | lab Peak Restore DHCP |
|  12 | SET_WEB_POOLS  | ``1`` enable / ``0`` disable   | lab Peak Web Pools Switch |
|  13 | SET_PARAM      | ``{id}={value}``               | PCAP+EXE |
|  14 | SET_PERMISSIONS| ``user1=2,user2=1,user3=0``   | lab Peak Permissions |
|  15 | GET_HASHRATE   | empty -> ``a:b:c:d`` detect GH | live     |
|  20 | EXPORT_LOG     | empty -> u32le size + gzip     | lab Peak Export Log |
|  21 | FIRMWARE_STATUS| empty -> ACK or ``upgrade=…``  | lab Peak after FW upload |
|  22 | GET_INFO       | empty -> MinerInfo push        | PCAP+live|
|  25 | RESTORE_SETTINGS | empty (+auth text)           | lab Peak Restore miner settings |

Firmware stream (same TCP after cmd **7** NetPacket)::

    u32le size  +  size bytes (encrypted/signed container, ~12 MB lab)
    response plain cmd=7 (lt=0 ok, lt=1 error/incomplete)
    then cmd **21** poll until binary ``upgrade=success\\n``

## SET_PARAM ids (cmd 13)

| id | Name            | Value examples              | Conf   |
|---:|-----------------|-----------------------------|--------|
|  1 | LED             | ``auto`` / ``red 200 100 0|green ...`` | lab Peak + EXE |
|  4 | HASH_PERCENT    | ``10`` / ``-10`` / ``0`` (Up/Down/Norm) | lab Peak |
|  6 | API_SWITCH     | 1=enable, 0=disable (WMT Miner API Switch); may status=9 | lab Peak |
|  7 | FAST_BOOT      | 1=enable, 0=disable (WMT Power Fast Boot) | lab Peak 2026-08-05 |
|  8 | MINING         | 1=resume, 0=suspend (WMT UI) | lab Peak 2026-08-05 |
|  9 | POWER_PCT_FAST | e.g. ``90`` (WMT Adjust Power **Fast Mode** %) | lab Peak |
| 13 | HEAT_MODE       | ``anti-icing`` / ``heating`` (UI Anti-Freezing / Power-Keeping) | lab Peak |
| 14 | POWER_LIMIT     | watts ``2500``              | PCAP   |
| 15 | UPFREQ_SPEED    | ``0``..``10`` (WMT Adjust upfreq speed) | lab Peak |
| 18 | NTP_SERVERS     | ``host1,host2,host3,host4`` (WMT Set NTP Server; empty slots ok) | lab Peak |
| 19 | TIMEZONE        | ``Asia/Novosibirsk,<+07>-7`` (zonename,POSIX TZ) | lab Peak |
| 20 | POWER_PCT       | e.g. ``96`` (WMT Adjust Power %, Normal Mode) | lab Peak |
| 21 | SYNC_TIME       | ``YYYY-MM-DD HH:MM,{unix_ts}`` (WMT Time Manage → Sync Time) | lab Peak |
| 22 | POWER_PCT_RESET | ``100`` (WMT Adjust Power → Reset settings) | lab Peak |
| 23 | FAST_MINING     | 1=on, 0=off (WMT Fast Hash) | lab Peak |
| 24 | (unknown int)   | int                         | EXE    |
| 25 | (unknown int)   | int                         | EXE    |

Use :meth:`NetPacketClient.set_param` / :meth:`send_command` for exploration.
"""

from __future__ import annotations

import re
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Optional, Union

from Crypto.Cipher import AES

from ..errors import AuthError, CommandError, ProtocolError

MAGIC = 0x7F7F5A5A
HEADER_SIZE = 16
DEFAULT_PORT = 8889
MAX_PACKET = 0x10000

# Extra 32-byte keys found immediately before KEY0 in AirToLiquid-1.5.5
# (also in WMT @ ~0x31dfd0, not always adjacent). Not used by NetPacket
# KEY0/KEY1 path; purpose TBD — PSU FW stream / gRPC payload / legacy.
# See projects/whatsminer/docs/AIR_TO_LIQUID.md §1.2
KEY_A = bytes.fromhex(
    "1fdda8338807c731b11210592780ec5f60517fa919b54a0d2de57a9f93c99cef"
)
KEY_B = bytes.fromhex(
    "a0e03b4dae2af5b0c8ebbb3c83539961172b047eba77d626e169146355210c7d"
)
# Aliases (same bytes)
KEYA = KEY_A
KEYB = KEY_B

KEY0 = bytes.fromhex(
    "f0d379ee4188bc6216cfa09adcd49100ee7f971217aaba26bc86c0b6ae1da90f"
)
KEY1 = bytes.fromhex(
    "66476cc48201182b9c27c302e48e120724a0e460fb970474a7539a48e787c296"
)
KEY2 = bytes.fromhex(
    "9be70afc109f7756383155083c120910ddfff76720a34786fa272611ead19bf1"
)

# Full static table as dumped from ATL (order on disk)
STATIC_AES_KEYS: dict[str, bytes] = {
    "KEY_A": KEY_A,
    "KEY_B": KEY_B,
    "KEY0": KEY0,
    "KEY1": KEY1,
    "KEY2": KEY2,
}


def key_for_cmd(cmd: int) -> bytes:
    """NetPacket wire key: handshake uses KEY0, everything else KEY1."""
    return KEY0 if cmd == 0 else KEY1


def extract_firmware_image(
    data: bytes,
    *,
    platform: str = "h616",
) -> tuple[bytes, dict[str, Any]]:
    """
    Load a wire image for :meth:`NetPacketClient.update_firmware`.

    Accepts either:

    - a raw WMT container (as sent after cmd 7's ``u32le`` size field), or
    - a multi-platform ``Whatsminer-all-*.bin`` package (lab: H616 slice at
      offset 6812, size 12 627 688 — same blob WMT uploaded successfully).

    Returns ``(image_bytes, meta)``.
    """
    meta: dict[str, Any] = {"source": "raw", "size": len(data)}
    if not data:
        raise ValueError("empty firmware file")

    # Already a single container: first dword is not a package magic we know,
    # and no multi-platform directory — send whole file.
    plat = platform.lower().encode("ascii")
    # Package magic observed on Whatsminer-all-20260416… (Peak lab)
    is_all_pkg = data[:4] == bytes.fromhex("3f0a64ae") or (
        plat in data[:0x200] and len(data) > 20_000_000
    )
    if not is_all_pkg:
        meta["source"] = "raw_container"
        return data, meta

    # Directory-style entries: platform name (16 B) then u32le offset, u32le size.
    # Scan first 8 KiB for ``h616`` / requested platform.
    candidates: list[tuple[int, int, int]] = []  # (dir_off, file_off, size)
    hay = data[:8192]
    start = 0
    while True:
        i = hay.find(plat, start)
        if i < 0:
            break
        # name field often 16-byte padded; offset/size within next 32 bytes
        for rel in (16 - (i % 16), 16, 0, 8, 24, 32):
            if rel is None:
                continue
            o = i + (rel if rel >= 0 else 0)
            # Prefer aligned after 16-byte name starting at i//16*16
            name_base = i
            # try fixed: name at i (or i&~15), then +16 = offset, +20 = size
            for name_off in {i, i & ~15, max(0, i - (i % 16))}:
                off_ptr = name_off + 16
                if off_ptr + 8 > len(data):
                    continue
                foff, fsz = struct.unpack_from("<II", data, off_ptr)
                if foff >= 64 and fsz >= 100_000 and foff + fsz <= len(data):
                    candidates.append((name_off, foff, fsz))
        start = i + 1

    # Dedup by (off,size)
    uniq: dict[tuple[int, int], int] = {}
    for name_off, foff, fsz in candidates:
        uniq[(foff, fsz)] = name_off

    if not uniq:
        # Fallback: known lab layout for this package family
        if len(data) > 6812 + 12_627_688:
            foff, fsz = 6812, 12_627_688
            if data[foff : foff + 4] == bytes.fromhex("7ca55b9a"):
                img = data[foff : foff + fsz]
                meta.update(
                    {
                        "source": "all_package_fallback",
                        "platform": platform,
                        "offset": foff,
                        "size": fsz,
                    }
                )
                return img, meta
        raise ValueError(
            f"could not find platform={platform!r} image in Whatsminer-all package "
            f"({len(data)} bytes)"
        )

    # Prefer the entry whose size matches the successful lab transfer, else largest
    preferred = None
    for (foff, fsz), name_off in uniq.items():
        if fsz == 12_627_688:
            preferred = (foff, fsz, name_off)
            break
    if preferred is None:
        (foff, fsz), name_off = max(uniq.items(), key=lambda kv: kv[0][1])
        preferred = (foff, fsz, name_off)
    foff, fsz, name_off = preferred
    img = data[foff : foff + fsz]
    meta.update(
        {
            "source": "all_package",
            "platform": platform,
            "offset": foff,
            "size": fsz,
            "dir_off": name_off,
            "candidates": [
                {"offset": o, "size": s} for (o, s) in sorted(uniq.keys())
            ],
        }
    )
    return img, meta


class Cmd(IntEnum):
    """NetPacket command codes (wire ``cmd`` field)."""

    HANDSHAKE = 0
    SET_POOLS = 2
    # 3 — not observed
    SET_PASSWORD = 4  # lenA,lenO,lenN,account+old+new (WMT Update password)
    SET_PERFORMANCE = 5  # WMT Performance Mode (SET wire ≠ MinerInfo for Low/Normal)
    SET_COIN = 6
    UPDATE_FIRMWARE = 7  # empty auth NetPacket, then u32le+blob on same TCP
    REBOOT = 8
    # 9 — not observed as top-level (9 is Param.POWER_PCT_FAST under SET_PARAM)
    FACTORY_RESET = 10
    RESTORE_DHCP = 11  # empty — WMT Restore miner → Restore DHCP
    SET_WEB_POOLS = 12  # "1" enable / "0" disable (WMT Web Pools Switch)
    SET_PARAM = 13
    SET_PERMISSIONS = 14  # user1=L,user2=L,user3=L (WMT Permissions Configuration)
    GET_HASHRATE = 15  # empty -> "gh0:gh1:gh2:gh3" (DetectedHashRate)
    EXPORT_LOG = 20  # empty -> u32le + gzip (WMT Export Log)
    FIRMWARE_STATUS = 21  # empty; final reply binary upgrade=success\\n
    GET_INFO = 22
    # 23–24 — not observed as top-level cmds (23 is Param.FAST_MINING under SET_PARAM)
    RESTORE_SETTINGS = 25  # empty — WMT Restore miner → Restore miner settings


class PermissionLevel(IntEnum):
    """WMT Permissions Configuration levels (lab Peak 2026-08-05)."""

    LEVEL_0 = 0
    LEVEL_1 = 1
    LEVEL_2 = 2


class Param(IntEnum):
    """Parameter ids for cmd 13 binary ``{id}={value}``."""

    LED = 1
    # WMT Adjust freq Up/Down N% (lab Peak): 4=10 / 4=-10 → HashPercent
    HASH_PERCENT = 4
    UNKNOWN_4 = 4  # alias of HASH_PERCENT
    # WMT Miner API Switch (lab Peak 2026-08-05): UI Enable → 6=1
    # NOT Performance Mode (that is cmd 5). ASIC may reply status 9
    # (Need change pwd) and leave MinerApiSwitch=0 until password policy ok.
    API_SWITCH = 6
    PARAM_6 = 6  # alias of API_SWITCH
    # WMT Power Fast Boot (lab Peak 2026-08-05): 7=1 enable → BtminerFastBoot=1
    FAST_BOOT = 7
    # WMT Mining Control (lab Peak/M63 2026-08-05, UI success frames):
    #   8=1 → Resume Mining,  8=0 → Suspend Mining
    # Historical name MINER_OFF is misleading (values are NOT 1=off).
    MINING = 8
    MINER_OFF = 8  # alias of MINING; do not assume 1 means "off"
    # WMT Adjust Power Fast Mode (lab Peak): UI 90 → 9=90
    POWER_PCT_FAST = 9
    HEAT_MODE = 13
    POWER_LIMIT = 14
    # WMT Adjust upfreq speed (lab Peak): 15=10 → UpfreqSpeed=10
    UPFREQ_SPEED = 15
    UNKNOWN_15 = 15  # alias of UPFREQ_SPEED
    # WMT Set NTP Server (lab Peak): 18=0.cn.pool.ntp.org,0.openwrt.pool.ntp.org,,
    NTP_SERVERS = 18
    UNKNOWN_18 = 18  # alias of NTP_SERVERS
    # WMT Set Time Zone (lab Peak): 19=Asia/Novosibirsk,<+07>-7
    TIMEZONE = 19
    UNKNOWN_19 = 19  # alias of TIMEZONE
    # WMT Adjust Power Normal Mode (lab Peak): UI 96 → 20=96 (not HashPercent/param4)
    POWER_PCT = 20
    # WMT Time Manage → Sync Time (lab Peak): 21=2026-08-06 00:10,1785949818
    # (not to be confused with top-level Cmd.FIRMWARE_STATUS = 21)
    SYNC_TIME = 21
    # WMT Adjust Power → Reset settings (lab Peak): 22=100
    POWER_PCT_RESET = 22
    # WMT Fast Hash On/Off (lab Peak): 23=1 → BtminerFastMining=1
    FAST_MINING = 23
    UNKNOWN_24 = 24
    UNKNOWN_25 = 25


class PerformanceMode(IntEnum):
    """
    Semantic **Performance Mode** (matches MinerInfo ``PowerMode`` / SUMMARY).

    - ``LOW=0``, ``NORMAL=1``, ``HIGH=2``  ← as reported by ASIC

    **WMT cmd 5 SET wire is different for Low/Normal** (lab Peak 2026-08-05):

    | UI / semantic | MinerInfo PowerMode | cmd5 binary |
    |---------------|---------------------|-------------|
    | Low           | 0                   | ``"1"``     |
    | Normal        | 1                   | ``"0"``     |
    | High          | 2                   | ``"2"``     |

    Use :func:`performance_mode_to_wire` / :meth:`NetPacketClient.set_performance_mode`
    — do not send ``str(int(mode))`` for Low/Normal.
    """

    LOW = 0
    NORMAL = 1
    HIGH = 2


# Back-compat alias (do not confuse with cmd13 param 6)
PowerMode = PerformanceMode

# cmd5 SET binary (WMT UI success frames, Peak/M63 lab)
_PERFORMANCE_MODE_TO_WIRE: dict[int, str] = {
    int(PerformanceMode.LOW): "1",
    int(PerformanceMode.NORMAL): "0",
    int(PerformanceMode.HIGH): "2",
}
_PERFORMANCE_WIRE_TO_MODE: dict[str, int] = {
    "1": int(PerformanceMode.LOW),
    "0": int(PerformanceMode.NORMAL),
    "2": int(PerformanceMode.HIGH),
}


def performance_mode_to_wire(mode: Union[int, PerformanceMode, str]) -> str:
    """Map semantic Low/Normal/High → cmd 5 binary string for WMT."""
    if isinstance(mode, str):
        key = mode.strip().lower()
        aliases = {
            "low": PerformanceMode.LOW,
            "normal": PerformanceMode.NORMAL,
            "high": PerformanceMode.HIGH,
            "0": PerformanceMode.LOW,
            "1": PerformanceMode.NORMAL,
            "2": PerformanceMode.HIGH,
        }
        if key not in aliases:
            raise ValueError(f"unknown performance mode {mode!r}")
        mode = aliases[key]
    m = int(mode)
    if m not in _PERFORMANCE_MODE_TO_WIRE:
        raise ValueError("performance mode must be 0=Low, 1=Normal, 2=High (semantic)")
    return _PERFORMANCE_MODE_TO_WIRE[m]


def performance_wire_to_mode(wire: Union[str, bytes, int]) -> PerformanceMode:
    """Map cmd 5 binary → semantic PerformanceMode."""
    s = wire.decode("ascii") if isinstance(wire, (bytes, bytearray)) else str(wire).strip()
    if s not in _PERFORMANCE_WIRE_TO_MODE:
        raise ValueError(f"unknown cmd5 performance wire {s!r}")
    return PerformanceMode(_PERFORMANCE_WIRE_TO_MODE[s])


# Human-readable param registry for docs / CLI
PARAM_INFO: dict[int, dict[str, str]] = {
    int(Param.LED): {
        "name": "LED",
        "type": "str",
        "example": "auto | red 200 100 0|green 200 100 0 (Flash) | red 200 200 0|… (slow)",
        "conf": "wmt-lab-2026-08-05+exe",
    },
    int(Param.HASH_PERCENT): {
        "name": "hash_percent",
        "type": "int",
        "example": "10 | -10 | 0 (Adjust freq Up/Down/Norm → HashPercent)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.API_SWITCH): {
        "name": "api_switch",
        "type": "int",
        "example": "1=enable 0=disable (WMT Miner API Switch; may status=9 Need change pwd)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.FAST_BOOT): {
        "name": "fast_boot",
        "type": "int",
        "example": "1=enable 0=disable (WMT Power Fast Boot; lab Peak)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.MINING): {
        "name": "mining",
        "type": "int",
        "example": "1=resume 0=suspend (WMT Mining Control; lab Peak)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.HEAT_MODE): {
        "name": "heat_mode",
        "type": "str",
        "example": "anti-icing | heating (UI Anti-Freezing | Power-Keeping)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.POWER_LIMIT): {
        "name": "power_limit",
        "type": "int",
        "example": "2500",
        "conf": "pcap",
    },
    int(Param.POWER_PCT_FAST): {
        "name": "power_pct_fast",
        "type": "int",
        "example": "90 (WMT Adjust Power Fast Mode % → 9=90)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.UPFREQ_SPEED): {
        "name": "upfreq_speed",
        "type": "int",
        "example": "0..10 (WMT Adjust upfreq speed → UpfreqSpeed)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.NTP_SERVERS): {
        "name": "ntp_servers",
        "type": "str",
        "example": "0.cn.pool.ntp.org,0.openwrt.pool.ntp.org,, (4 comma slots)",
        "conf": "wmt-lab-2026-08-06",
    },
    int(Param.TIMEZONE): {
        "name": "timezone",
        "type": "str",
        "example": "Asia/Novosibirsk,<+07>-7 (zonename,POSIX; WMT Set Time Zone)",
        "conf": "wmt-lab-2026-08-06",
    },
    int(Param.POWER_PCT): {
        "name": "power_pct",
        "type": "int",
        "example": "96 (WMT Adjust Power Normal Mode %; not param4 HashPercent)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.SYNC_TIME): {
        "name": "sync_time",
        "type": "str",
        "example": "2026-08-06 00:10,1785949818 (WMT Time Manage → Sync Time)",
        "conf": "wmt-lab-2026-08-06",
    },
    int(Param.POWER_PCT_RESET): {
        "name": "power_pct_reset",
        "type": "int",
        "example": "100 (WMT Adjust Power Reset settings → 22=100)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.FAST_MINING): {
        "name": "fast_mining",
        "type": "int",
        "example": "1=on 0=off (WMT Fast Hash → BtminerFastMining)",
        "conf": "wmt-lab-2026-08-05",
    },
    int(Param.UNKNOWN_24): {"name": "unknown_24", "type": "int", "example": "0", "conf": "exe"},
    int(Param.UNKNOWN_25): {"name": "unknown_25", "type": "int", "example": "0", "conf": "exe"},
}

# LED presets from WMT .rdata + lab Peak
LED_SLOW = "red 200 200 0|green 200 200 0"
LED_FAST = "red 200 100 0|green 200 100 0"
LED_AUTO = "auto"  # WMT LED Control Normal (lab Peak 2026-08-05)


class NetStatus(IntEnum):
    OK = 0
    INCOMPLETE = 1
    NETPACKET_ERROR = 2
    DATA_ERROR = 4
    INCORRECT_PASSWORD = 5
    BAD_REQUEST = 3  # live: empty SET_PARAM / unknown cmd1
    MISSING_PAYLOAD = 6  # live: cmd5 without "0"|"1"|"2"
    MINER_SYS_ERROR = 7
    NEED_CHANGE_PWD = 9
    # Live M63 (2026-08): handshake reply u16_a=0x10, u16_b=8, body=challenge+token
    # Earlier notes used 0x11 — accept both in decode_response.
    CHALLENGE = 0x10
    CHALLENGE_ALT = 0x11


STATUS_TEXT = {
    int(NetStatus.OK): "ok",
    int(NetStatus.INCOMPLETE): "incomplete/invalid header",
    int(NetStatus.NETPACKET_ERROR): "NetPacket Error",
    int(NetStatus.BAD_REQUEST): "bad request / missing fields (live)",
    int(NetStatus.DATA_ERROR): "Data Error",
    int(NetStatus.INCORRECT_PASSWORD): "Incorrect Password",
    int(NetStatus.MISSING_PAYLOAD): "missing/invalid payload (live)",
    int(NetStatus.MINER_SYS_ERROR): "Miner Sys Error",
    int(NetStatus.NEED_CHANGE_PWD): "Need change pwd",
    int(NetStatus.CHALLENGE): "handshake challenge",
    int(NetStatus.CHALLENGE_ALT): "handshake challenge",
}


def crc32_raw(data: bytes) -> int:
    """WMT CRC @ 0x4910A0: init 0xFFFFFFFF, no final XOR."""
    return (zlib.crc32(data) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def crc32_payload(data: bytes) -> int:
    return crc32_raw(data)


def crc16_payload(data: bytes) -> int:
    return crc32_raw(data) & 0xFFFF


def select_factory_password(fw_version_yyyymmdd: int) -> str:
    if fw_version_yyyymmdd >= 20190626:
        return "admin"
    if 0 < fw_version_yyyymmdd < 20181121:
        return "root"
    return "F-0jk;"


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 16 != 0:
        raise ValueError("AES-ECB length must be multiple of 16")
    return AES.new(key, AES.MODE_ECB).encrypt(data)


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 16 != 0:
        raise ValueError("AES-ECB length must be multiple of 16")
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def pad16(data: bytes) -> bytes:
    if len(data) % 16 == 0:
        return data
    return data + b"\0" * (16 - (len(data) % 16))


@dataclass
class NetPacket:
    cmdcode: int
    text: bytes = b""
    binary: bytes = b""
    magic: int = MAGIC

    @property
    def payload(self) -> bytes:
        return self.text + self.binary

    def checksum(self) -> int:
        return crc32_raw(self.payload)

    def encode(self, *, pad: bool = True, encrypt_key: Optional[bytes] = None) -> bytes:
        pl = self.payload
        if len(self.text) > 0xFFFF or len(self.binary) > 0xFFFF:
            raise ValueError("payload too large")
        hdr = struct.pack(
            "<IIHHI",
            self.magic,
            self.cmdcode & 0xFFFFFFFF,
            len(self.text) & 0xFFFF,
            len(self.binary) & 0xFFFF,
            crc32_raw(pl),
        )
        pkt = hdr + pl
        if pad:
            pkt = pad16(pkt)
        if encrypt_key is not None and len(pkt) > HEADER_SIZE:
            pkt = aes_ecb_encrypt(pkt, encrypt_key)
        return pkt

    @classmethod
    def decode(cls, data: bytes, *, decrypt_key: Optional[bytes] = None) -> "NetPacket":
        raw = data
        if decrypt_key is not None and len(raw) >= 32 and len(raw) % 16 == 0:
            if struct.unpack_from("<I", raw, 0)[0] != MAGIC:
                raw = aes_ecb_decrypt(raw, decrypt_key)
        if len(raw) < HEADER_SIZE:
            raise ValueError(f"packet too short: {len(raw)}")
        magic, cmd, l1, l2, crc = struct.unpack_from("<IIHHI", raw, 0)
        if magic != MAGIC:
            raise ValueError(f"bad magic {magic:#x}")
        need = HEADER_SIZE + l1 + l2
        if len(raw) < need:
            raise ValueError(f"truncated payload need={need} have={len(raw)}")
        text = raw[HEADER_SIZE : HEADER_SIZE + l1]
        binary = raw[HEADER_SIZE + l1 : HEADER_SIZE + l1 + l2]
        got = crc32_raw(text + binary)
        if got != crc and (got & 0xFFFF) != (crc & 0xFFFF):
            if crc & 0xFFFF0000:
                raise ValueError(f"crc mismatch got={got:#x} field={crc:#x}")
        return cls(cmdcode=cmd, text=text, binary=binary, magic=magic)

    @classmethod
    def decode_response(cls, data: bytes) -> dict[str, Any]:
        if len(data) < 16:
            return {"raw": data.hex(), "len": len(data), "error": "short"}
        if struct.unpack_from("<I", data, 0)[0] != MAGIC:
            return {"raw": data[:64].hex(), "len": len(data), "error": "no_magic"}
        magic, cmd, a, b, c = struct.unpack_from("<IIHHI", data, 0)
        body = data[HEADER_SIZE:]
        status = a if len(data) <= 32 and b <= 64 else 0
        out: dict[str, Any] = {
            "magic": magic,
            "cmd": cmd,
            "u16_a": a,
            "u16_b": b,
            "u32_c": c,
            "status": status,
            "status_text": STATUS_TEXT.get(status, f"unknown({status})"),
            "body": body,
            "len": len(data),
            "raw": data[:32].hex(),
            "ok": False,
        }
        if body and (b"[MinerInfo]" in body or b"MinerType" in body or body[:1] == b"["):
            out["text"] = body.split(b"\0", 1)[0].decode("utf-8", errors="replace")
            out["status"] = 0
            out["status_text"] = "ok"
            out["ok"] = True
        elif body and a == 0 and b > 0 and len(body) >= min(b, 1):
            # data reply: a=status0, b=datasize, c=crc_lo16, body=payload
            payload = body[:b] if b <= len(body) else body
            out["text"] = payload.split(b"\0", 1)[0].decode("utf-8", errors="replace")
            out["data"] = payload.rstrip(b"\0")
            out["status"] = 0
            out["status_text"] = "ok"
            out["ok"] = True
        elif len(data) == 24 and b == 8 and a in (
            int(NetStatus.CHALLENGE),
            int(NetStatus.CHALLENGE_ALT),
            0x10,
            0x11,
        ):
            # handshake challenge: 4 B code + 4 B token
            out["challenge_code"] = struct.unpack_from("<I", body, 0)[0]
            out["token"] = body[4:8].hex()
            out["status"] = int(a)
            out["status_text"] = "handshake challenge"
            out["ok"] = True
        elif len(data) >= 16 and a == 0 and cmd != 0:
            # short success ACK: e.g. 5a5a7f7f 0d000000 00000000 ffff0000
            out["ok"] = True
            out["status"] = 0
            out["status_text"] = "ok"
        elif status == int(NetStatus.NETPACKET_ERROR):
            out["ok"] = False
        return out


def build_auth_text(
    miner_ip: str,
    account: str,
    password: str,
    token: Optional[str] = None,
    ts: Optional[int] = None,
) -> str:
    if ts is None:
        ts = int(time.time())
    base = f"{miner_ip}|{ts}|{account}|{password}"
    if token:
        return f"{base}|{token}"
    return base


def format_param(param_id: int, value: Union[str, int]) -> bytes:
    return f"{int(param_id)}={value}".encode("utf-8")


def format_ntp_servers(*servers: str, slots: int = 4) -> bytes:
    """
    WMT **Set NTP Server** wire for cmd 13 param **18**.

    Lab Peak 2026-08-06::

        18=0.cn.pool.ntp.org,0.openwrt.pool.ntp.org,,

    Up to ``slots`` hosts (default **4**), comma-separated; missing slots
    are empty strings (trailing commas preserved as in WMT).
    """
    vals = [str(s).strip() for s in servers if s is not None]
    # allow passing a single list
    if len(vals) == 1 and isinstance(servers[0], (list, tuple)):
        vals = [str(s).strip() for s in servers[0]]
    if not any(vals):
        raise ValueError("at least one NTP server required")
    if len(vals) > slots:
        raise ValueError(f"at most {slots} NTP servers")
    while len(vals) < slots:
        vals.append("")
    return format_param(Param.NTP_SERVERS, ",".join(vals))


def format_timezone(zonename: str, posix_tz: str) -> bytes:
    """
    WMT **Set Time Zone** wire for cmd 13 param **19**.

    Lab Peak 2026-08-06::

        19=Asia/Novosibirsk,<+07>-7

    ``zonename`` is IANA-like (``Asia/Novosibirsk``); ``posix_tz`` is the
    POSIX TZ string (e.g. ``<+07>-7`` for UTC+7).
    """
    z = zonename.strip().replace("\\", "/")
    p = posix_tz.strip()
    if not z or not p:
        raise ValueError("zonename and posix_tz required")
    return format_param(Param.TIMEZONE, f"{z},{p}")


def format_sync_time(
    when: Optional[float] = None,
    *,
    local: bool = True,
) -> bytes:
    """
    WMT **Time Manage → Sync Time** wire value for cmd 13 param **21**.

    Lab Peak 2026-08-06::

        21=2026-08-06 00:10,1785949818

    i.e. ``YYYY-MM-DD HH:MM`` (no seconds) + comma + unix timestamp.
    ``when`` is unix seconds (default now). Uses local time for the string
    when ``local=True`` (WMT host clock).
    """
    import datetime as _dt

    ts = int(time.time() if when is None else when)
    if local:
        dt = _dt.datetime.fromtimestamp(ts)
    else:
        dt = _dt.datetime.utcfromtimestamp(ts)
    stamp = dt.strftime("%Y-%m-%d %H:%M")
    return format_param(Param.SYNC_TIME, f"{stamp},{ts}")


def format_pool_line(
    url: str,
    user: str,
    password: str = "x",
    *,
    strategy: str = "FAILOVER",
    index: int = 0,
) -> bytes:
    """
    One pool slot for cmd 2.

    Wire field order (lab Peak 2026-08-05 / WMT UI success)::

        {index},stratum+tcp://{host:port},{user},{strategy},,{password}|
    """
    if "://" not in url:
        url = f"stratum+tcp://{url}"
    return f"{index},{url},{user},{strategy},,{password}|".encode("utf-8")


def format_permissions(
    user1: Union[int, PermissionLevel] = 0,
    user2: Union[int, PermissionLevel] = 0,
    user3: Union[int, PermissionLevel] = 0,
) -> bytes:
    """
    cmd 14 binary (WMT Permissions Configuration).

    Lab Peak: user1=LEVEL_2, user2=LEVEL_1, user3=LEVEL_0 →
    ``user1=2,user2=1,user3=0``
    """
    return (
        f"user1={int(user1)},user2={int(user2)},user3={int(user3)}".encode("ascii")
    )


def format_password_change(
    account: str,
    old_password: str,
    new_password: str,
) -> bytes:
    """
    cmd 4 binary (WMT Update password, lab Peak 2026-08-05).

    Wire::

        {len(account)},{len(old)},{len(new)},{account}{old}{new}

    Example admin→admin1: ``5,5,6,adminadminadmin1``
    """
    a = account.encode("utf-8")
    o = old_password.encode("utf-8")
    n = new_password.encode("utf-8")
    return f"{len(a)},{len(o)},{len(n)},".encode("ascii") + a + o + n


def format_pools(
    pools: list[dict[str, Any]],
    *,
    strategy: str = "FAILOVER",
) -> bytes:
    """
    Full cmd-2 binary: one or more ``format_pool_line`` segments concatenated.

    WMT sends **all configured pools in a single cmd 2** (e.g. pool0|pool1|).

    Each dict: ``url`` (or ``pool``), ``user`` (or ``worker``), optional
    ``password``/``passwd``/``pass``, optional ``strategy``, optional ``index``.
    Index defaults to list order (0, 1, 2, …).
    """
    if not pools:
        raise ValueError("pools list must not be empty")
    out = bytearray()
    for i, p in enumerate(pools):
        url = p.get("url") or p.get("pool") or ""
        user = p.get("user") or p.get("worker") or ""
        password = p.get("password") or p.get("passwd") or p.get("pass") or "x"
        strat = p.get("strategy") or strategy
        idx = int(p["index"]) if "index" in p else i
        if not url or not user:
            raise ValueError(f"pool[{i}] needs url and user")
        out += format_pool_line(url, user, password, strategy=strat, index=idx)
    return bytes(out)


def parse_miner_info_text(text: str) -> dict[str, Any]:
    """Parse WMT [MinerInfo]/[PowerInfo]/SUMMARY dump into a dict."""
    result: dict[str, Any] = {"sections": {}, "raw_keys": {}}
    section = "_"
    result["sections"][section] = {}

    # Fix glued meta markers: #MAC#[PowerInfo]
    text = re.sub(r"#([^#\n]*)#(\[[^\]]+\])", r"\n#\1\n\2\n", text)

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            section = m.group(1)
            result["sections"].setdefault(section, {})
            continue
        if line.startswith("#"):
            result.setdefault("meta_lines", []).append(line)
            # #web_pool=1,sshd=0#super=255 ...
            for mm in re.finditer(r"([A-Za-z0-9_]+)=([0-9A-Za-z_.+-]+)", line):
                result["raw_keys"][mm.group(1)] = mm.group(2)
            continue
        if "=" in line:
            if any(
                line.startswith(p)
                for p in ("SUMMARY", "EDEVS", "POOLS", "ASC=", "POOL=", "EDEVS")
            ) or ",MHS " in line or line.startswith("Factory Error"):
                result["sections"].setdefault(section, {})
                result["sections"][section].setdefault("_lines", []).append(line)
                for key in (
                    "Power Mode",
                    "Power Limit",
                    "Elapsed",
                    "Uptime",
                    "MHS av",
                    "MHS 15m",
                    "HS RT",
                    "Power",
                    "EnvTemp",
                    "Chip Temp Avg",
                    "Chip Temp Min",
                    "Chip Temp Max",
                    "Fan Speed In",
                    "Fan Speed Out",
                    "Pool Rejected%",
                    "freq_avg",
                ):
                    mm = re.search(rf"{re.escape(key)}=([^,|]+)", line)
                    if mm:
                        result["raw_keys"][key] = mm.group(1).strip()
                # pool user/url from POOLS blob
                mm = re.search(r"URL=([^,|]+)", line)
                if mm:
                    result["raw_keys"]["pool_url"] = mm.group(1)
                mm = re.search(r"User=([^,|]+)", line)
                if mm:
                    result["raw_keys"]["pool_user"] = mm.group(1)
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            result["sections"].setdefault(section, {})[k] = v
            result["raw_keys"][k] = v

    mi = result["sections"].get("MinerInfo", {})
    pi = result["sections"].get("PowerInfo", {})
    result["miner_type"] = mi.get("MinerType")
    result["firmware"] = mi.get("FirmwareVersion")
    result["power_mode"] = mi.get("PowerMode") or result["raw_keys"].get("Power Mode")
    result["power_limit"] = mi.get("PowerLimitSet") or result["raw_keys"].get("Power Limit")
    result["api_switch"] = mi.get("MinerApiSwitch") or result["raw_keys"].get("MinerApiSwitch")
    result["power_on"] = pi.get("PowerOnOff") or result["raw_keys"].get("PowerOnOff")
    result["coin_type"] = mi.get("CoinType")
    result["hash_percent"] = mi.get("HashPercent")
    result["heat_mode"] = mi.get("HeatMode")
    result["fast_boot"] = mi.get("BtminerFastBoot")
    result["fast_mining"] = mi.get("BtminerFastMining")
    result["web_pool"] = result["raw_keys"].get("web_pool")
    result["upfreq_speed"] = mi.get("UpfreqSpeed") or result["raw_keys"].get("UpfreqSpeed")
    result["mac"] = None
    for meta in result.get("meta_lines", []):
        mm = re.search(r"#([0-9A-Fa-f:]{17})#", meta)
        if mm:
            result["mac"] = mm.group(1)
            break
    return result


class NetPacketClient:
    """
    WhatsMinerTool-compatible client on TCP 8889.

    Works with Miner API Switch OFF; uses static AES keys + Remote account.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        account: str = "super",
        password: str = "super",
        miner_ip: Optional[str] = None,
        timeout: float = 5.0,
        recv_wait: float = 3.0,
    ):
        self.host = host
        self.port = port
        self.account = account
        self.password = password
        self.miner_ip = miner_ip or host
        self.timeout = timeout
        self.recv_wait = recv_wait
        self.token: Optional[str] = None
        self.aes_key: Optional[bytes] = None  # override for tests

    # --- transport ---

    def _recv(self, sock: socket.socket, wait: Optional[float] = None) -> bytes:
        wait = self.recv_wait if wait is None else wait
        sock.settimeout(0.4)
        chunks: list[bytes] = []
        deadline = time.time() + wait
        last_data = time.time()
        while time.time() < deadline:
            try:
                c = sock.recv(65536)
                if not c:
                    break
                chunks.append(c)
                last_data = time.time()
                if sum(map(len, chunks)) >= MAX_PACKET:
                    break
                deadline = max(deadline, last_data + 0.6)
            except socket.timeout:
                if chunks:
                    break
                continue
        return b"".join(chunks)

    def exchange_raw(
        self, packet: bytes, *, wait: Optional[float] = None, shutdown: bool = False
    ) -> bytes:
        with socket.create_connection((self.host, self.port), self.timeout) as s:
            s.settimeout(self.timeout)
            s.sendall(packet)
            if shutdown:
                try:
                    s.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            return self._recv(s, wait)

    def send_command(
        self,
        cmd: int,
        binary: Union[str, bytes] = b"",
        *,
        text: Optional[Union[str, bytes]] = None,
        with_token: bool = True,
        key: Optional[bytes] = None,
        wait: Optional[float] = None,
        check: bool = False,
    ) -> dict[str, Any]:
        """
        Low-level send. Builds auth text unless ``text`` is provided.

        ``with_token=False`` only for handshake (cmd 0).
        """
        if text is None:
            if cmd == int(Cmd.HANDSHAKE) or not with_token:
                text_s = build_auth_text(self.miner_ip, self.account, self.password)
            else:
                text_s = build_auth_text(
                    self.miner_ip, self.account, self.password, token=self.ensure_token()
                )
            text_b = text_s.encode("utf-8")
        elif isinstance(text, str):
            text_b = text.encode("utf-8")
        else:
            text_b = text

        if isinstance(binary, str):
            bin_b = binary.encode("utf-8")
        else:
            bin_b = binary

        k = key if key is not None else (self.aes_key or key_for_cmd(cmd))
        pkt = NetPacket(cmdcode=int(cmd), text=text_b, binary=bin_b).encode(encrypt_key=k)
        resp = NetPacket.decode_response(self.exchange_raw(pkt, wait=wait))
        if check:
            st = resp.get("status")
            if st in (
                int(NetStatus.NETPACKET_ERROR),
                int(NetStatus.INCORRECT_PASSWORD),
                int(NetStatus.NEED_CHANGE_PWD),
                int(NetStatus.DATA_ERROR),
                int(NetStatus.MINER_SYS_ERROR),
            ) or resp.get("error"):
                raise CommandError(
                    f"{self.host}: cmd={cmd} rejected status={st} "
                    f"({resp.get('status_text')})",
                    raw=resp,
                )
        return resp

    # keep older name
    def send(
        self,
        cmd: int,
        text: Union[str, bytes] = b"",
        binary: Union[str, bytes] = b"",
        *,
        key: Optional[bytes] = None,
        wait: Optional[float] = None,
    ) -> dict[str, Any]:
        return self.send_command(cmd, binary=binary, text=text, key=key, wait=wait, with_token=False)

    # --- session ---

    def handshake(self) -> str:
        resp = self.send_command(
            Cmd.HANDSHAKE, with_token=False, key=KEY0, wait=2.0, check=False
        )
        token = resp.get("token")
        if not token:
            raise AuthError(
                f"{self.host}: handshake failed status={resp.get('status')} "
                f"({resp.get('status_text')}) len={resp.get('len')} raw={resp.get('raw')}"
            )
        self.token = token
        return token

    def ensure_token(self) -> str:
        if not self.token:
            return self.handshake()
        return self.token

    def invalidate_token(self) -> None:
        self.token = None

    # --- read ---

    def get_info(self, *, retry: bool = True) -> dict[str, Any]:
        """cmd=22 → MinerInfo / PowerInfo / SUMMARY."""
        resp = self.send_command(Cmd.GET_INFO, wait=4.0)
        body = resp.get("body") or b""
        if b"[MinerInfo]" not in body and b"MinerType" not in body:
            if retry:
                self.invalidate_token()
                resp = self.send_command(Cmd.GET_INFO, wait=4.0)
                body = resp.get("body") or b""
        if b"[MinerInfo]" not in body and b"MinerType" not in body:
            raise CommandError(
                f"{self.host}: get_info failed status={resp.get('status')} len={resp.get('len')}",
                raw=resp,
            )
        text_s = body.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        parsed = parse_miner_info_text(text_s)
        parsed["raw_text"] = text_s
        parsed["token"] = self.token
        return parsed

    def get_detected_hashrate(self) -> dict[str, Any]:
        """
        cmd=15 empty → DetectedHashRate string ``gh0:gh1:gh2:gh3`` (per board, GH/s units as in MinerInfo).
        """
        resp = self.send_command(Cmd.GET_HASHRATE, wait=2.0, check=False)
        data = resp.get("data") or resp.get("body") or b""
        text = (resp.get("text") or data.split(b"\0", 1)[0].decode("utf-8", "replace")).strip()
        if not text or (not resp.get("ok") and resp.get("status") not in (0, None)):
            raise CommandError(
                f"{self.host}: get_detected_hashrate failed status={resp.get('status')}",
                raw=resp,
            )
        parts = [p for p in text.split(":") if p != ""]
        values = []
        for p in parts:
            try:
                values.append(int(p))
            except ValueError:
                try:
                    values.append(float(p))
                except ValueError:
                    values.append(p)
        return {"raw": text, "boards": values, "token": self.token}

    # --- SET_PARAM (cmd 13) ---

    def set_param(
        self, param_id: Union[int, Param], value: Union[str, int], *, check: bool = True
    ) -> dict[str, Any]:
        """cmd=13 binary ``{id}={value}``."""
        return self.send_command(
            Cmd.SET_PARAM,
            binary=format_param(int(param_id), value),
            wait=2.0,
            check=check,
        )

    def set_power_limit(self, watts: int) -> dict[str, Any]:
        return self.set_param(Param.POWER_LIMIT, int(watts))

    def set_power_pct(self, percent: int, *, fast: bool = False) -> dict[str, Any]:
        """
        WMT **Adjust Power** percentage.

        Lab Peak:

        - **Normal Mode** (default): cmd 13 param **20** → ``20=96``
        - **Fast Mode** (``fast=True``): cmd 13 param **9** → ``9=90``

        Distinct from :meth:`set_hash_percent` (param **4**, freq adjust).
        Does not change ``PowerLimitSet`` watts; scales toward the limit.
        Public V2: ``set_power_pct`` (fast) / ``set_power_pct_v2`` (normal).
        """
        pid = Param.POWER_PCT_FAST if fast else Param.POWER_PCT
        return self.set_param(pid, int(percent))

    def set_power_pct_fast(self, percent: int) -> dict[str, Any]:
        """WMT **Adjust Power → Fast Mode** % (cmd 13 ``9=N``)."""
        return self.set_power_pct(percent, fast=True)

    def reset_power_pct(self) -> dict[str, Any]:
        """
        WMT **Adjust Power → Reset settings** (cmd 13 param 22).

        Lab Peak: binary ``22=100``, ACK status=0 (maps to public
        ``restore_power_pct``).
        """
        return self.set_param(Param.POWER_PCT_RESET, 100)

    def set_ntp_servers(
        self,
        *servers: str,
        slots: int = 4,
    ) -> dict[str, Any]:
        """
        WMT **Set NTP Server** (cmd 13 param **18**).

        Lab Peak 2026-08-06: binary
        ``18=0.cn.pool.ntp.org,0.openwrt.pool.ntp.org,,`` → ACK empty.

        Pass 1–4 hostnames (or a single list). Empty trailing slots are
        sent as empty fields between commas (WMT always uses 4 slots).
        """
        if len(servers) == 1 and isinstance(servers[0], (list, tuple)):
            hosts = list(servers[0])
        else:
            hosts = list(servers)
        return self.send_command(
            Cmd.SET_PARAM,
            binary=format_ntp_servers(*hosts, slots=slots),
            wait=2.0,
            check=True,
        )

    def set_timezone(
        self,
        zonename: str,
        posix_tz: Optional[str] = None,
        *,
        offset_hours: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        WMT **Set Time Zone** (cmd 13 param **19**).

        Lab Peak 2026-08-06: ``19=Asia/Novosibirsk,<+07>-7`` → ACK empty.

        Provide either:

        - ``posix_tz`` explicitly (e.g. ``"<+07>-7"``), or
        - ``offset_hours`` (e.g. ``7`` → ``<+07>-7``, ``-5`` → ``<-05>5``).

        Public V3 analogue: ``set.system.timezone`` with timezone + zonename.
        """
        if posix_tz is None:
            if offset_hours is None:
                raise ValueError("set_timezone needs posix_tz= or offset_hours=")
            h = int(offset_hours)
            mag = abs(h)
            # Lab MicroBT style: UTC+7 → <+07>-7 ; UTC-5 → <-05>5
            if h >= 0:
                posix_tz = f"<+{mag:02d}>-{h}"
            else:
                posix_tz = f"<-{mag:02d}>{mag}"
        return self.send_command(
            Cmd.SET_PARAM,
            binary=format_timezone(zonename, posix_tz),
            wait=2.0,
            check=True,
        )

    def sync_time(
        self,
        when: Optional[float] = None,
        *,
        local: bool = True,
    ) -> dict[str, Any]:
        """
        WMT **Time Manage → Sync Time** (cmd 13 param **21**).

        Lab Peak 2026-08-06: binary ``21=2026-08-06 00:10,1785949818``
        (``YYYY-MM-DD HH:MM,{unix_ts}``), ACK empty.

        Distinct from :meth:`set_timezone` (param **19**) and top-level
        cmd **21** (firmware status).
        """
        return self.send_command(
            Cmd.SET_PARAM,
            binary=format_sync_time(when, local=local),
            wait=2.0,
            check=True,
        )

    def set_upfreq_speed(self, speed: int) -> dict[str, Any]:
        """
        WMT **Adjust upfreq speed** (cmd 13 param 15).

        Lab Peak: UI values **0..10** → ``15={n}`` → ``UpfreqSpeed``
        (captured 0, 5, 10).
        """
        n = int(speed)
        if n < 0 or n > 10:
            raise ValueError("upfreq speed must be 0..10 (WMT UI range)")
        return self.set_param(Param.UPFREQ_SPEED, n)

    def set_fast_boot(self, enabled: bool = True) -> dict[str, Any]:
        """
        WMT **Power Fast Boot** (cmd 13 param 7).

        Lab Peak 2026-08-05: Enable → binary ``7=1`` → ``BtminerFastBoot=1``.
        Disable ``7=0`` → ``BtminerFastBoot=0``.
        """
        return self.set_param(Param.FAST_BOOT, 1 if enabled else 0)

    def set_fast_mining(self, enabled: bool = True) -> dict[str, Any]:
        """
        WMT **Fast Hash** (cmd 13 param 23).

        Lab Peak WMT UI: On → ``23=1``, Off → ``23=0`` → ``BtminerFastMining``.
        """
        return self.set_param(Param.FAST_MINING, 1 if enabled else 0)

    def set_fast_hash(self, enabled: bool = True) -> dict[str, Any]:
        """Alias of :meth:`set_fast_mining` (WMT UI name Fast Hash)."""
        return self.set_fast_mining(enabled)

    def set_api_switch(self, enabled: bool = True, *, check: bool = False) -> dict[str, Any]:
        """
        WMT **Miner API Switch** (cmd 13 param 6) — gates public write API.

        Lab Peak 2026-08-05:

        - Enable → ``6=1`` → ASIC **status 9** (Need change pwd); switch stays 0
        - Disable → ``6=0`` → **status 0** ok

        Default ``check=False`` so callers can inspect status 9 without exception.
        """
        return self.set_param(Param.API_SWITCH, 1 if enabled else 0, check=check)

    def set_performance_mode(
        self, mode: Union[int, PerformanceMode, str]
    ) -> dict[str, Any]:
        """
        WMT **Performance Mode**: Low / Normal / High.

        Wire: **cmd 5** with lab-verified binary (≠ MinerInfo for Low/Normal):

        - Low → ``"1"``, Normal → ``"0"``, High → ``"2"``

        ``mode`` is **semantic** (0/1/2 or ``"low"|"normal"|"high"``), same as
        MinerInfo ``PowerMode``. Encoding via :func:`performance_mode_to_wire`.
        """
        wire = performance_mode_to_wire(mode)
        return self.send_command(Cmd.SET_PERFORMANCE, binary=wire, wait=2.0, check=True)

    def set_power_mode(self, mode: Union[int, PerformanceMode]) -> dict[str, Any]:
        """Alias of :meth:`set_performance_mode` (WMT Performance Mode)."""
        return self.set_performance_mode(mode)

    def set_mining(self, enabled: bool) -> dict[str, Any]:
        """
        WMT **Mining Control** (cmd 13 param 8).

        Lab-verified vs WhatsMinerTool UI (Peak / M63, 2026-08-05):

        - ``True``  → Resume Mining → binary ``8=1``
        - ``False`` → Suspend Mining → binary ``8=0``

        Earlier notes had the polarity inverted; wire captures with UI
        "success" establish this mapping.
        """
        return self.set_param(Param.MINING, 1 if enabled else 0)

    def suspend(self) -> dict[str, Any]:
        """WMT Suspend Mining → ``8=0``."""
        return self.set_mining(False)

    def resume(self) -> dict[str, Any]:
        """WMT Resume Mining → ``8=1``."""
        return self.set_mining(True)

    def set_heat_mode(self, mode: str = "anti-icing") -> dict[str, Any]:
        """
        WMT **Protection Mode** (cmd 13 param 13).

        Lab Peak 2026-08-05:

        | WMT UI | Wire ``13=…`` | MinerInfo HeatMode |
        |--------|---------------|--------------------|
        | Anti-Freezing (default) | ``anti-icing`` | ``anti-freezing`` |
        | Power-Keeping | ``heating`` | ``heating`` |

        Aliases: anti-freezing/default → anti-icing; power-keeping/powerkeeping → heating.
        """
        key = (mode or "").strip().lower().replace("_", "-").replace(" ", "-")
        aliases = {
            "anti-freezing": "anti-icing",
            "antifreezing": "anti-icing",
            "default": "anti-icing",
            "anti-icing": "anti-icing",
            "antiicing": "anti-icing",
            "power-keeping": "heating",
            "powerkeeping": "heating",
            "heating": "heating",
        }
        wire = aliases.get(key, mode)
        return self.set_param(Param.HEAT_MODE, wire)

    def set_led(
        self,
        pattern: Optional[str] = None,
        *,
        preset: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        WMT **LED Control** (cmd 13 param 1).

        Lab Peak 2026-08-05:

        - **Flash** → ``1=red 200 100 0|green 200 100 0`` (``LED_FAST``)
        - **Normal** → ``1=auto`` (``LED_AUTO``)

        ``preset``: ``normal``/``auto`` | ``fast``/``flash`` | ``slow`` | raw ``pattern``.
        """
        if pattern is None:
            if preset in ("normal", "auto"):
                pattern = LED_AUTO
            elif preset in (None, "fast", "flash"):
                pattern = LED_FAST
            elif preset == "slow":
                pattern = LED_SLOW
            else:
                pattern = preset
        return self.set_param(Param.LED, pattern)

    def set_hash_percent(self, percent: Union[int, str]) -> dict[str, Any]:
        """
        WMT **Adjust freq** percent (cmd 13 param 4).

        Lab Peak 2026-08-05:

        - Up **10%** → ``4=10`` → ``HashPercent=10``
        - Down **10%** → ``4=-10`` → ``HashPercent=-10``
        - **Norm** → ``4=0`` → ``HashPercent=0``
        """
        return self.set_param(Param.HASH_PERCENT, int(percent))

    def set_power_percent(self, percent: Union[int, str]) -> dict[str, Any]:
        """Alias of :meth:`set_hash_percent` (WMT frequency / hash percent)."""
        return self.set_hash_percent(percent)

    def adjust_freq_up(self, percent: int = 10) -> dict[str, Any]:
        """WMT Adjust freq Up → ``4=+percent`` (positive)."""
        return self.set_hash_percent(abs(int(percent)))

    def adjust_freq_down(self, percent: int = 10) -> dict[str, Any]:
        """WMT Adjust freq Down → ``4=-percent``."""
        return self.set_hash_percent(-abs(int(percent)))

    def adjust_freq_norm(self) -> dict[str, Any]:
        """WMT Adjust freq Norm → ``4=0``."""
        return self.set_hash_percent(0)

    # --- other cmds ---

    def set_web_pools(self, enabled: bool = True) -> dict[str, Any]:
        """
        WMT **Web Pools Switch** (cmd 12).

        Lab Peak 2026-08-05:

        - Enable → ``1`` → ``web_pool=1``
        - Disable → ``0`` → ``web_pool=0``
        """
        return self.send_command(
            Cmd.SET_WEB_POOLS,
            binary="1" if enabled else "0",
            wait=2.0,
            check=True,
        )

    def set_pools(
        self,
        url: Optional[str] = None,
        user: Optional[str] = None,
        password: str = "x",
        *,
        strategy: str = "FAILOVER",
        index: int = 0,
        pools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """
        cmd=2 SET_POOLS (WMT pool dialog).

        Lab Peak 2026-08-05 — WMT UI success, one 176 B frame::

            0,stratum+tcp://…:3333,user0,FAILOVER,,pw|
            1,stratum+tcp://…:3333,user1,FAILOVER,,pw|

        Pass either a single ``url``/``user`` or ``pools=[{url,user,...}, ...]``
        (multi-pool in **one** command, as WMT does).
        """
        if pools is not None:
            binary = format_pools(pools, strategy=strategy)
        else:
            if not url or not user:
                raise ValueError("set_pools requires url+user or pools=[...]")
            binary = format_pool_line(
                url, user, password, strategy=strategy, index=index
            )
        return self.send_command(
            Cmd.SET_POOLS,
            binary=binary,
            wait=3.0,
            check=True,
        )

    def set_password(
        self,
        old_password: str,
        new_password: str,
        *,
        account: str = "admin",
    ) -> dict[str, Any]:
        """
        WMT **Update password** (cmd 4).

        Lab Peak 2026-08-05: admin→admin1 → binary
        ``5,5,6,admin`` + ``admin`` + ``admin1``, ACK status=0.

        NetPacket auth still uses Remote account (default ``super``); ``account``
        here is the miner web/API login being changed (usually ``admin``).
        """
        return self.send_command(
            Cmd.SET_PASSWORD,
            binary=format_password_change(account, old_password, new_password),
            wait=3.0,
            check=True,
        )

    def set_permissions(
        self,
        user1: Union[int, PermissionLevel] = 0,
        user2: Union[int, PermissionLevel] = 0,
        user3: Union[int, PermissionLevel] = 0,
    ) -> dict[str, Any]:
        """
        WMT **Permissions Configuration** (cmd 14).

        Lab Peak: LEVEL_2/1/0 → binary ``user1=2,user2=1,user3=0``;
        MinerInfo meta ``user1=2 user2=1 user3=0`` (``super=255`` unchanged).
        """
        return self.send_command(
            Cmd.SET_PERMISSIONS,
            binary=format_permissions(user1, user2, user3),
            wait=2.0,
            check=True,
        )

    def set_coin_type(self, coin: str = "HC") -> dict[str, Any]:
        """cmd=6 binary coin code (PCAP: ``HC``)."""
        return self.send_command(Cmd.SET_COIN, binary=coin, wait=2.0, check=True)

    def reboot(self) -> dict[str, Any]:
        """
        WMT **Reboot** (cmd 8, empty binary + auth).

        Lab Peak: ACK status=0 then unit reboots (frame may be sent twice).
        """
        return self.send_command(Cmd.REBOOT, wait=2.0, check=False)

    def factory_reset(self) -> dict[str, Any]:
        """cmd=10 empty — **factory restore** (destructive)."""
        return self.send_command(Cmd.FACTORY_RESET, wait=2.0, check=False)

    def firmware_status(self) -> dict[str, Any]:
        """
        WMT post-upgrade poll (cmd 21, empty binary + auth).

        Lab Peak: repeated empty ACKs, then binary ``upgrade=success\\n``.
        """
        return self.send_command(Cmd.FIRMWARE_STATUS, wait=5.0, check=False)

    def update_firmware(
        self,
        image: bytes,
        *,
        poll_status: bool = True,
        poll_attempts: int = 30,
        wait_upload: float = 600.0,
        progress: Callable[[dict[str, Any]], None] | None = None,
        chunk_size: int = 65536,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        """
        WMT **firmware upgrade** (lab Peak 2026-08-05).

        Wire on one TCP session to :8889::

            1. AES NetPacket KEY1 **cmd=7**, empty binary, auth text (+token)
            2. raw little-endian ``u32`` size + ``size`` bytes of image container
               (sent in chunks so ``progress`` can report upload %)
            3. plain response cmd=7 (``lt=0`` ok; lab incomplete transfer had ``lt=1``)
            4. optional: new sessions **cmd=21** until body contains ``upgrade=success``

        ``progress`` is called with dicts like::

            {"stage": "upload", "pct": 42, "sent": n, "total": m}
            {"stage": "status", "pct": 80, "status_text": "upgrade=…", "poll": 3}

        The image is the tool's signed/encrypted container (not raw rootfs);
        lab success blob ~12 627 688 bytes, md5 ``01f4d79a905e33a35467900b004e3fa6``.

        **Destructive** — only call with a known-good image for the platform.
        """
        if not image:
            raise ValueError("firmware image is empty")

        def _prog(stage: str, pct: float | None = None, **extra: Any) -> None:
            if progress is None:
                return
            try:
                payload: dict[str, Any] = {"stage": stage}
                if pct is not None:
                    payload["pct"] = float(pct)
                payload.update(extra)
                progress(payload)
            except Exception:
                pass

        _prog("auth", 0)
        token = self.ensure_token()
        text_s = build_auth_text(
            self.miner_ip, self.account, self.password, token=token
        )
        k = self.aes_key or key_for_cmd(int(Cmd.UPDATE_FIRMWARE))
        frame = NetPacket(
            cmdcode=int(Cmd.UPDATE_FIRMWARE),
            text=text_s.encode("utf-8"),
            binary=b"",
        ).encode(encrypt_key=k)
        stream = struct.pack("<I", len(image)) + image
        total = len(stream)
        cs = max(4096, int(chunk_size or 65536))

        def _recv_n(sock: socket.socket, n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = sock.recv(min(65536, n - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
            return bytes(buf)

        # Dedicated socket so the binary stream is not interleaved with pollers.
        _prog("connect", 1)
        sock = socket.create_connection(
            (self.host, self.port), timeout=max(30.0, self.timeout)
        )
        try:
            sock.settimeout(wait_upload)
            _prog("cmd7", 2)
            sock.sendall(frame)
            sent = 0
            while sent < total:
                n = min(cs, total - sent)
                sock.sendall(stream[sent : sent + n])
                sent += n
                # 3–75% reserved for wire upload
                pct = 3.0 + (72.0 * sent / total) if total else 75.0
                _prog(
                    "upload",
                    pct,
                    sent=sent,
                    total=total,
                    image_bytes=len(image),
                )
            _prog("ack", 76)
            hdr = _recv_n(sock, 16)
            if len(hdr) < 16 or struct.unpack_from("<I", hdr, 0)[0] != MAGIC:
                return {
                    "ok": False,
                    "cmd": int(Cmd.UPDATE_FIRMWARE),
                    "error": "bad_ack_header",
                    "raw": hdr,
                    "uploaded": len(image),
                }
            lt, lb = struct.unpack_from("<HH", hdr, 8)
            body = _recv_n(sock, lt + lb) if (lt + lb) else b""
            cmd = struct.unpack_from("<I", hdr, 4)[0]
            status_word = struct.unpack_from("<I", hdr, 12)[0]
            upload_resp: dict[str, Any] = {
                "ok": lt == 0 and cmd == int(Cmd.UPDATE_FIRMWARE),
                "cmd": cmd,
                "lt": lt,
                "lb": lb,
                "status_word": status_word,
                "body": body,
                "uploaded": len(image),
                "transport": "netpacket",
            }
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if not poll_status:
            _prog("done", 100 if upload_resp.get("ok") else 76)
            return upload_resp

        last: dict[str, Any] = {}
        polls = max(1, int(poll_attempts))
        interval = max(0.2, float(poll_interval or 1.0))
        status_log: list[str] = []
        for i in range(polls):
            last = self.firmware_status()
            body = last.get("body") or last.get("binary") or last.get("text") or b""
            if isinstance(body, str):
                body_b = body.encode("utf-8", "replace")
            else:
                body_b = bytes(body or b"")
            text = body_b.decode("utf-8", "replace").strip()
            if text:
                status_log.append(text)
            # 76–98% during status polls
            pct = 76.0 + (22.0 * (i + 1) / polls)
            _prog(
                "status",
                pct,
                poll=i + 1,
                polls=polls,
                status_text=text[:200],
                status_raw=body_b[:200],
            )
            low = text.lower()
            if b"upgrade=success" in body_b or "upgrade=success" in low:
                upload_resp["upgrade"] = "success"
                upload_resp["status_poll"] = last
                upload_resp["status_log"] = status_log[-20:]
                upload_resp["ok"] = True
                _prog("success", 100, status_text=text[:200])
                return upload_resp
            if (
                b"upgrade=fail" in body_b
                or b"upgrade=failed" in body_b
                or "upgrade=fail" in low
                or "upgrade=error" in low
            ):
                upload_resp["upgrade"] = "fail"
                upload_resp["status_poll"] = last
                upload_resp["status_log"] = status_log[-20:]
                upload_resp["ok"] = False
                upload_resp["error"] = text or "upgrade=fail"
                _prog("fail", pct, status_text=text[:200])
                return upload_resp
            time.sleep(interval)
        upload_resp["status_poll"] = last
        upload_resp["status_log"] = status_log[-20:]
        upload_resp["upgrade"] = "unknown"
        _prog(
            "unknown",
            99,
            status_text=(status_log[-1] if status_log else ""),
        )
        return upload_resp

    def restore_dhcp(self) -> dict[str, Any]:
        """
        WMT **Restore miner → Restore DHCP** (cmd 11, empty binary + auth).

        Lab Peak 2026-08-05: KEY1 cmd=11, text=auth, bin empty → plaintext ACK
        cmd=11 lt=0 lb=0 (w3=0xffff). May trigger DHCP renew on the ASIC.

        Public analogue: V2 ``net_config`` / V3 ``set.system.net_config`` (DHCP mode).
        """
        return self.send_command(Cmd.RESTORE_DHCP, wait=3.0, check=True)

    def restore_miner_settings(self) -> dict[str, Any]:
        """
        WMT **Restore miner → Restore miner settings** (cmd 25, empty binary + auth).

        Lab Peak 2026-08-05: KEY1 cmd=25, text=auth, bin empty → plaintext ACK
        cmd=25 lt=0 lb=0 (status 0). Softer than :meth:`factory_reset` (cmd 10).

        Public V3 analogue: ``set.miner.restore_setting``.
        """
        return self.send_command(Cmd.RESTORE_SETTINGS, wait=3.0, check=True)

    def restore_settings(self) -> dict[str, Any]:
        """Alias of :meth:`restore_miner_settings`."""
        return self.restore_miner_settings()

    def export_log(
        self,
        *,
        wait: float = 120.0,
        max_bytes: int = 50_000_000,
    ) -> dict[str, Any]:
        """
        WMT **Export Log** (cmd 20, empty binary + auth).

        Lab Peak 2026-08-05 response (not NetPacket ZZ frame)::

            u32le gzip_len  +  gzip stream (1f 8b …)

        Decompressed payload is a **ustar** of ``{ip}.logs/`` (system.log,
        miner-state, pools, temps, …). Live pull ~20 MB gzip → ~64 MB tar.

        Returns ``data`` (decompressed tar bytes) when gunzip succeeds.
        """
        import gzip
        import io

        self.ensure_token()
        text_s = build_auth_text(
            self.miner_ip, self.account, self.password, token=self.token
        )
        pkt = NetPacket(
            cmdcode=int(Cmd.EXPORT_LOG),
            text=text_s.encode("utf-8"),
            binary=b"",
        ).encode(encrypt_key=KEY1)

        with socket.create_connection((self.host, self.port), self.timeout) as s:
            s.settimeout(self.timeout)
            s.sendall(pkt)
            s.settimeout(0.5)
            chunks: list[bytes] = []
            deadline = time.time() + wait
            last = time.time()
            total = 0
            while time.time() < deadline and total < max_bytes:
                try:
                    c = s.recv(256 * 1024)
                    if not c:
                        break
                    chunks.append(c)
                    total += len(c)
                    last = time.time()
                    deadline = max(deadline, last + 2.0)
                except socket.timeout:
                    if chunks and time.time() - last > 1.5:
                        break
                    continue
            raw = b"".join(chunks)

        out: dict[str, Any] = {
            "cmd": int(Cmd.EXPORT_LOG),
            "len": len(raw),
            "ok": False,
            "declared_size": None,
            "gzip": None,
            "raw_path_hint": "raw starts with u32le + gzip",
        }
        if len(raw) >= 6 and raw[4:6] == b"\x1f\x8b":
            out["declared_size"] = struct.unpack_from("<I", raw, 0)[0]
            gz = raw[4:]
            out["gzip_len"] = len(gz)
            try:
                data = gzip.decompress(gz)
                out["ok"] = True
                out["data"] = data
                out["data_len"] = len(data)
            except Exception as e:
                out["gzip_error"] = f"{type(e).__name__}: {e}"
                out["gzip"] = gz  # possibly truncated
        elif raw[:4] == b"\x5a\x5a\x7f\x7f":
            # unexpected ZZ response
            out.update(NetPacket.decode_response(raw))
        else:
            out["raw_head"] = raw[:32].hex() if raw else ""
        return out

    # --- convenience snapshot ---

    def status(self) -> dict[str, Any]:
        """Compact status from get_info()."""
        info = self.get_info()
        keys = (
            "miner_type",
            "firmware",
            "power_mode",
            "power_limit",
            "api_switch",
            "power_on",
            "coin_type",
            "heat_mode",
            "fast_boot",
            "fast_mining",
            "web_pool",
            "upfreq_speed",
            "mac",
            "token",
        )
        out = {k: info.get(k) for k in keys}
        for rk in ("MHS av", "Power", "EnvTemp", "Uptime", "pool_url", "pool_user"):
            if rk in info.get("raw_keys", {}):
                out[rk] = info["raw_keys"][rk]
        return out

    # --- probes ---

    def probe(self, cmdcode: int = 0) -> dict[str, Any]:
        pkt = NetPacket(cmdcode=cmdcode).encode(pad=False)
        return NetPacket.decode_response(self.exchange_raw(pkt, wait=1.5, shutdown=True))

    def ping(self) -> bool:
        try:
            r = self.probe(0)
            return r.get("len", 0) >= 16 or r.get("status") is not None
        except OSError:
            return False

    @staticmethod
    def list_commands() -> list[dict[str, Any]]:
        """Return documented cmd table for tooling/CLI."""
        return [
            {"cmd": int(c), "name": c.name, "doc": (c.__doc__ or "").strip()}
            for c in Cmd
        ]

    @staticmethod
    def list_params() -> list[dict[str, Any]]:
        out = []
        for pid, info in sorted(PARAM_INFO.items()):
            out.append({"id": pid, **info})
        return out


def demo(host: str = "192.168.1.10") -> None:
    c = NetPacketClient(host)
    print("ping", c.ping())
    print("token", c.handshake())
    print("status", c.status())


if __name__ == "__main__":
    demo()
