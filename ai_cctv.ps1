[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('gui', 'cli')]
    [string]$Mode,
    [ValidateSet('on', 'off', 'keep')]
    [string]$LocalCamera = 'keep',
    [ValidateSet('on', 'off', 'keep')]
    [string]$Preprocessing = 'keep',
    [ValidateSet('on', 'off', 'keep')]
    [string]$Analysis = 'keep',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$CliArguments
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = $PSScriptRoot
$packagedCli = Join-Path $root 'AI_CCTV_CLI.exe'
$previousPythonPath = $env:PYTHONPATH
if (-not (Test-Path -LiteralPath $packagedCli)) {
    $sourcePaths = @($root, (Join-Path $root 'lib')) -join [IO.Path]::PathSeparator
    $env:PYTHONPATH = if ($previousPythonPath) {
        $sourcePaths + [IO.Path]::PathSeparator + $previousPythonPath
    } else {
        $sourcePaths
    }
}

function Invoke-AiCctvCli {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    if (Test-Path -LiteralPath $packagedCli) {
        & $packagedCli @Arguments
    }
    else {
        $uv = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
        if ($null -ne $uv) {
            & $uv.Source run --project (Join-Path $root 'server\setup\install_helper') --locked python -m server.cli @Arguments
        }
        else {
            & python -m server.cli @Arguments
        }
    }
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if ($Mode -eq 'cli') {
    if (-not $CliArguments -or $CliArguments.Count -eq 0) {
        throw 'cli mode requires a command.'
    }
    Invoke-AiCctvCli @CliArguments
    exit 0
}

Invoke-AiCctvCli start
if ($Preprocessing -ne 'keep') {
    Invoke-AiCctvCli preprocessing $Preprocessing
}
if ($Analysis -ne 'keep') {
    Invoke-AiCctvCli analysis $Analysis
}
if ($LocalCamera -ne 'keep') {
    Invoke-AiCctvCli local-camera $LocalCamera
}
Invoke-AiCctvCli admin
