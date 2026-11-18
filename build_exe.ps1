<#
  TG MCP — reproducible exe build (PyInstaller, one file, no console).
  Result: dist\TG MCP.exe (+ dist\TG MCP.exe.sha256). Needs the project venv: python -m venv .venv
  Nothing secret is embedded: api_id/api_hash are entered by each user in the app.

  Run:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
#>
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$Root = $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Build = Join-Path $Root 'build'
$Dist = Join-Path $Root 'dist'
$Exe = Join-Path $Dist 'TG MCP.exe'
$PyInstallerVersion = '6.22.3'

if (-not (Test-Path $Py)) { throw "No project venv ($Py). Create it first: python -m venv .venv" }

Write-Host "Installing requirements and PyInstaller $PyInstallerVersion..."
& $Py -m pip install --disable-pip-version-check -q -r (Join-Path $Root 'requirements.txt') "pyinstaller==$PyInstallerVersion"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "Cleaning build\ and the previous exe..."
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
if (Test-Path $Exe) { Remove-Item -Force $Exe }

$piArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN',
    '--onefile', '--windowed',
    '--name', 'TG MCP',
    '--icon', (Join-Path $Root 'assets\tg-mcp.ico'),
    '--version-file', (Join-Path $Root 'packaging\version_info.txt'),
    '--workpath', $Build, '--distpath', $Dist, '--specpath', $Build,
    '--paths', $Root,
    # keyring: the Windows Credential Manager backend is discovered via entry points
    '--hidden-import', 'keyring.backends.Windows',
    '--hidden-import', 'win32ctypes.core',
    '--hidden-import', 'win32ctypes.pywin32',
    '--copy-metadata', 'keyring',
    # mcp / FastMCP reads its own version from package metadata
    '--copy-metadata', 'mcp',
    # uvicorn and python-socks import parts dynamically
    '--collect-submodules', 'uvicorn',
    '--collect-submodules', 'python_socks',
    '--hidden-import', 'qrcode.image.pil',
    '--hidden-import', 'cryptg',
    '--exclude-module', 'tkinter',
    '--exclude-module', 'pytest',
    '--exclude-module', 'pystray',
    (Join-Path $Root 'run.pyw')
)
Write-Host "Building (PyInstaller --onefile --windowed)..."
Push-Location $Root
try {
    & $Py @piArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
    if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
}

if (-not (Test-Path $Exe)) { throw "exe was not produced: $Exe" }
$item = Get-Item $Exe
$hash = (Get-FileHash -Algorithm SHA256 $Exe).Hash
Set-Content -Path "$Exe.sha256" -Value "$($hash.ToLower())  TG MCP.exe" -Encoding ascii
$ver = $item.VersionInfo
Write-Host ""
Write-Host "Done: $Exe"
Write-Host ("Size: {0:N1} MB ({1} bytes)" -f ($item.Length / 1MB), $item.Length)
Write-Host "SHA256: $hash"
Write-Host "Version: $($ver.ProductName) $($ver.ProductVersion)"
