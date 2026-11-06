"""常见错误检测：每条结论都必须能被证伪，跳过必须说明为什么跳过。

这一层最容易犯的错是「不知道就当没问题」。所以测试盯三件事：
* 读不到的项一律 ``skip`` 且带理由，绝不升级成 ``ok``；
* 多个默认路由要报出来（这是「获取挑战码一直转圈」最常见的真因）；
* 单项探测抛异常不能把整份报告带走。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def quiet_probes(monkeypatch):
    """Every probe answered with a harmless, local-only value."""
    from drcom import autostart, binding, netiface, remote_engine, selfcheck, single_instance

    monkeypatch.setattr(selfcheck, "is_elevated", lambda: False)
    monkeypatch.setattr(selfcheck, "_run", lambda *a, **k: "")
    monkeypatch.setattr(binding, "probe_bind", lambda *a, **k: None)
    monkeypatch.setattr(binding, "read_excluded_port_ranges", lambda: [])
    monkeypatch.setattr(netiface, "get_default_route_ip", lambda *a, **k: "172.18.123.63")
    monkeypatch.setattr(netiface, "candidate_bind_addresses", lambda *a, **k: ["172.18.123.63"])
    monkeypatch.setattr(remote_engine, "daemon_is_up", lambda *a, **k: True)
    monkeypatch.setattr(remote_engine, "daemon_path", lambda: None)
    monkeypatch.setattr(autostart, "task_exists", lambda: False)
    monkeypatch.setattr(autostart, "task_command", lambda: "")
    monkeypatch.setattr(single_instance, "is_autostart_enabled", lambda: False)


def test_an_unreadable_check_is_skipped_not_passed(controller, quiet_probes) -> None:
    from drcom.selfcheck import SKIP, run_self_check

    report = run_self_check(controller)
    firewall = report.of("firewall")
    assert firewall is not None
    assert firewall.status == SKIP
    assert "管理员" in firewall.detail


def test_standard_user_is_reported_as_such(controller, quiet_probes) -> None:
    from drcom.selfcheck import run_self_check

    report = run_self_check(controller)
    assert report.context["运行权限"] == "标准用户"
    elevation = report.of("elevation")
    assert "标准用户" in elevation.detail
    assert "跳过" in elevation.advice


def test_two_default_routes_are_called_out(controller, quiet_probes, monkeypatch) -> None:
    """热点或 VPN 抢走默认路由，包根本到不了认证服务器。"""
    from drcom import selfcheck

    routes = "12\t192.168.43.1\t55\n7\t49.140.193.254\t25\n"
    monkeypatch.setattr(selfcheck, "_run", lambda cmd, **k: routes if "Get-NetRoute" in " ".join(cmd) else "")
    report = selfcheck.run_self_check(controller)
    route = report.of("route")
    assert route.status == "warn"
    assert "2 条默认路由" in route.detail
    assert "有线" in route.advice


def test_one_default_route_is_fine(controller, quiet_probes, monkeypatch) -> None:
    from drcom import selfcheck

    routes = "7\t49.140.193.254\t25\n"
    monkeypatch.setattr(selfcheck, "_run", lambda cmd, **k: routes if "Get-NetRoute" in " ".join(cmd) else "")
    report = selfcheck.run_self_check(controller)
    assert report.of("route").status == "ok"


def test_a_reserved_port_is_a_failure_with_the_fix(controller, quiet_probes, monkeypatch) -> None:
    from drcom import binding

    monkeypatch.setattr(binding, "read_excluded_port_ranges", lambda: [(61000, 62000)])
    report = __import__("drcom.selfcheck", fromlist=["run_self_check"]).run_self_check(controller)
    reserved = report.of("reserved")
    assert reserved.status == "fail"
    assert "61000-62000" in reserved.detail
    assert "dynamicport" in reserved.advice


def test_a_blocked_port_reports_alternatives(controller, quiet_probes, monkeypatch) -> None:
    from drcom import binding

    monkeypatch.setattr(binding, "probe_bind", lambda *a, **k: 10048)
    monkeypatch.setattr(binding, "find_free_nearby", lambda *a, **k: [61441, 61442])
    monkeypatch.setattr(binding, "diagnose_bind_error", lambda port, code: type(
        "D", (), {"to_text": lambda self: f"端口 {port} 已被占用", "headline": "端口被占用"}
    )())
    report = __import__("drcom.selfcheck", fromlist=["run_self_check"]).run_self_check(controller)
    bind = report.of("bind")
    assert bind.status == "fail"
    assert "10048" in bind.detail
    assert "61441" in bind.advice


def test_a_broken_probe_does_not_take_the_report_down(controller, quiet_probes, monkeypatch) -> None:
    from drcom import binding

    def explode(*a, **k):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(binding, "probe_bind", explode)
    report = __import__("drcom.selfcheck", fromlist=["run_self_check"]).run_self_check(controller)
    assert report.of("bind").status == "skip"
    assert "probe blew up" in report.of("bind").detail
    assert report.of("account") is not None, "后面的检查必须照常跑完"


def test_failures_sort_to_the_top(controller, quiet_probes, monkeypatch) -> None:
    from drcom import binding

    monkeypatch.setattr(binding, "probe_bind", lambda *a, **k: 10013)
    report = __import__("drcom.selfcheck", fromlist=["run_self_check"]).run_self_check(controller)
    statuses = [check.status for check in report.checks]
    assert statuses[0] in ("fail", "warn")
    assert statuses == sorted(statuses, key=lambda s: {"fail": 0, "warn": 1, "skip": 2, "ok": 3}[s])


def test_the_headline_counts_what_matters(controller, quiet_probes, monkeypatch) -> None:
    from drcom import binding

    monkeypatch.setattr(binding, "probe_bind", lambda *a, **k: 10013)
    report = __import__("drcom.selfcheck", fromlist=["run_self_check"]).run_self_check(controller)
    assert "异常" in report.headline()


def test_the_guide_is_readable_from_a_source_checkout() -> None:
    from drcom.helpdocs import find_guide, read_guide

    path = find_guide()
    assert path is not None, "源码树里 docs/troubleshooting.md 必须存在"
    text = read_guide()
    assert "常见故障排障指南" in text
    assert "10013" in text, "端口错误的两个成因必须写进去"
    assert "0x0B" in text, "错误码表必须写进去"
