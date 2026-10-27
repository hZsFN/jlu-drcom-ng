import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:jlu_drcom/jlu_drcom.dart';
import 'package:jlu_drcom_app/controller.dart';
import 'package:jlu_drcom_app/home_page.dart';

void main() {
  testWidgets('空配置时显示未连接，点连接给提示', (WidgetTester tester) async {
    final dir = await tester.runAsync(
      () => Directory.systemTemp.createTemp('drcom-ui-test'),
    );
    addTearDown(() => dir!.delete(recursive: true));
    final controller = DrcomController(
      store: ConfigStore(path: '${dir!.path}${Platform.pathSeparator}config.json'),
    );
    addTearDown(controller.dispose);

    await tester.pumpWidget(
      MaterialApp(home: HomePage(controller: controller)),
    );

    expect(find.text('未连接'), findsOneWidget);
    expect(find.text('还没有日志'), findsOneWidget);

    await tester.tap(find.text('连接'));
    await tester.pump();

    // 点连接后要有明确的失败提示，不能静默什么都不发生。
    expect(find.textContaining('配置里还没有账号'), findsWidgets);
  });
}
