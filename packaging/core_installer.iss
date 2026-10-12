#define AppName "Karaoke Studio Core"
#define AppVersion "0.1.0"
#define AppPublisher "Karaoke Studio"
#define AppExeName "KaraokeStudioCore.exe"

[Setup]
AppId={{D3C38E40-3C4A-4A0E-8D49-1B0E5F7F9A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Karaoke Studio Core
DefaultGroupName=Karaoke Studio Core
OutputDir=..\dist\installer
OutputBaseFilename=KaraokeStudioCoreSetup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
DisableProgramGroupPage=yes

[Files]
Source: "..\dist\KaraokeStudioCore\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autodesktop}\Karaoke Studio Core"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Karaoke Studio Core"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Karaoke Studio Core"; Flags: nowait postinstall skipifsilent
