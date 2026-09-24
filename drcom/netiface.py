"""Network interface information: addresses, MAC and byte counters.

No third-party dependency: on Windows we go through ``iphlpapi!GetIfTable2``
with ctypes, on Linux/macOS through ``/proc/net/dev`` or ``netstat``.  The
layout of ``MIB_IF_ROW2`` is validated at runtime (we check that the adapter
alias decodes to something sane) and the whole subsystem disables itself
quietly if the OS surprises us — traffic counters are a nice-to-have, never a
reason to fail authentication.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import socket
import subprocess
import sys
from dataclasses import dataclass

__all__ = ["InterfaceInfo", "get_default_route_ip", "list_interfaces", "pick_relevant_interface"]

IS_WINDOWS = sys.platform == "win32"

IF_MAX_STRING_SIZE = 256
IF_MAX_PHYS_ADDRESS_LENGTH = 32

_IF_TYPE_SOFTWARE_LOOPBACK = 24
_IF_TYPE_TUNNEL = 131
_IF_TYPE_IEEE80211 = 71
_IF_TYPE_ETHERNET_CSMACD = 6


@dataclass
class InterfaceInfo:
    """A snapshot of one interface."""

    name: str
    description: str = ""
    index: int = 0
    mtu: int = 0
    speed_bps: int = 0
    if_type: int = 0
    oper_status: int = 0
    mac: str = ""
    bytes_in: int = 0
    bytes_out: int = 0
    packets_in: int = 0
    packets_out: int = 0
    errors_in: int = 0
    errors_out: int = 0
    is_up: bool = False

    @property
    def kind(self) -> str:
        if self.if_type == _IF_TYPE_IEEE80211:
            return "无线"
        if self.if_type == _IF_TYPE_ETHERNET_CSMACD:
            return "以太网"
        if self.if_type == _IF_TYPE_TUNNEL:
            return "隧道/VPN"
        if self.if_type == _IF_TYPE_SOFTWARE_LOOPBACK:
            return "回环"
        return "其它"

    @property
    def label(self) -> str:
        return self.name or self.description or f"if{self.index}"


# --------------------------------------------------------------------------
# Windows: GetIfTable2
# --------------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _MIB_IF_ROW2(ctypes.Structure):
    """Mirror of the Win32 ``MIB_IF_ROW2`` (netioapi.h)."""

    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("InterfaceIndex", ctypes.c_ulong),
        ("InterfaceGuid", _GUID),
        ("Alias", ctypes.c_wchar * (IF_MAX_STRING_SIZE + 1)),
        ("Description", ctypes.c_wchar * (IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("PhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TunnelType", ctypes.c_ulong),
        ("MediaType", ctypes.c_ulong),
        ("PhysicalMediumType", ctypes.c_ulong),
        ("AccessType", ctypes.c_ulong),
        ("DirectionType", ctypes.c_ulong),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte * 8),
        ("OperStatus", ctypes.c_ulong),
        ("AdminStatus", ctypes.c_ulong),
        ("MediaConnectState", ctypes.c_ulong),
        ("NetworkGuid", _GUID),
        ("ConnectionType", ctypes.c_ulong),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("InOctets", ctypes.c_ulonglong),
        ("InUcastPkts", ctypes.c_ulonglong),
        ("InNUcastPkts", ctypes.c_ulonglong),
        ("InDiscards", ctypes.c_ulonglong),
        ("InErrors", ctypes.c_ulonglong),
        ("InUnknownProtos", ctypes.c_ulonglong),
        ("InUcastOctets", ctypes.c_ulonglong),
        ("InMulticastOctets", ctypes.c_ulonglong),
        ("InBroadcastOctets", ctypes.c_ulonglong),
        ("OutOctets", ctypes.c_ulonglong),
        ("OutUcastPkts", ctypes.c_ulonglong),
        ("OutNUcastPkts", ctypes.c_ulonglong),
        ("OutDiscards", ctypes.c_ulonglong),
        ("OutErrors", ctypes.c_ulonglong),
        ("OutUcastOctets", ctypes.c_ulonglong),
        ("OutMulticastOctets", ctypes.c_ulonglong),
        ("OutBroadcastOctets", ctypes.c_ulonglong),
        ("OutQLen", ctypes.c_ulonglong),
    ]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", ctypes.c_ulong), ("Table", _MIB_IF_ROW2 * 1)]


_windows_api_ok: bool | None = None


def _windows_if_table() -> list[_MIB_IF_ROW2]:
    """Read the interface table, or ``[]`` if the API/structure is unusable."""
    global _windows_api_ok
    if not IS_WINDOWS:
        return []
    if _windows_api_ok is False:
        return []
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        get_table = iphlpapi.GetIfTable2
        get_table.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        get_table.restype = ctypes.c_ulong
        free_table = iphlpapi.FreeMibTable
        free_table.argtypes = [ctypes.c_void_p]
        free_table.restype = None
    except (OSError, AttributeError):
        _windows_api_ok = False
        return []

    table_ptr = ctypes.c_void_p()
    if get_table(ctypes.byref(table_ptr)) != 0 or not table_ptr:
        _windows_api_ok = False
        return []

    try:
        count = ctypes.cast(table_ptr, ctypes.POINTER(ctypes.c_ulong)).contents.value
        if count <= 0 or count > 4096:
            _windows_api_ok = False
            return []
        # MIB_IF_TABLE2 is { ULONG NumEntries; MIB_IF_ROW2 Table[1]; }.  The
        # rows begin right after the ULONG, and since MIB_IF_ROW2 starts with a
        # ULONG64 the compiler pads that start up to an 8-byte boundary.
        table_addr = ctypes.addressof(ctypes.cast(table_ptr, ctypes.POINTER(_MIB_IF_TABLE2)).contents)
        rows_addr = (table_addr + ctypes.sizeof(ctypes.c_ulong) + 7) & ~7
        row_array = (_MIB_IF_ROW2 * count).from_address(rows_addr)
        rows = [row_array[i] for i in range(count)]

        # --- sanity check the struct layout ---------------------------------
        if rows and not any(r.Alias for r in rows[: min(4, count)]):
            _windows_api_ok = False
            return []
        _windows_api_ok = True
        return rows
    except (ValueError, OSError, TypeError):
        _windows_api_ok = False
        return []
    finally:
        free_table(table_ptr)


# --------------------------------------------------------------------------
# POSIX fallback
# --------------------------------------------------------------------------
def _posix_if_counters() -> list[InterfaceInfo]:
    out: list[InterfaceInfo] = []
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as handle:
            lines = handle.readlines()[2:]
    except OSError:
        return out
    for line in lines:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) < 16:
            continue
        out.append(
            InterfaceInfo(
                name=name.strip(),
                bytes_in=int(parts[0]),
                packets_in=int(parts[1]),
                errors_in=int(parts[2]),
                bytes_out=int(parts[8]),
                packets_out=int(parts[9]),
                errors_out=int(parts[10]),
                is_up=True,
            )
        )
    return out


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------
def list_interfaces() -> list[InterfaceInfo]:
    """Every interface the OS will tell us about, with counters."""
    if IS_WINDOWS:
        rows = _windows_if_table()
        out: list[InterfaceInfo] = []
        for row in rows:
            length = min(int(row.PhysicalAddressLength), IF_MAX_PHYS_ADDRESS_LENGTH)
            mac = (
                ":".join(f"{row.PhysicalAddress[i]:02X}" for i in range(length))
                if 0 < length <= IF_MAX_PHYS_ADDRESS_LENGTH
                else ""
            )
            out.append(
                InterfaceInfo(
                    name=str(row.Alias or ""),
                    description=str(row.Description or ""),
                    index=int(row.InterfaceIndex),
                    mtu=int(row.Mtu),
                    speed_bps=int(row.ReceiveLinkSpeed or row.TransmitLinkSpeed),
                    if_type=int(row.Type),
                    oper_status=int(row.OperStatus),
                    mac=mac,
                    bytes_in=int(row.InOctets),
                    bytes_out=int(row.OutOctets),
                    packets_in=int(row.InUcastPkts + row.InNUcastPkts),
                    packets_out=int(row.OutUcastPkts + row.OutNUcastPkts),
                    errors_in=int(row.InErrors),
                    errors_out=int(row.OutErrors),
                    is_up=int(row.OperStatus) == 1,
                )
            )
        if out:
            return out
    return _posix_if_counters()


def get_default_route_ip(server: str = "10.100.61.3", port: int = 61440) -> str:
    """The local address the OS would use to reach *server* — no packets sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((server, port))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


#: Adapter-name fragments that identify NDIS filter shims and virtual switches
#: rather than a physical NIC.  They report the same counters as their parent
#: but are useless for picking "the campus network adapter".
_VIRTUAL_HINTS = (
    "wfp ", "wfp-", "qos packet scheduler", "ndis 6", "virtual wifi filter",
    "native wifi filter", "mac layer", "lightweight filter", "vswitch",
    "vethernet", "hyper-v", "loopback", "teredo", "isatap", "bluetooth",
    "tap-", "tun ", "wintun", "kaspersky", "miniport", "wan miniport",
)


def _score_interface(iface: InterfaceInfo, route_ip: str, addrs: dict[int, list[str]]) -> int:
    """Rank an interface by how likely it is to be the real uplink.

    Higher is better.  The dominant signal is "the OS would route to the auth
    server through me"; after that we prefer physical media with real traffic
    and penalise NDIS filter shims / virtual switches.
    """
    score = 0
    haystack = f"{iface.name} {iface.description}".lower()

    if route_ip and route_ip in addrs.get(iface.index, ()):
        score += 1000
    elif route_ip and route_ip in (iface.name, iface.description):
        score += 500

    if iface.if_type == _IF_TYPE_ETHERNET_CSMACD:
        score += 60
    elif iface.if_type == _IF_TYPE_IEEE80211:
        score += 50
    elif iface.if_type == _IF_TYPE_TUNNEL:
        score -= 200
    elif iface.if_type == _IF_TYPE_SOFTWARE_LOOPBACK:
        score -= 500

    if iface.mac:
        score += 20
    if iface.is_up:
        score += 10
    # Interfaces that have actually carried traffic are the real thing.
    if iface.bytes_in > 0 or iface.bytes_out > 0:
        score += 30
    # A name like "以太网" / "Ethernet" / "WLAN" with no filter suffix is a
    # strong hint it is the base adapter rather than one of its shims.
    for shim in _VIRTUAL_HINTS:
        if shim in haystack:
            score -= 120
            break
    return score


def pick_relevant_interface(
    *, server: str = "10.100.61.3", port: int = 61440
) -> InterfaceInfo | None:
    """Best guess at the campus-network adapter.

    Deliberately a *scoring* function rather than a first-match rule: on a
    typical Windows box ``GetIfTable2`` returns dozens of entries, most of them
    NDIS filter shims that shadow a physical adapter's counters (this machine
    reports 65).  Picking the first "up" one grabs a Hyper-V switch.
    """
    interfaces = [i for i in list_interfaces() if i.if_type != _IF_TYPE_SOFTWARE_LOOPBACK]
    if not interfaces:
        return None

    route_ip = get_default_route_ip(server, port)
    addrs = _interface_addresses() if route_ip else {}
    # A loopback route (e.g. when pointing at a local test server) tells us
    # nothing about which physical adapter to use.
    if route_ip.startswith("127."):
        route_ip = ""

    ranked = sorted(
        interfaces,
        key=lambda item: _score_interface(item, route_ip, addrs),
        reverse=True,
    )
    best = ranked[0]
    return best if _score_interface(best, route_ip, addrs) > -100 else interfaces[0]


def _interface_addresses() -> dict[int, list[str]]:
    """Map interface index → IPv4 addresses (Windows)."""
    mapping: dict[int, list[str]] = {}
    if not IS_WINDOWS:
        return mapping
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    except OSError:
        return mapping

    class _SOCKADDR_IN(ctypes.Structure):
        _fields_ = [
            ("sin_family", ctypes.c_ushort),
            ("sin_port", ctypes.c_ushort),
            ("sin_addr", ctypes.c_ubyte * 4),
            ("sin_zero", ctypes.c_char * 8),
        ]

    class _SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
        pass

    _IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", ctypes.c_ulong),
        ("IfIndex", ctypes.c_ulong),
        ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Mtu", ctypes.c_ulong),
        ("IfType", ctypes.c_ulong),
        ("OperStatus", ctypes.c_ulong),
        ("Ipv6IfIndex", ctypes.c_ulong),
        ("ZoneIndices", ctypes.c_ulong * 16),
        ("FirstPrefix", ctypes.c_void_p),
    ]

    class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
        pass

    _IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
        ("Length", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
        ("Address", _SOCKET_ADDRESS),
    ]

    flags = 0x0080  # GAA_FLAG_SKIP_ANYCAST | SKIP_MULTICAST | SKIP_DNS_SERVER
    size = ctypes.c_ulong(16 * 1024)
    buf = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetAdaptersAddresses(
        socket.AF_INET, flags, None, ctypes.byref(buf), ctypes.byref(size)
    )
    if ret != 0:
        return mapping

    node = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    while node:
        entry = node.contents
        addrs: list[str] = []
        unicast = ctypes.cast(entry.FirstUnicastAddress, ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS))
        while unicast:
            sockaddr = ctypes.cast(unicast.contents.Address.lpSockaddr, ctypes.POINTER(_SOCKADDR_IN))
            if sockaddr and sockaddr.contents.sin_family == socket.AF_INET:
                addrs.append(".".join(str(b) for b in sockaddr.contents.sin_addr))
            unicast = unicast.contents.Next
        mapping[int(entry.IfIndex)] = addrs
        node = entry.Next
    return mapping
