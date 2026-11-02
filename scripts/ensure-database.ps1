$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonExe = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
$previousUtf8 = $env:PYTHONUTF8
Push-Location -LiteralPath (Join-Path $projectRoot 'backend')
try {
    $env:PYTHONUTF8 = '1'
    $dbInfo = & $pythonExe -m app.database_tools info
    if ($LASTEXITCODE -ne 0) { throw '无法读取数据库配置。' }
    $dbInfo = $dbInfo | ConvertFrom-Json
    if ($dbInfo.mode -ne 'shared') { throw '本项目已迁到共享 PostgreSQL，请使用 DATABASE_MODE=shared。不会再安装或启动项目内数据库。' }
    Write-Host ('使用共享 PostgreSQL：{0}:{1}/{2}，项目脚本不启停公共服务。' -f $dbInfo.host,$dbInfo.port,$dbInfo.database)
    & $pythonExe -m app.database_tools check
    if ($LASTEXITCODE -ne 0) { throw '数据库连接失败。' }
} finally {
    Pop-Location
    $env:PYTHONUTF8 = $previousUtf8
}
