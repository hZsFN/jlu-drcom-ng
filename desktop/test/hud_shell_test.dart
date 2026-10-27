import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:jlu_drcom_app/hud/dashboard.dart';
import 'package:jlu_drcom_app/hud/shell.dart';
import 'package:jlu_drcom_app/hud/theme.dart';

void main() {
  testWidgets('顶部标题栏显示品牌、状态与时钟', (WidgetTester tester) async {
    final HudPalette palette = HudPalette.standard;
    await tester.pumpWidget(
      MaterialApp(
        home: HudThemeScope(
          palette: palette,
          child: Scaffold(
            body: HudHeader(
              stateLabel: '在线',
              stateColor: palette.green,
              keepalivePhase: 0.5,
              clock: '12:00:00',
            ),
          ),
        ),
      ),
    );
    expect(find.text('JLU · DRCOM NG'), findsOneWidget);
    expect(find.text('在线'), findsOneWidget);
    expect(find.text('12:00:00'), findsOneWidget);
  });

  testWidgets('导航列出五个视图且高亮当前项', (WidgetTester tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: HudThemeScope(
          palette: HudPalette.standard,
          child: Scaffold(
            body: HudNav(active: '日志', onSelect: (_) {}),
          ),
        ),
      ),
    );
    for (final String name in hudViews) {
      expect(find.text(name), findsOneWidget);
    }
    expect(hudViews.length, 5);
  });
}
