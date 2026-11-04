from pathlib import Path

p = Path(__file__).resolve().parent.parent / "desktop/lib/controller.dart"
src = p.read_text(encoding="utf-8")
orig = src

# 1) import dart:io
src = src.replace(
    "import 'dart:async';",
    "import 'dart:async';\nimport 'dart:io';",
    1,
)

# 2) 构造函数 + 字段
src = src.replace(
    """  DrcomController({ConfigStore? store}) : store = store ?? ConfigStore() {
    config = this.store.load();
  }

  final ConfigStore store;
  late AppConfig config;""",
    """  DrcomController({ConfigStore? store, StatsStore? stats})
      : store = store ?? ConfigStore() {
    config = this.store.load();
    this.stats = stats ??
        StatsStore(
          File(this.store.path.replaceFirst(configFileName, statsFileName)),
        );
  }

  final ConfigStore store;
  late AppConfig config;

  /// 在线时长 / 断线 / 流量的账簿（与 [DrcomApp] 共用同一份，别各开一个）。
  late final StatsStore stats;

  /// 网卡流量采样器（PowerShell 读计数器，2 秒一次）。
  final TrafficMeter traffic = TrafficMeter();

  /// 延迟 / 抖动 / 丢包的滚动窗口。
  final ProbeHistory probe = ProbeHistory();

  /// 探测目标：认证服务器本身，ICMP 一般可达。
  String get probeTarget => config.auth.server;

  /// tick 计数，用来给采样定时（1 秒一跳）。
  int _tick = 0;

  /// 最近一次流量读数。
  TrafficSnapshot get trafficSnapshot => traffic.snapshot;

  /// 最近一次探测汇总（没有数据时各字段为 null）。
  Map<String, Object?> get probeSummary => probe.summary(probeTarget);

  /// 统计快照，字段与 `StatsStore.asStatus` 一致。
  Map<String, dynamic> get statsStatus => stats.asStatus();""",
    1,
)

# 3) idle 状态补上 stats
src = src.replace(
    """        'mac': activeAccount?.mac ?? '',
      };""",
    """        'mac': activeAccount?.mac ?? '',
        'stats': stats.asStatus(),
      };""",
    1,
)

# 4) 把同一份 stats 交给 DrcomApp
src = src.replace(
    "final app = DrcomApp(config: config, store: store, logSink: _onLog);",
    "final app =\n        DrcomApp(config: config, store: store, stats: stats, logSink: _onLog);",
    1,
)

# 5) ticker 里带采样
src = src.replace(
    """  void _startTicker() {
    _ticker?.cancel();
    // 引擎内部状态（uptime / 在线时长）不都通过事件暴露，每秒拉一次。
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) => _notify());
  }""",
    """  void _startTicker() {
    _ticker?.cancel();
    _tick = 0;
    // 引擎内部状态（uptime / 在线时长）不都通过事件暴露，每秒拉一次。
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      _tick++;
      // 流量每 2 秒采一次（每次要起一个 PowerShell，别太勤）。
      if (_tick % 2 == 0) {
        unawaited(_sampleTraffic());
      }
      // 探测每 10 秒一次，够画读数又不至于刷满日志。
      if (_tick % 10 == 1) {
        unawaited(_probeNow());
      }
      _notify();
    });
  }

  /// 把流量统计钉在真正在认证的网卡上，再采一次。
  Future<void> _sampleTraffic() async {
    final engine = _app?.engine;
    final address = engine?.pickedBindAddress ?? '';
    if (address.isNotEmpty) {
      await traffic.setSourceAddress(address);
    }
    await traffic.sample();
  }

  /// 探一次延迟（失败不抛，历史窗口自己记丢包）。
  Future<void> _probeNow() async {
    final target = probeTarget;
    if (target.isEmpty) return;
    try {
      final result = await pingOnce(target, count: 3, timeoutMs: 1000);
      probe.add(result);
    } on Object {
      // 探测失败不该影响认证。
    }
  }""",
    1,
)

if src == orig:
    raise SystemExit("no change applied")
p.write_text(src, encoding="utf-8")
print("patched controller.dart %d -> %d bytes" % (len(orig), len(src)))
