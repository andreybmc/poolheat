"""CLI: scan / summary / reboot / private NetPacket (8889) ops."""

from __future__ import annotations

import argparse
import json
import sys

from .client import MinerClient
from .fleet import iter_hosts, scan
from .protocol.netpacket import (
    PARAM_INFO,
    Cmd,
    NetPacketClient,
    Param,
    PerformanceMode,
    extract_firmware_image,
)


def _wmt_client(args: argparse.Namespace) -> NetPacketClient:
    return NetPacketClient(
        args.host,
        account=args.account,
        password=args.wmt_password,
        timeout=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whatsminer", description="Whatsminer management CLI")
    p.add_argument("--password", default="admin", help="API V2 admin password")
    p.add_argument("--timeout", type=float, default=5.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="Scan IP range / CIDR")
    s.add_argument("range", help="e.g. 192.168.1.0/24 or 192.168.1.10-192.168.1.50")
    s.add_argument("--workers", type=int, default=64)

    g = sub.add_parser("summary", help="Snapshot one miner (public API)")
    g.add_argument("host")
    g.add_argument("--api", choices=("v2", "v3"), default=None)

    det = sub.add_parser(
        "detect",
        help="Detect API + WMOC LuCI tools (wmoc_tools / fancontrol / …)",
    )
    det.add_argument("host", help="host, host:port, or URL (e.g. 10.121.15.76:8788)")
    det.add_argument("--api", choices=("v2", "v3"), default=None)
    det.add_argument(
        "--luci-user",
        default="admin",
        help="LuCI username (default admin)",
    )
    det.add_argument(
        "--luci-password",
        default=None,
        help="LuCI password (default: --password)",
    )
    det.add_argument(
        "--scheme",
        default=None,
        choices=("http", "https"),
        help="LuCI URL scheme (default: http for non-443 ports)",
    )
    det.add_argument("--luci-port", type=int, default=None, help="LuCI TCP port")
    det.add_argument(
        "--skip-wmoc",
        action="store_true",
        help="only probe public API ports",
    )

    pools_p = sub.add_parser(
        "pools",
        help="Read/write pools (LuCI passwords; set via --set-file)",
    )
    pools_p.add_argument("host")
    pools_p.add_argument("--api", choices=("v2", "v3"), default=None)
    pools_p.add_argument(
        "--passwords",
        action="store_true",
        help="include stratum passwords from LuCI /admin/network/btminer",
    )
    pools_p.add_argument(
        "--luci-user",
        default="admin",
        help="LuCI web username (default admin)",
    )
    pools_p.add_argument(
        "--luci-password",
        default=None,
        help="LuCI web password (default: --password)",
    )
    pools_p.add_argument(
        "--set-file",
        default=None,
        help="JSON file with pools list (or {pools:[...]}) — write via LuCI",
    )
    pools_p.add_argument(
        "--reinstall",
        action="store_true",
        help="with --set-file or alone: re-write pools via LuCI (deferred switch)",
    )
    pools_p.add_argument(
        "--restart-mining",
        action="store_true",
        help="after LuCI pool write, restart btminer so new pools become active",
    )

    r = sub.add_parser("reboot", help="Reboot via public write API")
    r.add_argument("host")
    r.add_argument("--api", choices=("v2", "v3"), default=None)

    api_list = sub.add_parser(
        "api-list",
        help="List official public API V1/V2/V3 commands (PDF + apidoc + manuals)",
    )
    api_list.add_argument(
        "--family",
        choices=("v1", "v2", "v3", "all"),
        default="all",
        help="filter by API family",
    )
    api_list.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON",
    )

    err_p = sub.add_parser(
        "error",
        help="Describe miner error code(s) from WhatsMinerTool i18n tables",
    )
    err_p.add_argument(
        "codes",
        nargs="+",
        help="error code(s), e.g. 110 200 100002",
    )
    err_p.add_argument(
        "--lang",
        default="en",
        help="language: en, zh (ch), ru (fallback en)",
    )
    err_p.add_argument(
        "--json",
        action="store_true",
        help="JSON array of resolve_error() objects",
    )

    probe_p = sub.add_parser(
        "probe",
        help="Probe miner: public API version, write path, NetPacket :8889, LuCI",
    )
    probe_p.add_argument("host")
    probe_p.add_argument(
        "--wmt-password",
        default="super",
        dest="wmt_password",
        help="NetPacket / WMT Remote password (default super)",
    )
    probe_p.add_argument(
        "--account",
        default="super",
        help="V3 / NetPacket account (default super)",
    )
    probe_p.add_argument(
        "--luci-user",
        default="admin",
        help="LuCI username",
    )
    probe_p.add_argument(
        "--luci-password",
        default=None,
        help="LuCI password (default: --password)",
    )
    probe_p.add_argument(
        "--no-luci",
        action="store_true",
        help="skip LuCI probe",
    )
    probe_p.add_argument(
        "--no-netpacket",
        action="store_true",
        help="skip NetPacket :8889 probe",
    )

    # Private NetPacket :8889
    w = sub.add_parser("wmt", help="Private NetPacket control on TCP 8889 (WMT path)")
    w.add_argument("host")
    w.add_argument("--account", default="super")
    w.add_argument("--wmt-password", default="super", dest="wmt_password")
    w.add_argument(
        "action",
        choices=(
            "ping",
            "handshake",
            "info",
            "status",
            "hashrate",
            "limit",
            "powerpct",
            "powerpct-reset",
            "hashpct",
            "upfreq",
            "perf",
            "mode",
            "suspend",
            "resume",
            "fastboot",
            "fasthash",
            "apiswitch",
            "passwd",
            "perms",
            "pools",
            "webpools",
            "export-log",
            "coin",
            "heat",
            "led",
            "param",
            "raw",
            "list-cmds",
            "list-params",
            "reboot",
            "factory",
            "restore-settings",
            "restore-dhcp",
            "firmware",
            "sync-time",
            "timezone",
            "ntp",
        ),
        help="perf/fastboot/apiswitch/passwd; reboot/factory/restore-*/firmware need --yes",
    )
    w.add_argument("--watts", type=int, default=None)
    w.add_argument(
        "--percent",
        type=int,
        default=None,
        help="for action=hashpct: Adjust freq percent (e.g. 10)",
    )
    w.add_argument(
        "--speed",
        type=int,
        default=None,
        help="for action=upfreq: Adjust upfreq speed (e.g. 10)",
    )
    w.add_argument(
        "--mode",
        type=int,
        default=None,
        help="Performance Mode semantic: 0=Low 1=Normal 2=High (cmd5 wire 1/0/2)",
    )
    w.add_argument(
        "--enable",
        type=int,
        choices=(0, 1),
        default=1,
        help="for action=fastboot|fasthash|apiswitch|webpools: 1=enable 0=disable (default 1)",
    )
    w.add_argument(
        "--fast",
        action="store_true",
        help="for action=powerpct: Adjust Power Fast Mode (param 9) instead of Normal (param 20)",
    )
    w.add_argument(
        "--file",
        default=None,
        dest="fw_file",
        help="firmware path for action=firmware (raw container or Whatsminer-all-*.bin)",
    )
    w.add_argument(
        "--platform",
        default="h616",
        help="platform slice inside Whatsminer-all package (default h616)",
    )
    w.add_argument(
        "--zonename",
        default=None,
        help="for action=timezone: e.g. Asia/Novosibirsk",
    )
    w.add_argument(
        "--posix-tz",
        default=None,
        dest="posix_tz",
        help='for action=timezone: e.g. "<+07>-7"',
    )
    w.add_argument(
        "--offset-hours",
        type=int,
        default=None,
        dest="offset_hours",
        help="for action=timezone: UTC offset hours (7 → <+07>-7)",
    )
    w.add_argument(
        "--servers",
        default=None,
        help="for action=ntp: comma-separated NTP hosts (max 4)",
    )
    w.add_argument("--url", default=None, help="pool URL for action=pools")
    w.add_argument("--user", default=None, help="pool user for action=pools")
    w.add_argument("--pool-pass", default="x", dest="pool_pass")
    w.add_argument("--old-pass", default=None, dest="old_pass", help="for action=passwd")
    w.add_argument("--new-pass", default=None, dest="new_pass", help="for action=passwd")
    w.add_argument("--user1", type=int, default=0, help="for action=perms LEVEL 0-2")
    w.add_argument("--user2", type=int, default=0, help="for action=perms LEVEL 0-2")
    w.add_argument("--user3", type=int, default=0, help="for action=perms LEVEL 0-2")
    w.add_argument("--strategy", default="FAILOVER")
    w.add_argument("--coin", default="HC")
    w.add_argument("--heat", default="anti-icing")
    w.add_argument("--led", default="fast", help="normal|auto|fast|flash|slow|or raw pattern")
    w.add_argument("--param-id", type=int, default=None, help="for action=param")
    w.add_argument("--value", default=None, help="for action=param")
    w.add_argument("--cmd-id", type=int, default=None, help="for action=raw")
    w.add_argument("--binary", default="", help="for action=raw")
    w.add_argument("--yes", action="store_true", help="confirm destructive actions")

    args = p.parse_args(argv)

    if args.cmd == "api-list":
        from .api.catalog import all_commands, commands_by_family, summary_table

        if args.family == "all":
            cmds = all_commands()
        else:
            cmds = commands_by_family(args.family)  # type: ignore[arg-type]
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "family": c.family,
                            "name": c.name,
                            "access": c.access,
                            "port": c.port,
                            "notes": c.notes,
                            "encrypt_param": c.encrypt_param,
                        }
                        for c in cmds
                    ],
                    indent=2,
                )
            )
        else:
            if args.family == "all":
                print(summary_table())
            else:
                for c in cmds:
                    flag = " [enc]" if c.encrypt_param else ""
                    print(
                        f"{c.family} {c.access:5} :{c.port}  {c.name}  {c.notes}{flag}".rstrip()
                    )
            print(f"\n# total: {len(cmds)} commands")
        return 0

    if args.cmd == "error":
        from .support.error_codes import resolve_error

        rows = [resolve_error(c, lang=args.lang) for c in args.codes]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                known = "" if r.get("known") else " [unknown]"
                print(f"{r['code']}: {r['cause']}{known}")
        return 0

    if args.cmd == "probe":
        from .universal import probe_capabilities

        cap = probe_capabilities(
            args.host,
            password=args.password,
            account=args.account,
            wmt_password=args.wmt_password,
            luci_username=args.luci_user,
            luci_password=args.luci_password,
            timeout=args.timeout,
            probe_luci=not args.no_luci,
            probe_netpacket=not args.no_netpacket,
        )
        print(json.dumps(cap.to_dict(), indent=2, default=str))
        return 0

    if args.cmd == "detect":
        from .client import detect_api
        from .web.wmoc import detect_wmoc

        out: dict = {
            "host": args.host,
            "api": args.api or detect_api(args.host, timeout=args.timeout),
        }
        if not args.skip_wmoc:
            try:
                out["wmoc"] = detect_wmoc(
                    args.host,
                    username=args.luci_user,
                    password=args.luci_password or args.password,
                    timeout=args.timeout,
                    scheme=args.scheme,
                    port=args.luci_port,
                )
            except Exception as e:
                out["wmoc"] = {"wmoc": False, "error": str(e)}
        print(json.dumps(out, indent=2, default=str))
        return 0

    if args.cmd == "pools":
        c = MinerClient(
            args.host,
            api=args.api,
            password=args.password,
            luci_username=args.luci_user,
            luci_password=args.luci_password,
            timeout=args.timeout,
        )
        if args.set_file or args.reinstall:
            pools_list = None
            if args.set_file:
                from pathlib import Path

                raw = json.loads(Path(args.set_file).read_text())
                if isinstance(raw, list):
                    pools_list = raw
                elif isinstance(raw, dict):
                    pools_list = raw.get("pools") or raw.get("pools_for_set")
                else:
                    print("bad --set-file JSON", file=sys.stderr)
                    return 2
                if not pools_list:
                    print("no pools in --set-file", file=sys.stderr)
                    return 2
            data = c.reinstall_pools_luci(
                pools_list,
                restart_mining=bool(args.restart_mining),
            )
            print(json.dumps(data, indent=2, default=str))
            return 0 if data.get("ok") else 1
        data = c.get_pools(include_passwords=bool(args.passwords))
        print(json.dumps(data, indent=2, default=str))
        return 0

    if args.cmd == "scan":
        hosts = iter_hosts(args.range)
        found = scan(hosts, password=args.password, workers=args.workers, timeout=args.timeout)
        for m in found:
            ths = f"{m.hashrate_ths:.2f}" if m.hashrate_ths is not None else "?"
            print(
                f"{m.ip:16} api={m.api:2} type={m.miner_type or '?':16} "
                f"ths={ths:>8} power={m.power_w or '?'} mode={m.power_mode or '?'}"
            )
        print(f"\nOnline: {len(found)} / {len(hosts)}")
        return 0

    if args.cmd == "summary":
        c = MinerClient(args.host, api=args.api, password=args.password, timeout=args.timeout)
        st = c.snapshot()
        print(json.dumps(st.__dict__, default=lambda o: o.__dict__, indent=2))
        return 0

    if args.cmd == "reboot":
        c = MinerClient(args.host, api=args.api, password=args.password, timeout=args.timeout)
        print(json.dumps(c.reboot(), indent=2, default=str))
        return 0

    if args.cmd == "wmt":
        if args.action == "list-cmds":
            print(json.dumps([{"cmd": int(c), "name": c.name} for c in Cmd], indent=2))
            return 0
        if args.action == "list-params":
            print(json.dumps(NetPacketClient.list_params(), indent=2))
            return 0

        nc = _wmt_client(args)

        if args.action == "ping":
            print(json.dumps({"ping": nc.ping()}, indent=2))
            return 0
        if args.action == "handshake":
            print(json.dumps({"token": nc.handshake()}, indent=2))
            return 0
        if args.action == "info":
            info = nc.get_info()
            out = {k: v for k, v in info.items() if k not in ("raw_text", "sections")}
            out["sections"] = {
                name: {kk: vv for kk, vv in sec.items() if kk != "_lines"}
                for name, sec in info.get("sections", {}).items()
            }
            print(json.dumps(out, indent=2, default=str))
            return 0
        if args.action == "status":
            print(json.dumps(nc.status(), indent=2, default=str))
            return 0
        if args.action == "hashrate":
            print(json.dumps(nc.get_detected_hashrate(), indent=2, default=str))
            return 0
        if args.action == "hashpct":
            if args.percent is None:
                print("--percent required (e.g. 10 for Adjust freq Up 10%)", file=sys.stderr)
                return 2
            print(json.dumps(nc.set_hash_percent(args.percent), indent=2, default=str))
            return 0
        if args.action == "powerpct":
            if args.percent is None:
                print("--percent required (e.g. 96 Normal / 90 --fast)", file=sys.stderr)
                return 2
            print(
                json.dumps(
                    nc.set_power_pct(args.percent, fast=bool(args.fast)),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "powerpct-reset":
            print(json.dumps(nc.reset_power_pct(), indent=2, default=str))
            return 0
        if args.action == "upfreq":
            if args.speed is None:
                print("--speed required (e.g. 10 for Adjust upfreq speed)", file=sys.stderr)
                return 2
            print(json.dumps(nc.set_upfreq_speed(args.speed), indent=2, default=str))
            return 0
        if args.action == "limit":
            if args.watts is None:
                print("--watts required", file=sys.stderr)
                return 2
            print(json.dumps(nc.set_power_limit(args.watts), indent=2, default=str))
            return 0
        if args.action in ("perf", "mode"):
            if args.mode is None:
                print("--mode required: 0=Low 1=Normal 2=High (Performance Mode)", file=sys.stderr)
                return 2
            print(json.dumps(nc.set_performance_mode(args.mode), indent=2, default=str))
            return 0
        if args.action == "suspend":
            print(json.dumps(nc.suspend(), indent=2, default=str))
            return 0
        if args.action == "resume":
            print(json.dumps(nc.resume(), indent=2, default=str))
            return 0
        if args.action == "fastboot":
            print(
                json.dumps(
                    nc.set_fast_boot(bool(args.enable)),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "fasthash":
            print(
                json.dumps(
                    nc.set_fast_mining(bool(args.enable)),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "apiswitch":
            print(
                json.dumps(
                    nc.set_api_switch(bool(args.enable)),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "passwd":
            if not args.old_pass or not args.new_pass:
                print("--old-pass and --new-pass required", file=sys.stderr)
                return 2
            print(
                json.dumps(
                    nc.set_password(
                        args.old_pass,
                        args.new_pass,
                        account=args.account,
                    ),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "perms":
            print(
                json.dumps(
                    nc.set_permissions(args.user1, args.user2, args.user3),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "webpools":
            print(
                json.dumps(
                    nc.set_web_pools(bool(args.enable)),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "export-log":
            r = nc.export_log()
            # write files if we got data
            from pathlib import Path
            out_dir = Path("captures")
            out_dir.mkdir(exist_ok=True)
            meta = {k: v for k, v in r.items() if k not in ("data", "gzip")}
            if r.get("data"):
                path = out_dir / "export-log-from-api.bin"
                path.write_bytes(r["data"])
                meta["saved"] = str(path)
                meta["data_len"] = len(r["data"])
            elif r.get("gzip"):
                path = out_dir / "export-log-from-api.bin.gz"
                path.write_bytes(r["gzip"])
                meta["saved_gzip"] = str(path)
                meta["gzip_len"] = len(r["gzip"])
            print(json.dumps(meta, indent=2, default=str))
            return 0
        if args.action == "pools":
            if not args.url or not args.user:
                print("--url and --user required", file=sys.stderr)
                return 2
            print(
                json.dumps(
                    nc.set_pools(args.url, args.user, args.pool_pass, strategy=args.strategy),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "coin":
            print(json.dumps(nc.set_coin_type(args.coin), indent=2, default=str))
            return 0
        if args.action == "heat":
            print(json.dumps(nc.set_heat_mode(args.heat), indent=2, default=str))
            return 0
        if args.action == "led":
            print(json.dumps(nc.set_led(preset=args.led), indent=2, default=str))
            return 0
        if args.action == "param":
            if args.param_id is None or args.value is None:
                print("--param-id and --value required", file=sys.stderr)
                print("known:", json.dumps(PARAM_INFO, indent=2), file=sys.stderr)
                return 2
            val: int | str = args.value
            if args.value.lstrip("-").isdigit():
                val = int(args.value)
            print(json.dumps(nc.set_param(args.param_id, val), indent=2, default=str))
            return 0
        if args.action == "raw":
            if args.cmd_id is None:
                print("--cmd-id required", file=sys.stderr)
                return 2
            print(
                json.dumps(
                    nc.send_command(args.cmd_id, binary=args.binary, wait=3.0),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "reboot":
            if not args.yes:
                print("Refusing reboot without --yes", file=sys.stderr)
                return 2
            print(json.dumps(nc.reboot(), indent=2, default=str))
            return 0
        if args.action == "factory":
            if not args.yes:
                print("Refusing factory reset without --yes", file=sys.stderr)
                return 2
            print(json.dumps(nc.factory_reset(), indent=2, default=str))
            return 0
        if args.action == "restore-settings":
            if not args.yes:
                print("Refusing restore-settings without --yes", file=sys.stderr)
                return 2
            print(json.dumps(nc.restore_miner_settings(), indent=2, default=str))
            return 0
        if args.action == "restore-dhcp":
            if not args.yes:
                print("Refusing restore-dhcp without --yes", file=sys.stderr)
                return 2
            print(json.dumps(nc.restore_dhcp(), indent=2, default=str))
            return 0
        if args.action == "firmware":
            if not args.yes:
                print("Refusing firmware upgrade without --yes", file=sys.stderr)
                return 2
            if not args.fw_file:
                print("--file required (path to .bin)", file=sys.stderr)
                return 2
            from pathlib import Path

            raw = Path(args.fw_file).read_bytes()
            image, meta = extract_firmware_image(raw, platform=args.platform)
            print(
                json.dumps(
                    {
                        "file": args.fw_file,
                        "file_size": len(raw),
                        "image_meta": meta,
                        "image_size": len(image),
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            # Long timeout: ~12–40 MB over LAN
            nc.timeout = max(nc.timeout, 30.0)
            result = nc.update_firmware(
                image,
                poll_status=True,
                poll_attempts=30,
                wait_upload=600.0,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("ok") else 1
        if args.action == "sync-time":
            print(json.dumps(nc.sync_time(), indent=2, default=str))
            return 0
        if args.action == "timezone":
            if not args.zonename:
                print("--zonename required (e.g. Asia/Novosibirsk)", file=sys.stderr)
                return 2
            if not args.posix_tz and args.offset_hours is None:
                print("--posix-tz or --offset-hours required", file=sys.stderr)
                return 2
            print(
                json.dumps(
                    nc.set_timezone(
                        args.zonename,
                        args.posix_tz,
                        offset_hours=args.offset_hours,
                    ),
                    indent=2,
                    default=str,
                )
            )
            return 0
        if args.action == "ntp":
            if not args.servers:
                print(
                    "--servers required (e.g. 0.cn.pool.ntp.org,0.openwrt.pool.ntp.org)",
                    file=sys.stderr,
                )
                return 2
            hosts = [h.strip() for h in args.servers.split(",") if h.strip()]
            print(json.dumps(nc.set_ntp_servers(*hosts), indent=2, default=str))
            return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
