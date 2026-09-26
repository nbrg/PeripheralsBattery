; Inno Setup script: per-user install (no admin prompt), Start menu entry,
; optional "Start with Windows", clean uninstall. Built by CI:
;   ISCC /DAppVersion=0.2.0 installer\PeripheralsBattery.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppExe "PeripheralsBattery.exe"

[Setup]
AppId={{8F3C2A51-6B0E-4C7D-9A2E-5B1D7E4F9C30}
AppName=Peripherals Battery
AppVersion={#AppVersion}
AppVerName=Peripherals Battery {#AppVersion}
AppPublisher=Peripherals Battery
AppPublisherURL=https://github.com/nbrg/PeripheralsBattery
DefaultDirName={localappdata}\Programs\PeripheralsBattery
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist
OutputBaseFilename=PeripheralsBattery-{#AppVersion}-setup
SetupIconFile=..\docs\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName=Peripherals Battery
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#AppVersion}
LicenseFile=..\LICENSE

[Tasks]
Name: "autostart"; Description: "Start Peripherals Battery with Windows"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\dist\PeripheralsBattery\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; files of an older version that the new one no longer ships
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\Peripherals Battery"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\Peripherals Battery"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--autostart on"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Parameters: "--autostart off"; Tasks: not autostart; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Start Peripherals Battery now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "StopApp"
Filename: "{app}\{#AppExe}"; Parameters: "--autostart off"; Flags: runhidden waituntilterminated; RunOnceId: "NoAutostart"

[Code]
// Close a running copy before files are replaced (the tray app has no window
// the Restart Manager could politely ask to close).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#AppExe}', '', SW_HIDE,
       ewWaitUntilTerminated, Code);
  Result := '';
end;
