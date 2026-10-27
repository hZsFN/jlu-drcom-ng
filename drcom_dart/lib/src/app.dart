/// 应用层：把配置、引擎、状态文件、日志串起来。
///
/// 引擎自己带重连退避（`ReconnectPolicy`），所以这里不再重复实现重连——
/// 只负责装配、把事件翻译成日志和状态文件、以及收尾。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'config.dart';
import 'engine.dart';
import 'secrets.dart';
import 'stats.dart';

/// Dart 客户端版本号（与 Python 版的版本序列分开）。
const String dartClientVersion = '2.0.0-dev';

/// 状态文件名（与参考实现一致，UI 和 API 可以直接读）。
const String statusFileName = 'status.json';

/// 在线统计文件名（与状态文件同目录）。
const String statsFileName = 'stats.json';

/// 把配置应用到引擎上需要的全部参数。
///
/// 单独抽出来是为了能单测"配置→引擎参数"这一层，不必真开 socket。
class EngineSettings {
  const EngineSettings({
    required this.account,
    required this.mac,
    required this.server,
    required this.port,
    required this.recvTimeout,
    required this.keepaliveInterval,
    required this.challengeRetries,
    required this.postLoginDelay,
    required this.bindAddress,
    required this.bindPort,
    required this.autoInterfaceFallback,
    required this.policy,
  });

  final String account;
  final String mac;
  final String server;
  final int port;
  final Duration recvTimeout;
  final Duration keepaliveInterval;
  final int challengeRetries;
  final Duration postLoginDelay;
  final String? bindAddress;
  final int bindPort;
  final bool autoInterfaceFallback;
  final ReconnectPolicy policy;

  /// 从配置推出来。
  factory EngineSettings.fromConfig(AppConfig config, Account account) {
    final auth = config.auth;
    final reconnect = config.reconnect;
    final bind = auth.bindAddress.trim();
    return EngineSettings(
      account: account.account,
      mac: account.mac,
      server: auth.server,
      port: auth.port,
      recvTimeout: Duration(milliseconds: auth.timeoutMs),
      keepaliveInterval:
          Duration(milliseconds: (auth.keepaliveInterval * 1000).round()),
      challengeRetries: auth.challengeRetries,
      postLoginDelay:
          Duration(milliseconds: (auth.postLoginDelay * 1000).round()),
      bindAddress: bind.isEmpty || bind == '0.0.0.0' ? null : bind,
      bindPort: auth.bindPort,
      autoInterfaceFallback: auth.autoInterfaceFallback,
      policy: ReconnectPolicy(
        enabled: reconnect.enabled,
        minDelay: Duration(milliseconds: (reconnect.minDelay * 1000).round()),
        maxDelay: Duration(milliseconds: (reconnect.maxDelay * 1000).round()),
        factor: reconnect.factor,
        jitter: reconnect.jitter,
        giveUpAfter: reconnect.giveUpAfter,
      ),
    );
  }
}

/// 一条日志行。
class AppLogLine {
  AppLogLine(this.time, this.text);

  final DateTime time;
  final String text;

  /// `2026-10-27 09:07:13 xxx`
  String format() {
    String two(int value) => value.toString().padLeft(2, '0');
    return '${time.year}-${two(time.month)}-${two(time.day)} '
        '${two(time.hour)}:${two(time.minute)}:${two(time.second)} $text';
  }
}

/// 应用本体。`start()` 之后一直跑，直到引擎自己进入 `fatal` 或者 [stop]。
class DrcomApp {
  DrcomApp({
    required AppConfig config,
    required this.store,
    String? statusPath,
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
  }

  final AppConfig _config;

  /// 传输工厂（测试注入假实现；生产留 null 走真 UDP）。
  final TransportFactory? transportFactory;

  /// 配置句柄（存盘用）。
  final ConfigStore store;

  /// 状态文件绝对路径。
  final String statusPath;

  /// 在线时长 / 断线 / 流量的账簿。
  late final StatsStore stats;

  /// 日志出口；null = 打到 stdout。
  final void Function(AppLogLine line)? logSink;

  AuthEngine? _engine;
  StreamSubscription<EngineEvent>? _subscription;
  Timer? _statusTimer;
  final List<AppLogLine> _logs = <AppLogLine>[];
  bool _stopping = false;
  String _lastError = '';

  AuthEngine? get engine => _engine;

  /// 最近的日志（最多保留 [maxLogLines] 条）。
  List<AppLogLine> get logs => List<AppLogLine>.unmodifiable(_logs);

  static const int maxLogLines = 500;

  /// 当前配置。
  AppConfig get config => _config;

  /// 启动。缺账号或缺密码时抛 [SecretError]/[StateError]。
  /// 临时诊断：把「写状态文件」和「起子进程」两条链路的真实结果落到临时目录，
  /// 用来判断 Flutter 宿主里到底是哪一环不通。定位完就删。

  Future<void> start() async {
    final account = _config.activeAccount();
    if (account == null) {
      throw StateError('配置里没有账号，先在设置里加一个');
    }
    if (!account.hasPassword) {
      throw StateError('账号 ${account.maskedAccount} 没有保存密码');
    }
    final password = account.revealPassword();
    if (password.isEmpty) {
      throw StateError('账号 ${account.maskedAccount} 的密码解出来是空的');
    }

    final settings = EngineSettings.fromConfig(_config, account);
    final engine = AuthEngine(
      account: settings.account,
      password: password,
      mac: settings.mac,
      server: settings.server,
      port: settings.port,
      recvTimeout: settings.recvTimeout,
      keepaliveInterval: settings.keepaliveInterval,
      challengeRetries: settings.challengeRetries,
      postLoginDelay: settings.postLoginDelay,
      bindAddress: settings.bindAddress,
      bindPort: settings.bindPort,
      autoInterfaceFallback: settings.autoInterfaceFallback,
      policy: settings.policy,
      transportFactory: transportFactory,
    );
    _engine = engine;

    _subscription = engine.events.listen(_onEvent);
    // 状态文件每秒刷一次：引擎内部状态（uptime / 在线时长）不都通过事件暴露。
    _statusTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      _syncStats();
      stats.checkpoint();
      _writeStatus();
    });
    _writeStatus();

    _log('启动：账号 ${engine.maskedAccount} → '
        '${settings.server}:${settings.port}'
        '${settings.bindAddress == null ? '' : '，绑定 ${settings.bindAddress}'}');

    try {
      await engine.run();
    } finally {
      _syncStats();
      _statusTimer?.cancel();
      _statusTimer = null;
      await _subscription?.cancel();
      _subscription = null;
      _writeStatus();
      _log('已退出');
    }
  }

  /// 停止（幂等）。
  void stop() {
    if (_stopping) {
      return;
    }
    _stopping = true;
    _engine?.stop();
  }

  /// 上一次 tick 时是否在线，用来识别「上线 / 掉线」这个边沿。
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

  void _onEvent(EngineEvent event) {
    final text = event.detail.isEmpty
        ? event.message
        : '${event.message}（${event.detail}）';
    _log('${event.kind.name}: $text');
    if (event.kind == EngineEventKind.error ||
        event.kind == EngineEventKind.fatal ||
        event.kind == EngineEventKind.loginFailed ||
        event.kind == EngineEventKind.challengeFailed) {
      _lastError = text;
    }
    _writeStatus();
  }

  void _log(String text) {
    final line = AppLogLine(DateTime.now(), text);
    _logs.add(line);
    if (_logs.length > maxLogLines) {
      _logs.removeRange(0, _logs.length - maxLogLines);
    }
    final sink = logSink;
    if (sink != null) {
      sink(line);
    } else {
      stdout.writeln(line.format());
    }
  }

  /// 组装状态文件内容（字段名与参考实现 `build_status_payload` 对齐）。
  Map<String, Object?> statusPayload() {
    final engine = _engine;
    final account = _config.activeAccount();
    final onlineSince = engine?.onlineSince;
    return <String, Object?>{
      'app': 'JLU-DrCOM-NG',
      'version': dartClientVersion,
      'implementation': 'dart',
      'state': (engine?.state ?? EngineState.idle).name,
      'online': engine?.isOnline ?? false,
      'ip': engine?.ip ?? '',
      'account': account?.maskedAccount ?? '',
      'server': _config.auth.server,
      'uptime_seconds': engine == null
          ? 0.0
          : (engine.uptime.inMilliseconds / 100).round() / 10,
      'online_since':
          onlineSince == null ? null : onlineSince.millisecondsSinceEpoch / 1000,
      'last_error': _lastError == '' ? '' : _lastError,
      'last_reason': engine?.reason?.name ?? '',
      'consecutive_failures': engine?.consecutiveFailures ?? 0,
      'ip_local': engine?.pickedBindAddress ?? '',
      'mac': account?.mac ?? '',
      'stats': stats.asStatus(),
      'timestamp': DateTime.now().millisecondsSinceEpoch / 1000,
    };
  }

  /// 状态文件写不进去时只提醒一次，别每秒刷屏（权限不对会一直失败）。
  bool _statusWriteWarned = false;

  void _writeStatus() {
    if (_stopping && _engine == null) {
      return;
    }
    try {
      final file = File(statusPath);
      final directory = file.parent;
      if (!directory.existsSync()) {
        directory.createSync(recursive: true);
      }
      final temporary = File('${file.path}.tmp');
      temporary.writeAsStringSync(
        jsonEncode(statusPayload()),
        flush: true,
      );
      temporary.renameSync(file.path);
      _statusWriteWarned = false;
    } on FileSystemException catch (error) {
      // 状态文件写不进去不该搞挂认证，也不能刷屏。
      if (!_statusWriteWarned) {
        _statusWriteWarned = true;
        _log('状态文件写入失败：${error.message}'
            ' path=$statusPath'
            ' errno=${error.osError?.errorCode}'
            '（后续不再重复提示）');
      }
    }
  }
}
