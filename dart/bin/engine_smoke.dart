/// 引擎真机冒烟测试：真登录 + 若干轮保活后自动退出。
///
/// 与 `login_once.dart` 的区别：这个会真的跑进保活循环——keepalive1 那个
/// 「声明 38 实则 42 字节」的应答包、首轮的 file 包、A→C 的 tail 交换，
/// 全都是真机验证的对象（协议对拍只能证明跟 Python 实现一致，
/// 证明不了服务端认）。
///
/// ```sh
/// echo '{"account":"...","password":"...","mac":"..."}' \
///   | dart run bin/engine_smoke.dart --rounds=2
/// ```
///
/// 跑之前建议先关掉现有客户端，避免两边同时保活同一会话。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:jlu_drcom/jlu_drcom.dart';

int _argInt(List<String> args, String name, int fallback) {
  for (final arg in args) {
    if (arg.startsWith('--$name=')) {
      return int.tryParse(arg.substring(name.length + 3)) ?? fallback;
    }
  }
  return fallback;
}

String? _argStr(List<String> args, String name) {
  for (final arg in args) {
    if (arg.startsWith('--$name=')) {
      return arg.substring(name.length + 3);
    }
  }
  return null;
}

String _hex(Iterable<int> bytes) =>
    bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();

Future<void> main(List<String> args) async {
  final rounds = _argInt(args, 'rounds', 2);
  final timeoutSeconds = _argInt(args, 'timeout', 120);
  final bindAddress = _argStr(args, 'bind');

  final raw = await stdin.transform(utf8.decoder).join();
  Map<String, dynamic> creds;
  try {
    final decoded = jsonDecode(raw);
    if (decoded is! Map<String, dynamic>) {
      throw const FormatException('顶层不是对象');
    }
    creds = decoded;
  } on Object catch (e) {
    stderr.writeln('无法解析 stdin 的凭据 JSON：$e');
    exit(2);
  }

  final engine = AuthEngine(
    account: (creds['account'] as String?)?.trim() ?? '',
    password: (creds['password'] as String?) ?? '',
    mac: (creds['mac'] as String?)?.trim() ?? '',
    maxSessions: 1,
    transportFactory: bindAddress == null
        ? null
        : () async => UdpAuthTransport(
              server: defaultAuthServer,
              port: defaultAuthPort,
              localAddress: bindAddress,
            ),
  );
  if (engine.account.isEmpty || engine.password.isEmpty || engine.mac.isEmpty) {
    stderr.writeln('凭据缺少 account / password / mac 之一');
    exit(2);
  }

  var okRounds = 0;
  var failed = false;

  final sub = engine.events.listen((event) {
    switch (event.kind) {
      case EngineEventKind.started:
        stdout.writeln('· ${event.message}');
        if (bindAddress != null) {
          stdout.writeln('· 绑定地址 = $bindAddress（强制指定出口网卡）');
        }
      case EngineEventKind.ip:
        stdout.writeln('· ${event.message}');
      case EngineEventKind.online:
        stdout.writeln('✓ 已上线  ${event.message}');
      case EngineEventKind.keepaliveOk:
        okRounds += 1;
        stdout.writeln(
          '✓ 保活第 $okRounds 轮通过（在线 ${event.payload['uptimeSeconds']}s）',
        );
        if (okRounds >= rounds) {
          stdout.writeln('· 达到 $rounds 轮，主动停止');
          engine.stop();
        }
      case EngineEventKind.keepaliveFailed:
        failed = true;
        stdout.writeln('✗ ${event.message}  ${event.detail}');
      case EngineEventKind.loginFailed:
      case EngineEventKind.challengeFailed:
      case EngineEventKind.fatal:
      case EngineEventKind.error:
        failed = true;
        stdout.writeln('✗ ${event.message}  ${event.detail}');
      case EngineEventKind.retryScheduled:
        stdout.writeln('· ${event.message}');
      case EngineEventKind.stopped:
        stdout.writeln('· ${event.message}');
    }
  });

  final watchdog = Timer(Duration(seconds: timeoutSeconds), () {
    stdout.writeln('· 超过 ${timeoutSeconds}s 上限，主动停止');
    engine.stop();
  });

  await engine.run();
  watchdog.cancel();
  await sub.cancel();

  stdout.writeln('');
  stdout.writeln('结果：');
  stdout.writeln('  结束状态   = ${engine.state.name}');
  stdout.writeln('  掉线原因   = ${engine.reason?.name ?? '-'}');
  stdout.writeln('  保活轮数   = $okRounds / $rounds');
  if (engine.authInformation.isNotEmpty) {
    stdout.writeln('  auth_info  = ${_hex(engine.authInformation)}');
  }
  exit(failed || okRounds < rounds ? 1 : 0);
}
