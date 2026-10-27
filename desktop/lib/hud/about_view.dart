/// 关于页。
library;

import 'package:flutter/material.dart';

import 'dashboard.dart' show HudPanel, HudThemeScope;
import 'theme.dart';

class AboutView extends StatelessWidget {
  const AboutView({super.key});

  static const String version = '2.0.0-dev';

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return ListView(
      padding: const EdgeInsets.fromLTRB(
        HudMetrics.pad,
        0,
        HudMetrics.pad,
        HudMetrics.pad,
      ),
      children: <Widget>[
        HudPanel(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                'JLU · DRCOM NG',
                style: TextStyle(
                  color: palette.green,
                  fontSize: 26,
                  fontWeight: FontWeight.w700,
                  fontFamily: HudMetrics.fontMono,
                  fontFamilyFallback: HudMetrics.fontMonoFallback,
                  shadows: <Shadow>[Shadow(color: palette.green, blurRadius: 10)],
                ),
              ),
              const SizedBox(height: 6),
              Text(
                '吉林大学校园网 DrCOM 认证客户端 · Dart / Flutter 实现',
                style: TextStyle(color: palette.textDim, fontSize: HudMetrics.sizeBody),
              ),
              const SizedBox(height: 14),
              _Row(label: '版本', value: version),
              _Row(label: '实现', value: 'Dart ${_dartVersion()} · Flutter 桌面端'),
              _Row(label: '协议', value: 'DrCOM 5.2.0(D) · UDP 61440'),
              _Row(label: '配置', value: r'%APPDATA%\JLU-DrCOM-NG\config.json'),
              _Row(label: '状态', value: r'%APPDATA%\JLU-DrCOM-NG\status.json'),
            ],
          ),
        ),
        const SizedBox(height: HudMetrics.gap),
        HudPanel(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                '关于这两套实现',
                style: TextStyle(
                  color: palette.cyan,
                  fontSize: HudMetrics.sizeTitle,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                '仓库里同时存在 Python（Flet）和 Dart（Flutter）两套实现，'
                '共用同一份配置文件和状态文件，所以随时可以换着用，不用重新录账号。'
                'Dart 版不需要常驻 Python 进程。',
                style: TextStyle(color: palette.textDim, fontSize: HudMetrics.sizeBody, height: 1.6),
              ),
            ],
          ),
        ),
        const SizedBox(height: HudMetrics.gap),
        HudPanel(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                '声明',
                style: TextStyle(
                  color: palette.cyan,
                  fontSize: HudMetrics.sizeTitle,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                '本项目仅用于个人校园网接入，与吉林大学及 DrCOM 官方无关。'
                '密码使用 Windows DPAPI 加密后保存在本机，不上传任何服务器。',
                style: TextStyle(color: palette.textMuted, fontSize: HudMetrics.sizeBody, height: 1.6),
              ),
            ],
          ),
        ),
      ],
    );
  }

  static String _dartVersion() => '3.x';
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          SizedBox(
            width: 72,
            child: Text(
              label,
              style: TextStyle(color: palette.textMuted, fontSize: HudMetrics.sizeLabel),
            ),
          ),
          Expanded(
            child: SelectableText(
              value,
              style: TextStyle(
                color: palette.text,
                fontSize: HudMetrics.sizeLabel,
                fontFamily: HudMetrics.fontMono,
                fontFamilyFallback: HudMetrics.fontMonoFallback,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
