"""
Per-model miner profiles: manufacturer, cooling, boards, chip layout, sensors.

Extensible for multiple vendors (MicroBT Whatsminer today; Bitmain planned).

SKU-level chipmap (cpd + hydro slot_link) lives in chipmap_skus.json
(HashSource/whatsminer_chip_map). Family profiles supply boards/cooling/sensors
when SKU is unknown or only a family prefix is known (e.g. M63_VK2A → M63).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent

# ── Manufacturers ────────────────────────────────────────────────────────────

MANUFACTURERS: dict[str, dict[str, Any]] = {
    "microbt": {
        "id": "microbt",
        "name": "MicroBT",
        "brand": "Whatsminer",
        "api_vendors": ["whatsminer"],
        "support": "full",
    },
    "bitmain": {
        "id": "bitmain",
        "name": "Bitmain",
        "brand": "Antminer",
        "api_vendors": ["bitmain"],
        "support": "planned",
        "note": "API / chipmap not implemented yet — profile stubs only.",
    },
}

# Canonical mining algorithms (id stored in miners.db / live).
# Default meter units (hashrate + efficiency) are per-algo, not per-model.
ALGO_INFO: dict[str, dict[str, str]] = {
    "sha256": {
        "id": "sha256",
        "name": "SHA-256",
        "name_ru": "SHA-256",
        "coin": "BTC",
        "hashrate_unit": "TH/s",
        "efficiency_unit": "J/T",
    },
    "scrypt": {
        "id": "scrypt",
        "name": "Scrypt",
        "name_ru": "Scrypt",
        "coin": "LTC",
        "hashrate_unit": "GH/s",
        "efficiency_unit": "J/G",
    },
    "eaglesong": {
        "id": "eaglesong",
        "name": "Eaglesong",
        "name_ru": "Eaglesong",
        "coin": "CKB",
        "hashrate_unit": "TH/s",
        "efficiency_unit": "J/T",
    },
    "ethash": {
        "id": "ethash",
        "name": "Ethash",
        "name_ru": "Ethash",
        "coin": "ETC",
        "hashrate_unit": "MH/s",
        "efficiency_unit": "J/M",
    },
    "kheavyhash": {
        "id": "kheavyhash",
        "name": "kHeavyHash",
        "name_ru": "kHeavyHash",
        "coin": "KAS",
        "hashrate_unit": "TH/s",
        "efficiency_unit": "J/T",
    },
    "blake2s": {
        "id": "blake2s",
        "name": "Blake2s",
        "name_ru": "Blake2s",
        "coin": "KDA",
        "hashrate_unit": "TH/s",
        "efficiency_unit": "J/T",
    },
    "handshake": {
        "id": "handshake",
        "name": "Handshake",
        "name_ru": "Handshake",
        "coin": "HNS",
        "hashrate_unit": "TH/s",
        "efficiency_unit": "J/T",
    },
    "equihash": {
        "id": "equihash",
        "name": "Equihash",
        "name_ru": "Equihash",
        "coin": "ZEC",
        "hashrate_unit": "MH/s",
        "efficiency_unit": "J/M",
    },
    "x11": {
        "id": "x11",
        "name": "X11",
        "name_ru": "X11",
        "coin": "DASH",
        "hashrate_unit": "GH/s",
        "efficiency_unit": "J/G",
    },
}

_VENDOR_DEFAULT_ALGO: dict[str, str] = {
    "microbt": "sha256",
    "whatsminer": "sha256",
    "bitmain": "sha256",
    "antminer": "sha256",
    "avalon": "sha256",
    "canaan": "sha256",
}


def normalize_algo(raw: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "", str(raw or "").strip().lower())
    aliases = {
        "sha2": "sha256",
        "sha256d": "sha256",
        "btc": "sha256",
        "bitcoin": "sha256",
        "ltc": "scrypt",
        "litecoin": "scrypt",
        "ckb": "eaglesong",
        "nervos": "eaglesong",
        "etc": "ethash",
        "etchash": "ethash",
        "ethash": "ethash",
        "etcethash": "ethash",
        "kas": "kheavyhash",
        "kaspa": "kheavyhash",
        "heavyhash": "kheavyhash",
    }
    s = aliases.get(s, s)
    return s


def algo_info(raw: Any) -> dict[str, str] | None:
    aid = normalize_algo(raw)
    if not aid:
        return None
    known = ALGO_INFO.get(aid)
    if known:
        return dict(known)
    # unknown but non-empty — keep as free-form id, BTC-class units
    label = str(raw or aid).strip() or aid
    return {
        "id": aid,
        "name": label,
        "name_ru": label,
        "coin": "",
        "hashrate_unit": "TH/s",
        "efficiency_unit": "J/T",
    }


def algo_display(raw: Any, *, lang: str = "en") -> str:
    info = algo_info(raw)
    if not info:
        return ""
    if str(lang).startswith("ru"):
        return info.get("name_ru") or info.get("name") or info["id"]
    return info.get("name") or info["id"]


def list_algos() -> list[dict[str, str]]:
    return [dict(v) for v in ALGO_INFO.values()]


def _eff_unit_key(u: str) -> str:
    s = re.sub(r"[^a-z/]", "", str(u or "").lower())
    if s in ("j/g", "j/gh"):
        return "j/g"
    if s in ("j/m", "j/mh"):
        return "j/m"
    return "j/t"


# How many J/T equal one unit of this efficiency label.
_JT_PER_UNIT = {"j/t": 1.0, "j/g": 1000.0, "j/m": 1_000_000.0}


def convert_efficiency(value: float, from_unit: str, to_unit: str) -> float:
    """Convert J/T ↔ J/G ↔ J/M. 205 J/T == 0.205 J/G == 0.000205 J/M."""
    src = _JT_PER_UNIT[_eff_unit_key(from_unit)]
    dst = _JT_PER_UNIT[_eff_unit_key(to_unit)]
    return float(value) * src / dst


def apply_algo_meta(dst: dict[str, Any], ainfo: dict[str, Any] | None) -> None:
    """Stamp algo id/name/coin and default meter units onto live or inventory."""
    if not isinstance(dst, dict) or not ainfo or not ainfo.get("id"):
        return
    dst["algo"] = ainfo["id"]
    dst["algo_display"] = ainfo.get("name") or ainfo["id"]
    if ainfo.get("coin") and not dst.get("coin"):
        dst["coin"] = ainfo["coin"]
    if ainfo.get("hashrate_unit"):
        dst["hashrate_unit"] = ainfo["hashrate_unit"]
    if ainfo.get("efficiency_unit"):
        dst["efficiency_unit"] = ainfo["efficiency_unit"]


def efficiency_from_power(
    power_w: float,
    hashrate_th: float,
    ainfo: dict[str, Any] | None,
) -> tuple[float | None, str]:
    unit = str((ainfo or {}).get("efficiency_unit") or "J/T")
    try:
        p = float(power_w)
        th = float(hashrate_th)
    except (TypeError, ValueError):
        return None, unit
    if p <= 0 or th <= 0:
        return None, unit
    val = convert_efficiency(p / th, "J/T", unit)
    key = _eff_unit_key(unit)
    digits = 1 if key == "j/t" else 3
    return round(val, digits), unit

# ── Family profiles (prefix match after normalize, longest first) ────────────
# id: manufacturer.family_key

# Whatsminer / Antminer stock: chassis-integrated smart PSU (API power + control).
_PSU_INTEGRATED_SMART = {
    "type": "integrated",
    "smart": True,
    "reports_power": True,
    "reports_temp": True,
    "controllable": True,
    "note": "Built-in smart PSU (power read + power/mode control via miner API)",
}

_FAMILIES: list[dict[str, Any]] = [
    # ── MicroBT liquid (hydro / immersion-style dual virtual slots) ─────────
    {
        "id": "microbt.m66",
        "manufacturer": "microbt",
        "family": "M66",
        "match": ["M66"],
        "cooling": "liquid",
        "boards": {
            "count": 4,
            "physical": 2,
            "virtual_per_physical": 2,
            "chart_slots": [0, 2],
            "slot_link": "0:1 2:3",
            "note": "4 slots · paired sensors (hydro)",
        },
        "chip_layout": {
            "style": "hydro",
            "chips_per_domain_default": 4,
            "chips_per_board_typical": 264,
        },
        "sensors": {
            "power": True,
            "liquid_temp": True,
            "env_temp": True,
            "chip_temp": True,  # summary Chip Temp Min/Avg/Max
            "pcb_temp": True,  # per-slot PCB (SM0…)
            "board_chip_temp": False,  # per-slot chip min/avg/max from DEVS (often absent)
            "psu_temp": True,
        },
        "psu": dict(_PSU_INTEGRATED_SMART),
        "efficiency": {"j_per_th": 19.0, "j_per_th_low": 18.0, "j_per_th_high": 21.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m63",
        "manufacturer": "microbt",
        "family": "M63",
        "match": ["M63"],
        "cooling": "liquid",
        "boards": {
            "count": 4,
            "physical": 2,
            "virtual_per_physical": 2,
            "chart_slots": [0, 2],
            "slot_link": "0:1 2:3",
            # odd link pairs are mirror-oriented in chassis (for Outliers geometry)
            "board_mirror": "odd_pair",
            "note": "2 physical × 2 virtual slots · boards face each other (mirror)",
        },
        "chip_layout": {
            "style": "hydro",
            "chips_per_domain_default": 4,
            "chips_per_board_typical": 264,
            # pair 0 (slots 0:1) normal C#; pair 1 (2:3) reverse for cross-board compare
            "outlier_mirror_pairs": True,
        },
        "sensors": {
            "power": True,
            "liquid_temp": True,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,  # M63 DEVS usually no SM chip min/avg/max
            "psu_temp": True,
        },
        # Same class as Antminer L9: built-in smart PSU with API control
        "psu": dict(_PSU_INTEGRATED_SMART),
        "efficiency": {"j_per_th": 19.0, "j_per_th_low": 18.0, "j_per_th_high": 21.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m56",
        "manufacturer": "microbt",
        "family": "M56",
        "match": ["M56"],
        "cooling": "liquid",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": "0:1 2:3",
            "board_mirror": "odd_pair",
            "note": "3 hashboards · hydro · mirrored pairs for Outliers",
        },
        "chip_layout": {
            "style": "hydro",
            "chips_per_domain_default": 3,
            "outlier_mirror_pairs": True,
        },
        "sensors": {
            "liquid_temp": True,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 20.0, "j_per_th_low": 18.0, "j_per_th_high": 22.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m53",
        "manufacturer": "microbt",
        "family": "M53",
        "match": ["M53"],
        "cooling": "liquid",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": "0:1 2:3",
            "board_mirror": "odd_pair",
            "note": "3 hashboards · hydro · mirrored pairs for Outliers",
        },
        "chip_layout": {
            "style": "hydro",
            "chips_per_domain_default": 3,
            "outlier_mirror_pairs": True,
        },
        "sensors": {
            "liquid_temp": True,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 20.0, "j_per_th_low": 18.0, "j_per_th_high": 22.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    # ── MicroBT air ─────────────────────────────────────────────────────────
    {
        "id": "microbt.m60s",
        "manufacturer": "microbt",
        "family": "M60S",
        "match": ["M60S++", "M60S+", "M60S"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards",
        },
        "chip_layout": {
            "style": "snake",
            "chips_per_domain_default": 3,
        },
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 20.5, "j_per_th_low": 20.0, "j_per_th_high": 21.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m60",
        "manufacturer": "microbt",
        "family": "M60",
        "match": ["M60"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards",
        },
        "chip_layout": {
            "style": "snake",
            "chips_per_domain_default": 3,
        },
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 20.5, "j_per_th_low": 20.0, "j_per_th_high": 21.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m50s",
        "manufacturer": "microbt",
        "family": "M50S",
        "match": ["M50S++", "M50S+", "M50S"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards",
        },
        "chip_layout": {
            "style": "snake",
            "chips_per_domain_default": 3,
        },
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 26.0, "j_per_th_low": 24.0, "j_per_th_high": 29.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m50",
        "manufacturer": "microbt",
        "family": "M50",
        "match": ["M50"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards",
        },
        "chip_layout": {
            "style": "snake",
            "chips_per_domain_default": 3,
        },
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 29.0, "j_per_th_low": 26.0, "j_per_th_high": 32.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m33s",
        "manufacturer": "microbt",
        "family": "M33S",
        "match": ["M33S", "M33"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards",
        },
        "chip_layout": {"style": "snake", "chips_per_domain_default": 3},
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 38.0, "j_per_th_low": 34.0, "j_per_th_high": 42.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m30s",
        "manufacturer": "microbt",
        "family": "M30S",
        "match": ["M30S++", "M30S+", "M30S", "M30"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards",
        },
        "chip_layout": {"style": "snake", "chips_per_domain_default": 3},
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 38.0, "j_per_th_low": 34.0, "j_per_th_high": 42.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "v3", "netpacket", "luci"],
        },
    },
    {
        "id": "microbt.m21s",
        "manufacturer": "microbt",
        "family": "M21S",
        "match": ["M21S", "M21", "M20S", "M20"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards",
        },
        "chip_layout": {"style": "snake", "chips_per_domain_default": 3},
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 42.0, "j_per_th_low": 38.0, "j_per_th_high": 48.0},
        "api": {
            "vendor": "whatsminer",
            "protocols": ["v2", "netpacket", "luci"],
        },
    },
    # ── Bitmain stubs (future) ──────────────────────────────────────────────
    {
        "id": "bitmain.s21",
        "manufacturer": "bitmain",
        "family": "S21",
        "match": ["S21XP", "S21PRO", "S21+", "S21"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards (typical) · Bitmain support planned",
        },
        "chip_layout": {
            "style": "grid",
            "chips_per_domain_default": None,
            "note": "chip layout TBD when Bitmain API is added",
        },
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 15.0, "j_per_th_low": 13.0, "j_per_th_high": 17.0},
        "api": {
            "vendor": "bitmain",
            "protocols": [],
            "status": "planned",
        },
    },
    {
        "id": "bitmain.s19",
        "manufacturer": "bitmain",
        "family": "S19",
        "match": ["S19XP", "S19JPRO", "S19PRO", "S19J", "S19"],
        "cooling": "air",
        "boards": {
            "count": 3,
            "physical": 3,
            "virtual_per_physical": 1,
            "chart_slots": [0, 1, 2],
            "slot_link": None,
            "note": "3 hashboards (typical) · Bitmain support planned",
        },
        "chip_layout": {
            "style": "grid",
            "chips_per_domain_default": None,
            "note": "chip layout TBD when Bitmain API is added",
        },
        "sensors": {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,
            "psu_temp": True,
        },
        "efficiency": {"j_per_th": 29.5, "j_per_th_low": 27.0, "j_per_th_high": 34.0},
        "api": {
            "vendor": "bitmain",
            "protocols": [],
            "status": "planned",
        },
    },
]

# BTC-class families inherit SHA-256 unless a row sets its own algo/coin.
for _fam in _FAMILIES:
    _fam.setdefault("algo", "sha256")
    _fam.setdefault("coin", "BTC")


def normalize_miner_type(miner_type: Optional[str]) -> str:
    if not miner_type:
        return ""
    s = str(miner_type).strip().upper()
    s = s.replace("WHATSMINER", "").replace("ANTMINER", "")
    s = re.sub(r"[^A-Z0-9+]+", "", s)
    return s


def _family_match_keys() -> list[tuple[str, dict[str, Any]]]:
    """(prefix, family) sorted by prefix length desc."""
    out: list[tuple[str, dict[str, Any]]] = []
    for fam in _FAMILIES:
        for p in fam.get("match") or []:
            out.append((str(p).upper(), fam))
    out.sort(key=lambda x: len(x[0]), reverse=True)
    return out


@lru_cache(maxsize=1)
def _load_chipmap_skus() -> list[dict[str, Any]]:
    """SKU rows: {sku, cpd, slot_link} from chipmap_skus.json."""
    paths = [
        _HERE / "chipmap_skus.json",
        Path("/opt/lib/poolheat/chipmap_skus.json"),
        Path("/opt/share/poolheat/chipmap_skus.json"),
    ]
    for p in paths:
        try:
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    clean: list[dict[str, Any]] = []
                    for row in raw:
                        if not isinstance(row, dict):
                            continue
                        sku = normalize_miner_type(row.get("sku") or row.get("model"))
                        if not sku:
                            continue
                        try:
                            cpd = int(row.get("cpd") or 0)
                        except (TypeError, ValueError):
                            cpd = 0
                        link = row.get("slot_link") or row.get("link")
                        if link is not None:
                            link = str(link).strip() or None
                        clean.append({"sku": sku, "cpd": cpd, "slot_link": link})
                    # longest SKU first for contains-match
                    clean.sort(key=lambda r: len(r["sku"]), reverse=True)
                    return clean
        except Exception:
            continue
    return []


def lookup_chipmap_sku(miner_type: Optional[str]) -> Optional[dict[str, Any]]:
    """Best SKU chipmap match for model string (exact contains, then prefix)."""
    n = normalize_miner_type(miner_type)
    if not n:
        return None
    skus = _load_chipmap_skus()
    best = None
    best_len = 0
    for row in skus:
        m = row["sku"]
        if m and m in n and len(m) > best_len:
            best = row
            best_len = len(m)
    if best:
        return {
            "sku": best["sku"],
            "cpd": best["cpd"],
            "slot_link": best.get("slot_link"),
            "fuzzy": False,
        }
    # prefix series fallback
    for length in range(min(len(n), 12), 3, -1):
        pref = n[:length]
        for row in skus:
            if row["sku"].startswith(pref):
                return {
                    "sku": row["sku"],
                    "cpd": row["cpd"],
                    "slot_link": row.get("slot_link"),
                    "fuzzy": True,
                }
    return None


def lookup_family(miner_type: Optional[str]) -> Optional[dict[str, Any]]:
    n = normalize_miner_type(miner_type)
    if not n:
        return None
    for prefix, fam in _family_match_keys():
        if n == prefix or n.startswith(prefix):
            return fam
    return None


def _chart_slots_for_boards(n: int, preferred: list | None = None) -> list[int]:
    n = max(1, min(8, int(n)))
    pref = [int(x) for x in (preferred or []) if isinstance(x, (int, float))]
    pref = [x for x in pref if 0 <= x < n]
    if n >= 4:
        if pref == [0, 2] or (len(pref) == 2 and pref[0] == 0 and pref[-1] == 2):
            return [0, 2]
        if len(pref) >= 2:
            return pref[:3]
        return [0, 2]
    if n >= 3:
        return [0, 1, 2]
    if n >= 2:
        return [0, 1]
    return [0]


def resolve_miner_model(
    miner_type: Optional[str] = None,
    *,
    n_devs: int | None = None,
    board_num: int | None = None,
) -> dict[str, Any]:
    """
    Resolve full model profile for UI / live / chipmap.

    Live DEVS / board-num override board count when present.
    """
    raw = str(miner_type or "").strip()
    norm = normalize_miner_type(raw)
    fam = lookup_family(raw)
    sku = lookup_chipmap_sku(raw)

    mfr_id = (fam or {}).get("manufacturer") or "microbt"
    # crude manufacturer guess when family unknown
    if not fam:
        if norm.startswith("S") and any(norm.startswith(p) for p in ("S9", "S1", "S2", "S3")):
            mfr_id = "bitmain"
        elif norm.startswith("T") and len(norm) >= 2 and norm[1].isdigit():
            mfr_id = "bitmain"
    mfr = dict(MANUFACTURERS.get(mfr_id) or MANUFACTURERS["microbt"])

    boards_cfg: dict[str, Any]
    if fam and isinstance(fam.get("boards"), dict):
        boards_cfg = dict(fam["boards"])
    else:
        n_guess = 4
        if board_num and int(board_num) > 0:
            n_guess = int(board_num)
        elif n_devs and int(n_devs) > 0:
            n_guess = int(n_devs)
        n_guess = max(1, min(8, n_guess))
        boards_cfg = {
            "count": n_guess,
            "physical": n_guess,
            "virtual_per_physical": 1,
            "chart_slots": _chart_slots_for_boards(n_guess),
            "slot_link": None,
            "note": "auto from DEVS/board-num",
        }

    # Live counts win
    if n_devs is not None and int(n_devs) > 0:
        n = max(1, min(8, int(n_devs)))
        boards_cfg["count"] = n
        boards_cfg["chart_slots"] = _chart_slots_for_boards(n, boards_cfg.get("chart_slots"))
    elif board_num is not None and int(board_num) > 0:
        n = max(1, min(8, int(board_num)))
        boards_cfg["count"] = n
        boards_cfg["chart_slots"] = _chart_slots_for_boards(n, boards_cfg.get("chart_slots"))

    # Chip layout: SKU overrides family defaults
    chip_layout: dict[str, Any] = {}
    if fam and isinstance(fam.get("chip_layout"), dict):
        chip_layout = dict(fam["chip_layout"])
    cpd = chip_layout.get("chips_per_domain_default")
    slot_link = boards_cfg.get("slot_link")
    style = chip_layout.get("style") or "snake"
    if sku:
        if sku.get("cpd"):
            cpd = int(sku["cpd"])
        if sku.get("slot_link"):
            slot_link = sku["slot_link"]
        if slot_link:
            style = "hydro"
        chip_layout["sku"] = sku["sku"]
        chip_layout["sku_fuzzy"] = bool(sku.get("fuzzy"))
    chip_layout["chips_per_domain"] = cpd
    chip_layout["slot_link"] = slot_link
    chip_layout["style"] = style if style else ("hydro" if slot_link else "snake")

    sensors = dict((fam or {}).get("sensors") or {})
    if not sensors:
        sensors = {
            "liquid_temp": False,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "psu_temp": True,
        }
    # hydro style implies liquid sensor expected for MicroBT
    if chip_layout.get("style") == "hydro" and "liquid_temp" not in sensors:
        sensors["liquid_temp"] = True

    # PSU: family override → manufacturer default (Whatsminer/Bitmain = integrated smart)
    psu = dict((fam or {}).get("psu") or {})
    if not psu:
        psu = _default_psu_for_manufacturer(str(mfr.get("id") or ""))
    if psu.get("reports_power") and "power" not in sensors:
        sensors["power"] = True
    if psu.get("reports_temp") and "psu_temp" not in sensors:
        sensors["psu_temp"] = True

    cooling = (fam or {}).get("cooling") or (
        "liquid" if chip_layout.get("style") == "hydro" else "air"
    )
    algo_id = normalize_algo((fam or {}).get("algo")) or _VENDOR_DEFAULT_ALGO.get(
        str(mfr.get("id") or ""), ""
    )
    coin = str((fam or {}).get("coin") or "").strip()
    if not coin:
        coin = (algo_info(algo_id) or {}).get("coin") or ""
    efficiency = dict((fam or {}).get("efficiency") or {})
    api = dict((fam or {}).get("api") or {"vendor": mfr.get("api_vendors", ["whatsminer"])[0]})

    return {
        "ok": True,
        "miner_type": raw or None,
        "miner_type_norm": norm or None,
        "manufacturer": mfr,
        "manufacturer_id": mfr.get("id"),
        "family_id": (fam or {}).get("id"),
        "family": (fam or {}).get("family"),
        "cooling": cooling,
        "algo": algo_id or None,
        "algo_display": algo_display(algo_id) or None,
        "coin": coin or None,
        "hashrate_unit": (algo_info(algo_id) or {}).get("hashrate_unit"),
        "efficiency_unit": (algo_info(algo_id) or {}).get("efficiency_unit"),
        "boards": boards_cfg,
        "chip_layout": chip_layout,
        "sensors": sensors,
        "psu": psu,
        "efficiency": efficiency,
        "api": api,
        "matched": bool(fam or sku),
        "sku": (sku or {}).get("sku"),
        # backward-compat aliases used by older live/chart code
        "board_count": int(boards_cfg.get("count") or 4),
        "board_chart_slots": list(boards_cfg.get("chart_slots") or [0, 2]),
        "board_layout_key": (fam or {}).get("family") or ("auto" if not fam else None),
        "board_layout_note": boards_cfg.get("note"),
    }


def _default_psu_for_manufacturer(mfr_id: str) -> dict[str, Any]:
    """Built-in smart PSU for MicroBT Whatsminer and Bitmain Antminer stock."""
    mid = str(mfr_id or "").strip().lower()
    if mid in ("microbt", "whatsminer", "bitmain", "antminer"):
        return dict(_PSU_INTEGRATED_SMART)
    return {
        "type": "external",
        "smart": False,
        "reports_power": False,
        "reports_temp": False,
        "controllable": False,
    }


def resolve_hashboard_layout(
    miner_type: str | None = None,
    *,
    n_devs: int | None = None,
    board_num: int | None = None,
) -> dict:
    """
    Compatibility wrapper for serve.py resolve_hashboard_layout callers.
    """
    p = resolve_miner_model(miner_type, n_devs=n_devs, board_num=board_num)
    boards = p.get("boards") or {}
    return {
        "boards": int(boards.get("count") or p.get("board_count") or 4),
        "chart": list(boards.get("chart_slots") or p.get("board_chart_slots") or [0, 2]),
        "model_key": p.get("board_layout_key") or p.get("family") or "auto",
        "note": boards.get("note") or p.get("board_layout_note"),
        "profile": p,
    }


def list_manufacturers() -> list[dict[str, Any]]:
    return [dict(v) for v in MANUFACTURERS.values()]


def list_families(*, manufacturer: str | None = None) -> list[dict[str, Any]]:
    out = []
    for fam in _FAMILIES:
        if manufacturer and fam.get("manufacturer") != manufacturer:
            continue
        mfr = MANUFACTURERS.get(fam.get("manufacturer") or "", {})
        out.append(
            {
                "id": fam.get("id"),
                "family": fam.get("family"),
                "manufacturer": fam.get("manufacturer"),
                "manufacturer_name": mfr.get("name"),
                "brand": mfr.get("brand"),
                "cooling": fam.get("cooling"),
                "algo": fam.get("algo"),
                "algo_display": algo_display(fam.get("algo")),
                "coin": fam.get("coin"),
                "hashrate_unit": (algo_info(fam.get("algo")) or {}).get("hashrate_unit"),
                "efficiency_unit": (algo_info(fam.get("algo")) or {}).get(
                    "efficiency_unit"
                ),
                "boards": fam.get("boards"),
                "chip_layout": fam.get("chip_layout"),
                "sensors": fam.get("sensors"),
                "psu": fam.get("psu")
                or _default_psu_for_manufacturer(str(fam.get("manufacturer") or "")),
                "efficiency": fam.get("efficiency"),
                "api": fam.get("api"),
                "match": list(fam.get("match") or []),
                "support": mfr.get("support"),
            }
        )
    return out


# ── Multi-vendor model profiles (display name · power estimate · flags) ─────
# Editable JSON next to this module / on Entware under /opt/lib/poolheat/

_PROFILE_FILE_CANDIDATES = (
    _HERE / "miner_model_profiles.json",
    Path("/opt/lib/poolheat/miner_model_profiles.json"),
    Path("/opt/share/poolheat/miner_model_profiles.json"),
)

_profiles_cache: list[dict[str, Any]] | None = None


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").strip().lower())


def load_model_profiles(*, force: bool = False) -> list[dict[str, Any]]:
    """Load miner_model_profiles.json (list of model dicts)."""
    global _profiles_cache
    if _profiles_cache is not None and not force:
        return _profiles_cache
    for p in _PROFILE_FILE_CANDIDATES:
        try:
            if not p.is_file():
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
            models = raw.get("models") if isinstance(raw, dict) else raw
            if isinstance(models, list):
                _profiles_cache = [m for m in models if isinstance(m, dict)]
                return _profiles_cache
        except Exception:
            continue
    _profiles_cache = []
    return _profiles_cache


def lookup_model_profile(
    *,
    vendor: str | None = None,
    miner_type: str | None = None,
    model: str | None = None,
    model_name: str | None = None,
    model_code: str | None = None,
) -> dict[str, Any] | None:
    """
    Match free-form ASIC identity strings to a profile.

    Match keys are normalized (lowercase, strip non-alnum). Longest match wins.
    """
    candidates = [
        str(x)
        for x in (model_name, model, model_code, miner_type)
        if x is not None and str(x).strip()
    ]
    vendor_n = _norm_key(vendor or "")
    best: dict[str, Any] | None = None
    best_len = -1
    for prof in load_model_profiles():
        if not isinstance(prof, dict):
            continue
        p_vendor = _norm_key(str(prof.get("vendor") or ""))
        if vendor_n and p_vendor and vendor_n != p_vendor:
            # still allow match if identity string contains vendor brand
            pass
        matches = prof.get("match") if isinstance(prof.get("match"), list) else []
        keys = [_norm_key(str(m)) for m in matches if str(m).strip()]
        # also match on display names / id
        for extra in (
            prof.get("id"),
            prof.get("display_name"),
            prof.get("display_name_full"),
        ):
            k = _norm_key(str(extra or ""))
            if k:
                keys.append(k)
        keys = sorted(set(k for k in keys if k), key=len, reverse=True)
        for cand in candidates:
            cn = _norm_key(cand)
            if not cn:
                continue
            for k in keys:
                if not k:
                    continue
                # exact or substring either way
                if cn == k or k in cn or cn in k:
                    # vendor preference: if both have vendor and mismatch, skip weak
                    if (
                        vendor_n
                        and p_vendor
                        and vendor_n != p_vendor
                        and k not in cn
                        and cn not in k
                    ):
                        continue
                    if len(k) > best_len:
                        best = prof
                        best_len = len(k)
    return dict(best) if best else None


def resolve_miner_algo(
    *,
    vendor: str | None = None,
    miner_type: str | None = None,
    model: str | None = None,
    model_name: str | None = None,
    model_code: str | None = None,
    explicit: str | None = None,
) -> dict[str, str]:
    """
    Per-model algorithm: JSON profile → family profile → vendor default.

    ``explicit`` (already stored on the miner) wins when set.
    Returns {id, name, name_ru, coin} or empty id.
    """
    if explicit and str(explicit).strip():
        info = algo_info(explicit)
        if info:
            return info
    prof = lookup_model_profile(
        vendor=vendor,
        miner_type=miner_type,
        model=model,
        model_name=model_name,
        model_code=model_code,
    )
    if isinstance(prof, dict) and (prof.get("algo") or prof.get("algorithm")):
        info = algo_info(prof.get("algo") or prof.get("algorithm"))
        if info:
            if not info.get("coin") and prof.get("coin"):
                info["coin"] = str(prof.get("coin") or "")
            return info
    fam = lookup_family(miner_type or model_code or model or model_name)
    if isinstance(fam, dict) and fam.get("algo"):
        info = algo_info(fam.get("algo"))
        if info:
            if not info.get("coin") and fam.get("coin"):
                info["coin"] = str(fam.get("coin") or "")
            return info
    vend = _norm_key(vendor or "")
    default = _VENDOR_DEFAULT_ALGO.get(vend, "")
    info = algo_info(default) if default else None
    return info or {"id": "", "name": "", "name_ru": "", "coin": ""}


def estimate_power_from_profile(
    profile: dict[str, Any] | None,
    *,
    hashrate_hs: float | None = None,
    hashrate_th: float | None = None,
    hashrate_gh: float | None = None,
    hashrate_mhs: float | None = None,
) -> dict[str, Any] | None:
    """
    Estimate wall power (W) from hashrate × model efficiency.

    Supports:
      - efficiency.j_per_th  (BTC-class)  → W = TH/s × J/TH
      - efficiency.j_per_gh  (CKB etc.)   → W = GH/s × J/GH
      - efficiency.j_per_mh  (ETC etc.)   → W = MH/s × J/MH

    Returns None when profile forbids estimate or hashrate missing.
    """
    if not isinstance(profile, dict):
        return None
    pwr = profile.get("power") if isinstance(profile.get("power"), dict) else {}
    if pwr.get("estimate_from_efficiency") is False:
        return None
    # if reported=true and estimate not forced, caller may still skip
    eff = profile.get("efficiency") if isinstance(profile.get("efficiency"), dict) else {}
    if not eff:
        return None

    hs = float(hashrate_hs) if hashrate_hs is not None else None
    th = float(hashrate_th) if hashrate_th is not None else None
    gh = float(hashrate_gh) if hashrate_gh is not None else None
    mh = float(hashrate_mhs) if hashrate_mhs is not None else None
    if hs is not None and hs > 0:
        if th is None:
            th = hs / 1e12
        if gh is None:
            gh = hs / 1e9
        if mh is None:
            mh = hs / 1e6

    j_th = eff.get("j_per_th")
    j_gh = eff.get("j_per_gh")
    j_mh = eff.get("j_per_mh")
    watts: float | None = None
    unit = None
    j_used = None
    rate_used = None

    if j_gh is not None and gh is not None and gh > 0:
        j_used = float(j_gh)
        rate_used = gh
        unit = "J/G"
        watts = gh * j_used
    elif j_mh is not None and mh is not None and mh > 0:
        j_used = float(j_mh)
        rate_used = mh
        unit = "J/M"
        watts = mh * j_used
    elif j_th is not None and th is not None and th > 0:
        j_used = float(j_th)
        rate_used = th
        unit = "J/T"
        watts = th * j_used
    else:
        return None

    if watts is None or watts <= 0:
        return None
    return {
        "power_w": round(watts, 1),
        "efficiency_value": j_used,
        "efficiency_unit": unit,
        "hashrate_for_eff": rate_used,
        "source": "model",
        "profile_id": profile.get("id"),
        "display_name": profile.get("display_name") or profile.get("display_name_full"),
        "power_reported": bool(pwr.get("reported", True)),
        "note": pwr.get("note") or eff.get("note"),
    }


def normalize_hardware_map(prof: dict[str, Any] | None) -> dict[str, Any]:
    """
    Canonical per-model hardware map for UI + collectors.

    Prefer explicit ``hardware`` block; fall back to coarse ``sensors`` flags.
    """
    if not isinstance(prof, dict):
        return {}
    hw = prof.get("hardware") if isinstance(prof.get("hardware"), dict) else {}
    sens = prof.get("sensors") if isinstance(prof.get("sensors"), dict) else {}
    poll = prof.get("poll") if isinstance(prof.get("poll"), dict) else {}

    boards = hw.get("hashboards") if isinstance(hw.get("hashboards"), dict) else {}
    fans = hw.get("fans") if isinstance(hw.get("fans"), dict) else {}
    temps = hw.get("temps") if isinstance(hw.get("temps"), dict) else {}
    psu = hw.get("psu") if isinstance(hw.get("psu"), dict) else {}
    ctrl = hw.get("controller") if isinstance(hw.get("controller"), dict) else {}

    def _count(block: Any, fallback: int = 0) -> int:
        if isinstance(block, dict):
            try:
                return max(0, int(block.get("count") or 0))
            except (TypeError, ValueError):
                return fallback
        return fallback

    hb_count = _count(boards)
    if not hb_count:
        try:
            hb_count = int(sens.get("hashboard_count") or 0)
        except (TypeError, ValueError):
            hb_count = 0
    fan_count = _count(fans)
    if not fan_count and sens.get("fans"):
        try:
            fan_count = int(sens.get("fan_count") or 0)
        except (TypeError, ValueError):
            fan_count = 0

    chip_t = temps.get("chip") if isinstance(temps.get("chip"), dict) else {}
    board_t = temps.get("board") if isinstance(temps.get("board"), dict) else {}
    env_t = temps.get("env") if isinstance(temps.get("env"), dict) else {}
    psu_t = temps.get("psu") if isinstance(temps.get("psu"), dict) else {}
    asc_t = temps.get("asc") if isinstance(temps.get("asc"), dict) else {}

    chip_n = _count(chip_t)
    if not chip_n and sens.get("chip_temp"):
        try:
            chip_n = int(sens.get("temp_chip_count") or 1)
        except (TypeError, ValueError):
            chip_n = 1
    pcb_n = _count(board_t)
    if not pcb_n and sens.get("pcb_temp"):
        try:
            pcb_n = int(sens.get("temp_pcb_count") or hb_count or 0)
        except (TypeError, ValueError):
            pcb_n = 0

    psu_type = str(psu.get("type") or "").strip().lower()
    if not psu_type:
        # coarse guess from sensors.power / reported
        pwr = prof.get("power") if isinstance(prof.get("power"), dict) else {}
        if psu.get("smart") or sens.get("power") or pwr.get("reported"):
            psu_type = "integrated"
        else:
            psu_type = "external"
    if psu_type not in ("integrated", "external", "external_smart"):
        psu_type = "external"

    fan_channels = []
    if isinstance(fans.get("channels"), list):
        for ch in fans["channels"]:
            if isinstance(ch, dict):
                fan_channels.append(dict(ch))

    out: dict[str, Any] = {
        "profile_id": prof.get("id"),
        "vendor": prof.get("vendor"),
        "display_name": prof.get("display_name") or prof.get("display_name_full"),
        "cooling": prof.get("cooling") or "air",
        "hashboards": {
            "count": hb_count or 1,
            "physical": int(boards.get("physical") or hb_count or 1),
            "label_prefix": boards.get("label_prefix") or "HB",
            "index_base": int(boards.get("index_base") if boards.get("index_base") is not None else 0),
            "serial_keys": list(boards.get("serial_keys") or []),
            "note": boards.get("note"),
        },
        "fans": {
            "count": fan_count,
            "smart": bool(fans.get("smart", False)),
            "channels": fan_channels,
            "note": fans.get("note"),
        },
        "temps": {
            "chip": {
                "count": chip_n,
                "api_keys": list(chip_t.get("api_keys") or []),
                "unit": chip_t.get("unit") or "C",
                "placeholder": chip_t.get("placeholder"),
                "per_board": bool(chip_t.get("per_board", False)),
                "note": chip_t.get("note"),
            },
            "board": {
                "count": pcb_n,
                "api_keys": list(board_t.get("api_keys") or []),
                "unit": board_t.get("unit") or "C",
                "per_board": bool(board_t.get("per_board", True)),
                "note": board_t.get("note"),
            },
            "env": {
                "count": _count(env_t) if env_t else (1 if sens.get("env_temp") else 0),
                "api_keys": list(env_t.get("api_keys") or []) if env_t else [],
            },
            "psu": {
                "count": _count(psu_t) if psu_t else (1 if sens.get("psu_temp") else 0),
                "api_keys": list(psu_t.get("api_keys") or []) if psu_t else [],
            },
            "asc": {
                "count": _count(asc_t),
                "api_keys": list(asc_t.get("api_keys") or []) if asc_t else [],
                "placeholder": asc_t.get("placeholder") if asc_t else None,
                "note": asc_t.get("note") if asc_t else None,
            },
        },
        "psu": {
            "type": psu_type,
            "smart": bool(psu.get("smart", psu_type in ("integrated", "external_smart"))),
            "reports_power": bool(
                psu.get("reports_power")
                if psu.get("reports_power") is not None
                else sens.get("power")
            ),
            "reports_temp": bool(
                psu.get("reports_temp")
                if psu.get("reports_temp") is not None
                else sens.get("psu_temp")
            ),
            "controllable": bool(
                psu.get("controllable")
                if psu.get("controllable") is not None
                else psu.get("smart", psu_type in ("integrated", "external_smart"))
            ),
            "note": psu.get("note"),
        },
        "controller": dict(ctrl) if ctrl else {},
        "poll": {
            "sources": list(poll.get("sources") or []),
            "temps_from": list(poll.get("temps_from") or [])
            if isinstance(poll.get("temps_from"), list)
            else ([poll["temps_from"]] if poll.get("temps_from") else []),
            "fans_from": list(poll.get("fans_from") or [])
            if isinstance(poll.get("fans_from"), list)
            else ([poll["fans_from"]] if poll.get("fans_from") else []),
            "power_from": list(poll.get("power_from") or [])
            if isinstance(poll.get("power_from"), list)
            else ([poll["power_from"]] if poll.get("power_from") else []),
            "ignore_keys": dict(poll.get("ignore_keys") or {})
            if isinstance(poll.get("ignore_keys"), dict)
            else {},
        },
        # flat flags for quick UI checks
        "sensors": {
            "power": bool(sens.get("power") or psu.get("reports_power")),
            "fans": bool(sens.get("fans") if sens.get("fans") is not None else fan_count > 0),
            "chip_temp": bool(
                sens.get("chip_temp") if sens.get("chip_temp") is not None else chip_n > 0
            ),
            "pcb_temp": bool(
                sens.get("pcb_temp") if sens.get("pcb_temp") is not None else pcb_n > 0
            ),
            "env_temp": bool(sens.get("env_temp") or _count(env_t)),
            "psu_temp": bool(sens.get("psu_temp") or psu.get("reports_temp")),
            "fan_count": fan_count,
            "temp_chip_count": chip_n,
            "temp_pcb_count": pcb_n,
            "hashboard_count": hb_count or 1,
        },
    }
    return out


def profile_temp_keys(prof: dict[str, Any] | None) -> list[str]:
    """API keys to read for chip temps (excluding ignore list / placeholders-only)."""
    hw = normalize_hardware_map(prof)
    keys = list((hw.get("temps") or {}).get("chip", {}).get("api_keys") or [])
    ignore = ((hw.get("poll") or {}).get("ignore_keys") or {}).get("temps") or []
    ignore_set = {str(x) for x in ignore}
    return [k for k in keys if k and k not in ignore_set]


def profile_fan_keys(prof: dict[str, Any] | None) -> list[str]:
    """API keys to read for physical fans (ordered)."""
    hw = normalize_hardware_map(prof)
    keys: list[str] = []
    for ch in (hw.get("fans") or {}).get("channels") or []:
        if not isinstance(ch, dict):
            continue
        for k in ch.get("api_keys") or []:
            if k and k not in keys:
                keys.append(str(k))
    ignore = ((hw.get("poll") or {}).get("ignore_keys") or {}).get("fans") or []
    ignore_set = {str(x) for x in ignore}
    return [k for k in keys if k not in ignore_set]


def profile_hashboard_count(prof: dict[str, Any] | None) -> int:
    hw = normalize_hardware_map(prof)
    try:
        return max(1, int((hw.get("hashboards") or {}).get("count") or 1))
    except (TypeError, ValueError):
        return 1


def apply_model_profile_to_live(
    live: dict[str, Any],
    *,
    vendor: str | None = None,
) -> dict[str, Any]:
    """
    Enrich a live snapshot with profile display name + estimated power.

    Does not overwrite a real metered ``power`` when present and > 0.
    Sets:
      model_display, model_display_full, model_profile_id
      model_hardware (boards/fans/temps/psu map)
      power (if estimated), power_source, power_estimated
      efficiency_value / efficiency_unit / efficiency_jth (compat when J/TH)
    """
    if not isinstance(live, dict):
        return live
    vend = vendor or live.get("vendor") or live.get("api_vendor")
    prof = lookup_model_profile(
        vendor=str(vend) if vend else None,
        miner_type=live.get("miner_type"),
        model=live.get("model") if isinstance(live.get("model"), str) else None,
        model_name=live.get("model_name"),
        model_code=live.get("model_code"),
    )
    if not prof:
        _apply_algo_when_no_profile(live, str(vend) if vend else None)
        _stamp_family_psu_to_live(live)
        return live

    disp = str(prof.get("display_name") or "").strip()
    disp_full = str(prof.get("display_name_full") or disp).strip()
    if disp:
        live["model_display"] = disp
        live["model_display_full"] = disp_full or disp
        # Prefer trade name for UI model field when raw is ugly (CKBox, etc.)
        raw_model = str(live.get("model_name") or live.get("model") or "").strip()
        if not raw_model or _norm_key(raw_model) != _norm_key(disp):
            live["model_name"] = disp_full or disp
        live["model_profile_id"] = prof.get("id")

    # Hardware / sensor map (what the model has vs what we poll)
    hw_map = normalize_hardware_map(prof)
    if hw_map:
        live["model_hardware"] = hw_map
        live["model_sensors"] = hw_map.get("sensors") or {}
        # Expected component counts for UI (do not invent readings)
        try:
            live["expected_boards"] = int(
                (hw_map.get("hashboards") or {}).get("count") or 0
            ) or None
        except (TypeError, ValueError):
            pass
        try:
            live["expected_fans"] = int((hw_map.get("fans") or {}).get("count") or 0) or None
        except (TypeError, ValueError):
            pass
        try:
            live["expected_temp_sensors"] = int(
                ((hw_map.get("temps") or {}).get("chip") or {}).get("count") or 0
            ) or None
        except (TypeError, ValueError):
            pass
        psu = hw_map.get("psu") or {}
        live["psu_type"] = psu.get("type")
        live["psu_smart"] = bool(psu.get("smart"))
        live["psu_reports_power"] = bool(psu.get("reports_power"))
        live["psu_controllable"] = bool(psu.get("controllable", psu.get("smart")))
    # Family fallback for Whatsminer M63 etc. when JSON profile has no hardware.psu
    if not live.get("psu_type"):
        _stamp_family_psu_to_live(live)

    pwr_cfg = prof.get("power") if isinstance(prof.get("power"), dict) else {}
    reported_flag = pwr_cfg.get("reported")
    live["power_reported"] = (
        bool(reported_flag) if reported_flag is not None else True
    )

    ainfo = resolve_miner_algo(
        vendor=str(vend) if vend else None,
        miner_type=live.get("miner_type"),
        model=live.get("model") if isinstance(live.get("model"), str) else None,
        model_name=live.get("model_name"),
        model_code=live.get("model_code"),
        explicit=live.get("algo") or live.get("algorithm"),
    )
    apply_algo_meta(live, ainfo)
    if isinstance(prof, dict) and prof.get("coin") and not live.get("coin"):
        live["coin"] = str(prof.get("coin") or "")
    # Profile may override hashrate unit only when algo has no default
    if prof.get("hashrate_unit") and not live.get("hashrate_unit"):
        live["hashrate_unit"] = prof.get("hashrate_unit")

    # Estimate power when missing / not reported by ASIC
    try:
        cur_p = live.get("power")
        cur_pf = float(cur_p) if cur_p is not None and cur_p != "" else None
    except (TypeError, ValueError):
        cur_pf = None

    src_now = str(live.get("power_source") or "").lower()
    already_est = live.get("power_estimated") is True or src_now in (
        "model",
        "estimate",
        "estimated",
        "profile",
    )
    need_est = cur_pf is None or cur_pf <= 0
    # Models that omit wall power on :4028 still get a model estimate — but
    # never clobber a real meter (Antminer :6060/miner_power, PSU, etc.).
    metered_src = src_now in (
        "antminer_6060",
        "6060",
        "meter",
        "psu",
        "summary",
        "api",
    )
    if reported_flag is False and not metered_src and (cur_pf is None or cur_pf <= 0):
        need_est = True
    if already_est and not metered_src:
        # second apply() must not re-label a model estimate as metered
        live["power_estimated"] = True
        live["power_source"] = src_now or "model"
        need_est = False
    if need_est and pwr_cfg.get("estimate_from_efficiency", True):
        try:
            hs = float(live["hashrate_hs"]) if live.get("hashrate_hs") is not None else None
        except (TypeError, ValueError):
            hs = None
        try:
            th = float(live["hashrate_th"]) if live.get("hashrate_th") is not None else None
        except (TypeError, ValueError):
            th = None
        est = estimate_power_from_profile(prof, hashrate_hs=hs, hashrate_th=th)
        if est:
            live["power"] = est["power_w"]
            live["power_source"] = "model"
            live["power_estimated"] = True
            want = str((ainfo or {}).get("efficiency_unit") or est["efficiency_unit"])
            live["efficiency_value"] = round(
                convert_efficiency(
                    float(est["efficiency_value"]), est["efficiency_unit"], want
                ),
                1 if _eff_unit_key(want) == "j/t" else 3,
            )
            live["efficiency_unit"] = want
            live["efficiency_jth"] = convert_efficiency(
                float(est["efficiency_value"]), est["efficiency_unit"], "J/T"
            )
            live["efficiency_note"] = est.get("note")
    elif cur_pf is not None and cur_pf > 0:
        if already_est and not metered_src:
            live["power_estimated"] = True
            live["power_source"] = src_now or "model"
        else:
            live.setdefault("power_source", "meter")
            live["power_estimated"] = False
        try:
            th = float(live.get("hashrate_th") or 0)
            ev, eu = efficiency_from_power(cur_pf, th, ainfo)
            if ev is not None:
                live["efficiency_value"] = ev
                live["efficiency_unit"] = eu
                live["efficiency_jth"] = convert_efficiency(ev, eu, "J/T")
        except (TypeError, ValueError):
            pass

    # Rated stats for UI tooltips
    rated = prof.get("rated") if isinstance(prof.get("rated"), dict) else None
    if rated:
        live["model_rated"] = rated

    # Shape boards[] length to expected physical hashboards when we only have
    # aggregate temps (e.g. iPollo V1: 1 HB, 2 chip sensors — not 2 boards).
    _shape_live_boards_to_hardware(live, hw_map)
    return live


def _shape_live_boards_to_hardware(
    live: dict[str, Any], hw_map: dict[str, Any] | None
) -> None:
    """Avoid treating N temp sensors as N hashboards when model has 1 HB."""
    if not isinstance(live, dict) or not isinstance(hw_map, dict):
        return
    try:
        expect = int((hw_map.get("hashboards") or {}).get("count") or 0)
    except (TypeError, ValueError):
        return
    if expect <= 0:
        return
    boards = live.get("boards")
    # If missing boards but we have chip temps and single-HB model, synthesize one.
    if (not isinstance(boards, list) or not boards) and expect == 1:
        temps = []
        for k in ("chip_avg", "chip_max", "chip_min", "temp"):
            try:
                v = live.get(k)
                if v is not None and v != "":
                    temps.append(float(v))
            except (TypeError, ValueError):
                pass
        chip_temps = live.get("chip_temps")
        if isinstance(chip_temps, list):
            for t in chip_temps:
                try:
                    temps.append(float(t))
                except (TypeError, ValueError):
                    pass
        prefix = str((hw_map.get("hashboards") or {}).get("label_prefix") or "HB")
        base = int((hw_map.get("hashboards") or {}).get("index_base") or 0)
        entry: dict[str, Any] = {
            "id": f"{prefix}{base if base else 1}",
            "index": base if base else 1,
            "name": f"{prefix}{base if base else 1}",
        }
        if temps:
            entry["temp"] = round(sum(temps) / len(temps), 1)
            entry["temp_max"] = round(max(temps), 1)
            entry["temp_min"] = round(min(temps), 1)
        if live.get("hashrate_hs"):
            try:
                entry["hashrate_hs"] = float(live["hashrate_hs"])
            except (TypeError, ValueError):
                pass
        live["boards"] = [entry]
        live["board_count"] = 1
        return
    if isinstance(boards, list) and len(boards) > expect:
        # Too many board slots vs model map — keep first N physical
        live["boards"] = boards[:expect]
        live["board_count"] = expect


def _stamp_family_psu_to_live(live: dict[str, Any]) -> None:
    """
    Stamp PSU class from family profile (Whatsminer M63, Antminer, …).

    M63 / L9 class: integrated smart PSU with API power + control.
    """
    if not isinstance(live, dict):
        return
    if live.get("psu_type"):
        return
    hint = (
        live.get("miner_type")
        or live.get("model_code")
        or live.get("model_name")
        or (live.get("model") if isinstance(live.get("model"), str) else None)
    )
    fam = lookup_family(hint)
    psu: dict[str, Any] = {}
    if fam and isinstance(fam.get("psu"), dict):
        psu = dict(fam["psu"])
    if not psu:
        vend = str(live.get("vendor") or live.get("api_vendor") or "").lower()
        mfr = "microbt" if vend in ("whatsminer", "microbt") else (
            "bitmain" if vend in ("antminer", "bitmain") else vend
        )
        if not mfr and fam:
            mfr = str(fam.get("manufacturer") or "")
        psu = _default_psu_for_manufacturer(mfr)
    if not psu:
        return
    live["psu_type"] = psu.get("type")
    live["psu_smart"] = bool(psu.get("smart"))
    live["psu_reports_power"] = bool(psu.get("reports_power"))
    live["psu_controllable"] = bool(psu.get("controllable", psu.get("smart")))
    # Merge into model_hardware if present
    mh = live.get("model_hardware")
    if isinstance(mh, dict):
        mh["psu"] = {
            "type": psu.get("type"),
            "smart": bool(psu.get("smart")),
            "reports_power": bool(psu.get("reports_power")),
            "reports_temp": bool(psu.get("reports_temp")),
            "controllable": bool(psu.get("controllable", psu.get("smart"))),
            "note": psu.get("note"),
        }


def _apply_algo_when_no_profile(live: dict[str, Any], vendor: str | None) -> None:
    """Fill algo + default units from family/vendor when JSON profile missed."""
    ainfo = resolve_miner_algo(
        vendor=vendor,
        miner_type=live.get("miner_type"),
        model=live.get("model") if isinstance(live.get("model"), str) else None,
        model_name=live.get("model_name"),
        model_code=live.get("model_code"),
        explicit=live.get("algo") or live.get("algorithm"),
    )
    apply_algo_meta(live, ainfo)
    try:
        p = float(live["power"]) if live.get("power") not in (None, "") else None
        th = float(live["hashrate_th"]) if live.get("hashrate_th") not in (None, "") else None
    except (TypeError, ValueError):
        p, th = None, None
    src = str(live.get("power_source") or "").lower()
    metered = src in (
        "antminer_6060",
        "6060",
        "meter",
        "psu",
        "summary",
        "api",
    )
    if (p is None or p <= 0) and not metered:
        fam = lookup_family(
            live.get("miner_type")
            or live.get("model_code")
            or (live.get("model") if isinstance(live.get("model"), str) else None)
        )
        if fam:
            hs = None
            try:
                hs = float(live["hashrate_hs"]) if live.get("hashrate_hs") is not None else None
            except (TypeError, ValueError):
                hs = None
            est = estimate_power_from_profile(fam, hashrate_hs=hs, hashrate_th=th)
            if est:
                live["power"] = est["power_w"]
                live["power_source"] = "model"
                live["power_estimated"] = True
                p = est["power_w"]
                want = str((ainfo or {}).get("efficiency_unit") or est["efficiency_unit"])
                live["efficiency_value"] = round(
                    convert_efficiency(
                        float(est["efficiency_value"]), est["efficiency_unit"], want
                    ),
                    1 if _eff_unit_key(want) == "j/t" else 3,
                )
                live["efficiency_unit"] = want
                live["efficiency_jth"] = convert_efficiency(
                    float(est["efficiency_value"]), est["efficiency_unit"], "J/T"
                )
    elif p and p > 0 and src in ("model", "estimate", "estimated", "profile"):
        live["power_estimated"] = True
    elif p and p > 0 and not live.get("power_estimated") and metered:
        live["power_estimated"] = False
    if p and th and th > 0 and live.get("efficiency_value") is None:
        ev, eu = efficiency_from_power(p, th, ainfo)
        if ev is not None:
            live["efficiency_value"] = ev
            live["efficiency_unit"] = eu
            live["efficiency_jth"] = convert_efficiency(ev, eu, "J/T")


def catalog_summary() -> dict[str, Any]:
    skus = _load_chipmap_skus()
    profiles = load_model_profiles()
    return {
        "ok": True,
        "manufacturers": list_manufacturers(),
        "families": list_families(),
        "chipmap_sku_count": len(skus),
        "algos": list_algos(),
        "model_profiles": [
            {
                "id": p.get("id"),
                "vendor": p.get("vendor"),
                "display_name": p.get("display_name"),
                "display_name_full": p.get("display_name_full"),
                "match": p.get("match"),
                "algo": normalize_algo(p.get("algo") or p.get("algorithm")),
                "algo_display": algo_display(p.get("algo") or p.get("algorithm")),
                "coin": p.get("coin"),
                "hashrate_unit": (algo_info(p.get("algo") or p.get("algorithm")) or {}).get(
                    "hashrate_unit"
                )
                or p.get("hashrate_unit"),
                "efficiency_unit": (
                    algo_info(p.get("algo") or p.get("algorithm")) or {}
                ).get("efficiency_unit"),
                "efficiency": p.get("efficiency"),
                "power": p.get("power"),
                "rated": p.get("rated"),
                "cooling": p.get("cooling"),
                "hardware": normalize_hardware_map(p),
                "sensors": (normalize_hardware_map(p) or {}).get("sensors") or p.get("sensors"),
                "poll": p.get("poll"),
            }
            for p in profiles
        ],
        "model_profile_count": len(profiles),
    }
