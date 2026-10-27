/// 日志页：等宽字体列表 + 工具条。
library;

import 'package:flutter/material.dart';

import 'dashboard.dart' show HudThemeScope, HudPanel;
import 'theme.dart';

/// 一条日志（来自引擎）。
class LogsView extends StatefulWidget {
  const LogsView({
    super.key,
    required this.lines,
    required this.onClear,
    required this.onExport,
  });

  final List<dynamic> lines;
  final VoidCallback onClear;
  final VoidCallback onExport;

  @override
  State<LogsView> createState() => _LogsViewState();
}

class _LogsViewState extends State<LogsView> {
  final ScrollController _scroll = ScrollController();
  bool _follow = true;

  @override
  void didUpdateWidget(LogsView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (_follow && widget.lines.length != oldWidget.lines.length) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scroll.hasClients) {
          _scroll.jumpTo(_scroll.position.maxScrollExtent);
        }
      });
    }
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(
        HudMetrics.pad,
        0,
        HudMetrics.pad,
        HudMetrics.pad,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            children: <Widget>[
              Text(
                '${widget.lines.length} 条',
                style: TextStyle(color: palette.textMuted, fontSize: HudMetrics.sizeLabel),
              ),
              const Spacer(),
              _ToolButton(
                label: _follow ? '跟随：开' : '跟随：关',
                onTap: () => setState(() => _follow = !_follow),
              ),
              const SizedBox(width: 8),
              _ToolButton(label: '清空', onTap: widget.onClear),
              const SizedBox(width: 8),
              _ToolButton(label: '导出', onTap: widget.onExport),
            ],
          ),
          const SizedBox(height: HudMetrics.gap),
          Expanded(
            child: HudPanel(
              padding: EdgeInsets.zero,
              color: palette.panelSunk,
              child: Scrollbar(
                controller: _scroll,
                child: ListView.builder(
                  controller: _scroll,
                  padding: const EdgeInsets.all(10),
                  itemCount: widget.lines.length,
                  itemBuilder: (BuildContext context, int index) {
                    final dynamic line = widget.lines[index];
                    final String text = line.toString();
                    return Padding(
                      padding: const EdgeInsets.only(bottom: 2),
                      child: SelectableText(
                        text,
                        style: TextStyle(
                          color: _colorFor(text, palette),
                          fontSize: 12,
                          fontFamily: HudMetrics.fontMono,
                          fontFamilyFallback: HudMetrics.fontMonoFallback,
                          height: 1.35,
                        ),
                      ),
                    );
                  },
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  /// 日志配色：错误红、警告琥珀、其余正常。
  Color _colorFor(String text, HudPalette palette) {
    final String lower = text.toLowerCase();
    if (lower.contains('error') ||
        lower.contains('失败') ||
        lower.contains('错误') ||
        lower.contains('fatal')) {
      return palette.danger;
    }
    if (lower.contains('warn') ||
        lower.contains('重试') ||
        lower.contains('超时') ||
        lower.contains('无响应')) {
      return palette.amber;
    }
    return palette.textDim;
  }
}

class _ToolButton extends StatelessWidget {
  const _ToolButton({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(HudMetrics.radius),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(HudMetrics.radius),
            border: Border.all(color: palette.border, width: HudMetrics.hairline),
          ),
          child: Text(
            label,
            style: TextStyle(color: palette.cyan, fontSize: HudMetrics.sizeLabel),
          ),
        ),
      ),
    );
  }
}
