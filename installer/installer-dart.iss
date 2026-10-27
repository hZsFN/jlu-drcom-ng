; JLU DrCOM NG — Dart/Flutter 桌面版的安装包
;
; 编译：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=2.0.0 installer\installer-dart.iss
;
; 与 installer.iss（Python/Flet 版）的区别：源目录指向 Flutter 的 release 产物，
; 可执行文件名不同，AppId 也不同——两版可以并存安装，互不覆盖。

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "JLU DrCOM NG"
#define MyAppShortName "JLU-DrCOM-NG"
#define MyAppExeName "jlu_drcom_app.exe"
#define MyAppPublisher "hZsFN"
#define MyAppUrl "https://github.com/hZsFN/jlu-drcom-ng"
#define SourceDir "..\desktop\build\windows\x64\runner\Release"

[Setup]
; 和 Python 版不同的 AppId：两版能共存，装这个不会把那个顶掉。
AppId={{B7E4A2F1-5C3D-4A8E-9F12-6D8C0B4A7E53}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion} (Dart)
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}
AppUpdatesURL={#MyAppUrl}
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} 安装程序（Dart 版）
VersionInfoCompany={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir=..\release
OutputBaseFilename={#MyAppShortName}-{#MyAppVersion}-dart-setup
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; 安装前把正在跑的客户端关掉，装完不强行拉起（用户自己开）。
CloseApplications=yes
RestartApplications=no
; 未签名：装的时候 SmartScreen 会拦一次，这是预期行为（见 docs/known-issues.md）。
PrivilegesRequired=admin
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce
Name: "autostart"; Description: "开机自动启动"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKLM64; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JLU-DrCOM-NG"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
