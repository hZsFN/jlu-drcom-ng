import 'dart:io';

Future<void> main() async {
  final addresses = <String>[
    '172.16.2.17', // OrayBoxVpnEnt
    '172.29.240.1', // vEthernet (Default Switch)
    '10.66.220.103', // WLAN
    '172.18.123.63', // 以太网
  ];
  for (final address in addresses) {
    try {
      final socket = await RawDatagramSocket.bind(InternetAddress(address), 0);
      stdout.writeln('$address -> bind 成功，端口 ${socket.port}');
      socket.close();
    } catch (e) {
      stdout.writeln('$address -> bind 失败：$e');
    }
  }
}
