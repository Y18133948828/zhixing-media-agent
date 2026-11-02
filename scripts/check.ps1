param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectDirectory 'backend\.venv\Scripts\python.exe'
$ruffPath = Join-Path $projectDirectory 'backend\.venv\Scripts\ruff.exe'
Push-Location -LiteralPath $projectDirectory
try {
    & $ruffPath check backend\app backend\tests backend\alembic
    if ($LASTEXITCODE -ne 0) { throw 'Ruff检查未通过。' }
    & $pythonPath -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Python依赖检查未通过。' }
    foreach ($scriptFile in Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' -File) {
        $tokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($scriptFile.FullName, [ref]$tokens, [ref]$parseErrors)
        if ($parseErrors.Count) { throw ('PowerShell语法错误：' + $scriptFile.Name) }
    }
    & (Join-Path $PSScriptRoot 'test.ps1')
    if (-not $SkipFrontend) { & (Join-Path $PSScriptRoot 'build-frontend.ps1') -SkipInstall }
} finally { Pop-Location }
