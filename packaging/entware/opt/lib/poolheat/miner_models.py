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
            "note": "2 physical × 2 virtual slots",
        },
        "chip_layout": {
            "style": "hydro",
            "chips_per_domain_default": 4,
            "chips_per_board_typical": 264,
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
            "note": "3 hashboards · hydro layout SKUs",
        },
        "chip_layout": {
            "style": "hydro",
            "chips_per_domain_default": 3,
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
            "note": "3 hashboards · hydro",
        },
        "chip_layout": {
            "style": "hydro",
            "chips_per_domain_default": 3,
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


def catalog_summary() -> dict[str, Any]:
    skus = _load_chipmap_skus()
    return {
        "ok": True,
        "manufacturers": list_manufacturers(),
        "families": list_families(),
        "chipmap_sku_count": len(skus),
    }
