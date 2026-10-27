/// MD4（RFC 1320）的 Dart 实现。
///
/// 为什么不用现成包：`package:crypto` 只提供 MD5/SHA，没有 MD4；而 DrCOM 的
/// keepalive CRC 在 `encrypt_type == 2` 时用的正是 MD4。这里按 RFC 1320 写
/// 一份够用的实现，正确性由 `dart/test/protocol_test.dart` 用标准测试向量
/// 和 Python 参考实现的输出双重验证。
library;

import 'dart:typed_data';

const int _mask32 = 0xFFFFFFFF;

int _rotl(int x, int s) => ((x << s) | (x >>> (32 - s))) & _mask32;

int _f(int x, int y, int z) => (x & y) | ((~x & _mask32) & z);
int _g(int x, int y, int z) => (x & y) | (x & z) | (y & z);
int _h(int x, int y, int z) => (x ^ y ^ z) & _mask32;

/// 计算 MD4 摘要，返回 16 字节。
Uint8List md4(List<int> input) {
  final byteLen = input.length;
  final bitLen = byteLen * 8;

  // 补齐：0x80，补零到 len % 64 == 56，再写 8 字节小端长度
  final padded = ((byteLen + 8) ~/ 64 + 1) * 64;
  final buf = Uint8List(padded);
  buf.setRange(0, byteLen, input);
  buf[byteLen] = 0x80;
  for (var i = 0; i < 8; i++) {
    buf[padded - 8 + i] = (bitLen >>> (8 * i)) & 0xFF;
  }

  var a = 0x67452301;
  var b = 0xEFCDAB89;
  var c = 0x98BADCFE;
  var d = 0x10325476;

  for (var off = 0; off < padded; off += 64) {
    final x = List<int>.generate(16, (i) {
      final o = off + i * 4;
      return buf[o] | (buf[o + 1] << 8) | (buf[o + 2] << 16) | (buf[o + 3] << 24);
    }, growable: false);

    final aa = a, bb = b, cc = c, dd = d;

    // ---- round 1：轮换 s = 3,7,11,19 ------------------------------
    a = _rotl((a + _f(b, c, d) + x[0]) & _mask32, 3);
    d = _rotl((d + _f(a, b, c) + x[1]) & _mask32, 7);
    c = _rotl((c + _f(d, a, b) + x[2]) & _mask32, 11);
    b = _rotl((b + _f(c, d, a) + x[3]) & _mask32, 19);
    a = _rotl((a + _f(b, c, d) + x[4]) & _mask32, 3);
    d = _rotl((d + _f(a, b, c) + x[5]) & _mask32, 7);
    c = _rotl((c + _f(d, a, b) + x[6]) & _mask32, 11);
    b = _rotl((b + _f(c, d, a) + x[7]) & _mask32, 19);
    a = _rotl((a + _f(b, c, d) + x[8]) & _mask32, 3);
    d = _rotl((d + _f(a, b, c) + x[9]) & _mask32, 7);
    c = _rotl((c + _f(d, a, b) + x[10]) & _mask32, 11);
    b = _rotl((b + _f(c, d, a) + x[11]) & _mask32, 19);
    a = _rotl((a + _f(b, c, d) + x[12]) & _mask32, 3);
    d = _rotl((d + _f(a, b, c) + x[13]) & _mask32, 7);
    c = _rotl((c + _f(d, a, b) + x[14]) & _mask32, 11);
    b = _rotl((b + _f(c, d, a) + x[15]) & _mask32, 19);

    // ---- round 2：s = 3,5,9,13，常数 0x5A827999 --------------------
    const k2 = 0x5A827999;
    a = _rotl((a + _g(b, c, d) + x[0] + k2) & _mask32, 3);
    d = _rotl((d + _g(a, b, c) + x[4] + k2) & _mask32, 5);
    c = _rotl((c + _g(d, a, b) + x[8] + k2) & _mask32, 9);
    b = _rotl((b + _g(c, d, a) + x[12] + k2) & _mask32, 13);
    a = _rotl((a + _g(b, c, d) + x[1] + k2) & _mask32, 3);
    d = _rotl((d + _g(a, b, c) + x[5] + k2) & _mask32, 5);
    c = _rotl((c + _g(d, a, b) + x[9] + k2) & _mask32, 9);
    b = _rotl((b + _g(c, d, a) + x[13] + k2) & _mask32, 13);
    a = _rotl((a + _g(b, c, d) + x[2] + k2) & _mask32, 3);
    d = _rotl((d + _g(a, b, c) + x[6] + k2) & _mask32, 5);
    c = _rotl((c + _g(d, a, b) + x[10] + k2) & _mask32, 9);
    b = _rotl((b + _g(c, d, a) + x[14] + k2) & _mask32, 13);
    a = _rotl((a + _g(b, c, d) + x[3] + k2) & _mask32, 3);
    d = _rotl((d + _g(a, b, c) + x[7] + k2) & _mask32, 5);
    c = _rotl((c + _g(d, a, b) + x[11] + k2) & _mask32, 9);
    b = _rotl((b + _g(c, d, a) + x[15] + k2) & _mask32, 13);

    // ---- round 3：s = 3,9,11,15，常数 0x6ED9EBA1 -------------------
    const k3 = 0x6ED9EBA1;
    a = _rotl((a + _h(b, c, d) + x[0] + k3) & _mask32, 3);
    d = _rotl((d + _h(a, b, c) + x[8] + k3) & _mask32, 9);
    c = _rotl((c + _h(d, a, b) + x[4] + k3) & _mask32, 11);
    b = _rotl((b + _h(c, d, a) + x[12] + k3) & _mask32, 15);
    a = _rotl((a + _h(b, c, d) + x[2] + k3) & _mask32, 3);
    d = _rotl((d + _h(a, b, c) + x[10] + k3) & _mask32, 9);
    c = _rotl((c + _h(d, a, b) + x[6] + k3) & _mask32, 11);
    b = _rotl((b + _h(c, d, a) + x[14] + k3) & _mask32, 15);
    a = _rotl((a + _h(b, c, d) + x[1] + k3) & _mask32, 3);
    d = _rotl((d + _h(a, b, c) + x[9] + k3) & _mask32, 9);
    c = _rotl((c + _h(d, a, b) + x[5] + k3) & _mask32, 11);
    b = _rotl((b + _h(c, d, a) + x[13] + k3) & _mask32, 15);
    a = _rotl((a + _h(b, c, d) + x[3] + k3) & _mask32, 3);
    d = _rotl((d + _h(a, b, c) + x[11] + k3) & _mask32, 9);
    c = _rotl((c + _h(d, a, b) + x[7] + k3) & _mask32, 11);
    b = _rotl((b + _h(c, d, a) + x[15] + k3) & _mask32, 15);

    a = (a + aa) & _mask32;
    b = (b + bb) & _mask32;
    c = (c + cc) & _mask32;
    d = (d + dd) & _mask32;
  }

  final out = Uint8List(16);
  final words = <int>[a, b, c, d];
  for (var i = 0; i < 4; i++) {
    final w = words[i];
    out[i * 4] = w & 0xFF;
    out[i * 4 + 1] = (w >>> 8) & 0xFF;
    out[i * 4 + 2] = (w >>> 16) & 0xFF;
    out[i * 4 + 3] = (w >>> 24) & 0xFF;
  }
  return out;
}
