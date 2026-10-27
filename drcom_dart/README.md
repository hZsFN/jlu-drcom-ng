# drcom_dart — 认证内核的 Dart 实现

`jlu-drcom-ng` 的认证部分用 Dart 重写了一遍。目的是**去掉常驻的 Python 进程**：
现在这一层只依赖 Dart SDK，编译出来是原生二进制，不需要在用户机器上装 Python、
不用带 38 MB 的 Flet 运行时。

配套的图形界面在 [`../desktop`](../desktop)（Flutter）。两者合起来就是 Dart 版客户端。

## 分层

| 文件 | 干什么 |
|---|---|
| `lib/src/md4.dart` | RFC 1320 MD4，协议摘要用 |
| `lib/src/protocol.dart` | 报文构造与解析，纯函数 |
| `lib/src/transport.dart` | UDP 收发，抽成接口好测 |
| `lib/src/engine.dart` | 登录 + 保活状态机 |
| `lib/src/secrets.dart` | 密码存取（Windows DPAPI，手写 FFI 调 crypt32） |
| `lib/src/config.dart` | 配置读写，字段与 `../drcom/config.py` 对齐 |
| `lib/src/app.dart` | 应用层：装配、`status.json`、日志 |

## 和 Python 版的关系

- **配置文件共用**：都读写 `%APPDATA%\JLU-DrCOM-NG\config.json`。密码格式也兼容
  （`D1:` / `F1:` / `X1:` 前缀都认），所以装过 Python 版的人换过来不用重输密码。
- **写回策略**：Dart 版以磁盘上的原文为底、只覆盖自己认识的键，不会把 Python 版
  新增的段或键抹掉。
- **协议逐字节对齐**：`test/golden/protocol_vectors.json` 是从 Python 参考实现
  （`drcom/protocol.py`）导出的黄金向量，改协议必须重新生成。

```bash
python tool/gen_golden.py   # 重新导出黄金向量
dart test                   # 全部测试
```

## 真机工具

```bash
# 只读探测：发一个挑战包，打印服务端回的 seed 和它认定的本机 IP
# 不发登录、不带凭据，换网后排查链路很方便
python tool/probe_auth.py

# 长时保活（凭据从 stdin 进，不落命令行）
dart run bin/engine_smoke.dart --rounds=10
```

## 已知差异

Dart 版目前**没有** Python 版里这些功能：托盘菜单之外的高级设置页、流量统计、
通知系统、单实例锁、内置 HTTP 状态接口。移植优先级见仓库 issue。
