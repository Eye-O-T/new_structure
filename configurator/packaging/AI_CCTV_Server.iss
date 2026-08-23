#define MyAppName "AI CCTV Server"
#ifndef MyAppVersion
  #define MyAppVersion "0.3.0"
#endif
#define MyAppPublisher "AI CCTV"
#define MyGuiExeName "AI_CCTV_Configurator.exe"
#define MyCliExeName "AI_CCTV_CLI.exe"

[Setup]
AppId={{9C5AB807-BB88-4929-982C-D5C7B92D62EA}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\AI_CCTV
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=AI_CCTV_Server_Setup_{#MyAppVersion}_x64
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayIcon={app}\{#MyGuiExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
UsePreviousTasks=yes
ChangesEnvironment=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "AI CCTV CLI를 시스템 PATH에 추가"; GroupDescription: "명령줄 도구:"; Flags: checkedonce

[Dirs]
; Runtime data is deliberately outside Program Files and survives upgrade and
; uninstall. It inherits the administrative ACL instead of granting all local
; Users modify access. Configurator requests elevation before writing here, and
; individual secret files receive an even narrower private DACL.
Name: "{commonappdata}\AI_CCTV"; Flags: uninsneveruninstall

[Files]
Source: "..\..\dist\AI_CCTV_Configurator.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AI_CCTV_CLI.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\.dockerignore"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc,*.pyo,*.egg-info\*"
Source: "..\..\server\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".env,secrets\*.env,secrets\*.json,runtime\*,certs\*,config\config.yaml,__pycache__\*,*.pyc,*.pyo,*.key,*.crt,*.pem"
Source: "..\..\README.md"; DestDir: "{app}\docs"; DestName: "README.md"; Flags: ignoreversion
Source: "..\..\docs\operations\windows-installer.md"; DestDir: "{app}\docs"; DestName: "windows-installation.md"; Flags: ignoreversion

[Icons]
Name: "{group}\AI CCTV Configurator"; Filename: "{app}\{#MyGuiExeName}"; WorkingDir: "{app}"
Name: "{group}\AI CCTV CLI Console"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoExit -Command ""& '{app}\{#MyCliExeName}' --help"""; WorkingDir: "{app}"
Name: "{group}\Installation guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\docs\windows-installation.md"""
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AI CCTV Configurator"; Filename: "{app}\{#MyGuiExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; The packaged Configurator carries an administrator execution manifest. This
; keeps Program Files read-only and limits ProgramData writes to administrators.
Filename: "{app}\{#MyGuiExeName}"; Description: "AI CCTV Configurator를 열어 모델 경로와 서버 설정 지정"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; `docker compose down` removes containers and the private network only. It does
; not delete the bind-mounted database, recordings, snapshots, model or secrets.
Filename: "{app}\{#MyCliExeName}"; Parameters: "--server-dir ""{app}\server"" stop --env-file ""{commonappdata}\AI_CCTV\config\compose.env"""; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAiCctvServices"

[UninstallDelete]
; Remove only the legacy generated env location. ProgramData is intentionally
; never listed here; reinstall discovers the preserved deployment there.
Type: files; Name: "{app}\server\.env"

[Code]
const
  SystemEnvironmentKey =
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function NormalizedPathEntry(Value: String): String;
begin
  Value := Trim(Value);
  if (Length(Value) >= 2) and (Value[1] = '"') and
     (Value[Length(Value)] = '"') then
  begin
    Delete(Value, Length(Value), 1);
    Delete(Value, 1, 1);
  end;
  while (Length(Value) > 3) and (Value[Length(Value)] = '\') do
    Delete(Value, Length(Value), 1);
  Result := Lowercase(Value);
end;

procedure SplitPathEntries(const Value: String; var Entries: TArrayOfString);
var
  Remaining, Entry: String;
  Separator, Count: Integer;
begin
  SetArrayLength(Entries, 0);
  Remaining := Value;
  repeat
    Separator := Pos(';', Remaining);
    if Separator > 0 then
    begin
      Entry := Copy(Remaining, 1, Separator - 1);
      Delete(Remaining, 1, Separator);
    end
    else
    begin
      Entry := Remaining;
      Remaining := '';
    end;
    Count := GetArrayLength(Entries);
    SetArrayLength(Entries, Count + 1);
    Entries[Count] := Entry;
  until Separator = 0;
end;

function PathContainsEntry(const ExistingPath, Candidate: String): Boolean;
var
  Entries: TArrayOfString;
  Index: Integer;
  NormalizedCandidate: String;
begin
  Result := False;
  NormalizedCandidate := NormalizedPathEntry(Candidate);
  SplitPathEntries(ExistingPath, Entries);
  for Index := 0 to GetArrayLength(Entries) - 1 do
    if NormalizedPathEntry(Entries[Index]) = NormalizedCandidate then
    begin
      Result := True;
      Exit;
    end;
end;

procedure AddInstallDirToPath;
var
  ExistingPath, InstallDir: String;
begin
  InstallDir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKLM, SystemEnvironmentKey, 'Path', ExistingPath) then
    ExistingPath := '';
  if PathContainsEntry(ExistingPath, InstallDir) then
    Exit;
  if (ExistingPath <> '') and (ExistingPath[Length(ExistingPath)] <> ';') then
    ExistingPath := ExistingPath + ';';
  if not RegWriteExpandStringValue(
      HKLM, SystemEnvironmentKey, 'Path', ExistingPath + InstallDir) then
    SuppressibleMsgBox(
      '시스템 PATH에 AI CCTV CLI 경로를 추가하지 못했습니다. ' +
      '시작 메뉴의 CLI Console 또는 전체 실행 파일 경로를 사용하십시오.',
      mbError, MB_OK, IDOK);
end;

procedure RemoveInstallDirFromPath;
var
  ExistingPath, InstallDir, UpdatedPath: String;
  Entries: TArrayOfString;
  Index: Integer;
begin
  InstallDir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKLM, SystemEnvironmentKey, 'Path', ExistingPath) then
    Exit;
  SplitPathEntries(ExistingPath, Entries);
  UpdatedPath := '';
  for Index := 0 to GetArrayLength(Entries) - 1 do
    if (Trim(Entries[Index]) <> '') and
       (NormalizedPathEntry(Entries[Index]) <> NormalizedPathEntry(InstallDir)) then
    begin
      if UpdatedPath <> '' then
        UpdatedPath := UpdatedPath + ';';
      UpdatedPath := UpdatedPath + Entries[Index];
    end;
  if UpdatedPath <> ExistingPath then
    RegWriteExpandStringValue(HKLM, SystemEnvironmentKey, 'Path', UpdatedPath);
end;

function DockerCliInstalled: Boolean;
begin
  Result :=
    (FileSearch('docker.exe', GetEnv('PATH')) <> '') or
    FileExists(ExpandConstant('{commonpf}\Docker\Docker\resources\bin\docker.exe')) or
    FileExists(ExpandConstant('{localappdata}\Docker\resources\bin\docker.exe'));
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if not DockerCliInstalled then
    SuppressibleMsgBox(
      'AI CCTV 서버 실행에는 Docker Desktop과 Docker Compose v2가 필요합니다.' + #13#10#13#10 +
      '설치는 계속할 수 있지만 Configurator에서 서비스를 시작하기 전에 ' +
      'Docker Desktop을 설치하고 실행하십시오.',
      mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddInstallDirToPath;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveInstallDirFromPath;
end;
