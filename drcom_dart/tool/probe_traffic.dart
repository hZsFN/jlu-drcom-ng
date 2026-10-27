import 'package:jlu_drcom/jlu_drcom.dart';

/// 手工验证 TrafficMeter：绑到给定地址所在的网卡，连采几次看速率。
///
/// 用法：`dart run tool/probe_traffic.dart 172.18.123.63`
Future<void> main(List<String> args) async {
  final String address = args.isEmpty ? '' : args.first;
  final TrafficMeter meter = TrafficMeter(log: (String m) => print('[log] $m'));
  if (address.isNotEmpty) {
    await meter.setSourceAddress(address);
  }
  for (int i = 0; i < 3; i++) {
    await Future<void>.delayed(const Duration(seconds: 2));
    final TrafficSnapshot snap = await meter.sample();
    print('第 ${i + 1} 次: 网卡="${snap.interfaceName}" '
        'total=${snap.totalRx}/${snap.totalTx} '
        'rate=${formatRate(snap.rxRate)} ↓ ${formatRate(snap.txRate)} ↑ '
        'label=${snap.totalLabel}');
  }
}
