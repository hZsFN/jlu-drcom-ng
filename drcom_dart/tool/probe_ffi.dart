import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:jlu_drcom/src/ffi_net.dart';

void main() {
  final net = FfiNetwork.open();
  if (net == null) {
    print('FFI 不可用');
    return;
  }
  final rows = net.readCounters();
  print('接口数 = ${rows.length}');
  for (final row in rows) {
    print('  ${row.alias}  up=${row.operUp}  rx=${row.inOctets}  tx=${row.outOctets}');
  }
  final up = rows.where((r) => r.operUp && r.inOctets > 0).toList();
  if (up.isNotEmpty) {
    final target = up.first;
    print('对照 ${target.alias}: rx=${target.inOctets} tx=${target.outOctets}');
  }
  for (final ip in <String>['127.0.0.1', '172.18.123.254', '10.100.61.3']) {
    final r = net.ping(ip, timeoutMs: 1200);
    print('ping $ip -> ok=${r.ok} rtt=${r.roundTripMs}ms status=${r.status}');
  }
}
