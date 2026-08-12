# Local data directory

Runtime data for **local** `python3 ui/serve.py` (not Entware `/opt/var/poolheat`):

- `history.db`, `energy.db` (+ `-shm` / `-wal`)
- zone / schedule / telegram / mode profiles
- live caches written by pollers

Do not commit secrets or DB files. Override with `POOLHEAT_DATA`.
