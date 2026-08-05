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
    """Load ``{code: message}`` for a language file stem (en/zh/ru/…)."""
    stem = normalize_lang(lang)
    path = _I18N_DIR / f"{stem}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


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

    Fallback chain: requested lang → meta fallback (usually ``en``) →
    ``default`` or ``"Unknown error {code}"``.
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
    known = bool(message)
    if not known:
        message = f"Unknown error {c}"
        used = want
    else:
        # detect which lang actually provided the text
        used = want
        if load_language(want).get(c) != message:
            for stem in list(fallback or load_meta().get("fallback") or _DEFAULT_FALLBACK):
                s = normalize_lang(stem)
                if load_language(s).get(c) == message:
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
        "known": known and c in load_language("en"),
    }
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
