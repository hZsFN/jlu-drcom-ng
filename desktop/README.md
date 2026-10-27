# desktop — Flutter 桌面端

Dart 版客户端的图形界面。认证逻辑全在 [`../drcom_dart`](../drcom_dart)，
这里只负责窗口、托盘和日志展示。

## 开发

```bash
flutter pub get
flutter run -d windows     # 调试运行
flutter test               # UI 冒烟测试
flutter analyze
```

## 打包

```bash
flutter build windows --release
# 产物：build/windows/x64/runner/Release/

# 再打成安装包（需要 Inno Setup 6）：
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=2.0.0 ..\installer\installer-dart.iss
# 产物：..\release\JLU-DrCOM-NG-<版本>-dart-setup.exe
```

## 结构

| 文件 | 干什么 |
|---|---|
| `lib/main.dart` | 入口：窗口尺寸、托盘图标与菜单、关窗口收进托盘 |
| `lib/controller.dart` | UI 与引擎之间的桥，把 `DrcomApp` 的长 Future 包成可驱动的东西 |
| `lib/home_page.dart` | 状态卡 + 连接/断开 + 日志 |
