import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:tray_manager/tray_manager.dart';
import 'package:window_manager/window_manager.dart';

import 'controller.dart';
import 'home_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await windowManager.ensureInitialized();

  const options = WindowOptions(
    size: Size(560, 640),
    minimumSize: Size(480, 560),
    center: true,
    title: 'JLU DrCOM-NG',
    backgroundColor: Colors.transparent,
    titleBarStyle: TitleBarStyle.normal,
  );
  await windowManager.waitUntilReadyToShow(options, () async {
    await windowManager.show();
    await windowManager.focus();
  });

  runApp(DrcomRoot(controller: DrcomController()));
}

class DrcomRoot extends StatefulWidget {
  const DrcomRoot({super.key, required this.controller});

  final DrcomController controller;

  @override
  State<DrcomRoot> createState() => _DrcomRootState();
}

class _DrcomRootState extends State<DrcomRoot> with TrayListener, WindowListener {
  DrcomController get _controller => widget.controller;

  @override
  void initState() {
    super.initState();
    windowManager.addListener(this);
    windowManager.setPreventClose(true);
    trayManager.addListener(this);
    _setupTray();
  }

  @override
  void dispose() {
    trayManager.removeListener(this);
    windowManager.removeListener(this);
    super.dispose();
  }

  Future<void> _setupTray() async {
    // tray_manager 要的是磁盘上的图标文件，asset 得先落到临时目录。
    final bytes = await rootBundle.load('assets/tray_icon.ico');
    final iconFile = File(
      '${Directory.systemTemp.path}${Platform.pathSeparator}jlu_drcom_tray.ico',
    );
    await iconFile.writeAsBytes(bytes.buffer.asUint8List(), flush: true);
    await trayManager.setIcon(iconFile.path);
    await trayManager.setToolTip('JLU DrCOM-NG');
    await trayManager.setContextMenu(
      Menu(
        items: <MenuItem>[
          MenuItem(key: 'show', label: '打开主界面'),
          MenuItem.separator(),
          MenuItem(key: 'connect', label: '连接'),
          MenuItem(key: 'disconnect', label: '断开'),
          MenuItem.separator(),
          MenuItem(key: 'quit', label: '退出'),
        ],
      ),
    );
  }

  @override
  void onTrayIconMouseDown() {
    windowManager.show();
    windowManager.focus();
  }

  @override
  void onTrayMenuItemClick(MenuItem menuItem) {
    switch (menuItem.key) {
      case 'show':
        windowManager.show();
        windowManager.focus();
      case 'connect':
        _controller.connect();
      case 'disconnect':
        _controller.disconnect();
      case 'quit':
        _quit();
    }
  }

  /// 关窗口 = 收进托盘，不退出。
  @override
  void onWindowClose() async {
    final prevent = await windowManager.isPreventClose();
    if (prevent) {
      await windowManager.hide();
    }
  }

  Future<void> _quit() async {
    await windowManager.setPreventClose(false);
    await _controller.shutdown();
    await trayManager.destroy();
    await windowManager.destroy();
    exit(0);
  }

  @override
  Widget build(BuildContext context) {
    final brightness =
        WidgetsBinding.instance.platformDispatcher.platformBrightness;
    return MaterialApp(
      title: 'JLU DrCOM-NG',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF1565C0),
          brightness: brightness,
        ),
      ),
      home: HomePage(controller: _controller),
    );
  }
}
