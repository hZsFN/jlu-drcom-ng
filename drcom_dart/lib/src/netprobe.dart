/// 网络质量探测：延迟、抖动、丢包 —— 对齐 Python 版 `drcom/netprobe.py`。
///
/// 用平台自带的 `ping` 二进制而不是原始 ICMP：Windows 上 raw socket 要管理员
/// 权限，而我们要的是免提权运行。输出解析同时兼容 Windows（`时间=1ms` /
/// `(0% 丢失)`）和 iputils（`time=1.2 ms` / `0% packet loss`）两种格式。
///
/// 抖动按 RFC 3550 的算法：先算**同一轮里相邻两次往返**的绝对差，再把各轮的
/// 均值平均。拿两轮的均值相减会把这个数字算小——每轮先取平均本身就是低通滤波。
library;

import 'dart:async';
import 'dart:io';

/// 一次探测的结果。
class PingResult {
  const PingResult({
    required this.target,
    required this.sent,
    required this.received,
    this.rttMs,
    required this.timestamp,
    this.rtts = const <double>[],
  });

  final String target;
  final int sent;
  final int received;
  final double? rttMs;
  final double timestamp;

  /// 这一轮里每一次往返的耗时（不只是均值）。
  final List<double> rtts;

  double get lossPercent {
    if (sent <= 0) return 0;
    return (sent - received) / sent * 100.0;
  }

  bool get ok => received > 0;
}

/// 目标是否是可安全探测的主机名/IP（挡掉以 `-` 开头、被 ping 当选项那种）。
bool isSafeTarget(String target) {
  if (target.isEmpty || target.startsWith('-')) return false;
  return _safeTarget.hasMatch(target) && target.length <= 254;
}

bool isHttpTarget(String target) =>
    target.isNotEmpty &&
    (target.toLowerCase().startsWith('http://') ||
        target.toLowerCase().startsWith('https://'));

final RegExp _safeTarget = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:\-]{0,253}$');
final RegExp _winReply = RegExp(r'(?:time|时间)[=<]\s*(\d+(?:\.\d+)?)\s*ms',
    caseSensitive: false);
final RegExp _winLoss =
    RegExp(r'\((\d+(?:\.\d+)?)%\s*(?:loss|丢失)\)', caseSensitive: false);
final RegExp _nixTime = RegExp(r'time[=<]([\d.]+)\s*ms');
final RegExp _nixLoss = RegExp(r'(\d+(?:\.\d+)?)%\s*packet loss');

bool get _isWindows => Platform.isWindows;

/// 对 [target] 发 [count] 个 ICMP 包，返回这一轮的结果。
Future<PingResult> pingOnce(
  String target, {
  int count = 3,
  int timeoutMs = 1500,
  String source = '',
}) async {
  final double started = DateTime.now().millisecondsSinceEpoch / 1000.0;
  if (!isSafeTarget(target)) {
    return PingResult(
      target: target,
      sent: count,
      received: 0,
      timestamp: started,
    );
  }

  final List<String> args = <String>['-n', '$count', '-w', '$timeoutMs'];
  if (_isWindows && source.isNotEmpty) {
    args.addAll(<String>['-S', source]);
  }
  args.add(target);

  ProcessResult result;
  try {
    result = await Process.run(
      'ping',
      args,
      stdoutEncoding: const SystemEncoding(),
      stderrEncoding: const SystemEncoding(),
    ).timeout(Duration(milliseconds: timeoutMs + count * 2000));
  } on Object {
    return PingResult(
      target: target,
      sent: count,
      received: 0,
      timestamp: started,
    );
  }

  final String output = result.stdout.toString();
  final List<double> times = <double>[];
  for (final RegExpMatch m
      in (_isWindows ? _winReply : _nixTime).allMatches(output)) {
    final double? value = double.tryParse(m.group(1) ?? '');
    if (value != null) times.add(value);
  }
  final RegExpMatch? lossMatch = (_isWindows ? _winLoss : _nixLoss).firstMatch(output);
  int received = times.length;
  if (lossMatch != null) {
    final double percent = double.tryParse(lossMatch.group(1) ?? '') ?? 0;
    final int implied = (count * (1 - percent / 100.0)).round();
    if (implied > received) received = implied;
  }
  if (received > count) received = count;

  double? rtt;
  if (times.isNotEmpty) {
    double sum = 0;
    for (final double t in times) {
      sum += t;
    }
    rtt = sum / times.length;
  }
  return PingResult(
    target: target,
    sent: count,
    received: received,
    rttMs: rtt,
    timestamp: started,
    rtts: List<double>.unmodifiable(times),
  );
}

/// 按目标分桶的滚动窗口。
class ProbeHistory {
  ProbeHistory({this.window = 40, this.sampleWindow = 180});

  /// 每个目标保留多少轮结果。
  final int window;

  /// 每个目标保留多少个单次往返样本（一轮 ICMP 贡献 count 个）。
  final int sampleWindow;

  final Map<String, List<PingResult>> results = <String, List<PingResult>>{};
  final Map<String, List<double>> _samples = <String, List<double>>{};
  final List<String> _order = <String>[];

  void add(PingResult result) {
    if (!_order.contains(result.target)) {
      _order.add(result.target);
    }
    final List<PingResult> bucket =
        results.putIfAbsent(result.target, () => <PingResult>[]);
    bucket.add(result);
    if (bucket.length > window) {
      bucket.removeRange(0, bucket.length - window);
    }
    final List<double> samples =
        _samples.putIfAbsent(result.target, () => <double>[]);
    samples.addAll(result.rtts);
    if (samples.length > sampleWindow) {
      samples.removeRange(0, samples.length - sampleWindow);
    }
  }

  List<String> get targets => List<String>.unmodifiable(_order);

  void clear() {
    results.clear();
    _samples.clear();
    _order.clear();
  }

  /// 最近 7 天的汇总（字段名与 Python 版 `summary` 一致）。
  Map<String, Object?> summary(String target) {
    final List<PingResult> bucket = results[target] ?? const <PingResult>[];
    final String kind = isHttpTarget(target) ? 'http' : 'icmp';
    if (bucket.isEmpty) {
      return <String, Object?>{
        'target': target,
        'kind': kind,
        'samples': 0,
        'pings': 0,
        'rtt_ms': null,
        'loss_percent': null,
        'jitter_ms': null,
      };
    }

    int sent = 0;
    int received = 0;
    final List<double> rtts = <double>[];
    for (final PingResult r in bucket) {
      sent += r.sent;
      received += r.received;
      final double? rtt = r.rttMs;
      if (rtt != null) rtts.add(rtt);
    }
    final double loss = sent > 0 ? (sent - received) / sent * 100.0 : 0;

    // 抖动在轮内算，再跨轮平均。
    final List<double> roundJitters = <double>[];
    for (final PingResult r in bucket) {
      if (r.rtts.length >= 2) {
        roundJitters.add(_meanAbsDelta(r.rtts));
      }
    }
    double? jitter;
    if (roundJitters.isNotEmpty) {
      double sum = 0;
      for (final double j in roundJitters) {
        sum += j;
      }
      jitter = sum / roundJitters.length;
    }

    double? meanRtt;
    if (rtts.isNotEmpty) {
      double sum = 0;
      for (final double r in rtts) {
        sum += r;
      }
      meanRtt = sum / rtts.length;
    }

    return <String, Object?>{
      'target': target,
      'kind': kind,
      'samples': _samples[target]?.length ?? 0,
      'pings': bucket.length,
      'rtt_ms': meanRtt,
      'loss_percent': loss,
      'jitter_ms': jitter,
    };
  }
}

double _meanAbsDelta(List<double> values) {
  if (values.length < 2) return 0;
  double sum = 0;
  for (int i = 1; i < values.length; i++) {
    sum += (values[i] - values[i - 1]).abs();
  }
  return sum / (values.length - 1);
}
