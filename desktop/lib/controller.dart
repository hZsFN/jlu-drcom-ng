import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:jlu_drcom/jlu_drcom.dart';

/// UI 与引擎之间的桥。
///
/// [DrcomApp] 的 `start()` 是个一直跑到断线的长 Future，UI 不能 await 它，
/// 所以这里把它包成 fire-and-forget，并用每秒一次的 tick 把在线时长刷出来。
class DrcomController extends ChangeNotifier {
  DrcomController({ConfigStore? store}) : store = store ?? ConfigStore() {
    config = this.store.load();
  }

  final ConfigStore store;
  late AppConfig config;

  DrcomApp? _app;
  Timer? _ticker;
  final List<AppLogLine> _logs = <AppLogLine>[];
  String _error = '';
  bool _disposed = false;

  bool get running => _app != null;
  String get error => _error;
  List<AppLogLine> get logs => List<AppLogLine>.unmodifiable(_logs);
  List<Account> get accounts => config.accounts;
  Account? get activeAccount => config.activeAccount();

  /// 当前状态。引擎没起来时给一份自己的兜底载荷，字段与
  /// [DrcomApp.statusPayload] 保持一致，UI 不用分两套渲染。
  Map<String, Object?> get status => _app?.statusPayload() ?? _idleStatus();

  /// 状态文件路径（界面上显示给人看）。
  String get statusPath =>
      _app?.statusPath ??
      store.path.replaceFirst(configFileName, statusFileName);

  Map<String, Object?> _idleStatus() => <String, Object?>{
        'state': 'stopped',
        'online': false,
        'ip': '',
        'account': activeAccount?.maskedAccount ?? '',
        'server': config.auth.server,
        'uptime_seconds': 0.0,
        'consecutive_failures': 0,
        'last_error': _error,
        'last_reason': '',
        'ip_local': '',
        'mac': activeAccount?.mac ?? '',
      };

  /// 重新读配置。用户在别处改了 config.json 之后刷新用。
  void reload() {
    config = store.load();
    _notify();
  }

  Future<void> connect() async {
    if (_app != null) return;

    final account = activeAccount;
    if (account == null) {
      _error = '配置里还没有账号';
      _appendLog('配置里还没有账号。先用参考实现的客户端登录一次，'
          '或手工编辑 ${store.path} 加一个账号。');
      return;
    }
    if (!account.hasPassword) {
      _error = '账号 ${account.maskedAccount} 没有保存密码';
      _appendLog(_error);
      return;
    }

    _error = '';
    final app = DrcomApp(config: config, store: store, logSink: _onLog);
    _app = app;
    _startTicker();
    _appendLog('正在连接 ${account.maskedAccount} → ${config.auth.server}');
    _notify();

    unawaited(
      app.start().catchError((Object e) {
        _error = describeError(e);
        _appendLog('启动失败：$_error');
      }).whenComplete(() {
        if (identical(_app, app)) {
          _app = null;
          _stopTicker();
        }
        _notify();
      }),
    );
  }

  /// 断开。引擎会在下一轮保活前退出。
  Future<void> disconnect() async {
    final app = _app;
    if (app == null) return;
    _app = null;
    app.stop();
    _stopTicker();
    _appendLog('已断开');
    _notify();
  }

  /// 退出前调用：停引擎、停定时器。
  Future<void> shutdown() async {
    _app?.stop();
    _app = null;
    _stopTicker();
    _disposed = true;
  }

  void _onLog(AppLogLine line) {
    _appendLog(line.text, time: line.time);
  }

  void _appendLog(String text, {DateTime? time}) {
    final line = AppLogLine(time ?? DateTime.now(), text);
    _logs.add(line);
    if (_logs.length > DrcomApp.maxLogLines) {
      _logs.removeRange(0, _logs.length - DrcomApp.maxLogLines);
    }
    _notify();
  }

  void _startTicker() {
    _ticker?.cancel();
    // 引擎内部状态（uptime / 在线时长）不都通过事件暴露，每秒拉一次。
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) => _notify());
  }

  void _stopTicker() {
    _ticker?.cancel();
    _ticker = null;
  }

  void _notify() {
    if (_disposed) return;
    notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _stopTicker();
    super.dispose();
  }
}

/// 把引擎/配置抛出的异常翻成人话。
String describeError(Object error) {
  if (error is SecretError) {
    return '密码解不开：${error.message}。'
        '配置可能是另一台电脑上生成的，请重新输入密码。';
  }
  if (error is StateError) return error.message;
  final text = error.toString();
  return text.startsWith('Exception: ') ? text.substring(11) : text;
}
