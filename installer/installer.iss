; JLU DrCOM NG —— Inno Setup 6 安装包脚本
;
; 起点是 Inno Setup 向导生成的 1.iss（桌面那份）：[Setup] 骨架沿用向导的写法，
; 向导留下的几个坑在这里改掉，并在注释里写明为什么改。
;
;   1. 版本号写死 "1.3.0"      → 由 make_installer.py 从 drcom/__init__.py 注入，
;                                程序内部版本 / 安装包文件名 / release 资产名三处一致。
;   2. ChangesAssociations=yes  → 删掉。向导顺手给 .exe 建了 OpenWithProgids 项，
;      + [Registry] 里一串 .exe 关联    等于把自家程序挂进所有 exe 的"打开方式"，
;                                污染系统且与程序无关。
;   3. OutputDir=桌面 / mysetup → release\JLU-DrCOM-NG-<版本>-setup.exe。
;   4. 没有许可页               → AGPL-3.0（仓库根目录 LICENSE）。
;   5. 自启只有 Run 键          → 计划任务（登录瞬间由任务计划服务并行拉起，
;                                优先级高于 normal；Run 键要排队等资源管理器）。
;
; 编译（别直接双击 ISCC，版本号会变成 0.0.0）：
;     python tools/make_installer.py
; 手工编译：
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=2.0.0 installer\installer.iss
;
; 安装位置：管理员 → {autopf}，/CURRENTUSER → %LOCALAPPDATA%\Programs。
; 用户数据在 %APPDATA%\JLU-DrCOM-NG（账号、日志、统计），卸载时故意保留。
;
; 签名：本机无代码签名证书，setup.exe 未签名，首次运行会被 SmartScreen 拦。
; 签名钩子见 tools/make_installer.py（SIGN_PFX / SIGN_CERT_SHA1）。

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "JLU DrCOM NG"
#define MyAppShortName "JLU-DrCOM-NG"
#define MyAppPublisher "hZsFN"
#define MyAppURL "https://github.com/hZsFN/jlu-drcom-ng"
#define MyAppExeName "JLU-DrCOM-NG.exe"
#ifndef SourceDir
  #define SourceDir "..\dist\JLU-DrCOM-NG"
#endif

[Setup]
; AppId 必须与历史安装包一致，否则新版本会在旁边再装一份而不是升级。
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
VersionInfoCopyright=AGPL-3.0
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes
; 许可页只在 LICENSE 确实存在时启用：把 iss 拷到别处单独编译也不会报错
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
WizardStyle=modern dynamic windows11
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; admin：启动即申请提权，装进 Program Files；/CURRENTUSER 可退回按用户安装
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
CloseApplications=yes
RestartApplications=no
MinVersion=10.0
; 每次安装都写 %TEMP%\Setup Log *.txt：查"到底有没有提权"看 Install mode 那行
SetupLogging=yes

[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "autostart"; Description: "开机自动启动（计划任务，登录后立即拉起认证）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 许可全文单独放到安装根目录，卸载前也能翻
#if FileExists(AddBackslash(SourcePath) + "..\LICENSE")
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; （老版本的 HKCU\...\Run 自启值改在 [Code] 里删：写在这里会触发 Inno 的
;  UsedUserAreasWarning —— 提权安装时 HKCU 指向谁容易让人误会。）

[Run]
; 计划任务注册：早于资源管理器、优先级高于 normal。注册失败不影响安装（程序里还能再开）。
Filename: "{app}\{#MyAppExeName}"; Parameters: "--cli autostart task"; Flags: runhidden waituntilterminated; Tasks: autostart; StatusMsg: "正在注册开机自启（计划任务）…"
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--cli autostart off"; Flags: runhidden; RunOnceId: "RemoveAutostart"

[UninstallDelete]
; 只清安装目录里的生成物；%APPDATA%\JLU-DrCOM-NG 里的账号配置/日志/统计一律保留
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// 装完把用户数据位置写进安装日志：提到「数据还在不在」时直接看日志，不用猜。
procedure CurStepChanged(CurStep: TSetupStep);
var
  AppData: String;
begin
  if CurStep = ssPostInstall then
  begin
    AppData := ExpandConstant('{userappdata}\JLU-DrCOM-NG');
    if DirExists(AppData) then
      Log('existing user data kept at ' + AppData);
  end;
end;

// 迁移：老版本把自启写在 HKCU\...\Run。新版改计划任务，这里顺手清掉那个值，
// 免得开机时 Run 键与计划任务各拉起一份。
procedure RemoveLegacyRunValue();
begin
  RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
    '{#MyAppShortName}');
end;

function InitializeSetup(): Boolean;
begin
  RemoveLegacyRunValue();
  Result := True;
end;
