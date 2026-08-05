# Whatsminer capture proxy

Локальный TCP‑прокси на ноутбуке: **эмулирует порты ASIC**, всё гонит на реальный майнер (`target_host`, по умолчанию `192.168.1.10`) и **пишет лог** — чтобы увидеть, как **WhatsMinerTools** управляет асиком при **выключенном** public API / Miner API Switch.

## Зачем

С `apiswitch=0` / «can't access write cmd» классический TCP `:4028` write часто закрыт, а Tools всё равно делает Suspend / Power Mode / Limit.  
Типичные каналы Tools:

| Порт | Что это |
|------|---------|
| **8889** | Remote Ctrl (проприетарный, часто 16‑байтный challenge) — главный кандидат при выкл. API |
| **4028** | Classic BTMiner JSON (read + encrypted privileged) |
| **4433** | API v3 (length‑prefixed JSON, `system.apiswitch`) |
| **80/443** | LuCI web (форма Power / `open_by_api`) |

Прокcи слушает те же порты (или 8080/8443 без sudo) на Mac → forward на ASIC.

## Быстрый старт

```bash
cd tools/whatsminer-proxy
cp config.example.json config.json
# при необходимости: "target_host": "192.168.1.10"
python3 proxy.py
```

Узнайте IP ноутбука в LAN (например `192.168.1.34`):

```bash
ipconfig getifaddr en0
```

### WhatsMinerTools

1. В Tools укажите **IP = ноутбук** (не `192.168.1.10`).
2. Порты Tools оставьте стандартные (**8889**, web и т.д.).
3. Убедитесь, что на Mac **не заняты** 4028/4433/8889 (закройте локальный poolheat, если слушает те же порты).
4. Делайте в Tools: Enable API, Mining Control, Power Mode, Power Limit — **с выключенным API** на асике.
5. Смотрите консоль и `logs/`.

### poolheat (опционально)

В UI / `config.json` Peak временно:

```json
"miner_host": "192.168.1.34"
```

(или IP Mac) — тогда poolheat тоже ходит через прокси.

## Конфиг

`config.json` (см. `config.example.json`):

```json
{
  "listen_host": "0.0.0.0",
  "target_host": "192.168.1.10",
  "log_dir": "logs",
  "ports": [
    { "name": "api_v2", "listen": 4028, "target": 4028, "parse": "json_line", "enabled": true },
    { "name": "api_v3", "listen": 4433, "target": 4433, "parse": "json_len_le", "enabled": true },
    { "name": "tools_remote", "listen": 8889, "target": 8889, "parse": "tools_8889", "enabled": true },
    { "name": "https_tcp", "listen": 8443, "target": 443, "parse": "raw", "enabled": true },
    { "name": "http", "listen": 8080, "target": 80, "parse": "http", "enabled": true }
  ]
}
```

- Порты **&lt; 1024** (80/443) на macOS → `sudo python3 proxy.py` и `"enabled": true` у `https` / `http80`.
- Без sudo: Tools web → `http://MAC:8080` / `https://MAC:8443` (TLS без MITM — только ciphertext, но видно факт обращения).

## Логи

| Файл | Содержимое |
|------|------------|
| `logs/sessions.jsonl` | Каждое распарсенное сообщение: dir, summary, json, hex_head |
| `logs/raw/CONN-*.log` | Полный hex+ascii dump TCP‑сессии |

Полезные фильтры:

```bash
# все записи Remote :8889
grep tools_remote logs/sessions.jsonl | python3 -m json.tool --no-ensure-ascii | head

# JSON cmd на 4028/4433
grep '"json"' logs/sessions.jsonl | head

# LuCI / open_by_api
grep -i 'open_by_api\|apiswitch\|cbid' logs/sessions.jsonl logs/raw/*
```

## Что смотреть при «API off»

1. **Первый пакет на :8889** — 16 байт challenge? (логируется `likely=fixed_16`).
2. После auth — есть ли JSON / бинарные cmd с power_mode / power_off.
3. Параллельно **:443** / **:80** — POST LuCI `open_by_api=1` (как уже умеет poolheat).
4. **:4028** `enc=1` privileged — пишет ли Tools в encrypted channel без «public» switch.
5. **:4433** `set.miner.*` при `apiswitch=0` — отказ или успех.

## Результаты live-capture (2026-08-05)

См. **[CAPTURE-NOTES.md](./CAPTURE-NOTES.md)** и `pcap/wmt.pcap` (~30 MB).

Кратко: при API off все write WMT (mode / limit / MC / reboot / factory / pools) идут через **:8889**, не через 4028/4433.  
Прокси на Mac для 8889 **не подходит** (auth); захват — `tcpdump` на Peak на IP майнера.

## Ограничения

- **Не MITM HTTPS** (сертификат ASIC). Для расшифровки LuCI нужен отдельный mitmproxy + доверие к CA, либо уже открытый HTTP.
- **:8889** write-команды всё ещё opaque (80/128 B); poll = 64 B auth + ZZ-framed telemetry.
- Прокси **не** подменяет ответы (transparent forward) — асик остаётся source of truth.

## CLI

```bash
python3 proxy.py -c config.json
python3 proxy.py --target 192.168.1.10
python3 proxy.py --list-only
```
