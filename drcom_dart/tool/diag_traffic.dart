import 'dart:convert';
import 'dart:io';

/// 诊断 2：完整复现 TrafficMeter 的采样路径，把异常打出来。
Future<Map<String, (int, int)>?> readCounters(String alias) async {
  try {
    final ProcessResult result = await Process.run(
      'powershell',
      <String>[
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        r"[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            r"Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,SentBytes | ConvertTo-Json -Compress",
      ],
      stdoutEncoding: utf8,
    ).timeout(const Duration(seconds: 5));
    stdout.writeln('exit=${result.exitCode}');
    if (result.exitCode != 0) {
      stdout.writeln('stderr=${result.stderr}');
      return null;
    }
    final Object? decoded = jsonDecode(result.stdout.toString());
    final Map<String, (int, int)> out = <String, (int, int)>{};
    void take(Map<String, dynamic> row) {
      final Object? name = row['Name'];
      if (name is! String) return;
      out[name] = (
        (row['ReceivedBytes'] as num?)?.toInt() ?? 0,
        (row['SentBytes'] as num?)?.toInt() ?? 0,
      );
    }

    if (decoded is List) {
      for (final Object? row in decoded) {
        if (row is Map<String, dynamic>) take(row);
      }
    } else if (decoded is Map<String, dynamic>) {
      take(decoded);
    }
    stdout.writeln('解析出 ${out.length} 条: ${out.keys.toList()}');
    return out;
  } on Object catch (e, st) {
    stdout.writeln('异常: $e');
    stdout.writeln(st.toString().split('\n').take(3).join('\n'));
    return null;
  }
}

Future<void> main(List<String> args) async {
  final String alias = args.isEmpty ? '以太网' : args.first;
  stdout.writeln('alias="$alias" len=${alias.length}');
  final Map<String, (int, int)>? counters = await readCounters(alias);
  stdout.writeln('counters == null ? ${counters == null}');
  if (counters != null) {
    stdout.writeln('命中: ${counters[alias]}');
  }
}
