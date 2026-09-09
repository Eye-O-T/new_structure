# uv.lock의 의존성을 별도 가상환경에 설치해 Windows 설치 파일과 검증 해시를 만든다.
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
$repositoryRoot = (Resolve-Path (Join-Path $packagingRoot '..\..\..\..')).Path
$buildRoot = Join-Path $repositoryRoot 'build\windows-installer'
$buildVenv = Join-Path $buildRoot '.venv'
$buildPython = Join-Path $buildVenv 'Scripts\python.exe'
$distRoot = Join-Path $repositoryRoot 'dist'
$installerDist = Join-Path $distRoot 'installer'
$helperRoot = Join-Path $repositoryRoot 'server\setup\install_helper'
$guiSpec = Join-Path $packagingRoot 'ai_cctv_install_helper.spec'
$cliSpec = Join-Path $packagingRoot 'ai_cctv_cli.spec'
$innoScript = Join-Path $packagingRoot 'AI_CCTV_Server.iss'

function Invoke-Checked {
    # 외부 명령이 실패하면 후속 빌드도 중단한다.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

# 명시한 실행 파일, 개발 환경, PATH 순으로 기존 Python을 선택하고 다운로드는 별도로 하지 않는다.
function Find-BootstrapPython {
    if ($PythonExecutable) {
        if (-not (Test-Path -LiteralPath $PythonExecutable)) {
            throw "Python executable was not found: $PythonExecutable"
        }
        return (Resolve-Path -LiteralPath $PythonExecutable).Path
    }
    $workspacePython = Join-Path $repositoryRoot 'server\setup\install_helper\.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $workspacePython) {
        return $workspacePython
    }
    $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw 'Python 3.11 was not found. Pass -PythonExecutable <path-to-python.exe>.'
    }
    return $pythonCommand.Source
}

# 명시한 컴파일러가 있으면 그것을 우선하며 PATH와 일반 설치 위치에서 Inno Setup을 찾는다.
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

# 빌드가 시작되기 전에 배포 소스·문서·잠금 파일이 모두 있는지 확인해 불완전한 설치 파일을 막는다.
foreach ($requiredFile in @(
    $guiSpec,
    $cliSpec,
    $innoScript,
    (Join-Path $repositoryRoot 'lib\pyproject.toml'),
    (Join-Path $repositoryRoot 'server\setup\install_helper\pyproject.toml'),
    (Join-Path $repositoryRoot 'server\setup\pyproject.toml'),
    (Join-Path $repositoryRoot 'server\setup\install_helper\uv.lock'),
    (Join-Path $repositoryRoot 'server\setup\validation.py'),
    (Join-Path $repositoryRoot 'server\setup\tools\init_runtime.py'),
    (Join-Path $repositoryRoot 'server\setup\tools\generate_secrets.py'),
    (Join-Path $repositoryRoot 'server\setup\tools\generate_dev_cert.py'),
    (Join-Path $repositoryRoot 'server\setup\tools\enable_object_processing.py'),
    (Join-Path $repositoryRoot 'server\services\data\tools\backup_database.py'),
    (Join-Path $repositoryRoot 'server\services\external\tools\bootstrap_admin.py'),
    (Join-Path $repositoryRoot 'tests\runner\ruff.toml'),
    (Join-Path $repositoryRoot 'README.md'),
    (Join-Path $repositoryRoot 'mobile\README.md'),
    (Join-Path $repositoryRoot 'docs\architecture.md'),
    (Join-Path $repositoryRoot 'docs\openapi.yaml')
)) {
    if (-not (Test-Path -LiteralPath $requiredFile)) {
        throw "Required packaging input is missing: $requiredFile"
    }
}

$compiler = Find-InnoCompiler
$uvCommand = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    throw 'uv was not found. Install uv and restart PowerShell.'
}
New-Item -ItemType Directory -Force -Path $buildRoot, $distRoot, $installerDist | Out-Null
$bootstrapPython = Find-BootstrapPython
# 잠금 파일이 지원하는 Python 3.11을 사용한다.
$bootstrapVersion = (& $bootstrapPython -c 'import sys; print(sys.version_info.major, sys.version_info.minor, sep=chr(46))').Trim()
if ($LASTEXITCODE -ne 0 -or $bootstrapVersion -ne '3.11') {
    throw "Bootstrap Python must be version 3.11; found $bootstrapVersion."
}
if ($SkipDependencyInstall -and -not (Test-Path -LiteralPath $buildPython)) {
    throw 'The build environment is missing. Run without -SkipDependencyInstall first.'
}
# 빌드용 환경 선택은 이 작업에만 적용하고 종료 경로와 관계없이 호출자의 환경 변수로 되돌린다.
$previousUvEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    # 개발용 .venv는 유지하고, 설치 생략 시에도 기존 빌드 환경의 잠금 일치를 확인한다.
    $env:UV_PROJECT_ENVIRONMENT = $buildVenv
    $syncArguments = @(
        'sync', '--locked', '--project', $helperRoot,
        '--python', $bootstrapPython, '--no-python-downloads',
        '--extra', 'test', '--extra', 'build', '--no-editable'
    )
    if ($SkipDependencyInstall) {
        $syncArguments += @('--check', '--offline')
    }
    Invoke-Checked $uvCommand.Source $syncArguments
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousUvEnvironment
}

Push-Location $repositoryRoot
try {
    if (-not $SkipTests) {
        Invoke-Checked $buildPython @(
            '-m', 'pytest', '-q', '-c', 'server\setup\install_helper\pyproject.toml',
            'server\setup\install_helper\tests', 'server\setup\tests'
        )
        Invoke-Checked $buildPython @(
            '-m', 'ruff', 'check', '--config', 'tests\runner\ruff.toml',
            'server\setup', 'server\services\data\tools', 'server\services\external\tools'
        )
    }

    # GUI와 CLI는 콘솔 사용 여부가 달라 각각의 실행 파일로 묶는다.
    foreach ($spec in @($guiSpec, $cliSpec)) {
        Invoke-Checked $buildPython @(
            '-m', 'PyInstaller', '--clean', '--noconfirm',
            '--distpath', $distRoot,
            '--workpath', (Join-Path $buildRoot 'pyinstaller'),
            $spec
        )
    }

    foreach ($executable in @('AI_CCTV_Server_Install_Helper.exe', 'AI_CCTV_CLI.exe')) {
        $path = Join-Path $distRoot $executable
        if (-not (Test-Path -LiteralPath $path)) {
            throw "PyInstaller did not create the expected executable: $path"
        }
    }

    # 실행 파일과 서버 코드를 바로가기·제거 기능이 있는 설치 파일로 묶는다.
    Invoke-Checked $compiler @("/DMyAppVersion=$Version", $innoScript)

    $installer = Join-Path $installerDist "AI_CCTV_Server_Setup_${Version}_x64.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Inno Setup did not create the expected installer: $installer"
    }
    # 배포 파일의 복사·다운로드 중 손상을 비교할 해시를 함께 저장한다.
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
