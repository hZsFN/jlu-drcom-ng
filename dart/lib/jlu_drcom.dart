/// JLU DrCOM-NG 的 Dart 实现。
///
/// 分层：
/// * `src/md4.dart`      —— RFC 1320 MD4（协议摘要算法）
/// * `src/protocol.dart` —— 报文构造与解析，纯函数，可对着黄金向量测试
/// * `src/transport.dart`—— UDP 收发，抽成接口以便引擎状态机用假实现测
library;

export 'src/md4.dart';
export 'src/protocol.dart';
export 'src/transport.dart';
export 'src/engine.dart';
