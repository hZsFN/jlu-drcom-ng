import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:jlu_drcom/jlu_drcom.dart';

/// UI 与引擎之间的桥。
///
/// [DrcomApp] 的 `start()` 是个一直跑到断线的长 Future，UI 不能 await 它，
/// 所以这里把它包成 fire-and-forget，并用每秒一次的 tick 把在线时长刷出来。
class DrcomController extends ChangeNotifier {
  DrcomController({ConfigStore? store, StatsStore? stats})
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
  Map<String, dynamic> get statsStatus => stats.asStatus();

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
        'stats': stats.asStatus(),
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
    final app =
        DrcomApp(config: config, store: store, stats: stats, logSink: _onLog);
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
    _tick = 0;
    // 引擎内部状态（uptime / 在线时长）不都通过事件暴露，每秒拉一次。
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      _tick++;
      // 流量每 5 秒采一次。采样走 FFI 直读 GetIfTable2，不再是子进程，
      // 成本可以忽略，这个间隔纯为让读数不跳。
      if (_tick % 5 == 0) {
        unawaited(_sampleTraffic());
      }
      // 探测每 10 秒一次，够画读数又不至于刷满日志。
      if (_tick % 10 == 1) {
        unawaited(_probeNow());
      }
      _notify();
    });
  }

  /// 上一次采样时的会话累计，用来算增量。
  int _lastTotalRx = 0;
  int _lastTotalTx = 0;

  /// 把流量统计钉在真正在认证的网卡上，采一次并把增量记进账簿。
  Future<void> _sampleTraffic() async {
    final engine = _app?.engine;
    final address = engine?.pickedBindAddress ?? '';
    if (address.isNotEmpty) {
      await traffic.setSourceAddress(address);
    }
    if (address.isEmpty) {
      return;
    }
    final TrafficSnapshot snapshot = await traffic.sample();
    final int deltaRx = snapshot.totalRx - _lastTotalRx;
    final int deltaTx = snapshot.totalTx - _lastTotalTx;
    if (deltaRx <= 0 && deltaTx <= 0) {
      return;
    }
    _lastTotalRx = snapshot.totalRx;
    _lastTotalTx = snapshot.totalTx;
    stats.addTraffic(
      rx: deltaRx > 0 ? deltaRx : 0,
      tx: deltaTx > 0 ? deltaTx : 0,
    );
  }

  /// 探一次延迟（失败不抛，历史窗口自己记丢包）。
  Future<void> _probeNow() async {
    // ICMP 探测已停用：这台机器上未签名进程发不出 ICMP（安全软件拦截），
    // 而 ping.exe 子进程在受限令牌下又起不来，结果是每次都失败、HUD 上恒显示
    // 「丢包 100%」，纯属误导。延迟/丢包要等引擎层用协议往返测，见 TODO。
    // 探针保留在 netprobe.dart 里，签名之后可以打开。
    if (1 > 0) return;
    final target = probeTarget;
    if (target.isEmpty) return;
    // 必须指定源地址：不指定会走默认路由（可能是 WiFi），校园网网关直接不回包。
    final source = _app?.engine?.pickedBindAddress ?? '';
    try {
      final result =
          await pingOnce(target, count: 3, timeoutMs: 1000, source: source);
      probe.add(result);
    } on Object {
      // 探测失败不该影响认证。
    }
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
