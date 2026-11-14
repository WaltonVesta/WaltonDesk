; ---------------------------------------------------------------------------
; WaltonDesk v5.0.0 — Windows 安装程序（Inno Setup 7，阶段 10.3）
;
; 编译:
;     iscc waltondesk_installer.iss
; 产物:
;     dist\WaltonDesk_setup-5.0.0.exe
;
; 前置：先跑 PyInstaller 产出 dist\WaltonDesk\ 目录
;     uv run --group package pyinstaller waltondesk.spec --noconfirm
;
; 关键决策（每条都是刻意为之，改前先读理由）：
;   1. 默认装到 {autopf}\WaltonDesk（32/64 位自动落到 Program Files 对应目录）。
;   2. 开机自启是**可选勾选项**，写 HKCU\Software\Microsoft\Windows\CurrentVersion\Run。
;      用 HKCU 不用 HKLM：本应用按当前用户跑被控端，且 HKCU 写入不需要管理员
;      权限（PERUSER 安装同样能生效）。值名 "WaltonDesk" 必须与
;      src/rdc/host/autostart.py 的 SERVICE_NAME 一致 —— 应用内托盘勾选与安装器
;      勾选操作的是同一个注册表值，两边状态才会互认同步。
;   3. 卸载时清 Run 值：装机者勾了自启又直接卸载却留下自启项，等于开机反复
;      拉起不存在的程序（Windows 每次弹"找不到文件"）。
;   4. 不装到公共桌面以外的地方，也不写共享 DLL 目录。
;   5. 卸载前提示将被控端进程结束：被控端常驻托盘会锁住 exe 文件，
;      不提示直接删会把文件标记为"重启后删除"，留下脏目录。
; ---------------------------------------------------------------------------

#define MyAppName "WaltonDesk"
#define MyAppVersion "5.0.0"
#define MyAppPublisher "WaltonDesk Team"
#define MyAppExeName "WaltonDesk.exe"
#define MySourceDir "dist\WaltonDesk"

[Setup]
; AppId 保持 GUID 固定：升级安装靠它识别"同一个产品"，改了会导致
; 新旧版本并存而不是覆盖升级。
AppId={{8F1C2E54-7A3B-4D9E-9C6F-0A5D1E8B7423}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://example.invalid/waltondesk
AppSupportURL=https://example.invalid/waltondesk
AppUpdatesURL=https://example.invalid/waltondesk

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no

; 允许 per-user 安装（无管理员时自动装到 {localappdata}\Programs），
; 与 HKCU 自启项配合，非管理员机器也能完整用。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog output

OutputDir=dist
OutputBaseFilename={#MyAppName}_setup-{#MyAppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 中文界面（Inno Setup 7 自带 Default.isl；如需完整中文可放 ChineseSimplified.isl）
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=no

[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"
; 开机自启：写 HKCU Run，值名与应用内 AutostartManager.SERVICE_NAME 一致。
; 默认不勾选（AutoCheck yes 让用户可自由取消）——被控端是"远程控制入口"，
; 未经用户明确同意不得默认开机常驻。
Name: "autostart"; Description: "开机自动启动 WaltonDesk 被控端（静默，托盘常驻）"; \
  GroupDescription: "运行选项："; Flags: unchecked

[Files]
; PyInstaller -D 产物整目录搬迁。 recursesubdirs createallsubdirs 保证
; _internal\ 下的 Qt 插件、FFmpeg DLL、pb/ 与 theme/ 数据目录一个不漏。
Source: "{#MySourceDir}\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
; 说明文档随包（存在才装，缺了不报错，避免文档收口顺序卡住构建）
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
; 数据根 ~/.waltondesk 由应用首启自建，这里**不**预建：预建空目录会让
; paths.migrate_legacy_root() 的"新根已有内容"判定语义变得含混。

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  Tasks: desktopicon
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} 卸载"; Filename: "{uninstallexe}"

[Registry]
; 开机自启可选勾选项 → HKCU Run。
; 值内容带 serve --tray --silent：与 src/rdc/host/autostart.py 的
; launch_command()（frozen 分支）逐字一致，应用内托盘勾选与安装器勾选操作的是
; 同一个注册表值，两边状态互认同步。
; **不加** createvalueifdoesntexist：用户可能先在托盘里勾过自启（值指向旧安装
; 路径），重装到不同目录时若因"值已存在"跳过写入，开机就会拉起不存在的路径。
; uninsdeletevalue：卸载时删值（应用内后来改过该值时 Inno 仍按写入方记账处理）。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "WaltonDesk"; \
  ValueData: """{app}\{#MyAppExeName}"" serve --tray --silent"; \
  Tasks: autostart; Flags: uninsdeletevalue

[Run]
; 装完直接启动（默认起 Launcher 让用户选被控端/主控端）。
; nowait：GUI 形态由 desktop_entry 释放控制台，不等它退出。
Filename: "{app}\{#MyAppExeName}"; \
  Description: "立即启动 {#MyAppName}"; \
  WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前温和结束被控端（它常驻托盘会锁住 exe）。
; taskkill /T 连子进程一起收；/F 必需——被控端事件循环不响应 WM_CLOSE。
; RunOnceId 唯一标识，重试卸载不会重复排队。
Filename: "{sys}\taskkill.exe"; \
  Parameters: "/IM {#MyAppExeName} /T /F"; \
  Flags: runhidden; RunOnceId: "KillWaltonDesk"

[UninstallDelete]
; 兜底删除整个安装目录：PyInstaller -D 的 _internal\ 下有上千个文件，逐个
; 记账删除时若有文件被占用会留下残壳；声明式整目录删除更干净。
; 注意：**不含** ~\.waltondesk（用户配置/证书/口令）——卸载不该销毁用户数据。
Type: filesandordirs; Name: "{app}"

[Code]
// 卸载确认：被控端可能正在被远程使用中，静默杀掉会让远端操作者毫无防备。
function InitializeUninstall(): Boolean;
begin
  Result := True;
  if Not MsgBox(
    '卸载前请确认：' + #13#10 +
    '· 被控端进程将被结束（正在进行的远程连接会中断）；' + #13#10 +
    '· 个人数据目录 ~\.waltondesk（配置、证书、口令）不会被删除，' +
    '需要彻底清除请手动删除该目录。' + #13#10 + #13#10 +
    '继续卸载？',
    '确认卸载', mbConfirmation, IDYES) = IDNO then
  begin
    Result := False;
    Exit;
  end;
end;

// 升级安装：检测同目录旧版本，提示先卸载（PyInstaller -D 产物整目录覆盖
// 容易留下孤儿 DLL —— 旧 _internal 里被删掉的文件不会被自动清理）。
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
                           'WaltonDesk', ExpandConstant('{app}\WaltonDesk.exe serve --tray --silent')) then
      Log('WaltonDesk: 开机自启已注册')
    else
      Log('WaltonDesk: 开机自启未注册（用户未勾选）');
  end;
end;
