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
    $workspacePython = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
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
    (Join-Path $repositoryRoot 'docs\operations\windows-installer.md')
)) {
    if (-not (Test-Path -LiteralPath $requiredFile)) {
        throw "Required packaging input is missing: $requiredFile"
    }
}

$compiler = Find-InnoCompiler
New-Item -ItemType Directory -Force -Path $buildRoot, $distRoot, $installerDist | Out-Null
$bootstrapPython = Find-BootstrapPython
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
        "${repositoryRoot}[configurator,test]"
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
            '-m', 'pytest', '-q',
            'tests\test_configurator.py', 'tests\test_windows_packaging.py'
        )
        Invoke-Checked $buildPython @(
            '-m', 'ruff', 'check', 'configurator',
            'tests\test_configurator.py', 'tests\test_windows_packaging.py'
        )
    }

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

    Invoke-Checked $compiler @("/DMyAppVersion=$Version", $innoScript)

    $installer = Join-Path $installerDist "AI_CCTV_Server_Setup_${Version}_x64.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Inno Setup did not create the expected installer: $installer"
    }
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
