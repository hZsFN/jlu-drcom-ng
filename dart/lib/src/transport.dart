import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'protocol.dart';

/// UDP 收发抽象。
///
/// 拆成 [send] / [receive] 而不是「一问一答」，是因为保活阶段服务端会主动
/// 插播通知包（首字节 0x4D），引擎要能在同一次等待里丢掉它再继续收
/// （见 Python 版 `engine._recv_until`）。抽成接口是为了让状态机能用假实现
/// 在没有网络的情况下逐字节喂脚本。
abstract interface class AuthTransport {
  /// 发一个报文给认证服务器。
  Future<void> send(Uint8List packet);

  /// 等一个回包；超时返回 `null`。
  ///
  /// 超时是协议的正常分支（重试、判离线都靠它），所以不抛异常。
  Future<Uint8List?> receive({Duration? timeout});

  /// 本端实际绑定的端口（诊断用）。
  int? get localPort;

  Future<void> close();
}

/// 基于 `RawDatagramSocket` 的真实 UDP 实现。
///
/// 约定：
/// * 惰性 bind —— 构造对象不碰网络，第一次 [send] 才占端口；
/// * 收到的包排进队列，[receive] 从队列取 —— 这样两个 `await` 之间到达的
///   回包不会丢失（服务端发得比我们要得快时很关键）；
/// * 只接受来自 [server] 的报文，别处来的直接丢弃并计数。
class UdpAuthTransport implements AuthTransport {
  UdpAuthTransport({
    required this.server,
    required this.port,
    this.bindPort = 0,
    this.localAddress,
    this.defaultTimeout = const Duration(milliseconds: recvTimeoutMs),
  });

  final String server;
  final int port;

  /// 0 = 交给系统分配。真机实测服务端不校验源端口，所以默认不抢 61440。
  final int bindPort;

  /// 绑定的本机地址；null / "0.0.0.0" = 交给系统选出口。
  ///
  /// 多网卡时绑定 `0.0.0.0` 通常也能成功，但内核随后按路由 metric 挑网卡：
  /// 一旦 VPN 或 Wi-Fi 的 metric 比校园网以太网低，挑战包就从错的网卡出去，
  /// 服务端根本不回——看起来像"服务器挂了"，其实是"我从错的地址问的"。
  /// 绑到具体地址才能把出口钉死。
  final String? localAddress;

  final Duration defaultTimeout;

  RawDatagramSocket? _socket;
  final List<Uint8List> _inbox = <Uint8List>[];
  Completer<Uint8List?>? _waiter;

  int _droppedForeign = 0;

  Object? _lastError;

  /// 最近一次 socket 层错误（路由不可达、网卡掉线等），诊断用。
  Object? get lastError => _lastError;

  /// 来自非认证服务器地址的报文数量（诊断串包/劫持用）。
  int get droppedForeign => _droppedForeign;

  @override
  int? get localPort => _socket?.port;

  Future<RawDatagramSocket> _ensureSocket() async {
    final existing = _socket;
    if (existing != null) {
      return existing;
    }
    final requested = localAddress?.trim() ?? '';
    final bindTo = (requested.isEmpty || requested == '0.0.0.0')
        ? InternetAddress.anyIPv4
        : (InternetAddress.tryParse(requested) ??
            (throw ArgumentError('localAddress 不是合法 IPv4：$requested')));
    final socket = await RawDatagramSocket.bind(bindTo, bindPort);
    socket.listen(
      (RawSocketEvent event) {
        if (event != RawSocketEvent.read) {
          return;
        }
        final datagram = socket.receive();
        if (datagram == null) {
          return;
        }
        if (datagram.address.address != server) {
          _droppedForeign += 1;
          return;
        }
        final data = Uint8List.fromList(datagram.data);
        final waiter = _waiter;
        if (waiter != null && !waiter.isCompleted) {
          _waiter = null;
          waiter.complete(data);
        } else {
          _inbox.add(data);
        }
      },
      // 不加 onError 的话，路由不可达（Windows errno 1231）这类 socket 异步
      // 错误会直接变成 zone 里的未捕获异常，把整个进程打掉。这里转成"这一轮
      // 收不到包"，由引擎的重连策略处理。
      onError: (Object error) {
        _lastError = error;
        final waiter = _waiter;
        if (waiter != null && !waiter.isCompleted) {
          _waiter = null;
          waiter.complete(null);
        }
      },
    );
    _socket = socket;
    return socket;
  }

  @override
  Future<void> send(Uint8List packet) async {
    final socket = await _ensureSocket();
    socket.send(packet, InternetAddress(server), port);
  }

  @override
  Future<Uint8List?> receive({Duration? timeout}) async {
    await _ensureSocket();
    if (_inbox.isNotEmpty) {
      return _inbox.removeAt(0);
    }
    final waiter = Completer<Uint8List?>();
    _waiter = waiter;
    return waiter.future.timeout(
      timeout ?? defaultTimeout,
      onTimeout: () {
        if (identical(_waiter, waiter)) {
          _waiter = null;
        }
        return null;
      },
    );
  }

  @override
  Future<void> close() async {
    _socket?.close();
    _socket = null;
    _inbox.clear();
    _waiter = null;
  }
}
