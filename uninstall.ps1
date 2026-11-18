<#
  TG MCP — удаление: закрывает приложение, убирает ярлыки и ключ автозапуска.
  Папка data\ (сессии Telegram, config.json с токеном MCP, журналы) остается, если не указан -RemoveData.

  Запуск:  powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-RemoveData] [-Force]
#>
param(
    [switch]$RemoveData,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$Root = $PSScriptRoot
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$VenvPyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$RunPyw = Join-Path $Root 'run.pyw'
$Data = Join-Path $Root 'data'
$ShortcutName = 'TG MCP.lnk'

function Get-AppProcesses {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($RunPyw) }
}

# 1. закрыть приложение: сначала штатно (как «Выход» в трее), потом принудительно
if (Get-AppProcesses) {
    Write-Host "Закрываю TG MCP…"
    if (Test-Path $VenvPy) { & $VenvPy $RunPyw --quit | Out-Null }
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-AppProcesses) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    $left = Get-AppProcesses
    if ($left) {
        Write-Host "Не закрылся сам — завершаю процессы."
        $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}

# 2. ярлыки
foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
    $p = Join-Path $folder $ShortcutName
    if (Test-Path $p) { Remove-Item $p -Force; Write-Host "Удален ярлык: $p" }
}

# 3. автозапуск
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if (Get-ItemProperty -Path $runKey -Name 'tg-mcp' -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $runKey -Name 'tg-mcp'
    Write-Host "Удален автозапуск (HKCU\...\Run\tg-mcp)"
}

# 4. данные — только по ключу
if ($RemoveData -and (Test-Path $Data)) {
    Write-Warning "В data\ — config.json (api_id, токен MCP, права), журналы и метаданные аккаунтов."
    Write-Warning "Ключи сессий Telegram (tg-mcp/<id>) и api_id/api_hash (tg-mcp / api_credentials) в Диспетчере учетных данных тоже будут удалены."
    Write-Warning "Сеансы на серверах Telegram при этом НЕ завершаются: перед удалением нажмите «Выйти» у аккаунта (раздел «Аккаунты»)"
    Write-Warning "или завершите сеанс «TG MCP Desktop» в Telegram: Настройки → Устройства."
    $ok = $Force
    if (-not $ok) { $ok = (Read-Host "Удалить data\ безвозвратно? (да/нет)") -match '^(д|да|y|yes)$' }
    if ($ok) {
        if (Test-Path $VenvPy) { & $VenvPy $RunPyw --forget-sessions | Out-Null }
        Remove-Item -Recurse -Force $Data
        Write-Host "Удалено: $Data"
    } else {
        Write-Host "data\ оставлена."
    }
} elseif (Test-Path $Data) {
    Write-Host "data\ оставлена (сессии и настройки). Удалить вместе с ней: uninstall.ps1 -RemoveData"
}

Write-Host "Готово. Папка проекта и .venv не удалялись."
