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

# Firmware composite codes: ABCXYZ → base ABC (slot family) + sub XYZ (chip / special).
# Observed on M6x logs (miner-state): e.g. 552999 = Slot2 chips been reset.
_SLOT_BY_BASE3: dict[str, int] = {
    # few / error / zero nonce, chip id / bad chips / balance / xfer / reset
    "520": 0, "521": 1, "522": 2, "523": 3,
    "530": 0, "531": 1, "532": 2, "533": 3,
    "540": 0, "541": 1, "542": 2, "543": 3,
    "550": 0, "551": 1, "552": 2, "553": 3,
    "560": 0, "561": 1, "562": 2, "563": 3,
    "570": 0, "571": 1, "572": 2, "573": 3,
    "580": 0, "581": 1, "582": 2, "583": 3,
}

# Family prefix (first 2 of base3) → message templates per lang.
# {slot} 0..3, {chip} int subcode, {Slot} "Slot0".."Slot3"
_COMPOSITE_FAMILY: dict[str, dict[str, dict[str, str]]] = {
    # 55x999 — mass chip reset after board re-init (not in WMT short list)
    "55": {
        "en": {
            "999": "Slot{slot} chips been reset",
            "chip": "Slot{slot} chip{chip} have bad chips",
        },
        "zh": {
            "999": "算力板{slot} 芯片被复位",
            "chip": "算力板{slot} 芯片{chip} 坏芯片",
        },
        "ru": {
            "999": "Слот {slot}: чипы были сброшены (reset)",
            "chip": "Слот {slot}: чип {chip} неисправен",
        },
    },
    # 58xNNN — per-chip bad; 58x999 — too many bad chips
    "58": {
        "en": {
            "999": "Slot{slot} too many chips error",
            "chip": "Slot{slot} chip{chip} is bad",
        },
        "zh": {
            "999": "算力板{slot} 坏芯片过多",
            "chip": "算力板{slot} 芯片{chip} 损坏",
        },
        "ru": {
            "999": "Слот {slot}: слишком много неисправных чипов",
            "chip": "Слот {slot}: чип {chip} неисправен",
        },
    },
    # 53xNNN — few nonce on chip N (WMT only lists chips 1–3)
    "53": {
        "en": {
            "999": "Slot{slot} too many chips few nonce",
            "chip": "Slot{slot} chip{chip} few nonce",
        },
        "zh": {
            "999": "算力板{slot} 芯片nonce过少过多",
            "chip": "算力板{slot} 芯片{chip} nonce过少",
        },
        "ru": {
            "999": "Слот {slot}: слишком много чипов с малым nonce",
            "chip": "Слот {slot}: чип {chip} — мало nonce",
        },
    },
    # 56xNNN — zero nonce
    "56": {
        "en": {
            "999": "Slot{slot} too many chips zero nonce",
            "chip": "Slot{slot} chip{chip} zero nonce",
        },
        "zh": {
            "999": "算力板{slot} 零nonce芯片过多",
            "chip": "算力板{slot} 芯片{chip} 零nonce",
        },
        "ru": {
            "999": "Слот {slot}: слишком много чипов с zero nonce",
            "chip": "Слот {slot}: чип {chip} — zero nonce",
        },
    },
    # 52xNNN — error nonce
    "52": {
        "en": {
            "999": "Slot{slot} too many chips error nonce",
            "chip": "Slot{slot} chip{chip} error nonce",
        },
        "zh": {
            "999": "算力板{slot} 错误nonce芯片过多",
            "chip": "算力板{slot} 芯片{chip} 错误nonce",
        },
        "ru": {
            "999": "Слот {slot}: слишком много чипов с error nonce",
            "chip": "Слот {slot}: чип {chip} — error nonce",
        },
    },
    # 54xNNN — chip temp protect (extended beyond WMT 0–3)
    "54": {
        "en": {
            "999": "Slot{slot} chip temp protected (many)",
            "chip": "Slot{slot} chip{chip} temp protected",
        },
        "zh": {
            "999": "算力板{slot} 多芯片温度保护",
            "chip": "算力板{slot} 芯片{chip} 温度保护",
        },
        "ru": {
            "999": "Слот {slot}: термозащита многих чипов",
            "chip": "Слот {slot}: чип {chip} — термозащита",
        },
    },
    # 57xNNN — xfer / crc style
    "57": {
        "en": {
            "999": "Slot{slot} too many xfer/crc chip errors",
            "chip": "Slot{slot} chip{chip} xfer error",
        },
        "zh": {
            "999": "算力板{slot} 传输/CRC错误过多",
            "chip": "算力板{slot} 芯片{chip} 传输错误",
        },
        "ru": {
            "999": "Слот {slot}: слишком много xfer/CRC ошибок чипов",
            "chip": "Слот {slot}: чип {chip} — xfer error",
        },
    },
}

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
    # 55x999 / 58x999 as static too (also covered by patterns)
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


def parse_composite_code(code: str | int) -> dict[str, Any] | None:
    """
    Parse firmware 6-digit composite ``ABCXYZ`` (base slot family + chip/sub).

    Returns ``None`` if not a recognized composite. Example::

        parse_composite_code(552999)
        # {"code": "552999", "base": "552", "slot": 2, "sub": 999, "family": "55"}
    """
    c = str(code).strip()
    if not c.isdigit() or len(c) != 6:
        return None
    base = c[:3]
    if base not in _SLOT_BY_BASE3:
        return None
    family = base[:2]
    if family not in _COMPOSITE_FAMILY:
        return None
    return {
        "code": c,
        "base": base,
        "slot": _SLOT_BY_BASE3[base],
        "sub": int(c[3:]),
        "family": family,
    }


def composite_message(code: str | int, *, lang: str = "en") -> str | None:
    """
    Human text for firmware composite codes (e.g. ``552999`` → Slot2 chips been reset).

    Returns ``None`` if the code is not a known composite pattern.
    """
    info = parse_composite_code(code)
    if not info:
        return None
    want = normalize_lang(lang)
    fam = _COMPOSITE_FAMILY[info["family"]]
    # lang → en fallback for templates
    tmpl_set = fam.get(want) or fam.get("en") or {}
    sub = int(info["sub"])
    slot = int(info["slot"])
    key = "999" if sub == 999 else "chip"
    tmpl = tmpl_set.get(key) or (fam.get("en") or {}).get(key)
    if not tmpl:
        return None
    return tmpl.format(slot=slot, chip=sub, Slot=f"Slot{slot}")


def is_known_code(code: str | int) -> bool:
    """True if code is in shipped tables or matches a composite pattern."""
    c = str(code).strip()
    if c in load_language("en"):
        return True
    return parse_composite_code(c) is not None


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
    Human-readable description for a miner error code.

    Fallback chain:
    1. exact entry in requested lang / meta fallback (usually ``en``)
    2. firmware composite pattern (e.g. ``552999`` Slot2 chips been reset)
    3. 3-digit base family for 6-digit codes (e.g. ``552`` have bad chips)
    4. ``default`` or ``"Unknown error {code}"``
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
    # composite patterns (55x999, 58xNNN, …)
    for stem in chain:
        msg = composite_message(c, lang=stem)
        if msg:
            return msg
    # 6-digit → base 3-digit catalog (WMT short codes)
    if c.isdigit() and len(c) == 6:
        base = c[:3]
        for stem in chain:
            msg = load_language(stem).get(base)
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
) -> dict[str, Any]:
    """
    Structured error for API responses.

    Returns::

        {
          "code": "110",
          "lang": "zh",          # language actually used for message
          "requested_lang": "zh",
          "message": "...",      # same as cause (WMT has a single line)
          "cause": "...",
          "known": true,
          "timestamp": "..."     # optional, if provided
        }
    """
    c = str(code).strip()
    want = normalize_lang(lang)
    message = describe_error(c, lang=want, fallback=fallback, default="")
    if not message:
        message = f"Unknown error {c}"
        used = want
        known = False
    else:
        known = is_known_code(c) or (
            bool(message) and not message.startswith("Unknown error ")
        )
        # detect which lang actually provided the text
        used = want
        if load_language(want).get(c) != message:
            # composite in requested lang?
            if composite_message(c, lang=want) == message:
                used = want
            else:
                for stem in list(fallback or load_meta().get("fallback") or _DEFAULT_FALLBACK):
                    s = normalize_lang(stem)
                    if load_language(s).get(c) == message or composite_message(c, lang=s) == message:
                        used = s
                        break
                else:
                    used = "en"
    out: dict[str, Any] = {
        "code": c,
        "lang": used,
        "requested_lang": want,
        "message": message,
        "cause": message,
        "known": known,
    }
    comp = parse_composite_code(c)
    if comp:
        out["base"] = comp["base"]
        out["slot"] = comp["slot"]
        out["chip"] = None if int(comp["sub"]) == 999 else int(comp["sub"])
        out["sub"] = int(comp["sub"])
    if timestamp is not None:
        out["timestamp"] = timestamp
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
    items: list[tuple[str, str | None]] = []
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
                items.append((str(k), str(v) if v is not None else None))
        elif "error_code" in raw:
            return enrich_error_codes(raw["error_code"], lang=lang)
    elif isinstance(raw, list):
        for el in raw:
            if isinstance(el, dict):
                if "code" in el:
                    items.append((str(el["code"]), el.get("timestamp") or el.get("time")))
                else:
                    for k, v in el.items():
                        items.append((str(k), str(v) if v is not None else None))
            else:
                items.append((str(el), None))
    else:
        return []

    return [
        resolve_error(code, lang=lang, timestamp=ts) for code, ts in items
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
