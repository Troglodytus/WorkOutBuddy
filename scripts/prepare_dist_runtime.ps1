param(
    [string]$DesktopPort = "8090"
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Dist = Join-Path $Root "dist"
$ProjectEnv = Join-Path $Root ".env"
$DistEnv = Join-Path $Dist ".env"

if (-not (Test-Path $Dist)) {
    New-Item -ItemType Directory -Path $Dist | Out-Null
}

if (-not (Test-Path $ProjectEnv)) {
    throw "Project .env not found at $ProjectEnv. Create it before preparing dist."
}

function Get-EnvValue($Path, $Name) {
    $line = Get-Content -Path $Path | Where-Object {
        $_ -match "^\s*$Name\s*="
    } | Select-Object -First 1
    if (-not $line) {
        return ""
    }
    return (($line -split "=", 2)[1]).Trim().Trim('"').Trim("'")
}

$ClientId = Get-EnvValue $ProjectEnv "STRAVA_CLIENT_ID"
$ClientSecret = Get-EnvValue $ProjectEnv "STRAVA_CLIENT_SECRET"

if (-not $ClientId -or -not $ClientSecret) {
    throw "STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be present in $ProjectEnv."
}

New-Item -ItemType Directory -Force -Path (Join-Path $Dist "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dist ".secrets") | Out-Null

@"
# WorkOutBuddy Desktop runtime configuration
# This file makes the dist folder a standalone app home.
# Keep this file private because it contains Strava API credentials.

STRAVA_CLIENT_ID=$ClientId
STRAVA_CLIENT_SECRET=$ClientSecret

# Fresh desktop database and token storage live next to the exe.
WORKOUTBUDDY_DB=data/workoutbuddy.sqlite
WORKOUTBUDDY_DESKTOP_HOST=127.0.0.1
WORKOUTBUDDY_DESKTOP_PORT=$DesktopPort
WORKOUTBUDDY_DESKTOP_PUBLIC_BASE_URL=http://localhost:$DesktopPort

# The browser/Tailscale web mode can still use the same local callback if needed.
WORKOUTBUDDY_WEB_HOST=127.0.0.1
WORKOUTBUDDY_WEB_PORT=$DesktopPort
WORKOUTBUDDY_PUBLIC_BASE_URL=http://localhost:$DesktopPort
"@ | Set-Content -Path $DistEnv -Encoding ASCII

@"
WorkOutBuddy Desktop
====================

Run:
  WorkOutBuddy.exe

This folder is a standalone WorkOutBuddy app home.

Local files created here:
  data\workoutbuddy.sqlite    Fresh desktop database
  .secrets\token.json         Strava token after authorization
  data\raw_activities         Synced Strava activity JSON
  data\streams                Synced/imported stream JSON
  data\maps                   Generated route maps
  data\exports                XLSX exports
  data\uploaded_tcx           Uploaded TCX files

For Strava API settings, use callback domain:
  localhost

Callback URL:
  http://localhost:$DesktopPort/strava/callback

Keep .env, .secrets, and data private.
"@ | Set-Content -Path (Join-Path $Dist "README_DIST.txt") -Encoding ASCII
