# deploy.ps1 — Build frontend and deploy corp-client-agent to Databricks Apps
#
# Usage:
#   .\deploy.ps1
#
# Optional env vars:
#   DATABRICKS_APP_NAME     — name of the Databricks App
#   DATABRICKS_APP_PATH     — workspace path (no /Workspace prefix)
#   DATABRICKS_PROFILE      — CLI profile to use
#   SKIP_FRONTEND_BUILD     — set to "1" to skip npm build

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot

# ── Config ───────────────────────────────────────────────────────────────────
$AppName        = if ($env:DATABRICKS_APP_NAME)  { $env:DATABRICKS_APP_NAME }  else { "corp-client-agent-sqlite" }
$AppPath        = if ($env:DATABRICKS_APP_PATH)  { $env:DATABRICKS_APP_PATH }  else { "/Users/leticia.santos@databricks.com/corp-client-agent-sqlite" }
$AppDeployPath  = "/Workspace$AppPath"
$SkipFrontend   = $env:SKIP_FRONTEND_BUILD -eq "1"

$ProfileArgs = @()
if ($env:DATABRICKS_PROFILE) {
    $ProfileArgs = @("--profile", $env:DATABRICKS_PROFILE)
}

# ── 1. Build frontend ─────────────────────────────────────────────────────────
if (-not $SkipFrontend) {
    Write-Host "Building frontend..."
    Push-Location "$ScriptDir\frontend"
    npm install --silent
    npm run build
    Pop-Location
    Write-Host "  Frontend built -> frontend/dist/"
} else {
    Write-Host "Skipping frontend build (SKIP_FRONTEND_BUILD=1)"
}

# ── 2. Copy dist -> backend/static ───────────────────────────────────────────
Write-Host "Copying frontend/dist -> backend/static..."
if (Test-Path "$ScriptDir\backend\static") {
    Remove-Item -Recurse -Force "$ScriptDir\backend\static"
}
Copy-Item -Recurse "$ScriptDir\frontend\dist" "$ScriptDir\backend\static"
Write-Host "  backend/static updated"

# ── 3. Generate root requirements.txt ────────────────────────────────────────
$WhlFile = Get-ChildItem "$ScriptDir\backend\wheels\*.whl" -ErrorAction SilentlyContinue |
           Sort-Object Name |
           Select-Object -Last 1

if (-not $WhlFile) {
    Write-Error "ERROR: No .whl found in backend/wheels/"
    exit 1
}
$WhlName = $WhlFile.Name

@"
-r backend/requirements.txt
./backend/wheels/$WhlName
"@ | Set-Content "$ScriptDir\requirements.txt" -Encoding UTF8

Write-Host "  requirements.txt generated (wheel: $WhlName)"

# ── 4. Sync to Databricks workspace ──────────────────────────────────────────
Write-Host "Syncing to workspace: $AppPath"

databricks @ProfileArgs sync `
    "$ScriptDir" `
    "$AppPath" `
    --exclude 'frontend/node_modules' `
    --exclude 'frontend/dist' `
    --exclude 'backend/.venv' `
    --exclude 'backend/local.db*' `
    --exclude 'tasks' `
    --full

Write-Host "  Files synced"

# ── 5. Deploy app ─────────────────────────────────────────────────────────────
Write-Host "Deploying app '$AppName'..."
databricks @ProfileArgs apps deploy "$AppName" --source-code-path "$AppDeployPath"

Write-Host ""
Write-Host "Deploy concluido: $AppName"
