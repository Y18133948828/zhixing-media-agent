param([string[]]$PytestArgs = @())
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
& "$PSScriptRoot\ensure-database.ps1"
Push-Location -LiteralPath (Join-Path $projectRoot 'backend')
$pythonExe = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
$oldTestUrl = $env:TEST_DATABASE_URL
$oldPythonUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONUTF8 = '1'
    & $pythonExe -m app.database_tools prepare-test
    if ($LASTEXITCODE -ne 0) { throw '测试数据库未准备好。' }
    $env:TEST_DATABASE_URL = & $pythonExe -m app.database_tools test-url
    if ($LASTEXITCODE -ne 0) { throw '生成测试连接配置失败。' }
    & $pythonExe -m pytest -q @PytestArgs
    if ($LASTEXITCODE -ne 0) { throw '测试未通过，请检查测试输出。' }
} finally {
    $env:TEST_DATABASE_URL = $oldTestUrl
    $env:PYTHONUTF8 = $oldPythonUtf8
    Pop-Location
}
