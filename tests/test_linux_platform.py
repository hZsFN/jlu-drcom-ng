"""Unit tests for Linux platform branches (sysfs, ss/proc, flock, systemd, notify, watchdog, selfcheck)."""

from __future__ import annotations

import errno
import signal
from types import SimpleNamespace

import drcom.autostart as autostart
import drcom.binding as binding
import drcom.netiface as netiface
import drcom.notify as notify
import drcom.selfcheck as selfcheck
import drcom.single_instance as single_instance
import drcom.watchdog as watchdog


def test_netiface_linux_sysfs_and_address_parsing(tmp_path, monkeypatch) -> None:
    eth = tmp_path / "eth0"
    (eth / "statistics").mkdir(parents=True)
    (eth / "ifindex").write_text("2\n", encoding="utf-8")
    (eth / "mtu").write_text("1500\n", encoding="utf-8")
    (eth / "speed").write_text("1000\n", encoding="utf-8")
    (eth / "type").write_text("1\n", encoding="utf-8")
    (eth / "operstate").write_text("up\n", encoding="utf-8")
    (eth / "address").write_text("aa:bb:cc:dd:ee:ff\n", encoding="utf-8")
    (eth / "statistics" / "rx_bytes").write_text("12345\n", encoding="utf-8")
    (eth / "statistics" / "tx_bytes").write_text("6789\n", encoding="utf-8")
    (eth / "statistics" / "rx_packets").write_text("10\n", encoding="utf-8")
    (eth / "statistics" / "tx_packets").write_text("20\n", encoding="utf-8")
    (eth / "statistics" / "rx_errors").write_text("0\n", encoding="utf-8")
    (eth / "statistics" / "tx_errors").write_text("1\n", encoding="utf-8")

    info = netiface._linux_if_from_sysfs(eth)
    assert info.name == "eth0"
    assert info.index == 2
    assert info.mac == "AA:BB:CC:DD:EE:FF"
    assert info.is_up is True
    assert info.speed_bps == 1_000_000_000
    assert info.if_type == netiface._IF_TYPE_ETHERNET_CSMACD
    assert info.bytes_in == 12345 and info.bytes_out == 6789

    # Fallback `ip -4 -o addr show` when getifaddrs is unavailable
    monkeypatch.setattr(netiface.ctypes, "CDLL", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("no libc")))
    monkeypatch.setattr(
        netiface.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0,
            stdout="2: eth0    inet 172.18.10.22/24 brd 172.18.10.255 scope global eth0\n",
        ),
    )
    addrs = netiface._posix_interface_addresses()
    assert addrs == {2: ["172.18.10.22"]}


def test_binding_linux_ss_reserved_ports_and_errno(monkeypatch) -> None:
    monkeypatch.setattr(binding, "IS_WINDOWS", False)
    monkeypatch.setattr(
        binding,
        "_run_hidden",
        lambda cmd, timeout=6.0: (
            'UNCONN 0 0 0.0.0.0:61440 0.0.0.0:* users:(("drcomd",pid=4321,fd=5))\n'
            if cmd[0] == "ss"
            else ""
        ),
    )
    holders = binding.find_port_holders(61440)
    assert holders == ["PID 4321 — drcomd"]

    ranges = binding._parse_linux_reserved_ports("61000-61500, 62000\n")
    assert ranges == [(61000, 61500), (62000, 62000)]

    diag = binding.diagnose_bind_error(61440, errno.EADDRINUSE, deep=False)
    assert diag.kind == binding.BindKind.IN_USE
    assert any("ss -ulnp" in tip for tip in diag.advice)


def test_single_instance_posix_lock_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(single_instance, "IS_WINDOWS", False)
    inst = single_instance.SingleInstance()
    assert inst._posix_lock_path() == tmp_path / "jlu-drcom-ng.lock"
    assert inst.acquire()
    inst.release()


def test_autostart_linux_systemd_unit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(autostart, "IS_WINDOWS", False)
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        autostart,
        "_run",
        lambda cmd: ran.append(list(cmd)) or SimpleNamespace(returncode=0, stdout="enabled\n", stderr=""),
    )

    unit_file = tmp_path / "jlu-drcom-ng.service"
    ok, msg = autostart.install_systemd_service(
        command="/usr/local/bin/jlu-drcom-ng",
        arguments="--autostart --minimized",
        working_dir="/usr/local/bin",
        unit_path=unit_file,
    )
    assert ok and "systemd" in msg
    content = unit_file.read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/bin/jlu-drcom-ng --autostart --minimized" in content
    assert autostart.systemd_service_exists(unit_path=unit_file)
    assert autostart.systemd_service_command(unit_path=unit_file) == "/usr/local/bin/jlu-drcom-ng --autostart --minimized"

    ok_rm, _ = autostart.remove_systemd_service(unit_path=unit_file)
    assert ok_rm
    assert not unit_file.exists()


def test_notify_linux_notify_send(controller, monkeypatch) -> None:
    monkeypatch.setattr(notify, "IS_WINDOWS", False)
    monkeypatch.setattr(notify.shutil, "which", lambda _n: "/usr/bin/notify-send")
    sent: list[list[str]] = []
    monkeypatch.setattr(
        notify.subprocess,
        "run",
        lambda cmd, **_kw: sent.append(list(cmd)) or SimpleNamespace(returncode=0),
    )
    n = notify.Notifier(controller.config.notify, controller.log)
    n._toast(notify.NotificationEvent(kind="online", title="已上线", body="10.0.0.1"))
    assert sent == [["/usr/bin/notify-send", "-a", "JLU DrCOM NG", "已上线", "10.0.0.1"]]


def test_watchdog_posix_proc_parser_and_terminate(monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "IS_WINDOWS", False)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(watchdog.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert watchdog._terminate(9876) is True
    assert killed == [(9876, signal.SIGTERM)]


def test_selfcheck_linux_routes_firewall_and_autostart(controller, monkeypatch) -> None:
    monkeypatch.setattr(selfcheck, "IS_WINDOWS", False)
    monkeypatch.setattr(selfcheck.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("ip", "ufw") else None)

    def fake_run(cmd, timeout=8.0):
        if cmd[0] == "ip":
            return "default via 10.67.0.1 dev wlan0 proto dhcp src 10.67.12.34 metric 600\n"
        if cmd[0] == "ufw":
            return "Status: active\n"
        return ""

    monkeypatch.setattr(selfcheck, "_run", fake_run)
    routes = selfcheck._default_routes()
    assert routes == [("wlan0", "10.67.0.1", "600")]
    assert selfcheck._firewall_state() == {"ufw": "ON"}

    report = selfcheck.Report()
    monkeypatch.setattr("drcom.autostart.systemd_service_exists", lambda: True)
    monkeypatch.setattr("drcom.autostart.systemd_service_command", lambda: "/usr/bin/jlu-drcom-ng")
    monkeypatch.setattr("drcom.single_instance.is_autostart_enabled", lambda: False)
    selfcheck._check_autostart(report, controller)
    assert report.of("autostart").status == selfcheck.OK


def test_linux_distro_package_generators() -> None:
    import sys
    from pathlib import Path

    tools_dir = str(Path(__file__).resolve().parent.parent / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import make_linux_packages as mlp

    assert mlp.normalize_arch("x86_64") == ("x86_64", "amd64", "x86_64")
    assert mlp.normalize_arch("aarch64") == ("aarch64", "arm64", "aarch64")

    ctrl = mlp.render_deb_control(version="2.2.0", deb_arch="amd64", installed_kb=51200)
    assert "Package: jlu-drcom-ng" in ctrl
    assert "Architecture: amd64" in ctrl
    assert "Version: 2.2.0" in ctrl

    pkginfo = mlp.render_arch_pkginfo(version="2.2.0", arch="x86_64", installed_bytes=52428800)
    assert "pkgname = jlu-drcom-ng" in pkginfo
    assert "pkgver = 2.2.0-1" in pkginfo
    assert "arch = x86_64" in pkginfo

    spec = (Path(__file__).resolve().parent.parent / "installer" / "linux" / "jlu-drcom-ng.spec.in").read_text(encoding="utf-8")
    assert "AutoReqProv: no" in spec
    assert "/usr/bin/drcomd" in spec

