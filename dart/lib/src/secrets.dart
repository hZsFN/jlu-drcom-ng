/// Windows DPAPI 加解密，配合参考实现的密码字段格式。
///
/// 只声明真正用到的那几个符号（crypt32 的两个函数 + kernel32 的
/// `LocalFree` / `GetLastError`），不引 `win32` 包——那个包面很大、
/// 跟着 SDK 版本变，而这里只需要四个 entry point。
///
/// 字段格式与 Python 版 `drcom/secrets_store.py` 一致：
/// `base64( 前缀 + 密文 )`，前缀 `D1:` 表示 DPAPI、`F1:` 表示 Fernet、
/// `X1:` 表示可逆混淆（降级）。这样 Dart 版能直接解开现有配置里的密码，
/// 用户换到新客户端不用重输。
library;

import 'dart:convert';
import 'dart:ffi';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';

/// 密码字段前缀：DPAPI。
const List<int> tagDpapi = <int>[0x44, 0x31, 0x3A]; // "D1:"

/// 密码字段前缀：Fernet。
const List<int> tagFernet = <int>[0x46, 0x31, 0x3A]; // "F1:"

/// 密码字段前缀：可逆混淆（降级方案）。
const List<int> tagObfuscated = <int>[0x58, 0x31, 0x3A]; // "X1:"

/// 密码保护相关失败。
class SecretError implements Exception {
  SecretError(this.message);

  final String message;

  @override
  String toString() => 'SecretError: $message';
}

/// 加解密结果，附带用的是哪种后端（展示/日志用）。
class Declassified {
  Declassified(this.plaintext, this.backend);

  final String plaintext;
  final String backend;
}

// -- Win32 绑定 -------------------------------------------------------------

final class _Blob extends Struct {
  @Uint32()
  external int cbData;

  external Pointer<Uint8> pbData;
}

typedef _CryptProtectNative = Int32 Function(
  Pointer<_Blob> pDataIn,
  Pointer<Utf16> szDataDescr,
  Pointer<_Blob> pOptionalEntropy,
  Pointer<Void> pvReserved,
  Pointer<Void> pPromptStruct,
  Uint32 dwFlags,
  Pointer<_Blob> pDataOut,
);

typedef _CryptUnprotectNative = Int32 Function(
  Pointer<_Blob> pDataIn,
  Pointer<Utf16> ppszDataDescr,
  Pointer<_Blob> pOptionalEntropy,
  Pointer<Void> pvReserved,
  Pointer<Void> pPromptStruct,
  Uint32 dwFlags,
  Pointer<_Blob> pDataOut,
);

typedef _CryptProtectDart = int Function(
  Pointer<_Blob>,
  Pointer<Utf16>,
  Pointer<_Blob>,
  Pointer<Void>,
  Pointer<Void>,
  int,
  Pointer<_Blob>,
);

typedef _LocalFreeNative = Pointer<Void> Function(Pointer<Void> ptr);
typedef _LocalFreeDart = Pointer<Void> Function(Pointer<Void>);

typedef _GetLastErrorNative = Uint32 Function();
typedef _GetLastErrorDart = int Function();

class _Win32 {
  _Win32._();

  static final DynamicLibrary _crypt32 = DynamicLibrary.open('crypt32.dll');
  static final DynamicLibrary _kernel32 = DynamicLibrary.open('kernel32.dll');

  static final _CryptProtectDart cryptProtectData =
      _crypt32.lookupFunction<_CryptProtectNative, _CryptProtectDart>(
    'CryptProtectData',
  );
  static final _CryptProtectDart cryptUnprotectData =
      _crypt32.lookupFunction<_CryptUnprotectNative, _CryptProtectDart>(
    'CryptUnprotectData',
  );
  static final _LocalFreeDart localFree =
      _kernel32.lookupFunction<_LocalFreeNative, _LocalFreeDart>('LocalFree');
  static final _GetLastErrorDart getLastError =
      _kernel32.lookupFunction<_GetLastErrorNative, _GetLastErrorDart>(
    'GetLastError',
  );
}

// -- 核心实现 ---------------------------------------------------------------

Uint8List _dpapiCall(
  _CryptProtectDart fn,
  String name,
  List<int> data,
  String? description,
) {
  final inputBlob = calloc<_Blob>();
  final outputBlob = calloc<_Blob>();
  final inputBytes = calloc<Uint8>(data.isEmpty ? 1 : data.length);
  Pointer<Utf16> descriptionPtr = nullptr;
  try {
    if (data.isNotEmpty) {
      inputBytes.asTypedList(data.length).setAll(0, data);
    }
    inputBlob.ref.cbData = data.length;
    inputBlob.ref.pbData = inputBytes;
    outputBlob.ref.cbData = 0;
    outputBlob.ref.pbData = nullptr;
    if (description != null) {
      descriptionPtr = description.toNativeUtf16();
    }

    final ok = fn(
      inputBlob,
      descriptionPtr,
      nullptr,
      nullptr,
      nullptr,
      0,
      outputBlob,
    );
    if (ok == 0) {
      throw SecretError('$name 失败（Win32 错误码 ${_Win32.getLastError()}）');
    }
    final length = outputBlob.ref.cbData;
    final pointer = outputBlob.ref.pbData;
    if (pointer == nullptr || length == 0) {
      throw SecretError('$name 返回了空结果');
    }
    return Uint8List.fromList(pointer.asTypedList(length));
  } finally {
    if (outputBlob.ref.pbData != nullptr) {
      _Win32.localFree(outputBlob.ref.pbData.cast<Void>());
    }
    if (descriptionPtr != nullptr) {
      calloc.free(descriptionPtr);
    }
    calloc.free(inputBytes);
    calloc.free(inputBlob);
    calloc.free(outputBlob);
  }
}

/// 用当前用户的 DPAPI 主密钥加密（同一台机器、同一个 Windows 账户才能解）。
Uint8List encryptDpapi(List<int> data, {String description = 'DrCOM password'}) {
  return _dpapiCall(_Win32.cryptProtectData, 'CryptProtectData', data, description);
}

/// 解开 [encryptDpapi] 产出的密文。
Uint8List decryptDpapi(List<int> data) {
  return _dpapiCall(_Win32.cryptUnprotectData, 'CryptUnprotectData', data, null);
}

// -- 对外接口 ---------------------------------------------------------------

/// 把明文密码打包成配置里 `password_protected` 的值。
String protectPassword(String plaintext) {
  final blob = <int>[...tagDpapi, ...encryptDpapi(utf8.encode(plaintext))];
  return base64Encode(blob);
}

/// 解开配置里的 `password_protected`。
///
/// 能解 DPAPI（D1:）就解；Fernet（F1:）和降级混淆（X1:）在这里直接报错，
/// 因为 Dart 侧没带 AES 与那个 key.bin 的派生逻辑——真碰上再说，
/// 反正 Windows 上参考实现首选 DPAPI。
Declassified unprotectPassword(String stored) {
  if (stored.isEmpty) {
    return Declassified('', 'none');
  }
  Uint8List blob;
  try {
    blob = base64Decode(stored);
  } on FormatException {
    throw SecretError('密码字段不是合法的 Base64，配置可能已损坏');
  }
  if (_startsWith(blob, tagDpapi)) {
    final body = Uint8List.fromList(blob.sublist(tagDpapi.length));
    return Declassified(utf8.decode(decryptDpapi(body)), 'Windows DPAPI');
  }
  if (_startsWith(blob, tagFernet)) {
    throw SecretError('该密码由 Fernet 加密，Dart 版暂不支持；请重新输入一次密码');
  }
  if (_startsWith(blob, tagObfuscated)) {
    throw SecretError('该密码是可逆混淆（降级）格式，请重新输入一次密码');
  }
  // 老版本没有前缀，裸 DPAPI blob。
  return Declassified(utf8.decode(decryptDpapi(blob)), 'Windows DPAPI（无前缀）');
}

bool _startsWith(List<int> data, List<int> prefix) {
  if (data.length < prefix.length) {
    return false;
  }
  for (var i = 0; i < prefix.length; i++) {
    if (data[i] != prefix[i]) {
      return false;
    }
  }
  return true;
}
