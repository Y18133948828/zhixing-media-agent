param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$previousUtf8 = $env:PYTHONUTF8
Push-Location -LiteralPath $projectRoot
try {
    $env:PYTHONUTF8 = '1'
    New-Item -ItemType Directory -Path '.runtime','backend\alembic\versions' -Force | Out-Null
    if (-not (Test-Path -LiteralPath 'backend\.venv\Scripts\python.exe')) {
        & $Python -m venv backend\.venv
        if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，需要 Python 3.11–3.14。' }
    }
    $venvPython = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
    & $venvPython -m pip install -r backend\requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
    & $venvPython -m pip install --no-deps -e backend
    if ($LASTEXITCODE -ne 0) { throw '项目安装失败。' }
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Chromium 安装失败。' }
    if (-not (Test-Path -LiteralPath 'backend\.env')) {
        $configText = Get-Content -LiteralPath 'backend\.env.example' -Raw
        $configText = $configText.Replace('replace-with-a-long-random-key', [guid]::NewGuid().ToString('N'))
        [IO.File]::WriteAllText((Join-Path $projectRoot 'backend\.env'), $configText, [Text.UTF8Encoding]::new($false))
        throw '已创建 backend\.env。请先为项目建立独立数据库，填写 DATABASE_URL 后重新运行；不会自动安装第二套 PostgreSQL。'
    }
    $dbInfo = (& $venvPython -m app.database_tools info) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw '数据库配置读取失败。' }
    if ($dbInfo.mode -ne 'shared') { throw '请设置 DATABASE_MODE=shared，使用公共 PostgreSQL；不会为本项目另装数据库。' }
    & "$PSScriptRoot\ensure-database.ps1"
    Push-Location -LiteralPath (Join-Path $projectRoot 'backend')
    try {
        & $venvPython -m app.init_db
        if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败。' }
    } finally { Pop-Location }
    & "$PSScriptRoot\build-frontend.ps1"
    Write-Host '准备完成。运行 scripts\start.ps1，打开 http://127.0.0.1:8000/app/。'
} finally {
    Pop-Location
    $env:PYTHONUTF8 = $previousUtf8
}
