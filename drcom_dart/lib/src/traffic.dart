/// 网卡流量统计 —— 对齐 Python 版 `drcom/traffic.py`。
///
/// Python 版走 ctypes 直读 `GetIfTable2`。Dart 这边不引 FFI（MIB_IF_ROW2 结构
/// 太大、对齐易错），改用 `Get-NetAdapterStatistics` 采样：一次拿全部网卡的
/// 收发字节数，再挑出认证所用网卡那一条。缺点是每次采样要起一个 PowerShell
/// 进程（约 150–250 ms），所以**采样频率由调用方控制**（HUD 里 2 秒一次）。
library;

import 'dart:async';
import 'dart:io';

import 'ffi_net.dart';

/// 人读的字节数（二进制单位，B 不带小数，其余一位）。
String formatBytes(double value) {
  if (value < 0) {
    value = 0;
  }
  const List<String> units = <String>['B', 'KB', 'MB', 'GB', 'TB'];
  for (int i = 0; i < units.length; i++) {
    if (value < 1024 || i == units.length - 1) {
      return i == 0
          ? '${value.toStringAsFixed(0)} ${units[i]}'
          : '${value.toStringAsFixed(1)} ${units[i]}';
    }
    value /= 1024;
  }
  return '${value.toStringAsFixed(1)} TB';
}

/// 速率文案，如 `1.2 MB/s`。
String formatRate(double value) => '${formatBytes(value)}/s';

/// 一次流量读数。
class TrafficSnapshot {
  const TrafficSnapshot({
    this.interfaceName = '',
    this.rxBytes = 0,
    this.txBytes = 0,
    this.rxRate = 0,
    this.txRate = 0,
    this.totalRx = 0,
    this.totalTx = 0,
    this.timestamp = 0,
  });

  final String interfaceName;
  final int rxBytes;
  final int txBytes;
  final double rxRate;
  final double txRate;
  final int totalRx;
  final int totalTx;
  final double timestamp;

  /// `↓1.2 MB/s  ↑340 KB/s`
  String get rateLabel => '↓${formatBytes(rxRate)}/s  ↑${formatBytes(txRate)}/s';

  /// `↓24.7 KB  ↑15.5 KB`
  String get totalLabel => '↓${formatBytes(totalRx.toDouble())}  ↑${formatBytes(totalTx.toDouble())}';

  TrafficSnapshot copyWith({double? rxRate, double? txRate}) => TrafficSnapshot(
        interfaceName: interfaceName,
        rxBytes: rxBytes,
        txBytes: txBytes,
        rxRate: rxRate ?? this.rxRate,
        txRate: txRate ?? this.txRate,
        totalRx: totalRx,
        totalTx: totalTx,
        timestamp: timestamp,
      );
}

/// 采样网卡计数器，算出瞬时速率与本次累计。
class TrafficMeter {
  TrafficMeter({this.log}) {
    _snapshot = TrafficSnapshot(timestamp: _nowSeconds());
  }

  /// 两次采样间隔小于这个值就直接返回上一个读数（避免噪声）。
  static const double minSampleInterval = 0.4;

  /// 采样失败时最多容忍几次连续查不到网卡。
  static const int _maxMisses = 3;

  /// 日志出口（可选）。
  final void Function(String message)? log;

  late TrafficSnapshot _snapshot;
  String _alias = '';
  String _sourceAddress = '';
  (int, int, double)? _last;
  int _totalRx = 0;
  int _totalTx = 0;
  int _misses = 0;
  FfiNetwork? _ffi;
  bool _sampling = false;

  TrafficSnapshot get snapshot => _snapshot;

  /// 认证所在的本机地址；为空表示不按地址挑网卡。
  String get sourceAddress => _sourceAddress;

  void resetTotals() {
    _totalRx = 0;
    _totalTx = 0;
    _snapshot = TrafficSnapshot(
      interfaceName: _snapshot.interfaceName,
      rxBytes: _snapshot.rxBytes,
      txBytes: _snapshot.txBytes,
      timestamp: _snapshot.timestamp,
    );
  }

  /// 把统计钉在「拥有这个地址的网卡」上。
  ///
  /// 不钉的话，一条路由度量更好的 WiFi 会顶替掉真正在认证的那条链路。
  Future<void> setSourceAddress(String address) async {
    final String next = address == '0.0.0.0' ? '' : address;
    if (next == _sourceAddress) {
      return;
    }
    _sourceAddress = next;
    _alias = '';
    _last = null;
    _misses = 0;
    if (next.isNotEmpty) {
      await _resolveAlias();
    }
  }

  /// 采一次读数。并发调用会直接返回上一次结果。
  Future<TrafficSnapshot> sample() async {
    if (_sampling) {
      return _snapshot;
    }
    _sampling = true;
    try {
      return await _sampleOnce();
    } finally {
      _sampling = false;
    }
  }

  Future<TrafficSnapshot> _sampleOnce() async {
    if (_alias.isEmpty) {
      await _resolveAlias();
      if (_alias.isEmpty) {
        _misses += 1;
        if (_misses == _maxMisses && log != null) {
          log!('找不到认证网卡的流量计数器');
        }
        return _snapshot;
      }
    }

    final Map<String, (int, int)>? counters = await _readCounters(_alias);
    if (counters == null) {
      // 网卡可能正在重新协商（链路 up/down、DHCP 续租），连着查不到才放弃缓存。
      _misses += 1;
      if (_misses < _maxMisses) {
        return _snapshot;
      }
      if (log != null) {
        log!('网卡 $_alias 连续多次查不到，重新选择');
      }
      _alias = '';
      _last = null;
      _misses = 0;
      return _snapshot;
    }

    final (int rx, int tx) = _pick(counters) ?? (0, 0);
    final double now = _nowSeconds();
    double rxRate = 0;
    double txRate = 0;
    final (int, int, double)? last = _last;
    if (last != null) {
      final double elapsed = now - last.$3;
      if (elapsed < minSampleInterval) {
        return _snapshot;
      }
      final int deltaRx = rx - last.$1;
      final int deltaTx = tx - last.$2;
      // 负数说明计数器回绕或网卡重置。
      if (deltaRx < 0 || deltaTx < 0) {
        if (log != null) {
          log!('网卡计数器回绕/重置，已重新同步');
        }
      } else {
        rxRate = deltaRx / elapsed;
        txRate = deltaTx / elapsed;
        _totalRx += deltaRx;
        _totalTx += deltaTx;
      }
    }

    _last = (rx, tx, now);
    _misses = 0;
    _snapshot = TrafficSnapshot(
      interfaceName: _alias,
      rxBytes: rx,
      txBytes: tx,
      rxRate: rxRate,
      txRate: txRate,
      totalRx: _totalRx,
      totalTx: _totalTx,
      timestamp: now,
    );
    return _snapshot;
  }

  /// 用一次 PowerShell 拿到「别名 → (ReceivedBytes, SentBytes)」。
  /// 用 FFI 直读 `GetIfTable2` 的收发计数（不再起 PowerShell）。
  ///
  /// 返回「别名 → (收, 发)」，同名接口的多个过滤层别名也会一并带上；
  /// 取值时优先精确匹配，匹配不到再按前缀找。
  Future<Map<String, (int, int)>?> _readCounters(String alias) async {
    final FfiNetwork? net = _ffi ??= FfiNetwork.open();
    if (net == null) {
      return null;
    }
    final List<IfaceCounters> rows = net.readCounters();
    if (rows.isEmpty) {
      return null;
    }
    final Map<String, (int, int)> out = <String, (int, int)>{};
    for (final IfaceCounters row in rows) {
      out[row.alias] = (row.inOctets, row.outOctets);
    }
    return out;
  }

  /// 从一坨别名里挑出真正要盯的那个。
  ///
  /// `GetIfTable2` 会把同一块网卡按过滤层（WFP / QoS / 杀软 NDIS 过滤）拆成好几行，
  /// 名字长得像「以太网-WFP Native MAC Layer LightWeight Filter-0000」。
  /// 这些行的计数是一样的，所以取第一个能对上的即可。
  (int, int)? _pick(Map<String, (int, int)> counters) {
    final (int, int)? exact = counters[_alias];
    if (exact != null) {
      return exact;
    }
    for (final MapEntry<String, (int, int)> entry in counters.entries) {
      if (entry.key.startsWith(_alias)) {
        return entry.value;
      }
    }
    return null;
  }

  /// 由绑定地址反查网卡别名（只做一次，之后靠别名采样）。
  /// 用 Dart 标准库的网卡枚举找「谁拥有这个地址」。
  ///
  /// 原来是起 PowerShell 查 `Get-NetIPAddress`，但受限令牌下子进程直接起不来，
  /// 而且一次几十毫秒。`NetworkInterface.list()` 是纯用户态调用，没有这些毛病。
  Future<void> _resolveAlias() async {
    if (_sourceAddress.isEmpty) {
      return;
    }
    try {
      final List<NetworkInterface> interfaces = await NetworkInterface.list(
        includeLinkLocal: true,
        type: InternetAddressType.IPv4,
      );
      for (final NetworkInterface iface in interfaces) {
        for (final InternetAddress address in iface.addresses) {
          if (address.address == _sourceAddress) {
            _alias = iface.name;
            return;
          }
        }
      }
    } on Object catch (error) {
      log?.call('网卡别名解析失败：$error');
    }
  }
}

double _nowSeconds() => DateTime.now().millisecondsSinceEpoch / 1000.0;

