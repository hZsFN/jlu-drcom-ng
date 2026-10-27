import 'dart:convert';
import 'dart:io';

import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:test/test.dart';

/// 造一个临时目录，用完删掉。
Directory _tempDir() {
  final dir = Directory.systemTemp.createTempSync('drcom-config-test-');
  addTearDown(() {
    if (dir.existsSync()) {
      dir.deleteSync(recursive: true);
    }
  });
  return dir;
}

void main() {
  group('配置读取', () {
    test('文件不存在时返回带默认值的空配置，不抛异常', () {
      final dir = _tempDir();
      final store = ConfigStore(path: '${dir.path}${Platform.pathSeparator}config.json');
      expect(store.exists, isFalse);
      final config = store.load();
      expect(config.accounts, isEmpty);
      expect(config.auth.server, '10.100.61.3');
      expect(config.auth.port, 61440);
      expect(config.auth.timeoutMs, 3000);
      expect(config.auth.autoInterfaceFallback, isTrue);
    });

    test('空文件当作空配置', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      file.writeAsStringSync('   \n');
      final config = ConfigStore(path: file.path).load();
      expect(config.accounts, isEmpty);
    });

    test('坏 JSON 报 ConfigError，且错误信息指出路径', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      file.writeAsStringSync('{ 这不是 json');
      expect(
        () => ConfigStore(path: file.path).load(),
        throwsA(isA<ConfigError>().having((e) => e.message, 'message', contains('JSON'))),
      );
    });

    test('顶层不是对象也报 ConfigError', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      file.writeAsStringSync('[1, 2, 3]');
      expect(
        () => ConfigStore(path: file.path).load(),
        throwsA(isA<ConfigError>()),
      );
    });

    test('字段缺失时逐项回落到默认值，不整体失败', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      file.writeAsStringSync(jsonEncode({
        'version': 2,
        'accounts': [
          {'account': '20230001', 'password_protected': 'abc'},
        ],
        'auth': {'server': '10.0.0.1'},
      }));
      final config = ConfigStore(path: file.path).load();
      expect(config.accounts.single.account, '20230001');
      expect(config.accounts.single.id, isNotEmpty, reason: '缺 id 要自动补');
      expect(config.auth.server, '10.0.0.1');
      expect(config.auth.port, 61440, reason: '没写就回落到默认端口');
    });

    test('数字写成字符串也认', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      file.writeAsStringSync(jsonEncode({
        'auth': {'port': '61440', 'timeout_ms': '2500'},
      }));
      final config = ConfigStore(path: file.path).load();
      expect(config.auth.port, 61440);
      expect(config.auth.timeoutMs, 2500);
    });
  });

  group('配置保存', () {
    test('写回时保留 Dart 版不认识的段和键（round-trip 安全）', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      final original = {
        'version': 2,
        'accounts': [
          {'id': 'abc123', 'account': '20230001', 'mac': 'AA:BB:CC:DD:EE:FF'},
        ],
        'auth': {'server': '10.100.61.3', 'port': 61440, '未知键': '别弄丢我'},
        'logging': {'level': 'INFO', 'dir': 'C:/logs'},
        'notify': {'enabled': true},
        'probe': {'enabled': false},
        '未来新增段': {'a': 1},
      };
      file.writeAsStringSync(jsonEncode(original));

      final store = ConfigStore(path: file.path);
      final config = store.load();
      store.save(config);

      final written = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
      expect(written['logging'], original['logging'], reason: 'logging 段必须原样保留');
      expect(written['notify'], original['notify']);
      expect(written['probe'], original['probe']);
      expect(written['未来新增段'], original['未来新增段']);
      expect((written['auth'] as Map)['未知键'], '别弄丢我', reason: '段内的未知键也要保留');
    });

    test('改动是持久化的', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      final store = ConfigStore(path: file.path);
      final config = store.load();
      config.auth.bindAddress = '172.18.123.63';
      config.auth.keepaliveInterval = 15.0;
      store.save(config);

      final reloaded = ConfigStore(path: file.path).load();
      expect(reloaded.auth.bindAddress, '172.18.123.63');
      expect(reloaded.auth.keepaliveInterval, 15.0);
    });

    test('目录不存在会自己建', () {
      final dir = _tempDir();
      final nested = '${dir.path}${Platform.pathSeparator}a'
          '${Platform.pathSeparator}b${Platform.pathSeparator}config.json';
      final store = ConfigStore(path: nested);
      store.save(store.load());
      expect(File(nested).existsSync(), isTrue);
    });

    test('保存不留下 .tmp 临时文件', () {
      final dir = _tempDir();
      final file = File('${dir.path}${Platform.pathSeparator}config.json');
      final store = ConfigStore(path: file.path);
      store.save(store.load());
      final leftovers = dir
          .listSync()
          .whereType<File>()
          .where((f) => f.path.endsWith('.tmp'))
          .toList();
      expect(leftovers, isEmpty);
    });
  });

  group('账号选择', () {
    AppConfig withAccounts(List<Account> accounts) =>
        AppConfig(accounts: accounts);

    test('优先选 auto_login 且有密码的那个', () {
      final config = withAccounts([
        Account(account: 'u1', passwordProtected: ''),
        Account(account: 'u2', passwordProtected: 'x', autoLogin: true),
      ]);
      expect(config.activeAccount()!.account, 'u2');
    });

    test('auto_login 但没密码时跳过，退到有密码的', () {
      final config = withAccounts([
        Account(account: 'u1', passwordProtected: 'x'),
        Account(account: 'u2', passwordProtected: '', autoLogin: true),
      ]);
      expect(config.activeAccount()!.account, 'u1');
    });

    test('都没有密码时返回第一个', () {
      final config = withAccounts([
        Account(account: 'u1'),
        Account(account: 'u2'),
      ]);
      expect(config.activeAccount()!.account, 'u1');
    });

    test('没有账号时返回 null', () {
      expect(withAccounts([]).activeAccount(), isNull);
    });
  });

  group('账号脱敏', () {
    test('短账号全靠星号，不留原文', () {
      expect(Account(account: 'ab').maskedAccount, '**');
      expect(Account(account: 'a').maskedAccount, '*');
      expect(Account(account: '').maskedAccount, '(未设置)');
    });

    test('中等长度只留首字', () {
      expect(Account(account: 'abcd').maskedAccount, 'a***');
    });

    test('长账号留前二后二', () {
      final masked = Account(account: '2023000126').maskedAccount;
      expect(masked, '20******26');
      expect(masked.contains('230001'), isFalse);
    });

    test('有 label 时显示 label', () {
      final account = Account(account: '2023000126', label: '寝室');
      expect(account.displayName, '寝室');
    });
  });
}
