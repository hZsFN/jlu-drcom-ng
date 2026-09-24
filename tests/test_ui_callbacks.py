"""Exercise every UI callback, not just build the views.

Written after a live bug: clicking "自动识别 MAC" raised

    pick_relevant_interface() takes 0 positional arguments but 2 were given

The existing smoke tests built each view and walked the control tree, but never
*invoked* a handler — so a broken callback was invisible to the suite.  This
module closes that class: it finds every actionable control and calls it.

Real side effects are stubbed (opening Explorer, writing the autostart registry
key, scanning ports), because a test must not touch the user's machine.
"""

from __future__ import annotations

import pytest

from tests.ui_fakes import FakeEvent, FakePage, iter_actionable, walk

flet = pytest.importorskip("flet")


@pytest.fixture
def guarded(controller, monkeypatch):
    """Stub everything that would touch the real machine."""
    import drcom.single_instance as single_instance
    import drcom.ui.app as ui_app

    opened: list[str] = []
    autostart: list[bool] = []
    scans: list[str] = []

    monkeypatch.setattr(single_instance, "open_in_file_manager", lambda path: opened.append(str(path)))

    # A headless test has no mounted page, so `control.update()` raises by
    # design.  Record the calls instead of letting them fail: the point here is
    # whether the handler *logic* runs, and tests/test_ui_smoke.py already
    # covers real construction.
    refresh_requests: list = []
    monkeypatch.setattr(flet.Control, "update", lambda self, *a, **k: refresh_requests.append(self))

    monkeypatch.setattr(
        type(controller), "set_autostart",
        lambda self, enabled: (autostart.append(enabled), (True, "stubbed"))[1],
    )
    monkeypatch.setattr(
        type(controller), "diagnostics",
        lambda self: (
            scans.append("diag") or {
                "bind_port": 61440, "bind_free": True, "bind_error_code": None,
                "blocked_range": None, "blocked_ports_nearby": [], "excluded_ranges": [],
                "excluded_covers_port": False, "suspects": [], "free_alternatives": [],
                "local_ip": "127.0.0.1", "interfaces": [], "data_dir": str(self.data_dir),
                "log_file": "", "version": "test",
            }
        ),
    )
    # The real HTTP server binds a port; keep it out of the test.
    monkeypatch.setattr(
        type(controller.api), "start", lambda self: (True, "stubbed")
    )
    monkeypatch.setattr(type(controller.api), "stop", lambda self: None)
    monkeypatch.setattr(ui_app, "tray_available", lambda: False)

    controller.opened_files = opened
    controller.autostart_calls = autostart
    return controller


@pytest.mark.parametrize("view", ["状态", "日志", "账号", "设置", "关于"])
def test_every_callback_in_every_view_runs(view: str, guarded, controller) -> None:
    """Call every on_click / on_change in a view; none may raise.

    This is the regression guard for the reported bug: the MAC-detection button
    was never invoked by a test, so its broken call survived.
    """
    from drcom.ui.app import DrcomApp

    app = DrcomApp(controller, enable_tray=False)
    app.page = FakePage()
    app.view_host = None
    app.active_view = view
    root = app._build_view(view)

    failures: list[str] = []
    invoked = 0
    for control, attr in iter_actionable(root):
        handler = getattr(control, attr)
        invoked += 1
        value = getattr(control, "value", None)
        try:
            handler(FakeEvent(control=control, value=value))
        except Exception as exc:  # noqa: BLE001 - collected and reported
            label = getattr(control, "label", None) or getattr(control, "text", None) or type(control).__name__
            failures.append(f"{attr} on {label!r}: {type(exc).__name__}: {exc}")

    assert invoked > 0, f"no callbacks found in view {view!r} - the walker is broken"
    assert not failures, "\n".join(failures)


def test_mac_detection_button_works(guarded, controller) -> None:
    """The exact reported failure, pinned as its own test.

    Note what this asserts: the button fills the MAC *text field*.  It does not
    write to the config — that happens when Save is pressed — so asserting on
    the account would pass vacuously.
    """
    from drcom.netiface import list_interfaces
    from drcom.protocol import mac_to_bytes
    from drcom.ui.app import DrcomApp

    app = DrcomApp(controller, enable_tray=False)
    app.page = FakePage()
    app.view_host = None
    root = app._build_view("账号")

    mac_field = None
    for control in walk(root):
        # _field() returns a bare TextField, which carries no callback, so it is
        # invisible to iter_actionable().
        if isinstance(control, flet.TextField) and getattr(control, "label", "") == "网卡 MAC":
            mac_field = control
            break

    detect_button = None
    for control, attr in iter_actionable(root):
        if getattr(getattr(control, "content", None), "value", None) == "自动识别 MAC":
            detect_button = (control, attr)
            break

    assert mac_field is not None, "the MAC field was not found in the account view"
    assert detect_button is not None, "the detect-MAC button was not found"

    control, attr = detect_button
    getattr(control, attr)(FakeEvent(control=control))  # must not raise

    detected = (mac_field.value or "").strip()
    if not detected:
        pytest.skip("no adapter with a hardware address in this environment")

    mac_to_bytes(detected)  # raises if malformed
    available = {i.mac.upper() for i in list_interfaces() if i.mac}
    assert detected.upper() in available, (
        f"the button filled {detected!r}, which is not a real adapter address"
    )


# --------------------------------------------------------------------------
# the second defect behind the same button
# --------------------------------------------------------------------------
def test_tunnel_adapter_cannot_outrank_a_real_nic() -> None:
    """With a VPN in TUN mode the default route runs through the tunnel.

    Awarding the route-match bonus to it made "detect MAC" pick an adapter with
    no hardware address and report "no adapter found" on a machine that plainly
    has a NIC.  Reproduces the real ranking: Mihomo 1040 vs Ethernet 120.
    """
    from drcom.netiface import InterfaceInfo, _score_interface

    tunnel = InterfaceInfo(name="Mihomo", index=56, if_type=0, is_up=True,
                           bytes_in=47_000_000, mac="")
    ethernet = InterfaceInfo(name="以太网", index=6, if_type=6, is_up=True,
                             mac="AA:BB:CC:DD:EE:FF", bytes_in=2_000_000_000)

    # Pretend the tunnel owns the route to the auth server.
    addrs = {56: ["28.0.0.1"], 6: ["172.18.123.63"]}
    route = "28.0.0.1"

    tunnel_score = _score_interface(tunnel, route, addrs)
    ethernet_score = _score_interface(ethernet, route, addrs)
    assert ethernet_score > tunnel_score, (
        f"tunnel scored {tunnel_score}, physical NIC scored {ethernet_score}"
    )


def test_route_match_still_wins_for_a_real_adapter() -> None:
    """The fix must not weaken the normal case: route match on a real NIC."""
    from drcom.netiface import InterfaceInfo, _score_interface

    on_route = InterfaceInfo(name="以太网", index=6, if_type=6, is_up=True,
                             mac="AA:BB:CC:DD:EE:FF")
    elsewhere = InterfaceInfo(name="WLAN", index=7, if_type=71, is_up=True,
                              mac="11:22:33:44:55:66")
    addrs = {6: ["172.18.123.63"], 7: ["10.66.201.107"]}

    assert _score_interface(on_route, "172.18.123.63", addrs) > _score_interface(
        elsewhere, "172.18.123.63", addrs
    )


def test_detected_interface_has_a_mac_when_one_exists() -> None:
    """The adapter chosen for MAC detection must actually carry a MAC."""
    from drcom.netiface import list_interfaces, pick_relevant_interface

    if not any(i.mac for i in list_interfaces()):
        pytest.skip("no adapter with a hardware address in this environment")

    picked = pick_relevant_interface(server="10.100.61.3", port=61440)
    assert picked is not None
    assert picked.mac, f"picked {picked.label!r}, which has no MAC"
