import 'dart:io';

import 'package:jlu_drcom/jlu_drcom.dart';

void main() {
  final store = ConfigStore();
  final status = store.path.replaceFirst(configFileName, statusFileName);
  print('APPDATA = ${Platform.environment['APPDATA']}');
  print('config  = ${store.path}');
  print('status  = $status');
  final f = File(status);
  print('exists  = ${f.existsSync()}');
  try {
    f.writeAsStringSync('{"probe":1}', flush: true);
    print('write   = OK');
  } catch (e) {
    print('write   = FAIL $e');
  }
}
