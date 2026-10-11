; Kikitori（听译）安装包脚本 —— Inno Setup 6
;
; 一般不直接运行，用仓库根目录的 build.ps1 -Installer 生成：
;   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipEngine -SkipApp -Installer
;
; 默认按“当前用户”安装到 %LOCALAPPDATA%\Programs\Kikitori，不需要管理员权限；
; 安装向导里也可以选择为所有用户安装（需要管理员权限）。
; 用户数据（设置、日志、字幕记录）在 %LOCALAPPDATA%\Kikitori，卸载时保留。
;
; 本文件含中文，需保存为 UTF-8 with BOM。

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Kikitori"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName "听译 Kikitori"
#define AppExe "Kikitori.exe"
#define RepoUrl "https://github.com/biubiu223/Kikitori"

[Setup]
AppId={{8F3C2A51-6B7E-4D2A-9C1F-4B7E2D9A6C33}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=biubiu223
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
DefaultDirName={autopf}\Kikitori
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=Kikitori-{#AppVersion}-setup
SetupIconFile=..\app\Kikitori\Assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
; 2GB+ 的 CUDA 运行库：lzma2 多线程压缩
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=6
; 运行中的程序由下面 [Code] 里的逻辑处理（关闭主窗口只会缩到托盘，Restart Manager 关不掉它）
CloseApplications=no

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chs.AppRunning=听译正在运行，需要先关闭它才能继续。%n%n点击“确定”自动关闭（正在进行的识别会停止）。
en.AppRunning=Kikitori is running and must be closed to continue.%n%nClick OK to close it.
chs.AppRunningAbort=请先退出听译（托盘图标或字幕条右键 → 退出），然后再试。
en.AppRunningAbort=Please exit Kikitori first (tray icon or overlay right-click → Exit), then try again.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 升级时先清掉旧引擎，避免旧版本残留的 DLL 混在一起
Type: filesandordirs; Name: "{app}\engine"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
function AppIsRunning(): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq Kikitori.exe" /NH | find /I "Kikitori.exe" >nul',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure KillApp();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM Kikitori.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM KikitoriEngine.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(800);
end;

{ 安装/升级前：程序在运行就先问用户，同意后关闭 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if AppIsRunning() then
  begin
    if SuppressibleMsgBox(CustomMessage('AppRunning'), mbConfirmation, MB_OKCANCEL, IDOK) = IDOK then
      KillApp()
    else
      Result := CustomMessage('AppRunningAbort');
  end;
end;

{ 卸载前：同样处理 }
function InitializeUninstall(): Boolean;
begin
  Result := True;
  if AppIsRunning() then
  begin
    if SuppressibleMsgBox(CustomMessage('AppRunning'), mbConfirmation, MB_OKCANCEL, IDOK) = IDOK then
      KillApp()
    else
    begin
      SuppressibleMsgBox(CustomMessage('AppRunningAbort'), mbInformation, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;
