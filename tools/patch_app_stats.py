from pathlib import Path

p = Path(__file__).resolve().parent.parent / "drcom_dart/lib/src/app.dart"
src = p.read_text(encoding="utf-8")
orig = src

# 1) import + 常量
src = src.replace(
    "import 'secrets.dart';",
    "import 'secrets.dart';\nimport 'stats.dart';",
    1,
)
src = src.replace(
    "const String statusFileName = 'status.json';",
    "const String statusFileName = 'status.json';\n\n/// 在线统计文件名（与状态文件同目录）。\nconst String statsFileName = 'stats.json';",
    1,
)

# 2) 构造参数 + 字段
src = src.replace(
    """    String? statusPath,
    this.logSink,
    this.transportFactory,
  })  : _config = config,
        statusPath = statusPath ??
            store.path.replaceFirst(configFileName, statusFileName);""",
    """    String? statusPath,
    StatsStore? stats,
    this.logSink,
    this.transportFactory,
  })  : _config = config,
        statusPath = statusPath ??
            store.path.replaceFirst(configFileName, statusFileName) {
    this.stats = stats ??
        StatsStore(
          File(this.statusPath.replaceFirst(statusFileName, statsFileName)),
        );
  }""",
    1,
)
src = src.replace(
    """  /// 状态文件绝对路径。
  final String statusPath;""",
    """  /// 状态文件绝对路径。
  final String statusPath;

  /// 在线时长 / 断线 / 流量的账簿。
  late final StatsStore stats;""",
    1,
)

# 3) ticker 里同步会话 + 检查点
src = src.replace(
    """    _statusTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      _writeStatus();
    });""",
    """    _statusTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      _syncStats();
      stats.checkpoint();
      _writeStatus();
    });""",
    1,
)

# 4) 退出前结账
src = src.replace(
    """    } finally {
      _statusTimer?.cancel();""",
    """    } finally {
      _syncStats();
      _statusTimer?.cancel();""",
    1,
)

# 5) _syncStats 方法 + _wasOnline 字段
src = src.replace(
    """  void _onEvent(EngineEvent event) {""",
    """  /// 上一次 tick 时是否在线，用来识别「上线 / 掉线」这个边沿。
  bool _wasOnline = false;

  /// 在线状态出现边沿时开关会话，折进 stats.json。
  void _syncStats() {
    final engine = _engine;
    final online = engine?.isOnline ?? false;
    if (online == _wasOnline) {
      return;
    }
    _wasOnline = online;
    if (online) {
      stats.openSession(
        ip: engine?.ip ?? '',
        account: _config.activeAccount()?.maskedAccount ?? '',
      );
    } else {
      stats.closeSession(drop: true);
    }
  }

  void _onEvent(EngineEvent event) {""",
    1,
)

# 6) 状态里带统计
src = src.replace(
    """      'timestamp': DateTime.now().millisecondsSinceEpoch / 1000,""",
    """      'stats': stats.asStatus(),
      'timestamp': DateTime.now().millisecondsSinceEpoch / 1000,""",
    1,
)

if src == orig:
    raise SystemExit("no change applied")
p.write_text(src, encoding="utf-8")
print("patched ok, %d -> %d bytes" % (len(orig), len(src)))
