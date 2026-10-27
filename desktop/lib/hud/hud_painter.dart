/// HUD 仪表盘绘制，逐函数移植自 Python 版 `drcom/ui/hud.py`。
///
/// 设计的核心是把网络指标映射成航空平显：速率＝空速带、在线时长＝高度带、
/// 保活周期＝航向带、延迟＝俯仰、丢包＝滚转。移植时保留了原实现里那些
/// 「为什么这么写」的注释，因为那些坑是实测踩出来的。
library;

import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import 'theme.dart';

/// HUD 需要的一切数据。
///
/// 喂**平滑后**的值：滤波是调用方的责任，因为原始 RTT / 速率读数天生抖动，
/// 一个抽搐的 HUD 比一个略滞后的 HUD 更糟。
@immutable
class HudTelemetry {
  const HudTelemetry({
    this.online = false,
    this.stateLabel = '离线',
    this.stateColor = '#7C8FA3',
    this.ip = '',
    this.account = '',
    this.uptime = 0,
    this.rxRate = 0,
    this.txRate = 0,
    this.sessionRx = 0,
    this.sessionTx = 0,
    this.rttMs,
    this.lossPercent,
    this.jitterMs,
    this.keepalivePhase = 0,
    this.drops = 0,
    this.attempts = 0,
    this.detail = '',
    this.extra = const <String, Object?>{},
  });

  final bool online;
  final String stateLabel;
  final String stateColor;
  final String ip;
  final String account;
  final double uptime;
  final double rxRate;
  final double txRate;
  final int sessionRx;
  final int sessionTx;
  final double? rttMs;
  final double? lossPercent;
  final double? jitterMs;

  /// 保活周期内的进度，0..1。
  final double keepalivePhase;
  final int drops;
  final int attempts;
  final String detail;
  final Map<String, Object?> extra;

  static const HudTelemetry empty = HudTelemetry();
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

/// 1/2/5×10^n 的刻度步长，给出大约 [targetDivisions] 个刻度。
double niceStep(double span, {int targetDivisions = 5}) {
  if (span <= 0) return 1.0;
  final double raw = span / math.max(1, targetDivisions);
  final double magnitude = math.pow(10, (math.log(raw) / math.ln10).floor()).toDouble();
  for (final double multiplier in <double>[1, 2, 5, 10]) {
    if (raw <= magnitude * multiplier) return magnitude * multiplier;
  }
  return magnitude * 10;
}

/// 带滞后的刻度尺，避免整条带子在 1/2/5 边界上乱跳。
class TapeScale {
  TapeScale(this.minimum) : step = niceStep(minimum, targetDivisions: 5);

  final double minimum;
  double step;

  /// 选一个让当前值大致居中的步长。
  ///
  /// 区间刻意留得宽：每帧从原始值重算步长，会让整条带子在跨过 1/2/5 边界时
  /// 跳一下，那是抖动的主要来源。
  double update(double value, {int divisions = 6}) {
    final double span = math.max(value.abs(), minimum) * 2.6;
    if (span > step * divisions * 1.6 || span < step * divisions * 0.45) {
      step = niceStep(span, targetDivisions: divisions);
    }
    return step;
  }
}

/// 字符宽度占字号的比值。实测：一个 CJK 字形占满 em（1.00），
/// 拉丁字母或数字约 0.60。两者用同一个比值，会让「账号」这个标签压到
/// 旁边的值上 —— 「账号」在 12 px 下是 24 px，而不是 0.62 预测的 14.9 px。
const double _wideAdvance = 1.00;
const double _narrowAdvance = 0.60;

/// 读数标签与值之间的间隙。
const double _labelGap = 8.0;

/// 该字符是否占满一个 em（CJK 及其伙伴）。
bool isWide(int code) =>
    (code >= 0x1100 && code <= 0x115F) || // Hangul Jamo
    (code >= 0x2E80 && code <= 0xA4CF) || // CJK 部首 到 彝文
    (code >= 0xAC00 && code <= 0xD7A3) || // Hangul 音节
    (code >= 0xF900 && code <= 0xFAFF) || // CJK 兼容表意
    (code >= 0xFE30 && code <= 0xFE6F) || // CJK 兼容形式
    (code >= 0xFF00 && code <= 0xFF60) || // 全角形式
    (code >= 0xFFE0 && code <= 0xFFE6) ||
    (code >= 0x20000 && code <= 0x3FFFD); // CJK 扩展 B 及以后

/// [text] 在 [size] 下的估算宽度（像素）。
double advance(String text, double size) {
  double total = 0;
  for (final int code in (text.isEmpty ? '' : text).runes) {
    total += size * (isWide(code) ? _wideAdvance : _narrowAdvance);
  }
  return total;
}

// ---------------------------------------------------------------------------
// 格式化
// ---------------------------------------------------------------------------

String formatMs(double? value) => value == null ? '--' : '${value.toStringAsFixed(0)}ms';

String formatBytes(double value) {
  double v = math.max(0, value);
  for (final String unit in <String>['B', 'K', 'M', 'G']) {
    if (v.abs() < 1024 || unit == 'G') {
      return unit == 'B' ? '${v.toStringAsFixed(0)}$unit' : '${v.toStringAsFixed(1)}$unit';
    }
    v /= 1024.0;
  }
  return '${v.toStringAsFixed(1)}G';
}

String formatRate(double value) => '${formatBytes(value)}/s';

String formatDuration(double seconds) {
  final int s = math.max(0, seconds).toInt();
  if (s < 3600) {
    final int m = s ~/ 60;
    return '$m:${(s % 60).toString().padLeft(2, '0')}';
  }
  final int h = s ~/ 3600;
  return '$h:${((s % 3600) ~/ 60).toString().padLeft(2, '0')}';
}

// ---------------------------------------------------------------------------
// 绘制器
// ---------------------------------------------------------------------------

/// HUD 配置：装饰开关、发光、扫描线动画。
@immutable
class HudConfig {
  const HudConfig({
    this.palette = HudPalette.standard,
    this.decorations = true,
    this.glow = true,
    this.scanlineAnimation = true,
    this.reduceMotion = false,
  });

  final HudPalette palette;
  final bool decorations;
  final bool glow;
  final bool scanlineAnimation;
  final bool reduceMotion;

  double get sizeMicro => HudMetrics.sizeMicro;
  double get sizeLabel => HudMetrics.sizeLabel;
  double get sizeBody => HudMetrics.sizeBody;
  double get sizeTitle => HudMetrics.sizeTitle;
  double get sizeHero => HudMetrics.sizeHero;
}

/// 绘制一帧 HUD。
class HudPainter extends CustomPainter {
  HudPainter({
    required this.config,
    required this.telemetry,
    this.phase = 0,
  });

  final HudConfig config;
  final HudTelemetry telemetry;
  final double phase;

  // 保留带的宽度，避免仪表与四角读数打架。
  static const double topBand = 78.0;
  static const double bottomBand = 62.0;
  static const double tapeInset = 104.0;

  // 刻度尺状态要跨帧保留（滞后逻辑靠它）。
  static final TapeScale _speedScale = TapeScale(512.0);
  static final TapeScale _altScale = TapeScale(60.0);

  late Canvas _canvas;
  late Size _size;
  double get _w => math.max(200.0, _size.width);
  double get _h => math.max(140.0, _size.height);
  HudPalette get _p => config.palette;

  // -- 基础绘制 -----------------------------------------------------------

  Paint _stroke(Color color, double width, [List<double>? dash]) {
    final Paint paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = width
      ..isAntiAlias = true;
    if (dash != null) paint.strokeCap = StrokeCap.butt;
    return paint;
  }

  Paint _fill(Color color) => Paint()
    ..color = color
    ..style = PaintingStyle.fill
    ..isAntiAlias = true;

  void line(
    double x1,
    double y1,
    double x2,
    double y2,
    Color color, {
    double width = 1.0,
    List<double>? dash,
  }) {
    if (dash == null) {
      _canvas.drawLine(Offset(x1, y1), Offset(x2, y2), _stroke(color, width));
      return;
    }
    _dashedLine(x1, y1, x2, y2, color, width, dash);
  }

  /// Flutter 没有内置虚线，按 on/off 长度手动切段。
  void _dashedLine(
    double x1,
    double y1,
    double x2,
    double y2,
    Color color,
    double width,
    List<double> dash,
  ) {
    final double dx = x2 - x1;
    final double dy = y2 - y1;
    final double total = math.sqrt(dx * dx + dy * dy);
    if (total <= 0) return;
    final double ux = dx / total;
    final double uy = dy / total;
    final Paint paint = _stroke(color, width);
    double travelled = 0;
    int index = 0;
    while (travelled < total) {
      final double segment = dash[index % dash.length];
      final double next = math.min(travelled + segment, total);
      if (index.isEven) {
        _canvas.drawLine(
          Offset(x1 + ux * travelled, y1 + uy * travelled),
          Offset(x1 + ux * next, y1 + uy * next),
          paint,
        );
      }
      travelled = next;
      index++;
    }
  }

  void outlineRect(
    double x,
    double y,
    double w,
    double h,
    Color color, {
    double width = 1.0,
    List<double>? dash,
  }) {
    if (dash == null) {
      _canvas.drawRect(
        Rect.fromLTWH(x, y, w, h),
        _stroke(color, width),
      );
      return;
    }
    line(x, y, x + w, y, color, width: width, dash: dash);
    line(x + w, y, x + w, y + h, color, width: width, dash: dash);
    line(x + w, y + h, x, y + h, color, width: width, dash: dash);
    line(x, y + h, x, y, color, width: width, dash: dash);
  }

  void filledRect(double x, double y, double w, double h, Color color) {
    _canvas.drawRect(Rect.fromLTWH(x, y, w, h), _fill(color));
  }

  void circle(double x, double y, double r, Color color, {double width = 1.0, bool fill = false}) {
    _canvas.drawCircle(Offset(x, y), r, fill ? _fill(color) : _stroke(color, width));
  }

  /// 弧度制在内部处理：Flet 的 Arc 吃弧度，这里统一按**度**传参。
  ///
  /// 传错很戏剧化：3 度的刻度当成 3 弧度会扫过约 172 度，七个刻度直接把
  /// 滚转标度画成一整圈。
  void arc(
    double x,
    double y,
    double w,
    double h,
    double startDeg,
    double sweepDeg,
    Color color, {
    double width = 1.5,
    bool useCenter = false,
  }) {
    _canvas.drawArc(
      Rect.fromLTWH(x, y, w, h),
      startDeg * math.pi / 180.0,
      sweepDeg * math.pi / 180.0,
      useCenter,
      _stroke(color, width),
    );
  }

  TextPainter _textPainter(String text, Color color, double size, {bool bold = false}) {
    final List<Shadow>? shadows = config.glow
        ? <Shadow>[Shadow(color: color, blurRadius: 4)]
        : null;
    final TextPainter painter = TextPainter(
      text: TextSpan(
        text: text,
        style: TextStyle(
          color: color,
          fontSize: size,
          fontFamily: HudMetrics.fontMono,
          fontFamilyFallback: HudMetrics.fontMonoFallback,
          fontWeight: bold ? FontWeight.w600 : FontWeight.w400,
          shadows: shadows,
          height: 1.0,
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    return painter;
  }

  void label(
    double x,
    double y,
    String text,
    Color color, {
    double? size,
    String align = 'left',
    bool bold = false,
  }) {
    final double fontSize = size ?? config.sizeMicro;
    final double approxWidth = advance(text, fontSize);
    double dx = x;
    if (align == 'center') {
      dx -= approxWidth / 2;
    } else if (align == 'right') {
      dx -= approxWidth;
    }
    _textPainter(text, color, fontSize, bold: bold).paint(_canvas, Offset(dx, y));
  }

  /// 实底读数框：不透明的底是重点，不管后面有什么网格线或梯级线穿过，
  /// 数字都读得清。
  void valueBox(
    double x,
    double y,
    double w,
    double h,
    String text, {
    required Color accent,
    required double size,
    String align = 'left',
  }) {
    filledRect(x, y, w, h, _p.panelSunk);
    outlineRect(x, y, w, h, accent, width: 1.5);
    const double padding = 7.0;
    final double textY = y + (h - size) / 2 - 1.5;
    if (align == 'left') {
      label(x + padding, textY, text, accent, size: size, bold: true);
    } else if (align == 'right') {
      label(x + w - padding, textY, text, accent, size: size, align: 'right', bold: true);
    } else {
      label(x + w / 2, textY, text, accent, size: size, align: 'center', bold: true);
    }
  }

  // -- 装饰层 -------------------------------------------------------------

  void grid() {
    if (!config.decorations) return;
    final Color color = _p.grid;
    const double spacing = 64.0; // 比 v1 稀：网格之前在和文字抢注意力
    double y = spacing;
    while (y < _h) {
      line(0, y, _w, y, color, width: 1.0);
      y += spacing;
    }
    double x = spacing;
    while (x < _w) {
      line(x, 0, x, _h, color, width: 1.0);
      x += spacing;
    }
  }

  void scanlines({double phase = 0}) {
    if (!config.decorations) return;
    final Color color = _p.scanline;
    // 4 px 间距在约 400 px 的面板上会产生肉眼可见的摩尔纹，和字形打架；
    // 稀一点、暗一点才有质感。
    const double spacing = 7.0;
    double y = phase % spacing;
    while (y < _h) {
      line(0, y, _w, y, color, width: 1.0);
      y += spacing;
    }
    if (config.scanlineAnimation) {
      final double band = (phase * 20.0) % (_h + 160.0) - 80.0;
      filledRect(0, band, _w, 2.0, _p.glowSoft);
    }
  }

  void frameMarks() {
    if (!config.decorations) return;
    final Color color = _p.border;
    const double length = 26.0;
    const double inset = 10.0;
    final List<List<double>> corners = <List<double>>[
      <double>[inset, inset, 1, 1],
      <double>[_w - inset, inset, -1, 1],
      <double>[inset, _h - inset, 1, -1],
      <double>[_w - inset, _h - inset, -1, -1],
    ];
    for (final List<double> c in corners) {
      final double x = c[0];
      final double y = c[1];
      final double dx = c[2];
      final double dy = c[3];
      line(x, y, x + dx * length, y, color, width: 2.0);
      line(x, y, x, y + dy * length, color, width: 2.0);
    }
  }

  // -- 仪表 ---------------------------------------------------------------

  void fpv(double cx, double cy) {
    final Color color = telemetry.online ? _p.green : _p.offline;
    const double radius = 12.0;
    if (telemetry.online) {
      circle(cx, cy, radius, color, width: 2.4);
      circle(cx, cy, 2.5, color, fill: true);
    } else {
      for (final double start in <double>[30, 150, 270]) {
        arc(cx - radius, cy - radius, radius * 2, radius * 2, start, 80, color, width: 2.0);
      }
      line(cx - 15, cy, cx - 5, cy, color, width: 2.0);
      line(cx + 5, cy, cx + 15, cy, color, width: 2.0);
    }
    // 机翼
    line(cx - 44, cy + 8, cx - 15, cy + 8, color, width: 2.6);
    line(cx + 15, cy + 8, cx + 44, cy + 8, color, width: 2.6);
    line(cx - 44, cy + 8, cx - 44, cy + 2, color, width: 2.6);
    line(cx + 44, cy + 8, cx + 44, cy + 2, color, width: 2.6);
    line(cx, cy - 32, cx, cy - 18, color, width: 1.8);
  }

  /// 5 度一档的梯级线：爬升实线在上，俯冲虚线在下。
  ///
  /// 只给每隔一档标数字 —— 九档全标是 v1 里杂乱的主要来源。
  void pitchLadder(double cx, double cy) {
    final Color color = _p.cyan;
    final double pixelsPerDegree = math.max(3.0, (_h - topBand - bottomBand) / 40.0);
    double pitch = 0;
    if (telemetry.rttMs != null) {
      pitch = math.max(-15.0, math.min(15.0, (telemetry.rttMs! - 25.0) / 5.0));
    }

    for (int degrees = -20; degrees <= 20; degrees += 5) {
      if (degrees == 0) continue;
      final double y = cy - (degrees - pitch) * pixelsPerDegree;
      if (y < topBand || y > _h - bottomBand) continue;
      final bool major = degrees % 10 == 0;
      final double half = major ? 96.0 : 70.0;
      final List<double>? dash = degrees < 0 ? <double>[6, 5] : null;
      line(cx - half, y, cx - 34, y, color, width: major ? 1.6 : 1.2, dash: dash);
      line(cx + 34, y, cx + half, y, color, width: major ? 1.6 : 1.2, dash: dash);
      final double tip = degrees > 0 ? -7.0 : 7.0;
      line(cx - half, y, cx - half, y + tip, color, width: 1.6, dash: dash);
      line(cx + half, y, cx + half, y + tip, color, width: 1.6, dash: dash);
      if (major) {
        final String text = degrees > 0 ? '+$degrees' : '$degrees';
        label(cx - half - 30, y - 7, text, color, size: config.sizeMicro + 1, bold: true);
        label(cx + half + 10, y - 7, text, color, size: config.sizeMicro + 1, bold: true);
      }
    }
  }

  /// 左侧带：下行速率，当前值放在实底框里。
  void speedTape(double x) {
    final Color color = _p.cyan;
    final double top = topBand;
    final double bottom = math.max(topBand + 80.0, _h - bottomBand);
    line(x, top, x, bottom, color, width: 1.4);
    line(x + 9, top, x + 9, bottom, color, width: 1.4);
    label(x - 10, top - 22, '下行速率', color, size: config.sizeMicro + 1, align: 'right', bold: true);

    final double value = math.max(0, telemetry.rxRate);
    final double step = _speedScale.update(value);
    final double center = (top + bottom) / 2;
    final double span = bottom - top;
    final double scale = span / math.max(step * 6, 1.0);

    final double startTick = ((value - step * 3) / step).floor() * step;
    for (int index = 0; index < 7; index++) {
      final double tickValue = startTick + index * step;
      if (tickValue < 0) continue;
      final double y = center + (value - tickValue) * scale;
      if (y < top - 4 || y > bottom + 4) continue;
      if (index.isOdd) {
        line(x + 9, y, x + 14, y, color, width: 1.0);
      } else {
        line(x + 9, y, x + 18, y, color, width: 1.2);
        label(x + 26, y - 7, formatRate(tickValue), color, size: config.sizeMicro + 1);
      }
    }

    valueBox(
      x - 74,
      center - 13,
      78,
      26,
      formatRate(value),
      accent: _p.green,
      size: config.sizeBody,
      align: 'left',
    );
  }

  /// 右侧带：本次在线时长，镜像绘制，让标签留在框内。
  void altitudeTape(double x) {
    final Color color = _p.cyan;
    final double top = topBand;
    final double bottom = math.max(topBand + 80.0, _h - bottomBand);
    line(x, top, x, bottom, color, width: 1.4);
    line(x - 9, top, x - 9, bottom, color, width: 1.4);
    label(x + 10, top - 22, '在线时长', color, size: config.sizeMicro + 1, bold: true);

    final double value = math.max(0, telemetry.uptime);
    final double step = _altScale.update(value);
    final double center = (top + bottom) / 2;
    final double span = bottom - top;
    final double scale = span / math.max(step * 6, 1.0);

    final double startTick = ((value - step * 3) / step).floor() * step;
    for (int index = 0; index < 7; index++) {
      final double tickValue = math.max(0.0, startTick + index * step);
      final double y = center + (value - tickValue) * scale;
      if (y < top - 4 || y > bottom + 4) continue;
      if (index.isOdd) {
        line(x - 9 - 5, y, x - 9, y, color, width: 1.0);
      } else {
        line(x - 9 - 9, y, x - 9, y, color, width: 1.2);
        label(x - 26, y - 7, formatDuration(tickValue), color,
            size: config.sizeMicro + 1, align: 'right');
      }
    }

    valueBox(
      x - 4,
      center - 13,
      82,
      26,
      formatDuration(value),
      accent: _p.green,
      size: config.sizeBody,
      align: 'right',
    );
  }

  /// 顶部居中带：保活周期相位。
  ///
  /// 标签量化到 5 度，文字才不闪：按刷新率算，连续变化的标签几乎每帧都变，
  /// 读起来是噪声而不是运动。
  void headingTape() {
    final Color color = _p.cyan;
    const double y = 48.0;
    double left = 268.0;
    double right = _w - 268.0;
    if (right - left < 170) {
      left = _w * 0.24;
      right = _w * 0.76;
    }
    if (right - left < 90) return;
    line(left, y, right, y, color, width: 1.4);

    final double degrees = (telemetry.keepalivePhase % 1.0) * 360.0;
    final int reference = (degrees / 5.0).round() * 5; // 量化：标签才稳
    final double pixelsPerDegree = (right - left) / 120.0;
    final double mid = (left + right) / 2;
    for (int tick = -60; tick <= 60; tick += 10) {
      final double x = mid + tick * pixelsPerDegree;
      if (x < left - 1 || x > right + 1) continue;
      final bool major = tick % 30 == 0;
      line(x, y, x, y - (major ? 10.0 : 6.0), color, width: major ? 1.4 : 1.0);
      if (major) {
        final int heading = ((reference + tick) % 360 + 360) % 360;
        label(x, y - 26, heading.toString().padLeft(3, '0'), color,
            size: config.sizeMicro + 1, align: 'center', bold: true);
      }
    }

    line(mid, y, mid, y - 15, _p.green, width: 2.4);
    final int wrapped = reference <= 180 ? reference : reference - 360;
    double bugX = mid + wrapped * pixelsPerDegree;
    bugX = math.max(left, math.min(right, bugX));
    filledRect(bugX - 3, y + 3, 6, 6, _p.green);
    label(left - 12, y - 8, '保活', color, size: config.sizeMicro + 1, align: 'right', bold: true);
  }

  /// 滚转标度，指针由丢包率驱动。
  ///
  /// `useCenter` 必须是 false：为 true 时 Flutter 画的是**扇形**，
  /// 2 度的扇形从圆心看像一根辐条，会把标度画成星芒。
  void bankArc(double cx, double cy, double radius) {
    final Color color = _p.cyan;
    final double loss = telemetry.lossPercent ?? 0.0;
    final double bank = math.max(-45.0, math.min(45.0, loss * 4.5 - 22.5));

    for (final double mark in <double>[-45, -30, -15, 0, 15, 30, 45]) {
      final bool major = mark % 30 == 0;
      final double sweep = major ? 3.5 : 2.0;
      arc(cx - radius, cy - radius, radius * 2, radius * 2, -90 + mark - sweep / 2, sweep, color,
          width: major ? 3.0 : 1.8);
    }

    final double angleRad = (-90 + bank) * math.pi / 180.0;
    final double inner = radius - 20;
    final double outer = radius - 7;
    line(
      cx + inner * math.cos(angleRad),
      cy + inner * math.sin(angleRad),
      cx + outer * math.cos(angleRad),
      cy + outer * math.sin(angleRad),
      _p.green,
      width: 3.0,
    );
    label(cx, cy - radius - 16, '滚转 / 丢包', color,
        size: config.sizeMicro + 1, align: 'center', bold: true);
  }

  /// 四角带框读数。
  ///
  /// 标签列宽度由最宽的值推导，而不是固定偏移。原先在框右边缘右对齐值、
  /// 标签放左 92 px，隐含假设每个值都窄于 92 px —— 而 `172.18.123.63`
  /// 在这个字号下约 109 px，地址会直接穿过自己的标签。
  void cornerReadout(String corner, List<List<String>> lines) {
    const double lineHeight = 20.0;
    final bool right = corner == 'tr' || corner == 'br';
    final bool bottom = corner == 'bl' || corner == 'br';

    final double sizeValue = config.sizeBody;
    final double sizeKey = config.sizeMicro + 1;

    double widestValue = 0;
    double widestKey = 0;
    for (final List<String> pair in lines) {
      widestValue = math.max(widestValue, advance(pair[1], sizeValue));
      widestKey = math.max(widestKey, advance(pair[0], sizeKey));
    }

    final double total = lines.length * lineHeight;
    final double baseY = bottom ? (_h - 16.0 - total) : 12.0;
    final double baseX = right ? (_w - 18.0) : 18.0;

    // 左侧角保留原来的留白；只有标签会撞上值时，偏移才变大。
    final double keyOffset = math.max(62.0, widestKey + _labelGap);
    final double contentW = widestValue + _labelGap + widestKey;

    final double boxW = math.max(190.0, contentW + 26.0);
    final double boxH = total + 8.0;
    final double boxX = right ? baseX - boxW + 8.0 : baseX - 8.0;
    filledRect(boxX, baseY - 4.0, boxW, boxH, _p.panelSunk);
    outlineRect(boxX, baseY - 4.0, boxW, boxH, _p.border, width: 1.0);

    for (int index = 0; index < lines.length; index++) {
      final String key = lines[index][0];
      final String value = lines[index][1];
      final double y = baseY + index * lineHeight;
      if (right) {
        label(baseX, y, value, _p.text, size: sizeValue, align: 'right', bold: true);
        label(baseX - widestValue - _labelGap, y, key, _p.textMuted,
            size: sizeKey, align: 'right');
      } else {
        label(baseX, y, key, _p.textMuted, size: sizeKey);
        label(baseX + keyOffset, y, value, _p.text, size: sizeValue, bold: true);
      }
    }
  }

  /// 底部模式条，居中放，给四角区块让位。
  void modeBar() {
    final double y = _h - 18;
    final double left = _w * 0.30;
    final double right = _w * 0.70;
    line(left, y - 10, right, y - 10, _p.border, width: 1.0);
    label(left, y - 6, '状态', _p.textMuted, size: config.sizeMicro + 1);
    label(left + 40, y - 7, telemetry.stateLabel,
        colorFromHex(telemetry.stateColor, _p.offline),
        size: config.sizeBody, bold: true);
    if (telemetry.detail.isNotEmpty) {
      final String text =
          telemetry.detail.length > 24 ? telemetry.detail.substring(0, 24) : telemetry.detail;
      label(right, y - 6, text, _p.textDim, size: config.sizeMicro, align: 'right');
    }
  }

  // -- 整帧 ---------------------------------------------------------------

  @override
  void paint(Canvas canvas, Size size) {
    _canvas = canvas;
    _size = size;
    final double cx = _w / 2;
    final double cy = _h / 2;

    grid();
    frameMarks();

    headingTape();
    bankArc(cx, cy, math.min(_h * 0.25, 104.0));
    pitchLadder(cx, cy);
    fpv(cx, cy);
    speedTape(tapeInset);
    altitudeTape(_w - tapeInset);

    // 只画两个角：其它数字已经在画布下方的部件里，重复只会更乱。
    cornerReadout('tl', <List<String>>[
      <String>['延迟', formatMs(telemetry.rttMs)],
      <String>['抖动', formatMs(telemetry.jitterMs)],
    ]);
    cornerReadout('tr', <List<String>>[
      <String>['丢包', telemetry.lossPercent == null ? '--' : '${telemetry.lossPercent!.toStringAsFixed(0)}%'],
      <String>['掉线', '${telemetry.drops}'],
    ]);
    cornerReadout('bl', <List<String>>[
      <String>['接收', formatBytes(telemetry.sessionRx.toDouble())],
      <String>['发送', formatBytes(telemetry.sessionTx.toDouble())],
    ]);
    cornerReadout('br', <List<String>>[
      <String>['账号', telemetry.account.isEmpty ? '--------' : telemetry.account],
      <String>['IP', telemetry.ip.isEmpty ? '--' : telemetry.ip],
    ]);

    modeBar();
    scanlines(phase: phase);
    outlineRect(0.5, 0.5, _w - 1, _h - 1, _p.border, width: 1.0);
  }

  @override
  bool shouldRepaint(covariant HudPainter old) =>
      old.telemetry != telemetry || old.phase != phase || old.config != config;
}

/// 把 `#RRGGBB` / `#AARRGGBB` 解析成 Color，解析不了就用 [fallback]。
Color colorFromHex(String hex, Color fallback) {
  String value = hex.trim();
  if (value.startsWith('#')) value = value.substring(1);
  if (value.length == 6) value = 'FF$value';
  if (value.length != 8) return fallback;
  final int? parsed = int.tryParse(value, radix: 16);
  return parsed == null ? fallback : ui.Color(parsed);
}
