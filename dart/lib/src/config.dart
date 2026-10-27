/// 配置读写，字段与 Python 版 `drcom/config.py` 逐一对齐。
///
/// 关键约束：**写回时以磁盘上的原始 JSON 为底**，只覆盖 Dart 版认识的字段。
/// 这样 Dart 客户端和参考实现能共用同一份 `%APPDATA%\JLU-DrCOM-NG\config.json`
/// 而不会把对方新增的段（notify / probe / traffic / logging …）抹掉。
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'secrets.dart';

/// 配置目录名（`%APPDATA%` 下）。
const String appDataDirName = 'JLU-DrCOM-NG';

/// 配置文件名。
const String configFileName = 'config.json';

/// 读不出、解不开配置时抛这个。
class ConfigError implements Exception {
  ConfigError(this.message);

  final String message;

  @override
  String toString() => 'ConfigError: $message';
}

/// 一个账号。字段名与 Python 版 `Account` 保持一致（下划线命名）。
class Account {
  Account({
    this.account = '',
    this.mac = defaultMacPlaceholder,
    this.passwordProtected = '',
    this.label = '',
    this.autoLogin = false,
    String? id,
  }) : id = id ?? _randomId();

  static const String defaultMacPlaceholder = '00:00:00:00:00:00';

  final String id;
  String account;
  String mac;
  String passwordProtected;
  String label;
  bool autoLogin;

  /// 脱敏后的账号，可安全进日志。
  String get maskedAccount {
    if (account.length <= 2) {
      return account.isEmpty ? '(未设置)' : '*' * account.length;
    }
    if (account.length <= 5) {
      return account[0] + '*' * (account.length - 1);
    }
    return '${account.substring(0, 2)}'
        '${'*' * (account.length - 4)}'
        '${account.substring(account.length - 2)}';
  }

  String get displayName => label.isNotEmpty ? label : maskedAccount;

  bool get hasPassword => passwordProtected.isNotEmpty;

  /// 明文密码（DPAPI 解密）。调用方负责不要把它写出去。
  String revealPassword() => unprotectPassword(passwordProtected).plaintext;

  static String _randomId() {
    final random = Random.secure();
    return List<int>.generate(6, (_) => random.nextInt(256))
        .map((byte) => byte.toRadixString(16).padLeft(2, '0'))
        .join();
  }

  factory Account.fromJson(Map<String, dynamic> json) {
    return Account(
      account: '${json['account'] ?? ''}',
      mac: '${json['mac'] ?? defaultMacPlaceholder}',
      passwordProtected: '${json['password_protected'] ?? ''}',
      label: '${json['label'] ?? ''}',
      autoLogin: json['auto_login'] == true,
      id: json['id']?.toString(),
    );
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'id': id,
        'account': account,
        'mac': mac,
        'password_protected': passwordProtected,
        'label': label,
        'auto_login': autoLogin,
      };
}

/// 线路参数。
class AuthConfig {
  AuthConfig({
    this.server = '10.100.61.3',
    this.port = 61440,
    this.bindAddress = '0.0.0.0',
    this.bindPort = 61440,
    this.timeoutMs = 3000,
    this.keepaliveInterval = 20.0,
    this.challengeRetries = 3,
    this.postLoginDelay = 0.2,
    this.allowAlternatePort = false,
    this.autoInterfaceFallback = true,
  });

  String server;
  int port;
  String bindAddress;
  int bindPort;
  int timeoutMs;
  double keepaliveInterval;
  int challengeRetries;
  double postLoginDelay;
  bool allowAlternatePort;
  bool autoInterfaceFallback;

  factory AuthConfig.fromJson(Map<String, dynamic> json) {
    return AuthConfig(
      server: '${json['server'] ?? '10.100.61.3'}',
      port: _asInt(json['port'], 61440),
      bindAddress: '${json['bind_address'] ?? '0.0.0.0'}',
      bindPort: _asInt(json['bind_port'], 61440),
      timeoutMs: _asInt(json['timeout_ms'], 3000),
      keepaliveInterval: _asDouble(json['keepalive_interval'], 20.0),
      challengeRetries: _asInt(json['challenge_retries'], 3),
      postLoginDelay: _asDouble(json['post_login_delay'], 0.2),
      allowAlternatePort: json['allow_alternate_port'] == true,
      autoInterfaceFallback: json['auto_interface_fallback'] != false,
    );
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'server': server,
        'port': port,
        'bind_address': bindAddress,
        'bind_port': bindPort,
        'timeout_ms': timeoutMs,
        'keepalive_interval': keepaliveInterval,
        'challenge_retries': challengeRetries,
        'post_login_delay': postLoginDelay,
        'allow_alternate_port': allowAlternatePort,
        'auto_interface_fallback': autoInterfaceFallback,
      };
}

/// 重连节流。
class ReconnectConfig {
  ReconnectConfig({
    this.enabled = true,
    this.minDelay = 5.0,
    this.maxDelay = 300.0,
    this.factor = 2.0,
    this.jitter = 0.25,
    this.giveUpAfter = 8,
  });

  bool enabled;
  double minDelay;
  double maxDelay;
  double factor;
  double jitter;
  int giveUpAfter;

  factory ReconnectConfig.fromJson(Map<String, dynamic> json) {
    return ReconnectConfig(
      enabled: json['enabled'] != false,
      minDelay: _asDouble(json['min_delay'], 5.0),
      maxDelay: _asDouble(json['max_delay'], 300.0),
      factor: _asDouble(json['factor'], 2.0),
      jitter: _asDouble(json['jitter'], 0.25),
      giveUpAfter: _asInt(json['give_up_after'], 8),
    );
  }
}

/// 界面行为（Dart 版只用得上一部分，其余原样保留给参考实现）。
class UiConfig {
  UiConfig({
    this.minimizeToTray = true,
    this.closeAction = 'ask',
    this.startMinimized = false,
    this.autostart = false,
    this.autoLoginOnLaunch = true,
    this.notifications = true,
  });

  bool minimizeToTray;
  String closeAction;
  bool startMinimized;
  bool autostart;
  bool autoLoginOnLaunch;
  bool notifications;

  factory UiConfig.fromJson(Map<String, dynamic> json) {
    return UiConfig(
      minimizeToTray: json['minimize_to_tray'] != false,
      closeAction: '${json['close_action'] ?? 'ask'}',
      startMinimized: json['start_minimized'] == true,
      autostart: json['autostart'] == true,
      autoLoginOnLaunch: json['auto_login_on_launch'] != false,
      notifications: json['notifications'] != false,
    );
  }
}

/// 整份配置。[raw] 是磁盘原文，保存时以它为底再覆盖已知字段。
class AppConfig {
  AppConfig({
    this.version = 2,
    List<Account>? accounts,
    AuthConfig? auth,
    ReconnectConfig? reconnect,
    UiConfig? ui,
    Map<String, dynamic>? raw,
  })  : accounts = accounts ?? <Account>[],
        auth = auth ?? AuthConfig(),
        reconnect = reconnect ?? ReconnectConfig(),
        ui = ui ?? UiConfig(),
        _raw = raw ?? <String, dynamic>{};

  int version;
  List<Account> accounts;
  AuthConfig auth;
  ReconnectConfig reconnect;
  UiConfig ui;

  Map<String, dynamic> _raw;

  /// 优先返回 `auto_login` 的账号，否则第一个。
  Account? activeAccount() {
    if (accounts.isEmpty) {
      return null;
    }
    for (final item in accounts) {
      if (item.autoLogin && item.hasPassword) {
        return item;
      }
    }
    for (final item in accounts) {
      if (item.hasPassword) {
        return item;
      }
    }
    return accounts.first;
  }

  factory AppConfig.fromJson(Map<String, dynamic> json) {
    final accountsJson = json['accounts'];
    final accounts = <Account>[];
    if (accountsJson is List) {
      for (final item in accountsJson) {
        if (item is Map) {
          accounts.add(Account.fromJson(item.cast<String, dynamic>()));
        }
      }
    }
    return AppConfig(
      version: _asInt(json['version'], 2),
      accounts: accounts,
      auth: AuthConfig.fromJson(_section(json['auth'])),
      reconnect: ReconnectConfig.fromJson(_section(json['reconnect'])),
      ui: UiConfig.fromJson(_section(json['ui'])),
      raw: Map<String, dynamic>.from(json),
    );
  }

  Map<String, dynamic> toJson() {
    final result = Map<String, dynamic>.from(_raw);
    result['version'] = version;
    result['accounts'] = accounts.map((account) => account.toJson()).toList();
    // 各段也做合并，别把 Dart 版不认识的键吃掉。
    result['auth'] = <String, dynamic>{
      ..._section(_raw['auth']),
      ...auth.toJson(),
    };
    result['reconnect'] = <String, dynamic>{
      ..._section(_raw['reconnect']),
      ...<String, dynamic>{
        'enabled': reconnect.enabled,
        'min_delay': reconnect.minDelay,
        'max_delay': reconnect.maxDelay,
        'factor': reconnect.factor,
        'jitter': reconnect.jitter,
        'give_up_after': reconnect.giveUpAfter,
      },
    };
    result['ui'] = <String, dynamic>{
      ..._section(_raw['ui']),
      ...<String, dynamic>{
        'minimize_to_tray': ui.minimizeToTray,
        'close_action': ui.closeAction,
        'start_minimized': ui.startMinimized,
        'autostart': ui.autostart,
        'auto_login_on_launch': ui.autoLoginOnLaunch,
        'notifications': ui.notifications,
      },
    };
    return result;
  }
}

/// 配置文件句柄。
class ConfigStore {
  ConfigStore({String? path}) : path = path ?? defaultConfigPath();

  /// 配置文件绝对路径。
  final String path;

  /// `%APPDATA%\JLU-DrCOM-NG\config.json`。
  static String defaultConfigPath() {
    final appData = Platform.environment['APPDATA'];
    if (appData == null || appData.isEmpty) {
      // 非 Windows / 环境变量缺失时退化到用户目录，主要是为了测试能跑。
      final home = Platform.environment['HOME'] ?? '.';
      return '$home${Platform.pathSeparator}$appDataDirName'
          '${Platform.pathSeparator}$configFileName';
    }
    return '$appData${Platform.pathSeparator}$appDataDirName'
        '${Platform.pathSeparator}$configFileName';
  }

  /// 文件是否存在。
  bool get exists => File(path).existsSync();

  /// 读配置。文件不存在时返回一份带默认值的空配置（不报错）。
  AppConfig load() {
    final file = File(path);
    if (!file.existsSync()) {
      return AppConfig();
    }
    String text;
    try {
      text = file.readAsStringSync();
    } on FileSystemException catch (error) {
      throw ConfigError('读不了配置文件：${error.message}');
    }
    if (text.trim().isEmpty) {
      return AppConfig();
    }
    Object? decoded;
    try {
      decoded = jsonDecode(text);
    } on FormatException catch (error) {
      throw ConfigError('配置文件不是合法 JSON（${error.message}）：$path');
    }
    if (decoded is! Map) {
      throw ConfigError('配置文件顶层必须是对象：$path');
    }
    return AppConfig.fromJson(decoded.cast<String, dynamic>());
  }

  /// 写配置（先写临时文件再改名，避免写一半崩掉把配置弄坏）。
  void save(AppConfig config) {
    final file = File(path);
    final directory = file.parent;
    if (!directory.existsSync()) {
      directory.createSync(recursive: true);
    }
    final text = const JsonEncoder.withIndent('  ').convert(config.toJson());
    final temporary = File('${file.path}.tmp');
    temporary.writeAsStringSync('$text\n', flush: true);
    temporary.renameSync(file.path);
  }
}

// -- 小工具 -----------------------------------------------------------------

Map<String, dynamic> _section(Object? value) {
  if (value is Map) {
    return value.cast<String, dynamic>();
  }
  return <String, dynamic>{};
}

int _asInt(Object? value, int fallback) {
  if (value is int) {
    return value;
  }
  if (value is num) {
    return value.toInt();
  }
  if (value is String) {
    return int.tryParse(value) ?? fallback;
  }
  return fallback;
}

double _asDouble(Object? value, double fallback) {
  if (value is num) {
    return value.toDouble();
  }
  if (value is String) {
    return double.tryParse(value) ?? fallback;
  }
  return fallback;
}
