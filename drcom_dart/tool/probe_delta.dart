import 'dart:ffi';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';

typedef _GetIfTable2C = Int32 Function(Pointer<Pointer<Uint8>> table);
typedef _GetIfTable2Dart = int Function(Pointer<Pointer<Uint8>> table);
typedef _FreeMibTableC = Void Function(Pointer<Uint8> memory);
typedef _FreeMibTableDart = void Function(Pointer<Uint8> memory);

final lib = DynamicLibrary.open('iphlpapi.dll');
final getIfTable2 =
    lib.lookupFunction<_GetIfTable2C, _GetIfTable2Dart>('GetIfTable2');
final freeMibTable =
    lib.lookupFunction<_FreeMibTableC, _FreeMibTableDart>('FreeMibTable');

/// 读出"以太网"那一行的原始字节。
List<int> snapshot() {
  final holder = calloc<Pointer<Uint8>>();
  if (getIfTable2(holder) != 0) return <int>[];
  final table = holder.value;
  final count = table.cast<Uint32>().value;
  const rowSize = 1352;
  final bytes = table.asTypedList(8 + count * rowSize);
  final data = ByteData.sublistView(bytes);
  List<int> out = <int>[];
  for (var i = 0; i < count; i++) {
    final base = 8 + i * rowSize;
    final sb = StringBuffer();
    for (var k = 0; k < 64; k++) {
      final u = data.getUint16(base + 28 + k * 2, Endian.little);
      if (u == 0) break;
      sb.writeCharCode(u);
    }
    if (sb.toString() == '以太网') {
      out = bytes.sublist(base, base + rowSize);
      break;
    }
  }
  freeMibTable(table);
  calloc.free(holder);
  return out;
}

void main() async {
  final a = snapshot();
  await Future<void>.delayed(const Duration(seconds: 3));
  final b = snapshot();
  if (a.isEmpty || b.isEmpty) {
    print('没找到以太网');
    return;
  }
  final da = ByteData.sublistView(Uint8List.fromList(a));
  final db = ByteData.sublistView(Uint8List.fromList(b));
  print('扫描 1160..1344 的 8 字节对齐字段，找增量为正的：');
  for (var off = 1160; off <= 1344; off += 8) {
    final va = da.getUint64(off, Endian.little);
    final vb = db.getUint64(off, Endian.little);
    final d = vb - va;
    if (d > 0) {
      print('  偏移 $off : $va -> $vb  (Δ$d)');
    }
  }
}
