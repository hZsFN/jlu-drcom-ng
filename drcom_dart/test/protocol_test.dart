/// 协议实现的黄金向量对拍。
///
/// 判据来自 `test/golden/protocol_vectors.json`——那份 JSON 由
/// `dart/tool/gen_golden.py` 从 **Python 参考实现** 生成，不是手写的期望值。
/// 也就是说：这组测试在问"你抄对了没有"，不是在问"你觉得应该是什么"。
///
/// 改协议时先跑 `python dart/tool/gen_golden.py` 重新生成向量，再看差异。
library;

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:jlu_drcom/src/md4.dart';
import 'package:jlu_drcom/src/protocol.dart';
import 'package:test/test.dart';

late Map<String, dynamic> golden;

Uint8List fromHex(String hex) => Uint8List.fromList(
      List<int>.generate(hex.length ~/ 2, (i) => int.parse(hex.substring(i * 2, i * 2 + 2), radix: 16)),
    );

String toHex(List<int> bytes) => bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();

/// 逐字节断言，失败时报出**第一个不一致的偏移**，而不是甩 676 个字符的 hex。
void expectBytes(List<int> actual, String expectedHex, {String reason = ''}) {
  final expected = fromHex(expectedHex);
  if (actual.length != expected.length) {
    fail('$reason 长度不符：实际 ${actual.length}，期望 ${expected.length}');
  }
  for (var i = 0; i < expected.length; i++) {
    if (actual[i] != expected[i]) {
      final a = actual[i].toRadixString(16).padLeft(2, '0');
      final e = expected[i].toRadixString(16).padLeft(2, '0');
      fail('$reason 第 $i 字节不符：实际 0x$a，期望 0x$e');
    }
  }
}

void main() {
  // 同步读：group() 的回调在注册阶段就会执行，用 setUpAll 会来不及。
  golden = jsonDecode(File('test/golden/protocol_vectors.json').readAsStringSync())
      as Map<String, dynamic>;

  group('MD4（RFC 1320 标准向量，自证实现本身）', () {
    test('空串', () {
      expect(toHex(md4(utf8.encode(''))), '31d6cfe0d16ae931b73c59d7e0c089c0');
    });
    test('abc', () {
      expect(toHex(md4(utf8.encode('abc'))), 'a448017aaf21d8525fc10ae87aa6729d');
    });
    test('message digest', () {
      expect(toHex(md4(utf8.encode('message digest'))), 'd9130a8164549fe818874806e1c7014b');
    });
    test('跨块（>56 字节，触发第二块）', () {
      expect(
        toHex(md4(utf8.encode('12345678901234567890123456789012345678901234567890123456789012345678901234567890'))),
        'e33b4ddc9c38f2199c3e7b164fcc0536',
      );
    });
  });

  group('常量与 Python 参考实现一致', () {
    test('loginBaseLen', () => expect(loginBaseLen, golden['constants']['LOGIN_BASE_LEN']));
    test('pwSectionOffset', () => expect(pwSectionOffset, golden['constants']['PW_SECTION_OFFSET']));
    test('keepalive1Packet1', () => expectBytes(keepalive1Packet1, golden['constants']['KEEPALIVE1_PACKET1']));
    test('keepalive1Packet2Len', () => expect(keepalive1Packet2Len, golden['constants']['KEEPALIVE1_PACKET2_LEN']));
    test('keepalive2Len', () => expect(keepalive2Len, golden['constants']['KEEPALIVE2_LEN']));
    test('servicePack', () => expectBytes(servicePack, golden['constants']['SERVICE_PACK']));
    test('hostnameBlob', () => expectBytes(hostnameBlob, golden['constants']['HOSTNAME_BLOB']));
    test('keepaliveVersionFile', () => expectBytes(keepaliveVersionFile, golden['constants']['KEEPALIVE_VERSION_FILE']));
    test('keepaliveVersionNormal', () => expectBytes(keepaliveVersionNormal, golden['constants']['KEEPALIVE_VERSION_NORMAL']));
    test('referenceMaxPasswordLen', () => expect(referenceMaxPasswordLen, golden['constants']['REFERENCE_MAX_PASSWORD_LEN']));
  });

  group('挑战包', () {
    final cases = golden['challenge'] as List<dynamic>;
    for (var i = 0; i < cases.length; i++) {
      final c = cases[i] as Map<String, dynamic>;
      test('rng_seed=${c['rng_seed']}', () {
        final randBytes = (c['rand_bytes'] as List<dynamic>).cast<int>();
        expectBytes(
          buildChallengePacket(randBytes: randBytes),
          c['packet'] as String,
          reason: '挑战包(seed=${c['rng_seed']})',
        );
      });
    }
  });

  group('登录包', () {
    final cases = golden['logins'] as List<dynamic>;
    for (var i = 0; i < cases.length; i++) {
      final c = cases[i] as Map<String, dynamic>;
      final label = '${c['account']}/pw${(c['password'] as String).length}';

      test('$label MAC 解析', () {
        expectBytes(macToBytes(c['mac'] as String), c['mac_bytes'] as String, reason: '$label mac');
      });

      test('$label md5a / md5b', () {
        final seed = fromHex(c['seed'] as String);
        final pw = utf8.encode(c['password'] as String);
        expectBytes(md5a(pw, seed), c['md5a'] as String, reason: '$label md5a');
        expectBytes(md5b(pw, seed), c['md5b'] as String, reason: '$label md5b');
      });

      test('$label 完整登录包（逐字节）', () {
        final packet = buildLoginPacket(
          account: c['account'] as String,
          password: c['password'] as String,
          mac: c['mac'] as String,
          seed: fromHex(c['seed'] as String),
        );
        expect(packet.length, c['login_len'] as int, reason: '$label 长度');
        expectBytes(packet, c['login_packet'] as String, reason: '$label 登录包');
        expectBytes(computeChecksum1(packet), c['checksum1'] as String, reason: '$label checksum1');
      });
    }
  });

  group('密码长度分支', () {
    final cases = golden['padding'] as List<dynamic>;
    for (final raw in cases) {
      final c = raw as Map<String, dynamic>;
      final n = c['len'] as int;
      test('len=$n', () {
        final (extra, jlu) = passwordPadding(n);
        expect(extra, c['extra'], reason: 'len=$n extra');
        expect(jlu, c['jlu_padding'], reason: 'len=$n jlu_padding');
        expect(passwordLengthSupported(n), c['supported'], reason: 'len=$n supported');
      });
    }
  });

  group('keepalive gen_crc', () {
    final seed = fromHex('11223344');
    final table = golden['gen_crc'] as Map<String, dynamic>;
    for (final entry in table.entries) {
      final t = int.parse(entry.key);
      test('encrypt_type=$t', () {
        expectBytes(genCrc(seed, t), entry.value as String, reason: 'crc type=$t');
      });
    }
    test('未知类型抛 ProtocolError', () {
      expect(() => genCrc(seed, 9), throwsA(isA<ProtocolError>()));
    });
  });

  group('keepalive-1 第二个包（42 字节）', () {
    final cases = golden['keepalive1_p2'] as List<dynamic>;
    for (final raw in cases) {
      final c = raw as Map<String, dynamic>;
      test('encrypt_type=${c['encrypt_type']}', () {
        final packet = buildKeepalive1Packet2(
          keepalive1Seed: fromHex(c['seed'] as String),
          authInformation: fromHex(c['auth_information'] as String),
          randBytes: (c['rand_bytes'] as List<dynamic>).cast<int>(),
        );
        expect(packet.length, 42, reason: '长度必须是 42 而不是 38');
        expectBytes(packet, c['packet'] as String, reason: 'ka1p2[${c['encrypt_type']}]');
      });
    }
    test('seed 长度非法抛 ArgumentError', () {
      expect(
        () => buildKeepalive1Packet2(
          keepalive1Seed: const <int>[1, 2, 3],
          authInformation: List<int>.filled(16, 0),
          randBytes: const <int>[0, 0],
        ),
        throwsA(isA<ArgumentError>()),
      );
    });
  });

  group('keepalive-2 包（40 字节）', () {
    final cases = golden['keepalive2'] as List<dynamic>;
    for (final raw in cases) {
      final c = raw as Map<String, dynamic>;
      final label = 'counter=${c['counter']} file=${c['file_packet']} type=${c['pkt_type']}';
      test(label, () {
        final tailHex = c['tail'] as String;
        final packet = buildKeepalive2Packet(
          counter: c['counter'] as int,
          filePacket: c['file_packet'] as bool,
          pktType: c['pkt_type'] as int,
          tail: tailHex.isEmpty ? const <int>[] : fromHex(tailHex),
        );
        expect(packet.length, 40);
        expectBytes(packet, c['packet'] as String, reason: label);
      });
    }
  });

  group('响应解析', () {
    final responses = golden['responses'] as Map<String, dynamic>;

    Map<String, dynamic> pick(String key) => responses[key] as Map<String, dynamic>;

    test('challenge_ok', () {
      final c = pick('challenge_ok');
      final r = parseChallengeResponse(fromHex(c['input'] as String));
      expect(toHex(r.seed), c['seed']);
      expect(r.ip, c['ip']);
    });

    test('login_ok', () {
      final c = pick('login_ok');
      final r = parseLoginResponse(fromHex(c['input'] as String));
      expect(r.success, c['success']);
      expect(toHex(r.authInformation), c['auth_information']);
      expect(r.message, '登录成功');
      expect(r.advice, '');
    });

    test('login_fail', () {
      final c = pick('login_fail');
      final r = parseLoginResponse(fromHex(c['input'] as String));
      expect(r.success, c['success']);
      expect(r.errorCode, c['error_code']);
      expect(r.message, c['message']);
      expect(r.advice, c['advice']);
    });

    test('keepalive1_ok', () {
      final c = pick('keepalive1_ok');
      final r = parseKeepalive1Response(fromHex(c['input'] as String));
      expect(toHex(r.seed), c['seed']);
      expect(r.encryptType, c['encrypt_type']);
    });

    test('keepalive2_ok', () {
      final c = pick('keepalive2_ok');
      final r = parseKeepalive2Response(fromHex(c['input'] as String));
      expect(r.ok, c['ok']);
      expect(toHex(r.tail), c['tail']);
    });

    test('keepalive2_file_ok', () {
      final c = pick('keepalive2_file_ok');
      final r = parseKeepalive2Response(fromHex(c['input'] as String), filePacket: true);
      expect(r.ok, c['ok']);
      expect(r.detail, c['detail']);
    });

    test('keepalive2_bad', () {
      final c = pick('keepalive2_bad');
      final r = parseKeepalive2Response(fromHex(c['input'] as String));
      expect(r.ok, c['ok']);
      expect(r.detail, c['detail']);
    });

    test('notification 包识别', () {
      final c = pick('notification');
      final bytes = fromHex(c['input'] as String);
      expect(looksLikeNotification(bytes), c['is_notification']);
      expect(looksLikeNotification(const <int>[]), isFalse);
    });

    // --- 异常路径：坏包必须炸，不能静默当成成功 ---
    test('challenge 首字节错 → ProtocolError', () {
      final bad = fromHex(pick('challenge_ok')['input'] as String)..[0] = 0x03;
      expect(() => parseChallengeResponse(bad), throwsA(isA<ProtocolError>()));
    });

    test('challenge 太短 → ProtocolError', () {
      expect(() => parseChallengeResponse(List<int>.filled(10, 0)), throwsA(isA<ProtocolError>()));
    });

    test('登录响应为空 → ProtocolError', () {
      expect(() => parseLoginResponse(const <int>[]), throwsA(isA<ProtocolError>()));
    });

    test('登录响应首字节异常 → ProtocolError', () {
      expect(() => parseLoginResponse(const <int>[0x09, 0, 0, 0, 0]), throwsA(isA<ProtocolError>()));
    });

    test('登录成功响应太短 → ProtocolError', () {
      expect(() => parseLoginResponse(const <int>[0x04, 0, 0]), throwsA(isA<ProtocolError>()));
    });

    test('keepalive1 首字节错 → ProtocolError', () {
      final bad = fromHex(pick('keepalive1_ok')['input'] as String)..[0] = 0x08;
      expect(() => parseKeepalive1Response(bad), throwsA(isA<ProtocolError>()));
    });

    test('keepalive2 首字节错 → ok=false 而不是抛异常', () {
      final bad = fromHex(pick('keepalive2_ok')['input'] as String)..[0] = 0x08;
      final r = parseKeepalive2Response(bad);
      expect(r.ok, isFalse);
      expect(r.detail, contains('0x08'));
    });

    test('keepalive2 过短 → ok=false', () {
      expect(parseKeepalive2Response(const <int>[0x07]).ok, isFalse);
    });
  });

  group('登录错误码表', () {
    final table = golden['login_errors'] as Map<String, dynamic>;
    for (final entry in table.entries) {
      final code = int.parse(entry.key);
      final c = entry.value as Map<String, dynamic>;
      test('code=0x${code.toRadixString(16)}', () {
        final (text, advice) = describeLoginError(code);
        expect(text, c['text']);
        expect(advice, c['advice']);
        expect(isFatalLoginError(code), c['fatal']);
      });
    }

    test('未收录码给兜底文案（不留空）', () {
      final (text, advice) = describeLoginError(0x7F);
      expect(text, '未知错误（错误码 0x7F）');
      expect(advice, contains('0x7F'));
      expect(isFatalLoginError(0x7F), isFalse);
    });

    test('SERVER_BUSY 是瞬态，不算致命', () {
      expect(isFatalLoginError(LoginError.serverBusy), isFalse);
    });
  });
}
