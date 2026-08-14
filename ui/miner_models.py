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
            "liquid_temp": True,
            "env_temp": True,
            "chip_temp": True,  # summary Chip Temp Min/Avg/Max
            "pcb_temp": True,  # per-slot PCB (SM0…)
            "board_chip_temp": False,  # per-slot chip min/avg/max from DEVS (often absent)
            "psu_temp": True,
        },
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
            "liquid_temp": True,
            "env_temp": True,
            "chip_temp": True,
            "pcb_temp": True,
            "board_chip_temp": False,  # M63 DEVS usually no SM chip min/avg/max
            "psu_temp": True,
        },
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
    return live


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
                "hashrate_unit": p.get("hashrate_unit"),
            }
            for p in profiles
        ],
        "model_profile_count": len(profiles),
    }
