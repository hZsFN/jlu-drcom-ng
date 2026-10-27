/// 一次性登录验证工具（真机 smoke test）。
///
/// 用法：凭据从 **stdin** 以 JSON 读入，绝不走命令行参数（进程列表里看得见）：
///
/// ```sh
/// echo '{"account":"...","password":"...","mac":"..."}' | dart run bin/login_once.dart
/// ```
///
/// 只做「挑战 → 登录」两步，不发 keepalive、不常驻、不写任何文件。
/// 退出码：0 成功；1 链路或认证失败；2 输入不合法。
library;

import 'dart:convert';
import 'dart:io';

import 'package:jlu_drcom/jlu_drcom.dart';

String _hex(Iterable<int> bytes) =>
    bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();

String _maskAccount(String account) {
  if (account.length <= 2) {
    return '*' * account.length;
  }
  if (account.length <= 5) {
    return '${account[0]}${'*' * (account.length - 1)}';
  }
  return '${account.substring(0, 2)}'
      '${'*' * (account.length - 4)}'
      '${account.substring(account.length - 2)}';
}

bool _flag(List<String> args, String short, String long) =>
    args.contains(short) || args.contains(long);

Future<void> main(List<String> args) async {
  final verbose = _flag(args, '-v', '--verbose');

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

  final account = (creds['account'] as String?)?.trim() ?? '';
  final password = (creds['password'] as String?) ?? '';
  final mac = (creds['mac'] as String?)?.trim() ?? '';
  if (account.isEmpty || password.isEmpty || mac.isEmpty) {
    stderr.writeln('凭据缺少 account / password / mac 之一');
    exit(2);
  }

  final transport = UdpAuthTransport(
    server: defaultAuthServer,
    port: defaultAuthPort,
  );

  var code = 1;
  try {
    stdout.writeln('账号 ${_maskAccount(account)}  →  ${defaultAuthServer}:${defaultAuthPort}');

    // ---- 1) 挑战 ------------------------------------------------------
    final challenge = buildChallengePacket();
    if (verbose) {
      stdout.writeln('→ 挑战包 ${challenge.length} 字节');
    }
    await transport.send(challenge);
    final challengeReply = await transport.receive();    if (challengeReply == null) {
      stderr.writeln('✗ 挑战包无响应（超时 ${recvTimeoutMs}ms）——检查是否在校园网内');
      code = 1;
      return;
    }
    final challengeData = parseChallengeResponse(challengeReply);
    if (verbose) {
      stdout.writeln(
        '← 挑战响应 ${challengeReply.length} 字节  '
        'seed=${_hex(challengeData.seed)}  服务端看到的 IP=${challengeData.ip}',
      );
    }

    // ---- 2) 登录 ------------------------------------------------------
    final loginPacket = buildLoginPacket(
      account: account,
      password: password,
      mac: mac,
      seed: challengeData.seed,
    );
    if (verbose) {
      stdout.writeln('→ 登录包 ${loginPacket.length} 字节（密码 ${password.length} 位）');
    }
    await transport.send(loginPacket);
    final loginReply = await transport.receive();
    if (loginReply == null) {
      stderr.writeln('✗ 登录包无响应（超时 ${recvTimeoutMs}ms）');
      code = 1;
      return;
    }
    final outcome = parseLoginResponse(loginReply);
    if (verbose) {
      stdout.writeln(
        '← 登录响应 ${loginReply.length} 字节  首字节 '
        '0x${loginReply[0].toRadixString(16).padLeft(2, '0')}',
      );
    }

    if (outcome.success) {
      stdout.writeln('✓ 登录成功');
      stdout.writeln('  auth_information = ${_hex(outcome.authInformation)}');
      stdout.writeln('  本端绑定端口 = ${transport.localPort}');
      code = 0;
    } else {
      final errCode = outcome.errorCode;
      stdout.writeln('✗ 登录失败');
      if (errCode != null) {
        stdout.writeln('  错误码 = 0x${errCode.toRadixString(16).padLeft(2, '0')}');
      }
      stdout.writeln('  ${outcome.message}');
      stdout.writeln('  ${outcome.advice}');
      code = 1;
    }
  } on ProtocolError catch (e) {
    stderr.writeln('✗ 协议错误：$e');
    code = 1;
  } on SocketException catch (e) {
    stderr.writeln('✗ 网络错误：$e');
    code = 1;
  } finally {
    await transport.close();
  }
  exit(code);
}
