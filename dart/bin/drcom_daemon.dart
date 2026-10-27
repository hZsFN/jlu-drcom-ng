/// 无头守护进程入口：读真实配置、登录、保活、写状态文件。
///
/// 用法：
///   dart run bin/drcom_daemon.dart
///   dart run bin/drcom_daemon.dart --config=C:\path\config.json
///   dart run bin/drcom_daemon.dart --status=C:\path\status.json
///
/// Ctrl+C 优雅退出（会把状态文件刷成 stopped 再走）。
library;

import 'dart:async';
import 'dart:io';

import 'package:jlu_drcom/jlu_drcom.dart';

Future<void> main(List<String> args) async {
  var configPath = ConfigStore.defaultConfigPath();
  String? statusPath;
  for (final arg in args) {
    if (arg.startsWith('--config=')) {
      configPath = arg.substring('--config='.length);
    } else if (arg.startsWith('--status=')) {
      statusPath = arg.substring('--status='.length);
    } else if (arg == '--help' || arg == '-h') {
      stdout.writeln('用法：dart run bin/drcom_daemon.dart '
          '[--config=路径] [--status=路径]');
      return;
    }
  }

  final store = ConfigStore(path: configPath);
  final AppConfig config;
  try {
    config = store.load();
  } on ConfigError catch (error) {
    stderr.writeln('读配置失败：${error.message}');
    exitCode = 2;
    return;
  }
  if (config.accounts.isEmpty) {
    stderr.writeln('配置里还没有账号：$configPath');
    exitCode = 2;
    return;
  }

  final app = DrcomApp(config: config, store: store, statusPath: statusPath);

  // Ctrl+C 优雅退出；run() 的 finally 会写最后一版状态文件。
  final signalSubscriptions = <StreamSubscription<ProcessSignal>>[
    ProcessSignal.sigint.watch().listen((_) {
      stdout.writeln('收到 Ctrl+C，正在停止…');
      app.stop();
    }),
  ];
  // SIGTERM 在 Windows 上压根不支持（listen 会抛 errno 50），只在类 Unix 上挂。
  if (!Platform.isWindows) {
    signalSubscriptions.add(
      ProcessSignal.sigterm.watch().listen((_) => app.stop()),
    );
  }

  try {
    await app.start();
  } on StateError catch (error) {
    stderr.writeln(error.message);
    exitCode = 2;
  } on SecretError catch (error) {
    stderr.writeln('密码解不开：${error.message}');
    exitCode = 2;
  } finally {
    for (final subscription in signalSubscriptions) {
      await subscription.cancel();
    }
  }
}
