$ErrorActionPreference = "Stop"

$BuildExe = Join-Path $PSScriptRoot "build_exe.ps1"
& $BuildExe @args
