$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html'))) {
    & "$PSScriptRoot\build-frontend.ps1"
}
& "$PSScriptRoot\ensure-database.ps1"
Push-Location -LiteralPath (Join-Path $projectRoot 'backend')
$previousUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONUTF8 = '1'
    & '.\.venv\Scripts\python.exe' -m app.init_db
    if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败，服务尚未启动。' }
    & '.\.venv\Scripts\python.exe' -m app.run
} finally {
    $env:PYTHONUTF8 = $previousUtf8
    Pop-Location
}
