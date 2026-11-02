param([ValidateSet('Start','Stop','Status')][string]$Action = 'Status')
$ErrorActionPreference = 'Stop'
if ($Action -eq 'Stop') {
    Write-Host '现在使用多个项目共用的 PostgreSQL。停止 Demo 只需 Ctrl+C；项目脚本不会停止公共数据库服务。'
} elseif ($Action -eq 'Start') {
    & "$PSScriptRoot\ensure-database.ps1"
} else {
    Get-Service -Name 'postgresql-x64-17' | Select-Object Name,Status,StartType
}