# miner_sniffer — optional poolheat module

Captures miner LAN traffic with `tcpdump` on the router (Entware / Keenetic Peak).

## Lifecycle

Installed **on demand** from GitHub when enabled in poolheat **Advanced → Miner sniffer**:

1. **Activate** → download module from repo `modules/miner_sniffer/` → install under data dir → start capture  
2. **Deactivate** → stop `tcpdump` (module files stay)  
3. **Uninstall** → stop + delete module files + clear pcaps (free space)

Default filter: `host <miner_ip>` on interface `br0`.  
Pcaps: `/tmp/poolheat-sniffer/` (tmpfs on Peak — does not fill flash).

## Requirements

```sh
opkg update
opkg install tcpdump
```

## Manual API (poolheat service)

| Method | Path | Action |
|--------|------|--------|
| GET | `/api/sniffer` | status + config |
| POST | `/api/sniffer/config` | `{ "enabled": true/false, "iface"?, "filter"? }` |
| POST | `/api/sniffer/install` | force re-download from GitHub |
| POST | `/api/sniffer/uninstall` | full remove |
| POST | `/api/sniffer/rotate` | rotate pcap + restart |
| POST | `/api/sniffer/clear` | delete stored pcaps |
| GET | `/api/sniffer/download` | download current pcap |

## Notes

- Does **not** stop poolheat (unlike lab capture scripts).  
- Tracks only its own pid file — no `killall tcpdump`.  
- Keep captures short: `/tmp` is limited on routers.
