"""开机自启只有一份：计划任务优先，注册成功就必须清掉旧的 Run 值。

Run 键是退路，不是并列的第二套机制。两边都留着，登录时会被拉起两份，第二份
被单实例互斥体挡掉，表现为「开机偶发弹窗」或「端口被占」——而这两条都不会在
任何日志里出现，所以必须在这里挡住。
"""

from __future__ import annotations


def _patch(monkeypatch, *, task_ok: bool, run_present: bool) -> dict:
    from drcom import autostart, controller as controller_module, single_instance

    calls: dict = {"installs": 0, "run_cleared": 0, "run_written": 0}

    def fake_install_current(*, minimized: bool = True, with_watchdog: bool = False):
        calls["installs"] += 1
        return (True, "已注册计划任务") if task_ok else (False, "Access is denied.")

    def fake_is_run() -> bool:
        return run_present

    def fake_disable_run():
        calls["run_cleared"] += 1
        return True, "已取消开机自启"

    def fake_enable_run(*, minimized: bool = True, with_watchdog: bool = False):
        calls["run_written"] += 1
        return True, "已写入开机自启"

    monkeypatch.setattr(autostart, "install_current", fake_install_current)
    monkeypatch.setattr(controller_module, "is_autostart_enabled", fake_is_run)
    monkeypatch.setattr(controller_module, "disable_autostart", fake_disable_run)
    monkeypatch.setattr(controller_module, "enable_autostart", fake_enable_run)
    return calls


def test_a_successful_task_retires_the_run_value(controller, monkeypatch) -> None:
    calls = _patch(monkeypatch, task_ok=True, run_present=True)
    ok, message = controller.set_autostart(True)
    assert ok
    assert calls["installs"] == 1
    assert calls["run_cleared"] == 1, "两边都留着就会开机拉起两份"
    assert "避免开机拉起两份" in message


def test_no_run_value_means_nothing_to_clean(controller, monkeypatch) -> None:
    calls = _patch(monkeypatch, task_ok=True, run_present=False)
    ok, _ = controller.set_autostart(True)
    assert ok
    assert calls["run_cleared"] == 0


def test_a_failed_task_still_falls_back_to_the_run_key(controller, monkeypatch) -> None:
    calls = _patch(monkeypatch, task_ok=False, run_present=False)
    ok, message = controller.set_autostart(True)
    assert ok, "任务注册不了时 Run 键是唯一能用的退路"
    assert calls["run_written"] == 1
    assert "计划任务注册失败" in message


def test_the_registration_overwrites_rather_than_appends(monkeypatch) -> None:
    """``schtasks /F`` 覆盖同名任务——这正是「不会重复注册」的依据。"""
    from drcom import autostart
    from pathlib import Path

    captured: dict = {}

    def fake_run(args):
        captured["args"] = args
        captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(autostart, "_run", fake_run)
    ok, _ = autostart.install_task(r"C:\app.exe", "--watchdog", user_id="PC\\user")
    assert ok
    assert captured["args"][:4] == ["schtasks", "/Create", "/TN", autostart.TASK_NAME]
    assert "/F" in captured["args"], "没有 /F 就会在任务名冲突时失败，而不是替换"
