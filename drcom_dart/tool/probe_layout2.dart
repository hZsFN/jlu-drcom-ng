import 'dart:ffi';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';

typedef _GetIfTable2C = Int32 Function(Pointer<Pointer<Uint8>> table);
typedef _GetIfTable2Dart = int Function(Pointer<Pointer<Uint8>> table);
typedef _FreeMibTableC = Void Function(Pointer<Uint8> memory);
typedef _FreeMibTableDart = void Function(Pointer<Uint8> memory);

void main(List<String> args) {
  final lib = DynamicLibrary.open('iphlpapi.dll');
  final getIfTable2 =
      lib.lookupFunction<_GetIfTable2C, _GetIfTable2Dart>('GetIfTable2');
  final freeMibTable =
      lib.lookupFunction<_FreeMibTableC, _FreeMibTableDart>('FreeMibTable');

  final holder = calloc<Pointer<Uint8>>();
  if (getIfTable2(holder) != 0) {
    print('失败');
    return;
  }
  final table = holder.value;
  final count = table.cast<Uint32>().value;
  const rowSize = 1352;
  final bytes = table.asTypedList(8 + count * rowSize);
  final data = ByteData.sublistView(bytes);

  // 找出名字里含"以太网"且在 1352 边界上的行
  for (var i = 0; i < count; i++) {
    final base = 8 + i * rowSize;
    var name = StringBuffer();
    for (var k = 0; k < 128; k++) {
      final u = data.getUint16(base + 28 + k * 2, Endian.little);
      if (u == 0) break;
      name.writeCharCode(u);
    }
    final n = name.toString();
    if (!n.contains('以太网')) continue;
    print('行 $i 名字="$n"');
    for (var off = 1150; off <= 1340; off += 8) {
      final v = data.getUint64(base + off, Endian.little);
      if (v > 1000000000 && v < 1000000000000) {
        print('   偏移 $off = $v');
      }
    }
  }
  freeMibTable(table);
  calloc.free(holder);
}
