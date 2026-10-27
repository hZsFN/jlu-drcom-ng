/// 顶部标题栏与导航，复刻 Python 版 `_build_header` / `_build_nav`。
library;

import 'package:flutter/material.dart';

import 'dashboard.dart' show HudThemeScope;
import 'theme.dart';

/// 五个视图的名字，顺序即导航顺序。
const List<String> hudViews = <String>['状态', '日志', '账号', '设置', '关于'];

/// 顶部标题栏：品牌条 + 状态胶囊 + 保活周期 + 时钟。
class HudHeader extends StatelessWidget {
  const HudHeader({
    super.key,
    required this.stateLabel,
    required this.stateColor,
    required this.keepalivePhase,
    required this.clock,
  });

  final String stateLabel;
  final Color stateColor;
  final double keepalivePhase;
  final String clock;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    final TextStyle mono = TextStyle(
      fontFamily: HudMetrics.fontMono,
      fontFamilyFallback: HudMetrics.fontMonoFallback,
    );

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: HudMetrics.pad, vertical: 12),
      decoration: BoxDecoration(
        color: palette.panel,
        border: Border(bottom: BorderSide(color: palette.border, width: HudMetrics.hairline)),
      ),
      child: Row(
        children: <Widget>[
          Container(width: 4, height: 26, color: palette.green),
          const SizedBox(width: 14),
          Text(
            'JLU · DRCOM NG',
            style: mono.copyWith(
              color: palette.green,
              fontSize: HudMetrics.sizeTitle + 3,
              fontWeight: FontWeight.w700,
              shadows: <Shadow>[Shadow(color: palette.green, blurRadius: 10)],
            ),
          ),
          const SizedBox(width: 14),
          Text(
            '校园网认证',
            style: TextStyle(
              color: palette.textMuted,
              fontSize: HudMetrics.sizeLabel,
            ),
          ),
          const SizedBox(width: 14),
          // 状态胶囊
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              color: palette.panelAlt,
              borderRadius: BorderRadius.circular(3),
              border: Border.all(color: palette.border, width: HudMetrics.hairline),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(color: stateColor, shape: BoxShape.circle),
                ),
                const SizedBox(width: 7),
                Text(
                  stateLabel,
                  style: mono.copyWith(color: stateColor, fontSize: HudMetrics.sizeLabel),
                ),
              ],
            ),
          ),
          const Spacer(),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              Text(
                '保活周期',
                style: mono.copyWith(color: palette.textMuted, fontSize: HudMetrics.sizeMicro),
              ),
              const SizedBox(height: 3),
              SizedBox(
                width: 120,
                height: 4,
                child: LinearProgressIndicator(
                  value: keepalivePhase.clamp(0.0, 1.0),
                  backgroundColor: palette.panelAlt,
                  valueColor: AlwaysStoppedAnimation<Color>(palette.green),
                  minHeight: 4,
                ),
              ),
            ],
          ),
          const SizedBox(width: 14),
          Text(
            clock,
            style: mono.copyWith(color: palette.textDim, fontSize: HudMetrics.sizeBody),
          ),
        ],
      ),
    );
  }
}

/// 顶部视图导航：选中项用青色底 + 下划线。
class HudNav extends StatelessWidget {
  const HudNav({super.key, required this.active, required this.onSelect});

  final String active;
  final ValueChanged<String> onSelect;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Padding(
      padding: const EdgeInsets.only(
        left: HudMetrics.pad,
        right: HudMetrics.pad,
        top: 8,
        bottom: 4,
      ),
      child: Row(
        children: <Widget>[
          for (final String name in hudViews)
            Padding(
              padding: const EdgeInsets.only(right: 6),
              child: _NavItem(
                label: name,
                active: name == active,
                palette: palette,
                onTap: () => onSelect(name),
              ),
            ),
        ],
      ),
    );
  }
}

class _NavItem extends StatelessWidget {
  const _NavItem({
    required this.label,
    required this.active,
    required this.palette,
    required this.onTap,
  });

  final String label;
  final bool active;
  final HudPalette palette;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: active ? palette.cyanWash : Colors.transparent,
      borderRadius: BorderRadius.circular(HudMetrics.radius),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(HudMetrics.radius),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 9),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(HudMetrics.radius),
            border: active
                ? Border(bottom: BorderSide(color: palette.cyan, width: 2))
                : null,
          ),
          child: Text(
            label,
            style: TextStyle(
              color: active ? palette.cyan : palette.textMuted,
              fontSize: HudMetrics.sizeBody,
              shadows: active
                  ? <Shadow>[Shadow(color: palette.cyan, blurRadius: 6)]
                  : null,
            ),
          ),
        ),
      ),
    );
  }
}
