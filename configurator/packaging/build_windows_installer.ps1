# Python 프로그램을 실행 파일로 묶은 뒤 Windows 설치 프로그램과 검증용 해시를 만든다.
# 개발용 가상환경과 빌드용 가상환경을 분리해 개발 중 설치한 패키지의 영향을 줄인다.
[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(?:\.\d+)?$')]
    [string]$Version = '0.3.0',
    [string]$PythonExecutable = '',
    [string]$InnoCompiler = '',
    [switch]$SkipDependencyInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$packagingRoot = $PSScriptRoot
$repositoryRoot = (Resolve-Path (Join-Path $packagingRoot '..\..')).Path
$buildRoot = Join-Path $repositoryRoot 'build\windows-installer'
$buildVenv = Join-Path $buildRoot '.venv'
$buildPython = Join-Path $buildVenv 'Scripts\python.exe'
$distRoot = Join-Path $repositoryRoot 'dist'
$installerDist = Join-Path $distRoot 'installer'
$buildRequirements = Join-Path $packagingRoot 'requirements-windows-build.txt'
$guiSpec = Join-Path $packagingRoot 'ai_cctv_configurator.spec'
$cliSpec = Join-Path $packagingRoot 'ai_cctv_cli.spec'
$innoScript = Join-Path $packagingRoot 'AI_CCTV_Server.iss'

function Invoke-Checked {
    # 외부 도구의 종료 코드를 확인해 앞 단계가 실패한 상태로 다음 빌드를 진행하지 않는다.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

function Find-BootstrapPython {
    if ($PythonExecutable) {
        if (-not (Test-Path -LiteralPath $PythonExecutable)) {
            throw "Python executable was not found: $PythonExecutable"
        }
        return (Resolve-Path -LiteralPath $PythonExecutable).Path
    }
    $workspacePython = Join-Path $repositoryRoot 'configurator\.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $workspacePython) {
        return $workspacePython
    }
    $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw 'Python 3.11 was not found. Pass -PythonExecutable <path-to-python.exe>.'
    }
    return $pythonCommand.Source
}

function Find-InnoCompiler {
    if ($InnoCompiler) {
        if (-not (Test-Path -LiteralPath $InnoCompiler)) {
            throw "Inno Setup compiler was not found: $InnoCompiler"
        }
        return (Resolve-Path -LiteralPath $InnoCompiler).Path
    }
    $command = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw 'Inno Setup 6 was not found. Install it or pass -InnoCompiler <path-to-ISCC.exe>.'
}

if ($env:OS -ne 'Windows_NT') {
    throw 'The Windows installer must be built on Windows x64.'
}

foreach ($requiredFile in @(
    $buildRequirements,
    $guiSpec,
    $cliSpec,
    $innoScript,
    (Join-Path $repositoryRoot 'lib\pyproject.toml'),
    (Join-Path $repositoryRoot 'configurator\pyproject.toml'),
    (Join-Path $repositoryRoot 'tests\runner\ruff.toml'),
    (Join-Path $repositoryRoot 'README.md'),
    (Join-Path $repositoryRoot 'mobile\README.md'),
    (Join-Path $repositoryRoot 'docs\architecture.md'),
    (Join-Path $repositoryRoot 'docs\SRS_interface_preprocessing.md'),
    (Join-Path $repositoryRoot 'docs\SRS_interface_analysis.md'),
    (Join-Path $repositoryRoot 'docs\openapi.yaml')
)) {
    if (-not (Test-Path -LiteralPath $requiredFile)) {
        throw "Required packaging input is missing: $requiredFile"
    }
}

$compiler = Find-InnoCompiler
New-Item -ItemType Directory -Force -Path $buildRoot, $distRoot, $installerDist | Out-Null
$bootstrapPython = Find-BootstrapPython
# 실행 파일에 포함될 Python 버전을 고정해야 빌드 PC마다 호환성이 달라지는 일을 줄일 수 있다.
$bootstrapVersion = (& $bootstrapPython -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))').Trim()
if ($LASTEXITCODE -ne 0 -or $bootstrapVersion -ne '3.11') {
    throw "Bootstrap Python must be version 3.11; found $bootstrapVersion."
}
if (-not (Test-Path -LiteralPath $buildPython)) {
    Invoke-Checked $bootstrapPython @('-m', 'venv', $buildVenv)
}

$pythonVersion = (& $buildPython -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))').Trim()
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.11') {
    throw "The packaging environment must use Python 3.11; found $pythonVersion."
}

if (-not $SkipDependencyInstall) {
    $pipAvailable = (& $buildPython -c 'import importlib.util; print(int(importlib.util.find_spec(chr(112)+chr(105)+chr(112)) is not None))').Trim()
    if ($LASTEXITCODE -ne 0 -or $pipAvailable -ne '1') {
        Invoke-Checked $buildPython @('-m', 'ensurepip', '--upgrade')
    }
    Invoke-Checked $buildPython @(
        '-m', 'pip', 'install', '--disable-pip-version-check',
        '--requirement', $buildRequirements
    )
    Invoke-Checked $buildPython @(
        '-m', 'pip', 'install', '--disable-pip-version-check',
        (Join-Path $repositoryRoot 'lib'),
        "$(Join-Path $repositoryRoot 'configurator')[test]"
    )
}
else {
    Invoke-Checked $buildPython @(
        '-c', 'import PyInstaller, PyQt5, argon2, pydantic, yaml'
    )
}

Push-Location $repositoryRoot
try {
    if (-not $SkipTests) {
        Invoke-Checked $buildPython @(
            '-m', 'pytest', '-q', '-c', 'configurator\pyproject.toml',
            'configurator\tests\test_configurator.py', 'configurator\tests\test_windows_packaging.py'
        )
        Invoke-Checked $buildPython @(
            '-m', 'ruff', 'check', '--config', 'tests\runner\ruff.toml', 'configurator',
            'configurator\tests\test_configurator.py', 'configurator\tests\test_windows_packaging.py'
        )
    }

    # PyInstaller는 Python 실행 환경과 의존성을 함께 묶는다. GUI와 CLI는 진입점과
    # 콘솔 사용 방식이 달라 각 spec 파일로 별도의 실행 파일을 만든다.
    foreach ($spec in @($guiSpec, $cliSpec)) {
        Invoke-Checked $buildPython @(
            '-m', 'PyInstaller', '--clean', '--noconfirm',
            '--distpath', $distRoot,
            '--workpath', (Join-Path $buildRoot 'pyinstaller'),
            $spec
        )
    }

    foreach ($executable in @('AI_CCTV_Configurator.exe', 'AI_CCTV_CLI.exe')) {
        $path = Join-Path $distRoot $executable
        if (-not (Test-Path -LiteralPath $path)) {
            throw "PyInstaller did not create the expected executable: $path"
        }
    }

    # Inno Setup은 실행 파일, 서버 코드, 바로가기와 제거 절차를 하나의 설치 파일로 묶는다.
    Invoke-Checked $compiler @("/DMyAppVersion=$Version", $innoScript)

    $installer = Join-Path $installerDist "AI_CCTV_Server_Setup_${Version}_x64.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Inno Setup did not create the expected installer: $installer"
    }
    # SHA-256은 배포 파일이 다운로드·복사 중 달라졌는지 비교할 수 있는 파일 지문이다.
    $checksumPath = "${installer}.sha256"
    $checksum = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $checksumPath -Encoding ascii -NoNewline -Value (
        "$checksum  $([IO.Path]::GetFileName($installer))`n"
    )

    Write-Host "Installer: $installer"
    Write-Host "SHA-256:   $checksumPath"
}
finally {
    Pop-Location
}
