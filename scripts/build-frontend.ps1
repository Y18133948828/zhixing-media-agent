param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$originalPath = $env:PATH
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    $knownNode = Join-Path $env:LOCALAPPDATA 'Author Software\nvm\.nodejs\node.exe'
    if (Test-Path -LiteralPath $knownNode) { $env:PATH = (Split-Path $knownNode -Parent) + ';' + $env:PATH }
}
Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
try {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) { $npmCommand = Get-Command npm.exe -ErrorAction SilentlyContinue }
    if (-not $nodeCommand -or -not $npmCommand) { throw '需要 Node.js 22.12+ 和 npm。安装后重新打开 PowerShell 再运行。' }
    & $nodeCommand.Source -e 'const [major,minor]=process.versions.node.split(".").map(Number);if(major<22 || (major===22 && minor<12))process.exit(1)'
    if ($LASTEXITCODE -ne 0) { throw '请使用 Node.js 22.12+。' }
    if (-not $SkipInstall) {
        & $npmCommand.Source ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
    }
    & $npmCommand.Source run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
} finally {
    Pop-Location
    $env:PATH = $originalPath
}
