#define MyAppName "AI CCTV Server"
#define MyAppVersion "0.3.0"
#define MyAppExeName "AI_CCTV_Configurator.exe"

[Setup]
AppId={{9C5AB807-BB88-4929-982C-D5C7B92D62EA}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\AI_CCTV
DefaultGroupName={#MyAppName}
OutputBaseFilename=AI_CCTV_Server_Setup_{#MyAppVersion}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes

[Files]
Source: "..\..\dist\AI_CCTV_Configurator.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\server\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".env,secrets.env,runtime\*"

[Icons]
Name: "{group}\AI CCTV Configurator"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Configure AI CCTV"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Runtime data intentionally remains under ProgramData. The Configurator must ask
; separately before a user-authorized data reset.
Type: filesandordirs; Name: "{app}\server"
