; V Bridge installer (Inno Setup 6). Build via packaging\build.ps1 or:
;   ISCC.exe packaging\installer.iss /DMyAppVersion=2.0.0
#ifndef MyAppVersion
  #define MyAppVersion "2.0.0"
#endif
#define MyAppName "V Bridge"
#define MyAppExe "VBridgeService.exe"

[Setup]
AppId={{7E1D7A2E-5C1F-4B7B-9C62-0B1D6E2A9F31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=Q-BIT
AppPublisherURL=https://t.me/R4ND0MQB1T
AppSupportURL=https://t.me/R4ND0MQB1T
AppCopyright=Copyright (C) 2026 Q-BIT
VersionInfoCompany=Q-BIT
VersionInfoProductName=V Bridge
VersionInfoDescription=V Bridge installer: ATAS X to MetaTrader 5 trade bridge
VersionInfoCopyright=Copyright (C) 2026 Q-BIT
VersionInfoVersion={#MyAppVersion}.0
SetupIconFile=assets\vbridge.ico
UninstallDisplayIcon={app}\{#MyAppExe}
DefaultDirName={localappdata}\Programs\VBridge
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=VBridge-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; service (PyInstaller one-folder output)
Source: "..\dist\VBridgeService\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; indicator into ATAS X (ATAS X hot-reloads indicator DLLs; no need to close it)
Source: "..\indicator\bin\Release\VBridge.dll"; DestDir: "{userappdata}\ATAS X\Indicators"; Flags: ignoreversion

[Icons]
Name: "{userstartup}\V Bridge"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"
Name: "{userprograms}\V Bridge"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"
Name: "{userdesktop}\V Bridge"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Start V Bridge now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/IM {#MyAppExe} /F"; Flags: runhidden; RunOnceId: "KillService"

[UninstallDelete]
Type: files; Name: "{userappdata}\ATAS X\Indicators\VBridge.dll"
Type: files; Name: "{userappdata}\ATAS X\VBridge\pairing.json"

[Code]
var
  NoticePage: TOutputMsgMemoWizardPage;

procedure InitializeWizard;
begin
  NoticePage := CreateOutputMsgMemoPage(wpWelcome, 'Please read', 'How V Bridge works and what it needs',
    'V Bridge sends your ATAS trades to your MT5 account.',
    'What it does' + #13#10 +
    '- You trade on the ATAS X chart as usual. V Bridge mirrors the DIRECTION of your position to the MT5 ' +
    'account(s) you enable, sized by the risk %, stop and target you set on each chart.' + #13#10 +
    '- When your ATAS position goes flat, the MT5 position is closed.' + #13#10#13#10 +
    'What it needs' + #13#10 +
    '- ATAS X installed. - MetaTrader 5 running and logged in, with the Algo Trading button ON.' + #13#10 +
    '- After install: tick your account in the V Bridge tray icon, then add the "V Bridge" indicator to your chart.' + #13#10#13#10 +
    'Notice' + #13#10 +
    'Some prop firms restrict copiers or external software. Check your firm''s rules; using V Bridge is your decision.' + #13#10 +
    'New accounts are disabled until you tick them. The kill switch in the tray blocks new trades at any time.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { stop a running service so files can be replaced; it is restarted by the post-install step }
  Exec('taskkill.exe', '/IM {#MyAppExe} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
