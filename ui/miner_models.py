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
        unit = "J/GH"
        watts = gh * j_used
    elif j_mh is not None and mh is not None and mh > 0:
        j_used = float(j_mh)
        rate_used = mh
        unit = "J/MH"
        watts = mh * j_used
    elif j_th is not None and th is not None and th > 0:
        j_used = float(j_th)
        rate_used = th
        unit = "J/TH"
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

    # Prefer preferred hashrate unit from profile for UI
    if prof.get("hashrate_unit") and not live.get("hashrate_unit"):
        live["hashrate_unit"] = prof.get("hashrate_unit")

    # Estimate power when missing / not reported by ASIC
    try:
        cur_p = live.get("power")
        cur_pf = float(cur_p) if cur_p is not None and cur_p != "" else None
    except (TypeError, ValueError):
        cur_pf = None

    need_est = cur_pf is None or cur_pf <= 0
    # Models that omit wall power on :4028 still get a model estimate — but
    # never clobber a real meter (Antminer :6060/miner_power, PSU, etc.).
    metered_src = str(live.get("power_source") or "").lower() in (
        "antminer_6060",
        "6060",
        "meter",
        "psu",
        "summary",
        "api",
    )
    if reported_flag is False and not metered_src and (cur_pf is None or cur_pf <= 0):
        need_est = True
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
            live["efficiency_value"] = est["efficiency_value"]
            live["efficiency_unit"] = est["efficiency_unit"]
            if est["efficiency_unit"] == "J/TH":
                live["efficiency_jth"] = est["efficiency_value"]
            elif est["efficiency_unit"] == "J/GH" and th and th > 0:
                # also expose J/TH equivalent for charts that only know jth
                live["efficiency_jth"] = float(est["efficiency_value"]) * 1000.0
            live["efficiency_note"] = est.get("note")
    elif cur_pf is not None and cur_pf > 0:
        live.setdefault("power_source", "meter")
        live["power_estimated"] = False
        # fill efficiency from measured power when possible
        try:
            th = float(live.get("hashrate_th") or 0)
            if th > 0 and live.get("efficiency_jth") is None:
                live["efficiency_jth"] = round(cur_pf / th, 2)
                live["efficiency_value"] = live["efficiency_jth"]
                live["efficiency_unit"] = "J/TH"
        except (TypeError, ValueError):
            pass

    # Rated stats for UI tooltips
    rated = prof.get("rated") if isinstance(prof.get("rated"), dict) else None
    if rated:
        live["model_rated"] = rated
    return live


def catalog_summary() -> dict[str, Any]:
    skus = _load_chipmap_skus()
    profiles = load_model_profiles()
    return {
        "ok": True,
        "manufacturers": list_manufacturers(),
        "families": list_families(),
        "chipmap_sku_count": len(skus),
        "model_profiles": [
            {
                "id": p.get("id"),
                "vendor": p.get("vendor"),
                "display_name": p.get("display_name"),
                "display_name_full": p.get("display_name_full"),
                "match": p.get("match"),
                "efficiency": p.get("efficiency"),
                "power": p.get("power"),
                "rated": p.get("rated"),
                "hashrate_unit": p.get("hashrate_unit"),
            }
            for p in profiles
        ],
        "model_profile_count": len(profiles),
    }
