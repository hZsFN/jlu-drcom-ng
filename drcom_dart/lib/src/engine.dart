import 'dart:async';
import 'dart:io' show InternetAddressType, NetworkInterface;
import 'dart:math' as math;
import 'dart:typed_data';

import 'protocol.dart';
import 'transport.dart';

/// 引擎状态。与 Python 版 `EngineState` 同名同义，两边日志可以直接对照。
enum EngineState {
  idle,
  challenging,
  authenticating,
  online,
  retryWait,
  fatal,
  stopped,
}

/// 掉线原因（写进状态文件与通知文案）。
enum OfflineReason {
  userLogout,
  challengeFailed,
  loginFailed,
  timeout,
  keepaliveFailed,
  stopped,
}

/// 引擎对外广播的事件。
enum EngineEventKind {
  started,
  ip,
  online,
  keepaliveOk,
  keepaliveFailed,
  loginFailed,
  challengeFailed,
  fatal,
  retryScheduled,
  error,
  stopped,
}

class EngineEvent {
  const EngineEvent(
    this.kind, {
    this.message = '',
    this.detail = '',
    this.payload = const <String, Object?>{},
  });

  final EngineEventKind kind;
  final String message;
  final String detail;
  final Map<String, Object?> payload;

  @override
  String toString() => 'EngineEvent(${kind.name}: $message)';
}

/// 重连退避参数。默认值照搬 Python 版 `ReconnectConfig`。
class ReconnectPolicy {
  const ReconnectPolicy({
    this.enabled = true,
    this.minDelay = const Duration(seconds: 5),
    this.maxDelay = const Duration(minutes: 5),
    this.factor = 2,
    this.jitter = 0,
    this.giveUpAfter = 20,
    this.quietHoursStart = '',
    this.quietHoursEnd = '',
    this.quietHoursDelay = const Duration(minutes: 15),
  });

  final bool enabled;
  final Duration minDelay;
  final Duration maxDelay;
  final double factor;

  /// 抖动幅度（0.15 = ±15%）。测试传 0 或固定 [random01] 即可复现。
  final double jitter;
  final int giveUpAfter;

  /// `"HH:MM"`；两端任一为空则不启用静默时段。
  final String quietHoursStart;
  final String quietHoursEnd;
  final Duration quietHoursDelay;

  /// 第 [consecutiveFailures] 次失败后的等待时长。
  ///
  /// 与 Python 版一致：指数从 `failures - 1` 起算、先封顶再抖动、下限 1 秒；
  /// 落在静默时段则抬到 [quietHoursDelay]。
  Duration nextDelay(
    int consecutiveFailures, {
    DateTime? now,
    double random01 = 0.5,
  }) {
    final exponent = consecutiveFailures > 1 ? consecutiveFailures - 1 : 0;
    var ms = minDelay.inMicroseconds.toDouble() * math.pow(factor, exponent) /
        Duration.microsecondsPerMillisecond;
    final cap = maxDelay.inMilliseconds.toDouble();
    if (ms > cap) {
      ms = cap;
    }
    if (jitter > 0) {
      ms *= 1 + (random01 * 2 - 1) * jitter;
    }
    if (ms < 1000) {
      ms = 1000;
    }
    if (_inQuietHours(now ?? DateTime.now())) {
      final quiet = quietHoursDelay.inMilliseconds.toDouble();
      if (ms < quiet) {
        ms = quiet;
      }
    }
    return Duration(milliseconds: ms.round());
  }

  bool _inQuietHours(DateTime now) {
    final begin = _parseHhmm(quietHoursStart);
    final finish = _parseHhmm(quietHoursEnd);
    if (begin == null || finish == null || begin == finish) {
      return false;
    }
    final current = now.hour * 60 + now.minute;
    if (begin < finish) {
      return current >= begin && current < finish;
    }
    return current >= begin || current < finish; // 跨午夜
  }

  static int? _parseHhmm(String value) {
    final parts = value.split(':');
    if (parts.length != 2) {
      return null;
    }
    final hours = int.tryParse(parts[0].trim());
    final minutes = int.tryParse(parts[1].trim());
    if (hours == null || minutes == null) {
      return null;
    }
    return hours * 60 + minutes;
  }
}

typedef TransportFactory = Future<AuthTransport> Function();

/// 可注入的等待：返回 `false` = 被叫停，循环该退出了。
typedef Sleeper = Future<bool> Function(Duration duration);

/// 探测哪个本机地址能把挑战包送到认证服务器。
///
/// 入参是候选地址（`null` 表示 `0.0.0.0`），返回第一个能收到合法挑战响应的
/// 地址；全不通返回 `null`。
typedef BindProber = Future<String?> Function(List<String?> candidates);

/// 探测出口网卡时每个地址等多久：真机实测认证服务器在校园网内回包 < 100ms，
/// 800ms 足够，又不至于让整个探测拖成十几秒。
const Duration _probeTimeout = Duration(milliseconds: 800);
enum _SessionResult { transient, fatal }

/// 认证引擎：绑定 → 挑战 → 登录 → 保活，断了按退避重连。
///
/// 与 Python 版 `AuthEngine` 的控制流逐段对齐（那是线上跑了几个月的实现），
/// 差异只在：这里没有线程和锁，`stop()` 通过打断等待实现；网络层是可注入的，
/// 所以整个状态机能用假传输在毫秒内跑完整轮。
class AuthEngine {
  AuthEngine({
    required this.account,
    required this.password,
    required this.mac,
    this.server = defaultAuthServer,
    this.port = defaultAuthPort,
    TransportFactory? transportFactory,
    this.recvTimeout = const Duration(milliseconds: recvTimeoutMs),
    this.keepaliveInterval = const Duration(seconds: 20),
    this.challengeRetries = 3,
    this.postLoginDelay = Duration.zero,
    this.policy = const ReconnectPolicy(),
    Sleeper? sleeper,
    this.maxSessions = 0,
    this.bindAddress,
    this.bindPort = defaultBindPort,
    this.autoInterfaceFallback = true,
    BindProber? bindProber,
  })  : _transportFactory = transportFactory,
        _bindProber = bindProber,
        _sleeper = sleeper;

  final String account;
  final String password;
  final String mac;
  final String server;
  final int port;

  final Duration recvTimeout;
  final Duration keepaliveInterval;
  final int challengeRetries;
  final Duration postLoginDelay;
  final ReconnectPolicy policy;

  /// `>0` 时跑完这么多轮会话就自动停（测试用；生产留 0 = 一直跑）。
  final int maxSessions;

  /// 显式指定出口网卡地址；留空=自动。
  ///
  /// 多网卡时绑定 `0.0.0.0` 也能成功，但内核按路由 metric 挑网卡：一旦 VPN
  /// 或 Wi-Fi 的 metric 比校园网以太网低，挑战包就从错的网卡发出去，服务端
  /// 根本不回——看着像"服务器挂了"，其实是"我从错的地址问的"。
  final String? bindAddress;

  /// 本地源端口。参考实现绑在 61440，服务端会按 (源 IP, 源端口) 认会话——
  /// 真机实测：源端口用临时端口时，前两轮保活能过，第三轮服务端就不回了。
  final int bindPort;

  /// 自动枚举本机地址逐个探测（[bindAddress] 为空时才生效）。
  final bool autoInterfaceFallback;

  final TransportFactory? _transportFactory;

  /// 探测出口网卡的可注入实现（测试用；null = 真发挑战包探）。
  final BindProber? _bindProber;

  /// 探测选出的绑定地址；null = 0.0.0.0。
  String? _pickedBind;

  /// 探测选中的地址（诊断用；null 表示走系统默认出口）。
  String? get pickedBindAddress => _pickedBind;

  final Sleeper? _sleeper;
  final math.Random _random = math.Random();

  final StreamController<EngineEvent> _events =
      StreamController<EngineEvent>.broadcast();
  final List<EngineEvent> _history = <EngineEvent>[];

  bool _running = false;
  void Function()? _wakeUp;

  EngineState _state = EngineState.idle;
  String _stateMessage = '';
  OfflineReason? reason;
  String ip = '';
  List<int> authInformation = const <int>[];
  DateTime? onlineSince;
  int consecutiveFailures = 0;
  int sessionCount = 0;

  EngineState get state => _state;

  /// 当前状态的一句话说明（“正在获取挑战码”“在线 · 1.2.3.4”之类）。
  String get stateMessage => _stateMessage;
  bool get isRunning => _running;
  bool get isOnline => _state == EngineState.online;
  Stream<EngineEvent> get events => _events.stream;

  /// 已发生的全部事件（测试与日志面板用；生产可只看 [events]）。
  List<EngineEvent> get history => List<EngineEvent>.unmodifiable(_history);

  String get maskedAccount {
    final text = account;
    if (text.length <= 2) {
      return '*' * text.length;
    }
    if (text.length <= 5) {
      return '${text[0]}${'*' * (text.length - 1)}';
    }
    return '${text.substring(0, 2)}'
        '${'*' * (text.length - 4)}'
        '${text.substring(text.length - 2)}';
  }

  Duration get uptime {
    final since = onlineSince;
    if (since == null) {
      return Duration.zero;
    }
    return DateTime.now().difference(since);
  }

  // -- 生命周期 ---------------------------------------------------------

  Future<void> run() async {
    if (_running) {
      throw StateError('引擎已在运行');
    }
    _running = true;
    _emit(
      EngineEventKind.started,
      '认证线程已启动（账号 $maskedAccount → $server:$port）',
    );

    // 只有在用真实 UDP（没注入自定义传输）时才需要探网卡：注入了工厂就说明
    // 调用方自己管传输，探测只会多占几秒。注入了 prober 则一律探（测试用）。
    if (autoInterfaceFallback &&
        (_transportFactory == null || _bindProber != null)) {
      _pickedBind = await _probeBind();
      if (_pickedBind != null) {
        _emit(
          EngineEventKind.started,
          '出口网卡已探测：绑定 $_pickedBind',
        );
      }
    }

    var sessions = 0;
    try {
      while (_running) {
        final result = await _runSession();
        sessions += 1;
        if (!_running) {
          break;
        }
        if (result == _SessionResult.fatal) {
          _setState(EngineState.fatal, '需要人工处理');
          return;
        }
        if (maxSessions > 0 && sessions >= maxSessions) {
          break;
        }
        if (!_reconnectAllowed()) {
          _setState(EngineState.fatal, '自动重连已关闭');
          return;
        }

        final delay = policy.nextDelay(
          consecutiveFailures,
          random01: _random.nextDouble(),
        );
        _setState(
          EngineState.retryWait,
          '${delay.inSeconds} 秒后重试（第 $consecutiveFailures 次失败）',
        );
        _emit(
          EngineEventKind.retryScheduled,
          '${delay.inSeconds} 秒后重试',
          payload: <String, Object?>{'delayMs': delay.inMilliseconds},
        );
        if (!await _sleep(delay)) {
          break;
        }
      }
      reason ??= OfflineReason.userLogout;
    } finally {
      _running = false;
      onlineSince = null;
      if (_state != EngineState.fatal) {
        _setState(EngineState.stopped, '已停止');
      }
      _emit(EngineEventKind.stopped, '认证已停止');
      await _events.close();
    }
  }

  /// 请求停止；正在跑的 [run] 会在当前等待点退出。
  void stop({
    OfflineReason why = OfflineReason.userLogout,
  }) {
    if (!_running) {
      return;
    }
    reason = why;
    _running = false;
    final wake = _wakeUp;
    _wakeUp = null;
    wake?.call();
  }

  // -- 出口网卡选择 -------------------------------------------------------

  /// 候选绑定地址，按尝试顺序排；`null` 表示交给系统（`0.0.0.0`）。
  Future<List<String?>> _bindCandidates() async {
    final explicit = (bindAddress ?? '').trim();
    if (explicit.isNotEmpty && explicit != '0.0.0.0') {
      return <String?>[explicit];
    }
    if (!autoInterfaceFallback) {
      return <String?>[null];
    }
    final candidates = <String?>[null];
    try {
      final interfaces = await NetworkInterface.list(
        type: InternetAddressType.IPv4,
        includeLoopback: false,
        includeLinkLocal: false,
      );
      for (final iface in interfaces) {
        for (final address in iface.addresses) {
          final text = address.address;
          if (text.startsWith('127.') || text.startsWith('169.254.')) {
            continue;
          }
          if (text == '0.0.0.0' || candidates.contains(text)) {
            continue;
          }
          candidates.add(text);
        }
      }
    } on Object {
      // 枚举失败就只试默认出口，不影响主流程。
    }
    return candidates;
  }

  /// 逐个地址发一个挑战包，谁先给出合法响应就用谁当出口。
  Future<String?> _probeBind() async {
    final candidates = await _bindCandidates();
    // 只有一个候选（显式指定了地址，或关掉了回退）就没什么可挑的，
    // 直接用它，别白等一个超时。
    if (candidates.length <= 1) {
      return candidates.isEmpty ? null : candidates.first;
    }
    final prober = _bindProber;
    if (prober != null) {
      return prober(candidates);
    }
    for (final candidate in candidates) {
      UdpAuthTransport? probe;
      try {
        probe = UdpAuthTransport(
          server: server,
          port: port,
          localAddress: candidate,
          defaultTimeout: _probeTimeout,
        );
        await probe.send(buildChallengePacket());
        final reply = await probe.receive();
        if (reply != null) {
          parseChallengeResponse(reply); // 不是合法挑战响应就换下一个
          return candidate;
        }
      } catch (e) {
        // 这个地址绑不上或收不到（虚拟网卡、VPN 网卡常直接 SocketException），
        // 试下一个。
        _emit(
          EngineEventKind.challengeFailed,
          '探测出口失败：${candidate ?? '0.0.0.0'}',
          detail: '$e',
        );
      } finally {
        try {
          await probe?.close();
        } catch (_) {
          // 本来就没绑成功，关不掉也无所谓。
        }
      }
      if (!_running) {
        break;
      }
    }
    return null;
  }

  Future<AuthTransport> _makeTransport() async {
    final factory = _transportFactory;
    if (factory != null) {
      return factory();
    }
    return UdpAuthTransport(
      server: server,
      port: port,
      bindPort: bindPort,
      localAddress: _pickedBind,
    );
  }

  // -- 一轮会话 ---------------------------------------------------------

  Future<_SessionResult> _runSession() async {
    final transport = await _makeTransport();
    try {
      // ---- 挑战 ----
      _setState(EngineState.challenging, '正在获取挑战码');
      ChallengeResponse? challenge;
      for (var attempt = 1; attempt <= challengeRetries; attempt++) {
        try {
          final reply = await _transact(transport, buildChallengePacket());
          if (reply == null) {
            _emit(
              EngineEventKind.challengeFailed,
              '挑战包无响应（第 $attempt/$challengeRetries 次）',
              detail: '服务器未在 ${recvTimeout.inMilliseconds}ms 内回复。',
            );
          } else {
            challenge = parseChallengeResponse(reply);
            break;
          }
        } on ProtocolError catch (e) {
          _emit(
            EngineEventKind.challengeFailed,
            '挑战响应异常（第 $attempt/$challengeRetries 次）',
            detail: e.message,
          );
        }
        if (attempt < challengeRetries) {
          if (!await _sleep(const Duration(milliseconds: 500))) {
            return _SessionResult.transient;
          }
        }
      }
      if (!_running) {
        return _SessionResult.transient;
      }
      if (challenge == null) {
        consecutiveFailures += 1;
        reason = OfflineReason.challengeFailed;
        _setState(EngineState.retryWait, '挑战失败');
        return _SessionResult.transient;
      }

      ip = challenge.ip;
      _emit(
        EngineEventKind.ip,
        '本机 IP：${challenge.ip}',
        payload: <String, Object?>{'ip': challenge.ip},
      );

      // ---- 登录 ----
      _setState(EngineState.authenticating, '正在提交认证');
      final Uint8List loginPacket;
      try {
        loginPacket = buildLoginPacket(
          account: account,
          password: password,
          mac: mac,
          seed: challenge.seed,
        );
      } on Object catch (e) {
        reason = OfflineReason.loginFailed;
        _setState(EngineState.fatal, '配置不完整');
        _emit(EngineEventKind.fatal, '配置错误：$e');
        return _SessionResult.fatal;
      }

      final reply = await _transact(
        transport,
        loginPacket,
        accept: (data) =>
            data.isNotEmpty && (data[0] == 0x04 || data[0] == 0x05),
      );
      if (reply == null) {
        consecutiveFailures += 1;
        reason = OfflineReason.timeout;
        _setState(EngineState.retryWait, '登录响应超时');
        _emit(
          EngineEventKind.error,
          '登录响应超时',
          detail: '服务器未在 ${recvTimeout.inMilliseconds}ms 内回复登录请求。',
        );
        return _SessionResult.transient;
      }

      final outcome = parseLoginResponse(reply);
      if (!outcome.success) {
        final code = outcome.errorCode;
        _emit(
          EngineEventKind.loginFailed,
          outcome.message,
          detail: outcome.advice,
          payload: <String, Object?>{'errorCode': code},
        );
        if (code != null && isFatalLoginError(code)) {
          reason = OfflineReason.loginFailed;
          _setState(EngineState.fatal, outcome.message);
          _emit(
            EngineEventKind.fatal,
            outcome.message,
            detail: '这类错误重试无用，请按提示修正配置后手动重试。',
          );
          return _SessionResult.fatal;
        }
        consecutiveFailures += 1;
        _setState(EngineState.retryWait, outcome.message);
        return _SessionResult.transient;
      }

      // ---- 上线 ----
      authInformation = List<int>.unmodifiable(outcome.authInformation);
      onlineSince = DateTime.now();
      consecutiveFailures = 0;
      sessionCount += 1;
      _setState(EngineState.online, '在线 · $ip');
      _emit(
        EngineEventKind.online,
        '已上线 · $ip',
        payload: <String, Object?>{'ip': ip},
      );

      // ---- 保活 ----（一直跑到出问题或叫停）
      final why = await _keepaliveLoop(transport);
      onlineSince = null;
      reason = why;
      return _SessionResult.transient;
    } finally {
      await transport.close();
    }
  }

  // -- 保活（规程 4.8 / 4.9） -------------------------------------------

  Future<OfflineReason> _keepaliveLoop(AuthTransport transport) async {
    var counter = 0;
    var first = true;
    var ka2Failures = 0;

    while (_running) {
      // ---- keepalive 1 ----
      try {
        await _keepalive1(transport);
      } on Object catch (e) {
        _emit(
          EngineEventKind.keepaliveFailed,
          '保活失败（keepalive1）',
          detail: '$e',
        );
        return OfflineReason.timeout;
      }
      if (!await _sleep(postLoginDelay)) {
        return OfflineReason.userLogout;
      }

      // ---- keepalive 2 ----
      var ok = false;
      try {
        final round = await _keepalive2(transport, counter, first: first);
        counter = round.counter;
        ok = round.ok;
      } on Object catch (e) {
        _emit(EngineEventKind.error, 'keepalive2 异常', detail: '$e');
        ok = false;
      }
      first = false;

      if (!ok) {
        ka2Failures += 1;
        if (ka2Failures >= 5) {
          _emit(
            EngineEventKind.keepaliveFailed,
            '保活失败（keepalive2 连续失败）',
            detail: '连续 $ka2Failures 次 keepalive2 未通过',
          );
          return OfflineReason.keepaliveFailed;
        }
        // 服务器病态时不至于把我们打成死循环。
        if (!await _sleep(const Duration(milliseconds: 500))) {
          return OfflineReason.userLogout;
        }
        continue;
      }

      ka2Failures = 0;
      _emit(
        EngineEventKind.keepaliveOk,
        '保活正常',
        payload: <String, Object?>{'uptimeSeconds': uptime.inSeconds},
      );
      if (!await _sleep(keepaliveInterval)) {
        return OfflineReason.userLogout;
      }
    }
    return OfflineReason.userLogout;
  }

  /// 4.8：先发 8 字节探针，收挑战，回 42 字节，再确认首字节 0x07。
  Future<Keepalive1Result> _keepalive1(AuthTransport transport) async {
    final probe = await _transact(
      transport,
      keepalive1Packet1,
      accept: (data) => data.isNotEmpty && !looksLikeNotification(data),
    );
    if (probe == null) {
      throw ProtocolError('keepalive1 挑战无响应');
    }
    final result = parseKeepalive1Response(probe);

    final answer = buildKeepalive1Packet2(
      keepalive1Seed: result.seed,
      authInformation: authInformation,
    );
    final reply = await _transact(transport, answer);
    if (reply == null) {
      throw ProtocolError('keepalive1 应答无响应');
    }
    if (reply.isEmpty || reply[0] != 0x07) {
      final got = reply.isEmpty
          ? '空包'
          : '0x${reply[0].toRadixString(16).padLeft(2, '0')}';
      throw ProtocolError('keepalive1 应答首字节应为 0x07，实际 $got');
    }
    return result;
  }

  /// 4.9：首轮先发 file 包，然后 A（pkt_type=1）→ 取 tail → C（pkt_type=3）。
  Future<({int counter, bool ok})> _keepalive2(
    AuthTransport transport,
    int counter, {
    required bool first,
  }) async {
    if (first) {
      final filePacket = buildKeepalive2Packet(
        counter: counter,
        filePacket: true,
      );
      counter += 1;
      final reply = await _transact(transport, filePacket);
      if (reply == null) {
        return (counter: counter, ok: false);
      }
      final result = parseKeepalive2Response(reply, filePacket: true);
      // file 包容忍服务端回 0x28，但完全不是 0x07 就判失败。
      if (!result.ok && (reply.isEmpty || reply[0] != 0x07)) {
        return (counter: counter, ok: false);
      }
    }

    // A 包
    final aPacket = buildKeepalive2Packet(counter: counter, pktType: 1);
    counter += 1;
    final aReply = await _transact(transport, aPacket);
    if (aReply == null) {
      return (counter: counter, ok: false);
    }
    final aResult = parseKeepalive2Response(aReply);
    if (!aResult.ok) {
      return (counter: counter, ok: false);
    }

    // C 包（把服务端给的 tail 带回去）
    final cPacket = buildKeepalive2Packet(
      counter: counter,
      pktType: 3,
      tail: aResult.tail,
    );
    counter += 1;
    final cReply = await _transact(transport, cPacket);
    if (cReply == null) {
      return (counter: counter, ok: false);
    }
    return (counter: counter, ok: parseKeepalive2Response(cReply).ok);
  }

  // -- 基础工具 ---------------------------------------------------------

  /// 发一包，收第一个被 [accept] 认可的包；超时返回 `null`。
  ///
  /// 与 Python 版 `_recv_until` 等价：每次 `receive` 都重置等待窗口，
  /// 所以话多的服务器不会把我们饿死，安静的服务器仍会快速失败。
  Future<Uint8List?> _transact(
    AuthTransport transport,
    Uint8List packet, {
    bool Function(Uint8List data)? accept,
    int maxPackets = 32,
  }) async {
    await transport.send(packet);
    for (var i = 0; i < maxPackets; i++) {
      final data = await transport.receive(timeout: recvTimeout);
      if (data == null) {
        return null;
      }
      if (accept == null || accept(data)) {
        return data;
      }
    }
    return null;
  }

  Future<bool> _sleep(Duration duration) {
    final injected = _sleeper;
    if (injected != null) {
      return injected(duration);
    }
    if (!_running) {
      return Future<bool>.value(false);
    }
    if (duration <= Duration.zero) {
      return Future<bool>.value(_running);
    }
    final completer = Completer<bool>();
    final timer = Timer(duration, () {
      if (!completer.isCompleted) {
        completer.complete(true);
      }
    });
    _wakeUp = () {
      timer.cancel();
      if (!completer.isCompleted) {
        completer.complete(false);
      }
    };
    return completer.future;
  }

  bool _reconnectAllowed() {
    if (!policy.enabled) {
      _emit(EngineEventKind.error, '自动重连已关闭，请手动登录');
      return false;
    }
    if (consecutiveFailures >= policy.giveUpAfter) {
      _emit(
        EngineEventKind.fatal,
        '连续失败 $consecutiveFailures 次，已暂停自动重连',
        detail: '问题多半不在网络抖动，而在配置或环境。',
      );
      return false;
    }
    return true;
  }

  void _setState(EngineState next, String message) {
    _state = next;
    _stateMessage = message;
  }

  void _emit(
    EngineEventKind kind,
    String message, {
    String detail = '',
    Map<String, Object?> payload = const <String, Object?>{},
  }) {
    final event = EngineEvent(
      kind,
      message: message,
      detail: detail,
      payload: payload,
    );
    _history.add(event);
    if (!_events.isClosed) {
      _events.add(event);
    }
  }
}
