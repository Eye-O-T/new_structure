; Windows 설치·제거 설정. 운영 데이터는 프로그램과 분리해 보존한다.
#define MyAppName "AI CCTV Server"
#ifndef MyAppVersion
  #define MyAppVersion "0.3.0"
#endif
#define MyAppPublisher "AI CCTV"
#define MyGuiExeName "AI_CCTV_Server_Install_Helper.exe"
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
OutputDir=..\..\..\..\dist\installer
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
Source: "..\..\..\..\dist\AI_CCTV_Server_Install_Helper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\..\dist\AI_CCTV_CLI.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\..\.dockerignore"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\..\lib\*"; DestDir: "{app}\lib"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc,*.pyo,*.egg-info\*,build\*,dist\*"
; 여섯 서비스의 실행 코드를 포함하고 개발·테스트 도구와 실제 운영 데이터는 제외한다.
Source: "..\..\..\..\server\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".env,secrets\*.env,secrets\*.json,runtime\*,certs\*,config\config.yaml,__pycache__\*,*.pyc,*.pyo,*.key,*.crt,*.pem,tests\*,setup\tests\*,setup\.venv\*,setup\*.egg-info\*,setup\build\*,setup\dist\*,setup\install_helper\*,.venv\*,*.egg-info\*,build\*,dist\*,compose.dev.yml,compose.test.yml,services\external\tools\export_openapi.py"
; 영구 README·상세 안내·구조 그림·API·라이선스를 같은 상대 위치에 둔다. 임시 인수 SRS는 배포하지 않는다.
Source: "..\..\..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\..\mobile\README.md"; DestDir: "{app}\mobile"; Flags: ignoreversion
Source: "..\..\..\..\server\setup\install_helper\README.md"; DestDir: "{app}\server\setup\install_helper"; Flags: ignoreversion
Source: "..\..\..\..\edge\README.md"; DestDir: "{app}\edge"; Flags: ignoreversion
Source: "..\..\..\..\tests\mock_edge\README.md"; DestDir: "{app}\tests\mock_edge"; Flags: ignoreversion
Source: "..\..\..\..\docs\README.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\..\..\docs\guide.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\..\..\docs\architecture.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\..\..\docs\assets\architecture\*.svg"; DestDir: "{app}\docs\assets\architecture"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\..\..\docs\openapi.yaml"; DestDir: "{app}\docs"; Flags: ignoreversion

[InstallDelete]
; 이름이 바뀐 실행 파일과 바로가기를 정리하며 운영 데이터는 유지한다.
Type: files; Name: "{app}\AI_CCTV_Configurator.exe"
Type: files; Name: "{group}\AI CCTV Configurator.lnk"
Type: files; Name: "{autodesktop}\AI CCTV Configurator.lnk"
Type: files; Name: "{app}\configurator\README.md"
Type: dirifempty; Name: "{app}\configurator"
; 이동 전 배포에 포함했던 도구·문서만 정리하고 설정·사용자 파일은 보존한다.
Type: files; Name: "{app}\server\install_helper\README.md"
Type: dirifempty; Name: "{app}\server\install_helper"
Type: files; Name: "{app}\server\scripts\init_runtime.py"
Type: files; Name: "{app}\server\scripts\generate_secrets.py"
Type: files; Name: "{app}\server\scripts\generate_dev_cert.py"
Type: files; Name: "{app}\server\scripts\enable_object_processing.py"
Type: files; Name: "{app}\server\scripts\doctor.py"
Type: files; Name: "{app}\server\scripts\bootstrap_admin.py"
Type: files; Name: "{app}\server\scripts\backup_database.py"
Type: dirifempty; Name: "{app}\server\scripts"

[Icons]
Name: "{group}\AI CCTV Server Install Helper"; Filename: "{app}\{#MyGuiExeName}"; WorkingDir: "{app}"
Name: "{group}\AI CCTV CLI Console"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoExit -Command ""& '{app}\{#MyCliExeName}' --help"""; WorkingDir: "{app}"
Name: "{group}\Installation guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\README.md"""
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AI CCTV Server Install Helper"; Filename: "{app}\{#MyGuiExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 보호된 운영 설정을 저장하기 위해 GUI는 관리자 권한을 요청한다.
Filename: "{app}\{#MyGuiExeName}"; Description: "AI CCTV Server Install Helper를 열어 모델 경로와 서버 설정 지정"; Flags: nowait postinstall skipifsilent

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

// PATH 항목의 원문은 보존한 채 세미콜론 단위로 분리해 정확한 항목 비교에 사용한다.
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

// 부분 문자열이 아닌 정규화된 항목 전체를 비교해 비슷한 이름의 다른 경로를 구분한다.
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

// 시스템 PATH에 설치 경로가 없을 때만 추가하며 기존 항목과 환경 변수 표현은 유지한다.
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

// 이번 설치 경로와 일치하는 항목만 제거하고 다른 프로그램의 PATH 항목은 보존한다.
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

// CLI 파일의 존재만 확인한다. 엔진 실행과 Linux 컨테이너 모드는 설치 도우미가 따로 검사한다.
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
      '설치는 계속할 수 있지만 서버 설치 도우미에서 서비스를 시작하기 전에 ' +
      'Docker Desktop을 설치하고 실행하십시오.',
      mbInformation, MB_OK, IDOK);
end;

// 실제 파일 설치가 끝나고 사용자가 PATH 추가 작업을 선택한 경우에만 시스템 환경을 수정한다.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddInstallDirToPath;
end;

// 제거 단계에서 설치 경로를 PATH에서도 정리해 삭제된 CLI 위치가 남지 않게 한다.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveInstallDirFromPath;
end;
