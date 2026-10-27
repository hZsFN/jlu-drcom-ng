/// 账号页 —— 对齐 Python 版 `drcom/ui/app.py` 的 `_view_account` / `_accounts_panel`。
///
/// 上半是「账号与网卡」表单（学号 / MAC / 备注 / 密码 + 自动登录开关 + 保存），
/// 下半是账号列表（圆点表示当前账号，可切换 / 删除）。
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:jlu_drcom/jlu_drcom.dart';

import '../controller.dart';
import 'dashboard.dart' show HudActionButton, HudPanel, HudThemeScope;
import 'theme.dart';

class AccountView extends StatefulWidget {
  const AccountView({super.key, required this.controller});

  final DrcomController controller;

  @override
  State<AccountView> createState() => _AccountViewState();
}

class _AccountViewState extends State<AccountView> {
  final TextEditingController _accountField = TextEditingController();
  final TextEditingController _macField = TextEditingController();
  final TextEditingController _labelField = TextEditingController();
  final TextEditingController _passwordField = TextEditingController();
  final FocusNode _passwordFocus = FocusNode();

  bool _autoLogin = false;
  String _flash = '';
  bool _flashOk = true;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_onControllerChanged);
    _loadActive();
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onControllerChanged);
    _accountField.dispose();
    _macField.dispose();
    _labelField.dispose();
    _passwordField.dispose();
    _passwordFocus.dispose();
    super.dispose();
  }

  void _onControllerChanged() {
    if (mounted) setState(() {});
  }

  /// 把当前账号的内容装进表单。
  void _loadActive() {
    final Account? active = widget.controller.activeAccount;
    _accountField.text = active?.account ?? '';
    _macField.text = active?.mac ?? '';
    _labelField.text = active?.label ?? '';
    _autoLogin = active?.autoLogin ?? false;
    _passwordField.text = '';
  }

  void _setFlash(String message, bool ok) {
    setState(() {
      _flash = message;
      _flashOk = ok;
    });
  }

  /// 保存表单。返回是否成功（用于测试与后续调用）。
  bool _save() {
    final DrcomController controller = widget.controller;
    Account? target = controller.activeAccount;
    if (target == null) {
      target = controller.createAccount(
        account: '',
        mac: Account.defaultMacPlaceholder,
      );
      if (target == null) {
        _setFlash('创建账号失败', false);
        return false;
      }
    }

    final String account = _accountField.text.trim();
    final String mac = _macField.text.trim().toUpperCase();
    final String label = _labelField.text.trim();

    if (account.isEmpty) {
      _setFlash('账号不能为空', false);
      return false;
    }
    if (mac.isNotEmpty) {
      try {
        macToBytes(mac);
      } on FormatException catch (exc) {
        _setFlash(exc.message, false);
        return false;
      }
    }

    controller.updateAccount(
      target.id,
      account: account,
      mac: mac,
      label: label,
    );
    // autoLogin 不在 updateAccount 的参数里，单独写回。
    final Account? refreshed = controller.accounts
        .where((Account a) => a.id == target!.id)
        .firstOrNull;
    refreshed?.autoLogin = _autoLogin;

    final List<String> notes = <String>[];
    if (_passwordField.text.isNotEmpty) {
      final bool ok = controller.setPassword(target.id, _passwordField.text);
      notes.add(ok ? '密码已保存' : '密码保存失败');
      if (ok) _passwordField.text = '';
    }
    controller.persist();
    notes.add('配置已保存');
    _setFlash(notes.join('；'), true);
    return true;
  }

  void _activate(Account account) {
    widget.controller.selectAccount(account.id);
    _loadActive();
    _setFlash('已切换到 ${account.displayName}', true);
  }

  void _delete(Account account) {
    if (widget.controller.accounts.length <= 1) {
      _setFlash('至少保留一个账号', false);
      return;
    }
    widget.controller.deleteAccount(account.id);
    _loadActive();
    _setFlash('已删除 ${account.displayName}', true);
  }

  void _addAccount() {
    final Account? created = widget.controller.createAccount(
      account: '',
      mac: Account.defaultMacPlaceholder,
      label: '账号 ${widget.controller.accounts.length + 1}',
    );
    if (created == null) {
      _setFlash('新增账号失败', false);
      return;
    }
    widget.controller.persist();
    _loadActive();
    _setFlash('已新增账号，请填写信息', true);
  }

  /// 当前账号 + 密码存储方式的一行说明（对齐 `_refresh_credentials_note`）。
  String get _credentialNote {
    final Account? active = widget.controller.activeAccount;
    if (active == null) return '';
    final String stored = active.passwordProtected.isEmpty ? '未设置' : '已加密保存';
    return '当前账号 ${active.maskedAccount} · ${active.mac.isEmpty ? '(无 MAC)' : active.mac} · 密码存储：$stored';
  }

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(
        HudMetrics.pad,
        0,
        HudMetrics.pad,
        HudMetrics.pad,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          if (widget.controller.running) _Note(text: '引擎正在运行，先断开才能改账号'),
          if (_flash.isNotEmpty)
            _Note(text: _flash, accent: _flashOk ? palette.green : palette.danger),
          HudPanel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                _PanelTitle(text: '账号与网卡'),
                const SizedBox(height: HudMetrics.gap),
                _HudField(
                  controller: _accountField,
                  label: '学号 / 账号',
                  hint: '例如 2023xxxxxxxx',
                ),
                const SizedBox(height: HudMetrics.gap),
                _HudField(
                  controller: _macField,
                  label: '网卡 MAC',
                  hint: 'AA:BB:CC:DD:EE:FF',
                  inputFormatters: <TextInputFormatter>[
                    UpperCaseTextFormatter(),
                  ],
                ),
                const SizedBox(height: HudMetrics.gap),
                _HudField(
                  controller: _labelField,
                  label: '备注名（可选）',
                  hint: '例如 宿舍以太网',
                ),
                const SizedBox(height: HudMetrics.gap),
                _HudField(
                  controller: _passwordField,
                  focusNode: _passwordFocus,
                  label: '密码',
                  hint: '留空表示不修改',
                  obscure: true,
                ),
                const SizedBox(height: HudMetrics.gap),
                Row(
                  children: <Widget>[
                    Text(
                      '开机后自动登录此账号',
                      style: TextStyle(
                        color: palette.textDim,
                        fontSize: HudMetrics.sizeLabel,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Switch(
                      value: _autoLogin,
                      activeThumbColor: palette.green,
                      onChanged: (bool v) => setState(() => _autoLogin = v),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  _credentialNote,
                  style: TextStyle(
                    color: palette.textMuted,
                    fontSize: HudMetrics.sizeMicro,
                  ),
                ),
                const SizedBox(height: HudMetrics.gap),
                Wrap(
                  spacing: HudMetrics.gap,
                  runSpacing: HudMetrics.gap,
                  children: <Widget>[
                    HudActionButton(
                      label: '保存',
                      icon: Icons.save_outlined,
                      accent: palette.green,
                      filled: true,
                      onPressed: _save,
                    ),
                    HudActionButton(
                      label: '新增账号',
                      icon: Icons.add,
                      accent: palette.cyan,
                      onPressed: _addAccount,
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: HudMetrics.gap),
          HudPanel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                _PanelTitle(
                  text: '账号列表（${widget.controller.accounts.length}）',
                ),
                const SizedBox(height: HudMetrics.gap),
                if (widget.controller.accounts.isEmpty)
                  Text(
                    '暂无账号。',
                    style: TextStyle(
                      color: palette.textMuted,
                      fontSize: HudMetrics.sizeLabel,
                    ),
                  )
                else
                  for (final Account account in widget.controller.accounts)
                    _AccountRow(
                      account: account,
                      active: account.id == widget.controller.config.activeAccountId,
                      palette: palette,
                      onActivate: () => _activate(account),
                      onDelete: () => _delete(account),
                    ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _AccountRow extends StatelessWidget {
  const _AccountRow({
    required this.account,
    required this.active,
    required this.palette,
    required this.onActivate,
    required this.onDelete,
  });

  final Account account;
  final bool active;
  final HudPalette palette;
  final VoidCallback onActivate;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final TextStyle mono = TextStyle(
      color: palette.textMuted,
      fontFamily: HudMetrics.fontMono,
      fontFamilyFallback: HudMetrics.fontMonoFallback,
      fontSize: HudMetrics.sizeMicro,
    );
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: active ? palette.panelAlt : null,
        border: Border.all(color: palette.border, width: HudMetrics.hairline),
        borderRadius: BorderRadius.circular(HudMetrics.radius),
      ),
      child: Row(
        children: <Widget>[
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              color: active ? palette.green : palette.border,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 10),
          Text(
            account.displayName,
            style: TextStyle(color: palette.text, fontSize: HudMetrics.sizeBody),
          ),
          const SizedBox(width: 12),
          Text(account.mac.isEmpty ? '(无 MAC)' : account.mac, style: mono),
          const SizedBox(width: 12),
          Text(
            account.passwordProtected.isEmpty ? '无密码' : '已存密码',
            style: mono,
          ),
          const Spacer(),
          HudActionButton(
            label: '切换',
            icon: Icons.swap_horiz,
            accent: palette.cyan,
            onPressed: active ? null : onActivate,
          ),
          const SizedBox(width: 8),
          HudActionButton(
            label: '删除',
            icon: Icons.delete_outline,
            accent: palette.danger,
            onPressed: onDelete,
          ),
        ],
      ),
    );
  }
}

/// HUD 风格输入框。
class _HudField extends StatelessWidget {
  const _HudField({
    required this.controller,
    required this.label,
    this.hint = '',
    this.obscure = false,
    this.focusNode,
    this.inputFormatters,
  });

  final TextEditingController controller;
  final String label;
  final String hint;
  final bool obscure;
  final FocusNode? focusNode;
  final List<TextInputFormatter>? inputFormatters;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Text(
          label,
          style: TextStyle(
            color: palette.textMuted,
            fontSize: HudMetrics.sizeMicro,
            letterSpacing: 0.6,
          ),
        ),
        const SizedBox(height: 4),
        TextField(
          controller: controller,
          focusNode: focusNode,
          obscureText: obscure,
          inputFormatters: inputFormatters,
          cursorColor: palette.cyan,
          style: TextStyle(
            color: palette.text,
            fontFamily: HudMetrics.fontMono,
            fontFamilyFallback: HudMetrics.fontMonoFallback,
            fontSize: HudMetrics.sizeBody,
          ),
          decoration: InputDecoration(
            isDense: true,
            hintText: hint,
            hintStyle: TextStyle(
              color: palette.textMuted.withValues(alpha: 0.7),
              fontSize: HudMetrics.sizeLabel,
            ),
            filled: true,
            fillColor: palette.panelSunk,
            contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(HudMetrics.radius),
              borderSide: BorderSide(color: palette.border, width: HudMetrics.hairline),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(HudMetrics.radius),
              borderSide: BorderSide(color: palette.cyan, width: HudMetrics.hairline),
            ),
          ),
        ),
      ],
    );
  }
}

class _PanelTitle extends StatelessWidget {
  const _PanelTitle({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    return Row(
      children: <Widget>[
        Container(width: 3, height: 12, color: palette.cyan),
        const SizedBox(width: 6),
        Text(
          text,
          style: TextStyle(
            color: palette.textDim,
            fontSize: HudMetrics.sizeLabel,
            letterSpacing: 1.2,
          ),
        ),
      ],
    );
  }
}

class _Note extends StatelessWidget {
  const _Note({required this.text, this.accent});

  final String text;
  final Color? accent;

  @override
  Widget build(BuildContext context) {
    final HudPalette palette = HudThemeScope.of(context);
    final Color tone = accent ?? palette.amber;
    return Container(
      margin: const EdgeInsets.only(bottom: HudMetrics.gap),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: palette.panelAlt,
        border: Border(left: BorderSide(color: tone, width: 3)),
        borderRadius: BorderRadius.circular(HudMetrics.radius),
      ),
      child: Row(
        children: <Widget>[
          Icon(Icons.info_outline, size: 14, color: tone),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              text,
              style: TextStyle(color: palette.textDim, fontSize: HudMetrics.sizeLabel),
            ),
          ),
        ],
      ),
    );
  }
}

/// MAC / 账号输入统一转大写。
class UpperCaseTextFormatter extends TextInputFormatter {
  @override
  TextEditingValue formatEditUpdate(
    TextEditingValue oldValue,
    TextEditingValue newValue,
  ) {
    return newValue.copyWith(text: newValue.text.toUpperCase());
  }
}
