; JLU DrCOM NG —— Inno Setup 6 安装包脚本（仓库版，供 tools/make_installer.py 注入版本）
;
; 日常打包请用桌面那份 1.iss（绝对路径、F9 直接编）；这份与它功能逐条一致，
; 只是路径改成相对、版本留成 #ifndef 由 /DMyAppVersion 注入。
;
; 相对向导原版只改四处：版本号、输出目录与文件名、AGPL 许可页、开机自启改计划任务。
; 向导的交互一律保留 —— 特别是下面这两行，改动会让装起来跟向导版不一样：
;   PrivilegesRequiredOverridesAllowed=dialog   安装时可选"为所有人安装 / 仅为我安装"
;   SetupIconFile=..\assets\app.ico             18 KB 多尺寸图标，不是 installer\app.ico（766 B）

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
VersionInfoCopyright=AGPL-3.0
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
InfoAfterFile=README-install.txt
OutputDir=..\release
OutputBaseFilename={#MyAppShortName}-{#MyAppVersion}-setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern dynamic windows11
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 向导原样：安装界面里可选择"为所有人安装 / 仅为我安装"
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no
MinVersion=10.0
SetupLogging=yes

[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "autostart"; Description: "开机自动启动（计划任务，登录后立即拉起认证）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--cli autostart task"; Flags: runhidden waituntilterminated; Tasks: autostart; StatusMsg: "正在注册开机自启（计划任务）…"
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--cli autostart off"; Flags: runhidden; RunOnceId: "RemoveAutostart"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
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
