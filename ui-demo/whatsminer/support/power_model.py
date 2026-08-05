"""
Honest wall-power estimates from hashrate × model efficiency.

Firmware ``power_rt`` is often only the *reported* PSU (``vin×i_in``). With a
second brick on a 12V voltage synchronizer, hashrate reflects full bus power
while ``power_rt`` under-counts. For those fleets we apply a **declared model
J/TH** (not a sensor reading).

Defaults (operator/process knowledge — adjust if you meter differently):

| Family   | J/TH (mid) | range     | Notes                          |
|----------|------------|-----------|--------------------------------|
| M60*     | 20.5       | 20–21     | dual-sync / high-power M60     |
| M63*     | 19.0       | 18–21     | liquid lab; prefer meter       |
| default  | None       | —         | refuse to invent without type  |

All estimates are labeled ``source="model"`` so they are never confused with
metered power.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# mid, (low, high) — joules per TH/s (= W per TH/s at steady state)
_MODEL_J_PER_TH: dict[str, tuple[float, float, float]] = {
    # M60 family (user: ~20–21 J/T with synchronizer dual-PSU builds)
    "M60": (20.5, 20.0, 21.0),
    "M60S": (20.5, 20.0, 21.0),
    "M60S+": (20.5, 20.0, 21.0),
    "M60S++": (20.5, 20.0, 21.0),
    # M63 liquid (lab Peak reported ~13.5 from primary PSU only — model higher)
    "M63": (19.0, 18.0, 21.0),
    "M63S": (19.0, 18.0, 21.0),
}

# Prefix match order: longer first
_PREFIXES: list[str] = sorted(_MODEL_J_PER_TH.keys(), key=len, reverse=True)

DEFAULT_M60_J_PER_TH = 20.5
DEFAULT_M60_J_RANGE = (20.0, 21.0)


def normalize_miner_type(miner_type: Optional[str]) -> str:
    if not miner_type:
        return ""
    s = str(miner_type).strip().upper().replace(" ", "")
    # MinerType sometimes "M60S VK2A" etc.
    s = re.split(r"[^A-Z0-9+]+", s, maxsplit=1)[0]
    return s


def resolve_model_efficiency(
    miner_type: Optional[str] = None,
    *,
    joules_per_th: Optional[float] = None,
    prefer: str = "mid",
) -> dict[str, Any]:
    """
    Resolve J/TH to use for wall-power model.

    Priority:
      1. explicit ``joules_per_th``
      2. table lookup from ``miner_type`` (M60 → 20.5, range 20–21)
      3. unknown type → no default number (honest)

    ``prefer``: ``mid`` | ``low`` | ``high`` within the model range.
    """
    if joules_per_th is not None:
        j = float(joules_per_th)
        return {
            "joules_per_th": j,
            "j_per_th_low": j,
            "j_per_th_high": j,
            "miner_type": normalize_miner_type(miner_type) or None,
            "source": "explicit",
            "honest_label": "model_explicit",
        }

    key = normalize_miner_type(miner_type)
    matched = None
    for p in _PREFIXES:
        if key == p or key.startswith(p):
            matched = p
            break

    if matched is None:
        return {
            "joules_per_th": None,
            "j_per_th_low": None,
            "j_per_th_high": None,
            "miner_type": key or None,
            "source": "unknown_type",
            "honest_label": "no_model",
            "note": "Pass miner_type (e.g. M60) or joules_per_th= explicitly.",
        }

    mid, lo, hi = _MODEL_J_PER_TH[matched]
    pick = {"mid": mid, "low": lo, "high": hi}.get(prefer, mid)
    return {
        "joules_per_th": pick,
        "j_per_th_low": lo,
        "j_per_th_high": hi,
        "miner_type": key,
        "model_family": matched,
        "source": "model_table",
        "honest_label": f"model_{matched}_{prefer}",
        "note": (
            f"{matched} wall-power model uses ~{lo}–{hi} J/TH "
            f"(using {pick} as {prefer}). Not a meter reading; for "
            f"synchronizer dual-PSU fleets where power_rt under-counts."
        ),
    }


def estimate_power_from_hashrate(
    hashrate_ths: float,
    *,
    joules_per_th: Optional[float] = None,
    miner_type: Optional[str] = None,
    prefer: str = "mid",
) -> dict[str, Any]:
    """
    ``P_wall_est = hashrate_TH/s × J/TH``.

    Always sets ``kind="estimate"`` / ``source`` so callers never treat it as
    measured PSU power.
    """
    thr = float(hashrate_ths)
    eff = resolve_model_efficiency(
        miner_type, joules_per_th=joules_per_th, prefer=prefer
    )
    j = eff.get("joules_per_th")
    if j is None:
        return {
            "ok": False,
            "kind": "estimate",
            "hashrate_ths": thr,
            "power_w_estimate": None,
            "power_w_low": None,
            "power_w_high": None,
            **eff,
        }

    lo = eff["j_per_th_low"]
    hi = eff["j_per_th_high"]
    return {
        "ok": True,
        "kind": "estimate",
        "hashrate_ths": thr,
        "joules_per_th": j,
        "j_per_th_low": lo,
        "j_per_th_high": hi,
        "power_w_estimate": thr * j,
        "power_w_low": thr * float(lo),
        "power_w_high": thr * float(hi),
        "formula": "P_W ≈ hashrate_TH/s * J_per_TH  (model, not metered)",
        "miner_type": eff.get("miner_type"),
        "model_family": eff.get("model_family"),
        "source": eff.get("source"),
        "honest_label": eff.get("honest_label"),
        "note": eff.get("note"),
    }


def compare_reported_vs_model_power(
    power_rt_w: float,
    hashrate_ths: float,
    *,
    joules_per_th: Optional[float] = None,
    miner_type: Optional[str] = None,
    prefer: str = "mid",
) -> dict[str, Any]:
    """
    Compare firmware ``power_rt`` (or vin×i_in) to model wall power.

    For M60 + synchronizer dual: pass ``miner_type="M60"`` → ~20.5 J/TH mid.
    """
    model = estimate_power_from_hashrate(
        hashrate_ths,
        joules_per_th=joules_per_th,
        miner_type=miner_type,
        prefer=prefer,
    )
    reported = float(power_rt_w)
    thr = float(hashrate_ths)
    est = model.get("power_w_estimate")
    out: dict[str, Any] = {
        **model,
        "power_rt_reported_w": reported,
        "reported_j_per_th": (reported / thr) if thr else None,
        "reported_kind": "firmware_power_rt",
        "estimate_kind": "model_hashrate_x_j_th",
    }
    if est is None:
        out["ok"] = False
        return out

    gap = float(est) - reported
    out.update(
        {
            "gap_w": gap,
            "gap_ratio": (float(est) / reported) if reported else None,
            "gap_w_low": float(model["power_w_low"]) - reported,
            "gap_w_high": float(model["power_w_high"]) - reported,
            "interpretation": (
                "positive gap ⇒ firmware likely under-counts wall power "
                "(e.g. second PSU on 12V sync not in i_in), and/or model J/TH "
                "higher than true silicon efficiency"
            ),
        }
    )
    return out


def estimate_dual_wall_power(
    hashrate_ths: float,
    power_rt_w: float | None = None,
    *,
    miner_type: Optional[str] = "M60",
    joules_per_th: Optional[float] = None,
    prefer: str = "mid",
    oc_power_target_w: Optional[float] = None,
    oc_power_max_w: Optional[float] = None,
    oc_target_hash_ths: Optional[float] = None,
    dual_j_threshold: float = 16.0,
    stock_j_per_th: Optional[float] = None,
) -> dict[str, Any]:
    """
    Near-real wall power picture for **dual-PSU / 12V synchronizer** fleets.

    Inputs (any subset; more → better confidence)::

        hashrate_ths       live TH/s (WMOC history ``total_hr`` or summary)
        power_rt_w         firmware primary PSU ≈ vin×i_in (undercounts dual)
        oc_power_target_w  WMOC Overclock ``power_lim`` or AGP preset watts
                           (e.g. AGP_17_237.1TH_3250W → 3250) — intended wall
        oc_power_max_w     OC ``power_max`` hard ceiling
        oc_target_hash_ths OC ``target_hash`` for the active profile
        dual_j_threshold   if power_rt/TH < this (default 16), flag dual undercount
        stock_j_per_th     optional lab clamp of true J/TH when dual was verified

    Priority for **best_w** (honest wall)::

        1. stock_j_per_th × TH          (AC clamp once — gold)
        2. if dual_suspect: model J/TH × TH   (M60 dual-sync 20–21)
           else: power_rt (single-PSU path)
        3. oc_power_target_w always exposed as **control-domain** layer
           (WMOC AGP/power_lim often tracks same ~13–15 J/TH as power_rt,
           not true dual wall — do not treat as wattmeter)

    Always returns both ``power_rt`` and estimates with labels — never pretends
    model is a wattmeter.
    """
    thr = float(hashrate_ths)
    prt = float(power_rt_w) if power_rt_w is not None else None
    reported_j = (prt / thr) if (prt is not None and thr > 0) else None

    dual_suspect = bool(
        reported_j is not None and reported_j < float(dual_j_threshold) and thr > 50
    )

    model = estimate_power_from_hashrate(
        thr,
        joules_per_th=joules_per_th,
        miner_type=miner_type,
        prefer=prefer,
    )

    # OC profile target (AGP name embeds intended wall watts)
    oc_scaled: Optional[float] = None
    if oc_power_target_w is not None:
        oc_w = float(oc_power_target_w)
        if oc_target_hash_ths and float(oc_target_hash_ths) > 10 and thr > 0:
            # scale profile wall to actual hashrate (under-hash vs target)
            oc_scaled = oc_w * (thr / float(oc_target_hash_ths))
        else:
            oc_scaled = oc_w

    # calibrated stock J/TH
    cal_w: Optional[float] = None
    if stock_j_per_th is not None and thr > 0:
        cal_w = thr * float(stock_j_per_th)

    candidates: list[tuple[str, float, str]] = []
    if cal_w is not None:
        candidates.append(
            (
                "calibrated_j_th",
                cal_w,
                "hashrate × lab-clamped J/TH (best if dual was metered once)",
            )
        )
    if oc_scaled is not None:
        candidates.append(
            (
                "oc_profile",
                oc_scaled,
                "WMOC power_lim / AGP preset watts (scaled by TH/target_hash if known)",
            )
        )
    if model.get("power_w_estimate") is not None:
        candidates.append(
            (
                "model_j_th",
                float(model["power_w_estimate"]),
                model.get("note")
                or "hashrate × model table J/TH (M60 dual-sync fleet default)",
            )
        )
    if prt is not None:
        candidates.append(
            (
                "power_rt",
                prt,
                "firmware primary PSU only — undercounts synchronizer dual",
            )
        )

    # Prefer calibrated; if dual_suspect use model wall (not OC watts — same domain as power_rt)
    best_source = None
    best_w = None
    best_note = None
    if cal_w is not None:
        best_source, best_w, best_note = (
            "calibrated_j_th",
            cal_w,
            "hashrate × lab-clamped J/TH (best if dual was metered once)",
        )
    elif dual_suspect and model.get("power_w_estimate") is not None:
        best_source = "model_j_th"
        best_w = float(model["power_w_estimate"])
        best_note = (
            "dual_psu_suspect (power_rt/TH low): wall ≈ TH×model J/TH; "
            "OC power_lim/AGP is control-domain (~same scale as power_rt), not clamp wall"
        )
    elif not dual_suspect and prt is not None:
        best_source, best_w, best_note = (
            "power_rt",
            prt,
            "single-PSU path (reported J/TH looks normal)",
        )
    elif model.get("power_w_estimate") is not None:
        best_source = "model_j_th"
        best_w = float(model["power_w_estimate"])
        best_note = model.get("note")
    elif prt is not None:
        best_source, best_w, best_note = "power_rt", prt, "fallback power_rt only"

    # band: low/high from model + OC clamp
    band_lo = model.get("power_w_low")
    band_hi = model.get("power_w_high")
    if oc_power_target_w is not None:
        ot = float(oc_power_target_w)
        band_lo = min(x for x in (band_lo, ot * 0.95, oc_scaled) if x is not None) if True else ot
        vals = [v for v in (band_lo, band_hi, ot, oc_scaled, best_w) if v is not None]
        if vals:
            band_lo = min(vals)
            band_hi = max(vals)
    if oc_power_max_w is not None and band_hi is not None:
        band_hi = min(band_hi, float(oc_power_max_w))

    gap = (float(best_w) - prt) if (best_w is not None and prt is not None) else None

    return {
        "ok": best_w is not None,
        "kind": "estimate",
        "honest_label": "dual_wall_stack",
        "hashrate_ths": thr,
        "power_rt_w": prt,
        "reported_j_per_th": reported_j,
        "dual_psu_suspect": dual_suspect,
        "dual_j_threshold": dual_j_threshold,
        "best_w": best_w,
        "best_source": best_source,
        "best_note": best_note,
        "band_w_low": band_lo,
        "band_w_high": band_hi,
        "gap_vs_power_rt_w": gap,
        "layers": {
            "power_rt": prt,
            "oc_profile_w": oc_scaled,
            "oc_power_target_w": oc_power_target_w,
            "oc_power_max_w": oc_power_max_w,
            "oc_target_hash_ths": oc_target_hash_ths,
            "model_w": model.get("power_w_estimate"),
            "model_w_low": model.get("power_w_low"),
            "model_w_high": model.get("power_w_high"),
            "calibrated_w": cal_w,
        },
        "model": model,
        "recipe": [
            "1) Clamp AC1+AC2 once → stock_j_per_th = P_total/TH (best forever)",
            "2) dual_suspect (power_rt/TH < ~16): best_w = TH × model J/TH (M60: 20–21)",
            "3) not dual: best_w = power_rt",
            "4) Always show layers: power_rt, oc power_lim/AGP (control), model band",
        ],
        "lab_note": (
            "Live WMOC: power_rt and AGP_*_yyyyW both sit ~13–15 J/TH; true dual "
            "wall needs clamp or model table, not power_lim alone."
        ),
        "control_domain": {
            "power_rt_w": prt,
            "oc_profile_w": oc_scaled,
            "note": "same accounting family — primary/firmware domain",
        },
    }


def parse_agp_preset_label(label: str) -> dict[str, Any]:
    """
    Parse WMOC Overclock preset text like ``AGP_17_237.1TH_3250W``.

    Returns ``{name, ths, power_w}`` or partial.
    """
    s = str(label or "").strip()
    out: dict[str, Any] = {"raw": s, "ths": None, "power_w": None, "index": None}
    m = re.search(
        r"AGP_?(\d+)[_\s]+([\d.]+)\s*TH[_\s]+(\d+)\s*W",
        s,
        re.I,
    )
    if m:
        out["index"] = int(m.group(1))
        out["ths"] = float(m.group(2))
        out["power_w"] = int(m.group(3))
        out["name"] = f"AGP_{out['index']:02d}_{out['ths']}TH_{out['power_w']}W"
        return out
    m2 = re.search(r"([\d.]+)\s*TH.*?(\d+)\s*W", s, re.I)
    if m2:
        out["ths"] = float(m2.group(1))
        out["power_w"] = int(m2.group(2))
    return out


def estimate_history_wall_power(
    records: list[dict[str, Any]],
    *,
    miner_type: Optional[str] = "M60",
    joules_per_th: Optional[float] = None,
    prefer: str = "mid",
) -> dict[str, Any]:
    """
    Apply model to WMOC history records (uses ``total_hr`` + ``psu.power_rt``).

    Returns latest point + series stats. Honest labels on every estimate.
    """
    if not records:
        return {"ok": False, "error": "no_records"}

    points: list[dict[str, Any]] = []
    for rec in records:
        thr = rec.get("total_hr")
        psu = rec.get("psu") or {}
        prt = psu.get("power_rt")
        if thr is None:
            continue
        cmp_ = compare_reported_vs_model_power(
            float(prt or 0),
            float(thr),
            joules_per_th=joules_per_th,
            miner_type=miner_type,
            prefer=prefer,
        )
        points.append(
            {
                "ts": rec.get("ts"),
                "total_hr": thr,
                "power_rt": prt,
                "power_w_estimate": cmp_.get("power_w_estimate"),
                "power_w_low": cmp_.get("power_w_low"),
                "power_w_high": cmp_.get("power_w_high"),
                "gap_w": cmp_.get("gap_w"),
                "reported_j_per_th": cmp_.get("reported_j_per_th"),
            }
        )

    if not points:
        return {"ok": False, "error": "no_usable_points"}

    latest = points[-1]
    gaps = [p["gap_w"] for p in points if p.get("gap_w") is not None]
    ests = [p["power_w_estimate"] for p in points if p.get("power_w_estimate") is not None]
    eff = resolve_model_efficiency(
        miner_type, joules_per_th=joules_per_th, prefer=prefer
    )
    return {
        "ok": True,
        "kind": "estimate",
        "honest_label": eff.get("honest_label"),
        "model": eff,
        "latest": latest,
        "points": points,
        "stats": {
            "n": len(points),
            "gap_w_mean": sum(gaps) / len(gaps) if gaps else None,
            "power_est_mean": sum(ests) / len(ests) if ests else None,
            "power_rt_mean": sum(p["power_rt"] or 0 for p in points) / len(points),
        },
        "note": (
            "power_w_estimate is model (TH×J/TH), not a wattmeter. "
            "power_rt is firmware primary-PSU accounting."
        ),
    }
