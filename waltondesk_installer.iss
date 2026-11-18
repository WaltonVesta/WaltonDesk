; ---------------------------------------------------------------------------
; WaltonDesk v5.0.0 — Windows 安装程序（Inno Setup 7，阶段 10.3）
;
; 编译:
;     iscc waltondesk_installer.iss
; 产物:
;     dist\WaltonDesk_setup.exe
;
; 前置：先跑 PyInstaller 产出 dist\WaltonDesk\ 目录
;     uv run --group package pyinstaller waltondesk.spec --noconfirm
;
; 关键决策（每条都是刻意为之，改前先读理由）：
;   1. 默认装到 {autopf}\WaltonDesk（64 位系统落 Program Files）。
;   2. 开机自启是**可选勾选项**，写 HKCU\...\Run。用 HKCU 不用 HKLM：本应用按
;      当前用户跑被控端，且 HKCU 写入不需要管理员权限。值名 "WaltonDesk" 必须与
;      src/rdc/host/autostart.py 的 SERVICE_NAME 一致 —— 安装器勾选与应用内托盘
;      勾选操作同一个注册表值，两边状态才互认同步。
;   3. 卸载清 Run 值：勾了自启又直接卸载却留下自启项，等于开机反复拉起不存在
;      的程序（Windows 每次弹"找不到文件"）。
;   4. 不写共享 DLL 目录，不动用户数据根 ~\.waltondesk。
;   5. 卸载前提示并结束被控端进程：它常驻托盘会锁住 exe，不处理会把文件标记
;      "重启后删除"留下脏目录。
;
; ⚠️ Inno Setup 语法约束：**段内条目不支持反斜杠续行**（只有 ISPP 预处理指令
;    支持），所有条目必须单行写完 —— 别按 C/Shell 习惯加 `\`。
; ---------------------------------------------------------------------------

#define MyAppName "WaltonDesk"
#define MyAppVersion "5.0.0"
#define MyAppPublisher "WaltonDesk Team"
#define MyAppURL "https://example.invalid/waltondesk"
#define MyAppExeName "WaltonDesk.exe"
#define MySourceDir "dist\WaltonDesk"
#define MyRegRunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
; AppId 固定 GUID：升级安装靠它识别"同一产品"。改了会导致新旧版本并存而非覆盖升级。
AppId={{8F1C2E54-7A3B-4D9E-9C6F-0A5D1E8B7423}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; 允许 per-user 安装：无管理员权限时装到 {localappdata}\Programs，与 HKCU 自启
; 项配合，非管理员机器也能完整使用。dialog = 安装时让用户在"为当前用户/为所有
; 用户"之间选。（合法值只有 none/commandline/dialog/all，**没有** output ——
; 写错会在编译期直接报 "directive is invalid"。）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

OutputDir=dist
OutputBaseFilename={#MyAppName}_setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; **向导全界面纯中文**（v5.0.0 收口批验收项）：整套向导消息来自 Inno 官方简体
; 中文语言包（欢迎页/许可证/目标位置/选择准备/进度/完成/卸载向导/全部错误框与
; 按钮）。上一版只挂 Default.isl（英文）+ 零星几处自译中文，主体全是英文——
; "割裂"即源于此。现在英文底包退役，自定义条目（[Tasks]/[Icons]/[Run]）全部
; 写成中文字面量，不再依赖 {cm:} 的英文回退。
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; 直接写中文字面量（不用 {cm:CreateDesktopIcon}）：即使语言包缺键也不会漏英文。
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："
; 默认**不勾选**：被控端是"远程控制入口"，未经用户明确同意不得默认开机常驻。
Name: "autostart"; Description: "开机自动启动 WaltonDesk 被控端（静默，托盘常驻）"; GroupDescription: "运行选项："; Flags: unchecked

[Files]
; PyInstaller -D 产物整目录搬迁。recursesubdirs createallsubdirs 保证 _internal\
; 下的 Qt 插件（platforms/qwindows.dll）、FFmpeg DLL、rdc/protocol/pb 与
; rdc/ui/theme（dark.qss + icon.ico）一个不漏 —— 漏任何一个 exe 就闪退。
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 文档随包（存在才装，缺了不报错，避免文档收口顺序卡住构建）
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; 数据根 ~\.waltondesk 由应用首启自建，安装器**不**预建：预建空目录会让
; rdc.config.paths.migrate_legacy_root() 的"新根是否已有内容"判定语义变含混。

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} 卸载"; Filename: "{uninstallexe}"

[Registry]
; 值内容与 rdc/host/autostart.py 的 launch_command()（frozen 分支）逐字一致：
;   "{exe}" serve --tray --silent
; **不加** createvalueifdoesntexist：用户可能先在托盘勾过自启（值指向旧安装
; 路径），重装到不同目录时若因"值已存在"跳过写入，开机就会拉起不存在的路径。
; uninsdeletevalue：卸载时删值。
Root: HKCU; Subkey: "{#MyRegRunKey}"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" serve --tray --silent"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; 装完可直接启动（起 Launcher 让用户选被控端/主控端）。nowait：GUI 形态由
; rdc/desktop_entry.py 释放控制台，安装器不等它退出。
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前结束被控端（常驻托盘会锁住 exe）。/T 连子进程一起收；/F 必需 —— 被控端
; 事件循环不响应 WM_CLOSE。RunOnceId 保证重试卸载不重复排队。
; 不加 skipifdoesntexist：该旗标属 [Run] 段，[UninstallRun] 不支持（编译期报错），
; 且 taskkill.exe 自 Vista 起为系统自带，无需存在性判断。
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#MyAppExeName} /T /F"; Flags: runhidden; RunOnceId: "KillWaltonDesk"

[UninstallDelete]
; 兜底删除整个安装目录：PyInstaller -D 的 _internal\ 有上千文件，逐个记账删除
; 时若有文件被占用会留下残壳；声明式整目录删除更干净。
; **不含** ~\.waltondesk（用户配置/证书/口令）—— 卸载不该销毁用户数据。
Type: filesandordirs; Name: "{app}"

[Code]
// 换行由 Chr(13)+Chr(10) 拼出。三个坑都实测踩过：
//   1. 脚本里出现以 # 开头的 Pascal 字符常量（#13#10）→ ISPP 预处理器把 # 开头
//      的行当预处理指令解析，报 "Unknown preprocessor directive"；
//   2. 写向导常量 NewLine → [Code] 段没有这个标识符，报 "Unknown identifier"；
//   3. MsgBox 只接受 **3 个参数**（文本, 类型, 默认按钮）——它没有标题参数位，
//      多传一个标题会报 "Invalid number of parameters"。
function InitializeUninstall(): Boolean;
var
  NL: String;
begin
  Result := True;
  NL := Chr(13) + Chr(10);
  if MsgBox(
    '确认卸载？' + NL + NL +
    '· 被控端进程将被结束（正在进行的远程连接会中断）；' + NL +
    '· 个人数据目录（配置、证书、口令）不会被删除，' +
    '需要彻底清除请手动删除该目录。',
    mbConfirmation, IDYES) = IDNO then
  begin
    Result := False;
    Exit;
  end;
end;

// 安装收尾：把自启注册现状写进安装日志（排障时能直接看出勾选项是否生效）。
// RegQueryStringValue 的最后一参是 **out 变量**，不能传常量表达式 —— 早期版本
// 这里误传 ExpandConstant(...) 会编译失败，已改为读入变量再打日志。
procedure LogAutostartState;
var
  RunValue: String;
begin
  if RegQueryStringValue(HKCU, '{#MyRegRunKey}', '{#MyAppName}', RunValue) then
    Log('[WaltonDesk] autostart registered: ' + RunValue)
  else
    Log('[WaltonDesk] autostart not registered (user did not tick the option)');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    LogAutostartState;
end;
