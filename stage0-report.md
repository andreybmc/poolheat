# Stage 0 report — Whatsminer M63 @ 192.168.1.10

**Captured:** 2026-08-01 (live probe from LAN)  
**Host:** `192.168.1.10:4028`  
**Raw snapshot:** `stage0-snapshot-192.168.1.10.json`  
**Writes:** not performed (read-only)

## Identity

| Field | Value |
|-------|--------|
| miner_type | **M63_VK28** |
| platform | H616 |
| fw_ver | `20251120.20.REL2` |
| api_ver | `2.2.2` |
| PSU | P564Y · pin ≈ 5745 W · vin 37.2 V |

## Live snapshot (at probe)

| Metric | Value |
|--------|--------|
| Power Mode | **Low** (`status.power_mode` = `low`, `summary.Power Mode` = `Low`) |
| Power (measured) | **~5740 W** |
| Power Limit (summary) | **5777 W** |
| power_limit_set (status) | **7000** (setpoint ceiling; measured lower in Low mode) |
| Env Temp | **~26.6 °C** |
| liquid_temp | **~27.1 °C** |
| Chip Temp Min / Max / Avg | **44.7 / 58.4 / 51.3 °C** |
| Hash Stable | **true** (stable cost ~560 s) |
| Uptime (summary) | ~37998 s |
| Errors | none |

### Hashboards (`devs`)

| Slot | PCB Temperature | Upfreq Complete | Chip Freq | Effective Chips |
|------|-----------------|-----------------|-----------|-----------------|
| 0 | **41.31 °C** | **1** (done) | 392 | 264 |
| 1 | **41.31 °C** | **1** | 390 | 264 |
| 2 | **34.81 °C** | **1** | 390 | 264 |
| 3 | **34.81 °C** | **1** | 387 | 264 |

Matches your earlier “hot pair 0/1 ~42 °C, cold pair 2/3 ~35 °C” pattern.  
**Warmup gate field:** use `Upfreq Complete == 1` on all boards (here all ready).  
`get_miner_info.upfreq_speed` was `"0"` — do **not** confuse with unfinished ramp; prefer `Upfreq Complete` + `Hash Stable`.

### Control metrics (for poolheat)

```text
T_ctrl = max(board[0], board[1])  →  41.31 °C
T_safe = max(all boards)          →  41.31 °C   (chips hotter: Chip Temp Max 58.4 — decide which for Critical)
```

**Open product choice:** Critical on **PCB board temp** vs **Chip Temp Max** from summary.  
PCB ~41 is your heat-band; chips ~58 are higher — calibrate carefully.

## API surface (read)

| Command | Result |
|---------|--------|
| `summary` | OK — power, chip temps, power mode, fans |
| `devs` / `edevs` | OK — per-board temp, Upfreq Complete |
| `status` | OK — power_mode, power_limit_set, liquid_temp, mineroff |
| `get_version` | OK |
| `get_miner_info` | OK |
| `get_psu` | OK |
| `get_error_code` | OK |
| `pools` | OK |
| `get_token` | OK — returns `time`, `salt`, `newsalt` (needed for privileged writes) |
| `get_power_limit` | **invalid cmd** |
| `get_power_percent` | **invalid cmd** |
| `get_power_mode` | **invalid cmd** |

Power state is already in **`summary` + `status`**, not separate get_power_* cmds on this FW.

## Implications for controller

1. **Default host** in UI: `192.168.1.10:4028`.
2. **Warmup:** `all(Upfreq Complete == 1)` and preferably `Hash Stable == true` → `miner_ready`.
3. **Current mode:** already **Low** @ ~**5.7 kW** (your “reduced/heat band” is close to live state).
4. **T_ctrl ≈ 41.3 °C** — right at edge of default `T_low=41` / deadband; map thresholds should be tuned to **this** HEX, not abstract numbers.
5. **Writes next (not done yet):** need signed privileged API via `get_token` + password:
   - `set_low_power` / `set_normal_power` (likely)
   - `set_power_pct` / `adjust_power_limit` — probe **one at a time** in maintenance window
6. **Do not thrash** until we know if limit/mode causes restart on this FW.

## Not done in this Stage 0 pass

- [ ] Switch Low → Normal and measure watts / temps / settle time  
- [ ] Switch Normal → Low  
- [ ] Test `set_power_pct` if available  
- [ ] Test `adjust_power_limit` (reboot risk)  
- [ ] Test sleep / power_off for Critical  
- [ ] Confirm privileged command format with miner password  

## Suggested MVP defaults (from this snapshot)

| Setting | Suggestion |
|---------|------------|
| miner.host | `192.168.1.10` |
| dry_run | `true` until write tests |
| T_ctrl boards | `{0, 1}` |
| Working point observed | PCB 0/1 ≈ **41.3 °C**, mode **Low**, **~5740 W** |
| T_low / T_high | start near **40 / 43** or **40.5 / 42.5** (tighter around 41.3) then widen |
| T_crit (PCB) | start **46–48** PCB — **or** use Chip Temp Max with higher threshold |
| actuator_strategy | prefer **mode_only** first (`low`/`normal`) |

---

*Stage 0 partial complete: read path proven. Write path still gated.*
