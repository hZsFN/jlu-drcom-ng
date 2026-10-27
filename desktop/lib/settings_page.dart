import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'controller.dart';
import 'services/autostart.dart';

/// 设置页：线路参数、保活节流、界面行为。
///
/// 直接编辑 `controller.config`，点「保存」才落盘 —— 这样中途关掉窗口
/// 不会把配置写坏。
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key, required this.controller});

  final DrcomController controller;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _server;
  late final TextEditingController _port;
  late final TextEditingController _bindPort;
  late final TextEditingController _timeout;
  late final TextEditingController _keepalive;
  late final TextEditingController _minDelay;
  late final TextEditingController _maxDelay;
  late final TextEditingController _giveUp;

  late bool _autoFallback;
  late bool _allowAlternate;
  late bool _reconnectEnabled;
  late bool _autoLoginOnLaunch;
  late bool _minimizeToTray;
  late bool _startMinimized;
  late bool _notifications;

  /// 开机自启的真身不在配置文件里，而是注册表 Run 键 —— 单独问它。
  /// null 表示还在读，读之前别让用户点。
  bool? _autostart;

  bool _dirty = false;

  @override
  void initState() {
    super.initState();
    final config = widget.controller.config;
    _server = TextEditingController(text: config.auth.server);
    _port = TextEditingController(text: '${config.auth.port}');
    _bindPort = TextEditingController(text: '${config.auth.bindPort}');
    _timeout = TextEditingController(text: '${config.auth.timeoutMs}');
    _keepalive =
        TextEditingController(text: _trimNum(config.auth.keepaliveInterval));
    _minDelay =
        TextEditingController(text: _trimNum(config.reconnect.minDelay));
    _maxDelay =
        TextEditingController(text: _trimNum(config.reconnect.maxDelay));
    _giveUp = TextEditingController(text: '${config.reconnect.giveUpAfter}');
    _autoFallback = config.auth.autoInterfaceFallback;
    _allowAlternate = config.auth.allowAlternatePort;
    _reconnectEnabled = config.reconnect.enabled;
    _autoLoginOnLaunch = config.ui.autoLoginOnLaunch;
    _minimizeToTray = config.ui.minimizeToTray;
    _startMinimized = config.ui.startMinimized;
    _notifications = config.ui.notifications;
    _loadAutostart();
  }

  Future<void> _loadAutostart() async {
    final on = await Autostart.isEnabled();
    if (mounted) {
      setState(() => _autostart = on);
    }
  }

  Future<void> _toggleAutostart(bool on) async {
    // 先记账再动手：开关要立刻反映，写失败再翻回来。
    setState(() => _autostart = on);
    final ok = await Autostart.setEnabled(on);
    if (!mounted) return;
    if (!ok) {
      setState(() => _autostart = !on);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('写注册表失败，开机自启没改成功')),
      );
      return;
    }
    widget.controller.config.ui.autostart = on;
    widget.controller.persist();
  }

  static String _trimNum(double value) {
    if (value == value.roundToDouble()) return '${value.toInt()}';
    return '$value';
  }

  @override
  void dispose() {
    for (final item in <TextEditingController>[
      _server,
      _port,
      _bindPort,
      _timeout,
      _keepalive,
      _minDelay,
      _maxDelay,
      _giveUp,
    ]) {
      item.dispose();
    }
    super.dispose();
  }

  void _markDirty() {
    if (!_dirty) setState(() => _dirty = true);
  }

  void _save() {
    final config = widget.controller.config;
    final port = int.tryParse(_port.text.trim());
    final bindPort = int.tryParse(_bindPort.text.trim());
    final timeout = int.tryParse(_timeout.text.trim());
    final keepalive = double.tryParse(_keepalive.text.trim());
    final minDelay = double.tryParse(_minDelay.text.trim());
    final maxDelay = double.tryParse(_maxDelay.text.trim());
    final giveUp = int.tryParse(_giveUp.text.trim());
    final server = _server.text.trim();

    final problem = <String>[
      if (server.isEmpty) '认证服务器不能为空',
      if (port == null || port <= 0 || port > 65535) '端口要是 1-65535 的整数',
      if (bindPort == null || bindPort < 0 || bindPort > 65535)
        '绑定端口要是 1-65535 的整数',
      if (timeout == null || timeout < 500) '超时不能小于 500 毫秒',
      if (keepalive == null || keepalive < 5) '保活间隔不能小于 5 秒',
      if (minDelay == null || minDelay <= 0) '最小重连间隔要大于 0',
      if (maxDelay == null || maxDelay < (minDelay ?? 0)) '最大间隔不能小于最小间隔',
      if (giveUp == null || giveUp < 1) '放弃次数至少 1 次',
    ];
    if (problem.isNotEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(problem.first)),
      );
      return;
    }

    config.auth
      ..server = server
      ..port = port!
      ..bindPort = bindPort!
      ..timeoutMs = timeout!
      ..keepaliveInterval = keepalive!
      ..autoInterfaceFallback = _autoFallback
      ..allowAlternatePort = _allowAlternate;
    config.reconnect
      ..enabled = _reconnectEnabled
      ..minDelay = minDelay!
      ..maxDelay = maxDelay!
      ..giveUpAfter = giveUp!;
    config.ui
      ..autoLoginOnLaunch = _autoLoginOnLaunch
      ..minimizeToTray = _minimizeToTray
      ..startMinimized = _startMinimized
      ..notifications = _notifications;

    final ok = widget.controller.persist();
    setState(() => _dirty = false);
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(ok ? '已保存，下次连接生效' : '保存失败，见主界面底部'),
      ),
    );
  }

  void _resetDefaults() {
    setState(() {
      _server.text = '10.100.61.3';
      _port.text = '61440';
      _bindPort.text = '61440';
      _timeout.text = '3000';
      _keepalive.text = '20';
      _minDelay.text = '5';
      _maxDelay.text = '300';
      _giveUp.text = '8';
      _autoFallback = true;
      _allowAlternate = false;
      _reconnectEnabled = true;
      _autoLoginOnLaunch = true;
      _minimizeToTray = true;
      _startMinimized = false;
      _notifications = true;
      _dirty = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final running = widget.controller.running;
    return Scaffold(
      appBar: AppBar(
        title: const Text('设置'),
        actions: <Widget>[
          IconButton(
            tooltip: '恢复默认值',
            icon: const Icon(Icons.restart_alt),
            onPressed: _resetDefaults,
          ),
        ],
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          children: <Widget>[
            if (running)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Text(
                  '引擎正在运行，改了要断开重连才生效。',
                  style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
                ),
              ),
            _Section(
              title: '认证服务器',
              children: <Widget>[
                _Row(
                  label: '地址',
                  child: _Field(
                    controller: _server,
                    hint: '10.100.61.3',
                    onChanged: _markDirty,
                  ),
                ),
                _Row(
                  label: '端口',
                  child: _Field(
                    controller: _port,
                    hint: '61440',
                    numeric: true,
                    onChanged: _markDirty,
                  ),
                ),
                _Row(
                  label: '绑定端口',
                  child: _Field(
                    controller: _bindPort,
                    hint: '61440',
                    numeric: true,
                    onChanged: _markDirty,
                  ),
                ),
                _Row(
                  label: '超时(ms)',
                  child: _Field(
                    controller: _timeout,
                    hint: '3000',
                    numeric: true,
                    onChanged: _markDirty,
                  ),
                ),
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('自动换网卡', style: TextStyle(fontSize: 14)),
                  subtitle: const Text(
                    '绑不上 61440 或网卡不通时自动退到其它本机地址',
                    style: TextStyle(fontSize: 12),
                  ),
                  value: _autoFallback,
                  onChanged: (bool v) {
                    setState(() => _autoFallback = v);
                    _markDirty();
                  },
                ),
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('允许改用备用端口', style: TextStyle(fontSize: 14)),
                  subtitle: const Text(
                    '61440 被别的程序占死时试 61441',
                    style: TextStyle(fontSize: 12),
                  ),
                  value: _allowAlternate,
                  onChanged: (bool v) {
                    setState(() => _allowAlternate = v);
                    _markDirty();
                  },
                ),
              ],
            ),
            const SizedBox(height: 14),
            _Section(
              title: '保活与重连',
              children: <Widget>[
                _Row(
                  label: '保活间隔(秒)',
                  child: _Field(
                    controller: _keepalive,
                    hint: '20',
                    numeric: true,
                    onChanged: _markDirty,
                  ),
                ),
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('掉线后自动重连', style: TextStyle(fontSize: 14)),
                  value: _reconnectEnabled,
                  onChanged: (bool v) {
                    setState(() => _reconnectEnabled = v);
                    _markDirty();
                  },
                ),
                _Row(
                  label: '最小间隔(秒)',
                  child: _Field(
                    controller: _minDelay,
                    hint: '5',
                    numeric: true,
                    onChanged: _markDirty,
                  ),
                ),
                _Row(
                  label: '最大间隔(秒)',
                  child: _Field(
                    controller: _maxDelay,
                    hint: '300',
                    numeric: true,
                    onChanged: _markDirty,
                  ),
                ),
                _Row(
                  label: '放弃前重试',
                  child: _Field(
                    controller: _giveUp,
                    hint: '8',
                    numeric: true,
                    onChanged: _markDirty,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 14),
            _Section(
              title: '界面',
              children: <Widget>[
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('启动后自动连接', style: TextStyle(fontSize: 14)),
                  value: _autoLoginOnLaunch,
                  onChanged: (bool v) {
                    setState(() => _autoLoginOnLaunch = v);
                    _markDirty();
                  },
                ),
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('关窗口时收进托盘', style: TextStyle(fontSize: 14)),
                  value: _minimizeToTray,
                  onChanged: (bool v) {
                    setState(() => _minimizeToTray = v);
                    _markDirty();
                  },
                ),
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('启动时最小化', style: TextStyle(fontSize: 14)),
                  value: _startMinimized,
                  onChanged: (bool v) {
                    setState(() => _startMinimized = v);
                    _markDirty();
                  },
                ),
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('开机自动启动', style: TextStyle(fontSize: 14)),
                  subtitle: const Text(
                    '写当前用户的注册表 Run 键，不需要管理员权限',
                    style: TextStyle(fontSize: 11),
                  ),
                  value: _autostart ?? false,
                  onChanged: (_autostart == null || !Autostart.isSupported)
                      ? null
                      : _toggleAutostart,
                ),
                SwitchListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('登录/掉线时气泡提醒', style: TextStyle(fontSize: 14)),
                  value: _notifications,
                  onChanged: (bool v) {
                    setState(() => _notifications = v);
                    _markDirty();
                  },
                ),
              ],
            ),
            const SizedBox(height: 20),
            FilledButton.icon(
              onPressed: _dirty ? _save : null,
              icon: const Icon(Icons.save_outlined),
              label: Text(_dirty ? '保存' : '没有改动'),
            ),
            const SizedBox(height: 10),
            Text(
              '配置文件：${widget.controller.store.path}',
              style: TextStyle(fontSize: 11, color: scheme.outline),
            ),
          ],
        ),
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.children});

  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.only(left: 4, bottom: 6),
          child: Text(
            title,
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: scheme.primary,
            ),
          ),
        ),
        Container(
          padding: const EdgeInsets.fromLTRB(14, 6, 14, 6),
          decoration: BoxDecoration(
            color: scheme.surfaceContainerHighest.withValues(alpha: 0.35),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: scheme.outlineVariant, width: 0.8),
          ),
          child: Column(children: children),
        ),
      ],
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.child});

  final String label;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: <Widget>[
          SizedBox(
            width: 104,
            child: Text(
              label,
              style: const TextStyle(fontSize: 14),
            ),
          ),
          Expanded(child: child),
        ],
      ),
    );
  }
}

class _Field extends StatelessWidget {
  const _Field({
    required this.controller,
    required this.hint,
    this.numeric = false,
    this.onChanged,
  });

  final TextEditingController controller;
  final String hint;
  final bool numeric;
  final VoidCallback? onChanged;

  @override
  Widget build(BuildContext context) {
    return TextField(
      controller: controller,
      onChanged: (_) => onChanged?.call(),
      keyboardType: numeric ? TextInputType.number : TextInputType.text,
      inputFormatters: numeric
          ? <TextInputFormatter>[FilteringTextInputFormatter.digitsOnly]
          : null,
      style: const TextStyle(fontSize: 14),
      decoration: InputDecoration(
        hintText: hint,
        isDense: true,
        contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
        border: const OutlineInputBorder(),
      ),
    );
  }
}
