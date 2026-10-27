import 'dart:ffi';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';

typedef _GetIfTable2C = Int32 Function(Pointer<Pointer<Uint8>> table);
typedef _GetIfTable2Dart = int Function(Pointer<Pointer<Uint8>> table);
typedef _FreeMibTableC = Void Function(Pointer<Uint8> memory);
typedef _FreeMibTableDart = void Function(Pointer<Uint8> memory);

void main() {
  final lib = DynamicLibrary.open('iphlpapi.dll');
  final getIfTable2 =
      lib.lookupFunction<_GetIfTable2C, _GetIfTable2Dart>('GetIfTable2');
  final freeMibTable =
      lib.lookupFunction<_FreeMibTableC, _FreeMibTableDart>('FreeMibTable');

  final holder = calloc<Pointer<Uint8>>();
  if (getIfTable2(holder) != 0) {
    print('GetIfTable2 失败');
    return;
  }
  final table = holder.value;
  final count = table.cast<Uint32>().value;
  const rowGuess = 1352;
  final total = 8 + count * rowGuess;
  final bytes = table.asTypedList(total);
  final data = ByteData.sublistView(bytes);
  print('NumEntries = $count, 扫描 $total 字节');

  const want = 6781561198;
  final target = <int>[];
  for (var k = 0; k < 8; k++) {
    target.add((want >> (8 * k)) & 0xFF);
  }
  final hits = <int>[];
  for (var i = 0; i < total - 8; i++) {
    var hit = true;
    for (var k = 0; k < 8; k++) {
      if (bytes[i + k] != target[k]) { hit = false; break; }
    }
    if (hit) hits.add(i);
  }
  print('6781561198 命中绝对偏移 = $hits');

  const chars = <int>[0x4EE5, 0x592A, 0x7F51];
  final nameHits = <int>[];
  for (var i = 0; i < total - 6; i += 2) {
    var hit = true;
    for (var k = 0; k < 3; k++) {
      if (data.getUint16(i + k * 2, Endian.little) != chars[k]) { hit = false; break; }
    }
    if (hit) nameHits.add(i);
  }
  print('"以太网" 命中绝对偏移 = $nameHits');
  for (final n in nameHits) {
    final base = (n - 8) ~/ rowGuess * rowGuess + 8;
    print('  若行起点=$base，则 alias 偏移=${n - base}');
  }
  for (final h in hits) {
    final base = (h - 8) ~/ rowGuess * rowGuess + 8;
    print('  若行起点=$base，则 inOctets 偏移=${h - base}');
  }
  freeMibTable(table);
  calloc.free(holder);
}
