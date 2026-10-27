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

  /// 把内存里的配置写回磁盘。改过账号/密码之后调它。
  ///
  /// 写失败不抛给 UI 崩掉，落到 [error] 里显示。
  bool persist() {
    try {
      store.save(config);
      return true;
    } on Object catch (e) {
      _error = '保存配置失败：${describeError(e)}';
      _notify();
      return false;
    }
  }

  /// 切换当前账号。引擎在跑时不让切，免得状态错乱。
  void selectAccount(String id) {
    if (running) {
      _error = '先断开再切换账号';
      _notify();
      return;
    }
    try {
      config.setActiveAccount(id);
    } on ConfigError catch (e) {
      _error = e.message;
      _notify();
      return;
    }
    _error = '';
    persist();
    _appendLog('已切换到 ${config.activeAccount()?.maskedAccount ?? id}');
  }

  /// 新增账号（同时设为当前）。返回新账号，失败返回 null。
  Account? createAccount({
    required String account,
    String mac = Account.defaultMacPlaceholder,
    String label = '',
  }) {
    final trimmed = account.trim();
    if (trimmed.isEmpty) {
      _error = '学号/账号不能为空';
      _notify();
      return null;
    }
    final created = config.addAccount(
      account: trimmed,
      mac: mac.trim().isEmpty ? Account.defaultMacPlaceholder : mac.trim(),
      label: label.trim(),
    );
    _error = '';
    persist();
    _appendLog('已添加账号 ${created.maskedAccount}');
    return created;
  }

  /// 修改账号的显示信息（学号 / MAC / 标签）。
  void updateAccount(
    String id, {
    required String account,
    required String mac,
    required String label,
  }) {
    final target = _findAccount(id);
    if (target == null) {
      _error = '找不到这个账号';
      _notify();
      return;
    }
    final trimmed = account.trim();
    if (trimmed.isEmpty) {
      _error = '学号/账号不能为空';
      _notify();
      return;
    }
    target.account = trimmed;
    target.mac = mac.trim().isEmpty ? Account.defaultMacPlaceholder : mac.trim();
    target.label = label.trim();
    _error = '';
    persist();
    _appendLog('已更新账号 ${target.maskedAccount}');
  }

  /// 给账号存密码（DPAPI 加密后写配置文件，明文不落盘）。
  bool setPassword(String id, String plaintext) {
    final target = _findAccount(id);
    if (target == null) {
      _error = '找不到这个账号';
      _notify();
      return false;
    }
    if (plaintext.isEmpty) {
      _error = '密码不能为空';
      _notify();
      return false;
    }
    try {
      target.passwordProtected = protectPassword(plaintext);
    } on Object catch (e) {
      _error = '加密密码失败：${describeError(e)}';
      _notify();
      return false;
    }
    _error = '';
    final saved = persist();
    if (saved) {
      _appendLog('已保存 ${target.maskedAccount} 的密码');
    }
    return saved;
  }

  /// 删账号（连同它的密码）。删当前账号时会自动落到下一个。
  void deleteAccount(String id) {
    if (running) {
      _error = '先断开再删账号';
      _notify();
      return;
    }
    final target = _findAccount(id);
    if (target == null) return;
    final label = target.maskedAccount;
    config.removeAccount(id);
    _error = '';
    persist();
    _appendLog('已删除账号 $label');
  }

  Account? _findAccount(String id) {
    for (final item in config.accounts) {
      if (item.id == id) return item;
    }
    return null;
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

  /// 清空界面上的日志（状态文件里的历史不受影响）。
  void clearLogs() {
    _logs.clear();
    _notify();
  }

  /// 把日志拼成纯文本，给「复制」按钮用。
  String exportLogs() {
    final head = <String>[
      '# JLU DrCOM-NG 日志',
      '# 账号 ${activeAccount?.maskedAccount ?? '-'}'
          ' · 服务器 ${config.auth.server}',
      '# 状态文件 $statusPath',
      '',
    ];
    final body = _logs.map((line) => line.format()).join('\n');
    return <String>[...head, body.isEmpty ? '（没有日志）' : body].join('\n');
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
