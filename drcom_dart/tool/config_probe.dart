/// 配置探测：读真实配置，验证 Dart 版能不能解开其中的密码。
///
/// 只输出账号脱敏名、密码长度、用的哪个保护后端——**绝不打印密码本身**。
///
/// 用法：
///   dart run tool/config_probe.dart [--config=路径]
library;

import 'dart:io';

import 'package:jlu_drcom/jlu_drcom.dart';

void main(List<String> args) {
  var configPath = ConfigStore.defaultConfigPath();
  for (final arg in args) {
    if (arg.startsWith('--config=')) {
      configPath = arg.substring('--config='.length);
    }
  }

  final store = ConfigStore(path: configPath);
  stdout.writeln('配置文件：$configPath');
  stdout.writeln('存在：${store.exists}');

  final AppConfig config;
  try {
    config = store.load();
  } on ConfigError catch (error) {
    stderr.writeln('读取失败：${error.message}');
    exitCode = 2;
    return;
  }

  stdout.writeln('schema 版本：${config.version}');
  stdout.writeln('账号数：${config.accounts.length}');
  final active = config.activeAccount();
  stdout.writeln('当前账号：${active?.maskedAccount ?? '(无)'}');

  stdout.writeln('');
  stdout.writeln('线路参数：');
  stdout.writeln('  server=${config.auth.server}:${config.auth.port}');
  stdout.writeln('  bind_address=${config.auth.bindAddress}'
      ' bind_port=${config.auth.bindPort}');
  stdout.writeln('  timeout_ms=${config.auth.timeoutMs}'
      ' keepalive_interval=${config.auth.keepaliveInterval}');
  stdout.writeln('  auto_interface_fallback=${config.auth.autoInterfaceFallback}');

  stdout.writeln('');
  stdout.writeln('密码复算：');
  for (final account in config.accounts) {
    final masked = account.maskedAccount;
    if (!account.hasPassword) {
      stdout.writeln('  $masked：没有密码');
      continue;
    }
    try {
      final revealed = unprotectPassword(account.passwordProtected);
      // 只报长度，绝不报内容。
      stdout.writeln(
        '  $masked：解得 ${revealed.plaintext.length} 个字符'
        '（后端 ${revealed.backend}）',
      );
    } on SecretError catch (error) {
      stdout.writeln('  $masked：解不开 —— ${error.message}');
    }
  }
}
