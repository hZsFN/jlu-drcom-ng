import 'dart:typed_data';

import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:test/test.dart';

// ---------------------------------------------------------------------------
// 造包工具：按协议格式手工拼服务端会回什么，这样状态机能脱网跑。
// ---------------------------------------------------------------------------

Uint8List challengeReply({
  List<int> seed = const <int>[0x8a, 0xf5, 0x5e, 0x0f],
  String ip = '192.0.2.10',
}) {
  final data = Uint8List(76)..[0] = 0x02;
  data.setRange(4, 8, seed);
  data.setRange(20, 24, ip.split('.').map(int.parse).toList());
  return data;
}

Uint8List loginOkReply({
  List<int> auth = const <int>[
    0x44, 0x72, 0x63, 0x6f, 0x0a, 0x64, 0x3d, 0x03,
    0x0f, 0xbb, 0xac, 0x12, 0x7b, 0x3f, 0x01, 0x00,
  ],
}) {
  final data = Uint8List(45)..[0] = 0x04;
  data.setRange(23, 39, auth);
  return data;
}

Uint8List loginFailReply(int code) {
  final data = Uint8List(8)..[0] = 0x05;
  data[4] = code;
  return data;
}

Uint8List notification() => Uint8List(16)..[0] = 0x4d;

Uint8List keepalive1Challenge({
  List<int> seed = const <int>[0x00, 0x11, 0x22, 0x33],
}) {
  final data = Uint8List(12)..[0] = 0x07;
  data.setRange(8, 12, seed);
  return data;
}

Uint8List keepalive1Ack() => Uint8List(40)..[0] = 0x07;

Uint8List keepalive2Reply({
  bool file = false,
  bool ok = true,
  List<int> tail = const <int>[0xde, 0xad, 0xbe, 0xef],
}) {
  final data = Uint8List(20);
  data[0] = ok ? 0x07 : 0x00;
  data[2] = ok ? (file ? 0x10 : 0x28) : 0x00;
  data.setRange(16, 20, tail);
  return data;
}

// ---------------------------------------------------------------------------
// 假传输：按脚本逐包喂回，`null` = 超时。
// ---------------------------------------------------------------------------

class FakeTransport implements AuthTransport {
  FakeTransport(this.script);

  final List<Uint8List?> script;
  final List<Uint8List> sent = <Uint8List>[];

  int _cursor = 0;
  bool closed = false;

  @override
  Future<void> send(Uint8List packet) async {
    sent.add(packet);
  }

  @override
  Future<Uint8List?> receive({Duration? timeout}) async {
    if (_cursor >= script.length) {
      return null;
    }
    return script[_cursor++];
  }

  @override
  int? get localPort => 40000;

  @override
  Future<void> close() async {
    closed = true;
  }
}

class _Rig {
  _Rig({
    required this.script,
    this.maxSessions = 1,
    this.sleepLimit = 1 << 20,
    this.policy = const ReconnectPolicy(),
  });

  final List<Uint8List?> script;
  final int maxSessions;
  final int sleepLimit;
  final ReconnectPolicy policy;

  final List<Duration> sleeps = <Duration>[];
  late final FakeTransport transport = FakeTransport(script);
  int sleepCalls = 0;

  AuthEngine build() => AuthEngine(
        account: 'testuser01',
        password: 'secret12',
        mac: '84:A9:38:FC:6A:51',
        transportFactory: () async => transport,
        policy: policy,
        maxSessions: maxSessions,
        sleeper: (duration) async {
          sleeps.add(duration);
          sleepCalls += 1;
          return sleepCalls <= sleepLimit;
        },
      );
}

void main() {
  group('引擎：上线与保活', () {
    test('挑战 → 登录 → 一轮保活（跳过 0x4D 通知包）→ 停止', () async {
      final rig = _Rig(
        script: <Uint8List?>[
          challengeReply(),
          loginOkReply(),
          notification(), // 服务端插播通知，必须被跳过
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(file: true),
          keepalive2Reply(),
          keepalive2Reply(),
        ],
        sleepLimit: 1, // 第 1 次是 postLoginDelay，第 2 次（keepaliveInterval）叫停
      );
      final engine = rig.build();

      await engine.run();

      final kinds = engine.history.map((e) => e.kind).toList();
      expect(kinds, contains(EngineEventKind.ip));
      expect(kinds, contains(EngineEventKind.online));
      expect(kinds, contains(EngineEventKind.keepaliveOk));
      expect(engine.state, EngineState.stopped);
      expect(engine.reason, OfflineReason.userLogout);
      expect(engine.sessionCount, 1);
      expect(engine.consecutiveFailures, 0);
      expect(engine.ip, '192.0.2.10');
      expect(engine.authInformation, hasLength(16));
      expect(rig.transport.closed, isTrue);
      // 发送序列：挑战 → 登录 → (跳过通知) ka1 探针 → ka1 应答 → file → A → C
      expect(rig.transport.sent, hasLength(7));
    });

    test('保活间隔用满 20 秒', () async {
      final rig = _Rig(
        script: <Uint8List?>[
          challengeReply(),
          loginOkReply(),
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(file: true),
          keepalive2Reply(),
          keepalive2Reply(),
        ],
        sleepLimit: 1,
      );
      await rig.build().run();
      expect(rig.sleeps, contains(const Duration(seconds: 20)));
    });
  });

  group('引擎：失败分支', () {
    test('挑战连超时 3 次 → 退避 5 秒 → 第二轮成功上线', () async {
      final rig = _Rig(
        script: <Uint8List?>[
          null, null, null, // 第 1 轮：三次挑战都没回
          challengeReply(),
          loginOkReply(),
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(file: true),
          keepalive2Reply(),
          keepalive2Reply(),
        ],
        maxSessions: 2,
        sleepLimit: 5,
      );
      final engine = rig.build();

      await engine.run();

      final challengeFails = engine.history
          .where((e) => e.kind == EngineEventKind.challengeFailed)
          .length;
      expect(challengeFails, 3);

      final retry = engine.history
          .where((e) => e.kind == EngineEventKind.retryScheduled)
          .toList();
      expect(retry, hasLength(1));
      expect(retry.single.payload['delayMs'], 5000);

      expect(
        engine.history.map((e) => e.kind),
        contains(EngineEventKind.online),
      );
      expect(engine.sessionCount, 1);
    });

    test('登录报密码错（0x03）→ 直接 FATAL，不再重试', () async {
      final rig = _Rig(
        script: <Uint8List?>[challengeReply(), loginFailReply(0x03)],
      );
      final engine = rig.build();

      await engine.run();

      expect(engine.state, EngineState.fatal);
      expect(engine.reason, OfflineReason.loginFailed);
      final fatal =
          engine.history.where((e) => e.kind == EngineEventKind.fatal).toList();
      expect(fatal, isNotEmpty);
      expect(fatal.first.message, contains('密码'));
      // 致命错误只发两个包（挑战 + 登录），不进入保活
      expect(rig.transport.sent, hasLength(2));
    });

    test('登录报服务器忙（0x02）→ 瞬态，算一次失败后重试', () async {
      final rig = _Rig(
        script: <Uint8List?>[challengeReply(), loginFailReply(0x02)],
        maxSessions: 1,
      );
      final engine = rig.build();

      await engine.run();

      expect(engine.state, EngineState.stopped);
      expect(engine.consecutiveFailures, 1);
      expect(
        engine.history.map((e) => e.kind),
        contains(EngineEventKind.loginFailed),
      );
    });

    test('keepalive1 无响应 → 掉线，原因是超时', () async {
      final rig = _Rig(
        script: <Uint8List?>[challengeReply(), loginOkReply(), null],
      );
      final engine = rig.build();

      await engine.run();

      expect(engine.reason, OfflineReason.timeout);
      expect(
        engine.history.map((e) => e.kind),
        contains(EngineEventKind.keepaliveFailed),
      );
    });

    test('keepalive2 连续 5 次未通过 → 掉线', () async {
      final rig = _Rig(
        script: <Uint8List?>[
          challengeReply(),
          loginOkReply(),
          // 第 1 轮：file 包成功，A 包失败
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(file: true),
          keepalive2Reply(ok: false),
          // 第 2~5 轮：A 包一直失败
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(ok: false),
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(ok: false),
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(ok: false),
          keepalive1Challenge(),
          keepalive1Ack(),
          keepalive2Reply(ok: false),
        ],
      );
      final engine = rig.build();

      await engine.run();

      expect(engine.reason, OfflineReason.keepaliveFailed);
      final failed = engine.history
          .where((e) => e.kind == EngineEventKind.keepaliveFailed)
          .toList();
      expect(failed, hasLength(1));
      expect(failed.single.detail, contains('5'));
    });

    test('连续失败达到 giveUpAfter 就停手，不再重连', () async {
      final rig = _Rig(
        script: <Uint8List?>[null, null, null],
        maxSessions: 0, // 不限轮次，交给策略判断
        policy: const ReconnectPolicy(giveUpAfter: 1),
      );
      final engine = rig.build();

      await engine.run();

      expect(engine.state, EngineState.fatal);
      expect(engine.consecutiveFailures, 1);
      expect(
        engine.history.map((e) => e.kind),
        contains(EngineEventKind.fatal),
      );
    });
  });

  group('退避策略', () {
    test('指数增长且封顶', () {
      const policy = ReconnectPolicy(
        minDelay: Duration(seconds: 5),
        maxDelay: Duration(minutes: 5),
        factor: 2,
      );
      expect(policy.nextDelay(1).inSeconds, 5);
      expect(policy.nextDelay(2).inSeconds, 10);
      expect(policy.nextDelay(3).inSeconds, 20);
      expect(policy.nextDelay(10).inSeconds, 300);
      expect(policy.nextDelay(50).inSeconds, 300);
    });

    test('抖动按给定随机数可复现，且不低于 1 秒', () {
      const policy = ReconnectPolicy(
        minDelay: Duration(seconds: 5),
        maxDelay: Duration(minutes: 5),
        factor: 2,
        jitter: 0.2,
      );
      final low = policy.nextDelay(1, random01: 0).inMilliseconds;
      final mid = policy.nextDelay(1, random01: 0.5).inMilliseconds;
      final high = policy.nextDelay(1, random01: 1).inMilliseconds;
      expect(low, 4000);
      expect(mid, 5000);
      expect(high, 6000);

      const tight = ReconnectPolicy(
        minDelay: Duration(milliseconds: 200),
        jitter: 0.5,
      );
      expect(
        tight.nextDelay(1, random01: 0).inMilliseconds,
        1000,
        reason: '最小值被夹到 1 秒',
      );
    });

    test('静默时段把间隔抬到 quietHoursDelay（含跨午夜）', () {
      const policy = ReconnectPolicy(
        minDelay: Duration(seconds: 5),
        quietHoursStart: '23:00',
        quietHoursEnd: '07:00',
        quietHoursDelay: Duration(minutes: 15),
      );
      // 23:30 在窗口内
      expect(
        policy.nextDelay(1, now: DateTime(2026, 10, 27, 23, 30)).inMinutes,
        15,
      );
      // 06:59 也在（跨午夜）
      expect(
        policy.nextDelay(1, now: DateTime(2026, 10, 27, 6, 59)).inMinutes,
        15,
      );
      // 12:00 不在
      expect(
        policy.nextDelay(1, now: DateTime(2026, 10, 27, 12, 0)).inSeconds,
        5,
      );
    });
  });
}
