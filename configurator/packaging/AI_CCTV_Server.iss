; Windows 설치·제거 설정. 운영 데이터는 프로그램과 분리해 보존한다.
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
; ProgramData의 녹화·DB·설정은 제거 후에도 보존하며, 관리자에게만 수정 권한을 준다.
Name: "{commonappdata}\AI_CCTV"; Flags: uninsneveruninstall

[Files]
Source: "..\..\dist\AI_CCTV_Configurator.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AI_CCTV_CLI.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\.dockerignore"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\lib\*"; DestDir: "{app}\lib"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc,*.pyo,*.egg-info\*,build\*,dist\*"
; 여섯 서비스의 실행 코드를 포함하고 개발·테스트 도구와 실제 운영 데이터는 제외한다.
Source: "..\..\server\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".env,secrets\*.env,secrets\*.json,runtime\*,certs\*,config\config.yaml,__pycache__\*,*.pyc,*.pyo,*.key,*.crt,*.pem,tests\*,compose.dev.yml,compose.test.yml,scripts\export_openapi.py"
; README와 네 규약 문서를 소스 저장소와 같은 상대 위치에 두어 링크를 유지한다.
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\mobile\README.md"; DestDir: "{app}\mobile"; Flags: ignoreversion
Source: "..\..\configurator\README.md"; DestDir: "{app}\configurator"; Flags: ignoreversion
Source: "..\..\edge\README.md"; DestDir: "{app}\edge"; Flags: ignoreversion
Source: "..\..\tests\mock_edge\README.md"; DestDir: "{app}\tests\mock_edge"; Flags: ignoreversion
Source: "..\..\docs\architecture.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\SRS_interface_preprocessing.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\SRS_interface_analysis.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\docs\openapi.yaml"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\AI CCTV Configurator"; Filename: "{app}\{#MyGuiExeName}"; WorkingDir: "{app}"
Name: "{group}\AI CCTV CLI Console"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoExit -Command ""& '{app}\{#MyCliExeName}' --help"""; WorkingDir: "{app}"
Name: "{group}\Installation guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\README.md"""
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AI CCTV Configurator"; Filename: "{app}\{#MyGuiExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 보호된 운영 설정을 저장하기 위해 GUI는 관리자 권한을 요청한다.
Filename: "{app}\{#MyGuiExeName}"; Description: "AI CCTV Configurator를 열어 모델 경로와 서버 설정 지정"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 컨테이너와 네트워크를 중지·제거하되 호스트의 운영 데이터는 남긴다.
Filename: "{app}\{#MyCliExeName}"; Parameters: "--server-dir ""{app}\server"" stop --env-file ""{commonappdata}\AI_CCTV\config\compose.env"""; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAiCctvServices"

[UninstallDelete]
; 과거 설치 경로의 env만 삭제하며 ProgramData의 운영 설정은 유지한다.
Type: files; Name: "{app}\server\.env"

[Code]
// 어디서든 CLI를 실행하도록 설치 경로만 PATH에 추가·제거한다.
const
  SystemEnvironmentKey =
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function NormalizedPathEntry(Value: String): String;
begin
  // 따옴표·마지막 역슬래시·대소문자 차이로 같은 경로가 중복 등록되지 않도록 비교 형식을 맞춘다.
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
  // 설치 파일 생성과 컨테이너 실행은 별개다. Docker가 없어도 설치는 허용하되 실행 조건을 알린다.
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
