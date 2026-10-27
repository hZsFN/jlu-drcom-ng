import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:test/test.dart';

/// 只回两包就够：挑战 + 登录成功。之后保活探测会超时（[FakeTransport] 用完
/// 脚本返回 null），但在那之前应用早就进 online 了，测试在那时叫停。
Uint8List _challengeReply() {
  final data = Uint8List(76)..[0] = 0x02;
  data.setRange(4, 8, <int>[0x8a, 0xf5, 0x5e, 0x0f]);
  data.setRange(20, 24, <int>[192, 0, 2, 10]);
  return data;
}

Uint8List _loginOkReply() {
  final data = Uint8List(45)..[0] = 0x04;
  data.setRange(23, 39, <int>[
    0x44, 0x72, 0x63, 0x6f, 0x0a, 0x64, 0x3d, 0x03,
    0x0f, 0xbb, 0xac, 0x12, 0x7b, 0x3f, 0x01, 0x00,
  ]);
  return data;
}

/// 上线之后引擎会立刻做一轮保活探测（`postLoginDelay` 默认 0.2 秒），
/// 这几包就是给它吃的——不吃的话 0.2 秒后就掉线，测不到 online。
List<Uint8List> _keepaliveReplies() {
  final challenge = Uint8List(12)..[0] = 0x07;
  challenge.setRange(8, 12, <int>[0x00, 0x11, 0x22, 0x33]);
  final ack = Uint8List(40)..[0] = 0x07;
  final fileReply = Uint8List(20);
  fileReply[0] = 0x07;
  fileReply[2] = 0x10;
  fileReply.setRange(16, 20, <int>[0xde, 0xad, 0xbe, 0xef]);
  final normalReply = Uint8List(20);
  normalReply[0] = 0x07;
  normalReply[2] = 0x28;
  normalReply.setRange(16, 20, <int>[0xde, 0xad, 0xbe, 0xef]);
  return <Uint8List>[challenge, ack, fileReply, normalReply, normalReply];
}

class _FakeTransport implements AuthTransport {
  _FakeTransport(this.script);

  final List<Uint8List> script;
  int _cursor = 0;
  bool closed = false;

  @override
  Future<void> send(Uint8List packet) async {}

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

Directory _tempDir() {
  final dir = Directory.systemTemp.createTempSync('drcom-app-test-');
  addTearDown(() {
    if (dir.existsSync()) {
      dir.deleteSync(recursive: true);
    }
  });
  return dir;
}

/// 造一份能跑起来的配置：一个带密码的账号，关掉网卡探测（测试不碰真网络）。
({AppConfig config, ConfigStore store, String statusPath}) _makeRig() {
  final dir = _tempDir();
  final configPath = '${dir.path}${Platform.pathSeparator}config.json';
  final config = AppConfig(
    accounts: [
      Account(
        account: 'testuser01',
        mac: '02:00:00:00:00:01',
        passwordProtected: protectPassword('secret12'),
        autoLogin: true,
      ),
    ],
  );
  config.auth.autoInterfaceFallback = false;
  return (
    config: config,
    store: ConfigStore(path: configPath),
    statusPath: '${dir.path}${Platform.pathSeparator}status.json',
  );
}

Future<Map<String, dynamic>?> _readStatus(String path) async {
  final file = File(path);
  if (!file.existsSync()) {
    return null;
  }
  try {
    return jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
  } on FormatException {
    return null;
  }
}

/// 轮询状态文件直到满足条件或超时。
Future<Map<String, dynamic>?> _waitForStatus(
  String path,
  bool Function(Map<String, dynamic>) predicate, {
  Duration timeout = const Duration(seconds: 8),
}) async {
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    final status = await _readStatus(path);
    if (status != null && predicate(status)) {
      return status;
    }
    await Future<void>.delayed(const Duration(milliseconds: 50));
  }
  return _readStatus(path);
}

void main() {
  group('应用层：装配与失败路径', () {
    test('没有账号时 start 抛 StateError，且不留下状态文件', () async {
      final dir = _tempDir();
      final app = DrcomApp(
        config: AppConfig(),
        store: ConfigStore(path: '${dir.path}${Platform.pathSeparator}c.json'),
        statusPath: '${dir.path}${Platform.pathSeparator}s.json',
        logSink: (_) {},
      );
      await expectLater(app.start(), throwsA(isA<StateError>()));
      expect(File('${dir.path}${Platform.pathSeparator}s.json').existsSync(),
          isFalse);
    });

    test('账号没有密码时 start 抛 StateError', () async {
      final dir = _tempDir();
      final app = DrcomApp(
        config: AppConfig(accounts: [Account(account: 'u1')]),
        store: ConfigStore(path: '${dir.path}${Platform.pathSeparator}c.json'),
        statusPath: '${dir.path}${Platform.pathSeparator}s.json',
        logSink: (_) {},
      );
      await expectLater(app.start(), throwsA(isA<StateError>()));
    });

    test('DPAPI 能往返自己加密的密码', () {
      final stored = protectPassword('p@ssw0rd-中文');
      expect(stored, isNot(contains('p@ssw0rd')));
      expect(unprotectPassword(stored).plaintext, 'p@ssw0rd-中文');
    });

    test('解不开的密码字段报 SecretError 而不是静默返回空', () {
      expect(
        () => unprotectPassword('不是base64'),
        throwsA(isA<SecretError>()),
      );
    });
  });

  group('应用层：跑一轮真实装配', () {
    test('上线后状态文件报告 online，停止后不再 online', () async {
      final rig = _makeRig();
      final logs = <AppLogLine>[];
      final app = DrcomApp(
        config: rig.config,
        store: rig.store,
        statusPath: rig.statusPath,
        logSink: logs.add,
        transportFactory: () async => _FakeTransport(<Uint8List>[
          _challengeReply(),
          _loginOkReply(),
          ..._keepaliveReplies(),
        ]),
      );

      final running = app.start();
      final online = await _waitForStatus(
        rig.statusPath,
        (status) => status['online'] == true,
      );
      expect(online, isNotNull, reason: '应写出状态文件');
      expect(
        online!['online'],
        isTrue,
        reason: '状态=${jsonEncode(online)}\n日志=\n'
            '${logs.map((line) => line.format()).join('\n')}',
      );
      expect(online['state'], 'online');
      expect(online['ip'], '192.0.2.10');
      expect(online['app'], 'JLU-DrCOM-NG');
      expect(online['implementation'], 'dart');
      expect(online['server'], '10.100.61.3');
      expect(online['account'], 'te******01', reason: '账号必须脱敏');
      expect(online['account'].toString().contains('testuser01'), isFalse);

      app.stop();
      await running;

      final stopped = await _readStatus(rig.statusPath);
      expect(stopped, isNotNull);
      expect(stopped!['online'], isFalse);
      expect(logs.map((line) => line.text).join('\n'), contains('online'));
    });

    test('配置→引擎参数：bind_address 0.0.0.0 归一成 null，具体地址原样传', () {
      final account = Account(account: 'u1', passwordProtected: protectPassword('x'));
      final wildcard = AppConfig(accounts: [account]);
      expect(EngineSettings.fromConfig(wildcard, account).bindAddress, isNull);

      final fixed = AppConfig(accounts: [account]);
      fixed.auth.bindAddress = '172.18.123.63';
      expect(
        EngineSettings.fromConfig(fixed, account).bindAddress,
        '172.18.123.63',
      );
    });

    test('保活间隔与超时从配置换算成 Duration', () {
      final account = Account(account: 'u1');
      final config = AppConfig(accounts: [account]);
      config.auth.keepaliveInterval = 15.0;
      config.auth.timeoutMs = 2500;
      final settings = EngineSettings.fromConfig(config, account);
      expect(settings.keepaliveInterval, const Duration(seconds: 15));
      expect(settings.recvTimeout, const Duration(milliseconds: 2500));
    });

    test('重连参数一项不落地搬过去', () {
      final account = Account(account: 'u1');
      final config = AppConfig(accounts: [account]);
      config.reconnect
        ..enabled = false
        ..minDelay = 3.0
        ..maxDelay = 60.0
        ..factor = 1.5
        ..jitter = 0.1
        ..giveUpAfter = 4;
      final policy = EngineSettings.fromConfig(config, account).policy;
      expect(policy.enabled, isFalse);
      expect(policy.minDelay, const Duration(seconds: 3));
      expect(policy.maxDelay, const Duration(seconds: 60));
      expect(policy.factor, 1.5);
      expect(policy.jitter, 0.1);
      expect(policy.giveUpAfter, 4);
    });
  });
}
