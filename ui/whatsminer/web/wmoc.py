"""
WMOC alternative firmware — LuCI apps and telemetry.

WMOC (lab e.g. ``http://host:8788``) adds custom LuCI modules on top of stock
Whatsminer web. Stock :class:`~whatsminer.web.luci.LuCIClient` stays firmware-neutral;
all WMOC-specific constants and ops live here.

Modules (activation = page responds with WMOC fingerprints)::

    installer  → /admin/system/wmoc_installer     base WMOC firmware
    tools      → /admin/system/wmoc_tools         Mining Tools package
    fancontrol → /admin/network/wmoc_fancontrol   FanControl / Hashrate Splitter
    overclock  → /admin/network/wmoc_overclock
    history    → /admin/status/wmoc_history       NDJSON telemetry

Usage::

    from whatsminer import LuCIClient, detect_wmoc, WMOCClient

    info = detect_wmoc("10.121.15.76:8788")
    if info["wmoc"]:
        w = WMOCClient.from_host("10.121.15.76:8788")
        hist = w.get_history(max_records=100)
        print(w.analyze_psu(history=hist))
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .luci import LuCIClient, LuCIError
from ..support.power_model import (
    estimate_dual_wall_power,
    estimate_history_wall_power,
    parse_agp_preset_label,
)

# ── module map ───────────────────────────────────────────────────────────────

# Lab http://10.121.15.76:8788 — activation = page is live.
# Base WMOC firmware always exposes the installer; optional packages add tools/fan/oc.
WMOC_MODULES: dict[str, dict[str, str]] = {
    "installer": {
        "path": "/cgi-bin/luci/admin/system/wmoc_installer",
        "title": "WMOC Package Installer",
        "role": "base",  # present on WMOC firmware by default
    },
    "tools": {
        "path": "/cgi-bin/luci/admin/system/wmoc_tools",
        "title": "WMOC Mining Tools",
        "role": "module",
    },
    "fancontrol": {
        "path": "/cgi-bin/luci/admin/network/wmoc_fancontrol",
        "title": "WMOC FanControl",  # also "Hashrate Splitter"
        "role": "module",
    },
    "overclock": {
        "path": "/cgi-bin/luci/admin/network/wmoc_overclock",
        "title": "WMOC Overclock",
        "role": "module",
    },
    "history": {
        "path": "/cgi-bin/luci/admin/status/wmoc_history",
        "title": "WMOC History",
        "role": "telemetry",  # NDJSON time-series when tools/history enabled
    },
}
WMOC_MARKER_PATHS: tuple[str, ...] = tuple(m["path"] for m in WMOC_MODULES.values())
WMOC_PRIMARY_PATH = WMOC_MODULES["installer"]["path"]
WMOC_TOOLS_PATH = WMOC_MODULES["tools"]["path"]
WMOC_HISTORY_PATH = WMOC_MODULES["history"]["path"]

# HTML fingerprints after login (lab 10.121.15.76:8788).
WMOC_HTML_MARKERS: tuple[str, ...] = (
    "WMOC Mining Tools",
    "WMOC Package Installer",
    "WMOC FanControl",
    "WMOC Hashrate Splitter",
    "WMOC Overclock",
    "wmoc_tools",
    "wmoc_installer",
    "wmoc_fancontrol",
    "wmoc_overclock",
    "wmoc.tech",
    "wmoc_logo",
    "cbid.wmoc_tools",
    "cbid.wmoc_installer",
    "cbid.fancontrol",
    "cbid.overclock",
)


# ── core ops (accept any LuCIClient) ─────────────────────────────────────────


def detect_on_client(
    client: LuCIClient,
    *,
    login: bool = True,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Detect WMOC and which modules are activated on an open LuCI session.

    ``wmoc=True`` if **installer** (base) or any module page is live.
    """
    cache = getattr(client, "_wmoc_cache", None)
    if use_cache and cache is not None:
        return cache

    if login:
        client.ensure_login()

    paths: dict[str, Any] = {}
    modules: dict[str, Any] = {}
    markers_found: list[str] = []

    for mod_name, meta in WMOC_MODULES.items():
        path = meta["path"]
        code, body, final = client._open(path)
        text = body.decode("utf-8", errors="replace")
        low = text.lower()
        hit_markers = [m for m in WMOC_HTML_MARKERS if m.lower() in low]
        title_hit = meta["title"].lower() in low if meta.get("title") else False
        # history is NDJSON (starts with '{'), not HTML
        is_history_feed = (
            mod_name == "history"
            and code == 200
            and (text.lstrip().startswith("{") or '"psu"' in text)
        )
        ok = code == 200 and (
            is_history_feed
            or bool(hit_markers)
            or title_hit
            or "cbid.wmoc" in low
            or "cbid.fancontrol" in low
            or "cbid.overclock" in low
            or ("wmoc" in path and "wmoc" in low and "luci_username" not in low)
        )
        entry = {
            "ok": bool(ok),
            "active": bool(ok),
            "status": code,
            "url": final,
            "path": path,
            "role": meta.get("role"),
            "title": meta.get("title"),
            "markers": hit_markers,
        }
        paths[path] = entry
        modules[mod_name] = entry
        for m in hit_markers:
            if m not in markers_found:
                markers_found.append(m)

    active = [n for n, e in modules.items() if e.get("active")]
    base_wmoc = bool(modules.get("installer", {}).get("active"))
    wmoc = base_wmoc or bool(active)

    result: dict[str, Any] = {
        "wmoc": bool(wmoc),
        "base_firmware": base_wmoc,
        "modules": modules,
        "active_modules": active,
        "base": client.base,
        "host": client.host,
        "port": client.port,
        "scheme": client.scheme,
        "paths": paths,
        "markers": markers_found,
        "primary_path": WMOC_PRIMARY_PATH,
        "primary_url": client._url(WMOC_PRIMARY_PATH) if wmoc else None,
        "tools_url": client._url(WMOC_TOOLS_PATH)
        if modules.get("tools", {}).get("active")
        else None,
    }
    client._wmoc_cache = result
    return result


def has_wmoc(client: LuCIClient, *, login: bool = True) -> bool:
    """True if WMOC base firmware or any WMOC module is present."""
    return bool(detect_on_client(client, login=login).get("wmoc"))


def get_history(
    client: LuCIClient,
    *,
    max_records: Optional[int] = None,
) -> dict[str, Any]:
    """
    Fetch ``/admin/status/wmoc_history`` (NDJSON time-series).

    Each line is a JSON object with ``ts``, ``psu``, ``env``, ``boards``, …
    ``psu.temps`` length 3 is multi-sensor on one brick, not three PSUs.
    """
    client.ensure_login()
    code, body, url = client._open(WMOC_HISTORY_PATH)
    if code >= 400:
        raise LuCIError(f"wmoc_history HTTP {code}")
    text = body.decode("utf-8", errors="replace")
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
        if max_records is not None and len(records) >= max_records:
            break
    return {
        "ok": True,
        "url": url,
        "count": len(records),
        "records": records,
    }


def analyze_psu(
    history: Optional[dict[str, Any]] = None,
    *,
    client: Optional[LuCIClient] = None,
    sample: int = 50,
) -> dict[str, Any]:
    """
    Infer *logical* PSU layout from WMOC history telemetry.

    Dual-PSU via 12 V synchronizer is usually **invisible** here — one logical
    ``psu`` object. See return field ``synchronizer_dual_invisible``.
    """
    if history is None:
        if client is None:
            raise ValueError("analyze_psu needs history= or client=")
        history = get_history(client, max_records=sample)
    records = history.get("records") or []
    if not records:
        return {
            "ok": False,
            "likely_dual_psu": None,
            "psu_count_estimate": None,
            "evidence": ["no_history_records"],
            "note": "wmoc_history empty or unavailable",
        }

    evidence: list[str] = []
    psu_list_hits = 0
    multi_vin_hits = 0
    temps_len = None
    vendors: set[str] = set()
    sample_psu: Any = None

    for rec in records:
        psu = rec.get("psu")
        if sample_psu is None:
            sample_psu = psu
        if isinstance(psu, list):
            psu_list_hits += 1
            evidence.append(f"psu_is_list_len_{len(psu)}")
        elif isinstance(psu, dict):
            for k in psu:
                if re.search(r"(vin|i_in|power).*[2-9]$|[2-9]$", k) or "psu2" in k.lower():
                    multi_vin_hits += 1
            t = psu.get("temps")
            if isinstance(t, list):
                temps_len = len(t)
            if psu.get("vendor") is not None:
                vendors.add(str(psu.get("vendor")))

    if psu_list_hits:
        max_n = 0
        for rec in records:
            p = rec.get("psu")
            if isinstance(p, list):
                max_n = max(max_n, len(p))
        return {
            "ok": True,
            "likely_dual_psu": max_n >= 2,
            "psu_count_estimate": max_n,
            "evidence": [f"psu_array_max_len={max_n}", f"hits={psu_list_hits}"],
            "sample_psu": sample_psu,
            "vendors": sorted(vendors),
            "note": "psu field is a list — multi-PSU telemetry",
        }

    if multi_vin_hits:
        return {
            "ok": True,
            "likely_dual_psu": True,
            "psu_count_estimate": 2,
            "evidence": [f"secondary_vin_or_psu_fields_hits={multi_vin_hits}"],
            "sample_psu": sample_psu,
            "vendors": sorted(vendors),
            "note": "found vin2/i_in2-style fields in psu object",
        }

    evidence.append("single_psu_object")
    if temps_len is not None:
        evidence.append(f"temps_len={temps_len}_sensors_not_psu_count")
    evidence.append("synchronizer_second_psu_not_in_telemetry")
    return {
        "ok": True,
        "likely_dual_psu": False,
        "psu_count_estimate": 1,
        "synchronizer_dual_invisible": True,
        "temps_sensor_count": temps_len,
        "evidence": evidence,
        "sample_psu": sample_psu,
        "vendors": sorted(vendors),
        "records_scanned": len(records),
        "note": (
            "Firmware reports one logical PSU. A second brick paralleled via "
            "12V voltage synchronizer does not appear as psu2/vin2 in "
            "wmoc_history — only more 12V headroom (higher power_rt / TH/s) "
            "when its AC is on. Detect dual physically (AC clamps) or by "
            "hashrate/power steps when toggling the second feed, not from "
            "this JSON alone."
        ),
        "power_accounting": {
            "power_rt_is": "usually vin*i_in of the *reported* PSU only",
            "missing": "second PSU AC inlet current; total 12V bus current (I_out)",
            "cannot_derive_true_wall_from": [
                "board freq/hr alone (no board amps in history)",
                "v_out alone (no total I_12V)",
            ],
            "indirect_estimate": (
                "P_model = total_hr * J_per_TH (user/process model); "
                "or calibrate ΔP from dual AC on/off + clamp once"
            ),
        },
    }


def estimate_wall_power_from_history(
    history: dict[str, Any] | list[dict[str, Any]],
    *,
    miner_type: str | None = "M60",
    joules_per_th: float | None = None,
    prefer: str = "mid",
) -> dict[str, Any]:
    """Apply J/TH wall-power model to WMOC history records."""
    records = (
        history.get("records")
        if isinstance(history, dict)
        else history
    ) or []
    return estimate_history_wall_power(
        records,
        miner_type=miner_type,
        joules_per_th=joules_per_th,
        prefer=prefer,
    )


def _html_field_values(html: str, prefix: str) -> dict[str, str]:
    """Extract ``name=\"prefix…\" value=\"…\"`` pairs from a LuCI page."""
    import re

    out: dict[str, str] = {}
    for n, v in re.findall(
        rf'name="({re.escape(prefix)}[^"]+)"[^>]*value="([^"]*)"', html
    ):
        out[n] = v
    for v, n in re.findall(
        rf'value="([^"]*)"[^>]*name="({re.escape(prefix)}[^"]+)"', html
    ):
        out.setdefault(n, v)
    return out


def _html_options(html: str) -> list[dict[str, str]]:
    import re

    return [
        {"value": a, "label": b.strip()}
        for a, b in re.findall(
            r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>', html
        )
    ]


def _get_page(client: LuCIClient, path: str) -> str:
    client.ensure_login()
    code, body, _ = client._open(path)
    if code >= 400:
        raise LuCIError(f"GET {path} -> {code}")
    text = body.decode("utf-8", errors="replace")
    tok = client._extract_token(text)
    if tok:
        client._token = tok
    return text


def _extract_cbi_form(html: str, field_prefix: str) -> dict[str, str]:
    """
    Build a LuCI CBI form dict from page HTML: token + cbi.submit + all cbid/cbi.cbe.
    """
    form: dict[str, str] = {}
    tok = re.search(r'name="token"\s+value="([^"]+)"', html)
    if tok:
        form["token"] = tok.group(1)
    form["cbi.submit"] = "1"
    # empty cbi map marker sometimes present
    if 'name="cbi"' in html:
        form["cbi"] = ""

    # all cbid.* and cbi.cbe.* with values
    for n, v in re.findall(
        r'name="((?:cbid|cbi\.cbe)\.[^"]+)"[^>]*value="([^"]*)"', html
    ):
        if n.startswith(field_prefix) or n.startswith("cbi.cbe."):
            form[n] = v
    for v, n in re.findall(
        r'value="([^"]*)"[^>]*name="((?:cbid|cbi\.cbe)\.[^"]+)"', html
    ):
        if n.startswith(field_prefix) or n.startswith("cbi.cbe."):
            form.setdefault(n, v)

    # selected <option>
    for m in re.finditer(
        r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', html, re.S | re.I
    ):
        name, body = m.group(1), m.group(2)
        if not (name.startswith(field_prefix) or name.startswith("cbi.")):
            continue
        sel = re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', body, re.I)
        if not sel:
            sel = re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', body, re.I)
        if sel:
            form[name] = sel.group(1)
        else:
            first = re.search(r'<option[^>]*value="([^"]*)"', body, re.I)
            if first and name not in form:
                form[name] = first.group(1)

    # checkboxes: if checked, value 1; LuCI also wants cbi.cbe.* present
    for m in re.finditer(
        r'<input[^>]*type="checkbox"[^>]*name="([^"]+)"[^>]*>', html, re.I
    ):
        # re-find with full tag
        pass
    for m in re.finditer(r"<input([^>]+)>", html, re.I):
        tag = m.group(1)
        if "checkbox" not in tag.lower():
            continue
        nm = re.search(r'name="([^"]+)"', tag)
        if not nm:
            continue
        name = nm.group(1)
        if not name.startswith(field_prefix):
            continue
        checked = "checked" in tag.lower()
        form[name] = "1" if checked else form.get(name, "1")
        # companion cbi.cbe field
        cbe = name.replace("cbid.", "cbi.cbe.", 1)
        form.setdefault(cbe, "")

    return form


def _cbi_post(
    client: LuCIClient,
    path: str,
    *,
    field_prefix: str,
    overrides: Optional[dict[str, Any]] = None,
    button: Optional[str] = None,
    button_value: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    GET form page → merge overrides → POST.

    ``button`` is a full field name (e.g. ``cbid.wmoc_tools.1._start``) or short
    suffix (``_start``) resolved under ``field_prefix.1.``.
    """
    import urllib.parse

    html = _get_page(client, path)
    form = _extract_cbi_form(html, field_prefix)
    if "token" not in form and client._token:
        form["token"] = client._token
    if "token" not in form:
        raise LuCIError(f"no CSRF token on {path}")

    overrides = dict(overrides or {})
    # normalize short keys → full cbid paths
    prefix1 = field_prefix.rstrip(".") + ".1."
    norm: dict[str, str] = {}
    for k, v in overrides.items():
        key = k if k.startswith("cbid.") or k.startswith("cbi.") else prefix1 + k.lstrip(".")
        if v is None:
            continue
        if isinstance(v, bool):
            norm[key] = "1" if v else "0"
        else:
            norm[key] = str(v)
    form.update(norm)

    # strip other buttons from form so only intended action fires
    button_keys = [k for k in list(form) if k.split(".")[-1].startswith("_") or k.endswith("_button") or "button" in k.split(".")[-1]]
    for bk in button_keys:
        # keep non-action value fields; remove clickable buttons
        short = bk.split(".")[-1]
        if short.startswith("_") or short.endswith("_button") or short.endswith("_btn"):
            form.pop(bk, None)

    if button:
        bname = button
        if not bname.startswith("cbid.") and not bname.startswith("cbi."):
            bname = prefix1 + button.lstrip(".")
        # recover label from original html if possible
        if button_value is None:
            m = re.search(
                rf'name="{re.escape(bname)}"[^>]*value="([^"]*)"', html
            ) or re.search(
                rf'value="([^"]*)"[^>]*name="{re.escape(bname)}"', html
            )
            button_value = m.group(1) if m else "1"
        # decode HTML entities lightly
        button_value = (
            str(button_value)
            .replace("&#38;", "&")
            .replace("&amp;", "&")
        )
        form[bname] = button_value

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "path": path,
            "form_keys": sorted(form.keys()),
            "form": {k: form[k] for k in sorted(form) if not k.startswith("token")},
            "token_set": bool(form.get("token")),
        }

    data = urllib.parse.urlencode(form).encode()
    code, body, final = client._open(path, data=data, method="POST")
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    tok = client._extract_token(text)
    if tok:
        client._token = tok
    # clear wmoc cache after mutations
    client._wmoc_cache = None
    return {
        "ok": code in (200, 302) and b"luci_username" not in (body[:500] if isinstance(body, bytes) else b""),
        "status": code,
        "path": path,
        "url": final,
        "button": button,
        "overrides": norm,
        "body_len": len(body) if isinstance(body, (bytes, str)) else 0,
    }


# ── writes: installer / tools / overclock ────────────────────────────────────


def installer_action(
    client: LuCIClient,
    action: str,
    *,
    key: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Click a WMOC Installer button.

    ``action`` one of::

        suspend, resume, restart_miner, download_logs, check_updates,
        find_keys, toggle_tools, toggle_fancontrol, toggle_overclock,
        delete_all, install_key
    """
    path = WMOC_MODULES["installer"]["path"]
    prefix = "cbid.wmoc_installer"
    buttons = {
        "suspend": "_suspend_button",
        "resume": "_resume_button",
        "restart_miner": "_restartminer_button",
        "download_logs": "_logs_button",
        "check_updates": "_update_button",
        "find_keys": "_find_button",
        "toggle_tools": "_tools_button",
        "toggle_fancontrol": "_fancontrol_button",
        "toggle_overclock": "_overclock_button",
        "delete_all": "_deleteall_button",
    }
    overrides: dict[str, Any] = {}
    if key is not None:
        overrides["mykey"] = key
    if action == "install_key":
        if not key:
            raise ValueError("install_key requires key=")
        # submitting key field with update/find style — use find after setting key
        return _cbi_post(
            client,
            path,
            field_prefix=prefix,
            overrides=overrides,
            button="_find_button",
            dry_run=dry_run,
        )
    if action not in buttons:
        raise ValueError(f"unknown installer action {action!r}; choose from {sorted(buttons)}")
    return _cbi_post(
        client,
        path,
        field_prefix=prefix,
        overrides=overrides,
        button=buttons[action],
        dry_run=dry_run,
    )


def tools_apply(
    client: LuCIClient,
    *,
    powerlim: Optional[int | str] = None,
    utspeed: Optional[int | str] = None,
    watchdog: Optional[bool] = None,
    watchdog_threshold: Optional[int | str] = None,
    heating: Optional[bool] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Save & Apply WMOC Mining Tools settings (free tier).

    - ``powerlim``: watts, ``0`` = off
    - ``utspeed``: upfreq speed 0–9
    - ``watchdog`` / ``heating``: bool flags
    """
    path = WMOC_MODULES["tools"]["path"]
    prefix = "cbid.wmoc_tools"
    ovr: dict[str, Any] = {}
    if powerlim is not None:
        ovr["powerlim"] = str(int(powerlim) if str(powerlim).isdigit() else powerlim)
    if utspeed is not None:
        u = int(utspeed)
        if u < 0 or u > 9:
            raise ValueError("utspeed must be 0..9")
        ovr["utspeed"] = str(u)
    if watchdog is not None:
        ovr["watchdog_flag"] = "1" if watchdog else "0"
    if watchdog_threshold is not None:
        ovr["watchdog_treshold"] = str(watchdog_threshold)  # WMOC spelling
    if heating is not None:
        ovr["heating_flag"] = "1" if heating else "0"
    return _cbi_post(
        client,
        path,
        field_prefix=prefix,
        overrides=ovr,
        button="_start",
        dry_run=dry_run,
    )


def tools_action(
    client: LuCIClient,
    action: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """tools: check_updates | download_logs | restart_miner | disable | remove | refresh."""
    path = WMOC_MODULES["tools"]["path"]
    buttons = {
        "check_updates": "_update_button",
        "download_logs": "_logs_button",
        "restart_miner": "_restartminer_button",
        "disable": "_disable_button",
        "remove": "_remove_button",
        "refresh": "_reload_button",
    }
    if action not in buttons:
        raise ValueError(f"unknown tools action {action!r}")
    return _cbi_post(
        client,
        path,
        field_prefix="cbid.wmoc_tools",
        button=buttons[action],
        dry_run=dry_run,
    )


def overclock_apply_preset(
    client: LuCIClient,
    preset: int | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Apply AGP preset by index (1..) or label containing AGP_NN.

    Does **not** call Save&Apply OC settings separately — only «Apply Selected Preset».
    """
    path = WMOC_MODULES["overclock"]["path"]
    st = get_overclock_state(client)
    presets = st.get("presets") or []
    value: Optional[str] = None
    label: Optional[str] = None
    if isinstance(preset, int) or (isinstance(preset, str) and str(preset).isdigit()):
        value = str(int(preset))
        for p in presets:
            if str(p.get("value")) == value:
                label = p.get("label")
                break
    else:
        s = str(preset).upper()
        for p in presets:
            lab = (p.get("label") or "").upper()
            if s in lab or s == (p.get("name") or "").upper():
                value = str(p.get("value"))
                label = p.get("label")
                break
    if value is None or value == "0":
        raise ValueError(f"preset not found: {preset!r}")
    return _cbi_post(
        client,
        path,
        field_prefix="cbid.overclock",
        overrides={"profiles_list": value},
        button="_apply_preset",
        dry_run=dry_run,
    )


def overclock_apply_settings(
    client: LuCIClient,
    *,
    power_lim: Optional[int | str] = None,
    power_max: Optional[int | str] = None,
    target_hash: Optional[float | str] = None,
    target_vol: Optional[int | str] = None,
    board_temp: Optional[int | str] = None,
    chip_temp_protect: Optional[int | str] = None,
    fan_pwm: Optional[int | str] = None,
    fan_manual: Optional[bool] = None,
    liquid_cooling: Optional[bool] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Save & Apply Overclock Settings (partial field update)."""
    path = WMOC_MODULES["overclock"]["path"]
    ovr: dict[str, Any] = {}
    if power_lim is not None:
        ovr["power_lim"] = str(int(float(power_lim)))
    if power_max is not None:
        ovr["power_max"] = str(int(float(power_max)))
    if target_hash is not None:
        ovr["target_hash"] = str(target_hash)
    if target_vol is not None:
        ovr["target_vol"] = str(int(float(target_vol)))
    if board_temp is not None:
        ovr["board_temp"] = str(int(float(board_temp)))
    if chip_temp_protect is not None:
        ovr["chip_temp_protect"] = str(int(float(chip_temp_protect)))
    if fan_pwm is not None:
        ovr["fan_new_pwm"] = str(int(float(fan_pwm)))
    if fan_manual is not None:
        ovr["fan_manual_switch"] = "1" if fan_manual else "0"
    if liquid_cooling is not None:
        ovr["liquid_cooling"] = "1" if liquid_cooling else "0"
    return _cbi_post(
        client,
        path,
        field_prefix="cbid.overclock",
        overrides=ovr,
        button="apply_oc_settings",
        dry_run=dry_run,
    )


def overclock_action(
    client: LuCIClient,
    action: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """OC: generate_presets | reset_psu | hide | delete_module | check_updates."""
    path = WMOC_MODULES["overclock"]["path"]
    buttons = {
        "generate_presets": "_generate_button",
        "reset_psu": "_resetpsu_button",
        "hide": "_hide_button",
        "delete_module": "_deletemodule_button",
        "check_updates": "_update_button",
    }
    if action not in buttons:
        raise ValueError(f"unknown overclock action {action!r}")
    return _cbi_post(
        client,
        path,
        field_prefix="cbid.overclock",
        button=buttons[action],
        dry_run=dry_run,
    )


def get_installer_state(client: LuCIClient) -> dict[str, Any]:
    """
    Snapshot WMOC Package Installer page (buttons + key field).

    Buttons (POST cbi.submit with matching button name):
      _tools_button, _fancontrol_button, _overclock_button,
      _update_button, _find_button, _logs_button,
      _suspend_button, _resume_button, _restartminer_button,
      _deleteall_button
    Key field: ``cbid.wmoc_installer.1.mykey``
    """
    html = _get_page(client, WMOC_MODULES["installer"]["path"])
    fields = _html_field_values(html, "cbid.wmoc_installer")
    return {
        "ok": True,
        "path": WMOC_MODULES["installer"]["path"],
        "title": "WMOC Package Installer",
        "fields": fields,
        "actions": [
            "install_tools",
            "install_fancontrol",
            "install_overclock",
            "check_updates",
            "find_keys",
            "download_logs",
            "suspend",
            "resume",
            "restart_miner",
            "delete_all_modules",
        ],
    }


def get_tools_state(client: LuCIClient) -> dict[str, Any]:
    """WMOC Mining Tools: powerlim, utspeed, watchdog, heating flags."""
    html = _get_page(client, WMOC_MODULES["tools"]["path"])
    fields = _html_field_values(html, "cbid.wmoc_tools")
    # short keys
    short = {
        k.split(".")[-1]: v
        for k, v in fields.items()
        if not k.endswith("_button") and not k.endswith("_start")
    }
    return {
        "ok": True,
        "path": WMOC_MODULES["tools"]["path"],
        "title": "WMOC Mining Tools",
        "fields": fields,
        "powerlim": short.get("powerlim"),
        "utspeed": short.get("utspeed"),
        "watchdog_flag": short.get("watchdog_flag"),
        "watchdog_treshold": short.get("watchdog_treshold"),
        "heating_flag": short.get("heating_flag"),
        "actions": [
            "save_apply",
            "check_updates",
            "download_logs",
            "restart_miner",
            "disable",
            "remove",
        ],
    }


def get_overclock_state(client: LuCIClient) -> dict[str, Any]:
    """
    WMOC Overclock Full — presets AGP_*_xxTH_yyW, power_lim, temps, fans.

    Live lab (example): target_hash, power_lim, power_max, AGP presets 1..36.
    """
    html = _get_page(client, WMOC_MODULES["overclock"]["path"])
    fields = _html_field_values(html, "cbid.overclock")
    short = {k.split(".")[-1]: v for k, v in fields.items()}
    presets_raw = _html_options(html)
    # filter AGP presets only
    presets = []
    for o in presets_raw:
        lab = o.get("label") or ""
        if "AGP" in lab.upper() or "TH" in lab.upper() and "W" in lab.upper():
            parsed = parse_agp_preset_label(lab)
            presets.append({**o, **parsed})
    # de-dupe by value keeping AGP
    seen: set[str] = set()
    uniq = []
    for p in presets:
        v = p.get("value") or ""
        if v in seen or v == "0":
            continue
        if "AGP" not in (p.get("label") or "").upper() and p.get("ths") is None:
            continue
        seen.add(v)
        uniq.append(p)

    power_lim = short.get("power_lim")
    target_hash = short.get("target_hash")
    try:
        power_lim_f = float(power_lim) if power_lim not in (None, "") else None
    except ValueError:
        power_lim_f = None
    try:
        target_hash_f = float(target_hash) if target_hash not in (None, "") else None
    except ValueError:
        target_hash_f = None
    try:
        power_max_f = float(short["power_max"]) if short.get("power_max") else None
    except (KeyError, ValueError):
        power_max_f = None

    return {
        "ok": True,
        "path": WMOC_MODULES["overclock"]["path"],
        "title": "WMOC Overclock Full",
        "fields": fields,
        "short": short,
        "presets": uniq,
        "target_hash": target_hash_f,
        "target_vol": short.get("target_vol"),
        "power_lim": power_lim_f,
        "power_max": power_max_f,
        "min_power": short.get("min_power"),
        "board_temp": short.get("board_temp"),
        "chip_temp_protect": short.get("chip_temp_protect"),
        "fan_new_pwm": short.get("fan_new_pwm"),
        "liquid_cooling": short.get("liquid_cooling"),
        "actions": [
            "apply_preset",
            "save_apply_oc",
            "generate_agp_presets",
            "apply_fan",
            "reset_psu",
            "hide_module",
            "delete_module",
        ],
    }


def get_fancontrol_state(client: LuCIClient) -> dict[str, Any]:
    """WMOC Hashrate Splitter / pool switcher (v2)."""
    html = _get_page(client, WMOC_MODULES["fancontrol"]["path"])
    fields = _html_field_values(html, "cbid.fancontrol")
    short = {k.split(".")[-1]: v for k, v in fields.items()}
    return {
        "ok": True,
        "path": WMOC_MODULES["fancontrol"]["path"],
        "title": "WMOC Hashrate Splitter",
        "fields": fields,
        "short": short,
        "poolswitcher_flag": short.get("poolswitcher_flag"),
        "actions": ["apply_pool_switcher", "disable", "remove", "update"],
    }


def utility_snapshot(
    client: LuCIClient,
    *,
    miner_type: str | None = "M60",
    history_records: int = 5,
) -> dict[str, Any]:
    """
    One-shot view useful for poolheat / ops: modules + OC + tools + dual power.

    Combines detect, latest history, overclock targets, dual-wall estimate.
    """
    det = detect_on_client(client, login=True)
    out: dict[str, Any] = {
        "ok": True,
        "wmoc": det,
        "installer": None,
        "tools": None,
        "overclock": None,
        "fancontrol": None,
        "history_latest": None,
        "wall_power": None,
    }
    mods = det.get("modules") or {}
    try:
        if mods.get("installer", {}).get("active"):
            out["installer"] = get_installer_state(client)
    except Exception as e:
        out["installer"] = {"ok": False, "error": str(e)}
    try:
        if mods.get("tools", {}).get("active"):
            out["tools"] = get_tools_state(client)
    except Exception as e:
        out["tools"] = {"ok": False, "error": str(e)}
    try:
        if mods.get("overclock", {}).get("active"):
            out["overclock"] = get_overclock_state(client)
    except Exception as e:
        out["overclock"] = {"ok": False, "error": str(e)}
    try:
        if mods.get("fancontrol", {}).get("active"):
            out["fancontrol"] = get_fancontrol_state(client)
    except Exception as e:
        out["fancontrol"] = {"ok": False, "error": str(e)}

    thr = None
    prt = None
    try:
        hist = get_history(client, max_records=history_records)
        recs = hist.get("records") or []
        if recs:
            last = recs[-1]
            out["history_latest"] = last
            thr = last.get("total_hr")
            psu = last.get("psu") or {}
            prt = psu.get("power_rt")
    except Exception as e:
        out["history_error"] = str(e)

    oc = out.get("overclock") or {}
    if thr is not None:
        out["wall_power"] = estimate_dual_wall_power(
            float(thr),
            float(prt) if prt is not None else None,
            miner_type=miner_type,
            oc_power_target_w=oc.get("power_lim"),
            oc_power_max_w=oc.get("power_max"),
            oc_target_hash_ths=oc.get("target_hash"),
        )
    return out


# ── façade ───────────────────────────────────────────────────────────────────


class WMOCClient:
    """
    WMOC-focused façade over :class:`LuCIClient`.

    Prefer this module for all WMOC work; use plain LuCI for stock admin pages.
    """

    def __init__(self, luci: LuCIClient):
        self.luci = luci

    @classmethod
    def from_host(
        cls,
        host: str,
        *,
        username: str = "admin",
        password: str = "admin",
        timeout: float = 10.0,
        scheme: Optional[str] = None,
        port: Optional[int] = None,
        base_url: Optional[str] = None,
    ) -> "WMOCClient":
        luci = LuCIClient(
            host,
            username=username,
            password=password,
            timeout=timeout,
            scheme=scheme,
            port=port,
            base_url=base_url,
        )
        return cls(luci)

    def detect(self, *, login: bool = True, use_cache: bool = True) -> dict[str, Any]:
        return detect_on_client(self.luci, login=login, use_cache=use_cache)

    def has_wmoc(self, *, login: bool = True) -> bool:
        return has_wmoc(self.luci, login=login)

    def get_history(self, *, max_records: Optional[int] = None) -> dict[str, Any]:
        return get_history(self.luci, max_records=max_records)

    def analyze_psu(
        self,
        *,
        history: Optional[dict[str, Any]] = None,
        sample: int = 50,
    ) -> dict[str, Any]:
        return analyze_psu(history=history, client=self.luci, sample=sample)

    def estimate_wall_power(
        self,
        *,
        miner_type: str | None = "M60",
        joules_per_th: float | None = None,
        prefer: str = "mid",
        max_records: int | None = 100,
    ) -> dict[str, Any]:
        hist = self.get_history(max_records=max_records)
        return estimate_wall_power_from_history(
            hist,
            miner_type=miner_type,
            joules_per_th=joules_per_th,
            prefer=prefer,
        )

    def get_installer_state(self) -> dict[str, Any]:
        return get_installer_state(self.luci)

    def get_tools_state(self) -> dict[str, Any]:
        return get_tools_state(self.luci)

    def get_overclock_state(self) -> dict[str, Any]:
        return get_overclock_state(self.luci)

    def get_fancontrol_state(self) -> dict[str, Any]:
        return get_fancontrol_state(self.luci)

    def utility_snapshot(
        self,
        *,
        miner_type: str | None = "M60",
        history_records: int = 5,
    ) -> dict[str, Any]:
        return utility_snapshot(
            self.luci,
            miner_type=miner_type,
            history_records=history_records,
        )

    def estimate_dual_wall(
        self,
        *,
        miner_type: str | None = "M60",
        stock_j_per_th: float | None = None,
    ) -> dict[str, Any]:
        """Near-real dual-PSU wall power using history + OC power_lim/presets."""
        snap = self.utility_snapshot(miner_type=miner_type, history_records=3)
        wp = snap.get("wall_power") or {}
        if stock_j_per_th is not None and snap.get("history_latest"):
            last = snap["history_latest"]
            thr = float(last.get("total_hr") or 0)
            prt = (last.get("psu") or {}).get("power_rt")
            oc = snap.get("overclock") or {}
            return estimate_dual_wall_power(
                thr,
                float(prt) if prt is not None else None,
                miner_type=miner_type,
                stock_j_per_th=stock_j_per_th,
                oc_power_target_w=oc.get("power_lim"),
                oc_power_max_w=oc.get("power_max"),
                oc_target_hash_ths=oc.get("target_hash"),
            )
        return wp

    # ── writes ───────────────────────────────────────────────────────────────

    def installer_action(
        self,
        action: str,
        *,
        key: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return installer_action(self.luci, action, key=key, dry_run=dry_run)

    def suspend(self, *, dry_run: bool = False) -> dict[str, Any]:
        return self.installer_action("suspend", dry_run=dry_run)

    def resume(self, *, dry_run: bool = False) -> dict[str, Any]:
        return self.installer_action("resume", dry_run=dry_run)

    def restart_miner(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Prefer installer restart; falls back to tools if needed."""
        try:
            return self.installer_action("restart_miner", dry_run=dry_run)
        except Exception:
            return tools_action(self.luci, "restart_miner", dry_run=dry_run)

    def tools_apply(
        self,
        *,
        powerlim: Optional[int | str] = None,
        utspeed: Optional[int | str] = None,
        watchdog: Optional[bool] = None,
        watchdog_threshold: Optional[int | str] = None,
        heating: Optional[bool] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return tools_apply(
            self.luci,
            powerlim=powerlim,
            utspeed=utspeed,
            watchdog=watchdog,
            watchdog_threshold=watchdog_threshold,
            heating=heating,
            dry_run=dry_run,
        )

    def tools_action(self, action: str, *, dry_run: bool = False) -> dict[str, Any]:
        return tools_action(self.luci, action, dry_run=dry_run)

    def overclock_apply_preset(
        self,
        preset: int | str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return overclock_apply_preset(self.luci, preset, dry_run=dry_run)

    def overclock_apply_settings(self, *, dry_run: bool = False, **kwargs: Any) -> dict[str, Any]:
        return overclock_apply_settings(self.luci, dry_run=dry_run, **kwargs)

    def overclock_action(self, action: str, *, dry_run: bool = False) -> dict[str, Any]:
        return overclock_action(self.luci, action, dry_run=dry_run)

    def toggle_module(
        self,
        module: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Toggle tools | fancontrol | overclock via installer buttons."""
        key = {
            "tools": "toggle_tools",
            "fancontrol": "toggle_fancontrol",
            "overclock": "toggle_overclock",
        }.get(module)
        if not key:
            raise ValueError("module must be tools|fancontrol|overclock")
        return self.installer_action(key, dry_run=dry_run)


def detect_wmoc(
    host: str,
    *,
    username: str = "admin",
    password: str = "admin",
    timeout: float = 10.0,
    scheme: Optional[str] = None,
    port: Optional[int] = None,
    base_url: Optional[str] = None,
    login: bool = True,
) -> dict[str, Any]:
    """
    Probe a miner for WMOC firmware/tools via LuCI.

    Example::

        detect_wmoc("10.121.15.76:8788")
        detect_wmoc("10.121.15.76", port=8788, scheme="http")
    """
    return WMOCClient.from_host(
        host,
        username=username,
        password=password,
        timeout=timeout,
        scheme=scheme,
        port=port,
        base_url=base_url,
    ).detect(login=login)
