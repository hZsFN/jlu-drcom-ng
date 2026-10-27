import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:flutter/services.dart';
import 'package:tray_manager/tray_manager.dart';
import 'package:window_manager/window_manager.dart';

import 'controller.dart';
import 'hud/dashboard.dart';
import 'hud/hud_painter.dart' hide formatBytes, formatRate;
import 'hud/logs_view.dart';
import 'hud/about_view.dart';
import 'hud/account_view.dart';
import 'hud/shell.dart';
import 'hud/theme.dart';
import 'settings_page.dart';

/// 引擎状态到中文标签，和 Python 版 `STATE_LABELS` 一字不差。
const Map<String, String> stateLabels = <String, String>{
  'idle': '待机',
  'binding': '绑定端口',
  'challenging': '获取挑战码',
  'authenticating': '认证中',
  'online': '在线',
  'retry_wait': '等待重试',
  'fatal': '需处理',
  'stopped': '已停止',
};

/// 引擎状态到配色键（喂给 `HudPalette.stateColor`）。
String stateColorKey(String state) => switch (state) {
  'online' => 'online',
  'authenticating' || 'challenging' || 'binding' => 'challenging',
  'retry_wait' => 'retry_wait',
  'fatal' => 'fatal',
  'idle' || 'stopped' => 'idle',
  _ => 'text',
};

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await windowManager.ensureInitialized();

  // 先把配置读出来：要不要开机就最小化、要不要自动连，全靠它。
  final controller = DrcomController();
  final ui = controller.config.ui;

  // 尺寸对齐 Python 版（780 px 高），HUD 的四角读数和模式条才不会被裁掉。
  const options = WindowOptions(
    size: Size(1100, 780),
    minimumSize: Size(900, 640),
    center: true,
    title: 'JLU DrCOM-NG',
    backgroundColor: Color(0xFF0A0E14),
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
    final HudPalette palette = HudPalette.standard;
    return MaterialApp(
      title: 'JLU DrCOM-NG',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        scaffoldBackgroundColor: palette.bg,
        canvasColor: palette.bg,
        colorScheme: ColorScheme.fromSeed(
          seedColor: palette.cyan,
          brightness: Brightness.dark,
          surface: palette.panel,
        ),
        // 全局把字体换成等宽，HUD 才有仪表感。
        fontFamily: HudMetrics.fontMono,
        fontFamilyFallback: HudMetrics.fontMonoFallback,
        splashFactory: InkSparkle.splashFactory,
      ),
      home: HudThemeScope(
        palette: palette,
        child: _HudShell(controller: _controller),
      ),
    );
  }
}

/// 五页外壳：顶部标题栏 + 顶部导航 + 内容区。
class _HudShell extends StatefulWidget {
  const _HudShell({required this.controller});

  final DrcomController controller;

  @override
  State<_HudShell> createState() => _HudShellState();
}

class _HudShellState extends State<_HudShell> {
  String _view = hudViews.first;
  TimerHandle? _clock;

  DrcomController get _controller => widget.controller;

  @override
  void initState() {
    super.initState();
    _clock = TimerHandle(() {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _clock?.cancel();
    super.dispose();
  }

  /// 从 controller 的状态载荷拼出 HUD 遥测。
  HudTelemetry _telemetry() {
    final Map<String, Object?> status = _controller.status;
    final String state = status['state']?.toString() ?? 'idle';
    final num uptime = (status['uptime_seconds'] as num?) ?? 0;
    final TrafficSnapshot traffic = _controller.trafficSnapshot;
    final Map<String, Object?> probe = _controller.probeSummary;
    final double? rtt = (probe['rtt_ms'] as num?)?.toDouble();
    final double? loss = (probe['loss_percent'] as num?)?.toDouble();
    final double? jitter = (probe['jitter_ms'] as num?)?.toDouble();
    final bool online = status['online'] == true;
    final double interval = _controller.config.auth.keepaliveInterval;
    final double phase = (online && interval > 0)
        ? (uptime.toDouble() % interval) / interval
        : 0;

    return HudTelemetry(
      online: online,
      stateLabel: stateLabels[state] ?? state,
      stateColor: _hexOf(stateColorKey(state)),
      ip: status['ip']?.toString() ?? '',
      account: status['account']?.toString() ?? '',
      uptime: uptime.toDouble(),
      rxRate: traffic.rxRate,
      txRate: traffic.txRate,
      sessionRx: traffic.totalRx,
      sessionTx: traffic.totalTx,
      rttMs: rtt,
      lossPercent: loss,
      jitterMs: jitter,
      keepalivePhase: phase,
      drops: ((status['consecutive_failures'] as num?) ?? 0).toInt(),
      detail: status['last_error']?.toString() ?? '',
    );
  }

  /// 底部统计栏里「延迟 / 丢包 / 本次流量」三格的文本。
  String get _rttLabel {
    final Object? rtt = _controller.probeSummary['rtt_ms'];
    if (rtt is! num) return '—';
    return '${rtt.round()} ms';
  }

  String get _lossLabel {
    final Object? loss = _controller.probeSummary['loss_percent'];
    if (loss is! num) return '—';
    return '${loss.round()}%';
  }

  String get _trafficLabel {
    final TrafficSnapshot traffic = _controller.trafficSnapshot;
    return '↓${formatBytes(traffic.totalRx.toDouble())} '
        '↑${formatBytes(traffic.totalTx.toDouble())}';
  }

  /// 当前引擎状态键，给头部胶囊上色用。
  String get _stateKey => _controller.status['state']?.toString() ?? 'idle';

  String _hexOf(String key) {
    final HudPalette p = HudPalette.standard;
    final Color c = p.stateColor(key);
    return '#${c.toARGB32().toRadixString(16).padLeft(8, '0').substring(2).toUpperCase()}';
  }

  Widget _body(HudTelemetry telemetry) {
    switch (_view) {
      case '状态':
        return DashboardView(
          telemetry: telemetry,
          config: const HudConfig(),
          statusLabel: telemetry.stateLabel,
          ip: telemetry.ip.isEmpty ? '—' : telemetry.ip,
          uptimeLabel: _uptimeLabel(telemetry.uptime),
          account: telemetry.account.isEmpty ? '(未设置)' : telemetry.account,
          message: _controller.error.isEmpty ? '' : _controller.error,
          advice: '',
          todayLabel: _uptimeLabel(_controller.stats.today().onlineSeconds +
              _controller.stats.sessionSeconds),
          weekLabel: _uptimeLabel(_controller.stats.weekSeconds()),
          dropsLabel: '${_controller.stats.today().drops}',
          rttLabel: _rttLabel,
          lossLabel: _lossLabel,
          trafficLabel: _trafficLabel,
          onLogin: _controller.running ? null : _controller.connect,
          onLogout: _controller.running ? _controller.disconnect : null,
          onReconnect: _controller.running ? _reconnect : null,
          onDiagnose: null,
          onExportLogs: _exportLogs,
          canvasHeight: HudSizes.initialCanvasHeight(MediaQuery.sizeOf(context).height),
        );
      case '日志':
        return LogsView(
          lines: _controller.logs,
          onClear: _controller.clearLogs,
          onExport: _exportLogs,
        );
      case '账号':
        return AccountView(controller: _controller);
      case '设置':
        return SettingsPage(controller: _controller);
      case '关于':
        return const AboutView();
      default:
        return const SizedBox.shrink();
    }
  }

  /// 重连＝断开再连，中间让引擎把旧 sock 收干净。
  Future<void> _reconnect() async {
    await _controller.disconnect();
    if (!mounted) return;
    await _controller.connect();
  }

  void _exportLogs() {
    final text = _controller.exportLogs();
    _showTextDialog('导出的日志', text);
  }

  void _showTextDialog(String title, String body) {
    showDialog<void>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        backgroundColor: HudPalette.standard.panelAlt,
        title: Text(title, style: TextStyle(color: HudPalette.standard.cyan)),
        content: SizedBox(
          width: 720,
          height: 420,
          child: SingleChildScrollView(
            child: SelectableText(
              body,
              style: TextStyle(
                color: HudPalette.standard.textDim,
                fontFamily: HudMetrics.fontMono,
                fontFamilyFallback: HudMetrics.fontMonoFallback,
                fontSize: 12,
              ),
            ),
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: Text('关闭', style: TextStyle(color: HudPalette.standard.cyan)),
          ),
        ],
      ),
    );
  }

  String _uptimeLabel(double seconds) {
    final int s = seconds.toInt();
    final int h = s ~/ 3600;
    final int m = (s % 3600) ~/ 60;
    return '${h.toString().padLeft(2, '0')}:${m.toString().padLeft(2, '0')}:'
        '${(s % 60).toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    final HudTelemetry telemetry = _telemetry();
    return Scaffold(
      backgroundColor: palette.bg,
      body: SafeArea(
        child: Column(
          children: <Widget>[
            HudHeader(
              stateLabel: telemetry.stateLabel,
              stateColor: palette.stateColor(stateColorKey(_stateKey)),
              keepalivePhase: 0,
              clock: _clockLabel(),
            ),
            HudNav(
              active: _view,
              onSelect: (String name) => setState(() => _view = name),
            ),
            Expanded(child: _body(telemetry)),
          ],
        ),
      ),
    );
  }

  String _clockLabel() {
    final DateTime now = DateTime.now();
    return '${now.hour.toString().padLeft(2, '0')}:'
        '${now.minute.toString().padLeft(2, '0')}:'
        '${now.second.toString().padLeft(2, '0')}';
  }
}

/// 每秒刷一次界面（HUD 不做 60fps 动画，省 CPU）。
class TimerHandle {
  TimerHandle(this._onTick) {
    _timer = Timer.periodic(const Duration(seconds: 1), (_) => _onTick());
  }

  final void Function() _onTick;
  late final Timer _timer;

  void cancel() => _timer.cancel();
}
