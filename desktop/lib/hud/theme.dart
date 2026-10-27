/// HUD 设计令牌，逐字对齐 Python 版 `drcom/ui/theme.py`。
///
/// 配色是照抄的：每个文本色都在 #0A0E14 上测过 WCAG 对比度，
/// 注释里的比值来自原实现。改色请连注释一起改，别让文档骗人。
library;

import 'package:flutter/painting.dart';

/// 一套完整的、经过对比度校验的配色。
class HudPalette {
  const HudPalette({
    required this.name,
    required this.bg,
    required this.panel,
    required this.panelAlt,
    required this.panelSunk,
    required this.border,
    required this.grid,
    required this.gridAlpha,
    required this.scanline,
    required this.glowSoft,
    required this.text,
    required this.textDim,
    required this.textMuted,
    required this.cyan,
    required this.green,
    required this.amber,
    required this.danger,
    required this.violet,
    required this.offline,
    required this.online,
    required this.warning,
    required this.cyanWash,
    required this.greenWash,
    required this.amberWash,
    required this.dangerWash,
    required this.violetWash,
  });

  final String name;

  // --- 表面层（始终不透明，spec 3.4 第 1 层）----------------------------
  final Color bg; // 窗口 / 页面底色
  final Color panel; // 卡片表面
  final Color panelAlt; // 抬起表面（输入框、表头）
  final Color panelSunk; // 日志控制台、代码块
  final Color border;

  // --- 装饰层（故意半透明，第 2 层）------------------------------------
  final Color grid;
  final Color gridAlpha;
  final Color scanline;
  final Color glowSoft;

  // --- 文本层（第 4 层：不透明 + 发光）---------------------------------
  final Color text;
  final Color textDim;
  final Color textMuted;

  // --- 强调色 -----------------------------------------------------------
  final Color cyan;
  final Color green;
  final Color amber;
  final Color danger;
  final Color violet;
  final Color offline;
  final Color online;
  final Color warning;

  // --- 半透明强调底色（只用于装饰）-------------------------------------
  final Color cyanWash;
  final Color greenWash;
  final Color amberWash;
  final Color dangerWash;
  final Color violetWash;

  /// 引擎状态到颜色的映射，和 Python 版 `state_color` 一一对应。
  Color stateColor(String state) => switch (state) {
    'online' => online,
    'authenticating' => cyan,
    'challenging' => cyan,
    'binding' => cyan,
    'retry_wait' => warning,
    'fatal' => danger,
    'idle' => offline,
    'stopped' => offline,
    _ => text,
  };

  static const HudPalette standard = HudPalette(
    name: 'hud',
    bg: Color(0xFF0A0E14),
    panel: Color(0xFF111823),
    panelAlt: Color(0xFF141C28),
    panelSunk: Color(0xFF070A0F),
    border: Color(0xFF243244),
    grid: Color(0xFF141E2A),
    gridAlpha: Color(0xFF16202C),
    scanline: Color(0xFF0C1118),
    glowSoft: Color(0xFF0E2A33),
    text: Color(0xFFE8F4F8), // 17.25:1
    textDim: Color(0xFFA9BED0), // 10.10:1
    textMuted: Color(0xFF7C8FA3), //  5.82:1
    cyan: Color(0xFF7FE7F5), // 13.49:1
    green: Color(0xFF86FF86), // 15.34:1
    amber: Color(0xFFFFC857), // 12.57:1
    danger: Color(0xFFFF7B7B), //  7.71:1
    violet: Color(0xFFC79BFF), //  8.81:1
    offline: Color(0xFF7C8FA3),
    online: Color(0xFF86FF86),
    warning: Color(0xFFFFC857),
    cyanWash: Color(0x1A7FE7F5),
    greenWash: Color(0x1A86FF86),
    amberWash: Color(0x1AFFC857),
    dangerWash: Color(0x1AFF7B7B),
    violetWash: Color(0x1AC79BFF),
  );

  /// 同样的几何，最大可读性：不发光、文字更亮、装饰更稀。
  static const HudPalette highContrast = HudPalette(
    name: 'high-contrast',
    bg: Color(0xFF0A0E14),
    panel: Color(0xFF111823),
    panelAlt: Color(0xFF141C28),
    panelSunk: Color(0xFF070A0F),
    border: Color(0xFF243244),
    grid: Color(0xFF111823),
    gridAlpha: Color(0xFF16202C),
    scanline: Color(0xFF0A0E14),
    glowSoft: Color(0xFF0A0E14),
    text: Color(0xFFFFFFFF), // 19.60:1
    textDim: Color(0xFFD8E6F2), // 13.76:1
    textMuted: Color(0xFFA9BED0), // 10.10:1
    cyan: Color(0xFF7FE7F5),
    green: Color(0xFF86FF86),
    amber: Color(0xFFFFC857),
    danger: Color(0xFFFF7B7B),
    violet: Color(0xFFC79BFF),
    offline: Color(0xFF7C8FA3),
    online: Color(0xFF86FF86),
    warning: Color(0xFFFFC857),
    cyanWash: Color(0x1A7FE7F5),
    greenWash: Color(0x1A86FF86),
    amberWash: Color(0x1AFFC857),
    dangerWash: Color(0x1AFF7B7B),
    violetWash: Color(0x1AC79BFF),
  );
}

/// 布局与排版度量，对齐 `theme.py` 的 `HUD` 数据类。
abstract final class HudMetrics {
  // 几何
  static const double pad = 14;
  static const double gap = 10;
  static const double radius = 4;
  static const double notch = 12;
  static const double hairline = 1.0;

  // 字体：Flutter 的 fontFamily 只接受单一族名，候选列表交给 fallback。
  static const String fontMono = 'Consolas';
  static const List<String> fontMonoFallback = <String>[
    'Cascadia Mono',
    'DejaVu Sans Mono',
    'monospace',
  ];

  // 字号
  static const double sizeHero = 36;
  static const double sizeTitle = 16;
  static const double sizeBody = 14;
  static const double sizeLabel = 12;
  static const double sizeMicro = 11;
}
