from pathlib import Path

p = Path(__file__).resolve().parent.parent / "desktop/lib/main.dart"
src = p.read_text(encoding="utf-8")
orig = src

# 1) import 包（formatBytes 来自 jlu_drcom）
src = src.replace(
    "import 'package:flutter/material.dart';",
    "import 'package:flutter/material.dart';\nimport 'package:jlu_drcom/jlu_drcom.dart';",
    1,
)

# 2) _telemetry 填上真实数据
src = src.replace(
    """  /// 从 controller 的状态载荷拼出 HUD 遥测。
  ///
  /// 延迟/丢包/速率/今日在线这些还没采集，先留空让 HUD 显示 `--`，
  /// 采集做好了直接往这里填。
  HudTelemetry _telemetry() {
    final Map<String, Object?> status = _controller.status;
    final String state = status['state']?.toString() ?? 'idle';
    final num uptime = (status['uptime_seconds'] as num?) ?? 0;
    return HudTelemetry(
      online: status['online'] == true,
      stateLabel: stateLabels[state] ?? state,
      stateColor: _hexOf(stateColorKey(state)),
      ip: status['ip']?.toString() ?? '',
      account: status['account']?.toString() ?? '',
      uptime: uptime.toDouble(),
      drops: ((status['consecutive_failures'] as num?) ?? 0).toInt(),
      detail: status['last_error']?.toString() ?? '',
    );
  }""",
    """  /// 从 controller 的状态载荷拼出 HUD 遥测。
  HudTelemetry _telemetry() {
    final Map<String, Object?> status = _controller.status;
    final String state = status['state']?.toString() ?? 'idle';
    final num uptime = (status['uptime_seconds'] as num?) ?? 0;
    final TrafficSnapshot traffic = _controller.trafficSnapshot;
    final Map<String, Object?> probe = _controller.probeSummary;
    final double? rtt = (probe['rtt_ms'] as num?)?.toDouble();
    final double? loss = (probe['loss_percent'] as num?)?.toDouble();
    final double? jitter = (probe['jitter_ms'] as num?)?.toDouble();
    final bool online = status['online'] == true;
    final double interval = _controller.config.auth.keepaliveInterval;
    final double phase = (online && interval > 0)
        ? (uptime.toDouble() % interval) / interval
        : 0;

    return HudTelemetry(
      online: online,
      stateLabel: stateLabels[state] ?? state,
      stateColor: _hexOf(stateColorKey(state)),
      ip: status['ip']?.toString() ?? '',
      account: status['account']?.toString() ?? '',
      uptime: uptime.toDouble(),
      rxRate: traffic.rxRate,
      txRate: traffic.txRate,
      sessionRx: traffic.totalRx,
      sessionTx: traffic.totalTx,
      rttMs: rtt,
      lossPercent: loss,
      jitterMs: jitter,
      keepalivePhase: phase,
      drops: ((status['consecutive_failures'] as num?) ?? 0).toInt(),
      detail: status['last_error']?.toString() ?? '',
    );
  }

  /// 底部统计栏里「延迟 / 丢包 / 本次流量」三格的文本。
  String get _rttLabel {
    final Object? rtt = _controller.probeSummary['rtt_ms'];
    if (rtt is! num) return '—';
    return '${rtt.round()} ms';
  }

  String get _lossLabel {
    final Object? loss = _controller.probeSummary['loss_percent'];
    if (loss is! num) return '—';
    return '${loss.round()}%';
  }

  String get _trafficLabel {
    final TrafficSnapshot traffic = _controller.trafficSnapshot;
    return '↓${formatBytes(traffic.totalRx.toDouble())} '
        '↑${formatBytes(traffic.totalTx.toDouble())}';
  }""",
    1,
)

# 3) 六个统计格换成真实值
src = src.replace(
    """          todayLabel: '—',
          weekLabel: '—',
          dropsLabel: '${telemetry.drops}',
          rttLabel: '—',
          lossLabel: '—',
          trafficLabel: '—',""",
    """          todayLabel: formatDuration(_controller.stats.today().onlineSeconds +
              _controller.stats.sessionSeconds),
          weekLabel: formatDuration(_controller.stats.weekSeconds()),
          dropsLabel: '${_controller.stats.today().drops}',
          rttLabel: _rttLabel,
          lossLabel: _lossLabel,
          trafficLabel: _trafficLabel,""",
    1,
)

if src == orig:
    raise SystemExit("no change applied")
p.write_text(src, encoding="utf-8")
print("patched main.dart %d -> %d bytes" % (len(orig), len(src)))
