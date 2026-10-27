/// JLU DrCOM-NG 协议实现（Dart 版）。
///
/// 这是 `drcom/protocol.py` 的逐字节等价移植。任何一行都要能和 Python 参考
/// 实现对得上——`dart/test/protocol_test.dart` 会用
/// `dart/test/golden/protocol_vectors.json`（由 `dart/tool/gen_golden.py`
/// 从 Python 生成）做硬断言。改动这里之前先看那份向量。
///
/// 参考：DrCOM 协议规程 4.2（挑战包）、4.3/4.7（登录包）、4.6（摘要）、
/// 4.8（心跳）。
library;

import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:crypto/crypto.dart' as crypto;

import 'md4.dart';

// ---------------------------------------------------------------------------
// 常量（与 drcom/protocol.py 一一对应）
// ---------------------------------------------------------------------------

/// 认证服务器地址。生产配置里由 Config 覆盖，这里只作为默认值。
const String defaultAuthServer = '10.100.61.3';
const int defaultAuthPort = 61440;
const int defaultBindPort = 61440;
const int recvTimeoutMs = 3000;
const double keepaliveIntervalSeconds = 20.0;

/// 密码长度 ≤8 时的登录包长度。
const int loginBaseLen = 338;

/// 变长密码段的起始偏移。
const int pwSectionOffset = 312;

/// 客户端固定发送的 40 字节服务包标识（ASCII）。
final Uint8List servicePack = Uint8List.fromList(utf8.encode('3dc79f5212e8170acfa9ec95f1d74916542be7b1'));

/// 固定主机名块：`b"DrCOM"` + 三个固定字节。
final Uint8List hostnameBlob = Uint8List.fromList(<int>[0x44, 0x72, 0x43, 0x4F, 0x4D, 0x00, 0xCF, 0x07, 0x68]);

/// 第一个 keepalive-1 包是常量。
final Uint8List keepalive1Packet1 = Uint8List.fromList(<int>[0x07, 0x01, 0x08, 0x00, 0x01, 0x00, 0x00, 0x00]);

/// keepalive-2 首轮的“文件包”魔数。
final Uint8List keepaliveVersionFile = Uint8List.fromList(<int>[0x0F, 0x27]);

/// 普通 keepalive-2 包的版本字节。
final Uint8List keepaliveVersionNormal = Uint8List.fromList(<int>[0xDC, 0x02]);

/// keepalive-2 包长度（固定）。
const int keepalive2Len = 40;

/// keepalive-1 第二个包长度：42，**不是** 38。参考实现声明 38 字节数组但
/// 实际发送 42，服务端接受 42。
const int keepalive1Packet2Len = 42;

/// 参考实现真正定义过的密码长度上界。
const int referenceMaxPasswordLen = 16;

// ---------------------------------------------------------------------------
// 错误
// ---------------------------------------------------------------------------

/// 收到的包不符合预期形状。
class ProtocolError implements Exception {
  ProtocolError(this.message);
  final String message;
  @override
  String toString() => 'ProtocolError: $message';
}

/// 登录失败码——服务端在响应 [4] 返回，前提是 [0]==0x05。
enum LoginErrorCode {
  macCheckFailed(0x01),
  serverBusy(0x02),
  wrongPassword(0x03),
  insufficientBalance(0x04),
  accountFrozen(0x05),
  ipMismatch(0x07),
  macMismatch(0x0B),
  tooManyIps(0x14),
  clientUpgradeRequired(0x15),
  ipAndMacMismatch(0x16),
  mustUseDhcp(0x17);

  const LoginErrorCode(this.code);
  final int code;

  static LoginErrorCode? fromCode(int code) {
    for (final v in LoginErrorCode.values) {
      if (v.code == code) return v;
    }
    return null;
  }
}

// ---------------------------------------------------------------------------
// 基础摘要 / 解析
// ---------------------------------------------------------------------------

/// 把 `aa:bb:cc:dd:ee:ff`（或 `aa-bb-...`）解析成 6 字节。
Uint8List macToBytes(String mac) {
  final cleaned = mac.trim().replaceAll('-', ':').replaceAll('.', ':');
  final parts = cleaned.split(':').where((p) => p.isNotEmpty).toList();
  if (parts.length != 6) {
    throw FormatException('MAC 地址格式不正确：$mac（应形如 AA:BB:CC:DD:EE:FF）');
  }
  if (parts.any((p) => p.length > 2)) {
    throw FormatException('MAC 地址分段过长：$mac');
  }
  final out = Uint8List(6);
  for (var i = 0; i < 6; i++) {
    final v = int.tryParse(parts[i], radix: 16);
    if (v == null) {
      throw FormatException('MAC 地址含非十六进制字符：$mac');
    }
    out[i] = v;
  }
  return out;
}

Uint8List _md5(List<int> data) => Uint8List.fromList(crypto.md5.convert(data).bytes);

/// `MD5(0x03 0x01 + seed + password)` — 规程 4.6。
Uint8List md5a(List<int> password, List<int> seed) =>
    _md5(<int>[0x03, 0x01, ...seed, ...password]);

/// `MD5(0x01 + password + seed + 0x00000000)`。
///
/// 注意尾部四个 NUL 字节。规程写的摘要输入是 `1 + len(password) + 4` 字节，
/// 但能用的实现分配了 `9 + len(password)` 字节并整块哈希，所以那四个零也算
/// 输入。以抓到的成功登录为准。
Uint8List md5b(List<int> password, List<int> seed) {
  final buf = Uint8List(9 + password.length);
  buf[0] = 0x01;
  buf.setRange(1, 1 + password.length, password);
  buf.setRange(1 + password.length, 5 + password.length, seed);
  // [5+len .. 9+len) 保持零
  return _md5(buf);
}

/// 把 MD5A 前 6 字节与 MAC 按大端读成整数后异或，再按大端写回 6 字节。
Uint8List macXorMd5a(List<int> digest, List<int> macBytes) {
  var sum = 0;
  for (var i = 0; i < 6; i++) {
    sum = digest[i] + sum * 256;
  }
  var macInt = 0;
  for (var i = 0; i < 6; i++) {
    macInt = macBytes[i] + macInt * 256;
  }
  sum ^= macInt;
  final out = Uint8List(6);
  for (var i = 0; i < 6; i++) {
    out[i] = (sum >> (8 * (5 - i))) & 0xFF;
  }
  return out;
}

/// [length] 是否落在参考实现验证过的范围内。
bool passwordLengthSupported(int length) => length >= 1 && length <= referenceMaxPasswordLen;

/// 返回 `(extraLength, jluPadding)`。密码 ≤8 字符时 extra 为 0。
(int, int) passwordPadding(int passwordLen) {
  if (passwordLen <= 8) return (0, 0);
  final jluPadding = passwordLen != 16 ? passwordLen ~/ 4 : 0;
  return (28 + passwordLen - 8 + jluPadding, jluPadding);
}

/// `MD5(packet[0:97] + [0x14,0x00,0x07,0x0b])[0:8]` — 规程 4.6。
Uint8List computeChecksum1(List<int> packet) {
  final head = packet.sublist(0, math.min(97, packet.length));
  return Uint8List.fromList(_md5(<int>[...head, 0x14, 0x00, 0x07, 0x0B]).sublist(0, 8));
}

/// 密码段之后那个 XOR 折叠校验和。
///
/// [packetPrefix] 必须是 `loginPacket[0 : counter+2)`。临时缓冲区与参考实现
/// 一致：先 6 字节 `01 26 07 11 00 00`，再 MAC，其余隐式零。
Uint8List computeChecksum2(List<int> packetPrefix, List<int> macBytes) {
  final counter = packetPrefix.length - 2;
  final buf = Uint8List(counter + 18);
  buf.setRange(0, counter + 2, packetPrefix);
  buf.setRange(counter + 2, counter + 8, <int>[0x01, 0x26, 0x07, 0x11, 0x00, 0x00]);
  buf.setRange(counter + 8, counter + 14, macBytes);

  var acc = 1234;
  for (var i = 0; i < counter + 14; i += 4) {
    // 小端 uint32
    final ret = buf[i] | (buf[i + 1] << 8) | (buf[i + 2] << 16) | (buf[i + 3] << 24);
    acc ^= ret;
  }
  acc = (1968 * acc) & 0xFFFFFFFF;
  return Uint8List(4)
    ..[0] = acc & 0xFF
    ..[1] = (acc >> 8) & 0xFF
    ..[2] = (acc >> 16) & 0xFF
    ..[3] = (acc >> 24) & 0xFF;
}

// ---------------------------------------------------------------------------
// 组包
// ---------------------------------------------------------------------------

/// 20 字节挑战包（规程 4.2）。
///
/// 传 [randBytes]（两字节）可完全复现——测试用这个，因为 Dart 与 Python 的
/// 伪随机算法不同，同一个种子抽不出同一对数。传 [rng] 则从该随机源抽。
Uint8List buildChallengePacket({math.Random? rng, List<int>? randBytes}) {
  final Uint8List rand;
  if (randBytes != null) {
    if (randBytes.length != 2) {
      throw ArgumentError('randBytes 必须是 2 字节，收到 ${randBytes.length}');
    }
    rand = Uint8List.fromList(randBytes);
  } else if (rng != null) {
    rand = Uint8List.fromList(<int>[rng.nextInt(256), rng.nextInt(256)]);
  } else {
    rand = Uint8List.fromList(<int>[_randomByte(), _randomByte()]);
  }
  final packet = Uint8List(20);
  packet[0] = 0x01;
  packet[1] = 0x02;
  packet[2] = rand[0];
  packet[3] = rand[1];
  packet[4] = 0x68;
  return packet;
}

final math.Random _entropy = math.Random.secure();
int _randomByte() => _entropy.nextInt(256);

/// 变长登录包（规程 4.3 + 4.7）。
///
/// [seed] 必须是 4 字节；[mac] 必须能被 [macToBytes] 解析。
Uint8List buildLoginPacket({
  required String account,
  required String password,
  required String mac,
  required List<int> seed,
}) {
  if (seed.length != 4) {
    throw ArgumentError('seed 必须是 4 字节，收到 ${seed.length}');
  }

  final accountBytes = utf8.encode(account);
  final passwordBytes = utf8.encode(password);
  final macBytes = macToBytes(mac);

  final (extra, jluPadding) = passwordPadding(passwordBytes.length);
  final packet = Uint8List(loginBaseLen + extra);

  // --- 固定包头 -------------------------------------------------------
  packet[0] = 0x03;
  packet[1] = 0x01;
  packet[2] = 0x00;
  packet[3] = accountBytes.length + 20;

  final digestA = md5a(passwordBytes, seed);
  packet.setRange(4, 20, digestA);
  packet.setRange(20, 20 + accountBytes.length, accountBytes);

  packet[56] = 0x20;
  packet[57] = 0x03;
  packet.setRange(58, 64, macXorMd5a(digestA, macBytes));
  packet.setRange(64, 80, md5b(passwordBytes, seed));

  packet[80] = 0x01;
  // [81:85) = 主机 IP，全零，保持不动

  packet.setRange(97, 105, computeChecksum1(packet));
  packet[105] = 0x01;
  packet.setRange(110, 120, utf8.encode('LIYUANYUAN'));

  packet.setRange(142, 146, <int>[10, 10, 10, 10]); // 主 DNS
  // [146:150) = DHCP 服务器，全零

  // OS 版本结构（Windows 6.2 build 0x23f0，platform 2）
  _writeU32LE(packet, 162, 0x94);
  _writeU32LE(packet, 166, 6);
  _writeU32LE(packet, 170, 2);
  _writeU32LE(packet, 174, 0x23F0);
  _writeU32LE(packet, 178, 0x02);

  packet.setRange(182, 191, hostnameBlob);
  packet.setRange(246, 286, servicePack);

  packet[310] = 0x68;
  packet[311] = 0x00;

  // --- 变长密码段 -----------------------------------------------------
  var counter = pwSectionOffset;
  packet[counter + 1] = passwordBytes.length;
  counter += 2;

  for (var i = 0; i < passwordBytes.length; i++) {
    // i >= 16 时取 digestA[i % 16] 是有意为之的确定性扩展：参考实现在那里
    // 越界读取，不可复现。见 passwordLengthSupported()。
    final mixed = digestA[i % digestA.length] ^ passwordBytes[i];
    // 字节内左旋 3 位
    packet[counter + i] = (((mixed << 3) & 0xFF) + (mixed >> 5)) & 0xFF;
  }
  counter += passwordBytes.length;

  packet[counter] = 0x02;
  packet[counter + 1] = 0x0C;

  packet.setRange(counter + 2, counter + 6, computeChecksum2(packet.sublist(0, counter + 2), macBytes));
  packet.setRange(counter + 8, counter + 14, macBytes);

  final rorPadding = passwordBytes.length <= 8 ? (8 - passwordBytes.length) : jluPadding;
  packet[counter + rorPadding + 14] = 0x60;
  packet[counter + rorPadding + 15] = 0xA2;

  return packet;
}

/// keepalive-1 第二个包用的 8 字节 CRC（规程 4.6）。
Uint8List genCrc(List<int> seed, int encryptType) {
  switch (encryptType) {
    case 0:
      return Uint8List.fromList(<int>[0xC7, 0x2F, 0x31, 0x01, 0x7E, 0x00, 0x00, 0x00]);
    case 1:
      final h = _md5(seed);
      return Uint8List.fromList(<int>[h[2], h[3], h[8], h[9], h[5], h[6], h[13], h[14]]);
    case 2:
      final h = md4(seed);
      return Uint8List.fromList(<int>[h[1], h[2], h[8], h[9], h[4], h[5], h[11], h[12]]);
    case 3:
      final h = Uint8List.fromList(crypto.sha1.convert(seed).bytes);
      return Uint8List.fromList(<int>[h[2], h[3], h[9], h[10], h[5], h[6], h[15], h[16]]);
    default:
      throw ProtocolError('未知的 encrypt_type: $encryptType');
  }
}

void _writeU32LE(Uint8List buf, int offset, int value) {
  buf[offset] = value & 0xFF;
  buf[offset + 1] = (value >> 8) & 0xFF;
  buf[offset + 2] = (value >> 16) & 0xFF;
  buf[offset + 3] = (value >> 24) & 0xFF;
}

// ---------------------------------------------------------------------------
// 登录失败码（服务端首字节 0x05 时把码放在 [4]）
// ---------------------------------------------------------------------------
abstract final class LoginError {
  static const int macCheckFailed = 0x01;
  static const int serverBusy = 0x02;
  static const int wrongPassword = 0x03;
  static const int insufficientBalance = 0x04;
  static const int accountFrozen = 0x05;
  static const int ipMismatch = 0x07;
  static const int macMismatch = 0x0B;
  static const int tooManyIps = 0x14;
  static const int clientUpgradeRequired = 0x15;
  static const int ipAndMacMismatch = 0x16;
  static const int mustUseDhcp = 0x17;
}

final Map<int, (String, String)> _loginErrorText = <int, (String, String)>{
  LoginError.macCheckFailed: (
    'MAC 校验失败',
    '网卡物理地址被改动或客户端伪造 MAC，请核对本机 MAC 是否与账号绑定的网卡一致。',
  ),
  LoginError.serverBusy: ('服务器忙', '认证服务器当前过载，稍等片刻会自动重试（无需手动操作）。'),
  LoginError.wrongPassword: ('密码错误', '请检查校园网密码；若密码刚改过，请在此处同步更新。'),
  LoginError.insufficientBalance: ('余额不足', '网费余额不足，请先充值后再登录。'),
  LoginError.accountFrozen: ('账号被冻结', '账号已被停机/冻结，请联系网络中心处理。'),
  LoginError.ipMismatch: (
    'IP 不匹配',
    '本机 IP 与账号绑定的 IP 不一致，请确认网线插在正确的端口上，或改为自动获取 IP（DHCP）。',
  ),
  LoginError.macMismatch: (
    'MAC 不匹配',
    '本机网卡 MAC 与账号绑定的不一致。若刚更换网卡/主板，需要先在自助服务里解绑，或使用旧网卡的 MAC。',
  ),
  LoginError.tooManyIps: ('IP 数超限', '该账号同时在线的设备数已达上限，请先注销其它设备。'),
  LoginError.clientUpgradeRequired: (
    '需要升级客户端',
    '服务端要求升级客户端；该码在本校网络下通常等同于密码错误，请先核对密码。',
  ),
  LoginError.ipAndMacMismatch: (
    'IP 和 MAC 都不匹配',
    '本机 IP 与网卡 MAC 均与账号绑定信息不符，请检查接入端口并确认使用了正确的网卡。',
  ),
  LoginError.mustUseDhcp: (
    '必须使用 DHCP',
    '服务端要求本机通过 DHCP 获取地址：请把网卡设置为「自动获得 IP 地址」。',
  ),
};

/// 逐码的重试提示：命中即代表"原样重试永远不会成功"，重连循环必须停下来等用户。
final Set<int> _loginErrorFatal = <int>{
  LoginError.macCheckFailed,
  LoginError.wrongPassword,
  LoginError.insufficientBalance,
  LoginError.accountFrozen,
  LoginError.ipMismatch,
  LoginError.macMismatch,
  LoginError.tooManyIps,
  LoginError.clientUpgradeRequired,
  LoginError.ipAndMacMismatch,
  LoginError.mustUseDhcp,
  // serverBusy 是瞬态，不在集合里 —— 默认 false，退回重试+退避。
};

/// 返回 `(简述, 建议)`；未收录的码给兜底文案，绝不留空。
(String, String) describeLoginError(int code) {
  final entry = _loginErrorText[code];
  if (entry != null) return entry;
  final hex = code.toRadixString(16).toUpperCase().padLeft(2, '0');
  return ('未知错误（错误码 0x$hex）', '服务端返回了未收录的错误码 0x$hex，请把日志反馈给开发者。');
}

/// 原样重试是否有意义；未收录的码按"可重试"处理。
bool isFatalLoginError(int code) => _loginErrorFatal.contains(code);

// ---------------------------------------------------------------------------
// keepalive 包构造
// ---------------------------------------------------------------------------
/// 42 字节的第二个 keepalive-1 包（规程 4.8）。
///
/// 注意长度是 42 而不是 38：参考实现声明了 38 字节的数组却发了 42 字节，
/// 服务端接受 42。这个坑必须保留，否则对端不认。
Uint8List buildKeepalive1Packet2({
  required List<int> keepalive1Seed,
  required List<int> authInformation,
  List<int>? randBytes,
  math.Random? rng,
}) {
  if (keepalive1Seed.length != 4) {
    throw ArgumentError('keepalive1Seed 必须是 4 字节，收到 ${keepalive1Seed.length}');
  }
  if (authInformation.length != 16) {
    throw ArgumentError('authInformation 必须是 16 字节，收到 ${authInformation.length}');
  }
  final List<int> tail;
  if (randBytes != null) {
    if (randBytes.length != 2) {
      throw ArgumentError('randBytes 必须是 2 字节，收到 ${randBytes.length}');
    }
    tail = randBytes;
  } else if (rng != null) {
    tail = <int>[rng.nextInt(256), rng.nextInt(256)];
  } else {
    tail = <int>[_randomByte(), _randomByte()];
  }

  final packet = Uint8List(keepalive1Packet2Len);
  packet[0] = 0xFF;
  packet.setRange(8, 12, keepalive1Seed);
  packet.setRange(12, 20, genCrc(keepalive1Seed, keepalive1Seed[0] & 3));
  packet.setRange(20, 36, authInformation);
  packet[36] = tail[0];
  packet[37] = tail[1];
  // [38..42) 保持 0
  return packet;
}

/// 40 字节的 keepalive-2 包（规程 4.9）。
///
/// [pktType] 为 1 是 A 包、3 是 C 包；发 C 包时把上一轮响应里取到的
/// 4 字节 [tail] 放在偏移 16。
Uint8List buildKeepalive2Packet({
  required int counter,
  bool filePacket = false,
  int pktType = 1,
  List<int> tail = const <int>[],
}) {
  final packet = Uint8List(keepalive2Len);
  packet[0] = 0x07;
  packet[1] = counter & 0xFF;
  packet[2] = 0x28;
  packet[4] = 0x0B;
  packet[5] = pktType;
  packet.setRange(6, 8, filePacket ? keepaliveVersionFile : keepaliveVersionNormal);
  packet[8] = 0x2F;
  packet[9] = 0x12;
  if (pktType == 3) {
    packet.setRange(16, 20, tail.length == 4 ? tail : const <int>[0, 0, 0, 0]);
  }
  return packet;
}

// ---------------------------------------------------------------------------
// 响应解析
// ---------------------------------------------------------------------------
String _hex2(int b) => b.toRadixString(16).toUpperCase().padLeft(2, '0');

class ChallengeResponse {
  const ChallengeResponse({required this.seed, required this.ip});

  final List<int> seed;
  final String ip;
}

/// 校验并解析 76 字节的挑战响应（规程 4.2）。
ChallengeResponse parseChallengeResponse(List<int> data) {
  if (data.length < 24) {
    throw ProtocolError('challenge 响应长度不足：${data.length} 字节（期望 76）');
  }
  if (data[0] != 0x02) {
    throw ProtocolError('challenge 响应首字节应为 0x02，实际 0x${_hex2(data[0])}');
  }
  return ChallengeResponse(
    seed: data.sublist(4, 8),
    ip: data.sublist(20, 24).join('.'),
  );
}

class LoginOutcome {
  const LoginOutcome({
    required this.success,
    this.authInformation = const <int>[],
    this.errorCode,
    this.raw = const <int>[],
  });

  final bool success;
  final List<int> authInformation;
  final int? errorCode;
  final List<int> raw;

  String get message {
    if (success) return '登录成功';
    final code = errorCode;
    if (code == null) return '登录失败（服务端未返回错误码）';
    return describeLoginError(code).$1;
  }

  String get advice {
    if (success) return '';
    final code = errorCode;
    if (code == null) return '服务端返回了无法识别的响应，请查看日志面板中的原始报文。';
    return describeLoginError(code).$2;
  }
}

/// 解析登录响应（规程 4.4 + 4.5）。
LoginOutcome parseLoginResponse(List<int> data) {
  if (data.isEmpty) throw ProtocolError('登录响应为空');
  if (data[0] == 0x04) {
    if (data.length < 39) {
      throw ProtocolError('登录成功响应长度不足：${data.length} 字节（需要 ≥39）');
    }
    return LoginOutcome(
      success: true,
      authInformation: data.sublist(23, 39),
      raw: List<int>.unmodifiable(data),
    );
  }
  if (data[0] == 0x05) {
    return LoginOutcome(
      success: false,
      errorCode: data.length > 4 ? data[4] : null,
      raw: List<int>.unmodifiable(data),
    );
  }
  throw ProtocolError('登录响应首字节异常：0x${_hex2(data[0])}（长度 ${data.length}）');
}

class Keepalive1Result {
  const Keepalive1Result({required this.seed, required this.encryptType});

  final List<int> seed;
  final int encryptType;
}

class Keepalive2Result {
  const Keepalive2Result({required this.ok, this.tail = const <int>[], this.detail = ''});

  final bool ok;
  final List<int> tail;
  final String detail;
}

/// 校验 keepalive-1 的首个响应并取出 seed（规程 4.8）。
Keepalive1Result parseKeepalive1Response(List<int> data) {
  if (data.length < 12) {
    throw ProtocolError('keepalive1 响应过短：${data.length} 字节');
  }
  if (data[0] != 0x07) {
    throw ProtocolError('keepalive1 响应首字节应为 0x07，实际 0x${_hex2(data[0])}');
  }
  return Keepalive1Result(seed: data.sublist(8, 12), encryptType: data[8] & 3);
}

/// 校验 keepalive-2 响应（规程 4.9）。
Keepalive2Result parseKeepalive2Response(List<int> data, {bool filePacket = false}) {
  if (data.length < 3) {
    return Keepalive2Result(ok: false, detail: '响应过短（${data.length} 字节）');
  }
  if (data[0] != 0x07) {
    return Keepalive2Result(ok: false, detail: '首字节应为 0x07，实际 0x${_hex2(data[0])}');
  }
  if (filePacket) {
    // 0x10 == "filepacket received"；0x28 也认。
    if (data[2] != 0x10 && data[2] != 0x28) {
      return Keepalive2Result(ok: false, detail: 'file packet 响应 [2]=0x${_hex2(data[2])}');
    }
    return const Keepalive2Result(ok: true, detail: 'file packet 已接收');
  }
  if (data[2] != 0x28) {
    return Keepalive2Result(ok: false, detail: '响应 [2] 应为 0x28，实际 0x${_hex2(data[2])}');
  }
  return Keepalive2Result(
    ok: true,
    tail: data.length >= 20 ? data.sublist(16, 20) : const <int>[],
  );
}

/// 服务端通知包（opcode 0x4D）：不是错误，keepalive-1 期间要跳过并继续等。
bool looksLikeNotification(List<int> data) => data.isNotEmpty && data[0] == 0x4D;
