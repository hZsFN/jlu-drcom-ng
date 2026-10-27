import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:tray_manager/tray_manager.dart';
import 'package:window_manager/window_manager.dart';

import 'controller.dart';
import 'home_page.dart';
import 'accounts_page.dart';
import 'settings_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await windowManager.ensureInitialized();

  // 先把配置读出来：要不要开机就最小化、要不要自动连，全靠它。
  final controller = DrcomController();
  final ui = controller.config.ui;

  const options = WindowOptions(
    size: Size(560, 640),
    minimumSize: Size(480, 560),
    center: true,
    title: 'JLU DrCOM-NG',
    backgroundColor: Colors.transparent,
    titleBarStyle: TitleBarStyle.normal,
  );
  await windowManager.waitUntilReadyToShow(options, () async {
    if (ui.startMinimized) {
      // 直接躲在托盘里，别弹窗吵人。
      await windowManager.setSkipTaskbar(true);
      await windowManager.hide();
    } else {
      await windowManager.show();
      await windowManager.focus();
    }
  });

  runApp(DrcomRoot(controller: controller));
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
    _maybeAutoLogin();
  }

  /// 配置里开了「启动时自动连接」、当前账号也存了密码，就自己连上去。
  void _maybeAutoLogin() {
    final account = _controller.activeAccount;
    if (_controller.config.ui.autoLoginOnLaunch != true ||
        account == null ||
        account.passwordProtected.isEmpty) {
      return;
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) {
        _controller.connect();
      }
    });
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

  /// 关窗口：配置里让收进托盘就藏起来，否则直接退出。
  @override
  void onWindowClose() async {
    final prevent = await windowManager.isPreventClose();
    if (!prevent) return;
    if (_controller.config.ui.minimizeToTray) {
      await windowManager.hide();
    } else {
      await _quit();
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
      home: _Shell(controller: _controller),
    );
  }
}

/// 三页外壳：主页 / 账号 / 设置。
class _Shell extends StatefulWidget {
  const _Shell({required this.controller});

  final DrcomController controller;

  @override
  State<_Shell> createState() => _ShellState();
}

class _ShellState extends State<_Shell> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _index,
        children: <Widget>[
          HomePage(controller: widget.controller),
          AccountsPage(controller: widget.controller),
          SettingsPage(controller: widget.controller),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        height: 58,
        selectedIndex: _index,
        onDestinationSelected: (int value) => setState(() => _index = value),
        destinations: const <NavigationDestination>[
          NavigationDestination(
            icon: Icon(Icons.wifi_outlined),
            selectedIcon: Icon(Icons.wifi),
            label: '连接',
          ),
          NavigationDestination(
            icon: Icon(Icons.people_outline),
            selectedIcon: Icon(Icons.people),
            label: '账号',
          ),
          NavigationDestination(
            icon: Icon(Icons.settings_outlined),
            selectedIcon: Icon(Icons.settings),
            label: '设置',
          ),
        ],
      ),
    );
  }
}
