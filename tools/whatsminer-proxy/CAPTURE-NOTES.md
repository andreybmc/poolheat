# WhatsMinerTools capture notes (M63, API off)

**Date:** 2026-08-05  
**Miner:** `192.168.1.10` · M63_VK2A · fw `20250915.16.Rel2` · MAC `CE:53:16:00:03:D8`  
**Capture:** Peak `tcpdump -i any -s 0 -w wmt.pcap host 192.168.1.10 and (port 8889 or 4028 or 4433 or 80 or 443)`  
**Artifact:** `tools/whatsminer-proxy/pcap/wmt.pcap` (~30 MB, 125k frames)  
**Tools client LAN IPs seen:** `192.168.1.201`, `192.168.1.200`, hairpin as `192.168.1.1`

---

## 1. Executive summary

With **Miner API Switch OFF** (`apiswitch=0` / `MinerApiSwitch=0`):

| Action in WMT | Applied? | Wire path for write |
|---------------|----------|---------------------|
| Connect / read telemetry | yes | **4028** read + **4433** `get.device.info` + **8889** status push |
| Performance Mode (Power Mode) | yes | **:8889** (no 4028 `set_*_power` seen) |
| Power Limit (e.g. 2200) | yes | **:8889** (no `adjust_power_limit` seen) |
| Mining Control Suspend/Resume | yes | **:8889** (`mineroff` true/false; no `power_off`/`power_on`) |
| Enable API | **no** | UI: *need change password*; switch stayed **0** |
| Reboot | yes | **:8889** (Uptime reset; no 4028 `reboot`) |
| Restore Factory Settings | yes | **:8889** (e.g. limit cleared; no 4028 `factory_reset`) |
| Pools (worker rename) | yes | **:8889** (`admin.test`→`admin.test1`; no `update_pools`) |

**Public write paths failed or unused in this session:**

- **4028** `get_token` → mostly **`Code 136 over max connect`** (poolheat + WMT concurrent load).
- **4433** `set.miner.service` `param=stop` (account super) → **`code -4 no permission`** while `apiswitch=0`.
- No successful plaintext privileged JSON write (`enc=1`, `set_normal_power`, `adjust_power_limit`, `power_off`, `factory_reset`, `update_pools`) was observed.

**Implication for poolheat:** matching Tools with API off requires implementing **TCP :8889 Remote Ctrl**, not only LuCI/`open_by_api` or 4028/4433 heuristics.

---

## 2. Ports used by Tools

| Port | Role in capture |
|------|-----------------|
| **4028** | High-rate read: `summary`, `status`, `devs`, `get_psu`, `get_error_code`, occasional `get_version` / `get_miner_info` / `pools`. Failed `get_token`. |
| **4433** | `get.device.info` (includes `system.apiswitch`). Failed writes: `set.miner.service`. |
| **8889** | Auth 64B poll + large ASCII telemetry push; **all successful control** (opaque client cmd framing). |
| **443/80** | Little useful cleartext (TLS). |

---

## 3. Read path (parameter polling)

### 3.1 TCP 4028 (JSON line)

Typical loop (also used by poolheat):

```
summary → status → devs → get_error_code → get_psu
```

Useful fields observed:

- `Power Mode`, `Power Limit`, `Power`, temps, `Uptime` / `Elapsed`
- `status.mineroff`, `power_limit_set`
- `devs[]`: `PCB SN`, `Factory GHS`, temps
- `get_psu`: `vin`/`iin` raw (×100 V / mA), `pin`, `temp0`

### 3.2 TCP 4433 (API v3, LE length-prefixed JSON)

```
get.device.info  →  system.apiswitch, power.liquid-temperature, pcbsn0..3, detect-hash-rate, …
```

Throughout capture: **`apiswitch: "0"`**.

### 3.3 TCP 8889 telemetry (after auth)

Server pushes text sections such as:

```
[MinerInfo]
MinerType = M63
…
PowerMode = 1
PowerLimitSet = 2200
MinerApiSwitch = 0
…

[PowerInfo]
PowerOnOff = on|off
PowerVout / PowerIout / …
```

Also summary-like CSV lines with `Power Mode=Normal`, `User=admin.test`, `stratum+tcp://…`.

---

## 4. Control actions — evidence

### 4.1 Performance Mode (= Power Mode)

- 4028 `summary` showed **`Low` ↔ `Normal`**.
- No `set_low_power` / `set_normal_power` / `enc` write payloads.
- Conclusion: mode change on **8889**.

### 4.2 Power Limit → 2200

- `power_limit_set` / `Power Limit` / `PowerLimitSet`: **2500 → 2200**.
- Mining restart symptoms (limit 0 briefly, upfreq).
- No `adjust_power_limit` on 4028.
- Conclusion: **8889**.

### 4.3 Mining Control (Suspend / Resume)

- `mineroff`: **false → true → false**.
- 8889 `PowerOnOff`: **on ↔ off**.
- 4433 repeatedly attempted `set.miner.service` + `param=stop` → **no permission** (not the successful path).
- No `power_off` / `power_on` on 4028.
- Conclusion: **8889**.

### 4.4 Enable API

- WMT UI: **need change password** (not present as cleartext on wire).
- `apiswitch` / `MinerApiSwitch` remained **0** for the whole capture.
- Enable API **not completed** without password change (operator chose not to change password).

### 4.5 Reboot

- **Uptime** drop e.g. **3649 → 47** (full board reboot).
- No 4028 `reboot` / 4433 reboot cmd in cleartext.
- Conclusion: **8889**.

### 4.6 Restore Factory Settings

- After action: live **Power Limit 0 / factory ~7700**, settings wiped vs session (e.g. custom limit gone).
- No 4028 `factory_reset`.
- Conclusion: **8889**.

### 4.7 Pools

- Live pool user **`admin.test` → `admin.test1`**, same URL `stratum+tcp://sha256ab.hashca.dev:3333`.
- 4028 only **`{"cmd":"pools"}`** reads — **no `update_pools`**.
- Conclusion: **8889**.

---

## 5. Protocol notes: TCP :8889

### 5.1 Client → miner (auth / poll)

Almost all successful polls start with **exactly 64 bytes**:

| Offset | Length | Content |
|--------|--------|---------|
| 0 | 16 | Variable (nonce / client challenge) |
| 16 | 16 | **Fixed** for this miner/session material: `4ae8190ec1a11f3c8b5e4211249a3876` |
| 32 | 16 | Variable (signature / response) |
| 48 | 16 | **Fixed**: `9df852445a0ed54909ee6f9e66e96811` |

Notes:

- Fixed halves were **constant across the entire capture** for this ASIC → almost certainly derived from **password + device identity (e.g. MAC)**, not random per process.
- Proxying Tools to Mac IP (`192.168.1.34`) produced the same 64B shape but miner often answered with short **error frame**; Tools “not found”. Auth is **not** transparent to destination-IP change / bad key material.
- Rare longer client frames (80 / 128 B) seen; likely **commands** (encrypted). Heads look random (no ASCII).

### 5.2 Miner → client frame header

Many server payloads begin with magic **`5A 5A 7F 7F`** (`ZZ..`).

Observed patterns:

| Header (hex, 16 B) | Meaning (inferred) |
|--------------------|--------------------|
| `5a5a7f7f 00000000 02000000 ffff0000` | Short **error / reject** (16 B only) — common after bad auth (e.g. proxy) |
| `5a5a7f7f 16000000 …` + body | **Success status**: body often starts with `[MinerInfo]\n…` (type field **0x16 = 22** LE at offset 4) |
| `5a5a7f7f 00000000 11000800 xxxx0000` | Short control/status (16 B) |
| Other type codes at +4 (`02`,`05`,`06`,`0d`,…) | Other short replies |

**Important:** `5a5a7f7f` is a **framing magic**, not “always error”. Error vs data is distinguished by **following fields + payload length**.

Example success (1310 B total):

```
5a5a7f7f 16000000 0000740a 09690000
[MinerInfo]
MinerType = M63
...
```

Also seen: TCP payload of **6× `0x00`** as keepalive/placeholder on some multipath captures.

### 5.3 Session shape (status poll)

Typical successful Tools poll (from `192.168.1.201`):

1. Client sends **64 B** auth/poll.  
2. Server replies with large chunk(s): ZZ header + `[MinerInfo]` / `[PowerInfo]` / SUMMARY CSV.  
3. **No further cleartext client command** in that flow.

So continuous dashboard updates ≈ **re-auth 64B + push telemetry**, not continuous 4028 alone (Tools uses both).

### 5.4 Account / key hypothesis (`AccountName=super`)

Official Whatsminer API (apidoc) write accounts: **`super`**, `user1`–`user3`  
(`admin` is deprecated for the JSON API). Live v3:

```text
get.device.info  →  msg.salt = "BQ5hoXV9"   (this unit)
                  msg.system.apiswitch = "0"
```

**Hypothesis:** 8889 Remote uses the same **API account identity** (`AccountName=super` / `account=super`) plus web/API password (`admin`) and possibly `salt`/MAC as KDF input — not the LuCI username alone.

**Offline tests against capture fixed halves** (`4ae8190e…` / `9df85244…` etc.):

| Material | md5 / sha256[:16] / AES-ECB(pt∈{super,admin,salt,MAC}) |
|----------|--------------------------------------------------------|
| `super`, `AccountName=super`, `super+admin`, `admin+super` | no match |
| `salt`, `super+salt`, `admin+salt`, `super+admin+salt` | no match |
| `super+MAC`, `MAC+super`, hmac(super, nonce) | no match |
| AES decrypt of full 64 B as JSON with those keys | no `super`/`admin` plaintext |

So **`super` is very plausible as the account name inside the crypto**, but the actual KDF is **not** a single MD5/SHA of that string. Next likely: proprietary AES with multi-step key schedule (token/session), or key mixed with device secret not visible in `get.device.info`.

Also: ~362 unique 64 B poll tokens in the pcap (not one static key forever); multipath capture inflates counts. Dominant mid↔tail pair is stable within a Tools session, then rotates on reconnect.

### 5.5 Write commands — **REVERSED** (2026-08-05, keys from firmware)

Firmware AES-256 keys (ECB, full frame):

| Key | Hex | Use |
|-----|-----|-----|
| KEY0 | `f0d379ee…ae1da90f` | cmd **0** auth |
| KEY1 | `66476cc4…e787c296` | status **0x16**, write **0x0d**, pools **0x02**, … |
| KEY2 | `9be70afc…ead19bf1` | reserved (unused in WMT capture) |

**Client plaintext layout** (then AES-256-ECB, zero-pad body to 16):

```
magic 5A5A7F7F | cmd u32LE | f2 u32LE | crc u32LE | body
f2 = (payload_len << 16) | cred_len
body = "{miner_ip}|{unix_ts}|{account}|{password}|{session}{payload}"
crc  = (~zlib.crc32(body)) & 0xffffffff   # no trailing NUL
```

- **miner_ip** must be the ASIC address (not client).
- Tools body: `account=super`, `password=super`, `session` = 8 hex from auth ACK.
- Auth cmd=0 → 24 B plaintext reply: `status u32 (=6)` + **session 4 B**.
- Status cmd=0x16 → large **plaintext** `[MinerInfo]` / `[PowerInfo]`.
- Write cmd=0x0d payload examples: `14=2500` (power limit), `8=1`/`8=0` (suspend/resume).
- Pools cmd=0x02 payload: `0,url,user,FAILOVER,,pass|`
- Write **16 B reply with tail `ffff` = SUCCESS** (live: PowerLimitSet 2500→2400→2500).
- Bad frame reject: mid field `0x02` + `ffff`.

Implemented in `ui/whatsminer_driver.py` → `Remote8889Client`.

### 5.5b Write commands (historical — pre-key)

State changes (mode / limit / MC / reboot / factory / pools) **correlate with 8889 activity**, but earlier notes assumed:

- No ASCII command names (`SetPowerMode`, `update_pools`, …) on c2s (true — encrypted).
- Candidate **encrypted** c2s sizes: **80** and **128** bytes (rare vs 64 B polls).
- Full reverse of encrypt/MAC for writes needs either:
  - known-password offline derivation of fixed 32 B key material, or  
  - more targeted capture with only one action and full bidirectional reassembly (TCP stream, not per-segment).

---

## 6. Side findings (ops)

1. **`over max connect` (4028)** when poolheat + WMT poll together — throttle or pause poolheat during Tools capture/control tests.  
2. **Factory restore** wiped custom power limit; live limit returned to factory-scale values (e.g. **7700** observed later).  
3. Local **TCP proxy on laptop** is fine for **cleartext 4028/4433**, but **8889 auth fails** if Tools targets the laptop IP (key/dest binding). Prefer **router span/tcpdump on real miner IP**.  
4. Peak `tcpdump -i any` uses **LINUX_SLL2** (linktype 276); each L3 packet may appear on multiple interfaces → multiply counts in raw frame stats.

---

## 7. Files

| Path | Description |
|------|-------------|
| `pcap/wmt.pcap` | Full capture (~30 MB) |
| `pcap/8889_summary.json` | Flow length histograms |
| `pcap/8889_session_sample.json` | One multipath session event list |
| `CAPTURE-NOTES.md` | This document |
| `proxy.py` | Laptop proxy (limited for 8889 control) |

---

## 8. Clean single-stream capture

Scripts:

| File | Role |
|------|------|
| `capture-8889-clean.sh` | Peak: `start`/`stop` — **br0**, filter `host MINER and tcp port 8889`, pauses poolheat |
| `analyze_8889_stream.py` | Mac: rebuild TCP streams, dump AUTH64 / WRITE80 / WRITE128 |

### 8.1 Attempt 2026-08-05 (Peak br0) — poll only

| | |
|--|--|
| Artifact | `pcap/wmt8889.pcap` (~19 KB, 120 frames, linktype **Ethernet**) |
| Client seen | **only `192.168.1.1`** (router) → miner:8889 |
| c2s | **11× AUTH64** only — **no 80/128 B writes** |
| s2c | ZZ MinerInfo + ASCII SUMMARY; `PowerOnOff=on` entire time |

Telemetry gold:

```text
MinerApiSwitch = 0
PowerOnOff = on
PowerLimitSet = 2500
PowerMode = 1
#web_pool=1,sshd=0#super=255 user1=0 user2=0 user3=0
```

→ **`super=255`** supports AccountName=`super` as the API/remote account (user1–3 idle).

**Why no write / no PC IP:** Keenetic **hardware switch** often does not deliver pure LAN→LAN frames to the CPU. `tcpdump -i br0` then only sees traffic that hits the router (hairpin / to-router), not Tools PC (`192.168.1.20x`) ↔ miner L2. Suspend either never hit the wire on a path we can see, or was L2-invisible.

### 8.2 Topology: WMT over SSTP VPN (`192.168.1.200`)

Office PC → **SSTP VPN** → Peak `sstp0` (addr path `192.168.1.200 dev sstp0`)  
→ **FASTNAT** → br0 → miner `192.168.1.10:8889`.

Conntrack example:

```text
src=192.168.1.200 dst=192.168.1.10 dport=8889
reply src=192.168.1.10 dst=192.168.1.1   [FASTNAT]
```

So on **br0-only** dump the client looks like **`192.168.1.1`**, not `.200`.  
Correct Peak capture:

```bash
tcpdump -i any -s 0 -w /tmp/wmt8889-vpn.pcap \
  'tcp port 8889 and host 192.168.1.10'
```

(`-i any` sees sstp0 + br0; filter miner only — Tools also scans other .1.x:8889.)

### 8.3 VPN captures 2026-08-05

| File | Size | FASTNAT | WRITE 80/128 | Notes |
|------|------|---------|--------------|--------|
| `wmt8889-vpn.pcap` | 42 KB | on | none | client `.200` visible; poll only |
| `wmt8889-vpn2.pcap` | 101 KB | **off** | **none** | `PowerOnOff on→off` (Suspend effect) but still only AUTH64 |

**Dual poll channels every cycle (VPN):**

| mid (offset 16) | typical s2c |
|-----------------|-------------|
| `68f361a707af8cd2…` | ZZ **type=0** len 24 (short) |
| `2299654e09f9a16c…` | ZZ **type=22** MinerInfo (status) |

**Ground truth from first big capture (`wmt.pcap`):** real write PDUs exist:

| src | size | correlates with |
|-----|------|-----------------|
| `.201` | **80 B** | (limit/mode era) |
| `.200` | **128 B** ×2 | `PowerOnOff=off` (Suspend) — mid=`2299654e…` |

So Suspend **does** use **128 B** on 8889 when fully visible; Peak path even with `fastnat=0` still missed those PDUs (another offload / asymmetric path). **Best next step: Wireshark on office PC `192.168.1.200`.**

```text
# on office Windows/Mac running WMT:
capture filter: host 192.168.1.10 and tcp port 8889
# one Suspend → save pcap → copy to repo → analyze_8889_stream.py
```

### 8.4 Next capture — on the **Tools PC** (optional if Peak any works)

Run capture **where WhatsMinerTool runs** (same machine), so L2 path is irrelevant.

**macOS** (Tools on this Mac):

```bash
# replace en0 if needed: route get 192.168.1.10
sudo tcpdump -i en0 -s 0 -w /tmp/wmt8889-client.pcap \
  'host 192.168.1.10 and tcp port 8889'
# Tools: connect → ONE Suspend → Ctrl+C
```

**Windows** (Tools on Windows): Wireshark → capture filter  
`host 192.168.1.10 and tcp port 8889` → start → Suspend → stop → save pcapng.

Then on Mac repo:

```bash
python3 tools/whatsminer-proxy/analyze_8889_stream.py \
  /path/to/wmt8889-client.pcap --out-dir tools/whatsminer-proxy/pcap/stream-out
```

Expect: `WRITE80` or `WRITE128` in timeline after AUTH64.

### 8.3 Optional Peak mirror

If Tools stays on another PC: enable **port mirroring** on Peak (LAN Tools port → port where Peak CPU sniffs), or put miner on a VLAN that **routes** through the router so frames hit CPU.

## 9. Next engineering steps (poolheat)

1. **Short term:** LuCI for mode/pools/reboot; TCP when apiswitch on; reduce get_token spam.  
2. **Tools-parity (API off):** reverse **8889** write packing from clean stream:
   - KDF: `AccountName=super` + password + salt/MAC (simple MD5 failed — need session handshake);
   - 80/128 B command packing;
   - flip `Remote8889Client._writes_ready` when crypto lands.  
3. Optional: password-change path → re-capture **Enable API** (`apiswitch 0→1`).

---

## 9. Operator sequence used

1. Connect Tools → `192.168.1.10` (not Mac proxy).  
2. Performance Mode change.  
3. Power Limit 2200.  
4. Suspend then Resume.  
5. Enable API → *need change password* (skipped).  
6. Reboot.  
7. Restore Factory Settings.  
8. Pools: worker `admin.test` → `admin.test1`.

---

*Generated from live M63 capture 2026-08-05 · poolheat_WM research*
