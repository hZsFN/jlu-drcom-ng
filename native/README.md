# drcomd —— 原生认证核心

认证与保活的原生实现，用 C 写，编译出约 220 KB 的 `drcomd.exe`。界面（Python +
Flet）只是这个进程的客户端，关掉窗口不影响登录。

**为什么要有它**：原来认证跑在 Python 主进程里，主进程又拖着 Flet 前端（`flet.exe`
常驻 100–190 MB），所以「关掉窗口」只能隐藏，内存一分不还。把认证拆出来以后，界面可以
真的退出，常驻的只剩这个进程——**私有内存约 1 MB**。

## 构建

```bat
native\build.bat          :: 编译 drcomd.exe 和测试程序
native\build.bat test     :: 顺便跑离线测试
```

需要 Visual Studio（2019/2022/2026 都行，Community 或 Build Tools 均可）。脚本自己找
`vcvars64.bat` 并调用，不需要先开 Developer Prompt，也不用手动设 `INCLUDE`/`LIB`。

> 为什么特意做成 `.bat`：在 POSIX shell 里把带引号的 Windows 路径传给 `cmd`，引号会被
> 吃掉，cmd 报「文件名、目录名或卷标语法不正确」。把整件事关在批处理里最省心。

也可以在 Visual Studio 里打开 `native\drcomd.sln`：项目开了 `/analyze`（PREfast）+
C++ Core Check + `AllRules.ruleset`，构建时就把静态分析跑一遍。

## 运行

```bat
drcomd.exe --config "%APPDATA%\JLU-DrCOM-NG\config.json" --api-port 61441
drcomd.exe --dry-run      :: 只读配置并打印，不联网
drcomd.exe --once         :: 认证一次就退出（调试用）
drcomd.exe --verbose      :: 连保活心跳一起打
```

它读**和 Python 版同一份** `config.json`，密码由 Windows DPAPI 解密，不需要重新输入。

## HTTP 接口

只监听 `127.0.0.1`，非环回连接一律 403。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 状态快照（字段与 Python 版 `StatusServer` 对齐） |
| GET | `/health` | `{"online":bool,"state":"..."}` |
| GET | `/logs` | 最近 40 条事件 |
| POST | `/login` | 开始/重新开始认证 |
| POST | `/logout` | 停止保活，进程继续常驻 |
| POST | `/reconnect` | 停 + 起 |
| POST | `/shutdown` | 退出进程 |

`--api-port` 优先于配置文件里的 `api.port`（界面必须知道自己该连哪里）。

## 代码结构

| 文件 | 内容 |
| --- | --- |
| `protocol.c/h` | 报文构造与解析；`md4.c`/`md5.c`/`sha1.c` 是自实现的哈希 |
| `engine.c/h` | 状态机、工作线程、keepalive、指数退避 |
| `config_load.c/h` | 极简 JSON 扫描器 + DPAPI 解密 |
| `main.c` | HTTP 服务、日志环、进程主循环 |
| `tests/` | 哈希向量、协议 golden 用例、引擎端到端（配 `mock_server.py`） |

协议的正确性靠**对拍**保证：`tools/make_golden.py` 从 `drcom/protocol.py` 导出 51 个
用例的期望字节流到 `tests/golden.h`，C 侧逐字节比对。**改了 Python 协议就重跑它**，
不跑的话 C 侧会悄悄落后。

```bat
python tools\make_golden.py
native\build.bat test
```

引擎的端到端测试需要一个假服务器：

```bat
python native\tests\mock_server.py --port 51440 --duration 20
native\tests\test_engine.exe 51440 51441 5
```

## 设计上的几条硬规矩

- **停止要立刻生效**：所有等待都走 `engine_wait()`（等 stop 事件），不会因为「正好在
  20 秒保活间隔里」让退出卡住
- **保活失败要退避**：登录成功不等于会话健康——网络吃保活包时登录次次成功、保活次次
  失败。所以计数器只在**一轮保活真正走通**之后才清零，否则会退化成每几秒重登一次
- **不在命令行里传密码**：密码由 DPAPI 在进程内解开，`--dry-run` 也只打印「已解密」
- **账号与 MAC 不外泄**：日志与接口里的账号一律 `ta****26` 脱敏，MAC 只打印「已读取」

## 已知限制

- 只支持 Windows（DPAPI、`WS2_32`）
- 密码格式支持 DPAPI（`D1:`）；Fernet（`F1:`）与降级混淆（`X1:`）会明确报错并要求改回
  DPAPI，不做静默降级
- `auto_interface_fallback`（多网卡时逐个源地址重试）尚未在原生侧实现，目前只用配置里
  的绑定地址或通配地址
