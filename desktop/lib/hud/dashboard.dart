/// 状态页（HUD 仪表盘），布局复刻 Python 版 `drcom/ui/app.py::_view_dashboard`。
library;

import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'hud_painter.dart';
import 'theme.dart';

/// HUD 画布的上下限与窗口专用的高度（照抄 Python 版常量）。
abstract final class HudSizes {
  static const double maxHeight = 420;
  static const double minHeight = 240;

  /// 窗口高减去头部 + 导航 + 读数 + 按钮 + 统计栏的大致占用。
  static const double chromeHeight = 453;

  /// 一次性算出画布高度：HUD 自己会适配被给到的高度，所以只可能"要得太多"。
  static double initialCanvasHeight(double windowHeight) {
    final double h = windowHeight <= 0 ? 780 : windowHeight;
    return math.min(maxHeight, math.max(minHeight, h - chromeHeight));
  }
}

/// 一个 HUD 风格的卡片：实底 + 1px 边框 + 小圆角。
class HudPanel extends StatelessWidget {
  const HudPanel({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(HudMetrics.pad),
    this.color,
    this.borderColor,
    this.borderLeft,
    this.radius = HudMetrics.radius,
  });

  final Widget child;
  final EdgeInsets padding;
  final Color? color;
  final Color? borderColor;
  final BorderSide? borderLeft;
  final double radius;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Container(
      padding: padding,
      decoration: BoxDecoration(
        color: color ?? palette.panel,
        borderRadius: BorderRadius.circular(radius),
        border: borderLeft != null
            ? Border(left: borderLeft!)
            : Border.all(color: borderColor ?? palette.border, width: HudMetrics.hairline),
      ),
      child: child,
    );
  }
}

/// 把当前配色下发到子树，省得每个组件都传一遍。
class HudThemeScope extends InheritedWidget {
  const HudThemeScope({super.key, required this.palette, required super.child});

  final HudPalette palette;

  static HudPalette of(BuildContext context) {
    final HudThemeScope? scope = context.dependOnInheritedWidgetOfExactType<HudThemeScope>();
    return scope?.palette ?? HudPalette.standard;
  }

  @override
  bool updateShouldNotify(HudThemeScope oldWidget) => oldWidget.palette != palette;
}

/// 标签在上、值在下的小块。
class ReadoutBlock extends StatelessWidget {
  const ReadoutBlock({
    super.key,
    required this.label,
    required this.value,
    required this.style,
    this.valueAlign = TextAlign.left,
  });

  final String label;
  final String value;
  final TextStyle style;
  final TextAlign valueAlign;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Column(
      crossAxisAlignment:
          valueAlign == TextAlign.right ? CrossAxisAlignment.end : CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Text(
          label,
          style: TextStyle(
            color: palette.textMuted,
            fontSize: HudMetrics.sizeLabel,
            fontFamily: HudMetrics.fontMono,
            fontFamilyFallback: HudMetrics.fontMonoFallback,
          ),
        ),
        const SizedBox(height: 3),
        SelectableText(value, style: style),
      ],
    );
  }
}

/// 统计栏里的一格。
class StatCell extends StatelessWidget {
  const StatCell({super.key, required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Text(
          label,
          style: TextStyle(
            color: palette.textMuted,
            fontSize: HudMetrics.sizeMicro,
            fontFamily: HudMetrics.fontMono,
            fontFamilyFallback: HudMetrics.fontMonoFallback,
          ),
        ),
        const SizedBox(height: 2),
        Text(
          value,
          style: TextStyle(
            color: palette.text,
            fontSize: 15,
            fontFamily: HudMetrics.fontMono,
            fontFamilyFallback: HudMetrics.fontMonoFallback,
          ),
        ),
      ],
    );
  }
}

/// HUD 动作按钮：图标 + 文字，强调色描边。
class HudActionButton extends StatelessWidget {
  const HudActionButton({
    super.key,
    required this.label,
    required this.icon,
    required this.accent,
    required this.onPressed,
    this.filled = false,
  });

  final String label;
  final IconData icon;
  final Color accent;
  final VoidCallback? onPressed;
  final bool filled;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    final bool enabled = onPressed != null;
    final Color tone = enabled ? accent : palette.textMuted;
    return Material(
      color: filled && enabled ? accent.withValues(alpha: 0.16) : Colors.transparent,
      borderRadius: BorderRadius.circular(HudMetrics.radius),
      child: InkWell(
        onTap: onPressed,
        borderRadius: BorderRadius.circular(HudMetrics.radius),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(HudMetrics.radius),
            border: Border.all(color: tone, width: HudMetrics.hairline),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              Icon(icon, size: 16, color: tone),
              const SizedBox(width: 7),
              Text(
                label,
                style: TextStyle(
                  color: tone,
                  fontSize: HudMetrics.sizeBody,
                  fontFamily: HudMetrics.fontMono,
                  fontFamilyFallback: HudMetrics.fontMonoFallback,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// 状态页主体：画布 + 读数 + 告警 + 按钮 + 统计。
class DashboardView extends StatefulWidget {
  const DashboardView({
    super.key,
    required this.telemetry,
    required this.config,
    required this.statusLabel,
    required this.ip,
    required this.uptimeLabel,
    required this.account,
    required this.message,
    required this.advice,
    required this.todayLabel,
    required this.weekLabel,
    required this.dropsLabel,
    required this.rttLabel,
    required this.lossLabel,
    required this.trafficLabel,
    required this.onLogin,
    required this.onLogout,
    required this.onReconnect,
    required this.onDiagnose,
    required this.onExportLogs,
    required this.canvasHeight,
  });

  final HudTelemetry telemetry;
  final HudConfig config;
  final String statusLabel;
  final String ip;
  final String uptimeLabel;
  final String account;
  final String message;
  final String advice;
  final String todayLabel;
  final String weekLabel;
  final String dropsLabel;
  final String rttLabel;
  final String lossLabel;
  final String trafficLabel;
  final VoidCallback? onLogin;
  final VoidCallback? onLogout;
  final VoidCallback? onReconnect;
  final VoidCallback? onDiagnose;
  final VoidCallback onExportLogs;
  final double canvasHeight;

  @override
  State<DashboardView> createState() => _DashboardViewState();
}

class _DashboardViewState extends State<DashboardView> {
  late Timer _phaseTimer;
  double _phase = 0;

  @override
  void initState() {
    super.initState();
    // 和 Python 版一致：不跑 60fps 动画，1 秒推进一次相位，省 CPU。
    _phaseTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      setState(() => _phase += 1);
    });
  }

  @override
  void dispose() {
    _phaseTimer.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    final TextStyle monoBody = TextStyle(
      color: palette.text,
      fontSize: 22,
      fontFamily: HudMetrics.fontMono,
      fontFamilyFallback: HudMetrics.fontMonoFallback,
    );

    return ListView(
      padding: const EdgeInsets.fromLTRB(
        HudMetrics.pad,
        0,
        HudMetrics.pad,
        HudMetrics.pad,
      ),
      children: <Widget>[
        // --- HUD 画布 ------------------------------------------------
        ClipRRect(
          borderRadius: BorderRadius.circular(HudMetrics.radius),
          child: Container(
            height: widget.canvasHeight,
            decoration: BoxDecoration(
              color: palette.panelSunk,
              border: Border.all(color: palette.border, width: HudMetrics.hairline),
            ),
            child: CustomPaint(
              painter: HudPainter(
                config: widget.config,
                telemetry: widget.telemetry,
                phase: _phase,
              ),
              size: Size.infinite,
            ),
          ),
        ),
        const SizedBox(height: HudMetrics.gap),

        // --- hero 读数 -------------------------------------------------
        HudPanel(
          child: Row(
            children: <Widget>[
              Expanded(
                child: ReadoutBlock(
                  label: '状态',
                  value: widget.statusLabel,
                  style: TextStyle(
                    color: colorFromHex(widget.telemetry.stateColor, palette.offline),
                    fontSize: HudMetrics.sizeHero,
                    fontWeight: FontWeight.w700,
                    fontFamily: HudMetrics.fontMono,
                    fontFamilyFallback: HudMetrics.fontMonoFallback,
                    shadows: widget.config.glow
                        ? <Shadow>[
                            Shadow(
                              color: colorFromHex(widget.telemetry.stateColor, palette.offline),
                              blurRadius: 14,
                            ),
                          ]
                        : null,
                  ),
                ),
              ),
              Expanded(
                child: ReadoutBlock(label: '本机 IP', value: widget.ip, style: monoBody),
              ),
              Expanded(
                child: ReadoutBlock(
                  label: '在线时长',
                  value: widget.uptimeLabel,
                  style: monoBody.copyWith(color: palette.cyan),
                ),
              ),
              Expanded(
                child: ReadoutBlock(
                  label: '账号',
                  value: widget.account,
                  style: monoBody.copyWith(color: palette.textDim, fontSize: 16),
                ),
              ),
            ],
          ),
        ),

        // --- 告警条 -----------------------------------------------------
        if (widget.message.isNotEmpty) ...<Widget>[
          const SizedBox(height: HudMetrics.gap),
          HudPanel(
            color: palette.panelAlt,
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            borderLeft: BorderSide(color: palette.cyan, width: 3),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Text(
                  widget.message,
                  style: TextStyle(color: palette.textDim, fontSize: HudMetrics.sizeBody),
                ),
                if (widget.advice.isNotEmpty) ...<Widget>[
                  const SizedBox(height: 6),
                  Text(
                    widget.advice,
                    style: TextStyle(color: palette.amber, fontSize: HudMetrics.sizeBody),
                  ),
                ],
              ],
            ),
          ),
        ],

        // --- 动作行 -----------------------------------------------------
        const SizedBox(height: HudMetrics.gap),
        Wrap(
          spacing: 10,
          runSpacing: 10,
          children: <Widget>[
            HudActionButton(
              label: '登录',
              icon: Icons.power_settings_new,
              accent: palette.green,
              filled: true,
              onPressed: widget.onLogin,
            ),
            HudActionButton(
              label: '注销',
              icon: Icons.logout,
              accent: palette.amber,
              onPressed: widget.onLogout,
            ),
            HudActionButton(
              label: '重新连接',
              icon: Icons.refresh,
              accent: palette.cyan,
              onPressed: widget.onReconnect,
            ),
            HudActionButton(
              label: '端口诊断',
              icon: Icons.medical_services_outlined,
              accent: palette.violet,
              onPressed: widget.onDiagnose,
            ),
            HudActionButton(
              label: '导出日志',
              icon: Icons.save_alt,
              accent: palette.textDim,
              onPressed: widget.onExportLogs,
            ),
          ],
        ),

        // --- 统计栏 -----------------------------------------------------
        const SizedBox(height: HudMetrics.gap),
        Row(
          children: <Widget>[
            Expanded(child: StatCell(label: '今日在线', value: widget.todayLabel)),
            Expanded(child: StatCell(label: '本周在线', value: widget.weekLabel)),
            Expanded(child: StatCell(label: '掉线次数', value: widget.dropsLabel)),
            Expanded(child: StatCell(label: '延迟', value: widget.rttLabel)),
            Expanded(child: StatCell(label: '丢包', value: widget.lossLabel)),
            Expanded(child: StatCell(label: '本次流量', value: widget.trafficLabel)),
          ],
        ),
      ],
    );
  }
}
