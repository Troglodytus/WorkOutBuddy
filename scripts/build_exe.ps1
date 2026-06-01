param(
    [switch]$SkipInstall,
    [switch]$SkipRuntime,
    [string]$DesktopPort = "8090"
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LockFile = Join-Path $Root "requirements-desktop-build.lock.txt"
$SpecFile = Join-Path $Root "WorkOutBuddy.spec"

if (-not (Test-Path $Python)) {
    throw "Virtual environment not found at $Python. Create it first and install requirements."
}

if (-not $SkipInstall) {
    & $Python -m pip install -r $LockFile
}

& $Python -m PyInstaller --clean --noconfirm $SpecFile

if (-not $SkipRuntime) {
    & (Join-Path $Root "scripts\prepare_dist_runtime.ps1") -DesktopPort $DesktopPort
}
