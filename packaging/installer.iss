; Inno Setup script for XP12 DEM Generator.
; Built by CI after PyInstaller:  iscc /DMyAppVersion=0.1.0 packaging\installer.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "XP12 DEM Generator"
#define MyAppExe "XP12DEMGenerator.exe"

[Setup]
AppId={{6F3E2B8A-4C1D-4E7B-9A52-2D7C1B0E9F31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Jumanja991xx
AppPublisherURL=https://github.com/Jumanja991xx/XP12-DEM-Generator
AppSupportURL=https://github.com/Jumanja991xx/XP12-DEM-Generator/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install by default - no administrator rights needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=XP12DEMGenerator-Setup-{#MyAppVersion}
SetupIconFile=..\xp12dem\resources\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\THIRD_PARTY_NOTICES.md

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\XP12DEMGenerator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

; Built scenery packs stay in X-Plane and the DEM cache in %LOCALAPPDATA%\XP12DEMGenerator;
; uninstalling the program removes neither.
