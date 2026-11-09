[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/fahadhossain0909/aitos.git",
    [string]$Ref = "main",
    [string]$TestPath = "tests/backtest/test_queue_lifecycle.py",
    [string]$WorkRoot = (Join-Path $env:TEMP ("aitos-apponfly-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss"))),
    [switch]$SkipToolInstall
)

$ErrorActionPreference = "Stop"
$LogPath = Join-Path $WorkRoot "apponfly-smoke.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    if (Test-Path (Split-Path -Parent $LogPath)) {
        Add-Content -Path $LogPath -Value $line
    }
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Write-Log ("RUN: {0} {1}" -f $Executable, ($Arguments -join " "))
    & $Executable @Arguments 2>&1 | Tee-Object -FilePath $LogPath -Append
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Command failed with exit code $exitCode : $Executable $($Arguments -join ' ')"
    }
}

New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
Write-Log "Starting bounded, offline AITOS smoke test on temporary Windows desktop."
Write-Log "Repository: $RepositoryUrl"
Write-Log "Ref: $Ref"
Write-Log "No credentials, API keys, orders, Docker, or persistent services are used."

$winget = Get-Command winget -ErrorAction SilentlyContinue
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    if ($SkipToolInstall -or -not $winget) {
        throw "Git is missing and automatic installation is unavailable. Install Git or rerun without -SkipToolInstall where winget is available."
    }
    Invoke-LoggedCommand -Executable $winget.Source -Arguments @(
        "install", "--id", "Git.Git", "-e", "--source", "winget", "--silent",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    $env:Path += ";C:\Program Files\Git\cmd;C:\Program Files\Git\bin"
    $git = Get-Command git -ErrorAction SilentlyContinue
}
if (-not $git) {
    throw "Git installation completed but git is not available in PATH. Open a fresh PowerShell session and rerun."
}
Invoke-LoggedCommand -Executable $git.Source -Arguments @("--version")

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) {
    if ($SkipToolInstall -or -not $winget) {
        throw "Python launcher (py) is missing and automatic installation is unavailable. Install Python 3.12 or rerun without -SkipToolInstall where winget is available."
    }
    Invoke-LoggedCommand -Executable $winget.Source -Arguments @(
        "install", "--id", "Python.Python.3.12", "-e", "--source", "winget", "--silent",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    $env:Path += ";$env:LOCALAPPDATA\Programs\Python\Launcher"
    $py = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $py) {
    throw "Python installation completed but the py launcher is not available. Open a fresh PowerShell session and rerun."
}
Invoke-LoggedCommand -Executable $py.Source -Arguments @("-3.12", "--version")

$repoDir = Join-Path $WorkRoot "aitos"
Invoke-LoggedCommand -Executable $git.Source -Arguments @(
    "clone", "--depth", "1", "--branch", $Ref, $RepositoryUrl, $repoDir
)

Push-Location $repoDir
try {
    Invoke-LoggedCommand -Executable $git.Source -Arguments @("rev-parse", "--short", "HEAD")

    if (-not (Test-Path $TestPath)) {
        throw "Configured smoke test does not exist in this checkout: $TestPath"
    }

    Invoke-LoggedCommand -Executable $py.Source -Arguments @("-3.12", "-m", "venv", ".venv")
    $venvPython = Join-Path $repoDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        throw "Virtual environment Python was not created: $venvPython"
    }

    Invoke-LoggedCommand -Executable $venvPython -Arguments @("-m", "pip", "install", "pytest")
    $testDirectory = Split-Path -Parent $TestPath
    Invoke-LoggedCommand -Executable $venvPython -Arguments @(
        "-m", "pytest", "-q", "--confcutdir=$testDirectory", $TestPath
    )

    Write-Log "SMOKE TEST PASSED."
    Write-Log "Workspace: $WorkRoot"
    Write-Log "Log: $LogPath"
}
catch {
    Write-Log ("SMOKE TEST FAILED: " + $_.Exception.Message)
    Write-Log "Inspect the log and resolve the reported dependency, environment, or test issue."
    throw
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "AITOS AppOnFly smoke test completed. Review: $LogPath"
