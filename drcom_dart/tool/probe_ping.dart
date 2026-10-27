import 'package:jlu_drcom/src/ffi_net.dart';

void main() {
  final net = FfiNetwork.open();
  if (net == null) {
    print('FFI 不可用');
    return;
  }
  for (final ip in <String>['127.0.0.1', '172.18.123.63', '172.18.123.254']) {
    for (var i = 0; i < 2; i++) {
      final r = net.ping(ip, timeoutMs: 1500);
      print('$ip  ok=${r.ok} rtt=${r.roundTripMs} status=${r.status}');
    }
  }
}
