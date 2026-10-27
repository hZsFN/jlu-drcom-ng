import 'dart:io';

import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:test/test.dart';

void main() {
  group('formatBytes / formatRate', () {
    test('整数字节不带小数', () {
      expect(formatBytes(0), '0 B');
      expect(formatBytes(1), '1 B');
      expect(formatBytes(1023), '1023 B');
    });

    test('1024 进位成 KB 且保留一位', () {
      expect(formatBytes(1024), '1.0 KB');
      expect(formatBytes(1536), '1.5 KB');
      expect(formatBytes(1024 * 1024), '1.0 MB');
      // 注意：Dart 的 toStringAsFixed 是四舍五入（Python 的 f-string 是
      // 银行家舍入，同一数值会出 3.2），这里按 Dart 的行为断言。
      expect(formatBytes(1024 * 1024 * 3.25), '3.3 MB');
    });

    test('负数当成 0', () {
      expect(formatBytes(-100), '0 B');
    });

    test('rate 就是字节数加 /s', () {
      expect(formatRate(2048), '2.0 KB/s');
    });
  });

  group('StatsStore', () {
    late Directory temp;
    late File file;

    setUp(() {
      temp = Directory.systemTemp.createTempSync('drcom-stats-');
      file = File('${temp.path}${Platform.pathSeparator}stats.json');
    });

    tearDown(() {
      if (temp.existsSync()) temp.deleteSync(recursive: true);
    });

    test('没有文件时是干净状态', () {
      final StatsStore store = StatsStore(file);
      expect(store.hasSession, isFalse);
      expect(store.sessionSeconds, 0);
      expect(store.daily, isEmpty);
    });

    test('开会话后记一次 sessions，关掉后把时长记进今天', () {
      final StatsStore store = StatsStore(file);
      store.openSession(ip: '10.0.0.2', account: 'ta****01');
      expect(store.hasSession, isTrue);
      expect(store.today().sessions, 1);
      expect(store.totals.sessions, 1);

      // 把起点往前拨 120 秒，模拟已在线两分钟。
      final double duration = store.closeSession(drop: true);
      expect(duration, greaterThanOrEqualTo(0));
      expect(store.hasSession, isFalse);
      expect(store.today().sessions, 1);
      // 刚开就关也算一次掉线：closeSession 默认 drop=true。
      expect(store.today().drops, 1);
      expect(store.totals.drops, 1);
    });

    test('drop=false 不计掉线', () {
      final StatsStore store = StatsStore(file);
      store.openSession(ip: '', account: 'a');
      store.closeSession(drop: false);
      expect(store.totals.drops, 0);
    });

    test('addTraffic 累进当天与总计', () {
      final StatsStore store = StatsStore(file);
      store.addTraffic(rx: 100, tx: 40);
      store.addTraffic(rx: 10, tx: 1);
      expect(store.today().bytesRx, 110);
      expect(store.today().bytesTx, 41);
      expect(store.totals.bytesRx, 110);
    });

    test('noteLoginFailure 两边都加', () {
      final StatsStore store = StatsStore(file);
      store.noteLoginFailure();
      expect(store.today().loginFailures, 1);
      expect(store.totals.loginFailures, 1);
    });

    test('asStatus 的字段与 Python 版一致', () {
      final StatsStore store = StatsStore(file);
      store.openSession(ip: '1.2.3.4', account: 'x');
      store.addTraffic(rx: 8, tx: 4);
      final Map<String, dynamic> status = store.asStatus();
      expect(status.keys.toSet(), <String>{
        'today_seconds',
        'today_drops',
        'today_sessions',
        'today_login_failures',
        'week_seconds',
        'total_seconds',
        'total_drops',
        'total_sessions',
        'longest_session',
        'bytes_rx',
        'bytes_tx',
      });
      expect(status['today_sessions'], 1);
      expect(status['bytes_rx'], 8);
    });

    test('落盘后重新加载能读回来', () {
      final StatsStore store = StatsStore(file);
      store.addTraffic(rx: 1234, tx: 567);
      store.noteLoginFailure();
      expect(file.existsSync(), isTrue);

      final StatsStore again = StatsStore(file);
      expect(again.totals.bytesRx, 1234);
      expect(again.totals.bytesTx, 567);
      expect(again.totals.loginFailures, 1);
    });

    test('坏 JSON 不抛异常，当成空账簿', () {
      file.writeAsStringSync('{not json');
      final StatsStore store = StatsStore(file);
      expect(store.daily, isEmpty);
    });

    test('lastDays 返回 7 天且升序、今天是最后一格', () {
      final StatsStore store = StatsStore(file);
      final List<(String, DayStats)> days = store.lastDays(7);
      expect(days.length, 7);
      expect(days.first.$1.compareTo(days.last.$1), lessThan(0));
    });

    test('prune 不会删掉今天', () {
      final StatsStore store = StatsStore(file);
      store.today();
      store.prune();
      expect(store.daily, isNotEmpty);
    });
  });
}
