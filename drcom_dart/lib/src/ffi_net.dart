/// Windows 网络信息采集，全部直连系统 API，不起任何子进程。
///
/// 之前用 `powershell Get-NetAdapterStatistics` / `ping.exe` 采数据，问题有三：
/// 一是每次采样要起一个进程，5 秒一次实测能吃掉单核一成以上；
/// 二是进程处于受限令牌（低完整性）时，子进程直接起不来；
/// 三是冷启动可能超过超时，采样时好时坏。
/// 这里改成 FFI 直调 `iphlpapi.dll`，毫秒级、无进程、无权限要求。
library;

import 'dart:ffi';
import 'dart:io';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';



/// 一个网卡接口的收发字节计数。
class IfaceCounters {
  const IfaceCounters({
    required this.alias,
    required this.description,
    required this.inOctets,
    required this.outOctets,
    required this.operUp,
  });

  final String alias;
  final String description;
  final int inOctets;
  final int outOctets;
  final bool operUp;

  @override
  String toString() =>
      'IfaceCounters($alias rx=$inOctets tx=$outOctets up=$operUp)';
}

/// 一次 ICMP 探测的结果。
class PingOutcome {
  const PingOutcome({required this.ok, required this.roundTripMs, this.status});

  const PingOutcome.failed(this.status)
      : ok = false,
        roundTripMs = null;

  final bool ok;
  final int? roundTripMs;
  final int? status;
}

/// `MIB_IF_ROW2` 在 x64 下的字节大小。
const int _rowSize = 1352;

/// `MIB_IF_TABLE2` 中 `Table` 数组相对结构体起始的偏移（NumEntries 4 字节 + 4 字节对齐）。
const int _tableOffset = 8;

/// `MIB_IF_ROW2` 字段偏移（x64，已按 MSDN 结构顺序与自然对齐推算）。
// 下面这几个偏移不是从 MSDN 结构体硬推的，是拿真机实测校准出来的：
// 先在表里按 1352 的步长定位到名为「以太网」的那一行，再间隔 3 秒采两次求差，
// 只有 InOctets / OutOctets 会出现与系统 `Get-NetAdapterStatistics` 同量级的增量。
// 推错 4 个字节就会读到隔壁字段，表现为速率时有时无、数值跳变。
const int _offAlias = 28; // WCHAR Alias[257]
const int _offDescription = 542; // WCHAR Description[257]
const int _offOperStatus = 1160;
const int _offInOctets = 1208;
const int _offOutOctets = 1280;

/// 网卡是否"运行中"（`IF_OPER_STATUS::IfOperStatusUp`）。
const int _operStatusUp = 1;

typedef _GetIfTable2C = Int32 Function(Pointer<Pointer<Uint8>> table);
typedef _GetIfTable2Dart = int Function(Pointer<Pointer<Uint8>> table);

typedef _FreeMibTableC = Void Function(Pointer<Uint8> memory);
typedef _FreeMibTableDart = void Function(Pointer<Uint8> memory);

typedef _IcmpCreateFileC = IntPtr Function();
typedef _IcmpCreateFileDart = int Function();

typedef _IcmpCloseHandleC = Int32 Function(IntPtr handle);
typedef _IcmpCloseHandleDart = int Function(int handle);

typedef _IcmpSendEchoC = Uint32 Function(
  IntPtr handle,
  Uint32 destination,
  Pointer<Uint8> requestData,
  Uint16 requestSize,
  Pointer<Uint8> requestOptions,
  Pointer<Uint8> replyBuffer,
  Uint32 replySize,
  Uint32 timeout,
);
typedef _IcmpSendEchoDart = int Function(
  int handle,
  int destination,
  Pointer<Uint8> requestData,
  int requestSize,
  Pointer<Uint8> requestOptions,
  Pointer<Uint8> replyBuffer,
  int replySize,
  int timeout,
);

/// FFI 版网络助手。构造失败（非 Windows / 缺库）时 [available] 为 false。
class FfiNetwork {
  FfiNetwork._(this._getIfTable2, this._freeMibTable, this._icmpCreateFile,
      this._icmpSendEcho, this._icmpCloseHandle);

  /// 建一个可用的实例；当前平台不支持时返回 null。
  static FfiNetwork? open() {
    if (!Platform.isWindows) {
      return null;
    }
    try {
      final DynamicLibrary iphlpapi = DynamicLibrary.open('iphlpapi.dll');
      return FfiNetwork._(
        iphlpapi.lookupFunction<_GetIfTable2C, _GetIfTable2Dart>('GetIfTable2'),
        iphlpapi.lookupFunction<_FreeMibTableC, _FreeMibTableDart>(
            'FreeMibTable'),
        iphlpapi.lookupFunction<_IcmpCreateFileC, _IcmpCreateFileDart>(
            'IcmpCreateFile'),
        iphlpapi.lookupFunction<_IcmpSendEchoC, _IcmpSendEchoDart>(
            'IcmpSendEcho'),
        iphlpapi.lookupFunction<_IcmpCloseHandleC, _IcmpCloseHandleDart>(
            'IcmpCloseHandle'),
      );
    } on Object {
      return null;
    }
  }

  final _GetIfTable2Dart _getIfTable2;
  final _FreeMibTableDart _freeMibTable;
  final _IcmpCreateFileDart _icmpCreateFile;
  final _IcmpSendEchoDart _icmpSendEcho;
  final _IcmpCloseHandleDart _icmpCloseHandle;

  /// 读全部网卡接口的收发计数。失败返回空表。
  List<IfaceCounters> readCounters() {
    final Pointer<Pointer<Uint8>> holder = calloc<Pointer<Uint8>>();
    try {
      if (_getIfTable2(holder) != 0) {
        return const <IfaceCounters>[];
      }
      final Pointer<Uint8> table = holder.value;
      if (table == nullptr) {
        return const <IfaceCounters>[];
      }
      try {
        final int count = table.cast<Uint32>().value;
        if (count <= 0) {
          return const <IfaceCounters>[];
        }
        final int total = _tableOffset + count * _rowSize;
        final Uint8List bytes = table.asTypedList(total);
        final ByteData data = ByteData.sublistView(bytes);
        final List<IfaceCounters> rows = <IfaceCounters>[];
        for (int i = 0; i < count; i++) {
          final int base = _tableOffset + i * _rowSize;
          rows.add(IfaceCounters(
            alias: _readWideString(data, base + _offAlias),
            description: _readWideString(data, base + _offDescription),
            inOctets: data.getUint64(base + _offInOctets, Endian.little),
            outOctets: data.getUint64(base + _offOutOctets, Endian.little),
            operUp: data.getUint32(base + _offOperStatus, Endian.little) ==
                _operStatusUp,
          ));
        }
        return rows;
      } finally {
        _freeMibTable(table);
      }
    } on Object {
      return const <IfaceCounters>[];
    } finally {
      calloc.free(holder);
    }
  }

  /// 对 [ipv4] 发一次 ICMP echo，最多等 [timeoutMs] 毫秒。
  ///
  /// [sourceAddress] 非空时用 `IcmpSendEcho2Ex` 的等价语义：这里仍走
  /// `IcmpSendEcho`，源地址由系统路由决定；需要绑源时调用方自己挑对网卡。
  PingOutcome ping(String ipv4, {int timeoutMs = 1000, int payload = 32}) {
    final int? destination = _ipToUint32(ipv4);
    if (destination == null) {
      return const PingOutcome.failed(-1);
    }
    final int handle = _icmpCreateFile();
    if (handle == 0 || handle == -1) {
      return const PingOutcome.failed(-2);
    }
    try {
      final Pointer<Uint8> request = calloc<Uint8>(payload);
      // ReplySize 要给 sizeof(ICMP_ECHO_REPLY)=40 + 数据 + 8 字节余量。
      final int replySize = 40 + payload + 64;
      final Pointer<Uint8> reply = calloc<Uint8>(replySize);
      try {
        for (int i = 0; i < payload; i++) {
          request[i] = 0x61 + (i % 26);
        }
        final int replies = _icmpSendEcho(
          handle,
          destination,
          request,
          payload,
          nullptr,
          reply,
          replySize,
          timeoutMs,
        );
        if (replies == 0) {
          return const PingOutcome.failed(0);
        }
        final ByteData data = ByteData.sublistView(reply.asTypedList(replySize));
        final int status = data.getUint32(4, Endian.little);
        final int roundTrip = data.getUint32(8, Endian.little);
        if (status != 0) {
          return PingOutcome.failed(status);
        }
        return PingOutcome(ok: true, roundTripMs: roundTrip);
      } finally {
        calloc.free(request);
        calloc.free(reply);
      }
    } finally {
      _icmpCloseHandle(handle);
    }
  }

  static String _readWideString(ByteData data, int offset) {
    final StringBuffer buffer = StringBuffer();
    for (int i = 0;; i++) {
      final int unit = data.getUint16(offset + i * 2, Endian.little);
      if (unit == 0) {
        break;
      }
      buffer.writeCharCode(unit);
      if (i > 256) {
        break;
      }
    }
    return buffer.toString();
  }

  /// `a.b.c.d` → 网络字节序的 uint32（Windows `IPAddr` 就是这种表示）。
  static int? _ipToUint32(String text) {
    final List<String> parts = text.split('.');
    if (parts.length != 4) {
      return null;
    }
    int value = 0;
    for (final String part in parts) {
      final int? octet = int.tryParse(part);
      if (octet == null || octet < 0 || octet > 255) {
        return null;
      }
      value = (value << 8) | octet;
    }
    return value;
  }
}
