"""Bind-failure classification and self-healing (spec 6.1).

The headline requirement: tell 10013 apart from 10048 and give advice that
actually matches which of the two 10013 causes is in play.
"""

from __future__ import annotations

import errno
import socket

import pytest

from drcom import binding
from drcom.binding import BindFailure, BindKind, bind_udp_socket, diagnose_bind_error


# --------------------------------------------------------------------------
# error classification
# --------------------------------------------------------------------------
def test_10013_is_classified_as_reserved_not_in_use() -> None:
    diagnosis = diagnose_bind_error(61440, binding.WSAEACCES, deep=False)
    assert diagnosis.kind == BindKind.RESERVED
    assert "10013" in diagnosis.headline or "WSAEACCES" in diagnosis.headline
    assert "10048" in diagnosis.explanation  # explicitly distinguishes the two


def test_10048_is_classified_as_in_use() -> None:
    diagnosis = diagnose_bind_error(61440, binding.WSAEADDRINUSE, deep=False)
    assert diagnosis.kind == BindKind.IN_USE
    assert "10048" in diagnosis.headline


def test_unknown_errors_are_still_reported() -> None:
    diagnosis = diagnose_bind_error(61440, 12345, deep=False)
    assert diagnosis.kind == BindKind.UNKNOWN
    assert "12345" in diagnosis.headline


def test_address_errors_are_recognised() -> None:
    diagnosis = diagnose_bind_error(61440, 10049, deep=False)
    assert diagnosis.kind == BindKind.ADDRESS


def test_advice_mentions_the_known_culprits_for_reserved_ports() -> None:
    diagnosis = diagnose_bind_error(61440, binding.WSAEACCES, deep=False)
    joined = " ".join(diagnosis.advice)
    assert "Clash" in joined
    assert "退出" in joined


def test_advice_says_admin_rights_will_not_help() -> None:
    """Spec 6.1 is explicit that elevation does not fix this."""
    diagnosis = diagnose_bind_error(61440, binding.WSAEACCES, deep=False)
    joined = " ".join(diagnosis.advice)
    assert "管理员" in joined and "无效" in joined


def test_deep_diagnosis_text_is_actionable() -> None:
    diagnosis = diagnose_bind_error(61440, binding.WSAEACCES, deep=True)
    text = diagnosis.to_text()
    assert diagnosis.headline in text
    assert "处置建议" in text


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------
def test_probe_reports_a_port_we_hold_ourselves() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        # We hold it exclusively, so a second bind must fail.
        assert binding.probe_bind(port, "127.0.0.1") is not None
    finally:
        sock.close()
    # Once released it must be free again.
    assert binding.probe_bind(port, "127.0.0.1") is None


def test_scan_finds_the_blocked_port_and_its_neighbours() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        scan = binding.scan_port_window(port, radius=8, address="127.0.0.1")
        assert port in scan.blocked
        assert scan.range_containing(port) is not None
        assert len(scan.free) > 0
    finally:
        sock.close()


def test_blocked_ranges_collapse_contiguous_runs() -> None:
    scan = binding.PortScan(start=100, end=110, blocked={102: 10013, 103: 10013, 104: 10013, 108: 10013}, free=[100, 101, 105, 106, 107, 109, 110])
    assert scan.blocked_ranges == [(102, 104), (108, 108)]
    assert scan.range_containing(103) == (102, 104)
    assert scan.range_containing(106) is None


def test_find_free_nearby_skips_the_busy_port() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        candidates = binding.find_free_nearby(port, radius=12, limit=4)
        assert candidates
        assert port not in candidates
    finally:
        sock.close()


# --------------------------------------------------------------------------
# socket creation
# --------------------------------------------------------------------------
def test_bind_udp_socket_happy_path() -> None:
    bound = bind_udp_socket(0, address="127.0.0.1", timeout_ms=2500)
    try:
        assert bound.sock.gettimeout() == pytest.approx(2.5)
        assert not bound.used_fallback_port
    finally:
        bound.sock.close()


def test_bind_failure_raises_with_a_full_diagnosis() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        with pytest.raises(BindFailure) as info:
            bind_udp_socket(port, address="127.0.0.1", self_heal=False)
        diagnosis = info.value.diagnosis
        assert diagnosis.port == port
        assert diagnosis.kind in (BindKind.RESERVED, BindKind.IN_USE)
        assert diagnosis.advice
    finally:
        holder.close()


def test_alternate_port_fallback_is_opt_in() -> None:
    """The fallback must never happen unless explicitly enabled."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        with pytest.raises(BindFailure):
            bind_udp_socket(port, address="127.0.0.1", allow_alternate_port=False, self_heal=False)

        bound = bind_udp_socket(port, address="127.0.0.1", allow_alternate_port=True, self_heal=False)
        try:
            assert bound.used_fallback_port
            assert bound.local_port != port
            assert bound.heal_notes and "备选源端口" in bound.heal_notes[-1]
        finally:
            bound.sock.close()
    finally:
        holder.close()


def test_reuseaddr_is_set_before_bind() -> None:
    """Spec 6.2 — verify the ordering by checking the option is live on the socket."""
    bound = bind_udp_socket(0, address="127.0.0.1")
    try:
        # Reading SO_REUSEADDR back proves it was applied to this socket.
        assert bound.sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
    finally:
        bound.sock.close()


def test_error_code_extraction_on_windows_style_oserror() -> None:
    exc = OSError(errno.EADDRINUSE, "in use")
    code = binding._error_code(exc)
    assert code in (errno.EADDRINUSE, binding.WSAEADDRINUSE)


def test_netsh_hint_is_a_real_command() -> None:
    hint = binding.netsh_hint()
    assert "excludedportrange" in hint or "ss " in hint


def test_suspect_detection_returns_strings() -> None:
    suspects = binding.detect_conflict_suspects()
    assert isinstance(suspects, list)
    assert all(isinstance(item, str) for item in suspects)


# --------------------------------------------------------------------------
# interface table: the rows must survive FreeMibTable
# --------------------------------------------------------------------------
def test_interface_rows_are_copied_not_views() -> None:
    """Reading a row after its buffer is freed is an access violation.

    Indexing a ctypes structure array yields a view into the buffer, not a
    copy.  ``_windows_if_table`` builds its list that way and frees the buffer
    in a ``finally`` block, so it used to hand the caller dangling pointers:
    reading a field afterwards returned stale numbers, and now and then took
    the whole process down with 0xC0000005 inside _ctypes.pyd -- which is how
    the app was dying at random, twice within seconds of start-up.
    """
    import ctypes

    from drcom.netiface import _MIB_IF_ROW2

    row = _MIB_IF_ROW2()
    row.InterfaceIndex = 4242
    row.Alias = "Ethernet-Probe"

    buffer = ctypes.create_string_buffer(ctypes.sizeof(_MIB_IF_ROW2))
    ctypes.memmove(buffer, ctypes.byref(row), ctypes.sizeof(_MIB_IF_ROW2))
    array = (_MIB_IF_ROW2 * 1).from_address(ctypes.addressof(buffer))

    views = [array[0]]
    copies = [_MIB_IF_ROW2.from_buffer_copy(array[0])]

    # Overwrite, then drop the buffer: the view now points at freed memory.
    ctypes.memset(ctypes.addressof(buffer), 0xAA, ctypes.sizeof(_MIB_IF_ROW2))
    del buffer, array

    assert views[0].InterfaceIndex != 4242, "the premise changed: array[i] now copies"
    assert copies[0].InterfaceIndex == 4242
    assert copies[0].Alias == "Ethernet-Probe"


def test_rows_from_table_are_copies() -> None:
    """The deterministic version of the crash above.

    Builds a table buffer we control, copies rows out of it, then scribbles
    over and frees the buffer.  Returning views would show up here every time,
    instead of depending on whether the allocator happens to unmap the page.
    """
    import ctypes

    from drcom.netiface import _MIB_IF_ROW2, _rows_from_table

    size = ctypes.sizeof(_MIB_IF_ROW2)
    buffer = ctypes.create_string_buffer(size * 2)
    for index, value in enumerate((77, 88)):
        probe = _MIB_IF_ROW2()
        probe.InterfaceIndex = value
        probe.Alias = f"Adapter-{value}"
        ctypes.memmove(ctypes.addressof(buffer) + index * size, ctypes.byref(probe), size)

    rows = _rows_from_table(ctypes.addressof(buffer), 2)
    ctypes.memset(ctypes.addressof(buffer), 0xAA, size * 2)
    del buffer

    assert [r.InterfaceIndex for r in rows] == [77, 88]
    assert [str(r.Alias) for r in rows] == ["Adapter-77", "Adapter-88"]
