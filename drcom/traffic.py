"""Interface traffic accounting: instantaneous rate plus session totals.

Wraps :mod:`drcom.netiface` counters into deltas.  Handles the two things that
always bite here: counter wraparound/reboot (negative delta → resync) and the
interface disappearing (adapter disabled) → we re-pick it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .netiface import (
    InterfaceInfo,
    _interface_addresses,
    get_interface_by_index,
    list_interfaces,
    pick_relevant_interface,
)

__all__ = ["TrafficMeter", "TrafficSnapshot"]

_ORIG_LIST_INTERFACES = list_interfaces


@dataclass(frozen=True)
class TrafficSnapshot:
    """A traffic reading."""

    interface: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_rate: float = 0.0  # bytes / second
    tx_rate: float = 0.0
    total_rx: int = 0  # since the meter started
    total_tx: int = 0
    timestamp: float = 0.0

    @property
    def rate_label(self) -> str:
        return f"↓{format_bytes(self.rx_rate)}/s  ↑{format_bytes(self.tx_rate)}/s"

    @property
    def total_label(self) -> str:
        return f"↓{format_bytes(self.total_rx)}  ↑{format_bytes(self.total_tx)}"


def format_bytes(value: float) -> str:
    """Human-readable byte count (1 decimal place, binary units)."""
    if value < 0:
        value = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_rate(value: float) -> str:
    return f"{format_bytes(value)}/s"


class TrafficMeter:
    """Samples interface counters over time."""

    #: Samples closer together than this reuse the previous rate.
    MIN_SAMPLE_INTERVAL = 0.4

    def __init__(self, *, server: str = "10.100.61.3", port: int = 61440, log=None) -> None:
        # Re-entrant: _resolve_interface() is called with the lock held.
        # sample() runs on the ticker thread *and* on HTTP request threads
        # (the status payload samples traffic), so this must be guarded.
        self._lock = threading.RLock()
        self._iface: InterfaceInfo | None = None
        self._last: tuple[int, int, float] | None = None
        self._baseline: tuple[int, int, int] | None = None
        self._totals = [0, 0]
        self._snapshot = TrafficSnapshot(timestamp=time.time())
        self._server = server
        self._port = port
        self.log = log
        self._failures = 0
        self._misses = 0
        #: When the auth engine binds to a concrete address, count bytes on the
        #: interface that owns it.  Otherwise a Wi-Fi link with a better route
        #: metric would be measured instead of the link actually authenticating.
        self.source_address = ""

    @property
    def server(self) -> str:
        return self._server

    @server.setter
    def server(self, value: str) -> None:
        self._server = value

    @property
    def port(self) -> int:
        return self._port

    @port.setter
    def port(self, value: int) -> None:
        self._port = value

    @property
    def snapshot(self) -> TrafficSnapshot:
        with self._lock:
            return self._snapshot

    def reset_totals(self) -> None:
        with self._lock:
            self._totals = [0, 0]
            self._baseline = None

    def set_source_address(self, address: str) -> None:
        """Pin accounting to the interface that owns *address*."""
        address = address if address and address != "0.0.0.0" else ""
        with self._lock:
            if address != self.source_address:
                self.source_address = address
                self._iface = None
                self._last = None
                self._baseline = None
                self._misses = 0

    def sync_baseline(
        self,
        *,
        if_index: int = 0,
        rx_base: int | None = None,
        tx_base: int | None = None,
        session_rx: int | None = None,
        session_tx: int | None = None,
    ) -> None:
        """Align session totals to a resident core's OS NIC counter baseline.

        When the GUI closes and later re-attaches to ``drcomd``, the daemon
        already recorded the interface byte counters at login time.  Syncing to
        that baseline immediately restores all bytes transferred while the UI
        was closed without any polling inside ``drcomd``.
        """
        with self._lock:
            now = time.time()
            iface: InterfaceInfo | None = None
            if if_index and if_index > 0:
                if list_interfaces is _ORIG_LIST_INTERFACES:
                    iface = get_interface_by_index(int(if_index))
                else:
                    for item in list_interfaces():
                        if item.index == int(if_index):
                            iface = item
                            break
                if iface is not None:
                    self._iface = iface
                    self._misses = 0

            if (
                if_index
                and if_index > 0
                and rx_base is not None
                and tx_base is not None
                and int(rx_base) >= 0
                and int(tx_base) >= 0
            ):
                base_idx = int(if_index)
                base_rx = int(rx_base)
                base_tx = int(tx_base)
                self._baseline = (base_idx, base_rx, base_tx)
                if iface is not None and iface.bytes_in >= base_rx and iface.bytes_out >= base_tx:
                    total_rx = iface.bytes_in - base_rx
                    total_tx = iface.bytes_out - base_tx
                    self._totals = [total_rx, total_tx]
                    self._last = (iface.bytes_in, iface.bytes_out, now)
                    self._snapshot = TrafficSnapshot(
                        interface=iface.label,
                        rx_bytes=iface.bytes_in,
                        tx_bytes=iface.bytes_out,
                        rx_rate=self._snapshot.rx_rate,
                        tx_rate=self._snapshot.tx_rate,
                        total_rx=total_rx,
                        total_tx=total_tx,
                        timestamp=now,
                    )
                    return

            if session_rx is not None and session_tx is not None:
                total_rx = max(0, int(session_rx))
                total_tx = max(0, int(session_tx))
                self._totals = [total_rx, total_tx]
                self._snapshot = TrafficSnapshot(
                    interface=iface.label if iface is not None else self._snapshot.interface,
                    rx_bytes=iface.bytes_in if iface is not None else self._snapshot.rx_bytes,
                    tx_bytes=iface.bytes_out if iface is not None else self._snapshot.tx_bytes,
                    rx_rate=self._snapshot.rx_rate,
                    tx_rate=self._snapshot.tx_rate,
                    total_rx=total_rx,
                    total_tx=total_tx,
                    timestamp=now,
                )

    def sample(self) -> TrafficSnapshot:
        """Take a reading and return the updated snapshot (thread-safe)."""
        with self._lock:
            return self._sample_locked()

    def _sample_locked(self) -> TrafficSnapshot:
        iface = self._resolve_interface()
        now = time.time()
        if iface is None:
            self._snapshot = TrafficSnapshot(
                timestamp=now, total_rx=self._totals[0], total_tx=self._totals[1]
            )
            return self._snapshot

        rx, tx = iface.bytes_in, iface.bytes_out
        rx_rate = tx_rate = 0.0
        delta_rx = delta_tx = 0
        had_last = self._last is not None
        if self._last is not None:
            last_rx, last_tx, last_at = self._last
            elapsed = now - last_at
            # Two samples closer together than this cannot produce a meaningful
            # rate; keep the previous one instead of reporting noise or zero.
            if elapsed < self.MIN_SAMPLE_INTERVAL:
                return self._snapshot
            elapsed = max(1e-3, elapsed)
            delta_rx, delta_tx = rx - last_rx, tx - last_tx
            # Negative deltas mean the counter wrapped or the adapter reset.
            if delta_rx < 0 or delta_tx < 0:
                delta_rx = delta_tx = 0
                if self.log:
                    self.log.debug("网卡计数器回绕/重置，已重新同步")
            rx_rate = delta_rx / elapsed
            tx_rate = delta_tx / elapsed

        used_baseline = False
        if self._baseline is not None:
            base_idx, base_rx, base_tx = self._baseline
            if iface.index == base_idx:
                if rx >= base_rx and tx >= base_tx:
                    self._totals[0] = rx - base_rx
                    self._totals[1] = tx - base_tx
                    used_baseline = True
                else:
                    self._baseline = None
        if not used_baseline and had_last:
            self._totals[0] += max(0, delta_rx)
            self._totals[1] += max(0, delta_tx)

        self._last = (rx, tx, now)
        self._snapshot = TrafficSnapshot(
            interface=iface.label,
            rx_bytes=rx,
            tx_bytes=tx,
            rx_rate=rx_rate,
            tx_rate=tx_rate,
            total_rx=self._totals[0],
            total_tx=self._totals[1],
            timestamp=now,
        )
        return self._snapshot

    def _resolve_interface(self) -> InterfaceInfo | None:
        """Find the interface we care about, re-picking it if it vanished.

        The caller must hold :attr:`_lock`: ``sample()`` runs on the ticker
        thread *and* on HTTP request threads (the status payload samples
        traffic), so the cached adapter could otherwise be nulled under a live
        reader — which is the AttributeError the long soak run caught.
        """
        if self._iface is not None:
            wanted_index = self._iface.index
            candidate: InterfaceInfo | None = None
            if list_interfaces is _ORIG_LIST_INTERFACES and wanted_index > 0:
                candidate = get_interface_by_index(wanted_index)
            else:
                for item in list_interfaces():
                    if item.index == wanted_index:
                        candidate = item
                        break
            if candidate is not None:
                self._iface = candidate
                self._misses = 0
                return candidate
            # A single missed lookup happens while an adapter renegotiates
            # (link up/down, DHCP renew).  Only give up on the cached adapter
            # once it stays missing across several consecutive samples.
            self._misses += 1
            if self._misses < 3:
                return self._iface
            if self.log:
                self.log.debug("网卡 %s 连续多次查不到，重新选择", self._iface.label)
            self._iface = None
            self._last = None
            self._misses = 0

        # Prefer the interface that owns the address we authenticated from.
        if self.source_address:
            try:
                # NB: `list_interfaces` must come from the module-level import.
                # Importing it here would make it a local name for the whole
                # function scope and break the fallback path below with
                # UnboundLocalError.
                addresses = _interface_addresses()
                for candidate in list_interfaces():
                    if self.source_address in addresses.get(candidate.index, ()):
                        self._iface = candidate
                        return candidate
            except Exception as exc:  # pragma: no cover - fall back to scoring
                if self.log:
                    self.log.debug("按绑定地址定位网卡失败：%r", exc)

        iface = pick_relevant_interface(server=self._server, port=self._port)
        if iface is None:
            self._failures += 1
            return None
        self._iface = iface
        return iface
