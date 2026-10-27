/// JLU DrCOM-NG 的 Dart 实现。
///
/// 分层：
/// * `src/md4.dart`      —— RFC 1320 MD4（协议摘要算法）
/// * `src/protocol.dart` —— 报文构造与解析，纯函数，可对着黄金向量测试
/// * `src/transport.dart`—— UDP 收发，抽成接口以便引擎状态机用假实现测
/// * `src/engine.dart`   —— 登录 + 保活状态机
/// * `src/secrets.dart`  —— 密码存取（Windows DPAPI）
/// * `src/config.dart`   —— 配置读写，字段与参考实现对齐
/// * `src/app.dart`      —— 应用层：装配、状态文件、日志
library;

export 'src/app.dart';
export 'src/config.dart';
export 'src/engine.dart';
export 'src/md4.dart';
export 'src/protocol.dart';
export 'src/secrets.dart';
export 'src/transport.dart';
export 'src/netprobe.dart';
export 'src/stats.dart';
export 'src/traffic.dart';
