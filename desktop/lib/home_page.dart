import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:jlu_drcom/jlu_drcom.dart';

import 'controller.dart';

/// 主界面：状态一眼看清，操作就两个按钮，日志放下面。
class HomePage extends StatelessWidget {
  const HomePage({super.key, required this.controller});

  final DrcomController controller;

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: controller,
      builder: (BuildContext context, _) {
        final status = controller.status;
        final online = status['online'] == true;
        final state = status['state']?.toString() ?? 'stopped';
        final busy = controller.running && !online;
        return Scaffold(
          body: SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 18, 20, 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: <Widget>[
                  _StatusCard(
                    state: state,
                    online: online,
                    busy: busy,
                    account: status['account']?.toString() ?? '',
                    ip: status['ip']?.toString() ?? '',
                    server: status['server']?.toString() ?? '',
                    uptimeSeconds:
                        (status['uptime_seconds'] as num?)?.toDouble() ?? 0,
                    failures:
                        (status['consecutive_failures'] as num?)?.toInt() ?? 0,
                    lastError: status['last_error']?.toString() ?? '',
                  ),
                  const SizedBox(height: 16),
                  _ActionBar(
                    running: controller.running,
                    onConnect: controller.connect,
                    onDisconnect: controller.disconnect,
                  ),
                  const SizedBox(height: 16),
                  Expanded(
                    child: _LogView(
                      lines: controller.logs,
                      onCopy: () async {
                        await Clipboard.setData(
                          ClipboardData(text: controller.exportLogs()),
                        );
                        if (!context.mounted) return;
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text('日志已复制到剪贴板')),
                        );
                      },
                      onClear: controller.logs.isEmpty
                          ? null
                          : controller.clearLogs,
                    ),
                  ),
                  const SizedBox(height: 8),
                  _Footer(
                    statusPath: controller.statusPath,
                    error: controller.error,
                  ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}

/// 状态卡片：大圆点 + 一行大字 + 三格明细。
class _StatusCard extends StatelessWidget {
  const _StatusCard({
    required this.state,
    required this.online,
    required this.busy,
    required this.account,
    required this.ip,
    required this.server,
    required this.uptimeSeconds,
    required this.failures,
    required this.lastError,
  });

  final String state;
  final bool online;
  final bool busy;
  final String account;
  final String ip;
  final String server;
  final double uptimeSeconds;
  final int failures;
  final String lastError;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final (String label, Color color) = _describe(scheme);
    return Card(
      elevation: 0,
      color: scheme.surfaceContainerHighest.withValues(alpha: 0.55),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 16, 18, 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Container(
                  width: 12,
                  height: 12,
                  decoration: BoxDecoration(color: color, shape: BoxShape.circle),
                ),
                const SizedBox(width: 10),
                Text(
                  label,
                  style: theme.textTheme.titleMedium
                      ?.copyWith(fontWeight: FontWeight.w600),
                ),
                const Spacer(),
                if (online)
                  Text(
                    _formatDuration(uptimeSeconds),
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 14),
            Row(
              children: <Widget>[
                Expanded(child: _Field(label: '账号', value: account)),
                Expanded(child: _Field(label: 'IP', value: ip)),
                Expanded(child: _Field(label: '服务器', value: server)),
              ],
            ),
            if (failures > 0) ...<Widget>[
              const SizedBox(height: 10),
              Text(
                '连续失败 $failures 次，正在自动重试',
                style: theme.textTheme.bodySmall?.copyWith(color: scheme.error),
              ),
            ],
            if (lastError.isNotEmpty) ...<Widget>[
              const SizedBox(height: 10),
              Text(
                lastError,
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.bodySmall?.copyWith(color: scheme.error),
              ),
            ],
          ],
        ),
      ),
    );
  }

  (String, Color) _describe(ColorScheme scheme) {
    if (online) return ('已连接', Colors.green.shade600);
    switch (state) {
      case 'fatal':
        return ('认证失败', scheme.error);
      case 'retry_wait':
        return ('重连中', Colors.orange.shade700);
      case 'binding':
      case 'challenging':
      case 'authenticating':
        return ('连接中…', Colors.orange.shade700);
      case 'stopped':
      case 'idle':
        return ('未连接', scheme.outline);
      default:
        return (state, scheme.outline);
    }
  }
}

class _Field extends StatelessWidget {
  const _Field({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          label,
          style: theme.textTheme.labelSmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 2),
        Text(
          value.isEmpty ? '—' : value,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: theme.textTheme.bodyMedium,
        ),
      ],
    );
  }
}

class _ActionBar extends StatelessWidget {
  const _ActionBar({
    required this.running,
    required this.onConnect,
    required this.onDisconnect,
  });

  final bool running;
  final Future<void> Function() onConnect;
  final Future<void> Function() onDisconnect;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: <Widget>[
        Expanded(
          child: FilledButton.icon(
            onPressed: running ? null : onConnect,
            icon: const Icon(Icons.play_arrow_rounded),
            label: const Text('连接'),
            style: FilledButton.styleFrom(
              padding: const EdgeInsets.symmetric(vertical: 14),
            ),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: OutlinedButton.icon(
            onPressed: running ? onDisconnect : null,
            icon: const Icon(Icons.stop_rounded),
            label: const Text('断开'),
            style: OutlinedButton.styleFrom(
              padding: const EdgeInsets.symmetric(vertical: 14),
            ),
          ),
        ),
      ],
    );
  }
}

/// 日志：最新的在最上面，省掉滚动逻辑。右上角两个小按钮（复制 / 清空）。
class _LogView extends StatelessWidget {
  const _LogView({
    required this.lines,
    required this.onCopy,
    required this.onClear,
  });

  final List<AppLogLine> lines;
  final Future<void> Function() onCopy;
  final VoidCallback? onClear;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.35),
        borderRadius: BorderRadius.circular(12),
      ),
      padding: const EdgeInsets.fromLTRB(12, 4, 6, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            children: <Widget>[
              Text(
                '日志',
                style: theme.textTheme.labelMedium?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
              const Spacer(),
              _MiniButton(
                tooltip: '复制全部日志',
                icon: Icons.copy_all_outlined,
                onPressed: () => onCopy(),
              ),
              _MiniButton(
                tooltip: '清空日志',
                icon: Icons.delete_sweep_outlined,
                onPressed: onClear,
              ),
            ],
          ),
          Expanded(
            child: lines.isEmpty
                ? Center(
                    child: Text(
                      '还没有日志',
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
                    ),
                  )
                : ListView.builder(
                    reverse: false,
                    itemCount: lines.length,
                    itemBuilder: (BuildContext context, int index) {
                      final line = lines[lines.length - 1 - index];
                      return Padding(
                        padding: const EdgeInsets.symmetric(vertical: 2),
                        child: Text(
                          line.format(),
                          style: theme.textTheme.bodySmall?.copyWith(
                            fontFamily: 'Consolas',
                            color: theme.colorScheme.onSurfaceVariant,
                          ),
                        ),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}

class _MiniButton extends StatelessWidget {
  const _MiniButton({
    required this.tooltip,
    required this.icon,
    required this.onPressed,
  });

  final String tooltip;
  final IconData icon;
  final VoidCallback? onPressed;

  @override
  Widget build(BuildContext context) {
    return IconButton(
      tooltip: tooltip,
      iconSize: 17,
      visualDensity: VisualDensity.compact,
      constraints: const BoxConstraints(minWidth: 30, minHeight: 30),
      padding: EdgeInsets.zero,
      icon: Icon(icon),
      onPressed: onPressed,
    );
  }
}

class _Footer extends StatelessWidget {
  const _Footer({required this.statusPath, required this.error});

  final String statusPath;
  final String error;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      children: <Widget>[
        Expanded(
          child: Text(
            '状态文件 $statusPath',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: theme.textTheme.labelSmall
                ?.copyWith(color: theme.colorScheme.outline),
          ),
        ),
        if (error.isNotEmpty)
          Tooltip(
            message: error,
            child: Icon(Icons.info_outline,
                size: 16, color: theme.colorScheme.error),
          ),
      ],
    );
  }
}

String _formatDuration(double seconds) {
  final total = seconds.round();
  final h = total ~/ 3600;
  final m = (total % 3600) ~/ 60;
  final s = total % 60;
  if (h > 0) return '$h 小时 $m 分';
  if (m > 0) return '$m 分 $s 秒';
  return '$s 秒';
}
