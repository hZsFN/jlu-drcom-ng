/// 在线时长与断线统计 —— 对齐 Python 版 `drcom/stats.py`。
///
/// 落盘到 `stats.json`，在线期间每 [StatsStore.checkpointInterval] 秒折存一次，
/// 这样异常退出（崩溃、断电、休眠）最多丢一个检查点，而不是整段会话。
/// 分桶按**本地日期**，所以「今日累计」与用户钟表一致。
library;

import 'dart:convert';
import 'dart:io';

/// 一天的累计。
class DayStats {
  DayStats({
    this.onlineSeconds = 0,
    this.drops = 0,
    this.sessions = 0,
    this.loginFailures = 0,
    this.bytesRx = 0,
    this.bytesTx = 0,
    this.longestSession = 0,
    this.firstOnlineAt,
  });

  double onlineSeconds;
  int drops;
  int sessions;
  int loginFailures;
  int bytesRx;
  int bytesTx;
  double longestSession;
  double? firstOnlineAt;

  factory DayStats.fromJson(Map<String, dynamic> json) => DayStats(
        onlineSeconds: _num(json['online_seconds']),
        drops: _int(json['drops']),
        sessions: _int(json['sessions']),
        loginFailures: _int(json['login_failures']),
        bytesRx: _int(json['bytes_rx']),
        bytesTx: _int(json['bytes_tx']),
        longestSession: _num(json['longest_session']),
        firstOnlineAt: json['first_online_at'] == null
            ? null
            : _num(json['first_online_at']),
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'online_seconds': _round1(onlineSeconds),
        'drops': drops,
        'sessions': sessions,
        'login_failures': loginFailures,
        'bytes_rx': bytesRx,
        'bytes_tx': bytesTx,
        'longest_session': _round1(longestSession),
        'first_online_at': firstOnlineAt,
      };
}

/// 全期累计。
class Totals {
  Totals({
    this.onlineSeconds = 0,
    this.drops = 0,
    this.sessions = 0,
    this.loginFailures = 0,
    this.bytesRx = 0,
    this.bytesTx = 0,
    this.longestSession = 0,
    this.lastOnlineAt,
  });

  double onlineSeconds;
  int drops;
  int sessions;
  int loginFailures;
  int bytesRx;
  int bytesTx;
  double longestSession;
  double? lastOnlineAt;

  factory Totals.fromJson(Map<String, dynamic> json) => Totals(
        onlineSeconds: _num(json['online_seconds']),
        drops: _int(json['drops']),
        sessions: _int(json['sessions']),
        loginFailures: _int(json['login_failures']),
        bytesRx: _int(json['bytes_rx']),
        bytesTx: _int(json['bytes_tx']),
        longestSession: _num(json['longest_session']),
        lastOnlineAt:
            json['last_online_at'] == null ? null : _num(json['last_online_at']),
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'online_seconds': _round1(onlineSeconds),
        'drops': drops,
        'sessions': sessions,
        'login_failures': loginFailures,
        'bytes_rx': bytesRx,
        'bytes_tx': bytesTx,
        'longest_session': _round1(longestSession),
        'last_online_at': lastOnlineAt,
      };
}

/// 会话生命周期 + 每日累计的账簿。
class StatsStore {
  StatsStore(this.path, {this.keepDays = 120}) {
    load();
  }

  /// 检查点间隔（秒）。
  static const double checkpointInterval = 60.0;

  final File path;
  final int keepDays;

  final Map<String, DayStats> daily = <String, DayStats>{};
  Totals totals = Totals();

  DateTime? _sessionStart;
  DateTime? _lastCheckpoint;
  String _ip = '';
  String _account = '';

  // -- 持久化 -----------------------------------------------------------

  void load() {
    if (!path.existsSync()) return;
    Map<String, dynamic> raw;
    try {
      final Object? decoded = jsonDecode(path.readAsStringSync());
      if (decoded is! Map<String, dynamic>) return;
      raw = decoded;
    } on Object {
      return;
    }
    daily.clear();
    final Object? days = raw['daily'];
    if (days is Map<String, dynamic>) {
      for (final MapEntry<String, dynamic> entry in days.entries) {
        final Object? value = entry.value;
        if (value is Map<String, dynamic>) {
          daily[entry.key] = DayStats.fromJson(value);
        }
      }
    }
    final Object? summary = raw['totals'];
    totals = summary is Map<String, dynamic> ? Totals.fromJson(summary) : Totals();
  }

  void save() {
    final Map<String, dynamic> payload = <String, dynamic>{
      'version': 1,
      'daily': <String, dynamic>{
        for (final MapEntry<String, DayStats> entry in daily.entries)
          entry.key: entry.value.toJson(),
      },
      'totals': totals.toJson(),
    };
    final Directory dir = path.parent;
    if (!dir.existsSync()) {
      dir.createSync(recursive: true);
    }
    final File tmp = File('${path.path}.tmp');
    try {
      tmp.writeAsStringSync(
        const JsonEncoder.withIndent('  ').convert(payload),
        flush: true,
      );
      tmp.renameSync(path.path);
    } on FileSystemException {
      // 写盘失败不影响在线；下次检查点会再试。
    }
  }

  // -- 会话生命周期 ------------------------------------------------------

  void openSession({required String ip, required String account}) {
    final DateTime now = DateTime.now();
    _sessionStart = now;
    _lastCheckpoint = now;
    _ip = ip;
    _account = account;
    final DayStats day = daily.putIfAbsent(_today(), DayStats.new);
    day.sessions += 1;
    day.firstOnlineAt ??= now.millisecondsSinceEpoch / 1000.0;
    totals.sessions += 1;
    totals.lastOnlineAt = now.millisecondsSinceEpoch / 1000.0;
    save();
  }

  /// 结束当前会话，返回时长（秒）。
  double closeSession({bool drop = true}) {
    final DateTime? start = _sessionStart;
    if (start == null) return 0;
    final double duration =
        DateTime.now().difference(start).inMilliseconds / 1000.0;
    final DayStats day = daily.putIfAbsent(_today(), DayStats.new);
    day.onlineSeconds += duration;
    if (duration > day.longestSession) day.longestSession = duration;
    totals.onlineSeconds += duration;
    if (duration > totals.longestSession) totals.longestSession = duration;
    if (drop) {
      day.drops += 1;
      totals.drops += 1;
    }
    _sessionStart = null;
    save();
    return duration;
  }

  void noteLoginFailure() {
    daily.putIfAbsent(_today(), DayStats.new).loginFailures += 1;
    totals.loginFailures += 1;
    save();
  }

  /// 把已过去的在线时间折进今天的桶里，但不结束会话。
  void checkpoint({bool force = false}) {
    final DateTime? start = _sessionStart;
    final DateTime? last = _lastCheckpoint;
    if (start == null || last == null) return;
    final DateTime now = DateTime.now();
    if (!force &&
        now.difference(last).inMilliseconds / 1000.0 < checkpointInterval) {
      return;
    }
    final double elapsed = now.difference(last).inMilliseconds / 1000.0;
    _lastCheckpoint = now;
    final double session = now.difference(start).inMilliseconds / 1000.0;
    final DayStats day = daily.putIfAbsent(_today(), DayStats.new);
    day.onlineSeconds += elapsed;
    if (session > day.longestSession) day.longestSession = session;
    totals.onlineSeconds += elapsed;
    if (session > totals.longestSession) totals.longestSession = session;
    save();
  }

  void addTraffic({required int rx, required int tx}) {
    final DayStats day = daily.putIfAbsent(_today(), DayStats.new);
    day.bytesRx += rx;
    day.bytesTx += tx;
    totals.bytesRx += rx;
    totals.bytesTx += tx;
  }

  // -- 查询 -------------------------------------------------------------

  String get currentIp => _ip;
  String get currentAccount => _account;

  /// 当前会话已在线秒数（无会话时为 0）。
  double get sessionSeconds {
    final DateTime? start = _sessionStart;
    if (start == null) return 0;
    return DateTime.now().difference(start).inMilliseconds / 1000.0;
  }

  bool get hasSession => _sessionStart != null;

  DayStats today() => daily.putIfAbsent(_today(), DayStats.new);

  /// 最近 [count] 天（含今天），按日期升序。
  List<(String, DayStats)> lastDays([int count = 7]) {
    final DateTime now = DateTime.now();
    final List<(String, DayStats)> out = <(String, DayStats)>[];
    for (int offset = count - 1; offset >= 0; offset--) {
      final DateTime day = DateTime(now.year, now.month, now.day)
          .subtract(Duration(days: offset));
      final String key = _key(day);
      out.add((key, daily[key] ?? DayStats()));
    }
    return out;
  }

  double weekSeconds() {
    double sum = 0;
    for (final (_, DayStats day) in lastDays(7)) {
      sum += day.onlineSeconds;
    }
    return sum;
  }

  /// 丢掉超过 [keepDays] 的分桶。
  void prune() {
    if (keepDays <= 0) return;
    final DateTime now = DateTime.now();
    final String cutoff =
        _key(DateTime(now.year, now.month, now.day).subtract(Duration(days: keepDays)));
    final List<String> removed = daily.keys.where((String k) => k.compareTo(cutoff) < 0).toList();
    for (final String key in removed) {
      daily.remove(key);
    }
    if (removed.isNotEmpty) save();
  }

  /// 给界面用的一份快照（字段名与 Python 版 `as_status` 一致）。
  Map<String, dynamic> asStatus() {
    final DayStats day = today();
    final double live = hasSession ? sessionSeconds : 0;
    return <String, dynamic>{
      'today_seconds': _round1(day.onlineSeconds + live),
      'today_drops': day.drops,
      'today_sessions': day.sessions,
      'today_login_failures': day.loginFailures,
      'week_seconds': _round1(weekSeconds()),
      'total_seconds': _round1(totals.onlineSeconds + live),
      'total_drops': totals.drops,
      'total_sessions': totals.sessions,
      'longest_session': _round1(totals.longestSession),
      'bytes_rx': totals.bytesRx,
      'bytes_tx': totals.bytesTx,
    };
  }
}

String _today() => _key(DateTime.now());

String _key(DateTime day) {
  String two(int v) => v.toString().padLeft(2, '0');
  return '${day.year.toString().padLeft(4, '0')}-${two(day.month)}-${two(day.day)}';
}

double _num(Object? value) {
  if (value is num) return value.toDouble();
  if (value is String) return double.tryParse(value) ?? 0;
  return 0;
}

int _int(Object? value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

double _round1(double value) => (value * 10).roundToDouble() / 10;
