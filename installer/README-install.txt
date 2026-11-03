装好了，三步就能用。

一、首次启动
  从开始菜单打开「JLU DrCOM NG」，进「账号」页：
  1. 填学号
  2. 点「自动识别 MAC」（或手填 AA:BB:CC:DD:EE:FF）
  3. 填密码
  4. 点保存

  密码在保存时立刻加密，磁盘上不会有明文。

二、自检（可选，不联网）
  JLU-DrCOM-NG.exe --selftest     协议向量、报文长度、加密后端
  JLU-DrCOM-NG.exe --doctor       61440 端口能不能绑，不能的话为什么

  两条都没副作用，出问题先跑这两条。

三、开机自启
  装的时候勾了「开机自动启动」，安装程序会注册一个计划任务（任务名 JLU-DrCOM-NG）：
  登录瞬间由任务计划服务并行拉起，优先级高于普通程序，比注册表 Run 项快一拍——
  认证要抢在浏览器、微信这些联网程序之前完成，否则它们会先弹「无 Internet」。
  没勾的话，程序「设置」页也能开，开了同样走计划任务；注册表 Run 项只作兜底。
  想确认有没有生效：
    schtasks /Query /TN "JLU-DrCOM-NG"

几件事先说明：
  · 默认装在 C:\Program Files\JLU-DrCOM-NG，安装时 Windows 会弹一次「是否允许此应用
    对你的设备进行更改」，点「是」就行。不想提权的话用 setup.exe /CURRENTUSER，
    会装到 %LOCALAPPDATA%\Programs\JLU-DrCOM-NG。
  · 卸载走「设置 → 应用」或开始菜单里的卸载项（同样要点一次 UAC）。
  · 你的账号配置、日志、流量统计在 %APPDATA%\JLU-DrCOM-NG，卸载时故意不删，
    重装后还能接着用；要彻底清掉请自己删这个目录。
  · 这是校园网认证客户端，只做登录与保活，不改系统网络配置、不装驱动、不驻留系统服务。
  · 许可证 AGPL-3.0：源码公开；若你改动过它并对外提供网络服务，改动也要按同样的许可公开。
  · 安装包还没做代码签名，第一次运行可能被 SmartScreen 拦成蓝色的
    「Windows 已保护你的电脑」——点「更多信息」→「仍要运行」。
    SHA256 见 release 页面的 SHA256SUMS.txt，对一下更放心。

有问题去 https://github.com/hZsFN/jlu-drcom-ng/issues 提。
