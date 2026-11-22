"""Tests for Phase 2 memory-leak fixes and allocation reductions."""

from __future__ import annotations

import logging
import time

from drcom.logbus import LogBus, LogRecordView
from drcom.ui.app import DrcomApp
from drcom.ui.hud import HudPainter, HudTelemetry
from drcom.ui.theme import hud_of
from drcom.ui.tray import TrayIcon, _make_icon_image


def test_drain_logs_does_not_leak_off_view_and_caps_on_view(controller) -> None:
    app = DrcomApp(controller)
    app.active_view = "状态"  # Off the 日志 tab

    for i in range(550):
        app._on_log_record_dummy = LogRecordView(
            seq=i + 1, timestamp=time.time(), level="INFO", message=f"line {i}"
        )
        app._enqueue_bounded(app.log_queue, app._on_log_record_dummy)

    added = app._drain_logs()
    assert added == 0
    assert len(app._log_rows) == 0
    assert app.log_queue.empty()

    # Switching to 日志 tab populates from LogBus snapshot and caps at 400
    for i in range(450):
        controller.log.info("msg %d", i)
    app.active_view = "日志"
    panel = app._view_logs()
    assert panel is not None
    assert len(app._log_rows) == 400
    assert len(app.log_list.controls) == 400


def test_enqueue_bounded_never_blocks_and_evicts_oldest() -> None:
    import queue

    q: queue.Queue[int] = queue.Queue(maxsize=4)
    for i in range(10):
        DrcomApp._enqueue_bounded(q, i)
    assert q.qsize() == 4
    items = [q.get_nowait() for _ in range(4)]
    assert items == [6, 7, 8, 9]


def test_logbus_file_logging_false_and_close_releases_handles(tmp_path) -> None:
    mem_log = LogBus(log_dir=tmp_path / "no_logs", file_logging=False)
    mem_log.info("hello memory only")
    assert mem_log.file_path is None
    assert not (tmp_path / "no_logs").exists()
    assert len(mem_log.snapshot()) >= 1
    mem_log.close()

    disk_log = LogBus(log_dir=tmp_path / "disk_logs", file_logging=True)
    disk_log.info("hello file")
    path = disk_log.file_path
    assert path is not None and path.exists()
    disk_log.close()
    assert not any(isinstance(h, logging.FileHandler) for h in disk_log.logger.handlers)
    # Deleting the file succeeds on Windows only if FileHandler was closed cleanly
    path.unlink()


def test_tray_icon_caches_bitmaps_and_skips_redundant_updates() -> None:
    img1 = _make_icon_image("#86FF86", size=64)
    img2 = _make_icon_image("#86FF86", size=64)
    assert img1 is img2

    icon = TrayIcon()

    class DummyIcon:
        def __init__(self) -> None:
            self.title_sets = 0
            self.icon_sets = 0
            self._title = ""
            self._icon = None

        @property
        def title(self):
            return self._title

        @title.setter
        def title(self, v):
            self.title_sets += 1
            self._title = v

        @property
        def icon(self):
            return self._icon

        @icon.setter
        def icon(self, v):
            self.icon_sets += 1
            self._icon = v

    dummy = DummyIcon()
    icon._icon = dummy
    icon.update(tooltip="same", color="#86FF86")
    icon.update(tooltip="same", color="#86FF86")
    assert dummy.title_sets == 1
    assert dummy.icon_sets == 1


def test_hud_painter_include_decor_false_skips_static_decor_shapes() -> None:
    painter = HudPainter(hud_of(), width=820, height=360)
    tel = HudTelemetry(online=True, state_label="在线", ip="10.0.0.1")
    full = painter.render(tel, include_decor=True)
    dynamic_only = painter.render(tel, include_decor=False)
    assert len(dynamic_only) < len(full)
    assert len(dynamic_only) > 20
