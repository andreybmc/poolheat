"""Concurrent fleet operations (WhatsMinerTool-style bulk scan / control)."""

from __future__ import annotations

import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, TypeVar

from .client import MinerClient, detect_api
from .models import MinerStatus

log = logging.getLogger(__name__)
T = TypeVar("T")


def iter_hosts(cidr_or_range: str) -> list[str]:
    """
    Accept:
      - CIDR: 192.168.1.0/24
      - range: 192.168.1.1-192.168.1.50
      - single IP
    """
    s = cidr_or_range.strip()
    if "/" in s:
        net = ipaddress.ip_network(s, strict=False)
        return [str(ip) for ip in net.hosts()]
    if "-" in s:
        a, b = s.split("-", 1)
        start = ipaddress.ip_address(a.strip())
        end = ipaddress.ip_address(b.strip())
        if int(end) < int(start):
            start, end = end, start
        return [str(ipaddress.ip_address(i)) for i in range(int(start), int(end) + 1)]
    return [str(ipaddress.ip_address(s))]


def scan(
    hosts: Iterable[str],
    *,
    password: str = "admin",
    workers: int = 64,
    timeout: float = 2.0,
) -> list[MinerStatus]:
    """Probe hosts; return online miners' snapshots."""
    host_list = list(hosts)
    results: list[MinerStatus] = []

    def _one(ip: str) -> MinerStatus | None:
        api = detect_api(ip, timeout=timeout)
        if not api:
            return None
        try:
            client = MinerClient(ip, api=api, password=password, timeout=timeout)
            return client.snapshot()
        except Exception as e:
            log.debug("scan %s: %s", ip, e)
            return MinerStatus(ip=ip, api=api, online=True, raw={"error": str(e)})

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, ip): ip for ip in host_list}
        for fut in as_completed(futs):
            st = fut.result()
            if st is not None:
                results.append(st)

    results.sort(key=lambda m: tuple(int(x) for x in m.ip.split(".")))
    return results


def map_miners(
    hosts: Iterable[str],
    fn: Callable[[MinerClient], T],
    *,
    password: str = "admin",
    workers: int = 32,
    timeout: float = 10.0,
) -> dict[str, T | Exception]:
    """Run ``fn(client)`` on each host in parallel."""

    def _one(ip: str) -> tuple[str, T | Exception]:
        try:
            api = detect_api(ip, timeout=min(timeout, 2.0)) or "v2"
            client = MinerClient(ip, api=api, password=password, timeout=timeout)
            return ip, fn(client)
        except Exception as e:
            return ip, e

    out: dict[str, T | Exception] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, ip) for ip in hosts]
        for fut in as_completed(futs):
            ip, val = fut.result()
            out[ip] = val
    return out
