; JLU DrCOM NG —— Inno Setup 6 安装包脚本
;
; 编译（别直接双击 ISCC）：
;     python tools/make_installer.py
; 或手工：
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.1.0 installer\installer.iss
;
; 版本号不是写死在这里：由 make_installer.py 从 git tag / drcom\__init__.py 读出来再传进来，
; 和 make_release.py 保持一致（单一来源）。
;
; 默认装到 {autopf}\JLU-DrCOM-NG（管理员 → C:\Program Files，仅当前用户 → %LOCALAPPDATA%\Programs）：
; 启动时自动申请提权（UAC），因为写 Program Files 需要管理员权限。
; 想只装给当前用户、不弹 UAC：setup.exe /CURRENTUSER
; 用户数据仍在 %APPDATA%\JLU-DrCOM-NG（账号配置、日志、统计），卸载时故意保留。
;
; 签名状态：本机无代码签名证书，setup.exe 是未签名状态，
; 首次运行会被 SmartScreen 拦成"Windows 已保护你的电脑"（需点 更多信息 → 仍要运行）。
; 去掉提示只能买代码签名证书（OV/EV）或 Azure Trusted Signing；签名钩子见 make_installer.py。

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "JLU DrCOM NG"
#define MyAppShortName "JLU-DrCOM-NG"
#define MyAppPublisher "hZsFN"
#define MyAppURL "https://github.com/hZsFN/jlu-drcom-ng"
#define MyAppExeName "JLU-DrCOM-NG.exe"
; 打包源目录：默认相对 installer\ 的上一级；换机器编译可用 /DSourceDir=D:\path\dist\JLU-DrCOM-NG 覆盖
#ifndef SourceDir
  #define SourceDir "..\dist\JLU-DrCOM-NG"
#endif

[Setup]
AppId={{8F3A2B14-6C5D-4E7A-9B21-3D4F5E6A7B8C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoCompany={#MyAppPublisher}
VersionInfoCopyright=MIT License
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes
; 许可页只在 LICENSE 确实存在时启用：把 iss 拷到别处单独编译也不会因此报错
#if FileExists(AddBackslash(SourcePath) + "..\LICENSE")
LicenseFile=..\LICENSE
#endif
InfoAfterFile=README-install.txt
OutputDir=..\release
OutputBaseFilename={#MyAppShortName}-{#MyAppVersion}-setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; admin = 启动时自动弹 UAC 申请提权（默认装进 Program Files，快捷方式对所有用户可见）
; 需要免提权安装的场景可加 /CURRENTUSER，会退回 %LOCALAPPDATA%\Programs
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
CloseApplications=yes
RestartApplications=no
MinVersion=10.0
; 每次运行都写安装日志到 %TEMP%\Setup Log *.txt —— 排查"到底有没有提权"直接看日志里的
; Install mode 那行（administrative / non administrative）
SetupLogging=yes

[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "autostart"; Description: "开机自动启动（静默守护，出错不弹窗）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppShortName}"; ValueData: """{app}\{#MyAppExeName}"" --autostart"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清安装目录里的生成物；%APPDATA%\JLU-DrCOM-NG 里的账号配置/日志/统计一律保留
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// 老版本（1.1.0 之前的 zip 版）可能被手工解压到别处，这里只提示不强删。
procedure CurStepChanged(CurStep: TSetupStep);
var
  LegacyData, AppData: String;
begin
  if CurStep = ssPostInstall then
  begin
    AppData := ExpandConstant('{userappdata}\JLU-DrCOM-NG');
    LegacyData := AppData;
    if DirExists(LegacyData) then
      Log('found existing user data at ' + LegacyData + ' (kept as-is)');
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;
