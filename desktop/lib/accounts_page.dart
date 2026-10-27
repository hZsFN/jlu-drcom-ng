import 'package:flutter/material.dart';
import 'package:jlu_drcom/jlu_drcom.dart';

import 'controller.dart';

/// 账号管理页：增删改、切换当前账号、设置密码。
///
/// 引擎在跑的时候不让动账号（[DrcomController] 会拒绝并给提示），
/// 免得切到一半状态错乱。
class AccountsPage extends StatelessWidget {
  const AccountsPage({super.key, required this.controller});

  final DrcomController controller;

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: controller,
      builder: (BuildContext context, _) {
        final accounts = controller.accounts;
        final activeId = controller.config.activeAccountId;
        return Scaffold(
          appBar: AppBar(
            title: const Text('账号'),
            actions: <Widget>[
              IconButton(
                tooltip: '刷新（重读配置文件）',
                icon: const Icon(Icons.refresh),
                onPressed: controller.running ? null : controller.reload,
              ),
              IconButton(
                tooltip: '添加账号',
                icon: const Icon(Icons.person_add_alt_1),
                onPressed: controller.running
                    ? null
                    : () => _showAccountDialog(context, controller),
              ),
            ],
          ),
          body: SafeArea(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                if (controller.running)
                  const _Banner('引擎正在运行，先断开才能改账号'),
                Expanded(
                  child: accounts.isEmpty
                      ? const _EmptyHint()
                      : ListView.separated(
                          padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                          itemCount: accounts.length,
                          separatorBuilder: (_, _) =>
                              const SizedBox(height: 10),
                          itemBuilder: (BuildContext context, int index) {
                            final account = accounts[index];
                            return _AccountCard(
                              account: account,
                              selected: account.id == activeId,
                              enabled: !controller.running,
                              onSelect: () =>
                                  controller.selectAccount(account.id),
                              onEdit: () =>
                                  _showAccountDialog(context, controller,
                                      existing: account),
                              onPassword: () =>
                                  _showPasswordDialog(context, controller,
                                      existing: account),
                              onDelete: () =>
                                  _confirmDelete(context, controller, account),
                            );
                          },
                        ),
                ),
                _FooterHint(
                  configPath: controller.store.path,
                  count: accounts.length,
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Future<void> _showAccountDialog(
    BuildContext context,
    DrcomController controller, {
    Account? existing,
  }) async {
    final result = await showDialog<_AccountFormResult>(
      context: context,
      builder: (_) => _AccountDialog(existing: existing),
    );
    if (result == null) return;
    if (existing == null) {
      controller.createAccount(
        account: result.account,
        mac: result.mac,
        label: result.label,
      );
    } else {
      controller.updateAccount(
        existing.id,
        account: result.account,
        mac: result.mac,
        label: result.label,
      );
    }
  }

  Future<void> _showPasswordDialog(
    BuildContext context,
    DrcomController controller, {
    required Account existing,
  }) async {
    final result = await showDialog<String>(
      context: context,
      builder: (_) => _PasswordDialog(accountLabel: existing.maskedAccount),
    );
    if (result == null || result.isEmpty) return;
    controller.setPassword(existing.id, result);
  }

  Future<void> _confirmDelete(
    BuildContext context,
    DrcomController controller,
    Account account,
  ) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => AlertDialog(
        title: const Text('删除账号'),
        content: Text(
          '要删除 ${account.maskedAccount} 吗？\n'
          '它保存的密码会一起删掉，这一步不能撤销。',
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (ok == true) {
      controller.deleteAccount(account.id);
    }
  }
}

class _AccountCard extends StatelessWidget {
  const _AccountCard({
    required this.account,
    required this.selected,
    required this.enabled,
    required this.onSelect,
    required this.onEdit,
    required this.onPassword,
    required this.onDelete,
  });

  final Account account;
  final bool selected;
  final bool enabled;
  final VoidCallback onSelect;
  final VoidCallback onEdit;
  final VoidCallback onPassword;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      elevation: 0,
      color: selected
          ? scheme.primaryContainer.withValues(alpha: 0.45)
          : scheme.surfaceContainerHighest.withValues(alpha: 0.35),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(
          color: selected ? scheme.primary : scheme.outlineVariant,
          width: selected ? 1.4 : 0.8,
        ),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: enabled && !selected ? onSelect : null,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 12, 6, 12),
          child: Row(
            children: <Widget>[
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Row(
                      children: <Widget>[
                        Text(
                          account.maskedAccount,
                          style: const TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        if (selected) ...<Widget>[
                          const SizedBox(width: 8),
                          _Chip(
                            text: '当前',
                            color: scheme.primary,
                          ),
                        ],
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      <String>[
                        if (account.label.isNotEmpty) account.label,
                        account.mac,
                      ].join(' · '),
                      style: TextStyle(
                        fontSize: 12,
                        color: scheme.onSurfaceVariant,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      account.hasPassword ? '已保存密码' : '还没有密码',
                      style: TextStyle(
                        fontSize: 12,
                        color: account.hasPassword
                            ? scheme.onSurfaceVariant
                            : scheme.error,
                      ),
                    ),
                  ],
                ),
              ),
              PopupMenuButton<String>(
                enabled: enabled,
                tooltip: '更多',
                onSelected: (String value) {
                  switch (value) {
                    case 'edit':
                      onEdit();
                    case 'password':
                      onPassword();
                    case 'select':
                      onSelect();
                    case 'delete':
                      onDelete();
                  }
                },
                itemBuilder: (_) => <PopupMenuEntry<String>>[
                  const PopupMenuItem<String>(
                    value: 'select',
                    child: Text('设为当前'),
                  ),
                  const PopupMenuItem<String>(
                    value: 'password',
                    child: Text('设置密码'),
                  ),
                  const PopupMenuItem<String>(
                    value: 'edit',
                    child: Text('编辑'),
                  ),
                  const PopupMenuDivider(),
                  const PopupMenuItem<String>(
                    value: 'delete',
                    child: Text('删除'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({required this.text, required this.color});

  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.18),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: color.withValues(alpha: 0.6), width: 0.8),
      ),
      child: Text(
        text,
        style: TextStyle(fontSize: 11, color: color),
      ),
    );
  }
}

class _Banner extends StatelessWidget {
  const _Banner(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 10, 16, 0),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
      decoration: BoxDecoration(
        color: scheme.tertiaryContainer.withValues(alpha: 0.4),
        borderRadius: BorderRadius.circular(9),
      ),
      child: Row(
        children: <Widget>[
          Icon(Icons.info_outline, size: 16, color: scheme.onSurfaceVariant),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              text,
              style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
            ),
          ),
        ],
      ),
    );
  }
}

class _EmptyHint extends StatelessWidget {
  const _EmptyHint();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            Icon(Icons.person_off_outlined, size: 40, color: scheme.outline),
            const SizedBox(height: 12),
            Text(
              '还没有账号',
              style: TextStyle(color: scheme.onSurfaceVariant),
            ),
            const SizedBox(height: 6),
            Text(
              '右上角「添加账号」建一个，\n或者用 Python 版客户端登录过一次让它自己写进来。',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 12,
                color: scheme.outline,
                height: 1.5,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _FooterHint extends StatelessWidget {
  const _FooterHint({required this.configPath, required this.count});

  final String configPath;
  final int count;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 10),
      child: Text(
        '共 $count 个 · 配置文件 $configPath',
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(fontSize: 11, color: scheme.outline),
      ),
    );
  }
}

class _AccountFormResult {
  const _AccountFormResult({
    required this.account,
    required this.mac,
    required this.label,
  });

  final String account;
  final String mac;
  final String label;
}

class _AccountDialog extends StatefulWidget {
  const _AccountDialog({this.existing});

  final Account? existing;

  @override
  State<_AccountDialog> createState() => _AccountDialogState();
}

class _AccountDialogState extends State<_AccountDialog> {
  late final TextEditingController _account = TextEditingController(
    text: widget.existing?.account ?? '',
  );
  late final TextEditingController _mac = TextEditingController(
    text: (widget.existing?.mac.isNotEmpty ?? false)
        ? widget.existing!.mac
        : Account.defaultMacPlaceholder,
  );
  late final TextEditingController _label = TextEditingController(
    text: widget.existing?.label ?? '',
  );

  @override
  void dispose() {
    _account.dispose();
    _mac.dispose();
    _label.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final editing = widget.existing != null;
    return AlertDialog(
      title: Text(editing ? '编辑账号' : '添加账号'),
      content: SizedBox(
        width: 380,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            TextField(
              controller: _account,
              autofocus: true,
              decoration: const InputDecoration(
                labelText: '学号 / 账号',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _mac,
              decoration: const InputDecoration(
                labelText: 'MAC 地址',
                helperText: '留占位符就用本机网卡的 MAC',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _label,
              decoration: const InputDecoration(
                labelText: '备注（可空）',
                border: OutlineInputBorder(),
              ),
            ),
          ],
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () {
            final account = _account.text.trim();
            if (account.isEmpty) return;
            Navigator.of(context).pop(
              _AccountFormResult(
                account: account,
                mac: _mac.text,
                label: _label.text,
              ),
            );
          },
          child: Text(editing ? '保存' : '添加'),
        ),
      ],
    );
  }
}

class _PasswordDialog extends StatefulWidget {
  const _PasswordDialog({required this.accountLabel});

  final String accountLabel;

  @override
  State<_PasswordDialog> createState() => _PasswordDialogState();
}

class _PasswordDialogState extends State<_PasswordDialog> {
  final TextEditingController _password = TextEditingController();
  bool _obscure = true;

  @override
  void dispose() {
    _password.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text('${widget.accountLabel} 的密码'),
      content: SizedBox(
        width: 380,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            TextField(
              controller: _password,
              autofocus: true,
              obscureText: _obscure,
              decoration: InputDecoration(
                labelText: '密码',
                border: const OutlineInputBorder(),
                suffixIcon: IconButton(
                  tooltip: _obscure ? '显示' : '隐藏',
                  icon: Icon(
                    _obscure ? Icons.visibility_off : Icons.visibility,
                  ),
                  onPressed: () => setState(() => _obscure = !_obscure),
                ),
              ),
            ),
            const SizedBox(height: 10),
            Text(
              '用 Windows DPAPI 加密后写进配置文件，明文不落盘，'
              '只有当前登录的 Windows 用户能解开。',
              style: TextStyle(
                fontSize: 12,
                height: 1.5,
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
            ),
          ],
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () {
            final text = _password.text;
            if (text.isEmpty) return;
            Navigator.of(context).pop(text);
          },
          child: const Text('保存'),
        ),
      ],
    );
  }
}
