import 'dart:io';

/// 开机自启：写当前用户的 Run 键，不碰 HKLM，不需要管理员权限。
///
/// 值名固定，值内容是本进程的 exe 全路径（带引号，路径含空格也不会断）。
class Autostart {
  const Autostart._();

  static const String _key =
      r'HKCU\Software\Microsoft\Windows\CurrentVersion\Run';
  static const String valueName = 'JLU DrCOM-NG';

  /// 只在 Windows 上可用，别的平台一律当不支持。
  static bool get isSupported => Platform.isWindows;

  static Future<bool> isEnabled() async {
    if (!isSupported) return false;
    final result = await Process.run('reg', <String>[
      'query',
      _key,
      '/v',
      valueName,
    ]);
    return result.exitCode == 0;
  }

  static Future<bool> enable() async {
    if (!isSupported) return false;
    final exe = Platform.resolvedExecutable;
    final result = await Process.run('reg', <String>[
      'add',
      _key,
      '/v',
      valueName,
      '/t',
      'REG_SZ',
      '/d',
      '"$exe"',
      '/f',
    ]);
    return result.exitCode == 0;
  }

  static Future<bool> disable() async {
    if (!isSupported) return false;
    final result = await Process.run('reg', <String>[
      'delete',
      _key,
      '/v',
      valueName,
      '/f',
    ]);
    // 本来就没有也算关成功。
    return result.exitCode == 0;
  }

  static Future<bool> setEnabled(bool on) => on ? enable() : disable();
}
