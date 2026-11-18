<#
  TG MCP — установка вручную (запускается пользователем один раз).
  Что делает: проверяет Python 3.13, создает venv .venv в папке проекта, ставит зависимости,
  создает ярлык «TG MCP» в меню Пуск (с -Desktop — и на рабочем столе).
  Ярлык ведет прямо на .venv\Scripts\pythonw.exe + run.pyw (без консоли и оберток).

  Запуск:  powershell -ExecutionPolicy Bypass -File install.ps1 [-Desktop]
#>
param(
    [switch]$Desktop
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$Root = $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$VenvPyw = Join-Path $Venv 'Scripts\pythonw.exe'
$RunPyw = Join-Path $Root 'run.pyw'
$Icon = Join-Path $Root 'assets\tg-mcp.ico'
$ShortcutName = 'TG MCP.lnk'

function Test-Py313([string[]]$cmd) {
    try {
        $exe = $cmd[0]; $rest = @(); if ($cmd.Count -gt 1) { $rest = $cmd[1..($cmd.Count - 1)] }
        & $exe @rest -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

Write-Host "TG MCP: установка в $Root"

# 1. Python 3.13
$candidates = @(
    @('py', '-3.13'),
    @('python'),
    @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'))
)
$Py = $null
foreach ($c in $candidates) {
    if (Test-Py313 $c) { $Py = $c; break }
}
if (-not $Py) {
    throw "Не найден Python 3.13. Установите его с python.org (галка «Add python.exe to PATH») и запустите install.ps1 снова."
}
Write-Host ("Python 3.13: " + ($Py -join ' '))

# 2. venv
if (-not (Test-Path $VenvPy)) {
    Write-Host "Создаю venv: $Venv"
    $exe = $Py[0]; $rest = @(); if ($Py.Count -gt 1) { $rest = $Py[1..($Py.Count - 1)] }
    & $exe @rest -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать venv" }
} else {
    Write-Host "venv уже есть: $Venv"
}

# 3. зависимости
Write-Host "Ставлю зависимости (requirements.txt)…"
& $VenvPy -m pip install --disable-pip-version-check -q --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip: не удалось обновить pip" }
& $VenvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "pip: не удалось поставить зависимости" }
& $VenvPy -c "import telethon, mcp, PySide6, PIL, qrcode, keyring; print('зависимости: ok, Telethon', telethon.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Проверка импорта зависимостей не прошла" }

# 4. ярлыки
function New-AppShortcut([string]$folder) {
    $path = Join-Path $folder $ShortcutName
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($path)
    $lnk.TargetPath = $VenvPyw
    $lnk.Arguments = '"' + $RunPyw + '" --show'
    $lnk.WorkingDirectory = $Root
    $lnk.Description = 'TG MCP — Telegram для Claude Code'
    if (Test-Path $Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
    Write-Host "Ярлык: $path"
}
New-AppShortcut ([Environment]::GetFolderPath('Programs'))
if ($Desktop) { New-AppShortcut ([Environment]::GetFolderPath('Desktop')) }

Write-Host ""
Write-Host "Готово. Дальше:"
Write-Host "  1. Запустите «TG MCP» из меню Пуск — откроется окно, в трее появится иконка-облачко"
Write-Host "     (может быть скрыта под стрелкой ^). Крестик окна сворачивает в трей; выход — ПКМ по иконке → «Выход»."
Write-Host "  2. Если Telegram у вас работает только через прокси: раздел «Прокси» → «Добавить прокси»"
Write-Host "     (MTProto с секретом FakeTLS ee… не поддерживается — используйте SOCKS5/HTTP или секрет dd…)."
Write-Host "  3. Раздел «Настройки» → api_id и api_hash (my.telegram.org → API development tools) → «Сохранить»."
Write-Host "  4. Раздел «Аккаунты» → метка (например «Личный») → «Продолжить» → отсканируйте QR в Telegram на телефоне"
Write-Host "     (Настройки → Устройства → «Подключить устройство»); облачный пароль — в том же разделе."
Write-Host "  5. Раздел «Права MCP» — по умолчанию у нового аккаунта «Только чтение»."
Write-Host "  6. Раздел «Статус» → «Скопировать адрес и токен MCP» → вставьте команду в терминал (claude mcp add …)."
Write-Host "Автозапуск при входе в Windows — тумблер в «Настройках». Удаление — uninstall.ps1."
