import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:test/test.dart';

PingResult _round(String target, List<double> rtts, {int sent = 3}) => PingResult(
      target: target,
      sent: sent,
      received: rtts.length,
      rttMs: rtts.isEmpty
          ? null
          : rtts.reduce((double a, double b) => a + b) / rtts.length,
      timestamp: 0,
      rtts: rtts,
    );

void main() {
  group('isSafeTarget', () {
    test('普通 IP 与域名放行', () {
      expect(isSafeTarget('10.100.61.3'), isTrue);
      expect(isSafeTarget('www.jlu.edu.cn'), isTrue);
    });

    test('空串、以短横开头、含空格的一律拒绝', () {
      expect(isSafeTarget(''), isFalse);
      expect(isSafeTarget('-f'), isFalse);
      expect(isSafeTarget('1.1.1.1 -f'), isFalse);
    });
  });

  group('isHttpTarget', () {
    test('只认 http/https 前缀', () {
      expect(isHttpTarget('https://www.bing.com'), isTrue);
      expect(isHttpTarget('HTTP://x'), isTrue);
      expect(isHttpTarget('10.100.61.3'), isFalse);
      expect(isHttpTarget(''), isFalse);
    });
  });

  group('ProbeHistory', () {
    test('空目标给出全 null 的汇总', () {
      final ProbeHistory history = ProbeHistory();
      final Map<String, Object?> summary = history.summary('10.0.0.1');
      expect(summary['kind'], 'icmp');
      expect(summary['pings'], 0);
      expect(summary['rtt_ms'], isNull);
      expect(summary['loss_percent'], isNull);
      expect(summary['jitter_ms'], isNull);
    });

    test('均值 rtt 与零丢包', () {
      final ProbeHistory history = ProbeHistory();
      history.add(_round('t', <double>[10, 12, 14]));
      final Map<String, Object?> summary = history.summary('t');
      expect(summary['pings'], 1);
      expect(summary['samples'], 3);
      expect(summary['rtt_ms'], 12.0);
      expect(summary['loss_percent'], 0.0);
      // 相邻差 |12-10| + |14-12| = 4，除以 2 = 2
      expect(summary['jitter_ms'], 2.0);
    });

    test('部分丢包按轮累计', () {
      final ProbeHistory history = ProbeHistory();
      history.add(const PingResult(
          target: 't', sent: 3, received: 3, timestamp: 0, rtts: <double>[5, 5, 5]));
      history.add(const PingResult(
          target: 't', sent: 3, received: 1, timestamp: 0, rtts: <double>[5]));
      final Map<String, Object?> summary = history.summary('t');
      expect(summary['loss_percent'], closeTo(33.33, 0.1));
    });

    test('窗口上限会裁掉最老的轮次', () {
      final ProbeHistory history = ProbeHistory(window: 2);
      for (int i = 0; i < 5; i++) {
        history.add(_round('t', <double>[1, 2]));
      }
      expect(history.summary('t')['pings'], 2);
    });

    test('targets 保持加入顺序且 clear 能清空', () {
      final ProbeHistory history = ProbeHistory();
      history.add(_round('a', <double>[1, 1]));
      history.add(_round('b', <double>[1, 1]));
      expect(history.targets, <String>['a', 'b']);
      history.clear();
      expect(history.targets, isEmpty);
    });

    test('http 目标标成 http', () {
      final ProbeHistory history = ProbeHistory();
      expect(history.summary('https://x')['kind'], 'http');
    });
  });

  group('pingOnce', () {
    test('不安全的目标不发包，直接算全丢', () async {
      final PingResult result = await pingOnce('-f');
      expect(result.received, 0);
      expect(result.ok, isFalse);
      expect(result.rtts, isEmpty);
    });

    test('本机回环能测到（真实调用系统 ping）', () async {
      final PingResult result =
          await pingOnce('127.0.0.1', count: 2, timeoutMs: 1000);
      expect(result.sent, 2);
      // 有些环境禁 ICMP，测不到就跳过，不算失败。
      if (result.ok) {
        expect(result.rttMs, isNotNull);
        expect(result.rtts, isNotEmpty);
      }
    }, skip: false);
  });
}
