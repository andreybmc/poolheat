"""
Universal miner client — auto-detect public API version and fall back to private protocol.

Priority for **reads**:
  1. API V3 (:4433) if answering
  2. API V2/V1 (:4028) if answering
  3. NetPacket private (:8889) GET_INFO / status

Priority for **writes**:
  1. Public write (V3 / V2) when token works **and** Miner API Switch is on
  2. Private NetPacket :8889 (WhatsMinerTool path) — works with API Switch off
  3. LuCI HTTPS/HTTP (pools, reboot, restart mining) when web login works

Use::

    from whatsminer import UniversalMiner

    m = UniversalMiner("192.168.1.10", password="admin", wmt_password="super")
    print(m.capabilities())
    print(m.snapshot())
    m.set_power_limit(3400)   # picks best transport automatically
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

from .errors import AuthError, CommandError, ProtocolError, WhatsminerError
from .web.luci import LuCIClient, LuCIError
from .web.wmoc import detect_wmoc
from .models import MinerStatus, PoolInfo
from .protocol.netpacket import DEFAULT_PORT as NETPACKET_PORT
from .protocol.netpacket import NetPacketClient
from .api.v1 import WhatsminerV1
from .api.v2 import WhatsminerV2
from .api.v3 import WhatsminerV3

log = logging.getLogger(__name__)

PublicApi = Literal["v1", "v2", "v3"]
Transport = Literal["v3", "v2", "v1", "netpacket", "luci", "none"]


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class Capabilities:
    """Result of probing a miner for available control paths."""

    host: str
    ports: dict[str, bool] = field(default_factory=dict)
    # public API
    public_api: PublicApi | None = None
    public_read: bool = False
    public_write: bool = False
    public_write_error: str | None = None
    api_switch: int | str | None = None  # from NetPacket GET_INFO when available
    # private / web
    netpacket: bool = False
    netpacket_error: str | None = None
    luci: bool = False
    luci_error: str | None = None
    wmoc: bool = False
    # identity (best-effort)
    miner_type: str = ""
    firmware: str = ""
    preferred_read: Transport = "none"
    preferred_write: Transport = "none"
    probed_at: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_api(
    host: str,
    *,
    prefer: PublicApi | None = None,
    timeout: float = 2.0,
) -> PublicApi | None:
    """
    Probe which public API answers.

    Order default: V3 → V2 (V1 shares port/protocol with V2).
    """
    order: list[PublicApi]
    if prefer == "v2" or prefer == "v1":
        order = ["v2", "v3"]
    elif prefer == "v3":
        order = ["v3", "v2"]
    else:
        order = ["v3", "v2"]

    for kind in order:
        port = 4433 if kind == "v3" else 4028
        if not _port_open(host, port, timeout=timeout):
            continue
        try:
            if kind == "v3":
                c = WhatsminerV3(host, timeout=timeout)
                c.get("get.device.info")
                return "v3"
            c = WhatsminerV2(host, timeout=timeout)
            ver = c.read("get_version")
            # Heuristic: Description / Msg may mention v1.3 vs later
            desc = str(ver.get("Description") or "")
            msg = ver.get("Msg")
            blob = desc + " " + (str(msg) if not isinstance(msg, dict) else str(msg))
            if "v1.3" in blob.lower() or "api v1" in blob.lower():
                return "v1"
            return "v2"
        except Exception as e:
            log.debug("detect %s on %s failed: %s", kind, host, e)
            # port open but handshake flaky — still report candidate
            return kind
    return None


def probe_capabilities(
    host: str,
    *,
    password: str = "admin",
    account: str = "super",
    v3_password: str | None = None,
    wmt_password: str = "super",
    luci_username: str = "admin",
    luci_password: str | None = None,
    timeout: float = 3.0,
    probe_write: bool = True,
    probe_luci: bool = True,
    probe_netpacket: bool = True,
) -> Capabilities:
    """
    Full capability probe (ports + public read/write + NetPacket + LuCI).

    Does **not** permanently change miner state. Write probe uses get_token /
    salt only (no destructive commands). API Switch is read from NetPacket
    GET_INFO when available.
    """
    v3_pw = v3_password if v3_password is not None else password
    luci_pw = luci_password if luci_password is not None else password
    cap = Capabilities(host=host, probed_at=time.time())
    cap.ports = {
        "4028": _port_open(host, 4028, timeout=timeout),
        "4433": _port_open(host, 4433, timeout=timeout),
        "8889": _port_open(host, NETPACKET_PORT, timeout=timeout),
        "80": _port_open(host, 80, timeout=min(timeout, 1.5)),
        "443": _port_open(host, 443, timeout=min(timeout, 1.5)),
    }

    # --- public API ---
    pub = detect_api(host, timeout=timeout)
    cap.public_api = pub
    if pub == "v3" and cap.ports.get("4433"):
        try:
            c3 = WhatsminerV3(host, account=account, password=v3_pw, timeout=timeout)
            info = c3.device_info()
            cap.public_read = True
            msg = info.get("msg") if isinstance(info.get("msg"), dict) else {}
            cap.miner_type = str(msg.get("miner_type") or msg.get("type") or "")
            cap.firmware = str(msg.get("fw_ver") or msg.get("firmware") or "")
            if probe_write:
                try:
                    c3.ensure_salt()
                    # salt present ⇒ account can form write tokens
                    cap.public_write = bool(c3.salt)
                except Exception as e:
                    cap.public_write = False
                    cap.public_write_error = str(e)
        except Exception as e:
            cap.public_read = False
            cap.notes.append(f"v3 read failed: {e}")
    elif pub in ("v1", "v2") and cap.ports.get("4028"):
        try:
            c2: WhatsminerV2
            if pub == "v1":
                c2 = WhatsminerV1(host, password=password, timeout=timeout)
            else:
                c2 = WhatsminerV2(host, password=password, timeout=timeout)
            ver = c2.get_version()
            cap.public_read = True
            vm = ver.get("Msg") if isinstance(ver.get("Msg"), dict) else {}
            if isinstance(vm, dict):
                cap.miner_type = str(vm.get("miner_type") or "")
                cap.firmware = str(vm.get("fw_ver") or "")
            if probe_write:
                try:
                    c2.refresh_token()
                    cap.public_write = True
                except AuthError as e:
                    cap.public_write = False
                    cap.public_write_error = str(e)
                except Exception as e:
                    cap.public_write = False
                    cap.public_write_error = str(e)
        except Exception as e:
            cap.public_read = False
            cap.notes.append(f"v2/v1 read failed: {e}")

    # --- NetPacket private ---
    if probe_netpacket and cap.ports.get("8889"):
        try:
            np = NetPacketClient(
                host,
                account=account,
                password=wmt_password,
                timeout=timeout,
            )
            if np.ping():
                cap.netpacket = True
                try:
                    st = np.status()
                    if st.get("api_switch") is not None:
                        cap.api_switch = st.get("api_switch")
                    if not cap.miner_type:
                        cap.miner_type = str(st.get("miner_type") or "")
                    if not cap.firmware:
                        cap.firmware = str(st.get("firmware") or "")
                    # If public write token works but switch is explicitly 0,
                    # privileged writes will get Code 45 — treat as no public write.
                    if cap.public_write and str(cap.api_switch) in ("0", "false", "False"):
                        cap.public_write = False
                        cap.public_write_error = (
                            cap.public_write_error
                            or "api_switch=0 (Miner API Switch off)"
                        )
                        cap.notes.append(
                            "public write token may work but API Switch is off — "
                            "use NetPacket for control"
                        )
                except Exception as e:
                    cap.notes.append(f"netpacket status: {e}")
            else:
                cap.netpacket = False
                cap.netpacket_error = "ping failed"
        except Exception as e:
            cap.netpacket = False
            cap.netpacket_error = str(e)

    # --- LuCI ---
    if probe_luci and (cap.ports.get("80") or cap.ports.get("443")):
        try:
            luci = LuCIClient(
                host,
                username=luci_username,
                password=luci_pw,
                timeout=timeout,
            )
            # lightweight: try login
            luci.login()
            cap.luci = True
            try:
                w = detect_wmoc(
                    host,
                    username=luci_username,
                    password=luci_pw,
                    timeout=timeout,
                )
                cap.wmoc = bool(w.get("wmoc"))
            except Exception:
                pass
        except Exception as e:
            cap.luci = False
            cap.luci_error = str(e)

    # preferred transports
    if cap.public_api == "v3" and cap.public_read:
        cap.preferred_read = "v3"
    elif cap.public_api in ("v1", "v2") and cap.public_read:
        cap.preferred_read = cap.public_api  # type: ignore[assignment]
    elif cap.netpacket:
        cap.preferred_read = "netpacket"
    elif cap.luci:
        cap.preferred_read = "luci"
    else:
        cap.preferred_read = "none"

    if cap.public_write and cap.public_api:
        cap.preferred_write = (
            "v3" if cap.public_api == "v3" else ("v1" if cap.public_api == "v1" else "v2")
        )
    elif cap.netpacket:
        cap.preferred_write = "netpacket"
        cap.notes.append("writes will use private NetPacket :8889")
    elif cap.luci:
        cap.preferred_write = "luci"
        cap.notes.append("writes limited to LuCI-capable ops (pools, reboot, restart)")
    else:
        cap.preferred_write = "none"
        cap.notes.append("no write path available")

    return cap


def _wrap_result(
    raw: Any,
    *,
    transport: Transport,
    action: str,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        out = dict(raw)
    else:
        out = {"result": raw}
    out.setdefault("ok", True)
    out["transport"] = transport
    out["action"] = action
    return out


class UniversalMiner:
    """
    Auto-detecting control façade.

    Parameters
    ----------
    host:
        Miner IP / hostname.
    password:
        Public API V1/V2 admin password (and default LuCI password).
    wmt_password:
        NetPacket / WMT Remote account password (default ``super``).
    account:
        NetPacket + V3 account (default ``super``).
    auto_probe:
        If True (default), run :meth:`probe` on first use of capabilities/writes.
    prefer_netpacket:
        If True, prefer :8889 for writes even when public write seems available.
    try_enable_api:
        If True, when public write is off but NetPacket is up, attempt
        ``set_api_switch(True)`` once (may fail with status 9 if password still default).
    """

    def __init__(
        self,
        host: str,
        *,
        password: str = "admin",
        account: str = "super",
        v3_password: str | None = None,
        wmt_password: str = "super",
        luci_username: str = "admin",
        luci_password: str | None = None,
        luci_scheme: str | None = None,
        luci_port: int | None = None,
        luci_base_url: str | None = None,
        timeout: float = 10.0,
        api: PublicApi | None = None,
        auto_probe: bool = True,
        prefer_netpacket: bool = False,
        try_enable_api: bool = False,
    ):
        self.host = host
        self.password = password
        self.account = account
        self.v3_password = v3_password if v3_password is not None else password
        self.wmt_password = wmt_password
        self.luci_username = luci_username
        self.luci_password = luci_password if luci_password is not None else password
        self.luci_scheme = luci_scheme
        self.luci_port = luci_port
        self.luci_base_url = luci_base_url
        self.timeout = timeout
        self.auto_probe = auto_probe
        self.prefer_netpacket = prefer_netpacket
        self.try_enable_api = try_enable_api

        self._caps: Capabilities | None = None
        self._api_forced = api
        # backcompat attribute used by older MinerClient code
        self.api: PublicApi = api or "v2"

        self._v1: WhatsminerV1 | None = None
        self._v2: WhatsminerV2 | None = None
        self._v3: WhatsminerV3 | None = None
        self._wmt: NetPacketClient | None = None
        self._luci: LuCIClient | None = None
        self._wmoc: dict[str, Any] | None = None
        self._api_enable_attempted = False

    # ── lazy backends ────────────────────────────────────────────────────────

    @property
    def v1(self) -> WhatsminerV1:
        if self._v1 is None:
            self._v1 = WhatsminerV1(
                self.host, password=self.password, timeout=self.timeout
            )
        return self._v1

    @property
    def v2(self) -> WhatsminerV2:
        if self._v2 is None:
            self._v2 = WhatsminerV2(
                self.host, password=self.password, timeout=self.timeout
            )
        return self._v2

    @property
    def v3(self) -> WhatsminerV3:
        if self._v3 is None:
            self._v3 = WhatsminerV3(
                self.host,
                account=self.account,
                password=self.v3_password,
                timeout=self.timeout,
            )
        return self._v3

    @property
    def wmt(self) -> NetPacketClient:
        """Private NetPacket client (:8889)."""
        if self._wmt is None:
            self._wmt = NetPacketClient(
                self.host,
                account=self.account,
                password=self.wmt_password,
                timeout=self.timeout,
            )
        return self._wmt

    # alias
    @property
    def netpacket(self) -> NetPacketClient:
        return self.wmt

    @property
    def luci(self) -> LuCIClient:
        if self._luci is None:
            self._luci = LuCIClient(
                self.host,
                username=self.luci_username,
                password=self.luci_password,
                timeout=self.timeout,
                scheme=self.luci_scheme,
                port=self.luci_port,
                base_url=self.luci_base_url,
            )
        return self._luci

    def public(self) -> WhatsminerV1 | WhatsminerV2 | WhatsminerV3:
        """Active public-API client for preferred version."""
        self._ensure_probed()
        kind = self.api
        if kind == "v3":
            return self.v3
        if kind == "v1":
            return self.v1
        return self.v2

    # ── probe ────────────────────────────────────────────────────────────────

    def probe(self, *, force: bool = False) -> Capabilities:
        """Run / refresh capability probe."""
        if self._caps is not None and not force:
            return self._caps
        self._caps = probe_capabilities(
            self.host,
            password=self.password,
            account=self.account,
            v3_password=self.v3_password,
            wmt_password=self.wmt_password,
            luci_username=self.luci_username,
            luci_password=self.luci_password,
            timeout=min(self.timeout, 5.0),
        )
        if self._api_forced:
            self.api = self._api_forced
            self._caps.public_api = self._api_forced
        elif self._caps.public_api:
            self.api = self._caps.public_api
        return self._caps

    def _ensure_probed(self) -> Capabilities:
        if self._caps is None and self.auto_probe:
            return self.probe()
        if self._caps is None:
            # minimal without network: assume v2
            self._caps = Capabilities(host=self.host, public_api=self.api)
        return self._caps

    def capabilities(self, *, force: bool = False) -> dict[str, Any]:
        """Dict form of :class:`Capabilities`."""
        return self.probe(force=force).to_dict()

    # ── transport selection ──────────────────────────────────────────────────

    def _write_transports(self) -> list[Transport]:
        cap = self._ensure_probed()
        order: list[Transport] = []
        if self.prefer_netpacket and cap.netpacket:
            order.append("netpacket")
        if cap.public_write and cap.public_api == "v3":
            order.append("v3")
        elif cap.public_write and cap.public_api == "v1":
            order.append("v1")
        elif cap.public_write:
            order.append("v2")
        if not self.prefer_netpacket and cap.netpacket:
            order.append("netpacket")
        if cap.luci:
            order.append("luci")
        # de-dupe preserve order
        seen: set[str] = set()
        out: list[Transport] = []
        for t in order:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _read_transport(self) -> Transport:
        cap = self._ensure_probed()
        return cap.preferred_read

    def _maybe_enable_public_api(self) -> None:
        if not self.try_enable_api or self._api_enable_attempted:
            return
        self._api_enable_attempted = True
        cap = self._ensure_probed()
        if cap.public_write or not cap.netpacket:
            return
        try:
            log.info("%s: trying NetPacket set_api_switch(True)", self.host)
            self.wmt.set_api_switch(True, check=False)
            # re-probe write
            self.probe(force=True)
        except Exception as e:
            log.debug("enable api switch failed: %s", e)

    def _dispatch_write(
        self,
        action: str,
        *,
        public: Callable[[], Any] | None = None,
        netpacket: Callable[[], Any] | None = None,
        luci: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        self._maybe_enable_public_api()
        errors: list[str] = []
        handlers: dict[Transport, Callable[[], Any] | None] = {
            "v3": public if self.api == "v3" else None,
            "v2": public if self.api in ("v2", None) else None,
            "v1": public if self.api == "v1" else None,
            "netpacket": netpacket,
            "luci": luci,
        }
        # Fix: public handler is shared for v1/v2/v3 — map all public slots
        if public is not None:
            handlers["v3"] = public
            handlers["v2"] = public
            handlers["v1"] = public

        for transport in self._write_transports():
            fn = handlers.get(transport)
            if fn is None:
                continue
            try:
                raw = fn()
                return _wrap_result(raw, transport=transport, action=action)
            except Exception as e:
                errors.append(f"{transport}: {e}")
                log.debug("%s write %s via %s failed: %s", self.host, action, transport, e)
                continue
        raise ProtocolError(
            f"{self.host}: no transport succeeded for {action}: "
            + "; ".join(errors[:5])
        )

    # ── high-level reads ─────────────────────────────────────────────────────

    def _snapshot_netpacket(self) -> MinerStatus:
        info = self.wmt.status()
        full: dict[str, Any] = info
        raw_keys: dict[str, Any] = {}
        try:
            full = self.wmt.get_info()
            raw_keys = full.get("raw_keys") or {}
        except Exception:
            pass
        mhs = None
        try:
            if "MHS av" in raw_keys:
                mhs = float(raw_keys["MHS av"])
        except (TypeError, ValueError):
            pass
        power = None
        try:
            if "Power" in raw_keys:
                power = float(raw_keys["Power"])
        except (TypeError, ValueError):
            pass
        pl = info.get("power_limit")
        try:
            pl_f = float(pl) if pl is not None and pl != "" else None
        except (TypeError, ValueError):
            pl_f = None
        return MinerStatus(
            ip=self.host,
            api="netpacket",
            miner_type=str(info.get("miner_type") or ""),
            firmware=str(info.get("firmware") or ""),
            power_mode=str(info.get("power_mode") or ""),
            power_w=power,
            power_limit=pl_f,
            hashrate_ths=MinerStatus.mhs_to_ths(mhs),
            mac=str(info.get("mac") or ""),
            raw={"netpacket": full},
        )

    def snapshot(self) -> MinerStatus:
        """Best-effort status snapshot (public API → NetPacket)."""
        self._ensure_probed()
        tr = self._read_transport()
        errors: list[str] = []

        attempts: list[tuple[str, Callable[[], MinerStatus]]] = []
        if tr == "v3":
            attempts.append(("v3", lambda: self.v3.snapshot()))
        elif tr == "v1":
            attempts.append(("v1", lambda: self.v1.snapshot()))
        elif tr == "v2":
            attempts.append(("v2", lambda: self.v2.snapshot()))
        elif tr == "netpacket":
            attempts.append(("netpacket", self._snapshot_netpacket))

        # always allow cascade
        for name, fn in (
            ("v3", lambda: self.v3.snapshot()),
            ("v2", lambda: self.v2.snapshot()),
            ("netpacket", self._snapshot_netpacket),
        ):
            if not any(a[0] == name for a in attempts):
                attempts.append((name, fn))

        for name, fn in attempts:
            try:
                st = fn()
                if st.api in ("", "v2") and name != "v2":
                    st.api = name
                return st
            except Exception as ex:
                errors.append(f"{name}: {ex}")
                log.debug("snapshot %s: %s", name, ex)
        raise ProtocolError(
            f"{self.host}: snapshot failed: " + "; ".join(errors[:4])
        )

    def ping(self) -> bool:
        try:
            self.snapshot()
            return True
        except Exception:
            try:
                return self.wmt.ping()
            except Exception:
                return False

    # ── unified writes ───────────────────────────────────────────────────────

    def reboot(self) -> dict[str, Any]:
        return self._dispatch_write(
            "reboot",
            public=lambda: self.public().reboot(),
            netpacket=lambda: self.wmt.reboot(),
            luci=lambda: self.luci.reboot(),
        )

    def power_off(self, respbefore: str | None = "true") -> dict[str, Any]:
        def _pub():
            p = self.public()
            if isinstance(p, WhatsminerV3):
                return p.set_service("stop")
            return p.power_off(respbefore=respbefore)

        return self._dispatch_write(
            "power_off",
            public=_pub,
            netpacket=lambda: self.wmt.suspend(),
        )

    def power_on(self) -> dict[str, Any]:
        def _pub():
            p = self.public()
            if isinstance(p, WhatsminerV3):
                return p.set_service("start")
            return p.power_on()

        return self._dispatch_write(
            "power_on",
            public=_pub,
            netpacket=lambda: self.wmt.resume(),
        )

    def set_power_mode(self, mode: str) -> dict[str, Any]:
        mode_l = mode.lower().strip()

        def _pub():
            return self.public().set_power_mode(mode_l)

        return self._dispatch_write(
            "set_power_mode",
            public=_pub,
            netpacket=lambda: self.wmt.set_power_mode(mode_l),
        )

    def set_power_limit(self, watts: int | str) -> dict[str, Any]:
        w = int(watts)

        def _pub():
            return self.public().set_power_limit(w)

        return self._dispatch_write(
            "set_power_limit",
            public=_pub,
            netpacket=lambda: self.wmt.set_power_limit(w),
        )

    def set_power_pct(self, percent: int | str, *, fast: bool = False) -> dict[str, Any]:
        pct = int(percent)

        def _pub():
            p = self.public()
            if isinstance(p, WhatsminerV3):
                return p.set_power_percent(pct, mode="fast" if fast else "normal")
            if isinstance(p, WhatsminerV1):
                return p.set_power_pct(pct)
            return p.set_power_pct(pct, fast=fast)

        return self._dispatch_write(
            "set_power_pct",
            public=_pub,
            netpacket=lambda: self.wmt.set_power_pct(pct, fast=fast),
        )

    def set_fast_boot(self, enable: bool = True) -> dict[str, Any]:
        def _pub():
            p = self.public()
            if isinstance(p, WhatsminerV3):
                return p.set_fastboot(enable)
            return p.set_fast_boot(enable)

        return self._dispatch_write(
            "set_fast_boot",
            public=_pub,
            netpacket=lambda: self.wmt.set_fast_boot(enable),
        )

    def set_heat_mode(self, mode: str) -> dict[str, Any]:
        def _pub():
            p = self.public()
            if isinstance(p, WhatsminerV3):
                return p.set_heat_mode(mode)
            if isinstance(p, WhatsminerV1):
                raise CommandError("set_heat_mode not in API V1.3.8")
            return p.set_heat_mode(mode)

        return self._dispatch_write(
            "set_heat_mode",
            public=_pub,
            netpacket=lambda: self.wmt.set_heat_mode(mode),
        )

    def factory_reset(self, *, netpacket_only: bool = True) -> dict[str, Any]:
        """
        Factory restore.

        Default **netpacket_only=True** (WhatsMinerTool path on :8889). Public
        API factory_reset is unreliable / token-limited; only used when
        ``netpacket_only=False`` and NetPacket is unavailable.
        """
        if netpacket_only:
            raw = self.wmt.factory_reset()
            return _wrap_result(raw, transport="netpacket", action="factory_reset")
        return self._dispatch_write(
            "factory_reset",
            public=lambda: self.public().factory_reset(),
            netpacket=lambda: self.wmt.factory_reset(),
        )

    def update_firmware(
        self,
        image: bytes,
        *,
        poll_status: bool = True,
        poll_attempts: int = 30,
        wait_upload: float = 600.0,
        progress: Any = None,
        netpacket_only: bool = True,
    ) -> dict[str, Any]:
        """
        Firmware upgrade via **NetPacket :8889** (WhatsMinerTool cmd 7 + status 21).

        Public TCP ``update_firmware`` is not used by default (``netpacket_only``).
        ``progress`` is forwarded to :meth:`NetPacketClient.update_firmware`.
        """
        if not image:
            raise ValueError("firmware image is empty")
        if not netpacket_only:
            # reserved for future public fallback — still prefer NetPacket first
            pass
        raw = self.wmt.update_firmware(
            image,
            poll_status=poll_status,
            poll_attempts=poll_attempts,
            wait_upload=wait_upload,
            progress=progress,
        )
        return _wrap_result(raw, transport="netpacket", action="update_firmware")

    def restart_mining(self) -> dict[str, Any]:
        def _pub():
            p = self.public()
            if isinstance(p, WhatsminerV3):
                return p.set_service("restart")
            return p.restart_btminer()

        return self._dispatch_write(
            "restart_mining",
            public=_pub,
            netpacket=lambda: self.wmt.set_mining(True),  # resume; no direct restart
            luci=lambda: self._luci_restart_mining(),
        )

    def _luci_restart_mining(self) -> dict[str, Any]:
        self.luci.restart_miner()
        return {"ok": True, "source": "luci", "action": "restart_mining"}

    def update_pools(
        self,
        pool1: str,
        worker1: str,
        passwd1: str = "x",
        pool2: str = "",
        worker2: str = "",
        passwd2: str = "x",
        pool3: str = "",
        worker3: str = "",
        passwd3: str = "x",
        *,
        coin_type: str | None = None,
        restart_mining: bool = True,
    ) -> dict[str, Any]:
        """
        Write up to 3 pool slots (auto transport).

        ``coin_type`` is applied on the LuCI path (form field) and, when the
        write went NetPacket/public, also via NetPacket SET_COIN / LuCI follow-up
        so presets like DGB actually stick. LuCI path restarts btminer by
        default so stratum switches immediately (deferred config is not enough).
        """
        coin = str(coin_type or "").strip()

        def _pub():
            p = self.public()
            if isinstance(p, WhatsminerV3):
                return p.set_pools(
                    [
                        {"pool": pool1, "worker": worker1, "passwd": passwd1},
                        {"pool": pool2, "worker": worker2, "passwd": passwd2},
                        {"pool": pool3, "worker": worker3, "passwd": passwd3},
                    ]
                )
            return p.update_pools(
                pool1,
                worker1,
                passwd1,
                pool2,
                worker2,
                passwd2,
                pool3,
                worker3,
                passwd3,
            )

        def _np():
            # NetPacket set_pools takes structured list
            pools = []
            for i, (u, w, pw) in enumerate(
                (
                    (pool1, worker1, passwd1),
                    (pool2, worker2, passwd2),
                    (pool3, worker3, passwd3),
                )
            ):
                if u or w:
                    pools.append({"url": u, "user": w, "password": pw, "index": i})
            raw = self.wmt.set_pools(pools)
            # Coin is a separate WMT cmd; best-effort after pools
            if coin:
                try:
                    self.wmt.set_coin_type(coin)
                    if isinstance(raw, dict):
                        raw = dict(raw)
                        raw["coin_type"] = coin
                except Exception as e:
                    log.debug("%s set_coin_type(%s) after NP pools: %s", self.host, coin, e)
            return raw

        def _luci():
            pools = []
            for i, (u, w, pw) in enumerate(
                (
                    (pool1, worker1, passwd1),
                    (pool2, worker2, passwd2),
                    (pool3, worker3, passwd3),
                )
            ):
                pools.append(
                    {"index": i, "url": u, "user": w, "password": pw}
                )
            # Always pass coin_type when known; LuCI keeps page value only if
            # caller left default BTC (see luci.update_pools).
            kw: dict[str, Any] = {"restart_mining": bool(restart_mining)}
            if coin:
                kw["coin_type"] = coin
            return self.luci.set_pools(pools, **kw)

        result = self._dispatch_write(
            "update_pools",
            public=_pub,
            netpacket=_np,
            luci=_luci,
        )

        # Public/NetPacket may leave coin/stratum stale — finish via LuCI when needed
        transport = str((result or {}).get("transport") or "")
        need_coin = bool(coin) and str((result or {}).get("coin_type") or "").upper() != coin.upper()
        need_restart = bool(restart_mining) and transport in ("netpacket", "v1", "v2", "v3", "public")
        if (need_coin or need_restart) and transport != "luci":
            try:
                pools = []
                for i, (u, w, pw) in enumerate(
                    (
                        (pool1, worker1, passwd1),
                        (pool2, worker2, passwd2),
                        (pool3, worker3, passwd3),
                    )
                ):
                    if u or w:
                        pools.append(
                            {"index": i, "url": u, "user": w, "password": pw}
                        )
                kw2: dict[str, Any] = {
                    "restart_mining": bool(restart_mining),
                }
                if coin:
                    kw2["coin_type"] = coin
                luci_out = self.luci.set_pools(pools, **kw2)
                if isinstance(result, dict):
                    result = dict(result)
                    result["coin_followup"] = luci_out
                    if coin:
                        result["coin_type"] = coin
                    result["restart_mining"] = bool(
                        (luci_out or {}).get("restart_mining") or restart_mining
                    )
            except Exception as e:
                log.debug("%s luci follow-up after %s pools: %s", self.host, transport, e)
                if isinstance(result, dict) and coin:
                    result = dict(result)
                    result["coin_followup_error"] = str(e)
        elif isinstance(result, dict) and coin and not result.get("coin_type"):
            result = dict(result)
            result["coin_type"] = coin
        return result

    def set_api_switch(self, enabled: bool = True) -> dict[str, Any]:
        """
        Enable/disable public Miner API Switch via NetPacket (WMT path).

        This is the usual way to turn public write on without WhatsMinerTool UI.
        """
        if not self._ensure_probed().netpacket and not _port_open(
            self.host, NETPACKET_PORT, timeout=2.0
        ):
            raise ProtocolError(f"{self.host}: NetPacket :8889 not available")
        raw = self.wmt.set_api_switch(enabled, check=False)
        self.probe(force=True)
        return _wrap_result(raw, transport="netpacket", action="set_api_switch")

    # ── pools / LuCI helpers (from MinerClient) ──────────────────────────────

    def get_pools(self, *, include_passwords: bool = False) -> dict[str, Any]:
        """Read pools via public API; optional LuCI merge for passwords."""
        self._ensure_probed()
        result: dict[str, Any] = {"pools": [], "source": self.api}
        api_pools: list[dict[str, Any]] = []
        try:
            if self.api == "v3":
                try:
                    st = self.v3.get("get.miner.status")
                    msg = st.get("msg") or st.get("Msg") or st
                    raw_list = []
                    if isinstance(msg, dict):
                        raw_list = msg.get("pools") or msg.get("POOLS") or []
                    for i, p in enumerate(raw_list or []):
                        if not isinstance(p, dict):
                            continue
                        api_pools.append(
                            {
                                "index": int(p.get("pool", p.get("POOL", i)) or i),
                                "url": str(
                                    p.get("url") or p.get("URL") or p.get("pool") or ""
                                ),
                                "user": str(
                                    p.get("user")
                                    or p.get("User")
                                    or p.get("worker")
                                    or ""
                                ),
                                "status": str(p.get("status") or p.get("Status") or ""),
                                "raw": p,
                            }
                        )
                except Exception as e:
                    log.debug("v3 pools: %s", e)
            elif self._ensure_probed().public_read:
                pr = self.v2.pools()
                for p in pr.get("POOLS") or []:
                    idx = int(p.get("POOL", 0))
                    if idx >= 1:
                        idx -= 1
                    api_pools.append(
                        {
                            "index": idx,
                            "url": str(p.get("URL") or p.get("Stratum URL") or ""),
                            "user": str(p.get("User") or ""),
                            "status": str(p.get("Status") or ""),
                            "accepted": int(p.get("Accepted") or 0),
                            "rejected": int(p.get("Rejected") or 0),
                            "raw": p,
                        }
                    )
            result["pools"] = api_pools
            result["source"] = self.api
        except Exception as e:
            result["api_error"] = str(e)

        if not include_passwords:
            return result
        try:
            luci_data = self.luci.get_pools()
        except LuCIError as e:
            result["luci_error"] = str(e)
            return result
        result["coin_type"] = luci_data.get("coin_type")
        luci_pools = luci_data.get("pools") or []
        if not result["pools"]:
            result["pools"] = luci_pools
            result["source"] = "luci"
            return result
        by_idx = {int(p["index"]): p for p in luci_pools if "index" in p}
        for p in result["pools"]:
            lp = by_idx.get(int(p.get("index", -1)))
            if lp is not None:
                p["password"] = lp.get("password")
        result["source"] = f"{self.api}+luci"
        return result

    def get_pools_list(self, *, include_passwords: bool = False) -> list[PoolInfo]:
        data = self.get_pools(include_passwords=include_passwords)
        out: list[PoolInfo] = []
        for p in data.get("pools") or []:
            out.append(
                PoolInfo(
                    index=int(p.get("index", 0)),
                    url=str(p.get("url") or ""),
                    user=str(p.get("user") or ""),
                    password=p.get("password") if include_passwords else None,
                    status=str(p.get("status") or ""),
                    accepted=int(p.get("accepted") or 0),
                    rejected=int(p.get("rejected") or 0),
                    raw=p.get("raw") or p,
                )
            )
        return out

    def set_pools_luci(
        self,
        pools: list[dict[str, Any]],
        *,
        coin_type: str = "BTC",
        restart_mining: bool = True,
    ) -> dict[str, Any]:
        return self.luci.set_pools(
            pools, coin_type=coin_type, restart_mining=restart_mining
        )

    def reinstall_pools_luci(
        self,
        pools: list[dict[str, Any]] | None = None,
        *,
        coin_type: str = "BTC",
        restart_mining: bool = False,
    ) -> dict[str, Any]:
        return self.luci.reinstall_pools(
            pools, coin_type=coin_type, restart_mining=restart_mining
        )

    def restart_mining_luci(self) -> dict[str, Any]:
        self.luci.restart_miner()
        return {"ok": True, "source": "luci", "action": "restart_mining"}

    # ── WMOC (see whatsminer.web.wmoc) ───────────────────────────────────────────

    @property
    def wmoc(self):
        """:class:`~whatsminer.web.wmoc.WMOCClient` over this miner's LuCI session."""
        from .web.wmoc import WMOCClient

        return WMOCClient(self.luci)

    def detect_wmoc(self, *, login: bool = True) -> dict[str, Any]:
        self._wmoc = self.wmoc.detect(login=login)
        return self._wmoc

    def has_wmoc(self, *, login: bool = True) -> bool:
        if self._wmoc is None:
            self.detect_wmoc(login=login)
        return bool(self._wmoc and self._wmoc.get("wmoc"))

    @property
    def is_wmoc(self) -> bool:
        return self.has_wmoc()

    def get_wmoc_history(self, *, max_records: int | None = None) -> dict[str, Any]:
        return self.wmoc.get_history(max_records=max_records)

    def analyze_wmoc_psu(self, **kwargs: Any) -> dict[str, Any]:
        return self.wmoc.analyze_psu(**kwargs)

    def estimate_wall_power(
        self,
        hashrate_ths: float,
        *,
        power_rt_w: float | None = None,
        miner_type: str | None = None,
        joules_per_th: float | None = None,
        prefer: str = "mid",
    ) -> dict[str, Any]:
        from .support.power_model import (
            compare_reported_vs_model_power,
            estimate_power_from_hashrate,
        )

        mt = miner_type or "M60"
        if power_rt_w is not None:
            return compare_reported_vs_model_power(
                power_rt_w,
                hashrate_ths,
                joules_per_th=joules_per_th,
                miner_type=mt,
                prefer=prefer,
            )
        return estimate_power_from_hashrate(
            hashrate_ths,
            joules_per_th=joules_per_th,
            miner_type=mt,
            prefer=prefer,
        )

    def estimate_wmoc_wall_power(
        self,
        *,
        miner_type: str | None = "M60",
        joules_per_th: float | None = None,
        prefer: str = "mid",
        max_records: int | None = 100,
    ) -> dict[str, Any]:
        return self.wmoc.estimate_wall_power(
            miner_type=miner_type,
            joules_per_th=joules_per_th,
            prefer=prefer,
            max_records=max_records,
        )
