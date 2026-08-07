# FinePower PLG-1 — local_key once, then LAN only

Known from LAN discovery (no cloud):

| Field | Value |
|--------|--------|
| IP | `10.1.30.40` (reserve in DHCP) |
| MAC | `38:a5:c9:3c:b9:53` (Tuya Smart Inc.) |
| Device ID | `bf60efac4222a06088bew9` |
| Protocol | **3.4** |
| Port | **6668** |
| productKey | `keyjup78v54myhan` (not the local_key) |

After you have `local_key`, runtime is **only** TCP to the plug. No Smart Life, no iot.tuya.com.

---

## Path A1 — extract key from Smart Life (Android / emulator)

Works **without** creating a Tuya IoT developer project. You use the fact that the **app already downloaded keys** for your account.

### Option A1a — BlueStacks + old workflow (classic)

1. Install [BlueStacks](https://www.bluestacks.com/) (or other Android emulator) on the Mac.
2. Install **Smart Life** (or Tuya Smart) from Play Store inside the emulator.
3. Log in with the **same account** where PLG-1 is already paired.
4. Open the device once (so app refreshes device list).
5. Pull app preferences that contain local keys:
   - Prefer **old Smart Life APK** (~3.6–4.x era) if current app encrypts prefs harder — see community guides (Mark Watt Tech “Tuya local keys”).
   - Tool: [MarkWattTech/TuyaKeyExtractor](https://github.com/MarkWattTech/TuyaKeyExtractor)
6. Look for device id `bf60efac4222a06088bew9` → field **localKey** / **local_key**.

### Option A1b — physical Android + backup / root

1. Same Smart Life account, device visible.
2. If **rooted**:  
   `adb shell` →  
   `/data/data/com.tuya.smartlife/` or `com.tuya.smart` →  
   shared_prefs / databases → search `localKey` / `bf60efac`.
3. If **not rooted**: full app backup (where OEM allows) → unpack → same search.  
   Modern Android often blocks this; emulator path is easier.

### Option A1c — iPhone

Harder (sandbox). Usually not worth it — use Android emulator or A2.

---

## Path A2 — tinytuya wizard (iot.tuya.com once)

If A1 fails (encrypted prefs / no Android):

1. Register free: https://iot.tuya.com  
2. Cloud → Create project → **Link Smart Life app account** (same login as the app).  
3. On the Mac (same Wi‑Fi `10.1.30.0/24` or any net — cloud only needs internet):

```bash
pip3 install -U tinytuya
python3 -m tinytuya wizard
```

4. Enter Access ID / Secret / region (eu/us/…).  
5. Wizard writes `devices.json` next to where you ran it — copy `key` for id `bf60efac4222a06088bew9`.

**After this:** you can ignore cloud forever for control. Do **not** re-pair the plug in Smart Life unless you re-run extract (key changes).

---

## Fill config and test (LAN only)

```bash
cd /Users/macbookpro16/Documents/poolheat/tools/tuya-local
cp plg1.env.example plg1.env
# edit plg1.env → TUYA_LOCAL_KEY=...

# must be on Wi‑Fi 10.1.30.x (not only Keenetic VPN)
python3 plg1_control.py discover
python3 plg1_control.py status
python3 plg1_control.py on
python3 plg1_control.py off
```

Success `status` looks like:

```json
{
  "dps": {
    "1": true,
    "9": 0,
    "18": 0,
    "19": 0,
    "20": 2300
  }
}
```

Typical DPS (may vary):

| dps | meaning |
|-----|---------|
| 1 | switch on/off |
| 9 | countdown |
| 18/19/20 | current / power / voltage (if monitoring) |

Err **914** → wrong key or version (try 3.3 only if discover says so; we saw **3.4**).

---

## Network tips (this Mac)

- Plug is on **en0** `10.1.30.0/24`. Laptop IP like `10.1.30.52`.
- VPN to home (`utun*` / `192.168.1.x`) can steal default route — for tests stay on `10.1.30.x` or bind to that interface.
- Reserve **10.1.30.40** in the AP/router DHCP for the MAC `38:a5:c9:3c:b9:53`.

---

## Security

- `plg1.env` = secret. Add to `.gitignore` (do not commit).
- Anyone on LAN with id+key can toggle the plug — treat key like a password.
- Optional: AP client isolation off for your automation host only; firewall 6668 to trusted hosts.

---

## After key works

1. Tell poolheat path: device type `tuya_local` (ip, id, key, version 3.4).  
2. Optional: block WAN for the plug’s MAC (it may still try cloud; local 6668 keeps working if key valid).
