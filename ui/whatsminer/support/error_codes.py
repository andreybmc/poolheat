"""
WhatsMiner error codes with i18n descriptions.

Source: WhatsMinerTool 9.2.4 ``data/error_code.txt`` (GBK; en + Chinese).
Shipped as JSON under :mod:`whatsminer.i18n.errors`::

    i18n/errors/en.json
    i18n/errors/zh.json
    i18n/errors/ru.json   # optional overrides; empty → fall back to en
    i18n/errors/meta.json

Usage::

    from whatsminer.support.error_codes import describe_error, resolve_error

    describe_error(110, lang="en")
    # "Intake Fan detect speed error"
    describe_error(110, lang="zh")
    # "进风口风扇转速探测错误"
    describe_error(110, lang="ru")  # no RU yet → English fallback

    resolve_error(110, lang="zh", timestamp="2026-08-06 12:00:00")
    # {"code": "110", "lang": "zh", "message": "...", "cause": "...", ...}
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

_ENTRY = re.compile(r"^\[(\d+)\]\s*$")
_EN = re.compile(r"^en=(.*)$")
_CH = re.compile(r"^ch=(.*)$")

# Package data root (installed wheel / editable checkout)
_I18N_DIR = Path(__file__).resolve().parent / "i18n" / "errors"

# Extra short codes seen on modern FW but missing from WMT 9.2.4 short dump
_EXTRA_STATIC: dict[str, dict[str, str]] = {
    "2000": {
        "en": "No pools config",
        "zh": "未配置矿池",
        "ru": "Пулы не настроены",
    },
    "330": {
        "en": "Env temperature reading error",
        "zh": "环境温度读取错误",
        "ru": "Ошибка чтения температуры окружающей среды",
    },
    "335": {
        "en": "Inlet temperature reading error",
        "zh": "进液温度读取错误",
        "ru": "Ошибка чтения температуры на входе",
    },
    "340": {
        "en": "Slot 0 temperature limited",
        "zh": "算力板0 温度限频",
        "ru": "Слот 0: ограничение по температуре",
    },
    "341": {
        "en": "Slot 1 temperature limited",
        "zh": "算力板1 温度限频",
        "ru": "Слот 1: ограничение по температуре",
    },
    "342": {
        "en": "Slot 2 temperature limited",
        "zh": "算力板2 温度限频",
        "ru": "Слот 2: ограничение по температуре",
    },
    "343": {
        "en": "Slot 3 temperature limited",
        "zh": "算力板3 温度限频",
        "ru": "Слот 3: ограничение по температуре",
    },
    "360": {
        "en": "Board temperature overheat",
        "zh": "算力板过热",
        "ru": "Перегрев хешплаты",
    },
    "652": {
        "en": "Liquid temperature unstable",
        "zh": "液冷温度不稳定",
        "ru": "Нестабильная температура жидкости",
    },
    "704": {
        "en": "Control board cpu freq error",
        "zh": "控制板CPU频率错误",
        "ru": "Ошибка частоты CPU контрольной платы",
    },
    "5130": {
        "en": "Slot 0 upfreq unstable",
        "zh": "算力板0 升频不稳定",
        "ru": "Слот 0: нестабильный разгон (upfreq)",
    },
    "5131": {
        "en": "Slot 1 upfreq unstable",
        "zh": "算力板1 升频不稳定",
        "ru": "Слот 1: нестабильный разгон (upfreq)",
    },
    "5132": {
        "en": "Slot 2 upfreq unstable",
        "zh": "算力板2 升频不稳定",
        "ru": "Слот 2: нестабильный разгон (upfreq)",
    },
    "5133": {
        "en": "Slot 3 upfreq unstable",
        "zh": "算力板3 升频不稳定",
        "ru": "Слот 3: нестабильный разгон (upfreq)",
    },
    "5410": {
        "en": "Slot 0 uart init error",
        "zh": "算力板0 UART初始化错误",
        "ru": "Слот 0: ошибка инициализации UART",
    },
    "5431": {
        "en": "Slot 1 boot fail with no data",
        "zh": "算力板1 启动失败无数据",
        "ru": "Слот 1: сбой загрузки, нет данных",
    },
    "5720": {
        "en": "Slot 0 crc error too much",
        "zh": "算力板0 CRC错误过多",
        "ru": "Слот 0: слишком много CRC-ошибок",
    },
    "6010": {
        "en": "Slot 0 disable chips too many",
        "zh": "算力板0 禁用芯片过多",
        "ru": "Слот 0: отключено слишком много чипов",
    },
    # Real FW/WMOC codes (short title only). Full runtime reason may append
    # chip list: "Slot2 chips been reset, U1-U2-U3-…" — that U-list is NOT
    # in the static catalog; prefer the miner/WMOC cause string when present.
    "550999": {
        "en": "Slot0 chips been reset",
        "zh": "算力板0 芯片被复位",
        "ru": "Слот 0: чипы были сброшены (reset)",
    },
    "551999": {
        "en": "Slot1 chips been reset",
        "zh": "算力板1 芯片被复位",
        "ru": "Слот 1: чипы были сброшены (reset)",
    },
    "552999": {
        "en": "Slot2 chips been reset",
        "zh": "算力板2 芯片被复位",
        "ru": "Слот 2: чипы были сброшены (reset)",
    },
    "553999": {
        "en": "Slot3 chips been reset",
        "zh": "算力板3 芯片被复位",
        "ru": "Слот 3: чипы были сброшены (reset)",
    },
    "580999": {
        "en": "Slot0 too many chips error",
        "zh": "算力板0 坏芯片过多",
        "ru": "Слот 0: слишком много неисправных чипов",
    },
    "581999": {
        "en": "Slot1 too many chips error",
        "zh": "算力板1 坏芯片过多",
        "ru": "Слот 1: слишком много неисправных чипов",
    },
    "582999": {
        "en": "Slot2 too many chips error",
        "zh": "算力板2 坏芯片过多",
        "ru": "Слот 2: слишком много неисправных чипов",
    },
    "583999": {
        "en": "Slot3 too many chips error",
        "zh": "算力板3 坏芯片过多",
        "ru": "Слот 3: слишком много неисправных чипов",
    },
}

# Language aliases (BCP-47-ish → our file stem)
_LANG_ALIASES: dict[str, str] = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "en-us": "en",
    "en_us": "en",
    "en-gb": "en",
    "zh": "zh",
    "zh-cn": "zh",
    "zh_cn": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",  # WMT only ships simplified; still map here
    "cn": "zh",
    "ch": "zh",  # WMT key name
    "chinese": "zh",
    "ru": "ru",
    "ru-ru": "ru",
    "ru_ru": "ru",
    "russian": "ru",
}

_DEFAULT_FALLBACK = ("en",)


def i18n_dir() -> Path:
    """Directory with ``en.json`` / ``zh.json`` / ``meta.json``."""
    return _I18N_DIR


def normalize_lang(lang: str | None) -> str:
    if not lang:
        return "en"
    key = str(lang).strip().lower().replace("_", "-")
    # full tag then primary subtag
    if key in _LANG_ALIASES:
        return _LANG_ALIASES[key]
    primary = key.split("-", 1)[0]
    return _LANG_ALIASES.get(primary, primary)


@lru_cache(maxsize=1)
def load_meta() -> dict[str, Any]:
    path = _I18N_DIR / "meta.json"
    if not path.is_file():
        return {
            "count": 0,
            "languages": ["en"],
            "fallback": list(_DEFAULT_FALLBACK),
            "aliases": dict(_LANG_ALIASES),
        }
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=16)
def load_language(lang: str) -> dict[str, str]:
    """Load ``{code: message}`` for a language file stem (en/zh/ru/…).

    Merges static extras (modern FW codes not in WMT 9.2.4 short dump).
    """
    stem = normalize_lang(lang)
    path = _I18N_DIR / f"{stem}.json"
    out: dict[str, str] = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out = {str(k): str(v) for k, v in data.items()}
    # overlay extras for this lang (en/zh/ru); prefer file if already present
    lang_key = stem if stem in ("en", "zh", "ru") else "en"
    for code, msgs in _EXTRA_STATIC.items():
        if code not in out:
            out[code] = msgs.get(lang_key) or msgs.get("en") or ""
    return {k: v for k, v in out.items() if v}


def is_known_code(code: str | int) -> bool:
    """True if code is in the shipped catalog (WMT JSON + static extras)."""
    return str(code).strip() in load_language("en")


def available_languages() -> list[str]:
    """Language stems that have at least one translation entry."""
    langs: list[str] = []
    if not _I18N_DIR.is_dir():
        return ["en"]
    for p in sorted(_I18N_DIR.glob("*.json")):
        if p.name == "meta.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data:
            langs.append(p.stem)
    # always advertise en if file exists even empty
    if "en" not in langs and (_I18N_DIR / "en.json").is_file():
        langs.insert(0, "en")
    return langs or ["en"]


def list_codes() -> list[str]:
    """All known error codes (from English table)."""
    table = load_language("en")
    return sorted(table.keys(), key=lambda c: int(c) if c.isdigit() else c)


def describe_error(
    code: str | int,
    *,
    lang: str = "en",
    fallback: Iterable[str] | None = None,
    default: str | None = None,
) -> str:
    """
    Short catalog description for a miner error code.

    Lookup is **exact code only** (WMT JSON + known FW extras such as
    ``552999``). No invented encoding / pattern expansion.

    Note: WMOC/runtime cause may be longer than the catalog line, e.g.
    ``Slot2 chips been reset, U1-U2-U3-…``. Prefer the live miner/WMOC
    reason string when available; use this as fallback title only.
    """
    c = str(code).strip()
    want = normalize_lang(lang)
    chain: list[str] = [want]
    if fallback is not None:
        chain.extend(normalize_lang(x) for x in fallback)
    else:
        meta_fb = load_meta().get("fallback") or list(_DEFAULT_FALLBACK)
        chain.extend(normalize_lang(x) for x in meta_fb)
    # de-dupe
    seen: set[str] = set()
    for stem in chain:
        if stem in seen:
            continue
        seen.add(stem)
        msg = load_language(stem).get(c)
        if msg:
            return msg
    if default is not None:
        return default
    return f"Unknown error {c}"


def resolve_error(
    code: str | int,
    *,
    lang: str = "en",
    timestamp: str | None = None,
    fallback: Iterable[str] | None = None,
    cause: str | None = None,
) -> dict[str, Any]:
    """
    Structured error for API responses.

    If ``cause`` is provided (native WMOC/firmware reason, possibly with
    chip list ``U1-U2-…``), it is used as message/cause and ``known`` is
    True. Otherwise falls back to the short catalog title.

    Returns::

        {
          "code": "110",
          "lang": "zh",
          "requested_lang": "zh",
          "message": "...",
          "cause": "...",
          "known": true,
          "timestamp": "..."     # optional
        }
    """
    c = str(code).strip()
    want = normalize_lang(lang)
    native = (str(cause).strip() if cause not in (None, "") else "")
    # ignore useless placeholders
    if native and native.lower() in (f"error code {c}".lower(), f"error {c}".lower()):
        native = ""

    if native:
        message = native
        used = want
        known = True
    else:
        message = describe_error(c, lang=want, fallback=fallback, default="")
        if not message:
            message = f"Unknown error {c}"
            used = want
            known = False
        else:
            known = is_known_code(c)
            used = want
            if load_language(want).get(c) != message:
                for stem in list(fallback or load_meta().get("fallback") or _DEFAULT_FALLBACK):
                    s = normalize_lang(stem)
                    if load_language(s).get(c) == message:
                        used = s
                        break
                else:
                    used = "en" if known else want
    out: dict[str, Any] = {
        "code": c,
        "lang": used,
        "requested_lang": want,
        "message": message,
        "cause": message,
        "known": known,
    }
    if timestamp is not None:
        out["timestamp"] = timestamp
    if native:
        out["native_cause"] = True
    return out


def resolve_errors(
    codes: Iterable[str | int | dict[str, Any]],
    *,
    lang: str = "en",
) -> list[dict[str, Any]]:
    """
    Resolve many codes. Accepts plain codes or ``{code: ts}`` / ErrorCode-like dicts.
    """
    out: list[dict[str, Any]] = []
    for item in codes:
        if isinstance(item, dict):
            # {"110": "2022-…"} single-key map or {code, timestamp}
            if "code" in item:
                out.append(
                    resolve_error(
                        item["code"],
                        lang=lang,
                        timestamp=item.get("timestamp") or item.get("time"),
                    )
                )
            elif len(item) == 1:
                k, v = next(iter(item.items()))
                out.append(resolve_error(k, lang=lang, timestamp=str(v) if v else None))
            else:
                out.append(resolve_error(item.get("code", "?"), lang=lang))
        else:
            out.append(resolve_error(item, lang=lang))
    return out


def enrich_error_codes(
    raw: Any,
    *,
    lang: str = "en",
) -> list[dict[str, Any]]:
    """
    Parse typical ``get_error_code`` Msg shapes and attach i18n cause.

    Accepts:

    - ``{"error_code": {"110": "2022-01-17 11:28:11", ...}}``
    - ``{"error_code": [{"110": "..."}, ...]}``
    - list/dict of codes
    """
    # (code, timestamp, native_cause?)
    items: list[tuple[str, str | None, str | None]] = []
    if isinstance(raw, dict) and "error_code" in raw:
        raw = raw["error_code"]
    if isinstance(raw, dict) and "Msg" in raw:
        msg = raw["Msg"]
        if isinstance(msg, dict) and "error_code" in msg:
            raw = msg["error_code"]
    if isinstance(raw, dict):
        # may be {code: ts} or nested
        if all(str(k).isdigit() or str(k).isdecimal() for k in raw.keys()):
            for k, v in raw.items():
                if isinstance(v, dict):
                    ts = v.get("time") or v.get("Time") or v.get("timestamp")
                    cause = (
                        v.get("cause")
                        or v.get("Cause")
                        or v.get("error_message")
                        or v.get("reason")
                    )
                    items.append(
                        (
                            str(k),
                            str(ts) if ts is not None else None,
                            str(cause) if cause not in (None, "") else None,
                        )
                    )
                else:
                    items.append((str(k), str(v) if v is not None else None, None))
        elif "error_code" in raw:
            return enrich_error_codes(raw["error_code"], lang=lang)
    elif isinstance(raw, list):
        for el in raw:
            if isinstance(el, dict):
                if "code" in el or "ErrorCode" in el or "error_code" in el:
                    code = el.get("code") or el.get("ErrorCode") or el.get("error_code")
                    ts = el.get("timestamp") or el.get("time") or el.get("Time")
                    cause = (
                        el.get("cause")
                        or el.get("Cause")
                        or el.get("error_message")
                        or el.get("reason")
                    )
                    items.append(
                        (
                            str(code),
                            str(ts) if ts is not None else None,
                            str(cause) if cause not in (None, "") else None,
                        )
                    )
                else:
                    for k, v in el.items():
                        items.append((str(k), str(v) if v is not None else None, None))
            else:
                items.append((str(el), None, None))
    else:
        return []

    return [
        resolve_error(code, lang=lang, timestamp=ts, cause=cause)
        for code, ts, cause in items
    ]


# ── legacy: parse original WMT error_code.txt ────────────────────────────────


def load_error_codes(path: str | Path) -> dict[str, dict[str, str]]:
    """
    Parse WhatsMinerTool ``error_code.txt`` (GBK or UTF-8).

    Returns: ``{ "100": {"en": "...", "zh": "..."}, ... }``
    (``ch`` from file is stored as ``zh``).
    """
    path = Path(path)
    raw = path.read_bytes()
    text: str | None = None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    out: dict[str, dict[str, str]] = {}
    code: str | None = None
    for line in text.splitlines():
        line = line.strip()
        m = _ENTRY.match(line)
        if m:
            code = m.group(1)
            out[code] = {}
            continue
        if code is None:
            continue
        me = _EN.match(line)
        if me:
            out[code]["en"] = me.group(1).strip()
            continue
        mc = _CH.match(line)
        if mc:
            # WMT key "ch" → i18n "zh"
            out[code]["zh"] = mc.group(1).strip()
            out[code]["ch"] = out[code]["zh"]
    return out


def describe(code: str | int, table: dict[str, dict[str, str]], lang: str = "en") -> str:
    """Legacy helper using an in-memory table from :func:`load_error_codes`."""
    entry = table.get(str(code), {})
    want = normalize_lang(lang)
    if want == "zh":
        return entry.get("zh") or entry.get("ch") or entry.get("en") or f"Unknown error {code}"
    return entry.get(want) or entry.get("en") or f"Unknown error {code}"


def clear_cache() -> None:
    """Drop loaded language tables (for tests / reload)."""
    load_language.cache_clear()
    load_meta.cache_clear()
