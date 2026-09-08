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
$repositoryRoot = (Resolve-Path (Join-Path $packagingRoot '..\..')).Path
$buildRoot = Join-Path $repositoryRoot 'build\windows-installer'
$buildVenv = Join-Path $buildRoot '.venv'
$buildPython = Join-Path $buildVenv 'Scripts\python.exe'
$distRoot = Join-Path $repositoryRoot 'dist'
$installerDist = Join-Path $distRoot 'installer'
$configuratorRoot = Join-Path $repositoryRoot 'configurator'
$guiSpec = Join-Path $packagingRoot 'ai_cctv_configurator.spec'
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
    $guiSpec,
    $cliSpec,
    $innoScript,
    (Join-Path $repositoryRoot 'lib\pyproject.toml'),
    (Join-Path $repositoryRoot 'configurator\pyproject.toml'),
    (Join-Path $repositoryRoot 'configurator\uv.lock'),
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
$previousUvEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    # 개발용 .venv는 유지하고, 설치 생략 시에도 기존 빌드 환경의 잠금 일치를 확인한다.
    $env:UV_PROJECT_ENVIRONMENT = $buildVenv
    $syncArguments = @(
        'sync', '--locked', '--project', $configuratorRoot,
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
            '-m', 'pytest', '-q', '-c', 'configurator\pyproject.toml',
            'configurator\tests\test_configurator.py', 'configurator\tests\test_windows_packaging.py'
        )
        Invoke-Checked $buildPython @(
            '-m', 'ruff', 'check', '--config', 'tests\runner\ruff.toml', 'configurator'
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

    foreach ($executable in @('AI_CCTV_Configurator.exe', 'AI_CCTV_CLI.exe')) {
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
